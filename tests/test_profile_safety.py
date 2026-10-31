"""Offline configuration validation and process/thread exclusivity."""
import json
import threading
from dataclasses import asdict, replace

import pytest

from wechatauto_android.errors import LockTimeout, ValidationError, PermissionDenied
from wechatauto_android.profile import Profile, Target
from wechatauto_android.safety import SafetyPolicy, OperationJournal, device_lock
from fakes import fixture_profile

@pytest.mark.parametrize("field,value", [
    ("name", ""), ("version", "*"), ("version", "7.0.0"), ("version", "8.0.x"),
    ("version", "8.0.0-beta"), ("package", "com.other"), ("calibrated", "true"),
    ("group_identity_supported", "false"), ("evidence", ""),
    ("android_sdks", (29,)), ("android_sdks", (True,)), ("android_sdks", ()),
])
def test_profile_invalid_fields(field, value):
    with pytest.raises(ValidationError):
        replace(fixture_profile(), **{field: value})

@pytest.mark.parametrize("value", ["{bad}", "{account_id.x}", "{account_id!r}", "{account_id:20}", "{"])
def test_profile_unknown_format_not_executed(value):
    with pytest.raises(ValidationError):
        replace(fixture_profile(), roles={"account.identity": {"text": value}})

@pytest.mark.parametrize("data", ["[]", "{}", '{"schema_version":true}', '{"schema_version":2}', '{"schema_version":1,"schema_version":1}', 'not json'])
def test_profile_file_validation(tmp_path, data):
    path = tmp_path / "profile.json"
    path.write_text(data)
    with pytest.raises(ValidationError):
        Profile.load(path)


def test_profile_roundtrip_and_symlink_rejection(tmp_path):
    path = tmp_path / "profile.json"
    value = {"schema_version": 1, **asdict(fixture_profile())}
    path.write_text(json.dumps(value))
    loaded = Profile.load(path)
    assert loaded == fixture_profile()
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ValidationError):
        Profile.load(link)

@pytest.mark.parametrize("args", [("", "title"), (None, "title"), ("id", ""), ("id", "title", "invalid"), ("id\x00", "title"), ("room@chatroom", "room"), ("contact", "room", "group")])
def test_invalid_target(args):
    with pytest.raises(ValidationError):
        Target(*args)

@pytest.mark.parametrize("kwargs", [
    {"serial": ""}, {"serial": None}, {"account_id": ""}, {"execute": "yes"},
    {"max_text_length": 0}, {"max_text_length": True}, {"lock_timeout": 0},
    {"lock_timeout": 301}, {"lock_timeout": float("nan")}, {"lock_timeout": True},
    {"allowed_targets": "id"}, {"allowed_actions": "send_text"}, {"allowed_targets": {None}},
])
def test_invalid_policy(kwargs):
    defaults = {"serial": "SERIAL", "account_id": "self"}
    defaults.update(kwargs)
    with pytest.raises(ValidationError):
        SafetyPolicy(**defaults)


def test_journal_invalid_and_private(tmp_path):
    journal = OperationJournal(tmp_path / "private" / "journal.sqlite")
    policy = SafetyPolicy("SERIAL", "account")
    assert journal.path.stat().st_mode & 0o777 == 0o600
    for op in ["short", "with spaces", "a" * 129]:
        with pytest.raises(ValidationError):
            journal.reserve(op, policy, "send_text", "chat", {})
    with pytest.raises(ValidationError):
        journal.mark("unknown", "verified")
    with pytest.raises(ValidationError):
        journal.mark("unknown", "fake")


def test_device_thread_lock_is_per_device():
    attempted = threading.Event()
    result = []
    def contender():
        attempted.set()
        try:
            with device_lock("same-serial", timeout=.02):
                result.append("unsafe")
        except LockTimeout:
            result.append("blocked")
    with device_lock("same-serial"):
        thread = threading.Thread(target=contender)
        thread.start()
        assert attempted.wait(1)
        thread.join(1)
        with device_lock("other-serial", timeout=.01):
            pass
    assert result == ["blocked"]
    with device_lock("same-serial", timeout=.01):
        pass


def test_default_policy_cannot_authorize():
    with pytest.raises(PermissionDenied):
        SafetyPolicy("SERIAL", "self").authorize("send_text", "id")


@pytest.mark.parametrize("which", ["role", "collection"])
def test_selector_profile_cannot_cross_package_boundaries(which):
    changes = ({"roles": {"home.marker": {"text": "secret", "package": "com.android.systemui"}}}
               if which == "role" else
               {"collections": {"messages": {"class_name": "android.widget.TextView", "package": "com.android.systemui"}}})
    with pytest.raises(ValidationError):
        replace(fixture_profile(), **changes)
