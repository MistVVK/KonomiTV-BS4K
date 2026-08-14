from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.routers.LiveStreamsRouter import LivePSIArchivedDataAPI
from app.streams.LiveEncodingTask import LiveEncodingTask
from app.utils.KonomiTVBS4KMMTTLV import (
    MMT_TLV_CONTAINER_FORMAT,
    BuildKonomiTVBS4KMMTTLVInputArguments,
    KonomiTVBS4KTLVSyncError,
    KonomiTVBS4KTLVSynchronizer,
)


def _BuildTLVPacket(payload: bytes, packet_type: int = 0x01) -> bytes:
    """テスト用の TLV packet を組み立てる。"""

    return b'\x7f' + bytes([packet_type]) + len(payload).to_bytes(2, 'big') + payload


def _BuildSynchronizationRun() -> bytes:
    """同期確定に必要な 4 個の TLV packet を返す。"""

    return b''.join(
        _BuildTLVPacket(bytes([index]) * (index + 1), packet_type)
        for index, packet_type in enumerate((0x01, 0x02, 0x03, 0xFE))
    )


def test_tlv_synchronizer_discards_arbitrary_prefix_and_emits_complete_packets() -> None:
    """任意 prefix の後ろにある 4 packet 連続境界から出力を開始する。"""

    synchronizer = KonomiTVBS4KTLVSynchronizer()
    packets = _BuildSynchronizationRun()

    assert synchronizer.feed(b'not-a-tlv-prefix' + packets) == packets
    assert synchronizer.synchronized is True


def test_tlv_synchronizer_accepts_input_starting_mid_packet() -> None:
    """先頭が TLV packet 途中でも、次の完全な連続境界へ同期する。"""

    synchronizer = KonomiTVBS4KTLVSynchronizer()
    partial_packet = _BuildTLVPacket(b'partial-packet')[5:]
    packets = _BuildSynchronizationRun()

    assert synchronizer.feed(partial_packet + packets) == packets


def test_tlv_synchronizer_preserves_packets_split_across_reads() -> None:
    """header・payload をまたぐ読み取り分割でも完全 packet だけを出力する。"""

    synchronizer = KonomiTVBS4KTLVSynchronizer()
    packets = _BuildSynchronizationRun()

    assert synchronizer.feed(packets[:3]) == b''
    assert synchronizer.feed(packets[3:11]) == b''
    assert synchronizer.feed(packets[11:]) == packets

    trailing_packet = _BuildTLVPacket(b'trailing', 0xFF)
    assert synchronizer.feed(trailing_packet[:6]) == b''
    assert synchronizer.feed(trailing_packet[6:]) == trailing_packet


def test_tlv_synchronizer_resynchronizes_after_corruption() -> None:
    """同期後の破損 byte を捨て、次の 4 packet 連続境界から復帰する。"""

    synchronizer = KonomiTVBS4KTLVSynchronizer()
    first_packets = _BuildSynchronizationRun()
    second_packets = _BuildSynchronizationRun()

    assert synchronizer.feed(first_packets) == first_packets
    assert synchronizer.feed(b'corrupted-data' + second_packets) == second_packets
    assert synchronizer.synchronized is True


def test_tlv_synchronizer_rejects_mpeg_ts_input() -> None:
    """TLV 選択時に MPEG-TS が届いた場合は明示的に拒否する。"""

    synchronizer = KonomiTVBS4KTLVSynchronizer()
    mpeg_ts = (b'\x47' + (b'\x00' * 187)) * 4

    with pytest.raises(KonomiTVBS4KTLVSyncError, match='MPEG-TS input'):
        synchronizer.feed(mpeg_ts)


def test_tlv_synchronizer_rejects_unsynchronized_input_over_one_mibibyte() -> None:
    """1 MiB を超えても TLV 境界がなければ入力を拒否する。"""

    synchronizer = KonomiTVBS4KTLVSynchronizer()

    assert synchronizer.feed(b'\x00' * synchronizer.MAX_UNSYNCHRONIZED_BUFFER_SIZE) == b''
    with pytest.raises(KonomiTVBS4KTLVSyncError, match='1 MiB'):
        synchronizer.feed(b'\x00')


def test_mmt_tlv_input_arguments_are_only_added_for_mmt_tlv() -> None:
    """libaribtlv demuxer の強制指定を MMT/TLV 録画だけへ限定する。"""

    assert BuildKonomiTVBS4KMMTTLVInputArguments(MMT_TLV_CONTAINER_FORMAT) == ['-f', 'libaribtlv']
    assert BuildKonomiTVBS4KMMTTLVInputArguments('MPEG-TS') == []


def test_tlv_live_uses_raw_channel_stream_endpoint() -> None:
    """Mirakurun service の channel 情報から decode=0 の Channel Stream API を組み立てる。"""

    endpoint = LiveEncodingTask.ResolveKonomiTVBS4KTLVChannelStreamEndpoint({
        'channel': {'type': 'BS4K', 'channel': '45330'},
    })

    assert endpoint == '/api/channels/BS4K/45330/stream?decode=0'


def test_tlv_live_rejects_missing_or_unsafe_channel_information() -> None:
    """channel 情報欠落を拒否し、path として使う値は URL encode する。"""

    assert LiveEncodingTask.ResolveKonomiTVBS4KTLVChannelStreamEndpoint({}) is None
    assert LiveEncodingTask.ResolveKonomiTVBS4KTLVChannelStreamEndpoint({
        'channel': {'type': 'BS4K/test', 'channel': '../45330'},
    }) == '/api/channels/BS4K%2Ftest/..%2F45330/stream?decode=0'


def test_tlv_live_psi_archived_data_returns_empty_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """PSI/SI アーカイバーを使えない TLV では待機・500 応答を発生させない。"""

    monkeypatch.setattr(
        'app.routers.LiveStreamsRouter.Config',
        lambda: SimpleNamespace(general=SimpleNamespace(konomitv_bs4k_live_transport='Tlv')),
    )

    response = asyncio.run(LivePSIArchivedDataAPI(None, 'bs4k181', None))  # type: ignore[arg-type]

    assert response.status_code == 200
    assert response.body == b''
