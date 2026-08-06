"""OpenAI 互換バックエンド。

既存の SelectRecordedSeriesCandidate / SearchRecordedEpisodeNumber を
バックエンドプロトコルに適合させるラッパー。
"""

from __future__ import annotations

from app.metadata.ai.backends import (
    ConnectionTestCheck,
    ConnectionTestResult,
    EpisodeLookupConnectionChecks,
)
from app.metadata.ai.episode_lookup import EpisodeLookupResult
from app.metadata.RecordedEpisodeSearch import (
    GetEpisodeLookupEvidence,
    RecordedEpisodeProgramPrompt,
)
from app.metadata.RecordedSeriesCandidates import (
    AIChoiceResult,
    RecordedSeriesAIError,
    RecordedSeriesProgramPrompt,
    SeriesChoiceCandidate,
)
from app.metadata.RecordedSeriesGeneration import (
    AISeriesMetadataResult,
    SeriesMetadataHints,
)
from app.metadata.RecordedSeriesSettings import (
    RecordedSeriesSettings,
)


def _buildEpisodeLookupConnectionChecks(
    result: EpisodeLookupResult,
) -> EpisodeLookupConnectionChecks:
    """共通 lookup result から、実際に確認できた能力だけを個別判定する。"""

    backend_connected = (
        result.outcome in {
            'Resolved',
            'NotNumbered',
            'InsufficientEvidence',
            'SearchNotRun',
            'InvalidModelOutput',
        } or
        result.http_status is not None
    )
    backend_connection = ConnectionTestCheck(
        status='Passed' if backend_connected else 'Failed',
        message=(
            'バックエンドから応答を受信しました。'
            if backend_connected
            else result.error_message or 'バックエンドへ接続できませんでした。'
        ),
    )

    if result.web_search_performed:
        web_search = ConnectionTestCheck(
            status='Passed',
            message='Web 検索の実行記録を確認しました。',
        )
    elif result.outcome == 'SearchNotRun':
        web_search = ConnectionTestCheck(
            status='Failed',
            message=result.error_message or 'Web 検索の実行記録を確認できませんでした。',
        )
    else:
        web_search = ConnectionTestCheck(
            status='NotRun',
            message='Web 検索の実行確認まで到達しませんでした。',
        )

    evidence = GetEpisodeLookupEvidence(result)
    if len(evidence) > 0:
        source_url = ConnectionTestCheck(
            status='Passed',
            message='検索実行記録から公開 HTTP(S) URL を取得しました。',
        )
    elif result.outcome == 'InvalidModelOutput':
        source_url = ConnectionTestCheck(
            status='NotRun',
            message='モデル出力が不正なため、検索元 URL の個別判定結果を保持できませんでした。',
        )
    elif result.web_search_performed:
        source_url = ConnectionTestCheck(
            status='Failed',
            message='検索実行記録から検索元 URL を取得できませんでした。',
        )
    else:
        source_url = ConnectionTestCheck(
            status='NotRun',
            message='Web 検索が未確認のため、検索元 URL は判定していません。',
        )

    if result.outcome in {'Resolved', 'NotNumbered', 'InsufficientEvidence'}:
        strict_schema = ConnectionTestCheck(
            status='Passed',
            message='strict schema に適合するモデル出力を確認しました。',
        )
    elif result.outcome == 'InvalidModelOutput':
        strict_schema = ConnectionTestCheck(
            status='Failed',
            message=result.error_message or 'モデル出力が strict schema に適合しませんでした。',
        )
    else:
        strict_schema = ConnectionTestCheck(
            status='NotRun',
            message='モデル出力の strict schema 検証まで到達しませんでした。',
        )

    timeout_cancel = ConnectionTestCheck(
        status='Passed' if result.error_code == 'Timeout' else 'NotRun',
        message=(
            'タイムアウトを検出し、要求を安全に終了しました。'
            if result.error_code == 'Timeout'
            else '通常応答の試験ではタイムアウト回収を意図的に発生させていません。'
        ),
    )
    permission_policy = ConnectionTestCheck(
        status='NotApplicable',
        message='OpenAI 互換 Responses API では ACP permission policy は対象外です。',
    )
    return EpisodeLookupConnectionChecks(
        backend_connection=backend_connection,
        web_search=web_search,
        source_url=source_url,
        strict_schema=strict_schema,
        timeout_cancel=timeout_cancel,
        permission_policy=permission_policy,
    )


def _episodeLookupConnectionSucceeded(
    checks: EpisodeLookupConnectionChecks,
) -> bool:
    """EpisodeLookup の実行能力4項目がすべて確認済みかを返す。"""

    return all(
        check.status == 'Passed'
        for check in (
            checks.backend_connection,
            checks.web_search,
            checks.source_url,
            checks.strict_schema,
        )
    )


class OpenAICompatibleBackend:
    """OpenAI 互換 API バックエンド。

    判定開始時に渡された settings / api_key snapshot を優先し、
    未指定時だけ設定ストアを再読する（接続試験など）。
    """

    def __init__(
        self,
        *,
        settings: RecordedSeriesSettings | None = None,
        api_key: str | None = None,
    ) -> None:
        self._settings_snapshot = settings
        self._api_key_snapshot = api_key
        self._has_snapshot = settings is not None

    @property
    def backend_kind(self) -> str:
        return 'OpenAICompatible'

    def _resolve_settings_and_key(self) -> tuple[RecordedSeriesSettings, str | None]:
        """廃止済み。常に例外。"""

        self._raiseRemoved()
        raise AssertionError('unreachable')

    def _raiseRemoved(self) -> None:
        """OpenAICompatible 経路は OpenCode へクリーンブレーク済み。"""

        raise RecordedSeriesAIError('OpenAICompatibleBackendRemoved')

    async def selectCandidate(
        self,
        program: RecordedSeriesProgramPrompt,
        candidates: list[SeriesChoiceCandidate],
        *,
        minimum_confidence: float = 0.80,
    ) -> AIChoiceResult:
        """OpenAI 互換 Chat Completions で候補選択を実行する（廃止）。"""

        _ = (program, candidates, minimum_confidence)
        self._raiseRemoved()
        raise AssertionError('unreachable')

    async def resolveSeriesMetadata(
        self,
        program: RecordedSeriesProgramPrompt,
        hints: SeriesMetadataHints,
    ) -> AISeriesMetadataResult:
        """OpenAI 互換 Chat Completions でシリーズ情報を一括生成する（廃止）。"""

        _ = (program, hints)
        self._raiseRemoved()
        raise AssertionError('unreachable')

    async def lookupEpisode(
        self,
        program: RecordedEpisodeProgramPrompt,
    ) -> EpisodeLookupResult:
        """OpenAI Responses API + web_search（廃止）。"""

        _ = program
        self._raiseRemoved()
        raise AssertionError('unreachable')

    async def testConnection(
        self,
        capability: str,
    ) -> ConnectionTestResult:
        """OpenAI 互換 API の接続試験（廃止）。"""

        _ = capability
        self._raiseRemoved()
        raise AssertionError('unreachable')
