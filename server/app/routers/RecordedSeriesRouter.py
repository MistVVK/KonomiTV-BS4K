from __future__ import annotations

from datetime import date, datetime
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
from app.metadata.RecordedEpisodeAutomation import RecordedEpisodeAutomation
from app.metadata.RecordedEpisodeResolver import (
    RecordedEpisodeAssignmentStaleError,
    RecordedEpisodeCrossSeriesError,
    RecordedEpisodeInvalidNumberError,
    RecordedEpisodeProgramNotFoundError,
    RecordedEpisodeResolver,
    RecordedEpisodeSeriesNotAssignedError,
    RecordedEpisodeTargetNotFoundError,
)
from app.metadata.RecordedEpisodeSearch import (
    RecordedEpisodeProgramPrompt,
    SearchRecordedEpisodeNumber,
)
from app.metadata.RecordedSeriesCandidates import (
    RecordedSeriesAIError,
    RecordedSeriesProgramPrompt,
    SelectRecordedSeriesCandidate,
    SeriesChoiceCandidate,
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
    RecordedSeriesProviderKeyMismatchError,
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
    tags = ['Recorded Series'],
    prefix = '/api/recorded-series',
)

NO_STORE_HEADERS = {'Cache-Control': 'no-store'}
MAX_API_KEY_LENGTH = 8192
RECORDED_SERIES_MANAGEMENT_DEFAULT_PAGE_SIZE = 30
RECORDED_SERIES_MANAGEMENT_MAX_PAGE_SIZE = 100


class RecordedSeriesConnectionTestResponse(BaseModel):
    """秘密情報を含まないOpenAI互換API接続試験結果。"""

    model_config = ConfigDict(extra='forbid')

    success: bool
    latency_ms: int
    model: str
    message: str


class RecordedSeriesStatusResponse(BaseModel):
    """Web設定画面へ返す録画シリーズ判定の集計。"""

    model_config = ConfigDict(extra='forbid')

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

    model_config = ConfigDict(extra='forbid')

    recorded_program_id: int | None


class RecordedSeriesBackfillRequest(BaseModel):
    """既存録画の一括判定方法。"""

    model_config = ConfigDict(extra='forbid')

    force: bool = False


class RecordedSeriesAssignmentRequest(BaseModel):
    """管理者が録画1件へ確定させるシリーズ所属。"""

    model_config = ConfigDict(extra='forbid')

    decision: Literal['Series', 'NotSeries']
    series_id: Annotated[int | None, Field(gt=0)] = None
    series_title: Annotated[str | None, Field(max_length=255)] = None

    @model_validator(mode='after')
    def validateTarget(self) -> Self:
        """decisionに対して既存IDまたは新規タイトルの指定が一意であることを検証する。

        Returns:
            検証後のリクエスト自身。

        Raises:
            ValueError: Series指定の過不足、またはNotSeriesへの不要な対象指定がある場合。
        """

        if self.series_title is not None:
            self.series_title = self.series_title.strip()
            if self.series_title == '':
                raise ValueError('series_title must not be blank.')
        if self.decision == 'Series':
            if (self.series_id is None) == (self.series_title is None):
                raise ValueError('Series assignment requires exactly one of series_id or series_title.')
        elif self.series_id is not None or self.series_title is not None:
            raise ValueError('NotSeries assignment cannot include series_id or series_title.')
        return self


class RecordedSeriesManagementItem(BaseModel):
    """管理画面の一覧で使用する、録画をネストしない軽量なSeries情報。"""

    model_config = ConfigDict(extra='forbid')

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

    model_config = ConfigDict(extra='forbid')

    total: int
    page: int
    page_size: int
    items: list[RecordedSeriesManagementItem]


class RecordedSeriesManagementUpdateRequest(BaseModel):
    """管理者が変更できるSeries表示メタデータ。"""

    model_config = ConfigDict(extra='forbid')

    title: Annotated[str, Field(max_length=255)]
    description: Annotated[str, Field(max_length=10000)]
    expected_title: str
    expected_description: str

    @model_validator(mode='after')
    def validateMetadata(self) -> Self:
        """タイトルを正規化可能な非空文字列へ制限する。

        Returns:
            前後空白を除去したリクエスト自身。

        Raises:
            ValueError: タイトルが空白だけの場合。
        """

        self.title = self.title.strip()
        self.description = self.description.strip()
        if self.title == '':
            raise ValueError('title must not be blank.')
        return self


