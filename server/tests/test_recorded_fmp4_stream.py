import asyncio
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.constants import QUALITY_TYPES
from app.streams.RecordedEncodingCodecs import AudioCodec, VideoCodec
from app.streams.RecordedFMP4Stream import (
    RecordedAudioRendition,
    RecordedFMP4Segment,
    RecordedFMP4Stream,
    RecordedVideoBitrate,
)


def test_subprocess_is_killed_and_reaped_when_communication_is_cancelled() -> None:
    """録画セグメント生成のキャンセル時にFFmpegを残留させない。"""

    class CancelledProcess:
        returncode: int | None = None

        def __init__(self) -> None:
            self.kill_count = 0
            self.wait_count = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            raise asyncio.CancelledError

        def kill(self) -> None:
            self.kill_count += 1

        async def wait(self) -> int:
            self.wait_count += 1
            self.returncode = -9
            return self.returncode

    process = CancelledProcess()

    async def Communicate() -> None:
        with pytest.raises(asyncio.CancelledError):
            await RecordedFMP4Stream._RecordedFMP4Stream__communicateSubprocess(  # pyright: ignore[reportAttributeAccessIssue]
                process,  # pyright: ignore[reportArgumentType]
            )

    asyncio.run(Communicate())

    assert process.kill_count == 1
    assert process.wait_count == 1
    assert process.returncode == -9


def test_ffmpeg_media_time_progress_is_reported_monotonically() -> None:
    """FFmpegの媒体時刻を対象時間で正規化し、終了時に1へ到達させる。"""

    class CompletedProcess:
        returncode: int | None = None

        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(
                b'out_time_us=1000000\nprogress=continue\n'
                b'out_time_us=4000000\nprogress=continue\n'
                b'out_time_us=3000000\nprogress=continue\n'
            )
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        async def wait(self) -> int:
            self.returncode = 0
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    stream = object.__new__(RecordedFMP4Stream)
    progresses: list[float] = []

    async def Communicate() -> tuple[bytes, int]:
        return await stream._RecordedFMP4Stream__communicateSubprocessWithProgress(  # pyright: ignore[reportPrivateUsage]
            CompletedProcess(),  # pyright: ignore[reportArgumentType]
            10.0,
            lambda progress: progresses.append(progress),
        )

    stderr, returncode = asyncio.run(Communicate())

    assert stderr == b''
    assert returncode == 0
    assert progresses == [0.1, 0.4, 0.4, 1.0]


def test_offline_progress_weights_video_and_audio_work() -> None:
    """映像ありでは映像90%、複数音声全体10%を媒体時間比で配分する。"""

    stream = object.__new__(RecordedFMP4Stream)
    stream._is_offline_continuous = True
    stream._segments = [
        RecordedFMP4Segment(0, 0.0, 6.0, 0, 0, 0, 0, 288_000),
        RecordedFMP4Segment(1, 6.0, 6.0, 0, 0, 0, 288_000, 288_000),
    ]
    stream.recorded_program = SimpleNamespace(recorded_video=SimpleNamespace(has_video=True))
    renditions = [
        RecordedAudioRendition('1', 1, 1, 'all', 'Track 1', 'ja'),
        RecordedAudioRendition('2', 2, 2, 'all', 'Track 2', 'en'),
    ]
    stream.getAudioRenditions = lambda: renditions
    progresses: list[float] = []

    stream.setOfflineProgressCallback(progresses.append)
    stream._RecordedFMP4Stream__updateOfflineWorkProgress(('Video', '0'), 0.5)  # pyright: ignore[reportPrivateUsage]
    stream._RecordedFMP4Stream__updateOfflineWorkProgress(('Audio', '1:0'), 1.0)  # pyright: ignore[reportPrivateUsage]
    stream._RecordedFMP4Stream__updateOfflineWorkProgress(('Audio', '2:0'), 1.0)  # pyright: ignore[reportPrivateUsage]

    assert progresses == pytest.approx([0.0, 0.45, 0.50, 0.55])


