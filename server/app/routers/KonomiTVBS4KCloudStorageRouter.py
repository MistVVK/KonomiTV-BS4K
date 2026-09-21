import asyncio
import configparser
import json
import subprocess
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from tortoise.transactions import in_transaction

from app import logging
from app.models.KonomiTVBS4KCloudCatalogSync import KonomiTVBS4KCloudCatalogSync
from app.models.KonomiTVBS4KCloudConnectionCheck import KonomiTVBS4KCloudConnectionCheck
from app.models.KonomiTVBS4KCloudDestination import KonomiTVBS4KCloudDestination
from app.models.User import User
from app.routers.UsersRouter import GetCurrentAdminUser
from app.utils.KonomiTVBS4KCloudStorage import (
    KonomiTVBS4KCloudConnection,
    KonomiTVBS4KCloudImport,
    KonomiTVBS4KCloudStorage,
)


router = APIRouter(prefix='/api/konomitv-bs4k/cloud-storage', tags=['KonomiTVBS4K Cloud Storage'])
Admin = Annotated[User, Depends(GetCurrentAdminUser)]


class KonomiTVBS4KCloudDestinationSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    folder: Annotated[str, Field(min_length=1, max_length=1024)]
    is_upload_destination: bool

    @field_validator('folder')
    @classmethod
    def validateFolder(cls, value: str) -> str:
        """クラウド内の相対フォルダだけを受け付ける。

        Args:
            value: 管理者の指定したフォルダ。
        Returns:
            検証済みのフォルダ。
        """
        if (value != value.strip() or any(ord(char) < 32 for char in value) or
            any(char in value for char in ':\\') or any(part in ('', '.', '..') for part in value.split('/'))):
            raise ValueError('Invalid cloud folder.')
        return value


class KonomiTVBS4KCloudDestinationInfo(KonomiTVBS4KCloudDestinationSettings):
    connection_id: UUID


@router.get('/destinations', response_model=list[KonomiTVBS4KCloudDestinationInfo])
async def KonomiTVBS4KCloudDestinationsAPI(user: Admin):
    """管理者所有の保存先を返す。認証情報には触れない。"""
    return [KonomiTVBS4KCloudDestinationInfo(
        connection_id=item.connection_id, folder=item.folder, is_upload_destination=item.upload_slot == 1,
    ) for item in await KonomiTVBS4KCloudDestination.filter(owner_id=user.id)]


@router.put('/{connection_id}/destination', response_model=KonomiTVBS4KCloudDestinationInfo)
async def KonomiTVBS4KCloudDestinationUpdateAPI(
    connection_id: UUID, settings: KonomiTVBS4KCloudDestinationSettings, user: Admin,
):
    """接続の保存先と単一のアップロード先を同じtransactionで保存する。"""
    try:
        # 解除・ユーザー削除と同じ順でlockを取り、認証が消えた後に設定を作らない。
        with KonomiTVBS4KCloudStorage.ownerLock(user.id) as root:
            path = root / f'{connection_id}.conf'
            if not path.is_file() or path.is_symlink():
                raise HTTPException(404)
            async with in_transaction() as db:
                if not await User.filter(id=user.id, is_admin=True).using_db(db).exists():
                    raise HTTPException(403)
                current = await KonomiTVBS4KCloudDestination.filter(connection_id=connection_id).using_db(db).first()
                # 別の管理者が所有する接続設定を引き継がない。
                if current is not None and current.owner_id != user.id:
                    raise HTTPException(409)
                if settings.is_upload_destination:
                    await KonomiTVBS4KCloudDestination.filter(upload_slot=1).using_db(db).update(upload_slot=None)
                await KonomiTVBS4KCloudDestination.update_or_create(
                    connection_id=connection_id, using_db=db,
                    defaults={'owner_id': user.id, 'folder': settings.folder,
                              'upload_slot': 1 if settings.is_upload_destination else None},
                )
    except BlockingIOError:
        raise HTTPException(409) from None
    except OSError:
        raise HTTPException(503) from None
    return KonomiTVBS4KCloudDestinationInfo(connection_id=connection_id, **settings.model_dump())


async def KonomiTVBS4KCloudOperation(
    user: User, operation: Literal['List', 'Import', 'Check', 'Disconnect'],
    connection_id: UUID | None = None, imported: KonomiTVBS4KCloudImport | None = None,
):
    """認証情報を含む例外をログ・APIへ出さない回復境界。

    Args:
        user: 認証済みの管理者。
        operation: 許可済み操作。
        connection_id: 既存接続ID。
        imported: 取り込み入力。
    Returns:
        秘密を含まない操作結果。
    """
    loop = asyncio.get_running_loop()

    async def OwnerIsActive() -> bool:
        """接続操作のlock取得後に管理者の現存を確認する。"""
        return await User.filter(id=user.id, is_admin=True).exists()

    async def RecordCheck(connection: UUID, connected: bool) -> None:
        """ownerLock内の接続確認結果を認証とは別に記録する。

        Args:
            connection: 確認した接続UUID。
            connected: フォルダ一覧取得が成功したか。
        Returns:
            None
        """
        await KonomiTVBS4KCloudConnectionCheck.update_or_create(
            connection_id=connection,
            defaults={'owner_id': user.id, 'connected': connected, 'checked_at': datetime.now(UTC)},
        )

    try:
        return await asyncio.to_thread(
            KonomiTVBS4KCloudStorage.operate, user.id, operation, connection_id, imported,
            owner_is_active=lambda: asyncio.run_coroutine_threadsafe(OwnerIsActive(), loop).result(timeout=5),
            record_check=lambda connection, connected: asyncio.run_coroutine_threadsafe(
                RecordCheck(connection, connected), loop).result(timeout=5),
        )
    except BlockingIOError:
        raise HTTPException(status_code=409, detail='別のクラウド接続操作が実行中です。完了後に再試行してください。') from None
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail='接続が見つからないか、rclone が利用できません。') from None
    except ConnectionError:
        raise HTTPException(503, '接続を確認できませんでした。時間をおいて再試行し、必要ならキーを再取り込みしてください。') from None
    except (ValidationError, ValueError, KeyError, configparser.Error, subprocess.SubprocessError):
        logging.warning('[KonomiTVBS4KCloudStorage] Cloud connection operation failed validation or connectivity checks.')
        raise HTTPException(status_code=400, detail='接続を確認できません。キー・有効期限・リージョンを確認し、必要なら再取り込みしてください。') from None
    except OSError:
        logging.warning('[KonomiTVBS4KCloudStorage] Cloud credential storage operation failed.')
        raise HTTPException(status_code=503, detail='接続情報を保存できません。サーバーの保存領域を確認してください。') from None


