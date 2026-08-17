from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from http.cookies import SimpleCookie

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient
from httpx import Response as HTTPXResponse
from jose import jwt
from tortoise import Tortoise

from app.CompatibilityAPI import CreateCompatibilityAPI
from app.constants import JST, JWT_SECRET_KEY, PASSWORD_CONTEXT
from app.models.User import User
from app.routers.KonomiTVBS4KSpeedTestRouter import router as speed_test_router
from app.routers.UsersRouter import GenerateAccessToken
from app.streams.KonomiTVBS4KPlaybackEncoding import (
    ResolveKonomiTVBS4KPlaybackVideoBitrate,
)
from app.utils.KonomiTVBS4KSpeedTest import (
    KONOMITV_BS4K_SPEED_TEST_COOKIE_NAME,
    KONOMITV_BS4K_SPEED_TEST_COOKIE_PATH,
    KONOMITV_BS4K_SPEED_TEST_DOWNLOAD_SECONDS,
    KONOMITV_BS4K_SPEED_TEST_DOWNLOAD_WINDOW_SECONDS,
    KONOMITV_BS4K_SPEED_TEST_JWT_ISSUER,
    KONOMITV_BS4K_SPEED_TEST_JWT_TYPE,
    KONOMITV_BS4K_SPEED_TEST_MAX_CK_SIZE,
    KONOMITV_BS4K_SPEED_TEST_TERRESTRIAL_QUALITIES,
    KONOMITV_BS4K_SPEED_TEST_VARIABLE_BITRATE_SAFETY_FACTOR,
    SPEED_TEST_SESSION_MANAGER,
    BuildKonomiTVBS4KSpeedTestQualityThresholds,
    GenerateKonomiTVBS4KSpeedTestSessionToken,
    ParseKonomiTVBS4KSpeedTestBitrateKbps,
    ResolveGarbageChunkCount,
    ResolveKonomiTVBS4KSpeedTestAudioBitrateKbps,
)


SAME_ORIGIN_HEADERS = {
    'Sec-Fetch-Site': 'same-origin',
    'Origin': 'http://testserver',
}


async def _InitializeDatabase() -> None:
    await Tortoise.init(
        db_url = 'sqlite://:memory:',
        modules = {
            'models': [
                'app.models.User',
                'app.models.RefreshToken',
                'app.models.TwitterAccount',
                'app.models.BlueskyAccount',
                'app.models.AccountLink',
            ],
        },
        timezone = 'Asia/Tokyo',
    )
    await Tortoise.generate_schemas()


async def _CloseDatabase() -> None:
    await Tortoise.close_connections()


async def _CreateUser(*, name: str, password: str = 'correct-password') -> User:
    return await User.create(
        name = name,
        password = PASSWORD_CONTEXT.hash(password),
        is_admin = False,
        client_settings = {},
    )


def _CreateApp() -> FastAPI:
    app = FastAPI()
    app.include_router(speed_test_router)
    return app


def _Bearer(user: User) -> dict[str, str]:
    return {'Authorization': f'Bearer {GenerateAccessToken(user.id, user.token_version)}'}


def _CookieFromResponse(response: HTTPXResponse) -> str:
    set_cookie = response.headers.get('set-cookie')
    assert set_cookie is not None
    parsed = SimpleCookie()
    parsed.load(set_cookie)
    morsel = parsed[KONOMITV_BS4K_SPEED_TEST_COOKIE_NAME]
    return morsel.value


def _CookieHeader(token: str) -> dict[str, str]:
    return {'Cookie': f'{KONOMITV_BS4K_SPEED_TEST_COOKIE_NAME}={token}'}


async def _CreateSession(client: HTTPXAsyncClient, user: User):
    return await client.post(
        '/api/konomitv-bs4k/speed-test/session',
        headers = {**_Bearer(user), **SAME_ORIGIN_HEADERS},
    )


def test_session_requires_authentication() -> None:
    """未認証の session 作成は 401 になる。"""

    async def Run() -> None:
        await _InitializeDatabase()
        await SPEED_TEST_SESSION_MANAGER.resetForTests()
        try:
            app = _CreateApp()
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=app),
                base_url = 'http://testserver',
            ) as client:
                response = await client.post(
                    '/api/konomitv-bs4k/speed-test/session',
                    headers = SAME_ORIGIN_HEADERS,
                )
            assert response.status_code == 401
        finally:
            await SPEED_TEST_SESSION_MANAGER.resetForTests()
            await _CloseDatabase()

    asyncio.run(Run())


