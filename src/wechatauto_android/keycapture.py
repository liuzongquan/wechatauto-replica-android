"""Experimental, explicitly consented WCDB password observation.

No device has been validated with this adapter. An exact WeChat version is a
scope restriction, NOT a compatibility claim. Supported experimental profiles
observe the documented WCDB openDatabase seven-argument overload or WCDB2's
setCipherKey(byte[], int, Database.CipherVersion) plus that object's getPath().
Frida 16.7.19 is pinned because Frida 17 removed the bundled Java bridge; neither
a runtime nor a bridge is installed here.

Use an already configured, trusted USB Frida endpoint and an existing, authorized
quiescent snapshot. The owner must cause a normal database-open or WCDB2
key-configuration event themselves. This module never launches, restarts, unlocks
or stops WeChat in its default attach mode. Optional spawn mode ALSO requires
explicit package-stop/relaunch and system_server/zygote-instrumentation consent.
Frida unconditionally force-stops the package even after an idle-app preflight;
that check is not atomic. Spawn is restricted to Android user 0. No such device
action was performed in development or tests. It never changes keys, database
arguments or method results.
Instrumentation can nevertheless
change timing or destabilize an app: root-read consent is NOT attach consent.

Candidates stay in memory and are never returned. Only a password that opens the
scoped encrypted snapshot, passes integrity/schema/account checks, and matches
an explicit cipher profile can become a BoundDatabaseKey. Scope nonces are not
remote attestation. The selected runtime and local process must be trusted.
With HMAC disabled, successful decryption and SQLite integrity/schema checks do
not provide cryptographic authentication of the database or account provenance.
Binary passwords of 1..40 bytes are preserved exactly (including NUL/non-UTF-8).
Longer passwords require lossless UTF-8 without NUL, because the pinned SQLCipher
hexkey implementation truncates binary input after 40 bytes. There is no lossy
decoding, raw-key hex conversion, default cipher guessing, or persistence.
Python/Frida may retain immutable copies: zeroization is best effort only.

Primary API references (implementation is original, no WeKit/GPL code):
https://tencent.github.io/wcdb/references/android/reference/com/tencent/wcdb/database/SQLiteDatabase.html
https://tencent.github.io/wcdb/references/android/reference/com/tencent/wcdb/database/SQLiteCipherSpec.html
https://raw.githubusercontent.com/Tencent/wcdb/v1.0.8/android/wcdb/src/com/tencent/wcdb/database/SQLiteCipherSpec.java
https://github.com/Tencent/wcdb/blob/17ebdcbb0a6d610432ea0106718a40676506b0a3/src/java/main/src/main/java/com/tencent/wcdb/core/Database.java
https://frida.re/docs/bridges/
https://raw.githubusercontent.com/frida/frida-python/16.7.19/frida/core.py
https://github.com/frida/frida-core/blob/16.7.19/src/linux/linux-host-session.vala
https://github.com/frida/frida-core/blob/16.7.19/src/linux/agent/system-server.js
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.resources
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import (
    IdentityMismatch, OutcomeUnknown, PermissionDenied,
    UnsupportedFeature, ValidationError, WeChatError,
)
from .rooted import AndroidDatabase, BoundDatabaseKey, CipherProfile, DatabaseOpenError, RootSnapshot

FRIDA_VERSION = "16.7.19"
HOOK_PROFILE = "wcdb-java-open7-experimental-v1"
MODERN_HOOK_PROFILE = "wcdb2-setcipher3-experimental-v1"
PACKAGE = "com.tencent.mm"
_SIGNATURE = (
    "java.lang.String", "[B", "com.tencent.wcdb.database.SQLiteCipherSpec",
    "com.tencent.wcdb.database.SQLiteDatabase$CursorFactory", "int",
    "com.tencent.wcdb.DatabaseErrorHandler", "int",
)
_MODERN_SIGNATURE = ("[B", "int", "com.tencent.wcdb.core.Database$CipherVersion")
_SIGNATURES = {HOOK_PROFILE: _SIGNATURE, MODERN_HOOK_PROFILE: _MODERN_SIGNATURE}
_ASSETS = {HOOK_PROFILE: "wcdb_open7.js", MODERN_HOOK_PROFILE: "wcdb2_setcipher3.js"}
_MAX_KEY_BYTES = 4096
_ALLOWED_FILES = frozenset(
    base + suffix for base in ("EnMicroMsg.db", "SnsMicroMsg.db")
    for suffix in ("", "-wal", "-shm", "-journal")
)


class CaptureUnavailable(WeChatError):
    """No verified candidate; intentionally contains no remote diagnostic text."""


@dataclass(frozen=True)
class CaptureConfig:
    serial: str
    account_dir: str
    account_id: str
    expected_wechat_version: str
    android_user: int = 0
    consent_attach: bool = False
    allow_unverified_profile: bool = False
    timeout_seconds: int = 30
    max_candidates: int = 8
    hook_profile: str = HOOK_PROFILE
    mode: str = "attach"
    consent_package_stop_relaunch: bool = False
    consent_system_instrumentation: bool = False

    def __post_init__(self):
        if not isinstance(self.serial, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.serial):
            raise ValidationError("an explicit trusted device serial is required")
        if not isinstance(self.account_dir, str) or not re.fullmatch(
                r"[0-9a-f]{32}", self.account_dir):
            raise ValidationError("account_dir must be 32 lowercase hexadecimal characters")
        if (not isinstance(self.account_id, str) or not 1 <= len(self.account_id) <= 256
                or "\x00" in self.account_id):
            raise ValidationError("an explicit account_id is required")
        if (not isinstance(self.expected_wechat_version, str) or not re.fullmatch(
                r"[0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?", self.expected_wechat_version)
                or int(self.expected_wechat_version.split('.')[0]) < 8):
            raise ValidationError("an exact WeChat version >= 8.0.0 is required")
        for value, minimum, maximum in (
                (self.android_user, 0, 9999), (self.timeout_seconds, 1, 300),
                (self.max_candidates, 1, 32)):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValidationError("capture bound or Android user is invalid")
        if self.consent_attach is not True:
            raise PermissionDenied("explicit process-attach consent is required separately from root")
        if self.allow_unverified_profile is not True:
            raise PermissionDenied("explicit experimental-profile acknowledgement is required")
        if type(self.hook_profile) is not str or self.hook_profile not in _SIGNATURES:
            raise UnsupportedFeature("an exact supported experimental hook profile is required")
        if self.mode not in ("attach", "spawn"):
            raise ValidationError("capture mode must be attach or spawn")
        if self.mode == "spawn":
            if (self.consent_package_stop_relaunch is not True
                    or self.consent_system_instrumentation is not True):
                raise PermissionDenied(
                    "spawn separately requires package-force-stop/relaunch and "
                    "zygote/system_server instrumentation consent")
            if self.android_user != 0:
                raise UnsupportedFeature("experimental spawn supports only Android user 0")

    @property
    def paths(self) -> tuple[str, ...]:
        tail = f"{PACKAGE}/MicroMsg/{self.account_dir}/EnMicroMsg.db"
        paths = (f"/data/user/{self.android_user}/{tail}",)
        return paths + ((f"/data/data/{tail}",) if self.android_user == 0 else ())


def _check_snapshot(snapshot, config):
    if not isinstance(snapshot, RootSnapshot) or type(snapshot.manifest) is not dict:
        raise ValidationError("an existing scoped RootSnapshot is required")
    expected = {
        "schema_version": 1, "kind": "android_rooted_quiesced_snapshot",
        "serial": config.serial, "package": PACKAGE, "android_user": config.android_user,
        "wechat_version": config.expected_wechat_version, "account_dir": config.account_dir,
        "asserted_account_id": config.account_id,
    }
    if any(type(snapshot.manifest.get(k)) is not type(v) or snapshot.manifest[k] != v
           for k, v in expected.items()):
        raise IdentityMismatch("snapshot scope differs from the authorized capture scope")
    api = snapshot.manifest.get("android_api")
    if type(api) is not int or api < 30:
        raise UnsupportedFeature("capture requires an Android API 30+ snapshot")
    directory = Path(snapshot.directory).absolute()
    if directory.resolve() != directory or not directory.is_dir():
        raise ValidationError("snapshot directory must be canonical and local")
    files = snapshot.manifest.get("files")
    if (type(files) is not dict or "EnMicroMsg.db" not in files
            or not set(files).issubset(_ALLOWED_FILES)):
        raise ValidationError("snapshot manifest has unsupported files")
    for name in _ALLOWED_FILES:
        path = directory / name
        if path.is_symlink() or path.exists() != (name in files):
            raise IdentityMismatch("snapshot file set differs from its manifest")
        if name not in files:
            continue
        record = files[name]
        if (type(record) is not dict or set(record) != {"size", "sha256"}
                or type(record["size"]) is not int or not 0 <= record["size"] <= 16 * 1024**3
                or type(record["sha256"]) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
                or not path.is_file() or path.stat().st_size != record["size"]):
            raise ValidationError("snapshot file metadata is invalid")
        if (name == "EnMicroMsg.db" and record["size"] == 0
                or name.endswith("-journal") and record["size"] != 0
                or name.endswith(("-wal", "-shm", "-journal"))
                   and name.rsplit("-", 1)[0] not in files):
            raise ValidationError("snapshot contains an empty database, hot journal or orphan sidecar")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != record["sha256"]:
            raise IdentityMismatch("snapshot bytes differ from its manifest")
    with snapshot.database_path.open("rb") as handle:
        if handle.read(16) == b"SQLite format 3\x00":
            raise ValidationError("key capture requires an encrypted snapshot, not plaintext")
    return api


def _check_cipher(profile):
    if not isinstance(profile, CipherProfile):
        raise ValidationError("an explicit CipherProfile is required")
    profile.__post_init__()
    if any(value is None for value in (profile.kdf_iterations, profile.use_hmac,
                                       profile.kdf_algorithm, profile.hmac_algorithm)):
        raise ValidationError("capture requires every cipher setting explicitly specified")


@contextmanager
def _verification_copy(snapshot, config):
    """Stage encrypted bytes only; SQLite may write transient SHM even in mode=ro.

    Keep the supplied snapshot and its manifest completely unchanged. The bound
    key fingerprint is logical main/WAL/journal bytes, so a verified copy has the
    same binding. No password/plaintext is persisted in this temporary directory.
    """
    _check_snapshot(snapshot, config)
    files = {name: dict(record) for name, record in snapshot.manifest["files"].items()
             if name == "EnMicroMsg.db" or name.startswith("EnMicroMsg.db-")}
    with tempfile.TemporaryDirectory(prefix="wechatauto-key-check-") as temporary:
        directory = Path(temporary)
        os.chmod(directory, 0o700)
        source_dir = os.open(snapshot.directory, os.O_RDONLY | os.O_DIRECTORY
                             | getattr(os, "O_NOFOLLOW", 0))
        try:
            for name, record in files.items():
                source_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                                    dir_fd=source_dir)
                with os.fdopen(source_fd, "rb") as source:
                    metadata = os.fstat(source.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != record["size"]:
                        raise IdentityMismatch("snapshot changed while staging verification")
                    target_fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                        0o600)
                    digest, copied = hashlib.sha256(), 0
                    with os.fdopen(target_fd, "wb") as target:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            copied += len(chunk)
                            if copied > record["size"]:
                                raise IdentityMismatch("snapshot grew while staging verification")
                            digest.update(chunk)
                            target.write(chunk)
                    if copied != record["size"] or digest.hexdigest() != record["sha256"]:
                        raise IdentityMismatch("verification copy differs from the scoped snapshot")
        finally:
            os.close(source_dir)
        _check_snapshot(snapshot, config)
        staged_manifest = dict(snapshot.manifest, files=files)
        _check_snapshot(RootSnapshot(directory, staged_manifest), config)
        try:
            yield directory / "EnMicroMsg.db"
        finally:
            _check_snapshot(snapshot, config)


def _cipher_matches(value, profile, hook_profile=HOOK_PROFILE):
    if hook_profile == MODERN_HOOK_PROFILE:
        # WCDB2 exposes no KDF/HMAC values here. Its defaultVersion is deliberately
        # represented as unknown, not mapped to a guessed SQLCipher generation.
        # The fully explicit host profile must independently decrypt the snapshot.
        return (type(value) is dict and len(value) == 2
                and set(value) == {"page_size", "compatibility"}
                and type(value["page_size"]) is int
                and value["page_size"] == profile.page_size
                and (value["compatibility"] is None
                     or (type(value["compatibility"]) is int
                         and value["compatibility"] == profile.compatibility)))
    keys = {"cipher", "page_size", "kdf_iterations", "use_hmac", "kdf_algorithm",
            "hmac_algorithm", "compatibility"}
    if type(value) is not dict or len(value) != len(keys) or set(value) != keys:
        return False
    if value["cipher"] not in (None, "aes-256-cbc"):
        return False
    for key, wanted in (("page_size", profile.page_size),
                        ("kdf_iterations", profile.kdf_iterations),
                        ("use_hmac", profile.use_hmac)):
        if type(value[key]) is not type(wanted) or value[key] != wanted:
            return False
    # Old official WCDB exposes no algorithm/version fields. Missing values are
    # not inferred; explicit profile values still must pass SQLCipher validation.
    for key, wanted in (("kdf_algorithm", profile.kdf_algorithm),
                        ("hmac_algorithm", profile.hmac_algorithm),
                        ("compatibility", profile.compatibility)):
        if value[key] is not None and (type(value[key]) is not type(wanted)
                                       or value[key] != wanted):
            return False
    return True


class _Candidate:
    __slots__ = ("_material",)

    def __init__(self, material):
        self._material = bytearray(material)

    def passphrase(self):
        if 1 <= len(self._material) <= 40:
            return bytes(self._material)
        try:
            result = self._material.decode("utf-8", errors="strict")
        except UnicodeError:
            raise CaptureUnavailable("long binary passwords are unsupported by the pinned driver") from None
        if not result or "\x00" in result:
            raise CaptureUnavailable("long binary passwords are unsupported by the pinned driver")
        return result

    def clear(self):
        self._material[:] = b"\x00" * len(self._material)

    def __repr__(self):
        return "<WCDBCandidate material=redacted unverified=True>"

    def __getstate__(self):
        raise TypeError("database key candidates cannot be serialized")


class _Inbox:
    """Callbacks cannot leak exception text through Frida's traceback printer."""
    def __init__(self, config, profile, nonce, api):
        self.config, self.profile, self.nonce, self.api = config, profile, nonce, api
        self.condition = threading.Condition()
        self.candidates = deque()
        self.ready = False
        self.failure = None
        self.closed = False
        self.events = 0
        self.sequence = 0

    def fail(self, message):
        with self.condition:
            if not self.closed:
                self.failure = message
                self.condition.notify_all()

    def on_message(self, message, data):
        with self.condition:
            if self.closed or self.failure is not None:
                return
            try:
                self.events += 1
                if self.events > self.config.max_candidates + 2:
                    raise ValueError
                if (type(message) is not dict or len(message) != 2
                        or set(message) != {"type", "payload"}):
                    raise ValueError
                if message["type"] != "send" or type(message["payload"]) is not dict:
                    raise ValueError
                value = message["payload"]
                if len(value) > 12:
                    raise ValueError
                if value.get("protocol") != 1 or type(value.get("protocol")) is not int:
                    raise ValueError
                if value.get("nonce") != self.nonce:
                    raise ValueError
                if value.get("kind") == "error":
                    raise ValueError
                common = {"protocol", "nonce", "kind", "package", "version", "android_user",
                          "api", "profile"}
                identity = {"package": PACKAGE, "version": self.config.expected_wechat_version,
                            "android_user": self.config.android_user, "api": self.api,
                            "profile": self.config.hook_profile}
                if any(type(value.get(k)) is not type(v) or value[k] != v
                       for k, v in identity.items()):
                    raise ValueError
                if value.get("kind") == "ready":
                    if (self.ready or set(value) != common | {"signature"}
                            or value["signature"] != list(_SIGNATURES[self.config.hook_profile])
                            or data is not None):
                        raise ValueError
                    self.ready = True
                elif value.get("kind") == "candidate":
                    if (not self.ready or set(value) != common | {"path", "cipher", "seq"}
                            or type(value["path"]) is not str or value["path"] not in self.config.paths
                            or type(value["seq"]) is not int or value["seq"] != self.sequence + 1
                            or self.sequence >= self.config.max_candidates
                            or type(data) is not bytes or not 1 <= len(data) <= _MAX_KEY_BYTES
                            or not _cipher_matches(value["cipher"], self.profile,
                                                   self.config.hook_profile)):
                        raise ValueError
                    self.sequence += 1
                    self.candidates.append(_Candidate(data))
                else:
                    raise ValueError
            except Exception:
                # Never echo a payload, error message, stack, candidate or remote log.
                self.failure = "observer event rejected or runtime profile unavailable"
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            while self.candidates:
                self.candidates.popleft().clear()
            self.condition.notify_all()


