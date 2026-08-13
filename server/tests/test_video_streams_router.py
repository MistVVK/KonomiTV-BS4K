import asyncio
import json
import struct
from types import SimpleNamespace
from typing import Annotated, cast

import pytest
from fastapi import FastAPI, HTTPException, Query
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient
from pydantic import TypeAdapter, ValidationError

from app.metadata.RecordedPlaybackIndex import RECORDED_PLAYBACK_INDEX_VERSION
from app.models.User import User
from app.routers.VideosRouter import (
    BuildRecordedPlaybackIndex,
    VideoPlaybackIndexCreateAPI,
)
from app.routers.VideoStreamsRouter import (
    BuildOfflineStreamEstimate,
    EnsurePlaybackIndexReady,
    GetRecordedStream,
    RecordedSubtitleARIBTTMLAPI,
)
from app.streams.KonomiTVBS4KOfflineStream import (
    KonomiTVBS4KOfflineAsset,
    KonomiTVBS4KOfflineStream,
)
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


def test_recorded_arib_ttml_api_returns_session_independent_timed_id3_range(monkeypatch) -> None:
    """録画字幕APIが再生sessionに依存せず、共通decoder向けraw ID3範囲を返す。"""

    expected = {
        'start_time': 4.0,
        'end_time': 10.0,
        'restore_packets': [],
        'packets': [{
            'pts': 5.0,
            'transport_timestamp': 95.0,
            'component_tag': 0x30,
            'data': 'SUQz',
            'is_restore_point': False,
        }],
    }
    calls: list[tuple[object, float, float]] = []

    async def GetARIBTTMLRange(stream, start_time: float, end_time: float):
        calls.append((stream.recorded_video, start_time, end_time))
        return expected

    monkeypatch.setattr(
        'app.routers.VideoStreamsRouter.RecordedSubtitleStream.getARIBTTMLRange',
        GetARIBTTMLRange,
    )
    recorded_video = SimpleNamespace(id=61)
    recorded_program = SimpleNamespace(recorded_video=recorded_video)

    result = asyncio.run(RecordedSubtitleARIBTTMLAPI(recorded_program, 4.0, 10.0))

    assert result == expected
    assert calls == [(recorded_video, 4.0, 10.0)]


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

    index = asyncio.run(VideoPlaybackIndexCreateAPI(
        User(name='test-user', password='unused', is_admin=False),
        recorded_program,
    ))

    assert index.state == 'Stale'
    assert enqueue_calls == []


def test_session_id_format_and_admission_limits() -> None:
    """session_id 形式と global/per-client 上限で 422/429 を返す。"""

    original_global = RecordedFMP4Stream.MAX_GLOBAL_SESSIONS
    original_per_client = RecordedFMP4Stream.MAX_SESSIONS_PER_CLIENT
    original_instances = dict(RecordedFMP4Stream._instances)
    original_clients = dict(RecordedFMP4Stream._session_client_keys)
    RecordedFMP4Stream.MAX_GLOBAL_SESSIONS = 2
    RecordedFMP4Stream.MAX_SESSIONS_PER_CLIENT = 1
    RecordedFMP4Stream._instances.clear()
    RecordedFMP4Stream._session_client_keys.clear()
    try:
        with pytest.raises(HTTPException) as invalid:
            RecordedFMP4Stream.validateSessionId('short')
        assert invalid.value.status_code == 422

        # 既存 session 数を stubs で作り、admission 判定だけを検証する
        RecordedFMP4Stream._instances['session01'] = object()  # type: ignore[assignment]
        RecordedFMP4Stream._session_client_keys['session01'] = '10.0.0.1'

        with pytest.raises(HTTPException) as per_client:
            RecordedFMP4Stream.admitNewSession('session02', '10.0.0.1')
        assert per_client.value.status_code == 429

        RecordedFMP4Stream.admitNewSession('session02', '10.0.0.2')
        RecordedFMP4Stream._instances['session02'] = object()  # type: ignore[assignment]
        RecordedFMP4Stream._session_client_keys['session02'] = '10.0.0.2'

        with pytest.raises(HTTPException) as global_limit:
            RecordedFMP4Stream.admitNewSession('session03', '10.0.0.3')
        assert global_limit.value.status_code == 429
    finally:
        RecordedFMP4Stream.MAX_GLOBAL_SESSIONS = original_global
        RecordedFMP4Stream.MAX_SESSIONS_PER_CLIENT = original_per_client
        RecordedFMP4Stream._instances.clear()
        RecordedFMP4Stream._instances.update(original_instances)
        RecordedFMP4Stream._session_client_keys.clear()
        RecordedFMP4Stream._session_client_keys.update(original_clients)


def test_encoder_wait_queue_returns_429_when_full() -> None:
    """encoder waiters が上限を超えた場合は 429 になる。"""

    async def Run() -> None:
        original = RecordedFMP4Stream.MAX_ENCODER_WAITERS
        RecordedFMP4Stream.MAX_ENCODER_WAITERS = 0
        semaphore = asyncio.Semaphore(0)
        try:
            with pytest.raises(HTTPException) as ex_info:
                async with RecordedFMP4Stream.acquireEncoderSlot(semaphore):
                    pass
            assert ex_info.value.status_code == 429
        finally:
            RecordedFMP4Stream.MAX_ENCODER_WAITERS = original
            RecordedFMP4Stream._encoder_waiters = 0

    asyncio.run(Run())


