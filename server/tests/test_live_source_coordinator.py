from __future__ import annotations

import asyncio
import importlib
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from app.streams.LiveSourceCoordinator import (
    LiveSourceCapacityError,
    LiveSourceCoordinator,
    LiveSourceDescriptor,
    LiveSourceError,
    LiveSourceKey,
    LiveSourcePreemptedError,
    LiveSourceSubscriberError,
    createTSReadExLiveSourceProducer,
)


class _FakeProducer:
    """任意 chunk・例外・EOF を投入できる共有 producer。"""

    psi_data_archiver = None
    source_geometry = None
    last_input_at = 1.0

    def __init__(self) -> None:
        """
        利用する状態を初期化する。

        Args:
            None

        Returns:
            None
        """
        self.input: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue()
        self.close_count = 0

    async def read(self) -> bytes:
        """
        次のデータを読み取る。

        Args:
            None

        Returns:
            bytes: 処理結果のバイト列。
        """
        item = await self.input.get()
        if item is None:
            return b''
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        """
        保持するリソースを閉じる。

        Args:
            None

        Returns:
            None
        """
        self.close_count += 1


class _CancellationResistantCloseProducer(_FakeProducer):
    """cancel 後も外部解放まで close() に留まる producer。"""

    def __init__(self) -> None:
        """
        利用する状態を初期化する。

        Args:
            None

        Returns:
            None
        """

        super().__init__()
        self.close_started = asyncio.Event()
        self.close_finished = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_cancellation_count = 0

    async def close(self) -> None:
        """
        cancel を記録しつつ、テスト側が明示解放するまで停止しない。

        Args:
            None

        Returns:
            None
        """

        self.close_count += 1
        self.close_started.set()
        try:
            while self.release_close.is_set() is False:
                try:
                    await self.release_close.wait()
                except asyncio.CancelledError:
                    self.close_cancellation_count += 1
        finally:
            self.close_finished.set()


class _FailingCloseProducer(_FakeProducer):
    """全解放を試みた後に cleanup 未確認を返す producer。"""

    async def close(self) -> None:
        """
        close 呼び出しを記録して、cleanup 失敗を送出する。

        Args:
            None

        Returns:
            None
        """

        self.close_count += 1
        raise RuntimeError('producer cleanup failed')


class _FakeShutdownSession:
    """coordinator shutdown の session 単位例外隔離を検証する fake。"""

    def __init__(self, *, failure: Exception | None = None) -> None:
        """
        利用する状態を初期化する。

        Args:
            failure (Exception | None): stop() から送出する例外。

        Returns:
            None
        """

        self.failure = failure
        self.stop_count = 0

    async def stop(self) -> None:
        """
        呼び出しを記録し、指定されていれば例外を送出する。

        Args:
            None

        Returns:
            None
        """

        self.stop_count += 1
        await asyncio.sleep(0)
        if self.failure is not None:
            raise self.failure


class _CancellationResistantShutdownSession:
    """deadline 後もテスト側の解放まで stop() に留まる shutdown 用 fake。"""

    def __init__(self) -> None:
        """
        利用する状態を初期化する。

        Args:
            None

        Returns:
            None
        """

        self.stop_count = 0
        self.stop_started = asyncio.Event()
        self.stop_finished = asyncio.Event()
        self.release_stop = asyncio.Event()
        self.stop_cancellation_count = 0

    async def stop(self) -> None:
        """
        cancel を受けても記録し、テスト側が明示解放するまで停止しない。

        Args:
            None

        Returns:
            None
        """

        self.stop_count += 1
        self.stop_started.set()
        try:
            while self.release_stop.is_set() is False:
                try:
                    await self.release_stop.wait()
                except asyncio.CancelledError:
                    self.stop_cancellation_count += 1
        finally:
            self.stop_finished.set()


class _FakeProcessStdin:
    def __init__(self) -> None:
        """
        利用する状態を初期化する。

        Args:
            None

        Returns:
            None
        """
        self.chunks: list[bytes] = []
        self.closed = False

    def write(self, chunk: bytes) -> None:
        """
        指定データを書き込む。

        Args:
            chunk (bytes): 処理対象の TS chunk。

        Returns:
            None
        """
        self.chunks.append(chunk)

    async def drain(self) -> None:
        """
        保留中の書き込み完了を待つ。

        Args:
            None

        Returns:
            None
        """
        pass

    def close(self) -> None:
        """
        保持するリソースを閉じる。

        Args:
            None

        Returns:
            None
        """
        self.closed = True


class _FakeProcessStdout:
    async def readexactly(self, size: int) -> bytes:
        """
        指定サイズのデータを読み取る。

        Args:
            size (int): 読み取るバイト数。

        Returns:
            bytes: 処理結果のバイト列。
        """
        raise asyncio.IncompleteReadError(b'', size)


class _FakeSubprocess:
    def __init__(self) -> None:
        """
        利用する状態を初期化する。

        Args:
            None

        Returns:
            None
        """
        self.stdin = _FakeProcessStdin()
        self.stdout = _FakeProcessStdout()
        self.returncode: int | None = None

    def kill(self) -> None:
        """
        テスト用 process を終了する。

        Args:
            None

        Returns:
            None
        """
        self.returncode = -9

    async def wait(self) -> int:
        """
        テスト用 process の終了を待つ。

        Args:
            None

        Returns:
            int: 処理結果の整数値。
        """
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _descriptor() -> LiveSourceDescriptor:
    """
    _descriptor の処理を実行する。

    Args:
        None

    Returns:
        LiveSourceDescriptor: 処理結果。
    """
    return LiveSourceDescriptor(
        key = LiveSourceKey(
            backend = 'EDCB',
            network_id = 32736,
            transport_stream_id = 32736,
            service_id = 1024,
        ),
        channel_type = 'GR',
    )


def _other_descriptor() -> LiveSourceDescriptor:
    """
    _other_descriptor の処理を実行する。

    Args:
        None

    Returns:
        LiveSourceDescriptor: 処理結果。
    """
    return LiveSourceDescriptor(
        key = LiveSourceKey(
            backend = 'EDCB',
            network_id = 4,
            transport_stream_id = 16625,
            service_id = 101,
        ),
        channel_type = 'BS',
    )


def _generation_factory() -> Iterator[int]:
    """
    _generation_factory の処理を実行する。

    Args:
        None

    Returns:
        Iterator[int]: 処理結果。
    """
    yield from (101, 202, 303)


