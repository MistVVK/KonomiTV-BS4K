import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

from app.streams.RecordedSubtitleStream import RecordedSubtitleStream


def _encode_pts(pts: int) -> bytes:
    """テスト用PES PTSを33bit形式へ符号化する。"""

    return bytes([
        0x21 | (((pts >> 30) & 0x07) << 1),
        (pts >> 22) & 0xFF,
        0x01 | (((pts >> 15) & 0x7F) << 1),
        (pts >> 7) & 0xFF,
        0x01 | ((pts & 0x7F) << 1),
    ])


def _mpeg_crc32(data: bytes) -> bytes:
    """PSI section用MPEG-2 CRC32を返す。"""

    crc = 0xFFFFFFFF
    for value in data:
        crc ^= value << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc.to_bytes(4, 'big')


def _psi_packet(pid: int, section: bytes) -> bytes:
    """単一TS packetへ収まるテスト用PSI sectionを包む。"""

    header = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10])
    payload = b'\x00' + section
    return header + payload + b'\xFF' * (188 - len(header) - len(payload))


def _make_pat(programs: list[tuple[int, int]] | None = None) -> bytes:
    """program numberとPMT PIDの組を持つPATを返す。"""

    programs = programs or [(1, 0x1000)]
    entries = b''.join(
        program_number.to_bytes(2, 'big') + (0xE000 | pmt_pid).to_bytes(2, 'big')
        for program_number, pmt_pid in programs
    )
    body = b'\x00\x01\xC1\x00\x00' + entries
    section = b'\x00' + (0xB000 | len(body) + 4).to_bytes(2, 'big') + body
    return section + _mpeg_crc32(section)


def _make_ttml_pmt(streams: list[tuple[int, int]], program_number: int = 1) -> bytes:
    """component tagとPIDの組を持つARIB-TTML PMTを返す。"""

    entries = bytearray()
    for component_tag, pid in streams:
        descriptors = b'\x52\x01' + bytes((component_tag,)) + b'\x26\x0D\xFF\xFFID3 \xFFID3 \x00\x0F'
        entries.extend(b'\x15' + (0xE000 | pid).to_bytes(2, 'big'))
        entries.extend((0xF000 | len(descriptors)).to_bytes(2, 'big') + descriptors)
    body = program_number.to_bytes(2, 'big') + b'\xC1\x00\x00\xE1\x00\xF0\x00' + bytes(entries)
    section_length = len(body) + 4
    section = b'\x02' + (0xB000 | section_length).to_bytes(2, 'big') + body
    return section + _mpeg_crc32(section)


def _synchsafe(value: int) -> bytes:
    """ID3v2.4用synchsafe integerを返す。"""

    return bytes(((value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F))


def _make_ttml_pes_packet(
    pid: int,
    component_tag: int,
    pts: int,
    *,
    owner: bytes = b'arib-ttml.js',
    envelope_version: int = 1,
) -> tuple[bytes, bytes]:
    """timed ID3のTS packetとPES payloadとしての生ID3を返す。"""

    envelope = bytearray(20)
    envelope[0] = envelope_version
    envelope[1] = component_tag
    if envelope_version == 2:
        envelope.extend((pts & 0x1FFFFFFFF).to_bytes(8, 'big'))
    priv_payload = owner + b'\x00' + bytes(envelope)
    frame = b'PRIV' + _synchsafe(len(priv_payload)) + b'\x00\x00' + priv_payload
    id3 = b'ID3\x04\x00\x00' + _synchsafe(len(frame)) + frame
    pes_optional_header = b'\x80\x80\x05' + _encode_pts(pts)
    pes_payload = pes_optional_header + id3
    pes = b'\x00\x00\x01\xBD' + len(pes_payload).to_bytes(2, 'big') + pes_payload
    header = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10])
    return (header + pes + b'\xFF' * (188 - len(header) - len(pes)), id3)