def _source(config, nonce):
    settings = {"nonce": nonce, "version": config.expected_wechat_version,
                "android_user": config.android_user, "paths": list(config.paths),
                "profile": config.hook_profile, "max_candidates": config.max_candidates,
                "max_key_bytes": _MAX_KEY_BYTES, "frida_version": FRIDA_VERSION,
                "mode": config.mode}
    asset = importlib.resources.files("wechatauto_android").joinpath(
        "assets/" + _ASSETS[config.hook_profile])
    return "const CAPTURE = " + json.dumps(settings, separators=(",", ":")) + ";\n" + asset.read_text("utf-8")


class FridaKeyCapture:
    def __init__(self, config: CaptureConfig):
        if not isinstance(config, CaptureConfig):
            raise ValidationError("CaptureConfig is required")
        config.__post_init__()
        self.config = config
        self.last_capture = None

    def __repr__(self):
        return f"<FridaKeyCapture experimental=True mode={self.config.mode}>"

    @property
    def operation_notice(self):
        """Caller-facing warning available before any runtime access; no logging."""
        if self.config.mode == "spawn":
            return ("EXPERIMENTAL: Frida will force-stop/relaunch com.tencent.mm for Android "
                    "user 0 and instrument zygote/system_server. An idle-app check is not "
                    "atomic. Detach only confirms our observer cleanup, not restoration of "
                    "Frida-owned system helpers. No live compatibility is claimed.")
        return ("EXPERIMENTAL: attach to the existing com.tencent.mm process only; "
                "a matching normal WCDB event is required and timing may change.")

    def capture(self, snapshot: RootSnapshot, cipher_profile: CipherProfile) -> BoundDatabaseKey:
        """Return only a verified, snapshot-bound key; never expose candidates.

        This synchronous call observes for at most timeout_seconds, plus bounded
        SQLCipher validation and cleanup. A fresh attach is never retried.
        Detach/cleanup uncertainty raises OutcomeUnknown, not success.
        """
        self.config.__post_init__()
        self.last_capture = None
        _check_cipher(cipher_profile)
        api = _check_snapshot(snapshot, self.config)
        try:
            frida = importlib.import_module("frida")
        except ImportError:
            raise UnsupportedFeature("install the optional pinned keycapture dependency locally") from None
        if getattr(frida, "__version__", None) != FRIDA_VERSION:
            raise UnsupportedFeature("this experimental adapter requires Frida 16.7.19 exactly")
        nonce = secrets.token_hex(32)
        inbox = _Inbox(self.config, cipher_profile, nonce, api)
        session = script = device = None
        attach_started = False
        spawn_started = False
        spawned_pid = None
        resumed = False
        verified = False
        deadline = time.monotonic() + self.config.timeout_seconds
        cancel = frida.Cancellable()
        timer = threading.Timer(self.config.timeout_seconds, cancel.cancel)
        timer.daemon = True
        timer.start()
        cleanup_failed = False
        try:
            with cancel:
                device = frida.get_device(self.config.serial, timeout=self.config.timeout_seconds)
                if device.id != self.config.serial or device.type != "usb":
                    raise IdentityMismatch("only the exact trusted USB Frida device is allowed")
                target = PACKAGE
                if self.config.mode == "spawn":
                    # This exact-package query already uses Frida's system-server
                    # helper: the separate consent gate was checked before import.
                    apps = device.enumerate_applications(identifiers=[PACKAGE], scope="metadata")
                    if type(apps) is not list or len(apps) != 1 or apps[0].identifier != PACKAGE:
                        raise IdentityMismatch("spawn requires exactly one matching installed package")
                    app = apps[0]
                    if type(app.pid) is not int or app.pid != 0:
                        raise PermissionDenied("target package must already be stopped; no running app is restarted")
                    parameters = app.parameters
                    if (type(parameters) is not dict
                            or parameters.get("version") != self.config.expected_wechat_version
                            or parameters.get("data-dir") not in (
                                "/data/user/0/com.tencent.mm", "/data/data/com.tencent.mm")):
                        raise IdentityMismatch("spawn package version or Android user differs from scope")
                    spawn_started = True
                    spawned_pid = device.spawn(PACKAGE, uid=0)
                    if type(spawned_pid) is not int or spawned_pid <= 0:
                        spawned_pid = None
                        raise OutcomeUnknown("spawn returned no usable process identity; inspect the selected runtime")
                    target = spawned_pid
                # Fixed process-name lookup only; no wildcard target or fallback.
                # Frida's SDK internally enumerates process metadata for this lookup.
                attach_started = True
                session = device.attach(target)
                session.on("detached", lambda *_: inbox.fail("observer detached before completion"))
                script = session.create_script(_source(self.config, nonce),
                                               name="wcdb-scoped-observer", runtime="qjs")
                # Frida's default log handler prints to stdout/stderr: disable it BEFORE load.
                script.set_log_handler(lambda *_: inbox.fail("observer emitted unexpected logging"))
                script.on("message", inbox.on_message)
                script.load()
                if spawned_pid is not None:
                    # Java.perform may wait for app initialization. Never wait for
                    # ready/events while the newly launched main thread is paused.
                    device.resume(spawned_pid)
                    resumed = True
            while True:
                with inbox.condition:
                    remaining = deadline - time.monotonic()
                    if inbox.failure is not None:
                        raise CaptureUnavailable(inbox.failure)
                    if remaining <= 0:
                        raise CaptureUnavailable("no verified key before timeout; a normal scoped database-open or key-configuration event is required")
                    if not inbox.candidates:
                        inbox.condition.wait(remaining)
                        continue
                    candidate = inbox.candidates.popleft()
                try:
                    material = candidate.passphrase()
                    # A current live event does not turn an older snapshot into live data.
                    with _verification_copy(snapshot, self.config) as verification_path:
                        with AndroidDatabase(verification_path, self.config.account_id,
                                expected_wechat_version=self.config.expected_wechat_version,
                                key_provider=lambda: material, cipher_profile=cipher_profile,
                                key_source="frida_observed_candidate_verified") as database:
                            key = database.validated_key
                            if not isinstance(key, BoundDatabaseKey):
                                raise CaptureUnavailable("candidate did not yield a decrypt-validated bound key")
                    with inbox.condition:
                        if inbox.failure is not None:
                            raise CaptureUnavailable(inbox.failure)
                    verified = True
                    return key
                except DatabaseOpenError:
                    # Wrong candidates are expected; never report driver diagnostics.
                    pass
                finally:
                    candidate.clear()
                    material = None
        except (WeChatError, KeyboardInterrupt):
            raise
        except Exception:
            if spawn_started and spawned_pid is None:
                raise OutcomeUnknown("spawn outcome could not be confirmed; inspect the selected runtime") from None
            if attach_started and session is None:
                raise OutcomeUnknown("attach outcome could not be confirmed; inspect the selected runtime") from None
            raise CaptureUnavailable("attach or observation failed; no automatic retry was attempted") from None
        finally:
            timer.cancel()
            inbox.close()
            # Fresh cancellation scope: the observation deadline may already have fired.
            cleanup_timer = None
            try:
                cleanup_cancel = frida.Cancellable()
                cleanup_timer = threading.Timer(5, cleanup_cancel.cancel)
                cleanup_timer.daemon = True
                cleanup_timer.start()
                with cleanup_cancel:
                    try:
                        if spawned_pid is not None and not resumed:
                            try:
                                device.resume(spawned_pid)
                                resumed = True
                            except Exception:
                                cleanup_failed = True
                    finally:
                        try:
                            if script is not None:
                                try:
                                    script.unload()
                                except Exception:
                                    cleanup_failed = True
                        finally:
                            if session is not None:
                                try:
                                    session.detach()
                                except Exception:
                                    cleanup_failed = True
            except Exception:
                cleanup_failed = True
            finally:
                if cleanup_timer is not None:
                    cleanup_timer.cancel()
            if cleanup_failed:
                raise OutcomeUnknown("observer cleanup could not be confirmed; inspect the selected runtime") from None
            if verified:
                self.last_capture = CaptureReceipt(self.config.mode, self.config.hook_profile,
                                                   spawned_pid is not None, resumed)


@dataclass(frozen=True)
class CaptureReceipt:
    """Non-secret process-local metadata; not an attestation or a persisted report."""
    mode: str
    hook_profile: str
    package_stop_relaunch_requested: bool
    spawned_process_resumed: bool
    observer_cleanup_confirmed: bool = True
    key_evidence: str = "encrypted_snapshot_decryption_integrity_schema_match"
    live_compatibility: str = "unverified"

    @property
    def system_instrumentation_requested(self):
        return self.mode == "spawn"
