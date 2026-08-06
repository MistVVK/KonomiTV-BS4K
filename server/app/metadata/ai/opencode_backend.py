# pyright: reportPrivateUsage=false
"""OpenCode 経路の RecordedSeriesAIBackend 実装（Phase 2）。

操作ごとに session create → prompt(json_schema) → delete を行い、
Pydantic で最終検証する。失敗時は abort + delete で session を片付ける。
EpisodeLookup の本実装は Phase 4（ここでは Unsupported）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app import logging
from app.constants import (
    OPENCODE_AGENT_EPISODE,
    OPENCODE_AGENT_GENERATE,
)
from app.metadata.ai.AIAPIUsageLedger import AIAPIUsageLedger
from app.metadata.ai.AIBackendSettings import (
    AIBackendService,
    AIBackendSettingsStore,
)
from app.metadata.ai.backends import (
    ConnectionTestCheck,
    ConnectionTestResult,
    EpisodeLookupConnectionChecks,
    UnsupportedOperationError,
)
from app.metadata.ai.episode_lookup import EpisodeLookupResult
from app.metadata.ai.opencode_client import (
    ExtractOpenCodeStructuredOutput,
    ExtractOpenCodeUsage,
    OpenCodeClient,
    OpenCodeClientError,
    OpenCodeUnavailableError,
)
from app.metadata.ai.opencode_types import OpenCodeNormalizedUsage
from app.metadata.RecordedEpisodeContext import RecordedEpisodeLookupContext
from app.metadata.RecordedSeriesCandidates import (
    AIChoiceResult,
    RecordedSeriesAIError,
    RecordedSeriesProgramPrompt,
    SeriesChoiceCandidate,
    _AIChoiceOutput,
)
from app.metadata.RecordedSeriesGeneration import (
    AISeriesMetadataOutput,
    AISeriesMetadataResult,
    BuildSeriesMetadataPrompt,
    SeriesMetadataClusterHint,
    SeriesMetadataExistingSeriesHint,
    SeriesMetadataHints,
    SeriesMetadataLocalParseHint,
    SeriesMetadataWikipediaHint,
    ValidateSeriesMetadataOutput,
)


# service 単位の同時実行上限（製品 serve と API 従量の暴走防止）。
_OPENCODE_SERVICE_MAX_CONCURRENCY = 2
# prompt の HTTP タイムアウト秒。
_OPENCODE_PROMPT_TIMEOUT_SEC = 120.0
# OpenCode 側 format.retryCount。
_OPENCODE_FORMAT_RETRY_COUNT = 1
# KonomiTV 側の Pydantic 検証失敗時の再試行回数（合計 2 回まで試す）。
_OPENCODE_LOCAL_VALIDATION_ATTEMPTS = 2

_service_semaphores: dict[str, asyncio.Semaphore] = {}
_service_semaphores_lock = asyncio.Lock()

_T = TypeVar('_T')


async def _GetServiceSemaphore(service_id: str) -> asyncio.Semaphore:
    """service_id ごとの同時実行セマフォを返す。"""

    async with _service_semaphores_lock:
        semaphore = _service_semaphores.get(service_id)
        if semaphore is None:
            semaphore = asyncio.Semaphore(_OPENCODE_SERVICE_MAX_CONCURRENCY)
            _service_semaphores[service_id] = semaphore
        return semaphore


def _MapClientError(error: OpenCodeClientError, *, latency_ms: int) -> RecordedSeriesAIError:
    """OpenCodeClientError を監査可能な固定コードへ写像する。"""

    if isinstance(error, OpenCodeUnavailableError):
        return RecordedSeriesAIError('OpenCodeUnavailable', http_status=503, latency_ms=latency_ms)
    status = error.status_code
    if status == 408:
        return RecordedSeriesAIError('Timeout', http_status=408, latency_ms=latency_ms)
    if status == 401:
        return RecordedSeriesAIError('HTTP401', http_status=401, latency_ms=latency_ms)
    if status == 403:
        return RecordedSeriesAIError('HTTP403', http_status=403, latency_ms=latency_ms)
    if status is not None and 400 <= status <= 599:
        return RecordedSeriesAIError(f'HTTP{status}', http_status=status, latency_ms=latency_ms)
    return RecordedSeriesAIError('OpenCodeClientError', http_status=status, latency_ms=latency_ms)


def _BuildCandidateSelectionPrompt(
    program: RecordedSeriesProgramPrompt,
    candidates: list[SeriesChoiceCandidate],
) -> str:
    """候補集合外を選べない JSON-only プロンプトを構築する。"""

    candidates_json = json.dumps(
        [
            {
                'choice_id': candidate['choice_id'],
                'kind': candidate['kind'],
                'title': candidate['title'],
                'description': candidate['description'],
            }
            for candidate in candidates
        ],
        ensure_ascii=False,
        indent=2,
    )
    return (
        'You are a TV recording series classifier. Select the single best candidate.\n\n'
        'Rules:\n'
        '- Select exactly one choice_id from the candidates below.\n'
        '- Never invent a choice_id.\n'
        '- Do not use tools, files, terminals, or external resources.\n'
        '- Return only one JSON object with choice_id and confidence.\n'
        '- confidence must be a number between 0.0 and 1.0.\n\n'
        f"Program:\n"
        f"Title: {program['title']}\n"
        f"Description: {program['description']}\n"
        f"Genres: {', '.join(program['genres'])}\n"
        f"Channel: {program.get('channel') or 'Unknown'}\n"
        f"Start Date: {program['start_date']}\n\n"
        f'Candidates:\n{candidates_json}\n\n'
        'Output schema:\n'
        '{"choice_id":"...","confidence":0.0}'
    )


def _JsonSchemaForModel(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic モデルから OpenCode へ渡す JSON Schema を生成する。"""

    return model.model_json_schema()


