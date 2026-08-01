import asyncio
from collections.abc import Generator
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.routers import AnalysisTasksRouter


class FakeExecutionQuery:
    """AnalysisTaskExecution のチェーン可能な一覧クエリをテスト用に代替する。"""

    def __init__(self, items: list[SimpleNamespace]) -> None:
        self.items = items

    def prefetch_related(self, *_fields: str) -> 'FakeExecutionQuery':
        return self

    def order_by(self, *_fields: str) -> 'FakeExecutionQuery':
        return self

    def limit(self, _count: int) -> 'FakeExecutionQuery':
        return self

    def __await__(self) -> Generator[object, None, list[SimpleNamespace]]:
        async def Resolve() -> list[SimpleNamespace]:
            return self.items

        return Resolve().__await__()


def MakeExecution(
    execution_id: int,
    *,
    parent_id: int | None,
    task_type: str,
    status: str,
    title: str,
    file_path: str | None,
    stage: str | None,
) -> SimpleNamespace:
    """概要 API のシリアライズに必要な属性を持つ実行履歴を生成する。"""

    now = datetime.now().astimezone()
    return SimpleNamespace(
        id=execution_id,
        parent_id=parent_id,
        recorded_video_id=None,
        recorded_video=None,
        task_type=task_type,
        status=status,
        trigger='Maintenance',
        title=title,
        file_path=file_path,
        stage=stage,
        progress=0.5,
        stage_history=[],
        current_count=1,
        total_count=2,
        succeeded_count=0,
        failed_count=0,
        skipped_count=0,
        summary=None,
        error_code=None,
        error_message=None,
        started_at=now,
        completed_at=None,
        created_at=now,
        updated_at=now,
    )


def test_overview_includes_only_active_children_of_active_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    root = MakeExecution(
        100,
        parent_id=None,
        task_type='BackgroundAnalysis',
        status='Running',
        title='バックグラウンド一括解析',
        file_path=None,
        stage='Processing',
    )
    child = MakeExecution(
        101,
        parent_id=100,
        task_type='CMAnalysis',
        status='Running',
        title='番組A',
        file_path='/recorded/番組A.ts',
        stage='ChapterAnalyzing',
    )
    filter_calls: list[dict[str, object]] = []

    def FilterExecutions(**filters: object) -> FakeExecutionQuery:
        filter_calls.append(filters)
        if (
            'parent_id' in filters
            and filters.get('parent_id') is None
            and filters.get('status__in') == ['Queued', 'Running']
        ):
            return FakeExecutionQuery([root])
        if filters.get('parent_id__in') == [100]:
            return FakeExecutionQuery([child])
        return FakeExecutionQuery([])

    monkeypatch.setattr(AnalysisTasksRouter.AnalysisTaskExecution, 'filter', FilterExecutions)
    current_user = cast(Any, SimpleNamespace(is_admin=False))

    overview = asyncio.run(AnalysisTasksRouter.AnalysisTaskOverviewAPI(current_user))

    assert [item.id for item in overview.active] == [100]
    assert [item.id for item in overview.active_children] == [101]
    assert overview.active_children[0].file_path is not None
    assert overview.active_children[0].file_path.endswith('/番組A.ts')
    assert overview.active_children[0].stage == 'ChapterAnalyzing'
    assert {
        'parent_id__in': [100],
        'status__in': ['Queued', 'Running'],
    } in filter_calls


def test_overview_skips_child_query_when_there_are_no_active_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    filter_calls: list[dict[str, object]] = []

    def FilterExecutions(**filters: object) -> FakeExecutionQuery:
        filter_calls.append(filters)
        return FakeExecutionQuery([])

    monkeypatch.setattr(AnalysisTasksRouter.AnalysisTaskExecution, 'filter', FilterExecutions)
    current_user = cast(Any, SimpleNamespace(is_admin=False))

    overview = asyncio.run(AnalysisTasksRouter.AnalysisTaskOverviewAPI(current_user))

    assert overview.active == []
    assert overview.active_children == []
    assert overview.recent == []
    assert all('parent_id__in' not in filters for filters in filter_calls)
