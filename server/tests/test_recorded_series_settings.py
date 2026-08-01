# pyright: reportPrivateUsage=false

import asyncio
import json
import os
import stat
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient
from pydantic import ValidationError

from app.metadata.ai import recorded_series_ai as RecordedSeriesAIModule
from app.metadata.ai.backends import (
    ConnectionTestCheck,
    ConnectionTestResult,
    EpisodeLookupConnectionChecks,
)
from app.metadata.ai.KonomiTVBS4KACPCredentials import KonomiTVBS4KACPCredentials
from app.metadata.ai.recorded_series_ai import (
    get_episode_lookup_provider_fingerprint,
    has_episode_lookup_capability_proof,
    invalidate_episode_lookup_capability_fingerprint,
    record_episode_lookup_capability_proof,
    reset_episode_lookup_capability_proofs_for_tests,
)
from app.metadata.RecordedEpisodeAutomation import RecordedEpisodeBackfillAccepted
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
    # 能力証明もテストごとに独立したファイルへ書き、本番 DATA_DIR を汚さない。
    monkeypatch.setattr(
        RecordedSeriesAIModule,
        'EPISODE_LOOKUP_CAPABILITY_PROOFS_PATH',
        temporary_directory / 'recorded-series-episode-lookup-proofs.json',
    )
    reset_episode_lookup_capability_proofs_for_tests()


def SuccessfulEpisodeLookupConnectionChecks() -> EpisodeLookupConnectionChecks:
    """Router/API 契約テストで使用する成功時の固定6項目。"""

    return EpisodeLookupConnectionChecks(
        backend_connection=ConnectionTestCheck(status='Passed', message='接続済み'),
        web_search=ConnectionTestCheck(status='Passed', message='検索済み'),
        source_url=ConnectionTestCheck(status='Passed', message='URL取得済み'),
        strict_schema=ConnectionTestCheck(status='Passed', message='schema検証済み'),
        timeout_cancel=ConnectionTestCheck(status='NotRun', message='未実行'),
        permission_policy=ConnectionTestCheck(status='NotApplicable', message='対象外'),
    )


def test_episode_lookup_capability_proof_is_bound_to_provider_and_revoked_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """能力証明は endpoint/model/key fingerprint に一致し、再試験失敗で失効する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(
        ai_backend='OpenAICompatible',
        api_base_url='https://capability-proof.example/v1',
        model='proof-model',
    )
    success = ConnectionTestResult(
        success=True,
        latency_ms=1,
        model='proof-model',
        message='verified',
        checks=SuccessfulEpisodeLookupConnectionChecks(),
    )
    failure = ConnectionTestResult(
        success=False,
        latency_ms=1,
        model='proof-model',
        message='failed',
        checks=EpisodeLookupConnectionChecks(
            backend_connection=ConnectionTestCheck(status='Passed', message='ok'),
            web_search=ConnectionTestCheck(status='Failed', message='failed'),
            source_url=ConnectionTestCheck(status='NotRun', message='not run'),
            strict_schema=ConnectionTestCheck(status='NotRun', message='not run'),
            timeout_cancel=ConnectionTestCheck(status='NotRun', message='not run'),
            permission_policy=ConnectionTestCheck(status='NotApplicable', message='n/a'),
        ),
    )

    assert record_episode_lookup_capability_proof(settings, 'proof-key', success) is True
    assert has_episode_lookup_capability_proof(settings, 'proof-key') is True
    assert has_episode_lookup_capability_proof(settings, 'different-key') is False
    assert record_episode_lookup_capability_proof(settings, 'proof-key', failure) is False
    assert has_episode_lookup_capability_proof(settings, 'proof-key') is False


def test_acp_episode_lookup_proof_binds_project_location_and_credential_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ACP proof は Gemini 実行先と固定 profile の認証世代をまたいで再利用しない。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    generation = 'credential-generation-a'

    def GetCredentialGeneration(
        _cls: type[KonomiTVBS4KACPCredentials],
        _provider: Literal['codex', 'grok', 'google'],
    ) -> str:
        return generation

    monkeypatch.setattr(
        KonomiTVBS4KACPCredentials,
        'getCredentialGeneration',
        classmethod(GetCredentialGeneration),
    )
    success = ConnectionTestResult(
        success=True,
        latency_ms=1,
        model='gemini-test',
        message='verified',
        checks=SuccessfulEpisodeLookupConnectionChecks(),
    )
    settings = RecordedSeriesSettings(
        ai_backend='AcpGemini',
        google_cloud_project='project-a',
        google_cloud_location='asia-northeast1',
    )
    assert record_episode_lookup_capability_proof(settings, None, success) is True
    assert has_episode_lookup_capability_proof(settings, None) is True
    assert has_episode_lookup_capability_proof(
        settings.model_copy(update={'google_cloud_project': 'project-b'}),
        None,
    ) is False
    assert has_episode_lookup_capability_proof(
        settings.model_copy(update={'google_cloud_location': 'us-central1'}),
        None,
    ) is False

    tested_fingerprint = get_episode_lookup_provider_fingerprint(settings, None)
    generation = 'credential-generation-b'
    assert has_episode_lookup_capability_proof(settings, None) is False
    assert (
        record_episode_lookup_capability_proof(
            settings,
            None,
            success,
            tested_provider_fingerprint=tested_fingerprint,
        )
        is False
    )
    assert has_episode_lookup_capability_proof(settings, None) is False
    assert record_episode_lookup_capability_proof(settings, None, success) is True
    assert has_episode_lookup_capability_proof(settings, None) is True
    invalidate_episode_lookup_capability_fingerprint(tested_fingerprint)
    assert has_episode_lookup_capability_proof(settings, None) is True
    generation = 'credential-generation-a'
    assert has_episode_lookup_capability_proof(settings, None) is False


def test_episode_lookup_capability_proof_survives_process_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """接続試験の能力証明はディスク永続化され、再読込後も再試験なしで有効。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(
        ai_backend='OpenAICompatible',
        api_base_url='https://proof-persist.example/v1',
        model='persist-model',
    )
    success = ConnectionTestResult(
        success=True,
        latency_ms=1,
        model='persist-model',
        message='verified',
        checks=SuccessfulEpisodeLookupConnectionChecks(),
    )
    assert (
        record_episode_lookup_capability_proof(settings, 'persist-key', success) is True
    )
    proof_path = tmp_path / 'recorded-series-episode-lookup-proofs.json'
    assert proof_path.is_file() is True

    # プロセス再起動を、メモリ状態の破棄 + 同一ファイルからの再読込で模擬する。
    reset_episode_lookup_capability_proofs_for_tests()
    assert has_episode_lookup_capability_proof(settings, 'persist-key') is True
    assert has_episode_lookup_capability_proof(settings, 'other-key') is False


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
    assert settings.ai_candidate_selection_enabled is True
    assert settings.ai_episode_number_search_enabled is False
    assert settings.ai_episode_number_acceptance_mode == 'Always'
    assert settings.api_base_url == 'https://api.openai.com/v1'
    assert settings.model == 'gpt-5.6-luna'
    assert settings.daily_ai_request_limit == 20
    assert RecordedSeriesSettingsStore.getAPIKey() is None


def test_legacy_settings_use_new_child_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ConfigureTemporaryStore(monkeypatch, tmp_path)
    RecordedSeriesSettingsStore.SETTINGS_PATH.write_text(
        json.dumps({
            'enabled': True,
            'ai_enabled': False,
            'api_base_url': 'https://api.openai.com/v1',
            'model': 'legacy-model',
            'daily_ai_request_limit': 20,
        }),
        encoding='utf-8',
    )

    settings = RecordedSeriesSettingsStore.getSettings()

    assert settings.ai_enabled is False
    assert settings.ai_candidate_selection_enabled is True
    assert settings.ai_episode_number_search_enabled is False
    assert settings.ai_episode_number_acceptance_mode == 'Always'


