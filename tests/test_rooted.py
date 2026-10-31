"""真实 SQLCipher 合成数据库测试 + 假 ADB 传输测试；不是微信真机验收。"""
import dataclasses
import hashlib
import json
import pickle
import shlex
import shutil
import sqlite3

import pytest

from wechatauto_android.errors import (
    IdentityMismatch, PermissionDenied, StaleSnapshot, UnsupportedFeature, ValidationError,
)
from wechatauto_android.rooted import (
    AdbRootSnapshotter, AndroidDatabase, BoundDatabaseKey, CipherProfile, DatabaseOpenError,
    LegacyKeyCandidate, RootSnapshotConfig, UnsupportedSchema,
)


ACCOUNT = 'wxid_own'
ACCOUNT_DIR = 'a' * 32
VERSION = '8.0.28'
SECRET = "secret'key;not-sql"
PROFILE = CipherProfile.legacy_android()


def populate(connection, own=ACCOUNT):
    connection.executescript('''
        CREATE TABLE userinfo(id INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE message(msgId INTEGER PRIMARY KEY, msgSvrId INTEGER, talker TEXT,
            content TEXT, type INTEGER, createTime INTEGER, isSend INTEGER);
        CREATE TABLE rcontact(username TEXT PRIMARY KEY,nickname TEXT,conRemark TEXT,alias TEXT);
        CREATE TABLE chatroom(chatroomname TEXT PRIMARY KEY,memberlist TEXT,roomowner TEXT);
    ''')
    connection.execute('INSERT INTO userinfo VALUES(2,?)', (own,))
    connection.executemany('INSERT INTO message VALUES(?,?,?,?,?,?,?)', [
        (1, 123, 'friend', '你好 👋', 1, 1_700_000_000_000, 0),
        (2, 124, 'friend', 'same', 1, 1_700_000_000_001, 1),
        (3, 0, 'friend', 'same', 1, 1_700_000_000_001, 1),
        (4, 125, 'room@chatroom', 'wxid_member:\n群消息', 1, 1_700_000_000_002, 0),
        (5, 126, 'room@chatroom', 'no sender header', 49, 1_700_000_000_003, 0),
    ])
    connection.executemany('INSERT INTO rcontact VALUES(?,?,?,?)', [
        ('friend', '同名', 'Alice', 'alice'), ('friend2', '同名', '', '')])
    connection.execute('INSERT INTO chatroom VALUES(?,?,?)', ('room@chatroom', 'wxid_own;wxid_member;', 'wxid_own'))
    connection.commit()


def plaintext(tmp_path, own=ACCOUNT):
    path = tmp_path / 'EnMicroMsg.db'
    connection = sqlite3.connect(path)
    populate(connection, own)
    connection.close()
    return path


def encrypted(tmp_path, *, secret=SECRET, profile=PROFILE, own=ACCOUNT):
    from sqlcipher3 import dbapi2 as cipher
    path = tmp_path / 'EnMicroMsg.db'
    connection = cipher.connect(path)
    if type(secret) is bytes:
        assert 1 <= len(secret) <= 40
        connection.execute("PRAGMA hexkey = '" + secret.hex() + "'")
    else:
        connection.execute("PRAGMA key = '" + secret.replace("'", "''") + "'")
    for statement in profile.pragmas():
        connection.execute(statement)
    populate(connection, own)
    connection.close()
    assert path.read_bytes()[:16] != b'SQLite format 3\0'
    return path


def open_db(path, **kw):
    return AndroidDatabase(path, ACCOUNT, expected_wechat_version=VERSION, **kw)