def _build_aac_init(
    audio_object_type: int = 2,
    sampling_frequency_index: int = 3,
    channels: int = 2,
    sample_entry_channels: int | None = None,
) -> bytes:
    """テスト用の最小AAC AudioSpecificConfig入りinitデータを生成する。"""

    bits = f'{audio_object_type:05b}{sampling_frequency_index:04b}{channels:04b}'
    bits = bits.ljust((len(bits) + 7) // 8 * 8, '0')
    audio_specific_config = int(bits, 2).to_bytes(len(bits) // 8, 'big')

    def Box(box_type: bytes, payload: bytes) -> bytes:
        """size/typeを持つ最小ISO BMFF boxを生成する。"""

        return (8 + len(payload)).to_bytes(4, 'big') + box_type + payload

    sample_entry_payload = b'' if sample_entry_channels is None else (
        bytes(16) + sample_entry_channels.to_bytes(2, 'big')
    )
    return Box(b'mp4a', sample_entry_payload) + Box(
        b'esds',
        bytes(4) + bytes([0x05, len(audio_specific_config)]) + audio_specific_config,
    )


def _build_opus_init(channels: int = 2) -> bytes:
    """テスト用の最小Opus sample entryとdOpsデータを生成する。"""

    def Box(box_type: bytes, payload: bytes) -> bytes:
        return (8 + len(payload)).to_bytes(4, 'big') + box_type + payload

    dops_payload = (
        bytes([0, channels]) +
        RecordedFMP4Stream.OPUS_ENCODER_DELAY_SAMPLES.to_bytes(2, 'big') +
        RecordedFMP4Stream.AUDIO_SAMPLE_RATE.to_bytes(4, 'big') +
        bytes(3)
    )
    return Box(b'Opus', b'') + Box(b'dOps', dops_payload)


def _build_audio_fragment(
    moof_sample_durations: list[list[int]],
    first_decode_time: int = 0,
    gap_before_moof: int = 0,
    audio_init: bytes | None = None,
) -> tuple[bytes, bytes]:
    """48kHz音声のtrun/tfdtを持つテスト用fMP4 fragmentを生成する。"""

    def Box(box_type: bytes, payload: bytes) -> bytes:
        return (8 + len(payload)).to_bytes(4, 'big') + box_type + payload

    timescale = 48_000
    mdhd = Box(
        b'mdhd',
        b'\x00\x00\x00\x00' + b'\x00' * 8 + timescale.to_bytes(4, 'big') + b'\x00' * 4,
    )
    trex = Box(
        b'trex',
        b'\x00\x00\x00\x00' + (1).to_bytes(4, 'big') + (1).to_bytes(4, 'big') +
        (1024).to_bytes(4, 'big') + b'\x00' * 8,
    )
    init = (audio_init or _build_aac_init()) + \
        Box(b'moov', Box(b'trak', Box(b'mdia', mdhd)) + Box(b'mvex', trex))
    media = bytearray()
    decode_time = first_decode_time
    for index, durations in enumerate(moof_sample_durations):
        if index == 1:
            decode_time += gap_before_moof
        mfhd = Box(b'mfhd', b'\x00\x00\x00\x00' + (index + 1).to_bytes(4, 'big'))
        tfhd = Box(b'tfhd', b'\x00\x00\x00\x00' + (1).to_bytes(4, 'big'))
        tfdt = Box(b'tfdt', b'\x01\x00\x00\x00' + decode_time.to_bytes(8, 'big'))
        trun = Box(
            b'trun',
            b'\x00\x00\x01\x00' + len(durations).to_bytes(4, 'big') +
            b''.join(duration.to_bytes(4, 'big') for duration in durations),
        )
        media += Box(b'moof', mfhd + Box(b'traf', tfhd + tfdt + trun))
        media += Box(b'mdat', bytes(len(durations)))
        decode_time += sum(durations)
    return init, bytes(media)


def _build_splittable_audio_fmp4(
    sample_durations: list[int],
    sample_sizes: list[int],
    audio_init: bytes | None = None,
    use_default_sample_size: bool = False,
    payload_suffix: bytes = b'',
    extra_top_level_box: bytes = b'',
    extra_traf_box: bytes = b'',
) -> tuple[bytes, bytes]:
    """厳密packet分割テスト用の単一moof/traf/trun音声fMP4を生成する。

    Args:
        sample_durations: 各packetの48kHz sample duration。
        sample_sizes: 各packetのmdat payload byte数。
        audio_init: moovへ格納するcodec初期化box。
        use_default_sample_size: sample sizeをtrunではなくtfhd既定値へ格納するか。
        payload_suffix: trunから参照されない余剰mdat payload。
        extra_top_level_box: moovとmoofの間へ挿入する未知box。
        extra_traf_box: tfdtとtrunの間へ挿入する未知box。

    Returns:
        自己完結fMP4と元mdat payload。
    """

    if len(sample_durations) != len(sample_sizes) or len(sample_durations) == 0:
        raise ValueError('Sample durations and sizes must have the same positive length.')
    if use_default_sample_size and len(set(sample_sizes)) != 1:
        raise ValueError('Default sample size requires every packet to have the same size.')

    def Box(box_type: bytes, payload: bytes) -> bytes:
        """テスト用の32bit size ISO BMFF boxを生成する。

        Args:
            box_type: 4byteのbox type。
            payload: box payload。

        Returns:
            box headerとpayloadを連結したbytes。
        """

        return (8 + len(payload)).to_bytes(4, 'big') + box_type + payload

    timescale = RecordedFMP4Stream.AUDIO_SAMPLE_RATE
    mdhd = Box(
        b'mdhd',
        b'\x00\x00\x00\x00' + b'\x00' * 8 + timescale.to_bytes(4, 'big') + b'\x00' * 4,
    )
    trex = Box(
        b'trex',
        b'\x00\x00\x00\x00' + (1).to_bytes(4, 'big') + (1).to_bytes(4, 'big') +
        sample_durations[0].to_bytes(4, 'big') + sample_sizes[0].to_bytes(4, 'big') +
        b'\x00' * 4,
    )
    moov = Box(
        b'moov',
        Box(b'trak', Box(b'mdia', mdhd + (audio_init or _build_aac_init()))) +
        Box(b'mvex', trex),
    )
    ftyp = Box(b'ftyp', b'iso6\x00\x00\x02\x00iso6mp41')

    tfhd_flags = 0x020020
    tfhd_payload = tfhd_flags.to_bytes(4, 'big') + (1).to_bytes(4, 'big')
    if use_default_sample_size:
        tfhd_flags |= 0x000010
        tfhd_payload = tfhd_flags.to_bytes(4, 'big') + (1).to_bytes(4, 'big') + \
            sample_sizes[0].to_bytes(4, 'big')
    tfhd_payload += (0).to_bytes(4, 'big')
    tfhd = Box(b'tfhd', tfhd_payload)
    tfdt = Box(b'tfdt', b'\x01\x00\x00\x00' + (0).to_bytes(8, 'big'))
    mfhd = Box(b'mfhd', b'\x00\x00\x00\x00' + (1).to_bytes(4, 'big'))

    trun_flags = 0x000101 | (0 if use_default_sample_size else 0x000200)
    trun_entries = bytearray()
    for duration, size in zip(sample_durations, sample_sizes, strict=True):
        trun_entries.extend(duration.to_bytes(4, 'big'))
        if use_default_sample_size is False:
            trun_entries.extend(size.to_bytes(4, 'big'))

    def BuildMoof(data_offset: int) -> bytes:
        """指定data_offsetを持つテスト用moofを生成する。

        Args:
            data_offset: moof先頭からmdat payload先頭までの相対位置。

        Returns:
            単一traf/trunを持つmoof。
        """

        trun = Box(
            b'trun',
            trun_flags.to_bytes(4, 'big') + len(sample_durations).to_bytes(4, 'big') +
            data_offset.to_bytes(4, 'big', signed=True) + bytes(trun_entries),
        )
        return Box(b'moof', mfhd + Box(b'traf', tfhd + tfdt + extra_traf_box + trun))

    provisional_moof = BuildMoof(0)
    moof = BuildMoof(len(provisional_moof) + 8)
    payload = b''.join(
        bytes([packet_index % 251]) * size
        for packet_index, size in enumerate(sample_sizes)
    ) + payload_suffix
    mdat = Box(b'mdat', payload)
    mfra = Box(b'mfra', Box(b'mfro', b'\x00\x00\x00\x00' + (24).to_bytes(4, 'big')))
    return ftyp + moov + extra_top_level_box + moof + mdat + mfra, payload


def _extract_fragment_mdat_payload(fragment: bytes) -> bytes:
    """テスト用のmoof+mdat fragmentからmdat payloadを返す。

    Args:
        fragment: 厳密packet分割が返したメディアfragment。

    Returns:
        mdat headerを除いた圧縮packet列。
    """

    moof_size = int.from_bytes(fragment[0:4], 'big')
    if fragment[4:8] != b'moof' or fragment[moof_size + 4:moof_size + 8] != b'mdat':
        raise ValueError('The test fragment does not contain adjacent moof and mdat boxes.')
    mdat_size = int.from_bytes(fragment[moof_size:moof_size + 4], 'big')
    return fragment[moof_size + 8:moof_size + mdat_size]


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


@pytest.mark.parametrize(
    ('audio_codec', 'sample_durations', 'sample_sizes', 'segment_sample_counts', 'use_default_sample_size'),
    [
        ('aac', [1024, 1024, 1024, 544], [5, 7, 6, 4], [2048, 1568], False),
        ('opus', [960, 959, 961, 960, 312], [3, 3, 3, 3, 3], [2880, 1272], True),
    ],
)
def test_audio_fmp4_packet_split_preserves_timeline_and_payload(
    audio_codec: AudioCodec,
    sample_durations: list[int],
    sample_sizes: list[int],
    segment_sample_counts: list[int],
    use_default_sample_size: bool,
) -> None:
    """AAC/Opusの補償packetと末尾partial packetを変形せず複数境界へ分割する。"""

    source, source_payload = _build_splittable_audio_fmp4(
        sample_durations,
        sample_sizes,
        audio_init=_build_opus_init() if audio_codec == 'opus' else _build_aac_init(),
        use_default_sample_size=use_default_sample_size,
    )

    init_data, media_fragments = RecordedFMP4Stream.splitTranscodedAudioFMP4(
        source,
        segment_sample_counts,
    )

    assert len(media_fragments) == len(segment_sample_counts)
    assert b'ftyp' in init_data and b'moov' in init_data
    packet_offset = 0
    for media_data, expected_sample_count in zip(media_fragments, segment_sample_counts, strict=True):
        info = RecordedFMP4Stream.inspectAudioFragment(init_data, media_data)
        assert info is not None
        expected_packet_durations: list[int] = []
        accumulated_samples = 0
        while accumulated_samples < expected_sample_count:
            duration = sample_durations[packet_offset]
            expected_packet_durations.append(duration)
            accumulated_samples += duration
            packet_offset += 1
        assert accumulated_samples == expected_sample_count
        assert info.first_decode_time == 0
        assert info.total_duration == expected_sample_count
        assert info.sample_durations == tuple(expected_packet_durations)
        assert RecordedFMP4Stream.validateTranscodedAudioFragmentTimeline(
            init_data,
            media_data,
            audio_codec,
            expected_start_sample=0,
            expected_sample_count=expected_sample_count,
            expected_channel_count=2,
        ) is True
    assert packet_offset == len(sample_durations)
    assert b''.join(_extract_fragment_mdat_payload(fragment) for fragment in media_fragments) == source_payload


@pytest.mark.parametrize(
    ('audio_codec', 'frame_samples', 'partial_samples'),
    [
        ('aac', 1024, 544),
        ('opus', 960, 312),
    ],
)
def test_audio_generation_validator_accepts_compensation_pair_across_fragment_boundary(
    audio_codec: AudioCodec,
    frame_samples: int,
    partial_samples: int,
) -> None:
    """AAC/Opusの隣接補償ペアがHLS境界をまたいでもgeneration全体では厳密一致する。"""

    sample_durations = [
        frame_samples,
        frame_samples - 1,
        frame_samples + 1,
        frame_samples,
        partial_samples,
    ]
    segment_sample_counts = [
        frame_samples * 2 - 1,
        frame_samples * 2 + 1 + partial_samples,
    ]
    source, source_payload = _build_splittable_audio_fmp4(
        sample_durations,
        [3] * len(sample_durations),
        audio_init=_build_opus_init() if audio_codec == 'opus' else _build_aac_init(),
    )

    init_data, media_fragments = RecordedFMP4Stream.splitTranscodedAudioFMP4(
        source,
        segment_sample_counts,
    )

    # 境界直後の+1 sampleは前fragment末尾の-1 sampleと対になるため、後半単体では判定できない。
    assert RecordedFMP4Stream.validateTranscodedAudioFragmentTimeline(
        init_data,
        media_fragments[1],
        audio_codec,
        expected_start_sample=0,
        expected_sample_count=segment_sample_counts[1],
    ) is False
    assert RecordedFMP4Stream.validateTranscodedAudioFragmentTimeline(
        init_data,
        media_fragments[1],
        audio_codec,
        expected_start_sample=0,
        expected_sample_count=segment_sample_counts[1],
        validate_packet_timeline=False,
    ) is True
    assert RecordedFMP4Stream.validateTranscodedAudioGenerationPacketTimeline(
        init_data,
        media_fragments,
        audio_codec,
        expected_sample_count=sum(segment_sample_counts),
    ) is True
    assert b''.join(_extract_fragment_mdat_payload(fragment) for fragment in media_fragments) == source_payload


@pytest.mark.parametrize(
    ('audio_codec', 'frame_samples', 'partial_samples'),
    [
        ('aac', 1024, 544),
        ('opus', 960, 312),
    ],
)
def test_audio_generation_validator_accepts_deferred_compensation_across_fragments(
    audio_codec: AudioCodec,
    frame_samples: int,
    partial_samples: int,
) -> None:
    """±1 sampleの補償間に通常packetがあっても累積誤差が1 sample以内なら受理する。"""

    sample_durations = [
        frame_samples,
        frame_samples - 1,
        frame_samples,
        frame_samples + 1,
        partial_samples,
    ]
    segment_sample_counts = [
        frame_samples * 2 - 1,
        frame_samples * 2 + 1 + partial_samples,
    ]
    source, _ = _build_splittable_audio_fmp4(
        sample_durations,
        [3] * len(sample_durations),
        audio_init=_build_opus_init() if audio_codec == 'opus' else _build_aac_init(),
    )
    init_data, media_fragments = RecordedFMP4Stream.splitTranscodedAudioFMP4(
        source,
        segment_sample_counts,
    )

    assert RecordedFMP4Stream.validateTranscodedAudioGenerationPacketTimeline(
        init_data,
        media_fragments,
        audio_codec,
        expected_sample_count=sum(segment_sample_counts),
    ) is True


@pytest.mark.parametrize(
    ('audio_codec', 'frame_samples', 'partial_samples'),
    [
        ('aac', 1024, 544),
        ('opus', 960, 312),
    ],
)
def test_audio_generation_validator_rejects_two_sample_cumulative_drift(
    audio_codec: AudioCodec,
    frame_samples: int,
    partial_samples: int,
) -> None:
    """総sample数が合っていても途中の累積丸め誤差が2 sampleへ達するpacket列を拒否する。"""

    sample_durations = [
        frame_samples,
        frame_samples - 1,
        frame_samples - 1,
        frame_samples + 1,
        frame_samples + 1,
        partial_samples,
    ]
    segment_sample_counts = [
        frame_samples * 3 - 2,
        frame_samples * 2 + 2 + partial_samples,
    ]
    source, _ = _build_splittable_audio_fmp4(
        sample_durations,
        [3] * len(sample_durations),
        audio_init=_build_opus_init() if audio_codec == 'opus' else _build_aac_init(),
    )
    init_data, media_fragments = RecordedFMP4Stream.splitTranscodedAudioFMP4(
        source,
        segment_sample_counts,
    )

    assert RecordedFMP4Stream.validateTranscodedAudioGenerationPacketTimeline(
        init_data,
        media_fragments,
        audio_codec,
        expected_sample_count=sum(segment_sample_counts),
    ) is False


def test_audio_fmp4_packet_split_rejects_boundary_inside_packet() -> None:
    """計画境界が圧縮packet途中へ入る場合はpayloadを切断せず拒否する。"""

    source, _ = _build_splittable_audio_fmp4([1024, 1024], [4, 5])

    with pytest.raises(ValueError, match='falls inside a packet'):
        RecordedFMP4Stream.splitTranscodedAudioFMP4(source, [1500, 548])


def test_audio_fmp4_packet_split_rejects_mdat_size_mismatch() -> None:
    """trun sample sizeで説明できない余剰または欠落mdat payloadを拒否する。"""

    source, _ = _build_splittable_audio_fmp4(
        [1024, 1024],
        [4, 5],
        payload_suffix=b'unreferenced',
    )

    with pytest.raises(ValueError, match='packet sizes do not exactly cover'):
        RecordedFMP4Stream.splitTranscodedAudioFMP4(source, [1024, 1024])


@pytest.mark.parametrize('unknown_box_location', ['TopLevel', 'Traf'])
def test_audio_fmp4_packet_split_rejects_unknown_box_structure(unknown_box_location: str) -> None:
    """未知のtop-level/traf構造を暗黙に読み飛ばさず安全に拒否する。"""

    unknown_box = (12).to_bytes(4, 'big') + b'free' + b'test'
    source, _ = _build_splittable_audio_fmp4(
        [1024, 1024],
        [4, 5],
        extra_top_level_box=unknown_box if unknown_box_location == 'TopLevel' else b'',
        extra_traf_box=unknown_box if unknown_box_location == 'Traf' else b'',
    )

    with pytest.raises(ValueError, match='must contain'):
        RecordedFMP4Stream.splitTranscodedAudioFMP4(source, [1024, 1024])


def test_audio_fmp4_packet_split_handles_video_177_scale_boundary_plan() -> None:
    """video_id=177の失敗世代相当となる269境界・80,430 packetでも累積ずれを生じない。"""

    segment_sample_counts = [288_000] * 268 + [29 * 960 + 312]
    sample_durations = [960] * 80_430
    # 実測で失敗した形と同じく、959/961補償ペアをsegment 169/170の境界へまたがせる。
    compensation_pair_start = 170 * 300 - 1
    sample_durations[compensation_pair_start:compensation_pair_start + 2] = [959, 961]
    segment_sample_counts[169] -= 1
    segment_sample_counts[170] += 1
    sample_durations[-1] = 312
    source, source_payload = _build_splittable_audio_fmp4(
        sample_durations,
        [1] * len(sample_durations),
        audio_init=_build_opus_init(),
        use_default_sample_size=True,
    )

    init_data, media_fragments = RecordedFMP4Stream.splitTranscodedAudioFMP4(
        source,
        segment_sample_counts,
    )

    assert len(media_fragments) == 269
    previous_info = RecordedFMP4Stream.inspectAudioFragment(init_data, media_fragments[169])
    next_info = RecordedFMP4Stream.inspectAudioFragment(init_data, media_fragments[170])
    last_info = RecordedFMP4Stream.inspectAudioFragment(init_data, media_fragments[-1])
    assert previous_info is not None and previous_info.sample_durations[-1] == 959
    assert next_info is not None and next_info.sample_durations[0] == 961
    assert last_info is not None and last_info.sample_durations[-1] == 312
    assert sum(
        info.total_duration
        for fragment in media_fragments
        if (info := RecordedFMP4Stream.inspectAudioFragment(init_data, fragment)) is not None
    ) == sum(segment_sample_counts)
    assert RecordedFMP4Stream.validateTranscodedAudioGenerationPacketTimeline(
        init_data,
        media_fragments,
        'opus',
        expected_sample_count=sum(segment_sample_counts),
    ) is True
    assert b''.join(_extract_fragment_mdat_payload(fragment) for fragment in media_fragments) == source_payload


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


def test_audio_fragment_parser_checks_every_moof_and_sample_duration() -> None:
    """複数moofの内部連続性・packet数・全1024-sample durationを検証する。"""

    init, media = _build_audio_fragment([[1024, 1024], [1024, 1024, 1024]])
    info = RecordedFMP4Stream.inspectAudioFragment(init, media)

    assert info is not None
    assert info.first_decode_time == 0
    assert info.total_duration == 5 * 1024
    assert info.sample_count == 5
    assert RecordedFMP4Stream.validateTranscodedAudioFragmentTimeline(
        init,
        media,
        'aac',
        expected_start_sample=0,
        expected_sample_count=5 * 1024,
    ) is True

    _, discontinuous_media = _build_audio_fragment(
        [[1024, 1024], [1024, 1024, 1024]],
        gap_before_moof=1024,
    )
    assert RecordedFMP4Stream.inspectAudioFragment(init, discontinuous_media) is None

    _, short_frame_media = _build_audio_fragment([[960, 1024]])
    assert RecordedFMP4Stream.validateTranscodedAudioFragmentTimeline(
        init,
        short_frame_media,
        'aac',
        expected_start_sample=0,
        expected_sample_count=1984,
    ) is False


def test_transcoded_aac_validator_accepts_only_exact_final_partial_frame() -> None:
    """AAC変換は整数sample境界と末尾partial frameまで厳密に検証する。"""

    init, media = _build_audio_fragment([[1024, 1024, 544]], first_decode_time=48_000)
    assert RecordedFMP4Stream.validateTranscodedAudioFragmentTimeline(
        init,
        media,
        'aac',
        expected_start_sample=48_000,
        expected_sample_count=2592,
    ) is True
    assert RecordedFMP4Stream.validateTranscodedAudioFragmentTimeline(
        init,
        media,
        'aac',
        expected_start_sample=48_000,
        expected_sample_count=2591,
    ) is False


def test_transcoded_aac_validator_rejects_channel_mismatch_and_missing_samples() -> None:
    """生成側で補正すべきチャンネル不一致と時間不足をvalidatorでは許容しない。"""

    stereo_init, exact_media = _build_audio_fragment([[1024, 1024]])
    assert RecordedFMP4Stream.validateTranscodedAudioFragmentTimeline(
        stereo_init,
        exact_media,
        'aac',
        expected_start_sample=0,
        expected_sample_count=2048,
        expected_channel_count=1,
    ) is False

    mono_init, short_media = _build_audio_fragment(
        [[1024, 1024]],
        audio_init=_build_aac_init(channels=1, sample_entry_channels=1),
    )
    assert RecordedFMP4Stream.validateTranscodedAudioFragmentTimeline(
        mono_init,
        short_media,
        'aac',
        expected_start_sample=0,
        expected_sample_count=2048 + 3744,
        expected_channel_count=1,
    ) is False


@pytest.mark.parametrize(
    ('durations', 'expected_sample_count'),
    [
        ([1024, 1023, 1025, 1024, 544], 4640),
        ([1024, 1025, 1023, 1024, 544], 4640),
        ([1024, 1023, 1024, 1025, 544], 4640),
        ([1024, 1024, 1023, 1025], 4096),
    ],
)
def test_transcoded_aac_validator_accepts_adjacent_compensated_rounding(
    durations: list[int],
    expected_sample_count: int,
) -> None:
    """MP4 muxerの累積誤差1 sample以内となる±1 sample補償を正常なAACとして扱う。"""

    init, media = _build_audio_fragment([durations], first_decode_time=48_000)
    assert RecordedFMP4Stream.validateTranscodedAudioFragmentTimeline(
        init,
        media,
        'aac',
        expected_start_sample=48_000,
        expected_sample_count=expected_sample_count,
    ) is True


@pytest.mark.parametrize(
    ('durations', 'expected_sample_count'),
    [
        ([1024, 1022, 1026, 1024, 544], 4640),
        ([1024, 1023, 1023, 1025, 1025, 544], 5664),
        ([1024, 1023, 545], 2592),
    ],
)
def test_transcoded_aac_validator_rejects_unpaired_or_large_rounding(
    durations: list[int],
    expected_sample_count: int,
) -> None:
    """累積2 sampleのdriftと±2 sample以上の変形は合計時間が一致しても拒否する。"""

    init, media = _build_audio_fragment([durations], first_decode_time=48_000)
    assert RecordedFMP4Stream.validateTranscodedAudioFragmentTimeline(
        init,
        media,
        'aac',
        expected_start_sample=48_000,
        expected_sample_count=expected_sample_count,
    ) is False


def test_transcoded_opus_validator_accepts_adjacent_compensated_rounding() -> None:
    """Opusでも隣接した±1 sampleの補償ペアを同じ規則で扱う。"""

    init, media = _build_audio_fragment(
        [[960, 959, 961, 960]],
        first_decode_time=48_000,
        audio_init=_build_opus_init(),
    )
    assert RecordedFMP4Stream.validateTranscodedAudioFragmentTimeline(
        init,
        media,
        'opus',
        expected_start_sample=48_000,
        expected_sample_count=3840,
        expected_channel_count=2,
    ) is True


def test_opus_channel_bitrates() -> None:
    """品質寄りのチャンネル数別Opusビットレートを固定する。"""
    assert [RecordedFMP4Stream.getOpusBitrate(channels) for channels in range(1, 9)] == [
        64_000, 128_000, 192_000, 192_000, 256_000, 256_000, 320_000, 320_000,
    ]
    assert RecordedFMP4Stream.getOpusBitrate(9) is None


def test_standard_ffmpeg_audio_layouts_keep_channel_count() -> None:
    """FFmpegが明示する標準1～8ch layoutを未知扱いせず、対応Opus tierへ割り当てる。"""

    expected_channels = {
        'mono': 1,
        'stereo': 2,
        '2.1': 3,
        '3.0(back)': 3,
        '3.1': 4,
        'quad': 4,
        'quad(side)': 4,
        '4.1': 5,
        '5.0': 5,
        '5.0(side)': 5,
        '5.1': 6,
        '6.0(front)': 6,
        'hexagonal': 6,
        '6.1': 7,
        '6.1(back)': 7,
        '7.0(front)': 7,
        '7.1': 8,
        '7.1(wide)': 8,
        'octagonal': 8,
    }
    actual_channels = {
        channel_layout: RecordedFMP4Stream.getAudioChannelCount({
            'index': 1,
            'codec': 'AAC-LC',
            'channel': channel_layout,
            'sampling_rate': 48_000,
            'language': 'ja',
            'channel_layout': channel_layout,
        })
        for channel_layout in expected_channels
    }

    assert actual_channels == expected_channels


def test_unknown_explicit_audio_layout_is_not_guessed_from_display_label() -> None:
    """未知の明示layoutを5.1ch表示だけで6chと推測せず、AACへ安全に落とす。"""

    track = {
        'index': 1,
        'codec': 'AAC-LC',
        'channel': '5.1ch',
        'sampling_rate': 48_000,
        'language': 'ja',
        'channel_layout': '5.1.2',
    }
    assert RecordedFMP4Stream.getAudioChannelCount(track) is None


def test_silent_audio_preserves_standard_ffmpeg_layout() -> None:
    """欠落区間の無音補完でも既知のmultichannel layout名をそのまま維持する。"""

    for channel_layout in ('quad', '4.1', '5.0(side)', '6.1', 'octagonal'):
        track = {
            'index': 1,
            'codec': 'AAC-LC',
            'channel': channel_layout,
            'sampling_rate': 48_000,
            'language': 'ja',
            'channel_layout': channel_layout,
        }
        stream = object.__new__(RecordedFMP4Stream)
        stream.recorded_program = SimpleNamespace(recorded_video=SimpleNamespace(
            audio_track_timeline=[],
            audio_tracks=[track],
        ))
        rendition = RecordedAudioRendition('1', 1, 2, 'all', 'Track 1', 'ja')

        assert stream._RecordedFMP4Stream__getSilentAudioChannelLayout(  # pyright: ignore[reportPrivateUsage]
            rendition,
        ) == channel_layout


@pytest.mark.parametrize(('indexed_layout', 'ffmpeg_layout'), [
    ('stereo downmix', 'stereo'),
    ('5.1(back)', '5.1'),
])
def test_silent_audio_canonicalizes_layout_aliases(indexed_layout: str, ffmpeg_layout: str) -> None:
    """索引上は既知でもanullsrcが受理しないlayout別名をcanonical名へ直す。"""

    track = {
        'index': 1,
        'codec': 'AAC-LC',
        'channel': indexed_layout,
        'sampling_rate': 48_000,
        'language': 'ja',
        'channel_layout': indexed_layout,
    }
    stream = object.__new__(RecordedFMP4Stream)
    stream.recorded_program = SimpleNamespace(recorded_video=SimpleNamespace(
        audio_track_timeline=[],
        audio_tracks=[track],
    ))
    rendition = RecordedAudioRendition('1', 1, 2, 'all', 'Track 1', 'ja')

    assert stream._RecordedFMP4Stream__getSilentAudioChannelLayout(  # pyright: ignore[reportPrivateUsage]
        rendition,
    ) == ffmpeg_layout


def test_audio_init_parser_extracts_codec_and_channel_count() -> None:
    """AAC/Opus initからcodec stringと1～8chの構成を取得する。"""

    aac_init = _build_aac_init(audio_object_type=2, sampling_frequency_index=3, channels=2)

    assert RecordedFMP4Stream.extractAudioCodecString(aac_init) == 'mp4a.40.2'
    assert RecordedFMP4Stream.extractAudioInitializationChannelCount(aac_init, 'aac') == 2

    pce_init = _build_aac_init(
        audio_object_type=2,
        sampling_frequency_index=3,
        channels=0,
        sample_entry_channels=4,
    )
    invalid_pce_init = _build_aac_init(
        audio_object_type=2,
        sampling_frequency_index=3,
        channels=0,
        sample_entry_channels=9,
    )
    assert RecordedFMP4Stream.extractAudioInitializationChannelCount(pce_init, 'aac') == 4
    assert RecordedFMP4Stream.extractAudioInitializationChannelCount(invalid_pce_init, 'aac') is None

    def Box(box_type: bytes, payload: bytes) -> bytes:
        return (8 + len(payload)).to_bytes(4, 'big') + box_type + payload

    opus_init = Box(b'Opus', b'') + Box(b'dOps', bytes([0, 6]))
    assert RecordedFMP4Stream.extractAudioInitializationChannelCount(opus_init, 'opus') == 6


def test_video_input_seek_decodes_preroll_before_exact_trim_position() -> None:
    """途中セグメントは10秒前から復号し、先頭付近では録画先頭から不足分だけ復号する。"""

    assert RecordedFMP4Stream.computeInputSeekWindow(300.3, 6.006) == (290.3, 10.0, 16.006)
    assert RecordedFMP4Stream.computeInputSeekWindow(4.0, 6.006) == (0.0, 4.0, 10.006)


def test_recorded_video_bitrates_strictly_increase_from_av1_to_avc() -> None:
    """通常・BS4Kの全録画画質でAV1 < VP9 < HEVC < AVCを保証する。"""

    qualities: tuple[QUALITY_TYPES, ...] = (
        '1080p-60fps', '1080p', '810p', '720p', '540p', '480p', '360p', '240p',
        '4320p', '2160p', '1440p', '1080p-30fps', '810p-60fps', '810p-30fps',
        '720p-60fps', '720p-30fps', '540p-30fps', '480p-30fps', '360p-30fps', '240p-30fps',
    )
    for quality in qualities:
        bitrates = [
            RecordedFMP4Stream.getVideoBitrate(quality, codec)
            for codec in ('av1', 'vp9', 'hevc', 'avc')
        ]
        specified_values = [int(bitrate.video_bitrate.removesuffix('K')) for bitrate in bitrates]
        maximum_values = [int(bitrate.video_bitrate_max.removesuffix('K')) for bitrate in bitrates]

        assert all(left < right for left, right in pairwise(specified_values))
        assert all(left < right for left, right in pairwise(maximum_values))


def test_recorded_video_bitrate_representative_values() -> None:
    """通常・BS4K・最低画質のコーデック別指定値と最大値を固定する。"""

    assert [
        RecordedFMP4Stream.getVideoBitrate('1080p', codec)
        for codec in ('av1', 'vp9', 'hevc', 'avc')
    ] == [
        RecordedVideoBitrate('2100K', '3150K'),
        RecordedVideoBitrate('2700K', '4050K'),
        RecordedVideoBitrate('3000K', '4500K'),
        RecordedVideoBitrate('9500K', '13000K'),
    ]
    assert [
        RecordedFMP4Stream.getVideoBitrate('2160p', codec)
        for codec in ('av1', 'vp9', 'hevc', 'avc')
    ] == [
        RecordedVideoBitrate('6300K', '9450K'),
        RecordedVideoBitrate('8100K', '12150K'),
        RecordedVideoBitrate('9000K', '13500K'),
        RecordedVideoBitrate('18000K', '25000K'),
    ]
    assert [
        RecordedFMP4Stream.getVideoBitrate('240p', codec)
        for codec in ('av1', 'vp9', 'hevc', 'avc')
    ] == [
        RecordedVideoBitrate('315K', '420K'),
        RecordedVideoBitrate('405K', '540K'),
        RecordedVideoBitrate('450K', '600K'),
        RecordedVideoBitrate('550K', '650K'),
    ]


@pytest.mark.parametrize(('codec', 'playback', 'offline'), [
    ('av1', RecordedVideoBitrate('1400K', '2100K'), RecordedVideoBitrate('1078K', '1617K')),
    ('vp9', RecordedVideoBitrate('1800K', '2700K'), RecordedVideoBitrate('1386K', '2079K')),
    ('hevc', RecordedVideoBitrate('2000K', '3000K'), RecordedVideoBitrate('1540K', '2310K')),
    ('avc', RecordedVideoBitrate('4500K', '6200K'), RecordedVideoBitrate('3465K', '4774K')),
])
def test_offline_720p_video_bitrates_are_77_percent_of_playback(
    codec: VideoCodec,
    playback: RecordedVideoBitrate,
    offline: RecordedVideoBitrate,
) -> None:
    """既定720pの全映像codecで通常値を保ち、保存用だけ77%へ抑える。"""

    assert RecordedFMP4Stream.getVideoBitrate('720p', codec) == playback
    assert RecordedFMP4Stream.getOfflineVideoBitrate('720p', codec) == offline


def test_video_segment_uses_codec_bitrate_and_keeps_16_by_9_display_aspect_ratio(monkeypatch, tmp_path) -> None:
    """AV1用帯域を指定し、1440x1080出力の表示アスペクト比も16:9に保つ。"""

    stream = object.__new__(RecordedFMP4Stream)
    stream.quality = '1080p-60fps'
    stream.encoding_options = SimpleNamespace(
        video_codec='av1',
        video_bit_depth=10,
        is_24fps_mode_enabled=False,
    )
    stream._is_offline_continuous = False
    stream.recorded_program = SimpleNamespace(recorded_video=SimpleNamespace(
        id=1,
        file_path='/recording.mp4',
        container_format='MP4',
        video_scan_type='Progressive',
        video_stream_timeline=[],
    ))
    commands: list[list[str]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b'fragment', b''

    async def CreateSubprocessExec(*command, **_kwargs):
        commands.append([str(argument) for argument in command])
        return FakeProcess()

    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__getBackend',
        lambda _self: 'FFmpeg',
    )
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

    asyncio.run(stream._RecordedFMP4Stream__encodeSegment(  # pyright: ignore[reportPrivateUsage]
        RecordedFMP4Segment(0, 0.0, 6.0, 0, 0),
        tmp_path / 'init.mp4',
        tmp_path / 'segment.m4s',
    ))

    aspect_index = commands[0].index('-aspect')
    assert commands[0][aspect_index + 1] == '16:9'
    bitrate_index = commands[0].index('-b:v')
    maxrate_index = commands[0].index('-maxrate')
    bufsize_index = commands[0].index('-bufsize')
    assert commands[0][bitrate_index + 1] == '2450K'
    assert commands[0][maxrate_index + 1] == '3640K'
    assert commands[0][bufsize_index + 1] == '7280K'


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


def test_opus_master_uses_maximum_timeline_channels_codec_and_bandwidth(monkeypatch) -> None:
    """masterは全音声区間の最大構成をCHANNELSとOpus帯域へ反映する。"""

    stereo_track = {
        'index': 1, 'stream_index': 2, 'codec': 'AAC-LC', 'channel': 'Stereo',
        'sampling_rate': 48_000, 'language': 'ja', 'channel_layout': 'stereo',
    }
    surround_track = {**stereo_track, 'channel': '5.1ch', 'channel_layout': '5.1'}
    stream = object.__new__(RecordedFMP4Stream)
    stream.session_id = 'opus-session'
    stream.quality = '1080p'
    stream.encoding_options = SimpleNamespace(video_codec='av1', video_bit_depth=10, audio_codec='opus')
    stream._effective_audio_codec = 'opus'
    stream._is_offline_continuous = False
    stream._segments = [
        RecordedFMP4Segment(0, 0.0, 6.0, 0, 0),
        RecordedFMP4Segment(1, 6.0, 6.0, 0, 1),
    ]
    stream.recorded_program = SimpleNamespace(recorded_video=SimpleNamespace(
        container_format='MP4',
        audio_tracks=[stereo_track],
        audio_track_timeline=[
            {'start_time': 0.0, 'end_time': 6.0, 'tracks': [stereo_track]},
            {'start_time': 6.0, 'end_time': 12.0, 'tracks': [surround_track]},
        ],
        subtitle_tracks=[],
    ))
    av1c = bytes([0x81, 0x0A, 0x40])
    video_init = (len(av1c) + 8).to_bytes(4, 'big') + b'av1C' + av1c
    requested_video_inits: list[tuple[int, int]] = []

    async def GetVideoInitSegment(_self, generation: int, sequence: int) -> bytes:
        requested_video_inits.append((generation, sequence))
        return video_init

    monkeypatch.setattr(RecordedFMP4Stream, 'keepAlive', lambda _self: None)
    monkeypatch.setattr(RecordedFMP4Stream, 'getVideoInitSegment', GetVideoInitSegment)

    master = asyncio.run(stream._RecordedFMP4Stream__getMasterPlaylist('cache'))  # pyright: ignore[reportPrivateUsage]
    expected_bandwidth = 3_150_000 + 256_000 * 5 // 4

    assert 'CHANNELS="6"' in master
    assert 'CODECS="av01.0.10M.10,opus"' in master
    assert f'BANDWIDTH={expected_bandwidth}' in master
    assert 'video_codec=av1' in master
    assert 'audio_codec=opus' in master
    # 同じ映像世代の末尾ではなく先頭を使い、連続エンコード全体の完了待ちを避ける。
    assert requested_video_inits == [(0, 0)]


def test_opus_fallback_master_keeps_declared_multichannel_count(monkeypatch) -> None:
    """未知layoutでAACへ落ちてもIndexer由来の4chをstereoと誤宣言しない。"""

    track = {
        'index': 1, 'stream_index': 2, 'codec': 'AAC-LC', 'channel': '4 Channels',
        'sampling_rate': 48_000, 'language': 'ja', 'channel_layout': 'ambisonic first order',
    }
    stream = object.__new__(RecordedFMP4Stream)
    stream.session_id = 'opus-fallback-session'
    stream.quality = '1080p'
    stream.encoding_options = SimpleNamespace(video_codec='avc', video_bit_depth=8, audio_codec='opus')
    stream._effective_audio_codec = 'aac'
    stream._is_offline_continuous = False
    stream._segments = [RecordedFMP4Segment(0, 0.0, 6.0, 0, 0)]
    stream.recorded_program = SimpleNamespace(recorded_video=SimpleNamespace(
        container_format='MP4',
        audio_tracks=[track],
        audio_track_timeline=[{'start_time': 0.0, 'end_time': 6.0, 'tracks': [track]}],
        subtitle_tracks=[],
    ))
    avcc = bytes([1, 100, 0, 40])
    video_init = (len(avcc) + 8).to_bytes(4, 'big') + b'avcC' + avcc

    async def GetVideoInitSegment(_self, _generation: int, _sequence: int) -> bytes:
        return video_init

    monkeypatch.setattr(RecordedFMP4Stream, 'keepAlive', lambda _self: None)
    monkeypatch.setattr(RecordedFMP4Stream, 'getVideoInitSegment', GetVideoInitSegment)

    master = asyncio.run(stream._RecordedFMP4Stream__getMasterPlaylist('cache'))  # pyright: ignore[reportPrivateUsage]

    assert 'CHANNELS="4"' in master
    assert 'CODECS="avc1.640028,mp4a.40.2"' in master
    assert 'audio_codec=opus' in master
    rendition = stream.getAudioRenditions()[0]
    assert stream._RecordedFMP4Stream__getSilentAudioChannelLayout(rendition) == '4.0'  # pyright: ignore[reportPrivateUsage]


def test_audio_only_master_uses_primary_audio_without_video_generation(monkeypatch) -> None:
    """音声のみ録画は映像initを要求せず、主音声playlistをvariantとして返す。"""

    track = {
        'index': 1, 'stream_index': 0, 'codec': 'AAC-LC', 'channel': 'Stereo',
        'sampling_rate': 48_000, 'language': 'ja', 'channel_layout': 'stereo',
    }
    stream = object.__new__(RecordedFMP4Stream)
    stream.session_id = 'audio-only-session'
    stream.quality = '1080p'
    stream.encoding_options = SimpleNamespace(video_codec='avc', video_bit_depth=8, audio_codec='aac')
    stream._effective_audio_codec = 'aac'
    stream._segments = [RecordedFMP4Segment(0, 0.0, 6.0, 0, 0)]
    stream._completed_sequences = set()
    stream._active_operations = 0
    stream.recorded_program = SimpleNamespace(recorded_video=SimpleNamespace(
        has_video=False,
        container_format='MP4',
        audio_tracks=[track],
        audio_track_timeline=[{'start_time': 0.0, 'end_time': 6.0, 'tracks': [track]}],
        subtitle_tracks=[],
    ))
    get_video_init = AsyncMock()

    async def GetAudioInitSegment(_self, _rendition_id: str, _sequence: int) -> bytes:
        return _build_aac_init()

    get_audio_segment = AsyncMock(return_value=b'audio-segment')
    monkeypatch.setattr(RecordedFMP4Stream, 'keepAlive', lambda _self: None)
    monkeypatch.setattr(RecordedFMP4Stream, 'getAudioInitSegment', GetAudioInitSegment)
    monkeypatch.setattr(RecordedFMP4Stream, 'getVideoInitSegment', get_video_init)
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__getTranscodedAudioSegment',
        get_audio_segment,
    )

    master = asyncio.run(stream._RecordedFMP4Stream__getMasterPlaylist('cache'))  # pyright: ignore[reportPrivateUsage]
    segment = asyncio.run(stream.getAudioSegment('1', 0))

    assert 'CODECS="mp4a.40.2"' in master
    assert 'audio/1/playlist?session_id=audio-only-session' in master
    assert 'video/playlist' not in master
    assert 'video_codec=avc' in master
    assert segment == b'audio-segment'
    assert stream._completed_sequences == {0}
    get_video_init.assert_not_awaited()


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
    # 音声構成だけが変わる最後の境界では、映像initは更新しない。
    assert video_playlist.count('#EXT-X-MAP') == 2
    assert audio_playlist.count('#EXT-X-MAP') == 3


def test_audio_delivery_boundary_does_not_reload_unchanged_video_init(monkeypatch) -> None:
    """6segment音声delivery境界ではCCだけを揃え、同じ映像MAPの再取得を避ける。"""

    stream = object.__new__(RecordedFMP4Stream)
    stream.session_id = 'session'
    stream._segments = [
        RecordedFMP4Segment(
            sequence,
            sequence * 6.0,
            6.0,
            0,
            0,
            transcoded_audio_generation=0 if sequence < 6 else 1,
        )
        for sequence in range(8)
    ]
    stream.recorded_program = SimpleNamespace(recorded_video=SimpleNamespace(
        container_format='MPEG-TS',
        audio_tracks=[{'index': 1, 'stream_index': 2, 'language': 'ja'}],
    ))
    stream.encoding_options = SimpleNamespace(video_codec='avc', video_bit_depth=8, audio_codec='aac')
    monkeypatch.setattr(RecordedFMP4Stream, 'keepAlive', lambda _self: None)

    video_playlist = stream.getVideoPlaylist('cache')
    audio_playlist = stream.getAudioPlaylist('1', 'cache')

    assert video_playlist.count('#EXT-X-DISCONTINUITY') == 1
    assert audio_playlist.count('#EXT-X-DISCONTINUITY') == 1
    assert video_playlist.count('#EXT-X-MAP') == 1
    assert audio_playlist.count('#EXT-X-MAP') == 2


def test_cached_video_init_does_not_wait_for_boundary_segment(monkeypatch, tmp_path: Path) -> None:
    """既存映像initは、音声だけの境界segmentをencodeせず即座に返す。"""

    init_path = tmp_path / 'init.mp4'
    init_path.write_bytes(b'cached-init')
    stream = object.__new__(RecordedFMP4Stream)
    stream._segments = [RecordedFMP4Segment(0, 0.0, 6.0, 0, 0)]
    acquire = AsyncMock()
    get_video_segment = AsyncMock(return_value=b'unused-segment')
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__acquire',
        acquire,
    )
    monkeypatch.setattr(RecordedFMP4Stream, 'getVideoSegment', get_video_segment)
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__buildCachePath',
        lambda _self, _segment, is_init: init_path if is_init else tmp_path / 'segment.mp4',
    )

    result = asyncio.run(stream.getVideoInitSegment(0, 0))

    assert result == b'cached-init'
    acquire.assert_awaited_once_with(init_path)
    get_video_segment.assert_not_awaited()


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
    stream._effective_audio_codec = 'aac'
    stream._is_offline_continuous = False

    segments = stream._RecordedFMP4Stream__buildSegments()  # pyright: ignore[reportPrivateUsage]

    assert [(segment.start_time, segment.generation) for segment in segments] == [(0.0, 0), (6.0, 1)]