def test_legacy_candidate_selection_switch_is_read_but_not_saved_or_returned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """廃止 field は旧 JSON / 旧 PUT を受理しても runtime 設定へ持ち越さない。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    RecordedSeriesSettingsStore.SETTINGS_PATH.write_text(
        json.dumps({
            'enabled': True,
            'ai_enabled': True,
            'ai_candidate_selection_enabled': False,
        }),
        encoding='utf-8',
    )

    settings = RecordedSeriesSettingsStore.getSettings()
    assert settings.ai_candidate_selection_enabled is False
    RecordedSeriesSettingsStore.saveSettings(settings)
    saved = json.loads(RecordedSeriesSettingsStore.SETTINGS_PATH.read_text(encoding='utf-8'))
    assert 'ai_candidate_selection_enabled' not in saved

    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.put(
                '/api/recorded-series/settings',
                json={
                    **settings.model_dump(mode='json'),
                    'ai_candidate_selection_enabled': False,
                },
            )
            assert response.status_code == 204
            get_response = await client.get('/api/recorded-series/settings')
            assert get_response.status_code == 200
            assert 'ai_candidate_selection_enabled' not in get_response.json()

    asyncio.run(Run())


def test_legacy_acp_home_and_share_mode_keys_are_ignored_only_when_reading_saved_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """未リリース WIP の旧4キーを保存済み JSON から除外し、API schema には残さない。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    RecordedSeriesSettingsStore.SETTINGS_PATH.write_text(
        json.dumps({
            **RecordedSeriesSettings().model_dump(mode='json'),
            'acp_user_codex_home': '/home/user/.codex',
            'acp_user_grok_home': '/home/user/.grok',
            'acp_user_gemini_home': '/home/user/.gemini',
            'acp_auth_share_mode': 'Symlink',
        }),
        encoding='utf-8',
    )

    settings = RecordedSeriesSettingsStore.getSettings()

    assert settings == RecordedSeriesSettings()
    for legacy_key in (
        'acp_user_codex_home',
        'acp_user_grok_home',
        'acp_user_gemini_home',
        'acp_auth_share_mode',
    ):
        assert legacy_key not in settings.model_dump()
        with pytest.raises(ValidationError):
            RecordedSeriesSettings.model_validate({legacy_key: '/unsafe/legacy-value'})


def test_gemini_settings_require_explicit_project_and_location() -> None:
    """Gemini / Vertex AI は資格情報以外の環境依存値を明示設定する。"""

    with pytest.raises(ValidationError):
        RecordedSeriesSettings(ai_backend='AcpGemini')
    with pytest.raises(ValidationError):
        RecordedSeriesSettings(
            ai_backend='AcpGemini',
            google_cloud_project='fixture-project',
        )

    settings = RecordedSeriesSettings(
        ai_backend='AcpGemini',
        google_cloud_project=' fixture-project ',
        google_cloud_location=' asia-northeast1 ',
    )

    assert settings.google_cloud_project == 'fixture-project'
    assert settings.google_cloud_location == 'asia-northeast1'


@pytest.mark.parametrize(
    ('backend', 'backend_settings'),
    [
        ('AcpCodex', {}),
        ('AcpGrok', {}),
        (
            'AcpGemini',
            {
                'google_cloud_project': 'fixture-project',
                'google_cloud_location': 'asia-northeast1',
            },
        ),
    ],
)
def test_fixed_acp_presets_preserve_episode_lookup_setting(
    backend: str,
    backend_settings: dict[str, str],
) -> None:
    """固定 ACP preset でも話数検索設定を強制 OFF にしない。"""

    settings = RecordedSeriesSettings.model_validate({
        'ai_backend': backend,
        'ai_episode_number_search_enabled': True,
        **backend_settings,
    })

    assert settings.ai_episode_number_search_enabled is True


def test_legacy_acp_custom_saved_settings_are_disabled_and_migrated_safely(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """旧 Custom 保存設定は任意コマンドを再実行せず、AI 停止状態へ移行する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    RecordedSeriesSettingsStore.SETTINGS_PATH.write_text(
        json.dumps({
            **RecordedSeriesSettings(ai_enabled=True).model_dump(mode='json'),
            'ai_backend': 'AcpCustom',
            'acp_cwd': '/home/user/work',
            'acp_command': '/opt/custom-acp-agent',
            'acp_args': ['--acp'],
            'acp_env': {'TERM': 'xterm'},
        }),
        encoding='utf-8',
    )

    settings = RecordedSeriesSettingsStore.getSettings()

    assert settings.ai_backend == 'OpenAICompatible'
    assert settings.ai_enabled is False
    for legacy_key in ('acp_cwd', 'acp_command', 'acp_args', 'acp_env'):
        assert legacy_key not in settings.model_dump()


def test_acp_custom_api_payload_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """廃止後の API は旧 backend と任意コマンド項目を受理しない。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.put(
                '/api/recorded-series/settings',
                json={
                    'ai_backend': 'AcpCustom',
                    'acp_command': '/opt/custom-acp-agent',
                },
            )

        assert response.status_code == 422
        assert RecordedSeriesSettingsStore.SETTINGS_PATH.exists() is False

    asyncio.run(Run())


@pytest.mark.parametrize('limit', [0, 1, 1000])
def test_daily_ai_request_limit_accepts_supported_boundaries(limit: int) -> None:
    assert RecordedSeriesSettings(daily_ai_request_limit=limit).daily_ai_request_limit == limit


@pytest.mark.parametrize('limit', [-1, 1001])
def test_daily_ai_request_limit_rejects_out_of_range_values(limit: int) -> None:
    with pytest.raises(ValidationError):
        RecordedSeriesSettings(daily_ai_request_limit=limit)


def test_episode_number_acceptance_mode_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        RecordedSeriesSettings(ai_episode_number_acceptance_mode='Unknown')  # type: ignore[arg-type]


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
    # map 形式では provider B のエントリが存在しないため None が返る。
    RecordedSeriesSettingsStore.SETTINGS_PATH.write_text(
        provider_b.model_dump_json(indent=4) + '\n',
        encoding='utf-8',
    )

    loaded_settings, loaded_api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()
    assert loaded_settings == provider_b
    assert loaded_api_key is None
    # provider A のキーは map 内で保持され続けている
    assert RecordedSeriesSettingsStore.getAPIKeyForURL('https://provider-a.example/v1') == 'provider-a-key'


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
                'ai_episode_number_search_enabled': False,
                'ai_episode_number_acceptance_mode': 'Always',
                'daily_ai_request_limit': 0,
                'ai_backend': 'OpenAICompatible',
                'api_base_url': 'https://compatible.example/v1',
                'model': 'gpt-5-nano',
                'acp_model': None,
                'acp_reasoning_effort': None,
                'acp_timeout_sec': 120,
                'google_cloud_project': None,
                'google_cloud_location': None,
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

    async def TestConnection(
        capability: str,
        *,
        settings: RecordedSeriesSettings | None = None,
        api_key: str | None = None,
    ) -> ConnectionTestResult:
        assert capability == 'CandidateSelection'
        assert settings is not None
        assert settings.api_base_url == 'http://127.0.0.1:11434/v1'
        nonlocal captured_api_key
        captured_api_key = api_key
        return ConnectionTestResult(
            success=True,
            latency_ms=34,
            model='compatible-model',
            message='Chat Completions 互換を確認しました。',
            prompt_tokens=12,
            completion_tokens=4,
            http_status=200,
            selected_choice_id='unresolved',
        )

    async def CreateAudit(**kwargs: object) -> object:
        del kwargs
        return object()

    monkeypatch.setattr(RecordedSeriesRouter, 'test_connection', TestConnection)
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
            'message': 'Chat Completions 互換を確認しました。',
            'checks': None,
        }
        assert draft_api_key not in response.text

    asyncio.run(Run())
    assert captured_api_key == draft_api_key
    assert RecordedSeriesSettingsStore.getAPIKey() is None


