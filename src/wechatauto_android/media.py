"""用户授权的本地导出媒体复制；无手机解密、密钥提取或静默设备下载。"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from pathlib import Path, PurePosixPath

from .errors import ValidationError
from .models import Message


def _parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or any(char in value for char in ("\x00", "\\", ":")):
        raise ValidationError("media path must be a nonempty relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValidationError("absolute/traversal media paths are forbidden")
    return path.parts


def _open_dir(path: Path) -> int:
    root = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return _walk(root, path.parts[1:])
    finally:
        os.close(root)


def _walk(root_fd: int, parts: tuple[str, ...], *, create: bool = False) -> int:
    current = os.dup(root_fd)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


class MediaDownloader:
    """从一个明确授权的本地目录复制导出媒体；目标永不覆盖。"""

    def __init__(self, source_root: str | Path, output_root: str | Path, *, max_bytes: int = 512 * 1024 * 1024) -> None:
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY") or os.open not in os.supports_dir_fd:
            raise ValidationError("secure media copying requires POSIX dir_fd and O_NOFOLLOW support")
        self.source_root = Path(source_root).absolute()
        self.output_root = Path(output_root).absolute()
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValidationError("max_bytes must be positive")
        self.max_bytes = max_bytes
        for root in (self.source_root, self.output_root):
            if root.resolve() != root or root.is_symlink() or not root.is_dir():
                raise ValidationError("media roots must be existing real directories without symlink ancestors")
        if self.source_root == self.output_root or self.source_root in self.output_root.parents or self.output_root in self.source_root.parents:
            raise ValidationError("media source/output roots must be disjoint")

    def download(self, relative_path: str, *, destination: str | None = None,
                 sha256: str | None = None) -> Path:
        """文件描述符逐级打开，拒绝中途符号链接/特殊文件/覆盖；校验后再发布。"""
        source_parts = _parts(relative_path)
        target_parts = _parts(destination if destination is not None else relative_path)
        if sha256 is not None and (not isinstance(sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None):
            raise ValidationError("sha256 must contain exactly 64 hexadecimal characters")
        descriptors: list[int] = []
        temp_name: str | None = None
        target_parent: int | None = None
        try:
            source_root = _open_dir(self.source_root)
            descriptors.append(source_root)
            target_root = _open_dir(self.output_root)
            descriptors.append(target_root)
            source_parent = _walk(source_root, source_parts[:-1])
            descriptors.append(source_parent)
            source = os.open(source_parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_parent)
            descriptors.append(source)
            info = os.fstat(source)
            if not stat.S_ISREG(info.st_mode) or info.st_size > self.max_bytes:
                raise ValidationError("media source must be a regular file within size limit")
            target_parent = _walk(target_root, target_parts[:-1], create=True)
            descriptors.append(target_parent)
            temp_name = ".wechat-media-" + uuid.uuid4().hex
            target = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=target_parent)
            descriptors.append(target)
            digest = hashlib.sha256()
            total = 0
            while True:
                data = os.read(source, 1024 * 1024)
                if not data:
                    break
                total += len(data)
                if total > self.max_bytes:
                    raise ValidationError("media grew beyond size limit")
                digest.update(data)
                view = memoryview(data)
                while view:
                    written = os.write(target, view)
                    if written <= 0:
                        raise OSError("short media write")
                    view = view[written:]
            if sha256 is not None and digest.hexdigest() != sha256.lower():
                raise ValidationError("media SHA-256 mismatch")
            os.fsync(target)
            # link 是原子不覆盖发布；现存普通文件和符号链接都不会被跟随或覆盖。
            os.link(temp_name, target_parts[-1], src_dir_fd=target_parent, dst_dir_fd=target_parent, follow_symlinks=False)
            os.unlink(temp_name, dir_fd=target_parent)
            temp_name = None
            return self.output_root.joinpath(*target_parts)
        except OSError as exc:
            raise ValidationError(f"media copy refused/failed: {exc}") from exc
        finally:
            if temp_name is not None and target_parent is not None:
                try:
                    os.unlink(temp_name, dir_fd=target_parent)
                except FileNotFoundError:
                    pass
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def download_message(self, message: Message, *, account_id: str, chat_id: str) -> Path:
        """媒体清单绑定指定账号/会话，不能把其它会话的同名 id 当作当前消息。"""
        if message.account_id != account_id or message.chat_id != chat_id:
            from .errors import IdentityMismatch
            raise IdentityMismatch("media message belongs to another account or chat")
        if message.origin != "user_export":
            raise ValidationError("local exported-media downloader requires user_export provenance")
        relative_path = message.metadata.get("media_path")
        if not isinstance(relative_path, str):
            raise ValidationError("message has no exported media_path")
        return self.download(relative_path, sha256=message.metadata.get("sha256"))
