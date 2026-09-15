import asyncio
import configparser
import json
import subprocess
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError

from app import logging
from app.models.User import User
from app.routers.UsersRouter import GetCurrentAdminUser
from app.utils.KonomiTVBS4KCloudStorage import (
    KonomiTVBS4KCloudConnection,
    KonomiTVBS4KCloudImport,
    KonomiTVBS4KCloudStorage,
)


router = APIRouter(prefix='/api/konomitv-bs4k/cloud-storage', tags=['KonomiTVBS4K Cloud Storage'])
Admin = Annotated[User, Depends(GetCurrentAdminUser)]


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

    try:
        return await asyncio.to_thread(
            KonomiTVBS4KCloudStorage.operate, user.id, operation, connection_id, imported,
            owner_is_active=lambda: asyncio.run_coroutine_threadsafe(OwnerIsActive(), loop).result(timeout=5),
        )
    except BlockingIOError:
        raise HTTPException(status_code=409, detail='別のクラウド接続操作が実行中です。完了後に再試行してください。') from None
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail='接続が見つからないか、rclone が利用できません。') from None
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
    return await KonomiTVBS4KCloudOperation(user, 'List')


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
    await KonomiTVBS4KCloudOperation(user, 'Disconnect', connection_id)
