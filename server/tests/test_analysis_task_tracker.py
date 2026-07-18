import asyncio
from datetime import datetime
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
