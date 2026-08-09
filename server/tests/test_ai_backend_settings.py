"""AIBackendSettings / secrets のユニットテスト（Phase 1）。"""

from __future__ import annotations

import json
import stat
from decimal import Decimal
from pathlib import Path

import pytest

from app.metadata.ai.AIBackendSettings import (
    AIBackendService,
    AIBackendServiceCreate,
    AIBackendServiceUpdate,
    AIBackendSettingsStore,
    BuildKonomiTVBS4KOpenCodeProviderID,
    IsRecordedSeriesReferencingService,
    NormalizeAPIBaseURL,
)
from app.metadata.RecordedSeriesSettings import (
    RecordedSeriesSettings,
    RecordedSeriesSettingsStore,
)


@pytest.fixture()
def ai_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """AI 設定・秘密パスを一時ディレクトリへ indirection する。"""

    settings_path = tmp_path / 'ai-backend-settings.json'
    secrets_path = tmp_path / 'secrets' / 'ai-api-keys.json'
    series_path = tmp_path / 'recorded-series-settings.json'
    monkeypatch.setattr(AIBackendSettingsStore, 'SETTINGS_PATH', settings_path)
    monkeypatch.setattr(AIBackendSettingsStore, 'SECRETS_PATH', secrets_path)
    monkeypatch.setattr(RecordedSeriesSettingsStore, 'SETTINGS_PATH', series_path)
    return tmp_path


def test_normalize_api_base_url_rejects_credentials() -> None:
    """URL 内認証情報を拒否する。"""

    with pytest.raises(ValueError):
        NormalizeAPIBaseURL('https://user:pass@example.com/v1')


def test_monthly_limits_zero_normalize_to_none() -> None:
    """月次上限 0 / 空文字は上限なし (None) に正規化する。"""

    service = AIBackendService.model_validate({
        'service_id': '11111111-1111-4111-8111-111111111111',
        'service_name': 'Zero Limit',
        'opencode_provider_id': 'deepseek',
        'opencode_model_id': 'deepseek-chat',
        'auth_mode': 'ApiKey',
        'billing_mode': 'Metered',
        'monthly_token_limit': 0,
        'monthly_cost_limit_usd': '0',
    })
    assert service.monthly_token_limit is None
    assert service.monthly_cost_limit_usd is None


def test_create_and_list_service(ai_paths: Path) -> None:
    """service 作成と一覧。"""

    service = AIBackendSettingsStore.createService(
        AIBackendServiceCreate(
            service_name='DeepSeek',
            opencode_provider_id='deepseek',
            opencode_model_id='deepseek-chat',
            opencode_model_variant='high',
            auth_mode='ApiKey',
            billing_mode='Metered',
            monthly_cost_limit_usd=Decimal('10.00'),
        ),
    )
    assert service.service_id
    listed = AIBackendSettingsStore.listServices()
    assert len(listed) == 1
    assert listed[0].service_name == 'DeepSeek'
    assert listed[0].opencode_model_variant == 'high'
    assert listed[0].getAuditModelLabel() == 'opencode:deepseek/deepseek-chat[high]'
    response = AIBackendSettingsStore.toResponse(service)
    assert response.auth_configured is False
    assert 'api_key' not in response.model_dump()


@pytest.mark.parametrize(
    ('provider_type', 'api_base_url'),
    [
        ('OpenAICompatible', 'https://api.example.com/v1'),
        ('AnthropicCompatible', 'https://api.example.com'),
    ],
)
def test_create_custom_provider_service(
    ai_paths: Path,
    provider_type: str,
    api_base_url: str,
) -> None:
    """互換 API は service 固有 provider ID と構造化出力方式を保存する。"""

    _ = ai_paths
    service = AIBackendSettingsStore.createService(AIBackendServiceCreate.model_validate({
        'service_name': 'Custom Provider',
        'opencode_provider_type': provider_type,
        'opencode_model_id': 'custom-model',
        'structured_output_mode': 'JSONText',
        'auth_mode': 'ApiKey',
        'billing_mode': 'Metered',
        'api_base_url': api_base_url,
    }))
    assert service.opencode_provider_id == BuildKonomiTVBS4KOpenCodeProviderID(service.service_id)
    assert service.opencode_provider_type == provider_type
    assert service.structured_output_mode == 'JSONText'
    assert service.api_base_url == api_base_url


