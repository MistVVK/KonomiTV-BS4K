"""OpenCodeBackend / client 抽出ロジックのユニットテスト（Phase 2）。"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from app.metadata.ai.AIAPIUsageLedger import AIAPIUsageLedger, AIAPIUsageReservation
from app.metadata.ai.AIBackendSettings import AIBackendService
from app.metadata.ai.episode_lookup import EpisodeLookupResult
from app.metadata.ai.opencode_backend import OpenCodeBackend
from app.metadata.ai.opencode_client import (
    ExtractOpenCodeJSONObjectFromText,
    ExtractOpenCodeStructuredOutput,
    ExtractOpenCodeUsage,
    ExtractOpenCodeWebToolEvidence,
    OpenCodeClient,
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
    RecordedSeriesProgramDetailItem,
    RecordedSeriesProgramPrompt,
    SeriesChoiceCandidate,
)
from app.metadata.RecordedSeriesGeneration import (
    AISeriesMetadataResult,
    SeriesMetadataClusterHint,
    SeriesMetadataClusterProgramHint,
    SeriesMetadataExistingSeriesHint,
    SeriesMetadataHints,
    SeriesMetadataLocalParseHint,
    SeriesMetadataWikipediaHint,
)


class _NoopProviderLease:
    """provider lease が不要な backend unit 向け async context manager。"""

    async def __aenter__(self) -> _NoopProviderLease:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def _service(**overrides: Any) -> AIBackendService:
    base = {
        'service_id': str(uuid4()),
        'service_name': 'DeepSeek Test',
        'opencode_provider_id': 'deepseek',
        'opencode_model_id': 'deepseek-chat',
        'auth_mode': 'ApiKey',
        'billing_mode': 'Metered',
    }
    base.update(overrides)
    return AIBackendService.model_validate(base)


@pytest.fixture(autouse=True)
def _mock_opencode_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config 未初期化の unit でも OpenCodeClient を生成できるようにする。"""

    monkeypatch.setattr(
        'app.metadata.ai.opencode_client.GetOpenCodeBaseURL',
        lambda: 'http://opencode.test',
    )


