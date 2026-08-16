import asyncio
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import Response

import app.streams.KonomiTVBS4KPlaybackCapabilities as playback_capabilities_module
from app.config import ServerSettings
from app.constants import QUALITY_TYPES
from app.routers import LiveStreamsRouter, VideoStreamsRouter
from app.streams.KonomiTVBS4KPlaybackCapabilities import (
    KonomiTVBS4KPlaybackCapabilityProbe,
)
from app.streams.KonomiTVBS4KPlaybackEncoding import (
    KONOMITV_BS4K_AUDIO_CODECS,
    KONOMITV_BS4K_AV1_MAIN_TIER_TSTD_LIMITS_BY_QUALITY,
    KONOMITV_BS4K_VIDEO_CODECS,
    BuildKonomiTVBS4KLiveAspectPreservingScaleFilters,
    BuildKonomiTVBS4KLiveHardwareVideoFilters,
    KonomiTVBS4KPlaybackAudioCapability,
    KonomiTVBS4KPlaybackCapabilities,
    KonomiTVBS4KPlaybackLiveCombinationCapability,
    KonomiTVBS4KPlaybackVideoCapability,
    KonomiTVBS4KVideoBitDepth,
    KonomiTVBS4KVideoBitDepthQuery,
    KonomiTVBS4KVideoCodec,
    ParseKonomiTVBS4KAdvancedLiveMuxrateKbps,
    ResolveKonomiTVBS4KAdvancedLiveMuxrate,
    ResolveKonomiTVBS4KLiveEncodePlan,
    ResolveKonomiTVBS4KLiveEncodeSize,
    ResolveKonomiTVBS4KLiveOutputGeometry,
    ResolveKonomiTVBS4KPlaybackVideoBitrate,
    ShouldUseKonomiTVBS4KLiveSoftwareDecodeForSar,
)
from app.streams.LiveStream import LiveStream
from app.streams.RecordedFMP4Stream import RecordedFMP4Stream
from app.streams.RecordedPlaybackCapabilities import (
    RecordedPlaybackCapability,
    RecordedPlaybackCapabilityProbe,
)
from app.streams.StreamEncodingOptions import (
    SplitQualityAndEncodingOptions,
    StreamEncodingOptions,
)


class _LiveChannelQuery:
    """ValidateQuality の実 backend 解決に使うチャンネルクエリ代替。"""

    def __init__(
        self,
        is_radiochannel: bool,
        *,
        channel_type: str = 'GR',
        network_id: int = 0x7880,
        service_id: int = 101,
    ) -> None:
        self.is_radiochannel = is_radiochannel
        self.channel_type = channel_type
        self.network_id = network_id
        self.service_id = service_id

    async def get_or_none(self) -> SimpleNamespace:
        """ラジオ・ワンセグ種別を持つチャンネル代替を返す。"""

        return SimpleNamespace(
            is_radiochannel = self.is_radiochannel,
            is_oneseg = False,
            type = self.channel_type,
            network_id = self.network_id,
            service_id = self.service_id,
        )


@pytest.fixture(autouse = True)
def RunThreadWorkInlineOnHost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """終了不能なhost default executorを避け、to_threadへ渡す同期処理本体を検証する。"""

    async def RunInline(
        function: Callable[..., object],
        /,
        *args: object,
        **kwargs: object,
    ) -> object:
        """能力検査がworkerへ委譲する同期処理を、この単体試験内だけ直接実行する。"""

        return function(*args, **kwargs)

    monkeypatch.setattr(playback_capabilities_module.asyncio, 'to_thread', RunInline)


def _mpeg_crc32(data: bytes) -> bytes:
    """PSI section 用の MPEG-2 CRC32 を返す。"""

    crc = 0xFFFFFFFF
    for value in data:
        crc ^= value << 24
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if crc & 0x80000000
                else (crc << 1) & 0xFFFFFFFF
            )
    return crc.to_bytes(4, 'big')


def _wrap_psi_section(pid: int, section: bytes) -> bytes:
    """単一 TS packet に収まる PSI section を pointer field 付きで包む。"""

    header = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10])
    payload = b'\x00' + section
    return header + payload + b'\xFF' * (188 - len(header) - len(payload))


def _build_live_probe_pat() -> bytes:
    """テスト用合成 probe の PMT PID を告知する PAT を返す。"""

    pat_body = b'\x00\x01\xC1\x00\x00' + b'\x00\x01\xF0\x00'
    pat_without_crc = b'\x00' + (0xB000 | len(pat_body) + 4).to_bytes(2, 'big') + pat_body
    return pat_without_crc + _mpeg_crc32(pat_without_crc)


def _build_live_probe_pmt(
    video_descriptors: bytes,
    opus_pid_count: int,
) -> bytes:
    """指定映像 descriptor と Opus track を持つ probe 用 PMT を返す。"""

    def stream(stream_type: int, pid: int, descriptors: bytes) -> bytes:
        """descriptor 列を持つ一つの ES entry を返す。"""

        return (
            bytes([stream_type]) +
            (0xE000 | pid).to_bytes(2, 'big') +
            (0xF000 | len(descriptors)).to_bytes(2, 'big') +
            descriptors
        )

    streams = stream(0x06, 0x0100, video_descriptors)
    for index in range(opus_pid_count):
        streams += stream(
            0x06,
            0x0110 + index,
            b'\x05\x04Opus\x7F\x02\x80\x01',
        )
    pmt_body = (
        b'\x00\x01\xC1\x00\x00' +
        b'\xE1\x00' +
        b'\xF0\x00' +
        streams
    )
    pmt_without_crc = b'\x02' + (0xB000 | len(pmt_body) + 4).to_bytes(2, 'big') + pmt_body
    return pmt_without_crc + _mpeg_crc32(pmt_without_crc)


def _build_radio_opus_probe_pmt(
    opus_pid_count: int,
    include_video: bool = False,
    opus_descriptor_case: str = 'valid',
) -> bytes:
    """指定数の Opus track と任意の混入映像を持つラジオ probe 用 PMT を返す。"""

    def stream(stream_type: int, pid: int, descriptors: bytes) -> bytes:
        """descriptor 列を持つ一つの ES entry を返す。"""

        return (
            bytes([stream_type])
            + (0xE000 | pid).to_bytes(2, 'big')
            + (0xF000 | len(descriptors)).to_bytes(2, 'big')
            + descriptors
        )

    streams = b''
    opus_descriptors = b'\x05\x04Opus\x7f\x02\x80\x01'
    if opus_descriptor_case == 'missing-extension':
        opus_descriptors = b'\x05\x04Opus'
    elif opus_descriptor_case == 'unknown-extension':
        opus_descriptors = b'\x05\x04Opus\x7f\x02\x81\x01'
    elif opus_descriptor_case == 'unsupported-channel-configuration':
        opus_descriptors = b'\x05\x04Opus\x7f\x02\x80\x09'
    for index in range(opus_pid_count):
        streams += stream(
            0x06,
            0x0110 + index,
            opus_descriptors,
        )
    if include_video is True:
        streams += stream(0x1B, 0x0100, b'')
    pmt_body = b'\x00\x01\xc1\x00\x00' + b'\xe1\x10' + b'\xf0\x00' + streams
    pmt_without_crc = (
        b'\x02' + (0xB000 | len(pmt_body) + 4).to_bytes(2, 'big') + pmt_body
    )
    return pmt_without_crc + _mpeg_crc32(pmt_without_crc)


def _build_live_probe_video_descriptors(
    video_codec: KonomiTVBS4KVideoCodec,
    bit_depth: KonomiTVBS4KVideoBitDepth,
    mapping_case: str,
) -> bytes:
    """正常系または指定した破損条件の映像 mapping descriptor を返す。"""

    if video_codec == 'vp9':
        registration = b'\x05\x04VP09'
        private_payload = b'KTVB\x09\x01\xF0\x00'
        if mapping_case == 'missing':
            return registration
        if mapping_case == 'unknown-version':
            private_payload = b'KTVB\x09\x02\xF0\x00'
        return registration + b'\x80\x08' + private_payload

    registration = b'\x05\x04AV01'
    high_bitdepth = 0x40 if bit_depth == 10 else 0x00
    video_payload = bytes([0x81, 0x00, high_bitdepth | 0x0C, 0xC0])
    if mapping_case == 'missing':
        return registration
    if mapping_case == 'invalid-marker-version':
        video_payload = bytes([0x82, *video_payload[1:]])
    elif mapping_case == 'reserved-bit':
        video_payload = bytes([*video_payload[:3], video_payload[3] | 0x20])
    elif mapping_case == 'bit-depth-mismatch':
        video_payload = bytes([
            *video_payload[:2],
            video_payload[2] ^ 0x40,
            video_payload[3],
        ])
    return registration + b'\x80\x04' + video_payload


def _encode_probe_pts(pts: int) -> bytes:
    """合成probe用にmarker bit付き33bit PTSを符号化する。"""

    return bytes([
        0x20 | (((pts >> 30) & 0x07) << 1) | 0x01,
        (pts >> 22) & 0xFF,
        (((pts >> 15) & 0x7F) << 1) | 0x01,
        (pts >> 7) & 0xFF,
        ((pts & 0x7F) << 1) | 0x01,
    ])


def _wrap_probe_pes_packet(
    pid: int,
    stream_id: int,
    *,
    continuity_counter: int = 0,
    pcr: bool = False,
    random_access: bool = False,
) -> bytes:
    """PTS付きPESを単一TS packetへ収め、任意にPCR/RAIを付ける。"""

    elementary_payload = b'\x12\x34\x56\x78'
    pes_optional_header = b'\x84\x80\x05' + _encode_probe_pts(90_000)
    pes_packet_length = len(pes_optional_header) + len(elementary_payload)
    pes = (
        b'\x00\x00\x01'
        + bytes([stream_id])
        + pes_packet_length.to_bytes(2, 'big')
        + pes_optional_header
        + elementary_payload
    )

    if pcr or random_access:
        flags = (0x10 if pcr else 0x00) | (0x40 if random_access else 0x00)
        adaptation = bytes([flags])
        if pcr:
            adaptation += b'\x00\x00\x00\x00\x7E\x00'
        header = bytes([
            0x47,
            0x40 | (pid >> 8),
            pid & 0xFF,
            0x30 | continuity_counter,
        ])
        packet = header + bytes([len(adaptation)]) + adaptation + pes
    else:
        header = bytes([
            0x47,
            0x40 | (pid >> 8),
            pid & 0xFF,
            0x10 | continuity_counter,
        ])
        packet = header + pes
    return packet + b'\xFF' * (188 - len(packet))


