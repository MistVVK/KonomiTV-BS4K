import asyncio
from datetime import datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.config import Config
from app.metadata.CMAnalysisOrchestrator import CMAnalysisOrchestrator
from app.metadata.RecordedScanTask import RecordedScanTask
from app.models.KonomiTVBS4KCloudCatalogSync import KonomiTVBS4KCloudCatalogSync
from app.models.KonomiTVBS4KCloudTransfer import (
    KonomiTVBS4KCloudTransfer,
    KonomiTVBS4KCloudTransferDirection,
    KonomiTVBS4KCloudTransferPhase,
    KonomiTVBS4KCloudTransferStatus,
)
from app.models.RecordedProgram import RecordedProgram
from app.models.User import User
from app.routers.KonomiTVBS4KCloudStorageRouter import KonomiTVBS4KCloudOperation
from app.routers.UsersRouter import GetCurrentAdminUser
from app.utils.HostPath import DOCKER_HOST_ROOT, ToRuntimePath
from app.utils.KonomiTVBS4KCloudCatalog import KonomiTVBS4KCloudCatalog
from app.utils.KonomiTVBS4KCloudCryptKeys import (
    KonomiTVBS4KCryptKeyBackupNotConfirmedError,
    KonomiTVBS4KCryptKeySnapshotChangedError,
)
from app.utils.KonomiTVBS4KCloudRemote import KonomiTVBS4KCloudRemote
from app.utils.KonomiTVBS4KCloudStorage import KonomiTVBS4KCloudConnection
from app.utils.KonomiTVBS4KCloudTransferManager import KonomiTVBS4KCloudTransferManager
from app.utils.KonomiTVBS4KCloudTransferStore import (
    KonomiTVBS4KCloudTransferConflict,
    KonomiTVBS4KCloudTransferStore,
)


router = APIRouter(prefix='/api/konomitv-bs4k/cloud-storage/transfers', tags=['KonomiTVBS4K Cloud Transfers'])
Admin = Annotated[User, Depends(GetCurrentAdminUser)]


class KonomiTVBS4KCloudTransferInfo(BaseModel):
    """秘密・原本パス・内部目録を返さない移動状態。"""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    recorded_video_id: int
    direction: KonomiTVBS4KCloudTransferDirection
    status: KonomiTVBS4KCloudTransferStatus
    phase: KonomiTVBS4KCloudTransferPhase
    cancel_requested: bool
    attempt: int
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class KonomiTVBS4KCloudTransferRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    recorded_program_id: Annotated[int, Field(gt=0)]
    direction: KonomiTVBS4KCloudTransferDirection
    key_backup_confirmed: bool = False
    key_revision: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
    restore_folder: str | None = None


class KonomiTVBS4KCloudCatalogSyncInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    connection_id: UUID
    connection_name: str
    folder: str
    status: str
    error_code: str | None
    updated_at: datetime


async def GetKonomiTVBS4KCloudCatalogSyncStates(user: User) -> list[KonomiTVBS4KCloudCatalogSyncInfo]:
    """現存する接続だけの目録同期状態へ、秘密を含まない表示名を付ける。

    Args:
        user: 認証済み管理者。
    Returns:
        管理者自身が現在所有する接続の同期状態。
    """
    connections = await KonomiTVBS4KCloudOperation(user, 'List')
    if not isinstance(connections, list):
        raise HTTPException(500)
    connection_names: dict[UUID, str] = {}
    for connection in connections:
        if not isinstance(connection, KonomiTVBS4KCloudConnection):
            raise HTTPException(500)
        connection_names[UUID(connection.id)] = connection.name
    states = await KonomiTVBS4KCloudCatalogSync.filter(
        owner_id=user.id, connection_id__in=connection_names,
    ).order_by('created_at')
    return [KonomiTVBS4KCloudCatalogSyncInfo(
        id=state.id,
        connection_id=state.connection_id,
        connection_name=connection_names[state.connection_id],
        folder=state.folder,
        status=state.status,
        error_code=state.error_code,
        updated_at=state.updated_at,
    ) for state in states]