def test_same_key_concurrent_subscribe_starts_one_producer_and_fans_out_identical_bytes() -> None:
    """
    同時 A/B は producer を1回だけ起動し、以後の全 chunk を同一順序で受信する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """
        非同期検証本体を実行する。

        Args:
            None

        Returns:
            None
        """
        producers: list[_FakeProducer] = []

        async def factory(_descriptor: LiveSourceDescriptor, _generation: int) -> _FakeProducer:
            """
            テスト用 producer を生成する。

            Args:
                _descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
                _generation (int): _generation に指定する値。

            Returns:
                _FakeProducer: 処理結果。
            """
            producer = _FakeProducer()
            producers.append(producer)
            return producer

        coordinator = LiveSourceCoordinator(producer_factory=factory)
        active, preparing = await asyncio.gather(
            coordinator.acquire(_descriptor(), role='Active'),
            coordinator.acquire(_descriptor(), role='Preparing'),
        )
        assert len(producers) == 1
        assert active.generation_id == preparing.generation_id

        chunks = (b'\x47' * 188, b'\x47' * 376)
        active_reads = [asyncio.create_task(active.read()) for _ in chunks]
        preparing_reads = [asyncio.create_task(preparing.read()) for _ in chunks]
        await asyncio.sleep(0)
        for chunk in chunks:
            await producers[0].input.put(chunk)
            await asyncio.sleep(0)
        assert await asyncio.gather(*active_reads) == list(chunks)
        assert await asyncio.gather(*preparing_reads) == list(chunks)

        await active.close(keep_source_alive=False)
        await preparing.close(keep_source_alive=False)
        await coordinator.shutdown()

    asyncio.run(run())


def test_shutdown_returns_true_after_all_sessions_stop() -> None:
    """
    引数なし shutdown は既定 deadline で全 session を停止し True を返す。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """
        非同期検証本体を実行する。

        Args:
            None

        Returns:
            None
        """

        producer = _FakeProducer()

        async def factory(_descriptor: LiveSourceDescriptor, _generation: int) -> _FakeProducer:
            """
            テスト用 producer を生成する。

            Args:
                _descriptor (LiveSourceDescriptor): 共有 source 記述子。
                _generation (int): generation ID。

            Returns:
                _FakeProducer: テスト用 producer。
            """

            return producer

        coordinator = LiveSourceCoordinator(producer_factory=factory)
        await coordinator.acquire(_descriptor(), role='Active')

        assert await coordinator.shutdown() is True
        assert producer.close_count == 1
        assert await coordinator.activeSessionCount() == 0

    asyncio.run(run())


def test_last_subscription_close_propagates_producer_cleanup_failure() -> None:
    """
    最後の購読解除は producer cleanup 失敗を成功扱いせず、同じ結果を再送する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """非同期検証本体を実行する。"""

        producer = _FailingCloseProducer()

        async def factory(
            _descriptor: LiveSourceDescriptor,
            _generation: int,
        ) -> _FailingCloseProducer:
            """cleanup 失敗 producer を返す。"""

            return producer

        coordinator = LiveSourceCoordinator(producer_factory=factory)
        subscription = await coordinator.acquire(_descriptor(), role='Active')

        with pytest.raises(RuntimeError, match='producer cleanup failed'):
            await subscription.close(keep_source_alive=False)
        with pytest.raises(RuntimeError, match='producer cleanup failed'):
            await subscription.close(keep_source_alive=False)

        assert producer.close_count == 1
        assert await coordinator.activeSessionCount() == 0

    asyncio.run(run())


def test_terminal_subscription_close_replays_source_cleanup_failure() -> None:
    """
    EOF を受け取った購読も、source task の cleanup 失敗を close barrier で受け取る。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """非同期検証本体を実行する。"""

        producer = _FailingCloseProducer()

        async def factory(
            _descriptor: LiveSourceDescriptor,
            _generation: int,
        ) -> _FailingCloseProducer:
            """cleanup 失敗 producer を返す。"""

            return producer

        coordinator = LiveSourceCoordinator(producer_factory=factory)
        subscription = await coordinator.acquire(_descriptor(), role='Active')
        terminal_read = asyncio.create_task(subscription.read())
        await asyncio.sleep(0)
        await producer.input.put(None)

        assert await terminal_read == b''
        with pytest.raises(RuntimeError, match='producer cleanup failed'):
            await subscription.close(keep_source_alive=False)

        assert producer.close_count == 1
        assert await coordinator.activeSessionCount() == 0

    asyncio.run(run())


def test_shutdown_reports_cleanup_failure_and_stops_other_sessions() -> None:
    """
    1 source の cleanup 失敗で shutdown を成功扱いせず、他 source は最後まで停止する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """非同期検証本体を実行する。"""

        failing_producer = _FailingCloseProducer()
        successful_producer = _FakeProducer()

        async def factory(
            descriptor: LiveSourceDescriptor,
            _generation: int,
        ) -> _FakeProducer:
            """source key ごとの producer を返す。"""

            if descriptor.key == _descriptor().key:
                return failing_producer
            return successful_producer

        coordinator = LiveSourceCoordinator(producer_factory=factory)
        first = await coordinator.acquire(_descriptor(), role='Active')
        second = await coordinator.acquire(_other_descriptor(), role='Active')

        assert await coordinator.shutdown(timeout_seconds=1.0) is False
        assert failing_producer.close_count == 1
        assert successful_producer.close_count == 1
        assert await coordinator.activeSessionCount() == 0

        # shutdown が subscription を terminal 化していることも確認する。
        assert await first.read() == b''
        assert await second.read() == b''

    asyncio.run(run())


def test_shutdown_waits_for_starting_producer_and_closes_it() -> None:
    """
    producer factory 中の shutdown でも、生成直後の producer を残さず終了確認する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """非同期検証本体を実行する。"""

        producer = _FakeProducer()
        factory_started = asyncio.Event()
        release_factory = asyncio.Event()

        async def factory(
            _descriptor: LiveSourceDescriptor,
            _generation: int,
        ) -> _FakeProducer:
            """shutdown と競合させるため、明示解放まで producer 生成を保留する。"""

            factory_started.set()
            await release_factory.wait()
            return producer

        coordinator = LiveSourceCoordinator(producer_factory=factory)
        acquire_task = asyncio.create_task(
            coordinator.acquire(_descriptor(), role='Active'),
        )
        await factory_started.wait()
        shutdown_task = asyncio.create_task(coordinator.shutdown(timeout_seconds=1.0))
        session = cast(Any, coordinator)._sessions[_descriptor().key]
        while session.is_closed is False:
            await asyncio.sleep(0)
        release_factory.set()

        with pytest.raises(LiveSourceError, match='live_source_coordinator_shutting_down'):
            await acquire_task
        assert await shutdown_task is True
        assert producer.close_count == 1
        assert await coordinator.activeSessionCount() == 0

    asyncio.run(run())


