"""録画予約一覧の録画中判定が EDCB への同時接続数を制限することを計測して検証する。

監査 F-22 / 改修ブロック R-16 の判断記録を兼ねる。
CtrlCmd プロトコルは 1 コマンドごとに EpgTimerSrv へ新規接続を開く (CtrlCmdUtil.__sendAndReceive) ため、
録画中判定 (sendGetRecFilePath) を無制限に並列実行すると、
判定対象予約数 × 同時リクエスト数と同数の同時接続が EpgTimerSrv へ発生する。

本テストは次の3つを検証する。
1. 判定対象 32 件で同時接続数が EDCB_RECORDING_CHECK_CONCURRENCY_LIMIT (8) に制限されること
2. 予約一覧リクエストが 2 件同時に重なっても、プロセス共有のセマフォにより合計の同時接続数が 8 を超えないこと
3. 実際の CtrlCmdUtil と loopback 上の偽 EpgTimerSrv (CtrlCmd TCP フレーミング準拠) を使い、
   予約件数別に、無制限時と上限 8 適用時の同時接続数・応答時間を比較計測する

なお 3. の計測は実機の EpgTimerSrv ではなく loopback TCP サーバーへの接続で行っている。
接続確立・フレーミング・切断の実コードパス (CtrlCmdUtil.__sendAndReceive) は本物だが、
EpgTimerSrv 本体の処理負荷は再現していない点に注意が必要。

回帰テスト内の計測結果 (loopback, 1 コマンド 20ms の遅延, 2026-08-09 実測):
- 予約 8 件: 無制限時 同時接続 8 / 24ms、上限 8 適用時 同時接続 8 / 23ms (上限以下では性能劣化なし)
- 予約 32 件: 無制限時 同時接続 32 / 32ms、上限 8 適用時 同時接続 8 / 93ms
- 予約 64 件: 無制限時 同時接続 64 / 42ms、上限 8 適用時 同時接続 8 / 186ms
この値は並列数の回帰検証用であり、実機 EpgTimerSrv の性能判断には使用しない。

構成済みの実機 EpgTimerSrv に対する手動計測結果 (2026-08-09 実測、各条件 1 回):
- 予約 8 件: 無制限時 同時接続 8 / 2.71ms、上限 8 適用時 同時接続 8 / 2.06ms
- 予約 32 件: 無制限時 同時接続 32 / 7.75ms、上限 8 適用時 同時接続 8 / 8.21ms
- 予約 64 件: 無制限時 同時接続 64 / 14.24ms、上限 8 適用時 同時接続 8 / 16.32ms
実予約への NWPlay セッション生成を避けるため、存在しない予約 ID への sendGetRecFilePath を使用した。
CtrlCmdUtil の asyncio.open_connection を計測用 wrapper で包み、接続確立から writer.close() までの実接続数と
全問い合わせの完了時間を計測している。録画中予約で成功応答を受けた場合に続く NWPLAY_CLOSE は含まない。
上限 8 は実機でも予約 8 件以下の応答時間を劣化させず、64 件時の追加遅延を約 2ms に抑えながら、
同時接続数だけを 8 へ固定できたため採用した。
"""

import asyncio
import time
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest

from app import config as config_module
from app.constants import JST
from app.routers import ReservationsRouter
from app.routers.ReservationsRouter import (
    EDCB_RECORDING_CHECK_CONCURRENCY_LIMIT,
    GetIsRecordingInProgress,
    GetIsRecordingInProgressByReserveId,
)
from app.utils.edcb import RecSettingDataRequired, ReserveDataRequired
from app.utils.edcb.CtrlCmdUtil import CtrlCmdUtil


# CtrlCmdUtil のコンストラクタが Config() を参照するため、安全な既定値を先に設定する
if config_module._CONFIG is None:
    config_module._CONFIG = config_module.HostServerSettings().toServerSettings(bypass_validation=True)


@pytest.fixture(autouse=True)
def ResetEDCBRecordingCheckSemaphore() -> Iterator[None]:
    """プロセス共有セマフォが特定テストのイベントループに束縛されたまま残らないよう、各テスト後に再生成させる。"""

    yield
    ReservationsRouter._edcb_recording_check_semaphore = None


