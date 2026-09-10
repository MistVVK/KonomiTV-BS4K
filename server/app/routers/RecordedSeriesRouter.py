from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tortoise import transactions
from tortoise.expressions import Q
from typing_extensions import TypedDict

from app import logging, schemas
from app.metadata.ai.episode_lookup import EpisodeLookupOutcome, IsPublicHTTPURL
from app.metadata.ai.KonomiTVBS4KACPCredentials import KonomiTVBS4KACPImportProvider
from app.metadata.AnalysisTaskTracker import AnalysisTaskHandle, AnalysisTaskTracker
from app.metadata.RecordedEpisodeAutomation import (
    RecordedEpisodeAutomation,
    RecordedEpisodeBackfillBusyError,
)
from app.metadata.RecordedEpisodeMessages import GetRecordedEpisodeErrorMessage
from app.metadata.RecordedSeriesLocks import RECORDED_SERIES_RESOLUTION_LOCK
from app.metadata.RecordedSeriesResolver import (
    RecordedSeriesProgramNotFoundError,
    RecordedSeriesResolver,
)
from app.metadata.RecordedSeriesSettings import (
    IsBangumiExternalMetadataEnabled,
    RecordedSeriesSettings,
    RecordedSeriesSettingsResponse,
    RecordedSeriesSettingsStore,
)
from app.metadata.SeriesAIFallbackTask import (
    SeriesAIFallbackBatchBusyError,
    SeriesAIFallbackTask,
)
from app.metadata.SeriesIndexer import SeriesIndexer
from app.models.KonomiTVBS4KBangumiEpisodeCompletion import (
    KonomiTVBS4KBangumiEpisodeCompletion,
)
from app.models.RecordedEpisode import RecordedEpisodeResolution, SeriesEpisode
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedSeries import (
    RecordedSeriesAIRequest,
    RecordedSeriesResolution,
    RecordedSeriesRule,
)
from app.models.Series import Series
from app.models.SeriesAIFallback import SeriesAIFallback
from app.models.SeriesAlias import SeriesAlias
from app.models.SeriesBroadcastPeriod import SeriesBroadcastPeriod
from app.models.User import User
from app.routers.UsersRouter import GetCurrentAdminUser
from app.utils.KonomiTVBS4KBangumiClient import KonomiTVBS4KBangumiClient
from app.utils.KonomiTVBS4KTmdbClient import KonomiTVBS4KTmdbClient
from app.utils.KonomiTVBS4KTmdbStore import KonomiTVBS4KTmdbStore


router = APIRouter(
    tags=["Recorded Series"],
    prefix="/api/recorded-series",
)

NO_STORE_HEADERS = {"Cache-Control": "no-store"}
MAX_API_KEY_LENGTH = 8192
RECORDED_SERIES_MANAGEMENT_DEFAULT_PAGE_SIZE = 30
RECORDED_SERIES_MANAGEMENT_MAX_PAGE_SIZE = 100
# Indexer は外部待機を含まないため従来の有限上限を維持する。
RECORDED_SERIES_PIPELINE_INDEXER_TIMEOUT_SECONDS = 90.0
# AI と外部同期は開始時バックログの全件処理を基本とし、実測に基づく絶対上限だけを設ける。
RECORDED_SERIES_PIPELINE_AI_FALLBACK_TIMEOUT_SECONDS = 10_800.0
RECORDED_SERIES_PIPELINE_EXTERNAL_SYNC_TIMEOUT_SECONDS = 3_600.0
RECORDED_SERIES_PIPELINE_EPISODE_BACKFILL_TIMEOUT_SECONDS = 10_800.0


_recorded_series_pipeline_start_lock = asyncio.Lock()
_recorded_series_pipeline_task: asyncio.Task[None] | None = None


class RecordedSeriesPipelineRequest(BaseModel):
    """シリーズ保守パイプラインで再評価する録画範囲。"""

    model_config = ConfigDict(extra='forbid')

    scope: Annotated[Literal['Unresolved', 'All'], Field()]


class _RecordedSeriesPipelineSummary(TypedDict):
    """単一executionへ保存する各段階の有限処理結果。"""

    scope: Literal['Unresolved', 'All']
    indexer_linked: int
    ai_processed_groups: int
    ai_remaining_groups: int
    tmdb_matched_series: int
    tmdb_remaining_series: int
    bangumi_matched_series: int
    bangumi_remaining_series: int
    episode_resolved: int
    episode_ai_requests: int
    episode_skipped: int
    episode_remaining_programs: int


class RecordedSeriesStatusResponse(BaseModel):
    """Web設定画面へ返す録画シリーズ判定の集計。"""

    model_config = ConfigDict(extra="forbid")

    total: int
    assigned: int
    unassigned: int
    episode_resolved: int
    episode_unknown: int
    episode_not_numbered: int
    episode_no_published_number: int
    episode_needs_review: int
    episode_failed: int
    episode_last_run_at: str | None
    is_running: bool
    is_episode_running: bool


class RecordedSeriesTmdbAPIKeyBody(BaseModel):
    """TMDb API キー設定ボディ。応答には絶対にエコーしない。"""

    model_config = ConfigDict(extra='forbid')

    api_key: Annotated[
        str,
        Field(min_length=1, max_length=KonomiTVBS4KTmdbStore.API_KEY_MAX_LENGTH),
    ]


class RecordedSeriesTmdbConnectionTestResponse(BaseModel):
    """TMDb 接続試験結果（API キー非返却）。"""

    model_config = ConfigDict(extra='forbid')

    success: Annotated[bool, Field()]
    latency_ms: Annotated[int, Field()]
    message: Annotated[str, Field()]
    http_status: Annotated[int | None, Field()]
    error_code: Annotated[str | None, Field()]


class RecordedSeriesNextProgramResponse(BaseModel):
    """録画終了後に自動再生する、同一シリーズ内の次の録画。"""

    model_config = ConfigDict(extra="forbid")

    recorded_program_id: int | None


class RecordedSeriesStandaloneProgram(BaseModel):
    """管理画面でシリーズへ割り当て直せる、シリーズ未所属の再生可能録画。"""

    model_config = ConfigDict(extra='forbid')

    recorded_program_id: int
    title: str
    subtitle: str | None
    start_time: datetime
    channel_id: str | None
    channel_name: str | None


class RecordedSeriesStandaloneProgramListResponse(BaseModel):
    """シリーズ未所属録画の管理用ページング一覧。"""

    model_config = ConfigDict(extra='forbid')

    total: int
    page: int
    page_size: int
    items: list[RecordedSeriesStandaloneProgram]


