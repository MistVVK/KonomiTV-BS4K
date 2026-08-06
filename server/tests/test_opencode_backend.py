"""OpenCodeBackend / client 抽出ロジックのユニットテスト（Phase 2）。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest

from app.metadata.ai.AIBackendSettings import AIBackendService
from app.metadata.ai.backends import UnsupportedOperationError
from app.metadata.ai.opencode_backend import OpenCodeBackend
from app.metadata.ai.opencode_client import (
    ExtractOpenCodeStructuredOutput,
    ExtractOpenCodeUsage,
)
from app.metadata.RecordedEpisodeContext import RecordedEpisodeLookupContext
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


def test_lookup_episode_unsupported() -> None:
    """EpisodeLookup は Phase 4 まで Unsupported。"""

    backend = OpenCodeBackend(_service(), api_key='sk-test')
    with pytest.raises(UnsupportedOperationError):
        asyncio.run(backend.lookupEpisode(cast(RecordedEpisodeLookupContext, {})))