def test_regular_boundary_near_structural_boundary_is_coalesced(monkeypatch) -> None:
    """AAC delay未満の短片を作る通常境界は、近接する構成境界へまとめる。"""

    recorded_video = SimpleNamespace(
        duration=12.0,
        video_frame_rate=60.0,
        video_stream_timeline=[],
        audio_track_timeline=[{'start_time': 0.0, 'end_time': 6.01, 'tracks': []}],
        audio_tracks=[],
        video_codec='HEVC',
        video_resolution_width=1920,
        video_resolution_height=1080,
        video_scan_type='Progressive',
    )
    stream = object.__new__(RecordedFMP4Stream)
    stream.recorded_program = SimpleNamespace(recorded_video=recorded_video)
    stream._effective_audio_codec = 'aac'
    stream._is_offline_continuous = False
    monkeypatch.setattr(
        'app.streams.RecordedFMP4Stream.VideoSegmentPlanner.computeSegmentDurationSeconds',
        lambda _frame_rate: 6.0,
    )

    segments = stream._RecordedFMP4Stream__buildSegments()  # pyright: ignore[reportPrivateUsage]

    assert [(segment.start_time, segment.duration) for segment in segments] == [
        (0.0, 6.01),
        (6.01, 5.99),
    ]


def test_nearby_video_and_audio_configuration_boundaries_coalesce_to_later_sample(monkeypatch) -> None:
    """164の9.8ms差の映像・音声境界を後側へ統合し、470-sampleの短片を作らない。"""

    audio_boundary = 1523.562666
    video_boundary = 1523.572466
    duration = 1530.0
    stereo_track = {
        'index': 1, 'stream_index': 2, 'pid': 0x111, 'codec': 'AAC-LC',
        'channel': 'Stereo', 'sampling_rate': 48_000, 'channel_layout': 'stereo',
    }
    mono_track = {
        **stereo_track,
        'channel': 'Monaural',
        'channel_layout': 'mono',
    }
    video_entry = {
        'stream_index': 0, 'codec': 'HEVC', 'profile': 'Main 10', 'width': 3840, 'height': 2160,
        'frame_rate': 60.0, 'scan_type': 'Progressive', 'bit_depth': 10,
    }
    recorded_video = SimpleNamespace(
        duration=duration,
        video_frame_rate=60.0,
        video_stream_timeline=[
            {**video_entry, 'start_time': 0.0, 'end_time': video_boundary},
            {**video_entry, 'stream_index': 3, 'start_time': video_boundary, 'end_time': duration},
        ],
        audio_track_timeline=[
            {'start_time': 0.0, 'end_time': audio_boundary, 'tracks': [stereo_track]},
            {'start_time': audio_boundary, 'end_time': duration, 'tracks': [mono_track]},
        ],
        audio_tracks=[stereo_track],
        video_codec='HEVC',
        video_resolution_width=3840,
        video_resolution_height=2160,
        video_scan_type='Progressive',
    )
    stream = object.__new__(RecordedFMP4Stream)
    stream.recorded_program = SimpleNamespace(recorded_video=recorded_video)
    stream._effective_audio_codec = 'aac'
    stream._is_offline_continuous = False
    monkeypatch.setattr(
        'app.streams.RecordedFMP4Stream.VideoSegmentPlanner.computeSegmentDurationSeconds',
        lambda _frame_rate: duration + 1.0,
    )

    segments = stream._RecordedFMP4Stream__buildSegments()  # pyright: ignore[reportPrivateUsage]

    expected_boundary_sample = round(video_boundary * RecordedFMP4Stream.AUDIO_SAMPLE_RATE)
    assert len(segments) == 2
    assert round(segments[1].start_time * RecordedFMP4Stream.AUDIO_SAMPLE_RATE) == expected_boundary_sample
    assert segments[0].duration == pytest.approx(video_boundary, abs=0.5 / 48_000)
    assert all(
        round(segment.duration * RecordedFMP4Stream.AUDIO_SAMPLE_RATE) > 470
        for segment in segments
    )