class RecordedEpisodeAssignmentListResponse(BaseModel):
    """Series内のEpisode候補と録画ごとの現在割当。"""

    model_config = ConfigDict(extra='forbid')

    series_id: int
    episodes: list[RecordedEpisodeAssignmentEpisode]
    programs: list[RecordedEpisodeAssignmentProgram]


class RecordedEpisodeAssignmentEpisode(BaseModel):
    """手動割当先として選択可能な構造化Episode。"""

    model_config = ConfigDict(extra='forbid')

    id: int
    season_number: int
    episode_number: schemas.RecordedEpisodeNumber


class RecordedEpisodeAssignmentProgram(BaseModel):
    """Series管理画面で話数を訂正できる再生可能録画。"""

    model_config = ConfigDict(extra='forbid')

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

    model_config = ConfigDict(extra='forbid')

    url: str
    title: str


class RecordedEpisodeAssignmentResolution(BaseModel):
    """手動判断時に表示する話数判定状態と根拠。"""

    model_config = ConfigDict(extra='forbid')

    status: Literal['Pending', 'Resolved', 'Unknown', 'NotNumbered', 'NeedsReview', 'Failed']
    source: Literal['Local', 'EPG', 'WebSearch', 'Manual', 'Migration'] | None
    proposed_season_number: int | None
    proposed_episode_number: schemas.RecordedEpisodeNumber | None
    confidence: float | None
    web_search_performed: bool
    citations: list[RecordedEpisodeAssignmentCitation]
    error_code: str | None


class _RecordedEpisodeAssignmentRequestBase(BaseModel):
    """全話数割当判断に共通する楽観ロック値。"""

    model_config = ConfigDict(extra='forbid')

    expected_series_id: Annotated[int, Field(gt=0)]
    expected_series_episode_id: Annotated[int | None, Field(gt=0)]


class RecordedEpisodeExistingAssignmentRequest(_RecordedEpisodeAssignmentRequestBase):
    """Series内の既存Episodeを選ぶ手動判断。"""

    decision: Literal['ExistingEpisode']
    episode_id: Annotated[int, Field(gt=0)]


class RecordedEpisodeStructuredAssignmentRequest(_RecordedEpisodeAssignmentRequestBase):
    """新しいシーズン・話数を入力する手動判断。"""

    decision: Literal['StructuredEpisode']
    season_number: Annotated[int, Field(ge=0, le=2_147_483_647)]
    episode_number: Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=3)]


class RecordedEpisodeUnknownAssignmentRequest(_RecordedEpisodeAssignmentRequestBase):
    """この録画の話数を不明として確定する手動判断。"""

    decision: Literal['Unknown']


RecordedEpisodeAssignmentRequest = Annotated[
    RecordedEpisodeExistingAssignmentRequest |
    RecordedEpisodeStructuredAssignmentRequest |
    RecordedEpisodeUnknownAssignmentRequest,
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
            recorded_video__status='Recorded',
        ).values(
            'series_id',
            'start_time',
        )
        # Tortoise ORMのreverse joinを使った複数集約はSQLite上で巨大な中間結果を
        # 作り得るため、最大100Series分のID・開始時刻だけを1回取得してメモリ上で集約する。
        for program_row in program_rows:
            program_series_id = program_row['series_id']
            start_time = program_row['start_time']
            summary = program_summaries.setdefault(
                program_series_id,
                _RecordedSeriesProgramSummary(
                    recorded_program_count=0,
                    first_recorded_at=start_time,
                    last_recorded_at=start_time,
                ),
            )
            summary['recorded_program_count'] += 1
            first_recorded_at = summary['first_recorded_at']
            last_recorded_at = summary['last_recorded_at']
            if start_time < first_recorded_at:
                summary['first_recorded_at'] = start_time
            if start_time > last_recorded_at:
                summary['last_recorded_at'] = start_time

    items: list[RecordedSeriesManagementItem] = []
    for series in series_list:
        summary = program_summaries.get(series.id)
        items.append(RecordedSeriesManagementItem(
            id=series.id,
            title=series.title,
            description=series.description,
            wikipedia_page_id=series.wikipedia_page_id,
            recorded_program_count=summary['recorded_program_count'] if summary is not None else 0,
            first_recorded_at=summary['first_recorded_at'] if summary is not None else None,
            last_recorded_at=summary['last_recorded_at'] if summary is not None else None,
            updated_at=series.updated_at,
        ))
    return items