@router.get('/catalog-sync', response_model=list[KonomiTVBS4KCloudCatalogSyncInfo])
async def KonomiTVBS4KCloudCatalogSyncStatusAPI(user: Admin):
    """目録同期の永続状態を返す。

    Args:
        user: 管理者。
    Returns:
        領域ごとの同期状態。
    """
    return await GetKonomiTVBS4KCloudCatalogSyncStates(user)


@router.post('/catalog-sync', status_code=202, response_model=list[KonomiTVBS4KCloudCatalogSyncInfo])
async def KonomiTVBS4KCloudCatalogSyncRequestAPI(user: Admin):
    """目録同期を受付し、接続が終了しても要求を保持する。

    Args:
        user: 管理者。
    Returns:
        永続化済みの同期要求。同期完了ではない。
    """
    await KonomiTVBS4KCloudCatalog.request()
    return await GetKonomiTVBS4KCloudCatalogSyncStates(user)


@router.get('', response_model=list[KonomiTVBS4KCloudTransferInfo])
async def KonomiTVBS4KCloudTransfersAPI(user: Admin):
    """管理者に移動履歴と再開可能な状態を返す。

    Args:
        user: 認可済み管理者。
    Returns:
        最新100件の公開状態。
    """
    return await KonomiTVBS4KCloudTransfer.all().order_by('-created_at').limit(100)


@router.get('/restore-folders', response_model=list[str])
async def KonomiTVBS4KCloudRestoreFoldersAPI(user: Admin):
    """復元要求で選択できる、設定済み録画フォルダだけを返す。

    Args:
        user: 認可済み管理者。
    Returns:
        設定に記録されたフォルダ。ここでは作成や書込みを行わない。
    """
    return [str(folder) for folder in Config().video.recorded_folders]


@router.post('', response_model=KonomiTVBS4KCloudTransferInfo, status_code=202)
async def KonomiTVBS4KCloudTransferCreateAPI(body: KonomiTVBS4KCloudTransferRequest, user: Admin):
    """方向・保存先・要求UUIDを固定して受付し、HTTP接続外のworkerへ渡す。

    Args:
        body: 管理者が確認した移動要求。
        user: 認可済み管理者。
    Returns:
        永続化済みのジョブ。転送完了ではない。
    """
    program = await RecordedProgram.get_or_none(id=body.recorded_program_id).select_related('recorded_video')
    if program is None:
        raise HTTPException(404, 'Recording was not found.')
    existing = await KonomiTVBS4KCloudTransfer.get_or_none(id=body.request_id)
    # 応答喪失後の再送では、所在や現在の設定を参照して逆方向・別保存先へ解釈しない。
    if existing is not None:
        if (existing.requested_by != user.id or existing.recorded_video_id != program.recorded_video.id or
            existing.direction != body.direction or existing.key_revision != body.key_revision):
            raise HTTPException(409, 'Transfer request ID was reused with different inputs.')
        if body.direction == 'ToLocal' and (body.restore_folder is None or
            ToRuntimePath(Path(body.restore_folder)).resolve() != Path(existing.local_path).parent):
            raise HTTPException(409, 'Restore destination differs from the original request.')
        return existing
    if body.direction == 'ToCloud':
        if not body.key_backup_confirmed:
            raise HTTPException(422, 'Crypt key backup confirmation is required.')
        local_path = ToRuntimePath(Path(program.recorded_video.file_path)).resolve(strict=True)
    else:
        folders = {str(folder): ToRuntimePath(folder).resolve(strict=True) for folder in Config().video.recorded_folders}
        if body.restore_folder not in folders:
            raise HTTPException(422, 'Select a configured recording folder.')
        local_path = folders[body.restore_folder] / Path(program.recorded_video.file_path).name
    roots = [ToRuntimePath(folder).resolve(strict=True) for folder in Config().video.recorded_folders]
    if not any(root != DOCKER_HOST_ROOT and root.is_relative_to(DOCKER_HOST_ROOT)
               and local_path.is_relative_to(root) and local_path != root for root in roots):
        raise HTTPException(422, 'Transfer path is outside the recording folders.')
    scanner = RecordedScanTask()
    lock_path = await scanner.resolveRecordedPath(anyio.Path(program.recorded_video.file_path))
    try:
        async with scanner.fileLock(lock_path), CMAnalysisOrchestrator.recordingLock(program.recorded_video.id):
            # 同じ要求がロック待機中に受付済みになった場合も、鍵の再確認より先に元の結果へ収束する。
            existing = await KonomiTVBS4KCloudTransfer.get_or_none(id=body.request_id)
            if existing is not None:
                if (existing.requested_by != user.id or existing.recorded_video_id != program.recorded_video.id or
                    existing.direction != body.direction or existing.key_revision != body.key_revision or
                    existing.local_path != str(local_path)):
                    raise HTTPException(409, 'Transfer request ID was reused with different inputs.')
                return existing
            try:
                identity = await asyncio.to_thread(
                    KonomiTVBS4KCloudRemote.pinKey,
                    body.key_revision,
                    require_backup_confirmation=body.direction == 'ToCloud',
                )
            except KonomiTVBS4KCryptKeyBackupNotConfirmedError:
                raise HTTPException(422, 'Crypt key backup confirmation is required.') from None
            except KonomiTVBS4KCryptKeySnapshotChangedError:
                raise HTTPException(409, 'Crypt key confirmation changed.') from None
            except (OSError, ValueError):
                raise HTTPException(409, 'Crypt key confirmation changed or is unavailable.') from None
            job = await KonomiTVBS4KCloudTransferStore.enqueue(body.request_id, program.recorded_video.id, user.id,
                body.direction, local_path=str(local_path), key_identity=identity, key_revision=body.key_revision)
            await KonomiTVBS4KCloudTransferManager.refresh()
            return job
    except KonomiTVBS4KCloudTransferConflict as error:
        raise HTTPException(409, str(error)) from None


