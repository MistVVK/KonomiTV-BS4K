
import base64
import json
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Query, Request, status
from jose import jwt

from app import logging, schemas
from app.constants import API_REQUEST_HEADERS, HTTPX_CLIENT, NICONICO_OAUTH_CLIENT_ID
from app.models.NiconicoOAuthState import NiconicoOAuthState
from app.models.User import User
from app.routers.UsersRouter import GetCurrentUser
from app.utils import Interlaced
from app.utils.OAuthCallbackResponse import OAuthCallbackResponse


# ルーター
router = APIRouter(
    tags = ['Niconico'],
    prefix = '/api/niconico',
)


def _BuildSettingsJikkyoRedirectUrl(client_url: str) -> str:
    """クライアント URL から実況設定画面へのリダイレクト先を組み立てる。"""

    return f'{client_url.rstrip("/")}/settings/jikkyo'


def _NormalizeClientUrl(client_url: str) -> str:
    """クライアント URL を末尾スラッシュ付きで正規化する。"""

    return client_url.rstrip('/') + '/'


@router.get(
    '/auth',
    summary = 'ニコニコ OAuth 認証 URL 発行 API',
    response_model = schemas.ThirdpartyAuthURL,
    response_description = 'ユーザーにアプリ連携してもらうための認証 URL。',
)
async def NiconicoAuthURLAPI(
    request: Request,
    current_user: Annotated[User, Depends(GetCurrentUser)],
):
    """
    ニコニコアカウントと連携するための認証 URL を取得する。<br>
    認証 URL をブラウザで開くとアプリ連携の許可を求められ、ユーザーが許可すると /api/niconico/callback に戻ってくる。

    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。<br>
    """

    # クライアント (フロントエンド) の URL を Origin ヘッダーから取得
    ## Origin ヘッダーがリクエストに含まれていない場合はこの API サーバーの URL を使う
    client_url = _NormalizeClientUrl(
        request.headers.get('Origin', f'https://{request.url.netloc}')
    )

    # コールバック URL を設定
    ## ニコニコ API の OAuth 連携では、事前にコールバック先の URL を運営側に設定しておく必要がある
    ## 一方 KonomiTV サーバーの URL はまちまちなので、コールバック先の URL を一旦 https://app.konomi.tv/api/redirect/niconico に集約する
    ## この API は、リクエストを認証 URL の "state" パラメーター内で指定された KonomiTV サーバーの NiconicoAuthCallbackAPI にリダイレクトする
    ## 最後に KonomiTV サーバーがリダイレクトを受け取ることで、コールバック対象の URL が定まらなくても OAuth 連携ができるようになる
    ## ref: https://github.com/tsukumijima/KonomiTV-API
    ## app.konomi.tv は state JSON の server 以外のキーをクエリへ転送する（user_access_token は載せない）
    callback_url = 'https://app.konomi.tv/api/redirect/niconico'

    # ログイン用 JWT は URL / 外部サービスへ出さない。
    # 代わりにサーバ側の短命・使い捨て state を発行し、その ID だけを OAuth state に載せる。
    oauth_state_id, code_challenge = await NiconicoOAuthState.issue(
        user_id = current_user.id,
        client_url = client_url,
    )

    # コールバック後の NiconicoAuthCallbackAPI に渡す state の値
    ## client も state 経由で転送されるが、サーバは DB 上の client_url を正本として使う
    state = {
        # リダイレクト先の KonomiTV サーバー
        'server': f'https://{request.url.netloc}/',
        # スマホ・タブレットでのフォールバック用クライアント URL（表示・旧経路向け）
        'client': client_url,
        # サーバ側セッション ID（ログイン JWT ではない）
        'oauth_state': oauth_state_id,
    }

    # state は URL パラメータとして送らないといけないので、JSON エンコードしたあと Base64 でエンコードする
    state_base64 = base64.b64encode(json.dumps(state, ensure_ascii=False).encode('utf-8')).decode('utf-8')

    # 末尾の = はニコニコ側でリダイレクトされる際に変に URL エンコードされる事があるので、削除する
    state_base64 = state_base64.replace('=', '')

    # 利用するスコープを指定
    scope = '%20'.join([
        'offline_access',
        'openid',
        'profile',
        'user.authorities.relives.watch.get',
        'user.authorities.relives.watch.interact',
        'user.premium',
    ])

    # 認証 URL を作成
    authorization_url = (
        f'https://oauth.nicovideo.jp/oauth2/authorize?response_type=code&'
        f'scope={scope}&client_id={NICONICO_OAUTH_CLIENT_ID}&redirect_uri={callback_url}&state={state_base64}&'
        f'code_challenge={code_challenge}&code_challenge_method=S256'
    )

    return {'authorization_url': authorization_url}


