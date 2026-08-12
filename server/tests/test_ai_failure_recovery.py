# pyright: reportPrivateUsage=false

"""AI バックエンド予備・失敗時ポリシーの単体テスト。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.metadata.ai import recorded_series_ai as RecordedSeriesAIModule
from app.metadata.ai.ai_failure_recovery import (
    BuildPrimaryTarget,
    BuildRecoveryTarget,
    ShouldRecoverEpisodeLookupResult,
    ShouldRecoverSeriesMetadataResult,
)
from app.metadata.ai.AIBackendSettings import IsRecordedSeriesReferencingService
from app.metadata.ai.episode_lookup import EpisodeLookupResult
from app.metadata.ai.recorded_series_ai import (
    GetAIExecutionFingerprint,
    get_episode_lookup_provider_fingerprint,
    lookup_episode,
    reset_episode_lookup_capability_proofs_for_tests,
    resolve_series_metadata,
)
from app.metadata.RecordedEpisodeContext import (
    RECORDED_EPISODE_CONTEXT_VERSION,
    RecordedEpisodeContextFile,
    RecordedEpisodeContextLocalParse,
    RecordedEpisodeContextProgram,
    RecordedEpisodeContextSeries,
    RecordedEpisodeLookupContext,
)
from app.metadata.RecordedSeriesCandidates import (
    RecordedSeriesAIError,
    RecordedSeriesProgramPrompt,
)
from app.metadata.RecordedSeriesGeneration import (
    AISeriesMetadataResult,
    SeriesMetadataHints,
)
from app.metadata.RecordedSeriesSettings import (
    AreAIBackendTargetsIdentical,
    RecordedSeriesSettings,
    RecordedSeriesSettingsStore,
)


def ConfigureTemporaryStore(monkeypatch: pytest.MonkeyPatch, temporary_directory: Path) -> None:
    """テストごとに独立した設定保存先を構成する。"""

    monkeypatch.setattr(
        RecordedSeriesSettingsStore,
        'SETTINGS_PATH',
        temporary_directory / 'recorded-series-settings.json',
    )
    monkeypatch.setattr(
        RecordedSeriesAIModule,
        'EPISODE_LOOKUP_CAPABILITY_PROOFS_PATH',
        temporary_directory / 'recorded-series-episode-lookup-proofs.json',
    )
    reset_episode_lookup_capability_proofs_for_tests()


def _EmptyHints() -> SeriesMetadataHints:
    return {
        'local_parse': {
            'series_title': 'Example',
            'season_number': None,
            'episode_number': None,
            'subtitle': None,
        },
        'cluster': {
            'display_title': 'Example',
            'normalized_key': 'example',
            'member_count': 1,
            'representative_programs': [],
        },
        'existing_series': [],
        'wikipedia': [],
    }


def _ProgramPrompt() -> RecordedSeriesProgramPrompt:
    return {
        'title': 'Example #1',
        'description': 'desc',
        'detail_items': [],
        'genres': [],
        'channel_id': None,
        'channel_name': None,
        'broadcast_datetime': '2026-01-01T12:00:00+09:00',
        'duration_seconds': 1800.0,
    }


def _EpisodeContext() -> RecordedEpisodeLookupContext:
    return RecordedEpisodeLookupContext(
        pipeline_version=RECORDED_EPISODE_CONTEXT_VERSION,
        series=RecordedEpisodeContextSeries(
            title='Example',
            genres=[],
            description='',
            known_episode_count=0,
            known_episode_min=None,
            known_episode_max=None,
            known_episode_sample=[],
        ),
        program=RecordedEpisodeContextProgram(
            title='Example #1',
            subtitle=None,
            description='',
            detail_items=[],
            broadcast_datetime='2026-01-01',
            channel=None,
            duration_seconds=1800.0,
        ),
        local_parse=RecordedEpisodeContextLocalParse(
            legacy_value=None,
            season_number=None,
            episode_number=None,
            unresolved_reason='MissingLegacyValue',
        ),
        neighbors=[],
        query_hints=['Example episode 1'],
        file=RecordedEpisodeContextFile(basename=None),
        constraints=[],
    )


def _SeriesResult(
    decision: str,
    *,
    model: str = 'test-model',
) -> AISeriesMetadataResult:
    is_series = decision == 'Series'
    return AISeriesMetadataResult(
        decision=decision,  # type: ignore[arg-type]
        series_title='Example' if is_series else None,
        season_number=1 if is_series else None,
        episode_number=Decimal('1') if is_series else None,
        episode_not_numbered=False,
        episode_no_published_number=False,
        subtitle=None,
        confidence=0.9,
        existing_series_id=None,
        wikipedia_page_id=None,
        rationale_short='ok',
        model=model,
        prompt_tokens=1,
        completion_tokens=1,
        http_status=200,
        latency_ms=10,
    )


def _EpisodeResult(
    outcome: str,
    *,
    model: str = 'test-model',
) -> EpisodeLookupResult:
    resolved = outcome == 'Resolved'
    return EpisodeLookupResult(
        outcome=outcome,  # type: ignore[arg-type]
        season_number=1 if resolved else None,
        episode_number=Decimal('1') if resolved else None,
        confidence=0.9 if outcome in {
            'Resolved',
            'NotNumbered',
            'NoPublishedNumber',
            'InsufficientEvidence',
        } else None,
        rationale_short='ok' if outcome in {
            'Resolved',
            'NotNumbered',
            'NoPublishedNumber',
            'InsufficientEvidence',
        } else None,
        citations=(),
        web_search_performed=outcome in {
            'Resolved',
            'NotNumbered',
            'NoPublishedNumber',
            'InsufficientEvidence',
        },
        model=model,
        prompt_tokens=1,
        completion_tokens=1,
        http_status=200,
        latency_ms=10,
        error_code=None if outcome in {
            'Resolved',
            'NotNumbered',
            'NoPublishedNumber',
            'InsufficientEvidence',
        } else outcome,
        error_message=None if outcome in {
            'Resolved',
            'NotNumbered',
            'NoPublishedNumber',
            'InsufficientEvidence',
        } else 'failed',
    )


class _FakeBackend:
    """呼び出し回数と prompt_variant を記録する假 backend。"""

    def __init__(
        self,
        *,
        backend_kind: str,
        series_results: list[AISeriesMetadataResult | Exception],
        episode_results: list[EpisodeLookupResult | Exception] | None = None,
    ) -> None:
        self.backend_kind = backend_kind
        self._series_results = list(series_results)
        self._episode_results = list(episode_results or [])
        self.series_calls: list[str] = []
        self.episode_calls: list[str] = []

    async def selectCandidate(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def resolveSeriesMetadata(
        self,
        program: Any,
        hints: Any,
        *,
        prompt_variant: str = 'Default',
        execution_guard: Any = None,
        local_validation_attempts: int = 2,
    ) -> AISeriesMetadataResult:
        _ = local_validation_attempts
        if execution_guard is not None:
            execution_guard()
        self.series_calls.append(prompt_variant)
        if not self._series_results:
            raise AssertionError('Unexpected series call')
        item = self._series_results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def lookupEpisode(
        self,
        program: Any,
        *,
        prompt_variant: str = 'Default',
        execution_guard: Any = None,
    ) -> EpisodeLookupResult:
        if execution_guard is not None:
            execution_guard()
        self.episode_calls.append(prompt_variant)
        if not self._episode_results:
            raise AssertionError('Unexpected episode call')
        item = self._episode_results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def testConnection(self, capability: str) -> Any:
        raise NotImplementedError


def test_settings_reject_identical_fallback_backend() -> None:
    """主系と同一の予備 AI は保存を拒否する。"""

    with pytest.raises(ValueError, match='異なるバックエンド'):
        RecordedSeriesSettings(
            ai_enabled=True,
            ai_backend='AcpCodex',
            ai_failure_recovery_strategy='FallbackBackend',
            ai_fallback_backend='AcpCodex',
        )
    with pytest.raises(ValueError, match='異なるバックエンド'):
        RecordedSeriesSettings(
            ai_enabled=True,
            ai_backend='OpenCode',
            ai_backend_service_id='00000000-0000-4000-8000-000000000001',
            ai_failure_recovery_strategy='FallbackBackend',
            ai_fallback_backend='OpenCode',
            ai_fallback_backend_service_id='00000000-0000-4000-8000-000000000001',
        )
    # 別 service なら許可する。
    settings = RecordedSeriesSettings(
        ai_enabled=True,
        ai_backend='OpenCode',
        ai_backend_service_id='00000000-0000-4000-8000-000000000001',
        ai_failure_recovery_strategy='FallbackBackend',
        ai_fallback_backend='OpenCode',
        ai_fallback_backend_service_id='00000000-0000-4000-8000-000000000002',
    )
    assert settings.ai_fallback_backend == 'OpenCode'


def test_settings_clear_fallback_when_not_fallback_strategy() -> None:
    """Fail / Retry では予備設定をクリアする。"""

    settings = RecordedSeriesSettings(
        ai_backend='AcpCodex',
        ai_failure_recovery_strategy='Fail',
        ai_fallback_backend='AcpGrok',
        ai_fallback_backend_service_id=None,
    )
    assert settings.ai_fallback_backend is None
    retry = RecordedSeriesSettings(
        ai_backend='AcpCodex',
        ai_failure_recovery_strategy='RetrySameBackend',
        ai_fallback_backend='AcpGrok',
    )
    assert retry.ai_fallback_backend is None


def test_are_backend_targets_identical() -> None:
    assert AreAIBackendTargetsIdentical(
        primary_backend='AcpCodex',
        primary_service_id=None,
        fallback_backend='AcpCodex',
        fallback_service_id=None,
    ) is True
    assert AreAIBackendTargetsIdentical(
        primary_backend='AcpCodex',
        primary_service_id=None,
        fallback_backend='AcpGrok',
        fallback_service_id=None,
    ) is False


def test_recovery_target_roles() -> None:
    fail = RecordedSeriesSettings(ai_failure_recovery_strategy='Fail')
    assert BuildRecoveryTarget(fail) is None
    retry = RecordedSeriesSettings(
        ai_backend='AcpCodex',
        ai_failure_recovery_strategy='RetrySameBackend',
    )
    target = BuildRecoveryTarget(retry)
    assert target is not None
    assert target.role == 'PrimaryRetry'
    assert target.prompt_variant == 'RecoveryRetry'
    fallback = RecordedSeriesSettings(
        ai_backend='AcpCodex',
        ai_failure_recovery_strategy='FallbackBackend',
        ai_fallback_backend='AcpGrok',
    )
    target = BuildRecoveryTarget(fallback)
    assert target is not None
    assert target.role == 'Fallback'
    assert target.backend_kind == 'AcpGrok'
    assert target.prompt_variant == 'Default'


def test_should_recover_decisions() -> None:
    assert ShouldRecoverSeriesMetadataResult(_SeriesResult('Unresolved')) is True
    assert ShouldRecoverSeriesMetadataResult(_SeriesResult('NotSeries')) is False
    assert ShouldRecoverSeriesMetadataResult(_SeriesResult('Series')) is False
    assert ShouldRecoverEpisodeLookupResult(_EpisodeResult('InsufficientEvidence')) is True
    assert ShouldRecoverEpisodeLookupResult(_EpisodeResult('Resolved')) is False
    assert ShouldRecoverEpisodeLookupResult(_EpisodeResult('SearchFailed')) is True


def test_resolve_series_fail_policy_keeps_unresolved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fail 方針では Unresolved を追加試行せず採用する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(
        ai_enabled=True,
        ai_backend='AcpCodex',
        ai_failure_recovery_strategy='Fail',
    )
    backend = _FakeBackend(
        backend_kind='AcpCodex',
        series_results=[_SeriesResult('Unresolved', model='primary')],
    )

    async def FakeRun(target: Any, operation: Any, **_kwargs: Any) -> Any:
        return await operation(backend)

    monkeypatch.setattr(RecordedSeriesAIModule, '_RunBackendOperation', FakeRun)

    async def Run() -> None:
        result = await resolve_series_metadata(
            _ProgramPrompt(),
            _EmptyHints(),
            settings=settings,
        )
        assert result.decision == 'Unresolved'
        assert len(backend.series_calls) == 1
        assert backend.series_calls[0] == 'Default'
        assert any('adopted' in item for item in result.recovery_attempt_summaries)

    asyncio.run(Run())


def test_resolve_series_retry_same_backend_uses_recovery_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """RetrySameBackend は 2 回目に RecoveryRetry プロンプトを使う。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(
        ai_enabled=True,
        ai_backend='AcpCodex',
        ai_failure_recovery_strategy='RetrySameBackend',
    )
    backend = _FakeBackend(
        backend_kind='AcpCodex',
        series_results=[
            _SeriesResult('Unresolved', model='primary'),
            _SeriesResult('Series', model='primary-retry'),
        ],
    )

    async def FakeRun(target: Any, operation: Any, **_kwargs: Any) -> Any:
        return await operation(backend)

    monkeypatch.setattr(RecordedSeriesAIModule, '_RunBackendOperation', FakeRun)

    async def Run() -> None:
        result = await resolve_series_metadata(
            _ProgramPrompt(),
            _EmptyHints(),
            settings=settings,
        )
        assert result.decision == 'Series'
        assert backend.series_calls == ['Default', 'RecoveryRetry']
        assert result.prompt_tokens == 2
        assert result.completion_tokens == 2
        assert result.latency_ms == 20
        assert len(result.recovery_attempt_summaries) == 2
        assert 'PrimaryRetry' in result.recovery_attempt_summaries[1]
        assert 'adopted' in result.recovery_attempt_summaries[1]

    asyncio.run(Run())


