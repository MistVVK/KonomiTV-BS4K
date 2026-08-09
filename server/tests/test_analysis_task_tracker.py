import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.constants import JST
from app.metadata.AnalysisTaskTracker import AnalysisTaskTracker


class FakeExecution:
    """DBを使わず履歴の状態遷移を検証するための最小実行レコード。"""

    next_id = 1

    def __init__(self, **values: Any) -> None:
        self.id = FakeExecution.next_id
        FakeExecution.next_id += 1
        self.parent_id = values.get('parent_id')
        self.status = values['status']
        self.task_type = values['task_type']
        self.trigger = values['trigger']
        self.title = values['title']
        self.file_path = values.get('file_path')
        self.stage = None
        self.progress = None
        self.stage_history: list[dict[str, object]] = []
        self.current_count = 0
        self.total_count = values['total_count']
        self.succeeded_count = 0
        self.failed_count = 0
        self.skipped_count = 0
        self.summary = None
        self.error_code = None
        self.error_message = None
        self.started_at = values['started_at']
        self.completed_at: datetime | None = None

    async def save(self, **_kwargs: Any) -> None:
        pass


def test_tracker_records_parent_child_and_success(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[FakeExecution] = []

    async def Create(**values: Any) -> FakeExecution:
        execution = FakeExecution(**values)
        created.append(execution)
        return execution

    async def Prune() -> None:
        pass

    monkeypatch.setattr('app.metadata.AnalysisTaskTracker.AnalysisTaskExecution.create', Create)
    monkeypatch.setattr(AnalysisTaskTracker, 'prune', Prune)

    async def Run() -> None:
        async with AnalysisTaskTracker.track('BatchCMAnalysis', title='一括', total_count=1) as parent:
            await parent.setStage('Processing', 0.0)
            async with AnalysisTaskTracker.track('CMAnalysis', title='番組') as child:
                await child.setStage('Analyzing', 0.5)

    asyncio.run(Run())

    assert [execution.status for execution in created] == ['Succeeded', 'Succeeded']
    assert created[1].parent_id == created[0].id
    assert created[0].stage_history[0]['completed_at'] is not None
    assert created[1].progress == 1.0


def test_tracker_records_failure_without_swallowing_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[FakeExecution] = []

    async def Create(**values: Any) -> FakeExecution:
        execution = FakeExecution(**values)
        created.append(execution)
        return execution

    async def Prune() -> None:
        pass

    monkeypatch.setattr('app.metadata.AnalysisTaskTracker.AnalysisTaskExecution.create', Create)
    monkeypatch.setattr(AnalysisTaskTracker, 'prune', Prune)

    async def Run() -> None:
        async with AnalysisTaskTracker.track('CMAnalysis', title='番組'):
            raise RuntimeError('analysis failed')

    with pytest.raises(RuntimeError, match='analysis failed'):
        asyncio.run(Run())

    assert created[0].status == 'Failed'
    assert created[0].error_code == 'RuntimeError'
    assert created[0].error_message == 'analysis failed'
    assert created[0].completed_at is not None
    assert created[0].completed_at.tzinfo == JST


def test_tracker_activates_queued_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[FakeExecution] = []

    async def Create(**values: Any) -> FakeExecution:
        execution = FakeExecution(**values)
        created.append(execution)
        return execution

    async def Prune() -> None:
        pass

    monkeypatch.setattr('app.metadata.AnalysisTaskTracker.AnalysisTaskExecution.create', Create)
    monkeypatch.setattr(AnalysisTaskTracker, 'prune', Prune)

    async def Run() -> None:
        queued = await AnalysisTaskTracker.start(
            'PlaybackIndex',
            title='番組',
            initial_status='Queued',
            inherit_parent=False,
        )
        assert queued.execution.started_at is None
        async with AnalysisTaskTracker.track(
            'PlaybackIndex',
            trigger='Manual',
            existing_handle=queued,
        ):
            pass

    asyncio.run(Run())

    assert created[0].status == 'Succeeded'
    assert created[0].trigger == 'Manual'
    assert created[0].started_at is not None


def test_delayed_progress_does_not_rewind_terminal_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """progress → finish → 遅延 progress commit の順でも terminal progress が 1.0 のまま。"""

    created: list[FakeExecution] = []
    save_events: list[tuple[str, float | None]] = []

    async def Create(**values: Any) -> FakeExecution:
        execution = FakeExecution(**values)
        original_save = execution.save

        async def Save(**kwargs: Any) -> None:
            save_events.append((execution.status, execution.progress))
            await original_save(**kwargs)

        execution.save = Save  # type: ignore[method-assign]
        created.append(execution)
        return execution

    async def Prune() -> None:
        pass

    monkeypatch.setattr('app.metadata.AnalysisTaskTracker.AnalysisTaskExecution.create', Create)
    monkeypatch.setattr(AnalysisTaskTracker, 'prune', Prune)
    # 間引きを無効化して必ず遅延 task を作る
    AnalysisTaskTracker._progress_saved_at.clear()

    async def Run() -> None:
        async with AnalysisTaskTracker.track('PlaybackIndex', title='番組') as handle:
            # 5秒閾値を回避するため時刻を進める
            AnalysisTaskTracker._progress_saved_at[handle.execution.id] = 0.0
            AnalysisTaskTracker.updateProgressSoon(0.4)
            # finish 前に pending task がある状態を作る
            assert handle._progress_save_task is not None
            await handle.finish('Succeeded')
            # finish 後に古い progress save を明示的に走らせても拒否される
            await handle.setProgress(0.4)

    asyncio.run(Run())

    assert created[0].status == 'Succeeded'
    assert created[0].progress == 1.0


def _CaptureWarnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """AnalysisTaskTracker モジュールの warning / error ログを記録するスタブを仕込む。"""

    warnings: list[str] = []
    monkeypatch.setattr(
        'app.metadata.AnalysisTaskTracker.logging',
        SimpleNamespace(
            warning=lambda message, exc_info=None: warnings.append(str(message)),
            error=lambda message, exc_info=None: warnings.append(str(message)),
        ),
    )
    return warnings


def _SetupFakeExecution(monkeypatch: pytest.MonkeyPatch) -> list[FakeExecution]:
    """DB を使わない実行レコード生成と prune を仕込む。"""

    created: list[FakeExecution] = []

    async def Create(**values: Any) -> FakeExecution:
        execution = FakeExecution(**values)
        created.append(execution)
        return execution

    async def Prune() -> None:
        pass

    monkeypatch.setattr('app.metadata.AnalysisTaskTracker.AnalysisTaskExecution.create', Create)
    monkeypatch.setattr(AnalysisTaskTracker, 'prune', Prune)
    AnalysisTaskTracker._progress_saved_at.clear()
    return created


def test_failed_progress_save_replaced_by_next_progress_logs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """保存失敗した progress タスクが次の updateProgressSoon() で置換されても warning が 1 回だけ記録されること。"""

    created = _SetupFakeExecution(monkeypatch)
    warnings = _CaptureWarnings(monkeypatch)
    state = {'fail': True}

    async def Run() -> None:
        async with AnalysisTaskTracker.track('PlaybackIndex', title='番組') as handle:
            execution = created[0]
            original_save = execution.save

            async def ProgressSave(**kwargs: Any) -> None:
                # 遅延 progress 保存 (update_fields が progress のみ) だけを一度失敗させる
                if state['fail'] is True and kwargs.get('update_fields') == ['progress', 'updated_at']:
                    raise RuntimeError('simulated progress save failure')
                await original_save(**kwargs)

            execution.save = ProgressSave  # type: ignore[method-assign]

            # 1 回目の遅延保存は失敗する
            AnalysisTaskTracker._progress_saved_at[handle.execution.id] = 0.0
            AnalysisTaskTracker.updateProgressSoon(0.4)
            first_task = handle._progress_save_task
            assert first_task is not None
            await first_task
            state['fail'] = False

            # finish より先に次の progress 保存が失敗済みタスクを置換する
            AnalysisTaskTracker._progress_saved_at[handle.execution.id] = 0.0
            AnalysisTaskTracker.updateProgressSoon(0.6)
            second_task = handle._progress_save_task
            assert second_task is not None and second_task is not first_task
            await second_task

    asyncio.run(Run())

    # 保存失敗は置換されても沈黙せず、発生地点で 1 回だけ warning となる
    assert len(warnings) == 1
    assert str(created[0].id) in warnings[0]
    # finish 本体は保存失敗に関わらず終端状態へ到達する
    assert created[0].status == 'Succeeded'
    assert created[0].progress == 1.0


def test_pending_progress_save_cancellation_does_not_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    """pending progress 保存のキャンセルは正常な終了手順であり warning を出さないこと。"""

    created = _SetupFakeExecution(monkeypatch)
    warnings = _CaptureWarnings(monkeypatch)

    async def Run() -> None:
        handle = await AnalysisTaskTracker.start('PlaybackIndex', title='番組')
        # finish 時点で実行中の遅延保存タスクはキャンセルされる
        pending_task = asyncio.create_task(asyncio.sleep(60))
        handle.replaceProgressSaveTask(pending_task)
        await handle.finish('Succeeded')
        assert pending_task.cancelled() is True

    asyncio.run(Run())

    assert created[0].status == 'Succeeded'
    assert warnings == []


def test_progress_during_finish_await_does_not_rewind_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """terminal save の await 中に開始された progress でも terminal が巻き戻らないこと。"""

    created: list[FakeExecution] = []
    terminal_save_started = asyncio.Event()
    allow_terminal_save = asyncio.Event()

    async def Create(**values: Any) -> FakeExecution:
        execution = FakeExecution(**values)
        original_save = execution.save

        async def Save(**kwargs: Any) -> None:
            # finish が status を Succeeded にした直後の save だけを停止する
            if execution.status == 'Succeeded' and execution.progress == 1.0:
                terminal_save_started.set()
                await allow_terminal_save.wait()
            await original_save(**kwargs)

        execution.save = Save  # type: ignore[method-assign]
        created.append(execution)
        return execution

    async def Prune() -> None:
        pass

    monkeypatch.setattr('app.metadata.AnalysisTaskTracker.AnalysisTaskExecution.create', Create)
    monkeypatch.setattr(AnalysisTaskTracker, 'prune', Prune)
    AnalysisTaskTracker._progress_saved_at.clear()

    async def Run() -> None:
        async with AnalysisTaskTracker.track('PlaybackIndex', title='番組') as handle:
            AnalysisTaskTracker._progress_saved_at[handle.execution.id] = 0.0
            finish_task = asyncio.create_task(handle.finish('Succeeded'))
            await terminal_save_started.wait()
            # finish の terminal save 待機中に progress を差し込む
            AnalysisTaskTracker.updateProgressSoon(0.4)
            await handle.setProgress(0.4)
            allow_terminal_save.set()
            await finish_task
            # 遅延 task があれば完了まで待つ
            pending = handle._progress_save_task
            if pending is not None:
                try:
                    await pending
                except Exception:
                    pass

    asyncio.run(Run())

    assert created[0].status == 'Succeeded'
    assert created[0].progress == 1.0
