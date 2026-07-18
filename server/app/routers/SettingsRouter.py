
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, status

from app import logging
from app.config import ClientSettings, Config, SaveConfig, ServerSettings
from app.models.User import User
from app.routers.UsersRouter import GetCurrentAdminUser, GetCurrentUser
from app.utils import GetPlatformEnvironment


# ルーター
router = APIRouter(
    tags = ['Settings'],
    prefix = '/api/settings',
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
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """
    return current_user.client_settings


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
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """

    # 現在サーバーに保存されているクライアント設定の最終同期時刻よりも古いクライアント設定が送られてきた場合、エラーを返す
    current_client_settings = ClientSettings.model_validate(current_user.client_settings)
    if client_settings.last_synced_at < current_client_settings.last_synced_at:
        logging.error(f'[ClientSettingsUpdateAPI] Client settings are outdated! [{client_settings.last_synced_at} < {current_client_settings.last_synced_at}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'The client settings are outdated. Please update the client settings from the server.',
        )

    # dict に変換してから入れる
    ## Pydantic モデルのままだと JSON にシリアライズできないので怒られる
    current_user.client_settings = dict(client_settings)

    # レコードを保存する
    await current_user.save()


@router.get(
    '/server',
    summary = 'サーバー設定取得 API',
    response_description = '現在稼働中の KonomiTV サーバーのサーバー設定。',
    response_model = ServerSettings,
)
async def ServerSettingsAPI():
    """
    現在稼働中の KonomiTV サーバーのサーバー設定を取得する。<br>
    Docker 環境では、録画fMP4キャッシュ保存先を除くパス項目はDocker用Prefix (/host-rootfs) 付きで返される。<br>
    録画fMP4キャッシュ保存先は設定画面で編集できるよう、ホスト側の絶対パスで返される。<br>
    """

    settings = Config().model_copy(deep=True)
    if (
        GetPlatformEnvironment() == 'Linux-Docker' and
        settings.video.recorded_fmp4_cache_folder is not None
    ):
        internal_path = str(settings.video.recorded_fmp4_cache_folder)
        settings.video.recorded_fmp4_cache_folder = Path(internal_path.removeprefix('/host-rootfs'))
    return settings


@router.put(
    '/server',
    summary = 'サーバー設定更新 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def ServerSettingsUpdateAPI(
    server_settings: Annotated[ServerSettings, Body(description='更新するサーバー設定のデータ。')],
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """
    現在稼働中の KonomiTV サーバーのサーバー設定を更新する。<br>
    Docker 環境では、録画fMP4キャッシュ保存先だけはホスト側の絶対パスを指定する。<br>
    その他のパス項目にはDocker用Prefix (/host-rootfs) を付与した状態でリクエストする。<br>
    Pydantic のカスタムバリデーターの実装の都合上、バリデーション処理中はメインスレッドが数秒間ブロッキングされることがあるので注意。<br>

    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    # バリデーションが完了したサーバー設定を config.yaml に保存する
    SaveConfig(server_settings)
