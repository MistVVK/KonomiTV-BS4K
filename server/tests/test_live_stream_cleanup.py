import asyncio

import pytest

import app.streams.LiveStream as LiveStreamModule
from app import schemas
from app.streams.LivePrepareCoordinator import LivePrepareLeaseError
from app.streams.LiveStream import LiveStream, LiveStreamClient
from app.streams.LiveStreamTelemetry import LiveStreamTelemetry


async def _read_all_clients(clients: list[LiveStreamClient]) -> list[bytes | None]:
    """
    すべてのクライアント Queue から終了通知を読み取る。

    Args:
        clients (list[LiveStreamClient]): 終了通知を検証するクライアント一覧。

    Returns:
        list[bytes | None]: 各クライアントが受け取ったデータ。
    """

    # 旧実装で一部の Queue が EOF を受けない場合も、テスト全体を無期限に待機させない
    return await asyncio.wait_for(asyncio.gather(*(client.readStreamData() for client in clients)), timeout=1.0)


async def _read_client_data(client: LiveStreamClient, count: int) -> list[bytes | None]:
    """
    指定した数だけクライアント Queue からデータを読み取る。

    Args:
        client (LiveStreamClient): データを読み取るクライアント。
        count (int): 読み取る要素数。

    Returns:
        list[bytes | None]: Queue に書き込まれた順のデータ。
    """

    return [await client.readStreamData() for _ in range(count)]


def _create_live_stream(live_stream_id: str = 'gr011-1080p') -> LiveStream:
    """
    cleanup の単体テストに必要な状態だけを持つライブストリームを作成する。

    Args:
        live_stream_id (str): テスト対象のライブストリーム ID。

    Returns:
        LiveStream: エンコードタスクを起動しない ONAir 状態のライブストリーム。
    """

    live_stream = object.__new__(LiveStream)
    live_stream.live_stream_id = live_stream_id
    live_stream._clients = []
    live_stream._status = 'ONAir'
    live_stream._detail = 'ライブストリームは ONAir です。'
    live_stream._started_at = 0
    live_stream._updated_at = 0
    live_stream._stream_data_written_at = 0
    live_stream._telemetry = LiveStreamTelemetry()
    live_stream._prepare_release_tasks = set()
    live_stream._prepare_cleanup_followup_tasks = {}
    live_stream._playback_session_retire_api_tasks = {}
    live_stream._playback_session_retire_api_completed_at = {}
    return live_stream


class _FailingClient:
    """終了通知だけを失敗させるクライアント代替。"""

    client_type = 'mpegts'
    client_id = 'MPEGTS-failing-client'
    prepare_token = None

    def writeStreamData(self, _stream_data: bytes | None) -> None:
        """
        Queue 通知失敗を再現する。

        Args:
            _stream_data (bytes | None): ストリーム終了通知。

        Returns:
            None
        """

        raise OSError('simulated queue notification failure')


class _UnsupportedClient:
    """将来の未対応 client type を模した、書き込み回数を記録する代替。"""

    client_type = 'unsupported'
    client_id = 'Unsupported-client'
    prepare_token = None

    def __init__(self) -> None:
        """
        書き込み回数を初期化する。

        Args:
            None

        Returns:
            None
        """

        self.write_count = 0


    def writeStreamData(self, _stream_data: bytes | None) -> None:
        """
        未対応 client type に誤って EOF を書き込んだ回数を記録する。

        Args:
            _stream_data (bytes | None): 書き込み要求されたデータ。

        Returns:
            None
        """

        self.write_count += 1


