from __future__ import annotations

import asyncio
import json
import math
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from fastapi import HTTPException, status

from app import logging
from app.config import Config
from app.constants import LIBRARY_PATH, QUALITY, QUALITY_TYPES
from app.models.RecordedProgram import RecordedProgram
from app.streams.RecordedFMP4Cache import RecordedFMP4CacheManager, RecordedFMP4Variant
from app.streams.RecordedPlaybackCapabilities import (
    RecordedPlaybackBackend,
    RecordedPlaybackCapabilityProbe,
    RecordedPlaybackEncoder,
)
from app.streams.RecordedSubtitleStream import RecordedSubtitleStream
from app.streams.StreamEncodingOptions import StreamEncodingOptions
from app.streams.VideoSegmentPlanner import VideoSegmentPlanner


@dataclass(frozen=True, slots=True)
class RecordedFMP4Segment:
    """録画先頭0秒基準のfMP4セグメント時間範囲を表す。"""

    sequence: int
    start_time: float
    duration: float
    generation: int
    audio_generation: int


@dataclass(frozen=True, slots=True)
class RecordedAudioRendition:
    """HLSへ公開する論理音声レンディションを表す。"""

    id: str
    track_index: int
    stream_index: int
    channel: Literal['all', 'main', 'sub']
    name: str
    language: str
    pid: int | None = None