def test_plaintext_known_native_schema(tmp_path):
    path = plaintext(tmp_path)
    with open_db(path) as db:
        assert db.capabilities['history_scope'] == 'validated_snapshot_only'
        assert db.capabilities['schema_self_id_matches']
        assert not db.capabilities['wechat_version_device_verified']
        assert db.capabilities['contacts'] and db.capabilities['groups']
        assert [m.id for m in db.get_messages('friend')] == ['3', '2', '1']
        assert [m.id for m in db.get_messages_after('friend', after=(1, '1'))] == ['2', '3']
        assert len(db.search_contact('同名')) == 2
        assert db.get_group_members('room@chatroom')[0]['is_owner']
        assert db.get_groups()[0]['id'] == 'room@chatroom'
        assert all(row['unread'] is None for row in db.get_sessions())
        rows = db.get_messages('room@chatroom')
        assert rows[0].sender_id == ''
        assert rows[1].sender_id == 'wxid_member'
        assert rows[1].content == '群消息'
        assert rows[1].origin == 'operator_supplied_android_snapshot'
        assert db.get_messages('friend')[0].sender_id == ACCOUNT
    with pytest.raises(DatabaseOpenError):
        db.get_messages('friend')


def test_real_encrypted_fixture_and_bound_key(tmp_path):
    path = encrypted(tmp_path)
    with open_db(path, key_provider=lambda: SECRET, cipher_profile=PROFILE) as db:
        assert len(db.get_messages('friend')) == 3
        key = db.validated_key
        assert isinstance(key, BoundDatabaseKey)
        assert SECRET not in repr(key) + repr(db) + json.dumps(db.capabilities)
        for operation in [lambda: pickle.dumps(key), lambda: dataclasses.asdict(key), lambda: json.dumps(key)]:
            with pytest.raises(TypeError):
                operation()
    with open_db(path, key_provider=key, cipher_profile=PROFILE) as db:
        assert db.get_messages('friend')[-1].content == '你好 👋'
    with pytest.raises(IdentityMismatch):
        AndroidDatabase(path, 'different', expected_wechat_version=VERSION, key_provider=key, cipher_profile=PROFILE)
    with pytest.raises(IdentityMismatch):
        AndroidDatabase(path, ACCOUNT, expected_wechat_version='8.0.29', key_provider=key, cipher_profile=PROFILE)
    with pytest.raises(IdentityMismatch):
        open_db(path, key_provider=key, cipher_profile=CipherProfile(4, 4096))
    data = bytearray(path.read_bytes())
    data[-50] ^= 1
    path.write_bytes(data)
    with pytest.raises(IdentityMismatch):
        open_db(path, key_provider=key, cipher_profile=PROFILE)


@pytest.mark.parametrize('failure', ['wrong_key', 'wrong_cipher', 'truncated', 'provider_error'])
def test_encrypted_rejections_never_echo_secret(tmp_path, capfd, failure):
    path = encrypted(tmp_path)
    def provider():
        return SECRET
    profile = PROFILE
    if failure == 'wrong_key':
        def provider():
            return SECRET + 'wrong'
    elif failure == 'wrong_cipher':
        profile = CipherProfile(4, 4096)
    elif failure == 'truncated':
        path.write_bytes(path.read_bytes()[:1500])
    else:
        def provider():
            raise RuntimeError(SECRET)
    with pytest.raises(DatabaseOpenError) as captured:
        open_db(path, key_provider=provider, cipher_profile=profile)
    assert SECRET not in str(captured.value)
    assert SECRET not in repr(captured.value)
    streams = capfd.readouterr()
    assert SECRET not in streams.out + streams.err


