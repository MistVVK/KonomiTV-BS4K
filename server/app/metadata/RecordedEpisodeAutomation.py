from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Literal, cast

from tortoise import transactions
from tortoise.backends.base.client import BaseDBAsyncClient
from typing_extensions import TypedDict

from app import logging
from app.constants import JST
from app.metadata.ai.episode_lookup import (
    EpisodeLookupOutcome,
    EpisodeLookupResult,
    MapLookupOutcomeToResolutionStatus,
)
from app.metadata.ai.recorded_series_ai import (
    get_audit_model,
    get_episode_lookup_provider_fingerprint,
    has_episode_lookup_capability_proof,
    invalidate_episode_lookup_capability_fingerprint,
)
from app.metadata.ai.recorded_series_ai import (
    lookup_episode as ai_lookup_episode,
)
from app.metadata.AnalysisTaskTracker import AnalysisTaskHandle, AnalysisTaskTracker
from app.metadata.RecordedEpisodeContext import (
    BuildEpisodeInputFingerprint,
    BuildRecordedEpisodeLookupContext,
)
from app.metadata.RecordedEpisodeMessages import GetRecordedEpisodeErrorMessage
from app.metadata.RecordedEpisodeResolver import (
    FormatEpisodeNumber,
    ParseLegacyEpisodeNumber,
)
from app.metadata.RecordedEpisodeSearch import (
    GetEpisodeLookupEvidence,
    IsEpisodeLookupResultAccepted,
)
from app.metadata.RecordedSeriesCandidates import RecordedSeriesAIError
from app.metadata.RecordedSeriesLocks import RECORDED_SERIES_RESOLUTION_LOCK
from app.metadata.RecordedSeriesSettings import RecordedSeriesSettingsStore
from app.models.RecordedEpisode import RecordedEpisodeResolution, SeriesEpisode
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedSeries import RecordedSeriesAIRequest


RECORDED_EPISODE_AUTOMATION_VERSION = "1"


class _EpisodeCitationRecord(TypedDict):
    """DBへ保存するWeb検索引用の最小構造。"""

    url: str
    title: str


@dataclass(frozen=True, slots=True)
class _EpisodeProgramSnapshot:
    """話数判定と応答後の再検証に使う録画メタデータ。"""

    id: int
    series_id: int
    series_title: str
    series_episode_id: int | None
    legacy_episode_number: str | None
    title: str
    subtitle: str | None
    description: str
    detail: dict[str, str]
    channel_id: str | None
    channel_name: str | None
    start_time: datetime


@dataclass(frozen=True, slots=True)
class RecordedEpisodeAutomationResult:
    """録画1件の話数判定結果と一括処理用区分。"""

    recorded_program_id: int
    status: Literal["Resolved", "NotNumbered", "NeedsReview", "Failed", "Skipped"]
    source: str
    ai_requested: bool
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class RecordedEpisodeBackfillAccepted:
    """既存録画話数一括判定の受付結果。"""

    execution_id: int
    reused: bool


class _RecordedEpisodeSnapshotChanged(Exception):
    """外部API待機中に話数判定入力またはSeries所属が変化した。"""


class RecordedEpisodeRelookupNotFoundError(Exception):
    """単票再検索の対象が存在しない、または再生可能な録画ではない。"""


class RecordedEpisodeRelookupConflictError(Exception):
    """単票再検索の楽観ロック、Manual 保護、または重複条件に違反した。"""


class RecordedEpisodeRelookupDisabledError(Exception):
    """話数 Web 検索を開始できる設定または能力がない。"""


class RecordedEpisodeRelookupRateLimitedError(Exception):
    """アプリの日次上限により単票再検索を開始できない。"""


def _sha256JSON(payload: object) -> str:
    """順序を固定したJSONから、秘密情報を含まないfingerprintを作る。"""

    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _buildInputFingerprint(snapshot: _EpisodeProgramSnapshot) -> str:
    """話数の決定に影響する録画入力だけをfingerprintへ含める。"""

    return _sha256JSON(
        {
            "automation_version": RECORDED_EPISODE_AUTOMATION_VERSION,
            "series_id": snapshot.series_id,
            "series_title": snapshot.series_title,
            "legacy_episode_number": snapshot.legacy_episode_number,
            "title": snapshot.title,
            "subtitle": snapshot.subtitle,
            "description": snapshot.description,
            "detail": snapshot.detail,
            "channel_id": snapshot.channel_id,
            "start_time": snapshot.start_time.isoformat(),
        }
    )


def _snapshotFromProgram(
    recorded_program: RecordedProgram,
    *,
    channel_name: str | None = None,
) -> _EpisodeProgramSnapshot | None:
    """Series所属録画を判定用の不変スナップショットへ変換する。"""

    if recorded_program.series_id is None:
        return None
    return _EpisodeProgramSnapshot(
        id=recorded_program.id,
        series_id=recorded_program.series_id,
        series_title=recorded_program.series_title or recorded_program.title,
        series_episode_id=recorded_program.series_episode_id,
        legacy_episode_number=recorded_program.episode_number,
        title=recorded_program.title,
        subtitle=recorded_program.subtitle,
        description=recorded_program.description,
        detail=recorded_program.detail,
        channel_id=recorded_program.channel_id,
        channel_name=channel_name,
        start_time=recorded_program.start_time,
    )


def _episodeLookupErrorResult(
    *,
    code: str,
    model: str,
    http_status: int | None = None,
    latency_ms: int | None = None,
) -> EpisodeLookupResult:
    """旧例外経路も backend 共通の安全な lookup outcome へ正規化する。"""

    if code in {
        "MissingWebSearchCall",
        "ACPWebSearchNotObserved",
        "EpisodeLookupCapabilityNotVerified",
    }:
        outcome: EpisodeLookupOutcome = "SearchNotRun"
        web_search_performed = False
    elif code in {
        "InvalidJSON",
        "InvalidJSONType",
        "InvalidOutputSchema",
        "InvalidModelOutput",
        "MissingOutputText",
        "MultipleOutputTexts",
    }:
        outcome = "InvalidModelOutput"
        web_search_performed = True
    elif code in {"HTTP429", "ProviderRateLimited", "DailyAIRequestLimitReached"}:
        outcome = "RateLimited"
        web_search_performed = False
    elif code in {"Cancelled", "AIRequestInterrupted"}:
        outcome = "Cancelled"
        web_search_performed = False
    else:
        outcome = "SearchFailed"
        web_search_performed = False
    return EpisodeLookupResult(
        outcome=outcome,
        season_number=None,
        episode_number=None,
        confidence=None,
        rationale_short=None,
        citations=(),
        web_search_performed=web_search_performed,
        model=model,
        prompt_tokens=None,
        completion_tokens=None,
        http_status=http_status
        if http_status is not None and 100 <= http_status <= 599
        else None,
        latency_ms=latency_ms or 0,
        error_code=code,
        error_message=GetRecordedEpisodeErrorMessage(code),
    )


