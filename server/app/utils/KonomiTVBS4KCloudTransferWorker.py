import asyncio
import hashlib
import os
import shutil
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from app import logging, schemas
from app.config import Config
from app.constants import THUMBNAILS_DIR
from app.metadata.KonomiTVBS4KChapterFile import GetKonomiTVBS4KChapterPath
from app.metadata.RecordedScanTask import RecordedScanTask
from app.models.AnalysisTask import AnalysisTaskExecution
from app.models.KonomiTVBS4KCloudTransfer import KonomiTVBS4KCloudTransfer
from app.models.RecordedProgram import RecordedProgram
from app.models.User import User
from app.utils.HostPath import DOCKER_HOST_ROOT, ToRuntimePath
from app.utils.KonomiTVBS4KCloudManifest import (
    KonomiTVBS4KCloudDeletion,
    KonomiTVBS4KCloudLocalFile,
    KonomiTVBS4KCloudManifest,
    KonomiTVBS4KCloudManifestFile,
    KonomiTVBS4KCloudTransferPlan,
)
from app.utils.KonomiTVBS4KCloudRemote import (
    KonomiTVBS4KCloudRemote,
    KonomiTVBS4KCloudTransferCancelled,
)
from app.utils.KonomiTVBS4KCloudTransferStore import (
    KonomiTVBS4KCloudTransferConflict,
    KonomiTVBS4KCloudTransferStore,
)