@pytest.mark.parametrize('secret', [b'\x00\xff\x01\x02\x03\x04\x05\x06', bytes(range(32)), bytes(range(40))])
def test_binary_passphrase_exact_bytes_roundtrip_binding_and_redaction(tmp_path, capfd, secret):
    path = encrypted(tmp_path, secret=secret)
    with open_db(path, key_provider=lambda: secret, cipher_profile=PROFILE) as database:
        assert database.get_messages('friend')[-1].content == '你好 👋'
        key = database.validated_key
        fingerprint = database.fingerprint
        assert key.for_database(ACCOUNT, fingerprint, VERSION, PROFILE) == secret
        public = repr(key) + repr(database) + json.dumps(database.capabilities)
        assert secret.hex() not in public and repr(secret) not in public
        with pytest.raises(TypeError):
            pickle.dumps(key)
    with open_db(path, key_provider=key, cipher_profile=PROFILE) as database:
        assert len(database.get_messages('friend')) == 3
    with pytest.raises(IdentityMismatch):
        key.for_database('another-account', fingerprint, VERSION, PROFILE)
    with pytest.raises(IdentityMismatch):
        key.for_database(ACCOUNT, '0' * 64, VERSION, PROFILE)
    wrong = secret[:-1] + bytes([secret[-1] ^ 1])
    with pytest.raises(DatabaseOpenError) as error:
        open_db(path, key_provider=lambda: wrong, cipher_profile=PROFILE)
    streams = capfd.readouterr()
    public = repr(error.value) + streams.out + streams.err
    assert secret.hex() not in public and wrong.hex() not in public
    assert repr(secret) not in public and repr(wrong) not in public


def test_binary_hexkey_preserves_password_kdf_not_raw_key(tmp_path):
    # A 32-byte binary input still uses the password KDF. The equivalent text
    # password opens it; treating those bytes as a raw AES key would not.
    secret = b'synthetic-passphrase-with32bytes'
    assert len(secret) == 32
    path = encrypted(tmp_path, secret=secret)
    with open_db(path, key_provider=lambda: secret.decode('ascii'), cipher_profile=PROFILE) as database:
        assert len(database.get_messages('friend')) == 3


@pytest.mark.parametrize('invalid', [b'', bytes(range(40)) + b'x', b'x' * 4097, bytearray(b'key')])
def test_binary_passphrase_rejects_empty_mutable_or_silently_truncated_material(tmp_path, invalid):
    # The first 40 bytes are a real working key: an unbounded hexkey call would
    # silently accept the 41-byte input by discarding its last byte.
    path = encrypted(tmp_path, secret=bytes(range(40)))
    with pytest.raises(DatabaseOpenError):
        open_db(path, key_provider=lambda: invalid, cipher_profile=PROFILE)


def test_key_provenance_is_allowlisted_not_arbitrary_material():
    with pytest.raises(ValidationError) as error:
        BoundDatabaseKey(ACCOUNT, 'a' * 64, VERSION, PROFILE, SECRET, source=SECRET)
    assert SECRET not in str(error.value)


def test_native_self_identity_verified_not_directory_assertion(tmp_path):
    path = encrypted(tmp_path, own='wxid_other')
    with pytest.raises(IdentityMismatch):
        open_db(path, key_provider=lambda: SECRET, cipher_profile=PROFILE)


def test_legacy_candidate_never_implies_universal_acquisition(tmp_path):
    candidate = LegacyKeyCandidate(VERSION, 'explicit-device-id', '123456')
    secret = candidate()
    assert secret == hashlib.md5(b'explicit-device-id123456').hexdigest()[:7]
    assert secret not in repr(candidate) and 'explicit-device-id' not in repr(candidate)
    with pytest.raises(TypeError):
        pickle.dumps(candidate)
    path = encrypted(tmp_path, secret=secret)
    with open_db(path, key_provider=candidate, cipher_profile=PROFILE) as db:
        assert db.validated_key.source == 'legacy_derived_candidate_verified'
    with pytest.raises(IdentityMismatch):
        AndroidDatabase(path, ACCOUNT, expected_wechat_version='8.0.79', key_provider=candidate, cipher_profile=PROFILE)


def test_unknown_schema_and_missing_key_fail_explicitly(tmp_path):
    path = tmp_path / 'EnMicroMsg.db'
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE message_other(body TEXT)')
    connection.close()
    with pytest.raises(UnsupportedSchema):
        open_db(path)
    path.unlink()
    encrypted(tmp_path)
    with pytest.raises(DatabaseOpenError):
        open_db(path)
    with pytest.raises(ValidationError):
        open_db(path, key_provider=lambda: SECRET)