@pytest.fixture(autouse=True)
def _skip_monthly_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """backend unit は DB 無しで回すため台帳を no-op にする。"""

    from decimal import Decimal

    async def FakeReserve(
        _cls: type[AIAPIUsageLedger],
        service: AIBackendService,
        **_kwargs: Any,
    ) -> AIAPIUsageReservation:
        return AIAPIUsageReservation(
            reservation_id='test',
            service_id=service.service_id,
            year_month='2026-08',
            billing_mode=service.billing_mode,
            skipped=True,
            reserved_total_tokens=0,
            reserved_estimated_cost_usd=Decimal('0'),
        )

    async def FakeSettle(
        _cls: type[AIAPIUsageLedger],
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        return None

    async def FakeRelease(
        _cls: type[AIAPIUsageLedger],
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        return None

    monkeypatch.setattr(AIAPIUsageLedger, 'Reserve', classmethod(FakeReserve))
    monkeypatch.setattr(AIAPIUsageLedger, 'Settle', classmethod(FakeSettle))
    monkeypatch.setattr(AIAPIUsageLedger, 'Release', classmethod(FakeRelease))


def _program() -> RecordedSeriesProgramPrompt:
    return RecordedSeriesProgramPrompt(
        title='Test',
        description='Desc',
        detail_items=[RecordedSeriesProgramDetailItem(name='Detail', value='Value')],
        genres=['Anime'],
        channel_id='NHKBSP',
        channel_name='Test Channel',
        broadcast_datetime='2024-01-01T20:00:00+09:00',
        duration_seconds=1800.0,
    )


def _hints() -> SeriesMetadataHints:
    return SeriesMetadataHints(
        local_parse=SeriesMetadataLocalParseHint(
            series_title='Connection Test Series',
            season_number=None,
            episode_number=None,
            subtitle=None,
        ),
        cluster=SeriesMetadataClusterHint(
            display_title='Connection Test Series',
            normalized_key='connectiontestseries',
            member_count=1,
            representative_programs=[
                SeriesMetadataClusterProgramHint(
                    title='Test',
                    description='Desc',
                    broadcast_datetime='2024-01-01T20:00:00+09:00',
                    duration_seconds=1800.0,
                ),
            ],
        ),
        existing_series=[
            SeriesMetadataExistingSeriesHint(
                id=1,
                title='Connection Test Series',
                description='',
                wikipedia_page_id=None,
                similarity=1.0,
                match_reason='NormalizedExact',
            ),
        ],
        wikipedia=[
            SeriesMetadataWikipediaHint(
                page_id=1,
                title='Connection Test Series',
                extract='',
            ),
        ],
    )


def test_extract_structured_output_from_tool_part() -> None:
    """StructuredOutput tool の input を取り出す。"""

    message = {
        'info': {
            'tokens': {
                'input': 10,
                'output': 5,
                'reasoning': 0,
                'cache': {'read': 0, 'write': 0},
            },
            'cost': 0.001,
        },
        'parts': [
            {'type': 'step-start'},
            {
                'type': 'tool',
                'tool': 'StructuredOutput',
                'state': {
                    'status': 'completed',
                    'input': {'choice_id': 'series:1', 'confidence': 0.9},
                },
            },
            {
                'type': 'step-finish',
                'tokens': {
                    'input': 10,
                    'output': 5,
                    'reasoning': 0,
                    'cache': {'read': 0, 'write': 0},
                },
                'cost': 0.001,
            },
        ],
    }
    structured = ExtractOpenCodeStructuredOutput(message)
    assert structured == {'choice_id': 'series:1', 'confidence': 0.9}
    usage = ExtractOpenCodeUsage(message)
    assert usage['prompt_tokens'] == 10
    assert usage['completion_tokens'] == 5
    assert usage['estimated_cost_usd'] == 0.001


def test_provider_lease_concurrent_same_key_mutates_auth_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同じ provider・同じキーの並行 session は PUT/dispose 1 回で lease を共有する。"""

    client = OpenCodeClient(base_url='http://same-key.test')
    auth_mutations: list[tuple[str, str]] = []
    active_count = 0
    maximum_active_count = 0
    both_active = asyncio.Event()

    async def FakePut(provider_id: str, api_key: str) -> None:
        auth_mutations.append((provider_id, api_key))

    monkeypatch.setattr(client, '_putApiKeyUnlocked', FakePut)

    async def UseProvider() -> None:
        nonlocal active_count, maximum_active_count
        lease = await client.acquireProviderLease('deepseek', api_key='same-secret')
        async with lease:
            active_count += 1
            maximum_active_count = max(maximum_active_count, active_count)
            if active_count == 2:
                both_active.set()
            await asyncio.wait_for(both_active.wait(), timeout=1)
            active_count -= 1

    async def Run() -> None:
        await asyncio.gather(UseProvider(), UseProvider())

    asyncio.run(Run())
    assert auth_mutations == [('deepseek', 'same-secret')]
    assert maximum_active_count == 2


def test_provider_lease_concurrent_different_keys_waits_for_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同じ provider の別キー PUT は利用中 session が lease を解放するまで待つ。"""

    client = OpenCodeClient(base_url='http://different-key.test')
    auth_mutations: list[str] = []
    first_acquired = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    second_acquired = asyncio.Event()

    async def FakePut(_provider_id: str, api_key: str) -> None:
        auth_mutations.append(api_key)

    monkeypatch.setattr(client, '_putApiKeyUnlocked', FakePut)

    async def UseFirstKey() -> None:
        lease = await client.acquireProviderLease('deepseek', api_key='first-secret')
        async with lease:
            first_acquired.set()
            await release_first.wait()

    async def UseSecondKey() -> None:
        await first_acquired.wait()
        second_started.set()
        lease = await client.acquireProviderLease('deepseek', api_key='second-secret')
        async with lease:
            second_acquired.set()

    async def Run() -> None:
        first_task = asyncio.create_task(UseFirstKey())
        second_task = asyncio.create_task(UseSecondKey())
        await second_started.wait()
        await asyncio.sleep(0)
        assert second_acquired.is_set() is False
        assert auth_mutations == ['first-secret']
        release_first.set()
        await asyncio.gather(first_task, second_task)

    asyncio.run(Run())
    assert auth_mutations == ['first-secret', 'second-secret']


def test_provider_auth_delete_waits_for_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一時 auth cleanup は同じ provider の active session を途中で dispose しない。"""

    client = OpenCodeClient(base_url='http://delete-wait.test')
    deleted: list[str] = []
    lease_acquired = asyncio.Event()
    release_lease = asyncio.Event()
    delete_started = asyncio.Event()

    async def FakePut(_provider_id: str, _api_key: str) -> None:
        return None

    async def FakeDelete(provider_id: str) -> None:
        deleted.append(provider_id)

    monkeypatch.setattr(client, '_putApiKeyUnlocked', FakePut)
    monkeypatch.setattr(client, '_deleteAuthUnlocked', FakeDelete)

    async def UseProvider() -> None:
        lease = await client.acquireProviderLease('deepseek', api_key='temporary-secret')
        async with lease:
            lease_acquired.set()
            await release_lease.wait()

    async def DeleteAuth() -> None:
        await lease_acquired.wait()
        delete_started.set()
        await client.deleteAuth('deepseek')

    async def Run() -> None:
        use_task = asyncio.create_task(UseProvider())
        delete_task = asyncio.create_task(DeleteAuth())
        await delete_started.wait()
        await asyncio.sleep(0)
        assert deleted == []
        release_lease.set()
        await asyncio.gather(use_task, delete_task)

    asyncio.run(Run())
    assert deleted == ['deepseek']


def test_provider_leases_for_different_providers_do_not_block_each_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider ID が異なる認証変更は互いを不要に直列化しない。"""

    client = OpenCodeClient(base_url='http://different-provider.test')
    entered_providers: set[str] = set()
    both_entered = asyncio.Event()

    async def FakePut(provider_id: str, _api_key: str) -> None:
        entered_providers.add(provider_id)
        if len(entered_providers) == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=1)

    monkeypatch.setattr(client, '_putApiKeyUnlocked', FakePut)

    async def UseProvider(provider_id: str) -> None:
        lease = await client.acquireProviderLease(provider_id, api_key='shared-secret')
        async with lease:
            return None

    async def Run() -> None:
        await asyncio.gather(UseProvider('deepseek'), UseProvider('openai'))

    asyncio.run(Run())
    assert entered_providers == {'deepseek', 'openai'}


def test_runtime_config_reload_waits_for_active_provider_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """instance 全体の config 再読込は実行中 provider session の完了を待つ。"""

    client = OpenCodeClient(base_url='http://config-reload.test')
    lease_acquired = asyncio.Event()
    release_lease = asyncio.Event()
    reload_started = asyncio.Event()
    disposed = asyncio.Event()

    async def FakeDispose() -> None:
        disposed.set()

    monkeypatch.setattr(client, '_disposeInstance', FakeDispose)

    async def UseProvider() -> None:
        lease = await client.acquireProviderLease('custom-provider')
        async with lease:
            lease_acquired.set()
            await release_lease.wait()

    async def Reload() -> None:
        await lease_acquired.wait()
        reload_started.set()
        await client.reloadConfiguration()

    async def Run() -> None:
        use_task = asyncio.create_task(UseProvider())
        reload_task = asyncio.create_task(Reload())
        await reload_started.wait()
        await asyncio.sleep(0)
        assert disposed.is_set() is False
        release_lease.set()
        await asyncio.gather(use_task, reload_task)

    asyncio.run(Run())
    assert disposed.is_set() is True


def test_select_candidate_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """候補選択が structured 結果を検証して返す。"""

    service = _service()
    backend = OpenCodeBackend(service, api_key='sk-test')

    async def FakeEnsure() -> _NoopProviderLease:
        return _NoopProviderLease()

    async def FakeRunStructured(**_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any], int]:
        usage = {
            'prompt_tokens': 11,
            'completion_tokens': 4,
            'reasoning_tokens': 0,
            'total_tokens': 15,
            'estimated_cost_usd': 0.0,
        }
        return {'choice_id': 'series:1', 'confidence': 0.95}, usage, 42

    monkeypatch.setattr(backend, 'ensureAuthInjected', FakeEnsure)
    monkeypatch.setattr(backend, '_runStructured', FakeRunStructured)

    candidates: list[SeriesChoiceCandidate] = [
        {
            'choice_id': 'series:1',
            'kind': 'ExistingSeries',
            'title': 'A',
            'description': 'd',
        },
        {
            'choice_id': 'unresolved',
            'kind': 'Unresolved',
            'title': '?',
            'description': '',
        },
    ]
    result = asyncio.run(backend.selectCandidate(_program(), candidates))
    assert result.choice_id == 'series:1'
    assert result.confidence == 0.95
    assert result.prompt_tokens == 11
    assert result.http_status == 200