def _build_live_probe_transport_packets(
    audio_pid_count: int,
    *,
    include_video: bool,
    video_stream_id: int = 0xE0,
) -> bytes:
    """PMT宣言だけでなくPCR・RAI・全ESの実PESを持つprobe断片を返す。"""

    packets = b''
    if include_video:
        packets += _wrap_probe_pes_packet(
            0x0100,
            video_stream_id,
            pcr = True,
            random_access = True,
        )
    for index in range(audio_pid_count):
        packets += _wrap_probe_pes_packet(
            0x0110 + index,
            0xC0 + index,
            pcr = include_video is False and index == 0,
        )
    return packets


def test_konomitv_bs4k_common_codec_domain_and_bitrate_are_shared() -> None:
    """codec型とビットレート計算の権威データがライブ・録画共通moduleにある。"""

    assert set(KONOMITV_BS4K_VIDEO_CODECS) == {'avc', 'hevc', 'vp9', 'av1'}
    assert set(KONOMITV_BS4K_AUDIO_CODECS) == {'aac', 'opus'}
    assert KONOMITV_BS4K_VIDEO_CODECS['avc'].supports_10bit is False
    assert all(KONOMITV_BS4K_VIDEO_CODECS[codec].supports_10bit for codec in ('hevc', 'vp9', 'av1'))
    assert RecordedFMP4Stream.getVideoBitrate('1080p', 'av1') == \
        ResolveKonomiTVBS4KPlaybackVideoBitrate('1080p', 'av1')


@pytest.mark.parametrize(
    ('width', 'height', 'is_fullhd', 'expected_coded', 'expected_sar', 'expected_square'),
    [
        # 選択解像度ごとの SAR（DAR 16:9）
        (1440, 1080, False, (1440, 1080), (4, 3), (1920, 1080)),
        (1440, 1080, True, (1920, 1080), (1, 1), (1920, 1080)),
        (1280, 720, False, (1280, 720), (1, 1), (1280, 720)),
        (1440, 810, False, (1440, 810), (1, 1), (1440, 810)),
        (960, 540, False, (960, 540), (1, 1), (960, 540)),
        (854, 480, False, (854, 480), (1280, 1281), (852, 480)),
        (640, 360, False, (640, 360), (1, 1), (640, 360)),
        (426, 240, False, (426, 240), (640, 639), (426, 240)),
    ],
)
def test_live_output_geometry_uses_resolution_specific_sar(
    width: int,
    height: int,
    is_fullhd: bool,
    expected_coded: tuple[int, int],
    expected_sar: tuple[int, int],
    expected_square: tuple[int, int],
) -> None:
    """画質解像度ごとに DAR 16:9 を満たす SAR / 正方画素サイズを返す。"""

    geometry = ResolveKonomiTVBS4KLiveOutputGeometry(
        width,
        height,
        is_fullhd_channel = is_fullhd,
    )
    assert (geometry.coded_width, geometry.coded_height) == expected_coded
    assert geometry.sample_aspect_ratio == expected_sar
    assert (geometry.square_pixel_width, geometry.square_pixel_height) == expected_square


@pytest.mark.parametrize('codec', ['avc', 'hevc', 'vp9', 'av1'])
def test_live_encode_size_applies_sar_policy_per_codec(codec: KonomiTVBS4KVideoCodec) -> None:
    """AVC/HEVC は anamorphic+SAR、VP9/AV1 は非正方 SAR 時に正方画素へ展開する。"""

    encode_w, encode_h, sar = ResolveKonomiTVBS4KLiveEncodeSize(
        1440,
        1080,
        video_codec = codec,
        is_fullhd_channel = False,
    )
    if codec in ('avc', 'hevc'):
        assert (encode_w, encode_h) == (1440, 1080)
        assert sar == (4, 3)
    else:
        assert (encode_w, encode_h) == (1920, 1080)
        assert sar == (1, 1)

    # 正方画素の 720p は 4 codec とも同じ
    encode_w, encode_h, sar = ResolveKonomiTVBS4KLiveEncodeSize(
        1280,
        720,
        video_codec = codec,
        is_fullhd_channel = False,
    )
    assert (encode_w, encode_h) == (1280, 720)
    assert sar == (1, 1)


def test_live_aspect_preserving_filters_use_isdb_sar_dynamically() -> None:
    """ISDB の SAR で正方画素化し、16:9 枠へ pad する filter 列を組む。"""

    # AVC 1080p anamorphic: 表示枠 1920x1080 → coded 1440x1080 SAR 4:3
    avc_plan = ResolveKonomiTVBS4KLiveEncodePlan(
        1440,
        1080,
        video_codec = 'avc',
        is_fullhd_channel = False,
    )
    avc_filters = BuildKonomiTVBS4KLiveAspectPreservingScaleFilters(avc_plan)
    assert "iw*sar" in avc_filters[0]
    assert avc_filters[1] == 'setsar=1/1'
    assert avc_filters[2] == (
        'scale=w=1920:h=1080:force_original_aspect_ratio=decrease:eval=frame'
    )
    assert avc_filters[3] == 'pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black'
    assert avc_filters[4] == 'scale=1440:1080'
    assert avc_filters[5] == 'setsar=4/3'

    # AV1 1080p: 正方画素 1920x1080（再 scale なし）
    av1_plan = ResolveKonomiTVBS4KLiveEncodePlan(
        1440,
        1080,
        video_codec = 'av1',
        is_fullhd_channel = False,
    )
    av1_filters = BuildKonomiTVBS4KLiveAspectPreservingScaleFilters(av1_plan)
    assert "iw*sar" in av1_filters[0]
    assert av1_filters[2].startswith('scale=w=1920:h=1080:')
    assert av1_filters[3] == 'pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black'
    assert av1_filters[-1] == 'setsar=1/1'
    assert not any(f == 'scale=1440:1080' for f in av1_filters)


def test_live_hwdownload_format_matches_hw_surface() -> None:
    """hwdownload 形式が HW 面とずれない。"""

    from app.streams.KonomiTVBS4KPlaybackEncoding import (
        ResolveKonomiTVBS4KLiveHwDownloadFormat,
    )

    assert ResolveKonomiTVBS4KLiveHwDownloadFormat(
        'QSV', channel_type = 'GR', encoder_pixel_format = 'p010le',
    ) == 'nv12'
    assert ResolveKonomiTVBS4KLiveHwDownloadFormat(
        'NVENC', channel_type = 'BS4K', encoder_pixel_format = 'nv12',
    ) == 'p010le'
    assert ResolveKonomiTVBS4KLiveHwDownloadFormat(
        'NVENC', channel_type = 'GR', encoder_pixel_format = 'nv12',
    ) == 'nv12'


def test_konomitv_bs4k_live_sar_mode_defaults_to_cpu() -> None:
    """yaml にキーが無いとき、ライブ SAR モードは CPU になる。"""

    settings = ServerSettings.model_validate({}, context = {'bypass_validation': True})
    assert settings.general.konomitv_bs4k_live_sar_mode == 'CPU'


@pytest.mark.parametrize(
    ('channel_type', 'sar_mode', 'is_24fps', 'expected'),
    [
        ('GR', 'CPU', False, True),
        ('GR', 'GPU', False, False),
        ('GR', 'GPU', True, True),
        ('BS', 'CPU', False, True),
        ('BS4K', 'CPU', False, False),
        ('BS4K', 'CPU', True, False),
        ('BS4K', 'GPU', True, False),
    ],
)
def test_live_software_decode_is_required_to_keep_mpeg2_sar(
    channel_type: str,
    sar_mode: str,
    is_24fps: bool,
    expected: bool,
) -> None:
    """地デジ/BS の SAR 追従は SW decode、BS4K と GPU 決め打ちは HW decode。"""

    assert ShouldUseKonomiTVBS4KLiveSoftwareDecodeForSar(
        channel_type = channel_type,
        sar_mode = sar_mode,  # type: ignore[arg-type]
        is_24fps_mode_enabled = is_24fps,
    ) is expected


@pytest.mark.parametrize(('encode_width', 'encode_height'), [(1440, 1080), (1920, 1080), (426, 240)])
def test_konomitv_bs4k_live_hardware_filters_stretch_without_size_assumption(
    encode_width: int,
    encode_height: int,
) -> None:
    """GPU 縮小は入力解像度を決め打ちせず、coded size を encode size へ伸縮するだけ。"""

    filters = BuildKonomiTVBS4KLiveHardwareVideoFilters(
        'QSV',
        encode_width = encode_width,
        encode_height = encode_height,
        encoder_pixel_format = 'nv12',
        is_interlaced = False,
        is_60fps = False,
        low_latency = True,
    )
    combined = ','.join(filters)
    assert f'w={encode_width}:h={encode_height}' in combined
    assert 'iw*sar' not in combined
    assert 'setsar' not in combined


@pytest.mark.parametrize('encoder_type', ['QSV', 'NVENC', 'AMF'])
def test_konomitv_bs4k_live_hardware_filters_use_top_field_first(encoder_type: str) -> None:
    """ライブの HW DI は ISDB の top-field-first に固定し、自動判定で逆順にしない。"""

    filters = BuildKonomiTVBS4KLiveHardwareVideoFilters(
        encoder_type,  # type: ignore[arg-type]
        encode_width = 426,
        encode_height = 240,
        encoder_pixel_format = 'nv12',
        is_interlaced = True,
        is_60fps = False,
        low_latency = True,
    )
    combined = ','.join(filters)
    if encoder_type == 'NVENC':
        assert 'parity=0' in combined
    elif encoder_type == 'AMF':
        assert 'auto=0' in combined


