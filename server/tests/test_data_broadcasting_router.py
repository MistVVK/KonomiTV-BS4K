
import asyncio
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.routers import DataBroadcastingRouter


class _FakeStreamResponse:
    """httpx.Response の最小代替。"""

    def __init__(self, status_code: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = httpx.Headers(headers or {'content-type': 'text/plain'})

    def iter_bytes(self):
        yield self._body


class _RecordingAsyncClient:
    """上流へ送られた URL / body / method を記録する httpx.AsyncClient 代替。"""

    last_method: str | None = None
    last_url: str | None = None
    last_content: bytes | None = None
    last_headers: dict[str, str] | None = None
    response_status: int = 200
    response_body: bytes = b'ok'

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).last_headers = kwargs.get('headers')

    async def __aenter__(self) -> '_RecordingAsyncClient':
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(self, url: str) -> _FakeStreamResponse:
        type(self).last_method = 'GET'
        type(self).last_url = url
        type(self).last_content = None
        return _FakeStreamResponse(self.response_status, self.response_body)

    async def post(self, url: str, content: bytes | str = b'') -> _FakeStreamResponse:
        type(self).last_method = 'POST'
        type(self).last_url = url
        type(self).last_content = content if isinstance(content, bytes) else content.encode('utf-8')
        return _FakeStreamResponse(self.response_status, self.response_body)


def _make_request(method: str, path: str, body: bytes = b'', content_type: str | None = None) -> Request:
    """Starlette Request を最小構成で組み立てる。"""

    headers: list[tuple[bytes, bytes]] = []
    if content_type is not None:
        headers.append((b'content-type', content_type.encode('ascii')))

    async def receive() -> dict[str, Any]:
        return {'type': 'http.request', 'body': body, 'more_body': False}

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

    _RecordingAsyncClient.response_status = 200
    _RecordingAsyncClient.response_body = b'body'
    monkeypatch.setattr(DataBroadcastingRouter.httpx, 'AsyncClient', _RecordingAsyncClient)
    monkeypatch.setattr(DataBroadcastingRouter.logging, 'debug', lambda *args, **kwargs: None)
    monkeypatch.setattr(DataBroadcastingRouter.logging, 'error', lambda *args, **kwargs: None)

    async def Run() -> None:
        # client は encodeURIComponent 相当で path に載せ、server は追加 unquote しない
        # FastAPI Path は URL decode するため、ここでも decode 済み URL を渡す
        request = _make_request('GET', f'/api/data-broadcasting/request/{quote(target_url, safe="")}')
        response = await DataBroadcastingRouter.BMLBrowserRequestGETProxyAPI(target_url, request)
        assert response.status_code == 200
        assert _RecordingAsyncClient.last_url == target_url

    asyncio.run(Run())


@pytest.mark.parametrize('status_code', [200, 304, 404, 500])
def test_get_proxy_preserves_upstream_status(monkeypatch: pytest.MonkeyPatch, status_code: int) -> None:
    """GET で上流 status を透過すること。"""

    _RecordingAsyncClient.response_status = status_code
    _RecordingAsyncClient.response_body = b'x'
    monkeypatch.setattr(DataBroadcastingRouter.httpx, 'AsyncClient', _RecordingAsyncClient)
    monkeypatch.setattr(DataBroadcastingRouter.logging, 'debug', lambda *args, **kwargs: None)
    monkeypatch.setattr(DataBroadcastingRouter.logging, 'error', lambda *args, **kwargs: None)

    async def Run() -> None:
        target = 'http://example.com/status'
        request = _make_request('GET', f'/api/data-broadcasting/request/{quote(target, safe="")}')
        response = await DataBroadcastingRouter.BMLBrowserRequestGETProxyAPI(target, request)
        assert response.status_code == status_code

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
    """Shift_JIS / EUC-JP や & + = を含む raw body が上流へ完全一致すること。"""

    _RecordingAsyncClient.response_status = 200
    _RecordingAsyncClient.response_body = b'ok'
    monkeypatch.setattr(DataBroadcastingRouter.httpx, 'AsyncClient', _RecordingAsyncClient)
    monkeypatch.setattr(DataBroadcastingRouter.logging, 'debug', lambda *args, **kwargs: None)
    monkeypatch.setattr(DataBroadcastingRouter.logging, 'error', lambda *args, **kwargs: None)

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
        assert _RecordingAsyncClient.last_content == raw_body
        assert _RecordingAsyncClient.last_url == target
        assert _RecordingAsyncClient.last_headers is not None
        assert _RecordingAsyncClient.last_headers['Content-Type'] == 'application/x-www-form-urlencoded'

    asyncio.run(Run())


def test_post_proxy_rejects_body_larger_than_4096_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """上限を超える raw body は上流へ送信せず 422 にする。"""

    monkeypatch.setattr(DataBroadcastingRouter.httpx, 'AsyncClient', _RecordingAsyncClient)
    monkeypatch.setattr(DataBroadcastingRouter.logging, 'debug', lambda *args, **kwargs: None)
    monkeypatch.setattr(DataBroadcastingRouter.logging, 'error', lambda *args, **kwargs: None)

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
        assert ex_info.value.status_code == 422

    asyncio.run(Run())


@pytest.mark.parametrize('status_code', [200, 304, 404, 500])
def test_post_proxy_preserves_upstream_status(monkeypatch: pytest.MonkeyPatch, status_code: int) -> None:
    """POST で上流 status を透過すること。"""

    _RecordingAsyncClient.response_status = status_code
    _RecordingAsyncClient.response_body = b'x'
    monkeypatch.setattr(DataBroadcastingRouter.httpx, 'AsyncClient', _RecordingAsyncClient)
    monkeypatch.setattr(DataBroadcastingRouter.logging, 'debug', lambda *args, **kwargs: None)
    monkeypatch.setattr(DataBroadcastingRouter.logging, 'error', lambda *args, **kwargs: None)

    async def Run() -> None:
        target = 'http://example.com/post'
        request = _make_request(
            'POST',
            f'/api/data-broadcasting/request/{quote(target, safe="")}',
            body=b'Denbun=test',
            content_type='application/x-www-form-urlencoded',
        )
        response = await DataBroadcastingRouter.BMLBrowserRequestPOSTProxyAPI(target, request)
        assert response.status_code == status_code

    asyncio.run(Run())