def test_session_cookie_attributes_and_body_omit_token() -> None:
    """session 応答は JWT を返さず、Cookie 属性だけを公開する。"""

    async def Run() -> None:
        await _InitializeDatabase()
        await SPEED_TEST_SESSION_MANAGER.resetForTests()
        try:
            user = await _CreateUser(name='speed-user')
            app = _CreateApp()
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=app),
                base_url = 'http://testserver',
            ) as client:
                response = await _CreateSession(client, user)
            assert response.status_code == 200
            body = response.json()
            assert 'sid' not in body
            assert 'token' not in str(body).lower()
            assert body['expires_in_seconds'] == 90
            assert body['limits'] == {
                'download_streams': 5,
                'upload_streams': 3,
                'download_seconds': 10,
                'upload_seconds': 10,
            }
            assert len(body['quality_thresholds']) > 0
            set_cookie = response.headers['set-cookie']
            assert 'HttpOnly' in set_cookie
            assert 'secure' in set_cookie.lower()
            assert 'samesite=strict' in set_cookie.lower()
            assert KONOMITV_BS4K_SPEED_TEST_COOKIE_PATH in set_cookie
            assert 'Domain=' not in set_cookie
        finally:
            await SPEED_TEST_SESSION_MANAGER.resetForTests()
            await _CloseDatabase()

    asyncio.run(Run())


def test_session_rejects_wrong_fetch_metadata_and_origin() -> None:
    """Sec-Fetch-Site と Origin の不一致は 403 になる。"""

    async def Run() -> None:
        await _InitializeDatabase()
        await SPEED_TEST_SESSION_MANAGER.resetForTests()
        try:
            user = await _CreateUser(name='speed-user')
            app = _CreateApp()
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=app),
                base_url = 'http://testserver',
            ) as client:
                missing_site = await client.post(
                    '/api/konomitv-bs4k/speed-test/session',
                    headers = _Bearer(user),
                )
                assert missing_site.status_code == 403

                created = await _CreateSession(client, user)
                token = _CookieFromResponse(created)
                cross_site = await client.get(
                    '/api/konomitv-bs4k/speed-test/empty',
                    headers = {
                        **_CookieHeader(token),
                        'Sec-Fetch-Site': 'cross-site',
                    },
                )
                assert cross_site.status_code == 403

                wrong_origin = await client.post(
                    '/api/konomitv-bs4k/speed-test/empty',
                    content = b'abc',
                    headers = {
                        **_CookieHeader(token),
                        'Sec-Fetch-Site': 'same-origin',
                        'Origin': 'https://evil.example',
                    },
                )
                assert wrong_origin.status_code == 403

                invalid_origin = await client.post(
                    '/api/konomitv-bs4k/speed-test/empty',
                    content = b'abc',
                    headers = {
                        **_CookieHeader(token),
                        'Sec-Fetch-Site': 'same-origin',
                        'Origin': 'https://testserver:99999',
                    },
                )
                assert invalid_origin.status_code == 403
        finally:
            await SPEED_TEST_SESSION_MANAGER.resetForTests()
            await _CloseDatabase()

    asyncio.run(Run())