@pytest.mark.parametrize('tested_capability', ['CandidateSelection', 'EpisodeLookup'])
def test_acp_connection_test_uses_unsaved_backend_draft_with_fixed_preset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tested_capability: Literal['CandidateSelection', 'EpisodeLookup'],
) -> None:
    """ACP の各機能試験は保存済み backend ではなく画面の全ドラフトを使用する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    RecordedSeriesSettingsStore.saveSettings(RecordedSeriesSettings())
    captured_settings: RecordedSeriesSettings | None = None
    audit_records: list[dict[str, object]] = []

    async def TestConnection(
        capability: str,
        *,
        settings: RecordedSeriesSettings | None = None,
        api_key: str | None = None,
    ) -> ConnectionTestResult:
        nonlocal captured_settings
        assert capability == tested_capability
        assert api_key is None
        captured_settings = settings
        return ConnectionTestResult(
            success=True,
            latency_ms=5,
            model='acp:codex:test-model',
            message='ok',
            checks=(
                SuccessfulEpisodeLookupConnectionChecks()
                if tested_capability == 'EpisodeLookup'
                else None
            ),
        )

    async def CreateAudit(**kwargs: object) -> object:
        audit_records.append(kwargs)
        return object()

    monkeypatch.setattr(RecordedSeriesRouter, 'test_connection', TestConnection)
    monkeypatch.setattr(RecordedSeriesRouter.RecordedSeriesAIRequest, 'create', CreateAudit)
    app = CreateAdminApp()

    async def Scenario() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://testserver',
        ) as client:
            response = await client.post('/api/recorded-series/settings/test', json={
                'capability': tested_capability,
                'ai_backend': 'AcpCodex',
                'acp_model': 'test-model',
                'acp_timeout_sec': 90,
            })
            assert response.status_code == 200
            assert response.json()['success'] is True
            assert response.json()['model'] == 'acp:codex:test-model'
            if tested_capability == 'EpisodeLookup':
                assert response.json()['checks']['backend_connection']['status'] == 'Passed'
                assert response.json()['checks']['timeout_cancel']['status'] == 'NotRun'
            else:
                assert response.json()['checks'] is None

    asyncio.run(Scenario())
    assert captured_settings is not None
    assert captured_settings.ai_backend == 'AcpCodex'
    assert captured_settings.acp_timeout_sec == 90
    assert audit_records[0]['purpose'] == 'ConnectionTest'
    assert audit_records[0]['status'] == 'Succeeded'


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

    async def TestConnection(
        capability: str,
        *,
        settings: RecordedSeriesSettings | None = None,
        api_key: str | None = None,
    ) -> ConnectionTestResult:
        assert capability == 'CandidateSelection'
        assert settings is not None
        nonlocal captured_api_key
        assert settings.api_base_url == 'https://provider-b.example/v1'
        captured_api_key = api_key
        return ConnectionTestResult(
            success=True,
            latency_ms=1,
            model='compatible-model',
            message='ok',
            prompt_tokens=1,
            completion_tokens=1,
            http_status=200,
            selected_choice_id='unresolved',
        )

    async def CreateAudit(**kwargs: object) -> object:
        del kwargs
        return object()

    monkeypatch.setattr(RecordedSeriesRouter, 'test_connection', TestConnection)
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


def test_connection_test_can_validate_episode_web_search_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """話数側の接続試験はResponses Web Searchを呼び、候補選択とは別に監査する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    app = CreateAdminApp()
    captured_settings: RecordedSeriesSettings | None = None
    captured_api_key: str | None = None
    audit_records: list[dict[str, object]] = []

    async def TestConnection(
        capability: str,
        *,
        settings: RecordedSeriesSettings | None = None,
        api_key: str | None = None,
    ) -> ConnectionTestResult:
        assert capability == 'EpisodeLookup'
        nonlocal captured_settings, captured_api_key
        captured_settings = settings
        captured_api_key = api_key
        return ConnectionTestResult(
            success=True,
            latency_ms=48,
            model='episode-model-response',
            message='Responses API + Web Search 互換を確認しました。',
            checks=SuccessfulEpisodeLookupConnectionChecks(),
            prompt_tokens=20,
            completion_tokens=5,
            http_status=200,
            selected_choice_id='S1E3',
        )

    async def CreateAudit(**kwargs: object) -> object:
        audit_records.append(kwargs)
        return object()

    monkeypatch.setattr(RecordedSeriesRouter, 'test_connection', TestConnection)
    monkeypatch.setattr(RecordedSeriesRouter.RecordedSeriesAIRequest, 'create', CreateAudit)

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(
                '/api/recorded-series/settings/test',
                json={
                    'capability': 'EpisodeLookup',
                    'api_base_url': 'https://api.openai.com/v1',
                    'model': 'episode-model',
                    'api_key': 'episode-test-key',
                },
            )

        assert response.status_code == 200
        assert response.json() == {
            'success': True,
            'latency_ms': 48,
            'model': 'episode-model-response',
            'message': 'Responses API + Web Search 互換を確認しました。',
            'checks': {
                'backend_connection': {'status': 'Passed', 'message': '接続済み'},
                'web_search': {'status': 'Passed', 'message': '検索済み'},
                'source_url': {'status': 'Passed', 'message': 'URL取得済み'},
                'strict_schema': {'status': 'Passed', 'message': 'schema検証済み'},
                'timeout_cancel': {'status': 'NotRun', 'message': '未実行'},
                'permission_policy': {'status': 'NotApplicable', 'message': '対象外'},
            },
        }

    asyncio.run(Run())
    assert captured_settings is not None
    assert captured_settings.model == 'episode-model'
    assert captured_api_key == 'episode-test-key'
    assert has_episode_lookup_capability_proof(
        captured_settings,
        captured_api_key,
    ) is True
    assert audit_records[0]['candidate_ids'] == ['episode-lookup']
    assert audit_records[0]['selected_choice_id'] == 'S1E3'


def test_episode_lookup_proof_is_not_left_when_connection_audit_save_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """接続成功でも監査保存に失敗した試験は単票再検索の proof にしない。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    app = CreateAdminApp()
    settings = RecordedSeriesSettings(
        api_base_url='https://audit-failure.example/v1',
        model='audit-failure-model',
    )
    connection_result = ConnectionTestResult(
        success=True,
        latency_ms=1,
        model='audit-failure-model',
        message='verified',
        checks=SuccessfulEpisodeLookupConnectionChecks(),
    )
    assert (
        record_episode_lookup_capability_proof(
            settings,
            'audit-failure-key',
            connection_result,
        )
        is True
    )

    async def TestConnection(
        capability: str,
        *,
        settings: RecordedSeriesSettings | None = None,
        api_key: str | None = None,
    ) -> ConnectionTestResult:
        assert capability == 'EpisodeLookup'
        assert settings is not None
        assert api_key == 'audit-failure-key'
        return connection_result

    async def FailAudit(**_kwargs: object) -> object:
        raise RuntimeError('simulated connection audit failure')

    monkeypatch.setattr(RecordedSeriesRouter, 'test_connection', TestConnection)
    monkeypatch.setattr(
        RecordedSeriesRouter.RecordedSeriesAIRequest,
        'create',
        FailAudit,
    )

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            with pytest.raises(
                RuntimeError,
                match='simulated connection audit failure',
            ):
                await client.post(
                    '/api/recorded-series/settings/test',
                    json={
                        'capability': 'EpisodeLookup',
                        'api_base_url': settings.api_base_url,
                        'model': settings.model,
                        'api_key': 'audit-failure-key',
                    },
                )

    asyncio.run(Run())
    assert (
        has_episode_lookup_capability_proof(
            settings,
            'audit-failure-key',
        )
        is False
    )


def test_connection_test_rejects_a_result_from_a_stale_provider_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """facade が実使用した世代と現在世代が違う接続成功は proof にしない。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    app = CreateAdminApp()
    used_provider_fingerprint = 'b' * 64
    audit_records: list[dict[str, object]] = []

    async def TestConnection(
        capability: str,
        *,
        settings: RecordedSeriesSettings | None = None,
        api_key: str | None = None,
    ) -> ConnectionTestResult:
        assert capability == 'EpisodeLookup'
        assert settings is not None
        assert api_key == 'stale-generation-key'
        return ConnectionTestResult(
            success=True,
            latency_ms=1,
            model='stale-generation-model',
            message='verified old generation',
            checks=SuccessfulEpisodeLookupConnectionChecks(),
            provider_fingerprint=used_provider_fingerprint,
        )

    class FakeAudit:
        """接続試験 API が確定後に更新する監査レコードを模擬する。"""

        def __init__(self, record: dict[str, object]) -> None:
            """監査レコードの可変フィールドを保持する。

            Args:
                record: create 時の監査フィールド。

            Returns:
                None
            """

            self._record = record
            self.status = cast(
                Literal['Pending', 'Succeeded', 'Rejected', 'Failed'],
                record['status'],
            )
            self.error_code = cast(str | None, record['error_code'])

        async def save(self, *, update_fields: list[str]) -> None:
            """指定された監査フィールドをテスト記録へ反映する。

            Args:
                update_fields: API が更新対象に指定したフィールド名。

            Returns:
                None
            """

            assert update_fields == ['status', 'error_code']
            self._record['status'] = self.status
            self._record['error_code'] = self.error_code

    async def CreateAudit(**kwargs: object) -> FakeAudit:
        audit_records.append(kwargs)
        return FakeAudit(kwargs)

    monkeypatch.setattr(RecordedSeriesRouter, 'test_connection', TestConnection)
    monkeypatch.setattr(
        RecordedSeriesRouter.RecordedSeriesAIRequest,
        'create',
        CreateAudit,
    )

    async def Run() -> dict[str, object]:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.post(
                '/api/recorded-series/settings/test',
                json={
                    'capability': 'EpisodeLookup',
                    'api_base_url': 'https://stale-generation.example/v1',
                    'model': 'stale-generation-model',
                    'api_key': 'stale-generation-key',
                },
            )
        assert response.status_code == 200
        return response.json()

    response = asyncio.run(Run())
    assert response['success'] is False
    assert response['message'] == (
        '接続試験中に AI 設定または認証状態が変更されました。もう一度試してください。'
    )
    assert audit_records[0]['status'] == 'Failed'
    assert audit_records[0]['error_code'] == 'ConnectionTestStateChanged'
    current_settings = RecordedSeriesSettings(
        api_base_url='https://stale-generation.example/v1',
        model='stale-generation-model',
    )
    assert (
        has_episode_lookup_capability_proof(
            current_settings,
            'stale-generation-key',
        )
        is False
    )