def test_select_candidate_rejects_outside_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """候補外 ID は拒否する。"""

    service = _service()
    backend = OpenCodeBackend(service, api_key='sk-test')
    calls = {'n': 0}

    async def FakeEnsure() -> _NoopProviderLease:
        return _NoopProviderLease()

    async def FakeRunStructured(**_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any], int]:
        calls['n'] += 1
        usage = {
            'prompt_tokens': 1,
            'completion_tokens': 1,
            'reasoning_tokens': 0,
            'total_tokens': 2,
            'estimated_cost_usd': None,
        }
        return {'choice_id': 'invented', 'confidence': 0.99}, usage, 10

    monkeypatch.setattr(backend, 'ensureAuthInjected', FakeEnsure)
    monkeypatch.setattr(backend, '_runStructured', FakeRunStructured)

    candidates: list[SeriesChoiceCandidate] = [
        {
            'choice_id': 'series:1',
            'kind': 'ExistingSeries',
            'title': 'A',
            'description': '',
        },
    ]
    with pytest.raises(RecordedSeriesAIError) as exc_info:
        asyncio.run(backend.selectCandidate(_program(), candidates))
    assert exc_info.value.code == 'ChoiceOutsideCandidateSet'
    assert calls['n'] == 2


def test_resolve_series_metadata_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """シリーズ生成が ValidateSeriesMetadataOutput を通る。"""

    service = _service()
    backend = OpenCodeBackend(service, api_key='sk-test')

    async def FakeEnsure() -> _NoopProviderLease:
        return _NoopProviderLease()

    async def FakeRunStructured(**_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any], int]:
        usage = {
            'prompt_tokens': 20,
            'completion_tokens': 8,
            'reasoning_tokens': 0,
            'total_tokens': 28,
            'estimated_cost_usd': 0.01,
        }
        return {
            'decision': 'Series',
            'series_title': 'Connection Test Series',
            'season_number': 1,
            'episode_number': '3',
            'subtitle': 'Ep',
            'confidence': 0.9,
            'existing_series_id': 1,
            'wikipedia_page_id': None,
            'rationale_short': 'ok',
        }, usage, 55

    monkeypatch.setattr(backend, 'ensureAuthInjected', FakeEnsure)
    monkeypatch.setattr(backend, '_runStructured', FakeRunStructured)

    result = asyncio.run(backend.resolveSeriesMetadata(_program(), _hints()))
    assert result.decision == 'Series'
    assert result.series_title == 'Connection Test Series'
    assert result.existing_series_id == 1