@pytest.mark.parametrize(
    ('video_bitrate_max', 'expected_muxrate'),
    [
        ('2100K', '3500K'),
        ('9450K', '11813K'),
    ],
)
def test_konomitv_bs4k_advanced_live_muxrate_reserves_bounded_headroom(
    video_bitrate_max: str,
    expected_muxrate: str,
) -> None:
    """高度映像TSは最低1400Kbpsまたは最大映像rateの25%を搬送余力にする。"""

    assert ResolveKonomiTVBS4KAdvancedLiveMuxrate(video_bitrate_max) == expected_muxrate


def test_konomitv_bs4k_av1_muxrate_obeys_required_minimum_level_tstd_rx() -> None:
    """全固定 AV1 画質で TS 全体を必要最小 Level の T-STD Rx 以下にする。"""

    cases: tuple[tuple[QUALITY_TYPES, str, int, int, str], ...] = (
        ('4320p', '6.1', 17, 110000, '63000K'),
        ('2160p', '5.1', 13, 44000, '28350K'),
        ('1440p', '5.0', 12, 33000, '16800K'),
        ('1080p-60fps', '4.1', 9, 22000, '12640K'),
        ('1080p-30fps', '4.0', 8, 13200, '12150K'),
        ('810p', '4.0', 8, 13200, '11590K'),
        ('720p', '3.1', 5, 11000, '11000K'),
        ('540p', '3.0', 4, 6600, '6600K'),
        ('480p', '3.0', 4, 6600, '6600K'),
        ('360p', '2.1', 1, 3300, '3300K'),
        ('240p', '2.0', 0, 1650, '1650K'),
    )
    for quality, expected_level, expected_sequence_level_index, expected_rx_kbps, expected_muxrate in cases:
        bitrate = ResolveKonomiTVBS4KPlaybackVideoBitrate(quality, 'av1')
        tstd_limit = KONOMITV_BS4K_AV1_MAIN_TIER_TSTD_LIMITS_BY_QUALITY[quality]
        assert tstd_limit.minimum_level == expected_level
        assert tstd_limit.minimum_sequence_level_index == expected_sequence_level_index
        assert tstd_limit.maximum_transport_rate_kbps == expected_rx_kbps
        assert tstd_limit.maximum_transport_rate_kbps == (
            tstd_limit.maximum_bitrate_kbps * 11 // 10
        )
        muxrate = ResolveKonomiTVBS4KAdvancedLiveMuxrate(
            bitrate.video_bitrate_max,
            quality = quality,
            video_codec = 'av1',
        )
        assert muxrate == expected_muxrate
        assert int(muxrate.removesuffix('K')) <= expected_rx_kbps






def test_konomitv_bs4k_advanced_live_muxrate_rejects_partial_stream_context() -> None:
    """T-STD 上限を選ぶ画質と codec は片方だけを渡せない。"""

    with pytest.raises(ValueError, match = 'quality and video codec'):
        ResolveKonomiTVBS4KAdvancedLiveMuxrate('420K', quality = '240p')
    with pytest.raises(ValueError, match = 'quality and video codec'):
        ResolveKonomiTVBS4KAdvancedLiveMuxrate('420K', video_codec = 'av1')


@pytest.mark.parametrize('invalid_bitrate', ['0K', 'abcK'])
def test_konomitv_bs4k_advanced_live_muxrate_rejects_invalid_input(
    invalid_bitrate: str,
) -> None:
    """muxrate計算は正のKbps表記以外を黙って受理しない。"""

    with pytest.raises(ValueError, match='Invalid advanced live maximum bitrate'):
        ResolveKonomiTVBS4KAdvancedLiveMuxrate(invalid_bitrate)


def test_konomitv_bs4k_advanced_live_muxrate_is_converted_for_bridge() -> None:
    """Bridge の T-STD 入力へ FFmpeg と同一の搬送レートを渡す。"""

    assert ParseKonomiTVBS4KAdvancedLiveMuxrateKbps('2200K') == '2200'




def test_konomitv_bs4k_invalid_avc_10bit_is_rejected_without_silent_fallback() -> None:
    """明示queryと旧suffixのどちらでもAVC 10bitを8bitへ黙って落とさない。"""

    assert SplitQualityAndEncodingOptions(
        '1080p',
        encoder = 'FFmpeg',
        video_codec = 'avc',
        video_bit_depth = 10,
    ) is None
    assert SplitQualityAndEncodingOptions('1080p-10bit', encoder = 'QSV') is None
    with pytest.raises(ValueError, match='Unsupported video codec/bit depth combination'):
        StreamEncodingOptions.fromRequest(
            '1080p',
            False,
            False,
            encoder = 'FFmpeg',
            video_codec = 'avc',
            video_bit_depth = 10,
        )


def test_konomitv_bs4k_legacy_hevc_10bit_suffix_stays_exact_for_every_backend() -> None:
    """旧HEVC 10bit suffixもbackendによって8bitへ黙って降格しない。"""

    for encoder in ('FFmpeg', 'QSV', 'NVENC', 'AMF'):
        stream_quality = SplitQualityAndEncodingOptions(
            '1080p-hevc-10bit',
            encoder = encoder,  # type: ignore[arg-type]
        )
        assert stream_quality is not None
        assert stream_quality.encoding_options.video_codec == 'hevc'
        assert stream_quality.encoding_options.video_bit_depth == 10


def test_konomitv_bs4k_live_stream_key_separates_every_encoding_condition() -> None:
    """同一チャンネル・画質でもcodec、bit depth、音声、24fps、降雨対応が違えば共有しない。"""

    display_channel_id = 'bs4k101-konomitv-bs4k-key-contract'
    profiles = (
        StreamEncodingOptions(),
        StreamEncodingOptions(video_codec = 'av1'),
        StreamEncodingOptions(video_codec = 'av1', video_bit_depth = 10),
        StreamEncodingOptions(video_codec = 'av1', video_bit_depth = 10, audio_codec = 'opus'),
        StreamEncodingOptions(
            is_24fps_mode_enabled = True,
            video_codec = 'av1',
            video_bit_depth = 10,
            audio_codec = 'opus',
        ),
        StreamEncodingOptions(
            is_24fps_mode_enabled = True,
            video_codec = 'av1',
            video_bit_depth = 10,
            audio_codec = 'opus',
            use_rain_fallback = False,
        ),
    )
    streams = [LiveStream(display_channel_id, '1080p', profile) for profile in profiles]

    assert len({id(stream) for stream in streams}) == len(profiles)
    assert LiveStream(display_channel_id, '1080p', profiles[-1]) is streams[-1]
    assert streams[0].live_stream_id == f'{display_channel_id}-1080p'
    assert streams[-1].live_stream_id == f'{display_channel_id}-1080p-av1-10bit-opus-24fps-norain'
    assert streams[-1].live_stream_key == (
        display_channel_id,
        '1080p',
        'av1',
        10,
        'opus',
        True,
        False,
    )


def test_konomitv_bs4k_rain_fallback_is_normalized_for_ineffective_quality() -> None:
    """1080pを超える画質では無効な降雨設定を既定値へ戻し、-norainストリームを作らない。"""

    stream_quality = SplitQualityAndEncodingOptions(
        '2160p',
        encoder='FFmpeg',
        use_rain_fallback=False,
    )

    assert stream_quality is not None
    assert stream_quality.encoding_options.use_rain_fallback is True
    assert stream_quality.encoding_options.buildSuffix() == ''


@pytest.mark.parametrize(
    ('transport', 'channel_type', 'network_id', 'service_id', 'quality', 'expected_use_rain_fallback'),
    [
        ('Tlv', 'BS4K', 0x000B, 101, '1080p', False),
        ('Tlv', 'BS4K', 0x000B, 102, '1080p', False),
        ('Tlv', 'BS4K', 0x000B, 101, '2160p', True),
        ('Tlv', 'BS4K', 0x000B, 191, '1080p', True),
        ('Tlv', 'BS4K', 0x0004, 101, '1080p', True),
        ('MpegTs', 'BS4K', 0x000B, 101, '1080p', True),
    ],
)
def test_konomitv_bs4k_live_query_keeps_rain_setting_only_when_effective(
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
    channel_type: str,
    network_id: int,
    service_id: int,
    quality: str,
    expected_use_rain_fallback: bool,
) -> None:
    """TLVのSID 101/102かつ1080p以下だけ、降雨設定を共有キーへ残す。"""

    monkeypatch.setattr(
        LiveStreamsRouter,
        'GetEncoderForLiveChannel',
        lambda _display_channel_id: 'FFmpeg',
    )
    monkeypatch.setattr(
        LiveStreamsRouter.Channel,
        'filter',
        lambda **_kwargs: _LiveChannelQuery(
            is_radiochannel=False,
            channel_type=channel_type,
            network_id=network_id,
            service_id=service_id,
        ),
    )
    monkeypatch.setattr(
        LiveStreamsRouter,
        'Config',
        lambda: SimpleNamespace(
            general=SimpleNamespace(konomitv_bs4k_live_transport=transport),
        ),
    )

    stream_quality = asyncio.run(
        LiveStreamsRouter.ValidateQuality(
            quality,
            f'bs4k{service_id}',
            None,
            None,
            'aac',
            False,
        )
    )

    assert stream_quality.encoding_options.use_rain_fallback is expected_use_rain_fallback
    assert stream_quality.encoding_options.buildSuffix().endswith('-norain') is (
        expected_use_rain_fallback is False
    )


def test_konomitv_bs4k_default_hevc_quality_keeps_legacy_external_id() -> None:
    """オプション省略時も既存 -hevc 画質をHEVCとして保持する。"""

    display_channel_id = 'gr-konomitv-bs4k-hevc-default'
    stream = LiveStream(display_channel_id, '1080p-hevc')

    assert stream.encoding_options.video_codec == 'hevc'
    assert stream.encoding_options.audio_codec == 'aac'
    assert stream.live_stream_id == f'{display_channel_id}-1080p-hevc'


