
import base64
import json
import re
from enum import StrEnum
from typing import Annotated
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, RedirectResponse
from jose import jwt

from app import logging, schemas
from app.config import Config
from app.constants import API_REQUEST_HEADERS, HTTPX_CLIENT, NICONICO_OAUTH_CLIENT_ID
from app.models.NiconicoOAuthState import NiconicoOAuthState
from app.models.User import User
from app.routers.JikkyoDependency import EnsureJikkyoEnabled
from app.routers.UsersRouter import GetCurrentUser
from app.utils import Interlaced


# ルーター
router = APIRouter(
    tags = ['Niconico'],
    prefix = '/api/niconico',
)


OAUTH_CALLBACK_RESPONSE_HEADERS = {
    'Cache-Control': 'no-store',
    'Content-Security-Policy': "default-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    'Referrer-Policy': 'no-referrer',
    'X-Content-Type-Options': 'nosniff',
}


class NiconicoOAuthResult(StrEnum):
    """クライアントへ通知するニコニコ OAuth 連携結果。"""

    Success = 'Success'
    AccessDenied = 'AccessDenied'
    AuthorizationError = 'AuthorizationError'
    AuthorizationCodeMissing = 'AuthorizationCodeMissing'
    UserNotFound = 'UserNotFound'
    TokenAPIError = 'TokenAPIError'
    TokenAPITimeout = 'TokenAPITimeout'
    UserAPIError = 'UserAPIError'
    UserAPITimeout = 'UserAPITimeout'


def _NormalizeClientOrigin(client_origin: str) -> str:
    """
    クライアント Origin を、パスなどを含まない HTTPS Origin へ正規化する。

    Args:
        client_origin: Origin ヘッダーまたはサーバー側 state に保存されたクライアント Origin。

    Returns:
        正規化済みの HTTPS Origin。

    Raises:
        ValueError: Origin として不正、または HTTPS 以外の URL が渡された場合。
    """

    try:
        parsed_origin = urlsplit(client_origin)
        port = parsed_origin.port
    except ValueError as ex:
        raise ValueError('Client Origin is invalid') from ex

    # OAuth 結果の転送先は Secure Context である KonomiTV クライアントの Origin だけに限定する。
    # ユーザー情報・パス・クエリ・フラグメントを許すと、資格情報の誤送信や任意パスへの転送につながる。
    hostname = parsed_origin.hostname
    if (
        parsed_origin.scheme != 'https'
        or hostname is None
        or parsed_origin.username is not None
        or parsed_origin.password is not None
        or parsed_origin.path not in ['', '/']
        or parsed_origin.query != ''
        or parsed_origin.fragment != ''
    ):
        raise ValueError('Client Origin must be an HTTPS Origin without credentials, path, query, or fragment')

    # Origin ヘッダーは ASCII で送信される。許可文字を限定し、空白や JavaScript 構文文字を排除する。
    normalized_hostname = hostname.lower()
    if ':' in normalized_hostname:
        if re.fullmatch(r'[0-9a-f:.]+', normalized_hostname) is None:
            raise ValueError('Client Origin contains an invalid IPv6 hostname')
        normalized_host = f'[{normalized_hostname}]'
    else:
        if re.fullmatch(r'[a-z0-9.-]+', normalized_hostname) is None:
            raise ValueError('Client Origin contains an invalid hostname')
        normalized_host = normalized_hostname

    # HTTPS の既定ポートは location.origin と同じ表現へそろえ、それ以外の明示ポートだけを保持する。
    if port is not None and port != 443:
        normalized_host += f':{port}'

    return f'https://{normalized_host}'


def _BuildSettingsJikkyoRedirectUrl(client_origin: str, result: NiconicoOAuthResult) -> str:
    """
    クライアント Origin から、固定形式の OAuth 結果を持つ実況設定画面 URL を組み立てる。

    Args:
        client_origin: 正規化済みのクライアント Origin。
        result: クライアントへ通知する固定の OAuth 連携結果。

    Returns:
        実況設定画面へのリダイレクト URL。
    """

    return f'{client_origin}/settings/jikkyo#{urlencode({"result": result.value})}'


