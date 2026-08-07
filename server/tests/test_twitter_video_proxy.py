"""R-06: Twitter 動画プロキシの認証・redirect・帯域制限を回帰テストする。"""

from __future__ import annotations

import asyncio
import inspect
import socket
from collections.abc import AsyncIterator
from typing import Annotated, Any, get_args, get_origin, get_type_hints
from unittest.mock import MagicMock

import httpcore
import httpx
import pytest
from fastapi import HTTPException, status
from fastapi.params import Depends as DependsParameter
from starlette.requests import Request

import app.routers.TwitterRouter as TwitterRouter
from app.routers.TwitterRouter import (
    ALLOWED_VIDEO_PROXY_DOMAINS,
    MAX_VIDEO_PROXY_BYTES,
    MAX_VIDEO_PROXY_REDIRECTS,
    TWITTER_VIDEO_PROXY_ACCESS_TOKEN_COOKIE,
    GetCurrentUserForTwitterVideoProxy,
    TwitterVideoProxyAPI,
    TwitterVideoProxyLimitExceeded,
    TwitterVideoProxyURLRejected,
    ValidateTwitterVideoProxyURL,
    _IsAllowedGlobalAddress,
    _IterUpstreamBytesWithLimit,
    _OpenTwitterVideoUpstream,
    _TwitterVideoProxyNetworkBackend,
    _TwitterVideoProxyTransport,
)


@pytest.fixture(autouse=True)
def _DisableLoggingConfig(monkeypatch: pytest.MonkeyPatch) -> None:
    """ルーターのテストで実設定を要求するログ初期化を無効化する。"""

    monkeypatch.setattr(TwitterRouter.logging, 'debug', _IgnoreLogging)
    monkeypatch.setattr(TwitterRouter.logging, 'warning', _IgnoreLogging)
    monkeypatch.setattr(TwitterRouter.logging, 'error', _IgnoreLogging)


def _IgnoreLogging(*args: object, **kwargs: object) -> None:
    """テスト中のアプリ設定依存ログを無視する。"""


def _make_request(
    *,
    authorization: str | None = None,
    access_token_cookie: str | None = None,
    range_header: str | None = None,
    path: str = '/api/twitter/video-proxy',
) -> Request:
    """Starlette Request を最小構成で組み立てる。"""

    headers: list[tuple[bytes, bytes]] = []
    if authorization is not None:
        headers.append((b'authorization', authorization.encode('ascii')))
    if access_token_cookie is not None:
        headers.append((b'cookie', f'{TWITTER_VIDEO_PROXY_ACCESS_TOKEN_COOKIE}={access_token_cookie}'.encode('ascii')))
    if range_header is not None:
        headers.append((b'range', range_header.encode('ascii')))

    async def receive() -> dict[str, Any]:
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    scope: dict[str, Any] = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'http_version': '1.1',
        'method': 'GET',
        'scheme': 'http',
        'path': path,
        'raw_path': path.encode('ascii'),
        'query_string': b'',
        'headers': headers,
        'client': ('testclient', 50000),
        'server': ('test', 80),
    }
    return Request(scope, receive)


def _addrinfo(addresses: list[str], port: int = 443) -> list[tuple[Any, ...]]:
    """getaddrinfo 互換の戻り値を組み立てる。"""

    records: list[tuple[Any, ...]] = []
    for address in addresses:
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        records.append((family, socket.SOCK_STREAM, 0, '', (address, port)))
    return records


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """許可 CDN を公開 IP へ解決する。"""

    async def getaddrinfo_patch(
        self: asyncio.AbstractEventLoop,
        host: str,
        port: int,
        **kwargs: Any,
    ) -> list[tuple[Any, ...]]:
        if host in ALLOWED_VIDEO_PROXY_DOMAINS:
            return _addrinfo(['93.184.216.34'], port)
        if host == 'evil.example':
            return _addrinfo(['93.184.216.34'], port)
        if host == 'private.example':
            return _addrinfo(['10.0.0.1'], port)
        raise socket.gaierror('name resolution failed')

    monkeypatch.setattr(asyncio.BaseEventLoop, 'getaddrinfo', getaddrinfo_patch)


def test_is_allowed_global_address_rejects_private_and_mapped() -> None:
    """loopback・private・link-local・IPv4-mapped を拒否する。"""

    assert _IsAllowedGlobalAddress('93.184.216.34') is True
    assert _IsAllowedGlobalAddress('127.0.0.1') is False
    assert _IsAllowedGlobalAddress('10.1.2.3') is False
    assert _IsAllowedGlobalAddress('169.254.1.1') is False
    assert _IsAllowedGlobalAddress('::1') is False
    assert _IsAllowedGlobalAddress('::ffff:127.0.0.1') is False
    assert _IsAllowedGlobalAddress('not-an-ip') is False