@pytest.mark.parametrize(
    ('live_stream_id', 'timeout', 'expired_flags'),
    [
        ('gr011-1080p', 10, [True, False, False]),
        ('gr011-1080p', 10, [True, False, True, False, True, False]),
        ('gr011-1080p', 10, [True, True, False]),
        ('bs4k101-1080p', 30, [True, False, True, False, True, False]),
    ],
    ids = [
        'expired-healthy-healthy',
        'alternating-expired-and-healthy',
        'consecutive-expired-before-healthy',
        'bs4k-alternating-expired-and-healthy',
    ],
)
def test_write_stream_data_removes_only_expired_clients_and_delivers_chunk(
    monkeypatch: pytest.MonkeyPatch,
    live_stream_id: str,
    timeout: int,
    expired_flags: list[bool],
) -> None:
    """
    timeout client の位置にかかわらず、全 healthy client へ同じ TS chunk を一度だけ配信する。

    Args:
        monkeypatch (pytest.MonkeyPatch): 時刻とログ出力を固定する fixture。
        live_stream_id (str): timeout 分岐を選ぶライブストリーム ID。
        timeout (int): テスト対象の timeout 秒数。
        expired_flags (list[bool]): クライアントごとの timeout 状態。

    Returns:
        None
    """

    now = 1_000.0
    stream_data = b'mpegts-chunk'
    live_stream = _create_live_stream(live_stream_id)
    clients = [LiveStreamClient(live_stream, 'mpegts') for _ in expired_flags]
    logged_messages: list[str] = []

    # 反復順序とログ内容を決定的に検証できるよう、各 client の状態と ID を固定する
    for index, (client, is_expired) in enumerate(zip(clients, expired_flags, strict=True)):
        client.client_id = f'MPEGTS-test-{index}'
        client._stream_data_read_at = now - (timeout + 0.001 if is_expired else 0)
    live_stream._clients = clients.copy()
    monkeypatch.setattr(LiveStreamModule.time, 'time', lambda: now)
    monkeypatch.setattr(LiveStreamModule.logging, 'info', logged_messages.append)

    live_stream.writeStreamData(stream_data)

    healthy_clients = [client for client, is_expired in zip(clients, expired_flags, strict=True) if not is_expired]
    expired_clients = [client for client, is_expired in zip(clients, expired_flags, strict=True) if is_expired]
    assert live_stream._clients == healthy_clients
    assert live_stream.getStatus().client_count == len(healthy_clients)
    assert [client._queue.get_nowait() for client in healthy_clients] == [stream_data] * len(healthy_clients)
    assert all(client._queue.empty() for client in healthy_clients)
    assert [client._queue.get_nowait() for client in expired_clients] == [None] * len(expired_clients)
    assert all(client._queue.empty() for client in expired_clients)
    assert live_stream.getStreamDataWrittenAt() == now
    assert logged_messages == [
        f'{live_stream.log_prefix} Client Disconnected (Timeout). Client ID: {client.client_id}'
        for client in expired_clients
    ]


@pytest.mark.parametrize(
    ('live_stream_id', 'timeout'),
    [
        ('gr011-1080p', 10),
        ('bs4k101-1080p', 30),
    ],
)
def test_write_stream_data_disconnects_only_after_timeout_boundary(
    monkeypatch: pytest.MonkeyPatch,
    live_stream_id: str,
    timeout: int,
) -> None:
    """
    通常10秒・BS4K 30秒の境界ちょうどでは維持し、境界を超えた client だけを削除する。

    Args:
        monkeypatch (pytest.MonkeyPatch): 現在時刻を固定する fixture。
        live_stream_id (str): timeout 分岐を選ぶライブストリーム ID。
        timeout (int): 期待する timeout 秒数。

    Returns:
        None
    """

    now = 1_000.0
    stream_data = b'mpegts-chunk'
    live_stream = _create_live_stream(live_stream_id)
    boundary_client = LiveStreamClient(live_stream, 'mpegts')
    expired_client = LiveStreamClient(live_stream, 'mpegts')
    boundary_client._stream_data_read_at = now - timeout
    expired_client._stream_data_read_at = now - timeout - 0.001
    live_stream._clients = [boundary_client, expired_client]
    monkeypatch.setattr(LiveStreamModule.time, 'time', lambda: now)

    live_stream.writeStreamData(stream_data)

    assert live_stream._clients == [boundary_client]
    assert live_stream.getStatus().client_count == 1
    assert boundary_client._queue.get_nowait() == stream_data
    assert boundary_client._queue.empty()
    assert expired_client._queue.get_nowait() is None
    assert expired_client._queue.empty()