def test_arib_packet_pts_is_relative_to_recording_start() -> None:
    """最初の字幕ではなく録画コンテナの開始PTSが0秒になることを確認する。"""

    probe = {'format': {'start_time': '90.000000'}}
    source_packets = [{
        'pts_time': '100.500000',
        'duration_time': '0.500000',
        'data': '00000000: 80ff 00                              ...',
    }]
    packets = RecordedSubtitleStream._RecordedSubtitleStream__buildARIBPackets(  # pyright: ignore[reportPrivateUsage]
        probe,
        source_packets,
    )

    assert packets == [{
        'pts': 10.5,
        'duration': 0.5,
        'data': 'gP8A',
        'is_restore_point': False,
    }]


def test_arib_packet_pts_is_clamped_at_recording_start() -> None:
    """コンテナ開始PTSより前の丸め誤差が負の再生時刻にならないことを確認する。"""

    probe = {'format': {'start_time': '100.000001'}}
    source_packets = [{
        'pts_time': '100.000000',
        'data': '00000000: 80                                  .',
    }]
    packets = RecordedSubtitleStream._RecordedSubtitleStream__buildARIBPackets(  # pyright: ignore[reportPrivateUsage]
        probe,
        source_packets,
    )

    assert packets[0]['pts'] == 0.0


def test_bin_data_arib_packet_is_extracted_directly_from_ts(tmp_path: Path, monkeypatch) -> None:
    """FFprobeがpacketを公開しないbin_data字幕をPID指定でPESから復元する。"""

    subtitle_pid = 0x0138
    raw_data = bytes([0x80, 0xFF, 0x00, 0x04, 0, 0, 0, 0])
    pes_optional_header = b'\x80\x80\x05' + _encode_pts(100 * 90_000)
    pes_payload = pes_optional_header + raw_data
    pes = b'\x00\x00\x01\xBD' + len(pes_payload).to_bytes(2, 'big') + pes_payload
    ts_header = bytes([0x47, 0x40 | (subtitle_pid >> 8), subtitle_pid & 0xFF, 0x10])
    fixture = tmp_path / 'bin-data-caption.ts'
    fixture.write_bytes(ts_header + pes + b'\xFF' * (188 - len(ts_header) - len(pes)))
    monkeypatch.setattr(
        'app.streams.RecordedSubtitleStream.TSKeyFrameSeeker.findStreamInfo',
        lambda _path: SimpleNamespace(packet_size=188),
    )

    packets = RecordedSubtitleStream._RecordedSubtitleStream__extractARIBPacketsFromTS(  # pyright: ignore[reportPrivateUsage]
        fixture,
        subtitle_pid,
        90.0,
    )

    assert packets == [{
        'pts': 10.0,
        'duration': None,
        'data': base64.b64encode(raw_data).decode(),
        'is_restore_point': False,
    }]


def test_arib_management_and_drcs_packet_is_identified() -> None:
    """管理データgroupとDRCS data unitを生PESから識別する。"""

    packet = bytes([0x80, 0xFF, 0x00, 0x00, 0, 0, 0, 0, 0x1F, 0x30, 0, 0, 1, 0xAA])

    result = RecordedSubtitleStream._RecordedSubtitleStream__inspectARIBPacket(  # pyright: ignore[reportPrivateUsage]
        packet,
    )

    assert result == (True, True)


