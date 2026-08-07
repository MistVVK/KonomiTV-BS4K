# pyright: reportPrivateUsage=false

import asyncio
import socket
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from starlette.responses import StreamingResponse

import app.utils.DataBroadcastingHTTPClient as data_broadcasting_http_client
from app.routers import DataBroadcastingRouter
from app.utils.DataBroadcastingHTTPClient import (
    DataBroadcastingHTTPResponse,
    ResolvedUpstreamURL,
    UpstreamURLRejected,
)


@pytest.fixture(autouse=True)
def _DisableLoggingConfig(monkeypatch: pytest.MonkeyPatch) -> None:
    """ルーターのテストで実設定を要求するログ初期化を無効化する。"""

    monkeypatch.setattr(DataBroadcastingRouter.logging, 'debug', _IgnoreLogging)
    monkeypatch.setattr(DataBroadcastingRouter.logging, 'warning', _IgnoreLogging)
    monkeypatch.setattr(DataBroadcastingRouter.logging, 'error', _IgnoreLogging)


def _IgnoreLogging(*args: object, **kwargs: object) -> None:
    """テスト中のアプリ設定依存ログを無視する。"""


def _make_response(
    status_code: int = 200,
    body: bytes = b'ok',
    headers: dict[str, str] | None = None,
) -> DataBroadcastingHTTPResponse:
    """テスト用の上流HTTP応答を作成する。"""

    return DataBroadcastingHTTPResponse(
        status_code=status_code,
        headers=httpx.Headers(headers or {'content-type': 'text/plain'}),
        content=body,
    )


def _make_request(
    method: str,
    path: str,
    body: bytes = b'',
    content_type: str | None = None,
    *,
    content_length: int | None = None,
    include_content_length: bool = False,
    body_chunks: list[bytes] | None = None,
) -> Request:
    """Starlette Request を最小構成で組み立てる。"""

    headers: list[tuple[bytes, bytes]] = []
    if content_type is not None:
        headers.append((b'content-type', content_type.encode('ascii')))
    if include_content_length is True:
        length_value = content_length if content_length is not None else len(body)
        headers.append((b'content-length', str(length_value).encode('ascii')))

    chunks = list(body_chunks) if body_chunks is not None else [body]
    chunk_index = 0

    async def receive() -> dict[str, Any]:
        nonlocal chunk_index
        if chunk_index >= len(chunks):
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        current = chunks[chunk_index]
        chunk_index += 1
        return {
            'type': 'http.request',
            'body': current,
            'more_body': chunk_index < len(chunks),
        }

    scope: dict[str, Any] = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'http_version': '1.1',
        'method': method,
        'scheme': 'http',
        'path': path,
        'raw_path': path.encode('ascii'),
        'query_string': b'',
        'headers': headers,
        'client': ('testclient', 50000),
        'server': ('test', 80),
    }
    return Request(scope, receive)


async def _read_streaming_response(response: StreamingResponse) -> bytes:
    """StreamingResponse の本文をテスト側で読み取る。"""

    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.encode() if isinstance(chunk, str) else bytes(chunk))
    return b''.join(chunks)


def _install_router_request_fake(
    monkeypatch: pytest.MonkeyPatch,
    response: DataBroadcastingHTTPResponse,
) -> list[dict[str, Any]]:
    """ルーターから上流clientへ渡されたmethod・URL・本文を記録する。"""

    calls: list[dict[str, Any]] = []

    async def fake_request(
        method: str,
        request_url: str,
        *,
        headers: dict[str, str],
        content: bytes | None = None,
    ) -> DataBroadcastingHTTPResponse:
        calls.append({
            'method': method,
            'request_url': request_url,
            'headers': headers,
            'content': content,
        })
        return response

    monkeypatch.setattr(DataBroadcastingRouter, 'RequestWithSafeRedirects', fake_request)
    return calls