def test_write_stream_data_updates_written_at_only_for_non_empty_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    空 chunk では最終書込時刻を維持し、非空 chunk でだけ現在時刻へ更新する。

    Args:
        monkeypatch (pytest.MonkeyPatch): 現在時刻を固定する fixture。

    Returns:
        None
    """

    now = 1_000.0
    live_stream = _create_live_stream()
    live_stream._stream_data_written_at = 500.0
    monkeypatch.setattr(LiveStreamModule.time, 'time', lambda: now)

    live_stream.writeStreamData(b'')
    assert live_stream.getStreamDataWrittenAt() == 500.0

    live_stream.writeStreamData(b'mpegts-chunk')
    assert live_stream.getStreamDataWrittenAt() == now


@pytest.mark.parametrize('client_count', range(6))
def test_disconnect_all_notifies_every_client_and_is_idempotent(client_count: int) -> None:
    """
    複数クライアントを反復中に削除せず、全 Queue へ一度だけ EOF を通知する。

    Args:
        None

    Returns:
        None
    """

    live_stream = _create_live_stream()
    clients = [LiveStreamClient(live_stream, 'mpegts') for _ in range(client_count)]
    live_stream._clients = clients

    live_stream.disconnectAll()

    assert live_stream.getStatus().client_count == 0
    assert asyncio.run(_read_all_clients(clients)) == [None] * client_count

    # 二回目は管理対象が空なので、すでに終端通知を消費した Queue へ追加通知しない
    live_stream.disconnectAll()
    assert all(client._queue.empty() for client in clients)


def test_disconnect_all_replaces_queued_data_with_eof() -> None:
    """
    終了時は滞留 MPEG-TS データを破棄し、待機中の応答へ EOF を直ちに届ける。

    Args:
        None

    Returns:
        None
    """

    live_stream = _create_live_stream()
    client = LiveStreamClient(live_stream, 'mpegts')
    client.writeStreamData(b'existing-mpegts-data')
    live_stream._clients = [client]

    live_stream.disconnectAll()

    assert asyncio.run(_read_client_data(client, 1)) == [None]


def test_disconnect_all_ignores_unsupported_client_type() -> None:
    """
    未対応 client type を管理リストから外しつつ、MPEG-TS にだけ EOF を通知する。

    Args:
        None

    Returns:
        None
    """

    live_stream = _create_live_stream()
    mpegts_client = LiveStreamClient(live_stream, 'mpegts')
    unsupported_client = _UnsupportedClient()
    live_stream._clients = [mpegts_client, unsupported_client]  # type: ignore[list-item]

    live_stream.disconnectAll()

    assert live_stream._clients == []
    assert asyncio.run(mpegts_client.readStreamData()) is None
    assert unsupported_client.write_count == 0


def test_disconnect_all_continues_after_client_notification_failure() -> None:
    """
    一つの Queue 通知が失敗しても、後続クライアントの終了通知を残留させない。

    Args:
        None

    Returns:
        None
    """

    live_stream = _create_live_stream()
    waiting_client = LiveStreamClient(live_stream, 'mpegts')
    live_stream._clients = [_FailingClient(), waiting_client]  # type: ignore[list-item]

    live_stream.disconnectAll()

    assert len(live_stream._clients) == 0
    assert asyncio.run(waiting_client.readStreamData()) is None


def test_shutdown_cancels_expiry_active_and_detached_tasks_and_closes_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    shutdownはHTTP外の全taskを回収し、task参照消失時もsource購読を閉じる。

    Args:
        monkeypatch (pytest.MonkeyPatch): singleton状態を分離する fixture。

    Returns:
        None
    """

    class _SourceSubscription:
        """shutdown fallbackのclose呼び出しを記録する。"""

        def __init__(self) -> None:
            """
            購読代替を初期化する。

            Returns:
                None
            """

            self.close_count = 0
            self.keep_source_alive: bool | None = None

        async def close(self, *, keep_source_alive: bool) -> None:
            """
            close引数を記録する。

            Args:
                keep_source_alive (bool): source producerを維持するか。

            Returns:
                None
            """

            self.close_count += 1
            self.keep_source_alive = keep_source_alive

    async def run() -> None:
        """
        shutdown後に全task・client・sourceが回収済みになることを検証する。

        Returns:
            None
        """

        monkeypatch.setattr(LiveStream, '_LiveStream__instances', {})
        live_stream = LiveStream('gr011', '1080p')
        live_stream._status = 'ONAir'
        client = LiveStreamClient(
            live_stream,
            'mpegts',
            playback_session_id = '88888888-8888-4888-8888-888888888888',
        )
        client.writeStreamData(b'queued')
        live_stream._clients.append(client)
        source_subscription = _SourceSubscription()
        live_stream._source_subscription = source_subscription  # type: ignore[assignment]

        async def pending() -> None:
            """
            shutdownまで残るbackground taskを模擬する。

            Returns:
                None
            """

            await asyncio.Event().wait()

        expiry_task = asyncio.create_task(pending())
        active_task = asyncio.create_task(pending())
        detached_task = asyncio.create_task(pending())
        live_stream._prepare_expiry_task = expiry_task
        live_stream._live_encoding_task_ref = active_task
        live_stream._detached_live_encoding_task_refs.add(detached_task)

        assert await live_stream.shutdown() is True
        assert expiry_task.done() is True
        assert active_task.done() is True
        assert detached_task.done() is True
        assert live_stream.getStatus().status == 'Offline'
        assert live_stream.getStatus().client_count == 0
        assert await client.readStreamData() is None
        assert source_subscription.close_count == 1
        assert source_subscription.keep_source_alive is False
        with pytest.raises(LivePrepareLeaseError, match='live_stream_shutting_down'):
            await live_stream.connect(
                'mpegts',
                playback_session_id = '99999999-9999-4999-8999-999999999999',
            )

    asyncio.run(run())


