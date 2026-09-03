import asyncio
import hashlib
from typing import Literal

from typing_extensions import TypedDict

from app import logging
from app.metadata.ai.recorded_series_ai import (
    get_audit_model,
    get_episode_lookup_provider_fingerprint,
    resolve_series_metadata,
)
from app.metadata.RecordedEpisodeAutomation import RecordedEpisodeAutomation
from app.metadata.RecordedSeriesCandidates import (
    RecordedSeriesAIError,
    RecordedSeriesProgramPrompt,
)
from app.metadata.RecordedSeriesGeneration import (
    BuildSeriesMetadataPrompt,
    SeriesMetadataClusterHint,
    SeriesMetadataClusterProgramHint,
    SeriesMetadataExistingSeriesHint,
    SeriesMetadataHints,
    SeriesMetadataLocalParseHint,
)
from app.metadata.RecordedSeriesSettings import (
    RecordedSeriesSettings,
    RecordedSeriesSettingsStore,
)
from app.models.RecordedProgram import RecordedProgram
from app.models.Series import Series
from app.models.SeriesAIFallback import SeriesAIFallback, SeriesAIFallbackStatus


_MAX_GROUP_PROGRAMS = 8
_MAX_TEXT_LENGTH = 600


SeriesAIFallbackWorkerState = Literal['Running', 'Idle', 'Disabled', 'Stopped']


class SeriesAIFallbackWorkerStatus(TypedDict):
    """バックグラウンド処理 UI へ公開する現在の batch 状態。"""

    state: SeriesAIFallbackWorkerState
    stopped_reason: str | None
    total_groups: int
    processed_groups: int
    resolved_count: int
    not_series_count: int
    insufficient_evidence_count: int
    failed_count: int
    pending_count: int
    cancelled_count: int
    current_title: str | None


def _truncateText(value: str, maximum_length: int = _MAX_TEXT_LENGTH) -> str:
    """AI 入力を先頭優先の固定長へ収める。

    Args:
        value: EPG 由来の入力文字列。
        maximum_length: 保持する最大文字数。

    Returns:
        空白と最大長を正規化した文字列。
    """

    normalized = ' '.join(value.split()).strip()
    return normalized[:maximum_length]


def _genreLabels(recorded_program: RecordedProgram) -> list[str]:
    """EPG ジャンルを bounded な表示文字列へ変換する。

    Args:
        recorded_program: ジャンルを読み取る録画番組。

    Returns:
        大分類と中分類を連結した表示文字列。
    """

    labels: list[str] = []
    for genre in recorded_program.genres[:8]:
        parts = [
            value.strip()
            for value in (genre['major'], genre['middle'])
            if value.strip() != ''
        ]
        if len(parts) > 0:
            labels.append(' / '.join(parts)[:160])
    return labels