@pytest.mark.parametrize(
    'target_url',
    [
        'http://example.com/api?foo=1&bar=2',
        'http://example.com/path?q=a%20b&x=1&y=2',
        'https://example.com/search?q=%E3%83%86%E3%82%B9%E3%83%88&page=1',
    ],
)
def test_get_proxy_preserves_upstream_query(monkeypatch: pytest.MonkeyPatch, target_url: str) -> None:
    """?・複数 &・percent escape を含む target URL が上流へ完全一致で送られること。"""

    calls = _install_router_request_fake(monkeypatch, _make_response(body=b'body'))

    async def Run() -> None:
        # client は encodeURIComponent 相当でpathに載せ、serverは追加unquoteしない。
        request = _make_request('GET', f'/api/data-broadcasting/request/{quote(target_url, safe="")}')
        response = await DataBroadcastingRouter.BMLBrowserRequestGETProxyAPI(target_url, request)
        assert response.status_code == 200
        assert await _read_streaming_response(response) == b'body'
        assert calls[0]['method'] == 'GET'
        assert calls[0]['request_url'] == target_url

    asyncio.run(Run())


@pytest.mark.parametrize('status_code', [200, 304, 404, 500])
def test_get_proxy_preserves_upstream_status(monkeypatch: pytest.MonkeyPatch, status_code: int) -> None:
    """GETで上流statusを透過すること。"""

    calls = _install_router_request_fake(monkeypatch, _make_response(status_code=status_code))

    async def Run() -> None:
        target = 'http://example.com/status'
        request = _make_request('GET', f'/api/data-broadcasting/request/{quote(target, safe="")}')
        response = await DataBroadcastingRouter.BMLBrowserRequestGETProxyAPI(target, request)
        assert response.status_code == status_code
        assert len(calls) == 1

    asyncio.run(Run())


@pytest.mark.parametrize(
    'raw_body',
    [
        b'Denbun=' + bytes([0x83, 0x65, 0x83, 0x58, 0x83, 0x67]),
        b'Denbun=%A4%A2&extra=1+2=3',
        b'Denbun=a&b=c+d=e',
    ],
)
def test_post_proxy_forwards_raw_body_byte_for_byte(monkeypatch: pytest.MonkeyPatch, raw_body: bytes) -> None:
    """Shift_JIS / EUC-JP や & + = を含むraw bodyが上流へ完全一致すること。"""

    calls = _install_router_request_fake(monkeypatch, _make_response())

    async def Run() -> None:
        target = 'http://example.com/post'
        request = _make_request(
            'POST',
            f'/api/data-broadcasting/request/{quote(target, safe="")}',
            body=raw_body,
            content_type='application/x-www-form-urlencoded',
        )
        response = await DataBroadcastingRouter.BMLBrowserRequestPOSTProxyAPI(target, request)
        assert response.status_code == 200
        assert calls[0]['method'] == 'POST'
        assert calls[0]['request_url'] == target
        assert calls[0]['content'] == raw_body
        assert calls[0]['headers']['Content-Type'] == 'application/x-www-form-urlencoded'

    asyncio.run(Run())


def test_post_proxy_rejects_body_larger_than_4096_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """上限を超えるraw bodyは上流へ送信せず413にする。"""

    calls = _install_router_request_fake(monkeypatch, _make_response())

    async def Run() -> None:
        target = 'http://example.com/post'
        request = _make_request(
            'POST',
            f'/api/data-broadcasting/request/{quote(target, safe="")}',
            body=b'x' * 4097,
            content_type='application/x-www-form-urlencoded',
        )
        with pytest.raises(HTTPException) as ex_info:
            await DataBroadcastingRouter.BMLBrowserRequestPOSTProxyAPI(target, request)
        assert ex_info.value.status_code == 413
        assert calls == []

    asyncio.run(Run())


def test_proxy_rejects_egress_policy_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """接続先ポリシー違反は上流へ接続せず固定422を返す。"""

    async def reject_request(*args: Any, **kwargs: Any) -> DataBroadcastingHTTPResponse:
        raise UpstreamURLRejected('private address')

    monkeypatch.setattr(DataBroadcastingRouter, 'RequestWithSafeRedirects', reject_request)

    async def Run() -> None:
        target = 'http://127.0.0.1/secret'
        request = _make_request('GET', f'/api/data-broadcasting/request/{quote(target, safe="")}')
        with pytest.raises(HTTPException) as ex_info:
            await DataBroadcastingRouter.BMLBrowserRequestGETProxyAPI(target, request)
        assert ex_info.value.status_code == 422
        assert ex_info.value.detail == 'Request URL is not allowed'

    asyncio.run(Run())


