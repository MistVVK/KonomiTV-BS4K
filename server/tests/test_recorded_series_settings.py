import asyncio
import stat
from pathlib import Path
from typing import Literal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient
from pydantic import ValidationError

from app.metadata.RecordedSeriesCandidates import (
    AIChoiceResult,
    RecordedSeriesProgramPrompt,
    SeriesChoiceCandidate,
)
from app.metadata.RecordedSeriesResolver import RecordedSeriesBackfillAccepted
from app.metadata.RecordedSeriesSettings import (
    RecordedSeriesSettings,
    RecordedSeriesSettingsStore,
)
from app.routers import RecordedSeriesRouter


def ConfigureTemporaryStore(monkeypatch: pytest.MonkeyPatch, temporary_directory: Path) -> None:
    """テストごとに独立した設定と API キーの保存先を構成する。"""

    monkeypatch.setattr(
        RecordedSeriesSettingsStore,
        'SETTINGS_PATH',
        temporary_directory / 'recorded-series-settings.json',
    )
    monkeypatch.setattr(
        RecordedSeriesSettingsStore,
        'API_KEY_PATH',
        temporary_directory / 'secrets' / 'recorded-series-api.key',
    )


def CreateAdminApp() -> FastAPI:
    """管理者認証をテスト用に置き換えた FastAPI アプリを作成する。"""

    async def GetAdminUser() -> object:
        return object()

    app = FastAPI()
    app.include_router(RecordedSeriesRouter.router)
    app.dependency_overrides[RecordedSeriesRouter.GetCurrentAdminUser] = GetAdminUser
    return app


def test_recorded_series_settings_defaults_are_safe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ConfigureTemporaryStore(monkeypatch, tmp_path)

    settings = RecordedSeriesSettingsStore.getSettings()

    assert settings.enabled is True
    assert settings.ai_enabled is False
    assert settings.api_base_url == 'https://api.openai.com/v1'
    assert settings.model == 'gpt-5.6-luna'
    assert settings.daily_ai_request_limit == 20
    assert RecordedSeriesSettingsStore.getAPIKey() is None


@pytest.mark.parametrize('limit', [0, 1, 1000])
def test_daily_ai_request_limit_accepts_supported_boundaries(limit: int) -> None:
    assert RecordedSeriesSettings(daily_ai_request_limit=limit).daily_ai_request_limit == limit


@pytest.mark.parametrize('limit', [-1, 1001])
def test_daily_ai_request_limit_rejects_out_of_range_values(limit: int) -> None:
    with pytest.raises(ValidationError):
        RecordedSeriesSettings(daily_ai_request_limit=limit)


def test_recorded_series_settings_store_separates_and_protects_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ConfigureTemporaryStore(monkeypatch, tmp_path)
    api_key = 'test-secret-api-key'
    settings = RecordedSeriesSettings(
        enabled = True,
        ai_enabled = True,
        api_base_url = 'http://127.0.0.1:11434/v1/',
        model = 'custom/model',
        daily_ai_request_limit = 37,
    )

    RecordedSeriesSettingsStore.saveSettings(settings, api_key=api_key)

    assert RecordedSeriesSettingsStore.getSettings().api_base_url == 'http://127.0.0.1:11434/v1'
    assert RecordedSeriesSettingsStore.getAPIKey() == api_key
    assert api_key not in RecordedSeriesSettingsStore.SETTINGS_PATH.read_text(encoding='utf-8')
    assert stat.S_IMODE(RecordedSeriesSettingsStore.SETTINGS_PATH.stat().st_mode) == 0o600
    assert stat.S_IMODE(RecordedSeriesSettingsStore.API_KEY_PATH.stat().st_mode) == 0o600
    assert stat.S_IMODE(RecordedSeriesSettingsStore.API_KEY_PATH.parent.stat().st_mode) == 0o700

    # API キーを省略した設定更新は、保存済みのキーを維持する。
    RecordedSeriesSettingsStore.saveSettings(settings.model_copy(update={'model': 'another-model'}))
    assert RecordedSeriesSettingsStore.getAPIKey() == api_key


