import asyncio
from types import SimpleNamespace
from typing import Annotated, cast

import pytest
from fastapi import FastAPI, HTTPException, Query
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient
from pydantic import TypeAdapter, ValidationError

from app.metadata.RecordedPlaybackIndex import RECORDED_PLAYBACK_INDEX_VERSION
from app.routers.VideosRouter import (
    BuildRecordedPlaybackIndex,
    VideoPlaybackIndexCreateAPI,
)
from app.routers.VideoStreamsRouter import EnsurePlaybackIndexReady, GetRecordedStream
from app.streams.RecordedEncodingCodecs import AudioCodec
from app.streams.RecordedFMP4Stream import RecordedFMP4Stream
from app.streams.StreamEncodingOptions import StreamEncodingOptions


def test_recorded_audio_codec_query_accepts_two_values_and_defaults_to_aac() -> None:
    """公開query型をAAC/Opusへ限定し、既存URLはAACのままにする。"""

    adapter = TypeAdapter(AudioCodec)
    assert [adapter.validate_python(value) for value in ('aac', 'opus')] == ['aac', 'opus']
    for invalid_codec in ('passthrough', 'copy'):
        with pytest.raises(ValidationError):
            adapter.validate_python(invalid_codec)
    options = StreamEncodingOptions.fromRequest('1080p', False, False, encoder='FFmpeg')
    assert options.audio_codec == 'aac'


@pytest.mark.parametrize('invalid_codec', ['passthrough', 'copy'])
def test_invalid_recorded_audio_codec_query_returns_422(invalid_codec: str) -> None:
    """VideoStreamsRouterと同じFastAPI query型が定義外の値を422として拒否する。"""

    app = FastAPI()

    @app.get('/audio-codec')
    async def AudioCodecAPI(
        audio_codec: Annotated[AudioCodec, Query()] = 'aac',
    ) -> dict[str, str]:
        return {'audio_codec': audio_codec}

    async def Request() -> tuple[int, dict]:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/audio-codec', params={'audio_codec': invalid_codec})
        return response.status_code, cast(dict, response.json())

    status_code, body = asyncio.run(Request())
    assert status_code == 422
    assert any(error['loc'][-1] == 'audio_codec' for error in body['detail'])


def test_existing_session_rejects_changed_audio_codec() -> None:
    """同じsession IDの後続APIで要求音声方式が変わった場合は422にする。"""

    session_id = 'audio-condition-mismatch'
    recorded_program = SimpleNamespace(id=10)
    aac_options = SimpleNamespace(
        is_hevc_10bit_enabled=False,
        is_24fps_mode_enabled=False,
        video_codec='avc',
        video_bit_depth=8,
        audio_codec='aac',
    )
    stream = object.__new__(RecordedFMP4Stream)
    stream.session_id = session_id
    stream.recorded_program = recorded_program
    stream.quality = '1080p'
    stream.encoding_options = aac_options
    RecordedFMP4Stream._instances[session_id] = stream
    try:
        same_quality = SimpleNamespace(quality='1080p', encoding_options=aac_options)
        assert GetRecordedStream(session_id, recorded_program, same_quality) is stream

        opus_options = SimpleNamespace(**{**vars(aac_options), 'audio_codec': 'opus'})
        changed_quality = SimpleNamespace(quality='1080p', encoding_options=opus_options)
        with pytest.raises(HTTPException) as ex_info:
            GetRecordedStream(session_id, recorded_program, changed_quality)
        assert ex_info.value.status_code == 422
        assert ex_info.value.detail == 'Session conditions mismatch'
    finally:
        RecordedFMP4Stream._instances.pop(session_id, None)


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
        recorded_video.playback_index_version = RECORDED_PLAYBACK_INDEX_VERSION

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
    assert recorded_video.playback_index_version == RECORDED_PLAYBACK_INDEX_VERSION


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
    assert index.current_version == RECORDED_PLAYBACK_INDEX_VERSION
    assert index.progress == 0.0
    assert index.stage == 'Queued'


def test_metadata_analysis_blocks_index_enqueue(monkeypatch) -> None:
    """軽量Metadata解析がRecordedへ戻るまで、旧情報による索引生成を開始しない。"""

    recorded_video = SimpleNamespace(
        id=61,
        status='Analyzing',
        playback_index_status='Ready',
        playback_index_version=7,
        playback_indexed_at=None,
        playback_index_error_code=None,
    )

    async def RefreshFromDB() -> None:
        return None

    recorded_video.refresh_from_db = RefreshFromDB
    recorded_program = SimpleNamespace(recorded_video=recorded_video)
    enqueue_calls: list[int] = []
    monkeypatch.setattr(
        'app.routers.VideosRouter.RecordedPlaybackIndexer.enqueue',
        lambda recorded_video_id, **_kwargs: enqueue_calls.append(recorded_video_id),
    )

    index = asyncio.run(VideoPlaybackIndexCreateAPI(recorded_program))

    assert index.state == 'Stale'
    assert enqueue_calls == []
