from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from tortoise.expressions import Q
from typing_extensions import TypedDict

from app import logging, schemas
from app.metadata.ai.episode_lookup import EpisodeLookupOutcome, IsPublicHTTPURL
from app.metadata.ai.KonomiTVBS4KACPCredentials import KonomiTVBS4KACPImportProvider
from app.metadata.RecordedEpisodeAutomation import (
    RecordedEpisodeAutomation,
    RecordedEpisodeRelookupConflictError,
    RecordedEpisodeRelookupDisabledError,
    RecordedEpisodeRelookupNotFoundError,
    RecordedEpisodeRelookupRateLimitedError,
)
from app.metadata.RecordedEpisodeMessages import GetRecordedEpisodeErrorMessage
from app.metadata.RecordedEpisodeResolver import (
    RecordedEpisodeAssignmentStaleError,
    RecordedEpisodeCrossSeriesError,
    RecordedEpisodeInvalidNumberError,
    RecordedEpisodeProgramNotFoundError,
    RecordedEpisodeResolver,
    RecordedEpisodeSeriesNotAssignedError,
    RecordedEpisodeTargetNotFoundError,
)
from app.metadata.RecordedSeriesLocks import RECORDED_SERIES_RESOLUTION_LOCK
from app.metadata.RecordedSeriesResolver import (
    RecordedSeriesChannelUnavailableError,
    RecordedSeriesInvalidTitleError,
    RecordedSeriesMetadataStaleError,
    RecordedSeriesProgramNotFoundError,
    RecordedSeriesResolver,
    RecordedSeriesResolverBusyError,
    RecordedSeriesTargetNotFoundError,
    RecordedSeriesTitleConflictError,
)
from app.metadata.RecordedSeriesSettings import (
    RecordedSeriesSettings,
    RecordedSeriesSettingsResponse,
    RecordedSeriesSettingsStore,
)
from app.models.RecordedEpisode import RecordedEpisodeResolution, SeriesEpisode
from app.models.RecordedProgram import RecordedProgram
from app.models.Series import Series
from app.models.User import User
from app.routers.UsersRouter import GetCurrentAdminUser


router = APIRouter(
    tags=["Recorded Series"],
    prefix="/api/recorded-series",
)

NO_STORE_HEADERS = {"Cache-Control": "no-store"}
MAX_API_KEY_LENGTH = 8192
RECORDED_SERIES_MANAGEMENT_DEFAULT_PAGE_SIZE = 30
RECORDED_SERIES_MANAGEMENT_MAX_PAGE_SIZE = 100


class RecordedSeriesStatusResponse(BaseModel):
    """Web設定画面へ返す録画シリーズ判定の集計。"""

    model_config = ConfigDict(extra="forbid")

    total: int
    pending: int
    resolved: int
    not_series: int
    needs_review: int
    failed: int
    episode_resolved: int
    episode_unknown: int
    episode_not_numbered: int
    episode_no_published_number: int
    episode_needs_review: int
    episode_failed: int
    last_run_at: str | None
    episode_last_run_at: str | None
    is_running: bool
    is_episode_running: bool


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
    resolution_status: (
        Literal[
            'Pending',
            'Resolved',
            'NotSeries',
            'NeedsReview',
            'Failed',
        ]
        | None
    )
    resolution_source: (
        Literal[
            'Rule',
            'Local',
            'EPG',
            'MediaWiki',
            'AI',
            'Manual',
        ]
        | None
    )


class RecordedSeriesStandaloneProgramListResponse(BaseModel):
    """シリーズ未所属録画の管理用ページング一覧。"""

    model_config = ConfigDict(extra='forbid')

    total: int
    page: int
    page_size: int
    items: list[RecordedSeriesStandaloneProgram]


class RecordedSeriesBackfillRequest(BaseModel):
    """既存録画の一括判定方法。"""

    model_config = ConfigDict(extra="forbid")

    force: bool = False


class RecordedSeriesAssignmentRequest(BaseModel):
    """管理者が録画1件へ確定させるシリーズ所属。"""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["Series", "NotSeries"]
    series_id: Annotated[int | None, Field(gt=0)] = None
    series_title: Annotated[str | None, Field(max_length=255)] = None

    @model_validator(mode="after")
    def validateTarget(self) -> Self:
        """decisionに対して既存IDまたは新規タイトルの指定が一意であることを検証する。

        Returns:
            検証後のリクエスト自身。

        Raises:
            ValueError: Series指定の過不足、またはNotSeriesへの不要な対象指定がある場合。
        """

        if self.series_title is not None:
            self.series_title = self.series_title.strip()
            if self.series_title == "":
                raise ValueError("series_title must not be blank.")
        if self.decision == "Series":
            if (self.series_id is None) == (self.series_title is None):
                raise ValueError(
                    "Series assignment requires exactly one of series_id or series_title."
                )
        elif self.series_id is not None or self.series_title is not None:
            raise ValueError(
                "NotSeries assignment cannot include series_id or series_title."
            )
        return self


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


