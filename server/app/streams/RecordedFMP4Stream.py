from __future__ import annotations

import asyncio
import json
import math
import tempfile
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, Literal, cast

from fastapi import HTTPException, status

from app import logging
from app.config import Config
from app.constants import LIBRARY_PATH, QUALITY, QUALITY_TYPES
from app.models.RecordedProgram import RecordedProgram
from app.schemas import AudioTrack
from app.streams.RecordedEncodingCodecs import AudioCodec, VideoCodec
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
    # 元音声構成と映像DISCONTINUITYに対応する世代。
    audio_generation: int
    # AAC/Opusの待ち時間を制限する最大6segmentのdelivery世代。
    transcoded_audio_generation: int | None = None
    transcoded_audio_start_sample: int | None = None
    transcoded_audio_sample_count: int | None = None


@dataclass(frozen=True, slots=True)
class RecordedVideoBitrate:
    """録画映像エンコードに適用する指定ビットレートと最大ビットレートを表す。"""

    video_bitrate: str
    video_bitrate_max: str


@dataclass(frozen=True, slots=True)
class RecordedAudioSegmentTiming:
    """音声fragmentの48kHz sample基準の生成範囲を表す。"""

    start_sample: int
    sample_count: int

    @property
    def start_time(self) -> float:
        return self.start_sample / 48_000

    @property
    def duration(self) -> float:
        return self.sample_count / 48_000


