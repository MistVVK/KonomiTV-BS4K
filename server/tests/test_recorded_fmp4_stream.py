import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.streams.RecordedFMP4Stream import (
    RecordedAudioRendition,
    RecordedFMP4Segment,
    RecordedFMP4Stream,
)


def test_active_generation_defers_session_timeout(monkeypatch) -> None:
    """エンコードやfsyncが30秒を超えても処理中のセッションを破棄しない。"""

    events: list[str] = []
    stream = object.__new__(RecordedFMP4Stream)
    stream.session_id = 'busy-session'
    stream._active_operations = 1
    RecordedFMP4Stream._instances[stream.session_id] = stream

    def KeepAlive(_self) -> None:
        events.append('keep-alive')

    async def Destroy(_self) -> None:
        events.append('destroy')

    monkeypatch.setattr(RecordedFMP4Stream, 'keepAlive', KeepAlive)
    monkeypatch.setattr(RecordedFMP4Stream, 'destroy', Destroy)
    try:
        asyncio.run(stream._RecordedFMP4Stream__destroyIfIdle())  # pyright: ignore[reportPrivateUsage]
        assert events == ['keep-alive']

        stream._active_operations = 0
        asyncio.run(stream._RecordedFMP4Stream__destroyIfIdle())  # pyright: ignore[reportPrivateUsage]
        assert events == ['keep-alive', 'destroy']
    finally:
        RecordedFMP4Stream._instances.pop(stream.session_id, None)


def test_fragmented_mp4_is_split_at_box_boundaries() -> None:
    """ftyp/moovとmoof/mdatがバイト列を壊さず分離されることを確認する。"""

    def Box(box_type: bytes, payload: bytes) -> bytes:
        """テスト用の最小ISO BMFF boxを生成する。"""

        return (8 + len(payload)).to_bytes(4, 'big') + box_type + payload

    ftyp = Box(b'ftyp', b'isom')
    moov = Box(b'moov', b'metadata')
    moof = Box(b'moof', b'fragment')
    mdat = Box(b'mdat', b'frame-data')
    init, media = RecordedFMP4Stream.splitFragmentedMP4(ftyp + moov + moof + mdat)
    assert init == ftyp + moov
    assert media == moof + mdat


def test_fragment_timeline_is_normalized_to_recording_time() -> None:
    """複数moofの相対tfdtを保ったまま録画先頭基準へ補正することを確認する。"""

    def Box(box_type: bytes, payload: bytes) -> bytes:
        """テスト用の最小ISO BMFF boxを生成する。"""

        return (8 + len(payload)).to_bytes(4, 'big') + box_type + payload

    timescale = 48_000
    mdhd = Box(
        b'mdhd',
        b'\x00\x00\x00\x00' + b'\x00' * 8 + timescale.to_bytes(4, 'big') + b'\x00' * 4,
    )
    mfhd1 = Box(b'mfhd', b'\x00\x00\x00\x00' + (1).to_bytes(4, 'big'))
    tfdt1 = Box(b'tfdt', b'\x01\x00\x00\x00' + (0).to_bytes(8, 'big'))
    mfhd2 = Box(b'mfhd', b'\x00\x00\x00\x00' + (2).to_bytes(4, 'big'))
    tfdt2 = Box(b'tfdt', b'\x01\x00\x00\x00' + (96_000).to_bytes(8, 'big'))
    media = (
        Box(b'moof', mfhd1 + Box(b'traf', tfdt1)) + Box(b'mdat', b'audio-1') +
        Box(b'moof', mfhd2 + Box(b'traf', tfdt2)) + Box(b'mdat', b'audio-2')
    )

    normalized = RecordedFMP4Stream.normalizeFragmentTimeline(mdhd, media, 303.3843, 51)
    mfhd1_offset = normalized.find(b'mfhd')
    mfhd2_offset = normalized.find(b'mfhd', mfhd1_offset + 4)
    tfdt1_offset = normalized.find(b'tfdt')
    tfdt2_offset = normalized.find(b'tfdt', tfdt1_offset + 4)
    assert int.from_bytes(normalized[mfhd1_offset + 8:mfhd1_offset + 12], 'big') == (51 << 16) + 1
    assert int.from_bytes(normalized[mfhd2_offset + 8:mfhd2_offset + 12], 'big') == (51 << 16) + 2
    base_decode_time = round(303.3843 * timescale)
    assert int.from_bytes(normalized[tfdt1_offset + 8:tfdt1_offset + 16], 'big') == base_decode_time
    assert int.from_bytes(normalized[tfdt2_offset + 8:tfdt2_offset + 16], 'big') == base_decode_time + 96_000


