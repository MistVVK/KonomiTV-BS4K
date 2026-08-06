# pyright: reportPrivateUsage=false

import asyncio
import json
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
    has_episode_lookup_capability_proof,
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
        'LEGACY_API_KEY_PATH',
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
        ai_backend='OpenCode',
        ai_backend_service_id='00000000-0000-4000-8000-000000000001',
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
    # OpenCode では API キーは fingerprint の核にならない。provider の違いは
    # service_id で表現され、異なる service_id では fingerprint が不一致になる。
    changed_settings = settings.model_copy(
        update={'ai_backend_service_id': '00000000-0000-4000-8000-000000000002'},
    )
    assert has_episode_lookup_capability_proof(changed_settings, 'proof-key') is False
    assert record_episode_lookup_capability_proof(settings, 'proof-key', failure) is False
    assert has_episode_lookup_capability_proof(settings, 'proof-key') is False




def test_episode_lookup_capability_proof_survives_process_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """接続試験の能力証明はディスク永続化され、再読込後も再試験なしで有効。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(
        ai_backend='OpenCode',
        ai_backend_service_id='00000000-0000-4000-8000-000000000001',
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
    # OpenCode では API キーは fingerprint の核にならないため、キー差は proof 不一致にならない。
    assert has_episode_lookup_capability_proof(settings, 'other-key') is True


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
    # デフォルトは AcpCodex（OpenCode は service_id 必須のためデフォルトにできない）。
    assert settings.ai_backend == 'AcpCodex'
    assert settings.ai_backend_service_id is None
    assert settings.acp_model == 'gpt-5.6-luna'
    assert settings.acp_reasoning_effort == 'Medium'
    assert settings.acp_timeout_sec == 120
    # 廃止された OpenAICompatible 系フィールドは存在しない。
    assert 'api_base_url' not in settings.model_dump()
    assert 'daily_ai_request_limit' not in settings.model_dump()


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

    # OpenAICompatible 専用フィールドと日次制限はクリーンブレークで拒否する。
    with pytest.raises(ValueError):
        RecordedSeriesSettingsStore.getSettings()

    # 旧キーを含まない AcpCodex 設定なら読み込める。
    RecordedSeriesSettingsStore.SETTINGS_PATH.write_text(
        json.dumps({
            'enabled': True,
            'ai_enabled': False,
            'ai_backend': 'AcpCodex',
            'acp_model': 'legacy-model',
        }),
        encoding='utf-8',
    )
    settings = RecordedSeriesSettingsStore.getSettings()

    assert settings.ai_enabled is False
    assert settings.ai_candidate_selection_enabled is True
    assert settings.ai_episode_number_search_enabled is False
    assert settings.ai_episode_number_acceptance_mode == 'Always'
    assert settings.ai_backend == 'AcpCodex'
    assert settings.acp_model == 'legacy-model'


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




@pytest.mark.parametrize(
    ('backend', 'backend_settings'),
    [
        ('AcpCodex', {}),
        ('AcpGrok', {}),
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
    """旧 Custom 保存設定は任意コマンドを再実行せず、クリーンブレークで拒否する。"""

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

    # AcpCustom は拒否済み backend のため、保存 JSON は読取時に明示エラーになる。
    with pytest.raises(ValueError):
        RecordedSeriesSettingsStore.getSettings()


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






def test_episode_number_acceptance_mode_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        RecordedSeriesSettings(ai_episode_number_acceptance_mode='Unknown')  # type: ignore[arg-type]
















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




@pytest.mark.parametrize('tested_capability', ['CandidateSelection', 'EpisodeLookup'])
def test_acp_connection_test_uses_unsaved_backend_draft_with_fixed_preset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tested_capability: Literal['CandidateSelection', 'EpisodeLookup'],
) -> None:
    """ACP の各機能試験は保存済み backend ではなく画面の全ドラフトを使用する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    RecordedSeriesSettingsStore.saveSettings(RecordedSeriesSettings())
    monkeypatch.setattr(
        KonomiTVBS4KACPCredentials,
        'getStatus',
        classmethod(
            lambda _cls: SimpleNamespace(
                codex_auth_imported=True,
                grok_auth_imported=True,
                google_adc_available=True,
            )
        ),
    )
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


