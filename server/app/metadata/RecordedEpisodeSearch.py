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
from app.metadata.RecordedSeriesSettings import RecordedEpisodeNumberAcceptanceMode


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
    acceptance_mode: RecordedEpisodeNumberAcceptanceMode,
    minimum_confidence: float = 0.80,
) -> bool:
    """設定された受理モードで、Web検索結果をEpisodeへ自動反映できるか判定する。

    Args:
        result: Web検索実行と出力スキーマを検証済みの結果。
        acceptance_mode: 数値があれば常に受理するか、高信頼結果だけに限るか。
        minimum_confidence: HighConfidenceOnlyで必要な最低信頼度。

    Returns:
        自動反映条件を満たす場合はTrue。
    """

    has_verified_evidence = len(GetEpisodeLookupEvidence(result)) > 0
    has_high_confidence_evidence = (
        result.confidence is not None and
        result.confidence >= minimum_confidence and
        has_verified_evidence
    )
    if result.outcome not in {'Resolved', 'NotNumbered'} or has_verified_evidence is False:
        return False
    if result.outcome == 'NotNumbered':
        # `Always` は有効な数値結果の受理を緩和する設定であり、
        # 話数なし判定は破壊的なキャッシュになるため引き続き高信頼の根拠を必須とする。
        return has_high_confidence_evidence
    if acceptance_mode == 'Always':
        return True
    return has_high_confidence_evidence
