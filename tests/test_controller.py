"""Offline synthetic workflows; these are not live WeChat acceptance tests."""
from dataclasses import replace
import hashlib

import pytest

from wechatauto_android import ProfileRegistry, SafetyPolicy, Target, WeChat
from wechatauto_android.errors import (AmbiguousTarget, DuplicateOperation, IdentityMismatch,
    OutcomeUnknown, PermissionDenied, SelectorNotFound, UnsupportedFeature, ValidationError)
from wechatauto_android.safety import OperationJournal
from fakes import FakeAdapter, fixture_profile

@pytest.fixture
def setup_client(tmp_path):
    adapter = FakeAdapter()
    profile = fixture_profile()
    actions = frozenset({"send_text", "reply", "recall", "voice_call", "video_call", "poke", "send_file", "send_image", "rename_remark"})
    policy = SafetyPolicy("SERIAL", "wxid_self", frozenset({"wxid_target"}), actions, True, assume_exclusive_device=True)
    client = WeChat(adapter, profile, policy, tmp_path / "journal.sqlite", transition_timeout=.001)
    return adapter, client, Target("wxid_target", "Exact Friend")

@pytest.mark.parametrize("text", ["hello", "中文😀\n第二行\t制表符", "café", "e\u0301", "a'\"<>&"])
def test_complete_text_state_machine(setup_client, text):
    adapter, client, target = setup_client
    result = client.send_text(target, text, operation_id="operation-text")
    assert result.status == "submitted" and not result.success
    assert adapter.draft == text and adapter.commits == ["chat.send"]
    assert adapter.calls.count(("click", "chat.info")) == 2
    assert OperationJournal(client.journal_path).status(result.operation_id) == "submitted"

@pytest.mark.parametrize("action", ["reply", "recall", "voice_call", "video_call", "poke"])
def test_complete_nontext_workflows(setup_client, action):
    adapter, client, target = setup_client
    if action == "reply":
        result = client.reply(target, "Original message", "answer", operation_id="operation-action")
        assert adapter.quote == "Original message"
    elif action == "recall":
        result = client.recall(target, "My message", operation_id="operation-action")
    elif action in {"voice_call", "video_call"}:
        result = client.voice_call(target, operation_id="operation-action", video=action == "video_call")
    else:
        result = client.poke(target, operation_id="operation-action")
    assert result.status == "submitted" and len(adapter.commits) == 1

@pytest.mark.parametrize("image", [False, True])
def test_immutable_attachment_preparation(setup_client, tmp_path, image):
    adapter, client, target = setup_client
    path = tmp_path / ("source.png" if image else "source.txt")
    path.write_bytes(b"approved bytes")
    adapter.on_account = lambda: path.write_bytes(b"changed after approval")
    result = client.send_file(target, path, operation_id="operation-attachment", image=image)
    assert result.status == "submitted"
    assert adapter.uploads[0][0] == b"approved bytes"
    assert hashlib.sha256(b"approved bytes").hexdigest()[:16] in adapter.uploads[0][1]

@pytest.mark.parametrize("reason", ["account", "target", "title", "duplicate", "sdk", "version"])
def test_binding_mismatches_fail_without_commit(setup_client, reason):
    adapter, client, target = setup_client
    if reason == "account":
        adapter.account = "another-account"
    elif reason == "target":
        adapter.chat_id = "another-target"
    elif reason == "title":
        adapter.title = "Impostor"
    elif reason == "duplicate":
        adapter.duplicate_result = True
    else:
        original = adapter.identity()
        adapter.identity = lambda: replace(original, **({"sdk": 33} if reason == "sdk" else {"version": "8.0.1"}))
    with pytest.raises((SelectorNotFound, IdentityMismatch, AmbiguousTarget)):
        client.send_text(target, "hello", operation_id="operation-badbinding")
    assert not adapter.commits

@pytest.mark.parametrize("mode", ["false_commit", "error_commit"])
def test_unknown_result_durable_no_retry(setup_client, mode):
    adapter, client, target = setup_client
    setattr(adapter, mode, True)
    with pytest.raises(OutcomeUnknown):
        client.send_text(target, "hello", operation_id="operation-uncertain")
    with pytest.raises(DuplicateOperation):
        client.send_text(target, "hello", operation_id="operation-uncertain")
    assert len(adapter.commits) == 1
    assert OperationJournal(client.journal_path).status("operation-uncertain") == "unknown"

@pytest.mark.parametrize("text", ["", "  ", "x\x00y", "x\x1by", "x" * 5001])
def test_text_validation_prevents_navigation(setup_client, text):
    adapter, client, target = setup_client
    with pytest.raises(ValidationError):
        client.send_text(target, text, operation_id="operation-badtext")
    assert not adapter.calls

