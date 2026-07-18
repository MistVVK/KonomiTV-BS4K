from __future__ import annotations

import asyncio
import errno
import json
import math
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, cast

from app import logging
from app.models.RecordedVideo import RecordedVideo


# CM解析native runtimeと録画先directory flockはPOSIX環境専用だが、このmodule
# 自体は他platformでもimportされ得る。fcntlがなければworkspace作成を明示的に
# 利用不能として扱い、server全体をImportErrorで停止させない。
try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None


TemporaryStorageErrorCode = Literal[
    'TemporaryStorageUnavailable',
    'TemporaryStorageInsufficient',
]


class CMAnalysisWorkspaceError(OSError):
    """CM解析用一時領域を安全に用意できないことを表す。"""

    def __init__(self, error_code: TemporaryStorageErrorCode, message: str) -> None:
        """構造化エラーコードと利用者向けメッセージを保持する。

        Args:
            error_code: CM解析結果へ保存する一時領域エラーコード。
            message: 録画パスを含まない診断メッセージ。
        """

        super().__init__(message)
        self.error_code = error_code


class TemporaryStorageUnavailableError(CMAnalysisWorkspaceError):
    """録画と同じファイルシステムへ一時領域を作れないことを表す。"""

    def __init__(self, message: str = 'CM analysis temporary storage is unavailable.') -> None:
        """利用不能エラーを初期化する。

        Args:
            message: 録画パスを含まない診断メッセージ。
        """

        super().__init__('TemporaryStorageUnavailable', message)


class TemporaryStorageInsufficientError(CMAnalysisWorkspaceError):
    """録画と同じファイルシステムの空き容量が不足していることを表す。"""

    def __init__(
        self,
        required_bytes: int,
        available_bytes: int,
        message: str | None = None,
    ) -> None:
        """必要量と利用可能量を含む容量不足エラーを初期化する。

        Args:
            required_bytes: CM解析開始前に必要と判定したバイト数。
            available_bytes: 一般ユーザーが実際に利用できる空きバイト数。
            message: 事前容量判定以外で容量不足になった場合の診断メッセージ。
        """

        super().__init__(
            'TemporaryStorageInsufficient',
            message or (
                f'CM analysis requires {required_bytes} bytes of temporary storage, '
                f'but only {available_bytes} bytes are available.'
            ),
        )
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes


