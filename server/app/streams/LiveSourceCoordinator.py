"""
同一 channel/service source epoch の放送波入力と tsreadex を共有する。

品質別エンコーダーは LiveSourceSubscription を通じて、共有 tsreadex が出力した同一の
source marker 列を受け取る。1つの遅い subscriber が他方や source producer を停止させないよう、
queue が満杯になった subscriber だけを明示的な失敗として切断する。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

import aiohttp

from app import logging
from app.constants import API_REQUEST_HEADERS, LIBRARY_PATH
from app.streams.LivePSIDataArchiver import LivePSIDataArchiver
from app.streams.LiveSourceAspectMonitor import (
    LiveSourceAspectMonitor,
    LiveSourceVideoGeometry,
)
from app.utils import GetMirakurunAPIEndpointURL
from app.utils.edcb.EDCBTuner import EDCBTuner
from app.utils.edcb.PipeStreamReader import PipeStreamReader


TS_PACKET_SIZE = 188
SOURCE_CHUNK_SIZE = TS_PACKET_SIZE * 256

LiveSourceBackend = Literal['EDCB', 'Mirakurun']
LiveSourceSubscriberRole = Literal['Active', 'Preparing']


def _consumeShutdownTaskResult(task: asyncio.Task[None]) -> None:
    """
    shutdown の deadline 後に完了した task の結果を回収する。

    Args:
        task (asyncio.Task[None]): coordinator から切り離した session 停止 task。

    Returns:
        None
    """

    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as ex:
        logging.warning(
            '[LiveSourceCoordinator] A detached source session failed during shutdown.',
            exc_info = ex,
        )


@dataclass(frozen=True, slots=True)
class LiveSourceKey:
    """同じ放送波 source epoch を共有できる入力の識別子。"""

    backend: LiveSourceBackend
    network_id: int
    transport_stream_id: int
    service_id: int

    def asLogString(self) -> str:
        """
        認証情報を含まないログ用文字列を返す。

        Args:
            None

        Returns:
            str: 処理結果の文字列。
        """

        return (
            f'{self.backend}:NID={self.network_id}:'
            f'TSID={self.transport_stream_id}:SID={self.service_id}'
        )


@dataclass(frozen=True, slots=True)
class LiveSourceDescriptor:
    """共有 source producer の起動に必要な不変情報。"""

    key: LiveSourceKey
    channel_type: str
    debug_mode_ts_path: Path | None = None
    startup_discard_seconds: float = 0.0


class LiveSourceError(RuntimeError):
    """共有 source の開始・読み取り・終了に失敗した場合の基底例外。"""


class LiveSourceCleanupError(LiveSourceError):
    """source 起動途中を含む資源解放を確認できず、新しい source を開始してはいけない。"""


class LiveSourceCapacityError(LiveSourceError):
    """同一 source に既に Preparing パイプラインが存在する。"""


class LiveSourceSubscriberError(LiveSourceError):
    """単一 subscriber だけを切断すべき失敗。"""


class LiveSourcePreemptedError(LiveSourceError):
    """viewer 0 の旧 source が新しい channel のため明示停止された。"""


class LiveSourceProducer(Protocol):
    """共有 tsreadex 出力を生成する producer の最小契約。"""

    @property
    def psi_data_archiver(self) -> LivePSIDataArchiver | None:
        """
        producer が所有する PSI/SI archiver。

        Args:
            None

        Returns:
            LivePSIDataArchiver | None: producer が共有する PSI/SI archiver。
        """

        ...

    @property
    def source_geometry(self) -> LiveSourceVideoGeometry | None:
        """
        現在検出済みの放送波 geometry。

        Args:
            None

        Returns:
            LiveSourceVideoGeometry | None: 検出できた場合の入力映像 geometry。
        """

        ...

    @property
    def last_input_at(self) -> float:
        """
        backend から最後に raw TS を受信した単調増加時刻。

        Args:
            None

        Returns:
            float: 処理結果の数値。
        """

        ...

    async def read(self) -> bytes:
        """
        共有 tsreadex の次 chunk を返す。

        Args:
            None

        Returns:
            bytes: 処理結果のバイト列。
        """

        ...

    async def close(self) -> None:
        """
        producer が所有する全資源を冪等に解放する。

        Args:
            None

        Returns:
            None
        """

        ...


LiveSourceProducerFactory = Callable[
    [LiveSourceDescriptor, int],
    Awaitable[LiveSourceProducer],
]


@dataclass(frozen=True, slots=True)
class _SubscriberFailure:
    reason: str


@dataclass(frozen=True, slots=True)
class _SourceFailure:
    reason: str


@dataclass(frozen=True, slots=True)
class _SourceEnd:
    pass


@dataclass(frozen=True, slots=True)
class _SourcePreempted:
    reason: str


_QueueItem = bytes | _SubscriberFailure | _SourceFailure | _SourcePreempted | _SourceEnd


class LiveSourceSubscription:
    """単一品質エンコーダーが共有 source を読むための購読。"""

    def __init__(
        self,
        session: _LiveSourceSession,
        subscriber_id: int,
        role: LiveSourceSubscriberRole,
        queue: asyncio.Queue[_QueueItem],
        is_in_use: Callable[[], bool],
    ) -> None:
        """
        利用する状態を初期化する。

        Args:
            session (_LiveSourceSession): 共有 source session または HTTP client session。
            subscriber_id (int): 対象 subscriber を識別する session 内 ID。
            role (LiveSourceSubscriberRole): 共有 source 上での購読 role。
            queue (asyncio.Queue[_QueueItem]): この subscriber 専用の未読 TS queue。
            is_in_use (Callable[[], bool]): 購読元に viewer が残るか判定する callback。

        Returns:
            None
        """
        self._session = session
        self._subscriber_id = subscriber_id
        self.role = role
        self._queue = queue
        self._is_in_use = is_in_use
        # acquire直後はencoder側のread taskがまだ起動していない。最初のread()と
        # broadcastをsession lockで直列化するまで、過去chunkをqueueへ積まない。
        self._activated = False
        self._closed = False
        self._failure_reason: str | None = None

    @property
    def generation_id(self) -> int:
        """
        共有 source epoch の初期 generation ID。

        Args:
            None

        Returns:
            int: 処理結果の整数値。
        """

        return self._session.generation_id

    @property
    def key(self) -> LiveSourceKey:
        """
        共有 source key。

        Args:
            None

        Returns:
            LiveSourceKey: この購読が参照する共有 source key。
        """

        return self._session.descriptor.key

    @property
    def psi_data_archiver(self) -> LivePSIDataArchiver | None:
        """
        source 共有の PSI/SI archiver。

        Args:
            None

        Returns:
            LivePSIDataArchiver | None: producer が共有する PSI/SI archiver。
        """

        producer = self._session.producer
        return None if producer is None else producer.psi_data_archiver

    @property
    def source_geometry(self) -> LiveSourceVideoGeometry | None:
        """
        source 共有の最新 geometry。

        Args:
            None

        Returns:
            LiveSourceVideoGeometry | None: 検出できた場合の入力映像 geometry。
        """

        producer = self._session.producer
        return None if producer is None else producer.source_geometry

    @property
    def last_input_at(self) -> float:
        """
        backend raw TS の最終受信時刻。

        Args:
            None

        Returns:
            float: 処理結果の数値。
        """

        producer = self._session.producer
        return 0.0 if producer is None else producer.last_input_at

    @property
    def failure_reason(self) -> str | None:
        """
        この subscriber が明示切断された理由。

        Args:
            None

        Returns:
            str | None: 検出済みなら異常理由。正常時は None。
        """

        return self._failure_reason

    @property
    def is_closed(self) -> bool:
        """
        購読が終了済みなら True を返す。

        Args:
            None

        Returns:
            bool: 判定結果。
        """

        return self._closed

    @property
    def is_activated(self) -> bool:
        """
        最初のread()が開始され、live edgeからの配信対象ならTrueを返す。

        Args:
            None

        Returns:
            bool: 判定結果。
        """

        return self._activated

    def activate(self) -> None:
        """
        最初のread()と同時に、この購読をbroadcast対象へ切り替える。

        Args:
            None

        Returns:
            None
        """

        self._activated = True

    def isInUse(self) -> bool:
        """
        購読元の LiveStream に実 viewer が残っているか返す。

        Args:
            None

        Returns:
            bool: 判定結果。
        """

        return self._is_in_use()

    def enqueue(self, chunk: bytes) -> None:
        """
        未読上限を超えない場合だけ TS chunk を追加する。

        Args:
            chunk (bytes): 処理対象の TS chunk。

        Returns:
            None
        """

        self._queue.put_nowait(chunk)

    def replacePendingWithSignal(
        self,
        signal: _SubscriberFailure | _SourceFailure | _SourcePreempted | _SourceEnd,
    ) -> None:
        """
        未読 TS を捨て、終了または失敗 signal を必ず1件だけ残す。

        Args:
            signal (_SubscriberFailure | _SourceFailure | _SourcePreempted | _SourceEnd): subscriber へ通知する終了または失敗 signal。

        Returns:
            None
        """

        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queue.put_nowait(signal)

    async def read(self) -> bytes:
        """
        次の完全な共有 TS chunk を返す。

        Args:
            None

        Returns:
            bytes: 処理結果のバイト列。
        """

        # acquireとproducer開始の間に届いた過去chunkは蓄積しない。最初のread()を
        # session lock下でbroadcastと直列化し、以後だけ有限queueでbackpressureを判定する。
        if self._activated is False:
            await self._session.activate(self._subscriber_id)
        item = await self._queue.get()
        if isinstance(item, bytes):
            return item
        if isinstance(item, _SubscriberFailure):
            self._failure_reason = item.reason
            self._closed = True
            raise LiveSourceSubscriberError(item.reason)
        if isinstance(item, _SourceFailure):
            self._failure_reason = item.reason
            self._closed = True
            raise LiveSourceError(item.reason)
        if isinstance(item, _SourcePreempted):
            self._failure_reason = item.reason
            self._closed = True
            raise LiveSourcePreemptedError(item.reason)
        self._closed = True
        return b''

    async def close(self, *, keep_source_alive: bool = True) -> None:
        """
        購読を冪等に解放する。

        encoder restart は keep_source_alive=True で idle grace を利用し、source generation を維持する。
        channel/service 終了時は False にして、最後の subscriber なら即時終了する。

        Args:
            keep_source_alive (bool): 最後の購読解除後も猶予時間中 source を維持するか。

        Returns:
            None
        """

        if self._closed:
            await self._session.confirmCleanupIfTerminated()
            return
        await self._session.unsubscribe(
            self._subscriber_id,
            keep_source_alive = keep_source_alive,
        )
        # unsubscribe の await がキャンセルされた場合は、外側 cleanup から再試行できるよう
        # session 登録を外し終えてから closed を確定する。
        self._closed = True
        # terminal signal を読む前に購読を閉じた場合も、既に source が終了していれば
        # producer cleanup の sticky result を成功へ反転させない。
        await self._session.confirmCleanupIfTerminated()

    async def promoteToActive(self) -> None:
        """
        Commit 済み B を Preparing から Active へ原子的に昇格する。

        Args:
            None

        Returns:
            None
        """

        await self._session.promoteToActive(self._subscriber_id)


class _LiveSourceSession:
    """1つの source epoch と最大2つの encoder subscriber を所有する。"""

    def __init__(
        self,
        descriptor: LiveSourceDescriptor,
        generation_id: int,
        producer_factory: LiveSourceProducerFactory,
        *,
        queue_size: int,
        idle_grace_seconds: float,
        on_terminated: Callable[[_LiveSourceSession], Awaitable[None]],
    ) -> None:
        """
        利用する状態を初期化する。

        Args:
            descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
            generation_id (int): 共有 source epoch を識別する generation ID。
            producer_factory (LiveSourceProducerFactory): backend 入力と共有 tsreadex producer を生成する callback。
            queue_size (int): subscriber ごとに保持できる最大 TS chunk 数。
            idle_grace_seconds (float): subscriber が 0 になってから source を維持する猶予秒数。
            on_terminated (Callable[[_LiveSourceSession], Awaitable[None]]): source session 終了時に registry を更新する callback。

        Returns:
            None
        """
        self.descriptor = descriptor
        self.generation_id = generation_id
        self._producer_factory = producer_factory
        self._queue_size = queue_size
        self._idle_grace_seconds = idle_grace_seconds
        self._on_terminated = on_terminated
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._subscribers: dict[int, LiveSourceSubscription] = {}
        self._next_subscriber_id = 1
        self._producer: LiveSourceProducer | None = None
        self._producer_task: asyncio.Task[None] | None = None
        self._producer_run_started = False
        self._idle_stop_task: asyncio.Task[None] | None = None
        self._closed = False
        self._preempt_reason: str | None = None
        self._producer_close_task: asyncio.Task[None] | None = None

    @property
    def producer(self) -> LiveSourceProducer | None:
        """
        開始済み producer を返す。

        Args:
            None

        Returns:
            LiveSourceProducer | None: 起動済みなら共有 source producer。
        """

        return self._producer

    @property
    def is_closed(self) -> bool:
        """
        session が終了済みなら True を返す。

        Args:
            None

        Returns:
            bool: 判定結果。
        """

        return self._closed

    @property
    def cleanup_failure(self) -> BaseException | None:
        """
        producer cleanup task の確定済み失敗を返す。

        Args:
            None

        Returns:
            BaseException | None: cleanup が失敗・cancelされた場合の例外。
        """

        task = self._producer_close_task
        if task is None or task.done() is False:
            return None
        if task.cancelled():
            return LiveSourceError('live_source_cleanup_cancelled')
        return task.exception()

    async def _ensureStarted(self) -> None:
        """
        producer と broadcast task を一度だけ開始する。

        Args:
            None

        Returns:
            None
        """

        async with self._start_lock:
            if self._closed:
                raise LiveSourceError('live_source_closed')
            if self._producer_task is not None:
                return
            producer = await self._producer_factory(
                self.descriptor,
                self.generation_id,
            )
            should_close_without_start = False
            async with self._lock:
                self._producer = producer
                if self._closed:
                    should_close_without_start = True
                else:
                    self._producer_task = asyncio.create_task(
                        self._run(),
                        name=f'LiveSource-{self.descriptor.key.asLogString()}',
                    )
            if should_close_without_start:
                await self._closeProducer()
                raise LiveSourceError('live_source_closed')

    async def subscribe(
        self,
        role: LiveSourceSubscriberRole,
        is_in_use: Callable[[], bool],
    ) -> LiveSourceSubscription:
        """
        A または B の subscriber を登録する。

        Args:
            role (LiveSourceSubscriberRole): 共有 source 上での購読 role。
            is_in_use (Callable[[], bool]): 購読元に viewer が残るか判定する callback。

        Returns:
            LiveSourceSubscription: 登録または取得した source 購読。
        """

        async with self._lock:
            if self._closed:
                raise LiveSourceError('live_source_closed')
            # Active は別視聴者・別品質で複数存在できる。二段階 B だけは source ごとに1本へ制限する。
            if role == 'Preparing' and any(
                subscription.role == 'Preparing'
                for subscription in self._subscribers.values()
            ):
                raise LiveSourceCapacityError('live_source_preparing_already_exists')
            if self._idle_stop_task is not None:
                self._idle_stop_task.cancel()
                self._idle_stop_task = None

            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            subscription = LiveSourceSubscription(
                self,
                subscriber_id,
                role,
                asyncio.Queue(maxsize=self._queue_size),
                is_in_use,
            )
            self._subscribers[subscriber_id] = subscription

        try:
            await self._ensureStarted()
        except BaseException:
            async with self._lock:
                self._subscribers.pop(subscriber_id, None)
            raise
        return subscription

    def hasViewers(self) -> bool:
        """
        いずれかの品質 subscriber に実viewerが残っているか返す。

        Args:
            None

        Returns:
            bool: 判定結果。
        """

        for subscription in self._subscribers.values():
            try:
                if subscription.isInUse() is True:
                    return True
            except Exception:
                # 判定不能なsubscriberを誤ってpreemptしない
                return True
        return False

    async def promoteToActive(self, subscriber_id: int) -> None:
        """
        指定 Preparing subscriber を Active へ昇格する。

        Args:
            subscriber_id (int): 対象 subscriber を識別する session 内 ID。

        Returns:
            None
        """

        async with self._lock:
            subscription = self._subscribers.get(subscriber_id)
            if subscription is None or subscription.is_closed:
                raise LiveSourceSubscriberError('live_source_subscription_not_found')
            if subscription.role == 'Active':
                return
            if subscription.role != 'Preparing':
                raise LiveSourceSubscriberError('live_source_subscription_is_not_preparing')
            subscription.role = 'Active'

    async def activate(self, subscriber_id: int) -> None:
        """
        最初のread()をbroadcastと直列化し、live edgeから購読を開始する。

        Args:
            subscriber_id (int): 対象 subscriber を識別する session 内 ID。

        Returns:
            None
        """

        async with self._lock:
            subscription = self._subscribers.get(subscriber_id)
            if subscription is None:
                # source終了やslow切断は既にterminal signalをqueueへ残している。
                # ここで別例外へ置き換えず、read()に元のsignalを処理させる。
                return
            if subscription.is_closed:
                return
            subscription.activate()

    @staticmethod
    def _replaceQueueWithSignal(
        subscription: LiveSourceSubscription,
        signal: _SubscriberFailure | _SourceFailure | _SourcePreempted | _SourceEnd,
    ) -> None:
        """
        失敗 subscriber の未読 TS だけを捨て、必ず終了 signal を届ける。

        Args:
            subscription (LiveSourceSubscription): 関連付けまたは通知対象の共有 source subscription。
            signal (_SubscriberFailure | _SourceFailure | _SourcePreempted | _SourceEnd): subscriber へ通知する終了または失敗 signal。

        Returns:
            None
        """

        subscription.replacePendingWithSignal(signal)

    async def _scheduleIdleStopLocked(self) -> None:
        """
        subscriber 0 の source を grace 後に終了する task を登録する。

        Args:
            None

        Returns:
            None
        """

        if self._idle_stop_task is not None or self._closed:
            return

        async def stop_after_grace() -> None:
            """
            猶予時間後に未使用 source を停止する。

            Args:
                None

            Returns:
                None
            """
            try:
                await asyncio.sleep(self._idle_grace_seconds)
                async with self._lock:
                    if self._subscribers or self._closed:
                        return
                await self.stop()
            except asyncio.CancelledError:
                return

        self._idle_stop_task = asyncio.create_task(stop_after_grace())

    async def _broadcast(self, chunk: bytes) -> None:
        """
        満杯でない全 subscriber へ同一 chunk を一度ずつ配信する。

        Args:
            chunk (bytes): 処理対象の TS chunk。

        Returns:
            None
        """

        async with self._lock:
            failed_ids: list[int] = []
            for subscriber_id, subscription in self._subscribers.items():
                # encoder側が最初のread()を開始する前のchunkはlive edgeより古いため捨てる。
                # activate()も同じlockを使うので、最初に届けるchunkとのraceは発生しない。
                if subscription.is_activated is False:
                    continue
                try:
                    subscription.enqueue(chunk)
                except asyncio.QueueFull:
                    failed_ids.append(subscriber_id)
                    self._replaceQueueWithSignal(
                        subscription,
                        _SubscriberFailure('live_source_subscriber_too_slow'),
                    )

            for subscriber_id in failed_ids:
                self._subscribers.pop(subscriber_id, None)
            if failed_ids:
                logging.warning(
                    f'[LiveSource {self.descriptor.key.asLogString()}] '
                    f'Disconnected {len(failed_ids)} slow subscriber(s).'
                )
            if not self._subscribers:
                await self._scheduleIdleStopLocked()

    async def _signalAll(self, signal: _SourceFailure | _SourcePreempted | _SourceEnd) -> None:
        """
        全 subscriber へ source 終了を通知して登録を空にする。

        Args:
            signal (_SourceFailure | _SourcePreempted | _SourceEnd): subscriber へ通知する終了または失敗 signal。

        Returns:
            None
        """

        async with self._lock:
            subscriptions = tuple(self._subscribers.values())
            self._subscribers.clear()
            for subscription in subscriptions:
                self._replaceQueueWithSignal(subscription, signal)

    async def _closeProducer(self) -> None:
        """
        producer を一度だけ閉じる。

        Args:
            None

        Returns:
            None
        """

        if self._producer is None:
            return
        if self._producer_close_task is None:
            self._producer_close_task = asyncio.create_task(
                self._producer.close(),
                name=f'LiveSourceClose-{self.descriptor.key.asLogString()}',
            )
        try:
            await asyncio.shield(self._producer_close_task)
        except BaseException as ex:
            logging.warning(
                f'[LiveSource {self.descriptor.key.asLogString()}] '
                'Failed to close source producer.',
                exc_info = ex,
            )
            raise

    async def _waitForProducerTermination(self, task: asyncio.Task[None]) -> None:
        """
        producer task の終了と、その finally 内で行う cleanup の成否を確認する。

        呼び出し側だけがキャンセルされた場合は producer task を巻き込まず、既に producer task
        自体が Cancelled で終了した場合だけ正常な停止として扱う。

        Args:
            task (asyncio.Task[None]): 終了確認対象の producer task。

        Returns:
            None
        """

        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done() is False or task.cancelled() is False:
                raise
        # producer task が Cancelled で終了していても、close task の sticky result を再確認する。
        await self._closeProducer()

    async def _waitForProducerTerminationAndDeregister(
        self,
        task: asyncio.Task[None],
    ) -> None:
        """
        producer 終了を確認し、coroutine 開始前 cancel の場合も registry を確実に更新する。

        Args:
            task (asyncio.Task[None]): 終了確認対象の producer task。

        Returns:
            None
        """

        try:
            await self._waitForProducerTermination(task)
        except asyncio.CancelledError:
            # 呼び出し側だけが cancel された場合、cleanup は producer task 側で継続する。
            raise
        except BaseException:
            await self._on_terminated(self)
            raise
        await self._on_terminated(self)

    async def confirmCleanupIfTerminated(self) -> None:
        """
        source終了後のproducer cleanup結果を、terminal signalを読んだsubscriberへ再送する。

        slow subscriber単独切断ではsource自体が生存するため待機しない。

        Args:
            None

        Returns:
            None
        """

        if self._closed is False:
            return
        task = self._producer_task
        if task is not None and task is not asyncio.current_task():
            await self._waitForProducerTerminationAndDeregister(task)
            return
        try:
            await self._closeProducer()
        finally:
            await self._on_terminated(self)

    async def _run(self) -> None:
        """
        producer 出力を subscriber へ fanoutする。

        Args:
            None

        Returns:
            None
        """

        assert self._producer is not None
        self._producer_run_started = True
        failure: str | None = None
        try:
            while True:
                chunk = await self._producer.read()
                if chunk == b'':
                    break
                if len(chunk) % TS_PACKET_SIZE != 0:
                    raise LiveSourceError('live_source_output_is_not_ts_aligned')
                await self._broadcast(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            failure = str(ex) or ex.__class__.__name__
            logging.warning(
                f'[LiveSource {self.descriptor.key.asLogString()}] '
                f'Source producer failed: {failure}'
            )
        finally:
            self._closed = True
            if self._preempt_reason is not None:
                await self._signalAll(_SourcePreempted(self._preempt_reason))
            elif failure is None:
                await self._signalAll(_SourceEnd())
            else:
                await self._signalAll(_SourceFailure(failure))
            try:
                await self._closeProducer()
            finally:
                await self._on_terminated(self)

    async def unsubscribe(self, subscriber_id: int, *, keep_source_alive: bool) -> None:
        """
        subscriber を取り除き、必要なら source 終了を予約する。

        Args:
            subscriber_id (int): 対象 subscriber を識別する session 内 ID。
            keep_source_alive (bool): 最後の購読解除後も猶予時間中 source を維持するか。

        Returns:
            None
        """

        should_stop = False
        async with self._lock:
            self._subscribers.pop(subscriber_id, None)
            if not self._subscribers:
                if keep_source_alive:
                    await self._scheduleIdleStopLocked()
                else:
                    should_stop = True
        if should_stop:
            await self.stop()

    async def stop(self) -> None:
        """
        session を冪等に停止する。

        Args:
            None

        Returns:
            None
        """

        task: asyncio.Task[None] | None
        was_closed = False
        async with self._lock:
            if self._closed:
                was_closed = True
            else:
                self._closed = True
                if self._idle_stop_task is not None:
                    current_task = asyncio.current_task()
                    if self._idle_stop_task is not current_task:
                        self._idle_stop_task.cancel()
                    self._idle_stop_task = None
            task = self._producer_task

        # producer factory と stop が競合した場合は、factory が返した producer の回収まで確認する。
        if task is None:
            async with self._start_lock:
                task = self._producer_task
        if task is not None and task is not asyncio.current_task():
            if was_closed is False:
                task.cancel()
            if self._producer_run_started is False:
                terminal_signal: _SourceEnd | _SourcePreempted = (
                    _SourcePreempted(self._preempt_reason)
                    if self._preempt_reason is not None
                    else _SourceEnd()
                )
                await self._signalAll(terminal_signal)
            await self._waitForProducerTerminationAndDeregister(task)
        elif was_closed:
            await self._closeProducer()
        else:
            await self._signalAll(_SourceEnd())
            try:
                await self._closeProducer()
            finally:
                await self._on_terminated(self)

    async def preemptIfNoViewers(self) -> bool:
        """
        viewer 0 を lock 下で再確認し、該当する場合だけ source 単位で停止する。

        Args:
            None

        Returns:
            bool: 判定結果。
        """

        task: asyncio.Task[None] | None
        was_closed = False
        async with self._lock:
            if self._closed:
                was_closed = True
            else:
                # coordinator が候補を列挙した後に旧 source へ新しい viewer が入る場合がある。
                # subscribe() と同じ lock 下で再確認し、視聴中の A を preempt しない。
                if self.hasViewers():
                    return False
                self._closed = True
                self._preempt_reason = 'live_source_preempted_no_viewers'
                if self._idle_stop_task is not None:
                    current_task = asyncio.current_task()
                    if self._idle_stop_task is not current_task:
                        self._idle_stop_task.cancel()
                    self._idle_stop_task = None
            task = self._producer_task

        # producer factory と preempt が競合した場合も、未開始 producer を残したまま次へ進まない。
        if task is None:
            async with self._start_lock:
                task = self._producer_task
        if task is not None and task is not asyncio.current_task():
            if was_closed is False:
                task.cancel()
            if self._producer_run_started is False:
                await self._signalAll(_SourcePreempted('live_source_preempted_no_viewers'))
            await self._waitForProducerTerminationAndDeregister(task)
        elif was_closed:
            await self._closeProducer()
        else:
            await self._signalAll(_SourcePreempted('live_source_preempted_no_viewers'))
            try:
                await self._closeProducer()
            finally:
                await self._on_terminated(self)
        return was_closed is False


class LiveSourceCoordinator:
    """source key ごとに1つの producer/tsreadex と複数 Active + 1 Preparing を管理する。"""

    # QSV / Bridge は起動直後の出力確定時に encoder stdin の読み取りを一時停止することがある。
    # SOURCE_CHUNK_SIZE 128件は約5.9MiBで、通常時は即座に消費されるため遅延を増やさず、
    # 最も高レートの放送波でも約1秒超の一時停止を吸収できる。これを使い切る継続遅延は
    # 従来どおり当該subscriberだけを切断し、共有sourceともう一方のAは止めない。
    DEFAULT_QUEUE_SIZE = 128
    DEFAULT_IDLE_GRACE_SECONDS = 10.0
    DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 30.0

    def __init__(
        self,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        idle_grace_seconds: float = DEFAULT_IDLE_GRACE_SECONDS,
        generation_factory: Callable[[], int] | None = None,
        producer_factory: LiveSourceProducerFactory | None = None,
    ) -> None:
        """
        利用する状態を初期化する。

        Args:
            queue_size (int): subscriber ごとに保持できる最大 TS chunk 数。
            idle_grace_seconds (float): subscriber が 0 になってから source を維持する猶予秒数。
            generation_factory (Callable[[], int] | None): 共有 source の generation ID を生成する callback。
            producer_factory (LiveSourceProducerFactory | None): backend 入力と共有 tsreadex producer を生成する callback。

        Returns:
            None
        """
        if queue_size <= 0:
            raise ValueError('queue_size must be greater than zero.')
        if idle_grace_seconds < 0:
            raise ValueError('idle_grace_seconds must not be negative.')
        self._queue_size = queue_size
        self._idle_grace_seconds = idle_grace_seconds
        self._generation_factory = generation_factory or self._newGenerationID
        self._producer_factory = producer_factory or createTSReadExLiveSourceProducer
        self._lock = asyncio.Lock()
        self._sessions: dict[LiveSourceKey, _LiveSourceSession] = {}
        self._shutdown_started = False
        self._shutdown_task: asyncio.Task[bool] | None = None
        self._shutdown_stop_tasks: set[asyncio.Task[None]] = set()
        self._cleanup_failure: BaseException | None = None

    @staticmethod
    def _newGenerationID() -> int:
        """
        0 以外のランダムな uint64 generation ID を返す。

        Args:
            None

        Returns:
            int: 処理結果の整数値。
        """

        generation_id = 0
        while generation_id == 0:
            generation_id = secrets.randbits(64)
        return generation_id

    async def _sessionTerminated(self, session: _LiveSourceSession) -> None:
        """
        終了した session だけを registry から取り除く。

        Args:
            session (_LiveSourceSession): 共有 source session または HTTP client session。

        Returns:
            None
        """

        cleanup_failure = session.cleanup_failure
        async with self._lock:
            if cleanup_failure is not None and self._cleanup_failure is None:
                self._cleanup_failure = cleanup_failure
            if self._sessions.get(session.descriptor.key) is session:
                self._sessions.pop(session.descriptor.key, None)

    def _createSession(self, descriptor: LiveSourceDescriptor) -> _LiveSourceSession:
        """
        新しい source epoch session を生成する。

        Args:
            descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。

        Returns:
            _LiveSourceSession: registry 登録前の新規 session。
        """

        generation_id = self._generation_factory()
        if generation_id <= 0 or generation_id > (1 << 64) - 1:
            raise ValueError('generation_factory must return a non-zero uint64.')
        return _LiveSourceSession(
            descriptor,
            generation_id,
            self._producer_factory,
            queue_size = self._queue_size,
            idle_grace_seconds = self._idle_grace_seconds,
            on_terminated = self._sessionTerminated,
        )

    async def _recordCleanupFailure(self, error: BaseException) -> None:
        """
        cleanup 未確認後の新 source 起動を、プロセス再起動まで fail-close する。

        Args:
            error (BaseException): cleanup を確認できなかった原因。

        Returns:
            None
        """

        async with self._lock:
            if self._cleanup_failure is None:
                self._cleanup_failure = error

    def _raiseIfAcquisitionBlocked(self) -> None:
        """
        shutdown または cleanup failure 後の acquire を拒否する。

        Raises:
            LiveSourceError: 新しい source を安全に開始できない場合。
        """

        if self._shutdown_started:
            raise LiveSourceError('live_source_coordinator_shutting_down')
        if self._cleanup_failure is not None:
            raise LiveSourceError(
                'live_source_coordinator_cleanup_blocked'
            ) from self._cleanup_failure

    async def acquire(
        self,
        descriptor: LiveSourceDescriptor,
        *,
        role: LiveSourceSubscriberRole,
        is_in_use: Callable[[], bool] = lambda: True,
    ) -> LiveSourceSubscription:
        """
        同じ key の session を再利用して subscriber を取得する。

        Args:
            descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
            role (LiveSourceSubscriberRole): 共有 source 上での購読 role。
            is_in_use (Callable[[], bool]): 購読元に viewer が残るか判定する callback。

        Returns:
            LiveSourceSubscription: 登録または取得した source 購読。
        """

        # 別チャンネル開始前に、同じ backend の旧 source を候補として列挙する。
        # 実際の viewer 0 判定は session lock 下で行い、列挙後に入った viewer を保護する。
        async with self._lock:
            self._raiseIfAcquisitionBlocked()
            reclaimable_sessions = tuple(
                session
                for key, session in self._sessions.items()
                if (
                    key.backend == descriptor.key.backend and
                    key != descriptor.key
                )
            )
        if reclaimable_sessions:
            preempt_results = await asyncio.gather(
                *(session.preemptIfNoViewers() for session in reclaimable_sessions),
                return_exceptions=True,
            )
            cleanup_failure = next(
                (
                    result
                    for result in preempt_results
                    if isinstance(result, BaseException)
                ),
                None,
            )
            if cleanup_failure is not None:
                await self._recordCleanupFailure(cleanup_failure)
                raise LiveSourceError('live_source_preempt_cleanup_failed') from cleanup_failure

        # 終了競合は旧 session の cleanup 完了を待ってから同じ registry lockへ戻す。
        # 複数 acquire が同時に再試行しても、lock 内で最初の1件だけがreplacementを生成する。
        for _attempt in range(3):
            terminated_session: _LiveSourceSession | None = None
            async with self._lock:
                self._raiseIfAcquisitionBlocked()
                session = self._sessions.get(descriptor.key)
                if session is None:
                    session = self._createSession(descriptor)
                    self._sessions[descriptor.key] = session
                elif session.is_closed:
                    terminated_session = session
                elif session.descriptor != descriptor:
                    raise LiveSourceError('live_source_descriptor_mismatch')

            if terminated_session is not None:
                try:
                    await terminated_session.confirmCleanupIfTerminated()
                except asyncio.CancelledError:
                    raise
                except BaseException as ex:
                    await self._recordCleanupFailure(ex)
                    raise LiveSourceError(
                        'live_source_cleanup_failed_before_acquire'
                    ) from ex
                continue

            try:
                return await session.subscribe(role, is_in_use)
            except LiveSourceCleanupError as ex:
                await self._recordCleanupFailure(ex)
                raise
            except LiveSourceError as ex:
                if str(ex) != 'live_source_closed':
                    raise
                try:
                    await session.confirmCleanupIfTerminated()
                except asyncio.CancelledError:
                    raise
                except BaseException as cleanup_error:
                    await self._recordCleanupFailure(cleanup_error)
                    raise LiveSourceError(
                        'live_source_cleanup_failed_before_acquire'
                    ) from cleanup_error
        raise LiveSourceError('live_source_closed')

    async def activeSessionCount(self) -> int:
        """
        現在 registry にある source session 数を返す。

        Args:
            None

        Returns:
            int: 処理結果の整数値。
        """

        async with self._lock:
            return len(self._sessions)

    async def shutdown(
        self,
        *,
        timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> bool:
        """
        全 source session を全体 deadline 付きで並列に停止する。

        Args:
            timeout_seconds (float): cancel 後の短い reap を含む最大待機秒数。

        Returns:
            bool: 全 session の停止を deadline 内に確認できた場合は True。
        """

        if timeout_seconds <= 0:
            raise ValueError('timeout_seconds must be greater than zero.')
        if self._shutdown_task is None:
            # acquire側のlock取得より先に同期的に閉門し、shutdown開始後の新session生成を拒否する。
            self._shutdown_started = True
            self._shutdown_task = asyncio.create_task(
                self._shutdownInternal(timeout_seconds),
                name='LiveSourceCoordinatorShutdown',
            )
        return await asyncio.shield(self._shutdown_task)

    async def _shutdownInternal(self, timeout_seconds: float) -> bool:
        """
        最初の shutdown 呼び出しだけが実行する全 source 停止処理。

        Args:
            timeout_seconds (float): cancel 後の短い reap を含む最大待機秒数。

        Returns:
            bool: 全 session の停止を deadline 内に確認できた場合は True。
        """

        deadline = time.monotonic() + timeout_seconds
        async with self._lock:
            sessions = tuple(self._sessions.values())
            # timeout や例外でも coordinator が停止不能 session を保持し続けないよう、
            # 停止を開始する前に registry の所有権を放棄する。
            self._sessions.clear()
        if len(sessions) == 0:
            return self._cleanup_failure is None

        stop_tasks = {
            asyncio.create_task(session.stop())
            for session in sessions
        }
        self._shutdown_stop_tasks.update(stop_tasks)
        for task in stop_tasks:
            task.add_done_callback(self._shutdown_stop_tasks.discard)
        remaining_seconds = max(0.0, deadline - time.monotonic())
        done, pending = await asyncio.wait(stop_tasks, timeout=remaining_seconds)

        shutdown_confirmed = True
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                shutdown_confirmed = False
            except Exception as ex:
                shutdown_confirmed = False
                logging.warning(
                    '[LiveSourceCoordinator] A source session failed during shutdown.',
                    exc_info = ex,
                )

        if len(pending) == 0:
            return shutdown_confirmed and self._cleanup_failure is None

        for task in pending:
            # session.stop() 自体は producer を停止済みなので、待機 Task は cancel せず保持する。
            # deadline後も実 cleanup を継続しながら、完了時の未回収例外を処理する。
            task.add_done_callback(_consumeShutdownTaskResult)

        logging.warning(
            '[LiveSourceCoordinator] '
            f'{len(pending)} source session(s) did not stop before the shutdown timeout.'
        )
        return False


class _LiveSourceInput(Protocol):
    """EDCB/Mirakurun raw TS 入力の共通境界。"""

    async def readexactly(self, size: int) -> bytes:
        """
        size byte の raw TS を読む。

        Args:
            size (int): 読み取るバイト数。

        Returns:
            bytes: 処理結果のバイト列。
        """

        ...

    async def close(self) -> None:
        """
        入力と backend 所有資源を閉じる。

        Args:
            None

        Returns:
            None
        """

        ...


class _EDCBLiveSourceInput:
    """共有 source が所有する EDCB NetworkTV 入力。"""

    def __init__(
        self,
        tuner: EDCBTuner,
        owner_id: str,
        reader: asyncio.StreamReader | PipeStreamReader,
    ) -> None:
        """
        利用する状態を初期化する。

        Args:
            tuner (EDCBTuner): 共有 source が所有する EDCB tuner。
            owner_id (str): EDCB tuner の排他所有者 ID。
            reader (asyncio.StreamReader | PipeStreamReader): EDCB NetworkTV から TS を読む stream reader。

        Returns:
            None
        """
        self._tuner = tuner
        self._owner_id = owner_id
        self._reader = reader
        self._closed = False

    async def readexactly(self, size: int) -> bytes:
        """
        指定サイズのデータを読み取る。

        Args:
            size (int): 読み取るバイト数。

        Returns:
            bytes: 処理結果のバイト列。
        """
        return await self._reader.readexactly(size)

    async def close(self) -> None:
        """
        保持するリソースを閉じる。

        Args:
            None

        Returns:
            None
        """
        if self._closed:
            return
        self._closed = True
        failures: list[BaseException] = []
        try:
            await self._tuner.disconnect(self._owner_id)
        except BaseException as ex:
            failures.append(ex)
        try:
            if self._tuner.unlock(self._owner_id) is False:
                failures.append(LiveSourceError('edcb_tuner_unlock_failed'))
        except BaseException as ex:
            failures.append(ex)
        try:
            if await self._tuner.close(self._owner_id) is False:
                failures.append(LiveSourceError('edcb_tuner_close_failed'))
        except BaseException as ex:
            failures.append(ex)
        if failures:
            raise LiveSourceError('edcb_live_source_input_cleanup_failed') from BaseExceptionGroup(
                'EDCB live source input cleanup failures',
                failures,
            )


class _MirakurunLiveSourceInput:
    """共有 source が所有する Mirakurun Service Stream API 入力。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        response: aiohttp.ClientResponse,
    ) -> None:
        """
        利用する状態を初期化する。

        Args:
            session (aiohttp.ClientSession): 共有 source session または HTTP client session。
            response (aiohttp.ClientResponse): Mirakurun Service Stream API の streaming response。

        Returns:
            None
        """
        self._session = session
        self._response = response
        self._closed = False

    async def readexactly(self, size: int) -> bytes:
        """
        指定サイズのデータを読み取る。

        Args:
            size (int): 読み取るバイト数。

        Returns:
            bytes: 処理結果のバイト列。
        """
        return await self._response.content.readexactly(size)

    async def close(self) -> None:
        """
        保持するリソースを閉じる。

        Args:
            None

        Returns:
            None
        """
        if self._closed:
            return
        self._closed = True
        self._response.close()
        await self._session.close()


