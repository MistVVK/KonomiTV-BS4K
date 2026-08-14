
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import pathlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Literal, cast

import anyio
from fastapi import HTTPException, status
from tortoise import transactions
from tortoise.exceptions import IntegrityError
from watchfiles import Change, awatch
from watchfiles.filters import DefaultFilter

from app import logging, schemas
from app.config import Config
from app.constants import JST, THUMBNAILS_DIR
from app.metadata.AnalysisTaskTracker import AnalysisTaskHandle, AnalysisTaskTracker
from app.metadata.CMAnalysisOrchestrator import CMAnalysisOrchestrator
from app.metadata.CMAnalysisWorkspace import CMAnalysisWorkspace
from app.metadata.CMChapterFile import (
    GetRecordedPathFromCMChapterPath,
    SelectRecordedPathForCMChapter,
)
from app.metadata.KonomiTVBS4KChapterFile import (
    GetRecordedPathFromKonomiTVBS4KChapterPath,
)
from app.metadata.MetadataAnalyzer import MetadataAnalyzer
from app.metadata.RecordedAnalysisPlan import (
    AnalysisRequest,
    BuildRecordedAnalysisPlan,
    ContentState,
    RecordedAnalysisPlan,
)
from app.metadata.RecordedSeriesResolver import RecordedSeriesResolver
from app.metadata.ThumbnailGenerator import ThumbnailGenerator
from app.models.Channel import Channel
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedVideo import RecordedVideo
from app.streams.RecordedFMP4Cache import RecordedFMP4CacheManager
from app.streams.RecordedSubtitleStream import RecordedSubtitleStream
from app.streams.VideoSegmentPlanner import VideoSegmentPlanner
from app.utils import ShutdownProcessPoolExecutor
from app.utils.DriveIOLimiter import DriveIOLimiter
from app.utils.Git import GetGitCommit
from app.utils.ProcessLimiter import ProcessLimiter
from app.utils.TSInformation import TSInformation


@dataclass(slots=True)
class FileRecordingInfo:
    """
    - last_modified: ファイルの最終更新日時
    - last_checked: ファイルの最終チェック日時
    - file_size: ファイルのサイズ
    - mtime_continuous_start_at: ファイルの最終更新日時が継続的に更新されている場合の継続更新の開始日時
    """
    last_modified: datetime
    last_checked: datetime
    file_size: int
    mtime_continuous_start_at: datetime | None


@dataclass(slots=True)
class RecordedFileLockEntry:
    """録画path単位のholder・waiter参照と排他lockを一体で管理する。"""

    lock: asyncio.Lock
    reference_count: int = 0


@dataclass(slots=True)
class RecordedVideoSummary:
    """
    RecordedScanTask.runBatchScan() 内でのメモリ使用量を抑えるため、RecordedVideo のうち必要最低限の情報のみを保持する軽量データ構造
    slots=True を指定し、メモリ使用量を抑える
    """

    id: int
    file_path: str
    created_at: datetime
    recorded_program_id: int
    status: Literal['Recording', 'Analyzing', 'Recorded', 'AnalysisFailed', 'Deleting', 'DeleteFailed']
    file_created_at: datetime
    file_modified_at: datetime
    file_size: int
    file_hash: str
    playback_index_status: Literal['Pending', 'Analyzing', 'Ready', 'Failed'] = 'Pending'
    playback_index_version: int | None = None

    def isFileContentUnchanged(self, file_modified_at: datetime, file_size: int) -> bool:
        """ファイル内容に関係しない ctime の変化を無視して、再解析が不要かを判定する。"""

        return (
            self.status == 'Recorded' and
            self.file_modified_at == file_modified_at and
            self.file_size == file_size
        )