def test_settings_write_failure_rolls_back_new_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ConfigureTemporaryStore(monkeypatch, tmp_path)
    original_settings = RecordedSeriesSettings(model='old-model')
    RecordedSeriesSettingsStore.saveSettings(original_settings, api_key='old-key')
    original_write_atomic = RecordedSeriesSettingsStore._writeAtomic

    def WriteAtomic(
        cls: type[RecordedSeriesSettingsStore],
        destination: Path,
        content: str,
    ) -> None:
        del cls
        if destination == RecordedSeriesSettingsStore.SETTINGS_PATH:
            raise OSError('simulated settings write failure')
        original_write_atomic(destination, content)

    monkeypatch.setattr(RecordedSeriesSettingsStore, '_writeAtomic', classmethod(WriteAtomic))

    with pytest.raises(OSError, match='simulated settings write failure'):
        RecordedSeriesSettingsStore.saveSettings(
            original_settings.model_copy(update={'model': 'new-model'}),
            api_key='new-key',
        )

    assert RecordedSeriesSettingsStore.getAPIKey() == 'old-key'
    assert RecordedSeriesSettingsStore.getSettings().model == 'old-model'


def test_settings_and_api_key_are_read_as_one_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(api_base_url='https://provider.example/v1', model='paired-model')
    RecordedSeriesSettingsStore.saveSettings(settings, api_key='paired-key')

    loaded_settings, loaded_api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()

    assert loaded_settings == settings
    assert loaded_api_key == 'paired-key'