class _FileLiveSourceInput:
    """debug TS ファイルを実時間相当で読む共有 raw 入力。"""

    READ_LIMIT_BYTES_PER_SECOND = 2350 * 1024

    def __init__(self, path: Path) -> None:
        """
        利用する状態を初期化する。

        Args:
            path (Path): debug TS 入力ファイルのパス。

        Returns:
            None
        """
        self._file: BinaryIO = path.open('rb')
        self._started_at = time.monotonic()
        self._bytes_read = 0
        self._closed = False

    async def readexactly(self, size: int) -> bytes:
        """
        指定サイズのデータを読み取る。

        Args:
            size (int): 読み取るバイト数。

        Returns:
            bytes: 処理結果のバイト列。
        """
        if self._closed:
            raise asyncio.IncompleteReadError(b'', size)
        chunk = await asyncio.to_thread(self._file.read, size)
        if chunk:
            self._bytes_read += len(chunk)
            read_deadline = (
                self._started_at +
                self._bytes_read / self.READ_LIMIT_BYTES_PER_SECOND
            )
            delay = read_deadline - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        if len(chunk) != size:
            raise asyncio.IncompleteReadError(chunk, size)
        return chunk

    async def close(self) -> None:
        """
        保持するリソースを閉じる。

        Args:
            None

        Returns:
            None
        """
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._file.close)


