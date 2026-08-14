from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import ClassVar, Literal, TypeVar, cast

from app import logging
from app.config import Config
from app.models.RecordedVideo import RecordedVideo


RecordedFMP4CacheKind = Literal['video-init', 'video', 'audio-init', 'audio']
_T = TypeVar('_T')


@dataclass(frozen=True, slots=True)
class RecordedFMP4Variant:
    """映像・音声fMP4キャッシュを一意に識別する生成条件を表す。"""

    # 生成パイプラインの変更時にこの値を上げ、互換性のない旧キャッシュとの衝突を防ぐ。
    # キャッシュの配置形式は変わらないため、LAYOUT_VERSION とは独立した内部改訂値とする。
    PIPELINE_REVISION: ClassVar[int] = 7

    quality: str
    codec: str
    bit_depth: int
    is_24fps: bool
    backend: str
    configuration_generation: int
    seek_generation: int
    rendition: str | None = None
    # 通常再生とオフライン保存では映像ビットレートと音声の連続生成単位が異なるため、
    # 同じ画質・codecでも生成物を相互流用しないよう配信用途をdigestへ含める。
    delivery_mode: Literal['Playback', 'Offline'] = 'Playback'

    def digest(self) -> str:
        """フィールド順に依存しない安定した短縮SHA-256を返す。"""

        # dataclass の生成条件と内部パイプライン改訂値の両方をdigestへ含める。
        # ClassVar は asdict() に含まれないため、専用のメタデータとして明示的に直列化する。
        serialized = json.dumps(
            {
                'pipeline_revision': self.PIPELINE_REVISION,
                'variant': asdict(self),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        return hashlib.sha256(serialized.encode()).hexdigest()[:24]


class KonomiTVBS4KRecordedFMP4PathLock:
    """同じ task からの再入を許可する BS4K fMP4 path 単位 async lock。"""

    def __init__(self) -> None:
        """未取得の path lock を初期化する。

        Returns:
            None
        """

        # task 間の排他を担う基底 Lock。path ごとに Manager が1個だけ生成する。
        self._lock = asyncio.Lock()
        # 現在 Lock を所有する task。writeAtomic() の同一 task 再入判定に使う。
        self._owner: asyncio.Task[object] | None = None
        # owner task が再入した深さ。0 へ戻った時だけ基底 Lock を解放する。
        self._depth = 0

    async def acquire(self) -> bool:
        """現在 task で lock を取得する。

        Returns:
            取得に成功した場合は常に True。

        Raises:
            RuntimeError: asyncio task 外から呼ばれた場合。
        """

        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError('Recorded fMP4 path lock requires an asyncio task.')
        if self._owner is current_task:
            self._depth += 1
            return True
        await self._lock.acquire()
        self._owner = current_task
        self._depth = 1
        return True

    def release(self) -> None:
        """現在 task が所有する lock を1段解放する。

        Returns:
            None

        Raises:
            RuntimeError: 所有していない task から解放された場合。
        """

        current_task = asyncio.current_task()
        if current_task is None or self._owner is not current_task or self._depth <= 0:
            raise RuntimeError('Recorded fMP4 path lock is not owned by the current task.')
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()

    def locked(self) -> bool:
        """いずれかの task が path lock を所有しているか返す。

        Returns:
            所有中なら True。
        """

        return self._lock.locked()

    async def __aenter__(self) -> KonomiTVBS4KRecordedFMP4PathLock:
        """async with 開始時に lock を取得する。

        Returns:
            この path lock 自身。
        """

        await self.acquire()
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """async with 終了時に lock を解放する。

        Args:
            _exc_type: block で発生した例外型。
            _exc_value: block で発生した例外。
            _traceback: block で発生した例外の traceback。

        Returns:
            None
        """

        self.release()


class RecordedFMP4CacheManager:
    """録画ファイルと同階層または指定フォルダ上のfMP4キャッシュを管理する。"""

    LAYOUT_VERSION: ClassVar[int] = 1
    RELEASE_DELAY_SECONDS: ClassVar[float] = 60.0
    FILE_PREFIX: ClassVar[str] = f'.konomitv-bs4k-fmp4-v{LAYOUT_VERSION}-'
    _FILE_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        rf'^\.konomitv-bs4k-fmp4-v{LAYOUT_VERSION}-\d+-[0-9a-f]+-[0-9a-f]{{24}}-'
        r'(?:video-init-\d+\.mp4|video-\d+\.m4s|audio-[A-Za-z0-9_-]+-\d+-init\.mp4|'
        r'audio-[A-Za-z0-9_-]+-\d+\.m4s)(?:\.tmp-[0-9a-f-]+)?$',
    )
    # bd4e0b2c のnamespace移行前に生成された既知形式だけをmigration cleanup対象にする。
    # layoutやsuffixの未知形式まで広げると第三者ファイルを所有物と誤認するため、旧v1へ完全固定する。
    _LEGACY_FILE_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r'^\.konomitv-fmp4-v1-\d+-[0-9a-f]+-[0-9a-f]{24}-'
        r'(?:video-init-\d+\.mp4|video-\d+\.m4s|audio-[A-Za-z0-9_-]+-\d+-init\.mp4|'
        r'audio-[A-Za-z0-9_-]+-\d+\.m4s)(?:\.tmp-[0-9a-f-]+)?$',
    )
    _references: ClassVar[dict[str, set[str]]] = {}
    _release_tasks: ClassVar[dict[str, asyncio.Task[None]]] = {}
    _locks: ClassVar[dict[str, KonomiTVBS4KRecordedFMP4PathLock]] = {}
    # lock 待機中も含む Manager 内操作数。0 になった時だけ registry から安全に破棄する。
    _lock_users: ClassVar[dict[str, int]] = {}
    # writeAtomic() が open/fsync 中の一時 path を保護し、watcher が途中 unlink しないようにする
    _in_progress_paths: ClassVar[set[str]] = set()

    @classmethod
    def isCacheFileName(cls, file_name: str) -> bool:
        """ファイル名が現行または既知legacyのfMP4予約形式と完全一致するかを返す。"""

        return (
            cls._FILE_PATTERN.fullmatch(file_name) is not None
            or cls._LEGACY_FILE_PATTERN.fullmatch(file_name) is not None
        )

    @staticmethod
    def getCacheFolder(recorded_video: RecordedVideo) -> Path:
        """設定値または元動画の親フォルダから実際の保存先を解決する。"""

        configured_folder = Config().video.recorded_fmp4_cache_folder
        return configured_folder if configured_folder is not None else Path(recorded_video.file_path).parent

    @classmethod
    def buildPath(
        cls,
        recorded_video: RecordedVideo,
        variant: RecordedFMP4Variant,
        kind: RecordedFMP4CacheKind,
        index: int,
    ) -> Path:
        """録画・生成条件・メディア種別から予約キャッシュパスを組み立てる。"""

        prefix = (
            f'{cls.FILE_PREFIX}{recorded_video.id}-{recorded_video.file_hash}-{variant.digest()}-'
        )
        if kind == 'video-init':
            suffix = f'video-init-{index}.mp4'
        elif kind == 'video':
            suffix = f'video-{index}.m4s'
        elif kind == 'audio-init':
            if variant.rendition is None:
                raise ValueError('Audio cache requires a rendition ID.')
            suffix = f'audio-{cls.__normalizeRendition(variant.rendition)}-{index}-init.mp4'
        else:
            if variant.rendition is None:
                raise ValueError('Audio cache requires a rendition ID.')
            suffix = f'audio-{cls.__normalizeRendition(variant.rendition)}-{index}.m4s'
        return cls.getCacheFolder(recorded_video) / f'{prefix}{suffix}'

    @classmethod
    @asynccontextmanager
    async def __pathLock(
        cls,
        cache_path: Path,
    ) -> AsyncGenerator[KonomiTVBS4KRecordedFMP4PathLock, None]:
        """Manager 内操作を cache path 固有 lock で直列化する。

        Args:
            cache_path: 状態またはファイルを操作する cache path。

        Yields:
            取得済みの reentrant path lock。
        """

        cache_key = str(cache_path)
        path_lock = cls._locks.get(cache_key)
        if path_lock is None:
            path_lock = KonomiTVBS4KRecordedFMP4PathLock()
            cls._locks[cache_key] = path_lock
        # lock 取得待ちの操作も registry user に数え、待機中に別 lock へ差し替わることを防ぐ。
        cls._lock_users[cache_key] = cls._lock_users.get(cache_key, 0) + 1
        try:
            async with path_lock:
                yield path_lock
        finally:
            remaining_users = cls._lock_users.get(cache_key, 1) - 1
            if remaining_users > 0:
                cls._lock_users[cache_key] = remaining_users
            else:
                cls._lock_users.pop(cache_key, None)
                cls.__discardPathLockIfUnused(cache_key, path_lock)

    @classmethod
    def __discardPathLockIfUnused(
        cls,
        cache_key: str,
        path_lock: KonomiTVBS4KRecordedFMP4PathLock,
    ) -> None:
        """状態も待機操作もない path lock だけを registry から破棄する。

        Args:
            cache_key: 対象 path の文字列表現。
            path_lock: 破棄候補の lock instance。

        Returns:
            None
        """

        if (
            cls._locks.get(cache_key) is path_lock
            and cls._lock_users.get(cache_key, 0) == 0
            and cache_key not in cls._references
            and cache_key not in cls._release_tasks
            and cache_key not in cls._in_progress_paths
            and path_lock.locked() is False
        ):
            cls._locks.pop(cache_key, None)

    @staticmethod
    async def __runThreadWorker(function: Callable[[], _T]) -> _T:
        """cancel 時も to_thread worker の完了を待ってから CancelledError を再送出する。

        Args:
            function: thread で実行する引数なし同期関数。

        Returns:
            同期関数の戻り値。

        Raises:
            asyncio.CancelledError: worker 完了後に呼び出し task の cancel を再送出する。
            Exception: 通常実行中に同期関数が送出した例外。
        """

        worker_task = asyncio.create_task(asyncio.to_thread(function))
        try:
            return await asyncio.shield(worker_task)
        except asyncio.CancelledError as cancelled_error:
            # repeated cancel でも worker が終わるまでは戻らず、path lock と in-progress を保持する。
            while worker_task.done() is False:
                try:
                    await asyncio.shield(worker_task)
                except asyncio.CancelledError:
                    continue
            try:
                worker_task.result()
            except Exception as worker_error:
                logging.warning(
                    '[RecordedFMP4CacheManager] Thread worker failed during cancellation: '
                    f'{worker_error}',
                )
            raise cancelled_error

    @staticmethod
    async def __waitReleaseTask(release_task: asyncio.Task[None] | None) -> None:
        """cancel 済みの遅延削除 task が finally を終えるまで待つ。

        Args:
            release_task: 待機対象。None なら何もしない。

        Returns:
            None

        Raises:
            asyncio.CancelledError: 遅延削除完了後に呼び出し task の cancel を再送出する。
        """

        if release_task is None:
            return
        caller_cancelled: asyncio.CancelledError | None = None
        while release_task.done() is False:
            try:
                # wait() は待機対象の cancel を呼び出し側へ伝播せず正常に完了するため、
                # release_task 自身の cancel と呼び出し側 task の cancel を明確に分離できる。
                await asyncio.wait({release_task})
            except asyncio.CancelledError as cancelled_error:
                # 呼び出し側が cancel されても、release_task の finally が path lock 状態を
                # 確定するまでは待機を継続し、その後に元の CancelledError を再送出する。
                caller_cancelled = cancelled_error
        try:
            release_task.result()
        except asyncio.CancelledError:
            pass
        except Exception as error:
            logging.warning(
                f'[RecordedFMP4CacheManager] Delayed cache cleanup failed: {error}',
            )
        if caller_cancelled is not None:
            raise caller_cancelled

    @classmethod
    async def writeAtomic(cls, destination: Path, data: bytes) -> None:
        """同じディレクトリの一時ファイルをfsync後、完成パスへatomic renameする。"""

        # migration cleanupで認識する旧prefixを新規生成へ再利用させず、書込みは現行namespaceへ限定する。
        if cls._FILE_PATTERN.fullmatch(destination.name) is None:
            raise ValueError(f'Unmanaged fMP4 cache path: {destination}')
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        temporary_path = destination.with_name(f'{destination.name}.tmp-{uuid.uuid4()}')
        temporary_key = str(temporary_path)
        destination_key = str(destination)

        def WriteAndReplace() -> None:
            """イベントループを塞がずキャッシュを書き込み、ディレクトリエントリまで同期する。"""

            with temporary_path.open('wb') as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            temporary_path.replace(destination)
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

        # destination と一意な tmp の両 lock を保持し、scanner cleanup と delayed delete を直列化する。
        ## generation lock 保持中の同一 task から呼ばれる destination lock は reentrant に取得できる。
        async with cls.__pathLock(destination):
            async with cls.__pathLock(temporary_path):
                cls._in_progress_paths.add(temporary_key)
                cls._in_progress_paths.add(destination_key)
                try:
                    await cls.__runThreadWorker(WriteAndReplace)
                finally:
                    try:
                        await cls.__runThreadWorker(
                            lambda: temporary_path.unlink(missing_ok=True),
                        )
                    finally:
                        cls._in_progress_paths.discard(temporary_key)
                        cls._in_progress_paths.discard(destination_key)

    @classmethod
    async def acquire(
        cls,
        cache_path: Path,
        session_id: str,
    ) -> KonomiTVBS4KRecordedFMP4PathLock:
        """セッション参照を追加し、保留中の60秒削除を解除して生成ロックを返す。"""

        cache_key = str(cache_path)
        release_task: asyncio.Task[None] | None = None
        async with cls.__pathLock(cache_path) as path_lock:
            release_task = cls._release_tasks.pop(cache_key, None)
            if release_task is not None:
                release_task.cancel()
            cls._references.setdefault(cache_key, set()).add(session_id)
        # delayed task の finally は同じ path lock を取るため、lock を解放してから待つ。
        await cls.__waitReleaseTask(release_task)
        return path_lock

    @classmethod
    async def release(cls, cache_path: Path, session_id: str) -> None:
        """セッション参照を外し、最後の参照なら60秒後の削除を予約する。"""

        cache_key = str(cache_path)
        previous_task: asyncio.Task[None] | None = None
        async with cls.__pathLock(cache_path):
            references = cls._references.get(cache_key)
            if references is None:
                return
            references.discard(session_id)
            if len(references) > 0:
                return
            cls._references.pop(cache_key, None)
            previous_task = cls._release_tasks.pop(cache_key, None)
            if previous_task is not None:
                previous_task.cancel()
            cls._release_tasks[cache_key] = asyncio.create_task(
                cls.__deleteAfterDelay(cache_path),
            )
        # previous task の finally と path lock を取り合わないよう、現在の lock 解放後に待つ。
        await cls.__waitReleaseTask(previous_task)

    @classmethod
    async def cleanupStale(cls) -> None:
        """前プロセスが残した予約キャッシュだけを起動時に削除する。"""

        folders: set[Path] = set()
        configured_folder = Config().video.recorded_fmp4_cache_folder
        if configured_folder is not None:
            folders.add(configured_folder)
        file_paths = cast(list[str], await RecordedVideo.all().values_list('file_path', flat=True))
        folders.update(Path(file_path).parent for file_path in file_paths)
        for folder in folders:
            if folder.is_dir() is False:
                continue
            try:
                entries = await asyncio.to_thread(lambda: list(folder.iterdir()))
            except OSError:
                continue
            for entry in entries:
                if entry.is_file() and cls.isCacheFileName(entry.name):
                    # 録画 scanner は既に並行稼働しているため、起動時回収も通常 cleanup と
                    # 同じ path lock・参照再確認・cancel 安全な unlink 規約へ統一する。
                    await cls.cleanupDiscovered(entry)

    @classmethod
    async def cleanupDiscovered(cls, cache_path: Path) -> None:
        """録画スキャンで発見した未参照の予約キャッシュ残骸だけを削除する。"""

        if cls.isCacheFileName(cache_path.name) is False:
            return
        cache_key = str(cache_path)
        release_task: asyncio.Task[None] | None = None
        async with cls.__pathLock(cache_path):
            # 参照確認と unlink worker 完了を同じ lock 内に置き、確認後の acquire 割り込みを防ぐ。
            if cache_key in cls._references or cache_key in cls._in_progress_paths:
                return
            release_task = cls._release_tasks.pop(cache_key, None)
            if release_task is not None:
                release_task.cancel()
            try:
                await cls.__runThreadWorker(
                    lambda: cache_path.unlink(missing_ok=True),
                )
            except OSError:
                logging.warning(f'[RecordedFMP4CacheManager] Failed to remove discovered cache: {cache_path}')
        # canceled delayed task の finally は path lock 解放後に完了させる。
        await cls.__waitReleaseTask(release_task)

    @classmethod
    async def __deleteAfterDelay(cls, cache_path: Path) -> None:
        """再利用猶予を待ってから参照のないキャッシュを削除する。"""

        cache_key = str(cache_path)
        try:
            await asyncio.sleep(cls.RELEASE_DELAY_SECONDS)
            async with cls.__pathLock(cache_path):
                # 再取得・atomic write と同じ lock 内で最終確認し、unlink worker 完了まで占有する。
                if cache_key in cls._references or cache_key in cls._in_progress_paths:
                    return
                try:
                    await cls.__runThreadWorker(
                        lambda: cache_path.unlink(missing_ok=True),
                    )
                except OSError:
                    logging.warning(
                        f'[RecordedFMP4CacheManager] Failed to remove released cache: {cache_path}',
                    )
        finally:
            # acquire が先に task を registry から外していても、同じ lock で状態確認を完結させる。
            async with cls.__pathLock(cache_path):
                if cls._release_tasks.get(cache_key) is asyncio.current_task():
                    cls._release_tasks.pop(cache_key, None)

    @staticmethod
    def __normalizeRendition(rendition: str) -> str:
        """予約ファイル名へ安全に埋め込める音声レンディションIDへ正規化する。"""

        normalized = re.sub(r'[^A-Za-z0-9_-]', '_', rendition)
        return normalized or 'unknown'
