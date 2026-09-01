from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Path, status

from app import logging, schemas
from app.constants import BANGUMI_REQUEST_HEADERS, HTTPX_CLIENT
from app.models.KonomiTVBS4KBangumiEpisodeCompletion import (
    KonomiTVBS4KBangumiEpisodeCompletion,
)
from app.models.RecordedProgram import RecordedProgram
from app.models.User import User
from app.routers.UsersRouter import GetCurrentAdminUser, GetCurrentUser
from app.utils.KonomiTVBS4KBangumiClient import KonomiTVBS4KBangumiClient
from app.utils.KonomiTVBS4KBangumiSharedStore import KonomiTVBS4KBangumiSharedStore


# ルーター
router = APIRouter(
    tags = ['Bangumi'],
    prefix = '/api/bangumi',
)



@router.get(
    '/me',
    summary = 'Bangumi 公開プロフィール取得 API',
    response_model = schemas.KonomiTVBS4KBangumiProfile,
)
async def BangumiProfileAPI(
    current_user: Annotated[User, Depends(GetCurrentUser)],
):
    """
    管理者1件の共有 Bangumi 公開プロフィールを返す。トークンは含めない。

    Args:
        current_user (User): ログイン中の KonomiTV ユーザー。

    Returns:
        schemas.KonomiTVBS4KBangumiProfile: 公開プロフィール。未連携なら各欄が None。
    """

    del current_user
    profile = KonomiTVBS4KBangumiSharedStore.getProfile()
    return schemas.KonomiTVBS4KBangumiProfile(
        bangumi_user_id = profile['bangumi_user_id'],
        bangumi_user_name = profile['bangumi_user_name'],
        bangumi_user_nickname = profile['bangumi_user_nickname'],
        bangumi_user_avatar_url = profile['bangumi_user_avatar_url'],
    )


async def UpdateBangumiEpisodeCollection(access_token: str, subject_id: int, episode_id: int) -> None:
    """
    Bangumi の対象エピソードを「看過」に更新する。

    Args:
        access_token (str): Bangumi 個人アクセストークン。
        subject_id (int): Bangumi 条目 ID。
        episode_id (int): Bangumi エピソード ID。

    Returns:
        None: Bangumi 上でエピソードの更新が完了した場合。

    Raises:
        HTTPException: Bangumi API への接続、認証、または更新に失敗した場合。
    """

    headers = {**BANGUMI_REQUEST_HEADERS, 'Authorization': f'Bearer {access_token}'}
    try:
        async with HTTPX_CLIENT() as httpx_client:
            # エピソードを「看過」にする。同じ値への PUT なのでリトライしても結果は変わらない
            episode_response = await httpx_client.put(
                url = f'{KonomiTVBS4KBangumiClient.API_BASE_URL}/users/-/collections/-/episodes/{episode_id}',
                headers = headers,
                json = {'type': 2},
            )

            # Bangumi は未収藏条目のエピソードを更新できないため、初回だけ「在看」を作成して再送する
            if episode_response.status_code == status.HTTP_400_BAD_REQUEST:
                collection_status_response = await httpx_client.get(
                    url = f'{KonomiTVBS4KBangumiClient.API_BASE_URL}/users/-/collections/{subject_id}/episodes',
                    headers = headers,
                )
                # 未収藏の場合だけ「在看」を作成し、既存の想看・看過・擱置・拋棄は上書きしない
                if collection_status_response.status_code == status.HTTP_404_NOT_FOUND:
                    collection_response = await httpx_client.post(
                        url = f'{KonomiTVBS4KBangumiClient.API_BASE_URL}/users/-/collections/{subject_id}',
                        headers = headers,
                        json = {'type': 3},
                    )
                    collection_response.raise_for_status()
                else:
                    collection_status_response.raise_for_status()
                episode_response = await httpx_client.put(
                    url = f'{KonomiTVBS4KBangumiClient.API_BASE_URL}/users/-/collections/-/episodes/{episode_id}',
                    headers = headers,
                    json = {'type': 2},
                )
            episode_response.raise_for_status()
    except (httpx.NetworkError, httpx.TimeoutException) as ex:
        logging.error('[KonomiTVBS4KBangumiRouter][UpdateBangumiEpisodeCollection] Failed to connect to Bangumi API.')
        raise HTTPException(
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE,
            detail = 'Failed to connect to Bangumi API',
        ) from ex
    except httpx.HTTPStatusError as ex:
        if ex.response.status_code in [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN]:
            logging.warning('[KonomiTVBS4KBangumiRouter][UpdateBangumiEpisodeCollection] Bangumi access token is invalid or forbidden.')
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = 'Bangumi access token is invalid or does not have write:collection permission',
            ) from ex
        logging.error(
            f'[KonomiTVBS4KBangumiRouter][UpdateBangumiEpisodeCollection] Bangumi API returned HTTP {ex.response.status_code}.',
        )
        raise HTTPException(
            status_code = status.HTTP_502_BAD_GATEWAY,
            detail = f'Failed to update Bangumi episode collection (HTTP Error {ex.response.status_code})',
        ) from ex