def _parseOptionalAPIKey(request_body: dict[str, object]) -> str | None:
    """リクエストからAPIキーを取り除き、本文へ再露出しない形で検証する。"""

    api_key_value = request_body.pop('api_key', None)
    if api_key_value is None:
        return None
    if (
        not isinstance(api_key_value, str) or
        api_key_value.strip() == '' or
        len(api_key_value) > MAX_API_KEY_LENGTH
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='API key is invalid.',
            headers=NO_STORE_HEADERS,
        )
    return api_key_value.strip()


def _connectionErrorMessage(
    error_code: str,
    capability: Literal['CandidateSelection', 'EpisodeLookup'],
) -> str:
    """内部エラーコードをキーや外部レスポンスを含まない表示文へ変換する。"""

    if error_code == 'HTTP401':
        return '認証に失敗しました。API キーを確認してください。'
    if error_code == 'HTTP404':
        endpoint_name = 'Responses / Web Search' if capability == 'EpisodeLookup' else 'Chat Completions'
        return f'{endpoint_name} の URL またはモデル ID を確認してください。'
    if error_code.startswith('HTTP'):
        return f'OpenAI 互換 API がエラーを返しました。（{error_code}）'
    if error_code in {'Timeout', 'NetworkError'}:
        return 'OpenAI 互換 API へ接続できませんでした。URL と稼働状態を確認してください。'
    if error_code == 'RedirectRejected':
        return '接続先からのリダイレクトは安全のため拒否しました。最終 URL を指定してください。'
    if capability == 'EpisodeLookup':
        return 'Responses API のWeb検索・構造化出力に対応していないか、結果を検証できませんでした。'
    return '応答が Chat Completions 互換形式ではないか、候補選択結果を検証できませんでした。'


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
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Recorded series settings request must be a JSON object.',
            headers = NO_STORE_HEADERS,
        ) from ex
    if isinstance(request_body, dict) is False:
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Recorded series settings request must be a JSON object.',
            headers = NO_STORE_HEADERS,
        )

    settings_body = dict(request_body)
    api_key_value = _parseOptionalAPIKey(settings_body)

    try:
        settings = RecordedSeriesSettings.model_validate(settings_body)
    except ValidationError as ex:
        # Pydantic のエラーから input とドキュメント URL を除外し、リクエスト本文を返さない。
        sanitized_errors = [
            {
                'type': validation_error['type'],
                'loc': ['body', *validation_error['loc']],
                'msg': validation_error['msg'],
            }
            for validation_error in ex.errors(include_url=False, include_input=False)
        ]
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = sanitized_errors,
            headers = NO_STORE_HEADERS,
        ) from ex
    return settings, api_key_value


@router.get(
    '/settings',
    summary = '録画シリーズ判定設定取得 API',
    response_description = 'API キー本体を含まない録画シリーズ判定設定。',
    response_model = RecordedSeriesSettingsResponse,
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
        logging.error('[RecordedSeriesSettingsAPI] Failed to load recorded series settings:', exc_info=ex)
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail = 'Failed to load recorded series settings.',
            headers = NO_STORE_HEADERS,
        ) from ex
    return RecordedSeriesSettingsResponse(
        **settings.model_dump(),
        api_key_configured = api_key_configured,
    )


@router.put(
    '/settings',
    summary = '録画シリーズ判定設定更新 API',
    status_code = status.HTTP_204_NO_CONTENT,
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
    except RecordedSeriesProviderKeyMismatchError as ex:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Replace or delete the saved API key before changing the API base URL.',
            headers=NO_STORE_HEADERS,
        ) from ex
    except (OSError, ValueError) as ex:
        logging.error('[RecordedSeriesSettingsUpdateAPI] Failed to save recorded series settings:', exc_info=ex)
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail = 'Failed to save recorded series settings.',
            headers = NO_STORE_HEADERS,
        ) from ex
    # 起動時に設定破損などでPending回収できなかった場合も、設定修復直後に再試行する。
    await RecordedSeriesResolver.retryPendingRecovery()
    # 話数側はAlwaysへの変更で既存提案を無課金昇格し、新規録画の保留だけを再評価する。
    await RecordedEpisodeAutomation.settingsUpdated()