def test_validate_rejects_external_domain(public_dns: None) -> None:
    """許可 CDN 以外の domain を拒否する。"""

    async def Run() -> None:
        with pytest.raises(TwitterVideoProxyURLRejected):
            await ValidateTwitterVideoProxyURL('https://evil.example/video.mp4')

    asyncio.run(Run())


def test_validate_rejects_non_https(public_dns: None) -> None:
    """http scheme を拒否する。"""

    async def Run() -> None:
        with pytest.raises(TwitterVideoProxyURLRejected):
            await ValidateTwitterVideoProxyURL('http://video.twimg.com/video.mp4')

    asyncio.run(Run())


def test_validate_rejects_private_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """許可ホスト名でも private IP へ解決されたら拒否する。"""

    async def private_getaddrinfo(self: asyncio.AbstractEventLoop, host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        return _addrinfo(['10.0.0.8'], port)

    monkeypatch.setattr(asyncio.BaseEventLoop, 'getaddrinfo', private_getaddrinfo)

    async def Run() -> None:
        with pytest.raises(TwitterVideoProxyURLRejected):
            await ValidateTwitterVideoProxyURL('https://video.twimg.com/video.mp4')

    asyncio.run(Run())


def test_validate_accepts_allowed_cdn(public_dns: None) -> None:
    """許可 CDN の HTTPS URL を受け入れる。"""

    async def Run() -> None:
        resolved_video = await ValidateTwitterVideoProxyURL('https://video.twimg.com/ext_tw_video/1.mp4')
        assert str(resolved_video.url) == 'https://video.twimg.com/ext_tw_video/1.mp4'
        assert resolved_video.addresses == ('93.184.216.34',)
        resolved_image = await ValidateTwitterVideoProxyURL('https://pbs.twimg.com/media/abc.mp4')
        assert str(resolved_image.url) == 'https://pbs.twimg.com/media/abc.mp4'
        assert resolved_image.addresses == ('93.184.216.34',)

    asyncio.run(Run())


@pytest.mark.parametrize('url', [
    'https://video.twimg.com:444/video.mp4',
    'https://video.twimg.com:invalid/video.mp4',
    'https://video.twimg.com/video.mp4#fragment',
])
def test_validate_rejects_nonstandard_port_and_fragment(public_dns: None, url: str) -> None:
    """許可 CDN でも 443 以外の port や fragment を拒否する。"""

    async def Run() -> None:
        with pytest.raises(TwitterVideoProxyURLRejected):
            await ValidateTwitterVideoProxyURL(url)

    asyncio.run(Run())


def test_auth_requires_bearer_or_cookie_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bearer も動画専用 Cookie も無い場合は 401 になる。"""

    async def Run() -> None:
        with pytest.raises(HTTPException) as exc_info:
            await GetCurrentUserForTwitterVideoProxy(_make_request())
        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED

    asyncio.run(Run())


def test_auth_accepts_bearer_and_cookie_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bearer と path 限定 Cookie の両方で GetCurrentUser へ到達する。"""

    calls: list[str] = []

    class _User:
        id = 1

    async def fake_get_current_user(*, token: str) -> _User:
        calls.append(token)
        return _User()

    monkeypatch.setattr(TwitterRouter, 'GetCurrentUser', fake_get_current_user)

    async def Run() -> None:
        user = await GetCurrentUserForTwitterVideoProxy(
            _make_request(authorization='Bearer bearer-token'),
        )
        assert user.id == 1
        user = await GetCurrentUserForTwitterVideoProxy(
            _make_request(access_token_cookie='cookie-token'),
        )
        assert user.id == 1
        assert calls == ['bearer-token', 'cookie-token']

    asyncio.run(Run())


class _FakeUpstreamResponse:
    """httpx.Response の最小モック。"""

    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b'video-bytes',
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {'content-type': 'video/mp4', 'content-length': str(len(body))})
        self._body = body
        self._chunks = chunks
        self.closed = False
        self.read_calls = 0

    async def aread(self) -> bytes:
        self.read_calls += 1
        return self._body

    async def aclose(self) -> None:
        self.closed = True

    async def aiter_bytes(self, chunk_size: int = 65536) -> AsyncIterator[bytes]:
        if self._chunks is not None:
            for chunk in self._chunks:
                yield chunk
            return
        yield self._body