class SeriesAIFallbackTask:
    """Indexer 未所属の EPG タイトル群を束ね、Web 検索を直列実行する。"""

    _worker_task: asyncio.Task[None] | None = None
    _wake_event: asyncio.Event | None = None
    _start_lock = asyncio.Lock()
    _batch_lock = asyncio.Lock()
    _scan_requested = False
    # UI には永続履歴ではなく、直列 worker が所有する現在の batch snapshot を公開する。
    _worker_state: SeriesAIFallbackWorkerState = 'Stopped'
    _worker_stopped_reason: str | None = 'WorkerNotStarted'
    _total_groups = 0
    _processed_groups = 0
    _resolved_count = 0
    _not_series_count = 0
    _insufficient_evidence_count = 0
    _failed_count = 0
    _pending_count = 0
    _cancelled_count = 0
    _current_title: str | None = None
    # rebuild と新規録画の Indexer 経路が、録画ごとに永続表を再照会せず参照する確定 cache。
    _resolved_assignments: dict[str, tuple[str, str, int | None]] = {}
    _cache_loaded = False

    @classmethod
    async def restoreResolvedAssignments(cls) -> None:
        """公開 Web 根拠を持つ確定所属だけをメモリ cache へ復元する。

        Returns:
            None
        """

        fallbacks = await SeriesAIFallback.filter(
            status='Resolved',
            web_search_performed=True,
        ).all()
        restored: dict[str, tuple[str, str, int | None]] = {}
        for fallback in fallbacks:
            if (
                fallback.series_title is None
                or fallback.normalized_title is None
                or len(fallback.citations) == 0
            ):
                continue
            restored[fallback.grouping_key] = (
                fallback.series_title,
                fallback.normalized_title,
                fallback.series_id,
            )
        # 構築途中を Indexer へ見せず一度に公開する。照会中に worker が確定した値も
        # 消さないよう、永続表の snapshot と現在の確定 cache を merge する。
        cls._resolved_assignments = {**cls._resolved_assignments, **restored}
        cls._cache_loaded = True

    @classmethod
    async def start(cls) -> None:
        """中断監査を閉じ、未所属タイトル群のワーカーを開始する。

        Returns:
            None
        """

        async with cls._start_lock:
            if cls._cache_loaded is False:
                await cls.restoreResolvedAssignments()
            if cls._worker_task is None or cls._worker_task.done():
                cls._wake_event = asyncio.Event()
                cls._worker_state = 'Stopped'
                cls._worker_stopped_reason = 'WorkerStarting'
                cls._worker_task = asyncio.create_task(cls._runWorker())
            # 外部 POST 済みかもしれない Pending は起動だけでは自動再送せず、
            # 明示的な設定保存または入力・backend 世代変更まで終端へ閉じる。
            await SeriesAIFallback.filter(status='Pending').update(
                status='Cancelled',
                error_code='AIRequestInterrupted',
            )
            await cls.schedule()

    @classmethod
    async def stop(cls) -> None:
        """実行中の Web 検索をキャンセルし、ワーカー所有状態を解放する。

        Returns:
            None
        """

        task = cls._worker_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        cls._worker_task = None
        cls._wake_event = None
        cls._scan_requested = False
        cls._worker_state = 'Stopped'
        cls._worker_stopped_reason = 'WorkerStopped'
        cls._pending_count = 0
        cls._current_title = None
        cls._resolved_assignments = {}
        cls._cache_loaded = False

    @classmethod
    async def schedule(cls, *, retry_cancelled: bool = False) -> None:
        """録画スキャンを待たせず、未所属集合の再評価を1回だけ予約する。

        Args:
            retry_cancelled: 明示的な設定変更に伴い、中断済み bundle も再試行するか。

        Returns:
            None
        """

        # 起動時の自動予約では外部 POST 済みか不明な中断を再送しない。
        # 設定保存という明示的な操作だけが、同じ fingerprint の再試行を許可する。
        if retry_cancelled:
            await SeriesAIFallback.filter(status='Cancelled').update(status='Pending')
        cls._scan_requested = True
        if cls._wake_event is not None:
            cls._wake_event.set()

    @classmethod
    def getStatus(cls) -> SeriesAIFallbackWorkerStatus:
        """現在の worker 状態と batch 進捗を返す。

        Returns:
            バックグラウンド処理 UI 用の現在状態。
        """

        state = cls._worker_state
        stopped_reason = cls._worker_stopped_reason
        # task が予期せず終了した場合も、最後の Running / Idle 表示を残さず停止として公開する。
        if cls._worker_task is None or cls._worker_task.done():
            state = 'Stopped'
            stopped_reason = stopped_reason or 'WorkerNotRunning'
        return SeriesAIFallbackWorkerStatus(
            state=state,
            stopped_reason=stopped_reason if state == 'Stopped' else None,
            total_groups=cls._total_groups,
            processed_groups=cls._processed_groups,
            resolved_count=cls._resolved_count,
            not_series_count=cls._not_series_count,
            insufficient_evidence_count=cls._insufficient_evidence_count,
            failed_count=cls._failed_count,
            pending_count=cls._pending_count,
            cancelled_count=cls._cancelled_count,
            current_title=cls._current_title,
        )

    @classmethod
    async def getCachedAssignment(
        cls,
        recorded_program: RecordedProgram,
    ) -> tuple[str, str, int | None] | None:
        """rebuild が再利用できる AI 確定タイトルを返す。

        Args:
            recorded_program: Indexer が所属を確定できなかった録画。

        Returns:
            表示タイトル、完全一致キー、既存 Series ID。確定 cache がなければ None。
        """

        from app.metadata.SeriesIndexer import BuildSeriesAIFallbackGroupingKey

        grouping_key = BuildSeriesAIFallbackGroupingKey(recorded_program.title)
        return cls._resolved_assignments.get(grouping_key)

    @classmethod
    async def _runWorker(cls) -> None:
        """予約を合流し、未所属タイトル群を1 group ずつ処理する。

        Returns:
            None
        """

        assert cls._wake_event is not None
        while True:
            await cls._wake_event.wait()
            cls._wake_event.clear()
            cls._scan_requested = False
            try:
                await cls._runBatch()
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                cls._worker_state = 'Stopped'
                cls._worker_stopped_reason = type(ex).__name__
                cls._pending_count = 0
                cls._current_title = None
                logging.error('[SeriesAIFallbackTask] Fallback batch failed.', exc_info=ex)
            if cls._scan_requested:
                cls._wake_event.set()

    @classmethod
    async def _loadGroups(cls) -> dict[str, list[RecordedProgram]]:
        """未所属の再生可能録画を完全一致 EPG タイトル系へ束ねる。

        Returns:
            grouping key ごとの Indexer 未所属録画。
        """

        from app.metadata.SeriesIndexer import (
            GENERIC_SERIES_TITLES,
            BuildSeriesAIFallbackGroupingKey,
            ParseSeriesTitle,
        )

        programs = await RecordedProgram.filter(
            series_id=None,
            recorded_video__status='Recorded',
        ).prefetch_related('channel').order_by('id')
        groups: dict[str, list[RecordedProgram]] = {}
        for recorded_program in programs:
            # 起動時全件走査や rebuild との競合でも、Indexer が確定できる録画は
            # AI bundle に含めず、従来の決定論的な所属を常に優先する。
            if ParseSeriesTitle(
                recorded_program.title,
                recorded_program.genres,
                recorded_program.description,
            ) is not None:
                continue
            grouping_key = BuildSeriesAIFallbackGroupingKey(recorded_program.title)
            if grouping_key == '' or grouping_key in GENERIC_SERIES_TITLES:
                continue
            groups.setdefault(grouping_key, []).append(recorded_program)
        return groups

    @classmethod
    async def _buildPrompt(
        cls,
        grouping_key: str,
        programs: list[RecordedProgram],
    ) -> tuple[RecordedSeriesProgramPrompt, SeriesMetadataHints, str]:
        """同一タイトル群から bounded prompt と入力 fingerprint を構築する。

        Args:
            grouping_key: 装飾除去済み EPG タイトルの完全一致キー。
            programs: 同じ grouping key に属する録画番組。

        Returns:
            代表番組、同一群 hints、入力 fingerprint。
        """

        from app.metadata.SeriesIndexer import (
            AreAIFallbackSeriesTitlesClose,
            NormalizeSeriesTitle,
        )

        representative = programs[0]
        channel = representative.channel
        program = RecordedSeriesProgramPrompt(
            title=_truncateText(representative.title, 300),
            description=_truncateText(representative.description),
            detail_items=[],
            genres=_genreLabels(representative),
            channel_id=representative.channel_id,
            channel_name=channel.name if channel is not None else None,
            broadcast_datetime=representative.start_time.isoformat(),
            duration_seconds=representative.duration,
        )
        samples = [
            SeriesMetadataClusterProgramHint(
                title=_truncateText(item.title, 300),
                broadcast_datetime=item.start_time.isoformat(),
                channel_name=item.channel.name if item.channel is not None else None,
                genres=_genreLabels(item),
                description=_truncateText(item.description),
                duration_seconds=item.duration,
            )
            for item in programs[:_MAX_GROUP_PROGRAMS]
        ]
        # 完全一致または接尾辞拡張の候補だけを ID 付き hints として公開し、
        # AI が同じ作品と判断した場合に既存 Series を明示的に選べるようにする。
        existing_candidates: list[tuple[int, Series, str]] = []
        for series in await Series.all().order_by('id'):
            normalized_title = series.normalized_title or NormalizeSeriesTitle(series.title)
            if AreAIFallbackSeriesTitlesClose(grouping_key, normalized_title):
                existing_candidates.append((
                    abs(len(grouping_key) - len(normalized_title)),
                    series,
                    normalized_title,
                ))
        existing_candidates.sort(key=lambda item: (item[0], item[1].id))
        hints = SeriesMetadataHints(
            local_parse=SeriesMetadataLocalParseHint(
                series_title=_truncateText(representative.title, 300),
                season_number=None,
                episode_number=None,
                subtitle=None,
            ),
            cluster=SeriesMetadataClusterHint(
                display_title=_truncateText(representative.title, 300),
                normalized_key=grouping_key,
                member_count=len(programs),
                representative_programs=samples,
            ),
            existing_series=[
                SeriesMetadataExistingSeriesHint(
                    id=series.id,
                    title=series.title,
                    description=_truncateText(series.description),
                    wikipedia_page_id=series.wikipedia_page_id,
                    similarity=round(
                        min(len(grouping_key), len(normalized_title)) /
                        max(len(grouping_key), len(normalized_title)),
                        4,
                    ),
                    match_reason=(
                        'NormalizedExact'
                        if grouping_key == normalized_title
                        else 'TitleSuffixExtension'
                    ),
                )
                for _length_difference, series, normalized_title in existing_candidates[:5]
            ],
            wikipedia=[],
        )
        fingerprint_source = BuildSeriesMetadataPrompt(
            program,
            hints,
            require_web_search=True,
        )
        return (
            program,
            hints,
            hashlib.sha256(fingerprint_source.encode('utf-8')).hexdigest(),
        )

    @classmethod
    async def _runBatch(cls) -> None:
        """同一 EPG タイトル系につき最大1回だけ Web 検索する。

        Returns:
            None
        """

        async with cls._batch_lock:
            settings, api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()
            # 接続試験の実行履歴ではなく現在の有効設定を開始条件にする。
            # 接続能力や認証の失敗は実リクエストの監査行へ記録し、補完を黙って省略しない。
            if settings.enabled is False or settings.ai_enabled is False:
                cls._worker_state = 'Disabled'
                cls._worker_stopped_reason = None
                cls._total_groups = 0
                cls._processed_groups = 0
                cls._resolved_count = 0
                cls._not_series_count = 0
                cls._insufficient_evidence_count = 0
                cls._failed_count = 0
                cls._pending_count = 0
                cls._cancelled_count = 0
                cls._current_title = None
                logging.warning('[SeriesAIFallbackTask] Fallback batch skipped because series AI is disabled.')
                return
            provider_fingerprint = get_episode_lookup_provider_fingerprint(settings, api_key)
            groups = await cls._loadGroups()
            cls._worker_state = 'Idle' if len(groups) == 0 else 'Running'
            cls._worker_stopped_reason = None
            cls._total_groups = len(groups)
            cls._processed_groups = 0
            cls._resolved_count = 0
            cls._not_series_count = 0
            cls._insufficient_evidence_count = 0
            cls._failed_count = 0
            cls._pending_count = 0
            cls._cancelled_count = 0
            cls._current_title = None
            for grouping_key, programs in groups.items():
                # 設定 OFF や backend 世代変更後も、古い snapshot で新規リクエストを続けない。
                latest_settings, latest_api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()
                if latest_settings.enabled is False or latest_settings.ai_enabled is False:
                    cls._worker_state = 'Disabled'
                    cls._worker_stopped_reason = None
                    cls._pending_count = 0
                    cls._current_title = None
                    logging.warning('[SeriesAIFallbackTask] Fallback batch stopped because series AI was disabled.')
                    return
                if (
                    get_episode_lookup_provider_fingerprint(latest_settings, latest_api_key)
                    != provider_fingerprint
                ):
                    cls._pending_count = 0
                    cls._current_title = None
                    await cls.schedule()
                    return
                # Web 検索中は現在の代表 EPG タイトルと Pending 1 件を公開する。
                cls._current_title = programs[0].title.strip() or None
                cls._pending_count = 1
                result_status = await cls._resolveGroup(
                    grouping_key,
                    programs,
                    settings=latest_settings,
                    api_key=latest_api_key,
                    provider_fingerprint=provider_fingerprint,
                )
                cls._pending_count = 0
                cls._processed_groups += 1
                if result_status == 'Resolved':
                    cls._resolved_count += 1
                elif result_status == 'NotSeries':
                    cls._not_series_count += 1
                elif result_status == 'InsufficientEvidence':
                    cls._insufficient_evidence_count += 1
                elif result_status == 'Failed':
                    cls._failed_count += 1
                elif result_status == 'Cancelled':
                    cls._cancelled_count += 1
            # 最終 group の外部待機中に設定が無効化された場合も、完了後に Idle へ上書きしない。
            latest_settings = RecordedSeriesSettingsStore.getSettings()
            if latest_settings.enabled is False or latest_settings.ai_enabled is False:
                cls._worker_state = 'Disabled'
                cls._worker_stopped_reason = None
            elif cls._cancelled_count > 0:
                # 起動時に中断監査へ閉じた group は、明示的な設定保存まで自動再送できないことを示す。
                cls._worker_state = 'Stopped'
                cls._worker_stopped_reason = 'InterruptedGroupsRequireRetry'
            else:
                cls._worker_state = 'Idle'
                cls._worker_stopped_reason = None
            cls._current_title = None

    @classmethod
    async def _resolveGroup(
        cls,
        grouping_key: str,
        programs: list[RecordedProgram],
        *,
        settings: RecordedSeriesSettings,
        api_key: str | None,
        provider_fingerprint: str,
    ) -> SeriesAIFallbackStatus:
        """1タイトル群を cache または1回の Web 検索で確定する。

        Args:
            grouping_key: 装飾除去済み EPG タイトルの完全一致キー。
            programs: 同じ grouping key に属する Indexer 未所属録画。
            settings: 判定開始時の録画シリーズ設定 snapshot。
            api_key: 同じ時点の互換 API key snapshot。
            provider_fingerprint: backend 設定・認証世代の fingerprint。

        Returns:
            処理後または再利用した fallback status。
        """

        from app.metadata.SeriesIndexer import (
            GENERIC_SERIES_TITLES,
            NormalizeSeriesTitle,
            SeriesIndexer,
        )

        program, hints, input_fingerprint = await cls._buildPrompt(grouping_key, programs)
        fallback = await SeriesAIFallback.get_or_none(grouping_key=grouping_key)
        if (
            fallback is not None
            and fallback.status == 'Resolved'
            and fallback.web_search_performed
            and len(fallback.citations) > 0
        ):
            await cls._applyResolvedFallback(fallback, programs)
            return 'Resolved'
        if (
            fallback is not None
            and fallback.input_fingerprint == input_fingerprint
            and fallback.provider_fingerprint == provider_fingerprint
            and fallback.status in {
                'NotSeries',
                'InsufficientEvidence',
                'Failed',
                'Cancelled',
            }
        ):
            return fallback.status

        fallback, _created = await SeriesAIFallback.update_or_create(
            grouping_key=grouping_key,
            defaults={
                'input_fingerprint': input_fingerprint,
                'provider_fingerprint': provider_fingerprint,
                'status': 'Pending',
                'series_id': None,
                'series_title': None,
                'normalized_title': None,
                'web_search_performed': False,
                'citations': [],
                'rationale_short': None,
                'ai_model': get_audit_model(settings),
                'prompt_tokens': None,
                'completion_tokens': None,
                'http_status': None,
                'latency_ms': None,
                'attempt_summaries': [],
                'error_code': None,
            },
        )
        cls._resolved_assignments.pop(grouping_key, None)
        try:
            result = await resolve_series_metadata(
                program,
                hints,
                settings=settings,
                api_key=api_key,
                require_web_search=True,
            )
        except asyncio.CancelledError:
            await SeriesAIFallback.filter(grouping_key=grouping_key, status='Pending').update(
                status='Cancelled',
                error_code='Cancelled',
            )
            raise
        except RecordedSeriesAIError as ex:
            await SeriesAIFallback.filter(grouping_key=grouping_key).update(
                status='Failed',
                error_code=ex.code,
                http_status=ex.http_status,
                latency_ms=ex.latency_ms,
                attempt_summaries=list(ex.recovery_attempt_summaries),
            )
            return 'Failed'
        except Exception as ex:
            logging.error(
                f'[SeriesAIFallbackTask] Series lookup failed unexpectedly. grouping_key: {grouping_key}',
                exc_info=ex,
            )
            await SeriesAIFallback.filter(grouping_key=grouping_key).update(
                status='Failed',
                error_code='SeriesAIFallbackFailed',
            )
            return 'Failed'

        citation_records = [
            {'url': citation.url, 'title': citation.title}
            for citation in result.citations
        ]
        # 外部待機中に無効化・backend 世代変更された結果は所属へ反映しない。
        # 利用量と検索 telemetry は監査へ残し、有効な新世代があれば既存の予約へ合流させる。
        latest_settings, latest_api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()
        latest_provider_fingerprint = (
            get_episode_lookup_provider_fingerprint(latest_settings, latest_api_key)
            if latest_settings.enabled and latest_settings.ai_enabled
            else None
        )
        if (
            latest_settings.enabled is False
            or latest_settings.ai_enabled is False
            or latest_provider_fingerprint != provider_fingerprint
        ):
            await SeriesAIFallback.filter(grouping_key=grouping_key).update(
                status='Cancelled',
                web_search_performed=result.web_search_performed,
                citations=citation_records,
                rationale_short=result.rationale_short,
                ai_model=result.model,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                http_status=result.http_status,
                latency_ms=result.latency_ms,
                attempt_summaries=list(result.recovery_attempt_summaries),
                error_code='AISettingsChanged',
            )
            if (
                latest_settings.enabled
                and latest_settings.ai_enabled
            ):
                await cls.schedule(retry_cancelled=True)
            return 'Cancelled'

        normalized_title = (
            NormalizeSeriesTitle(result.series_title)
            if result.series_title is not None
            else None
        )
        accepted = (
            result.decision == 'Series'
            and result.series_title is not None
            and normalized_title is not None
            and normalized_title != ''
            and normalized_title not in GENERIC_SERIES_TITLES
            and result.season_number is None
            and result.episode_number is None
            and result.episode_not_numbered is False
            and result.episode_no_published_number is False
            and result.subtitle is None
            and result.web_search_performed
            and len(result.citations) > 0
        )
        if accepted is False:
            terminal_status: SeriesAIFallbackStatus = (
                'NotSeries'
                if (
                    result.decision == 'NotSeries'
                    and result.web_search_performed
                    and len(result.citations) > 0
                )
                else 'InsufficientEvidence'
            )
            await SeriesAIFallback.filter(grouping_key=grouping_key).update(
                status=terminal_status,
                web_search_performed=result.web_search_performed,
                citations=citation_records,
                rationale_short=result.rationale_short,
                ai_model=result.model,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                http_status=result.http_status,
                latency_ms=result.latency_ms,
                attempt_summaries=list(result.recovery_attempt_summaries),
                error_code=None,
            )
            return terminal_status

        assert result.series_title is not None
        assert normalized_title is not None
        # AI が既存 Series を選んだ場合は、録画への適用成否とは独立に実在とタイトル関係を検証する。
        # 検証済みの場合だけ既存側の ID・正本タイトルを、回復可能な最初の Resolved 行へ保存する。
        resolved_series = await SeriesIndexer.resolveAIFallbackSeries(
            existing_series_id=result.existing_series_id,
            normalized_title=normalized_title,
        )
        resolved_series_id: int | None = None
        resolved_series_title = result.series_title
        resolved_normalized_title = normalized_title
        if resolved_series is not None:
            series, resolved_normalized_title = resolved_series
            resolved_series_id = series.id
            resolved_series_title = series.title

        # 確定 cache を必須状態として先に保存する。録画ごとの関連付けが途中失敗しても、
        # rebuild が検証済みの同じ Series ID と正本タイトルから残りの録画へ収束させられる。
        await SeriesAIFallback.filter(grouping_key=grouping_key).update(
            status='Resolved',
            series_id=resolved_series_id,
            series_title=resolved_series_title,
            normalized_title=resolved_normalized_title,
            web_search_performed=True,
            citations=citation_records,
            rationale_short=result.rationale_short,
            ai_model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            http_status=result.http_status,
            latency_ms=result.latency_ms,
            attempt_summaries=list(result.recovery_attempt_summaries),
            error_code=None,
        )
        cls._resolved_assignments[grouping_key] = (
            resolved_series_title,
            resolved_normalized_title,
            resolved_series_id,
        )
        fallback = await SeriesAIFallback.get(grouping_key=grouping_key)
        await cls._applyResolvedFallback(
            fallback,
            programs,
            existing_series_id=resolved_series_id,
        )
        return 'Resolved'

    @classmethod
    async def _applyResolvedFallback(
        cls,
        fallback: SeriesAIFallback,
        programs: list[RecordedProgram],
        *,
        existing_series_id: int | None = None,
    ) -> None:
        """AI 確定 cache を、まだ未所属の束へ適用して話数キューへ渡す。

        Args:
            fallback: 公開 Web 根拠を保存済みの所属 cache。
            programs: 同じ grouping key に属する録画番組。
            existing_series_id: 今回の AI 応答が選んだ hints 内の既存 Series ID。

        Returns:
            None
        """

        if fallback.series_title is None or fallback.normalized_title is None:
            return
        from app.metadata.SeriesIndexer import NormalizeSeriesTitle, SeriesIndexer
        from app.utils.KonomiTVBS4KBangumiClient import KonomiTVBS4KBangumiClient

        target_series_id = existing_series_id if existing_series_id is not None else fallback.series_id
        linked_series_id = fallback.series_id
        linked_series_title = fallback.series_title
        linked_normalized_title = fallback.normalized_title
        linked_any = False
        for program in programs:
            latest = await RecordedProgram.get_or_none(id=program.id, series_id=None)
            if latest is None:
                continue
            linked = await SeriesIndexer.applyAIFallback(
                latest,
                display_title=fallback.series_title,
                normalized_title=fallback.normalized_title,
                existing_series_id=target_series_id,
            )
            if linked is False:
                continue
            linked_any = True
            linked_series_id = latest.series_id
            if latest.series_title is not None:
                linked_series_title = latest.series_title
                linked_normalized_title = NormalizeSeriesTitle(latest.series_title)
            await RecordedEpisodeAutomation.enqueue(latest.id)
        if linked_any:
            if (
                linked_series_id != fallback.series_id
                or linked_series_title != fallback.series_title
                or linked_normalized_title != fallback.normalized_title
            ):
                await SeriesAIFallback.filter(grouping_key=fallback.grouping_key).update(
                    series_id=linked_series_id,
                    series_title=linked_series_title,
                    normalized_title=linked_normalized_title,
                )
            cls._resolved_assignments[fallback.grouping_key] = (
                linked_series_title,
                linked_normalized_title,
                linked_series_id,
            )
            # Series 作成後の既存 Bangumi 照合は従来の合流可能なバックグラウンド経路へ渡す。
            KonomiTVBS4KBangumiClient.scheduleUserCollectionSync()