def test_shutdown_returns_false_by_deadline_when_session_stop_ignores_cancellation() -> None:
    """
    producer close が cancel を飲み込んでも全体 deadline で False を返して registry を空にする。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """
        非同期検証本体を実行する。

        Args:
            None

        Returns:
            None
        """

        producer = _CancellationResistantCloseProducer()

        async def factory(
            _descriptor: LiveSourceDescriptor,
            _generation: int,
        ) -> _CancellationResistantCloseProducer:
            """
            cancel 耐性を持つテスト用 producer を生成する。

            Args:
                _descriptor (LiveSourceDescriptor): 共有 source 記述子。
                _generation (int): generation ID。

            Returns:
                _CancellationResistantCloseProducer: テスト用 producer。
            """

            return producer

        coordinator = LiveSourceCoordinator(producer_factory=factory)
        subscription = await coordinator.acquire(_descriptor(), role='Active')
        session = cast(Any, subscription)._session
        producer_task = session._producer_task

        started_at = time.monotonic()
        assert await coordinator.shutdown(timeout_seconds=0.05) is False
        elapsed_seconds = time.monotonic() - started_at

        assert elapsed_seconds < 0.2
        assert producer.close_started.is_set() is True
        assert await coordinator.activeSessionCount() == 0
        assert cast(Any, coordinator)._sessions == {}

        # close task は停止要求側の cancel から shield され、deadline 後も実資源解放を継続する。
        assert producer.close_cancellation_count == 0
        # asyncio.run() の loop 終了まで固着 task を持ち越さないよう、テスト用 close を解放する。
        producer.release_close.set()
        async with asyncio.timeout(1):
            await producer.close_finished.wait()
            if producer_task is not None:
                await asyncio.gather(producer_task, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(run())


def test_shutdown_timeout_result_is_sticky_and_lingering_stop_task_is_retained() -> None:
    """
    deadline 超過後の False を固定し、cancel 耐性 stop task を完了まで強参照する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """非同期検証本体を実行する。"""

        session = _CancellationResistantShutdownSession()
        coordinator = LiveSourceCoordinator()
        cast(Any, coordinator)._sessions = {
            _descriptor().key: session,
        }

        assert await coordinator.shutdown(timeout_seconds=0.05) is False
        assert session.stop_started.is_set() is True
        assert session.stop_cancellation_count == 0
        assert session.stop_count == 1

        # shutdown task の False は sticky で、短い deadline の再試行や registry 空化で
        # 成功へ反転しない。期限超過中の stop task も coordinator が強参照し続ける。
        shutdown_task = cast(Any, coordinator)._shutdown_task
        assert shutdown_task is not None
        assert shutdown_task.done() is True
        assert len(cast(Any, coordinator)._shutdown_stop_tasks) == 1
        assert await coordinator.shutdown(timeout_seconds=1.0) is False
        assert cast(Any, coordinator)._shutdown_task is shutdown_task
        assert session.stop_count == 1

        session.release_stop.set()
        async with asyncio.timeout(1):
            await session.stop_finished.wait()
            while cast(Any, coordinator)._shutdown_stop_tasks:
                await asyncio.sleep(0)

        # 実 stop が後から完了しても、最初の deadline 超過結果は成功へ反転しない。
        assert await coordinator.shutdown(timeout_seconds=1.0) is False
        assert session.stop_count == 1

    asyncio.run(run())


def test_shutdown_isolates_session_stop_exceptions_and_stops_every_session() -> None:
    """
    1 session の stop 例外で他 session を止めず、全停止未確認として False を返す。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """
        非同期検証本体を実行する。

        Args:
            None

        Returns:
            None
        """

        failing_session = _FakeShutdownSession(failure=RuntimeError('stop failed'))
        successful_session = _FakeShutdownSession()
        coordinator = LiveSourceCoordinator()
        cast(Any, coordinator)._sessions = {
            _descriptor().key: failing_session,
            _other_descriptor().key: successful_session,
        }

        assert await coordinator.shutdown(timeout_seconds=1.0) is False
        assert failing_session.stop_count == 1
        assert successful_session.stop_count == 1
        assert await coordinator.activeSessionCount() == 0

    asyncio.run(run())


def test_stop_and_preempt_signal_subscribers_when_producer_task_is_cancelled_before_start() -> None:
    """
    producer coroutine 開始前の cancel でも stop/preempt の terminal signal を失わない。

    Args:
        None

    Returns:
        None
    """

    async def run_stop() -> None:
        """stop の開始前 cancel を検証する。"""

        producer = _FakeProducer()

        async def factory(
            _descriptor: LiveSourceDescriptor,
            _generation: int,
        ) -> _FakeProducer:
            """開始前 cancel 対象の producer を返す。"""

            return producer

        coordinator = LiveSourceCoordinator(producer_factory=factory)
        subscription = await coordinator.acquire(_descriptor(), role='Active')
        session = cast(Any, subscription)._session
        assert session._producer_run_started is False

        await session.stop()

        async with asyncio.timeout(1):
            assert await subscription.read() == b''
        assert subscription.is_closed is True
        assert producer.close_count == 1
        assert await coordinator.activeSessionCount() == 0

    async def run_preempt() -> None:
        """preempt の開始前 cancel を検証する。"""

        producer = _FakeProducer()

        async def factory(
            _descriptor: LiveSourceDescriptor,
            _generation: int,
        ) -> _FakeProducer:
            """開始前 cancel 対象の producer を返す。"""

            return producer

        coordinator = LiveSourceCoordinator(producer_factory=factory)
        subscription = await coordinator.acquire(
            _descriptor(),
            role='Active',
            is_in_use=lambda: False,
        )
        session = cast(Any, subscription)._session
        assert session._producer_run_started is False

        assert await session.preemptIfNoViewers() is True

        async with asyncio.timeout(1):
            with pytest.raises(
                LiveSourcePreemptedError,
                match='live_source_preempted_no_viewers',
            ):
                await subscription.read()
        assert subscription.failure_reason == 'live_source_preempted_no_viewers'
        assert producer.close_count == 1
        assert await coordinator.activeSessionCount() == 0

    asyncio.run(run_stop())
    asyncio.run(run_preempt())


def test_slow_subscriber_fails_without_dropping_fast_subscriber_or_stopping_source() -> None:
    """
    満杯 queue は遅い B だけを切断し、A と producer は継続する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """
        非同期検証本体を実行する。

        Args:
            None

        Returns:
            None
        """
        producer = _FakeProducer()

        async def factory(_descriptor: LiveSourceDescriptor, _generation: int) -> _FakeProducer:
            """
            テスト用 producer を生成する。

            Args:
                _descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
                _generation (int): _generation に指定する値。

            Returns:
                _FakeProducer: 処理結果。
            """
            return producer

        coordinator = LiveSourceCoordinator(
            queue_size = 1,
            producer_factory = factory,
        )
        fast = await coordinator.acquire(_descriptor(), role='Active')
        slow = await coordinator.acquire(_descriptor(), role='Preparing')

        # A/Bとも最初のread()でlive edge購読を有効化する。
        first_fast = asyncio.create_task(fast.read())
        first_slow = asyncio.create_task(slow.read())
        await asyncio.sleep(0)
        await producer.input.put(b'\x47' * 188)
        assert await first_fast == b'\x47' * 188
        assert await first_slow == b'\x47' * 188

        # 以後Bだけを読まないまま上限を超えさせる。満杯になるまではBへ残す。
        second_fast = asyncio.create_task(fast.read())
        await producer.input.put(b'\x47' * 376)
        assert await second_fast == b'\x47' * 376
        third_fast = asyncio.create_task(fast.read())
        await producer.input.put(b'\x47' * 564)
        assert await third_fast == b'\x47' * 564

        with pytest.raises(LiveSourceSubscriberError, match='live_source_subscriber_too_slow'):
            await slow.read()
        assert producer.close_count == 0

        fourth_fast = asyncio.create_task(fast.read())
        await producer.input.put(b'\x47' * 752)
        assert await fourth_fast == b'\x47' * 752
        await fast.close(keep_source_alive=False)

    asyncio.run(run())


