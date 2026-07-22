from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from datetime import time as datetime_time
from decimal import Decimal
from typing import Literal, cast

from tortoise import transactions
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.expressions import Q
from typing_extensions import TypedDict

from app import logging
from app.constants import JST
from app.metadata.AnalysisTaskTracker import AnalysisTaskHandle, AnalysisTaskTracker
from app.metadata.RecordedEpisodeResolver import (
    FormatEpisodeNumber,
    ParseLegacyEpisodeNumber,
)
from app.metadata.RecordedEpisodeSearch import (
    AIEpisodeLookupResult,
    GetEpisodeLookupEvidence,
    IsEpisodeLookupResultAccepted,
    RecordedEpisodeProgramPrompt,
    SearchRecordedEpisodeNumber,
)
from app.metadata.RecordedSeriesCandidates import RecordedSeriesAIError
from app.metadata.RecordedSeriesLocks import RECORDED_SERIES_RESOLUTION_LOCK
from app.metadata.RecordedSeriesSettings import RecordedSeriesSettingsStore
from app.models.RecordedEpisode import RecordedEpisodeResolution, SeriesEpisode
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedSeries import RecordedSeriesAIRequest


RECORDED_EPISODE_AUTOMATION_VERSION = '1'


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
    status: Literal['Resolved', 'NotNumbered', 'NeedsReview', 'Failed', 'Skipped']
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


def _sha256JSON(payload: object) -> str:
    """順序を固定したJSONから、秘密情報を含まないfingerprintを作る。"""

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def _buildInputFingerprint(snapshot: _EpisodeProgramSnapshot) -> str:
    """話数の決定に影響する録画入力だけをfingerprintへ含める。"""

    return _sha256JSON({
        'automation_version': RECORDED_EPISODE_AUTOMATION_VERSION,
        'series_id': snapshot.series_id,
        'series_title': snapshot.series_title,
        'legacy_episode_number': snapshot.legacy_episode_number,
        'title': snapshot.title,
        'subtitle': snapshot.subtitle,
        'description': snapshot.description,
        'detail': snapshot.detail,
        'channel_id': snapshot.channel_id,
        'start_time': snapshot.start_time.isoformat(),
    })


def _buildProviderFingerprint(
    *,
    api_base_url: str,
    model: str,
    api_key: str | None,
) -> str:
    """provider変更時だけ再試行できるよう、APIキー自体を保持せず識別する。"""

    return _sha256JSON({
        'automation_version': RECORDED_EPISODE_AUTOMATION_VERSION,
        'api_base_url': api_base_url,
        'model': model,
        'api_key_hash': hashlib.sha256(api_key.encode('utf-8')).hexdigest() if api_key is not None else None,
    })


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