def _CreateOAuthCallbackErrorResponse(detail: str, status_code: int) -> PlainTextResponse:
    """
    信頼できるクライアント Origin がない場合の、スクリプトを含まない固定エラー応答を作成する。

    Args:
        detail: サーバー側で定義した固定エラーメッセージ。
        status_code: 応答する HTTP ステータスコード。

    Returns:
        キャッシュと外部リソース読み込みを禁止したプレーンテキスト応答。
    """

    return PlainTextResponse(
        content = detail,
        status_code = status_code,
        headers = OAUTH_CALLBACK_RESPONSE_HEADERS,
    )


def _CreateOAuthCallbackRedirectResponse(
    client_origin: str,
    result: NiconicoOAuthResult,
) -> RedirectResponse:
    """
    state に保存されたクライアント Origin へ OAuth 結果を返すリダイレクト応答を作成する。

    Args:
        client_origin: 正規化済みのクライアント Origin。
        result: クライアントへ通知する固定の OAuth 連携結果。

    Returns:
        クライアントの実況設定画面へ遷移する 303 応答。
    """

    return RedirectResponse(
        url = _BuildSettingsJikkyoRedirectUrl(client_origin, result),
        status_code = status.HTTP_303_SEE_OTHER,
        headers = OAUTH_CALLBACK_RESPONSE_HEADERS,
    )