class RecordedEpisodeAutomation:
    """Series確定後の決定論的話数解析と有料Web検索を独立キューで管理する。"""

    _queue: asyncio.Queue[int] | None = None
    _worker_task: asyncio.Task[None] | None = None
    _queued_ids: set[int] = set()
    _running_ids: set[int] = set()
    _rerun_ids: set[int] = set()
    _backfill_task: asyncio.Task[None] | None = None
    _backfill_handle: AnalysisTaskHandle | None = None
    _relookup_tasks: dict[int, asyncio.Task[None]] = {}
    _relookup_handles: dict[int, AnalysisTaskHandle] = {}
    _start_lock = asyncio.Lock()
    _backfill_start_lock = asyncio.Lock()
    _relookup_start_lock = asyncio.Lock()
    _recovery_completed = False

    @classmethod
    async def start(cls) -> None:
        """中断監査を回収し、話数判定ワーカーと未処理録画を復元する。

        Returns:
            None
        """

        async with cls._start_lock:
            if cls._worker_task is None or cls._worker_task.done():
                cls._queue = asyncio.Queue()
                cls._queued_ids = set()
                cls._running_ids = set()
                cls._rerun_ids = set()
                cls._recovery_completed = False
                cls._worker_task = asyncio.create_task(cls._runWorker())
            if cls._recovery_completed is False:
                await cls._recoverInterruptedRequests()
                await cls._enqueueRecoverablePrograms()
                cls._recovery_completed = True

    @classmethod
    async def stop(cls) -> None:
        """話数ワーカーと手動一括処理を停止する。

        Returns:
            None
        """

        tasks = [
            task
            for task in (
                cls._backfill_task,
                cls._worker_task,
                *cls._relookup_tasks.values(),
            )
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        if len(tasks) > 0:
            await asyncio.gather(*tasks, return_exceptions=True)
        cls._queue = None
        cls._worker_task = None
        cls._queued_ids = set()
        cls._running_ids = set()
        cls._rerun_ids = set()
        cls._backfill_task = None
        cls._backfill_handle = None
        cls._relookup_tasks = {}
        cls._relookup_handles = {}
        cls._recovery_completed = False

    @classmethod
    async def enqueue(cls, recorded_program_id: int) -> None:
        """Series判定を待たせず、話数判定対象IDを重複排除して投入する。

        Args:
            recorded_program_id: Series確定後のRecordedProgram ID。

        Returns:
            None
        """

        await cls.start()
        assert cls._queue is not None
        async with cls._relookup_start_lock:
            relookup_task = cls._relookup_tasks.get(recorded_program_id)
            if relookup_task is not None and relookup_task.done() is False:
                # 明示再検索と通常 enqueue を並走させず、完了後の決定論的再評価だけ予約する。
                cls._rerun_ids.add(recorded_program_id)
                return
            if recorded_program_id in cls._running_ids:
                cls._rerun_ids.add(recorded_program_id)
                return
            if recorded_program_id in cls._queued_ids:
                return
            cls._queued_ids.add(recorded_program_id)
            await cls._queue.put(recorded_program_id)

    @classmethod
    async def _runWorker(cls) -> None:
        """外部APIを録画スキャンから分離し、話数判定を順次実行する。"""

        assert cls._queue is not None
        while True:
            recorded_program_id = await cls._queue.get()
            cls._queued_ids.discard(recorded_program_id)
            cls._running_ids.add(recorded_program_id)
            retry_after_current = False
            try:
                await cls.resolveProgram(recorded_program_id)
            except asyncio.CancelledError:
                raise
            except _RecordedEpisodeSnapshotChanged:
                retry_after_current = True
            except Exception as ex:
                logging.error(
                    f"[RecordedEpisodeAutomation] Failed to resolve episode. "
                    f"recorded_program_id: {recorded_program_id}",
                    exc_info=ex,
                )
            finally:
                cls._running_ids.discard(recorded_program_id)
                cls._queue.task_done()
            if recorded_program_id in cls._rerun_ids:
                cls._rerun_ids.discard(recorded_program_id)
                retry_after_current = True
            if retry_after_current:
                await cls.enqueue(recorded_program_id)

    @classmethod
    async def _recoverInterruptedRequests(cls) -> None:
        """再起動前に外部POST済みだった可能性のある監査を再送せず失敗へ閉じる。"""

        async with transactions.in_transaction() as connection:
            pending_requests = await RecordedSeriesAIRequest.filter(
                purpose="EpisodeLookup",
                status="Pending",
            ).using_db(connection)
            for request in pending_requests:
                if request.episode_resolution_id is not None:
                    resolution = (
                        await RecordedEpisodeResolution.filter(
                            id=request.episode_resolution_id,
                        )
                        .using_db(connection)
                        .first()
                    )
                    # force再検索中の確定値は Resolution の status/source を保持する。
                    # Manual override は source=Manual のまま lookup_outcome だけ Pending
                    # になるため、その試行も中断状態へ閉じる。検索中に新たな Manual
                    # 確定が勝って Pending が消えた行は変更しない。
                    if resolution is not None and (
                        resolution.source != "Manual"
                        or resolution.lookup_outcome == "Pending"
                    ):
                        if (
                            resolution.source != "Manual"
                            and resolution.status not in {"Resolved", "NotNumbered"}
                        ):
                            # 外部 POST 済みかもしれない処理は自動再送せず、明示再検索可能な
                            # Unknown へ戻す。lookup 監査だけを Cancelled として残す。
                            resolution.status = "Unknown"
                            resolution.source = "WebSearch"
                        resolution.lookup_outcome = "Cancelled"
                        resolution.error_code = "AIRequestInterrupted"
                        resolution.error_message = GetRecordedEpisodeErrorMessage(
                            "AIRequestInterrupted"
                        )
                        await resolution.save(
                            update_fields=[
                                "status",
                                "source",
                                "lookup_outcome",
                                "error_code",
                                "error_message",
                                "updated_at",
                            ],
                            using_db=connection,
                        )
                request.status = "Failed"
                request.error_code = "AIRequestInterrupted"
                await request.save(
                    update_fields=["status", "error_code"],
                    using_db=connection,
                )

    @classmethod
    async def _enqueueRecoverablePrograms(
        cls,
        *,
        provider_fingerprint: str | None = None,
    ) -> None:
        """未処理録画と、設定変更で再試行可能になった新規録画をキューへ戻す。"""

        assert cls._queue is not None
        program_ids = cast(
            list[int],
            await RecordedProgram.filter(
                series_id__not_isnull=True,
                recorded_video__status="Recorded",
            ).values_list("id", flat=True),
        )
        if len(program_ids) == 0:
            return
        resolutions = await RecordedEpisodeResolution.filter(
            recorded_program_id__in=program_ids,
        ).all()
        resolutions_by_program_id = {
            resolution.recorded_program_id: resolution for resolution in resolutions
        }
        for recorded_program_id in program_ids:
            resolution = resolutions_by_program_id.get(recorded_program_id)
            if resolution is not None:
                # migrationでseedされた既存録画は、手動一括実行まで有料検索へ進めない。
                if resolution.is_legacy_recording or resolution.source == "Manual":
                    continue
                # 中断前に provider へ送信済みだった可能性があるため、自動再送しない。
                # ユーザーの明示的な単票再検索だけを再試行入口とする。
                if resolution.lookup_outcome == "Cancelled":
                    continue
                retry_for_provider_change = (
                    provider_fingerprint is not None
                    and resolution.source == "WebSearch"
                    and resolution.status in {"NeedsReview", "Failed"}
                    and resolution.provider_fingerprint != provider_fingerprint
                )
                if (
                    resolution.status not in {"Pending", "Unknown"}
                    and retry_for_provider_change is False
                ):
                    continue
            async with cls._relookup_start_lock:
                # provider変更と旧providerへの実行中リクエストが重なった場合、完了後に新設定で
                # もう一度評価する。旧処理が成功していればStructuredCacheとなるため再課金はしない。
                relookup_task = cls._relookup_tasks.get(recorded_program_id)
                if (
                    relookup_task is not None
                    and relookup_task.done() is False
                ) or recorded_program_id in cls._running_ids:
                    cls._rerun_ids.add(recorded_program_id)
                    continue
                if recorded_program_id in cls._queued_ids:
                    continue
                cls._queued_ids.add(recorded_program_id)
                await cls._queue.put(recorded_program_id)

    @classmethod
    async def _loadSnapshot(
        cls, recorded_program_id: int
    ) -> _EpisodeProgramSnapshot | None:
        """再生可能かつSeries所属中の録画を、最新の判定入力として取得する。"""

        recorded_program = (
            await RecordedProgram.filter(
                id=recorded_program_id,
                recorded_video__status="Recorded",
            )
            .prefetch_related("channel")
            .first()
        )
        if recorded_program is None:
            return None
        channel_name = (
            recorded_program.channel.name
            if recorded_program.channel is not None
            else None
        )
        return _snapshotFromProgram(recorded_program, channel_name=channel_name)

    @classmethod
    async def _getOrCreateResolution(
        cls,
        snapshot: _EpisodeProgramSnapshot,
    ) -> RecordedEpisodeResolution:
        """新規録画にだけ、自動判定可能な非legacy Resolutionを作成する。"""

        resolution, _created = await RecordedEpisodeResolution.get_or_create(
            recorded_program_id=snapshot.id,
            defaults={
                "episode_id": snapshot.series_episode_id,
                "status": "Resolved"
                if snapshot.series_episode_id is not None
                else "Pending",
                "source": "Local",
                "lookup_outcome": None,
                "is_legacy_recording": False,
                "input_fingerprint": _buildInputFingerprint(snapshot),
                "provider_fingerprint": None,
                "proposed_season_number": None,
                "proposed_episode_number": None,
                "confidence": None,
                "web_search_performed": False,
                "rationale_short": None,
                "citations": [],
                "ai_model": None,
                "error_code": None,
                "error_message": None,
                "resolved_at": datetime.now(tz=JST)
                if snapshot.series_episode_id is not None
                else None,
            },
        )
        return resolution

    @classmethod
    async def _finishAIRequest(
        cls,
        request: RecordedSeriesAIRequest,
        *,
        status: Literal["Succeeded", "Failed", "Rejected"],
        result: EpisodeLookupResult | None = None,
        error: RecordedSeriesAIError | None = None,
        selected_choice_id: str | None = None,
        connection: BaseDBAsyncClient | None = None,
    ) -> None:
        """話数Web検索監査を、生レスポンスを保持せず完了状態へ更新する。"""

        request_to_update = request
        if connection is not None:
            locked_request = (
                await RecordedSeriesAIRequest.filter(id=request.id)
                .select_for_update()
                .using_db(connection)
                .first()
            )
            if locked_request is None:
                raise RuntimeError(
                    "Episode AI request disappeared before finalization."
                )
            request_to_update = locked_request
        request_to_update.status = status
        request_to_update.selected_choice_id = selected_choice_id
        request_to_update.model = (
            result.model if result is not None else request_to_update.model
        )
        request_to_update.prompt_tokens = (
            result.prompt_tokens if result is not None else None
        )
        request_to_update.completion_tokens = (
            result.completion_tokens if result is not None else None
        )
        request_to_update.http_status = (
            result.http_status
            if result is not None
            else error.http_status
            if error is not None
            else None
        )
        request_to_update.latency_ms = (
            result.latency_ms
            if result is not None
            else error.latency_ms
            if error is not None
            else None
        )
        request_to_update.error_code = (
            error.code
            if error is not None
            else result.error_code
            if result is not None
            else None
        )
        await request_to_update.save(
            update_fields=[
                "status",
                "selected_choice_id",
                "model",
                "prompt_tokens",
                "completion_tokens",
                "http_status",
                "latency_ms",
                "error_code",
            ],
            using_db=connection,
        )

    @classmethod
    async def _snapshotManualLaneIfNeeded(
        cls,
        resolution: RecordedEpisodeResolution,
        connection: BaseDBAsyncClient,
    ) -> None:
        """Manual 正本を AI へ切り替える直前に、未退避の手動値を手動レーンへ写す。

        新しい手動保存は assignProgramEpisode が manual_* を埋める。
        移行前の Manual 行や、manual_status が空のレガシー行だけここで退避する。
        既に手動レーンがある場合は、管理者が残した値を上書きしない。

        Args:
            resolution: 同一 transaction で select_for_update 済みの Resolution。
            connection: 共有中の DB 接続。

        Returns:
            None
        """

        if resolution.source != 'Manual':
            return
        if resolution.manual_status is not None:
            return

        resolution.manual_episode_id = resolution.episode_id
        if resolution.status == 'Resolved':
            resolution.manual_status = 'Resolved'
        elif resolution.status == 'NotNumbered':
            resolution.manual_status = 'NotNumbered'
        else:
            # Unknown および Failed 等の Manual は「話数不明」相当として退避する。
            resolution.manual_status = 'Unknown'
        if resolution.episode_id is None:
            resolution.manual_season_number = None
            resolution.manual_episode_number = None
            return

        episode = (
            await SeriesEpisode.filter(
                id=resolution.episode_id,
            )
            .using_db(connection)
            .first()
        )
        if episode is None:
            resolution.manual_season_number = None
            resolution.manual_episode_number = None
            return
        resolution.manual_season_number = episode.season_number
        resolution.manual_episode_number = episode.episode_number

    @classmethod
    async def _applyResolvedEpisode(
        cls,
        *,
        snapshot: _EpisodeProgramSnapshot,
        resolution_id: int,
        season_number: int,
        episode_number: Decimal,
        source: Literal["Local", "Migration", "WebSearch"],
        provider_fingerprint: str | None,
        confidence: float | None,
        citations: list[_EpisodeCitationRecord],
        ai_model: str | None,
        write_legacy_value: bool,
        rationale_short: str | None = None,
        input_fingerprint: str | None = None,
        ai_request: RecordedSeriesAIRequest | None = None,
        ai_request_status: Literal["Succeeded", "Failed", "Rejected"] | None = None,
        ai_request_result: EpisodeLookupResult | None = None,
        ai_request_error: RecordedSeriesAIError | None = None,
        selected_choice_id: str | None = None,
        allow_manual_overwrite: bool = False,
    ) -> bool:
        """最新入力と手動優先を再確認し、Episodeリンクを原子的に確定する。

        Args:
            allow_manual_overwrite: 単票再検索の受理結果で Manual を WebSearch へ
                置き換えることを許可するか。通常の自動判定では False。
        """

        legacy_value = FormatEpisodeNumber(season_number, episode_number)
        async with transactions.in_transaction() as connection:
            recorded_program = (
                await RecordedProgram.filter(id=snapshot.id)
                .select_for_update()
                .using_db(connection)
                .first()
            )
            resolution = (
                await RecordedEpisodeResolution.filter(id=resolution_id)
                .select_for_update()
                .using_db(connection)
                .first()
            )
            if recorded_program is None or resolution is None:
                raise _RecordedEpisodeSnapshotChanged
            # 自動判定中に管理者が手動保存した場合は Manual を優先する。
            # 単票再検索の受理反映だけは明示的に Manual を上書きする。
            if resolution.source == 'Manual' and allow_manual_overwrite is False:
                if ai_request is not None:
                    if ai_request_result is None:
                        raise RuntimeError("Episode AI result is missing.")
                    await cls._finishAIRequest(
                        ai_request,
                        status="Rejected",
                        result=ai_request_result,
                        error=RecordedSeriesAIError("ManualAssignmentWonRace"),
                        selected_choice_id=selected_choice_id,
                        connection=connection,
                    )
                return False
            # 単票再検索で Manual 正本を AI へ切り替える前に、手動レーンへ現状を退避する。
            if resolution.source == 'Manual' and allow_manual_overwrite is True:
                await cls._snapshotManualLaneIfNeeded(resolution, connection)
            current_snapshot = _snapshotFromProgram(recorded_program)
            if current_snapshot is None or _buildInputFingerprint(
                current_snapshot
            ) != _buildInputFingerprint(snapshot):
                raise _RecordedEpisodeSnapshotChanged

            episode = (
                await SeriesEpisode.filter(
                    series_id=snapshot.series_id,
                    season_number=season_number,
                    episode_number=episode_number,
                )
                .using_db(connection)
                .first()
            )
            if episode is None:
                episode = await SeriesEpisode.create(
                    series_id=snapshot.series_id,
                    season_number=season_number,
                    episode_number=episode_number,
                    using_db=connection,
                )
            recorded_program.series_episode_id = episode.id
            update_fields = ["series_episode_id", "updated_at"]
            if write_legacy_value:
                recorded_program.episode_number = legacy_value
                update_fields.append("episode_number")
            await recorded_program.save(
                update_fields=update_fields, using_db=connection
            )

            resolution.episode_id = episode.id
            resolution.status = "Resolved"
            resolution.source = source
            resolution.lookup_outcome = "Resolved" if source == "WebSearch" else None
            resolution.input_fingerprint = input_fingerprint or _buildInputFingerprint(
                _snapshotFromProgram(recorded_program) or snapshot
            )
            resolution.provider_fingerprint = provider_fingerprint
            resolution.proposed_season_number = season_number
            resolution.proposed_episode_number = episode_number
            resolution.confidence = confidence
            resolution.web_search_performed = source == "WebSearch"
            resolution.rationale_short = (
                ai_request_result.rationale_short
                if source == "WebSearch" and ai_request_result is not None
                else rationale_short
                if source == "WebSearch"
                else None
            )
            resolution.citations = cast(list[dict[str, str]], citations)
            resolution.ai_model = ai_model
            resolution.error_code = None
            resolution.error_message = None
            resolution.resolved_at = datetime.now(tz=JST)
            # manual_* は上書きしない（手動レーン保持）。save() は全列を書き戻す。
            await resolution.save(using_db=connection)
            if ai_request is not None:
                if ai_request_status is None:
                    raise RuntimeError("Episode AI request final status is missing.")
                await cls._finishAIRequest(
                    ai_request,
                    status=ai_request_status,
                    result=ai_request_result,
                    error=ai_request_error,
                    selected_choice_id=selected_choice_id,
                    connection=connection,
                )
        return True

    @classmethod
    async def _markResolutionState(
        cls,
        *,
        snapshot: _EpisodeProgramSnapshot,
        resolution_id: int,
        status: Literal["Pending", "Unknown", "NotNumbered", "NeedsReview", "Failed"],
        source: Literal["Local", "WebSearch", "Migration"],
        provider_fingerprint: str | None,
        error_code: str | None,
        result: EpisodeLookupResult | None = None,
        lookup_outcome: EpisodeLookupOutcome | None = None,
        input_fingerprint: str | None = None,
        ai_request: RecordedSeriesAIRequest | None = None,
        ai_request_status: Literal["Succeeded", "Failed", "Rejected"] | None = None,
        ai_request_error: RecordedSeriesAIError | None = None,
        selected_choice_id: str | None = None,
        allow_manual_overwrite: bool = False,
    ) -> bool:
        """構造化Episodeを作らない判定結果も、手動判断を保護して永続化する。

        Args:
            allow_manual_overwrite: 単票再検索の受理結果で Manual を置き換えるか。
        """

        citations = (
            [
                _EpisodeCitationRecord(url=citation.url, title=citation.title)
                for citation in GetEpisodeLookupEvidence(result)
            ]
            if result is not None
            else []
        )
        async with transactions.in_transaction() as connection:
            recorded_program = (
                await RecordedProgram.filter(id=snapshot.id)
                .select_for_update()
                .using_db(connection)
                .first()
            )
            resolution = (
                await RecordedEpisodeResolution.filter(id=resolution_id)
                .select_for_update()
                .using_db(connection)
                .first()
            )
            if recorded_program is None or resolution is None:
                raise _RecordedEpisodeSnapshotChanged
            if resolution.source == 'Manual' and allow_manual_overwrite is False:
                if ai_request is not None:
                    await cls._finishAIRequest(
                        ai_request,
                        status="Rejected",
                        result=result,
                        error=RecordedSeriesAIError("ManualAssignmentWonRace"),
                        selected_choice_id=selected_choice_id,
                        connection=connection,
                    )
                return False
            # 単票再検索で Manual 正本を AI へ切り替える前に、手動レーンへ現状を退避する。
            if resolution.source == 'Manual' and allow_manual_overwrite is True:
                await cls._snapshotManualLaneIfNeeded(resolution, connection)
            current_snapshot = _snapshotFromProgram(recorded_program)
            if current_snapshot is None or _buildInputFingerprint(
                current_snapshot
            ) != _buildInputFingerprint(snapshot):
                raise _RecordedEpisodeSnapshotChanged

            resolution.episode_id = None
            resolution.status = status
            resolution.source = source
            resolution.lookup_outcome = lookup_outcome or (
                result.outcome if result is not None else None
            )
            resolution.input_fingerprint = input_fingerprint or _buildInputFingerprint(
                snapshot
            )
            resolution.provider_fingerprint = provider_fingerprint
            resolution.proposed_season_number = (
                result.season_number if result is not None else None
            )
            resolution.proposed_episode_number = (
                result.episode_number if result is not None else None
            )
            resolution.confidence = result.confidence if result is not None else None
            resolution.web_search_performed = (
                result.web_search_performed if result is not None else False
            )
            resolution.rationale_short = (
                result.rationale_short if result is not None else None
            )
            resolution.citations = cast(list[dict[str, str]], citations)
            resolution.ai_model = result.model if result is not None else None
            resolution.error_code = error_code
            resolution.error_message = GetRecordedEpisodeErrorMessage(error_code)
            resolution.resolved_at = datetime.now(tz=JST)
            await resolution.save(using_db=connection)
            if ai_request is not None:
                if ai_request_status is None:
                    raise RuntimeError("Episode AI request final status is missing.")
                await cls._finishAIRequest(
                    ai_request,
                    status=ai_request_status,
                    result=result,
                    error=ai_request_error,
                    selected_choice_id=selected_choice_id,
                    connection=connection,
                )
        return True

    @classmethod
    async def _applyNotNumbered(
        cls,
        *,
        snapshot: _EpisodeProgramSnapshot,
        resolution_id: int,
        provider_fingerprint: str,
        result: EpisodeLookupResult,
        ai_request: RecordedSeriesAIRequest,
        selected_choice_id: str,
        input_fingerprint: str | None = None,
        allow_manual_overwrite: bool = False,
    ) -> bool:
        """公式話数なしの受理時に、旧Episodeリンクと自動話数を原子的に解除する。

        Args:
            allow_manual_overwrite: 単票再検索の受理結果で Manual を WebSearch へ
                置き換えることを許可するか。
        """

        citations = [
            _EpisodeCitationRecord(url=citation.url, title=citation.title)
            for citation in GetEpisodeLookupEvidence(result)
        ]
        async with transactions.in_transaction() as connection:
            recorded_program = (
                await RecordedProgram.filter(id=snapshot.id)
                .select_for_update()
                .using_db(connection)
                .first()
            )
            resolution = (
                await RecordedEpisodeResolution.filter(id=resolution_id)
                .select_for_update()
                .using_db(connection)
                .first()
            )
            if recorded_program is None or resolution is None:
                raise _RecordedEpisodeSnapshotChanged
            if resolution.source == 'Manual' and allow_manual_overwrite is False:
                await cls._finishAIRequest(
                    ai_request,
                    status="Rejected",
                    result=result,
                    error=RecordedSeriesAIError("ManualAssignmentWonRace"),
                    selected_choice_id=selected_choice_id,
                    connection=connection,
                )
                return False
            # 単票再検索で Manual 正本を AI へ切り替える前に、手動レーンへ現状を退避する。
            if resolution.source == 'Manual' and allow_manual_overwrite is True:
                await cls._snapshotManualLaneIfNeeded(resolution, connection)
            current_snapshot = _snapshotFromProgram(recorded_program)
            if current_snapshot is None or _buildInputFingerprint(
                current_snapshot
            ) != _buildInputFingerprint(snapshot):
                raise _RecordedEpisodeSnapshotChanged

            # legacy値を残すと次回のローカル解析で旧Episodeが復活するため、リンクと一緒に消す。
            recorded_program.series_episode_id = None
            recorded_program.episode_number = None
            await recorded_program.save(
                update_fields=["series_episode_id", "episode_number", "updated_at"],
                using_db=connection,
            )

            resolution.episode_id = None
            resolution.status = "NotNumbered"
            resolution.source = "WebSearch"
            resolution.lookup_outcome = "NotNumbered"
            resolution.input_fingerprint = input_fingerprint or _buildInputFingerprint(
                _snapshotFromProgram(recorded_program) or snapshot
            )
            resolution.provider_fingerprint = provider_fingerprint
            resolution.proposed_season_number = None
            resolution.proposed_episode_number = None
            resolution.confidence = result.confidence
            resolution.web_search_performed = True
            resolution.rationale_short = result.rationale_short
            resolution.citations = cast(list[dict[str, str]], citations)
            resolution.ai_model = result.model
            resolution.error_code = None
            resolution.error_message = None
            resolution.resolved_at = datetime.now(tz=JST)
            await resolution.save(using_db=connection)
            await cls._finishAIRequest(
                ai_request,
                status="Succeeded",
                result=result,
                selected_choice_id=selected_choice_id,
                connection=connection,
            )
        return True

    @classmethod
    async def _recordPreservedLookupResult(
        cls,
        *,
        snapshot: _EpisodeProgramSnapshot,
        resolution_id: int,
        input_fingerprint: str,
        provider_fingerprint: str,
        result: EpisodeLookupResult,
        ai_request: RecordedSeriesAIRequest,
        selected_choice_id: str | None,
        ai_request_status: Literal["Succeeded", "Failed", "Rejected"] | None = None,
        ai_request_error: RecordedSeriesAIError | None = None,
    ) -> bool:
        """確定値を変えず、単票再検索の lookup 監査と提案だけを保存する。

        Local / EPG / Migration / Manual の確定値は Web 検索の成功時にも
        自動上書きしない。失敗時は既存の提案・引用も維持する。
        """

        async with transactions.in_transaction() as connection:
            recorded_program = (
                await RecordedProgram.filter(id=snapshot.id)
                .select_for_update()
                .using_db(connection)
                .first()
            )
            resolution = (
                await RecordedEpisodeResolution.filter(id=resolution_id)
                .select_for_update()
                .using_db(connection)
                .first()
            )
            if recorded_program is None or resolution is None:
                raise _RecordedEpisodeSnapshotChanged
            current_snapshot = _snapshotFromProgram(recorded_program)
            if current_snapshot is None or _buildInputFingerprint(
                current_snapshot
            ) != _buildInputFingerprint(snapshot):
                raise _RecordedEpisodeSnapshotChanged

            resolution.lookup_outcome = result.outcome
            resolution.input_fingerprint = input_fingerprint
            resolution.provider_fingerprint = provider_fingerprint
            resolution.ai_model = result.model
            # acceptance policy の不受理理由は AI 監査へだけ記録する。
            # 有効な InsufficientEvidence outcome の resolution error は空に保つ。
            resolution.error_code = result.error_code
            resolution.error_message = GetRecordedEpisodeErrorMessage(
                resolution.error_code
            )
            if result.outcome in {"Resolved", "NotNumbered", "InsufficientEvidence"}:
                # 提案付き outcome だけ evidence を今回の検索結果へ差し替える。
                citations = [
                    _EpisodeCitationRecord(url=citation.url, title=citation.title)
                    for citation in GetEpisodeLookupEvidence(result)
                ]
                resolution.web_search_performed = result.web_search_performed
                resolution.proposed_season_number = result.season_number
                resolution.proposed_episode_number = result.episode_number
                resolution.confidence = result.confidence
                resolution.rationale_short = result.rationale_short
                resolution.citations = cast(list[dict[str, str]], citations)
            else:
                # 失敗時は確定値・前回成功 evidence を維持する。
                # result.web_search_performed=False で上書きすると出典とフラグが矛盾する。
                resolution.rationale_short = None
            resolution.resolved_at = datetime.now(tz=JST)
            await resolution.save(using_db=connection)

            request_status: Literal["Succeeded", "Failed", "Rejected"]
            if ai_request_status is not None:
                request_status = ai_request_status
            elif result.outcome in {"Resolved", "NotNumbered", "InsufficientEvidence"}:
                request_status = "Succeeded"
            elif result.outcome in {
                "SearchNotRun",
                "InvalidModelOutput",
                "RateLimited",
            }:
                request_status = "Rejected"
            else:
                request_status = "Failed"
            await cls._finishAIRequest(
                ai_request,
                status=request_status,
                result=result,
                error=ai_request_error,
                selected_choice_id=selected_choice_id,
                connection=connection,
            )
        return True

    @classmethod
    async def _recordPreflightOutcome(
        cls,
        *,
        snapshot: _EpisodeProgramSnapshot,
        resolution: RecordedEpisodeResolution,
        input_fingerprint: str,
        provider_fingerprint: str,
        outcome: Literal["Disabled", "RateLimited", "SearchNotRun"],
        error_code: str,
    ) -> None:
        """検索開始前の停止理由を、確定済み値を壊さず lookup 監査へ保存する。"""

        if resolution.source == "Manual" or resolution.status in {
            "Resolved",
            "NotNumbered",
        }:
            # 確定値・Manual の preflight 失敗でも前回成功 evidence は残す。
            # web_search_performed だけ False にすると出典表示と矛盾する。
            resolution.lookup_outcome = outcome
            resolution.input_fingerprint = input_fingerprint
            resolution.provider_fingerprint = provider_fingerprint
            resolution.rationale_short = None
            resolution.error_code = error_code
            resolution.error_message = GetRecordedEpisodeErrorMessage(error_code)
            await resolution.save(
                update_fields=[
                    "lookup_outcome",
                    "input_fingerprint",
                    "provider_fingerprint",
                    "rationale_short",
                    "error_code",
                    "error_message",
                    "updated_at",
                ]
            )
            return
        mapped_status = MapLookupOutcomeToResolutionStatus(outcome)
        assert mapped_status in {"Unknown", "NeedsReview"}
        await cls._markResolutionState(
            snapshot=snapshot,
            resolution_id=resolution.id,
            status=cast(Literal["Unknown", "NeedsReview"], mapped_status),
            source="Local",
            provider_fingerprint=provider_fingerprint,
            error_code=error_code,
            lookup_outcome=outcome,
            input_fingerprint=input_fingerprint,
        )

    @classmethod
    async def _recordSnapshotChangedOutcome(
        cls,
        *,
        resolution_id: int,
        ai_request: RecordedSeriesAIRequest,
        error_code: Literal["InputChangedBeforeRequest", "InputChangedAfterRequest"],
        result: EpisodeLookupResult | None = None,
    ) -> None:
        """入力競合で破棄した lookup を Pending のまま残さず原子的に閉じる。"""

        async with transactions.in_transaction() as connection:
            resolution = (
                await RecordedEpisodeResolution.filter(id=resolution_id)
                .select_for_update()
                .using_db(connection)
                .first()
            )
            if resolution is None:
                await cls._finishAIRequest(
                    ai_request,
                    status="Failed",
                    result=result,
                    error=RecordedSeriesAIError(error_code),
                    connection=connection,
                )
                return

            if (
                resolution.source == "Manual"
                and resolution.lookup_outcome != "Pending"
            ):
                # 検索中に新しい手動確定が競合した場合は、lookup 表示も手動側の
                # 状態を一切変更しない。開始時から Manual だった override 検索は
                # Pending のため、status/source/Episode を保ったまま下で閉じる。
                await cls._finishAIRequest(
                    ai_request,
                    status="Rejected",
                    result=result,
                    error=RecordedSeriesAIError("ManualAssignmentWonRace"),
                    connection=connection,
                )
                return

            if resolution.status not in {"Resolved", "NotNumbered"}:
                resolution.status = "Unknown"
            resolution.lookup_outcome = "Cancelled"
            resolution.web_search_performed = (
                result.web_search_performed if result is not None else False
            )
            resolution.rationale_short = None
            resolution.error_code = error_code
            resolution.error_message = GetRecordedEpisodeErrorMessage(error_code)
            await resolution.save(
                update_fields=[
                    "status",
                    "lookup_outcome",
                    "web_search_performed",
                    "rationale_short",
                    "error_code",
                    "error_message",
                    "updated_at",
                ],
                using_db=connection,
            )
            await cls._finishAIRequest(
                ai_request,
                status="Failed",
                result=result,
                error=RecordedSeriesAIError(error_code),
                connection=connection,
            )

    @classmethod
    async def resolveProgram(
        cls,
        recorded_program_id: int,
        *,
        force: bool = False,
        allow_disabled: bool = False,
        allow_legacy_ai: bool = False,
        override_manual: bool = False,
        apply_accepted_lookup: bool = False,
        expected_series_id: int | None = None,
        expected_series_episode_id: int | None = None,
        expected_provider_fingerprint: str | None = None,
    ) -> RecordedEpisodeAutomationResult:
        """Series確定録画を、既存値解析から必要時のWeb検索まで順に判定する。

        Args:
            recorded_program_id: 判定対象RecordedProgram ID。
            force: 手動訂正を除く前回結果を再利用せず検索し直すか。
            allow_disabled: 全体自動判定OFFでも手動一括処理として実行するか。
            allow_legacy_ai: migration前から存在する録画に有料検索を許可するか。
            override_manual: Manual でも検索を明示許可するか。
            apply_accepted_lookup: 単票再検索として、受理結果を WebSearch 正本へ
                反映するか。True のとき Manual / Local / EPG / Migration の確定値も
                受理成功時は AI 判定で置き換える。失敗時は既存値を維持する。
            expected_series_id: 単票受付時点の Series ID。指定時は不一致を拒否する。
            expected_series_episode_id: 単票受付時点の Episode ID。
            expected_provider_fingerprint: 手動検索受付時に能力検証した provider。

        Returns:
            確定状態、出典、AI呼び出し有無。
        """

        async with RECORDED_SERIES_RESOLUTION_LOCK:
            snapshot = await cls._loadSnapshot(recorded_program_id)
            context = await BuildRecordedEpisodeLookupContext(recorded_program_id)
            if snapshot is None or context is None:
                if expected_series_id is not None:
                    raise _RecordedEpisodeSnapshotChanged
                return RecordedEpisodeAutomationResult(
                    recorded_program_id,
                    "Skipped",
                    "MissingOrNotSeries",
                    False,
                )
            if expected_series_id is not None and (
                snapshot.series_id != expected_series_id
                or snapshot.series_episode_id != expected_series_episode_id
            ):
                raise _RecordedEpisodeSnapshotChanged
            input_fingerprint = BuildEpisodeInputFingerprint(context)
            resolution = await cls._getOrCreateResolution(snapshot)

            # Manual は通常スキップする。単票の明示再検索だけ override / 受理反映を許可する。
            if resolution.source == 'Manual' and override_manual is False:
                return RecordedEpisodeAutomationResult(
                    recorded_program_id,
                    "Resolved" if resolution.status == "Resolved" else "Skipped",
                    "Manual",
                    False,
                )

            refreshing_confirmed = force and resolution.status in {
                "Resolved",
                "NotNumbered",
            }
            # 単票再検索 (apply_accepted_lookup) では、受理成功時に AI 結果を正本化する。
            # それ以外の force 再検索は Local / EPG / Migration / Manual を壊さない。
            preserving_deterministic_value = (
                force
                and apply_accepted_lookup is False
                and resolution.status in {'Resolved', 'NotNumbered'}
                and resolution.source in {'Local', 'EPG', 'Migration', 'Manual', 'AI'}
            )
            preserving_current_value = (
                resolution.source == 'Manual' and apply_accepted_lookup is False
            ) or preserving_deterministic_value
            parsed_episode = ParseLegacyEpisodeNumber(snapshot.legacy_episode_number)

            # 既存の構造化 Episode は通常処理で再課金せず、壊れた Resolution だけを修復する。
            if force is False and snapshot.series_episode_id is not None:
                episode = await SeriesEpisode.filter(
                    id=snapshot.series_episode_id,
                    series_id=snapshot.series_id,
                ).first()
                structured_value_matches = (
                    resolution.source in {"WebSearch", "EPG", "AI"}
                    or parsed_episode is None
                    or (
                        episode is not None
                        and parsed_episode.season_number == episode.season_number
                        and parsed_episode.episode_number == episode.episode_number
                    )
                )
                if episode is not None and structured_value_matches:
                    if (
                        resolution.status == "Resolved"
                        and resolution.episode_id == episode.id
                    ):
                        return RecordedEpisodeAutomationResult(
                            recorded_program_id,
                            "Resolved",
                            "StructuredCache",
                            False,
                        )
                    applied = await cls._applyResolvedEpisode(
                        snapshot=snapshot,
                        resolution_id=resolution.id,
                        season_number=episode.season_number,
                        episode_number=episode.episode_number,
                        source="Migration"
                        if resolution.is_legacy_recording
                        else "Local",
                        provider_fingerprint=None,
                        confidence=None,
                        citations=[],
                        ai_model=None,
                        write_legacy_value=False,
                    )
                    return RecordedEpisodeAutomationResult(
                        recorded_program_id,
                        "Resolved" if applied else "Skipped",
                        "StructuredCache",
                        False,
                    )

            # Local / Migration の決定論的解析を AI より先に確定する。
            if force is False and parsed_episode is not None:
                applied = await cls._applyResolvedEpisode(
                    snapshot=snapshot,
                    resolution_id=resolution.id,
                    season_number=parsed_episode.season_number,
                    episode_number=parsed_episode.episode_number,
                    source="Migration" if resolution.is_legacy_recording else "Local",
                    provider_fingerprint=None,
                    confidence=None,
                    citations=[],
                    ai_model=None,
                    write_legacy_value=False,
                )
                return RecordedEpisodeAutomationResult(
                    recorded_program_id,
                    "Resolved" if applied else "Skipped",
                    "LegacyMetadata",
                    False,
                )

            # 既存録画を暗黙に有料検索へ送らず、単票の明示操作だけ例外にする。
            if resolution.is_legacy_recording and allow_legacy_ai is False:
                if resolution.source == "WebSearch" and resolution.status in {
                    "NotNumbered",
                    "NeedsReview",
                    "Failed",
                }:
                    return RecordedEpisodeAutomationResult(
                        recorded_program_id,
                        cast(
                            Literal["NotNumbered", "NeedsReview", "Failed"],
                            resolution.status,
                        ),
                        "LegacyCache",
                        False,
                        resolution.error_code,
                    )
                if resolution.status != "NeedsReview":
                    await cls._markResolutionState(
                        snapshot=snapshot,
                        resolution_id=resolution.id,
                        status="NeedsReview",
                        source="Migration",
                        provider_fingerprint=None,
                        error_code=(
                            "LegacyEpisodeMissing"
                            if snapshot.legacy_episode_number is None
                            else "LegacyEpisodeUnknown"
                        ),
                        input_fingerprint=input_fingerprint,
                    )
                return RecordedEpisodeAutomationResult(
                    recorded_program_id,
                    "Skipped",
                    "LegacyRequiresManualBackfill",
                    False,
                    "LegacyRequiresManualBackfill",
                )

            settings, _ = RecordedSeriesSettingsStore.getSettingsAndAPIKey()
            audit_model = get_audit_model(settings)
            provider_fingerprint = get_episode_lookup_provider_fingerprint(
                settings,
                (
                    None  # OpenCode: API key not on recorded-series settings
                ),
            )

            # 同一 context / provider の終端結果は明示再検索まで再利用する。
            if (
                force is False
                and resolution.input_fingerprint == input_fingerprint
                and resolution.provider_fingerprint == provider_fingerprint
                and resolution.status in {"NotNumbered", "NeedsReview", "Failed"}
            ):
                return RecordedEpisodeAutomationResult(
                    recorded_program_id,
                    cast(
                        Literal["NotNumbered", "NeedsReview", "Failed"],
                        resolution.status,
                    ),
                    "Cache",
                    False,
                    resolution.error_code,
                )

            unavailable: tuple[
                Literal["Disabled", "RateLimited", "SearchNotRun"],
                str,
            ] | None = None
            if settings.enabled is False and allow_disabled is False:
                unavailable = ("Disabled", "RecordedSeriesIsDisabled")
            elif settings.ai_enabled is False:
                unavailable = ("Disabled", "AIIsDisabled")
            elif settings.ai_episode_number_search_enabled is False:
                unavailable = ("Disabled", "AIEpisodeNumberSearchIsDisabled")
            elif expected_provider_fingerprint is not None and (
                provider_fingerprint != expected_provider_fingerprint
                or has_episode_lookup_capability_proof(
                    settings,
                    (
                        None  # OpenCode: API key not on recorded-series settings
                    ),
                )
                is False
            ):
                unavailable = (
                    "SearchNotRun",
                    "EpisodeLookupCapabilityNotVerified",
                )
            if unavailable is not None:
                outcome, error_code = unavailable
                await cls._recordPreflightOutcome(
                    snapshot=snapshot,
                    resolution=resolution,
                    input_fingerprint=input_fingerprint,
                    provider_fingerprint=provider_fingerprint,
                    outcome=outcome,
                    error_code=error_code,
                )
                return RecordedEpisodeAutomationResult(
                    recorded_program_id,
                    "Skipped",
                    (
                        "CapabilityNotVerified"
                        if outcome == "SearchNotRun"
                        else "Disabled"
                    ),
                    False,
                    error_code,
                )

            # 日次上限は廃止。月次 cost/token 上限は Phase 3 の AIBackend 台帳で扱う。

            # 確定値はそのまま表示し、lookup 監査だけを Pending にする。
            # 前回成功時の evidence（web_search_performed / citations / proposed_*）は
            # 新しい試行の in-flight でも残す。フラグだけ False にすると UI が
            # 「未実行なのに出典あり」となり、失敗時の preserve 契約とも矛盾する。
            if preserving_current_value is False and refreshing_confirmed is False:
                resolution.status = "Pending"
                resolution.source = "WebSearch"
            resolution.lookup_outcome = "Pending"
            resolution.input_fingerprint = input_fingerprint
            resolution.provider_fingerprint = provider_fingerprint
            resolution.ai_model = audit_model
            # 直前試行の失敗表示・根拠は新試行開始で消し、error と根拠の同時表示を避ける。
            resolution.rationale_short = None
            resolution.error_code = None
            resolution.error_message = None
            await resolution.save()
            ai_request = await RecordedSeriesAIRequest.create(
                resolution_id=None,
                episode_resolution_id=resolution.id,
                purpose="EpisodeLookup",
                status="Pending",
                model=audit_model,
                input_fingerprint=input_fingerprint,
                candidate_set_hash=None,
                candidate_ids=[],
                selected_choice_id=None,
                prompt_tokens=None,
                completion_tokens=None,
                http_status=None,
                latency_ms=None,
                error_code=None,
            )

            latest_context = await BuildRecordedEpisodeLookupContext(
                recorded_program_id
            )
            if (
                latest_context is None
                or BuildEpisodeInputFingerprint(latest_context) != input_fingerprint
            ):
                await cls._recordSnapshotChangedOutcome(
                    resolution_id=resolution.id,
                    ai_request=ai_request,
                    error_code="InputChangedBeforeRequest",
                )
                raise _RecordedEpisodeSnapshotChanged

        # ネットワーク / ACP subprocess 待機中は解決 lock を保持しない。
        cancelled = False
        try:
            result = await ai_lookup_episode(
                program=context,
                settings=settings,
                api_key=None,
                expected_provider_fingerprint=provider_fingerprint,
                require_capability_proof=(
                    expected_provider_fingerprint is not None
                ),
            )
        except asyncio.CancelledError:
            cancelled = True
            result = _episodeLookupErrorResult(code="Cancelled", model=audit_model)
        except RecordedSeriesAIError as ex:
            result = _episodeLookupErrorResult(
                code=ex.code,
                model=audit_model,
                http_status=ex.http_status,
                latency_ms=ex.latency_ms,
            )
        except Exception as ex:
            logging.error(
                f"[RecordedEpisodeAutomation] Episode lookup backend failed unexpectedly. "
                f"recorded_program_id: {recorded_program_id}",
                exc_info=ex,
            )
            result = _episodeLookupErrorResult(
                code="EpisodeLookupFailed",
                model=audit_model,
            )
        if result.outcome == "Pending":
            # adapter の戻り値は必ず終端でなければならず、Pending の自己申告は受理しない。
            result = _episodeLookupErrorResult(
                code="InvalidModelOutput",
                model=audit_model,
                http_status=result.http_status,
                latency_ms=result.latency_ms,
            )
        if (
            result.outcome in {"SearchFailed", "SearchNotRun", "InvalidModelOutput"}
            or (
                result.web_search_performed
                and len(GetEpisodeLookupEvidence(result)) == 0
            )
        ):
            # 接続試験後でも、実 lookup で tool/schema/source 能力が否定された
            # provider は次回の単票再検索前に再試験を要求する。
            invalidate_episode_lookup_capability_fingerprint(
                provider_fingerprint,
            )

        async with RECORDED_SERIES_RESOLUTION_LOCK:
            latest_context = await BuildRecordedEpisodeLookupContext(
                recorded_program_id
            )
            if (
                latest_context is None
                or BuildEpisodeInputFingerprint(latest_context) != input_fingerprint
            ):
                await cls._recordSnapshotChangedOutcome(
                    resolution_id=resolution.id,
                    ai_request=ai_request,
                    error_code="InputChangedAfterRequest",
                    result=result,
                )
                raise _RecordedEpisodeSnapshotChanged

            # 外部待機中に受理条件が更新されても、課金済み結果を旧 snapshot の
            # 条件で取りこぼさない。backend や認証 snapshot は呼出し時点を維持する。
            latest_settings, _latest_api_key = (
                RecordedSeriesSettingsStore.getSettingsAndAPIKey()
            )
            selected_choice_id: str | None
            if result.outcome == "NotNumbered":
                selected_choice_id = "not-numbered"
            elif result.season_number is not None and result.episode_number is not None:
                selected_choice_id = f"S{result.season_number}E{result.episode_number}"
            elif result.outcome == "InsufficientEvidence":
                selected_choice_id = "insufficient-evidence"
            else:
                selected_choice_id = None

            terminal_status: Literal["Succeeded", "Failed", "Rejected"]
            terminal_error: RecordedSeriesAIError | None = None
            if result.outcome in {"Resolved", "NotNumbered"}:
                accepted = IsEpisodeLookupResultAccepted(
                    result,
                    latest_settings.ai_episode_number_acceptance_mode,
                )
                if accepted:
                    terminal_status = "Succeeded"
                else:
                    result = replace(result, outcome="InsufficientEvidence")
                    terminal_status = "Rejected"
                    terminal_error = RecordedSeriesAIError("AcceptancePolicyRejected")
            elif result.outcome == "InsufficientEvidence":
                accepted = False
                terminal_status = "Succeeded"
            elif result.outcome in {
                "SearchNotRun",
                "InvalidModelOutput",
                "RateLimited",
            }:
                accepted = False
                terminal_status = "Rejected"
            else:
                accepted = False
                terminal_status = "Failed"

            # 単票再検索の失敗時は Manual 正本も確定済み値も壊さない。
            preserve_manual_on_failed_lookup = (
                apply_accepted_lookup
                and accepted is False
                and resolution.source == 'Manual'
            )
            # Manual / deterministic の正本、および確定済み値の失敗・不受理は置き換えない。
            # ただし単票再検索で受理された結果は WebSearch として正本化する。
            if (
                preserving_current_value
                or (refreshing_confirmed and accepted is False)
                or preserve_manual_on_failed_lookup
            ):
                await cls._recordPreservedLookupResult(
                    snapshot=snapshot,
                    resolution_id=resolution.id,
                    input_fingerprint=input_fingerprint,
                    provider_fingerprint=provider_fingerprint,
                    result=result,
                    ai_request=ai_request,
                    selected_choice_id=selected_choice_id,
                    ai_request_status=terminal_status,
                    ai_request_error=terminal_error,
                )
                automation_status: Literal[
                    "Resolved",
                    "NotNumbered",
                    "NeedsReview",
                    "Failed",
                    "Skipped",
                ]
                automation_status = (
                    "Resolved"
                    if resolution.status == "Resolved"
                    else "NotNumbered"
                    if resolution.status == "NotNumbered"
                    else "Skipped"
                )
                response = RecordedEpisodeAutomationResult(
                    recorded_program_id,
                    automation_status,
                    "WebSearchRefreshPreserved",
                    True,
                    result.error_code,
                )
            elif result.outcome == "Resolved" and accepted:
                assert result.season_number is not None
                assert result.episode_number is not None
                citation_records = [
                    _EpisodeCitationRecord(url=citation.url, title=citation.title)
                    for citation in GetEpisodeLookupEvidence(result)
                ]
                applied = await cls._applyResolvedEpisode(
                    snapshot=snapshot,
                    resolution_id=resolution.id,
                    season_number=result.season_number,
                    episode_number=result.episode_number,
                    source="WebSearch",
                    provider_fingerprint=provider_fingerprint,
                    confidence=result.confidence,
                    citations=citation_records,
                    ai_model=result.model,
                    write_legacy_value=True,
                    input_fingerprint=input_fingerprint,
                    ai_request=ai_request,
                    ai_request_status=terminal_status,
                    ai_request_result=result,
                    selected_choice_id=selected_choice_id,
                    allow_manual_overwrite=apply_accepted_lookup,
                )
                response = RecordedEpisodeAutomationResult(
                    recorded_program_id,
                    "Resolved" if applied else "Skipped",
                    "WebSearch",
                    True,
                )
            elif result.outcome == "NotNumbered" and accepted:
                applied = await cls._applyNotNumbered(
                    snapshot=snapshot,
                    resolution_id=resolution.id,
                    provider_fingerprint=provider_fingerprint,
                    result=result,
                    ai_request=ai_request,
                    selected_choice_id=selected_choice_id or "not-numbered",
                    input_fingerprint=input_fingerprint,
                    allow_manual_overwrite=apply_accepted_lookup,
                )
                response = RecordedEpisodeAutomationResult(
                    recorded_program_id,
                    "NotNumbered" if applied else "Skipped",
                    "WebSearch",
                    True,
                )
            else:
                mapped_status = MapLookupOutcomeToResolutionStatus(result.outcome)
                assert mapped_status in {"Pending", "Unknown", "NeedsReview", "Failed"}
                persistence_status = cast(
                    Literal["Pending", "Unknown", "NeedsReview", "Failed"],
                    mapped_status,
                )
                applied = await cls._markResolutionState(
                    snapshot=snapshot,
                    resolution_id=resolution.id,
                    status=persistence_status,
                    source="WebSearch",
                    provider_fingerprint=provider_fingerprint,
                    error_code=result.error_code,
                    result=result,
                    lookup_outcome=result.outcome,
                    input_fingerprint=input_fingerprint,
                    ai_request=ai_request,
                    ai_request_status=terminal_status,
                    ai_request_error=terminal_error,
                    selected_choice_id=selected_choice_id,
                    allow_manual_overwrite=apply_accepted_lookup,
                )
                if mapped_status == "NeedsReview":
                    automation_status = "NeedsReview"
                elif mapped_status == "Failed":
                    automation_status = "Failed"
                else:
                    automation_status = "Skipped"
                response = RecordedEpisodeAutomationResult(
                    recorded_program_id,
                    automation_status if applied else "Skipped",
                    "WebSearch",
                    True,
                    result.error_code,
                )

        if cancelled:
            raise asyncio.CancelledError
        return response

    @classmethod
    async def startRelookup(
        cls,
        recorded_program_id: int,
        *,
        expected_series_id: int,
        expected_series_episode_id: int | None,
        override_manual: bool = False,
    ) -> RecordedEpisodeBackfillAccepted:
        """録画1件の話数再検索を検証し、HTTP 待機外の解析タスクとして開始する。"""

        async with cls._relookup_start_lock:
            recorded_program = (
                await RecordedProgram.filter(
                    id=recorded_program_id,
                    recorded_video__status="Recorded",
                    series_id__not_isnull=True,
                )
                .prefetch_related("recorded_video")
                .first()
            )
            if recorded_program is None or recorded_program.series_id is None:
                raise RecordedEpisodeRelookupNotFoundError
            if (
                recorded_program.series_id != expected_series_id
                or recorded_program.series_episode_id != expected_series_episode_id
            ):
                raise RecordedEpisodeRelookupConflictError

            resolution = await RecordedEpisodeResolution.filter(
                recorded_program_id=recorded_program_id,
            ).first()
            if (
                resolution is not None
                and resolution.source == "Manual"
                and override_manual is False
            ):
                raise RecordedEpisodeRelookupConflictError

            existing_task = cls._relookup_tasks.get(recorded_program_id)
            existing_handle = cls._relookup_handles.get(recorded_program_id)
            if (
                existing_task is not None
                and existing_task.done() is False
                and existing_handle is not None
            ):
                return RecordedEpisodeBackfillAccepted(
                    existing_handle.execution.id, True
                )
            cls._relookup_tasks.pop(recorded_program_id, None)
            cls._relookup_handles.pop(recorded_program_id, None)

            if (
                recorded_program_id in cls._running_ids
                or recorded_program_id in cls._queued_ids
            ):
                raise RecordedEpisodeRelookupConflictError

            settings, _ = RecordedSeriesSettingsStore.getSettingsAndAPIKey()
            if (
                settings.enabled is False
                or settings.ai_enabled is False
                or settings.ai_episode_number_search_enabled is False
                or has_episode_lookup_capability_proof(
                    settings,
                    (
                        None  # OpenCode: API key not on recorded-series settings
                    ),
                )
                is False
            ):
                raise RecordedEpisodeRelookupDisabledError
            expected_provider_fingerprint = (
                get_episode_lookup_provider_fingerprint(
                    settings,
                    (
                        None  # OpenCode: API key not on recorded-series settings
                    ),
                )
            )

            # 日次上限は廃止。月次上限は Phase 3。

            handle = await AnalysisTaskTracker.start(
                "BatchEpisodeResolution",
                recorded_video_id=recorded_program.recorded_video.id,
                title=f"話数 AI 再検索: {recorded_program.title}",
                trigger="Manual",
                total_count=1,
                initial_status="Queued",
                inherit_parent=False,
            )
            task = asyncio.create_task(
                cls._runRelookup(
                    handle,
                    recorded_program_id,
                    expected_series_id=expected_series_id,
                    expected_series_episode_id=expected_series_episode_id,
                    override_manual=override_manual,
                    expected_provider_fingerprint=expected_provider_fingerprint,
                )
            )
            cls._relookup_handles[recorded_program_id] = handle
            cls._relookup_tasks[recorded_program_id] = task
            return RecordedEpisodeBackfillAccepted(handle.execution.id, False)

    @classmethod
    async def _runRelookup(
        cls,
        handle: AnalysisTaskHandle,
        recorded_program_id: int,
        *,
        expected_series_id: int,
        expected_series_episode_id: int | None,
        override_manual: bool,
        expected_provider_fingerprint: str,
    ) -> None:
        """単票再検索を進捗履歴へ結び付け、全終端で重複制御を解放する。"""

        was_cancelled = False
        try:
            async with AnalysisTaskTracker.track(
                "BatchEpisodeResolution",
                trigger="Manual",
                existing_handle=handle,
            ) as history:
                await history.setStage("Searching", 0.0)
                try:
                    result = await cls.resolveProgram(
                        recorded_program_id,
                        force=True,
                        allow_disabled=False,
                        allow_legacy_ai=True,
                        override_manual=override_manual,
                        # 単票 UI からの再検索は、受理結果を AI (WebSearch) として保存する。
                        apply_accepted_lookup=True,
                        expected_series_id=expected_series_id,
                        expected_series_episode_id=expected_series_episode_id,
                        expected_provider_fingerprint=expected_provider_fingerprint,
                    )
                except _RecordedEpisodeSnapshotChanged:
                    await history.setCounts(
                        current=1,
                        total=1,
                        failed=1,
                    )
                    await history.finish(
                        "Failed",
                        error_code="EpisodeStateChanged",
                        error_message="録画または話数割当が更新されたため、検索結果を反映しませんでした。",
                    )
                    return

                # 受付後の設定変更や上限競合による preflight 終端も成功扱いしない。
                failed = int(result.error_code is not None)
                skipped = int(result.error_code is None and result.ai_requested is False)
                succeeded = int(failed == 0 and skipped == 0)
                await history.setCounts(
                    current=1,
                    total=1,
                    succeeded=succeeded,
                    failed=failed,
                    skipped=skipped,
                )
                await history.finish(
                    "Failed" if failed > 0 else "Succeeded",
                    summary={
                        "recorded_program_id": recorded_program_id,
                        "resolution_status": result.status,
                        "source": result.source,
                        "ai_requested": result.ai_requested,
                        "error_code": result.error_code,
                    },
                    error_code=result.error_code if failed > 0 else None,
                    error_message=(
                        GetRecordedEpisodeErrorMessage(result.error_code)
                        if failed > 0
                        else None
                    ),
                )
        except asyncio.CancelledError:
            was_cancelled = True
            raise
        except Exception as ex:
            logging.error(
                f"[RecordedEpisodeAutomation] Episode relookup task failed. "
                f"recorded_program_id: {recorded_program_id}",
                exc_info=ex,
            )
        finally:
            current_task = asyncio.current_task()
            retry_after_relookup = False
            if cls._relookup_tasks.get(recorded_program_id) is current_task:
                cls._relookup_tasks.pop(recorded_program_id, None)
                cls._relookup_handles.pop(recorded_program_id, None)
                if recorded_program_id in cls._rerun_ids:
                    cls._rerun_ids.discard(recorded_program_id)
                    retry_after_relookup = True
            if retry_after_relookup and was_cancelled is False:
                await cls.enqueue(recorded_program_id)

    @classmethod
    async def promoteStoredProposals(cls) -> int:
        """Alwaysへ変更されたとき、保存済み数値提案を再課金なしでEpisodeへ昇格する。

        Returns:
            新たにEpisodeへ関連付けた録画件数。
        """

        # 旧受理条件で実行中の検索があれば完了を待つ。候補取得だけをlock外で行うと、
        # Always保存時にまだPendingだった低信頼提案を永久に取りこぼしてしまう。
        async with RECORDED_SERIES_RESOLUTION_LOCK:
            settings = RecordedSeriesSettingsStore.getSettings()
            if (
                settings.enabled is False
                or settings.ai_enabled is False
                or settings.ai_episode_number_search_enabled is False
                or settings.ai_episode_number_acceptance_mode != "Always"
            ):
                return 0
            promoted_count = 0
            proposal_ids = cast(
                list[int],
                await RecordedEpisodeResolution.filter(
                    status="NeedsReview",
                    source="WebSearch",
                    lookup_outcome="InsufficientEvidence",
                    web_search_performed=True,
                )
                .exclude(proposed_season_number=None)
                .exclude(proposed_episode_number=None)
                .order_by("id")
                .values_list("id", flat=True),
            )
            promotable_proposals: list[
                tuple[
                    _EpisodeProgramSnapshot,
                    RecordedEpisodeResolution,
                    list[_EpisodeCitationRecord],
                ]
            ] = []
            for resolution_id in proposal_ids:
                # 一覧取得後にforce検索や手動訂正が完了していても、古い提案を上書きしない。
                resolution = await RecordedEpisodeResolution.filter(
                    id=resolution_id,
                    status="NeedsReview",
                    source="WebSearch",
                    lookup_outcome="InsufficientEvidence",
                    web_search_performed=True,
                ).first()
                if (
                    resolution is None
                    or resolution.proposed_season_number is None
                    or resolution.proposed_episode_number is None
                    or resolution.error_code
                    not in {None, "AcceptancePolicyRejected", "LowConfidence"}
                ):
                    continue
                snapshot = await cls._loadSnapshot(resolution.recorded_program_id)
                context = await BuildRecordedEpisodeLookupContext(
                    resolution.recorded_program_id
                )
                if snapshot is None or context is None:
                    continue
                if resolution.input_fingerprint != BuildEpisodeInputFingerprint(
                    context
                ):
                    # 提案取得後に番組情報やSeries所属が変わった場合、旧文脈の数値は昇格しない。
                    continue
                citations = [
                    _EpisodeCitationRecord(
                        url=citation.get("url", ""),
                        title=citation.get("title", ""),
                    )
                    for citation in resolution.citations
                    if citation.get("url", "") != ""
                ]
                promotable_proposals.append((snapshot, resolution, citations))

            # 同一 Series の先行提案を Episode 化すると rich context の既知話数
            # sample が変わる。全候補の fingerprint を mutation 前に検証し終えて
            # から適用し、後続の課金済み提案を自己変化で取りこぼさない。
            for snapshot, resolution, citations in promotable_proposals:
                assert resolution.proposed_season_number is not None
                assert resolution.proposed_episode_number is not None
                applied = await cls._applyResolvedEpisode(
                    snapshot=snapshot,
                    resolution_id=resolution.id,
                    season_number=resolution.proposed_season_number,
                    episode_number=resolution.proposed_episode_number,
                    source="WebSearch",
                    provider_fingerprint=resolution.provider_fingerprint,
                    confidence=resolution.confidence,
                    citations=citations,
                    ai_model=resolution.ai_model,
                    write_legacy_value=True,
                    rationale_short=resolution.rationale_short,
                    input_fingerprint=resolution.input_fingerprint,
                )
                promoted_count += int(applied)
        return promoted_count

    @classmethod
    async def settingsUpdated(cls) -> None:
        """AI設定変更後に、無課金昇格と新規録画の保留キューだけを再評価する。"""

        # ライフサイクル開始前（ルーター単体テストを含む）は、サーバーstartup側へ初期化を委ねる。
        if cls._worker_task is None:
            return
        await cls.start()
        await cls.promoteStoredProposals()
        settings, _ = RecordedSeriesSettingsStore.getSettingsAndAPIKey()
        # OFFへの設定変更では、旧Web検索の提案・引用をUnknownへ上書きしない。
        # 再有効化時には同じsettingsUpdated()を通るため、その時点で再試行できる。
        if (
            settings.enabled is False
            or settings.ai_enabled is False
            or settings.ai_episode_number_search_enabled is False
        ):
            return
        await cls._enqueueRecoverablePrograms(
            provider_fingerprint=get_episode_lookup_provider_fingerprint(
                settings,
                (
                    None  # OpenCode: API key not on recorded-series settings
                ),
            )
        )

    @classmethod
    async def startBackfill(
        cls, *, force: bool = False
    ) -> RecordedEpisodeBackfillAccepted:
        """migration以前からある話数不明録画の手動Web検索を開始する。

        Args:
            force: 手動訂正を除く確定・失敗・低信頼・NotNumbered結果も再送するか。

        Returns:
            解析履歴IDと、進行中タスクを再利用したか。
        """

        async with cls._backfill_start_lock:
            if cls._backfill_task is not None and cls._backfill_task.done() is False:
                assert cls._backfill_handle is not None
                return RecordedEpisodeBackfillAccepted(
                    cls._backfill_handle.execution.id, True
                )

            await cls.start()
            settings, _ = RecordedSeriesSettingsStore.getSettingsAndAPIKey()
            # 一括処理を受け付けてから全件 Skipped にするのではなく、保存済み設定と
            # 接続試験で話数 Web 検索能力を確認できる場合だけ実行履歴を作成する。
            if (
                settings.ai_enabled is False
                or settings.ai_episode_number_search_enabled is False
                or has_episode_lookup_capability_proof(
                    settings,
                    (
                        None  # OpenCode: API key not on recorded-series settings
                    ),
                )
                is False
            ):
                raise RecordedEpisodeRelookupDisabledError
            provider_fingerprint = get_episode_lookup_provider_fingerprint(
                settings,
                (
                    None  # OpenCode: API key not on recorded-series settings
                ),
            )
            candidates = await cls._loadBackfillCandidateIDs(
                force=force,
                provider_fingerprint=provider_fingerprint,
            )
            handle = await AnalysisTaskTracker.start(
                "BatchEpisodeResolution",
                title="既存録画話数一括判定",
                trigger="Manual",
                total_count=len(candidates),
                initial_status="Queued",
                inherit_parent=False,
            )
            cls._backfill_handle = handle
            cls._backfill_task = asyncio.create_task(
                cls._runBackfill(
                    handle,
                    candidates,
                    force=force,
                    expected_provider_fingerprint=provider_fingerprint,
                )
            )
            return RecordedEpisodeBackfillAccepted(handle.execution.id, False)

    @classmethod
    async def _loadBackfillCandidateIDs(
        cls,
        *,
        force: bool,
        provider_fingerprint: str | None = None,
    ) -> list[int]:
        """手動実行対象を、前回結果とforce設定から固定する。"""

        # Resolution を起点にすると、旧DBや処理競合で状態行がまだ作成されていない
        # Series 所属録画を取りこぼす。録画を母集合にして、Resolution がない録画も
        # resolveProgram() 内の get_or_create を通る対象として含める。
        program_ids = cast(
            list[int],
            await RecordedProgram.filter(
                series_id__not_isnull=True,
                recorded_video__status='Recorded',
            )
            .order_by('id')
            .values_list('id', flat=True),
        )
        if len(program_ids) == 0:
            return []
        resolutions = await RecordedEpisodeResolution.filter(
            recorded_program_id__in=program_ids,
        ).order_by('recorded_program_id')
        resolutions_by_program_id = {
            resolution.recorded_program_id: resolution for resolution in resolutions
        }
        candidate_ids: list[int] = []
        for recorded_program_id in program_ids:
            resolution = resolutions_by_program_id.get(recorded_program_id)
            if resolution is None:
                candidate_ids.append(recorded_program_id)
                continue
            if resolution.source == 'Manual':
                continue
            if force:
                candidate_ids.append(recorded_program_id)
                continue
            if resolution.status == "Resolved":
                continue
            if resolution.status in {'Pending', 'Unknown'}:
                candidate_ids.append(recorded_program_id)
                continue
            if resolution.source == 'Migration' and resolution.status == 'NeedsReview':
                candidate_ids.append(recorded_program_id)
                continue
            if (
                provider_fingerprint is not None
                and resolution.source == "WebSearch"
                and resolution.status in {"NeedsReview", "Failed"}
                and resolution.provider_fingerprint != provider_fingerprint
            ):
                candidate_ids.append(recorded_program_id)
        return candidate_ids

    @classmethod
    async def _runBackfill(
        cls,
        handle: AnalysisTaskHandle,
        candidate_ids: list[int],
        *,
        force: bool,
        expected_provider_fingerprint: str,
    ) -> None:
        """1回の管理者操作につき、同じAI設定で当日の残り上限まで順次検索する。"""

        try:
            async with AnalysisTaskTracker.track(
                "BatchEpisodeResolution",
                trigger="Manual",
                existing_handle=handle,
            ) as history:
                await history.setStage("Searching", 0.0)
                succeeded_count = 0
                failed_count = 0
                skipped_count = 0
                resolved_count = 0
                not_numbered_count = 0
                needs_review_count = 0
                preserved_count = 0
                ai_request_count = 0
                stopped_by_daily_limit = False
                for index, recorded_program_id in enumerate(candidate_ids, start=1):
                    try:
                        result = await cls.resolveProgram(
                            recorded_program_id,
                            force=force,
                            allow_disabled=True,
                            allow_legacy_ai=True,
                            expected_provider_fingerprint=expected_provider_fingerprint,
                        )
                    except asyncio.CancelledError:
                        raise
                    except _RecordedEpisodeSnapshotChanged:
                        skipped_count += 1
                        await history.setCounts(
                            current=index,
                            total=len(candidate_ids),
                            succeeded=succeeded_count,
                            failed=failed_count,
                            skipped=skipped_count,
                        )
                        continue
                    except Exception as ex:
                        failed_count += 1
                        logging.error(
                            f"[RecordedEpisodeAutomation] Episode backfill item failed. "
                            f"recorded_program_id: {recorded_program_id}",
                            exc_info=ex,
                        )
                        await history.setCounts(
                            current=index,
                            total=len(candidate_ids),
                            succeeded=succeeded_count,
                            failed=failed_count,
                            skipped=skipped_count,
                        )
                        continue

                    ai_request_count += int(result.ai_requested)
                    preserved_count += int(result.source == "WebSearchRefreshPreserved")
                    if result.status == "Resolved":
                        succeeded_count += 1
                        resolved_count += 1
                    elif result.status == "NotNumbered":
                        succeeded_count += 1
                        not_numbered_count += 1
                    elif result.status == "NeedsReview":
                        succeeded_count += 1
                        needs_review_count += 1
                    elif result.status == "Failed":
                        failed_count += 1
                    else:
                        skipped_count += 1
                    await history.setCounts(
                        current=index,
                        total=len(candidate_ids),
                        succeeded=succeeded_count,
                        failed=failed_count,
                        skipped=skipped_count,
                    )
                    if result.error_code == "DailyAIRequestLimitReached":
                        # 翌日に自動再開せず、残りは次回の明示操作まで保留する。
                        stopped_by_daily_limit = True
                        skipped_count += len(candidate_ids) - index
                        await history.setCounts(
                            current=len(candidate_ids),
                            total=len(candidate_ids),
                            succeeded=succeeded_count,
                            failed=failed_count,
                            skipped=skipped_count,
                        )
                        break

                await history.finish(
                    "Failed" if failed_count > 0 else "Succeeded",
                    summary=cast(
                        dict[str, object],
                        {
                            "resolved_or_reviewed": succeeded_count,
                            "resolved": resolved_count,
                            "not_numbered": not_numbered_count,
                            "needs_review": needs_review_count,
                            "preserved": preserved_count,
                            "failed": failed_count,
                            "skipped": skipped_count,
                            "ai_requests": ai_request_count,
                            "stopped_by_daily_limit": stopped_by_daily_limit,
                        },
                    ),
                    error_code="PartialFailure" if failed_count > 0 else None,
                )
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logging.error(
                "[RecordedEpisodeAutomation] Episode backfill failed:", exc_info=ex
            )
        finally:
            cls._backfill_task = None
            cls._backfill_handle = None

    @classmethod
    async def getStatus(cls) -> dict[str, int | str | bool | None]:
        """管理画面向けに、Series所属録画の話数構造化状態を集計する。"""

        series_program_count = await RecordedProgram.filter(
            series_id__not_isnull=True,
            recorded_video__status="Recorded",
        ).count()
        status_counts = {
            status: await RecordedEpisodeResolution.filter(
                recorded_program__series_id__not_isnull=True,
                recorded_program__recorded_video__status="Recorded",
                status=status,
            ).count()
            for status in (
                "Pending",
                "Resolved",
                "Unknown",
                "NotNumbered",
                "NeedsReview",
                "Failed",
            )
        }
        known_resolution_count = sum(status_counts.values())
        unknown_count = (
            status_counts["Pending"]
            + status_counts["Unknown"]
            + max(0, series_program_count - known_resolution_count)
        )
        last_resolution = (
            await RecordedEpisodeResolution.filter(
                recorded_program__series_id__not_isnull=True,
                recorded_program__recorded_video__status="Recorded",
            )
            .order_by("-updated_at")
            .first()
        )
        return {
            "episode_resolved": status_counts["Resolved"],
            "episode_unknown": unknown_count,
            "episode_not_numbered": status_counts["NotNumbered"],
            "episode_needs_review": status_counts["NeedsReview"],
            "episode_failed": status_counts["Failed"],
            "episode_last_run_at": (
                last_resolution.updated_at.isoformat()
                if last_resolution is not None
                else None
            ),
            "is_episode_running": (
                cls._backfill_task is not None and cls._backfill_task.done() is False
            )
            or any(task.done() is False for task in cls._relookup_tasks.values()),
        }