async def ReadKonomiTVBS4KCloudImport(request: Request) -> KonomiTVBS4KCloudImport:
    """キーを含む検証エラーのinput値をFastAPI標準422応答へ渡さない。

    Args:
        request: 管理者からの取り込み要求。
    Returns:
        検証済み入力。秘密値はレスポンスに使用しない。
    """
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 65536:
            raise HTTPException(status_code=413, detail='取り込みデータが大きすぎます。')
        body.extend(chunk)
    try:
        return KonomiTVBS4KCloudImport.model_validate(json.loads(body))
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=400, detail='rclone のトークン JSON を確認してください。') from None


@router.get('', response_model=list[KonomiTVBS4KCloudConnection])
async def KonomiTVBS4KCloudConnectionsAPI(user: Admin, response: Response):
    """管理者自身の接続一覧を返す。"""
    response.headers['Cache-Control'] = 'no-store'
    result = await KonomiTVBS4KCloudOperation(user, 'List')
    if not isinstance(result, list):
        raise HTTPException(500)
    checks = {str(check.connection_id): check
              for check in await KonomiTVBS4KCloudConnectionCheck.filter(owner_id=user.id)}
    for connection in result:
        if not isinstance(connection, KonomiTVBS4KCloudConnection):
            raise HTTPException(500)
        check = checks.get(connection.id)
        # 再取り込み時の確認より古い失敗履歴で、新しい認証をErrorへ戻さない。
        if check is not None and (
            connection.checked_at is None or check.checked_at >= datetime.fromisoformat(connection.checked_at)
        ):
            connection.checked_at = check.checked_at.isoformat()
            if not check.connected:
                connection.status = 'Error'
            elif connection.expires_at is not None and datetime.fromisoformat(connection.expires_at) <= datetime.now(UTC):
                connection.status = 'NeedsCheck'
            else:
                connection.status = 'Connected'
    return result


@router.post('', response_model=KonomiTVBS4KCloudConnection)
async def KonomiTVBS4KCloudConnectionImportAPI(request: Request, user: Admin, response: Response):
    """rcloneのキーを確認して新規接続として取り込む。"""
    response.headers['Cache-Control'] = 'no-store'
    imported = await ReadKonomiTVBS4KCloudImport(request)
    return await KonomiTVBS4KCloudOperation(user, 'Import', imported=imported)


@router.put('/{connection_id}', response_model=KonomiTVBS4KCloudConnection)
async def KonomiTVBS4KCloudConnectionReimportAPI(connection_id: UUID, request: Request, user: Admin, response: Response):
    """所有する既存接続へ新しいキーを取り込む。"""
    response.headers['Cache-Control'] = 'no-store'
    imported = await ReadKonomiTVBS4KCloudImport(request)
    return await KonomiTVBS4KCloudOperation(user, 'Import', connection_id, imported)


@router.post('/{connection_id}/check', response_model=list[str])
async def KonomiTVBS4KCloudConnectionCheckAPI(connection_id: UUID, user: Admin, response: Response):
    """接続の有効性を確認し直下フォルダを返す。"""
    response.headers['Cache-Control'] = 'no-store'
    return await KonomiTVBS4KCloudOperation(user, 'Check', connection_id)


@router.delete('/{connection_id}', status_code=204)
async def KonomiTVBS4KCloudConnectionDisconnectAPI(connection_id: UUID, user: Admin):
    """クラウドのファイルは削除せずローカルの連携だけを解除する。"""
    try:
        await KonomiTVBS4KCloudOperation(user, 'Disconnect', connection_id)
    except HTTPException as error:
        # 認証削除後・DB整理前に中断したDELETEも、同じ要求の再送でローカル状態の整理へ収束させる。
        if error.status_code != 404:
            raise
    async with in_transaction() as db:
        await KonomiTVBS4KCloudDestination.filter(
            connection_id=connection_id, owner_id=user.id,
        ).using_db(db).delete()
        await KonomiTVBS4KCloudCatalogSync.filter(
            connection_id=connection_id, owner_id=user.id,
        ).using_db(db).delete()
        await KonomiTVBS4KCloudConnectionCheck.filter(
            connection_id=connection_id, owner_id=user.id,
        ).using_db(db).delete()
