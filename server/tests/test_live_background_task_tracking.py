import asyncio

from app.streams.LiveEncodingTask import LiveEncodingTask


def test_track_background_task_discards_completed_tasks() -> None:
    """
    即完了 Task を大量追加しても、set に完了済み参照が残らないことを検証する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """
        短命 Task を大量登録し、完了後の set サイズを確認する。

        Args:
            None

        Returns:
            None
        """

        background_tasks: set[asyncio.Task[None]] = set()

        async def ImmediateTask() -> None:
            """即座に完了する短命 Task。"""

            return None

        for _ in range(3000):
            LiveEncodingTask.trackBackgroundTask(asyncio.create_task(ImmediateTask()), background_tasks)

        # 全 Task の完了を待ち、done callback による discard を反映させる
        if len(background_tasks) > 0:
            await asyncio.gather(*tuple(background_tasks), return_exceptions=True)
        await asyncio.sleep(0)

        assert background_tasks == set()

    asyncio.run(run())


def test_track_background_task_keeps_only_running_tasks() -> None:
    """
    実行中 Task だけが set に残り、完了した Task は取り除かれることを検証する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """
        実行中 Task と完了 Task を混在させて保持状況を確認する。

        Args:
            None

        Returns:
            None
        """

        background_tasks: set[asyncio.Task[None]] = set()
        gate = asyncio.Event()

        async def ImmediateTask() -> None:
            """即座に完了する短命 Task。"""

            return None

        async def LongLivedTask() -> None:
            """gate が開くまで生き残る長寿命 Task。"""

            await gate.wait()

        for _ in range(100):
            LiveEncodingTask.trackBackgroundTask(asyncio.create_task(ImmediateTask()), background_tasks)

        long_lived = LiveEncodingTask.trackBackgroundTask(
            asyncio.create_task(LongLivedTask()),
            background_tasks,
        )

        # 短命 Task が完了し、done callback が処理されるまで待つ
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert background_tasks == {long_lived}
        assert long_lived in background_tasks

        gate.set()
        await long_lived
        await asyncio.sleep(0)
        assert background_tasks == set()

    asyncio.run(run())
