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
from app.utils.KonomiTVBS4KTLVServiceResolver import KonomiTVBS4KTLVServiceResolver
from app.utils.KonomiTVBS4KTLVStreamPump import KonomiTVBS4KTLVStreamPump


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


def test_service_resolver_parses_metadata_line() -> None:
    """ストリーミング JSON 行からサービスと映像トラックの context_id を抽出する。"""

    parse = KonomiTVBS4KTLVServiceResolver.parseMetadataLine

    assert parse(
        '{"services":[{"service_id":101,"context_id":1},{"service_id":103,"context_id":2}],'
        '"tracks":[{"context_id":1,"kind":"Audio"},{"context_id":2,"kind":"Video"}]}'
    ) == ({101: 1, 103: 2}, {2})
    # 主/降雨対応以外のサービスや追加フィールドは無視しない (service_id さえあれば拾う)。
    assert parse(
        '{"services":[{"context_id":1,"service_id":101,"service_name":"NHK BS4K"}],"tracks":[]}'
    ) == ({101: 1}, set())


def test_service_resolver_ignores_malformed_metadata_line() -> None:
    """不正な JSON・shape は None、bool 混入項目は除外して返す。"""

    parse = KonomiTVBS4KTLVServiceResolver.parseMetadataLine

    assert parse('not-a-json') is None
    assert parse('{"events":[]}') is None
    assert parse('{"services":"not-a-list","tracks":[]}') is None
    # bool は int のサブクラスなので、service_id/context_id の bool は数値として扱わない。
    assert parse('{"services":[{"service_id":true,"context_id":1}],"tracks":[]}') == ({}, set())
    assert parse('{"services":[{"service_id":101,"context_id":true}],"tracks":[]}') == ({}, set())
    assert parse('{"services":[],"tracks":[{"context_id":true,"kind":"Video"}]}') == ({}, set())


class _FakeTLVMetadataStreamWriter:
    """resolve のテスト用に、書き込んだ chunk を保持するだけの fake StreamWriter。"""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        # 実運用でも pipe に余裕があれば即時完了するため、resolve 側が明示的に reader へ処理機会を渡す。
        return None

    def close(self) -> None:
        self.closed = True


class _FakeTLVMetadataProcess:
    """resolve のテスト用の fake subprocess。stdout は実 StreamReader を使う。"""

    def __init__(self, stdout_reader: asyncio.StreamReader) -> None:
        self.stdin = _FakeTLVMetadataStreamWriter()
        self.stdout = stdout_reader
        self.returncode: int | None = None
        self.killed = False
        self.wait_count = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.wait_count += 1
        return self.returncode if self.returncode is not None else 0


