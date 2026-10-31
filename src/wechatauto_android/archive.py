"""用户提供的规范化导出档案；不读取或解密 Android 微信数据库。"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .errors import AmbiguousTarget, IdentityMismatch, ValidationError
from .models import Message

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MESSAGES = 500_000


def _string(value: Any, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not empty) or "\x00" in value:
        raise ValidationError(f"{name} must be a {'possibly empty ' if empty else ''}string without NUL")
    return value


def _number(value: Any, name: str, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValidationError(f"{name} must be an integer between 0 and {maximum}")
    return value


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path, max_bytes: int = MAX_ARCHIVE_BYTES) -> dict[str, Any]:
    """限制大小，不跟随文件符号链接，拒绝重复键和非有限数字。"""
    if path.is_symlink() or path.absolute().parent.resolve() != path.absolute().parent:
        raise ValidationError("symlink input is not allowed")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValidationError("JSON input must be a regular file")
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValidationError("JSON input exceeds size limit")
        value = json.loads(raw, object_pairs_hook=_json_pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValidationError("non-finite JSON number")))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError(f"cannot read JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError("JSON root must be an object")
    return value


def atomic_json(path: Path, data: dict[str, Any], *, overwrite: bool = True) -> None:
    """同目录原子替换，权限 0600；不跟随目标或目录中的符号链接。"""
    path = Path(path).absolute()
    if path.parent.resolve() != path.parent or path.is_symlink():
        raise ValidationError("symlink output path is not allowed")
    if not path.parent.is_dir():
        raise ValidationError("output parent directory must already exist")
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path, follow_symlinks=False)
            os.unlink(temporary)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class ExportArchive:
    """一个账号、一个导出数据源的内存快照。读取不会访问设备。"""

    def __init__(self, data: dict[str, Any], account_id: str, *, source_id: str | None = None) -> None:
        self.account_id = _string(account_id, "account_id")
        self._path: Path | None = None
        self._load(data, source_id)

    @classmethod
    def open(cls, path: str | Path, account_id: str) -> ExportArchive:
        path = Path(path).absolute()
        result = cls(read_json(path), account_id,
                     source_id="file:" + hashlib.sha256(str(path).encode()).hexdigest())
        result._path = path
        return result

    def _load(self, data: dict[str, Any], source_id: str | None) -> None:
        if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ValidationError("expected normalized export schema_version=1")
        if data.get("account_id") != self.account_id:
            raise IdentityMismatch("export account_id does not match requested account")
        allowed = {"schema_version", "account_id", "export_id", "messages", "contacts", "groups", "labels", "moments", "origin"}
        if set(data) - allowed:
            raise ValidationError(f"unknown export fields: {sorted(set(data) - allowed)}")
        if data.get("origin", "user_export") != "user_export":
            raise ValidationError("normalized archives must declare user_export origin")
        export_id = data.get("export_id")
        if export_id is not None:
            _string(export_id, "export_id")
        identity = source_id or export_id
        if source_id is not None and export_id is not None:
            identity = source_id + ":" + hashlib.sha256(export_id.encode()).hexdigest()
        self.source_id = _string(identity, "export_id/source_id")
        try:
            json.dumps(data, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValidationError("archive must contain JSON-compatible finite values") from exc
        rows = data.get("messages")
        if not isinstance(rows, list) or len(rows) > MAX_MESSAGES:
            raise ValidationError("messages must be a bounded array")
        messages: dict[tuple[str, str], Message] = {}
        for row in rows:
            message = self._message(row)
            key = (message.chat_id, message.id)
            if key in messages and messages[key] != message:
                raise ValidationError("conflicting duplicate message identity")
            messages[key] = message
        by_chat: dict[str, list[Message]] = defaultdict(list)
        for message in messages.values():
            by_chat[message.chat_id].append(message)
        self._messages = messages
        self._by_chat = {chat: tuple(sorted(items, key=lambda item: (item.sequence, item.id)))
                         for chat, items in by_chat.items()}
        self._data = copy.deepcopy(data)
        self._data["origin"] = "user_export"
        self._data["messages"] = [item.to_dict() for item in messages.values()]
        for collection in ("contacts", "groups", "labels", "moments"):
            records = self._data.get(collection, [])
            if not isinstance(records, list) or len(records) > MAX_MESSAGES:
                raise ValidationError(f"{collection} must be a bounded array")
            identities: set[str] = set()
            for record in records:
                if not isinstance(record, dict):
                    raise ValidationError(f"invalid {collection} record")
                if record.get("account_id", self.account_id) != self.account_id:
                    raise IdentityMismatch(f"cross-account {collection} record")
                if record.get("origin", "user_export") != "user_export":
                    raise ValidationError("collection records cannot impersonate device observations")
                record["account_id"] = self.account_id
                record["origin"] = "user_export"
                identity_field = {"contacts": "id", "groups": "id", "labels": "id", "moments": "id"}[collection]
                identity = _string(record.get(identity_field), f"{collection}.id")
                if identity in identities:
                    raise ValidationError(f"duplicate {collection} identity")
                identities.add(identity)
                for name in ("name", "nickname", "remark", "content"):
                    if name in record:
                        _string(record[name], f"{collection}.{name}", empty=True)
                if collection in {"groups", "labels"}:
                    members = record.get("members", [])
                    if not isinstance(members, list):
                        raise ValidationError(f"{collection}.members must be an array")
                    seen: set[str] = set()
                    for member in members:
                        member_id = member.get("id") if isinstance(member, dict) else member
                        member_id = _string(member_id, "member.id")
                        if member_id in seen:
                            raise ValidationError("duplicate group/label member")
                        seen.add(member_id)
                        if isinstance(member, dict):
                            if member.get("account_id", self.account_id) != self.account_id:
                                raise IdentityMismatch("cross-account member")
                            if member.get("origin", "user_export") != "user_export":
                                raise ValidationError("member cannot impersonate device observation")
                            member["account_id"] = self.account_id
                            member["origin"] = "user_export"

    def _message(self, row: Any) -> Message:
        if not isinstance(row, dict):
            raise ValidationError("message must be an object")
        allowed = {"id", "account_id", "chat_id", "sequence", "sender_id", "content", "type", "timestamp", "origin", "metadata"}
        if set(row) - allowed:
            raise ValidationError("unknown message field")
        if row.get("account_id", self.account_id) != self.account_id:
            raise IdentityMismatch("cross-account message")
        if row.get("origin", "user_export") != "user_export":
            raise ValidationError("export messages cannot impersonate device observations")
        metadata = row.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValidationError("message metadata must be an object")
        timestamp = row.get("timestamp")
        if timestamp is not None:
            _string(timestamp, "timestamp")
        return Message(id=_string(row.get("id"), "id"), account_id=self.account_id,
                       chat_id=_string(row.get("chat_id"), "chat_id"),
                       sequence=_number(row.get("sequence"), "sequence", 2**63 - 1),
                       sender_id=_string(row.get("sender_id"), "sender_id"),
                       content=_string(row.get("content"), "content", empty=True),
                       type=_string(row.get("type", "text"), "type"), timestamp=timestamp,
                       origin="user_export", metadata=copy.deepcopy(metadata))

    def refresh(self) -> None:
        """刷新外部原子替换的导出文件；消息必须仅追加，禁止悄悄丢失游标前插入。"""
        if self._path is None:
            return
        replacement = ExportArchive.open(self._path, self.account_id)
        if replacement.source_id != self.source_id:
            raise IdentityMismatch("export source changed")
        for key, message in self._messages.items():
            if replacement._messages.get(key) != message:
                raise ValidationError("archive refresh must not remove or change existing messages")
        for chat, items in replacement._by_chat.items():
            old = self._by_chat.get(chat, ())
            if old:
                last = (old[-1].sequence, old[-1].id)
                for message in items:
                    if (chat, message.id) not in self._messages and (message.sequence, message.id) <= last:
                        raise ValidationError("late message insertion before cursor is not supported")
        self.__dict__.update(replacement.__dict__)

    def get_messages(self, chat_id: str, limit: int = 20, offset: int = 0) -> list[Message]:
        _string(chat_id, "chat_id")
        _number(limit, "limit")
        _number(offset, "offset")
        return copy.deepcopy(list(reversed(self._by_chat.get(chat_id, ())))[offset:offset + limit])

    def get_messages_after(self, chat_id: str, after: tuple[int, str] | None = None,
                           limit: int = 200) -> list[Message]:
        _string(chat_id, "chat_id")
        _number(limit, "limit")
        if after is not None:
            if not isinstance(after, tuple) or len(after) != 2:
                raise ValidationError("cursor must be (sequence, id)")
            _number(after[0], "cursor sequence", 2**63 - 1)
            _string(after[1], "cursor id")
        items = self._by_chat.get(chat_id, ())
        return copy.deepcopy([item for item in items if after is None or (item.sequence, item.id) > after][:limit])

    def get_new_messages(self, chat_id: str, since_seq: int = 0, limit: int = 200) -> list[Message]:
        _number(since_seq, "since_seq", 2**63 - 1)
        _number(limit, "limit")
        _string(chat_id, "chat_id")
        return copy.deepcopy([item for item in self._by_chat.get(chat_id, ()) if item.sequence > since_seq][:limit])

    def get_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        _number(limit, "limit")
        rows = [{"id": chat, "chat_id": chat, "account_id": self.account_id,
                 "message_count": len(items), "last_sequence": items[-1].sequence, "origin": "user_export"}
                for chat, items in self._by_chat.items()]
        return sorted(rows, key=lambda row: (-row["last_sequence"], row["chat_id"]))[:limit]

    def search_contact(self, keyword: str) -> list[dict[str, Any]]:
        _string(keyword, "keyword")
        return copy.deepcopy([row for row in self._data.get("contacts", [])
                              if any(keyword.casefold() in str(row.get(key, "")).casefold()
                                     for key in ("id", "name", "nickname", "remark"))])

    def get_nickname(self, user: str) -> str | None:
        _string(user, "user")
        record = next((row for row in self._data.get("contacts", []) if row["id"] == user), None)
        if record is None:
            return None
        return record.get("remark") or record.get("nickname") or record.get("name") or record["id"]

    def username_by_nickname(self, nickname: str) -> str | None:
        _string(nickname, "nickname")
        matches = [row["id"] for row in self._data.get("contacts", [])
                   if nickname in {row.get("nickname"), row.get("remark"), row.get("name")}]
        if len(matches) > 1:
            raise AmbiguousTarget("multiple exported contacts share that exact nickname/remark")
        return matches[0] if matches else None

    def nickname_map(self) -> dict[str, str]:
        return {row["id"]: self.get_nickname(row["id"]) for row in self._data.get("contacts", [])}

    def group_name_to_id(self, name: str) -> str | None:
        _string(name, "name")
        matches = [row["id"] for row in self._data.get("groups", []) if row.get("name") == name]
        if len(matches) > 1:
            raise AmbiguousTarget("multiple exported groups share that exact name")
        return matches[0] if matches else None

    def group_id_to_name(self, chat_id: str) -> str | None:
        _string(chat_id, "chat_id")
        record = next((row for row in self._data.get("groups", []) if row["id"] == chat_id), None)
        return record.get("name") if record else None

    def label_names(self) -> list[str]:
        return [row["name"] for row in self._data.get("labels", []) if "name" in row]

    def label_members(self, label_id: str) -> list[dict[str, Any]]:
        _string(label_id, "label_id")
        record = next((row for row in self._data.get("labels", []) if row["id"] == label_id), None)
        if record is None:
            return []
        contacts = {row["id"]: row for row in self._data.get("contacts", [])}
        result = []
        for member in record.get("members", []):
            member_id = member["id"] if isinstance(member, dict) else member
            result.append(copy.deepcopy(contacts.get(member_id, {"id": member_id,
                                                                 "account_id": self.account_id,
                                                                 "origin": "user_export"})))
        return result

    def get_groups(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._data.get("groups", []))

    def get_group_members(self, chat_id: str) -> list[dict[str, Any]]:
        _string(chat_id, "chat_id")
        group = next((row for row in self._data.get("groups", []) if row["id"] == chat_id), None)
        if group is None:
            return []
        return copy.deepcopy([member if isinstance(member, dict) else {"id": member, "account_id": self.account_id, "origin": "user_export"}
                              for member in group.get("members", [])])

    def list_labels(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._data.get("labels", []))

    def get_moments(self, limit: int = 100) -> list[dict[str, Any]]:
        _number(limit, "limit")
        return copy.deepcopy(self._data.get("moments", [])[:limit])

    def get_message_by_id(self, chat_id: str, message_id: str) -> Message | None:
        _string(chat_id, "chat_id")
        _string(message_id, "message_id")
        return copy.deepcopy(self._messages.get((chat_id, message_id)))

    def export_history(self, out_path: str | Path, fmt: str = "json", users: Iterable[str] | None = None,
                       limit_per_chat: int | None = None) -> Path:
        """仅重新导出当前用户档案；不读取手机历史。"""
        path = Path(out_path).absolute()
        if self._path is not None and path == self._path:
            raise ValidationError("refusing to overwrite source archive")
        if path.exists() or path.is_symlink():
            raise ValidationError("export destination already exists")
        if path.parent.resolve() != path.parent or not path.parent.is_dir():
            raise ValidationError("export parent must exist and not contain symlinks")
        if users is not None and isinstance(users, (str, bytes)):
            raise ValidationError("users must be a collection, not a string")
        chats = sorted(self._by_chat) if users is None else sorted({_string(user, "user") for user in users})
        if limit_per_chat is not None:
            _number(limit_per_chat, "limit_per_chat")
        selected = []
        for chat in chats:
            items = list(self._by_chat.get(chat, ()))
            selected.extend(items if limit_per_chat is None else items[-limit_per_chat:] if limit_per_chat else [])
        if fmt == "json":
            data = {"schema_version": 1, "account_id": self.account_id,
                    "export_id": self.source_id + ":history", "origin": "user_export",
                    "messages": [item.to_dict() for item in selected]}
            atomic_json(path, data, overwrite=False)
        elif fmt == "sqlite":
            fd, temporary = tempfile.mkstemp(prefix=".archive-", dir=path.parent)
            os.close(fd)
            try:
                with sqlite3.connect(temporary) as connection:
                    connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    connection.executemany("INSERT INTO metadata VALUES (?, ?)",
                                           [("schema_version", "1"), ("account_id", self.account_id),
                                            ("source_id", self.source_id), ("origin", "user_export")])
                    connection.execute("CREATE TABLE messages (account_id TEXT NOT NULL, chat_id TEXT NOT NULL, id TEXT NOT NULL, sequence INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(account_id, chat_id, id))")
                    connection.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
                                           [(item.account_id, item.chat_id, item.id, item.sequence,
                                             json.dumps(item.to_dict(), ensure_ascii=False, allow_nan=False)) for item in selected])
                os.link(temporary, path, follow_symlinks=False)
                os.unlink(temporary)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        else:
            raise ValidationError("fmt must be json or sqlite")
        return path


class GroupMemberWatcher:
    """仅比较用户提供档案的群成员快照，不监听手机数据库。"""

    def __init__(self, source: ExportArchive, chat_id: str) -> None:
        self.source = source
        self.chat_id = _string(chat_id, "chat_id")
        self._baseline: set[str] | None = None

    def capture(self) -> set[str]:
        self._baseline = {row["id"] for row in self.source.get_group_members(self.chat_id)}
        return set(self._baseline)

    def members(self) -> list[dict[str, Any]]:
        return self.source.get_group_members(self.chat_id)

    def poll(self) -> dict[str, list[str]]:
        self.source.refresh()
        current = {row["id"] for row in self.members()}
        before = self._baseline if self._baseline is not None else current
        self._baseline = current
        return {"added": sorted(current - before), "removed": sorted(before - current)}


class MomentObserver:
    """比较用户导出朋友圈快照；不触发手机刷新、点赞或评论。"""

    def __init__(self, source: ExportArchive) -> None:
        self.source = source
        self._baseline: dict[str, dict[str, Any]] | None = None

    def capture(self) -> list[dict[str, Any]]:
        rows = self.source.get_moments(limit=MAX_MESSAGES)
        self._baseline = {row["id"]: row for row in rows}
        return copy.deepcopy(rows)

    def poll(self) -> dict[str, list[dict[str, Any]]]:
        self.source.refresh()
        current = {row["id"]: row for row in self.source.get_moments(limit=MAX_MESSAGES)}
        before = self._baseline if self._baseline is not None else current
        result = {
            "added": [current[key] for key in sorted(current.keys() - before.keys())],
            "removed": [before[key] for key in sorted(before.keys() - current.keys())],
            "changed": [current[key] for key in sorted(current.keys() & before.keys()) if current[key] != before[key]],
        }
        self._baseline = current
        return copy.deepcopy(result)
