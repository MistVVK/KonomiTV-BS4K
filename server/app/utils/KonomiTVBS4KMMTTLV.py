
from __future__ import annotations

from typing import ClassVar


MMT_TLV_CONTAINER_FORMAT = 'MMT/TLV'
MMT_TLV_FILE_EXTENSIONS = ('.tlv', '.mmt', '.mmts')


class KonomiTVBS4KTLVSyncError(ValueError):
    """TLV 入力が同期不能、または別形式だったことを表す。"""


class KonomiTVBS4KTLVSynchronizer:
    """任意の読み取り境界から TLV パケット境界を確定し、破損時にも再同期する。"""

    SYNC_BYTE: ClassVar[int] = 0x7F
    VALID_PACKET_TYPES: ClassVar[frozenset[int]] = frozenset((0x01, 0x02, 0x03, 0xFE, 0xFF))
    HEADER_SIZE: ClassVar[int] = 4
    REQUIRED_CONSECUTIVE_PACKETS: ClassVar[int] = 4
    MAX_UNSYNCHRONIZED_BUFFER_SIZE: ClassVar[int] = 1024 * 1024
    MPEG_TS_PACKET_SIZE: ClassVar[int] = 188

    def __init__(self) -> None:
        """
        TLV synchronizer を初期化する。

        Args:
            なし。

        Returns:
            なし。
        """

        # 次の TLV パケット境界を探索・組み立てるため、まだ出力できない入力を保持する。
        # feed() だけから参照され、同期前は 1MiB、同期後は TLV 1 パケット分を上限とする。
        self._buffer = bytearray()

        # 4 個連続した正常な TLV パケットで境界を確定済みかを保持する。
        # feed() は破損を検出すると False に戻し、同じバッファ内から再探索する。
        self._synchronized = False

    @property
    def synchronized(self) -> bool:
        """
        TLV パケット境界を確定済みか返す。

        Args:
            なし。

        Returns:
            bool: 4 パケット連続で境界を確認済みなら True。
        """

        return self._synchronized

    @classmethod
    def _getPacketSize(cls, buffer: bytearray, offset: int) -> int | None:
        """
        指定位置が TLV ヘッダーならパケット全長を返す。

        Args:
            buffer (bytearray): 検査対象バッファ。
            offset (int): ヘッダー候補の開始位置。

        Returns:
            int | None: TLV パケット全長。ヘッダーでなければ None。
        """

        if len(buffer) - offset < cls.HEADER_SIZE:
            return None
        if buffer[offset] != cls.SYNC_BYTE or buffer[offset + 1] not in cls.VALID_PACKET_TYPES:
            return None
        return cls.HEADER_SIZE + int.from_bytes(buffer[offset + 2:offset + 4], byteorder='big')

    @classmethod
    def _looksLikeMPEGTS(cls, buffer: bytearray) -> bool:
        """
        同じ位相に MPEG-TS sync byte が 4 パケット連続するか検査する。

        Args:
            buffer (bytearray): 同期前の入力バッファ。

        Returns:
            bool: MPEG-TS と判断できる場合は True。
        """

        required_size = cls.MPEG_TS_PACKET_SIZE * cls.REQUIRED_CONSECUTIVE_PACKETS
        if len(buffer) < required_size:
            return False
        # 入力が TS パケット途中から始まる場合もあるため、最初の 188 byte の全位相を調べる。
        for offset in range(cls.MPEG_TS_PACKET_SIZE):
            if offset + required_size > len(buffer):
                break
            if all(
                buffer[offset + (index * cls.MPEG_TS_PACKET_SIZE)] == 0x47
                for index in range(cls.REQUIRED_CONSECUTIVE_PACKETS)
            ):
                return True
        return False

    @classmethod
    def _findSynchronizationOffset(cls, buffer: bytearray) -> int | None:
        """
        完全な TLV パケットが 4 個連続する最初の位置を返す。

        Args:
            buffer (bytearray): 同期前の入力バッファ。

        Returns:
            int | None: 境界位置。まだ確定できなければ None。
        """

        for candidate in range(max(0, len(buffer) - cls.HEADER_SIZE + 1)):
            if buffer[candidate] != cls.SYNC_BYTE:
                continue
            offset = candidate
            for packet_index in range(cls.REQUIRED_CONSECUTIVE_PACKETS):
                packet_size = cls._getPacketSize(buffer, offset)
                if packet_size is None or len(buffer) - offset < packet_size:
                    break
                offset += packet_size
                if packet_index == cls.REQUIRED_CONSECUTIVE_PACKETS - 1:
                    return candidate
        return None

    def feed(self, data: bytes) -> bytes:
        """
        入力を追加し、境界が確定した完全な TLV パケットだけを返す。

        Args:
            data (bytes): チューナーから受信した任意境界のバイト列。

        Returns:
            bytes: 0 個以上の完全な TLV パケット。

        Raises:
            KonomiTVBS4KTLVSyncError: MPEG-TS 入力、または 1MiB 内に TLV 境界を発見できない場合。
        """

        if data:
            self._buffer.extend(data)
        output = bytearray()

        while self._buffer:
            # 未同期時は偶然の 0x7F ではなく、完全な 4 パケットの連続で境界を確定する。
            if self._synchronized is False:
                if self._looksLikeMPEGTS(self._buffer):
                    raise KonomiTVBS4KTLVSyncError('MPEG-TS input was received while MMT/TLV was selected.')
                synchronization_offset = self._findSynchronizationOffset(self._buffer)
                if synchronization_offset is None:
                    if len(self._buffer) > self.MAX_UNSYNCHRONIZED_BUFFER_SIZE:
                        raise KonomiTVBS4KTLVSyncError(
                            'MMT/TLV packet synchronization was not found within the 1 MiB input limit.'
                        )
                    break
                if synchronization_offset > 0:
                    del self._buffer[:synchronization_offset]
                self._synchronized = True

            # 同期後は完全なパケットだけを出力し、次の読み取りを待つ不完全パケットは保持する。
            packet_size = self._getPacketSize(self._buffer, 0)
            if packet_size is None:
                self._synchronized = False
                continue
            if len(self._buffer) < packet_size:
                break
            output.extend(self._buffer[:packet_size])
            del self._buffer[:packet_size]

        return bytes(output)


def BuildKonomiTVBS4KMMTTLVInputArguments(container_format: str) -> list[str]:
    """
    MMT/TLV 録画だけに libaribtlv demuxer を強制する FFmpeg 入力引数を返す。

    Args:
        container_format (str): RecordedVideo.container_format。

    Returns:
        list[str]: `-i` より前へ挿入する FFmpeg / FFprobe 引数。
    """

    if container_format == MMT_TLV_CONTAINER_FORMAT:
        return ['-f', 'libaribtlv']
    return []