def test_konomitv_bs4k_capabilities_separate_live_recorded_and_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge不在時も録画能力を失わず、Opusを音声行として独立表示する。"""

    async def GetRecordedCapabilities(
        _cls: type[RecordedPlaybackCapabilityProbe],
    ) -> list[RecordedPlaybackCapability]:
        return [
            RecordedPlaybackCapability('FFmpeg', 'avc', 8, False, 'High', 'EncodeFailed'),
            RecordedPlaybackCapability('FFmpeg', 'av1', 10, True, 'Main', None),
        ]

    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getCapabilities',
        classmethod(GetRecordedCapabilities),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isBridgeAvailable',
        classmethod(lambda _cls: False),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isFFmpegAvailable',
        classmethod(lambda _cls: True),
    )

    capabilities = asyncio.run(KonomiTVBS4KPlaybackCapabilityProbe.getCapabilities())
    avc, av1 = capabilities.video
    assert avc.live_available is False
    assert avc.recorded_available is False
    assert avc.live_reason_code == 'EncodeFailed'
    assert av1.live_available is False
    assert av1.recorded_available is True
    assert av1.live_reason_code == 'BridgeUnavailable'
    assert av1.recorded_reason_code is None
    assert capabilities.audio[0] == KonomiTVBS4KPlaybackAudioCapability(
        codec = 'aac',
        live_available = True,
        recorded_available = True,
        live_reason_code = None,
        recorded_reason_code = None,
    )
    assert capabilities.audio[1].codec == 'opus'
    assert capabilities.audio[1].live_available is False
    assert capabilities.audio[1].recorded_available is True
    assert capabilities.audio[1].live_reason_code == 'BridgeUnavailable'


def test_konomitv_bs4k_advanced_live_codec_skips_probe_when_bridge_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge 不在では高度ライブ codec を公開せず、実 pipe probe も起動しない。"""

    settings = ServerSettings.model_validate({}, context={'bypass_validation': True})
    assert settings.general.konomitv_bs4k_acceptance_diagnostics_enabled is False

    async def GetRecordedCapabilities(
        _cls: type[RecordedPlaybackCapabilityProbe],
    ) -> list[RecordedPlaybackCapability]:
        return [
            RecordedPlaybackCapability('FFmpeg', 'av1', 10, True, 'Main', None),
        ]

    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getCapabilities',
        classmethod(GetRecordedCapabilities),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isBridgeAvailable',
        classmethod(lambda _cls: False),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isFFmpegAvailable',
        classmethod(lambda _cls: True),
    )
    live_probe_calls = 0
    radio_opus_probe_calls = 0
    subprocess_calls = 0

    async def IsLiveTransportAvailable(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        _encoder: str,
        _codec: str,
        _bit_depth: int,
        _audio_codec: str,
    ) -> bool:
        nonlocal live_probe_calls

        live_probe_calls += 1
        return True

    async def IsRadioOpusTransportAvailable(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
    ) -> bool:
        nonlocal radio_opus_probe_calls

        radio_opus_probe_calls += 1
        return True

    async def CreateSubprocessExec(*_args: object, **_kwargs: object) -> None:
        nonlocal subprocess_calls

        subprocess_calls += 1
        raise AssertionError('Bridge 不在では外部 process を起動してはならない')

    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isLiveTransportAvailable',
        classmethod(IsLiveTransportAvailable),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isRadioOpusTransportAvailable',
        classmethod(IsRadioOpusTransportAvailable),
    )
    monkeypatch.setattr(
        playback_capabilities_module.asyncio,
        'create_subprocess_exec',
        CreateSubprocessExec,
    )

    capabilities = asyncio.run(KonomiTVBS4KPlaybackCapabilityProbe.getCapabilities())

    assert live_probe_calls == 0
    assert radio_opus_probe_calls == 0
    assert subprocess_calls == 0
    assert capabilities.video[0].live_available is False
    assert capabilities.video[0].recorded_available is True
    assert capabilities.video[0].live_reason_code == 'BridgeUnavailable'
    assert capabilities.audio[1].live_available is False
    assert capabilities.audio[1].recorded_available is True
    assert capabilities.audio[1].live_reason_code == 'BridgeUnavailable'
    assert {
        (combination.audio_codec, combination.available, combination.reason_code)
        for combination in capabilities.live_combinations
    } == {
        ('aac', False, 'BridgeUnavailable'),
        ('opus', False, 'BridgeUnavailable'),
    }


def test_konomitv_bs4k_advanced_live_codec_available_when_bridge_and_probe_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge と実 probe が成功すれば AV1 / Opus を能力 API で公開する。"""

    async def GetRecordedCapabilities(
        _cls: type[RecordedPlaybackCapabilityProbe],
    ) -> list[RecordedPlaybackCapability]:
        return [
            RecordedPlaybackCapability('FFmpeg', 'av1', 10, True, 'Main', None),
        ]

    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getCapabilities',
        classmethod(GetRecordedCapabilities),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isBridgeAvailable',
        classmethod(lambda _cls: True),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isFFmpegAvailable',
        classmethod(lambda _cls: True),
    )

    async def IsLiveTransportAvailable(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        _encoder: str,
        _codec: str,
        _bit_depth: int,
        _audio_codec: str,
    ) -> bool:
        return True

    async def IsRadioOpusTransportAvailable(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
    ) -> bool:
        return True

    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isLiveTransportAvailable',
        classmethod(IsLiveTransportAvailable),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isRadioOpusTransportAvailable',
        classmethod(IsRadioOpusTransportAvailable),
    )

    capabilities = asyncio.run(KonomiTVBS4KPlaybackCapabilityProbe.getCapabilities())

    assert capabilities.video[0].live_available is True
    assert capabilities.video[0].live_reason_code is None
    assert capabilities.audio[1].live_available is True
    assert capabilities.audio[1].live_reason_code is None
    assert {
        (combination.audio_codec, combination.available, combination.reason_code)
        for combination in capabilities.live_combinations
    } == {
        ('aac', True, None),
        ('opus', True, None),
    }


def test_konomitv_bs4k_live_combination_matrix_probes_each_backend_advanced_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HW高度構成をSW結果で代用せず、各backendを含む厳密な組み合わせで実probeする。"""

    async def GetRecordedCapabilities(
        _cls: type[RecordedPlaybackCapabilityProbe],
    ) -> list[RecordedPlaybackCapability]:
        return [
            RecordedPlaybackCapability('FFmpeg', 'avc', 8, True, 'High', None),
            RecordedPlaybackCapability('FFmpeg', 'av1', 10, True, 'Main', None),
            RecordedPlaybackCapability('QSV', 'avc', 8, True, 'High', None),
            RecordedPlaybackCapability('QSV', 'vp9', 10, True, 'Profile 2', None),
            RecordedPlaybackCapability('NVENC', 'av1', 10, True, 'Main', None),
            RecordedPlaybackCapability('AMF', 'hevc', 8, True, 'Main', None),
        ]

    probed_combinations: list[tuple[str, str, int, str]] = []

    async def IsLiveTransportAvailable(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        encoder: str,
        codec: str,
        bit_depth: int,
        audio_codec: str,
    ) -> bool:
        probed_combinations.append((encoder, codec, bit_depth, audio_codec))
        return True

    async def IsRadioOpusTransportAvailable(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
    ) -> bool:
        """ラジオ固有の audio-only probe を成功させる。"""

        return True

    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getCapabilities',
        classmethod(GetRecordedCapabilities),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isBridgeAvailable',
        classmethod(lambda _cls: True),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isFFmpegAvailable',
        classmethod(lambda _cls: True),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isLiveTransportAvailable',
        classmethod(IsLiveTransportAvailable),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isRadioOpusTransportAvailable',
        classmethod(IsRadioOpusTransportAvailable),
    )

    capabilities = asyncio.run(KonomiTVBS4KPlaybackCapabilityProbe.getCapabilities())
    combinations = {
        (
            combination.encoder,
            combination.video_codec,
            combination.video_bit_depth,
            combination.audio_codec,
        ): combination
        for combination in capabilities.live_combinations
    }

    assert probed_combinations == [
        ('FFmpeg', 'avc', 8, 'opus'),
        ('FFmpeg', 'av1', 10, 'aac'),
        ('FFmpeg', 'av1', 10, 'opus'),
        ('QSV', 'avc', 8, 'opus'),
        ('QSV', 'vp9', 10, 'aac'),
        ('QSV', 'vp9', 10, 'opus'),
        ('NVENC', 'av1', 10, 'aac'),
        ('NVENC', 'av1', 10, 'opus'),
        ('AMF', 'hevc', 8, 'opus'),
    ]
    assert combinations[('FFmpeg', 'avc', 8, 'aac')].available is True
    assert combinations[('FFmpeg', 'av1', 10, 'opus')].available is True
    assert combinations[('QSV', 'avc', 8, 'aac')].available is True
    for combination_key in (
        ('QSV', 'avc', 8, 'opus'),
        ('QSV', 'vp9', 10, 'aac'),
        ('QSV', 'vp9', 10, 'opus'),
        ('NVENC', 'av1', 10, 'aac'),
        ('NVENC', 'av1', 10, 'opus'),
        ('AMF', 'hevc', 8, 'opus'),
    ):
        assert combinations[combination_key].available is True
        assert combinations[combination_key].reason_code is None