def test_custom_provider_rejects_oauth(ai_paths: Path) -> None:
    """カスタム provider は共有 provider 向け OAuth 認証を受け付けない。"""

    _ = ai_paths
    with pytest.raises(ValueError, match='ApiKey または NoneLocal'):
        AIBackendSettingsStore.createService(AIBackendServiceCreate.model_validate({
            'service_name': 'Invalid Custom Provider',
            'opencode_provider_type': 'OpenAICompatible',
            'opencode_model_id': 'custom-model',
            'auth_mode': 'OAuthSubscription',
            'billing_mode': 'Subscription',
            'api_base_url': 'https://api.example.com/v1',
        }))


def test_reject_legacy_backend_kind(ai_paths: Path) -> None:
    """旧 backend を含む JSON を拒否する。"""

    AIBackendSettingsStore.SETTINGS_PATH.write_text(
        json.dumps({
            'version': 1,
            'services': [{
                'service_id': '11111111-1111-4111-8111-111111111111',
                'service_name': 'legacy',
                'backend_kind': 'OpenAICompatible',
                'opencode_provider_id': 'openai',
                'opencode_model_id': 'gpt',
                'auth_mode': 'ApiKey',
                'billing_mode': 'Metered',
            }],
        }),
        encoding='utf-8',
    )
    with pytest.raises(ValueError, match='Legacy'):
        AIBackendSettingsStore.getDocument()


def test_secrets_atomic_and_mode(ai_paths: Path) -> None:
    """秘密ファイルが 0600 で保存されキーが非公開である。"""

    service = AIBackendSettingsStore.createService(
        AIBackendServiceCreate(
            service_name='KeyTest',
            opencode_provider_id='deepseek',
            opencode_model_id='deepseek-chat',
            auth_mode='ApiKey',
            billing_mode='Metered',
        ),
    )
    AIBackendSettingsStore.setAPIKey(service.service_id, 'sk-test-secret-value')
    assert AIBackendSettingsStore.hasAPIKey(service.service_id) is True
    mode = stat.S_IMODE(AIBackendSettingsStore.SECRETS_PATH.stat().st_mode)
    assert mode == 0o600
    # 応答にキーが出ない
    response = AIBackendSettingsStore.toResponse(
        AIBackendSettingsStore.getService(service.service_id),  # type: ignore[arg-type]
    )
    dumped = json.dumps(response.model_dump(mode='json'))
    assert 'sk-test-secret-value' not in dumped
    assert response.auth_configured is True


def test_delete_api_key(ai_paths: Path) -> None:
    """API キー削除。"""

    service = AIBackendSettingsStore.createService(
        AIBackendServiceCreate(
            service_name='DelKey',
            opencode_provider_id='deepseek',
            opencode_model_id='deepseek-chat',
            auth_mode='ApiKey',
            billing_mode='Metered',
        ),
    )
    AIBackendSettingsStore.setAPIKey(service.service_id, 'sk-delete-me')
    assert AIBackendSettingsStore.deleteAPIKey(service.service_id) is True
    assert AIBackendSettingsStore.hasAPIKey(service.service_id) is False


def test_provider_refcount(ai_paths: Path) -> None:
    """同一 provider の参照カウント。"""

    a = AIBackendSettingsStore.createService(
        AIBackendServiceCreate(
            service_name='A',
            opencode_provider_id='deepseek',
            opencode_model_id='m1',
            auth_mode='ApiKey',
            billing_mode='Metered',
        ),
    )
    b = AIBackendSettingsStore.createService(
        AIBackendServiceCreate(
            service_name='B',
            opencode_provider_id='deepseek',
            opencode_model_id='m2',
            auth_mode='ApiKey',
            billing_mode='Metered',
        ),
    )
    assert AIBackendSettingsStore.countServicesUsingProvider('deepseek') == 2
    assert AIBackendSettingsStore.countServicesUsingProvider(
        'deepseek',
        excluding_service_id=a.service_id,
    ) == 1
    _ = b


