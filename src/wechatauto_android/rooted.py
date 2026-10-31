"""已获授权、已 root 且已静止设备的只读快照；明确配置的 Android WCDB 读取。

不会 root/刷机/停止应用，不注入进程，不把兼容性目标当作真机验证。
只支持经结构验证的 EnMicroMsg message/rcontact/chatroom/userinfo 模式。
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .errors import (
    DeviceUnavailable, IdentityMismatch, PermissionDenied, StaleSnapshot,
    UnsupportedFeature, ValidationError, WeChatError,
)
from .models import Message

_PACKAGE = "com.tencent.mm"
_DATABASES = ("EnMicroMsg.db", "SnsMicroMsg.db")
_FILES = tuple(name + suffix for name in _DATABASES for suffix in ("", "-wal", "-shm", "-journal"))
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?\Z")


class DatabaseOpenError(WeChatError):
    """密钥、加密配置、完整性或格式不匹配；消息不包含密钥。"""


class UnsupportedSchema(UnsupportedFeature):
    """未识别的真实 Android 数据库结构；不静默变成空结果。"""


def _text(value, label, maximum=256):
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise ValidationError(f"invalid {label}")
    return value


def _integer(value, label, low=0, high=2**63 - 1):
    if type(value) is not int or not low <= value <= high:
        raise ValidationError(f"invalid {label}")
    return value


def _version(value):
    if not isinstance(value, str) or not _VERSION.fullmatch(value) or int(value.split('.')[0]) < 8:
        raise ValidationError("an exact WeChat version >= 8.0.0 is required")
    return value


def _safe_local(path: str | Path, *, directory=False) -> Path:
    path = Path(path).absolute()
    if path.resolve() != path or path.is_symlink():
        raise ValidationError("symlink or non-canonical local path is not allowed")
    if directory:
        if not path.is_dir():
            raise ValidationError("local directory must already exist")
    elif not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise ValidationError("database input must be an existing regular file")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class RootSnapshotConfig:
    serial: str
    account_dir: str
    account_id: str
    expected_wechat_version: str
    android_user: int = 0
    consent_root_read: bool = False
    confirm_quiesced: bool = False
    max_file_bytes: int = 2 * 1024**3
    max_total_bytes: int = 4 * 1024**3
    timeout_seconds: int = 120

    def __post_init__(self):
        if not isinstance(self.serial, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', self.serial):
            raise ValidationError("invalid explicit device serial")
        if not isinstance(self.account_dir, str) or not re.fullmatch(r'[a-f0-9]{32}', self.account_dir):
            raise ValidationError("account_dir must be exactly 32 lowercase hexadecimal characters")
        _text(self.account_id, "account_id")
        _version(self.expected_wechat_version)
        _integer(self.android_user, 'android_user', 0, 9999)
        _integer(self.max_file_bytes, 'max_file_bytes', 4096, 16 * 1024**3)
        _integer(self.max_total_bytes, 'max_total_bytes', self.max_file_bytes, 32 * 1024**3)
        _integer(self.timeout_seconds, 'timeout_seconds', 1, 3600)
        if self.consent_root_read is not True or self.confirm_quiesced is not True:
            raise PermissionDenied("explicit device/account root-read consent and quiescence confirmation required")

    @property
    def remote_dir(self):
        return f'/data/user/{self.android_user}/{_PACKAGE}/MicroMsg/{self.account_dir}'


@dataclass(frozen=True)
class RootSnapshot:
    directory: Path
    manifest: dict

    @property
    def database_path(self):
        return self.directory / 'EnMicroMsg.db'


class AdbRootSnapshotter:
    """只执行固定命令。构造与捕获均验证授权，失败不保留半成品。

    confirm_quiesced 是用户对静止状态的承诺，不是工具主动停止微信的授权。
    两次进程检查和所有源文件哈希相等是补充证据，不冒充原子在线备份。
    """

    def __init__(self, config: RootSnapshotConfig, adb: str = 'adb'):
        if not isinstance(config, RootSnapshotConfig):
            raise ValidationError("RootSnapshotConfig required")
        config.__post_init__()
        self.config = config
        self.adb = _text(adb, 'adb executable', 4096)

    def _run(self, args: list[str], *, output: Path | None = None, limit=1024 * 1024) -> bytes:
        """输出流、stderr 和执行时间均有上限；不回显远端错误内容。"""
        try:
            proc = subprocess.Popen([self.adb, '-s', self.config.serial, *args],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, shell=False)
        except OSError:
            raise DeviceUnavailable("cannot start configured adb executable") from None
        chunks = []
        failures = []

        def consume(stream, bound, destination=None, collect=False):
            size = 0
            try:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > bound:
                        failures.append('bound')
                        proc.kill()
                        return
                    if destination is not None:
                        destination.write(chunk)
                    elif collect:
                        chunks.append(chunk)
            except (OSError, ValueError):
                failures.append('io')
                proc.kill()
            finally:
                stream.close()

        target = None
        try:
            if output is not None:
                fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                target = os.fdopen(fd, 'wb')
            threads = [threading.Thread(target=consume,
                        args=(proc.stdout, limit, target, output is None), daemon=True),
                       threading.Thread(target=consume, args=(proc.stderr, 65536), daemon=True)]
            for thread in threads:
                thread.start()
            try:
                code = proc.wait(timeout=self.config.timeout_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise DeviceUnavailable("adb snapshot operation timed out") from None
            finally:
                for thread in threads:
                    thread.join(timeout=3)
            if failures or any(thread.is_alive() for thread in threads):
                raise DeviceUnavailable("adb snapshot output exceeded limit or was interrupted")
            if code != 0:
                raise DeviceUnavailable("adb/root read failed; check device, root grant and file access")
            return b''.join(chunks)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if target is not None:
                target.close()

    def _small(self, args: list[str]) -> str:
        try:
            return self._run(args).decode('utf-8', errors='strict').strip()
        except UnicodeError:
            raise DeviceUnavailable("device returned invalid text") from None

    @staticmethod
    def _root(script: str) -> list[str]:
        # adb 会把参数重新拼接成远端命令；quote 保证 su -c 后仍是一个参数。
        return ['exec-out', 'su', '-c', shlex.quote(script)]

    def _verify_device(self):
        if self._small(['get-state']) != 'device':
            raise DeviceUnavailable("explicit device is not online")
        sdk = self._small(['shell', 'getprop', 'ro.build.version.sdk'])
        if not sdk.isdigit() or int(sdk) < 30:
            raise UnsupportedFeature("root snapshot target requires Android API >= 30")
        package = self._small(['shell', 'dumpsys', 'package', _PACKAGE])
        versions = set(re.findall(r'\bversionName=([0-9.]+)', package))
        if versions != {self.config.expected_wechat_version}:
            raise IdentityMismatch("installed WeChat version does not match explicit version")
        if self._small(self._root('id -u')) != '0':
            raise PermissionDenied("already-rooted device with an explicit su grant is required")
        return int(sdk)

    def _quiescent(self):
        text = self._small(self._root('ps -A -o NAME'))
        rows = [line.strip() for line in text.splitlines() if line.strip()]
        if not rows or rows[0] != 'NAME':
            raise DeviceUnavailable("cannot verify application process quiescence")
        if any(row == _PACKAGE or row.startswith(_PACKAGE + ':') for row in rows[1:]):
            raise PermissionDenied("WeChat is running; quiesce it explicitly outside this tool")

    def _manifest(self):
        remote = self.config.remote_dir
        canonical = [remote]
        if self.config.android_user == 0:
            canonical.append(f'/data/data/{_PACKAGE}/MicroMsg/{self.config.account_dir}')
        choices = '|'.join(shlex.quote(value) for value in canonical)
        names = ' '.join(shlex.quote(name) for name in _FILES)
        script = f'''set -eu
 d={shlex.quote(remote)}
 [ -d "$d" ] && [ ! -L "$d" ] || exit 40
 r=$(readlink -f "$d")
 case "$r" in {choices}) ;; *) exit 41;; esac
 for f in {names}; do
  p="$d/$f"
  [ ! -L "$p" ] || exit 42
  if [ -e "$p" ]; then
   [ -f "$p" ] || exit 43
   z=$(stat -c %s "$p")
   h=$(sha256sum "$p"); h=${{h%% *}}
   printf '%s\\t%s\\t%s\\n' "$f" "$z" "$h"
  fi
 done'''
        result = {}
        total = 0
        for line in self._small(self._root(script)).splitlines():
            parts = line.split('\t')
            if len(parts) != 3 or parts[0] not in _FILES or parts[0] in result:
                raise ValidationError("invalid remote file manifest")
            name, size, sha = parts
            if not size.isdigit() or not re.fullmatch(r'[0-9a-f]{64}', sha):
                raise ValidationError("invalid remote file metadata")
            number = int(size)
            if number > self.config.max_file_bytes:
                raise ValidationError("snapshot file exceeds configured bound")
            if name.endswith('-journal') and number:
                raise StaleSnapshot("rollback journal present; a verified quiescent snapshot is required")
            total += number
            result[name] = {'size': number, 'sha256': sha}
        if total > self.config.max_total_bytes or not result.get('EnMicroMsg.db', {}).get('size'):
            raise ValidationError("snapshot missing main database or exceeds total size bound")
        for name in result:
            if name.endswith(('-wal', '-shm', '-journal')) and name.rsplit('-', 1)[0] not in result:
                raise StaleSnapshot("orphan database sidecar")
        return result

    def capture(self, output_dir: str | Path) -> RootSnapshot:
        self.config.__post_init__()
        destination = Path(output_dir).absolute()
        parent = _safe_local(destination.parent, directory=True)
        if destination.exists() or destination.is_symlink():
            raise ValidationError("snapshot destination must not already exist")
        sdk = self._verify_device()
        self._quiescent()
        before = self._manifest()
        temporary = Path(tempfile.mkdtemp(prefix='.wechat-snapshot-', dir=parent))
        os.chmod(temporary, 0o700)
        try:
            for name, record in before.items():
                remote = f'{self.config.remote_dir}/{name}'
                script = f'[ ! -L {shlex.quote(remote)} ] && [ -f {shlex.quote(remote)} ] && cat {shlex.quote(remote)}'
                target = temporary / name
                self._run(self._root(script), output=target, limit=record['size'])
                if target.stat().st_size != record['size'] or _sha256(target) != record['sha256']:
                    raise StaleSnapshot("copied database bundle differs from its manifest")
            self._quiescent()
            after = self._manifest()
            if after != before:
                raise StaleSnapshot("database bundle changed while capturing")
            if self._verify_device() != sdk:
                raise IdentityMismatch("Android API changed while capturing the snapshot")
            manifest = {
                'schema_version': 1, 'kind': 'android_rooted_quiesced_snapshot',
                'serial': self.config.serial, 'android_user': self.config.android_user,
                'android_api': sdk, 'package': _PACKAGE,
                'wechat_version': self.config.expected_wechat_version,
                'account_dir': self.config.account_dir, 'asserted_account_id': self.config.account_id,
                'account_identity_verified': False, 'files': before,
                'consistency': 'user_quiescence_assertion_process_absence_and_unchanged_hashes',
                'captured_at': datetime.now(timezone.utc).isoformat(),
            }
            manifest_path = temporary / 'manifest.json'
            fd = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
            # mkdir 是独占创建；避免 rename 在竞争条件下覆盖别人新建的空目录。
            try:
                destination.mkdir(mode=0o700)
            except FileExistsError:
                raise ValidationError('snapshot destination appeared during capture') from None
            try:
                for name in before:
                    (temporary / name).rename(destination / name)
                # 清单最后出现；没有清单的目录不能视作完成快照。
                manifest_path.rename(destination / 'manifest.json')
                return RootSnapshot(destination, manifest)
            except BaseException:
                shutil.rmtree(destination)
                raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


@dataclass(frozen=True)
class CipherProfile:
    """由使用者/实测记录明确指定；名称不代表任意 8.x 已验证。"""
    compatibility: int
    page_size: int
    kdf_iterations: int | None = None
    use_hmac: bool | None = None
    kdf_algorithm: str | None = None
    hmac_algorithm: str | None = None

    def __post_init__(self):
        _integer(self.compatibility, 'cipher compatibility', 1, 4)
        if type(self.page_size) is not int or self.page_size not in (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536):
            raise ValidationError("invalid cipher page size")
        if self.kdf_iterations is not None:
            _integer(self.kdf_iterations, 'KDF iterations', 1, 2_000_000)
        if self.use_hmac is not None and type(self.use_hmac) is not bool:
            raise ValidationError("invalid cipher HMAC flag")
        if self.kdf_algorithm not in (None, 'PBKDF2_HMAC_SHA1', 'PBKDF2_HMAC_SHA256', 'PBKDF2_HMAC_SHA512'):
            raise ValidationError("invalid cipher KDF algorithm")
        if self.hmac_algorithm not in (None, 'HMAC_SHA1', 'HMAC_SHA256', 'HMAC_SHA512'):
            raise ValidationError("invalid cipher HMAC algorithm")

    @classmethod
    def legacy_android(cls):
        return cls(compatibility=1, page_size=1024, kdf_iterations=4000, use_hmac=False,
                   kdf_algorithm='PBKDF2_HMAC_SHA1', hmac_algorithm='HMAC_SHA1')

    def pragmas(self):
        values = [('cipher_compatibility', self.compatibility), ('cipher_page_size', self.page_size)]
        for name, value in [('kdf_iter', self.kdf_iterations), ('cipher_use_hmac', self.use_hmac),
                            ('cipher_kdf_algorithm', self.kdf_algorithm), ('cipher_hmac_algorithm', self.hmac_algorithm)]:
            if value is not None:
                values.append((name, int(value) if type(value) is bool else value))
        return tuple(f'PRAGMA {name} = {value}' for name, value in values)


class LegacyKeyCandidate:
    """历史派生法候选；仅显式版本，必须经实际数据库验证，不读取任何凭据。"""
    __slots__ = ('wechat_version', '_device_id', '_uin')

    def __init__(self, wechat_version: str, device_id: str, uin: str):
        object.__setattr__(self, 'wechat_version', _version(wechat_version))
        object.__setattr__(self, '_device_id', _text(device_id, 'device identifier', 128))
        if not isinstance(uin, str) or not re.fullmatch(r'-?[0-9]{1,20}', uin):
            raise ValidationError("invalid explicit UIN")
        object.__setattr__(self, '_uin', uin)

    def __setattr__(self, name, value):
        raise AttributeError('key candidate is immutable')

    def __call__(self):
        return hashlib.md5((self._device_id + self._uin).encode('utf-8')).hexdigest()[:7]

    def __repr__(self):
        return '<LegacyKeyCandidate material=redacted unverified=True>'

    def __getstate__(self):
        raise TypeError("database key providers cannot be serialized")


class BoundDatabaseKey:
    """仅内存的已验证口令；绑定账号/数据包指纹/版本/加密配置，不支持序列化。"""
    __slots__ = ('account_id', 'database_fingerprint', 'wechat_version', 'cipher_profile',
                 '_passphrase', 'source')

    def __init__(self, account_id: str, database_fingerprint: str, wechat_version: str,
                 cipher_profile: CipherProfile, passphrase: str | bytes,
                 source: str = 'operator_supplied_verified'):
        if source not in ('operator_supplied_verified', 'legacy_derived_candidate_verified',
                          'frida_observed_candidate_verified'):
            raise ValidationError('invalid database key provenance')
        for name, value in [('account_id', account_id), ('database_fingerprint', database_fingerprint),
                            ('wechat_version', wechat_version), ('cipher_profile', cipher_profile),
                            ('_passphrase', passphrase), ('source', source)]:
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        raise AttributeError('validated key binding is immutable')

    def for_database(self, account_id, fingerprint, version, profile):
        if (account_id, fingerprint, version, profile) != (
                self.account_id, self.database_fingerprint, self.wechat_version, self.cipher_profile):
            raise IdentityMismatch("validated database key is bound to a different account, bundle, version or cipher")
        return self._passphrase

    def __repr__(self):
        return '<BoundDatabaseKey material=redacted>'

    def __getstate__(self):
        raise TypeError("database keys cannot be serialized")


class AndroidDatabase:
    """只读 EnMicroMsg 快照；不接受规范化档案冒充数据库，不建立实时轮询。

    known schema 只承诺由当前文件实测存在的列，不承诺整个 WeChat 8.x 系列。
    userinfo(id=2) 必须与 account_id 一致。SQLCipher 仅在内存接收密钥。
    """
    REQUIRED = {
        'message': {'msgId', 'msgSvrId', 'talker', 'content', 'type', 'createTime', 'isSend'},
        'userinfo': {'id', 'value'},
    }
    TYPE_NAMES = {1: 'text', 3: 'image', 34: 'voice', 42: 'contact', 43: 'video',
                  47: 'emoji', 48: 'location', 49: 'app', 50: 'call', 10000: 'system', 10002: 'recall'}

    def __init__(self, path: str | Path, account_id: str, *, expected_wechat_version: str,
                 key_provider: Callable[[], str | bytes] | BoundDatabaseKey | None = None,
                 cipher_profile: CipherProfile | None = None, query_timeout_seconds: int = 30,
                 key_source: str = 'operator_supplied_verified'):
        self.path = _safe_local(path)
        if self.path.name != 'EnMicroMsg.db':
            raise ValidationError("AndroidDatabase requires an EnMicroMsg.db snapshot")
        self.account_id = _text(account_id, 'account_id')
        self.wechat_version = _version(expected_wechat_version)
        _integer(query_timeout_seconds, 'query timeout', 1, 300)
        self.query_timeout_seconds = query_timeout_seconds
        self.cipher_profile = cipher_profile
        if key_source not in ('operator_supplied_verified', 'frida_observed_candidate_verified'):
            raise ValidationError('invalid database key provenance')
        self._conn = None
        self._bound_key = None
        self.fingerprint = self._bundle_fingerprint()
        self.source_id = f'android-schema-snapshot:{self.fingerprint}:{self.account_id}'
        self.origin = 'operator_supplied_android_snapshot'
        self._deadline = 0.0
        try:
            if key_provider is None:
                with self.path.open('rb') as handle:
                    if handle.read(16) != b'SQLite format 3\x00':
                        raise DatabaseOpenError("encrypted database requires a local key provider and cipher profile")
                if cipher_profile is not None:
                    raise ValidationError("cipher profile requires a key provider")
                driver = sqlite3
                passphrase = None
            else:
                if not isinstance(cipher_profile, CipherProfile):
                    raise ValidationError("encrypted database requires an explicit CipherProfile")
                cipher_profile.__post_init__()
                try:
                    driver = importlib.import_module('sqlcipher3.dbapi2')
                except ImportError:
                    raise UnsupportedFeature("install the optional sqlcipher3-binary dependency to decrypt snapshots") from None
                if isinstance(key_provider, BoundDatabaseKey):
                    passphrase = key_provider.for_database(self.account_id, self.fingerprint,
                                                          self.wechat_version, cipher_profile)
                else:
                    if isinstance(key_provider, LegacyKeyCandidate) and key_provider.wechat_version != self.wechat_version:
                        raise IdentityMismatch("candidate version does not match requested WeChat version")
                    if not callable(key_provider):
                        raise ValidationError("key_provider must be callable or BoundDatabaseKey")
                    try:
                        passphrase = key_provider()
                    except Exception:
                        raise DatabaseOpenError("local database key provider failed") from None
                if type(passphrase) is bytes:
                    # SQLCipher 4.6.1 pragma.c 的 hexkey 路径只有 40 字节缓冲区。
                    # 更长会被静默截断，因此在交给驱动前必须明确拒绝。
                    if not 1 <= len(passphrase) <= 40:
                        raise DatabaseOpenError("binary database passphrase must contain 1 to 40 bytes")
                elif (type(passphrase) is not str or not passphrase or len(passphrase) > 4096
                      or '\x00' in passphrase):
                    raise DatabaseOpenError("local database key provider returned invalid material")
            # mode=ro 仍读取相邻 WAL；immutable=1 在此绝不能使用。
            self._conn = driver.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=2)
            self._conn.row_factory = driver.Row
            self._conn.set_progress_handler(lambda: int(time.monotonic() > self._deadline), 1000)
            self._deadline = time.monotonic() + self.query_timeout_seconds
            if passphrase is not None:
                if not self._conn.execute('PRAGMA cipher_version').fetchone():
                    raise DatabaseOpenError("driver does not support SQLCipher")
                if type(passphrase) is bytes:
                    # hexkey 解码成原始口令字节，再经 sqlite3_key_v2/KDF；不是绕过
                    # KDF 的 PRAGMA key="x'...'" 原始密钥格式。仅生成有效偶数 hex。
                    self._conn.execute("PRAGMA hexkey = '" + passphrase.hex() + "'")
                else:
                    self._conn.execute("PRAGMA key = '" + passphrase.replace("'", "''") + "'")
                for statement in cipher_profile.pragmas():
                    self._conn.execute(statement)
            self._conn.execute('PRAGMA query_only=ON')
            self._conn.execute('PRAGMA trusted_schema=OFF')
            result = self._conn.execute('PRAGMA integrity_check').fetchall()
            if len(result) != 1 or result[0][0] != 'ok':
                raise DatabaseOpenError("database integrity validation failed")
            self._tables = {row[0]: {col[1] for col in self._conn.execute(f'PRAGMA table_info("{row[0]}")')}
                            for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                            if row[0] in ('message', 'userinfo', 'rcontact', 'chatroom', 'rconversation')}
            for table, required in self.REQUIRED.items():
                if not required.issubset(self._tables.get(table, set())):
                    raise UnsupportedSchema("unrecognized Android EnMicroMsg schema; required message/self fields missing")
            invalid = self._conn.execute(
                "SELECT 1 FROM message WHERE typeof(msgId)!='integer' OR msgId<0 LIMIT 1").fetchone()
            duplicate = self._conn.execute(
                'SELECT msgId FROM message GROUP BY msgId HAVING count(*)>1 LIMIT 1').fetchone()
            if invalid or duplicate:
                raise UnsupportedSchema('native message IDs must be nonnegative unique integers')
            rows = self._conn.execute('SELECT value FROM userinfo WHERE id=?', (2,)).fetchall()
            if len(rows) != 1 or rows[0][0] != self.account_id:
                raise IdentityMismatch("database self identity does not match explicitly requested account")
            if self._bundle_fingerprint() != self.fingerprint:
                raise StaleSnapshot("snapshot bundle changed during validation")
            if passphrase is not None:
                source = 'legacy_derived_candidate_verified' if isinstance(key_provider, LegacyKeyCandidate) else key_source
                if isinstance(key_provider, BoundDatabaseKey):
                    source = key_provider.source
                self._bound_key = BoundDatabaseKey(self.account_id, self.fingerprint,
                    self.wechat_version, cipher_profile, passphrase, source)
        except (ValidationError, UnsupportedFeature, IdentityMismatch, DatabaseOpenError, StaleSnapshot):
            self.close()
            raise
        except Exception:
            self.close()
            raise DatabaseOpenError("database could not be opened: key, cipher profile, integrity or format mismatch") from None

    def __repr__(self):
        return f'<AndroidDatabase schema=android-enmicromsg-v1 open={self._conn is not None}>'

    def _bundle_fingerprint(self):
        entries = []
        for suffix in ('', '-wal', '-journal'):
            path = self.path.with_name(self.path.name + suffix)
            if path.is_symlink():
                raise ValidationError("database sidecar symlinks are not allowed")
            if path.exists():
                _safe_local(path)
                size = path.stat().st_size
                if suffix == '-journal' and size:
                    raise StaleSnapshot("nonempty rollback journal is not an accepted read-only snapshot")
                if size > 16 * 1024**3:
                    raise ValidationError("database snapshot file exceeds supported bound")
                entries.append((suffix, size, _sha256(path)))
        shm = self.path.with_name(self.path.name + '-shm')
        if shm.is_symlink() or (shm.exists() and not shm.is_file()):
            raise ValidationError("invalid shared-memory sidecar")
        return hashlib.sha256(json.dumps(entries, separators=(',', ':')).encode()).hexdigest()

    @property
    def validated_key(self) -> BoundDatabaseKey | None:
        return self._bound_key

    @property
    def capabilities(self):
        return {'schema': 'android-enmicromsg-v1', 'origin': self.origin,
                'schema_self_id_matches': True, 'authenticated_device_account_verified': False,
                'account_identity_evidence': 'known_schema_userinfo_id_2_matches_requested_account',
                'wechat_version_asserted': self.wechat_version,
                'wechat_version_device_verified': False, 'history_scope': 'validated_snapshot_only',
                'messages': True, 'contacts': self._has('rcontact', {'username', 'nickname', 'conRemark'}),
                'groups': self._has('chatroom', {'chatroomname', 'memberlist', 'roomowner'}),
                'moments': False, 'media_extraction': False, 'live_listening': False,
                'key_source': self._bound_key.source if self._bound_key else 'plaintext_snapshot'}

    def _has(self, table, columns):
        return columns.issubset(self._tables.get(table, set()))

    def _query(self, sql, parameters=()):
        if self._conn is None:
            raise DatabaseOpenError("database is closed")
        if self._bundle_fingerprint() != self.fingerprint:
            raise StaleSnapshot('validated database bundle changed before query')
        self._deadline = time.monotonic() + self.query_timeout_seconds
        try:
            rows = self._conn.execute(sql, parameters).fetchall()
        except Exception:
            raise DatabaseOpenError("read-only database query failed or exceeded its time bound") from None
        if self._bundle_fingerprint() != self.fingerprint:
            raise StaleSnapshot('validated database bundle changed during query')
        return rows

    @staticmethod
    def _bounds(limit, offset=0):
        _integer(limit, 'limit', 0, 10000)
        _integer(offset, 'offset', 0, 10_000_000)

    def _message(self, row):
        local_id = _integer(row['msgId'], 'native message id')
        chat = _text(row['talker'], 'native talker')
        raw = row['content']
        if raw is None:
            raw = ''
        if not isinstance(raw, str):
            raise UnsupportedSchema("binary message content requires a version-qualified decoder")
        code = _integer(row['type'], 'message type')
        is_send = _integer(row['isSend'], 'message direction', 0, 1)
        milliseconds = _integer(row['createTime'], 'message timestamp', 0, 253402300799999)
        sender = self.account_id if is_send else ('' if chat.endswith('@chatroom') else chat)
        content = raw
        if not is_send and chat.endswith('@chatroom'):
            match = re.match(r'^([A-Za-z0-9_@.\-]{1,256}):\n', raw)
            if match:
                sender, content = match[1], raw[match.end():]
        return Message(id=str(local_id), account_id=self.account_id, chat_id=chat,
            sequence=local_id, sender_id=sender, content=content,
            type=self.TYPE_NAMES.get(code, f'unknown_{code}'),
            timestamp=datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat(),
            origin=self.origin, metadata={'native_msg_svr_id': str(row['msgSvrId']), 'native_type': code,
                'is_send': bool(is_send), 'create_time_ms': milliseconds, 'schema': 'android-enmicromsg-v1',
                'source_database_fingerprint': self.fingerprint, 'raw_content': raw})

    def get_messages(self, chat_id: str, limit=20, offset=0):
        _text(chat_id, 'chat_id')
        self._bounds(limit, offset)
        return [self._message(row) for row in self._query(
            'SELECT msgId,msgSvrId,talker,content,type,createTime,isSend FROM message WHERE talker=? '
            'ORDER BY createTime DESC,msgId DESC LIMIT ? OFFSET ?', (chat_id, limit, offset))]

    def get_new_messages(self, chat_id: str, since_seq=0, limit=200):
        _integer(since_seq, 'since_seq')
        return self.get_messages_after(chat_id, after=(since_seq, str(since_seq)), limit=limit)

    def get_messages_after(self, chat_id: str, after=None, limit=200):
        _text(chat_id, 'chat_id')
        self._bounds(limit)
        since = -1
        if after is not None:
            if not isinstance(after, tuple) or len(after) != 2:
                raise ValidationError("invalid message cursor")
            since = _integer(after[0], 'message cursor')
            if after[1] != str(since):
                raise ValidationError("native message cursor ID/sequence mismatch")
        return [self._message(row) for row in self._query(
            'SELECT msgId,msgSvrId,talker,content,type,createTime,isSend FROM message WHERE talker=? '
            'AND msgId>? ORDER BY msgId ASC LIMIT ?', (chat_id, since, limit))]

    def search_contact(self, keyword: str, limit=1000):
        _text(keyword, 'contact keyword')
        self._bounds(limit)
        if not self.capabilities['contacts']:
            raise UnsupportedSchema("rcontact schema is not supported")
        alias = 'alias' if 'alias' in self._tables['rcontact'] else "'' AS alias"
        rows = self._query(f'SELECT username,nickname,conRemark,{alias} FROM rcontact '
                           'WHERE instr(username,?)>0 OR instr(nickname,?)>0 OR instr(conRemark,?)>0 '
                           'ORDER BY username LIMIT ?', (keyword, keyword, keyword, limit))
        return [{'id': row['username'], 'username': row['username'], 'nickname': row['nickname'] or '',
                 'remark': row['conRemark'] or '', 'alias': row['alias'] or '',
                 'account_id': self.account_id, 'origin': self.origin} for row in rows]

    def get_groups(self, limit=1000):
        self._bounds(limit)
        if not self.capabilities['groups']:
            raise UnsupportedSchema("chatroom schema is not supported")
        return [{'id': row['chatroomname'], 'chatroomname': row['chatroomname'],
                 'owner_id': row['roomowner'] or '', 'account_id': self.account_id, 'origin': self.origin}
                for row in self._query('SELECT chatroomname,roomowner FROM chatroom ORDER BY chatroomname LIMIT ?', (limit,))]

    def get_group_members(self, chat_id: str):
        _text(chat_id, 'chat_id')
        if not chat_id.endswith('@chatroom'):
            raise ValidationError("group ID must end in @chatroom")
        if not self.capabilities['groups']:
            raise UnsupportedSchema("chatroom schema is not supported")
        rows = self._query('SELECT memberlist,roomowner FROM chatroom WHERE chatroomname=?', (chat_id,))
        if len(rows) > 1:
            raise UnsupportedSchema("duplicate native group identity")
        if not rows:
            return []
        if not isinstance(rows[0]['memberlist'], str):
            raise UnsupportedSchema("group members require a version-qualified decoder")
        members = list(dict.fromkeys(part for part in rows[0]['memberlist'].split(';') if part))
        if len(members) > 10000:
            raise ValidationError("group member count exceeds bound")
        return [{'id': member, 'username': member, 'is_owner': member == rows[0]['roomowner'],
                 'account_id': self.account_id, 'origin': self.origin} for member in members]

    def get_sessions(self, limit=1000):
        self._bounds(limit)
        return [{'chat_id': row['talker'], 'username': row['talker'], 'last_time_ms': row['last_time'],
                 'message_count': row['message_count'], 'unread': None, 'origin': self.origin,
                 'coverage': 'derived_from_snapshot_messages', 'account_id': self.account_id}
                for row in self._query('SELECT talker,MAX(createTime) AS last_time,COUNT(*) AS message_count '
                    'FROM message GROUP BY talker ORDER BY last_time DESC,talker LIMIT ?', (limit,))]

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
