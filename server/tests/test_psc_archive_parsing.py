"""PSI/SI 書庫 (.psc) 解析の失敗分類とフォールバック契約を検証するテスト。"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app import schemas
from app.constants import JST
from app.metadata.TSInfoAnalyzer import TSInfoAnalyzer
from app.routers.VideosRouter import ExtractTOTTimeListFromPSCArchive


def _BuildPSCBlock(sections: list[tuple[int, bytes]], base_time_ticks: int = 0) -> bytes:
    """テスト用の最小 PSI/SI 書庫ブロックを構築する。

    Args:
        sections: (PID, セクションデータ) のリスト。全エントリを新規辞書として書き出す
        base_time_ticks: セクションへ付与する時刻の基準値 (1/11250 秒単位)

    Returns:
        bytes: 書庫ブロックのバイナリ
    """

    dictionary_len = len(sections)
    dictionary_data_size = sum(2 + len(section) for _, section in sections)

    # ヘッダ (32 バイト)
    header = bytearray(32)
    header[0:8] = b'Pssc\x0d\x0a\x9a\x0a'
    # time_list_len = 2 (基準時刻エントリ + コード参照エントリ)
    header[10:12] = (2).to_bytes(2, 'little')
    header[12:14] = dictionary_len.to_bytes(2, 'little')
    header[14:16] = dictionary_len.to_bytes(2, 'little')  # dictionary_window_len
    header[16:20] = dictionary_data_size.to_bytes(4, 'little')
    header[20:24] = dictionary_data_size.to_bytes(4, 'little')  # dictionary_buff_size
    header[24:28] = (0).to_bytes(4, 'little')  # code_list_len

    # time_list + 辞書エントリ
    time_buf = bytearray()
    time_buf += (0x80000000 | base_time_ticks).to_bytes(4, 'little')  # 基準時刻
    time_buf += (0).to_bytes(2, 'little')  # 時刻差分
    time_buf += (len(sections) - 1).to_bytes(2, 'little')  # コード数 - 1
    for _, section in sections:
        time_buf += (len(section) - 1).to_bytes(2, 'little')  # セクションサイズ - 1

    # PID とセクションデータ
    dictionary_data = bytearray()
    for pid, section in sections:
        dictionary_data += pid.to_bytes(2, 'little')
        dictionary_data += section
    # dictionary_data_size が奇数の場合は 1 バイトのパディングが読み飛ばされる
    if dictionary_data_size % 2 == 1:
        dictionary_data += b'\x00'

    # コード参照 (全辞書エントリを順に参照)
    codes = bytearray()
    for index in range(len(sections)):
        codes += (4096 + index).to_bytes(2, 'little')

    # トレイラー
    trailer_size = 4 - (dictionary_len * 2 + (dictionary_data_size + 1) // 2 * 2 + 0) % 4

    return bytes(header + time_buf + dictionary_data + codes + b'\x00' * trailer_size)


def _BuildTOTSection() -> bytes:
    """ariblib がパース可能な最小の TOT セクション (2026-08-09 12:34:56 JST) を構築する。"""

    # MJD の計算 (ARIB-STD-B10 第2部付録C の逆変換)
    year, month, day = 2026, 8, 9
    if month <= 2:
        year -= 1
        month += 12
    mjd = int(365.25 * year) + int(year / 400) - int(year / 100) + int(30.59 * (month - 2)) + day - 678912

    section = bytearray()
    section.append(0x73)  # table_id
    section += bytes([0x30, 0x0B])  # section_syntax_indicator=0 / section_length=11
    section += mjd.to_bytes(2, 'big')
    section += bytes([0x12, 0x34, 0x56])  # BCD 時刻
    section += bytes([0xF0, 0x00])  # reserved + descriptors_loop_length=0
    section += b'\x00' * 4  # CRC_32 (ariblib は CRC を検証しない)
    return bytes(section)


def _CaptureLogs(monkeypatch: pytest.MonkeyPatch, target: str) -> dict[str, list[str]]:
    """指定モジュールの logging スタブを仕込み、レベルごとのメッセージを記録する。"""

    logs: dict[str, list[str]] = {'warning': [], 'debug': [], 'error': []}
    monkeypatch.setattr(
        target,
        SimpleNamespace(
            warning=lambda message, exc_info=None: logs['warning'].append(str(message)),
            debug=lambda message, exc_info=None: logs['debug'].append(str(message)),
            error=lambda message, exc_info=None: logs['error'].append(str(message)),
        ),
    )
    return logs


def _BuildRecordedVideo(file_path: Path) -> schemas.RecordedVideo:
    """非 MPEG-TS 録画 (.psc 解析パス) を指す最小の RecordedVideo スキーマを構築する。"""

    now = datetime(2026, 8, 9, 12, 0, 0, tzinfo=JST)
    return schemas.RecordedVideo(
        id = -1,
        status = 'Recorded',
        file_path = str(file_path),
        file_hash = 'test-hash',
        file_size = 0,
        file_created_at = now,
        file_modified_at = now,
        recording_start_time = None,
        recording_end_time = None,
        duration = 60.0,
        container_format = 'MPEG-4',
        video_codec = 'h264',
        video_codec_profile = 'High',
        video_scan_type = 'Progressive',
        video_frame_rate = 29.97,
        video_resolution_width = 1920,
        video_resolution_height = 1080,
        primary_audio_codec = 'aac',
        primary_audio_channel = 'Stereo',
        primary_audio_sampling_rate = 48000,
        created_at = now,
        updated_at = now,
    )


class TestReadPSIData:
    """TSInfoAnalyzer.readPSIData() の読み取り結果分類を検証する。"""

    def test_complete_archive(self) -> None:
        """末尾まで正常に読み切れる書庫は Complete を返す。"""

        collected: list[tuple[float, int, bytes]] = []
        archive = _BuildPSCBlock([(0x14, b'\x73' + b'\x00' * 10)])
        result = TSInfoAnalyzer.readPSIData(BytesIO(archive), [0x14], lambda *args: collected.append(args) or True)
        assert result == 'Complete'
        # セクションが 1 件 callback へ渡っている
        assert len(collected) == 1
        assert collected[0][1] == 0x14
        assert collected[0][2] == b'\x73' + b'\x00' * 10

    def test_empty_archive_is_complete(self) -> None:
        """空の書庫 (ブロックなし) は Complete を返す。"""

        result = TSInfoAnalyzer.readPSIData(BytesIO(b''), [0x14], lambda *args: True)
        assert result == 'Complete'

    def test_truncated_archive(self) -> None:
        """ブロック途中でデータが尽きた書庫は Truncated を返す。"""

        archive = _BuildPSCBlock([(0x14, b'\x73' + b'\x00' * 10)])
        # 末尾のトレイラーとコード参照を欠落させて不完全な末尾を模倣する
        truncated = archive[:-4]
        result = TSInfoAnalyzer.readPSIData(BytesIO(truncated), [0x14], lambda *args: True)
        assert result == 'Truncated'

    def test_invalid_archive(self) -> None:
        """長さフィールドが矛盾する書庫は Invalid を返す。"""

        header = bytearray(32)
        header[0:8] = b'Pssc\x0d\x0a\x9a\x0a'
        # dictionary_window_len (0) < dictionary_len (5) の矛盾
        header[12:14] = (5).to_bytes(2, 'little')
        header[14:16] = (0).to_bytes(2, 'little')
        result = TSInfoAnalyzer.readPSIData(BytesIO(bytes(header)), [0x14], lambda *args: True)
        assert result == 'Invalid'

    def test_callback_stop(self) -> None:
        """callback が False を返した場合は Stopped を返す。"""

        archive = _BuildPSCBlock([(0x14, b'\x73' + b'\x00' * 10)])
        result = TSInfoAnalyzer.readPSIData(BytesIO(archive), [0x14], lambda *args: False)
        assert result == 'Stopped'






class TestTSInfoAnalyzerPSCFallback:
    """TSInfoAnalyzer の .psc 読み込みにおけるフォールバックとログ分類を検証する。"""

    def test_missing_psc_falls_back_silently(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """.psc が存在しない場合は正常なフォールバックとして warning を出さない。"""

        logs = _CaptureLogs(monkeypatch, 'app.metadata.TSInfoAnalyzer.logging')
        analyzer = TSInfoAnalyzer(_BuildRecordedVideo(tmp_path / 'recording.m4v'))
        # 書庫なし → 空の仮想 TS で続行する従来どおりの契約
        assert analyzer.end_ts_offset == 0
        assert analyzer.analyze() is None
        assert logs['warning'] == []

    def test_invalid_psc_logs_warning(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """構造的に破損した .psc は warning を記録してフォールバックする。"""

        logs = _CaptureLogs(monkeypatch, 'app.metadata.TSInfoAnalyzer.logging')
        header = bytearray(32)
        header[0:8] = b'Pssc\x0d\x0a\x9a\x0a'
        header[12:14] = (5).to_bytes(2, 'little')  # dictionary_window_len < dictionary_len の矛盾
        (tmp_path / 'recording.psc').write_bytes(bytes(header))

        analyzer = TSInfoAnalyzer(_BuildRecordedVideo(tmp_path / 'recording.m4v'))
        assert len(logs['warning']) == 1
        assert 'invalid' in logs['warning'][0]
        # フォールバック契約: 番組情報は取得できないが例外は送出されない
        assert analyzer.analyze() is None

    def test_truncated_psc_does_not_warn(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """末尾が不完全な .psc (録画中の書庫など) では warning を出さない。"""

        logs = _CaptureLogs(monkeypatch, 'app.metadata.TSInfoAnalyzer.logging')
        archive = _BuildPSCBlock([(0x14, _BuildTOTSection())])
        (tmp_path / 'recording.psc').write_bytes(archive[:-4])

        TSInfoAnalyzer(_BuildRecordedVideo(tmp_path / 'recording.m4v'))
        assert logs['warning'] == []
        # ベストエフォート解析として debug ログで追跡可能になっている
        assert len(logs['debug']) == 1

    def test_valid_psc_is_loaded(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """正常な .psc は warning なく仮想 TS へ変換される。"""

        logs = _CaptureLogs(monkeypatch, 'app.metadata.TSInfoAnalyzer.logging')
        archive = _BuildPSCBlock([(0x14, _BuildTOTSection())])
        (tmp_path / 'recording.psc').write_bytes(archive)

        analyzer = TSInfoAnalyzer(_BuildRecordedVideo(tmp_path / 'recording.m4v'))
        assert logs['warning'] == []
        # TOT セクションが TS パケットへ変換されている
        assert analyzer.end_ts_offset > 0

    def test_invalid_suffix_discards_partial_data(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """正常ブロックの後に破損ブロックが続く .psc では、読み取り済みの部分データも破棄される。"""

        logs = _CaptureLogs(monkeypatch, 'app.metadata.TSInfoAnalyzer.logging')
        archive = _BuildPSCBlock([(0x14, _BuildTOTSection())])
        broken_header = bytearray(32)
        broken_header[0:8] = b'BROKEN!!'
        (tmp_path / 'recording.psc').write_bytes(archive + bytes(broken_header))

        analyzer = TSInfoAnalyzer(_BuildRecordedVideo(tmp_path / 'recording.m4v'))
        # 破損が検出された場合は書庫なしと同じ状態へフォールバックする
        assert len(logs['warning']) == 1
        assert analyzer.end_ts_offset == 0
        assert analyzer.analyze() is None

    def test_unexpected_parse_exception_discards_partial_data(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """解析中の想定外例外では warning を記録し、部分データを破棄してフォールバックする。"""

        logs = _CaptureLogs(monkeypatch, 'app.metadata.TSInfoAnalyzer.logging')
        archive = _BuildPSCBlock([(0x14, _BuildTOTSection())])
        (tmp_path / 'recording.psc').write_bytes(archive)

        def RaiseUnexpected(reader: Any, target_pids: list[int], callback: Any) -> str:
            raise RuntimeError('simulated parse failure')

        monkeypatch.setattr(TSInfoAnalyzer, 'readPSIData', staticmethod(RaiseUnexpected))

        analyzer = TSInfoAnalyzer(_BuildRecordedVideo(tmp_path / 'recording.m4v'))
        assert len(logs['warning']) == 1
        assert analyzer.end_ts_offset == 0


class TestExtractTOTTimeListFromPSCArchive:
    """過去ログコメント API 向け TOT 時刻リスト抽出のフォールバック契約を検証する。"""

    def test_valid_psc_returns_tot_time_list(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """正常な .psc から TOT 時刻リストを取得でき、例外なく処理が継続する。"""

        logs = _CaptureLogs(monkeypatch, 'app.routers.VideosRouter.logging')
        psc_path = tmp_path / 'recording.psc'
        psc_path.write_bytes(_BuildPSCBlock([(0x14, _BuildTOTSection())]))

        result = ExtractTOTTimeListFromPSCArchive(psc_path)
        assert logs['warning'] == []
        assert len(result) == 1
        time_sec, _, tot_start, tot_end = result[0]
        assert time_sec == 0.0
        assert (tot_start.year, tot_start.month, tot_start.day) == (2026, 8, 9)
        assert tot_start == tot_end

    def test_corrupt_section_content_falls_back_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """構造は正しいがセクション内容が壊れた .psc でも例外を送出せずフォールバックする。"""

        _CaptureLogs(monkeypatch, 'app.routers.VideosRouter.logging')
        psc_path = tmp_path / 'recording.psc'
        # TOT としてパースできない内容のセクション (JST_time が不定の 0xFF 埋め)
        psc_path.write_bytes(_BuildPSCBlock([(0x14, b'\x73' + b'\xff' * 10)]))

        # JST_time が None となるため callback が False を返し Stopped → 空リストへフォールバックする
        result = ExtractTOTTimeListFromPSCArchive(psc_path)
        assert result == []
        # コメント API の継続を妨げない (例外が送出されない) ことを確認する

    def test_unexpected_parse_exception_returns_empty_list_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """解析中の想定外例外でも過去ログコメント取得処理が継続できるよう、warning 付きで空リストを返す。"""

        logs = _CaptureLogs(monkeypatch, 'app.routers.VideosRouter.logging')
        psc_path = tmp_path / 'recording.psc'
        psc_path.write_bytes(_BuildPSCBlock([(0x14, _BuildTOTSection())]))

        def RaiseUnexpected(reader: Any, target_pids: list[int], callback: Any) -> str:
            raise RuntimeError('simulated parse failure')

        monkeypatch.setattr(
            'app.routers.VideosRouter.TSInfoAnalyzer.readPSIData',
            staticmethod(RaiseUnexpected),
        )

        result = ExtractTOTTimeListFromPSCArchive(psc_path)
        assert result == []
        assert len(logs['warning']) == 1