def test_konomitv_bs4k_exact_capability_getters_do_not_probe_full_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ライブ開始時の1組検査が全backend・全codecの行列probeへ拡大しない。"""

    recorded_calls: list[tuple[str, str, int]] = []
    live_calls: list[tuple[str, str, int, str]] = []
    radio_calls = 0

    async def GetCapability(
        _cls: type[RecordedPlaybackCapabilityProbe],
        encoder: str,
        codec: str,
        bit_depth: int,
    ) -> RecordedPlaybackCapability:
        """要求された録画能力キーだけを返す。"""

        recorded_calls.append((encoder, codec, bit_depth))
        return RecordedPlaybackCapability(
            'QSV',
            'av1',
            10,
            True,
            'Main',
            None,
        )

    async def UnexpectedGetCapabilities(
        _cls: type[RecordedPlaybackCapabilityProbe],
    ) -> list[RecordedPlaybackCapability]:
        """全行列probeへの退行を即座に失敗させる。"""

        raise AssertionError('exact getter must not request the full capability matrix')

    async def IsLiveTransportAvailable(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        encoder: str,
        codec: str,
        bit_depth: int,
        audio_codec: str,
    ) -> bool:
        """exact live probeキーを記録する。"""

        live_calls.append((encoder, codec, bit_depth, audio_codec))
        return True

    async def IsRadioOpusTransportAvailable(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
    ) -> bool:
        """audio-only probeの呼び出しを記録する。"""

        nonlocal radio_calls

        radio_calls += 1
        return True

    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getCapability',
        classmethod(GetCapability),
    )
    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getCapabilities',
        classmethod(UnexpectedGetCapabilities),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isFFmpegAvailable',
        classmethod(lambda _cls: True),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isBridgeAvailable',
        classmethod(lambda _cls: True),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isLiveTransportAvailable',
        classmethod(IsLiveTransportAvailable),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isRadioOpusTransportAvailable',
        classmethod(IsRadioOpusTransportAvailable),
    )

    async def Verify() -> None:
        video = await KonomiTVBS4KPlaybackCapabilityProbe.getVideoCapability(
            'QSV',
            'av1',
            10,
        )
        combination = (
            await KonomiTVBS4KPlaybackCapabilityProbe.getLiveCombinationCapability(
                'QSV',
                'av1',
                10,
                'opus',
            )
        )
        audio = await KonomiTVBS4KPlaybackCapabilityProbe.getAudioCapability('opus')
        assert video is not None and video.live_available is True
        assert combination is not None and combination.available is True
        assert audio is not None and audio.live_available is True

    asyncio.run(Verify())

    assert recorded_calls == [
        ('QSV', 'av1', 10),
        ('QSV', 'av1', 10),
    ]
    assert live_calls == [
        ('QSV', 'av1', 10, 'aac'),
        ('QSV', 'av1', 10, 'opus'),
    ]
    assert radio_calls == 1


def test_konomitv_bs4k_exact_baseline_video_capability_requires_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """単一AVC行もmain liveのAnchor Bridge不在を利用可として公開しない。"""

    async def GetCapability(
        _cls: type[RecordedPlaybackCapabilityProbe],
        encoder: str,
        codec: str,
        bit_depth: int,
    ) -> RecordedPlaybackCapability:
        assert (encoder, codec, bit_depth) == ('FFmpeg', 'avc', 8)
        return RecordedPlaybackCapability('FFmpeg', 'avc', 8, True, 'High', None)

    async def IsBridgeAvailableAsync(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
    ) -> bool:
        return False

    async def UnexpectedLiveTransportProbe(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        _encoder: str,
        _codec: str,
        _bit_depth: int,
        _audio_codec: str,
    ) -> bool:
        raise AssertionError('baseline AVC/AAC must not run the advanced transport probe')

    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getCapability',
        classmethod(GetCapability),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isFFmpegAvailable',
        classmethod(lambda _cls: True),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isBridgeAvailableAsync',
        classmethod(IsBridgeAvailableAsync),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isLiveTransportAvailable',
        classmethod(UnexpectedLiveTransportProbe),
    )

    capability = asyncio.run(
        KonomiTVBS4KPlaybackCapabilityProbe.getVideoCapability(
            'FFmpeg',
            'avc',
            8,
        )
    )

    assert capability is not None
    assert capability.recorded_available is True
    assert capability.live_available is False
    assert capability.live_reason_code == 'BridgeUnavailable'


def test_konomitv_bs4k_bridge_runtime_verification_runs_outside_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SHA計算と版確認を能力APIからasyncio.to_threadへ委譲する。"""

    verifier_calls = 0
    delegated_functions: list[str] = []

    def IsBridgeAvailable(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
    ) -> bool:
        nonlocal verifier_calls

        verifier_calls += 1
        return True

    async def ToThread(function: Callable[[], bool]) -> bool:
        """host executorを起動せず、委譲された関数名と結果だけを検証する。"""

        delegated_functions.append(function.__name__)
        return function()

    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isBridgeAvailable',
        classmethod(IsBridgeAvailable),
    )
    monkeypatch.setattr(
        playback_capabilities_module.asyncio,
        'to_thread',
        ToThread,
    )

    assert asyncio.run(
        KonomiTVBS4KPlaybackCapabilityProbe.isBridgeAvailableAsync()
    ) is True
    assert verifier_calls == 1
    assert delegated_functions == ['IsBridgeAvailable']


def test_konomitv_bs4k_opus_audio_capability_uses_radio_audio_only_probe_as_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """映像付き Opus 行が成功しても、audio-only probe 失敗時は集約音声能力を公開しない。"""

    async def GetRecordedCapabilities(
        _cls: type[RecordedPlaybackCapabilityProbe],
    ) -> list[RecordedPlaybackCapability]:
        """利用可能な FFmpeg AVC 能力を返す。"""

        return [RecordedPlaybackCapability('FFmpeg', 'avc', 8, True, 'High', None)]

    async def IsLiveTransportAvailable(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        _encoder: str,
        _codec: str,
        _bit_depth: int,
        _audio_codec: str,
    ) -> bool:
        """映像付きの exact combination probe を成功させる。"""

        return True

    async def IsRadioOpusTransportAvailable(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
    ) -> bool:
        """ラジオ固有の audio-only probe を失敗させる。"""

        return False

    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getCapabilities',
        classmethod(GetRecordedCapabilities),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isBridgeAvailable',
        classmethod(lambda _cls: True),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isFFmpegAvailable',
        classmethod(lambda _cls: True),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isLiveTransportAvailable',
        classmethod(IsLiveTransportAvailable),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isRadioOpusTransportAvailable',
        classmethod(IsRadioOpusTransportAvailable),
    )

    capabilities = asyncio.run(KonomiTVBS4KPlaybackCapabilityProbe.getCapabilities())
    opus_combination = next(
        combination
        for combination in capabilities.live_combinations
        if combination.audio_codec == 'opus'
    )
    opus_audio = next(
        capability for capability in capabilities.audio if capability.codec == 'opus'
    )

    assert opus_combination.available is True
    assert opus_audio.live_available is False
    assert opus_audio.live_reason_code == 'ProbeFailed'


def test_konomitv_bs4k_radio_query_uses_audio_capability_without_video_combination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ラジオqueryを解決し、映像付き exact combination を参照せず音声能力だけを使う。"""

    monkeypatch.setattr(
        LiveStreamsRouter, 'GetEncoderForLiveChannel', lambda _display_channel_id: 'QSV'
    )
    monkeypatch.setattr(
        LiveStreamsRouter.Channel,
        'filter',
        lambda **_kwargs: _LiveChannelQuery(is_radiochannel = True),
    )

    async def GetAudioCapability(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        _codec: str,
    ) -> KonomiTVBS4KPlaybackAudioCapability:
        return KonomiTVBS4KPlaybackAudioCapability(
            codec = 'opus',
            live_available = True,
            recorded_available = True,
            live_reason_code = None,
            recorded_reason_code = None,
        )

    async def UnexpectedVideoCapability(*_args: object, **_kwargs: object) -> None:
        raise AssertionError('radio validation must not inspect video capability')

    async def UnexpectedLiveCombination(*_args: object, **_kwargs: object) -> None:
        raise AssertionError('radio validation must not inspect a video combination')

    requested_combinations: list[tuple[str, str, int, str]] = []

    async def GetLiveCombinationCapability(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        encoder: str,
        codec: str,
        bit_depth: int,
        audio_codec: str,
    ) -> KonomiTVBS4KPlaybackLiveCombinationCapability:
        requested_combinations.append((encoder, codec, bit_depth, audio_codec))
        return KonomiTVBS4KPlaybackLiveCombinationCapability(
            encoder = 'FFmpeg',
            video_codec = 'av1',
            video_bit_depth = 10,
            audio_codec = 'opus',
            available = True,
            reason_code = None,
        )

    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getVideoCapability',
        classmethod(UnexpectedVideoCapability),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getAudioCapability',
        classmethod(GetAudioCapability),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getLiveCombinationCapability',
        classmethod(UnexpectedLiveCombination),
    )

    stream_quality = asyncio.run(
        LiveStreamsRouter.ValidateQuality(
            '1080p',
            'gr011',
            'av1',
            KonomiTVBS4KVideoBitDepthQuery.BIT_10,
            'opus',
        )
    )
    equivalent_stream_quality = asyncio.run(
        LiveStreamsRouter.ValidateQuality(
            '1080p-hevc-10bit-24fps',
            'gr011',
            'vp9',
            KonomiTVBS4KVideoBitDepthQuery.BIT_10,
            'opus',
        )
    )
    assert stream_quality.quality == '1080p'
    assert stream_quality.encoding_options.video_codec == 'avc'
    assert stream_quality.encoding_options.video_bit_depth == 8
    assert stream_quality.encoding_options.audio_codec == 'opus'
    assert stream_quality.encoding_options.is_24fps_mode_enabled is False
    assert equivalent_stream_quality == stream_quality
    assert requested_combinations == []


def test_konomitv_bs4k_radio_query_rejects_unavailable_audio_only_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ラジオOpusの audio-only probe 失敗を映像能力へフォールバックせず422で拒否する。"""

    monkeypatch.setattr(
        LiveStreamsRouter, 'GetEncoderForLiveChannel', lambda _display_channel_id: 'QSV'
    )
    monkeypatch.setattr(
        LiveStreamsRouter.Channel,
        'filter',
        lambda **_kwargs: _LiveChannelQuery(is_radiochannel = True),
    )

    async def GetAudioCapability(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        _codec: str,
    ) -> KonomiTVBS4KPlaybackAudioCapability:
        """audio-only probe 失敗を表す音声能力を返す。"""

        return KonomiTVBS4KPlaybackAudioCapability(
            codec = 'opus',
            live_available = False,
            recorded_available = True,
            live_reason_code = 'ProbeFailed',
            recorded_reason_code = None,
        )

    async def UnexpectedVideoCapability(*_args: object, **_kwargs: object) -> None:
        """ラジオ経路が映像能力を参照した場合にテストを失敗させる。"""

        raise AssertionError('ラジオ経路は映像能力を参照してはならない')

    async def UnexpectedLiveCombination(*_args: object, **_kwargs: object) -> None:
        """ラジオ経路が映像付き組み合わせを参照した場合にテストを失敗させる。"""

        raise AssertionError('ラジオ経路は映像付き組み合わせを参照してはならない')

    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getAudioCapability',
        classmethod(GetAudioCapability),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getVideoCapability',
        classmethod(UnexpectedVideoCapability),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getLiveCombinationCapability',
        classmethod(UnexpectedLiveCombination),
    )

    with pytest.raises(HTTPException) as unavailable_error:
        asyncio.run(
            LiveStreamsRouter.ValidateQuality(
                '1080p',
                'gr011',
                'av1',
                KonomiTVBS4KVideoBitDepthQuery.BIT_10,
                'opus',
            )
        )

    assert unavailable_error.value.status_code == 422
    assert unavailable_error.value.detail == {
        'code': 'ProbeFailed',
        'message': 'The requested live audio encoding is unavailable.',
    }


