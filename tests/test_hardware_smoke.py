"""真机 smoke 入口的离线守卫测试；这些测试不连接任何 Android 设备。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from wechatauto_android.errors import DeviceUnavailable
from wechatauto_android.models import DeviceIdentity

_SPEC = importlib.util.spec_from_file_location(
    "hardware_smoke", Path(__file__).parents[1] / "scripts" / "hardware_smoke.py"
)
smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoke)


@pytest.fixture
def profile_path(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({
        "schema_version": 1, "name": "synthetic-smoke-only", "version": "8.0.0",
        "calibrated": True, "evidence": "synthetic fixture; NOT live validation",
        "android_sdks": [30],
        "roles": {role: {"text": role} for role in smoke.ACCOUNT_ROLES},
    }))
    return str(path)


def arguments(profile_path):
    return ["--serial", "synthetic-device", "--profile", profile_path,
            "--account", "synthetic-account"]


def test_missing_required_arguments_never_skip():
    with pytest.raises(SystemExit) as result:
        smoke.main([])
    assert result.value.code == 2


@pytest.mark.parametrize("flags", [[], ["--confirm-authorized-device"], ["--allow-device-setup"]])
def test_each_authorization_is_required_before_device_access(profile_path, monkeypatch, flags):
    def forbidden(*args, **kwargs):
        pytest.fail("must not access device before both permissions")
    monkeypatch.setattr(smoke, "Uiautomator2Adapter", forbidden)
    assert smoke.main(arguments(profile_path) + flags) == 2


def test_uncalibrated_profile_fails_before_device_access(profile_path, monkeypatch):
    data = json.loads(Path(profile_path).read_text())
    data["calibrated"] = False
    Path(profile_path).write_text(json.dumps(data))
    def forbidden(*args, **kwargs):
        pytest.fail("draft profile must not navigate a device")
    monkeypatch.setattr(smoke, "Uiautomator2Adapter", forbidden)
    assert smoke.main(arguments(profile_path) +
                      ["--confirm-authorized-device", "--allow-device-setup"]) == 2


def test_missing_device_is_a_failure_not_a_skip(profile_path, monkeypatch, capsys):
    def missing(*args, **kwargs):
        raise DeviceUnavailable("synthetic missing device")
    monkeypatch.setattr(smoke, "Uiautomator2Adapter", missing)
    assert smoke.main(arguments(profile_path) +
                      ["--confirm-authorized-device", "--allow-device-setup"]) == 2
    assert json.loads(capsys.readouterr().err)["result"] == "failed_read_only_smoke"


def test_success_is_only_read_only_and_redacted(profile_path, monkeypatch, capsys):
    identity = DeviceIdentity("synthetic-device", "com.tencent.mm", "8.0.0", 30)
    class FakeAdapter:
        def __init__(self, serial, *, allow_device_setup):
            assert serial == identity.serial and allow_device_setup is True
        def identity(self):
            return identity
    class FakeClient:
        def __init__(self, adapter, profile, policy):
            assert policy.execute is False
            assert not policy.allowed_actions and not policy.allowed_targets
        def verify_account(self):
            return {"account_id": "synthetic-account"}
    monkeypatch.setattr(smoke, "Uiautomator2Adapter", FakeAdapter)
    monkeypatch.setattr(smoke, "WeChat", FakeClient)
    assert smoke.main(arguments(profile_path) +
                      ["--confirm-authorized-device", "--allow-device-setup"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["result"] == "passed_read_only_smoke"
    assert report["live_business_tests_run"] is False
    assert report["root_snapshot_tested"] is False
    assert "synthetic-device" not in output and "synthetic-account" not in output