def test_arib_seek_returns_all_prior_restore_packets(monkeypatch) -> None:
    """任意seekで管理データ・DRCS・表示状態を録画先頭から再構築できるようにする。"""

    management_packet = bytes([0x80, 0xFF, 0x00, 0x00, 0, 0, 0, 0, 0x1F, 0x30, 0, 0, 1, 0xAA])
    statement_packet = bytes([0x80, 0xFF, 0x00, 0x04, 0, 0, 0, 0])
    packets = [
        {'pts': 0.5, 'duration': None, 'data': base64.b64encode(management_packet).decode(), 'is_restore_point': False},
        {'pts': 1.5, 'duration': None, 'data': base64.b64encode(statement_packet).decode(), 'is_restore_point': False},
        {'pts': 2.5, 'duration': None, 'data': base64.b64encode(statement_packet).decode(), 'is_restore_point': False},
    ]

    async def LoadPackets(_self, _subtitle_index, _track):
        """テスト用ARIB packet索引を返す。"""

        return packets

    monkeypatch.setattr(
        RecordedSubtitleStream,
        '_RecordedSubtitleStream__loadARIBPackets',
        LoadPackets,
    )
    recorded_video = SimpleNamespace(subtitle_tracks=[{
        'index': 1,
        'stream_index': 3,
        'codec': 'arib_caption',
    }])
    stream = RecordedSubtitleStream(recorded_video)

    result = asyncio.run(stream.getARIBRange(1, 2.0, 4.0))

    assert result is not None
    assert [packet['pts'] for packet in result['restore_packets']] == [0.5, 1.5]
    assert [packet['pts'] for packet in result['packets']] == [2.5]
    assert result['management_data'] == [base64.b64encode(management_packet).decode()]
    assert result['drcs'] == [base64.b64encode(management_packet).decode()]


def test_arib_ttml_packets_are_extracted_with_component_and_relative_pts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """PMTとownerが一致する字幕・文字スーパーID3を録画先頭0秒へ補正して索引化する。"""

    caption_packet, caption_id3 = _make_ttml_pes_packet(0x0130, 0x30, 100 * 90_000)
    # v1（source PTSなし）とv2（source PTSあり）が同じ録画に存在しても両方を抽出する。
    superimpose_packet, superimpose_id3 = _make_ttml_pes_packet(
        0x0138,
        0x38,
        101 * 90_000,
        envelope_version=2,
    )
    wrong_owner_packet, _ = _make_ttml_pes_packet(0x0139, 0x30, 102 * 90_000, owner=b'other-owner')
    fixture = tmp_path / 'arib-ttml.ts'
    fixture.write_bytes(
        _psi_packet(0x0000, _make_pat()) +
        _psi_packet(0x1000, _make_ttml_pmt([(0x30, 0x0130), (0x38, 0x0138), (0x30, 0x0139)])) +
        caption_packet + superimpose_packet + wrong_owner_packet
    )
    monkeypatch.setattr(
        'app.streams.RecordedSubtitleStream.TSKeyFrameSeeker.findStreamInfo',
        lambda _path: SimpleNamespace(packet_size=188),
    )

    packets = RecordedSubtitleStream._RecordedSubtitleStream__extractARIBTTMLPacketsFromTS(  # pyright: ignore[reportPrivateUsage]
        fixture,
        90.0,
    )

    assert packets == [
        {
            'pts': 10.0,
            'transport_timestamp': 100.0,
            'component_tag': 0x30,
            'data': base64.b64encode(caption_id3).decode(),
            'is_restore_point': False,
        },
        {
            'pts': 11.0,
            'transport_timestamp': 101.0,
            'component_tag': 0x38,
            'data': base64.b64encode(superimpose_id3).decode(),
            'is_restore_point': False,
        },
    ]


def test_arib_ttml_track_is_not_routed_to_legacy_b24_range() -> None:
    """ARIB-TTML論理trackを既存B24 packet APIへ誤って渡さない。"""

    recorded_video = SimpleNamespace(subtitle_tracks=[{
        'index': 1,
        'codec': 'arib_ttml',
        'language': 'jpn',
        'title': None,
        'pid': 0x0130,
        'component_tag': 0x30,
    }])
    stream = RecordedSubtitleStream(recorded_video)

    assert asyncio.run(stream.getARIBRange(1, 0.0, 6.0)) is None