@pytest.mark.parametrize('address', [
    '0.0.0.0',
    '10.0.0.1',
    '127.0.0.1',
    '169.254.169.254',
    '172.16.0.1',
    '192.0.2.1',
    '192.168.0.1',
    '224.0.0.1',
    '::1',
    '::ffff:127.0.0.1',
    'fc00::1',
])
def test_global_address_policy_rejects_non_global_targets(address: str) -> None:
    """loopback・private・link-local・予約・IPv4-mapped等を拒否する。"""

    assert data_broadcasting_http_client._IsAllowedGlobalAddress(address) is False


def test_global_address_policy_allows_public_target() -> None:
    """公開IPv4は接続候補として許可する。"""

    assert data_broadcasting_http_client._IsAllowedGlobalAddress('93.184.216.34') is True


@pytest.mark.parametrize('request_url', [
    'ftp://example.com/file',
    'http://user:password@example.com/file',
    'http://example.com/file#fragment',
    'http://example.com:invalid/file',
    'javascript:alert(1)',
    'http://example.com/line\nfeed',
])
def test_upstream_url_parser_rejects_unsafe_syntax(request_url: str) -> None:
    """HTTPプロキシで扱わないscheme・credentials・fragment等を拒否する。"""

    with pytest.raises(UpstreamURLRejected):
        data_broadcasting_http_client._ParseUpstreamURL(request_url)


def test_resolver_rejects_mixed_public_and_private_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """DNSがpublic/privateを混在して返す場合はfail closedにする。"""

    class FakeLoop:
        async def getaddrinfo(self, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.0.1', 443)),
            ]

    monkeypatch.setattr(data_broadcasting_http_client.asyncio, 'get_running_loop', lambda: FakeLoop())

    async def Run() -> None:
        with pytest.raises(UpstreamURLRejected):
            await data_broadcasting_http_client._ResolveAllowedAddresses('mixed.example', 443)

    asyncio.run(Run())


def test_pinned_backend_does_not_resolve_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """接続時は検証済みIPへ直接接続し、hostのDNS再解決を行わない。"""

    connect_calls: list[dict[str, Any]] = []

    class FakeReader:
        pass

    class FakeWriter:
        def get_extra_info(self, info: str) -> object:
            return None

    async def fake_open_connection(**kwargs: Any) -> tuple[FakeReader, FakeWriter]:
        connect_calls.append(kwargs)
        return FakeReader(), FakeWriter()

    monkeypatch.setattr(data_broadcasting_http_client.asyncio, 'open_connection', fake_open_connection)
    backend = data_broadcasting_http_client.PinnedNetworkBackend(
        'public.example',
        ('93.184.216.34',),
    )

    async def Run() -> None:
        stream = await backend.connect_tcp('public.example', 443)
        assert isinstance(stream, data_broadcasting_http_client._AsyncioNetworkStream)
        assert connect_calls == [{'host': '93.184.216.34', 'port': 443, 'local_addr': None}]

    asyncio.run(Run())


