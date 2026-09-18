import asyncio
import fcntl
import os
from pathlib import Path
from typing import ClassVar, cast
from uuid import uuid4

from app import logging
from app.constants import DATA_DIR
from app.models.KonomiTVBS4KCloudTransfer import (
    KonomiTVBS4KCloudRecording,
    KonomiTVBS4KCloudTransfer,
)
from app.models.RecordedVideo import RecordedVideo
from app.models.User import User
from app.utils.KonomiTVBS4KCloudRemote import (
    KonomiTVBS4KCloudKeyChanged,
    KonomiTVBS4KCloudRemote,
)
from app.utils.KonomiTVBS4KCloudTransferStore import KonomiTVBS4KCloudTransferStore


class KonomiTVBS4KCloudTransferManager:
    """SQLiteの移動要求を単一実行者が処理し、起動時に旧転送を停止して同じ段階から復旧する。"""

    _task: ClassVar[asyncio.Task[None] | None] = None
    _lock_fd: ClassVar[int | None] = None
    _wake: ClassVar[asyncio.Event] = asyncio.Event()
    _state_lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    # スキャナーなどの同一プロセス内のガード用。開始前にDBから復旧し、受付・完了時も所有者が更新する。
    _protected_ids: ClassVar[set[int]] = set()
    _active_ids: ClassVar[set[int]] = set()

    @classmethod
    def protects(cls, video_id: int) -> bool:
        """スキャナーによるパス重複・不在回収から保持すべき録画かを返す。

        Args:
            video_id: RecordedVideoのID。
        Returns:
            クラウド所在または未完了ジョブが占有しているか。
        """
        return video_id in cls._protected_ids

    @classmethod
    def isActive(cls, video_id: int) -> bool:
        """同一プロセス内の解析・削除と移動の競合を防ぐため、占有を返す。

        Args:
            video_id: RecordedVideoのID。
        Returns:
            失敗・取消清掃を含む未完了ジョブが占有しているか。
        """
        return video_id in cls._active_ids

    @classmethod
    async def refresh(cls) -> None:
        """永続状態のcommit後に、同一プロセスのガードを更新する。

        Args:
            None
        Returns:
            None
        """
        async with cls._state_lock:
            active = cast(list[int], await KonomiTVBS4KCloudTransfer.filter(active_video_id__isnull=False).values_list('recorded_video_id', flat=True))
            cloud = cast(list[int], await KonomiTVBS4KCloudRecording.filter(location='Cloud').values_list('recorded_video_id', flat=True))
            cls._active_ids = set(active)
            cls._protected_ids = cls._active_ids | set(cloud)
        cls._wake.set()

    @classmethod
    async def start(cls) -> None:
        """スキャナーより先にガードを復旧し、二重起動を拒否してworkerを開始する。

        Args:
            None
        Returns:
            None
        """
        if cls._task is not None:
            return
        fd = os.open(DATA_DIR / '.konomitv-bs4k-cloud-worker.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await cls.refresh()
        except BaseException:
            os.close(fd)
            raise
        cls._lock_fd = fd
        cls._task = asyncio.create_task(cls._run(), name='KonomiTVBS4KCloudTransferManager')

    @classmethod
    async def stop(cls) -> None:
        """同期I/Oの回収を待ってからプロセス内workerの所有権を解放する。

        Args:
            None
        Returns:
            None
        """
        if cls._task is not None:
            cls._task.cancel()
            try:
                await cls._task
            except asyncio.CancelledError:
                pass
            cls._task = None
        if cls._lock_fd is not None:
            os.close(cls._lock_fd)
            cls._lock_fd = None

    @classmethod
    async def _run(cls) -> None:
        """失敗したジョブを勝手に放棄せず、受付済み要求と異常終了の復旧だけを処理する。

        Args:
            None
        Returns:
            None
        """
        # 相互参照する録画パイプラインの初期化が完了した後に読み込む。
        import anyio

        from app.metadata.CMAnalysisOrchestrator import CMAnalysisOrchestrator
        from app.metadata.RecordedScanTask import RecordedScanTask
        from app.utils.KonomiTVBS4KCloudTransferWorker import (
            KonomiTVBS4KCloudTransferWorker,
        )

        # 完了commit直後の終了で残った一時参照を回収する。失敗しても、必須の移動状態は変更しない。
        for completed in await KonomiTVBS4KCloudTransfer.filter(status__in=['Completed', 'Cancelled']):
            try:
                await KonomiTVBS4KCloudTransferWorker(completed, uuid4()).cleanupStage()
            except (OSError, ValueError):
                logging.warning(f'[KonomiTVBS4KCloudTransferManager] Completed staging cleanup failed. Job: {completed.id}')
        while True:
            cls._wake.clear()
            try:
                # このmanagerはファイルlockで単独所有している。Runningは前プロセスの残留だけ。
                interrupted = await KonomiTVBS4KCloudTransfer.filter(status='Running')
                for job in interrupted:
                    await asyncio.to_thread(KonomiTVBS4KCloudRemote.stopConnection, job.owner_id, job.connection_id)
                    await KonomiTVBS4KCloudTransfer.filter(id=job.id, status='Running', worker_token=job.worker_token).update(
                        status='Pending', worker_token=None,
                    )
                queued = await KonomiTVBS4KCloudTransfer.filter(status='Pending').order_by('created_at').first()
                if queued is not None:
                    token = uuid4()
                    job = await KonomiTVBS4KCloudTransferStore.claim(queued.id, token)
                    if job is not None:
                        try:
                            # 既存解析・削除と同じロックを取得し、開始直前までの処理が終わってから対象を確定する。
                            scanner = RecordedScanTask()
                            path = await scanner.resolveRecordedPath(anyio.Path(job.local_path))
                            async with scanner.fileLock(path), CMAnalysisOrchestrator.recordingLock(job.recorded_video_id):
                                await KonomiTVBS4KCloudTransferWorker(job, token).run()
                        except asyncio.CancelledError:
                            # workerが同期I/Oを停止確認してからここへ戻る。段階は維持して次回起動へ渡す。
                            raise
                        except Exception as error:
                            # provider・鍵・ファイル名を含む下位例外を通常APIやログへ反射しない回復境界。
                            await KonomiTVBS4KCloudTransferStore.fail(job.id, token,
                                'CryptKeyChanged' if isinstance(error, KonomiTVBS4KCloudKeyChanged) else 'TransferFailed')
                            logging.warning(f'[KonomiTVBS4KCloudTransferManager] Transfer failed. Job: {job.id}')
                        finally:
                            await cls.refresh()
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                # DBやsupervisorの障害では占有を保持する。次の有限間隔で復旧を試し、別workerを作らない。
                logging.error('[KonomiTVBS4KCloudTransferManager] Transfer scheduler recovery failed.')
            try:
                await asyncio.wait_for(cls._wake.wait(), timeout=5)
            except TimeoutError:
                pass

    @staticmethod
    async def resolvePath(video_id: int, stored_path: str) -> Path:
        """クラウド録画の読取り前にmountを復旧し、ローカル録画はそのまま返す。

        Args:
            video_id: RecordedVideoのID。
            stored_path: DBが現在指す読取り先。
        Returns:
            現在の接続で読めるパス。到達不能をローカル消失として扱わない。
        """
        path = Path(stored_path)
        if KonomiTVBS4KCloudTransferManager.protects(video_id) or path.is_relative_to('/cloud-mounts'):
            current_paths = cast(list[str], await RecordedVideo.filter(id=video_id).values_list('file_path', flat=True))
            if not current_paths:
                raise FileNotFoundError('Cloud recording is no longer registered.')
            path = Path(current_paths[0])
        if not path.is_relative_to('/cloud-mounts'):
            return path
        location = await KonomiTVBS4KCloudRecording.get(recorded_video_id=video_id, location='Cloud')
        # 既存mountを使う読取りは、別録画の長時間アップロードが保持する所有者lockを必要としない。
        if len(path.parents) >= 4 and await asyncio.to_thread(path.parents[3].is_mount):
            return path
        loop = asyncio.get_running_loop()
        def OwnerExists() -> bool:
            async def Check() -> bool:
                return await User.filter(id=location.owner_id, is_admin=True).exists()
            future = asyncio.run_coroutine_threadsafe(Check(), loop)
            try:
                return future.result(timeout=10)
            finally:
                if not future.done():
                    future.cancel()
        with KonomiTVBS4KCloudRemote.keyIdentity(location.key_identity):
            root = await asyncio.to_thread(KonomiTVBS4KCloudRemote.ensureMount, location.owner_id, location.connection_id,
                                           location.folder, owner_is_active=OwnerExists)
        return root / f'recordings/{location.id}/files/{path.name}'
