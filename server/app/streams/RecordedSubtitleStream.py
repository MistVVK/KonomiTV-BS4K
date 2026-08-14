from __future__ import annotations

import asyncio
import base64
import json
import re
import stat
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal, NamedTuple, cast

from biim.mpeg2ts.parser import PESParser, SectionParser
from biim.mpeg2ts.pat import PATSection
from biim.mpeg2ts.pes import PES
from biim.mpeg2ts.pmt import PMTSection
from typing_extensions import TypedDict

from app import logging
from app.constants import LIBRARY_PATH, RECORDED_SUBTITLES_DIR
from app.models.RecordedVideo import RecordedVideo
from app.schemas import SubtitleTrack
from app.utils.KonomiTVBS4KMMTTLV import (
    MMT_TLV_CONTAINER_FORMAT,
    BuildKonomiTVBS4KMMTTLVInputArguments,
)
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


class ARIBTTMLPacket(TypedDict):
    pts: float
    transport_timestamp: float
    component_tag: int
    data: str
    is_restore_point: bool


class ARIBTTMLRange(TypedDict):
    start_time: float
    end_time: float
    restore_packets: list[ARIBTTMLPacket]
    packets: list[ARIBTTMLPacket]


class ARIBTTMLPacketIndex(NamedTuple):
    """ARIB-TTML packetと時間範囲探索用のメモリ索引。"""

    packets: list[ARIBTTMLPacket]
    pts: list[float]
    packet_indices_by_component: dict[int, list[int]]
    pts_by_component: dict[int, list[float]]


@dataclass(slots=True)
class RecordedSubtitleCacheLockEntry:
    """字幕cache生成中のholder・waiter参照と排他lockを一体で管理する。"""

    lock: asyncio.Lock
    reference_count: int = 0


