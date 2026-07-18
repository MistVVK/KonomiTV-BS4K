import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers.VideosRouter import BuildRecordedPlaybackIndex
from app.routers.VideoStreamsRouter import EnsurePlaybackIndexReady, GetRecordedStream


def test_non_ready_recording_is_rejected_without_legacy_fallback() -> None:
    """索引未完了の録画を旧MPEG-TS経路へフォールバックしない。"""

    recorded_program = SimpleNamespace(
        recorded_video=SimpleNamespace(playback_index_status='Pending', playback_index_version=None),
    )
    stream_quality = SimpleNamespace(quality='1080p', encoding_options=None)

    with pytest.raises(HTTPException) as ex_info:
        GetRecordedStream(
            session_id='non-ready-recording',
            recorded_program=recorded_program,
            stream_quality=stream_quality,
            is_new_session_allowed=True,
        )

    assert ex_info.value.status_code == 422
    assert ex_info.value.detail['code'] == 'PlaybackIndexUnavailable'


def test_master_playlist_waits_for_on_demand_playback_index(monkeypatch) -> None:
    """未解析録画のマスタープレイリスト要求が優先度0の索引完了を待つ。"""

    recorded_video = SimpleNamespace(
        id=61,
        playback_index_status='Pending',
        playback_index_version=None,
        playback_index_error_code=None,
    )

    async def RefreshFromDB() -> None:
        recorded_video.playback_index_status = 'Ready'
        recorded_video.playback_index_version = 6

    recorded_video.refresh_from_db = RefreshFromDB
    recorded_program = SimpleNamespace(recorded_video=recorded_video)
    enqueued_priorities: list[tuple[int, int]] = []

    def Enqueue(recorded_video_id: int, priority: int) -> asyncio.Future[bool]:
        enqueued_priorities.append((recorded_video_id, priority))
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        future.set_result(True)
        return future

    monkeypatch.setattr('app.routers.VideoStreamsRouter.RecordedFMP4Stream.hasSession', lambda _session_id: False)
    monkeypatch.setattr('app.routers.VideoStreamsRouter.RecordedPlaybackIndexer.enqueue', Enqueue)

    asyncio.run(EnsurePlaybackIndexReady(recorded_program, 'new-session'))

    assert enqueued_priorities == [(61, 0)]
    assert recorded_video.playback_index_status == 'Ready'
    assert recorded_video.playback_index_version == 6


def test_stale_ready_recording_is_rejected() -> None:
    """DB上Readyでも旧Versionの録画は現行経路で再生開始しない。"""

    recorded_program = SimpleNamespace(
        recorded_video=SimpleNamespace(playback_index_status='Ready', playback_index_version=5),
    )
    stream_quality = SimpleNamespace(quality='1080p', encoding_options=None)

    with pytest.raises(HTTPException) as ex_info:
        GetRecordedStream(
            session_id='stale-ready-recording',
            recorded_program=recorded_program,
            stream_quality=stream_quality,
            is_new_session_allowed=True,
        )

    assert ex_info.value.status_code == 422
    assert ex_info.value.detail['code'] == 'PlaybackIndexUnavailable'


def test_recorded_playback_index_response_contains_stale_and_current_version() -> None:
    """状態APIはDB値を保ったままStaleとサーバーの現行Versionを返す。"""

    recorded_program = SimpleNamespace(recorded_video=SimpleNamespace(
        id=61,
        playback_index_status='Ready',
        playback_index_version=5,
        playback_indexed_at=None,
        playback_index_error_code=None,
    ))

    index = BuildRecordedPlaybackIndex(recorded_program)

    assert index.status == 'Ready'
    assert index.state == 'Stale'
    assert index.version == 5
    assert index.current_version == 6
    assert index.progress == 0.0
    assert index.stage == 'Queued'
