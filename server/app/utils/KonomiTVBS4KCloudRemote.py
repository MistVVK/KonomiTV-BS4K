import hashlib
import hmac
import math
import os
import stat
import tempfile
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from uuid import UUID

from typing_extensions import TypedDict

from app.constants import DATA_DIR
from app.utils.HostPath import DOCKER_HOST_ROOT, ToRuntimePath
from app.utils.KonomiTVBS4KCloudCryptKeys import (
    KonomiTVBS4KCloudCryptKeys,
    KonomiTVBS4KCryptKeyBackupNotConfirmedError,
    KonomiTVBS4KCryptKeySnapshotChangedError,
)
from app.utils.KonomiTVBS4KCloudRC import (
    KonomiTVBS4KCloudControlRequest,
    KonomiTVBS4KCloudListOptions,
    KonomiTVBS4KCloudListRequest,
    KonomiTVBS4KCloudRC,
)
from app.utils.KonomiTVBS4KCloudStorage import KonomiTVBS4KCloudStorage


class KonomiTVBS4KCloudMountDirectoryRequest(KonomiTVBS4KCloudControlRequest):
    mount_id: str


class KonomiTVBS4KCloudObscureRequest(TypedDict):
    clear: str


class KonomiTVBS4KCloudMountRequest(TypedDict):
    fs: str
    mountPoint: str
    mountOpt: KonomiTVBS4KCloudMountOptions
    vfsOpt: KonomiTVBS4KCloudVFSOptions


class KonomiTVBS4KCloudMountOptions(TypedDict):
    AllowOther: bool
    DeviceName: str


class KonomiTVBS4KCloudVFSOptions(TypedDict):
    ReadOnly: bool
    CacheMode: int


class KonomiTVBS4KCloudCopyRequest(TypedDict):
    srcFs: str
    srcRemote: str
    dstFs: str
    dstRemote: str
    _async: bool
    _config: KonomiTVBS4KCloudCopyOptions


class KonomiTVBS4KCloudCopyOptions(TypedDict):
    IgnoreTimes: bool


class KonomiTVBS4KCloudStatRequest(TypedDict):
    fs: str
    remote: str


class KonomiTVBS4KCloudJobRequest(TypedDict):
    jobid: int


class KonomiTVBS4KCloudHashRequest(KonomiTVBS4KCloudStatRequest):
    hashType: str
    download: bool
    base64: bool
    _async: bool


class KonomiTVBS4KCloudTransferCancelled(Exception):
    """転送の終了を確認してからworkerへ取消要求を返す。"""


class KonomiTVBS4KCloudKeyChanged(ValueError):
    """実行開始時と違う鍵で転送・検証・清掃を継続することを拒否する。"""


