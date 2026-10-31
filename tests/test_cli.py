"""CLI 离线集成；所有设备接入都被替换或明确阻断，不是微信真机测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import pytest

from fakes import FakeAdapter, fixture_profile
from wechatauto_android import __version__
from wechatauto_android.cli import build_parser, main
from wechatauto_android.errors import DeviceUnavailable, PermissionDenied
from wechatauto_android.models import DeviceIdentity


@pytest.fixture
def profile_path(tmp_path):
    value = asdict(fixture_profile())
    value["schema_version"] = 1
    path = tmp_path / "synthetic-profile.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def archive_path(tmp_path):
    data = {
        "schema_version": 1,
        "account_id": "account-fixture",
        "export_id": "synthetic-cli-only",
        "messages": [
            {
                "id": "a",
                "chat_id": "chat-a",
                "sequence": 1,
                "sender_id": "sender",
                "content": "中文 👩\u200d💻",
            },
            {
                "id": "b",
                "chat_id": "chat-a",
                "sequence": 2,
                "sender_id": "sender",
                "content": "latest",
            },
            {
                "id": "a",
                "chat_id": "chat-b",
                "sequence": 3,
                "sender_id": "other",
                "content": "other chat",
            },
        ],
    }
    path = tmp_path / "archive.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def send_args(profile_path, tmp_path):
    return [
        "send",
        "--serial",
        "SERIAL",
        "--profile",
        str(profile_path),
        "--account",
        "wxid_self",
        "--chat-id",
        "wxid_target",
        "--title",
        "Exact Friend",
        "--text",
        "精确 Unicode e\u0301 👩\u200d💻",
        "--journal",
        str(tmp_path / "private" / "ops.sqlite"),
    ]


def replace_arg(args, name, value):
    result = list(args)
    result[result.index(name) + 1] = value
    return result


def prohibit_connection(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("nonconnecting CLI path unexpectedly initialized the device adapter")

    monkeypatch.setattr("wechatauto_android.adapter.Uiautomator2Adapter", forbidden)


def run_cli(tmp_path, *args):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    return subprocess.run(
        [sys.executable, "-m", "wechatauto_android", *map(str, args)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_help_and_version_subprocess(tmp_path):
    result = run_cli(tmp_path, "--help")
    assert result.returncode == 0
    assert "profile-check" in result.stdout and "snapshot" in result.stdout
    result = run_cli(tmp_path, "--version")
    assert result.returncode == 0 and result.stdout.strip() == __version__
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("command", ["doctor", "snapshot", "send"])
def test_device_subcommand_help_documents_consent(command, capsys):
    with pytest.raises(SystemExit) as outcome:
        build_parser().parse_args([command, "--help"])
    assert outcome.value.code == 0
    assert "--allow-device-setup" in capsys.readouterr().out


def test_missing_subcommand_is_parser_error(capsys):
    with pytest.raises(SystemExit) as outcome:
        main([])
    assert outcome.value.code == 2
    assert "required" in capsys.readouterr().err


def test_capabilities_subprocess_has_no_live_claim(tmp_path):
    result = run_cli(tmp_path, "capabilities")
    assert result.returncode == 0 and result.stderr == ""
    report = json.loads(result.stdout)
    assert report["verified_versions"] == []
    assert report["profile"] is None
    assert all(not action["live_tested"] for action in report["actions"].values())
    assert all(not action["available_for_live_execution"] for action in report["actions"].values())
    assert "not Android database extraction" in report["exports"]


def test_profile_check_subprocess_is_explicitly_not_live(profile_path, tmp_path):
    result = run_cli(tmp_path, "profile-check", profile_path)
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["name"] == "SYNTHETIC-NOT-LIVE"
    assert report["android_sdks"] == [34]
    assert report["live_tested_by_this_project"] is False


def test_capabilities_with_profile_does_not_connect(profile_path, monkeypatch, capsys):
    prohibit_connection(monkeypatch)
    assert main(["capabilities", "--profile", str(profile_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["profile"] == "SYNTHETIC-NOT-LIVE"
    assert report["verified_versions"] == []
    assert report["profile_calibration_is_operator_assertion"] is True


@pytest.mark.parametrize("contents", ["not json", "{}", '{"schema_version":99}', "[]"])
def test_invalid_profile_is_json_error_without_traceback(tmp_path, contents, capsys):
    path = tmp_path / "bad.json"
    path.write_text(contents)
    assert main(["profile-check", str(path)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["error"] == "ValidationError"
    assert "Traceback" not in output.err


def test_missing_profile_is_json_domain_error(tmp_path, capsys):
    assert main(["profile-check", str(tmp_path / "missing.json")]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "ValidationError"


def test_archive_messages_subprocess_exact_scope(archive_path, tmp_path):
    result = run_cli(
        tmp_path,
        "archive",
        "--file",
        archive_path,
        "--account",
        "account-fixture",
        "messages",
        "chat-a",
        "--limit",
        "1",
    )
    assert result.returncode == 0
    rows = json.loads(result.stdout)
    assert len(rows) == 1 and rows[0]["id"] == "b"
    assert rows[0]["chat_id"] == "chat-a"
    assert rows[0]["account_id"] == "account-fixture"
    assert rows[0]["origin"] == "user_export"


def test_archive_sessions_cli_contract(archive_path, capsys):
    assert (
        main(
            [
                "archive",
                "--file",
                str(archive_path),
                "--account",
                "account-fixture",
                "sessions",
                "--limit",
                "1",
            ]
        )
        == 0
    )
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]["chat_id"] == "chat-b"
    assert rows[0]["origin"] == "user_export"


def test_archive_account_mismatch_and_invalid_limit(archive_path, capsys):
    prefix = ["archive", "--file", str(archive_path), "--account", "other"]
    assert main(prefix + ["sessions"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "IdentityMismatch"
    prefix[-1] = "account-fixture"
    assert main(prefix + ["messages", "chat-a", "--limit", "-1"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "ValidationError"


def test_dry_run_does_not_connect_or_create_journal(profile_path, tmp_path, monkeypatch, capsys):
    prohibit_connection(monkeypatch)
    assert main(send_args(profile_path, tmp_path)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "dry_run"
    assert report["target"] == "wxid_target"
    assert not (tmp_path / "private").exists()
    assert "精确 Unicode" not in json.dumps(report, ensure_ascii=False)


def test_dry_run_subprocess_is_nonconnecting(profile_path, tmp_path):
    result = run_cli(tmp_path, *send_args(profile_path, tmp_path))
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout)["status"] == "dry_run"
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize(
    "extra",
    [
        ["--execute", "--allow-target", "wxid_target"],
        ["--execute", "--operation-id", "cli-test-001"],
        ["--execute", "--operation-id", "cli-test-001", "--allow-target", "other"],
    ],
)
def test_execute_missing_operation_or_allowlist_never_connects(
    profile_path, tmp_path, monkeypatch, capsys, extra
):
    prohibit_connection(monkeypatch)
    assert main(send_args(profile_path, tmp_path) + extra) == 2
    assert json.loads(capsys.readouterr().err)["error"] in {"ValidationError", "PermissionDenied"}
    assert not (tmp_path / "private").exists()


def test_execute_uncalibrated_never_connects(profile_path, tmp_path, monkeypatch, capsys):
    data = json.loads(profile_path.read_text())
    data["calibrated"] = False
    profile_path.write_text(json.dumps(data))
    prohibit_connection(monkeypatch)
    assert (
        main(
            send_args(profile_path, tmp_path)
            + ["--execute", "--operation-id", "cli-test-001", "--allow-target", "wxid_target"]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"] == "ValidationError"
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize(
    "name,value",
    [
        ("--text", ""),
        ("--title", ""),
        ("--chat-id", ""),
        ("--operation-id", ""),
        ("--operation-id", "short"),
        ("--operation-id", "contains space"),
    ],
)
def test_invalid_execute_input_never_initializes_device(
    profile_path, tmp_path, monkeypatch, capsys, name, value
):
    args = send_args(profile_path, tmp_path) + [
        "--execute",
        "--operation-id",
        "cli-test-001",
        "--allow-target",
        "wxid_target",
    ]
    args = replace_arg(args, name, value)
    prohibit_connection(monkeypatch)
    assert main(args) == 2
    assert json.loads(capsys.readouterr().err)["error"] in {"ValidationError", "PermissionDenied"}
    assert not (tmp_path / "private").exists()


def test_execute_missing_roles_never_initializes_device(
    profile_path, tmp_path, monkeypatch, capsys
):
    data = json.loads(profile_path.read_text())
    del data["roles"]["chat.send"]
    profile_path.write_text(json.dumps(data))
    prohibit_connection(monkeypatch)
    assert (
        main(
            send_args(profile_path, tmp_path)
            + ["--execute", "--operation-id", "cli-test-001", "--allow-target", "wxid_target"]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"] == "UnsupportedFeature"
    assert not (tmp_path / "private").exists()


def test_execute_passes_consent_and_reports_submitted_not_delivered(
    profile_path, tmp_path, monkeypatch, capsys
):
    fake = FakeAdapter()
    constructor = Mock(return_value=fake)
    monkeypatch.setattr("wechatauto_android.adapter.Uiautomator2Adapter", constructor)
    args = send_args(profile_path, tmp_path) + [
        "--execute",
        "--operation-id",
        "cli-test-001",
        "--allow-target",
        "wxid_target",
        "--allow-device-setup",
        "--assume-exclusive-device",
    ]
    assert main(args) == 0
    constructor.assert_called_once_with("SERIAL", allow_device_setup=True)
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "submitted"
    assert fake.commits == ["chat.send"]
    assert (tmp_path / "private" / "ops.sqlite").is_file()
    # 相同 id 即使通过新的 CLI 调用也不能重放最终发送。
    assert main(args) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "DuplicateOperation"
    assert fake.commits == ["chat.send"]


def test_execute_strict_account_proof_missing_fails_before_device(
    profile_path, tmp_path, monkeypatch, capsys
):
    prohibit_connection(monkeypatch)
    args = send_args(profile_path, tmp_path) + [
        "--execute",
        "--operation-id",
        "cli-test-001",
        "--allow-target",
        "wxid_target",
        "--allow-device-setup",
    ]
    assert main(args) == 2
    report = json.loads(capsys.readouterr().err)
    assert report["error"] == "ValidationError"
    assert not (tmp_path / "private").exists()


def test_doctor_reports_exact_metadata_without_account_claim(profile_path, monkeypatch, capsys):
    constructor = Mock(return_value=FakeAdapter())
    monkeypatch.setattr("wechatauto_android.adapter.Uiautomator2Adapter", constructor)
    assert (
        main(
            ["doctor", "--serial", "SERIAL", "--profile", str(profile_path), "--allow-device-setup"]
        )
        == 0
    )
    constructor.assert_called_once_with("SERIAL", allow_device_setup=True)
    report = json.loads(capsys.readouterr().out)
    assert report["device"]["version"] == "8.0.0"
    assert report["device"]["sdk"] == 34
    assert report["account_verified"] is False and report["live_feature_tests_run"] is False


@pytest.mark.parametrize(
    "identity",
    [
        DeviceIdentity("SERIAL", "com.tencent.mm", "8.0.1", 34),
        DeviceIdentity("SERIAL", "other", "8.0.0", 34),
        DeviceIdentity("SERIAL", "com.tencent.mm", "8.0.0", 33),
    ],
)
def test_doctor_rejects_version_package_or_profile_sdk_mismatch(
    profile_path, monkeypatch, capsys, identity
):
    fake = Mock()
    fake.identity.return_value = identity
    monkeypatch.setattr("wechatauto_android.adapter.Uiautomator2Adapter", Mock(return_value=fake))
    assert (
        main(
            ["doctor", "--serial", "SERIAL", "--profile", str(profile_path), "--allow-device-setup"]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"] in {"ValidationError", "IdentityMismatch"}


@pytest.mark.parametrize("command", ["doctor", "snapshot", "send"])
def test_helper_consent_required_before_actual_connect(
    profile_path, tmp_path, monkeypatch, capsys, command
):
    def forbidden(*args, **kwargs):
        pytest.fail("SDK setup was attempted without consent")

    monkeypatch.setattr("wechatauto_android.adapter._connect", forbidden)
    if command == "doctor":
        args = [command, "--serial", "SERIAL", "--profile", str(profile_path)]
    elif command == "snapshot":
        args = [command, "--serial", "SERIAL", "--out", str(tmp_path / "ui.xml")]
    else:
        args = send_args(profile_path, tmp_path) + [
            "--assume-exclusive-device",
            "--execute",
            "--operation-id",
            "cli-test-001",
            "--allow-target",
            "wxid_target",
        ]
    assert main(args) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "PermissionDenied"
    assert not (tmp_path / "ui.xml").exists()
    assert not (tmp_path / "private").exists()


def test_snapshot_delegates_private_output_and_never_prints_xml(tmp_path, monkeypatch, capsys):
    output = tmp_path / "private.xml"
    fake = Mock()
    fake.save_hierarchy.side_effect = lambda path: path.write_text(
        "<hierarchy>PRIVATE FIXTURE CONTENT</hierarchy>"
    )
    constructor = Mock(return_value=fake)
    monkeypatch.setattr("wechatauto_android.adapter.Uiautomator2Adapter", constructor)
    assert (
        main(["snapshot", "--serial", "SERIAL", "--out", str(output), "--allow-device-setup"]) == 0
    )
    constructor.assert_called_once_with("SERIAL", allow_device_setup=True)
    fake.save_hierarchy.assert_called_once_with(output)
    captured = capsys.readouterr()
    assert json.loads(captured.out)["contains_private_data"] is True
    assert "PRIVATE FIXTURE CONTENT" not in captured.out + captured.err
    assert output.is_file()


def test_snapshot_denial_and_device_unavailable_are_json_errors(tmp_path, monkeypatch, capsys):
    output = tmp_path / "private.xml"
    fake = Mock()
    fake.save_hierarchy.side_effect = PermissionDenied("existing output")
    constructor = Mock(return_value=fake)
    monkeypatch.setattr("wechatauto_android.adapter.Uiautomator2Adapter", constructor)
    args = ["snapshot", "--serial", "SERIAL", "--out", str(output), "--allow-device-setup"]
    assert main(args) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "PermissionDenied"
    constructor.side_effect = DeviceUnavailable("offline fixture")
    assert main(args) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "DeviceUnavailable"
    assert not output.exists()
