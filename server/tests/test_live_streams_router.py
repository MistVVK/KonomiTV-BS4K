import asyncio
from types import SimpleNamespace

import pytest

from app.routers import LiveStreamsRouter
from app.streams.LiveStream import LiveStream
from app.streams.LiveStreamTelemetry import LiveStreamTelemetry


class _ConnectedRequest:
    """MPEG-TS generator の待機中に切断しないリクエスト代替。"""

    scope = {'state': {}}
    headers: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        """
        リクエスト接続中の状態を返す。

        Args:
            None

        Returns:
            bool: 常に False。
        """

        return False


def _create_onair_live_stream() -> LiveStream:
    """
    Router 統合テストに必要な ONAir 状態のライブストリームを作成する。

    Args:
        None

    Returns:
        LiveStream: 実際の connect() / disconnectAll() を使えるライブストリーム。
    """

    live_stream = object.__new__(LiveStream)
    live_stream.live_stream_id = 'gr011-1080p'
    live_stream._clients = []
    live_stream._status = 'ONAir'
    live_stream._detail = 'ライブストリームは ONAir です。'
    live_stream._started_at = 0
    live_stream._updated_at = 0
    live_stream._telemetry = LiveStreamTelemetry()
    live_stream._tuner_lock = asyncio.Lock()
    live_stream._retirement_lock = asyncio.Lock()
    live_stream._retirement_cleanup_task_ref = None
    live_stream._last_live_encoding_cleanup_confirmed = None
    live_stream._detached_live_encoding_task_refs = set()
    live_stream._retired_playback_sessions = {}
    live_stream._is_shutting_down = False
    live_stream._prepare_release_tasks = set()
    live_stream._prepare_cleanup_followup_tasks = {}
    live_stream._playback_session_retire_api_tasks = {}
    live_stream._playback_session_retire_api_completed_at = {}
    return live_stream


def test_live_mpegts_generators_finish_after_disconnect_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    4 本の実 Router generator が全て EOF を受け、None を HTTP body に出さず終了する。

    Args:
        monkeypatch (pytest.MonkeyPatch): Router が取得するライブストリームを差し替える fixture。

    Returns:
        None
    """

    live_stream = _create_onair_live_stream()
    monkeypatch.setattr(LiveStreamsRouter, 'LiveStream', lambda *_args: live_stream)

    def ignore_debug(_message: str) -> None:
        """
        未初期化のサーバー設定を参照する debug ログをテスト中だけ抑制する。

        Args:
            _message (str): 出力しないログメッセージ。

        Returns:
            None
        """

    monkeypatch.setattr(LiveStreamsRouter.logging, 'debug', ignore_debug)

    async def verify() -> None:
        """
        Queue 待機中の 4 本の generator を disconnectAll() で終了させる。

        Args:
            None

        Returns:
            None
        """

        stream_quality = SimpleNamespace(quality='1080p', encoding_options=None)
        responses = [
            await LiveStreamsRouter.LiveMPEGTSStreamAPI(
                _ConnectedRequest(),  # type: ignore[arg-type]
                'gr011',
                stream_quality,  # type: ignore[arg-type]
            )
            for _ in range(4)
        ]
        readers = [asyncio.create_task(anext(response.body_iterator)) for response in responses]

        # 全 generator が Queue.get() で待機してから、エンコーダー cleanup を再現する
        await asyncio.sleep(0)
        assert all(reader.done() is False for reader in readers)
        assert all(response.media_type == 'video/mp2t' for response in responses)

        live_stream.disconnectAll()

        results = await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), timeout=1.0)
        assert all(isinstance(result, StopAsyncIteration) for result in results), results
        assert live_stream.getStatus().client_count == 0

    asyncio.run(verify())
