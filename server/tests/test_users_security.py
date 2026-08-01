"""SEC-013 認証ハードニングの回帰テスト。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient
from jose import jwt
from tortoise import Tortoise

import app.routers.UsersRouter as users_router_module
from app.constants import JST, JWT_SECRET_KEY, PASSWORD_CONTEXT
from app.models.User import User
from app.routers.UsersRouter import (
    REFRESH_TOKEN_COOKIE_NAME,
    GenerateAccessToken,
    GetCurrentUser,
    UserUpdateAPI,
)
from app.schemas import UserUpdateRequest
from app.utils.AuthSecurity import (
    DUMMY_PASSWORD_HASH,
    LOGIN_ATTEMPT_LIMITER,
    GetUsernameRateLimitKey,
    LoginAttemptLimiter,
)


async def _InitializeDatabase() -> None:
    await Tortoise.init(
        db_url = 'sqlite://:memory:',
        modules = {
            'models': [
                'app.models.User',
                'app.models.RefreshToken',
            ],
        },
        timezone = 'Asia/Tokyo',
    )
    await Tortoise.generate_schemas()


async def _CloseDatabase() -> None:
    await Tortoise.close_connections()


async def _CreateUser(*, name: str = 'security-user', password: str = 'correct-password') -> User:
    return await User.create(
        name = name,
        password = PASSWORD_CONTEXT.hash(password),
        is_admin = True,
        client_settings = {},
    )


def _CreateApp() -> FastAPI:
    app = FastAPI()
    app.include_router(users_router_module.router)
    return app


def test_login_does_not_enumerate_users(monkeypatch: pytest.MonkeyPatch) -> None:
    """存在しないユーザーと誤パスワードは同一応答で、固定ハッシュも検証する。"""

    async def Run() -> None:
        await _InitializeDatabase()
        await LOGIN_ATTEMPT_LIMITER.Reset()
        try:
            await _CreateUser()
            verified_hashes: list[str] = []

            def FakeVerify(password: str, password_hash: str) -> bool:
                del password
                verified_hashes.append(password_hash)
                return False

            monkeypatch.setattr(users_router_module.PASSWORD_CONTEXT, 'verify', FakeVerify)
            app = _CreateApp()
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=app),
                base_url = 'http://testserver',
            ) as client:
                missing_user_response = await client.post(
                    '/api/users/token',
                    data = {'username': 'missing-user', 'password': 'wrong-password'},
                )
                wrong_password_response = await client.post(
                    '/api/users/token',
                    data = {'username': 'security-user', 'password': 'wrong-password'},
                )

            assert missing_user_response.status_code == 401
            assert wrong_password_response.status_code == 401
            assert missing_user_response.json() == {'detail': 'Invalid credentials'}
            assert wrong_password_response.json() == {'detail': 'Invalid credentials'}
            assert missing_user_response.headers['www-authenticate'] == 'Bearer'
            assert wrong_password_response.headers['www-authenticate'] == 'Bearer'
            assert DUMMY_PASSWORD_HASH in verified_hashes
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_login_is_rate_limited_by_username_and_ip() -> None:
    """ユーザー名軸とIP軸のどちらでも試行を制限する。"""

    async def Run() -> None:
        clock_value = 0.0
        limiter = LoginAttemptLimiter(
            ip_capacity = 3.0,
            username_capacity = 2.0,
            refill_interval = 60.0,
            clock = lambda: clock_value,
        )
        username_key = GetUsernameRateLimitKey('same-user')

        assert await limiter.Check('198.51.100.1', username_key) is None
        await limiter.RecordFailure('198.51.100.1', username_key)
        await limiter.RecordFailure('198.51.100.1', username_key)
        assert await limiter.Check('198.51.100.2', username_key) is not None
        await limiter.RecordFailure('198.51.100.1', GetUsernameRateLimitKey('other-user'))
        assert await limiter.Check('198.51.100.1', GetUsernameRateLimitKey('other-user')) is not None

        # 十分に時間が空けばバックオフ段階とトークンを回復する
        clock_value = 180.0
        assert await limiter.Check('198.51.100.2', username_key) is None

    asyncio.run(Run())


def test_login_endpoint_returns_429_after_failed_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """ログインAPIが一定回数の失敗後に429を返す。"""

    async def Run() -> None:
        await _InitializeDatabase()
        await LOGIN_ATTEMPT_LIMITER.Reset()
        try:
            def FakeVerify(password: str, password_hash: str) -> bool:
                del password, password_hash
                return False

            monkeypatch.setattr(users_router_module.PASSWORD_CONTEXT, 'verify', FakeVerify)
            app = _CreateApp()
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=app),
                base_url = 'http://testserver',
            ) as client:
                statuses = []
                for _ in range(6):
                    response = await client.post(
                        '/api/users/token',
                        data = {'username': 'rate-limited-user', 'password': 'wrong-password'},
                    )
                    statuses.append(response.status_code)
                final_response = response

            assert statuses[:5] == [401, 401, 401, 401, 401]
            assert statuses[5] == 429
            assert final_response.json() == {'detail': 'Too many login attempts'}
            assert int(final_response.headers['retry-after']) >= 1
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_password_change_invalidates_old_access_token() -> None:
    """パスワード変更でtoken_versionが増え、旧JWTが使えなくなる。"""

    async def Run() -> None:
        await _InitializeDatabase()
        try:
            user = await _CreateUser()
            old_token = GenerateAccessToken(user.id, user.token_version)
            assert (await GetCurrentUser(token=old_token)).id == user.id

            await UserUpdateAPI(
                user_update_request = UserUpdateRequest(password = 'new-password'),
                current_user = user,
            )

            with pytest.raises(HTTPException) as ex_info:
                await GetCurrentUser(token=old_token)
            assert ex_info.value.status_code == 401
            assert ex_info.value.detail == 'Access token is invalid'

            updated_user = await User.get(id=user.id)
            assert updated_user.token_version == 1
            new_token = GenerateAccessToken(updated_user.id, updated_user.token_version)
            assert (await GetCurrentUser(token=new_token)).id == user.id
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_legacy_access_token_is_rejected() -> None:
    """token_versionを含まない既存JWTは再ログインを要求する。"""

    async def Run() -> None:
        await _InitializeDatabase()
        try:
            user = await _CreateUser()
            issued_at = datetime.now(JST)
            legacy_token = jwt.encode(
                claims = {
                    'iss': 'KonomiTV Server',
                    'typ': 'AccessToken',
                    'sub': str(user.id),
                    'iat': issued_at,
                    'exp': issued_at + timedelta(days=180),
                    'jti': 'legacy-token',
                },
                key = JWT_SECRET_KEY,
                algorithm = 'HS256',
            )

            with pytest.raises(HTTPException) as ex_info:
                await GetCurrentUser(token=legacy_token)
            assert ex_info.value.status_code == 401
            assert ex_info.value.detail == 'Access token is invalid'
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_refresh_token_rotates_and_reuse_revokes_family() -> None:
    """更新トークンは一度だけ使え、再利用時にはファミリー全体を失効させる。"""

    async def Run() -> None:
        await _InitializeDatabase()
        try:
            await _CreateUser()
            app = _CreateApp()
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=app),
                base_url = 'http://testserver',
            ) as client:
                login_response = await client.post(
                    '/api/users/token',
                    data = {'username': 'security-user', 'password': 'correct-password'},
                )
                assert login_response.status_code == 200, login_response.text
                access_claims = jwt.decode(
                    login_response.json()['access_token'],
                    JWT_SECRET_KEY,
                    algorithms = ['HS256'],
                    issuer = 'KonomiTV Server',
                )
                assert access_claims['token_version'] == 0
                assert access_claims['exp'] - access_claims['iat'] == 15 * 60
                old_refresh_token = client.cookies.get(REFRESH_TOKEN_COOKIE_NAME)
                assert old_refresh_token is not None
                assert 'httponly' in login_response.headers['set-cookie'].lower()

                refresh_response = await client.post('/api/users/refresh')
                assert refresh_response.status_code == 200, refresh_response.text
                new_refresh_token = client.cookies.get(REFRESH_TOKEN_COOKIE_NAME)
                assert new_refresh_token is not None
                assert new_refresh_token != old_refresh_token

            async with HTTPXAsyncClient(
                transport = ASGITransport(app=app),
                base_url = 'http://testserver',
                cookies = {REFRESH_TOKEN_COOKIE_NAME: old_refresh_token},
            ) as replay_client:
                replay_response = await replay_client.post('/api/users/refresh')
                assert replay_response.status_code == 401
                assert 'max-age=0' in replay_response.headers['set-cookie'].lower()

            async with HTTPXAsyncClient(
                transport = ASGITransport(app=app),
                base_url = 'http://testserver',
                cookies = {REFRESH_TOKEN_COOKIE_NAME: new_refresh_token},
            ) as current_client:
                family_response = await current_client.post('/api/users/refresh')
                assert family_response.status_code == 401
        finally:
            await _CloseDatabase()

    asyncio.run(Run())
