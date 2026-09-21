import asyncio
import hashlib
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import ClassVar
from uuid import UUID, uuid4

from tortoise import timezone
from tortoise.transactions import in_transaction

from app import logging, schemas
from app.constants import DATA_DIR, THUMBNAILS_DIR
from app.models.AnalysisTask import AnalysisTaskExecution
from app.models.Channel import Channel
from app.models.KonomiTVBS4KCloudCatalogSync import (
    KonomiTVBS4KCloudCatalogSync,
    KonomiTVBS4KCloudDeletedRecording,
    KonomiTVBS4KCloudPublication,
)
from app.models.KonomiTVBS4KCloudDestination import KonomiTVBS4KCloudDestination
from app.models.KonomiTVBS4KCloudTransfer import (
    KonomiTVBS4KCloudRecording,
    KonomiTVBS4KCloudTransfer,
)
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedVideo import RecordedVideo
from app.models.User import User
from app.utils.KonomiTVBS4KCloudManifest import (
    KonomiTVBS4KCloudDeletion,
    KonomiTVBS4KCloudLocalFile,
    KonomiTVBS4KCloudManifest,
    KonomiTVBS4KCloudManifestFile,
    KonomiTVBS4KCloudPublicationPlan,
)
from app.utils.KonomiTVBS4KCloudRemote import KonomiTVBS4KCloudRemote
from app.utils.KonomiTVBS4KCloudStorage import KonomiTVBS4KCloudStorage
from app.utils.KonomiTVBS4KCloudTransferManager import KonomiTVBS4KCloudTransferManager
from app.utils.TSInformation import TSInformation


