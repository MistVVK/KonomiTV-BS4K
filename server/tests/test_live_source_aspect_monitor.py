"""LiveSourceAspectMonitor と full-GPU フィルタ分岐の単体試験。"""

from __future__ import annotations

from app.streams.KonomiTVBS4KPlaybackEncoding import (
    BuildKonomiTVBS4KLiveHardwareVideoFilters,
    CanUseKonomiTVBS4KLiveHardwareFilterPath,
)
from app.streams.LiveSourceAspectMonitor import (
    EstimateDefaultLiveSourceGeometry,
    LiveSourceAspectMonitor,
    LiveSourceVideoGeometry,
    ParseMpeg2SequenceHeaderGeometry,
)


def _buildMpeg2SequenceHeader(
    width: int,
    height: int,
    aspect_ratio_information: int,
) -> bytes:
    """
    最小限の MPEG-2 sequence header (start code + 4 bytes)。

    Args:
        width (int): width に指定する値。
        height (int): height に指定する値。
        aspect_ratio_information (int): aspect_ratio_information に指定する値。

    Returns:
        bytes: 処理結果のバイト列。
    """

    byte0 = (width >> 4) & 0xFF
    byte1 = ((width & 0x0F) << 4) | ((height >> 8) & 0x0F)
    byte2 = height & 0xFF
    byte3 = ((aspect_ratio_information & 0x0F) << 4) | 0x03  # frame_rate_code=3 (25fps 等)
    return b'\x00\x00\x01\xb3' + bytes([byte0, byte1, byte2, byte3])


def _buildTsPacket(pid: int, payload: bytes, *, pusi: bool = True, continuity: int = 0) -> bytes:
    """
    テスト用の最小 188 バイト TS パケット。

    Args:
        pid (int): pid に指定する値。
        payload (bytes): payload に指定する値。
        pusi (bool): pusi に指定する値。
        continuity (int): continuity に指定する値。

    Returns:
        bytes: 処理結果のバイト列。
    """

    header = bytearray(4)
    header[0] = 0x47
    header[1] = (0x40 if pusi else 0x00) | ((pid >> 8) & 0x1F)
    header[2] = pid & 0xFF
    header[3] = 0x10 | (continuity & 0x0F)  # payload only
    body = bytes(header) + payload
    if len(body) > 188:
        raise ValueError('payload too large for single TS packet')
    return body + bytes(188 - len(body))


def test_parse_mpeg2_sequence_header_1440x1080_dar_16_9() -> None:
    """
    地デジ HD 典型: 1440x1080 DAR 16:9 → SAR 4:3。

    Args:
        None

    Returns:
        None
    """

    header = _buildMpeg2SequenceHeader(1440, 1080, aspect_ratio_information=3)
    geometry = ParseMpeg2SequenceHeaderGeometry(header)
    assert geometry is not None
    assert geometry.coded_width == 1440
    assert geometry.coded_height == 1080
    assert geometry.dar_width == 16
    assert geometry.dar_height == 9
    assert geometry.sample_aspect_ratio == (4, 3)
    assert geometry.is_approximately_16_9 is True
    assert geometry.is_approximately_4_3 is False


def test_parse_mpeg2_sequence_header_dar_4_3() -> None:
    """
    4:3 表示の sequence header を DAR として読む。

    Args:
        None

    Returns:
        None
    """

    header = _buildMpeg2SequenceHeader(720, 480, aspect_ratio_information=2)
    geometry = ParseMpeg2SequenceHeaderGeometry(header)
    assert geometry is not None
    assert geometry.is_approximately_4_3 is True
    assert geometry.is_approximately_16_9 is False


def test_default_geometry_hd_and_fullhd() -> None:
    """
    既定 geometry は HD アナモルフィック / フル HD を区別する。

    Args:
        None

    Returns:
        None
    """

    hd = EstimateDefaultLiveSourceGeometry('GR', is_fullhd_channel=False)
    assert hd.coded_width == 1440
    assert hd.sample_aspect_ratio == (4, 3)

    fullhd = EstimateDefaultLiveSourceGeometry('GR', is_fullhd_channel=True)
    assert fullhd.coded_width == 1920
    assert fullhd.sample_aspect_ratio == (1, 1)


def test_hardware_path_allowed_only_for_16_9_without_24fps() -> None:
    """
    full-GPU 経路は 16:9 かつ 24fps 以外。

    Args:
        None

    Returns:
        None
    """

    assert CanUseKonomiTVBS4KLiveHardwareFilterPath(
        source_dar_width=16,
        source_dar_height=9,
        is_24fps_mode_enabled=False,
    ) is True
    assert CanUseKonomiTVBS4KLiveHardwareFilterPath(
        source_dar_width=4,
        source_dar_height=3,
        is_24fps_mode_enabled=False,
    ) is False
    assert CanUseKonomiTVBS4KLiveHardwareFilterPath(
        source_dar_width=16,
        source_dar_height=9,
        is_24fps_mode_enabled=True,
    ) is False


def test_build_qsv_hardware_filters_include_vpp() -> None:
    """
    QSV full-GPU 経路は vpp_qsv deinterlace + scale を使う。

    Args:
        None

    Returns:
        None
    """

    filters = BuildKonomiTVBS4KLiveHardwareVideoFilters(
        'QSV',
        encode_width=1920,
        encode_height=1080,
        encoder_pixel_format='p010le',
        is_interlaced=True,
        is_60fps=False,
        low_latency=True,
    )
    assert len(filters) == 1
    assert filters[0].startswith('vpp_qsv=')
    assert 'deinterlace=advanced' in filters[0]
    assert 'w=1920:h=1080' in filters[0]
    assert 'format=p010le' in filters[0]
    assert 'hwdownload' not in ','.join(filters)


def test_monitor_detects_geometry_from_video_pes_packets() -> None:
    """
    映像 PID 上の PES に載せた sequence header を monitor が拾う。

    Args:
        None

    Returns:
        None
    """

    seq = _buildMpeg2SequenceHeader(1440, 1080, 3)
    # PES header: startcode + stream_id + length + flags + header_data_length 0
    pes = b'\x00\x00\x01\xe0\x00\x00\x80\x00\x00' + seq

    monitor = LiveSourceAspectMonitor()
    # PAT/PMT を通さず、映像 PID を直接指定して PES 経路だけを試験する
    monitor._video_pid = 0x0100  # 単体試験用
    geometry = monitor.push(_buildTsPacket(0x0100, pes, continuity=0))
    assert geometry is not None
    assert geometry.coded_width == 1440
    assert geometry.dar_width == 16
    assert monitor.geometry == geometry


def test_geometry_equality_for_restart_decision() -> None:
    """
    DAR 16:9 同士は経路切替不要。

    Args:
        None

    Returns:
        None
    """

    a = LiveSourceVideoGeometry(1440, 1080, 16, 9)
    b = LiveSourceVideoGeometry(1920, 1080, 16, 9)
    assert a.is_approximately_16_9 is True
    assert b.is_approximately_16_9 is True
    c = LiveSourceVideoGeometry(720, 480, 4, 3)
    assert c.is_approximately_16_9 is False
