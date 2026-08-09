"""AIBackendRouter の unit テスト（Phase 2: connection-test / availability / ACP）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient

from app.metadata.ai import recorded_series_ai as RecordedSeriesAIModule
from app.metadata.ai.ACPSettings import ACPSettingsStore
from app.metadata.ai.AIBackendSettings import (
    AIBackendServiceCreate,
    AIBackendSettingsStore,
)
from app.metadata.ai.backends import (
    ConnectionTestCheck,
    ConnectionTestResult,
    EpisodeLookupConnectionChecks,
)
from app.metadata.ai.KonomiTVBS4KACPCredentials import KonomiTVBS4KACPCredentials
from app.metadata.ai.opencode_backend import OpenCodeBackend
from app.metadata.ai.recorded_series_ai import (
    has_episode_lookup_capability_proof,
    reset_episode_lookup_capability_proofs_for_tests,
)
from app.metadata.RecordedSeriesSettings import RecordedSeriesSettings
from app.routers import AIBackendRouter


def CreateAdminApp() -> FastAPI:
    """管理者認証をテスト用に置き換えた FastAPI アプリを作成する。"""

    async def GetAdminUser() -> object:
        return object()

    app = FastAPI()
    app.include_router(AIBackendRouter.router)
    app.dependency_overrides[AIBackendRouter.GetCurrentAdminUser] = GetAdminUser
    return app


@pytest.fixture()
def ai_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """AI 設定・秘密パスを一時ディレクトリへ indirection する。"""

    settings_path = tmp_path / 'ai-backend-settings.json'
    secrets_path = tmp_path / 'secrets' / 'ai-api-keys.json'
    monkeypatch.setattr(AIBackendSettingsStore, 'SETTINGS_PATH', settings_path)
    monkeypatch.setattr(AIBackendSettingsStore, 'SECRETS_PATH', secrets_path)
    return tmp_path


@pytest.fixture()
def acp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """ACP 設定・能力証明を一時ディレクトリへ indirection する。"""

    monkeypatch.setattr(
        ACPSettingsStore,
        'SETTINGS_PATH',
        tmp_path / 'acp-settings.json',
    )
    monkeypatch.setattr(
        RecordedSeriesAIModule,
        'EPISODE_LOOKUP_CAPABILITY_PROOFS_PATH',
        tmp_path / 'recorded-series-episode-lookup-proofs.json',
    )
    reset_episode_lookup_capability_proofs_for_tests()
    return tmp_path


def SuccessfulEpisodeLookupConnectionChecks() -> EpisodeLookupConnectionChecks:
    """ACP 接続試験 API 契約テストで使用する成功時の固定6項目。"""

    return EpisodeLookupConnectionChecks(
        backend_connection=ConnectionTestCheck(status='Passed', message='接続済み'),
        web_search=ConnectionTestCheck(status='Passed', message='検索済み'),
        source_url=ConnectionTestCheck(status='Passed', message='URL取得済み'),
        strict_schema=ConnectionTestCheck(status='Passed', message='schema検証済み'),
        timeout_cancel=ConnectionTestCheck(status='NotRun', message='未実行'),
        permission_policy=ConnectionTestCheck(status='NotApplicable', message='対象外'),
    )


def PatchACPAuthImported(monkeypatch: pytest.MonkeyPatch) -> None:
    """ACP preflight を通過させるため、Codex / Grok 認証を取り込み済みにする。"""

    monkeypatch.setattr(
        KonomiTVBS4KACPCredentials,
        'getStatus',
        classmethod(
            lambda _cls: SimpleNamespace(
                codex_auth_imported=True,
                grok_auth_imported=True,
                google_adc_available=False,
            )
        ),
    )
    # fingerprint を決定的にするため、credential 世代を固定する。
    monkeypatch.setattr(
        KonomiTVBS4KACPCredentials,
        'getCredentialGeneration',
        classmethod(lambda _cls, _provider: 'test-generation'),
    )


def _draft_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        'capability': 'CandidateSelection',
        'service_name': 'Draft DeepSeek',
        'opencode_provider_id': 'deepseek',
        'opencode_model_id': 'deepseek-chat',
        'auth_mode': 'ApiKey',
        'billing_mode': 'Metered',
        'api_key': 'sk-test-not-for-production',
    }
    body.update(overrides)
    return body


def test_connection_test_returns_503_when_opencode_unavailable(
    ai_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """serve が available=false のとき connection-test は 503。"""

    _ = ai_paths
    monkeypatch.setattr(AIBackendRouter, 'IsOpenCodeAvailable', lambda: False)
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.post(
                '/api/ai-backends/connection-test',
                json=_draft_body(),
            )
            assert response.status_code == 503
            assert 'unavailable' in response.json()['detail'].lower()
            # キーがエラー詳細にエコーされないこと
            assert 'sk-test' not in response.text

    asyncio.run(Run())


def test_connection_test_draft_requires_fields(
    ai_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """draft 必須フィールド欠落は 422。"""

    _ = ai_paths
    monkeypatch.setattr(AIBackendRouter, 'IsOpenCodeAvailable', lambda: True)
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.post(
                '/api/ai-backends/connection-test',
                json={'capability': 'CandidateSelection', 'service_name': 'only-name'},
            )
            assert response.status_code == 422
            assert 'api_key' not in response.text.lower() or 'sk-' not in response.text

    asyncio.run(Run())


def test_connection_test_saved_service_success(
    ai_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保存済み service_id 経路で接続試験成功を返す。"""

    service = AIBackendSettingsStore.createService(
        AIBackendServiceCreate(
            service_name='Saved',
            opencode_provider_id='deepseek',
            opencode_model_id='deepseek-chat',
            auth_mode='ApiKey',
            billing_mode='Metered',
        ),
    )
    AIBackendSettingsStore.setAPIKey(service.service_id, 'sk-saved')

    async def FakeTestConnection(self: OpenCodeBackend, capability: str) -> ConnectionTestResult:
        _ = self
        assert capability == 'CandidateSelection'
        return ConnectionTestResult(
            success=True,
            latency_ms=12,
            model='opencode:deepseek/deepseek-chat',
            message='ok',
            prompt_tokens=3,
            completion_tokens=2,
            http_status=200,
        )

    monkeypatch.setattr(AIBackendRouter, 'IsOpenCodeAvailable', lambda: True)
    monkeypatch.setattr(OpenCodeBackend, 'testConnection', FakeTestConnection)
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.post(
                '/api/ai-backends/connection-test',
                json={
                    'capability': 'CandidateSelection',
                    'service_id': service.service_id,
                },
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload['success'] is True
            assert payload['latency_ms'] == 12
            assert payload['prompt_tokens'] == 3
            assert 'api_key' not in payload
            assert 'sk-saved' not in response.text
            assert response.headers.get('cache-control') == 'no-store'

    asyncio.run(Run())


def test_connection_test_saved_service_not_found(
    ai_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """存在しない service_id は 404。"""

    _ = ai_paths
    monkeypatch.setattr(AIBackendRouter, 'IsOpenCodeAvailable', lambda: True)
    app = CreateAdminApp()
    missing = str(uuid4())

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.post(
                '/api/ai-backends/connection-test',
                json={
                    'capability': 'CandidateSelection',
                    'service_id': missing,
                },
            )
            assert response.status_code == 404

    asyncio.run(Run())


def test_connection_test_draft_cleans_unshared_auth(
    ai_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """draft + api_key で共有 provider が無ければ試験後に deleteAuth する。"""

    _ = ai_paths
    deleted: list[str] = []

    class FakeClient:
        async def deleteAuth(self, provider_id: str) -> None:
            deleted.append(provider_id)

    async def FakeTestConnection(self: OpenCodeBackend, capability: str) -> ConnectionTestResult:
        _ = (self, capability)
        return ConnectionTestResult(
            success=True,
            latency_ms=1,
            model='opencode:deepseek/deepseek-chat',
            message='ok',
            http_status=200,
        )

    monkeypatch.setattr(AIBackendRouter, 'IsOpenCodeAvailable', lambda: True)
    monkeypatch.setattr(OpenCodeBackend, 'testConnection', FakeTestConnection)
    monkeypatch.setattr(AIBackendRouter, 'OpenCodeClient', FakeClient)
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.post(
                '/api/ai-backends/connection-test',
                json=_draft_body(),
            )
            assert response.status_code == 200
            assert response.json()['success'] is True
            assert deleted == ['deepseek']
            assert 'sk-test-not-for-production' not in response.text

    asyncio.run(Run())


def test_connection_test_draft_rejects_shared_provider_temp_key(
    ai_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 provider を使う保存 service がある draft 一時キー試験は 409。"""

    AIBackendSettingsStore.createService(
        AIBackendServiceCreate(
            service_name='Existing DeepSeek',
            opencode_provider_id='deepseek',
            opencode_model_id='deepseek-chat',
            auth_mode='ApiKey',
            billing_mode='Metered',
        ),
    )
    deleted: list[str] = []
    tested = {'called': False}

    class FakeClient:
        async def deleteAuth(self, provider_id: str) -> None:
            deleted.append(provider_id)

    async def FakeTestConnection(self: OpenCodeBackend, capability: str) -> ConnectionTestResult:
        _ = (self, capability)
        tested['called'] = True
        return ConnectionTestResult(
            success=True,
            latency_ms=5,
            model='opencode:deepseek/deepseek-chat',
            message='should-not-run',
            http_status=200,
        )

    monkeypatch.setattr(AIBackendRouter, 'IsOpenCodeAvailable', lambda: True)
    monkeypatch.setattr(OpenCodeBackend, 'testConnection', FakeTestConnection)
    monkeypatch.setattr(AIBackendRouter, 'OpenCodeClient', FakeClient)
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.post(
                '/api/ai-backends/connection-test',
                json=_draft_body(),
            )
            assert response.status_code == 409
            assert tested['called'] is False
            assert deleted == []
            assert 'sk-test-not-for-production' not in response.text

    asyncio.run(Run())


def test_connection_test_saved_episode_lookup_records_proof(
    ai_paths: Path,
    acp_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保存済み OpenCode service の話数試験成功で proof を記録する。"""

    _ = (ai_paths, acp_paths)
    service = AIBackendSettingsStore.createService(
        AIBackendServiceCreate(
            service_name='DeepSeek Proof',
            opencode_provider_id='deepseek',
            opencode_model_id='deepseek-chat',
            auth_mode='ApiKey',
            billing_mode='Metered',
        ),
    )
    AIBackendSettingsStore.setAPIKey(service.service_id, 'sk-saved')

    async def FakeTestConnection(self: OpenCodeBackend, capability: str) -> ConnectionTestResult:
        _ = self
        assert capability == 'EpisodeLookup'
        return ConnectionTestResult(
            success=True,
            latency_ms=40,
            model='opencode:deepseek/deepseek-chat',
            message='ok',
            checks=SuccessfulEpisodeLookupConnectionChecks(),
            prompt_tokens=10,
            completion_tokens=4,
            http_status=200,
        )

    class FakeClient:
        async def putApiKey(self, provider_id: str, api_key: str) -> None:
            _ = (provider_id, api_key)

        async def deleteAuth(self, provider_id: str) -> None:
            _ = provider_id

    async def _save(**_kw: Any) -> None:
        return None

    async def CreateAuditRecord(**_kwargs: Any) -> object:
        return SimpleNamespace(status='Succeeded', error_code=None, save=_save)

    monkeypatch.setattr(AIBackendRouter, 'IsOpenCodeAvailable', lambda: True)
    monkeypatch.setattr(OpenCodeBackend, 'testConnection', FakeTestConnection)
    monkeypatch.setattr(AIBackendRouter, 'OpenCodeClient', FakeClient)
    monkeypatch.setattr(AIBackendRouter.RecordedSeriesAIRequest, 'create', CreateAuditRecord)
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.post(
                '/api/ai-backends/connection-test',
                json={
                    'capability': 'EpisodeLookup',
                    'service_id': service.service_id,
                },
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload['success'] is True
            assert payload['checks'] is not None
            assert payload['checks']['web_search']['status'] == 'Passed'
            settings = RecordedSeriesSettings(
                ai_backend='OpenCode',
                ai_enabled=True,
                ai_backend_service_id=service.service_id,
            )
            assert has_episode_lookup_capability_proof(settings, None) is True

    asyncio.run(Run())


def test_connection_test_normalizes_backend_exception(
    ai_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """backend が RecordedSeriesAIError を投げても ConnectionTest 形へ正規化する。"""

    _ = ai_paths
    from app.metadata.RecordedSeriesCandidates import RecordedSeriesAIError

    async def FakeTestConnection(self: OpenCodeBackend, capability: str) -> ConnectionTestResult:
        _ = (self, capability)
        raise RecordedSeriesAIError('OpenCodeAPIKeyMissing', http_status=400, latency_ms=7)

    class FakeClient:
        async def deleteAuth(self, provider_id: str) -> None:
            _ = provider_id

    monkeypatch.setattr(AIBackendRouter, 'IsOpenCodeAvailable', lambda: True)
    monkeypatch.setattr(OpenCodeBackend, 'testConnection', FakeTestConnection)
    monkeypatch.setattr(AIBackendRouter, 'OpenCodeClient', FakeClient)
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.post(
                '/api/ai-backends/connection-test',
                json=_draft_body(api_key=None),
            )
            # api_key 無し draft でも backend まで到達（cleanup 無し）
            assert response.status_code == 200
            payload = response.json()
            assert payload['success'] is False
            assert payload['error_code'] == 'OpenCodeAPIKeyMissing'
            assert payload['latency_ms'] == 7

    asyncio.run(Run())


def test_health_endpoint_reports_availability(
    ai_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/ai-backends/health が availability を返す。"""

    _ = ai_paths

    def FakeProbe() -> dict[str, object]:
        return {
            'available': True,
            'base_url': 'http://127.0.0.1:4097',
            'host': '127.0.0.1',
            'port': 4097,
            'version': '1.18.13',
            'pinned_version': '1.18.13',
            'pid': 12345,
            'workspace': '/tmp/opencode-workspace',
        }

    monkeypatch.setattr(AIBackendRouter, 'ProbeOpenCodeAvailability', FakeProbe)
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.get('/api/ai-backends/health')
            assert response.status_code == 200
            payload = response.json()
            assert payload['available'] is True
            assert payload['port'] == 4097
            assert payload['version'] == '1.18.13'
            assert response.headers.get('cache-control') == 'no-store'

    asyncio.run(Run())


def test_service_response_never_includes_api_key(ai_paths: Path) -> None:
    """CRUD 応答に api_key が混入しない。"""

    service = AIBackendSettingsStore.createService(
        AIBackendServiceCreate(
            service_name='NoKeyLeak',
            opencode_provider_id='openai',
            opencode_model_id='gpt-4.1-mini',
            auth_mode='ApiKey',
            billing_mode='Metered',
        ),
    )
    AIBackendSettingsStore.setAPIKey(service.service_id, 'sk-must-not-appear')
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            listed = await client.get('/api/ai-backends')
            assert listed.status_code == 200
            assert 'sk-must-not-appear' not in listed.text
            one = await client.get(f'/api/ai-backends/{service.service_id}')
            assert one.status_code == 200
            body = one.json()
            assert body['auth_configured'] is True
            assert 'api_key' not in body
            assert 'sk-must-not-appear' not in one.text

    asyncio.run(Run())


def test_create_custom_provider_service_syncs_runtime_config(
    ai_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """カスタム service 作成 API は provider ID を発行して runtime config を同期する。"""

    _ = ai_paths
    sync_calls = 0

    async def FakeSync() -> None:
        nonlocal sync_calls
        sync_calls += 1

    monkeypatch.setattr(
        AIBackendRouter,
        '_SyncKonomiTVBS4KOpenCodeConfigAfterServiceChange',
        FakeSync,
    )
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.post('/api/ai-backends', json={
                'service_name': 'Custom GLM',
                'opencode_provider_type': 'OpenAICompatible',
                'opencode_model_id': 'glm-5.2',
                'structured_output_mode': 'JSONText',
                'auth_mode': 'ApiKey',
                'billing_mode': 'Metered',
                'api_base_url': 'https://api.example.com/v1',
            })
            assert response.status_code == 201
            payload = response.json()
            assert payload['opencode_provider_type'] == 'OpenAICompatible'
            assert payload['opencode_provider_id'].startswith('konomitv-bs4k-')
            assert payload['structured_output_mode'] == 'JSONText'
            assert payload['auth_configured'] is False
            assert 'api_key' not in payload

    asyncio.run(Run())
    assert sync_calls == 1


def test_provider_list_endpoint_returns_catalog_with_auth_methods(
    ai_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/ai-backends/providers が OpenCode Web 相当のカタログを返す。"""

    _ = ai_paths

    async def FakeListAllProviders(_self: object) -> dict[str, Any]:
        return {
            'all': [
                {
                    'id': 'openai',
                    'name': 'OpenAI',
                    'env': ['OPENAI_API_KEY'],
                    'models': {
                        'gpt-5.6': {
                            'name': 'GPT-5.6',
                            'api': {'id': 'gpt-5.6'},
                            'variants': {
                                'low': {'reasoningEffort': 'low'},
                                'medium': {'reasoningEffort': 'medium'},
                                'high': {'reasoningEffort': 'high'},
                            },
                        },
                        'gpt-5.6-fast': {
                            'name': 'GPT-5.6 Fast',
                            'api': {'id': 'gpt-5.6'},
                            'options': {'serviceTier': 'priority'},
                            'variants': {
                                'low': {'reasoningEffort': 'low'},
                                'medium': {'reasoningEffort': 'medium'},
                                'high': {'reasoningEffort': 'high'},
                            },
                        },
                    },
                },
                {
                    'id': 'deepseek',
                    'name': 'DeepSeek',
                    'env': ['DEEPSEEK_API_KEY'],
                    'models': {
                        'deepseek-chat': {
                            'name': 'DeepSeek Chat',
                            'capabilities': {'WebSearch': True},
                            'default': True,
                        },
                        'deepseek-v4-pro': {
                            'name': 'DeepSeek V4 Pro',
                        },
                    },
                },
                {
                    'id': 'google-vertex',
                    'name': 'Vertex',
                    'env': [
                        'GOOGLE_VERTEX_PROJECT',
                        'GOOGLE_VERTEX_LOCATION',
                        'GOOGLE_APPLICATION_CREDENTIALS',
                    ],
                    'models': {},
                },
                {
                    'id': 'azure',
                    'name': 'Azure',
                    'env': ['AZURE_RESOURCE_NAME', 'AZURE_API_KEY'],
                    'models': {},
                },
                {
                    'id': 'konomitv-bs4k-11111111-1111-4111-8111-111111111111',
                    'name': 'Managed Custom Provider',
                    'models': {},
                },
                {
                    'id': 12345,  # 不正エントリはスキップする
                    'models': {},
                },
            ],
            'default': {'openai': 'gpt-5.6-fast'},
            'connected': ['deepseek', 'google-vertex'],
        }

    async def FakeListProviderAuthMethods(_self: object) -> dict[str, list[dict[str, Any]]]:
        return {
            'openai': [
                {'type': 'oauth', 'label': 'ChatGPT Pro/Plus (browser)'},
                {'type': 'oauth', 'label': 'ChatGPT Pro/Plus (headless)'},
                {'type': 'api', 'label': 'Manually enter API Key'},
            ],
            'azure': [
                {
                    'type': 'api',
                    'label': 'API key',
                    'prompts': [
                        {'type': 'text', 'key': 'resourceName', 'message': 'Enter Azure Resource Name'},
                    ],
                },
            ],
        }

    monkeypatch.setattr(
        AIBackendRouter.OpenCodeClient,
        'listAllProviders',
        FakeListAllProviders,
    )
    monkeypatch.setattr(
        AIBackendRouter.OpenCodeClient,
        'listProviderAuthMethods',
        FakeListProviderAuthMethods,
    )
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.get('/api/ai-backends/providers')
            assert response.status_code == 200
            payload = response.json()
            providers = payload['providers']
            # 不正エントリ除外で 4 件
            assert len(providers) == 4

            openai = next(p for p in providers if p['provider_id'] == 'openai')
            assert openai['provider_name'] == 'OpenAI'
            assert openai['connected'] is False
            assert openai['support_kind'] == 'Supported'
            # OpenCode Web と同じく OAuth ×2 + API Key
            assert len(openai['auth_methods']) == 3
            assert openai['auth_methods'][0]['type'] == 'oauth'
            assert openai['auth_methods'][0]['label'] == 'ChatGPT Pro/Plus (browser)'
            assert openai['auth_methods'][0]['auth_mode'] == 'OAuthSubscription'
            assert openai['auth_methods'][0]['method_index'] == 0
            assert openai['auth_methods'][2]['type'] == 'api'
            assert openai['auth_methods'][2]['auth_mode'] == 'ApiKey'
            assert openai['default_model_id'] == 'gpt-5.6-fast'
            standard_model = next(m for m in openai['models'] if m['model_id'] == 'gpt-5.6')
            fast_model = next(m for m in openai['models'] if m['model_id'] == 'gpt-5.6-fast')
            assert standard_model['variants'] == ['low', 'medium', 'high']
            assert standard_model['openai_fast_mode'] is False
            assert standard_model['openai_paired_model_id'] == 'gpt-5.6-fast'
            assert fast_model['openai_fast_mode'] is True
            assert fast_model['openai_paired_model_id'] == 'gpt-5.6'

            deepseek = next(p for p in providers if p['provider_id'] == 'deepseek')
            assert deepseek['connected'] is True
            assert deepseek['support_kind'] == 'Supported'
            # /provider/auth に無い → ベストエフォート API キー
            assert len(deepseek['auth_methods']) == 1
            assert deepseek['auth_methods'][0]['type'] == 'api'
            chat = next(m for m in deepseek['models'] if m['model_id'] == 'deepseek-chat')
            assert chat['model_name'] == 'DeepSeek Chat'
            assert chat['capabilities'].get('WebSearch') is True

            vertex = next(p for p in providers if p['provider_id'] == 'google-vertex')
            assert vertex['support_kind'] == 'Supported'
            assert len(vertex['auth_methods']) == 1
            assert vertex['auth_methods'][0]['type'] == 'vertex_adc'
            assert vertex['auth_methods'][0]['auth_mode'] == 'VertexAdc'

            azure = next(p for p in providers if p['provider_id'] == 'azure')
            assert azure['support_kind'] == 'UnsupportedComplex'
            assert azure['auth_methods'] == []
            assert response.headers.get('cache-control') == 'no-store'

    asyncio.run(Run())


def test_oauth_start_passes_method_and_returns_url(
    ai_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST oauth/start が method を OpenCode に渡し URL を返す。"""

    _ = ai_paths
    service = AIBackendSettingsStore.createService(AIBackendServiceCreate(
        service_name='OpenAI OAuth',
        opencode_provider_id='openai',
        opencode_model_id='gpt-5.6',
        auth_mode='OAuthSubscription',
        billing_mode='Subscription',
    ))

    async def FakeStart(
        _self: object,
        provider_id: str,
        *,
        method: int = 0,
        inputs: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        assert provider_id == 'openai'
        assert method == 1
        assert inputs is None
        return {
            'url': 'https://auth.openai.com/codex/device',
            'method': 'auto',
            'instructions': 'Enter code: ABCD-1234',
        }

    monkeypatch.setattr(AIBackendRouter.OpenCodeClient, 'startOAuthAuthorize', FakeStart)
    monkeypatch.setattr(AIBackendRouter, 'IsOpenCodeAvailable', lambda: True)
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.post(
                f'/api/ai-backends/{service.service_id}/oauth/start',
                json={'method': 1},
            )
            assert response.status_code == 200
            body = response.json()
            assert body['provider_id'] == 'openai'
            assert body['method'] == 1
            assert body['url'] == 'https://auth.openai.com/codex/device'
            assert body['authorization_method'] == 'auto'
            assert 'ABCD-1234' in (body['instructions'] or '')

    asyncio.run(Run())


def test_oauth_callback_sets_connected(
    ai_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST oauth/callback 成功で oauth_connected が立つ。"""

    _ = ai_paths
    service = AIBackendSettingsStore.createService(AIBackendServiceCreate(
        service_name='OpenAI OAuth',
        opencode_provider_id='openai',
        opencode_model_id='gpt-5.6',
        auth_mode='OAuthSubscription',
        billing_mode='Subscription',
    ))
    assert service.oauth_connected is False

    async def FakeCallback(
        _self: object,
        provider_id: str,
        *,
        method: int = 0,
        code: str | None = None,
    ) -> bool:
        assert provider_id == 'openai'
        assert method == 1
        assert code == 'ABCD-1234'
        return True

    monkeypatch.setattr(AIBackendRouter.OpenCodeClient, 'completeOAuthCallback', FakeCallback)
    monkeypatch.setattr(AIBackendRouter, 'IsOpenCodeAvailable', lambda: True)
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.post(
                f'/api/ai-backends/{service.service_id}/oauth/callback',
                json={'method': 1, 'code': 'ABCD-1234'},
            )
            assert response.status_code == 204

    asyncio.run(Run())
    updated = AIBackendSettingsStore.getService(service.service_id)
    assert updated is not None
    assert updated.oauth_connected is True


def test_provider_list_returns_503_when_opencode_unavailable(
    ai_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """serve が unavailable のとき providers は 503。"""

    _ = ai_paths
    monkeypatch.setattr(AIBackendRouter, 'IsOpenCodeAvailable', lambda: False)
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.get('/api/ai-backends/providers')
            assert response.status_code == 503

    asyncio.run(Run())


# ===== ACP 固定プリセット（Codex / Grok Build） =====


def test_acp_settings_get_and_update_round_trip(
    acp_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ACP 設定は GET でき、PUT で全体置き換え・再取得できる。"""

    _ = acp_paths
    _ = monkeypatch
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            fetched = await client.get('/api/ai-backends/acp-settings')
            assert fetched.status_code == 200
            body = fetched.json()
            assert body['codex']['model'] == 'gpt-5.6-luna'
            assert body['codex']['reasoning_effort'] == 'Medium'
            assert body['grok']['reasoning_effort'] == 'High'

            updated = await client.put('/api/ai-backends/acp-settings', json={
                'codex': {
                    'model': 'gpt-5.6-sol',
                    'reasoning_effort': 'Ultra',
                    'codex_fast_mode_enabled': True,
                    'timeout_sec': 90,
                },
                'grok': {
                    'model': 'grok-4',
                    'reasoning_effort': 'Low',
                    'codex_fast_mode_enabled': True,
                    'timeout_sec': 60,
                },
            })
            assert updated.status_code == 204

            refetched = await client.get('/api/ai-backends/acp-settings')
            assert refetched.status_code == 200
            refetched_body = refetched.json()
            assert refetched_body['codex']['model'] == 'gpt-5.6-sol'
            assert refetched_body['codex']['reasoning_effort'] == 'Ultra'
            assert refetched_body['codex']['codex_fast_mode_enabled'] is True
            assert refetched_body['codex']['timeout_sec'] == 90
            # Grok はモデル固定・Fast 無効へ正規化される。
            assert refetched_body['grok']['model'] is None
            assert refetched_body['grok']['reasoning_effort'] == 'Low'
            assert refetched_body['grok']['codex_fast_mode_enabled'] is False
            assert refetched_body['grok']['timeout_sec'] == 60

    asyncio.run(Run())


@pytest.mark.parametrize('tested_capability', ['CandidateSelection', 'EpisodeLookup'])
def test_acp_connection_test_uses_saved_settings_with_fixed_preset(
    acp_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
    tested_capability: Literal['CandidateSelection', 'EpisodeLookup'],
) -> None:
    """ACP の各機能試験は保存済み ACP 設定を使用する。"""

    _ = acp_paths
    PatchACPAuthImported(monkeypatch)
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

    monkeypatch.setattr(AIBackendRouter, 'test_connection', TestConnection)
    monkeypatch.setattr(AIBackendRouter.RecordedSeriesAIRequest, 'create', CreateAudit)
    app = CreateAdminApp()

    async def Scenario() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://testserver',
        ) as client:
            response = await client.post('/api/ai-backends/acp/test', json={
                'capability': tested_capability,
                'backend_kind': 'AcpCodex',
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
    assert audit_records[0]['purpose'] == 'ConnectionTest'
    assert audit_records[0]['status'] == 'Succeeded'


@pytest.mark.parametrize(
    ('backend', 'expected_message'),
    [
        ('AcpCodex', 'Codex 認証が未取り込み'),
        ('AcpGrok', 'Grok Build 認証が未取り込み'),
    ],
)
def test_acp_connection_test_rejects_missing_auth_before_backend_start(
    acp_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: Literal['AcpCodex', 'AcpGrok'],
    expected_message: str,
) -> None:
    """未設定認証では ACP agent を起動せず、0ms の固定結果を返す。"""

    _ = acp_paths
    monkeypatch.setattr(
        KonomiTVBS4KACPCredentials,
        'getStatus',
        classmethod(
            lambda _cls: SimpleNamespace(
                codex_auth_imported=False,
                grok_auth_imported=False,
                google_adc_available=False,
            )
        ),
    )

    async def TestConnection(*_args: object, **_kwargs: object) -> ConnectionTestResult:
        raise AssertionError('認証 preflight 失敗時に backend を起動してはならない')

    async def CreateAudit(**_kwargs: object) -> object:
        return object()

    monkeypatch.setattr(AIBackendRouter, 'test_connection', TestConnection)
    monkeypatch.setattr(AIBackendRouter.RecordedSeriesAIRequest, 'create', CreateAudit)
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.post('/api/ai-backends/acp/test', json={
                'capability': 'EpisodeLookup',
                'backend_kind': backend,
            })

        assert response.status_code == 200
        assert response.json()['success'] is False
        assert response.json()['latency_ms'] == 0
        assert expected_message in response.json()['message']
        assert response.json()['checks']['backend_connection']['status'] == 'NotRun'

    asyncio.run(Run())


def test_acp_connection_test_rejects_another_running_agent_without_queueing(
    acp_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """別 ACP 実行中の手動接続試験は待機キューへ積まず即時結果を返す。"""

    _ = acp_paths
    operation_lock = asyncio.Lock()
    monkeypatch.setattr(RecordedSeriesAIModule, 'ACP_OPERATION_LOCK', operation_lock)
    PatchACPAuthImported(monkeypatch)

    async def TestConnection(*_args: object, **_kwargs: object) -> ConnectionTestResult:
        raise AssertionError('実行中 preflight 失敗時に backend を起動してはならない')

    async def CreateAudit(**_kwargs: object) -> object:
        return object()

    monkeypatch.setattr(AIBackendRouter, 'test_connection', TestConnection)
    monkeypatch.setattr(AIBackendRouter.RecordedSeriesAIRequest, 'create', CreateAudit)
    app = CreateAdminApp()

    async def Run() -> None:
        await operation_lock.acquire()
        try:
            async with HTTPXAsyncClient(
                transport=ASGITransport(app=app),
                base_url='http://test',
            ) as client:
                response = await asyncio.wait_for(
                    client.post('/api/ai-backends/acp/test', json={
                        'capability': 'CandidateSelection',
                        'backend_kind': 'AcpGrok',
                    }),
                    timeout=0.5,
                )
        finally:
            operation_lock.release()

        assert response.status_code == 200
        assert response.json()['success'] is False
        assert response.json()['latency_ms'] == 0
        assert '別の ACP AI 処理を実行中' in response.json()['message']

    asyncio.run(Run())


def test_acp_connection_test_can_validate_episode_web_search_capability(
    acp_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """話数側の接続試験は Web Search を呼び、候補選択とは別に監査する。"""

    _ = acp_paths
    PatchACPAuthImported(monkeypatch)
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
            message='ACP Web Search 互換を確認しました。',
            checks=SuccessfulEpisodeLookupConnectionChecks(),
            prompt_tokens=20,
            completion_tokens=5,
            http_status=200,
            selected_choice_id='S1E3',
        )

    async def CreateAudit(**kwargs: object) -> object:
        audit_records.append(kwargs)
        return object()

    monkeypatch.setattr(AIBackendRouter, 'test_connection', TestConnection)
    monkeypatch.setattr(AIBackendRouter.RecordedSeriesAIRequest, 'create', CreateAudit)

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/ai-backends/acp/test', json={
                'capability': 'EpisodeLookup',
                'backend_kind': 'AcpCodex',
            })

        assert response.status_code == 200
        assert response.json() == {
            'success': True,
            'latency_ms': 48,
            'model': 'episode-model-response',
            'message': 'ACP Web Search 互換を確認しました。',
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
    assert captured_settings.ai_backend == 'AcpCodex'
    assert captured_api_key is None
    assert has_episode_lookup_capability_proof(
        captured_settings,
        captured_api_key,
    ) is True
    assert audit_records[0]['candidate_ids'] == ['episode-lookup']
    assert audit_records[0]['selected_choice_id'] == 'S1E3'


def test_acp_connection_test_rejects_a_result_from_a_stale_provider_generation(
    acp_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """facade が実使用した世代と現在世代が違う接続成功は proof にしない。"""

    _ = acp_paths
    PatchACPAuthImported(monkeypatch)
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
        assert api_key is None
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

    monkeypatch.setattr(AIBackendRouter, 'test_connection', TestConnection)
    monkeypatch.setattr(AIBackendRouter.RecordedSeriesAIRequest, 'create', CreateAudit)

    async def Run() -> dict[str, object]:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.post('/api/ai-backends/acp/test', json={
                'capability': 'EpisodeLookup',
                'backend_kind': 'AcpCodex',
            })
        assert response.status_code == 200
        return response.json()

    response = asyncio.run(Run())
    assert response['success'] is False
    assert response['message'] == (
        '接続試験中に AI 設定または認証状態が変更されました。もう一度試してください。'
    )
    assert audit_records[0]['status'] == 'Failed'
    assert audit_records[0]['error_code'] == 'ConnectionTestStateChanged'
    current_settings = RecordedSeriesSettings(ai_backend='AcpCodex', ai_enabled=True)
    assert (
        has_episode_lookup_capability_proof(
            current_settings,
            None,
        )
        is False
    )


def test_acp_credentials_status_returns_shared_state(
    acp_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ACP 認証状態 API は認証内容を含まない安全な状態を返す。"""

    _ = acp_paths
    monkeypatch.setattr(
        KonomiTVBS4KACPCredentials,
        'getStatus',
        classmethod(
            lambda _cls: SimpleNamespace(
                codex_host_auth_available=True,
                codex_auth_imported=True,
                codex_auth_imported_at=None,
                grok_host_auth_available=False,
                grok_auth_imported=False,
                grok_auth_imported_at=None,
                google_adc_available=False,
            )
        ),
    )
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.get('/api/ai-backends/acp-credentials')
            assert response.status_code == 200
            body = response.json()
            assert body['acp_operation_running'] is False
            assert body['codex_host_auth_available'] is True
            assert body['codex_auth_imported'] is True
            assert body['grok_host_auth_available'] is False
            assert body['grok_auth_imported'] is False
            assert body['google_adc_available'] is False

    asyncio.run(Run())


def test_acp_credential_import_calls_provider_auth_and_revokes_proof(
    acp_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auth.json 取り込みは provider 管理 API を呼び、backend proof を失効させる。"""

    _ = acp_paths
    monkeypatch.setattr(
        KonomiTVBS4KACPCredentials,
        'importProviderAuth',
        classmethod(lambda _cls, _provider: None),
    )
    invalidated_backends: list[str] = []
    monkeypatch.setattr(
        AIBackendRouter,
        'invalidate_episode_lookup_capability_proof',
        lambda *, backend_kind: invalidated_backends.append(backend_kind),
    )
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.post('/api/ai-backends/acp-credentials/codex/import')
            assert response.status_code == 200
            assert invalidated_backends == ['AcpCodex']

    asyncio.run(Run())


def test_acp_credential_delete_calls_provider_auth_and_revokes_proof(
    acp_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取り込み済み認証の削除は provider 管理 API を呼び、backend proof を失効させる。"""

    _ = acp_paths
    monkeypatch.setattr(
        KonomiTVBS4KACPCredentials,
        'deleteProviderAuth',
        classmethod(lambda _cls, _provider: None),
    )
    invalidated_backends: list[str] = []
    monkeypatch.setattr(
        AIBackendRouter,
        'invalidate_episode_lookup_capability_proof',
        lambda *, backend_kind: invalidated_backends.append(backend_kind),
    )
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            response = await client.delete('/api/ai-backends/acp-credentials/grok')
            assert response.status_code == 200
            assert invalidated_backends == ['AcpGrok']

    asyncio.run(Run())
