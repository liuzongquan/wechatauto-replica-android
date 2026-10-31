"""Exercise bounded subprocess IO with a local fake executable, never ADB or a device."""
import json
import os
import stat
import sys

import pytest

from wechatauto_android.errors import DeviceUnavailable
from wechatauto_android.rooted import AdbRootSnapshotter, RootSnapshotConfig


@pytest.fixture
def transport(tmp_path):
    fake = tmp_path / "synthetic-adb"
    fake.write_text(f"#!{sys.executable}\n" + '''import json, sys, time
mode = sys.argv[3]
if mode == "echo":
    print(json.dumps(sys.argv[1:]))
elif mode == "binary":
    sys.stdout.buffer.write(bytes(range(256)))
elif mode == "stdout-overflow":
    sys.stdout.buffer.write(b"x" * 200000)
elif mode == "stderr-overflow":
    sys.stderr.buffer.write(b"x" * 200000)
elif mode == "failure":
    print("PRIVATE_REMOTE_ERROR_MUST_NOT_LEAK", file=sys.stderr)
    raise SystemExit(7)
elif mode == "sleep":
    time.sleep(10)
elif mode == "invalid-text":
    sys.stdout.buffer.write(b"\\xff")
''')
    fake.chmod(0o700)
    config = RootSnapshotConfig("SYNTHETIC-ONLY", "a" * 32, "test-account", "8.0.0",
                                consent_root_read=True, confirm_quiesced=True, timeout_seconds=1)
    return AdbRootSnapshotter(config, adb=str(fake))


def test_process_arguments_are_not_interpolated_by_host_shell(transport):
    payload = "literal; $(do-not-run) 'quoted'"
    result = json.loads(transport._small(["echo", payload]))
    assert result == ["-s", "SYNTHETIC-ONLY", "echo", payload]


def test_real_pipe_binary_stream_to_private_exclusive_output(transport, tmp_path):
    target = tmp_path / "capture.bin"
    assert transport._run(["binary"], output=target) == b""
    assert target.read_bytes() == bytes(range(256))
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_pipe_memory_result_preserves_raw_bytes(transport):
    assert transport._run(["binary"]) == bytes(range(256))

@pytest.mark.parametrize("mode", ["stdout-overflow", "stderr-overflow"])
def test_pipe_output_bounds_terminate_child(transport, mode):
    with pytest.raises(DeviceUnavailable, match="exceeded limit"):
        transport._run([mode], limit=1024)


def test_remote_failure_output_is_not_exposed(transport):
    with pytest.raises(DeviceUnavailable) as exc:
        transport._run(["failure"])
    assert "PRIVATE_REMOTE_ERROR" not in str(exc.value)


def test_real_process_timeout_is_bounded(transport):
    with pytest.raises(DeviceUnavailable, match="timed out"):
        transport._run(["sleep"])


def test_invalid_text_pipe_fails_closed(transport):
    with pytest.raises(DeviceUnavailable, match="invalid text"):
        transport._small(["invalid-text"])


def test_output_is_never_overwritten(transport, tmp_path):
    path = tmp_path / "existing.bin"
    path.write_bytes(b"unchanged")
    with pytest.raises(FileExistsError):
        transport._run(["binary"], output=path)
    assert path.read_bytes() == b"unchanged"


def test_missing_executable_reports_domain_error(transport):
    os.unlink(transport.adb)
    with pytest.raises(DeviceUnavailable, match="cannot start"):
        transport._run(["echo"])