def test_delete_service_blocked_when_referenced(ai_paths: Path) -> None:
    """録画シリーズが参照中なら削除判定が True。"""

    service = AIBackendSettingsStore.createService(
        AIBackendServiceCreate(
            service_name='Ref',
            opencode_provider_id='openai',
            opencode_model_id='gpt',
            auth_mode='ApiKey',
            billing_mode='Metered',
        ),
    )
    RecordedSeriesSettingsStore.saveSettings(
        RecordedSeriesSettings(
            ai_enabled=True,
            ai_backend_service_id=service.service_id,
        ),
    )
    assert IsRecordedSeriesReferencingService(service.service_id) is True
    assert IsRecordedSeriesReferencingService(
        '22222222-2222-4222-8222-222222222222',
    ) is False


def test_recorded_series_rejects_legacy_json(ai_paths: Path) -> None:
    """旧 recorded-series-settings を拒否する。"""

    RecordedSeriesSettingsStore.SETTINGS_PATH.write_text(
        json.dumps({
            'enabled': True,
            'ai_enabled': True,
            'ai_backend': 'AcpCodex',
            'daily_ai_request_limit': 20,
        }),
        encoding='utf-8',
    )
    with pytest.raises(ValueError, match='Legacy'):
        RecordedSeriesSettingsStore.getSettings()


def test_recorded_series_service_id_required_when_ai_enabled() -> None:
    """OpenCode を AI 有効で使う場合は service_id 必須。ACP では不要。"""

    with pytest.raises(ValueError):
        RecordedSeriesSettings(
            ai_enabled=True,
            ai_backend='OpenCode',
            ai_backend_service_id=None,
        )
    # AcpCodex / AcpGrok は service_id を参照しないため、AI 有効でも必須にしない。
    RecordedSeriesSettings(ai_enabled=True, ai_backend='AcpCodex')


def test_auth_mode_vertex_requires_project() -> None:
    """VertexAdc は project 必須。"""

    from app.metadata.ai.AIBackendSettings import AIBackendService
    with pytest.raises(ValueError):
        AIBackendService(
            service_id='33333333-3333-4333-8333-333333333333',
            service_name='V',
            opencode_provider_id='google-vertex',
            opencode_model_id='gemini',
            auth_mode='VertexAdc',
            billing_mode='Metered',
        )


def test_update_service_clear_limits(ai_paths: Path) -> None:
    """上限クリアフラグ。"""

    service = AIBackendSettingsStore.createService(
        AIBackendServiceCreate(
            service_name='U',
            opencode_provider_id='deepseek',
            opencode_model_id='m',
            opencode_model_variant='high',
            auth_mode='ApiKey',
            billing_mode='Metered',
            monthly_token_limit=1000,
        ),
    )
    updated = AIBackendSettingsStore.updateService(
        service.service_id,
        AIBackendServiceUpdate(
            clear_opencode_model_variant=True,
            clear_monthly_token_limit=True,
        ),
    )
    assert updated.opencode_model_variant is None
    assert updated.monthly_token_limit is None


def test_reject_old_secrets_format(ai_paths: Path) -> None:
    """旧 recorded-series-api.key 形式の秘密を拒否する。"""

    AIBackendSettingsStore.SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    AIBackendSettingsStore.SECRETS_PATH.write_text(
        json.dumps({'api_base_url': 'https://api.openai.com/v1', 'api_key': 'x'}),
        encoding='utf-8',
    )
    with pytest.raises(ValueError):
        AIBackendSettingsStore.getAPIKey('11111111-1111-4111-8111-111111111111')


def test_opencode_usage_normalize() -> None:
    """usage 正規化の土台。"""

    from app.metadata.ai.opencode_types import NormalizeOpenCodeUsage

    usage = NormalizeOpenCodeUsage(
        {'input': 10, 'output': 5, 'reasoning': 2, 'cache': {'read': 0, 'write': 0}},
        0.0123,
    )
    assert usage['prompt_tokens'] == 10
    assert usage['completion_tokens'] == 5
    assert usage['reasoning_tokens'] == 2
    assert usage['total_tokens'] == 17
    assert usage['estimated_cost_usd'] == 0.0123
