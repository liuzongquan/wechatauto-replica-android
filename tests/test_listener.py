"""Offline polling semantics, never live WeChat listening."""
import json
import threading
from dataclasses import replace

import pytest

from wechatauto_android.archive import ExportArchive
from wechatauto_android.errors import IdentityMismatch, LockTimeout, UnsupportedFeature, ValidationError
from wechatauto_android.listener import Listener


def source(messages=4):
    return ExportArchive({"schema_version": 1, "account_id": "a", "export_id": "fixture",
                          "messages": [{"id": str(index), "chat_id": "c", "sequence": index // 3,
                                        "sender_id": "friend", "content": "hello"} for index in range(messages)]}, "a")


def test_tied_sequences_cross_page_and_checkpoint(tmp_path):
    store = source(7)
    state = tmp_path / "state.json"
    seen = []
    listener = Listener(store, "a", watermark_file=state, batch_size=2)
    def callback(message, _):
        seen.append(message.id)
    listener.add_listener("c", callback)
    listener.add_listener("c", callback)
    assert listener.poll_once() == 2
    assert listener.poll_once() == 2
    assert listener.poll_once() == 2
    assert listener.poll_once() == 1
    assert listener.poll_once() == 0
    assert seen == [str(index) for index in range(7)]
    assert listener.watermark == {"c": (2, "6")}
    restored = Listener(store, "a", watermark_file=state, batch_size=2)
    restored.add_listener("c", callback)
    assert restored.poll_once() == 0
    assert state.stat().st_mode & 0o777 == 0o600
    listener.remove_listener("c", callback)
    listener.remove_listener("c")
    assert listener.poll_once() == 0


def test_failure_does_not_advance_and_may_repeat_successful_callback():
    listener = Listener(source(1), "a")
    seen = []
    fail = [True]
    listener.add_listener("c", lambda message, _: seen.append(message.id))
    def callback(message, owner):
        if fail[0]:
            raise RuntimeError("temporary failure")
    listener.add_listener("c", callback)
    with pytest.raises(RuntimeError):
        listener.poll_once()
    assert listener.watermark == {}
    fail[0] = False
    assert listener.poll_once() == 1
    assert seen == ["0", "0"]


def test_failed_persistence_rolls_back_memory_cursor(tmp_path, monkeypatch):
    listener = Listener(source(1), "a", watermark_file=tmp_path / "checkpoint.json")
    listener.add_listener("c", lambda *_: None)
    monkeypatch.setattr(listener, "save_watermark", lambda: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        listener.poll_once()
    assert listener.watermark == {}


@pytest.mark.parametrize("kind", ["account", "source"])
def test_checkpoint_scope(tmp_path, kind):
    state = tmp_path / "state.json"
    data = {"schema_version": 1, "account_id": "a", "source_id": "fixture", "cursors": {}}
    data["account_id" if kind == "account" else "source_id"] = "different"
    state.write_text(json.dumps(data))
    with pytest.raises(IdentityMismatch):
        Listener(source(), "a", watermark_file=state)


@pytest.mark.parametrize("cursor", [None, [True, "x"], [-1, "x"], [0, ""], [1], [1, "x", 3]])
def test_bad_checkpoint(tmp_path, cursor):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"schema_version": 1, "account_id": "a", "source_id": "fixture", "cursors": {"c": cursor}}))
    with pytest.raises(ValidationError):
        Listener(source(), "a", watermark_file=state)


def test_constructor_validation():
    with pytest.raises(IdentityMismatch):
        Listener(source(), "wrong")
    class SequenceOnly:
        account_id = "a"
        source_id = "s"
        def get_new_messages(self, *args, **kwargs):
            return []
    with pytest.raises(UnsupportedFeature):
        Listener(SequenceOnly(), "a")
    for kwargs in ({"interval": 0}, {"interval": float("nan")}, {"batch_size": 0}, {"batch_size": True}):
        with pytest.raises(ValidationError):
            Listener(source(), "a", **kwargs)
    with pytest.raises(ValidationError):
        Listener(source(), "a").add_listener("c", None)


@pytest.mark.parametrize("change", ["account", "chat", "order", "duplicate", "oversize", "nonmessage"])
def test_bad_source_batch_fails_before_callbacks(change):
    store = source(2)
    rows = store.get_messages_after("c")
    if change == "account":
        rows[-1] = replace(rows[-1], account_id="other")
    elif change == "chat":
        rows[-1] = replace(rows[-1], chat_id="other")
    elif change == "order":
        rows.reverse()
    elif change == "duplicate":
        rows[-1] = rows[0]
    elif change == "oversize":
        rows *= 3
    else:
        rows[-1] = {}
    store.get_messages_after = lambda *args, **kwargs: rows
    listener = Listener(store, "a", batch_size=2)
    seen = []
    listener.add_listener("c", lambda *args: seen.append(args))
    with pytest.raises((ValidationError, IdentityMismatch)):
        listener.poll_once()
    assert seen == []
    assert listener.watermark == {}


def test_identity_change_before_and_after_read():
    store = source(1)
    listener = Listener(store, "a")
    listener.add_listener("c", lambda *_: None)
    store.account_id = "other"
    with pytest.raises(IdentityMismatch):
        listener.poll_once()
    store.account_id = "a"
    read = store.get_messages_after
    def mutate(*args, **kwargs):
        rows = read(*args, **kwargs)
        store.source_id = "other"
        return rows
    store.get_messages_after = mutate
    with pytest.raises(IdentityMismatch):
        listener.poll_once()


def test_concurrent_poll_and_bounded_stop():
    listener = Listener(source(1), "a", interval=0.01)
    entered = threading.Event()
    release = threading.Event()
    def callback(*_):
        entered.set()
        release.wait(3)
    listener.add_listener("c", callback)
    listener.start()
    listener.start()
    assert entered.wait(2)
    with pytest.raises(LockTimeout):
        listener.poll_once()
    with pytest.raises(LockTimeout):
        listener.stop(timeout=0)
    release.set()
    listener.stop(timeout=2)
    assert not listener.running
    assert listener.last_error is None


def test_thread_stops_and_surfaces_error():
    listener = Listener(source(1), "a", interval=0.01)
    entered = threading.Event()
    def callback(*_):
        entered.set()
        raise RuntimeError("callback failed")
    listener.add_listener("c", callback)
    listener.start()
    assert entered.wait(2)
    listener.stop(timeout=2)
    assert isinstance(listener.last_error, RuntimeError)
    assert listener.watermark == {}


def test_callback_can_stop_self_and_mutation_copy():
    listener = Listener(source(1), "a", interval=0.01)
    entered = threading.Event()
    def callback(message, owner):
        message.metadata["change"] = "should not affect source"
        owner.stop()
        entered.set()
    listener.add_listener("c", callback)
    with listener:
        assert entered.wait(2)
    assert listener.source.get_messages("c")[0].metadata == {}
    with pytest.raises(ValidationError):
        listener.stop(timeout=-1)