def test_aac_frame_limit_accounts_for_encoder_priming() -> None:
    """AAC encoderの追加packet込みで要求セグメント時間へ収まることを確認する。"""

    assert RecordedFMP4Stream.computeAACFrameLimit(0.727333) == 34
    assert RecordedFMP4Stream.computeAACFrameLimit(2.9217) == 136


def test_video_input_seek_decodes_preroll_before_exact_trim_position() -> None:
    """途中セグメントは10秒前から復号し、先頭付近では録画先頭から不足分だけ復号する。"""

    assert RecordedFMP4Stream.computeInputSeekWindow(300.3, 6.006) == (290.3, 10.0, 16.006)
    assert RecordedFMP4Stream.computeInputSeekWindow(4.0, 6.006) == (0.0, 4.0, 10.006)


def test_extract_avc_codec_string_from_actual_configuration_box() -> None:
    """avcCのprofile/compatibility/levelをcodec stringへ反映する。"""

    avcc = bytes([1, 100, 0, 40])
    box = (len(avcc) + 8).to_bytes(4, 'big') + b'avcC' + avcc
    assert RecordedFMP4Stream.extractCodecString(box) == 'avc1.640028'


def test_extract_hevc_codec_string_reverses_compatibility_flags() -> None:
    """hvcCの互換フラグとconstraintをRFC 6381形式へ変換する。"""

    hvcc = bytes([1, 2]) + bytes.fromhex('20000000') + bytes.fromhex('B00000000000') + bytes([153])
    box = (len(hvcc) + 8).to_bytes(4, 'big') + b'hvcC' + hvcc
    assert RecordedFMP4Stream.extractCodecString(box) == 'hvc1.2.4.L153.B0'


def test_extract_vp9_and_av1_codec_strings() -> None:
    """vpcCとav1Cの実profile/level/bit depthをcodec stringへ反映する。"""

    vpcc = bytes(4) + bytes([2, 40, 0xA0])
    vp9_box = (len(vpcc) + 8).to_bytes(4, 'big') + b'vpcC' + vpcc
    assert RecordedFMP4Stream.extractCodecString(vp9_box) == 'vp09.02.40.10'

    av1c = bytes([0x81, 0x0A, 0x40])
    av1_box = (len(av1c) + 8).to_bytes(4, 'big') + b'av1C' + av1c
    assert RecordedFMP4Stream.extractCodecString(av1_box) == 'av01.0.10M.10'


def test_master_playlist_uses_highest_level_across_configuration_generations() -> None:
    """先頭だけ低levelの構成でも後続initを満たすCODECS値になることを確認する。"""

    assert RecordedFMP4Stream.selectHighestCodecLevel(['avc1.640028', 'avc1.64002A']) == 'avc1.64002A'
    assert RecordedFMP4Stream.selectHighestCodecLevel(['hvc1.2.4.L90.B0', 'hvc1.2.4.L153.B0']) == \
        'hvc1.2.4.L153.B0'
    assert RecordedFMP4Stream.selectHighestCodecLevel(['vp09.02.40.10', 'vp09.02.50.10']) == 'vp09.02.50.10'
    assert RecordedFMP4Stream.selectHighestCodecLevel(['av01.0.10M.10', 'av01.0.13M.10']) == 'av01.0.13M.10'


def test_video_and_audio_playlists_share_discontinuity_boundaries(monkeypatch) -> None:
    """hls.jsが代替音声のinit PTSを対応付けられるよう、構成境界を両プレイリストで揃える。"""

    stream = object.__new__(RecordedFMP4Stream)
    stream.session_id = 'session'
    stream._segments = [
        RecordedFMP4Segment(0, 0.0, 6.0, 0, 0),
        RecordedFMP4Segment(1, 6.0, 6.0, 1, 0),
        RecordedFMP4Segment(2, 12.0, 6.0, 1, 1),
    ]
    stream.recorded_program = SimpleNamespace(recorded_video=SimpleNamespace(
        container_format='MPEG-TS',
        audio_tracks=[{'index': 1, 'stream_index': 2, 'language': 'ja'}],
    ))
    stream.encoding_options = SimpleNamespace(video_codec='avc', video_bit_depth=8, audio_codec='aac')
    monkeypatch.setattr(RecordedFMP4Stream, 'keepAlive', lambda _self: None)

    video_playlist = stream.getVideoPlaylist('cache')
    audio_playlist = stream.getAudioPlaylist('1', 'cache')

    assert video_playlist.count('#EXT-X-DISCONTINUITY') == 2
    assert audio_playlist.count('#EXT-X-DISCONTINUITY') == 2
    assert video_playlist.count('#EXT-X-MAP') == 3
    assert audio_playlist.count('#EXT-X-MAP') == 3


