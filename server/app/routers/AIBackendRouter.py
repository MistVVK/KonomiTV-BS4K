"""AI バックエンド（OpenCode service）管理 API。

含む: service CRUD / APIキー set・delete / OAuth 開始・切断（仮） /
OpenCode auth 注入と DELETE /auth/{id} 連動 / health・availability /
draft 接続試験（Phase 2）。

含まない（後続 Phase）: 月次利用量本実装（P3）。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app import logging
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


class OAuthStartResponse(BaseModel):
    """OAuth 開始の仮応答（Phase 7b で実機検証）。"""

    model_config = ConfigDict(extra='forbid')

    provider_id: str
    # OpenCode が返す authorize 情報（URL 等）。token は含めない想定。
    authorize: dict[str, Any]


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
    summary='AI バックエンド OAuth 開始 API（仮）',
    response_model=OAuthStartResponse,
)
async def AIBackendOAuthStartAPI(
    service_id: Annotated[str, Path(min_length=36, max_length=36)],
    response: Response,
    _current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> OAuthStartResponse:
    """OAuth 認可を開始する（headless 可否は Phase 7b）。"""

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
        authorize = await client.startOAuthAuthorize(service.opencode_provider_id)
    except OpenCodeClientError as error:
        raise _httpErrorFromOpenCode(error) from error
    # 仮: authorize が成功したら connected にするのは callback 側が本来。
    # ここでは開始できたことだけを返す。
    return OAuthStartResponse(
        provider_id=service.opencode_provider_id,
        authorize=authorize,
    )


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
