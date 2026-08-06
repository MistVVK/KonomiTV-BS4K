"""OpenCodeBackend / client 抽出ロジックのユニットテスト（Phase 2）。"""

from __future__ import annotations

import asyncio
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
    SeriesChoiceCandidate,
)
from app.metadata.RecordedSeriesGeneration import (
    AISeriesMetadataResult,
    SeriesMetadataClusterHint,
    SeriesMetadataExistingSeriesHint,
    SeriesMetadataHints,
    SeriesMetadataLocalParseHint,
    SeriesMetadataWikipediaHint,
)


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

    monkeypatch.setattr(AIAPIUsageLedger, 'Reserve', classmethod(FakeReserve))
    monkeypatch.setattr(AIAPIUsageLedger, 'Settle', classmethod(FakeSettle))


def _program() -> RecordedSeriesProgramPrompt:
    return RecordedSeriesProgramPrompt(
        title='Test',
        description='Desc',
        genres=['Anime'],
        channel='NHKBSP',
        start_date='2024-01-01',
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
        ),
        existing_series=[
            SeriesMetadataExistingSeriesHint(
                id=1,
                title='Connection Test Series',
                description='',
                wikipedia_page_id=None,
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


def test_select_candidate_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """候補選択が structured 結果を検証して返す。"""

    service = _service()
    backend = OpenCodeBackend(service, api_key='sk-test')

    async def FakeEnsure() -> None:
        return None

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

    async def FakeEnsure() -> None:
        return None

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

    async def FakeEnsure() -> None:
        return None

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

    async def FakeEnsure() -> None:
        return None

    async def FakeRun(
        program: RecordedEpisodeLookupContext,
        *,
        timeout_sec: float = 120.0,
    ) -> tuple[EpisodeLookupResult, Any, dict[str, Any]]:
        _ = (program, timeout_sec)
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


def test_lookup_episode_local_disabled() -> None:
    """NoneLocal は既定で EpisodeLookup 非対応。"""

    backend = OpenCodeBackend(
        _service(
            auth_mode='NoneLocal',
            billing_mode='Local',
            api_base_url='http://127.0.0.1:11434/v1',
        ),
        api_key=None,
    )
    result = asyncio.run(backend.lookupEpisode(_lookup_context()))
    assert result.outcome == 'Disabled'
    assert result.error_code == 'OpenCodeEpisodeLookupLocalDisabled'


def test_connection_test_episode_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """EpisodeLookup 接続試験は 4 checks Passed で success。"""

    service = _service()
    backend = OpenCodeBackend(service, api_key='sk-test')

    async def FakeEnsure() -> None:
        return None

    async def FakeRun(
        program: RecordedEpisodeLookupContext,
        *,
        timeout_sec: float = 120.0,
    ) -> tuple[EpisodeLookupResult, Any, dict[str, Any]]:
        _ = (program, timeout_sec)
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
    実機で判明したこの挙動に合わせ、一覧を走査して証明を抽出できること。
    """

    from app.metadata.ai.opencode_backend import (
        _BuildEpisodeLookupPrompt,
        _ValidatedOpenCodeEpisodeLookupResult,
    )

    service = _service()
    backend = OpenCodeBackend(service, api_key='sk-test')

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

    async def FakePromptJsonSchema(_session_id: str, **_kwargs: Any) -> dict[str, Any]:
        return final_message

    async def FakeListMessages(_session_id: str) -> list[dict[str, Any]]:
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
