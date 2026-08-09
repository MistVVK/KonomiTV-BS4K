from dataclasses import replace
from decimal import Decimal

from app.metadata.ai.episode_lookup import EpisodeLookupCitation
from app.metadata.RecordedEpisodeSearch import (
    AIEpisodeLookupResult,
    GetEpisodeLookupEvidence,
    IsEpisodeLookupResultAccepted,
)


def _result(
    *,
    outcome: str = 'Resolved',
    confidence: float = 0.94,
    citations: tuple[EpisodeLookupCitation, ...] = (
        EpisodeLookupCitation(url='https://example.com/official/episode-3', title='番組公式 第3話'),
    ),
    sources: tuple[EpisodeLookupCitation, ...] = (
        EpisodeLookupCitation(url='https://example.com/official/episode-3', title='検索ソース題名'),
    ),
) -> AIEpisodeLookupResult:
    return AIEpisodeLookupResult(
        outcome=outcome,  # type: ignore[arg-type]
        season_number=2 if outcome == 'Resolved' else None,
        episode_number=Decimal('3.5') if outcome == 'Resolved' else None,
        confidence=confidence,
        rationale_short='公式あらすじと放送日時が一致',
        citations=citations,
        sources=sources,
        model='test-model',
        prompt_tokens=120,
        completion_tokens=24,
        http_status=200,
        latency_ms=10,
    )


def test_get_episode_lookup_evidence_deduplicates_citation_and_source_urls() -> None:
    result = _result(
        citations=(
            EpisodeLookupCitation(url='https://example.com/a', title='本文引用'),
        ),
        sources=(
            EpisodeLookupCitation(url='https://example.com/a', title='検索ソース'),
            EpisodeLookupCitation(url='https://example.com/b', title='別ソース'),
        ),
    )
    evidence = GetEpisodeLookupEvidence(result)
    assert [(item.url, item.title) for item in evidence] == [
        ('https://example.com/a', '本文引用'),
        ('https://example.com/b', '別ソース'),
    ]


def test_episode_lookup_acceptance_requires_verified_evidence() -> None:
    result = _result()
    assert IsEpisodeLookupResultAccepted(result) is True
    assert IsEpisodeLookupResultAccepted(
        replace(result, citations=()),
    ) is True
    assert IsEpisodeLookupResultAccepted(
        replace(result, citations=(), sources=()),
    ) is False
    assert IsEpisodeLookupResultAccepted(
        replace(result, confidence=0.25, citations=()),
    ) is True


def test_not_numbered_acceptance_is_confidence_independent_but_requires_evidence() -> None:
    result = _result(outcome='NotNumbered', confidence=0.91)
    assert IsEpisodeLookupResultAccepted(result) is True
    assert IsEpisodeLookupResultAccepted(
        replace(result, confidence=0.25, citations=()),
    ) is True
    assert IsEpisodeLookupResultAccepted(
        replace(result, citations=(), sources=()),
    ) is False
