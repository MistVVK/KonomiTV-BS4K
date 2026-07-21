from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import time as datetime_time
from difflib import SequenceMatcher
from typing import Literal, cast

import httpx
from tortoise import transactions
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.exceptions import IntegrityError
from tortoise.expressions import Q

from app import logging
from app.constants import JST
from app.metadata.AnalysisTaskTracker import AnalysisTaskHandle, AnalysisTaskTracker
from app.metadata.RecordedSeriesCandidates import (
    AIChoiceResult,
    RecordedSeriesAIError,
    RecordedSeriesProgramPrompt,
    SearchWikipediaCandidates,
    SelectRecordedSeriesCandidate,
    SeriesChoiceCandidate,
    WikipediaCandidate,
)
from app.metadata.RecordedSeriesSettings import RecordedSeriesSettingsStore
from app.metadata.SeriesTitleParser import (
    SERIES_TITLE_PARSER_VERSION,
    BuildSeriesGroupingKey,
    NormalizeProgramText,
    ParseSeriesTitle,
    SeriesTitleParseResult,
)
from app.models.Program import Program
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedSeries import (
    RecordedSeriesAIRequest,
    RecordedSeriesResolution,
    RecordedSeriesRule,
    RecordedSeriesSource,
)
from app.models.Series import Series
from app.models.SeriesBroadcastPeriod import SeriesBroadcastPeriod
from app.schemas import Genre


RECORDED_SERIES_RESOLVER_VERSION = f'1-parser{SERIES_TITLE_PARSER_VERSION}'
AI_ATTEMPT_CACHE_TTL_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class _ProgramSnapshot:
    """クラスタ構築と入力fingerprintに必要な録画メタデータのスナップショット。"""

    id: int
    channel_id: str | None
    network_id: int | None
    service_id: int | None
    event_id: int | None
    title: str
    description: str
    detail: dict[str, str]
    genres: list[Genre]
    start_time: datetime


@dataclass(frozen=True, slots=True)
class _ClusterEvidence:
    """同一シリーズ候補としてまとめた録画群と再判定用fingerprint。"""

    display_title: str
    normalized_key: str
    member_ids: tuple[int, ...]
    evidence_hash: str
    is_strong_repeat: bool


@dataclass(frozen=True, slots=True)
class _EPGEnrichment:
    """保持EPGとの一意照合で補完できた話数情報。"""

    parse_result: SeriesTitleParseResult
    program_id: str


@dataclass(frozen=True, slots=True)
class RecordedSeriesResolveResult:
    """1録画のシリーズ判定結果とバックフィル集計区分。"""

    recorded_program_id: int
    status: Literal['Resolved', 'NotSeries', 'NeedsReview', 'Failed', 'Skipped']
    source: str | None
    ai_requested: bool


@dataclass(frozen=True, slots=True)
class RecordedSeriesBackfillAccepted:
    """バックフィル要求で作成または再利用した解析履歴。"""

    execution_id: int
    reused: bool


class _RecordedProgramSnapshotChanged(Exception):
    """判定中に録画メタデータが更新され、古い結果を保存できなくなったことを表す。"""


class RecordedSeriesProgramNotFoundError(Exception):
    """手動割当の対象となる録画番組が存在しないことを表す。"""


class RecordedSeriesTargetNotFoundError(Exception):
    """手動割当で指定されたSeriesが存在しないことを表す。"""


class RecordedSeriesChannelUnavailableError(Exception):
    """手動でシリーズ化するのに必要なチャンネル情報がないことを表す。"""


class RecordedSeriesInvalidTitleError(Exception):
    """手動割当用タイトルから有効な正規化キーを作れないことを表す。"""


class RecordedSeriesTitleConflictError(Exception):
    """Series名の変更が既存Seriesまたは判定Ruleと衝突することを表す。"""


class RecordedSeriesMetadataStaleError(Exception):
    """編集開始後にSeriesが更新され、古い画面からの上書きを拒否したことを表す。"""


class RecordedSeriesResolverBusyError(Exception):
    """自動判定または別の更新が実行中で、編集を即時受理できないことを表す。"""


def _sha256JSON(payload: object) -> str:
    """順序を固定したJSON表現からSHA-256を生成する。"""

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def _buildInputFingerprint(program: _ProgramSnapshot) -> str:
    """シリーズ判定へ影響する録画メタデータだけから入力fingerprintを作る。"""

    return _sha256JSON({
        'resolver_version': RECORDED_SERIES_RESOLVER_VERSION,
        'channel_id': program.channel_id,
        'network_id': program.network_id,
        'service_id': program.service_id,
        'event_id': program.event_id,
        'title': program.title,
        'description': program.description,
        'detail': program.detail,
        'genres': program.genres,
        'start_time': program.start_time.isoformat(),
    })


def _assertRecordedProgramSnapshotCurrent(
    recorded_program: RecordedProgram,
    snapshot: _ProgramSnapshot,
) -> None:
    """トランザクション内の最新録画が判定開始時の入力と同一であることを検証する。"""

    current_snapshot = _ProgramSnapshot(
        id=recorded_program.id,
        channel_id=recorded_program.channel_id,
        network_id=recorded_program.network_id,
        service_id=recorded_program.service_id,
        event_id=recorded_program.event_id,
        title=recorded_program.title,
        description=recorded_program.description,
        detail=recorded_program.detail,
        genres=recorded_program.genres,
        start_time=recorded_program.start_time,
    )
    if _buildInputFingerprint(current_snapshot) != _buildInputFingerprint(snapshot):
        raise _RecordedProgramSnapshotChanged


def _buildRuleKeyHash(normalized_key: str) -> str:
    """長い番組名でも一意制約を安定して適用できる固定長キーを返す。"""

    return hashlib.sha256(normalized_key.encode('utf-8')).hexdigest()


def _buildAIAttemptKey(
    *,
    resolution_id: int,
    input_fingerprint: str,
    evidence_hash: str,
    candidate_set_hash: str,
    api_base_url: str,
    model: str,
    api_key: str | None,
) -> str:
    """同一プロセス内で同じ入力・provider設定へのAI再送を抑止する秘密非保持キーを返す。"""

    return _sha256JSON({
        'resolution_id': resolution_id,
        'input_fingerprint': input_fingerprint,
        'evidence_hash': evidence_hash,
        'candidate_set_hash': candidate_set_hash,
        'api_base_url': api_base_url,
        'model': model,
        # APIキー自体は保持せず、設定変更を区別するための一方向hashだけを使う。
        'api_key_hash': hashlib.sha256(api_key.encode('utf-8')).hexdigest() if api_key is not None else None,
    })


def _buildContentEvidence(program: _ProgramSnapshot) -> str:
    """同名の再放送と異なるエピソードを区別するための内容fingerprintを返す。"""

    return _sha256JSON({
        'title': NormalizeProgramText(program.title),
        'description': NormalizeProgramText(program.description),
        'detail': {
            key: NormalizeProgramText(value)
            for key, value in sorted(program.detail.items())
        },
    })


def _candidateRootPrefixes(title: str) -> set[str]:
    """一律空白分割をせず、複数録画で裏付け可能なroot候補だけを列挙する。"""

    candidates: set[str] = set()
    for index, character in enumerate(title):
        if character not in {' ', '　', '「', '『', '【', '[', '▽', '▼'}:
            continue
        candidate = title[:index].strip()
        if len(BuildSeriesGroupingKey(candidate)) >= 4:
            candidates.add(candidate)
    return candidates


def _hasTitlePrefixBoundary(title: str, prefix: str) -> bool:
    """前方一致の直後が番組名の区切りである場合だけroot一致として扱う。"""

    if title == prefix:
        return True
    if not title.startswith(prefix):
        return False
    return title[len(prefix)] in {' ', '　', '「', '『', '【', '[', '▽', '▼', ':', '：', '-', '―', '／', '/'}


def _buildClusterEvidence(
    programs: list[_ProgramSnapshot],
    parses: dict[int, SeriesTitleParseResult],
) -> dict[int, _ClusterEvidence]:
    """全録画を見て強い共通rootとnegative cache再判定fingerprintを構築する。"""

    exact_groups: dict[str, list[int]] = {}
    prefix_groups: dict[str, set[int]] = {}
    for program in programs:
        parse = parses[program.id]
        exact_groups.setdefault(parse.normalized_key, []).append(program.id)
        for prefix in _candidateRootPrefixes(parse.series_title):
            prefix_groups.setdefault(prefix, set()).add(program.id)

    # ある録画から抽出したprefixは、同じprefix候補を明示していない別録画にも前方一致する。
    # これにより「運転席からの風景 東武…」「運転席からの風景 京王…」を同じrootへ畳む。
    for prefix in list(prefix_groups):
        for program in programs:
            if _hasTitlePrefixBoundary(parses[program.id].series_title, prefix):
                prefix_groups[prefix].add(program.id)

    evidence_by_id: dict[int, _ClusterEvidence] = {}
    snapshots_by_id = {program.id: program for program in programs}
    for program in programs:
        parse = parses[program.id]
        root_title = parse.series_title
        member_ids = set(exact_groups.get(parse.normalized_key, [program.id]))

        # 同一録画が属する候補のうち、最長rootを採用して過剰な上位グループ化を避ける。
        matching_prefixes: list[str] = []
        for prefix, ids in prefix_groups.items():
            if program.id not in ids or len(ids) < 2:
                continue
            prefix_content_hashes = {
                _buildContentEvidence(snapshots_by_id[member_id])
                for member_id in ids
            }
            if len(prefix_content_hashes) >= 2:
                matching_prefixes.append(prefix)
        if len(matching_prefixes) > 0:
            root_title = max(matching_prefixes, key=lambda value: len(BuildSeriesGroupingKey(value)))
            member_ids = set(prefix_groups[root_title])

        sorted_member_ids = tuple(sorted(member_ids))
        content_hashes = {
            _buildContentEvidence(snapshots_by_id[member_id])
            for member_id in sorted_member_ids
        }
        normalized_key = BuildSeriesGroupingKey(root_title)
        evidence_hash = _sha256JSON({
            'normalized_key': normalized_key,
            'members': [
                {
                    'id': member_id,
                    'content': _buildContentEvidence(snapshots_by_id[member_id]),
                }
                for member_id in sorted_member_ids
            ],
        })
        evidence_by_id[program.id] = _ClusterEvidence(
            display_title=root_title,
            normalized_key=normalized_key,
            member_ids=sorted_member_ids,
            evidence_hash=evidence_hash,
            is_strong_repeat=len(sorted_member_ids) >= 2 and len(content_hashes) >= 2,
        )
    return evidence_by_id


def _seriesSimilarity(left: str, right: str) -> tuple[float, int]:
    """シリーズ候補同士の全体類似度と最長共通部分長を返す。"""

    matcher = SequenceMatcher(None, BuildSeriesGroupingKey(left), BuildSeriesGroupingKey(right))
    return matcher.ratio(), matcher.find_longest_match().size