class RecordedFMP4Stream:
    """FFmpeg 8で録画映像fMP4をオンデマンド生成する視聴セッション。"""

    _cpu_semaphore: ClassVar[asyncio.Semaphore] = asyncio.Semaphore(2)
    _gpu_semaphores: ClassVar[dict[str, asyncio.Semaphore]] = {}
    _instances: ClassVar[dict[str, RecordedFMP4Stream]] = {}
    SESSION_TIMEOUT: ClassVar[float] = 30.0
    SEEK_PREROLL_SECONDS: ClassVar[float] = 10.0

    # セッションを識別し、ルーターの後続API要求とキャッシュ参照に利用する。
    session_id: str
    # 生成元ファイルとDB上の再生インデックスを参照する録画番組。
    recorded_program: RecordedProgram
    # 解像度・ビットレートをQUALITYから引くための画質キー。
    quality: QUALITY_TYPES
    # codec・24fpsなど、初回プレイリスト要求で固定される生成条件。
    encoding_options: StreamEncodingOptions
    # 全APIが同じ時間境界を参照するため、初期化時に確定したセグメント計画。
    _segments: list[RecordedFMP4Segment]
    # destroy()で一括releaseする、このセッションが参照済みのキャッシュパス。
    _referenced_paths: set[Path]
    # Buffer APIへ生成済み範囲を返すための完了シーケンス集合。
    _completed_sequences: set[int]
    # エンコード・fsync中の要求をセッションタイムアウトから保護する参照数。
    _active_operations: int
    # Keep-Aliveが途切れたセッションを破棄するイベントループタイマー。
    _destroy_handle: asyncio.TimerHandle

    def __new__(
        cls,
        session_id: str,
        recorded_program: RecordedProgram,
        quality: QUALITY_TYPES,
        encoding_options: StreamEncodingOptions | None = None,
        is_new_session_allowed: bool = False,
    ) -> RecordedFMP4Stream:
        """session ID単位で単一の新録画視聴セッションを返す。"""

        if session_id not in cls._instances:
            if is_new_session_allowed is False or encoding_options is None:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Session does not exist')
            instance = super().__new__(cls)
            instance.session_id = session_id
            instance.recorded_program = recorded_program
            instance.quality = quality
            instance.encoding_options = encoding_options
            instance._segments = instance.__buildSegments()
            instance._referenced_paths = set()
            instance._completed_sequences = set()
            instance._active_operations = 0
            instance._destroy_handle = asyncio.get_running_loop().call_later(
                cls.SESSION_TIMEOUT,
                lambda: asyncio.create_task(instance.__destroyIfIdle()),
            )
            cls._instances[session_id] = instance
        instance = cls._instances[session_id]
        if instance.recorded_program.id != recorded_program.id or instance.quality != quality:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Session conditions mismatch')
        return instance

    def keepAlive(self) -> None:
        """視聴中のセッション破棄タイマーを延長する。"""

        self._destroy_handle.cancel()
        self._destroy_handle = asyncio.get_running_loop().call_later(
            self.SESSION_TIMEOUT,
            lambda: asyncio.create_task(self.__destroyIfIdle()),
        )

    async def __destroyIfIdle(self) -> None:
        """生成中の要求があればセッション破棄を延期する。"""

        if self._active_operations > 0:
            self.keepAlive()
            return
        await self.destroy()

    @asynccontextmanager
    async def __activeOperation(self) -> AsyncIterator[None]:
        """長時間のエンコードやfsync中にセッションタイムアウトを防ぐ。"""

        self._active_operations += 1
        self.keepAlive()
        try:
            yield
        finally:
            self._active_operations = max(0, self._active_operations - 1)
            if self._instances.get(self.session_id) is self:
                self.keepAlive()

    @classmethod
    def hasActiveSessions(cls) -> bool:
        """新fMP4経路の録画視聴セッションが存在するかを返す。"""

        return len(cls._instances) > 0

    @classmethod
    def hasSession(cls, session_id: str) -> bool:
        """指定session IDの新fMP4視聴セッションが存在するかを返す。

        Args:
            session_id: クライアントが発行した視聴セッションID。

        Returns:
            セッションが存在する場合はTrue。
        """

        return session_id in cls._instances

    @property
    def log_prefix(self) -> str:
        """既存録画経路と同じ形式のログ接頭辞を返す。"""

        return f'[Video-fMP4: {self.recorded_program.id}/{self.session_id}/{self.quality}]'

    def getBufferRange(self) -> tuple[float, float]:
        """生成済み映像fragmentの連続範囲を返す。"""

        if len(self._completed_sequences) == 0:
            return (0.0, 0.0)
        completed = [self._segments[sequence] for sequence in sorted(self._completed_sequences)]
        return (completed[0].start_time, completed[-1].start_time + completed[-1].duration)

    async def destroy(self) -> None:
        """セッション参照を解放し、キャッシュの60秒削除猶予を開始する。"""

        if self._instances.get(self.session_id) is not self:
            return
        self._destroy_handle.cancel()
        self._instances.pop(self.session_id, None)
        for cache_path in self._referenced_paths:
            RecordedFMP4CacheManager.release(cache_path, self.session_id)
        self._referenced_paths.clear()

    async def getMasterPlaylist(self, cache_key: str | None = None) -> str:
        """映像fMP4メディアプレイリストを参照するHLSマスターを返す。"""

        async with self.__activeOperation():
            return await self.__getMasterPlaylist(cache_key)

    async def __getMasterPlaylist(self, cache_key: str | None = None) -> str:
        """実initを生成してHLSマスターを組み立てる。"""

        self.keepAlive()
        cache_key = cache_key or uuid.uuid4().hex[:8]
        quality = QUALITY[self.quality]
        bandwidth = int(float(quality.video_bitrate_max.rstrip('K')) * 1000)
        lines = ['#EXTM3U', '#EXT-X-VERSION:7']
        renditions = self.getAudioRenditions()
        for index, rendition in enumerate(renditions):
            default = 'YES' if index == 0 else 'NO'
            language = rendition.language.replace('"', '')
            name = rendition.name.replace('"', '')
            lines.append(
                '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",'
                f'NAME="{name}",DEFAULT={default},AUTOSELECT=YES,LANGUAGE="{language}",'
                f'URI="audio/{rendition.id}/playlist?session_id={self.session_id}&cache_key={cache_key}'
                f'&{self.getCodecQuery()}"'
            )
        subtitle_group_enabled = False
        subtitle_stream = RecordedSubtitleStream(self.recorded_program.recorded_video)
        for track in self.recorded_program.recorded_video.subtitle_tracks:
            if subtitle_stream.getTrackKind(track['index']) != 'Text':
                continue
            subtitle_group_enabled = True
            name = (track.get('title') or track.get('language') or f'Subtitle {track["index"]}').replace('"', '')
            lines.append(
                '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subtitles",'
                f'NAME="{name}",DEFAULT=NO,AUTOSELECT=YES,LANGUAGE="{track.get("language") or "und"}",'
                f'URI="subtitle/{track["index"]}/playlist?session_id={self.session_id}&cache_key={cache_key}'
                f'&{self.getCodecQuery()}"'
            )
        # CODECSは要求条件から推測せず、実際に配信するinit内のconfiguration boxから得る。
        # エンコーダーが選択したlevelやconstraintも実データと一致する値をブラウザーへ渡す。
        generation_segments = {
            segment.generation: segment
            for segment in self._segments
        }
        codec_strings: list[str] = []
        for segment in generation_segments.values():
            init_segment = await self.getVideoInitSegment(segment.generation, segment.sequence)
            if init_segment is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail='Video initialization segment could not be generated',
                )
            codec_string = self.extractCodecString(init_segment)
            if codec_string is not None:
                codec_strings.append(codec_string)
        codec_string = self.selectHighestCodecLevel(codec_strings)
        if codec_string is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    'code': 'CodecMismatch',
                    'message': 'The generated initialization segment has no supported codec configuration.',
                },
            )
        stream_attributes = [f'BANDWIDTH={bandwidth + 256_000}', f'CODECS="{codec_string},mp4a.40.2"']
        if len(renditions) > 0:
            stream_attributes.append('AUDIO="audio"')
        if subtitle_group_enabled:
            stream_attributes.append('SUBTITLES="subtitles"')
        lines.append('#EXT-X-STREAM-INF:' + ','.join(stream_attributes))
        lines.append(f'video/playlist?session_id={self.session_id}&cache_key={cache_key}&{self.getCodecQuery()}')
        return '\n'.join(lines) + '\n'

    @staticmethod
    def selectHighestCodecLevel(codec_strings: list[str]) -> str | None:
        """同一映像codecの実init群から最も高いlevelのcodec stringを返す。"""

        if len(codec_strings) == 0:
            return None

        def GetLevel(codec_string: str) -> int:
            if codec_string.startswith('avc1.') and len(codec_string) >= 11:
                return int(codec_string[-2:], 16)
            parts = codec_string.split('.')
            if codec_string.startswith('hvc1.'):
                level = next((part[1:] for part in parts if part.startswith('L')), '0')
                return int(level)
            if codec_string.startswith('vp09.') and len(parts) >= 3:
                return int(parts[2])
            if codec_string.startswith('av01.') and len(parts) >= 3:
                return int(parts[2][:-1])
            return 0

        return max(codec_strings, key=GetLevel)

    @staticmethod
    def extractCodecString(init_segment: bytes) -> str | None:
        """init内のcodec configuration boxからRFC 6381 codec stringを取得する。

        Args:
            init_segment: FFmpegが生成したfMP4初期化セグメント。

        Returns:
            AVC / HEVC / VP9 / AV1のcodec string。対応boxがない場合はNone。
        """

        # configuration boxはVisualSampleEntry内にネストされるため、親固有の固定長領域に
        # 依存せず、size/typeヘッダーがinit範囲内に収まる候補だけを採用する。
        def FindBox(box_type: bytes) -> bytes | None:
            """検証済みboxのpayloadを返す。"""

            search_offset = 0
            while True:
                type_offset = init_segment.find(box_type, search_offset)
                if type_offset < 0:
                    return None
                if type_offset >= 4:
                    size = int.from_bytes(init_segment[type_offset - 4:type_offset], 'big')
                    box_start = type_offset - 4
                    if size >= 8 and box_start + size <= len(init_segment):
                        return init_segment[type_offset + 4:box_start + size]
                search_offset = type_offset + 1

        # AVCDecoderConfigurationRecordのprofile/compatibility/levelは先頭4 byteにある。
        avcc = FindBox(b'avcC')
        if avcc is not None and len(avcc) >= 4 and avcc[0] == 1:
            return f'avc1.{avcc[1]:02X}{avcc[2]:02X}{avcc[3]:02X}'

        # HEVCのcompatibility flagsはcodec stringでbit順が逆になる。constraint bytesは
        # 末尾のゼロだけを省略し、実際のprofile/tier/levelとともに表現する。
        hvcc = FindBox(b'hvcC')
        if hvcc is not None and len(hvcc) >= 13 and hvcc[0] == 1:
            profile_space = ('', 'A', 'B', 'C')[hvcc[1] >> 6]
            profile_idc = hvcc[1] & 0x1F
            compatibility = int.from_bytes(hvcc[2:6], 'big')
            compatibility = int(f'{compatibility:032b}'[::-1], 2)
            tier = 'H' if hvcc[1] & 0x20 else 'L'
            constraints = hvcc[6:12].rstrip(b'\x00')
            constraint_suffix = ''.join(f'.{value:X}' for value in constraints)
            return (
                f'hvc1.{profile_space}{profile_idc}.{compatibility:X}.'
                f'{tier}{hvcc[12]}{constraint_suffix}'
            )

        # VPCodecConfigurationBoxのFullBoxヘッダー直後にある実profile/level/bit depthを使う。
        vpcc = FindBox(b'vpcC')
        if vpcc is not None and len(vpcc) >= 7:
            return f'vp09.{vpcc[4]:02}.{vpcc[5]:02}.{vpcc[6] >> 4:02}'

        # AV1CodecConfigurationRecordから実profile/level/tier/bit depthを使う。
        av1c = FindBox(b'av1C')
        if av1c is not None and len(av1c) >= 3 and av1c[0] & 0x80:
            profile = av1c[1] >> 5
            level = av1c[1] & 0x1F
            tier = 'H' if av1c[2] & 0x80 else 'M'
            bit_depth = 10 if av1c[2] & 0x40 else 8
            return f'av01.{profile}.{level:02}{tier}.{bit_depth:02}'

        return None

    def getVideoPlaylist(self, cache_key: str | None = None) -> str:
        """世代別initと映像fragmentを並べたVODメディアプレイリストを返す。"""

        self.keepAlive()
        cache_key = cache_key or uuid.uuid4().hex[:8]
        lines = [
            '#EXTM3U',
            '#EXT-X-VERSION:7',
            '#EXT-X-PLAYLIST-TYPE:VOD',
            '#EXT-X-INDEPENDENT-SEGMENTS',
            f'#EXT-X-TARGETDURATION:{math.ceil(max(segment.duration for segment in self._segments))}',
        ]
        previous_generation_key: tuple[int, int] | None = None
        for segment in self._segments:
            # 代替音声と映像でcontinuity counterがずれると、hls.jsのAudioStreamControllerが
            # 対応する映像init PTSを見つけられずWAITING_INIT_PTSのまま停止する。
            # どちらかの構成世代が変わる境界を両メディアプレイリストへ同じ順序で出す。
            generation_key = (segment.generation, segment.audio_generation)
            if previous_generation_key != generation_key:
                if previous_generation_key is not None:
                    lines.append('#EXT-X-DISCONTINUITY')
                lines.append(
                    f'#EXT-X-MAP:URI="init?session_id={self.session_id}&generation={segment.generation}'
                    f'&sequence={segment.sequence}&cache_key={cache_key}&{self.getCodecQuery()}"'
                )
                previous_generation_key = generation_key
            lines.append(f'#EXTINF:{segment.duration:.6f},')
            lines.append(
                f'segment?session_id={self.session_id}&sequence={segment.sequence}&cache_key={cache_key}'
                f'&{self.getCodecQuery()}'
            )
        lines.append('#EXT-X-ENDLIST')
        return '\n'.join(lines) + '\n'

    def getAudioRenditions(self) -> list[RecordedAudioRendition]:
        """DBの音声トラックからDual Monoを展開したHLSレンディションを返す。"""

        renditions: list[RecordedAudioRendition] = []
        legacy_stream_offset = 1 if self.recorded_program.recorded_video.container_format == 'MPEG-TS' else 0
        for fallback_index, track in enumerate(self.recorded_program.recorded_video.audio_tracks, start=1):
            track_index = int(track.get('index', fallback_index))
            stream_index = int(track.get('stream_index', track_index + legacy_stream_offset))
            language = track.get('language') or 'und'
            title = track.get('title') or ''
            if track.get('is_dual_mono') is True:
                languages = [item.strip() for item in language.split('+') if item.strip()]
                main_language = languages[0] if languages else 'ja'
                sub_language = languages[1] if len(languages) > 1 else 'und'
                renditions.extend([
                    RecordedAudioRendition(
                        f'{track_index}-main', track_index, stream_index, 'main',
                        f'Track{len(renditions) + 1} {main_language} (Monaural) 主音声', main_language,
                        track.get('pid'),
                    ),
                    RecordedAudioRendition(
                        f'{track_index}-sub', track_index, stream_index, 'sub',
                        f'Track{len(renditions) + 2} {sub_language} (Monaural) 副音声', sub_language,
                        track.get('pid'),
                    ),
                ])
            else:
                renditions.append(RecordedAudioRendition(
                    str(track_index), track_index, stream_index, 'all',
                    title or f'Track{len(renditions) + 1} {language}', language, track.get('pid'),
                ))
        return renditions

    def getAudioPlaylist(self, rendition_id: str, cache_key: str | None = None) -> str:
        """映像と同じ境界を使うAAC fMP4レンディションプレイリストを返す。"""

        rendition = self.__getAudioRendition(rendition_id)
        if rendition is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Audio rendition was not found')
        self.keepAlive()
        cache_key = cache_key or uuid.uuid4().hex[:8]
        lines = [
            '#EXTM3U', '#EXT-X-VERSION:7', '#EXT-X-PLAYLIST-TYPE:VOD',
            f'#EXT-X-TARGETDURATION:{math.ceil(max(segment.duration for segment in self._segments))}',
        ]
        previous_generation_key: tuple[int, int] | None = None
        for segment in self._segments:
            generation_key = (segment.generation, segment.audio_generation)
            if generation_key != previous_generation_key:
                if previous_generation_key is not None:
                    lines.append('#EXT-X-DISCONTINUITY')
                lines.append(
                    f'#EXT-X-MAP:URI="init?session_id={self.session_id}&sequence={segment.sequence}'
                    f'&cache_key={cache_key}&{self.getCodecQuery()}"'
                )
                previous_generation_key = generation_key
            lines.append(f'#EXTINF:{segment.duration:.6f},')
            lines.append(
                f'segment?session_id={self.session_id}&sequence={segment.sequence}&cache_key={cache_key}'
                f'&{self.getCodecQuery()}'
            )
        lines.append('#EXT-X-ENDLIST')
        return '\n'.join(lines) + '\n'

    async def getAudioInitSegment(self, rendition_id: str, sequence: int) -> bytes | None:
        """指定レンディションのAAC初期化セグメントを返す。"""

        segment = self.__getSegment(sequence)
        rendition = self.__getAudioRendition(rendition_id)
        if segment is None or rendition is None:
            return None
        await self.getAudioSegment(rendition_id, sequence)
        init_path = self.__buildAudioCachePath(segment, rendition, is_init=True)
        return await asyncio.to_thread(init_path.read_bytes) if init_path.is_file() else None

    async def getAudioSegment(self, rendition_id: str, sequence: int) -> bytes | None:
        """映像とは独立して指定音声のAAC fragmentを生成または再利用する。"""

        async with self.__activeOperation():
            return await self.__getAudioSegment(rendition_id, sequence)

    async def __getAudioSegment(self, rendition_id: str, sequence: int) -> bytes | None:
        """AAC fragment生成の本体処理を行う。"""

        self.keepAlive()
        segment = self.__getSegment(sequence)
        rendition = self.__getAudioRendition(rendition_id)
        if segment is None or rendition is None:
            return None
        segment_path = self.__buildAudioCachePath(segment, rendition, is_init=False)
        init_path = self.__buildAudioCachePath(segment, rendition, is_init=True)
        segment_lock = await self.__acquire(segment_path)
        await self.__acquire(init_path)
        async with segment_lock:
            if segment_path.is_file():
                return await asyncio.to_thread(segment_path.read_bytes)
            await self.__encodeAudioSegment(segment, rendition, init_path, segment_path)
            return await asyncio.to_thread(segment_path.read_bytes) if segment_path.is_file() else None

    async def getVideoInitSegment(self, generation: int, sequence: int) -> bytes | None:
        """指定世代の実エンコード結果から得た初期化セグメントを返す。"""

        segment = self.__getSegment(sequence)
        if segment is None or segment.generation != generation:
            return None
        await self.getVideoSegment(sequence)
        init_path = self.__buildCachePath(segment, is_init=True)
        if init_path.is_file() is False:
            return None
        return await asyncio.to_thread(init_path.read_bytes)

    async def getVideoSegment(self, sequence: int) -> bytes | None:
        """要求シーケンスのAVC映像fragmentを生成またはキャッシュから返す。"""

        async with self.__activeOperation():
            return await self.__getVideoSegment(sequence)

    async def __getVideoSegment(self, sequence: int) -> bytes | None:
        """映像fragment生成の本体処理を行う。"""

        self.keepAlive()
        segment = self.__getSegment(sequence)
        if segment is None:
            return None
        backend = self.__getBackend()
        if RecordedPlaybackBackend.isCombinationSupported(
            backend,
            self.encoding_options.video_codec,
            self.encoding_options.video_bit_depth,
        ) is False:
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = {'code': 'UnsupportedCombination', 'message': 'The requested recorded encoding is unavailable.'},
            )
        segment_path = self.__buildCachePath(segment, is_init=False)
        init_path = self.__buildCachePath(segment, is_init=True)
        segment_lock = await self.__acquire(segment_path)
        await self.__acquire(init_path)
        async with segment_lock:
            if segment_path.is_file():
                self._completed_sequences.add(sequence)
                return await asyncio.to_thread(segment_path.read_bytes)
            await self.__encodeSegment(segment, init_path, segment_path)
            if segment_path.is_file() is False:
                return None
            self._completed_sequences.add(sequence)
            return await asyncio.to_thread(segment_path.read_bytes)

    def __buildSegments(self) -> list[RecordedFMP4Segment]:
        """通常約6秒境界と映像構成変化点の和集合からセグメント計画を作る。"""

        recorded_video = self.recorded_program.recorded_video
        duration = recorded_video.duration
        segment_duration = VideoSegmentPlanner.computeSegmentDurationSeconds(recorded_video.video_frame_rate or 0)
        boundaries = {0.0, duration}
        boundaries.update(
            min(duration, index * segment_duration)
            for index in range(1, max(1, math.ceil(duration / segment_duration)))
        )
        timeline = recorded_video.video_stream_timeline or []
        for entry in timeline:
            boundaries.add(max(0.0, min(duration, float(entry['start_time']))))
            boundaries.add(max(0.0, min(duration, float(entry['end_time']))))
        audio_timeline = recorded_video.audio_track_timeline
        for entry in audio_timeline:
            boundaries.add(max(0.0, min(duration, float(entry['start_time']))))
            boundaries.add(max(0.0, min(duration, float(entry['end_time']))))
        ordered_boundaries = sorted(boundaries)
        segments: list[RecordedFMP4Segment] = []
        previous_signature: tuple[object, ...] | None = None
        previous_audio_signature: tuple[object, ...] | None = None
        generation = -1
        audio_generation = -1
        for sequence, start_time in enumerate(ordered_boundaries[:-1]):
            entry = next(
                (item for item in timeline if float(item['start_time']) <= start_time < float(item['end_time'])),
                None,
            )
            signature: tuple[object, ...] = (
                entry.get('stream_index') if entry else 0,
                entry.get('codec') if entry else recorded_video.video_codec,
                entry.get('profile') if entry else None,
                entry.get('width') if entry else recorded_video.video_resolution_width,
                entry.get('height') if entry else recorded_video.video_resolution_height,
                entry.get('frame_rate') if entry else recorded_video.video_frame_rate,
                entry.get('scan_type') if entry else recorded_video.video_scan_type,
                entry.get('bit_depth') if entry else None,
                entry.get('color_range') if entry else None,
                entry.get('color_space') if entry else None,
                entry.get('color_primaries') if entry else None,
                entry.get('color_transfer') if entry else None,
                json.dumps(entry.get('mastering_display_metadata'), sort_keys=True) if entry else None,
                json.dumps(entry.get('content_light_level'), sort_keys=True) if entry else None,
            )
            if signature != previous_signature:
                generation += 1
                previous_signature = signature
            audio_entry = next(
                (
                    item for item in audio_timeline
                    if float(item['start_time']) <= start_time < float(item['end_time'])
                ),
                None,
            )
            audio_signature = tuple(
                (
                    track.get('index'), track.get('pid'), track.get('stream_index'), track.get('codec'),
                    track.get('channel'), track.get('sampling_rate'),
                )
                for track in (audio_entry['tracks'] if audio_entry is not None else recorded_video.audio_tracks)
            )
            if audio_signature != previous_audio_signature:
                audio_generation += 1
                previous_audio_signature = audio_signature
            segments.append(RecordedFMP4Segment(
                sequence = sequence,
                start_time = start_time,
                duration = max(0.001, ordered_boundaries[sequence + 1] - start_time),
                generation = generation,
                audio_generation = audio_generation,
            ))
        return segments

    async def __encodeSegment(
        self,
        segment: RecordedFMP4Segment,
        init_path: Path,
        segment_path: Path,
    ) -> None:
        """FFmpeg 8で自己完結fMP4を生成し、initとfragmentへ分離する。"""

        quality = QUALITY[self.quality]
        backend = self.__getBackend()
        codec = self.encoding_options.video_codec
        bit_depth = self.encoding_options.video_bit_depth
        spec = RecordedPlaybackBackend.getCodecSpec(codec, bit_depth)
        encoder_name = RecordedPlaybackBackend.getEncoderName(backend, codec)
        if encoder_name is None:
            return
        input_seek, trim_start, input_duration = self.computeInputSeekWindow(
            segment.start_time,
            segment.duration,
        )
        filters: list[str] = []
        timeline = self.recorded_program.recorded_video.video_stream_timeline or []
        entry = next(
            (item for item in timeline if float(item['start_time']) <= segment.start_time < float(item['end_time'])),
            None,
        )
        scan_type = entry.get('scan_type') if entry else self.recorded_program.recorded_video.video_scan_type
        is_interlaced = scan_type == 'Interlaced'
        if is_interlaced:
            if backend == 'FFmpeg':
                filters.append(f'bwdif=mode={"send_field" if quality.is_60fps else "send_frame"}:parity=auto:deint=interlaced')
        elif scan_type not in ('Progressive', None):
            logging.warning(
                '[RecordedFMP4Stream] video scan type is unknown; deinterlace is disabled. '
                f'[recorded_video_id: {self.recorded_program.recorded_video.id}, sequence: {segment.sequence}]'
            )
        if backend == 'FFmpeg':
            filters += [
                f'scale=w={quality.width}:h={quality.height}:force_original_aspect_ratio=decrease',
                f'pad={quality.width}:{quality.height}:(ow-iw)/2:(oh-ih)/2',
            ]
            if is_interlaced and self.encoding_options.is_24fps_mode_enabled:
                filters += ['pullup', 'dejudder']
            filters += [
                f'trim=start={trim_start:.6f}:duration={segment.duration:.6f}',
                'setpts=PTS-STARTPTS',
                f'format={spec.pixel_format}',
            ]

        command = [
            RecordedPlaybackBackend.getExecutable(backend), '-hide_banner', '-loglevel', 'error',
        ]
        device: str | None = None
        if backend in ('QSVEncC', 'VCEEncC'):
            selected_device = RecordedPlaybackCapabilityProbe.getSelectedDevice(backend)
            devices = [selected_device] if selected_device is not None else \
                RecordedPlaybackBackend.discoverRenderDevices(backend)
            if len(devices) == 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={'code': 'DeviceUnavailable', 'message': 'A compatible render device was not found.'},
                )
            device = devices[0]
        if backend == 'QSVEncC':
            command += [
                '-init_hw_device', f'qsv=recorded_qsv:{device}', '-filter_hw_device', 'recorded_qsv',
                '-hwaccel', 'qsv', '-hwaccel_output_format', 'qsv',
            ]
        elif backend == 'NVEncC':
            command += [
                '-init_hw_device', 'cuda=recorded_cuda:0', '-filter_hw_device', 'recorded_cuda',
                '-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda',
            ]
        elif backend == 'VCEEncC':
            command += [
                '-init_hw_device', f'vaapi=recorded_vaapi:{device}', '-filter_hw_device', 'recorded_vaapi',
                '-hwaccel', 'vaapi', '-hwaccel_device', 'recorded_vaapi', '-hwaccel_output_format', 'vaapi',
            ]
        command += [
            # TSを要求位置から直接復号すると、直前キーフレームの参照画像を失い、次の
            # 復号可能なキーフレームまで数秒飛ぶ。手前から復号してfilterで要求位置へ切る。
            '-ss', f'{input_seek:.6f}',
            '-i', self.recorded_program.recorded_video.file_path,
            '-t', f'{input_duration:.6f}',
            '-map', '0:v:0', '-an',
        ]
        if backend == 'QSVEncC':
            if is_interlaced:
                filters.append(
                    f'vpp_qsv=deinterlace=advanced:rate={"field" if quality.is_60fps else "frame"}:'
                    f'w={quality.width}:h={quality.height}:format={spec.encoder_pixel_format}'
                )
            else:
                filters.append(
                    f'vpp_qsv=w={quality.width}:h={quality.height}:format={spec.encoder_pixel_format}'
                )
            if is_interlaced and self.encoding_options.is_24fps_mode_enabled:
                filters += [
                    f'hwdownload,format={spec.encoder_pixel_format}', 'pullup', 'dejudder',
                    f'format={spec.encoder_pixel_format}', 'hwupload=extra_hw_frames=32',
                ]
            filters += [
                f'trim=start={trim_start:.6f}:duration={segment.duration:.6f}',
                'setpts=PTS-STARTPTS',
            ]
        elif backend == 'NVEncC':
            if is_interlaced:
                filters.append(f'bwdif_cuda=mode={"send_field" if quality.is_60fps else "send_frame"}:parity=auto')
            filters.append(f'scale_cuda=w={quality.width}:h={quality.height}:format={spec.encoder_pixel_format}')
            if is_interlaced and self.encoding_options.is_24fps_mode_enabled:
                filters += [
                    f'hwdownload,format={spec.encoder_pixel_format}', 'pullup', 'dejudder',
                    f'format={spec.encoder_pixel_format}', 'hwupload_cuda',
                ]
            filters += [
                f'trim=start={trim_start:.6f}:duration={segment.duration:.6f}',
                'setpts=PTS-STARTPTS',
            ]
        elif backend == 'VCEEncC':
            if is_interlaced:
                filters.append(
                    f'deinterlace_vaapi=rate={"field" if quality.is_60fps else "frame"}:auto=1'
                )
            filters += [
                f'scale_vaapi=w={quality.width}:h={quality.height}:format={spec.encoder_pixel_format}',
                f'hwdownload,format={spec.encoder_pixel_format}',
            ]
            if is_interlaced and self.encoding_options.is_24fps_mode_enabled:
                filters += [
                    'pullup', 'dejudder', f'format={spec.encoder_pixel_format}', 'hwupload',
                    f'scale_vaapi=w={quality.width}:h={quality.height}:format={spec.encoder_pixel_format}',
                    f'hwdownload,format={spec.encoder_pixel_format}',
                ]
            filters += [
                f'trim=start={trim_start:.6f}:duration={segment.duration:.6f}',
                'setpts=PTS-STARTPTS',
            ]
        command += [
            '-vf', ','.join(filters),
            '-c:v', encoder_name,
        ]
        if backend == 'FFmpeg':
            command += ['-pix_fmt', spec.pixel_format]
        elif backend == 'NVEncC':
            command += ['-pix_fmt', 'cuda']
        elif backend == 'VCEEncC':
            command += ['-pix_fmt', spec.encoder_pixel_format]
        command += self.__getProfileArguments(backend, codec, bit_depth)
        command += RecordedPlaybackBackend.getTuningArguments(backend, codec)
        if codec == 'hevc':
            command += ['-tag:v', 'hvc1']
        command += [
            '-b:v', quality.video_bitrate, '-maxrate', quality.video_bitrate_max,
            '-bufsize', str(int(float(quality.video_bitrate_max.rstrip('K')) * 2)) + 'K',
            # libaom-av1は最初のpacketでsequence headerを返すため、empty_moovでは空のav1Cが
            # 出力される。delay_moovで全codecの実configurationを含むinitを確定してから書く。
            '-movflags', '+frag_keyframe+delay_moov+default_base_moof+negative_cts_offsets',
            '-f', 'mp4', 'pipe:1',
        ]
        semaphore_key = 'CPU' if backend == 'FFmpeg' else f'{backend}:{device or 0}'
        semaphore = self._cpu_semaphore if backend == 'FFmpeg' else self._gpu_semaphores.setdefault(
            semaphore_key,
            asyncio.Semaphore(1),
        )
        async with semaphore:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout = asyncio.subprocess.PIPE,
                stderr = asyncio.subprocess.PIPE,
                env = RecordedPlaybackBackend.getEnvironment(backend),
            )
            stdout, stderr = await process.communicate()
            if (
                process.returncode != 0 and backend != 'FFmpeg' and
                self.recorded_program.recorded_video.container_format != 'MPEG-TS' and
                self.shouldRetryWithSoftwareDecode(stderr)
            ):
                # MP4/MKV/WebM でHW decodeだけが失敗した場合は、同じGPU encoderへ
                # system-memoryフレームをuploadし直す。エンコーダーのCPU降格は行わない。
                fallback_command = self.buildSoftwareDecodeFallback(command, backend, spec.encoder_pixel_format)
                process = await asyncio.create_subprocess_exec(
                    *fallback_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=RecordedPlaybackBackend.getEnvironment(backend),
                )
                stdout, stderr = await process.communicate()
        if process.returncode != 0:
            logging.error(
                '[RecordedFMP4Stream] FFmpeg 8 video segment failed. '
                f'[recorded_video_id: {self.recorded_program.recorded_video.id}, sequence: {segment.sequence}, '
                f'stderr: {stderr.decode(errors="ignore").strip()}]'
            )
            return
        init_data, media_data = self.splitFragmentedMP4(stdout)
        if len(init_data) == 0 or len(media_data) == 0:
            logging.error('[RecordedFMP4Stream] FFmpeg 8 output did not contain init and media fragments.')
            return
        media_data = self.normalizeFragmentTimeline(
            init_data,
            media_data,
            segment.start_time,
            segment.sequence,
        )
        if init_path.is_file() is False:
            await RecordedFMP4CacheManager.writeAtomic(init_path, init_data)
        await RecordedFMP4CacheManager.writeAtomic(segment_path, media_data)

    @staticmethod
    def computeInputSeekWindow(start_time: float, duration: float) -> tuple[float, float, float]:
        """映像を手前から復号して要求位置で切り出すseek範囲を返す。

        Args:
            start_time: 録画先頭基準の要求開始時刻。
            duration: 要求する映像セグメント長。

        Returns:
            FFmpeg入力seek時刻、filter trim開始時刻、入力処理時間。
        """

        input_seek = max(0.0, start_time - RecordedFMP4Stream.SEEK_PREROLL_SECONDS)
        trim_start = start_time - input_seek
        return input_seek, trim_start, trim_start + duration

    @staticmethod
    def shouldRetryWithSoftwareDecode(stderr: bytes) -> bool:
        """HW decoderの初期化失敗時だけsoftware decode再試行を許可する。"""

        # encode・hardware filterの失敗までsoftware decodeで再試行すると、GPU encode失敗を
        # decode失敗として誤認してしまう。FFmpegがHW decoderの初期化に失敗したと明示した
        # 場合だけ再試行し、曖昧なエラーは元の失敗として扱う。
        normalized_stderr = stderr.decode(errors='ignore').lower()
        hardware_decode_failure_markers = (
            'device setup failed for decoder',
            'no device available for decoder',
            'failed setup for format cuda',
            'failed setup for format qsv',
            'failed setup for format vaapi',
            'hwaccel initialisation returned error',
            'failed to create decoder device',
            'failed to initialise decoder device',
            'failed to initialize decoder device',
        )
        return any(marker in normalized_stderr for marker in hardware_decode_failure_markers)

    @staticmethod
    def buildSoftwareDecodeFallback(
        command: list[str],
        backend: RecordedPlaybackEncoder,
        pixel_format: str,
    ) -> list[str]:
        """HW decode引数だけを除き、同じGPU filter/encoderへuploadする。"""

        decode_options = {'-hwaccel', '-hwaccel_output_format', '-hwaccel_device'}
        fallback_command: list[str] = []
        index = 0
        while index < len(command):
            if command[index] in decode_options:
                index += 2
                continue
            fallback_command.append(command[index])
            index += 1
        filter_index = fallback_command.index('-vf') + 1
        upload_filter = {
            'QSVEncC': f'format={pixel_format},hwupload=extra_hw_frames=32',
            'NVEncC': f'format={pixel_format},hwupload_cuda',
            'VCEEncC': f'format={pixel_format},hwupload',
        }[backend]
        fallback_command[filter_index] = f'{upload_filter},{fallback_command[filter_index]}'
        return fallback_command

    def __getBackend(self) -> RecordedPlaybackEncoder:
        """録画のネットワーク種別に応じた公開エンコーダー設定を返す。"""

        encoder = Config().general.encoder_bs4k \
            if self.recorded_program.network_id == 0x000B else Config().general.encoder
        return encoder

    @staticmethod
    def __getProfileArguments(
        backend: RecordedPlaybackEncoder,
        codec: str,
        bit_depth: int,
    ) -> list[str]:
        """コーデックとバックエンドに合わせた固定profile引数を返す。"""

        if codec == 'avc':
            return ['-profile:v', 'high']
        if codec == 'hevc':
            return ['-profile:v', 'main10' if bit_depth == 10 else 'main']
        if codec == 'vp9':
            if backend == 'QSVEncC':
                return ['-profile:v', 'profile2' if bit_depth == 10 else 'profile0']
            return ['-profile:v', '2' if bit_depth == 10 else '0']
        if codec == 'av1':
            if backend == 'NVEncC':
                return []
            return ['-profile:v', 'main' if backend != 'FFmpeg' else '0']
        return []

    async def __encodeAudioSegment(
        self,
        segment: RecordedFMP4Segment,
        rendition: RecordedAudioRendition,
        init_path: Path,
        segment_path: Path,
    ) -> None:
        """指定Trackを48kHz AACへ変換し、消失区間は同じ長さの無音で補う。"""

        availability = self.__getAudioRenditionAvailability(segment.start_time, rendition)
        # タイムライン不明時は実入力を優先するが、TrackやDual Monoの副channelが
        # 実際には存在しない場合も映像再生を止めないよう、無音AACへ再試行する。
        source_attempts = [False] if availability is False else [True, False]
        stdout = b''
        stderr = b''
        is_succeeded = False
        for use_recorded_audio in source_attempts:
            command = [LIBRARY_PATH['FFmpeg8'], '-hide_banner', '-loglevel', 'error']
            if use_recorded_audio:
                # TS の stream index は入力開始位置の PMT により変わるため、PIDが保存されていれば
                # FFmpegのstream ID指定を使い、途中追加された音声を確実に選択する。
                stream_specifier = f'0:i:0x{rendition.pid:x}' \
                    if (self.recorded_program.recorded_video.container_format == 'MPEG-TS' and
                        rendition.pid is not None) else f'0:{rendition.stream_index}'
                command += [
                    '-ss', f'{segment.start_time:.6f}',
                    '-i', self.recorded_program.recorded_video.file_path,
                    '-t', f'{segment.duration + self.SEEK_PREROLL_SECONDS:.6f}',
                    '-map', stream_specifier,
                ]
                filters: list[str] = []
                if rendition.channel == 'main':
                    filters.append('pan=mono|c0=c0')
                elif rendition.channel == 'sub':
                    filters.append('pan=mono|c0=c1')
                filters.append('asetpts=PTS-STARTPTS')
                command += ['-af', ','.join(filters)]
            else:
                # Track消失区間も同じAAC構成を維持する。Dual Monoから展開した
                # 主音声・副音声レンディションは常にmono、それ以外は元Trackの構成を使う。
                channel_layout = self.__getSilentAudioChannelLayout(rendition)
                command += [
                    '-f', 'lavfi', '-i', f'anullsrc=r=48000:cl={channel_layout}',
                    '-t', f'{segment.duration:.6f}',
                    '-af', 'asetpts=PTS-STARTPTS',
                ]
            command += [
                '-vn', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000',
                # 入力の音声構成変更でfilter graphが再初期化されても、AAC packet数を
                # 48kHz / 1024 samples基準で制限し、fragmentが要求境界を越えないようにする。
                # AAC encoderがflush時にpriming packetを1つ追加するため、その分を差し引く。
                '-frames:a', str(self.computeAACFrameLimit(segment.duration)),
                '-movflags', '+frag_keyframe+empty_moov+default_base_moof+negative_cts_offsets',
                '-f', 'mp4', 'pipe:1',
            ]
            async with self._cpu_semaphore:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout = asyncio.subprocess.PIPE,
                    stderr = asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()
            if process.returncode == 0:
                is_succeeded = True
                break
            if use_recorded_audio:
                logging.warning(
                    '[RecordedFMP4Stream] FFmpeg 8 audio source failed; retrying with silence. '
                    f'[recorded_video_id: {self.recorded_program.recorded_video.id}, rendition: {rendition.id}, '
                    f'sequence: {segment.sequence}, stderr: {stderr.decode(errors="ignore").strip()}]'
                )
        if is_succeeded is False:
            logging.error(
                '[RecordedFMP4Stream] FFmpeg 8 audio segment failed. '
                f'[recorded_video_id: {self.recorded_program.recorded_video.id}, rendition: {rendition.id}, '
                f'sequence: {segment.sequence}, stderr: {stderr.decode(errors="ignore").strip()}]'
            )
            return
        init_data, media_data = self.splitFragmentedMP4(stdout)
        if len(init_data) == 0 or len(media_data) == 0:
            logging.error(
                '[RecordedFMP4Stream] FFmpeg 8 audio output did not contain init and media fragments. '
                f'[recorded_video_id: {self.recorded_program.recorded_video.id}, rendition: {rendition.id}, '
                f'sequence: {segment.sequence}]'
            )
            return
        media_data = self.normalizeFragmentTimeline(
            init_data,
            media_data,
            segment.start_time,
            segment.sequence,
        )
        if init_path.is_file() is False:
            await RecordedFMP4CacheManager.writeAtomic(init_path, init_data)
        await RecordedFMP4CacheManager.writeAtomic(segment_path, media_data)

    def __buildCachePath(self, segment: RecordedFMP4Segment, is_init: bool) -> Path:
        """現在の映像条件に対応するinitまたはfragmentのキャッシュパスを返す。"""

        variant = RecordedFMP4Variant(
            quality = self.quality,
            codec = self.encoding_options.video_codec,
            bit_depth = self.encoding_options.video_bit_depth,
            is_24fps = self.encoding_options.is_24fps_mode_enabled,
            backend = self.__getBackend(),
            configuration_generation = segment.generation,
            seek_generation = 0,
        )
        return RecordedFMP4CacheManager.buildPath(
            self.recorded_program.recorded_video,
            variant,
            'video-init' if is_init else 'video',
            segment.generation if is_init else segment.sequence,
        )

    def __buildAudioCachePath(
        self,
        segment: RecordedFMP4Segment,
        rendition: RecordedAudioRendition,
        is_init: bool,
    ) -> Path:
        """音声条件に対応するinitまたはfragmentのキャッシュパスを返す。"""

        variant = RecordedFMP4Variant(
            quality = self.quality,
            codec = 'aac',
            bit_depth = 0,
            is_24fps = False,
            backend = 'FFmpeg',
            configuration_generation = segment.audio_generation,
            seek_generation = 0,
            rendition = rendition.id,
        )
        return RecordedFMP4CacheManager.buildPath(
            self.recorded_program.recorded_video,
            variant,
            'audio-init' if is_init else 'audio',
            segment.audio_generation if is_init else segment.sequence,
        )

    def __getAudioRendition(self, rendition_id: str) -> RecordedAudioRendition | None:
        """公開済みIDに一致する音声レンディションを返す。"""

        return next((rendition for rendition in self.getAudioRenditions() if rendition.id == rendition_id), None)

    def __getAudioRenditionAvailability(
        self,
        start_time: float,
        rendition: RecordedAudioRendition,
    ) -> bool | None:
        """指定時刻に音声レンディションが存在するかを返す。

        Args:
            start_time: 録画先頭0秒基準の確認時刻。
            rendition: 確認対象のHLS音声レンディション。

        Returns:
            Trueは存在、Falseは不在、Noneはタイムライン不明。
        """

        interval = next(
            (
                item for item in self.recorded_program.recorded_video.audio_track_timeline
                if float(item['start_time']) <= start_time < float(item['end_time'])
            ),
            None,
        )
        if interval is None:
            return None
        timeline_track = next(
            (track for track in interval['tracks'] if int(track.get('index', 0)) == rendition.track_index),
            None,
        )
        if timeline_track is None:
            return False
        # Dual Monoから展開した副音声は、同じ論理Trackがモノラルに変化した区間には存在しない。
        if rendition.channel == 'sub' and timeline_track.get('is_dual_mono') is not True:
            return False
        return True

    def __getSilentAudioChannelLayout(self, rendition: RecordedAudioRendition) -> str:
        """Track消失区間へ挿入する無音AACのchannel layoutを返す。"""

        if rendition.channel in ('main', 'sub'):
            return 'mono'
        source_track = next(
            (
                track
                for interval in self.recorded_program.recorded_video.audio_track_timeline
                for track in interval['tracks']
                if int(track.get('index', 0)) == rendition.track_index
            ),
            next(
                (
                    track for track in self.recorded_program.recorded_video.audio_tracks
                    if int(track.get('index', 0)) == rendition.track_index
                ),
                None,
            ),
        )
        if source_track is None:
            return 'stereo'
        channel_layout = str(source_track.get('channel_layout') or '').lower()
        if channel_layout in ('mono', 'stereo', '2.1', '3.0', '4.0', '5.1', '7.1'):
            return channel_layout
        channel_label = str(source_track.get('channel') or '').lower()
        if 'monaural' in channel_label or 'mono' in channel_label:
            return 'mono'
        if '5.1' in channel_label:
            return '5.1'
        if '7.1' in channel_label:
            return '7.1'
        return 'stereo'

    async def __acquire(self, cache_path: Path) -> asyncio.Lock:
        """初回参照時だけキャッシュへのセッション参照を登録する。"""

        if cache_path not in self._referenced_paths:
            self._referenced_paths.add(cache_path)
        return await RecordedFMP4CacheManager.acquire(cache_path, self.session_id)

    def __getSegment(self, sequence: int) -> RecordedFMP4Segment | None:
        """範囲内のシーケンスだけを返す。"""

        if sequence < 0 or sequence >= len(self._segments):
            return None
        return self._segments[sequence]

    def getCodecQuery(self) -> str:
        """子APIへ引き継ぐ録画エンコード条件を返す。"""

        return (
            f'video_codec={self.encoding_options.video_codec}'
            f'&video_bit_depth={self.encoding_options.video_bit_depth}'
            f'&audio_codec={self.encoding_options.audio_codec}'
        )

    @staticmethod
    def splitFragmentedMP4(data: bytes) -> tuple[bytes, bytes]:
        """自己完結fMP4からftyp/moovとfragment boxを分離する。"""

        init = bytearray()
        media = bytearray()
        offset = 0
        while offset + 8 <= len(data):
            size = int.from_bytes(data[offset:offset + 4], 'big')
            box_type = data[offset + 4:offset + 8]
            header_size = 8
            if size == 1 and offset + 16 <= len(data):
                size = int.from_bytes(data[offset + 8:offset + 16], 'big')
                header_size = 16
            elif size == 0:
                size = len(data) - offset
            if size < header_size or offset + size > len(data):
                break
            box = data[offset:offset + size]
            if box_type in (b'ftyp', b'moov'):
                init.extend(box)
            elif box_type in (b'styp', b'sidx', b'moof', b'mdat'):
                media.extend(box)
            offset += size
        return bytes(init), bytes(media)

    @staticmethod
    def normalizeFragmentTimeline(
        init_data: bytes,
        media_data: bytes,
        start_time: float,
        sequence: int,
    ) -> bytes:
        """独立FFmpegプロセスのfragment時刻と連番をHLSタイムラインへ合わせる。

        Args:
            init_data: 同じFFmpeg出力から分離した初期化セグメント。
            media_data: moofから始まるメディアfragment。
            start_time: 録画先頭0秒基準のfragment開始時刻。
            sequence: HLSメディアシーケンス番号。

        Returns:
            tfdtとmfhdを補正したメディアfragment。
        """

        # init内のmdhdから、このTrackが使うtimescaleを取得する。
        mdhd_type_offset = init_data.find(b'mdhd')
        if mdhd_type_offset < 0 or mdhd_type_offset + 20 > len(init_data):
            raise ValueError('The fMP4 init segment does not contain a valid mdhd box.')
        mdhd_version = init_data[mdhd_type_offset + 4]
        if mdhd_version == 1 and mdhd_type_offset + 28 > len(init_data):
            raise ValueError('The fMP4 init segment contains a truncated mdhd box.')
        timescale_offset = mdhd_type_offset + (24 if mdhd_version == 1 else 16)
        timescale = int.from_bytes(init_data[timescale_offset:timescale_offset + 4], 'big')
        if timescale <= 0:
            raise ValueError('The fMP4 media timescale is invalid.')

        normalized_media = bytearray(media_data)

        def IterateBoxes(start: int, end: int) -> Iterator[tuple[int, int, int, bytes]]:
            """指定範囲の直下にあるISO BMFF boxを順に返す。"""

            offset = start
            while offset + 8 <= end:
                box_size = int.from_bytes(normalized_media[offset:offset + 4], 'big')
                box_type = bytes(normalized_media[offset + 4:offset + 8])
                header_size = 8
                if box_size == 1:
                    if offset + 16 > end:
                        raise ValueError('The fMP4 fragment contains a truncated extended box header.')
                    box_size = int.from_bytes(normalized_media[offset + 8:offset + 16], 'big')
                    header_size = 16
                elif box_size == 0:
                    box_size = end - offset
                if box_size < header_size or offset + box_size > end:
                    raise ValueError('The fMP4 fragment contains an invalid box size.')
                yield offset, box_size, header_size, box_type
                offset += box_size

        # frag_keyframeにより1つのHLS segment内へ複数moofが生成される場合がある。
        # 全moofの相対decode timeを維持したまま、録画先頭基準へ平行移動する。
        base_decode_time = round(start_time * timescale)
        fragment_count = 0
        for moof_offset, moof_size, moof_header_size, moof_type in IterateBoxes(0, len(normalized_media)):
            if moof_type != b'moof':
                continue
            mfhd_found = False
            tfdt_found = False
            for child_offset, child_size, child_header_size, child_type in IterateBoxes(
                moof_offset + moof_header_size,
                moof_offset + moof_size,
            ):
                if child_type == b'mfhd':
                    if child_size < child_header_size + 8:
                        raise ValueError('The fMP4 fragment contains a truncated mfhd box.')
                    sequence_number = (sequence << 16) + fragment_count + 1
                    if sequence_number >= 1 << 32:
                        raise ValueError('The fMP4 fragment sequence exceeds the mfhd field size.')
                    sequence_offset = child_offset + child_header_size + 4
                    normalized_media[sequence_offset:sequence_offset + 4] = sequence_number.to_bytes(4, 'big')
                    mfhd_found = True
                elif child_type == b'traf':
                    for traf_offset, traf_size, traf_header_size, traf_type in IterateBoxes(
                        child_offset + child_header_size,
                        child_offset + child_size,
                    ):
                        if traf_type != b'tfdt':
                            continue
                        if traf_size < traf_header_size + 8:
                            raise ValueError('The fMP4 fragment contains a truncated tfdt box.')
                        tfdt_version = normalized_media[traf_offset + traf_header_size]
                        decode_time_size = 8 if tfdt_version == 1 else 4
                        decode_time_offset = traf_offset + traf_header_size + 4
                        if decode_time_offset + decode_time_size > traf_offset + traf_size:
                            raise ValueError('The fMP4 fragment contains a truncated tfdt decode time.')
                        relative_decode_time = int.from_bytes(
                            normalized_media[decode_time_offset:decode_time_offset + decode_time_size],
                            'big',
                        )
                        decode_time = base_decode_time + relative_decode_time
                        if decode_time >= 1 << (decode_time_size * 8):
                            raise ValueError('The fMP4 decode time exceeds the tfdt field size.')
                        normalized_media[decode_time_offset:decode_time_offset + decode_time_size] = \
                            decode_time.to_bytes(decode_time_size, 'big')
                        tfdt_found = True
            if mfhd_found is False or tfdt_found is False:
                raise ValueError('The fMP4 moof does not contain valid mfhd and tfdt boxes.')
            fragment_count += 1
        if fragment_count == 0:
            raise ValueError('The fMP4 fragment does not contain a moof box.')
        return bytes(normalized_media)

    @staticmethod
    def computeAACFrameLimit(duration: float) -> int:
        """要求時間を越えないAAC encoder入力フレーム上限を返す。

        Args:
            duration: HLSセグメントの要求時間。

        Returns:
            FFmpegの-frames:aへ渡すフレーム数。
        """

        # AAC-LCは48kHzで1packetあたり1024 samples。encoderのflush時にpriming packetが
        # 1つ加わるため、切り上げたpacket数から入力フレームを1つ差し引く。
        return max(1, math.ceil(duration * 48_000 / 1024) - 1)