@router.post('/{job_id}/cancel', response_model=KonomiTVBS4KCloudTransferInfo, status_code=202)
async def KonomiTVBS4KCloudTransferCancelAPI(job_id: UUID, user: Admin):
    """公開前の取消を永続化し、停止・清掃は所有workerに実行させる。

    Args:
        job_id: 要求UUID。
        user: 認可済み管理者。
    Returns:
        取消受付後の状態。開始済みの取消ではまだ占有を解放しない。
    """
    if not await KonomiTVBS4KCloudTransfer.filter(id=job_id).exists():
        raise HTTPException(404, 'Transfer was not found.')
    try:
        await KonomiTVBS4KCloudTransferStore.cancel(job_id)
    except KonomiTVBS4KCloudTransferConflict as error:
        raise HTTPException(409, str(error)) from None
    await KonomiTVBS4KCloudTransferManager.refresh()
    return await KonomiTVBS4KCloudTransfer.get(id=job_id)


@router.post('/{job_id}/retry', response_model=KonomiTVBS4KCloudTransferInfo, status_code=202)
async def KonomiTVBS4KCloudTransferRetryAPI(job_id: UUID, user: Admin):
    """失敗した同じ段階を再開し、取消清掃を通常の転送へ戻さない。

    Args:
        job_id: 要求UUID。
        user: 認可済み管理者。
    Returns:
        再開受付後の状態。
    """
    if not await KonomiTVBS4KCloudTransfer.filter(id=job_id).exists():
        raise HTTPException(404, 'Transfer was not found.')
    try:
        await KonomiTVBS4KCloudTransferStore.retry(job_id)
    except KonomiTVBS4KCloudTransferConflict as error:
        raise HTTPException(409, str(error)) from None
    await KonomiTVBS4KCloudTransferManager.refresh()
    return await KonomiTVBS4KCloudTransfer.get(id=job_id)
