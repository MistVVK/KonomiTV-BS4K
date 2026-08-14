import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.streams.RecordedSubtitleStream as recorded_subtitle_stream_module
from app.models.RecordedVideo import RecordedVideo
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


def test_mmt_tlv_arib_ttml_packets_are_indexed_by_ffprobe(monkeypatch) -> None:
    """MMT/TLV の raw MFU timed ID3 を stream index 経由で取得し、v2 source PTS を保持する。"""

    _packet, raw_id3 = _make_ttml_pes_packet(
        0x0130,
        0x30,
        100 * 90_000,
        envelope_version=2,
    )
    probe = {
        'format': {'start_time': '90.0'},
        'packets': [
            {
                'stream_index': 8,
                'pts_time': '99.0',
                'data': '00000000: ' + raw_id3.hex(),
            },
            {
                'stream_index': 3,
                'pts_time': '100.0',
                'data': '00000000: ' + raw_id3.hex(),
            },
        ],
    }
    commands: list[tuple[object, ...]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            import json
            return json.dumps(probe).encode(), b''

    async def CreateSubprocess(*command: object, **_kwargs: object) -> FakeProcess:
        commands.append(command)
        return FakeProcess()

    monkeypatch.setattr(recorded_subtitle_stream_module.asyncio, 'create_subprocess_exec', CreateSubprocess)
    stream = RecordedSubtitleStream(SimpleNamespace(
        id=1,
        file_path='/recording.tlv',
        container_format='MMT/TLV',
        subtitle_tracks=[{
            'index': 1,
            'stream_index': 3,
            'codec': 'arib_ttml',
            'language': 'jpn',
            'title': None,
            'component_tag': 0x30,
        }],
    ))

    packets = asyncio.run(
        stream._RecordedSubtitleStream__extractARIBTTMLPacketsFromMMTTLV()  # pyright: ignore[reportPrivateUsage]
    )

    assert '-f' in commands[0]
    assert commands[0][commands[0].index('-f') + 1] == 'libaribtlv'
    assert packets == [{
        'pts': 10.0,
        'transport_timestamp': 100.0,
        'component_tag': 0x30,
        'data': base64.b64encode(raw_id3).decode(),
        'is_restore_point': False,
    }]


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


def test_orphan_cleanup_preserves_reachable_active_and_third_party_caches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reachable・active lockを保持し、deleted hash・旧version・中断tmpだけを削除する。"""

    file_hash = 'a' * 32
    deleted_hash = 'b' * 32
    reachable = tmp_path / f'v3-{file_hash}-1-7.vtt'
    reachable_ttml = tmp_path / f'ttml-v3-{file_hash}-7-101.json'
    old_cache_version = tmp_path / f'v2-{file_hash}-1-7.vtt'
    old_index_version = tmp_path / f'v3-{file_hash}-1-6.vtt'
    deleted_cache = tmp_path / f'v3-{deleted_hash}-1-7.vtt'
    active_cache = tmp_path / f'v3-{deleted_hash}-2-7.arib.json'
    interrupted_temporary = tmp_path / f'v3-{deleted_hash}-3-7.vtt.tmp'
    third_party = tmp_path / f'copy-v3-{deleted_hash}-1-7.vtt'
    managed_name_symlink = tmp_path / f'v3-{deleted_hash}-4-7.vtt'
    symlink_target = tmp_path / 'third-party-target'
    for path in (
        reachable,
        reachable_ttml,
        old_cache_version,
        old_index_version,
        deleted_cache,
        active_cache,
        interrupted_temporary,
        third_party,
        symlink_target,
    ):
        path.write_bytes(b'cache')
    managed_name_symlink.symlink_to(symlink_target)

    class RecordedVideoQuery:
        async def values(self, *_fields: str) -> list[dict[str, object]]:
            return [{
                'file_hash': file_hash,
                'playback_index_version': 7,
                'subtitle_tracks': [
                    {'index': 1, 'stream_index': 3, 'codec': 'subrip'},
                    {'index': 2, 'stream_index': 4, 'codec': 'arib_caption'},
                    {'index': 3, 'codec': 'arib_ttml', 'program_number': 101},
                ],
            }]

    async def RunSynchronously(function, *args, **kwargs):  # type: ignore[no-untyped-def]
        """終了不能なhost ThreadPoolExecutorを避け、GCの同期I/O本体だけを検証する。"""

        return function(*args, **kwargs)

    monkeypatch.setattr(recorded_subtitle_stream_module, 'RECORDED_SUBTITLES_DIR', tmp_path)
    monkeypatch.setattr(recorded_subtitle_stream_module.asyncio, 'to_thread', RunSynchronously)
    monkeypatch.setattr(RecordedVideo, 'all', classmethod(lambda _cls: RecordedVideoQuery()))

    async def Run() -> None:
        # 削除対象TTMLがメモリLRUにも残っている状態を作り、disk削除と同時に除外されることを確認する。
        orphan_ttml = tmp_path / f'ttml-v3-{deleted_hash}-7-all.json'
        orphan_ttml.write_text('[]')
        RecordedSubtitleStream._RecordedSubtitleStream__rememberARIBTTMLPacketIndex(  # pyright: ignore[reportPrivateUsage]
            orphan_ttml,
            [],
        )
        async with RecordedSubtitleStream._RecordedSubtitleStream__cacheLock(active_cache):  # pyright: ignore[reportPrivateUsage]
            await RecordedSubtitleStream.cleanupOrphanedCaches()
            assert active_cache.is_file() is True
        await RecordedSubtitleStream.cleanupOrphanedCaches()
        assert orphan_ttml not in RecordedSubtitleStream._arib_ttml_memory_cache  # pyright: ignore[reportPrivateUsage]
        assert orphan_ttml not in RecordedSubtitleStream._arib_ttml_memory_cache_bytes  # pyright: ignore[reportPrivateUsage]

    asyncio.run(Run())

    assert reachable.is_file() is True
    assert reachable_ttml.is_file() is True
    assert active_cache.exists() is False
    assert old_cache_version.exists() is False
    assert old_index_version.exists() is False
    assert deleted_cache.exists() is False
    assert interrupted_temporary.exists() is False
    assert third_party.is_file() is True
    assert managed_name_symlink.is_symlink() is True
    assert symlink_target.read_bytes() == b'cache'


def test_orphan_cleanup_is_fail_closed_when_db_enumeration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DBからreachable集合を確定できない場合は管理形式のファイルも削除しない。"""

    cache_path = tmp_path / f'v3-{"a" * 32}-1-7.vtt'
    cache_path.write_bytes(b'cache')

    class FailingRecordedVideoQuery:
        async def values(self, *_fields: str) -> list[dict[str, object]]:
            raise RuntimeError('simulated database failure')

    async def RunSynchronously(function, *args, **kwargs):  # type: ignore[no-untyped-def]
        """終了不能なhost ThreadPoolExecutorを避け、GCの同期I/O本体だけを検証する。"""

        return function(*args, **kwargs)

    monkeypatch.setattr(recorded_subtitle_stream_module, 'RECORDED_SUBTITLES_DIR', tmp_path)
    monkeypatch.setattr(recorded_subtitle_stream_module.asyncio, 'to_thread', RunSynchronously)
    monkeypatch.setattr(RecordedVideo, 'all', classmethod(lambda _cls: FailingRecordedVideoQuery()))

    asyncio.run(RecordedSubtitleStream.cleanupOrphanedCaches())

    assert cache_path.is_file() is True


def test_orphan_cleanup_is_fail_closed_when_db_row_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1件でもreachable判定不能なDB行があれば、管理形式のorphanも削除しない。"""

    cache_path = tmp_path / f'v3-{"a" * 32}-1-7.vtt'
    cache_path.write_bytes(b'cache')

    class InvalidRecordedVideoQuery:
        async def values(self, *_fields: str) -> list[dict[str, object]]:
            return [{
                'file_hash': 'invalid-hash',
                'playback_index_version': 7,
                'subtitle_tracks': [],
            }]

    async def RunSynchronously(function, *args, **kwargs):  # type: ignore[no-untyped-def]
        """終了不能なhost ThreadPoolExecutorを避け、GCの同期I/O本体だけを検証する。"""

        return function(*args, **kwargs)

    monkeypatch.setattr(recorded_subtitle_stream_module, 'RECORDED_SUBTITLES_DIR', tmp_path)
    monkeypatch.setattr(recorded_subtitle_stream_module.asyncio, 'to_thread', RunSynchronously)
    monkeypatch.setattr(RecordedVideo, 'all', classmethod(lambda _cls: InvalidRecordedVideoQuery()))

    asyncio.run(RecordedSubtitleStream.cleanupOrphanedCaches())

    assert cache_path.is_file() is True


def test_orphan_cleanup_blocks_new_writer_until_temporary_unlink_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """active判定後に始まる生成もGCのtmp削除完了まで待機し、atomic replaceを成功させる。"""

    completed_path = tmp_path / f'v3-{"a" * 32}-1-7.vtt'
    temporary_path = completed_path.with_name(f'{completed_path.name}.tmp')
    temporary_path.write_bytes(b'interrupted')

    class RecordedVideoQuery:
        async def values(self, *_fields: str) -> list[dict[str, object]]:
            return []

    unlink_started = asyncio.Event()
    allow_unlink = asyncio.Event()

    async def RunControlled(function, *args, **kwargs):  # type: ignore[no-untyped-def]
        """GCのunlinkだけを停止し、active判定後の生成参入順序を決定的に作る。"""

        if getattr(function, '__self__', None) == temporary_path and getattr(function, '__name__', '') == 'unlink':
            unlink_started.set()
            await allow_unlink.wait()
        return function(*args, **kwargs)

    monkeypatch.setattr(recorded_subtitle_stream_module, 'RECORDED_SUBTITLES_DIR', tmp_path)
    monkeypatch.setattr(recorded_subtitle_stream_module.asyncio, 'to_thread', RunControlled)
    monkeypatch.setattr(RecordedVideo, 'all', classmethod(lambda _cls: RecordedVideoQuery()))

    async def Run() -> None:
        writer_entered = asyncio.Event()

        async def GenerateCache() -> None:
            async with RecordedSubtitleStream._RecordedSubtitleStream__cacheLock(  # pyright: ignore[reportPrivateUsage]
                completed_path,
            ):
                writer_entered.set()
                temporary_path.write_bytes(b'generated')
                temporary_path.replace(completed_path)

        cleanup_task = asyncio.create_task(RecordedSubtitleStream.cleanupOrphanedCaches())
        await unlink_started.wait()
        writer_task = asyncio.create_task(GenerateCache())
        await asyncio.sleep(0)

        # GCが同じpathのlockを保持している間は、新規生成をtmpへ進めてはならない。
        assert writer_entered.is_set() is False
        allow_unlink.set()
        await asyncio.wait_for(cleanup_task, timeout=1)
        await asyncio.wait_for(writer_task, timeout=1)

    asyncio.run(Run())

    assert completed_path.read_bytes() == b'generated'
    assert temporary_path.exists() is False
    assert completed_path not in RecordedSubtitleStream._locks  # pyright: ignore[reportPrivateUsage]