def test_distant_video_and_audio_configuration_boundaries_remain_separate(monkeypatch) -> None:
    """AAC 1 frameを超える30ms差のcross-media境界は独立した構成変化として維持する。"""

    video_boundary = 6.0
    audio_boundary = 6.03
    duration = 12.0
    stereo_track = {
        'index': 1, 'stream_index': 2, 'pid': 0x111, 'codec': 'AAC-LC',
        'channel': 'Stereo', 'sampling_rate': 48_000, 'channel_layout': 'stereo',
    }
    mono_track = {
        **stereo_track,
        'channel': 'Monaural',
        'channel_layout': 'mono',
    }
    video_entry = {
        'stream_index': 0, 'codec': 'HEVC', 'profile': 'Main', 'width': 1920, 'height': 1080,
        'frame_rate': 60.0, 'scan_type': 'Progressive', 'bit_depth': 8,
    }
    recorded_video = SimpleNamespace(
        duration=duration,
        video_frame_rate=60.0,
        video_stream_timeline=[
            {**video_entry, 'start_time': 0.0, 'end_time': video_boundary},
            {**video_entry, 'stream_index': 3, 'start_time': video_boundary, 'end_time': duration},
        ],
        audio_track_timeline=[
            {'start_time': 0.0, 'end_time': audio_boundary, 'tracks': [stereo_track]},
            {'start_time': audio_boundary, 'end_time': duration, 'tracks': [mono_track]},
        ],
        audio_tracks=[stereo_track],
        video_codec='HEVC',
        video_resolution_width=1920,
        video_resolution_height=1080,
        video_scan_type='Progressive',
    )
    stream = object.__new__(RecordedFMP4Stream)
    stream.recorded_program = SimpleNamespace(recorded_video=recorded_video)
    stream._effective_audio_codec = 'aac'
    stream._is_offline_continuous = False
    monkeypatch.setattr(
        'app.streams.RecordedFMP4Stream.VideoSegmentPlanner.computeSegmentDurationSeconds',
        lambda _frame_rate: duration + 1.0,
    )

    segments = stream._RecordedFMP4Stream__buildSegments()  # pyright: ignore[reportPrivateUsage]

    assert [segment.start_time for segment in segments] == pytest.approx([0.0, video_boundary, audio_boundary])