class _ConcurrencyTrackingCtrlCmdUtil:
    """sendGetRecFilePath の同時実行数を計測する EDCB クライアントスタブ。"""

    def __init__(self) -> None:
        # EDCB への問い合わせが行われた回数
        self.call_count = 0
        # 現在実行中の sendGetRecFilePath 数
        self.current_concurrency = 0
        # 観測された同時実行数の最大値 (= EDCB への同時接続数の最大値に相当)
        self.max_concurrency = 0

    async def sendGetRecFilePath(self, reserve_id: int) -> str:
        # 計測のため、わざと await を挟んで並列実行を交錯させる
        self.call_count += 1
        self.current_concurrency += 1
        self.max_concurrency = max(self.max_concurrency, self.current_concurrency)
        await asyncio.sleep(0.01)
        self.current_concurrency -= 1
        return f'/recorded/{reserve_id}.ts'


def _MakeReserveData(reserve_id: int, start_time: datetime, rec_mode: int = 1) -> ReserveDataRequired:
    """指定した開始時刻・録画モードの予約情報を生成する。"""

    return ReserveDataRequired(
        title = f'テスト番組 {reserve_id}',
        start_time = start_time,
        duration_second = 1800,
        station_name = 'テスト局',
        onid = 1,
        tsid = 1,
        sid = 1,
        eid = reserve_id,
        comment = '',
        reserve_id = reserve_id,
        overlap_mode = 0,
        start_time_epg = start_time,
        rec_setting = RecSettingDataRequired(
            rec_mode = rec_mode,
            priority = 2,
            tuijyuu_flag = False,
            service_mode = 0,
            pittari_flag = False,
            bat_file_path = '',
            rec_folder_list = [],
            suspend_mode = 0,
            reboot_flag = False,
            continue_rec_flag = False,
            partial_rec_flag = 0,
            tuner_id = 0,
            partial_rec_folder = [],
        ),
        rec_file_name_list = [],
    )


def _MakeInWindowReserves(count: int, reserve_id_offset: int = 0) -> list[ReserveDataRequired]:
    """現在時刻の 30 分後開始 (= すべて録画中判定の対象になる) の予約を count 件生成する。"""

    start_time = datetime.now(tz=JST) + timedelta(minutes=30)
    return [
        _MakeReserveData(reserve_id_offset + reserve_id, start_time)
        for reserve_id in range(count)
    ]


def test_recording_check_concurrency_is_bounded() -> None:
    """判定対象の予約が上限 (8) を大きく超える 32 件あっても、EDCB への同時接続数は上限に制限される。"""

    reserve_data_list = _MakeInWindowReserves(32)
    edcb = _ConcurrencyTrackingCtrlCmdUtil()

    result = asyncio.run(GetIsRecordingInProgressByReserveId(reserve_data_list, edcb))  # type: ignore[arg-type]

    # 全予約が録画中 (ファイルパスが返る) と判定される
    assert result == {reserve_id: True for reserve_id in range(32)}
    # 判定対象の全予約について EDCB へ問い合わせが行われる
    assert edcb.call_count == 32
    # 同時接続数は上限まで並列化されるが、上限を超えない (制限なしでは 32 に達する)
    assert edcb.max_concurrency == EDCB_RECORDING_CHECK_CONCURRENCY_LIMIT


def test_recording_check_concurrency_is_bounded_across_concurrent_requests() -> None:
    """予約一覧リクエストが 2 件同時に重なっても、EDCB への合計同時接続数は上限 (8) を超えない。"""

    # 予約一覧 API の同時リクエスト 2 件を再現するため、32 件ずつの予約に対する判定を 2 系統同時に実行する
    ## リクエストごとにセマフォを生成していた場合、合計の同時接続数は最大 16 (8 × 2) に達する
    reserve_data_list_a = _MakeInWindowReserves(32)
    reserve_data_list_b = _MakeInWindowReserves(32, reserve_id_offset=1000)
    edcb = _ConcurrencyTrackingCtrlCmdUtil()

    async def RunConcurrentRequests() -> None:
        await asyncio.gather(
            GetIsRecordingInProgressByReserveId(reserve_data_list_a, edcb),  # type: ignore[arg-type]
            GetIsRecordingInProgressByReserveId(reserve_data_list_b, edcb),  # type: ignore[arg-type]
        )

    asyncio.run(RunConcurrentRequests())

    # 2 リクエスト分の全予約について EDCB へ問い合わせが行われる
    assert edcb.call_count == 64
    # プロセス共有のセマフォにより、リクエストをまたいだ合計の同時接続数も上限を超えない
    assert edcb.max_concurrency == EDCB_RECORDING_CHECK_CONCURRENCY_LIMIT