async def _openLiveSourceInput(descriptor: LiveSourceDescriptor, generation_id: int) -> _LiveSourceInput:
    """
    descriptor の backend から raw TS 入力を1本だけ開く。

    Args:
        descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
        generation_id (int): 共有 source epoch を識別する generation ID。

    Returns:
        _LiveSourceInput: 開いた EDCB・Mirakurun・debug file 入力。
    """

    key = descriptor.key
    if key.backend == 'EDCB':
        owner_id = (
            f'LiveSource-{key.network_id}-{key.transport_stream_id}-'
            f'{key.service_id}-{generation_id:016x}'
        )
        tuner = EDCBTuner.getOrCreate(owner_id)
        tuner_locked = False
        try:
            opened = await tuner.setChannel(
                key.network_id,
                key.service_id,
                key.transport_stream_id,
                owner_id,
            )
            if opened is False:
                raise LiveSourceError('edcb_tuner_start_failed')
            tuner_locked = tuner.lock(owner_id)
            if tuner_locked is False:
                raise LiveSourceError('edcb_tuner_lock_failed')
            reader = await tuner.connect(owner_id)
            if reader is None:
                raise LiveSourceError('edcb_tuner_connect_failed')
            return _EDCBLiveSourceInput(tuner, owner_id, reader)
        except BaseException as startup_failure:
            cleanup_failures: list[BaseException] = []
            if tuner_locked:
                try:
                    if tuner.unlock(owner_id) is False:
                        cleanup_failures.append(LiveSourceError('edcb_tuner_unlock_failed'))
                except BaseException as ex:
                    cleanup_failures.append(ex)
            try:
                if await tuner.close(owner_id) is False:
                    cleanup_failures.append(LiveSourceError('edcb_tuner_close_failed'))
            except BaseException as ex:
                cleanup_failures.append(ex)
            if cleanup_failures:
                raise LiveSourceCleanupError(
                    'edcb_tuner_startup_cleanup_failed'
                ) from BaseExceptionGroup(
                    'EDCB tuner startup and cleanup failures',
                    [startup_failure, *cleanup_failures],
                )
            raise

    mirakurun_service_id = int(str(key.network_id).zfill(5) + str(key.service_id).zfill(5))
    session = aiohttp.ClientSession()
    timeout = 40 if descriptor.channel_type == 'BS4K' else 15
    try:
        response = await session.get(
            url = GetMirakurunAPIEndpointURL(f'/api/services/{mirakurun_service_id}/stream'),
            headers = {**API_REQUEST_HEADERS, 'X-Mirakurun-Priority': '0'},
            timeout = aiohttp.ClientTimeout(
                connect = timeout,
                sock_connect = timeout,
                sock_read = timeout,
            ),
        )
        if response.status != 200:
            response.close()
            raise LiveSourceError(f'mirakurun_stream_http_{response.status}')
        return _MirakurunLiveSourceInput(session, response)
    except BaseException as startup_failure:
        try:
            await session.close()
        except BaseException as cleanup_failure:
            raise LiveSourceCleanupError(
                'mirakurun_session_startup_cleanup_failed'
            ) from BaseExceptionGroup(
                'Mirakurun startup and cleanup failures',
                [startup_failure, cleanup_failure],
            )
        raise


