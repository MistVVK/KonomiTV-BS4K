"""録画シリーズ AI バックエンドの抽象プロトコルと共通型。

AI バックエンド（OpenAI 互換 / ACP）を統一的に扱うための
Protocol 定義と、facade が使用する共通データ型を提供する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from app.metadata.ai.episode_lookup import EpisodeLookupResult
from app.metadata.RecordedEpisodeContext import RecordedEpisodeLookupContext
from app.metadata.RecordedSeriesCandidates import (
    AIChoiceResult,
    RecordedSeriesProgramPrompt,
    SeriesChoiceCandidate,
)
from app.metadata.RecordedSeriesGeneration import (
    AISeriesMetadataResult,
    SeriesMetadataHints,
)


class UnsupportedOperationError(Exception):
    """バックエンドが要求された操作をサポートしない場合に送出する。"""

    def __init__(self, operation: str, backend: str) -> None:
        super().__init__(f'{operation} is not supported by {backend}')
        self.operation = operation
        self.backend = backend


ConnectionTestCheckStatus = Literal['Passed', 'Failed', 'NotRun', 'NotApplicable']


@dataclass(frozen=True, slots=True)
class ConnectionTestCheck:
    """接続試験の1能力について、実測範囲を誤魔化さず返す。"""

    status: ConnectionTestCheckStatus
    message: str


@dataclass(frozen=True, slots=True)
class EpisodeLookupConnectionChecks:
    """EpisodeLookup 接続試験で個別表示する固定6項目。"""

    backend_connection: ConnectionTestCheck
    web_search: ConnectionTestCheck
    source_url: ConnectionTestCheck
    strict_schema: ConnectionTestCheck
    timeout_cancel: ConnectionTestCheck
    permission_policy: ConnectionTestCheck


@dataclass(frozen=True, slots=True)
class ConnectionTestResult:
    """バックエンド共通の接続試験結果。"""

    success: bool
    latency_ms: int
    model: str
    message: str
    checks: EpisodeLookupConnectionChecks | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    http_status: int | None = None
    error_code: str | None = None
    selected_choice_id: str | None = None
    # API へは返さず、接続試験が実際に使用した認証世代と proof を結ぶ内部値。
    provider_fingerprint: str | None = None


@runtime_checkable
class RecordedSeriesAIBackend(Protocol):
    """録画シリーズ AI バックエンドのプロトコル。

    各バックエンド実装はこのプロトコルを満たす必要がある。
    """

    @property
    def backend_kind(self) -> str:
        """バックエンド種別を返す（OpenAICompatible / AcpCodex など）。"""
        ...

    async def selectCandidate(
        self,
        program: RecordedSeriesProgramPrompt,
        candidates: list[SeriesChoiceCandidate],
        *,
        minimum_confidence: float = 0.80,
    ) -> AIChoiceResult:
        """候補選択を実行する。"""
        ...

    async def resolveSeriesMetadata(
        self,
        program: RecordedSeriesProgramPrompt,
        hints: SeriesMetadataHints,
    ) -> AISeriesMetadataResult:
        """シリーズ名・話数・話名を一括生成する。"""
        ...

    async def lookupEpisode(
        self,
        program: RecordedEpisodeLookupContext,
    ) -> EpisodeLookupResult:
        """話数 Web 検索を実行する。

        Raises:
            UnsupportedOperationError: バックエンドが話数検索をサポートしない場合。
        """
        ...

    async def testConnection(
        self,
        capability: str,  # 'CandidateSelection' | 'EpisodeLookup'
    ) -> ConnectionTestResult:
        """接続試験を実行する。"""
        ...
