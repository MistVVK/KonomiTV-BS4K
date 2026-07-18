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
