from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar, Literal, cast

from app import logging
from app.config import Config
from app.models.RecordedVideo import RecordedVideo


RecordedFMP4CacheKind = Literal['video-init', 'video', 'audio-init', 'audio']


@dataclass(frozen=True, slots=True)
class RecordedFMP4Variant:
    """映像・音声fMP4キャッシュを一意に識別する生成条件を表す。"""

    # 生成パイプラインの変更時にこの値を上げ、互換性のない旧キャッシュとの衝突を防ぐ。
    # キャッシュの配置形式は変わらないため、LAYOUT_VERSION とは独立した内部改訂値とする。
    PIPELINE_REVISION: ClassVar[int] = 2

    quality: str
    codec: str
    bit_depth: int
    is_24fps: bool
    backend: str
    configuration_generation: int
    seek_generation: int
    rendition: str | None = None

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
    _references: ClassVar[dict[str, set[str]]] = {}
    _release_tasks: ClassVar[dict[str, asyncio.Task[None]]] = {}
    _locks: ClassVar[dict[str, asyncio.Lock]] = {}

    @classmethod
    def isCacheFileName(cls, file_name: str) -> bool:
        """ファイル名がKonomiTV-BS4K管理下のfMP4予約形式と完全一致するかを返す。"""

        return cls._FILE_PATTERN.fullmatch(file_name) is not None

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
    async def writeAtomic(cls, destination: Path, data: bytes) -> None:
        """同じディレクトリの一時ファイルをfsync後、完成パスへatomic renameする。"""

        if cls.isCacheFileName(destination.name) is False:
            raise ValueError(f'Unmanaged fMP4 cache path: {destination}')
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        temporary_path = destination.with_name(f'{destination.name}.tmp-{uuid.uuid4()}')

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

        try:
            await asyncio.to_thread(WriteAndReplace)
        finally:
            await asyncio.to_thread(temporary_path.unlink, missing_ok=True)

    @classmethod
    async def acquire(cls, cache_path: Path, session_id: str) -> asyncio.Lock:
        """セッション参照を追加し、保留中の60秒削除を解除して生成ロックを返す。"""

        cache_key = str(cache_path)
        release_task = cls._release_tasks.pop(cache_key, None)
        if release_task is not None:
            release_task.cancel()
            try:
                await release_task
            except asyncio.CancelledError:
                pass
        cls._references.setdefault(cache_key, set()).add(session_id)
        return cls._locks.setdefault(cache_key, asyncio.Lock())

    @classmethod
    def release(cls, cache_path: Path, session_id: str) -> None:
        """セッション参照を外し、最後の参照なら60秒後の削除を予約する。"""

        cache_key = str(cache_path)
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
        cls._release_tasks[cache_key] = asyncio.create_task(cls.__deleteAfterDelay(cache_path))

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
                    try:
                        await asyncio.to_thread(entry.unlink)
                    except OSError:
                        logging.warning(f'[RecordedFMP4CacheManager] Failed to remove stale cache: {entry}')

    @classmethod
    async def cleanupDiscovered(cls, cache_path: Path) -> None:
        """録画スキャンで発見した未参照の予約キャッシュ残骸だけを削除する。"""

        if cls.isCacheFileName(cache_path.name) is False:
            return
        cache_key = str(cache_path)
        if cache_key in cls._references:
            return
        try:
            await asyncio.to_thread(cache_path.unlink, missing_ok=True)
        except OSError:
            logging.warning(f'[RecordedFMP4CacheManager] Failed to remove discovered cache: {cache_path}')

    @classmethod
    async def __deleteAfterDelay(cls, cache_path: Path) -> None:
        """再利用猶予を待ってから参照のないキャッシュを削除する。"""

        cache_key = str(cache_path)
        try:
            await asyncio.sleep(cls.RELEASE_DELAY_SECONDS)
            if cache_key in cls._references:
                return
            await asyncio.to_thread(cache_path.unlink, missing_ok=True)
            cls._locks.pop(cache_key, None)
        finally:
            if cls._release_tasks.get(cache_key) is asyncio.current_task():
                cls._release_tasks.pop(cache_key, None)

    @staticmethod
    def __normalizeRendition(rendition: str) -> str:
        """予約ファイル名へ安全に埋め込める音声レンディションIDへ正規化する。"""

        normalized = re.sub(r'[^A-Za-z0-9_-]', '_', rendition)
        return normalized or 'unknown'