@dataclass(frozen=True, slots=True)
class RecordedAudioFragmentInfo:
    """fMP4音声fragmentから検証したdecode timeline情報。"""

    timescale: int
    first_decode_time: int
    total_duration: int
    sample_count: int
    sample_durations: tuple[int, ...]


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
    AAC_ENCODER_DELAY_SAMPLES: ClassVar[int] = 1024
    AAC_PACKET_SAMPLES: ClassVar[int] = 1024
    AUDIO_SAMPLE_RATE: ClassVar[int] = 48_000
    OPUS_ENCODER_DELAY_SAMPLES: ClassVar[int] = 312
    AUDIO_BOUNDARY_COALESCE_SECONDS: ClassVar[float] = 1024 / 48_000
    AUDIO_DELIVERY_GENERATION_SEGMENTS: ClassVar[int] = 6
    AUDIO_INPUT_TAIL_MARGIN_SECONDS: ClassVar[float] = 0.1
    OPUS_BITRATES: ClassVar[tuple[tuple[range, int], ...]] = (
        (range(1, 2), 64_000),
        (range(2, 3), 128_000),
        (range(3, 5), 192_000),
        (range(5, 7), 256_000),
        (range(7, 9), 320_000),
    )
    # AVC / HEVC の既存調整値を保ちながら、VP9 と AV1 は HEVC より段階的に帯域を抑える。
    # ユーザーが選んだコーデックごとの通信量の差を明確にしつつ、全画質で同じ比率を適用する。
    VIDEO_BITRATE_RATIOS_FROM_HEVC: ClassVar[dict[VideoCodec, tuple[int, int]]] = {
        'hevc': (100, 100),
        'vp9': (90, 100),
        'av1': (70, 100),
    }
    # 240p の既存最大値は AVC / HEVC とも 650K のため、録画再生だけ最低 50K の差を確保する。
    VIDEO_BITRATE_MINIMUM_GAP_KBPS: ClassVar[int] = 50

    # セッションを識別し、ルーターの後続API要求とキャッシュ参照に利用する。
    session_id: str
    # 生成元ファイルとDB上の再生インデックスを参照する録画番組。
    recorded_program: RecordedProgram
    # 解像度・ビットレートをQUALITYから引くための画質キー。
    quality: QUALITY_TYPES
    # codec・24fpsなど、初回プレイリスト要求で固定される生成条件。
    encoding_options: StreamEncodingOptions
    # 録画メタデータと実生成結果を踏まえ、このセッションで実際に配信する音声方式。
    _effective_audio_codec: AudioCodec
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
            instance._effective_audio_codec = instance.__resolveEffectiveAudioCodec(encoding_options.audio_codec)
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
        # HLS子APIにも初回と同じqueryを必須とし、同一session IDを異なる生成条件で再利用させない。
        # audio_rendition_idは旧MPEG-TS経路向けで現在の代替音声HLSでは未使用のため照合対象外とする。
        if encoding_options is not None and (
            instance.encoding_options.is_hevc_10bit_enabled != encoding_options.is_hevc_10bit_enabled or
            instance.encoding_options.is_24fps_mode_enabled != encoding_options.is_24fps_mode_enabled or
            instance.encoding_options.video_codec != encoding_options.video_codec or
            instance.encoding_options.video_bit_depth != encoding_options.video_bit_depth or
            instance.encoding_options.audio_codec != encoding_options.audio_codec
        ):
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
    async def __activeOperation(self) -> AsyncGenerator[None]:
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

    @classmethod
    def getVideoBitrate(cls, quality: QUALITY_TYPES, codec: VideoCodec) -> RecordedVideoBitrate:
        """録画画質と映像コーデックから指定値・最大値を解決する。

        Args:
            quality: 解像度・フレームレートを表す既存画質キー。
            codec: 録画再生で出力する映像コーデック。

        Returns:
            FFmpeg と HLS マスターで共有するコーデック別ビットレート。
        """

        # QUALITY はライブと録画で共用されているため定義自体は変更せず、同じ解像度の
        # AVC / HEVC ペアを録画専用ポリシーの基準値として利用する。
        quality_without_codec = quality.removesuffix('-hevc')
        avc_quality_key = cast(QUALITY_TYPES, quality_without_codec)
        hevc_quality_key = cast(QUALITY_TYPES, f'{quality_without_codec}-hevc')
        if avc_quality_key not in QUALITY or hevc_quality_key not in QUALITY:
            raise ValueError(f'Video bitrate is not defined for quality: {quality}')
        avc_quality = QUALITY[avc_quality_key]
        hevc_quality = QUALITY[hevc_quality_key]

        def ParseKbps(value: str) -> int:
            """QUALITY の Kbps 文字列を整数へ変換する。"""

            if value.endswith('K') is False:
                raise ValueError(f'Invalid video bitrate: {value}')
            return int(value[:-1])

        avc_bitrate_kbps = ParseKbps(avc_quality.video_bitrate)
        avc_bitrate_max_kbps = ParseKbps(avc_quality.video_bitrate_max)
        if codec == 'avc':
            return RecordedVideoBitrate(
                video_bitrate = f'{avc_bitrate_kbps}K',
                video_bitrate_max = f'{avc_bitrate_max_kbps}K',
            )

        # HEVC は既存値を維持する。ただし AVC と同値になる端点だけは 50K 下へ制限し、
        # 指定値・最大値の双方で AV1 < VP9 < HEVC < AVC を厳密に成立させる。
        hevc_bitrate_kbps = min(
            ParseKbps(hevc_quality.video_bitrate),
            avc_bitrate_kbps - cls.VIDEO_BITRATE_MINIMUM_GAP_KBPS,
        )
        hevc_bitrate_max_kbps = min(
            ParseKbps(hevc_quality.video_bitrate_max),
            avc_bitrate_max_kbps - cls.VIDEO_BITRATE_MINIMUM_GAP_KBPS,
        )
        ratio_numerator, ratio_denominator = cls.VIDEO_BITRATE_RATIOS_FROM_HEVC[codec]
        return RecordedVideoBitrate(
            video_bitrate = f'{hevc_bitrate_kbps * ratio_numerator // ratio_denominator}K',
            video_bitrate_max = f'{hevc_bitrate_max_kbps * ratio_numerator // ratio_denominator}K',
        )

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

    def __getAudioBandwidth(self, renditions: list[RecordedAudioRendition]) -> int:
        """実効音声方式を含むmasterの音声帯域加算値を返す。"""

        if len(renditions) == 0:
            return 0
        if self._effective_audio_codec == 'opus':
            bitrates = [
                self.getOpusBitrate(channels)
                for rendition in renditions
                if (channels := self.__getMaximumAudioRenditionChannelCount(rendition)) is not None
            ]
            valid_bitrates = [bitrate for bitrate in bitrates if bitrate is not None]
            if len(valid_bitrates) > 0:
                return math.ceil(max(valid_bitrates) * 1.25)
        return 256_000

    async def __getMasterPlaylist(self, cache_key: str | None = None) -> str:
        """実initを生成してHLSマスターを組み立てる。"""

        self.keepAlive()
        cache_key = cache_key or uuid.uuid4().hex[:8]
        video_bitrate = self.getVideoBitrate(self.quality, self.encoding_options.video_codec)
        bandwidth = int(video_bitrate.video_bitrate_max.removesuffix('K')) * 1000
        lines = ['#EXTM3U', '#EXT-X-VERSION:7']
        renditions = self.getAudioRenditions()
        for index, rendition in enumerate(renditions):
            default = 'YES' if index == 0 else 'NO'
            language = rendition.language.replace('"', '')
            name = rendition.name.replace('"', '')
            channels = self.__getMaximumDeclaredAudioRenditionChannelCount(rendition)
            channels_attribute = f',CHANNELS="{channels}"' if channels is not None else ''
            lines.append(
                '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",'
                f'NAME="{name}",DEFAULT={default},AUTOSELECT=YES,LANGUAGE="{language}"{channels_attribute},'
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
        audio_codec_string = 'opus' if self._effective_audio_codec == 'opus' else 'mp4a.40.2'
        codec_attribute = f'{codec_string},{audio_codec_string}' if len(renditions) > 0 else codec_string
        stream_attributes = [
            f'BANDWIDTH={bandwidth + self.__getAudioBandwidth(renditions)}',
            f'CODECS="{codec_attribute}"',
        ]
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

    @staticmethod
    def extractAACInitializationConfiguration(init_segment: bytes) -> tuple[int, int, int] | None:
        """AAC initのAudioSpecificConfigからobject type/sample rate/channel構成を取得する。

        Args:
            init_segment: FFmpegが生成した音声fMP4初期化セグメント。

        Returns:
            Audio Object Type、sampling rate、channel configuration。判定不能時はNone。
        """

        # AACはmp4a sample entryだけではLC/HE-AACを区別できないため、esds内の
        # DecoderSpecificInfo(tag 0x05)からAudio Object Typeを読み取る。
        if b'mp4a' not in init_segment:
            return None
        esds_type_offset = init_segment.find(b'esds')
        if esds_type_offset < 4:
            return None
        esds_size = int.from_bytes(init_segment[esds_type_offset - 4:esds_type_offset], 'big')
        esds_start = esds_type_offset + 4
        esds_end = esds_type_offset - 4 + esds_size
        if esds_size < 12 or esds_end > len(init_segment):
            return None

        # FullBoxヘッダー4byteの後ろに可変長descriptor列がある。入れ子descriptorの
        # 境界を完全展開する必要はなく、長さがbox内に収まるtag 0x05だけを採用する。
        offset = esds_start + 4
        while offset < esds_end:
            if init_segment[offset] != 0x05:
                offset += 1
                continue
            length_offset = offset + 1
            descriptor_length = 0
            for _ in range(4):
                if length_offset >= esds_end:
                    return None
                value = init_segment[length_offset]
                length_offset += 1
                descriptor_length = (descriptor_length << 7) | (value & 0x7F)
                if value & 0x80 == 0:
                    break
            else:
                offset += 1
                continue
            if descriptor_length < 2 or length_offset + descriptor_length > esds_end:
                offset += 1
                continue
            audio_specific_config = init_segment[length_offset:length_offset + descriptor_length]
            bit_string = ''.join(f'{value:08b}' for value in audio_specific_config)
            bit_offset = 0

            def ReadBits(length: int) -> int | None:
                """AudioSpecificConfigから指定bit数を安全に読む。"""

                nonlocal bit_offset
                if bit_offset + length > len(bit_string):
                    return None
                value = int(bit_string[bit_offset:bit_offset + length], 2)
                bit_offset += length
                return value

            audio_object_type = ReadBits(5)
            if audio_object_type is None or audio_object_type == 0:
                return None
            if audio_object_type == 31:
                extended_type = ReadBits(6)
                if extended_type is None:
                    return None
                audio_object_type = 32 + extended_type
            sampling_frequency_index = ReadBits(4)
            if sampling_frequency_index is None:
                return None
            sampling_frequencies = (
                96_000, 88_200, 64_000, 48_000, 44_100, 32_000, 24_000,
                22_050, 16_000, 12_000, 11_025, 8_000, 7_350,
            )
            if sampling_frequency_index == 0x0F:
                sampling_rate = ReadBits(24)
            elif sampling_frequency_index < len(sampling_frequencies):
                sampling_rate = sampling_frequencies[sampling_frequency_index]
            else:
                sampling_rate = None
            channel_configuration = ReadBits(4)
            if sampling_rate is None or channel_configuration is None:
                return None
            return audio_object_type, sampling_rate, channel_configuration
        return None

    @staticmethod
    def getAACChannelCountFromConfiguration(channel_configuration: int) -> int | None:
        """AudioSpecificConfigのchannelConfigurationを実チャンネル数へ変換する。"""

        # ISO/IEC 14496-3の標準speaker mapping。0はPCE依存のため安全側で判定不能とする。
        return {
            1: 1,
            2: 2,
            3: 3,
            4: 4,
            5: 5,
            6: 6,
            7: 8,
        }.get(channel_configuration)

    @classmethod
    def extractAudioInitializationChannelCount(
        cls,
        init_segment: bytes,
        audio_codec: Literal['aac', 'opus'],
    ) -> int | None:
        """AAC/Opus initから実際に宣言された出力チャンネル数を取得する。"""

        if audio_codec == 'aac':
            configuration = cls.extractAACInitializationConfiguration(init_segment)
            if configuration is None:
                return None
            channel_count = cls.getAACChannelCountFromConfiguration(configuration[2])
            if channel_count is not None:
                return channel_count
            # channelConfiguration=0 の AAC は Program Config Element (PCE) で構成を表す。
            # PCE のビット列を独自解釈せず、同じ AudioSampleEntry に muxer が書いた
            # channelcount を size 検証した上で参照する。
            if configuration[2] == 0:
                mp4a_box = cls.__findMP4Box(init_segment, b'mp4a')
                if mp4a_box is None:
                    return None
                mp4a_offset, mp4a_size, mp4a_header_size = mp4a_box
                channel_count_offset = mp4a_offset + mp4a_header_size + 16
                if channel_count_offset + 2 > mp4a_offset + mp4a_size:
                    return None
                channel_count = int.from_bytes(
                    init_segment[channel_count_offset:channel_count_offset + 2],
                    'big',
                )
                return channel_count if 1 <= channel_count <= 8 else None
            return None
        dops_box = cls.__findMP4Box(init_segment, b'dOps')
        if dops_box is None:
            return None
        dops_offset, dops_size, dops_header_size = dops_box
        payload_offset = dops_offset + dops_header_size
        if payload_offset + 2 > dops_offset + dops_size:
            return None
        return init_segment[payload_offset + 1]

    @classmethod
    def extractAudioCodecString(cls, init_segment: bytes) -> str | None:
        """音声initからHLS CODECSへ記載するRFC 6381 codec stringを取得する。

        Args:
            init_segment: FFmpegが生成した音声fMP4初期化セグメント。

        Returns:
            AAC Audio Object TypeまたはOpusのcodec string。判定できない場合はNone。
        """

        # OpusSampleEntryは大文字のOpus、構成boxはdOpsで識別される。
        if b'Opus' in init_segment and b'dOps' in init_segment:
            return 'opus'
        configuration = cls.extractAACInitializationConfiguration(init_segment)
        return f'mp4a.40.{configuration[0]}' if configuration is not None else None

    @staticmethod
    def __getTranscodedAudioGeneration(segment: RecordedFMP4Segment) -> int:
        """AAC/Opus delivery generationを返す。"""

        return segment.transcoded_audio_generation \
            if segment.transcoded_audio_generation is not None else segment.audio_generation

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
        previous_video_generation: int | None = None
        for segment in self._segments:
            # 代替音声と映像でcontinuity counterがずれると、hls.jsのAudioStreamControllerが
            # 対応する映像init PTSを見つけられずWAITING_INIT_PTSのまま停止する。
            # どちらかの構成世代が変わる境界を両メディアプレイリストへ同じ順序で出す。
            generation_key = (segment.generation, self.__getTranscodedAudioGeneration(segment))
            if previous_generation_key != generation_key:
                if previous_generation_key is not None:
                    lines.append('#EXT-X-DISCONTINUITY')
                previous_generation_key = generation_key
            # 音声delivery generationだけが変わる境界では、映像の初期化情報は変わらない。
            # ここで同じ映像initを再指定すると、hls.jsはMAP取得中に先行した代替音声を
            # WAITING_INIT_PTSから再要求し続ける場合がある。MAPは実映像世代だけで更新し、
            # DISCONTINUITYは上で音声playlistと同じ位置に維持する。
            if previous_video_generation != segment.generation:
                lines.append(
                    f'#EXT-X-MAP:URI="init?session_id={self.session_id}&generation={segment.generation}'
                    f'&sequence={segment.sequence}&cache_key={cache_key}&{self.getCodecQuery()}"'
                )
                previous_video_generation = segment.generation
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

    @staticmethod
    def getAudioChannelCount(track: AudioTrack) -> int | None:
        """索引済み音声Trackのchannel layoutから1～8chのチャンネル数を返す。

        Args:
            track: 録画再生索引に保存された音声Track。

        Returns:
            対応するチャンネル数。layoutを安全に判定できない場合はNone。
        """

        normalized_layout = str(track.get('channel_layout') or '').strip().lower()
        layout_channels = {
            'mono': 1,
            'stereo': 2,
            'stereo downmix': 2,
            '2.1': 3,
            '3.0': 3,
            '3.0(back)': 3,
            '3.1': 4,
            '4.0': 4,
            'quad': 4,
            'quad(side)': 4,
            '4.1': 5,
            '5.0': 5,
            '5.0(side)': 5,
            '5.1': 6,
            '5.1(side)': 6,
            '5.1(back)': 6,
            '6.0': 6,
            '6.0(front)': 6,
            'hexagonal': 6,
            '6.1': 7,
            '6.1(back)': 7,
            '6.1(front)': 7,
            '7.0': 7,
            '7.0(front)': 7,
            '7.1': 8,
            '7.1(wide)': 8,
            '7.1(wide-side)': 8,
            'octagonal': 8,
        }
        if normalized_layout in layout_channels:
            return layout_channels[normalized_layout]

        # FFmpeg が明示した未知 layout は、表示ラベルが 5.1ch などでも推測しない。
        # 実 layout が欠けた古い索引だけ、下の表示ラベルから安全な構成を補完する。
        if normalized_layout != '':
            return None

        # 古い索引にはchannel_layoutがないため、既存の表示ラベルで確実に判定できる
        # 構成だけを補完する。曖昧なラベルをstereoと推測してOpus化しない。
        channel_label = str(track.get('channel') or '').strip().lower()
        if channel_label in ('monaural', 'mono'):
            return 1
        if channel_label == 'stereo':
            return 2
        if channel_label == 'dual mono':
            return 2
        if '5.1' in channel_label:
            return 6
        if '7.1' in channel_label:
            return 8
        return None

    @classmethod
    def getOpusBitrate(cls, channels: int) -> int | None:
        """Opusのチャンネル数別固定ビットレートを返す。

        Args:
            channels: 出力音声のチャンネル数。

        Returns:
            1～8chに対応するビットレート。範囲外はNone。
        """

        for channel_range, bitrate in cls.OPUS_BITRATES:
            if channels in channel_range:
                return bitrate
        return None

    def __getAudioSourceTrack(
        self,
        rendition: RecordedAudioRendition,
        start_time: float | None = None,
    ) -> AudioTrack | None:
        """レンディションの指定時刻または代表構成に対応する索引Trackを返す。"""

        timeline_track = next(
            (
                track
                for interval in self.recorded_program.recorded_video.audio_track_timeline
                if start_time is None or
                float(interval['start_time']) <= start_time < float(interval['end_time'])
                for track in interval['tracks']
                if int(track.get('index', 0)) == rendition.track_index
            ),
            None,
        )
        if timeline_track is not None:
            return timeline_track
        return next(
            (
                track for track in self.recorded_program.recorded_video.audio_tracks
                if int(track.get('index', 0)) == rendition.track_index
            ),
            None,
        )

    def __getAudioRenditionChannelCount(
        self,
        rendition: RecordedAudioRendition,
        start_time: float | None = None,
    ) -> int | None:
        """指定時刻にエンコードするレンディションのチャンネル数を返す。"""

        # Dual Monoを主/副へ展開したレンディションは、入力が2chでも出力は常にmono。
        if rendition.channel in ('main', 'sub'):
            return 1
        source_track = self.__getAudioSourceTrack(rendition, start_time)
        return self.getAudioChannelCount(source_track) if source_track is not None else None

    def __getAudioRenditionChannelCounts(self, rendition: RecordedAudioRendition) -> list[int] | None:
        """録画全区間で出現するレンディションのチャンネル数を返す。"""

        if rendition.channel in ('main', 'sub'):
            return [1]
        source_tracks = [
            track
            for interval in self.recorded_program.recorded_video.audio_track_timeline
            for track in interval['tracks']
            if int(track.get('index', 0)) == rendition.track_index
        ]
        if len(source_tracks) == 0:
            source_track = self.__getAudioSourceTrack(rendition)
            source_tracks = [source_track] if source_track is not None else []
        channel_counts = [self.getAudioChannelCount(track) for track in source_tracks]
        if len(channel_counts) == 0 or any(channels is None for channels in channel_counts):
            return None
        return sorted({channels for channels in channel_counts if channels is not None})

    def __getMaximumAudioRenditionChannelCount(self, rendition: RecordedAudioRendition) -> int | None:
        """masterのCHANNELS/BANDWIDTHへ使う録画全区間の最大チャンネル数を返す。"""

        channel_counts = self.__getAudioRenditionChannelCounts(rendition)
        return max(channel_counts) if channel_counts is not None else None

    @classmethod
    def getDeclaredAudioChannelCount(cls, track: AudioTrack) -> int | None:
        """AAC fallbackでもHLSへ宣言できる索引上の実チャンネル数を返す。"""

        # Opus可否は未知layoutを拒否する必要があるが、AACへfallbackした後のCHANNELSまで
        # stereoと推測してはならない。Indexerが実channelsから保存した表示ラベルを別途使う。
        if (channels := cls.getAudioChannelCount(track)) is not None:
            return channels
        channel_label = str(track.get('channel') or '').strip().lower()
        label_parts = channel_label.split()
        if (
            len(label_parts) == 2 and
            label_parts[0].isdigit() and
            label_parts[1] in ('channel', 'channels')
        ):
            declared_channels = int(label_parts[0])
            return declared_channels if 1 <= declared_channels <= 8 else None
        if '5.1' in channel_label:
            return 6
        if '7.1' in channel_label:
            return 8
        return None

    def __getMaximumDeclaredAudioRenditionChannelCount(
        self,
        rendition: RecordedAudioRendition,
    ) -> int | None:
        """masterのCHANNELSへ使う録画全区間の最大実チャンネル数を返す。"""

        if rendition.channel in ('main', 'sub'):
            return 1
        source_tracks = [
            track
            for interval in self.recorded_program.recorded_video.audio_track_timeline
            for track in interval['tracks']
            if int(track.get('index', 0)) == rendition.track_index
        ]
        if len(source_tracks) == 0:
            source_track = self.__getAudioSourceTrack(rendition)
            source_tracks = [source_track] if source_track is not None else []
        channel_counts = [self.getDeclaredAudioChannelCount(track) for track in source_tracks]
        if len(channel_counts) == 0 or any(channels is None for channels in channel_counts):
            return None
        return max(channels for channels in channel_counts if channels is not None)

    def __resolveEffectiveAudioCodec(self, requested_codec: AudioCodec) -> AudioCodec:
        """録画の実音声構成からセッション全体の実効音声方式を決定する。"""

        if requested_codec == 'opus':
            # Opusは既知の1～8ch構成だけを保持してエンコードする。未知layoutを暗黙に
            # stereoへdownmixせず、セッション全体を互換性の高いAACへ切り替える。
            renditions = self.getAudioRenditions()
            if len(renditions) == 0 or any(
                channel_counts is None or
                any(self.getOpusBitrate(channels) is None for channels in channel_counts)
                for rendition in renditions
                for channel_counts in [self.__getAudioRenditionChannelCounts(rendition)]
            ):
                logging.warning(
                    '[RecordedFMP4Stream] Opus channel layout is unsupported; falling back to AAC. '
                    f'[recorded_video_id: {self.recorded_program.recorded_video.id}]'
                )
                return 'aac'
        return requested_codec

    def __getAudioSegmentTiming(self, segment: RecordedFMP4Segment) -> RecordedAudioSegmentTiming:
        """AAC/Opus音声fragmentの整数presentation sample境界を返す。"""

        if (
            segment.transcoded_audio_start_sample is not None and
            segment.transcoded_audio_sample_count is not None
        ):
            start_sample = segment.transcoded_audio_start_sample
            sample_count = segment.transcoded_audio_sample_count
        else:
            start_sample = round(segment.start_time * self.AUDIO_SAMPLE_RATE)
            end_sample = round((segment.start_time + segment.duration) * self.AUDIO_SAMPLE_RATE)
            sample_count = max(1, end_sample - start_sample)
        return RecordedAudioSegmentTiming(start_sample, sample_count)

    def __getAudioPlaylistDuration(self, segment: RecordedFMP4Segment) -> float:
        """codec delayを反映した音声fragmentのpresentation durationを返す。"""

        # segment muxerへ負のinitial_offsetと(B + encoder delay)のsplit時刻を渡すため、
        # edit list適用後の各fragmentはここで計画したpresentation sample数と一致する。
        return self.__getAudioSegmentTiming(segment).duration

    def getAudioPlaylist(self, rendition_id: str, cache_key: str | None = None) -> str:
        """映像と同じsequenceで、方式に適した音声時間境界のプレイリストを返す。"""

        rendition = self.__getAudioRendition(rendition_id)
        if rendition is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Audio rendition was not found')
        self.keepAlive()
        cache_key = cache_key or uuid.uuid4().hex[:8]
        audio_durations = [self.__getAudioPlaylistDuration(segment) for segment in self._segments]
        lines = [
            '#EXTM3U', '#EXT-X-VERSION:7', '#EXT-X-PLAYLIST-TYPE:VOD',
            f'#EXT-X-TARGETDURATION:{math.ceil(max(audio_durations))}',
        ]
        previous_generation_key: tuple[int, int] | None = None
        for segment in self._segments:
            generation_key = (segment.generation, self.__getTranscodedAudioGeneration(segment))
            if generation_key != previous_generation_key:
                if previous_generation_key is not None:
                    lines.append('#EXT-X-DISCONTINUITY')
                lines.append(
                    f'#EXT-X-MAP:URI="init?session_id={self.session_id}&sequence={segment.sequence}'
                    f'&cache_key={cache_key}&{self.getCodecQuery()}"'
                )
                previous_generation_key = generation_key
            audio_duration = self.__getAudioPlaylistDuration(segment)
            lines.append(f'#EXTINF:{audio_duration:.6f},')
            lines.append(
                f'segment?session_id={self.session_id}&sequence={segment.sequence}&cache_key={cache_key}'
                f'&{self.getCodecQuery()}'
            )
        lines.append('#EXT-X-ENDLIST')
        return '\n'.join(lines) + '\n'

    async def getAudioInitSegment(self, rendition_id: str, sequence: int) -> bytes | None:
        """指定レンディションの音声初期化セグメントを返す。"""

        segment = self.__getSegment(sequence)
        rendition = self.__getAudioRendition(rendition_id)
        if segment is None or rendition is None:
            return None
        await self.getAudioSegment(rendition_id, sequence)
        init_path = self.__buildAudioCachePath(segment, rendition, is_init=True)
        return await asyncio.to_thread(init_path.read_bytes) if init_path.is_file() else None

    async def getAudioSegment(self, rendition_id: str, sequence: int) -> bytes | None:
        """映像とは独立して指定方式の音声fragmentを生成または再利用する。"""

        async with self.__activeOperation():
            return await self.__getAudioSegment(rendition_id, sequence)

    async def __getAudioSegment(self, rendition_id: str, sequence: int) -> bytes | None:
        """音声fragment生成の本体処理を行う。"""

        self.keepAlive()
        segment = self.__getSegment(sequence)
        rendition = self.__getAudioRendition(rendition_id)
        if segment is None or rendition is None:
            return None
        return await self.__getTranscodedAudioSegment(segment, rendition, self._effective_audio_codec)

    async def getVideoInitSegment(self, generation: int, sequence: int) -> bytes | None:
        """指定世代の実エンコード結果から得た初期化セグメントを返す。"""

        segment = self.__getSegment(sequence)
        if segment is None or segment.generation != generation:
            return None
        init_path = self.__buildCachePath(segment, is_init=True)
        # master生成や同じ映像世代の先行segmentですでにinitを確定済みなら、音声だけの
        # DISCONTINUITY境界に対応するsegment encodeを待たず即座に返す。
        if init_path.is_file():
            # initは数KB以下の小さな固定データなので、executorへ渡すより同期読込の方が
            # 境界MAPへの応答を確実に即時化できる。
            return init_path.read_bytes()
        await self.getVideoSegment(sequence)
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

    @staticmethod
    def __alignTranscodedAudioGenerationBoundaries(
        desired_boundaries: list[int],
        frame_samples: int,
        boundary_phase: int,
    ) -> list[int] | None:
        """generation内部境界をcodec packet位相を保ったまま近傍へ揃える。"""

        if len(desired_boundaries) <= 2:
            return desired_boundaries.copy()
        generation_start = desired_boundaries[0]
        generation_end = desired_boundaries[-1]
        internal_count = len(desired_boundaries) - 2
        minimum_k = 1 if boundary_phase == 0 else 0
        maximum_k = math.floor(
            (generation_end - generation_start - 1 - boundary_phase) / frame_samples
        )
        if maximum_k - minimum_k + 1 < internal_count:
            return None

        aligned_k: list[int] = []
        for index, boundary in enumerate(desired_boundaries[1:-1]):
            relative_sample = boundary - generation_start
            desired_k = math.floor((relative_sample - boundary_phase) / frame_samples + 0.5)
            minimum_for_index = minimum_k + index
            maximum_for_index = maximum_k - (internal_count - index - 1)
            aligned_value = max(minimum_for_index, min(maximum_for_index, desired_k))
            if len(aligned_k) > 0:
                aligned_value = max(aligned_value, aligned_k[-1] + 1)
            aligned_k.append(aligned_value)
        return [
            generation_start,
            *[
                generation_start + boundary_phase + k * frame_samples
                for k in aligned_k
            ],
            generation_end,
        ]

    @classmethod
    def __coalesceAudioVideoStructuralBoundaries(
        cls,
        video_boundaries: set[float],
        audio_boundaries: set[float],
        duration: float,
    ) -> set[float]:
        """近接する映像・音声構成境界だけを一対一で後側時刻へまとめる。

        Args:
            video_boundaries: 映像構成区間の開始・終了時刻。
            audio_boundaries: 音声構成区間の開始・終了時刻。
            duration: 録画全体の長さ。

        Returns:
            同一メディア内の短区間を維持したまま統合した構成境界。
        """

        # 映像と音声の解析では同じ放送上の切替でも数msずれる場合がある。全境界を単純に
        # cluster化すると同一メディア内の本物の短区間まで消えるため、cross-mediaの候補だけを
        # sample精度で列挙し、最も近い組から一対一で採用する。
        candidates: list[tuple[int, float, float, float, float]] = []
        for video_boundary in video_boundaries:
            if video_boundary <= 0.0 or video_boundary >= duration:
                continue
            video_sample = round(video_boundary * cls.AUDIO_SAMPLE_RATE)
            for audio_boundary in audio_boundaries:
                if audio_boundary <= 0.0 or audio_boundary >= duration:
                    continue
                audio_sample = round(audio_boundary * cls.AUDIO_SAMPLE_RATE)
                sample_difference = abs(video_sample - audio_sample)
                if sample_difference <= cls.AAC_PACKET_SAMPLES:
                    candidates.append((
                        sample_difference,
                        max(video_boundary, audio_boundary),
                        min(video_boundary, audio_boundary),
                        video_boundary,
                        audio_boundary,
                    ))

        consumed_video_boundaries: set[float] = set()
        consumed_audio_boundaries: set[float] = set()
        canonical_boundaries: set[float] = set()
        for _, later_boundary, _, video_boundary, audio_boundary in sorted(candidates):
            if (
                video_boundary in consumed_video_boundaries or
                audio_boundary in consumed_audio_boundaries
            ):
                continue
            consumed_video_boundaries.add(video_boundary)
            consumed_audio_boundaries.add(audio_boundary)
            # 半開区間の後側を採用すれば、その時刻で映像・音声とも切替後の構成が選ばれる。
            canonical_boundaries.add(later_boundary)

        return (
            (video_boundaries - consumed_video_boundaries) |
            (audio_boundaries - consumed_audio_boundaries) |
            canonical_boundaries |
            {0.0, duration}
        )

    def __buildSegments(self) -> list[RecordedFMP4Segment]:
        """通常約6秒境界と映像構成変化点の和集合からセグメント計画を作る。"""

        recorded_video = self.recorded_program.recorded_video
        duration = recorded_video.duration
        segment_duration = VideoSegmentPlanner.computeSegmentDurationSeconds(recorded_video.video_frame_rate or 0)
        regular_boundaries = {
            min(duration, index * segment_duration)
            for index in range(1, max(1, math.ceil(duration / segment_duration)))
        }
        # 構成変化点の直前直後へ通常境界が重なると、AAC encoder delayより短い
        # 数msのsegmentが生まれる。構成境界を優先し、その1 AAC frame以内の通常境界だけを除く。
        video_structural_boundaries = {0.0, duration}
        timeline = recorded_video.video_stream_timeline or []
        for entry in timeline:
            video_structural_boundaries.add(max(0.0, min(duration, float(entry['start_time']))))
            video_structural_boundaries.add(max(0.0, min(duration, float(entry['end_time']))))
        audio_structural_boundaries = {0.0, duration}
        audio_timeline = recorded_video.audio_track_timeline
        for entry in audio_timeline:
            audio_structural_boundaries.add(max(0.0, min(duration, float(entry['start_time']))))
            audio_structural_boundaries.add(max(0.0, min(duration, float(entry['end_time']))))
        structural_boundaries = self.__coalesceAudioVideoStructuralBoundaries(
            video_structural_boundaries,
            audio_structural_boundaries,
            duration,
        )
        boundaries = structural_boundaries | {
            boundary
            for boundary in regular_boundaries
            if all(
                abs(boundary - structural_boundary) > self.AUDIO_BOUNDARY_COALESCE_SECONDS
                for structural_boundary in structural_boundaries
            )
        }
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
            is_video_generation_changed = signature != previous_signature
            if is_video_generation_changed:
                generation += 1
                previous_signature = signature
            audio_entry = next(
                (
                    item for item in audio_timeline
                    if float(item['start_time']) <= start_time < float(item['end_time'])
                ),
                None,
            )
            # タイムライン自体が存在するのに該当区間がない場合は、索引が明示した音声欠落区間。
            # legacy録画のようにタイムラインが空の場合だけ、全体メタデータへfallbackする。
            audio_tracks = audio_entry['tracks'] if audio_entry is not None else (
                recorded_video.audio_tracks if len(audio_timeline) == 0 else []
            )
            audio_signature = tuple(
                (
                    track.get('index'), track.get('pid'), track.get('stream_index'), track.get('codec'),
                    track.get('channel'), track.get('sampling_rate'), track.get('channel_layout'),
                    track.get('is_dual_mono'),
                )
                for track in audio_tracks
            )
            # 映像playlistも(video generation, audio generation)変化でDISCONTINUITYを出すため、
            # 映像だけの構成変化でも音声encoder/initを新世代にする。連続encoder途中のmediaへ
            # 同じedit listを再適用してcodec delayを二重にskipする状態を防ぐ。
            if audio_signature != previous_audio_signature or is_video_generation_changed:
                audio_generation += 1
                previous_audio_signature = audio_signature
            segments.append(RecordedFMP4Segment(
                sequence = sequence,
                start_time = start_time,
                duration = max(0.001, ordered_boundaries[sequence + 1] - start_time),
                generation = generation,
                audio_generation = audio_generation,
            ))

        # AAC/Opusはaudio_generationごとに1つのencoderを連続稼働させる。各generationの
        # 正確な開始sampleを原点にし、内部境界だけをcodec frame gridへ丸めることで、
        # segmentごとのencoder primingと長時間の丸め誤差を同時に排除する。構成変化点である
        # generation終端だけは任意sampleを許し、muxerが最終packet durationでpaddingをclipする。
        frame_samples = 960 if self._effective_audio_codec == 'opus' else self.AAC_PACKET_SAMPLES
        # Opusは312-sample pre-skip後の最初のpacketが648 samplesだけpresentationへ現れる。
        # したがってgeneration内の安全なpacket境界は648 + 960*k、AACは1024*kになる。
        boundary_phase = frame_samples - self.OPUS_ENCODER_DELAY_SAMPLES \
            if self._effective_audio_codec == 'opus' else 0
        next_audio_generation = 0
        for audio_generation in sorted({segment.audio_generation for segment in segments}):
            configuration_segments = [
                segment for segment in segments
                if segment.audio_generation == audio_generation
            ]
            # 長時間番組の全編encode完了を先頭segment要求が待たないよう、同じ構成でも最大6
            # segmentのdelivery generationへ分割する。各境界は両playlistのDISCONTINUITYと
            # 専用initに反映され、3segment以上の連続decode検証と一定の初回待ちを両立する。
            for group_start in range(0, len(configuration_segments), self.AUDIO_DELIVERY_GENERATION_SEGMENTS):
                generation_segments = configuration_segments[
                    group_start:group_start + self.AUDIO_DELIVERY_GENERATION_SEGMENTS
                ]
                if len(generation_segments) == 0:
                    continue
                generation_start_time = generation_segments[0].start_time
                generation_end_time = generation_segments[-1].start_time + generation_segments[-1].duration
                generation_start_sample = round(generation_start_time * self.AUDIO_SAMPLE_RATE)
                generation_end_sample = round(generation_end_time * self.AUDIO_SAMPLE_RATE)
                desired_boundaries = [
                    generation_start_sample,
                    *[
                        round(generation_segment.start_time * self.AUDIO_SAMPLE_RATE)
                        for generation_segment in generation_segments[1:]
                    ],
                    generation_end_sample,
                ]
                local_boundaries = self.__alignTranscodedAudioGenerationBoundaries(
                    desired_boundaries,
                    frame_samples,
                    boundary_phase,
                )
                if local_boundaries is None:
                    # 同一delivery generation内にpacket grid点より多い映像境界がある場合は、
                    # 各segmentを独立generationへ分け、短いterminal packetを次へ接続しない。
                    for generation_segment in generation_segments:
                        segment_start_sample = round(generation_segment.start_time * self.AUDIO_SAMPLE_RATE)
                        segment_end_sample = round(
                            (generation_segment.start_time + generation_segment.duration) * self.AUDIO_SAMPLE_RATE
                        )
                        segments[generation_segment.sequence] = replace(
                            generation_segment,
                            transcoded_audio_generation=next_audio_generation,
                            transcoded_audio_start_sample=segment_start_sample,
                            transcoded_audio_sample_count=max(1, segment_end_sample - segment_start_sample),
                        )
                        next_audio_generation += 1
                    continue
                for index, generation_segment in enumerate(generation_segments):
                    segments[generation_segment.sequence] = replace(
                        generation_segment,
                        transcoded_audio_generation=next_audio_generation,
                        transcoded_audio_start_sample=local_boundaries[index],
                        transcoded_audio_sample_count=local_boundaries[index + 1] - local_boundaries[index],
                    )
                next_audio_generation += 1
        return segments

    async def __encodeSegment(
        self,
        segment: RecordedFMP4Segment,
        init_path: Path,
        segment_path: Path,
    ) -> None:
        """FFmpeg 8で自己完結fMP4を生成し、initとfragmentへ分離する。"""

        quality = QUALITY[self.quality]
        video_bitrate = self.getVideoBitrate(self.quality, self.encoding_options.video_codec)
        video_bitrate_max_kbps = int(video_bitrate.video_bitrate_max.removesuffix('K'))
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
            '-b:v', video_bitrate.video_bitrate, '-maxrate', video_bitrate.video_bitrate_max,
            '-bufsize', f'{video_bitrate_max_kbps * 2}K',
            # 1080p 品質は帯域削減のため 1440x1080 の anamorphic 映像として出力する。
            # 入力が square pixel の 1920x1080 / 3840x2160 でも 4:3 と解釈されないよう、
            # encoder / MP4 muxer へ表示アスペクト比を明示する。
            '-aspect', '16:9',
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

    def __getAudioGenerationSegments(self, audio_generation: int) -> list[RecordedFMP4Segment]:
        """同じ音声構成を共有する連続segment群を返す。"""

        return [
            segment for segment in self._segments
            if self.__getTranscodedAudioGeneration(segment) == audio_generation
        ]

    async def __getTranscodedAudioSegment(
        self,
        segment: RecordedFMP4Segment,
        rendition: RecordedAudioRendition,
        audio_codec: Literal['aac', 'opus'],
    ) -> bytes | None:
        """generation全体を単一encoderで生成し、要求fragmentを返す。"""

        generation_segments = self.__getAudioGenerationSegments(
            self.__getTranscodedAudioGeneration(segment)
        )
        if len(generation_segments) == 0:
            return None
        init_path = self.__buildAudioCachePath(segment, rendition, is_init=True)
        segment_paths = {
            generation_segment.sequence: self.__buildAudioCachePath(
                generation_segment,
                rendition,
                is_init=False,
            )
            for generation_segment in generation_segments
        }
        # init pathはaudio_generationとcodec/renditionで一意なのでgeneration lockとしても使う。
        # ランダムな後方segmentが先に要求されても、同じgenerationを重複encodeせず全cacheを確定する。
        generation_lock = await self.__acquire(init_path)
        for segment_path in segment_paths.values():
            await self.__acquire(segment_path)
        async with generation_lock:
            if init_path.is_file() and all(path.is_file() for path in segment_paths.values()):
                return await asyncio.to_thread(segment_paths[segment.sequence].read_bytes)
            is_succeeded = await self.__encodeTranscodedAudioGeneration(
                generation_segments,
                rendition,
                init_path,
                segment_paths,
                audio_codec,
            )
            if is_succeeded is False:
                return None
            requested_path = segment_paths[segment.sequence]
            return await asyncio.to_thread(requested_path.read_bytes) if requested_path.is_file() else None

    async def __encodeTranscodedAudioGeneration(
        self,
        generation_segments: list[RecordedFMP4Segment],
        rendition: RecordedAudioRendition,
        init_path: Path,
        segment_paths: dict[int, Path],
        audio_codec: Literal['aac', 'opus'],
    ) -> bool:
        """AAC/Opusを音声構成generation単位で連続encodeし、全fragmentをcacheする。"""

        first_segment = generation_segments[0]
        first_timing = self.__getAudioSegmentTiming(first_segment)
        configuration_time = first_segment.start_time
        expected_channel_count = self.__getAudioRenditionChannelCount(rendition, configuration_time)
        expected_channel_layout = self.__getAudioRenditionChannelLayout(rendition, configuration_time)
        availability = self.__getAudioRenditionAvailability(configuration_time, rendition)
        source_attempts = [False] if availability is False else [True, False]
        timings = [self.__getAudioSegmentTiming(item) for item in generation_segments]
        total_input_samples = sum(timing.sample_count for timing in timings)
        if total_input_samples <= 0:
            return False

        stderr = b''
        for use_recorded_audio in source_attempts:
            command = [LIBRARY_PATH['FFmpeg8'], '-hide_banner', '-loglevel', 'error']
            normalization_command: list[str] | None = None
            trim_start_samples = 0
            if use_recorded_audio:
                source_track = self.__getAudioSourceTrack(rendition, configuration_time)
                source_pid = rendition.pid
                source_stream_index = rendition.stream_index
                if source_track is not None:
                    timeline_pid = source_track.get('pid')
                    timeline_stream_index = source_track.get('stream_index')
                    if timeline_pid is not None:
                        source_pid = int(timeline_pid)
                    if timeline_stream_index is not None:
                        source_stream_index = int(timeline_stream_index)
                stream_specifier = f'0:i:0x{int(source_pid):x}' \
                    if (self.recorded_program.recorded_video.container_format == 'MPEG-TS' and
                        source_pid is not None) else f'0:{source_stream_index}'
                # 同じ音声構成のdelivery境界だけは手前からdecoderをwarm upする。構成世代の
                # 先頭でprerollすると、target時刻から出現する新PIDをinput probeできず、
                # generation全体を無音へ誤fallbackするため、そこでのseekはtargetへ直行する。
                use_decoder_preroll = (
                    first_segment.sequence > 0 and
                    self._segments[first_segment.sequence - 1].audio_generation == first_segment.audio_generation
                )
                input_seek = max(0.0, first_timing.start_time - self.SEEK_PREROLL_SECONDS) \
                    if use_decoder_preroll else first_timing.start_time
                trim_start_samples = round(
                    (first_timing.start_time - input_seek) * self.AUDIO_SAMPLE_RATE
                )
                input_duration = (
                    (trim_start_samples + total_input_samples) / self.AUDIO_SAMPLE_RATE +
                    self.AUDIO_INPUT_TAIL_MARGIN_SECONDS
                )
                source_input_arguments: list[str] = []
                if input_seek > 0:
                    source_input_arguments += ['-ss', f'{input_seek:.6f}']
                source_input_arguments += [
                    '-i', self.recorded_program.recorded_video.file_path,
                    '-t', f'{input_duration:.6f}',
                    '-map', stream_specifier,
                ]
                source_filters: list[str] = []
                if rendition.channel == 'main':
                    source_filters.append('pan=mono|c0=c0')
                elif rendition.channel == 'sub':
                    source_filters.append('pan=mono|c0=c1')

                if expected_channel_layout is not None and expected_channel_count is not None:
                    # 可変AACを同じstateful filter graphへ直接入れると、Stereo/Monaural切替で
                    # graphが再初期化され、trim・resampler・padの状態が失われる。第1段は
                    # layout変換だけを行い、元PTSを保持した固定layout PCM/NUTへ正規化する。
                    source_filters.append(
                        'aformat=sample_fmts=flt:sample_rates=48000:'
                        f'channel_layouts={expected_channel_layout}'
                    )
                    normalization_command = [
                        LIBRARY_PATH['FFmpeg8'], '-hide_banner', '-loglevel', 'error',
                        *source_input_arguments,
                        '-af', ','.join(source_filters),
                        '-vn', '-c:a', 'pcm_f32le', '-ac', str(expected_channel_count), '-ar', '48000',
                        '-f', 'nut',
                    ]
                else:
                    # 未知の明示layoutを標準layoutへ推測するとチャンネル配置を壊すため、
                    # 安全に固定できないAACだけは従来の単段経路を維持する。
                    source_filters.extend([
                        'aresample=48000',
                        f'atrim=start_sample={trim_start_samples}:'
                        f'end_sample={trim_start_samples + total_input_samples}',
                        'asetpts=N/SR/TB',
                    ])
                    command += [*source_input_arguments, '-af', ','.join(source_filters)]
            else:
                # 入力読込・Track欠落のどちらでもgeneration全体を同じcodec/layoutの無音で作り直す。
                # 一部segmentだけを差し替えるとencoder stateが切れて継ぎ目が生じるため禁止する。
                channel_layout = self.__getSilentAudioChannelLayout(rendition, configuration_time)
                command += [
                    '-f', 'lavfi', '-i', f'anullsrc=r=48000:cl={channel_layout}',
                    '-af', f'atrim=end_sample={total_input_samples},asetpts=N/SR/TB',
                ]

            encoder_arguments: list[str] = []
            if audio_codec == 'aac':
                frame_samples = self.AAC_PACKET_SAMPLES
                encoder_delay = self.AAC_ENCODER_DELAY_SAMPLES
                encoder_arguments += ['-vn', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000']
            else:
                frame_samples = 960
                encoder_delay = self.OPUS_ENCODER_DELAY_SAMPLES
                bitrate = self.getOpusBitrate(expected_channel_count) \
                    if expected_channel_count is not None else None
                if expected_channel_count is None or bitrate is None:
                    logging.error(
                        '[RecordedFMP4Stream] Opus channel layout became unavailable during generation encoding. '
                        f'[recorded_video_id: {self.recorded_program.recorded_video.id}, '
                        f'rendition: {rendition.id}, audio_generation: '
                        f'{self.__getTranscodedAudioGeneration(first_segment)}]'
                    )
                    return False
                encoder_arguments += [
                    '-vn', '-c:a', 'libopus', '-b:a', str(bitrate), '-ar', '48000',
                    '-vbr', 'on', '-application', 'audio', '-frame_duration', '20',
                    '-compression_level', '10',
                ]
                if expected_channel_count >= 3:
                    encoder_arguments += ['-mapping_family', '1']
            if expected_channel_count is not None:
                # encoder側にもチャンネル数を明示し、initのAudioSpecificConfig/dOpsを
                # generationの期待値から変化させない。
                encoder_arguments += ['-ac', str(expected_channel_count)]

            # encoder delayを含む最後のpacketまで出力させる。内部境界はframe gridに揃っており、
            # generation終端のpaddingだけをMP4 muxerの最終sample durationでclipする。
            frame_limit = math.ceil((total_input_samples + encoder_delay) / frame_samples)
            encoder_arguments += ['-frames:a', str(frame_limit)]
            cumulative_samples = 0
            split_samples: list[int] = []
            for timing in timings[:-1]:
                cumulative_samples += timing.sample_count
                split_samples.append(cumulative_samples + encoder_delay)

            with tempfile.TemporaryDirectory(prefix='konomitv-bs4k-audio-generation-') as temporary_directory:
                temporary_directory_path = Path(temporary_directory)
                normalized_audio_path = temporary_directory_path / 'normalized-audio.nut'
                output_pattern = str(Path(temporary_directory) / 'segment-%06d.mp4')
                if normalization_command is not None:
                    assert expected_channel_layout is not None
                    normalization_command.append(str(normalized_audio_path))
                    # 第2段は固定layout NUTだけを入力するため、PTS補完・trim・padの状態が
                    # Stereo/Monaural切替で再初期化されない。NUTのPCM layoutは明示指定し、
                    # 元PTSの64ms欠落を同じ位置へ無音として補完してから予定sample数へ揃える。
                    command = [
                        LIBRARY_PATH['FFmpeg8'], '-hide_banner', '-loglevel', 'error',
                        '-ch_layout:a:0', expected_channel_layout,
                        '-i', str(normalized_audio_path),
                        '-map', '0:a:0',
                        '-af', ','.join([
                            f'aresample=48000:out_chlayout={expected_channel_layout}:'
                            'async=1:min_hard_comp=0.001:first_pts=0',
                            f'atrim=start_sample={trim_start_samples}',
                            f'apad=whole_len={total_input_samples}',
                            f'atrim=end_sample={total_input_samples}',
                            'asetpts=N/SR/TB',
                        ]),
                    ]

                # 入力経路に関係なく、codecとsample上限は最終段だけへ適用する。
                command += encoder_arguments
                if len(split_samples) > 0:
                    command += [
                        '-segment_times',
                        ','.join(f'{sample / self.AUDIO_SAMPLE_RATE:.9f}' for sample in split_samples),
                    ]
                command += [
                    '-initial_offset', f'{-encoder_delay / self.AUDIO_SAMPLE_RATE:.9f}',
                    '-f', 'segment', '-segment_format', 'mp4',
                    '-segment_format_options',
                    'movflags=+frag_keyframe+delay_moov+default_base_moof+negative_cts_offsets',
                    '-reset_timestamps', '0',
                    output_pattern,
                ]
                async with self._cpu_semaphore:
                    if normalization_command is not None:
                        normalization_process = await asyncio.create_subprocess_exec(
                            *normalization_command,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        _, stderr = await normalization_process.communicate()
                        if normalization_process.returncode != 0 or normalized_audio_path.is_file() is False:
                            logging.warning(
                                '[RecordedFMP4Stream] FFmpeg 8 audio normalization source failed; '
                                'retrying the entire generation with silence. '
                                f'[recorded_video_id: {self.recorded_program.recorded_video.id}, '
                                f'rendition: {rendition.id}, audio_generation: '
                                f'{self.__getTranscodedAudioGeneration(first_segment)}, '
                                f'stderr: {stderr.decode(errors="ignore").strip()}]'
                            )
                            continue
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await process.communicate()
                output_paths = sorted(Path(temporary_directory).glob('segment-*.mp4'))
                if process.returncode != 0 or len(output_paths) != len(generation_segments):
                    if use_recorded_audio:
                        logging.warning(
                            '[RecordedFMP4Stream] FFmpeg 8 audio generation source failed; '
                            'retrying the entire generation with silence. '
                            f'[recorded_video_id: {self.recorded_program.recorded_video.id}, '
                            f'rendition: {rendition.id}, audio_generation: '
                            f'{self.__getTranscodedAudioGeneration(first_segment)}, '
                            f'stderr: {stderr.decode(errors="ignore").strip()}]'
                        )
                    continue
                outputs = [await asyncio.to_thread(path.read_bytes) for path in output_paths]

            media_fragments: list[bytes] = []
            common_init_data = b''
            next_decode_sample = first_timing.start_sample
            is_valid = True
            for index, (generation_segment, timing, output) in enumerate(zip(
                generation_segments,
                timings,
                outputs,
                strict=True,
            )):
                init_data, media_data = self.splitFragmentedMP4(output)
                expected_duration = timing.sample_count
                if index == 0:
                    expected_duration += encoder_delay
                if self.validateTranscodedAudioFragmentTimeline(
                    init_data,
                    media_data,
                    audio_codec,
                    expected_start_sample=0,
                    expected_sample_count=expected_duration,
                    expected_channel_count=expected_channel_count,
                ) is False:
                    is_valid = False
                    break
                try:
                    normalized_media = self.normalizeFragmentTimeline(
                        init_data,
                        media_data,
                        next_decode_sample / self.AUDIO_SAMPLE_RATE,
                        generation_segment.sequence,
                    )
                except ValueError:
                    is_valid = False
                    break
                if self.validateTranscodedAudioFragmentTimeline(
                    init_data,
                    normalized_media,
                    audio_codec,
                    expected_start_sample=next_decode_sample,
                    expected_sample_count=expected_duration,
                    expected_channel_count=expected_channel_count,
                ) is False:
                    is_valid = False
                    break
                if index == 0:
                    patched_init = self.patchAudioInitializationEditList(init_data, encoder_delay)
                    if (
                        patched_init is None or
                        self.validateAudioInitializationDelay(patched_init, audio_codec, encoder_delay) is False
                    ):
                        is_valid = False
                        break
                    common_init_data = patched_init
                media_fragments.append(normalized_media)
                next_decode_sample += expected_duration

            if is_valid and len(common_init_data) > 0 and len(media_fragments) == len(generation_segments):
                await RecordedFMP4CacheManager.writeAtomic(init_path, common_init_data)
                for generation_segment, media_data in zip(generation_segments, media_fragments, strict=True):
                    await RecordedFMP4CacheManager.writeAtomic(segment_paths[generation_segment.sequence], media_data)
                return True
            if use_recorded_audio:
                # 実音声を正常に生成できた後の検証失敗は、入力Track欠落とは異なる実装上の異常。
                # ここで無音へ置換するとHTTP上は成功したまま音声だけが消えるため、失敗を呼び出し元へ返す。
                logging.error(
                    '[RecordedFMP4Stream] FFmpeg 8 recorded audio generation failed validation; '
                    'refusing to replace recorded audio with silence. '
                    f'[recorded_video_id: {self.recorded_program.recorded_video.id}, '
                    f'rendition: {rendition.id}, audio_generation: '
                    f'{self.__getTranscodedAudioGeneration(first_segment)}]'
                )
                return False

        logging.error(
            '[RecordedFMP4Stream] FFmpeg 8 audio generation failed. '
            f'[recorded_video_id: {self.recorded_program.recorded_video.id}, rendition: {rendition.id}, '
            f'audio_generation: {self.__getTranscodedAudioGeneration(first_segment)}, '
            f'stderr: {stderr.decode(errors="ignore").strip()}]'
        )
        return False

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
            codec = self.__getAudioCacheCodec(segment, rendition),
            bit_depth = 0,
            is_24fps = False,
            backend = 'FFmpeg',
            configuration_generation = self.__getTranscodedAudioGeneration(segment),
            seek_generation = 0,
            rendition = rendition.id,
        )
        return RecordedFMP4CacheManager.buildPath(
            self.recorded_program.recorded_video,
            variant,
            'audio-init' if is_init else 'audio',
            self.__getTranscodedAudioGeneration(segment) if is_init else segment.sequence,
        )

    def __getAudioCacheCodec(
        self,
        segment: RecordedFMP4Segment,
        rendition: RecordedAudioRendition,
    ) -> str:
        """要求方式・実効方式・ビットレートを含む音声cache codecキーを返す。"""

        channel_layout = self.__getAudioRenditionChannelLayout(rendition, segment.start_time) or 'unfixed'
        if self._effective_audio_codec == 'opus':
            channels = self.__getAudioRenditionChannelCount(rendition, segment.start_time)
            bitrate = self.getOpusBitrate(channels) if channels is not None else None
            if bitrate is None:
                return 'opus-invalid'
            return f'opus-continuous-{bitrate}-{channel_layout}'
        if self.encoding_options.audio_codec == 'opus':
            return f'opus-fallback-aac-continuous-192000-{channel_layout}'
        return f'aac-continuous-192000-{channel_layout}'

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
            return None if len(self.recorded_program.recorded_video.audio_track_timeline) == 0 else False
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

    def __getAudioRenditionChannelLayout(
        self,
        rendition: RecordedAudioRendition,
        start_time: float | None = None,
    ) -> str | None:
        """指定時刻のレンディションを安全に固定できるchannel layoutへ解決する。

        Args:
            rendition: 固定対象の論理音声レンディション。
            start_time: 録画先頭0秒基準の構成確認時刻。

        Returns:
            FFmpegへ明示できる既知layout。未知の明示layoutは推測せずNone。
        """

        if rendition.channel in ('main', 'sub'):
            return 'mono'
        source_track = self.__getAudioSourceTrack(rendition, start_time)
        if source_track is None:
            return None
        channel_layout = str(source_track.get('channel_layout') or '').strip().lower()
        # FFmpegが明示した既知1～8ch layoutは名前ごと保持し、5.0/6.1/quadなどを
        # stereoへ暗黙変換しない。未知の明示layoutも表示ラベルから推測しない。
        if channel_layout != '' and self.getAudioChannelCount(source_track) is not None:
            return {
                # FFmpeg 8 の anullsrc が受理しない索引上の別名を同等の canonical 名へ直す。
                'stereo downmix': 'stereo',
                '5.1(back)': '5.1',
            }.get(channel_layout, channel_layout)
        if channel_layout != '':
            return None

        # channel_layoutを持たない古い索引だけ、明確な表示ラベルまたは宣言チャンネル数を
        # FFmpeg標準layoutへ補完する。
        channel_label = str(source_track.get('channel') or '').lower()
        if 'monaural' in channel_label or 'mono' in channel_label:
            return 'mono'
        if '5.1' in channel_label:
            return '5.1'
        if '7.1' in channel_label:
            return '7.1'
        declared_channels = self.getDeclaredAudioChannelCount(source_track)
        if declared_channels is not None:
            return {
                1: 'mono',
                2: 'stereo',
                3: '3.0',
                4: '4.0',
                5: '5.0',
                6: '5.1',
                7: '6.1',
                8: '7.1',
            }[declared_channels]
        return None

    def __getSilentAudioChannelLayout(
        self,
        rendition: RecordedAudioRendition,
        start_time: float | None = None,
    ) -> str:
        """Track消失区間へ挿入する無音音声のchannel layoutを返す。

        Args:
            rendition: 無音を生成する論理音声レンディション。
            start_time: 録画先頭0秒基準の構成確認時刻。

        Returns:
            anullsrcへ渡す既知layout。判断材料がなければ従来どおりstereo。
        """

        channel_layout = self.__getAudioRenditionChannelLayout(rendition, start_time)
        if channel_layout is not None:
            return channel_layout
        source_track = self.__getAudioSourceTrack(rendition, start_time)
        declared_channels = self.getDeclaredAudioChannelCount(source_track) if source_track is not None else None
        if declared_channels is not None:
            # 実音声では未知layoutを推測しないが、入力自体がない無音fallbackでは
            # master playlistへ宣言済みのチャンネル数を保つ必要がある。
            return {
                1: 'mono',
                2: 'stereo',
                3: '3.0',
                4: '4.0',
                5: '5.0',
                6: '5.1',
                7: '6.1',
                8: '7.1',
            }[declared_channels]
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
    def __iterateMP4Boxes(
        data: bytes | bytearray,
        start: int,
        end: int,
    ) -> Iterator[tuple[int, int, int, bytes]]:
        """指定範囲の直下にある検証済みISO BMFF boxを順に返す。"""

        offset = start
        while offset + 8 <= end:
            box_size = int.from_bytes(data[offset:offset + 4], 'big')
            box_type = bytes(data[offset + 4:offset + 8])
            header_size = 8
            if box_size == 1:
                if offset + 16 > end:
                    raise ValueError('The fMP4 data contains a truncated extended box header.')
                box_size = int.from_bytes(data[offset + 8:offset + 16], 'big')
                header_size = 16
            elif box_size == 0:
                box_size = end - offset
            if box_size < header_size or offset + box_size > end:
                raise ValueError('The fMP4 data contains an invalid box size.')
            yield offset, box_size, header_size, box_type
            offset += box_size
        if offset != end:
            raise ValueError('The fMP4 data ends with a truncated box header.')

    @staticmethod
    def __findMP4Box(data: bytes, box_type: bytes) -> tuple[int, int, int] | None:
        """任意階層にあるsize検証済みboxの位置を返す。"""

        search_offset = 0
        while True:
            type_offset = data.find(box_type, search_offset)
            if type_offset < 0:
                return None
            if type_offset >= 4:
                box_start = type_offset - 4
                box_size = int.from_bytes(data[box_start:type_offset], 'big')
                header_size = 8
                if box_size == 1 and box_start + 16 <= len(data):
                    box_size = int.from_bytes(data[box_start + 8:box_start + 16], 'big')
                    header_size = 16
                if box_size >= header_size and box_start + box_size <= len(data):
                    return box_start, box_size, header_size
            search_offset = type_offset + 1

    @classmethod
    def patchAudioInitializationEditList(cls, init_data: bytes, encoder_delay: int) -> bytes | None:
        """generation共通initの先頭codec delayだけをedit listで除外する。

        Args:
            init_data: segment muxer先頭出力から分離した初期化セグメント。
            encoder_delay: AAC primingまたはOpus pre-skipの48kHz sample数。

        Returns:
            先頭media_timeをcodec delayへ補正したinit。安全に補正できない場合はNone。
        """

        elst_box = cls.__findMP4Box(init_data, b'elst')
        if elst_box is None or encoder_delay < 0:
            return None
        elst_offset, elst_size, elst_header_size = elst_box
        payload_offset = elst_offset + elst_header_size
        box_end = elst_offset + elst_size
        if payload_offset + 8 > box_end:
            return None
        version = init_data[payload_offset]
        entry_count = int.from_bytes(init_data[payload_offset + 4:payload_offset + 8], 'big')
        if entry_count < 1:
            return None
        # version 0は32bit、version 1は64bitのsegment_duration/media_timeを持つ。
        if version == 0:
            media_time_offset = payload_offset + 12
            media_time_size = 4
        elif version == 1:
            media_time_offset = payload_offset + 16
            media_time_size = 8
        else:
            return None
        if media_time_offset + media_time_size + 4 > box_end:
            return None
        if encoder_delay >= 1 << (media_time_size * 8 - 1):
            return None
        patched_init = bytearray(init_data)
        patched_init[media_time_offset:media_time_offset + media_time_size] = encoder_delay.to_bytes(
            media_time_size,
            'big',
            signed=True,
        )
        return bytes(patched_init)

    @classmethod
    def validateAudioInitializationDelay(
        cls,
        init_data: bytes,
        audio_codec: Literal['aac', 'opus'],
        encoder_delay: int,
    ) -> bool:
        """共通initがcodec delayをgeneration先頭で一度だけ除外するか検証する。"""

        elst_box = cls.__findMP4Box(init_data, b'elst')
        if elst_box is None:
            return False
        elst_offset, elst_size, elst_header_size = elst_box
        payload_offset = elst_offset + elst_header_size
        box_end = elst_offset + elst_size
        if payload_offset + 8 > box_end:
            return False
        version = init_data[payload_offset]
        entry_count = int.from_bytes(init_data[payload_offset + 4:payload_offset + 8], 'big')
        if entry_count != 1:
            return False
        if version == 0:
            duration_offset = payload_offset + 8
            duration_size = 4
            media_time_offset = payload_offset + 12
            media_time_size = 4
        elif version == 1:
            duration_offset = payload_offset + 8
            duration_size = 8
            media_time_offset = payload_offset + 16
            media_time_size = 8
        else:
            return False
        rate_offset = media_time_offset + media_time_size
        if rate_offset + 4 > box_end:
            return False
        segment_duration = int.from_bytes(
            init_data[duration_offset:duration_offset + duration_size],
            'big',
        )
        media_time = int.from_bytes(
            init_data[media_time_offset:media_time_offset + media_time_size],
            'big',
            signed=True,
        )
        if segment_duration != 0 or media_time != encoder_delay or init_data[rate_offset:rate_offset + 4] != b'\x00\x01\x00\x00':
            return False
        if audio_codec == 'aac':
            return True

        dops_box = cls.__findMP4Box(init_data, b'dOps')
        if dops_box is None:
            return False
        dops_offset, dops_size, dops_header_size = dops_box
        dops_payload = dops_offset + dops_header_size
        if dops_payload + 8 > dops_offset + dops_size:
            return False
        pre_skip = int.from_bytes(init_data[dops_payload + 2:dops_payload + 4], 'big')
        input_sample_rate = int.from_bytes(init_data[dops_payload + 4:dops_payload + 8], 'big')
        return pre_skip == encoder_delay and input_sample_rate == cls.AUDIO_SAMPLE_RATE

    @classmethod
    def inspectAudioFragment(
        cls,
        init_data: bytes,
        media_data: bytes,
    ) -> RecordedAudioFragmentInfo | None:
        """fMP4音声fragmentの全moofからsample数・duration・decode連続性を検証する。"""

        mdhd_box = cls.__findMP4Box(init_data, b'mdhd')
        if mdhd_box is None:
            return None
        mdhd_offset, mdhd_size, mdhd_header_size = mdhd_box
        mdhd_payload = mdhd_offset + mdhd_header_size
        if mdhd_payload + 4 > mdhd_offset + mdhd_size:
            return None
        mdhd_version = init_data[mdhd_payload]
        timescale_offset = mdhd_payload + (20 if mdhd_version == 1 else 12)
        if timescale_offset + 4 > mdhd_offset + mdhd_size:
            return None
        timescale = int.from_bytes(init_data[timescale_offset:timescale_offset + 4], 'big')
        if timescale <= 0:
            return None

        # trunでsample durationが省略された場合に使うtrex既定値をtrack ID別に保持する。
        trex_defaults: dict[int, int] = {}
        search_offset = 0
        while True:
            type_offset = init_data.find(b'trex', search_offset)
            if type_offset < 0:
                break
            box = cls.__findMP4Box(init_data[search_offset:], b'trex')
            if box is None:
                break
            relative_offset, box_size, header_size = box
            box_offset = search_offset + relative_offset
            payload_offset = box_offset + header_size
            if payload_offset + 16 <= box_offset + box_size:
                track_id = int.from_bytes(init_data[payload_offset + 4:payload_offset + 8], 'big')
                default_duration = int.from_bytes(init_data[payload_offset + 12:payload_offset + 16], 'big')
                trex_defaults[track_id] = default_duration
            search_offset = box_offset + box_size

        sample_durations: list[int] = []
        first_decode_time: int | None = None
        previous_decode_end: int | None = None
        try:
            top_level_boxes = cls.__iterateMP4Boxes(media_data, 0, len(media_data))
            for moof_offset, moof_size, moof_header_size, moof_type in top_level_boxes:
                if moof_type != b'moof':
                    continue
                for traf_offset, traf_size, traf_header_size, traf_type in cls.__iterateMP4Boxes(
                    media_data,
                    moof_offset + moof_header_size,
                    moof_offset + moof_size,
                ):
                    if traf_type != b'traf':
                        continue
                    track_id: int | None = None
                    default_duration: int | None = None
                    decode_time: int | None = None
                    trun_boxes: list[tuple[int, int, int]] = []
                    for child_offset, child_size, child_header_size, child_type in cls.__iterateMP4Boxes(
                        media_data,
                        traf_offset + traf_header_size,
                        traf_offset + traf_size,
                    ):
                        payload_offset = child_offset + child_header_size
                        child_end = child_offset + child_size
                        if child_type == b'tfhd':
                            if payload_offset + 8 > child_end:
                                return None
                            flags = int.from_bytes(media_data[payload_offset + 1:payload_offset + 4], 'big')
                            track_id = int.from_bytes(media_data[payload_offset + 4:payload_offset + 8], 'big')
                            cursor = payload_offset + 8
                            if flags & 0x000001:
                                cursor += 8
                            if flags & 0x000002:
                                cursor += 4
                            if flags & 0x000008:
                                if cursor + 4 > child_end:
                                    return None
                                default_duration = int.from_bytes(media_data[cursor:cursor + 4], 'big')
                                cursor += 4
                            if flags & 0x000010:
                                cursor += 4
                            if flags & 0x000020:
                                cursor += 4
                            if cursor > child_end:
                                return None
                        elif child_type == b'tfdt':
                            if payload_offset + 8 > child_end:
                                return None
                            version = media_data[payload_offset]
                            decode_time_size = 8 if version == 1 else 4
                            if payload_offset + 4 + decode_time_size > child_end:
                                return None
                            decode_time = int.from_bytes(
                                media_data[payload_offset + 4:payload_offset + 4 + decode_time_size],
                                'big',
                            )
                        elif child_type == b'trun':
                            trun_boxes.append((child_offset, child_size, child_header_size))
                    if track_id is None or decode_time is None or len(trun_boxes) == 0:
                        return None
                    if default_duration is None:
                        default_duration = trex_defaults.get(track_id)

                    traf_durations: list[int] = []
                    for trun_offset, trun_size, trun_header_size in trun_boxes:
                        payload_offset = trun_offset + trun_header_size
                        trun_end = trun_offset + trun_size
                        if payload_offset + 8 > trun_end:
                            return None
                        flags = int.from_bytes(media_data[payload_offset + 1:payload_offset + 4], 'big')
                        sample_count = int.from_bytes(media_data[payload_offset + 4:payload_offset + 8], 'big')
                        cursor = payload_offset + 8
                        if flags & 0x000001:
                            cursor += 4
                        if flags & 0x000004:
                            cursor += 4
                        for _ in range(sample_count):
                            if flags & 0x000100:
                                if cursor + 4 > trun_end:
                                    return None
                                duration = int.from_bytes(media_data[cursor:cursor + 4], 'big')
                                cursor += 4
                            elif default_duration is not None:
                                duration = default_duration
                            else:
                                return None
                            if flags & 0x000200:
                                cursor += 4
                            if flags & 0x000400:
                                cursor += 4
                            if flags & 0x000800:
                                cursor += 4
                            if cursor > trun_end or duration <= 0:
                                return None
                            traf_durations.append(duration)

                    traf_duration = sum(traf_durations)
                    if first_decode_time is None:
                        first_decode_time = decode_time
                    if previous_decode_end is not None and decode_time != previous_decode_end:
                        return None
                    previous_decode_end = decode_time + traf_duration
                    sample_durations.extend(traf_durations)
        except ValueError:
            return None

        if first_decode_time is None or previous_decode_end is None or len(sample_durations) == 0:
            return None
        return RecordedAudioFragmentInfo(
            timescale=timescale,
            first_decode_time=first_decode_time,
            total_duration=previous_decode_end - first_decode_time,
            sample_count=len(sample_durations),
            sample_durations=tuple(sample_durations),
        )

    @classmethod
    def validateTranscodedAudioFragmentTimeline(
        cls,
        init_data: bytes,
        media_data: bytes,
        audio_codec: Literal['aac', 'opus'],
        expected_start_sample: int,
        expected_sample_count: int,
        expected_channel_count: int | None = None,
    ) -> bool:
        """AAC/Opus変換fragmentが48kHzの予定timelineを連続して覆うか判定する。"""

        info = cls.inspectAudioFragment(init_data, media_data)
        expected_codec = 'mp4a.40.2' if audio_codec == 'aac' else 'opus'
        frame_samples = cls.AAC_PACKET_SAMPLES if audio_codec == 'aac' else 960
        expected_packet_count = math.ceil(expected_sample_count / frame_samples)
        expected_last_duration = expected_sample_count % frame_samples or frame_samples
        if info is None or not (
            cls.extractAudioCodecString(init_data) == expected_codec and
            (
                expected_channel_count is None or
                cls.extractAudioInitializationChannelCount(init_data, audio_codec) == expected_channel_count
            ) and
            info.timescale == cls.AUDIO_SAMPLE_RATE and
            info.first_decode_time == expected_start_sample and
            info.total_duration == expected_sample_count and
            info.sample_count == expected_packet_count
        ):
            return False

        # FFmpegのsegment muxerは境界時刻をTrack timebaseへ丸める際、連続する2 packetを
        # 1023/1025 samples（Opusでは959/961）のような相殺済み±1 sampleへすることがある。
        # 合計時間とdecode連続性はinspectAudioFragment()で厳密に確認済みなので、この隣接する
        # 補償ペアだけを許容し、それ以外の短縮・伸長や末尾partial packetのずれは拒否する。
        # 末尾がpartial packetのときだけ、その長さを補償対象から外して完全一致を要求する。
        # 末尾も完全frameなら、直前packetとの補償ペアが境界に現れても同じ規則で許容する。
        if expected_last_duration != frame_samples and info.sample_durations[-1] != expected_last_duration:
            return False
        full_frame_durations = info.sample_durations[:-1] \
            if expected_last_duration != frame_samples else info.sample_durations
        duration_index = 0
        while duration_index < len(full_frame_durations):
            duration = full_frame_durations[duration_index]
            if duration == frame_samples:
                duration_index += 1
                continue
            if (
                duration in (frame_samples - 1, frame_samples + 1) and
                duration_index + 1 < len(full_frame_durations) and
                full_frame_durations[duration_index + 1] == frame_samples * 2 - duration
            ):
                duration_index += 2
                continue
            return False

        return True

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