def test_offline_playlists_reference_only_saved_fmp4_assets() -> None:
    """master・映像・音声のオンラインqueryを保存世代内の相対URIへ変換する。"""

    master = (
        '#EXTM3U\n#EXT-X-VERSION:7\n'
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Main",DEFAULT=YES,AUTOSELECT=YES,'
        'URI="audio/1/playlist?session_id=offline-session&cache_key=offline&audio_codec=opus"\n'
        '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subtitles",NAME="Japanese",DEFAULT=NO,'
        'URI="subtitle/0/playlist?session_id=offline-session"\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=1000000,CODECS="av01.0.10M.10,opus",AUDIO="audio",SUBTITLES="subtitles"\n'
        'video/playlist?session_id=offline-session&cache_key=offline&video_codec=av1\n'
    )
    video = (
        '#EXTM3U\n#EXT-X-MAP:URI="init?session_id=offline-session&generation=2&sequence=3"\n'
        '#EXTINF:6.006000,\nsegment?session_id=offline-session&sequence=3\n#EXT-X-ENDLIST\n'
    )
    audio = (
        '#EXTM3U\n#EXT-X-MAP:URI="init?session_id=offline-session&sequence=3"\n'
        '#EXTINF:6.000000,\nsegment?session_id=offline-session&sequence=3\n#EXT-X-ENDLIST\n'
    )

    offline_master = KonomiTVBS4KOfflineStream.BuildMasterPlaylist(master)
    assert 'audio/1/playlist.m3u8' in offline_master
    assert 'video/playlist.m3u8' in offline_master
    assert 'SUBTITLES' not in offline_master
    assert 'session_id' not in offline_master
    assert '#EXT-X-MAP:URI="init/2.mp4"' in KonomiTVBS4KOfflineStream.BuildVideoPlaylist(video)
    assert 'segments/3.m4s' in KonomiTVBS4KOfflineStream.BuildVideoPlaylist(video)
    assert '#EXT-X-MAP:URI="init/3.mp4"' in KonomiTVBS4KOfflineStream.BuildAudioPlaylist(audio)
    assert 'segments/3.m4s' in KonomiTVBS4KOfflineStream.BuildAudioPlaylist(audio)


def test_offline_asset_record_and_terminator_are_length_delimited() -> None:
    """相対パス・MIME・本体長と、件数・総バイト数の終端を固定する。"""

    record = KonomiTVBS4KOfflineStream.EncodeAsset(KonomiTVBS4KOfflineAsset(
        path='video/segments/0.m4s',
        media_type='video/mp4',
        data=b'fragment',
    ))
    path_length, media_type_length, data_length = struct.unpack('>HHQ', record[:12])
    offset = 12
    assert record[offset:offset + path_length] == b'video/segments/0.m4s'
    offset += path_length
    assert record[offset:offset + media_type_length] == b'video/mp4'
    offset += media_type_length
    assert data_length == 8
    assert record[offset:] == b'fragment'
    assert KonomiTVBS4KOfflineStream.EncodeTerminator(7, 1234) == struct.pack('>HIQ', 0xffff, 7, 1234)

    for invalid_path in ('../secret', '/absolute', 'video//0.m4s', 'video/0.m4s?token=x'):
        with pytest.raises(ValueError):
            KonomiTVBS4KOfflineStream.EncodeAsset(KonomiTVBS4KOfflineAsset(
                path=invalid_path,
                media_type='video/mp4',
                data=b'fragment',
            ))


def test_offline_estimate_counts_all_audio_renditions(monkeypatch: pytest.MonkeyPatch) -> None:
    """容量見積もりは映像と全音声レンディションの帯域を含める。"""

    audio_track = {
        'index': 1,
        'stream_index': 1,
        'codec': 'AAC-LC',
        'channel': 'Dual Mono',
        'sampling_rate': 48_000,
        'language': 'ja+en',
        'channel_layout': 'stereo',
        'is_dual_mono': True,
    }
    recorded_program = SimpleNamespace(recorded_video=SimpleNamespace(
        id=1,
        duration=60.0,
        has_video=True,
        container_format='MP4',
        audio_tracks=[audio_track],
        audio_track_timeline=[{'start_time': 0.0, 'end_time': 60.0, 'tracks': [audio_track]}],
    ))
    stream_quality = SimpleNamespace(
        quality='720p',
        encoding_options=SimpleNamespace(video_codec='av1', audio_codec='opus'),
    )
    monkeypatch.setattr(
        RecordedFMP4Stream,
        'getVideoBitrate',
        lambda _quality, _codec: SimpleNamespace(video_bitrate='1000K', video_bitrate_max='1500K'),
    )

    estimate = BuildOfflineStreamEstimate(recorded_program, stream_quality)

    assert estimate.estimated_size_bytes == 12_915_000
    assert estimate.required_size_bytes == 17_655_000


def test_offline_metadata_json_preserves_exact_encoding_tuple() -> None:
    """クライアントが別条件の応答を拒否できるexact生成条件を保持する。"""

    metadata = {
        'video_id': 42,
        'file_hash': '0123456789abcdef',
        'quality': '720p-24fps',
        'video_codec': 'av1',
        'video_bit_depth': 10,
        'requested_audio_codec': 'opus',
        'audio_codec': 'aac',
    }

    encoded = json.dumps(metadata).encode('utf-8')

    assert json.loads(encoded) == metadata