def test_jwt_typ_token_version_and_registry_are_required() -> None:
    """typ・token_version・registry のいずれかが欠けると cookie は無効になる。"""

    async def Run() -> None:
        await _InitializeDatabase()
        await SPEED_TEST_SESSION_MANAGER.resetForTests()
        try:
            user = await _CreateUser(name='speed-user')
            app = _CreateApp()
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=app),
                base_url = 'http://testserver',
            ) as client:
                created = await _CreateSession(client, user)
                token = _CookieFromResponse(created)

                access_token = GenerateAccessToken(user.id, user.token_version)
                access_as_cookie = await client.get(
                    '/api/konomitv-bs4k/speed-test/empty',
                    headers = {
                        **_CookieHeader(access_token),
                        **SAME_ORIGIN_HEADERS,
                    },
                )
                assert access_as_cookie.status_code == 401

                user.token_version += 1
                await user.save()
                obsolete = await client.get(
                    '/api/konomitv-bs4k/speed-test/empty',
                    headers = {
                        **_CookieHeader(token),
                        **SAME_ORIGIN_HEADERS,
                    },
                )
                assert obsolete.status_code == 401

                user.token_version -= 1
                await user.save()
                await SPEED_TEST_SESSION_MANAGER.resetForTests()
                missing_registry = await client.get(
                    '/api/konomitv-bs4k/speed-test/empty',
                    headers = {
                        **_CookieHeader(token),
                        **SAME_ORIGIN_HEADERS,
                    },
                )
                assert missing_registry.status_code == 401
        finally:
            await SPEED_TEST_SESSION_MANAGER.resetForTests()
            await _CloseDatabase()

    asyncio.run(Run())


def test_user_and_global_session_limits_return_retry_after() -> None:
    """同一ユーザー1枠と全体2枠を超えると 429 になる。"""

    async def Run() -> None:
        await _InitializeDatabase()
        await SPEED_TEST_SESSION_MANAGER.resetForTests()
        try:
            first = await _CreateUser(name='speed-user-1')
            second = await _CreateUser(name='speed-user-2')
            third = await _CreateUser(name='speed-user-3')
            app = _CreateApp()
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=app),
                base_url = 'http://testserver',
            ) as client:
                first_session = await _CreateSession(client, first)
                assert first_session.status_code == 200
                duplicate = await _CreateSession(client, first)
                assert duplicate.status_code == 429
                assert 'retry-after' in duplicate.headers

                second_session = await _CreateSession(client, second)
                assert second_session.status_code == 200
                third_session = await _CreateSession(client, third)
                assert third_session.status_code == 429
                assert 'retry-after' in third_session.headers
        finally:
            await SPEED_TEST_SESSION_MANAGER.resetForTests()
            await _CloseDatabase()

    asyncio.run(Run())


def test_expired_session_is_reclaimed() -> None:
    """期限切れの枠は次の作成前に回収される。"""

    async def Run() -> None:
        await _InitializeDatabase()
        await SPEED_TEST_SESSION_MANAGER.resetForTests()
        try:
            user = await _CreateUser(name='speed-user')
            session = await SPEED_TEST_SESSION_MANAGER.createSession(user)
            session.expires_at_monotonic = time_monotonic_past()
            recreated = await SPEED_TEST_SESSION_MANAGER.createSession(user)
            assert recreated.session_id != session.session_id
        finally:
            await SPEED_TEST_SESSION_MANAGER.resetForTests()
            await _CloseDatabase()

    asyncio.run(Run())


def time_monotonic_past() -> float:
    import time
    return time.monotonic() - 1


def test_garbage_chunk_count_and_exact_bytes() -> None:
    """ckSize の正規化と garbage の正確なバイト数を確認する。"""

    assert ResolveGarbageChunkCount(None) == 4
    assert ResolveGarbageChunkCount('abc') == 4
    assert ResolveGarbageChunkCount('0') == 4
    assert ResolveGarbageChunkCount('-3') == 4
    assert ResolveGarbageChunkCount('101') == KONOMITV_BS4K_SPEED_TEST_MAX_CK_SIZE
    assert ResolveGarbageChunkCount('2') == 2

    async def Run() -> None:
        await _InitializeDatabase()
        await SPEED_TEST_SESSION_MANAGER.resetForTests()
        try:
            user = await _CreateUser(name='speed-user')
            app = _CreateApp()
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=app),
                base_url = 'http://testserver',
            ) as client:
                created = await _CreateSession(client, user)
                token = _CookieFromResponse(created)
                response = await client.get(
                    '/api/konomitv-bs4k/speed-test/garbage',
                    params = {'ckSize': '2'},
                    headers = {
                        **_CookieHeader(token),
                        **SAME_ORIGIN_HEADERS,
                    },
                )
            assert response.status_code == 200
            assert response.headers['content-type'].startswith('application/octet-stream')
            assert 'no-store' in response.headers['cache-control']
            assert 'no-transform' in response.headers['cache-control']
            assert response.headers.get('content-encoding') == 'identity'
            assert len(response.content) == 2 * 1024 * 1024
        finally:
            await SPEED_TEST_SESSION_MANAGER.resetForTests()
            await _CloseDatabase()

    asyncio.run(Run())