class _FakeAsyncClient:
    """redirect 系列を返す httpx.AsyncClient モック。"""

    def __init__(self, responses: list[_FakeUpstreamResponse]) -> None:
        self._responses = list(responses)
        self._index = 0
        self.closed = False
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def build_request(self, method: str, url: str, headers: dict[str, str] | None = None) -> MagicMock:
        request = MagicMock()
        request.method = method
        request.url = url
        request.headers = headers or {}
        return request

    async def send(self, request: MagicMock, stream: bool = False) -> _FakeUpstreamResponse:
        self.requests.append((request.method, str(request.url), dict(request.headers)))
        if self._index >= len(self._responses):
            raise AssertionError('unexpected extra upstream request')
        response = self._responses[self._index]
        self._index += 1
        return response

    async def aclose(self) -> None:
        self.closed = True


def _install_client(monkeypatch: pytest.MonkeyPatch, client: _FakeAsyncClient) -> None:
    """AsyncClient コンストラクタを差し替える。"""

    def factory(*args: Any, **kwargs: Any) -> _FakeAsyncClient:
        assert kwargs.get('follow_redirects') is False
        assert isinstance(kwargs.get('transport'), _TwitterVideoProxyTransport)
        return client

    monkeypatch.setattr(TwitterRouter.httpx, 'AsyncClient', factory)


def test_network_backend_connects_only_to_pinned_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """実接続 backend は論理 hostname を再解決せず、検証済み IP だけを使う。"""

    connect_calls: list[dict[str, Any]] = []

    class FakeWriter:
        def get_extra_info(self, info: str) -> object:
            return None

    async def fake_open_connection(**kwargs: Any) -> tuple[object, FakeWriter]:
        connect_calls.append(kwargs)
        return object(), FakeWriter()

    monkeypatch.setattr(TwitterRouter.asyncio, 'open_connection', fake_open_connection)
    backend = _TwitterVideoProxyNetworkBackend()
    backend.setAllowedAddresses('video.twimg.com', ('93.184.216.34',))

    async def Run() -> None:
        await backend.connect_tcp('video.twimg.com', 443)
        assert connect_calls == [{'host': '93.184.216.34', 'port': 443, 'local_addr': None}]

        with pytest.raises(httpcore.ConnectError):
            await backend.connect_tcp('unvalidated.example', 443)

    asyncio.run(Run())