class RecordedSeriesResolver:
    """録画スキャンと外部APIを分離し、シリーズ判定を1ワーカーで直列化する。"""

    _queue: asyncio.Queue[int] | None = None
    _worker_task: asyncio.Task[None] | None = None
    _backfill_task: asyncio.Task[None] | None = None
    _backfill_handle: AnalysisTaskHandle | None = None
    _queued_ids: set[int] = set()
    _running_ids: set[int] = set()
    _rerun_ids: set[int] = set()
    _ai_attempt_keys: dict[str, float] = {}
    _snapshot_generation = 0
    _resolve_lock = asyncio.Lock()
    _backfill_start_lock = asyncio.Lock()
    _recovery_lock = asyncio.Lock()
    _canonical_key_backfill_lock = asyncio.Lock()
    _pending_recovery_completed = False
    _canonical_key_backfill_completed = False

    @classmethod
    async def start(cls) -> None:
        """シリーズ判定ワーカーを開始する。

        Returns:
            None
        """

        await cls._backfillLegacySeriesCanonicalKeys()
        if cls._worker_task is None or cls._worker_task.done():
            cls._queue = asyncio.Queue()
            cls._queued_ids = set()
            cls._running_ids = set()
            cls._rerun_ids = set()
            cls._pending_recovery_completed = False
            cls._worker_task = asyncio.create_task(cls._runWorker())
        await cls._recoverPendingResolutions()

    @classmethod
    async def _backfillLegacySeriesCanonicalKeys(cls) -> None:
        """canonical key導入前のSeriesを、衝突時は最古の1件を代表として補完する。"""

        async with cls._canonical_key_backfill_lock:
            if cls._canonical_key_backfill_completed:
                return
            try:
                async with transactions.in_transaction() as connection:
                    series_list = await Series.all().using_db(connection).order_by('id')
                    occupied_keys = {
                        series.canonical_key
                        for series in series_list
                        if series.canonical_key is not None
                    }
                    for series in series_list:
                        if series.canonical_key is not None:
                            continue
                        normalized_key = BuildSeriesGroupingKey(series.title)
                        if normalized_key == '':
                            continue
                        canonical_key = _buildRuleKeyHash(normalized_key)
                        # 表記揺れ由来の既存重複はここで自動統合せず、
                        # 最古のSeriesにだけキーを付与して今後の新規重複を止める。
                        if canonical_key in occupied_keys:
                            continue
                        series.canonical_key = canonical_key
                        await series.save(
                            update_fields=['canonical_key', 'updated_at'],
                            using_db=connection,
                        )
                        occupied_keys.add(canonical_key)
            except Exception as ex:
                # 任意機能の補完失敗でKonomiTV全体の起動は止めない。
                # completedは下げたままにし、次のstart()で再試行する。
                logging.error('[RecordedSeriesResolver] Failed to backfill Series canonical keys:', exc_info=ex)
                return
            cls._canonical_key_backfill_completed = True

    @classmethod
    async def _recoverPendingResolutions(cls) -> None:
        """中断AI監査を原子的に閉じ、再送してよいPendingだけをキューへ戻す。"""

        async with cls._recovery_lock:
            if cls._pending_recovery_completed:
                return
            try:
                async with transactions.in_transaction() as connection:
                    pending_ai_requests = await RecordedSeriesAIRequest.filter(
                        purpose='Resolution',
                        status='Pending',
                    ).using_db(connection)
                    for ai_request in pending_ai_requests:
                        resolution = None
                        if ai_request.resolution_id is not None:
                            resolution = await RecordedSeriesResolution.filter(
                                id=ai_request.resolution_id,
                            ).using_db(connection).first()
                        if (
                            resolution is not None and
                            resolution.status == 'Pending' and
                            ai_request.input_fingerprint is not None and
                            ai_request.candidate_set_hash is not None and
                            resolution.input_fingerprint == ai_request.input_fingerprint and
                            resolution.candidate_set_hash == ai_request.candidate_set_hash
                        ):
                            resolution.status = 'NeedsReview'
                            resolution.source = 'AI'
                            resolution.error_code = 'AIRequestInterrupted'
                            resolution.error_message = None
                            resolution.resolved_at = None
                            await resolution.save(update_fields=[
                                'status',
                                'source',
                                'error_code',
                                'error_message',
                                'resolved_at',
                                'updated_at',
                            ], using_db=connection)
                        ai_request.status = 'Failed'
                        ai_request.error_code = 'AIRequestInterrupted'
                        await ai_request.save(update_fields=['status', 'error_code'], using_db=connection)
            except Exception as ex:
                logging.error('[RecordedSeriesResolver] Failed to restore pending resolutions:', exc_info=ex)
                return
            cls._pending_recovery_completed = True

        await cls._enqueuePendingResolutionsIfEnabled()

    @classmethod
    async def _enqueuePendingResolutionsIfEnabled(cls) -> None:
        """自動判定が有効なときだけ、監査中断と無関係なPendingを復元する。"""

        try:
            settings = RecordedSeriesSettingsStore.getSettings()
            if settings.enabled is False or cls._queue is None:
                return
            pending_ids = cast(list[int], await RecordedSeriesResolution.filter(
                Q(status='Pending') | Q(source='Manual', input_fingerprint='0' * 64),
                recorded_program__recorded_video__status='Recorded',
            ).values_list('recorded_program_id', flat=True))
            for recorded_program_id in pending_ids:
                if recorded_program_id in cls._queued_ids or recorded_program_id in cls._running_ids:
                    continue
                cls._queued_ids.add(recorded_program_id)
                await cls._queue.put(recorded_program_id)
        except Exception as ex:
            # 任意機能の設定破損や一時的なDB障害をKonomiTV全体の起動失敗にしない。
            # 設定修復後は retryPendingRecovery() からこのキュー復元だけを再試行できる。
            logging.error('[RecordedSeriesResolver] Failed to restore pending resolution queue:', exc_info=ex)

    @classmethod
    async def retryPendingRecovery(cls) -> None:
        """設定修復・有効化後に起動時Pending回収を安全に再試行する。"""

        if cls._worker_task is None:
            return
        if cls._pending_recovery_completed is False:
            await cls._recoverPendingResolutions()
            return
        # 起動時回収は1回限りのまま、無効中に残ったPendingだけを
        # 有効化後に再キューする。実行中のPending AI監査には触れない。
        await cls._enqueuePendingResolutionsIfEnabled()

    @classmethod
    async def stop(cls) -> None:
        """新規判定を停止し、実行中ワーカーとバックフィルを回収する。

        Returns:
            None
        """

        tasks = [task for task in (cls._backfill_task, cls._worker_task) if task is not None]
        for task in tasks:
            task.cancel()
        if len(tasks) > 0:
            await asyncio.gather(*tasks, return_exceptions=True)
        cls._backfill_task = None
        cls._backfill_handle = None
        cls._worker_task = None
        cls._queue = None
        cls._queued_ids = set()
        cls._running_ids = set()
        cls._rerun_ids = set()
        cls._ai_attempt_keys = {}
        cls._pending_recovery_completed = False
        cls._canonical_key_backfill_completed = False

    @classmethod
    async def enqueue(cls, recorded_program_id: int, *, input_changed: bool = False) -> None:
        """録画保存処理を待たせず、シリーズ判定対象IDだけをキューへ投入する。

        Args:
            recorded_program_id: DB保存済みのRecordedProgram ID。
            input_changed: 録画スキャンが判定入力を保存した直後の通知かどうか。

        Returns:
            None
        """

        if input_changed:
            # バックフィルの共有snapshotが古くなったことを、DB更新後の通知で判別する。
            cls._snapshot_generation += 1
        if input_changed:
            # メモリキューへ積んだ直後にプロセスが落ちても起動時に回収できるよう、先にDBへ
            # 軽量なPendingマーカーを残す。実fingerprint/evidenceはワーカー開始時に置き換える。
            # ただし手動訂正は管理者の明示的な最終判断なので、source/status/series_idは
            # 変更せずfingerprintだけをsentinelへ変える。これにより処理前に再起動しても
            # 起動時回収からManual decisionを最新メタデータへ再適用できる。
            # ここでResolverのlockを待つと外部API通信中に録画スキャンを止めるため、
            # DBの一意制約と条件付きUPDATEだけで競合を解決する。
            try:
                pending_fingerprint = '0' * 64
                pending_defaults: dict[str, object] = {
                    'input_fingerprint': pending_fingerprint,
                    'evidence_hash': '0' * 64,
                    'resolver_version': RECORDED_SERIES_RESOLVER_VERSION,
                    'normalized_title': '',
                    'status': 'Pending',
                    'source': None,
                    'series_id': None,
                    'wikipedia_page_id': None,
                    'candidate_set_hash': None,
                    'candidate_snapshot': None,
                    'ai_model': None,
                    'error_code': None,
                    'error_message': None,
                    'resolved_at': None,
                }
                manual_updated_count = await RecordedSeriesResolution.filter(
                    recorded_program_id=recorded_program_id,
                    source='Manual',
                ).update(input_fingerprint=pending_fingerprint)
                updated_count = await RecordedSeriesResolution.filter(
                    recorded_program_id=recorded_program_id,
                ).filter(
                    Q(source=None) | Q(source__not='Manual'),
                ).update(**pending_defaults)
                if manual_updated_count == 0 and updated_count == 0:
                    manual_exists = await RecordedSeriesResolution.filter(
                        recorded_program_id=recorded_program_id,
                        source='Manual',
                    ).exists()
                    if manual_exists is False:
                        try:
                            await RecordedSeriesResolution.create(
                                recorded_program_id=recorded_program_id,
                                input_fingerprint=pending_fingerprint,
                                evidence_hash='0' * 64,
                                resolver_version=RECORDED_SERIES_RESOLVER_VERSION,
                                normalized_title='',
                                status='Pending',
                                source=None,
                                series_id=None,
                                wikipedia_page_id=None,
                                candidate_set_hash=None,
                                candidate_snapshot=None,
                                ai_model=None,
                                error_code=None,
                                error_message=None,
                                resolved_at=None,
                            )
                        except IntegrityError:
                            # 存在確認とINSERTの間に手動訂正がcommitされた場合は、そのManual行を
                            # 保持する。通常行が別経路で作成された場合だけPendingへ更新する。
                            await RecordedSeriesResolution.filter(
                                recorded_program_id=recorded_program_id,
                            ).filter(
                                Q(source=None) | Q(source__not='Manual'),
                            ).update(**pending_defaults)
            except Exception as ex:
                # 一時的なDB競合ではメモリキューによる通常処理を継続し、耐久化失敗だけを記録する。
                logging.error(
                    f'[RecordedSeriesResolver] Failed to persist pending queue marker. '
                    f'recorded_program_id: {recorded_program_id}',
                    exc_info=ex,
                )
        # 無効化中も上のPending sentinelは残し、再有効化時に最新入力を回収できるようにする。
        # 設定破損で録画スキャン後段の索引・CM処理を止めない点は従来どおり。
        try:
            settings = RecordedSeriesSettingsStore.getSettings()
        except Exception as ex:
            logging.error('[RecordedSeriesResolver] Failed to load settings while enqueueing:', exc_info=ex)
            return
        if settings.enabled is False:
            return
        await cls.start()
        assert cls._queue is not None
        if recorded_program_id in cls._running_ids:
            # 実行中に録画メタデータが更新された場合は、現在の判定終了後に最新入力でもう一度実行する。
            cls._rerun_ids.add(recorded_program_id)
            return
        # 待機中なら実行開始時に最新DBを読むため、重複実行は不要。
        if recorded_program_id in cls._queued_ids:
            return
        cls._queued_ids.add(recorded_program_id)
        await cls._queue.put(recorded_program_id)

    @classmethod
    async def _runWorker(cls) -> None:
        """録画スキャンとは独立した単一ワーカーでキューを順次処理する。"""

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
            except _RecordedProgramSnapshotChanged:
                retry_after_current = True
            except Exception as ex:
                await cls._markUnexpectedFailure(recorded_program_id, type(ex).__name__)
                logging.error(
                    f'[RecordedSeriesResolver] Failed to resolve recorded program. '
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
    async def startBackfill(
        cls,
        *,
        trigger: Literal['Manual', 'StartupBackfill'] = 'Manual',
        force: bool = False,
    ) -> RecordedSeriesBackfillAccepted:
        """全録画の二段階シリーズ判定をバックグラウンドで開始する。

        Args:
            trigger: 手動実行または起動時バックフィルの区分。
            force: fingerprintが一致する確定済み録画も再判定するか。

        Returns:
            解析履歴IDと、進行中タスクを再利用したかどうか。
        """

        async with cls._backfill_start_lock:
            if cls._backfill_task is not None and cls._backfill_task.done() is False:
                assert cls._backfill_handle is not None
                return RecordedSeriesBackfillAccepted(cls._backfill_handle.execution.id, True)

            await cls.start()
            total = await RecordedProgram.filter(recorded_video__status='Recorded').count()
            handle = await AnalysisTaskTracker.start(
                'BatchSeriesResolution',
                title='録画シリーズ一括判定',
                trigger=trigger,
                total_count=total,
                initial_status='Queued',
                inherit_parent=False,
            )
            cls._backfill_handle = handle
            cls._backfill_task = asyncio.create_task(cls._runBackfill(handle, trigger=trigger, force=force))
            return RecordedSeriesBackfillAccepted(handle.execution.id, False)

    @classmethod
    async def _runBackfill(
        cls,
        handle: AnalysisTaskHandle,
        *,
        trigger: Literal['Manual', 'StartupBackfill'],
        force: bool,
    ) -> None:
        """強いクラスタを先に確定し、そのSeriesを曖昧候補へ渡す二段階バックフィルを実行する。"""

        try:
            async with AnalysisTaskTracker.track(
                'BatchSeriesResolution',
                trigger=trigger,
                existing_handle=handle,
            ) as history:
                await history.setStage('Clustering', 0.0)
                # 録画保存通知とsnapshot取得が交差した場合は、世代が安定するまで読み直す。
                while True:
                    snapshot_generation = cls._snapshot_generation
                    snapshots = await cls._loadProgramSnapshots(recorded_only=True)
                    if snapshot_generation == cls._snapshot_generation:
                        break
                parses = {
                    snapshot.id: ParseSeriesTitle(
                        snapshot.title,
                        snapshot.description,
                        snapshot.detail,
                        snapshot.genres,
                    )
                    for snapshot in snapshots
                }
                evidence = _buildClusterEvidence(snapshots, parses)

                # 明示話数・異なる内容を持つ反復番組を先に解決し、Series候補集合を構築する。
                strong_ids = [
                    snapshot.id
                    for snapshot in snapshots
                    if parses[snapshot.id].has_explicit_episode or evidence[snapshot.id].is_strong_repeat
                ]
                strong_id_set = set(strong_ids)
                weak_ids = [snapshot.id for snapshot in snapshots if snapshot.id not in strong_id_set]
                ordered_ids = strong_ids + weak_ids
                succeeded_count = 0
                failed_count = 0
                skipped_count = 0
                ai_request_count = 0
                await history.setStage('Resolving', 0.02)
                for index, recorded_program_id in enumerate(ordered_ids, start=1):
                    try:
                        result = await cls.resolveProgram(
                            recorded_program_id,
                            force=force,
                            allow_disabled=trigger == 'Manual',
                            snapshots=snapshots,
                            parses=parses,
                            cluster_evidence=evidence,
                            snapshot_generation=snapshot_generation,
                        )
                    except asyncio.CancelledError:
                        raise
                    except _RecordedProgramSnapshotChanged:
                        skipped_count += 1
                        await cls.enqueue(recorded_program_id)
                        await history.setCounts(
                            current=index,
                            total=len(ordered_ids),
                            succeeded=succeeded_count,
                            failed=failed_count,
                            skipped=skipped_count,
                        )
                        continue
                    except Exception as ex:
                        failed_count += 1
                        await cls._markUnexpectedFailure(recorded_program_id, type(ex).__name__)
                        logging.error(
                            f'[RecordedSeriesResolver] Backfill item failed. '
                            f'recorded_program_id: {recorded_program_id}',
                            exc_info=ex,
                        )
                        await history.setCounts(
                            current=index,
                            total=len(ordered_ids),
                            succeeded=succeeded_count,
                            failed=failed_count,
                            skipped=skipped_count,
                        )
                        continue
                    if result.status in {'Resolved', 'NotSeries', 'NeedsReview'}:
                        succeeded_count += 1
                    elif result.status == 'Skipped':
                        skipped_count += 1
                    else:
                        failed_count += 1
                    ai_request_count += int(result.ai_requested)
                    await history.setCounts(
                        current=index,
                        total=len(ordered_ids),
                        succeeded=succeeded_count,
                        failed=failed_count,
                        skipped=skipped_count,
                    )
                await history.finish(
                    'Failed' if failed_count > 0 else 'Succeeded',
                    summary=cast(dict[str, object], {
                        'resolved_or_reviewed': succeeded_count,
                        'failed': failed_count,
                        'skipped': skipped_count,
                        'ai_requests': ai_request_count,
                    }),
                    error_code='PartialFailure' if failed_count > 0 else None,
                )
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logging.error('[RecordedSeriesResolver] Backfill failed:', exc_info=ex)
        finally:
            cls._backfill_task = None
            cls._backfill_handle = None

    @staticmethod
    async def _markUnexpectedFailure(recorded_program_id: int, error_code: str) -> None:
        """想定外例外を秘密情報なしの失敗状態へ確定する。"""

        try:
            await RecordedSeriesResolution.filter(
                recorded_program_id=recorded_program_id,
                status='Pending',
            ).filter(
                Q(source=None) | Q(source__not='Manual'),
            ).update(
                status='Failed',
                source=None,
                error_code=error_code[:255],
                error_message=None,
                resolved_at=None,
            )
        except Exception as ex:
            logging.error(
                f'[RecordedSeriesResolver] Failed to persist unexpected failure. '
                f'recorded_program_id: {recorded_program_id}',
                exc_info=ex,
            )

    @classmethod
    async def _loadProgramSnapshots(cls, *, recorded_only: bool) -> list[_ProgramSnapshot]:
        """クラスタ判定に必要なRecordedProgram列だけを一括取得する。"""

        query = RecordedProgram.all()
        if recorded_only is True:
            query = query.filter(recorded_video__status='Recorded')
        rows = await query.order_by('id').values(
            'id',
            'channel_id',
            'network_id',
            'service_id',
            'event_id',
            'title',
            'description',
            'detail',
            'genres',
            'start_time',
        )
        return [
            _ProgramSnapshot(
                id=row['id'],
                channel_id=row['channel_id'],
                network_id=row['network_id'],
                service_id=row['service_id'],
                event_id=row['event_id'],
                title=row['title'],
                description=row['description'],
                detail=row['detail'],
                genres=row['genres'],
                start_time=row['start_time'],
            )
            for row in rows
        ]

    @classmethod
    async def _loadProgramSnapshot(cls, recorded_program_id: int) -> _ProgramSnapshot | None:
        """判定保存直前の競合検証向けに、1録画の最新入力だけを取得する。"""

        rows = await RecordedProgram.filter(
            id=recorded_program_id,
            recorded_video__status='Recorded',
        ).limit(1).values(
            'id',
            'channel_id',
            'network_id',
            'service_id',
            'event_id',
            'title',
            'description',
            'detail',
            'genres',
            'start_time',
        )
        if len(rows) == 0:
            return None
        row = rows[0]
        return _ProgramSnapshot(
            id=row['id'],
            channel_id=row['channel_id'],
            network_id=row['network_id'],
            service_id=row['service_id'],
            event_id=row['event_id'],
            title=row['title'],
            description=row['description'],
            detail=row['detail'],
            genres=row['genres'],
            start_time=row['start_time'],
        )

    @classmethod
    async def _matchEPG(
        cls,
        snapshot: _ProgramSnapshot,
        local_parse: SeriesTitleParseResult,
    ) -> _EPGEnrichment | None:
        """話名だけの概要を保持EPGへ一意照合して話数を補完する。"""

        if local_parse.has_explicit_episode:
            return None
        description_key = BuildSeriesGroupingKey(snapshot.description)
        if len(description_key) < 4 or snapshot.network_id is None or snapshot.service_id is None:
            return None

        same_service_rows = await Program.filter(
            network_id=snapshot.network_id,
            service_id=snapshot.service_id,
        ).values('id', 'title', 'description', 'detail', 'genres')

        same_service_matches = cls._collectEPGMatches(snapshot, local_parse, same_service_rows)
        if len(same_service_matches) == 1:
            return next(iter(same_service_matches.values()))
        if len(same_service_matches) > 1:
            return None

        # 同時放送・リピート放送では、録画局側のEPGから話数や話名が省かれ、別サービス側だけに
        # 完全なメタデータが残ることがある。作品名をSQL上の包含条件にして候補を狭めたうえで、
        # 話名・作品名・話数の一意性をもう一度検証する。
        cross_service_rows = await Program.filter(
            title__icontains=local_parse.series_title,
        ).values('id', 'title', 'description', 'detail', 'genres')
        cross_service_matches = cls._collectEPGMatches(snapshot, local_parse, cross_service_rows)
        if len(cross_service_matches) != 1:
            return None
        return next(iter(cross_service_matches.values()))

    @staticmethod
    def _collectEPGMatches(
        snapshot: _ProgramSnapshot,
        local_parse: SeriesTitleParseResult,
        rows: list[dict[str, object]],
    ) -> dict[tuple[str, str | None, str | None], _EPGEnrichment]:
        """取得済みEPGから、同じ作品かつ録画概要の話名に一致する候補を集める。"""

        description_key = BuildSeriesGroupingKey(snapshot.description)
        matches: dict[tuple[str, str | None, str | None], _EPGEnrichment] = {}
        for row in rows:
            row_description = row.get('description')
            row_detail = row.get('detail')
            row_title = row.get('title')
            row_genres = row.get('genres')
            row_id = row.get('id')
            if (
                not isinstance(row_description, str) or
                not isinstance(row_detail, dict) or
                not isinstance(row_title, str) or
                not isinstance(row_genres, list) or
                not isinstance(row_id, str)
            ):
                continue
            typed_detail = cast(dict[str, str], row_detail)
            typed_genres = cast(list[Genre], row_genres)
            candidate_texts = [row_description, *typed_detail.values()]
            candidate_keys = [BuildSeriesGroupingKey(value) for value in candidate_texts]
            if not any(
                description_key == key or
                (len(description_key) >= 4 and description_key in key)
                for key in candidate_keys
            ):
                continue
            candidate_parse = ParseSeriesTitle(
                row_title,
                row_description,
                typed_detail,
                typed_genres,
            )
            if candidate_parse.has_explicit_episode is False:
                continue
            if candidate_parse.subtitle is None and local_parse.subtitle is not None:
                candidate_parse = SeriesTitleParseResult(
                    series_title=candidate_parse.series_title,
                    normalized_key=candidate_parse.normalized_key,
                    episode_number=candidate_parse.episode_number,
                    subtitle=local_parse.subtitle,
                    season_number=candidate_parse.season_number,
                    episode_source=candidate_parse.episode_source,
                    has_explicit_episode=candidate_parse.has_explicit_episode,
                    is_hard_standalone=candidate_parse.is_hard_standalone,
                    is_soft_standalone=candidate_parse.is_soft_standalone,
                )
            ratio, common_length = _seriesSimilarity(local_parse.series_title, candidate_parse.series_title)
            if ratio < 0.55 and common_length < 6:
                continue
            key = (
                candidate_parse.normalized_key,
                candidate_parse.episode_number,
                BuildSeriesGroupingKey(candidate_parse.subtitle or snapshot.description),
            )
            matches[key] = _EPGEnrichment(candidate_parse, row_id)
        return matches

    @classmethod
    async def _findExistingSeriesCandidates(cls, title: str) -> list[Series]:
        """文字列包含・共通部分が十分な既存SeriesだけをAI候補へ渡す。"""

        scored: list[tuple[float, int, Series]] = []
        title_key = BuildSeriesGroupingKey(title)
        for series in await Series.all():
            series_key = BuildSeriesGroupingKey(series.title)
            ratio, common_length = _seriesSimilarity(title, series.title)
            contains = min(len(title_key), len(series_key)) >= 4 and (
                title_key in series_key or series_key in title_key
            )
            if contains or common_length >= 5 or ratio >= 0.50:
                scored.append((ratio, common_length, series))
        scored.sort(key=lambda item: (item[1], item[0]), reverse=True)
        return [item[2] for item in scored[:5]]

    @classmethod
    async def _getOrCreateResolution(
        cls,
        snapshot: _ProgramSnapshot,
        cluster: _ClusterEvidence,
    ) -> RecordedSeriesResolution:
        """RecordedProgramごとの判定状態を作成し、今回入力でPendingへ更新する。"""

        resolution = await RecordedSeriesResolution.get_or_none(recorded_program_id=snapshot.id)
        if resolution is None:
            resolution = await RecordedSeriesResolution.create(
                recorded_program_id=snapshot.id,
                input_fingerprint=_buildInputFingerprint(snapshot),
                evidence_hash=cluster.evidence_hash,
                resolver_version=RECORDED_SERIES_RESOLVER_VERSION,
                normalized_title=cluster.normalized_key,
                status='Pending',
            )
        return resolution

    @classmethod
    async def _markResolution(
        cls,
        resolution: RecordedSeriesResolution,
        *,
        status: Literal['Resolved', 'NotSeries', 'NeedsReview', 'Failed'],
        source: RecordedSeriesSource | None,
        series_id: int | None = None,
        wikipedia_page_id: int | None = None,
        candidate_set_hash: str | None = None,
        candidate_snapshot: list[SeriesChoiceCandidate] | None = None,
        ai_model: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        connection: BaseDBAsyncClient | None = None,
    ) -> None:
        """秘密情報を含まない判定状態をRecordedSeriesResolutionへ保存する。"""

        resolution.status = status
        resolution.source = source
        resolution.series_id = series_id
        resolution.wikipedia_page_id = wikipedia_page_id
        resolution.candidate_set_hash = candidate_set_hash
        resolution.candidate_snapshot = cast(list[object] | None, candidate_snapshot)
        resolution.ai_model = ai_model
        resolution.error_code = error_code
        resolution.error_message = error_message
        resolution.resolved_at = datetime.now(tz=JST) if status in {'Resolved', 'NotSeries'} else None
        await resolution.save(using_db=connection)

    @staticmethod
    async def _reconcileBroadcastPeriod(
        period_id: int,
        *,
        connection: BaseDBAsyncClient,
    ) -> None:
        """再所属後の残存録画から放送期間を再計算し、空期間は削除する。"""

        period = await SeriesBroadcastPeriod.filter(id=period_id).using_db(connection).first()
        if period is None:
            return
        start_times = cast(list[datetime], await RecordedProgram.filter(
            series_broadcast_period_id=period_id,
        ).using_db(connection).values_list('start_time', flat=True))
        if len(start_times) == 0:
            await period.delete(using_db=connection)
            return
        start_date = min(start_times).date()
        end_date = max(start_times).date()
        if period.start_date == start_date and period.end_date == end_date:
            return
        period.start_date = start_date
        period.end_date = end_date
        await period.save(update_fields=['start_date', 'end_date'], using_db=connection)

    @staticmethod
    async def _findLegacySeriesByCanonicalKey(
        canonical_key: str,
        *,
        connection: BaseDBAsyncClient,
    ) -> Series | None:
        """canonical key未設定の既存SeriesをPython側の正規化規則で照合する。"""

        legacy_series_list = await Series.filter(canonical_key=None).using_db(connection).order_by('id')
        for legacy_series in legacy_series_list:
            normalized_key = BuildSeriesGroupingKey(legacy_series.title)
            if normalized_key != '' and _buildRuleKeyHash(normalized_key) == canonical_key:
                return legacy_series
        return None

    @classmethod
    async def updateSeriesMetadata(
        cls,
        series_id: int,
        *,
        title: str,
        description: str,
        expected_title: str,
        expected_description: str,
    ) -> None:
        """管理者が編集したSeries名・説明を関連録画と原子的に同期する。

        Args:
            series_id: 更新するSeries ID。
            title: 前後空白を除去して保存する新しい表示名。
            description: 保存する新しい説明。
            expected_title: 編集開始時に取得したSeries名。
            expected_description: 編集開始時に取得したSeries説明。

        Returns:
            None

        Raises:
            RecordedSeriesTargetNotFoundError: 対象Seriesが存在しない場合。
            RecordedSeriesInvalidTitleError: タイトルから有効な正規化キーを作れない場合。
            RecordedSeriesTitleConflictError: 別Seriesまたは既存Ruleが新タイトルのキーを所有する場合。
            RecordedSeriesMetadataStaleError: 編集開始後に対象Seriesが更新された場合。
            RecordedSeriesResolverBusyError: 別のSeries判定・更新が実行中の場合。
        """

        cleaned_title = title.strip()
        normalized_title = BuildSeriesGroupingKey(cleaned_title)
        if normalized_title == '':
            raise RecordedSeriesInvalidTitleError
        normalized_title_hash = _buildRuleKeyHash(normalized_title)

        # 長時間のMediaWiki/AI判定の後ろでHTTPリクエストを待たせると、
        # 画面がタイムアウトした後に遅れてcommitされる。使用中なら即時再試行を促す。
        if cls._resolve_lock.locked():
            raise RecordedSeriesResolverBusyError

        # 自動Resolverが旧Series名を読み取った後に判定結果をcommitする競合を防ぐため、
        # 録画の手動割当と同じlockを使い、Seriesと全RecordedProgramを一括更新する。
        async with cls._resolve_lock:
            async with transactions.in_transaction() as connection:
                series = await Series.filter(id=series_id).using_db(connection).first()
                if series is None:
                    raise RecordedSeriesTargetNotFoundError

                # updated_atは録画追加・再判定でも更新されるため、編集対象の2項目だけでlost updateを
                # 検出する。比較と更新を同一transaction内に置き、全録画名の巻き戻しも防ぐ。
                if series.title != expected_title or series.description != expected_description:
                    raise RecordedSeriesMetadataStaleError

                current_normalized_title = BuildSeriesGroupingKey(series.title)
                title_changed = cleaned_title != series.title
                normalized_title_changed = normalized_title != current_normalized_title
                target_rule: RecordedSeriesRule | None = None

                # 正規化キーが同じ表記修正でも、完全に同じ表示名の別Seriesを新たに作る変更だけは拒否する。
                if title_changed:
                    exact_title_owner = await Series.filter(title=cleaned_title) \
                        .exclude(id=series.id).using_db(connection).first()
                    if exact_title_owner is not None:
                        raise RecordedSeriesTitleConflictError

                if normalized_title_changed:
                    # canonical_keyは元のEPG名を将来も同じSeriesへ寄せるstable aliasなので、
                    # renameでは書き換えない。一方、新しい正規化キーを別Seriesが所有する場合は、
                    # 将来の自動判定先が曖昧になるため暗黙mergeせず拒否する。
                    other_series_list = await Series.exclude(id=series.id).using_db(connection).values(
                        'canonical_key',
                        'title',
                    )
                    for other_series in other_series_list:
                        if (
                            other_series['canonical_key'] == normalized_title_hash or
                            BuildSeriesGroupingKey(str(other_series['title'])) == normalized_title
                        ):
                            raise RecordedSeriesTitleConflictError

                    # 新表示名と同じ入力キーのRuleが別Series・単発を指す状態でrenameを許すと、
                    # 一覧上の名前と次回自動判定の所属先が食い違うため409で止める。
                    target_rule = await RecordedSeriesRule.filter(
                        key_hash=normalized_title_hash,
                    ).using_db(connection).first()
                    if target_rule is not None and (
                        target_rule.decision != 'Series' or
                        target_rule.series_id != series.id
                    ):
                        raise RecordedSeriesTitleConflictError

                updated_at = datetime.now(tz=JST)
                series.title = cleaned_title
                series.description = description
                series.updated_at = updated_at
                await series.save(
                    update_fields=['title', 'description', 'updated_at'],
                    using_db=connection,
                )

                # RecordedProgram.series_titleはAPI・検索・Media Sessionが直接参照する非正規化列なので、
                # Series.titleだけを変えて表示が混在しないよう、録画状態を問わず全所属行へ反映する。
                await RecordedProgram.filter(series_id=series.id).using_db(connection).update(
                    series_title=cleaned_title,
                    updated_at=updated_at,
                )

                # 旧canonical keyと過去Ruleは入力履歴として保持したまま、新しい正規化名も
                # 同じSeriesへ戻せるManual aliasを追加する。これにより空白・記号の表記揺れでも
                # rename直後に別Seriesを作らず、管理者の明示変更を将来の判定へ再利用できる。
                if normalized_title_changed:
                    await RecordedSeriesRule.update_or_create(
                        key_hash=normalized_title_hash,
                        defaults={
                            'normalized_key': normalized_title,
                            'display_title': cleaned_title,
                            'decision': 'Series',
                            'series_id': series.id,
                            'wikipedia_page_id': series.wikipedia_page_id,
                            'source': 'Manual',
                            'confidence': 1.0,
                            'evidence_hash': _sha256JSON({
                                'source': 'ManualSeriesRename',
                                'series_id': series.id,
                                'normalized_key': normalized_title,
                            }),
                            'resolver_version': RECORDED_SERIES_RESOLVER_VERSION,
                        },
                        using_db=connection,
                    )

    @classmethod
    async def assignProgram(
        cls,
        recorded_program_id: int,
        *,
        decision: Literal['Series', 'NotSeries'],
        series_id: int | None = None,
        series_title: str | None = None,
    ) -> None:
        """管理者の手動訂正を録画・Series・Rule・Resolutionへ原子的に反映する。

        Args:
            recorded_program_id: 割当を変更するRecordedProgram ID。
            decision: Seriesへ割り当てるか、単発番組にするか。
            series_id: 再利用する既存Series ID。
            series_title: 作成またはタイトル正規化キーで再利用するSeries名。

        Returns:
            None

        Raises:
            ValueError: decisionとSeries指定の組み合わせが不正な場合。
            RecordedSeriesProgramNotFoundError: 対象録画が存在しない場合。
            RecordedSeriesTargetNotFoundError: 指定された既存Seriesが存在しない場合。
            RecordedSeriesChannelUnavailableError: Series API用の放送期間を作れない場合。
            RecordedSeriesInvalidTitleError: 指定タイトルを有効なキーへ正規化できない場合。
        """

        # API層以外から誤った形で呼ばれても半端な割当を作らない。
        if decision == 'Series':
            if (series_id is None) == (series_title is None):
                raise ValueError('Series assignment requires exactly one target.')
        elif decision == 'NotSeries':
            if series_id is not None or series_title is not None:
                raise ValueError('NotSeries assignment cannot have a target.')
        else:
            raise ValueError('Unsupported recorded series assignment decision.')

        # 自動Resolverの読取りからcommitまでと同じlockを使うことで、進行中の
        # MediaWiki/AI判定が手動訂正の後からcommitして上書きする競合を防ぐ。
        async with cls._resolve_lock:
            async with transactions.in_transaction() as connection:
                recorded_program = await RecordedProgram.filter(
                    id=recorded_program_id,
                    recorded_video__status='Recorded',
                ).using_db(connection).first()
                if recorded_program is None:
                    raise RecordedSeriesProgramNotFoundError

                # 現在の録画入力と同じフィールドで監査fingerprintを作り、秘密情報や
                # 候補応答は保存しない。個別訂正はこのResolutionで保護し、同じ
                # 正規化番組名への将来の再利用はManual Ruleが担う。
                snapshot = _ProgramSnapshot(
                    id=recorded_program.id,
                    channel_id=recorded_program.channel_id,
                    network_id=recorded_program.network_id,
                    service_id=recorded_program.service_id,
                    event_id=recorded_program.event_id,
                    title=recorded_program.title,
                    description=recorded_program.description,
                    detail=recorded_program.detail,
                    genres=recorded_program.genres,
                    start_time=recorded_program.start_time,
                )
                parse_result = ParseSeriesTitle(
                    snapshot.title,
                    snapshot.description,
                    snapshot.detail,
                    snapshot.genres,
                )
                normalized_key = parse_result.normalized_key or BuildSeriesGroupingKey(snapshot.title)
                if normalized_key == '':
                    # 署名する番組名が全て記号でもRule行自体は必ず残し、
                    # この録画のManual Resolutionを自動判定から保護する。
                    normalized_key = f'__recorded_program_{recorded_program.id}'

                target_series: Series | None = None
                target_period: SeriesBroadcastPeriod | None = None
                previous_period_id = recorded_program.series_broadcast_period_id
                if decision == 'Series':
                    # Series APIはチャンネル別の放送期間経由で録画を公開するため、
                    # channel不明のままseries_idだけが付く半端な状態は拒否する。
                    if recorded_program.channel_id is None:
                        raise RecordedSeriesChannelUnavailableError

                    if series_id is not None:
                        target_series = await Series.filter(id=series_id).using_db(connection).first()
                        if target_series is None:
                            raise RecordedSeriesTargetNotFoundError
                    else:
                        assert series_title is not None
                        cleaned_series_title = series_title.strip()
                        target_title_key = BuildSeriesGroupingKey(cleaned_series_title)
                        if target_title_key == '':
                            raise RecordedSeriesInvalidTitleError
                        canonical_key = _buildRuleKeyHash(target_title_key)
                        target_series = await Series.filter(
                            canonical_key=canonical_key,
                        ).using_db(connection).first()
                        if target_series is None:
                            # canonical_key導入前のSeriesも、同じ表示名なら重複作成しない。
                            target_series = await Series.filter(
                                title=cleaned_series_title,
                            ).using_db(connection).order_by('id').first()
                        if target_series is None:
                            # 空白・記号など表記だけが異なる移行前Seriesも再利用する。
                            target_series = await cls._findLegacySeriesByCanonicalKey(
                                canonical_key,
                                connection=connection,
                            )
                        if target_series is None:
                            target_series = await Series.create(
                                canonical_key=canonical_key,
                                title=cleaned_series_title,
                                description=recorded_program.description,
                                genres=recorded_program.genres,
                                using_db=connection,
                            )
                        elif target_series.canonical_key is None:
                            target_series.canonical_key = canonical_key
                            await target_series.save(
                                update_fields=['canonical_key', 'updated_at'],
                                using_db=connection,
                            )

                    recording_date = recorded_program.start_time.date()
                    target_period = await SeriesBroadcastPeriod.filter(
                        series_id=target_series.id,
                        channel_id=recorded_program.channel_id,
                    ).using_db(connection).order_by('id').first()
                    if target_period is None:
                        target_period = await SeriesBroadcastPeriod.create(
                            series_id=target_series.id,
                            channel_id=recorded_program.channel_id,
                            start_date=recording_date,
                            end_date=recording_date,
                            using_db=connection,
                        )

                    recorded_program.series_id = target_series.id
                    recorded_program.series_broadcast_period_id = target_period.id
                    recorded_program.series_title = target_series.title
                    # シリーズの所属先だけを訂正するとき、既存の話数・話名は
                    # TS/EPG解析から得た情報なので再解析・消去せずそのまま保持する。一方、
                    # 過去のNotSeries判定で空なら現在タイトルのローカル解析結果だけを補完する。
                    recorded_program_update_fields = [
                        'series_id',
                        'series_broadcast_period_id',
                        'series_title',
                        'updated_at',
                    ]
                    if recorded_program.episode_number is None and parse_result.episode_number is not None:
                        recorded_program.episode_number = parse_result.episode_number
                        recorded_program_update_fields.append('episode_number')
                    if recorded_program.subtitle is None and parse_result.subtitle is not None:
                        recorded_program.subtitle = parse_result.subtitle
                        recorded_program_update_fields.append('subtitle')
                    await recorded_program.save(
                        update_fields=recorded_program_update_fields,
                        using_db=connection,
                    )
                    await target_series.save(update_fields=['updated_at'], using_db=connection)
                else:
                    recorded_program.series_id = None
                    recorded_program.series_broadcast_period_id = None
                    recorded_program.series_title = None
                    recorded_program.episode_number = None
                    recorded_program.subtitle = None
                    await recorded_program.save(update_fields=[
                        'series_id',
                        'series_broadcast_period_id',
                        'series_title',
                        'episode_number',
                        'subtitle',
                        'updated_at',
                    ], using_db=connection)

                # 移動元と移動先の両方を実際の残存録画日から再集計する。
                # 移動元が空になった場合は _reconcileBroadcastPeriod() が期間行を削除する。
                affected_period_ids = {
                    period_id
                    for period_id in (
                        previous_period_id,
                        target_period.id if target_period is not None else None,
                    )
                    if period_id is not None
                }
                for affected_period_id in affected_period_ids:
                    await cls._reconcileBroadcastPeriod(affected_period_id, connection=connection)

                input_fingerprint = _buildInputFingerprint(snapshot)
                evidence_hash = _sha256JSON({
                    'source': 'Manual',
                    'input_fingerprint': input_fingerprint,
                    'normalized_key': normalized_key,
                    'decision': decision,
                    'series_id': target_series.id if target_series is not None else None,
                })
                await RecordedSeriesRule.update_or_create(
                    key_hash=_buildRuleKeyHash(normalized_key),
                    defaults={
                        'normalized_key': normalized_key,
                        'display_title': parse_result.series_title or recorded_program.title,
                        'decision': decision,
                        'series_id': target_series.id if target_series is not None else None,
                        'wikipedia_page_id': (
                            target_series.wikipedia_page_id if target_series is not None else None
                        ),
                        'source': 'Manual',
                        'confidence': 1.0,
                        'evidence_hash': evidence_hash,
                        'resolver_version': RECORDED_SERIES_RESOLVER_VERSION,
                    },
                    using_db=connection,
                )
                await RecordedSeriesResolution.update_or_create(
                    recorded_program_id=recorded_program.id,
                    defaults={
                        'series_id': target_series.id if target_series is not None else None,
                        'input_fingerprint': input_fingerprint,
                        'evidence_hash': evidence_hash,
                        'resolver_version': RECORDED_SERIES_RESOLVER_VERSION,
                        'normalized_title': normalized_key,
                        'status': 'Resolved' if decision == 'Series' else 'NotSeries',
                        'source': 'Manual',
                        'wikipedia_page_id': (
                            target_series.wikipedia_page_id if target_series is not None else None
                        ),
                        'candidate_set_hash': None,
                        'candidate_snapshot': None,
                        'ai_model': None,
                        'error_code': None,
                        'error_message': None,
                        'resolved_at': datetime.now(tz=JST),
                    },
                    using_db=connection,
                )

    @classmethod
    async def _applySeries(
        cls,
        *,
        snapshot: _ProgramSnapshot,
        parse_result: SeriesTitleParseResult,
        cluster: _ClusterEvidence,
        resolution: RecordedSeriesResolution,
        expected_generation: int,
        source: RecordedSeriesSource,
        title: str,
        description: str,
        confidence: float,
        wikipedia_page_id: int | None = None,
        existing_series_id: int | None = None,
        candidate_set_hash: str | None = None,
        candidate_snapshot: list[SeriesChoiceCandidate] | None = None,
        ai_model: str | None = None,
    ) -> Series:
        """Series・放送期間・録画・positive ruleを同一トランザクションで冪等更新する。"""

        canonical_key = _buildRuleKeyHash(BuildSeriesGroupingKey(title))
        async with transactions.in_transaction() as connection:
            series: Series | None = None
            if existing_series_id is not None:
                series = await Series.filter(id=existing_series_id).using_db(connection).first()
                if series is None:
                    raise RecordedSeriesAIError('SelectedSeriesNoLongerExists')
            if series is None and wikipedia_page_id is not None:
                series = await Series.filter(wikipedia_page_id=wikipedia_page_id).using_db(connection).first()
            if series is None:
                series = await Series.filter(canonical_key=canonical_key).using_db(connection).first()
            if series is None:
                # 旧実装やEPG由来でcanonical_keyを持たない既存Seriesも重複作成しない。
                series = await Series.filter(title=title).using_db(connection).order_by('id').first()
            if series is None:
                # 完全一致しない表記揺れも、同じ正規化キーなら移行前Seriesへひも付ける。
                series = await cls._findLegacySeriesByCanonicalKey(
                    canonical_key,
                    connection=connection,
                )
            if series is None:
                series = await Series.create(
                    title=title,
                    description=description,
                    genres=snapshot.genres,
                    canonical_key=canonical_key,
                    wikipedia_page_id=wikipedia_page_id,
                    using_db=connection,
                )
            else:
                changed_fields: list[str] = []
                if series.canonical_key is None:
                    canonical_key_owner = await Series.filter(
                        canonical_key=canonical_key,
                    ).exclude(id=series.id).using_db(connection).first()
                    # 移行前から同じ正規化名のSeriesが複数ある場合、
                    # 管理者が明示選択した行は維持し、既存代表の一意キーは奪わない。
                    if canonical_key_owner is None:
                        series.canonical_key = canonical_key
                        changed_fields.append('canonical_key')
                if series.wikipedia_page_id is None and wikipedia_page_id is not None:
                    wikipedia_page_owner = await Series.filter(
                        wikipedia_page_id=wikipedia_page_id,
                    ).exclude(id=series.id).using_db(connection).first()
                    if wikipedia_page_owner is None:
                        series.wikipedia_page_id = wikipedia_page_id
                        changed_fields.append('wikipedia_page_id')
                if len(changed_fields) > 0:
                    changed_fields.append('updated_at')
                    await series.save(update_fields=changed_fields, using_db=connection)

            recorded_program = await RecordedProgram.filter(id=snapshot.id).using_db(connection).first()
            if recorded_program is None:
                raise RecordedSeriesAIError('RecordedProgramNoLongerExists')
            if expected_generation != cls._snapshot_generation:
                raise _RecordedProgramSnapshotChanged
            _assertRecordedProgramSnapshotCurrent(recorded_program, snapshot)
            previous_period_id = recorded_program.series_broadcast_period_id
            period: SeriesBroadcastPeriod | None = None
            if recorded_program.channel_id is not None:
                period = await SeriesBroadcastPeriod.filter(
                    series_id=series.id,
                    channel_id=recorded_program.channel_id,
                ).using_db(connection).first()
                recording_date = recorded_program.start_time.date()
                if period is None:
                    period = await SeriesBroadcastPeriod.create(
                        series_id=series.id,
                        channel_id=recorded_program.channel_id,
                        start_date=recording_date,
                        end_date=recording_date,
                        using_db=connection,
                    )
                else:
                    changed_period_fields: list[str] = []
                    if recording_date < period.start_date:
                        period.start_date = recording_date
                        changed_period_fields.append('start_date')
                    if recording_date > period.end_date:
                        period.end_date = recording_date
                        changed_period_fields.append('end_date')
                    if len(changed_period_fields) > 0:
                        await period.save(update_fields=changed_period_fields, using_db=connection)

            recorded_program.series_id = series.id
            recorded_program.series_broadcast_period_id = period.id if period is not None else None
            recorded_program.series_title = series.title
            # Manual割当・再適用では、既存のTS/EPG由来話数と話名を優先し、欠けている値だけを
            # 現在タイトルのローカル解析で補完する。自動判定時は従来どおり解析結果へ更新する。
            if source == 'Manual':
                if recorded_program.episode_number is None:
                    recorded_program.episode_number = parse_result.episode_number
                if recorded_program.subtitle is None:
                    recorded_program.subtitle = parse_result.subtitle
            else:
                recorded_program.episode_number = parse_result.episode_number
                recorded_program.subtitle = parse_result.subtitle
            await recorded_program.save(update_fields=[
                'series_id',
                'series_broadcast_period_id',
                'series_title',
                'episode_number',
                'subtitle',
                'updated_at',
            ], using_db=connection)

            affected_period_ids = {
                period_id
                for period_id in (previous_period_id, period.id if period is not None else None)
                if period_id is not None
            }
            for affected_period_id in affected_period_ids:
                await cls._reconcileBroadcastPeriod(affected_period_id, connection=connection)

            # シリーズ一覧が新しい録画の追加順に上がるよう、関連付け成功時にSeriesも更新する。
            await series.save(update_fields=['updated_at'], using_db=connection)

            await RecordedSeriesRule.update_or_create(
                key_hash=_buildRuleKeyHash(cluster.normalized_key),
                defaults={
                    'normalized_key': cluster.normalized_key,
                    'display_title': cluster.display_title,
                    'decision': 'Series',
                    'series_id': series.id,
                    'wikipedia_page_id': wikipedia_page_id,
                    'source': source,
                    'confidence': confidence,
                    'evidence_hash': cluster.evidence_hash,
                    'resolver_version': RECORDED_SERIES_RESOLVER_VERSION,
                },
                using_db=connection,
            )
            await cls._markResolution(
                resolution,
                status='Resolved',
                source=source,
                series_id=series.id,
                wikipedia_page_id=wikipedia_page_id,
                candidate_set_hash=candidate_set_hash,
                candidate_snapshot=candidate_snapshot,
                ai_model=ai_model,
                connection=connection,
            )
            return series

    @classmethod
    async def _applyNotSeries(
        cls,
        *,
        snapshot: _ProgramSnapshot,
        cluster: _ClusterEvidence,
        resolution: RecordedSeriesResolution,
        expected_generation: int,
        source: RecordedSeriesSource,
        confidence: float,
        candidate_set_hash: str | None = None,
        candidate_snapshot: list[SeriesChoiceCandidate] | None = None,
        ai_model: str | None = None,
        persist_rule: bool = True,
    ) -> None:
        """単発判定とnegative ruleを保存し、以前の自動シリーズ関連付けを解除する。"""

        async with transactions.in_transaction() as connection:
            recorded_program = await RecordedProgram.filter(id=snapshot.id).using_db(connection).first()
            if recorded_program is None:
                raise RecordedSeriesAIError('RecordedProgramNoLongerExists')
            if expected_generation != cls._snapshot_generation:
                raise _RecordedProgramSnapshotChanged
            _assertRecordedProgramSnapshotCurrent(recorded_program, snapshot)
            previous_period_id = recorded_program.series_broadcast_period_id
            recorded_program.series_id = None
            recorded_program.series_broadcast_period_id = None
            recorded_program.series_title = None
            recorded_program.episode_number = None
            recorded_program.subtitle = None
            await recorded_program.save(update_fields=[
                'series_id',
                'series_broadcast_period_id',
                'series_title',
                'episode_number',
                'subtitle',
                'updated_at',
            ], using_db=connection)
            if previous_period_id is not None:
                await cls._reconcileBroadcastPeriod(previous_period_id, connection=connection)
            if persist_rule:
                await RecordedSeriesRule.update_or_create(
                    key_hash=_buildRuleKeyHash(cluster.normalized_key),
                    defaults={
                        'normalized_key': cluster.normalized_key,
                        'display_title': cluster.display_title,
                        'decision': 'NotSeries',
                        'series_id': None,
                        'wikipedia_page_id': None,
                        'source': source,
                        'confidence': confidence,
                        'evidence_hash': cluster.evidence_hash,
                        'resolver_version': RECORDED_SERIES_RESOLVER_VERSION,
                    },
                    using_db=connection,
                )
            await cls._markResolution(
                resolution,
                status='NotSeries',
                source=source,
                candidate_set_hash=candidate_set_hash,
                candidate_snapshot=candidate_snapshot,
                ai_model=ai_model,
                connection=connection,
            )

    @classmethod
    async def _applyNeedsReview(
        cls,
        *,
        snapshot: _ProgramSnapshot,
        resolution: RecordedSeriesResolution,
        expected_generation: int,
        source: RecordedSeriesSource,
        error_code: str,
        candidate_set_hash: str | None = None,
        candidate_snapshot: list[SeriesChoiceCandidate] | None = None,
        ai_model: str | None = None,
        clear_series: bool,
        series_id: int | None = None,
    ) -> None:
        """最新入力を再確認し、必要なら旧関連を外してNeedsReviewを保存する。"""

        async with transactions.in_transaction() as connection:
            recorded_program = await RecordedProgram.filter(id=snapshot.id).using_db(connection).first()
            if recorded_program is None:
                raise RecordedSeriesAIError('RecordedProgramNoLongerExists')
            if expected_generation != cls._snapshot_generation:
                raise _RecordedProgramSnapshotChanged
            _assertRecordedProgramSnapshotCurrent(recorded_program, snapshot)
            if clear_series:
                previous_period_id = recorded_program.series_broadcast_period_id
                recorded_program.series_id = None
                recorded_program.series_broadcast_period_id = None
                recorded_program.series_title = None
                cleared_series_fields = [
                    'series_id',
                    'series_broadcast_period_id',
                    'series_title',
                    'updated_at',
                ]
                # Manual割当はchannel復帰後に同じSeriesへ戻す一時的な切り離しなので、
                # 管理者が訂正対象にしていない既存の話数・話名まで失わない。
                if source != 'Manual':
                    recorded_program.episode_number = None
                    recorded_program.subtitle = None
                    cleared_series_fields.extend(['episode_number', 'subtitle'])
                await recorded_program.save(
                    update_fields=cleared_series_fields,
                    using_db=connection,
                )
                if previous_period_id is not None:
                    await cls._reconcileBroadcastPeriod(previous_period_id, connection=connection)
            elif recorded_program.series_id is not None and recorded_program.channel_id is not None:
                # force再判定で新しい確定根拠を得られないときは、最後の確定済みSeriesを
                # 保持する。ただしchannel/start_timeが変わっている可能性があるため、
                # 現チャンネルの期間へ付け替え、移動元・移動先を再集計する。
                previous_period_id = recorded_program.series_broadcast_period_id
                current_period = await SeriesBroadcastPeriod.filter(
                    series_id=recorded_program.series_id,
                    channel_id=recorded_program.channel_id,
                ).using_db(connection).order_by('id').first()
                if current_period is None:
                    recording_date = recorded_program.start_time.date()
                    current_period = await SeriesBroadcastPeriod.create(
                        series_id=recorded_program.series_id,
                        channel_id=recorded_program.channel_id,
                        start_date=recording_date,
                        end_date=recording_date,
                        using_db=connection,
                    )
                if recorded_program.series_broadcast_period_id != current_period.id:
                    recorded_program.series_broadcast_period_id = current_period.id
                    await recorded_program.save(
                        update_fields=['series_broadcast_period_id', 'updated_at'],
                        using_db=connection,
                    )
                affected_period_ids = {
                    period_id
                    for period_id in (previous_period_id, current_period.id)
                    if period_id is not None
                }
                for affected_period_id in affected_period_ids:
                    await cls._reconcileBroadcastPeriod(affected_period_id, connection=connection)
            await cls._markResolution(
                resolution,
                status='NeedsReview',
                source=source,
                series_id=series_id,
                candidate_set_hash=candidate_set_hash,
                candidate_snapshot=candidate_snapshot,
                ai_model=ai_model,
                error_code=error_code,
                connection=connection,
            )

    @classmethod
    async def _finishAIRequest(
        cls,
        *,
        request: RecordedSeriesAIRequest,
        status: Literal['Succeeded', 'Failed', 'Rejected'],
        selected_choice_id: str | None = None,
        result: AIChoiceResult | None = None,
        error: RecordedSeriesAIError | None = None,
    ) -> None:
        """事前予約済み監査行を、プロンプト・応答・キーなしで終端状態へ更新する。"""

        request.status = status
        request.model = result.model if result is not None else request.model
        request.selected_choice_id = selected_choice_id
        request.prompt_tokens = result.prompt_tokens if result is not None else None
        request.completion_tokens = result.completion_tokens if result is not None else None
        request.http_status = result.http_status if result is not None else (error.http_status if error is not None else None)
        request.latency_ms = result.latency_ms if result is not None else (error.latency_ms if error is not None else None)
        request.error_code = error.code if error is not None else None
        await request.save(update_fields=[
            'status',
            'model',
            'selected_choice_id',
            'prompt_tokens',
            'completion_tokens',
            'http_status',
            'latency_ms',
            'error_code',
        ])

    @classmethod
    async def resolveProgram(
        cls,
        recorded_program_id: int,
        *,
        force: bool = False,
        allow_disabled: bool = False,
        snapshots: list[_ProgramSnapshot] | None = None,
        parses: dict[int, SeriesTitleParseResult] | None = None,
        cluster_evidence: dict[int, _ClusterEvidence] | None = None,
        snapshot_generation: int | None = None,
    ) -> RecordedSeriesResolveResult:
        """1録画をcache→local→EPG→MediaWiki→候補制約AIの順で判定する。

        Args:
            recorded_program_id: 判定対象RecordedProgram ID。
            force: 既存の同一fingerprint結果を再利用せず再判定するか。
            allow_disabled: 自動判定が無効でも、管理者の手動一括判定として実行するか。
            snapshots: バックフィルで共有する全録画スナップショット。
            parses: バックフィルで共有するローカル解析結果。
            cluster_evidence: バックフィルで共有するクラスタ情報。
            snapshot_generation: 共有snapshotを取得した時点の録画入力世代。

        Returns:
            判定状態・出典・AI呼び出し有無。
        """

        async with cls._resolve_lock:
            settings, runtime_api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()
            if settings.enabled is False and allow_disabled is False:
                return RecordedSeriesResolveResult(recorded_program_id, 'Skipped', 'Disabled', False)

            supplied_cluster_context = (
                snapshots is not None and parses is not None and cluster_evidence is not None
            )
            if snapshots is None or parses is None or cluster_evidence is None:
                while True:
                    decision_generation = cls._snapshot_generation
                    snapshots = await cls._loadProgramSnapshots(recorded_only=True)
                    if decision_generation == cls._snapshot_generation:
                        break
                parses = {
                    item.id: ParseSeriesTitle(
                        item.title,
                        item.description,
                        item.detail,
                        item.genres,
                    )
                    for item in snapshots
                }
                cluster_evidence = _buildClusterEvidence(snapshots, parses)
            else:
                decision_generation = snapshot_generation \
                    if snapshot_generation is not None else cls._snapshot_generation
            snapshots_by_id = {item.id: item for item in snapshots}
            snapshot = snapshots_by_id.get(recorded_program_id)
            if snapshot is None:
                return RecordedSeriesResolveResult(recorded_program_id, 'Skipped', 'MissingOrRecording', False)
            local_parse = parses[recorded_program_id]
            cluster = cluster_evidence[recorded_program_id]
            input_fingerprint = _buildInputFingerprint(snapshot)

            # バックフィル共有snapshotの取得後に1件でも録画入力が更新された場合や、対象自身が
            # 現DBと異なる場合は、ResolutionをPendingへ戻す前に拒否して最新入力を再キューする。
            if decision_generation != cls._snapshot_generation:
                raise _RecordedProgramSnapshotChanged
            current_snapshot = await cls._loadProgramSnapshot(recorded_program_id)
            if current_snapshot is None:
                return RecordedSeriesResolveResult(recorded_program_id, 'Skipped', 'MissingOrRecording', False)
            if _buildInputFingerprint(current_snapshot) != input_fingerprint:
                raise _RecordedProgramSnapshotChanged

            resolution = await cls._getOrCreateResolution(snapshot, cluster)
            # 録画単位の手動訂正はforce付きバックフィルよりも優先する。ただし録画入力が
            # 更新された場合は、保存済みの手動判断を新しいchannel/start_timeへ再適用して
            # 放送期間を整合させる必要があるため、同一fingerprintの場合だけ即時再利用する。
            manual_resolution_status: Literal['Resolved', 'NotSeries'] | None = None
            manual_resolution_series_id: int | None = None
            manual_resolution_requires_reapply = False
            if resolution.source == 'Manual' and resolution.status in {'Resolved', 'NotSeries'}:
                manual_resolution_status = cast(Literal['Resolved', 'NotSeries'], resolution.status)
                manual_resolution_series_id = resolution.series_id
            elif (
                resolution.source == 'Manual' and
                resolution.status == 'NeedsReview' and
                resolution.error_code == 'ManualChannelUnavailable' and
                resolution.series_id is not None
            ):
                # channel欠落中も手動で選んだSeries IDをResolutionへ保持する。番組名まで
                # 変化して元Ruleに一致しなくても、channel復帰後は個別判断を直接復元できる。
                manual_resolution_status = 'Resolved'
                manual_resolution_series_id = resolution.series_id
                manual_resolution_requires_reapply = True
            if (
                manual_resolution_status is not None and
                manual_resolution_requires_reapply is False and
                resolution.input_fingerprint == input_fingerprint and
                resolution.resolver_version == RECORDED_SERIES_RESOLVER_VERSION
            ):
                return RecordedSeriesResolveResult(
                    recorded_program_id,
                    manual_resolution_status,
                    'Manual',
                    False,
                )
            if (
                force is False and
                resolution.input_fingerprint == input_fingerprint and
                resolution.evidence_hash == cluster.evidence_hash and
                resolution.resolver_version == RECORDED_SERIES_RESOLVER_VERSION and
                resolution.status in {'Resolved', 'NotSeries'}
            ):
                if decision_generation != cls._snapshot_generation:
                    raise _RecordedProgramSnapshotChanged
                return RecordedSeriesResolveResult(recorded_program_id, 'Skipped', 'Cache', False)
            resolution.input_fingerprint = input_fingerprint
            resolution.evidence_hash = cluster.evidence_hash
            resolution.resolver_version = RECORDED_SERIES_RESOLVER_VERSION
            resolution.normalized_title = cluster.normalized_key
            resolution.status = 'Pending'
            resolution.error_code = None
            resolution.error_message = None
            await resolution.save()

            # Manual Resolutionの入力が変わった場合はタイトルkey検索へ落とさず、その録画へ
            # 保存された決定そのものを最新snapshotへ再適用する。これにより番組名が変わっても
            # 個別訂正を失わず、旧・新SeriesBroadcastPeriodも現在値から再計算できる。
            if manual_resolution_status == 'Resolved':
                if snapshot.channel_id is None:
                    await cls._applyNeedsReview(
                        snapshot=snapshot,
                        resolution=resolution,
                        expected_generation=decision_generation,
                        source='Manual',
                        error_code='ManualChannelUnavailable',
                        clear_series=True,
                        series_id=manual_resolution_series_id,
                    )
                    return RecordedSeriesResolveResult(recorded_program_id, 'NeedsReview', 'Manual', False)
                manual_series = None
                if manual_resolution_series_id is not None:
                    manual_series = await Series.get_or_none(id=manual_resolution_series_id)
                if manual_series is None:
                    await cls._applyNeedsReview(
                        snapshot=snapshot,
                        resolution=resolution,
                        expected_generation=decision_generation,
                        source='Manual',
                        error_code='ManualSeriesNoLongerExists',
                        clear_series=False,
                    )
                    return RecordedSeriesResolveResult(recorded_program_id, 'NeedsReview', 'Manual', False)
                await cls._applySeries(
                    snapshot=snapshot,
                    parse_result=local_parse,
                    cluster=cluster,
                    resolution=resolution,
                    expected_generation=decision_generation,
                    source='Manual',
                    title=manual_series.title,
                    description=manual_series.description,
                    confidence=1.0,
                    wikipedia_page_id=manual_series.wikipedia_page_id,
                    existing_series_id=manual_series.id,
                )
                return RecordedSeriesResolveResult(recorded_program_id, 'Resolved', 'Manual', False)
            if manual_resolution_status == 'NotSeries':
                await cls._applyNotSeries(
                    snapshot=snapshot,
                    cluster=cluster,
                    resolution=resolution,
                    expected_generation=decision_generation,
                    source='Manual',
                    confidence=1.0,
                )
                return RecordedSeriesResolveResult(recorded_program_id, 'NotSeries', 'Manual', False)

            # 同じ正規化番組名への手動訂正は、現在のクラスタ root よりも
            # 個別タイトルの明示 rule を優先して再利用する。cluster 構成が後から
            # 変化しても手動 NotSeries を evidence_hash 不一致で無効化しない。
            manual_rule = await RecordedSeriesRule.get_or_none(
                key_hash=_buildRuleKeyHash(local_parse.normalized_key),
                source='Manual',
            )
            if manual_rule is None and local_parse.normalized_key != cluster.normalized_key:
                manual_rule = await RecordedSeriesRule.get_or_none(
                    key_hash=_buildRuleKeyHash(cluster.normalized_key),
                    source='Manual',
                )
            if manual_rule is not None:
                if manual_rule.decision == 'Series':
                    if snapshot.channel_id is None:
                        await cls._applyNeedsReview(
                            snapshot=snapshot,
                            resolution=resolution,
                            expected_generation=decision_generation,
                            source='Manual',
                            error_code='ManualChannelUnavailable',
                            clear_series=True,
                            series_id=manual_rule.series_id,
                        )
                        return RecordedSeriesResolveResult(recorded_program_id, 'NeedsReview', 'Manual', False)
                    manual_series = None
                    if manual_rule.series_id is not None:
                        manual_series = await Series.get_or_none(id=manual_rule.series_id)
                    if manual_series is not None:
                        await cls._applySeries(
                            snapshot=snapshot,
                            parse_result=local_parse,
                            cluster=cluster,
                            resolution=resolution,
                            expected_generation=decision_generation,
                            source='Manual',
                            title=manual_series.title,
                            description=manual_series.description,
                            confidence=1.0,
                            wikipedia_page_id=manual_series.wikipedia_page_id,
                            existing_series_id=manual_series.id,
                        )
                        return RecordedSeriesResolveResult(recorded_program_id, 'Resolved', 'Manual', False)
                    # on_delete=SET_NULL後などManual先Seriesが消えている場合に、
                    # 推測で別シリーズへ付け替えず管理者の再確認を待つ。
                    await cls._applyNeedsReview(
                        snapshot=snapshot,
                        resolution=resolution,
                        expected_generation=decision_generation,
                        source='Manual',
                        error_code='ManualSeriesNoLongerExists',
                        clear_series=False,
                    )
                    return RecordedSeriesResolveResult(recorded_program_id, 'NeedsReview', 'Manual', False)
                if manual_rule.decision == 'NotSeries':
                    await cls._applyNotSeries(
                        snapshot=snapshot,
                        cluster=cluster,
                        resolution=resolution,
                        expected_generation=decision_generation,
                        source='Manual',
                        confidence=1.0,
                    )
                    return RecordedSeriesResolveResult(recorded_program_id, 'NotSeries', 'Manual', False)

            # 絶対的な単発除外は過去の自動positive ruleより優先する。映画枠名などが偶然
            # 既存シリーズの正規化キーと一致しても、管理者のManual指定がない限り関連付けを再利用しない。
            if local_parse.is_hard_standalone:
                await cls._applyNotSeries(
                    snapshot=snapshot,
                    cluster=cluster,
                    resolution=resolution,
                    expected_generation=decision_generation,
                    source='Local',
                    confidence=1.0,
                    persist_rule=False,
                )
                return RecordedSeriesResolveResult(recorded_program_id, 'NotSeries', 'Local', False)

            # Series APIは放送期間（チャンネル必須）経由で録画を公開するため、チャンネル不明の
            # 録画をseries_idだけの半端な確定状態にしない。単発のsoft判定だけは安全に確定できる。
            if snapshot.channel_id is None:
                if local_parse.is_soft_standalone:
                    await cls._applyNotSeries(
                        snapshot=snapshot,
                        cluster=cluster,
                        resolution=resolution,
                        expected_generation=decision_generation,
                        source='Local',
                        confidence=0.95,
                    )
                    return RecordedSeriesResolveResult(recorded_program_id, 'NotSeries', 'Local', False)
                await cls._applyNeedsReview(
                    snapshot=snapshot,
                    resolution=resolution,
                    expected_generation=decision_generation,
                    source='Local',
                    error_code='ChannelIsUnavailable',
                    clear_series=True,
                )
                return RecordedSeriesResolveResult(recorded_program_id, 'NeedsReview', 'Local', False)

            rule = await RecordedSeriesRule.get_or_none(key_hash=_buildRuleKeyHash(cluster.normalized_key))
            if force is False and rule is not None and rule.resolver_version == RECORDED_SERIES_RESOLVER_VERSION:
                if rule.decision == 'Series' and rule.series_id is not None:
                    series = await Series.get_or_none(id=rule.series_id)
                    if series is not None:
                        await cls._applySeries(
                            snapshot=snapshot,
                            parse_result=local_parse,
                            cluster=cluster,
                            resolution=resolution,
                            expected_generation=decision_generation,
                            source='Rule',
                            title=series.title,
                            description=series.description,
                            confidence=rule.confidence,
                            wikipedia_page_id=series.wikipedia_page_id,
                            existing_series_id=series.id,
                        )
                        return RecordedSeriesResolveResult(recorded_program_id, 'Resolved', 'Rule', False)
                elif rule.decision == 'NotSeries' and rule.evidence_hash == cluster.evidence_hash:
                    await cls._applyNotSeries(
                        snapshot=snapshot,
                        cluster=cluster,
                        resolution=resolution,
                        expected_generation=decision_generation,
                        source='Rule',
                        confidence=rule.confidence,
                    )
                    return RecordedSeriesResolveResult(recorded_program_id, 'NotSeries', 'Rule', False)

            # 明示話数は最も強いローカル証拠。同じrootで異なる内容が2件以上ある場合も
            # 外部アクセスなしでシリーズ化し、重複再放送だけの同名録画は除外する。
            if local_parse.has_explicit_episode or cluster.is_strong_repeat:
                effective_parse = local_parse
                if cluster.is_strong_repeat:
                    effective_parse = SeriesTitleParseResult(
                        series_title=cluster.display_title,
                        normalized_key=cluster.normalized_key,
                        episode_number=local_parse.episode_number,
                        subtitle=local_parse.subtitle,
                        season_number=local_parse.season_number,
                        episode_source=local_parse.episode_source,
                        has_explicit_episode=local_parse.has_explicit_episode,
                        is_hard_standalone=False,
                        is_soft_standalone=local_parse.is_soft_standalone,
                    )
                await cls._applySeries(
                    snapshot=snapshot,
                    parse_result=effective_parse,
                    cluster=cluster,
                    resolution=resolution,
                    expected_generation=decision_generation,
                    source='Local',
                    title=effective_parse.series_title,
                    description=snapshot.description,
                    confidence=1.0,
                )
                # 2件目が揃ってstrong clusterになった場合、先に要確認・単発となった既存録画も
                # 同じpositive ruleで自動回収する。バックフィルは自身の順序ですべて処理するため不要。
                if supplied_cluster_context is False and cluster.is_strong_repeat:
                    for member_id in cluster.member_ids:
                        if member_id != recorded_program_id:
                            await cls.enqueue(member_id)
                return RecordedSeriesResolveResult(recorded_program_id, 'Resolved', 'Local', False)

            epg_enrichment = await cls._matchEPG(snapshot, local_parse)
            if epg_enrichment is not None:
                await cls._applySeries(
                    snapshot=snapshot,
                    parse_result=epg_enrichment.parse_result,
                    cluster=cluster,
                    resolution=resolution,
                    expected_generation=decision_generation,
                    source='EPG',
                    title=epg_enrichment.parse_result.series_title,
                    description=snapshot.description,
                    confidence=1.0,
                )
                return RecordedSeriesResolveResult(recorded_program_id, 'Resolved', 'EPG', False)

            # 映画・劇場公演などのgenreは副ジャンルにも付くため、異なる内容が複数ある
            # strong clusterやEPG一致を確認した後にだけ単発根拠として使う。
            if local_parse.is_soft_standalone:
                await cls._applyNotSeries(
                    snapshot=snapshot,
                    cluster=cluster,
                    resolution=resolution,
                    expected_generation=decision_generation,
                    source='Local',
                    confidence=0.95,
                )
                return RecordedSeriesResolveResult(recorded_program_id, 'NotSeries', 'Local', False)

            existing_series = await cls._findExistingSeriesCandidates(cluster.display_title)
            if settings.ai_enabled is False:
                await cls._applyNeedsReview(
                    snapshot=snapshot,
                    resolution=resolution,
                    expected_generation=decision_generation,
                    source='Local',
                    error_code='AIIsDisabled',
                    clear_series=force is False,
                )
                return RecordedSeriesResolveResult(recorded_program_id, 'NeedsReview', 'Local', False)

            if settings.daily_ai_request_limit > 0:
                today_start = datetime.combine(datetime.now(tz=JST).date(), datetime_time.min, tzinfo=JST)
                requests_today = await RecordedSeriesAIRequest.filter(
                    purpose='Resolution',
                    created_at__gte=today_start,
                ).filter(error_code__not='InputChangedBeforeRequest').count()
                if requests_today >= settings.daily_ai_request_limit:
                    await cls._applyNeedsReview(
                        snapshot=snapshot,
                        resolution=resolution,
                        expected_generation=decision_generation,
                        source='Local',
                        error_code='DailyAIRequestLimitReached',
                        clear_series=force is False,
                    )
                    return RecordedSeriesResolveResult(recorded_program_id, 'NeedsReview', 'Local', False)

            try:
                wikipedia_candidates = await SearchWikipediaCandidates(cluster.display_title, limit=5)
            except (httpx.HTTPError, ValueError):
                await cls._applyNeedsReview(
                    snapshot=snapshot,
                    resolution=resolution,
                    expected_generation=decision_generation,
                    source='MediaWiki',
                    error_code='MediaWikiUnavailable',
                    clear_series=force is False,
                )
                return RecordedSeriesResolveResult(recorded_program_id, 'NeedsReview', 'MediaWiki', False)

            # Wikipedia・既存Series候補が見つからない場合も、KonomiTVが固定した
            # ローカルSeries・単発・未解決の3候補からだけAIに選ばせる。外部候補がないことを
            # 理由にここで要確認へ戻すと、同じ入力を何度再判定しても解消できなくなる。
            local_choice_id = f'local:{_buildRuleKeyHash(cluster.normalized_key)}'
            candidates: list[SeriesChoiceCandidate] = [
                SeriesChoiceCandidate(
                    choice_id=local_choice_id,
                    kind='Local',
                    title=cluster.display_title,
                    description='Create a new local series using this exact server-provided title.',
                ),
            ]
            candidates.extend(
                SeriesChoiceCandidate(
                    choice_id=f'series:{series.id}',
                    kind='ExistingSeries',
                    title=series.title,
                    description=series.description[:600],
                )
                for series in existing_series
            )
            candidates.extend(
                SeriesChoiceCandidate(
                    choice_id=f'wiki:{candidate["page_id"]}',
                    kind='Wikipedia',
                    title=candidate['title'],
                    description=candidate['extract'],
                )
                for candidate in wikipedia_candidates
            )
            candidates.extend([
                SeriesChoiceCandidate(
                    choice_id='standalone',
                    kind='Standalone',
                    title='Standalone',
                    description='The recording is a one-off program and must not be grouped as a series.',
                ),
                SeriesChoiceCandidate(
                    choice_id='unresolved',
                    kind='Unresolved',
                    title='Unresolved',
                    description='Evidence is insufficient; leave the recording for review.',
                ),
            ])
            candidate_set_hash = _sha256JSON(candidates)
            candidate_ids = [candidate['choice_id'] for candidate in candidates]
            program_prompt = RecordedSeriesProgramPrompt(
                title=snapshot.title,
                description=snapshot.description[:800],
                genres=[genre['major'] for genre in snapshot.genres],
                channel=snapshot.channel_id,
                start_date=snapshot.start_time.date().isoformat(),
            )

            # MediaWiki待機中に録画入力やクラスタ構成が変わった場合は、
            # 古い候補をAIへ送って日次枠を消費する前に中止する。
            if decision_generation != cls._snapshot_generation:
                raise _RecordedProgramSnapshotChanged
            latest_snapshot = await cls._loadProgramSnapshot(recorded_program_id)
            if (
                decision_generation != cls._snapshot_generation or
                latest_snapshot is None or
                _buildInputFingerprint(latest_snapshot) != input_fingerprint
            ):
                raise _RecordedProgramSnapshotChanged

            # 外部POSTより先に1リクエスト分を予約し、応答後〜監査保存前のクラッシュでも
            # 未監査リクエストとして日次上限を超えないようにする。
            ai_attempt_key = _buildAIAttemptKey(
                resolution_id=resolution.id,
                input_fingerprint=input_fingerprint,
                evidence_hash=cluster.evidence_hash,
                candidate_set_hash=candidate_set_hash,
                api_base_url=settings.api_base_url,
                model=settings.model,
                api_key=runtime_api_key,
            )
            attempt_started_at = time.monotonic()
            expired_attempt_keys = [
                key
                for key, started_at in cls._ai_attempt_keys.items()
                if attempt_started_at - started_at >= AI_ATTEMPT_CACHE_TTL_SECONDS
            ]
            for expired_attempt_key in expired_attempt_keys:
                cls._ai_attempt_keys.pop(expired_attempt_key, None)
            if force is False and ai_attempt_key in cls._ai_attempt_keys:
                await cls._applyNeedsReview(
                    snapshot=snapshot,
                    resolution=resolution,
                    expected_generation=decision_generation,
                    source='AI',
                    candidate_set_hash=candidate_set_hash,
                    candidate_snapshot=candidates,
                    ai_model=settings.model,
                    error_code='RecentAIAttempt',
                    clear_series=force is False,
                )
                return RecordedSeriesResolveResult(recorded_program_id, 'Skipped', 'AIAttemptCache', False)
            resolution.candidate_set_hash = candidate_set_hash
            resolution.candidate_snapshot = cast(list[object], candidates)
            resolution.ai_model = settings.model
            await resolution.save(update_fields=[
                'candidate_set_hash',
                'candidate_snapshot',
                'ai_model',
                'updated_at',
            ])
            ai_request = await RecordedSeriesAIRequest.create(
                resolution_id=resolution.id,
                purpose='Resolution',
                status='Pending',
                model=settings.model,
                input_fingerprint=input_fingerprint,
                candidate_set_hash=candidate_set_hash,
                candidate_ids=candidate_ids,
                selected_choice_id=None,
                prompt_tokens=None,
                completion_tokens=None,
                http_status=None,
                latency_ms=None,
                error_code=None,
            )
            # candidate snapshotと監査予約のDB await中に入力が変わる窓も閉じる。
            # この失敗監査は実際のAPIリクエストではないため、日次利用数から除外する。
            latest_snapshot = await cls._loadProgramSnapshot(recorded_program_id)
            if (
                decision_generation != cls._snapshot_generation or
                latest_snapshot is None or
                _buildInputFingerprint(latest_snapshot) != input_fingerprint
            ):
                await cls._finishAIRequest(
                    request=ai_request,
                    status='Failed',
                    error=RecordedSeriesAIError('InputChangedBeforeRequest'),
                )
                raise _RecordedProgramSnapshotChanged
            cls._ai_attempt_keys[ai_attempt_key] = attempt_started_at
            try:
                ai_result = await SelectRecordedSeriesCandidate(
                    api_base_url=settings.api_base_url,
                    api_key=runtime_api_key,
                    model=settings.model,
                    program=program_prompt,
                    candidates=candidates,
                )
                await cls._finishAIRequest(
                    request=ai_request,
                    status='Succeeded',
                    selected_choice_id=ai_result.choice_id,
                    result=ai_result,
                )
            except RecordedSeriesAIError as ex:
                rejected_codes = {
                    'ChoiceOutsideCandidateSet',
                    'InvalidOutputSchema',
                    'InvalidJSON',
                    'InvalidJSONType',
                    'LowConfidence',
                }
                await cls._finishAIRequest(
                    request=ai_request,
                    status='Rejected' if ex.code in rejected_codes else 'Failed',
                    error=ex,
                )
                await cls._applyNeedsReview(
                    snapshot=snapshot,
                    resolution=resolution,
                    expected_generation=decision_generation,
                    source='AI',
                    candidate_set_hash=candidate_set_hash,
                    candidate_snapshot=candidates,
                    ai_model=settings.model,
                    error_code=ex.code,
                    clear_series=force is False,
                )
                return RecordedSeriesResolveResult(recorded_program_id, 'NeedsReview', 'AI', True)

            if ai_result.choice_id == 'unresolved':
                await cls._applyNeedsReview(
                    snapshot=snapshot,
                    resolution=resolution,
                    expected_generation=decision_generation,
                    source='AI',
                    candidate_set_hash=candidate_set_hash,
                    candidate_snapshot=candidates,
                    ai_model=ai_result.model,
                    error_code='AISelectedUnresolved',
                    clear_series=force is False,
                )
                return RecordedSeriesResolveResult(recorded_program_id, 'NeedsReview', 'AI', True)
            if ai_result.choice_id == 'standalone':
                await cls._applyNotSeries(
                    snapshot=snapshot,
                    cluster=cluster,
                    resolution=resolution,
                    expected_generation=decision_generation,
                    source='AI',
                    confidence=ai_result.confidence,
                    candidate_set_hash=candidate_set_hash,
                    candidate_snapshot=candidates,
                    ai_model=ai_result.model,
                )
                return RecordedSeriesResolveResult(recorded_program_id, 'NotSeries', 'AI', True)
            if ai_result.choice_id == local_choice_id:
                await cls._applySeries(
                    snapshot=snapshot,
                    parse_result=local_parse,
                    cluster=cluster,
                    resolution=resolution,
                    expected_generation=decision_generation,
                    source='AI',
                    title=cluster.display_title,
                    description=snapshot.description,
                    confidence=ai_result.confidence,
                    candidate_set_hash=candidate_set_hash,
                    candidate_snapshot=candidates,
                    ai_model=ai_result.model,
                )
                return RecordedSeriesResolveResult(recorded_program_id, 'Resolved', 'AI', True)
            if ai_result.choice_id.startswith('series:'):
                selected_series_id = int(ai_result.choice_id.removeprefix('series:'))
                if selected_series_id not in {series.id for series in existing_series}:
                    # 直前の候補集合検証に加え、commit時にもDB候補集合を再確認する。
                    await cls._applyNeedsReview(
                        snapshot=snapshot,
                        resolution=resolution,
                        expected_generation=decision_generation,
                        source='AI',
                        error_code='SeriesOutsideCandidateSetAtCommit',
                        clear_series=force is False,
                    )
                    return RecordedSeriesResolveResult(recorded_program_id, 'NeedsReview', 'AI', True)
                selected_series = next(series for series in existing_series if series.id == selected_series_id)
                await cls._applySeries(
                    snapshot=snapshot,
                    parse_result=local_parse,
                    cluster=cluster,
                    resolution=resolution,
                    expected_generation=decision_generation,
                    source='AI',
                    title=selected_series.title,
                    description=selected_series.description,
                    confidence=ai_result.confidence,
                    existing_series_id=selected_series.id,
                    wikipedia_page_id=selected_series.wikipedia_page_id,
                    candidate_set_hash=candidate_set_hash,
                    candidate_snapshot=candidates,
                    ai_model=ai_result.model,
                )
                return RecordedSeriesResolveResult(recorded_program_id, 'Resolved', 'AI', True)
            if ai_result.choice_id.startswith('wiki:'):
                selected_page_id = int(ai_result.choice_id.removeprefix('wiki:'))
                wikipedia_by_id: dict[int, WikipediaCandidate] = {
                    candidate['page_id']: candidate
                    for candidate in wikipedia_candidates
                }
                selected_wikipedia = wikipedia_by_id.get(selected_page_id)
                if selected_wikipedia is None:
                    await cls._applyNeedsReview(
                        snapshot=snapshot,
                        resolution=resolution,
                        expected_generation=decision_generation,
                        source='AI',
                        error_code='WikipediaOutsideCandidateSetAtCommit',
                        clear_series=force is False,
                    )
                    return RecordedSeriesResolveResult(recorded_program_id, 'NeedsReview', 'AI', True)
                await cls._applySeries(
                    snapshot=snapshot,
                    parse_result=local_parse,
                    cluster=cluster,
                    resolution=resolution,
                    expected_generation=decision_generation,
                    source='AI',
                    title=selected_wikipedia['title'],
                    description=selected_wikipedia['extract'] or snapshot.description,
                    confidence=ai_result.confidence,
                    wikipedia_page_id=selected_wikipedia['page_id'],
                    candidate_set_hash=candidate_set_hash,
                    candidate_snapshot=candidates,
                    ai_model=ai_result.model,
                )
                return RecordedSeriesResolveResult(recorded_program_id, 'Resolved', 'AI', True)

            # SelectRecordedSeriesCandidate()が候補集合を検証済みなので通常到達しないが、
            # 将来候補種別が増えた場合も暗黙採用せずNeedsReviewへ倒す。
            await cls._applyNeedsReview(
                snapshot=snapshot,
                resolution=resolution,
                expected_generation=decision_generation,
                source='AI',
                error_code='UnsupportedChoiceKind',
                clear_series=force is False,
            )
            return RecordedSeriesResolveResult(recorded_program_id, 'NeedsReview', 'AI', True)

    @classmethod
    async def getNextProgramID(cls, recorded_program_id: int) -> int | None:
        """同一シリーズ内で現在の録画より後に放送された、次の再生可能な録画 ID を返す。

        Args:
            recorded_program_id: 現在再生している RecordedProgram ID。

        Returns:
            開始日時と ID の安定順で直後にある RecordedProgram ID。
            シリーズ未所属または続きがない場合は None。

        Raises:
            RecordedSeriesProgramNotFoundError: 対象が存在しないか、再生可能な録画ではない場合。
        """

        # 自動再生の起点自体が解析中・失敗・削除済みなら、古い画面から別録画へ遷移させない。
        current_program = await RecordedProgram.filter(
            id=recorded_program_id,
            recorded_video__status='Recorded',
        ).first()
        if current_program is None:
            raise RecordedSeriesProgramNotFoundError
        if current_program.series_id is None:
            return None

        # 同時刻録画でも結果が揺れないよう ID を第2ソートキーにする。
        # Recording / Analyzing / AnalysisFailed は視聴画面でまだ再生できないため候補から除外する。
        next_program = await RecordedProgram.filter(
            series_id=current_program.series_id,
            recorded_video__status='Recorded',
        ).filter(
            Q(start_time__gt=current_program.start_time) |
            Q(start_time=current_program.start_time, id__gt=current_program.id),
        ).order_by('start_time', 'id').first()
        return next_program.id if next_program is not None else None

    @classmethod
    async def getStatus(cls) -> dict[str, int | str | bool | None]:
        """Web設定画面向けにシリーズ判定件数とAI利用回数を集計する。"""

        total = await RecordedProgram.filter(recorded_video__status='Recorded').count()
        status_counts = {
            status: await RecordedSeriesResolution.filter(
                recorded_program__recorded_video__status='Recorded',
                status=status,
            ).count()
            for status in ('Pending', 'Resolved', 'NotSeries', 'NeedsReview', 'Failed')
        }
        today_start = datetime.combine(datetime.now(tz=JST).date(), datetime_time.min, tzinfo=JST)
        ai_requests_today = await RecordedSeriesAIRequest.filter(
            purpose='Resolution',
            created_at__gte=today_start,
        ).filter(error_code__not='InputChangedBeforeRequest').count()
        last_resolution = await RecordedSeriesResolution.all().order_by('-updated_at').first()
        resolved_count = status_counts['Resolved']
        not_series_count = status_counts['NotSeries']
        needs_review_count = status_counts['NeedsReview']
        failed_count = status_counts['Failed']
        pending_count = max(
            status_counts['Pending'],
            total - resolved_count - not_series_count - needs_review_count - failed_count,
        )
        return {
            'total': total,
            'pending': pending_count,
            'resolved': resolved_count,
            'not_series': not_series_count,
            'needs_review': needs_review_count,
            'failed': failed_count,
            'ai_requests_today': ai_requests_today,
            'last_run_at': last_resolution.updated_at.isoformat() if last_resolution is not None else None,
            'is_running': cls._backfill_task is not None and cls._backfill_task.done() is False,
        }