class RecordedSeriesDatabaseDeleteResponse(BaseModel):
    """シリーズ DB 削除で消した表ごとの行数。録画本体は含まない。"""

    model_config = ConfigDict(extra="forbid")

    series: int
    series_episodes: int
    series_aliases: int
    series_broadcast_periods: int
    series_ai_fallbacks: int
    recorded_series_rules: int
    recorded_series_resolutions: int
    recorded_series_ai_requests: int
    recorded_episode_resolutions: int
    bangumi_episode_completions: int


class RecordedSeriesManagementItem(BaseModel):
    """管理画面の一覧で使用する、録画をネストしない軽量なSeries情報。"""

    model_config = ConfigDict(extra="forbid")

    id: int
    title: str
    description: str
    wikipedia_page_id: int | None
    recorded_program_count: int
    first_recorded_at: datetime | None
    last_recorded_at: datetime | None
    updated_at: datetime


class RecordedSeriesManagementListResponse(BaseModel):
    """管理画面向けSeries一覧とページング情報。"""

    model_config = ConfigDict(extra="forbid")

    total: int
    page: int
    page_size: int
    items: list[RecordedSeriesManagementItem]


class RecordedEpisodeAssignmentListResponse(BaseModel):
    """Series内のEpisode候補と録画ごとの現在割当。"""

    model_config = ConfigDict(extra="forbid")

    series_id: int
    episodes: list[RecordedEpisodeAssignmentEpisode]
    programs: list[RecordedEpisodeAssignmentProgram]


class RecordedEpisodeAssignmentEpisode(BaseModel):
    """手動割当先として選択可能な構造化Episode。"""

    model_config = ConfigDict(extra="forbid")

    id: int
    season_number: int
    episode_number: schemas.RecordedEpisodeNumber


class RecordedEpisodeAssignmentProgram(BaseModel):
    """Series管理画面で話数を訂正できる再生可能録画。"""

    model_config = ConfigDict(extra="forbid")

    recorded_program_id: int
    title: str
    subtitle: str | None
    legacy_episode_number: str | None
    start_time: datetime
    channel_id: str | None
    channel_name: str | None
    series_episode_id: int | None
    resolution: RecordedEpisodeAssignmentResolution | None


class RecordedEpisodeAssignmentCitation(BaseModel):
    """Web検索結果から保存したHTTP(S)出典。"""

    model_config = ConfigDict(extra="forbid")

    url: str
    title: str