def test_episode_assignment_api_returns_complete_safe_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """話数一覧 API は全フィールドを順序どおり返し、DB の生エラーを公開しない。"""

    class FakeQuery:
        def __init__(self, rows: list[object], *, row_exists: bool = True) -> None:
            self._rows = rows
            self._row_exists = row_exists

        async def exists(self) -> bool:
            return self._row_exists

        async def all(self) -> list[object]:
            return self._rows

        def prefetch_related(self, *relations: str) -> 'FakeQuery':
            del relations
            return self

        def order_by(self, *fields: str) -> 'FakeQuery':
            del fields
            return self

        def __await__(self):  # type: ignore[no-untyped-def]
            return self.all().__await__()

    episode = SimpleNamespace(id=100, season_number=1, episode_number=Decimal('12'))
    program = SimpleNamespace(
        id=1000,
        title='話数一覧作品 第12話',
        subtitle='テスト話',
        episode_number='#12',
        start_time=datetime.fromisoformat('2026-07-27T20:00:00+09:00'),
        channel_id='NID4-SID101',
        channel=SimpleNamespace(name='テストチャンネル'),
        series_episode_id=100,
    )
    resolution = SimpleNamespace(
        recorded_program_id=1000,
        status='Failed',
        source='WebSearch',
        lookup_outcome='SearchFailed',
        proposed_season_number=1,
        proposed_episode_number=Decimal('13'),
        confidence=0.42,
        web_search_performed=True,
        citations=[
            {
                'url': 'https://example.com/episode-13',
                'title': '第13話',
            },
        ],
        rationale_short=None,
        manual_season_number=None,
        manual_episode_number=None,
        manual_status=None,
        error_code='HTTP500',
        error_message='/home/user/private/profile: raw provider response',
    )

    def FilterSeries(**kwargs: object) -> FakeQuery:
        return FakeQuery([], row_exists=kwargs == {'id': 10})

    def FilterEpisodes(**kwargs: object) -> FakeQuery:
        del kwargs
        return FakeQuery([episode])

    def FilterPrograms(**kwargs: object) -> FakeQuery:
        del kwargs
        return FakeQuery([program])

    def FilterResolutions(**kwargs: object) -> FakeQuery:
        del kwargs
        return FakeQuery([resolution])

    monkeypatch.setattr(
        RecordedSeriesRouter.Series,
        'filter',
        staticmethod(FilterSeries),
    )
    monkeypatch.setattr(
        RecordedSeriesRouter.SeriesEpisode,
        'filter',
        staticmethod(FilterEpisodes),
    )
    monkeypatch.setattr(
        RecordedSeriesRouter.RecordedProgram,
        'filter',
        staticmethod(FilterPrograms),
    )
    monkeypatch.setattr(
        RecordedSeriesRouter.RecordedEpisodeResolution,
        'filter',
        staticmethod(FilterResolutions),
    )
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/api/recorded-series/series/10/episode-assignments')

        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        body = response.json()
        assert len(body['programs']) == 1
        resolution_body = body['programs'][0]['resolution']
        assert list(resolution_body) == [
            'status',
            'source',
            'lookup_outcome',
            'proposed_season_number',
            'proposed_episode_number',
            'confidence',
            'web_search_performed',
            'citations',
            'rationale_short',
            'manual_season_number',
            'manual_episode_number',
            'manual_status',
            'error_code',
            'error_message',
        ]
        assert resolution_body == {
            'status': 'Failed',
            'source': 'WebSearch',
            'lookup_outcome': 'SearchFailed',
            'proposed_season_number': 1,
            'proposed_episode_number': '13',
            'confidence': 0.42,
            'web_search_performed': True,
            'citations': [
                {
                    'url': 'https://example.com/episode-13',
                    'title': '第13話',
                },
            ],
            'rationale_short': None,
            'manual_season_number': None,
            'manual_episode_number': None,
            'manual_status': None,
            'error_code': 'HTTP500',
            'error_message': 'AI プロバイダーが通信エラーを返しました。',
        }
        assert '/home/user/private/profile' not in response.text

    asyncio.run(Run())


def test_settings_update_allows_url_change_without_key_and_preserves_previous_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """URLだけの変更でprovider Aの保存キーをprovider Bへ転送せず、旧URLキーはmapで保持する。"""

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
            # URL だけ変更し、キー未指定 → 許可（provider B にはキーなし）
            allowed_response = await client.put(
                '/api/recorded-series/settings',
                json={
                    'enabled': True,
                    'ai_enabled': True,
                    'api_base_url': 'https://provider-b.example/v1',
                    'model': 'provider-b-model',
                    'daily_ai_request_limit': 20,
                },
            )
            # provider B 用のキーを設定する更新
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
            get_response = await client.get('/api/recorded-series/settings')

        assert allowed_response.status_code == 204
        assert replaced_response.status_code == 204
        assert get_response.json()['api_base_url'] == 'https://provider-b.example/v1'
        assert get_response.json()['api_key_configured'] is True

    asyncio.run(Run())
    # provider A のキーは map に残っている
    assert RecordedSeriesSettingsStore.getAPIKeyForURL('https://provider-a.example/v1') == 'provider-a-secret'
    # 現在のURL (provider B) のキー
    assert RecordedSeriesSettingsStore.getAPIKey() == 'provider-b-secret'


