"""AIBackendRouter の unit テスト（Phase 2: connection-test / availability）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient

from app.metadata.ai.AIBackendSettings import (
    AIBackendServiceCreate,
    AIBackendSettingsStore,
)
from app.metadata.ai.backends import ConnectionTestResult
from app.metadata.ai.opencode_backend import OpenCodeBackend
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


def test_connection_test_draft_keeps_shared_provider_auth(
    ai_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 provider を使う保存 service がある場合は deleteAuth しない。"""

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

    class FakeClient:
        async def deleteAuth(self, provider_id: str) -> None:
            deleted.append(provider_id)

    async def FakeTestConnection(self: OpenCodeBackend, capability: str) -> ConnectionTestResult:
        _ = (self, capability)
        return ConnectionTestResult(
            success=False,
            latency_ms=5,
            model='opencode:deepseek/deepseek-chat',
            message='fail',
            error_code='HTTP401',
            http_status=401,
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
            payload = response.json()
            assert payload['success'] is False
            assert payload['error_code'] == 'HTTP401'
            assert deleted == []

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
                            'default': True,
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
                    'id': 12345,  # 不正エントリはスキップする
                    'models': {},
                },
            ],
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
            assert openai['default_model_id'] == 'gpt-5.6'

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
