"""KonomiTV-BS4K 録画シリーズ ACP の資格情報管理。

Codex / Grok Build のホスト生成済み ``auth.json`` は、Docker Compose が
固定パスへ読み取り専用 mount したファイルだけを明示操作時に取り込む。
Gemini CLI の Google ADC はコピーせず、同じ固定 mount の読取り可否だけを確認する。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from app.constants import DATA_DIR


KonomiTVBS4KACPImportProvider = Literal['codex', 'grok']

_KONOMITV_BS4K_HOST_AUTH_ROOT = Path('/run/konomitv-bs4k-host-auth')
_KONOMITV_BS4K_HOST_AUTH_PATHS: dict[str, Path] = {
    'codex': _KONOMITV_BS4K_HOST_AUTH_ROOT / 'codex' / 'auth.json',
    'grok': _KONOMITV_BS4K_HOST_AUTH_ROOT / 'grok' / 'auth.json',
    'google': _KONOMITV_BS4K_HOST_AUTH_ROOT / 'google' / 'application_default_credentials.json',
}
_KONOMITV_BS4K_ACP_PROFILES_ROOT = DATA_DIR / 'acp-profiles' / 'recorded-series'
_KONOMITV_BS4K_AUTH_FILENAME = 'auth.json'
_KONOMITV_BS4K_IMPORT_MARKER_FILENAME = '.auth-imported.json'
_MAX_CREDENTIAL_FILE_BYTES = 1_048_576


class KonomiTVBS4KACPCredentialError(Exception):
    """資格情報の内容を含まない固定コードだけを公開するエラー。"""

    def __init__(self, code: Literal['HostAuthUnavailable', 'InvalidHostAuth', 'CredentialStorageFailed']) -> None:
        """公開可能な固定エラーコードを保持する。

        Args:
            code: API やテストへ公開してよい固定エラーコード。

        Returns:
            None
        """

        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class KonomiTVBS4KACPCredentialStatus:
    """認証内容を含まない ACP 資格情報の状態。"""

    codex_host_auth_available: bool
    codex_auth_imported: bool
    codex_auth_imported_at: datetime | None
    grok_host_auth_available: bool
    grok_auth_imported: bool
    grok_auth_imported_at: datetime | None
    google_adc_available: bool


class KonomiTVBS4KACPCredentials:
    """固定 mount と KonomiTV-BS4K 専用コピーの間だけで資格情報を管理する。"""

    # 同一プロセス内の import / delete / status を直列化し、marker と auth.json の観測を一貫させる。
    _lock = threading.RLock()

    @classmethod
    def getStatus(cls) -> KonomiTVBS4KACPCredentialStatus:
        """認証内容を読み出さず、各固定ファイルの利用可否と取り込み状態を返す。

        Returns:
            KonomiTVBS4KACPCredentialStatus: API へ安全に公開できる状態。
        """

        with cls._lock:
            codex_imported, codex_imported_at = cls._getImportedStatus('codex')
            grok_imported, grok_imported_at = cls._getImportedStatus('grok')
            return KonomiTVBS4KACPCredentialStatus(
                codex_host_auth_available=cls._isHostCredentialAvailable('codex'),
                codex_auth_imported=codex_imported,
                codex_auth_imported_at=codex_imported_at,
                grok_host_auth_available=cls._isHostCredentialAvailable('grok'),
                grok_auth_imported=grok_imported,
                grok_auth_imported_at=grok_imported_at,
                google_adc_available=cls._isHostCredentialAvailable('google'),
            )

    @classmethod
    def getCredentialGeneration(
        cls,
        provider: Literal['codex', 'grok', 'google'],
    ) -> str:
        """実行に使う資格情報の世代を、内容を露出しない hash で識別する。

        Codex / Grok は明示的な import ごとに更新する marker、Gemini は固定
        read-only mount の ADC を対象にする。OAuth agent は正常な token refresh でも
        auth.json を更新するため、可変な token 本文を能力証明の世代に使わない。
        資格情報が無い・不正な場合も固定値だけを返し、path や JSON、token を
        呼び出し側へ渡さない。

        Args:
            provider: 固定 ACP provider の識別子。

        Returns:
            str: 検証済み JSON bytes の SHA-256、または ``missing``。
        """

        with cls._lock:
            if provider in {'codex', 'grok'}:
                profile_dir = cls._profileDirectory(
                    cast(KonomiTVBS4KACPImportProvider, provider),
                )
                try:
                    # agent が認証失敗時に auth.json を削除した場合は、marker が残っていても
                    # 使用可能な世代として扱わない。marker 自体は明示 import だけが更新する。
                    cls._readValidatedJSONObject(
                        profile_dir / _KONOMITV_BS4K_AUTH_FILENAME,
                    )
                    marker_bytes = cls._readValidatedJSONObject(
                        profile_dir / _KONOMITV_BS4K_IMPORT_MARKER_FILENAME,
                    )
                except KonomiTVBS4KACPCredentialError:
                    return 'missing'
                return hashlib.sha256(marker_bytes).hexdigest()

            try:
                credential_bytes = cls._readValidatedJSONObject(
                    _KONOMITV_BS4K_HOST_AUTH_PATHS['google'],
                )
            except KonomiTVBS4KACPCredentialError:
                return 'missing'
            return hashlib.sha256(credential_bytes).hexdigest()

    @classmethod
    def importProviderAuth(cls, provider: KonomiTVBS4KACPImportProvider) -> datetime:
        """固定 mount の auth.json を専用プロファイルへ atomic に取り込む。

        Args:
            provider: ``codex`` または ``grok``。

        Returns:
            datetime: サーバー側で確定した取り込み日時。

        Raises:
            KonomiTVBS4KACPCredentialError: 入力が不正、または専用コピーを保存できない場合。
        """

        with cls._lock:
            # API から任意 path を受け取らず、provider に紐付いた固定 mount だけを開く。
            source_path = _KONOMITV_BS4K_HOST_AUTH_PATHS[provider]
            credential_bytes = cls._readValidatedJSONObject(source_path)
            profile_dir = cls._profileDirectory(provider)
            destination_path = profile_dir / _KONOMITV_BS4K_AUTH_FILENAME
            marker_path = profile_dir / _KONOMITV_BS4K_IMPORT_MARKER_FILENAME
            imported_at = datetime.now(UTC)
            # auth と marker の二段更新途中失敗時に旧世代へ戻すため、書込前の内容を保持する。
            previous_auth = cls._readExistingFileBytes(destination_path)
            previous_marker = cls._readExistingFileBytes(marker_path)
            # _writeAtomic は replace 成功後の directory fsync 失敗でも OSError を返す。
            # その時点で新 auth は既に残っているため、「書込開始後は常に rollback」とする。
            auth_mutation_started = False

            try:
                cls._ensureProfileDirectory(profile_dir)
                auth_mutation_started = True
                cls._writeAtomic(destination_path, credential_bytes)
                marker_bytes = (
                    json.dumps(
                        {'imported_at': imported_at.isoformat()},
                        ensure_ascii=False,
                        separators=(',', ':'),
                    ) + '\n'
                ).encode('utf-8')
                cls._writeAtomic(marker_path, marker_bytes)
            except OSError as ex:
                if auth_mutation_started:
                    try:
                        cls._restoreOrUnlinkFile(destination_path, previous_auth)
                        cls._restoreOrUnlinkFile(marker_path, previous_marker)
                    except OSError as rollback_error:
                        # 復元不能でも新 auth だけ残る状態を避けるため、両方を最善努力で除去する。
                        try:
                            cls._restoreOrUnlinkFile(destination_path, None)
                        except OSError:
                            pass
                        try:
                            cls._restoreOrUnlinkFile(marker_path, None)
                        except OSError:
                            pass
                        raise KonomiTVBS4KACPCredentialError('CredentialStorageFailed') from rollback_error
                # OS 例外には path が含まれ得るため、呼び出し側へは固定コードだけを返す。
                raise KonomiTVBS4KACPCredentialError('CredentialStorageFailed') from ex
            return imported_at

    @classmethod
    def deleteProviderAuth(cls, provider: KonomiTVBS4KACPImportProvider) -> None:
        """KonomiTV-BS4K 専用コピーと marker だけを冪等に削除する。

        Args:
            provider: ``codex`` または ``grok``。

        Returns:
            None

        Raises:
            KonomiTVBS4KACPCredentialError: 専用コピーを削除できない場合。
        """

        with cls._lock:
            profile_dir = cls._profileDirectory(provider)
            directory_descriptor: int | None = None
            try:
                directory_descriptor = cls._openExistingProfileDirectory(profile_dir)
                # 検証後に保持した directory-fd 配下の entry だけを unlink する。
                # 旧実装の auth symlink が残っていてもリンク先は追跡せず、symlink 自体だけを削除する。
                for filename in (_KONOMITV_BS4K_AUTH_FILENAME, _KONOMITV_BS4K_IMPORT_MARKER_FILENAME):
                    try:
                        os.unlink(filename, dir_fd=directory_descriptor)
                    except FileNotFoundError:
                        pass
                os.fsync(directory_descriptor)
            except FileNotFoundError:
                # profile 自体がまだない場合も削除済みとして扱う。
                return
            except OSError as ex:
                raise KonomiTVBS4KACPCredentialError('CredentialStorageFailed') from ex
            finally:
                if directory_descriptor is not None:
                    os.close(directory_descriptor)

    @classmethod
    def _profileDirectory(cls, provider: KonomiTVBS4KACPImportProvider) -> Path:
        """provider に対応する固定の専用プロファイルパスを返す。

        Args:
            provider: ``codex`` または ``grok``。

        Returns:
            Path: KonomiTV-BS4K 管理下の固定パス。
        """

        return _KONOMITV_BS4K_ACP_PROFILES_ROOT / provider

    @classmethod
    def _ensureProfileDirectory(cls, profile_dir: Path) -> None:
        """管理対象 directory を symlink を許可せず作成する。

        Args:
            profile_dir: provider に対応する固定 profile directory。

        Returns:
            None

        Raises:
            OSError: 管理対象 path が symlink・非 directory、または作成不能な場合。
        """

        profiles_root = _KONOMITV_BS4K_ACP_PROFILES_ROOT
        try:
            relative_root = profiles_root.relative_to(DATA_DIR)
        except ValueError:
            # テスト用に root 定数を差し替えた場合も、差し替え先そのものは検証する。
            managed_directories = [profiles_root]
        else:
            managed_directories = []
            current_directory = DATA_DIR
            for path_component in relative_root.parts:
                current_directory /= path_component
                managed_directories.append(current_directory)
        managed_directories.append(profile_dir)

        for managed_directory in managed_directories:
            if managed_directory.is_symlink():
                raise OSError('Managed ACP profile path must not be a symlink.')
            try:
                managed_stat = managed_directory.lstat()
            except FileNotFoundError:
                managed_directory.mkdir(mode=0o700)
                managed_stat = managed_directory.lstat()
            if stat.S_ISDIR(managed_stat.st_mode) is False:
                raise OSError('Managed ACP profile path must be a directory.')
            os.chmod(managed_directory, 0o700)

    @classmethod
    def _validateExistingProfileDirectory(cls, profile_dir: Path) -> None:
        """既存の管理対象 directory 階層が symlink を含まないことを確認する。

        Args:
            profile_dir: provider に対応する固定 profile directory。

        Returns:
            None

        Raises:
            FileNotFoundError: profile directory がまだ存在しない場合。
            OSError: 管理対象 path が symlink または非 directory の場合。
        """

        profiles_root = _KONOMITV_BS4K_ACP_PROFILES_ROOT
        try:
            relative_root = profiles_root.relative_to(DATA_DIR)
        except ValueError:
            managed_directories = [profiles_root]
        else:
            managed_directories = []
            current_directory = DATA_DIR
            for path_component in relative_root.parts:
                current_directory /= path_component
                managed_directories.append(current_directory)
        managed_directories.append(profile_dir)

        for managed_directory in managed_directories:
            managed_stat = managed_directory.lstat()
            if stat.S_ISDIR(managed_stat.st_mode) is False:
                raise OSError('Managed ACP profile path must be a directory.')

    @classmethod
    def _openExistingProfileDirectory(cls, profile_dir: Path) -> int:
        """管理 root から symlink を辿らず provider directory を開く。

        Args:
            profile_dir: provider に対応する固定 profile directory。

        Returns:
            int: 呼び出し側が閉じる provider directory の file descriptor。

        Raises:
            OSError: directory がない、symlink・非 directory、または管理 root 外の場合。
        """

        profiles_root = _KONOMITV_BS4K_ACP_PROFILES_ROOT
        try:
            relative_profile = profile_dir.relative_to(DATA_DIR)
        except ValueError:
            # テストで管理 root を差し替えた場合も、root 自体を O_NOFOLLOW で固定する。
            current_descriptor = os.open(
                profiles_root,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            relative_profile = profile_dir.relative_to(profiles_root)
        else:
            current_descriptor = os.open(
                DATA_DIR,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            )

        try:
            for path_component in relative_profile.parts:
                next_descriptor = os.open(
                    path_component,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current_descriptor,
                )
                os.close(current_descriptor)
                current_descriptor = next_descriptor
            return current_descriptor
        except BaseException:
            os.close(current_descriptor)
            raise

    @classmethod
    def _isHostCredentialAvailable(cls, provider: Literal['codex', 'grok', 'google']) -> bool:
        """固定 mount が安全に読み取れる JSON object かを判定する。

        Args:
            provider: 検査する固定 mount の識別子。

        Returns:
            bool: 通常ファイルかつ制限内の JSON object の場合だけ True。
        """

        try:
            cls._readValidatedJSONObject(_KONOMITV_BS4K_HOST_AUTH_PATHS[provider])
        except KonomiTVBS4KACPCredentialError:
            return False
        return True

    @classmethod
    def _getImportedStatus(
        cls,
        provider: KonomiTVBS4KACPImportProvider,
    ) -> tuple[bool, datetime | None]:
        """専用 auth.json と marker から取り込み状態を取得する。

        Args:
            provider: ``codex`` または ``grok``。

        Returns:
            tuple[bool, datetime | None]: 0600 の通常ファイルであるかと、marker 内の取り込み日時。
        """

        profile_dir = cls._profileDirectory(provider)
        auth_path = profile_dir / _KONOMITV_BS4K_AUTH_FILENAME
        try:
            cls._validateExistingProfileDirectory(profile_dir)
            auth_stat = auth_path.lstat()
        except OSError:
            return False, None
        if (
            stat.S_ISREG(auth_stat.st_mode) is False or
            stat.S_IMODE(auth_stat.st_mode) != 0o600
        ):
            return False, None

        marker_path = profile_dir / _KONOMITV_BS4K_IMPORT_MARKER_FILENAME
        try:
            marker_bytes = cls._readValidatedJSONObject(marker_path)
            marker_data = cast(dict[str, object], json.loads(marker_bytes.decode('utf-8')))
            imported_at_value = marker_data.get('imported_at')
            if not isinstance(imported_at_value, str):
                return True, None
            imported_at = datetime.fromisoformat(imported_at_value)
            if imported_at.tzinfo is None:
                return True, None
            return True, imported_at
        except (KonomiTVBS4KACPCredentialError, UnicodeDecodeError, ValueError):
            return True, None

    @classmethod
    def _readValidatedJSONObject(cls, path: Path) -> bytes:
        """symlink を追跡せず、上限内の UTF-8 JSON object を読み込む。

        Args:
            path: 内部定数または固定 profile から構築済みのファイルパス。

        Returns:
            bytes: 検証済みの元ファイル内容。

        Raises:
            KonomiTVBS4KACPCredentialError: ファイルがない、通常ファイルでない、または内容が不正な場合。
        """

        file_descriptor: int | None = None
        try:
            # O_NONBLOCK により FIFO を誤って開いても待機せず、fstat() で通常ファイル以外を拒否する。
            file_descriptor = os.open(
                path,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
        except FileNotFoundError as ex:
            raise KonomiTVBS4KACPCredentialError('HostAuthUnavailable') from ex
        except OSError as ex:
            raise KonomiTVBS4KACPCredentialError('InvalidHostAuth') from ex

        try:
            file_stat = os.fstat(file_descriptor)
            if stat.S_ISREG(file_stat.st_mode) is False:
                raise KonomiTVBS4KACPCredentialError('InvalidHostAuth')
            if file_stat.st_size < 1 or file_stat.st_size > _MAX_CREDENTIAL_FILE_BYTES:
                raise KonomiTVBS4KACPCredentialError('InvalidHostAuth')

            chunks: list[bytes] = []
            total_bytes = 0
            while True:
                chunk = os.read(
                    file_descriptor,
                    min(65_536, _MAX_CREDENTIAL_FILE_BYTES + 1 - total_bytes),
                )
                if chunk == b'':
                    break
                chunks.append(chunk)
                total_bytes += len(chunk)
                if total_bytes > _MAX_CREDENTIAL_FILE_BYTES:
                    raise KonomiTVBS4KACPCredentialError('InvalidHostAuth')
            content = b''.join(chunks)
            if len(content) < 1:
                raise KonomiTVBS4KACPCredentialError('InvalidHostAuth')
            try:
                parsed = json.loads(content.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError) as ex:
                raise KonomiTVBS4KACPCredentialError('InvalidHostAuth') from ex
            if isinstance(parsed, dict) is False:
                raise KonomiTVBS4KACPCredentialError('InvalidHostAuth')
            return content
        except OSError as ex:
            raise KonomiTVBS4KACPCredentialError('InvalidHostAuth') from ex
        finally:
            os.close(file_descriptor)

    @classmethod
    def _readExistingFileBytes(cls, path: Path) -> bytes | None:
        """既存の通常ファイルを読み取り、無い場合は None を返す。

        Args:
            path: 読み取る固定 profile 配下の path。

        Returns:
            bytes | None: 既存内容。未作成または読めない場合は None。
        """

        try:
            if path.is_file() is False or path.is_symlink():
                return None
            return path.read_bytes()
        except OSError:
            return None

    @classmethod
    def _restoreOrUnlinkFile(cls, path: Path, previous_content: bytes | None) -> None:
        """二段更新の rollback として、旧内容を復元するかファイルを削除する。

        Args:
            path: 復元または削除する固定 path。
            previous_content: 書込前の内容。None の場合はファイルを unlink する。

        Returns:
            None

        Raises:
            OSError: 復元または削除に失敗した場合。
        """

        if previous_content is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            if path.parent.is_dir():
                cls._fsyncDirectory(path.parent)
            return
        cls._writeAtomic(path, previous_content)

    @classmethod
    def _writeAtomic(cls, destination_path: Path, content: bytes) -> None:
        """0600 の一時ファイルを fsync して同一 directory 内で atomic rename する。

        Args:
            destination_path: KonomiTV-BS4K 専用 profile 配下の固定保存先。
            content: 保存する検証済み bytes。

        Returns:
            None

        Raises:
            OSError: directory またはファイルを安全に更新できない場合。
        """

        temporary_name = f'.{destination_path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp'
        directory_descriptor = os.open(
            destination_path.parent,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        file_descriptor: int | None = None
        try:
            try:
                os.fchmod(directory_descriptor, 0o700)
                file_descriptor = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_descriptor,
                )
                written_bytes = 0
                while written_bytes < len(content):
                    written_bytes += os.write(file_descriptor, content[written_bytes:])
                os.fsync(file_descriptor)
                os.fchmod(file_descriptor, 0o600)
                os.close(file_descriptor)
                file_descriptor = None
                os.replace(
                    temporary_name,
                    destination_path.name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                )
                os.fsync(directory_descriptor)
            finally:
                # 一時ファイル削除の例外でも directory FD を必ず閉じる。
                if file_descriptor is not None:
                    try:
                        os.close(file_descriptor)
                    except OSError:
                        pass
                    file_descriptor = None
                try:
                    os.unlink(temporary_name, dir_fd=directory_descriptor)
                except FileNotFoundError:
                    pass
        finally:
            os.close(directory_descriptor)

    @staticmethod
    def _fsyncDirectory(directory_path: Path) -> None:
        """rename / unlink の directory entry を永続化する。

        Args:
            directory_path: fsync する固定 profile directory。

        Returns:
            None

        Raises:
            OSError: directory を開くか fsync できない場合。
        """

        directory_descriptor = os.open(directory_path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
