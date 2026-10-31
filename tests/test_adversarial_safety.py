"""独立安全回归；所有设备都是内存夹具，不能替代 Android/微信真机验收。"""
from __future__ import annotations

import unittest
import tempfile
import hashlib
import sqlite3
import traceback
import contextlib
import io
import pickle
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch
from types import SimpleNamespace

from wechatauto_android.controller import ACCOUNT_ROLES, CHAT_ROLES, IMPLEMENTED_ACTIONS, WeChat
from wechatauto_android.profile import Profile, Target
from wechatauto_android.errors import OutcomeUnknown, StaleSnapshot
from wechatauto_android.rooted import (AndroidDatabase, AdbRootSnapshotter, BoundDatabaseKey,
    CipherProfile, DatabaseOpenError, LegacyKeyCandidate, RootSnapshot, RootSnapshotConfig, UnsupportedSchema)
from wechatauto_android.keycapture import (CaptureConfig, CaptureUnavailable, FridaKeyCapture,
    HOOK_PROFILE, _Candidate, _Inbox, _SIGNATURE, _check_snapshot)

from wechatauto_android.archive import ExportArchive
from wechatauto_android.listener import Listener
from wechatauto_android.media import MediaDownloader
from wechatauto_android.errors import DuplicateOperation, IdentityMismatch, PermissionDenied
from wechatauto_android.models import DeviceIdentity
from wechatauto_android.safety import OperationJournal, SafetyPolicy
from xml.sax.saxutils import quoteattr

from wechatauto_android.errors import AmbiguousTarget, SelectorNotFound, ValidationError
from wechatauto_android.ui import Hierarchy, Selector, Node
from wechatauto_android.adapter import Uiautomator2Adapter


