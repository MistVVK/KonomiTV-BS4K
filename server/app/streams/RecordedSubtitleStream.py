from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, ClassVar, Literal, cast

from biim.mpeg2ts.parser import PESParser
from biim.mpeg2ts.pes import PES
from typing_extensions import TypedDict

from app import logging
from app.constants import LIBRARY_PATH, RECORDED_SUBTITLES_DIR
from app.models.RecordedVideo import RecordedVideo
from app.schemas import SubtitleTrack
from app.utils.TSKeyFrameSeeker import TSKeyFrameSeeker


class ARIBSubtitlePacket(TypedDict):
    pts: float
    duration: float | None
    data: str
    is_restore_point: bool


class ARIBSubtitleRange(TypedDict):
    track: int
    start_time: float
    end_time: float
    management_data: list[str]
    drcs: list[str]
    restore_packets: list[ARIBSubtitlePacket]
    packets: list[ARIBSubtitlePacket]


class RecordedSubtitleStream:
    """録画映像・音声と独立して字幕キャッシュと時間範囲APIを管理する。"""

    _locks: ClassVar[dict[Path, asyncio.Lock]] = {}
    _image_codecs: ClassVar[set[str]] = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle', 'xsub'}
    _cache_version = 3

    def __init__(self, recorded_video: RecordedVideo) -> None:
        self.recorded_video = recorded_video

    def getTrack(self, subtitle_index: int) -> SubtitleTrack | None:
        """論理トラック番号に一致する字幕を返す。"""

        return next((track for track in self.recorded_video.subtitle_tracks if track['index'] == subtitle_index), None)

    def getTrackKind(self, subtitle_index: int) -> Literal['ARIB', 'Text', 'Image'] | None:
        """字幕コーデックを公開方式に分類する。"""

        track = self.getTrack(subtitle_index)
        if track is None:
            return None
        codec = track['codec'].lower()
        if 'arib' in codec:
            return 'ARIB'
        if codec in self._image_codecs:
            return 'Image'
        return 'Text'

    async def getWebVTT(self, subtitle_index: int) -> bytes | None:
        """テキスト字幕を永続WebVTTキャッシュとして返す。"""

        track = self.getTrack(subtitle_index)
        if track is None or self.getTrackKind(subtitle_index) != 'Text':
            return None
        cache_path = self.__getCachePath(subtitle_index, 'vtt')
        if cache_path.is_file():
            return await asyncio.to_thread(cache_path.read_bytes)
        async with self._locks.setdefault(cache_path, asyncio.Lock()):
            if cache_path.is_file():
                return await asyncio.to_thread(cache_path.read_bytes)
            process = await asyncio.create_subprocess_exec(
                LIBRARY_PATH['FFmpeg8'], '-hide_banner', '-loglevel', 'error',
                '-i', self.recorded_video.file_path, '-map', f'0:{track["stream_index"]}',
                '-f', 'webvtt', 'pipe:1',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                logging.error(
                    '[RecordedSubtitleStream] WebVTT conversion failed. '
                    f'[recorded_video_id: {self.recorded_video.id}, track: {subtitle_index}, '
                    f'stderr: {stderr.decode(errors="ignore").strip()}]'
                )
                return None
            await self.__writeAtomic(cache_path, stdout)
            return stdout

    async def getARIBRange(self, subtitle_index: int, start_time: float, end_time: float) -> ARIBSubtitleRange | None:
        """指定範囲のPTS付きARIB生データと直前の復元点を返す。"""

        track = self.getTrack(subtitle_index)
        if track is None or self.getTrackKind(subtitle_index) != 'ARIB' or end_time <= start_time:
            return None
        packets = await self.__loadARIBPackets(subtitle_index, track)
        # CanvasRendererは管理データ・DRCS・表示状態を内部に保持する。任意位置へのseekでは
        # 直前の1packetだけでは状態を復元できないため、録画先頭から対象直前までを時系列で返す。
        # クライアントはseek/初回だけこれを投入し、通常の先読みではrange packetsだけを使う。
        restore_packets: list[ARIBSubtitlePacket] = [
            {
                'pts': packet['pts'],
                'duration': packet['duration'],
                'data': packet['data'],
                'is_restore_point': True,
            }
            for packet in packets
            if packet['pts'] < start_time
        ]
        range_packets: list[ARIBSubtitlePacket] = []
        for packet in packets:
            if start_time <= packet['pts'] < end_time:
                range_packets.append({
                    'pts': packet['pts'],
                    'duration': packet['duration'],
                    'data': packet['data'],
                    'is_restore_point': False,
                })
        management_data: list[str] = []
        drcs: list[str] = []
        for packet in [*restore_packets, *range_packets]:
            raw_data = base64.b64decode(packet['data'])
            is_management, has_drcs = self.__inspectARIBPacket(raw_data)
            if is_management and packet['data'] not in management_data:
                management_data.append(packet['data'])
            if has_drcs and packet['data'] not in drcs:
                drcs.append(packet['data'])
        return {
            'track': subtitle_index,
            'start_time': start_time,
            'end_time': end_time,
            # API利用者が復元内容を明示的に把握できるよう、該当生packetも別掲する。
            'management_data': management_data,
            'drcs': drcs,
            'restore_packets': restore_packets,
            'packets': range_packets,
        }

    async def __loadARIBPackets(self, subtitle_index: int, track: SubtitleTrack) -> list[ARIBSubtitlePacket]:
        """FFprobe 8の全packet索引をサーバーデータ領域へ永続化する。"""

        cache_path = self.__getCachePath(subtitle_index, 'arib.json')
        async with self._locks.setdefault(cache_path, asyncio.Lock()):
            if cache_path.is_file():
                try:
                    return cast(list[ARIBSubtitlePacket], json.loads(await asyncio.to_thread(cache_path.read_text)))
                except (OSError, json.JSONDecodeError):
                    pass
            process = await asyncio.create_subprocess_exec(
                LIBRARY_PATH['FFprobe8'], '-v', 'error', '-select_streams', str(track['stream_index']),
                '-show_packets', '-show_data', '-show_format',
                '-show_entries', 'packet=pts_time,duration_time,data:format=start_time', '-of', 'json',
                self.recorded_video.file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                logging.error(
                    '[RecordedSubtitleStream] ARIB packet indexing failed. '
                    f'[recorded_video_id: {self.recorded_video.id}, track: {subtitle_index}, '
                    f'stderr: {stderr.decode(errors="ignore").strip()}]'
                )
                return []
            try:
                probe = json.loads(stdout)
                source_packets = probe.get('packets', [])
            except json.JSONDecodeError:
                return []
            packets = self.__buildARIBPackets(probe, source_packets)
            # FFmpegが未知のprivate dataとして扱うARIB字幕は、stream自体をbin_dataとして
            # probeできてもpacketを公開しない。PMTで確定済みのPIDがある場合だけTS/PESを
            # 直接組み立て、data_identifier 0x80の字幕を文字スーパー0x81と分離する。
            subtitle_pid = track.get('pid')
            if len(packets) == 0 and subtitle_pid is not None:
                format_start_time = probe.get('format', {}).get('start_time')
                source_start_time = float(format_start_time) if format_start_time is not None else 0.0
                packets = await asyncio.to_thread(
                    self.__extractARIBPacketsFromTS,
                    Path(self.recorded_video.file_path),
                    subtitle_pid,
                    source_start_time,
                )
            await self.__writeAtomic(cache_path, json.dumps(packets, separators=(',', ':')).encode())
            return packets

    @staticmethod
    def __extractARIBPacketsFromTS(
        file_path: Path,
        subtitle_pid: int,
        source_start_time: float,
    ) -> list[ARIBSubtitlePacket]:
        """FFmpegがpacketを公開しないTSのARIB字幕PESを直接索引化する。"""

        try:
            packet_size = TSKeyFrameSeeker.findStreamInfo(file_path).packet_size
        except (OSError, ValueError):
            return []
        parser = PESParser(PES)
        packets: list[ARIBSubtitlePacket] = []
        unwrap_target_pts = round(source_start_time * 90_000)
        chunk_size = packet_size * 32_768
        with file_path.open('rb') as source:
            while chunk := source.read(chunk_size):
                for offset in range(0, len(chunk) - packet_size + 1, packet_size):
                    packet_offset = offset + (4 if packet_size == 192 else 0)
                    if packet_offset + 188 > len(chunk) or chunk[packet_offset] != 0x47:
                        continue
                    packet_pid = ((chunk[packet_offset + 1] & 0x1F) << 8) | chunk[packet_offset + 2]
                    if packet_pid != subtitle_pid:
                        continue
                    packet = chunk[packet_offset:packet_offset + 188]
                    try:
                        parser.push(packet)
                    except (IndexError, ValueError):
                        parser = PESParser(PES)
                        continue
                    for pes in parser:
                        raw_pts = pes.pts()
                        data = bytes(pes.PES_packet_data())
                        if raw_pts is None or data[:1] != b'\x80':
                            continue
                        source_pts = TSKeyFrameSeeker.unwrapNear(raw_pts, unwrap_target_pts)
                        unwrap_target_pts = source_pts
                        packets.append({
                            'pts': max(0.0, (source_pts / 90_000) - source_start_time),
                            'duration': None,
                            'data': base64.b64encode(data).decode(),
                            'is_restore_point': False,
                        })
        return packets

    @classmethod
    def __buildARIBPackets(
        cls,
        probe: dict[str, Any],
        source_packets: list[dict[str, Any]],
    ) -> list[ARIBSubtitlePacket]:
        """FFprobe packetを録画先頭0秒基準のARIB packetへ変換する。"""

        # MPEG-TSのPTSは放送波由来の大きな値になるため、映像インデックスと同じく
        # format.start_timeを録画先頭として差し引く。最初の字幕を0秒にすると、
        # 字幕が途中から始まる録画で表示時刻が前倒しされてしまう。
        format_start_time = probe.get('format', {}).get('start_time')
        source_start_time = float(format_start_time) if format_start_time is not None else 0.0
        packets: list[ARIBSubtitlePacket] = []
        for packet in source_packets:
            if packet.get('pts_time') is None or packet.get('data') is None:
                continue
            raw_data = cls.__decodeFFprobeData(str(packet['data']))
            packets.append({
                'pts': max(0.0, float(packet['pts_time']) - source_start_time),
                'duration': float(packet['duration_time']) if packet.get('duration_time') is not None else None,
                'data': base64.b64encode(raw_data).decode(),
                'is_restore_point': False,
            })
        return packets

    def __getCachePath(self, subtitle_index: int, suffix: str) -> Path:
        index_version = self.recorded_video.playback_index_version or 0
        return RECORDED_SUBTITLES_DIR / (
            f'v{self._cache_version}-{self.recorded_video.file_hash}-{subtitle_index}-{index_version}.{suffix}'
        )

    @staticmethod
    def __decodeFFprobeData(data: str) -> bytes:
        """FFprobe -show_dataのhex dumpを元のbyte列へ戻す。"""

        hex_text = ''
        for line in data.splitlines():
            if ':' not in line:
                continue
            hex_column = line.split(':', 1)[1].split('  ', 1)[0]
            hex_text += ''.join(character for character in hex_column if character in '0123456789abcdefABCDEF')
        try:
            return bytes.fromhex(hex_text)
        except ValueError:
            return b''

    @staticmethod
    def __inspectARIBPacket(data: bytes) -> tuple[bool, bool]:
        """ARIB PESから管理データgroupとDRCS data unitの有無を調べる。

        Args:
            data: FFprobeから復元したARIB字幕PES payload。

        Returns:
            管理データgroupか、DRCSを含むかの組。
        """

        # aribb24.jsと同じPES_data_packet_header_length解釈でdata group先頭を得る。
        if len(data) <= 3 or data[0] not in (0x80, 0x81):
            return (False, False)
        data_group_begin = 3 + (data[2] & 0x0F)
        if len(data) <= data_group_begin:
            return (False, False)
        data_group_id = (data[data_group_begin] & 0xFC) >> 2
        is_management = (data_group_id & 0x0F) == 0

        # data unitは0x1F, parameter, 24bit sizeで始まる。管理データは言語情報を
        # 挟むため固定offsetに依存せず、宣言sizeがpacket内に収まるunitだけを走査する。
        has_drcs = False
        offset = data_group_begin + 5
        while offset + 5 <= len(data):
            if data[offset] != 0x1F:
                offset += 1
                continue
            parameter = data[offset + 1]
            unit_size = int.from_bytes(data[offset + 2:offset + 5], 'big')
            unit_end = offset + 5 + unit_size
            if unit_end > len(data):
                offset += 1
                continue
            if parameter in (0x30, 0x31):
                has_drcs = True
            offset = unit_end
        return (is_management, has_drcs)

    @staticmethod
    async def __writeAtomic(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f'{path.name}.tmp')
        await asyncio.to_thread(temporary_path.write_bytes, data)
        await asyncio.to_thread(temporary_path.replace, path)
