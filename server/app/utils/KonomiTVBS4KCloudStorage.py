import configparser
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from typing_extensions import TypedDict

from app import logging
from app.constants import DATA_DIR
from app.models.User import User


class KonomiTVBS4KCloudConnection(BaseModel):
    id: str
    name: str
    provider: Literal['GoogleDrive', 'PCloud']
    region: Literal['US', 'EU']
    status: Literal['Connected', 'NeedsCheck', 'Error']
    checked_at: str | None
    expires_at: str | None


class KonomiTVBS4KCloudImport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Annotated[str, Field(min_length=1, max_length=80)]
    provider: Literal['GoogleDrive', 'PCloud']
    region: Literal['US', 'EU'] = 'US'
    token_json: Annotated[SecretStr, Field(max_length=32768)]
    client_id: Annotated[SecretStr, Field(max_length=1024)] = SecretStr('')
    client_secret: Annotated[SecretStr, Field(max_length=4096)] = SecretStr('')

    @field_validator('client_id', 'client_secret')
    @classmethod
    def validateClientCredential(cls, value: SecretStr) -> SecretStr:
        """rclone設定へ改行や制御文字を持ち込まない。

        Args:
            value: 任意のOAuthクライアント情報。
        Returns:
            検証済みの非公開値。
        """
        if any(not 33 <= ord(character) <= 126 for character in value.get_secret_value()):
            raise ValueError('Invalid OAuth client credential.')
        return value

    @field_validator('name')
    @classmethod
    def validateName(cls, value: str) -> str:
        """設定ファイル内の表示名に制御文字を持ち込まない。

        Args:
            value: 管理者が付けた表示名。
        Returns:
            前後の空白を除いた表示名。
        """
        if not value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError('Invalid connection name.')
        return value.strip()


class KonomiTVBS4KCloudToken(BaseModel):
    model_config = ConfigDict(extra='forbid')
    access_token: Annotated[SecretStr, Field(min_length=1, max_length=16384)]
    token_type: Literal['Bearer', 'bearer'] = 'Bearer'
    refresh_token: Annotated[SecretStr, Field(max_length=16384)] = SecretStr('')
    expiry: datetime | None = None
    expires_in: Annotated[int, Field(ge=0)] | None = None

    @field_validator('expiry')
    @classmethod
    def validateExpiry(cls, value: datetime | None) -> datetime | None:
        """タイムゾーンのない有効期限を拒否する。

        Args:
            value: rclone の有効期限。
        Returns:
            UTC と比較できる有効期限。
        """
        if value is not None and value.tzinfo is None:
            raise ValueError('Token expiry must include a timezone.')
        return value


class KonomiTVBS4KCloudStoredToken(TypedDict):
    access_token: str
    token_type: str
    refresh_token: str
    expiry: str