def test_resolve_series_metadata_retries_invalid_schema_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail 方針でも従来の OpenCode schema session 再作成を維持する。"""

    service = _service()
    backend = OpenCodeBackend(service, api_key='sk-test')
    calls = {'count': 0}

    async def FakeEnsure() -> _NoopProviderLease:
        return _NoopProviderLease()

    async def FakeRunStructured(**_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any], int]:
        calls['count'] += 1
        usage = {
            'prompt_tokens': 1,
            'completion_tokens': 1,
            'reasoning_tokens': 0,
            'total_tokens': 2,
            'estimated_cost_usd': None,
        }
        if calls['count'] == 1:
            return {'decision': 'Series'}, usage, 10
        return {
            'decision': 'Series',
            'series_title': 'Connection Test Series',
            'season_number': 1,
            'episode_number': '3',
            'subtitle': 'Ep',
            'confidence': 0.9,
            'existing_series_id': 1,
            'wikipedia_page_id': None,
            'rationale_short': 'ok',
        }, usage, 20

    monkeypatch.setattr(backend, 'ensureAuthInjected', FakeEnsure)
    monkeypatch.setattr(backend, '_runStructured', FakeRunStructured)

    result = asyncio.run(backend.resolveSeriesMetadata(_program(), _hints()))
    assert result.decision == 'Series'
    assert calls['count'] == 2


def test_connection_test_candidate_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    """接続試験がシリーズ生成成功を success にする。"""

    service = _service()
    backend = OpenCodeBackend(service, api_key='sk-test')

    async def FakeResolve(program: Any, hints: Any) -> AISeriesMetadataResult:
        _ = (program, hints)
        return AISeriesMetadataResult(
            decision='Series',
            series_title='Connection Test Series',
            season_number=1,
            episode_number=Decimal('1'),
            episode_not_numbered=False,
            episode_no_published_number=False,
            subtitle=None,
            confidence=0.9,
            existing_series_id=1,
            wikipedia_page_id=None,
            rationale_short='ok',
            model='opencode:deepseek/deepseek-chat',
            prompt_tokens=10,
            completion_tokens=5,
            http_status=200,
            latency_ms=33,
        )

    monkeypatch.setattr(backend, '_resolveSeriesMetadataUnlocked', FakeResolve)
    result = asyncio.run(backend.testConnection('CandidateSelection'))
    assert result.success is True
    assert result.latency_ms == 33
    assert result.prompt_tokens == 10


def _lookup_context() -> RecordedEpisodeLookupContext:
    return RecordedEpisodeLookupContext(
        pipeline_version=RECORDED_EPISODE_CONTEXT_VERSION,
        series=RecordedEpisodeContextSeries(
            title='Test Series',
            genres=['Anime'],
            description='',
            known_episode_count=0,
            known_episode_min=None,
            known_episode_max=None,
            known_episode_sample=[],
        ),
        program=RecordedEpisodeContextProgram(
            title='Test Episode',
            subtitle=None,
            description='desc',
            detail_items=[],
            broadcast_datetime='2024-01-01',
            channel=None,
            duration_seconds=0.0,
        ),
        local_parse=RecordedEpisodeContextLocalParse(
            legacy_value=None,
            season_number=None,
            episode_number=None,
            unresolved_reason='MissingLegacyValue',
        ),
        neighbors=[],
        file=RecordedEpisodeContextFile(basename=None),
        constraints=[],
    )


def test_extract_web_tool_evidence_from_completed_websearch() -> None:
    """完了 websearch から public citation を取り出す。"""

    message = {
        'parts': [
            {
                'type': 'tool',
                'tool': 'websearch',
                'state': {
                    'status': 'completed',
                    'output': {
                        'sources': [
                            {
                                'url': 'https://example.com/episode/1',
                                'title': 'Official episode list',
                            },
                            {
                                'url': 'http://127.0.0.1/private',
                                'title': 'private',
                            },
                        ],
                    },
                },
            },
            {
                'type': 'tool',
                'tool': 'StructuredOutput',
                'state': {
                    'status': 'completed',
                    'input': {
                        'outcome': 'Resolved',
                        'season_number': 1,
                        'episode_number': '12',
                        'confidence': 0.9,
                        'rationale_short': 'official list',
                    },
                },
            },
        ],
    }
    evidence = ExtractOpenCodeWebToolEvidence(message)
    assert evidence['web_search_performed'] is True
    assert evidence['completed_web_calls'] == 1
    urls = [item['url'] for item in evidence['citations']]
    assert 'https://example.com/episode/1' in urls
    assert 'http://127.0.0.1/private' not in urls
    structured = ExtractOpenCodeStructuredOutput(message)
    assert structured is not None
    assert structured['outcome'] == 'Resolved'


def test_extract_web_tool_evidence_without_tools() -> None:
    """tool 無しは web_search_performed=False。"""

    evidence = ExtractOpenCodeWebToolEvidence({
        'parts': [
            {
                'type': 'tool',
                'tool': 'StructuredOutput',
                'state': {
                    'status': 'completed',
                    'input': {'outcome': 'Resolved'},
                },
            },
        ],
    })
    assert evidence['web_search_performed'] is False
    assert evidence['citations'] == []


def test_merge_web_tool_evidence_prefers_completed_over_failed() -> None:
    """failed → completed の順でも合算後は performed=True と citation を残す。"""

    from app.metadata.ai.opencode_backend import _MergeOpenCodeWebToolEvidence

    failed = ExtractOpenCodeWebToolEvidence({
        'parts': [
            {
                'type': 'tool',
                'tool': 'websearch',
                'state': {'status': 'error', 'output': 'timeout'},
            },
        ],
    })
    completed = ExtractOpenCodeWebToolEvidence({
        'parts': [
            {
                'type': 'tool',
                'tool': 'websearch',
                'state': {
                    'status': 'completed',
                    'output': 'Title: ok\nURL: https://example.com/ok\n',
                },
            },
        ],
    })
    merged = _MergeOpenCodeWebToolEvidence([failed, completed])
    assert merged['web_search_performed'] is True
    assert merged['web_search_failed'] is False
    assert merged['completed_web_calls'] == 1
    assert merged['failed_web_calls'] == 1
    assert any(
        item['url'] == 'https://example.com/ok'
        for item in merged['citations']
    )


def test_extract_web_tool_evidence_exa_text_output() -> None:
    """Exa のテキスト形式出力（URL: https://...）から public URL を抽出する。"""

    message = {
        'parts': [
            {
                'type': 'tool',
                'tool': 'websearch',
                'state': {
                    'status': 'completed',
                    'input': {'query': 'latest news about OpenAI August 2026'},
                    'output': (
                        'Title: OpenAI reportedly slows research\n'
                        'URL: https://the-decoder.com/openai-reportedly-slows-research/\n'
                        'Published: 2026-08-06T11:49:26.000Z\n'
                        'Highlights:\nSome text here\n'
                        'Another URL: http://127.0.0.1/private\n'
                    ),
                    'title': 'Exa Web Search: latest news',
                    'metadata': {'provider': 'exa', 'truncated': False},
                },
            },
        ],
    }
    evidence = ExtractOpenCodeWebToolEvidence(message)
    assert evidence['web_search_performed'] is True
    assert evidence['completed_web_calls'] == 1
    urls = [item['url'] for item in evidence['citations']]
    assert 'https://the-decoder.com/openai-reportedly-slows-research/' in urls
    # 非公開ホスト（localhost / ループバック）は抽出しない。
    assert not any(url.startswith('http://127.0.0.1') for url in urls)