def test_default_queue_absorbs_temporary_preparing_stall_without_harming_active() -> None:
    """
    B起動中の一時停止は吸収し、追いついた後もA/Bへ同じchunkを配信する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """
        非同期検証本体を実行する。

        Args:
            None

        Returns:
            None
        """
        producer = _FakeProducer()

        async def factory(_descriptor: LiveSourceDescriptor, _generation: int) -> _FakeProducer:
            """
            テスト用 producer を生成する。

            Args:
                _descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
                _generation (int): _generation に指定する値。

            Returns:
                _FakeProducer: 処理結果。
            """
            return producer

        coordinator = LiveSourceCoordinator(producer_factory=factory)
        active = await coordinator.acquire(_descriptor(), role='Active')
        preparing = await coordinator.acquire(_descriptor(), role='Preparing')
        first_chunk = b'\x47' * 188
        active_read = asyncio.create_task(active.read())
        preparing_read = asyncio.create_task(preparing.read())
        await asyncio.sleep(0)
        await producer.input.put(first_chunk)
        assert await asyncio.gather(active_read, preparing_read) == [first_chunk, first_chunk]
        buffered_chunks = [
            bytes([index % 256]) * 188
            for index in range(LiveSourceCoordinator.DEFAULT_QUEUE_SIZE)
        ]

        # Bのencoder stdinだけが一時停止しても、独立queueを持つAは全chunkを継続して受信する。
        for chunk in buffered_chunks:
            active_read = asyncio.create_task(active.read())
            await producer.input.put(chunk)
            assert await active_read == chunk
        assert preparing.is_closed is False
        assert producer.close_count == 0

        # Bが消費を再開すれば欠落なく追いつき、その後のfanoutもA/Bとも継続する。
        assert [
            await preparing.read()
            for _ in buffered_chunks
        ] == buffered_chunks
        next_chunk = b'\x47' * 376
        active_read = asyncio.create_task(active.read())
        preparing_read = asyncio.create_task(preparing.read())
        await producer.input.put(next_chunk)
        assert await asyncio.gather(active_read, preparing_read) == [next_chunk, next_chunk]

        await active.close(keep_source_alive=False)
        await preparing.close(keep_source_alive=False)

    asyncio.run(run())


def test_chunks_before_first_read_are_dropped_and_subscription_starts_at_live_edge() -> None:
    """
    read開始前の過去chunkは積まず、最初のread以後だけを購読する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """
        非同期検証本体を実行する。

        Args:
            None

        Returns:
            None
        """
        producer = _FakeProducer()

        async def factory(_descriptor: LiveSourceDescriptor, _generation: int) -> _FakeProducer:
            """
            テスト用 producer を生成する。

            Args:
                _descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
                _generation (int): _generation に指定する値。

            Returns:
                _FakeProducer: 処理結果。
            """
            return producer

        coordinator = LiveSourceCoordinator(
            queue_size = 1,
            producer_factory = factory,
        )
        preparing = await coordinator.acquire(_descriptor(), role='Preparing')

        # acquire直後にproducerが先行しても、未開始Bをslow扱いせず過去chunkを捨てる。
        for length in (188, 376, 564):
            await producer.input.put(b'\x47' * length)
            while producer.input.empty() is False:
                await asyncio.sleep(0)
            await asyncio.sleep(0)
        assert preparing.is_closed is False
        assert preparing.failure_reason is None

        # 最初のread()を開始した時点のlive edgeから、次のchunkを受け取る。
        live_edge_read = asyncio.create_task(preparing.read())
        await asyncio.sleep(0)
        live_edge_chunk = b'\x47' * 752
        await producer.input.put(live_edge_chunk)
        assert await live_edge_read == live_edge_chunk
        await preparing.close(keep_source_alive=False)

    asyncio.run(run())


def test_first_read_race_with_broadcast_neither_hangs_nor_duplicates_chunk() -> None:
    """
    最初のreadとbroadcastの競合はlock順にlive edgeを確定する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """
        非同期検証本体を実行する。

        Args:
            None

        Returns:
            None
        """
        producer = _FakeProducer()

        async def factory(_descriptor: LiveSourceDescriptor, _generation: int) -> _FakeProducer:
            """
            テスト用 producer を生成する。

            Args:
                _descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
                _generation (int): _generation に指定する値。

            Returns:
                _FakeProducer: 処理結果。
            """
            return producer

        coordinator = LiveSourceCoordinator(
            queue_size = 1,
            producer_factory = factory,
        )
        preparing = await coordinator.acquire(_descriptor(), role='Preparing')
        session = cast(Any, preparing)._session

        # activate()とbroadcast()を同じlockで待たせ、解除後の取得順がどちらでも
        # 最初のreadが有限時間内にlive edgeを1件だけ受け取ることを確認する。
        await session._lock.acquire()
        try:
            first_read = asyncio.create_task(preparing.read())
            await producer.input.put(b'\x47' * 188)
            await asyncio.sleep(0)
        finally:
            session._lock.release()
        await asyncio.sleep(0)
        if first_read.done() is False:
            await producer.input.put(b'\x47' * 376)
        assert await asyncio.wait_for(first_read, timeout=1) in (
            b'\x47' * 188,
            b'\x47' * 376,
        )
        assert preparing.is_closed is False
        await preparing.close(keep_source_alive=False)

    asyncio.run(run())


def test_resubscribe_during_idle_grace_keeps_generation_and_producer() -> None:
    """
    encoder restart の一時 subscriber 0では source epoch を維持する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """
        非同期検証本体を実行する。

        Args:
            None

        Returns:
            None
        """
        producers: list[_FakeProducer] = []
        generations = _generation_factory()

        async def factory(_descriptor: LiveSourceDescriptor, _generation: int) -> _FakeProducer:
            """
            テスト用 producer を生成する。

            Args:
                _descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
                _generation (int): _generation に指定する値。

            Returns:
                _FakeProducer: 処理結果。
            """
            producer = _FakeProducer()
            producers.append(producer)
            return producer

        coordinator = LiveSourceCoordinator(
            idle_grace_seconds = 0.1,
            generation_factory = lambda: next(generations),
            producer_factory = factory,
        )
        first = await coordinator.acquire(_descriptor(), role='Active')
        await first.close()
        await asyncio.sleep(0.02)
        restarted = await coordinator.acquire(_descriptor(), role='Active')

        assert first.generation_id == restarted.generation_id == 101
        assert len(producers) == 1
        assert producers[0].close_count == 0
        await restarted.close(keep_source_alive=False)

    asyncio.run(run())