class KonomiTVBS4KCloudStorage:
    """管理者所有のクラウド認証と、読み取り専用の接続確認を管理する。"""

    @staticmethod
    @contextmanager
    def ownerLock(owner_id: int) -> Generator[Path]:
        """認証ディレクトリを削除しても同じinodeで所有者操作を排他する。

        Args:
            owner_id: 所有者のDB ID。
        Returns:
            所有者の認証ディレクトリをyieldするコンテキスト。
        """
        base = DATA_DIR / 'konomitv-bs4k-cloud-storage'
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        # lockは秘密を含まず、所有者ディレクトリの外に残して削除とのinode競合を避ける。
        fd = os.open(base / f'.owner-{owner_id}.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield base / str(owner_id)

    @staticmethod
    def removeOwner(root: Path) -> None:
        """ownerLock保持中に所有者のローカル認証だけを破棄する。

        Args:
            root: ownerLockが返した認証ディレクトリ。
        Returns:
            None
        """
        # shutilはディレクトリsymlinkを拒否し、配下のsymlinkも辿らず除去する。
        if root.exists() or root.is_symlink():
            shutil.rmtree(root)
            directory_fd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    @classmethod
    async def cleanupOrphanedOwners(cls) -> None:
        """DB commit後の中断等で残った、存在しない所有者の認証を起動時に回収する。

        Args:
            None
        Returns:
            None
        """
        base = DATA_DIR / 'konomitv-bs4k-cloud-storage'
        if not base.is_dir():
            return
        try:
            roots = list(base.iterdir())
        except OSError:
            logging.warning('[KonomiTVBS4KCloudStorage] Unable to enumerate orphaned owners.')
            return
        for root in roots:
            if (not root.name.isascii() or not root.name.isdecimal() or len(root.name) > 19 or
                str(int(root.name)) != root.name or int(root.name) > 2**63 - 1 or
                not root.is_dir() or root.is_symlink()):
                continue
            try:
                with cls.ownerLock(int(root.name)):
                    # 管理者権限の有無ではなくアカウントの存在で判定する。DB照会失敗時は削除しない。
                    if not await User.filter(id=int(root.name)).exists():
                        cls.removeOwner(root)
            except OSError:
                logging.warning('[KonomiTVBS4KCloudStorage] Orphaned owner cleanup could not complete.')
            except Exception:
                # DB照会の失敗を「所有者なし」と解釈しない。認証は保持して次回起動時に再試行する。
                logging.warning('[KonomiTVBS4KCloudStorage] Unable to verify orphaned owners; cleanup stopped.')
                return

    @classmethod
    def operate(
        cls,
        owner_id: int,
        operation: Literal['List', 'Import', 'Check', 'Disconnect'],
        connection_id: UUID | None = None,
        imported: KonomiTVBS4KCloudImport | None = None,
        *,
        owner_is_active: Callable[[], bool],
    ) -> list[KonomiTVBS4KCloudConnection] | KonomiTVBS4KCloudConnection | list[str] | None:
        """
        同じ認証ファイルへの取り込み・rclone更新・解除を排他して実行する。

        Args:
            owner_id: 認証済み管理者のID。
            operation: 許可されたローカル操作。
            connection_id: 既存接続ID。新規取り込みでは省略する。
            imported: 新しい表示名・プロバイダー・秘密のトークンJSON。
            owner_is_active: lock内で所有者の現存・管理権限を再確認するDB照会。
        Returns:
            秘密を含まない接続情報またはフォルダ名。解除時はNone。
        """
        # flock は別workerも含めて保護する。rclone による更新tokenの書戻しも同じ境界内で行う。
        with cls.ownerLock(owner_id) as root:
            # 認証済みHTTP要求でも、アカウント削除後に遅れて取り込みを開始させない。
            if not owner_is_active():
                raise FileNotFoundError('Cloud connection owner is no longer active.')
            root.mkdir(exist_ok=True, mode=0o700)
            os.chmod(root, 0o700)
            if operation == 'List':
                result: list[KonomiTVBS4KCloudConnection] = []
                for path in sorted(root.glob('*.conf')):
                    # 作成中ファイルと他用途のファイルは公開対象にしない。
                    try:
                        UUID(path.stem)
                    except ValueError:
                        continue
                    result.append(cls.describe(path, cls.readConfig(path)))
                return result

            path = root / f'{connection_id or uuid4()}.conf'
            if connection_id is not None and (not path.is_file() or path.is_symlink()):
                raise FileNotFoundError('Cloud connection not found.')
            if operation == 'Disconnect':
                # クラウドAPIへ削除・revoke要求を送らず、所有者のローカル認証だけを解除する。
                path.unlink()
                directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                return None
            if operation == 'Import':
                assert imported is not None
                client_id = imported.client_id.get_secret_value()
                client_secret = imported.client_secret.get_secret_value()
                # 空欄は以前の値の保持ではなく組み込みアプリ。独自アプリはIDとsecretの組を必須にする。
                if bool(client_id) != bool(client_secret) or (imported.provider != 'GoogleDrive' and client_id):
                    raise ValueError('Specify both OAuth client fields for Google Drive only.')
                if connection_id is not None:
                    previous = cls.readConfig(path)
                    if previous['connection']['konomitv_bs4k_provider'] != imported.provider:
                        raise ValueError('Reimport cannot change the cloud provider.')
                token = KonomiTVBS4KCloudToken.model_validate_json(imported.token_json.get_secret_value())
                # Google の一時access tokenだけでは再起動後の連携を維持できない。
                if imported.provider == 'GoogleDrive' and not token.refresh_token.get_secret_value():
                    raise ValueError('A Google Drive refresh token is required.')
                if imported.provider == 'GoogleDrive' and (token.expiry is None or token.expiry.year <= 1):
                    raise ValueError('A Google Drive token expiry is required.')
                if token.expiry is not None and token.expiry.year > 1 and token.expiry <= datetime.now(UTC):
                    if not token.refresh_token.get_secret_value():
                        raise ValueError('The imported token has expired.')
                config = configparser.ConfigParser(interpolation=None)
                # Googleは取り込み時に更新を必須にし、発行アプリとrefresh tokenの組を確認する。
                # 過去の期限は一時設定だけに使用し、成功後のrclone更新値を保管する。
                token_expiry = '2000-01-01T00:00:00Z' if imported.provider == 'GoogleDrive' else (
                    token.expiry.isoformat() if token.expiry else '0001-01-01T00:00:00Z'
                )
                config['connection'] = {
                    'type': 'drive' if imported.provider == 'GoogleDrive' else 'pcloud',
                    'token': json.dumps(KonomiTVBS4KCloudStoredToken(
                        access_token=token.access_token.get_secret_value(),
                        token_type=token.token_type,
                        refresh_token=token.refresh_token.get_secret_value(),
                        expiry=token_expiry,
                    )),
                    'konomitv_bs4k_name': imported.name,
                    'konomitv_bs4k_provider': imported.provider,
                    'konomitv_bs4k_region': imported.region,
                }
                if imported.provider == 'PCloud':
                    config['connection']['hostname'] = 'eapi.pcloud.com' if imported.region == 'EU' else 'api.pcloud.com'
                elif client_id:
                    # rcloneはtokenを発行した同じアプリで更新する。秘密をCLI引数や公開メタデータへ渡さない。
                    config['connection']['client_id'] = client_id
                    config['connection']['client_secret'] = client_secret
                # 確認に成功した認証だけを公開する。再取り込み失敗では既存接続を保持する。
                fd, staging_name = tempfile.mkstemp(prefix='.import-', suffix='.conf', dir=root)
                staging = Path(staging_name)
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as output:
                        config.write(output)
                    cls.listFolders(staging)
                    config = cls.readConfig(staging)
                    config['connection']['konomitv_bs4k_status'] = 'Connected'
                    config['connection']['konomitv_bs4k_checked_at'] = datetime.now(UTC).isoformat()
                    cls.writeConfig(path, config)
                    return cls.describe(path, config)
                finally:
                    staging.unlink(missing_ok=True)

            config = cls.readConfig(path)
            try:
                folders = cls.listFolders(path)
            except (OSError, subprocess.SubprocessError, ValueError):
                # rclone が更新したtokenを古いsnapshotで上書きしない。
                config = cls.readConfig(path)
                config['connection']['konomitv_bs4k_status'] = 'Error'
                config['connection']['konomitv_bs4k_checked_at'] = datetime.now(UTC).isoformat()
                cls.writeConfig(path, config)
                raise
            config = cls.readConfig(path)
            config['connection']['konomitv_bs4k_status'] = 'Connected'
            config['connection']['konomitv_bs4k_checked_at'] = datetime.now(UTC).isoformat()
            cls.writeConfig(path, config)
            return folders

    @staticmethod
    def readConfig(path: Path) -> configparser.ConfigParser:
        """保管形式を読み、公開用メタデータと認証を同じ世代から取得する。

        Args:
            path: 所有者ディレクトリ内の認証ファイル。
        Returns:
            補間を行わない設定パーサー。
        """
        if path.is_symlink():
            raise ValueError('Invalid cloud credential file.')
        config = configparser.ConfigParser(interpolation=None)
        with path.open(encoding='utf-8') as source:
            config.read_file(source)
        return config

    @staticmethod
    def writeConfig(path: Path, config: configparser.ConfigParser) -> None:
        """認証と状態を同一ファイルへアトミックに保存する。

        Args:
            path: 保存先。
            config: rclone更新後の設定。
        Returns:
            None
        """
        fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                config.write(output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def describe(path: Path, config: configparser.ConfigParser) -> KonomiTVBS4KCloudConnection:
        """許可したフィールドだけを公開し、token本文を応答へ混ぜない。

        Args:
            path: 接続IDを持つファイル名。
            config: 読み込んだ設定。
        Returns:
            秘密を含まない接続情報。
        """
        remote = config['connection']
        token = KonomiTVBS4KCloudToken.model_validate_json(remote['token'])
        expiry = token.expiry if token.expiry is not None and token.expiry.year > 1 else None
        status = remote.get('konomitv_bs4k_status', 'NeedsCheck')
        if status == 'Connected' and expiry is not None and expiry <= datetime.now(UTC):
            status = 'NeedsCheck'
        return KonomiTVBS4KCloudConnection.model_validate({
            'id': path.stem,
            'name': remote['konomitv_bs4k_name'],
            'provider': remote['konomitv_bs4k_provider'],
            'region': remote['konomitv_bs4k_region'],
            'status': status,
            'checked_at': remote.get('konomitv_bs4k_checked_at'),
            'expires_at': expiry.isoformat() if expiry else None,
        })

    @staticmethod
    def listFolders(path: Path) -> list[str]:
        """rcloneに更新tokenの管理を任せ、直下フォルダの読取りで接続を確認する。

        Args:
            path: サーバー内だけで扱うrclone設定ファイル。
        Returns:
            直下のフォルダ名（表示上限100件）。
        """
        # stdoutを一時ファイルへ逃がし、巨大一覧でメモリを使い切らない。stderrは秘密を含み得る。
        with tempfile.TemporaryFile() as output:
            subprocess.run(
                ['/usr/bin/rclone', '--config', str(path), '--ask-password=false',
                 '--contimeout', '10s', '--timeout', '20s', '--retries', '1', '--low-level-retries', '1',
                 'lsjson', 'connection:', '--dirs-only', '--max-depth', '1'],
                stdout=output, stderr=subprocess.DEVNULL, check=True, timeout=25,
            )
            if output.tell() > 4 * 1024 * 1024:
                raise ValueError('The folder listing is too large.')
            output.seek(0)
            listing = json.load(output)
        if not isinstance(listing, list):
            raise ValueError('Invalid cloud folder listing.')
        return [item['Name'] for item in listing[:100] if isinstance(item, dict) and isinstance(item.get('Name'), str)]