class RecordedSeriesManagementUpdateRequest(BaseModel):
    """管理者が変更できるSeries表示メタデータ。"""

    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, Field(max_length=255)]
    description: Annotated[str, Field(max_length=10000)]
    expected_title: str
    expected_description: str

    @model_validator(mode="after")
    def validateMetadata(self) -> Self:
        """タイトルを正規化可能な非空文字列へ制限する。

        Returns:
            前後空白を除去したリクエスト自身。

        Raises:
            ValueError: タイトルが空白だけの場合。
        """

        self.title = self.title.strip()
        self.description = self.description.strip()
        if self.title == "":
            raise ValueError("title must not be blank.")
        return self


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


class _RecordedEpisodeAssignmentRequestBase(BaseModel):
    """全話数割当判断に共通する楽観ロック値。"""

    model_config = ConfigDict(extra="forbid")

    expected_series_id: Annotated[int, Field(gt=0)]
    expected_series_episode_id: Annotated[int | None, Field(gt=0)]


class RecordedEpisodeRelookupRequest(_RecordedEpisodeAssignmentRequestBase):
    """録画1件のAI話数再検索を開始する楽観ロック付き要求。"""

    override_manual: bool = False


class RecordedEpisodeExistingAssignmentRequest(_RecordedEpisodeAssignmentRequestBase):
    """Series内の既存Episodeを選ぶ手動判断。"""

    decision: Literal["ExistingEpisode"]
    episode_id: Annotated[int, Field(gt=0)]


class RecordedEpisodeStructuredAssignmentRequest(_RecordedEpisodeAssignmentRequestBase):
    """新しいシーズン・話数を入力する手動判断。"""

    decision: Literal["StructuredEpisode"]
    season_number: Annotated[int, Field(ge=0, le=2_147_483_647)]
    episode_number: Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=3)]


class RecordedEpisodeUnknownAssignmentRequest(_RecordedEpisodeAssignmentRequestBase):
    """この録画の話数を不明として確定する手動判断。"""

    decision: Literal["Unknown"]


class RecordedEpisodeNoPublishedNumberAssignmentRequest(_RecordedEpisodeAssignmentRequestBase):
    """公開話数のない録画を、任意のシーズンへ所属させる手動判断。"""

    decision: Literal['NoPublishedNumber']
    season_number: Annotated[int | None, Field(ge=0, le=2_147_483_647)] = None


class RecordedEpisodeNotNumberedAssignmentRequest(_RecordedEpisodeAssignmentRequestBase):
    """話数番号制度を持たない録画を、任意のシーズンへ所属させる手動判断。"""

    decision: Literal['NotNumbered']
    season_number: Annotated[int | None, Field(ge=0, le=2_147_483_647)] = None


class RecordedEpisodeAdoptAIAssignmentRequest(_RecordedEpisodeAssignmentRequestBase):
    """保存済み AI レーンの提案を正本として採用する判断。"""

    decision: Literal['AdoptAI']


RecordedEpisodeAssignmentRequest = Annotated[
    RecordedEpisodeExistingAssignmentRequest
    | RecordedEpisodeStructuredAssignmentRequest
    | RecordedEpisodeNoPublishedNumberAssignmentRequest
    | RecordedEpisodeNotNumberedAssignmentRequest
    | RecordedEpisodeUnknownAssignmentRequest
    | RecordedEpisodeAdoptAIAssignmentRequest,
    Field(discriminator='decision'),
]


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
    # 起動時に設定破損などでPending回収できなかった場合も、設定修復直後に再試行する。
    await RecordedSeriesResolver.retryPendingRecovery()
    # 話数側は旧受理条件の保存済み提案を無課金昇格し、新規録画の保留だけを再評価する。
    await RecordedEpisodeAutomation.settingsUpdated()


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
    ).prefetch_related('channel', 'series_resolution')
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
        # series_resolution は型定義済みの reverse OneToOne。prefetch 済みで、
        # 判定行が未作成の録画では None になる。
        resolution = program.series_resolution
        resolution_status: (
            Literal['Pending', 'Resolved', 'NotSeries', 'NeedsReview', 'Failed'] | None
        ) = None
        resolution_source: (
            Literal['Rule', 'Local', 'EPG', 'MediaWiki', 'AI', 'Manual'] | None
        ) = None
        if resolution is not None:
            if resolution.status in {
                'Pending',
                'Resolved',
                'NotSeries',
                'NeedsReview',
                'Failed',
            }:
                resolution_status = resolution.status
            if resolution.source in {
                'Rule',
                'Local',
                'EPG',
                'MediaWiki',
                'AI',
                'Manual',
            }:
                resolution_source = resolution.source
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
                resolution_status=resolution_status,
                resolution_source=resolution_source,
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
    """競合後の再編集にも使える、管理画面向けSeries最新情報を返す。"""

    response.headers.update(NO_STORE_HEADERS)
    series = await Series.filter(id=series_id).first()
    if series is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Specified series_id was not found.",
            headers=NO_STORE_HEADERS,
        )
    return (await _buildRecordedSeriesManagementItems([series]))[0]