def test_konomitv_bs4k_live_query_rejects_unavailable_exact_codec_combination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """個別能力が有効でも、HW高度構成はSW probeへ逃がさず422で拒否する。"""

    monkeypatch.setattr(
        LiveStreamsRouter, 'GetEncoderForLiveChannel', lambda _display_channel_id: 'QSV'
    )
    monkeypatch.setattr(
        LiveStreamsRouter.Channel,
        'filter',
        lambda **_kwargs: _LiveChannelQuery(is_radiochannel = False),
    )

    async def UnexpectedIndividualCapability(*_args: object, **_kwargs: object) -> None:
        raise AssertionError('advanced live validation must use only the exact combination')

    requested_combinations: list[tuple[str, str, int, str]] = []

    async def GetLiveCombinationCapability(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        encoder: str,
        codec: str,
        bit_depth: int,
        audio_codec: str,
    ) -> KonomiTVBS4KPlaybackLiveCombinationCapability:
        requested_combinations.append((encoder, codec, bit_depth, audio_codec))
        return KonomiTVBS4KPlaybackLiveCombinationCapability(
            encoder = 'QSV',
            video_codec = 'av1',
            video_bit_depth = 10,
            audio_codec = 'opus',
            available = False,
            reason_code = 'UnsupportedCombination',
        )

    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getVideoCapability',
        classmethod(UnexpectedIndividualCapability),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getAudioCapability',
        classmethod(UnexpectedIndividualCapability),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getLiveCombinationCapability',
        classmethod(GetLiveCombinationCapability),
    )

    with pytest.raises(HTTPException) as unavailable_error:
        asyncio.run(
            LiveStreamsRouter.ValidateQuality(
                '1080p',
                'gr011',
                'av1',
                KonomiTVBS4KVideoBitDepthQuery.BIT_10,
                'opus',
            )
        )

    assert unavailable_error.value.status_code == 422
    assert requested_combinations == [('QSV', 'av1', 10, 'opus')]
    assert unavailable_error.value.detail == {
        'code': 'UnsupportedCombination',
        'message': 'The requested live video and audio encoding combination is unavailable.',
    }


def test_konomitv_bs4k_live_query_rejects_invalid_or_unavailable_combinations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仕様外bit depthとlive能力不足をどちらも422で拒否する。"""

    monkeypatch.setattr(
        LiveStreamsRouter,
        'GetEncoderForLiveChannel',
        lambda _display_channel_id: 'FFmpeg',
    )
    monkeypatch.setattr(
        LiveStreamsRouter.Channel,
        'filter',
        lambda **_kwargs: _LiveChannelQuery(is_radiochannel = False),
    )

    with pytest.raises(HTTPException) as invalid_error:
        asyncio.run(
            LiveStreamsRouter.ValidateQuality(
                '1080p',
                'gr011',
                'avc',
                KonomiTVBS4KVideoBitDepthQuery.BIT_10,
                'aac',
            )
        )
    assert invalid_error.value.status_code == 422

    async def GetUnavailableLiveCombinationCapability(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        _encoder: str,
        _codec: str,
        _bit_depth: int,
        _audio_codec: str,
    ) -> KonomiTVBS4KPlaybackLiveCombinationCapability:
        return KonomiTVBS4KPlaybackLiveCombinationCapability(
            encoder = 'FFmpeg',
            video_codec = 'av1',
            video_bit_depth = 8,
            audio_codec = 'aac',
            available = False,
            reason_code = 'BridgeUnavailable',
        )

    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getLiveCombinationCapability',
        classmethod(GetUnavailableLiveCombinationCapability),
    )
    with pytest.raises(HTTPException) as unavailable_error:
        asyncio.run(
            LiveStreamsRouter.ValidateQuality(
                '1080p',
                'gr011',
                'av1',
                KonomiTVBS4KVideoBitDepthQuery.BIT_8,
                'aac',
            )
        )
    assert unavailable_error.value.status_code == 422
    assert unavailable_error.value.detail['code'] == 'BridgeUnavailable'


def test_konomitv_bs4k_live_explicit_avc_aac_query_probes_exact_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AVC/AACの明示queryもStream Anchorを含むexact能力でfail closedにする。"""

    monkeypatch.setattr(
        LiveStreamsRouter,
        'GetEncoderForLiveChannel',
        lambda _display_channel_id: 'QSV',
    )
    monkeypatch.setattr(
        LiveStreamsRouter.Channel,
        'filter',
        lambda **_kwargs: _LiveChannelQuery(is_radiochannel = False),
    )

    requested_combinations: list[tuple[str, str, int, str]] = []

    async def GetLiveCombinationCapability(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        encoder: str,
        codec: str,
        bit_depth: int,
        audio_codec: str,
    ) -> KonomiTVBS4KPlaybackLiveCombinationCapability:
        requested_combinations.append((encoder, codec, bit_depth, audio_codec))
        return KonomiTVBS4KPlaybackLiveCombinationCapability(
            encoder = 'QSV',
            video_codec = 'avc',
            video_bit_depth = 8,
            audio_codec = 'aac',
            available = True,
            reason_code = None,
        )

    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getVideoCapability',
        classmethod(lambda *_args, **_kwargs: None),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getAudioCapability',
        classmethod(lambda *_args, **_kwargs: None),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getLiveCombinationCapability',
        classmethod(GetLiveCombinationCapability),
    )

    stream_quality = asyncio.run(
        LiveStreamsRouter.ValidateQuality(
            '1080p',
            'gr011',
            'avc',
            KonomiTVBS4KVideoBitDepthQuery.BIT_8,
            'aac',
        )
    )
    assert stream_quality.encoding_options.video_codec == 'avc'
    assert stream_quality.encoding_options.video_bit_depth == 8
    assert stream_quality.encoding_options.audio_codec == 'aac'
    assert requested_combinations == [('QSV', 'avc', 8, 'aac')]


def test_konomitv_bs4k_capability_api_exposes_video_and_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KonomiTV-BS4K固有APIの応答に両再生経路と音声能力を含める。"""

    async def GetCapabilities(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
    ) -> KonomiTVBS4KPlaybackCapabilities:
        return KonomiTVBS4KPlaybackCapabilities(
            video = (
                KonomiTVBS4KPlaybackVideoCapability(
                    encoder = 'FFmpeg',
                    codec = 'avc',
                    bit_depth = 8,
                    profile = 'High',
                    live_available = True,
                    recorded_available = False,
                    live_reason_code = None,
                    recorded_reason_code = 'EncodeFailed',
                ),
            ),
            audio = (
                KonomiTVBS4KPlaybackAudioCapability(
                    codec = 'aac',
                    live_available = True,
                    recorded_available = True,
                    live_reason_code = None,
                    recorded_reason_code = None,
                ),
            ),
            live_combinations = (
                KonomiTVBS4KPlaybackLiveCombinationCapability(
                    encoder = 'FFmpeg',
                    video_codec = 'avc',
                    video_bit_depth = 8,
                    audio_codec = 'aac',
                    available = True,
                    reason_code = None,
                ),
            ),
        )

    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getCapabilities',
        classmethod(GetCapabilities),
    )
    monkeypatch.setattr(
        VideoStreamsRouter.TSCodecBridgeRuntimeVerifier,
        'getProcessStartSnapshot',
        classmethod(lambda _cls: ('full-api-generation', 41, 17, 12, 12)),
    )
    monkeypatch.setattr(
        VideoStreamsRouter,
        'Config',
        lambda: SimpleNamespace(
            general=SimpleNamespace(
                konomitv_bs4k_acceptance_diagnostics_enabled=True,
            ),
        ),
    )
    api_response = Response()
    response = asyncio.run(
        VideoStreamsRouter.KonomiTVBS4KPlaybackCapabilitiesAPI(api_response)
    )
    assert (
        api_response.headers['X-KonomiTV-BS4K-TSCodecBridge-Process-Generation']
        == 'full-api-generation'
    )
    assert (
        api_response.headers['X-KonomiTV-BS4K-TSCodecBridge-Process-Start-Count']
        == '41'
    )
    assert (
        api_response.headers['X-KonomiTV-BS4K-TSCodecBridge-Live-Process-Start-Count']
        == '17'
    )
    assert (
        api_response.headers['X-KonomiTV-BS4K-TSCodecBridge-Probe-Process-Start-Count']
        == '12'
    )
    assert (
        api_response.headers[
            'X-KonomiTV-BS4K-TSCodecBridge-Verification-Process-Start-Count'
        ]
        == '12'
    )
    assert response.model_dump() == {
        'video': [
            {
                'encoder': 'FFmpeg',
                'codec': 'avc',
                'bit_depth': 8,
                'profile': 'High',
                'live_available': True,
                'recorded_available': False,
                'live_reason_code': None,
                'recorded_reason_code': 'EncodeFailed',
            }
        ],
        'audio': [
            {
                'codec': 'aac',
                'live_available': True,
                'recorded_available': True,
                'live_reason_code': None,
                'recorded_reason_code': None,
            }
        ],
        'live_combinations': [
            {
                'encoder': 'FFmpeg',
                'video_codec': 'avc',
                'video_bit_depth': 8,
                'audio_codec': 'aac',
                'available': True,
                'reason_code': None,
            }
        ],
    }


def test_konomitv_bs4k_bridge_process_counters_are_hidden_outside_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受入診断を明示しない通常運用ではBridge起動telemetryを公開しない。"""

    monkeypatch.setattr(
        VideoStreamsRouter,
        'Config',
        lambda: SimpleNamespace(
            general=SimpleNamespace(
                konomitv_bs4k_acceptance_diagnostics_enabled=False,
            ),
        ),
    )
    monkeypatch.setattr(
        VideoStreamsRouter.TSCodecBridgeRuntimeVerifier,
        'getProcessStartSnapshot',
        classmethod(
            lambda _cls: (_ for _ in ()).throw(
                AssertionError('disabled diagnostics must not read process counters')
            )
        ),
    )
    response = Response()
    VideoStreamsRouter.SetTSCodecBridgeProcessCounterHeaders(response)
    assert all(
        header.startswith('x-konomitv-bs4k-tscodecbridge') is False
        for header in response.headers
    )


