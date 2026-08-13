from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import BinaryIO, ClassVar

from app import logging, schemas
from app.constants import KONOMITV_BS4K_OFFLINE_JOBS_DIR
from app.streams.KonomiTVBS4KOfflineStream import KonomiTVBS4KOfflineStream
from app.streams.RecordedFMP4Stream import RecordedFMP4Stream


KonomiTVBS4KOfflineStreamFactory = Callable[
    [str],
    Awaitable[tuple[RecordedFMP4Stream, schemas.KonomiTVBS4KOfflineStreamMetadata]],
]


class KonomiTVBS4KOfflineJobNotFoundError(Exception):
    """指定されたオフライン保存ジョブが存在しないことを表す。"""


class KonomiTVBS4KOfflineJobConflictError(Exception):
    """同じ録画番組の生成ジョブがすでに実行中であることを表す。"""


class KonomiTVBS4KOfflineJobNotReadyError(Exception):
    """完成済みパッケージがまだ配信可能でないことを表す。"""


class KonomiTVBS4KOfflineJobManager:
    """HTTP 接続から独立したオフライン保存パッケージ生成ジョブを管理する。"""

    JOBS_DIR: ClassVar[Path] = KONOMITV_BS4K_OFFLINE_JOBS_DIR
    MAX_CONCURRENT_JOBS: ClassVar[int] = 3
    RETENTION_SECONDS: ClassVar[float] = 24 * 60 * 60
    _jobs: ClassVar[dict[str, schemas.KonomiTVBS4KOfflineJob]] = {}
    _tasks: ClassVar[dict[str, asyncio.Task[None]]] = {}
    _semaphore: ClassVar[asyncio.Semaphore | None] = None
    _mutation_lock: ClassVar[asyncio.Lock | None] = None
    _is_initialized: ClassVar[bool] = False
    _is_stopping: ClassVar[bool] = False

    @classmethod
    def _getStatusPath(cls, job_id: str) -> Path:
        """検証済みジョブ ID に対応する状態ファイルを返す。

        Args:
            job_id: 32桁の小文字16進数ジョブ ID。

        Returns:
            Path: 専用ディレクトリ直下の状態ファイル。
        """

        cls._validateJobID(job_id)
        return cls.JOBS_DIR / f'{job_id}.json'

    @classmethod
    def _getPackagePath(cls, job_id: str) -> Path:
        """検証済みジョブ ID に対応する完成パッケージを返す。

        Args:
            job_id: 32桁の小文字16進数ジョブ ID。

        Returns:
            Path: 専用ディレクトリ直下の完成パッケージ。
        """

        cls._validateJobID(job_id)
        return cls.JOBS_DIR / f'{job_id}.package'

    @staticmethod
    def _validateJobID(job_id: str) -> None:
        """ジョブ ID がパス要素として安全な32桁16進数か検証する。

        Args:
            job_id: API または永続状態から得たジョブ ID。

        Returns:
            None
        """

        if len(job_id) != 32 or any(character not in '0123456789abcdef' for character in job_id):
            raise KonomiTVBS4KOfflineJobNotFoundError('Offline job was not found')

    @classmethod
    def _writeStatusSync(cls, job: schemas.KonomiTVBS4KOfflineJob) -> None:
        """ジョブ状態を fsync と atomic rename で永続化する。

        Args:
            job: 永続化する完全なジョブ状態。

        Returns:
            None
        """

        cls.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        status_path = cls._getStatusPath(job.job_id)
        temporary_path = cls.JOBS_DIR / f'.{job.job_id}.status-{uuid.uuid4().hex}.tmp'
        file_descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(file_descriptor, mode='w', encoding='utf-8') as temporary_file:
                file_descriptor = -1
                temporary_file.write(job.model_dump_json())
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, status_path)
            directory_descriptor = os.open(cls.JOBS_DIR, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
            temporary_path.unlink(missing_ok=True)

    @classmethod
    async def _writeStatus(cls, job: schemas.KonomiTVBS4KOfflineJob) -> None:
        """イベントループを塞がずにジョブ状態を永続化する。

        Args:
            job: 永続化する完全なジョブ状態。

        Returns:
            None
        """

        await asyncio.to_thread(cls._writeStatusSync, job.model_copy(deep=True))

    @classmethod
    def _loadJobsSync(cls) -> dict[str, schemas.KonomiTVBS4KOfflineJob]:
        """起動時に完成パッケージと永続状態を検査して読み込む。

        Returns:
            dict[str, schemas.KonomiTVBS4KOfflineJob]: 復旧できたジョブ状態。
        """

        cls.JOBS_DIR.mkdir(parents=True, exist_ok=True)

        # atomic rename 前の一時ファイルは完成物として参照せず、前回異常終了の残片として回収する。
        for temporary_path in cls.JOBS_DIR.glob('.*.tmp'):
            if temporary_path.is_file():
                temporary_path.unlink(missing_ok=True)

        current_time = time.time()
        jobs: dict[str, schemas.KonomiTVBS4KOfflineJob] = {}
        for status_path in cls.JOBS_DIR.glob('*.json'):
            try:
                job = schemas.KonomiTVBS4KOfflineJob.model_validate_json(status_path.read_text(encoding='utf-8'))
                if status_path.name != f'{job.job_id}.json':
                    raise ValueError('Offline job status filename does not match its job ID')
            except (OSError, ValueError) as ex:
                logging.warning(
                    '[KonomiTVBS4KOfflineJobManager] Removed an invalid offline job status file. '
                    f'[file: {status_path.name}]',
                    exc_info=ex,
                )
                status_path.unlink(missing_ok=True)
                continue

            package_path = cls._getPackagePath(job.job_id)
            if current_time - job.updated_at > cls.RETENTION_SECONDS:
                status_path.unlink(missing_ok=True)
                package_path.unlink(missing_ok=True)
                continue

            # 生成途中のバイト列は公開していないため、再起動後は再開を装わず明示的な失敗へ確定する。
            if job.state in ('Queued', 'Generating'):
                package_path.unlink(missing_ok=True)
                job.state = 'Failed'
                job.phase = 'Failed'
                job.error = 'サーバーが再起動したため、オフライン保存の生成が中断されました。'
                job.updated_at = current_time
                cls._writeStatusSync(job)
            elif job.state == 'Ready':
                package_size = package_path.stat().st_size if package_path.is_file() else -1
                if job.package_size_bytes is None or package_size != job.package_size_bytes:
                    package_path.unlink(missing_ok=True)
                    job.state = 'Failed'
                    job.phase = 'Failed'
                    job.error = '完成済みオフライン保存パッケージを検証できませんでした。'
                    job.package_size_bytes = None
                    job.updated_at = current_time
                    cls._writeStatusSync(job)
            elif job.state == 'Cancelled':
                status_path.unlink(missing_ok=True)
                package_path.unlink(missing_ok=True)
                continue
            jobs[job.job_id] = job

        # status 更新直前の異常終了などで正本から到達不能になった完成パスも、専用ディレクトリ内だけで回収する。
        ready_job_ids = {job.job_id for job in jobs.values() if job.state == 'Ready'}
        for package_path in cls.JOBS_DIR.glob('*.package'):
            if package_path.stem not in ready_job_ids:
                package_path.unlink(missing_ok=True)
        return jobs

    @classmethod
    async def initialize(cls) -> None:
        """永続状態を復旧し、新しいイベントループ用の実行枠を初期化する。

        Returns:
            None
        """

        if cls._is_initialized is True:
            return
        cls._jobs = await asyncio.to_thread(cls._loadJobsSync)
        cls._tasks = {}
        cls._semaphore = asyncio.Semaphore(cls.MAX_CONCURRENT_JOBS)
        cls._mutation_lock = asyncio.Lock()
        cls._is_stopping = False
        cls._is_initialized = True

    @classmethod
    async def _ensureInitialized(cls) -> None:
        """API 単体テストを含め、初回利用前に必ず初期化する。

        Returns:
            None
        """

        if cls._is_initialized is False:
            await cls.initialize()

    @classmethod
    async def createJob(
        cls,
        video_id: int,
        stream_factory: KonomiTVBS4KOfflineStreamFactory,
    ) -> schemas.KonomiTVBS4KOfflineJob:
        """待機状態を永続化し、HTTP 接続と独立した生成 Task を開始する。

        Args:
            video_id: 保存対象の録画番組 ID。
            stream_factory: 実行枠取得後に能力検証済みストリームを作る非同期関数。

        Returns:
            schemas.KonomiTVBS4KOfflineJob: 作成直後の待機状態。
        """

        await cls._ensureInitialized()
        if cls._is_stopping is True or cls._mutation_lock is None:
            raise RuntimeError('Offline job manager is stopping')

        async with cls._mutation_lock:
            # 同じ録画の生成を重ねるとエンコーダーと共有キャッシュを無駄に競合させるため、生成中だけ拒否する。
            if any(job.video_id == video_id and job.state in ('Queued', 'Generating') for job in cls._jobs.values()):
                raise KonomiTVBS4KOfflineJobConflictError('Offline job is already active')

            current_time = time.time()
            job = schemas.KonomiTVBS4KOfflineJob(
                job_id=uuid.uuid4().hex,
                video_id=video_id,
                state='Queued',
                phase='Queued',
                progress=0.0,
                completed_assets=0,
                total_assets=0,
                completed_bytes=0,
                package_size_bytes=None,
                error=None,
                created_at=current_time,
                updated_at=current_time,
            )
            cls._jobs[job.job_id] = job
            await cls._writeStatus(job)
            task = asyncio.create_task(
                cls._runJob(job.job_id, stream_factory),
                name=f'offline-package-{job.job_id}',
            )
            cls._tasks[job.job_id] = task
            return job.model_copy(deep=True)

    @classmethod
    def _applyStreamProgress(
        cls,
        job_id: str,
        snapshot: schemas.KonomiTVBS4KOfflineStreamProgress,
    ) -> None:
        """fMP4 生成進捗を永続ジョブの生成工程へ単調反映する。

        Args:
            job_id: 更新対象のジョブ ID。
            snapshot: 媒体時間とアセット数を合成した生成進捗。

        Returns:
            None
        """

        job = cls._jobs.get(job_id)
        if job is None or job.state != 'Generating':
            return
        job.phase = 'Encoding'
        # 最後の fsync と atomic rename の表示余地を残し、生成中は98%を上限にする。
        job.progress = max(job.progress, min(0.98, snapshot.progress * 0.98))
        job.completed_assets = max(job.completed_assets, snapshot.completed_assets)
        job.total_assets = max(job.total_assets, snapshot.total_assets)
        job.completed_bytes = max(job.completed_bytes, snapshot.completed_bytes)
        job.updated_at = time.time()

    @staticmethod
    def _writePackageChunk(package_file: BinaryIO, chunk: bytes) -> None:
        """生成チャンクを欠落なくパッケージ一時ファイルへ書き込む。

        Args:
            package_file: バイナリ書き込み用ファイルオブジェクト。
            chunk: 独自パッケージ形式の1チャンク。

        Returns:
            None
        """

        written_size = package_file.write(chunk)
        if written_size != len(chunk):
            raise OSError('Offline package write was incomplete')

    @classmethod
    def _publishPackageSync(cls, package_file: BinaryIO, temporary_path: Path, package_path: Path) -> int:
        """一時パッケージを fsync 後に完成パスへ atomic 公開する。

        Args:
            package_file: 全チャンクを書き込み済みのファイルオブジェクト。
            temporary_path: 同一ディレクトリ内の非公開一時パス。
            package_path: Ready 状態だけが参照する完成パス。

        Returns:
            int: 完成パッケージの正確なバイト数。
        """

        package_file.flush()
        os.fsync(package_file.fileno())
        package_size = package_file.tell()
        package_file.close()
        os.replace(temporary_path, package_path)
        directory_descriptor = os.open(cls.JOBS_DIR, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return package_size

    @classmethod
    async def _runJob(
        cls,
        job_id: str,
        stream_factory: KonomiTVBS4KOfflineStreamFactory,
    ) -> None:
        """実行枠取得後に全アセットを生成し、完成パッケージだけを公開する。

        Args:
            job_id: 実行する永続ジョブ ID。
            stream_factory: 保存専用 RecordedFMP4Stream とメタデータを作る関数。

        Returns:
            None
        """

        semaphore = cls._semaphore
        if semaphore is None:
            raise RuntimeError('Offline job manager is not initialized')

        package_path = cls._getPackagePath(job_id)
        temporary_path = cls.JOBS_DIR / f'.{job_id}.package-{uuid.uuid4().hex}.tmp'
        video_stream: RecordedFMP4Stream | None = None
        keep_alive_task: asyncio.Task[None] | None = None
        package_file: BinaryIO | None = None
        try:
            async with semaphore:
                job = cls._jobs.get(job_id)
                if job is None or job.state != 'Queued':
                    return
                job.state = 'Generating'
                job.phase = 'Preparing'
                job.progress = max(job.progress, 0.001)
                job.updated_at = time.time()
                await cls._writeStatus(job)

                # 索引生成と能力検証も切断されないジョブ Task 内で行い、POST 応答を長時間保持しない。
                video_stream, metadata = await stream_factory(job_id)

                async def KeepAlive() -> None:
                    """長時間の連続生成中に録画セッションの無通信破棄を防ぐ。

                    Returns:
                        None
                    """

                    while True:
                        await asyncio.sleep(5)
                        video_stream.keepAlive()

                keep_alive_task = asyncio.create_task(KeepAlive(), name=f'offline-keep-alive-{job_id}')
                offline_stream = KonomiTVBS4KOfflineStream(
                    video_stream,
                    metadata,
                    progress_callback=lambda snapshot: cls._applyStreamProgress(job_id, snapshot),
                )
                package_file = temporary_path.open('xb')
                async for chunk in offline_stream.Generate():
                    await asyncio.to_thread(cls._writePackageChunk, package_file, chunk)

                # 終端レコードまで生成できた後も、fsync と rename が終わるまではダウンロード API へ公開しない。
                job = cls._jobs.get(job_id)
                if job is None or job.state != 'Generating':
                    return
                job.phase = 'Packaging'
                job.progress = max(job.progress, 0.99)
                job.updated_at = time.time()
                await cls._writeStatus(job)
                package_size = await asyncio.to_thread(
                    cls._publishPackageSync,
                    package_file,
                    temporary_path,
                    package_path,
                )
                package_file = None

                job.state = 'Ready'
                job.phase = 'Ready'
                job.progress = 1.0
                job.package_size_bytes = package_size
                job.updated_at = time.time()
                await cls._writeStatus(job)
                logging.info(
                    '[KonomiTVBS4KOfflineJobManager] Offline package is ready. '
                    f'[job_id: {job_id}, video_id: {job.video_id}, bytes: {package_size}]'
                )
        except asyncio.CancelledError:
            # DELETE は先に Cancelled を確定する。サーバー終了時だけ、再試行可能な Failed として残す。
            job = cls._jobs.get(job_id)
            if job is not None and job.state not in ('Cancelled', 'Failed'):
                job.state = 'Failed' if cls._is_stopping is True else 'Cancelled'
                job.phase = 'Failed' if cls._is_stopping is True else 'Cancelled'
                job.error = (
                    'サーバー終了のため、オフライン保存の生成が中断されました。'
                    if cls._is_stopping is True else None
                )
                job.updated_at = time.time()
                await asyncio.shield(cls._writeStatus(job))
            raise
        except Exception as ex:
            # エンコーダー・厳密検証・ファイル I/O のどの失敗も、Task 例外を放置せず永続 Failed へ集約する。
            await asyncio.to_thread(package_path.unlink, missing_ok=True)
            job = cls._jobs.get(job_id)
            if job is not None and job.state not in ('Cancelled', 'Failed'):
                job.state = 'Failed'
                job.phase = 'Failed'
                job.error = 'オフライン保存パッケージの生成または検証に失敗しました。'
                job.package_size_bytes = None
                job.updated_at = time.time()
                await cls._writeStatus(job)
            logging.error(
                '[KonomiTVBS4KOfflineJobManager] Offline package generation failed. '
                f'[job_id: {job_id}]',
                exc_info=ex,
            )
        finally:
            if keep_alive_task is not None:
                keep_alive_task.cancel()
                await asyncio.gather(keep_alive_task, return_exceptions=True)
            if video_stream is not None:
                await video_stream.destroy()
            if package_file is not None:
                package_file.close()
            await asyncio.to_thread(temporary_path.unlink, missing_ok=True)
            current_job = cls._jobs.get(job_id)
            if current_job is None or current_job.state != 'Ready':
                await asyncio.to_thread(package_path.unlink, missing_ok=True)
            current_task = asyncio.current_task()
            if cls._tasks.get(job_id) is current_task:
                cls._tasks.pop(job_id, None)

    @classmethod
    async def getJob(cls, video_id: int, job_id: str) -> schemas.KonomiTVBS4KOfflineJob:
        """録画 ID との対応も検証して現在のジョブ状態を返す。

        Args:
            video_id: API パスの録画番組 ID。
            job_id: API パスのジョブ ID。

        Returns:
            schemas.KonomiTVBS4KOfflineJob: 呼び出し側が変更できない複製状態。
        """

        await cls._ensureInitialized()
        cls._validateJobID(job_id)
        job = cls._jobs.get(job_id)
        if job is None or job.video_id != video_id:
            raise KonomiTVBS4KOfflineJobNotFoundError('Offline job was not found')
        return job.model_copy(deep=True)

    @classmethod
    async def getDownloadPath(cls, video_id: int, job_id: str) -> tuple[Path, int]:
        """Ready 状態とファイルサイズを再検証して完成パッケージを返す。

        Args:
            video_id: API パスの録画番組 ID。
            job_id: API パスのジョブ ID。

        Returns:
            tuple[Path, int]: 完成パッケージパスと正確なサイズ。
        """

        job = await cls.getJob(video_id, job_id)
        if job.state != 'Ready' or job.package_size_bytes is None:
            raise KonomiTVBS4KOfflineJobNotReadyError('Offline package is not ready')
        package_path = cls._getPackagePath(job_id)
        try:
            package_size = await asyncio.to_thread(lambda: package_path.stat().st_size)
        except OSError:
            package_size = -1
        if package_size != job.package_size_bytes:
            # Ready 契約が壊れた状態を繰り返し返さず、正本も Failed へ確定して不完全ファイルを回収する。
            current_job = cls._jobs.get(job_id)
            if current_job is not None and current_job.state == 'Ready':
                current_job.state = 'Failed'
                current_job.phase = 'Failed'
                current_job.error = '完成済みオフライン保存パッケージを検証できませんでした。'
                current_job.package_size_bytes = None
                current_job.updated_at = time.time()
                await asyncio.to_thread(package_path.unlink, missing_ok=True)
                await cls._writeStatus(current_job)
            raise KonomiTVBS4KOfflineJobNotReadyError('Offline package size does not match')
        return package_path, package_size

    @classmethod
    async def deleteJob(cls, video_id: int, job_id: str) -> None:
        """生成を中止し、完成物を含む指定ジョブだけを削除する。

        Args:
            video_id: API パスの録画番組 ID。
            job_id: API パスのジョブ ID。

        Returns:
            None
        """

        await cls._ensureInitialized()
        cls._validateJobID(job_id)
        if cls._mutation_lock is None:
            raise RuntimeError('Offline job manager is not initialized')
        async with cls._mutation_lock:
            job = cls._jobs.get(job_id)
            if job is None or job.video_id != video_id:
                raise KonomiTVBS4KOfflineJobNotFoundError('Offline job was not found')
            task = cls._tasks.get(job_id)
            if job.state in ('Queued', 'Generating'):
                job.state = 'Cancelled'
                job.phase = 'Cancelled'
                job.error = None
                job.updated_at = time.time()
                await cls._writeStatus(job)
            if task is not None:
                task.cancel()

        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        async with cls._mutation_lock:
            cls._tasks.pop(job_id, None)
            cls._jobs.pop(job_id, None)
            await asyncio.to_thread(cls._getPackagePath(job_id).unlink, missing_ok=True)
            await asyncio.to_thread(cls._getStatusPath(job_id).unlink, missing_ok=True)

    @classmethod
    async def cleanupExpired(cls) -> None:
        """保持期限を超えた非実行中ジョブと完成パッケージを回収する。

        Returns:
            None
        """

        await cls._ensureInitialized()
        if cls._mutation_lock is None:
            raise RuntimeError('Offline job manager is not initialized')
        expired_job_ids: list[str] = []
        current_time = time.time()
        async with cls._mutation_lock:
            for job in cls._jobs.values():
                if job.state not in ('Queued', 'Generating') and current_time - job.updated_at > cls.RETENTION_SECONDS:
                    expired_job_ids.append(job.job_id)
            for job_id in expired_job_ids:
                cls._jobs.pop(job_id, None)
                await asyncio.to_thread(cls._getPackagePath(job_id).unlink, missing_ok=True)
                await asyncio.to_thread(cls._getStatusPath(job_id).unlink, missing_ok=True)

    @classmethod
    async def stop(cls) -> None:
        """実行中ジョブを失敗へ確定し、全生成 Task を停止する。

        Returns:
            None
        """

        if cls._is_initialized is False:
            return
        cls._is_stopping = True
        tasks = list(cls._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        cls._tasks.clear()