def test_extract_json_object_from_text() -> None:
    """format なし応答の末尾 JSON object を抽出し、平文付きは受理しない。"""

    assert ExtractOpenCodeJSONObjectFromText(
        '{"outcome":"InsufficientEvidence","season_number":null,'
        '"episode_number":null,"confidence":0.5,"rationale_short":"test"}',
    ) == {
        'outcome': 'InsufficientEvidence',
        'season_number': None,
        'episode_number': None,
        'confidence': 0.5,
        'rationale_short': 'test',
    }
    # 短い平文 prefix は無視して JSON を抽出する。
    extracted = ExtractOpenCodeJSONObjectFromText(
        'OK\n{"outcome":"Resolved","season_number":1,"episode_number":"1",'
        '"confidence":0.9,"rationale_short":"ok"}',
    )
    assert extracted is not None
    assert extracted['outcome'] == 'Resolved'
    # Markdown や説明文が残るものは受理しない（厳格契約）。
    assert ExtractOpenCodeJSONObjectFromText(
        '```json\n{"outcome":"Resolved"}```',
    ) is None
    assert ExtractOpenCodeJSONObjectFromText(
        '{"outcome":"Resolved"}\n説明が続く',
    ) is None
    assert ExtractOpenCodeJSONObjectFromText(None) is None
    assert ExtractOpenCodeJSONObjectFromText('') is None


def test_lookup_episode_with_web_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """web tool 完了 + citation + schema で Resolved を返す。"""

    service = _service()
    backend = OpenCodeBackend(service, api_key='sk-test')

    async def FakeEnsure() -> _NoopProviderLease:
        return _NoopProviderLease()

    async def FakeRun(
        program: RecordedEpisodeLookupContext,
        *,
        timeout_sec: float = 120.0,
        prompt_variant: str = 'Default',
    ) -> tuple[EpisodeLookupResult, Any, dict[str, Any]]:
        _ = (program, timeout_sec, prompt_variant)
        from app.metadata.ai.episode_lookup import EpisodeLookupCitation

        result = EpisodeLookupResult(
            outcome='Resolved',
            season_number=1,
            episode_number=Decimal('12'),
            confidence=0.91,
            rationale_short='official source',
            citations=(EpisodeLookupCitation(
                url='https://example.com/ep12',
                title='ep12',
            ),),
            web_search_performed=True,
            model='opencode:deepseek/deepseek-chat',
            prompt_tokens=10,
            completion_tokens=5,
            http_status=200,
            latency_ms=40,
            sources=(EpisodeLookupCitation(
                url='https://example.com/ep12',
                title='ep12',
            ),),
        )
        return result, None, {
            'backend_connected': True,
            'completed_web_calls': 1,
            'session_cleaned_up': True,
            'timed_out': False,
        }

    monkeypatch.setattr(backend, 'ensureAuthInjected', FakeEnsure)
    monkeypatch.setattr(backend, '_runEpisodeLookupSession', FakeRun)
    result = asyncio.run(backend.lookupEpisode(_lookup_context()))
    assert result.outcome == 'Resolved'
    assert result.web_search_performed is True
    assert len(result.citations) == 1