class KonomiTVBS4KCloudRemote:
    """鍵と接続の寿命を保護し、crypt経由のmountと録画コピーを管理する。"""

    GENERATED_SOURCE_DIRECTORY = DATA_DIR / 'konomitv-bs4k-cloud-generated-source'
    SIDECAR_GENERATED_SOURCE_DIRECTORY = Path('/cloud-generated-source')
    _key_identity: ContextVar[str | None] = ContextVar('konomitv_bs4k_cloud_key_identity', default=None)

    @staticmethod
    def pinKey(revision: str | None = None, *, require_backup_confirmation: bool = False) -> str:
        """確認した鍵のidentityだけを返し、バックアップ確認を別の鍵へ流用させない。

        Args:
            revision: UIが確認した鍵一覧revision。内部同期ではNoneで現在の鍵を固定する。
            require_backup_confirmation: 現在の鍵に対応するバックアップ確認を必須にするか。
        Returns:
            password/saltの内容に対応する非公開identity。鍵の値やファイル情報は返さない。
        """
        with KonomiTVBS4KCloudCryptKeys.lock(shared=True) as root:
            try:
                paths, state = KonomiTVBS4KCloudCryptKeys.snapshotLocked(root)
                if revision is None:
                    if not state.key_present or state.legacy_migration_required:
                        raise ValueError('A single crypt key is required.')
                    path = paths[0]
                else:
                    path = KonomiTVBS4KCloudCryptKeys.selectedPath(root, revision)
                if require_backup_confirmation and not state.backup_confirmed:
                    raise KonomiTVBS4KCryptKeyBackupNotConfirmedError('Crypt key backup confirmation is required.')
                keys = KonomiTVBS4KCloudCryptKeys.load(path)[0]
                return hashlib.sha256(KonomiTVBS4KCloudCryptKeys.serializePlaintext(keys)).hexdigest()
            except KonomiTVBS4KCryptKeyBackupNotConfirmedError:
                raise
            except KonomiTVBS4KCryptKeySnapshotChangedError:
                raise KonomiTVBS4KCryptKeySnapshotChangedError('Crypt key confirmation changed.') from None
            except (ValueError, KeyError, TypeError):
                raise ValueError('Crypt key confirmation changed or is unavailable.') from None

    @classmethod
    @contextmanager
    def keyIdentity(cls, identity: str | None) -> Generator[None]:
        """一実行の全RC操作を同じ鍵へ束縛し、to_threadにも引き継ぐ。

        Args:
            identity: 永続ジョブまたは同期開始時に固定したidentity。
        Returns:
            None。identity不明の古いジョブを現在の鍵へ自動的に紐付けない。
        """
        if identity is None or len(identity) != 64 or any(char not in '0123456789abcdef' for char in identity):
            raise ValueError('A pinned crypt key identity is required.')
        token = cls._key_identity.set(identity)
        try:
            yield
        finally:
            cls._key_identity.reset(token)

    @classmethod
    def boundKeyIdentity(cls) -> str:
        """現在の実行が固定した非公開identityを返す。

        Args:
            None
        Returns:
            固定済みidentity。未固定なら失敗させる。
        """
        identity = cls._key_identity.get()
        if identity is None:
            raise ValueError('Cloud operation has no pinned key identity.')
        return identity

    @classmethod
    def guardLocalCleanup(
        cls, owner_id: int, connection_id: UUID, folder: str, operation: Callable[[], None], *,
        owner_is_active: Callable[[], bool],
    ) -> None:
        """原本清掃中も認証と固定した鍵を保持し、鍵変更後の原本削除を拒否する。

        Args:
            owner_id: 接続所有者。
            connection_id: 固定した接続。
            folder: 固定した領域。
            operation: 検証済み原本だけのローカル清掃。
            owner_is_active: 所有者の現存確認。
        Returns:
            None
        """
        cls.boundKeyIdentity()
        with cls._access(owner_id, connection_id, folder, owner_is_active=owner_is_active):
            operation()

    @classmethod
    def uploadGeneratedBytes(
        cls, owner_id: int, connection_id: UUID, folder: str, payload: bytes, destination: str, *,
        owner_is_active: Callable[[], bool], timeout: float,
    ) -> None:
        """アプリ生成物だけを専用spoolに置き、サイズ既知の通常ファイルとして同じRCで送る。

        Args:
            owner_id: 接続所有者。
            connection_id: 固定した接続UUID。
            folder: 固定した保存領域。
            payload: 公開目録・削除マーカー・完成解析結果。認証情報や鍵を渡さない。
            destination: 暗号化領域内の論理名。
            owner_is_active: 所有者の現存確認。
            timeout: 転送の待機上限。
        Returns:
            None。通常の録画フォルダ設定は不要で、sidecarにはspoolだけをRO共有する。
        """
        root = cls.GENERATED_SOURCE_DIRECTORY
        # 固定spoolの親も含めて逸脱を拒否し、既存ディレクトリの所有者・modeは変更しない。
        if root.resolve() != root:
            raise ValueError('Generated cloud source directory must not be redirected.')
        root.mkdir(mode=0o700, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=root, prefix='generated-', suffix='.bin', delete=False) as output:
                temporary = Path(output.name)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            cls.uploadFile(owner_id, connection_id, folder, temporary, destination,
                           recorded_folders=[], owner_is_active=owner_is_active, timeout=timeout)
        finally:
            # この呼出しが作った入力だけを回収する。失敗したクラウド側の公開・清掃状態は呼出し元が保持する。
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def validateRelativePath(path: str) -> None:
        """remote設定として解釈されない、空でない相対パスに限定する。

        Args:
            path: フォルダまたは暗号化領域内のファイル名。
        Returns:
            None
        """
        if (not path or len(path) > 1024 or path != path.strip() or
            any(ord(char) < 32 or char in ':\\' for char in path) or
            any(part in ('', '.', '..') for part in path.split('/'))):
            raise ValueError('Invalid cloud relative path.')

    @staticmethod
    def stopConnection(owner_id: int, connection_id: UUID) -> None:
        """前プロセスの転送停止を確認し、復旧workerが古い書込みと並走することを防ぐ。

        Args:
            owner_id: 永続ジョブの所有者。
            connection_id: 永続ジョブが固定した接続。
        Returns:
            None。停止不能なら清掃や再実行を始めず例外を返す。
        """
        with KonomiTVBS4KCloudStorage.ownerLock(owner_id):
            KonomiTVBS4KCloudRC.stop(owner_id, connection_id)

    @classmethod
    def listNames(
        cls, owner_id: int, connection_id: UUID, folder: str, prefix: str, *,
        directories: bool, owner_is_active: Callable[[], bool],
    ) -> list[str]:
        """crypt領域の指定階層だけを列挙し、通信失敗を空一覧へ置き換えない。

        Args:
            owner_id: 接続の所有者。
            connection_id: 接続UUID。
            folder: 固定した暗号化領域。
            prefix: 目録・変更記録・削除マーカーの論理ディレクトリ。
            directories: ディレクトリ名だけを返す場合True、ファイル名ならFalse。
            owner_is_active: 所有者の現存確認。
        Returns:
            当該階層の名前。明示的な不在だけを空一覧とする。
        """
        cls.validateRelativePath(prefix)
        with cls._access(owner_id, connection_id, folder, owner_is_active=owner_is_active) as (endpoint, remote, _):
            try:
                directory = KonomiTVBS4KCloudRC.request(endpoint, 'operations/stat', KonomiTVBS4KCloudStatRequest(fs=remote, remote=prefix))
                if not isinstance(directory, dict) or 'item' not in directory:
                    raise ConnectionError('Cloud catalog directory could not be verified.')
                if directory['item'] is None:
                    return []
                if not isinstance(directory['item'], dict) or directory['item'].get('IsDir') is not True:
                    raise ValueError('Cloud catalog target is not a directory.')
                result = KonomiTVBS4KCloudRC.request(endpoint, 'operations/list', KonomiTVBS4KCloudListRequest(
                    fs=remote, remote=prefix, opt=KonomiTVBS4KCloudListOptions(dirsOnly=directories, recurse=False),
                ))
            except FileNotFoundError:
                return []
            if not isinstance(result, dict) or not isinstance(result.get('list'), list):
                raise ConnectionError('Invalid cloud catalog listing.')
            names: list[str] = []
            for item in result['list']:
                if not isinstance(item, dict) or not isinstance(item.get('IsDir'), bool) or not isinstance(item.get('Name'), str):
                    raise ConnectionError('Invalid cloud catalog entry.')
                if item['IsDir'] != directories:
                    continue
                name = item['Name']
                cls.validateRelativePath(name)
                if '/' in name:
                    raise ValueError('Cloud catalog listing escaped its directory.')
                names.append(name)
            return names

    @staticmethod
    def _runJob(
        owner_id: int, connection_id: UUID, endpoint: Path, operation: str, body: object,
        timeout: float, cancellation_requested: Callable[[], bool] | None,
    ) -> object:
        """ownerLock内で非同期RCを有限待機し、終了不明の書込み・読取りを残さない。

        Args:
            owner_id: 所有者。
            connection_id: 所有中の接続。
            endpoint: 起動確認済みのUnix socket。
            operation: 内部で固定したRC操作名。
            body: _asyncを有効にした要求。秘密を含み得るため表示しない。
            timeout: 処理全体の待機上限秒数。
            cancellation_requested: 公開前の取消要求確認。公開後はNone。
        Returns:
            RCのoutput。呼出し元が構造を検証し、原文を外部へ反射しない。
        """
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('A positive finite transfer timeout is required.')
        finished = False
        deadline = time.monotonic() + timeout
        try:
            if cancellation_requested is not None and cancellation_requested():
                raise KonomiTVBS4KCloudTransferCancelled()
            result = KonomiTVBS4KCloudRC.request(endpoint, operation, body)
            if not isinstance(result, dict) or type(result.get('jobid')) is not int or result['jobid'] < 0:
                raise ConnectionError('Invalid cloud job response.')
            job = KonomiTVBS4KCloudJobRequest(jobid=result['jobid'])
            while time.monotonic() < deadline:
                if cancellation_requested is not None and cancellation_requested():
                    raise KonomiTVBS4KCloudTransferCancelled()
                state = KonomiTVBS4KCloudRC.request(endpoint, 'job/status', job)
                if not isinstance(state, dict) or type(state.get('finished')) is not bool:
                    raise ConnectionError('Invalid cloud job status.')
                if state['finished']:
                    finished = True
                    if state.get('success') is not True:
                        raise ConnectionError('Cloud job failed.')
                    return state.get('output')
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
            raise TimeoutError('Cloud job timed out.')
        finally:
            # 応答を失った要求はjob IDさえ不明になり得る。停止確認不能なら清掃へ進ませない。
            if not finished:
                KonomiTVBS4KCloudRC.stop(owner_id, connection_id)

    @classmethod
    def verifyFile(
        cls, owner_id: int, connection_id: UUID, folder: str, path: str, sha256: str, *,
        owner_is_active: Callable[[], bool], timeout: float,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> None:
        """crypt経由で全量を読み戻してSHA-256を照合し、mountの温キャッシュに依存しない。

        Args:
            owner_id: 所有者。
            connection_id: 固定した接続。
            folder: 固定した保存先。
            path: 録画UUID内の相対ファイル名。
            sha256: ローカル原本から得たSHA-256。
            owner_is_active: 所有者の現存確認。
            timeout: 全量検証の上限秒数。
            cancellation_requested: 公開前の取消確認。
        Returns:
            None。内容が一致しない場合は公開・原本清掃へ進めない。
        """
        cls.validateRelativePath(path)
        with cls._access(owner_id, connection_id, folder, owner_is_active=owner_is_active) as (endpoint, remote, _):
            result = cls._runJob(owner_id, connection_id, endpoint, 'operations/hashsumfile',
                KonomiTVBS4KCloudHashRequest(fs=remote, remote=path, hashType='SHA-256', download=True,
                                            base64=False, _async=True), timeout, cancellation_requested)
            if not isinstance(result, dict) or result.get('hash') != sha256:
                raise ValueError('Cloud file verification failed.')

    @classmethod
    def removeUnpublishedRecording(
        cls, owner_id: int, connection_id: UUID, folder: str, recording_uuid: UUID, *,
        owner_is_active: Callable[[], bool],
    ) -> None:
        """公開前の録画UUID領域だけを清掃する。呼出し元は取消状態と転送停止を先に確定する。

        Args:
            owner_id: 永続ジョブの所有者。
            connection_id: 永続ジョブが固定した接続。
            folder: 永続ジョブが固定したクラウドフォルダ。
            recording_uuid: このジョブ専用に新規割当した録画UUID。任意パスは受け付けない。
            owner_is_active: 所有者が現在も存在することの確認関数。
        Returns:
            None。公開済みまたは公開の有無が確認不能なら削除せず例外を返す。
        """
        with cls._access(owner_id, connection_id, folder, owner_is_active=owner_is_active) as (endpoint, remote, _):
            prefix = f'recordings/{recording_uuid}'
            # purgeは不在ディレクトリを500として返すbackendもある。不在を事前のstatで明示確認する。
            try:
                directory = KonomiTVBS4KCloudRC.request(endpoint, 'operations/stat', KonomiTVBS4KCloudStatRequest(
                    fs=remote, remote=prefix,
                ))
            except FileNotFoundError:
                return
            if not isinstance(directory, dict) or 'item' not in directory:
                raise ConnectionError('Cloud cleanup directory could not be verified.')
            if directory['item'] is None:
                return
            if not isinstance(directory['item'], dict) or directory['item'].get('IsDir') is not True:
                raise ValueError('Cloud cleanup target is not a directory.')
            try:
                result = KonomiTVBS4KCloudRC.request(endpoint, 'operations/stat', KonomiTVBS4KCloudStatRequest(
                    fs=remote, remote=f'{prefix}/manifest.json',
                ))
            except FileNotFoundError:
                # 対象目録の明示的な不在以外の失敗は、この境界からそのまま伝播させる。
                pass
            else:
                if not isinstance(result, dict) or 'item' not in result or result['item'] is not None:
                    raise ValueError('A published or unverifiable cloud recording cannot be cancelled.')
            # 外部公開前だけのUUID領域で、rcloneが残した部分ファイルも一緒に回収する。
            # 未公開の判定だけでは所有権を証明しないため、workerは自身の永続ジョブのUUIDだけを渡す。
            try:
                KonomiTVBS4KCloudRC.request(endpoint, 'operations/purge', KonomiTVBS4KCloudStatRequest(
                    fs=remote, remote=prefix,
                ))
            except FileNotFoundError:
                # 直前の清掃の応答喪失でも、同じ取消を完了できる。
                return

    @classmethod
    def removeDeletedRecording(
        cls, owner_id: int, connection_id: UUID, folder: str, recording_uuid: UUID, deletion_uuid: UUID, *,
        owner_is_active: Callable[[], bool],
    ) -> None:
        """検証済み削除マーカーを先に残した録画だけを回収し、マーカー自体は保持する。

        Args:
            owner_id: 固定した所有者。
            connection_id: 固定した接続。
            folder: 固定した保存先。
            recording_uuid: 削除対象の録画UUID。
            deletion_uuid: 呼出し元が内容検証済みの削除マーカーUUID。
            owner_is_active: 所有者の現存確認。
        Returns:
            None。マーカーが不在または確認不能なら本体を削除しない。
        """
        with cls._access(owner_id, connection_id, folder, owner_is_active=owner_is_active) as (endpoint, remote, _):
            marker = KonomiTVBS4KCloudRC.request(endpoint, 'operations/stat', KonomiTVBS4KCloudStatRequest(
                fs=remote, remote=f'deleted/{recording_uuid}/{deletion_uuid}.json',
            ))
            item = marker.get('item') if isinstance(marker, dict) else None
            if not isinstance(item, dict) or item.get('IsDir') is not False:
                raise ValueError('Cloud deletion marker is unavailable.')
            for prefix in (f'recordings/{recording_uuid}', f'changes/{recording_uuid}'):
                try:
                    result = KonomiTVBS4KCloudRC.request(endpoint, 'operations/stat', KonomiTVBS4KCloudStatRequest(
                        fs=remote, remote=prefix,
                    ))
                except FileNotFoundError:
                    continue
                if not isinstance(result, dict) or 'item' not in result:
                    raise ConnectionError('Cloud deletion target could not be verified.')
                if result['item'] is None:
                    continue
                if not isinstance(result['item'], dict) or result['item'].get('IsDir') is not True:
                    raise ValueError('Cloud deletion target is not a directory.')
                KonomiTVBS4KCloudRC.request(endpoint, 'operations/purge', KonomiTVBS4KCloudStatRequest(fs=remote, remote=prefix))

    @classmethod
    @contextmanager
    def _access(
        cls, owner_id: int, connection_id: UUID, folder: str, *, owner_is_active: Callable[[], bool],
    ) -> Generator[tuple[Path, str, Path]]:
        """内部操作が完了するまで、認証変更と鍵削除を排他する。

        Args:
            owner_id: 接続の所有者。
            connection_id: 接続UUID。
            folder: 操作開始時に固定したクラウド内フォルダ。
            owner_is_active: ownerLock内で所有者の現存を確認する関数。
        Returns:
            Unix socket、秘密を含むcrypt接続文字列、mount先の内部タプル。
            接続文字列はこのクラス外や例外・ログへ持ち出さない。
        """
        cls.validateRelativePath(folder)
        with KonomiTVBS4KCloudStorage.ownerLock(owner_id) as credentials:
            config = credentials / f'{connection_id}.conf'
            if not owner_is_active() or not config.is_file() or config.is_symlink():
                raise FileNotFoundError('Cloud connection is unavailable.')
            # 鍵の削除は同じlockを保持してrcloneを止める。初期化途中の鍵を残さない。
            with KonomiTVBS4KCloudCryptKeys.lock(shared=True) as root:
                try:
                    paths, state = KonomiTVBS4KCloudCryptKeys.snapshotLocked(root)
                    if not state.key_present or state.legacy_migration_required:
                        raise ValueError('A single crypt key is required.')
                    keys = KonomiTVBS4KCloudCryptKeys.load(paths[0])[0]
                except (ValueError, KeyError, TypeError):
                    # ジョブ側で失敗を記録しても、鍵ファイルの検証例外を原文で持ち出さない。
                    raise ValueError('Crypt key is unavailable.') from None
                fingerprint = hashlib.sha256(KonomiTVBS4KCloudCryptKeys.serializePlaintext(keys)).digest()
                expected = cls._key_identity.get()
                if expected is not None and not hmac.compare_digest(fingerprint.hex(), expected):
                    raise KonomiTVBS4KCloudKeyChanged('Crypt key changed during cloud operation.')
                mount_id = hashlib.sha256(folder.encode() + b'\0' + fingerprint).hexdigest()
                directory = Path('/cloud-mounts') / f'{owner_id}_{connection_id}' / mount_id
                endpoint = KonomiTVBS4KCloudRC.start(owner_id, connection_id)
                # rcloneの可逆難読化はIPC本文の中だけで行う。鍵をargvやOAuthファイルへ渡さない。
                values = ['connection:' + folder]
                for secret in (keys.password, keys.salt):
                    result = KonomiTVBS4KCloudRC.request(
                        endpoint, 'core/obscure', KonomiTVBS4KCloudObscureRequest(clear=secret.get_secret_value()),
                    )
                    if not isinstance(result, dict) or not isinstance(result.get('obscured'), str):
                        raise ConnectionError('Crypt key preparation failed.')
                    values.append(result['obscured'])
                # 接続文字列の引用符を二重化し、フォルダ中のカンマや引用符を設定として解釈させない。
                options = ','.join(name + '="' + value.replace('"', '""') + '"'
                                   for name, value in zip(('remote', 'password', 'password2'), values, strict=True))
                remote = f':crypt,{options},filename_encryption=standard,directory_name_encryption=true:'
                yield endpoint, remote, directory

    @classmethod
    def ensureMount(cls, owner_id: int, connection_id: UUID, folder: str, *, owner_is_active: Callable[[], bool]) -> Path:
        """鍵を外部へ返さず、共有mount内の読取り起点だけを返す。

        Args:
            owner_id: 接続の所有者。
            connection_id: 接続UUID。
            folder: ジョブ開始時などに固定したクラウド内相対フォルダ。
            owner_is_active: ownerLock内で所有者の現存を確認する関数。
        Returns:
            読取り用mountの起点。クラウド到達性やファイル検証の保証ではない。
        """
        with cls._access(owner_id, connection_id, folder, owner_is_active=owner_is_active) as (endpoint, remote, directory):
            mounts = KonomiTVBS4KCloudRC.request(endpoint, 'mount/listmounts', {})
            if not isinstance(mounts, dict) or not isinstance(mounts.get('mountPoints'), list):
                raise ConnectionError('Invalid mount listing.')
            if any(isinstance(item, dict) and item.get('MountPoint') == str(directory) for item in mounts['mountPoints']):
                return directory
            result = KonomiTVBS4KCloudRC.request(
                KonomiTVBS4KCloudRC.CONTROL_DIRECTORY / 'supervisor.sock', 'mount-directory',
                KonomiTVBS4KCloudMountDirectoryRequest(owner_id=owner_id, connection_id=str(connection_id), mount_id=directory.name),
            )
            if not isinstance(result, dict) or result.get('ready') is not True:
                raise ConnectionError('Mount directory is unavailable.')
            KonomiTVBS4KCloudRC.request(endpoint, 'mount/mount', KonomiTVBS4KCloudMountRequest(
                fs=remote, mountPoint=str(directory),
                mountOpt=KonomiTVBS4KCloudMountOptions(AllowOther=True, DeviceName='konomitv-bs4k-cloud'),
                vfsOpt=KonomiTVBS4KCloudVFSOptions(ReadOnly=True, CacheMode=0),
            ))
            return directory

    @classmethod
    def uploadFile(
        cls, owner_id: int, connection_id: UUID, folder: str, source: Path, destination: str, *,
        recorded_folders: Sequence[Path], owner_is_active: Callable[[], bool], timeout: float,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> None:
        """録画共有の通常ファイルをcryptへコピーし、RC完了と原本不変を確認する。

        Args:
            owner_id: 接続の所有者。
            connection_id: 永続ジョブが固定した接続UUID。
            folder: 永続ジョブが固定した保存先フォルダ。
            source: 読取り専用でsidecarへ共有済みの録画ファイル、または固定spool内のアプリ生成物。
            destination: 暗号化領域内の録画UUIDに属する相対ファイル名。
            recorded_folders: 設定から取得した録画フォルダ。要求値をそのまま許可範囲に使わない。
            owner_is_active: ownerLock内で所有者の現存を確認する関数。
            timeout: 転送完了を待つ上限秒数。各RC要求は別途45秒以内に制限される。
            cancellation_requested: 公開前のコピーだけに渡す取消確認。公開処理ではNoneにする。
        Returns:
            None。内容検証・目録公開・所在切替・原本削除は行わない。
            呼出し前に永続ジョブを保存し、RC job IDを再起動後の正本には使わない。
        """
        cls.validateRelativePath(destination)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('A positive finite transfer timeout is required.')
        raw_source = source.absolute()
        generated_root = cls.GENERATED_SOURCE_DIRECTORY
        if raw_source.is_relative_to(generated_root):
            source = raw_source.resolve(strict=True)
            # spool経由の逸脱を録画共有へフォールバックしない。DATA_DIR全体は共有しない。
            if generated_root.resolve(strict=True) != generated_root or not source.is_relative_to(generated_root):
                raise ValueError('Generated cloud source escaped its directory.')
            sidecar_source = cls.SIDECAR_GENERATED_SOURCE_DIRECTORY / source.relative_to(generated_root)
        else:
            source = ToRuntimePath(source).resolve(strict=True)
            roots = [ToRuntimePath(path).resolve(strict=True) for path in recorded_folders]
            # sidecarに共有する正規化済みパスだけを使い、ホスト全体を許可範囲へ広げない。
            if (not source.is_relative_to(DOCKER_HOST_ROOT) or
                not any(root != DOCKER_HOST_ROOT and root.is_relative_to(DOCKER_HOST_ROOT) and
                        source.is_relative_to(root) for root in roots)):
                raise ValueError('Source is outside the recording folders.')
            sidecar_source = source
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as original:
            before = os.fstat(original.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('Source must be a regular file.')
            with cls._access(owner_id, connection_id, folder, owner_is_active=owner_is_active) as (endpoint, remote, _):
                result = KonomiTVBS4KCloudRC.request(endpoint, 'operations/stat', KonomiTVBS4KCloudStatRequest(
                    fs=str(sidecar_source.parent), remote=sidecar_source.name,
                ))
                item = result.get('item') if isinstance(result, dict) else None
                # rclone側でも既知サイズの通常ファイルとして到達できなければ、転送を始めない。
                if (not isinstance(item, dict) or item.get('IsDir') is not False or
                    type(item.get('Size')) is not int or item['Size'] != before.st_size):
                    raise ValueError('Shared recording source is unavailable or changed.')
                cls._runJob(owner_id, connection_id, endpoint, 'operations/copyfile', KonomiTVBS4KCloudCopyRequest(
                    srcFs=str(sidecar_source.parent), srcRemote=sidecar_source.name, dstFs=remote, dstRemote=destination,
                    _async=True, _config=KonomiTVBS4KCloudCopyOptions(IgnoreTimes=True),
                ), timeout, cancellation_requested)
                # 開いたinodeだけでなく同じパスの置換も検出する。コピー成功は原本削除の許可ではない。
                for current in (os.fstat(original.fileno()), source.stat(follow_symlinks=False)):
                    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                        (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)):
                        raise ValueError('Recording source changed during transfer.')
