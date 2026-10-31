"""Local exported-file security tests; no device/media extraction."""
import hashlib
import os

import pytest

from wechatauto_android.errors import IdentityMismatch, ValidationError
from wechatauto_android.media import MediaDownloader
from wechatauto_android.models import Message


@pytest.fixture
def roots(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    (source / "photo.png").write_bytes(b"synthetic file bytes")
    return source, output


def test_copy_and_integrity(roots):
    source, output = roots
    downloader = MediaDownloader(source, output)
    digest = hashlib.sha256((source / "photo.png").read_bytes()).hexdigest()
    result = downloader.download("photo.png", destination="nested/结果.png", sha256=digest)
    assert result.read_bytes() == b"synthetic file bytes"
    assert result.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValidationError):
        downloader.download("photo.png", destination="nested/结果.png")


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "a/../../x", "a//b", "./a", "a/./b", "", "x\x00", "C:\\x", "a\\b", "a:", "a/"])
def test_path_rejection(roots, path):
    source, output = roots
    downloader = MediaDownloader(source, output)
    with pytest.raises(ValidationError):
        downloader.download(path)
    with pytest.raises(ValidationError):
        downloader.download("photo.png", destination=path)
    assert list(output.iterdir()) == []


def test_source_symlink_and_parent_escape(roots, tmp_path):
    source, output = roots
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("private")
    (source / "link").symlink_to(outside / "secret")
    (source / "directory").symlink_to(outside, target_is_directory=True)
    downloader = MediaDownloader(source, output)
    for path in ("link", "directory/secret"):
        with pytest.raises(ValidationError):
            downloader.download(path)
    assert list(output.iterdir()) == []


def test_output_symlink_and_parent_escape(roots, tmp_path):
    source, output = roots
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_text("unchanged")
    (output / "link").symlink_to(secret)
    (output / "directory").symlink_to(outside, target_is_directory=True)
    downloader = MediaDownloader(source, output)
    for path in ("link", "directory/secret"):
        with pytest.raises(ValidationError):
            downloader.download("photo.png", destination=path)
    assert secret.read_text() == "unchanged"
    assert not list(output.glob(".wechat-media-*"))


def test_root_symlink_change_is_refused(roots, tmp_path):
    source, output = roots
    downloader = MediaDownloader(source, output)
    source.rename(tmp_path / "old")
    source.symlink_to(tmp_path / "old", target_is_directory=True)
    with pytest.raises(ValidationError):
        downloader.download("photo.png")


def test_root_validation(roots, tmp_path):
    source, output = roots
    for a, b in ((source, source), (source, source / "nested"), (tmp_path / "missing", output)):
        with pytest.raises(ValidationError):
            MediaDownloader(a, b)
    link = tmp_path / "linked"
    link.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValidationError):
        MediaDownloader(link, output)
    with pytest.raises(ValidationError):
        MediaDownloader(source, output, max_bytes=0)


def test_hash_and_size_failure_leave_no_file(roots):
    source, output = roots
    downloader = MediaDownloader(source, output)
    for digest in ("0" * 64, "not-a-hash", 12):
        with pytest.raises(ValidationError):
            downloader.download("photo.png", sha256=digest)
    with pytest.raises(ValidationError):
        MediaDownloader(source, output, max_bytes=1).download("photo.png")
    assert list(output.iterdir()) == []


def test_nonregular_source_no_blocking(roots):
    source, output = roots
    os.mkfifo(source / "pipe")
    (source / "directory").mkdir()
    downloader = MediaDownloader(source, output)
    for path in ("pipe", "directory"):
        with pytest.raises(ValidationError):
            downloader.download(path)


def test_message_namespace_and_provenance(roots):
    source, output = roots
    downloader = MediaDownloader(source, output)
    message = Message("same-id", "account-a", "chat-a", 1, "sender", "image", type="image", metadata={"media_path": "photo.png"})
    for account, chat in (("other", "chat-a"), ("account-a", "other")):
        with pytest.raises(IdentityMismatch):
            downloader.download_message(message, account_id=account, chat_id=chat)
    assert downloader.download_message(message, account_id="account-a", chat_id="chat-a").exists()
    forged = Message("x", "account-a", "chat-a", 2, "s", "", origin="device", metadata={"media_path": "photo.png"})
    with pytest.raises(ValidationError):
        downloader.download_message(forged, account_id="account-a", chat_id="chat-a")
