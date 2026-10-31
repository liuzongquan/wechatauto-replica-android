"""Observed-only recall tests using synthetic records; no device or external messages."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
import sqlite3
from types import SimpleNamespace

import pytest

from wechatauto_android.errors import IdentityMismatch, ValidationError
from wechatauto_android.models import Message
from wechatauto_android.recall import RecallGuard, RecallStoreError


def original(server="123", chat="group@chatroom", account="account-a", **extra):
    return Message("local-1", account, chat, 1, "native-sender", "previously seen 私密🙂",
                   metadata={"native_msg_svr_id": server, "native_type": 1,
                             "source_id": "synthetic", "media_path": "../../not-opened"}, **extra)


def recall(server="123", chat="group@chatroom", account="account-a", session=None, **extra):
    xml = '<sysmsg type="revokemsg"><revokemsg>'
    xml += f'<session>{chat if session is None else session}</session>'
    xml += f'<msgid>1</msgid><newmsgid>{server}</newmsgid>'
    xml += '<replacemsg>A recalled a message</replacemsg></revokemsg></sysmsg>'
    return Message("local-1", account, chat, 1, "", xml, "recall",
                   metadata={"native_msg_svr_id": "999", "native_type": 10002,
                             "source_id": "synthetic"}, **extra)


def test_exact_observed_original_recovered_without_any_external_access():
    item = original()
    with RecallGuard("account-a") as guard:
        assert guard.observe(item).status == "observed"
        result = guard.observe(recall())
        assert result.status == "recovered" and result.original == item
        assert result.original.origin == "user_export"
        assert result.original.metadata["media_path"] == "../../not-opened"
        assert result.source == recall() and not result.duplicate
        assert guard.events() == [result]
        assert item.content not in repr(result)
        assert "../../not-opened" not in repr(result)


def test_account_chat_and_native_id_are_all_required():
    with RecallGuard("account-a") as guard:
        guard.observe(original(chat="chat-one"))
        assert guard.observe(recall(chat="chat-two")).status == "unknown"
        assert guard.observe(recall(server="124", chat="chat-one")).status == "unknown"
        with pytest.raises(IdentityMismatch):
            guard.observe(original(account="account-b"))
        with pytest.raises(IdentityMismatch):
            guard.observe(recall(account="account-b"))
        assert guard.observe(recall(chat="chat-one")).status == "recovered"
    with RecallGuard("account-b") as second:
        assert second.observe(recall(account="account-b", chat="chat-one")).status == "unknown"


def test_xml_session_must_match_and_mismatch_does_not_poison_real_event():
    with RecallGuard("account-a") as guard:
        guard.observe(original())
        result = guard.observe(recall(session="another-chat"))
        assert result.status == "unknown" and result.reason == "recall_chat_mismatch"
        assert guard.observe(recall()).status == "recovered"
        assert len(guard.events()) == 2


def test_local_ids_display_names_timestamps_not_used_as_fallback():
    with RecallGuard("account-a") as guard:
        item = replace(original(), metadata={"native_type": 1, "sender_name": "Alice"})
        assert guard.observe(item).status == "unmatchable"
        notice = replace(recall(), content=recall().content.replace('<newmsgid>123</newmsgid>', ''))
        result = guard.observe(notice)
        assert result.status == "unknown" and result.original is None
        assert guard.observe(recall(server="1")).status == "unknown"


def test_duplicates_preserve_first_original_and_first_recall_result():
    with RecallGuard("account-a") as guard:
        item = original()
        assert not guard.observe(item).duplicate
        assert guard.observe(item).duplicate
        event = recall()
        first = guard.observe(event)
        second = guard.observe(event)
        third = guard.observe(replace(event, id="new-local-observation", sequence=900))
        assert first.status == second.status == third.status == "recovered"
        assert second.duplicate and third.duplicate
        assert second.original == item and third.source == event
        assert len(guard.events()) == 1
        assert guard.observe(item).duplicate


def test_unknown_outcome_stays_unknown_after_late_original():
    with RecallGuard("account-a") as guard:
        first = guard.observe(recall())
        assert first.status == "unknown" and first.reason == "not_previously_observed"
        late = guard.observe(original())
        assert late.status == "unmatchable" and late.reason == "observed_after_recall"
        again = guard.observe(recall())
        assert again.status == "unknown" and again.duplicate and again.original is None


def test_out_of_sequence_messages_use_observation_order_not_clock_guessing():
    with RecallGuard("account-a") as guard:
        seen = replace(original(), sequence=9000, timestamp="2099-01-01T00:00:00Z")
        guard.observe(seen)
        assert guard.observe(recall()).original == seen


def test_conflicting_native_identity_fails_closed_even_after_restart(tmp_path):
    path = tmp_path / "recall.sqlite"
    with RecallGuard("account-a", path) as guard:
        guard.observe(original())
        result = guard.observe(replace(original(), content="different same native id"))
        assert result.status == "conflict"
    with RecallGuard("account-a", path) as guard:
        result = guard.observe(recall())
        assert result.status == "unknown" and result.reason == "conflicting_observed_identity"
        assert result.original is None


def test_restart_scope_permissions_and_no_auto_backfill(tmp_path):
    path = tmp_path / "recall.sqlite"
    with RecallGuard("account-a", path) as guard:
        guard.observe(original())
    assert os.stat(path).st_mode & 0o777 == 0o600
    with RecallGuard("account-a", path) as guard:
        result = guard.observe(recall())
        assert result.status == "recovered"
    with RecallGuard("account-a", path) as guard:
        assert guard.observe(recall()).duplicate
        assert guard.events()[0].original == original()
        assert guard.observe(recall(server="9999")).status == "unknown"
    with pytest.raises(IdentityMismatch):
        RecallGuard("account-b", path)
    with RecallGuard("account-a", path) as guard:
        assert len(guard.events()) == 2
    assert not list(tmp_path.glob("*-wal"))


def test_mutable_metadata_cannot_change_stored_original_or_returned_outcomes():
    item = original()
    with RecallGuard("account-a") as guard:
        guard.observe(item)
        item.metadata["source_id"] = "changed"
        result = guard.observe(recall())
        assert result.original.metadata["source_id"] == "synthetic"
        result.original.metadata["source_id"] = "changed-again"
        assert guard.events()[0].original.metadata["source_id"] == "synthetic"


@pytest.mark.parametrize("server", [None, "", "0", "-1", "../123", True, 1.2])
def test_invalid_native_id_cannot_create_mirror(server):
    with RecallGuard("account-a") as guard:
        assert guard.observe(original(server=server)).status == "unmatchable"
        assert guard.observe(recall()).status == "unknown"


def test_canonical_server_id_and_no_nickname_lookup():
    with RecallGuard("account-a") as guard:
        guard.observe(original(server=123))
        assert guard.observe(recall(server="00123")).status == "recovered"


def test_normal_text_containing_revoke_xml_is_not_an_event():
    with RecallGuard("account-a") as guard:
        item = replace(original(), content=recall().content)
        assert guard.observe(item).status == "observed"
        assert not guard.events()


@pytest.mark.parametrize("content", ["<sysmsg>", "<!DOCTYPE x [<!ENTITY a 'sensitive'>]><x>&a;</x>",
                                     "not XML", '<sysmsg type="revokemsg"/>'])
def test_invalid_recall_never_overwrites_original(content):
    with RecallGuard("account-a") as guard:
        guard.observe(original())
        notice = replace(recall(), content=content)
        assert guard.observe(notice).status == "unknown"
        assert guard.observe(recall()).original == original()


def test_system_notices_and_unsafe_originals_do_not_enter_mirror():
    with RecallGuard("account-a") as guard:
        system = replace(original(), type="system", metadata={"native_type": 10000,
                                                              "native_msg_svr_id": "123"})
        assert guard.observe(system).status == "unmatchable"
        unsafe = replace(original(), content='<msg>', type="app",
                         metadata={"native_type": 49, "native_msg_svr_id": "123"})
        assert guard.observe(unsafe).status == "unmatchable"
        assert guard.observe(recall()).status == "unknown"


def test_listener_callback_requires_bound_account_and_concurrent_duplicates():
    with RecallGuard("account-a") as guard:
        with pytest.raises(IdentityMismatch):
            guard.on_msg(original(), SimpleNamespace(account_id="other"))
        with pytest.raises(IdentityMismatch):
            guard.on_msg(original(), object())
        assert guard.on_msg(original(), SimpleNamespace(account_id="account-a")).status == "observed"
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(guard.observe, [recall()] * 10))
        assert sum(not result.duplicate for result in results) == 1
        assert len(guard.events()) == 1


def test_private_path_guards_and_foreign_database_rejected(tmp_path):
    for value in (":memory:", "file:/tmp/not-allowed", "https://invalid.test/db", "", 123):
        with pytest.raises(ValidationError):
            RecallGuard("account-a", value)
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    with pytest.raises(ValidationError):
        RecallGuard("account-a", shared / "recall.sqlite")
    existing = tmp_path / "public.sqlite"
    existing.touch(mode=0o644)
    with pytest.raises(ValidationError):
        RecallGuard("account-a", existing)
    link = tmp_path / "link.sqlite"
    link.symlink_to(existing)
    with pytest.raises(ValidationError):
        RecallGuard("account-a", link)
    parent_link = tmp_path / "link-dir"
    parent_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValidationError):
        RecallGuard("account-a", parent_link / "new.sqlite")
    with pytest.raises(ValidationError):
        RecallGuard("account-a", tmp_path / ".." / tmp_path.name / "new.sqlite")
    hard = tmp_path / "hard.sqlite"
    hard.touch(mode=0o600)
    os.link(hard, tmp_path / "second-link")
    with pytest.raises(ValidationError):
        RecallGuard("account-a", hard)
    foreign = tmp_path / "foreign.sqlite"
    foreign.touch(mode=0o600)
    with sqlite3.connect(foreign) as connection:
        connection.execute("CREATE TABLE unrelated (secret TEXT)")
    with pytest.raises(RecallStoreError):
        RecallGuard("account-a", foreign)


def test_sidecar_and_storage_errors_do_not_expose_paths_or_contents(tmp_path, capsys):
    path = tmp_path / "private-secret-path.sqlite"
    Path = type(path)
    Path(str(path) + "-journal").symlink_to(tmp_path / "untrusted-target")
    with pytest.raises(ValidationError) as caught:
        RecallGuard("account-a", path)
    assert str(path) not in str(caught.value)
    assert not path.exists()
    corrupt = tmp_path / "corrupt-private.sqlite"
    corrupt.write_bytes(b"not sqlite private secret")
    corrupt.chmod(0o600)
    with pytest.raises(RecallStoreError) as caught:
        RecallGuard("account-a", corrupt)
    assert "private secret" not in str(caught.value)
    assert str(corrupt) not in str(caught.value)
    assert capsys.readouterr().out == capsys.readouterr().err == ""


def test_closed_store_limits_and_invalid_messages():
    guard = RecallGuard("account-a")
    assert guard.events(0) == []
    for value in (-1, True, 10001, "1"):
        with pytest.raises(ValidationError):
            guard.events(value)
    with pytest.raises(ValidationError):
        guard.observe({})
    guard.close()
    guard.close()
    with pytest.raises(RecallStoreError):
        guard.observe(original())
    with pytest.raises(RecallStoreError):
        guard.events()
    with pytest.raises(RecallStoreError):
        guard.__enter__()


def test_tampered_stored_original_cannot_cross_identity_boundary(tmp_path):
    path = tmp_path / "recall.sqlite"
    with RecallGuard("account-a", path) as guard:
        guard.observe(original())
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE messages SET message_json=replace(message_json,?,?)",
                           ('"account-a"', '"account-b"'))
    with RecallGuard("account-a", path) as guard:
        with pytest.raises(RecallStoreError, match="identity"):
            guard.observe(recall())
        assert guard.events() == []


@pytest.mark.parametrize("account", ["", "a\x00b", "\ud800", 123])
def test_invalid_account_binding(account):
    with pytest.raises(ValidationError):
        RecallGuard(account)


def test_account_binding_is_not_switchable():
    with RecallGuard("account-a") as guard:
        with pytest.raises(AttributeError):
            guard.account_id = "account-b"


def test_nonexistent_parent_is_sanitized(tmp_path):
    path = tmp_path / "missing-private-parent" / "db.sqlite"
    with pytest.raises(RecallStoreError) as caught:
        RecallGuard("account-a", path)
    assert str(path) not in str(caught.value)


@pytest.mark.parametrize("sql", ["DELETE FROM metadata", "UPDATE metadata SET value='9' WHERE key='schema_version'",
    "ALTER TABLE messages ADD COLUMN unexpected TEXT", "PRAGMA journal_mode=WAL"])
def test_incompatible_or_corrupted_schema_rejected(tmp_path, sql):
    path = tmp_path / "db.sqlite"
    with RecallGuard("account-a", path):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute(sql)
    connection.close()
    with pytest.raises(RecallStoreError):
        RecallGuard("account-a", path)


@pytest.mark.parametrize("target,sql", [
    ("events", "UPDATE events SET status='fictional'"),
    ("events", "UPDATE events SET source_json=replace(source_json, 'account-a', 'account-b')"),
    ("events", "UPDATE events SET original_json='not JSON'"),
    ("events", "UPDATE events SET source_json='{}'"),
])
def test_corrupted_events_never_return_false_recovery(tmp_path, target, sql):
    path = tmp_path / "db.sqlite"
    with RecallGuard("account-a", path) as guard:
        guard.observe(original())
        guard.observe(recall())
    with sqlite3.connect(path) as connection:
        connection.execute(sql)
    with RecallGuard("account-a", path) as guard:
        with pytest.raises(RecallStoreError):
            guard.events()


def test_transaction_failure_is_sanitized_and_rolled_back(capsys):
    with RecallGuard("account-a") as guard:
        guard.observe(original())
        def deny_insert(action, *args):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT else sqlite3.SQLITE_OK
        guard._connection.set_authorizer(deny_insert)
        with pytest.raises(RecallStoreError):
            guard.observe(recall())
        guard._connection.set_authorizer(None)
        assert guard.events() == []
        assert guard.observe(recall()).status == "recovered"
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_read_failure_is_sanitized():
    guard = RecallGuard("account-a")
    guard._connection.close()
    with pytest.raises(RecallStoreError):
        guard.events()
    guard.close()


def test_oversized_stored_records_and_event_batches_rejected(monkeypatch):
    import wechatauto_android.recall as module
    with RecallGuard("account-a") as guard:
        guard.observe(original())
        guard.observe(recall())
        monkeypatch.setattr(module, "MAX_RECORD_BYTES", 10)
        with pytest.raises(RecallStoreError, match="batch"):
            guard.events()
        with pytest.raises(RecallStoreError, match="size"):
            guard.observe(recall())


def test_duplicate_cannot_read_event_in_wrong_chat(tmp_path):
    path = tmp_path / "db.sqlite"
    with RecallGuard("account-a", path) as guard:
        guard.observe(recall())
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE events SET source_json=replace(source_json,?,?)",
                           ('group@chatroom', 'wrong-chat'))
    with RecallGuard("account-a", path) as guard:
        with pytest.raises(RecallStoreError, match="target"):
            guard.observe(recall())


def test_persisted_recovery_requires_event_original_and_target_server_ids_equal(tmp_path):
    path = tmp_path / "db.sqlite"
    with RecallGuard("account-a", path) as guard:
        guard.observe(original())
        guard.observe(recall())
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE events SET source_json=replace(source_json,?,?)",
                           ('<newmsgid>123</newmsgid>', '<newmsgid>456</newmsgid>'))
    with RecallGuard("account-a", path) as guard:
        with pytest.raises(RecallStoreError, match="target"):
            guard.events()
        with pytest.raises(RecallStoreError, match="target"):
            guard.observe(recall())
