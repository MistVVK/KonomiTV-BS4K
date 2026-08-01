
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from tortoise.transactions import in_transaction

from app import logging
from app.config import ClientSettings, Config, HostServerSettings, SaveConfig
from app.models.User import User
from app.routers.UsersRouter import GetCurrentAdminUser, GetCurrentUser
from app.utils.HostPath import HostPathError, ToUserHostPathText


# ルーター
router = APIRouter(
    tags = ['Settings'],
    prefix = '/api/settings',
)

_HOST_PATH_SETTINGS_API_PATHS = {
    '/api/settings/server',
    '/api/cm-analysis/settings',
}


def _SanitizeValidationErrors(
    exception: ValidationError | RequestValidationError,
    include_body_location: bool = False,
) -> list[dict[str, object]]:
    """
    Pydanticの検証エラーから入力値と内部パスを除き、項目情報を保持する。

    Args:
        exception (ValidationError | RequestValidationError): サニタイズ対象の検証エラー。
        include_body_location (bool): locの先頭へFastAPIのbody区分を追加するかどうか。

    Returns:
        list[dict[str, object]]: type・loc・安全なmsgだけを含むエラー一覧。
    """

    sanitized_errors: list[dict[str, object]] = []
    for error in exception.errors():
        location = list(error['loc'])
        if include_body_location and (len(location) == 0 or location[0] != 'body'):
            location.insert(0, 'body')
        sanitized_errors.append({
            'type': str(error['type']),
            'loc': location,
            'msg': ToUserHostPathText(str(error['msg'])),
        })
    return sanitized_errors


async def HostPathRequestValidationErrorHandler(
    request: Request,
    exception: RequestValidationError,
) -> JSONResponse:
    """
    パス設定APIの422レスポンスから入力値を除き、内部接頭辞の再露出を防ぐ。

    Args:
        request (Request): バリデーションに失敗したHTTPリクエスト。
        exception (RequestValidationError): FastAPIが生成した入力エラー。

    Returns:
        JSONResponse: 対象APIでは入力値なし、それ以外ではFastAPI標準の422レスポンス。
    """

    if request.url.path not in _HOST_PATH_SETTINGS_API_PATHS:
        default_response = await request_validation_exception_handler(request, exception)
        assert isinstance(default_response, JSONResponse)
        return default_response

    # FastAPI標準のエラー要素に含まれるinputとctxは、旧内部パスをそのまま転載し得る。
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={'detail': _SanitizeValidationErrors(exception)},
    )


@router.get(
    '/client',
    summary = 'クライアント設定取得 API',
    response_description = 'ログイン中のユーザーアカウントのクライアント設定。',
    response_model = ClientSettings,
)
async def ClientSettingsAPI(
    current_user: Annotated[User, Depends(GetCurrentUser)],
):
    """
    現在ログイン中のユーザーアカウントのクライアント設定を取得する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。<br>
    既存 DB に NaN/Inf など非有限値が残っている場合は default へ自己修復する。
    """
    try:
        return ClientSettings.model_validate(current_user.client_settings)
    except ValidationError as ex:
        logging.warning(
            f'[ClientSettingsAPI] Corrupted client settings detected for user {current_user.id}. '
            f'Resetting to defaults. ({ex})',
        )
        repaired = ClientSettings()
        current_user.client_settings = repaired.model_dump(mode='json')
        await current_user.save(update_fields=['client_settings', 'updated_at'])
        return repaired


@router.put(
    '/client',
    summary = 'クライアント設定更新 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def ClientSettingsUpdateAPI(
    client_settings: Annotated[ClientSettings, Body(description='更新するクライアント設定のデータ。')],
    current_user: Annotated[User, Depends(GetCurrentUser)],
):
    """
    現在ログイン中のユーザーアカウントのクライアント設定を更新する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。<br>
    非有限値 (NaN / Inf) は ClientSettings の Field 制約により 422 で拒否される。
    """

    # dependency 解決時の snapshot ではなく、行ロック付きで最新 row を読み直して CAS する
    ## 並行 PUT が同じ旧 snapshot を読んだあと、遅れて commit した古い値が新しい値を巻き戻すのを防ぐ
    async with in_transaction():
        user = await User.filter(id=current_user.id).select_for_update().get()
        try:
            current_client_settings = ClientSettings.model_validate(user.client_settings)
        except ValidationError:
            # 汚染済み既存値は default 相当として CAS 比較し、今回の更新で上書き修復する
            current_client_settings = ClientSettings()
        if client_settings.last_synced_at < current_client_settings.last_synced_at:
            logging.error(
                f'[ClientSettingsUpdateAPI] Client settings are outdated! '
                f'[{client_settings.last_synced_at} < {current_client_settings.last_synced_at}]',
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='The client settings are outdated. Please update the client settings from the server.',
            )

        # dict に変換してから入れる
        ## Pydantic モデルのままだと JSON にシリアライズできないので怒られる
        ## mode='json' で非有限値が残らない形に正規化する
        user.client_settings = client_settings.model_dump(mode='json')
        await user.save()


@router.get(
    '/server',
    summary = 'サーバー設定取得 API',
    response_description = '現在稼働中の KonomiTV-BS4K サーバーのサーバー設定。',
    response_model = HostServerSettings,
)
async def ServerSettingsAPI() -> HostServerSettings:
    """
    現在稼働中の KonomiTV-BS4K サーバーのサーバー設定を取得する。<br>
    Docker環境でも、ユーザーが設定するすべてのパス項目はホスト側の絶対パスで返される。<br>
    """

    return HostServerSettings.fromServerSettings(Config())


@router.put(
    '/server',
    summary = 'サーバー設定更新 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def ServerSettingsUpdateAPI(
    server_settings: Annotated[HostServerSettings, Body(description='更新するサーバー設定のデータ。')],
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """
    現在稼働中の KonomiTV-BS4K サーバーのサーバー設定を更新する。<br>
    Docker環境でも、すべてのパス項目にはホスト側の絶対パスを指定する。<br>
    Pydantic のカスタムバリデーターの実装の都合上、バリデーション処理中はメインスレッドが数秒間ブロッキングされることがあるので注意。<br>

    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    # DirectoryPath / FilePathの検証前に、全対象パスをDocker内部の実アクセス先へ変換する。
    ## 検証エラーの入力値は返さず、内部パスが422レスポンスへ露出しないようにする。
    try:
        server_settings.toServerSettings()
    except HostPathError as ex:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(ex),
        ) from ex
    except ValidationError as ex:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_SanitizeValidationErrors(ex, include_body_location=True),
        ) from ex

    # バリデーションが完了したサーバー設定を config.yaml に保存する
    SaveConfig(server_settings)