class _TSReadExLiveSourceProducer:
    """backend raw TS と共有 tsreadex process を所有する producer。"""

    CLEANUP_STEP_TIMEOUT_SECONDS = 5.0
    CLEANUP_REAP_TIMEOUT_SECONDS = 1.0

    def __init__(
        self,
        descriptor: LiveSourceDescriptor,
        generation_id: int,
        process: asyncio.subprocess.Process,
        source_input: _LiveSourceInput | None,
    ) -> None:
        """
        利用する状態を初期化する。

        Args:
            descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
            generation_id (int): 共有 source epoch を識別する generation ID。
            process (asyncio.subprocess.Process): 共有 tsreadex subprocess。
            source_input (_LiveSourceInput | None): 共有 tsreadex へ供給する backend raw TS 入力。

        Returns:
            None
        """
        self._descriptor = descriptor
        self._generation_id = generation_id
        self._process = process
        self._source_input = source_input
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._detached_cleanup_tasks: set[asyncio.Future[object]] = set()
        self._feed_error: BaseException | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._aspect_monitor = LiveSourceAspectMonitor()
        self._source_geometry: LiveSourceVideoGeometry | None = None
        self._last_input_at = time.monotonic()
        self._psi_data_archiver = LivePSIDataArchiver(descriptor.key.service_id)
        self._feeder_task: asyncio.Task[None] | None = None
        if source_input is not None:
            self._feeder_task = asyncio.create_task(self._feedRawInput())

    @property
    def source_geometry(self) -> LiveSourceVideoGeometry | None:
        """
        source_現在検出済みの入力映像 geometry を返す。

        Args:
            None

        Returns:
            LiveSourceVideoGeometry | None: 検出できた場合の入力映像 geometry。
        """
        return self._source_geometry

    @property
    def psi_data_archiver(self) -> LivePSIDataArchiver:
        """
        source epoch で共有する PSI/SI archiver を返す。

        Args:
            None

        Returns:
            LivePSIDataArchiver: source epoch で共有する PSI/SI archiver。
        """
        return self._psi_data_archiver

    @property
    def last_input_at(self) -> float:
        """
        backend raw TS を最後に受信した単調増加時刻を返す。

        Args:
            None

        Returns:
            float: 処理結果の数値。
        """
        return self._last_input_at

    def _trackTask(self, task: asyncio.Task[None]) -> None:
        """
        短命 PSI push task を終了まで強参照する。

        Args:
            task (asyncio.Task[None]): 完了まで強参照する background task。

        Returns:
            None
        """

        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _feedRawInput(self) -> None:
        """
        backend raw TS を共有 tsreadex stdin へ送る。

        Args:
            None

        Returns:
            None
        """

        assert self._source_input is not None
        assert self._process.stdin is not None
        discard_until = (
            time.monotonic() + self._descriptor.startup_discard_seconds
            if self._descriptor.startup_discard_seconds > 0
            else 0.0
        )
        try:
            while True:
                try:
                    chunk = await self._source_input.readexactly(SOURCE_CHUNK_SIZE)
                except asyncio.IncompleteReadError as ex:
                    chunk = ex.partial
                    if chunk == b'':
                        break
                self._last_input_at = time.monotonic()
                self._trackTask(asyncio.create_task(self.psi_data_archiver.pushTSPacketData(chunk)))

                geometry = self._aspect_monitor.push(chunk)
                if geometry is not None:
                    self._source_geometry = geometry

                if discard_until > 0 and time.monotonic() < discard_until:
                    continue
                self._process.stdin.write(chunk)
                await self._process.stdin.drain()
                if len(chunk) < SOURCE_CHUNK_SIZE:
                    break
        except asyncio.CancelledError:
            raise
        except BaseException as ex:
            self._feed_error = ex
        finally:
            try:
                self._process.stdin.close()
            except Exception:
                pass

    async def read(self) -> bytes:
        """
        共有 tsreadex stdout を TS packet 境界で読む。

        Args:
            None

        Returns:
            bytes: 処理結果のバイト列。
        """

        assert self._process.stdout is not None
        try:
            return await self._process.stdout.readexactly(SOURCE_CHUNK_SIZE)
        except asyncio.IncompleteReadError as ex:
            if ex.partial:
                if len(ex.partial) % TS_PACKET_SIZE != 0:
                    raise LiveSourceError('tsreadex_output_is_not_ts_aligned')
                return ex.partial
            if self._feed_error is not None:
                raise LiveSourceError(
                    f'live_source_input_failed:{self._feed_error.__class__.__name__}'
                ) from self._feed_error
            if self._process.returncode not in (None, 0):
                raise LiveSourceError(f'tsreadex_exited_{self._process.returncode}')
            return b''

    async def _closeResources(self) -> None:
        """
        raw input・tsreadex・archiverを、各工程の期限付きで最後まで解放する。

        Args:
            None

        Returns:
            None
        """

        self._closed = True
        failures: list[tuple[str, BaseException]] = []

        def record_failure(label: str, error: BaseException) -> None:
            """cleanup の失敗工程と元例外を保持する。"""

            failures.append((label, error))
            logging.warning(
                f'[LiveSource {self._descriptor.key.asLogString()}] '
                f'Cleanup step failed: {label}.',
                exc_info = error,
            )

        async def run_bounded(
            label: str,
            awaitable: Awaitable[object],
            *,
            timeout_seconds: float | None = None,
        ) -> None:
            """単一 cleanup 工程を期限付きで実行し、失敗後も残りを続行する。"""

            step_timeout_seconds = (
                self.CLEANUP_STEP_TIMEOUT_SECONDS
                if timeout_seconds is None
                else timeout_seconds
            )
            step_task = asyncio.ensure_future(awaitable)

            def consume_detached_step(done_task: asyncio.Future[object]) -> None:
                """期限超過後も継続するcleanup taskの参照と例外を回収する。"""

                self._detached_cleanup_tasks.discard(done_task)
                try:
                    done_task.result()
                except asyncio.CancelledError:
                    pass
                except BaseException as ex:
                    logging.warning(
                        f'[LiveSource {self._descriptor.key.asLogString()}] '
                        f'Detached cleanup step failed after timeout: {label}.',
                        exc_info = ex,
                    )

            def detach_step() -> None:
                """step task をcancel要求後も完了まで強参照する。"""

                if step_task.done() is False:
                    step_task.cancel()
                self._detached_cleanup_tasks.add(step_task)
                step_task.add_done_callback(consume_detached_step)

            try:
                done, _pending = await asyncio.wait(
                    {step_task},
                    timeout = step_timeout_seconds,
                )
            except asyncio.CancelledError:
                detach_step()
                raise
            if len(done) == 0:
                detach_step()
                record_failure(
                    label,
                    TimeoutError(
                        f'cleanup step exceeded {step_timeout_seconds:.3f} seconds'
                    ),
                )
                return
            try:
                step_task.result()
            except asyncio.CancelledError as ex:
                record_failure(label, ex)
            except BaseException as ex:
                record_failure(label, ex)

        if self._feeder_task is not None and not self._feeder_task.done():
            self._feeder_task.cancel()
        background_tasks = tuple(self._background_tasks)
        for task in background_tasks:
            if not task.done():
                task.cancel()

        if self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except BaseException as ex:
                record_failure('tsreadex_stdin_close', ex)
        if self._process.returncode is None:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
            except BaseException as ex:
                record_failure('tsreadex_kill', ex)

        cleanup_tasks: list[asyncio.Task[None]] = []
        if self._feeder_task is not None:
            cleanup_tasks.append(asyncio.create_task(run_bounded(
                'raw_input_feeder',
                asyncio.gather(self._feeder_task, return_exceptions=True),
            )))
        if self._source_input is not None:
            cleanup_tasks.append(asyncio.create_task(run_bounded(
                'backend_input_close',
                self._source_input.close(),
            )))
        cleanup_tasks.append(asyncio.create_task(run_bounded(
            'tsreadex_wait',
            self._process.wait(),
        )))
        if background_tasks:
            cleanup_tasks.append(asyncio.create_task(run_bounded(
                'psi_background_tasks',
                asyncio.gather(*background_tasks, return_exceptions=True),
            )))
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks)

        # 最初の kill 後も process が残る場合だけ再度 reap を試み、残留を成功扱いしない。
        if self._process.returncode is None:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
            except BaseException as ex:
                record_failure('tsreadex_rekill', ex)
            await run_bounded(
                'tsreadex_reap',
                self._process.wait(),
                timeout_seconds = self.CLEANUP_REAP_TIMEOUT_SECONDS,
            )
        if self._process.returncode is None:
            record_failure(
                'tsreadex_still_running',
                LiveSourceError('shared_tsreadex_process_still_running'),
            )

        await run_bounded('psi_data_archiver_destroy', self.psi_data_archiver.destroy())

        if failures:
            labels = ','.join(label for label, _ in failures)
            cause = BaseExceptionGroup(
                'live source cleanup failures',
                [error for _, error in failures],
            )
            raise LiveSourceError(f'live_source_cleanup_failed:{labels}') from cause

    async def close(self) -> None:
        """
        raw input・tsreadex・archiverを一度だけ解放し、同じ成否を全呼び出しへ返す。

        Args:
            None

        Returns:
            None
        """

        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._closeResources(),
                name=f'TSReadExLiveSourceClose-{self._descriptor.key.asLogString()}',
            )
        await asyncio.shield(self._close_task)