def test_real_encrypted_wal_snapshot_reads_uncheckpointed_message(tmp_path):
    from sqlcipher3 import dbapi2 as cipher
    source = tmp_path / 'source'
    source.mkdir()
    source_db = source / 'EnMicroMsg.db'
    connection = cipher.connect(source_db)
    connection.execute("PRAGMA key = '" + SECRET.replace("'", "''") + "'")
    for statement in PROFILE.pragmas():
        connection.execute(statement)
    populate(connection)
    assert connection.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    connection.execute('PRAGMA wal_autocheckpoint=0')
    connection.execute('INSERT INTO message VALUES(?,?,?,?,?,?,?)',
                       (6, 200, 'friend', 'WAL only', 1, 1_700_000_001_000, 0))
    connection.commit()
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    for source_file in source.iterdir():
        shutil.copyfile(source_file, snapshot / source_file.name)
    assert (snapshot / 'EnMicroMsg.db-wal').stat().st_size > 0
    connection.close()
    with open_db(snapshot / 'EnMicroMsg.db', key_provider=lambda: SECRET, cipher_profile=PROFILE) as db:
        assert db.get_messages('friend')[0].content == 'WAL only'


@pytest.mark.parametrize('suffix', ['', '-wal', '-shm', '-journal'])
def test_local_bundle_symlink_rejected(tmp_path, suffix):
    source = tmp_path / 'original'
    source.mkdir()
    path = plaintext(source)
    local = tmp_path / 'local'
    local.mkdir()
    target = local / 'EnMicroMsg.db'
    if suffix:
        shutil.copyfile(path, target)
    target.with_name(target.name + suffix).symlink_to(path)
    with pytest.raises(ValidationError):
        open_db(target)


def test_nonempty_rollback_journal_rejected(tmp_path):
    path = plaintext(tmp_path)
    path.with_name(path.name + '-journal').write_bytes(b'hot')
    with pytest.raises(StaleSnapshot):
        open_db(path)


def config(**kw):
    values = dict(serial='test-serial', account_dir=ACCOUNT_DIR, account_id=ACCOUNT,
                  expected_wechat_version=VERSION, consent_root_read=True, confirm_quiesced=True)
    values.update(kw)
    return RootSnapshotConfig(**values)


@pytest.mark.parametrize('kw', [
    {'serial': 'x;id'}, {'serial': '-x'}, {'serial': 'x y'}, {'account_dir': '../etc'},
    {'account_dir': 'A'*32}, {'account_dir': 'x'*32}, {'android_user': True},
    {'android_user': -1}, {'android_user': '0;id'}, {'expected_wechat_version': '8.0+'},
    {'expected_wechat_version': '7.0.0'}, {'consent_root_read': False},
    {'confirm_quiesced': False}, {'confirm_quiesced': 'yes'}, {'timeout_seconds': 0},
])
def test_config_validation_before_device_access(kw):
    with pytest.raises((ValidationError, PermissionDenied)):
        config(**kw)


