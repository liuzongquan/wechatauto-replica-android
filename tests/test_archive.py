"""Synthetic normalized-export tests; never Android DB extraction tests."""
import json
import os
import sqlite3

import pytest

from wechatauto_android.archive import ExportArchive, GroupMemberWatcher
from wechatauto_android.errors import IdentityMismatch, ValidationError


def row(id="m1", sequence=1, chat="chat-a", **extra):
    return {"id": id, "sequence": sequence, "chat_id": chat, "sender_id": "sender", "content": "你好🙂\nline", **extra}


def data(messages=None, **extra):
    return {"schema_version": 1, "account_id": "account-a", "export_id": "synthetic", "messages": messages if messages is not None else [row()], **extra}


def archive(messages=None, **extra):
    return ExportArchive(data(messages, **extra), "account-a")


def test_read_order_ties_and_isolation():
    store = archive([row("c", 2), row("a", 1), row("b", 1), row("a", 9, "chat-b")])
    assert [m.id for m in store.get_messages("chat-a")] == ["c", "b", "a"]
    assert [m.id for m in store.get_messages("chat-a", limit=1, offset=1)] == ["b"]
    assert [m.id for m in store.get_messages_after("chat-a", (1, "a"))] == ["b", "c"]
    assert [m.id for m in store.get_new_messages("chat-a", since_seq=1)] == ["c"]
    assert store.get_message_by_id("chat-b", "a").sequence == 9
    assert store.get_message_by_id("missing", "a") is None
    assert store.get_messages("missing") == []
    assert store.get_messages("chat-a", limit=0) == []


def test_provenance_and_copies():
    original = data([row(metadata={"media_path": "a.png"})])
    store = ExportArchive(original, "account-a")
    original["messages"][0]["content"] = "modified"
    item = store.get_messages("chat-a")[0]
    item.metadata["media_path"] = "changed"
    assert store.get_messages("chat-a")[0].metadata["media_path"] == "a.png"
    assert item.origin == "user_export"
    assert item.content == "你好🙂\nline"


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"schema_version": 2}, {"messages": {}}, {"origin": "device"},
    {"unknown": 1}, {"export_id": 0}, {"messages": [row(sequence=True)]},
    {"messages": [row(sequence=-1)]}, {"messages": [row(sequence=2**63)]},
    {"messages": [row(content=None)]}, {"messages": [row(id="")]},
    {"messages": [row(origin="android_ui")]}, {"messages": [row(metadata=[])]},
    {"messages": [row(unknown=True)]}, {"messages": [row(timestamp=False)]},
    {"messages": [row(metadata={"n": float("nan")})]},
])
def test_invalid_schema(change):
    value = data()
    value.update(change)
    with pytest.raises(ValidationError):
        ExportArchive(value, "account-a")


@pytest.mark.parametrize("changes", [
    {"account_id": "other"}, {"messages": [row(account_id="other")]},
    {"contacts": [{"id": "x", "account_id": "other"}]},
    {"groups": [{"id": "g", "members": [{"id": "x", "account_id": "other"}]}]},
])
def test_cross_account_rejected(changes):
    value = data()
    value.update(changes)
    with pytest.raises(IdentityMismatch):
        ExportArchive(value, "account-a")


def test_duplicates():
    assert len(archive([row(), row()]).get_messages("chat-a")) == 1
    with pytest.raises(ValidationError, match="conflicting"):
        archive([row(), row(content="changed")])


@pytest.mark.parametrize("collection", ["contacts", "groups", "labels", "moments"])
def test_collection_provenance_cannot_be_forged(collection):
    with pytest.raises(ValidationError):
        archive(**{collection: [{"id": "x", "origin": "device_verified"}]})
    with pytest.raises(ValidationError):
        archive(**{collection: [{"id": "x"}, {"id": "x"}]})


def test_collection_queries_and_watcher():
    store = archive(contacts=[{"id": "wxid_a", "nickname": "Alice", "remark": "朋友"}],
                    groups=[{"id": "g@chatroom", "members": ["wxid_a", {"id": "wxid_b"}]}],
                    labels=[{"id": "l", "name": "friends", "members": ["wxid_a"]}],
                    moments=[{"id": "post", "content": "周末"}])
    assert store.search_contact("ali")[0]["id"] == "wxid_a"
    assert store.search_contact("朋友")[0]["origin"] == "user_export"
    assert store.get_groups()[0]["id"] == "g@chatroom"
    assert len(store.get_group_members("g@chatroom")) == 2
    assert store.get_group_members("missing") == []
    assert store.list_labels()[0]["name"] == "friends"
    assert store.get_moments()[0]["origin"] == "user_export"
    assert store.get_sessions()[0]["origin"] == "user_export"
    watcher = GroupMemberWatcher(store, "g@chatroom")
    assert watcher.capture() == {"wxid_a", "wxid_b"}
    assert watcher.poll() == {"added": [], "removed": []}


@pytest.mark.parametrize("member", [{"id": "x", "origin": "device"}, {"id": ""}, None])
def test_invalid_members(member):
    with pytest.raises(ValidationError):
        archive(groups=[{"id": "g", "members": [member]}])


