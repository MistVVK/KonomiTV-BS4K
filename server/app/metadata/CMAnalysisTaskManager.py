from __future__ import annotations

import asyncio
from typing import ClassVar, Literal

from app import logging
from app.metadata.AnalysisTaskTracker import AnalysisTaskHandle, AnalysisTaskTracker
from app.metadata.CMAnalysisOrchestrator import CMAnalysisOrchestrator
from app.models.AnalysisTask import AnalysisTaskStatus
from app.models.CMAnalysis import RecordedVideoCMAnalysis


CMManualAnalysisIntent = Literal['CMDetection', 'CMRegeneration']


class CMAnalysisTaskManager:
    """個別の手動CM再判定をHTTP接続から分離し、実行履歴と寿命を一元管理する。"""

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
    ) -> tuple[int, bool]:
        """手動CM再判定を開始し、永続的な実行IDを即座に返す。

        Args:
            recorded_video_id: 再判定対象のRecordedVideo ID。
            intent: 既存chapterを保持するか、安全に再生成するかを表す手動実行意図。

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
                trigger='Manual',
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
    async def stop(cls) -> None:
        """実行中の手動CM再判定を中断へ確定し、DB接続終了前に回収する。

        Returns:
            None
        """

        async with cls._guard:
            tasks = list(cls._tasks.values())
        for task in tasks:
            task.cancel()
        if len(tasks) > 0:
            await asyncio.gather(*tasks, return_exceptions=True)

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
            await history_handle.activate('Manual')
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
                '[CMAnalysisTaskManager] Unexpected manual CM analysis failure. '
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
