"""SDK/适配器离线合约；没有连接或操作真实 Android/微信设备。"""

from __future__ import annotations

import hashlib
import inspect
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wechatauto_android.adapter import (
    PACKAGE,
    REMOTE_ROOT,
    SDK_VERSION,
    Uiautomator2Adapter,
    _no_retry_device_class,
    _connect,
)
from wechatauto_android.errors import (
    AmbiguousTarget,
    DeviceUnavailable,
    IdentityMismatch,
    OutcomeUnknown,
    PermissionDenied,
    StaleSnapshot,
    ValidationError,
)
from wechatauto_android.ui import Selector

XML = '<hierarchy><node text="发送" package="com.tencent.mm" resource-id="send" class="android.widget.Button" enabled="true" clickable="true" long-clickable="true" bounds="[10,20][110,120]" /></hierarchy>'


def device():
    result = Mock()
    result.serial = "serial-1"
    result.app_current.return_value = {"package": PACKAGE}
    result.app_info.return_value = {"versionName": "test-version"}
    result.info = {"screenOn": True, "currentPackageName": PACKAGE, "sdkInt": 30}
    result.dump_hierarchy.return_value = XML
    result.window_size.return_value = (1080, 1920)
    result.jsonrpc.click.return_value = True
    result.jsonrpc.setText.return_value = True
    result.jsonrpc.clearTextField.return_value = True
    result.jsonrpc.pressKey.return_value = True
    result.jsonrpc.swipe.return_value = True
    result.return_value.count = 1
    result.return_value.selector = {"resourceId": "editor"}
    result.shell.side_effect = lambda cmd, **kwargs: SimpleNamespace(
        output=cmd[-1] + "\n", exit_code=0
    )
    result.adb_device.sync.stat.return_value = SimpleNamespace(mode=0, size=0)
    return result


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.device = device()
        self.adapter = Uiautomator2Adapter("serial-1", _device=self.device)
        self.selector = Selector(resource_id="send")

    def test_scoped_node_requires_same_snapshot(self):
        snapshot = self.adapter.hierarchy()
        node = snapshot.one(self.selector)
        self.adapter.click_node(node, expected_fingerprint=snapshot.fingerprint)
        self.device.jsonrpc.click.assert_called_once_with(*node.center)
        with self.assertRaises(StaleSnapshot):
            self.adapter.click_node(node, expected_fingerprint="changed")
        self.assertEqual(self.device.jsonrpc.click.call_count, 1)

    def test_scoped_node_rejects_foreign_node_and_bad_types(self):
        snapshot = self.adapter.hierarchy()
        from dataclasses import replace
        node = replace(snapshot.one(self.selector), bounds=(1, 2, 3, 4))
        with self.assertRaises(AmbiguousTarget):
            self.adapter.click_node(node, expected_fingerprint=snapshot.fingerprint)
        with self.assertRaises(ValidationError):
            self.adapter.click_node("coordinates", expected_fingerprint=snapshot.fingerprint)
        self.device.jsonrpc.click.assert_not_called()

    def test_scoped_node_never_clicks_disabled_or_nonclickable(self):
        for key in ("enabled", "clickable"):
            self.device.dump_hierarchy.return_value = XML.replace(key + '="true"', key + '="false"')
            snapshot = self.adapter.hierarchy()
            with self.assertRaises(PermissionDenied):
                self.adapter.click_node(snapshot.one(self.selector), expected_fingerprint=snapshot.fingerprint)
        self.device.jsonrpc.click.assert_not_called()

    def test_double_click_is_two_deliberate_taps_not_retry(self):
        with patch("wechatauto_android.adapter.time.sleep") as sleep:
            self.adapter.double_click(self.selector, expected_fingerprint=self.adapter.hierarchy().fingerprint)
        self.assertEqual(self.device.jsonrpc.click.call_count, 2)
        sleep.assert_called_once_with(0.1)

    def test_double_click_first_uncertain_never_second_tap(self):
        self.device.jsonrpc.click.side_effect = TimeoutError("uncertain")
        with self.assertRaises(OutcomeUnknown):
            self.adapter.double_click(self.selector)
        self.assertEqual(self.device.jsonrpc.click.call_count, 1)

    def test_double_click_second_uncertain_never_retry(self):
        self.device.jsonrpc.click.side_effect = [True, TimeoutError("uncertain")]
        with patch("wechatauto_android.adapter.time.sleep"), self.assertRaises(OutcomeUnknown):
            self.adapter.double_click(self.selector)
        self.assertEqual(self.device.jsonrpc.click.call_count, 2)

    def test_identity_and_hierarchy(self):
        self.assertEqual(self.adapter.identity().serial, "serial-1")
        self.assertEqual(self.adapter.identity().version, "test-version")
        self.assertEqual(self.adapter.hierarchy().one(self.selector).text, "发送")
        self.device.dump_hierarchy.assert_called_with(compressed=False, pretty=False, max_depth=128)

    def test_setup_consent_is_required_before_connect(self):
        with patch("wechatauto_android.adapter._connect") as connect:
            with self.assertRaises(PermissionDenied):
                Uiautomator2Adapter("serial-1")
            with self.assertRaises(ValidationError):
                Uiautomator2Adapter("serial-1", allow_device_setup="true")
            connect.assert_not_called()
            connect.return_value = self.device
            Uiautomator2Adapter("serial-1", allow_device_setup=True)
            connect.assert_called_once_with("serial-1")

    def test_save_hierarchy_preserves_original_private_xml(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "hierarchy.xml"
            self.adapter.save_hierarchy(output)
            self.assertEqual(output.read_text(), XML)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            with self.assertRaises(PermissionDenied):
                self.adapter.save_hierarchy(output)

    def test_save_hierarchy_invalid_xml_creates_nothing(self):
        self.device.dump_hierarchy.return_value = "malformed"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "hierarchy.xml"
            with self.assertRaises(ValidationError):
                self.adapter.save_hierarchy(output)
            self.assertFalse(output.exists())

    def test_explicit_serial_validation(self):
        for value in (None, "", "-s device; ls", "https://endpoint", "serial\n", "x" * 256):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Uiautomator2Adapter(value, _device=self.device)

    def test_serial_identity_mismatch(self):
        self.device.serial = "other"
        with self.assertRaises(IdentityMismatch):
            self.adapter.identity()

    def test_malformed_identity_data_is_rejected(self):
        self.device.app_current.return_value = None
        with self.assertRaises(DeviceUnavailable):
            self.adapter.identity()

    def test_wrong_package_is_rejected(self):
        self.device.app_current.return_value = {"package": "other"}
        with self.assertRaises(IdentityMismatch):
            self.adapter.click(self.selector)
        self.device.jsonrpc.click.assert_not_called()

    def test_screen_off_is_rejected(self):
        self.device.info["screenOn"] = False
        with self.assertRaises(IdentityMismatch):
            self.adapter.identity()

    def test_accessibility_overlay_is_rejected(self):
        self.device.info["currentPackageName"] = "com.android.systemui"
        with self.assertRaises(IdentityMismatch):
            self.adapter.identity()

    def test_android_floor_and_missing_sdk(self):
        for sdk in (None, 29, "30", True):
            self.device.info["sdkInt"] = sdk
            with self.subTest(sdk=sdk), self.assertRaises(IdentityMismatch):
                self.adapter.identity()

    def test_missing_version_and_device_failure(self):
        self.device.app_info.return_value = {}
        with self.assertRaises(IdentityMismatch):
            self.adapter.identity()
        self.device.app_current.side_effect = TimeoutError
        with self.assertRaises(DeviceUnavailable):
            self.adapter.identity()

    def test_hierarchy_failure(self):
        self.device.dump_hierarchy.side_effect = TimeoutError
        with self.assertRaises(DeviceUnavailable):
            self.adapter.hierarchy()

    def test_identity_drift_during_hierarchy(self):
        self.device.app_info.side_effect = [{"versionName": "a"}, {"versionName": "b"}]
        with self.assertRaises(IdentityMismatch):
            self.adapter.hierarchy()

    def test_click_and_long_click_acknowledgements(self):
        fingerprint = self.adapter.hierarchy().fingerprint
        self.adapter.click(self.selector, expected_fingerprint=fingerprint)
        self.device.jsonrpc.click.assert_called_once_with(60, 70)
        self.device.jsonrpc.click.reset_mock()
        self.adapter.long_click(self.selector, expected_fingerprint=fingerprint)
        self.device.jsonrpc.click.assert_called_once_with(60, 70, 600)

    def test_stale_snapshot_blocks_before_click(self):
        with self.assertRaises(StaleSnapshot):
            self.adapter.click(self.selector, expected_fingerprint="not-current")
        self.device.jsonrpc.click.assert_not_called()

    def test_nonaffirmative_ack_is_unknown_without_retry(self):
        for value in (False, None, 1, "true"):
            self.device.jsonrpc.click.reset_mock()
            self.device.jsonrpc.click.return_value = value
            with self.subTest(value=value), self.assertRaises(OutcomeUnknown):
                self.adapter.click(self.selector)
            self.device.jsonrpc.click.assert_called_once()

    def test_click_timeout_is_unknown_without_retry(self):
        self.device.jsonrpc.click.side_effect = TimeoutError
        with self.assertRaises(OutcomeUnknown):
            self.adapter.click(self.selector)
        self.device.jsonrpc.click.assert_called_once()

    def test_duplicate_button_blocks_click(self):
        self.device.dump_hierarchy.return_value = XML.replace("</hierarchy>", XML[11:])
        with self.assertRaises(AmbiguousTarget):
            self.adapter.click(self.selector)
        self.device.jsonrpc.click.assert_not_called()

    def test_missing_display_size_is_rejected(self):
        self.device.window_size.side_effect = TimeoutError
        with self.assertRaises(DeviceUnavailable):
            self.adapter.click(self.selector)
        self.device.jsonrpc.click.assert_not_called()

    def test_nonactionable_node_blocks_click(self):
        changes = [
            ('enabled="true"', 'enabled="false"'),
            ('clickable="true"', 'clickable="false"'),
            ("[10,20][110,120]", "[0,0][0,0]"),
            ("[10,20][110,120]", "[0,0][2000,3000]"),
        ]
        for old, new in changes:
            self.device.dump_hierarchy.return_value = XML.replace(old, new)
            with self.subTest(change=new), self.assertRaises(PermissionDenied):
                self.adapter.click(self.selector)
        self.device.jsonrpc.click.assert_not_called()

    def test_foreign_node_blocks_click(self):
        self.device.dump_hierarchy.return_value = XML.replace(
            'package="com.tencent.mm"', 'package="evil"'
        )
        with self.assertRaises(IdentityMismatch):
            self.adapter.click(self.selector)
        self.device.jsonrpc.click.assert_not_called()

    def test_hidden_node_and_non_longclickable(self):
        self.device.dump_hierarchy.return_value = XML.replace("/>", 'visible-to-user="false"/>')
        with self.assertRaises(PermissionDenied):
            self.adapter.click(self.selector)
        self.device.dump_hierarchy.return_value = XML.replace(
            'long-clickable="true"', 'long-clickable="false"'
        )
        with self.assertRaises(PermissionDenied):
            self.adapter.long_click(self.selector)

    def test_set_text_preserves_unicode_and_clears(self):
        self.device.dump_hierarchy.return_value = XML.replace(
            "android.widget.Button", "android.widget.EditText"
        )
        content = "中文 e\u0301 👩\u200d💻 & < > ' \"\n"
        self.adapter.set_text(self.selector, content)
        self.device.jsonrpc.setText.assert_called_once_with({"resourceId": "editor"}, content)
        self.adapter.set_text(self.selector, "")
        self.device.jsonrpc.clearTextField.assert_called_once_with({"resourceId": "editor"})

    def test_set_text_requires_edittext(self):
        with self.assertRaises(PermissionDenied):
            self.adapter.set_text(self.selector, "value")
        self.device.jsonrpc.setText.assert_not_called()

    def test_invalid_text(self):
        for content in (None, "x" * 20001, "\x00", "\ud800"):
            with self.subTest(content=str(content)[:20]), self.assertRaises(ValidationError):
                self.adapter.set_text(self.selector, content)

    def test_text_rechecks_native_count(self):
        self.device.dump_hierarchy.return_value = XML.replace(
            "android.widget.Button", "android.widget.EditText"
        )
        self.device.return_value.count = 2
        with self.assertRaises(AmbiguousTarget):
            self.adapter.set_text(self.selector, "x")
        self.device.jsonrpc.setText.assert_not_called()

    def test_native_selector_transport_failure_prevents_text(self):
        self.device.dump_hierarchy.return_value = XML.replace(
            "android.widget.Button", "android.widget.EditText"
        )
        self.device.side_effect = TimeoutError
        with self.assertRaises(DeviceUnavailable):
            self.adapter.set_text(self.selector, "x")
        self.device.jsonrpc.setText.assert_not_called()

    def test_text_false_is_unknown(self):
        self.device.dump_hierarchy.return_value = XML.replace(
            "android.widget.Button", "android.widget.EditText"
        )
        self.device.jsonrpc.setText.return_value = False
        with self.assertRaises(OutcomeUnknown):
            self.adapter.set_text(self.selector, "x")
        self.device.jsonrpc.setText.assert_called_once()

    def test_back_and_bounded_swipe(self):
        self.adapter.press_back()
        self.device.jsonrpc.pressKey.assert_called_once_with("back")
        for direction in ("up", "down", "left", "right"):
            self.adapter.swipe(direction)
        self.assertEqual(self.device.jsonrpc.swipe.call_count, 4)
        with self.assertRaises(ValidationError):
            self.adapter.swipe("next")

    def test_screenshot_no_overwrite(self):
        self.device.screenshot.return_value = b"screenshot fixture"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shot.jpg"
            self.adapter.screenshot(path)
            self.assertEqual(path.read_bytes(), b"screenshot fixture")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(PermissionDenied):
                self.adapter.screenshot(path)

    def test_output_symlink_and_missing_parent_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / "linked"
            link.symlink_to(Path(directory) / "real")
            with self.assertRaises(PermissionDenied):
                self.adapter.screenshot(link)
            with self.assertRaises(ValidationError):
                self.adapter.screenshot(Path(directory) / "missing" / "output")
            self.device.screenshot.assert_not_called()

    def test_screenshot_failure_cleans_partial_output(self):
        self.device.screenshot.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shot.jpg"
            with self.assertRaises(DeviceUnavailable):
                self.adapter.screenshot(path)
            self.assertFalse(path.exists())

    def test_remote_paths_reject_private_and_traversal(self):
        invalid = [
            "/data/data/com.tencent.mm/databases/x",
            "/sdcard/Pictures/x",
            REMOTE_ROOT + "/../secret",
            REMOTE_ROOT + "/./x",
            REMOTE_ROOT + "//x",
            REMOTE_ROOT + "/x\n",
            REMOTE_ROOT + "/x\\y",
            REMOTE_ROOT + "/",
        ]
        for remote in invalid:
            with self.subTest(remote=remote), self.assertRaises(PermissionDenied):
                self.adapter._remote(remote, source=False)

    def test_remote_symlink_rejected(self):
        self.device.shell.side_effect = None
        self.device.shell.return_value = SimpleNamespace(output="/data/private", exit_code=0)
        with self.assertRaises(PermissionDenied):
            self.adapter._remote(REMOTE_ROOT + "/file", source=True)

    def test_remote_missing_regular_and_oversized(self):
        with self.assertRaises(PermissionDenied):
            self.adapter._remote(REMOTE_ROOT + "/file", source=True)
        self.device.adb_device.sync.stat.return_value = SimpleNamespace(
            mode=stat.S_IFREG, size=100 * 1024 * 1024 + 1
        )
        with self.assertRaises(PermissionDenied):
            self.adapter._remote(REMOTE_ROOT + "/file", source=True)

    def test_missing_or_nonregular_upload_source(self):
        with tempfile.TemporaryDirectory() as directory:
            for source in (Path(directory), Path(directory) / "missing"):
                with self.subTest(source=source), self.assertRaises(ValidationError):
                    self.adapter.push(source, REMOTE_ROOT + "/send")
            self.device.adb_device.sync.push.assert_not_called()

    def test_pull_growth_aborts_and_removes_output(self):
        self.device.adb_device.sync.stat.return_value = SimpleNamespace(mode=stat.S_IFREG, size=2)
        self.device.adb_device.sync.iter_content.return_value = iter((b"12", b"34"))
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("wechatauto_android.adapter._MAX_TRANSFER", 3),
        ):
            output = Path(directory) / "out"
            with self.assertRaises(PermissionDenied):
                self.adapter.pull(REMOTE_ROOT + "/remote", output)
            self.assertFalse(output.exists())

    def test_push_and_overwrite_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "send.txt"
            source.write_text("authorized fixture")
            self.device.adb_device.sync.push.return_value = len(source.read_bytes())
            self.adapter.push(
                source,
                "/sdcard/Download/wechatauto/send.txt",
                expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            )
            self.device.adb_device.sync.push.assert_called_once_with(
                source.read_bytes(), REMOTE_ROOT + "/send.txt", mode=0o600, check=True
            )
            self.device.adb_device.sync.stat.return_value = SimpleNamespace(
                mode=stat.S_IFREG, size=1
            )
            with self.assertRaises(PermissionDenied):
                self.adapter.push(source, REMOTE_ROOT + "/send.txt")

    def test_changed_upload_bytes_fail_before_transfer(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "send"
            source.write_bytes(b"authorized")
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            source.write_bytes(b"different")
            with self.assertRaises(IdentityMismatch):
                self.adapter.push(source, REMOTE_ROOT + "/send", expected_sha256=expected)
            self.device.adb_device.sync.push.assert_not_called()
            for digest in ("bad", 123, "A" * 64):
                with self.subTest(digest=digest), self.assertRaises(ValidationError):
                    self.adapter.push(source, REMOTE_ROOT + "/send", expected_sha256=digest)

    def test_upload_unconfirmed_bytecount_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "send"
            source.write_bytes(b"bytes")
            self.device.adb_device.sync.push.return_value = None
            with self.assertRaises(OutcomeUnknown):
                self.adapter.push(source, REMOTE_ROOT + "/send")
            self.device.adb_device.sync.push.assert_called_once()

    def test_push_symlink_and_uncertain_transfer(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.write_text("fixture")
            link = Path(directory) / "symlink"
            link.symlink_to(source)
            with self.assertRaises(PermissionDenied):
                self.adapter.push(link, REMOTE_ROOT + "/send")
            self.device.adb_device.sync.push.side_effect = TimeoutError
            with self.assertRaises(OutcomeUnknown):
                self.adapter.push(source, REMOTE_ROOT + "/send")
            self.device.adb_device.sync.push.assert_called_once()

    def test_pull_and_no_overwrite(self):
        self.device.adb_device.sync.stat.return_value = SimpleNamespace(mode=stat.S_IFREG, size=2)
        self.device.adb_device.sync.iter_content.return_value = iter((b"a", b"b"))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "local"
            self.adapter.pull(REMOTE_ROOT + "/remote", output)
            self.assertEqual(output.read_bytes(), b"ab")
            with self.assertRaises(PermissionDenied):
                self.adapter.pull(REMOTE_ROOT + "/remote", output)

    def test_pull_failure_removes_partial_file(self):
        self.device.adb_device.sync.stat.return_value = SimpleNamespace(mode=stat.S_IFREG, size=2)

        def chunks():
            yield b"a"
            raise TimeoutError

        self.device.adb_device.sync.iter_content.return_value = chunks()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "local"
            with self.assertRaises(TimeoutError):
                self.adapter.pull(REMOTE_ROOT + "/remote", output)
            self.assertFalse(output.exists())


class InstalledSDKContractTests(unittest.TestCase):
    """安装的真实 SDK 合约测试；不调用 connect，也不操作任何设备。"""

    def test_connection_never_guesses_serial_or_reconnects(self):
        selected = SimpleNamespace(serial="selected")
        other = SimpleNamespace(serial="other")
        with (
            patch("wechatauto_android.adapter._no_retry_device_class") as factory,
            patch("adbutils.adb.device_list", return_value=[other, selected]),
        ):
            self.assertIs(_connect("selected"), factory.return_value.return_value)
            factory.return_value.assert_called_once_with(selected)
        for devices in ([], [other], [selected, selected]):
            with (
                self.subTest(devices=devices),
                patch("wechatauto_android.adapter._no_retry_device_class") as factory,
                patch("adbutils.adb.device_list", return_value=devices),
            ):
                with self.assertRaises(DeviceUnavailable):
                    _connect("selected")
                factory.return_value.assert_not_called()

    def test_adb_disconnection_is_a_domain_failure(self):
        with (
            patch("wechatauto_android.adapter._no_retry_device_class"),
            patch("adbutils.adb.device_list", side_effect=TimeoutError),
        ):
            with self.assertRaises(DeviceUnavailable):
                _connect("selected")

    def test_missing_sdk_extra_has_actionable_failure(self):
        from importlib.metadata import PackageNotFoundError

        with patch("importlib.metadata.version", side_effect=PackageNotFoundError):
            with self.assertRaisesRegex(DeviceUnavailable, "android"):
                _no_retry_device_class()

    def test_pinned_sdk_contract_exists(self):
        import importlib.metadata
        from adbutils.sync import Sync
        from uiautomator2 import Device
        from uiautomator2.core import _jsonrpc_call

        self.assertEqual(importlib.metadata.version("uiautomator2"), SDK_VERSION)
        self.assertEqual(
            list(inspect.signature(_jsonrpc_call).parameters),
            ["dev", "device_port", "method", "params", "timeout", "print_request"],
        )
        for name in (
            "serial",
            "app_current",
            "app_info",
            "dump_hierarchy",
            "window_size",
            "jsonrpc",
            "screenshot",
            "shell",
            "adb_device",
        ):
            self.assertTrue(hasattr(Device, name), name)
        self.assertTrue(hasattr(Sync, "iter_content"))
        self.assertIn("check", inspect.signature(Sync.push).parameters)

    def test_rpc_error_does_not_restart_or_retry(self):
        from uiautomator2.exceptions import HTTPError

        with patch("uiautomator2.core._jsonrpc_call", side_effect=HTTPError("uncertain")) as raw:
            cls = _no_retry_device_class()
            instance = object.__new__(cls)
            instance._dev = object()
            instance._device_server_port = 9008
            instance.start_uiautomator = Mock()
            instance.stop_uiautomator = Mock()
            with self.assertRaises(HTTPError):
                instance.jsonrpc.click(1, 2, http_timeout=10)
            raw.assert_called_once_with(instance._dev, 9008, "click", (1, 2), 10, False)
            instance.start_uiautomator.assert_not_called()
            instance.stop_uiautomator.assert_not_called()

    def test_real_rpc_marshalling_preserves_false_ack_and_unicode(self):
        from uiautomator2.core import _jsonrpc_call

        response = SimpleNamespace(json=lambda: {"result": False})
        content = "中文 👩\u200d💻 & < >"
        with patch("uiautomator2.core._http_request", return_value=response) as transport:
            self.assertIs(
                _jsonrpc_call(object(), 9008, "setText", ({"resourceId": "x"}, content), 10, False),
                False,
            )
            transport.assert_called_once()
            payload = transport.call_args.args[4]
            self.assertEqual(payload["params"][1], content)
            self.assertEqual(payload["method"], "setText")

    def test_native_selector_parameters_match_installed_sdk(self):
        from uiautomator2._selector import Selector as NativeSelector

        values = {
            "text": "消息",
            "resourceId": "id",
            "description": "描述",
            "className": "android.widget.EditText",
            "packageName": PACKAGE,
            "enabled": True,
            "clickable": True,
        }
        selector = NativeSelector(**values)
        for name, value in values.items():
            self.assertEqual(selector[name], value)

    def test_sdk_version_drift_is_rejected(self):
        with patch("importlib.metadata.version", return_value="999.0"):
            with self.assertRaises(DeviceUnavailable):
                _no_retry_device_class()


if __name__ == "__main__":
    unittest.main()