@router.post(
    '/auth',
    summary = 'Bangumi 個人アクセストークン認証 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def BangumiAuthAPI(
    auth_request: Annotated[schemas.BangumiAuthRequest, Body(description='Bangumi 認証リクエスト。')],
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """
    指定された個人アクセストークンで共有 Bangumi 連携を行い、管理者1件として保存する。<br>
    管理者の JWT が Authorization: Bearer に設定されていないとアクセスできない。

    Args:
        auth_request (schemas.BangumiAuthRequest): Bangumi 個人アクセストークン。
        current_user (User): ログイン中の KonomiTV ユーザー。

    Returns:
        None: 連携が完了した場合。

    Raises:
        HTTPException: Bangumi API への接続、認証、またはレスポンス形式の検証に失敗した場合。
    """

    # 前後の空白や改行はコピー時に混入しやすいため除去してから検証する
    access_token = auth_request.access_token.strip()
    if access_token == '':
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Bangumi access token is empty',
        )

    try:
        # 個人アクセストークンに対応するユーザーを取得し、有効なトークンであることとプロフィールを同時に確認する
        async with HTTPX_CLIENT() as httpx_client:
            user_api_response = await httpx_client.get(
                url = 'https://api.bgm.tv/v0/me',
                headers = {**BANGUMI_REQUEST_HEADERS, 'Authorization': f'Bearer {access_token}'},
            )
    except (httpx.NetworkError, httpx.TimeoutException) as ex:
        logging.error('[KonomiTVBS4KBangumiRouter][BangumiAuthAPI] Failed to get user information. (Connection Timeout)')
        raise HTTPException(
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE,
            detail = 'Failed to get Bangumi user information (Connection Timeout)',
        ) from ex

    # 認証エラーは入力したトークンが無効または期限切れであることをクライアントへ明示する
    if user_api_response.status_code == status.HTTP_401_UNAUTHORIZED:
        logging.warning('[KonomiTVBS4KBangumiRouter][BangumiAuthAPI] Bangumi access token is invalid or expired.')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Bangumi access token is invalid or expired',
        )

    # Bangumi API 側の障害や仕様外レスポンスを認証エラーと混同しない
    if user_api_response.status_code != status.HTTP_200_OK:
        logging.error(
            f'[KonomiTVBS4KBangumiRouter][BangumiAuthAPI] Failed to get user information. (HTTP Error {user_api_response.status_code})',
        )
        raise HTTPException(
            status_code = status.HTTP_502_BAD_GATEWAY,
            detail = f'Failed to get Bangumi user information (HTTP Error {user_api_response.status_code})',
        )

    try:
        user_api_response_json = cast(dict[str, Any], user_api_response.json())
        bangumi_user_id = int(user_api_response_json['id'])
        bangumi_user_name = str(user_api_response_json['username'])
        bangumi_user_nickname = str(user_api_response_json['nickname'])
        avatar = cast(dict[str, Any], user_api_response_json['avatar'])
        bangumi_user_avatar_url = str(avatar['large'])
    except (KeyError, TypeError, ValueError) as ex:
        logging.error('[KonomiTVBS4KBangumiRouter][BangumiAuthAPI] Bangumi user information is invalid.', exc_info=ex)
        raise HTTPException(
            status_code = status.HTTP_502_BAD_GATEWAY,
            detail = 'Bangumi user information is invalid',
        ) from ex

    # すべての外部 API 検証が完了してから、共有ストアへ暗号化済みトークンと公開プロフィールを保存する。
    KonomiTVBS4KBangumiSharedStore.save(
        access_token = access_token,
        bangumi_user_id = bangumi_user_id,
        bangumi_user_name = bangumi_user_name,
        bangumi_user_nickname = bangumi_user_nickname,
        bangumi_user_avatar_url = bangumi_user_avatar_url,
    )
    del current_user

    # 認証 API の応答を外部收藏一覧のページングから切り離し、以後の照合をバックエンドだけで進める。
    KonomiTVBS4KBangumiClient.scheduleUserCollectionSync()

    logging.info(
        f'[KonomiTVBS4KBangumiRouter][BangumiAuthAPI] Linked shared Bangumi account. '
        f'[bangumi_user_id: {bangumi_user_id}]',
    )