@dataclass(slots=True)
class CMAnalysisWorkspace:
    """録画と同じファイルシステム上の排他的なCM解析作業領域を管理する。"""

    ROOT_DIRECTORY_NAME: ClassVar[str] = '.konomitv-cm-analysis'
    LAYOUT_VERSION: ClassVar[int] = 1
    ROOT_MARKER_NAME: ClassVar[str] = '.workspace-root.json'
    JOB_MARKER_NAME: ClassVar[str] = '.workspace-owner.json'
    LOCK_FILE_NAME: ClassVar[str] = '.workspace.lock'
    AUDIO_BYTES_PER_SECOND: ClassVar[int] = 96_000
    MINIMUM_HEADROOM_BYTES: ClassVar[int] = 128 * 1024 * 1024
    FIXED_RESERVE_BYTES: ClassVar[int] = 2 * 1024 * 1024 * 1024
    _JOB_NAME_PATTERN: ClassVar[re.Pattern[str]] = re.compile(r'^(?P<recorded_video_id>\d+)-(?P<token>[0-9a-f]{32})$')
    _MARKER_APPLICATION: ClassVar[str] = 'KonomiTV-CMAnalysisWorkspace'

    path: Path
    required_bytes: int
    available_bytes: int
    _lock_fd: int
    _closed: bool = False

    @classmethod
    async def create(
        cls,
        source_path: Path,
        recorded_video_id: int,
        duration_seconds: float,
    ) -> CMAnalysisWorkspace:
        """録画の親ディレクトリへ容量確認済みの排他的workspaceを作る。

        Args:
            source_path: 解析対象録画のパス。
            recorded_video_id: workspace名とmarkerへ保存する録画ID。
            duration_seconds: PCM正規化後の必要容量を見積もる録画尺。

        Returns:
            lockを保持した作成済みworkspace。

        Raises:
            TemporaryStorageUnavailableError: 親領域を安全に利用できない場合。
            TemporaryStorageInsufficientError: ``f_bavail`` 基準の空き容量が不足する場合。
        """

        if _fcntl is None:
            raise TemporaryStorageUnavailableError(
                'CM analysis temporary storage requires POSIX advisory locking.',
            )

        try:
            resolved_source_path = await asyncio.to_thread(source_path.resolve, strict=True)
            source_stat = await asyncio.to_thread(resolved_source_path.stat)
            if stat.S_ISREG(source_stat.st_mode) is False:
                raise OSError('CM analysis source is not a regular file.')
            if math.isfinite(duration_seconds) is False or duration_seconds < 0:
                raise OSError('CM analysis duration is invalid.')
            filesystem = await asyncio.to_thread(os.statvfs, resolved_source_path.parent)
        except OSError as ex:
            raise TemporaryStorageUnavailableError() from ex

        required_bytes = cls.calculateRequiredBytes(source_stat.st_size, duration_seconds)
        available_bytes = max(0, filesystem.f_bavail) * max(1, filesystem.f_frsize)
        if available_bytes < required_bytes:
            raise TemporaryStorageInsufficientError(required_bytes, available_bytes)

        creation_task = asyncio.create_task(asyncio.to_thread(
            cls._createSynchronous,
            resolved_source_path.parent,
            recorded_video_id,
            required_bytes,
        ))
        cancellation_requested = False
        while creation_task.done() is False:
            try:
                await asyncio.shield(creation_task)
            except asyncio.CancelledError:
                # worker threadはcancelできないため、戻り値を捨てず必ずjoinしてから後始末する。
                cancellation_requested = True
            except Exception:
                # 完了したworkerの例外は下のresult()で構造化して再送する。
                break
        try:
            workspace = creation_task.result()
        except CMAnalysisWorkspaceError:
            if cancellation_requested is True:
                raise asyncio.CancelledError
            raise
        except OSError as ex:
            if cancellation_requested is True:
                raise asyncio.CancelledError
            if ex.errno in (errno.ENOSPC, errno.EDQUOT):
                raise TemporaryStorageInsufficientError(
                    required_bytes,
                    available_bytes,
                    'CM analysis temporary storage could not be allocated because free space, '
                    'quota, or filesystem capacity became insufficient.',
                ) from ex
            raise TemporaryStorageUnavailableError() from ex

        if cancellation_requested is True:
            try:
                await workspace.cleanup()
            except asyncio.CancelledError:
                # cleanup()は再cancel時も削除とunlockを完了してからこの例外を送出する。
                pass
            except Exception as ex:
                logging.warning('[CMAnalysisWorkspace] Failed to clean a cancelled workspace:', exc_info=ex)
            raise asyncio.CancelledError
        return workspace

    @classmethod
    def calculateRequiredBytes(cls, source_size: int, duration_seconds: float) -> int:
        """canonical MKV・PCM音声・安全余白を含む必要容量を返す。

        Args:
            source_size: 元録画ファイルのバイト数。
            duration_seconds: 元録画の秒数。

        Returns:
            workspace作成前に要求する空き容量のバイト数。
        """

        normalized_source_size = max(0, source_size)
        audio_size = math.ceil(max(0.0, duration_seconds) * cls.AUDIO_BYTES_PER_SECOND)
        variable_headroom = math.ceil(normalized_source_size * 0.05)
        return (
            normalized_source_size
            + audio_size
            + max(variable_headroom, cls.MINIMUM_HEADROOM_BYTES)
            + cls.FIXED_RESERVE_BYTES
        )

    @classmethod
    def isWorkspacePath(cls, path: Path) -> bool:
        """パスが予約workspace root自身またはその配下かを返す。

        Args:
            path: 録画スキャナーが検査するパス。

        Returns:
            予約ディレクトリ名が完全なパス要素として含まれる場合は ``True``。
        """

        return cls.ROOT_DIRECTORY_NAME in path.parts

    async def cleanup(self) -> None:
        """lockを保持したまま自身のjob directoryだけを削除する。

        Returns:
            None
        """

        if self._closed is True:
            return
        self._closed = True
        cleanup_task = asyncio.create_task(asyncio.to_thread(shutil.rmtree, self.path))
        cancellation_requested = False
        try:
            while cleanup_task.done() is False:
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    # 複数回cancelされても削除スレッドへcancelを伝播させず、lockを最後まで保持する。
                    cancellation_requested = True
            cleanup_task.result()
        except FileNotFoundError:
            pass
        finally:
            try:
                assert _fcntl is not None
                _fcntl.flock(self._lock_fd, _fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
        try:
            await asyncio.to_thread(self._removeEmptyRoot, self.path.parent)
        except OSError:
            # 別job・不明なentry・並行作成があれば予約rootをそのまま保持する。
            pass
        if cancellation_requested is True:
            raise asyncio.CancelledError

    @classmethod
    async def cleanupStale(cls) -> None:
        """DBに残る録画親ディレクトリから前プロセスのworkspaceだけを回収する。

        Returns:
            None
        """

        if _fcntl is None:
            return

        try:
            file_paths = cast(list[str], await RecordedVideo.all().values_list('file_path', flat=True))
            parents = await asyncio.to_thread(cls._resolveWorkspaceParents, file_paths)
            await cls.cleanupStaleInParents(parents)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            # 一時領域の掃除失敗だけでKonomiTV全体の起動を妨げない。
            logging.warning('[CMAnalysisWorkspace] Failed to enumerate stale workspaces:', exc_info=ex)

    @staticmethod
    def _resolveWorkspaceParents(file_paths: list[str]) -> set[Path]:
        """DB上の録画パスからworkspaceが実際に置かれる親を解決する。

        Args:
            file_paths: DBに保存された録画パス。

        Returns:
            録画ファイル自体がsymlinkの場合も実体側を指す親ディレクトリ集合。
        """

        parents: set[Path] = set()
        for file_path in file_paths:
            path = Path(file_path)
            try:
                parents.add(path.resolve(strict=True).parent)
            except (OSError, RuntimeError):
                # 録画が既に消えていても、通常ファイルなら保存パス側の残骸を回収できる。
                # strict=Falseなら残っているfile symlinkの参照先も可能な範囲で解決される。
                try:
                    parents.add(path.resolve(strict=False).parent)
                except (OSError, RuntimeError):
                    parents.add(path.parent)
        return parents

    @classmethod
    async def cleanupStaleInParents(cls, parents: set[Path]) -> None:
        """指定された録画親ディレクトリにある非稼働workspaceを回収する。

        Args:
            parents: DB登録済み録画から得た重複除去済みの親ディレクトリ。

        Returns:
            None
        """

        if _fcntl is None:
            return

        for parent in parents:
            try:
                await asyncio.to_thread(cls._cleanupStaleInParentSynchronous, parent)
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                # ある録画先の権限・I/O問題がほかの録画先や起動処理へ波及しないよう継続する。
                logging.warning('[CMAnalysisWorkspace] Failed to clean a stale workspace root:', exc_info=ex)

    @classmethod
    def _createSynchronous(
        cls,
        source_parent: Path,
        recorded_video_id: int,
        required_bytes: int,
    ) -> CMAnalysisWorkspace:
        """workspaceとmarker/lockを同期的に作成する。

        Args:
            source_parent: symlinkを解決した録画実体の親ディレクトリ。
            recorded_video_id: workspaceを所有する録画ID。
            required_bytes: 事前計算済みの必要容量。

        Returns:
            lockを保持したworkspace。
        """

        parent_lock_fd = cls._lockDirectory(source_parent)
        try:
            # 非同期の事前確認から実際の作成までに空き容量が減った場合も、
            # 古いf_bavail値で開始しないよう録画親のprocess間排他中に再確認する。
            filesystem = os.statvfs(source_parent)
            available_bytes = max(0, filesystem.f_bavail) * max(1, filesystem.f_frsize)
            if available_bytes < required_bytes:
                raise TemporaryStorageInsufficientError(required_bytes, available_bytes)
            root = source_parent / cls.ROOT_DIRECTORY_NAME
            cls._ensureOwnedRoot(root)
            job_name = f'{recorded_video_id}-{uuid.uuid4().hex}'
            path = root / job_name
            path.mkdir(mode=0o700)
            lock_fd = -1
            try:
                job_stat = path.lstat()
                if stat.S_ISDIR(job_stat.st_mode) is False or job_stat.st_uid != os.geteuid():
                    raise OSError('CM analysis workspace ownership validation failed.')
                os.chmod(path, 0o700, follow_symlinks=False)
                lock_fd = os.open(
                    path / cls.LOCK_FILE_NAME,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
                    0o600,
                )
                assert _fcntl is not None
                _fcntl.flock(lock_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                cls._writeMarker(
                    path / cls.JOB_MARKER_NAME,
                    {
                        'application': cls._MARKER_APPLICATION,
                        'kind': 'job',
                        'layout_version': cls.LAYOUT_VERSION,
                        'name': job_name,
                        'recorded_video_id': recorded_video_id,
                        'owner_uid': os.geteuid(),
                        'owner_pid': os.getpid(),
                    },
                )
                return cls(path, required_bytes, available_bytes, lock_fd)
            except Exception:
                if lock_fd >= 0:
                    try:
                        assert _fcntl is not None
                        _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)
                shutil.rmtree(path, ignore_errors=True)
                try:
                    cls._removeEmptyRootUnderParentLock(root)
                except OSError:
                    pass
                raise
        finally:
            cls._unlockDirectory(parent_lock_fd)

    @classmethod
    def _ensureOwnedRoot(cls, root: Path) -> None:
        """予約rootを作成し、既存時はmarker・owner・実体を検証する。

        Args:
            root: 作成または検証する予約root。

        Returns:
            None
        """

        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            root_stat = root.lstat()
            if (
                stat.S_ISDIR(root_stat.st_mode) is False
                or root.is_symlink()
                or root_stat.st_uid != os.geteuid()
            ):
                raise OSError('CM analysis workspace root is not an owned real directory.')
            marker = cls._readMarker(root / cls.ROOT_MARKER_NAME)
            if marker != cls._rootMarker():
                raise OSError('CM analysis workspace root marker is invalid.')
        else:
            try:
                cls._writeMarker(root / cls.ROOT_MARKER_NAME, cls._rootMarker())
            except Exception:
                shutil.rmtree(root, ignore_errors=True)
                raise
        os.chmod(root, 0o700, follow_symlinks=False)

    @classmethod
    def _cleanupStaleInParentSynchronous(cls, parent: Path) -> None:
        """単一親ディレクトリのmarker検証済み非稼働jobだけを削除する。

        Args:
            parent: DB登録録画から得た親ディレクトリ。

        Returns:
            None
        """

        resolved_parent = parent.resolve(strict=True)
        parent_lock_fd = cls._lockDirectory(resolved_parent)
        try:
            root = resolved_parent / cls.ROOT_DIRECTORY_NAME
            try:
                root_stat = root.lstat()
            except FileNotFoundError:
                return
            if (
                stat.S_ISDIR(root_stat.st_mode) is False
                or root.is_symlink()
                or root_stat.st_uid != os.geteuid()
                or cls._readMarker(root / cls.ROOT_MARKER_NAME) != cls._rootMarker()
            ):
                logging.warning('[CMAnalysisWorkspace] Ignored an unowned or invalid workspace root.')
                return

            with os.scandir(root) as entries:
                for entry in entries:
                    if entry.name == cls.ROOT_MARKER_NAME:
                        continue
                    match = cls._JOB_NAME_PATTERN.fullmatch(entry.name)
                    if match is None or entry.is_dir(follow_symlinks=False) is False or entry.is_symlink():
                        continue
                    path = root / entry.name
                    path_stat = path.lstat()
                    recorded_video_id = int(match.group('recorded_video_id'))
                    marker = cls._readMarker(path / cls.JOB_MARKER_NAME)
                    if (
                        path_stat.st_uid != os.geteuid()
                        or marker is None
                        or marker.get('application') != cls._MARKER_APPLICATION
                        or marker.get('kind') != 'job'
                        or marker.get('layout_version') != cls.LAYOUT_VERSION
                        or marker.get('name') != entry.name
                        or marker.get('recorded_video_id') != recorded_video_id
                        or marker.get('owner_uid') != path_stat.st_uid
                    ):
                        continue
                    cls._removeIfUnlocked(path)
            cls._removeEmptyRootUnderParentLock(root)
        finally:
            cls._unlockDirectory(parent_lock_fd)

    @classmethod
    def _removeIfUnlocked(cls, path: Path) -> None:
        """job lockを非ブロッキング取得できた場合だけディレクトリを削除する。

        Args:
            path: marker検証済みjob directory。

        Returns:
            None
        """

        lock_fd = -1
        try:
            lock_fd = os.open(
                path / cls.LOCK_FILE_NAME,
                os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0),
            )
            lock_stat = os.fstat(lock_fd)
            if stat.S_ISREG(lock_stat.st_mode) is False or lock_stat.st_uid != os.geteuid():
                return
            try:
                assert _fcntl is not None
                _fcntl.flock(lock_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except OSError as ex:
                if ex.errno in (errno.EACCES, errno.EAGAIN):
                    return
                raise
            shutil.rmtree(path)
        except FileNotFoundError:
            return
        finally:
            if lock_fd >= 0:
                try:
                    assert _fcntl is not None
                    _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)

    @classmethod
    def _removeEmptyRoot(cls, root: Path) -> None:
        """marker以外がない所有rootだけをmarkerとともに削除する。

        Args:
            root: 所有markerを検証する予約root。

        Returns:
            None
        """

        parent_lock_fd = cls._lockDirectory(root.parent)
        try:
            cls._removeEmptyRootUnderParentLock(root)
        finally:
            cls._unlockDirectory(parent_lock_fd)

    @classmethod
    def _removeEmptyRootUnderParentLock(cls, root: Path) -> None:
        """録画親の排他取得中に、空の所有rootだけを削除する。"""

        root_stat = root.lstat()
        if (
            stat.S_ISDIR(root_stat.st_mode) is False
            or root.is_symlink()
            or root_stat.st_uid != os.geteuid()
            or cls._readMarker(root / cls.ROOT_MARKER_NAME) != cls._rootMarker()
        ):
            return
        entries = list(os.scandir(root))
        if any(entry.name != cls.ROOT_MARKER_NAME for entry in entries):
            return
        (root / cls.ROOT_MARKER_NAME).unlink()
        root.rmdir()

    @staticmethod
    def _lockDirectory(path: Path) -> int:
        """永続lockファイルを残さず、実ディレクトリ自体を排他的にflockする。"""

        if _fcntl is None:
            raise TemporaryStorageUnavailableError(
                'CM analysis temporary storage requires POSIX advisory locking.',
            )
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
        )
        try:
            if stat.S_ISDIR(os.fstat(fd).st_mode) is False:
                raise OSError('CM analysis workspace parent is not a real directory.')
            assert _fcntl is not None
            _fcntl.flock(fd, _fcntl.LOCK_EX)
            return fd
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    def _unlockDirectory(fd: int) -> None:
        """録画親ディレクトリの排他を解除してfdを閉じる。"""

        try:
            assert _fcntl is not None
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @classmethod
    def _rootMarker(cls) -> dict[str, object]:
        """予約rootの所有権を検証する決定的markerを返す。

        Returns:
            現在のlayoutと実行UIDを含むmarker。
        """

        return {
            'application': cls._MARKER_APPLICATION,
            'kind': 'root',
            'layout_version': cls.LAYOUT_VERSION,
            'owner_uid': os.geteuid(),
        }

    @staticmethod
    def _writeMarker(path: Path, marker: dict[str, object]) -> None:
        """symlinkを追わず排他的にmarkerを書き込む。

        Args:
            path: 排他的に新規作成するmarker path。
            marker: JSONへ直列化する所有情報。

        Returns:
            None
        """

        payload = json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
            0o600,
        )
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _readMarker(path: Path) -> dict[str, object] | None:
        """通常ファイルのmarkerだけをsymlink非追跡で読み込む。

        Args:
            path: 読み込むmarker path。

        Returns:
            妥当なJSON object、または検証できない場合は ``None``。
        """

        fd = -1
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            marker_stat = os.fstat(fd)
            if stat.S_ISREG(marker_stat.st_mode) is False or marker_stat.st_size > 4096:
                return None
            with os.fdopen(fd, 'rb') as marker_file:
                fd = -1
                value = json.loads(marker_file.read().decode())
            return value if isinstance(value, dict) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        finally:
            if fd >= 0:
                os.close(fd)