@router.put(
    "/series/{series_id}",
    summary="録画シリーズ管理情報更新 API",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def RecordedSeriesManagementUpdateAPI(
    series_id: Annotated[int, Path(gt=0, description="更新するSeries ID。")],
    request: RecordedSeriesManagementUpdateRequest,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """Seriesの表示名と説明を、関連録画の表示名と原子的に更新する。

    Args:
        series_id: 更新対象のSeries ID。
        request: 新しいタイトルと説明。
        response: Cache-Controlヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        None

    Raises:
        HTTPException: Series不在、無効タイトル、競合、更新済み、またはResolver実行中の場合。
    """

    response.headers.update(NO_STORE_HEADERS)
    try:
        await RecordedSeriesResolver.updateSeriesMetadata(
            series_id,
            title=request.title,
            description=request.description,
            expected_title=request.expected_title,
            expected_description=request.expected_description,
        )
    except RecordedSeriesTargetNotFoundError as ex:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Specified series_id was not found.",
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedSeriesTitleConflictError as ex:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another series or recorded-series rule already uses the specified title.",
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedSeriesMetadataStaleError as ex:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recorded series metadata was updated by another request.",
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedSeriesResolverBusyError as ex:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Recorded series resolution is currently busy.",
            headers={**NO_STORE_HEADERS, "Retry-After": "5"},
        ) from ex
    except RecordedSeriesInvalidTitleError as ex:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Recorded series title is invalid.",
            headers=NO_STORE_HEADERS,
        ) from ex


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