@pytest.mark.parametrize("kind", ["dry", "uncalibrated", "no-action", "no-target"])
def test_execution_gates(setup_client, kind):
    adapter, client, target = setup_client
    if kind == "dry":
        client.policy = replace(client.policy, execute=False)
        result = client.send_text(target, "hello", operation_id="operation-dryrun")
        assert result.status == "dry_run" and not client.journal_path.exists()
    else:
        if kind == "uncalibrated":
            client.profile = replace(client.profile, calibrated=False)
        elif kind == "no-action":
            client.policy = replace(client.policy, allowed_actions=frozenset())
        else:
            client.policy = replace(client.policy, allowed_targets=frozenset())
        with pytest.raises(PermissionDenied):
            client.send_text(target, "hello", operation_id="operation-denied")
    assert not adapter.calls


def test_ui_observations_are_snapshot_scoped(setup_client):
    adapter, client, target = setup_client
    observations = client.read_visible(target)
    assert len(observations) == 1
    assert observations[0].origin == "android_visible_ui"
    assert observations[0].account_id == "wxid_self"
    assert observations[0].chat_id == "wxid_target"
    assert observations[0].snapshot and not hasattr(observations[0], "sequence")


def test_profile_registry_exact_version_sdk_only():
    profile = fixture_profile()
    registry = ProfileRegistry([profile])
    assert registry.resolve("8.0.0", 34) is profile
    for version, sdk in [("8.0.1", 34), ("8.0.0", 35), ("8.1.0", 34), ("9.0.0", 34)]:
        with pytest.raises(UnsupportedFeature):
            registry.resolve(version, sdk)
    with pytest.raises(ValidationError):
        ProfileRegistry([profile, profile])


def test_profile_incomplete_not_successful(setup_client):
    _, client, target = setup_client
    roles = dict(client.profile.roles)
    del roles["chat.identity"]
    client.profile = replace(client.profile, roles=roles)
    with pytest.raises(UnsupportedFeature):
        client.send_text(target, "hello", operation_id="operation-unavailable")


def test_strict_mode_refuses_missing_fresh_account_proof(setup_client):
    adapter, client, target = setup_client
    client.policy = replace(client.policy, assume_exclusive_device=False)
    with pytest.raises(PermissionDenied, match="fresh commit account proof"):
        client.send_text(target, "hello", operation_id="strict-missing-proof")
    assert not adapter.calls


def test_independent_account_attestation_detects_switch(setup_client):
    adapter, client, target = setup_client
    client.policy = replace(client.policy, assume_exclusive_device=False)
    client.account_verifier = lambda: adapter.account
    original = adapter.click
    def switch_account(selector, **kwargs):
        result = original(selector, **kwargs)
        if "home.search" in selector.resource_id:
            adapter.account = "switched_account"
        return result
    adapter.click = switch_account
    with pytest.raises(IdentityMismatch, match="precommit account proof changed"):
        client.send_text(target, "hello", operation_id="strict-switched-account")
    assert not adapter.commits


def test_fresh_ui_account_attestation_detects_switch(setup_client):
    adapter, client, target = setup_client
    client.policy = replace(client.policy, assume_exclusive_device=False)
    roles = dict(client.profile.roles)
    roles["commit.account_identity"] = {"resource_id": "com.tencent.mm:id/synthetic_commit.account_identity", "text": "{account_id}"}
    client.profile = replace(client.profile, roles=roles)
    old_items = adapter._items
    adapter._items = lambda: old_items() + [("commit.account_identity", adapter.account)]
    original = adapter.click
    def switch_account(selector, **kwargs):
        result = original(selector, **kwargs)
        if "home.search" in selector.resource_id:
            adapter.account = "switched_account"
        return result
    adapter.click = switch_account
    with pytest.raises(SelectorNotFound):
        client.send_text(target, "hello", operation_id="strict-ui-switched-account")
    assert not adapter.commits


def test_bounded_contact_remark_workflow(setup_client):
    adapter, client, target = setup_client
    result = client.rename_remark(target, "精确备注 😀", operation_id="remark-operation")
    assert result.status == "submitted"
    assert adapter.remark == "精确备注 😀"
    assert adapter.commits == ["remark.save"]


def test_contact_identity_requires_nested_information_profile_navigation(setup_client):
    adapter, client, target = setup_client
    client.profile = replace(client.profile, identity_route="contact_profile")
    result = client.send_text(target, "nested proof", operation_id="nested-contact-proof")
    assert result.status == "submitted"
    assert adapter.calls.count(("click", "contact.entry")) == 2
    assert adapter.calls.count(("back", "contact_profile")) == 2
    assert adapter.commits == ["chat.send"]


def test_uncalibrated_read_navigation_is_blocked(setup_client):
    adapter, client, target = setup_client
    client.profile = replace(client.profile, calibrated=False)
    with pytest.raises(PermissionDenied):
        client.verify_account()
    with pytest.raises(PermissionDenied):
        client.read_visible(target)
    assert not adapter.calls