class RecordedEpisodeAssignmentResolution(BaseModel):
    """手動判断時に表示する話数判定状態と根拠。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal[
        'Pending',
        'Resolved',
        'Unknown',
        'NotNumbered',
        'NoPublishedNumber',
        'NeedsReview',
        'Failed',
    ]
    season_number: int | None
    # 現在の正本（採用中のレーン）。
    source: Literal['Local', 'EPG', 'WebSearch', 'Manual', 'Migration', 'AI'] | None
    lookup_outcome: EpisodeLookupOutcome | None
    # AI レーン。
    proposed_outcome: Literal[
        'Resolved',
        'NotNumbered',
        'NoPublishedNumber',
        'InsufficientEvidence',
    ] | None
    proposed_season_number: int | None
    proposed_episode_number: schemas.RecordedEpisodeNumber | None
    confidence: float | None
    web_search_performed: bool
    citations: list[RecordedEpisodeAssignmentCitation]
    rationale_short: str | None
    # 手動レーン。
    manual_season_number: int | None
    manual_episode_number: schemas.RecordedEpisodeNumber | None
    manual_status: Literal[
        'Resolved',
        'Unknown',
        'NotNumbered',
        'NoPublishedNumber',
    ] | None
    error_code: str | None
    error_message: str | None


class _RecordedSeriesProgramSummary(TypedDict):
    """管理一覧1件に集約する再生可能な録画の件数と期間。"""

    recorded_program_count: int
    first_recorded_at: datetime
    last_recorded_at: datetime


async def _buildRecordedSeriesManagementItems(
    series_list: list[Series],
) -> list[RecordedSeriesManagementItem]:
    """Series本体と再生可能な録画の軽量集計を管理画面用レスポンスへ変換する。"""

    series_ids = [series.id for series in series_list]
    program_summaries: dict[int, _RecordedSeriesProgramSummary] = {}
    if len(series_ids) > 0:
        program_rows = await RecordedProgram.filter(
            series_id__in=series_ids,
            recorded_video__status="Recorded",
        ).values(
            "series_id",
            "start_time",
        )
        # Tortoise ORMのreverse joinを使った複数集約はSQLite上で巨大な中間結果を
        # 作り得るため、最大100Series分のID・開始時刻だけを1回取得してメモリ上で集約する。
        for program_row in program_rows:
            program_series_id = program_row["series_id"]
            start_time = program_row["start_time"]
            summary = program_summaries.setdefault(
                program_series_id,
                _RecordedSeriesProgramSummary(
                    recorded_program_count=0,
                    first_recorded_at=start_time,
                    last_recorded_at=start_time,
                ),
            )
            summary["recorded_program_count"] += 1
            first_recorded_at = summary["first_recorded_at"]
            last_recorded_at = summary["last_recorded_at"]
            if start_time < first_recorded_at:
                summary["first_recorded_at"] = start_time
            if start_time > last_recorded_at:
                summary["last_recorded_at"] = start_time

    items: list[RecordedSeriesManagementItem] = []
    for series in series_list:
        summary = program_summaries.get(series.id)
        items.append(
            RecordedSeriesManagementItem(
                id=series.id,
                title=series.title,
                description=series.description,
                wikipedia_page_id=series.wikipedia_page_id,
                recorded_program_count=summary["recorded_program_count"]
                if summary is not None
                else 0,
                first_recorded_at=summary["first_recorded_at"]
                if summary is not None
                else None,
                last_recorded_at=summary["last_recorded_at"]
                if summary is not None
                else None,
                updated_at=series.updated_at,
            )
        )
    return items
async def ParseRecordedSeriesSettingsUpdate(
    request: Request,
) -> RecordedSeriesSettings:
    """録画シリーズ固有設定の更新リクエストを解析する。

    API キー・OpenAI 互換接続情報・日次制限は受理しない（AI バックエンド API へ移行済み）。
    AcpCodex / AcpGrok の ACP フィールドは引き続き受理する。

    Args:
        request: Web UI から送信された HTTP リクエスト。

    Returns:
        検証済み設定。

    Raises:
        HTTPException: JSON または設定が不正な場合。
    """

    try:
        request_body = await request.json()
    except Exception as ex:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Recorded series settings request must be a JSON object.",
            headers=NO_STORE_HEADERS,
        ) from ex
    if isinstance(request_body, dict) is False:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Recorded series settings request must be a JSON object.",
            headers=NO_STORE_HEADERS,
        )

    settings_body = dict(request_body)
    # TMDb API キーは秘密のため設定本体では受理せず、専用エンドポイントへ誘導する。
    if 'tmdb_api_key' in settings_body:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Use PUT /api/recorded-series/settings/tmdb-api-key instead.',
            headers=NO_STORE_HEADERS,
        )
    # 旧クライアントの秘密・OpenAI 互換接続 field・日次制限は受理せず拒否する（クリーンブレーク）。
    rejected_keys = [
        key
        for key in (
            'api_key',
            'api_base_url',
            'model',
            'daily_ai_request_limit',
        )
        if key in settings_body
    ]
    if rejected_keys:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                'Legacy AI connection fields are not accepted. '
                f'Use /api/ai-backends instead (rejected: {", ".join(rejected_keys)}).'
            ),
            headers=NO_STORE_HEADERS,
        )
    # 旧クライアントの廃止済み field は受理するが、保存値や runtime 分岐へ反映しない。
    settings_body.pop("ai_candidate_selection_enabled", None)
    settings_body.pop('ai_episode_number_search_enabled', None)
    settings_body.pop('ai_episode_number_acceptance_mode', None)
    settings_body.pop("ai_backend_service_name", None)
    settings_body.pop("ai_backend_auth_configured", None)
    settings_body.pop("ai_fallback_backend_service_name", None)
    settings_body.pop("ai_fallback_backend_auth_configured", None)
    # 応答専用の TMDb キー表示は保存値へ持ち込まない。
    settings_body.pop('tmdb_api_key_configured', None)
    settings_body.pop('tmdb_api_key_masked', None)

    try:
        settings = RecordedSeriesSettings.model_validate(settings_body)
    except ValidationError as ex:
        # Pydantic のエラーから input とドキュメント URL を除外し、リクエスト本文を返さない。
        sanitized_errors = [
            {
                "type": validation_error["type"],
                "loc": ["body", *validation_error["loc"]],
                "msg": validation_error["msg"],
            }
            for validation_error in ex.errors(include_url=False, include_input=False)
        ]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=sanitized_errors,
            headers=NO_STORE_HEADERS,
        ) from ex
    return settings


@router.get(
    "/settings",
    summary="録画シリーズ判定設定取得 API",
    response_description="録画シリーズ固有設定（AI 接続秘密は含まない）。",
    response_model=RecordedSeriesSettingsResponse,
)
async def RecordedSeriesSettingsAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> RecordedSeriesSettingsResponse:
    """録画シリーズ判定設定を取得する。

    Args:
        response: Cache-Control ヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        AI service 参照情報付きの現在の設定。
    """

    response.headers.update(NO_STORE_HEADERS)
    try:
        return RecordedSeriesSettingsStore.getSettingsResponse()
    except (OSError, ValueError) as ex:
        logging.error(
            "[RecordedSeriesSettingsAPI] Failed to load recorded series settings:",
            exc_info=ex,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load recorded series settings.",
            headers=NO_STORE_HEADERS,
        ) from ex


@router.put(
    "/settings",
    summary="録画シリーズ判定設定更新 API",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def RecordedSeriesSettingsUpdateAPI(
    request: Request,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """録画シリーズ判定の固有設定を更新する。

    Args:
        request: 設定更新リクエスト。
        response: Cache-Control ヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        None
    """

    response.headers.update(NO_STORE_HEADERS)
    settings = await ParseRecordedSeriesSettingsUpdate(request)
    try:
        bangumi_was_enabled = IsBangumiExternalMetadataEnabled()
        RecordedSeriesSettingsStore.saveSettings(settings)
    except ValueError as ex:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(ex),
            headers=NO_STORE_HEADERS,
        ) from ex
    except OSError as ex:
        logging.error(
            "[RecordedSeriesSettingsUpdateAPI] Failed to save recorded series settings:",
            exc_info=ex,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save recorded series settings.",
            headers=NO_STORE_HEADERS,
        ) from ex
    # 起動時に重複送信を避けて中断扱いにした bundle も、明示的な設定保存後は再試行する。
    # 話数側は旧受理条件の保存済み提案を無課金昇格し、新規録画の保留だけを再評価する。
    await SeriesAIFallbackTask.schedule(retry_cancelled=True)
    await RecordedEpisodeAutomation.settingsUpdated()
    # 外部メタデータソースを TMDb 有効へ切り替えたときは、次のスキャンを待たずに照合を予約する。
    ## TmdbOnly / BangumiOnly / None の判定と API キー未設定の skip は scheduleSeriesSync() 内で行う。
    KonomiTVBS4KTmdbClient.scheduleSeriesSync()
    # Bangumi を再び有効にした場合も次の起動時スキャンまで待たず、既存の同期経路を再開する。
    if not bangumi_was_enabled and IsBangumiExternalMetadataEnabled(settings):
        from app.utils.KonomiTVBS4KBangumiClient import KonomiTVBS4KBangumiClient
        KonomiTVBS4KBangumiClient.scheduleUserCollectionSync()


@router.put(
    '/settings/tmdb-api-key',
    summary='TMDb API キー設定 API',
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def RecordedSeriesTmdbAPIKeySetAPI(
    body: RecordedSeriesTmdbAPIKeyBody,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> None:
    """UI から受け取った TMDb API キーを Fernet ストアへ暗号化して保存する。

    Args:
        body: TMDb API キーを含むリクエスト。応答へは含めない。
        response: Cache-Control ヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        None
    """

    response.headers.update(NO_STORE_HEADERS)
    try:
        KonomiTVBS4KTmdbStore.save(api_key=body.api_key)
    except ValueError as ex:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='TMDb API key is invalid.',
            headers=NO_STORE_HEADERS,
        ) from ex
    except OSError as ex:
        logging.error(
            '[RecordedSeriesTmdbAPIKeySetAPI] Failed to store TMDb API key:',
            exc_info=ex,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to store TMDb API key.',
            headers=NO_STORE_HEADERS,
        ) from ex
    # キーを保存したあとは、Bangumi 連携と同じく照合をバックグラウンドで開始する。
    KonomiTVBS4KTmdbClient.scheduleSeriesSync()


@router.delete(
    '/settings/tmdb-api-key',
    summary='TMDb API キー削除 API',
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def RecordedSeriesTmdbAPIKeyDeleteAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> None:
    """Fernet ストアから TMDb API キーを削除する。

    削除後も既存の `tmdb_*` メタデータは残る。以降の TMDb 経路だけが skip される。

    Args:
        response: Cache-Control ヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        None
    """

    response.headers.update(NO_STORE_HEADERS)
    try:
        KonomiTVBS4KTmdbStore.clear()
    except OSError as ex:
        logging.error(
            '[RecordedSeriesTmdbAPIKeyDeleteAPI] Failed to delete TMDb API key:',
            exc_info=ex,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete TMDb API key.',
            headers=NO_STORE_HEADERS,
        ) from ex


@router.post(
    '/settings/tmdb-connection-test',
    summary='TMDb 接続試験 API',
    response_model=RecordedSeriesTmdbConnectionTestResponse,
)
async def RecordedSeriesTmdbConnectionTestAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> RecordedSeriesTmdbConnectionTestResponse:
    """保存済み TMDb API キーで TMDb へ実通信し、成否を返す。

    Args:
        response: Cache-Control ヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        秘密を含まない接続試験結果。
    """

    response.headers.update(NO_STORE_HEADERS)
    result = await KonomiTVBS4KTmdbClient.testConnection()
    return RecordedSeriesTmdbConnectionTestResponse(
        success=result.success,
        latency_ms=result.latency_ms,
        message=result.message,
        http_status=result.http_status,
        error_code=result.error_code,
    )


@router.delete(
    "/settings/api-key",
    summary="録画シリーズ判定 API キー削除 API（廃止）",
    status_code=status.HTTP_410_GONE,
)
async def RecordedSeriesAPIKeyDeleteAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
    url: Annotated[
        str | None,
        Query(
            max_length=2048,
            description="廃止。",
        ),
    ] = None,
):
    """旧 OpenAI 互換 API キー削除。AI バックエンド API へ移行済み。

    Args:
        response: Cache-Control ヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。
        url: 無視される。

    Returns:
        None
    """

    response.headers.update(NO_STORE_HEADERS)
    _ = url
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail='Use DELETE /api/ai-backends/{service_id}/api-key instead.',
        headers=NO_STORE_HEADERS,
    )


@router.get(
    "/settings/acp-credentials",
    summary="ACP 認証状態取得 API（廃止）",
    status_code=status.HTTP_410_GONE,
)
async def KonomiTVBS4KACPCredentialStatusAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """ACP 認証状態取得。AI バックエンド API へ移行済み。

    Args:
        response: Cache-Control ヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        None
    """

    response.headers.update(NO_STORE_HEADERS)
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Use GET /api/ai-backends/acp-credentials instead.",
        headers=NO_STORE_HEADERS,
    )


@router.post(
    "/settings/acp-credentials/{provider}/import",
    summary="ACP 認証取り込み API（廃止）",
    status_code=status.HTTP_410_GONE,
)
async def KonomiTVBS4KACPCredentialImportAPI(
    provider: Annotated[
        KonomiTVBS4KACPImportProvider, Path(description="取り込む ACP provider。")
    ],
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """ACP 認証取り込み。AI バックエンド API へ移行済み。

    Args:
        provider: ``codex`` または ``grok``。
        response: Cache-Control ヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        None
    """

    response.headers.update(NO_STORE_HEADERS)
    _ = provider
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Use POST /api/ai-backends/acp-credentials/{provider}/import instead.",
        headers=NO_STORE_HEADERS,
    )


@router.delete(
    "/settings/acp-credentials/{provider}",
    summary="ACP 認証削除 API（廃止）",
    status_code=status.HTTP_410_GONE,
)
async def KonomiTVBS4KACPCredentialDeleteAPI(
    provider: Annotated[
        KonomiTVBS4KACPImportProvider, Path(description="削除する ACP provider。")
    ],
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """ACP 認証削除。AI バックエンド API へ移行済み。

    Args:
        provider: ``codex`` または ``grok``。
        response: Cache-Control ヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        None
    """

    response.headers.update(NO_STORE_HEADERS)
    _ = provider
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Use DELETE /api/ai-backends/acp-credentials/{provider} instead.",
        headers=NO_STORE_HEADERS,
    )


@router.post(
    "/settings/test",
    summary="AI バックエンド接続試験 API（廃止）",
    status_code=status.HTTP_410_GONE,
)
async def RecordedSeriesConnectionTestAPI(
    request: Request,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """AI バックエンド接続試験。AI バックエンド API へ移行済み。

    Args:
        request: 無視される。
        response: Cache-Control ヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        None
    """

    response.headers.update(NO_STORE_HEADERS)
    _ = request
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Use POST /api/ai-backends/connection-test or /api/ai-backends/acp/test instead.",
        headers=NO_STORE_HEADERS,
    )


@router.get(
    '/standalone-programs',
    summary='シリーズ未所属録画一覧取得 API',
    response_model=RecordedSeriesStandaloneProgramListResponse,
)
async def RecordedSeriesStandaloneProgramListAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
    query: Annotated[
        str,
        Query(max_length=255, description='タイトルまたはサブタイトルの部分一致検索。'),
    ] = '',
    page: Annotated[int, Query(ge=1, description='1から始まるページ番号。')] = 1,
    page_size: Annotated[
        int,
        Query(
            ge=1,
            le=RECORDED_SERIES_MANAGEMENT_MAX_PAGE_SIZE,
            description='1ページに返す録画数。',
        ),
    ] = RECORDED_SERIES_MANAGEMENT_DEFAULT_PAGE_SIZE,
) -> RecordedSeriesStandaloneProgramListResponse:
    """管理画面から単発化・シリーズ化を完結できるよう、series_id が無い録画を返す。

    Args:
        response: Cache-Control ヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。
        query: タイトルまたはサブタイトルへ適用する検索語。
        page: 1 から始まるページ番号。
        page_size: 1 ページに返す録画数。

    Returns:
        再生可能なシリーズ未所属録画のページング一覧。
    """

    response.headers.update(NO_STORE_HEADERS)
    normalized_query = query.strip()
    program_query = RecordedProgram.filter(
        series_id=None,
        recorded_video__status='Recorded',
    ).prefetch_related('channel')
    if normalized_query != '':
        program_query = program_query.filter(
            Q(title__icontains=normalized_query)
            | Q(subtitle__icontains=normalized_query),
        )

    total = await program_query.count()
    programs = (
        await program_query.order_by('-start_time', '-id')
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items: list[RecordedSeriesStandaloneProgram] = []
    for program in programs:
        items.append(
            RecordedSeriesStandaloneProgram(
                recorded_program_id=program.id,
                title=program.title,
                subtitle=program.subtitle,
                start_time=program.start_time,
                channel_id=program.channel_id,
                channel_name=(
                    program.channel.name if program.channel is not None else None
                ),
            ),
        )
    return RecordedSeriesStandaloneProgramListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=items,
    )


@router.get(
    '/series',
    summary='録画シリーズ管理一覧取得 API',
    response_model=RecordedSeriesManagementListResponse,
)
async def RecordedSeriesManagementListAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
    query: Annotated[
        str, Query(max_length=255, description="タイトルまたは説明の部分一致検索。")
    ] = "",
    page: Annotated[int, Query(ge=1, description="1から始まるページ番号。")] = 1,
    page_size: Annotated[
        int,
        Query(
            ge=1,
            le=RECORDED_SERIES_MANAGEMENT_MAX_PAGE_SIZE,
            description="1ページに返すSeries数。",
        ),
    ] = RECORDED_SERIES_MANAGEMENT_DEFAULT_PAGE_SIZE,
) -> RecordedSeriesManagementListResponse:
    """管理画面向けに、孤立Seriesを含む軽量なページング一覧を返す。

    Args:
        response: Cache-Controlヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。
        query: タイトルまたは説明へ適用する検索語。
        page: 1から始まるページ番号。
        page_size: 1ページに返すSeries数。

    Returns:
        Series本体と再生可能な録画の集計だけを含む一覧。
    """

    response.headers.update(NO_STORE_HEADERS)
    normalized_query = query.strip()
    series_query = Series.all()
    if normalized_query != "":
        series_query = series_query.filter(
            Q(title__icontains=normalized_query)
            | Q(description__icontains=normalized_query),
        )

    # 通常の /api/series は全録画をネストし、Recorded録画を持つSeriesだけを返す。
    # 管理画面では孤立Seriesも修正できるよう全Seriesを対象にし、録画集計だけを別Queryで取得する。
    total = await series_query.count()
    series_list = (
        await series_query.order_by("-updated_at", "-id")
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = await _buildRecordedSeriesManagementItems(series_list)
    return RecordedSeriesManagementListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=items,
    )


@router.get(
    "/series/{series_id}",
    summary="録画シリーズ管理情報取得 API",
    response_model=RecordedSeriesManagementItem,
)
async def RecordedSeriesManagementDetailAPI(
    series_id: Annotated[int, Path(gt=0, description="取得するSeries ID。")],
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> RecordedSeriesManagementItem:
    """管理画面向けSeries最新情報を返す。"""

    response.headers.update(NO_STORE_HEADERS)
    series = await Series.filter(id=series_id).first()
    if series is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Specified series_id was not found.",
            headers=NO_STORE_HEADERS,
        )
    return (await _buildRecordedSeriesManagementItems([series]))[0]


@router.get(
    "/series/{series_id}/episode-assignments",
    summary="録画シリーズ話数割当一覧取得 API",
    response_model=RecordedEpisodeAssignmentListResponse,
)
async def RecordedEpisodeAssignmentListAPI(
    series_id: Annotated[int, Path(gt=0, description="取得するSeries ID。")],
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> RecordedEpisodeAssignmentListResponse:
    """Series内の構造化Episodeと再生可能録画の現在割当を遅延取得する。

    Args:
        series_id: 取得対象のSeries ID。
        response: Cache-Controlヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        Episode候補、録画、AIまたは手動の判定概要。

    Raises:
        HTTPException: 指定Seriesが存在しない場合。
    """

    response.headers.update(NO_STORE_HEADERS)
    if await Series.filter(id=series_id).exists() is False:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Specified series_id was not found.",
            headers=NO_STORE_HEADERS,
        )

    episodes = await SeriesEpisode.filter(series_id=series_id).all()
    # SQLiteではDecimalFieldが文字列保存されるため、PythonのDecimalで確実に数値順へそろえる。
    episodes.sort(
        key=lambda episode: (episode.season_number, episode.episode_number, episode.id)
    )
    programs = (
        await RecordedProgram.filter(
            series_id=series_id,
            recorded_video__status="Recorded",
        )
        .prefetch_related("channel")
        .order_by("start_time", "id")
    )
    program_ids = [program.id for program in programs]
    resolutions = (
        await RecordedEpisodeResolution.filter(
            recorded_program_id__in=program_ids
        ).all()
        if len(program_ids) > 0
        else []
    )
    resolutions_by_program_id = {
        resolution.recorded_program_id: resolution for resolution in resolutions
    }

    response_programs: list[RecordedEpisodeAssignmentProgram] = []
    for program in programs:
        resolution = resolutions_by_program_id.get(program.id)
        resolution_response = None
        if resolution is not None:
            resolution_response = RecordedEpisodeAssignmentResolution(
                status=resolution.status,
                season_number=resolution.season_number,
                source=resolution.source,
                lookup_outcome=resolution.lookup_outcome,
                proposed_outcome=resolution.proposed_outcome,
                proposed_season_number=resolution.proposed_season_number,
                proposed_episode_number=resolution.proposed_episode_number,
                confidence=resolution.confidence,
                web_search_performed=resolution.web_search_performed,
                citations=[
                    RecordedEpisodeAssignmentCitation(
                        url=citation.get("url", ""),
                        title=citation.get("title", "")[:300],
                    )
                    for citation in resolution.citations
                    if IsPublicHTTPURL(citation.get("url", ""))
                ],
                rationale_short=resolution.rationale_short,
                manual_season_number=resolution.manual_season_number,
                manual_episode_number=resolution.manual_episode_number,
                manual_status=resolution.manual_status,
                error_code=resolution.error_code,
                error_message=GetRecordedEpisodeErrorMessage(resolution.error_code),
            )
        response_programs.append(
            RecordedEpisodeAssignmentProgram(
                recorded_program_id=program.id,
                title=program.title,
                subtitle=program.subtitle,
                legacy_episode_number=program.episode_number,
                start_time=program.start_time,
                channel_id=program.channel_id,
                channel_name=program.channel.name
                if program.channel is not None
                else None,
                series_episode_id=program.series_episode_id,
                resolution=resolution_response,
            )
        )

    return RecordedEpisodeAssignmentListResponse(
        series_id=series_id,
        episodes=[
            RecordedEpisodeAssignmentEpisode(
                id=episode.id,
                season_number=episode.season_number,
                episode_number=episode.episode_number,
            )
            for episode in episodes
        ],
        programs=response_programs,
    )


@router.get(
    "/status",
    summary="録画シリーズ判定状況取得 API",
    response_model=RecordedSeriesStatusResponse,
)
async def RecordedSeriesStatusAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> RecordedSeriesStatusResponse:
    """Indexer の所属件数と話数判定の件数を返す。旧 Resolver の区分は使わない。"""

    response.headers.update(NO_STORE_HEADERS)
    # 所属の正本は RecordedProgram.series_id。Resolution 表は集計しない。
    total = await RecordedProgram.all().count()
    assigned = await RecordedProgram.filter(series_id__not_isnull=True).count()
    episode_status = await RecordedEpisodeAutomation.getStatus()
    return RecordedSeriesStatusResponse.model_validate({
        'total': total,
        'assigned': assigned,
        'unassigned': total - assigned,
        'is_running': (
            _recorded_series_pipeline_task is not None
            and _recorded_series_pipeline_task.done() is False
        ),
        **episode_status,
    })


@router.post(
    '/pipeline',
    summary='録画シリーズ保守パイプライン API',
    response_model=schemas.AnalysisTaskAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def RecordedSeriesPipelineAPI(
    request: RecordedSeriesPipelineRequest,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> schemas.AnalysisTaskAccepted:
    """指定範囲を4段階で再評価し、単一の解析履歴として開始する。

    Args:
        request: 未確定だけ、または全録画を処理する範囲。
        response: no-storeを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        開始した解析履歴ID。

    Raises:
        HTTPException: 設定無効または関連処理の実行中。
    """

    global _recorded_series_pipeline_task

    response.headers.update(NO_STORE_HEADERS)
    async with _recorded_series_pipeline_start_lock:
        if RecordedSeriesSettingsStore.getSettings().enabled is False:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='Recorded series indexing is disabled.',
                headers=NO_STORE_HEADERS,
            )
        episode_status = await RecordedEpisodeAutomation.getStatus()
        is_pipeline_running = (
            _recorded_series_pipeline_task is not None
            and _recorded_series_pipeline_task.done() is False
        )
        if (
            is_pipeline_running
            or SeriesAIFallbackTask.isBatchBusy()
            or bool(episode_status['is_episode_running'])
            or KonomiTVBS4KTmdbClient.hasRunningSync()
            or KonomiTVBS4KBangumiClient.hasRunningSync()
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='Another recorded series maintenance operation is running.',
                headers=NO_STORE_HEADERS,
            )
        handle = await AnalysisTaskTracker.start(
            'BatchSeriesPipeline',
            title=(
                '未確定の録画を一括判定'
                if request.scope == 'Unresolved'
                else 'すべての録画を一括再判定'
            ),
            trigger='Maintenance',
            initial_status='Queued',
            inherit_parent=False,
        )
        _recorded_series_pipeline_task = asyncio.create_task(
            _RunRecordedSeriesPipeline(handle, request.scope),
        )
        return schemas.AnalysisTaskAccepted(
            execution_id=handle.execution.id,
            reused=False,
        )


async def _RunRecordedSeriesPipeline(
    handle: AnalysisTaskHandle,
    scope: Literal['Unresolved', 'All'],
) -> None:
    """有限な4段階を順次実行し、失敗時は後続を開始しない。

    Args:
        handle: 単一の親解析履歴。
        scope: Indexerを再適用する録画範囲。

    Returns:
        None
    """

    global _recorded_series_pipeline_task

    try:
        async with AnalysisTaskTracker.track(
            'BatchSeriesPipeline',
            trigger='Maintenance',
            existing_handle=handle,
        ) as history:
            await history.setStage('indexer_rebuild', 0.0)
            async with asyncio.timeout(RECORDED_SERIES_PIPELINE_INDEXER_TIMEOUT_SECONDS):
                linked_count = await SeriesIndexer.rebuild(
                    scope=scope,
                    schedule_background_tasks=False,
                )
            await history.setProgress(0.25)

            await history.setStage('ai_fallback', 0.25)
            ai_status = await SeriesAIFallbackTask.runBoundedBatch(
                history,
                retry_cancelled=True,
                timeout_seconds=RECORDED_SERIES_PIPELINE_AI_FALLBACK_TIMEOUT_SECONDS,
                progress_start=0.25,
                progress_end=0.5,
                schedule_external_sync=False,
                schedule_episode_automation=False,
            )
            if (
                ai_status['failed_count'] > 0
                or (
                    ai_status['state'] == 'Stopped'
                    and ai_status['stopped_reason'] != 'StageTimeoutReached'
                )
            ):
                raise RuntimeError('SeriesAIFallbackPartialFailure')

            await history.setStage('external_sync', 0.5)
            if (
                KonomiTVBS4KTmdbClient.hasRunningSync()
                or KonomiTVBS4KBangumiClient.hasRunningSync()
            ):
                raise RuntimeError('ExternalSyncBusy')

            # 2サービスの開始時総数と完了数を共有し、作品1件の完了ごとに親progressを更新する。
            external_progress_lock = asyncio.Lock()
            tmdb_progress_ready = False
            tmdb_processed_series = 0
            tmdb_total_series = 0
            tmdb_matched_series = 0
            bangumi_progress_ready = False
            bangumi_processed_series = 0
            bangumi_total_series = 0
            bangumi_matched_series = 0

            async def UpdateExternalProgress(
                source: Literal['Tmdb', 'Bangumi'],
                processed: int,
                total: int,
                matched: int,
            ) -> None:
                """外部同期2経路の進捗を単一stageの範囲へ合成する。

                Args:
                    source: 進捗を通知した外部サービス。
                    processed: 完了済みSeries数。
                    total: 開始時の対象Series総数。
                    matched: 今回照合できたSeries数。

                Returns:
                    None
                """

                nonlocal tmdb_progress_ready, tmdb_processed_series, tmdb_total_series, tmdb_matched_series
                nonlocal bangumi_progress_ready, bangumi_processed_series, bangumi_total_series, bangumi_matched_series
                async with external_progress_lock:
                    if source == 'Tmdb':
                        tmdb_progress_ready = True
                        tmdb_processed_series = processed
                        tmdb_total_series = total
                        tmdb_matched_series = matched
                    else:
                        bangumi_progress_ready = True
                        bangumi_processed_series = processed
                        bangumi_total_series = total
                        bangumi_matched_series = matched
                    # 両方の開始時総数が確定する前は、後着側によるprogressの巻き戻りを避ける。
                    if tmdb_progress_ready and bangumi_progress_ready:
                        processed_total = tmdb_processed_series + bangumi_processed_series
                        target_total = tmdb_total_series + bangumi_total_series
                        stage_fraction = processed_total / target_total if target_total > 0 else 1.0
                        await history.setProgress(0.5 + 0.25 * stage_fraction)

            tmdb_sync_task = KonomiTVBS4KTmdbClient.startPipelineSync(
                progress_callback=lambda processed, total, matched: UpdateExternalProgress(
                    'Tmdb', processed, total, matched,
                ),
            )
            bangumi_sync_task = KonomiTVBS4KBangumiClient.startPipelineSync(
                progress_callback=lambda processed, total, matched: UpdateExternalProgress(
                    'Bangumi', processed, total, matched,
                ),
            )
            external_item_failure_count = 0
            external_sync_timeout = asyncio.timeout(RECORDED_SERIES_PIPELINE_EXTERNAL_SYNC_TIMEOUT_SECONDS)
            try:
                try:
                    async with external_sync_timeout:
                        tmdb_failed_series, bangumi_failed_series = await asyncio.gather(
                            tmdb_sync_task,
                            bangumi_sync_task,
                        )
                        external_item_failure_count = tmdb_failed_series + bangumi_failed_series
                except TimeoutError:
                    if external_sync_timeout.expired() is False:
                        raise
                    # 絶対上限では現在の外部待機を止め、未完了数をsummaryへ残して後続へ進む。
                    pass
            finally:
                # timeout・片側失敗でも同期taskを裏で継続させず、両方を回収してから失敗させる。
                for sync_task in (tmdb_sync_task, bangumi_sync_task):
                    if sync_task.done() is False:
                        sync_task.cancel()
                await asyncio.gather(tmdb_sync_task, bangumi_sync_task, return_exceptions=True)
            await history.setProgress(0.75)
            # 回復可能な作品単位エラーでは開始時スナップショットを全走査し、
            ## 後続stageへ進める前に既存の部分失敗契約へ載せる。
            if external_item_failure_count > 0:
                raise RuntimeError('ExternalSyncPartialFailure')

            await history.setStage('episode_backfill', 0.75)
            try:
                episode_summary = await RecordedEpisodeAutomation.runBoundedBackfill(
                    history,
                    progress_start=0.75,
                    progress_end=1.0,
                    timeout_seconds=RECORDED_SERIES_PIPELINE_EPISODE_BACKFILL_TIMEOUT_SECONDS,
                )
            except RecordedEpisodeBackfillBusyError as ex:
                raise RuntimeError('EpisodeBackfillBusy') from ex
            if episode_summary['failed'] > 0:
                raise RuntimeError('EpisodeBackfillPartialFailure')

            ai_remaining_groups = max(
                0,
                ai_status['total_groups'] - ai_status['processed_groups'],
            )
            summary = _RecordedSeriesPipelineSummary(
                scope=scope,
                indexer_linked=linked_count,
                ai_processed_groups=ai_status['processed_groups'],
                ai_remaining_groups=ai_remaining_groups,
                tmdb_matched_series=tmdb_matched_series,
                tmdb_remaining_series=max(0, tmdb_total_series - tmdb_processed_series),
                bangumi_matched_series=bangumi_matched_series,
                bangumi_remaining_series=max(0, bangumi_total_series - bangumi_processed_series),
                episode_resolved=episode_summary['resolved'],
                episode_ai_requests=episode_summary['ai_requests'],
                episode_skipped=episode_summary['skipped'],
                episode_remaining_programs=episode_summary['remaining'],
            )
            await history.finish(summary=cast(dict[str, object], summary))
    except asyncio.CancelledError:
        raise
    except Exception as ex:
        logging.error('[RecordedSeriesPipeline] Pipeline failed.', exc_info=ex)
    finally:
        _recorded_series_pipeline_task = None


async def StopRecordedSeriesPipeline() -> None:
    """サーバー終了前に実行中パイプラインを中断状態へ閉じる。

    Returns:
        None
    """

    global _recorded_series_pipeline_task

    task = _recorded_series_pipeline_task
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    _recorded_series_pipeline_task = None


@router.delete(
    "/database",
    summary="シリーズデータベース削除 API",
    response_model=RecordedSeriesDatabaseDeleteResponse,
)
async def RecordedSeriesDatabaseDeleteAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> RecordedSeriesDatabaseDeleteResponse:
    """シリーズ関連データだけを単一トランザクションで削除する。

    録画本体・サムネイル・CM 解析・視聴履歴・ユーザー/チャンネルは残し、
    番組のシリーズ関連列だけ NULL 化する。Indexer 再適用は自動起動しない。
    """

    response.headers.update(NO_STORE_HEADERS)
    try:
        async with _recorded_series_pipeline_start_lock:
            counts = await _DeleteSeriesDatabase()
    except _SeriesDatabaseBusyError as ex:
        busy_detail = {
            'AIFallbackRunning': 'Series AI fallback batch is running.',
            'EpisodeBackfillRunning': 'Episode backfill is running.',
            'PipelineRunning': 'Recorded series maintenance pipeline is running.',
        }[ex.reason]
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=busy_detail,
            headers=NO_STORE_HEADERS,
        ) from ex
    return RecordedSeriesDatabaseDeleteResponse.model_validate({
        'series': counts['series'],
        'series_episodes': counts['series_episodes'],
        'series_aliases': counts['series_aliases'],
        'series_broadcast_periods': counts['series_broadcast_periods'],
        'series_ai_fallbacks': counts['series_ai_fallbacks'],
        'recorded_series_rules': counts['recorded_series_rules'],
        'recorded_series_resolutions': counts['recorded_series_resolutions'],
        'recorded_series_ai_requests': counts['recorded_series_ai_requests'],
        'recorded_episode_resolutions': counts['recorded_episode_resolutions'],
        'bangumi_episode_completions': counts['bangumi_episode_completions'],
    })


class _SeriesDatabaseBusyError(Exception):
    """削除中に走らせてはいけないバッチが実行中のため削除できない。"""

    def __init__(
        self,
        reason: Literal['AIFallbackRunning', 'EpisodeBackfillRunning', 'PipelineRunning'],
    ) -> None:
        """実行中バッチの種別を保持する。

        Args:
            reason: 409 の原因となった実行中バッチ。
        """

        super().__init__(reason)
        # 呼び出し元の HTTP 変換が参照する実行中バッチ種別。
        self.reason = reason


class _SeriesDatabaseDeleteCounts(TypedDict):
    """シリーズ DB 削除で消した行数の概要。録画本体の行数は含まない。"""

    series: int
    series_episodes: int
    series_aliases: int
    series_broadcast_periods: int
    series_ai_fallbacks: int
    recorded_series_rules: int
    recorded_series_resolutions: int
    recorded_series_ai_requests: int
    recorded_episode_resolutions: int
    bangumi_episode_completions: int


async def _DeleteSeriesDatabase() -> _SeriesDatabaseDeleteCounts:
    """シリーズ関連データを排他境界の中で削除し、件数概要を返す。

    補完バッチの batch lock を非待機で取得し、話数解決 lock を保持したまま
    判定・commit・cache clear を行う。実行中バッチがある場合は待たずに失敗
    し、新規バッチ開始は lock で直列化する。開始済み backfill の最初の解決は
    lock に止まり、削除確定後の状態で安全に skip する。話数の実行中判定は
    lock 待ちの前後で行い、完了後の status だけでの可否判断にしない。

    Returns:
        表ごとの削除行数。

    Raises:
        _SeriesDatabaseBusyError: AI 補完バッチまたは話数一括判定の実行中。
    """

    # 話数一括判定・単票再検索の実行中は resolution lock 待ちへ入る前に拒否する。
    ## 完了後の status だけでは実行中を見逃すため、lock 保持中に再判定する。
    if (
        _recorded_series_pipeline_task is not None
        and _recorded_series_pipeline_task.done() is False
    ):
        raise _SeriesDatabaseBusyError('PipelineRunning')
    pre_delete_status = await RecordedEpisodeAutomation.getStatus()
    if bool(pre_delete_status['is_episode_running']):
        raise _SeriesDatabaseBusyError('EpisodeBackfillRunning')
    try:
        # holdBatchExclusion() は非待機で取得する。実行中は待たずに例外で失敗する。
        async with SeriesAIFallbackTask.holdBatchExclusion():
            async with RECORDED_SERIES_RESOLUTION_LOCK:
                # lock 待ちの間に開始・終了した話数バッチを最終判定する。
                episode_status = await RecordedEpisodeAutomation.getStatus()
                if bool(episode_status['is_episode_running']):
                    raise _SeriesDatabaseBusyError('EpisodeBackfillRunning')
                async with transactions.in_transaction() as connection:
                    # CASCADE で録画本体が消えないよう、先に番組側の関連列だけ NULL 化する。
                    ## episode_number は Indexer が EPG から再導出するため、再適用で復元する。
                    await RecordedProgram.all().using_db(connection).update(
                        series_id=None,
                        series_broadcast_period_id=None,
                        series_title=None,
                        episode_number=None,
                        series_episode_id=None,
                        bangumi_episode_id=None,
                    )
                    counts = _SeriesDatabaseDeleteCounts(
                        series=await Series.all().using_db(connection).count(),
                        series_episodes=await SeriesEpisode.all().using_db(connection).count(),
                        series_aliases=await SeriesAlias.all().using_db(connection).count(),
                        series_broadcast_periods=await SeriesBroadcastPeriod.all().using_db(connection).count(),
                        series_ai_fallbacks=await SeriesAIFallback.all().using_db(connection).count(),
                        recorded_series_rules=await RecordedSeriesRule.all().using_db(connection).count(),
                        recorded_series_resolutions=await RecordedSeriesResolution.all().using_db(connection).count(),
                        recorded_series_ai_requests=await RecordedSeriesAIRequest.all().using_db(connection).count(),
                        recorded_episode_resolutions=await RecordedEpisodeResolution.all().using_db(connection).count(),
                        bangumi_episode_completions=await KonomiTVBS4KBangumiEpisodeCompletion.all().using_db(connection).count(),
                    )
                    # 子表から親表の順に消し、外部キー制約の有無に依らず成立させる。
                    await RecordedEpisodeResolution.all().using_db(connection).delete()
                    await RecordedSeriesAIRequest.all().using_db(connection).delete()
                    await RecordedSeriesResolution.all().using_db(connection).delete()
                    await RecordedSeriesRule.all().using_db(connection).delete()
                    await SeriesAIFallback.all().using_db(connection).delete()
                    await KonomiTVBS4KBangumiEpisodeCompletion.all().using_db(connection).delete()
                    await SeriesAlias.all().using_db(connection).delete()
                    await SeriesBroadcastPeriod.all().using_db(connection).delete()
                    await SeriesEpisode.all().using_db(connection).delete()
                    await Series.all().using_db(connection).delete()

                # commit 成功後に確定 cache を破棄する。rollback 時は古い cache を残す。
                ## batch lock 保持中のため、破棄後の再充填は起きない。
                SeriesAIFallbackTask.clearResolvedAssignments()
                logging.info(
                    '[RecordedSeriesDatabase] Deleted series database. '
                    f'[series: {counts["series"]}]',
                )
                return counts
    except SeriesAIFallbackBatchBusyError:
        raise _SeriesDatabaseBusyError('AIFallbackRunning') from None


@router.get(
    "/programs/{recorded_program_id}/next",
    summary="録画シリーズ次番組取得 API",
    response_model=RecordedSeriesNextProgramResponse,
)
async def RecordedSeriesNextProgramAPI(
    recorded_program_id: Annotated[
        int, Path(gt=0, description="現在再生している録画番組の ID 。")
    ],
    response: Response,
) -> RecordedSeriesNextProgramResponse:
    """同一シリーズ内で現在の録画の直後に再生できる録画 ID を返す。

    Args:
        recorded_program_id: 現在再生している RecordedProgram ID。
        response: Cache-Control ヘッダーを設定するレスポンス。

    Returns:
        次の録画 ID。シリーズ未所属または最終話なら null。

    Raises:
        HTTPException: 現在の録画が存在しないか再生可能でない場合。
    """

    response.headers.update(NO_STORE_HEADERS)
    try:
        next_program_id = await RecordedSeriesResolver.getNextProgramID(
            recorded_program_id
        )
    except RecordedSeriesProgramNotFoundError as ex:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Specified recorded_program_id was not found.",
            headers=NO_STORE_HEADERS,
        ) from ex
    return RecordedSeriesNextProgramResponse(recorded_program_id=next_program_id)