def test_recording_check_skips_reserves_outside_check_window() -> None:
    """判定時間範囲外・視聴予約の予約では EDCB への問い合わせ自体が行われない。"""

    current_time = datetime.now(tz=JST)
    reserve_data_list = [
        # 3 時間後開始の予約は判定時間範囲 (現在時刻 ±2 時間) 外
        _MakeReserveData(1, current_time + timedelta(hours=3)),
        # 視聴予約 (rec_mode=4) は録画ファイルパスが存在しないため判定対象外
        _MakeReserveData(2, current_time + timedelta(minutes=30), rec_mode=4),
        # 無効予約 (rec_mode=5) も判定対象外
        _MakeReserveData(3, current_time + timedelta(minutes=30), rec_mode=5),
    ]
    edcb = _ConcurrencyTrackingCtrlCmdUtil()

    result = asyncio.run(GetIsRecordingInProgressByReserveId(reserve_data_list, edcb))  # type: ignore[arg-type]

    # すべて録画中でないと判定され、EDCB への問い合わせは一度も発生しない
    assert result == {1: False, 2: False, 3: False}
    assert edcb.call_count == 0


class _FakeEpgTimerSrv:
    """CtrlCmd の TCP フレーミングだけを実装した計測用の偽 EpgTimerSrv。

    1 接続 = 1 コマンドの CtrlCmd プロトコルに従い、リクエストを読み切ってから
    response_delay 秒だけ処理を遅延し、失敗応答 (ret != __CMD_SUCCESS) を返す。
    接続の同時数を計測することで、EDCB への同時接続数を直接観測できる。
    """

    def __init__(self, response_delay: float) -> None:
        # 1 コマンドあたりの擬似的な処理時間 (秒)
        self.response_delay = response_delay
        # 受け付けた接続 (= コマンド) の総数
        self.connection_count = 0
        # 現在処理中の接続数
        self.current_connections = 0
        # 観測された同時接続数の最大値
        self.max_concurrent_connections = 0
        self._server: asyncio.AbstractServer | None = None

    @property
    def port(self) -> int:
        """listen 中のポート番号を返す。"""

        assert self._server is not None and self._server.sockets is not None
        return int(self._server.sockets[0].getsockname()[1])

    async def Start(self) -> None:
        """loopback で listen を開始する。"""

        self._server = await asyncio.start_server(self._HandleConnection, host='127.0.0.1', port=0)

    async def Stop(self) -> None:
        """listen を停止する。"""

        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _HandleConnection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """リクエストのフレーミング (int32 cmd + int32 size + payload) を読み切り、遅延後に失敗応答を返す。"""

        self.connection_count += 1
        self.current_connections += 1
        self.max_concurrent_connections = max(self.max_concurrent_connections, self.current_connections)
        try:
            header = await reader.readexactly(8)
            request_size = int.from_bytes(header[4:8], byteorder='little', signed=True)
            if request_size > 0:
                await reader.readexactly(request_size)
            # EpgTimerSrv のコマンド処理に相当する遅延
            await asyncio.sleep(self.response_delay)
            # ret=0 (成功を示す __CMD_SUCCESS=1 以外) / size=0 の失敗応答を返す
            ## sendGetRecFilePath は None (= 録画中でない) として扱うが、接続レベルの計測には影響しない
            writer.write((0).to_bytes(4, byteorder='little', signed=True))
            writer.write((0).to_bytes(4, byteorder='little', signed=True))
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.current_connections -= 1
            writer.close()


