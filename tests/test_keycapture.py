"""Offline-only capture tests: synthetic events and synthetic encrypted DBs.

No Frida installation, endpoint discovery, device attachment, real account or
real password is used by this suite. Node executes the JS in a mock Java bridge.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import pickle
import shutil
import subprocess

import pytest

from wechatauto_android import keycapture as capture
from wechatauto_android.errors import (
    IdentityMismatch, OutcomeUnknown, PermissionDenied, UnsupportedFeature, ValidationError,
)
from wechatauto_android.rooted import AndroidDatabase, CipherProfile, RootSnapshot

FAKE_KEY = b"offline-fixture-password"
ACCOUNT = "wxid_offline_fixture"


def config(**changes):
    values = dict(serial="offline-usb-serial", account_dir="a" * 32, account_id=ACCOUNT,
                  expected_wechat_version="8.0.49", consent_attach=True,
                  allow_unverified_profile=True, timeout_seconds=1)
    values.update(changes)
    return capture.CaptureConfig(**values)


def profile():
    return CipherProfile.legacy_android()


def snapshot(tmp_path, contents=b"offline-encrypted-placeholder"):
    path = tmp_path / "EnMicroMsg.db"
    if contents is not None:
        path.write_bytes(contents)
    files = {}
    for file in tmp_path.iterdir():
        if file.name in capture._ALLOWED_FILES:
            files[file.name] = {"size": file.stat().st_size,
                                "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
    return RootSnapshot(tmp_path, {"schema_version": 1,
        "kind": "android_rooted_quiesced_snapshot", "serial": config().serial,
        "android_user": 0, "android_api": 30, "package": capture.PACKAGE,
        "wechat_version": config().expected_wechat_version, "account_dir": config().account_dir,
        "asserted_account_id": ACCOUNT, "account_identity_verified": False, "files": files})


def payload(nonce="n", kind="ready", **updates):
    value = {"protocol": 1, "nonce": nonce, "kind": kind, "package": capture.PACKAGE,
             "version": config().expected_wechat_version, "android_user": 0,
             "api": 30, "profile": capture.HOOK_PROFILE}
    if kind == "ready":
        value["signature"] = list(capture._SIGNATURE)
    elif kind == "candidate":
        value.update(path=config().paths[0], seq=1, cipher={
            "cipher": None, "page_size": 1024, "kdf_iterations": 4000, "use_hmac": False,
            "kdf_algorithm": "PBKDF2_HMAC_SHA1", "hmac_algorithm": "HMAC_SHA1",
            "compatibility": 1})
    value.update(updates)
    return {"type": "send", "payload": value}


class FakeCancel:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class FakeScript:
    def __init__(self, source, actions):
        self.settings = json.loads(source.split(";\n", 1)[0].removeprefix("const CAPTURE = "))
        self.actions = actions
        self.callback = None
        self.unloaded = False
        self.log_handler = None
        self.fail_unload = False

    def set_log_handler(self, callback):
        self.log_handler = callback

    def on(self, signal, callback):
        assert signal == "message"
        self.callback = callback

    def load(self):
        assert self.log_handler is not None
        for action in self.actions:
            if isinstance(action, Exception):
                raise action
            if action == "log":
                self.log_handler("error", FAKE_KEY.decode())
            else:
                event, data = action
                event = json.loads(json.dumps(event))
                if isinstance(event.get("payload"), dict):
                    event["payload"]["nonce"] = self.settings["nonce"]
                self.callback(event, data)

    def unload(self):
        self.unloaded = True
        if self.fail_unload:
            raise RuntimeError(FAKE_KEY.decode())


class FakeSession:
    def __init__(self, actions):
        self.actions = actions
        self.detached = False
        self.script = None
        self.fail_detach = False

    def on(self, signal, callback):
        assert signal == "detached"
        self.callback = callback

    def create_script(self, source, name, runtime):
        assert name == "wcdb-scoped-observer"
        assert runtime == "qjs"
        self.script = FakeScript(source, self.actions)
        return self.script

    def detach(self):
        self.detached = True
        if self.fail_detach:
            raise RuntimeError(FAKE_KEY.decode())
        self.callback("application-requested", None)


class FakeFrida:
    __version__ = capture.FRIDA_VERSION
    Cancellable = FakeCancel

    def __init__(self, actions):
        self.calls = []
        self.session = FakeSession(actions)
        self.id = config().serial
        self.type = "usb"
        self.attach_error = None

    def get_device(self, serial, timeout):
        self.calls.append(("device", serial, timeout))
        return self

    def attach(self, target):
        self.calls.append(("attach", target))
        if self.attach_error:
            raise self.attach_error
        return self.session


def install_fake(monkeypatch, actions):
    fake = FakeFrida(actions)
    original = capture.importlib.import_module
    monkeypatch.setattr(capture.importlib, "import_module",
                        lambda name: fake if name == "frida" else original(name))
    return fake


@pytest.mark.parametrize("field,value,error", [
    ("consent_attach", False, PermissionDenied),
    ("consent_attach", 1, PermissionDenied),
    ("allow_unverified_profile", False, PermissionDenied),
    ("serial", "", ValidationError), ("serial", "adb;reboot", ValidationError),
    ("account_dir", "../other", ValidationError), ("account_id", "", ValidationError),
    ("android_user", True, ValidationError), ("android_user", -1, ValidationError),
    ("expected_wechat_version", "8.*", ValidationError),
    ("expected_wechat_version", "7.0.1", ValidationError),
    ("timeout_seconds", 0, ValidationError), ("timeout_seconds", 301, ValidationError),
    ("max_candidates", 0, ValidationError), ("max_candidates", 33, ValidationError),
])
def test_config_refuses_invalid_scope_or_missing_consent(field, value, error):
    with pytest.raises(error):
        config(**{field: value})


def test_config_paths_no_cross_user_alias():
    assert len(config().paths) == 2
    assert config(android_user=10).paths == (
        f"/data/user/10/com.tencent.mm/MicroMsg/{'a' * 32}/EnMicroMsg.db",)


def test_inbox_accepts_only_scoped_private_binary_candidate():
    inbox = capture._Inbox(config(), profile(), "n", 30)
    inbox.on_message(payload(), None)
    inbox.on_message(payload(kind="candidate"), FAKE_KEY)
    candidate = inbox.candidates.popleft()
    assert inbox.failure is None
    assert candidate.passphrase() == FAKE_KEY
    assert FAKE_KEY.decode() not in repr(candidate)
    with pytest.raises(TypeError):
        pickle.dumps(candidate)
    candidate.clear()
    assert not any(candidate._material)


@pytest.mark.parametrize("mutation", [
    {"nonce": "spoof"}, {"version": "8.0.50"}, {"package": "other.app"},
    {"android_user": 1}, {"api": 29}, {"profile": "anything"}, {"seq": 2},
    {"seq": True}, {"protocol": True}, {"path": "/data/user/0/com.tencent.mm/other.db"},
    {"path": config().paths[0] + "/../EnMicroMsg.db"}, {"extra": FAKE_KEY.decode()},
])
def test_inbox_rejects_spoof_scope_sequence_or_extra_fields(mutation):
    inbox = capture._Inbox(config(), profile(), "n", 30)
    inbox.on_message(payload(), None)
    inbox.on_message(payload(kind="candidate", **mutation), FAKE_KEY)
    assert inbox.failure is not None
    assert not inbox.candidates
    assert FAKE_KEY.decode() not in inbox.failure


@pytest.mark.parametrize("data", [b"", b"x" * 4097, "not-bytes", bytearray(b"candidate"), None])
def test_inbox_refuses_bad_key_payload_without_copying(data):
    inbox = capture._Inbox(config(), profile(), "n", 30)
    inbox.on_message(payload(), None)
    inbox.on_message(payload(kind="candidate"), data)
    assert inbox.failure is not None
    assert not inbox.candidates


@pytest.mark.parametrize("cipher_change", [
    {"page_size": 0}, {"kdf_iterations": 0}, {"use_hmac": 0},
    {"cipher": "devlock"}, {"hmac_algorithm": "HMAC_SHA512"},
    {"compatibility": True}, {"kdf_algorithm": "unknown"},
])
def test_inbox_rejects_unknown_or_inconsistent_cipher(cipher_change):
    inbox = capture._Inbox(config(), profile(), "n", 30)
    inbox.on_message(payload(), None)
    event = payload(kind="candidate")
    event["payload"]["cipher"].update(cipher_change)
    inbox.on_message(event, FAKE_KEY)
    assert inbox.failure is not None


def test_no_candidates_before_ready_or_after_close():
    inbox = capture._Inbox(config(), profile(), "n", 30)
    inbox.on_message(payload(kind="candidate"), FAKE_KEY)
    assert inbox.failure
    inbox.close()
    inbox.on_message(payload(), None)
    assert not inbox.ready


def test_message_caps_and_candidate_scrub():
    inbox = capture._Inbox(config(max_candidates=1), profile(), "n", 30)
    inbox.on_message(payload(), None)
    inbox.on_message(payload(kind="candidate"), FAKE_KEY)
    candidate = inbox.candidates[0]
    inbox.on_message(payload(kind="candidate", seq=2), FAKE_KEY)
    assert inbox.failure
    inbox.close()
    assert not any(candidate._material)
    assert not inbox.candidates


@pytest.mark.parametrize("data", [b"invalid\xff" * 6, b"zero\x00tail" * 6])
def test_no_lossy_password_transform(data):
    candidate = capture._Candidate(data)
    with pytest.raises(capture.CaptureUnavailable, match="long binary passwords are unsupported"):
        candidate.passphrase()


@pytest.mark.parametrize("data", [b"invalid\xff", b"zero\x00tail", bytes(range(40))])
def test_binary_passwords_preserve_exact_bytes(data):
    candidate = capture._Candidate(data)
    assert candidate.passphrase() == data
    assert type(candidate.passphrase()) is bytes


def test_long_utf8_password_roundtrips_without_normalizing():
    data = ("e\u0301'" * 20).encode("utf-8")
    candidate = capture._Candidate(data)
    assert type(candidate.passphrase()) is str
    assert candidate.passphrase().encode("utf-8") == data


@pytest.mark.parametrize("mutation", [
    {"serial": "other"}, {"android_user": 10}, {"android_api": 29},
    {"account_dir": "b" * 32}, {"asserted_account_id": "someone_else"},
    {"wechat_version": "8.0.50"}, {"package": "other"}, {"schema_version": True},
])
def test_snapshot_identity_mismatch_before_attach(tmp_path, monkeypatch, mutation):
    snap = snapshot(tmp_path)
    snap.manifest.update(mutation)
    fake = install_fake(monkeypatch, [])
    with pytest.raises((IdentityMismatch, UnsupportedFeature)):
        capture.FridaKeyCapture(config()).capture(snap, profile())
    assert fake.calls == []


@pytest.mark.parametrize("tamper", ["main", "new_wal", "symlink", "manifest", "plaintext"])
def test_snapshot_tampering_before_attach(tmp_path, monkeypatch, tamper):
    snap = snapshot(tmp_path)
    if tamper == "main":
        snap.database_path.write_bytes(b"changed")
    elif tamper == "new_wal":
        (tmp_path / "EnMicroMsg.db-wal").write_bytes(b"new")
    elif tamper == "symlink":
        snap.database_path.rename(tmp_path / "elsewhere")
        snap.database_path.symlink_to(tmp_path / "elsewhere")
    elif tamper == "manifest":
        snap.manifest["files"]["../../private"] = {"size": 1, "sha256": "0" * 64}
    else:
        snap = snapshot(tmp_path, b"SQLite format 3\x00" + b"x" * 100)
    fake = install_fake(monkeypatch, [])
    with pytest.raises((IdentityMismatch, ValidationError)):
        capture.FridaKeyCapture(config()).capture(snap, profile())
    assert fake.calls == []


def test_missing_cipher_setting_before_attach(tmp_path, monkeypatch):
    fake = install_fake(monkeypatch, [])
    with pytest.raises(ValidationError):
        capture.FridaKeyCapture(config()).capture(snapshot(tmp_path), CipherProfile(1, 1024))
    assert fake.calls == []


@pytest.mark.parametrize("invalid", ["empty", "hot_journal", "orphan"])
def test_invalid_snapshot_bundle_rejected_before_attach(tmp_path, monkeypatch, invalid):
    snapshot(tmp_path)
    if invalid == "empty":
        (tmp_path / "EnMicroMsg.db").write_bytes(b"")
    elif invalid == "hot_journal":
        (tmp_path / "EnMicroMsg.db-journal").write_bytes(b"uncommitted")
    else:
        (tmp_path / "SnsMicroMsg.db-wal").write_bytes(b"orphan")
    snap = snapshot(tmp_path, contents=None)
    fake = install_fake(monkeypatch, [])
    with pytest.raises(ValidationError):
        capture.FridaKeyCapture(config()).capture(snap, profile())
    assert fake.calls == []


def test_wrong_runtime_version_before_device_lookup(tmp_path, monkeypatch):
    fake = install_fake(monkeypatch, [])
    fake.__version__ = "17.0.0"
    with pytest.raises(UnsupportedFeature):
        capture.FridaKeyCapture(config()).capture(snapshot(tmp_path), profile())
    assert not fake.calls


def test_installed_frida_sdk_contract_without_device_connection():
    # This is deliberately not importorskip: keycapture-extra CI must install the
    # real pinned SDK. Introspection/cancellation do not enumerate or attach devices.
    import frida
    assert frida.__version__ == capture.FRIDA_VERSION
    assert "timeout" in inspect.signature(frida.get_device).parameters
    assert "target" in inspect.signature(frida.core.Device.attach).parameters
    assert "name" in inspect.signature(frida.core.Session.create_script).parameters
    assert "runtime" in inspect.signature(frida.core.Session.create_script).parameters
    assert "handler" in inspect.signature(frida.core.Script.set_log_handler).parameters
    assert "program" in inspect.signature(frida.core.Device.spawn).parameters
    assert "target" in inspect.signature(frida.core.Device.resume).parameters
    assert "identifiers" in inspect.signature(frida.core.Device.enumerate_applications).parameters
    cancel = frida.Cancellable()
    with cancel:
        assert not cancel.is_cancelled
        cancel.cancel()
        assert cancel.is_cancelled


def test_uncertain_attach_never_retried(tmp_path, monkeypatch):
    fake = install_fake(monkeypatch, [])
    fake.attach_error = RuntimeError(FAKE_KEY.decode())
    with pytest.raises(OutcomeUnknown) as error:
        capture.FridaKeyCapture(config()).capture(snapshot(tmp_path), profile())
    assert FAKE_KEY.decode() not in str(error.value)
    assert fake.calls.count(("attach", capture.PACKAGE)) == 1


@pytest.mark.parametrize("field,value", [("id", "other-device"), ("type", "remote")])
def test_wrong_endpoint_never_attaches(tmp_path, monkeypatch, field, value):
    fake = install_fake(monkeypatch, [])
    setattr(fake, field, value)
    with pytest.raises(IdentityMismatch):
        capture.FridaKeyCapture(config()).capture(snapshot(tmp_path), profile())
    assert all(call[0] != "attach" for call in fake.calls)


@pytest.mark.parametrize("actions", [
    [(payload(), None)],
    [(payload(), None), ({"type": "error", "description": FAKE_KEY.decode()}, None)],
    [RuntimeError(FAKE_KEY.decode())],
    ["log"],
])
def test_timeout_script_error_and_logging_cleanup_without_leaks(tmp_path, monkeypatch, capsys, actions):
    fake = install_fake(monkeypatch, actions)
    with pytest.raises(capture.CaptureUnavailable) as error:
        capture.FridaKeyCapture(config()).capture(snapshot(tmp_path), profile())
    assert FAKE_KEY.decode() not in str(error.value)
    assert fake.session.script.unloaded and fake.session.detached
    assert fake.calls == [("device", config().serial, 1), ("attach", capture.PACKAGE)]
    assert capsys.readouterr() == ("", "")


def test_cleanup_uncertainty_not_reported_as_success(tmp_path, monkeypatch):
    fake = install_fake(monkeypatch, [(payload(), None)])
    fake.session.fail_detach = True
    with pytest.raises(OutcomeUnknown) as error:
        capture.FridaKeyCapture(config()).capture(snapshot(tmp_path), profile())
    assert FAKE_KEY.decode() not in str(error.value)
    assert fake.session.script.unloaded and fake.session.detached


def encrypted_snapshot(tmp_path, key=FAKE_KEY):
    sqlcipher = pytest.importorskip("sqlcipher3.dbapi2")
    path = tmp_path / "EnMicroMsg.db"
    connection = sqlcipher.connect(path)
    connection.execute("PRAGMA hexkey = '" + key.hex() + "'")
    for pragma in profile().pragmas():
        connection.execute(pragma)
    connection.execute("CREATE TABLE message (msgId INTEGER, msgSvrId INTEGER, talker TEXT, "
                       "content TEXT, type INTEGER, createTime INTEGER, isSend INTEGER)")
    connection.execute("CREATE TABLE userinfo (id INTEGER, value TEXT)")
    connection.execute("INSERT INTO userinfo VALUES (2, ?)", (ACCOUNT,))
    connection.commit()
    connection.close()
    return snapshot(tmp_path, contents=None)


def test_real_sqlcipher_candidate_validation_and_bound_key(tmp_path, monkeypatch, capsys):
    snap = encrypted_snapshot(tmp_path)
    fake = install_fake(monkeypatch, [(payload(), None),
        (payload(kind="candidate"), b"synthetic-wrong-password"),
        (payload(kind="candidate", seq=2), FAKE_KEY)])
    key = capture.FridaKeyCapture(config()).capture(snap, profile())
    assert key.source == "frida_observed_candidate_verified"
    assert FAKE_KEY.decode() not in repr(key)
    assert fake.session.script.unloaded and fake.session.detached
    with AndroidDatabase(snap.database_path, ACCOUNT, expected_wechat_version="8.0.49",
                         key_provider=key, cipher_profile=profile()) as database:
        assert database.get_messages("test") == []
        assert key.for_database(ACCOUNT, database.fingerprint, "8.0.49", profile()) == FAKE_KEY
        with pytest.raises(IdentityMismatch):
            key.for_database("someone_else", database.fingerprint, "8.0.49", profile())
    with pytest.raises(TypeError):
        pickle.dumps(key)
    assert FAKE_KEY.decode() not in str(capsys.readouterr())


def test_js_observer_contract_with_fake_java_bridge(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required only for the offline JS contract test")
    source = capture._source(config(), "n")
    harness = r'''
const vm = require('vm');
const source = SOURCE;
function run(options) {
  const events = [];
  let implementation = null, calls = 0;
  const originalResult = { marker: 'original' };
  const originalError = new Error('application-error');
  const overload = {returnType: {className:'com.tencent.wcdb.database.SQLiteDatabase'},
    call: function (...args) { calls++; if (options.throws) throw originalError; return originalResult; },
    set implementation(value) { implementation = value; }};
  const field = value => ({value});
  const cipherClass = {HMAC_SHA1:field(0), HMAC_SHA256:field(1), HMAC_SHA512:field(2)};
  const classes = {
    'android.app.ActivityThread':{currentApplication:()=>({getApplicationContext:()=>({
      getPackageName:()=>options.package || 'com.tencent.mm', getPackageManager:()=>({
        getPackageInfo:()=>({versionName:field(options.version || '8.0.49')})})})})},
    'android.app.Application':{getProcessName:()=> 'com.tencent.mm'},
    'android.os.Process':{myUid:()=>10001},
    'android.os.Build$VERSION':{SDK_INT:field(30)},
    'com.tencent.wcdb.database.SQLiteCipherSpec':cipherClass,
    'com.tencent.wcdb.database.SQLiteDatabase':{openDatabase:{overload:function(...args) {
      if (options.unknown) throw new Error('missing-signature');
      if (args.length !== 7 || args[1] !== '[B') throw new Error('wrong-overload');
      return overload;
    }}}
  };
  const context = {Java:{available:true, perform:f=>f(), use:name=>classes[name]},
    Frida:{version:'16.7.19'}, Uint8Array, send:(event,data)=> {
      // Frida send copies binary bytes before return; reflect that contract.
      events.push({event, data:data == null ? null : Array.from(new Uint8Array(data))});
      if (options.sendError && event.kind === 'candidate') throw new Error('transport');
    }};
  vm.runInNewContext(source, context);
  if (!implementation) return {events,calls};
  const password = [111,102,102,108,105,110,101];
  const spec = {cipher:field(null), pageSize:field(1024), kdfIteration:field(4000),
    hmacEnabled:field(false), hmacAlgorithm:field(0), kdfAlgorithm:field(0),cipherVersion:field(1)};
  const path = options.otherPath ? '/data/user/0/com.tencent.mm/MicroMsg/other/EnMicroMsg.db'
    : '/data/user/0/com.tencent.mm/MicroMsg/' + 'a'.repeat(32) + '/EnMicroMsg.db';
  let sameResult=false,sameError=false;
  try { sameResult=implementation.call({}, path,password,spec,null,0,null,0)===originalResult; }
  catch(error) { sameError=error===originalError; }
  return {events,calls,sameResult,sameError,password};
}
const normal=run({});
if (normal.calls!==1 || !normal.sameResult || normal.events.length!==2 ||
    normal.events[1].data.join(',')!=='111,102,102,108,105,110,101') throw new Error('normal');
const throwing=run({throws:true});
if (throwing.calls!==1 || !throwing.sameError || throwing.events.length!==1) throw new Error('throw');
const other=run({otherPath:true});
if (other.calls!==1 || !other.sameResult || other.events.length!==1) throw new Error('scope');
const transport=run({sendError:true});
if (transport.calls!==1 || !transport.sameResult) throw new Error('send');
for (const option of [{unknown:true},{version:'8.0.50'},{package:'other.app'}]) {
 const result=run(option);
 if (result.calls!==0 || result.events.length!==1 || result.events[0].event.kind!=='error')
   throw new Error('failclosed');
}
'''.replace("SOURCE", json.dumps(source))
    file = tmp_path / "offline_harness.js"
    file.write_text(harness)
    result = subprocess.run([node, str(file)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def modern_config(**changes):
    return config(hook_profile=capture.MODERN_HOOK_PROFILE, **changes)


def modern_payload(kind="ready", **changes):
    value = payload(kind=kind)
    value["payload"]["profile"] = capture.MODERN_HOOK_PROFILE
    if kind == "ready":
        value["payload"]["signature"] = list(capture._MODERN_SIGNATURE)
    else:
        value["payload"]["cipher"] = {"page_size": 1024, "compatibility": None}
    value["payload"].update(changes)
    return value


def test_profile_selection_is_explicit_and_never_fallbacks():
    with pytest.raises(UnsupportedFeature):
        config(hook_profile="auto")
    source = capture._source(modern_config(), "n")
    assert "database.setCipherKey.overload" in source
    assert "database.openDatabase.overload" not in source
    legacy_source = capture._source(config(), "n")
    assert "database.setCipherKey.overload" not in legacy_source


@pytest.mark.parametrize("compatibility", [None, 1])
def test_modern_inbox_does_not_guess_runtime_default_version(compatibility):
    inbox = capture._Inbox(modern_config(), profile(), "n", 30)
    inbox.on_message(modern_payload(), None)
    inbox.on_message(modern_payload(kind="candidate", cipher={
        "page_size": 1024, "compatibility": compatibility}), FAKE_KEY)
    assert inbox.failure is None
    assert len(inbox.candidates) == 1
    inbox.close()


@pytest.mark.parametrize("cipher", [
    {"page_size": 0, "compatibility": None},
    {"page_size": 4096, "compatibility": None},
    {"page_size": 1024, "compatibility": 4},
    {"page_size": 1024, "compatibility": True},
    {"page_size": 1024, "compatibility": "defaultVersion"},
    {"page_size": 1024},
    {"page_size": 1024, "compatibility": None, "kdf_iterations": 4000},
])
def test_modern_inbox_refuses_conflicting_unknown_or_extra_cipher_fields(cipher):
    inbox = capture._Inbox(modern_config(), profile(), "n", 30)
    inbox.on_message(modern_payload(), None)
    inbox.on_message(modern_payload(kind="candidate", cipher=cipher), FAKE_KEY)
    assert inbox.failure
    assert not inbox.candidates


def test_modern_profile_cannot_accept_legacy_ready_event():
    inbox = capture._Inbox(modern_config(), profile(), "n", 30)
    inbox.on_message(payload(), None)
    assert inbox.failure


def test_modern_real_sqlcipher_validation_not_setter_success(tmp_path, monkeypatch):
    snap = encrypted_snapshot(tmp_path)
    fake = install_fake(monkeypatch, [(modern_payload(), None),
        (modern_payload(kind="candidate"), b"wrong-but-setter-accepted"),
        (modern_payload(kind="candidate", seq=2), FAKE_KEY)])
    key = capture.FridaKeyCapture(modern_config()).capture(snap, profile())
    assert key.source == "frida_observed_candidate_verified"
    assert fake.session.script.unloaded and fake.session.detached
    with AndroidDatabase(snap.database_path, ACCOUNT, expected_wechat_version="8.0.49",
                         key_provider=key, cipher_profile=profile()) as database:
        assert database.validated_key is not None


@pytest.mark.parametrize("data", [b"\x00\xff\x01\x80", bytes(range(32)), bytes(range(40))])
@pytest.mark.parametrize("modern", [False, True])
def test_capture_binary_key_decrypts_real_fixture_exactly(tmp_path, monkeypatch, data, modern):
    snap = encrypted_snapshot(tmp_path, key=data)
    event_factory = modern_payload if modern else payload
    fake = install_fake(monkeypatch, [(event_factory(), None),
        (event_factory(kind="candidate"), b"not-the-correct-key"),
        (event_factory(kind="candidate", seq=2), data)])
    selected = modern_config() if modern else config()
    key = capture.FridaKeyCapture(selected).capture(snap, profile())
    assert fake.session.detached and fake.session.script.unloaded
    with AndroidDatabase(snap.database_path, ACCOUNT, expected_wechat_version="8.0.49",
                         key_provider=key, cipher_profile=profile()) as database:
        assert key.for_database(ACCOUNT, database.fingerprint, "8.0.49", profile()) == data
        assert database.get_messages("offline") == []


def test_modern_js_exact_object_path_and_original_setter_contract(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required only for offline JS contract testing")
    source = capture._source(modern_config(), "n")
    harness = r'''
const vm = require('vm');
const source = SOURCE;
function run(options) {
  const events=[],calls=[];
  let implementation=null, pathCalls=0;
  const originalError=new Error('application-error');
  const overload={returnType:{className:'void'},
    call:function(receiver,...args) {
      calls.push({receiver,args});
      if(options.throws) throw originalError;
      return undefined;
    }, set implementation(value) {implementation=value;}};
  const getPath={returnType:{className:'java.lang.String'}, call:function(receiver) {
    pathCalls++;
    if(options.pathError) throw new Error('path-error');
    return receiver.path;
  }};
  const field=value=>({value});
  const classes={
    'android.app.ActivityThread':{currentApplication:()=>({getApplicationContext:()=>({
      getPackageName:()=> 'com.tencent.mm',getPackageManager:()=>({
        getPackageInfo:()=>({versionName:field('8.0.49')})})})})},
    'android.app.Application':{getProcessName:()=> 'com.tencent.mm'},
    'android.os.Process':{myUid:()=>10001},
    'android.os.Build$VERSION':{SDK_INT:field(30)},
    'com.tencent.wcdb.core.Database':{
      setCipherKey:{overload:function(...args) {
        if(options.unknown) throw new Error('missing-signature');
        if(args.length!==3 || args[0]!=='[B' || args[2]!==
          'com.tencent.wcdb.core.Database$CipherVersion') throw new Error('wrong-signature');
        return overload;
      }},getPath:{overload:function(...args) {
        if(args.length!==0) throw new Error('wrong-getpath'); return getPath;
      }}}
  };
  vm.runInNewContext(source,{Java:{available:true,perform:f=>f(),use:name=>classes[name]},
    Frida:{version:'16.7.19'},Uint8Array,send:(event,data)=> {
      events.push({event,data:data==null?null:Array.from(new Uint8Array(data))});
      if(options.sendError && event.kind==='candidate') throw new Error('transport');
    }});
  if(!implementation) return {events,calls};
  const key=[0,-1,1,2];
  const version={name:()=>options.enumName||'defaultVersion'};
  const other={path:'/data/user/0/com.tencent.mm/MicroMsg/other/EnMicroMsg.db'};
  const authorized={path:'/data/user/0/com.tencent.mm/MicroMsg/'+'a'.repeat(32)+'/EnMicroMsg.db'};
  let resultPreserved=false,exceptionPreserved=false;
  if(options.otherFirst) implementation.call(other,[8,8,8],1024,version);
  try {resultPreserved=implementation.call(authorized,key,1024,version)===undefined;}
  catch(error) {exceptionPreserved=error===originalError;}
  const last=calls[calls.length-1];
  const argsPreserved=last.receiver===authorized && last.args[0]===key &&
    last.args[1]===1024 && last.args[2]===version;
  return {events,calls:calls.length,pathCalls,resultPreserved,exceptionPreserved,argsPreserved};
}
const normal=run({});
if(normal.calls!==1 || normal.pathCalls!==1 || !normal.resultPreserved || !normal.argsPreserved ||
  normal.events[1].data.join(',')!=='0,255,1,2' ||
  normal.events[1].event.cipher.compatibility!==null) throw new Error('normal');
const scoped=run({otherFirst:true});
if(scoped.calls!==2 || scoped.events.length!==2) throw new Error('first-unrelated-key');
const versioned=run({enumName:'version4'});
if(versioned.events[1].event.cipher.compatibility!==4) throw new Error('enum');
const throwing=run({throws:true});
if(throwing.calls!==1 || !throwing.exceptionPreserved || throwing.events.length!==1)
  throw new Error('original-exception');
for(const option of [{pathError:true},{enumName:'futureVersion'},{sendError:true}]) {
  const result=run(option);
  if(result.calls!==1 || !result.resultPreserved || !result.argsPreserved)
    throw new Error('observer-side-effect');
}
const unknown=run({unknown:true});
if(unknown.calls.length!==0 || unknown.events[0].event.kind!=='error') throw new Error('unknown');
'''.replace("SOURCE", json.dumps(source))
    file = tmp_path / "modern_harness.js"
    file.write_text(harness)
    result = subprocess.run([node, str(file)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not result.stdout


def test_capture_encrypted_wal_stages_private_copy_and_preserves_snapshot(tmp_path, monkeypatch):
    from sqlcipher3 import dbapi2 as cipher
    source, snapshot_dir = tmp_path / "source", tmp_path / "snapshot"
    source.mkdir()
    snapshot_dir.mkdir()
    encrypted_snapshot(source)
    connection = cipher.connect(source / "EnMicroMsg.db")
    connection.execute("PRAGMA hexkey = '" + FAKE_KEY.hex() + "'")
    for statement in profile().pragmas():
        connection.execute(statement)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("INSERT INTO message VALUES (1, 1, 'offline', 'WAL-only', 1, 1000, 0)")
    connection.commit()
    for file in source.iterdir():
        if file.name in capture._ALLOWED_FILES:
            shutil.copyfile(file, snapshot_dir / file.name)
    connection.close()
    snap = snapshot(snapshot_dir, contents=None)
    before = {file.name: file.read_bytes() for file in snapshot_dir.iterdir()}
    assert before["EnMicroMsg.db-wal"]
    fake = install_fake(monkeypatch, [(payload(), None),
        (payload(kind="candidate"), b"wrong-password"),
        (payload(kind="candidate", seq=2), FAKE_KEY)])
    actual_database = capture.AndroidDatabase
    temporary_paths = []

    def verify_on_copy(path, *args, **kwargs):
        temporary_paths.append(path)
        assert path.parent != snapshot_dir
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert all(file.stat().st_mode & 0o777 == 0o600 for file in path.parent.iterdir())
        db = actual_database(path, *args, **kwargs)
        assert db.get_messages("offline")[0].content == "WAL-only"
        return db

    monkeypatch.setattr(capture, "AndroidDatabase", verify_on_copy)
    key = capture.FridaKeyCapture(config()).capture(snap, profile())
    assert key.source == "frida_observed_candidate_verified"
    assert len(temporary_paths) == 2
    assert all(not path.parent.exists() for path in temporary_paths)
    assert {file.name: file.read_bytes() for file in snapshot_dir.iterdir()} == before
    assert fake.session.detached


def test_private_verification_copy_removed_on_failure(tmp_path, monkeypatch):
    snap = encrypted_snapshot(tmp_path)
    temporary = None
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with capture._verification_copy(snap, config()) as path:
            temporary = path.parent
            raise RuntimeError("synthetic failure")
    assert temporary is not None and not temporary.exists()


def spawn_config(**changes):
    values = dict(mode="spawn", consent_package_stop_relaunch=True,
                  consent_system_instrumentation=True)
    values.update(changes)
    return config(**values)


def install_spawn_fake(monkeypatch, actions, failure=None, app_pid=0, version=None, data_dir=None):
    from types import SimpleNamespace
    fake = install_fake(monkeypatch, actions)
    fake.apps = [SimpleNamespace(identifier=capture.PACKAGE, pid=app_pid,
        parameters={"version": version or config().expected_wechat_version,
                    "data-dir": data_dir or "/data/user/0/com.tencent.mm"})]
    fake.loaded = False
    fake.resume_attempts = 0
    normal_create = fake.session.create_script
    normal_detach = fake.session.detach

    def fail(stage):
        if failure == stage:
            raise RuntimeError("synthetic-private-error-" + FAKE_KEY.decode())

    def enumerate_applications(identifiers, scope):
        fake.calls.append(("applications", identifiers, scope))
        fail("preflight")
        return fake.apps

    def spawn(package, uid):
        fake.calls.append(("spawn", package, uid))
        fail("spawn")
        return 4242

    def create(source, name, runtime):
        fake.calls.append(("create_script",))
        fail("create")
        script = normal_create(source, name, runtime)
        normal_load, normal_unload = script.load, script.unload
        script.deferred_load = normal_load

        def load():
            fake.calls.append(("load",))
            fail("load")
            fake.loaded = True

        def unload():
            fake.calls.append(("unload",))
            fail("unload")
            normal_unload()

        script.load = load
        script.unload = unload
        return script

    def resume(pid):
        fake.calls.append(("resume", pid))
        fake.resume_attempts += 1
        assert pid == 4242
        if failure == "resume_once" and fake.resume_attempts == 1:
            raise RuntimeError(FAKE_KEY.decode())
        fail("resume_always")
        if fake.loaded:
            fake.session.script.deferred_load()

    def detach():
        fake.calls.append(("detach",))
        fail("detach")
        normal_detach()

    fake.enumerate_applications = enumerate_applications
    fake.spawn, fake.resume = spawn, resume
    fake.session.create_script, fake.session.detach = create, detach
    if failure == "attach":
        fake.attach_error = RuntimeError(FAKE_KEY.decode())
    return fake


@pytest.mark.parametrize("flag", ["consent_attach", "allow_unverified_profile",
    "consent_package_stop_relaunch", "consent_system_instrumentation"])
@pytest.mark.parametrize("value", [False, 1, "yes"])
def test_spawn_requires_each_separate_boolean_authorization(flag, value):
    with pytest.raises(PermissionDenied):
        spawn_config(**{flag: value})


def test_spawn_rejects_other_android_users_and_unknown_mode():
    with pytest.raises(UnsupportedFeature):
        spawn_config(android_user=10)
    with pytest.raises(ValidationError):
        config(mode="spawn-anything")


def test_spawn_warnings_available_without_runtime_access():
    observer = capture.FridaKeyCapture(spawn_config())
    assert "force-stop/relaunch" in observer.operation_notice
    assert "zygote/system_server" in observer.operation_notice
    assert "not atomic" in observer.operation_notice
    assert observer.last_capture is None
    assert capture.FridaKeyCapture(config()).config.mode == "attach"


@pytest.mark.parametrize("pid", [1, 300, True, None])
def test_spawn_refuses_existing_or_unknown_app_process(tmp_path, monkeypatch, pid):
    fake = install_spawn_fake(monkeypatch, [], app_pid=pid)
    with pytest.raises(PermissionDenied):
        capture.FridaKeyCapture(spawn_config()).capture(snapshot(tmp_path), profile())
    assert not any(call[0] in ("spawn", "attach", "resume") for call in fake.calls)


@pytest.mark.parametrize("kwargs", [{"version": "8.0.50"},
    {"data_dir": "/data/user/10/com.tencent.mm"}, {"data_dir": "/other"}])
def test_spawn_refuses_package_scope_before_relaunch(tmp_path, monkeypatch, kwargs):
    fake = install_spawn_fake(monkeypatch, [], **kwargs)
    with pytest.raises(IdentityMismatch):
        capture.FridaKeyCapture(spawn_config()).capture(snapshot(tmp_path), profile())
    assert not any(call[0] == "spawn" for call in fake.calls)


def test_spawn_load_resume_then_ready_and_verified_result(tmp_path, monkeypatch):
    snap = encrypted_snapshot(tmp_path)
    fake = install_spawn_fake(monkeypatch, [(payload(), None), (payload(kind="candidate"), FAKE_KEY)])
    observer = capture.FridaKeyCapture(spawn_config())
    key = observer.capture(snap, profile())
    operations = [call[0] for call in fake.calls]
    assert operations == ["device", "applications", "spawn", "attach", "create_script",
                          "load", "resume", "unload", "detach"]
    assert ("applications", [capture.PACKAGE], "metadata") in fake.calls
    assert ("spawn", capture.PACKAGE, 0) in fake.calls
    assert ("attach", 4242) in fake.calls
    assert key.source == "frida_observed_candidate_verified"
    receipt = observer.last_capture
    assert receipt.mode == "spawn" and receipt.package_stop_relaunch_requested
    assert receipt.spawned_process_resumed and receipt.observer_cleanup_confirmed
    assert receipt.live_compatibility == "unverified"
    assert FAKE_KEY.decode() not in repr(receipt)


@pytest.mark.parametrize("failure", ["preflight", "spawn", "attach", "create", "load",
    "resume_once", "resume_always", "unload", "detach"])
def test_spawn_failure_cleanup_resumes_known_pid_and_redacts(tmp_path, monkeypatch, capsys, failure):
    fake = install_spawn_fake(monkeypatch, [(payload(), None)], failure=failure)
    observer = capture.FridaKeyCapture(spawn_config())
    with pytest.raises((capture.CaptureUnavailable, OutcomeUnknown)) as error:
        observer.capture(snapshot(tmp_path), profile())
    assert FAKE_KEY.decode() not in str(error.value)
    operations = [call[0] for call in fake.calls]
    if failure not in ("preflight", "spawn"):
        assert "resume" in operations
    if failure not in ("preflight", "spawn", "attach"):
        assert "detach" in operations
    if failure in ("load", "resume_always", "resume_once", "unload", "detach"):
        assert "unload" in operations
    if failure in ("resume_always", "resume_once"):
        assert fake.resume_attempts == 2  # One bounded safety-resume cleanup attempt.
    else:
        assert fake.resume_attempts <= 1
    assert operations.count("spawn") <= 1
    assert observer.last_capture is None
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("profile_name", [capture.HOOK_PROFILE, capture.MODERN_HOOK_PROFILE])
def test_spawn_js_preapplication_identity_gate_without_guessing(tmp_path, profile_name):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required only for offline Java bridge contracts")
    selected = spawn_config(hook_profile=profile_name)
    source = capture._source(selected, "n")
    harness = r'''
const vm = require('vm');
const source = SOURCE;
function run(options) {
  const events=[],lookups=[];
  const field=value=>({value});
  const pm={getPackageInfo:(packageName,flags)=> {
    if(packageName!=='com.tencent.mm' || flags!==0) throw new Error('scope');
    return {versionName:field(options.version||'8.0.49'),
      applicationInfo:field({uid:field(options.uid===undefined?10001:options.uid)})};
  }};
  const context={getPackageManager:()=>pm,getPackageName:()=> 'com.tencent.mm'};
  const classes={
    'android.app.ActivityThread':{
      currentApplication:()=>null,
      currentPackageName:()=>options.package || 'com.tencent.mm',
      currentProcessName:()=>options.process || 'com.tencent.mm',
      currentActivityThread:()=>({getSystemContext:()=>{
        if(options.missingContext) throw new Error('missing-api'); return context;
      }})
    },
    'android.os.Process':{myUid:()=>10001},
    'android.os.Build$VERSION':{SDK_INT:field(options.api||30)},
    'com.tencent.wcdb.database.SQLiteCipherSpec':{},
    'com.tencent.wcdb.database.SQLiteDatabase':{openDatabase:{overload:()=>({
      returnType:{className:'com.tencent.wcdb.database.SQLiteDatabase'}})}},
    'com.tencent.wcdb.core.Database':{setCipherKey:{overload:()=>({returnType:{className:'void'}})},
      getPath:{overload:()=>({returnType:{className:'java.lang.String'}})}}
  };
  vm.runInNewContext(source,{Java:{available:true,perform:f=>f(),use:name=> {
      lookups.push(name); return classes[name];
    }},Frida:{version:'16.7.19'},send:event=>events.push(event)});
  return {events,lookups};
}
const valid=run({});
if(valid.events.length!==1 || valid.events[0].kind!=='ready') throw new Error('startup-ready');
for(const options of [{uid:10002},{uid:1010001},{api:29},{version:'8.0.50'},
    {package:'other.app'},{process:'com.tencent.mm:push'},{missingContext:true}]) {
  const result=run(options);
  if(result.events.length!==1 || result.events[0].kind!=='error' ||
      result.lookups.some(name=>name.indexOf('com.tencent.wcdb.')===0))
    throw new Error('unsafe-identity');
}
'''.replace("SOURCE", json.dumps(source))
    file = tmp_path / "startup_identity.js"
    file.write_text(harness)
    result = subprocess.run([node, str(file)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not result.stdout


def test_spawn_keyboard_interrupt_during_load_still_resumes_and_detaches(tmp_path, monkeypatch):
    fake = install_spawn_fake(monkeypatch, [])
    normal_create = fake.session.create_script

    def create(*args, **kwargs):
        script = normal_create(*args, **kwargs)
        def interrupted_load():
            raise KeyboardInterrupt()
        script.load = interrupted_load
        return script

    fake.session.create_script = create
    with pytest.raises(KeyboardInterrupt):
        capture.FridaKeyCapture(spawn_config()).capture(snapshot(tmp_path), profile())
    assert ("resume", 4242) in fake.calls
    assert fake.session.detached and fake.session.script.unloaded


def test_spawn_invalid_return_pid_is_uncertain_not_retried(tmp_path, monkeypatch):
    fake = install_spawn_fake(monkeypatch, [])
    fake.spawn = lambda *args, **kwargs: None
    with pytest.raises(OutcomeUnknown):
        capture.FridaKeyCapture(spawn_config()).capture(snapshot(tmp_path), profile())
    assert not any(call[0] in ("attach", "resume") for call in fake.calls)


def test_spawn_unknown_package_count_refused_without_relaunch(tmp_path, monkeypatch):
    fake = install_spawn_fake(monkeypatch, [])
    fake.apps = []
    with pytest.raises(IdentityMismatch):
        capture.FridaKeyCapture(spawn_config()).capture(snapshot(tmp_path), profile())
    assert not any(call[0] == "spawn" for call in fake.calls)


def test_long_binary_candidate_reports_supported_bound_without_material(tmp_path, monkeypatch):
    fake = install_fake(monkeypatch, [(payload(), None),
        (payload(kind="candidate"), b"\xff" * 41)])
    with pytest.raises(capture.CaptureUnavailable, match="long binary passwords are unsupported"):
        capture.FridaKeyCapture(config()).capture(snapshot(tmp_path), profile())
    assert fake.session.detached and fake.session.script.unloaded