def test_download_and_upload_stream_limits() -> None:
    """下り5・上り3を超える同時 request は 429 になる。"""

    async def Run() -> None:
        await _InitializeDatabase()
        await SPEED_TEST_SESSION_MANAGER.resetForTests()
        try:
            user = await _CreateUser(name='speed-user')
            created = await SPEED_TEST_SESSION_MANAGER.createSession(user)
            held = []
            for _ in range(5):
                context = SPEED_TEST_SESSION_MANAGER.acquireTransfer(created.session_id, 'download')
                await context.__aenter__()
                held.append(context)
            with pytest.raises(HTTPException) as download_error:
                async with SPEED_TEST_SESSION_MANAGER.acquireTransfer(created.session_id, 'download'):
                    pass
            assert download_error.value.status_code == 429
            assert download_error.value.headers is not None
            assert 'Retry-After' in download_error.value.headers
            for context in held:
                await context.__aexit__(None, None, None)

            held = []
            for _ in range(3):
                context = SPEED_TEST_SESSION_MANAGER.acquireTransfer(created.session_id, 'upload')
                await context.__aenter__()
                held.append(context)
            with pytest.raises(HTTPException) as upload_error:
                async with SPEED_TEST_SESSION_MANAGER.acquireTransfer(created.session_id, 'upload'):
                    pass
            assert upload_error.value.status_code == 429
            for context in held:
                await context.__aexit__(None, None, None)
        finally:
            await SPEED_TEST_SESSION_MANAGER.resetForTests()
            await _CloseDatabase()

    asyncio.run(Run())


def test_upload_rejects_oversized_content_length_and_chunked_body() -> None:
    """Content-Length 超過と chunked 超過は本文を溜めずに 413 になる。"""

    async def Run() -> None:
        await _InitializeDatabase()
        await SPEED_TEST_SESSION_MANAGER.resetForTests()
        try:
            user = await _CreateUser(name='speed-user')
            app = _CreateApp()
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=app),
                base_url = 'http://testserver',
            ) as client:
                created = await _CreateSession(client, user)
                token = _CookieFromResponse(created)
                too_large = await client.post(
                    '/api/konomitv-bs4k/speed-test/empty',
                    content = b'x',
                    headers = {
                        **_CookieHeader(token),
                        **SAME_ORIGIN_HEADERS,
                        'Content-Length': str(20 * 1024 * 1024 + 1),
                    },
                )
                assert too_large.status_code == 413

                async def OversizedChunkedBody() -> AsyncIterator[bytes]:
                    yield b'x' * (20 * 1024 * 1024)
                    yield b'x'

                oversized = await client.post(
                    '/api/konomitv-bs4k/speed-test/empty',
                    content = OversizedChunkedBody(),
                    headers = {
                        **_CookieHeader(token),
                        **SAME_ORIGIN_HEADERS,
                    },
                )
                assert oversized.status_code == 413

                accepted = await client.post(
                    '/api/konomitv-bs4k/speed-test/empty',
                    content = b'0123456789',
                    headers = {
                        **_CookieHeader(token),
                        **SAME_ORIGIN_HEADERS,
                    },
                )
                assert accepted.status_code == 200
        finally:
            await SPEED_TEST_SESSION_MANAGER.resetForTests()
            await _CloseDatabase()

    asyncio.run(Run())


