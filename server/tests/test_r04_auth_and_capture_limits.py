"""R-04: 重処理・Capture・設定 GET の権限と Capture 上限を回帰テストする。"""

from __future__ import annotations

import asyncio
import importlib
import io
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import puremagic
import pytest
from fastapi import HTTPException, status
from fastapi.routing import APIRoute
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient
from starlette.datastructures import Headers, UploadFile
from starlette.types import Message, Receive, Scope, Send

from app import config as config_module
from app.routers import (
    CapturesRouter,
    CMAnalysisRouter,
    MaintenanceRouter,
    SettingsRouter,
    VideosRouter,
)
from app.routers.UsersRouter import GetCurrentAdminUser, GetCurrentUser


if config_module._CONFIG is None:
    config_module._CONFIG = config_module.HostServerSettings().toServerSettings(bypass_validation=True)
app_module = importlib.import_module('app.app')
app_module._shutdown_completed = True
main_app = app_module.app


class _RegularUser:
    id = 7
    name = 'regular-user'
    is_admin = False


class _AdminUser:
    id = 1
    name = 'admin-user'
    is_admin = True


async def _GetRegularUser() -> _RegularUser:
    return _RegularUser()


async def _GetAdminUser() -> _AdminUser:
    return _AdminUser()


def _ClearMainAppOverrides() -> None:
    main_app.dependency_overrides.pop(GetCurrentUser, None)
    main_app.dependency_overrides.pop(GetCurrentAdminUser, None)


def _RouteDependsOn(route: APIRoute, dependency: Any) -> bool:
    # include_router の dependencies と endpoint 引数の Depends の両方を見る
    for item in route.dependencies:
        if item.dependency is dependency:
            return True
    for item in route.dependant.dependencies:
        if item.call is dependency:
            return True
    return False


def _FindRoute(path: str, method: str) -> APIRoute:
    for route in main_app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path != path:
            continue
        if method.upper() not in (route.methods or set()):
            continue
        return route
    raise AssertionError(f'route not found: {method} {path}')


def test_maintenance_and_heavy_video_routes_require_admin() -> None:
    """Maintenance 全件と手動再解析系は GetCurrentAdminUser に依存する。"""

    admin_paths = [
        ('POST', '/api/maintenance/update-database'),
        ('POST', '/api/maintenance/run-batch-scan'),
        ('POST', '/api/maintenance/reanalyze-all-recorded-videos'),
        ('POST', '/api/maintenance/detect-cm-sections-for-all-recorded-videos'),
        ('POST', '/api/maintenance/run-background-analysis'),
        ('POST', '/api/videos/{video_id}/reanalyze'),
        ('POST', '/api/videos/{video_id}/detect-cm-sections'),
        ('POST', '/api/videos/{video_id}/thumbnail/regenerate'),
        ('GET', '/api/settings/server'),
        ('GET', '/api/cm-analysis/settings'),
    ]
    missing: list[str] = []
    for method, path in admin_paths:
        route = _FindRoute(path, method)
        if not _RouteDependsOn(route, GetCurrentAdminUser):
            missing.append(f'{method} {path}')
    assert missing == [], f'GetCurrentAdminUser 未付与: {missing}'


def test_capture_and_playback_index_require_current_user() -> None:
    """Capture と再生索引生成はログインユーザー依存を持つ。"""

    capture_route = _FindRoute('/api/captures', 'POST')
    assert _RouteDependsOn(capture_route, GetCurrentUser)
    index_route = _FindRoute('/api/videos/{video_id}/playback-index', 'POST')
    assert _RouteDependsOn(index_route, GetCurrentUser)


def test_unauthenticated_sensitive_apis_return_401() -> None:
    """未認証では権限境界のある API が 401 になる。"""

    _ClearMainAppOverrides()

    async def GetResponses():
        async with HTTPXAsyncClient(transport=ASGITransport(app=main_app), base_url='http://test') as client:
            # 録画 DB 依存の endpoint は GetRecordedProgram より先に auth が解決されることを確認する
            return (
                await client.post('/api/maintenance/update-database'),
                await client.post('/api/maintenance/run-batch-scan'),
                await client.get('/api/settings/server'),
                await client.get('/api/cm-analysis/settings'),
                await client.post('/api/captures', files={
                    'image': ('shot.jpg', b'\xff\xd8\xff\xd9', 'image/jpeg'),
                }),
                await client.post('/api/videos/1/playback-index'),
                await client.post('/api/videos/1/reanalyze'),
                await client.post('/api/videos/1/thumbnail/regenerate'),
            )

    try:
        responses = asyncio.run(GetResponses())
    finally:
        _ClearMainAppOverrides()
    assert [response.status_code for response in responses] == [401] * len(responses)