def test_service_resolver_resolves_context_ids_and_returns_head_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolve はストリームを読み、主/降雨対応 SID の context_id と生バイトを返し、子プロセスを必ず殺す。"""

    async def scenario() -> None:
        stdout_reader = asyncio.StreamReader()
        stdout_reader.feed_data(
            b'{"services":[{"service_id":101,"context_id":1},{"service_id":103,"context_id":2}],'
            b'"tracks":[{"context_id":1,"kind":"Video"},{"context_id":2,"kind":"Video"}]}\n'
        )
        stdout_reader.feed_eof()
        process = _FakeTLVMetadataProcess(stdout_reader)

        async def fake_exec(*_args: object, **_kwargs: object) -> _FakeTLVMetadataProcess:
            return process

        monkeypatch.setattr(
            'app.utils.KonomiTVBS4KTLVServiceResolver.asyncio.subprocess.create_subprocess_exec',
            fake_exec,
        )

        async def stream():
            yield b'head-chunk'
            yield b'tail-chunk'

        main_ctx, rain_ctx, head_buffer = await KonomiTVBS4KTLVServiceResolver.resolve(
            stream(),
            main_service_id=101,
            rain_service_id=103,
            need_rain_fallback=True,
            log_prefix='test',
        )

        assert main_ctx == 1
        assert rain_ctx == 2
        # 早期終了により 2 番目の chunk は読まれない。先頭バッファは 1 番目だけ。
        assert head_buffer == b'head-chunk'
        assert process.stdin.closed is True
        assert process.killed is True
        assert process.wait_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    'metadata_lines',
    [
        [
            b'{"services":[{"service_id":101,"context_id":1},{"service_id":103,"context_id":2}],"tracks":[]}\n',
            b'{"services":[{"service_id":101,"context_id":1},{"service_id":103,"context_id":2}],'
            b'"tracks":[{"context_id":2,"kind":"Video"}]}\n',
        ],
        [
            b'{"services":[],"tracks":[{"context_id":2,"kind":"Video"}]}\n',
            b'{"services":[{"service_id":101,"context_id":1},{"service_id":103,"context_id":2}],'
            b'"tracks":[{"context_id":2,"kind":"Video"}]}\n',
        ],
    ],
)
def test_service_resolver_accepts_service_and_track_in_either_order(
    monkeypatch: pytest.MonkeyPatch,
    metadata_lines: list[bytes],
) -> None:
    """サービスと映像トラックのどちらが先に通知されても、同じ context の確認後だけ降雨映像を採用する。"""

    async def scenario() -> None:
        stdout_reader = asyncio.StreamReader()
        for line in metadata_lines:
            stdout_reader.feed_data(line)
        stdout_reader.feed_eof()
        process = _FakeTLVMetadataProcess(stdout_reader)

        async def fake_exec(*_args: object, **_kwargs: object) -> _FakeTLVMetadataProcess:
            return process

        monkeypatch.setattr(
            'app.utils.KonomiTVBS4KTLVServiceResolver.asyncio.subprocess.create_subprocess_exec',
            fake_exec,
        )

        async def stream():
            yield b'head-chunk'

        main_ctx, rain_ctx, _ = await KonomiTVBS4KTLVServiceResolver.resolve(
            stream(),
            main_service_id=101,
            rain_service_id=103,
            need_rain_fallback=True,
            log_prefix='test',
        )

        assert main_ctx == 1
        assert rain_ctx == 2

    asyncio.run(scenario())


def test_service_resolver_cancellation_still_kills_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolve がキャンセルされても metadata 子プロセスが kill + wait で回収され、CancelledError が伝播する。"""

    async def scenario() -> None:
        # stdout に EOF を与えず、readline をブロックさせたままにする。
        stdout_reader = asyncio.StreamReader()
        process = _FakeTLVMetadataProcess(stdout_reader)

        async def fake_exec(*_args: object, **_kwargs: object) -> _FakeTLVMetadataProcess:
            return process

        monkeypatch.setattr(
            'app.utils.KonomiTVBS4KTLVServiceResolver.asyncio.subprocess.create_subprocess_exec',
            fake_exec,
        )

        async def stream():
            # キャンセルされるまで yield しないストリーム。
            await asyncio.Event().wait()
            yield b'never'

        resolve_task = asyncio.create_task(
            KonomiTVBS4KTLVServiceResolver.resolve(
                stream(),
                main_service_id=101,
                rain_service_id=103,
                need_rain_fallback=True,
                log_prefix='test',
            )
        )
        # resolve が create_subprocess_exec を済ませ、ストリーム読み待ちに入るまで譲る。
        await asyncio.sleep(0)
        resolve_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await resolve_task

        # キャンセル経路でも子プロセスが kill + wait で回収され、stdin も閉じられている。
        assert process.killed is True
        assert process.wait_count == 1
        assert process.stdin.closed is True

    asyncio.run(scenario())