def test_quality_thresholds_use_playback_source_and_are_monotonic() -> None:
    """閾値は再生正本と一致し、画質が上がるほど required_mbps が下がらない。"""

    thresholds = BuildKonomiTVBS4KSpeedTestQualityThresholds()
    terrestrial_avc = [
        threshold
        for threshold in thresholds
        if threshold.broadcast_type == 'Terrestrial' and threshold.codec == 'AVC'
    ]
    assert [threshold.quality for threshold in terrestrial_avc] == list(
        KONOMITV_BS4K_SPEED_TEST_TERRESTRIAL_QUALITIES
    )

    sample = next(
        threshold
        for threshold in thresholds
        if threshold.broadcast_type == 'Terrestrial' and threshold.codec == 'HEVC' and threshold.quality == '1080p'
    )
    video_bitrate = ResolveKonomiTVBS4KPlaybackVideoBitrate('1080p', 'hevc')
    audio_kbps = ResolveKonomiTVBS4KSpeedTestAudioBitrateKbps('1080p', 'hevc')
    expected = (
        ParseKonomiTVBS4KSpeedTestBitrateKbps(video_bitrate.video_bitrate_max) + audio_kbps
    ) * KONOMITV_BS4K_SPEED_TEST_VARIABLE_BITRATE_SAFETY_FACTOR / 1000.0
    assert sample.basis == 'VariableBitrate'
    assert sample.required_mbps == pytest.approx(expected)

    av1_sample = next(
        threshold
        for threshold in thresholds
        if threshold.broadcast_type == 'BS4K' and threshold.codec == 'AV1' and threshold.quality == '1080p-60fps'
    )
    av1_bitrate = ResolveKonomiTVBS4KPlaybackVideoBitrate('1080p-60fps', 'av1')
    av1_audio_kbps = ResolveKonomiTVBS4KSpeedTestAudioBitrateKbps('1080p-60fps', 'av1')
    expected_av1 = (
        ParseKonomiTVBS4KSpeedTestBitrateKbps(av1_bitrate.video_bitrate_max) + av1_audio_kbps
    ) * KONOMITV_BS4K_SPEED_TEST_VARIABLE_BITRATE_SAFETY_FACTOR / 1000.0
    assert av1_sample.basis == 'VariableBitrate'
    assert av1_sample.required_mbps == pytest.approx(expected_av1)

    # 表示順は高画質が先なので、required_mbps は広義の単調減少になる。
    # 同一画質では再生 bitrate 正本どおり AV1 < VP9 < HEVC < AVC になる。
    for broadcast_type in ('Terrestrial', 'BS4K'):
        for codec in ('AVC', 'HEVC', 'VP9', 'AV1'):
            required = [
                threshold.required_mbps
                for threshold in thresholds
                if threshold.broadcast_type == broadcast_type and threshold.codec == codec
            ]
            assert required == sorted(required, reverse=True)
        lowest = {
            codec: next(
                threshold.required_mbps
                for threshold in thresholds
                if threshold.broadcast_type == broadcast_type and
                threshold.codec == codec and
                threshold.quality.startswith('240p')
            )
            for codec in ('AVC', 'HEVC', 'VP9', 'AV1')
        }
        assert lowest['AV1'] < lowest['VP9'] < lowest['HEVC'] < lowest['AVC']


def test_delete_without_cookie_does_not_release_another_tabs_session() -> None:
    """Cookie の無い DELETE は測定中タブの枠を消さない。"""

    async def Run() -> None:
        await _InitializeDatabase()
        await SPEED_TEST_SESSION_MANAGER.resetForTests()
        try:
            user = await _CreateUser(name='speed-user')
            app = _CreateApp()
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=app),
                base_url = 'http://testserver',
            ) as client:
                created = await _CreateSession(client, user)
                assert created.status_code == 200
                deleted = await client.delete(
                    '/api/konomitv-bs4k/speed-test/session',
                    headers = {**_Bearer(user), **SAME_ORIGIN_HEADERS},
                )
                assert deleted.status_code == 204
                # 遅延した旧 DELETE が新しい session Cookie を消さないよう、Cookie 自体は変更しない。
                assert 'set-cookie' not in deleted.headers
                duplicate = await _CreateSession(client, user)
                assert duplicate.status_code == 429
        finally:
            await SPEED_TEST_SESSION_MANAGER.resetForTests()
            await _CloseDatabase()

    asyncio.run(Run())


def test_delete_matching_cookie_releases_session_without_overwriting_cookie() -> None:
    """一致する Cookie の DELETE だけが枠を解放し、応答は共有 Cookie を変更しない。"""

    async def Run() -> None:
        await _InitializeDatabase()
        await SPEED_TEST_SESSION_MANAGER.resetForTests()
        try:
            user = await _CreateUser(name='speed-user')
            app = _CreateApp()
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=app),
                base_url = 'http://testserver',
            ) as client:
                created = await _CreateSession(client, user)
                token = _CookieFromResponse(created)
                deleted = await client.delete(
                    '/api/konomitv-bs4k/speed-test/session',
                    headers = {
                        **_Bearer(user),
                        **_CookieHeader(token),
                        **SAME_ORIGIN_HEADERS,
                    },
                )
                assert deleted.status_code == 204
                assert 'set-cookie' not in deleted.headers
                recreated = await _CreateSession(client, user)
                assert recreated.status_code == 200
        finally:
            await SPEED_TEST_SESSION_MANAGER.resetForTests()
            await _CloseDatabase()

    asyncio.run(Run())