@router.delete(
    '/settings/api-key',
    summary = '録画シリーズ判定 API キー削除 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def RecordedSeriesAPIKeyDeleteAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """保存済みの OpenAI 互換 API キーを再定義可能な形で削除する。

    Args:
        response: Cache-Control ヘッダーを設定するレスポンス。
        _current_user: 管理者認証済みのユーザー。

    Returns:
        None
    """

    response.headers.update(NO_STORE_HEADERS)
    try:
        RecordedSeriesSettingsStore.deleteAPIKey()
    except OSError as ex:
        logging.error('[RecordedSeriesAPIKeyDeleteAPI] Failed to delete recorded series API key:', exc_info=ex)
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail = 'Failed to delete recorded series API key.',
            headers = NO_STORE_HEADERS,
        ) from ex


@router.post(
    '/settings/test',
    summary='OpenAI 互換 API 接続試験 API',
    response_model=RecordedSeriesConnectionTestResponse,
)
async def RecordedSeriesConnectionTestAPI(
    request: Request,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> RecordedSeriesConnectionTestResponse:
    """入力中のURL・モデル・任意キーを保存せず、選択したAI機能を1回だけ試す。"""

    response.headers.update(NO_STORE_HEADERS)
    try:
        request_body = await request.json()
    except Exception as ex:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Connection test request must be a JSON object.',
            headers=NO_STORE_HEADERS,
        ) from ex
    if not isinstance(request_body, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Connection test request must be a JSON object.',
            headers=NO_STORE_HEADERS,
        )

    draft = dict(request_body)
    api_key = _parseOptionalAPIKey(draft)
    capability_value = draft.pop('capability', 'CandidateSelection')
    if (
        not isinstance(capability_value, str) or
        capability_value not in {'CandidateSelection', 'EpisodeLookup'}
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Connection test capability is invalid.',
            headers=NO_STORE_HEADERS,
        )
    capability = cast(Literal['CandidateSelection', 'EpisodeLookup'], capability_value)
    if set(draft) != {'api_base_url', 'model'}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Connection test accepts only capability, api_base_url, model, and api_key.',
            headers=NO_STORE_HEADERS,
        )
    try:
        validated_settings = RecordedSeriesSettings(
            api_base_url=draft['api_base_url'],
            model=draft['model'],
        )
    except ValidationError as ex:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Connection test URL or model is invalid.',
            headers=NO_STORE_HEADERS,
        ) from ex

    try:
        saved_settings, saved_api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()
    except (OSError, ValueError) as ex:
        logging.error('[RecordedSeriesConnectionTestAPI] Failed to load recorded series settings:', exc_info=ex)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load recorded series settings.',
            headers=NO_STORE_HEADERS,
        ) from ex
    # 入力中URLが保存先と異なるときに保存済みキーを暗黙転送すると、別providerへ秘密が漏れる。
    # 明示入力キーはそのテストだけで使用し、保存済みキーは同一ベースURLの場合に限り再利用する。
    effective_api_key = api_key
    if effective_api_key is None and validated_settings.api_base_url == saved_settings.api_base_url:
        effective_api_key = saved_api_key
    try:
        if capability == 'CandidateSelection':
            candidates = [
                SeriesChoiceCandidate(
                    choice_id='unresolved',
                    kind='Unresolved',
                    title='Connection test',
                    description='Select this only candidate to prove Chat Completions compatibility.',
                ),
            ]
            program = RecordedSeriesProgramPrompt(
                title='KonomiTV OpenAI-compatible API connection test',
                description='This is a connection test, not recorded content.',
                genres=['ConnectionTest'],
                channel=None,
                start_date=date.today().isoformat(),
            )
            candidate_result = await SelectRecordedSeriesCandidate(
                api_base_url=validated_settings.api_base_url,
                api_key=effective_api_key,
                model=validated_settings.model,
                program=program,
                candidates=candidates,
                minimum_confidence=0.0,
            )
            result_model = candidate_result.model
            selected_choice_id = candidate_result.choice_id
            prompt_tokens = candidate_result.prompt_tokens
            completion_tokens = candidate_result.completion_tokens
            http_status = candidate_result.http_status
            latency_ms = candidate_result.latency_ms
            candidate_ids = ['unresolved']
            success_message = '接続と候補制約付き応答の検証に成功しました。'
        else:
            episode_result = await SearchRecordedEpisodeNumber(
                api_base_url=validated_settings.api_base_url,
                api_key=effective_api_key,
                model=validated_settings.model,
                program=RecordedEpisodeProgramPrompt(
                    series_title='KonomiTV connection test',
                    program_title='Episode lookup connection test',
                    subtitle=None,
                    description='This request only verifies Responses API Web Search support.',
                    detail='ConnectionTest: true',
                    channel=None,
                    broadcast_datetime=datetime.now().astimezone().isoformat(),
                ),
            )
            result_model = episode_result.model
            selected_choice_id = (
                f'S{episode_result.season_number}E{episode_result.episode_number}'
                if episode_result.numbered
                else 'not-numbered'
            )
            prompt_tokens = episode_result.prompt_tokens
            completion_tokens = episode_result.completion_tokens
            http_status = episode_result.http_status
            latency_ms = episode_result.latency_ms
            candidate_ids = ['episode-lookup']
            success_message = 'Responses API のWeb検索と構造化出力の検証に成功しました。'
        await RecordedSeriesAIRequest.create(
            resolution_id=None,
            purpose='ConnectionTest',
            status='Succeeded',
            model=result_model,
            candidate_ids=candidate_ids,
            selected_choice_id=selected_choice_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            http_status=http_status,
            latency_ms=latency_ms,
            error_code=None,
        )
        return RecordedSeriesConnectionTestResponse(
            success=True,
            latency_ms=latency_ms,
            model=result_model,
            message=success_message,
        )
    except RecordedSeriesAIError as ex:
        await RecordedSeriesAIRequest.create(
            resolution_id=None,
            purpose='ConnectionTest',
            status='Rejected' if ex.code in {
                'ChoiceOutsideCandidateSet',
                'InvalidOutputSchema',
                'InvalidJSON',
                'InvalidJSONType',
                'LowConfidence',
                'MissingWebSearchCall',
            } else 'Failed',
            model=validated_settings.model,
            candidate_ids=['episode-lookup'] if capability == 'EpisodeLookup' else ['unresolved'],
            selected_choice_id=None,
            prompt_tokens=None,
            completion_tokens=None,
            http_status=ex.http_status,
            latency_ms=ex.latency_ms,
            error_code=ex.code,
        )
        return RecordedSeriesConnectionTestResponse(
            success=False,
            latency_ms=ex.latency_ms or 0,
            model=validated_settings.model,
            message=_connectionErrorMessage(ex.code, capability),
        )


@router.get(
    '/series',
    summary='録画シリーズ管理一覧取得 API',
    response_model=RecordedSeriesManagementListResponse,
)
async def RecordedSeriesManagementListAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
    query: Annotated[str, Query(max_length=255, description='タイトルまたは説明の部分一致検索。')] = '',
    page: Annotated[int, Query(ge=1, description='1から始まるページ番号。')] = 1,
    page_size: Annotated[
        int,
        Query(
            ge=1,
            le=RECORDED_SERIES_MANAGEMENT_MAX_PAGE_SIZE,
            description='1ページに返すSeries数。',
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
    if normalized_query != '':
        series_query = series_query.filter(
            Q(title__icontains=normalized_query) |
            Q(description__icontains=normalized_query),
        )

    # 通常の /api/series は全録画をネストし、Recorded録画を持つSeriesだけを返す。
    # 管理画面では孤立Seriesも修正できるよう全Seriesを対象にし、録画集計だけを別Queryで取得する。
    total = await series_query.count()
    series_list = await series_query.order_by('-updated_at', '-id') \
        .offset((page - 1) * page_size) \
        .limit(page_size)
    items = await _buildRecordedSeriesManagementItems(series_list)
    return RecordedSeriesManagementListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=items,
    )


@router.get(
    '/series/{series_id}',
    summary='録画シリーズ管理情報取得 API',
    response_model=RecordedSeriesManagementItem,
)
async def RecordedSeriesManagementDetailAPI(
    series_id: Annotated[int, Path(gt=0, description='取得するSeries ID。')],
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> RecordedSeriesManagementItem:
    """競合後の再編集にも使える、管理画面向けSeries最新情報を返す。"""

    response.headers.update(NO_STORE_HEADERS)
    series = await Series.filter(id=series_id).first()
    if series is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Specified series_id was not found.',
            headers=NO_STORE_HEADERS,
        )
    return (await _buildRecordedSeriesManagementItems([series]))[0]


@router.put(
    '/series/{series_id}',
    summary='録画シリーズ管理情報更新 API',
    status_code=status.HTTP_204_NO_CONTENT,
)
async def RecordedSeriesManagementUpdateAPI(
    series_id: Annotated[int, Path(gt=0, description='更新するSeries ID。')],
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
            detail='Specified series_id was not found.',
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedSeriesTitleConflictError as ex:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Another series or recorded-series rule already uses the specified title.',
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedSeriesMetadataStaleError as ex:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Recorded series metadata was updated by another request.',
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedSeriesResolverBusyError as ex:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Recorded series resolution is currently busy.',
            headers={**NO_STORE_HEADERS, 'Retry-After': '5'},
        ) from ex
    except RecordedSeriesInvalidTitleError as ex:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Recorded series title is invalid.',
            headers=NO_STORE_HEADERS,
        ) from ex


@router.get(
    '/series/{series_id}/episode-assignments',
    summary='録画シリーズ話数割当一覧取得 API',
    response_model=RecordedEpisodeAssignmentListResponse,
)
async def RecordedEpisodeAssignmentListAPI(
    series_id: Annotated[int, Path(gt=0, description='取得するSeries ID。')],
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
            detail='Specified series_id was not found.',
            headers=NO_STORE_HEADERS,
        )

    episodes = await SeriesEpisode.filter(series_id=series_id).all()
    # SQLiteではDecimalFieldが文字列保存されるため、PythonのDecimalで確実に数値順へそろえる。
    episodes.sort(key=lambda episode: (episode.season_number, episode.episode_number, episode.id))
    programs = await RecordedProgram.filter(
        series_id=series_id,
        recorded_video__status='Recorded',
    ).prefetch_related('channel').order_by('start_time', 'id')
    program_ids = [program.id for program in programs]
    resolutions = (
        await RecordedEpisodeResolution.filter(recorded_program_id__in=program_ids).all()
        if len(program_ids) > 0
        else []
    )
    resolutions_by_program_id = {
        resolution.recorded_program_id: resolution
        for resolution in resolutions
    }

    response_programs: list[RecordedEpisodeAssignmentProgram] = []
    for program in programs:
        resolution = resolutions_by_program_id.get(program.id)
        resolution_response = None
        if resolution is not None:
            resolution_response = RecordedEpisodeAssignmentResolution(
                status=resolution.status,
                source=resolution.source,
                proposed_season_number=resolution.proposed_season_number,
                proposed_episode_number=resolution.proposed_episode_number,
                confidence=resolution.confidence,
                web_search_performed=resolution.web_search_performed,
                citations=[
                    RecordedEpisodeAssignmentCitation(
                        url=citation['url'],
                        title=citation['title'],
                    )
                    for citation in resolution.citations
                ],
                error_code=resolution.error_code,
            )
        response_programs.append(RecordedEpisodeAssignmentProgram(
            recorded_program_id=program.id,
            title=program.title,
            subtitle=program.subtitle,
            legacy_episode_number=program.episode_number,
            start_time=program.start_time,
            channel_id=program.channel_id,
            channel_name=program.channel.name if program.channel is not None else None,
            series_episode_id=program.series_episode_id,
            resolution=resolution_response,
        ))

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
    '/programs/{recorded_program_id}/episode-assignment',
    summary='録画シリーズ話数手動割当更新 API',
    status_code=status.HTTP_204_NO_CONTENT,
)
async def RecordedEpisodeAssignmentUpdateAPI(
    recorded_program_id: Annotated[int, Path(gt=0, description='録画番組の ID。')],
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
    episode_id = request.episode_id if request.decision == 'ExistingEpisode' else None
    season_number = request.season_number if request.decision == 'StructuredEpisode' else None
    episode_number = request.episode_number if request.decision == 'StructuredEpisode' else None
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
            detail='Specified recorded_program_id was not found.',
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedEpisodeTargetNotFoundError as ex:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Specified episode_id was not found.',
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedEpisodeSeriesNotAssignedError as ex:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='The recorded program is not assigned to a series.',
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedEpisodeAssignmentStaleError as ex:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='The series or episode assignment was updated by another request.',
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedEpisodeCrossSeriesError as ex:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='The specified episode belongs to another series.',
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedEpisodeInvalidNumberError as ex:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='The episode assignment is invalid.',
            headers=NO_STORE_HEADERS,
        ) from ex


@router.get(
    '/status',
    summary='録画シリーズ判定状況取得 API',
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
    return RecordedSeriesStatusResponse.model_validate({**series_status, **episode_status})


@router.post(
    '/backfill',
    summary='既存録画シリーズ一括判定 API',
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
    accepted = await RecordedSeriesResolver.startBackfill(trigger='Manual', force=request.force)
    return schemas.AnalysisTaskAccepted(
        execution_id=accepted.execution_id,
        reused=accepted.reused,
    )


@router.post(
    '/episodes/backfill',
    summary='既存録画話数一括判定 API',
    response_model=schemas.AnalysisTaskAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def RecordedEpisodeBackfillAPI(
    request: RecordedSeriesBackfillRequest,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> schemas.AnalysisTaskAccepted:
    """既存録画の話数を、明示操作1回につき当日のAI上限まで判定・再検索する。"""

    response.headers.update(NO_STORE_HEADERS)
    try:
        settings = RecordedSeriesSettingsStore.getSettings()
    except (OSError, ValueError) as ex:
        logging.error('[RecordedEpisodeBackfillAPI] Failed to load recorded series settings:', exc_info=ex)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load recorded series settings.',
            headers=NO_STORE_HEADERS,
        ) from ex
    if settings.ai_enabled is False or settings.ai_episode_number_search_enabled is False:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='AI episode number search must be enabled before starting episode backfill.',
            headers=NO_STORE_HEADERS,
        )
    accepted = await RecordedEpisodeAutomation.startBackfill(force=request.force)
    return schemas.AnalysisTaskAccepted(
        execution_id=accepted.execution_id,
        reused=accepted.reused,
    )


@router.get(
    '/programs/{recorded_program_id}/next',
    summary='録画シリーズ次番組取得 API',
    response_model=RecordedSeriesNextProgramResponse,
)
async def RecordedSeriesNextProgramAPI(
    recorded_program_id: Annotated[int, Path(gt=0, description='現在再生している録画番組の ID 。')],
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
        next_program_id = await RecordedSeriesResolver.getNextProgramID(recorded_program_id)
    except RecordedSeriesProgramNotFoundError as ex:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Specified recorded_program_id was not found.',
            headers=NO_STORE_HEADERS,
        ) from ex
    return RecordedSeriesNextProgramResponse(recorded_program_id=next_program_id)


@router.put(
    '/programs/{recorded_program_id}/assignment',
    summary='録画シリーズ手動割当更新 API',
    status_code=status.HTTP_204_NO_CONTENT,
)
async def RecordedSeriesAssignmentUpdateAPI(
    recorded_program_id: Annotated[int, Path(gt=0, description='録画番組の ID 。')],
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
        if request.decision == 'Series':
            await RecordedEpisodeAutomation.enqueue(recorded_program_id)
    except RecordedSeriesProgramNotFoundError as ex:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Specified recorded_program_id was not found.',
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedSeriesTargetNotFoundError as ex:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Specified series_id was not found.',
            headers=NO_STORE_HEADERS,
        ) from ex
    except RecordedSeriesChannelUnavailableError as ex:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='The recorded program has no channel and cannot be assigned to a series.',
            headers=NO_STORE_HEADERS,
        ) from ex
    except (RecordedSeriesInvalidTitleError, ValueError) as ex:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Recorded series assignment is invalid.',
            headers=NO_STORE_HEADERS,
        ) from ex
