from __future__ import annotations

import asyncio
from typing import ClassVar, Literal, cast

import anyio

from app import logging
from app.metadata.AnalysisTaskTracker import AnalysisTaskHandle, AnalysisTaskTracker
from app.metadata.CMAnalysisOrchestrator import CMAnalysisOrchestrator
from app.models.AnalysisTask import AnalysisTaskStatus
from app.models.CMAnalysis import RecordedVideoCMAnalysis
from app.models.RecordedVideo import RecordedVideo


CMManualAnalysisIntent = Literal['CMDetection', 'CMRegeneration']


class CMAnalysisTaskManager:
    """CM再判定の実行履歴と寿命を管理し、起動時の補完を手動実行より後に直列処理する。"""

    # 実行中Taskを録画ID単位で強参照し、同じ録画への連打を同一実行へ合流させる。
    _tasks: ClassVar[dict[int, asyncio.Task[None]]] = {}
    # enqueue() が応答前に作成したQueued履歴をTaskと同じ寿命で保持する。
    _history_handles: ClassVar[dict[int, AnalysisTaskHandle]] = {}
    # 履歴作成をまたぐ同時enqueueでも、同一録画のTaskを二重作成させない。
    _guard: ClassVar[asyncio.Lock] = asyncio.Lock()

    @classmethod
    async def enqueue(
        cls,
        recorded_video_id: int,
        intent: CMManualAnalysisIntent,
        *,
        trigger: Literal['Manual', 'StartupBackfill'] = 'Manual',
    ) -> tuple[int, bool]:
        """CM再判定を開始し、永続的な実行IDを即座に返す。

        Args:
            recorded_video_id: 再判定対象のRecordedVideo ID。
            intent: 既存chapterを保持するか、安全に再生成するかを表す手動実行意図。
            trigger: 手動実行または起動時補完の実行契機。

        Returns:
            実行履歴IDと、既存の実行へ合流したかどうか。
        """

        async with cls._guard:
            existing_task = cls._tasks.get(recorded_video_id)
            existing_handle = cls._history_handles.get(recorded_video_id)
            if (
                existing_task is not None
                and existing_task.done() is False
                and existing_handle is not None
            ):
                return existing_handle.execution.id, True

            # HTTP応答前にQueued履歴を永続化することで、応答直後から同じIDをpollできるようにする。
            history_handle = await AnalysisTaskTracker.start(
                'CMAnalysis',
                recorded_video_id=recorded_video_id,
                trigger=trigger,
                initial_status='Queued',
                inherit_parent=False,
            )
            task = asyncio.create_task(
                cls.__run(recorded_video_id, intent, history_handle),
                name=f'CMAnalysisTask-{recorded_video_id}',
            )
            cls._tasks[recorded_video_id] = task
            cls._history_handles[recorded_video_id] = history_handle
            return history_handle.execution.id, False

    @classmethod
    async def runStartupBackfill(cls) -> None:
        """起動時スキャン後の未完了CM解析を、手動実行を優先して一度ずつ補完する。

        Returns:
            None
        """

        # 最初に候補IDだけを固定し、今回FailedやPendingになった録画を同じ起動中に再試行しない。
        recorded_video_ids = cast(list[int], await RecordedVideoCMAnalysis.filter(
            status__in=['Pending', 'Interrupted'],
        ).order_by('recorded_video_id').values_list('recorded_video_id', flat=True))
        for recorded_video_id in recorded_video_ids:
            while True:
                # 前の録画の完了待ちに手動解析・削除が入るため、投入直前に状態と実在を再確認する。
                video = await RecordedVideo.filter(
                    id=recorded_video_id,
                    status='Recorded',
                    cm_analysis__status__in=['Pending', 'Interrupted'],
                ).only('id', 'file_path').first()
                if video is None or await anyio.Path(video.file_path).is_file() is False:
                    break

                # 候補全件を共有semaphoreの待ち行列へ積まない。手動実行の受付は従来どおり通し、
                # 登録済みの実行がすべて完了するまで次のstartup履歴も作らない。
                async with cls._guard:
                    active_tasks = [task for task in cls._tasks.values() if task.done() is False]
                if active_tasks:
                    # スキャン側の停止でHTTP接続から独立した手動実行までキャンセルしない。
                    await asyncio.gather(*(asyncio.shield(task) for task in active_tasks), return_exceptions=True)
                    continue

                await cls.enqueue(recorded_video_id, 'CMDetection', trigger='StartupBackfill')
                task = cls._tasks.get(recorded_video_id)
                if task is not None:
                    # 起動時補完は1件の終端まで待ち、実行中の1件より後に手動の待機枠を確保する。
                    # shutdown時の実行回収は、producer停止後のstop()へ任せる。
                    await asyncio.shield(task)
                break

    @classmethod
    async def stop(cls) -> None:
        """実行中のCM再判定を中断へ確定し、DB接続終了前に回収する。

        Returns:
            None
        """

        async with cls._guard:
            tasks = list(cls._tasks.items())
        for _, task in tasks:
            task.cancel()
        if len(tasks) > 0:
            await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)

        # create_task直後のcancelでは__run()のfinallyに入らないため、Queued履歴もここで回収する。
        async with cls._guard:
            for recorded_video_id, task in tasks:
                if cls._tasks.get(recorded_video_id) is not task:
                    continue
                history_handle = cls._history_handles.get(recorded_video_id)
                if history_handle is not None and history_handle.terminal is False:
                    await history_handle.finish('Interrupted', error_code='Cancelled')
                cls._tasks.pop(recorded_video_id, None)
                cls._history_handles.pop(recorded_video_id, None)

    @classmethod
    async def __run(
        cls,
        recorded_video_id: int,
        intent: CMManualAnalysisIntent,
        history_handle: AnalysisTaskHandle,
    ) -> None:
        """HTTP接続から独立したTask上で再判定し、全返却状態を履歴の終端へ写す。

        Args:
            recorded_video_id: 再判定対象のRecordedVideo ID。
            intent: Orchestratorへ渡す手動実行意図。
            history_handle: enqueue() が応答前に永続化したQueued履歴。

        Returns:
            None
        """

        try:
            # 実解析前のchapter確認や設定確認も、この実行履歴のRunning区間に含める。
            await history_handle.activate(history_handle.execution.trigger)
            await history_handle.setStage('Probing', 0.0)
            state = await CMAnalysisOrchestrator().run(
                recorded_video_id,
                intent,
                existing_handle=history_handle,
            )

            # 実解析へ進んだ場合はOrchestrator内で同じhandleが既に終端化されている。
            # 既存chapter・設定無効・除外などの早期returnだけをここで確定する。
            if history_handle.terminal is False:
                terminal_status, error_code = cls._resolveTerminalOutcome(state, intent)
                await history_handle.finish(
                    terminal_status,
                    error_code=error_code,
                    error_message=(
                        state.error_message
                        if state is not None and terminal_status == 'Failed'
                        else None
                    ),
                )
        except asyncio.CancelledError:
            # Orchestrator開始前や早期return処理中のキャンセルも、Queued/Runningのまま残さない。
            if history_handle.terminal is False:
                try:
                    await history_handle.finish('Interrupted', error_code='Cancelled')
                except Exception as ex:
                    logging.warning('[CMAnalysisTaskManager] Failed to mark a cancelled task:', exc_info=ex)
            raise
        except Exception as ex:
            # detached Taskの例外を必ず回収し、ポーリング側が停止できるFailed履歴へ変換する。
            logging.error(
                '[CMAnalysisTaskManager] Unexpected CM analysis failure. '
                f'[recorded_video_id: {recorded_video_id}]',
                exc_info=ex,
            )
            if history_handle.terminal is False:
                try:
                    await history_handle.finish(
                        'Failed',
                        error_code=type(ex).__name__,
                        error_message=str(ex),
                    )
                except Exception as history_ex:
                    logging.warning('[CMAnalysisTaskManager] Failed to finalize task history:', exc_info=history_ex)
        finally:
            # 早期returnではAnalysisTaskTracker.track()を通らないため、ここでも保存期限を適用する。
            if history_handle.terminal and history_handle.execution.parent_id is None:
                try:
                    await AnalysisTaskTracker.prune()
                except Exception as ex:
                    logging.warning('[CMAnalysisTaskManager] Failed to prune task history:', exc_info=ex)

            current_task = asyncio.current_task()
            async with cls._guard:
                # 完了直後に新しい実行が登録された場合、その参照を古いTaskのfinallyで消さない。
                if cls._tasks.get(recorded_video_id) is current_task:
                    cls._tasks.pop(recorded_video_id, None)
                    cls._history_handles.pop(recorded_video_id, None)

    @staticmethod
    def _resolveTerminalOutcome(
        state: RecordedVideoCMAnalysis | None,
        intent: CMManualAnalysisIntent,
    ) -> tuple[AnalysisTaskStatus, str | None]:
        """Orchestratorの公開状態を、利用者操作一回分の終端履歴へ変換する。

        Args:
            state: Orchestratorが返した最新のCM解析状態。
            intent: 今回の手動実行意図。

        Returns:
            解析履歴の終端状態と理由コード。
        """

        if state is None:
            return 'Failed', 'RecordedVideoUnavailable'
        if state.status == 'Completed':
            # CMDetectionのCompletedがここへ来るのは、既存chapterを保持して実解析前に返った場合だけ。
            # 実解析まで進んだ場合は同じhandleがOrchestrator内ですでに終端化される。
            if intent == 'CMDetection':
                if state.chapter_source == 'Generated':
                    return 'Skipped', 'GeneratedChapterKept'
                if state.chapter_path_kind == 'Canonical':
                    return 'Skipped', 'ExternalCanonicalChapterProtected'
                return 'Skipped', 'ExistingChapterKept'
            if state.chapter_source == 'Generated':
                return 'Succeeded', None
            if intent == 'CMRegeneration' and state.chapter_path_kind == 'Canonical':
                return 'Skipped', 'ExternalCanonicalChapterProtected'
            return 'Skipped', 'ExistingChapterKept'
        if state.status in ('Pending', 'Excluded'):
            return 'Skipped', state.error_code or state.status
        if state.status in ('Failed', 'Unsupported'):
            return 'Failed', state.error_code or state.status
        if state.status == 'Interrupted':
            return 'Interrupted', state.error_code or state.status
        return 'Failed', 'NonTerminalCMAnalysisState'