@pytest.mark.parametrize(
    ('backend', 'expected_message'),
    [
        ('AcpCodex', 'Codex 認証が未取り込み'),
        ('AcpGrok', 'Grok Build 認証が未取り込み'),
    ],
)
def test_acp_connection_test_rejects_missing_auth_before_backend_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    backend: Literal['AcpCodex', 'AcpGrok'],
    expected_message: str,
) -> None:
    """未設定認証では ACP agent を起動せず、0ms の固定結果を返す。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
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

    monkeypatch.setattr(RecordedSeriesRouter, 'test_connection', TestConnection)
    monkeypatch.setattr(RecordedSeriesRouter.RecordedSeriesAIRequest, 'create', CreateAudit)
    app = CreateAdminApp()

    async def Run() -> None:
        async with HTTPXAsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as client:
            request_body: dict[str, object] = {
                'capability': 'EpisodeLookup',
                'ai_backend': backend,
            }
            response = await client.post(
                '/api/recorded-series/settings/test',
                json=request_body,
            )

        assert response.status_code == 200
        assert response.json()['success'] is False
        assert response.json()['latency_ms'] == 0
        assert expected_message in response.json()['message']
        assert response.json()['checks']['backend_connection']['status'] == 'NotRun'

    asyncio.run(Run())


def test_acp_connection_test_rejects_another_running_agent_without_queueing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """別 ACP 実行中の手動接続試験は待機キューへ積まず即時結果を返す。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    operation_lock = asyncio.Lock()
    monkeypatch.setattr(RecordedSeriesAIModule, 'ACP_OPERATION_LOCK', operation_lock)
    monkeypatch.setattr(
        KonomiTVBS4KACPCredentials,
        'getStatus',
        classmethod(
            lambda _cls: SimpleNamespace(
                codex_auth_imported=True,
                grok_auth_imported=True,
                google_adc_available=True,
            )
        ),
    )

    async def TestConnection(*_args: object, **_kwargs: object) -> ConnectionTestResult:
        raise AssertionError('実行中 preflight 失敗時に backend を起動してはならない')

    async def CreateAudit(**_kwargs: object) -> object:
        return object()

    monkeypatch.setattr(RecordedSeriesRouter, 'test_connection', TestConnection)
    monkeypatch.setattr(RecordedSeriesRouter.RecordedSeriesAIRequest, 'create', CreateAudit)
    app = CreateAdminApp()

    async def Run() -> None:
        await operation_lock.acquire()
        try:
            async with HTTPXAsyncClient(
                transport=ASGITransport(app=app),
                base_url='http://test',
            ) as client:
                response = await asyncio.wait_for(
                    client.post(
                        '/api/recorded-series/settings/test',
                        json={
                            'capability': 'CandidateSelection',
                            'ai_backend': 'AcpGrok',
                        },
                    ),
                    timeout=0.5,
                )
        finally:
            operation_lock.release()

        assert response.status_code == 200
        assert response.json()['success'] is False
        assert response.json()['latency_ms'] == 0
        assert '別の ACP AI 処理を実行中' in response.json()['message']

    asyncio.run(Run())




def test_connection_test_can_validate_episode_web_search_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """話数側の接続試験はResponses Web Searchを呼び、候補選択とは別に監査する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    # ACP preflight を通過させるため、Codex 認証は取り込み済み扱いにする。
    monkeypatch.setattr(
        KonomiTVBS4KACPCredentials,
        'getStatus',
        classmethod(
            lambda _cls: SimpleNamespace(
                codex_auth_imported=True,
                grok_auth_imported=False,
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
                    'ai_backend': 'AcpCodex',
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
    assert captured_settings.ai_backend == 'AcpCodex'
    assert captured_api_key is None
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
        ai_backend='OpenCode',
        ai_backend_service_id='00000000-0000-4000-8000-000000000001',
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
        assert api_key is None
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
                        'ai_backend': 'OpenCode',
                        'ai_backend_service_id': settings.ai_backend_service_id,
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
    # ACP preflight を通過させ、fingerprint を決定的にする。
    monkeypatch.setattr(
        KonomiTVBS4KACPCredentials,
        'getStatus',
        classmethod(
            lambda _cls: SimpleNamespace(
                codex_auth_imported=True,
                grok_auth_imported=False,
                google_adc_available=False,
            )
        ),
    )
    monkeypatch.setattr(
        KonomiTVBS4KACPCredentials,
        'getCredentialGeneration',
        classmethod(lambda _cls, _provider: 'test-generation'),
    )
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
                    'ai_backend': 'AcpCodex',
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
        ai_backend='AcpCodex',
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





























