def test_status_and_backfill_endpoints_return_task_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ConfigureTemporaryStore(monkeypatch, tmp_path)
    RecordedSeriesSettingsStore.saveSettings(RecordedSeriesSettings(
        ai_enabled=True,
        ai_episode_number_search_enabled=True,
    ))
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
            'series_ai_requests_today': 2,
            'episode_ai_requests_today': 1,
            'last_run_at': '2026-07-21T12:34:56+09:00',
            'is_running': False,
        }

    async def GetEpisodeStatus() -> dict[str, int | str | bool | None]:
        return {
            'episode_resolved': 90,
            'episode_unknown': 8,
            'episode_not_numbered': 1,
            'episode_needs_review': 1,
            'episode_failed': 0,
            'episode_last_run_at': '2026-07-21T12:35:00+09:00',
            'is_episode_running': False,
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

    async def StartEpisodeBackfill(
        *, force: bool = False
    ) -> RecordedEpisodeBackfillAccepted:
        received_force_values.append(force)
        return RecordedEpisodeBackfillAccepted(execution_id=43, reused=True)

    monkeypatch.setattr(RecordedSeriesRouter.RecordedSeriesResolver, 'getStatus', GetStatus)
    monkeypatch.setattr(RecordedSeriesRouter.RecordedSeriesResolver, 'startBackfill', StartBackfill)
    monkeypatch.setattr(RecordedSeriesRouter.RecordedEpisodeAutomation, 'getStatus', GetEpisodeStatus)
    monkeypatch.setattr(RecordedSeriesRouter.RecordedEpisodeAutomation, 'startBackfill', StartEpisodeBackfill)

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            status_response = await client.get('/api/recorded-series/status')
            backfill_response = await client.post('/api/recorded-series/backfill', json={'force': True})
            episode_backfill_response = await client.post(
                '/api/recorded-series/episodes/backfill',
                json={'force': False},
            )

        assert status_response.status_code == 200
        assert status_response.json()['total'] == 160
        assert status_response.json()['episode_resolved'] == 90
        assert status_response.json()['series_ai_requests_today'] == 2
        assert status_response.json()['episode_ai_requests_today'] == 1
        assert backfill_response.status_code == 202
        assert backfill_response.json() == {'execution_id': 42, 'reused': False}
        assert episode_backfill_response.status_code == 202
        assert episode_backfill_response.headers['cache-control'] == 'no-store'
        assert episode_backfill_response.json() == {'execution_id': 43, 'reused': True}

    asyncio.run(Run())
    assert received_force_values == [True, False]


def test_episode_backfill_endpoint_rejects_unavailable_ai_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一括話数判定は、保存済み設定でWeb検索能力を確認できない場合に開始しない。"""

    async def StartEpisodeBackfill(
        *, force: bool = False
    ) -> RecordedEpisodeBackfillAccepted:
        del force
        raise RecordedSeriesRouter.RecordedEpisodeRelookupDisabledError

    monkeypatch.setattr(
        RecordedSeriesRouter.RecordedEpisodeAutomation,
        'startBackfill',
        StartEpisodeBackfill,
    )
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.post(
                '/api/recorded-series/episodes/backfill',
                json={'force': False},
            )

        assert response.status_code == 409
        assert response.headers['cache-control'] == 'no-store'
        assert response.json() == {
            'detail': 'AI episode number search is not available with the current settings.',
        }

    asyncio.run(Run())


def test_episode_relookup_endpoint_starts_one_program_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """単票再検索 API は楽観ロック値を渡し、再利用を含む 202 契約を返す。"""

    received_requests: list[tuple[int, int, int | None, bool]] = []

    async def StartRelookup(
        recorded_program_id: int,
        *,
        expected_series_id: int,
        expected_series_episode_id: int | None,
        override_manual: bool,
    ) -> RecordedEpisodeBackfillAccepted:
        received_requests.append((
            recorded_program_id,
            expected_series_id,
            expected_series_episode_id,
            override_manual,
        ))
        return RecordedEpisodeBackfillAccepted(execution_id=43, reused=True)

    monkeypatch.setattr(RecordedSeriesRouter.RecordedEpisodeAutomation, 'startRelookup', StartRelookup)
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(
                '/api/recorded-series/programs/100/episode-relookup',
                json={
                    'expected_series_id': 7,
                    'expected_series_episode_id': None,
                    'override_manual': True,
                },
            )
            invalid_response = await client.post(
                '/api/recorded-series/programs/100/episode-relookup',
                json={
                    'expected_series_id': 0,
                    'expected_series_episode_id': None,
                    'unknown_field': True,
                },
            )

        assert response.status_code == 202
        assert response.headers['cache-control'] == 'no-store'
        assert response.json() == {'execution_id': 43, 'reused': True}
        assert invalid_response.status_code == 422

    asyncio.run(Run())
    assert received_requests == [(100, 7, None, True)]


def test_episode_relookup_endpoint_requires_authentication() -> None:
    """単票再検索 API は未認証リクエストを処理開始前に拒否する。"""

    app = FastAPI()
    app.include_router(RecordedSeriesRouter.router)

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.post(
                '/api/recorded-series/programs/100/episode-relookup',
                json={
                    'expected_series_id': 7,
                    'expected_series_episode_id': None,
                },
            )

        assert response.status_code == 401

    asyncio.run(Run())


@pytest.mark.parametrize(
    ('exception_name', 'expected_status'),
    [
        ('RecordedEpisodeRelookupNotFoundError', 404),
        ('RecordedEpisodeRelookupConflictError', 409),
        ('RecordedEpisodeRelookupDisabledError', 409),
        ('RecordedEpisodeRelookupRateLimitedError', 429),
    ],
)
def test_episode_relookup_endpoint_maps_domain_errors(
    monkeypatch: pytest.MonkeyPatch,
    exception_name: str,
    expected_status: int,
) -> None:
    """単票再検索の不在・競合・設定不足を安全な HTTP 状態へ変換する。"""

    exception_type = getattr(RecordedSeriesRouter, exception_name)

    async def StartRelookup(
        recorded_program_id: int,
        *,
        expected_series_id: int,
        expected_series_episode_id: int | None,
        override_manual: bool,
    ) -> RecordedEpisodeBackfillAccepted:
        del recorded_program_id, expected_series_id, expected_series_episode_id, override_manual
        raise exception_type()

    monkeypatch.setattr(RecordedSeriesRouter.RecordedEpisodeAutomation, 'startRelookup', StartRelookup)
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(
                '/api/recorded-series/programs/100/episode-relookup',
                json={
                    'expected_series_id': 7,
                    'expected_series_episode_id': 11,
                },
            )

        assert response.status_code == expected_status
        assert response.headers['cache-control'] == 'no-store'
        assert 'detail' in response.json()

    asyncio.run(Run())


# --- Part A: OpenAI 互換マルチプロバイダー (API キー map) テスト ---

def test_multi_url_api_key_map_save_and_retrieve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """複数 URL の API キーを保存し、URL 切替時に正しいキーが自動選択される。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(api_base_url='https://api.openai.com/v1')
    RecordedSeriesSettingsStore.saveSettings(settings, api_key='openai-key')
    settings_ollama = RecordedSeriesSettings(api_base_url='http://localhost:11434/v1')
    RecordedSeriesSettingsStore.saveSettings(settings_ollama, api_key='ollama-key')

    # 現在の設定 URL に対応するキーが取得される
    current_settings = RecordedSeriesSettingsStore.getSettings()
    assert current_settings.api_base_url == 'http://localhost:11434/v1'
    assert RecordedSeriesSettingsStore.getAPIKey() == 'ollama-key'
    # 別 URL のキーも map から取得可能
    assert RecordedSeriesSettingsStore.getAPIKeyForURL('https://api.openai.com/v1') == 'openai-key'
    # 存在しない URL は None
    assert RecordedSeriesSettingsStore.getAPIKeyForURL('https://unknown.example/v1') is None


def test_multi_url_key_map_switch_preserves_all_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """URL 切替時、以前の URL のキーが map 上で保持され再入力不要。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    # OpenAI 用設定 + キー保存
    settings_openai = RecordedSeriesSettings(api_base_url='https://api.openai.com/v1')
    RecordedSeriesSettingsStore.saveSettings(settings_openai, api_key='openai-key')

    # Groq 用に切り替え + キー保存
    settings_groq = RecordedSeriesSettings(api_base_url='https://api.groq.com/openai/v1')
    RecordedSeriesSettingsStore.saveSettings(settings_groq, api_key='groq-key')

    # OpenAI に戻す（キーなしで設定だけ変更）
    RecordedSeriesSettingsStore.saveSettings(settings_openai)

    assert RecordedSeriesSettingsStore.getAPIKey() == 'openai-key'
    assert RecordedSeriesSettingsStore.getAPIKeyForURL('https://api.groq.com/openai/v1') == 'groq-key'
    # Groq キーは OpenAI URL で入手できない
    assert RecordedSeriesSettingsStore.getAPIKeyForURL('https://api.openai.com/v1') != 'groq-key'


def test_old_single_record_format_migration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """旧形式 {"api_base_url": "...", "api_key": "..."} から map 形式へ自動移行する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    # 旧形式データを手動で書き込む
    old_format_content = json.dumps({
        'api_base_url': 'https://api.openai.com/v1',
        'api_key': 'old-format-key',
    }, ensure_ascii=False, indent=4) + '\n'
    RecordedSeriesSettingsStore.API_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    RecordedSeriesSettingsStore.API_KEY_PATH.write_text(old_format_content, encoding='utf-8')

    # 読み込み時に map 形式へ変換される
    settings = RecordedSeriesSettings(api_base_url='https://api.openai.com/v1')
    RecordedSeriesSettingsStore.saveSettings(settings)
    assert RecordedSeriesSettingsStore.getAPIKey() == 'old-format-key'

    # 設定だけの保存でも、旧形式ファイルは map 形式へ書き戻されている
    raw_content = RecordedSeriesSettingsStore.API_KEY_PATH.read_text(encoding='utf-8')
    stored_map = json.loads(raw_content)
    assert isinstance(stored_map, dict)
    assert 'api_base_url' not in stored_map  # 旧形式キーは存在しない
    assert stored_map.get('https://api.openai.com/v1') == 'old-format-key'

    # キー更新時の保存も引き続き map 形式で動作する
    RecordedSeriesSettingsStore.saveSettings(settings, api_key='updated-key')
    raw_content = RecordedSeriesSettingsStore.API_KEY_PATH.read_text(encoding='utf-8')
    assert json.loads(raw_content).get('https://api.openai.com/v1') == 'updated-key'


def test_empty_api_key_deletes_current_url_entry_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """空キー保存は現在の URL のエントリだけを削除し、他 URL のキーは残る。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(api_base_url='https://api.openai.com/v1')
    RecordedSeriesSettingsStore.saveSettings(settings, api_key='openai-key')
    settings_groq = RecordedSeriesSettings(api_base_url='https://api.groq.com/openai/v1')
    RecordedSeriesSettingsStore.saveSettings(settings_groq, api_key='groq-key')

    # 空キーで OpenAI URL のキーを削除
    RecordedSeriesSettingsStore.saveSettings(settings, api_key='')
    assert RecordedSeriesSettingsStore.getAPIKey() is None
    # Groq のキーは残る
    assert RecordedSeriesSettingsStore.getAPIKeyForURL('https://api.groq.com/openai/v1') == 'groq-key'


def test_api_key_written_as_map_format(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """保存されるキーファイルが正規化 URL をキーとする map 形式である。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(api_base_url='https://api.openai.com/v1')
    RecordedSeriesSettingsStore.saveSettings(settings, api_key='test-key')

    raw_content = RecordedSeriesSettingsStore.API_KEY_PATH.read_text(encoding='utf-8')
    data = json.loads(raw_content)
    assert isinstance(data, dict)
    assert 'api_base_url' not in data  # 旧形式キーが含まれていない
    assert data.get('https://api.openai.com/v1') == 'test-key'


def test_delete_api_key_removes_only_current_url_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """deleteAPIKey は現在の URL のキーだけを削除する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings_openai = RecordedSeriesSettings(api_base_url='https://api.openai.com/v1')
    RecordedSeriesSettingsStore.saveSettings(settings_openai, api_key='openai-key')
    settings_groq = RecordedSeriesSettings(api_base_url='https://api.groq.com/openai/v1')
    RecordedSeriesSettingsStore.saveSettings(settings_groq, api_key='groq-key')

    # 現在 URL (Groq) のキーだけを削除
    RecordedSeriesSettingsStore.deleteAPIKey()
    assert RecordedSeriesSettingsStore.getAPIKey() is None
    # OpenAI のキーは残る
    assert RecordedSeriesSettingsStore.getAPIKeyForURL('https://api.openai.com/v1') == 'openai-key'

    # 再度 deleteAPIKey（既に存在しない）→ エラーにならず何もしない
    RecordedSeriesSettingsStore.deleteAPIKey()
    assert RecordedSeriesSettingsStore.getAPIKey() is None


def test_delete_api_key_api_uses_explicit_saved_url_when_draft_url_differs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """画面が未保存 URL B を表示中でも、明示した保存済み URL A のキーだけを削除する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    url_a = 'https://provider-a.example/v1'
    url_b = 'https://provider-b.example/v1'
    RecordedSeriesSettingsStore.saveSettings(
        RecordedSeriesSettings(api_base_url=url_a),
        api_key='key-a',
    )
    RecordedSeriesSettingsStore.saveSettings(
        RecordedSeriesSettings(api_base_url=url_b),
        api_key='key-b',
    )
    # 保存済み設定は A。ブラウザ側の未保存 draft は B でも、DELETE は snapshot の A を明示する。
    RecordedSeriesSettingsStore.saveSettings(RecordedSeriesSettings(api_base_url=url_a))
    app = CreateAdminApp()

    async def Scenario() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://testserver',
        ) as client:
            response = await client.delete(
                '/api/recorded-series/settings/api-key',
                params={'url': url_a},
            )
            assert response.status_code == 204

    asyncio.run(Scenario())
    assert RecordedSeriesSettingsStore.getAPIKeyForURL(url_a) is None
    assert RecordedSeriesSettingsStore.getAPIKeyForURL(url_b) == 'key-b'