def test_video_color_metadata_change_starts_new_generation() -> None:
    """解像度が同じでも色特性やHDR構成が変わる境界でinit世代を更新する。"""

    base_entry = {
        'start_time': 0.0,
        'end_time': 6.0,
        'pid': 256,
        'stream_index': 0,
        'codec': 'hevc',
        'profile': 'Main 10',
        'width': 1920,
        'height': 1080,
        'frame_rate': 60.0,
        'scan_type': 'Progressive',
        'bit_depth': 10,
        'color_range': 'tv',
        'color_space': 'bt709',
        'color_primaries': 'bt709',
        'color_transfer': 'bt709',
        'mastering_display_metadata': None,
        'content_light_level': None,
    }
    hdr_entry = {
        **base_entry,
        'start_time': 6.0,
        'end_time': 12.0,
        'color_space': 'bt2020nc',
        'color_primaries': 'bt2020',
        'color_transfer': 'smpte2084',
        'mastering_display_metadata': {'max_luminance': '1000/1'},
    }
    recorded_video = SimpleNamespace(
        duration=12.0,
        video_frame_rate=60.0,
        video_stream_timeline=[base_entry, hdr_entry],
        audio_track_timeline=[],
        audio_tracks=[],
        video_codec='HEVC',
        video_resolution_width=1920,
        video_resolution_height=1080,
        video_scan_type='Progressive',
    )
    stream = object.__new__(RecordedFMP4Stream)
    stream.recorded_program = SimpleNamespace(recorded_video=recorded_video)

    segments = stream._RecordedFMP4Stream__buildSegments()  # pyright: ignore[reportPrivateUsage]

    assert [(segment.start_time, segment.generation) for segment in segments] == [(0.0, 0), (6.0, 1)]


def test_silent_audio_keeps_rendition_channel_layout() -> None:
    """Track消失区間の無音AACがレンディションと同じchannel layoutを使うことを確認する。"""

    stream = object.__new__(RecordedFMP4Stream)
    stream.recorded_program = SimpleNamespace(recorded_video=SimpleNamespace(
        audio_track_timeline=[{
            'start_time': 0.0,
            'end_time': 6.0,
            'tracks': [{
                'index': 1,
                'channel': 'Stereo',
                'channel_layout': 'stereo',
            }],
        }],
        audio_tracks=[],
    ))
    stereo = RecordedAudioRendition('1', 1, 1, 'all', 'Track 1', 'ja')
    dual_mono_sub = RecordedAudioRendition('1-sub', 1, 1, 'sub', '副音声', 'en')
    assert stream._RecordedFMP4Stream__getSilentAudioChannelLayout(stereo) == 'stereo'  # pyright: ignore[reportPrivateUsage]
    assert stream._RecordedFMP4Stream__getSilentAudioChannelLayout(dual_mono_sub) == 'mono'  # pyright: ignore[reportPrivateUsage]


def test_dual_mono_sub_rendition_is_unavailable_in_monaural_interval() -> None:
    """同じ論理Trackでもモノラル区間ではDual Monoの副音声を不在と判定する。"""

    stream = object.__new__(RecordedFMP4Stream)
    stream.recorded_program = SimpleNamespace(recorded_video=SimpleNamespace(
        audio_track_timeline=[
            {
                'start_time': 0.0,
                'end_time': 6.0,
                'tracks': [{'index': 1, 'channel': 'Monaural', 'is_dual_mono': False}],
            },
            {
                'start_time': 6.0,
                'end_time': 12.0,
                'tracks': [{'index': 1, 'channel': 'Dual Mono', 'is_dual_mono': True}],
            },
        ],
    ))
    main = RecordedAudioRendition('1-main', 1, 1, 'main', '主音声', 'ja')
    sub = RecordedAudioRendition('1-sub', 1, 1, 'sub', '副音声', 'en')
    get_availability = stream._RecordedFMP4Stream__getAudioRenditionAvailability  # pyright: ignore[reportPrivateUsage]

    assert get_availability(1.0, main) is True
    assert get_availability(1.0, sub) is False
    assert get_availability(7.0, sub) is True
    assert get_availability(13.0, sub) is None