class KonomiTVBS4KCloudCatalog:
    """暗号化目録の同期要求を永続化し、明示的な削除と不完全な一覧を区別して取り込む。"""

    _task: ClassVar[asyncio.Task[None] | None] = None
    _wake: ClassVar[asyncio.Event] = asyncio.Event()
    ANALYSIS_DIRECTORY = DATA_DIR / 'konomitv-bs4k-cloud-analysis'

    @classmethod
    async def artifactPath(cls, video_id: int, stored_path: str, suffix: str) -> Path | None:
        """共有目録の採用世代からsidecarの読取り先を解決する。

        Args:
            video_id: 録画ID。
            stored_path: 録画本体の既知パス。
            suffix: 呼出し元で固定したsidecar拡張子。
        Returns:
            ローカルの導出先または検証済み目録の参照先。目録にない付随物はNone。
        """
        source = await KonomiTVBS4KCloudTransferManager.resolvePath(video_id, stored_path)
        if not source.is_relative_to('/cloud-mounts'):
            return source.with_suffix(suffix)
        location = await KonomiTVBS4KCloudRecording.get(recorded_video_id=video_id, location='Cloud')
        manifest = KonomiTVBS4KCloudManifest.model_validate(location.manifest)
        name = source.with_suffix(suffix).name
        item = next((item for item in manifest.files if item.name == name and item.kind == 'Sidecar'), None)
        return None if item is None else source.parents[3] / item.relativePath(location.id)

    @classmethod
    async def requestPublication(cls, execution: AnalysisTaskExecution) -> None:
        """成功履歴と同じtransaction内で、完成結果の公開要求を一度だけ保存する。

        Args:
            execution: 成功した解析履歴。
        Returns:
            None。通常のローカル録画や削除済み録画には要求を作らない。
        """
        location = await KonomiTVBS4KCloudRecording.get_or_none(recorded_video_id=execution.recorded_video_id, location='Cloud')
        if location is None:
            return
        if location.key_identity is None:
            raise ValueError('Cloud publication requires a pinned key.')
        await KonomiTVBS4KCloudPublication.get_or_create(id=execution.id, defaults={
            'recorded_video_id': location.recorded_video_id, 'recording_uuid': location.id,
            'owner_id': location.owner_id, 'connection_id': location.connection_id,
            'folder': location.folder, 'key_identity': location.key_identity,
        })
        # 接続解除とクラウド録画削除は別操作なので、公開要求は保持しつつ、認証がない接続の同期状態は作らない。
        try:
            connection_exists = await asyncio.to_thread(
                KonomiTVBS4KCloudStorage.connectionExists, location.owner_id, location.connection_id,
            )
        except BlockingIOError:
            return
        if connection_exists is False:
            return
        state, _ = await KonomiTVBS4KCloudCatalogSync.get_or_create(owner_id=location.owner_id,
            connection_id=location.connection_id, folder=location.folder)
        await KonomiTVBS4KCloudCatalogSync.filter(id=state.id).exclude(status='Running').update(
            status='Pending', error_code=None, updated_at=timezone.now())
        cls._wake.set()

    @classmethod
    async def materializeAnalysis(cls, video_id: int) -> Path:
        """書込みを伴う解析だけに、検証済みの入力とsidecarをローカルへ用意する。

        Args:
            video_id: 録画ID。呼出し元が録画単位の解析lockを保持する。
        Returns:
            精密mtimeを復元したローカル入力。DBの所在やfile_pathは変更しない。
        """
        location = await KonomiTVBS4KCloudRecording.get(recorded_video_id=video_id, location='Cloud')
        with KonomiTVBS4KCloudRemote.keyIdentity(location.key_identity):
            manifest, root = await cls.readManifest(location.owner_id, location.connection_id, location.folder, location.id)
        directory = cls.ANALYSIS_DIRECTORY / str(location.id)
        if directory.resolve() != directory:
            raise ValueError('Cloud analysis directory must not be redirected.')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        keep_local_results = await KonomiTVBS4KCloudPublication.filter(recorded_video_id=video_id).exclude(
            status__in=['Completed', 'Superseded']).exists()
        stopped = threading.Event()
        def Copy() -> Path:
            deadline = time.monotonic() + 24 * 3600
            for item in manifest.files:
                if item.kind == 'Thumbnail':
                    continue
                destination = directory / item.name
                if destination.resolve() != destination:
                    raise ValueError('Cloud analysis input must not be redirected.')
                if item.kind == 'Sidecar' and keep_local_results and destination.is_file():
                    # 公開待ちのローカル結果を、古い共有sidecarで上書きしない。
                    continue
                if destination.is_file() and item.kind == 'Recording':
                    local, digest = KonomiTVBS4KCloudLocalFile.capture(destination, remove_source=False,
                                                                 cancellation_requested=stopped.is_set)
                    if local.size == item.size and digest == item.sha256:
                        os.utime(destination, ns=(item.modified_ns, item.modified_ns))
                        continue
                if shutil.disk_usage(directory).free < item.size + 64 * 1024 * 1024:
                    raise OSError('Not enough local space for cloud analysis.')
                temporary = directory / f'.{uuid4()}.part'
                try:
                    size = 0
                    digest = hashlib.sha256()
                    with (root / item.relativePath(location.id)).open('rb') as source, temporary.open('xb') as output:
                        while chunk := source.read(1024 * 1024):
                            if stopped.is_set():
                                raise InterruptedError('Cloud analysis preparation was interrupted.')
                            if time.monotonic() >= deadline:
                                raise TimeoutError('Cloud analysis preparation timed out.')
                            size += len(chunk)
                            if size > item.size:
                                raise ValueError('Cloud analysis input exceeds its manifest size.')
                            digest.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if size != item.size or digest.hexdigest() != item.sha256:
                        raise ValueError('Cloud analysis input verification failed.')
                    os.utime(temporary, ns=(item.modified_ns, item.modified_ns))
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            return directory / next(item.name for item in manifest.files if item.kind == 'Recording')
        task = asyncio.create_task(asyncio.to_thread(Copy))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stopped.set()
            try:
                await asyncio.shield(task)
            except InterruptedError:
                pass
            raise

    @classmethod
    async def publishResults(cls, request: KonomiTVBS4KCloudPublication) -> bool:
        """固定した新世代へ解析結果を保存し、最後の変更記録で公開する。

        Args:
            request: 解析完了と同時に永続化された要求。
        Returns:
            終端状態になればTrue。解析・移動がまだ稼働中ならFalse。
        """
        import anyio

        from app.metadata.CMAnalysisOrchestrator import CMAnalysisOrchestrator
        from app.metadata.KonomiTVBS4KChapterFile import GetKonomiTVBS4KChapterPath
        from app.metadata.RecordedScanTask import RecordedScanTask

        if request.status in ('Publishing', 'Failed') and request.plan is not None:
            # 前プロセスや応答喪失後の旧RCを止め、同じ世代への再送・清掃と並走させない。
            await asyncio.to_thread(KonomiTVBS4KCloudRemote.stopConnection, request.owner_id, request.connection_id)
        location = await KonomiTVBS4KCloudRecording.get_or_none(id=request.recording_uuid, location='Cloud')
        if location is None:
            request.status = 'Superseded'
            await request.save(update_fields=['status', 'updated_at'])
            return True
        if await KonomiTVBS4KCloudDeletedRecording.filter(id=request.recording_uuid).exists():
            # 一度確認したマーカーは保持される。遅延公開が再作成した同一UUIDだけを再度回収する。
            with KonomiTVBS4KCloudRemote.keyIdentity(request.key_identity):
                purged = await cls.purgeDeletedRecording(
                    request.owner_id, request.connection_id, request.folder, request.recording_uuid,
                )
            if not purged:
                raise ValueError('Cloud deletion marker is unavailable for cleanup.')
            request.status = 'Superseded'
            await request.save(update_fields=['status', 'updated_at'])
            return True
        if (location.recorded_video_id != request.recorded_video_id or location.key_identity != request.key_identity or
            location.owner_id != request.owner_id or location.connection_id != request.connection_id or location.folder != request.folder):
            raise ValueError('Cloud publication ownership changed.')
        if (await AnalysisTaskExecution.filter(recorded_video_id=request.recorded_video_id, status__in=['Queued', 'Running']).exists() or
            await KonomiTVBS4KCloudTransfer.filter(active_video_id=request.recorded_video_id).exists()):
            return False
        video = await RecordedVideo.get(id=request.recorded_video_id)
        scanner = RecordedScanTask()
        lock_path = await scanner.resolveRecordedPath(anyio.Path(video.file_path))
        async with scanner.fileLock(lock_path), CMAnalysisOrchestrator.recordingLock(video.id):
            if await KonomiTVBS4KCloudTransfer.filter(active_video_id=video.id).exists():
                return False
            stage = KonomiTVBS4KCloudRemote.GENERATED_SOURCE_DIRECTORY / f'publication-{request.id}'
            if stage.resolve() != stage:
                raise ValueError('Cloud publication stage escaped its directory.')
            with KonomiTVBS4KCloudRemote.keyIdentity(request.key_identity):
                # ローカルDBがまだ取り込んでいない他インスタンスの削除も、成果物を送る前に反映する。
                if await cls.purgeDeletedRecording(
                    request.owner_id, request.connection_id, request.folder, request.recording_uuid,
                ):
                    request.status = 'Superseded'
                    await request.save(update_fields=['status', 'updated_at'])
                    return True
                if request.plan is None:
                    previous, _ = await cls.readManifest(request.owner_id, request.connection_id, request.folder, request.recording_uuid)
                    program = await RecordedProgram.get(id=video.recorded_program_id).select_related('recorded_video', 'channel')
                    if program.recorded_video.status != 'Recorded' or program.recorded_video.playback_index_status == 'Analyzing':
                        return False
                    portable = schemas.RecordedProgram.model_validate(program, from_attributes=True)
                    original_snapshot = portable.model_dump(mode='python')
                    portable.id = 0
                    portable.recorded_video.id = 0
                    portable.recorded_video.file_path = previous.program.recorded_video.file_path
                    portable.recorded_video.storage_location = 'Local'
                    portable.series_id = None
                    portable.series_broadcast_period_id = None
                    portable.series_episode = None
                    # ローカルのチャンネル表が未登録でも、共有済みの番組情報を欠落へ巻き戻さない。
                    if portable.channel is None:
                        portable.channel = previous.program.channel
                    change_uuid = uuid4()
                    files = [item for item in previous.files if item.kind != 'Thumbnail' or
                             portable.recorded_video.file_hash == previous.program.recorded_video.file_hash]
                    local_files: list[KonomiTVBS4KCloudLocalFile] = []
                    stage.mkdir(mode=0o700, parents=True, exist_ok=True)
                    source_name = portable.recorded_video.file_path
                    candidates = [THUMBNAILS_DIR / f'{portable.recorded_video.file_hash}{suffix}'
                                  for suffix in ('.webp', '.jpg', '_tile.webp', '_tile.jpg')]
                    candidates.append(GetKonomiTVBS4KChapterPath(cls.ANALYSIS_DIRECTORY / str(location.id) / source_name))
                    for source in candidates:
                        if not source.is_file():
                            continue
                        if source.is_symlink():
                            raise ValueError('Cloud publication source must not be a symlink.')
                        staged = stage / source.name
                        original_file, original_digest = await asyncio.to_thread(KonomiTVBS4KCloudLocalFile.capture, source, remove_source=False)
                        await asyncio.to_thread(shutil.copyfile, source, staged)
                        local, digest = await asyncio.to_thread(KonomiTVBS4KCloudLocalFile.capture, staged, remove_source=False)
                        original_file.validateStat(source.stat(follow_symlinks=False))
                        if digest != original_digest:
                            raise ValueError('Analysis artifact changed while staging its publication.')
                        previous_file = next((item for item in files if item.name == source.name), None)
                        if previous_file is not None and previous_file.sha256 == digest:
                            staged.unlink()
                            continue
                        item = KonomiTVBS4KCloudManifestFile(name=source.name,
                            kind='Thumbnail' if source.parent == THUMBNAILS_DIR else 'Sidecar', size=local.size,
                            sha256=digest, modified_ns=local.modified_ns, generation_uuid=change_uuid)
                        files = [entry for entry in files if entry.name != item.name]
                        files.append(item)
                        local_files.append(local)
                    manifest = KonomiTVBS4KCloudManifest(recording_uuid=location.id, generation=previous.generation + 1,
                        change_uuid=change_uuid, files=files, program=portable)
                    plan = KonomiTVBS4KCloudPublicationPlan(manifest=manifest, local_files=local_files)
                    request.plan = plan.model_dump(mode='json')
                    request.status = 'Publishing'
                    async with in_transaction() as db:
                        if await AnalysisTaskExecution.filter(recorded_video_id=video.id, status__in=['Queued', 'Running']).using_db(db).exists():
                            return False
                        current_program = await RecordedProgram.filter(id=program.id).using_db(db).select_related('recorded_video', 'channel').get()
                        if schemas.RecordedProgram.model_validate(current_program, from_attributes=True).model_dump(mode='python') != original_snapshot:
                            return False
                        await request.save(using_db=db, update_fields=['plan', 'status', 'updated_at'])
                else:
                    plan = KonomiTVBS4KCloudPublicationPlan.model_validate(request.plan)
                if plan.manifest.recording_uuid != request.recording_uuid:
                    raise ValueError('Cloud publication plan identity mismatch.')
                loop = asyncio.get_running_loop()
                def OwnerExists() -> bool:
                    return cls.ownerExists(request.owner_id, loop)
                for local in plan.local_files:
                    source = Path(local.path)
                    if source.resolve() != source or source.parent != stage:
                        raise ValueError('Cloud publication source escaped its stage.')
                    local.validateStat(source.stat(follow_symlinks=False))
                    remote = next(item for item in plan.manifest.files if item.name == source.name)
                    key = remote.relativePath(request.recording_uuid)
                    await asyncio.to_thread(KonomiTVBS4KCloudRemote.uploadFile,
                        request.owner_id, request.connection_id, request.folder, source, key,
                        recorded_folders=[], owner_is_active=OwnerExists, timeout=3600)
                    await asyncio.to_thread(KonomiTVBS4KCloudRemote.verifyFile,
                        request.owner_id, request.connection_id, request.folder, key, remote.sha256,
                        owner_is_active=OwnerExists, timeout=3600)
                if await cls.purgeDeletedRecording(
                    request.owner_id, request.connection_id, request.folder, request.recording_uuid,
                ):
                    request.status = 'Superseded'
                    await request.save(update_fields=['status', 'updated_at'])
                    return True
                payload = plan.manifest.model_dump_json().encode('utf-8')
                key = f'changes/{request.recording_uuid}/{plan.manifest.change_uuid}.json'
                await asyncio.to_thread(KonomiTVBS4KCloudRemote.uploadGeneratedBytes,
                    request.owner_id, request.connection_id, request.folder, payload, key,
                    owner_is_active=OwnerExists, timeout=3600)
                await asyncio.to_thread(KonomiTVBS4KCloudRemote.verifyFile,
                    request.owner_id, request.connection_id, request.folder, key, hashlib.sha256(payload).hexdigest(),
                    owner_is_active=OwnerExists, timeout=3600)
                # 成果物確認後からchange公開までに成立した削除でも、今作成した残骸を同じ実行で回収する。
                if await cls.purgeDeletedRecording(
                    request.owner_id, request.connection_id, request.folder, request.recording_uuid,
                ):
                    request.status = 'Superseded'
                    await request.save(update_fields=['status', 'updated_at'])
                    return True
                # 応答喪失後も同じchange UUIDとバイト列を再送する。完了を保存してから一時領域を回収する。
                async with in_transaction() as db:
                    await KonomiTVBS4KCloudRecording.filter(id=location.id, location='Cloud').using_db(db).update(
                        manifest=plan.manifest.model_dump(mode='json'), updated_at=timezone.now())
                    await KonomiTVBS4KCloudPublication.filter(id=request.id).using_db(db).update(
                        status='Completed', updated_at=timezone.now())
                try:
                    await asyncio.to_thread(shutil.rmtree, stage)
                    analysis_input = cls.ANALYSIS_DIRECTORY / str(location.id) / plan.manifest.program.recorded_video.file_path
                    if analysis_input.resolve() == analysis_input:
                        await asyncio.to_thread(analysis_input.unlink, missing_ok=True)
                except FileNotFoundError:
                    pass
                except OSError:
                    logging.warning(f'[KonomiTVBS4KCloudCatalog] Publication stage cleanup failed. Request: {request.id}')
                return True

    @staticmethod
    def ownerExists(owner_id: int, loop: asyncio.AbstractEventLoop) -> bool:
        """所有者lock内から管理者の現存を有限待機で確認する。

        Args:
            owner_id: 接続所有者。
            loop: DB接続を所有するイベントループ。
        Returns:
            現在も管理者であるか。
        """
        async def Check() -> bool:
            return await User.filter(id=owner_id, is_admin=True).exists()
        future = asyncio.run_coroutine_threadsafe(Check(), loop)
        try:
            return future.result(timeout=10)
        finally:
            if not future.done():
                future.cancel()

    @classmethod
    async def names(cls, owner_id: int, connection_id: UUID, folder: str, prefix: str, *, directories: bool) -> list[str]:
        """対象領域内の一階層を取得する。

        Args:
            owner_id: 所有者。
            connection_id: 接続。
            folder: 固定した領域。
            prefix: 論理ディレクトリ。
            directories: ディレクトリを取得するか。
        Returns:
            明示的な不在以外のエラーを空一覧にしない名前一覧。
        """
        loop = asyncio.get_running_loop()
        return await asyncio.to_thread(KonomiTVBS4KCloudRemote.listNames, owner_id, connection_id, folder, prefix,
                                       directories=directories, owner_is_active=lambda: cls.ownerExists(owner_id, loop))

    @classmethod
    async def mount(cls, owner_id: int, connection_id: UUID, folder: str) -> Path:
        """対象領域の読み取り起点を準備する。

        Args:
            owner_id: 所有者。
            connection_id: 接続。
            folder: 固定した領域。
        Returns:
            読取り専用mount。
        """
        loop = asyncio.get_running_loop()
        return await asyncio.to_thread(KonomiTVBS4KCloudRemote.ensureMount, owner_id, connection_id, folder,
                                       owner_is_active=lambda: cls.ownerExists(owner_id, loop))

    @classmethod
    async def purgeDeletedRecording(
        cls, owner_id: int, connection_id: UUID, folder: str, recording_uuid: UUID, *, root: Path | None = None,
    ) -> bool:
        """検証済み削除マーカーを根拠に、同じUUIDの遅延生成物を冪等に回収する。

        Args:
            owner_id: マーカーを確認する接続所有者。
            connection_id: マーカーを確認する接続。
            folder: 固定済み暗号化領域。
            recording_uuid: 削除対象の録画UUID。
            root: 同期処理が既に準備した読取りmount。未指定ならここで準備する。
        Returns:
            一件以上の有効な削除マーカーを確認した場合True。
        """
        root = root if root is not None else await cls.mount(owner_id, connection_id, folder)
        markers: list[KonomiTVBS4KCloudDeletion] = []
        marker_names = await cls.names(owner_id, connection_id, folder, f'deleted/{recording_uuid}', directories=False)
        for marker_name in marker_names:
            marker = KonomiTVBS4KCloudDeletion.model_validate_json(
                await cls.readJSON(root / f'deleted/{recording_uuid}/{marker_name}', limit=4096),
            )
            if marker.recording_uuid != recording_uuid or marker_name != f'{marker.deletion_uuid}.json':
                raise ValueError('Invalid cloud deletion marker identity.')
            markers.append(marker)
        if not markers:
            return False
        await KonomiTVBS4KCloudDeletedRecording.get_or_create(id=recording_uuid)
        loop = asyncio.get_running_loop()
        for marker in markers:
            await asyncio.to_thread(
                KonomiTVBS4KCloudRemote.removeDeletedRecording,
                owner_id,
                connection_id,
                folder,
                recording_uuid,
                marker.deletion_uuid,
                owner_is_active=lambda: cls.ownerExists(owner_id, loop),
            )
        return True

    @staticmethod
    async def readJSON(path: Path, *, limit: int = 16 * 1024 * 1024) -> bytes:
        """目録をサイズ制限付きで読み、未完成や過大なJSONを取り込まない。

        Args:
            path: 型付きUUIDと検証済みの名前から導出したパス。
            limit: 読取りサイズの上限。
        Returns:
            JSONのバイト列。通常APIへそのまま返さない。
        """
        def Read() -> bytes:
            with path.open('rb') as source:
                data = source.read(limit + 1)
            if len(data) > limit:
                raise ValueError('Cloud catalog object exceeds its size limit.')
            return data
        return await asyncio.to_thread(Read)

    @classmethod
    async def readManifest(
        cls, owner_id: int, connection_id: UUID, folder: str, recording_uuid: UUID,
    ) -> tuple[KonomiTVBS4KCloudManifest, Path]:
        """初期目録と追記型変更から決定的に最新世代を選び、削除・未完成を拒否する。

        Args:
            owner_id: 所有者。
            connection_id: 接続。
            folder: 領域。
            recording_uuid: 録画UUID。
        Returns:
            検証済みの完成目録と読取り起点。
        """
        root = await cls.mount(owner_id, connection_id, folder)
        if await KonomiTVBS4KCloudDeletedRecording.filter(id=recording_uuid).exists():
            raise ValueError('Cloud recording was deleted.')
        # 全体の一覧が不完全でも、対象UUIDの削除記録を個別に確認する。
        for name in await cls.names(owner_id, connection_id, folder, f'deleted/{recording_uuid}', directories=False):
            marker = KonomiTVBS4KCloudDeletion.model_validate_json(await cls.readJSON(root / f'deleted/{recording_uuid}/{name}', limit=4096))
            if marker.recording_uuid != recording_uuid or name != f'{marker.deletion_uuid}.json':
                raise ValueError('Invalid cloud deletion marker identity.')
            await KonomiTVBS4KCloudDeletedRecording.get_or_create(id=recording_uuid)
            raise ValueError('Cloud recording was deleted.')
        initial = KonomiTVBS4KCloudManifest.model_validate_json(await cls.readJSON(root / f'recordings/{recording_uuid}/manifest.json'))
        if initial.recording_uuid != recording_uuid:
            raise ValueError('Cloud manifest identity mismatch.')
        source = next(item for item in initial.files if item.kind == 'Recording')
        selected = initial
        for name in await cls.names(owner_id, connection_id, folder, f'changes/{recording_uuid}', directories=False):
            candidate = KonomiTVBS4KCloudManifest.model_validate_json(await cls.readJSON(root / f'changes/{recording_uuid}/{name}'))
            if (candidate.recording_uuid != recording_uuid or name != f'{candidate.change_uuid}.json' or
                next(item for item in candidate.files if item.kind == 'Recording') != source):
                raise ValueError('Cloud change record identity mismatch.')
            if (candidate.generation, candidate.change_uuid.int) > (selected.generation, selected.change_uuid.int):
                selected = candidate
        # 共有目録はローカルDB採番・任意のサムネイルパスを持ち込む入口にしない。
        video = selected.program.recorded_video
        if re.fullmatch(r'[0-9a-f]{32}', video.file_hash) is None or source.generation_uuid is not None:
            raise ValueError('Invalid cloud recording fingerprint.')
        allowed_thumbnails = {f'{video.file_hash}{suffix}' for suffix in ('.webp', '.jpg', '_tile.webp', '_tile.jpg')}
        allowed_sidecars = {str(Path(source.name).with_suffix(suffix)) for suffix in ('.psc', '.vtt', '.chapter.txt')}
        allowed_sidecars.add(source.name + '.konomitv-bs4k-chapters.yaml')
        for item in selected.files:
            if ((item.kind == 'Thumbnail' and item.name not in allowed_thumbnails) or
                (item.kind == 'Sidecar' and item.name not in allowed_sidecars)):
                raise ValueError('Unexpected cloud recording artifact.')
            file_stat = await asyncio.to_thread((root / item.relativePath(recording_uuid)).stat)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size != item.size:
                raise ValueError('Cloud recording is not completely published.')
        return selected, root

    @classmethod
    async def request(cls) -> None:
        """現在の保存先と既存クラウド所在の領域を同期対象として永続化する。

        Args:
            None
        Returns:
            None。実行中の対象・状態は上書きしない。
        """
        roots: set[tuple[int, UUID, str]] = set()
        for destination in await KonomiTVBS4KCloudDestination.all():
            roots.add((destination.owner_id, destination.connection_id, destination.folder))
        for recording in await KonomiTVBS4KCloudRecording.filter(location='Cloud'):
            roots.add((recording.owner_id, recording.connection_id, recording.folder))
        for publication in await KonomiTVBS4KCloudPublication.exclude(status__in=['Completed', 'Superseded']):
            roots.add((publication.owner_id, publication.connection_id, publication.folder))
        existing_states = await KonomiTVBS4KCloudCatalogSync.all()
        connections = {(owner_id, connection_id) for owner_id, connection_id, _ in roots}
        connections.update((state.owner_id, state.connection_id) for state in existing_states)
        active_roots: set[tuple[int, UUID, str]] = set()
        disconnected: set[tuple[int, UUID]] = set()
        available_connections: set[tuple[int, UUID]] = set()
        for owner_id, connection_id in connections:
            try:
                connection_exists = await asyncio.to_thread(
                    KonomiTVBS4KCloudStorage.connectionExists, owner_id, connection_id,
                )
            except BlockingIOError:
                # 接続の追加・更新・解除中は、その操作が確定するまで既存の同期要求を変更しない。
                continue
            if connection_exists:
                available_connections.add((owner_id, connection_id))
            else:
                disconnected.add((owner_id, connection_id))
        active_roots.update(
            root for root in roots if (root[0], root[1]) in available_connections
        )
        async with in_transaction() as db:
            # 接続解除はクラウド録画を削除しないが、実行不能な目録同期状態は一覧・schedulerの双方から除く。
            for owner_id, connection_id in disconnected:
                await KonomiTVBS4KCloudCatalogSync.filter(
                    owner_id=owner_id, connection_id=connection_id,
                ).using_db(db).delete()
            for owner_id, connection_id, folder in active_roots:
                state, _ = await KonomiTVBS4KCloudCatalogSync.get_or_create(owner_id=owner_id, connection_id=connection_id,
                                                                          folder=folder, using_db=db)
                await KonomiTVBS4KCloudCatalogSync.filter(id=state.id).exclude(status='Running').using_db(db).update(
                    status='Pending', error_code=None, updated_at=timezone.now(),
                )
        cls._wake.set()

    @classmethod
    async def start(cls) -> None:
        """中断した同期を戻し、起動時・定期・手動要求を同じworkerで処理する。

        Args:
            None
        Returns:
            None
        """
        if cls._task is not None:
            return
        await KonomiTVBS4KCloudCatalogSync.filter(status='Running').update(status='Pending')
        for terminal in await KonomiTVBS4KCloudPublication.filter(status__in=['Completed', 'Superseded']):
            stage = KonomiTVBS4KCloudRemote.GENERATED_SOURCE_DIRECTORY / f'publication-{terminal.id}'
            if stage.resolve() == stage and stage.is_dir():
                try:
                    await asyncio.to_thread(shutil.rmtree, stage)
                except OSError:
                    logging.warning(f'[KonomiTVBS4KCloudCatalog] Old publication stage cleanup failed. Request: {terminal.id}')
        # DELETE受付後のプロセス終了は、同じDELETEから再試行できる状態へ戻す。
        await RecordedVideo.filter(file_path__startswith='/cloud-mounts/', status='Deleting').update(status='DeleteFailed')
        await cls.request()
        cls._task = asyncio.create_task(cls._run(), name='KonomiTVBS4KCloudCatalog')

    @classmethod
    async def stop(cls) -> None:
        """DB接続を閉じる前に同期workerを停止する。

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

    @classmethod
    async def _run(cls) -> None:
        """一つの接続障害を他接続の消失判定へ拡大せず、固定した領域を順に同期する。

        Args:
            None
        Returns:
            None
        """
        next_refresh = timezone.now() + timedelta(minutes=5)
        while True:
            cls._wake.clear()
            try:
                if timezone.now() >= next_refresh:
                    await cls.request()
                    next_refresh = timezone.now() + timedelta(minutes=5)
                state = await KonomiTVBS4KCloudCatalogSync.filter(status='Pending').order_by('updated_at').first()
                if state is not None:
                    if await KonomiTVBS4KCloudCatalogSync.filter(id=state.id, status='Pending').update(status='Running'):
                        try:
                            completed = await cls.sync(state)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            # 目録の内容やproviderの例外本文をログへ出さない。失敗は永続化して画面から確認可能にする。
                            await KonomiTVBS4KCloudCatalogSync.filter(id=state.id).update(
                                status='Failed', error_code='CatalogSyncFailed', updated_at=timezone.now(),
                            )
                            logging.warning(f'[KonomiTVBS4KCloudCatalog] Synchronization failed. Request: {state.id}')
                        else:
                            await KonomiTVBS4KCloudCatalogSync.filter(id=state.id).update(
                                status='Completed' if completed else 'Pending', error_code=None, updated_at=timezone.now(),
                            )
                            if not completed:
                                try:
                                    await asyncio.wait_for(cls._wake.wait(), timeout=5)
                                except TimeoutError:
                                    pass
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.error('[KonomiTVBS4KCloudCatalog] Catalog scheduler recovery failed.')
            try:
                await asyncio.wait_for(cls._wake.wait(), timeout=5)
            except TimeoutError:
                pass

    @classmethod
    async def sync(cls, state: KonomiTVBS4KCloudCatalogSync) -> bool:
        """同期の全操作を開始時点の鍵へ固定する。

        Args:
            state: 永続化済みの対象領域。
        Returns:
            公開待ちも含めて完了した場合True。
        """
        identity = await asyncio.to_thread(KonomiTVBS4KCloudRemote.pinKey)
        if await KonomiTVBS4KCloudRecording.filter(location='Cloud', owner_id=state.owner_id,
            connection_id=state.connection_id, folder=state.folder).exclude(key_identity=identity).exists():
            raise ValueError('Cloud catalog requires its original crypt key.')
        with KonomiTVBS4KCloudRemote.keyIdentity(identity):
            return await cls._sync(state)

    @classmethod
    async def _sync(cls, state: KonomiTVBS4KCloudCatalogSync) -> bool:
        """削除を先に適用し、完成目録だけをローカルSQLiteへupsertする。

        Args:
            state: 受付時に固定した接続・領域。
        Returns:
            None。不完全な一覧の差分を削除要求にしない。
        """
        publications_complete = True
        for publication in await KonomiTVBS4KCloudPublication.filter(owner_id=state.owner_id,
            connection_id=state.connection_id, folder=state.folder).exclude(status__in=['Completed', 'Superseded']).order_by('id'):
            try:
                if not await cls.publishResults(publication):
                    publications_complete = False
            except Exception:
                # 公開の失敗は入力・世代・鍵を保持して再試行する。通常の目録取込でローカル成果を上書きしない。
                await KonomiTVBS4KCloudPublication.filter(id=publication.id).update(status='Failed', updated_at=timezone.now())
                raise
        root = await cls.mount(state.owner_id, state.connection_id, state.folder)
        # 過去に観測した削除は、不完全な新しい一覧に載らなくても適用し続ける。
        for known in await KonomiTVBS4KCloudRecording.filter(location='Cloud', owner_id=state.owner_id,
            connection_id=state.connection_id, folder=state.folder):
            if await KonomiTVBS4KCloudDeletedRecording.filter(id=known.id).exists():
                if not await cls.purgeDeletedRecording(
                    state.owner_id, state.connection_id, state.folder, known.id, root=root,
                ):
                    raise ValueError('Cloud deletion marker is unavailable for cleanup.')
                await cls.removeRegistration(known.id, state)
        for name in await cls.names(state.owner_id, state.connection_id, state.folder, 'deleted', directories=True):
            try:
                recording_uuid = UUID(name)
            except ValueError:
                continue
            if await cls.purgeDeletedRecording(
                state.owner_id, state.connection_id, state.folder, recording_uuid, root=root,
            ):
                await cls.removeRegistration(recording_uuid, state)
        for name in await cls.names(state.owner_id, state.connection_id, state.folder, 'recordings', directories=True):
            try:
                recording_uuid = UUID(name)
            except ValueError:
                continue
            if await KonomiTVBS4KCloudDeletedRecording.filter(id=recording_uuid).exists():
                if not await cls.purgeDeletedRecording(
                    state.owner_id, state.connection_id, state.folder, recording_uuid, root=root,
                ):
                    raise ValueError('Cloud deletion marker is unavailable for cleanup.')
                await cls.removeRegistration(recording_uuid, state)
                continue
            # 完成目録がまだない転送領域は未公開。次の同期で同じUUIDを再確認する。
            try:
                manifest, root = await cls.readManifest(state.owner_id, state.connection_id, state.folder, recording_uuid)
            except FileNotFoundError:
                continue
            await cls.importManifest(state, manifest, root)
        await KonomiTVBS4KCloudTransferManager.refresh()
        return publications_complete

    @staticmethod
    async def cacheThumbnails(manifest: KonomiTVBS4KCloudManifest, root: Path) -> None:
        """共有用サムネイルを全量検証してから、既存のローカル配信キャッシュへ公開する。

        Args:
            manifest: 名前・サイズ・参照先を検証済みの目録。
            root: 読取り起点。
        Returns:
            None。失敗時は既存キャッシュを上書きしない。
        """
        for item in manifest.files:
            if item.kind != 'Thumbnail':
                continue
            def Copy() -> None:
                temporary: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(dir=THUMBNAILS_DIR, prefix='.konomitv-bs4k-cloud-', delete=False) as output:
                        temporary = Path(output.name)
                        digest = hashlib.sha256()
                        size = 0
                        with (root / item.relativePath(manifest.recording_uuid)).open('rb') as source:
                            while chunk := source.read(1024 * 1024):
                                size += len(chunk)
                                if size > item.size:
                                    raise ValueError('Cloud thumbnail exceeds its manifest size.')
                                digest.update(chunk)
                                output.write(chunk)
                        if size != item.size or digest.hexdigest() != item.sha256:
                            raise ValueError('Cloud thumbnail verification failed.')
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, THUMBNAILS_DIR / item.name)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            await asyncio.to_thread(Copy)

    @staticmethod
    async def removeRegistration(recording_uuid: UUID, state: KonomiTVBS4KCloudCatalogSync) -> None:
        """削除マーカーを見たCloud所在だけを除去し、復元済みローカル録画を残す。

        Args:
            recording_uuid: 削除されたUUID。
            state: マーカーを確認した領域。
        Returns:
            None
        """
        async with in_transaction() as db:
            location = await KonomiTVBS4KCloudRecording.filter(id=recording_uuid, location='Cloud', owner_id=state.owner_id,
                connection_id=state.connection_id, folder=state.folder).using_db(db).first()
            if location is None or await KonomiTVBS4KCloudTransfer.filter(active_video_id=location.recorded_video_id).using_db(db).exists():
                return
            video = await RecordedVideo.filter(id=location.recorded_video_id).using_db(db).first()
            if video is not None and video.status in ('Deleting', 'DeleteFailed'):
                # 自サーバーの削除は、清掃完了まで再試行の正本を保持する。
                return
            await location.delete(using_db=db)
            if video is not None:
                await RecordedProgram.filter(id=video.recorded_program_id).using_db(db).delete()

    @classmethod
    async def deleteFiles(cls, video_id: int) -> None:
        """自サーバーで受付済みのクラウド削除を、マーカー先行で冪等に実行する。

        Args:
            video_id: Deletingへ遷移済みの録画ID。
        Returns:
            None。失敗時は呼出し元がDeleteFailedを保存し、登録を保持する。
        """
        location = await KonomiTVBS4KCloudRecording.get(recorded_video_id=video_id, location='Cloud')
        # 同じ録画のDELETE再送・再起動でも同じマーカーを使い、別領域を清掃しない。
        marker = KonomiTVBS4KCloudDeletion(recording_uuid=location.id, deletion_uuid=location.id)
        payload = marker.model_dump_json().encode('utf-8')
        key = f'deleted/{location.id}/{location.id}.json'
        loop = asyncio.get_running_loop()
        with KonomiTVBS4KCloudRemote.keyIdentity(location.key_identity):
            await asyncio.to_thread(KonomiTVBS4KCloudRemote.uploadGeneratedBytes,
                location.owner_id, location.connection_id, location.folder, payload, key,
                owner_is_active=lambda: cls.ownerExists(location.owner_id, loop), timeout=300)
            await asyncio.to_thread(KonomiTVBS4KCloudRemote.verifyFile,
                location.owner_id, location.connection_id, location.folder, key, hashlib.sha256(payload).hexdigest(),
                owner_is_active=lambda: cls.ownerExists(location.owner_id, loop), timeout=300)
            await KonomiTVBS4KCloudDeletedRecording.get_or_create(id=location.id)
            await asyncio.to_thread(KonomiTVBS4KCloudRemote.removeDeletedRecording,
                location.owner_id, location.connection_id, location.folder, location.id, location.id,
                owner_is_active=lambda: cls.ownerExists(location.owner_id, loop))
        analysis_directory = cls.ANALYSIS_DIRECTORY / str(location.id)
        if analysis_directory.resolve() != analysis_directory:
            raise ValueError('Cloud analysis cleanup directory changed.')
        if analysis_directory.is_dir():
            await asyncio.to_thread(shutil.rmtree, analysis_directory)

    @staticmethod
    async def importManifest(state: KonomiTVBS4KCloudCatalogSync, manifest: KonomiTVBS4KCloudManifest, root: Path) -> None:
        """DB連番や他環境の外部キーを採用せず、録画UUIDに対応する番組情報を取り込む。

        Args:
            state: 取得元の固定した領域。
            manifest: 参照先が揃っていると確認した目録。
            root: 読取り起点。
        Returns:
            None
        """
        program = manifest.program
        existing = await KonomiTVBS4KCloudRecording.get_or_none(id=manifest.recording_uuid)
        if await KonomiTVBS4KCloudPublication.filter(recording_uuid=manifest.recording_uuid).exclude(status__in=['Completed', 'Superseded']).exists():
            return
        if existing is not None:
            if await AnalysisTaskExecution.filter(recorded_video_id=existing.recorded_video_id, status__in=['Queued', 'Running']).exists():
                return
            if (existing.location != 'Cloud' or existing.owner_id != state.owner_id or
                existing.connection_id != state.connection_id or existing.folder != state.folder or
                existing.key_identity != KonomiTVBS4KCloudRemote.boundKeyIdentity() or
                await KonomiTVBS4KCloudTransfer.filter(active_video_id=existing.recorded_video_id).exists()):
                return
            if existing.manifest is not None:
                cached = KonomiTVBS4KCloudManifest.model_validate(existing.manifest)
                if (manifest.generation, manifest.change_uuid.int) <= (cached.generation, cached.change_uuid.int):
                    # 同じ世代の再同期で、ローカルで再生成した未公開サムネイルも上書きしない。
                    return
        await KonomiTVBS4KCloudCatalog.cacheThumbnails(manifest, root)
        # 公開されているORM記述からDB列だけを選び、API派生フィールドや相手側のIDを持ち込まない。
        program_fields = {field['name'] for field in RecordedProgram.describe()['data_fields']} - {
            'id', 'created_at', 'updated_at', 'channel_id', 'series_id', 'series_broadcast_period_id', 'series_episode_id',
        }
        video_fields = {field['name'] for field in RecordedVideo.describe()['data_fields']} - {'id', 'created_at', 'updated_at', 'recorded_program_id'}
        program_values = program.model_dump(mode='python', include=program_fields)
        video_values = program.recorded_video.model_dump(mode='python', include=video_fields)
        source = next(item for item in manifest.files if item.kind == 'Recording')
        video_values['file_path'] = str(root / source.relativePath(manifest.recording_uuid))
        video_values['status'] = 'Recorded'
        channel_id = None
        if program.channel is not None:
            channel = await Channel.filter(network_id=program.channel.network_id, service_id=program.channel.service_id).first()
            if channel is not None:
                channel_id = channel.id
        async with in_transaction() as db:
            if await KonomiTVBS4KCloudDeletedRecording.filter(id=manifest.recording_uuid).using_db(db).exists():
                return
            if channel_id is None and program.channel is not None:
                channel_info = program.channel
                if not (0 <= channel_info.network_id <= 65535 and 0 <= channel_info.service_id <= 65535):
                    raise ValueError('Invalid cloud channel identifier.')
                channel_number = await TSInformation.calculateChannelNumber(channel_info.type, channel_info.network_id,
                    channel_info.service_id, channel_info.remocon_id)
                channel, _ = await Channel.get_or_create(id=f'NID{channel_info.network_id}-SID{channel_info.service_id:03d}',
                    using_db=db, defaults={
                        'display_channel_id': channel_info.type.lower() + channel_number,
                        'network_id': channel_info.network_id, 'service_id': channel_info.service_id,
                        'transport_stream_id': channel_info.transport_stream_id, 'remocon_id': channel_info.remocon_id,
                        'channel_number': channel_number, 'type': channel_info.type, 'name': channel_info.name,
                        'is_subchannel': channel_info.is_subchannel, 'is_radiochannel': channel_info.is_radiochannel,
                        'is_watchable': False,
                    })
                channel_id = channel.id
            location = await KonomiTVBS4KCloudRecording.filter(id=manifest.recording_uuid).using_db(db).first()
            if location is not None:
                if (location.location != 'Cloud' or location.owner_id != state.owner_id or
                    location.connection_id != state.connection_id or location.folder != state.folder or
                    await KonomiTVBS4KCloudTransfer.filter(active_video_id=location.recorded_video_id).using_db(db).exists()):
                    return
                if location.manifest is not None:
                    cached = KonomiTVBS4KCloudManifest.model_validate(location.manifest)
                    # 同じ目録を繰り返し上書きし、ローカルで進行した解析状態を巻き戻さない。
                    if (manifest.generation, manifest.change_uuid.int) <= (cached.generation, cached.change_uuid.int):
                        return
                video = await RecordedVideo.filter(id=location.recorded_video_id).using_db(db).get()
                if channel_id is not None:
                    program_values['channel_id'] = channel_id
                await RecordedProgram.filter(id=video.recorded_program_id).using_db(db).update(**program_values)
                await RecordedVideo.filter(id=video.id).using_db(db).update(**video_values)
                location.manifest = manifest.model_dump(mode='json')
                await location.save(using_db=db)
            else:
                imported = await RecordedProgram.create(using_db=db, channel_id=channel_id, **program_values)
                video = await RecordedVideo.create(using_db=db, recorded_program_id=imported.id, **video_values)
                await KonomiTVBS4KCloudRecording.create(using_db=db, id=manifest.recording_uuid, recorded_video_id=video.id,
                    location='Cloud', owner_id=state.owner_id, connection_id=state.connection_id, folder=state.folder,
                    original_path=source.name, manifest=manifest.model_dump(mode='json'),
                    key_identity=KonomiTVBS4KCloudRemote.boundKeyIdentity())