def test_resolve_series_preserves_unresolved_when_recovery_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """回復試行の技術障害では主系の正常な Unresolved を保持する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(
        ai_enabled=True,
        ai_backend='AcpCodex',
        ai_failure_recovery_strategy='RetrySameBackend',
    )
    backend = _FakeBackend(
        backend_kind='AcpCodex',
        series_results=[
            _SeriesResult('Unresolved', model='primary'),
            RecordedSeriesAIError('Timeout', latency_ms=30),
        ],
    )

    async def FakeRun(target: Any, operation: Any, **_kwargs: Any) -> Any:
        return await operation(backend)

    monkeypatch.setattr(RecordedSeriesAIModule, '_RunBackendOperation', FakeRun)

    async def Run() -> None:
        result = await resolve_series_metadata(
            _ProgramPrompt(),
            _EmptyHints(),
            settings=settings,
        )
        assert result.decision == 'Unresolved'
        assert result.latency_ms == 40
        assert ':adopted:' in result.recovery_attempt_summaries[0]
        assert ':not-adopted:' in result.recovery_attempt_summaries[1]
        assert 'latency_ms=30' in result.recovery_attempt_summaries[1]

    asyncio.run(Run())


def test_resolve_series_fallback_backend_independent_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """FallbackBackend は予備を通常プロンプトで独立実行する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(
        ai_enabled=True,
        ai_backend='AcpCodex',
        ai_failure_recovery_strategy='FallbackBackend',
        ai_fallback_backend='AcpGrok',
    )
    primary = _FakeBackend(
        backend_kind='AcpCodex',
        series_results=[RecordedSeriesAIError('InvalidJSON')],
    )
    fallback = _FakeBackend(
        backend_kind='AcpGrok',
        series_results=[_SeriesResult('NotSeries', model='fallback')],
    )

    async def FakeRun(target: Any, operation: Any, **_kwargs: Any) -> Any:
        backend = primary if target.backend_kind == 'AcpCodex' else fallback
        return await operation(backend)

    monkeypatch.setattr(RecordedSeriesAIModule, '_RunBackendOperation', FakeRun)

    async def Run() -> None:
        result = await resolve_series_metadata(
            _ProgramPrompt(),
            _EmptyHints(),
            settings=settings,
        )
        assert result.decision == 'NotSeries'
        assert primary.series_calls == ['Default']
        assert fallback.series_calls == ['Default']
        assert any('Fallback' in item for item in result.recovery_attempt_summaries)

    asyncio.run(Run())