class FakeSnapshotter(AdbRootSnapshotter):
    def __init__(self, config, *, active=False, sdk='30', version=VERSION, changed=False, oversized=False):
        super().__init__(config)
        self.files = {'EnMicroMsg.db': b'database bytes', 'EnMicroMsg.db-wal': b'wal bytes'}
        self.active, self.sdk, self.version, self.changed, self.oversized = active, sdk, version, changed, oversized
        self.calls = []
        self.manifests = 0

    def _small(self, args):
        self.calls.append(args)
        if args == ['get-state']:
            return 'device'
        if args == ['shell', 'getprop', 'ro.build.version.sdk']:
            return self.sdk
        if args == ['shell', 'dumpsys', 'package', 'com.tencent.mm']:
            return 'versionName=' + self.version
        script = shlex.split(args[-1])[0]
        if script == 'id -u':
            return '0'
        if script == 'ps -A -o NAME':
            return 'NAME\ninit\n' + ('com.tencent.mm:push' if self.active else '')
        assert 'sha256sum' in script
        assert '[ ! -L "$p" ]' in script and 'readlink -f' in script
        self.manifests += 1
        rows = []
        for name, content in self.files.items():
            size = self.config.max_file_bytes + 1 if self.oversized else len(content)
            sha = hashlib.sha256(content).hexdigest()
            if self.changed and self.manifests > 1:
                sha = 'f' * 64
            rows.append(f'{name}\t{size}\t{sha}')
        return '\n'.join(rows)

    def _run(self, args, *, output=None, limit=1024*1024):
        self.calls.append(args)
        assert output is not None
        script = shlex.split(args[-1])[0]
        assert 'cat /data/user/0/com.tencent.mm/MicroMsg/' + ACCOUNT_DIR in script
        assert '[ ! -L ' in script
        content = self.files[output.name]
        assert len(content) <= limit
        output.write_bytes(content)
        output.chmod(0o600)
        return b''


def test_snapshot_captures_complete_coherent_bundle_and_private_files(tmp_path):
    snapshotter = FakeSnapshotter(config())
    result = snapshotter.capture(tmp_path / 'new')
    assert result.database_path.read_bytes() == b'database bytes'
    assert (result.directory / 'EnMicroMsg.db-wal').read_bytes() == b'wal bytes'
    assert result.manifest['account_identity_verified'] is False
    assert result.manifest['asserted_account_id'] == ACCOUNT
    assert set(result.manifest['files']) == set(snapshotter.files)
    assert result.directory.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o077 == 0 for path in result.directory.iterdir())
    assert len([call for call in snapshotter.calls if 'ps -A' in str(call)]) == 2
    assert not any(token in str(snapshotter.calls) for token in ['force-stop', 'kill ', 'adb root', 'chmod ', 'setenforce'])


@pytest.mark.parametrize('options,error', [
    ({'active': True}, PermissionDenied), ({'sdk': '29'}, UnsupportedFeature),
    ({'version': '8.0.79'}, IdentityMismatch), ({'changed': True}, StaleSnapshot),
    ({'oversized': True}, ValidationError),
])
def test_snapshot_refuses_running_changed_unknown_or_oversized_state(tmp_path, options, error):
    snapshotter = FakeSnapshotter(config(), **options)
    with pytest.raises(error):
        snapshotter.capture(tmp_path / 'new')
    assert not (tmp_path / 'new').exists()
    assert not list(tmp_path.glob('.wechat-snapshot-*'))


def test_snapshot_never_overwrites_or_uses_symlink_destination(tmp_path):
    snapshotter = FakeSnapshotter(config())
    existing = tmp_path / 'existing'
    existing.mkdir()
    with pytest.raises(ValidationError):
        snapshotter.capture(existing)
    linked = tmp_path / 'linked'
    linked.symlink_to(existing, target_is_directory=True)
    with pytest.raises(ValidationError):
        snapshotter.capture(linked / 'child')
    assert snapshotter.calls == []


def test_cipher_profile_rejects_injection():
    with pytest.raises(ValidationError):
        CipherProfile(1, 1024, kdf_algorithm='SHA1;DROP TABLE message')
    with pytest.raises(ValidationError):
        CipherProfile(True, 1024)


def test_sql_query_input_is_parameterized(tmp_path):
    path = plaintext(tmp_path)
    with open_db(path) as db:
        assert db.get_messages("friend' OR 1=1 --") == []
        assert db.search_contact("' OR 1=1 --") == []
        assert len(db.get_messages('friend')) == 3
        with pytest.raises(ValidationError):
            db.get_messages('friend', limit=-1)