@router.get(
    '/auth',
    summary = 'ニコニコ OAuth 認証 URL 発行 API',
    response_model = schemas.ThirdpartyAuthURL,
    response_description = 'ユーザーにアプリ連携してもらうための認証 URL。',
    dependencies = [Depends(EnsureJikkyoEnabled)],
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
    try:
        client_origin = _NormalizeClientOrigin(
            request.headers.get('Origin', f'https://{request.url.netloc}')
        )
    except ValueError as ex:
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail = 'Client Origin is invalid',
        ) from ex

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
        client_url = client_origin,
    )

    # コールバック後の NiconicoAuthCallbackAPI に渡す state の値
    ## client は app.konomi.tv の従来リレー契約を維持するためだけに残し、サーバーは DB 上の client_url だけを正本として使う
    state = {
        # リダイレクト先の KonomiTV サーバー
        'server': f'https://{request.url.netloc}/',
        # 従来リレーとの互換用クライアント Origin（callback 側では使用しない）
        'client': client_origin,
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
    response_class = RedirectResponse,
    response_description = 'OAuth 連携結果を KonomiTV クライアントへ通知するリダイレクト。',
)
async def NiconicoAuthCallbackAPI(
    oauth_state: Annotated[str | None, Query(description='サーバ発行の OAuth state ID。ログイン JWT ではない。')] = None,
    client: Annotated[str | None, Query(description='互換のため残す。リダイレクト先には使用しない。', include_in_schema=False)] = None,
    code: Annotated[str | None, Query(description='コールバック元から渡された認証コード。OAuth 認証が成功したときのみセットされる。')] = None,
    error: Annotated[str | None, Query(description='このパラメーターがセットされているとき、OAuth 認証がユーザーによって拒否されたことを示す。')] = None,
    # 旧実装が state に載せていたログイン JWT。受理してユーザー認証に使わない（SEC-001）。
    user_access_token: Annotated[str | None, Query(description='互換のため残す。無視される。', include_in_schema=False)] = None,
):
    """
    ニコニコの OAuth 認証のコールバックを受け取り、ログイン中のユーザーアカウントとニコニコアカウントを紐づける。
    """

    # 旧リレーが転送する client と user_access_token は受理だけして、認証・リダイレクトには一切使わない。
    del client, user_access_token

    # 実況無効時は state DB や外部 API に触れず、最初に固定の 403 を返す。
    if Config().general.jikkyo_enabled is False:
        return _CreateOAuthCallbackErrorResponse(
            detail = 'Jikkyo is disabled by server settings',
            status_code = status.HTTP_403_FORBIDDEN,
        )

    # state は OAuth の成功・拒否にかかわらず、callback へ到達した時点で一度だけ消費する。
    pending_state: NiconicoOAuthState | None = None
    if oauth_state is not None:
        try:
            pending_state = await NiconicoOAuthState.consume(oauth_state)
        except ValueError:
            pass

    if oauth_state is None:
        logging.warning('[NiconicoRouter][NiconicoAuthCallbackAPI] OAuth state is missing.')
        return _CreateOAuthCallbackErrorResponse(
            detail = 'OAuth state is missing',
            status_code = status.HTTP_401_UNAUTHORIZED,
        )

    if pending_state is None:
        logging.warning('[NiconicoRouter][NiconicoAuthCallbackAPI] OAuth state is invalid or expired.')
        return _CreateOAuthCallbackErrorResponse(
            detail = 'OAuth state is invalid or expired',
            status_code = status.HTTP_401_UNAUTHORIZED,
        )

    # DB が手動変更されていても不正 URL へ転送しないよう、保存済み Origin を callback 側でも再検証する。
    try:
        client_origin = _NormalizeClientOrigin(pending_state.client_url)
    except ValueError:
        logging.error('[NiconicoRouter][NiconicoAuthCallbackAPI] Stored client Origin is invalid.')
        return _CreateOAuthCallbackErrorResponse(
            detail = 'Stored client Origin is invalid',
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # "error" パラメーターがセットされている
    # OAuth 認証がユーザーによって拒否されたことを示しているので、401 エラーにする
    if error is not None:

        # 外部から渡された error は本文やログへ反映せず、既知値だけを固定結果へ変換する。
        oauth_result = (
            NiconicoOAuthResult.AccessDenied
            if error == 'access_denied'
            else NiconicoOAuthResult.AuthorizationError
        )
        logging.warning(
            f'[NiconicoRouter][NiconicoAuthCallbackAPI] Authorization failed. [result: {oauth_result.value}]'
        )
        return _CreateOAuthCallbackRedirectResponse(client_origin, oauth_result)

    # なぜか code がない
    if code is None:
        logging.error('[NiconicoRouter][NiconicoAuthCallbackAPI] Authorization code does not exist.')
        return _CreateOAuthCallbackRedirectResponse(
            client_origin,
            NiconicoOAuthResult.AuthorizationCodeMissing,
        )

    current_user = await User.filter(id=pending_state.user_id).get_or_none()
    if current_user is None:
        logging.warning(
            f'[NiconicoRouter][NiconicoAuthCallbackAPI] User for OAuth state does not exist. '
            f'[user_id: {pending_state.user_id}]'
        )
        return _CreateOAuthCallbackRedirectResponse(
            client_origin,
            NiconicoOAuthResult.UserNotFound,
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
            return _CreateOAuthCallbackRedirectResponse(
                client_origin,
                NiconicoOAuthResult.TokenAPIError,
            )

        token_api_response_json = token_api_response.json()

    # 接続エラー（サーバーメンテナンスやタイムアウトなど）
    except (httpx.NetworkError, httpx.TimeoutException):
        logging.error('[NiconicoRouter][NiconicoAuthCallbackAPI] Failed to get access token. (Connection Timeout)')
        return _CreateOAuthCallbackRedirectResponse(
            client_origin,
            NiconicoOAuthResult.TokenAPITimeout,
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
            return _CreateOAuthCallbackRedirectResponse(
                client_origin,
                NiconicoOAuthResult.UserAPIError,
            )

        # ユーザー名
        current_user.niconico_user_name = str(user_api_response.json()['data']['user']['nickname'])
        # プレミアム会員かどうか
        current_user.niconico_user_premium = bool(user_api_response.json()['data']['user']['isPremium'])

    # 接続エラー（サーバー再起動やタイムアウトなど）
    except (httpx.NetworkError, httpx.TimeoutException):
        logging.error('[NiconicoRouter][NiconicoAuthCallbackAPI] Failed to get user information. (Connection Timeout)')
        return _CreateOAuthCallbackRedirectResponse(
            client_origin,
            NiconicoOAuthResult.UserAPITimeout,
        )

    # 変更をデータベースに保存
    await current_user.save()

    # OAuth 連携が正常に完了したことを伝える
    return _CreateOAuthCallbackRedirectResponse(
        client_origin,
        NiconicoOAuthResult.Success,
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