def test_connection_test_uses_map_lookup_for_key_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """接続試験で、テスト対象 URL に map 上キーがあれば自動利用する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    # 複数 URL のキーを保存
    settings = RecordedSeriesSettings(api_base_url='https://api.openai.com/v1')
    RecordedSeriesSettingsStore.saveSettings(settings, api_key='openai-key')
    RecordedSeriesSettingsStore.saveSettings(
        RecordedSeriesSettings(api_base_url='https://api.groq.com/openai/v1'),
        api_key='groq-key',
    )

    app = CreateAdminApp()
    captured_api_key: str | None = None

    async def TestConnection(
        capability: str,
        *,
        settings: RecordedSeriesSettings | None = None,
        api_key: str | None = None,
    ) -> ConnectionTestResult:
        assert capability == 'CandidateSelection'
        assert settings is not None
        nonlocal captured_api_key
        assert settings.api_base_url == 'https://api.openai.com/v1'
        captured_api_key = api_key
        return ConnectionTestResult(
            success=True,
            latency_ms=1,
            model='compatible-model',
            message='ok',
            prompt_tokens=1,
            completion_tokens=1,
            http_status=200,
            selected_choice_id='unresolved',
        )

    async def CreateAudit(**kwargs: object) -> object:
        del kwargs
        return object()

    monkeypatch.setattr(RecordedSeriesRouter, 'test_connection', TestConnection)
    monkeypatch.setattr(RecordedSeriesRouter.RecordedSeriesAIRequest, 'create', CreateAudit)

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(
                '/api/recorded-series/settings/test',
                json={
                    'api_base_url': 'https://api.openai.com/v1',
                    'model': 'compatible-model',
                },
            )
        assert response.status_code == 200

    asyncio.run(Run())
    # API キー未指定でも、map から対応 URL のキーが自動選択されている
    assert captured_api_key == 'openai-key'


def test_settings_api_key_configured_reflects_map_after_url_switch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """URL 切替後、GET /settings の api_key_configured が正しい map 状態を返す。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            # URL A にキーを保存
            await client.put(
                '/api/recorded-series/settings',
                json={
                    'enabled': True, 'ai_enabled': True,
                    'api_base_url': 'https://provider-a.example/v1',
                    'model': 'gpt-5-nano',
                    'daily_ai_request_limit': 0,
                    'api_key': 'key-for-a',
                },
            )
            get_a = await client.get('/api/recorded-series/settings')
            assert get_a.json()['api_key_configured'] is True

            # URL B へ切り替え（キーなしで設定だけ変更）
            await client.put(
                '/api/recorded-series/settings',
                json={
                    'enabled': True, 'ai_enabled': True,
                    'api_base_url': 'https://provider-b.example/v1',
                    'model': 'gpt-5-nano',
                    'daily_ai_request_limit': 0,
                },
            )
            get_b = await client.get('/api/recorded-series/settings')
            assert get_b.json()['api_key_configured'] is False

            # URL A へ戻す（map からキー自動再利用）
            await client.put(
                '/api/recorded-series/settings',
                json={
                    'enabled': True, 'ai_enabled': True,
                    'api_base_url': 'https://provider-a.example/v1',
                    'model': 'gpt-5-nano',
                    'daily_ai_request_limit': 0,
                },
            )
            get_a2 = await client.get('/api/recorded-series/settings')
            assert get_a2.json()['api_key_configured'] is True

    asyncio.run(Run())


@pytest.mark.parametrize(
    'invalid_url',
    [
        'http://localhost:notaport',
        'http://localhost:0',
        'http://localhost:99999',
        'http://localhost:-1',
    ],
)
def test_api_base_url_rejects_invalid_ports(invalid_url: str) -> None:
    """非数・範囲外 port の API ベース URL を保存時に拒否する。"""

    with pytest.raises(ValidationError):
        RecordedSeriesSettings.model_validate({'api_base_url': invalid_url})


def test_api_base_url_accepts_valid_port() -> None:
    settings = RecordedSeriesSettings.model_validate({'api_base_url': 'http://localhost:8080/v1'})
    assert settings.api_base_url == 'http://localhost:8080/v1'