def test_audio_timeline_gap_starts_a_dedicated_silent_generation(monkeypatch) -> None:
    """明示音声gapを前後の有音区間へ結合せず、generation全体の無音化を防ぐ。"""

    track = {
        'index': 1, 'stream_index': 2, 'pid': 0x111, 'codec': 'AAC-LC',
        'channel': 'Stereo', 'sampling_rate': 48_000, 'channel_layout': 'stereo',
    }
    recorded_video = SimpleNamespace(
        duration=18.0,
        video_frame_rate=60.0,
        video_stream_timeline=[],
        audio_track_timeline=[
            {'start_time': 0.0, 'end_time': 6.0, 'tracks': [track]},
            {'start_time': 12.0, 'end_time': 18.0, 'tracks': [track]},
        ],
        audio_tracks=[track],
        video_codec='HEVC',
        video_resolution_width=1920,
        video_resolution_height=1080,
        video_scan_type='Progressive',
    )
    stream = object.__new__(RecordedFMP4Stream)
    stream.recorded_program = SimpleNamespace(recorded_video=recorded_video)
    stream._effective_audio_codec = 'aac'
    stream._is_offline_continuous = False
    monkeypatch.setattr(
        'app.streams.RecordedFMP4Stream.VideoSegmentPlanner.computeSegmentDurationSeconds',
        lambda _frame_rate: 6.0,
    )

    segments = stream._RecordedFMP4Stream__buildSegments()  # pyright: ignore[reportPrivateUsage]
    rendition = RecordedAudioRendition('1', 1, 2, 'all', 'Track 1', 'ja', 0x111)
    get_availability = stream._RecordedFMP4Stream__getAudioRenditionAvailability  # pyright: ignore[reportPrivateUsage]

    assert [segment.audio_generation for segment in segments] == [0, 1, 2]
    assert get_availability(7.0, rendition) is False
    recorded_video.audio_track_timeline = []
    assert get_availability(7.0, rendition) is None


def test_aac_audio_boundaries_follow_generation_local_packet_grid(monkeypatch) -> None:
    """AAC内部境界をgeneration開始基準の1024-sample packet gridへ揃える。"""

    boundary = 1024 / 48_000
    recorded_video = SimpleNamespace(
        duration=6.0,
        video_frame_rate=60.0,
        video_stream_timeline=[],
        audio_track_timeline=[
            {'start_time': 0.0, 'end_time': boundary, 'tracks': []},
            {'start_time': boundary, 'end_time': 6.0, 'tracks': []},
        ],
        audio_tracks=[],
        video_codec='HEVC',
        video_resolution_width=1920,
        video_resolution_height=1080,
        video_scan_type='Progressive',
    )
    stream = object.__new__(RecordedFMP4Stream)
    stream.recorded_program = SimpleNamespace(recorded_video=recorded_video)
    stream._effective_audio_codec = 'aac'
    stream._is_offline_continuous = False
    monkeypatch.setattr(
        'app.streams.RecordedFMP4Stream.VideoSegmentPlanner.computeSegmentDurationSeconds',
        lambda _frame_rate: 6.0,
    )

    segments = stream._RecordedFMP4Stream__buildSegments()  # pyright: ignore[reportPrivateUsage]
    transcoded_ranges = [
        (segment.transcoded_audio_start_sample, segment.transcoded_audio_sample_count)
        for segment in segments
    ]
    assert transcoded_ranges == [(0, 1024), (1024, 288_000 - 1024)]
    assert transcoded_ranges[0][0] + transcoded_ranges[0][1] == transcoded_ranges[1][0]
    assert sum(sample_count for _, sample_count in transcoded_ranges) == 288_000


