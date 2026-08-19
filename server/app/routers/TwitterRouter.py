
import asyncio
import ipaddress
import socket
import ssl
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, cast
from urllib.parse import urlsplit

import httpcore
import httpx
from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from fastapi.security.utils import get_authorization_scheme_param

from app import logging, schemas
from app.constants import API_REQUEST_HEADERS
from app.models.TwitterAccount import TwitterAccount
from app.models.User import User
from app.routers.UsersRouter import GetCurrentUser
from app.utils.DataBroadcastingHTTPClient import PinnedNetworkBackend
from app.utils.TwitterGraphQLAPI import TwitterGraphQLAPI
from app.utils.TwitterScrapeBrowser import TwitterScrapeBrowser


# ルーター
router = APIRouter(
    tags = ['Twitter'],
    prefix = '/api/twitter',
)

# Twitter 動画プロキシで許可する CDN ホスト名。
# 完全一致のみを許可し、サブドメインの追加や外部ドメインへの redirect を拒否する。
ALLOWED_VIDEO_PROXY_DOMAINS = frozenset({
    'video.twimg.com',
    'pbs.twimg.com',
})

# 自動 redirect を無効化したうえで、手動で追う hop 数の上限。
# redirect loop と、許可 CDN 外への連鎖転送をここで打ち切る。
MAX_VIDEO_PROXY_REDIRECTS = 3

# 1 リクエストあたり上流から転送してよい最大バイト数。
# Range 再生は維持しつつ、未認証に近い帯域悪用や巨大応答の中継を防ぐ。
MAX_VIDEO_PROXY_BYTES = 100 * 1024 * 1024

# 上流接続・読み取りのタイムアウト (秒)。
VIDEO_PROXY_TIMEOUT_SECONDS = 30.0

# `<video>` が Authorization ヘッダを送れない場合に使う、path 限定の認証 Cookie 名。
TWITTER_VIDEO_PROXY_ACCESS_TOKEN_COOKIE = 'KonomiTV-TwitterVideoAccessToken'


class TwitterVideoProxyURLRejected(ValueError):
    """Twitter 動画プロキシの接続先ポリシーで拒否された URL。"""


class TwitterVideoProxyLimitExceeded(ValueError):
    """Twitter 動画プロキシの転送量上限を超えた。"""


@dataclass(frozen=True, slots=True)
class ResolvedTwitterVideoProxyURL:
    """接続先検証済みの Twitter CDN URL と、接続時に再解決しない IP 一覧。"""

    url: httpx.URL
    host: str
    port: int
    addresses: tuple[str, ...]


class _ClosableAsyncByteStream(Protocol):
    """httpcore 応答 stream のうち、この transport が必要とする操作。"""

    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


class _TwitterVideoProxyResponseStream(httpx.AsyncByteStream):
    """httpcore の応答 stream を httpx の transport 応答へ橋渡しする。"""

    def __init__(self, stream: _ClosableAsyncByteStream) -> None:
        """
        Args:
            stream: httpcore が返した非同期応答 stream。
        """

        self.stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.stream:
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