def test_service_resolver_without_rain_fallback_stops_at_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """need_rain_fallback=False では主 SID 解決後に即終了し、降雨対応 SID を待たない。"""

    async def scenario() -> None:
        stdout_reader = asyncio.StreamReader()
        stdout_reader.feed_data(
            b'{"services":[{"service_id":101,"context_id":1},{"service_id":103,"context_id":2}],'
            b'"tracks":[{"context_id":2,"kind":"Video"}]}\n'
        )
        stdout_reader.feed_eof()
        process = _FakeTLVMetadataProcess(stdout_reader)

        async def fake_exec(*_args: object, **_kwargs: object) -> _FakeTLVMetadataProcess:
            return process

        monkeypatch.setattr(
            'app.utils.KonomiTVBS4KTLVServiceResolver.asyncio.subprocess.create_subprocess_exec',
            fake_exec,
        )

        async def stream():
            yield b'head-chunk'
            yield b'should-not-be-read'

        main_ctx, rain_ctx, head_buffer = await KonomiTVBS4KTLVServiceResolver.resolve(
            stream(),
            main_service_id=101,
            rain_service_id=103,
            need_rain_fallback=False,
            log_prefix='test',
        )

        assert main_ctx == 1
        assert rain_ctx is None
        # 主 SID 解決で早期終了するため、2 番目の chunk は読まれない。
        assert head_buffer == b'head-chunk'

    asyncio.run(scenario())


def test_service_resolver_rain_timeout_returns_main_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """need_rain_fallback=True でも降雨対応 SID が現れなければ短い窓で打ち切り、主 SID だけを返す。"""

    async def scenario() -> None:
        monkeypatch.setattr(KonomiTVBS4KTLVServiceResolver, 'RAIN_FALLBACK_WAIT_SECONDS', 0.01)
        stdout_reader = asyncio.StreamReader()
        # 降雨対応 SID は SDT にあるが映像トラックがない。EOF も与えず、reader は次の行を待ち続ける。
        stdout_reader.feed_data(
            b'{"services":[{"service_id":101,"context_id":1},{"service_id":103,"context_id":2}],'
            b'"tracks":[{"context_id":2,"kind":"Audio"}]}\n'
        )
        # 実入力では同じ context の別 SID が後続 SDT snapshot として届く。主 SID の解決結果は失わない。
        stdout_reader.feed_data(
            b'{"services":[{"service_id":181,"context_id":1}],'
            b'"tracks":[{"context_id":1,"kind":"Video"}]}\n'
        )
        process = _FakeTLVMetadataProcess(stdout_reader)

        async def fake_exec(*_args: object, **_kwargs: object) -> _FakeTLVMetadataProcess:
            return process

        monkeypatch.setattr(
            'app.utils.KonomiTVBS4KTLVServiceResolver.asyncio.subprocess.create_subprocess_exec',
            fake_exec,
        )

        async def stream():
            yield b'head-chunk'
            # 以後は yield せずブロックし、降雨対応 SID の待機窓がタイムアウトする。
            await asyncio.Event().wait()

        main_ctx, rain_ctx, head_buffer = await KonomiTVBS4KTLVServiceResolver.resolve(
            stream(),
            main_service_id=101,
            rain_service_id=103,
            need_rain_fallback=True,
            log_prefix='test',
        )

        assert main_ctx == 1
        assert rain_ctx is None
        assert head_buffer == b'head-chunk'

    asyncio.run(scenario())


def test_service_resolver_starts_probe_timeout_after_first_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """response header 後の first-byte 待機を metadata 解析予算へ含めない。"""

    async def scenario() -> None:
        monkeypatch.setattr(KonomiTVBS4KTLVServiceResolver, 'FIRST_BYTE_TIMEOUT_SECONDS', 0.1)
        monkeypatch.setattr(KonomiTVBS4KTLVServiceResolver, 'PROBE_TIMEOUT_SECONDS', 0.01)
        stdout_reader = asyncio.StreamReader()
        stdout_reader.feed_data(
            b'{"services":[{"service_id":101,"context_id":1}],'
            b'"tracks":[{"context_id":1,"kind":"Video"}]}\n'
        )
        stdout_reader.feed_eof()
        process = _FakeTLVMetadataProcess(stdout_reader)

        async def fake_exec(*_args: object, **_kwargs: object) -> _FakeTLVMetadataProcess:
            return process

        monkeypatch.setattr(
            'app.utils.KonomiTVBS4KTLVServiceResolver.asyncio.subprocess.create_subprocess_exec',
            fake_exec,
        )

        async def stream():
            # metadata 予算より長く待たせても、first-byte 専用予算内なら解析を開始できる。
            await asyncio.sleep(0.03)
            yield b'head-chunk'

        main_ctx, rain_ctx, head_buffer = await KonomiTVBS4KTLVServiceResolver.resolve(
            stream(),
            main_service_id=101,
            rain_service_id=None,
            need_rain_fallback=False,
            log_prefix='test',
        )

        assert main_ctx == 1
        assert rain_ctx is None
        assert head_buffer == b'head-chunk'

    asyncio.run(scenario())


