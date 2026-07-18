from pathlib import Path

from app.utils.TSKeyFrameSeeker import TSKeyFrameSeeker


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


def _pes_packet(pid: int, data_identifier: int, *, stream_id: int = 0xBD) -> bytes:
    """data_identifierを持つテスト用ARIB PESを単一TS packetへ包む。"""

    header = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10])
    if stream_id == 0xBD:
        payload = bytes.fromhex('000001bd0004800000') + bytes([data_identifier])
    else:
        payload = bytes.fromhex('000001bf0001') + bytes([data_identifier])
    return header + payload + b'\xFF' * (188 - len(header) - len(payload))


def test_arib_caption_pid_is_detected_from_data_component_descriptor(tmp_path: Path) -> None:
    """bin_dataでもdata_component_id 0x0008のPIDだけをARIB字幕として検出する。"""

    pat_body = b'\x00\x01\xC1\x00\x00' + b'\x00\x01\xF0\x00'
    pat_without_crc = b'\x00\xB0\x0D' + pat_body
    pat = pat_without_crc + _mpeg_crc32(pat_without_crc)

    caption_descriptor = b'\xFD\x02\x00\x08'
    data_broadcast_descriptor = b'\xFD\x02\x00\x12'
    streams = (
        b'\x06\xE1\x38\xF0\x04' + caption_descriptor +
        b'\x06\xE1\x39\xF0\x04' + data_broadcast_descriptor
    )
    pmt_body = b'\x00\x01\xC1\x00\x00\xE1\x00\xF0\x00' + streams
    section_length = len(pmt_body) + 4
    pmt_without_crc = b'\x02' + (0xB000 | section_length).to_bytes(2, 'big') + pmt_body
    pmt = pmt_without_crc + _mpeg_crc32(pmt_without_crc)

    fixture = tmp_path / 'caption.ts'
    fixture.write_bytes(
        _psi_packet(0x0000, pat) +
        _psi_packet(0x1000, pmt) +
        _pes_packet(0x0138, 0x80)
    )

    assert TSKeyFrameSeeker.findARIBCaptionPIDs(fixture) == {0x0138}


def test_arib_superimpose_pid_is_not_detected_as_caption(tmp_path: Path) -> None:
    """同じdata_component_idでもdata_identifier 0x81の文字スーパーを字幕から除外する。"""

    pat_body = b'\x00\x01\xC1\x00\x00' + b'\x00\x01\xF0\x00'
    pat_without_crc = b'\x00\xB0\x0D' + pat_body
    pat = pat_without_crc + _mpeg_crc32(pat_without_crc)
    descriptor = b'\xFD\x02\x00\x08'
    streams = b'\x06\xE1\x38\xF0\x04' + descriptor
    pmt_body = b'\x00\x01\xC1\x00\x00\xE1\x00\xF0\x00' + streams
    section_length = len(pmt_body) + 4
    pmt_without_crc = b'\x02' + (0xB000 | section_length).to_bytes(2, 'big') + pmt_body
    pmt = pmt_without_crc + _mpeg_crc32(pmt_without_crc)

    fixture = tmp_path / 'superimpose.ts'
    fixture.write_bytes(
        _psi_packet(0x0000, pat) +
        _psi_packet(0x1000, pmt) +
        _pes_packet(0x0138, 0x81, stream_id = 0xBF)
    )

    assert TSKeyFrameSeeker.findARIBCaptionPIDs(fixture) == set()