class KonomiTVBS4KCloudTransferWorker:
    """永続ジョブの一実行を所有し、転送・全量検証・公開と取消清掃を段階順に実行する。"""

    def __init__(self, job: KonomiTVBS4KCloudTransfer, token: UUID) -> None:
        """claim済みのジョブを実行するための、プロセス内資源だけを初期化する。

        Args:
            job: SQLiteでclaim済みのジョブ。対象・方向・接続は受付時から変更しない。
            token: このworkerだけの占有トークン。
        Returns:
            None
        """
        # 各段階でrefreshし、取消要求と現在の占有をSQLiteから確認する。
        self.job = job
        # advance / failが古い実行からの更新を拒否するために使用する。
        self.token = token
        # 同期RC処理へ、イベントループが確認した取消・shutdownを伝える。
        self.stop_requested = threading.Event()
        # ownerLock内から管理者の現存を確認するDB要求を、このループへ戻す。
        self.loop = asyncio.get_running_loop()
        # 転送元・復元先の許可範囲。HTTP要求由来の値ではなくサーバー設定だけを正本にする。
        self.roots = [ToRuntimePath(path).resolve(strict=True) for path in Config().video.recorded_folders]
        # ローカルの副作用をジョブUUID配下へ閉じ、取消で他のファイルを回収しない。
        self.stage = self.localPath(Path(job.local_path)).parent / '.konomitv-bs4k-cloud-transfer' / str(job.id)

    def localPath(self, path: Path) -> Path:
        """設定済みの録画領域内だけをローカルI/Oの対象にする。

        Args:
            path: 永続ジョブが固定したパス。
        Returns:
            正規化済みのパス。ホスト全体やsymlinkによる領域外への脱出は拒否する。
        """
        resolved = ToRuntimePath(path).resolve()
        if not any(root != DOCKER_HOST_ROOT and root.is_relative_to(DOCKER_HOST_ROOT)
                   and resolved.is_relative_to(root) and resolved != root for root in self.roots):
            raise ValueError('Cloud transfer path is outside the recording folders.')
        return resolved

    def ownerExists(self) -> bool:
        """同期RC処理の所有者lock内で、管理者の現存を確認する。

        Args:
            None
        Returns:
            現在も管理者であるか。DB待機は有限とする。
        """
        async def Check() -> bool:
            return await User.filter(id=self.job.owner_id, is_admin=True).exists()
        future = asyncio.run_coroutine_threadsafe(Check(), self.loop)
        try:
            return future.result(timeout=10)
        finally:
            if not future.done():
                future.cancel()

    async def io[T](self, operation: Callable[[], T]) -> T:
        """取消要求を監視しながら同期I/Oを待ち、スレッドだけを置き去りにしない。

        Args:
            operation: 有限待機または取消確認を持つI/O処理。
        Returns:
            同期I/Oの結果。
        """
        task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.5)
                await self.job.refresh_from_db()
                if self.job.worker_token != self.token or self.job.cancel_requested:
                    self.stop_requested.set()
            return task.result()
        except BaseException:
            # shutdownも含めて、旧書込みを停止確認してから実行所有者を手放す。
            self.stop_requested.set()
            try:
                await asyncio.shield(task)
            except KonomiTVBS4KCloudTransferCancelled:
                pass
            raise

    def prepareStage(self) -> None:
        """ジョブ専用の一時領域を作り、別領域へのsymlinkを拒否する。

        Args:
            None
        Returns:
            None
        """
        if self.stage.resolve() != self.stage:
            raise ValueError('Cloud transfer staging directory must not be a symlink.')
        self.stage.mkdir(mode=0o700, parents=True, exist_ok=True)

    async def cleanupStage(self) -> None:
        """完了済みジョブが残した一時ファイルだけを回収する。

        Args:
            None
        Returns:
            None。復元先へのhardlinkは残し、一時領域の参照だけを外す。
        """
        if self.stage.exists():
            if self.stage.resolve() != self.stage:
                raise ValueError('Cloud transfer staging directory changed.')
            await asyncio.to_thread(shutil.rmtree, self.stage)

    async def prepareUpload(self) -> KonomiTVBS4KCloudTransferPlan:
        """完成録画と関連ファイルの集合を、コピーより前に固定する。

        Args:
            None
        Returns:
            公開目録とローカル専用の転送・清掃対象集合。
        """
        program = await RecordedProgram.get(recorded_video__id=self.job.recorded_video_id).select_related('recorded_video', 'channel')
        video = program.recorded_video
        if video.status != 'Recorded' or video.playback_index_status != 'Ready':
            raise ValueError('Recording analysis must complete before a cloud move.')
        if await AnalysisTaskExecution.filter(recorded_video_id=video.id, status__in=['Queued', 'Running']).exists():
            raise ValueError('Recording analysis is still active.')
        source = self.localPath(Path(self.job.local_path))
        source_stat = source.stat(follow_symlinks=False)
        if source_stat.st_size != video.file_size or datetime.fromtimestamp(source_stat.st_mtime, tz=UTC) != video.file_modified_at:
            raise ValueError('Recording changed since its metadata was analyzed.')
        self.prepareStage()
        candidates: list[tuple[Path, Literal['Recording', 'Sidecar', 'Thumbnail'], bool]] = [(source, 'Recording', True)]
        # stemを共有する別コンテナの録画がある場合、共有sidecarを転送しても原本は残す。
        shared_stem = any(source.with_suffix(extension).is_file() for extension in RecordedScanTask.SCAN_TARGET_EXTENSIONS
                          if extension != source.suffix.lower())
        for sidecar in (source.with_suffix('.psc'), source.with_suffix('.vtt'), source.with_suffix('.chapter.txt')):
            if sidecar.is_file():
                candidates.append((sidecar, 'Sidecar', not shared_stem))
        chapter = GetKonomiTVBS4KChapterPath(source)
        if chapter.is_file():
            candidates.append((chapter, 'Sidecar', True))
        # サムネイルは同じfile_hashの録画から共有され得るため、移動元清掃では消さない。
        for suffix in ('.webp', '.jpg', '_tile.webp', '_tile.jpg'):
            thumbnail = THUMBNAILS_DIR / f'{video.file_hash}{suffix}'
            if thumbnail.is_file():
                staged = self.stage / thumbnail.name
                await self.io(lambda thumbnail=thumbnail, staged=staged: shutil.copyfile(thumbnail, staged))
                candidates.append((staged, 'Thumbnail', False))
        files: list[KonomiTVBS4KCloudManifestFile] = []
        local_files: list[KonomiTVBS4KCloudLocalFile] = []
        for path, kind, remove_source in candidates:
            if self.stop_requested.is_set():
                raise KonomiTVBS4KCloudTransferCancelled()
            local, digest = await self.io(lambda path=path, remove_source=remove_source:
                                         KonomiTVBS4KCloudLocalFile.capture(path, remove_source=remove_source,
                                                                          cancellation_requested=self.stop_requested.is_set))
            local_files.append(local)
            if kind == 'Recording':
                current = path.stat(follow_symlinks=False)
                local.validateStat(current)
                if datetime.fromtimestamp(current.st_mtime, tz=UTC) != video.file_modified_at:
                    raise ValueError('Recording changed before its transfer plan was fixed.')
            files.append(KonomiTVBS4KCloudManifestFile(
                name=path.name, kind=kind, size=local.size, sha256=digest, modified_ns=local.modified_ns,
            ))
        portable = schemas.RecordedProgram.model_validate(program, from_attributes=True)
        # DB連番と元のホストパスは、他インスタンスの識別子・復元先として使わせない。
        portable.id = 0
        portable.recorded_video.id = 0
        portable.recorded_video.file_path = source.name
        portable.series_id = None
        portable.series_broadcast_period_id = None
        portable.series_episode = None
        manifest = KonomiTVBS4KCloudManifest(recording_uuid=self.job.recording_uuid, change_uuid=uuid4(), files=files, program=portable)
        return KonomiTVBS4KCloudTransferPlan(manifest=manifest, local_files=local_files)

    async def upload(self, source: Path, key: str, *, cancellable: bool) -> None:
        """既存のRCコピーを、永続ジョブの固定した接続と取消要求へ接続する。

        Args:
            source: 共有済みの通常ファイル。
            key: 暗号化領域内の論理名。
            cancellable: 公開前だけTrue。
        Returns:
            None
        """
        await self.io(lambda: KonomiTVBS4KCloudRemote.uploadFile(
            self.job.owner_id, self.job.connection_id, self.job.folder, source, key,
            recorded_folders=self.roots, owner_is_active=self.ownerExists, timeout=24 * 3600,
            cancellation_requested=self.stop_requested.is_set if cancellable else None,
        ))

    async def verify(self, key: str, digest: str, *, cancellable: bool) -> None:
        """mountのキャッシュではなくクラウド原本を復号して全量照合する。

        Args:
            key: 暗号化領域内の論理名。
            digest: 確定済み原本のSHA-256。
            cancellable: 公開前だけTrue。
        Returns:
            None
        """
        await self.io(lambda: KonomiTVBS4KCloudRemote.verifyFile(
            self.job.owner_id, self.job.connection_id, self.job.folder, key, digest,
            owner_is_active=self.ownerExists, timeout=24 * 3600,
            cancellation_requested=self.stop_requested.is_set if cancellable else None,
        ))

    async def mount(self) -> Path:
        """録画の接続に対応する読取り専用mountを準備する。

        Args:
            None
        Returns:
            mountの起点。
        """
        return await self.io(lambda: KonomiTVBS4KCloudRemote.ensureMount(
            self.job.owner_id, self.job.connection_id, self.job.folder, owner_is_active=self.ownerExists,
        ))

    async def cancel(self) -> None:
        """書込み停止を確認し、未公開の転送先だけを清掃してから占有を解放する。

        Args:
            None
        Returns:
            None。停止や清掃の失敗は呼出し元でFailedとして保存する。
        """
        await self.io(lambda: KonomiTVBS4KCloudRemote.stopConnection(self.job.owner_id, self.job.connection_id))
        await KonomiTVBS4KCloudTransferStore.beginCancellation(self.job.id, self.token)
        if self.job.direction == 'ToCloud':
            await self.io(lambda: KonomiTVBS4KCloudRemote.removeUnpublishedRecording(
                self.job.owner_id, self.job.connection_id, self.job.folder, self.job.recording_uuid,
                owner_is_active=self.ownerExists,
            ))
        # 復元の一時ファイルも公開前だけここへ来る。原本・公開先・共有sidecarは削除しない。
        if self.stage.exists():
            if self.stage.resolve() != self.stage:
                raise ValueError('Cloud transfer staging directory changed.')
            await self.io(lambda: shutil.rmtree(self.stage))
        await KonomiTVBS4KCloudTransferStore.advance(self.job.id, self.token, 'Cancelling')

    async def copyUpload(self, plan: KonomiTVBS4KCloudTransferPlan) -> None:
        """固定済みの対象集合をコピーし、再試行時も原本の変更を拒否する。

        Args:
            plan: SQLiteに保存済みの転送計画。
        Returns:
            None
        """
        for local, remote in zip(plan.local_files, plan.manifest.files, strict=True):
            path = self.localPath(Path(local.path))
            local.validateStat(path.stat(follow_symlinks=False))
            await self.upload(path, remote.relativePath(self.job.recording_uuid), cancellable=True)

    async def verifyUpload(self, plan: KonomiTVBS4KCloudTransferPlan) -> None:
        """全ファイルの復号全量が、準備時点の内容と一致することを確認する。

        Args:
            plan: SQLiteに保存済みの転送計画。
        Returns:
            None
        """
        for remote in plan.manifest.files:
            await self.verify(remote.relativePath(self.job.recording_uuid), remote.sha256, cancellable=True)

    async def publishUpload(self, plan: KonomiTVBS4KCloudTransferPlan) -> Path:
        """完成目録を最後に公開し、mountから本体を実際に読めることを確認する。

        Args:
            plan: 検証済みの転送計画。呼出し前にPublishingを永続化する。
        Returns:
            公開先の録画パス。まだローカル原本は削除しない。
        """
        payload = plan.manifest.model_dump_json().encode('utf-8')
        # 変更記録だけが見えても完成録画にはしない。初期目録の公開を常に最後にする。
        await self.io(lambda: KonomiTVBS4KCloudRemote.uploadGeneratedBytes(
            self.job.owner_id, self.job.connection_id, self.job.folder, payload,
            f'changes/{self.job.recording_uuid}/{plan.manifest.change_uuid}.json',
            owner_is_active=self.ownerExists, timeout=24 * 3600,
        ))
        key = f'recordings/{self.job.recording_uuid}/manifest.json'
        await self.io(lambda: KonomiTVBS4KCloudRemote.uploadGeneratedBytes(
            self.job.owner_id, self.job.connection_id, self.job.folder, payload, key,
            owner_is_active=self.ownerExists, timeout=24 * 3600,
        ))
        await self.verify(key, hashlib.sha256(payload).hexdigest(), cancellable=False)
        source = next(item for item in plan.manifest.files if item.kind == 'Recording')
        path = await self.mount() / source.relativePath(self.job.recording_uuid)
        def ReadPublishedSource() -> None:
            with path.open('rb') as data:
                if os.fstat(data.fileno()).st_size != source.size or not data.read(1):
                    raise ValueError('Published cloud recording cannot be read.')
        await self.io(ReadPublishedSource)
        return path

    async def runUploadUntilPublished(self) -> Path | None:
        """アップロードの公開前までを再開し、取消要求があれば清掃へ切り替える。

        Args:
            None
        Returns:
            公開・読取り確認したパス。取消完了ならNone。
        """
        if self.job.direction != 'ToCloud':
            raise ValueError('Upload worker received a different transfer direction.')
        try:
            await self.job.refresh_from_db()
            if self.job.cancel_requested:
                await self.cancel()
                return None
            if self.job.phase == 'Preparing':
                plan = await self.prepareUpload()
                await KonomiTVBS4KCloudTransferStore.advance(self.job.id, self.token, 'Preparing', manifest=plan)
                await self.job.refresh_from_db()
            plan = KonomiTVBS4KCloudTransferPlan.model_validate(self.job.manifest)
            if self.job.phase == 'Copying':
                await self.copyUpload(plan)
                await KonomiTVBS4KCloudTransferStore.advance(self.job.id, self.token, 'Copying')
                await self.job.refresh_from_db()
            if self.job.phase == 'Verifying':
                await self.verifyUpload(plan)
                await KonomiTVBS4KCloudTransferStore.advance(self.job.id, self.token, 'Verifying')
                await self.job.refresh_from_db()
            if self.job.phase == 'Publishing':
                return await self.publishUpload(plan)
            raise KonomiTVBS4KCloudTransferConflict('Upload is not in a publishable phase.')
        except (KonomiTVBS4KCloudTransferCancelled, KonomiTVBS4KCloudTransferConflict, InterruptedError):
            await self.job.refresh_from_db()
            if self.job.cancel_requested and self.job.worker_token == self.token:
                await self.cancel()
                return None
            raise

    async def cleanUpload(self, plan: KonomiTVBS4KCloudTransferPlan) -> None:
        """専有している元ファイルだけを隔離して再検証し、公開後に回収する。

        Args:
            plan: 公開・全量検証済みの計画。
        Returns:
            None。変更されたファイルや共有sidecarは削除しない。
        """
        self.prepareStage()
        for index, (local, remote) in enumerate(zip(plan.local_files, plan.manifest.files, strict=True)):
            if not local.remove_source:
                continue
            source = self.localPath(Path(local.path))
            quarantine = self.stage / f'cleanup-{index}.part'
            marker = self.stage / f'cleanup-{index}.committed'
            def Clean() -> None:
                # rename後に置かれた同名の新録画を再試行で消さない。清掃意思は隔離後にfsyncする。
                if marker.is_file() and not quarantine.exists():
                    return
                if not quarantine.exists():
                    if not source.exists():
                        return
                    local.validateStat(source.stat(follow_symlinks=False))
                    os.rename(source, quarantine)
                isolated, digest = KonomiTVBS4KCloudLocalFile.capture(quarantine, remove_source=False)
                # rename自体がctimeを変えるため、隔離後はinode・size・mtimeと全量ハッシュで照合する。
                if ((isolated.device, isolated.inode, isolated.size, isolated.modified_ns) !=
                    (local.device, local.inode, local.size, local.modified_ns) or digest != remote.sha256):
                    # 競合で別ファイルを隔離した場合は、上書きせず戻す。戻せなければ隔離先を保持して失敗する。
                    os.link(quarantine, source)
                    quarantine.unlink()
                    raise ValueError('Source changed before cloud move cleanup.')
                with marker.open('wb') as output:
                    output.write(str(self.job.id).encode('ascii'))
                    output.flush()
                    os.fsync(output.fileno())
                for directory in (source.parent, self.stage):
                    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                quarantine.unlink()
            await self.io(lambda: KonomiTVBS4KCloudRemote.guardLocalCleanup(
                self.job.owner_id, self.job.connection_id, self.job.folder, Clean, owner_is_active=self.ownerExists,
            ))

    async def prepareDownload(self) -> KonomiTVBS4KCloudTransferPlan:
        """共有目録を読み、上書きしない復元先と対象集合を固定する。

        Args:
            None
        Returns:
            公開目録と、復元先を含むローカル専用計画。
        """
        from app.utils.KonomiTVBS4KCloudCatalog import KonomiTVBS4KCloudCatalog
        manifest, _ = await KonomiTVBS4KCloudCatalog.readManifest(
            self.job.owner_id, self.job.connection_id, self.job.folder, self.job.recording_uuid,
        )
        local_files: list[KonomiTVBS4KCloudLocalFile] = []
        parent = self.localPath(Path(self.job.local_path)).parent
        recording = next(item for item in manifest.files if item.kind == 'Recording')
        if parent / recording.name != Path(self.job.local_path):
            raise ValueError('Restore target does not match the cloud manifest.')
        for item in manifest.files:
            target = (THUMBNAILS_DIR if item.kind == 'Thumbnail' else parent) / item.name
            if target.exists() and item.kind != 'Thumbnail':
                raise FileExistsError('Restore target already exists.')
            local_files.append(KonomiTVBS4KCloudLocalFile(path=str(target), device=0, inode=0,
                size=item.size, modified_ns=item.modified_ns, changed_ns=0, remove_source=False))
        return KonomiTVBS4KCloudTransferPlan(manifest=manifest, local_files=local_files)

    async def copyDownload(self, plan: KonomiTVBS4KCloudTransferPlan) -> None:
        """復号データをジョブ専用の未公開ファイルへ読み出す。

        Args:
            plan: SQLiteに保存済みの復元計画。
        Returns:
            None。取消時は一時ファイルを残して取消清掃へ渡す。
        """
        self.prepareStage()
        root = await self.mount()
        for index, remote in enumerate(plan.manifest.files):
            source = root / remote.relativePath(self.job.recording_uuid)
            temporary = self.stage / f'restore-{index}.part'
            def Copy() -> None:
                deadline = time.monotonic() + 24 * 3600
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with source.open('rb') as input_file, os.fdopen(fd, 'wb') as output:
                    while chunk := input_file.read(1024 * 1024):
                        if self.stop_requested.is_set():
                            raise KonomiTVBS4KCloudTransferCancelled()
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Cloud download timed out.')
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                os.utime(temporary, ns=(remote.modified_ns, remote.modified_ns))
            await self.io(Copy)

    async def verifyDownload(self, plan: KonomiTVBS4KCloudTransferPlan) -> None:
        """復元した全量を共有目録のサイズ・SHA-256と照合する。

        Args:
            plan: SQLiteに保存済みの復元計画。
        Returns:
            None。不一致なら公開・クラウド原本清掃へ進まない。
        """
        for index, remote in enumerate(plan.manifest.files):
            local, digest = await self.io(lambda index=index: KonomiTVBS4KCloudLocalFile.capture(
                self.stage / f'restore-{index}.part', remove_source=False,
                cancellation_requested=self.stop_requested.is_set,
            ))
            if local.size != remote.size or digest != remote.sha256:
                raise ValueError('Restored cloud recording verification failed.')

    async def publishDownload(self, plan: KonomiTVBS4KCloudTransferPlan) -> Path:
        """検証済み一時ファイルを既存ファイルへ上書きせず公開する。

        Args:
            plan: 検証済みの復元計画。呼出し前にPublishingを永続化する。
        Returns:
            公開された録画本体のパス。
        """
        for index, (local, remote) in enumerate(zip(plan.local_files, plan.manifest.files, strict=True)):
            staged = self.stage / f'restore-{index}.part'
            target = Path(local.path)
            if remote.kind != 'Thumbnail':
                self.localPath(target)
            elif target.parent != THUMBNAILS_DIR:
                raise ValueError('Invalid thumbnail restore path.')
            def Publish() -> None:
                publish_source = staged
                # 録画ディスクとアプリのサムネイル領域は別filesystemになり得る。リンク元を公開先に置く。
                if remote.kind == 'Thumbnail':
                    publish_source = target.parent / f'.konomitv-bs4k-restore-{self.job.id}-{index}.part'
                    incomplete = target.parent / f'.konomitv-bs4k-restore-{self.job.id}-{index}.copying'
                    # 前回のcopy中断物は公開候補へ流用せず、検証済みstageから毎回再構築する。
                    incomplete.unlink(missing_ok=True)
                    try:
                        with staged.open('rb') as source, incomplete.open('xb') as output:
                            shutil.copyfileobj(source, output, 1024 * 1024)
                            output.flush()
                            os.fsync(output.fileno())
                        copied, digest = KonomiTVBS4KCloudLocalFile.capture(incomplete, remove_source=False)
                        if copied.size != remote.size or digest != remote.sha256:
                            raise ValueError('Staged thumbnail does not match the cloud recording.')
                        # 完成・検証済みファイルだけを、最終linkに使うジョブ専用候補として公開する。
                        os.replace(incomplete, publish_source)
                    finally:
                        incomplete.unlink(missing_ok=True)
                # 同じinodeなら、このジョブが前回公開したもの。別ファイルは内容が同じでも上書きしない。
                try:
                    os.link(publish_source, target)
                except FileExistsError:
                    if not os.path.samefile(publish_source, target):
                        if remote.kind != 'Thumbnail':
                            raise
                        existing, digest = KonomiTVBS4KCloudLocalFile.capture(target, remove_source=False)
                        if existing.size != remote.size or digest != remote.sha256:
                            raise ValueError('Existing shared thumbnail differs from the cloud recording.') from None
                if remote.kind == 'Thumbnail':
                    publish_source.unlink()
                fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            await self.io(Publish)
        return Path(self.job.local_path)

    async def cleanDownload(self) -> None:
        """ローカル公開後に削除マーカーを全量検証し、クラウド原本を回収する。

        Args:
            None
        Returns:
            None。マーカー公開や清掃の失敗ではCleaningを維持する。
        """
        payload = KonomiTVBS4KCloudDeletion(recording_uuid=self.job.recording_uuid, deletion_uuid=self.job.id).model_dump_json().encode()
        key = f'deleted/{self.job.recording_uuid}/{self.job.id}.json'
        await self.io(lambda: KonomiTVBS4KCloudRemote.uploadGeneratedBytes(
            self.job.owner_id, self.job.connection_id, self.job.folder, payload, key,
            owner_is_active=self.ownerExists, timeout=24 * 3600,
        ))
        await self.verify(key, hashlib.sha256(payload).hexdigest(), cancellable=False)
        await self.io(lambda: KonomiTVBS4KCloudRemote.removeDeletedRecording(
            self.job.owner_id, self.job.connection_id, self.job.folder, self.job.recording_uuid, self.job.id,
            owner_is_active=self.ownerExists,
        ))

    async def run(self) -> None:
        """永続化した鍵identityの範囲でジョブ全体を実行する。

        Args:
            None
        Returns:
            None。鍵不明や差替え時は原本を残して失敗する。
        """
        with KonomiTVBS4KCloudRemote.keyIdentity(self.job.key_identity):
            await self._run()

    async def _run(self) -> None:
        """claim済みのジョブを再開し、公開・元清掃まで完了するか失敗を呼出し元へ返す。

        Args:
            None
        Returns:
            None。失敗を握りつぶさず、スケジューラーが同じトークンで永続化する。
        """
        try:
            await self.job.refresh_from_db()
            if self.job.cancel_requested:
                await self.cancel()
                return
            if self.job.phase != 'Cleaning':
                if self.job.direction == 'ToCloud':
                    published = await self.runUploadUntilPublished()
                    if published is None:
                        return
                else:
                    if self.job.phase == 'Preparing':
                        plan = await self.prepareDownload()
                        await KonomiTVBS4KCloudTransferStore.advance(self.job.id, self.token, 'Preparing', manifest=plan)
                        await self.job.refresh_from_db()
                    plan = KonomiTVBS4KCloudTransferPlan.model_validate(self.job.manifest)
                    if self.job.phase == 'Copying':
                        await self.copyDownload(plan)
                        await KonomiTVBS4KCloudTransferStore.advance(self.job.id, self.token, 'Copying')
                        await self.job.refresh_from_db()
                    if self.job.phase == 'Verifying':
                        await self.verifyDownload(plan)
                        await KonomiTVBS4KCloudTransferStore.advance(self.job.id, self.token, 'Verifying')
                        await self.job.refresh_from_db()
                    if self.job.phase != 'Publishing':
                        raise KonomiTVBS4KCloudTransferConflict('Restore is not in a publishable phase.')
                    published = await self.publishDownload(plan)
                # 所在とパスを同じtransactionで切り替えた後にだけ元ファイルの清掃へ進む。
                await KonomiTVBS4KCloudTransferStore.advance(self.job.id, self.token, 'Publishing', published_path=str(published))
                await self.job.refresh_from_db()
            plan = KonomiTVBS4KCloudTransferPlan.model_validate(self.job.manifest)
            if self.job.direction == 'ToCloud':
                await self.cleanUpload(plan)
            else:
                # 公開後の中断中に復元先が書き換えられていても、クラウド原本を消さない。
                for local, remote in zip(plan.local_files, plan.manifest.files, strict=True):
                    target = Path(local.path)
                    if remote.kind != 'Thumbnail':
                        target = self.localPath(target)
                    elif target.parent != THUMBNAILS_DIR or target.resolve() != target:
                        raise ValueError('Thumbnail restore target changed before cleanup.')
                    current, digest = await self.io(lambda target=target: KonomiTVBS4KCloudLocalFile.capture(target, remove_source=False))
                    if current.size != remote.size or digest != remote.sha256:
                        raise ValueError('Restore target changed before cloud source cleanup.')
                await self.cleanDownload()
            await KonomiTVBS4KCloudTransferStore.advance(self.job.id, self.token, 'Cleaning')
            try:
                await self.cleanupStage()
            except (OSError, ValueError):
                # 移動結果はcommit済み。一時領域の回収失敗で移動を失敗へ巻き戻さず、次回起動で回収する。
                logging.warning(f'[KonomiTVBS4KCloudTransferWorker] Completed staging cleanup failed. Job: {self.job.id}')
        except (KonomiTVBS4KCloudTransferCancelled, KonomiTVBS4KCloudTransferConflict, InterruptedError):
            await self.job.refresh_from_db()
            if self.job.cancel_requested and self.job.worker_token == self.token:
                await self.cancel()
                return
            raise