class RecordedScanTask:
    """
    録画フォルダの監視とメタデータの DB への同期を行うタスク
    サーバーの起動中は常時稼働し続け、以下の処理を担う
    - サーバー起動時の録画フォルダの一括スキャン・同期
    - 録画フォルダ以下のファイルシステム変更の監視を開始し、変更があれば随時メタデータを解析後、DB に永続化
    - 録画中ファイルの状態管理
    - NFS / CIFS など OS notification が来ない共有ストレージ向けの低頻度 periodic reconciliation
    """

    # watcher event が欠落しても別 host 書き込みを発見するための強制再スキャン間隔
    RECONCILIATION_INTERVAL_SECONDS: ClassVar[int] = 900
    # Docker 上で host / を参照する bind 先
    DOCKER_HOST_ROOTFS: ClassVar[pathlib.Path] = pathlib.Path('/host-rootfs')

    # シングルトンインスタンス
    __instance: ClassVar[RecordedScanTask | None] = None

    # スキャン対象の拡張子
    SCAN_TARGET_EXTENSIONS: ClassVar[list[str]] = [
        '.ts', '.m2t', '.m2ts', '.mts', '.tlv', '.mmt', '.mmts',
        '.mp4', '.m4v', '.mov', '.mkv', '.webm', '.ogv', '.ogg',
    ]

    # 録画中ファイルの更新イベントを間引く間隔 (ログ出力用) (秒)
    UPDATE_THROTTLE_SECONDS: ClassVar[int] = 30

    # 録画完了と判断するまでの無更新時間 (秒)
    RECORDING_COMPLETE_SECONDS: ClassVar[int] = 15

    # 録画中と判断する最大の経過時間 (秒)
    RECORDING_MAX_AGE_SECONDS: ClassVar[int] = 300  # 5分

    # 録画中ファイルの最小データ長 (秒)
    MINIMUM_RECORDING_SECONDS: ClassVar[int] = 60

    # 継続更新を録画中と判断する最小時間 (秒)
    CONTINUOUS_UPDATE_THRESHOLD_SECONDS: ClassVar[int] = 60

    # 継続更新を強制的に完了とする時間 (秒)
    CONTINUOUS_UPDATE_MAX_SECONDS: ClassVar[int] = 86400  # 24時間

    # 既知のハッシュ衝突が発生しうる file_hash の集合
    KNOWN_COLLISION_FILE_HASHES: ClassVar[set[str]] = {
        'd1dd210d6b1312cb342b56d02bd5e651',
    }

    # 一括スキャンは録画単位のパイプラインを複数流し、別録画の各解析段階を重ねる。
    # 各MetadataAnalyzerが子プロセスを1つ使うため、CPUコア数の50%を上限とする。
    BATCH_PIPELINE_CONCURRENCY: ClassVar[int] = max(1, (os.cpu_count() or 2) // 2)


    def __new__(cls) -> RecordedScanTask:
        """
        シングルトンインスタンスを作成または取得する
        既にインスタンスが存在する場合はそれを返し、存在しない場合は新規作成する

        Returns:
            RecordedScanTask: シングルトンインスタンス
        """

        if cls.__instance is None:
            cls.__instance = super().__new__(cls)
        return cls.__instance


    def __init__(self) -> None:
        """
        録画フォルダの監視タスクを初期化する
        """

        # 初期化済みの場合は何もしない
        if hasattr(self, '_initialized') and self._initialized:
            return

        # 設定を読み込む
        self.config = Config()
        self.recorded_folders = [anyio.Path(folder) for folder in self.config.video.recorded_folders]

        # 録画中ファイルの状態管理
        self._recording_files: dict[anyio.Path, FileRecordingInfo] = {}

        # タスクの状態管理
        self._is_running = False
        self._task: asyncio.Task[None] | None = None

        # 録画フォルダ以下の一括スキャンを実行中かどうか
        self._is_batch_scan_running = False
        # 一括スキャンが起動した録画単位の pipeline task
        # runBatchScan() の例外・キャンセル時に未完了 task を cancel / join するために保持する
        self._batch_scan_pipeline_tasks: set[asyncio.Task[None]] = set()

        # バックグラウンドタスクの状態管理
        self._background_tasks: dict[anyio.Path, asyncio.Task[None]] = {}

        # シンボリックリンクの元パスと実体パスのマッピング
        self._symlink_path_map: dict[str, str] = {}
        self._symlink_path_map_lock = asyncio.Lock()

        # ファイルパスごとのlockとholder・waiter参照数を管理する辞書
        self._file_locks: dict[anyio.Path, RecordedFileLockEntry] = {}
        # _file_locks 辞書自体へのアクセスを保護するためのロック
        self._file_locks_dict_lock = asyncio.Lock()
        # 録画専用チャンネルの枝番計算と保存を直列化するためのロック
        self._recording_only_channels_lock = asyncio.Lock()

        # 初期化済みフラグをセット
        self._initialized = True


    @asynccontextmanager
    async def fileLock(self, file_path: anyio.Path) -> AsyncGenerator[None, None]:
        """path単位lockのholder・waiterを参照数へ含め、最後の解放後にentryを回収する。

        Args:
            file_path: 排他制御する録画ファイルのcanonical path。

        Yields:
            指定pathの処理権を保持している間のコンテキスト。
        """

        # lock待機へ入る前に参照数を増やし、holder解放時に待機者のentryを誤って削除しない。
        async with self._file_locks_dict_lock:
            entry = self._file_locks.get(file_path)
            if entry is None:
                entry = RecordedFileLockEntry(asyncio.Lock())
                self._file_locks[file_path] = entry
            entry.reference_count += 1

        try:
            async with entry.lock:
                yield
        finally:
            # holderのlock解放後、またはwaiterのcancel後に参照を外す。
            # 同じpathへ別entryが作られていた場合はidentity guardで新entryを保持する。
            async with self._file_locks_dict_lock:
                entry.reference_count -= 1
                if entry.reference_count == 0 and self._file_locks.get(file_path) is entry:
                    self._file_locks.pop(file_path, None)


    @staticmethod
    def buildRecordedFolderWatchFilter() -> DefaultFilter:
        """watchfiles既定除外にCM解析workspaceの予約rootを追加する。

        Returns:
            録画フォルダ監視に使用するwatchfiles filter。
        """

        return DefaultFilter(ignore_dirs=(
            *DefaultFilter.ignore_dirs,
            *CMAnalysisWorkspace.getRootDirectoryNames(),
        ))


    @classmethod
    async def iterRecordedFolderPaths(cls, folder: anyio.Path) -> AsyncGenerator[anyio.Path, None]:
        """CM解析workspaceを枝刈りしながら録画フォルダを列挙する。

        Args:
            folder: 列挙を開始する設定済み録画フォルダ。

        Yields:
            workspace予約rootとその配下を除くファイル・ディレクトリ。
        """

        directories = [pathlib.Path(str(folder))]
        while directories:
            directory = directories.pop()
            try:
                entries = await asyncio.to_thread(cls._scanDirectory, directory)
            except OSError as ex:
                logging.warning(f'{directory}: Failed to scan directory:', exc_info=ex)
                continue
            for entry_path, is_directory in entries:
                if CMAnalysisWorkspace.isWorkspacePath(entry_path):
                    if is_directory and CMAnalysisWorkspace.isWorkspaceRootName(entry_path.name):
                        # DB行が消えて起動時DB列挙で見つからなかった残骸もmarker/lock検証付きで回収する。
                        await CMAnalysisWorkspace.cleanupStaleInParents({entry_path.parent})
                    continue
                yield anyio.Path(entry_path)
                if is_directory:
                    directories.append(entry_path)


    @staticmethod
    def _scanDirectory(directory: pathlib.Path) -> list[tuple[pathlib.Path, bool]]:
        """単一ディレクトリをsymlink非追跡で同期列挙する。"""

        with os.scandir(directory) as entries:
            return [
                (pathlib.Path(entry.path), entry.is_dir(follow_symlinks=False))
                for entry in entries
            ]


    @staticmethod
    async def cleanupStaleWorkspaceForResolvedSymlink(
        original_path: str,
        canonical_path: pathlib.Path,
        cleaned_parents: set[pathlib.Path],
    ) -> None:
        """録画symlinkの実体親に残ったCM解析workspaceを一括スキャン中1回だけ回収する。"""

        canonical_path_str = str(canonical_path)
        if original_path == canonical_path_str or CMAnalysisWorkspace.isWorkspacePath(canonical_path):
            return

        canonical_parent = canonical_path.parent
        if canonical_parent in cleaned_parents:
            return
        # cleanup中に同じ親を指す別symlinkを処理しても再実行しないよう、awaitより先に記録する。
        cleaned_parents.add(canonical_parent)
        await CMAnalysisWorkspace.cleanupStaleInParents({canonical_parent})


    async def start(self) -> None:
        """
        録画フォルダの監視タスクを開始する
        このメソッドはサーバー起動時に app.py から自動的に呼ばれ、サーバーの起動中は常時稼働し続ける
        """

        # 既に実行中の場合は何もしない
        if self._is_running:
            return

        # 前回プロセスが削除処理の途中で終了した場合、同じ DELETE API から再試行できる状態へ戻す
        interrupted_deletion_count = await RecordedVideo.filter(status='Deleting').update(status='DeleteFailed')
        if interrupted_deletion_count > 0:
            logging.warning(
                f'Recovered {interrupted_deletion_count} interrupted recorded video deletion(s).'
            )

        # バックグラウンドタスクとして実行
        self._is_running = True
        self._task = asyncio.create_task(self.run())


    async def stop(self) -> None:
        """
        録画フォルダの監視タスクを停止する
        このメソッドはサーバー終了時に app.py から自動的に呼ばれる
        """

        # 既に停止中の場合は何もしない
        if not self._is_running:
            return

        # 実行中タスクを停止
        self._is_running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


    async def run(self) -> None:
        """
        録画フォルダ以下の一括スキャンと DB への同期と録画フォルダ以下のファイルシステム変更の監視を開始し、
        変更があれば随時メタデータを解析後、DB に永続化する
        このメソッドは start() 経由でサーバー起動時に app.py から自動的に呼ばれ、サーバーの起動中は常時稼働し続ける
        """

        try:
            # runBatchScan() が完了しなくても新しく録画されたファイルの監視を開始するため、同時に実行する
            await asyncio.gather(
                # サーバー起動時の一括スキャン・同期を実行
                self.runBatchScan(),
                # 録画フォルダの監視を開始
                self.watchRecordedFolders(),
                # NFS/CIFS 向けの低頻度 reconciliation
                self.__runPeriodicReconciliation(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logging.error('Error in RecordedScanTask:', exc_info=ex)
        finally:
            self._is_running = False


    async def runBatchScan(self) -> None:
        """
        録画フォルダ以下の一括スキャンを排他実行し、成否にかかわらず状態と子タスクを回収する。
        """

        # API から手動で一括スキャンを実行した際に重複して実行されないようにする
        if self._is_batch_scan_running:
            raise HTTPException(
                status_code = status.HTTP_429_TOO_MANY_REQUESTS,
                detail = 'Batch scan of recording folders is already running',
            )

        logging.info('Batch scan of recording folders has been started.')
        self._is_batch_scan_running = True
        self._batch_scan_pipeline_tasks.clear()
        try:
            await self.__runBatchScan()
            # 削除・再解析・索引version更新をDBへ反映し終えた集合を基準に字幕cacheをreconcileする。
            await RecordedSubtitleStream.cleanupOrphanedCaches()
        finally:
            # DB / I/O 例外やサーバー停止によるキャンセルでも、録画単位の解析を孤児化させない
            pipeline_tasks = tuple(self._batch_scan_pipeline_tasks)
            for pipeline_task in pipeline_tasks:
                if pipeline_task.done() is False:
                    pipeline_task.cancel()
            if len(pipeline_tasks) > 0:
                await asyncio.gather(*pipeline_tasks, return_exceptions=True)
            self._batch_scan_pipeline_tasks.clear()
            self._is_batch_scan_running = False


    async def __runBatchScan(self) -> None:
        """
        録画フォルダ以下の一括スキャンと DB への同期を実行する
        - 録画フォルダ内の全 TS ファイルをスキャン
        - 追加・変更があったファイルのみメタデータを解析し、DB に永続化
        - 存在しない録画ファイルに対応するレコードを一括削除
        """

        # 現在登録されている全ての RecordedVideo レコードの情報をキャッシュ
        ## すべての情報をキャッシュすると key_frames フィールドのデータ量が大きすぎてメモリとディスク I/O を大量に食うため、
        ## 必要最低限の情報のみをキャッシュする
        logging.info('Gathering all recorded video records...')
        all_video_rows = await RecordedVideo.all().values(
            'id',
            'file_path',
            'created_at',
            'recorded_program_id',
            'status',
            'file_created_at',
            'file_modified_at',
            'file_size',
            'file_hash',
            'playback_index_status',
            'playback_index_version',
        )
        videos_by_path: dict[str, list[RecordedVideoSummary]] = {}
        videos_to_keep: list[RecordedVideoSummary] = []  # 保持するレコードのリスト
        for index, row in enumerate(all_video_rows, start=1):
            recorded_video_summary = RecordedVideoSummary(
                id = row['id'],
                file_path = row['file_path'],
                created_at = row['created_at'],
                recorded_program_id = row['recorded_program_id'],
                status = row['status'],
                file_created_at = row['file_created_at'],
                file_modified_at = row['file_modified_at'],
                file_size = row['file_size'],
                file_hash = row['file_hash'],
                playback_index_status = row['playback_index_status'],
                playback_index_version = row['playback_index_version'],
            )
            if recorded_video_summary.file_path not in videos_by_path:
                videos_by_path[recorded_video_summary.file_path] = []
            videos_by_path[recorded_video_summary.file_path].append(recorded_video_summary)
            if index % 100 == 0:
                # 起動時にイベントループが他のタスクを処理できるよう定期的に制御を返す
                await asyncio.sleep(0)

        # 同一ファイルパスに対応するレコードが複数存在する場合、最新のものを保持して残りを削除する
        ## 重複削除処理をトランザクション配下で実行
        logging.info('Checking for duplicate recorded video records...')
        duplicates_found = False
        total_deleted_count = 0
        async with transactions.in_transaction():
            for index, (file_path, videos) in enumerate(videos_by_path.items(), start=1):
                if len(videos) > 1:
                    duplicates_found = True
                    logging.warning(f'{file_path}: Found {len(videos)} duplicate records. Keeping the latest one.')
                    # created_at でソートして最新のレコードを特定
                    videos.sort(key=lambda v: v.created_at, reverse=True)
                    latest_video = videos[0]
                    videos_to_keep.append(latest_video)  # 最新のものを保持リストに追加
                    # 最新以外のレコードを削除
                    for video_to_delete in videos[1:]:
                        try:
                            # RecordedProgram を削除 (CASCADE により RecordedVideo も削除される)
                            await RecordedProgram.filter(id=video_to_delete.recorded_program_id).delete()
                            logging.info(
                                f'{file_path}: Deleted duplicate record. [deleted recorded_program_id: {video_to_delete.recorded_program_id}] '
                                f'[kept recorded_program_id: {latest_video.recorded_program_id}]'
                            )
                        except Exception as ex_del:
                            logging.error(
                                f'{file_path}: Failed to delete duplicate record. [deleted recorded_program_id: {video_to_delete.recorded_program_id}]',
                                exc_info=ex_del,
                            )
                    # 削除対象のレコード数をカウント
                    deleted_count = len(videos) - 1  # -1 は最新のレコードを除いた数
                    total_deleted_count += deleted_count
                else:
                    # 重複がない場合も保持リストに追加
                    videos_to_keep.append(videos[0])
                if index % 50 == 0:
                    # 重複チェックがイベントループを占有し続けないよう適宜制御を返す
                    await asyncio.sleep(0)
        if duplicates_found:
            logging.info(f'Duplicate record cleanup finished. Total {total_deleted_count} duplicate records were deleted.')
        else:
            logging.info('No duplicate records found.')

        # 旧 key_frames が残っている録画は、再生開始位置キャッシュへ変換して DB サイズを抑える
        await self.__migrateKeyFramesToSegmentMap()

        # 現在登録されている全ての RecordedVideo レコードをキャッシュ
        ## 重複削除処理で保持すると判断されたレコードのみを使う
        existing_db_recorded_videos: dict[anyio.Path, RecordedVideoSummary] = {}
        for video in videos_to_keep:
            # 既存レコードのファイルパスもシンボリックリンクを解決して正規化する
            canonical_path = await self.resolveRecordedPath(anyio.Path(video.file_path))
            video.file_path = str(canonical_path)
            existing_db_recorded_videos[anyio.Path(video.file_path)] = video

        # スキャン対象から除外するフォルダ
        # 空文字列は全パスにマッチしてしまうため除外する
        exclude_scan_paths = [
            self.__normalizePathForPrefixMatch(pattern)
            for pattern in self.config.video.exclude_scan_paths
            if type(pattern) is str and pattern.strip() != ''
        ]

        # 各録画フォルダをスキャン
        logging.info('Scanning recorded folders...')
        processed_canonical_paths: set[str] = set()
        cleaned_symlink_target_parents: set[pathlib.Path] = set()
        for folder in self.recorded_folders:
            async for file_path in self.iterRecordedFolderPaths(folder):
                try:
                    # CM解析のcanonical MKVなどは録画と同じFSへ置くため、名前空間ごと最優先で除外する。
                    if CMAnalysisWorkspace.isWorkspacePath(pathlib.Path(str(file_path))):
                        continue
                    # Mac の metadata ファイルをスキップ
                    if file_path.name.startswith('._'):
                        continue
                    # 録画と同じ階層へ置かれるfMP4予約キャッシュは録画ファイルとして登録しない。
                    if RecordedFMP4CacheManager.isCacheFileName(file_path.name):
                        await RecordedFMP4CacheManager.cleanupDiscovered(pathlib.Path(str(file_path)))
                        continue
                    # 除外パターンのチェック（シンボリックリンク解決前）
                    original_path_str = str(file_path)
                    if self.isPathExcludedByPatterns(original_path_str, exclude_scan_paths) is True:
                        continue
                    # シンボリックリンクを含むパスは実体に解決して処理する
                    canonical_path = await self.resolveRecordedPath(file_path)
                    canonical_path_str = str(canonical_path)
                    # workspaceへのsymlinkも、解決後の正規パス要素で確実に除外する。
                    if CMAnalysisWorkspace.isWorkspacePath(pathlib.Path(canonical_path_str)):
                        continue
                    # 除外パターンのチェック（シンボリックリンク解決後）
                    if self.isPathExcludedByPatterns(canonical_path_str, exclude_scan_paths) is True:
                        continue
                    # シンボリックリンクのマッピングを更新する
                    await self.__updateSymlinkMapping(original_path_str, canonical_path_str)
                    if await canonical_path.is_dir():
                        continue
                    # 対象拡張子のファイル以外をスキップ
                    if canonical_path.suffix.lower() not in self.SCAN_TARGET_EXTENSIONS:
                        continue
                    # 録画ファイルが確実に存在することを確認する
                    ## 環境次第では、稀に glob で取得したファイルが既に存在しなくなっているケースがある
                    if not await self.isFileExists(canonical_path):
                        continue
                    # DB行がなくても、録画ファイルsymlinkの実体と同じ場所に残ったworkspaceを回収する。
                    # 受付可能な実在録画だけを対象とし、実体親ごとに一括スキャン中1回だけ実行する。
                    await self.cleanupStaleWorkspaceForResolvedSymlink(
                        original_path_str,
                        pathlib.Path(canonical_path_str),
                        cleaned_symlink_target_parents,
                    )
                    if canonical_path_str in processed_canonical_paths:
                        continue
                    processed_canonical_paths.add(canonical_path_str)

                    # 録画ごとに独立したパイプラインとして処理する。
                    # 上限へ達した時だけ完了済みタスクを回収し、別録画のMetadata/Index/CM/Thumbnailを重ねる。
                    scan_task = asyncio.create_task(self.processRecordedFile(
                        file_path = canonical_path,
                        original_path = file_path,
                        existing_db_recorded_videos = existing_db_recorded_videos,
                    ))
                    self._batch_scan_pipeline_tasks.add(scan_task)
                    if len(self._batch_scan_pipeline_tasks) >= self.BATCH_PIPELINE_CONCURRENCY:
                        done_tasks, _ = await asyncio.wait(
                            self._batch_scan_pipeline_tasks,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for done_task in done_tasks:
                            done_task.result()
                        self._batch_scan_pipeline_tasks.difference_update(done_tasks)
                except Exception as ex:
                    logging.error(f'{file_path}: Failed to process recorded file:', exc_info=ex)

        # ファイル消失判定や不要サムネイル削除は、全パイプラインがDB保存まで完了してから行う。
        if len(self._batch_scan_pipeline_tasks) > 0:
            await asyncio.gather(*self._batch_scan_pipeline_tasks)
            self._batch_scan_pipeline_tasks.clear()

        # 存在しない録画ファイルに対応するレコードを一括削除
        await self.__cleanupNonExistentRecordedVideoRecords(existing_db_recorded_videos)

        # DB に存在する全ての RecordedVideo レコードのハッシュを取得
        logging.info('Gathering all recorded video hashes...')
        db_recorded_video_hashes = set(
            cast(list[str], await RecordedVideo.all().values_list('file_hash', flat=True))
        )

        # サムネイルフォルダ内の全ファイルをスキャンし、不要なサムネイルファイルを削除
        logging.info('Deleting orphaned thumbnail files...')
        thumbnails_dir = anyio.Path(str(THUMBNAILS_DIR))
        if await thumbnails_dir.is_dir():
            async for thumbnail_path in thumbnails_dir.glob('*'):
                try:
                    # .git から始まるファイルは無視
                    if thumbnail_path.name.startswith('.git'):
                        continue
                    # ディレクトリは無視
                    if await thumbnail_path.is_dir():
                        continue

                    # ファイル名からハッシュを抽出
                    ## ファイル名は "{hash}.webp" または "{hash}_tile.webp" の形式
                    file_name = thumbnail_path.stem
                    if file_name.endswith('_tile'):
                        file_hash = file_name[:-5]  # "_tile" を除去
                    else:
                        file_hash = file_name

                    # DB に存在しないハッシュのファイルを削除
                    if file_hash not in db_recorded_video_hashes:
                        await thumbnail_path.unlink()
                        logging.info(f'{thumbnail_path.name}: Deleted orphaned thumbnail file.')
                except Exception as ex:
                    logging.error(f'{thumbnail_path}: Error deleting orphaned thumbnail file:', exc_info=ex)

        # かつてのバグで RecordedVideo.file_hash が衝突している録画ファイルのメタデータを再解析する
        ## トランザクション配下に入れることでパフォーマンスが向上する
        ## メモリ使用量を抑えるため、key_frames などの大きなフィールドは取得せず、必要最低限のフィールドのみを取得する
        ## ref: https://github.com/tsukumijima/KonomiTV/commit/92e8630f41b6440ebd10defa5fdde1489ac7376a
        async with transactions.in_transaction():
            collision_video_rows = await RecordedVideo.filter(
                file_hash__in=list(self.KNOWN_COLLISION_FILE_HASHES),
            ).values(
                'status',
                'file_path',
                'file_hash',
            )
            processed_collision_paths: set[str] = set()
            if len(collision_video_rows) > 0:
                logging.info(f'Found {len(collision_video_rows)} videos affected by known hash collisions. Reanalyzing...')
                for collision_video_row in collision_video_rows:
                    file_path_str = collision_video_row['file_path']
                    # 既に処理済みのファイルはスキップ
                    if file_path_str in processed_collision_paths:
                        continue
                    # 録画中のファイルは今後の解析に任せる
                    if collision_video_row['status'] == 'Recording':
                        continue
                    file_path = anyio.Path(file_path_str)
                    # ファイルが存在しない場合はスキップ
                    if not await self.isFileExists(file_path):
                        continue
                    try:
                        # メタデータ再解析を実行
                        logging.info(f'{file_path}: Reanalyzing due to known hash collision ({collision_video_row["file_hash"]}).')
                        await self.processRecordedFile(
                            file_path = file_path,
                            analysis_request = 'MetadataReanalysis',
                        )
                        # 処理済みファイルに追加
                        processed_collision_paths.add(file_path_str)
                        logging.info(f'{file_path}: Reanalysis completed.')
                    except Exception as ex:
                        logging.error(f'{file_path}: Failed to reanalyze known hash collision file:', exc_info=ex)

        # メタデータ解析に失敗した録画ファイルの数をログ出力
        analysis_failed_count = await RecordedVideo.filter(status='AnalysisFailed').count()
        if analysis_failed_count > 0:
            logging.warning(
                f'Batch scan completed with files in AnalysisFailed status. '
                f'count: {analysis_failed_count}. '
                f'Re-run metadata analysis after checking source files.',
            )
        logging.info('Batch scan of recording folders has been completed.')


    async def __cleanupNonExistentRecordedVideoRecords(
        self,
        existing_db_recorded_videos: dict[anyio.Path, RecordedVideoSummary],
    ) -> None:
        """存在しない録画ファイルのDBレコードを、削除再試行状態を保護しながら回収する。

        Args:
            existing_db_recorded_videos: batch scan後もファイルとの対応を確認できなかった録画の一覧。

        Returns:
            None
        """

        # トランザクション配下でまとめて削除することで、大量の消失レコードがある場合のDB処理を高速化する
        logging.info('Deleting records for non-existent files...')
        async with transactions.in_transaction():
            for index, (file_path, existing_recorded_video_summary) in enumerate(
                existing_db_recorded_videos.items(),
                start=1,
            ):
                # ファイルが消失した録画だけをDBレコード回収の対象にする
                if not await self.isFileExists(file_path):
                    # 削除APIが録画本体を削除した後に失敗したレコードは、同じAPIからの再試行対象として保持する
                    # ここでDBを消すと未削除の補助ファイルを辿れなくなるため、自動回収の対象から除外する
                    if existing_recorded_video_summary.status in ('Deleting', 'DeleteFailed'):
                        logging.info(
                            f'{file_path}: Preserved record for deletion retry. '
                            f'Status: {existing_recorded_video_summary.status}'
                        )
                    else:
                        # batch開始後に削除APIが状態遷移した場合も保護できるよう、削除時点の永続statusも条件へ含める
                        # RecordedProgram を削除すると、CASCADE 制約により RecordedVideo も同時に削除される
                        deleted_count = await RecordedProgram.filter(
                            id=existing_recorded_video_summary.recorded_program_id,
                        ).exclude(
                            recorded_video__status__in=['Deleting', 'DeleteFailed'],
                        ).delete()
                        if deleted_count > 0:
                            logging.info(f'{file_path}: Deleted record for non-existent file.')
                        else:
                            logging.info(f'{file_path}: Preserved record after deletion status changed during batch scan.')

                if index % 50 == 0:
                    # 既存レコードの走査がイベントループを占有し続けないよう適宜制御を返す
                    await asyncio.sleep(0)


    async def processRecordedFile(
        self,
        file_path: anyio.Path,
        original_path: anyio.Path | None = None,
        existing_db_recorded_videos: dict[anyio.Path, RecordedVideoSummary] | None = None,
        analysis_request: AnalysisRequest = 'Automatic',
    ) -> None:
        """
        指定された録画ファイルのメタデータを解析し、DB に永続化する
        既に当該ファイルの情報が DB に登録されており、ファイル内容に変更がない場合は何も行われない

        Args:
            file_path (anyio.Path): 処理対象のファイルパス
            original_path (anyio.Path | None): シンボリックリンクなどで取得した元のファイルパス
            existing_db_recorded_videos (dict[anyio.Path, RecordedVideoSummary] | None): 既に DB に永続化されている録画ファイルパスと RecordedVideo のサマリーデータのマッピング
                (ファイル変更イベントから呼ばれた場合、watchfiles 初期化時に取得した全レコードと今で状態が一致しているとは限らないため、None が入る)
            analysis_request: 自動判定、手動メタデータ再解析、索引再実行、CM再判定のいずれか。
        """

        file_path = await self.resolveRecordedPath(file_path)
        file_path_str = str(file_path)
        original_path_str = str(original_path) if original_path is not None else None

        # 同一ファイルパスへの DB レコード操作を排他制御する
        async with self.fileLock(file_path):
            metadata_analysis_recorded_video_id: int | None = None
            metadata_history: AnalysisTaskHandle | None = None
            try:
                # 万が一この時点でファイルが存在しない場合はスキップ
                # ファイル変更イベント発火後に即座にファイルが削除される可能性も考慮
                if not await self.isFileExists(file_path):
                    logging.warning(f'{file_path}: File does not exist after acquiring lock! ignored.')
                    return

                # ファイルの状態をチェック
                stat = await file_path.stat()
                now = datetime.now(tz=JST)
                file_size = stat.st_size
                file_modified_at = datetime.fromtimestamp(stat.st_mtime, tz=JST)

                # 全く録画できていない0バイトのファイルをスキップ
                if file_size == 0:
                    logging.warning(f'{file_path}: File size is 0. ignored.')
                    return

                # シンボリックリンク経由で検出した場合は元パスと実体パスのマッピングを保持する
                if original_path_str is not None:
                    await self.__updateSymlinkMapping(original_path_str, file_path_str)

                # 同じファイルパスの既存レコードのサマリーがあれば取り出す
                if existing_db_recorded_videos is not None:
                    existing_recorded_video_summary = existing_db_recorded_videos.pop(file_path, None)
                    if existing_recorded_video_summary is None and original_path_str is not None:
                        existing_recorded_video_summary = existing_db_recorded_videos.pop(anyio.Path(original_path_str), None)
                else:
                    existing_recorded_video_summary = None

                # この時点でサマリーがない場合、DB に同一ファイルパスのレコードがないか最小限のカラムで取得する
                ## ファイル変更イベントから呼ばれた場合は existing_db_recorded_videos は None となるが、
                ## DB には同一ファイルパスのレコードが存在する可能性がある
                if existing_recorded_video_summary is None:
                    query_paths = [file_path_str]
                    if original_path_str is not None and original_path_str not in query_paths:
                        query_paths.append(original_path_str)
                    summary_rows = await RecordedVideo.filter(
                        file_path__in=query_paths
                    ).values(
                        'id',
                        'file_path',
                        'created_at',
                        'recorded_program_id',
                        'status',
                        'file_created_at',
                        'file_modified_at',
                        'file_size',
                        'file_hash',
                        'playback_index_status',
                        'playback_index_version',
                    )
                    if len(summary_rows) > 0:
                        row = summary_rows[0]
                        existing_recorded_video_summary = RecordedVideoSummary(
                            id = row['id'],
                            file_path = row['file_path'],
                            created_at = row['created_at'],
                            recorded_program_id = row['recorded_program_id'],
                            status = row['status'],
                            file_created_at = row['file_created_at'],
                            file_modified_at = row['file_modified_at'],
                            file_size = row['file_size'],
                            file_hash = row['file_hash'],
                            playback_index_status = row['playback_index_status'],
                            playback_index_version = row['playback_index_version'],
                        )
                        existing_recorded_video_summary.file_path = file_path_str

                # 削除処理中または削除失敗後のレコードは、APIからの再試行まで状態とファイルをそのまま保持する
                # 自動スキャンで Analyzing / Recorded へ戻すと削除状態を失い、再試行不能になるため処理対象外とする
                if (
                    existing_recorded_video_summary is not None and
                    existing_recorded_video_summary.status in ('Deleting', 'DeleteFailed')
                ):
                    return

                # 更新日時とサイズが一致する既存録画は、内容をUnchangedと確定できる。
                # マトリックスから処理集合を決め、索引単独更新などMetadataAnalyzer不要の経路をここで完結させる。
                ## こうすることで、録画済みファイルに対するハッシュ算出やメタデータ解析を省略する。
                ## Linux の ctime は権限・所有者などのメタデータ変更でも更新されるため、内容変更の判定には使わない
                ## 万が一前回実行時からファイルサイズや最終更新日時の変更を伴わずに録画が完了した場合に状態を適切に反映できるよう、録画中はスキップしない
                if (existing_recorded_video_summary is not None and
                    existing_recorded_video_summary.isFileContentUnchanged(file_modified_at, file_size)):
                    unchanged_plan = BuildRecordedAnalysisPlan(
                        'Unchanged',
                        analysis_request,
                        existing_recorded_video_summary.playback_index_status,
                        existing_recorded_video_summary.playback_index_version,
                    )
                    if 'AnalyzeMetadata' not in unchanged_plan.actions:
                        await self.__executePlanWithoutMetadata(file_path, existing_recorded_video_summary.id, unchanged_plan)
                        return

                # 現在録画中とマークされているファイルの処理
                is_recording = file_path in self._recording_files
                if is_recording:
                    # 既に DB に登録済みで録画中の場合は再解析しない
                    if (existing_recorded_video_summary is not None and
                        existing_recorded_video_summary.status == 'Recording'):
                        return
                    # まだ DB に登録されていない＆ファイルサイズが前回から変化していない場合
                    recording_info = self._recording_files[file_path]
                    last_size = recording_info.file_size
                    mtime_continuous_start_at = recording_info.mtime_continuous_start_at
                    if file_size == last_size:
                        # 最終更新日時の継続更新中でない場合はスキップ
                        if mtime_continuous_start_at is None:
                            logging.warning(f'{file_path}: File is not recording. ignored.')
                            return
                        # 最終更新日時の継続更新が1分未満の場合もスキップ
                        continuous_duration = (now - mtime_continuous_start_at).total_seconds()
                        if continuous_duration < self.CONTINUOUS_UPDATE_THRESHOLD_SECONDS:
                            return
                        # 最終更新日時の継続更新が24時間を超えた場合は何かがおかしい可能性が高いため打ち切る
                        if continuous_duration >= self.CONTINUOUS_UPDATE_MAX_SECONDS:
                            logging.warning(f'{file_path}: Continuous mtime updates for {continuous_duration:.1f} seconds. (> {self.CONTINUOUS_UPDATE_MAX_SECONDS}s) ignored.')
                            return
                        # ここまで到達した時点で（ファイルサイズこそ変化していないが）最終更新日時の推移から1分以上ファイル内容の更新が続いているとみなし、
                        # 後続の処理でメタデータを解析し、解析に成功次第 DB に録画中として登録する
                        # 録画開始前にファイルアロケーションを行う録画予約ソフトでは、録画中も表面上ファイルサイズが変化しない問題への対処
                        pass

                # 既存録画はMetadataAnalyzerを開始する時点で再生をブロックする。
                # status='Analyzing' は索引キューとは独立した軽量メタデータ解析中を表し、
                # 内容変更後のファイルを旧索引で再生する競合を防ぐ。
                status_before_metadata_analysis = existing_recorded_video_summary.status \
                    if existing_recorded_video_summary is not None else None
                if existing_recorded_video_summary is not None:
                    metadata_analysis_recorded_video_id = existing_recorded_video_summary.id
                    await RecordedVideo.filter(id=existing_recorded_video_summary.id).update(status='Analyzing')
                    existing_recorded_video_summary.status = 'Analyzing'

                # ProcessPoolExecutor を使い、別プロセス上でメタデータを解析
                ## メタデータ解析処理は実装上同期 I/O で実装されており、また CPU-bound な処理のため、別プロセスで実行している
                ## コンテキストマネージャーはキャンセル時にも子プロセス終了を同期的に待つため、イベントループ上では使わない
                ## 正常完了時は明示的に待ってクリーンアップし、リクエスト切断時だけ待機なしで解放処理へ進める
                metadata_history = await AnalysisTaskTracker.start(
                    'MetadataAnalysis',
                    recorded_video_id=metadata_analysis_recorded_video_id,
                    title=file_path.name,
                    # DB 未登録の解析失敗でも履歴からフルパスを辿れるようにする。
                    file_path=file_path_str,
                    trigger='Automatic'
                    if analysis_request == 'Automatic'
                    else 'Manual',
                )
                try:
                    await metadata_history.setStage('Probing')
                    recorded_program = await self.__analyzeMetadata(file_path)
                    if recorded_program is None:
                        await metadata_history.finish('Failed', error_code='MetadataUnavailable')
                    else:
                        await metadata_history.setTitle(recorded_program.title)
                        await metadata_history.setStage('Saving', 0.9)
                except Exception as ex:
                    await metadata_history.finish(
                        'Failed',
                        error_code=type(ex).__name__,
                        error_message=str(ex),
                    )
                    logging.error(f'{file_path}: Error analyzing metadata:', exc_info=ex)
                    # メタデータ解析中に例外が発生した場合も、この時点ですでに DB にエントリが存在している場合は、UI から判別できるようステータスを更新する
                    if existing_recorded_video_summary is not None:
                        await RecordedVideo.filter(id=existing_recorded_video_summary.id).update(status='AnalysisFailed')
                        existing_recorded_video_summary.status = 'AnalysisFailed'
                    self._recording_files.pop(file_path, None)  # もし録画中扱いであればここで削除
                    return
                if recorded_program is None:
                    logging.error(f'{file_path}: Failed to analyze metadata.')
                    # メタデータ解析に失敗したがこの時点ですでに DB にエントリが存在している場合は、UI から判別できるようステータスを更新する
                    ## 本来メタデータ解析に失敗した録画ファイルは DB には登録されないが、「録画中は問題なく解析できていたが、録画完了後に解析できなくなった」
                    ## といったシチュエーションも稀に考えられなくもないため、そうした場合の保険として実装した
                    if existing_recorded_video_summary is not None:
                        await RecordedVideo.filter(id=existing_recorded_video_summary.id).update(status='AnalysisFailed')
                        existing_recorded_video_summary.status = 'AnalysisFailed'
                    self._recording_files.pop(file_path, None)  # もし録画中扱いであればここで削除
                    return

                # 60秒未満のファイルは録画失敗または切り抜きとみなしてスキップ
                # 録画中だがまだ60秒に満たない場合、今後のファイル変更イベント発火時に60秒を超えていれば録画中ファイルとして処理される
                if recorded_program.recorded_video.duration < self.MINIMUM_RECORDING_SECONDS:
                    logging.debug(f'{file_path}: This file is too short. (duration {recorded_program.recorded_video.duration:.1f}s < {self.MINIMUM_RECORDING_SECONDS}s) Skipped.')
                    await metadata_history.finish('Skipped', error_code='RecordingTooShort')
                    if metadata_analysis_recorded_video_id is not None:
                        await RecordedVideo.filter(id=metadata_analysis_recorded_video_id).update(status='AnalysisFailed')
                    return

                # 前回の DB 取得からメタデータ解析までの間に他のタスクがレコードを作成/更新している可能性があるため、
                # メタデータ解析後に再度ファイルパスに対応するレコードを取得する
                existing_db_recorded_video_after_analyze = await RecordedVideo.get_or_none(
                    file_path=file_path_str
                ).select_related('recorded_program', 'recorded_program__channel')
                if existing_db_recorded_video_after_analyze is None and original_path_str is not None:
                    existing_db_recorded_video_after_analyze = await RecordedVideo.get_or_none(
                        file_path=original_path_str
                    ).select_related('recorded_program', 'recorded_program__channel')

                # 部分ハッシュまで比較して最終的なContentStateを確定する。
                # 更新日時だけが変わった同一内容の録画は、Automaticならメタデータを保存せず索引状態だけを処理する。
                if existing_db_recorded_video_after_analyze is None:
                    content_state: ContentState = 'New'
                elif existing_db_recorded_video_after_analyze.file_hash == recorded_program.recorded_video.file_hash:
                    content_state = 'Unchanged'
                else:
                    content_state = 'Changed'
                analysis_plan = BuildRecordedAnalysisPlan(
                    content_state,
                    analysis_request,
                    existing_db_recorded_video_after_analyze.playback_index_status
                        if existing_db_recorded_video_after_analyze is not None else None,
                    existing_db_recorded_video_after_analyze.playback_index_version
                        if existing_db_recorded_video_after_analyze is not None else None,
                )
                if (
                    content_state == 'Unchanged' and
                    analysis_request == 'Automatic' and
                    status_before_metadata_analysis == 'Recorded'
                ):
                    assert existing_db_recorded_video_after_analyze is not None
                    await RecordedVideo.filter(id=existing_db_recorded_video_after_analyze.id).update(status='Recorded')
                    existing_db_recorded_video_after_analyze.status = 'Recorded'
                    metadata_analysis_recorded_video_id = None
                    await metadata_history.finish('Succeeded')
                    await self.__executePlanWithoutMetadata(
                        file_path,
                        existing_db_recorded_video_after_analyze.id,
                        analysis_plan,
                    )
                    return

                # 録画中のファイルとして処理
                ## 他ドライブからファイルコピー中のファイルも、実際の録画処理より高速に書き込まれるだけで随時書き込まれることに変わりはないので、
                ## 録画中として判断されることがある（その場合、ファイルコピーが完了した段階で「録画完了」扱いとなる）
                if is_recording or (now - file_modified_at).total_seconds() < self.RECORDING_COMPLETE_SECONDS:
                    # status を Recording に設定
                    recorded_program.recorded_video.status = 'Recording'
                    # 状態を更新
                    self._recording_files[file_path] = FileRecordingInfo(
                        last_modified = file_modified_at,
                        last_checked = now,
                        file_size = file_size,
                        mtime_continuous_start_at = file_modified_at,  # 初回は必ず mtime_continuous_start_at を設定
                    )
                    logging.debug(f'{file_path}: This file is recording or copying. (duration {recorded_program.recorded_video.duration:.1f}s >= {self.MINIMUM_RECORDING_SECONDS}s)')
                else:
                    # status を Recorded に設定
                    # MetadataAnalyzer 側で既に Recorded に設定されているが、念のため
                    recorded_program.recorded_video.status = 'Recorded'

                # DB に永続化
                # メタデータ解析後の最新のデータベース情報を使う
                saved_recorded_video_id, saved_recorded_program_id = await self.__saveRecordedMetadataToDB(
                    recorded_program,
                    existing_db_recorded_video_after_analyze,
                    content_state,
                )
                metadata_analysis_recorded_video_id = None
                logging.info(f'{file_path}: {"Updated" if existing_db_recorded_video_after_analyze else "Saved"} metadata to DB. (status: {recorded_program.recorded_video.status})')
                await metadata_history.finish('Succeeded')

                # 録画中は確定解析を行わず、録画完了後の変更イベントで改めて計画する。
                if recorded_program.recorded_video.status != 'Recorded':
                    return

                # 録画スキャンを外部API待ちで止めないよう、DB保存済みIDだけを専用ワーカーへ渡す。
                # 任意機能の設定ファイル破損やキュー障害で、後続の索引・CM解析まで中断しない。
                try:
                    await RecordedSeriesResolver.enqueue(saved_recorded_program_id, input_changed=True)
                except Exception as ex:
                    logging.error(
                        f'{file_path}: Failed to enqueue recorded series resolution. '
                        f'recorded_program_id: {saved_recorded_program_id}',
                        exc_info=ex,
                    )

                # サムネイルだけは直列処理に含めず、従来どおりバックグラウンドで生成する。
                # CMは索引完了後に直列実行するため、このタスクへ混在させない。
                if 'GenerateThumbnail' in analysis_plan.actions:
                    if file_path not in self._background_tasks:
                        task = asyncio.create_task(self.__runThumbnailGeneration(
                            recorded_program,
                            saved_recorded_video_id,
                        ))
                        self._background_tasks[file_path] = task

                # 新規・内容変更・手動再解析は、DBへ暫定保存した後で全編索引を直列実行する。
                # MetadataAnalyzerが得たEIT候補はDBの旧確定索引を上書きせず、今回の索引入力としてだけ渡す。
                if 'BuildPlaybackIndex' in analysis_plan.actions:
                    assert analysis_plan.index_priority is not None
                    db_recorded_video = await RecordedVideo.get(file_path=file_path_str)
                    from app.metadata.RecordedPlaybackIndexer import (
                        RecordedPlaybackIndexer,
                    )
                    await RecordedPlaybackIndexer.enqueue(
                        db_recorded_video.id,
                        priority=analysis_plan.index_priority,
                        metadata_seed=recorded_program.recorded_video,
                        force_rebuild=analysis_request == 'MetadataReanalysis',
                    )

                # 索引が失敗してもファイルが残っていればCM判定を続行し、結果を必ず明示保存する。
                if 'DetectCM' in analysis_plan.actions and await self.isFileExists(file_path):
                    db_recorded_video = await RecordedVideo.get(file_path=file_path_str)
                    cm_intent = 'CMDetection' if analysis_request == 'CMDetection' else 'DetectCM'
                    await CMAnalysisOrchestrator().run(db_recorded_video.id, cm_intent)

            except Exception as ex:
                if metadata_history is not None:
                    await metadata_history.finish(
                        'Failed',
                        error_code=type(ex).__name__,
                        error_message=str(ex),
                    )
                logging.error(f'{file_path}: Error processing file inside lock:', exc_info=ex)
                if metadata_analysis_recorded_video_id is not None:
                    await RecordedVideo.filter(id=metadata_analysis_recorded_video_id).update(status='AnalysisFailed')


    @staticmethod
    async def __analyzeMetadata(file_path: anyio.Path) -> schemas.RecordedProgram | None:
        """同期MetadataAnalyzerを専用プロセスで実行し、キャンセル時も確実に回収する。"""

        loop = asyncio.get_running_loop()
        analyzer = MetadataAnalyzer(pathlib.Path(str(file_path)))
        executor = concurrent.futures.ProcessPoolExecutor(max_workers=1)
        should_wait_executor = True
        try:
            return await loop.run_in_executor(executor, analyzer.analyze)
        except asyncio.CancelledError:
            should_wait_executor = False
            await ShutdownProcessPoolExecutor(executor, is_cancelled=True)
            raise
        finally:
            if should_wait_executor is True:
                await ShutdownProcessPoolExecutor(executor, is_cancelled=False)


    @staticmethod
    async def isFileExists(file_path: anyio.Path) -> bool:
        """
        ファイルが存在し、通常のファイルであるかを安全にチェックする。
        PermissionError やその他のファイルアクセスエラーが発生した場合は False を返す。

        Args:
            file_path (anyio.Path): チェックするファイルパス

        Returns:
            bool: ファイルが存在し、通常のファイルであるかどうか
        """
        try:
            return await file_path.is_file()
        except PermissionError:
            logging.warning(f'{file_path}: Permission denied when checking file.')
            return False
        except FileNotFoundError:
            return False
        except OSError as e:
            logging.warning(f'{file_path}: OSError during is_file() check:', exc_info=e)
            return False


    async def __executePlanWithoutMetadata(
        self,
        file_path: anyio.Path,
        recorded_video_id: int,
        analysis_plan: RecordedAnalysisPlan,
    ) -> None:
        """MetadataAnalyzer不要の索引単独更新またはCM再判定を実行する。

        Args:
            file_path: 解析対象の録画ファイル。
            recorded_video_id: 対応するRecordedVideoのID。
            analysis_plan: マトリックスから構築済みの処理計画。

        Returns:
            None
        """

        if 'BuildPlaybackIndex' in analysis_plan.actions:
            assert analysis_plan.index_priority is not None
            from app.metadata.RecordedPlaybackIndexer import RecordedPlaybackIndexer
            index_future = RecordedPlaybackIndexer.enqueue(
                recorded_video_id,
                priority=analysis_plan.index_priority,
            )
            # 自動バックフィルはファイルスキャンを待たせない。明示的な再実行だけ完了まで待つ。
            if analysis_plan.index_priority != 2:
                await index_future

        if 'DetectCM' in analysis_plan.actions and await self.isFileExists(file_path):
            await CMAnalysisOrchestrator().run(recorded_video_id, 'CMDetection')
        else:
            # 録画本体がUnchangedでも、外部ツールがchapterを追加・更新・削除している可能性がある。
            await CMAnalysisOrchestrator().syncIfChapterChanged(recorded_video_id)


    @classmethod
    async def resolveRecordedPath(cls, file_path: anyio.Path) -> anyio.Path:
        """
        シンボリックリンクを解決して実体のパスを取得する。解決に失敗した場合は元のパスを返す。

        Docker 環境では host 絶対 path を指す symlink を /host-rootfs 配下へ再解決する。
        relative / broken / cycle symlink は安全に元 path（または解決可能な範囲）を返す。

        Args:
            file_path (anyio.Path): ファイルパス

        Returns:
            anyio.Path: シンボリックの参照先である実体のパス
        """
        try:
            resolved = await asyncio.to_thread(cls._resolveRecordedPathSync, pathlib.Path(str(file_path)))
            return anyio.Path(resolved)
        except (OSError, RuntimeError) as ex:
            logging.warning(f'{file_path}: Failed to resolve symlink. Using original path:', exc_info=ex)
            return file_path


    @classmethod
    def _resolveRecordedPathSync(cls, file_path: pathlib.Path) -> pathlib.Path:
        """
        host absolute symlink を Docker の /host-rootfs へ rebase しつつ同期解決する。

        Args:
            file_path (pathlib.Path): 解決対象パス

        Returns:
            pathlib.Path: 解決後パス。broken / cycle 時は安全に元 path。
        """

        host_rootfs = cls.DOCKER_HOST_ROOTFS
        use_host_rootfs = host_rootfs.is_dir()
        seen: set[pathlib.Path] = set()
        current = file_path

        # 親ディレクトリを含む path 全体を先頭から解決する
        parts = current.parts
        if len(parts) == 0:
            return current

        # 絶対 path の先頭は '/' のみ
        if current.is_absolute():
            resolved = pathlib.Path(parts[0])
            remaining = parts[1:]
        else:
            resolved = pathlib.Path()
            remaining = parts

        for part in remaining:
            candidate = resolved / part
            try:
                if candidate.is_symlink() is False:
                    resolved = candidate
                    continue
            except OSError:
                return file_path

            # 循環 symlink は元 path を返す
            try:
                identity = candidate.resolve(strict=False)
            except (OSError, RuntimeError):
                return file_path
            if identity in seen:
                return file_path
            seen.add(identity)

            try:
                target = pathlib.Path(os.readlink(candidate))
            except OSError:
                return file_path

            if target.is_absolute():
                # Docker: host 絶対 target を /host-rootfs 配下へ rebase する
                if use_host_rootfs is True and not str(target).startswith(str(host_rootfs)):
                    rebased = host_rootfs / target.relative_to(target.anchor)
                    if rebased.exists() or rebased.is_symlink():
                        resolved = rebased
                    else:
                        # broken は安全に元 path 扱い
                        return file_path
                else:
                    if target.exists() or target.is_symlink():
                        resolved = target
                    else:
                        return file_path
            else:
                resolved = (candidate.parent / target)

            # 中間 symlink の先がさらに symlink なら再帰相当の再解決を行う
            try:
                if resolved.is_symlink():
                    nested = cls._resolveRecordedPathSync(resolved)
                    resolved = nested
            except (OSError, RuntimeError):
                return file_path

        try:
            return resolved.resolve(strict=False)
        except (OSError, RuntimeError):
            return file_path


    @staticmethod
    def __normalizePathForPrefixMatch(path_str: str) -> str:
        """
        パス区切り文字を統一し、前方一致の比較を安定させる。

        Args:
            path_str (str): 正規化対象のパス文字列

        Returns:
            str: パス区切り文字を / に統一した文字列
        """

        # Windows ではパス区切り文字として / と \\ の両方が使えるため、比較前に / に統一する
        return path_str.replace('\\', '/')


    @classmethod
    def isPathExcludedByPatterns(cls, path_str: str, exclude_patterns: list[str]) -> bool:
        """
        path component 境界を考慮して除外パターンに一致するかを判定する。

        指定 folder 自身とその子孫だけを除外し、同 prefix の兄弟 folder は除外しない。
        例: 除外 `/recordings/temp` は `/recordings/temporary` に一致しない。

        Args:
            path_str (str): 判定対象のパス
            exclude_patterns (list[str]): 正規化済み除外パターン一覧

        Returns:
            bool: 除外対象なら True
        """

        normalized_path = cls.__normalizePathForPrefixMatch(path_str).rstrip('/')
        if normalized_path == '':
            return False
        for pattern in exclude_patterns:
            normalized_pattern = cls.__normalizePathForPrefixMatch(pattern).rstrip('/')
            if normalized_pattern == '':
                continue
            if normalized_path == normalized_pattern:
                return True
            if normalized_path.startswith(normalized_pattern + '/'):
                return True
        return False


    async def __runPeriodicReconciliation(self) -> None:
        """
        watcher event が来ない NFS/CIFS 上の新規ファイルを低頻度で強制再スキャンする。

        batch scan 実行中はスキップし、完了後の次周期で再試行する。
        """

        logging.info(
            'Starting periodic recorded folder reconciliation '
            f'(interval={self.RECONCILIATION_INTERVAL_SECONDS}s).'
        )
        try:
            while self._is_running:
                await asyncio.sleep(self.RECONCILIATION_INTERVAL_SECONDS)
                if self._is_running is False:
                    break
                if self._is_batch_scan_running is True:
                    logging.debug('Skipping periodic reconciliation because batch scan is running.')
                    continue
                try:
                    logging.info('Periodic recorded folder reconciliation started.')
                    await self.runBatchScan()
                    logging.info('Periodic recorded folder reconciliation finished.')
                except HTTPException as ex:
                    # 手動 batch scan と衝突した場合は次周期まで待つ
                    if ex.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
                        logging.debug('Periodic reconciliation deferred: batch scan already running.')
                        continue
                    logging.error('Periodic reconciliation failed:', exc_info=ex)
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    logging.error('Periodic reconciliation failed:', exc_info=ex)
        except asyncio.CancelledError:
            raise
        finally:
            logging.info('Periodic recorded folder reconciliation has been stopped.')


    async def __updateSymlinkMapping(self, original_path_str: str | None, canonical_path_str: str) -> None:
        """
        シンボリックリンクの元パスと実体パスのマッピングを管理する。

        Args:
            original_path_str (str | None): シンボリックリンクの元パス
            canonical_path_str (str): シンボリックリンクの実体パス
        """
        if original_path_str is None:
            return
        async with self._symlink_path_map_lock:
            if original_path_str == canonical_path_str:
                self._symlink_path_map.pop(original_path_str, None)
            else:
                self._symlink_path_map[original_path_str] = canonical_path_str


    @staticmethod
    def __populateChannelModelFromSchema(db_channel: Channel, channel_schema: schemas.Channel) -> None:
        """
        Pydantic スキーマから Channel モデルへ属性を転写する

        Args:
            db_channel (Channel): 値を設定する DB モデル
            channel_schema (schemas.Channel): 転写元のチャンネル情報
        """

        # 録画メタデータ解析結果のチャンネル情報を、そのまま DB モデルへ反映する
        db_channel.id = channel_schema.id
        db_channel.display_channel_id = channel_schema.display_channel_id
        db_channel.network_id = channel_schema.network_id
        db_channel.service_id = channel_schema.service_id
        db_channel.transport_stream_id = channel_schema.transport_stream_id
        db_channel.remocon_id = channel_schema.remocon_id
        db_channel.channel_number = channel_schema.channel_number
        db_channel.type = channel_schema.type
        db_channel.name = channel_schema.name
        db_channel.jikkyo_force = channel_schema.jikkyo_force
        db_channel.is_subchannel = channel_schema.is_subchannel
        db_channel.is_radiochannel = channel_schema.is_radiochannel
        db_channel.is_watchable = channel_schema.is_watchable


    async def __saveRecordedMetadataToDB(
        self,
        recorded_program: schemas.RecordedProgram,
        existing_db_recorded_video: RecordedVideo | None,
        content_state: ContentState,
    ) -> tuple[int, int]:
        """
        録画ファイルのメタデータ解析結果を DB に保存する
        既存レコードがある場合は更新し、ない場合は新規作成する
        録画専用の地デジチャンネルは、保存直前にメインプロセス側で枝番を再計算する
        並行保存時の枝番衝突を避けるため、録画専用の地デジチャンネル作成だけは直列化する

        Args:
            recorded_program (schemas.RecordedProgram): 保存する録画番組情報
            existing_db_recorded_video (RecordedVideo | None): 既に DB に永続化されている録画ファイルの RecordedVideo レコード
            content_state: DB保存時点で確定したファイル内容状態。

        Returns:
            DB 保存後に確定した RecordedVideo ID と RecordedProgram ID。
        """

        # トランザクション配下に入れることでパフォーマンスが向上する
        async with transactions.in_transaction():

            # Channel の保存（まだ当該チャンネルが DB に存在しない場合のみ）
            db_channel = None
            if recorded_program.channel is not None:
                db_channel = await Channel.get_or_none(id=recorded_program.channel.id)
                if db_channel is None:
                    # 録画専用の地デジチャンネルは、地方違いの TS ファイルを並行解析すると枝番衝突が発生しうる
                    ## そのため、ここで保存直前に最新の DB 状態を見て枝番を再計算し、保存処理自体も直列化する
                    if recorded_program.channel.type == 'GR' and recorded_program.channel.is_watchable is False:
                        async with self._recording_only_channels_lock:
                            db_channel = await Channel.get_or_none(id=recorded_program.channel.id)
                            if db_channel is None:
                                # 同一プロセス内では lock で競合を防げているが、
                                ## 別プロセスや想定外の外部介入で display_channel_id が衝突した場合に備えて 1 回だけ再試行する
                                for retry_count in range(2):
                                    recalculated_channel_number = await TSInformation.calculateChannelNumber(
                                        recorded_program.channel.type,
                                        recorded_program.channel.network_id,
                                        recorded_program.channel.service_id,
                                        recorded_program.channel.remocon_id,
                                    )
                                    recorded_program.channel.channel_number = recalculated_channel_number
                                    recorded_program.channel.display_channel_id = (
                                        recorded_program.channel.type.lower() + recalculated_channel_number
                                    )

                                    db_channel = Channel()
                                    self.__populateChannelModelFromSchema(db_channel, recorded_program.channel)
                                    try:
                                        await db_channel.save()
                                        # 初回の INSERT で競合した場合のみ、リトライで解消されたことをログに残す
                                        if retry_count > 0:
                                            logging.info(
                                                f'{recorded_program.recorded_video.file_path}: '
                                                f'Recorded-only channel save recovered after retry. '
                                                f'retry_count: {retry_count}, '
                                                f'display_channel_id: {db_channel.display_channel_id}'
                                            )
                                        break
                                    except IntegrityError as ex:
                                        if retry_count == 0:
                                            logging.warning(
                                                f'{recorded_program.recorded_video.file_path}: '
                                                f'Retrying recording-only channel save due to display_channel_id conflict.'
                                            )
                                            continue
                                        raise ex
                    else:
                        db_channel = Channel()
                        self.__populateChannelModelFromSchema(db_channel, recorded_program.channel)
                        await db_channel.save()
                elif (
                    db_channel.transport_stream_id is None and
                    recorded_program.channel.transport_stream_id is not None
                ):
                    # 既存チャンネルに TSID がない場合だけ、録画メタデータから得た値で補完する
                    ## Mirakurun のチャンネル情報には TSID が含まれないが、NID/SID/TSID の組は放送運用上ほぼ不変なので、
                    ## 録画メタデータから判明した TSID は失わず保持する
                    db_channel.transport_stream_id = recorded_program.channel.transport_stream_id
                    await db_channel.save(update_fields=['transport_stream_id'])

            # RecordedProgram の保存または更新
            if existing_db_recorded_video is not None:
                db_recorded_program = existing_db_recorded_video.recorded_program
            else:
                db_recorded_program = RecordedProgram()

            # RecordedProgram の属性を設定 (id, created_at, updated_at は自動生成のため指定しない)
            db_recorded_program.recording_start_margin = recorded_program.recording_start_margin
            db_recorded_program.recording_end_margin = recorded_program.recording_end_margin
            db_recorded_program.is_partially_recorded = recorded_program.is_partially_recorded
            db_recorded_program.channel = db_channel  # type: ignore
            db_recorded_program.network_id = recorded_program.network_id
            db_recorded_program.service_id = recorded_program.service_id
            db_recorded_program.event_id = recorded_program.event_id
            db_recorded_program.title = recorded_program.title
            # SeriesResolverが所有する確定済み関連付けは、ファイル内容が変わった場合も保持する。
            # Resolverはメタデータ保存後に非同期で再判定するため、Analyzerの初期値を使うのは
            # 新規作成時だけとし、既存行ではseries関連列をUPDATE対象にも含めない。
            if existing_db_recorded_video is None:
                db_recorded_program.series_id = recorded_program.series_id
                db_recorded_program.series_broadcast_period_id = recorded_program.series_broadcast_period_id
                db_recorded_program.series_title = recorded_program.series_title
                db_recorded_program.episode_number = recorded_program.episode_number
                db_recorded_program.subtitle = recorded_program.subtitle
            db_recorded_program.description = recorded_program.description
            db_recorded_program.detail = recorded_program.detail
            db_recorded_program.start_time = recorded_program.start_time
            db_recorded_program.end_time = recorded_program.end_time
            db_recorded_program.duration = recorded_program.duration
            db_recorded_program.is_free = recorded_program.is_free
            db_recorded_program.genres = recorded_program.genres
            db_recorded_program.primary_audio_type = recorded_program.primary_audio_type
            db_recorded_program.primary_audio_language = recorded_program.primary_audio_language
            db_recorded_program.secondary_audio_type = recorded_program.secondary_audio_type
            db_recorded_program.secondary_audio_language = recorded_program.secondary_audio_language
            if existing_db_recorded_video is None:
                await db_recorded_program.save()
            else:
                # 解析開始後にSeriesResolverや管理画面が更新した関連付けを、取得済みの古い
                # モデルインスタンスから後勝ちで巻き戻さないよう、Scanner所有列だけを保存する。
                await db_recorded_program.save(update_fields=[
                    'recording_start_margin',
                    'recording_end_margin',
                    'is_partially_recorded',
                    'channel_id',
                    'network_id',
                    'service_id',
                    'event_id',
                    'title',
                    'description',
                    'detail',
                    'start_time',
                    'end_time',
                    'duration',
                    'is_free',
                    'genres',
                    'primary_audio_type',
                    'primary_audio_language',
                    'secondary_audio_type',
                    'secondary_audio_language',
                    'updated_at',
                ])

            # RecordedVideo の保存または更新
            if existing_db_recorded_video is not None:
                db_recorded_video = existing_db_recorded_video
            else:
                db_recorded_video = RecordedVideo()

            # RecordedVideo の属性を設定 (id, created_at, updated_at は自動生成のため指定しない)
            # ここでは軽量メタデータ解析が所有するファイル情報・時刻・暫定メディア情報だけを更新する。
            db_recorded_video.recorded_program = db_recorded_program
            db_recorded_video.status = recorded_program.recorded_video.status
            db_recorded_video.file_path = str(recorded_program.recorded_video.file_path)
            db_recorded_video.file_hash = recorded_program.recorded_video.file_hash
            db_recorded_video.file_size = recorded_program.recorded_video.file_size
            db_recorded_video.file_created_at = recorded_program.recorded_video.file_created_at
            db_recorded_video.file_modified_at = recorded_program.recorded_video.file_modified_at
            # 解析結果を保存する直前の日時と実行中ビルドを記録し、後から解析結果の由来を追跡できるようにする
            db_recorded_video.analyzed_at = datetime.now(tz=JST)
            db_recorded_video.analysis_git_commit = await GetGitCommit()
            db_recorded_video.recording_start_time = recorded_program.recorded_video.recording_start_time
            db_recorded_video.recording_end_time = recorded_program.recorded_video.recording_end_time
            db_recorded_video.duration = recorded_program.recorded_video.duration
            db_recorded_video.container_format = recorded_program.recorded_video.container_format
            db_recorded_video.has_video = recorded_program.recorded_video.has_video
            db_recorded_video.has_audio = recorded_program.recorded_video.has_audio

            # New/Changedでは全編索引がまだ存在しないため、画面表示に必要な暫定値を保存する。
            # Unchangedの手動再解析では、Indexerが所有する既存の確定値を成功時まで変更しない。
            if content_state in ('New', 'Changed'):
                db_recorded_video.video_codec = recorded_program.recorded_video.video_codec
                db_recorded_video.video_codec_profile = recorded_program.recorded_video.video_codec_profile
                db_recorded_video.video_scan_type = recorded_program.recorded_video.video_scan_type
                db_recorded_video.video_frame_rate = recorded_program.recorded_video.video_frame_rate
                db_recorded_video.video_resolution_width = recorded_program.recorded_video.video_resolution_width
                db_recorded_video.video_resolution_height = recorded_program.recorded_video.video_resolution_height
                db_recorded_video.has_video_stream_changes = recorded_program.recorded_video.has_video_stream_changes
                db_recorded_video.primary_audio_codec = recorded_program.recorded_video.primary_audio_codec
                db_recorded_video.primary_audio_channel = recorded_program.recorded_video.primary_audio_channel
                db_recorded_video.primary_audio_sampling_rate = recorded_program.recorded_video.primary_audio_sampling_rate
                db_recorded_video.secondary_audio_codec = recorded_program.recorded_video.secondary_audio_codec
                db_recorded_video.secondary_audio_channel = recorded_program.recorded_video.secondary_audio_channel
                db_recorded_video.secondary_audio_sampling_rate = recorded_program.recorded_video.secondary_audio_sampling_rate
                db_recorded_video.audio_tracks = recorded_program.recorded_video.audio_tracks
                db_recorded_video.audio_track_timeline = recorded_program.recorded_video.audio_track_timeline
                db_recorded_video.subtitle_tracks = recorded_program.recorded_video.subtitle_tracks
                db_recorded_video.playback_index_status = 'Pending'
                db_recorded_video.playback_index_version = None
                db_recorded_video.playback_indexed_at = None
                db_recorded_video.playback_index_error_code = None
                db_recorded_video.video_stream_timeline = None
                # 再解析中も前回のCM公開結果は維持し、入力変更の判定と結果の差し替えは
                # CMAnalysisOrchestratorへ委ねる。再生キャッシュとサムネイルだけをここで無効化する。
                db_recorded_video.key_frames = []
                db_recorded_video.segment_map = []
                db_recorded_video.ts_source_base_dts = None
                db_recorded_video.thumbnail_info = None
            await db_recorded_video.save()

            return db_recorded_video.id, db_recorded_program.id


    async def __runThumbnailGeneration(
        self,
        recorded_program: schemas.RecordedProgram,
        recorded_video_id: int,
    ) -> None:
        """
        録画完了後のサムネイルをバックグラウンド生成する。

        Args:
            recorded_program (schemas.RecordedProgram): 解析対象の録画番組情報
            recorded_video_id (int): DB 保存後に確定した RecordedVideo の ID
        """

        # 録画ファイルのパスを anyio.Path に変換
        file_path = anyio.Path(recorded_program.recorded_video.file_path)

        try:
            async with AnalysisTaskTracker.track(
                'ThumbnailGeneration',
                recorded_video_id=recorded_video_id,
                title=recorded_program.title,
            ) as history:
                logging.info(f'{file_path}: Starting background analysis task...')
                await history.setStage('Generating')
                # ProcessLimiter で稼働中のバックグラウンドタスクの同時実行数を CPU コア数の 50% に制限
                async with ProcessLimiter.getSemaphore('RecordedScanTask'):
                    # DriveIOLimiter で同一 HDD に対してのバックグラウンドタスクの同時実行数を原則1セッションに制限
                    async with DriveIOLimiter.getSemaphore(file_path):
                        if recorded_program.recorded_video.has_video is False:
                            logging.info(f'{file_path}: Skipping thumbnail generation for audio-only recording.')
                            await history.finish('Skipped', error_code='AudioOnly')
                            return
                        # シークバー用サムネイルとリスト表示用の代表サムネイルの両方を生成する。
                        await ThumbnailGenerator.fromRecordedProgram(recorded_program).generateAndSave()
                logging.info(f'{file_path}: Background analysis task completed.')

        except Exception as ex:
            logging.error(f'{file_path}: Error in background analysis task:', exc_info=ex)
        finally:
            # 完了したタスクを管理対象から削除
            self._background_tasks.pop(file_path, None)


    async def __migrateKeyFramesToSegmentMap(self) -> None:
        """
        旧 key_frames を再生開始位置キャッシュへ移行する

        このメソッドは runBatchScan() から呼び出され、以下の処理を行う:
        - TS コンテナは key_frames から segment_map を生成して保存
        - MPEG-4 コンテナは moov の同期サンプル表を再生時に読むため key_frames だけ破棄
        - 変換後の key_frames は空配列へ戻し、巨大な JSON が残り続けないようにする
        """

        logging.info('Starting keyframe to segment map migration...')

        migrated_count = 0
        repaired_count = 0
        skipped_count = 0
        last_seen_id = 0
        next_progress_log_count = 500

        while True:
            # key_frames は ORM 取得時に list へ復元されるため、Python 側で空配列かどうかを判定する
            ## DB 側で巨大 JSON の文字列比較を走らせず、ID 順に少量ずつ読み出して移行する
            video_rows = await RecordedVideo.filter(
                status = 'Recorded',
                id__gt = last_seen_id,
            ).order_by('id').limit(50).values(
                'id',
                'file_path',
                'duration',
                'container_format',
                'video_frame_rate',
                'key_frames',
                'segment_map',
            )
            if len(video_rows) == 0:
                break

            for video_row in video_rows:
                last_seen_id = video_row['id']

                try:
                    segment_map = video_row['segment_map']
                    if not isinstance(segment_map, list):
                        segment_map = []

                    is_broken_segment_map = False
                    # 旧変換ロジックで同じ入力位置が連続保存された MPEG-TS は、再生時に同じ映像を繰り返す
                    ## key_frames が既に空でも検出できるよう、移行対象判定より先に segment_map を確認する
                    if (
                        video_row['container_format'] == 'MPEG-TS' and
                        len(segment_map) > 0 and
                        VideoSegmentPlanner.isSegmentMapProbablyBroken(cast(list[schemas.SegmentMapEntry], segment_map)) is True
                    ):
                        is_broken_segment_map = True

                    key_frames = video_row['key_frames']
                    if not isinstance(key_frames, list) or len(key_frames) == 0:
                        # 壊れた既存キャッシュだけを空に戻し、通常の未キャッシュ状態としてオンデマンド探索へ戻す
                        ## key_frames が空の録画は旧データから再変換できないため、誤った値を温存しない
                        if is_broken_segment_map is True:
                            await RecordedVideo.filter(id=video_row['id']).update(segment_map = [])
                            repaired_count += 1
                            logging.warning(
                                f'{video_row["file_path"]}: Broken segment map was cleared. '
                                f'[video_id: {video_row["id"]}]'
                            )
                        continue

                    # TS コンテナは既存 key_frames をオンデマンド探索と同じ規則のキャッシュへ変換できる
                    if video_row['container_format'] == 'MPEG-TS':
                        if len(segment_map) == 0 or is_broken_segment_map is True:
                            video_frame_rate = video_row['video_frame_rate']
                            # 旧 DB に壊れたフレームレートが混じっている場合、セグメント長を復元できないため移行対象から外す
                            if (
                                isinstance(video_frame_rate, bool) is True or
                                isinstance(video_frame_rate, int | float) is False
                            ):
                                skipped_count += 1
                                logging.warning(
                                    f'{video_row["file_path"]}: Invalid video frame rate. '
                                    f'[video_id: {video_row["id"]}, video_frame_rate: {video_frame_rate}]'
                                )
                                continue
                            # 0 以下のフレームレートは segment_map の時刻計算で除算できないため移行対象から外す
                            if video_frame_rate <= 0:
                                skipped_count += 1
                                logging.warning(
                                    f'{video_row["file_path"]}: Invalid video frame rate. '
                                    f'[video_id: {video_row["id"]}, video_frame_rate: {video_frame_rate}]'
                                )
                                continue
                            segment_map = VideoSegmentPlanner.convertKeyFramesToSegmentMap(
                                key_frames = key_frames,
                                video_frame_rate = float(video_frame_rate),
                                duration_seconds = video_row['duration'],
                            )

                        await RecordedVideo.filter(id=video_row['id']).update(
                            segment_map = segment_map,
                            key_frames = [],
                        )
                        migrated_count += 1
                    # MP4 は moov から同期サンプル DTS を短時間で復元できるため、巨大な旧キャッシュだけ破棄する
                    else:
                        await RecordedVideo.filter(id=video_row['id']).update(key_frames = [])
                        migrated_count += 1
                except Exception as ex:
                    skipped_count += 1
                    logging.error(f'{video_row["file_path"]}: Failed to migrate keyframes to segment map:', exc_info=ex)

            # 大量の録画を持つ環境では起動直後に沈黙すると不安になるため、500件ごとに進捗をログへ出す
            processed_count = migrated_count + repaired_count + skipped_count
            if processed_count >= next_progress_log_count:
                logging.info(
                    f'Keyframe to segment map migration progress. '
                    f'[processed: {processed_count}, migrated: {migrated_count}, repaired: {repaired_count}, '
                    f'skipped: {skipped_count}]'
                )
                next_progress_log_count += 500

            # 移行処理がイベントループを占有し続けないよう適宜制御を返す
            await asyncio.sleep(0)

        logging.info(
            f'Keyframe to segment map migration completed. '
            f'[migrated: {migrated_count}, repaired: {repaired_count}, skipped: {skipped_count}]'
        )

    async def watchRecordedFolders(self) -> None:
        """
        録画フォルダ以下のファイルシステム変更の監視を開始し、変更があれば随時メタデータを解析後、DB に永続化する
        """

        logging.info('Starting file system watch of recording folders.')

        # 監視対象のディレクトリを設定
        watch_paths = [str(path) for path in self.recorded_folders]

        # スキャン対象から除外するフォルダ
        # 空文字列は全パスにマッチしてしまうため除外する
        exclude_scan_paths = [
            self.__normalizePathForPrefixMatch(pattern)
            for pattern in self.config.video.exclude_scan_paths
            if type(pattern) is str and pattern.strip() != ''
        ]

        # 録画完了チェック用のタスク
        completion_check_task = asyncio.create_task(self.__checkRecordingCompletion())

        try:
            # watchfiles によるファイル監視
            async for changes in awatch(
                *watch_paths,
                recursive=True,
                watch_filter=self.buildRecordedFolderWatchFilter(),
            ):
                if not self._is_running:
                    break

                # 変更があったファイルごとに処理
                for change_type, file_path_str in changes:
                    if not self._is_running:
                        break

                    file_path = anyio.Path(file_path_str)
                    # chapter判定や録画拡張子判定より先に、CM解析workspaceの全イベントを除外する。
                    if CMAnalysisWorkspace.isWorkspacePath(pathlib.Path(file_path_str)):
                        continue
                    # Mac の metadata ファイルをスキップ
                    if file_path.name.startswith('._'):
                        continue
                    # 再生中にも生成・削除イベントが発生するため、キャッシュ管理側だけに処理を任せる。
                    if RecordedFMP4CacheManager.isCacheFileName(file_path.name):
                        await RecordedFMP4CacheManager.cleanupDiscovered(pathlib.Path(str(file_path)))
                        continue
                    # 除外パターンのチェック（シンボリックリンク解決前）
                    original_path_str = str(file_path)
                    if self.isPathExcludedByPatterns(original_path_str, exclude_scan_paths) is True:
                        continue
                    # シンボリックリンクを含むパスは実体に解決して処理する
                    canonical_path = await self.resolveRecordedPath(file_path)
                    if CMAnalysisWorkspace.isWorkspacePath(pathlib.Path(str(canonical_path))):
                        continue
                    # 除外パターンのチェック（シンボリックリンク解決後）
                    canonical_path_str = str(canonical_path)
                    if self.isPathExcludedByPatterns(canonical_path_str, exclude_scan_paths) is True:
                        continue
                    if await canonical_path.is_dir():
                        continue
                    # chapterイベントは通常の録画拡張子フィルターより先に元録画へ関連付ける。
                    # 削除イベントでは実体が存在しないため、イベント種別を問わずファイル名から逆引きする。
                    if canonical_path.name.lower().endswith(('.chapter.txt', '.konomitv-bs4k-chapters.yaml')):
                        try:
                            await self.__handleChapterFileChange(canonical_path)
                        except Exception as ex:
                            logging.error(f'{file_path}: Error handling chapter file change:', exc_info=ex)
                        continue
                    # 対象拡張子のファイル以外は無視
                    if canonical_path.suffix.lower() not in self.SCAN_TARGET_EXTENSIONS:
                        continue

                    try:
                        # 追加 or 変更イベント
                        if change_type == Change.added or change_type == Change.modified:
                            await self.__handleFileChange(canonical_path, original_file_path=file_path)
                        # 削除イベント
                        elif change_type == Change.deleted:
                            await self.__handleFileDeletion(canonical_path, original_file_path=file_path)
                    except Exception as ex:
                        logging.error(f'{file_path}: Error handling file change:', exc_info=ex)

        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logging.error('Error in file system watch of recording folders:', exc_info=ex)
        finally:
            completion_check_task.cancel()
            try:
                await completion_check_task
            except asyncio.CancelledError:
                pass
            logging.info('File system watch of recording folders has been stopped.')


    async def __handleChapterFileChange(self, chapter_file_path: anyio.Path) -> None:
        """追加・更新・削除されたchapterを対応する録画へ同期する。

        Args:
            chapter_file_path: watchfilesが通知したchapterファイルパス。

        Returns:
            None
        """

        # KonomiTV-BS4K YAMLは録画ファイル名を拡張子ごと保持するため、基本名を推測せず完全一致で対応付ける。
        recorded_path = GetRecordedPathFromKonomiTVBS4KChapterPath(
            pathlib.Path(str(chapter_file_path)),
            set(self.SCAN_TARGET_EXTENSIONS),
        )
        if recorded_path is not None:
            recorded_video = await RecordedVideo.get_or_none(file_path=str(recorded_path))
            if recorded_video is None:
                # 元録画がシンボリックリンクの場合も、解決後の完全パスだけを照合する。
                try:
                    resolved_recorded_path = await asyncio.to_thread(recorded_path.resolve)
                except (OSError, RuntimeError):
                    resolved_recorded_path = recorded_path
                if resolved_recorded_path != recorded_path:
                    recorded_video = await RecordedVideo.get_or_none(file_path=str(resolved_recorded_path))
            if recorded_video is None:
                logging.debug(
                    f'{chapter_file_path}: Corresponding recorded video was not found. Skipping chapter sync.'
                )
                return
            await CMAnalysisOrchestrator().run(recorded_video.id, 'CMChapterSync')
            return

        candidate_paths = GetRecordedPathFromCMChapterPath(
            pathlib.Path(str(chapter_file_path)),
            set(self.SCAN_TARGET_EXTENSIONS),
        )
        if len(candidate_paths) == 0:
            return

        # 基本名候補をすべてDBと照合してから、所有者が一意な場合だけ同期する。
        # QuerySet.first()へ任せると、同一stemの別拡張子録画へchapterを誤同期する可能性がある。
        chapter_path = pathlib.Path(str(chapter_file_path))
        base_name = chapter_path.name[:-len('.chapter.txt')]
        recorded_videos = await RecordedVideo.filter(
            file_path__istartswith=str(chapter_path.with_name(base_name)),
        ).all()
        candidate_to_recorded_video = {
            pathlib.Path(recorded_video.file_path): recorded_video
            for recorded_video in recorded_videos
        }

        # 元録画がシンボリックリンクの場合もあるため、保存パスで見つからない候補だけ実体へ解決する。
        for candidate in candidate_paths:
            if candidate in candidate_to_recorded_video:
                continue
            try:
                resolved_candidate = await asyncio.to_thread(candidate.resolve)
            except (OSError, RuntimeError):
                continue
            recorded_video = await RecordedVideo.get_or_none(file_path=str(resolved_candidate))
            if recorded_video is not None:
                candidate_to_recorded_video[candidate] = recorded_video

        selection = SelectRecordedPathForCMChapter(
            pathlib.Path(str(chapter_file_path)),
            candidate_to_recorded_video.keys(),
            set(self.SCAN_TARGET_EXTENSIONS),
        )
        if selection is None:
            logging.debug(
                f'{chapter_file_path}: Corresponding recorded video was not found or was ambiguous. '
                'Skipping chapter sync.'
            )
            return
        recorded_video = candidate_to_recorded_video[selection.recorded_file_path]
        # 追加・変更・削除の区別と公開結果の更新は、chapter内容と保存済みfingerprintを知るOrchestratorへ委ねる。
        await CMAnalysisOrchestrator().run(recorded_video.id, 'CMChapterSync')


    async def __handleFileChange(self, file_path: anyio.Path, original_file_path: anyio.Path | None = None) -> None:
        """
        ファイル追加・変更イベントを受け取り、適切な頻度で __processFile() を呼び出す
        - 録画中ファイルの状態管理
        - メタデータ解析のスロットリング
        - 最終更新日時の継続更新検出による録画中判定

        Args:
            file_path (anyio.Path): 解決後のファイルパス
            original_file_path (anyio.Path | None): 監視で検知した元のファイルパス
        """

        try:
            # ファイルの状態をチェック
            stat = await file_path.stat()
            last_modified = datetime.fromtimestamp(stat.st_mtime, tz=JST)
            now = datetime.now(tz=JST)
            file_size = stat.st_size

            # 既に録画中とマークされているファイルの処理
            if file_path in self._recording_files:
                recording_info = self._recording_files[file_path]
                last_checked = recording_info.last_checked
                last_size = recording_info.file_size
                mtime_continuous_start_at = recording_info.mtime_continuous_start_at

                # 前回のチェックから UPDATE_THROTTLE_SECONDS 秒以上経過していない場合はログを間引く（状態自体は更新する）
                throttle_event = False
                if (now - last_checked).total_seconds() < self.UPDATE_THROTTLE_SECONDS:
                    throttle_event = True

                # ファイルサイズが変化している場合は継続更新判定をリセット
                if file_size != last_size:
                    mtime_continuous_start_at = None
                    if not throttle_event:
                        logging.debug(f'{file_path}: File size changed.')
                # mtime が変化している場合は継続更新判定を更新
                elif last_modified > recording_info.last_modified:
                    if mtime_continuous_start_at is None:
                        mtime_continuous_start_at = last_modified
                        if not throttle_event:
                            logging.debug(f'{file_path}: File modified time changed.')
                    else:
                        continuous_duration = (now - mtime_continuous_start_at).total_seconds()
                        if continuous_duration >= self.CONTINUOUS_UPDATE_THRESHOLD_SECONDS:
                            if not throttle_event:
                                logging.debug(f'{file_path}: Still recording. (continuous mtime updates for {continuous_duration:.1f} seconds)')

                # 状態を更新
                recording_info.last_modified = last_modified
                # 前回のチェックから UPDATE_THROTTLE_SECONDS 秒以上経過していない場合は前回のチェック日時を使う
                recording_info.last_checked = last_checked if throttle_event else now
                recording_info.file_size = file_size
                recording_info.mtime_continuous_start_at = mtime_continuous_start_at

                # メタデータ解析を実行
                await self.processRecordedFile(file_path, original_file_path)

            # まだ録画中とマークされていないファイルの処理
            else:
                # 最終更新時刻から一定時間以上経過している場合は録画中とみなさない
                # それ以外の場合、今後継続的に追記されていく（＝録画中）可能性もあるので、録画中マークをつけておく
                if (now - last_modified).total_seconds() <= self.RECORDING_MAX_AGE_SECONDS:
                    self._recording_files[file_path] = FileRecordingInfo(
                        last_modified = last_modified,
                        last_checked = now,
                        file_size = file_size,
                        mtime_continuous_start_at = last_modified,  # 初回は必ず mtime_continuous_start_at を設定
                    )
                    logging.info(f'{file_path}: New recording or copying file detected.')

                # メタデータ解析を実行
                await self.processRecordedFile(file_path, original_file_path)

        except FileNotFoundError:
            # ファイルが既に削除されている場合
            pass
        except Exception as ex:
            logging.error(f'{file_path}: Error handling file change:', exc_info=ex)


    async def __handleFileDeletion(self, file_path: anyio.Path, original_file_path: anyio.Path | None = None) -> None:
        """
        ファイル削除イベントを受け取り、DB からレコードを削除する

        Args:
            file_path (anyio.Path): 解決後の削除対象ファイルパス
            original_file_path (anyio.Path | None): 監視で検知した元のファイルパス
        """

        # 同一ファイルパスへの DB レコード操作を排他制御する
        async with self.fileLock(file_path):
            try:
                mapped_canonical_path: str | None = None
                async with self._symlink_path_map_lock:
                    if original_file_path is not None:
                        mapped_canonical_path = self._symlink_path_map.pop(str(original_file_path), None)
                    if mapped_canonical_path is None:
                        mapped_canonical_path = self._symlink_path_map.pop(str(file_path), None)
                if mapped_canonical_path is not None:
                    file_path = anyio.Path(mapped_canonical_path)

                # 録画中とマークされていたファイルの場合は記録から削除
                self._recording_files.pop(file_path, None)

                # DB からレコードを削除
                db_recorded_video = await RecordedVideo.get_or_none(file_path=str(file_path))
                if db_recorded_video is None and original_file_path is not None:
                    db_recorded_video = await RecordedVideo.get_or_none(file_path=str(original_file_path))
                if db_recorded_video is not None:
                    # 削除APIが録画本体を削除した後に失敗したレコードは、同じAPIからの再試行対象として保持する
                    # watcherは削除APIのpath lock解放後に到達するため、lockだけでなく永続statusも必ず確認する
                    if db_recorded_video.status in ('Deleting', 'DeleteFailed'):
                        logging.info(
                            f'{file_path}: Preserved record for deletion retry. '
                            f'Status: {db_recorded_video.status}'
                        )
                        return

                    # RecordedVideo の親テーブルである RecordedProgram を削除すると、
                    # CASCADE 制約により RecordedVideo も同時に削除される (Channel は親テーブルにあたるため削除されない)
                    await db_recorded_video.recorded_program.delete()
                    logging.info(f'{file_path}: Deleted record for removed file.')

            except Exception as ex:
                logging.error(f'{file_path}: Error handling file deletion inside lock:', exc_info=ex)


    async def __checkRecordingCompletion(self) -> None:
        """
        録画 (またはファイルコピー) の完了状態を定期的にチェックする
        - 30秒間ファイルの更新がない場合に録画完了 (またはファイルコピー完了) と判断
        - 完了したファイルは再度メタデータを解析して DB に保存
        """

        while self._is_running:
            try:
                now = datetime.now(tz=JST)
                completed_files: list[tuple[anyio.Path, FileRecordingInfo]] = []

                # await中にwatcherが辞書を変更できるため、周期開始時点のsnapshotだけを巡回する。
                recording_files_snapshot = tuple(self._recording_files.items())
                for file_path, recording_info in recording_files_snapshot:
                    try:
                        # ファイルの現在の状態を取得
                        stat = await file_path.stat()
                        current_modified = datetime.fromtimestamp(stat.st_mtime, tz=JST)
                        current_size = stat.st_size

                        # stat()待機中に削除・差し替えられた状態は、現entryを誤完了させず次周期へ送る。
                        if self._recording_files.get(file_path) is not recording_info:
                            continue

                        # RECORDING_COMPLETE_SECONDS 秒以上更新がなく、かつファイルサイズが変化していない場合は録画完了と判断
                        if ((now - current_modified).total_seconds() >= self.RECORDING_COMPLETE_SECONDS and
                            current_size == recording_info.file_size):
                            completed_files.append((file_path, recording_info))
                    except FileNotFoundError:
                        # snapshotと同じentryが残っている場合だけ、削除済み録画として完了処理へ送る。
                        if self._recording_files.get(file_path) is recording_info:
                            completed_files.append((file_path, recording_info))
                    except Exception as ex:
                        logging.error(f'{file_path}: Error checking recording completion:', exc_info=ex)

                # 完了したファイルを処理
                for file_path, recording_info in completed_files:
                    try:
                        # 巡回完了後にもwatcherが状態を更新できるため、同じsnapshot entryだけを処理する。
                        if self._recording_files.get(file_path) is not recording_info:
                            continue
                        # 記録から削除
                        self._recording_files.pop(file_path, None)

                        # ファイルが存在する場合のみ再解析
                        if await self.isFileExists(file_path):
                            # この時点で、録画（またはファイルコピー）が確実に完了しているはず
                            logging.info(f'{file_path}: Recording or copying has just completed or has already completed.')
                            await self.processRecordedFile(file_path)
                    except Exception as ex:
                        logging.error(f'{file_path}: Error processing completed file:', exc_info=ex)

            except asyncio.CancelledError:
                raise
            except Exception as ex:
                logging.error('Error in recording completion check:', exc_info=ex)

            # 5秒待機
            await asyncio.sleep(5)
