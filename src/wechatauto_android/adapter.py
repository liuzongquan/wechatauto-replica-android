"""真实 uiautomator2 适配器；精确设备、无写操作重试、无 root。

uiautomator2 3.7.1 的公共 jsonrpc_call 会重启服务并重试。这里固定版本并
覆盖该方法，单次调用已审查的底层 RPC。不能据此声称 UI 观测和点击是原子的；
外部触摸、账号切换、弹窗仍可能竞争，控制器还必须检查账号和会话身份证据。
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import re
import stat
import time
from pathlib import Path, PurePosixPath
from typing import Mapping, Protocol, runtime_checkable

from .errors import (
    AmbiguousTarget,
    DeviceUnavailable,
    IdentityMismatch,
    OutcomeUnknown,
    PermissionDenied,
    StaleSnapshot,
    ValidationError,
)
from .models import DeviceIdentity
from .ui import Hierarchy, Node, Selector

SDK_VERSION = "3.7.1"
PACKAGE = "com.tencent.mm"
REMOTE_ROOT = "/storage/emulated/0/Download/wechatauto"
_MAX_TRANSFER = 100 * 1024 * 1024


@runtime_checkable
class AndroidAdapter(Protocol):
    def identity(self) -> DeviceIdentity: ...
    def hierarchy(self) -> Hierarchy: ...
    def click(self, selector: Selector, *, expected_fingerprint: str | None = None) -> None: ...
    def long_click(
        self, selector: Selector, *, expected_fingerprint: str | None = None
    ) -> None: ...
    def click_node(self, node: Node, *, expected_fingerprint: str) -> None: ...
    def double_click(self, selector: Selector, *, expected_fingerprint: str | None = None) -> None: ...
    def set_text(self, selector: Selector, text: str) -> None: ...
    def press_back(self) -> None: ...
    def swipe(self, direction: str) -> None: ...
    def screenshot(self, path: str | Path) -> None: ...
    def save_hierarchy(self, path: str | Path) -> None: ...
    def push(
        self, local: str | Path, remote: str, *, expected_sha256: str | None = None
    ) -> None: ...
    def pull(self, remote: str, local: str | Path) -> None: ...


def _no_retry_device_class():
    """版本固定的内部接口必须由真实 SDK 合约测试保护。"""
    try:
        if importlib.metadata.version("uiautomator2") != SDK_VERSION:
            raise DeviceUnavailable(f"install audited device extra: uiautomator2=={SDK_VERSION}")
        import uiautomator2
        from uiautomator2.core import _jsonrpc_call
    except ImportError as exc:
        raise DeviceUnavailable(
            "install wechatauto-replica-android[android] to access a device"
        ) from exc

    class NoRetryDevice(uiautomator2.Device):
        def jsonrpc_call(self, method, params=None, timeout=10):
            return _jsonrpc_call(
                self._dev, self._device_server_port, method, params, timeout, False
            )

    return NoRetryDevice


def _connect(serial: str):
    device_class = _no_retry_device_class()
    import adbutils

    try:
        matches = [device for device in adbutils.adb.device_list() if device.serial == serial]
        if len(matches) != 1:
            raise DeviceUnavailable(
                "explicit serial is not exactly one already connected authorized ADB device"
            )
        # 传入 AdbDevice 避免 SDK 自动断开/重新连接远程端点。
        return device_class(matches[0])
    except DeviceUnavailable:
        raise
    except Exception as exc:
        raise DeviceUnavailable("cannot attach the explicitly selected ADB device") from exc


def _validate_serial(serial: str) -> None:
    if not isinstance(serial, str) or not serial or len(serial) > 255:
        raise ValidationError("an explicit ADB serial is required")
    if not re.fullmatch(r"[A-Za-z0-9_.:\-\[\]]+", serial):
        raise ValidationError("invalid ADB serial")


def _local_source(path: str | Path) -> Path:
    source = Path(path).absolute()
    if any(part.is_symlink() for part in (source, *source.parents)):
        raise PermissionDenied("local transfer source must not contain symlinks")
    try:
        result = source.stat()
    except OSError as exc:
        raise ValidationError("local transfer source is unreadable") from exc
    if not stat.S_ISREG(result.st_mode) or result.st_size > _MAX_TRANSFER:
        raise ValidationError("local transfer source must be a regular file <=100 MiB")
    return source


def _exclusive_output(path: str | Path):
    destination = Path(path).absolute()
    if any(part.is_symlink() for part in (destination, *destination.parents)):
        raise PermissionDenied("local output must not contain symlinks")
    if not destination.parent.is_dir():
        raise ValidationError("local output parent must already exist")
    try:
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        return destination, os.fdopen(descriptor, "wb")
    except OSError as exc:
        raise PermissionDenied("local output must be a new file") from exc


class Uiautomator2Adapter:
    """只连接显式序列号；_device 仅供离线适配器合约测试注入。"""

    def __init__(self, serial: str, *, allow_device_setup: bool = False, _device=None):
        _validate_serial(serial)
        if type(allow_device_setup) is not bool:
            raise ValidationError("allow_device_setup must be a boolean")
        if _device is None and not allow_device_setup:
            raise PermissionDenied(
                "connecting starts uiautomator2 and may install /data/local/tmp/u2.jar; "
                "explicit allow_device_setup=True is required, even for diagnostics"
            )
        self.serial = serial
        self._device = _device if _device is not None else _connect(serial)
        self.identity()

    def identity(self) -> DeviceIdentity:
        try:
            serial = self._device.serial
            current = self._device.app_current()
            info = self._device.app_info(PACKAGE)
            screen = self._device.info
        except Exception as exc:
            raise DeviceUnavailable(
                "unable to inspect device and foreground WeChat version"
            ) from exc
        if not all(isinstance(value, Mapping) for value in (current, info, screen)):
            raise DeviceUnavailable("malformed device identity response")
        if serial != self.serial:
            raise IdentityMismatch("device serial changed")
        if current.get("package") != PACKAGE or screen.get("currentPackageName") != PACKAGE:
            raise IdentityMismatch("WeChat must be the foreground accessibility package")
        if screen.get("screenOn") is not True:
            raise IdentityMismatch("device screen is off; no automatic unlocking is performed")
        version = info.get("versionName")
        if not isinstance(version, str) or not version.strip():
            raise IdentityMismatch("WeChat version is unavailable")
        sdk = screen.get("sdkInt")
        if type(sdk) is not int or sdk < 30:
            raise IdentityMismatch(
                "Android API 30+ (Android 11+) is required; SDK must be observable"
            )
        return DeviceIdentity(serial, PACKAGE, version, sdk)

    def _read_hierarchy(self) -> tuple[str, Hierarchy]:
        before = self.identity()
        try:
            xml = self._device.dump_hierarchy(compressed=False, pretty=False, max_depth=128)
        except Exception as exc:
            raise DeviceUnavailable("unable to read hierarchy") from exc
        result = Hierarchy.parse(xml)
        if self.identity() != before:
            raise IdentityMismatch("device identity changed during hierarchy read")
        return xml, result

    def hierarchy(self) -> Hierarchy:
        return self._read_hierarchy()[1]

    def save_hierarchy(self, path: str | Path) -> None:
        """保存经安全校验的原始 XML；私有新文件，不覆盖，也不上传。"""
        xml, _ = self._read_hierarchy()
        destination, output = _exclusive_output(path)
        try:
            with output:
                output.write(xml.encode("utf-8"))
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def _node(self, selector: Selector, fingerprint: str | None = None) -> Node:
        hierarchy = self.hierarchy()
        if fingerprint is not None and fingerprint != hierarchy.fingerprint:
            raise StaleSnapshot("UI changed since the checked snapshot; action was not invoked")
        node = hierarchy.one(selector)
        if node.attrs.get("package") != PACKAGE:
            raise IdentityMismatch("selected node is not owned by WeChat")
        if node.attrs.get("enabled") != "true" or node.attrs.get("visible-to-user") == "false":
            raise PermissionDenied("selected node is disabled or hidden")
        left, top, right, bottom = node.bounds
        try:
            width, height = self._device.window_size()
        except Exception as exc:
            raise DeviceUnavailable("cannot determine display size") from exc
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise PermissionDenied("selected node is offscreen or has no valid hit area")
        return node

    @staticmethod
    def _invoke_once(call) -> None:
        try:
            result = call()
        except Exception as exc:
            raise OutcomeUnknown(
                "device action may have happened; inspect before any manual retry"
            ) from exc
        if result is not True:
            raise OutcomeUnknown(
                "device action did not return affirmative acknowledgement; do not retry"
            )

    def click(self, selector: Selector, *, expected_fingerprint: str | None = None) -> None:
        node = self._node(selector, expected_fingerprint)
        if node.attrs.get("clickable") != "true":
            raise PermissionDenied(
                "selected node is not clickable; select its exact actionable container"
            )
        self.identity()
        # SDK Device.click() 吞掉返回值，因此直接调用单次 RPC 并检查 bool。
        self._invoke_once(lambda: self._device.jsonrpc.click(*node.center))

    def long_click(self, selector: Selector, *, expected_fingerprint: str | None = None) -> None:
        node = self._node(selector, expected_fingerprint)
        if node.attrs.get("long-clickable") != "true":
            raise PermissionDenied("selected node does not support long click")
        self.identity()
        self._invoke_once(lambda: self._device.jsonrpc.click(*node.center, 600))

    def click_node(self, node: Node, *, expected_fingerprint: str) -> None:
        """点击经过父容器作用域解析的节点；必须匹配同一完整快照，不能裸传坐标。"""
        if not isinstance(node, Node) or not isinstance(expected_fingerprint, str):
            raise ValidationError("scoped click requires a parsed Node and its snapshot")
        snapshot = self.hierarchy()
        if snapshot.fingerprint != expected_fingerprint:
            raise StaleSnapshot("scoped node snapshot is stale")
        matches = [candidate for candidate in snapshot.nodes
                   if candidate.bounds == node.bounds and candidate.attrs == node.attrs]
        if len(matches) != 1:
            raise AmbiguousTarget("scoped node is absent or duplicated in the current snapshot")
        if (node.attrs.get("package") != PACKAGE or node.attrs.get("enabled") != "true"
                or node.attrs.get("visible-to-user") == "false"
                or node.attrs.get("clickable") != "true"):
            raise PermissionDenied("scoped node is not an enabled WeChat click target")
        width, height = self._device.window_size()
        left, top, right, bottom = node.bounds
        if not 0 <= left < right <= width or not 0 <= top < bottom <= height:
            raise PermissionDenied("scoped node is outside the display")
        self.identity()
        self._invoke_once(lambda: self._device.jsonrpc.click(*node.center))

    def double_click(self, selector: Selector, *, expected_fingerprint: str | None = None) -> None:
        """一次有界双击：恰好两次点击 RPC，不是失败重试；漂移时停止第二击。"""
        before = self.hierarchy()
        if expected_fingerprint is not None and before.fingerprint != expected_fingerprint:
            raise StaleSnapshot("double-click snapshot is stale before the first tap")
        node = self._node(selector, before.fingerprint)
        if node.attrs.get("clickable") != "true":
            raise PermissionDenied("double-click target must be clickable")
        identity = self.identity()
        self._invoke_once(lambda: self._device.jsonrpc.click(*node.center))
        time.sleep(0.1)
        try:
            if self.identity() != identity:
                raise IdentityMismatch("device changed between deliberate taps")
            current = self._node(selector, before.fingerprint)
            if current.bounds != node.bounds or current.attrs != node.attrs:
                raise StaleSnapshot("double-click target changed between taps")
        except Exception as exc:
            raise OutcomeUnknown("First tap occurred; drift blocked the second tap, do not retry") from exc
        self._invoke_once(lambda: self._device.jsonrpc.click(*node.center))

    def set_text(self, selector: Selector, text: str) -> None:
        if not isinstance(text, str) or len(text) > 20_000 or "\x00" in text:
            raise ValidationError(
                "input text must be a string of at most 20000 characters without NUL"
            )
        try:
            text.encode("utf-8")
        except UnicodeError as exc:
            raise ValidationError("input text must be valid Unicode") from exc
        node = self._node(selector)
        if node.attrs.get("class") != "android.widget.EditText":
            raise PermissionDenied("text input requires an exact EditText node")
        names = {
            "resource_id": "resourceId",
            "description": "description",
            "class_name": "className",
            "package": "packageName",
        }
        kwargs = {names.get(key, key): value for key, value in selector.to_dict().items()}
        kwargs["packageName"] = PACKAGE
        try:
            native = self._device(**kwargs)
            if native.count != 1:
                raise AmbiguousTarget("native selector changed before text input")
        except AmbiguousTarget:
            raise
        except Exception as exc:
            raise DeviceUnavailable("unable to revalidate native text field") from exc
        self.identity()
        if text:
            self._invoke_once(lambda: self._device.jsonrpc.setText(native.selector, text))
        else:
            self._invoke_once(lambda: self._device.jsonrpc.clearTextField(native.selector))

    def press_back(self) -> None:
        self.identity()
        self._invoke_once(lambda: self._device.jsonrpc.pressKey("back"))

    def swipe(self, direction: str) -> None:
        if direction not in {"up", "down", "left", "right"}:
            raise ValidationError("swipe direction must be up/down/left/right")
        self.identity()
        width, height = self._device.window_size()
        points = {
            "up": (0.5, 0.75, 0.5, 0.25),
            "down": (0.5, 0.25, 0.5, 0.75),
            "left": (0.75, 0.5, 0.25, 0.5),
            "right": (0.25, 0.5, 0.75, 0.5),
        }
        fx, fy, tx, ty = points[direction]
        self._invoke_once(
            lambda: self._device.jsonrpc.swipe(
                int(width * fx), int(height * fy), int(width * tx), int(height * ty), 60
            )
        )

    def screenshot(self, path: str | Path) -> None:
        self.identity()
        destination, output = _exclusive_output(path)
        try:
            with output:
                data = self._device.screenshot(format="raw")
                if not isinstance(data, bytes) or not data or len(data) > _MAX_TRANSFER:
                    raise DeviceUnavailable("invalid screenshot response")
                output.write(data)
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def _remote(self, remote: str, *, source: bool) -> str:
        if not isinstance(remote, str) or any(ord(char) < 32 for char in remote) or "\\" in remote:
            raise PermissionDenied("invalid shared-storage path")
        path = PurePosixPath(remote)
        if ".." in path.parts or "." in remote.split("/") or "//" in remote:
            raise PermissionDenied("path traversal is not permitted")
        canonical = (
            remote.replace("/sdcard/", "/storage/emulated/0/", 1)
            if remote.startswith("/sdcard/")
            else remote
        )
        if not canonical.startswith(REMOTE_ROOT + "/") or canonical.endswith("/"):
            raise PermissionDenied(f"transfers are confined to {REMOTE_ROOT}/")
        to_resolve = canonical if source else str(PurePosixPath(canonical).parent)
        response = self._device.shell(["readlink", "-f", to_resolve], timeout=10)
        resolved = response.output.strip()
        if response.exit_code != 0 or resolved != to_resolve:
            raise PermissionDenied("remote shared-storage path is missing, aliased or symlinked")
        info = self._device.adb_device.sync.stat(canonical)
        if source:
            if not stat.S_ISREG(info.mode) or info.size > _MAX_TRANSFER:
                raise PermissionDenied(
                    "remote source must be a regular shared-storage file <=100 MiB"
                )
        elif info.mode:
            raise PermissionDenied("remote destination already exists; overwrite is not allowed")
        return canonical

    def push(self, local: str | Path, remote: str, *, expected_sha256: str | None = None) -> None:
        self.identity()
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        ):
            raise ValidationError("expected_sha256 must be a lowercase SHA-256 hex digest")
        source = _local_source(local)
        try:
            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_TRANSFER:
                    raise ValidationError("upload source changed or exceeds the transfer limit")
                data = stream.read(_MAX_TRANSFER + 1)
        except OSError as exc:
            raise ValidationError("unable to open the exact upload source") from exc
        if len(data) > _MAX_TRANSFER:
            raise ValidationError("upload source grew beyond the transfer limit")
        if expected_sha256 is not None and hashlib.sha256(data).hexdigest() != expected_sha256:
            raise IdentityMismatch("upload bytes differ from the authorized content digest")
        destination = self._remote(remote, source=False)
        try:
            sent = self._device.adb_device.sync.push(data, destination, mode=0o600, check=True)
        except Exception as exc:
            raise OutcomeUnknown("upload may be partial; inspect destination before retry") from exc
        if type(sent) is not int or sent != len(data):
            raise OutcomeUnknown(
                "upload byte count is unconfirmed; inspect destination before retry"
            )

    def pull(self, remote: str, local: str | Path) -> None:
        self.identity()
        source = self._remote(remote, source=True)
        destination, output = _exclusive_output(local)
        try:
            with output:
                for chunk in self._device.adb_device.sync.iter_content(source):
                    if output.tell() + len(chunk) > _MAX_TRANSFER:
                        raise PermissionDenied("remote file exceeded the transfer limit")
                    output.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