@router.post(
    '/videos/{video_id}/progress',
    summary = 'Bangumi 録画視聴進捗 API',
    response_model = schemas.BangumiPlaybackProgressResponse,
    status_code = status.HTTP_200_OK,
)
async def BangumiPlaybackProgressAPI(
    video_id: Annotated[int, Path(description='視聴中の録画番組 ID。')],
    progress_request: Annotated[schemas.BangumiPlaybackProgressRequest, Body(description='録画視聴進捗。')],
    current_user: Annotated[User, Depends(GetCurrentUser)],
):
    """
    指定した録画番組の視聴進捗を受け取り、完了位置到達後に照合済み Bangumi エピソードを「看過」に更新する。<br>
    JWT エンコードされたアクセストークンが Authorization: Bearer に設定されていないとアクセスできない。

    Args:
        video_id (int): 視聴中の録画番組 ID。
        progress_request (schemas.BangumiPlaybackProgressRequest): プレイヤーが解決した再生位置と録画時間。
        current_user (User): ログイン中の KonomiTV ユーザー。

    Returns:
        schemas.BangumiPlaybackProgressResponse: 同期処理の結果。

    Raises:
        HTTPException: 録画番組が存在しない、連携がない、または Bangumi API 更新に失敗した場合。
    """

    # await を挟む前に token と Bangumi user ID を同じ共有ストアスナップショットへ固定する。
    # 連携切替と競合しても、外部 PUT と完了記録は必ず同じ Bangumi アカウントへ結び付ける。
    shared_credentials = KonomiTVBS4KBangumiSharedStore.getCredentials()
    access_token = shared_credentials['access_token']
    shared_bangumi_user_id = shared_credentials['bangumi_user_id']
    if access_token is None or shared_bangumi_user_id is None:
        return schemas.BangumiPlaybackProgressResponse(status='NotEligible')

    recorded_program = await RecordedProgram.get_or_none(id=video_id).prefetch_related('recorded_video', 'series')
    if recorded_program is None:
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified video_id was not found',
        )

    # CM 解析済みならサーバーの録画時間軸を正とする。未解析時は Raw MMT/TLV を含め、実プレイヤーが解決した時間を使う。
    completion_duration = recorded_program.recorded_video.duration \
        if recorded_program.recorded_video.cm_sections else progress_request.duration
    if KonomiTVBS4KBangumiClient.isPlaybackCompleted(
        progress_request.playback_position,
        completion_duration,
        recorded_program.recorded_video.cm_sections,
    ) is False:
        return schemas.BangumiPlaybackProgressResponse(status='Pending')

    # 録画中は CM 解析とファイル終端が未確定なので、録画完了後にクライアントから再送してもらう。
    if recorded_program.recorded_video.status != 'Recorded':
        return schemas.BangumiPlaybackProgressResponse(status='Pending')
    # 単一の正整数話数を抽出できない番組は誤同期を避けるため対象外とする。
    if KonomiTVBS4KBangumiClient.parseEpisodeNumber(recorded_program.episode_number) is None:
        return schemas.BangumiPlaybackProgressResponse(status='NotEligible')

    # 過去の同期済み記録があれば、重播や複数タブからの重複更新を避ける。
    # 完了記録は共有 Bangumi アカウント identity で区切り、切替後の旧行では PUT を止めない。
    if recorded_program.bangumi_episode_id is not None:
        is_completed = await KonomiTVBS4KBangumiEpisodeCompletion.filter(
            user_id = current_user.id,
            bangumi_user_id = shared_bangumi_user_id,
            bangumi_episode_id = recorded_program.bangumi_episode_id,
        ).exists()
        if is_completed:
            return schemas.BangumiPlaybackProgressResponse(status='AlreadyCompleted')

    # 条目・話数照合は收藏一覧同期だけが担当し、再生 API から作品ごとの検索を発生させない。
    # 所属 Series の確定 subject と一致しない旧 mapping からは外部 PUT しない。
    series = recorded_program.series
    if (
        recorded_program.series_id is None
        or series is None
        or series.bangumi_subject_id is None
        or recorded_program.bangumi_subject_id != series.bangumi_subject_id
        or recorded_program.bangumi_episode_id is None
    ):
        logging.info(
            f'[KonomiTVBS4KBangumiRouter][BangumiPlaybackProgressAPI] Bangumi episode is not mapped. [video_id: {video_id}]',
        )
        return schemas.BangumiPlaybackProgressResponse(status='Pending')

    bangumi_subject_id = recorded_program.bangumi_subject_id
    bangumi_episode_id = recorded_program.bangumi_episode_id
    assert bangumi_subject_id is not None
    assert bangumi_episode_id is not None

    # 外部更新に成功した後で完了記録を作り、失敗を成功として扱わない
    await UpdateBangumiEpisodeCollection(
        access_token,
        bangumi_subject_id,
        bangumi_episode_id,
    )
    await KonomiTVBS4KBangumiEpisodeCompletion.get_or_create(
        user_id = current_user.id,
        bangumi_user_id = shared_bangumi_user_id,
        bangumi_episode_id = bangumi_episode_id,
        defaults = {'source_recorded_program_id': recorded_program.id},
    )
    logging.info(
        f'[KonomiTVBS4KBangumiRouter][BangumiPlaybackProgressAPI] Completed Bangumi episode. '
        f'[konomitv_user_id: {current_user.id}, video_id: {video_id}, '
        f'bangumi_episode_id: {bangumi_episode_id}]',
    )
    return schemas.BangumiPlaybackProgressResponse(status='Completed')


@router.post(
    '/logout',
    summary = 'Bangumi アカウント連携解除 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def BangumiAccountLogoutAPI(
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """
    管理者1件の共有 Bangumi 連携を解除する。<br>
    管理者の JWT が Authorization: Bearer に設定されていないとアクセスできない。

    Args:
        current_user (User): ログイン中の管理者。

    Returns:
        None: 連携解除が完了した場合。
    """

    del current_user
    KonomiTVBS4KBangumiSharedStore.clear()
    logging.info('[KonomiTVBS4KBangumiRouter][BangumiAccountLogoutAPI] Unlinked shared Bangumi account.')