def test_opus_audio_boundary_uses_post_preskip_packet_phase(monkeypatch) -> None:
    """Opus内部境界をpre-skip後の648+960*k sample位相へ揃える。"""

    boundary = 312 / 48_000
    recorded_video = SimpleNamespace(
        duration=1.0,
        video_frame_rate=60.0,
        video_stream_timeline=[],
        audio_track_timeline=[
            {'start_time': 0.0, 'end_time': boundary, 'tracks': []},
            {'start_time': boundary, 'end_time': 1.0, 'tracks': []},
        ],
        audio_tracks=[],
        video_codec='HEVC',
        video_resolution_width=1920,
        video_resolution_height=1080,
        video_scan_type='Progressive',
    )
    stream = object.__new__(RecordedFMP4Stream)
    stream.recorded_program = SimpleNamespace(recorded_video=recorded_video)
    stream._effective_audio_codec = 'opus'
    stream._is_offline_continuous = False
    monkeypatch.setattr(
        'app.streams.RecordedFMP4Stream.VideoSegmentPlanner.computeSegmentDurationSeconds',
        lambda _frame_rate: 6.0,
    )

    segments = stream._RecordedFMP4Stream__buildSegments()  # pyright: ignore[reportPrivateUsage]

    assert segments[0].transcoded_audio_sample_count == 648
    assert segments[1].transcoded_audio_start_sample == 648
    assert sum(segment.transcoded_audio_sample_count or 0 for segment in segments) == 48_000


def test_transcoded_boundary_alignment_preserves_phase_and_strict_order() -> None:
    """近接境界とterminal近傍でもpacket位相を壊さず正のsegment長を保つ。"""

    align = RecordedFMP4Stream._RecordedFMP4Stream__alignTranscodedAudioGenerationBoundaries  # pyright: ignore[reportPrivateUsage]

    aac = align([0, 10_000, 10_010, 30_000], 1024, 0)
    opus = align([0, 10_000, 10_010, 30_000], 960, 648)
    aac_tail = align([0, 29_950, 30_000], 1024, 0)
    opus_tail = align([0, 29_950, 30_000], 960, 648)

    assert aac == [0, 10_240, 11_264, 30_000]
    assert opus == [0, 10_248, 11_208, 30_000]
    assert aac_tail is not None and aac_tail[-1] - aac_tail[-2] == 304
    assert opus_tail is not None and opus_tail[-1] - opus_tail[-2] == 552
    assert all(boundary % 1024 == 0 for boundary in aac[1:-1])
    assert all((boundary - 648) % 960 == 0 for boundary in opus[1:-1])


def test_transcoded_delivery_generation_is_bounded_to_six_segments(monkeypatch) -> None:
    """同じ音声構成でも連続encodeの待ち時間を最大6segmentへ制限する。"""

    recorded_video = SimpleNamespace(
        duration=48.0,
        video_frame_rate=60.0,
        video_stream_timeline=[],
        audio_track_timeline=[],
        audio_tracks=[],
        video_codec='HEVC',
        video_resolution_width=1920,
        video_resolution_height=1080,
        video_scan_type='Progressive',
    )
    stream = object.__new__(RecordedFMP4Stream)
    stream.recorded_program = SimpleNamespace(recorded_video=recorded_video)
    stream._effective_audio_codec = 'aac'
    stream._is_offline_continuous = False
    monkeypatch.setattr(
        'app.streams.RecordedFMP4Stream.VideoSegmentPlanner.computeSegmentDurationSeconds',
        lambda _frame_rate: 6.0,
    )

    segments = stream._RecordedFMP4Stream__buildSegments()  # pyright: ignore[reportPrivateUsage]

    assert [segment.audio_generation for segment in segments] == [0] * 8
    assert [segment.transcoded_audio_generation for segment in segments] == [0] * 6 + [1] * 2


def test_offline_transcoded_delivery_generation_covers_whole_configuration(monkeypatch) -> None:
    """オフライン保存は同じ音声構成の全区間を1回の一括生成へまとめる。"""

    recorded_video = SimpleNamespace(
        duration=48.0,
        video_frame_rate=60.0,
        video_stream_timeline=[],
        audio_track_timeline=[],
        audio_tracks=[],
        video_codec='HEVC',
        video_resolution_width=1920,
        video_resolution_height=1080,
        video_scan_type='Progressive',
    )
    stream = object.__new__(RecordedFMP4Stream)
    stream.recorded_program = SimpleNamespace(recorded_video=recorded_video)
    stream._effective_audio_codec = 'aac'
    stream._is_offline_continuous = True
    monkeypatch.setattr(
        'app.streams.RecordedFMP4Stream.VideoSegmentPlanner.computeSegmentDurationSeconds',
        lambda _frame_rate: 6.0,
    )

    segments = stream._RecordedFMP4Stream__buildSegments()  # pyright: ignore[reportPrivateUsage]

    assert [segment.audio_generation for segment in segments] == [0] * 8
    assert [segment.transcoded_audio_generation for segment in segments] == [0] * 8


def test_opus_cache_bitrate_follows_each_audio_configuration_generation() -> None:
    """同じレンディションがstereoから5.1chへ変わる場合も区間別bitrate/cacheを分離する。"""

    stereo_track = {
        'index': 1, 'stream_index': 2, 'codec': 'AAC-LC', 'channel': 'Stereo',
        'sampling_rate': 48_000, 'language': 'ja', 'channel_layout': 'stereo',
    }
    surround_track = {
        **stereo_track,
        'channel': '5.1ch',
        'channel_layout': '5.1',
    }
    stream = object.__new__(RecordedFMP4Stream)
    stream.recorded_program = SimpleNamespace(recorded_video=SimpleNamespace(
        audio_tracks=[stereo_track],
        audio_track_timeline=[
            {'start_time': 0.0, 'end_time': 6.0, 'tracks': [stereo_track]},
            {'start_time': 6.0, 'end_time': 12.0, 'tracks': [surround_track]},
        ],
    ))
    stream.encoding_options = SimpleNamespace(audio_codec='opus')
    stream._effective_audio_codec = 'opus'
    rendition = RecordedAudioRendition('1', 1, 2, 'all', 'Track 1', 'ja')
    get_cache_codec = stream._RecordedFMP4Stream__getAudioCacheCodec  # pyright: ignore[reportPrivateUsage]

    assert get_cache_codec(RecordedFMP4Segment(0, 0.0, 6.0, 0, 0), rendition) == \
        'opus-continuous-128000-stereo'
    assert get_cache_codec(RecordedFMP4Segment(1, 6.0, 6.0, 0, 1), rendition) == \
        'opus-continuous-256000-5.1'
    # backward制約でseekだけが構成境界の1 sample手前へ動いても、sequenceの
    # audio_generationが意図する5.1ch bitrateをencoder/cache keyの双方で使う。
    adjusted_segment = RecordedFMP4Segment(
        1,
        6.0,
        6.0,
        0,
        1,
        transcoded_audio_start_sample=6 * 48_000 - 1,
        transcoded_audio_sample_count=6 * 48_000 + 1,
    )
    assert get_cache_codec(adjusted_segment, rendition) == 'opus-continuous-256000-5.1'


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
    assert get_availability(13.0, sub) is False