def _WriteRawAPIKeyFile(content: str) -> None:
    """検証を通さず秘密ファイルへ生 JSON を書き込む。"""

    RecordedSeriesSettingsStore.API_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    RecordedSeriesSettingsStore.API_KEY_PATH.write_text(content, encoding='utf-8')


@pytest.mark.parametrize(
    'payload',
    [
        {'api_base_url': ['https://provider.example/v1'], 'api_key': 'secret'},
        {'api_base_url': 'https://provider.example/v1', 'api_key': ['secret-value']},
        {'api_base_url': 'https://provider.example/v1', 'api_key': {'nested': 'x'}},
        {'api_base_url': 'https://provider.example/v1', 'api_key': 123},
        {'api_base_url': 'https://provider.example/v1', 'api_key': None},
        {'api_base_url': None, 'api_key': 'secret'},
    ],
)
def test_old_format_rejects_non_string_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    """旧形式の api_base_url / api_key が非文字列なら拒否する（str() 変換しない）。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    _WriteRawAPIKeyFile(json.dumps(payload) + '\n')
    with pytest.raises(ValueError, match='invalid'):
        RecordedSeriesSettingsStore._readAPIKeysMap()


@pytest.mark.parametrize('payload', [[], 'string', 1, None, True])
def test_new_map_rejects_non_object_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: object,
) -> None:
    """新 map の root が list / string / number / null なら拒否する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    _WriteRawAPIKeyFile(json.dumps(payload) + '\n')
    with pytest.raises(ValueError, match='invalid'):
        RecordedSeriesSettingsStore._readAPIKeysMap()


@pytest.mark.parametrize(
    'payload',
    [
        {'https://provider.example/v1': ['secret-value']},
        {1: 'secret-value'},
        {'https://provider.example/v1': 1},
        {'https://provider.example/v1': None},
        {'https://provider.example/v1': {'k': 'v'}},
        {'': 'secret'},
        {'https://provider.example/v1': ''},
        {'https://provider.example/v1': '   '},
        {'https://user:pass@provider.example/v1': 'secret'},
        {'https://provider.example/v1?x=1': 'secret'},
        {'https://provider.example/v1#frag': 'secret'},
        {'http://localhost:notaport': 'secret'},
    ],
)
def test_new_map_rejects_invalid_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: dict[object, object],
) -> None:
    """新 map の非文字列・空値・不正 URL を黙殺せず拒否する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    _WriteRawAPIKeyFile(json.dumps(payload) + '\n')
    with pytest.raises(ValueError, match='invalid'):
        RecordedSeriesSettingsStore._readAPIKeysMap()
    # エラーメッセージへ API キー本体を含めない
    try:
        RecordedSeriesSettingsStore._readAPIKeysMap()
    except ValueError as error:
        assert 'secret' not in str(error)


def test_new_map_rejects_normalized_url_collision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """正規化後に同一 URL へ衝突する map を拒否する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    payload = {
        'https://provider.example/v1': 'key-a',
        'https://provider.example/v1/': 'key-b',
    }
    _WriteRawAPIKeyFile(json.dumps(payload) + '\n')
    with pytest.raises(ValueError, match='invalid'):
        RecordedSeriesSettingsStore._readAPIKeysMap()


def test_delete_corrupt_map_unlinks_secret_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """構造不正 map の delete は全体 unlink し、204 相当で秘密値を残さない。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    secret = 'must-not-remain-after-delete'
    _WriteRawAPIKeyFile(json.dumps({
        'https://provider.example/v1': [secret],
    }) + '\n')
    assert RecordedSeriesSettingsStore.API_KEY_PATH.is_file()

    RecordedSeriesSettingsStore.deleteAPIKey(url='https://provider.example/v1')

    assert RecordedSeriesSettingsStore.API_KEY_PATH.is_file() is False
    # ディスク上に secret 文字列が残っていない（親 directory 内）
    remaining = ''.join(
        path.read_text(encoding='utf-8', errors='ignore')
        for path in tmp_path.rglob('*')
        if path.is_file()
    )
    assert secret not in remaining


def test_delete_last_entry_removes_secret_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """最後の1件を削除すると秘密ファイル自体がなくなる。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(api_base_url='https://api.openai.com/v1')
    RecordedSeriesSettingsStore.saveSettings(settings, api_key='only-key')
    assert RecordedSeriesSettingsStore.API_KEY_PATH.is_file()

    RecordedSeriesSettingsStore.deleteAPIKey()
    assert RecordedSeriesSettingsStore.API_KEY_PATH.is_file() is False
    assert RecordedSeriesSettingsStore.getAPIKey() is None


def test_delete_one_of_multiple_preserves_others(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """複数 URL のうち1件削除しても他のキーは残る。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    RecordedSeriesSettingsStore.saveSettings(
        RecordedSeriesSettings(api_base_url='https://provider-a.example/v1'),
        api_key='key-a',
    )
    RecordedSeriesSettingsStore.saveSettings(
        RecordedSeriesSettings(api_base_url='https://provider-b.example/v1'),
        api_key='key-b',
    )
    RecordedSeriesSettingsStore.deleteAPIKey(url='https://provider-a.example/v1')
    assert RecordedSeriesSettingsStore.getAPIKeyForURL('https://provider-a.example/v1') is None
    assert RecordedSeriesSettingsStore.getAPIKeyForURL('https://provider-b.example/v1') == 'key-b'
    assert RecordedSeriesSettingsStore.API_KEY_PATH.is_file()


def test_delete_api_returns_204_and_removes_corrupt_secrets_from_disk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """DELETE API が 204 を返した場合、corrupt map の秘密値もディスクに残らない。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    secret = 'corrupt-secret-value-xyz'
    _WriteRawAPIKeyFile(json.dumps({
        'https://provider.example/v1': [secret],
    }) + '\n')
    app = CreateAdminApp()

    async def Scenario() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://testserver',
        ) as client:
            response = await client.delete(
                '/api/recorded-series/settings/api-key',
                params={'url': 'https://provider.example/v1'},
            )
            assert response.status_code == 204
            body = response.text
            assert secret not in body
            assert 'api_key' not in body.lower() or body == ''

    asyncio.run(Scenario())
    assert RecordedSeriesSettingsStore.API_KEY_PATH.is_file() is False
    remaining = ''.join(
        path.read_text(encoding='utf-8', errors='ignore')
        for path in tmp_path.rglob('*')
        if path.is_file()
    )
    assert secret not in remaining


def test_settings_get_does_not_expose_key_or_url_list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """GET 設定 API がキー本体・保存済み URL 一覧を返さない。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    RecordedSeriesSettingsStore.saveSettings(
        RecordedSeriesSettings(api_base_url='https://provider-a.example/v1'),
        api_key='secret-key-body',
    )
    RecordedSeriesSettingsStore.saveSettings(
        RecordedSeriesSettings(api_base_url='https://provider-b.example/v1'),
        api_key='other-secret',
    )
    app = CreateAdminApp()

    async def Scenario() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://testserver',
        ) as client:
            response = await client.get('/api/recorded-series/settings')
            assert response.status_code == 200
            payload = response.json()
            assert payload['api_key_configured'] is True
            assert 'api_key' not in payload
            assert 'api_keys' not in payload
            assert 'secret-key-body' not in response.text
            assert 'other-secret' not in response.text
            # 保存済み URL 一覧も返さない（現在 URL の設定フィールドは通常設定として返る）
            assert 'provider-a.example' not in response.text

    asyncio.run(Scenario())


def test_empty_map_save_unlinks_secret_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """空キー保存で map が空になったとき秘密ファイルを残さない。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(api_base_url='https://api.openai.com/v1')
    RecordedSeriesSettingsStore.saveSettings(settings, api_key='only')
    RecordedSeriesSettingsStore.saveSettings(settings, api_key='')
    assert RecordedSeriesSettingsStore.API_KEY_PATH.is_file() is False