def test_akebi_https_origin_is_accepted_against_internal_http_host() -> None:
    """Akebi 配下では公開 HTTPS Origin と Host が一致すれば POST を通す。"""

    async def Run() -> None:
        await _InitializeDatabase()
        await SPEED_TEST_SESSION_MANAGER.resetForTests()
        try:
            user = await _CreateUser(name='speed-user')
            app = _CreateApp()
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=app),
                base_url = 'http://127.0.0.77:7010',
            ) as client:
                created = await client.post(
                    '/api/konomitv-bs4k/speed-test/session',
                    headers = {
                        **_Bearer(user),
                        'Sec-Fetch-Site': 'same-origin',
                        'Origin': 'https://viewer.example:7000',
                        'Host': 'viewer.example:7000',
                    },
                )
                assert created.status_code == 200
                token = _CookieFromResponse(created)
                uploaded = await client.post(
                    '/api/konomitv-bs4k/speed-test/empty',
                    content = b'0123456789',
                    headers = {
                        **_CookieHeader(token),
                        'Sec-Fetch-Site': 'same-origin',
                        'Origin': 'https://viewer.example:7000',
                        'Host': 'viewer.example:7000',
                    },
                )
                assert uploaded.status_code == 200
        finally:
            await SPEED_TEST_SESSION_MANAGER.resetForTests()
            await _CloseDatabase()

    asyncio.run(Run())


def test_released_session_cancels_inflight_transfer_and_reclaims_user_slot() -> None:
    """DELETE は in-flight 転送を停止し、完了後に同じユーザーの枠を回収する。"""

    async def Run() -> None:
        await _InitializeDatabase()
        await SPEED_TEST_SESSION_MANAGER.resetForTests()
        try:
            user = await _CreateUser(name='speed-user')
            session = await SPEED_TEST_SESSION_MANAGER.createSession(user)
            transfer_started = asyncio.Event()

            async def HoldUpload() -> None:
                async with SPEED_TEST_SESSION_MANAGER.acquireTransfer(session.session_id, 'upload'):
                    transfer_started.set()
                    await asyncio.Event().wait()

            transfer_task = asyncio.create_task(HoldUpload())
            await transfer_started.wait()
            await SPEED_TEST_SESSION_MANAGER.releaseSession(session.session_id)
            with pytest.raises(asyncio.CancelledError):
                await transfer_task
            assert await SPEED_TEST_SESSION_MANAGER.addTransferredBytes(session.session_id, 'upload', 1) is False
            recreated = await SPEED_TEST_SESSION_MANAGER.createSession(user)
            assert recreated.session_id != session.session_id
        finally:
            await SPEED_TEST_SESSION_MANAGER.resetForTests()
            await _CloseDatabase()

    asyncio.run(Run())


def test_download_window_rejects_late_requests() -> None:
    """下りは LibreSpeed の grace time を許容し、測定窓を超えた追加 request は 429 にする。"""

    async def Run() -> None:
        await _InitializeDatabase()
        await SPEED_TEST_SESSION_MANAGER.resetForTests()
        try:
            user = await _CreateUser(name='speed-user')
            session = await SPEED_TEST_SESSION_MANAGER.createSession(user)
            async with SPEED_TEST_SESSION_MANAGER.acquireTransfer(session.session_id, 'download'):
                session.download_started_monotonic = time_monotonic_past() - KONOMITV_BS4K_SPEED_TEST_DOWNLOAD_SECONDS
            # Worker は grace time 後に計測時刻をリセットするため、計測本体の10秒直後でも受け付ける。
            async with SPEED_TEST_SESSION_MANAGER.acquireTransfer(session.session_id, 'download'):
                pass
            session.download_started_monotonic = (
                time_monotonic_past() - KONOMITV_BS4K_SPEED_TEST_DOWNLOAD_WINDOW_SECONDS
            )
            with pytest.raises(HTTPException) as window_error:
                async with SPEED_TEST_SESSION_MANAGER.acquireTransfer(session.session_id, 'download'):
                    pass
            assert window_error.value.status_code == 429
        finally:
            await SPEED_TEST_SESSION_MANAGER.resetForTests()
            await _CloseDatabase()

    asyncio.run(Run())


