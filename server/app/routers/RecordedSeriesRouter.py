from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self, cast

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
from app.metadata.ai.backends import (
    ConnectionTestCheck,
    ConnectionTestResult,
    EpisodeLookupConnectionChecks,
)
from app.metadata.ai.episode_lookup import EpisodeLookupOutcome, IsPublicHTTPURL
from app.metadata.ai.KonomiTVBS4KACPCredentials import (
    KonomiTVBS4KACPCredentialError,
    KonomiTVBS4KACPCredentials,
    KonomiTVBS4KACPImportProvider,
)
from app.metadata.ai.recorded_series_ai import (
    ACP_CREDENTIAL_OPERATION_LOCK,
    get_audit_model,
    get_episode_lookup_provider_fingerprint,
    invalidate_episode_lookup_capability_fingerprint,
    invalidate_episode_lookup_capability_proof,
    record_episode_lookup_capability_proof,
    test_connection,
)
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
from app.metadata.RecordedSeriesCandidates import RecordedSeriesAIError
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
from app.models.RecordedSeries import RecordedSeriesAIRequest
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


class RecordedSeriesConnectionTestCheckResponse(BaseModel):
    """接続試験の1能力について、実測できた状態と安全な説明を返す。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["Passed", "Failed", "NotRun", "NotApplicable"]
    message: str


class RecordedSeriesEpisodeLookupConnectionChecksResponse(BaseModel):
    """EpisodeLookup 接続試験の固定6項目。"""

    model_config = ConfigDict(extra="forbid")

    backend_connection: RecordedSeriesConnectionTestCheckResponse
    web_search: RecordedSeriesConnectionTestCheckResponse
    source_url: RecordedSeriesConnectionTestCheckResponse
    strict_schema: RecordedSeriesConnectionTestCheckResponse
    timeout_cancel: RecordedSeriesConnectionTestCheckResponse
    permission_policy: RecordedSeriesConnectionTestCheckResponse


class RecordedSeriesConnectionTestResponse(BaseModel):
    """秘密情報を含まないAIバックエンド接続試験結果。"""

    model_config = ConfigDict(extra="forbid")

    success: bool
    latency_ms: int
    model: str
    message: str
    checks: RecordedSeriesEpisodeLookupConnectionChecksResponse | None


class KonomiTVBS4KACPCredentialStatusResponse(BaseModel):
    """認証内容を含まない録画シリーズ ACP の共有管理者資格情報状態。"""

    model_config = ConfigDict(extra="forbid")

    codex_host_auth_available: bool
    codex_auth_imported: bool
    codex_auth_imported_at: datetime | None
    grok_host_auth_available: bool
    grok_auth_imported: bool
    grok_auth_imported_at: datetime | None
    google_adc_available: bool


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
    episode_needs_review: int
    episode_failed: int
    ai_requests_today: int
    series_ai_requests_today: int
    episode_ai_requests_today: int
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
        "Pending", "Resolved", "Unknown", "NotNumbered", "NeedsReview", "Failed"
    ]
    # 現在の正本（採用中のレーン）。
    source: Literal['Local', 'EPG', 'WebSearch', 'Manual', 'Migration', 'AI'] | None
    lookup_outcome: EpisodeLookupOutcome | None
    # AI レーン。
    proposed_season_number: int | None
    proposed_episode_number: schemas.RecordedEpisodeNumber | None
    confidence: float | None
    web_search_performed: bool
    citations: list[RecordedEpisodeAssignmentCitation]
    rationale_short: str | None
    # 手動レーン。
    manual_season_number: int | None
    manual_episode_number: schemas.RecordedEpisodeNumber | None
    manual_status: Literal['Resolved', 'Unknown', 'NotNumbered'] | None
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


class RecordedEpisodeAdoptAIAssignmentRequest(_RecordedEpisodeAssignmentRequestBase):
    """保存済み AI レーンの提案を正本として採用する判断。"""

    decision: Literal['AdoptAI']


RecordedEpisodeAssignmentRequest = Annotated[
    RecordedEpisodeExistingAssignmentRequest
    | RecordedEpisodeStructuredAssignmentRequest
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


def _parseOptionalAPIKey(request_body: dict[str, object]) -> str | None:
    """リクエストからAPIキーを取り除き、本文へ再露出しない形で検証する。"""

    api_key_value = request_body.pop("api_key", None)
    if api_key_value is None:
        return None
    if (
        not isinstance(api_key_value, str)
        or api_key_value.strip() == ""
        or len(api_key_value) > MAX_API_KEY_LENGTH
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="API key is invalid.",
            headers=NO_STORE_HEADERS,
        )
    return api_key_value.strip()


def _connectionErrorMessage(
    error_code: str,
    capability: Literal["CandidateSelection", "EpisodeLookup"],
) -> str:
    """内部エラーコードをキーや外部レスポンスを含まない表示文へ変換する。"""

    if error_code == "HTTP401":
        return "認証に失敗しました。API キーを確認してください。"
    if error_code == "HTTP404":
        endpoint_name = (
            "Responses / Web Search"
            if capability == "EpisodeLookup"
            else "Chat Completions"
        )
        return f"{endpoint_name} の URL またはモデル ID を確認してください。"
    if error_code.startswith("HTTP"):
        return f"OpenAI 互換 API がエラーを返しました。（{error_code}）"
    if error_code in {"Timeout", "NetworkError"}:
        return (
            "OpenAI 互換 API へ接続できませんでした。URL と稼働状態を確認してください。"
        )
    if error_code == "RedirectRejected":
        return "接続先からのリダイレクトは安全のため拒否しました。最終 URL を指定してください。"
    if capability == "EpisodeLookup":
        return "Responses API のWeb検索・構造化出力に対応していないか、結果を検証できませんでした。"
    return "応答が Chat Completions 互換形式ではないか、シリーズ生成結果を検証できませんでした。"


def _notRunEpisodeLookupConnectionChecks(
    message: str,
    *,
    permission_not_applicable: bool,
) -> EpisodeLookupConnectionChecks:
    """接続試験を開始できなかった場合の固定6項目を構築する。"""

    not_run = ConnectionTestCheck(status="NotRun", message=message)
    permission_policy = (
        ConnectionTestCheck(
            status="NotApplicable",
            message="OpenAI 互換 Responses API では ACP permission policy は対象外です。",
        )
        if permission_not_applicable
        else not_run
    )
    return EpisodeLookupConnectionChecks(
        backend_connection=not_run,
        web_search=not_run,
        source_url=not_run,
        strict_schema=not_run,
        timeout_cancel=not_run,
        permission_policy=permission_policy,
    )


def _connectionChecksResponse(
    checks: EpisodeLookupConnectionChecks | None,
) -> RecordedSeriesEpisodeLookupConnectionChecksResponse | None:
    """内部 dataclass を秘密情報のない API response へ変換する。"""

    if checks is None:
        return None

    def Convert(
        check: ConnectionTestCheck,
    ) -> RecordedSeriesConnectionTestCheckResponse:
        return RecordedSeriesConnectionTestCheckResponse(
            status=check.status,
            message=check.message,
        )

    return RecordedSeriesEpisodeLookupConnectionChecksResponse(
        backend_connection=Convert(checks.backend_connection),
        web_search=Convert(checks.web_search),
        source_url=Convert(checks.source_url),
        strict_schema=Convert(checks.strict_schema),
        timeout_cancel=Convert(checks.timeout_cancel),
        permission_policy=Convert(checks.permission_policy),
    )


async def ParseRecordedSeriesSettingsUpdate(
    request: Request,
) -> tuple[RecordedSeriesSettings, str | None]:
    """API キーを検証エラーに含めず、設定更新リクエストを解析する。

    Args:
        request: Web UI から送信された HTTP リクエスト。

    Returns:
        検証済み設定と、置き換える API キーの組。
        API キーが省略または null の場合は None。

    Raises:
        HTTPException: JSON、設定、または API キーが不正な場合。
    """

    # FastAPI 標準の request body 検証は、422 レスポンスの input に API キーを含めることがある。
    # そのため JSON を手動で解析し、最初に API キーを分離してから残りの設定を Pydantic で検証する。
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
    api_key_value = _parseOptionalAPIKey(settings_body)
    # 旧クライアントの廃止済み field は受理するが、保存値や runtime 分岐へ反映しない。
    settings_body.pop("ai_candidate_selection_enabled", None)

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
    return settings, api_key_value


@router.get(
    "/settings",
    summary="録画シリーズ判定設定取得 API",
    response_description="API キー本体を含まない録画シリーズ判定設定。",
    response_model=RecordedSeriesSettingsResponse,
)
async def RecordedSeriesSettingsAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> RecordedSeriesSettingsResponse:
    """録画シリーズ判定設定を API キー本体を公開せず取得する。

    Args:
        response: Cache-Control ヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        API キー本体を含まない現在の設定。
    """

    response.headers.update(NO_STORE_HEADERS)
    try:
        settings, api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()
        api_key_configured = api_key is not None
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
    return RecordedSeriesSettingsResponse(
        **settings.model_dump(),
        api_key_configured=api_key_configured,
    )


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
    """録画シリーズ判定設定と、指定された場合のみ API キーを更新する。

    Args:
        request: API キーを含み得る設定更新リクエスト。
        response: Cache-Control ヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        None
    """

    response.headers.update(NO_STORE_HEADERS)
    settings, api_key = await ParseRecordedSeriesSettingsUpdate(request)
    try:
        RecordedSeriesSettingsStore.saveSettings(settings, api_key=api_key)
    except (OSError, ValueError) as ex:
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
    # 話数側はAlwaysへの変更で既存提案を無課金昇格し、新規録画の保留だけを再評価する。
    await RecordedEpisodeAutomation.settingsUpdated()


@router.delete(
    "/settings/api-key",
    summary="録画シリーズ判定 API キー削除 API",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def RecordedSeriesAPIKeyDeleteAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
    url: Annotated[
        str | None,
        Query(
            max_length=2048,
            description="削除対象のベースURL。省略時は保存済み設定URL。",
        ),
    ] = None,
):
    """保存済みの OpenAI 互換 API キーを再定義可能な形で削除する。

    url パラメータを指定すると、その URL のキーだけを削除する。
    指定しない場合は現在の設定の api_base_url に対応するキーを削除する。

    Args:
        response: Cache-Control ヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。
        url: 削除対象の API ベース URL（省略可）。

    Returns:
        None
    """

    response.headers.update(NO_STORE_HEADERS)
    normalized_url = url
    if url is not None:
        try:
            # 削除対象 URL も通常設定と同等に正規化・検証する（認証情報・query 等を拒否）
            normalized_url = RecordedSeriesSettings.validateAPIBaseURL(url)
        except ValueError as ex:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid API base URL.",
                headers=NO_STORE_HEADERS,
            ) from ex
    try:
        RecordedSeriesSettingsStore.deleteAPIKey(url=normalized_url)
    except OSError as ex:
        logging.error(
            "[RecordedSeriesAPIKeyDeleteAPI] Failed to delete recorded series API key:",
            exc_info=ex,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete recorded series API key.",
            headers=NO_STORE_HEADERS,
        ) from ex
    except ValueError as ex:
        # 構造不正は store 側で全体 unlink 済み。ここへ来るのは URL 検証失敗など。
        logging.error(
            "[RecordedSeriesAPIKeyDeleteAPI] Failed to delete recorded series API key:",
            exc_info=ex,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Failed to delete recorded series API key.",
            headers=NO_STORE_HEADERS,
        ) from ex


def _KonomiTVBS4KACPCredentialStatusResponse() -> (
    KonomiTVBS4KACPCredentialStatusResponse
):
    """資格情報管理モジュールの状態を API response model へ変換する。

    Returns:
        KonomiTVBS4KACPCredentialStatusResponse: 認証内容を含まない現在状態。
    """

    credential_status = KonomiTVBS4KACPCredentials.getStatus()
    return KonomiTVBS4KACPCredentialStatusResponse(
        codex_host_auth_available=credential_status.codex_host_auth_available,
        codex_auth_imported=credential_status.codex_auth_imported,
        codex_auth_imported_at=credential_status.codex_auth_imported_at,
        grok_host_auth_available=credential_status.grok_host_auth_available,
        grok_auth_imported=credential_status.grok_auth_imported,
        grok_auth_imported_at=credential_status.grok_auth_imported_at,
        google_adc_available=credential_status.google_adc_available,
    )


def _KonomiTVBS4KACPCredentialHTTPException(
    error: KonomiTVBS4KACPCredentialError,
) -> HTTPException:
    """内部 path・JSON・例外詳細を公開しない固定 HTTP error へ変換する。

    Args:
        error: 資格情報管理モジュールの固定コード付きエラー。

    Returns:
        HTTPException: ``no-store`` を付与した無害なエラー。
    """

    if error.code == "HostAuthUnavailable":
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The host authentication file is not available.",
            headers=NO_STORE_HEADERS,
        )
    if error.code == "InvalidHostAuth":
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The host authentication file is invalid.",
            headers=NO_STORE_HEADERS,
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to update the imported authentication state.",
        headers=NO_STORE_HEADERS,
    )


@router.get(
    "/settings/acp-credentials",
    summary="KonomiTV-BS4K 録画シリーズ ACP 認証状態取得 API",
    response_model=KonomiTVBS4KACPCredentialStatusResponse,
)
async def KonomiTVBS4KACPCredentialStatusAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> KonomiTVBS4KACPCredentialStatusResponse:
    """管理者へ共有 ACP 資格情報の存在状態と取り込み日時だけを返す。

    Args:
        response: ``Cache-Control: no-store`` を設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        KonomiTVBS4KACPCredentialStatusResponse: token・JSON・hash を含まない状態。
    """

    response.headers.update(NO_STORE_HEADERS)
    return _KonomiTVBS4KACPCredentialStatusResponse()


@router.post(
    "/settings/acp-credentials/{provider}/import",
    summary="KonomiTV-BS4K 録画シリーズ ACP 認証取り込み API",
    response_model=KonomiTVBS4KACPCredentialStatusResponse,
)
async def KonomiTVBS4KACPCredentialImportAPI(
    provider: Annotated[
        KonomiTVBS4KACPImportProvider, Path(description="取り込む ACP provider。")
    ],
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> KonomiTVBS4KACPCredentialStatusResponse:
    """管理者の明示操作で固定 mount の auth.json だけを専用 profile へ取り込む。

    Args:
        provider: ``codex`` または ``grok``。
        response: ``Cache-Control: no-store`` を設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        KonomiTVBS4KACPCredentialStatusResponse: 更新後の安全な状態。

    Raises:
        HTTPException: host-auth が不正、または専用コピーを保存できない場合。
    """

    response.headers.update(NO_STORE_HEADERS)
    async with ACP_CREDENTIAL_OPERATION_LOCK:
        try:
            KonomiTVBS4KACPCredentials.importProviderAuth(provider)
        except KonomiTVBS4KACPCredentialError as ex:
            # 内容や OS 例外をログへ渡さず、provider と固定コードだけを記録する。
            logging.error(
                f"[KonomiTVBS4KACPCredentialImportAPI] Failed to import {provider} auth ({ex.code}).",
            )
            raise _KonomiTVBS4KACPCredentialHTTPException(ex) from ex
        invalidate_episode_lookup_capability_proof(
            backend_kind="AcpCodex" if provider == "codex" else "AcpGrok",
        )
    return _KonomiTVBS4KACPCredentialStatusResponse()


@router.delete(
    "/settings/acp-credentials/{provider}",
    summary="KonomiTV-BS4K 録画シリーズ ACP 認証削除 API",
    response_model=KonomiTVBS4KACPCredentialStatusResponse,
)
async def KonomiTVBS4KACPCredentialDeleteAPI(
    provider: Annotated[
        KonomiTVBS4KACPImportProvider, Path(description="削除する ACP provider。")
    ],
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> KonomiTVBS4KACPCredentialStatusResponse:
    """管理者の明示操作で KonomiTV-BS4K 専用コピーだけを削除する。

    Args:
        provider: ``codex`` または ``grok``。
        response: ``Cache-Control: no-store`` を設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        KonomiTVBS4KACPCredentialStatusResponse: 更新後の安全な状態。

    Raises:
        HTTPException: 専用コピーを削除できない場合。
    """

    response.headers.update(NO_STORE_HEADERS)
    async with ACP_CREDENTIAL_OPERATION_LOCK:
        try:
            KonomiTVBS4KACPCredentials.deleteProviderAuth(provider)
        except KonomiTVBS4KACPCredentialError as ex:
            logging.error(
                f"[KonomiTVBS4KACPCredentialDeleteAPI] Failed to delete {provider} auth ({ex.code}).",
            )
            raise _KonomiTVBS4KACPCredentialHTTPException(ex) from ex
        invalidate_episode_lookup_capability_proof(
            backend_kind="AcpCodex" if provider == "codex" else "AcpGrok",
        )
    return _KonomiTVBS4KACPCredentialStatusResponse()


@router.post(
    "/settings/test",
    summary="AI バックエンド接続試験 API",
    response_model=RecordedSeriesConnectionTestResponse,
)
async def RecordedSeriesConnectionTestAPI(
    request: Request,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> RecordedSeriesConnectionTestResponse:
    """保存済みバックエンド設定を使用して選択したAI機能を1回だけ試す。

    OpenAI 互換バックエンドでは、入力中のURL・モデル・任意キーを使用する。
    ACP バックエンドでは保存前のドラフト設定から一時 backend を構築する。
    """

    response.headers.update(NO_STORE_HEADERS)
    try:
        request_body = await request.json()
    except Exception as ex:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Connection test request must be a JSON object.",
            headers=NO_STORE_HEADERS,
        ) from ex
    if not isinstance(request_body, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Connection test request must be a JSON object.",
            headers=NO_STORE_HEADERS,
        )

    draft = dict(request_body)
    capability_value = draft.pop("capability", "CandidateSelection")
    if not isinstance(capability_value, str) or capability_value not in {
        "CandidateSelection",
        "EpisodeLookup",
    }:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Connection test capability is invalid.",
            headers=NO_STORE_HEADERS,
        )
    capability = cast(Literal["CandidateSelection", "EpisodeLookup"], capability_value)

    api_key = _parseOptionalAPIKey(draft)
    try:
        saved_settings = RecordedSeriesSettingsStore.getSettings()
    except (OSError, ValueError) as ex:
        logging.error(
            "[RecordedSeriesConnectionTestAPI] Failed to load settings:", exc_info=ex
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load recorded series settings.",
            headers=NO_STORE_HEADERS,
        ) from ex
    try:
        # 旧クライアントの OpenAI 部分 payload も維持しつつ、新 UI は全ドラフトを送れる。
        merged_settings = saved_settings.model_dump(mode="json")
        merged_settings.update(draft)
        validated_settings = RecordedSeriesSettings.model_validate(merged_settings)
    except ValidationError as ex:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Connection test settings are invalid.",
            headers=NO_STORE_HEADERS,
        ) from ex

    audit_model = get_audit_model(validated_settings)

    # OpenAI 互換だけ URL ごとの保存キーを解決する。ACP へ API キーを渡さない。
    effective_api_key = api_key
    preflight_result: ConnectionTestResult | None = None
    if (
        validated_settings.ai_backend == "OpenAICompatible"
        and effective_api_key is None
    ):
        try:
            effective_api_key = RecordedSeriesSettingsStore.getAPIKeyForURL(
                validated_settings.api_base_url,
            )
        except ValueError:
            # 秘密 map が構造不正のときは fail-closed し、外部通信を開始しない。
            logging.error(
                "[RecordedSeriesConnectionTestAPI] API key map is invalid; refusing connection test.",
            )
            message = "APIキー設定が不正なため接続テストを実行できません。"
            preflight_result = ConnectionTestResult(
                success=False,
                latency_ms=0,
                model=audit_model,
                message=message,
                checks=(
                    _notRunEpisodeLookupConnectionChecks(
                        message,
                        permission_not_applicable=True,
                    )
                    if capability == "EpisodeLookup"
                    else None
                ),
                error_code="APIKeyMapInvalid",
            )
        except OSError as ex:
            logging.error(
                "[RecordedSeriesConnectionTestAPI] Failed to resolve API key for connection test URL:",
                exc_info=ex,
            )
            message = "APIキーを読み取れないため接続テストを実行できません。"
            preflight_result = ConnectionTestResult(
                success=False,
                latency_ms=0,
                model=audit_model,
                message=message,
                checks=(
                    _notRunEpisodeLookupConnectionChecks(
                        message,
                        permission_not_applicable=True,
                    )
                    if capability == "EpisodeLookup"
                    else None
                ),
                error_code="APIKeyReadFailed",
            )
    tested_provider_fingerprint = (
        get_episode_lookup_provider_fingerprint(
            validated_settings,
            (
                effective_api_key
                if validated_settings.ai_backend == "OpenAICompatible"
                else None
            ),
        )
        if capability == "EpisodeLookup"
        else None
    )
    if preflight_result is not None:
        result = preflight_result
    else:
        try:
            # OpenAI / ACP とも、接続試験の正本 facade と immutable draft snapshot を使う。
            result = await test_connection(
                capability,
                settings=validated_settings,
                api_key=effective_api_key
                if validated_settings.ai_backend == "OpenAICompatible"
                else None,
            )
        except RecordedSeriesAIError as ex:
            message = _connectionErrorMessage(ex.code, capability)
            result = ConnectionTestResult(
                success=False,
                latency_ms=ex.latency_ms or 0,
                model=audit_model,
                message=message,
                checks=(
                    _notRunEpisodeLookupConnectionChecks(
                        message,
                        permission_not_applicable=(
                            validated_settings.ai_backend == "OpenAICompatible"
                        ),
                    )
                    if capability == "EpisodeLookup"
                    else None
                ),
                http_status=ex.http_status,
                error_code=ex.code,
            )
        except Exception:
            # CLI・provider 由来の例外には path や認証詳細が含まれ得るため公開しない。
            logging.error("[RecordedSeriesConnectionTestAPI] Connection test failed.")
            message = (
                "ACP 接続テストに失敗しました。設定と認証状態を確認してください。"
                if validated_settings.ai_backend != "OpenAICompatible"
                else "AI バックエンド接続テストに失敗しました。"
            )
            result = ConnectionTestResult(
                success=False,
                latency_ms=0,
                model=audit_model,
                message=message,
                checks=(
                    _notRunEpisodeLookupConnectionChecks(
                        message,
                        permission_not_applicable=(
                            validated_settings.ai_backend == "OpenAICompatible"
                        ),
                    )
                    if capability == "EpisodeLookup"
                    else None
                ),
                error_code="ConnectionTestFailed",
            )
    if (
        capability == "EpisodeLookup"
        and result.provider_fingerprint is not None
    ):
        # facade が operation lock 内で実際に試験した世代を正本にする。
        # Router の事前観測値は preflight / 例外時の失効対象にだけ使う。
        tested_provider_fingerprint = result.provider_fingerprint

    rejected_error_codes = {
        "ChoiceOutsideCandidateSet",
        "InvalidOutputSchema",
        "InvalidJSON",
        "InvalidJSONType",
        "InvalidModelOutput",
        "LowConfidence",
        "MissingWebSearchCall",
        "SearchNotRun",
    }
    if capability == "EpisodeLookup":
        # 成功 proof は監査レコードと一体で成立させる。監査保存中は旧 proof
        # も使わせず、DB 保存失敗時に未監査 proof だけが残らないようにする。
        assert tested_provider_fingerprint is not None
        invalidate_episode_lookup_capability_fingerprint(
            tested_provider_fingerprint,
        )
    audit_error_code = (
        None
        if result.success
        else result.error_code or "ConnectionTestFailed"
    )
    connection_test_audit = await RecordedSeriesAIRequest.create(
        resolution_id=None,
        purpose="ConnectionTest",
        status=(
            "Succeeded"
            if result.success
            else "Rejected"
            if audit_error_code in rejected_error_codes
            else "Failed"
        ),
        model=audit_model,
        candidate_ids=(
            ["episode-lookup"]
            if capability == "EpisodeLookup"
            else ["unresolved"]
        ),
        selected_choice_id=result.selected_choice_id,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        http_status=result.http_status,
        latency_ms=result.latency_ms,
        error_code=audit_error_code,
    )
    proof_recorded: bool | None = None
    if capability == "EpisodeLookup":
        assert tested_provider_fingerprint is not None
        proof_recorded = record_episode_lookup_capability_proof(
            validated_settings,
            (
                effective_api_key
                if validated_settings.ai_backend == "OpenAICompatible"
                else None
            ),
            result,
            tested_provider_fingerprint=tested_provider_fingerprint,
        )
        if result.success and proof_recorded is False:
            current_provider_fingerprint = (
                get_episode_lookup_provider_fingerprint(
                    validated_settings,
                    (
                        effective_api_key
                        if validated_settings.ai_backend == "OpenAICompatible"
                        else None
                    ),
                )
            )
            state_changed = (
                current_provider_fingerprint
                != tested_provider_fingerprint
            )
            result = replace(
                result,
                success=False,
                message=(
                    "接続試験中に AI 設定または認証状態が変更されました。もう一度試してください。"
                    if state_changed
                    else "話数 Web 検索に必要な能力をすべて確認できませんでした。"
                ),
                error_code=(
                    "ConnectionTestStateChanged"
                    if state_changed
                    else "EpisodeLookupCapabilityNotVerified"
                ),
            )
            # 監査保存後に provider / credential 世代が変わった場合も、API 応答と
            # 監査履歴を同じ失敗状態へそろえる。成功監査だけが残ると、能力証明が
            # 登録されていない実態と履歴表示が食い違う。
            connection_test_audit.status = 'Failed'
            connection_test_audit.error_code = result.error_code
            await connection_test_audit.save(
                update_fields=['status', 'error_code'],
            )
    return RecordedSeriesConnectionTestResponse(
        success=result.success,
        latency_ms=result.latency_ms,
        model=result.model,
        message=result.message,
        checks=_connectionChecksResponse(result.checks),
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
                source=resolution.source,
                lookup_outcome=resolution.lookup_outcome,
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
    season_number = (
        request.season_number if request.decision == "StructuredEpisode" else None
    )
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