class OpenCodeBackend:
    """OpenCode serve 経由の録画シリーズ AI バックエンド。"""

    def __init__(
        self,
        service: AIBackendService,
        *,
        api_key: str | None = None,
        client: OpenCodeClient | None = None,
        temporary_auth: bool = False,
        remove_auth_on_cleanup: bool = False,
    ) -> None:
        """バックエンドを初期化する。

        Args:
            service: OpenCode service 定義（秘密を含まない）。
            api_key: ApiKey モードで使うキー。None なら secrets ストアから読む。
            client: テスト差し替え用クライアント。
            temporary_auth: draft 接続試験などで一時キーを注入したか。
            remove_auth_on_cleanup: cleanup 時に OpenCode auth を消すか
                （共有 provider が無い一時試験向け）。
        """

        # この backend が参照する service 定義（不変 snapshot）。
        self._service = service
        # 明示 API キー。None ならストア参照。ログに出さない。
        self._api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() != '' else None
        # HTTP クライアント。
        self._client = client or OpenCodeClient()
        # 一時 auth を注入したか（cleanup 判断用）。
        self._temporary_auth = temporary_auth
        # cleanup で OpenCode auth を削除するか。
        self._remove_auth_on_cleanup = remove_auth_on_cleanup
        # 監査ラベル。
        self._audit_model = (
            f'opencode:{service.opencode_provider_id}/{service.opencode_model_id}'
        )

    @property
    def backend_kind(self) -> str:
        return 'OpenCode'

    @property
    def service(self) -> AIBackendService:
        """参照中の service 定義。"""

        return self._service

    async def ensureAuthInjected(self) -> None:
        """必要なら API キーを OpenCode auth へ注入する。

        Raises:
            RecordedSeriesAIError: キー不足または注入失敗。
        """

        if self._service.auth_mode == 'NoneLocal':
            # ローカル推論は OpenCode 側 provider 設定前提。キー注入は不要。
            return
        if self._service.auth_mode == 'VertexAdc':
            # Vertex は ADC。OpenCode 側の env/config 前提（Phase 7b）。
            return
        if self._service.auth_mode == 'OAuthSubscription':
            if self._service.oauth_connected is False:
                raise RecordedSeriesAIError('OpenCodeOAuthNotConnected')
            return
        # ApiKey
        key = self._api_key
        if key is None:
            key = AIBackendSettingsStore.getAPIKey(self._service.service_id)
        if key is None or key.strip() == '':
            raise RecordedSeriesAIError('OpenCodeAPIKeyMissing')
        try:
            await self._client.putApiKey(self._service.opencode_provider_id, key)
        except OpenCodeClientError as error:
            raise _MapClientError(error, latency_ms=0) from error

    async def cleanupTemporaryAuth(self) -> None:
        """一時 auth を OpenCode から除去する（失敗は warning）。"""

        if self._remove_auth_on_cleanup is False:
            return
        try:
            await self._client.deleteAuth(self._service.opencode_provider_id)
        except OpenCodeClientError as error:
            logging.warning(
                f'[OpenCodeBackend] Failed to cleanup temporary auth for '
                f'provider={self._service.opencode_provider_id}: {error}',
            )

    async def _runStructured(
        self,
        *,
        prompt_text: str,
        schema_model: type[BaseModel],
        agent: str,
        timeout_sec: float = _OPENCODE_PROMPT_TIMEOUT_SEC,
    ) -> tuple[dict[str, Any], OpenCodeNormalizedUsage, int]:
        """1 回の session で structured output を取得する。

        Args:
            prompt_text: プロンプト本文。
            schema_model: JSON Schema 元の Pydantic モデル。
            agent: OpenCode agent 名。
            timeout_sec: prompt HTTP タイムアウト。

        Returns:
            (structured_dict, usage, latency_ms)

        Raises:
            RecordedSeriesAIError: 通信・構造化失敗。
        """

        started = time.monotonic()
        session_id: str | None = None
        try:
            session_id = await self._client.createSession()
            message = await self._client.promptJsonSchema(
                session_id,
                text=prompt_text,
                provider_id=self._service.opencode_provider_id,
                model_id=self._service.opencode_model_id,
                agent=agent,
                schema=_JsonSchemaForModel(schema_model),
                retry_count=_OPENCODE_FORMAT_RETRY_COUNT,
                timeout_sec=timeout_sec,
            )
            usage = ExtractOpenCodeUsage(message)
            structured = ExtractOpenCodeStructuredOutput(message)
            latency_ms = int((time.monotonic() - started) * 1000)
            if structured is None:
                raise RecordedSeriesAIError(
                    'OpenCodeStructuredOutputMissing',
                    latency_ms=latency_ms,
                )
            return structured, usage, latency_ms
        except OpenCodeClientError as error:
            latency_ms = int((time.monotonic() - started) * 1000)
            if session_id is not None:
                try:
                    await self._client.abortSession(session_id)
                except OpenCodeClientError:
                    pass
            raise _MapClientError(error, latency_ms=latency_ms) from error
        finally:
            if session_id is not None:
                try:
                    await self._client.deleteSession(session_id)
                except OpenCodeClientError as cleanup_error:
                    logging.warning(
                        f'[OpenCodeBackend] Failed to delete session: {cleanup_error}',
                    )

    async def _withServiceLimit(
        self,
        operation: Callable[[], Awaitable[_T]],
    ) -> _T:
        """service 単位の同時実行上限内で operation を実行する。"""

        semaphore = await _GetServiceSemaphore(self._service.service_id)
        async with semaphore:
            return await operation()

    async def _withMonthlyReservation(
        self,
        operation: Callable[[], Awaitable[tuple[_T, OpenCodeNormalizedUsage | None]]],
    ) -> _T:
        """月次台帳の予約→実行→精算で operation を包む。

        operation は (結果, 合計 usage) を返す。usage が None かつ例外なしは予約解放のみ。
        draft / 一時 service（temporary_auth）は台帳に載せない（orphan 行防止）。
        保存済み service の接続試験・本番呼び出しのみ算入する。
        """

        # 未保存 draft 接続試験は service_id が一時 UUID のため台帳対象外。
        if self._temporary_auth:
            result, _usage = await operation()
            return result

        reservation = await AIAPIUsageLedger.Reserve(self._service)
        settled = False
        try:
            result, usage = await operation()
            await AIAPIUsageLedger.Settle(reservation, usage)
            settled = True
            return result
        except RecordedSeriesAIError as error:
            # 外部呼出前・認証失敗など課金なしが明確なコードは予約解放のみ。
            free_codes = {
                'OpenCodeAPIKeyMissing',
                'OpenCodeOAuthNotConnected',
                'OpenCodeUnavailable',
                'OpenCodeServiceNotFound',
                'MonthlyTokenLimitReached',
                'MonthlyCostLimitReached',
                'EmptyCandidateSet',
            }
            if error.code in free_codes:
                await AIAPIUsageLedger.Settle(reservation, free_failure=True)
            elif error.http_status in {401, 403}:
                # 認証失敗は通常課金されない。
                await AIAPIUsageLedger.Settle(reservation, free_failure=True)
            else:
                # timeout / 5xx / schema 失敗後など: 実 usage を部分回収できていないため
                # 予約見積を安全側で settled へ振り替える（unknown_interrupt）。
                await AIAPIUsageLedger.Settle(reservation, unknown_interrupt=True)
            settled = True
            raise
        except Exception:
            await AIAPIUsageLedger.Settle(reservation, unknown_interrupt=True)
            settled = True
            raise
        finally:
            if settled is False:
                await AIAPIUsageLedger.Settle(reservation, unknown_interrupt=True)

    async def _selectCandidateUnlocked(
        self,
        program: RecordedSeriesProgramPrompt,
        candidates: list[SeriesChoiceCandidate],
        *,
        minimum_confidence: float = 0.80,
    ) -> AIChoiceResult:
        """候補選択本体（セマフォは呼び出し側）。"""

        allowed_ids = {candidate['choice_id'] for candidate in candidates}
        if len(allowed_ids) == 0:
            raise RecordedSeriesAIError('EmptyCandidateSet')

        await self.ensureAuthInjected()

        async def Run() -> tuple[AIChoiceResult, OpenCodeNormalizedUsage | None]:
            prompt = _BuildCandidateSelectionPrompt(program, candidates)
            last_error: RecordedSeriesAIError | None = None
            total_prompt = 0
            total_completion = 0
            total_reasoning = 0
            total_cost = 0.0
            has_cost = False
            latency_ms = 0
            for attempt in range(_OPENCODE_LOCAL_VALIDATION_ATTEMPTS):
                try:
                    structured, usage, latency_ms = await self._runStructured(
                        prompt_text=prompt,
                        schema_model=_AIChoiceOutput,
                        agent=OPENCODE_AGENT_GENERATE,
                    )
                    total_prompt += usage['prompt_tokens']
                    total_completion += usage['completion_tokens']
                    total_reasoning += usage['reasoning_tokens']
                    if usage['estimated_cost_usd'] is not None:
                        total_cost += float(usage['estimated_cost_usd'])
                        has_cost = True
                    try:
                        validated = _AIChoiceOutput.model_validate(structured)
                    except ValidationError as error:
                        last_error = RecordedSeriesAIError(
                            'InvalidOutputSchema',
                            latency_ms=latency_ms,
                        )
                        if attempt + 1 < _OPENCODE_LOCAL_VALIDATION_ATTEMPTS:
                            continue
                        raise last_error from error
                    if validated.choice_id not in allowed_ids:
                        last_error = RecordedSeriesAIError(
                            'ChoiceOutsideCandidateSet',
                            latency_ms=latency_ms,
                        )
                        if attempt + 1 < _OPENCODE_LOCAL_VALIDATION_ATTEMPTS:
                            continue
                        raise last_error
                    if validated.confidence < minimum_confidence:
                        raise RecordedSeriesAIError(
                            'LowConfidence',
                            latency_ms=latency_ms,
                        )
                    combined = OpenCodeNormalizedUsage(
                        prompt_tokens=total_prompt,
                        completion_tokens=total_completion,
                        reasoning_tokens=total_reasoning,
                        total_tokens=total_prompt + total_completion + total_reasoning,
                        estimated_cost_usd=total_cost if has_cost else None,
                    )
                    return AIChoiceResult(
                        choice_id=validated.choice_id,
                        confidence=float(validated.confidence),
                        model=self._audit_model,
                        prompt_tokens=total_prompt or None,
                        completion_tokens=total_completion or None,
                        http_status=200,
                        latency_ms=latency_ms,
                    ), combined
                except RecordedSeriesAIError as error:
                    last_error = error
                    if error.code in {
                        'InvalidOutputSchema',
                        'OpenCodeStructuredOutputMissing',
                        'ChoiceOutsideCandidateSet',
                    } and attempt + 1 < _OPENCODE_LOCAL_VALIDATION_ATTEMPTS:
                        continue
                    # 失敗時の usage は _withMonthlyReservation が unknown_interrupt
                    # （予約見積の安全側確定）で扱う。部分 usage の精算は行わない。
                    raise
            assert last_error is not None
            raise last_error

        return await self._withMonthlyReservation(Run)

    async def selectCandidate(
        self,
        program: RecordedSeriesProgramPrompt,
        candidates: list[SeriesChoiceCandidate],
        *,
        minimum_confidence: float = 0.80,
    ) -> AIChoiceResult:
        """候補選択を OpenCode structured output で実行する。"""

        return await self._withServiceLimit(
            lambda: self._selectCandidateUnlocked(
                program,
                candidates,
                minimum_confidence=minimum_confidence,
            ),
        )

    async def _resolveSeriesMetadataUnlocked(
        self,
        program: RecordedSeriesProgramPrompt,
        hints: SeriesMetadataHints,
    ) -> AISeriesMetadataResult:
        """シリーズ情報生成本体（セマフォは呼び出し側）。"""

        await self.ensureAuthInjected()

        async def Run() -> tuple[AISeriesMetadataResult, OpenCodeNormalizedUsage | None]:
            prompt = BuildSeriesMetadataPrompt(program, hints)
            last_error: RecordedSeriesAIError | None = None
            total_prompt = 0
            total_completion = 0
            total_reasoning = 0
            total_cost = 0.0
            has_cost = False
            latency_ms = 0
            for attempt in range(_OPENCODE_LOCAL_VALIDATION_ATTEMPTS):
                try:
                    structured, usage, latency_ms = await self._runStructured(
                        prompt_text=prompt,
                        schema_model=AISeriesMetadataOutput,
                        agent=OPENCODE_AGENT_GENERATE,
                    )
                    total_prompt += usage['prompt_tokens']
                    total_completion += usage['completion_tokens']
                    total_reasoning += usage['reasoning_tokens']
                    if usage['estimated_cost_usd'] is not None:
                        total_cost += float(usage['estimated_cost_usd'])
                        has_cost = True
                    try:
                        result = ValidateSeriesMetadataOutput(
                            structured,
                            hints=hints,
                            model=self._audit_model,
                            prompt_tokens=total_prompt or None,
                            completion_tokens=total_completion or None,
                            http_status=200,
                            latency_ms=latency_ms,
                        )
                        combined = OpenCodeNormalizedUsage(
                            prompt_tokens=total_prompt,
                            completion_tokens=total_completion,
                            reasoning_tokens=total_reasoning,
                            total_tokens=total_prompt + total_completion + total_reasoning,
                            estimated_cost_usd=total_cost if has_cost else None,
                        )
                        return result, combined
                    except RecordedSeriesAIError as error:
                        last_error = error
                        if (
                            error.code in {
                                'InvalidSeriesMetadataSchema',
                                'InvalidJSON',
                                'InvalidJSONType',
                            }
                            and attempt + 1 < _OPENCODE_LOCAL_VALIDATION_ATTEMPTS
                        ):
                            continue
                        raise
                except RecordedSeriesAIError as error:
                    last_error = error
                    if (
                        error.code in {
                            'InvalidSeriesMetadataSchema',
                            'OpenCodeStructuredOutputMissing',
                        }
                        and attempt + 1 < _OPENCODE_LOCAL_VALIDATION_ATTEMPTS
                    ):
                        continue
                    raise
            assert last_error is not None
            raise last_error

        return await self._withMonthlyReservation(Run)

    async def resolveSeriesMetadata(
        self,
        program: RecordedSeriesProgramPrompt,
        hints: SeriesMetadataHints,
    ) -> AISeriesMetadataResult:
        """シリーズ情報を OpenCode structured output で一括生成する。"""

        return await self._withServiceLimit(
            lambda: self._resolveSeriesMetadataUnlocked(program, hints),
        )

    async def lookupEpisode(
        self,
        program: RecordedEpisodeLookupContext,
    ) -> EpisodeLookupResult:
        """EpisodeLookup は Phase 4。

        Args:
            program: 未使用。

        Raises:
            UnsupportedOperationError: 常に。
        """

        _ = program
        _ = OPENCODE_AGENT_EPISODE
        raise UnsupportedOperationError('lookupEpisode', 'OpenCode')

    async def testConnection(
        self,
        capability: str,
    ) -> ConnectionTestResult:
        """接続試験。CandidateSelection は本番同等のシリーズ生成 schema を通す。"""

        if capability == 'EpisodeLookup':
            not_run = ConnectionTestCheck(
                status='NotRun',
                message='OpenCode の EpisodeLookup 接続試験は Phase 4 で実装します。',
            )
            return ConnectionTestResult(
                success=False,
                latency_ms=0,
                model=self._audit_model,
                message='OpenCode EpisodeLookup は Phase 4 で実装予定です。',
                checks=EpisodeLookupConnectionChecks(
                    backend_connection=not_run,
                    web_search=not_run,
                    source_url=not_run,
                    strict_schema=not_run,
                    timeout_cancel=not_run,
                    permission_policy=ConnectionTestCheck(
                        status='NotApplicable',
                        message='通常生成 agent の permission は EpisodeLookup 試験では対象外です。',
                    ),
                ),
                error_code='OpenCodeEpisodeLookupNotReady',
            )

        # CandidateSelection: ACP と同様にシリーズ情報生成 schema で疎通確認する。
        test_program = RecordedSeriesProgramPrompt(
            title='KonomiTV-BS4K OpenCode connection test',
            description='Synthetic series metadata generation connection test.',
            genres=['ConnectionTest'],
            channel=None,
            start_date='2000-01-01',
        )
        test_hints = SeriesMetadataHints(
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

        async def Run() -> ConnectionTestResult:
            started = time.monotonic()
            try:
                # セマフォは外側で1回だけ。内側は unlocked を呼んでデッドロックを避ける。
                result = await self._resolveSeriesMetadataUnlocked(test_program, test_hints)
            except RecordedSeriesAIError as error:
                latency_ms = error.latency_ms or int((time.monotonic() - started) * 1000)
                message = _ConnectionTestMessage(error.code)
                return ConnectionTestResult(
                    success=False,
                    latency_ms=latency_ms,
                    model=self._audit_model,
                    message=message,
                    prompt_tokens=None,
                    completion_tokens=None,
                    http_status=error.http_status,
                    error_code=error.code,
                )
            return ConnectionTestResult(
                success=True,
                latency_ms=result.latency_ms,
                model=self._audit_model,
                message='OpenCode 経由でシリーズ情報生成 schema の接続を確認しました。',
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                http_status=result.http_status,
                selected_choice_id=None,
            )

        return await self._withServiceLimit(Run)


def _ConnectionTestMessage(error_code: str) -> str:
    """接続試験失敗コードを利用者向け短文にする（秘密を含めない）。"""

    if error_code == 'OpenCodeUnavailable':
        return 'OpenCode serve が利用できません。サーバーログを確認してください。'
    if error_code == 'OpenCodeAPIKeyMissing':
        return 'API キーが未設定です。'
    if error_code == 'OpenCodeOAuthNotConnected':
        return 'OAuth が未接続です。'
    if error_code == 'HTTP401':
        return '認証に失敗しました。API キーを確認してください。'
    if error_code in {'Timeout', 'HTTP408'}:
        return 'OpenCode への応答がタイムアウトしました。'
    if error_code.startswith('HTTP'):
        return f'OpenCode がエラーを返しました。（{error_code}）'
    if error_code in {
        'InvalidSeriesMetadataSchema',
        'OpenCodeStructuredOutputMissing',
        'InvalidOutputSchema',
    }:
        return '応答が structured output / 本番 schema と一致しませんでした。'
    return 'OpenCode 接続試験に失敗しました。'


def BuildOpenCodeBackendFromServiceID(
    service_id: str,
    *,
    api_key: str | None = None,
    client: OpenCodeClient | None = None,
) -> OpenCodeBackend:
    """保存済み service_id から backend を構築する。

    Args:
        service_id: AI バックエンド service UUID。
        api_key: 上書きキー（通常は None）。
        client: テスト差し替え。

    Returns:
        OpenCodeBackend。

    Raises:
        RecordedSeriesAIError: service 不在。
    """

    service = AIBackendSettingsStore.getService(service_id)
    if service is None:
        raise RecordedSeriesAIError('OpenCodeServiceNotFound')
    return OpenCodeBackend(service, api_key=api_key, client=client)


def BuildOpenCodeBackendFromDraft(
    service: AIBackendService,
    *,
    api_key: str | None = None,
    client: OpenCodeClient | None = None,
    remove_auth_on_cleanup: bool = False,
) -> OpenCodeBackend:
    """未保存 draft service から一時 backend を構築する。

    Args:
        service: draft の service 定義。
        api_key: 一時キー。
        client: テスト差し替え。
        remove_auth_on_cleanup: 試験後に auth を消すか。

    Returns:
        OpenCodeBackend。
    """

    # draft は常に temporary（台帳 skip）。api_key 無しの NoneLocal 試験も含む。
    return OpenCodeBackend(
        service,
        api_key=api_key,
        client=client,
        temporary_auth=True,
        remove_auth_on_cleanup=remove_auth_on_cleanup,
    )