def test_lookup_episode_local_attempts_web_search() -> None:
    """NoneLocal も OpenCode 経由の話数 Web 検索を実行対象にする。"""

    backend = OpenCodeBackend(
        _service(
            auth_mode='NoneLocal',
            billing_mode='Local',
            api_base_url='http://127.0.0.1:11434/v1',
        ),
        api_key=None,
    )
    result = asyncio.run(backend.lookupEpisode(_lookup_context()))
    # テスト環境では製品 OpenCode serve が unavailable だが、旧 Disabled ではなく
    # session 作成まで進んだ SearchFailed になることで経路が有効なことを確認する。
    assert result.outcome == 'SearchFailed'
    assert result.error_code == 'OpenCodeUnavailable'


def test_connection_test_episode_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """EpisodeLookup 接続試験は 4 checks Passed で success。"""

    service = _service()
    backend = OpenCodeBackend(service, api_key='sk-test')

    async def FakeEnsure() -> _NoopProviderLease:
        return _NoopProviderLease()

    async def FakeRun(
        program: RecordedEpisodeLookupContext,
        *,
        timeout_sec: float = 120.0,
        prompt_variant: str = 'Default',
    ) -> tuple[EpisodeLookupResult, Any, dict[str, Any]]:
        _ = (program, timeout_sec, prompt_variant)
        from app.metadata.ai.episode_lookup import EpisodeLookupCitation

        result = EpisodeLookupResult(
            outcome='InsufficientEvidence',
            season_number=None,
            episode_number=None,
            confidence=0.4,
            rationale_short='connection test',
            citations=(EpisodeLookupCitation(
                url='https://example.com/docs',
                title='docs',
            ),),
            web_search_performed=True,
            model='opencode:deepseek/deepseek-chat',
            prompt_tokens=11,
            completion_tokens=6,
            http_status=200,
            latency_ms=55,
            sources=(EpisodeLookupCitation(
                url='https://example.com/docs',
                title='docs',
            ),),
        )
        return result, None, {
            'backend_connected': True,
            'completed_web_calls': 1,
            'session_cleaned_up': True,
            'timed_out': False,
        }

    monkeypatch.setattr(backend, 'ensureAuthInjected', FakeEnsure)
    monkeypatch.setattr(backend, '_runEpisodeLookupSession', FakeRun)
    result = asyncio.run(backend.testConnection('EpisodeLookup'))
    assert result.success is True
    assert result.checks is not None
    assert result.checks.backend_connection.status == 'Passed'
    assert result.checks.web_search.status == 'Passed'
    assert result.checks.source_url.status == 'Passed'
    assert result.checks.strict_schema.status == 'Passed'
    assert result.checks.permission_policy.status == 'NotApplicable'


def test_validated_lookup_requires_citations() -> None:
    """citation 0 件の Resolved は InsufficientEvidence に降格する。"""

    from app.metadata.ai.opencode_backend import _ValidatedOpenCodeEpisodeLookupResult

    result = _ValidatedOpenCodeEpisodeLookupResult(
        {
            'outcome': 'Resolved',
            'season_number': 1,
            'episode_number': '3',
            'confidence': 0.95,
            'rationale_short': 'model only',
        },
        evidence={
            'web_search_performed': True,
            'web_search_failed': False,
            'citations': [],
            'completed_web_calls': 1,
            'failed_web_calls': 0,
        },
        model='opencode:deepseek/deepseek-chat',
        latency_ms=10,
        prompt_tokens=1,
        completion_tokens=1,
    )
    assert result.outcome == 'InsufficientEvidence'
    assert result.season_number is None
    assert result.web_search_performed is True


def test_validated_lookup_defaults_missing_resolved_season_to_one() -> None:
    """明示シーズンのない連続番組は、番号付き検索結果を Season 1 へ正規化する。"""

    from app.metadata.ai.opencode_backend import _ValidatedOpenCodeEpisodeLookupResult

    result = _ValidatedOpenCodeEpisodeLookupResult(
        {
            'outcome': 'Resolved',
            'season_number': None,
            'episode_number': '1250',
            'confidence': 0.95,
            'rationale_short': 'The official listing identifies episode 1250.',
        },
        evidence={
            'web_search_performed': True,
            'web_search_failed': False,
            'citations': [{
                'url': 'https://example.com/episode-1250',
                'title': 'Episode 1250',
            }],
            'completed_web_calls': 1,
            'failed_web_calls': 0,
        },
        model='opencode:google-vertex/gemini-3.6-flash',
        latency_ms=10,
        prompt_tokens=1,
        completion_tokens=1,
    )

    assert result.outcome == 'Resolved'
    assert result.season_number == 1
    assert result.episode_number == Decimal('1250')


