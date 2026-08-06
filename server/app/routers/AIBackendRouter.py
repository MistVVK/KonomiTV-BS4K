"""AI バックエンド（OpenCode service）管理 API。

含む: service CRUD / APIキー set・delete / OAuth 開始・callback・切断 /
OpenCode auth 注入と DELETE /auth/{id} 連動 / health・availability /
provider カタログ（OpenCode Web 相当の認証方式選択）/
draft 接続試験（Phase 2）/ 月次利用量 GET（Phase 3）。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app import logging
from app.metadata.ai.AIAPIUsageLedger import (
    AIAPIUsageLedger,
    CurrentYearMonth,
    SnapshotToDict,
)
from app.metadata.ai.AIBackendSettings import (
    AIBackendService,
    AIBackendServiceCreate,
    AIBackendServiceResponse,
    AIBackendServiceUpdate,
    AIBackendSettingsStore,
    IsRecordedSeriesReferencingService,
)
from app.metadata.ai.backends import ConnectionTestResult
from app.metadata.ai.opencode_backend import (
    BuildOpenCodeBackendFromDraft,
    BuildOpenCodeBackendFromServiceID,
)
from app.metadata.ai.opencode_client import (
    OpenCodeClient,
    OpenCodeClientError,
    OpenCodeUnavailableError,
)
from app.metadata.ai.opencode_serve import (
    IsOpenCodeAvailable,
    ProbeOpenCodeAvailability,
)
from app.metadata.RecordedSeriesCandidates import RecordedSeriesAIError
from app.models.User import User
from app.routers.UsersRouter import GetCurrentAdminUser


# Vertex ADC のみ「面倒な認証」として正式対応する provider ID。
# それ以外の ADC / 複数資格情報 / prompts 付き method は UI から除外する。
_VERTEX_PROVIDER_IDS = frozenset({
    'google-vertex',
    'google-vertex-anthropic',
})

# 単一 API キーでも「追加フィールド必須」など複雑すぎるため非対応とする provider。
# （API キーのみ・OAuth のみはベストエフォートで通す方針の例外リスト）
_COMPLEX_UNSUPPORTED_PROVIDER_IDS = frozenset({
    'amazon-bedrock',
    'azure',
    'cloudflare-ai-gateway',
    'cloudflare-workers-ai',
    'github-copilot',
    'gitlab',
    'snowflake-cortex',
})


# EpisodeLookup proof 無効化は Phase 4 まで no-op でもよいが、
# 鍵削除時に呼ぶフックを先に用意する。
try:
    from app.metadata.ai.recorded_series_ai import (
        invalidate_episode_lookup_capability_proof as _invalidate_episode_lookup_proof,
    )
except Exception:  # pragma: no cover - 起動時の循環・未実装耐性
    def _invalidate_episode_lookup_proof(*_args: Any, **_kwargs: Any) -> None:
        return None


router = APIRouter(
    tags=['AI Backend'],
    prefix='/api/ai-backends',
)

NO_STORE_HEADERS = {'Cache-Control': 'no-store'}
MAX_API_KEY_LENGTH = 8192


class AIBackendAPIKeyBody(BaseModel):
    """API キー設定ボディ。応答には絶対にエコーしない。"""

    model_config = ConfigDict(extra='forbid')

    api_key: Annotated[str, Field(min_length=1, max_length=MAX_API_KEY_LENGTH)]


class OpenCodeAvailabilityResponse(BaseModel):
    """製品用 opencode serve の availability。"""

    model_config = ConfigDict(extra='forbid')

    available: bool
    base_url: str
    host: str
    port: int
    version: str | None
    pinned_version: str
    pid: int | None
    workspace: str


class OAuthStartRequest(BaseModel):
    """OAuth 開始リクエスト。method は GET /provider/auth の index。"""

    model_config = ConfigDict(extra='forbid')

    # OpenCode の auth method 配列 index（0 始まり）。省略時は 0。
    method: Annotated[int, Field(ge=0, le=32)] = 0


class OAuthStartResponse(BaseModel):
    """OAuth 開始応答（URL / instructions。token は含めない）。"""

    model_config = ConfigDict(extra='forbid')

    provider_id: str
    # authorize 時に指定した method index（callback で同じ値を使う）。
    method: int
    # OpenCode が返す認可 URL（browser で開く）。
    url: str | None = None
    # 'auto' = browser 完了待ち / 'code' = ユーザーが code を入力。
    authorization_method: Annotated[Literal['auto', 'code'] | None, Field()] = None
    # 画面表示用の手順テキスト（device code 等を含むことがある）。
    instructions: str | None = None
    # 生の authorize 応答（デバッグ・将来拡張用。秘密は含めない想定）。
    authorize: dict[str, Any] = Field(default_factory=dict)


class OAuthCallbackRequest(BaseModel):
    """OAuth callback リクエスト。"""

    model_config = ConfigDict(extra='forbid')

    method: Annotated[int, Field(ge=0, le=32)] = 0
    # headless / device code 時にユーザーが貼り付ける認可コード。
    code: Annotated[str | None, Field(max_length=4096)] = None


class AIBackendProviderModelResponse(BaseModel):
    """service 追加 UI 用の provider モデル 1 件。"""

    model_config = ConfigDict(extra='forbid')

    model_id: str
    model_name: str
    # モデル能力（WebSearch 等）。無ければ空。
    capabilities: dict[str, Any] = Field(default_factory=dict)


class AIBackendProviderAuthMethodResponse(BaseModel):
    """OpenCode Web 相当の認証方式 1 件（provider 選択後に選ぶ）。"""

    model_config = ConfigDict(extra='forbid')

    # OpenCode /provider/auth の method index。Vertex など合成 method は null。
    method_index: int | None = None
    # OpenCode method type または vertex_adc。
    type: Annotated[Literal['api', 'oauth', 'vertex_adc'], Field()]
    # 画面表示ラベル（例: ChatGPT Pro/Plus (browser)）。
    label: str
    # KonomiTV service に保存する auth_mode。
    auth_mode: Annotated[Literal['ApiKey', 'OAuthSubscription', 'VertexAdc'], Field()]
    # 選択時の既定 billing_mode。
    billing_mode_default: Annotated[Literal['Metered', 'Subscription'], Field()]


class AIBackendProviderResponse(BaseModel):
    """service 追加 UI 用の provider 1 件（全カタログ / 接続済みの両方で共用）。"""

    model_config = ConfigDict(extra='forbid')

    provider_id: str
    # OpenCode の表示名（無ければ provider_id）。
    provider_name: str
    # 現在 OpenCode に auth 済みか。
    connected: bool = False
    # 対応可否。UnsupportedComplex は面倒な認証のため選択不可。
    support_kind: Annotated[Literal['Supported', 'UnsupportedComplex'], Field()] = 'Supported'
    # 非対応理由など（UI の補足）。
    support_note: str | None = None
    # OpenCode Web と同じく provider 選択後に並べる認証方式。
    auth_methods: list[AIBackendProviderAuthMethodResponse] = Field(default_factory=list)
    models: list[AIBackendProviderModelResponse] = Field(default_factory=list)
    # OpenCode 側の既定モデル ID。無ければ None。
    default_model_id: str | None = None


class AIBackendProviderListResponse(BaseModel):
    """provider カタログ（OpenCode Web 相当。未接続も含む）。"""

    model_config = ConfigDict(extra='forbid')

    providers: list[AIBackendProviderResponse]


class AIBackendUsageResponse(BaseModel):
    """月次利用量スナップショット（キー非返却・推定料金）。"""

    model_config = ConfigDict(extra='forbid')

    service_id: str
    year_month: str
    billing_mode: str
    settled_prompt_tokens: int
    settled_completion_tokens: int
    settled_total_tokens: int
    settled_estimated_cost_usd: str
    reserved_total_tokens: int
    reserved_estimated_cost_usd: str
    settled_request_count: int
    monthly_token_limit: int | None = None
    monthly_cost_limit_usd: str | None = None
    cost_limit_effective: bool
    token_limit_reached: bool
    cost_limit_reached: bool
    service_name_snapshot: str
    opencode_provider_id_snapshot: str
    opencode_model_id_snapshot: str
    # 設定から削除済みの service 履歴行（上限 enforce 対象外・表示用）
    service_deleted: bool = False


class AIBackendConnectionTestRequest(BaseModel):
    """OpenCode 接続試験リクエスト。

    service_id 指定時は保存済み service を使う。
    未指定時は draft フィールドから一時 service を構築する。
    api_key は応答に絶対に含めない。
    """

    model_config = ConfigDict(extra='forbid')

    capability: Annotated[
        Literal['CandidateSelection', 'EpisodeLookup'],
        Field(),
    ] = 'CandidateSelection'
    service_id: Annotated[str | None, Field(min_length=36, max_length=36)] = None
    # draft（service_id 無し）用
    service_name: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    opencode_provider_id: Annotated[str | None, Field(min_length=1, max_length=64)] = None
    opencode_model_id: Annotated[str | None, Field(min_length=1, max_length=255)] = None
    auth_mode: Annotated[
        Literal['ApiKey', 'OAuthSubscription', 'VertexAdc', 'NoneLocal'] | None,
        Field(),
    ] = None
    billing_mode: Annotated[
        Literal['Metered', 'Subscription', 'Local'] | None,
        Field(),
    ] = None
    api_base_url: Annotated[str | None, Field(max_length=2048)] = None
    google_cloud_project: Annotated[str | None, Field(max_length=255)] = None
    google_cloud_location: Annotated[str | None, Field(max_length=255)] = None
    api_key: Annotated[str | None, Field(min_length=1, max_length=MAX_API_KEY_LENGTH)] = None


class AIBackendConnectionTestResponse(BaseModel):
    """接続試験結果（キー非返却）。"""

    model_config = ConfigDict(extra='forbid')

    success: bool
    latency_ms: int
    model: str
    message: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    http_status: int | None = None
    error_code: str | None = None


def _httpErrorFromOpenCode(error: OpenCodeClientError) -> HTTPException:
    """OpenCodeClientError を HTTPException へ写像する。"""

    if isinstance(error, OpenCodeUnavailableError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='OpenCode serve is unavailable.',
            headers=NO_STORE_HEADERS,
        )
    code = error.status_code or status.HTTP_502_BAD_GATEWAY
    if code < 400 or code > 599:
        code = status.HTTP_502_BAD_GATEWAY
    return HTTPException(
        status_code=code,
        detail=str(error),
        headers=NO_STORE_HEADERS,
    )


def _ExtractProviderModels(
    raw_models: object,
) -> tuple[list[AIBackendProviderModelResponse], str | None]:
    """OpenCode provider.models を API 応答形へ整形する。

    Args:
        raw_models: dict[model_id, ModelInfo] または不正値。

    Returns:
        (model 一覧, default_model_id)。
    """

    model_list: list[AIBackendProviderModelResponse] = []
    default_model_id: str | None = None
    if not isinstance(raw_models, dict):
        return model_list, default_model_id
    typed_models: dict[Any, Any] = raw_models
    for model_id, model_info in typed_models.items():
        if not isinstance(model_id, str) or model_id.strip() == '':
            continue
        model_name = model_id
        capabilities: dict[str, Any] = {}
        if isinstance(model_info, dict):
            name = model_info.get('name')
            if isinstance(name, str) and name.strip() != '':
                model_name = name.strip()
            caps = model_info.get('capabilities')
            if isinstance(caps, dict):
                capabilities = caps
            if default_model_id is None and model_info.get('default') is True:
                default_model_id = model_id
        model_list.append(AIBackendProviderModelResponse(
            model_id=model_id,
            model_name=model_name,
            capabilities=capabilities,
        ))
    return model_list, default_model_id


def _IsComplexEnvAuth(env_names: list[str]) -> bool:
    """複数資格情報・クラウド固有 env など「面倒な認証」かを判定する。

    Vertex 以外の ADC / AWS 多キー / Azure resource 名必須などを弾く。
    単一の API_KEY 系 env だけなら False（ベストエフォート API キー扱い）。

    Args:
        env_names: provider.env の文字列リスト。

    Returns:
        面倒な認証なら True。
    """

    if len(env_names) == 0:
        return False
    upper = [name.upper() for name in env_names]
    # AWS 多キー・Bedrock 系
    if any(name.startswith('AWS_') for name in upper):
        return True
    # Azure は resource name + key が必要
    if any(name.startswith('AZURE_') for name in upper):
        return True
    # Vertex 以外の ADC ファイル認証
    if 'GOOGLE_APPLICATION_CREDENTIALS' in upper:
        return True
    # 3 本以上の env は追加フィールド必須とみなす
    if len(env_names) >= 3:
        return True
    return False


def _BuildAuthMethodsForProvider(
    provider_id: str,
    *,
    env_names: list[str],
    raw_auth_methods: list[dict[str, Any]] | None,
) -> tuple[list[AIBackendProviderAuthMethodResponse], str, str | None]:
    """provider に出す認証方式を OpenCode Web 相当に組み立てる。

    方針:
    - API キーのみ: ベストエフォートで出す
    - OAuth: ベストエフォートで出す（prompts 付き method は除外）
    - Vertex 系: VertexAdc のみ（ADC は Docker env 前提）
    - それ以外の面倒な認証: UnsupportedComplex

    Args:
        provider_id: OpenCode provider ID。
        env_names: provider.env。
        raw_auth_methods: GET /provider/auth の当該 provider 配列。

    Returns:
        (auth_methods, support_kind, support_note)。
    """

    # Vertex は ADC のみ正式対応（他の面倒な認証は一切出さない）。
    if provider_id in _VERTEX_PROVIDER_IDS:
        return (
            [
                AIBackendProviderAuthMethodResponse(
                    method_index=None,
                    type='vertex_adc',
                    label='Vertex AI (Application Default Credentials)',
                    auth_mode='VertexAdc',
                    billing_mode_default='Metered',
                ),
            ],
            'Supported',
            None,
        )

    if provider_id in _COMPLEX_UNSUPPORTED_PROVIDER_IDS:
        return (
            [],
            'UnsupportedComplex',
            '複数資格情報や追加フィールドが必要な認証のため未対応です（Vertex AI のみ例外対応）。',
        )

    methods: list[AIBackendProviderAuthMethodResponse] = []
    skipped_prompt_methods = 0
    if raw_auth_methods:
        for index, raw in enumerate(raw_auth_methods):
            method_type = raw.get('type')
            label_raw = raw.get('label')
            label = label_raw.strip() if isinstance(label_raw, str) and label_raw.strip() else ''
            # prompts 付き（Enterprise URL 等）は面倒な認証として method 単位で除外。
            prompts = raw.get('prompts')
            if isinstance(prompts, list) and len(prompts) > 0:
                skipped_prompt_methods += 1
                continue
            if method_type == 'api':
                methods.append(AIBackendProviderAuthMethodResponse(
                    method_index=index,
                    type='api',
                    label=label or 'Manually enter API Key',
                    auth_mode='ApiKey',
                    billing_mode_default='Metered',
                ))
            elif method_type == 'oauth':
                methods.append(AIBackendProviderAuthMethodResponse(
                    method_index=index,
                    type='oauth',
                    label=label or 'OAuth',
                    auth_mode='OAuthSubscription',
                    billing_mode_default='Subscription',
                ))

    if methods:
        note = None
        if skipped_prompt_methods > 0:
            note = (
                f'{skipped_prompt_methods} 件の追加入力付き認証方式は未対応のため非表示です。'
            )
        return methods, 'Supported', note

    # /provider/auth に載らない provider は env から API キーを推定（ベストエフォート）。
    if _IsComplexEnvAuth(env_names):
        return (
            [],
            'UnsupportedComplex',
            '複数資格情報やクラウド固有の認証が必要なため未対応です（Vertex AI のみ例外対応）。',
        )

    # 単一キー想定。env が空でも API キー入力をベストエフォートで出す。
    return (
        [
            AIBackendProviderAuthMethodResponse(
                method_index=None,
                type='api',
                label='Manually enter API Key',
                auth_mode='ApiKey',
                billing_mode_default='Metered',
            ),
        ],
        'Supported',
        'OpenCode の認証定義が無い provider です。API キー接続はベストエフォートです。',
    )


def _BuildProviderCatalog(
    catalog_payload: dict[str, Any],
    auth_methods_by_provider: dict[str, list[dict[str, Any]]],
) -> list[AIBackendProviderResponse]:
    """GET /provider + /provider/auth から UI 用カタログを組み立てる。

    Args:
        catalog_payload: OpenCode GET /provider の JSON。
        auth_methods_by_provider: GET /provider/auth の整形結果。

    Returns:
        provider 応答のリスト（provider_name でソート）。
    """

    raw_all_value = catalog_payload.get('all')
    raw_all: list[Any] = raw_all_value if isinstance(raw_all_value, list) else []
    connected_raw = catalog_payload.get('connected')
    connected_ids: set[str] = set()
    if isinstance(connected_raw, list):
        for item in connected_raw:
            if isinstance(item, str) and item.strip() != '':
                connected_ids.add(item.strip())
            elif isinstance(item, dict):
                item_id = item.get('id')
                if isinstance(item_id, str) and item_id.strip() != '':
                    connected_ids.add(item_id.strip())

    providers: list[AIBackendProviderResponse] = []
    for raw in raw_all:
        if not isinstance(raw, dict):
            continue
        provider_id = raw.get('id')
        if not isinstance(provider_id, str) or provider_id.strip() == '':
            continue
        provider_id = provider_id.strip()
        name_raw = raw.get('name')
        provider_name = (
            name_raw.strip()
            if isinstance(name_raw, str) and name_raw.strip() != ''
            else provider_id
        )
        env_raw = raw.get('env')
        env_names: list[str] = []
        if isinstance(env_raw, list):
            env_names = [
                item.strip() for item in env_raw
                if isinstance(item, str) and item.strip() != ''
            ]
        models, default_model_id = _ExtractProviderModels(raw.get('models'))
        auth_methods, support_kind, support_note = _BuildAuthMethodsForProvider(
            provider_id,
            env_names=env_names,
            raw_auth_methods=auth_methods_by_provider.get(provider_id),
        )
        providers.append(AIBackendProviderResponse(
            provider_id=provider_id,
            provider_name=provider_name,
            connected=provider_id in connected_ids,
            support_kind=support_kind,  # type: ignore[arg-type]
            support_note=support_note,
            auth_methods=auth_methods,
            models=models,
            default_model_id=default_model_id,
        ))

    providers.sort(key=lambda item: (item.provider_name.lower(), item.provider_id))
    return providers


async def _removeOpenCodeAuthIfUnshared(provider_id: str, *, excluding_service_id: str | None) -> None:
    """他 service が同一 provider を使っていなければ OpenCode auth を削除する。"""

    remaining = AIBackendSettingsStore.countServicesUsingProvider(
        provider_id,
        excluding_service_id=excluding_service_id,
    )
    if remaining > 0:
        logging.info(
            f'[AIBackend] Keep OpenCode auth for provider={provider_id} '
            f'(still referenced by {remaining} service(s)).',
        )
        return
    client = OpenCodeClient()
    try:
        await client.deleteAuth(provider_id)
    except OpenCodeUnavailableError:
        # serve 停止中は secrets 側は消済み。再試行可能な警告を残す。
        logging.warning(
            f'[AIBackend] OpenCode unavailable while deleting auth for provider={provider_id}.',
        )
        raise
    except OpenCodeClientError as error:
        logging.warning(
            f'[AIBackend] Failed to delete OpenCode auth for provider={provider_id}: {error}',
        )
        raise


@router.get(
    '/health',
    summary='OpenCode serve availability API',
    response_model=OpenCodeAvailabilityResponse,
)
async def OpenCodeHealthAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> OpenCodeAvailabilityResponse:
    """製品用 opencode serve の availability を返す。"""

    response.headers.update(NO_STORE_HEADERS)
    snapshot = ProbeOpenCodeAvailability()
    return OpenCodeAvailabilityResponse.model_validate(snapshot)


@router.get(
    '/providers',
    summary='AI バックエンド provider カタログ API',
    response_model=AIBackendProviderListResponse,
)
async def AIBackendProviderListAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> AIBackendProviderListResponse:
    """service 追加 UI 用に、OpenCode Web 相当の provider カタログを返す。

    GET /provider（全カタログ）と GET /provider/auth（認証方式）を合成する。
    - API キーのみ: ベストエフォートで選択可
    - OAuth: ベストエフォートで選択可（prompts 付き method は除外）
    - Vertex AI: ADC のみ正式対応
    - それ以外の面倒な認証（Azure/Bedrock/GitHub Enterprise 等）: UnsupportedComplex
    """

    response.headers.update(NO_STORE_HEADERS)
    client = OpenCodeClient()
    try:
        catalog_payload = await client.listAllProviders()
        auth_methods = await client.listProviderAuthMethods()
    except OpenCodeClientError as error:
        raise _httpErrorFromOpenCode(error) from error

    providers = _BuildProviderCatalog(catalog_payload, auth_methods)
    return AIBackendProviderListResponse(providers=providers)


@router.get(
    '/usage',
    summary='AI バックエンド月次利用量一覧 API',
    response_model=list[AIBackendUsageResponse],
)
async def AIBackendUsageListAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
    year_month: Annotated[str | None, Query(pattern=r'^\d{4}-\d{2}$')] = None,
) -> list[AIBackendUsageResponse]:
    """登録済み + 削除済み履歴の指定月（省略時は Asia/Tokyo 当月）利用量を返す。"""

    response.headers.update(NO_STORE_HEADERS)
    month = year_month or CurrentYearMonth()
    try:
        services = AIBackendSettingsStore.listServices()
        snapshots = await AIAPIUsageLedger.ListMonthSnapshots(
            services,
            year_month=month,
            include_deleted=True,
        )
        return [
            AIBackendUsageResponse.model_validate(SnapshotToDict(item))
            for item in snapshots
        ]
    except (OSError, ValueError) as error:
        logging.error('[AIBackendUsageListAPI] Failed to load usage:', exc_info=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load AI backend usage.',
            headers=NO_STORE_HEADERS,
        ) from error


@router.get(
    '/{service_id}/usage',
    summary='AI バックエンド月次利用量 API',
    response_model=AIBackendUsageResponse,
)
async def AIBackendUsageGetAPI(
    service_id: Annotated[str, Path(min_length=36, max_length=36)],
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
    year_month: Annotated[str | None, Query(pattern=r'^\d{4}-\d{2}$')] = None,
) -> AIBackendUsageResponse:
    """1 service の月次利用量を返す（削除済み履歴も snapshot で返す）。"""

    response.headers.update(NO_STORE_HEADERS)
    month = year_month or CurrentYearMonth()
    service = AIBackendSettingsStore.getService(service_id)
    if service is not None:
        snapshot = await AIAPIUsageLedger.GetMonthSnapshot(service, year_month=month)
        return AIBackendUsageResponse.model_validate(SnapshotToDict(snapshot))
    # 設定から消えた service でも台帳履歴があれば返す。
    deleted = await AIAPIUsageLedger.GetDeletedMonthSnapshot(
        service_id,
        year_month=month,
    )
    if deleted is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='AI backend service not found.',
            headers=NO_STORE_HEADERS,
        )
    return AIBackendUsageResponse.model_validate(SnapshotToDict(deleted))


@router.get(
    '',
    summary='AI バックエンド service 一覧 API',
    response_model=list[AIBackendServiceResponse],
)
async def AIBackendServiceListAPI(
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> list[AIBackendServiceResponse]:
    """登録済み service 一覧を返す（キー非返却）。"""

    response.headers.update(NO_STORE_HEADERS)
    try:
        services = AIBackendSettingsStore.listServices()
        return [AIBackendSettingsStore.toResponse(item) for item in services]
    except (OSError, ValueError) as error:
        logging.error('[AIBackendServiceListAPI] Failed to load services:', exc_info=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load AI backend services.',
            headers=NO_STORE_HEADERS,
        ) from error


@router.post(
    '',
    summary='AI バックエンド service 作成 API',
    response_model=AIBackendServiceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def AIBackendServiceCreateAPI(
    body: AIBackendServiceCreate,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> AIBackendServiceResponse:
    """service を新規作成する。"""

    response.headers.update(NO_STORE_HEADERS)
    try:
        service = AIBackendSettingsStore.createService(body)
        return AIBackendSettingsStore.toResponse(service)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
            headers=NO_STORE_HEADERS,
        ) from error
    except OSError as error:
        logging.error('[AIBackendServiceCreateAPI] Failed to create service:', exc_info=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to create AI backend service.',
            headers=NO_STORE_HEADERS,
        ) from error


@router.get(
    '/{service_id}',
    summary='AI バックエンド service 取得 API',
    response_model=AIBackendServiceResponse,
)
async def AIBackendServiceGetAPI(
    service_id: Annotated[str, Path(min_length=36, max_length=36)],
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> AIBackendServiceResponse:
    """1 件取得する。"""

    response.headers.update(NO_STORE_HEADERS)
    try:
        service = AIBackendSettingsStore.getService(service_id)
    except (OSError, ValueError) as error:
        logging.error('[AIBackendServiceGetAPI] Failed to load service:', exc_info=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load AI backend service.',
            headers=NO_STORE_HEADERS,
        ) from error
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='AI backend service not found.',
            headers=NO_STORE_HEADERS,
        )
    return AIBackendSettingsStore.toResponse(service)


@router.put(
    '/{service_id}',
    summary='AI バックエンド service 更新 API',
    response_model=AIBackendServiceResponse,
)
async def AIBackendServiceUpdateAPI(
    service_id: Annotated[str, Path(min_length=36, max_length=36)],
    body: AIBackendServiceUpdate,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> AIBackendServiceResponse:
    """service 定義を更新する（キーは別 API）。"""

    response.headers.update(NO_STORE_HEADERS)
    try:
        previous = AIBackendSettingsStore.getService(service_id)
        if previous is None:
            raise KeyError(service_id)
        service = AIBackendSettingsStore.updateService(service_id, body)
        # provider が変わった場合、旧 provider の auth を参照カウント付きで掃除
        if previous.opencode_provider_id != service.opencode_provider_id:
            try:
                await _removeOpenCodeAuthIfUnshared(
                    previous.opencode_provider_id,
                    excluding_service_id=service.service_id,
                )
            except OpenCodeClientError as error:
                # settings は更新済み。auth 掃除失敗は警告に留め再試行可能とする。
                logging.warning(
                    f'[AIBackendServiceUpdateAPI] Provider auth cleanup failed: {error}',
                )
        return AIBackendSettingsStore.toResponse(service)
    except KeyError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='AI backend service not found.',
            headers=NO_STORE_HEADERS,
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
            headers=NO_STORE_HEADERS,
        ) from error
    except OSError as error:
        logging.error('[AIBackendServiceUpdateAPI] Failed to update service:', exc_info=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to update AI backend service.',
            headers=NO_STORE_HEADERS,
        ) from error


@router.delete(
    '/{service_id}',
    summary='AI バックエンド service 削除 API',
    status_code=status.HTTP_204_NO_CONTENT,
    # from __future__ import annotations 下では -> None が文字列化され、FastAPI が
    # response_model を NoneType と推論して「204 must not have a response body」で
    # import 自体が失敗する。204 では response_model=None を明示して推論を止める。
    response_model=None,
)
async def AIBackendServiceDeleteAPI(
    service_id: Annotated[str, Path(min_length=36, max_length=36)],
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> None:
    """service を削除する。録画シリーズ参照中は 409。"""

    response.headers.update(NO_STORE_HEADERS)
    if IsRecordedSeriesReferencingService(service_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='AI backend service is referenced by recorded-series settings.',
            headers=NO_STORE_HEADERS,
        )
    try:
        removed = AIBackendSettingsStore.deleteService(service_id)
    except KeyError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='AI backend service not found.',
            headers=NO_STORE_HEADERS,
        ) from error
    except (OSError, ValueError) as error:
        logging.error('[AIBackendServiceDeleteAPI] Failed to delete service:', exc_info=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete AI backend service.',
            headers=NO_STORE_HEADERS,
        ) from error

    try:
        _invalidate_episode_lookup_proof(service_id=removed.service_id)
    except TypeError:
        try:
            _invalidate_episode_lookup_proof()
        except Exception:
            pass
    except Exception as error:
        logging.warning(f'[AIBackendServiceDeleteAPI] Proof invalidation failed: {error}')

    # 順序: KonomiTV secrets/settings 更新済み → OpenCode auth 除去
    try:
        await _removeOpenCodeAuthIfUnshared(
            removed.opencode_provider_id,
            excluding_service_id=removed.service_id,
        )
    except OpenCodeClientError as error:
        logging.warning(
            f'[AIBackendServiceDeleteAPI] OpenCode auth removal failed after local delete: {error}',
        )
        # ローカルは削除済み。再試行可能な状態として 204 を返す（幽霊 auth は次回削除で掃除）。


@router.put(
    '/{service_id}/api-key',
    summary='AI バックエンド API キー設定 API',
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def AIBackendAPIKeySetAPI(
    service_id: Annotated[str, Path(min_length=36, max_length=36)],
    body: AIBackendAPIKeyBody,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> None:
    """API キーを secrets に保存し、OpenCode auth へ注入する。"""

    response.headers.update(NO_STORE_HEADERS)
    service = AIBackendSettingsStore.getService(service_id)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='AI backend service not found.',
            headers=NO_STORE_HEADERS,
        )
    if service.auth_mode != 'ApiKey':
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='API key can only be set when auth_mode is ApiKey.',
            headers=NO_STORE_HEADERS,
        )
    try:
        AIBackendSettingsStore.setAPIKey(service.service_id, body.api_key)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='API key is invalid.',
            headers=NO_STORE_HEADERS,
        ) from error
    except OSError as error:
        logging.error('[AIBackendAPIKeySetAPI] Failed to store API key:', exc_info=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to store API key.',
            headers=NO_STORE_HEADERS,
        ) from error

    client = OpenCodeClient()
    try:
        # キー本体はログに出さない
        await client.putApiKey(service.opencode_provider_id, body.api_key)
    except OpenCodeClientError as error:
        raise _httpErrorFromOpenCode(error) from error

    try:
        _invalidate_episode_lookup_proof(service_id=service.service_id)
    except TypeError:
        pass
    except Exception:
        pass


@router.delete(
    '/{service_id}/api-key',
    summary='AI バックエンド API キー削除 API',
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def AIBackendAPIKeyDeleteAPI(
    service_id: Annotated[str, Path(min_length=36, max_length=36)],
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> None:
    """API キーを secrets から消し、共有が無ければ OpenCode auth も除去する。"""

    response.headers.update(NO_STORE_HEADERS)
    service = AIBackendSettingsStore.getService(service_id)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='AI backend service not found.',
            headers=NO_STORE_HEADERS,
        )
    try:
        AIBackendSettingsStore.deleteAPIKey(service.service_id)
    except (OSError, ValueError) as error:
        logging.error('[AIBackendAPIKeyDeleteAPI] Failed to delete API key:', exc_info=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete API key.',
            headers=NO_STORE_HEADERS,
        ) from error

    try:
        _invalidate_episode_lookup_proof(service_id=service.service_id)
    except TypeError:
        pass
    except Exception:
        pass

    try:
        await _removeOpenCodeAuthIfUnshared(
            service.opencode_provider_id,
            # 自分自身は secrets からキーを消した直後なので、参照カウントから除外する。
            # これにより同一 provider の他 service がいなければ OpenCode auth も消え、
            # 幽霊認証を残さない。
            excluding_service_id=service.service_id,
        )
    except OpenCodeClientError as error:
        logging.warning(
            f'[AIBackendAPIKeyDeleteAPI] OpenCode auth removal failed: {error}',
        )
        # secrets は削除済み。OpenCode 側は再試行可能。


@router.post(
    '/{service_id}/oauth/start',
    summary='AI バックエンド OAuth 開始 API',
    response_model=OAuthStartResponse,
)
async def AIBackendOAuthStartAPI(
    service_id: Annotated[str, Path(min_length=36, max_length=36)],
    body: OAuthStartRequest,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> OAuthStartResponse:
    """OAuth 認可を開始し、browser 用 URL と手順を返す。

    OpenCode Web と同様に method index を渡し、返却 URL をクライアントが開く。
    接続済みフラグの更新は callback API 側で行う。
    """

    response.headers.update(NO_STORE_HEADERS)
    service = AIBackendSettingsStore.getService(service_id)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='AI backend service not found.',
            headers=NO_STORE_HEADERS,
        )
    if service.auth_mode != 'OAuthSubscription':
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='OAuth start requires auth_mode=OAuthSubscription.',
            headers=NO_STORE_HEADERS,
        )
    client = OpenCodeClient()
    try:
        authorize = await client.startOAuthAuthorize(
            service.opencode_provider_id,
            method=body.method,
        )
    except OpenCodeClientError as error:
        raise _httpErrorFromOpenCode(error) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
            headers=NO_STORE_HEADERS,
        ) from error

    url_raw = authorize.get('url')
    url = url_raw.strip() if isinstance(url_raw, str) and url_raw.strip() != '' else None
    method_raw = authorize.get('method')
    authorization_method: Literal['auto', 'code'] | None = None
    if method_raw in {'auto', 'code'}:
        authorization_method = method_raw  # type: ignore[assignment]
    instructions_raw = authorize.get('instructions')
    instructions = (
        instructions_raw.strip()
        if isinstance(instructions_raw, str) and instructions_raw.strip() != ''
        else None
    )
    return OAuthStartResponse(
        provider_id=service.opencode_provider_id,
        method=body.method,
        url=url,
        authorization_method=authorization_method,
        instructions=instructions,
        authorize=authorize,
    )


@router.post(
    '/{service_id}/oauth/callback',
    summary='AI バックエンド OAuth 完了 API',
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def AIBackendOAuthCallbackAPI(
    service_id: Annotated[str, Path(min_length=36, max_length=36)],
    body: OAuthCallbackRequest,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> None:
    """OAuth callback を OpenCode に渡し、成功時に oauth_connected を立てる。

    browser (auto) では code 無し、headless/device では code を渡す。
    ベストエフォート: provider によっては redirect が Docker 内 localhost の
    ため完了できない場合がある。その場合は headless method を選ぶ。
    """

    response.headers.update(NO_STORE_HEADERS)
    service = AIBackendSettingsStore.getService(service_id)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='AI backend service not found.',
            headers=NO_STORE_HEADERS,
        )
    if service.auth_mode != 'OAuthSubscription':
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='OAuth callback requires auth_mode=OAuthSubscription.',
            headers=NO_STORE_HEADERS,
        )
    client = OpenCodeClient()
    try:
        ok = await client.completeOAuthCallback(
            service.opencode_provider_id,
            method=body.method,
            code=body.code,
        )
    except OpenCodeClientError as error:
        raise _httpErrorFromOpenCode(error) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
            headers=NO_STORE_HEADERS,
        ) from error
    if ok is not True:
        # OpenCode が false を返した場合も接続未完了として 502。
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='OpenCode OAuth callback did not complete.',
            headers=NO_STORE_HEADERS,
        )
    try:
        AIBackendSettingsStore.setOAuthConnected(service.service_id, True)
    except KeyError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='AI backend service not found.',
            headers=NO_STORE_HEADERS,
        ) from error
    except (OSError, ValueError) as error:
        logging.error('[AIBackendOAuthCallbackAPI] Failed to set oauth_connected:', exc_info=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to mark OAuth as connected.',
            headers=NO_STORE_HEADERS,
        ) from error

    try:
        _invalidate_episode_lookup_proof(service_id=service.service_id)
    except TypeError:
        pass
    except Exception:
        pass


@router.post(
    '/{service_id}/oauth/disconnect',
    summary='AI バックエンド OAuth 切断 API',
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def AIBackendOAuthDisconnectAPI(
    service_id: Annotated[str, Path(min_length=36, max_length=36)],
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> None:
    """OAuth 接続を切断し、共有が無ければ OpenCode auth を除去する。"""

    response.headers.update(NO_STORE_HEADERS)
    service = AIBackendSettingsStore.getService(service_id)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='AI backend service not found.',
            headers=NO_STORE_HEADERS,
        )
    if service.auth_mode != 'OAuthSubscription':
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='OAuth disconnect requires auth_mode=OAuthSubscription.',
            headers=NO_STORE_HEADERS,
        )
    try:
        AIBackendSettingsStore.setOAuthConnected(service.service_id, False)
    except KeyError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='AI backend service not found.',
            headers=NO_STORE_HEADERS,
        ) from error
    except (OSError, ValueError) as error:
        logging.error('[AIBackendOAuthDisconnectAPI] Failed:', exc_info=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to disconnect OAuth.',
            headers=NO_STORE_HEADERS,
        ) from error

    try:
        _invalidate_episode_lookup_proof(service_id=service.service_id)
    except TypeError:
        pass
    except Exception:
        pass

    try:
        await _removeOpenCodeAuthIfUnshared(
            service.opencode_provider_id,
            # 切断した service 自身は oauth_connected=False なので参照カウントから除外する。
            excluding_service_id=service.service_id,
        )
    except OpenCodeClientError as error:
        logging.warning(
            f'[AIBackendOAuthDisconnectAPI] OpenCode auth removal failed: {error}',
        )


def _ConnectionTestResponse(result: ConnectionTestResult) -> AIBackendConnectionTestResponse:
    """内部結果を API 応答へ変換する（秘密なし）。"""

    return AIBackendConnectionTestResponse(
        success=result.success,
        latency_ms=result.latency_ms,
        model=result.model,
        message=result.message,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        http_status=result.http_status,
        error_code=result.error_code,
    )


@router.post(
    '/connection-test',
    summary='AI バックエンド OpenCode 接続試験 API',
    response_model=AIBackendConnectionTestResponse,
)
async def AIBackendConnectionTestAPI(
    body: AIBackendConnectionTestRequest,
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> AIBackendConnectionTestResponse:
    """保存済み service または draft から OpenCode 接続試験を実行する。

    draft + 一時 api_key の場合、他 service が同一 provider を使っていなければ
    試験後に OpenCode auth を削除する。
    """

    response.headers.update(NO_STORE_HEADERS)

    if IsOpenCodeAvailable() is False:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='OpenCode serve is unavailable.',
            headers=NO_STORE_HEADERS,
        )

    backend = None
    remove_auth_on_cleanup = False
    provider_id_for_cleanup: str | None = None

    try:
        if body.service_id is not None:
            try:
                backend = BuildOpenCodeBackendFromServiceID(
                    body.service_id,
                    api_key=body.api_key,
                )
            except RecordedSeriesAIError as error:
                if error.code == 'OpenCodeServiceNotFound':
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail='AI backend service not found.',
                        headers=NO_STORE_HEADERS,
                    ) from error
                raise
            # 保存済み service への一時キー上書き時のみ、共有が無ければ掃除候補。
            if body.api_key is not None:
                remaining = AIBackendSettingsStore.countServicesUsingProvider(
                    backend.service.opencode_provider_id,
                    excluding_service_id=backend.service.service_id,
                )
                remove_auth_on_cleanup = remaining == 0
                provider_id_for_cleanup = backend.service.opencode_provider_id
        else:
            # draft から一時 service を組み立てる。
            if (
                body.service_name is None
                or body.opencode_provider_id is None
                or body.opencode_model_id is None
                or body.auth_mode is None
                or body.billing_mode is None
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        'Draft connection test requires service_name, '
                        'opencode_provider_id, opencode_model_id, auth_mode, billing_mode.'
                    ),
                    headers=NO_STORE_HEADERS,
                )
            try:
                draft_service = AIBackendService(
                    service_id=str(uuid4()),
                    service_name=body.service_name,
                    opencode_provider_id=body.opencode_provider_id,
                    opencode_model_id=body.opencode_model_id,
                    auth_mode=body.auth_mode,
                    billing_mode=body.billing_mode,
                    api_base_url=body.api_base_url,
                    google_cloud_project=body.google_cloud_project,
                    google_cloud_location=body.google_cloud_location,
                )
            except ValueError as error:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(error),
                    headers=NO_STORE_HEADERS,
                ) from error
            remaining = AIBackendSettingsStore.countServicesUsingProvider(
                draft_service.opencode_provider_id,
            )
            # 他 service が同一 provider を使っていなければ、試験後に auth を残さない。
            remove_auth_on_cleanup = remaining == 0 and body.api_key is not None
            provider_id_for_cleanup = draft_service.opencode_provider_id
            backend = BuildOpenCodeBackendFromDraft(
                draft_service,
                api_key=body.api_key,
                remove_auth_on_cleanup=remove_auth_on_cleanup,
            )

        try:
            result = await backend.testConnection(body.capability)
        except RecordedSeriesAIError as error:
            # backend が例外で落ちた場合も ConnectionTest 形へ正規化する。
            result = ConnectionTestResult(
                success=False,
                latency_ms=error.latency_ms or 0,
                model=f'opencode:{backend.service.opencode_provider_id}/{backend.service.opencode_model_id}',
                message=str(error.code),
                http_status=error.http_status,
                error_code=error.code,
            )
        return _ConnectionTestResponse(result)
    finally:
        # 一時キー試験後の OpenCode auth 掃除（共有 provider は壊さない）。
        if remove_auth_on_cleanup and provider_id_for_cleanup is not None:
            client = OpenCodeClient()
            try:
                await client.deleteAuth(provider_id_for_cleanup)
            except OpenCodeClientError as error:
                logging.warning(
                    f'[AIBackendConnectionTestAPI] Temporary auth cleanup failed: {error}',
                )
