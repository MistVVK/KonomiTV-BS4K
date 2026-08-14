from pathlib import Path

from app.utils.TSKeyFrameSeeker import ARIBTTMLStreamInfo, TSKeyFrameSeeker


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


def _synchsafe(value: int) -> bytes:
    """ID3v2.4用synchsafe integerを返す。"""

    return bytes(((value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F))


def _timed_id3(
    component_tag: int,
    owner: bytes = b'arib-ttml.js',
    envelope_version: int = 1,
) -> bytes:
    """ARIB-TTML PRIVを持つテスト用ID3 tagを返す。"""

    envelope = bytearray(20)
    envelope[0] = envelope_version
    envelope[1] = component_tag
    if envelope_version == 2:
        envelope.extend((12_345 * 90_000).to_bytes(8, 'big'))
    priv_payload = owner + b'\x00' + bytes(envelope)
    frame = b'PRIV' + _synchsafe(len(priv_payload)) + b'\x00\x00' + priv_payload
    return b'ID3\x04\x00\x00' + _synchsafe(len(frame)) + frame


def _timed_id3_packet(
    pid: int,
    component_tag: int,
    owner: bytes = b'arib-ttml.js',
    envelope_version: int = 1,
) -> bytes:
    """ARIB-TTML PRIVを持つテスト用timed ID3 PESを単一TS packetへ包む。"""

    id3 = _timed_id3(component_tag, owner, envelope_version)
    pes_payload = b'\x80\x00\x00' + id3
    pes = b'\x00\x00\x01\xBD' + len(pes_payload).to_bytes(2, 'big') + pes_payload
    header = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10])
    return header + pes + b'\xFF' * (188 - len(header) - len(pes))


def _metadata_pmt(component_tag: int, pid: int, *, include_metadata_descriptor: bool = True) -> bytes:
    """timed ID3 streamを持つテスト用PMT sectionを返す。"""

    descriptors = b'\x52\x01' + bytes((component_tag,))
    if include_metadata_descriptor is True:
        descriptors += b'\x26\x0D\xFF\xFFID3 \xFFID3 \x00\x0F'
    streams = b'\x15' + (0xE000 | pid).to_bytes(2, 'big') + (0xF000 | len(descriptors)).to_bytes(2, 'big') + descriptors
    pmt_body = b'\x00\x01\xC1\x00\x00\xE1\x00\xF0\x00' + streams
    section_length = len(pmt_body) + 4
    pmt_without_crc = b'\x02' + (0xB000 | section_length).to_bytes(2, 'big') + pmt_body
    return pmt_without_crc + _mpeg_crc32(pmt_without_crc)


def _pat(programs: list[tuple[int, int]] | None = None) -> bytes:
    """program numberとPMT PIDの組を持つテスト用PAT sectionを返す。"""

    programs = programs or [(1, 0x1000)]
    entries = b''.join(
        program_number.to_bytes(2, 'big') + (0xE000 | pmt_pid).to_bytes(2, 'big')
        for program_number, pmt_pid in programs
    )
    pat_body = b'\x00\x01\xC1\x00\x00' + entries
    pat_without_crc = b'\x00' + (0xB000 | len(pat_body) + 4).to_bytes(2, 'big') + pat_body
    return pat_without_crc + _mpeg_crc32(pat_without_crc)


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


def test_arib_ttml_stream_requires_metadata_descriptor_and_priv_owner(tmp_path: Path) -> None:
    """PMTのID3記述子とARIB-TTML PRIV ownerが揃ったstreamだけを検出する。"""

    valid_pid = 0x0130
    wrong_owner_pid = 0x0131
    missing_descriptor_pid = 0x0132
    fixture = tmp_path / 'arib-ttml.ts'
    fixture.write_bytes(
        _psi_packet(0x0000, _pat()) +
        _psi_packet(0x1000, _metadata_pmt(0x30, valid_pid)) +
        _timed_id3_packet(valid_pid, 0x30) +
        _psi_packet(0x1000, _metadata_pmt(0x30, wrong_owner_pid)) +
        _timed_id3_packet(wrong_owner_pid, 0x30, owner=b'other-owner') +
        _psi_packet(0x1000, _metadata_pmt(0x30, missing_descriptor_pid, include_metadata_descriptor=False)) +
        _timed_id3_packet(missing_descriptor_pid, 0x30)
    )

    assert TSKeyFrameSeeker.findARIBTTMLStreams(fixture, sample_count=1) == [
        ARIBTTMLStreamInfo(program_number=1, pid=valid_pid, component_tag=0x30),
    ]