def test_idle_grace_cleanup_and_next_acquire_create_new_epoch() -> None:
    """
    grace 満了後は producer を閉じ、再取得で別 generation を発行する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """
        非同期検証本体を実行する。

        Args:
            None

        Returns:
            None
        """
        producers: list[_FakeProducer] = []
        generations = _generation_factory()

        async def factory(_descriptor: LiveSourceDescriptor, _generation: int) -> _FakeProducer:
            """
            テスト用 producer を生成する。

            Args:
                _descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
                _generation (int): _generation に指定する値。

            Returns:
                _FakeProducer: 処理結果。
            """
            producer = _FakeProducer()
            producers.append(producer)
            return producer

        coordinator = LiveSourceCoordinator(
            idle_grace_seconds = 0.01,
            generation_factory = lambda: next(generations),
            producer_factory = factory,
        )
        first = await coordinator.acquire(_descriptor(), role='Active')
        await first.close()
        await asyncio.sleep(0.04)
        assert producers[0].close_count == 1
        assert await coordinator.activeSessionCount() == 0

        second = await coordinator.acquire(_descriptor(), role='Active')
        assert second.generation_id == 202
        assert len(producers) == 2
        await second.close(keep_source_alive=False)

    asyncio.run(run())


def test_same_key_replacement_waits_for_cleanup_and_concurrent_acquire_uses_one_generation() -> None:
    """
    closed session の cleanup 完了前は同一 key を再起動せず、同時再取得を1世代へ束ねる。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """非同期検証本体を実行する。"""

        producers: list[_FakeProducer] = []
        generations = _generation_factory()

        async def factory(
            _descriptor: LiveSourceDescriptor,
            _generation: int,
        ) -> _FakeProducer:
            """最初は cleanup 保留、replacement は通常 producer を返す。"""

            producer: _FakeProducer
            if producers:
                producer = _FakeProducer()
            else:
                producer = _CancellationResistantCloseProducer()
            producers.append(producer)
            return producer

        coordinator = LiveSourceCoordinator(
            generation_factory=lambda: next(generations),
            producer_factory=factory,
        )
        old_subscription = await coordinator.acquire(_descriptor(), role='Active')
        old_producer = cast(_CancellationResistantCloseProducer, producers[0])
        terminal_read = asyncio.create_task(old_subscription.read())
        await asyncio.sleep(0)
        await old_producer.input.put(None)
        assert await terminal_read == b''
        async with asyncio.timeout(1):
            await old_producer.close_started.wait()

        # 同時 acquire は旧 epoch の cleanup barrier で停止し、この時点では新 producer を作らない。
        first_acquire = asyncio.create_task(
            coordinator.acquire(_descriptor(), role='Active'),
        )
        second_acquire = asyncio.create_task(
            coordinator.acquire(_descriptor(), role='Active'),
        )
        await asyncio.sleep(0.02)
        assert first_acquire.done() is False
        assert second_acquire.done() is False
        assert len(producers) == 1

        old_producer.release_close.set()
        first, second = await asyncio.gather(first_acquire, second_acquire)

        assert old_producer.close_finished.is_set() is True
        assert len(producers) == 2
        assert first.generation_id == second.generation_id == 202
        assert cast(Any, first)._session is cast(Any, second)._session
        assert await coordinator.activeSessionCount() == 1

        await first.close(keep_source_alive=False)
        await second.close(keep_source_alive=False)

    asyncio.run(run())


def test_producer_failure_is_propagated_to_every_subscriber() -> None:
    """
    backend/tsreadex の例外は A/B 双方へ同じ source failure として届く。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """
        非同期検証本体を実行する。

        Args:
            None

        Returns:
            None
        """
        producers: list[_FakeProducer] = []
        generations = _generation_factory()

        async def factory(_descriptor: LiveSourceDescriptor, _generation: int) -> _FakeProducer:
            """
            テスト用 producer を生成する。

            Args:
                _descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
                _generation (int): _generation に指定する値。

            Returns:
                _FakeProducer: 処理結果。
            """
            producer = _FakeProducer()
            producers.append(producer)
            return producer

        coordinator = LiveSourceCoordinator(
            generation_factory = lambda: next(generations),
            producer_factory = factory,
        )
        active = await coordinator.acquire(_descriptor(), role='Active')
        preparing = await coordinator.acquire(_descriptor(), role='Preparing')
        active_read = asyncio.create_task(active.read())
        preparing_read = asyncio.create_task(preparing.read())
        await producers[0].input.put(RuntimeError('synthetic producer failure'))

        results = await asyncio.gather(active_read, preparing_read, return_exceptions=True)
        assert all(isinstance(result, LiveSourceError) for result in results)
        assert all('synthetic producer failure' in str(result) for result in results)
        # terminal signal と producer cleanup は別工程。close barrier で後者まで確認する。
        await asyncio.gather(active.close(), preparing.close())
        assert producers[0].close_count == 1
        assert await coordinator.activeSessionCount() == 0

        restarted = await coordinator.acquire(_descriptor(), role='Active')
        assert restarted.generation_id == 202
        assert len(producers) == 2
        await restarted.close(keep_source_alive=False)

    asyncio.run(run())


def test_source_allows_multiple_active_but_only_one_preparing_pipeline() -> None:
    """
    別viewerの Active は共存させ、二段階 Preparing だけを1本に固定する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """
        非同期検証本体を実行する。

        Args:
            None

        Returns:
            None
        """
        producer = _FakeProducer()

        async def factory(_descriptor: LiveSourceDescriptor, _generation: int) -> _FakeProducer:
            """
            テスト用 producer を生成する。

            Args:
                _descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
                _generation (int): _generation に指定する値。

            Returns:
                _FakeProducer: 処理結果。
            """
            return producer

        coordinator = LiveSourceCoordinator(producer_factory=factory)
        first_active = await coordinator.acquire(_descriptor(), role='Active')
        second_active = await coordinator.acquire(_descriptor(), role='Active')
        preparing = await coordinator.acquire(_descriptor(), role='Preparing')
        with pytest.raises(LiveSourceCapacityError, match='live_source_preparing_already_exists'):
            await coordinator.acquire(_descriptor(), role='Preparing')

        await first_active.close(keep_source_alive=False)
        await second_active.close(keep_source_alive=False)
        await preparing.close(keep_source_alive=False)

    asyncio.run(run())