def test_validated_lookup_without_web_search() -> None:
    """web tool 無しは SearchNotRun。"""

    from app.metadata.ai.opencode_backend import _ValidatedOpenCodeEpisodeLookupResult

    result = _ValidatedOpenCodeEpisodeLookupResult(
        {
            'outcome': 'Resolved',
            'season_number': 1,
            'episode_number': '3',
            'confidence': 0.95,
            'rationale_short': 'no tools',
        },
        evidence={
            'web_search_performed': False,
            'web_search_failed': False,
            'citations': [],
            'completed_web_calls': 0,
            'failed_web_calls': 0,
        },
        model='opencode:deepseek/deepseek-chat',
        latency_ms=10,
        prompt_tokens=1,
        completion_tokens=1,
    )
    assert result.outcome == 'SearchNotRun'
    assert result.error_code == 'OpenCodeWebSearchNotObserved'


def test_run_episode_lookup_session_evidence_from_message_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /message 応答に tool が無くても、GET 一覧から evidence を復元する。

    OpenCode 1.18.13 の POST /session/{id}/message は最終メッセージしか返さず、
    web ツールの tool part は GET /session/{id}/message の一覧にのみ含まれる。
    一方、json_schema format 付きターンを履歴へ追加した後の一覧 API は OpenCode 自身の
    レスポンス検証で HTTP 400 になるため、その前に一覧を走査して証明を抽出できること。
    """

    from app.metadata.ai.opencode_backend import (
        _BuildEpisodeLookupPrompt,
        _ValidatedOpenCodeEpisodeLookupResult,
    )

    service = _service(opencode_model_variant='high')
    backend = OpenCodeBackend(service, api_key='sk-test')
    captured_prompts: list[dict[str, Any]] = []

    final_message = {
        'info': {
            'id': 'msg_final',
            'tokens': {
                'input': 11,
                'output': 4,
                'reasoning': 0,
                'cache': {'read': 0, 'write': 0},
                'total': 15,
            },
        },
        'parts': [
            {'type': 'step-start'},
            {'type': 'text', 'text': (
                '{"outcome":"Resolved","season_number":1,"episode_number":"1",'
                '"confidence":0.9,"rationale_short":"official source"}'
            )},
            {'type': 'step-finish'},
        ],
    }
    historical_messages = [
        {
            'info': {'id': 'msg_user', 'role': 'user'},
            'parts': [{'type': 'text', 'text': _BuildEpisodeLookupPrompt(
                RecordedEpisodeLookupContext(
                    series=RecordedEpisodeContextSeries(
                        id=1,
                        title='Test Series',
                        normalized_key='testseries',
                    ),
                    program=RecordedEpisodeContextProgram(
                        title='Test Program',
                        subtitle=None,
                        description='',
                        detail_items=[],
                        broadcast_datetime='2026-08-01T00:00:00',
                        channel=None,
                        duration_seconds=0.0,
                    ),
                    local_parse=RecordedEpisodeContextLocalParse(
                        legacy_value=None,
                        season_number=None,
                        episode_number=None,
                        unresolved_reason='MissingLegacyValue',
                    ),
                    neighbors=[],
                    file=RecordedEpisodeContextFile(basename=None),
                    constraints=[],
                ),
            )}],
        },
        {
            'info': {'id': 'msg_tool'},
            'parts': [
                {'type': 'step-start'},
                {
                    'type': 'tool',
                    'tool': 'websearch',
                    'state': {
                        'status': 'completed',
                        'input': {'query': 'Test Series episode 1'},
                        'output': (
                            'Title: Test Series official page\n'
                            'URL: https://example.com/testseries\n'
                        ),
                        'metadata': {'provider': 'exa'},
                    },
                },
                {'type': 'step-finish'},
            ],
        },
    ]

    async def FakeCreateSession() -> str:
        return 'ses_test'

    async def FakePromptJsonSchema(_session_id: str, **kwargs: Any) -> dict[str, Any]:
        captured_prompts.append(kwargs)
        if kwargs['include_format']:
            return {
                **final_message,
                'parts': [
                    {
                        'type': 'tool',
                        'tool': 'StructuredOutput',
                        'state': {
                            'status': 'completed',
                            'input': {
                                'outcome': 'Resolved',
                                'season_number': 1,
                                'episode_number': '1',
                                'confidence': 0.9,
                                'rationale_short': 'official source',
                            },
                        },
                    },
                ],
            }
        return final_message

    async def FakeListMessages(_session_id: str) -> list[dict[str, Any]]:
        # 検索ターンだけが完了し、json_schema format 付き最終ターンはまだ送られていない。
        assert len(captured_prompts) == 1
        assert captured_prompts[0]['include_format'] is False
        return historical_messages

    async def FakeDeleteSession(_session_id: str) -> None:
        return None

    monkeypatch.setattr(backend._client, 'createSession', FakeCreateSession)
    monkeypatch.setattr(backend._client, 'promptJsonSchema', FakePromptJsonSchema)
    monkeypatch.setattr(backend._client, 'listMessages', FakeListMessages)
    monkeypatch.setattr(backend._client, 'deleteSession', FakeDeleteSession)

    result, _usage, trace = asyncio.run(backend._runEpisodeLookupSession(
        RecordedEpisodeLookupContext(
            series=RecordedEpisodeContextSeries(
                id=1,
                title='Test Series',
                normalized_key='testseries',
            ),
            program=RecordedEpisodeContextProgram(
                title='Test Program',
                subtitle=None,
                description='',
                detail_items=[],
                broadcast_datetime='2026-08-01T00:00:00',
                channel=None,
                duration_seconds=0.0,
            ),
            local_parse=RecordedEpisodeContextLocalParse(
                legacy_value=None,
                season_number=None,
                episode_number=None,
                unresolved_reason='MissingLegacyValue',
            ),
            neighbors=[],
            file=RecordedEpisodeContextFile(basename=None),
            constraints=[],
        ),
    ))
    assert trace['completed_web_calls'] == 1
    assert len(captured_prompts) == 2
    assert captured_prompts[0]['include_format'] is False
    assert captured_prompts[0]['tools'] == {'websearch': True, 'webfetch': False}
    assert captured_prompts[1]['include_format'] is True
    assert captured_prompts[1]['tools'] == {'websearch': False, 'webfetch': False}
    assert all(item['variant'] == 'high' for item in captured_prompts)
    assert result.web_search_performed is True
    assert result.outcome == 'Resolved'
    assert result.season_number == 1
    assert result.episode_number == Decimal('1')
    assert any(
        citation.url == 'https://example.com/testseries'
        for citation in result.citations
    )
    validated = _ValidatedOpenCodeEpisodeLookupResult(
        {'outcome': 'Resolved', 'season_number': 1, 'episode_number': '1',
         'confidence': 0.9, 'rationale_short': 'official source'},
        evidence={
            'web_search_performed': True,
            'web_search_failed': False,
            'citations': [{'url': 'https://example.com/testseries',
                           'title': 'Test Series official page'}],
            'completed_web_calls': 1,
            'failed_web_calls': 0,
        },
        model='opencode:deepseek/deepseek-chat',
        latency_ms=40,
        prompt_tokens=11,
        completion_tokens=4,
    )
    assert validated.outcome == 'Resolved'


def test_auto_structured_output_falls_back_to_json_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto は schema 不正な StructuredOutput を同一 session の JSON text で補正する。"""

    from app.metadata.ai.opencode_backend import _OpenCodeEpisodeLookupOutput

    service = _service(structured_output_mode='Auto')
    backend = OpenCodeBackend(service, api_key='sk-test')
    calls: list[bool] = []

    async def FakePromptJsonSchema(_session_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(bool(kwargs['include_format']))
        if kwargs['include_format']:
            return {
                'parts': [{
                    'type': 'tool',
                    'tool': 'StructuredOutput',
                    'state': {
                        'status': 'completed',
                        # Synthetic / GLM で実測した nullable の文字列化を再現する。
                        'input': {
                            'outcome': 'InsufficientEvidence',
                            'season_number': 'null',
                            'episode_number': 'None',
                            'confidence': 0.4,
                            'rationale_short': 'insufficient',
                        },
                    },
                }],
            }
        return {
            'parts': [{
                'type': 'text',
                'text': (
                    '{"outcome":"InsufficientEvidence","season_number":null,'
                    '"episode_number":null,"confidence":0.4,'
                    '"rationale_short":"insufficient"}'
                ),
            }],
        }

    monkeypatch.setattr(backend._client, 'promptJsonSchema', FakePromptJsonSchema)
    structured, _usage = asyncio.run(backend._promptJSONInSession(
        'ses-test',
        prompt_text='Return JSON.',
        schema_model=_OpenCodeEpisodeLookupOutput,
        agent='recorded-series-episode',
        timeout_sec=120.0,
        tools={'websearch': False, 'webfetch': False},
    ))
    assert calls == [True, False]
    assert structured is not None
    assert structured['season_number'] is None
    assert structured['episode_number'] is None


@pytest.mark.parametrize(
    ('mode', 'expected_include_format'),
    [
        ('StructuredOutput', True),
        ('JSONText', False),
    ],
)
def test_explicit_structured_output_mode_does_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_include_format: bool,
) -> None:
    """明示方式は選択した OpenCode format の1回だけを実行する。"""

    from app.metadata.ai.opencode_backend import _OpenCodeEpisodeLookupOutput

    backend = OpenCodeBackend(
        _service(structured_output_mode=mode),
        api_key='sk-test',
    )
    calls: list[bool] = []

    async def FakePromptJsonSchema(_session_id: str, **kwargs: Any) -> dict[str, Any]:
        include_format = bool(kwargs['include_format'])
        calls.append(include_format)
        output = {
            'outcome': 'InsufficientEvidence',
            'season_number': None,
            'episode_number': None,
            'confidence': 0.4,
            'rationale_short': 'insufficient',
        }
        if include_format:
            return {
                'parts': [{
                    'type': 'tool',
                    'tool': 'StructuredOutput',
                    'state': {'status': 'completed', 'input': output},
                }],
            }
        return {'parts': [{'type': 'text', 'text': json.dumps(output)}]}

    monkeypatch.setattr(backend._client, 'promptJsonSchema', FakePromptJsonSchema)
    structured, _usage = asyncio.run(backend._promptJSONInSession(
        'ses-test',
        prompt_text='Return JSON.',
        schema_model=_OpenCodeEpisodeLookupOutput,
        agent='recorded-series-episode',
        timeout_sec=120.0,
    ))
    assert calls == [expected_include_format]
    assert structured is not None
    assert structured['outcome'] == 'InsufficientEvidence'