def test_lookup_episode_fallback_on_insufficient_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """InsufficientEvidence でも失敗時ポリシーを適用する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(
        ai_enabled=True,
        ai_backend='AcpCodex',
        ai_failure_recovery_strategy='FallbackBackend',
        ai_fallback_backend='AcpGrok',
    )
    primary = _FakeBackend(
        backend_kind='AcpCodex',
        series_results=[],
        episode_results=[_EpisodeResult('InsufficientEvidence', model='primary')],
    )
    fallback = _FakeBackend(
        backend_kind='AcpGrok',
        series_results=[],
        episode_results=[_EpisodeResult('Resolved', model='fallback')],
    )

    async def FakeRun(target: Any, operation: Any, **_kwargs: Any) -> Any:
        backend = primary if target.backend_kind == 'AcpCodex' else fallback
        return await operation(backend)

    monkeypatch.setattr(RecordedSeriesAIModule, '_RunBackendOperation', FakeRun)

    async def Run() -> None:
        result = await lookup_episode(_EpisodeContext(), settings=settings)
        assert result.outcome == 'Resolved'
        assert primary.episode_calls == ['Default']
        assert fallback.episode_calls == ['Default']
        assert len(result.recovery_attempt_summaries) == 2
        assert result.prompt_tokens == 2
        assert result.completion_tokens == 2
        assert result.latency_ms == 20

    asyncio.run(Run())


def test_lookup_episode_preserves_insufficient_evidence_when_recovery_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """回復先の技術障害では主系の InsufficientEvidence を保持する。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(
        ai_enabled=True,
        ai_backend='AcpCodex',
        ai_failure_recovery_strategy='RetrySameBackend',
    )
    backend = _FakeBackend(
        backend_kind='AcpCodex',
        series_results=[],
        episode_results=[
            _EpisodeResult('InsufficientEvidence', model='primary'),
            _EpisodeResult('SearchFailed', model='retry'),
        ],
    )

    async def FakeRun(target: Any, operation: Any, **_kwargs: Any) -> Any:
        return await operation(backend)

    monkeypatch.setattr(RecordedSeriesAIModule, '_RunBackendOperation', FakeRun)

    async def Run() -> None:
        result = await lookup_episode(_EpisodeContext(), settings=settings)
        assert result.outcome == 'InsufficientEvidence'
        assert result.latency_ms == 20
        assert ':adopted:' in result.recovery_attempt_summaries[0]
        assert ':not-adopted:' in result.recovery_attempt_summaries[1]

    asyncio.run(Run())


