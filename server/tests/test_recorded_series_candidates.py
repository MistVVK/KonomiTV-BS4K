import asyncio
from typing import Any

import httpx
import pytest

from app.metadata.RecordedSeriesCandidates import (
    BuildChatCompletionsURL,
    RecordedSeriesAIError,
    RecordedSeriesProgramPrompt,
    SelectRecordedSeriesCandidate,
    SeriesChoiceCandidate,
)


def test_build_chat_completions_url_accepts_service_root_or_full_endpoint() -> None:
    assert BuildChatCompletionsURL('https://api.example/v1') == 'https://api.example/v1/chat/completions'
    assert BuildChatCompletionsURL('http://127.0.0.1:1234/v1/') == 'http://127.0.0.1:1234/v1/chat/completions'
    assert BuildChatCompletionsURL('https://api.example/v1/chat/completions') == \
        'https://api.example/v1/chat/completions'


def _program() -> RecordedSeriesProgramPrompt:
    return RecordedSeriesProgramPrompt(
        title='候補選択テスト',
        description='テスト用メタデータ',
        genres=['ドラマ'],
        channel='テスト局',
        start_date='2026-07-21',
    )


def _candidates() -> list[SeriesChoiceCandidate]:
    return [
        SeriesChoiceCandidate(
            choice_id='wiki:123',
            kind='Wikipedia',
            title='候補選択テスト',
            description='Wikipedia候補',
        ),
        SeriesChoiceCandidate(
            choice_id='unresolved',
            kind='Unresolved',
            title='未確定',
            description='根拠不足の場合',
        ),
    ]


def _install_transport(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    async def Handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get('authorization') == 'Bearer secret-value'
        return httpx.Response(200, json=payload, request=request)

    original_client = httpx.AsyncClient

    def Client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs['transport'] = httpx.MockTransport(Handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', Client)


def test_ai_accepts_only_an_input_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(monkeypatch, {
        'model': 'test-model',
        'choices': [{'message': {'content': '{"choice_id":"wiki:123","confidence":0.95}'}}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 10},
    })
    result = asyncio.run(SelectRecordedSeriesCandidate(
        api_base_url='https://api.example/v1',
        api_key='secret-value',
        model='test-model',
        program=_program(),
        candidates=_candidates(),
    ))
    assert result.choice_id == 'wiki:123'
    assert result.prompt_tokens == 100


@pytest.mark.parametrize(
    ('content', 'error_code'),
    [
        ('{"choice_id":"wiki:999","confidence":0.99}', 'ChoiceOutsideCandidateSet'),
        ('{"choice_id":"series:999","confidence":0.99}', 'ChoiceOutsideCandidateSet'),
        ('{"choice_id":"wiki:123","confidence":0.50}', 'LowConfidence'),
        ('{"choice_id":"wiki:123","confidence":0.99,"title":"invented"}', 'InvalidOutputSchema'),
        ('not-json', 'InvalidJSON'),
    ],
)
def test_ai_rejects_unsafe_or_invalid_output(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    error_code: str,
) -> None:
    _install_transport(monkeypatch, {
        'model': 'test-model',
        'choices': [{'message': {'content': content}}],
    })
    with pytest.raises(RecordedSeriesAIError) as error:
        asyncio.run(SelectRecordedSeriesCandidate(
            api_base_url='https://api.example/v1',
            api_key='secret-value',
            model='test-model',
            program=_program(),
            candidates=_candidates(),
        ))
    assert error.value.code == error_code
