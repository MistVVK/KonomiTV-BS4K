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