def test_regular_user_is_forbidden_from_admin_settings_and_maintenance() -> None:
    """一般ユーザーは admin 専用 API で 403 になる。"""

    _ClearMainAppOverrides()
    # GetCurrentAdminUser は GetCurrentUser に依存するため、一般ユーザーへ差し替えると 403 になる
    main_app.dependency_overrides[GetCurrentUser] = _GetRegularUser

    async def GetResponses():
        async with HTTPXAsyncClient(transport=ASGITransport(app=main_app), base_url='http://test') as client:
            return (
                await client.get('/api/settings/server'),
                await client.get('/api/cm-analysis/settings'),
                await client.post('/api/maintenance/update-database'),
            )

    try:
        responses = asyncio.run(GetResponses())
    finally:
        _ClearMainAppOverrides()
    assert [response.status_code for response in responses] == [403, 403, 403]


def test_regular_user_can_reach_capture_endpoint_auth_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一般ユーザーは Capture API の認証境界を越え、保存処理まで到達できる。"""

    _ClearMainAppOverrides()
    main_app.dependency_overrides[GetCurrentUser] = _GetRegularUser
    upload_dir = tmp_path / 'captures'
    upload_dir.mkdir()
    fake_config = MagicMock()
    fake_config.capture.upload_folders = [str(upload_dir)]
    monkeypatch.setattr(CapturesRouter, 'Config', lambda: fake_config)

    async def GetResponse():
        async with HTTPXAsyncClient(transport=ASGITransport(app=main_app), base_url='http://test') as client:
            # 最小 JPEG を送り、auth を越えて 204 になることを確認する
            return await client.post('/api/captures', files={
                'image': ('shot.jpg', b'\xff\xd8\xff\xd9', 'image/jpeg'),
            })

    try:
        response = asyncio.run(GetResponse())
    finally:
        _ClearMainAppOverrides()
    assert response.status_code == 204
    assert any(upload_dir.iterdir())


def test_version_api_exposes_non_sensitive_runtime_fields() -> None:
    """/api/version は未認証でも encoder 系の非機密 runtime 情報を返す。"""

    _ClearMainAppOverrides()

    async def GetResponse():
        async with HTTPXAsyncClient(transport=ASGITransport(app=main_app), base_url='http://test') as client:
            return await client.get('/api/version')

    try:
        response = asyncio.run(GetResponse())
    finally:
        _ClearMainAppOverrides()
    assert response.status_code == 200
    payload = response.json()
    assert 'encoder' in payload
    assert 'encoder_bs4k' in payload
    assert 'bs4k_ignore_viewer_low_latency' in payload
    assert 'backend' in payload
    # 機密パスは version に載せない
    assert 'recorded_folders' not in payload
    assert 'logo_directory' not in payload


def test_capture_upload_rejects_oversize_and_removes_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture は累積 byte 上限を超えず、途中ファイルを残さない。"""

    upload_dir = tmp_path / 'captures'
    upload_dir.mkdir()
    fake_config = MagicMock()
    fake_config.capture.upload_folders = [str(upload_dir)]
    monkeypatch.setattr(CapturesRouter, 'Config', lambda: fake_config)
    # puremagic を JPEG 判定へ固定し、巨大 body でも magic 検査で止まらないようにする
    monkeypatch.setattr(
        puremagic,
        'magic_stream',
        lambda _stream: [MagicMock(mime_type='image/jpeg')],
    )

    oversized = b'x' * (CapturesRouter.MAX_CAPTURE_UPLOAD_BYTES + 1)
    upload = UploadFile(
        file=io.BytesIO(oversized),
        size=len(oversized),
        filename='huge.jpg',
        headers=Headers({'content-type': 'image/jpeg'}),
    )

    with pytest.raises(HTTPException) as exc_info:
        CapturesRouter.CaptureUploadAPI(image=upload, current_user=_RegularUser())  # type: ignore[arg-type]

    assert exc_info.value.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    assert list(upload_dir.iterdir()) == []