def test_commit_promotes_preparing_and_allows_next_prepare_with_existing_active_viewers() -> None:
    """
    A→B Commit 後はBをActive化し、旧Aが別viewer向けに残っても次のC Prepareを許可する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """
        非同期検証本体を実行する。

        Args:
            None

        Returns:
            None
        """
        producer = _FakeProducer()

        async def factory(_descriptor: LiveSourceDescriptor, _generation: int) -> _FakeProducer:
            """
            テスト用 producer を生成する。

            Args:
                _descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
                _generation (int): _generation に指定する値。

            Returns:
                _FakeProducer: 処理結果。
            """
            return producer

        coordinator = LiveSourceCoordinator(producer_factory=factory)
        old_active = await coordinator.acquire(_descriptor(), role='Active')
        prepared_b = await coordinator.acquire(_descriptor(), role='Preparing')
        await prepared_b.promoteToActive()
        assert prepared_b.role == 'Active'

        prepared_c = await coordinator.acquire(_descriptor(), role='Preparing')
        assert prepared_c.role == 'Preparing'
        await old_active.close(keep_source_alive=False)
        await prepared_b.close(keep_source_alive=False)
        await prepared_c.close(keep_source_alive=False)

    asyncio.run(run())


def test_new_channel_preempts_only_old_source_with_zero_viewers() -> None:
    """
    別channel開始時は旧source全品質viewer 0だけを閉じ、viewerが残るsourceは維持する。

    Args:
        None

    Returns:
        None
    """

    async def run_zero_viewers() -> None:
        """
        run_zero_viewers の処理を実行する。

        Args:
            None

        Returns:
            None
        """
        producers: list[_FakeProducer] = []

        async def factory(_descriptor: LiveSourceDescriptor, _generation: int) -> _FakeProducer:
            """
            テスト用 producer を生成する。

            Args:
                _descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
                _generation (int): _generation に指定する値。

            Returns:
                _FakeProducer: 処理結果。
            """
            producer = _FakeProducer()
            producers.append(producer)
            return producer

        coordinator = LiveSourceCoordinator(producer_factory=factory)
        first = await coordinator.acquire(_descriptor(), role='Active', is_in_use=lambda: False)
        second = await coordinator.acquire(_descriptor(), role='Active', is_in_use=lambda: False)
        first_read = asyncio.create_task(first.read())
        second_read = asyncio.create_task(second.read())
        next_channel = await coordinator.acquire(_other_descriptor(), role='Active')
        preempted = await asyncio.gather(first_read, second_read, return_exceptions=True)
        assert producers[0].close_count == 1
        assert all(
            isinstance(result, LiveSourcePreemptedError)
            for result in preempted
        ), repr(preempted)
        assert first.failure_reason == 'live_source_preempted_no_viewers'
        assert second.failure_reason == 'live_source_preempted_no_viewers'
        assert len(producers) == 2
        assert await coordinator.activeSessionCount() == 1
        await next_channel.close(keep_source_alive=False)

    async def run_with_viewer() -> None:
        """
        run_with_viewer の処理を実行する。

        Args:
            None

        Returns:
            None
        """
        producers: list[_FakeProducer] = []

        async def factory(_descriptor: LiveSourceDescriptor, _generation: int) -> _FakeProducer:
            """
            テスト用 producer を生成する。

            Args:
                _descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
                _generation (int): _generation に指定する値。

            Returns:
                _FakeProducer: 処理結果。
            """
            producer = _FakeProducer()
            producers.append(producer)
            return producer

        coordinator = LiveSourceCoordinator(producer_factory=factory)
        viewed = await coordinator.acquire(_descriptor(), role='Active', is_in_use=lambda: True)
        next_channel = await coordinator.acquire(_other_descriptor(), role='Active')
        assert producers[0].close_count == 0
        assert await coordinator.activeSessionCount() == 2
        await viewed.close(keep_source_alive=False)
        await next_channel.close(keep_source_alive=False)

    asyncio.run(run_zero_viewers())
    asyncio.run(run_with_viewer())


def test_new_channel_does_not_start_when_old_source_cleanup_fails() -> None:
    """
    viewer 0 の旧 source を解放できない場合は、新 channel producer を開始しない。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """非同期検証本体を実行する。"""

        producers: list[_FakeProducer] = []

        async def factory(
            _descriptor: LiveSourceDescriptor,
            _generation: int,
        ) -> _FakeProducer:
            """最初だけ cleanup 失敗 producer を返す。"""

            producer: _FakeProducer
            if producers:
                producer = _FakeProducer()
            else:
                producer = _FailingCloseProducer()
            producers.append(producer)
            return producer

        coordinator = LiveSourceCoordinator(producer_factory=factory)
        old = await coordinator.acquire(
            _descriptor(),
            role='Active',
            is_in_use=lambda: False,
        )

        with pytest.raises(LiveSourceError, match='live_source_preempt_cleanup_failed'):
            await coordinator.acquire(_other_descriptor(), role='Active')

        # cleanup 未確認は coordinator 全体をプロセス再起動まで poison する。
        # 最初の acquire だけを拒否して次回に新 producer を起動してはならない。
        with pytest.raises(
            LiveSourceError,
            match='live_source_coordinator_cleanup_blocked',
        ):
            await coordinator.acquire(_other_descriptor(), role='Active')

        assert len(producers) == 1
        assert producers[0].close_count == 1
        assert await coordinator.activeSessionCount() == 0
        with pytest.raises(RuntimeError, match='producer cleanup failed'):
            await old.close()

    asyncio.run(run())


