"""显式来源的顺序轮询；游标绑定账号及来源，不声称实时 Android 数据库监听。"""
from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Any, Callable, Protocol

from .archive import _number, _string, atomic_json, read_json
from .errors import IdentityMismatch, LockTimeout, UnsupportedFeature, ValidationError
from .models import Message


class MessageSource(Protocol):
    account_id: str
    source_id: str

    def get_messages_after(self, chat_id: str, after: tuple[int, str] | None = None,
                           limit: int = 200) -> list[Message]: ...


Callback = Callable[[Message, "Listener"], None]


class Listener:
    """按 (sequence,id) 游标投递。回调失败/崩溃后可能重投，回调须自行幂等。

    不接受只有 strict since_seq 的来源，因为同序号分页可能永久丢消息。
    来源必须保持游标前数据不变且不能后插入；ExportArchive.refresh 会验证此条件。
    """

    def __init__(self, source: MessageSource, account_id: str, watermark_file: str | Path | None = None,
                 interval: float = 1.0, batch_size: int = 200) -> None:
        self.source = source
        self.account_id = _string(account_id, "account_id")
        self.source_id = _string(getattr(source, "source_id", None), "source.source_id")
        if getattr(source, "account_id", None) != self.account_id:
            raise IdentityMismatch("listener account does not match source")
        if not callable(getattr(source, "get_messages_after", None)):
            raise UnsupportedFeature("listener requires a lossless (sequence,id) cursor source")
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 0.01 <= interval <= 3600:
            raise ValidationError("interval must be between 0.01 and 3600 seconds")
        _number(batch_size, "batch_size", 10_000)
        if not batch_size:
            raise ValidationError("batch_size must be positive")
        self.interval = float(interval)
        self.batch_size = batch_size
        self.watermark_file = Path(watermark_file).absolute() if watermark_file is not None else None
        self._cursors: dict[str, tuple[int, str]] = {}
        self._callbacks: dict[str, list[Callback]] = {}
        self._registration_lock = threading.RLock()
        self._poll_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: Exception | None = None
        if self.watermark_file is not None and (self.watermark_file.exists() or self.watermark_file.is_symlink()):
            self._load_watermark()

    def _load_watermark(self) -> None:
        data = read_json(self.watermark_file, max_bytes=4 * 1024 * 1024)  # type: ignore[arg-type]
        if set(data) != {"schema_version", "account_id", "source_id", "cursors"} or type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValidationError("invalid listener checkpoint schema")
        if data["account_id"] != self.account_id or data["source_id"] != self.source_id:
            raise IdentityMismatch("checkpoint belongs to another account or source")
        if not isinstance(data["cursors"], dict):
            raise ValidationError("invalid checkpoint cursors")
        for chat, cursor in data["cursors"].items():
            _string(chat, "checkpoint chat")
            if not isinstance(cursor, list) or len(cursor) != 2:
                raise ValidationError("invalid checkpoint cursor")
            _number(cursor[0], "checkpoint sequence", 2**63 - 1)
            _string(cursor[1], "checkpoint id")
            self._cursors[chat] = (cursor[0], cursor[1])

    @property
    def watermark(self) -> dict[str, tuple[int, str]]:
        with self._registration_lock:
            return dict(self._cursors)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def add_listener(self, chat_id: str, callback: Callback) -> None:
        _string(chat_id, "chat_id")
        if not callable(callback):
            raise ValidationError("callback must be callable")
        with self._registration_lock:
            callbacks = self._callbacks.setdefault(chat_id, [])
            if callback not in callbacks:
                callbacks.append(callback)

    def remove_listener(self, chat_id: str, callback: Callback | None = None) -> None:
        _string(chat_id, "chat_id")
        with self._registration_lock:
            if callback is None:
                self._callbacks.pop(chat_id, None)
            elif callback in self._callbacks.get(chat_id, []):
                self._callbacks[chat_id].remove(callback)
                if not self._callbacks[chat_id]:
                    del self._callbacks[chat_id]

    def save_watermark(self, force: bool = False) -> None:
        del force  # 每次已确认消息都原子持久化，不节流。
        if self.watermark_file is None:
            return
        with self._registration_lock:
            atomic_json(self.watermark_file, {"schema_version": 1, "account_id": self.account_id,
                                              "source_id": self.source_id,
                                              "cursors": {chat: list(cursor) for chat, cursor in self._cursors.items()}})

    def _check_identity(self) -> None:
        if getattr(self.source, "account_id", None) != self.account_id or getattr(self.source, "source_id", None) != self.source_id:
            raise IdentityMismatch("listener source identity changed")

    def poll_once(self) -> int:
        if not self._poll_lock.acquire(blocking=False):
            raise LockTimeout("listener poll already in progress")
        delivered = 0
        try:
            if self._stop.is_set():
                return 0
            self._check_identity()
            refresh = getattr(self.source, "refresh", None)
            if callable(refresh):
                refresh()
            self._check_identity()
            with self._registration_lock:
                registrations = [(chat, tuple(callbacks)) for chat, callbacks in sorted(self._callbacks.items())]
            for chat, callbacks in registrations:
                after = self._cursors.get(chat)
                rows = self.source.get_messages_after(chat, after=after, limit=self.batch_size)
                self._check_identity()
                if not isinstance(rows, list) or len(rows) > self.batch_size:
                    raise ValidationError("source returned invalid or oversized batch")
                previous = after
                seen: set[str] = set()
                for row in rows:
                    if not isinstance(row, Message):
                        raise ValidationError("source must return Message instances")
                    if row.account_id != self.account_id or row.chat_id != chat:
                        raise IdentityMismatch("source returned cross-account or cross-chat message")
                    _number(row.sequence, "message.sequence", 2**63 - 1)
                    _string(row.id, "message.id")
                    key = (row.sequence, row.id)
                    if row.id in seen or (previous is not None and key <= previous):
                        raise ValidationError("source batch is not strictly ordered and unique")
                    seen.add(row.id)
                    previous = key
                # 整批先校验，再调用任何用户回调。
                for row in rows:
                    if self._stop.is_set():
                        return delivered
                    self._check_identity()
                    with self._registration_lock:
                        callbacks = tuple(self._callbacks.get(chat, ()))
                    if not callbacks:
                        break
                    for callback in callbacks:
                        if self._stop.is_set():
                            return delivered
                        with self._registration_lock:
                            if callback not in self._callbacks.get(chat, ()):
                                continue
                        self._check_identity()
                        callback(copy.deepcopy(row), self)
                    self._check_identity()
                    cursor = (row.sequence, row.id)
                    with self._registration_lock:
                        old = self._cursors.get(chat)
                        self._cursors[chat] = cursor
                        try:
                            self.save_watermark()
                        except Exception:
                            if old is None:
                                self._cursors.pop(chat, None)
                            else:
                                self._cursors[chat] = old
                            raise
                    delivered += 1
            return delivered
        finally:
            self._poll_lock.release()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self.poll_once()
                self._stop.wait(self.interval)
        except Exception as exc:
            self.last_error = exc
            self._stop.set()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.running:
                return
            self._check_identity()
            self.last_error = None
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="wechat-export-listener", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout <= 3600:
            raise ValidationError("timeout must be between 0 and 3600 seconds")
        with self._lifecycle_lock:
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                raise LockTimeout("listener callback/source did not stop within timeout")

    def __enter__(self) -> Listener:
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop()