@pytest.mark.parametrize(
    (
        'previous_audio_generation', 'fail_source', 'fail_validation', 'availability_known',
        'expected_success', 'generation_start_time', 'expected_seek', 'expected_trim_start',
        'audio_codec', 'channel_layout', 'segment_count',
    ),
    [
        (0, False, False, True, True, 36.0, '26.000000', 480_000, 'aac', 'stereo', 3),
        (9, True, False, False, True, 36.0, '36.000000', 0, 'aac', 'stereo', 3),
        (9, True, False, True, False, 36.0, '36.000000', 0, 'aac', 'stereo', 3),
        (0, False, False, True, True, 36.0, '26.000000', 480_000, 'opus', '5.1', 3),
        (0, False, True, True, False, 36.0, '26.000000', 480_000, 'aac', 'stereo', 3),
        (9, False, False, True, True, 7.04, '7.040000', 0, 'aac', 'mono', 3),
        (9, False, False, True, True, 7.04, '7.040000', 0, 'aac', 'mono', 1),
    ],
)
def test_continuous_audio_generation_uses_exact_boundaries_and_safe_fallback(
    monkeypatch,
    tmp_path,
    previous_audio_generation: int,
    fail_source: bool,
    fail_validation: bool,
    availability_known: bool,
    expected_success: bool,
    generation_start_time: float,
    expected_seek: str,
    expected_trim_start: int,
    audio_codec: AudioCodec,
    channel_layout: str,
    segment_count: int,
) -> None:
    """実音声を固定PCM/NUT化してから欠落補完し、generation全体を厳密に確定する。"""

    track = {
        'index': 1, 'stream_index': 2, 'pid': 0x111, 'codec': 'AAC-LC',
        'channel': {
            'mono': 'Monaural',
            'stereo': 'Stereo',
            '5.1': '5.1ch',
        }[channel_layout],
        'sampling_rate': 48_000,
        'channel_layout': channel_layout,
    }
    previous_track = {
        **track,
        'channel': 'Stereo',
        'channel_layout': 'stereo',
    }
    audio_track_timeline = (
        [{'start_time': 0.0, 'end_time': 60.0, 'tracks': [track]}]
        if availability_known is True else []
    )
    if generation_start_time == 7.04:
        # 録画127と同じく、直前までStereoだった入力へMonaural構成の先頭から直接seekする。
        audio_track_timeline = [
            {'start_time': 0.0, 'end_time': generation_start_time, 'tracks': [previous_track]},
            {'start_time': generation_start_time, 'end_time': 60.0, 'tracks': [track]},
        ]
    start_sample = round(generation_start_time * 48_000)
    sample_counts = (
        [48_648, 48_000, 47_640] if audio_codec == 'opus' else [48_128, 48_128, 48_032]
    )[:segment_count]
    generation_segments: list[RecordedFMP4Segment] = []
    next_sample = start_sample
    for sequence, sample_count in enumerate(sample_counts, start=1):
        generation_segments.append(RecordedFMP4Segment(
            sequence=sequence,
            start_time=next_sample / 48_000,
            duration=sample_count / 48_000,
            generation=0,
            audio_generation=0,
            transcoded_audio_generation=7,
            transcoded_audio_start_sample=next_sample,
            transcoded_audio_sample_count=sample_count,
        ))
        next_sample += sample_count

    stream = object.__new__(RecordedFMP4Stream)
    stream._segments = [
        RecordedFMP4Segment(0, generation_start_time - 6.0, 6.0, 0, previous_audio_generation),
        *generation_segments,
    ]
    stream.recorded_program = SimpleNamespace(recorded_video=SimpleNamespace(
        id=1,
        file_path='/recording.ts',
        container_format='MPEG-TS',
        audio_tracks=[previous_track],
        audio_track_timeline=audio_track_timeline,
    ))
    rendition = RecordedAudioRendition('1', 1, 2, 'all', 'Track 1', 'ja', 0x111)
    commands: list[list[str]] = []
    normalized_starts: list[float] = []
    validation_calls: list[dict[str, int]] = []
    generation_validation_calls: list[tuple[int, int]] = []
    writes: list[tuple[Path, bytes]] = []
    work_progresses: list[float] = []
    split_calls: list[tuple[bytes, list[int]]] = []
    error_messages: list[str] = []

    class FakeProcess:
        def __init__(self, command: list[str], returncode: int) -> None:
            self.command = command
            self.returncode = returncode
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(b'out_time_us=1500000\nprogress=continue\n')
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(b'source failure' if returncode != 0 else b'')
            self.stderr.feed_eof()

        async def wait(self) -> int:
            if self.returncode == 0:
                Path(self.command[-1]).write_bytes(b'single-output')
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    async def CreateSubprocessExec(*command, **_kwargs):
        normalized_command = [str(argument) for argument in command]
        commands.append(normalized_command)
        return FakeProcess(normalized_command, 1 if fail_source and len(commands) == 1 else 0)

    def Normalize(_init: bytes, media: bytes, start_time: float, _sequence: int) -> bytes:
        normalized_starts.append(start_time)
        return media

    def Validate(_cls, _init: bytes, _media: bytes, _codec: str, **kwargs) -> bool:
        validation_calls.append(kwargs)
        return fail_validation is False

    def ValidateGeneration(
        _cls,
        _init: bytes,
        media_fragments: list[bytes],
        _codec: str,
        expected_sample_count: int,
    ) -> bool:
        generation_validation_calls.append((len(media_fragments), expected_sample_count))
        return True

    async def WriteAtomic(path: Path, data: bytes) -> None:
        writes.append((path, data))

    async def ToThread(function, *args):
        return function(*args)

    def Split(_cls, output: bytes, expected_sample_counts: list[int]) -> tuple[bytes, list[bytes]]:
        split_calls.append((output, expected_sample_counts))
        return b'init-' + output, [
            f'media-{index}'.encode()
            for index in range(len(expected_sample_counts))
        ]

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', CreateSubprocessExec)
    monkeypatch.setattr(asyncio, 'to_thread', ToThread)
    monkeypatch.setattr(
        'app.streams.RecordedFMP4Stream.RecordedFMP4CacheManager.writeAtomic',
        WriteAtomic,
    )
    monkeypatch.setattr('app.streams.RecordedFMP4Stream.logging.error', error_messages.append)
    monkeypatch.setattr(
        RecordedFMP4Stream,
        'splitTranscodedAudioFMP4',
        classmethod(Split),
    )
    monkeypatch.setattr(RecordedFMP4Stream, 'normalizeFragmentTimeline', staticmethod(Normalize))
    monkeypatch.setattr(RecordedFMP4Stream, 'validateTranscodedAudioFragmentTimeline', classmethod(Validate))
    monkeypatch.setattr(
        RecordedFMP4Stream,
        'validateTranscodedAudioGenerationPacketTimeline',
        classmethod(ValidateGeneration),
    )
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__updateOfflineWorkProgress',
        lambda _self, _key, progress: work_progresses.append(progress),
    )
    monkeypatch.setattr(
        RecordedFMP4Stream,
        'patchAudioInitializationEditList',
        staticmethod(lambda _init, _delay: b'patched-init'),
    )
    monkeypatch.setattr(
        RecordedFMP4Stream,
        'validateAudioInitializationDelay',
        staticmethod(lambda _init, _codec, _delay: True),
    )

    async def Encode() -> bool:
        class NoopSemaphore:
            def locked(self) -> bool:
                return False

            async def acquire(self) -> bool:
                return True

            def release(self) -> None:
                return None

        stream._cpu_semaphore = NoopSemaphore()
        result = await stream._RecordedFMP4Stream__encodeTranscodedAudioGeneration(  # pyright: ignore[reportPrivateUsage]
            generation_segments,
            rendition,
            tmp_path / 'init.mp4',
            {segment.sequence: tmp_path / f'{segment.sequence}.m4s' for segment in generation_segments},
            audio_codec,
        )
        return result

    succeeded = asyncio.run(Encode())

    assert succeeded is expected_success
    # 固定PCM/NUT化と単一fMP4生成の2 processだけを使い、分割用FFmpegは起動しない。
    assert len(commands) == (1 if fail_source is True and availability_known is True else 2)
    assert all(
        '-c:a' not in command or command[command.index('-c:a') + 1] != 'copy'
        for command in commands
    )
    source_command = commands[0]
    assert source_command[source_command.index('-ss') + 1] == expected_seek
    source_input_index = source_command.index('-i')
    assert source_command[source_command.index('-probesize') + 1] == '50000000'
    assert source_command[source_command.index('-analyzeduration') + 1] == '5000000'
    assert source_command.index('-probesize') < source_input_index
    assert source_command.index('-analyzeduration') < source_input_index
    assert float(source_command[source_command.index('-t') + 1]) == pytest.approx(
        (expected_trim_start + sum(sample_counts)) / 48_000 + 0.1,
        abs=0.000001,
    )
    assert source_command[source_command.index('-c:a') + 1] == 'pcm_f32le'
    assert source_command[source_command.index('-f') + 1] == 'nut'
    source_filter = source_command[source_command.index('-af') + 1]
    assert 'aformat=' in source_filter
    assert f'channel_layouts={channel_layout}' in source_filter
    assert all(filter_name not in source_filter for filter_name in ('aresample', 'atrim', 'apad', 'asetpts'))
    if fail_source is True and availability_known is True:
        # 再生索引が存在を明示した音声を、抽出失敗時に成功扱いの無音へ置換しない。
        assert all('anullsrc=' not in argument for command in commands for argument in command)
        assert split_calls == []
        assert writes == []
        assert any('refusing to replace an indexed audio track with silence' in message for message in error_messages)
        return
    successful_command = commands[-1]
    if fail_source is False:
        input_index = successful_command.index('-i')
        input_layout_index = successful_command.index('-ch_layout:a:0')
        assert input_layout_index < input_index
        assert successful_command[input_layout_index + 1] == channel_layout
        assert successful_command[input_index + 1].endswith('.nut')
        normalize_filter = successful_command[successful_command.index('-af') + 1]
        normalize_steps = normalize_filter.split(',')
        assert normalize_steps == [
            f'aresample=48000:out_chlayout={channel_layout}:async=1:min_hard_comp=0.001:first_pts=0',
            f'atrim=start_sample={expected_trim_start}',
            f'apad=whole_len={sum(sample_counts)}',
            f'atrim=end_sample={sum(sample_counts)}',
            'asetpts=N/SR/TB',
        ]
    if audio_codec == 'opus':
        assert successful_command[successful_command.index('-c:a') + 1] == 'libopus'
        assert successful_command[successful_command.index('-b:a') + 1] == '256000'
        assert successful_command[successful_command.index('-vbr') + 1] == 'on'
        assert successful_command[successful_command.index('-application') + 1] == 'audio'
        assert successful_command[successful_command.index('-frame_duration') + 1] == '20'
        assert successful_command[successful_command.index('-compression_level') + 1] == '10'
        assert successful_command[successful_command.index('-mapping_family') + 1] == '1'
        expected_frame_count = (sum(sample_counts) + 312 + 959) // 960
        assert successful_command[successful_command.index('-frames:a') + 1] == str(expected_frame_count)
        assert successful_command[successful_command.index('-initial_offset') + 1] == '-0.006500000'
        expected_decode_offsets = [0, 48_960, 96_960][:segment_count]
        expected_fragment_sample_counts = [sample_counts[0] + 312, *sample_counts[1:]]
    else:
        assert successful_command[successful_command.index('-c:a') + 1] == 'aac'
        assert successful_command[successful_command.index('-b:a') + 1] == '192k'
        expected_frame_count = (sum(sample_counts) + 1024 + 1023) // 1024
        assert successful_command[successful_command.index('-frames:a') + 1] == str(expected_frame_count)
        assert successful_command[successful_command.index('-initial_offset') + 1] == '-0.021333333'
        expected_decode_offsets = [0, 49_152, 97_280][:segment_count]
        expected_fragment_sample_counts = [sample_counts[0] + 1024, *sample_counts[1:]]
    assert 'delay_moov' in successful_command[successful_command.index('-movflags') + 1]
    assert '-segment_times' not in successful_command
    assert split_calls == [(b'single-output', expected_fragment_sample_counts)]
    if fail_source:
        assert any('anullsrc=r=48000:cl=stereo' in argument for argument in successful_command)
        # 正規化を持たない無音再試行は、旧実装の固定10%ではなく0%からencode枠を使う。
        assert work_progresses[1] == pytest.approx(
            min(1.0, 1.5 / (sum(sample_counts) / 48_000)) * 0.95,
        )
    if fail_validation:
        assert all('anullsrc=r=48000:cl=stereo' not in argument for argument in successful_command)
        assert len(validation_calls) == 1
        assert generation_validation_calls == []
        assert writes == []
        assert any(
            'sequence: 1' in message and
            'expected_start_sample: 0' in message and
            f'expected_sample_count: {expected_fragment_sample_counts[0]}' in message and
            'actual_sample_count: None' in message
            for message in error_messages
        )
        return
    assert 0.99 in work_progresses
    assert normalized_starts == pytest.approx([
        (start_sample + offset) / 48_000
        for offset in expected_decode_offsets
    ])
    assert [call['expected_start_sample'] for call in validation_calls] == [
        expected_start
        for offset in expected_decode_offsets
        for expected_start in (0, start_sample + offset)
    ]
    assert all(call['validate_packet_timeline'] is False for call in validation_calls)
    assert generation_validation_calls == [
        (segment_count, sum(expected_fragment_sample_counts)),
        (segment_count, sum(expected_fragment_sample_counts)),
    ]
    assert len(writes) == segment_count + 1
    assert writes[0] == (tmp_path / 'init.mp4', b'patched-init')


def _BuildOfflineVideoStream() -> RecordedFMP4Stream:
    """オフライン連続encodeテスト用の最小セッションを返す。"""

    stream = object.__new__(RecordedFMP4Stream)
    stream.quality = '1080p-60fps'
    stream.encoding_options = SimpleNamespace(
        video_codec='av1',
        video_bit_depth=10,
        is_24fps_mode_enabled=False,
    )
    stream.recorded_program = SimpleNamespace(
        id=1,
        recorded_video=SimpleNamespace(
            id=1,
            file_path='/recording.mp4',
            container_format='MP4',
            video_scan_type='Progressive',
            video_stream_timeline=[],
        ),
    )
    stream._is_offline_continuous = True
    stream._offline_video_sequence_tasks = {}
    stream._offline_video_segment_events = {}
    stream._offline_video_encode_lock = asyncio.Lock()
    stream._completed_sequences = set()
    stream._referenced_paths = set()
    stream.keepAlive = lambda: None
    stream._segments = [
        RecordedFMP4Segment(0, 0.0, 6.0, 0, 0),
        RecordedFMP4Segment(1, 6.0, 6.0, 0, 0),
        RecordedFMP4Segment(2, 12.0, 6.0, 0, 0),
        RecordedFMP4Segment(3, 18.0, 6.0, 1, 0),
    ]
    return stream


def test_offline_video_run_split_times_use_output_timeline() -> None:
    """連続encodeの内部境界は出力時刻0起点の累積durationになる。"""

    stream = _BuildOfflineVideoStream()
    assert stream._RecordedFMP4Stream__getVideoRunSplitTimes(  # pyright: ignore[reportPrivateUsage]
        stream._segments[:3],
    ) == [6.0, 12.0]


def test_single_segment_offline_generation_uses_continuous_task(monkeypatch) -> None:
    """短い1segmentの映像世代も媒体時間進捗を持つ一括生成経路へ載せる。"""

    async def Verify() -> None:
        stream = _BuildOfflineVideoStream()
        stream._segments = [stream._segments[0]]
        created_runs: list[list[int]] = []

        async def EncodeRun(_self: RecordedFMP4Stream, segments: list[RecordedFMP4Segment]) -> bool:
            created_runs.append([segment.sequence for segment in segments])
            return True

        monkeypatch.setattr(RecordedFMP4Stream, '_RecordedFMP4Stream__encodeOfflineVideoRun', EncodeRun)

        await stream._RecordedFMP4Stream__startOfflineVideoEncodeIfNeeded(stream._segments[0])  # pyright: ignore[reportPrivateUsage]
        tasks = set(stream._offline_video_sequence_tasks.values())
        await asyncio.gather(*tasks)

        assert created_runs == [[0]]

    asyncio.run(Verify())


