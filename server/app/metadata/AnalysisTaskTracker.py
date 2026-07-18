from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta

from app import logging
from app.constants import JST
from app.models.AnalysisTask import (
    AnalysisTaskExecution,
    AnalysisTaskStatus,
    AnalysisTaskTrigger,
    AnalysisTaskType,
)
from app.models.RecordedVideo import RecordedVideo


_current_execution_id: ContextVar[int | None] = ContextVar('analysis_task_execution_id', default=None)


class AnalysisTaskHandle:
    """単一実行の進捗と終了状態を安全に更新する。"""

    def __init__(self, execution: AnalysisTaskExecution) -> None:
        """追跡対象のDBレコードを保持する。

        Args:
            execution: このハンドルが更新する実行履歴。
        """

        # executionはtrack()のコンテキスト中だけ更新し、terminal後は変更しない。
        self.execution = execution
        self.terminal = execution.status in ('Succeeded', 'Failed', 'Interrupted', 'Skipped')

    async def setStage(self, stage: str, progress: float | None = None) -> None:
        """処理段階を切り替え、直前段階の終了時刻も保存する。"""

        if self.terminal:
            return
        now = datetime.now(tz=JST)
        history = list(self.execution.stage_history)
        if history and history[-1].get('completed_at') is None:
            history[-1]['completed_at'] = now.isoformat()
        history.append({'stage': stage, 'started_at': now.isoformat(), 'completed_at': None})
        self.execution.stage = stage
        self.execution.progress = progress
        self.execution.stage_history = history
        await self.execution.save(update_fields=['stage', 'progress', 'stage_history', 'updated_at'])

    async def setTitle(self, title: str) -> None:
        """ファイル名だけで開始した履歴を解析後の番組名へ更新する。"""

        self.execution.title = title
        await self.execution.save(update_fields=['title', 'updated_at'])

    async def activate(self, trigger: AnalysisTaskTrigger) -> None:
        """待機履歴を実行中へ遷移する。"""

        if self.terminal or self.execution.status == 'Running':
            return
        self.execution.status = 'Running'
        self.execution.trigger = trigger
        self.execution.started_at = datetime.now(tz=JST)
        await self.execution.save(update_fields=['status', 'trigger', 'started_at', 'updated_at'])

    async def setProgress(self, progress: float) -> None:
        """0～1へ正規化した進捗をメモリとDBへ反映する。"""

        if self.terminal:
            return
        self.execution.progress = max(0.0, min(1.0, progress))
        await self.execution.save(update_fields=['progress', 'updated_at'])

    async def setCounts(
        self,
        *,
        current: int | None = None,
        total: int | None = None,
        succeeded: int | None = None,
        failed: int | None = None,
        skipped: int | None = None,
    ) -> None:
        """一括処理の件数と算出可能な進捗率を更新する。"""

        if self.terminal:
            return
        for field, value in (
            ('current_count', current),
            ('total_count', total),
            ('succeeded_count', succeeded),
            ('failed_count', failed),
            ('skipped_count', skipped),
        ):
            if value is not None:
                setattr(self.execution, field, value)
        if self.execution.total_count > 0:
            self.execution.progress = min(1.0, self.execution.current_count / self.execution.total_count)
        await self.execution.save(update_fields=[
            'current_count', 'total_count', 'succeeded_count', 'failed_count', 'skipped_count',
            'progress', 'updated_at',
        ])

    async def attachCMLogoBatch(self, batch_id: int) -> None:
        """詳細画面から既存ロゴ生成試行を参照できるよう関連付ける。"""

        self.execution.cm_logo_generation_batch_id = batch_id
        await self.execution.save(update_fields=['cm_logo_generation_batch_id', 'updated_at'])

    async def finish(
        self,
        status: AnalysisTaskStatus = 'Succeeded',
        *,
        summary: dict[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """実行を一度だけ終端状態へ確定する。"""

        if self.terminal:
            return
        now = datetime.now(tz=JST)
        history = list(self.execution.stage_history)
        if history and history[-1].get('completed_at') is None:
            history[-1]['completed_at'] = now.isoformat()
        self.execution.status = status
        self.execution.completed_at = now
        self.execution.progress = 1.0 if status == 'Succeeded' else self.execution.progress
        self.execution.stage_history = history
        self.execution.summary = summary
        self.execution.error_code = error_code
        self.execution.error_message = error_message[-8192:] if error_message else None
        await self.execution.save(update_fields=[
            'status', 'completed_at', 'progress', 'stage_history', 'summary', 'error_code', 'error_message', 'updated_at',
        ])
        self.terminal = True


_current_handle = ContextVar[AnalysisTaskHandle | None]('analysis_task_handle', default=None)


class AnalysisTaskTracker:
    """解析処理の親子関係・状態遷移・保存期限を一元管理する。"""

    _progress_saved_at: dict[int, float] = {}

    @classmethod
    @asynccontextmanager
    async def track(
        cls,
        task_type: AnalysisTaskType,
        *,
        recorded_video_id: int | None = None,
        title: str | None = None,
        trigger: AnalysisTaskTrigger = 'Automatic',
        total_count: int = 0,
        parent_id: int | None = None,
        existing_handle: AnalysisTaskHandle | None = None,
    ) -> AsyncGenerator[AnalysisTaskHandle, None]:
        """実行履歴を作成し、例外・キャンセルを必ず終端状態へ変換する。"""

        handle = existing_handle
        if handle is None:
            handle = await cls.start(
                task_type,
                recorded_video_id=recorded_video_id,
                title=title,
                trigger=trigger,
                total_count=total_count,
                parent_id=parent_id,
            )
        else:
            await handle.activate(trigger)
        execution = handle.execution
        token = _current_execution_id.set(execution.id)
        handle_token = _current_handle.set(handle)
        try:
            yield handle
        except asyncio.CancelledError:
            await handle.finish('Interrupted', error_code='Cancelled')
            raise
        except Exception as ex:
            await handle.finish('Failed', error_code=type(ex).__name__, error_message=str(ex))
            raise
        else:
            await handle.finish()
        finally:
            _current_handle.reset(handle_token)
            _current_execution_id.reset(token)
            cls._progress_saved_at.pop(execution.id, None)
            if execution.parent_id is None and handle.terminal:
                try:
                    await cls.prune()
                except Exception as ex:
                    # 履歴の保存期限適用に失敗しても、本体処理の成功・失敗は変えない。
                    logging.error('[AnalysisTaskTracker] Failed to prune execution history:', exc_info=ex)

    @classmethod
    async def start(
        cls,
        task_type: AnalysisTaskType,
        *,
        recorded_video_id: int | None = None,
        title: str | None = None,
        trigger: AnalysisTaskTrigger = 'Automatic',
        total_count: int = 0,
        parent_id: int | None = None,
        initial_status: AnalysisTaskStatus = 'Running',
        inherit_parent: bool = True,
    ) -> AnalysisTaskHandle:
        """コンテキストを占有せず、明示的に終了する子履歴を開始する。"""

        if title is None and recorded_video_id is not None:
            video = await RecordedVideo.get_or_none(id=recorded_video_id).select_related('recorded_program')
            if video is not None:
                title = video.recorded_program.title
        execution = await AnalysisTaskExecution.create(
            parent_id=(parent_id if parent_id is not None else _current_execution_id.get()) if inherit_parent else None,
            recorded_video_id=recorded_video_id,
            task_type=task_type,
            status=initial_status,
            trigger=trigger,
            title=title or cls.getTaskLabel(task_type),
            total_count=total_count,
            started_at=datetime.now(tz=JST) if initial_status == 'Running' else None,
        )
        return AnalysisTaskHandle(execution)

    @staticmethod
    def currentExecutionID() -> int | None:
        """現在の非同期コンテキストが属する親実行IDを返す。"""

        return _current_execution_id.get()

    @staticmethod
    def currentHandle() -> AnalysisTaskHandle | None:
        """現在の処理を追跡するハンドルを返す。"""

        return _current_handle.get()

    @classmethod
    def updateProgressSoon(cls, progress: float) -> None:
        """高頻度の走査進捗を5秒に一度だけDBへ非同期保存する。"""

        handle = cls.currentHandle()
        if handle is None or handle.terminal:
            return
        normalized = max(0.0, min(1.0, progress))
        handle.execution.progress = normalized
        now = time.monotonic()
        if now - cls._progress_saved_at.get(handle.execution.id, 0.0) < 5.0:
            return
        cls._progress_saved_at[handle.execution.id] = now
        task = asyncio.create_task(handle.setProgress(normalized))
        task.add_done_callback(lambda completed: None if completed.cancelled() else completed.exception())

    @staticmethod
    def getTaskLabel(task_type: AnalysisTaskType) -> str:
        """録画がない一括処理用の安定した表示名を返す。"""

        return {
            'RecordedScan': '録画フォルダスキャン',
            'MetadataAnalysis': 'メタデータ解析',
            'PlaybackIndex': '再生索引作成',
            'ThumbnailGeneration': 'サムネイル生成',
            'CMAnalysis': 'CM区間解析',
            'CMLogoGeneration': 'CMロゴ生成',
            'BatchScan': '録画フォルダ一括スキャン',
            'BatchMetadataReanalysis': '全件メタデータ再解析',
            'BatchCMAnalysis': '全件CM再判定',
            'BackgroundAnalysis': 'バックグラウンド一括解析',
        }[task_type]

    @classmethod
    async def initialize(cls) -> None:
        """前回プロセスの未完了履歴を中断へ確定し、保存期限を適用する。"""

        now = datetime.now(tz=JST)
        await AnalysisTaskExecution.filter(status__in=['Queued', 'Running']).update(
            status='Interrupted',
            completed_at=now,
            error_code='ServerRestarted',
        )
        await cls.prune()

    @staticmethod
    async def prune() -> None:
        """完了済みルート履歴を30日かつ最新1000件へ制限する。"""

        terminal = ['Succeeded', 'Failed', 'Interrupted', 'Skipped']
        cutoff = datetime.now(tz=JST) - timedelta(days=30)
        await AnalysisTaskExecution.filter(
            parent_id=None,
            status__in=terminal,
            completed_at__lt=cutoff,
        ).delete()
        retained_ids = await AnalysisTaskExecution.filter(
            parent_id=None,
            status__in=terminal,
        ).order_by('-created_at').limit(1000).values_list('id', flat=True)
        if len(retained_ids) == 1000:
            await AnalysisTaskExecution.filter(parent_id=None, status__in=terminal).exclude(id__in=retained_ids).delete()