class RecordedSubtitleStream:
    """録画映像・音声と独立して字幕キャッシュと時間範囲APIを管理する。"""

    _locks: ClassVar[dict[Path, RecordedSubtitleCacheLockEntry]] = {}
    _image_codecs: ClassVar[set[str]] = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle', 'xsub'}
    _cache_version = 3
    _arib_ttml_cache_version = 3
    _file_hash_pattern: ClassVar[re.Pattern[str]] = re.compile(r'^[0-9a-f]{32}$')
    _cache_file_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r'^v(?:0|[1-9]\d*)-[0-9a-f]{32}-(?:0|[1-9]\d*)-(?:0|[1-9]\d*)'
        r'\.(?:vtt|arib\.json)$',
    )
    _arib_ttml_cache_file_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r'^ttml-v(?:0|[1-9]\d*)-[0-9a-f]{32}-(?:0|[1-9]\d*)-'
        r'(?:all|(?:0|[1-9]\d*)(?:-(?:0|[1-9]\d*))*)\.json$',
    )
    _arib_ttml_memory_cache_size = 8
    _arib_ttml_memory_cache_bytes_limit = 128 * 1024 * 1024
    _arib_ttml_memory_cache: ClassVar[OrderedDict[Path, ARIBTTMLPacketIndex]] = OrderedDict()
    _arib_ttml_memory_cache_bytes: ClassVar[dict[Path, int]] = {}
    _arib_ttml_memory_cache_bytes_total = 0

    def __init__(self, recorded_video: RecordedVideo) -> None:
        self.recorded_video = recorded_video

    @classmethod
    @asynccontextmanager
    async def __cacheLock(cls, cache_path: Path) -> AsyncGenerator[None, None]:
        """cache pathのholder・waiterを参照数へ含め、最後の解放後にentryを回収する。"""

        # asyncio taskはawait地点でのみ切り替わるため、entry作成と参照追加を連続して行えば、
        # GCが同じイベントループ上でactive判定する前に必ず利用中として可視化される。
        entry = cls._locks.setdefault(cache_path, RecordedSubtitleCacheLockEntry(asyncio.Lock()))
        entry.reference_count += 1
        try:
            async with entry.lock:
                yield
        finally:
            # waiterのcancelも参照から外し、同じpathへ作り直された別entryは削除しない。
            entry.reference_count -= 1
            if entry.reference_count == 0 and cls._locks.get(cache_path) is entry:
                cls._locks.pop(cache_path, None)

    @classmethod
    async def cleanupOrphanedCaches(cls) -> None:
        """DBから到達不能な厳密所有cacheと中断後tmpだけを回収する。"""

        # DB取得・JSON field解釈のどちらかが不完全な場合、reachableを過小評価してはならない。
        # そのため全行から集合を構築し終えるまでファイル削除を一切開始しない。
        try:
            rows = await RecordedVideo.all().values('file_hash', 'playback_index_version', 'subtitle_tracks')
            reachable_paths = cls.__buildReachableCachePaths(rows)
        except Exception as ex:
            logging.warning(
                '[RecordedSubtitleStream] Failed to determine reachable subtitle caches; cleanup skipped.',
                exc_info=ex,
            )
            return

        try:
            entries = await asyncio.to_thread(lambda: list(RECORDED_SUBTITLES_DIR.iterdir()))
        except FileNotFoundError:
            return
        except OSError as ex:
            logging.warning('[RecordedSubtitleStream] Failed to enumerate subtitle caches:', exc_info=ex)
            return

        deleted_count = 0
        for cache_path in entries:
            parsed_path = cls.__parseManagedCachePath(cache_path)
            if parsed_path is None:
                continue
            completed_path, is_temporary = parsed_path

            # 完成済みの現行cacheはDBから到達できる限り保持する。tmpは完成cacheがreachableでも
            # 正常終了時には存在しないため、active生成で保護されていなければ中断残骸として回収する。
            if is_temporary is False and completed_path in reachable_paths:
                continue
            lock_entry = cls._locks.get(completed_path)
            if lock_entry is not None and lock_entry.reference_count > 0:
                continue

            # active判定後に生成側が同じpathへ参入すると、GCのunlinkが生成中tmpを消し得る。
            # GC自身も同じlockのholderとして登録し、削除完了まで新規生成を待機させる。
            async with cls.__cacheLock(completed_path):
                # lock取得までにentryが消滅・置換され得るため、通常ファイルかを改めて確認する。
                # 同名symlinkやdirectoryは第三者entryとして保持する。
                try:
                    cache_stat = await asyncio.to_thread(cache_path.lstat)
                except FileNotFoundError:
                    continue
                except OSError as ex:
                    logging.warning(
                        f'[RecordedSubtitleStream] Failed to inspect subtitle cache: {cache_path}',
                        exc_info=ex,
                    )
                    continue
                if stat.S_ISREG(cache_stat.st_mode) is False:
                    continue

                try:
                    # taskのcancel時もworkerのunlink完了までは同じlockを保持し、生成側との競合を防ぐ。
                    unlink_task = asyncio.create_task(asyncio.to_thread(cache_path.unlink))
                    try:
                        await asyncio.shield(unlink_task)
                    except asyncio.CancelledError:
                        try:
                            await unlink_task
                        except OSError:
                            # cleanupのcancelを優先しつつ、workerが終わるまではlockを解放しない。
                            pass
                        raise
                except FileNotFoundError:
                    continue
                except OSError as ex:
                    logging.warning(
                        f'[RecordedSubtitleStream] Failed to remove orphaned subtitle cache: {cache_path}',
                        exc_info=ex,
                    )
                    continue

                deleted_count += 1
                if is_temporary is False:
                    cls.__forgetARIBTTMLMemoryCache(completed_path)

        if deleted_count > 0:
            logging.info(f'[RecordedSubtitleStream] Deleted {deleted_count} orphaned subtitle cache files.')

    @classmethod
    def __buildReachableCachePaths(cls, rows: list[dict[str, Any]]) -> set[Path]:
        """DB行から現実装が参照しうる完成cache pathを過不足なく再構築する。"""

        reachable_paths: set[Path] = set()
        for row in rows:
            file_hash = row.get('file_hash')
            playback_index_version = row.get('playback_index_version')
            subtitle_tracks = row.get('subtitle_tracks')
            if (
                not isinstance(file_hash, str)
                or cls._file_hash_pattern.fullmatch(file_hash) is None
                or (
                    playback_index_version is not None
                    and (type(playback_index_version) is not int or playback_index_version < 0)
                )
                or not isinstance(subtitle_tracks, list)
            ):
                raise ValueError('Recorded subtitle cache ownership fields are invalid.')
            index_version = playback_index_version or 0
            arib_ttml_program_numbers: set[int] = set()
            has_arib_ttml_track = False
            for track in subtitle_tracks:
                if not isinstance(track, dict):
                    raise ValueError('Recorded subtitle track is invalid.')
                subtitle_index = track.get('index')
                codec = track.get('codec')
                stream_index = track.get('stream_index')
                if (
                    type(subtitle_index) is not int
                    or subtitle_index < 0
                    or not isinstance(codec, str)
                    or (stream_index is not None and (type(stream_index) is not int or stream_index < 0))
                ):
                    raise ValueError('Recorded subtitle track cache fields are invalid.')
                normalized_codec = codec.lower()
                if normalized_codec == 'arib_ttml':
                    has_arib_ttml_track = True
                    program_number = track.get('program_number')
                    if program_number is not None:
                        if type(program_number) is not int or program_number < 0:
                            raise ValueError('Recorded ARIB-TTML program number is invalid.')
                        arib_ttml_program_numbers.add(program_number)
                elif 'arib' in normalized_codec:
                    if stream_index is not None:
                        reachable_paths.add(RECORDED_SUBTITLES_DIR / (
                            f'v{cls._cache_version}-{file_hash}-{subtitle_index}-{index_version}.arib.json'
                        ))
                elif normalized_codec not in cls._image_codecs and stream_index is not None:
                    reachable_paths.add(RECORDED_SUBTITLES_DIR / (
                        f'v{cls._cache_version}-{file_hash}-{subtitle_index}-{index_version}.vtt'
                    ))

            if has_arib_ttml_track is True:
                program_key = '-'.join(str(number) for number in sorted(arib_ttml_program_numbers)) \
                    if len(arib_ttml_program_numbers) > 0 else 'all'
                reachable_paths.add(RECORDED_SUBTITLES_DIR / (
                    f'ttml-v{cls._arib_ttml_cache_version}-{file_hash}-{index_version}-{program_key}.json'
                ))
        return reachable_paths

    @classmethod
    def __parseManagedCachePath(cls, cache_path: Path) -> tuple[Path, bool] | None:
        """厳密な管理cache名を完成pathとtmp種別へ分解する。"""

        is_temporary = cache_path.name.endswith('.tmp')
        completed_path = cache_path.with_name(cache_path.name[:-4]) if is_temporary is True else cache_path
        if (
            cls._cache_file_pattern.fullmatch(completed_path.name) is None
            and cls._arib_ttml_cache_file_pattern.fullmatch(completed_path.name) is None
        ):
            return None
        return (completed_path, is_temporary)

    @classmethod
    def __forgetARIBTTMLMemoryCache(cls, cache_path: Path) -> None:
        """削除済み永続cacheに対応するLRU索引とbyte counterを同期して除外する。"""

        cls._arib_ttml_memory_cache.pop(cache_path, None)
        cls._arib_ttml_memory_cache_bytes_total -= cls._arib_ttml_memory_cache_bytes.pop(cache_path, 0)

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
        stream_index = track.get('stream_index')
        if stream_index is None:
            return None
        cache_path = self.__getCachePath(subtitle_index, 'vtt')
        if cache_path.is_file():
            return await asyncio.to_thread(cache_path.read_bytes)
        async with self.__cacheLock(cache_path):
            if cache_path.is_file():
                return await asyncio.to_thread(cache_path.read_bytes)
            process = await asyncio.create_subprocess_exec(
                LIBRARY_PATH['FFmpeg8'], '-hide_banner', '-loglevel', 'error',
                *BuildKonomiTVBS4KMMTTLVInputArguments(self.recorded_video.container_format),
                '-i', self.recorded_video.file_path, '-map', f'0:{stream_index}',
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
        if (
            track is None or track['codec'].lower() == 'arib_ttml' or
            self.getTrackKind(subtitle_index) != 'ARIB' or end_time <= start_time
        ):
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

    async def getARIBTTMLRange(self, start_time: float, end_time: float) -> ARIBTTMLRange | None:
        """指定範囲のPTS付きARIB-TTML timed ID3とシーク復元packetを返す。"""

        if (
            end_time <= start_time or
            not any(track['codec'].lower() == 'arib_ttml' for track in self.recorded_video.subtitle_tracks)
        ):
            return None
        packet_index = await self.__loadARIBTTMLPackets()
        # liveと録画で同じJavaScript decoderを使うため、サーバー側ではID3/envelope/MFUを
        # 展開しない。seek復元はcomponentごとの直前PTSへ絞るが、同一PTSのfragment/subsampleは
        # MFU再構成に全て必要なのでまとめて返す。
        restore_packet_indices: list[int] = []
        for component_tag, component_pts in packet_index.pts_by_component.items():
            restore_end = bisect_left(component_pts, start_time)
            if restore_end == 0:
                continue
            latest_pts = component_pts[restore_end - 1]
            same_pts_start = bisect_left(component_pts, latest_pts, 0, restore_end)
            same_pts_end = bisect_right(component_pts, latest_pts, same_pts_start, restore_end)
            restore_packet_indices.extend(
                packet_index.packet_indices_by_component[component_tag][same_pts_start:same_pts_end]
            )
        restore_packet_indices.sort()
        restore_packets: list[ARIBTTMLPacket] = [
            {
                'pts': packet['pts'],
                'transport_timestamp': packet['transport_timestamp'],
                'component_tag': packet['component_tag'],
                'data': packet['data'],
                'is_restore_point': True,
            }
            for packet_index_value in restore_packet_indices
            for packet in [packet_index.packets[packet_index_value]]
        ]
        range_start = bisect_left(packet_index.pts, start_time)
        range_end = bisect_left(packet_index.pts, end_time, range_start)
        range_packets: list[ARIBTTMLPacket] = [
            {
                'pts': packet['pts'],
                'transport_timestamp': packet['transport_timestamp'],
                'component_tag': packet['component_tag'],
                'data': packet['data'],
                'is_restore_point': False,
            }
            for packet in packet_index.packets[range_start:range_end]
        ]
        return {
            'start_time': start_time,
            'end_time': end_time,
            'restore_packets': restore_packets,
            'packets': range_packets,
        }

    async def __loadARIBTTMLPackets(self) -> ARIBTTMLPacketIndex:
        """TS全体のARIB-TTML raw ID3 packet索引を永続化する。"""

        cache_path = self.__getARIBTTMLCachePath()
        memory_cache = self._arib_ttml_memory_cache.get(cache_path)
        if memory_cache is not None:
            self._arib_ttml_memory_cache.move_to_end(cache_path)
            return memory_cache
        async with self.__cacheLock(cache_path):
            memory_cache = self._arib_ttml_memory_cache.get(cache_path)
            if memory_cache is not None:
                self._arib_ttml_memory_cache.move_to_end(cache_path)
                return memory_cache
            if cache_path.is_file():
                try:
                    packets = cast(list[ARIBTTMLPacket], json.loads(await asyncio.to_thread(cache_path.read_text)))
                    return self.__rememberARIBTTMLPacketIndex(cache_path, packets)
                except (OSError, json.JSONDecodeError):
                    pass

            # fMP4映像タイムラインと同じ録画先頭0秒へPTSを揃えるため、コンテナの開始時刻だけを得る。
            process = await asyncio.create_subprocess_exec(
                LIBRARY_PATH['FFprobe8'], '-v', 'error',
                '-show_entries', 'format=start_time', '-of', 'json',
                *BuildKonomiTVBS4KMMTTLVInputArguments(self.recorded_video.container_format),
                self.recorded_video.file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                logging.error(
                    '[RecordedSubtitleStream] ARIB-TTML start time probe failed. '
                    f'[recorded_video_id: {self.recorded_video.id}, '
                    f'stderr: {stderr.decode(errors="ignore").strip()}]'
                )
                return self.__buildARIBTTMLPacketIndex([])
            try:
                probe = cast(dict[str, Any], json.loads(stdout))
                format_start_time = probe.get('format', {}).get('start_time')
                source_start_time = float(format_start_time) if format_start_time is not None else 0.0
            except (json.JSONDecodeError, TypeError, ValueError):
                return self.__buildARIBTTMLPacketIndex([])
            if self.recorded_video.container_format == MMT_TLV_CONTAINER_FORMAT:
                packets = await self.__extractARIBTTMLPacketsFromMMTTLV()
            else:
                packets = await asyncio.to_thread(
                    self.__extractARIBTTMLPacketsFromTS,
                    Path(self.recorded_video.file_path),
                    source_start_time,
                    self.__getARIBTTMLProgramNumbers(),
                )
            await self.__writeAtomic(cache_path, json.dumps(packets, separators=(',', ':')).encode())
            return self.__rememberARIBTTMLPacketIndex(cache_path, packets)

    @classmethod
    def __rememberARIBTTMLPacketIndex(
        cls,
        cache_path: Path,
        packets: list[ARIBTTMLPacket],
    ) -> ARIBTTMLPacketIndex:
        """PTS索引をLRUへ保持し、range要求ごとのJSON再読込を避ける。"""

        packet_index = cls.__buildARIBTTMLPacketIndex(packets)
        previous_size = cls._arib_ttml_memory_cache_bytes.pop(cache_path, 0)
        cls._arib_ttml_memory_cache_bytes_total -= previous_size
        cache_size = sum(len(packet['data']) + 64 for packet in packets)
        cls._arib_ttml_memory_cache[cache_path] = packet_index
        cls._arib_ttml_memory_cache_bytes[cache_path] = cache_size
        cls._arib_ttml_memory_cache_bytes_total += cache_size
        cls._arib_ttml_memory_cache.move_to_end(cache_path)
        while (
            len(cls._arib_ttml_memory_cache) > cls._arib_ttml_memory_cache_size or
            (
                cls._arib_ttml_memory_cache_bytes_total > cls._arib_ttml_memory_cache_bytes_limit and
                len(cls._arib_ttml_memory_cache) > 1
            )
        ):
            evicted_path, _evicted_index = cls._arib_ttml_memory_cache.popitem(last=False)
            cls._arib_ttml_memory_cache_bytes_total -= cls._arib_ttml_memory_cache_bytes.pop(evicted_path, 0)
        return packet_index

    @staticmethod
    def __buildARIBTTMLPacketIndex(packets: list[ARIBTTMLPacket]) -> ARIBTTMLPacketIndex:
        """raw packetを安定PTS順へ並べ、全体・component別の二分探索索引を作る。"""

        sorted_packets = sorted(packets, key=lambda packet: packet['pts'])
        pts = [packet['pts'] for packet in sorted_packets]
        packet_indices_by_component: dict[int, list[int]] = {}
        pts_by_component: dict[int, list[float]] = {}
        for packet_index, packet in enumerate(sorted_packets):
            component_tag = packet['component_tag']
            packet_indices_by_component.setdefault(component_tag, []).append(packet_index)
            pts_by_component.setdefault(component_tag, []).append(packet['pts'])
        return ARIBTTMLPacketIndex(
            packets=sorted_packets,
            pts=pts,
            packet_indices_by_component=packet_indices_by_component,
            pts_by_component=pts_by_component,
        )

    @staticmethod
    def __extractARIBTTMLPacketsFromTS(
        file_path: Path,
        source_start_time: float,
        program_numbers: set[int] | None = None,
    ) -> list[ARIBTTMLPacket]:
        """動的PMTを追跡しつつARIB-TTML timed ID3 PESを録画全体から索引化する。"""

        try:
            packet_size = TSKeyFrameSeeker.findStreamInfo(file_path).packet_size
        except (OSError, ValueError, RuntimeError):
            return []
        pat_parser = SectionParser(PATSection)
        pmt_parsers: dict[int, SectionParser[PMTSection]] = {}
        pmt_program_numbers: dict[int, int] = {}
        component_tags_by_pid: dict[int, set[int]] = {}
        pes_parsers: dict[int, PESParser[PES]] = {}
        packets: list[ARIBTTMLPacket] = []
        unwrap_targets: dict[int, int] = {}
        chunk_size = packet_size * 32_768
        with file_path.open('rb') as source:
            while chunk := source.read(chunk_size):
                for offset in range(0, len(chunk) - packet_size + 1, packet_size):
                    packet_offset = offset + (4 if packet_size == 192 else 0)
                    if packet_offset + 188 > len(chunk) or chunk[packet_offset] != 0x47:
                        continue
                    packet_pid = ((chunk[packet_offset + 1] & 0x1F) << 8) | chunk[packet_offset + 2]
                    # 5GB級録画でも初回索引を実用時間に収めるため、PAT・既知PMT・候補PES以外は
                    # 188byte packetを切り出す前にPID headerだけで除外する。
                    if packet_pid != 0x00 and packet_pid not in pmt_parsers and packet_pid not in pes_parsers:
                        continue
                    packet = chunk[packet_offset:packet_offset + 188]
                    if packet_pid == 0x00:
                        pat_parser.push(packet)
                        for pat in pat_parser:
                            if pat.CRC32() != 0:
                                continue
                            for program_number, pmt_pid in pat:
                                if program_number != 0:
                                    pmt_parsers.setdefault(pmt_pid, SectionParser(PMTSection))
                                    pmt_program_numbers[pmt_pid] = program_number
                        continue
                    pmt_parser = pmt_parsers.get(packet_pid)
                    if pmt_parser is not None:
                        pmt_parser.push(packet)
                        for pmt in pmt_parser:
                            if pmt.CRC32() != 0:
                                continue
                            program_number = pmt_program_numbers.get(packet_pid)
                            if program_numbers is not None and program_number not in program_numbers:
                                continue
                            for stream_type, elementary_pid, descriptors in pmt:
                                component_tag = TSKeyFrameSeeker.getARIBTTMLComponentTag(stream_type, descriptors)
                                if component_tag is None:
                                    continue
                                component_tags_by_pid.setdefault(elementary_pid, set()).add(component_tag)
                                pes_parsers.setdefault(elementary_pid, PESParser(PES))
                                unwrap_targets.setdefault(elementary_pid, round(source_start_time * 90_000))
                        continue
                    parser = pes_parsers.get(packet_pid)
                    if parser is None:
                        continue
                    try:
                        parser.push(packet)
                    except (IndexError, ValueError):
                        pes_parsers[packet_pid] = PESParser(PES)
                        continue
                    for pes in parser:
                        raw_pts = pes.pts()
                        data = bytes(pes.PES_packet_data())
                        component_tag = TSKeyFrameSeeker.getARIBTTMLTimedID3ComponentTag(data)
                        if (
                            raw_pts is None or component_tag is None or
                            component_tag not in component_tags_by_pid.get(packet_pid, set())
                        ):
                            continue
                        source_pts = TSKeyFrameSeeker.unwrapNear(raw_pts, unwrap_targets[packet_pid])
                        unwrap_targets[packet_pid] = source_pts
                        packets.append({
                            'pts': max(0.0, (source_pts / 90_000) - source_start_time),
                            # ARIB-TTMLのTMD=2時刻をライブと録画で同じ基準へ写像できるよう、
                            # unwrap・録画先頭補正前の33bit PES PTSも秒単位で保持する。
                            'transport_timestamp': (raw_pts & 0x1FFFFFFFF) / 90_000,
                            'component_tag': component_tag,
                            'data': base64.b64encode(data).decode(),
                            'is_restore_point': False,
                        })
        return packets

    async def __extractARIBTTMLPacketsFromMMTTLV(self) -> list[ARIBTTMLPacket]:
        """
        libaribtlv が公開する timed ID3 data stream を FFprobe 8 で全編索引化する。

        Args:
            なし。

        Returns:
            list[ARIBTTMLPacket]: 録画先頭 0 秒基準の raw ID3 packet 一覧。
        """

        process = await asyncio.create_subprocess_exec(
            LIBRARY_PATH['FFprobe8'],
            '-v', 'error',
            '-select_streams', 'd',
            '-show_packets',
            '-show_data',
            '-show_format',
            '-show_entries', 'packet=stream_index,pts_time,data:format=start_time',
            '-of', 'json',
            '-f', 'libaribtlv',
            self.recorded_video.file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            logging.error(
                '[RecordedSubtitleStream] MMT/TLV ARIB-TTML packet indexing failed. '
                f'[recorded_video_id: {self.recorded_video.id}, '
                f'stderr: {stderr.decode(errors="ignore").strip()}]'
            )
            return []
        try:
            probe = cast(dict[str, Any], json.loads(stdout))
            source_packets = probe.get('packets', [])
            source_start_time = float(probe.get('format', {}).get('start_time') or 0.0)
        except (json.JSONDecodeError, TypeError, ValueError):
            return []

        component_tags = {
            int(component_tag)
            for track in self.recorded_video.subtitle_tracks
            if track['codec'].lower() == 'arib_ttml'
            if (component_tag := track.get('component_tag')) is not None
        }
        stream_indexes = {
            int(stream_index)
            for track in self.recorded_video.subtitle_tracks
            if track['codec'].lower() == 'arib_ttml'
            if (stream_index := track.get('stream_index')) is not None
        }
        packets: list[ARIBTTMLPacket] = []
        for packet in source_packets if isinstance(source_packets, list) else []:
            if not isinstance(packet, dict) or packet.get('pts_time') is None or packet.get('data') is None:
                continue
            try:
                stream_index = int(packet['stream_index'])
                packet_pts = float(packet['pts_time'])
            except (KeyError, TypeError, ValueError):
                continue
            if stream_indexes and stream_index not in stream_indexes:
                continue
            raw_data = self.__decodeFFprobeData(str(packet['data']))
            component_tag = TSKeyFrameSeeker.getARIBTTMLTimedID3ComponentTag(raw_data)
            if component_tag is None or (component_tags and component_tag not in component_tags):
                continue
            transport_timestamp = self.__getARIBTTMLTransportTimestamp(raw_data)
            packets.append({
                'pts': max(0.0, packet_pts - source_start_time),
                'transport_timestamp': (
                    transport_timestamp
                    if transport_timestamp is not None
                    else packet_pts % ((1 << 33) / 90_000)
                ),
                'component_tag': component_tag,
                'data': base64.b64encode(raw_data).decode(),
                'is_restore_point': False,
            })
        return packets

    @staticmethod
    def __getARIBTTMLTransportTimestamp(data: bytes) -> float | None:
        """
        KonomiTV ARIB-TTML envelope v2 から 33bit source PTS を秒へ変換する。

        Args:
            data (bytes): timed ID3 tag 全体。

        Returns:
            float | None: source PTS の秒表現。v2 でなければ None。
        """

        marker = b'arib-ttml.js\x00'
        marker_offset = data.find(marker)
        while marker_offset >= 0:
            envelope_offset = marker_offset + len(marker)
            if envelope_offset + 28 <= len(data) and data[envelope_offset] == 2:
                source_pts = int.from_bytes(data[envelope_offset + 20:envelope_offset + 28], 'big')
                return (source_pts & 0x1FFFFFFFF) / 90_000
            marker_offset = data.find(marker, marker_offset + 1)
        return None

    async def __loadARIBPackets(self, subtitle_index: int, track: SubtitleTrack) -> list[ARIBSubtitlePacket]:
        """FFprobe 8の全packet索引をサーバーデータ領域へ永続化する。"""

        stream_index = track.get('stream_index')
        if stream_index is None:
            return []
        cache_path = self.__getCachePath(subtitle_index, 'arib.json')
        async with self.__cacheLock(cache_path):
            if cache_path.is_file():
                try:
                    return cast(list[ARIBSubtitlePacket], json.loads(await asyncio.to_thread(cache_path.read_text)))
                except (OSError, json.JSONDecodeError):
                    pass
            process = await asyncio.create_subprocess_exec(
                LIBRARY_PATH['FFprobe8'], '-v', 'error', '-select_streams', str(stream_index),
                '-show_packets', '-show_data', '-show_format',
                '-show_entries', 'packet=pts_time,duration_time,data:format=start_time', '-of', 'json',
                *BuildKonomiTVBS4KMMTTLVInputArguments(self.recorded_video.container_format),
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

    def __getARIBTTMLCachePath(self) -> Path:
        """ARIB-TTML集約索引のキャッシュパスを返す。"""

        index_version = self.recorded_video.playback_index_version or 0
        program_numbers = self.__getARIBTTMLProgramNumbers()
        program_key = '-'.join(str(number) for number in sorted(program_numbers)) if program_numbers is not None else 'all'
        return RECORDED_SUBTITLES_DIR / (
            f'ttml-v{self._arib_ttml_cache_version}-{self.recorded_video.file_hash}-{index_version}-{program_key}.json'
        )

    def __getARIBTTMLProgramNumbers(self) -> set[int] | None:
        """録画対象serviceのprogram numberをTTML論理trackから得る。"""

        program_numbers = {
            int(program_number)
            for track in self.recorded_video.subtitle_tracks
            if track['codec'].lower() == 'arib_ttml'
            if (program_number := track.get('program_number')) is not None
        }
        return program_numbers if len(program_numbers) > 0 else None

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