def test_open_upstream_rejects_external_domain_before_connect(public_dns: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """外部 domain は上流接続前に拒否する。"""

    client = _FakeAsyncClient([])
    _install_client(monkeypatch, client)

    async def Run() -> None:
        with pytest.raises(TwitterVideoProxyURLRejected):
            await _OpenTwitterVideoUpstream(_make_request(), 'https://evil.example/a.mp4')
        assert client.closed is True
        assert client.requests == []

    asyncio.run(Run())


def test_open_upstream_revalidates_redirect_and_keeps_range(public_dns: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """redirect 先を再検証し、許可 CDN 内なら Range を維持して最終応答を返す。"""

    redirect = _FakeUpstreamResponse(
        status_code=302,
        headers={'location': 'https://video.twimg.com/final.mp4'},
        body=b'',
    )
    final = _FakeUpstreamResponse(
        status_code=206,
        headers={
            'content-type': 'video/mp4',
            'content-length': '11',
            'content-range': 'bytes 0-10/11',
            'accept-ranges': 'bytes',
        },
        body=b'0123456789a',
    )
    client = _FakeAsyncClient([redirect, final])
    _install_client(monkeypatch, client)

    async def Run() -> None:
        opened_client, response = await _OpenTwitterVideoUpstream(
            _make_request(range_header='bytes=0-10'),
            'https://video.twimg.com/start.mp4',
        )
        assert opened_client is client
        assert response is final
        assert len(client.requests) == 2
        assert client.requests[0][1] == 'https://video.twimg.com/start.mp4'
        assert client.requests[1][1] == 'https://video.twimg.com/final.mp4'
        assert client.requests[0][2].get('range') == 'bytes=0-10'
        assert client.requests[1][2].get('range') == 'bytes=0-10'
        assert client.requests[0][2].get('Accept-Encoding') == 'identity'
        assert client.requests[1][2].get('Accept-Encoding') == 'identity'
        assert redirect.closed is True
        assert final.closed is False

    asyncio.run(Run())


def test_open_upstream_rejects_external_redirect(public_dns: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """許可 CDN 外へ向かう redirect を拒否する。"""

    redirect = _FakeUpstreamResponse(
        status_code = 302,
        headers = {'location': 'https://evil.example/secret'},
        body = b'',
    )
    client = _FakeAsyncClient([redirect])
    _install_client(monkeypatch, client)

    async def Run() -> None:
        with pytest.raises(TwitterVideoProxyURLRejected):
            await _OpenTwitterVideoUpstream(_make_request(), 'https://video.twimg.com/start.mp4')
        assert redirect.closed is True
        assert client.closed is True
        assert len(client.requests) == 1

    asyncio.run(Run())


def test_open_upstream_rejects_private_ip_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """許可ホスト名でも redirect 後の DNS が private IP なら拒否する。"""

    resolve_count = 0

    async def flipping_getaddrinfo(
        self: asyncio.AbstractEventLoop,
        host: str,
        port: int,
        **kwargs: Any,
    ) -> list[tuple[Any, ...]]:
        nonlocal resolve_count
        resolve_count += 1
        # 1 hop 目は公開 IP、redirect 後の再解決だけ private IP を返す。
        if resolve_count == 1:
            return _addrinfo(['93.184.216.34'], port)
        return _addrinfo(['10.0.0.1'], port)

    monkeypatch.setattr(asyncio.BaseEventLoop, 'getaddrinfo', flipping_getaddrinfo)

    redirect = _FakeUpstreamResponse(
        status_code = 302,
        headers = {'location': 'https://video.twimg.com/final.mp4'},
        body = b'',
    )
    client = _FakeAsyncClient([redirect])
    _install_client(monkeypatch, client)

    async def Run() -> None:
        with pytest.raises(TwitterVideoProxyURLRejected):
            await _OpenTwitterVideoUpstream(_make_request(), 'https://video.twimg.com/start.mp4')
        assert redirect.closed is True
        assert client.closed is True
        assert len(client.requests) == 1
        assert resolve_count >= 2

    asyncio.run(Run())


def test_open_upstream_rejects_redirect_loop(public_dns: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """redirect hop 上限を超えたら拒否する。"""

    responses = [
        _FakeUpstreamResponse(
            status_code=302,
            headers={'location': f'https://video.twimg.com/hop-{index}.mp4'},
            body=b'',
        )
        for index in range(MAX_VIDEO_PROXY_REDIRECTS + 1)
    ]
    client = _FakeAsyncClient(responses)
    _install_client(monkeypatch, client)

    async def Run() -> None:
        with pytest.raises(TwitterVideoProxyURLRejected, match='Too many upstream redirects'):
            await _OpenTwitterVideoUpstream(_make_request(), 'https://video.twimg.com/start.mp4')
        assert client.closed is True
        assert len(client.requests) == MAX_VIDEO_PROXY_REDIRECTS + 1

    asyncio.run(Run())


def test_open_upstream_rejects_content_length_over_limit(public_dns: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Content-Length が転送上限を超える応答をストリーム開始前に拒否する。"""

    oversized = _FakeUpstreamResponse(
        status_code=200,
        headers={
            'content-type': 'video/mp4',
            'content-length': str(MAX_VIDEO_PROXY_BYTES + 1),
        },
        body=b'',
    )
    client = _FakeAsyncClient([oversized])
    _install_client(monkeypatch, client)

    async def Run() -> None:
        with pytest.raises(TwitterVideoProxyLimitExceeded):
            await _OpenTwitterVideoUpstream(_make_request(), 'https://video.twimg.com/huge.mp4')
        assert oversized.closed is True
        assert client.closed is True

    asyncio.run(Run())


def test_open_upstream_does_not_read_error_body(public_dns: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """上流エラー本文はサイズを信頼できないため読み込まず、接続だけを閉じる。"""

    upstream_error = _FakeUpstreamResponse(
        status_code=500,
        headers={'content-type': 'text/plain'},
        body=b'attacker-controlled-body',
    )
    client = _FakeAsyncClient([upstream_error])
    _install_client(monkeypatch, client)

    async def Run() -> None:
        with pytest.raises(HTTPException) as exc_info:
            await _OpenTwitterVideoUpstream(_make_request(), 'https://video.twimg.com/error.mp4')
        assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY
        assert upstream_error.read_calls == 0
        assert upstream_error.closed is True
        assert client.closed is True

    asyncio.run(Run())


def test_iter_bytes_enforces_transfer_limit() -> None:
    """ストリーム累積が上限を超えたら打ち切る。"""

    chunk = b'x' * 1024
    # 上限をわずかに超えるチャンク列を流す。
    total_chunks = (MAX_VIDEO_PROXY_BYTES // len(chunk)) + 2
    response = _FakeUpstreamResponse(chunks=[chunk] * total_chunks)

    async def Run() -> None:
        with pytest.raises(TwitterVideoProxyLimitExceeded):
            async for _ in _IterUpstreamBytesWithLimit(response, max_bytes=MAX_VIDEO_PROXY_BYTES):
                pass

    asyncio.run(Run())


def test_api_streams_allowed_response_for_authenticated_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """認証済みユーザーには許可 CDN 応答をストリームする。"""

    class _User:
        id = 9

    open_called = False
    client = _FakeAsyncClient([])
    upstream = _FakeUpstreamResponse(
        body=b'abc',
        headers={'content-type': 'video/mp4', 'content-length': '3'},
    )

    async def fake_open_success(request: Request, url: str) -> tuple[_FakeAsyncClient, _FakeUpstreamResponse]:
        nonlocal open_called
        open_called = True
        return client, upstream

    monkeypatch.setattr(TwitterRouter, '_OpenTwitterVideoUpstream', fake_open_success)

    async def Run() -> None:
        response = await TwitterVideoProxyAPI(
            _make_request(),
            url = 'https://video.twimg.com/a.mp4',
            current_user = _User(),  # type: ignore[arg-type]
        )
        assert open_called is True
        assert response.status_code == 200
        chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
        assert b''.join(chunks) == b'abc'
        assert response.headers['cache-control'] == 'private, no-store'
        assert response.headers['referrer-policy'] == 'no-referrer'
        assert upstream.closed is True
        assert client.closed is True

    asyncio.run(Run())


def test_api_aborts_limited_stream_and_closes_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """stream 上限超過を正常終了に見せず、上流 response と client を閉じる。"""

    class _User:
        id = 9

    client = _FakeAsyncClient([])
    upstream = _FakeUpstreamResponse(headers={'content-type': 'video/mp4'})

    async def fake_open_success(request: Request, url: str) -> tuple[_FakeAsyncClient, _FakeUpstreamResponse]:
        return client, upstream

    async def fake_limited_stream(response: _FakeUpstreamResponse) -> AsyncIterator[bytes]:
        yield b'partial'
        raise TwitterVideoProxyLimitExceeded('too large')

    monkeypatch.setattr(TwitterRouter, '_OpenTwitterVideoUpstream', fake_open_success)
    monkeypatch.setattr(TwitterRouter, '_IterUpstreamBytesWithLimit', fake_limited_stream)

    async def Run() -> None:
        response = await TwitterVideoProxyAPI(
            _make_request(),
            url='https://video.twimg.com/a.mp4',
            current_user=_User(),  # type: ignore[arg-type]
        )
        chunks: list[bytes] = []
        with pytest.raises(TwitterVideoProxyLimitExceeded):
            async for chunk in response.body_iterator:
                chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
        assert chunks == [b'partial']
        assert upstream.closed is True
        assert client.closed is True

    asyncio.run(Run())


def test_api_maps_policy_rejection_to_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """ポリシー拒否は 422 になる。"""

    class _User:
        id = 1

    async def fake_open(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
        raise TwitterVideoProxyURLRejected('external domain')

    monkeypatch.setattr(TwitterRouter, '_OpenTwitterVideoUpstream', fake_open)

    async def Run() -> None:
        with pytest.raises(HTTPException) as exc_info:
            await TwitterVideoProxyAPI(
                _make_request(),
                url = 'https://evil.example/a.mp4',
                current_user = _User(),  # type: ignore[arg-type]
            )
        assert exc_info.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    asyncio.run(Run())


def test_endpoint_requires_current_user_dependency() -> None:
    """TwitterVideoProxyAPI が current_user 引数と認証 Depends を必須にしている。"""

    signature = inspect.signature(TwitterVideoProxyAPI)
    assert 'current_user' in signature.parameters

    # Annotated[..., Depends(GetCurrentUserForTwitterVideoProxy)] を型ヒントから取り出す。
    hints = get_type_hints(TwitterVideoProxyAPI, include_extras=True)
    current_user_hint = hints['current_user']
    assert get_origin(current_user_hint) is Annotated
    dependency_markers = [
        arg.dependency
        for arg in get_args(current_user_hint)[1:]
        if isinstance(arg, DependsParameter)
    ]
    assert GetCurrentUserForTwitterVideoProxy in dependency_markers
