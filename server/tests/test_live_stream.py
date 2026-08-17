import asyncio

import pytest

from app.streams.LiveStream import LiveStream, LiveStreamClient


def test_onair_transition_resets_stream_output_watchdog_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Standby の起動時間を ONAir の無出力時間へ持ち越さないことを検証する。"""

    now = 100.0
    monkeypatch.setattr('app.streams.LiveStream.time.time', lambda: now)

    live_stream = object.__new__(LiveStream)
    live_stream.live_stream_id = 'bs4k101-1080p'
    live_stream._status = 'Standby'
    live_stream._detail = '起動中'
    live_stream._started_at = 90.0
    live_stream._updated_at = 90.0
    live_stream._stream_data_written_at = 90.0
    live_stream.tuner = None

    assert live_stream.setStatus('ONAir', 'ライブストリームは ONAir です。', quiet=True) is True
    assert live_stream.getStreamDataWrittenAt() == now


@pytest.mark.parametrize('client_count', [1, 2, 5])
def test_disconnect_all_notifies_every_client(client_count: int) -> None:
    """disconnectAll() が接続クライアント全員へ終了を通知することを検証する。"""

    live_stream = object.__new__(LiveStream)
    live_stream.live_stream_id = 'gr999-240p'
    live_stream._clients = [LiveStreamClient(live_stream, 'mpegts') for _ in range(client_count)]
    clients = live_stream._clients.copy()

    live_stream.disconnectAll()

    assert live_stream._clients == []
    assert [client._queue.get_nowait() for client in clients] == [None] * client_count


def test_write_stream_data_does_not_skip_clients_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """timeout client の削除後も残りの全クライアントへデータを配信することを検証する。"""

    now = 100.0
    monkeypatch.setattr('app.streams.LiveStream.time.time', lambda: now)

    live_stream = object.__new__(LiveStream)
    live_stream.live_stream_id = 'gr999-240p'
    live_stream._stream_data_written_at = 0.0
    live_stream._clients = [LiveStreamClient(live_stream, 'mpegts') for _ in range(4)]
    timed_out_clients = live_stream._clients[::2]
    active_clients = live_stream._clients[1::2]
    for client in timed_out_clients:
        client._stream_data_read_at = now - 11

    live_stream.writeStreamData(b'chunk')

    assert live_stream._clients == active_clients
    assert all(client._queue.empty() is True for client in timed_out_clients)
    assert [client._queue.get_nowait() for client in active_clients] == [b'chunk', b'chunk']


def test_replace_live_encoding_task_keeps_new_generation_after_old_completion() -> None:
    """旧世代のdone callbackが、登録済みの次世代Task参照を消さないことを検証する。"""

    async def scenario() -> None:
        live_stream = object.__new__(LiveStream)
        live_stream._live_encoding_task_ref = None
        live_stream._detached_live_encoding_task_refs = set()
        first_release = asyncio.Event()
        second_release = asyncio.Event()

        first = asyncio.create_task(first_release.wait())
        second = asyncio.create_task(second_release.wait())
        live_stream.replaceLiveEncodingTask(first)
        live_stream.replaceLiveEncodingTask(second)
        first_release.set()
        await first
        await asyncio.sleep(0)

        assert live_stream._live_encoding_task_ref is second
        second_release.set()
        await second

    asyncio.run(scenario())


def test_connect_waits_until_restart_generation_finishes() -> None:
    """連続Restartでも新規clientを旧世代へ登録せず、最終的な次世代Standbyまで待機させる。"""

    async def scenario() -> None:
        live_stream = object.__new__(LiveStream)
        live_stream.live_stream_id = 'bs4k101-1080p'
        live_stream._status = 'Restart'
        live_stream._clients = []
        live_stream._tuner_lock = asyncio.Lock()
        first_restart_finished_event = asyncio.Event()
        live_stream._restart_finished_event = first_restart_finished_event

        connect_task = asyncio.create_task(live_stream.connect('mpegts'))
        await asyncio.sleep(0)
        assert connect_task.done() is False
        assert live_stream._clients == []

        # 1世代目の待機解除直後に2世代目のRestartへ入っても、set済みの古いEventで空回りしない。
        live_stream._status = 'Standby'
        first_restart_finished_event.set()
        live_stream._status = 'Restart'
        second_restart_finished_event = asyncio.Event()
        live_stream._restart_finished_event = second_restart_finished_event
        await asyncio.sleep(0)
        assert connect_task.done() is False
        assert live_stream._clients == []

        live_stream._status = 'Standby'
        second_restart_finished_event.set()
        client = await connect_task

        assert live_stream._clients == [client]

    asyncio.run(scenario())