def test_unknown_audio_timeline_falls_back_to_silence_after_source_failure(monkeypatch, tmp_path) -> None:
    """タイムライン不明時に実入力が失敗しても無音AACを生成して再生を継続する。"""

    stream = object.__new__(RecordedFMP4Stream)
    stream.recorded_program = SimpleNamespace(recorded_video=SimpleNamespace(
        id=1,
        file_path='/recording.ts',
        container_format='MPEG-TS',
        audio_track_timeline=[],
    ))
    rendition = RecordedAudioRendition('1-sub', 1, 1, 'sub', '副音声', 'en')
    segment = RecordedFMP4Segment(0, 0.0, 6.0, 0, 0)
    commands: list[list[str]] = []
    return_codes = iter([1, 0])

    class FakeProcess:
        def __init__(self, return_code: int) -> None:
            self.returncode = return_code

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b'fragment', b'test source failure' if self.returncode != 0 else b'')

    async def CreateSubprocessExec(*command, **_kwargs):
        commands.append([str(argument) for argument in command])
        return FakeProcess(next(return_codes))

    write_atomic = AsyncMock()
    monkeypatch.setattr('app.streams.RecordedFMP4Stream.asyncio.create_subprocess_exec', CreateSubprocessExec)
    monkeypatch.setattr(
        'app.streams.RecordedFMP4Stream.RecordedFMP4CacheManager.writeAtomic',
        write_atomic,
    )
    monkeypatch.setattr(RecordedFMP4Stream, 'splitFragmentedMP4', staticmethod(lambda _data: (b'init', b'media')))
    monkeypatch.setattr(
        RecordedFMP4Stream,
        'normalizeFragmentTimeline',
        staticmethod(lambda _init, media, _start_time, _sequence: media),
    )

    asyncio.run(stream._RecordedFMP4Stream__encodeAudioSegment(  # pyright: ignore[reportPrivateUsage]
        segment,
        rendition,
        tmp_path / 'init.mp4',
        tmp_path / 'segment.m4s',
    ))

    assert '/recording.ts' in commands[0]
    assert any('anullsrc=r=48000:cl=mono' in argument for argument in commands[1])
    assert write_atomic.await_count == 2


def test_ts_audio_segment_maps_saved_pid_instead_of_stream_index(monkeypatch, tmp_path) -> None:
    """TSの途中追加音声は入力位置で変動するstream indexではなくPIDから選択する。"""

    stream = object.__new__(RecordedFMP4Stream)
    stream.recorded_program = SimpleNamespace(recorded_video=SimpleNamespace(
        id=1,
        file_path='/recording.ts',
        container_format='MPEG-TS',
        audio_track_timeline=[],
    ))
    rendition = RecordedAudioRendition('2', 2, 3, 'all', 'Track 2', 'ja', 0x113)
    segment = RecordedFMP4Segment(0, 0.0, 6.0, 0, 0)
    commands: list[list[str]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b'fragment', b'')

    async def CreateSubprocessExec(*command, **_kwargs):
        commands.append([str(argument) for argument in command])
        return FakeProcess()

    monkeypatch.setattr('app.streams.RecordedFMP4Stream.asyncio.create_subprocess_exec', CreateSubprocessExec)
    monkeypatch.setattr(
        'app.streams.RecordedFMP4Stream.RecordedFMP4CacheManager.writeAtomic',
        AsyncMock(),
    )
    monkeypatch.setattr(RecordedFMP4Stream, 'splitFragmentedMP4', staticmethod(lambda _data: (b'init', b'media')))
    monkeypatch.setattr(
        RecordedFMP4Stream,
        'normalizeFragmentTimeline',
        staticmethod(lambda _init, media, _start_time, _sequence: media),
    )

    asyncio.run(stream._RecordedFMP4Stream__encodeAudioSegment(  # pyright: ignore[reportPrivateUsage]
        segment,
        rendition,
        tmp_path / 'init.mp4',
        tmp_path / 'segment.m4s',
    ))

    map_index = commands[0].index('-map')
    assert commands[0][map_index + 1] == '0:i:0x113'