def test_api_key_is_bound_to_the_provider_url_across_a_partial_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """2ファイル更新中の停止を模擬し、別providerへのキー転送を防ぐ。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    provider_a = RecordedSeriesSettings(api_base_url='https://provider-a.example/v1')
    provider_b = RecordedSeriesSettings(api_base_url='https://provider-b.example/v1')
    RecordedSeriesSettingsStore.saveSettings(provider_a, api_key='provider-a-key')

    # クラッシュで設定側だけが別URLになった状態でも、キーは返さない。
    RecordedSeriesSettingsStore.SETTINGS_PATH.write_text(
        provider_b.model_dump_json(indent=4) + '\n',
        encoding='utf-8',
    )

    loaded_settings, loaded_api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()
    assert loaded_settings == provider_b
    assert loaded_api_key is None


def test_recorded_series_settings_api_never_returns_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ConfigureTemporaryStore(monkeypatch, tmp_path)
    app = CreateAdminApp()
    api_key = 'api-key-that-must-never-be-returned'

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            update_response = await client.put(
                '/api/recorded-series/settings',
                json={
                    'enabled': True,
                    'ai_enabled': True,
                    'api_base_url': 'https://compatible.example/v1',
                    'model': 'gpt-5-nano',
                    'daily_ai_request_limit': 0,
                    'api_key': api_key,
                },
            )
            assert update_response.status_code == 204
            assert update_response.headers['cache-control'] == 'no-store'

            get_response = await client.get('/api/recorded-series/settings')
            assert get_response.status_code == 200
            assert get_response.headers['cache-control'] == 'no-store'
            assert get_response.json() == {
                'enabled': True,
                'ai_enabled': True,
                'api_base_url': 'https://compatible.example/v1',
                'model': 'gpt-5-nano',
                'daily_ai_request_limit': 0,
                'api_key_configured': True,
            }
            assert api_key not in get_response.text

    asyncio.run(Run())


def test_recorded_series_settings_validation_does_not_echo_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ConfigureTemporaryStore(monkeypatch, tmp_path)
    app = CreateAdminApp()
    api_key = 'invalid-request-secret-api-key'

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.put(
                '/api/recorded-series/settings',
                json={
                    'enabled': True,
                    'ai_enabled': True,
                    'api_base_url': 'not-a-url',
                    'model': 'gpt-5-nano',
                    'daily_ai_request_limit': 0,
                    'api_key': api_key,
                },
            )

        assert response.status_code == 422
        assert response.headers['cache-control'] == 'no-store'
        assert api_key not in response.text
        assert RecordedSeriesSettingsStore.getAPIKey() is None

    asyncio.run(Run())


def test_recorded_series_api_key_delete_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ConfigureTemporaryStore(monkeypatch, tmp_path)
    RecordedSeriesSettingsStore.saveSettings(RecordedSeriesSettings(), api_key='deletable-api-key')
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            first_response = await client.delete('/api/recorded-series/settings/api-key')
            second_response = await client.delete('/api/recorded-series/settings/api-key')
            get_response = await client.get('/api/recorded-series/settings')

        assert first_response.status_code == 204
        assert second_response.status_code == 204
        assert first_response.headers['cache-control'] == 'no-store'
        assert get_response.json()['api_key_configured'] is False
        assert RecordedSeriesSettingsStore.getAPIKey() is None

    asyncio.run(Run())


def test_recorded_series_settings_api_requires_authentication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ConfigureTemporaryStore(monkeypatch, tmp_path)
    app = FastAPI()
    app.include_router(RecordedSeriesRouter.router)

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/api/recorded-series/settings')

        assert response.status_code == 401

    asyncio.run(Run())


def test_connection_test_uses_draft_key_without_saving_or_returning_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ConfigureTemporaryStore(monkeypatch, tmp_path)
    app = CreateAdminApp()
    draft_api_key = 'connection-test-key-that-must-stay-secret'
    captured_api_key: str | None = None

    async def SelectCandidate(
        *,
        api_base_url: str,
        api_key: str | None,
        model: str,
        program: RecordedSeriesProgramPrompt,
        candidates: list[SeriesChoiceCandidate],
        minimum_confidence: float,
    ) -> AIChoiceResult:
        del api_base_url, model, program, candidates, minimum_confidence
        nonlocal captured_api_key
        captured_api_key = api_key
        return AIChoiceResult(
            choice_id='unresolved',
            confidence=1.0,
            model='compatible-model',
            prompt_tokens=12,
            completion_tokens=4,
            http_status=200,
            latency_ms=34,
        )

    async def CreateAudit(**kwargs: object) -> object:
        del kwargs
        return object()

    monkeypatch.setattr(RecordedSeriesRouter, 'SelectRecordedSeriesCandidate', SelectCandidate)
    monkeypatch.setattr(RecordedSeriesRouter.RecordedSeriesAIRequest, 'create', CreateAudit)

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(
                '/api/recorded-series/settings/test',
                json={
                    'api_base_url': 'http://127.0.0.1:11434/v1',
                    'model': 'custom-model',
                    'api_key': draft_api_key,
                },
            )

        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert response.json() == {
            'success': True,
            'latency_ms': 34,
            'model': 'compatible-model',
            'message': '接続と候補制約付き応答の検証に成功しました。',
        }
        assert draft_api_key not in response.text

    asyncio.run(Run())
    assert captured_api_key == draft_api_key
    assert RecordedSeriesSettingsStore.getAPIKey() is None


def test_connection_test_does_not_send_saved_key_to_a_different_base_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ConfigureTemporaryStore(monkeypatch, tmp_path)
    RecordedSeriesSettingsStore.saveSettings(
        RecordedSeriesSettings(api_base_url='https://provider-a.example/v1'),
        api_key='provider-a-secret',
    )
    app = CreateAdminApp()
    captured_api_key: str | None = 'not-called'

    async def SelectCandidate(
        *,
        api_base_url: str,
        api_key: str | None,
        model: str,
        program: RecordedSeriesProgramPrompt,
        candidates: list[SeriesChoiceCandidate],
        minimum_confidence: float,
    ) -> AIChoiceResult:
        del model, program, candidates, minimum_confidence
        nonlocal captured_api_key
        assert api_base_url == 'https://provider-b.example/v1'
        captured_api_key = api_key
        return AIChoiceResult(
            choice_id='unresolved',
            confidence=1.0,
            model='compatible-model',
            prompt_tokens=1,
            completion_tokens=1,
            http_status=200,
            latency_ms=1,
        )

    async def CreateAudit(**kwargs: object) -> object:
        del kwargs
        return object()

    monkeypatch.setattr(RecordedSeriesRouter, 'SelectRecordedSeriesCandidate', SelectCandidate)
    monkeypatch.setattr(RecordedSeriesRouter.RecordedSeriesAIRequest, 'create', CreateAudit)

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(
                '/api/recorded-series/settings/test',
                json={
                    'api_base_url': 'https://provider-b.example/v1',
                    'model': 'compatible-model',
                },
            )
        assert response.status_code == 200

    asyncio.run(Run())
    assert captured_api_key is None


def test_settings_update_rejects_sending_saved_key_to_a_different_base_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """URLだけの変更でprovider Aの保存キーをprovider Bへ転送しない。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    provider_a = RecordedSeriesSettings(
        ai_enabled=True,
        api_base_url='https://provider-a.example/v1',
        model='provider-a-model',
    )
    RecordedSeriesSettingsStore.saveSettings(provider_a, api_key='provider-a-secret')
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            rejected_response = await client.put(
                '/api/recorded-series/settings',
                json={
                    'enabled': True,
                    'ai_enabled': True,
                    'api_base_url': 'https://provider-b.example/v1',
                    'model': 'provider-b-model',
                    'daily_ai_request_limit': 20,
                },
            )
            assert RecordedSeriesSettingsStore.getSettings() == provider_a
            assert RecordedSeriesSettingsStore.getAPIKey() == 'provider-a-secret'
            replaced_response = await client.put(
                '/api/recorded-series/settings',
                json={
                    'enabled': True,
                    'ai_enabled': True,
                    'api_base_url': 'https://provider-b.example/v1',
                    'model': 'provider-b-model',
                    'daily_ai_request_limit': 20,
                    'api_key': 'provider-b-secret',
                },
            )

        assert rejected_response.status_code == 422
        assert rejected_response.headers['cache-control'] == 'no-store'
        assert 'provider-a-secret' not in rejected_response.text
        assert replaced_response.status_code == 204

    asyncio.run(Run())
    assert RecordedSeriesSettingsStore.getSettings().api_base_url == 'https://provider-b.example/v1'
    assert RecordedSeriesSettingsStore.getAPIKey() == 'provider-b-secret'