class RecordedEpisodeAutomation:
    """Series確定後の決定論的話数解析と有料Web検索を独立キューで管理する。"""

    _queue: asyncio.Queue[int] | None = None
    _worker_task: asyncio.Task[None] | None = None
    _queued_ids: set[int] = set()
    _running_ids: set[int] = set()
    _rerun_ids: set[int] = set()
    _backfill_task: asyncio.Task[None] | None = None
    _backfill_handle: AnalysisTaskHandle | None = None
    _start_lock = asyncio.Lock()
    _backfill_start_lock = asyncio.Lock()
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

        tasks = [task for task in (cls._backfill_task, cls._worker_task) if task is not None]
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
                    f'[RecordedEpisodeAutomation] Failed to resolve episode. '
                    f'recorded_program_id: {recorded_program_id}',
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
                purpose='EpisodeLookup',
                status='Pending',
            ).using_db(connection)
            for request in pending_requests:
                if request.episode_resolution_id is not None:
                    resolution = await RecordedEpisodeResolution.filter(
                        id=request.episode_resolution_id,
                    ).using_db(connection).first()
                    # force再検索中の確定値はResolutionをResolvedのまま保持する。
                    # 再起動で監査だけがPendingに残っても、最後の確定値をFailedで壊さない。
                    if (
                        resolution is not None and
                        resolution.source != 'Manual' and
                        resolution.status != 'Resolved'
                    ):
                        resolution.status = 'Failed'
                        resolution.source = 'WebSearch'
                        resolution.error_code = 'AIRequestInterrupted'
                        resolution.error_message = None
                        await resolution.save(
                            update_fields=[
                                'status',
                                'source',
                                'error_code',
                                'error_message',
                                'updated_at',
                            ],
                            using_db=connection,
                        )
                request.status = 'Failed'
                request.error_code = 'AIRequestInterrupted'
                await request.save(
                    update_fields=['status', 'error_code'],
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
        program_ids = cast(list[int], await RecordedProgram.filter(
            series_id__not_isnull=True,
            recorded_video__status='Recorded',
        ).values_list('id', flat=True))
        if len(program_ids) == 0:
            return
        resolutions = await RecordedEpisodeResolution.filter(
            recorded_program_id__in=program_ids,
        ).all()
        resolutions_by_program_id = {
            resolution.recorded_program_id: resolution
            for resolution in resolutions
        }
        for recorded_program_id in program_ids:
            resolution = resolutions_by_program_id.get(recorded_program_id)
            if resolution is not None:
                # migrationでseedされた既存録画は、手動一括実行まで有料検索へ進めない。
                if resolution.is_legacy_recording or resolution.source == 'Manual':
                    continue
                retry_for_provider_change = (
                    provider_fingerprint is not None and
                    resolution.source == 'WebSearch' and
                    resolution.status in {'NeedsReview', 'Failed'} and
                    resolution.provider_fingerprint != provider_fingerprint
                )
                if resolution.status not in {'Pending', 'Unknown'} and retry_for_provider_change is False:
                    continue
            # provider変更と旧providerへの実行中リクエストが重なった場合、完了後に新設定で
            # もう一度評価する。旧処理が成功していればStructuredCacheとなるため再課金はしない。
            if recorded_program_id in cls._running_ids:
                cls._rerun_ids.add(recorded_program_id)
                continue
            if recorded_program_id in cls._queued_ids:
                continue
            cls._queued_ids.add(recorded_program_id)
            await cls._queue.put(recorded_program_id)

    @classmethod
    async def _loadSnapshot(cls, recorded_program_id: int) -> _EpisodeProgramSnapshot | None:
        """再生可能かつSeries所属中の録画を、最新の判定入力として取得する。"""

        recorded_program = await RecordedProgram.filter(
            id=recorded_program_id,
            recorded_video__status='Recorded',
        ).prefetch_related('channel').first()
        if recorded_program is None:
            return None
        channel_name = recorded_program.channel.name if recorded_program.channel is not None else None
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
                'episode_id': snapshot.series_episode_id,
                'status': 'Resolved' if snapshot.series_episode_id is not None else 'Pending',
                'source': 'Local',
                'is_legacy_recording': False,
                'input_fingerprint': _buildInputFingerprint(snapshot),
                'provider_fingerprint': None,
                'proposed_season_number': None,
                'proposed_episode_number': None,
                'confidence': None,
                'web_search_performed': False,
                'citations': [],
                'ai_model': None,
                'error_code': None,
                'error_message': None,
                'resolved_at': datetime.now(tz=JST) if snapshot.series_episode_id is not None else None,
            },
        )
        return resolution

    @classmethod
    async def _finishAIRequest(
        cls,
        request: RecordedSeriesAIRequest,
        *,
        status: Literal['Succeeded', 'Failed', 'Rejected'],
        result: AIEpisodeLookupResult | None = None,
        error: RecordedSeriesAIError | None = None,
        selected_choice_id: str | None = None,
        connection: BaseDBAsyncClient | None = None,
    ) -> None:
        """話数Web検索監査を、生レスポンスを保持せず完了状態へ更新する。"""

        request_to_update = request
        if connection is not None:
            locked_request = await RecordedSeriesAIRequest.filter(id=request.id) \
                .select_for_update() \
                .using_db(connection) \
                .first()
            if locked_request is None:
                raise RuntimeError('Episode AI request disappeared before finalization.')
            request_to_update = locked_request
        request_to_update.status = status
        request_to_update.selected_choice_id = selected_choice_id
        request_to_update.model = result.model if result is not None else request_to_update.model
        request_to_update.prompt_tokens = result.prompt_tokens if result is not None else None
        request_to_update.completion_tokens = result.completion_tokens if result is not None else None
        request_to_update.http_status = (
            result.http_status if result is not None else error.http_status if error is not None else None
        )
        request_to_update.latency_ms = (
            result.latency_ms if result is not None else error.latency_ms if error is not None else None
        )
        request_to_update.error_code = error.code if error is not None else None
        await request_to_update.save(update_fields=[
            'status',
            'selected_choice_id',
            'model',
            'prompt_tokens',
            'completion_tokens',
            'http_status',
            'latency_ms',
            'error_code',
        ], using_db=connection)

    @classmethod
    async def _applyResolvedEpisode(
        cls,
        *,
        snapshot: _EpisodeProgramSnapshot,
        resolution_id: int,
        season_number: int,
        episode_number: Decimal,
        source: Literal['Local', 'Migration', 'WebSearch'],
        provider_fingerprint: str | None,
        confidence: float | None,
        citations: list[_EpisodeCitationRecord],
        ai_model: str | None,
        write_legacy_value: bool,
        ai_request: RecordedSeriesAIRequest | None = None,
        ai_request_status: Literal['Succeeded', 'Failed', 'Rejected'] | None = None,
        ai_request_result: AIEpisodeLookupResult | None = None,
        ai_request_error: RecordedSeriesAIError | None = None,
        selected_choice_id: str | None = None,
    ) -> bool:
        """最新入力と手動優先を再確認し、Episodeリンクを原子的に確定する。"""

        legacy_value = FormatEpisodeNumber(season_number, episode_number)
        async with transactions.in_transaction() as connection:
            recorded_program = await RecordedProgram.filter(id=snapshot.id) \
                .select_for_update() \
                .using_db(connection) \
                .first()
            resolution = await RecordedEpisodeResolution.filter(id=resolution_id) \
                .select_for_update() \
                .using_db(connection) \
                .first()
            if recorded_program is None or resolution is None:
                raise _RecordedEpisodeSnapshotChanged
            if resolution.source == 'Manual':
                return False
            current_snapshot = _snapshotFromProgram(recorded_program)
            if (
                current_snapshot is None or
                _buildInputFingerprint(current_snapshot) != _buildInputFingerprint(snapshot)
            ):
                raise _RecordedEpisodeSnapshotChanged

            episode = await SeriesEpisode.filter(
                series_id=snapshot.series_id,
                season_number=season_number,
                episode_number=episode_number,
            ).using_db(connection).first()
            if episode is None:
                episode = await SeriesEpisode.create(
                    series_id=snapshot.series_id,
                    season_number=season_number,
                    episode_number=episode_number,
                    using_db=connection,
                )
            recorded_program.series_episode_id = episode.id
            update_fields = ['series_episode_id', 'updated_at']
            if write_legacy_value:
                recorded_program.episode_number = legacy_value
                update_fields.append('episode_number')
            await recorded_program.save(update_fields=update_fields, using_db=connection)

            resolution.episode_id = episode.id
            resolution.status = 'Resolved'
            resolution.source = source
            resolution.input_fingerprint = _buildInputFingerprint(
                _snapshotFromProgram(recorded_program) or snapshot,
            )
            resolution.provider_fingerprint = provider_fingerprint
            resolution.proposed_season_number = season_number
            resolution.proposed_episode_number = episode_number
            resolution.confidence = confidence
            resolution.web_search_performed = source == 'WebSearch'
            resolution.citations = cast(list[dict[str, str]], citations)
            resolution.ai_model = ai_model
            resolution.error_code = None
            resolution.error_message = None
            resolution.resolved_at = datetime.now(tz=JST)
            await resolution.save(using_db=connection)
            if ai_request is not None:
                if ai_request_status is None:
                    raise RuntimeError('Episode AI request final status is missing.')
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
        status: Literal['Unknown', 'NotNumbered', 'NeedsReview', 'Failed'],
        source: Literal['Local', 'WebSearch', 'Migration'],
        provider_fingerprint: str | None,
        error_code: str | None,
        result: AIEpisodeLookupResult | None = None,
        ai_request: RecordedSeriesAIRequest | None = None,
        ai_request_status: Literal['Succeeded', 'Failed', 'Rejected'] | None = None,
        ai_request_error: RecordedSeriesAIError | None = None,
        selected_choice_id: str | None = None,
    ) -> bool:
        """構造化Episodeを作らない判定結果も、手動判断を保護して永続化する。"""

        citations = [
            _EpisodeCitationRecord(url=citation.url, title=citation.title)
            for citation in GetEpisodeLookupEvidence(result)
        ] if result is not None else []
        async with transactions.in_transaction() as connection:
            recorded_program = await RecordedProgram.filter(id=snapshot.id) \
                .select_for_update() \
                .using_db(connection) \
                .first()
            resolution = await RecordedEpisodeResolution.filter(id=resolution_id) \
                .select_for_update() \
                .using_db(connection) \
                .first()
            if recorded_program is None or resolution is None:
                raise _RecordedEpisodeSnapshotChanged
            if resolution.source == 'Manual':
                return False
            current_snapshot = _snapshotFromProgram(recorded_program)
            if (
                current_snapshot is None or
                _buildInputFingerprint(current_snapshot) != _buildInputFingerprint(snapshot)
            ):
                raise _RecordedEpisodeSnapshotChanged

            resolution.episode_id = None
            resolution.status = status
            resolution.source = source
            resolution.input_fingerprint = _buildInputFingerprint(snapshot)
            resolution.provider_fingerprint = provider_fingerprint
            resolution.proposed_season_number = result.season_number if result is not None else None
            resolution.proposed_episode_number = result.episode_number if result is not None else None
            resolution.confidence = result.confidence if result is not None else None
            resolution.web_search_performed = result is not None
            resolution.citations = cast(list[dict[str, str]], citations)
            resolution.ai_model = result.model if result is not None else None
            resolution.error_code = error_code
            resolution.error_message = None
            resolution.resolved_at = datetime.now(tz=JST)
            await resolution.save(using_db=connection)
            if ai_request is not None:
                if ai_request_status is None:
                    raise RuntimeError('Episode AI request final status is missing.')
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
        result: AIEpisodeLookupResult,
        ai_request: RecordedSeriesAIRequest,
        selected_choice_id: str,
    ) -> bool:
        """公式話数なしの受理時に、旧Episodeリンクと自動話数を原子的に解除する。"""

        citations = [
            _EpisodeCitationRecord(url=citation.url, title=citation.title)
            for citation in GetEpisodeLookupEvidence(result)
        ]
        async with transactions.in_transaction() as connection:
            recorded_program = await RecordedProgram.filter(id=snapshot.id) \
                .select_for_update() \
                .using_db(connection) \
                .first()
            resolution = await RecordedEpisodeResolution.filter(id=resolution_id) \
                .select_for_update() \
                .using_db(connection) \
                .first()
            if recorded_program is None or resolution is None:
                raise _RecordedEpisodeSnapshotChanged
            if resolution.source == 'Manual':
                return False
            current_snapshot = _snapshotFromProgram(recorded_program)
            if (
                current_snapshot is None or
                _buildInputFingerprint(current_snapshot) != _buildInputFingerprint(snapshot)
            ):
                raise _RecordedEpisodeSnapshotChanged

            # legacy値を残すと次回のローカル解析で旧Episodeが復活するため、リンクと一緒に消す。
            recorded_program.series_episode_id = None
            recorded_program.episode_number = None
            await recorded_program.save(
                update_fields=['series_episode_id', 'episode_number', 'updated_at'],
                using_db=connection,
            )

            resolution.episode_id = None
            resolution.status = 'NotNumbered'
            resolution.source = 'WebSearch'
            resolution.input_fingerprint = _buildInputFingerprint(
                _snapshotFromProgram(recorded_program) or snapshot,
            )
            resolution.provider_fingerprint = provider_fingerprint
            resolution.proposed_season_number = None
            resolution.proposed_episode_number = None
            resolution.confidence = result.confidence
            resolution.web_search_performed = True
            resolution.citations = cast(list[dict[str, str]], citations)
            resolution.ai_model = result.model
            resolution.error_code = None
            resolution.error_message = None
            resolution.resolved_at = datetime.now(tz=JST)
            await resolution.save(using_db=connection)
            await cls._finishAIRequest(
                ai_request,
                status='Succeeded',
                result=result,
                selected_choice_id=selected_choice_id,
                connection=connection,
            )
        return True

    @classmethod
    async def resolveProgram(
        cls,
        recorded_program_id: int,
        *,
        force: bool = False,
        allow_disabled: bool = False,
        allow_legacy_ai: bool = False,
    ) -> RecordedEpisodeAutomationResult:
        """Series確定録画を、既存値解析から必要時のWeb検索まで順に判定する。

        Args:
            recorded_program_id: 判定対象RecordedProgram ID。
            force: 手動訂正を除く前回結果を再利用せず検索し直すか。
            allow_disabled: 全体自動判定OFFでも手動一括処理として実行するか。
            allow_legacy_ai: migration前から存在する録画に有料検索を許可するか。

        Returns:
            確定状態、出典、AI呼び出し有無。
        """

        async with RECORDED_SERIES_RESOLUTION_LOCK:
            snapshot = await cls._loadSnapshot(recorded_program_id)
            if snapshot is None:
                return RecordedEpisodeAutomationResult(
                    recorded_program_id, 'Skipped', 'MissingOrNotSeries', False,
                )
            input_fingerprint = _buildInputFingerprint(snapshot)
            resolution = await cls._getOrCreateResolution(snapshot)

            # 管理者の数値指定と明示Unknownは、forceを含む全自動処理より優先する。
            if resolution.source == 'Manual':
                return RecordedEpisodeAutomationResult(
                    recorded_program_id,
                    'Resolved' if resolution.status == 'Resolved' else 'Skipped',
                    'Manual',
                    False,
                )

            # force時は、確定済みのローカル解析・移行値・Web検索値も再検索する。
            # 外部検索が失敗または不受理ならこのResolved状態を保持し、受理時だけ置き換える。
            refreshing_resolved = force and resolution.status == 'Resolved'

            parsed_episode = ParseLegacyEpisodeNumber(snapshot.legacy_episode_number)

            # すでに同じSeriesのEpisodeへ接続済みなら、Resolutionだけを修復して再検索しない。
            if refreshing_resolved is False and snapshot.series_episode_id is not None:
                episode = await SeriesEpisode.filter(
                    id=snapshot.series_episode_id,
                    series_id=snapshot.series_id,
                ).first()
                structured_value_matches = (
                    resolution.source in {'WebSearch', 'EPG'} or
                    parsed_episode is None or
                    (
                        parsed_episode.season_number == episode.season_number and
                        parsed_episode.episode_number == episode.episode_number
                    )
                ) if episode is not None else False
                if episode is not None and structured_value_matches:
                    if resolution.status == 'Resolved' and resolution.episode_id == episode.id:
                        return RecordedEpisodeAutomationResult(
                            recorded_program_id,
                            'Resolved',
                            'StructuredCache',
                            False,
                        )
                    applied = await cls._applyResolvedEpisode(
                        snapshot=snapshot,
                        resolution_id=resolution.id,
                        season_number=episode.season_number,
                        episode_number=episode.episode_number,
                        source='Migration' if resolution.is_legacy_recording else 'Local',
                        provider_fingerprint=None,
                        confidence=None,
                        citations=[],
                        ai_model=None,
                        write_legacy_value=False,
                    )
                    return RecordedEpisodeAutomationResult(
                        recorded_program_id,
                        'Resolved' if applied else 'Skipped',
                        'StructuredCache',
                        False,
                    )

            # ローカルで一意に読める値は、AI設定や日次上限より先に無通信で確定する。
            if refreshing_resolved is False and parsed_episode is not None:
                applied = await cls._applyResolvedEpisode(
                    snapshot=snapshot,
                    resolution_id=resolution.id,
                    season_number=parsed_episode.season_number,
                    episode_number=parsed_episode.episode_number,
                    source='Migration' if resolution.is_legacy_recording else 'Local',
                    provider_fingerprint=None,
                    confidence=None,
                    citations=[],
                    ai_model=None,
                    write_legacy_value=False,
                )
                return RecordedEpisodeAutomationResult(
                    recorded_program_id,
                    'Resolved' if applied else 'Skipped',
                    'LegacyMetadata',
                    False,
                )

            # migration以前の録画は、専用の管理者操作なしに設定読込みや有料検索へ進めない。
            if resolution.is_legacy_recording and allow_legacy_ai is False:
                if (
                    resolution.source == 'WebSearch' and
                    resolution.status in {'NotNumbered', 'NeedsReview', 'Failed'}
                ):
                    return RecordedEpisodeAutomationResult(
                        recorded_program_id,
                        cast(Literal['NotNumbered', 'NeedsReview', 'Failed'], resolution.status),
                        'LegacyCache',
                        False,
                        resolution.error_code,
                    )
                if resolution.status == 'NeedsReview':
                    return RecordedEpisodeAutomationResult(
                        recorded_program_id,
                        'Skipped',
                        'LegacyRequiresManualBackfill',
                        False,
                        'LegacyRequiresManualBackfill',
                    )
                await cls._markResolutionState(
                    snapshot=snapshot,
                    resolution_id=resolution.id,
                    status='NeedsReview',
                    source='Migration',
                    provider_fingerprint=None,
                    error_code=(
                        'LegacyEpisodeMissing'
                        if snapshot.legacy_episode_number is None
                        else 'LegacyEpisodeUnknown'
                    ),
                )
                return RecordedEpisodeAutomationResult(
                    recorded_program_id,
                    'Skipped',
                    'LegacyRequiresManualBackfill',
                    False,
                    'LegacyRequiresManualBackfill',
                )

            settings, runtime_api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()
            provider_fingerprint = _buildProviderFingerprint(
                api_base_url=settings.api_base_url,
                model=settings.model,
                api_key=runtime_api_key,
            )

            # 同じ入力・providerへの失敗や低信頼結果を繰り返し課金しない。
            if (
                force is False and
                resolution.input_fingerprint == input_fingerprint and
                resolution.provider_fingerprint == provider_fingerprint and
                resolution.status in {'NotNumbered', 'NeedsReview', 'Failed'}
            ):
                return RecordedEpisodeAutomationResult(
                    recorded_program_id,
                    cast(Literal['NotNumbered', 'NeedsReview', 'Failed'], resolution.status),
                    'Cache',
                    False,
                    resolution.error_code,
                )

            if settings.enabled is False and allow_disabled is False:
                if refreshing_resolved is False:
                    await cls._markResolutionState(
                        snapshot=snapshot,
                        resolution_id=resolution.id,
                        status='Unknown',
                        source='Local',
                        provider_fingerprint=provider_fingerprint,
                        error_code='RecordedSeriesIsDisabled',
                    )
                return RecordedEpisodeAutomationResult(
                    recorded_program_id, 'Skipped', 'Disabled', False, 'RecordedSeriesIsDisabled',
                )
            if settings.ai_enabled is False or settings.ai_episode_number_search_enabled is False:
                error_code = (
                    'AIIsDisabled'
                    if settings.ai_enabled is False
                    else 'AIEpisodeNumberSearchIsDisabled'
                )
                if refreshing_resolved is False:
                    await cls._markResolutionState(
                        snapshot=snapshot,
                        resolution_id=resolution.id,
                        status='Unknown',
                        source='Local',
                        provider_fingerprint=provider_fingerprint,
                        error_code=error_code,
                    )
                return RecordedEpisodeAutomationResult(
                    recorded_program_id, 'Skipped', 'Disabled', False, error_code,
                )

            # Series候補選択とEpisode検索を同じlock内で数え、共有上限を超えないようにする。
            if settings.daily_ai_request_limit > 0:
                today_start = datetime.combine(datetime.now(tz=JST).date(), datetime_time.min, tzinfo=JST)
                requests_today = await RecordedSeriesAIRequest.filter(
                    purpose__in=['Resolution', 'EpisodeLookup'],
                    created_at__gte=today_start,
                ).filter(
                    Q(error_code=None) | Q(error_code__not='InputChangedBeforeRequest'),
                ).count()
                if requests_today >= settings.daily_ai_request_limit:
                    if refreshing_resolved is False:
                        await cls._markResolutionState(
                            snapshot=snapshot,
                            resolution_id=resolution.id,
                            status='Unknown',
                            source='Local',
                            provider_fingerprint=provider_fingerprint,
                            error_code='DailyAIRequestLimitReached',
                        )
                    return RecordedEpisodeAutomationResult(
                        recorded_program_id,
                        'Skipped',
                        'DailyLimit',
                        False,
                        'DailyAIRequestLimitReached',
                    )

            # 確定済み値の再検索では、検索中・失敗時にも現在値を表示し続ける。
            # AI監査だけをPendingで作り、受理された結果の保存時にResolutionを置き換える。
            if refreshing_resolved is False:
                resolution.status = 'Pending'
                resolution.source = 'WebSearch'
                resolution.input_fingerprint = input_fingerprint
                resolution.provider_fingerprint = provider_fingerprint
                resolution.ai_model = settings.model
                resolution.error_code = None
                resolution.error_message = None
                await resolution.save()
            ai_request = await RecordedSeriesAIRequest.create(
                resolution_id=None,
                episode_resolution_id=resolution.id,
                purpose='EpisodeLookup',
                status='Pending',
                model=settings.model,
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

            # 監査予約中の入力変更は外部POST前に検出し、日次利用数から除外する。
            latest_snapshot = await cls._loadSnapshot(recorded_program_id)
            if latest_snapshot is None or _buildInputFingerprint(latest_snapshot) != input_fingerprint:
                error = RecordedSeriesAIError('InputChangedBeforeRequest')
                await cls._finishAIRequest(ai_request, status='Failed', error=error)
                raise _RecordedEpisodeSnapshotChanged

            detail_text = '\n'.join(
                f'{key}: {value}'
                for key, value in sorted(snapshot.detail.items())
            )[:1200]
            prompt = RecordedEpisodeProgramPrompt(
                series_title=snapshot.series_title[:500],
                program_title=snapshot.title[:500],
                subtitle=snapshot.subtitle[:500] if snapshot.subtitle is not None else None,
                description=snapshot.description[:1200],
                detail=detail_text,
                channel=snapshot.channel_name,
                broadcast_datetime=snapshot.start_time.isoformat(),
            )
            try:
                result = await SearchRecordedEpisodeNumber(
                    api_base_url=settings.api_base_url,
                    api_key=runtime_api_key,
                    model=settings.model,
                    program=prompt,
                )
            except RecordedSeriesAIError as ex:
                rejected_codes = {
                    'InvalidJSON',
                    'InvalidJSONType',
                    'InvalidOutputSchema',
                    'MissingWebSearchCall',
                }
                if refreshing_resolved:
                    # 再検索の通信失敗・不正応答では、最後に確定した値を維持して監査だけを閉じる。
                    await cls._finishAIRequest(
                        ai_request,
                        status='Rejected' if ex.code in rejected_codes else 'Failed',
                        error=ex,
                    )
                else:
                    try:
                        await cls._markResolutionState(
                            snapshot=snapshot,
                            resolution_id=resolution.id,
                            status='NeedsReview' if ex.code in rejected_codes else 'Failed',
                            source='WebSearch',
                            provider_fingerprint=provider_fingerprint,
                            error_code=ex.code,
                            ai_request=ai_request,
                            ai_request_status='Rejected' if ex.code in rejected_codes else 'Failed',
                            ai_request_error=ex,
                        )
                    except _RecordedEpisodeSnapshotChanged:
                        # POST後に入力が変わった検索は利用数へ数えつつ、旧入力の結果を番組へ反映しない。
                        await cls._finishAIRequest(
                            ai_request,
                            status='Failed',
                            error=RecordedSeriesAIError('InputChangedAfterRequest'),
                        )
                        raise
                return RecordedEpisodeAutomationResult(
                    recorded_program_id,
                    'NeedsReview' if ex.code in rejected_codes else 'Failed',
                    'WebSearchRefreshPreserved' if refreshing_resolved else 'WebSearch',
                    True,
                    ex.code,
                )

            selected_choice_id = (
                'not-numbered'
                if result.numbered is False
                else f'S{result.season_number}E{result.episode_number}'
            )
            accepted = IsEpisodeLookupResultAccepted(
                result,
                settings.ai_episode_number_acceptance_mode,
            )
            terminal_status: Literal['Succeeded', 'Rejected'] = 'Succeeded' if accepted else 'Rejected'
            terminal_error = None if accepted else RecordedSeriesAIError('AcceptancePolicyRejected')
            citation_records = [
                _EpisodeCitationRecord(url=citation.url, title=citation.title)
                for citation in GetEpisodeLookupEvidence(result)
            ]
            try:
                if result.numbered is False and accepted:
                    applied = await cls._applyNotNumbered(
                        snapshot=snapshot,
                        resolution_id=resolution.id,
                        provider_fingerprint=provider_fingerprint,
                        result=result,
                        ai_request=ai_request,
                        selected_choice_id=selected_choice_id,
                    )
                    return RecordedEpisodeAutomationResult(
                        recorded_program_id,
                        'NotNumbered' if applied else 'Skipped',
                        'WebSearch',
                        True,
                    )
                if accepted and result.season_number is not None and result.episode_number is not None:
                    applied = await cls._applyResolvedEpisode(
                        snapshot=snapshot,
                        resolution_id=resolution.id,
                        season_number=result.season_number,
                        episode_number=result.episode_number,
                        source='WebSearch',
                        provider_fingerprint=provider_fingerprint,
                        confidence=result.confidence,
                        citations=citation_records,
                        ai_model=result.model,
                        write_legacy_value=True,
                        ai_request=ai_request,
                        ai_request_status=terminal_status,
                        ai_request_result=result,
                        ai_request_error=terminal_error,
                        selected_choice_id=selected_choice_id,
                    )
                    return RecordedEpisodeAutomationResult(
                        recorded_program_id,
                        'Resolved' if applied else 'Skipped',
                        'WebSearch',
                        True,
                    )

                if refreshing_resolved:
                    # 低信頼提案も旧確定値へは反映せず、監査上の候補だけを残す。
                    await cls._finishAIRequest(
                        ai_request,
                        status=terminal_status,
                        result=result,
                        error=terminal_error,
                        selected_choice_id=selected_choice_id,
                    )
                else:
                    await cls._markResolutionState(
                        snapshot=snapshot,
                        resolution_id=resolution.id,
                        status='NeedsReview',
                        source='WebSearch',
                        provider_fingerprint=provider_fingerprint,
                        error_code='AcceptancePolicyRejected',
                        result=result,
                        ai_request=ai_request,
                        ai_request_status=terminal_status,
                        ai_request_error=terminal_error,
                        selected_choice_id=selected_choice_id,
                    )
                return RecordedEpisodeAutomationResult(
                    recorded_program_id,
                    'NeedsReview',
                    'WebSearchRefreshPreserved' if refreshing_resolved else 'WebSearch',
                    True,
                    'AcceptancePolicyRejected',
                )
            except _RecordedEpisodeSnapshotChanged:
                await cls._finishAIRequest(
                    ai_request,
                    status='Failed',
                    error=RecordedSeriesAIError('InputChangedAfterRequest'),
                )
                raise

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
                settings.enabled is False or
                settings.ai_enabled is False or
                settings.ai_episode_number_search_enabled is False or
                settings.ai_episode_number_acceptance_mode != 'Always'
            ):
                return 0
            promoted_count = 0
            proposal_ids = cast(list[int], await RecordedEpisodeResolution.filter(
                status='NeedsReview',
                source='WebSearch',
                web_search_performed=True,
            ).exclude(proposed_season_number=None).exclude(proposed_episode_number=None) \
                .order_by('id').values_list('id', flat=True))
            for resolution_id in proposal_ids:
                # 一覧取得後にforce検索や手動訂正が完了していても、古い提案を上書きしない。
                resolution = await RecordedEpisodeResolution.filter(
                    id=resolution_id,
                    status='NeedsReview',
                    source='WebSearch',
                    web_search_performed=True,
                ).first()
                if (
                    resolution is None or
                    resolution.proposed_season_number is None or
                    resolution.proposed_episode_number is None
                ):
                    continue
                snapshot = await cls._loadSnapshot(resolution.recorded_program_id)
                if snapshot is None:
                    continue
                if resolution.input_fingerprint != _buildInputFingerprint(snapshot):
                    # 提案取得後に番組情報やSeries所属が変わった場合、旧文脈の数値は昇格しない。
                    continue
                citations = [
                    _EpisodeCitationRecord(
                        url=citation.get('url', ''),
                        title=citation.get('title', ''),
                    )
                    for citation in resolution.citations
                    if citation.get('url', '') != ''
                ]
                applied = await cls._applyResolvedEpisode(
                    snapshot=snapshot,
                    resolution_id=resolution.id,
                    season_number=resolution.proposed_season_number,
                    episode_number=resolution.proposed_episode_number,
                    source='WebSearch',
                    provider_fingerprint=resolution.provider_fingerprint,
                    confidence=resolution.confidence,
                    citations=citations,
                    ai_model=resolution.ai_model,
                    write_legacy_value=True,
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
        settings, runtime_api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()
        # OFFへの設定変更では、旧Web検索の提案・引用をUnknownへ上書きしない。
        # 再有効化時には同じsettingsUpdated()を通るため、その時点で再試行できる。
        if (
            settings.enabled is False or
            settings.ai_enabled is False or
            settings.ai_episode_number_search_enabled is False
        ):
            return
        await cls._enqueueRecoverablePrograms(provider_fingerprint=_buildProviderFingerprint(
            api_base_url=settings.api_base_url,
            model=settings.model,
            api_key=runtime_api_key,
        ))

    @classmethod
    async def startBackfill(cls, *, force: bool = False) -> RecordedEpisodeBackfillAccepted:
        """migration以前からある話数不明録画の手動Web検索を開始する。

        Args:
            force: 手動訂正を除く確定・失敗・低信頼・NotNumbered結果も再送するか。

        Returns:
            解析履歴IDと、進行中タスクを再利用したか。
        """

        async with cls._backfill_start_lock:
            if cls._backfill_task is not None and cls._backfill_task.done() is False:
                assert cls._backfill_handle is not None
                return RecordedEpisodeBackfillAccepted(cls._backfill_handle.execution.id, True)

            await cls.start()
            settings, runtime_api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()
            candidates = await cls._loadBackfillCandidateIDs(
                force=force,
                provider_fingerprint=_buildProviderFingerprint(
                    api_base_url=settings.api_base_url,
                    model=settings.model,
                    api_key=runtime_api_key,
                ),
            )
            handle = await AnalysisTaskTracker.start(
                'BatchEpisodeResolution',
                title='既存録画話数一括判定',
                trigger='Manual',
                total_count=len(candidates),
                initial_status='Queued',
                inherit_parent=False,
            )
            cls._backfill_handle = handle
            cls._backfill_task = asyncio.create_task(cls._runBackfill(handle, candidates, force=force))
            return RecordedEpisodeBackfillAccepted(handle.execution.id, False)

    @classmethod
    async def _loadBackfillCandidateIDs(
        cls,
        *,
        force: bool,
        provider_fingerprint: str | None = None,
    ) -> list[int]:
        """手動実行対象を、前回結果とforce設定から固定する。"""

        resolutions = await RecordedEpisodeResolution.filter(
            recorded_program__series_id__not_isnull=True,
            recorded_program__recorded_video__status='Recorded',
        ).exclude(source='Manual').order_by('recorded_program_id')
        candidate_ids: list[int] = []
        for resolution in resolutions:
            if force:
                candidate_ids.append(resolution.recorded_program_id)
                continue
            if resolution.status == 'Resolved':
                continue
            if resolution.status in {'Pending', 'Unknown'}:
                candidate_ids.append(resolution.recorded_program_id)
                continue
            if resolution.source == 'Migration' and resolution.status == 'NeedsReview':
                candidate_ids.append(resolution.recorded_program_id)
                continue
            if (
                provider_fingerprint is not None and
                resolution.source == 'WebSearch' and
                resolution.status in {'NeedsReview', 'Failed'} and
                resolution.provider_fingerprint != provider_fingerprint
            ):
                candidate_ids.append(resolution.recorded_program_id)
        return candidate_ids

    @classmethod
    async def _runBackfill(
        cls,
        handle: AnalysisTaskHandle,
        candidate_ids: list[int],
        *,
        force: bool,
    ) -> None:
        """1回の管理者操作につき、当日の残り上限まで対象録画を順次検索する。"""

        try:
            async with AnalysisTaskTracker.track(
                'BatchEpisodeResolution',
                trigger='Manual',
                existing_handle=handle,
            ) as history:
                await history.setStage('Searching', 0.0)
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
                            f'[RecordedEpisodeAutomation] Episode backfill item failed. '
                            f'recorded_program_id: {recorded_program_id}',
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
                    preserved_count += int(result.source == 'WebSearchRefreshPreserved')
                    if result.status == 'Resolved':
                        succeeded_count += 1
                        resolved_count += 1
                    elif result.status == 'NotNumbered':
                        succeeded_count += 1
                        not_numbered_count += 1
                    elif result.status == 'NeedsReview':
                        succeeded_count += 1
                        needs_review_count += 1
                    elif result.status == 'Failed':
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
                    if result.error_code == 'DailyAIRequestLimitReached':
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
                    'Failed' if failed_count > 0 else 'Succeeded',
                    summary=cast(dict[str, object], {
                        'resolved_or_reviewed': succeeded_count,
                        'resolved': resolved_count,
                        'not_numbered': not_numbered_count,
                        'needs_review': needs_review_count,
                        'preserved': preserved_count,
                        'failed': failed_count,
                        'skipped': skipped_count,
                        'ai_requests': ai_request_count,
                        'stopped_by_daily_limit': stopped_by_daily_limit,
                    }),
                    error_code='PartialFailure' if failed_count > 0 else None,
                )
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logging.error('[RecordedEpisodeAutomation] Episode backfill failed:', exc_info=ex)
        finally:
            cls._backfill_task = None
            cls._backfill_handle = None

    @classmethod
    async def getStatus(cls) -> dict[str, int | str | bool | None]:
        """管理画面向けに、Series所属録画の話数構造化状態を集計する。"""

        series_program_count = await RecordedProgram.filter(
            series_id__not_isnull=True,
            recorded_video__status='Recorded',
        ).count()
        status_counts = {
            status: await RecordedEpisodeResolution.filter(
                recorded_program__series_id__not_isnull=True,
                recorded_program__recorded_video__status='Recorded',
                status=status,
            ).count()
            for status in ('Pending', 'Resolved', 'Unknown', 'NotNumbered', 'NeedsReview', 'Failed')
        }
        known_resolution_count = sum(status_counts.values())
        unknown_count = (
            status_counts['Pending'] +
            status_counts['Unknown'] +
            max(0, series_program_count - known_resolution_count)
        )
        last_resolution = await RecordedEpisodeResolution.filter(
            recorded_program__series_id__not_isnull=True,
            recorded_program__recorded_video__status='Recorded',
        ).order_by('-updated_at').first()
        return {
            'episode_resolved': status_counts['Resolved'],
            'episode_unknown': unknown_count,
            'episode_not_numbered': status_counts['NotNumbered'],
            'episode_needs_review': status_counts['NeedsReview'],
            'episode_failed': status_counts['Failed'],
            'episode_last_run_at': (
                last_resolution.updated_at.isoformat()
                if last_resolution is not None
                else None
            ),
            'is_episode_running': cls._backfill_task is not None and cls._backfill_task.done() is False,
        }