@router.put(
    "/programs/{recorded_program_id}/episode-assignment",
    summary="録画シリーズ話数手動割当更新 API",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def RecordedEpisodeAssignmentUpdateAPI(
    recorded_program_id: Annotated[int, Path(gt=0, description="録画番組の ID。")],
    request: RecordedEpisodeAssignmentRequest,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """管理者の話数判断をSeries・Episodeの楽観ロック付きで保存する。

    Args:
        recorded_program_id: 更新対象のRecordedProgram ID。
        request: 既存Episode、新規構造化話数、またはUnknownの判断。
        response: Cache-Controlヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        None

    Raises:
        HTTPException: 録画・Episode不在、競合、別Series指定、または不正値の場合。
    """

    response.headers.update(NO_STORE_HEADERS)
    episode_id = request.episode_id if request.decision == "ExistingEpisode" else None
    if request.decision == 'StructuredEpisode':
        season_number = request.season_number
    elif request.decision == 'NoPublishedNumber':
        season_number = request.season_number
    elif request.decision == 'NotNumbered':
        season_number = request.season_number
    else:
        season_number = None
    episode_number = (
        request.episode_number if request.decision == "StructuredEpisode" else None
    )
    try:
        async with RECORDED_SERIES_RESOLUTION_LOCK:
            await RecordedEpisodeResolver.assignProgramEpisode(
                recorded_program_id,
                expected_series_id=request.expected_series_id,
                expected_series_episode_id=request.expected_series_episode_id,
                decision=request.decision,
                episode_id=episode_id,
                season_number=season_number,
                episode_number=episode_number,
            )
    except RecordedEpisodeProgramNotFoundError as ex:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Specified recorded_program_id was not found.",
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedEpisodeTargetNotFoundError as ex:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Specified episode_id was not found.",
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedEpisodeSeriesNotAssignedError as ex:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The recorded program is not assigned to a series.",
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedEpisodeAssignmentStaleError as ex:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The series or episode assignment was updated by another request.",
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedEpisodeCrossSeriesError as ex:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The specified episode belongs to another series.",
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedEpisodeInvalidNumberError as ex:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The episode assignment is invalid.",
            headers=NO_STORE_HEADERS,
        ) from ex


@router.post(
    "/programs/{recorded_program_id}/episode-relookup",
    summary="録画1件の話数AI再検索 API",
    response_model=schemas.AnalysisTaskAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def RecordedEpisodeRelookupAPI(
    recorded_program_id: Annotated[
        int, Path(gt=0, description="再検索する録画番組の ID。")
    ],
    request: RecordedEpisodeRelookupRequest,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> schemas.AnalysisTaskAccepted:
    """録画1件の話数Web検索を楽観ロック付きでバックグラウンド開始する。"""

    response.headers.update(NO_STORE_HEADERS)
    try:
        accepted = await RecordedEpisodeAutomation.startRelookup(
            recorded_program_id,
            expected_series_id=request.expected_series_id,
            expected_series_episode_id=request.expected_series_episode_id,
            override_manual=request.override_manual,
        )
    except RecordedEpisodeRelookupNotFoundError as ex:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The recorded program is not available for episode lookup.",
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedEpisodeRelookupConflictError as ex:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The recorded program state conflicts with the episode lookup request.",
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedEpisodeRelookupDisabledError as ex:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="AI episode number search is not available with the current settings.",
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedEpisodeRelookupRateLimitedError as ex:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="The daily AI request limit has been reached.",
            headers={**NO_STORE_HEADERS, "Retry-After": "3600"},
        ) from ex
    return schemas.AnalysisTaskAccepted(
        execution_id=accepted.execution_id,
        reused=accepted.reused,
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
    """録画シリーズ判定の件数、当日AI利用数、一括処理状態を返す。"""

    response.headers.update(NO_STORE_HEADERS)
    series_status = await RecordedSeriesResolver.getStatus()
    episode_status = await RecordedEpisodeAutomation.getStatus()
    return RecordedSeriesStatusResponse.model_validate(
        {**series_status, **episode_status}
    )


@router.post(
    "/backfill",
    summary="既存録画シリーズ一括判定 API",
    response_model=schemas.AnalysisTaskAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def RecordedSeriesBackfillAPI(
    request: RecordedSeriesBackfillRequest,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> schemas.AnalysisTaskAccepted:
    """未判定・入力変更済みの既存録画をバックグラウンドで二段階判定する。"""

    response.headers.update(NO_STORE_HEADERS)
    settings = RecordedSeriesSettingsStore.getSettings()
    if (
        settings.ai_enabled is False or
        RecordedSeriesSettingsStore.isAIBackendConfigured(settings) is False
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='AI backend is not configured for recorded series resolution.',
            headers=NO_STORE_HEADERS,
        )
    accepted = await RecordedSeriesResolver.startBackfill(
        trigger="Manual", force=request.force
    )
    return schemas.AnalysisTaskAccepted(
        execution_id=accepted.execution_id,
        reused=accepted.reused,
    )


@router.post(
    "/episodes/backfill",
    summary="既存録画話数一括判定 API",
    response_model=schemas.AnalysisTaskAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def RecordedEpisodeBackfillAPI(
    request: RecordedSeriesBackfillRequest,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> schemas.AnalysisTaskAccepted:
    """Series所属済みの既存録画を対象に、話数Web検索をバックグラウンド開始する。"""

    response.headers.update(NO_STORE_HEADERS)
    try:
        accepted = await RecordedEpisodeAutomation.startBackfill(force=request.force)
    except RecordedEpisodeRelookupDisabledError as ex:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='AI episode number search is not available with the current settings.',
            headers=NO_STORE_HEADERS,
        ) from ex
    return schemas.AnalysisTaskAccepted(
        execution_id=accepted.execution_id,
        reused=accepted.reused,
    )


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


@router.put(
    "/programs/{recorded_program_id}/assignment",
    summary="録画シリーズ手動割当更新 API",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def RecordedSeriesAssignmentUpdateAPI(
    recorded_program_id: Annotated[int, Path(gt=0, description="録画番組の ID 。")],
    request: RecordedSeriesAssignmentRequest,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """管理者の確定判断で録画1件を既存・新規Series、または単発番組へ変更する。

    Args:
        recorded_program_id: 変更対象のRecordedProgram ID。
        request: Series指定またはNotSeries指定。
        response: Cache-Controlヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        None

    Raises:
        HTTPException: 録画・Series不在、チャンネル不明、または無効なタイトルの場合。
    """

    response.headers.update(NO_STORE_HEADERS)
    try:
        await RecordedSeriesResolver.assignProgram(
            recorded_program_id,
            decision=request.decision,
            series_id=request.series_id,
            series_title=request.series_title,
        )
        if request.decision == "Series":
            await RecordedEpisodeAutomation.enqueue(recorded_program_id)
    except RecordedSeriesProgramNotFoundError as ex:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Specified recorded_program_id was not found.",
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedSeriesTargetNotFoundError as ex:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Specified series_id was not found.",
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedSeriesChannelUnavailableError as ex:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The recorded program has no channel and cannot be assigned to a series.",
            headers=NO_STORE_HEADERS,
        ) from ex
    except (RecordedSeriesInvalidTitleError, ValueError) as ex:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Recorded series assignment is invalid.",
            headers=NO_STORE_HEADERS,
        ) from ex
