import asyncio
import base64
import json
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from app.models.User import User
from app.routers.UsersRouter import GetCurrentAdminUser
from app.utils.KonomiTVBS4KCloudCryptKeys import (
    KonomiTVBS4KCloudCryptKeyInfo,
    KonomiTVBS4KCloudCryptKeys,
    KonomiTVBS4KCloudCryptKeySet,
    KonomiTVBS4KCloudCryptKeySnapshot,
    KonomiTVBS4KCryptKeyNotExtractedError,
    KonomiTVBS4KCryptKeySnapshotChangedError,
    KonomiTVBS4KCryptLegacyMigrationRequiredError,
)


router = APIRouter(prefix='/api/konomitv-bs4k/cloud-crypt-keys', tags=['KonomiTVBS4K Cloud Crypt Keys'])


async def RunKonomiTVBS4KCryptKeyOperation[T](operation: Callable[[], T], response: Response) -> T:
    """秘密を含む検証例外を通常のAPI応答へ反射しない。

    Args:
        operation: 管理者向けの鍵操作。
        response: キャッシュ禁止を設定する応答。
    Returns:
        操作結果。平文を返すのは専用取出APIだけ。
    """
    response.headers['Cache-Control'] = 'no-store'
    try:
        return await asyncio.to_thread(operation)
    except FileExistsError:
        raise HTTPException(409, '既に暗号化鍵が登録されています。既存の鍵を上書きすることはできません。新しい鍵を投入または生成する場合は、先に現在の鍵を削除してください。') from None
    except BlockingIOError:
        raise HTTPException(409) from None
    except FileNotFoundError:
        raise HTTPException(404) from None
    except KonomiTVBS4KCryptKeyNotExtractedError:
        raise HTTPException(409, '鍵を取り出す前にバックアップ完了を確認することはできません。まず鍵を取り出して内容を安全な場所へ保管してください。') from None
    except KonomiTVBS4KCryptKeySnapshotChangedError:
        raise HTTPException(412, '暗号化鍵の状態が変更されたため、操作を中断しました。画面を再読み込みして現在の鍵の状態を確認してください。') from None
    except KonomiTVBS4KCryptLegacyMigrationRequiredError:
        raise HTTPException(409, '旧形式の暗号化鍵が残っているため、この操作を実行できません。先に旧形式の鍵を一括削除し、移行を完了してください。') from None
    except (ValueError, TypeError, KeyError):
        raise HTTPException(400, '暗号化鍵の形式が不正です。取り出した Base64 文字列が正しく入力されているか確認してください。') from None
    except OSError:
        raise HTTPException(503) from None


@router.get('', response_model=KonomiTVBS4KCloudCryptKeySnapshot)
async def KonomiTVBS4KCryptKeysListAPI(
    response: Response, _user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """鍵を含まない領域鍵状態だけを列挙する。"""
    snapshot = await RunKonomiTVBS4KCryptKeyOperation(KonomiTVBS4KCloudCryptKeys.snapshot, response)
    response.headers['ETag'] = f'"{snapshot.revision}"'
    return snapshot


@router.post('', response_model=KonomiTVBS4KCloudCryptKeyInfo)
async def KonomiTVBS4KCryptKeysCreateAPI(
    response: Response, _user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """鍵を生成し、公開状態だけを返す。"""
    return await RunKonomiTVBS4KCryptKeyOperation(KonomiTVBS4KCloudCryptKeys.create, response)


@router.post('/insert', response_model=KonomiTVBS4KCloudCryptKeyInfo)
async def KonomiTVBS4KCryptKeysInsertAPI(
    request: Request, response: Response, _user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """管理者の平文鍵投入を受け付ける。入力値を検証エラーへ含めない。"""
    body = bytearray()
    async for chunk in request.stream():
        # JSONのエスケープとbase64による増加を含めても、入力は有限サイズに制限する。
        if len(body) + len(chunk) > 262144:
            raise HTTPException(413)
        body.extend(chunk)

    def InsertKeys() -> KonomiTVBS4KCloudCryptKeyInfo:
        # 外向けの形式だけを復号し、内部の鍵検証・保管形式・既存鍵上書き禁止は維持する。
        encoded = json.loads(body)
        if not isinstance(encoded, str):
            raise ValueError('Crypt key export must be a base64 string.')
        keys = KonomiTVBS4KCloudCryptKeySet.model_validate_json(base64.b64decode(encoded, validate=True))
        # 転送表現の上限を広げても、保管後に既存loaderが読めない鍵ファイルは作らない。
        if len(KonomiTVBS4KCloudCryptKeys.serializePlaintext(keys)) > 32768:
            raise ValueError('Crypt key file is too large.')
        return KonomiTVBS4KCloudCryptKeys.insert(keys)
    return await RunKonomiTVBS4KCryptKeyOperation(InsertKeys, response)


@router.post('/extract')
async def KonomiTVBS4KCryptKeysExtractAPI(
    if_match: Annotated[str, Header(pattern='^"[0-9a-f]{64}"$')],
    _user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """管理者が明示的に要求した領域の平文鍵だけを返す。"""
    response = Response(media_type='application/json', headers={'Cache-Control': 'no-store'})

    def ExtractKeys() -> bytes:
        # base64は秘密の保護ではなく受渡し表現。確認マーカーは従来と同じ鍵内容へ結び付ける。
        plaintext = KonomiTVBS4KCloudCryptKeys.extract(if_match[1:-1])
        return json.dumps(base64.b64encode(plaintext).decode('ascii')).encode('utf-8')
    response.body = await RunKonomiTVBS4KCryptKeyOperation(ExtractKeys, response)
    response.headers['Content-Length'] = str(len(response.body))
    return response


@router.post('/confirm-backup', response_model=KonomiTVBS4KCloudCryptKeyInfo)
async def KonomiTVBS4KCryptKeysConfirmBackupAPI(
    if_match: Annotated[str, Header(pattern='^"[0-9a-f]{64}"$')],
    response: Response, _user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """管理者が取出後に行う保管完了確認を記録する。"""
    return await RunKonomiTVBS4KCryptKeyOperation(lambda: KonomiTVBS4KCloudCryptKeys.confirmBackup(if_match[1:-1]), response)


@router.delete('', status_code=204)
async def KonomiTVBS4KCryptKeysDeleteAPI(
    if_match: Annotated[str, Header(pattern='^"[0-9a-f]{64}"$')],
    response: Response, _user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """確認ダイアログで承認したローカル鍵だけを削除する。"""
    await RunKonomiTVBS4KCryptKeyOperation(lambda: KonomiTVBS4KCloudCryptKeys.delete(if_match[1:-1]), response)