def test_status_and_backfill_endpoints_return_task_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ConfigureTemporaryStore(monkeypatch, tmp_path)
    app = CreateAdminApp()

    async def GetStatus() -> dict[str, int | str | bool | None]:
        return {
            'total': 160,
            'pending': 10,
            'resolved': 100,
            'not_series': 20,
            'needs_review': 29,
            'failed': 1,
            'ai_requests_today': 3,
            'last_run_at': '2026-07-21T12:34:56+09:00',
            'is_running': False,
        }

    received_force_values: list[bool] = []

    async def StartBackfill(
        *,
        trigger: Literal['Manual', 'StartupBackfill'],
        force: bool = False,
    ) -> RecordedSeriesBackfillAccepted:
        assert trigger == 'Manual'
        received_force_values.append(force)
        return RecordedSeriesBackfillAccepted(execution_id=42, reused=False)

    monkeypatch.setattr(RecordedSeriesRouter.RecordedSeriesResolver, 'getStatus', GetStatus)
    monkeypatch.setattr(RecordedSeriesRouter.RecordedSeriesResolver, 'startBackfill', StartBackfill)

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            status_response = await client.get('/api/recorded-series/status')
            backfill_response = await client.post('/api/recorded-series/backfill', json={'force': True})

        assert status_response.status_code == 200
        assert status_response.json()['total'] == 160
        assert backfill_response.status_code == 202
        assert backfill_response.json() == {'execution_id': 42, 'reused': False}

    asyncio.run(Run())
    assert received_force_values == [True]