def test_capture_upload_skips_folder_without_reserved_free_space(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最終フォルダでも予約空き容量が足りなければ書き込まない。"""

    upload_dir = tmp_path / 'captures'
    upload_dir.mkdir()
    fake_config = MagicMock()
    fake_config.capture.upload_folders = [str(upload_dir)]
    monkeypatch.setattr(CapturesRouter, 'Config', lambda: fake_config)
    monkeypatch.setattr(
        puremagic,
        'magic_stream',
        lambda _stream: [MagicMock(mime_type='image/jpeg')],
    )
    monkeypatch.setattr(
        CapturesRouter.shutil,
        'disk_usage',
        lambda _path: MagicMock(free=CapturesRouter._MIN_FREE_BYTES_AFTER_WRITE - 1),
    )

    upload = UploadFile(
        file=io.BytesIO(b'\xff\xd8\xff\xd9'),
        size=4,
        filename='shot.jpg',
        headers=Headers({'content-type': 'image/jpeg'}),
    )
    with pytest.raises(HTTPException) as exc_info:
        CapturesRouter.CaptureUploadAPI(image=upload, current_user=_RegularUser())  # type: ignore[arg-type]
    assert exc_info.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert list(upload_dir.iterdir()) == []


def test_capture_body_limit_middleware_rejects_content_length_before_body_read() -> None:
    """Content-Length 超過は本文 receive 前に 413 になる。"""

    receive_call_count = 0

    async def tracking_receive() -> Message:
        nonlocal receive_call_count
        receive_call_count += 1
        # Content-Length 先行拒否では body を要求されないはず
        return {
            'type': 'http.request',
            'body': b'x' * 1024,
            'more_body': True,
        }

    sent_messages: list[Message] = []

    async def tracking_send(message: Message) -> None:
        sent_messages.append(message)

    scope: Scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'http_version': '1.1',
        'method': 'POST',
        'scheme': 'http',
        'path': '/api/captures',
        'raw_path': b'/api/captures',
        'query_string': b'',
        'headers': [
            (b'content-type', b'multipart/form-data; boundary=----test'),
            (b'content-length', str(CapturesRouter.MAX_CAPTURE_REQUEST_BODY_BYTES + 1).encode('ascii')),
        ],
        'client': ('127.0.0.1', 12345),
        'server': ('test', 80),
    }

    async def downstream_app(_scope: Scope, _receive: Receive, _send: Send) -> None:
        raise AssertionError('downstream app must not run when Content-Length exceeds the limit')

    middleware = CapturesRouter.CaptureUploadBodyLimitMiddleware(downstream_app)
    asyncio.run(middleware(scope, tracking_receive, tracking_send))

    assert receive_call_count == 0
    assert any(
        message.get('type') == 'http.response.start'
        and message.get('status') == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        for message in sent_messages
    )


def test_capture_body_limit_middleware_stops_streamed_body_before_full_spool() -> None:
    """Content-Length 無しの巨大ストリームは累積上限で multiparty 解析前に止める。"""

    chunk_size = 1024 * 1024
    total_chunks = (CapturesRouter.MAX_CAPTURE_REQUEST_BODY_BYTES // chunk_size) + 3
    total_source_bytes = total_chunks * chunk_size
    chunks_sent = 0
    bytes_delivered_to_app = 0

    async def streaming_receive() -> Message:
        nonlocal chunks_sent
        if chunks_sent >= total_chunks:
            return {
                'type': 'http.request',
                'body': b'',
                'more_body': False,
            }
        chunks_sent += 1
        return {
            'type': 'http.request',
            'body': b'x' * chunk_size,
            'more_body': chunks_sent < total_chunks,
        }

    sent_messages: list[Message] = []

    async def tracking_send(message: Message) -> None:
        sent_messages.append(message)

    scope: Scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'http_version': '1.1',
        'method': 'POST',
        'scheme': 'http',
        'path': '/api/captures',
        'raw_path': b'/api/captures',
        'query_string': b'',
        'headers': [
            (b'content-type', b'multipart/form-data; boundary=----test'),
        ],
        'client': ('127.0.0.1', 12345),
        'server': ('test', 80),
    }

    async def body_consuming_app(_scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal bytes_delivered_to_app
        # FastAPI の multipart 解析と同様に、本文を読み切ろうとする
        while True:
            message = await receive()
            if message['type'] != 'http.request':
                continue
            bytes_delivered_to_app += len(message.get('body', b''))
            if message.get('more_body', False) is False:
                break
        await send({
            'type': 'http.response.start',
            'status': 204,
            'headers': [],
        })
        await send({
            'type': 'http.response.body',
            'body': b'',
            'more_body': False,
        })

    middleware = CapturesRouter.CaptureUploadBodyLimitMiddleware(body_consuming_app)
    asyncio.run(middleware(scope, streaming_receive, tracking_send))

    # 超過検知後は source の残りも後段への本文も読まず、直ちに 413 へ切り替える
    assert chunks_sent < total_chunks
    assert bytes_delivered_to_app <= CapturesRouter.MAX_CAPTURE_REQUEST_BODY_BYTES
    assert bytes_delivered_to_app < total_source_bytes
    assert any(
        message.get('type') == 'http.response.start'
        and message.get('status') == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        for message in sent_messages
    )


def test_main_app_capture_rejects_oversized_content_length_with_401_or_413() -> None:
    """本番アプリでも巨大 Content-Length の Capture は 401 より前に 413 になる。"""

    _ClearMainAppOverrides()

    async def GetResponse():
        async with HTTPXAsyncClient(transport=ASGITransport(app=main_app), base_url='http://test') as client:
            oversized = b'x' * (CapturesRouter.MAX_CAPTURE_REQUEST_BODY_BYTES + 1)
            return await client.post(
                '/api/captures',
                content=oversized,
                headers={'Content-Type': 'multipart/form-data; boundary=----test'},
            )

    try:
        response = asyncio.run(GetResponse())
    finally:
        _ClearMainAppOverrides()
    # 未認証でも本文上限が先に働き 413 になる（multipart 全量 spool 後の 401 ではない）
    assert response.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE


def test_source_routers_keep_auth_on_endpoints_not_router_globals() -> None:
    """source router 自体へ global dependencies を書かず、endpoint 単位で付与する。"""

    for router in (
        CapturesRouter.router,
        MaintenanceRouter.router,
        SettingsRouter.router,
        CMAnalysisRouter.router,
        VideosRouter.router,
    ):
        assert list(router.dependencies) == []