def test_arib_ttml_extraction_filters_other_programs(tmp_path: Path, monkeypatch) -> None:
    """複数service TSから録画対象programのraw ID3だけを索引化する。"""

    first_packet, _ = _make_ttml_pes_packet(0x0130, 0x30, 100 * 90_000)
    second_packet, second_id3 = _make_ttml_pes_packet(0x0230, 0x30, 101 * 90_000)
    fixture = tmp_path / 'multi-service-arib-ttml.ts'
    fixture.write_bytes(
        _psi_packet(0x0000, _make_pat([(1, 0x1000), (2, 0x1001)])) +
        _psi_packet(0x1000, _make_ttml_pmt([(0x30, 0x0130)], program_number=1)) +
        _psi_packet(0x1001, _make_ttml_pmt([(0x30, 0x0230)], program_number=2)) +
        first_packet + second_packet
    )
    monkeypatch.setattr(
        'app.streams.RecordedSubtitleStream.TSKeyFrameSeeker.findStreamInfo',
        lambda _path: SimpleNamespace(packet_size=188),
    )

    packets = RecordedSubtitleStream._RecordedSubtitleStream__extractARIBTTMLPacketsFromTS(  # pyright: ignore[reportPrivateUsage]
        fixture,
        90.0,
        {2},
    )

    assert len(packets) == 1
    assert packets[0]['pts'] == 11.0
    assert packets[0]['transport_timestamp'] == 101.0
    assert packets[0]['data'] == base64.b64encode(second_id3).decode()


def test_arib_ttml_range_returns_prior_packets_for_seek_restore(monkeypatch) -> None:
    """録画seek時は共通JavaScript decoderへ先頭からのraw ID3を復元用として返す。"""

    packets = [
        {
            'pts': 0.5, 'transport_timestamp': 90.5, 'component_tag': 0x30,
            'data': 'ZA==', 'is_restore_point': False,
        },
        {
            'pts': 1.0, 'transport_timestamp': 91.0, 'component_tag': 0x30,
            'data': 'YQ==', 'is_restore_point': False,
        },
        {
            'pts': 1.0, 'transport_timestamp': 91.0, 'component_tag': 0x30,
            'data': 'ZQ==', 'is_restore_point': False,
        },
        {
            'pts': 2.0, 'transport_timestamp': 92.0, 'component_tag': 0x38,
            'data': 'Yg==', 'is_restore_point': False,
        },
        {
            'pts': 3.0, 'transport_timestamp': 93.0, 'component_tag': 0x30,
            'data': 'Yw==', 'is_restore_point': False,
        },
    ]

    async def LoadPackets(_self):
        """テスト用ARIB-TTML索引を返す。"""

        return RecordedSubtitleStream._RecordedSubtitleStream__buildARIBTTMLPacketIndex(  # pyright: ignore[reportPrivateUsage]
            packets,
        )

    monkeypatch.setattr(
        RecordedSubtitleStream,
        '_RecordedSubtitleStream__loadARIBTTMLPackets',
        LoadPackets,
    )
    recorded_video = SimpleNamespace(subtitle_tracks=[{
        'index': 1,
        'codec': 'arib_ttml',
        'language': 'jpn',
        'title': None,
        'pid': 0x0130,
        'component_tag': 0x30,
    }])
    stream = RecordedSubtitleStream(recorded_video)

    result = asyncio.run(stream.getARIBTTMLRange(2.5, 4.0))

    assert result is not None
    assert [packet['pts'] for packet in result['restore_packets']] == [1.0, 1.0, 2.0]
    assert [packet['transport_timestamp'] for packet in result['restore_packets']] == [91.0, 91.0, 92.0]
    assert all(packet['is_restore_point'] is True for packet in result['restore_packets'])
    assert [packet['pts'] for packet in result['packets']] == [3.0]
    assert result['packets'][0]['transport_timestamp'] == 93.0
    assert result['packets'][0]['is_restore_point'] is False
