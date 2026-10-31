"""显式授权、设备互斥及先落盘后提交的去重日志。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .errors import DuplicateOperation, LockTimeout, PermissionDenied, ValidationError
from .models import DeviceIdentity

@dataclass(frozen=True)
class SafetyPolicy:
    serial: str
    account_id: str
    allowed_targets: frozenset[str] = field(default_factory=frozenset)
    allowed_actions: frozenset[str] = field(default_factory=frozenset)
    execute: bool = False
    max_text_length: int = 5000
    lock_timeout: float = 10.0
    assume_exclusive_device: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.serial, str) or not isinstance(self.account_id, str) or not self.serial.strip() or not self.account_id.strip():
            raise ValidationError("Explicit device serial and account ID are required")
        if type(self.assume_exclusive_device) is not bool:
            raise ValidationError("assume_exclusive_device must be an explicit boolean")
        if type(self.execute) is not bool or type(self.max_text_length) is not int or self.max_text_length < 1:
            raise ValidationError("Invalid safety policy")
        if type(self.lock_timeout) not in (int, float) or not 0 < self.lock_timeout <= 300:
            raise ValidationError("lock_timeout must be in (0,300]")
        if isinstance(self.allowed_targets, str) or isinstance(self.allowed_actions, str):
            raise ValidationError("Allowlists must be collections, not strings")
        if any(not isinstance(value, str) or not value for value in (*self.allowed_targets, *self.allowed_actions)):
            raise ValidationError("Allowlists must contain nonempty strings")
        object.__setattr__(self, "allowed_targets", frozenset(self.allowed_targets))
        object.__setattr__(self, "allowed_actions", frozenset(self.allowed_actions))

    def authorize(self, action: str, target: str) -> None:
        if not self.execute:
            raise PermissionDenied("Execution disabled: dry-run is the default")
        if action not in self.allowed_actions or target not in self.allowed_targets:
            raise PermissionDenied("Action and exact target must both be explicitly allowlisted")

    def verify_device(self, identity: DeviceIdentity, package: str, version: str) -> None:
        if type(identity.sdk) is not int or identity.sdk < 30:
            from .errors import IdentityMismatch
            raise IdentityMismatch("Android API 30+ is required")
        if (identity.serial, identity.package, identity.version) != (self.serial, package, version):
            from .errors import IdentityMismatch
            raise IdentityMismatch("Device serial, foreground package or app version changed")

def validate_operation_id(operation_id: str) -> None:
    if not isinstance(operation_id, str) or not 8 <= len(operation_id) <= 128:
        raise ValidationError("operation_id must be 8..128 characters")
    if not all(c.isalnum() or c in "-_.:" for c in operation_id):
        raise ValidationError("Invalid operation_id")


class OperationJournal:
    """仅记录摘要，不保存消息正文。结果不明和已提交的 operation_id 永不重放。"""
    def __init__(self, path: str | Path):
        self.path = Path(path).absolute()
        if any(part.is_symlink() for part in (self.path, *self.path.parents)):
            raise ValidationError("Journal path must not contain symlinks")
        if self.path.exists() and not self.path.is_file():
            raise ValidationError("Journal must be a regular file")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, serial TEXT NOT NULL, account TEXT NOT NULL,
                action TEXT NOT NULL, target TEXT NOT NULL, digest TEXT NOT NULL,
                status TEXT NOT NULL, updated REAL NOT NULL)""")

    def _db(self) -> sqlite3.Connection:
        if any(part.is_symlink() for part in (self.path, *self.path.parents)):
            raise ValidationError("Journal path changed to a symlink")
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def reserve(self, operation_id: str, policy: SafetyPolicy, action: str,
                target: str, payload: object) -> None:
        validate_operation_id(operation_id)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        try:
            with self._db() as db:
                db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)", (
                    operation_id, policy.serial, policy.account_id, action, target,
                    digest, "reserved", time.time()))
        except sqlite3.IntegrityError as exc:
            raise DuplicateOperation("Operation ID already recorded; inspect outcome, never retry") from exc

    def mark(self, operation_id: str, status: str) -> None:
        if status not in {"committing", "submitted", "verified", "unknown", "failed_before_commit"}:
            raise ValidationError("Invalid operation status")
        with self._db() as db:
            cursor = db.execute("UPDATE operations SET status=?, updated=? WHERE id=?",
                                (status, time.time(), operation_id))
            if cursor.rowcount != 1:
                raise ValidationError("Unknown operation ID")

    def status(self, operation_id: str) -> str | None:
        with self._db() as db:
            row = db.execute("SELECT status FROM operations WHERE id=?", (operation_id,)).fetchone()
        return row[0] if row else None

_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()

@contextmanager
def device_lock(serial: str, timeout: float = 10.0) -> Iterator[None]:
    """同一操作系统用户的进程/线程共用每台设备的锁；不覆盖人工操作。"""
    key = hashlib.sha256(serial.encode()).hexdigest()
    with _registry_lock:
        lock = _locks.setdefault(key, threading.Lock())
    deadline = time.monotonic() + timeout
    if not lock.acquire(timeout=timeout):
        raise LockTimeout("Device busy in this process")
    handle = None
    try:
        root = Path(tempfile.gettempdir()) / f"wechatauto-locks-{getattr(os, 'getuid', lambda: 0)()}"
        if root.is_symlink():
            raise ValidationError("Lock directory cannot be a symlink")
        root.mkdir(mode=0o700, exist_ok=True)
        path = root / key
        if path.is_symlink():
            raise ValidationError("Lock file cannot be a symlink")
        handle = open(path, "a+b")
        if os.name == "nt":
            import msvcrt
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            def acquire():
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            def release():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        while True:
            try:
                acquire()
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout("Device busy in another process")
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            release()
    finally:
        if handle:
            handle.close()
        lock.release()