def test_offline_video_uncached_run_regenerates_whole_generation(tmp_path: Path) -> None:
    """オフライン保存は途中cacheがあっても映像generation全体を単一生成する。"""

    stream = _BuildOfflineVideoStream()
    cached = {1}

    def BuildCachePath(_self: RecordedFMP4Stream, segment: RecordedFMP4Segment, is_init: bool) -> Path:
        kind = 'init' if is_init else 'segment'
        return tmp_path / f'{kind}-{segment.sequence}.m4s'

    stream._RecordedFMP4Stream__buildCachePath = BuildCachePath.__get__(  # pyright: ignore[reportAttributeAccessIssue]
        stream,
        RecordedFMP4Stream,
    )
    uncached_run = stream._RecordedFMP4Stream__getUncachedVideoRun(  # pyright: ignore[reportPrivateUsage]
        stream._segments[0],
    )
    assert [segment.sequence for segment in uncached_run] == [0, 1, 2]

    (tmp_path / 'segment-1.m4s').write_bytes(b'cached')
    first_run = stream._RecordedFMP4Stream__getUncachedVideoRun(  # pyright: ignore[reportPrivateUsage]
        stream._segments[0],
    )
    third_run = stream._RecordedFMP4Stream__getUncachedVideoRun(  # pyright: ignore[reportPrivateUsage]
        stream._segments[2],
    )

    assert [segment.sequence for segment in first_run] == [0, 1, 2]
    assert [segment.sequence for segment in third_run] == [0, 1, 2]
    assert cached == {1}


def test_offline_continuous_video_command_uses_single_seek_and_output(monkeypatch) -> None:
    """オフライン連続encodeはpreroll付きseekを1回にし、単一MP4へ出力する。"""

    stream = _BuildOfflineVideoStream()
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__getBackend',
        lambda _self: 'FFmpeg',
    )

    command, backend, device, _pixel_format = stream._RecordedFMP4Stream__buildVideoEncodeCommand(  # pyright: ignore[reportPrivateUsage]
        0.0,
        18.0,
        [
            '-movflags', '+frag_keyframe+delay_moov+default_base_moof+negative_cts_offsets',
            '-progress', 'pipe:1', '-nostats',
            '-f', 'mp4', str(Path('/tmp') / 'encoded.mp4'),
        ],
        force_keyframe_times=[6.0, 12.0],
        sequence_for_warning=0,
    )

    assert backend == 'FFmpeg'
    assert device is None
    assert command[command.index('-ss') + 1] == '0.000000'
    assert command[command.index('-t') + 1] == '18.000000'
    assert command.count('-ss') == 1
    assert command[command.index('-force_key_frames') + 1] == '6.000000,12.000000'
    assert '-segment_times' not in command
    assert command[command.index('-progress') + 1] == 'pipe:1'
    assert command[command.index('-b:v') + 1] == '1887K'
    assert command[command.index('-maxrate') + 1] == '2803K'
    assert command[command.index('-bufsize') + 1] == '5606K'
    assert command[-1] == str(Path('/tmp') / 'encoded.mp4')


def test_mmt_tlv_video_command_forces_libaribtlv_before_input(monkeypatch) -> None:
    """録画映像 fMP4 の TLV 入力へ libaribtlv を指定し、出力条件は共通経路を使う。"""

    stream = _BuildOfflineVideoStream()
    stream.recorded_program.recorded_video.container_format = 'MMT/TLV'
    stream.recorded_program.recorded_video.file_path = '/recording.tlv'
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__getBackend',
        lambda _self: 'FFmpeg',
    )

    command, _backend, _device, _pixel_format = stream._RecordedFMP4Stream__buildVideoEncodeCommand(  # pyright: ignore[reportPrivateUsage]
        0.0,
        6.0,
        ['-f', 'mp4', 'pipe:1'],
        sequence_for_warning=0,
    )

    input_index = command.index('/recording.tlv')
    assert command[input_index - 5:input_index] == ['-f', 'libaribtlv', '-ss', '0.000000', '-i']
    assert command[command.index('-f') + 1] == 'libaribtlv'


def test_playback_video_segment_still_encodes_one_fragment(monkeypatch, tmp_path: Path) -> None:
    """通常再生は連続encodeせず、要求された1セグメントだけを生成する。"""

    async def RunSynchronously(function, *args, **kwargs):  # type: ignore[no-untyped-def]
        return function(*args, **kwargs)

    stream = _BuildOfflineVideoStream()
    stream._is_offline_continuous = False
    encoded: list[int] = []

    async def EncodeSegment(
        _self: RecordedFMP4Stream,
        segment: RecordedFMP4Segment,
        init_path: Path,
        segment_path: Path,
    ) -> None:
        encoded.append(segment.sequence)
        init_path.write_bytes(b'init')
        segment_path.write_bytes(b'media')

    monkeypatch.setattr(RecordedFMP4Stream, '_RecordedFMP4Stream__encodeSegment', EncodeSegment)
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__getBackend',
        lambda _self: 'FFmpeg',
    )
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__buildCachePath',
        lambda _self, segment, is_init: tmp_path / ('init.mp4' if is_init else f'segment-{segment.sequence}.m4s'),
    )
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__acquire',
        AsyncMock(side_effect=lambda path: _ImmediatePathLock()),
    )
    monkeypatch.setattr(
        'app.streams.RecordedFMP4Stream.RecordedPlaybackBackend.isCombinationSupported',
        staticmethod(lambda *_args, **_kwargs: True),
    )
    monkeypatch.setattr(asyncio, 'to_thread', RunSynchronously)

    data = asyncio.run(stream._RecordedFMP4Stream__getVideoSegment(0))  # pyright: ignore[reportPrivateUsage]

    assert data == b'media'
    assert encoded == [0]


def test_video_segment_is_regenerated_when_initialization_is_missing(monkeypatch, tmp_path: Path) -> None:
    """fragmentだけが残った不完全cacheではinitとfragmentを再生成する。"""

    async def RunSynchronously(function, *args, **kwargs):  # type: ignore[no-untyped-def]
        return function(*args, **kwargs)

    stream = _BuildOfflineVideoStream()
    stream._is_offline_continuous = False
    segment_path = tmp_path / 'segment-0.m4s'
    init_path = tmp_path / 'init.mp4'
    segment_path.write_bytes(b'stale-media')
    encoded: list[int] = []

    async def EncodeSegment(
        _self: RecordedFMP4Stream,
        segment: RecordedFMP4Segment,
        generated_init_path: Path,
        generated_segment_path: Path,
    ) -> None:
        encoded.append(segment.sequence)
        generated_init_path.write_bytes(b'init')
        generated_segment_path.write_bytes(b'fresh-media')

    monkeypatch.setattr(RecordedFMP4Stream, '_RecordedFMP4Stream__encodeSegment', EncodeSegment)
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__getBackend',
        lambda _self: 'FFmpeg',
    )
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__buildCachePath',
        lambda _self, _segment, is_init: init_path if is_init else segment_path,
    )
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__acquire',
        AsyncMock(side_effect=lambda path: _ImmediatePathLock()),
    )
    monkeypatch.setattr(
        'app.streams.RecordedFMP4Stream.RecordedPlaybackBackend.isCombinationSupported',
        staticmethod(lambda *_args, **_kwargs: True),
    )
    monkeypatch.setattr(asyncio, 'to_thread', RunSynchronously)

    data = asyncio.run(stream._RecordedFMP4Stream__getVideoSegment(0))  # pyright: ignore[reportPrivateUsage]

    assert data == b'fresh-media'
    assert init_path.read_bytes() == b'init'
    assert encoded == [0]


def test_offline_continuous_video_regenerates_fragment_when_initialization_is_missing_after_wait(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """連続生成の通知後に fragment だけなら、個別生成で init と fragment を復旧する。"""

    async def RunSynchronously(function, *args, **kwargs):  # type: ignore[no-untyped-def]
        return function(*args, **kwargs)

    stream = _BuildOfflineVideoStream()
    segment_path = tmp_path / 'segment-0.m4s'
    init_path = tmp_path / 'init.mp4'
    encoded: list[int] = []

    async def EncodeSegment(
        _self: RecordedFMP4Stream,
        segment: RecordedFMP4Segment,
        generated_init_path: Path,
        generated_segment_path: Path,
    ) -> None:
        encoded.append(segment.sequence)
        generated_init_path.write_bytes(b'init')
        generated_segment_path.write_bytes(b'fresh-media')

    async def StartOffline(_self: RecordedFMP4Stream, _segment: RecordedFMP4Segment) -> None:
        return None

    async def WaitOffline(_self: RecordedFMP4Stream, _segment: RecordedFMP4Segment) -> None:
        # 実ブラウザで観測した、連続生成の完了通知後に fragment だけが見える状態を再現する。
        segment_path.write_bytes(b'incomplete-media')

    monkeypatch.setattr(RecordedFMP4Stream, '_RecordedFMP4Stream__encodeSegment', EncodeSegment)
    monkeypatch.setattr(RecordedFMP4Stream, '_RecordedFMP4Stream__startOfflineVideoEncodeIfNeeded', StartOffline)
    monkeypatch.setattr(RecordedFMP4Stream, '_RecordedFMP4Stream__waitOfflineVideoSegment', WaitOffline)
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__getBackend',
        lambda _self: 'FFmpeg',
    )
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__buildCachePath',
        lambda _self, _segment, is_init: init_path if is_init else segment_path,
    )
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__acquire',
        AsyncMock(side_effect=lambda path: _ImmediatePathLock()),
    )
    monkeypatch.setattr(
        'app.streams.RecordedFMP4Stream.RecordedPlaybackBackend.isCombinationSupported',
        staticmethod(lambda *_args, **_kwargs: True),
    )
    monkeypatch.setattr(asyncio, 'to_thread', RunSynchronously)

    data = asyncio.run(stream._RecordedFMP4Stream__getVideoSegment(0))  # pyright: ignore[reportPrivateUsage]

    assert data == b'fresh-media'
    assert init_path.read_bytes() == b'init'
    assert encoded == [0]


def test_offline_continuous_video_falls_back_to_single_segment(monkeypatch, tmp_path: Path) -> None:
    """連続encodeが失敗したsequenceは現行の1セグメントencodeへ落とす。"""

    async def RunSynchronously(function, *args, **kwargs):  # type: ignore[no-untyped-def]
        return function(*args, **kwargs)

    stream = _BuildOfflineVideoStream()
    encoded: list[int] = []

    async def EncodeSegment(
        _self: RecordedFMP4Stream,
        segment: RecordedFMP4Segment,
        init_path: Path,
        segment_path: Path,
    ) -> None:
        encoded.append(segment.sequence)
        init_path.write_bytes(b'init')
        segment_path.write_bytes(b'fallback')

    async def StartOffline(_self: RecordedFMP4Stream, _segment: RecordedFMP4Segment) -> None:
        return None

    async def WaitOffline(_self: RecordedFMP4Stream, _segment: RecordedFMP4Segment) -> None:
        return None

    monkeypatch.setattr(RecordedFMP4Stream, '_RecordedFMP4Stream__encodeSegment', EncodeSegment)
    monkeypatch.setattr(RecordedFMP4Stream, '_RecordedFMP4Stream__startOfflineVideoEncodeIfNeeded', StartOffline)
    monkeypatch.setattr(RecordedFMP4Stream, '_RecordedFMP4Stream__waitOfflineVideoSegment', WaitOffline)
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__getBackend',
        lambda _self: 'FFmpeg',
    )
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__buildCachePath',
        lambda _self, segment, is_init: tmp_path / ('init.mp4' if is_init else f'segment-{segment.sequence}.m4s'),
    )
    monkeypatch.setattr(
        RecordedFMP4Stream,
        '_RecordedFMP4Stream__acquire',
        AsyncMock(side_effect=lambda path: _ImmediatePathLock()),
    )
    monkeypatch.setattr(
        'app.streams.RecordedFMP4Stream.RecordedPlaybackBackend.isCombinationSupported',
        staticmethod(lambda *_args, **_kwargs: True),
    )
    monkeypatch.setattr(asyncio, 'to_thread', RunSynchronously)

    data = asyncio.run(stream._RecordedFMP4Stream__getVideoSegment(0))  # pyright: ignore[reportPrivateUsage]

    assert data == b'fallback'
    assert encoded == [0]


class _ImmediatePathLock:
    """テスト用に即座に出入りできる path lock。"""

    async def __aenter__(self) -> '_ImmediatePathLock':
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None