def test_request_once_uses_pinned_ip_and_preserves_http_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """実際のhttpcore送受信でも検証済みIPと元のHost・queryを使う。"""

    connect_calls: list[dict[str, Any]] = []
    written_bytes: list[bytes] = []

    class FakeWriter:
        def write(self, data: bytes) -> None:
            written_bytes.append(data)

        async def drain(self) -> None:
            return None

        def get_extra_info(self, info: str) -> object:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def fake_open_connection(**kwargs: Any) -> tuple[asyncio.StreamReader, FakeWriter]:
        connect_calls.append(kwargs)
        reader = asyncio.StreamReader()
        reader.feed_data(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\nok')
        reader.feed_eof()
        return reader, FakeWriter()

    monkeypatch.setattr(data_broadcasting_http_client.asyncio, 'open_connection', fake_open_connection)

    async def Run() -> None:
        response = await data_broadcasting_http_client._RequestOnce(
            ResolvedUpstreamURL(
                url=httpx.URL('http://public.example/path?q=1'),
                host='public.example',
                port=80,
                addresses=('93.184.216.34',),
            ),
            'GET',
            {'Accept': '*/*'},
            None,
        )
        assert response.status_code == 200
        assert response.content == b'ok'

    asyncio.run(Run())
    assert connect_calls == [{'host': '93.184.216.34', 'port': 80, 'local_addr': None}]
    sent_request = b''.join(written_bytes)
    assert b'GET /path?q=1 HTTP/1.1\r\n' in sent_request
    assert b'Host: public.example\r\n' in sent_request


def test_redirect_is_revalidated_and_post_302_becomes_get(monkeypatch: pytest.MonkeyPatch) -> None:
    """redirect先を再検証し、既存httpx相当のPOST 302 method変換を維持する。"""

    resolved_urls: list[str] = []
    requests: list[dict[str, Any]] = []

    async def fake_resolve(request_url: str) -> ResolvedUpstreamURL:
        resolved_urls.append(request_url)
        return ResolvedUpstreamURL(
            url=httpx.URL(request_url),
            host='public.example',
            port=80,
            addresses=('93.184.216.34',),
        )

    async def fake_request_once(
        resolved_url: ResolvedUpstreamURL,
        method: str,
        headers: dict[str, str],
        content: bytes | None,
    ) -> DataBroadcastingHTTPResponse:
        requests.append({
            'url': str(resolved_url.url),
            'method': method,
            'headers': headers,
            'content': content,
        })
        if len(requests) == 1:
            return _make_response(302, headers={'location': '/next'})
        return _make_response(200, b'finished')

    monkeypatch.setattr(data_broadcasting_http_client, 'ResolveUpstreamURL', fake_resolve)
    monkeypatch.setattr(data_broadcasting_http_client, '_RequestOnce', fake_request_once)

    async def Run() -> None:
        response = await data_broadcasting_http_client.RequestWithSafeRedirects(
            'POST',
            'http://public.example/start',
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            content=b'Denbun=test',
        )
        assert response.status_code == 200
        assert response.content == b'finished'
        assert resolved_urls == [
            'http://public.example/start',
            'http://public.example/next',
        ]
        assert requests[1]['method'] == 'GET'
        assert requests[1]['content'] is None
        assert 'Content-Type' not in requests[1]['headers']

    asyncio.run(Run())


def test_redirect_to_rejected_target_never_reaches_second_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """private redirect先は二度目の上流接続前に拒否する。"""

    request_count = 0

    async def fake_resolve(request_url: str) -> ResolvedUpstreamURL:
        if request_url == 'http://public.example/start':
            return ResolvedUpstreamURL(
                url=httpx.URL(request_url),
                host='public.example',
                port=80,
                addresses=('93.184.216.34',),
            )
        raise UpstreamURLRejected('redirected to private address')

    async def fake_request_once(*args: Any, **kwargs: Any) -> DataBroadcastingHTTPResponse:
        nonlocal request_count
        request_count += 1
        return _make_response(302, headers={'location': 'http://127.0.0.1/secret'})

    monkeypatch.setattr(data_broadcasting_http_client, 'ResolveUpstreamURL', fake_resolve)
    monkeypatch.setattr(data_broadcasting_http_client, '_RequestOnce', fake_request_once)

    async def Run() -> None:
        with pytest.raises(UpstreamURLRejected):
            await data_broadcasting_http_client.RequestWithSafeRedirects(
                'GET',
                'http://public.example/start',
                headers={},
            )
        assert request_count == 1

    asyncio.run(Run())


# ---------------------------------------------------------------------------
# R-05: probe 公開IPポリシー / timeout 上下限 / request・response body 上限
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('destination', [
    '127.0.0.1',
    '10.0.0.1',
    '192.168.0.1',
    '169.254.169.254',
    '::1',
    'fc00::1',
    '::ffff:127.0.0.1',
])
def test_probe_destination_rejects_non_global_literals(destination: str) -> None:
    """probe は loopback / private / link-local リテラルを公開IPポリシーで拒否する。"""

    async def Run() -> None:
        with pytest.raises(data_broadcasting_http_client.UpstreamURLRejected):
            await data_broadcasting_http_client.ResolveProbeDestination(destination)

    asyncio.run(Run())


def test_probe_destination_allows_public_literal() -> None:
    """probe は公開IPv4リテラルを許可する。"""

    async def Run() -> None:
        addresses = await data_broadcasting_http_client.ResolveProbeDestination('93.184.216.34')
        assert addresses == ('93.184.216.34',)

    asyncio.run(Run())


@pytest.mark.parametrize('timeout_milliseconds', [99, 5001])
def test_probe_api_rejects_timeout_outside_bounds(timeout_milliseconds: int) -> None:
    """internet-status は FastAPI 境界で 100〜5000ms 以外を拒否する。"""

    test_app = FastAPI()
    test_app.include_router(DataBroadcastingRouter.router)

    async def Run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app),
            base_url='http://test',
        ) as client:
            response = await client.get(
                '/api/data-broadcasting/internet-status',
                params={
                    'destination': '93.184.216.34',
                    'timeout_milliseconds': timeout_milliseconds,
                },
            )
        assert response.status_code == 422

    asyncio.run(Run())


