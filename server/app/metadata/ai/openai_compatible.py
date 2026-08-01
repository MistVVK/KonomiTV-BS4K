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
from app.metadata.ai.episode_lookup import EpisodeLookupOutcome, EpisodeLookupResult
from app.metadata.RecordedEpisodeContext import (
    RECORDED_EPISODE_CONTEXT_VERSION,
    RecordedEpisodeContextFile,
    RecordedEpisodeContextLocalParse,
    RecordedEpisodeContextProgram,
    RecordedEpisodeContextSeries,
    RecordedEpisodeLookupContext,
)
from app.metadata.RecordedEpisodeMessages import GetRecordedEpisodeErrorMessage
from app.metadata.RecordedEpisodeSearch import (
    GetEpisodeLookupEvidence,
    RecordedEpisodeProgramPrompt,
    SearchRecordedEpisodeNumber,
)
from app.metadata.RecordedSeriesCandidates import (
    AIChoiceResult,
    RecordedSeriesAIError,
    RecordedSeriesProgramPrompt,
    SelectRecordedSeriesCandidate,
    SeriesChoiceCandidate,
)
from app.metadata.RecordedSeriesGeneration import (
    AISeriesMetadataResult,
    ResolveRecordedSeriesMetadata,
    SeriesMetadataClusterHint,
    SeriesMetadataExistingSeriesHint,
    SeriesMetadataHints,
    SeriesMetadataLocalParseHint,
    SeriesMetadataWikipediaHint,
)
from app.metadata.RecordedSeriesSettings import (
    RecordedSeriesSettings,
    RecordedSeriesSettingsStore,
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
        """immutable snapshot があればそれを返し、無ければ同一 lock 世代の組を取得する。"""

        if self._has_snapshot:
            assert self._settings_snapshot is not None
            return self._settings_snapshot, self._api_key_snapshot
        return RecordedSeriesSettingsStore.getSettingsAndAPIKey()

    async def selectCandidate(
        self,
        program: RecordedSeriesProgramPrompt,
        candidates: list[SeriesChoiceCandidate],
        *,
        minimum_confidence: float = 0.80,
    ) -> AIChoiceResult:
        """OpenAI 互換 Chat Completions で候補選択を実行する。"""
        settings, api_key = self._resolve_settings_and_key()
        return await SelectRecordedSeriesCandidate(
            api_base_url=settings.api_base_url,
            api_key=api_key,
            model=settings.model,
            program=program,
            candidates=candidates,
            minimum_confidence=minimum_confidence,
        )

    async def resolveSeriesMetadata(
        self,
        program: RecordedSeriesProgramPrompt,
        hints: SeriesMetadataHints,
    ) -> AISeriesMetadataResult:
        """OpenAI 互換 Chat Completions でシリーズ情報を一括生成する。"""

        settings, api_key = self._resolve_settings_and_key()
        return await ResolveRecordedSeriesMetadata(
            api_base_url=settings.api_base_url,
            api_key=api_key,
            model=settings.model,
            program=program,
            hints=hints,
        )

    async def lookupEpisode(
        self,
        program: RecordedEpisodeProgramPrompt,
    ) -> EpisodeLookupResult:
        """OpenAI Responses API + web_search の全終端を共通 outcome へ正規化する。"""

        settings, api_key = self._resolve_settings_and_key()
        try:
            return await SearchRecordedEpisodeNumber(
                api_base_url=settings.api_base_url,
                api_key=api_key,
                model=settings.model,
                program=program,
            )
        except RecordedSeriesAIError as ex:
            outcome: EpisodeLookupOutcome
            if ex.code == 'MissingWebSearchCall':
                outcome = 'SearchNotRun'
                web_search_performed = False
            elif ex.code in {
                'InvalidJSON',
                'InvalidJSONType',
                'InvalidOutputSchema',
                'MissingOutputText',
                'MultipleOutputTexts',
            }:
                outcome = 'InvalidModelOutput'
                web_search_performed = True
            elif ex.code == 'HTTP429':
                outcome = 'RateLimited'
                web_search_performed = False
            else:
                outcome = 'SearchFailed'
                web_search_performed = False
            return EpisodeLookupResult(
                outcome=outcome,
                season_number=None,
                episode_number=None,
                confidence=None,
                rationale_short=None,
                citations=(),
                web_search_performed=web_search_performed,
                model=settings.model,
                prompt_tokens=None,
                completion_tokens=None,
                http_status=ex.http_status,
                latency_ms=ex.latency_ms or 0,
                error_code=ex.code,
                error_message=GetRecordedEpisodeErrorMessage(ex.code) or '話数 Web 検索に失敗しました。',
            )

    async def testConnection(
        self,
        capability: str,
    ) -> ConnectionTestResult:
        """OpenAI 互換 API の接続試験を実行する。

        snapshot が無い場合のみ設定ストアから API キーを解決する。
        """
        from datetime import date

        settings, _api_key = self._resolve_settings_and_key()

        try:
            if capability == 'CandidateSelection':
                program = RecordedSeriesProgramPrompt(
                    title='KonomiTV-BS4K OpenAI-compatible API connection test',
                    description='Synthetic input for the series metadata generation contract.',
                    genres=['ConnectionTest'],
                    channel=None,
                    start_date=date.today().isoformat(),
                )
                hints = SeriesMetadataHints(
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
                            description='Synthetic existing Series hint.',
                            wikipedia_page_id=None,
                        ),
                    ],
                    wikipedia=[
                        SeriesMetadataWikipediaHint(
                            page_id=1,
                            title='Connection Test Series',
                            extract='Synthetic Wikipedia hint.',
                        ),
                    ],
                )
                result = await self.resolveSeriesMetadata(
                    program=program,
                    hints=hints,
                )
                return ConnectionTestResult(
                    success=True,
                    latency_ms=result.latency_ms,
                    model=result.model,
                    message='Chat Completions のシリーズ情報生成契約を確認しました。',
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    http_status=result.http_status,
                    selected_choice_id=(
                        f'series:{result.existing_series_id}'
                        if result.existing_series_id is not None
                        else 'series:generated'
                        if result.decision == 'Series'
                        else 'not-series'
                        if result.decision == 'NotSeries'
                        else 'unresolved'
                    ),
                )
            elif capability == 'EpisodeLookup':
                program = RecordedEpisodeLookupContext(
                    pipeline_version=RECORDED_EPISODE_CONTEXT_VERSION,
                    series=RecordedEpisodeContextSeries(
                        title='KonomiTV-BS4K connection test',
                        genres=['ConnectionTest'],
                        description='',
                        known_episode_count=0,
                        known_episode_min=None,
                        known_episode_max=None,
                        known_episode_sample=[],
                    ),
                    program=RecordedEpisodeContextProgram(
                        title='OpenAI Responses API connection test',
                        subtitle=None,
                        description='This is a capability test. Find an official OpenAI documentation page.',
                        detail_items=[],
                        broadcast_datetime=date.today().isoformat(),
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
                    constraints=[
                        'This is a synthetic connection test.',
                        'Use Web search and return a verified source URL.',
                    ],
                )
                result = await self.lookupEpisode(program)
                checks = _buildEpisodeLookupConnectionChecks(result)
                selected_choice_id: str | None = result.outcome
                if (
                    result.outcome == 'Resolved' and
                    result.season_number is not None and
                    result.episode_number is not None
                ):
                    selected_choice_id = f'S{result.season_number}E{result.episode_number}'
                if _episodeLookupConnectionSucceeded(checks) is False:
                    failed_check = next(
                        check
                        for check in (
                            checks.backend_connection,
                            checks.web_search,
                            checks.source_url,
                            checks.strict_schema,
                        )
                        if check.status != 'Passed'
                    )
                    return ConnectionTestResult(
                        success=False,
                        latency_ms=result.latency_ms,
                        model=result.model,
                        message=failed_check.message,
                        checks=checks,
                        prompt_tokens=result.prompt_tokens,
                        completion_tokens=result.completion_tokens,
                        http_status=result.http_status,
                        error_code=result.error_code,
                        selected_choice_id=selected_choice_id,
                    )
                return ConnectionTestResult(
                    success=True,
                    latency_ms=result.latency_ms,
                    model=result.model,
                    message='Responses API + Web Search 互換を確認しました。',
                    checks=checks,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    http_status=result.http_status,
                    selected_choice_id=selected_choice_id,
                )
        except RecordedSeriesAIError as ex:
            return ConnectionTestResult(
                success=False,
                latency_ms=ex.latency_ms or 0,
                model=settings.model,
                message=f'API エラー: {ex.code}',
                http_status=ex.http_status,
                error_code=ex.code,
            )
        return ConnectionTestResult(
            success=False,
            latency_ms=0,
            model=settings.model,
            message=f'未対応の接続試験です: {capability}',
        )
