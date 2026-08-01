"""
ライブ放送波 TS から薄いヘッダ解析で映像 geometry (coded size / DAR) を取る。

フル FFmpeg を立てず、MPEG-2 sequence header (0x000001B3) だけを追う。
エンコード本体は QSV 等の full-GPU 経路に載せ、SAR を毎フレーム hwdownload
しないための入力契約を提供する。
"""

from __future__ import annotations

import math
from dataclasses import dataclass


# ISO/IEC 13818-2 Table 6-3 aspect_ratio_information → 表示アスペクト (DAR)
_MPEG2_ASPECT_RATIO_INFORMATION_TO_DAR: dict[int, tuple[int, int]] = {
    1: (1, 1),
    2: (4, 3),
    3: (16, 9),
    4: (221, 100),
}


@dataclass(frozen=True, slots=True)
class LiveSourceVideoGeometry:
    """放送波側の coded size と表示アスペクト比。"""

    coded_width: int
    coded_height: int
    dar_width: int
    dar_height: int
    source: str = 'mpeg2-sequence-header'

    def __post_init__(self) -> None:
        """
        初期化後の値を検証する。

        Args:
            None

        Returns:
            None
        """
        if self.coded_width < 2 or self.coded_height < 2:
            raise ValueError(
                f'Invalid coded size: {self.coded_width}x{self.coded_height}',
            )
        if self.dar_width <= 0 or self.dar_height <= 0:
            raise ValueError(
                f'Invalid DAR: {self.dar_width}:{self.dar_height}',
            )

    @property
    def sample_aspect_ratio(self) -> tuple[int, int]:
        """
        coded size と DAR から SAR を還元する。

        SAR = DAR / (coded_w/coded_h) = (dar_w * h) : (dar_h * w)

        Args:
            None

        Returns:
            tuple[int, int]: 約分済みの sample aspect ratio。
        """

        sar_w = self.dar_width * self.coded_height
        sar_h = self.dar_height * self.coded_width
        divisor = math.gcd(sar_w, sar_h)
        return (sar_w // divisor, sar_h // divisor)

    @property
    def is_approximately_16_9(self) -> bool:
        """
        16:9 表示とみなせるか（誤差 2% 以内）。

        Args:
            None

        Returns:
            bool: 判定結果。
        """

        return abs((self.dar_width / self.dar_height) - (16 / 9)) <= 0.02

    @property
    def is_approximately_4_3(self) -> bool:
        """
        4:3 表示とみなせるか（誤差 2% 以内）。

        Args:
            None

        Returns:
            bool: 判定結果。
        """

        return abs((self.dar_width / self.dar_height) - (4 / 3)) <= 0.02


def EstimateDefaultLiveSourceGeometry(
    channel_type: str,
    *,
    is_fullhd_channel: bool,
    is_oneseg: bool = False,
) -> LiveSourceVideoGeometry:
    """
    ヘッダ未取得時の既定 geometry。

    地デジ HD は 1440x1080 + DAR 16:9 が典型。フル HD 特例は 1920x1080。

    Args:
        channel_type (str): 入力チャンネルの放送種別。
        is_fullhd_channel (bool): 入力が 1920x1080 のフル HD チャンネルか。
        is_oneseg (bool): 入力がワンセグ放送か。

    Returns:
        LiveSourceVideoGeometry: 入力条件から推定した既定 geometry。
    """

    if is_oneseg is True:
        return LiveSourceVideoGeometry(
            coded_width = 320,
            coded_height = 180,
            dar_width = 16,
            dar_height = 9,
            source = 'default-oneseg',
        )
    if channel_type == 'BS4K':
        return LiveSourceVideoGeometry(
            coded_width = 3840,
            coded_height = 2160,
            dar_width = 16,
            dar_height = 9,
            source = 'default-bs4k',
        )
    if is_fullhd_channel is True:
        return LiveSourceVideoGeometry(
            coded_width = 1920,
            coded_height = 1080,
            dar_width = 16,
            dar_height = 9,
            source = 'default-fullhd',
        )
    return LiveSourceVideoGeometry(
        coded_width = 1440,
        coded_height = 1080,
        dar_width = 16,
        dar_height = 9,
        source = 'default-hd-anamorphic',
    )


def ParseMpeg2SequenceHeaderGeometry(payload: bytes) -> LiveSourceVideoGeometry | None:
    """
    ES payload 内の MPEG-2 sequence header から geometry を1件返す。

    見つからない・壊れている場合は None。

    Args:
        payload (bytes): 解析対象 section または sequence header の payload。

    Returns:
        LiveSourceVideoGeometry | None: 検出できた場合の入力映像 geometry。
    """

    start = 0
    while True:
        index = payload.find(b'\x00\x00\x01\xb3', start)
        if index < 0:
            return None
        header = payload[index + 4:index + 8]
        if len(header) < 4:
            return None
        coded_width = (header[0] << 4) | (header[1] >> 4)
        coded_height = ((header[1] & 0x0F) << 8) | header[2]
        aspect_ratio_information = (header[3] >> 4) & 0x0F
        dar = _MPEG2_ASPECT_RATIO_INFORMATION_TO_DAR.get(aspect_ratio_information)
        if dar is None or coded_width < 16 or coded_height < 16:
            start = index + 4
            continue
        # 拡張 size (horizontal/vertical_size_extension) は sequence_extension 側。
        ## 当面 ISDB HD/SD の 12bit 範囲で足りる。
        return LiveSourceVideoGeometry(
            coded_width = coded_width,
            coded_height = coded_height,
            dar_width = dar[0],
            dar_height = dar[1],
            source = 'mpeg2-sequence-header',
        )


class LiveSourceAspectMonitor:
    """
    188 バイト TS を流し込み、映像 PID 上の MPEG-2 sequence header を監視する。

    - フルデコードはしない
    - PES 再結合は PUSI 単位の簡易バッファ（巨大 PES は先頭側だけ見る）
    """

    # 1 PES あたり保持する最大バイト（sequence header は先頭付近）
    _MAX_PES_BYTES: int = 64 * 1024

    def __init__(self) -> None:
        """
        利用する状態を初期化する。

        Args:
            None

        Returns:
            None
        """
        self._video_pid: int | None = None
        self._pes_buffer = bytearray()
        self._geometry: LiveSourceVideoGeometry | None = None
        self._pmt_pid: int | None = None
        self._pat_seen = False

    @property
    def geometry(self) -> LiveSourceVideoGeometry | None:
        """
        現在検出済みの入力映像 geometry を返す。

        Args:
            None

        Returns:
            LiveSourceVideoGeometry | None: 検出できた場合の入力映像 geometry。
        """
        return self._geometry

    def push(self, chunk: bytes) -> LiveSourceVideoGeometry | None:
        """
        TS chunk を取り込み、新しく確定した geometry があれば返す。

        変化がない／未確定なら None。

        Args:
            chunk (bytes): 処理対象の TS chunk。

        Returns:
            LiveSourceVideoGeometry | None: 検出できた場合の入力映像 geometry。
        """

        if len(chunk) < 188:
            return None
        previous = self._geometry
        for offset in range(0, len(chunk) - 187, 188):
            packet = chunk[offset:offset + 188]
            if packet[0] != 0x47:
                continue
            self._ingestPacket(packet)
        if self._geometry is not None and self._geometry != previous:
            return self._geometry
        return None

    def _ingestPacket(self, packet: bytes) -> None:
        """
        単一 TS packet の PAT・PMT・video PES を対応 parser へ振り分ける。

        Args:
            packet (bytes): 解析対象の 188 byte TS packet。

        Returns:
            None
        """
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        payload_unit_start = (packet[1] & 0x40) != 0
        adaptation_field_control = (packet[3] >> 4) & 0x03
        index = 4
        if adaptation_field_control in (2, 3):
            adaptation_length = packet[4]
            index = 5 + adaptation_length
        if adaptation_field_control not in (1, 3) or index >= 188:
            return
        payload = packet[index:]

        if pid == 0x0000:
            self._parsePat(payload, payload_unit_start)
            return
        if self._pmt_pid is not None and pid == self._pmt_pid:
            self._parsePmt(payload, payload_unit_start)
            return
        if self._video_pid is None or pid != self._video_pid:
            return

        if payload_unit_start is True:
            if self._pes_buffer:
                self._scanPes(bytes(self._pes_buffer))
            self._pes_buffer = bytearray()
            # pointer ではない PES ヘッダ付き payload
            if len(payload) >= 9 and payload[0:3] == b'\x00\x00\x01':
                header_data_length = payload[8]
                payload_start = 9 + header_data_length
                if payload_start < len(payload):
                    self._pes_buffer.extend(payload[payload_start:])
            else:
                self._pes_buffer.extend(payload)
        elif self._pes_buffer:
            remaining = self._MAX_PES_BYTES - len(self._pes_buffer)
            if remaining > 0:
                self._pes_buffer.extend(payload[:remaining])
        # sequence header は 8 バイトあれば足りる。到着次第スキャンする。
        if self._geometry is None and len(self._pes_buffer) >= 8:
            self._scanPes(bytes(self._pes_buffer))

    def _scanPes(self, pes_payload: bytes) -> None:
        """
        video PES から MPEG-2 sequence header geometry を探索する。

        Args:
            pes_payload (bytes): pes_解析対象 section または sequence header の payload。

        Returns:
            None
        """
        geometry = ParseMpeg2SequenceHeaderGeometry(pes_payload)
        if geometry is not None:
            self._geometry = geometry

    def _parsePat(self, payload: bytes, payload_unit_start: bool) -> None:
        """
        PAT section から対象 program の PMT PID を更新する。

        Args:
            payload (bytes): 解析対象 section または sequence header の payload。
            payload_unit_start (bool): payload 先頭に PSI pointer field があるか。

        Returns:
            None
        """
        data = payload
        if payload_unit_start is True and data:
            pointer = data[0]
            data = data[1 + pointer:]
        if len(data) < 12 or data[0] != 0x00:
            return
        section_length = ((data[1] & 0x0F) << 8) | data[2]
        end = 3 + section_length - 4
        if end > len(data):
            end = len(data)
        index = 8
        while index + 4 <= end:
            program_number = (data[index] << 8) | data[index + 1]
            program_map_pid = ((data[index + 2] & 0x1F) << 8) | data[index + 3]
            index += 4
            if program_number != 0:
                self._pmt_pid = program_map_pid
                self._pat_seen = True
                return

    def _parsePmt(self, payload: bytes, payload_unit_start: bool) -> None:
        """
        PMT section から MPEG-2 video PID を更新する。

        Args:
            payload (bytes): 解析対象 section または sequence header の payload。
            payload_unit_start (bool): payload 先頭に PSI pointer field があるか。

        Returns:
            None
        """
        data = payload
        if payload_unit_start is True and data:
            pointer = data[0]
            data = data[1 + pointer:]
        if len(data) < 12 or data[0] != 0x02:
            return
        section_length = ((data[1] & 0x0F) << 8) | data[2]
        program_info_length = ((data[10] & 0x0F) << 8) | data[11]
        index = 12 + program_info_length
        end = 3 + section_length - 4
        if end > len(data):
            end = len(data)
        while index + 5 <= end:
            stream_type = data[index]
            elementary_pid = ((data[index + 1] & 0x1F) << 8) | data[index + 2]
            es_info_length = ((data[index + 3] & 0x0F) << 8) | data[index + 4]
            index += 5 + es_info_length
            # MPEG-2 video
            if stream_type == 0x02:
                self._video_pid = elementary_pid
                return
            # 予備: H.264/H.265 は sequence header 監視対象外（既定 geometry を使う）
            if stream_type in (0x1B, 0x24) and self._video_pid is None:
                self._video_pid = elementary_pid