async def _MeasureEDCBConnections(
    reserve_count: int,
    response_delay: float,
    bounded: bool,
) -> tuple[int, float]:
    """
    偽 EpgTimerSrv に対して録画中判定を実行し、同時接続数の最大値と所要時間を計測する。

    Args:
        reserve_count (int): 判定対象の予約数
        response_delay (float): 偽 EpgTimerSrv の 1 コマンドあたりの処理時間 (秒)
        bounded (bool): True なら上限適用後のヘルパー経由、False なら上限なし (修正前相当) の直接 gather

    Returns:
        tuple[int, float]: (観測された同時接続数の最大値, 全体の所要時間 [秒])
    """

    server = _FakeEpgTimerSrv(response_delay)
    await server.Start()
    try:
        # 実際の CtrlCmdUtil を偽 EpgTimerSrv へ向ける (TCP 接続・フレーミングは本物のコードパスを通る)
        edcb = CtrlCmdUtil()
        edcb.setNWSetting('127.0.0.1', server.port)
        edcb.setConnectTimeOutSec(5.0)
        reserve_data_list = _MakeInWindowReserves(reserve_count)

        started_at = time.monotonic()
        if bounded is True:
            # 上限適用後: プロセス共有セマフォ経由で問い合わせる
            await GetIsRecordingInProgressByReserveId(reserve_data_list, edcb)
        else:
            # 上限なし (修正前相当): セマフォを介さず全予約を直接 gather する
            await asyncio.gather(*(GetIsRecordingInProgress(reserve_data, edcb) for reserve_data in reserve_data_list))
        elapsed = time.monotonic() - started_at
        return server.max_concurrent_connections, elapsed
    finally:
        await server.Stop()


def test_recording_check_concurrency_measured_with_real_ctrl_cmd_util() -> None:
    """実際の CtrlCmdUtil で、件数別に無制限時と上限 8 適用時の同時接続数・応答時間を比較計測する。"""

    # 偽 EpgTimerSrv の 1 コマンドあたりの処理時間
    ## 実機の EpgTimerSrv の応答は通常これより速いが、並列性を確実に交錯させるため意図的に遅延させている
    response_delay = 0.02

    async def Measure() -> list[tuple[int, int, float, int, float]]:
        results = []
        for reserve_count in (8, 32, 64):
            unbounded_connections, unbounded_elapsed = await _MeasureEDCBConnections(reserve_count, response_delay, bounded=False)
            bounded_connections, bounded_elapsed = await _MeasureEDCBConnections(reserve_count, response_delay, bounded=True)
            results.append((reserve_count, unbounded_connections, unbounded_elapsed, bounded_connections, bounded_elapsed))
        return results

    results = asyncio.run(Measure())

    for reserve_count, unbounded_connections, unbounded_elapsed, bounded_connections, bounded_elapsed in results:
        # 計測結果はレビューしやすいようログに残す
        print(
            f'[F-22 計測] 予約 {reserve_count} 件 / 1 コマンド {response_delay * 1000:.0f}ms: '
            f'無制限時は同時接続 {unbounded_connections}・{unbounded_elapsed * 1000:.0f}ms、'
            f'上限 {EDCB_RECORDING_CHECK_CONCURRENCY_LIMIT} 適用時は同時接続 {bounded_connections}・{bounded_elapsed * 1000:.0f}ms',
        )
        # 上限なし (修正前相当) では、同時接続数が予約数に比例して上限を超える
        assert unbounded_connections == reserve_count
        assert unbounded_connections > EDCB_RECORDING_CHECK_CONCURRENCY_LIMIT or reserve_count <= EDCB_RECORDING_CHECK_CONCURRENCY_LIMIT
        # 上限適用後は、同時接続数が上限を超えない
        assert bounded_connections <= EDCB_RECORDING_CHECK_CONCURRENCY_LIMIT
        # 上限適用後は直列化分だけ所要時間が伸びる (少なくとも 予約数 / 上限 回の往復が必要)
        assert bounded_elapsed >= (reserve_count // EDCB_RECORDING_CHECK_CONCURRENCY_LIMIT) * response_delay