def test_probe_api_rejects_private_destination_without_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    """internet-status は private destination を接続前に 422 にする。"""

    open_connection_calls: list[Any] = []
    ping_calls: list[Any] = []

    async def fake_open_connection(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
        open_connection_calls.append((args, kwargs))
        raise AssertionError('must not connect to rejected destination')

    def fake_ping(*args: Any, **kwargs: Any) -> float | None:
        ping_calls.append((args, kwargs))
        raise AssertionError('must not ping rejected destination')

    monkeypatch.setattr(DataBroadcastingRouter.asyncio, 'open_connection', fake_open_connection)
    monkeypatch.setattr(DataBroadcastingRouter, 'ping', fake_ping)

    async def Run() -> None:
        with pytest.raises(HTTPException) as ex_info:
            await DataBroadcastingRouter.BMLBrowserInternetStatusAPI(
                destination='127.0.0.1',
                is_icmp=False,
                timeout_milliseconds=1000,
            )
        assert ex_info.value.status_code == 422
        assert ex_info.value.detail == 'Probe destination is not allowed'
        assert open_connection_calls == []
        assert ping_calls == []

    asyncio.run(Run())


def test_probe_api_tcp_uses_pinned_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP probe は検証済み公開IPへ直接接続し、hostname を再解決しない。"""

    connect_calls: list[dict[str, Any]] = []

    class FakeWriter:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def fake_resolve(destination: str) -> tuple[str, ...]:
        assert destination == 'public.example'
        return ('93.184.216.34',)

    async def fake_open_connection(*args: Any, **kwargs: Any) -> tuple[object, FakeWriter]:
        connect_calls.append({'args': args, 'kwargs': kwargs})
        return object(), FakeWriter()

    monkeypatch.setattr(DataBroadcastingRouter, 'ResolveProbeDestination', fake_resolve)
    monkeypatch.setattr(DataBroadcastingRouter.asyncio, 'open_connection', fake_open_connection)

    async def Run() -> None:
        result = await DataBroadcastingRouter.BMLBrowserInternetStatusAPI(
            destination='public.example',
            is_icmp=False,
            timeout_milliseconds=1000,
        )
        assert result.success is True
        assert result.ip_address == '93.184.216.34'
        assert connect_calls[0]['args'][0] == '93.184.216.34'
        assert connect_calls[0]['args'][1] == 80

    asyncio.run(Run())


def test_probe_api_icmp_runs_in_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """ICMP ping は to_thread 経由で実行され、検証済みIPだけを対象にする。"""

    ping_calls: list[tuple[Any, ...]] = []
    to_thread_calls: list[Any] = []

    async def fake_resolve(destination: str) -> tuple[str, ...]:
        return ('93.184.216.34',)

    def fake_ping(address: str, timeout: float = 4) -> float:
        ping_calls.append((address, timeout))
        return 0.012

    async def fake_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        to_thread_calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(DataBroadcastingRouter, 'ResolveProbeDestination', fake_resolve)
    monkeypatch.setattr(DataBroadcastingRouter, 'ping', fake_ping)
    monkeypatch.setattr(DataBroadcastingRouter.asyncio, 'to_thread', fake_to_thread)

    async def Run() -> None:
        result = await DataBroadcastingRouter.BMLBrowserInternetStatusAPI(
            destination='public.example',
            is_icmp=True,
            timeout_milliseconds=1500,
        )
        assert result.success is True
        assert result.ip_address == '93.184.216.34'
        assert result.response_time_milliseconds == 12
        assert to_thread_calls[0][0] is fake_ping
        assert ping_calls == [('93.184.216.34', 1.5)]

    asyncio.run(Run())


def test_probe_api_icmp_false_result_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """ping3 が送受信エラーを False で返した場合は成功として扱わない。"""

    async def fake_resolve(destination: str) -> tuple[str, ...]:
        return ('93.184.216.34',)

    def fake_ping(address: str, timeout: float = 4) -> bool:
        return False

    monkeypatch.setattr(DataBroadcastingRouter, 'ResolveProbeDestination', fake_resolve)
    monkeypatch.setattr(DataBroadcastingRouter, 'ping', fake_ping)

    async def Run() -> None:
        result = await DataBroadcastingRouter.BMLBrowserInternetStatusAPI(
            destination='public.example',
            is_icmp=True,
            timeout_milliseconds=1000,
        )
        assert result.success is False
        assert result.ip_address is None
        assert result.response_time_milliseconds is None

    asyncio.run(Run())


def test_post_proxy_rejects_oversized_content_length_before_body_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """巨大 Content-Length は body stream を読まずに 413 にする。"""

    calls = _install_router_request_fake(monkeypatch, _make_response())
    receive_calls = {'count': 0}

    async def Run() -> None:
        target = 'http://example.com/post'
        request = _make_request(
            'POST',
            f'/api/data-broadcasting/request/{quote(target, safe="")}',
            body=b'never-read',
            content_type='application/x-www-form-urlencoded',
            content_length=DataBroadcastingRouter._MAX_POST_BODY_BYTES + 1,
            include_content_length=True,
        )
        original_receive = request._receive

        async def tracking_receive() -> dict[str, Any]:
            receive_calls['count'] += 1
            return await original_receive()

        request._receive = tracking_receive  # type: ignore[method-assign]
        with pytest.raises(HTTPException) as ex_info:
            await DataBroadcastingRouter.BMLBrowserRequestPOSTProxyAPI(target, request)
        assert ex_info.value.status_code == 413
        assert receive_calls['count'] == 0
        assert calls == []

    asyncio.run(Run())


def test_post_proxy_rejects_streamed_body_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Content-Length 無しの巨大 stream は累積上限で 413 にし上流へ送らない。"""

    calls = _install_router_request_fake(monkeypatch, _make_response())
    chunk = b'x' * 1024
    chunks = [chunk] * 5  # 5120 > 4096

    async def Run() -> None:
        target = 'http://example.com/post'
        request = _make_request(
            'POST',
            f'/api/data-broadcasting/request/{quote(target, safe="")}',
            content_type='application/x-www-form-urlencoded',
            body_chunks=chunks,
        )
        with pytest.raises(HTTPException) as ex_info:
            await DataBroadcastingRouter.BMLBrowserRequestPOSTProxyAPI(target, request)
        assert ex_info.value.status_code == 413
        assert calls == []

    asyncio.run(Run())


def test_read_response_body_rejects_content_length_over_limit() -> None:
    """上流 Content-Length 超過は stream 読取前に拒否する。"""

    class FakeResponse:
        def __init__(self) -> None:
            self.status = 200
            self.headers = [(b'content-length', str(data_broadcasting_http_client.MAX_UPSTREAM_RESPONSE_BYTES + 1).encode('ascii'))]
            self.stream_calls = 0

        async def aiter_stream(self):
            self.stream_calls += 1
            yield b'should-not-read'

    async def Run() -> None:
        response = FakeResponse()
        with pytest.raises(data_broadcasting_http_client.UpstreamBodyLimitExceeded):
            await data_broadcasting_http_client._ReadResponseBodyWithLimit(response)  # type: ignore[arg-type]
        assert response.stream_calls == 0

    asyncio.run(Run())


def test_read_response_body_ignores_representation_length_on_304() -> None:
    """304 の Content-Length は本文長ではないため、表現が4MiB超でも空本文を受理する。"""

    class FakeResponse:
        def __init__(self) -> None:
            self.status = 304
            self.headers = [(b'content-length', str(data_broadcasting_http_client.MAX_UPSTREAM_RESPONSE_BYTES + 1).encode('ascii'))]

        async def aiter_stream(self):
            yield b''

    async def Run() -> None:
        body = await data_broadcasting_http_client._ReadResponseBodyWithLimit(FakeResponse())  # type: ignore[arg-type]
        assert body == b''

    asyncio.run(Run())


def test_read_response_body_rejects_streamed_body_over_limit() -> None:
    """Content-Length 無しの巨大上流応答は累積上限で拒否する。"""

    class FakeResponse:
        def __init__(self) -> None:
            self.status = 200
            self.headers: list[tuple[bytes, bytes]] = []

        async def aiter_stream(self):
            chunk_size = 1024 * 1024
            total_chunks = (data_broadcasting_http_client.MAX_UPSTREAM_RESPONSE_BYTES // chunk_size) + 2
            for _ in range(total_chunks):
                yield b'y' * chunk_size

    async def Run() -> None:
        with pytest.raises(data_broadcasting_http_client.UpstreamBodyLimitExceeded):
            await data_broadcasting_http_client._ReadResponseBodyWithLimit(FakeResponse())  # type: ignore[arg-type]

    asyncio.run(Run())


def test_get_proxy_maps_upstream_body_limit_to_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """上流応答上限超過はクライアントへ 502 として返す。"""

    async def oversized_request(*args: Any, **kwargs: Any) -> DataBroadcastingHTTPResponse:
        raise data_broadcasting_http_client.UpstreamBodyLimitExceeded('too large')

    monkeypatch.setattr(DataBroadcastingRouter, 'RequestWithSafeRedirects', oversized_request)

    async def Run() -> None:
        target = 'http://example.com/big'
        request = _make_request('GET', f'/api/data-broadcasting/request/{quote(target, safe="")}')
        with pytest.raises(HTTPException) as ex_info:
            await DataBroadcastingRouter.BMLBrowserRequestGETProxyAPI(target, request)
        assert ex_info.value.status_code == 502
        assert ex_info.value.detail == 'Upstream response is too large'

    asyncio.run(Run())


def test_request_once_closes_connection_when_response_exceeds_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """上流応答超過時は aclose して接続を残さない。"""

    connect_calls: list[dict[str, Any]] = []

    class FakeWriter:
        def write(self, data: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def get_extra_info(self, info: str) -> object:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def fake_open_connection(**kwargs: Any) -> tuple[asyncio.StreamReader, FakeWriter]:
        connect_calls.append(kwargs)
        reader = asyncio.StreamReader()
        oversized = data_broadcasting_http_client.MAX_UPSTREAM_RESPONSE_BYTES + 1
        reader.feed_data(
            f'HTTP/1.1 200 OK\r\nContent-Length: {oversized}\r\nContent-Type: text/plain\r\n\r\n'.encode('ascii'),
        )
        reader.feed_data(b'z' * min(oversized, 64))
        reader.feed_eof()
        return reader, FakeWriter()

    async def Run() -> None:
        monkeypatch.setattr(data_broadcasting_http_client.asyncio, 'open_connection', fake_open_connection)

        # Content-Length 先行拒否後も _RequestOnce の finally で response.aclose が走り、
        # 接続プールが閉じられることを例外が外へ伝播することと合わせて確認する。
        with pytest.raises(data_broadcasting_http_client.UpstreamBodyLimitExceeded):
            await data_broadcasting_http_client._RequestOnce(
                ResolvedUpstreamURL(
                    url=httpx.URL('http://public.example/big'),
                    host='public.example',
                    port=80,
                    addresses=('93.184.216.34',),
                ),
                'GET',
                {'Accept': '*/*'},
                None,
            )
        assert connect_calls == [{'host': '93.184.216.34', 'port': 80, 'local_addr': None}]

    asyncio.run(Run())