async def createTSReadExLiveSourceProducer(
    descriptor: LiveSourceDescriptor,
    generation_id: int,
) -> LiveSourceProducer:
    """
    共有 backend input と `tsreadex -g` producer を起動する。

    Args:
        descriptor (LiveSourceDescriptor): 共有するライブ入力の接続記述子。
        generation_id (int): 共有 source epoch を識別する generation ID。

    Returns:
        LiveSourceProducer: 起動した共有 tsreadex producer。
    """

    is_debug_file = descriptor.debug_mode_ts_path is not None
    options = [
        '-x', '18/38/39',
        '-n', str(descriptor.key.service_id) if is_debug_file is False else '-1',
        '-A', '1',
        '-c', '5',
        '-u', '1',
        '-d', '9',
        '-g', str(generation_id),
    ]
    source_input: _LiveSourceInput | None = None
    if is_debug_file is False:
        source_input = await _openLiveSourceInput(descriptor, generation_id)
    else:
        assert descriptor.debug_mode_ts_path is not None
        source_input = _FileLiveSourceInput(descriptor.debug_mode_ts_path)
    options.append('-')

    try:
        process = await asyncio.create_subprocess_exec(
            LIBRARY_PATH['tsreadex'],
            *options,
            stdin = asyncio.subprocess.PIPE,
            stdout = asyncio.subprocess.PIPE,
            stderr = asyncio.subprocess.DEVNULL,
        )
    except BaseException as startup_failure:
        if source_input is not None:
            try:
                await source_input.close()
            except BaseException as cleanup_failure:
                raise LiveSourceCleanupError(
                    'live_source_input_startup_cleanup_failed'
                ) from BaseExceptionGroup(
                    'tsreadex startup and source input cleanup failures',
                    [startup_failure, cleanup_failure],
                )
        raise

    logging.info(
        f'[LiveSource {descriptor.key.asLogString()}] '
        f'Started shared tsreadex generation {generation_id}.'
    )
    return _TSReadExLiveSourceProducer(
        descriptor,
        generation_id,
        process,
        source_input,
    )


LIVE_SOURCE_COORDINATOR = LiveSourceCoordinator()
