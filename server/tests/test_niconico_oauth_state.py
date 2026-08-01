"""ニコニコ OAuth state と callback の信頼境界を保護する SEC-001 / SEC-002 回帰テスト。"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import timedelta
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient
from tortoise import Tortoise, timezone

import app.routers.NiconicoRouter as niconico_router_module
from app.constants import PASSWORD_CONTEXT
from app.models.NiconicoOAuthState import NiconicoOAuthState
from app.models.User import User
from app.routers.UsersRouter import GenerateAccessToken, GetCurrentUser


async def _InitializeDatabase() -> None:
    await Tortoise.init(
        db_url='sqlite://:memory:',
        modules={'models': [
            'app.models.User',
            'app.models.NiconicoOAuthState',
        ]},
        timezone='Asia/Tokyo',
    )
    await Tortoise.generate_schemas()


async def _CloseDatabase() -> None:
    await Tortoise.close_connections()


async def _CreateUser(*, name: str = 'oauth-user') -> User:
    return await User.create(
        name=name,
        password=PASSWORD_CONTEXT.hash('password-for-tests'),
        is_admin=True,
        client_settings={},
    )


def test_niconico_oauth_state_issue_and_consume_are_one_time() -> None:
    """state は短命で、一度消費したら再利用できない。"""

    async def Run() -> None:
        await _InitializeDatabase()
        try:
            user = await _CreateUser()
            state_id, code_challenge = await NiconicoOAuthState.issue(
                user_id=user.id,
                client_url='https://client.example',
            )
            assert state_id
            assert code_challenge
            assert len(NiconicoOAuthState.hashStateId(state_id)) == 64

            record = await NiconicoOAuthState.consume(state_id)
            assert record.user_id == user.id
            assert record.client_url == 'https://client.example'
            assert record.consumed_at is not None
            assert record.code_verifier is not None

            stored_record = await NiconicoOAuthState.get(id=record.id)
            assert stored_record.code_verifier is None

            with pytest.raises(ValueError, match=r'invalid|expired|consumed'):
                await NiconicoOAuthState.consume(state_id)
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_niconico_oauth_state_rejects_expired() -> None:
    """期限切れ state は消費できない。"""

    async def Run() -> None:
        await _InitializeDatabase()
        try:
            user = await _CreateUser()
            state_id, _ = await NiconicoOAuthState.issue(
                user_id=user.id,
                client_url='https://client.example',
                ttl=timedelta(seconds=1),
            )
            # 期限を過去へずらす
            state_hash = NiconicoOAuthState.hashStateId(state_id)
            await NiconicoOAuthState.filter(state_hash=state_hash).update(
                expires_at=timezone.now() - timedelta(seconds=1),
            )
            with pytest.raises(ValueError):
                await NiconicoOAuthState.consume(state_id)
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_niconico_oauth_state_cleans_up_expired_verifiers() -> None:
    """期限切れ state の PKCE verifier は発行時と明示クリーンアップ時に消去する。"""

    async def Run() -> None:
        await _InitializeDatabase()
        try:
            abandoned_user = await _CreateUser(name='abandoned-oauth-user')
            abandoned_state_id, _ = await NiconicoOAuthState.issue(
                user_id=abandoned_user.id,
                client_url='https://client.example/',
            )
            abandoned_state_hash = NiconicoOAuthState.hashStateId(abandoned_state_id)
            await NiconicoOAuthState.filter(state_hash=abandoned_state_hash).update(
                expires_at=timezone.now() - timedelta(seconds=1),
            )

            # 別ユーザーの state 発行を契機に、放置された期限切れ verifier も回収する。
            active_user = await _CreateUser(name='active-oauth-user')
            active_state_id, _ = await NiconicoOAuthState.issue(
                user_id=active_user.id,
                client_url='https://client.example/',
            )
            abandoned_record = await NiconicoOAuthState.get(user_id=abandoned_user.id)
            active_record = await NiconicoOAuthState.get(user_id=active_user.id)
            assert abandoned_record.code_verifier is None
            assert active_record.code_verifier is not None
            assert await NiconicoOAuthState.all().count() == 2

            # 定期タスクと同じ明示呼び出しでも、期限切れ行だけを1件回収する。
            await NiconicoOAuthState.filter(user_id=active_user.id).update(
                expires_at=timezone.now() - timedelta(seconds=1),
            )
            assert await NiconicoOAuthState.cleanupExpired() == 1
            await active_record.refresh_from_db()
            assert active_record.code_verifier is None

            with pytest.raises(ValueError):
                await NiconicoOAuthState.consume(active_state_id)
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_niconico_oauth_state_reissue_keeps_one_row_per_user() -> None:
    """大量・並行発行でもユーザーごとの state 行を 1 件に保ち、旧 state を無効化する。"""

    async def Run() -> None:
        await _InitializeDatabase()
        try:
            user = await _CreateUser()

            # 逐次連打では常に同じユーザー行を置換し、DB ファイルを発行数に比例させない。
            serial_states: list[str] = []
            for _ in range(50):
                state_id, _ = await NiconicoOAuthState.issue(
                    user_id=user.id,
                    client_url='https://client.example',
                )
                serial_states.append(state_id)
            assert await NiconicoOAuthState.filter(user_id=user.id).count() == 1
            with pytest.raises(ValueError):
                await NiconicoOAuthState.consume(serial_states[0])

            # 同時発行でも DB で最後に採用された state だけが消費できる。
            concurrent_states = await asyncio.gather(*(
                NiconicoOAuthState.issue(
                    user_id=user.id,
                    client_url='https://client.example',
                )
                for _ in range(10)
            ))
            assert await NiconicoOAuthState.filter(user_id=user.id).count() == 1

            consumed_count = 0
            for state_id, _ in concurrent_states:
                try:
                    await NiconicoOAuthState.consume(state_id)
                    consumed_count += 1
                except ValueError:
                    pass
            assert consumed_count == 1
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_niconico_auth_url_does_not_embed_login_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    """/api/niconico/auth の OAuth state にログイン JWT を載せない。"""

    async def Run() -> None:
        await _InitializeDatabase()
        try:
            user = await _CreateUser()
            access_token = GenerateAccessToken(user.id)

            app = FastAPI()
            app.include_router(niconico_router_module.router)

            async def OverrideUser() -> User:
                return user

            app.dependency_overrides[GetCurrentUser] = OverrideUser
            app.dependency_overrides[niconico_router_module.GetCurrentUser] = OverrideUser

            async with HTTPXAsyncClient(
                transport=ASGITransport(app=app),
                base_url='https://tv.example',
                headers={
                    'Authorization': f'Bearer {access_token}',
                    'Origin': 'https://client.example',
                },
            ) as client:
                response = await client.get('/api/niconico/auth')

            assert response.status_code == 200, response.text
            authorization_url = response.json()['authorization_url']
            query = parse_qs(urlparse(authorization_url).query)
            state_b64 = query['state'][0]
            # 発行時に padding を落としているので復元する
            padded = state_b64 + ('=' * (-len(state_b64) % 4))
            state = json.loads(base64.b64decode(padded).decode('utf-8'))

            assert 'user_access_token' not in state
            assert 'oauth_state' in state
            assert state['oauth_state']
            assert access_token not in authorization_url
            assert access_token not in state['oauth_state']
            # JWT の典型的な3セグメント形が state に混入していない
            assert state['oauth_state'].count('.') != 2
            assert state['client'] == 'https://client.example'
            assert state['server'].startswith('https://')
            assert query['code_challenge_method'] == ['S256']
            assert len(query['code_challenge'][0]) == 43

            # DB に state のハッシュと、外部 URL に出さない PKCE verifier だけが残る。
            rows = await NiconicoOAuthState.all()
            assert len(rows) == 1
            assert rows[0].state_hash == NiconicoOAuthState.hashStateId(state['oauth_state'])
            assert rows[0].user_id == user.id
            assert rows[0].code_verifier is not None
            assert query['code_challenge'][0] == NiconicoOAuthState.buildCodeChallenge(rows[0].code_verifier)
            assert rows[0].code_verifier not in authorization_url
            assert rows[0].code_verifier not in state.values()
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_niconico_callback_uses_oauth_state_not_query_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    """callback は oauth_state でユーザーを特定し、user_access_token クエリを無視する。"""

    async def Run() -> None:
        await _InitializeDatabase()
        try:
            user = await _CreateUser()
            state_id, _ = await NiconicoOAuthState.issue(
                user_id=user.id,
                client_url='https://client.example',
            )
            issued_state = await NiconicoOAuthState.get(user_id=user.id)
            expected_code_verifier = issued_state.code_verifier
            assert expected_code_verifier is not None
            login_jwt = GenerateAccessToken(user.id)

            # ニコニコ token / user API をスタブ
            token_response = Mock()
            token_response.status_code = 200
            token_response.json = Mock(return_value={
                'access_token': 'nico-access',
                'refresh_token': 'nico-refresh',
                # sub だけ使えればよい。署名検証は get_unverified_claims
                'id_token': (
                    'eyJhbGciOiJub25lIn0.'
                    + base64.urlsafe_b64encode(b'{"sub":"12345"}').decode().rstrip('=')
                    + '.'
                ),
            })
            user_response = Mock()
            user_response.status_code = 200
            user_response.json = Mock(return_value={
                'data': {
                    'user': {
                        'nickname': 'NicoUser',
                        'isPremium': True,
                    },
                },
            })
            token_api_request = Mock()

            class _FakeClient:
                async def __aenter__(self) -> _FakeClient:
                    return self

                async def __aexit__(self, *args: object) -> None:
                    return None

                async def post(self, *args: object, **kwargs: object) -> Mock:
                    token_api_request(*args, **kwargs)
                    return token_response

                async def get(self, *args: object, **kwargs: object) -> Mock:
                    return user_response

            monkeypatch.setattr(niconico_router_module, 'HTTPX_CLIENT', lambda: _FakeClient())
            monkeypatch.setattr(niconico_router_module, 'Interlaced', lambda _index: 'client-secret')

            app = FastAPI()
            app.include_router(niconico_router_module.router)

            async with HTTPXAsyncClient(
                transport=ASGITransport(app=app),
                base_url='https://tv.example',
            ) as client:
                # 旧実装互換の user_access_token を付けても無視され、oauth_state が使われる
                response = await client.get(
                    '/api/niconico/callback',
                    params={
                        'oauth_state': state_id,
                        'client': 'https://evil.example/',
                        'user_access_token': login_jwt,
                        'code': 'authorization-code',
                    },
                )

            assert response.status_code == 303, response.text
            # クエリ client の改ざんは使わず、発行時 client_url だけへ固定結果を返す。
            assert response.headers['Location'] == 'https://client.example/settings/jikkyo#result=Success'
            assert 'evil.example' not in response.headers['Location']
            assert response.headers['Cache-Control'] == 'no-store'
            assert response.headers['Referrer-Policy'] == 'no-referrer'

            await user.refresh_from_db()
            assert user.niconico_access_token == 'nico-access'
            assert user.niconico_refresh_token == 'nico-refresh'
            assert user.niconico_user_id == 12345
            assert user.niconico_user_name == 'NicoUser'
            assert user.niconico_user_premium is True
            assert token_api_request.call_args.kwargs['data']['code_verifier'] == expected_code_verifier

            consumed_state = await NiconicoOAuthState.get(user_id=user.id)
            assert consumed_state.code_verifier is None

            # ワンタイム
            async with HTTPXAsyncClient(
                transport=ASGITransport(app=app),
                base_url='https://tv.example',
            ) as client:
                replay = await client.get(
                    '/api/niconico/callback',
                    params={
                        'oauth_state': state_id,
                        'code': 'authorization-code-2',
                    },
                )
            assert replay.status_code == 401
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_niconico_callback_consumes_state_when_authorization_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """認可拒否 callback でも state と PKCE verifier を再利用不能にする。"""

    async def Run() -> None:
        await _InitializeDatabase()
        try:
            user = await _CreateUser()
            state_id, _ = await NiconicoOAuthState.issue(
                user_id=user.id,
                client_url='https://client.example',
            )
            network_client = Mock(side_effect=AssertionError('HTTP client must not be created'))
            monkeypatch.setattr(niconico_router_module, 'HTTPX_CLIENT', network_client)

            app = FastAPI()
            app.include_router(niconico_router_module.router)

            async with HTTPXAsyncClient(
                transport=ASGITransport(app=app),
                base_url='https://tv.example',
            ) as client:
                response = await client.get(
                    '/api/niconico/callback',
                    params={
                        'oauth_state': state_id,
                        'client': 'https://evil.example/',
                        'error': 'access_denied',
                    },
                )

            assert response.status_code == 303
            assert response.headers['Location'] == 'https://client.example/settings/jikkyo#result=AccessDenied'
            assert 'evil.example' not in response.headers['Location']
            network_client.assert_not_called()

            consumed_state = await NiconicoOAuthState.get(user_id=user.id)
            assert consumed_state.consumed_at is not None
            assert consumed_state.code_verifier is None
            with pytest.raises(ValueError):
                await NiconicoOAuthState.consume(state_id)
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_niconico_callback_rejects_missing_oauth_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """oauth_state 無し（旧 JWT クエリのみ）では連携できない。"""

    async def Run() -> None:
        await _InitializeDatabase()
        try:
            user = await _CreateUser()
            login_jwt = GenerateAccessToken(user.id)
            network_client = Mock(side_effect=AssertionError('HTTP client must not be created'))
            monkeypatch.setattr(niconico_router_module, 'HTTPX_CLIENT', network_client)

            app = FastAPI()
            app.include_router(niconico_router_module.router)

            async with HTTPXAsyncClient(
                transport=ASGITransport(app=app),
                base_url='https://tv.example',
            ) as client:
                response = await client.get(
                    '/api/niconico/callback',
                    params={
                        'client': 'https://client.example/',
                        'user_access_token': login_jwt,
                        'code': 'authorization-code',
                    },
                )

            assert response.status_code == 401
            assert response.text == 'OAuth state is missing'
            assert 'Location' not in response.headers
            assert 'client.example' not in response.text
            assert response.headers['Content-Type'].startswith('text/plain')
            assert response.headers['Content-Security-Policy'] == (
                "default-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            )
            network_client.assert_not_called()
            await user.refresh_from_db()
            assert user.niconico_access_token is None
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


@pytest.mark.parametrize((
    'client_origin',
    'expected_origin',
), [
    ('https://client.example', 'https://client.example'),
    ('https://CLIENT.EXAMPLE/', 'https://client.example'),
    ('https://client.example:443', 'https://client.example'),
    ('https://client.example:7001/', 'https://client.example:7001'),
    ('https://127.0.0.1:7001', 'https://127.0.0.1:7001'),
    ('https://[2001:db8::1]:7001', 'https://[2001:db8::1]:7001'),
])
def test_niconico_client_origin_is_normalized(client_origin: str, expected_origin: str) -> None:
    """認証開始時のクライアント Origin を location.origin と同じ形式へ正規化する。"""

    assert niconico_router_module._NormalizeClientOrigin(client_origin) == expected_origin


@pytest.mark.parametrize('client_origin', [
    'http://client.example',
    'https://user:password@client.example',
    'https://client.example/settings/jikkyo',
    'https://client.example/?query=1',
    'https://client.example/#fragment',
    'https://client.example`;.invalid',
    'javascript:alert(1)',
    '//client.example',
    'https://client.example:invalid',
    'https://client.example:70000',
    'null',
    '',
])
def test_niconico_client_origin_rejects_unsafe_values(client_origin: str) -> None:
    """Origin 以外の URL や JavaScript 構文文字を含む値を拒否する。"""

    with pytest.raises(ValueError):
        niconico_router_module._NormalizeClientOrigin(client_origin)


def test_niconico_callback_does_not_reflect_external_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """外部 error を HTML・Location・ログへ反映せず、固定結果だけをクライアントへ返す。"""

    async def Run() -> None:
        await _InitializeDatabase()
        try:
            user = await _CreateUser()
            state_id, _ = await NiconicoOAuthState.issue(
                user_id=user.id,
                client_url='https://client.example',
            )
            network_client = Mock(side_effect=AssertionError('HTTP client must not be created'))
            warning_log = Mock()
            monkeypatch.setattr(niconico_router_module, 'HTTPX_CLIENT', network_client)
            monkeypatch.setattr(niconico_router_module.logging, 'warning', warning_log)

            app = FastAPI()
            app.include_router(niconico_router_module.router)
            attack = "</script><script>document.body.dataset.pwn='1'</script>\r\nFAKE LOG"

            async with HTTPXAsyncClient(
                transport=ASGITransport(app=app),
                base_url='https://tv.example',
            ) as client:
                response = await client.get(
                    '/api/niconico/callback',
                    params={
                        'oauth_state': state_id,
                        'client': 'https://evil.example/`;document.body.dataset.pwn=1;//',
                        'error': attack,
                    },
                )

            assert response.status_code == 303
            assert response.headers['Location'] == (
                'https://client.example/settings/jikkyo#result=AuthorizationError'
            )
            assert attack not in response.text
            assert attack not in response.headers['Location']
            assert all(attack not in str(call) for call in warning_log.call_args_list)
            network_client.assert_not_called()
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_niconico_callback_rejects_corrupted_stored_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB の保存値が壊れていても、不正 Origin へリダイレクトしない。"""

    async def Run() -> None:
        await _InitializeDatabase()
        try:
            user = await _CreateUser()
            state_id, _ = await NiconicoOAuthState.issue(
                user_id=user.id,
                client_url='https://client.example/`;document.body.dataset.pwn=1;//',
            )
            network_client = Mock(side_effect=AssertionError('HTTP client must not be created'))
            monkeypatch.setattr(niconico_router_module, 'HTTPX_CLIENT', network_client)

            app = FastAPI()
            app.include_router(niconico_router_module.router)

            async with HTTPXAsyncClient(
                transport=ASGITransport(app=app),
                base_url='https://tv.example',
            ) as client:
                response = await client.get(
                    '/api/niconico/callback',
                    params={'oauth_state': state_id, 'code': 'authorization-code'},
                )

            assert response.status_code == 500
            assert response.text == 'Stored client Origin is invalid'
            assert 'Location' not in response.headers
            assert 'document.body' not in response.text
            network_client.assert_not_called()
        finally:
            await _CloseDatabase()

    asyncio.run(Run())