def test_resolve_series_never_exceeds_two_attempts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """どの方針でも AI 実行は最大 2 試行。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(
        ai_enabled=True,
        ai_backend='AcpCodex',
        ai_failure_recovery_strategy='RetrySameBackend',
    )
    backend = _FakeBackend(
        backend_kind='AcpCodex',
        series_results=[
            RecordedSeriesAIError('Timeout'),
            RecordedSeriesAIError('Timeout'),
        ],
    )
    calls = {'count': 0}

    async def FakeRun(target: Any, operation: Any, **_kwargs: Any) -> Any:
        calls['count'] += 1
        return await operation(backend)

    monkeypatch.setattr(RecordedSeriesAIModule, '_RunBackendOperation', FakeRun)

    async def Run() -> None:
        with pytest.raises(RecordedSeriesAIError) as raised:
            await resolve_series_metadata(
                _ProgramPrompt(),
                _EmptyHints(),
                settings=settings,
            )
        assert calls['count'] == 2
        assert len(raised.value.recovery_attempt_summaries) == 2

    asyncio.run(Run())


def test_acp_recovery_attempts_share_one_hard_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主系と ACP 回復試行は同じ絶対期限を facade から受け取る。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(
        ai_enabled=True,
        ai_backend='AcpCodex',
        ai_failure_recovery_strategy='RetrySameBackend',
    )
    backend = _FakeBackend(
        backend_kind='AcpCodex',
        series_results=[
            _SeriesResult('Unresolved'),
            _SeriesResult('Series'),
        ],
    )
    deadlines: list[float | None] = []

    async def FakeRun(target: Any, operation: Any, **kwargs: Any) -> Any:
        deadlines.append(kwargs.get('acp_hard_deadline'))
        return await operation(backend)

    monkeypatch.setattr(RecordedSeriesAIModule, '_RunBackendOperation', FakeRun)

    async def Run() -> None:
        result = await resolve_series_metadata(
            _ProgramPrompt(),
            _EmptyHints(),
            settings=settings,
        )
        assert result.decision == 'Series'
        assert len(deadlines) == 2
        assert deadlines[0] is not None
        assert deadlines[1] == deadlines[0]

    asyncio.run(Run())


def test_fingerprint_includes_failure_recovery_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """fingerprint に失敗時ポリシーと予備設定が含まれる。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    base = RecordedSeriesSettings(
        ai_backend='AcpCodex',
        ai_failure_recovery_strategy='Fail',
    )
    changed = RecordedSeriesSettings(
        ai_backend='AcpCodex',
        ai_failure_recovery_strategy='RetrySameBackend',
    )
    monkeypatch.setattr(
        RecordedSeriesAIModule,
        'get_audit_model',
        lambda settings=None: 'acp:codex:test',
    )
    from app.metadata.ai import KonomiTVBS4KACPCredentials as ACPCredModule
    monkeypatch.setattr(
        ACPCredModule.KonomiTVBS4KACPCredentials,
        'getCredentialGeneration',
        staticmethod(lambda provider: 'gen-1'),
    )
    fp1 = get_episode_lookup_provider_fingerprint(base, None)
    fp2 = get_episode_lookup_provider_fingerprint(changed, None)
    assert fp1 != fp2