class _BlockingTLVHTTPStreamReader:
    """pump 単体テスト用に、指定チャンク送信後も EOF にせず待機する fake HTTP reader。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.sent_all_chunks = asyncio.Event()
        self.release = asyncio.Event()
        self.iter_chunked_count = 0

    async def iter_chunked(self, _chunk_size: int):
        self.iter_chunked_count += 1
        for chunk in self.chunks:
            yield chunk
        self.sent_all_chunks.set()
        await self.release.wait()


def test_tlv_stream_pump_keeps_probe_data_before_live_chunks() -> None:
    """プローブ済みデータと後続 HTTP データを同じ FIFO から受信順に返す。"""

    async def scenario() -> None:
        stream_reader = _BlockingTLVHTTPStreamReader([b'live-1', b'live-2'])
        pump = KonomiTVBS4KTLVStreamPump(stream_reader, b'probe', 'test')  # type: ignore[arg-type]
        pump.start()
        await stream_reader.sent_all_chunks.wait()

        iterator = pump.iterChunks()
        assert await iterator.__anext__() == b'probe'
        assert await iterator.__anext__() == b'live-1'
        assert await iterator.__anext__() == b'live-2'
        assert stream_reader.iter_chunked_count == 1

        pump.cancel()
        await pump.wait()

    asyncio.run(scenario())


def test_tlv_stream_pump_drops_only_oldest_chunks_during_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """起動時上限を超えた場合は最新の連続窓だけを保持する。"""

    async def scenario() -> None:
        monkeypatch.setattr(KonomiTVBS4KTLVStreamPump, 'MAX_BUFFERED_CHUNKS', 3)
        stream_reader = _BlockingTLVHTTPStreamReader([b'live-1', b'live-2', b'live-3'])
        pump = KonomiTVBS4KTLVStreamPump(stream_reader, b'old-probe', 'test')  # type: ignore[arg-type]
        pump.start()
        await stream_reader.sent_all_chunks.wait()

        iterator = pump.iterChunks()
        assert await iterator.__anext__() == b'live-1'
        assert await iterator.__anext__() == b'live-2'
        assert await iterator.__anext__() == b'live-3'

        pump.cancel()
        await pump.wait()

    asyncio.run(scenario())


def test_tlv_stream_pump_applies_backpressure_without_dropping_after_encoder_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lossless 切替後は Queue 満杯でも古いデータを残し、consumer が読むまで pump を待機させる。"""

    async def scenario() -> None:
        monkeypatch.setattr(KonomiTVBS4KTLVStreamPump, 'MAX_BUFFERED_CHUNKS', 2)
        monkeypatch.setattr(KonomiTVBS4KTLVStreamPump, 'CHUNK_SIZE', 7)
        stream_reader = _BlockingTLVHTTPStreamReader([b'live'])
        # initial_data を2分割し、HTTP 読取開始前から Queue が満杯の状態を作る。
        pump = KonomiTVBS4KTLVStreamPump(stream_reader, b'probe-1probe-2', 'test')  # type: ignore[arg-type]
        pump.switchToLosslessMode()
        pump.start()
        await asyncio.sleep(0)

        iterator = pump.iterChunks()
        assert await iterator.__anext__() == b'probe-1'
        assert await iterator.__anext__() == b'probe-2'
        assert await iterator.__anext__() == b'live'

        pump.cancel()
        await pump.wait()

    asyncio.run(scenario())
