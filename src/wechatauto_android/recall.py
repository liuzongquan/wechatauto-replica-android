"""Observed-only recall mirror, with optional explicit private SQLite storage.

This is not a device poller and cannot restore previously unseen messages. It
matches only (bound account, exact chat, native server ID). It never guesses from
names, timestamps or local message IDs and never fetches or copies attachments.
Stored records may contain private plaintext: storage is local, not encrypted,
with no automatic expiry. The caller owns retention/deletion after close().
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import IdentityMismatch, ValidationError, WeChatError
from .message_formats import MAX_RECORD_BYTES, native_server_id, parse_message, snapshot_message
from .models import Message


class RecallStoreError(WeChatError):
    """Sanitized storage error; never includes record text, SQL or local paths."""


@dataclass(frozen=True)
class RecallObservation:
    status: str
    account_id: str
    chat_id: str
    server_id: str | None
    source: Message = field(repr=False)
    original: Message | None = field(default=None, repr=False)
    duplicate: bool = False
    reason: str | None = None


def _identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value:
        raise ValidationError(f"invalid {label}")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValidationError(f"invalid {label}") from None
    return value


def _dump(message: Message) -> str:
    return json.dumps(message.to_dict(), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _load(raw: str) -> Message:
    try:
        if not isinstance(raw, str) or len(raw) > MAX_RECORD_BYTES:
            raise RecallStoreError("stored message exceeds size limit")
        return snapshot_message(Message(**json.loads(raw)))
    except (TypeError, ValueError, RecursionError):
        raise RecallStoreError("invalid stored message record") from None


def _private_path(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise ValidationError("recall path must be an explicit local path")
    text = str(value)
    if not text or "\x00" in text or "://" in text or text.startswith("file:") or text == ":memory:":
        raise ValidationError("recall path must be an explicit local path")
    path = Path(value).absolute()
    if ".." in path.parts or path.resolve() != path or path.is_symlink():
        raise ValidationError("recall path must be canonical without symlinks")
    try:
        parent = path.parent.stat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o077
                or (hasattr(os, "getuid") and parent.st_uid != os.getuid())):
            raise ValidationError("recall parent must be an existing owner-private directory")
        # SQLite reopens by name and creates journals: require a non-shared parent
        # and reject preexisting sidecars rather than following arbitrary files.
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                raise ValidationError("unexpected recall database sidecar")
        if path.exists():
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
                    or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
                raise ValidationError("recall database must be an owner-private regular file")
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
    except OSError:
        raise RecallStoreError("cannot access private recall storage") from None
    return path


class RecallGuard:
    """Mirror explicitly delivered Message objects for exactly one account.

    ``path=None`` is in-memory only. An explicit SQLite path requires an existing
    private directory (POSIX 0700, database 0600). A different account cannot reuse
    that file. ``observe``/``on_msg`` do not register listeners or read a device.

    Native identity must be in ``metadata['native_msg_svr_id']``. Revoke events
    use XML ``sysmsg[@type='revokemsg']/revokemsg/newmsgid``; ``msgid`` is NOT a
    substitute. Duplicate events return their first durable outcome with
    ``duplicate=True``. Unknown outcomes never change after late arrival.
    """

    def __init__(self, account_id: str, path: str | Path | None = None):
        self._account_id = _identity(account_id, "account_id")
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        database = str(_private_path(path)) if path is not None else ":memory:"
        try:
            connection = sqlite3.connect(database, timeout=5, check_same_thread=False)
            self._connection = connection
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA secure_delete=ON")
            # Only accept our exact table schema. No trigger/view executes against
            # imported message content; external SQLite databases are not imports.
            tables = connection.execute(
                "SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if tables:
                if set(tables) != {("metadata", "table"), ("messages", "table"), ("events", "table")}:
                    raise RecallStoreError("not a supported recall store")
                self._check_schema(connection)
                records = connection.execute("SELECT key,value FROM metadata LIMIT 3").fetchall()
                if len(records) != 2:
                    raise RecallStoreError("not a supported recall store")
                values = dict(records)
                if values.get("schema_version") != "1" or set(values) != {"schema_version", "account_id"}:
                    raise RecallStoreError("not a supported recall store")
                if values["account_id"] != self.account_id:
                    raise IdentityMismatch("recall store belongs to another account")
            else:
                with connection:
                    connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                    connection.execute("CREATE TABLE messages (account_id TEXT NOT NULL,"
                        "chat_id TEXT NOT NULL,server_id TEXT NOT NULL,message_json TEXT NOT NULL,"
                        "conflicted INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(account_id,chat_id,server_id))")
                    connection.execute("CREATE TABLE events (account_id TEXT NOT NULL,chat_id TEXT NOT NULL,"
                        "event_key TEXT NOT NULL,server_id TEXT,status TEXT NOT NULL,reason TEXT,"
                        "source_json TEXT NOT NULL,original_json TEXT,PRIMARY KEY(account_id,chat_id,event_key))")
                    connection.executemany("INSERT INTO metadata VALUES (?,?)",
                                           [("schema_version", "1"), ("account_id", self.account_id)])
            if connection.execute("PRAGMA journal_mode").fetchone()[0] not in {"delete", "memory"}:
                raise RecallStoreError("unsupported recall journal mode")
        except (sqlite3.Error, OSError):
            self.close()
            raise RecallStoreError("cannot open private recall storage") from None
        except Exception:
            self.close()
            raise

    @staticmethod
    def _check_schema(connection: sqlite3.Connection) -> None:
        expected = {
            "metadata": (("key", "TEXT", 0, 1), ("value", "TEXT", 1, 0)),
            "messages": (("account_id", "TEXT", 1, 1), ("chat_id", "TEXT", 1, 2),
                         ("server_id", "TEXT", 1, 3), ("message_json", "TEXT", 1, 0),
                         ("conflicted", "INTEGER", 1, 0)),
            "events": (("account_id", "TEXT", 1, 1), ("chat_id", "TEXT", 1, 2),
                       ("event_key", "TEXT", 1, 3), ("server_id", "TEXT", 0, 0),
                       ("status", "TEXT", 1, 0), ("reason", "TEXT", 0, 0),
                       ("source_json", "TEXT", 1, 0), ("original_json", "TEXT", 0, 0)),
        }
        for table, columns in expected.items():
            # Table names are fixed internal constants, never supplied identifiers.
            actual = tuple((row[1], row[2], row[3], row[5]) for row in
                           connection.execute(f"PRAGMA table_info({table})"))
            if actual != columns:
                raise RecallStoreError("not a supported recall store")

    @property
    def account_id(self) -> str:
        return self._account_id

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RecallStoreError("recall store is closed")
        return self._connection

    def _result(self, message, status, server_id=None, **extra) -> RecallObservation:
        return RecallObservation(status, self.account_id, message.chat_id, server_id, message, **extra)

    def _event_result(self, row, *, duplicate=False) -> RecallObservation:
        server_id, status, reason, source_json, original_json = row
        source = _load(source_json)
        original = _load(original_json) if original_json is not None else None
        if source.account_id != self.account_id or (original is not None and (
            original.account_id != self.account_id or original.chat_id != source.chat_id
            or native_server_id(original.metadata.get("native_msg_svr_id")) != server_id
        )):
            raise RecallStoreError("stored recall identity mismatch")
        if status not in {"unknown", "recovered"} or (status == "recovered") != (original is not None):
            raise RecallStoreError("invalid stored recall outcome")
        if original is not None:
            parsed = parse_message(source)
            if (parsed.kind != "recall" or parsed.status != "parsed"
                    or parsed.fields.get("server_id") != server_id
                    or parsed.fields.get("session") not in (None, source.chat_id)):
                raise RecallStoreError("stored recall target mismatch")
        return self._result(source, status, server_id, reason=reason,
                            original=original, duplicate=duplicate)

    def _recall(self, connection, message, parsed) -> RecallObservation:
        fields = parsed.fields
        server_id = fields.get("server_id")
        reason = None
        if parsed.status != "parsed" or server_id is None:
            reason = "unparseable_native_recall"
        elif fields.get("session") not in (None, message.chat_id):
            reason = "recall_chat_mismatch"
        # Bad/mismatched envelopes cannot poison a genuine target's dedupe key.
        event_key = ("target:" + server_id if reason is None else
                     "unmatched:" + hashlib.sha256(_dump(message).encode()).hexdigest())
        saved = connection.execute("SELECT server_id,status,reason,source_json,original_json "
            "FROM events WHERE account_id=? AND chat_id=? AND event_key=?",
            (self.account_id, message.chat_id, event_key)).fetchone()
        if saved is not None:
            result = self._event_result(saved, duplicate=True)
            if result.chat_id != message.chat_id or result.server_id != server_id:
                raise RecallStoreError("stored recall target mismatch")
            return result
        original = None
        if reason is None:
            row = connection.execute("SELECT message_json,conflicted FROM messages "
                "WHERE account_id=? AND chat_id=? AND server_id=?",
                (self.account_id, message.chat_id, server_id)).fetchone()
            if row is None:
                reason = "not_previously_observed"
            elif row[1]:
                reason = "conflicting_observed_identity"
            else:
                original = _load(row[0])
                if (original.account_id != self.account_id or original.chat_id != message.chat_id
                        or native_server_id(original.metadata.get("native_msg_svr_id")) != server_id):
                    raise RecallStoreError("stored original identity mismatch")
        status = "recovered" if original is not None else "unknown"
        connection.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
            (self.account_id, message.chat_id, event_key, server_id, status, reason,
             _dump(message), _dump(original) if original else None))
        return self._result(message, status, server_id, original=original, reason=reason)

    def observe(self, message: Message) -> RecallObservation:
        """Observe one record atomically; no message text or paths are logged.

        ``recovered`` means an earlier explicitly supplied record is returned,
        NOT that WeChat was modified or a deleted media file recovered.
        """
        message = snapshot_message(message)
        if message.account_id != self.account_id:
            raise IdentityMismatch("message belongs to another account")
        parsed = parse_message(message)
        with self._lock:
            connection = self._db()
            try:
                with connection:
                    if parsed.kind == "recall" or message.type == "recall":
                        return self._recall(connection, message, parsed)
                    if parsed.kind == "system" or message.type == "system":
                        return self._result(message, "unmatchable", reason="system_notice_not_original")
                    server_id = native_server_id(message.metadata.get("native_msg_svr_id"))
                    if server_id is None:
                        return self._result(message, "unmatchable", reason="missing_native_server_id")
                    if parsed.status in {"unsafe", "invalid", "too_large"}:
                        return self._result(message, "unmatchable", server_id, reason="invalid_original_payload")
                    serialized = _dump(message)
                    row = connection.execute("SELECT message_json,conflicted FROM messages "
                        "WHERE account_id=? AND chat_id=? AND server_id=?",
                        (self.account_id, message.chat_id, server_id)).fetchone()
                    if row:
                        if row[0] != serialized or row[1]:
                            connection.execute("UPDATE messages SET conflicted=1 "
                                "WHERE account_id=? AND chat_id=? AND server_id=?",
                                (self.account_id, message.chat_id, server_id))
                            return self._result(message, "conflict", server_id,
                                                reason="conflicting_observed_identity")
                        return replace(self._result(message, "observed", server_id), duplicate=True)
                    if connection.execute("SELECT 1 FROM events WHERE account_id=? AND chat_id=? "
                        "AND event_key=?", (self.account_id, message.chat_id, "target:" + server_id)).fetchone():
                        return self._result(message, "unmatchable", server_id,
                                            reason="observed_after_recall")
                    connection.execute("INSERT INTO messages VALUES (?,?,?,?,0)",
                                       (self.account_id, message.chat_id, server_id, serialized))
                    return self._result(message, "observed", server_id)
            except sqlite3.Error:
                raise RecallStoreError("recall storage operation failed") from None

    def on_msg(self, message: Message, listener: Any = None) -> RecallObservation:
        """Listener-compatible callback, with optional listener account verification."""
        if listener is not None and getattr(listener, "account_id", None) != self.account_id:
            raise IdentityMismatch("listener belongs to another account or lacks identity")
        return self.observe(message)

    def events(self, limit: int = 100) -> list[RecallObservation]:
        """Read stored recall outcomes in observation order; no automatic callback."""
        if type(limit) is not int or not 0 <= limit <= 10000:
            raise ValidationError("event limit must be between 0 and 10000")
        with self._lock:
            try:
                rows = self._db().execute("SELECT server_id,status,reason,source_json,original_json "
                    "FROM events WHERE account_id=? ORDER BY rowid LIMIT ?",
                    (self.account_id, limit))
                results = []
                characters = 0
                for row in rows:
                    characters += len(row[3]) + len(row[4] or "")
                    if characters > 16 * MAX_RECORD_BYTES:
                        raise RecallStoreError("recall event batch exceeds size limit")
                    results.append(self._event_result(row))
                return results
            except sqlite3.Error:
                raise RecallStoreError("cannot read recall events") from None

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                connection, self._connection = self._connection, None
                connection.close()

    def __enter__(self) -> RecallGuard:
        self._db()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
