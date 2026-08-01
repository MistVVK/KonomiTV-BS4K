import asyncio
import importlib
from collections.abc import Awaitable, Callable

import pytest

from app import config as config_module


# app.app は通常 KonomiTV.py で初期化済みの設定を参照するため、単体テストでは安全な既定値を先に設定する。
if config_module._CONFIG is None:
    config_module._CONFIG = config_module.HostServerSettings().toServerSettings(bypass_validation=True)
app_module = importlib.import_module('app.app')

# テストプロセス終了時の atexit fallback が実サービス用 cleanup を起動しないよう、既定状態を完了済みにする。
app_module._shutdown_completed = True


def _RunShutdownScenario(
    monkeypatch: pytest.MonkeyPatch,
    cleanup: Callable[[], Awaitable[None]],
    scenario: Callable[[], Awaitable[None]],
) -> None:
    """終了状態をテスト用に初期化し、指定した非同期シナリオを実行する。"""

    monkeypatch.setattr(app_module, '_shutdown_completed', False)
    monkeypatch.setattr(app_module, '_shutdown_task', None)
    monkeypatch.setattr(app_module, '_RunShutdownCleanup', cleanup)
    asyncio.run(scenario())


def test_shutdown_shares_task_and_shields_it_from_waiter_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同時呼び出しは1個の cleanup Task を共有し、片方のキャンセル後も完了まで継続する。"""

    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_calls = 0

    async def Cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        cleanup_started.set()
        await cleanup_release.wait()

    async def Scenario() -> None:
        first_waiter = asyncio.create_task(app_module.Shutdown())
        await cleanup_started.wait()
        shared_task = app_module._shutdown_task
        assert shared_task is not None

        second_waiter = asyncio.create_task(app_module.Shutdown())
        await asyncio.sleep(0)
        first_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_waiter

        assert shared_task.cancelled() is False
        cleanup_release.set()
        await second_waiter
        assert app_module._shutdown_completed is True
        assert cleanup_calls == 1

        # 成功後の再呼び出しは同じ完了状態を再利用し、cleanup を再実行しない。
        await app_module.Shutdown()
        assert cleanup_calls == 1

    _RunShutdownScenario(monkeypatch, Cleanup, Scenario)


@pytest.mark.parametrize('failure_kind', ['exception', 'cancellation'])
def test_shutdown_retries_after_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    """cleanup Task 自体の例外・キャンセルでは完了扱いせず、次の呼び出しで再試行する。"""

    cleanup_calls = 0

    async def Cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls != 1:
            return
        if failure_kind == 'exception':
            raise RuntimeError('test shutdown failure')
        raise asyncio.CancelledError

    async def Scenario() -> None:
        expected_error = RuntimeError if failure_kind == 'exception' else asyncio.CancelledError
        with pytest.raises(expected_error):
            await app_module.Shutdown()

        assert app_module._shutdown_completed is False
        assert app_module._shutdown_task is None

        await app_module.Shutdown()
        assert app_module._shutdown_completed is True
        assert cleanup_calls == 2

    _RunShutdownScenario(monkeypatch, Cleanup, Scenario)


def test_shutdown_retries_cancelled_task_from_closed_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """呼び出し側キャンセル後に旧 loop が閉じても、atexit 相当の新しい loop で再試行できる。"""

    cleanup_calls = 0

    async def Cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            await asyncio.Future()

    async def CancelFirstWaiter() -> None:
        waiter = asyncio.create_task(app_module.Shutdown())
        while app_module._shutdown_task is None:
            await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    monkeypatch.setattr(app_module, '_shutdown_completed', False)
    monkeypatch.setattr(app_module, '_shutdown_task', None)
    monkeypatch.setattr(app_module, '_RunShutdownCleanup', Cleanup)

    asyncio.run(CancelFirstWaiter())
    shutdown_task = app_module._shutdown_task
    assert shutdown_task is not None
    assert shutdown_task.cancelled() is True
    assert app_module._shutdown_completed is False

    asyncio.run(app_module.Shutdown())
    assert app_module._shutdown_completed is True
    assert cleanup_calls == 2


def test_run_shutdown_cleanup_reports_unconfirmed_source_after_remaining_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """source停止未確認を成功扱いせず、後続cleanupをすべて試行してから失敗を送出する。"""

    completed_steps: list[str] = []

    async def SourceShutdown() -> bool:
        completed_steps.append('source')
        return False

    async def DrainFinalizeTasks() -> bool:
        completed_steps.append('prepare-drain')
        return True

    async def ReleaseAll(*, reason: str) -> int:
        assert reason == 'shutdown'
        completed_steps.append('prepare-release')
        return 0

    async def StopStep(_cls: object) -> None:
        completed_steps.append('metadata-stop')

    async def CloseAll(_cls: object) -> None:
        completed_steps.append('edcb-close')

    monkeypatch.setattr(app_module.LiveStream, 'getAllLiveStreams', staticmethod(lambda: []))
    monkeypatch.setattr(app_module.LIVE_SOURCE_COORDINATOR, 'shutdown', SourceShutdown)
    monkeypatch.setattr(
        app_module.LIVE_PREPARE_COORDINATOR,
        'drainFinalizeTasks',
        DrainFinalizeTasks,
    )
    monkeypatch.setattr(app_module.LIVE_PREPARE_COORDINATOR, 'releaseAll', ReleaseAll)
    monkeypatch.setattr(app_module, 'recorded_scan_task', None)
    monkeypatch.setattr(app_module.EDCBTuner, 'closeAll', classmethod(CloseAll))
    monkeypatch.setattr(app_module.RecordedSeriesResolver, 'stop', classmethod(StopStep))
    monkeypatch.setattr(app_module.RecordedEpisodeAutomation, 'stop', classmethod(StopStep))
    monkeypatch.setattr(app_module.CMAnalysisTaskManager, 'stop', classmethod(StopStep))
    monkeypatch.setattr(app_module.RecordedPlaybackIndexer, 'stop', classmethod(StopStep))

    with pytest.raises(RuntimeError, match='shutdown_cleanup_failed'):
        asyncio.run(app_module._RunShutdownCleanup())

    assert completed_steps[:3] == ['source', 'prepare-drain', 'prepare-release']
    assert completed_steps.count('metadata-stop') == 4