@router.get(
    '/callback',
    summary = 'ニコニコ OAuth コールバック API',
    response_class = OAuthCallbackResponse,
    response_description = 'ユーザーアカウントにニコニコアカウントのアクセストークン・リフレッシュトークンが登録できたことを示す。',
)
async def NiconicoAuthCallbackAPI(
    oauth_state: Annotated[str | None, Query(description='サーバ発行の OAuth state ID。ログイン JWT ではない。')] = None,
    client: Annotated[str | None, Query(description='OAuth 連携元クライアント URL（リダイレクト用フォールバック）。')] = None,
    code: Annotated[str | None, Query(description='コールバック元から渡された認証コード。OAuth 認証が成功したときのみセットされる。')] = None,
    error: Annotated[str | None, Query(description='このパラメーターがセットされているとき、OAuth 認証がユーザーによって拒否されたことを示す。')] = None,
    # 旧実装が state に載せていたログイン JWT。受理してユーザー認証に使わない（SEC-001）。
    user_access_token: Annotated[str | None, Query(description='互換のため残す。無視される。', include_in_schema=False)] = None,
):
    """
    ニコニコの OAuth 認証のコールバックを受け取り、ログイン中のユーザーアカウントとニコニコアカウントを紐づける。
    """

    del user_access_token  # 意図的に未使用。クエリに残っていてもセッション認証には使わない。

    # エラー応答用のリダイレクト先（state 消費前はクエリ client をフォールバックに使う）
    if client:
        redirect_url = _BuildSettingsJikkyoRedirectUrl(client)
    else:
        # client も無い場合は最低限の相対パス相当を避け、自サーバ設定画面を示すプレースホルダ
        redirect_url = '/settings/jikkyo'

    # state は OAuth の成功・拒否にかかわらず、callback へ到達した時点で一度だけ消費する。
    pending_state: NiconicoOAuthState | None = None
    if oauth_state is not None:
        try:
            pending_state = await NiconicoOAuthState.consume(oauth_state)
        except ValueError:
            pass

    if oauth_state is None:
        logging.warning('[NiconicoRouter][NiconicoAuthCallbackAPI] OAuth state is missing.')
        return OAuthCallbackResponse(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail = 'OAuth state is missing',
            redirect_to = redirect_url,
        )

    if pending_state is None:
        logging.warning('[NiconicoRouter][NiconicoAuthCallbackAPI] OAuth state is invalid or expired.')
        return OAuthCallbackResponse(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail = 'OAuth state is invalid or expired',
            redirect_to = redirect_url,
        )

    # リダイレクト先は発行時に保存した client_url を正本にする（クエリ改ざんを無視）
    redirect_url = _BuildSettingsJikkyoRedirectUrl(pending_state.client_url)

    # "error" パラメーターがセットされている
    # OAuth 認証がユーザーによって拒否されたことを示しているので、401 エラーにする
    if error is not None:

        # 401 エラーを送出
        ## コールバック元から渡されたエラーメッセージをそのまま表示する
        logging.warning(f'[NiconicoRouter][NiconicoAuthCallbackAPI] Authorization was denied. ({error})')
        return OAuthCallbackResponse(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail = f'Authorization was denied ({error})',
            redirect_to = redirect_url,
        )

    # なぜか code がない
    if code is None:
        logging.error('[NiconicoRouter][NiconicoAuthCallbackAPI] Authorization code does not exist.')
        return OAuthCallbackResponse(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail = 'Authorization code does not exist',
            redirect_to = redirect_url,
        )

    current_user = await User.filter(id=pending_state.user_id).get_or_none()
    if current_user is None:
        logging.warning(
            f'[NiconicoRouter][NiconicoAuthCallbackAPI] User for OAuth state does not exist. '
            f'[user_id: {pending_state.user_id}]'
        )
        return OAuthCallbackResponse(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail = 'User associated with OAuth state does not exist',
            redirect_to = redirect_url,
        )

    try:

        # 認証コードを使い、ニコニコ OAuth のアクセストークンとリフレッシュトークンを取得
        token_api_url = 'https://oauth.nicovideo.jp/oauth2/token'
        async with HTTPX_CLIENT() as httpx_client:
            token_api_response = await httpx_client.post(
                url = token_api_url,
                headers = {**API_REQUEST_HEADERS, 'Content-Type': 'application/x-www-form-urlencoded'},
                data = {
                    'grant_type': 'authorization_code',
                    'client_id': NICONICO_OAUTH_CLIENT_ID,
                    'client_secret': Interlaced(3),
                    'code': code,
                    'code_verifier': pending_state.code_verifier,
                    'redirect_uri': 'https://app.konomi.tv/api/redirect/niconico',
                },
            )

        # ステータスコードが 200 以外
        if token_api_response.status_code != 200:
            logging.error(f'[NiconicoRouter][NiconicoAuthCallbackAPI] Failed to get access token. (HTTP Error {token_api_response.status_code})')
            return OAuthCallbackResponse(
                status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail = f'Failed to get access token. (HTTP Error {token_api_response.status_code})',
                redirect_to = redirect_url,
            )

        token_api_response_json = token_api_response.json()

    # 接続エラー（サーバーメンテナンスやタイムアウトなど）
    except (httpx.NetworkError, httpx.TimeoutException):
        logging.error('[NiconicoRouter][NiconicoAuthCallbackAPI] Failed to get access token. (Connection Timeout)')
        return OAuthCallbackResponse(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail = 'Failed to get access token. (Connection Timeout)',
            redirect_to = redirect_url,
        )

    # 取得したアクセストークンとリフレッシュトークンをユーザーアカウントに設定
    ## アクセストークンは1時間で有効期限が切れるので、適宜リフレッシュトークンで再取得する
    current_user.niconico_access_token = str(token_api_response_json['access_token'])
    current_user.niconico_refresh_token = str(token_api_response_json['refresh_token'])

    # ニコニコアカウントのユーザー ID を取得
    # ユーザー ID は id_token の JWT の中に含まれている
    id_token_jwt = jwt.get_unverified_claims(token_api_response_json['id_token'])
    current_user.niconico_user_id = int(id_token_jwt.get('sub', 0))

    try:

        # ニコニコアカウントのユーザー情報を取得
        ## 3秒応答がなかったらタイムアウト
        user_api_url = f'https://nvapi.nicovideo.jp/v1/users/{current_user.niconico_user_id}'
        async with HTTPX_CLIENT() as httpx_client:
            # X-Frontend-Id がないと INVALID_PARAMETER になる
            user_api_response = await httpx_client.get(user_api_url, headers={**API_REQUEST_HEADERS, 'X-Frontend-Id': '6'})

        # ステータスコードが 200 以外
        if user_api_response.status_code != 200:
            logging.error(f'[NiconicoRouter][NiconicoAuthCallbackAPI] Failed to get user information. (HTTP Error {user_api_response.status_code})')
            return OAuthCallbackResponse(
                status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail = f'Failed to get user information (HTTP Error {user_api_response.status_code})',
                redirect_to = redirect_url,
            )

        # ユーザー名
        current_user.niconico_user_name = str(user_api_response.json()['data']['user']['nickname'])
        # プレミアム会員かどうか
        current_user.niconico_user_premium = bool(user_api_response.json()['data']['user']['isPremium'])

    # 接続エラー（サーバー再起動やタイムアウトなど）
    except (httpx.NetworkError, httpx.TimeoutException):
        logging.error('[NiconicoRouter][NiconicoAuthCallbackAPI] Failed to get user information. (Connection Timeout)')
        return OAuthCallbackResponse(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail = 'Failed to get user information (Connection Timeout)',
            redirect_to = redirect_url,
        )

    # 変更をデータベースに保存
    await current_user.save()

    # OAuth 連携が正常に完了したことを伝える
    return OAuthCallbackResponse(
        status_code = status.HTTP_200_OK,
        detail = 'Success',
        redirect_to = redirect_url,
    )


@router.delete(
    '/logout',
    summary = 'ニコニコアカウント連携解除 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def NiconicoAccountLogoutAPI(
    current_user: Annotated[User, Depends(GetCurrentUser)],
):
    """
    現在ログイン中のユーザーアカウントに紐づくニコニコアカウントとの連携を解除する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """

    # ニコニコ関連のフィールドをすべて None (null) にすることで連携解除とする
    current_user.niconico_user_id = None
    current_user.niconico_user_name = None
    current_user.niconico_user_premium = None
    current_user.niconico_access_token = None
    current_user.niconico_refresh_token = None
    await current_user.save()