def test_execution_fingerprint_includes_fallback_acp_settings_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """予備 ACP のモデル設定と認証世代も実行 fingerprint に含める。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    settings = RecordedSeriesSettings(
        ai_backend='AcpCodex',
        ai_failure_recovery_strategy='FallbackBackend',
        ai_fallback_backend='AcpGrok',
    )
    generation = {'grok': 'generation-a'}

    monkeypatch.setattr(
        RecordedSeriesAIModule.KonomiTVBS4KACPCredentials,
        'getCredentialGeneration',
        classmethod(lambda _cls, provider: generation.get(provider, 'codex-generation')),
    )
    first = GetAIExecutionFingerprint(settings)
    generation['grok'] = 'generation-b'
    second = GetAIExecutionFingerprint(settings)
    assert first != second


def test_fallback_service_is_protected_from_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """予備として参照中の OpenCode service は削除保護対象。"""

    ConfigureTemporaryStore(monkeypatch, tmp_path)
    service_id = '00000000-0000-4000-8000-000000000099'
    monkeypatch.setattr(
        RecordedSeriesSettingsStore,
        'isFallbackAIBackendConfigured',
        classmethod(lambda _cls, _settings=None: True),
    )
    RecordedSeriesSettingsStore.saveSettings(
        RecordedSeriesSettings(
            ai_enabled=True,
            ai_backend='AcpCodex',
            ai_failure_recovery_strategy='FallbackBackend',
            ai_fallback_backend='OpenCode',
            ai_fallback_backend_service_id=service_id,
        ),
    )
    assert IsRecordedSeriesReferencingService(service_id) is True
    assert IsRecordedSeriesReferencingService(
        '00000000-0000-4000-8000-000000000001',
    ) is False


def test_primary_target_builder() -> None:
    settings = RecordedSeriesSettings(
        ai_backend='OpenCode',
        ai_backend_service_id='00000000-0000-4000-8000-000000000001',
    )
    target = BuildPrimaryTarget(settings)
    assert target.role == 'Primary'
    assert target.service_id == '00000000-0000-4000-8000-000000000001'
    assert target.prompt_variant == 'Default'
