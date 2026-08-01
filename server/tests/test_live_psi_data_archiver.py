import asyncio

import pytest

from app.streams.LivePSIDataArchiver import LivePSIDataArchiver


class _FakePSISIArcProcess:
    """destroy() の個別失敗を検証するための psisiarc プロセス代替。"""

    def __init__(self, pid: int, should_fail: bool = False) -> None:
        """
        プロセス代替を初期化する。

        Args:
            pid (int): ログ出力に使用するプロセス ID。
            should_fail (bool): kill() を失敗させるか。

        Returns:
            None
        """

        self.pid = pid
        self.returncode: int | None = None
        self.should_fail = should_fail
        self.kill_count = 0
        self.wait_count = 0
        self.stdin = None

    def kill(self) -> None:
        """
        kill 呼び出しを記録し、必要に応じて失敗させる。

        Args:
            None

        Returns:
            None
        """

        self.kill_count += 1
        if self.should_fail is True:
            raise OSError('simulated kill failure')
        self.returncode = 0

    async def wait(self) -> int:
        """wait 呼び出しを記録する。"""

        self.wait_count += 1
        return self.returncode or 0


def test_destroy_continues_after_individual_process_failure() -> None:
    """
    一つの psisiarc の kill が失敗しても、残りを回収して二重 destroy を no-op にする。

    Args:
        None

    Returns:
        None
    """

    archiver = LivePSIDataArchiver(1)
    failing_process = _FakePSISIArcProcess(100, should_fail=True)
    succeeding_process = _FakePSISIArcProcess(101)
    already_exited_process = _FakePSISIArcProcess(102)
    already_exited_process.returncode = 0
    archiver._psisiarc_processes = [failing_process, succeeding_process, already_exited_process]  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match='psi_data_archiver_cleanup_failed'):
        asyncio.run(archiver.destroy())

    assert failing_process.kill_count == 1
    assert succeeding_process.kill_count == 1
    assert already_exited_process.kill_count == 0
    assert failing_process.wait_count == 1
    assert succeeding_process.wait_count == 1
    assert already_exited_process.wait_count == 1
    assert archiver._psisiarc_processes == []

    asyncio.run(archiver.destroy())
    assert failing_process.kill_count == 1
    assert succeeding_process.kill_count == 1


def test_generator_cancel_during_blocked_read_reaps_psisiarc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """readexactly() 待機中の HTTP generator cancel でも process と登録参照を残さない。"""

    class FakeProcess(_FakePSISIArcProcess):
        def __init__(self) -> None:
            super().__init__(200)
            self.stdout = asyncio.StreamReader()
            self.stderr = None

    process: FakeProcess | None = None

    async def CreateProcess(*_args, **_kwargs):
        nonlocal process
        process = FakeProcess()
        return process

    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    monkeypatch.setattr(
        'app.streams.LivePSIDataArchiver.asyncio.subprocess.create_subprocess_exec',
        CreateProcess,
    )
    monkeypatch.setattr('app.streams.LivePSIDataArchiver.logging.debug', lambda *_args, **_kwargs: None)
    archiver = LivePSIDataArchiver(1)

    async def Verify() -> None:
        generator = archiver.getPSIArchivedData(ConnectedRequest())  # type: ignore[arg-type]
        reader = asyncio.create_task(anext(generator))
        await asyncio.sleep(0)
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        assert process is not None
        assert process.kill_count == 1
        assert process.wait_count == 1
        assert archiver._psisiarc_processes == []

    asyncio.run(Verify())