def test_new_viewer_entering_after_preempt_candidate_selection_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    旧 source の候補列挙後に入った viewer を原子的な再確認で保護する。

    Args:
        monkeypatch (pytest.MonkeyPatch): テスト対象の依存を差し替える fixture。

    Returns:
        None
    """

    async def run() -> None:
        """
        非同期検証本体を実行する。

        Args:
            None

        Returns:
            None
        """
        producers: list[_FakeProducer] = []

        async def factory(_descriptor: LiveSourceDescriptor, _generation: int) -> _FakeProducer:
            """
            テスト用 producer を生成する。

            Args:
                _descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
                _generation (int): _generation に指定する値。

            Returns:
                _FakeProducer: 処理結果。
            """
            producer = _FakeProducer()
            producers.append(producer)
            return producer

        coordinator = LiveSourceCoordinator(producer_factory=factory)
        old_without_viewer = await coordinator.acquire(
            _descriptor(),
            role='Active',
            is_in_use=lambda: False,
        )
        session = cast(Any, old_without_viewer)._session
        original_preempt = session.preemptIfNoViewers
        candidate_selected = asyncio.Event()
        continue_preempt = asyncio.Event()

        async def delayed_preempt() -> bool:
            """
            競合再現用に preempt を遅延実行する。

            Args:
                None

            Returns:
                bool: 判定結果。
            """
            candidate_selected.set()
            await continue_preempt.wait()
            return await original_preempt()

        monkeypatch.setattr(session, 'preemptIfNoViewers', delayed_preempt)
        next_channel_task = asyncio.create_task(
            coordinator.acquire(_other_descriptor(), role='Active'),
        )
        await candidate_selected.wait()

        late_viewer = await coordinator.acquire(
            _descriptor(),
            role='Active',
            is_in_use=lambda: True,
        )
        continue_preempt.set()
        next_channel = await next_channel_task

        assert producers[0].close_count == 0
        assert old_without_viewer.is_closed is False
        assert late_viewer.is_closed is False
        assert await coordinator.activeSessionCount() == 2

        await old_without_viewer.close(keep_source_alive=False)
        await late_viewer.close(keep_source_alive=False)
        await next_channel.close(keep_source_alive=False)

    asyncio.run(run())


def test_edcb_live_source_input_reports_false_unlock_and_close_after_disconnect() -> None:
    """
    EDCB input は disconnect 後も unlock/close を両方試し、False を cleanup 失敗として返す。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """非同期検証本体を実行する。"""

        source_module = importlib.import_module('app.streams.LiveSourceCoordinator')

        class FalseCleanupTuner:
            def __init__(self) -> None:
                """呼び出し順を保持する。"""

                self.calls: list[tuple[str, str]] = []

            async def disconnect(self, owner_id: str) -> None:
                """disconnect 呼び出しを記録する。"""

                self.calls.append(('disconnect', owner_id))

            def unlock(self, owner_id: str) -> bool:
                """unlock 呼び出しを記録し、未確認を返す。"""

                self.calls.append(('unlock', owner_id))
                return False

            async def close(self, owner_id: str) -> bool:
                """close 呼び出しを記録し、未確認を返す。"""

                self.calls.append(('close', owner_id))
                return False

        tuner = FalseCleanupTuner()
        source_input = source_module._EDCBLiveSourceInput(
            tuner,
            'test-owner',
            cast(Any, object()),
        )

        with pytest.raises(
            LiveSourceError,
            match='edcb_live_source_input_cleanup_failed',
        ) as error:
            await source_input.close()

        assert tuner.calls == [
            ('disconnect', 'test-owner'),
            ('unlock', 'test-owner'),
            ('close', 'test-owner'),
        ]
        cause = error.value.__cause__
        assert isinstance(cause, BaseExceptionGroup)
        assert {
            str(nested_error)
            for nested_error in cause.exceptions
        } == {
            'edcb_tuner_unlock_failed',
            'edcb_tuner_close_failed',
        }

    asyncio.run(run())


def test_edcb_startup_cleanup_failure_globally_blocks_next_acquire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    EDCB 起動途中の unlock/close 未確認で coordinator を poison し、次世代を開始しない。

    Args:
        monkeypatch (pytest.MonkeyPatch): テスト対象の依存を差し替える fixture。

    Returns:
        None
    """

    async def run() -> None:
        """非同期検証本体を実行する。"""

        source_module = importlib.import_module('app.streams.LiveSourceCoordinator')

        class StartupCleanupFailingTuner:
            def __init__(self) -> None:
                """起動・cleanup の呼び出しを保持する。"""

                self.calls: list[str] = []

            async def setChannel(
                self,
                _network_id: int,
                _service_id: int,
                _transport_stream_id: int,
                _owner_id: str,
            ) -> bool:
                """チャンネル設定成功を返す。"""

                self.calls.append('setChannel')
                return True

            def lock(self, _owner_id: str) -> bool:
                """排他取得成功を返す。"""

                self.calls.append('lock')
                return True

            async def connect(self, _owner_id: str) -> None:
                """接続失敗を表す None を返す。"""

                self.calls.append('connect')
                return None

            def unlock(self, _owner_id: str) -> bool:
                """unlock 未確認を返す。"""

                self.calls.append('unlock')
                return False

            async def close(self, _owner_id: str) -> bool:
                """close 未確認を返す。"""

                self.calls.append('close')
                return False

        tuner = StartupCleanupFailingTuner()
        tuner_factory_calls: list[str] = []
        generation_calls: list[int] = []

        def fake_get_or_create(owner_id: str) -> StartupCleanupFailingTuner:
            """EDCB tuner factory の呼び出し世代を記録する。"""

            tuner_factory_calls.append(owner_id)
            return tuner

        def generation_factory() -> int:
            """coordinator の source 世代発行回数を記録する。"""

            generation_id = 101 + len(generation_calls)
            generation_calls.append(generation_id)
            return generation_id

        monkeypatch.setattr(
            source_module.EDCBTuner,
            'getOrCreate',
            staticmethod(fake_get_or_create),
        )
        coordinator = LiveSourceCoordinator(generation_factory=generation_factory)

        with pytest.raises(
            LiveSourceError,
            match='edcb_tuner_startup_cleanup_failed',
        ):
            await coordinator.acquire(_descriptor(), role='Active')

        with pytest.raises(
            LiveSourceError,
            match='live_source_coordinator_cleanup_blocked',
        ):
            await coordinator.acquire(_descriptor(), role='Active')

        assert tuner.calls == [
            'setChannel',
            'lock',
            'connect',
            'unlock',
            'close',
        ]
        assert len(tuner_factory_calls) == 1
        assert generation_calls == [101]

    asyncio.run(run())


def test_debug_ts_file_uses_common_raw_feeder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    debug TS も last input・PSI・geometry を更新する共通 raw feeder を通す。

    Args:
        tmp_path (Path): tmp_path に指定する値。
        monkeypatch (pytest.MonkeyPatch): テスト対象の依存を差し替える fixture。

    Returns:
        None
    """

    async def run() -> None:
        """
        非同期検証本体を実行する。

        Args:
            None

        Returns:
            None
        """
        source_module = importlib.import_module('app.streams.LiveSourceCoordinator')
        process = _FakeSubprocess()
        process_args: tuple[object, ...] | None = None
        raw_chunks: list[bytes] = []
        geometry_chunks: list[bytes] = []

        class FakeArchiver:
            def __init__(self, _service_id: int) -> None:
                """
                利用する状態を初期化する。

                Args:
                    _service_id (int): _service_id に指定する値。

                Returns:
                    None
                """
                pass

            async def pushTSPacketData(self, chunk: bytes) -> None:
                """
                pushTSPacketData の処理を実行する。

                Args:
                    chunk (bytes): 処理対象の TS chunk。

                Returns:
                    None
                """
                raw_chunks.append(chunk)

            async def destroy(self) -> None:
                """
                destroy の処理を実行する。

                Args:
                    None

                Returns:
                    None
                """
                pass

        class FakeAspectMonitor:
            def push(self, chunk: bytes) -> None:
                """
                push の処理を実行する。

                Args:
                    chunk (bytes): 処理対象の TS chunk。

                Returns:
                    None
                """
                geometry_chunks.append(chunk)

        async def fake_create_subprocess_exec(
            *args: object,
            **_kwargs: object,
        ) -> _FakeSubprocess:
            """
            テスト用 subprocess を生成する。

            Args:
                *args (object): *args に指定する値。
                **_kwargs (object): **_kwargs に指定する値。

            Returns:
                _FakeSubprocess: 処理結果。
            """
            nonlocal process_args
            process_args = args
            return process

        monkeypatch.setattr(asyncio, 'create_subprocess_exec', fake_create_subprocess_exec)
        monkeypatch.setattr(source_module, 'LivePSIDataArchiver', FakeArchiver)
        monkeypatch.setattr(source_module, 'LiveSourceAspectMonitor', FakeAspectMonitor)

        debug_ts_path = tmp_path / 'debug.ts'
        source_data = b'\x47' * (188 * 258)
        debug_ts_path.write_bytes(source_data)
        descriptor = LiveSourceDescriptor(
            key = _descriptor().key,
            channel_type = 'GR',
            debug_mode_ts_path = debug_ts_path,
        )
        started_at = time.monotonic()
        producer = await createTSReadExLiveSourceProducer(descriptor, 101)
        async with asyncio.timeout(1):
            while process.stdin.closed is False:
                await asyncio.sleep(0)
        await producer.close()

        assert process_args is not None
        assert process_args[-1] == '-'
        assert str(debug_ts_path) not in process_args
        assert '-l' not in process_args
        assert b''.join(process.stdin.chunks) == source_data
        assert producer.last_input_at >= started_at
        assert b''.join(raw_chunks) == source_data
        assert b''.join(geometry_chunks) == source_data

    asyncio.run(run())


