import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from typing_extensions import TypedDict

from app.constants import DATA_DIR
from app.utils.KonomiTVBS4KCloudRC import KonomiTVBS4KCloudRC


class KonomiTVBS4KCryptKeyNotExtractedError(ValueError):
    """取出前のバックアップ確認を拒否する。"""


class KonomiTVBS4KCryptKeySnapshotChangedError(ValueError):
    """一覧の変化による別の鍵への操作を拒否する。"""


class KonomiTVBS4KCryptKeyBackupNotConfirmedError(ValueError):
    """現在の鍵を取り出して保管した確認がない利用を拒否する。"""


class KonomiTVBS4KCryptLegacyMigrationRequiredError(ValueError):
    """旧版の複数鍵を一組へ自動選択することを拒否する。"""


class KonomiTVBS4KCloudCryptKeyInfo(BaseModel):
    key_present: bool
    backup_confirmed: bool
    legacy_migration_required: bool = False


class KonomiTVBS4KCloudCryptKeySnapshot(KonomiTVBS4KCloudCryptKeyInfo):
    revision: str


class KonomiTVBS4KCloudCryptKeySet(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    password: Annotated[SecretStr, Field(min_length=32, max_length=4096)]
    salt: Annotated[SecretStr, Field(min_length=32, max_length=4096)]


class KonomiTVBS4KCloudCryptPlaintext(TypedDict):
    password: str
    salt: str


class KonomiTVBS4KCloudCryptKeys:
    """OAuthと分離した不変crypt鍵とバックアップ確認を保管する。"""

    @staticmethod
    @contextmanager
    def lock(*, shared: bool = False) -> Generator[Path]:
        """一覧変更と位置による選択を同じ排他境界で扱う。

        Args:
            shared: 読取り・利用中は共有lock、生成・削除・確認状態の変更は排他lock。
        Returns:
            鍵保管ディレクトリをyieldするコンテキスト。
        """
        root = DATA_DIR / 'konomitv-bs4k-cloud-crypt-keys'
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(root / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as handle:
            fcntl.flock(handle, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            yield root

    @staticmethod
    def serializePlaintext(keys: KonomiTVBS4KCloudCryptKeySet) -> bytes:
        """保管と専用取出に限りpassword/saltだけを平文化する。

        Args:
            keys: 内部の鍵セット。
        Returns:
            秘密を含むJSON。通常APIやログへ渡してはならない。
        """
        return json.dumps(KonomiTVBS4KCloudCryptPlaintext(
            password=keys.password.get_secret_value(), salt=keys.salt.get_secret_value(),
        ), separators=(',', ':')).encode('utf-8')

    @staticmethod
    def load(path: Path) -> tuple[KonomiTVBS4KCloudCryptKeySet, bytes]:
        """既存鍵も値を変えず読み、旧バックアップ確認用の照合値を返す。

        Args:
            path: 内部で列挙した鍵ファイル。
        Returns:
            保護された鍵セットと元ファイルの照合値。
        """
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError('Invalid crypt key file.')
            data = source.read(32769)
        if len(data) > 32768:
            raise ValueError('Crypt key file is too large.')
        value = json.loads(data)
        # 展開済みの旧形式だけ読み取り互換を保つ。旧IDはAPIへ出さず、鍵を移行名目で書き換えない。
        is_legacy = isinstance(value, dict) and value.get('format') == 'KonomiTVBS4KCryptKeys'
        if is_legacy:
            if (value.get('version') != 1 or value.get('filename_encryption') != 'standard' or
                value.get('directory_name_encryption') is not True or
                str(UUID(value['region_id'])) != path.stem):
                raise ValueError('Invalid legacy crypt key file.')
            value = KonomiTVBS4KCloudCryptPlaintext(password=value['password'], salt=value['salt'])
        keys = KonomiTVBS4KCloudCryptKeySet.model_validate(value)
        if not is_legacy and path.stem != hashlib.sha256(keys.salt.get_secret_value().encode()).hexdigest():
            raise ValueError('Crypt key filename does not match its salt.')
        return keys, hashlib.sha256(data).digest()

    @classmethod
    def snapshotLocked(cls, root: Path) -> tuple[list[Path], KonomiTVBS4KCloudCryptKeySnapshot]:
        """lock内で鍵を列挙し、秘密を含まない一覧と変更検出revisionを作る。

        Args:
            root: lockで保護された保管先。
        Returns:
            内部パスと公開一覧。
        """
        paths = sorted(root.glob('*.json'))
        pending = root / '.legacy-delete'
        if pending.exists() or len(paths) > 1:
            # 中断で一件だけ残っても通常の単一鍵として採用しない。
            # 旧鍵の内容は読まず、確認対象のファイル名とstatだけで状態を比較する。
            if pending.exists():
                fd = os.open(pending, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ValueError('Invalid legacy key deletion entry.')
                    names = json.loads(source.read(4097))
                if not isinstance(names, list) or len(names) != 2 or not all(isinstance(name, str) for name in names):
                    raise ValueError('Invalid legacy key deletion state.')
                targets = [root / name for name in names]
                if not set(paths).issubset(targets):
                    raise ValueError('Unexpected keys during legacy deletion.')
                paths = targets
            if len(paths) != 2 or len(set(paths)) != 2:
                raise ValueError('Unexpected legacy crypt key count.')
            revision = hashlib.sha256(str(pending.exists()).encode())
            for path in paths:
                if path.parent != root or path.suffix != '.json' or str(UUID(path.stem)) != path.stem:
                    raise ValueError('Invalid legacy crypt key filename.')
                try:
                    file_stat = path.lstat()
                except FileNotFoundError:
                    if not pending.exists():
                        raise
                    revision.update(f'{path.name}:missing\n'.encode())
                    continue
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ValueError('Invalid legacy crypt key entry.')
                revision.update(f'{path.name}:{file_stat.st_ino}:{file_stat.st_size}:{file_stat.st_mtime_ns}:{file_stat.st_ctime_ns}\n'.encode())
            return paths, KonomiTVBS4KCloudCryptKeySnapshot(
                revision=revision.hexdigest(), key_present=True, backup_confirmed=False,
                legacy_migration_required=True,
            )
        confirmed = False
        revision = hashlib.sha256()
        for path in paths:
            keys, old_digest = cls.load(path)
            digest = hashlib.sha256(cls.serializePlaintext(keys)).digest()
            try:
                saved = path.with_suffix('.backup').read_bytes()
                confirmed = hmac.compare_digest(saved, digest) or hmac.compare_digest(saved, old_digest)
            except FileNotFoundError:
                confirmed = False
            file_stat = path.stat()
            revision.update(f'{path.name}:{file_stat.st_size}:{file_stat.st_mtime_ns}:{confirmed}\n'.encode())
            # 粗いmtimeの保存先でも、削除後に別のpasswordを投入した変更を見落とさない。
            revision.update(digest)
        return paths, KonomiTVBS4KCloudCryptKeySnapshot(
            revision=revision.hexdigest(), key_present=bool(paths), backup_confirmed=confirmed,
        )

    @classmethod
    def snapshot(cls) -> KonomiTVBS4KCloudCryptKeySnapshot:
        """通常API用の一覧を返す。鍵や領域IDは含めない。

        Args:
            None
        Returns:
            公開一覧。
        """
        with cls.lock(shared=True) as root:
            return cls.snapshotLocked(root)[1]

    @classmethod
    def create(cls) -> KonomiTVBS4KCloudCryptKeyInfo:
        """製品がcryptのpassword/saltを生成する。

        Args:
            None
        Returns:
            秘密を含まない状態。
        """
        return cls.insert(KonomiTVBS4KCloudCryptKeySet(
            password=SecretStr(secrets.token_urlsafe(32)), salt=SecretStr(secrets.token_urlsafe(32)),
        ))

    @classmethod
    def insert(cls, keys: KonomiTVBS4KCloudCryptKeySet) -> KonomiTVBS4KCloudCryptKeyInfo:
        """salt由来の内部名で鍵を投入し、既存鍵は上書きしない。

        Args:
            keys: 管理者からのpassword/salt。
        Returns:
            秘密を含まない状態。
        """
        fingerprint = hashlib.sha256(keys.salt.get_secret_value().encode()).hexdigest()
        with cls.lock() as root:
            # saltが異なっても二組目を作らない。置換は管理者の明示削除後だけ許可する。
            if any(root.glob('*.json')) or (root / '.legacy-delete').exists():
                raise FileExistsError('Crypt key already exists.')
            cls.write(root / f'{fingerprint}.json', cls.serializePlaintext(keys), exclusive=True)
        return KonomiTVBS4KCloudCryptKeyInfo(key_present=True, backup_confirmed=False)

    @classmethod
    def selectedPath(cls, root: Path, revision: str) -> Path:
        """古い一覧の位置が別の鍵を指す場合は操作を拒否する。

        Args:
            root: lockで保護された保管先。
            revision: その一覧のrevision。
        Returns:
            内部の選択先。
        """
        paths, current = cls.snapshotLocked(root)
        if not hmac.compare_digest(current.revision, revision):
            raise KonomiTVBS4KCryptKeySnapshotChangedError('Crypt key list changed.')
        if current.legacy_migration_required:
            raise KonomiTVBS4KCryptLegacyMigrationRequiredError('Legacy key cleanup requires confirmation.')
        if not paths:
            raise FileNotFoundError('Crypt key not found.')
        return paths[0]

    @classmethod
    def extract(cls, revision: str) -> bytes:
        """専用取出操作へpassword/saltだけを返す。

        Args:
            revision: 選択時の一覧revision。
        Returns:
            平文JSON。
        """
        with cls.lock() as root:
            path = cls.selectedPath(root, revision)
            data = cls.serializePlaintext(cls.load(path)[0])
            cls.write(path.with_suffix('.extracted'), hashlib.sha256(data).digest())
            return data

    @classmethod
    def confirmBackup(cls, revision: str) -> KonomiTVBS4KCloudCryptKeyInfo:
        """管理者による取出後のバックアップ確認を記録する。

        Args:
            revision: 選択時の一覧revision。
        Returns:
            公開状態。
        """
        with cls.lock() as root:
            path = cls.selectedPath(root, revision)
            keys, old_digest = cls.load(path)
            digest = hashlib.sha256(cls.serializePlaintext(keys)).digest()
            try:
                extracted = path.with_suffix('.extracted').read_bytes()
            except FileNotFoundError:
                raise KonomiTVBS4KCryptKeyNotExtractedError('Extract the key before confirming its backup.') from None
            if not (hmac.compare_digest(extracted, digest) or hmac.compare_digest(extracted, old_digest)):
                raise KonomiTVBS4KCryptKeyNotExtractedError('Extract the key before confirming its backup.')
            cls.write(path.with_suffix('.backup'), digest)
        return KonomiTVBS4KCloudCryptKeyInfo(key_present=True, backup_confirmed=True)

    @classmethod
    def delete(cls, revision: str) -> None:
        """管理者が確認した単一鍵または旧版二件を削除する。クラウドにはアクセスしない。

        Args:
            revision: 削除確認を開いた時点の状態。
        Returns:
            None
        """
        with cls.lock() as root:
            paths, current = cls.snapshotLocked(root)
            if not hmac.compare_digest(current.revision, revision):
                raise KonomiTVBS4KCryptKeySnapshotChangedError('Crypt key state changed.')
            if not paths:
                raise FileNotFoundError('Crypt key not found.')
            # cryptの初期化もこの鍵lock内で行う。保存鍵を消す前に鍵を保持するrcloneを全て停止する。
            # 通常の接続確認は鍵を読み込まないため、並行して起動しても古い鍵は復活しない。
            for marker in KonomiTVBS4KCloudRC.CONTROL_DIRECTORY.glob('*.active'):
                owner, connection = marker.stem.split('_', 1)
                KonomiTVBS4KCloudRC.stop(int(owner), UUID(connection))
            pending = root / '.legacy-delete'
            if current.legacy_migration_required and not pending.exists():
                # 管理者が確認した二件を先に永続化し、中断後も別の鍵を巻き込まない。
                cls.write(pending, json.dumps([path.name for path in paths]).encode(), exclusive=True)
            for path in paths:
                # 削除後の投入で古いバックアップ確認を引き継がない。
                path.with_suffix('.extracted').unlink(missing_ok=True)
                path.with_suffix('.backup').unlink(missing_ok=True)
                path.unlink(missing_ok=True)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                # 鍵の削除を先に確定し、クラッシュ後にマーカーだけ消えて鍵が残ることを防ぐ。
                os.fsync(fd)
                pending.unlink(missing_ok=True)
                os.fsync(fd)
            finally:
                os.close(fd)

    @staticmethod
    def write(path: Path, data: bytes, exclusive: bool = False) -> None:
        """鍵の上書き禁止とマーカーのatomic更新を永続化する。

        Args:
            path: 内部の保存先。
            data: 保管するバイト列。ログへ渡さない。
            exclusive: 既存鍵を置換しない場合True。
        Returns:
            None
        """
        fd, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            if exclusive:
                os.link(temporary, path)
            else:
                os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)