def test_konomitv_bs4k_capability_api_uses_namespaced_path() -> None:
    """upstreamにない能力APIをKonomiTV-BS4K固有のパスでのみ公開する。"""

    route_paths = {route.path for route in VideoStreamsRouter.router.routes}
    assert '/api/streams/video/konomitv-bs4k-playback-capabilities' in route_paths
    assert '/api/streams/video/capabilities' not in route_paths


@pytest.mark.parametrize(
    ('video_codec', 'bit_depth', 'mapping_case', 'opus_pid_count', 'expected_available'),
    [
        ('vp9', 8, 'valid', 2, True),
        ('vp9', 8, 'missing', 2, False),
        ('av1', 10, 'valid', 2, True),
        ('av1', 8, 'invalid-marker-version', 2, False),
        ('av1', 10, 'bit-depth-mismatch', 2, False),
    ],
)
def test_konomitv_bs4k_live_transport_probe_validates_mapping_and_all_audio_tracks(
    monkeypatch: pytest.MonkeyPatch,
    video_codec: KonomiTVBS4KVideoCodec,
    bit_depth: KonomiTVBS4KVideoBitDepth,
    mapping_case: str,
    opus_pid_count: int,
    expected_available: bool,
) -> None:
    """実 pipe probe は両 process を drain し、厳密 mapping と二音声が揃った場合だけ成功する。"""

    output = (
        _wrap_psi_section(0x0000, _build_live_probe_pat()) +
        _wrap_psi_section(
            0x1000,
            _build_live_probe_pmt(
                _build_live_probe_video_descriptors(
                    video_codec,
                    bit_depth,
                    mapping_case,
                ),
                opus_pid_count,
            ),
        ) +
        _build_live_probe_transport_packets(
            opus_pid_count,
            include_video = True,
        )
    )
    communicate_count = 0
    both_processes_communicating = asyncio.Event()

    class _ProbeProcess:
        """probe の subprocess と communicate 待機関係を再現する。"""

        def __init__(self, stdout: bytes) -> None:
            """標準出力と正常終了状態を保持する。"""

            self.stdout = stdout
            self.returncode: int | None = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            """両 process の communicate() が開始するまで戻らない。"""

            nonlocal communicate_count

            communicate_count += 1
            if communicate_count == 2:
                both_processes_communicating.set()
            await both_processes_communicating.wait()
            return self.stdout, b''

        def kill(self) -> None:
            """異常終了時の強制停止を再現する。"""

            self.returncode = -9

        async def wait(self) -> int:
            """強制停止後の終了待機を再現する。"""

            assert self.returncode is not None
            return self.returncode

    bridge_process = _ProbeProcess(output)
    ffmpeg_process = _ProbeProcess(b'')
    processes = [bridge_process, ffmpeg_process]
    subprocess_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    bridge_start_records: list[None] = []

    async def create_subprocess_exec(*args: str, **kwargs: object) -> _ProbeProcess:
        """Bridge、FFmpeg の順に process 代替を返す。"""

        subprocess_calls.append((args, kwargs))
        return processes.pop(0)

    monkeypatch.setitem(playback_capabilities_module.LIBRARY_PATH, 'FFmpeg8', '/runtime/ffmpeg8.elf')
    monkeypatch.setitem(
        playback_capabilities_module.LIBRARY_PATH,
        KonomiTVBS4KPlaybackCapabilityProbe.BRIDGE_LIBRARY_PATH_KEY,
        '/runtime/ts-codec-bridge.elf',
    )
    monkeypatch.setattr(
        playback_capabilities_module.asyncio,
        'create_subprocess_exec',
        create_subprocess_exec,
    )
    monkeypatch.setattr(
        playback_capabilities_module.TSCodecBridgeRuntimeVerifier,
        'recordProcessStart',
        classmethod(lambda _cls, kind: bridge_start_records.append(kind)),
    )
    monkeypatch.setattr(
        playback_capabilities_module.Path, 'is_file', lambda _path: True
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe, '_live_probe_signature', None
    )
    monkeypatch.setattr(KonomiTVBS4KPlaybackCapabilityProbe, '_live_probe_results', {})
    monkeypatch.setattr(KonomiTVBS4KPlaybackCapabilityProbe, '_live_probe_lock', asyncio.Lock())

    async def run_probe() -> bool:
        """逐次 communicate() へ退行した場合は短時間で失敗させる。"""

        return await asyncio.wait_for(
            KonomiTVBS4KPlaybackCapabilityProbe.isLiveTransportAvailable(
                'FFmpeg',
                video_codec,
                bit_depth,
                'opus',
            ),
            timeout=1,
        )

    assert asyncio.run(run_probe()) is expected_available
    assert len(subprocess_calls) == 2
    assert bridge_start_records == ['probe']
    bridge_command, bridge_options = subprocess_calls[0]
    ffmpeg_command, ffmpeg_options = subprocess_calls[1]
    expected_bridge_command = [
        '/runtime/ts-codec-bridge.elf',
        '--video-codec',
        video_codec,
        '--audio-codec',
        'opus',
    ]
    if video_codec == 'av1':
        expected_bridge_command += ['--transport-rate-kbps', '2200']
    assert bridge_command == tuple(expected_bridge_command)
    assert isinstance(bridge_options['stdin'], int)
    assert ffmpeg_options['stdout'] != asyncio.subprocess.PIPE
    assert [
        ffmpeg_command[index + 1]
        for index, option in enumerate(ffmpeg_command)
        if option == '-map'
    ] == ['0:v:0', '1:a:0', '2:a:0']
    assert ffmpeg_command[ffmpeg_command.index('-muxrate') + 1] == '2200K'
    assert ffmpeg_command[ffmpeg_command.index('-pcr_period') + 1] == '20'


def test_konomitv_bs4k_live_probe_rejects_malformed_aligned_psi_without_exception() -> None:
    """188-byte整列済みでも壊れたPAT/PMTは能力APIの例外へ漏らさず空集合に閉じる。"""

    malformed_pat = b'\x47\x40\x00\x30' + (b'\xFF' * 184)
    collect_streams = getattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        '_KonomiTVBS4KPlaybackCapabilityProbe__collectLiveProbePMTStreams',
    )

    assert collect_streams(malformed_pat) == {}


@pytest.mark.parametrize(
    ('corruption', 'expected_valid'),
    [
        ('valid', True),
        ('psi-only', False),
        ('tei', False),
        ('cc-gap', False),
        ('truncated', False),
    ],
)
def test_konomitv_bs4k_live_probe_requires_real_clean_transport(
    corruption: str,
    expected_valid: bool,
) -> None:
    """PMT宣言だけを信用せず、PCR・PES・PTS・RAI・CCとpacket境界を実測する。"""

    psi = (
        _wrap_psi_section(0x0000, _build_live_probe_pat())
        + _wrap_psi_section(
            0x1000,
            _build_live_probe_pmt(
                _build_live_probe_video_descriptors('av1', 8, 'valid'),
                2,
            ),
        )
    )
    # AV1 MPEG-2 TS draft は video PES を private_stream_1 (0xBD) で運ぶ。
    output = psi + _build_live_probe_transport_packets(
        2,
        include_video = True,
        video_stream_id = 0xBD,
    )
    if corruption == 'psi-only':
        output = psi
    elif corruption == 'tei':
        damaged = bytearray(output)
        damaged[1] |= 0x80
        output = bytes(damaged)
    elif corruption == 'scrambled':
        damaged = bytearray(output)
        damaged[3] |= 0xC0
        output = bytes(damaged)
    elif corruption == 'afc-zero':
        damaged = bytearray(output)
        damaged[(188 * 2) + 3] &= 0xCF
        output = bytes(damaged)
    elif corruption == 'cc-gap':
        output += _wrap_probe_pes_packet(
            0x0100,
            0xBD,
            continuity_counter = 2,
            pcr = True,
            random_access = True,
        )
    elif corruption == 'truncated':
        output = output[:-1]

    validate_transport = getattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        '_KonomiTVBS4KPlaybackCapabilityProbe__validateLiveProbeProgramTransport',
    )
    assert (
        validate_transport(
            output,
            0x0100,
            {0x0100, 0x0110, 0x0111},
            0x0100,
        )
        is expected_valid
    )