def test_connect_observes_failed_done_callback_before_pruning_detached_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    完了済みdetached taskのcallback待ち1 tickを挟み、cleanup失敗世代へ重ねない。

    Args:
        monkeypatch (pytest.MonkeyPatch): singleton状態を分離するfixture。

    Returns:
        None
    """

    class _FailedCleanupEncodingTask:
        """cleanup失敗を公開する完了済みLiveEncodingTask代替。"""

        def isCleanupConfirmed(self) -> bool:
            """
            cleanup未確認を返す。

            Returns:
                bool: 常にFalse。
            """

            return False

        def getCleanupFailures(self) -> tuple[str, ...]:
            """
            未回収資源名を返す。

            Returns:
                tuple[str, ...]: encoder process残留を示すtuple。
            """

            return ('process:encoder',)

    async def run() -> None:
        """
        callback未実行のdone taskをconnectが成功扱いしないことを検証する。

        Returns:
            None
        """

        monkeypatch.setattr(LiveStream, '_LiveStream__instances', {})
        live_stream = LiveStream('gr011', '1080p')

        async def completed_task() -> None:
            """
            直ちに完了する旧encoder世代を模擬する。

            Returns:
                None
            """

        detached_task = asyncio.create_task(completed_task())
        await detached_task
        failed_cleanup = _FailedCleanupEncodingTask()
        live_stream._LiveStream__registerLiveEncodingTaskRef(  # pyright: ignore[reportPrivateUsage]
            detached_task,
            failed_cleanup,  # type: ignore[arg-type]
        )
        live_stream._LiveStream__detachLiveEncodingTaskRef(  # pyright: ignore[reportPrivateUsage]
            detached_task,
        )

        with pytest.raises(LivePrepareLeaseError, match='playback_session_cleanup_failed'):
            await live_stream.connect(
                'mpegts',
                playback_session_id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            )

        assert live_stream._last_live_encoding_cleanup_confirmed is False
        assert live_stream._detached_live_encoding_task_refs == set()
        assert live_stream.getStatus().client_count == 0
        assert live_stream._live_encoding_task_ref is None

    asyncio.run(run())


def test_shutdown_drains_pending_retire_api_settlement_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    requestから分離したRetire settlement taskもLiveStream shutdown完了前にdrainする。

    Args:
        monkeypatch (pytest.MonkeyPatch): singleton状態を分離するfixture。

    Returns:
        None
    """

    async def run() -> None:
        """
        pending settlementが終わるまでshutdownが完了しないことを検証する。

        Returns:
            None
        """

        monkeypatch.setattr(LiveStream, '_LiveStream__instances', {})
        live_stream = LiveStream('gr011', '1080p')
        settlement_started = asyncio.Event()
        settlement_release = asyncio.Event()

        async def settlement_operation() -> schemas.LivePlaybackSessionRetireResponse:
            """
            外部許可まで完了しないRetire settlementを模擬する。

            Returns:
                schemas.LivePlaybackSessionRetireResponse: cleanup成功結果。
            """

            settlement_started.set()
            await settlement_release.wait()
            return schemas.LivePlaybackSessionRetireResponse(
                accepted = True,
                result = 'cleanup_completed',
                reason = None,
                cleanup_confirmed = True,
                terminal_restart_required = False,
            )

        settlement_task = live_stream.schedulePlaybackSessionRetireAPI(
            'cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd',
            settlement_operation,
        )
        await settlement_started.wait()
        shutdown_task = asyncio.create_task(live_stream.shutdown())
        await asyncio.sleep(0)
        assert shutdown_task.done() is False
        assert settlement_task.done() is False

        settlement_release.set()
        assert await asyncio.wait_for(shutdown_task, timeout=1) is True
        assert settlement_task.done() is True
        assert settlement_task.result().accepted is True

    asyncio.run(run())


