import asyncio
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient

import app.models.Channel as channel_module
import app.models.User as user_module
import app.routers.JikkyoDependency as jikkyo_dependency_module
import app.routers.NiconicoRouter as niconico_router_module
import app.routers.VersionRouter as version_router_module
import app.utils.JikkyoClient as jikkyo_client_module
from app.CompatibilityAPI import CreateCompatibilityAPI
from app.config import ServerSettings
from app.models.Channel import Channel
from app.models.User import User
from app.routers import ChannelsRouter, NiconicoRouter, VideosRouter
from app.utils.JikkyoClient import JikkyoClient


def DisabledSettings() -> ServerSettings:
    """外部サービスへ接続しない設定を返す。"""

    return ServerSettings.model_validate({}, context={'bypass_validation': True})


def test_server_settings_jikkyo_defaults_to_disabled() -> None:
    assert DisabledSettings().general.jikkyo_enabled is False


def test_jikkyo_client_does_not_access_network_when_disabled(monkeypatch) -> None:
    settings = DisabledSettings()
    monkeypatch.setattr(jikkyo_client_module, 'Config', lambda: settings)
    network_client = Mock(side_effect=AssertionError('HTTP client must not be created'))
    monkeypatch.setattr(jikkyo_client_module, 'HTTPX_CLIENT', network_client)

    async def Run() -> None:
        await JikkyoClient.updateStatuses()
        client = JikkyoClient(network_id=15, service_id=1024)
        websocket_info = await client.fetchWebSocketInfo(None)
        comments = await client.fetchJikkyoComments(datetime.now(), datetime.now())

        assert websocket_info.watch_session_url is None
        assert websocket_info.comment_session_url is None
        assert comments.is_success is False
        assert comments.comments == []

    asyncio.run(Run())
    network_client.assert_not_called()


def test_channel_update_clears_jikkyo_state_without_external_update(monkeypatch) -> None:
    settings = DisabledSettings()
    monkeypatch.setattr(channel_module, 'Config', lambda: settings)
    external_update = AsyncMock(side_effect=AssertionError('External update must not run'))
    clear_status = AsyncMock()
    monkeypatch.setattr(JikkyoClient, 'updateStatuses', external_update)
    monkeypatch.setattr(Channel, 'clearJikkyoStatus', clear_status)

    asyncio.run(Channel.updateJikkyoStatus())

    clear_status.assert_awaited_once_with()
    external_update.assert_not_awaited()


def test_channel_clear_removes_cache_and_all_database_forces(monkeypatch) -> None:
    clear_cache = Mock()
    update_query = AsyncMock(return_value=3)
    query = SimpleNamespace(update=update_query)
    monkeypatch.setattr(JikkyoClient, 'clearStatuses', clear_cache)
    monkeypatch.setattr(Channel, 'all', staticmethod(lambda: query))

    asyncio.run(Channel.clearJikkyoStatus())

    clear_cache.assert_called_once_with()
    update_query.assert_awaited_once_with(jikkyo_force=None)


@pytest.mark.parametrize('compatibility', [False, True])
def test_jikkyo_api_routes_return_403_before_database_dependencies(monkeypatch, compatibility: bool) -> None:
    settings = DisabledSettings()
    monkeypatch.setattr(jikkyo_dependency_module, 'Config', lambda: settings)

    dependency_calls: list[str] = []

    async def GetChannelMustNotRun():
        dependency_calls.append('channel')
        raise AssertionError('Channel lookup must not run')

    async def GetRecordedProgramMustNotRun():
        dependency_calls.append('video')
        raise AssertionError('Recorded program lookup must not run')

    if compatibility is True:
        app = CreateCompatibilityAPI()
    else:
        app = FastAPI()
        app.include_router(ChannelsRouter.router)
        app.include_router(VideosRouter.router)

    app.dependency_overrides[ChannelsRouter.GetChannel] = GetChannelMustNotRun
    app.dependency_overrides[VideosRouter.GetRecordedProgram] = GetRecordedProgramMustNotRun

    async def GetResponses():
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            return (
                await client.get('/api/channels/does-not-exist/jikkyo'),
                await client.get('/api/videos/999999/jikkyo'),
            )

    channel_response, video_response = asyncio.run(GetResponses())

    assert channel_response.status_code == 403
    assert video_response.status_code == 403
    assert dependency_calls == []


def test_niconico_auth_and_callback_are_blocked_but_logout_remains_local(monkeypatch) -> None:
    settings = DisabledSettings()
    monkeypatch.setattr(jikkyo_dependency_module, 'Config', lambda: settings)
    monkeypatch.setattr(niconico_router_module, 'Config', lambda: settings)
    network_client = Mock(side_effect=AssertionError('HTTP client must not be created'))
    monkeypatch.setattr(niconico_router_module, 'HTTPX_CLIENT', network_client)

    class LocalUser:
        niconico_user_id = 1
        niconico_user_name = 'user'
        niconico_user_premium = True
        niconico_access_token = 'access-token'
        niconico_refresh_token = 'refresh-token'

        async def save(self, *, update_fields: list[str] | None = None) -> None:
            assert update_fields == [
                'niconico_user_id',
                'niconico_user_name',
                'niconico_user_premium',
                'niconico_access_token',
                'niconico_refresh_token',
                'updated_at',
            ]

    local_user = LocalUser()

    async def GetLocalUser() -> LocalUser:
        return local_user

    app = FastAPI()
    app.include_router(NiconicoRouter.router)
    app.dependency_overrides[NiconicoRouter.GetCurrentUser] = GetLocalUser

    async def GetResponses():
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            auth_response = await client.get('/api/niconico/auth')
            callback_response = await client.get(
                '/api/niconico/callback',
                params={
                    'client': 'https://client.example/',
                    'user_access_token': 'token',
                    'code': 'authorization-code',
                },
            )
            logout_response = await client.delete('/api/niconico/logout')
            return auth_response, callback_response, logout_response

    auth_response, callback_response, logout_response = asyncio.run(GetResponses())

    assert auth_response.status_code == 403
    assert callback_response.status_code == 403
    assert logout_response.status_code == 204
    assert local_user.niconico_user_id is None
    assert local_user.niconico_user_name is None
    assert local_user.niconico_user_premium is None
    assert local_user.niconico_access_token is None
    assert local_user.niconico_refresh_token is None
    network_client.assert_not_called()


def test_niconico_token_refresh_is_blocked_before_network_access(monkeypatch) -> None:
    settings = DisabledSettings()
    monkeypatch.setattr(user_module, 'Config', lambda: settings)
    network_client = Mock(side_effect=AssertionError('HTTP client must not be created'))
    monkeypatch.setattr(user_module, 'HTTPX_CLIENT', network_client)

    with pytest.raises(RuntimeError, match='実況機能はサーバー設定で無効'):
        asyncio.run(User.refreshNiconicoAccessToken(SimpleNamespace()))

    network_client.assert_not_called()


def test_version_information_reports_active_jikkyo_capability(monkeypatch) -> None:
    settings = DisabledSettings()
    monkeypatch.setattr(version_router_module, 'Config', lambda: settings)
    monkeypatch.setattr(version_router_module, 'GetPlatformEnvironment', lambda: 'Linux')
    monkeypatch.setattr(version_router_module, 'GetGitCommit', AsyncMock(return_value='test-commit'))
    monkeypatch.setattr(version_router_module, 'latest_version', '0.0.0')
    monkeypatch.setattr(version_router_module, 'latest_version_updated_at', time.time())

    response = asyncio.run(version_router_module.VersionInformationAPI())

    assert response['jikkyo_enabled'] is False