def test_completed_request_task_releases_download_lease_without_starting_generator() -> None:
    """StreamingResponse 開始前に request Task が終わっても、done callback で下り lease を返す。"""

    async def Run() -> None:
        await _InitializeDatabase()
        await SPEED_TEST_SESSION_MANAGER.resetForTests()
        try:
            user = await _CreateUser(name='speed-user')
            session = await SPEED_TEST_SESSION_MANAGER.createSession(user)

            async def AcquireAndReturn() -> None:
                await SPEED_TEST_SESSION_MANAGER.beginTransfer(session.session_id, 'download')

            request_task = asyncio.create_task(AcquireAndReturn())
            await request_task
            # done callback が作成した非同期解放 Task まで実行する。
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert session.download_leases == {}
            await SPEED_TEST_SESSION_MANAGER.releaseSession(session.session_id)
            recreated = await SPEED_TEST_SESSION_MANAGER.createSession(user)
            assert recreated.session_id != session.session_id
        finally:
            await SPEED_TEST_SESSION_MANAGER.resetForTests()
            await _CloseDatabase()

    asyncio.run(Run())


def test_sixth_download_returns_429_before_streaming() -> None:
    """6本目の下りは StreamingResponse を開かず 429 を返す。"""

    async def Run() -> None:
        await _InitializeDatabase()
        await SPEED_TEST_SESSION_MANAGER.resetForTests()
        try:
            user = await _CreateUser(name='speed-user')
            app = _CreateApp()
            held = []
            session = await SPEED_TEST_SESSION_MANAGER.createSession(user)
            for _ in range(5):
                context = SPEED_TEST_SESSION_MANAGER.acquireTransfer(session.session_id, 'download')
                await context.__aenter__()
                held.append(context)
            token = GenerateKonomiTVBS4KSpeedTestSessionToken(user, session.session_id)
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=app),
                base_url = 'http://testserver',
            ) as client:
                sixth = await client.get(
                    '/api/konomitv-bs4k/speed-test/garbage',
                    params = {'ckSize': '1'},
                    headers = {
                        **_CookieHeader(token),
                        **SAME_ORIGIN_HEADERS,
                    },
                )
            assert sixth.status_code == 429
            for context in held:
                await context.__aexit__(None, None, None)
        finally:
            await SPEED_TEST_SESSION_MANAGER.resetForTests()
            await _CloseDatabase()

    asyncio.run(Run())


def test_speed_test_routes_are_not_exposed_on_compatibility_api() -> None:
    """互換 API には speed-test 経路が無い。"""

    compatibility_app = CreateCompatibilityAPI()
    paths = {
        getattr(route, 'path', '')
        for route in compatibility_app.routes
    }
    assert all('speed-test' not in path for path in paths)


def test_generated_session_token_uses_dedicated_typ() -> None:
    """専用 JWT は AccessToken ではなく SpeedTestSession になる。"""

    async def Run() -> None:
        await _InitializeDatabase()
        try:
            user = await _CreateUser(name='speed-user')
            token = GenerateKonomiTVBS4KSpeedTestSessionToken(user, 'abc123')
            payload = jwt.decode(
                token = token,
                key = JWT_SECRET_KEY,
                algorithms = ['HS256'],
                issuer = KONOMITV_BS4K_SPEED_TEST_JWT_ISSUER,
            )
            assert payload['typ'] == KONOMITV_BS4K_SPEED_TEST_JWT_TYPE
            assert payload['sid'] == 'abc123'
            assert payload['token_version'] == user.token_version
            assert datetime.fromtimestamp(payload['exp'], JST) <= datetime.now(JST) + timedelta(seconds=91)
        finally:
            await _CloseDatabase()

    asyncio.run(Run())