class _TwitterVideoProxyNetworkBackend(httpcore.AsyncNetworkBackend):
    """hop ごとに検証した CDN ホストと IP の組だけへ接続する backend。"""

    def __init__(self) -> None:
        # redirect の各 hop で検証済みになったホストに対応する pinned backend。
        self.backends: dict[str, PinnedNetworkBackend] = {}

    def setAllowedAddresses(self, host: str, addresses: tuple[str, ...]) -> None:
        """host の接続先を、その hop で検証した公開 IP 一覧へ固定する。"""

        self.backends[host] = PinnedNetworkBackend(host, addresses)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        backend = self.backends.get(host)
        if backend is None:
            raise httpcore.ConnectError('Upstream host was not pinned before connection')
        return await backend.connect_tcp(host, port, timeout, local_address, socket_options)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Twitter CDN への接続で Unix domain socket は許可しない。"""

        raise httpcore.ConnectError('Unix socket upstreams are not allowed')

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class _TwitterVideoProxyTransport(httpx.AsyncBaseTransport):
    """検証済み IP だけへ接続する Twitter 動画用 httpx transport。"""

    def __init__(self, network_backend: _TwitterVideoProxyNetworkBackend) -> None:
        """
        Args:
            network_backend: hop ごとの検証済み IP を保持する backend。
        """

        # Twitter CDN は通常の公開証明書を使うため、OS の信頼ストアで厳格に検証する。
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=1,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            network_backend=network_backend,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not isinstance(request.stream, httpx.AsyncByteStream):
            raise TypeError('Twitter video proxy request stream must be asynchronous')

        core_response = await self.pool.handle_async_request(httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        ))
        response_stream = cast(_ClosableAsyncByteStream, core_response.stream)
        return httpx.Response(
            status_code=core_response.status,
            headers=core_response.headers,
            stream=_TwitterVideoProxyResponseStream(response_stream),
            extensions=core_response.extensions,
        )

    async def aclose(self) -> None:
        await self.pool.aclose()


def _IsAllowedGlobalAddress(address: str) -> bool:
    """外部インターネット向け接続先として許可できる IP か判定する。"""

    try:
        parsed_address = ipaddress.ip_address(address)
    except ValueError:
        return False

    # IPv4-mapped IPv6 は内側の IPv4 へ戻し、::ffff:127.0.0.1 などを弾く。
    if isinstance(parsed_address, ipaddress.IPv6Address) and parsed_address.ipv4_mapped is not None:
        parsed_address = parsed_address.ipv4_mapped

    return (
        parsed_address.is_global
        and not parsed_address.is_private
        and not parsed_address.is_loopback
        and not parsed_address.is_link_local
        and not parsed_address.is_reserved
        and not parsed_address.is_multicast
        and not parsed_address.is_unspecified
    )


async def _ResolveAllowedAddresses(host: str, port: int) -> tuple[str, ...]:
    """
    host を一度だけ解決し、全結果がグローバル IP であることを確認する。

    Args:
        host: 接続先ホスト名。
        port: 接続先ポート。

    Returns:
        検証済みの公開 IP 一覧。

    Raises:
        TwitterVideoProxyURLRejected: 解決失敗、または非公開 IP を含む場合。
    """

    loop = asyncio.get_running_loop()
    try:
        address_records = await loop.getaddrinfo(
            host,
            port,
            family = socket.AF_UNSPEC,
            type = socket.SOCK_STREAM,
        )
    except socket.gaierror as ex:
        raise TwitterVideoProxyURLRejected('Upstream host could not be resolved') from ex

    resolved_addresses: list[str] = []
    for _family, _socktype, _protocol, _canonname, sockaddr in address_records:
        address = sockaddr[0]
        if isinstance(address, str):
            resolved_addresses.append(address)

    addresses = tuple(dict.fromkeys(resolved_addresses))
    if not addresses or any(not _IsAllowedGlobalAddress(address) for address in addresses):
        raise TwitterVideoProxyURLRejected('Upstream host resolved to a non-global address')
    return addresses


async def ValidateTwitterVideoProxyURL(url: str) -> ResolvedTwitterVideoProxyURL:
    """
    Twitter 動画プロキシで許可する URL か検証し、接続先 IP を固定して返す。

    scheme・hostname・認証情報・制御文字を検査したうえで DNS を解決し、
    解決結果に private / loopback / link-local が含まれる場合は拒否する。

    Args:
        url: 初期 URL、または redirect 先 URL。

    Returns:
        正規化済み URL と検証済み公開 IP 一覧。

    Raises:
        TwitterVideoProxyURLRejected: ポリシー違反の URL。
    """

    # CRLF や NUL を含む値はヘッダ注入やパーサ差分の温床になるため先に拒否する。
    if any(control_character in url for control_character in ('\r', '\n', '\x00')):
        raise TwitterVideoProxyURLRejected('Upstream URL contains a control character')

    try:
        parsed_url = httpx.URL(url)
        parsed_parts = urlsplit(url)
        parsed_port = parsed_url.port
    except (httpx.InvalidURL, ValueError) as ex:
        raise TwitterVideoProxyURLRejected('Upstream URL is invalid') from ex

    if parsed_url.scheme != 'https':
        raise TwitterVideoProxyURLRejected('Upstream URL scheme must be https')
    if not parsed_url.host:
        raise TwitterVideoProxyURLRejected('Upstream URL host is missing')
    # userinfo 付き URL は認証情報の持ち込みとパーサ差分を避けるため拒否する。
    if '@' in parsed_parts.netloc or parsed_url.userinfo != b'':
        raise TwitterVideoProxyURLRejected('Upstream URL credentials are not allowed')
    if parsed_url.fragment:
        raise TwitterVideoProxyURLRejected('Upstream URL fragments are not allowed')
    if parsed_url.host not in ALLOWED_VIDEO_PROXY_DOMAINS:
        raise TwitterVideoProxyURLRejected('Upstream URL domain is not allowed')
    if parsed_port not in (None, 443):
        raise TwitterVideoProxyURLRejected('Upstream URL port is not allowed')

    port = parsed_port or 443
    # hop ごとに DNS を再解決し、検証結果を接続時まで保持して再解決を防ぐ。
    addresses = await _ResolveAllowedAddresses(parsed_url.host, port)
    return ResolvedTwitterVideoProxyURL(
        url=parsed_url,
        host=parsed_url.host,
        port=port,
        addresses=addresses,
    )


async def GetCurrentUserForTwitterVideoProxy(
    request: Request,
) -> User:
    """
    Twitter 動画プロキシ用に現在のユーザーを取得する。

    `<video src>` は Authorization ヘッダを送れないため、通常の Bearer に加えて
    path 限定の Secure Cookie も受け付ける。どちらか一方で GetCurrentUser と同じ検証を行う。

    Args:
        request: クライアントからのリクエスト。
    Returns:
        認証済みユーザー。

    Raises:
        HTTPException: トークンが欠落または不正な場合。
    """

    authorization = request.headers.get('Authorization')
    scheme, param = get_authorization_scheme_param(authorization)
    token: str | None = None
    if scheme.lower() == 'bearer' and param != '':
        token = param
    else:
        cookie_token = request.cookies.get(TWITTER_VIDEO_PROXY_ACCESS_TOKEN_COOKIE)
        if cookie_token is not None and cookie_token != '':
            # video 要素は Authorization を送れないため、同一 JWT を path 限定 Cookie で受け取る。
            token = cookie_token

    if token is None:
        logging.warning('[TwitterRouter][GetCurrentUserForTwitterVideoProxy] Access token is missing.')
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail = 'Not authenticated',
            headers = {'WWW-Authenticate': 'Bearer'},
        )

    return await GetCurrentUser(token = token)


async def _IterUpstreamBytesWithLimit(
    upstream_response: httpx.Response,
    max_bytes: int = MAX_VIDEO_PROXY_BYTES,
) -> AsyncIterator[bytes]:
    """
    上流レスポンスをチャンク転送し、累積転送量が上限を超えたら打ち切る。

    Args:
        upstream_response: stream=True で取得した上流レスポンス。
        max_bytes: 許可する最大転送バイト数。

    Yields:
        上流から読み取ったチャンク。

    Raises:
        TwitterVideoProxyLimitExceeded: 累積転送量が上限を超えた場合。
    """

    total_bytes = 0
    async for chunk in upstream_response.aiter_bytes(chunk_size = 65536):
        if not chunk:
            continue
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise TwitterVideoProxyLimitExceeded('Upstream response body exceeds the limit')
        yield chunk


async def _OpenTwitterVideoUpstream(
    request: Request,
    url: str,
) -> tuple[httpx.AsyncClient, httpx.Response]:
    """
    許可 CDN への上流 GET を開き、必要なら redirect を手動で辿る。

    自動 redirect は使わず、各 hop で scheme・hostname・解決 IP を再検証する。
    成功時はストリーミング中の client / response を呼び出し側へ渡し、失敗時は両方閉じる。

    Args:
        request: クライアントからのリクエスト。Range 等の転送対象ヘッダを含む。
        url: 初期の Twitter 動画 URL。

    Returns:
        (httpx.AsyncClient, httpx.Response) の組。response は stream オープン済み。

    Raises:
        TwitterVideoProxyURLRejected: URL ポリシー違反、redirect 過多。
        HTTPException: 上流接続失敗、上流 4xx/5xx、Content-Length 超過。
    """

    # Range など再生に必要なヘッダだけを転送し、Cookie や Authorization は漏らさない。
    proxy_headers: dict[str, str] = {
        'User-Agent': API_REQUEST_HEADERS['User-Agent'],
        # httpx の自動復号で Content-Length / Range と実転送 byte がずれないよう identity に固定する。
        'Accept-Encoding': 'identity',
    }
    allowed_request_headers = {
        'range',
        'accept',
        'if-range',
        'if-none-match',
        'if-modified-since',
    }
    for key, value in request.headers.items():
        if key.lower() in allowed_request_headers:
            proxy_headers[key] = value

    # follow_redirects=False で自動追従を止め、hop ごとの再検証と IP pinning を強制する。
    network_backend = _TwitterVideoProxyNetworkBackend()
    client = httpx.AsyncClient(
        follow_redirects = False,
        timeout = VIDEO_PROXY_TIMEOUT_SECONDS,
        transport = _TwitterVideoProxyTransport(network_backend),
    )
    current_url = url
    upstream_response: httpx.Response | None = None

    try:
        for redirect_count in range(MAX_VIDEO_PROXY_REDIRECTS + 1):
            # 初期 URL と redirect 先の両方で許可 CDN・公開 IP を確認し、接続先を検証結果へ固定する。
            resolved_url = await ValidateTwitterVideoProxyURL(current_url)
            network_backend.setAllowedAddresses(resolved_url.host, resolved_url.addresses)

            try:
                upstream_request = client.build_request('GET', resolved_url.url, headers = proxy_headers)
                upstream_response = await client.send(upstream_request, stream = True)
            except Exception as ex:
                logging.error('[TwitterRouter][_OpenTwitterVideoUpstream] Failed to request upstream:', exc_info = ex)
                raise HTTPException(
                    status_code = status.HTTP_502_BAD_GATEWAY,
                    detail = 'Failed to request upstream resource',
                ) from ex

            # redirect 系は Location を取り出し、本文は読まずに次 hop へ進む。
            if upstream_response.status_code in {301, 302, 303, 307, 308}:
                location = upstream_response.headers.get('location')
                await upstream_response.aclose()
                upstream_response = None
                if location is None or location == '':
                    raise TwitterVideoProxyURLRejected('Redirect response is missing Location')
                if redirect_count >= MAX_VIDEO_PROXY_REDIRECTS:
                    raise TwitterVideoProxyURLRejected('Too many upstream redirects')
                # 相対 Location も現在 URL 基準で解決し、その結果を次 hop で再検証する。
                current_url = str(resolved_url.url.join(location))
                continue

            # 上流エラー本文は攻撃者が任意サイズにできるため読み込まず、status だけを記録する。
            if upstream_response.status_code >= 400:
                upstream_status_code = upstream_response.status_code
                await upstream_response.aclose()
                upstream_response = None
                logging.error(
                    f'[TwitterRouter][_OpenTwitterVideoUpstream] Upstream returned HTTP {upstream_status_code}.',
                )
                raise HTTPException(
                    status_code = status.HTTP_502_BAD_GATEWAY,
                    detail = 'Upstream returned an error response',
                )

            # Content-Length が分かる場合はストリーム開始前に拒否し、巨大応答の中継を防ぐ。
            content_length_header = upstream_response.headers.get('content-length')
            if content_length_header is not None:
                try:
                    content_length = int(content_length_header.strip())
                except ValueError:
                    content_length = MAX_VIDEO_PROXY_BYTES + 1
                # 304 の Content-Length は本文ではなく選択された表現のサイズなので上限判定に使わない。
                if upstream_response.status_code != 304 and content_length > MAX_VIDEO_PROXY_BYTES:
                    await upstream_response.aclose()
                    upstream_response = None
                    raise TwitterVideoProxyLimitExceeded('Upstream response Content-Length exceeds the limit')

            return client, upstream_response

        raise TwitterVideoProxyURLRejected('Too many upstream redirects')
    except Exception:
        # 例外経路では必ず接続を閉じ、呼び出し側へリークさせない。
        if upstream_response is not None:
            await upstream_response.aclose()
        await client.aclose()
        raise


def ParseAcceptLanguageHeader(accept_language: str | None) -> list[str]:
    """
    Accept-Language ヘッダーを CDP に渡しやすい言語タグ配列へ変換する

    Args:
        accept_language (str | None): HTTP リクエストの Accept-Language ヘッダー

    Returns:
        list[str]: q 値などの重みを除去した言語タグ配列
    """

    # CDP の acceptLanguage は q 値付き文字列を渡すと Chrome 側で q 値が二重化するため、
    # 保存時点でヘッダーの生値とは別に q 値を除いた配列を持っておく
    if accept_language is None:
        return []

    language_tags: list[str] = []
    for language_part in accept_language.split(','):
        # `ja-JP;q=0.9` のような重み部分は CDP へ渡す値には不要なので、先頭の言語タグだけを使う
        language_tag = language_part.strip().split(';', maxsplit=1)[0].strip()
        if language_tag != '':
            language_tags.append(language_tag)
    return language_tags


def BuildCookieBrowserInfo(
    request: Request,
    browser_info: schemas.BrowserEnvironmentInfoRequest | None,
) -> schemas.BrowserEnvironmentInfo | None:
    """
    Cookie 認証 API の HTTP リクエストヘッダーと、クライアント側で採取した情報から、
    TwitterAccount.cookie_browser_info に永続化するヘッドレスブラウザ向けの環境情報を組み立てる

    Args:
        request (Request): Twitter Cookie 認証 API のリクエスト
        browser_info (schemas.BrowserEnvironmentInfoRequest | None): クライアント JavaScript で採取した環境情報

    Returns:
        schemas.BrowserEnvironmentInfo | None: 永続化するヘッドレスブラウザ向けの環境情報
    """

    # UA-CH 高エントロピー値が取れないブラウザでは補正に必要な OS 情報が不足しているため諦める
    if browser_info is None:
        return None

    # Accept-Language は navigator.languages より実際の HTTP リクエストヘッダーから解析した方が正確なので、サーバー側で直接採取する
    ## sec-ch-ua 系の低エントロピー値も同じ HTTP リクエストヘッダーから保存しておき、後で実ブラウザ側の送信値と比較できるようにする
    accept_language = request.headers.get('accept-language')
    cookie_browser_info = schemas.BrowserEnvironmentInfo(
        http_headers=schemas.BrowserEnvironmentHTTPHeaders(
            user_agent=request.headers.get('user-agent'),
            accept_language=accept_language,
            accept_languages=ParseAcceptLanguageHeader(accept_language),
            sec_ch_ua=request.headers.get('sec-ch-ua'),
            sec_ch_ua_mobile=request.headers.get('sec-ch-ua-mobile'),
            sec_ch_ua_platform=request.headers.get('sec-ch-ua-platform'),
        ),
        user_agent_data=browser_info.user_agent_data,
        navigator_platform=browser_info.navigator_platform,
        locale=browser_info.locale,
        timezone=browser_info.timezone,
    )
    return cookie_browser_info


async def GetCurrentTwitterAccount(
    screen_name: Annotated[str, Path(description='Twitter アカウントのスクリーンネーム。')],
    current_user: Annotated[User, Depends(GetCurrentUser)],
) -> TwitterAccount:
    """ 現在ログイン中のユーザーに紐づく Twitter アカウントを取得する """

    # 指定されたスクリーンネームに紐づく Twitter アカウントを取得
    # 自分が所有していない Twitter アカウントでツイートできないよう、ログイン中のユーザーに限って絞り込む
    ## 通常あり得ないが、万が一同一スクリーンネームのアカウントが作成されてしまった場合に削除できるよう
    ## あえて get_or_none() ではなく all() で取得している
    twitter_account = await TwitterAccount.filter(user_id=current_user.id, screen_name=screen_name).all()

    # 指定された Twitter アカウントがユーザーアカウントに紐付けられていない or 登録されていない
    ## 実際に Twitter にそのスクリーンネームのアカウントが登録されているかとは無関係
    if len(twitter_account) == 0:
        logging.error(f'[TwitterRouter][GetCurrentTwitterAccount] TwitterAccount associated with screen_name does not exist. [screen_name: {screen_name}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'TwitterAccount associated with screen_name does not exist',
        )

    # 古い形式のレコード (access_token が "NETSCAPE_COOKIE_FILE" でない) は利用できない
    # これが検出された場合、その場で当該レコードを削除する
    ## 重複レコードの可能性もなくもないため、リスト全体をイテレートしてチェックする
    is_removed = False
    valid_accounts: list[TwitterAccount] = []
    for record in twitter_account:
        if record.access_token != 'NETSCAPE_COOKIE_FILE':
            logging.error(f'[TwitterRouter][GetCurrentTwitterAccount] Old cookie format or OAuth session is no longer available. [screen_name: {record.screen_name}, id: {record.id}]')
            await record.delete()
            is_removed = True
        else:
            valid_accounts.append(record)

    # 古い形式のレコードが削除された場合、エラーを返す
    if is_removed is True:
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Old cookie format or OAuth session is no longer available',
        )

    # 有効なアカウントが存在しない場合
    if len(valid_accounts) == 0:
        logging.error(f'[TwitterRouter][GetCurrentTwitterAccount] No valid TwitterAccount found after cleanup. [screen_name: {screen_name}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'TwitterAccount associated with screen_name does not exist',
        )

    return valid_accounts[0]


@router.post(
    '/auth',
    summary = 'Twitter 認証 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def TwitterCookieAuthAPI(
    request: Request,
    auth_request: Annotated[schemas.TwitterCookieAuthRequest, Body(description='Twitter 認証リクエスト')],
    current_user: Annotated[User, Depends(GetCurrentUser)],
):
    """
    指定された Cookie 情報 (Netscape 形式) で Twitter 連携を行い、ログイン中のユーザーアカウントと Twitter アカウントを紐づける。
    """

    # cookies.txt (Netscape 形式) をパースして Cookie が取得できるかを試す
    # パースしたデータ自体は使われないが、正しいフォーマットかを検証するために必須
    try:
        cookie_params = TwitterScrapeBrowser.parseNetscapeCookieFile(auth_request.cookies_txt)
        # Twitter 関連の Cookie が存在するかを確認
        ## CookieParam の domain に 'x.com' が含まれているかチェック
        twitter_cookies = [
            param for param in cookie_params
            if param.domain is not None and 'x.com' in param.domain
        ]
    except Exception as ex:
        error_message = f'Failed to parse cookies.txt: {ex!s}'
        logging.error(f'[TwitterRouter][TwitterCookieAuthAPI] {error_message}')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = error_message,
        ) from ex
    if len(twitter_cookies) == 0:
        logging.error('[TwitterRouter][TwitterCookieAuthAPI] No valid cookies found in the provided cookies.txt.')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'No valid cookies found in the provided cookies.txt',
        )

    # Cookie 認証 API の HTTP リクエストヘッダーと、クライアント JavaScript で採取した情報から、
    # TwitterAccount.cookie_browser_info に永続化するヘッドレスブラウザ向けの環境情報を組み立てる
    cookie_browser_info = BuildCookieBrowserInfo(request, auth_request.browser_info)

    # TwitterAccount のレコードを作成
    ## アクセストークンは "NETSCAPE_COOKIE_FILE" の固定値、
    ## アクセストークンシークレットとして Netscape 形式の Cookie ファイルの内容をそのまま保存する
    ## ここでは ORM インスタンスのみを作成するが、認証中に更新 Cookie を同期するため先に DB 保存される場合がある
    ## その場合もプロフィール確定後に明示登録するまでは、TwitterGraphQLAPI の共有レジストリへ登録しない
    twitter_account = TwitterAccount(
        user = current_user,
        name = 'Temporary',
        screen_name = 'Temporary',
        icon_url = 'Temporary',
        # Netscape Cookie ファイル形式の場合は "NETSCAPE_COOKIE_FILE" で固定
        access_token = 'NETSCAPE_COOKIE_FILE',
        # Netscape 形式の Cookie ファイルの内容をそのまま保存
        access_token_secret = auth_request.cookies_txt,
        # Cookie 採取元ブラウザの環境情報
        cookie_browser_info = cookie_browser_info,
    )

    # 未保存アカウント用の API インスタンスは共有レジストリへ登録せず、この認証リクエストだけで使用する
    twitter_api = TwitterGraphQLAPI(twitter_account)

    # 上記で作成した TwitterAccount ORM インスタンスを使い、現在ログイン中の Twitter アカウント情報を取得
    try:
        viewer_result = await twitter_api.fetchLoggedViewer()
    except Exception as ex:
        # 認証失敗後に一時 browser と Cookie がメモリ上へ残り続けないよう即座に回収する
        await twitter_api.shutdown()
        logging.error('[TwitterRouter][TwitterCookieAuthAPI] Failed to get user information:', exc_info=ex)
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail = 'Failed to get user information',
        ) from ex

    # ユーザー情報の取得に失敗した場合
    if isinstance(viewer_result, schemas.TwitterAPIResult) and viewer_result.is_success is False:
        await twitter_api.shutdown()
        logging.error(f'[TwitterRouter][TwitterCookieAuthAPI] Failed to get user information: {viewer_result.detail}')
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail = viewer_result.detail,  # エラーメッセージをそのまま返す
        )

    # viewer_result が TweetUser の場合のみ処理を続行
    if not isinstance(viewer_result, schemas.TweetUser):
        await twitter_api.shutdown()
        logging.error('[TwitterRouter][TwitterCookieAuthAPI] Failed to get user information: Invalid response type')
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail = 'Failed to get user information',
        )

    # アカウント名を設定
    twitter_account.name = viewer_result.name
    # スクリーンネームを設定
    twitter_account.screen_name = viewer_result.screen_name
    # アイコン URL を設定
    twitter_account.icon_url = viewer_result.icon_url
    # Cookie を暗号化して保持
    twitter_account.access_token_secret = twitter_account.encryptAccessTokenSecret(auth_request.cookies_txt)

    # 同じユーザー ID とスクリーンネームを持つアカウント情報の重複チェック
    existing_accounts = await TwitterAccount.filter(
        user_id = twitter_account.user_id,
        screen_name = twitter_account.screen_name,
    )

    # 永続化後に使う TwitterAccount を示す変数
    persisted_account: TwitterAccount | None = None

    # 既存のアカウントが見つかった場合、最も古いアカウント情報を更新
    if existing_accounts:
        oldest_account = min(existing_accounts, key=lambda x: x.id)

        # 最も古いアカウント情報を更新
        oldest_account.name = twitter_account.name  # アカウント名
        oldest_account.icon_url = twitter_account.icon_url  # アイコン URL
        oldest_account.access_token = twitter_account.access_token  # アクセストークン
        oldest_account.access_token_secret = twitter_account.access_token_secret  # アクセストークンシークレット
        oldest_account.cookie_browser_info = twitter_account.cookie_browser_info  # Cookie 採取元ブラウザ情報
        await oldest_account.save()

        # 他の重複アカウントを削除
        for account in existing_accounts:
            if account.id != oldest_account.id:
                await account.delete()
                logging.info(f'[TwitterRouter][TwitterCookieAuthAPI] Deleted duplicate account. [id: {account.id}, screen_name: {account.screen_name}]')

        logging.info(f'[TwitterRouter][TwitterCookieAuthAPI] Updated existing account. [id: {twitter_account.id}, screen_name: {twitter_account.screen_name}]')

        # 永続化後に使う TwitterAccount を設定
        persisted_account = oldest_account

    # 既存のアカウントが見つからなかった場合、新しいアカウント情報を DB に保存
    # ここで twitter_account.id が新規に auto-increment で自動採番される
    else:
        await twitter_account.save()
        logging.info(f'[TwitterRouter][TwitterCookieAuthAPI] Created new account. [id: {twitter_account.id}, screen_name: {twitter_account.screen_name}]')

        # 永続化後に使う TwitterAccount を設定
        persisted_account = twitter_account

    # 認証専用で立ち上げた GraphQL API インスタンスを、永続化後に初めて実 ID で登録する
    if persisted_account is not None:
        await TwitterGraphQLAPI.registerInstance(twitter_api, persisted_account)
        twitter_account = persisted_account

    # 古い形式のレコード (access_token が "NETSCAPE_COOKIE_FILE" でない) を自動削除
    ## これにより、古い OAuth 認証や旧 Cookie 形式のレコードが処理に用いられないようにする
    deleted_count = await TwitterAccount.filter(
        user_id = current_user.id,
    ).exclude(access_token = 'NETSCAPE_COOKIE_FILE').delete()
    if deleted_count > 0:
        logging.info(f'[TwitterRouter][TwitterCookieAuthAPI] Deleted {deleted_count} old format account(s).')

    # 処理完了
    logging.info(f'[TwitterRouter][TwitterCookieAuthAPI] Logged in with cookie. [id: {twitter_account.id}, screen_name: {twitter_account.screen_name}]')


@router.delete(
    '/accounts/{screen_name}',
    summary = 'Twitter アカウント連携解除 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def TwitterAccountDeleteAPI(
    twitter_account: Annotated[TwitterAccount, Depends(GetCurrentTwitterAccount)],
):
    """
    指定された Twitter アカウントの連携を解除する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """

    # Twitter アカウントレコードの ID を取得（削除前に取得する必要がある）
    # この値は Twitter 側のアカウント ID とは異なるので注意
    twitter_account_id = twitter_account.id

    # 指定された Twitter アカウントのレコードを削除
    ## Cookie 情報などが保持されたレコードを削除することで連携解除とする
    await twitter_account.delete()

    # シングルトンインスタンスを削除してリソースリークを防ぐ
    await TwitterGraphQLAPI.removeInstance(twitter_account_id)


@router.post(
    '/accounts/{screen_name}/keep-alive',
    summary = 'Twitter ヘッドレスブラウザ Keep-Alive API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def TwitterKeepAliveAPI(
    twitter_account: Annotated[TwitterAccount, Depends(GetCurrentTwitterAccount)],
):
    """
    この API がユーザーが視聴画面の Twitter パネルで操作を継続している間アクセスされ続けることで、起動中のヘッドレスブラウザの自動シャットダウンを抑制する。<br>
    JWT エンコードされたアクセストークンが Authorization: Bearer に設定されていないとアクセスできない。
    """

    await TwitterGraphQLAPI(twitter_account).keepAlive()


@router.post(
    '/accounts/{screen_name}/tweets',
    summary = 'ツイート送信 API',
    response_description = 'ツイートの送信結果。',
    response_model = schemas.PostTweetResult | schemas.TwitterAPIResult,
)
async def TwitterTweetAPI(
    twitter_account: Annotated[TwitterAccount, Depends(GetCurrentTwitterAccount)],
    tweet: Annotated[str, Form(description='ツイートの本文 (基本的には140文字までだが、プレミアムの加入状態や英数字の量に依存する) 。')] = '',
    images: Annotated[list[UploadFile], File(description='ツイートに添付する画像 (4枚まで) 。')] = [],
    in_reply_to_status_id: Annotated[str | None, Form(description='リプライ先のツイート ID (省略時は単独ツイート) 。')] = None,
):
    """
    Twitter にツイートを送信する。ツイート本文 or 画像のみ送信することもできる。<br>
    ツイートには screen_name で指定したスクリーンネームに紐づく Twitter アカウントが利用される。

    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """

    # 画像が4枚を超えている
    if len(images) > 4:
        logging.error(f'[TwitterRouter][TwitterTweetAPI] Can tweet up to 4 images. [image length: {len(images)}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Can tweet up to 4 images',
        )

    # Twitter Web App 経由でツイートを送信し、結果をそのまま返す
    return await TwitterGraphQLAPI(twitter_account).createTweet(tweet, images, in_reply_to_status_id)


@router.put(
    '/accounts/{screen_name}/tweets/{tweet_id}/retweet',
    summary = 'リツイート実行 API',
    response_description = 'リツイートの実行結果。',
    response_model = schemas.TwitterAPIResult,
)
async def TwitterRetweetAPI(
    twitter_account: Annotated[TwitterAccount, Depends(GetCurrentTwitterAccount)],
    tweet_id: Annotated[str, Path(description='リツイートするツイートの ID。')],
):
    """
    指定されたツイートをリツイートする。<br>
    リツイートには screen_name で指定したスクリーンネームに紐づく Twitter アカウントが利用される。

    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """

    return await TwitterGraphQLAPI(twitter_account).createRetweet(tweet_id)


@router.delete(
    '/accounts/{screen_name}/tweets/{tweet_id}/retweet',
    summary = 'リツイート取り消し API',
    response_description = 'リツイートの取り消し結果。',
    response_model = schemas.TwitterAPIResult,
)
async def TwitterRetweetCancelAPI(
    twitter_account: Annotated[TwitterAccount, Depends(GetCurrentTwitterAccount)],
    tweet_id: Annotated[str, Path(description='リツイートを取り消すツイートの ID。')],
):
    """
    指定されたツイートのリツイートを取り消す。<br>
    リツイートの取り消しには screen_name で指定したスクリーンネームに紐づく Twitter アカウントが利用される。

    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """

    return await TwitterGraphQLAPI(twitter_account).deleteRetweet(tweet_id)


@router.put(
    '/accounts/{screen_name}/tweets/{tweet_id}/favorite',
    summary = 'いいね実行 API',
    response_description = 'いいねの実行結果。',
    response_model = schemas.TwitterAPIResult,
)
async def TwitterFavoriteAPI(
    twitter_account: Annotated[TwitterAccount, Depends(GetCurrentTwitterAccount)],
    tweet_id: Annotated[str, Path(description='いいねするツイートの ID。')],
):
    """
    指定されたツイートをいいねする。<br>
    いいねには screen_name で指定したスクリーンネームに紐づく Twitter アカウントが利用される。

    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """

    return await TwitterGraphQLAPI(twitter_account).favoriteTweet(tweet_id)


@router.delete(
    '/accounts/{screen_name}/tweets/{tweet_id}/favorite',
    summary = 'いいね取り消し API',
    response_description = 'いいねの取り消し結果。',
    response_model = schemas.TwitterAPIResult,
)
async def TwitterFavoriteCancelAPI(
    twitter_account: Annotated[TwitterAccount, Depends(GetCurrentTwitterAccount)],
    tweet_id: Annotated[str, Path(description='いいねを取り消すツイートの ID。')],
):
    """
    指定されたツイートのいいねを取り消す。<br>
    いいねの取り消しには screen_name で指定したスクリーンネームに紐づく Twitter アカウントが利用される。

    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """

    return await TwitterGraphQLAPI(twitter_account).unfavoriteTweet(tweet_id)


@router.get(
    '/accounts/{screen_name}/timeline',
    summary = 'ホームタイムライン取得 API',
    response_description = 'タイムラインのツイートのリスト。',
    response_model = schemas.TimelineTweetsResult | schemas.TwitterAPIResult,
)
async def TwitterTimelineAPI(
    twitter_account: Annotated[TwitterAccount, Depends(GetCurrentTwitterAccount)],
    cursor_id: Annotated[str | None, Query(description='前回のレスポンスから取得した、次のページを取得するためのカーソル ID 。')] = None,
    cursor_type: Annotated[
        Literal['Top', 'Bottom', 'Gap', 'ShowMore'],
        Query(description='カーソル ID の種類。Top はより新しいツイート、Bottom / Gap / ShowMore は未取得範囲のツイート。'),
    ] = 'Top',
    seen_tweet_ids: Annotated[
        str | None,
        Query(description='Twitter Web App 上で閲覧済みとして扱われるツイート ID のカンマ区切りリスト。'),
    ] = None,
):
    """
    ホームタイムラインを取得する。<br>
    ホームタイムラインの取得には screen_name で指定したスクリーンネームに紐づく Twitter アカウントが利用される。

    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """

    parsed_seen_tweet_ids = [
        seen_tweet_id
        for seen_tweet_id in (seen_tweet_ids.split(',') if seen_tweet_ids is not None else [])
        if seen_tweet_id != ''
    ]

    return await TwitterGraphQLAPI(twitter_account).homeLatestTimeline(
        cursor_id = cursor_id,
        cursor_type = cursor_type,
        seen_tweet_ids = parsed_seen_tweet_ids,
    )


@router.get(
    '/accounts/{screen_name}/search',
    summary = 'ツイート検索 API',
    response_description = '検索結果のツイートのリスト。',
    response_model = schemas.TimelineTweetsResult | schemas.TwitterAPIResult,
)
async def TwitterSearchAPI(
    twitter_account: Annotated[TwitterAccount, Depends(GetCurrentTwitterAccount)],
    query: Annotated[str, Query(description='検索クエリ。')],
    search_type: Annotated[Literal['Top', 'Latest'], Query(description='検索タイプ。Top は話題のツイート、Latest は最新のツイート。')] = 'Latest',
    cursor_id: Annotated[str | None, Query(description='前回のレスポンスから取得した、次のページを取得するためのカーソル ID 。')] = None,
    cursor_type: Annotated[
        Literal['Top', 'Bottom', 'Gap', 'ShowMore'],
        Query(description='カーソル ID の種類。Top はより新しいツイート、Bottom / Gap / ShowMore は未取得範囲のツイート。'),
    ] = 'Top',
):
    """
    指定されたクエリでツイートを検索する。

    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """

    return await TwitterGraphQLAPI(twitter_account).searchTimeline(
        search_type = search_type,
        query = query,
        cursor_id = cursor_id,
        cursor_type = cursor_type,
    )


@router.get(
    '/video-proxy',
    summary = 'Twitter 動画プロキシ API',
    response_class = StreamingResponse,
)
async def TwitterVideoProxyAPI(
    request: Request,
    url: Annotated[str, Query(description='プロキシ対象の Twitter 動画 URL 。')],
    current_user: Annotated[User, Depends(GetCurrentUserForTwitterVideoProxy)],
):
    """
    Twitter の動画を KonomiTV-BS4K サーバー経由でプロキシ配信する。<br>
    Twitter 側の仕様変更により、許可されたオリジン以外からの動画 URL への直接アクセスが<br>
    403 Forbidden で拒否されるようになったため、サーバー側でリクエストを中継することでこの制限を回避する。<br>
    Range リクエストに対応しており、動画のシーク操作が可能。<br>
    ログイン中のユーザーのみ利用でき、`video.twimg.com` / `pbs.twimg.com` の HTTPS URL だけを中継する。<br>
    自動 redirect は行わず、各 hop で scheme・hostname・解決 IP を再検証し、hop 数と転送量に上限を設ける。
    """

    # Depends で認証済みであることを保証する。ユーザー本体はこの API では使わない。
    del current_user

    try:
        client, upstream_response = await _OpenTwitterVideoUpstream(request, url)
    except TwitterVideoProxyURLRejected:
        logging.warning('[TwitterRouter][TwitterVideoProxyAPI] Upstream URL rejected by proxy policy.')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'URL is not allowed',
        )
    except TwitterVideoProxyLimitExceeded:
        logging.warning('[TwitterRouter][TwitterVideoProxyAPI] Upstream response exceeded transfer limit.')
        raise HTTPException(
            status_code = status.HTTP_502_BAD_GATEWAY,
            detail = 'Upstream response is too large',
        )

    # 動画のストリーミング再生に必要なヘッダだけを転送する。
    allowed_response_headers = {
        'content-type',
        'content-length',
        'content-range',
        'accept-ranges',
        'etag',
        'last-modified',
    }
    response_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() in allowed_response_headers
    }
    upstream_status_code = upstream_response.status_code
    # 認証 Cookie を使うレスポンスを共有キャッシュへ保存させず、URL を Referer に使わせない。
    response_headers['Cache-Control'] = 'private, no-store'
    response_headers['Referrer-Policy'] = 'no-referrer'
    if upstream_status_code == status.HTTP_304_NOT_MODIFIED:
        response_headers.pop('content-length', None)

    async def stream_with_limit() -> AsyncIterator[bytes]:
        """転送量上限付きで上流チャンクを返し、完了・中断を問わず接続を閉じる。"""

        try:
            async for chunk in _IterUpstreamBytesWithLimit(upstream_response):
                yield chunk
        except TwitterVideoProxyLimitExceeded:
            # ヘッダ送信後は status を変えられないため例外を再送出し、正常完了に見せず接続を中断する。
            logging.warning('[TwitterRouter][TwitterVideoProxyAPI] Aborted stream after transfer limit.')
            raise
        finally:
            await upstream_response.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_with_limit(),
        status_code = upstream_status_code,
        headers = response_headers,
    )
