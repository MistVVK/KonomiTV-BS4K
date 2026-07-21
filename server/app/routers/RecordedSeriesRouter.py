from __future__ import annotations

from datetime import date, datetime
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
from app.metadata.RecordedSeriesCandidates import (
    RecordedSeriesAIError,
    RecordedSeriesProgramPrompt,
    SelectRecordedSeriesCandidate,
    SeriesChoiceCandidate,
)
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
    ai_requests_today: int
    last_run_at: str | None
    is_running: bool


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


def _connectionErrorMessage(error_code: str) -> str:
    """内部エラーコードをキーや外部レスポンスを含まない表示文へ変換する。"""

    if error_code == 'HTTP401':
        return '認証に失敗しました。API キーを確認してください。'
    if error_code == 'HTTP404':
        return 'Chat Completions の URL またはモデル ID を確認してください。'
    if error_code.startswith('HTTP'):
        return f'OpenAI 互換 API がエラーを返しました。（{error_code}）'
    if error_code in {'Timeout', 'NetworkError'}:
        return 'OpenAI 互換 API へ接続できませんでした。URL と稼働状態を確認してください。'
    if error_code == 'RedirectRejected':
        return '接続先からのリダイレクトは安全のため拒否しました。最終 URL を指定してください。'
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
    """入力中のURL・モデル・任意キーを保存せず、最小候補選択を1回だけ試す。"""

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
    if set(draft) != {'api_base_url', 'model'}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Connection test accepts only api_base_url, model, and api_key.',
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
    try:
        result = await SelectRecordedSeriesCandidate(
            api_base_url=validated_settings.api_base_url,
            api_key=effective_api_key,
            model=validated_settings.model,
            program=program,
            candidates=candidates,
            minimum_confidence=0.0,
        )
        await RecordedSeriesAIRequest.create(
            resolution_id=None,
            purpose='ConnectionTest',
            status='Succeeded',
            model=result.model,
            candidate_ids=['unresolved'],
            selected_choice_id=result.choice_id,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            http_status=result.http_status,
            latency_ms=result.latency_ms,
            error_code=None,
        )
        return RecordedSeriesConnectionTestResponse(
            success=True,
            latency_ms=result.latency_ms,
            model=result.model,
            message='接続と候補制約付き応答の検証に成功しました。',
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
            } else 'Failed',
            model=validated_settings.model,
            candidate_ids=['unresolved'],
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
            message=_connectionErrorMessage(ex.code),
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
    return RecordedSeriesStatusResponse.model_validate(await RecordedSeriesResolver.getStatus())


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