def test_arib_ttml_component_tag_accepts_all_caption_and_superimpose_groups() -> None:
    """ARIB規定の字幕0x30..0x37・文字スーパー0x38..0x3Fを全て受理する。"""

    metadata_descriptor = (0x26, b'\xFF\xFFID3 \xFFID3 \x00\x0F')
    for component_tag in range(0x30, 0x40):
        assert TSKeyFrameSeeker.getARIBTTMLComponentTag(
            0x15,
            [(0x52, bytes((component_tag,))), metadata_descriptor],
        ) == component_tag
        for envelope_version in (1, 2):
            assert TSKeyFrameSeeker.getARIBTTMLTimedID3ComponentTag(
                _timed_id3(component_tag, envelope_version=envelope_version),
            ) == component_tag


def test_arib_ttml_component_tag_rejects_unknown_envelope_version() -> None:
    """source PTSの有無を定義していない独自外装versionは受理しない。"""

    for envelope_version in (0, 3):
        assert TSKeyFrameSeeker.getARIBTTMLTimedID3ComponentTag(
            _timed_id3(0x30, envelope_version=envelope_version),
        ) is None


def test_arib_ttml_stream_keeps_program_number_for_multi_service_ts(tmp_path: Path) -> None:
    """複数service TSの同一componentをprogram number付きで分離する。"""

    fixture = tmp_path / 'multi-service-arib-ttml.ts'
    fixture.write_bytes(
        _psi_packet(0x0000, _pat([(1, 0x1000), (2, 0x1001)])) +
        _psi_packet(0x1000, _metadata_pmt(0x30, 0x0130)) +
        _timed_id3_packet(0x0130, 0x30) +
        _psi_packet(0x1001, _metadata_pmt(0x30, 0x0230)) +
        _timed_id3_packet(0x0230, 0x30)
    )

    assert TSKeyFrameSeeker.findARIBTTMLStreams(fixture, sample_count=1) == [
        ARIBTTMLStreamInfo(program_number=1, pid=0x0130, component_tag=0x30),
        ARIBTTMLStreamInfo(program_number=2, pid=0x0230, component_tag=0x30),
    ]


def test_arib_ttml_stream_is_found_after_dynamic_pmt_update(tmp_path: Path) -> None:
    """録画先頭に存在せず、後半のPMTで追加されたARIB-TTML字幕も検出する。"""

    subtitle_pid = 0x0130
    null_packet = bytes.fromhex('471fff10') + b'\xFF' * 184
    prefix = _psi_packet(0x0000, _pat()) + _psi_packet(0x1000, _metadata_pmt(0x30, 0x0131))
    dynamic_stream = (
        _psi_packet(0x0000, _pat()) +
        _psi_packet(0x1000, _metadata_pmt(0x30, subtitle_pid)) +
        _timed_id3_packet(subtitle_pid, 0x30)
    )
    fixture = tmp_path / 'dynamic-arib-ttml.ts'
    fixture.write_bytes(prefix + null_packet * 32 + dynamic_stream)

    assert TSKeyFrameSeeker.findARIBTTMLStreams(
        fixture,
        sample_count=2,
        max_scan_bytes_per_sample=8 * 188,
    ) == [ARIBTTMLStreamInfo(program_number=1, pid=subtitle_pid, component_tag=0x30)]