def xml_nodes(*names: str) -> str:
    children = ''.join(
        f'<node text={quoteattr(name)} resource-id="com.tencent.mm:id/target" '
        'package="com.tencent.mm" class="android.widget.TextView" '
        'enabled="true" clickable="true" bounds="[0,0][100,40]" />'
        for name in names
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><hierarchy>{children}</hierarchy>'


class ExactTargetAdversarialTests(unittest.TestCase):
    def test_duplicate_exact_labels_fail_closed(self):
        tree = Hierarchy.parse(xml_nodes('同名联系人', '同名联系人'))
        with self.assertRaises(AmbiguousTarget):
            tree.one(Selector(text='同名联系人'))

    def test_substring_is_not_identity(self):
        tree = Hierarchy.parse(xml_nodes('张三同事', '旧张三'))
        with self.assertRaises(SelectorNotFound):
            tree.one(Selector(text='张三'))

    def test_unicode_target_identity_is_not_normalized_or_trimmed(self):
        pairs = [
            ('é', 'e\u0301'), ('Alice', 'alice'), ('张三', '张三 '),
            ('张三', '\u200b张三'), ('Ａlice', 'Alice'),
            ('👩\u200d💻', '👩💻'), ('好友\ufe0f', '好友'),
            ('a b', 'a\u00a0b'),
        ]
        for requested, other in pairs:
            with self.subTest(requested=requested, other=other):
                tree = Hierarchy.parse(xml_nodes(other))
                with self.assertRaises(SelectorNotFound):
                    tree.one(Selector(text=requested))
                self.assertEqual(tree.one(Selector(text=other)).text, other)

    def test_package_disambiguates_foreign_overlay(self):
        xml = xml_nodes('发送').replace('com.tencent.mm" class=', 'com.evil.overlay" class=')
        with self.assertRaises(SelectorNotFound):
            Hierarchy.parse(xml).one(Selector(text='发送', package='com.tencent.mm'))

    def test_all_selector_fields_are_conjunctive(self):
        tree = Hierarchy.parse(xml_nodes('发送'))
        with self.assertRaises(SelectorNotFound):
            tree.one(Selector(text='发送', resource_id='com.tencent.mm:id/not_send'))

    def test_boolean_only_selector_is_rejected(self):
        for kwargs in ({}, {'enabled': True}, {'clickable': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                Selector(**kwargs)

    def test_unknown_selector_fields_are_rejected_not_ignored(self):
        for key in ('textContains', 'instance', 'xpath', 'allow_first_match'):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                Selector.from_dict({'text': '发送', key: True})

    def test_xml_attributes_preserve_quotes_and_ampersands(self):
        name = '"\' & <测试>😀'
        node = Hierarchy.parse(xml_nodes(name)).one(Selector(text=name))
        self.assertEqual(node.text, name)

    def test_selector_round_trip_preserves_exact_content(self):
        original = Selector(text=' e\u0301\u200b ', resource_id='x', package='com.tencent.mm', enabled=False)
        self.assertEqual(Selector.from_dict(original.to_dict()), original)

    def test_fingerprint_detects_geometry_and_text_drift(self):
        a = Hierarchy.parse(xml_nodes('旧'))
        b = Hierarchy.parse(xml_nodes('新'))
        c = Hierarchy.parse(xml_nodes('旧').replace('[100,40]', '[101,40]'))
        self.assertEqual(len({a.fingerprint, b.fingerprint, c.fingerprint}), 3)

    def test_xml_external_entity_is_not_resolved(self):
        xml = '<!DOCTYPE hierarchy [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><hierarchy><node text="&xxe;" bounds="[0,0][1,1]" /></hierarchy>'
        with self.assertRaises(ValidationError):
            Hierarchy.parse(xml)

    def test_xml_internal_entity_expansion_is_rejected(self):
        xml = '<!DOCTYPE hierarchy [<!ENTITY a "expanded">]><hierarchy><node text="&a;" bounds="[0,0][1,1]" /></hierarchy>'
        with self.assertRaises(ValidationError):
            Hierarchy.parse(xml)

    def test_malformed_xml_fails_as_validation(self):
        with self.assertRaises(ValidationError):
            Hierarchy.parse('<hierarchy><node></hierarchy>')



def archive_data(**extra):
    return dict(schema_version=1, account_id='self', export_id='qa-source', messages=[], **extra)


def message_row(**extra):
    return dict(id='1', chat_id='friend', sequence=1, sender_id='sender', content='正文', **extra)


class ArchiveProvenanceAdversarialTests(unittest.TestCase):
    def test_imported_collections_cannot_claim_device_verified_origin(self):
        for collection in ('contacts', 'groups', 'labels', 'moments'):
            with self.subTest(collection=collection), self.assertRaises(ValidationError):
                ExportArchive(archive_data(**{collection: [{'id': 'x', 'origin': 'device_verified'}]}), 'self')

    def test_imported_member_cannot_claim_device_origin(self):
        with self.assertRaises(ValidationError):
            ExportArchive(archive_data(groups=[{'id': 'g', 'members': [{'id': 'f', 'origin': 'device_verified'}]}]), 'self')

    def test_message_origin_cannot_claim_live_device_evidence(self):
        data = archive_data()
        data['messages'] = [message_row(origin='android_ui')]
        with self.assertRaises(ValidationError):
            ExportArchive(data, 'self')

    def test_message_account_cannot_be_swapped(self):
        data = archive_data()
        data['messages'] = [message_row(account_id='other')]
        with self.assertRaises(IdentityMismatch):
            ExportArchive(data, 'self')

    def test_message_ids_are_scoped_to_exact_chat(self):
        data = archive_data()
        a = message_row()
        b = dict(a, chat_id='other', content='不同消息')
        data['messages'] = [a, b]
        archive = ExportArchive(data, 'self')
        self.assertEqual(archive.get_message_by_id('friend', '1').content, '正文')
        self.assertEqual(archive.get_message_by_id('other', '1').content, '不同消息')

    def test_conflicting_duplicate_message_id_fails(self):
        data = archive_data()
        data['messages'] = [message_row(), dict(message_row(), content='changed')]
        with self.assertRaises(ValidationError):
            ExportArchive(data, 'self')

    def test_returned_metadata_cannot_mutate_archive_evidence(self):
        data = archive_data()
        data['messages'] = [message_row(metadata={'file': {'path': 'safe'}})]
        archive = ExportArchive(data, 'self')
        message = archive.get_message_by_id('friend', '1')
        message.metadata['file']['path'] = '../../secret'
        self.assertEqual(archive.get_message_by_id('friend', '1').metadata['file']['path'], 'safe')

    def test_tied_sequences_can_be_paged_without_loss(self):
        data = archive_data()
        data['messages'] = [dict(message_row(), id=str(n)) for n in range(10)]
        archive = ExportArchive(data, 'self')
        cursor, seen = None, []
        while batch := archive.get_messages_after('friend', cursor, limit=3):
            seen.extend(m.id for m in batch)
            cursor = (batch[-1].sequence, batch[-1].id)
        self.assertEqual(seen, [str(n) for n in range(10)])


class PolicyAndJournalAdversarialTests(unittest.TestCase):
    def policy(self, **changes):
        values = dict(serial='serial-1', account_id='self', allowed_targets=frozenset({'friend'}),
                      allowed_actions=frozenset({'send_text'}), execute=True)
        values.update(changes)
        return SafetyPolicy(**values)

    def test_default_dry_run_rejects_authorization(self):
        with self.assertRaises(PermissionDenied):
            self.policy(execute=False).authorize('send_text', 'friend')

    def test_both_exact_action_and_target_required(self):
        policy = self.policy()
        for action, target in [('send_text', 'Friend'), ('send_text', 'friend '),
                               ('forward', 'friend'), ('send_text', '*')]:
            with self.subTest(action=action, target=target), self.assertRaises(PermissionDenied):
                policy.authorize(action, target)
        policy.authorize('send_text', 'friend')

    def test_serial_package_version_are_jointly_bound(self):
        policy = self.policy()
        policy.verify_device(DeviceIdentity('serial-1', 'com.tencent.mm', '8.0.1', sdk=30), 'com.tencent.mm', '8.0.1')
        for identity in [DeviceIdentity('other', 'com.tencent.mm', '8.0.1', sdk=30),
                         DeviceIdentity('serial-1', 'other.package', '8.0.1', sdk=30),
                         DeviceIdentity('serial-1', 'com.tencent.mm', '8.0.2', sdk=30)]:
            with self.subTest(identity=identity), self.assertRaises(IdentityMismatch):
                policy.verify_device(identity, 'com.tencent.mm', '8.0.1')

    def test_unknown_result_operation_survives_process_style_reopen(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'journal.sqlite'
            first = OperationJournal(path)
            first.reserve('operation-001', self.policy(), 'send_text', 'friend', {'body': '秘密内容'})
            first.mark('operation-001', 'committing')
            first.mark('operation-001', 'unknown')
            reopened = OperationJournal(path)
            self.assertEqual(reopened.status('operation-001'), 'unknown')
            with self.assertRaises(DuplicateOperation):
                reopened.reserve('operation-001', self.policy(), 'send_text', 'friend', {'body': '秘密内容'})
            self.assertNotIn('秘密内容'.encode(), path.read_bytes())

    def test_duplicate_operation_cannot_be_reused_for_different_payload_or_account(self):
        with tempfile.TemporaryDirectory() as root:
            journal = OperationJournal(Path(root) / 'journal.sqlite')
            journal.reserve('operation-002', self.policy(), 'send_text', 'friend', {'body': 'a'})
            with self.assertRaises(DuplicateOperation):
                journal.reserve('operation-002', self.policy(account_id='other'), 'send_text', 'friend', {'body': 'b'})

    def test_journal_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'target'
            target.write_text('do not modify')
            link = Path(root) / 'link'
            link.symlink_to(target)
            with self.assertRaises(ValidationError):
                OperationJournal(link)
            self.assertEqual(target.read_text(), 'do not modify')



    def test_dangling_journal_symlink_cannot_create_outside_file(self):
        with tempfile.TemporaryDirectory() as root:
            outside = Path(root) / 'must-not-be-created'
            link = Path(root) / 'journal-link'
            link.symlink_to(outside)
            with self.assertRaises(ValidationError):
                OperationJournal(link)
            self.assertFalse(outside.exists())



def synthetic_profile(*, calibrated=True):
    roles = {name: {'resource_id': 'qa:' + name}
             for name in ACCOUNT_ROLES + CHAT_ROLES + IMPLEMENTED_ACTIONS['send_text']}
    roles['account.identity']['text'] = '{account_id}'
    roles['chat.identity']['text'] = '{chat_id}'
    roles['chat.title']['text'] = '{title}'
    return Profile('qa-only-not-device-calibration', '8.0.1', roles,
                   calibrated=calibrated, evidence='synthetic unit fixture only', android_sdks=(30,))


class ControllerCommitAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Path(self.temp.name) / 'operations.sqlite'
        self.adapter = Mock()
        self.adapter.click.return_value = None
        self.target = Target('friend', '测试联系人')

    def client(self, *, execute=True, calibrated=True, allowed_target='friend'):
        policy = SafetyPolicy('serial-1', 'self', frozenset({allowed_target}),
                              frozenset({'send_text'}), execute=execute, assume_exclusive_device=True)
        return WeChat(self.adapter, synthetic_profile(calibrated=calibrated), policy,
                      self.journal, transition_timeout=0.01)

    def test_generic_title_selector_cannot_bypass_target_title_readback(self):
        client = self.client()
        roles = {name: dict(selector) for name, selector in client.profile.roles.items()}
        roles['chat.title'].pop('text')
        client.profile = replace(client.profile, roles=roles)
        with self.assertRaises(PermissionDenied):
            client.send_text(self.target, '消息', operation_id='qa-unbound-title')
        self.assertEqual(self.adapter.mock_calls, [])
        self.assertFalse(self.journal.exists())

    def test_strict_mode_without_fresh_account_proof_stops_before_navigation(self):
        client = self.client()
        client.policy = replace(client.policy, assume_exclusive_device=False)
        with self.assertRaises(PermissionDenied):
            client.send_text(self.target, '消息', operation_id='qa-strict-proof')
        self.assertEqual(self.adapter.mock_calls, [])
        self.assertFalse(self.journal.exists())

    def test_independent_precommit_account_drift_blocks_send(self):
        client = self.client()
        client.policy = replace(client.policy, assume_exclusive_device=False)
        client.account_verifier = Mock(return_value='another-account')
        client._verify_account = Mock()
        client._open_chat = Mock()
        snapshot = Hierarchy.parse(xml_nodes('发送'))
        with self.assertRaises(IdentityMismatch):
            client._run('send_text', self.target, 'qa-account-drift', {},
                        lambda: (Selector(text='发送'), snapshot))
        self.adapter.click.assert_not_called()
        self.assertEqual(OperationJournal(self.journal).status('qa-account-drift'), 'failed_before_commit')

    def test_dry_run_never_touches_adapter_or_creates_journal(self):
        result = self.client(execute=False).send_text(self.target, '消息', operation_id='qa-dry-run')
        self.assertEqual(result.status, 'dry_run')
        self.assertFalse(result.success)
        self.assertEqual(self.adapter.mock_calls, [])
        self.assertFalse(self.journal.exists())

    def test_uncalibrated_profile_cannot_touch_adapter(self):
        with self.assertRaises(PermissionDenied):
            self.client(calibrated=False).send_text(self.target, '消息', operation_id='qa-uncalibrated')
        self.assertEqual(self.adapter.mock_calls, [])

    def test_out_of_scope_target_cannot_touch_adapter(self):
        with self.assertRaises(PermissionDenied):
            self.client(allowed_target='another').send_text(self.target, '消息', operation_id='qa-scope')
        self.assertEqual(self.adapter.mock_calls, [])

    def test_ambiguous_commit_exception_is_durable_and_never_retried(self):
        client = self.client()
        client._verify_account = Mock()
        client._open_chat = Mock()
        self.adapter.click.side_effect = ConnectionError('response lost after click')
        snapshot = Hierarchy.parse(xml_nodes('发送'))
        prepare = Mock(return_value=(Selector(text='发送'), snapshot))
        with self.assertRaises(OutcomeUnknown):
            client._run('send_text', self.target, 'qa-unknown-001', {'text': '消息'}, prepare)
        self.assertEqual(OperationJournal(self.journal).status('qa-unknown-001'), 'unknown')
        with self.assertRaises(DuplicateOperation):
            client._run('send_text', self.target, 'qa-unknown-001', {'text': '消息'}, prepare)
        self.adapter.click.assert_called_once()
        client._verify_account.assert_called_once()
        prepare.assert_called_once()

    def test_no_rpc_ack_cannot_be_reported_verified(self):
        client = self.client()
        client._verify_account = Mock()
        client._open_chat = Mock()
        self.adapter.click.return_value = False
        snapshot = Hierarchy.parse(xml_nodes('发送'))
        with self.assertRaises(OutcomeUnknown):
            client._run('send_text', self.target, 'qa-false-001', {},
                        lambda: (Selector(text='发送'), snapshot))
        self.assertEqual(OperationJournal(self.journal).status('qa-false-001'), 'unknown')

    def test_successful_rpc_without_postcondition_is_only_submitted(self):
        client = self.client()
        client._verify_account = Mock()
        client._open_chat = Mock()
        snapshot = Hierarchy.parse(xml_nodes('发送'))
        result = client._run('send_text', self.target, 'qa-submitted-001', {},
                             lambda: (Selector(text='发送'), snapshot))
        self.assertEqual(result.status, 'submitted')
        self.assertFalse(result.success)

    def test_explicit_failed_navigation_ack_stops_before_next_stage(self):
        client = self.client()
        client._wait = Mock(return_value=Hierarchy.parse(xml_nodes('发送')))
        self.adapter.click.return_value = False
        with self.assertRaises(OutcomeUnknown):
            client._click('home.me')

    def test_explicit_failed_back_ack_stops_before_next_stage(self):
        client = self.client()
        client._snapshot = Mock()
        client._wait = Mock()
        self.adapter.press_back.return_value = False
        with self.assertRaises(OutcomeUnknown):
            client._back('home.marker')
        client._wait.assert_not_called()



class ListenerCancellationAdversarialTests(unittest.TestCase):
    def source(self):
        data = archive_data()
        data['messages'] = [dict(message_row(), id=str(i), sequence=i) for i in range(1, 4)]
        return ExportArchive(data, 'self')

    def test_callback_stop_prevents_later_rows(self):
        listener = Listener(self.source(), 'self')
        seen = []
        def callback(message, owner):
            seen.append(message.id)
            owner.stop()
        listener.add_listener('friend', callback)
        listener.poll_once()
        self.assertEqual(seen, ['1'])

    def test_callback_removal_takes_effect_before_later_rows(self):
        listener = Listener(self.source(), 'self')
        seen = []
        def callback(message, owner):
            seen.append(message.id)
            owner.remove_listener('friend', callback)
        listener.add_listener('friend', callback)
        listener.poll_once()
        self.assertEqual(seen, ['1'])

    def test_second_removed_callback_is_not_called(self):
        listener = Listener(self.source(), 'self')
        second = Mock()
        def first(message, owner):
            owner.remove_listener('friend', second)
        listener.add_listener('friend', first)
        listener.add_listener('friend', second)
        listener.poll_once()
        second.assert_not_called()

    def test_callback_failure_does_not_advance_failed_message(self):
        listener = Listener(self.source(), 'self')
        seen = []
        def fail(message, _):
            seen.append(message.id)
            raise RuntimeError('callback failure')
        listener.add_listener('friend', fail)
        with self.assertRaises(RuntimeError):
            listener.poll_once()
        self.assertNotIn('friend', listener.watermark)
        with self.assertRaises(RuntimeError):
            listener.poll_once()
        self.assertEqual(seen, ['1', '1'])

    def test_changed_source_account_stops_before_callback(self):
        source = self.source()
        listener = Listener(source, 'self')
        callback = Mock()
        listener.add_listener('friend', callback)
        source.account_id = 'other'
        with self.assertRaises(IdentityMismatch):
            listener.poll_once()
        callback.assert_not_called()


class MediaPathAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.output = self.root / 'output'
        self.source.mkdir()
        self.output.mkdir()
        (self.source / 'ok').write_bytes(b'known media')
        self.downloader = MediaDownloader(self.source, self.output)

    def test_traversal_or_absolute_media_path_never_writes(self):
        for value in ('../outside', '/etc/passwd', 'a/../../outside', 'a//b', 'a/./b', 'C:\\secret', 'a\\b'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.downloader.download(value)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_source_symlink_leaf_and_parent_escape_are_rejected(self):
        external = self.root / 'outside'
        external.mkdir()
        (external / 'secret').write_text('secret')
        (self.source / 'leaf').symlink_to(external / 'secret')
        (self.source / 'parent').symlink_to(external, target_is_directory=True)
        for value in ('leaf', 'parent/secret'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.downloader.download(value)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_output_symlink_leaf_cannot_overwrite_outside(self):
        outside = self.root / 'important'
        outside.write_text('unchanged')
        (self.output / 'destination').symlink_to(outside)
        with self.assertRaises(ValidationError):
            self.downloader.download('ok', destination='destination')
        self.assertEqual(outside.read_text(), 'unchanged')

    def test_output_symlink_parent_cannot_redirect_copy(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.output / 'escape').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValidationError):
            self.downloader.download('ok', destination='escape/copied')
        self.assertEqual(list(outside.iterdir()), [])

    def test_checksum_mismatch_leaves_no_final_or_partial_file(self):
        with self.assertRaises(ValidationError):
            self.downloader.download('ok', sha256='0' * 64)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_source_output_overlap_is_rejected(self):
        with self.assertRaises(ValidationError):
            MediaDownloader(self.source, self.source)
        nested = self.source / 'nested'
        nested.mkdir()
        with self.assertRaises(ValidationError):
            MediaDownloader(self.source, nested)



def root_config(**changes):
    kwargs = dict(serial='serial-1', account_dir='a' * 32, account_id='wxid_self',
                  expected_wechat_version='8.0.1', consent_root_read=True, confirm_quiesced=True)
    kwargs.update(changes)
    return RootSnapshotConfig(**kwargs)


def native_db(path, *, duplicate_id=False):
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE userinfo(id INTEGER, value TEXT)')
        connection.execute('INSERT INTO userinfo VALUES(2, ?)', ('wxid_self',))
        connection.execute('CREATE TABLE message(msgId INTEGER, msgSvrId INTEGER, talker TEXT, content TEXT, type INTEGER, createTime INTEGER, isSend INTEGER)')
        connection.execute('INSERT INTO message VALUES(1, 1, "friend", "original", 1, 1700000000000, 0)')
        if duplicate_id:
            connection.execute('INSERT INTO message VALUES(1, 2, "friend", "duplicate", 1, 1700000000001, 0)')


class RootScopeAdversarialTests(unittest.TestCase):
    def test_root_requires_both_explicit_consent_and_quiescence(self):
        for changes in ({'consent_root_read': False}, {'confirm_quiesced': False},
                        {'consent_root_read': 1}, {'confirm_quiesced': 'yes'}):
            with self.subTest(changes=changes), self.assertRaises(PermissionDenied):
                root_config(**changes)

    def test_root_shell_scope_is_strictly_validated(self):
        for changes in ({'serial': 's;id'}, {'serial': 's\n'},
                        {'account_dir': '../' + 'a' * 29}, {'account_dir': 'A' * 32},
                        {'android_user': True}, {'android_user': -1},
                        {'expected_wechat_version': '8.*'}, {'expected_wechat_version': '7.0.0'}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                root_config(**changes)

    def test_existing_snapshot_target_blocks_before_device_access(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshotter = AdbRootSnapshotter(root_config())
            snapshotter._run = Mock()
            with self.assertRaises(ValidationError):
                snapshotter.capture(directory)
            snapshotter._run.assert_not_called()

    def test_wechat_secondary_process_prevents_snapshot(self):
        snapshotter = AdbRootSnapshotter(root_config())
        snapshotter._small = Mock(return_value='NAME\ninit\ncom.tencent.mm:tools')
        with self.assertRaises(PermissionDenied):
            snapshotter._quiescent()

    def test_changed_root_bundle_is_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            body = b'not a live database; transport-only fixture'
            record = {'EnMicroMsg.db': {'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}}
            snapshotter = AdbRootSnapshotter(root_config())
            snapshotter._verify_device = Mock(return_value=30)
            snapshotter._quiescent = Mock()
            snapshotter._manifest = Mock(side_effect=[record, {}])
            def fake_run(args, *, output=None, limit=0):
                output.write_bytes(body)
                return b''
            snapshotter._run = fake_run
            output = Path(directory) / 'snapshot'
            with self.assertRaises(StaleSnapshot):
                snapshotter.capture(output)
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_android_sdk_drift_during_snapshot_cannot_publish_stale_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            body = b'transport-only synthetic database bytes'
            record = {'EnMicroMsg.db': {'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}}
            snapshotter = AdbRootSnapshotter(root_config())
            snapshotter._verify_device = Mock(side_effect=[30, 31])
            snapshotter._quiescent = Mock()
            snapshotter._manifest = Mock(return_value=record)
            def fake_run(args, *, output=None, limit=0):
                output.write_bytes(body)
                return b''
            snapshotter._run = fake_run
            output = Path(directory) / 'snapshot'
            with self.assertRaises((StaleSnapshot, IdentityMismatch)):
                snapshotter.capture(output)
            self.assertFalse(output.exists())

    def test_root_manifest_does_not_authenticate_caller_asserted_account(self):
        with tempfile.TemporaryDirectory() as directory:
            body = b'not a live database; transport-only fixture'
            record = {'EnMicroMsg.db': {'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}}
            snapshotter = AdbRootSnapshotter(root_config())
            snapshotter._verify_device = Mock(return_value=30)
            snapshotter._quiescent = Mock()
            snapshotter._manifest = Mock(return_value=record)
            def fake_run(args, *, output=None, limit=0):
                output.write_bytes(body)
                return b''
            snapshotter._run = fake_run
            result = snapshotter.capture(Path(directory) / 'snapshot')
            self.assertFalse(result.manifest['account_identity_verified'])
            self.assertEqual(result.manifest['asserted_account_id'], 'wxid_self')


class DatabaseKeyAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'EnMicroMsg.db'

    def open(self, **kwargs):
        return AndroidDatabase(self.path, 'wxid_self', expected_wechat_version='8.0.1', **kwargs)

    def test_database_key_repr_hides_secret_and_binding_mismatch_denies(self):
        secret = 'synthetic-only-sensitive-key'
        profile = CipherProfile(4, 4096)
        bound = BoundDatabaseKey('wxid_self', 'a' * 64, '8.0.1', profile, secret)
        self.assertNotIn(secret, repr(bound))
        for arguments in [('other', 'a' * 64, '8.0.1', profile),
                          ('wxid_self', 'b' * 64, '8.0.1', profile),
                          ('wxid_self', 'a' * 64, '8.0.2', profile),
                          ('wxid_self', 'a' * 64, '8.0.1', CipherProfile(3, 1024))]:
            with self.subTest(arguments=arguments), self.assertRaises(IdentityMismatch):
                bound.for_database(*arguments)

    def test_legacy_candidate_repr_hides_input_material(self):
        candidate = LegacyKeyCandidate('8.0.1', 'SYNTHETIC_DEVICE_IDENTIFIER', '123456789012345')
        for value in ('SYNTHETIC_DEVICE_IDENTIFIER', '123456789012345', candidate()):
            self.assertNotIn(value, repr(candidate))

    def test_key_provider_exception_is_redacted_from_traceback(self):
        native_db(self.path)
        secret = 'SYNTHETIC_SECRET_DO_NOT_LOG'
        def provider():
            raise RuntimeError(secret)
        with patch('wechatauto_android.rooted.importlib.import_module', return_value=sqlite3):
            try:
                self.open(key_provider=provider, cipher_profile=CipherProfile(4, 4096))
            except DatabaseOpenError as error:
                self.assertNotIn(secret, ''.join(traceback.format_exception(error)))
            else:
                self.fail('Provider failure was not rejected')

    def test_wrong_database_self_field_is_not_accepted_as_requested_account(self):
        native_db(self.path)
        with sqlite3.connect(self.path) as connection:
            connection.execute('UPDATE userinfo SET value="different" WHERE id=2')
        with self.assertRaises(IdentityMismatch):
            self.open()

    def test_unknown_native_schema_does_not_become_empty_history(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute('CREATE TABLE completely_unknown(data TEXT)')
        with self.assertRaises(UnsupportedSchema):
            self.open()

    def test_sqlite_header_alone_is_not_integrity_proof(self):
        self.path.write_bytes(b'SQLite format 3\x00' + b'broken database' * 300)
        with self.assertRaises(DatabaseOpenError):
            self.open()

    def test_snapshot_change_cannot_emit_old_fingerprint_provenance(self):
        native_db(self.path)
        database = self.open()
        self.addCleanup(database.close)
        with sqlite3.connect(self.path) as connection:
            connection.execute('UPDATE message SET content="changed after validation"')
        with self.assertRaises(StaleSnapshot):
            database.get_messages('friend')

    def test_duplicate_native_message_ids_cannot_silently_break_cursor(self):
        native_db(self.path, duplicate_id=True)
        with self.assertRaises(UnsupportedSchema):
            self.open()

    def test_nonempty_rollback_journal_is_rejected_before_read(self):
        native_db(self.path)
        self.path.with_name(self.path.name + '-journal').write_bytes(b'uncommitted')
        with self.assertRaises(StaleSnapshot):
            self.open()

    def test_cipher_profile_cannot_inject_sql_or_guess_unknown_algorithm(self):
        for kwargs in ({'page_size': '4096; DROP TABLE message'},
                       {'compatibility': True}, {'kdf_algorithm': 'AUTO'},
                       {'hmac_algorithm': 'HMAC_SHA1; SELECT 1'}):
            params = dict(compatibility=4, page_size=4096)
            params.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                CipherProfile(**params)



def capture_config(**changes):
    kwargs = dict(serial='serial-1', account_dir='a' * 32, account_id='wxid_self',
                  expected_wechat_version='8.0.1', consent_attach=True,
                  allow_unverified_profile=True)
    kwargs.update(changes)
    return CaptureConfig(**kwargs)


class KeyCaptureAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.config = capture_config()
        self.profile = CipherProfile.legacy_android()
        self.inbox = _Inbox(self.config, self.profile, 'qa-nonce', 30)

    def event(self, kind, **extras):
        payload = dict(protocol=1, nonce='qa-nonce', kind=kind, package='com.tencent.mm',
                       version='8.0.1', android_user=0, api=30, profile=HOOK_PROFILE)
        payload.update(extras)
        return {'type': 'send', 'payload': payload}

    def ready(self):
        self.inbox.on_message(self.event('ready', signature=list(_SIGNATURE)), None)
        self.assertTrue(self.inbox.ready)

    def candidate(self, **changes):
        values = dict(path=self.config.paths[0], seq=1, cipher=dict(cipher=None,
                      page_size=self.profile.page_size, kdf_iterations=self.profile.kdf_iterations,
                      use_hmac=self.profile.use_hmac, kdf_algorithm=self.profile.kdf_algorithm,
                      hmac_algorithm=self.profile.hmac_algorithm, compatibility=self.profile.compatibility))
        values.update(changes)
        return self.event('candidate', **values)

    def snapshot(self, root):
        directory = Path(root)
        body = b'opaque transport-only fixture; not a genuine cipher fixture'
        path = directory / 'EnMicroMsg.db'
        path.write_bytes(body)
        manifest = dict(schema_version=1, kind='android_rooted_quiesced_snapshot',
                        serial=self.config.serial, package='com.tencent.mm', android_user=0,
                        android_api=30, wechat_version='8.0.1', account_dir='a' * 32,
                        asserted_account_id='wxid_self', account_identity_verified=False,
                        files={'EnMicroMsg.db': {'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}})
        return RootSnapshot(directory, manifest)

    def test_session_detach_is_attempted_even_if_script_unload_is_interrupted(self):
        callback = {}
        script = Mock()
        script.on.side_effect = lambda name, handler: callback.setdefault(name, handler)
        script.load.side_effect = lambda: callback['message']({'type': 'error', 'description': 'synthetic'}, None)
        script.unload.side_effect = KeyboardInterrupt()
        session = Mock()
        session.create_script.return_value = script
        device = Mock()
        device.id, device.type = self.config.serial, 'usb'
        device.attach.return_value = session
        frida = SimpleNamespace(__version__='16.7.19', Cancellable=lambda: MagicMock(),
                                get_device=Mock(return_value=device))
        with patch('wechatauto_android.keycapture._check_snapshot', return_value=30), \
             patch('wechatauto_android.keycapture._source', return_value='// synthetic lifecycle test only'), \
             patch('wechatauto_android.keycapture.importlib.import_module', return_value=frida):
            with self.assertRaises((CaptureUnavailable, OutcomeUnknown, KeyboardInterrupt)):
                FridaKeyCapture(self.config).capture(None, self.profile)
        session.detach.assert_called_once()

    def test_root_consent_does_not_implicitly_grant_process_attach(self):
        for changes in ({'consent_attach': False}, {'allow_unverified_profile': False},
                        {'consent_attach': 1}, {'allow_unverified_profile': 'yes'}):
            with self.subTest(changes=changes), self.assertRaises(PermissionDenied):
                capture_config(**changes)

    def test_candidate_before_ready_is_rejected_and_not_retained(self):
        self.inbox.on_message(self.candidate(), b'synthetic-secret')
        self.assertIsNotNone(self.inbox.failure)
        self.assertEqual(len(self.inbox.candidates), 0)

    def test_wrong_account_directory_candidate_is_rejected(self):
        self.ready()
        path = self.config.paths[0].replace('a' * 32, 'b' * 32)
        self.inbox.on_message(self.candidate(path=path), b'synthetic-secret')
        self.assertIsNotNone(self.inbox.failure)
        self.assertEqual(len(self.inbox.candidates), 0)

    def test_wrong_scope_or_replayed_event_is_rejected(self):
        for key, value in [('nonce', 'other'), ('package', 'com.other'), ('version', '8.0.2'),
                           ('android_user', 1), ('api', 31), ('seq', 0)]:
            with self.subTest(key=key):
                self.inbox = _Inbox(self.config, self.profile, 'qa-nonce', 30)
                self.ready()
                event = self.candidate()
                event['payload'][key] = value
                self.inbox.on_message(event, b'synthetic-secret')
                self.assertIsNotNone(self.inbox.failure)
                self.assertEqual(len(self.inbox.candidates), 0)

    def test_remote_exception_payload_never_prints_or_surfaces_secret(self):
        secret = 'SYNTHETIC_PRIVATE_FRIDA_DETAIL'
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.inbox.on_message({'type': 'error', 'description': secret, 'stack': secret}, None)
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn(secret, self.inbox.failure)

    def test_candidate_bounds_fail_closed(self):
        self.ready()
        self.inbox.on_message(self.candidate(), b'x' * 4097)
        self.assertIsNotNone(self.inbox.failure)
        self.assertFalse(self.inbox.candidates)

    def test_non_utf8_candidate_is_never_lossily_decoded(self):
        material = b'\xff\xfeSYNTHETIC'
        candidate = _Candidate(material)
        self.assertEqual(candidate.passphrase(), material)
        self.assertNotIn('SYNTHETIC', repr(candidate))
        candidate.clear()
        self.assertEqual(bytes(candidate._material), b'\x00' * len(material))
        candidate.clear()
        self.assertEqual(bytes(candidate._material), b'\x00' * 11)

    def test_candidate_repr_pickle_and_close_do_not_expose_secret(self):
        self.ready()
        secret = b'SYNTHETIC_PRIVATE_CANDIDATE'
        self.inbox.on_message(self.candidate(), secret)
        candidate = self.inbox.candidates[0]
        self.assertNotIn(secret.decode(), repr(candidate))
        with self.assertRaises(TypeError):
            pickle.dumps(candidate)
        self.inbox.close()
        self.assertEqual(bytes(candidate._material), b'\x00' * len(secret))
        self.assertFalse(self.inbox.candidates)

    def test_changed_snapshot_is_rejected_before_any_frida_import(self):
        with tempfile.TemporaryDirectory() as root:
            snapshot = self.snapshot(root)
            snapshot.database_path.write_bytes(b'changed')
            with patch('wechatauto_android.keycapture.importlib.import_module') as importer:
                with self.assertRaises((IdentityMismatch, ValidationError)):
                    FridaKeyCapture(self.config).capture(snapshot, self.profile)
                importer.assert_not_called()

    def test_snapshot_scope_and_manifest_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            snapshot = self.snapshot(root)
            snapshot.manifest['serial'] = 'another-device'
            with self.assertRaises(IdentityMismatch):
                _check_snapshot(snapshot, self.config)
            snapshot.manifest['serial'] = self.config.serial
            snapshot.manifest['files']['../../other'] = {'size': 1, 'sha256': '0' * 64}
            with self.assertRaises(ValidationError):
                _check_snapshot(snapshot, self.config)

    def test_unmanifested_wal_is_rejected_before_capture(self):
        with tempfile.TemporaryDirectory() as root:
            snapshot = self.snapshot(root)
            (Path(root) / 'EnMicroMsg.db-wal').write_bytes(b'new-unmanifested-wal')
            with self.assertRaises(IdentityMismatch):
                _check_snapshot(snapshot, self.config)



class ScopedGestureAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.device = Mock()
        self.device.serial = 'serial-1'
        self.device.app_current.return_value = {'package': 'com.tencent.mm'}
        self.device.app_info.return_value = {'versionName': '8.0.1'}
        self.device.info = {'screenOn': True, 'currentPackageName': 'com.tencent.mm', 'sdkInt': 30}
        self.device.dump_hierarchy.return_value = xml_nodes('发送')
        self.device.window_size.return_value = (1080, 1920)
        self.device.jsonrpc.click.return_value = True
        self.adapter = Uiautomator2Adapter('serial-1', _device=self.device)

    def test_scoped_node_cannot_forge_coordinates_from_valid_snapshot(self):
        snapshot = self.adapter.hierarchy()
        node = snapshot.one(Selector(text='发送'))
        forged = Node(node.attrs, (500, 500, 600, 600), node.children)
        with self.assertRaises(AmbiguousTarget):
            self.adapter.click_node(forged, expected_fingerprint=snapshot.fingerprint)
        self.device.jsonrpc.click.assert_not_called()

    def test_scoped_node_stale_even_if_same_button_still_exists(self):
        snapshot = self.adapter.hierarchy()
        node = snapshot.one(Selector(text='发送'))
        self.device.dump_hierarchy.return_value = xml_nodes('发送', 'new popup')
        with self.assertRaises(StaleSnapshot):
            self.adapter.click_node(node, expected_fingerprint=snapshot.fingerprint)
        self.device.jsonrpc.click.assert_not_called()

    def test_scoped_node_false_ack_is_unknown_once(self):
        snapshot = self.adapter.hierarchy()
        self.device.jsonrpc.click.return_value = False
        with self.assertRaises(OutcomeUnknown):
            self.adapter.click_node(snapshot.one(Selector(text='发送')),
                                    expected_fingerprint=snapshot.fingerprint)
        self.device.jsonrpc.click.assert_called_once()

    def test_double_click_package_drift_blocks_second_rpc(self):
        snapshot = self.adapter.hierarchy()
        def first_click(*_):
            self.device.app_current.return_value = {'package': 'other.app'}
            return True
        self.device.jsonrpc.click.side_effect = first_click
        with patch('wechatauto_android.adapter.time.sleep'):
            with self.assertRaises(OutcomeUnknown):
                self.adapter.double_click(Selector(text='发送'), expected_fingerprint=snapshot.fingerprint)
        self.device.jsonrpc.click.assert_called_once()

    def test_double_click_geometry_drift_blocks_second_rpc(self):
        snapshot = self.adapter.hierarchy()
        def first_click(*_):
            self.device.dump_hierarchy.return_value = xml_nodes('发送').replace('[100,40]', '[200,40]')
            return True
        self.device.jsonrpc.click.side_effect = first_click
        with patch('wechatauto_android.adapter.time.sleep'):
            with self.assertRaises(OutcomeUnknown):
                self.adapter.double_click(Selector(text='发送'), expected_fingerprint=snapshot.fingerprint)
        self.device.jsonrpc.click.assert_called_once()

    def test_double_click_nonaffirmative_first_rpc_is_never_repeated(self):
        self.device.jsonrpc.click.return_value = False
        with patch('wechatauto_android.adapter.time.sleep'):
            with self.assertRaises(OutcomeUnknown):
                self.adapter.double_click(Selector(text='发送'))
        self.device.jsonrpc.click.assert_called_once()


class InteractionScopeAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_source_chat_visible_drift_blocks_forward_before_long_press(self):
        from test_interactions import InteractionAdapter, SOURCE, client, forward
        adapter = InteractionAdapter(SOURCE)
        api = client(self.root, adapter)
        open_original = api._open_chat
        def change_chat_after_proof(target):
            open_original(target)
            adapter.chat_id = 'wrong-source'
            adapter.title = 'Other chat with identical message text'
        api._open_chat = change_chat_after_proof
        with self.assertRaises((SelectorNotFound, IdentityMismatch)):
            forward(api, operation_id='qa-forward-drift')
        self.assertFalse(any(call[0] == 'long_click' for call in adapter.calls))
        self.assertFalse(adapter.commits)

    def test_normalized_member_identifier_cannot_inherit_allowlist(self):
        from test_interactions import client, GROUP, MEMBER_NAME
        api = client(self.root, allowed={GROUP.chat_id, 'é'})
        with self.assertRaises(PermissionDenied):
            api.send_at(GROUP, 'body', member_id='e\u0301', member_name=MEMBER_NAME,
                        operation_id='qa-member-unicode')
        self.assertEqual(api.adapter.calls, [])

    def test_forward_target_title_never_inherits_id_permission(self):
        from test_interactions import InteractionAdapter, SOURCE, DESTINATION, client, forward
        adapter = InteractionAdapter(SOURCE)
        api = client(self.root, adapter, allowed={SOURCE.chat_id, DESTINATION.title})
        with self.assertRaises(PermissionDenied):
            forward(api, operation_id='qa-forward-name')
        self.assertEqual(adapter.calls, [])

    def test_native_mention_is_never_replaced_after_picker_selection(self):
        from test_interactions import client, mention, InteractionAdapter
        adapter = InteractionAdapter()
        api = client(self.root, adapter)
        original = adapter.set_text
        def reject_post_selection_text(selector, text):
            if adapter.native_chosen:
                raise AssertionError('native mention span would be lost by rewriting text')
            return original(selector, text)
        adapter.set_text = reject_post_selection_text
        self.assertEqual(mention(api, operation_id='qa-native-span').status, 'submitted')
        self.assertTrue(adapter.native_chosen)
        self.assertEqual(adapter.commits, ['chat.send'])


class MomentScopeAdversarialTests(unittest.TestCase):
    def setUp(self):
        from test_moments import MomentFake, MOMENT_CONTEXT_ROLES
        from fakes import fixture_profile, PACKAGE
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adapter = MomentFake()
        profile = fixture_profile()
        roles = dict(profile.roles)
        for role in MOMENT_CONTEXT_ROLES:
            roles[role] = {'resource_id': PACKAGE + ':id/synthetic_' + role}
        profile = replace(profile, roles=roles, collections={
            'moments_rows': {'resource_id': PACKAGE + ':id/synthetic_moments.rows'}})
        policy = SafetyPolicy('SERIAL', 'wxid_self', allowed_actions={
            'moment_like', 'moment_unlike', 'moment_comment'}, execute=True,
            assume_exclusive_device=True)
        self.client = WeChat(self.adapter, profile, policy, self.root / 'ops.sqlite',
                             transition_timeout=0.001)

    def test_uncalibrated_feed_observation_never_presses_back(self):
        self.adapter.state = 'feed'
        self.client.profile = replace(self.client.profile, calibrated=False)
        with self.assertRaises(PermissionDenied):
            self.client.Moment.observe()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.adapter.state, 'feed')

    def test_author_display_lookalike_cannot_reuse_post_permission(self):
        first, second = self.client.Moment.observe()
        self.client.policy = replace(self.client.policy, allowed_targets={first.id})
        with self.assertRaises(PermissionDenied):
            self.client.Moment.like(second, operation_id='qa-other-post')
        self.assertFalse(self.adapter.commits)

    def test_recomputed_reference_hash_does_not_reuse_original_authorization(self):
        first, second = self.client.Moment.observe()
        self.client.policy = replace(self.client.policy, allowed_targets={first.id})
        forged = replace(first, row_signature=second.row_signature,
                         author_display=second.author_display, content_visible=second.content_visible,
                         time_visible=second.time_visible, id='')
        forged = replace(forged, id=forged.expected_id())
        with self.assertRaises(PermissionDenied):
            self.client.Moment.like(forged, operation_id='qa-forged-post')
        self.assertFalse(self.adapter.commits)

    def test_duplicate_like_control_cannot_commit_to_another_menu(self):
        reference = self.client.Moment.observe()[0]
        self.client.policy = replace(self.client.policy, allowed_targets={reference.id})
        original_items = self.adapter._items
        def duplicate_items():
            rows = original_items()
            if self.adapter.state == 'moment_menu':
                rows.append(('moments.like', '赞'))
            return rows
        self.adapter._items = duplicate_items
        with self.assertRaises(AmbiguousTarget):
            self.client.Moment.like(reference, operation_id='qa-duplicate-like')
        self.assertFalse(self.adapter.commits)

    def test_dry_run_never_calls_context_navigation_or_commit(self):
        reference = self.client.Moment.observe()[0]
        self.client.policy = replace(self.client.policy, execute=False)
        before = list(self.adapter.calls)
        self.assertEqual(self.client.Moment.comment(reference, 'private content',
                         operation_id='qa-context-dryrun').status, 'dry_run')
        self.assertEqual(self.adapter.calls, before)
        self.assertFalse((self.root / 'ops.sqlite').exists())

    def test_moment_journal_contains_no_post_or_comment_body(self):
        reference = self.client.Moment.observe()[0]
        self.client.policy = replace(self.client.policy, allowed_targets={reference.id})
        text = 'PRIVATE_COMMENT_qa_secret'
        self.client.Moment.comment(reference, text, operation_id='qa-comment-private')
        data = (self.root / 'ops.sqlite').read_bytes()
        self.assertNotIn(text.encode(), data)
        self.assertNotIn(reference.content_visible.encode(), data)

class SpawnLifecycleAdversarialTests(unittest.TestCase):
    """No Frida runtime: injected SDK only, including every interruption below."""

    def setUp(self):
        import pytest
        import test_keycapture as fixtures
        self.fixtures = fixtures
        self.monkeypatch = pytest.MonkeyPatch()
        self.addCleanup(self.monkeypatch.undo)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_revoked_spawn_consent_rechecked_before_any_runtime_import(self):
        from wechatauto_android import keycapture
        for consent in ('consent_attach', 'allow_unverified_profile',
                        'consent_package_stop_relaunch', 'consent_system_instrumentation'):
            with self.subTest(consent=consent):
                config = self.fixtures.spawn_config()
                observer = FridaKeyCapture(config)
                object.__setattr__(config, consent, False)
                with patch.object(keycapture.importlib, 'import_module') as runtime:
                    with self.assertRaises(PermissionDenied):
                        observer.capture(None, self.fixtures.profile())
                    runtime.assert_not_called()

    def test_wrong_usb_endpoint_rejected_before_system_package_query(self):
        fake = self.fixtures.install_spawn_fake(self.monkeypatch, [])
        fake.type = 'remote'
        with self.assertRaises(IdentityMismatch):
            FridaKeyCapture(self.fixtures.spawn_config()).capture(
                self.fixtures.snapshot(self.root), self.fixtures.profile())
        self.assertEqual([call[0] for call in fake.calls], ['device'])

    def test_foreign_package_cannot_inherit_same_version_and_data_path(self):
        fake = self.fixtures.install_spawn_fake(self.monkeypatch, [])
        fake.apps[0].identifier = 'com.tencent.mm.clone'
        with self.assertRaises(IdentityMismatch):
            FridaKeyCapture(self.fixtures.spawn_config()).capture(
                self.fixtures.snapshot(self.root), self.fixtures.profile())
        self.assertEqual([call[0] for call in fake.calls], ['device', 'applications'])

    def test_bool_spawn_pid_is_not_valid_process_identity(self):
        fake = self.fixtures.install_spawn_fake(self.monkeypatch, [])
        spawn = Mock(return_value=True)
        fake.spawn = spawn
        with self.assertRaises(OutcomeUnknown):
            FridaKeyCapture(self.fixtures.spawn_config()).capture(
                self.fixtures.snapshot(self.root), self.fixtures.profile())
        spawn.assert_called_once_with('com.tencent.mm', uid=0)
        self.assertFalse(any(call[0] in ('attach', 'resume') for call in fake.calls))

    def test_interrupt_after_spawn_before_attach_result_resumes_exact_known_pid(self):
        fake = self.fixtures.install_spawn_fake(self.monkeypatch, [])
        fake.attach_error = KeyboardInterrupt()
        observer = FridaKeyCapture(self.fixtures.spawn_config())
        with self.assertRaises(KeyboardInterrupt):
            observer.capture(self.fixtures.snapshot(self.root), self.fixtures.profile())
        operations = [call[0] for call in fake.calls]
        self.assertEqual(operations.count('spawn'), 1)
        self.assertEqual(operations.count('attach'), 1)
        self.assertEqual(operations.count('resume'), 1)
        self.assertEqual(fake.calls[-1], ('resume', 4242))
        self.assertIsNone(observer.last_capture)

    def test_interrupted_initial_resume_gets_one_safety_resume_then_detach(self):
        fake = self.fixtures.install_spawn_fake(self.monkeypatch, [])
        normal_resume = fake.resume
        attempts = []
        def interrupt_once(pid):
            attempts.append(pid)
            if len(attempts) == 1:
                raise KeyboardInterrupt()
            return normal_resume(pid)
        fake.resume = interrupt_once
        observer = FridaKeyCapture(self.fixtures.spawn_config())
        with self.assertRaises(KeyboardInterrupt):
            observer.capture(self.fixtures.snapshot(self.root), self.fixtures.profile())
        self.assertEqual(attempts, [4242, 4242])
        self.assertTrue(fake.session.script.unloaded)
        self.assertTrue(fake.session.detached)
        self.assertIsNone(observer.last_capture)

    def test_verified_key_never_returns_when_unload_is_interrupted(self):
        f = self.fixtures
        fake = f.install_spawn_fake(self.monkeypatch,
            [(f.payload(), None), (f.payload(kind='candidate'), f.FAKE_KEY)])
        normal_create = fake.session.create_script
        def interrupt_unload(*args, **kwargs):
            script = normal_create(*args, **kwargs)
            script.unload = Mock(side_effect=KeyboardInterrupt())
            return script
        fake.session.create_script = interrupt_unload
        observer = FridaKeyCapture(f.spawn_config())
        with self.assertRaises(KeyboardInterrupt):
            observer.capture(f.encrypted_snapshot(self.root), f.profile())
        self.assertTrue(fake.session.detached)
        self.assertIsNone(observer.last_capture)
        self.assertEqual(fake.resume_attempts, 1)

    def test_snapshot_change_after_resume_cannot_produce_a_bound_key(self):
        f = self.fixtures
        snap = f.encrypted_snapshot(self.root)
        fake = f.install_spawn_fake(self.monkeypatch,
            [(f.payload(), None), (f.payload(kind='candidate'), f.FAKE_KEY)])
        normal_resume = fake.resume
        def mutate_snapshot(pid):
            result = normal_resume(pid)
            with snap.database_path.open('ab') as handle:
                handle.write(b'foreign snapshot bytes')
            return result
        fake.resume = mutate_snapshot
        observer = FridaKeyCapture(f.spawn_config())
        with self.assertRaises((IdentityMismatch, ValidationError)):
            observer.capture(snap, f.profile())
        self.assertTrue(fake.session.script.unloaded and fake.session.detached)
        self.assertIsNone(observer.last_capture)


if __name__ == '__main__':
    unittest.main()