def test_json_and_sqlite_exports(tmp_path):
    store = archive([row("a", 1), row("b", 2), row("a", 3, "chat-b")])
    result = store.export_history(tmp_path / "output.json", users=["chat-a"], limit_per_chat=1)
    exported = ExportArchive.open(result, "account-a")
    assert [m.id for m in exported.get_messages("chat-a")] == ["b"]
    assert exported.get_messages("chat-b") == []
    sql = store.export_history(tmp_path / "output.sqlite", fmt="sqlite")
    with sqlite3.connect(sql) as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 3
        assert connection.execute("SELECT value FROM metadata WHERE key='origin'").fetchone()[0] == "user_export"
    assert os.stat(result).st_mode & 0o777 == 0o600
    with pytest.raises(ValidationError):
        store.export_history(result)
    with pytest.raises(ValidationError):
        store.export_history(tmp_path / "other.json", users="chat-a")
    with pytest.raises(ValidationError):
        store.export_history(tmp_path / "other.json", fmt="wat")


def test_json_input_and_symlinks(tmp_path):
    target = tmp_path / "input.json"
    target.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(ValidationError, match="duplicate"):
        ExportArchive.open(target, "account-a")
    target.write_text(json.dumps(data()))
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(ValidationError):
        ExportArchive.open(link, "account-a")
    symlink_dir = tmp_path / "linkdir"
    symlink_dir.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValidationError):
        ExportArchive.open(symlink_dir / "input.json", "account-a")
    with pytest.raises(ValidationError):
        archive().export_history(symlink_dir / "output.json")


def test_refresh_append_only_and_source_identity(tmp_path):
    path = tmp_path / "input.json"
    value = data([row("a", 1)])
    path.write_text(json.dumps(value))
    store = ExportArchive.open(path, "account-a")
    value["messages"].append(row("b", 1))
    path.write_text(json.dumps(value))
    store.refresh()
    assert len(store.get_messages("chat-a")) == 2
    value["messages"].append(row("0", 1))
    path.write_text(json.dumps(value))
    with pytest.raises(ValidationError, match="late"):
        store.refresh()
    assert len(store.get_messages("chat-a")) == 2
    value["messages"] = [row("b", 1)]
    path.write_text(json.dumps(value))
    with pytest.raises(ValidationError, match="remove"):
        store.refresh()
    value["export_id"] = "new-source"
    path.write_text(json.dumps(value))
    with pytest.raises(IdentityMismatch):
        store.refresh()
    with pytest.raises(ValidationError):
        store.export_history(path)


@pytest.mark.parametrize("method,args,kwargs", [
    ("get_messages", ("chat-a",), {"limit": -1}), ("get_messages", ("chat-a",), {"offset": True}),
    ("get_messages_after", ("chat-a",), {"after": (1,)}), ("get_new_messages", ("chat-a",), {"since_seq": -1}),
    ("get_sessions", (), {"limit": -1}), ("get_moments", (), {"limit": True}),
    ("search_contact", ("",), {}), ("get_messages_after", ("chat-a",), {"limit": -1}),
])
def test_query_validation(method, args, kwargs):
    with pytest.raises(ValidationError):
        getattr(archive(), method)(*args, **kwargs)


def test_group_and_moments_snapshot_diff(tmp_path):
    from wechatauto_android.archive import MomentObserver
    path = tmp_path / "snapshots.json"
    value = data(groups=[{"id": "group", "members": ["a", "b"]}],
                 moments=[{"id": "p1", "content": "old"}, {"id": "p2", "content": "remove"}])
    path.write_text(json.dumps(value))
    store = ExportArchive.open(path, "account-a")
    groups = GroupMemberWatcher(store, "group")
    moments = MomentObserver(store)
    groups.capture()
    assert len(moments.capture()) == 2
    value["groups"][0]["members"] = ["b", "c"]
    value["moments"] = [{"id": "p1", "content": "updated"}, {"id": "p3", "content": "added"}]
    path.write_text(json.dumps(value))
    assert groups.poll() == {"added": ["c"], "removed": ["a"]}
    delta = moments.poll()
    assert [item["id"] for item in delta["added"]] == ["p3"]
    assert [item["id"] for item in delta["removed"]] == ["p2"]
    assert [item["id"] for item in delta["changed"]] == ["p1"]
    assert moments.poll() == {"added": [], "removed": [], "changed": []}


def test_exact_name_resolution_and_ambiguity():
    from wechatauto_android.errors import AmbiguousTarget
    store = archive(contacts=[{"id": "a", "nickname": "same", "remark": "first"},
                              {"id": "b", "nickname": "same", "name": "second"}],
                    groups=[{"id": "g1", "name": "same group"}, {"id": "g2", "name": "same group"}],
                    labels=[{"id": "label", "name": "work", "members": ["a", "unknown"]}])
    assert store.get_nickname("a") == "first"
    assert store.get_nickname("absent") is None
    assert store.nickname_map() == {"a": "first", "b": "same"}
    assert store.username_by_nickname("first") == "a"
    assert store.username_by_nickname("missing") is None
    with pytest.raises(AmbiguousTarget):
        store.username_by_nickname("same")
    with pytest.raises(AmbiguousTarget):
        store.group_name_to_id("same group")
    assert store.group_name_to_id("missing") is None
    assert store.group_id_to_name("g1") == "same group"
    assert store.group_id_to_name("missing") is None
    assert store.label_names() == ["work"]
    assert [item["id"] for item in store.label_members("label")] == ["a", "unknown"]
    assert store.label_members("missing") == []