@pytest.mark.parametrize(
    (
        'probe_case',
        'opus_pid_count',
        'include_video',
        'opus_descriptor_case',
        'bridge_returncode',
        'expected_available',
    ),
    [
        ('valid', 2, False, 'valid', 0, True),
        ('bridge-failed', 2, False, 'valid', 1, False),
        ('opus-pid-missing', 1, False, 'valid', 0, False),
        ('video-mixed', 2, True, 'valid', 0, False),
        ('missing-extension', 2, False, 'missing-extension', 0, False),
    ],
)
def test_konomitv_bs4k_radio_opus_transport_probe_requires_audio_only_two_track_output(
    monkeypatch: pytest.MonkeyPatch,
    probe_case: str,
    opus_pid_count: int,
    include_video: bool,
    opus_descriptor_case: str,
    bridge_returncode: int,
    expected_available: bool,
) -> None:
    """ラジオ実 pipe probe は Bridge 成功かつ映像なし・Opus二音声の出力だけを受理する。"""

    output = (
        _wrap_psi_section(0x0000, _build_live_probe_pat())
        + _wrap_psi_section(
            0x1000,
            _build_radio_opus_probe_pmt(
                opus_pid_count,
                include_video = include_video,
                opus_descriptor_case = opus_descriptor_case,
            ),
        )
        + _build_live_probe_transport_packets(
            opus_pid_count,
            include_video = False,
        )
    )
    communicate_count = 0
    both_processes_communicating = asyncio.Event()

    class _ProbeProcess:
        """radio probe の subprocess と同時 drain を再現する。"""

        def __init__(self, stdout: bytes, returncode: int, stderr: bytes = b'') -> None:
            """標準出力・標準エラー・終了状態を保持する。"""

            self.stdout = stdout
            self.stderr = stderr
            self.returncode: int | None = returncode

        async def communicate(self) -> tuple[bytes, bytes]:
            """両 process の communicate() が開始するまで戻らない。"""

            nonlocal communicate_count

            communicate_count += 1
            if communicate_count == 2:
                both_processes_communicating.set()
            await both_processes_communicating.wait()
            return self.stdout, self.stderr

        def kill(self) -> None:
            """異常終了時の強制停止を再現する。"""

            self.returncode = -9

        async def wait(self) -> int:
            """強制停止後の終了待機を再現する。"""

            assert self.returncode is not None
            return self.returncode

    bridge_process = _ProbeProcess(
        output,
        bridge_returncode,
        b'bridge failed' if probe_case == 'bridge-failed' else b'',
    )
    ffmpeg_process = _ProbeProcess(b'', 0)
    processes = [bridge_process, ffmpeg_process]
    subprocess_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    async def CreateSubprocessExec(*args: str, **kwargs: object) -> _ProbeProcess:
        """Bridge、FFmpeg の順に process 代替を返す。"""

        subprocess_calls.append((args, kwargs))
        return processes.pop(0)

    monkeypatch.setitem(
        playback_capabilities_module.LIBRARY_PATH, 'FFmpeg8', '/runtime/ffmpeg8.elf'
    )
    monkeypatch.setitem(
        playback_capabilities_module.LIBRARY_PATH,
        KonomiTVBS4KPlaybackCapabilityProbe.BRIDGE_LIBRARY_PATH_KEY,
        '/runtime/ts-codec-bridge.elf',
    )
    monkeypatch.setattr(
        playback_capabilities_module.asyncio,
        'create_subprocess_exec',
        CreateSubprocessExec,
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe, '_live_probe_signature', None
    )
    monkeypatch.setattr(KonomiTVBS4KPlaybackCapabilityProbe, '_live_probe_results', {})
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe, '_radio_opus_probe_result', None
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe, '_live_probe_lock', asyncio.Lock()
    )

    async def RunProbeTwice() -> tuple[bool, bool]:
        """同じ署名の二回目が cache を再利用することも含めて実行する。"""

        return (
            await KonomiTVBS4KPlaybackCapabilityProbe.isRadioOpusTransportAvailable(),
            await KonomiTVBS4KPlaybackCapabilityProbe.isRadioOpusTransportAvailable(),
        )

    assert asyncio.run(RunProbeTwice()) == (expected_available, expected_available)
    assert len(subprocess_calls) == 2
    bridge_command, bridge_options = subprocess_calls[0]
    ffmpeg_command, ffmpeg_options = subprocess_calls[1]
    assert bridge_command == (
        '/runtime/ts-codec-bridge.elf',
        '--video-codec',
        'passthrough',
        '--audio-codec',
        'opus',
    )
    assert isinstance(bridge_options['stdin'], int)
    assert ffmpeg_options['stdout'] != asyncio.subprocess.PIPE
    assert [
        ffmpeg_command[index + 1]
        for index, option in enumerate(ffmpeg_command)
        if option == '-map'
    ] == ['0:a:0', '1:a:0']
    assert '-vn' in ffmpeg_command
    assert '-c:v' not in ffmpeg_command
    assert not any('testsrc' in argument for argument in ffmpeg_command)
    assert ffmpeg_command[ffmpeg_command.index('-ac') + 1] == '2'
    assert ffmpeg_command[ffmpeg_command.index('-b:a') + 1] == '192K'
    assert ffmpeg_command[ffmpeg_command.index('-af') + 1] == 'volume=2.0'
    assert ffmpeg_command[ffmpeg_command.index('-max_delay') + 1] == '250000'
    assert ffmpeg_command[ffmpeg_command.index('-max_interleave_delta') + 1] == '500K'
    assert ffmpeg_command[ffmpeg_command.index('-threads') + 1] == 'auto'


def test_konomitv_bs4k_radio_opus_probe_shares_live_binary_signature_cache_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FFmpeg/Bridge署名の世代変更時に映像付き行列とradio probeのcacheを同時破棄する。"""

    async def RunRadioOpusTransportProbe(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
    ) -> bool:
        """新世代の radio probe 結果を返す。"""

        return False

    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe, '_live_probe_signature', 'old-generation'
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        '_live_probe_results',
        {('av1', 8, 'opus'): True},
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe, '_radio_opus_probe_result', True
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe, '_live_probe_lock', asyncio.Lock()
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        '_KonomiTVBS4KPlaybackCapabilityProbe__getLiveProbeSignature',
        classmethod(lambda _cls: 'new-generation'),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        '_KonomiTVBS4KPlaybackCapabilityProbe__runRadioOpusTransportProbe',
        classmethod(RunRadioOpusTransportProbe),
    )

    assert (
        asyncio.run(KonomiTVBS4KPlaybackCapabilityProbe.isRadioOpusTransportAvailable())
        is False
    )
    assert KonomiTVBS4KPlaybackCapabilityProbe._live_probe_signature == 'new-generation'
    assert KonomiTVBS4KPlaybackCapabilityProbe._live_probe_results == {}
    assert KonomiTVBS4KPlaybackCapabilityProbe._radio_opus_probe_result is False


def test_konomitv_bs4k_live_negative_cache_expires_but_success_cache_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一時的な失敗だけを短TTLで再検査し、成功は同じ署名中に再利用する。"""

    now = 100.0
    probe_results = iter((False, True))
    probe_calls = 0

    async def RunLiveTransportProbe(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        _encoder: str,
        _codec: str,
        _bit_depth: int,
        _audio_codec: str,
    ) -> bool:
        nonlocal probe_calls
        probe_calls += 1
        return next(probe_results)

    monkeypatch.setattr(
        playback_capabilities_module.time,
        'monotonic',
        lambda: now,
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        '_KonomiTVBS4KPlaybackCapabilityProbe__getLiveProbeSignature',
        classmethod(lambda _cls: 'stable-generation'),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        '_KonomiTVBS4KPlaybackCapabilityProbe__runLiveTransportProbe',
        classmethod(RunLiveTransportProbe),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe, '_live_probe_signature', None
    )
    monkeypatch.setattr(KonomiTVBS4KPlaybackCapabilityProbe, '_live_probe_results', {})
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        '_live_probe_failure_timestamps',
        {},
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe, '_live_probe_event_loop', None
    )

    async def Verify() -> None:
        nonlocal now
        key = ('QSV', 'av1', 10, 'opus')
        assert await KonomiTVBS4KPlaybackCapabilityProbe.isLiveTransportAvailable(*key) is False
        now = 104.9
        assert await KonomiTVBS4KPlaybackCapabilityProbe.isLiveTransportAvailable(*key) is False
        assert probe_calls == 1
        now = 105.1
        assert await KonomiTVBS4KPlaybackCapabilityProbe.isLiveTransportAvailable(*key) is True
        now = 10_000.0
        assert await KonomiTVBS4KPlaybackCapabilityProbe.isLiveTransportAvailable(*key) is True

    asyncio.run(Verify())
    assert probe_calls == 2




def test_konomitv_bs4k_radio_opus_probe_closes_pipe_and_reaps_partial_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FFmpeg 起動失敗時も親 FD を両端とも閉じ、先に起動した Bridge を回収する。"""

    read_pipe = 9100
    write_pipe = 9101
    closed_pipes: list[int] = []
    subprocess_call_count = 0

    class _StartedBridge:
        """FFmpeg より先に起動済みの Bridge process を再現する。"""

        def __init__(self) -> None:
            """終了状態と回収回数を初期化する。"""

            self.returncode: int | None = None
            self.kill_count = 0
            self.wait_count = 0

        def kill(self) -> None:
            """Bridge の強制終了を記録する。"""

            self.kill_count += 1
            self.returncode = -9

        async def wait(self) -> int:
            """Bridge の終了待機を記録する。"""

            self.wait_count += 1
            assert self.returncode is not None
            return self.returncode

    bridge_process = _StartedBridge()

    async def CreateSubprocessExec(*_args: str, **_kwargs: object) -> _StartedBridge:
        """Bridge は起動し、続く FFmpeg 起動だけを失敗させる。"""

        nonlocal subprocess_call_count

        subprocess_call_count += 1
        if subprocess_call_count == 1:
            return bridge_process
        raise OSError('synthetic FFmpeg spawn failure')

    monkeypatch.setitem(
        playback_capabilities_module.LIBRARY_PATH, 'FFmpeg8', '/runtime/ffmpeg8.elf'
    )
    monkeypatch.setitem(
        playback_capabilities_module.LIBRARY_PATH,
        KonomiTVBS4KPlaybackCapabilityProbe.BRIDGE_LIBRARY_PATH_KEY,
        '/runtime/ts-codec-bridge.elf',
    )
    monkeypatch.setattr(
        playback_capabilities_module,
        'os',
        SimpleNamespace(
            pipe = lambda: (read_pipe, write_pipe),
            close = closed_pipes.append,
        ),
    )
    monkeypatch.setattr(
        playback_capabilities_module.asyncio,
        'create_subprocess_exec',
        CreateSubprocessExec,
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe, '_live_probe_signature', None
    )
    monkeypatch.setattr(KonomiTVBS4KPlaybackCapabilityProbe, '_live_probe_results', {})
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe, '_radio_opus_probe_result', None
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe, '_live_probe_lock', asyncio.Lock()
    )

    assert (
        asyncio.run(KonomiTVBS4KPlaybackCapabilityProbe.isRadioOpusTransportAvailable())
        is False
    )
    assert subprocess_call_count == 2
    assert closed_pipes == [read_pipe, write_pipe]
    assert bridge_process.kill_count == 1
    assert bridge_process.wait_count == 1