def test_tsreadex_producer_close_continues_after_backend_failure_and_replays_error() -> None:
    """
    backend close 失敗後も process・archiverを回収し、同じ cleanup 失敗を再送する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """非同期検証本体を実行する。"""

        source_module = importlib.import_module('app.streams.LiveSourceCoordinator')
        process = _FakeSubprocess()

        class FailingSourceInput:
            close_count = 0

            async def readexactly(self, size: int) -> bytes:
                """このテストでは raw input を読み取らない。"""

                raise asyncio.IncompleteReadError(b'', size)

            async def close(self) -> None:
                """呼び出しを記録し、backend cleanup 失敗を送出する。"""

                self.close_count += 1
                raise RuntimeError('backend close failed')

        class FakeArchiver:
            destroy_count = 0

            async def destroy(self) -> None:
                """archiver cleanup 呼び出しを記録する。"""

                self.destroy_count += 1

        source_input = FailingSourceInput()
        producer = source_module._TSReadExLiveSourceProducer(
            _descriptor(),
            101,
            process,
            None,
        )
        cast(Any, producer)._source_input = source_input
        archiver = FakeArchiver()
        cast(Any, producer)._psi_data_archiver = archiver

        for _ in range(2):
            with pytest.raises(
                LiveSourceError,
                match='live_source_cleanup_failed:backend_input_close',
            ):
                await producer.close()

        assert source_input.close_count == 1
        assert process.stdin.closed is True
        assert process.returncode == -9
        assert archiver.destroy_count == 1

    asyncio.run(run())


def test_tsreadex_cleanup_step_timeout_does_not_block_later_cleanup() -> None:
    """
    cancel 耐性 backend close が期限超過しても、process/archiver を回収して短時間で失敗確定する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """非同期検証本体を実行する。"""

        source_module = importlib.import_module('app.streams.LiveSourceCoordinator')
        process = _FakeSubprocess()
        source_input = _CancellationResistantCloseProducer()

        class FakeArchiver:
            def __init__(self) -> None:
                """cleanup 呼び出し回数を保持する。"""

                self.destroy_count = 0

            async def destroy(self) -> None:
                """archiver cleanup 呼び出しを記録する。"""

                self.destroy_count += 1

        producer = source_module._TSReadExLiveSourceProducer(
            _descriptor(),
            101,
            process,
            None,
        )
        cast(Any, producer)._source_input = source_input
        archiver = FakeArchiver()
        cast(Any, producer)._psi_data_archiver = archiver
        cast(Any, producer).CLEANUP_STEP_TIMEOUT_SECONDS = 0.01
        cast(Any, producer).CLEANUP_REAP_TIMEOUT_SECONDS = 0.01

        started_at = time.monotonic()
        async with asyncio.timeout(0.3):
            with pytest.raises(
                LiveSourceError,
                match='live_source_cleanup_failed:backend_input_close',
            ):
                await producer.close()
        elapsed_seconds = time.monotonic() - started_at

        assert elapsed_seconds < 0.2
        assert source_input.close_started.is_set() is True
        assert source_input.close_cancellation_count >= 1
        assert source_input.close_finished.is_set() is False
        assert process.stdin.closed is True
        assert process.returncode == -9
        assert archiver.destroy_count == 1
        detached_tasks = tuple(cast(Any, producer)._detached_cleanup_tasks)
        assert len(detached_tasks) == 1

        # asyncio.run() 終了時に cancellation-resistant task を残さないよう明示解放する。
        source_input.release_close.set()
        async with asyncio.timeout(1):
            await source_input.close_finished.wait()
            await asyncio.gather(*detached_tasks, return_exceptions=True)
            while cast(Any, producer)._detached_cleanup_tasks:
                await asyncio.sleep(0)

        # close の失敗結果は sticky で、後から backend close が完了しても成功へ反転しない。
        with pytest.raises(
            LiveSourceError,
            match='live_source_cleanup_failed:backend_input_close',
        ):
            await producer.close()
        assert source_input.close_count == 1
        assert archiver.destroy_count == 1

    asyncio.run(run())