def test_write_api_keys_map_rejects_serialized_size_over_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """シリアライズ後に 256 KiB を超える map は atomic write 前に拒否する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    oversized_key = 'k' * RecordedSeriesSettingsStore._API_KEY_MAX_LENGTH
    keys_map = {
        f'https://provider-{index:02d}.example/v1': oversized_key
        for index in range(RecordedSeriesSettingsStore._API_KEY_MAP_MAX_ENTRIES)
    }
    serialized = json.dumps(keys_map, ensure_ascii=False, indent=4) + '\n'
    assert len(serialized.encode('utf-8')) > RecordedSeriesSettingsStore._API_KEY_MAP_MAX_BYTES

    with pytest.raises(ValueError, match='invalid'):
        RecordedSeriesSettingsStore._writeAPIKeysMap(keys_map)
    assert RecordedSeriesSettingsStore.API_KEY_PATH.is_file() is False


def test_write_api_keys_map_accepts_size_at_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """上限ちょうど以下の map は保存でき、直後に読める。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    # 件数を抑えつつ実キー長を調整し、上限付近だが超過しない payload を作る。
    keys_map = {
        'https://provider-a.example/v1': 'a' * 1000,
        'https://provider-b.example/v1': 'b' * 1000,
    }
    content = RecordedSeriesSettingsStore._serializeAPIKeysMap(keys_map)
    assert len(content.encode('utf-8')) <= RecordedSeriesSettingsStore._API_KEY_MAP_MAX_BYTES
    RecordedSeriesSettingsStore._writeAPIKeysMap(keys_map)
    assert RecordedSeriesSettingsStore._readAPIKeysMap() == keys_map


def test_new_map_rejects_unnormalized_or_oversized_url_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """新 map は保存キーが正規化済みかつ 2048 文字以内であることを要求する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)

    unnormalized = {
        ' https://provider.example/v1/ ': 'secret-key',
    }
    _WriteRawAPIKeyFile(json.dumps(unnormalized) + '\n')
    with pytest.raises(ValueError, match='invalid'):
        RecordedSeriesSettingsStore._readAPIKeysMap()

    long_host = 'a' * 2030
    oversized_url = f'https://{long_host}.example/v1'
    assert len(oversized_url) > 2048
    _WriteRawAPIKeyFile(json.dumps({oversized_url: 'secret-key'}) + '\n')
    with pytest.raises(ValueError, match='invalid'):
        RecordedSeriesSettingsStore._readAPIKeysMap()


def test_unlink_propagates_directory_open_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """directory open 失敗は黙殺せず OSError を返し、偽の削除成功にしない。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    RecordedSeriesSettingsStore.saveSettings(
        RecordedSeriesSettings(api_base_url='https://api.openai.com/v1'),
        api_key='secret-to-delete',
    )
    assert RecordedSeriesSettingsStore.API_KEY_PATH.is_file()

    real_open = os.open

    def FailDirectoryOpen(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if flags & os.O_DIRECTORY:
            raise OSError(24, 'Too many open files')
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, 'open', FailDirectoryOpen)
    with pytest.raises(OSError):
        RecordedSeriesSettingsStore.deleteAPIKey()


def test_legacy_format_rejects_oversized_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """旧形式の raw URL が 2048 文字を超える場合は拒否する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    long_host = 'a' * 2030
    oversized_url = f'https://{long_host}.example/v1'
    assert len(oversized_url) > 2048
    _WriteRawAPIKeyFile(json.dumps({
        'api_base_url': oversized_url,
        'api_key': 'legacy-secret',
    }) + '\n')
    with pytest.raises(ValueError, match='invalid'):
        RecordedSeriesSettingsStore._readAPIKeysMap()


def test_api_key_map_rejects_duplicate_json_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """同一 URL キーが JSON 上で重複する場合は後勝ちにせず fail-closed で拒否する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    # json.loads の既定は後勝ちだが、秘密 map では黙った上書きを許可しない。
    raw = (
        '{\n'
        '  "https://provider.example/v1": "first-secret",\n'
        '  "https://provider.example/v1": "second-secret"\n'
        '}\n'
    )
    _WriteRawAPIKeyFile(raw)
    with pytest.raises(ValueError, match='invalid'):
        RecordedSeriesSettingsStore._readAPIKeysMap()


def test_settings_write_failure_restores_previous_key_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """設定書込失敗時は再シリアライズせず、直前の秘密ファイル bytes を完全復元する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(
        api_base_url='https://provider.example/v1',
        model='old-model',
    )
    RecordedSeriesSettingsStore.saveSettings(settings, api_key='old-key')
    previous_bytes = RecordedSeriesSettingsStore.API_KEY_PATH.read_bytes()
    original_write_atomic = RecordedSeriesSettingsStore._writeAtomic

    def FailSettingsWrite(
        cls: type[RecordedSeriesSettingsStore],
        destination: Path,
        content: str,
    ) -> None:
        del cls
        if destination == RecordedSeriesSettingsStore.SETTINGS_PATH:
            raise OSError('simulated settings write failure')
        original_write_atomic(destination, content)

    monkeypatch.setattr(
        RecordedSeriesSettingsStore,
        '_writeAtomic',
        classmethod(FailSettingsWrite),
    )

    with pytest.raises(OSError, match='simulated settings write failure'):
        RecordedSeriesSettingsStore.saveSettings(
            settings.model_copy(update={'model': 'new-model'}),
            api_key='new-key-must-not-remain',
        )

    assert RecordedSeriesSettingsStore.API_KEY_PATH.read_bytes() == previous_bytes
    assert RecordedSeriesSettingsStore.getAPIKey() == 'old-key'
    assert b'new-key-must-not-remain' not in RecordedSeriesSettingsStore.API_KEY_PATH.read_bytes()
    assert RecordedSeriesSettingsStore.getSettings().model == 'old-model'


def test_api_key_replace_directory_fsync_failure_restores_previous_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """API キー replace 後の directory fsync 失敗でも新キーを残さず旧 bytes へ戻す。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(
        api_base_url='https://provider.example/v1',
        model='old-model',
    )
    RecordedSeriesSettingsStore.saveSettings(settings, api_key='old-key')
    previous_bytes = RecordedSeriesSettingsStore.API_KEY_PATH.read_bytes()
    directory_fsync_failures = {'remaining': 1}
    original_fsync = os.fsync

    def FailDirectoryFsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        if stat.S_ISDIR(mode) and directory_fsync_failures['remaining'] > 0:
            directory_fsync_failures['remaining'] -= 1
            raise OSError('simulated directory fsync failure after api key replace')
        original_fsync(fd)

    monkeypatch.setattr(os, 'fsync', FailDirectoryFsync)

    with pytest.raises(OSError, match='simulated directory fsync failure after api key replace'):
        RecordedSeriesSettingsStore.saveSettings(
            settings.model_copy(update={
                'api_base_url': 'https://other.example/v1',
                'model': 'new-model',
            }),
            api_key='new-key-must-not-remain',
        )

    assert RecordedSeriesSettingsStore.API_KEY_PATH.is_file()
    assert RecordedSeriesSettingsStore.API_KEY_PATH.read_bytes() == previous_bytes
    assert b'new-key-must-not-remain' not in RecordedSeriesSettingsStore.API_KEY_PATH.read_bytes()
    # 設定ファイルはキー書込段階で失敗したため旧設定のまま
    assert RecordedSeriesSettingsStore.getSettings().model == 'old-model'
    assert RecordedSeriesSettingsStore.getSettings().api_base_url == 'https://provider.example/v1'


def test_read_rejects_map_that_exceeds_canonical_size_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """compact 形式では読めても canonical 再シリアライズが上限超過なら読込を拒否する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    keys_map = {
        'https://provider-a.example/v1': 'a' * 80,
        'https://provider-b.example/v1': 'b' * 80,
    }
    compact = json.dumps(keys_map, ensure_ascii=False, separators=(',', ':')) + '\n'
    canonical = json.dumps(keys_map, ensure_ascii=False, indent=4) + '\n'
    compact_size = len(compact.encode('utf-8'))
    canonical_size = len(canonical.encode('utf-8'))
    assert compact_size < canonical_size
    # 上限を compact と canonical の間に置き、読込時の canonical 検査だけを失敗させる。
    boundary_limit = (compact_size + canonical_size) // 2
    monkeypatch.setattr(RecordedSeriesSettingsStore, '_API_KEY_MAP_MAX_BYTES', boundary_limit)
    assert compact_size <= boundary_limit < canonical_size
    _WriteRawAPIKeyFile(compact)

    with pytest.raises(ValueError, match='invalid'):
        RecordedSeriesSettingsStore._readAPIKeysMap()
