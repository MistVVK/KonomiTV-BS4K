from __future__ import annotations

from decimal import Decimal

from app.metadata.ai.episode_lookup import (
    EpisodeLookupCitation,
    EpisodeLookupResult,
    ModelEpisodeLookupOutcome,
)
from app.metadata.RecordedEpisodeContext import (
    RecordedEpisodeLookupContext,
)


RecordedEpisodeProgramPrompt = RecordedEpisodeLookupContext


AIEpisodeCitation = EpisodeLookupCitation


class AIEpisodeLookupResult(EpisodeLookupResult):
    """旧テスト fixture も受け付ける共通 EpisodeLookupResult の互換名。"""

    __slots__ = ()

    def __init__(
        self,
        *,
        season_number: int | None,
        episode_number: Decimal | None,
        confidence: float | None,
        citations: tuple[AIEpisodeCitation, ...],
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        http_status: int | None,
        latency_ms: int,
        outcome: ModelEpisodeLookupOutcome | None = None,
        rationale_short: str | None = None,
        numbered: bool | None = None,
        sources: tuple[AIEpisodeCitation, ...] = (),
        web_search_performed: bool = True,
        error_code: str | None = None,
        error_message: str | None = None,
        recovery_attempt_summaries: tuple[str, ...] = (),
    ) -> None:
        """新 outcome または旧 numbered から共通結果を初期化する。"""

        effective_outcome: ModelEpisodeLookupOutcome
        if outcome is not None:
            effective_outcome = outcome
        elif numbered is True:
            effective_outcome = 'Resolved'
        else:
            effective_outcome = 'NotNumbered'
        super().__init__(
            outcome=effective_outcome,
            season_number=season_number,
            episode_number=episode_number,
            confidence=confidence,
            rationale_short=rationale_short or 'Web 検索結果に基づく話数判定です。',
            citations=citations,
            web_search_performed=web_search_performed,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            http_status=http_status,
            latency_ms=latency_ms,
            error_code=error_code,
            error_message=error_message,
            sources=sources,
            recovery_attempt_summaries=recovery_attempt_summaries,
        )


def GetEpisodeLookupEvidence(
    result: EpisodeLookupResult,
) -> tuple[AIEpisodeCitation, ...]:
    """本文引用とWeb検索元を、URL重複を除いた保存可能な根拠へまとめる。

    厳格なJSON Schema出力では本文がJSONだけになるため、Responses APIが
    `web_search_call.action.sources` を返しても `url_citation` annotationが
    付かないことがある。本文引用を優先しつつ、検索元も正式な根拠として扱う。

    Args:
        result: Web検索済みの話数判定結果。

    Returns:
        URLごとに重複排除した引用・検索元。
    """

    evidence_by_url: dict[str, AIEpisodeCitation] = {}
    for evidence in (*result.citations, *result.sources):
        evidence_by_url.setdefault(evidence.url, evidence)
    return tuple(evidence_by_url.values())


def IsEpisodeLookupResultAccepted(
    result: EpisodeLookupResult,
) -> bool:
    """Web検索と公開URLの根拠を検証できた有効な結果だけを自動反映する。

    confidence はモデルの自己評価なので監査表示だけに使用し、受理判定には使わない。

    Args:
        result: Web検索実行と出力スキーマを検証済みの結果。

    Returns:
        自動反映条件を満たす場合はTrue。
    """

    has_verified_evidence = len(GetEpisodeLookupEvidence(result)) > 0
    return (
        result.outcome in {'Resolved', 'NotNumbered', 'NoPublishedNumber'}
        and result.web_search_performed
        and has_verified_evidence
    )