def test_retire_api_rejects_new_uuid_when_inflight_capacity_is_exhausted() -> None:
    """
    異なる UUID の Retire が上限まで保留中なら、新規要求を追跡せず terminal failure で拒否する。

    Args:
        None

    Returns:
        None
    """

    async def run() -> None:
        """保留中 task 数・同一 UUID 共有・上限超過結果を検証する。"""

        live_stream = _create_live_stream()
        operation_release = asyncio.Event()

        async def pending_operation() -> schemas.LivePlaybackSessionRetireResponse:
            """明示解放まで完了しない Retire operation を模擬する。"""

            await operation_release.wait()
            return schemas.LivePlaybackSessionRetireResponse(
                accepted = True,
                result = 'cleanup_completed',
                reason = None,
                cleanup_confirmed = True,
                terminal_restart_required = False,
            )

        retire_tasks: list[asyncio.Task[schemas.LivePlaybackSessionRetireResponse]] = []
        for index in range(LiveStream.PLAYBACK_SESSION_RETIRE_MAX_INFLIGHT):
            playback_session_id = f'{index:08x}-0000-4000-8000-{index:012x}'
            retire_tasks.append(
                live_stream.schedulePlaybackSessionRetireAPI(
                    playback_session_id,
                    pending_operation,
                )
            )

        duplicate_task = live_stream.schedulePlaybackSessionRetireAPI(
            '00000000-0000-4000-8000-000000000000',
            pending_operation,
        )
        assert duplicate_task is retire_tasks[0]

        rejected_task = live_stream.schedulePlaybackSessionRetireAPI(
            'ffffffff-ffff-4fff-8fff-ffffffffffff',
            pending_operation,
        )
        rejected = await rejected_task
        assert rejected.accepted is False
        assert rejected.result == 'cleanup_failed'
        assert rejected.reason == 'playback_session_retire_capacity_exceeded'
        assert rejected.cleanup_confirmed is False
        assert rejected.terminal_restart_required is True
        assert len(live_stream._playback_session_retire_api_tasks) == (
            LiveStream.PLAYBACK_SESSION_RETIRE_MAX_INFLIGHT
        )

        operation_release.set()
        completed = await asyncio.gather(*retire_tasks)
        assert all(result.accepted is True for result in completed)

    asyncio.run(run())
