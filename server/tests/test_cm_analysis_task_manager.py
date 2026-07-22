# pyright: reportPrivateUsage=false, reportArgumentType=false

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from app.metadata.AnalysisTaskTracker import AnalysisTaskHandle, AnalysisTaskTracker
from app.metadata.CMAnalysisOrchestrator import CMAnalysisOrchestrator
from app.metadata.CMAnalysisTaskManager import CMAnalysisTaskManager
from app.models.CMAnalysis import RecordedVideoCMAnalysis


class FakeAnalysisTaskHandle:
    """DBを使わず、マネージャーの履歴遷移だけを観測するテスト用handle。"""

    def __init__(self, execution_id: int) -> None:
        """実行IDと初期Queued相当の状態を作る。

        Args:
            execution_id: APIへ返すテスト用実行ID。
        """

        self.execution = SimpleNamespace(id=execution_id, parent_id=None)
        self.terminal = False
        self.activations: list[str] = []
        self.stages: list[tuple[str, float | None]] = []
        self.finishes: list[tuple[str, str | None, str | None]] = []

    async def activate(self, trigger: str) -> None:
        """Running遷移の呼び出しを記録する。

        Args:
            trigger: 実行契機。

        Returns:
            None
        """

        self.activations.append(trigger)

    async def setStage(self, stage: str, progress: float | None = None) -> None:
        """stage更新を記録する。

        Args:
            stage: 処理段階。
            progress: 進捗率。

        Returns:
            None
        """

        self.stages.append((stage, progress))

    async def finish(
        self,
        status: str = 'Succeeded',
        *,
        summary: dict[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """終端状態を記録する。

        Args:
            status: 終端状態。
            summary: 未使用の実行概要。
            error_code: 終端理由コード。
            error_message: 管理者向けエラー本文。

        Returns:
            None
        """

        del summary
        self.finishes.append((status, error_code, error_message))
        self.terminal = True


@pytest.mark.parametrize(
    ('state', 'intent', 'expected'),
    [
        pytest.param(
            RecordedVideoCMAnalysis(
                status='Completed',
                chapter_source='Generated',
                chapter_path_kind='Canonical',
            ),
            'CMRegeneration',
            ('Succeeded', None),
            id='generated-yaml-regeneration',
        ),
        pytest.param(
            RecordedVideoCMAnalysis(
                status='Completed',
                chapter_source='Generated',
                chapter_path_kind='Canonical',
            ),
            'CMDetection',
            ('Skipped', 'GeneratedChapterKept'),
            id='generated-yaml-detection-keeps-generated',
        ),
        pytest.param(
            RecordedVideoCMAnalysis(
                status='Completed',
                chapter_source='Existing',
                chapter_path_kind='Canonical',
            ),
            'CMRegeneration',
            ('Skipped', 'ExternalCanonicalChapterProtected'),
            id='manual-yaml-regeneration-is-protected',
        ),
        pytest.param(
            RecordedVideoCMAnalysis(
                status='Completed',
                chapter_source='Existing',
                chapter_path_kind='Canonical',
            ),
            'CMDetection',
            ('Skipped', 'ExternalCanonicalChapterProtected'),
            id='manual-yaml-detection-is-protected',
        ),
        pytest.param(
            RecordedVideoCMAnalysis(status='Completed', chapter_source='Existing', chapter_path_kind='Legacy'),
            'CMDetection',
            ('Skipped', 'ExistingChapterKept'),
            id='basic-name-text-detection-keeps-existing',
        ),
        (
            RecordedVideoCMAnalysis(status='Pending', error_code='CMAnalysisDisabled'),
            'CMRegeneration',
            ('Skipped', 'CMAnalysisDisabled'),
        ),
        (
            RecordedVideoCMAnalysis(status='Excluded', error_code='ExcludedDirectory'),
            'CMRegeneration',
            ('Skipped', 'ExcludedDirectory'),
        ),
        (
            RecordedVideoCMAnalysis(status='Failed', error_code='MediaProbeFailed'),
            'CMRegeneration',
            ('Failed', 'MediaProbeFailed'),
        ),
        (
            RecordedVideoCMAnalysis(status='Unsupported', error_code='UnsupportedCodec'),
            'CMRegeneration',
            ('Failed', 'UnsupportedCodec'),
        ),
        (
            RecordedVideoCMAnalysis(status='Interrupted', error_code='ServerRestarted'),
            'CMRegeneration',
            ('Interrupted', 'ServerRestarted'),
        ),
        (RecordedVideoCMAnalysis(status='Analyzing'), 'CMRegeneration', ('Failed', 'NonTerminalCMAnalysisState')),
        (None, 'CMRegeneration', ('Failed', 'RecordedVideoUnavailable')),
    ],
)
def test_terminal_outcome_preserves_success_skip_and_failure_meaning(
    state: RecordedVideoCMAnalysis | None,
    intent: str,
    expected: tuple[str, str | None],
) -> None:
    """HTTP成功と解析成功を分離し、早期returnも正しい終端理由へ変換する。"""

    assert CMAnalysisTaskManager._resolveTerminalOutcome(state, intent) == expected


def test_enqueue_reuses_active_recording_and_returns_before_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同じ録画の連打を同じ実行IDへ合流し、解析完了をenqueue内で待たない。"""

    async def Run() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        history = FakeAnalysisTaskHandle(1234)
        start_history = AsyncMock(return_value=cast(AnalysisTaskHandle, history))
        prune = AsyncMock()
        run_count = 0

        async def RunOrchestrator(
            orchestrator: CMAnalysisOrchestrator,
            recorded_video_id: int,
            intent: str,
            *,
            existing_handle: AnalysisTaskHandle | None = None,
        ) -> RecordedVideoCMAnalysis:
            del orchestrator
            nonlocal run_count
            run_count += 1
            assert recorded_video_id == 157
            assert intent == 'CMRegeneration'
            assert existing_handle is history
            started.set()
            await release.wait()
            return RecordedVideoCMAnalysis(status='Completed', chapter_source='Generated')

        monkeypatch.setattr(AnalysisTaskTracker, 'start', start_history)
        monkeypatch.setattr(AnalysisTaskTracker, 'prune', prune)
        monkeypatch.setattr(CMAnalysisOrchestrator, 'run', RunOrchestrator)
        CMAnalysisTaskManager._tasks = {}
        CMAnalysisTaskManager._history_handles = {}
        CMAnalysisTaskManager._guard = asyncio.Lock()

        first = await CMAnalysisTaskManager.enqueue(157, 'CMRegeneration')
        await started.wait()
        second = await CMAnalysisTaskManager.enqueue(157, 'CMRegeneration')

        assert first == (1234, False)
        assert second == (1234, True)
        assert start_history.await_count == 1
        assert run_count == 1
        task = CMAnalysisTaskManager._tasks[157]
        release.set()
        await task

        assert history.activations == ['Manual']
        assert history.stages == [('Probing', 0.0)]
        assert history.finishes == [('Succeeded', None, None)]
        assert CMAnalysisTaskManager._tasks == {}
        assert CMAnalysisTaskManager._history_handles == {}

    asyncio.run(Run())


def test_stop_cancels_and_joins_detached_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    """shutdown時はdetached Taskを回収し、履歴をInterruptedへ確定する。"""

    async def Run() -> None:
        started = asyncio.Event()
        history = FakeAnalysisTaskHandle(4321)

        async def RunOrchestrator(
            orchestrator: CMAnalysisOrchestrator,
            recorded_video_id: int,
            intent: str,
            *,
            existing_handle: AnalysisTaskHandle | None = None,
        ) -> RecordedVideoCMAnalysis:
            del orchestrator, recorded_video_id, intent, existing_handle
            started.set()
            await asyncio.Event().wait()
            raise AssertionError('unreachable')

        monkeypatch.setattr(
            AnalysisTaskTracker,
            'start',
            AsyncMock(return_value=cast(AnalysisTaskHandle, history)),
        )
        monkeypatch.setattr(AnalysisTaskTracker, 'prune', AsyncMock())
        monkeypatch.setattr(CMAnalysisOrchestrator, 'run', RunOrchestrator)
        CMAnalysisTaskManager._tasks = {}
        CMAnalysisTaskManager._history_handles = {}
        CMAnalysisTaskManager._guard = asyncio.Lock()

        assert await CMAnalysisTaskManager.enqueue(157, 'CMRegeneration') == (4321, False)
        await started.wait()
        await CMAnalysisTaskManager.stop()

        assert history.finishes == [('Interrupted', 'Cancelled', None)]
        assert CMAnalysisTaskManager._tasks == {}
        assert CMAnalysisTaskManager._history_handles == {}

    asyncio.run(Run())