def test_arib_ttml_v2_owner_is_verified_outside_pmt_sample_window(tmp_path: Path) -> None:
    """PMT窓内に台詞がなくても、候補PIDだけを追跡して後続ARIB-TTMLを確認する。"""

    subtitle_pid = 0x0130
    null_packet = bytes.fromhex('471fff10') + b'\xFF' * 184
    fixture = tmp_path / 'sparse-arib-ttml.ts'
    fixture.write_bytes(
        _psi_packet(0x0000, _pat()) +
        _psi_packet(0x1000, _metadata_pmt(0x30, subtitle_pid)) +
        null_packet * 32 +
        _timed_id3_packet(subtitle_pid, 0x30, envelope_version=2)
    )

    assert TSKeyFrameSeeker.findARIBTTMLStreams(
        fixture,
        sample_count=1,
        max_scan_bytes_per_sample=3 * 188,
    ) == [ARIBTTMLStreamInfo(program_number=1, pid=subtitle_pid, component_tag=0x30)]


def test_detect_packet_size_and_offset_ignores_leading_junk(tmp_path: Path) -> None:
    """188-byte TS と 192-byte TTS の代表的な prefix から物理 packet 先頭を検出する。"""

    for packet_size, prefix_len in ((188, 187), (192, 3)):
        packet = _psi_packet(0x0000, _pat())
        if packet_size == 192:
            packet = b'\x00\x00\x00\x00' + packet
        fixture = tmp_path / f'prefixed-{packet_size}-{prefix_len}.ts'
        fixture.write_bytes((b'\xFF' * prefix_len) + (packet * 8))

        detected_size, start_offset = TSKeyFrameSeeker._TSKeyFrameSeeker__detectPacketSizeAndOffset(fixture)  # pyright: ignore[reportPrivateUsage]
        assert detected_size == packet_size
        assert start_offset == prefix_len


def _video_pmt(video_pid: int = 0x0100, pcr_pid: int = 0x0100, stream_type: int = 0x1B) -> bytes:
    """H.264 映像 1 本を持つテスト用 PMT section を返す。"""

    streams = bytes([stream_type]) + (0xE000 | video_pid).to_bytes(2, 'big') + b'\xF0\x00'
    pmt_body = b'\x00\x01\xC1\x00\x00' + (0xE000 | pcr_pid).to_bytes(2, 'big') + b'\xF0\x00' + streams
    section_length = len(pmt_body) + 4
    pmt_without_crc = b'\x02' + (0xB000 | section_length).to_bytes(2, 'big') + pmt_body
    return pmt_without_crc + _mpeg_crc32(pmt_without_crc)


def _wrap_ts_packets(packets_188: bytes, packet_size: int) -> bytes:
    """188-byte 連続パケットを 188/192 のファイル表現へ包む。"""

    if packet_size == 188:
        return packets_188
    if len(packets_188) % 188 != 0:
        raise ValueError('packets_188 length must be a multiple of 188')
    wrapped = bytearray()
    for index in range(0, len(packets_188), 188):
        wrapped.extend(b'\x00\x00\x00\x00')
        wrapped.extend(packets_188[index:index + 188])
    return bytes(wrapped)


def test_find_stream_info_with_prefix_matches_clean(tmp_path: Path) -> None:
    """188/192-byte TS に prefix を付けても PAT/PMT 解析結果が無 prefix と同じになること。"""

    video_pid = 0x0100
    body_188 = (
        _psi_packet(0x0000, _pat([(1, 0x1000)])) +
        _psi_packet(0x1000, _video_pmt(video_pid=video_pid, pcr_pid=video_pid))
    ) * 4
    for packet_size, prefix_len in ((188, 187), (192, 3)):
        body = _wrap_ts_packets(body_188, packet_size)
        clean = tmp_path / f'clean-{packet_size}.ts'
        clean.write_bytes(body)
        dirty = tmp_path / f'dirty-{packet_size}-{prefix_len}.ts'
        dirty.write_bytes((b'\xAA' * prefix_len) + body)

        clean_info = TSKeyFrameSeeker.findStreamInfo(clean)
        dirty_info = TSKeyFrameSeeker.findStreamInfo(dirty)

        assert clean_info.packet_size == dirty_info.packet_size == packet_size
        assert clean_info.video_pid == dirty_info.video_pid == video_pid
        assert clean_info.pcr_pid == dirty_info.pcr_pid == video_pid
        assert clean_info.codec == dirty_info.codec == 'H.264'
        assert clean_info.stream_start_offset == 0
        assert dirty_info.stream_start_offset == prefix_len
