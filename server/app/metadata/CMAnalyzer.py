from __future__ import annotations

import asyncio
import configparser
import errno
import hashlib
import json
import math
import os
import re
import signal
import struct
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Literal, Protocol, cast

import av
from typing_extensions import TypedDict

from app import logging
from app.utils.KonomiTVBS4KMMTTLV import MMT_TLV_FILE_EXTENSIONS


CMAnalyzerStatus = Literal[
    'completed',
    'analysis_failed',
    'unsupported',
    'interrupted',
]
CMDecodeMode = Literal['Hardware', 'CPU']
KonomiTVBS4KLogicalAudioRebuildPreference = Literal['PyAV', 'FFmpegAfterPreviousFailure']
KonomiTVBS4KLogicalAudioRebuildStrategy = Literal[
    'PyAV',
    'FFmpegPrimaryAfterPreviousFailure',
    'FFmpegFallbackAfterSignal',
    'FFmpegFallbackAfterReportedFailure',
]
CMAnalysisStage = Literal[
    'PreparingMedia',
    'IndexingMedia',
    'ChapterAnalyzing',
    'LogoAnalyzing',
    'HardwareFallback',
    'CombiningCM',
]
CMAnalysisStageCallback = Callable[[CMAnalysisStage, float | None], Awaitable[None]]


class CMSectionJSON(TypedDict):
    """CM 区間を秒単位の半開区間で表す。"""

    start_time: float
    end_time: float


@dataclass(frozen=True, slots=True)
class CMInputDescriptor:
    """実ファイルを FFprobe して確定した CM 解析入力。"""

    format_name: str | None
    video_stream_index: int
    audio_stream_index: int | None
    video_codec_name: str | None
    pixel_format: str | None
    bit_depth: int | None
    width: int
    height: int
    field_order: str | None
    time_base: Fraction | None
    source_frame_rate: Fraction | None
    duration_seconds: float
    program_id: int | None
    service_id: int | None
    has_variable_video_format: bool = False
    has_variable_audio_stream: bool = False
    video_start_time_seconds: float | None = None
    video_duration_seconds: float | None = None
    audio_start_time_seconds: float | None = None
    audio_stream_id: int | None = None

    def toJSON(self) -> dict[str, object]:
        """解析 key に利用できる決定的な JSON 値へ変換する。"""

        return {
            'format_name': self.format_name,
            'video_stream_index': self.video_stream_index,
            'audio_stream_index': self.audio_stream_index,
            'video_codec_name': self.video_codec_name,
            'pixel_format': self.pixel_format,
            'bit_depth': self.bit_depth,
            'width': self.width,
            'height': self.height,
            'field_order': self.field_order,
            'time_base': str(self.time_base) if self.time_base is not None else None,
            'source_frame_rate': str(self.source_frame_rate) if self.source_frame_rate is not None else None,
            'duration_seconds': self.duration_seconds,
            'program_id': self.program_id,
            'service_id': self.service_id,
            'has_variable_video_format': self.has_variable_video_format,
            'has_variable_audio_stream': self.has_variable_audio_stream,
            'video_start_time_seconds': self.video_start_time_seconds,
            'video_duration_seconds': self.video_duration_seconds,
            'audio_start_time_seconds': self.audio_start_time_seconds,
            'audio_stream_id': self.audio_stream_id,
        }


@dataclass(frozen=True, slots=True)
class CMAnalyzerRequest:
    """汎用 CM 解析バックエンドへ渡す要求。"""

    recorded_file_path: Path
    work_directory: Path
    service_id: int | None = None
    logo_paths: tuple[Path, ...] = ()
    hardware_device: str | None = None
    hardware_environment: Mapping[str, str] | None = None
    duration_seconds: float = 0.0
    has_variable_video_format: bool = False
    input_descriptor: CMInputDescriptor | None = None
    stage_callback: CMAnalysisStageCallback | None = None
    konomitv_bs4k_logical_audio_rebuild_preference: KonomiTVBS4KLogicalAudioRebuildPreference = 'PyAV'


@dataclass(frozen=True, slots=True)
class CMAnalyzerResult:
    """解析バックエンドから返す構造化結果。"""

    status: CMAnalyzerStatus
    chapter_file: Path | None
    sections: tuple[CMSectionJSON, ...] = ()
    matched_logo: str | None = None
    analysis_fps: str | None = None
    library: str | None = None
    analyzer_version: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    warnings: tuple[str, ...] = ()
    descriptor: CMInputDescriptor | None = None
    decode_mode: CMDecodeMode | None = None
    total_frames: int | None = None
    konomitv_bs4k_logical_audio_rebuild_strategy: KonomiTVBS4KLogicalAudioRebuildStrategy | None = None

    def toJSON(self) -> dict[str, object]:
        """ログ・テスト用の JSON 互換値を返す。"""

        return {
            'status': self.status,
            'sections': list(self.sections),
            'matched_logo': self.matched_logo,
            'analysis_fps': self.analysis_fps,
            'chapter_file': str(self.chapter_file) if self.chapter_file is not None else None,
            'library': self.library,
            'analyzer_version': self.analyzer_version,
            'error_code': self.error_code,
            'error_message': self.error_message,
            'warnings': list(self.warnings),
            'descriptor': self.descriptor.toJSON() if self.descriptor is not None else None,
            'decode_mode': self.decode_mode,
            'total_frames': self.total_frames,
            'konomitv_bs4k_logical_audio_rebuild_strategy': (
                self.konomitv_bs4k_logical_audio_rebuild_strategy
            ),
        }


@dataclass(frozen=True, slots=True)
class CMLogoValidationResult:
    """将来のロゴ生成入口との互換用。現行解析からは呼び出さない。"""

    matched: bool
    logo_ratio: float | None
    error_code: str | None = None
    error_message: str | None = None


class CMAnalyzer(Protocol):
    """CM 解析バックエンドの非同期インターフェイス。"""

    @property
    def runtimeFingerprint(self) -> dict[str, object]:
        """解析結果を無効化すべきランタイム構成を返す。"""
        ...

    async def resolveInputDescriptor(self, request: CMAnalyzerRequest) -> CMInputDescriptor:
        """実ファイルをprobeし、解析対象ストリームを確定する。"""
        ...

    async def analyze(self, request: CMAnalyzerRequest) -> CMAnalyzerResult:
        """録画を解析し、検出したCM区間を構造化結果として返す。"""
        ...


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    return_code: int
    output: str
    error_output: str = ''
    konomitv_bs4k_logical_audio_rebuild_strategy: KonomiTVBS4KLogicalAudioRebuildStrategy | None = None

    @property
    def diagnostic(self) -> str:
        return '\n'.join(part for part in (self.output, self.error_output) if part)[-4000:]


@dataclass(frozen=True, slots=True)
class _LogicalAudioRebuildReport:
    """隔離rebuilderがstdoutへ返した段階付き統計。"""

    stage: str
    statistics: dict[str, int]
    error: str | None


@dataclass(frozen=True, slots=True)
class _LogoFrameOutput:
    path: Path | None
    matched_logo: str | None
    frame_count: int | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _PreparedMedia:
    """一つのCM解析job内で全native解析が共有する正規化媒体。"""

    media_path: Path
    index_path: Path
    video_stream_index: int
    audio_path: Path
    audio_index_path: Path
    audio_stream_index: int


class CMInputUnsupportedError(Exception):
    """入力probeだけで確定できる、同一入力では再試行不要な非対応状態。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message


class GenericCMAnalyzer:
    """全登録メディアを FFMS2 の共有媒体・索引で解析する CM 解析器。"""

    ANALYSIS_FPS = Fraction(30_000, 1001)
    ANALYZER_VERSION = 'cm-9'
    NORMALIZATION_POLICY_VERSION = 6
    _LOGO_FRAME_MAX_WORKERS = 15
    _LOGO_FRAME_MIN_FRAMES_PER_WORKER = 600
    # stream列挙だけのFFprobeは録画尺に比例しないため、索引側と同じ短い上限で固着を防ぐ。
    _PROBE_PROCESS_TIMEOUT_SECONDS = 60.0
    # 全編処理は低速なCPU fallbackも許容しつつ、停止した1件が直列CM解析を永久に塞がない上限にする。
    _MEDIA_PROCESS_MIN_TIMEOUT_SECONDS = 30 * 60.0
    _MEDIA_PROCESS_MAX_TIMEOUT_SECONDS = 24 * 60 * 60.0
    _MEDIA_PROCESS_DURATION_MULTIPLIER = 8.0
    _PROCESS_TERMINATE_TIMEOUT_SECONDS = 10.0
    _TRIM_PATTERN = re.compile(r'\btrim\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)', re.IGNORECASE)
    _VIDEO_FRAME_COUNT_PATTERN = re.compile(r'Video Frames:\s*(\d+)\b')
    _FINAL_SCENE_PATTERN = re.compile(r'^#\s*SCPos:\s*(\d+)\s+(\d+)\s*$', re.MULTILINE)
    _HARDWARE_FAILURE_MARKER = 'ffms2-hw:'
    _DECODER_UNAVAILABLE_MARKERS = (
        'codec not found', 'decoder not found', 'decoder unavailable', 'no decoder',
        'unknown decoder', 'unsupported codec',
    )
    _STORAGE_INSUFFICIENT_MARKERS = ('no space left on device', 'disk quota exceeded', 'enospc', 'edquot')
    _STORAGE_UNAVAILABLE_MARKERS = ('permission denied', 'read-only file system', 'erofs', 'eacces')

    def __init__(
        self,
        runtime_directory: Path = Path('/code/server/thirdparty/CMAnalysis'),
        ffms2_path: Path | None = None,
        ffmsindex_path: Path | None = None,
        chapter_executable_path: Path | None = None,
        logoframe_path: Path | None = None,
        join_logo_scp_path: Path | None = None,
        join_logo_scp_command_path: Path | None = None,
        runtime_manifest_path: Path | None = None,
        ffmpeg_path: Path = Path('/code/server/thirdparty/FFmpeg8/ffmpeg8.elf'),
        ffprobe_path: Path = Path('/code/server/thirdparty/FFmpeg8/ffprobe8.elf'),
    ) -> None:
        self.runtime_directory = runtime_directory
        self.ffms2_path = ffms2_path or runtime_directory / 'libffms2.so'
        self.ffmsindex_path = ffmsindex_path or runtime_directory / 'ffmsindex'
        self.chapter_executable_path = chapter_executable_path or runtime_directory / 'chapter_exe'
        self.logoframe_path = logoframe_path or runtime_directory / 'logoframe'
        self.join_logo_scp_path = join_logo_scp_path or runtime_directory / 'join_logo_scp'
        self.join_logo_scp_command_path = (
            join_logo_scp_command_path or runtime_directory / 'JL/JL_標準.txt'
        )
        self.runtime_manifest_path = runtime_manifest_path or runtime_directory / 'Runtime-Manifest.json'
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.logical_audio_rebuilder_path = Path(__file__).with_name('CMLogicalAudioRebuilder.py')

    @property
    def runtimeFingerprint(self) -> dict[str, object]:
        """解析 key に含める固定ランタイム識別情報。"""

        manifest_sha256, manifest_error = self._runtimeManifestFingerprint()
        media_preparer_sha256, media_preparer_error = self._fileFingerprint(self.ffmpeg_path)
        audio_rebuilder_sha256, audio_rebuilder_error = self._fileFingerprint(
            self.logical_audio_rebuilder_path,
        )
        return {
            'analyzer_version': self.ANALYZER_VERSION,
            'normalization_policy_version': self.NORMALIZATION_POLICY_VERSION,
            'analysis_fps_policy': f'source-with-{self.ANALYSIS_FPS}-fallback',
            'native_runtime_manifest_sha256': manifest_sha256,
            'native_runtime_manifest_error': manifest_error,
            'media_preparer_sha256': media_preparer_sha256,
            'media_preparer_error': media_preparer_error,
            'logical_audio_rebuilder_sha256': audio_rebuilder_sha256,
            'logical_audio_rebuilder_error': audio_rebuilder_error,
            'pyav_version': av.__version__,
            'ffmpeg_library_versions': {
                name: '.'.join(str(part) for part in version)
                for name, version in sorted(av.library_versions.items())
            },
        }

    async def analyze(self, request: CMAnalyzerRequest) -> CMAnalyzerResult:
        """実ストリームを解決し、GPU 一回・CPU 一回の上限で解析する。"""

        missing = [
            str(path) for path in (
                self.ffms2_path,
                self.ffmsindex_path,
                self.chapter_executable_path,
                self.logoframe_path,
                self.join_logo_scp_path,
                self.join_logo_scp_command_path,
                self.runtime_manifest_path,
                self.ffmpeg_path,
                self.ffprobe_path,
                self.logical_audio_rebuilder_path,
            )
            if path.is_file() is False
        ]
        if missing:
            return CMAnalyzerResult(
                status='unsupported',
                chapter_file=None,
                analyzer_version=self.ANALYZER_VERSION,
                error_code='AnalyzerUnavailable',
                error_message=f'Required CM analyzer files are missing: {", ".join(missing)}',
            )
        _, manifest_error = self._runtimeManifestFingerprint()
        if manifest_error is not None:
            return CMAnalyzerResult(
                status='unsupported',
                chapter_file=None,
                analyzer_version=self.ANALYZER_VERSION,
                error_code='RuntimeManifestInvalid',
                error_message=manifest_error,
            )

        try:
            descriptor = request.input_descriptor or await self.resolveInputDescriptor(request)
        except CMInputUnsupportedError as ex:
            return CMAnalyzerResult(
                status='unsupported',
                chapter_file=None,
                analyzer_version=self.ANALYZER_VERSION,
                error_code=ex.code,
                error_message=ex.message,
            )
        except (OSError, ValueError, json.JSONDecodeError) as ex:
            return CMAnalyzerResult(
                status='analysis_failed',
                chapter_file=None,
                analyzer_version=self.ANALYZER_VERSION,
                error_code='MediaProbeFailed',
                error_message=str(ex),
            )
        # 一つの固定Avisynth clipへ安全に正規化できない途中format変更は、
        # 欠落時間軸を正常公開するより入力が変わるまで安定した非対応にする。
        if descriptor.has_variable_video_format:
            return CMAnalyzerResult(
                status='unsupported',
                chapter_file=None,
                analyzer_version=self.ANALYZER_VERSION,
                error_code='VariableVideoFormat',
                descriptor=descriptor,
            )
        if request.work_directory.is_dir() is False:
            return CMAnalyzerResult(
                status='analysis_failed',
                chapter_file=None,
                analyzer_version=self.ANALYZER_VERSION,
                error_code='TemporaryStorageUnavailable',
                error_message='The CM analysis workspace has not been created.',
                descriptor=descriptor,
            )

        # コンテナ・codec・放送種別に依存せず、選択映像だけのMatroskaと
        # chapter_exe用の固定PCM音声へ一度だけ正規化する。映像は全sourceと
        # fallbackが共有し、音声は途中format変更でも時系列順に保持できるWAVに分離する。
        await self._emitStage(request, 'PreparingMedia', 0.10)
        prepared_media_path = request.work_directory / 'prepared-media.cmwork'
        prepared_audio_path = request.work_directory / 'prepared-audio.wav'
        prepare_process = await self._prepareMedia(
            request,
            descriptor,
            prepared_media_path,
            prepared_audio_path,
            self._buildMediaEnvironment(request),
        )
        if (
            prepare_process.return_code != 0
            or prepared_media_path.is_file() is False
            or prepared_audio_path.is_file() is False
        ):
            storage_error = self._temporaryStorageErrorCode(prepare_process.diagnostic)
            decoder_unavailable = self._isDecoderUnavailable(prepare_process.diagnostic)
            return CMAnalyzerResult(
                status='unsupported' if storage_error is None and decoder_unavailable else 'analysis_failed',
                chapter_file=None,
                analyzer_version=self.ANALYZER_VERSION,
                error_code=(
                    storage_error
                    or ('DecoderUnavailable' if decoder_unavailable else 'MediaPreparationFailed')
                ),
                error_message=prepare_process.diagnostic or 'FFmpeg did not produce prepared media.',
                descriptor=descriptor,
                konomitv_bs4k_logical_audio_rebuild_strategy=(
                    prepare_process.konomitv_bs4k_logical_audio_rebuild_strategy
                ),
            )

        await self._emitStage(request, 'IndexingMedia', 0.25)
        index_path = request.work_directory / 'prepared.ffindex'
        audio_index_path = request.work_directory / 'prepared-audio.ffindex'
        index_process, audio_index_process = await asyncio.gather(
            self._indexMedia(
                descriptor.duration_seconds,
                prepared_media_path,
                index_path,
                self._buildEnvironment(request),
            ),
            self._indexMedia(
                descriptor.duration_seconds,
                prepared_audio_path,
                audio_index_path,
                self._buildEnvironment(request),
            ),
        )
        failed_index = next(
            (
                process for process, path in (
                    (index_process, index_path),
                    (audio_index_process, audio_index_path),
                )
                if process.return_code != 0 or path.is_file() is False
            ),
            None,
        )
        if failed_index is not None:
            storage_error = self._temporaryStorageErrorCode(failed_index.diagnostic)
            decoder_unavailable = self._isDecoderUnavailable(failed_index.diagnostic)
            return CMAnalyzerResult(
                status='unsupported' if storage_error is None and decoder_unavailable else 'analysis_failed',
                chapter_file=None,
                analyzer_version=self.ANALYZER_VERSION,
                error_code=storage_error or ('DecoderUnavailable' if decoder_unavailable else 'MediaIndexFailed'),
                error_message=failed_index.diagnostic or 'FFMS2 did not produce an index.',
                descriptor=descriptor,
                konomitv_bs4k_logical_audio_rebuild_strategy=(
                    prepare_process.konomitv_bs4k_logical_audio_rebuild_strategy
                ),
            )
        try:
            prepared_video_index, prepared_audio_index = await asyncio.gather(
                self._resolvePreparedTrack(request, prepared_media_path, 'video'),
                self._resolvePreparedTrack(request, prepared_audio_path, 'audio'),
            )
        except (OSError, ValueError, json.JSONDecodeError) as ex:
            storage_error = self._temporaryStorageExceptionCode(ex) if isinstance(ex, OSError) else None
            return CMAnalyzerResult(
                status='analysis_failed',
                chapter_file=None,
                analyzer_version=self.ANALYZER_VERSION,
                error_code=storage_error or 'PreparedMediaInvalid',
                error_message=str(ex),
                descriptor=descriptor,
                konomitv_bs4k_logical_audio_rebuild_strategy=(
                    prepare_process.konomitv_bs4k_logical_audio_rebuild_strategy
                ),
            )
        prepared = _PreparedMedia(
            media_path=prepared_media_path,
            index_path=index_path,
            video_stream_index=prepared_video_index,
            audio_path=prepared_audio_path,
            audio_index_path=audio_index_path,
            audio_stream_index=prepared_audio_index,
        )

        attempts: list[tuple[CMDecodeMode, str | None]] = []
        if request.hardware_device is not None:
            attempts.append(('Hardware', request.hardware_device))
        attempts.append(('CPU', None))
        hardware_failure: CMAnalyzerResult | None = None
        for decode_mode, hardware_device in attempts:
            attempt_directory = request.work_directory / decode_mode.lower()
            try:
                result = await self._analyzeOnce(
                    request,
                    descriptor,
                    prepared,
                    attempt_directory,
                    decode_mode,
                    hardware_device,
                )
            except OSError as ex:
                result = CMAnalyzerResult(
                    status='analysis_failed',
                    chapter_file=None,
                    analyzer_version=self.ANALYZER_VERSION,
                    error_code=self._temporaryStorageExceptionCode(ex) or 'AnalyzerIOFailed',
                    error_message=str(ex),
                    descriptor=descriptor,
                    decode_mode=decode_mode,
                )
            # media準備で実際に採用した論理音声再構築方式は、chapter/logo解析の
            # 成否にかかわらず呼び出し元へ返す。同一入力の再解析時にnative crash
            # 済みのPyAV経路を避けるため、最終結果へこの実行時情報を引き継ぐ。
            result = replace(
                result,
                konomitv_bs4k_logical_audio_rebuild_strategy=(
                    prepare_process.konomitv_bs4k_logical_audio_rebuild_strategy
                ),
            )
            if decode_mode == 'Hardware' and self._isHardwareDecodeFailure(result):
                hardware_failure = result
                await self._emitStage(request, 'HardwareFallback', None)
                continue
            if hardware_failure is not None and result.status == 'completed':
                return replace(result, warnings=('HardwareDecodeFallback', *result.warnings))
            if (
                decode_mode == 'CPU'
                and result.status == 'analysis_failed'
                and self._isDecoderUnavailable(result.error_message)
            ):
                return replace(result, status='unsupported', error_code='DecoderUnavailable')
            return result

        assert hardware_failure is not None
        return hardware_failure

    async def resolveInputDescriptor(self, request: CMAnalyzerRequest) -> CMInputDescriptor:
        """FFprobe の実データから対象 stream を決定する。"""

        # 現行の LGPL CM runtime は libaribtlv をリンクしないため、同じ入力での再試行を行わない。
        if request.recorded_file_path.suffix.lower() in MMT_TLV_FILE_EXTENSIONS:
            raise CMInputUnsupportedError(
                'ContainerUnsupportedMMTTLV',
                'MMT/TLV recordings are not supported by the CM analysis pipeline.',
            )

        process = await self._runProcess((
            str(self.ffprobe_path),
            '-v', 'error',
            '-show_format',
            '-show_streams',
            '-show_programs',
            '-of', 'json',
            str(request.recorded_file_path),
        ), self._buildMediaEnvironment(request),
            timeout_seconds=self._PROBE_PROCESS_TIMEOUT_SECONDS,
        )
        if process.return_code != 0:
            raise OSError(process.diagnostic or 'FFprobe failed.')
        payload = json.loads(process.output)
        raw_streams = payload.get('streams')
        if not isinstance(raw_streams, list):
            raise ValueError('FFprobe did not return a stream list.')
        streams = [stream for stream in raw_streams if isinstance(stream, dict)]
        format_payload = payload.get('format') if isinstance(payload.get('format'), dict) else {}
        format_name = (
            str(format_payload['format_name'])
            if format_payload.get('format_name') is not None
            else None
        )
        videos = [
            stream for stream in streams
            if stream.get('codec_type') == 'video' and self._isAttachedPicture(stream) is False
        ]
        if not videos:
            raise CMInputUnsupportedError('VideoStreamUnavailable')

        programs = [program for program in payload.get('programs', []) if isinstance(program, dict)]
        matching_program = next(
            (
                program for program in programs
                if request.service_id is not None
                and self._parseInteger(program.get('program_id', program.get('program_num'))) == request.service_id
            ),
            None,
        )
        if (
            self._isMPEGTS(format_name)
            and request.service_id is not None
            and matching_program is None
        ):
            raise CMInputUnsupportedError('ProgramUnavailable')
        selected_program: dict[str, object] | None = None
        if matching_program is not None:
            indexes = self._programStreamIndexes(matching_program)
            program_videos = [stream for stream in videos if self._streamIndex(stream) in indexes]
            if not program_videos:
                raise CMInputUnsupportedError('ProgramVideoStreamUnavailable')
            videos = program_videos
            selected_program = matching_program
        video = max(videos, key=self._videoSelectionKey)
        video_index = self._streamIndex(video)

        if selected_program is None:
            selected_program = next(
                (program for program in programs if video_index in self._programStreamIndexes(program)),
                None,
            )
        audios = [stream for stream in streams if stream.get('codec_type') == 'audio']
        if selected_program is not None:
            indexes = self._programStreamIndexes(selected_program)
            audios = [stream for stream in audios if self._streamIndex(stream) in indexes]
        # 論理音声0はdefault属性や全編durationではなく、選択program/container内の
        # 最初の音声track。TSの後続PMT/PID交代はprepare時のmerge_pmt_versionsで追う。
        audio = min(audios, key=self._streamIndex) if audios else None
        if audio is None:
            raise CMInputUnsupportedError('AudioStreamUnavailable')

        duration = self._parsePositiveFloat(format_payload.get('duration'))
        if duration is None:
            duration = self._parsePositiveFloat(video.get('duration'))
        if duration is None and math.isfinite(request.duration_seconds) and request.duration_seconds > 0:
            duration = request.duration_seconds
        if duration is None:
            raise CMInputUnsupportedError('DurationUnavailable')

        width = self._parseInteger(video.get('width')) or 0
        height = self._parseInteger(video.get('height')) or 0
        if width <= 0 or height <= 0:
            raise CMInputUnsupportedError('VideoGeometryUnavailable')
        pixel_format = str(video['pix_fmt']) if video.get('pix_fmt') is not None else None
        video_duration = self._parsePositiveFloat(video.get('duration'))
        bit_depth = self._parseInteger(video.get('bits_per_raw_sample'))
        if bit_depth is None:
            bit_depth = self._inferBitDepth(pixel_format)
        program_id = (
            self._parseInteger(selected_program.get('program_id', selected_program.get('program_num')))
            if selected_program is not None else None
        )
        return CMInputDescriptor(
            format_name=format_name,
            video_stream_index=video_index,
            audio_stream_index=self._streamIndex(audio) if audio is not None else None,
            video_codec_name=str(video['codec_name']) if video.get('codec_name') is not None else None,
            pixel_format=pixel_format,
            bit_depth=bit_depth,
            width=width,
            height=height,
            field_order=str(video['field_order']) if video.get('field_order') is not None else None,
            time_base=self._parseFraction(video.get('time_base')),
            source_frame_rate=(
                self._parseFraction(video.get('avg_frame_rate'))
                or self._parseFraction(video.get('r_frame_rate'))
            ),
            duration_seconds=duration,
            program_id=program_id,
            service_id=request.service_id,
            has_variable_video_format=request.has_variable_video_format,
            video_start_time_seconds=self._streamStartSeconds(video),
            video_duration_seconds=video_duration,
            audio_start_time_seconds=self._streamStartSeconds(audio),
            audio_stream_id=self._streamID(audio),
        )

    async def _analyzeOnce(
        self,
        request: CMAnalyzerRequest,
        descriptor: CMInputDescriptor,
        prepared: _PreparedMedia,
        work_directory: Path,
        decode_mode: CMDecodeMode,
        hardware_device: str | None,
    ) -> CMAnalyzerResult:
        work_directory.mkdir(parents=True, exist_ok=True)
        environment = self._buildEnvironment(request)
        warnings: list[str] = []
        # chapter_exe と logoframe は別々にデコードするが、完成済みMatroskaと
        # FFMS2索引は共有する。索引生成はattempt外で完了している。
        await self._emitStage(request, 'ChapterAnalyzing', 0.30)
        chapter_script = work_directory / 'chapter.avs'
        chapter_output = work_directory / 'chapter-analysis.txt'
        chapter_script.write_text(
            self._buildAviSynthScript(
                descriptor,
                prepared,
                hardware_device,
                for_chapter=True,
            ),
            encoding='utf-8',
        )
        analysis_frame_rate = self._analysisFrameRate(descriptor)
        chapter_process = await self._runProcess((
            str(self.chapter_executable_path),
            '-v', str(chapter_script),
            '-o', str(chapter_output),
            '-s', (
                '20'
                if math.floor(float(analysis_frame_rate) + 0.5) >= 60
                else '10'
            ),
        ), environment,
            timeout_seconds=self._mediaProcessTimeout(descriptor.duration_seconds),
        )
        # chapter_exe は AviSynth の読み込み失敗時にも 0 を返し、不正な出力を
        # 作ることがある。出力中の明示的な AviSynth エラーも工程失敗として扱う。
        if (
            chapter_process.return_code != 0
            or chapter_output.is_file() is False
            or 'avisynth error:' in chapter_process.diagnostic.lower()
        ):
            return self._failure(
                'ChapterExeFailed', chapter_process, descriptor, decode_mode, warnings,
            )
        try:
            chapter_text = chapter_output.read_text(encoding='utf-8-sig')
            total_frames = self._parseChapterFrameCount(chapter_process, chapter_text)
        except OSError as ex:
            return CMAnalyzerResult(
                status='analysis_failed', chapter_file=None,
                analyzer_version=self.ANALYZER_VERSION,
                error_code=self._temporaryStorageExceptionCode(ex) or 'ChapterOutputInvalid',
                error_message=str(ex),
                warnings=tuple(warnings), descriptor=descriptor, decode_mode=decode_mode,
            )
        except ValueError as ex:
            return CMAnalyzerResult(
                status='analysis_failed', chapter_file=None,
                analyzer_version=self.ANALYZER_VERSION,
                error_code='ChapterOutputInvalid', error_message=str(ex),
                warnings=tuple(warnings), descriptor=descriptor, decode_mode=decode_mode,
            )

        logo_output = _LogoFrameOutput(None, None, None)
        usable_logos = tuple(path for path in request.logo_paths if path.is_file())
        if len(usable_logos) != len(request.logo_paths):
            warnings.append('LogoTemplateMissing')
        if usable_logos:
            await self._emitStage(request, 'LogoAnalyzing', 0.45)
            logo_worker_count = self._logoFrameWorkerCount(total_frames)
            logo_script = work_directory / 'logo.avs'
            logo_script.write_text(
                self._buildAviSynthScript(
                    descriptor,
                    prepared,
                    # 範囲分割 worker ごとに HW decoder context を作ると、単一 GPU 上で
                    # decode が直列化・競合して CPU 並列より大幅に遅くなる。chapter は
                    # HW のまま、logoframe は Amatsukaze と同じ範囲分割を CPU で並列する。
                    None,
                    for_chapter=False,
                    logo_worker_count=logo_worker_count,
                ),
                encoding='utf-8',
            )
            logo_process, logo_output = await self._runLogoFrame(
                logo_script,
                usable_logos,
                work_directory,
                environment,
                logo_worker_count,
                analysis_frame_rate,
                self._mediaProcessTimeout(descriptor.duration_seconds),
            )
            if logo_process.return_code != 0:
                return self._failure(
                    'LogoFrameFailed', logo_process, descriptor, decode_mode, warnings,
                )
            warnings.extend(logo_output.warnings)
            if logo_output.frame_count is not None and logo_output.frame_count != total_frames:
                return CMAnalyzerResult(
                    status='analysis_failed', chapter_file=None,
                    analyzer_version=self.ANALYZER_VERSION,
                    error_code='AnalyzerFrameCountMismatch',
                    error_message=(
                        f'chapter_exe={total_frames}, logoframe={logo_output.frame_count}'
                    ),
                    warnings=tuple(warnings), descriptor=descriptor, decode_mode=decode_mode,
                )
        else:
            warnings.append('LogoUnavailable')

        await self._emitStage(request, 'CombiningCM', 0.90)
        trim_output = work_directory / 'trim.avs'
        detail_output = work_directory / 'jls-detail.txt'
        div_output = work_directory / 'jls-div.txt'
        command: list[str] = [str(self.join_logo_scp_path)]
        if logo_output.path is not None:
            command.extend(('-inlogo', str(logo_output.path)))
        command.extend((
            '-inscp', str(chapter_output),
            '-incmd', str(self.join_logo_scp_command_path),
            '-o', str(trim_output),
            '-oscp', str(detail_output),
            '-odiv', str(div_output),
        ))
        jls_environment = dict(environment)
        jls_environment.update({
            'CLI_IN_PATH': str(request.recorded_file_path),
            'TS_IN_PATH': str(request.recorded_file_path),
            'SERVICE_ID': str(descriptor.program_id or request.service_id or 0),
            'CLI_OUT_PATH': str(work_directory / 'result'),
        })
        jls_process = await self._runProcess(
            tuple(command),
            jls_environment,
            timeout_seconds=self._mediaProcessTimeout(descriptor.duration_seconds),
        )
        if jls_process.return_code != 0 or trim_output.is_file() is False:
            return self._failure(
                'JoinLogoScpFailed', jls_process, descriptor, decode_mode, warnings,
            )
        try:
            trim_text = trim_output.read_text(encoding='utf-8-sig')
            # chapter_exe/JLS は正規化後の総フレーム数を基準にするため、コンテナ末尾の
            # パディングまで含む時刻が KonomiTV-BS4K の録画時間をわずかに超えることがある。
            # 公開するCM区間はプレイヤーとYAMLが共有する録画時間軸へ収める一方、
            # JLS自身のTrim範囲検証は総フレーム数に対して厳格なまま維持する。
            timeline_duration_seconds = (
                request.duration_seconds
                if math.isfinite(request.duration_seconds) and request.duration_seconds > 0
                else descriptor.duration_seconds
            )
            sections = self._parseCMSections(
                trim_text,
                total_frames,
                analysis_frame_rate,
                timeline_duration_seconds=timeline_duration_seconds,
            )
        except OSError as ex:
            return CMAnalyzerResult(
                status='analysis_failed', chapter_file=None,
                analyzer_version=self.ANALYZER_VERSION,
                error_code=self._temporaryStorageExceptionCode(ex) or 'AnalyzerOutputInvalid',
                error_message=str(ex),
                warnings=tuple(warnings), descriptor=descriptor, decode_mode=decode_mode,
                total_frames=total_frames,
            )
        except ValueError as ex:
            return CMAnalyzerResult(
                status='analysis_failed', chapter_file=None,
                analyzer_version=self.ANALYZER_VERSION,
                error_code='AnalyzerOutputInvalid', error_message=str(ex),
                warnings=tuple(warnings), descriptor=descriptor, decode_mode=decode_mode,
                total_frames=total_frames,
            )
        return CMAnalyzerResult(
            status='completed',
            chapter_file=None,
            sections=tuple(sections),
            matched_logo=logo_output.matched_logo,
            analysis_fps=str(analysis_frame_rate),
            library='FFMS2-FFmpeg8-LGPL',
            analyzer_version=self.ANALYZER_VERSION,
            warnings=tuple(warnings),
            descriptor=descriptor,
            decode_mode=decode_mode,
            total_frames=total_frames,
        )

    async def _runLogoFrame(
        self,
        script_path: Path,
        logo_paths: tuple[Path, ...],
        work_directory: Path,
        environment: dict[str, str],
        worker_count: int,
        analysis_frame_rate: Fraction,
        timeout_seconds: float,
    ) -> tuple[_ProcessResult, _LogoFrameOutput]:
        analysis_output = work_directory / 'logoframe-analysis.txt'
        command = [
            str(self.logoframe_path), str(script_path),
            '-oanum', str(len(logo_paths)),
            '-oasel', '1',
            '-oa', str(analysis_output),
            '-parallel', str(worker_count),
            '-dispoff', '1',
            '-paramoff', '1',
        ]
        for index, logo_path in enumerate(logo_paths, start=1):
            command.extend((f'-logo{index}', str(logo_path)))
        process = await self._runProcess(
            tuple(command),
            environment,
            timeout_seconds=timeout_seconds,
        )
        if process.return_code != 0:
            return process, _LogoFrameOutput(None, None, None)
        list_path = analysis_output.with_name(f'{analysis_output.stem}_list.ini')
        if list_path.is_file() is False:
            return process, _LogoFrameOutput(None, None, None, ('LogoNotMatched',))
        try:
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(list_path, encoding='utf-8-sig')
            section = parser['logodata']
            frame_count = int(section['FrameTotal'])
            if int(section.get('LogoTotalN', '0')) <= 0:
                return process, _LogoFrameOutput(None, None, frame_count, ('LogoNotMatched',))
            logo_frame_count = int(section.get('FrameSum_N1', '0'))
            duration_seconds = frame_count / float(analysis_frame_rate)
            minimum_ratio = 0.03 if duration_seconds <= 7 * 60 else 0.10
            if frame_count <= 0 or logo_frame_count / frame_count < minimum_ratio:
                return process, _LogoFrameOutput(None, None, frame_count, ('LogoNotMatched',))
            matched_logo = Path(section['LogoName_N1']).name
            output_name = section.get('oaFileName_N1')
            output_path = Path(output_name) if output_name else None
            if output_path is not None and output_path.is_absolute() is False:
                output_path = work_directory / output_path
            if output_path is None or output_path.is_file() is False or output_path.stat().st_size == 0:
                return process, _LogoFrameOutput(None, None, frame_count, ('LogoMatchAmbiguous',))
            return process, _LogoFrameOutput(output_path, matched_logo, frame_count)
        except (KeyError, OSError, ValueError, configparser.Error) as ex:
            return process, _LogoFrameOutput(None, None, None, (f'LogoResultInvalid:{type(ex).__name__}',))

    @classmethod
    def _logoFrameWorkerCount(cls, total_frames: int) -> int:
        """Amatsukaze と同じ基準で範囲分割ロゴ走査の worker 数を決める。"""

        processor_count = cls._effectiveCPUCount()
        preferred_workers = cls._preferredLogoWorkers(processor_count)
        workers_for_duration = max(
            1,
            (max(0, total_frames) + cls._LOGO_FRAME_MIN_FRAMES_PER_WORKER // 2)
            // cls._LOGO_FRAME_MIN_FRAMES_PER_WORKER,
        )
        return max(1, min(processor_count, preferred_workers, workers_for_duration))

    @classmethod
    def _preferredLogoWorkers(cls, processor_count: int) -> int:
        candidates = range(1, cls._LOGO_FRAME_MAX_WORKERS + 1)
        return min(
            candidates,
            key=lambda workers: (
                processor_count % workers
                + max(0, processor_count // workers - 4),
                abs(workers - 8),
                workers,
            ),
        )

    @staticmethod
    def _effectiveCPUCount() -> int:
        try:
            return max(1, len(os.sched_getaffinity(0)))
        except (AttributeError, OSError):
            return max(1, os.cpu_count() or 1)

    @classmethod
    def _chapterDecoderThreadCount(cls) -> int:
        """全編を1プロセスで走査する chapter_exe 用の decoder thread 数。"""

        return min(16, cls._effectiveCPUCount())

    @classmethod
    def _logoDecoderThreadCount(cls, worker_count: int) -> int:
        """範囲分割された各 logoframe worker 用の decoder thread 数。"""

        processors = cls._effectiveCPUCount()
        return max(1, min(16, processors // max(1, worker_count)))

    def _buildAviSynthScript(
        self,
        descriptor: CMInputDescriptor,
        prepared: _PreparedMedia,
        hardware_device: str | None,
        *,
        for_chapter: bool,
        logo_worker_count: int | None = None,
    ) -> str:
        source = self._escapeAviSynthString(str(prepared.media_path))
        plugin = self._escapeAviSynthString(str(self.ffms2_path))
        cache = self._escapeAviSynthString(str(prepared.index_path))
        hardware = '' if hardware_device is None else (
            f', hwdevice="{self._escapeAviSynthString(hardware_device)}", extrahwframes=8'
        )
        if for_chapter:
            decoder_threads = self._chapterDecoderThreadCount()
        else:
            if logo_worker_count is None:
                raise ValueError('logo_worker_count is required for the logoframe script')
            decoder_threads = self._logoDecoderThreadCount(logo_worker_count)
        # logoframe には FFMS2 の native planar luma をそのまま渡す。
        # colorspace=Y* を強制すると swscale が limited/full range を誤変換し得るほか、
        # Y14 は AviSynth+ の明示的な出力色空間として扱えない。
        analysis_frame_rate = self._analysisFrameRate(descriptor)
        video_common = (
            f'fpsnum={analysis_frame_rate.numerator}, fpsden={analysis_frame_rate.denominator}, '
            f'cache=true, cachefile="{cache}", threads={decoder_threads}'
            f'{hardware}'
        )
        lines = [
            'ClearAutoloadDirs()',
            f'LoadPlugin("{plugin}")',
            (
                f'video = FFVideoSource("{source}", track={prepared.video_stream_index}, '
                f'{video_common})'
            ),
        ]
        if for_chapter:
            audio_source = self._escapeAviSynthString(str(prepared.audio_path))
            audio_cache = self._escapeAviSynthString(str(prepared.audio_index_path))
            lines.append(
                f'audio = FFAudioSource("{audio_source}", track={prepared.audio_stream_index}, '
                f'cache=true, cachefile="{audio_cache}", adjustdelay=-3, fill_gaps=1)'
            )
            lines.append('clip = AudioDubEx(video, audio)')
        else:
            lines.append('clip = video')
        if descriptor.field_order in ('tt', 'tb', 'tff'):
            lines.append('clip = AssumeTFF(clip)')
        elif descriptor.field_order in ('bb', 'bt', 'bff'):
            lines.append('clip = AssumeBFF(clip)')
        if for_chapter:
            lines.extend((
                'clip = (Width(clip) % 2 == 0 && Height(clip) % 2 == 0) ? clip : '
                'AddBorders(clip, 0, 0, Width(clip) % 2, Height(clip) % 2)',
                'clip = ConvertBits(clip, 8)',
                'clip = ConvertToYV12(clip)',
            ))
            target_width, target_height = self._chapterDimensions(descriptor.width, descriptor.height)
            if target_width != descriptor.width or target_height != descriptor.height:
                lines.append(f'clip = Spline36Resize(clip, {target_width}, {target_height})')
        lines.extend(('clip = Prefetch(clip, 1)', 'return clip'))
        return '\n'.join(lines) + '\n'

    async def _prepareMedia(
        self,
        request: CMAnalyzerRequest,
        descriptor: CMInputDescriptor,
        video_output_path: Path,
        audio_output_path: Path,
        environment: Mapping[str, str],
    ) -> _ProcessResult:
        """論理音声0をPTS再構築し、選択映像と別々の共有媒体へ正規化する。"""

        if descriptor.audio_stream_index is None:
            return _ProcessResult(-1, '', 'The selected audio stream is missing.')
        video_partial_path = video_output_path.with_name(f'{video_output_path.name}.partial')
        raw_audio_partial_path = audio_output_path.with_name(
            f'{audio_output_path.stem}.normalized.partial.wav',
        )
        temporary_paths = (
            video_partial_path,
            raw_audio_partial_path,
        )
        published_paths: list[Path] = []
        logical_audio_strategy: KonomiTVBS4KLogicalAudioRebuildStrategy = 'PyAV'
        try:
            if request.konomitv_bs4k_logical_audio_rebuild_preference == 'FFmpegAfterPreviousFailure':
                # 同一入力で過去にMediaPreparationFailedまたはsignal fallbackを確認済みなら、
                # 捕捉不能なPyAV native crashを再現させず、既に成功実績のある同一stream固定
                # FFmpeg経路を主経路として使う。初回入力では従来どおりStreamReform準拠
                # assemblerを優先するため、すべてのMPEG-TSを一律に切り替えることはない。
                logical_audio_strategy = 'FFmpegPrimaryAfterPreviousFailure'
                logging.warning(
                    '[CMAnalyzer] AudioRebuildPrimary=FFmpeg; '
                    'Reason=PreviousMediaPreparationFailure; '
                    f'StreamMap=0:{descriptor.audio_stream_index}; '
                    f'StreamID={descriptor.audio_stream_id}'
                )
                audio_process = await self._rebuildLogicalAudioWithFFmpeg(
                    request,
                    descriptor,
                    raw_audio_partial_path,
                    environment,
                )
                audio_validation_error = self._validatePreparedWAV(raw_audio_partial_path)
                if audio_process.return_code != 0 or audio_validation_error is not None:
                    audio_diagnostic = (
                        audio_process.diagnostic
                        if audio_process.return_code != 0
                        else f'InvalidWAV: {audio_validation_error}'
                    )
                    self._removeFiles(temporary_paths)
                    return _ProcessResult(
                        audio_process.return_code or -1,
                        '',
                        (
                            'AudioRebuildPrimary=FFmpeg failed after a previous media preparation '
                            f'failure: {audio_diagnostic or "FFmpeg did not produce logical audio."}'
                        ),
                        logical_audio_strategy,
                    )
                logging.warning('[CMAnalyzer] AudioRebuildPrimary=FFmpeg completed.')
            else:
                audio_process = await self._rebuildLogicalAudio(
                    request,
                    descriptor,
                    raw_audio_partial_path,
                    environment,
                )
                audio_report = self._parseLogicalAudioRebuildReport(audio_process.output)
                audio_validation_error = self._validatePreparedWAV(raw_audio_partial_path)
                if audio_process.return_code != 0 or audio_validation_error is not None:
                    primary_diagnostic = self._logicalAudioFailureDiagnostic(
                        audio_process,
                        audio_report,
                        audio_validation_error,
                    )
                    if self._shouldFallbackLogicalAudio(
                        audio_process,
                        audio_report,
                        audio_validation_error,
                    ):
                        # PyAV固有のSIGSEGV・decoder拒否・WAV書き込み失敗時だけ、
                        # Amatsukazeと異なるFFmpegのasync resample/無音補完経路を使う。
                        # mapはprobeで確定した同じstream indexに固定し、別audioIdxや
                        # 最長trackへ切り替えて成功扱いすることはない。
                        logical_audio_strategy = (
                            'FFmpegFallbackAfterSignal'
                            if audio_process.return_code < 0
                            else 'FFmpegFallbackAfterReportedFailure'
                        )
                        self._removeFiles((raw_audio_partial_path,))
                        logging.warning(
                            '[CMAnalyzer] AudioRebuildFallback=FFmpeg; '
                            f'StreamMap=0:{descriptor.audio_stream_index}; '
                            f'StreamID={descriptor.audio_stream_id}; '
                            f'PrimaryFailure={primary_diagnostic}'
                        )
                        fallback_process = await self._rebuildLogicalAudioWithFFmpeg(
                            request,
                            descriptor,
                            raw_audio_partial_path,
                            environment,
                        )
                        fallback_validation_error = self._validatePreparedWAV(raw_audio_partial_path)
                        if fallback_process.return_code != 0 or fallback_validation_error is not None:
                            fallback_diagnostic = (
                                fallback_process.diagnostic
                                if fallback_process.return_code != 0
                                else f'InvalidWAV: {fallback_validation_error}'
                            )
                            self._removeFiles(temporary_paths)
                            return _ProcessResult(
                                fallback_process.return_code or audio_process.return_code or -1,
                                '',
                                (
                                    f'{primary_diagnostic}; AudioRebuildFallback=FFmpeg failed: '
                                    f'{fallback_diagnostic or "FFmpeg did not produce logical audio."}'
                                ),
                                logical_audio_strategy,
                            )
                        logging.warning('[CMAnalyzer] AudioRebuildFallback=FFmpeg completed.')
                    else:
                        self._removeFiles(temporary_paths)
                        return _ProcessResult(
                            audio_process.return_code or -1,
                            '',
                            primary_diagnostic,
                            logical_audio_strategy,
                        )

            input_options = (
                ('-merge_pmt_versions', '1')
                if self._isMPEGTS(descriptor.format_name)
                else ()
            )
            process = await self._runProcess((
                str(self.ffmpeg_path),
                '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                '-fflags', '+genpts+discardcorrupt',
                *input_options,
                '-i', str(request.recorded_file_path),
                # video-only Matroska: chapterとlogoがこの同じ媒体・indexを読む。
                '-map', f'0:{descriptor.video_stream_index}',
                '-an', '-sn', '-dn',
                '-c:v', 'copy',
                '-map_metadata', '-1', '-map_chapters', '-1',
                '-f', 'matroska',
                str(video_partial_path),
            ), environment,
                timeout_seconds=self._mediaProcessTimeout(descriptor.duration_seconds),
            )
            if process.return_code != 0:
                self._removeFiles(temporary_paths)
                return replace(
                    process,
                    konomitv_bs4k_logical_audio_rebuild_strategy=logical_audio_strategy,
                )
            if video_partial_path.is_file() is False or raw_audio_partial_path.is_file() is False:
                self._removeFiles(temporary_paths)
                return _ProcessResult(
                    -1,
                    process.output,
                    'FFmpeg did not produce both prepared video and audio.',
                    logical_audio_strategy,
                )

            # work directoryはjob専用でconsumerはこの関数の完了後にだけ起動する。
            # 両方が完成してから公開し、片方のrename失敗時は公開済み側も除去する。
            os.replace(video_partial_path, video_output_path)
            published_paths.append(video_output_path)
            os.replace(raw_audio_partial_path, audio_output_path)
            published_paths.append(audio_output_path)
            self._removeFiles(temporary_paths)
            return replace(
                process,
                konomitv_bs4k_logical_audio_rebuild_strategy=logical_audio_strategy,
            )
        except asyncio.CancelledError:
            self._removeFiles((*temporary_paths, *published_paths))
            raise
        except OSError as ex:
            self._removeFiles((*temporary_paths, *published_paths))
            return _ProcessResult(-1, '', str(ex), logical_audio_strategy)

    async def _rebuildLogicalAudio(
        self,
        request: CMAnalyzerRequest,
        descriptor: CMInputDescriptor,
        output_path: Path,
        environment: Mapping[str, str],
    ) -> _ProcessResult:
        """隔離subprocessで論理音声0のPTS付きPCMを構築する。"""

        if descriptor.audio_stream_index is None:
            return _ProcessResult(-1, '', 'The selected audio stream is missing.')
        command = [
            sys.executable,
            '-m', 'app.metadata.CMLogicalAudioRebuilder',
            '--input', str(request.recorded_file_path),
            '--output', str(output_path),
            '--stream-index', str(descriptor.audio_stream_index),
            '--video-start-time', str(descriptor.video_start_time_seconds or 0.0),
            '--video-duration', str(
                descriptor.video_duration_seconds or descriptor.duration_seconds
            ),
        ]
        if descriptor.format_name is not None:
            command.extend(('--format-name', descriptor.format_name))
        if descriptor.audio_stream_id is not None:
            command.extend(('--stream-id', str(descriptor.audio_stream_id)))
        process = await self._runProcess(
            tuple(command),
            environment,
            timeout_seconds=self._mediaProcessTimeout(descriptor.duration_seconds),
        )
        report = self._parseLogicalAudioRebuildReport(process.output)
        if report is not None:
            residual_statistics = {
                name: value
                for name, value in report.statistics.items()
                if name in ('decode_errors', 'demux_errors', 'skipped_frames')
                and value > 0
            }
            if residual_statistics:
                logging.warning(
                    '[CMAnalyzer] Logical audio rebuild residual statistics: '
                    f'{json.dumps(residual_statistics, separators=(",", ":"), sort_keys=True)}'
                )
        if process.return_code < 0:
            signal_number = -process.return_code
            signal_diagnostic = f'Logical audio rebuild killed by signal {signal_number}.'
            if process.error_output:
                signal_diagnostic = f'{signal_diagnostic} {process.error_output}'
            return _ProcessResult(process.return_code, process.output, signal_diagnostic)
        if process.return_code != 0 and report is not None:
            report_error = report.error or 'No detailed error was reported.'
            return _ProcessResult(
                process.return_code,
                process.output,
                (
                    f'Logical audio rebuild failed at {report.stage} '
                    f'(exit code {process.return_code}): {report_error}'
                ),
            )
        return process

    async def _rebuildLogicalAudioWithFFmpeg(
        self,
        request: CMAnalyzerRequest,
        descriptor: CMInputDescriptor,
        output_path: Path,
        environment: Mapping[str, str],
    ) -> _ProcessResult:
        """同じ論理streamだけをFFmpeg CLIで固定長PCM WAVへ変換する。

        Args:
            request: 録画入力とjob作業領域。
            descriptor: probeで確定済みの論理音声0と映像時間軸。
            output_path: 未公開のWAV出力先。
            environment: media処理subprocess用環境変数。

        Returns:
            FFmpeg CLIの終了状態と診断。
        """

        if descriptor.audio_stream_index is None:
            return _ProcessResult(-1, '', 'The selected audio stream is missing.')
        video_start_time = descriptor.video_start_time_seconds or 0.0
        video_duration = descriptor.video_duration_seconds or descriptor.duration_seconds
        start_sample = round(video_start_time * 48_000)
        duration_samples = max(1, math.ceil(video_duration * 48_000))
        end_sample = start_sample + duration_samples
        input_options = (
            ('-merge_pmt_versions', '1')
            if self._isMPEGTS(descriptor.format_name)
            else ()
        )
        # 前半のaresample asyncは破損packetによるtimestamp gapをPCM時間軸へ反映する。
        # 後半のapad/atrimはStreamReformにはない、fallback限定の固定長WAV契約。
        audio_filter = (
            f'aresample=48000:async=1:first_pts={start_sample},'
            f'atrim=start_pts={start_sample}:end_pts={end_sample},'
            'asetpts=PTS-STARTPTS,'
            'aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=stereo,'
            f'apad=whole_len={duration_samples},'
            f'atrim=end_sample={duration_samples}'
        )
        return await self._runProcess((
            str(self.ffmpeg_path),
            '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-fflags', '+genpts+discardcorrupt',
            '-err_detect', 'ignore_err',
            *input_options,
            '-i', str(request.recorded_file_path),
            '-map', f'0:{descriptor.audio_stream_index}',
            '-vn', '-sn', '-dn',
            '-af', audio_filter,
            '-ar', '48000', '-ac', '2',
            '-c:a', 'pcm_s16le',
            '-map_metadata', '-1', '-map_chapters', '-1',
            '-rf64', 'auto',
            '-f', 'wav',
            str(output_path),
        ), environment,
            timeout_seconds=self._mediaProcessTimeout(descriptor.duration_seconds),
        )

    @staticmethod
    def _parseLogicalAudioRebuildReport(output: str) -> _LogicalAudioRebuildReport | None:
        """rebuilder stdoutの1行JSONを検証して内部表現へ変換する。

        Args:
            output: rebuilderのstdout。

        Returns:
            stage・統計・errorが揃ったreport。非JSONや旧形式ならNone。
        """

        try:
            payload = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        stage = payload.get('stage')
        raw_statistics = payload.get('statistics')
        error = payload.get('error')
        if not isinstance(stage, str) or not isinstance(raw_statistics, dict):
            return None
        if error is not None and not isinstance(error, str):
            return None
        statistics = {
            str(name): value
            for name, value in raw_statistics.items()
            if isinstance(name, str) and isinstance(value, int) and isinstance(value, bool) is False
        }
        return _LogicalAudioRebuildReport(
            stage=stage,
            statistics=statistics,
            error=error,
        )

    @staticmethod
    def _logicalAudioFailureDiagnostic(
        process: _ProcessResult,
        report: _LogicalAudioRebuildReport | None,
        validation_error: str | None,
    ) -> str:
        """親だけが分かるsignal・終了段階・WAV不正を一つの英語診断にする。

        Args:
            process: rebuilder subprocessの終了状態。
            report: parseできた子の段階付きreport。
            validation_error: WAV検証エラー。正常時はNone。

        Returns:
            MediaPreparationFailedへ載せる英語診断。
        """

        if process.return_code < 0:
            return process.error_output or f'Logical audio rebuild killed by signal {-process.return_code}.'
        if process.return_code != 0 and report is not None:
            return (
                f'Logical audio rebuild failed at {report.stage} '
                f'(exit code {process.return_code}): '
                f'{report.error or "No detailed error was reported."}'
            )
        if process.return_code != 0:
            return process.error_output or f'Logical audio rebuild exited with code {process.return_code}.'
        return f'Logical audio rebuild produced an invalid WAV: {validation_error or "unknown error"}'

    @staticmethod
    def _shouldFallbackLogicalAudio(
        process: _ProcessResult,
        report: _LogicalAudioRebuildReport | None,
        validation_error: str | None,
    ) -> bool:
        """設計で許可したdecode系失敗だけをFFmpeg fallback対象にする。

        Args:
            process: rebuilder subprocessの終了状態。
            report: parseできた子の段階付きreport。
            validation_error: WAV検証エラー。正常時はNone。

        Returns:
            同じstream indexをFFmpeg CLIで再構築してよい場合はTrue。
        """

        if process.return_code < 0 or process.return_code == 2:
            return True
        if report is not None and report.stage in ('Demux', 'Decode', 'Assemble', 'Write'):
            return True
        # 正常終了したのに空・破損WAVだった場合は子のWrite失敗相当として扱う。
        return process.return_code == 0 and validation_error is not None

    @staticmethod
    def _validatePreparedWAV(path: Path) -> str | None:
        """WAV containerとchapter_exe向けPCM形式・非空dataを検証する。

        Args:
            path: rebuilderまたはfallbackが生成した未公開WAV。

        Returns:
            正常時はNone。不正時は原因層を示す英語診断。
        """

        if path.is_file() is False:
            return 'WAV file was not produced.'
        try:
            file_size = path.stat().st_size
            if file_size < 44:
                return 'WAV file is missing or shorter than its header.'
            with path.open('rb') as wav_file:
                header = wav_file.read(12)
                if len(header) != 12 or header[:4] not in (b'RIFF', b'RF64') or header[8:] != b'WAVE':
                    return 'WAV container header is invalid.'
                is_rf64 = header[:4] == b'RF64'
                rf64_data_size: int | None = None
                format_fields: tuple[int, int, int, int] | None = None
                data_size: int | None = None
                data_offset: int | None = None
                while wav_file.tell() + 8 <= file_size:
                    chunk_header = wav_file.read(8)
                    if len(chunk_header) != 8:
                        break
                    chunk_id = chunk_header[:4]
                    chunk_size = struct.unpack('<I', chunk_header[4:])[0]
                    chunk_data_offset = wav_file.tell()
                    if chunk_id == b'ds64' and chunk_size >= 28:
                        ds64 = wav_file.read(28)
                        rf64_data_size = struct.unpack('<QQQI', ds64)[1]
                    elif chunk_id == b'fmt ' and chunk_size >= 16:
                        raw_format = wav_file.read(16)
                        format_tag, channels, sample_rate, _, block_align, bits_per_sample = struct.unpack(
                            '<HHIIHH',
                            raw_format,
                        )
                        format_fields = (format_tag, channels, sample_rate, bits_per_sample)
                        if block_align != 4:
                            return f'WAV block alignment is {block_align}, expected 4.'
                    elif chunk_id == b'data':
                        data_offset = chunk_data_offset
                        data_size = (
                            rf64_data_size
                            if is_rf64 and chunk_size == 0xFFFFFFFF
                            else chunk_size
                        )
                        break
                    next_chunk_offset = chunk_data_offset + chunk_size + (chunk_size % 2)
                    if next_chunk_offset > file_size:
                        return f'WAV chunk {chunk_id!r} exceeds the file size.'
                    wav_file.seek(next_chunk_offset)
        except OSError as ex:
            return f'{type(ex).__name__}: {ex}'

        if format_fields is None:
            return 'WAV fmt chunk is missing.'
        if format_fields != (1, 2, 48_000, 16):
            return (
                'WAV format is invalid: '
                f'format={format_fields[0]}, channels={format_fields[1]}, '
                f'sample_rate={format_fields[2]}, bits={format_fields[3]}.'
            )
        if data_size is None or data_offset is None or data_size <= 0:
            return 'WAV data chunk is empty or missing.'
        if data_size % 4 != 0:
            return f'WAV data size {data_size} is not aligned to stereo s16le samples.'
        if data_offset + data_size > file_size:
            return 'WAV data chunk exceeds the file size.'
        return None

    @staticmethod
    def _removeFiles(paths: tuple[Path, ...]) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    async def _indexMedia(
        self,
        duration_seconds: float,
        media_path: Path,
        index_path: Path,
        environment: Mapping[str, str],
    ) -> _ProcessResult:
        """全trackを一度だけindexし、完成後に原子的に公開する。"""

        partial_path = index_path.with_name(f'{index_path.name}.partial')
        process = await self._runProcess((
            str(self.ffmsindex_path),
            '-f', '-t', '-1',
            str(media_path), str(partial_path),
        ), environment,
            timeout_seconds=self._mediaProcessTimeout(duration_seconds),
        )
        if process.return_code == 0 and partial_path.is_file():
            try:
                os.replace(partial_path, index_path)
            except OSError as ex:
                return _ProcessResult(-1, process.output, str(ex))
        return process

    async def _resolvePreparedTrack(
        self,
        request: CMAnalyzerRequest,
        media_path: Path,
        track_type: Literal['video', 'audio'],
    ) -> int:
        process = await self._runProcess((
            str(self.ffprobe_path),
            '-v', 'error', '-show_streams', '-of', 'json', str(media_path),
        ), self._buildMediaEnvironment(request),
            timeout_seconds=self._PROBE_PROCESS_TIMEOUT_SECONDS,
        )
        if process.return_code != 0:
            raise OSError(process.diagnostic or 'Prepared media FFprobe failed.')
        payload = json.loads(process.output)
        streams = payload.get('streams')
        if not isinstance(streams, list):
            raise ValueError('Prepared media has no stream list.')
        videos = [stream for stream in streams if isinstance(stream, dict) and stream.get('codec_type') == 'video']
        audios = [stream for stream in streams if isinstance(stream, dict) and stream.get('codec_type') == 'audio']
        if track_type == 'video' and (len(videos) != 1 or audios):
            raise ValueError('Prepared video media must contain exactly one video and no audio tracks.')
        if track_type == 'audio' and (len(audios) != 1 or videos):
            raise ValueError('Prepared audio media must contain exactly one audio and no video tracks.')
        return self._streamIndex(videos[0] if track_type == 'video' else audios[0])

    @staticmethod
    async def _emitStage(
        request: CMAnalyzerRequest,
        stage: CMAnalysisStage,
        progress: float | None,
    ) -> None:
        if request.stage_callback is not None:
            await request.stage_callback(stage, progress)

    @classmethod
    def _parseCMSections(
        cls,
        trim_text: str,
        total_frames: int,
        fps: Fraction,
        *,
        timeline_duration_seconds: float | None = None,
    ) -> list[CMSectionJSON]:
        keep_ranges = [
            (int(match.group(1)), int(match.group(2)) + 1)
            for match in cls._TRIM_PATTERN.finditer(trim_text)
        ]
        if not keep_ranges:
            raise ValueError('JLS did not emit a determinate Trim range.')
        if total_frames <= 0:
            raise ValueError('Analysis frame count must be positive.')
        if fps <= 0:
            raise ValueError('Analysis frame rate must be positive.')
        analysis_duration_seconds = total_frames / float(fps)
        timeline_end_seconds = analysis_duration_seconds
        timeline_is_clipped = False
        if timeline_duration_seconds is not None:
            if math.isfinite(timeline_duration_seconds) is False or timeline_duration_seconds <= 0:
                raise ValueError('Timeline duration must be a finite positive number.')
            # DB/プレイヤーの録画時間が解析clipより短い場合だけ末尾を切り詰める。
            # 逆に長い場合は、解析できていない範囲をCMとして捏造しない。
            timeline_end_seconds = min(timeline_duration_seconds, analysis_duration_seconds)
            timeline_is_clipped = timeline_duration_seconds < analysis_duration_seconds
        previous_end = 0
        for start, end in keep_ranges:
            if start < 0 or end <= start or end > total_frames:
                raise ValueError(f'Invalid JLS Trim range: {start}-{end - 1}')
            if start < previous_end:
                raise ValueError('JLS Trim ranges overlap or are out of order.')
            previous_end = end
        cursor = 0
        sections: list[CMSectionJSON] = []

        def AppendCMSection(start_frame: int, end_frame: int) -> None:
            """フレーム区間を公開時間軸内のCM区間として追加する。

            Args:
                start_frame: CM区間の開始フレーム。inclusive。
                end_frame: CM区間の終了フレーム。exclusive。

            Returns:
                None
            """

            start_seconds = round(start_frame / float(fps), 6)
            raw_end_seconds = end_frame / float(fps)
            if start_seconds >= timeline_end_seconds:
                return
            # 上限へ到達した区間では丸めによる再超過を避け、録画時間そのものを使う。
            end_seconds = (
                timeline_end_seconds
                if timeline_is_clipped and raw_end_seconds >= timeline_end_seconds
                else round(raw_end_seconds, 6)
            )
            if end_seconds <= start_seconds:
                return
            sections.append(CMSectionJSON(start_time=start_seconds, end_time=end_seconds))

        for start, end in keep_ranges:
            if cursor < start:
                AppendCMSection(cursor, start)
            cursor = end
        if cursor < total_frames:
            AppendCMSection(cursor, total_frames)
        return sections

    async def _runProcess(
        self,
        command: tuple[str, ...],
        environment: Mapping[str, str],
        *,
        timeout_seconds: float,
    ) -> _ProcessResult:
        """有限のnative処理を絶対上限内で実行し、終了時にpipeとprocess groupを回収する。

        Args:
            command: 実行ファイルと引数。
            environment: 子プロセスへ渡す環境変数。
            timeout_seconds: 起動後の絶対タイムアウト秒数。

        Returns:
            終了コードと標準出力・標準エラー。タイムアウト時は負の終了コードを返す。
        """

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(environment),
                start_new_session=True,
            )
        except OSError as ex:
            return _ProcessResult(-1, '', str(ex))
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            await self._terminateProcessGroup(process)
            return _ProcessResult(
                -1,
                '',
                (
                    f'ProcessTimeout: {Path(command[0]).name} exceeded '
                    f'{timeout_seconds:.0f} seconds.'
                ),
            )
        except asyncio.CancelledError:
            await self._terminateProcessGroup(process)
            raise
        return _ProcessResult(
            process.returncode or 0,
            stdout.decode('utf-8', errors='replace').strip(),
            stderr.decode('utf-8', errors='replace').strip(),
        )

    @classmethod
    def _mediaProcessTimeout(cls, duration_seconds: float) -> float:
        """録画尺に比例しつつ有限な全編native処理の絶対上限を返す。

        Args:
            duration_seconds: 解析対象録画の秒数。

        Returns:
            低速なCPU fallbackを許容する30分から24時間までのタイムアウト秒数。
        """

        scaled_timeout = (
            duration_seconds * cls._MEDIA_PROCESS_DURATION_MULTIPLIER
            if math.isfinite(duration_seconds) and duration_seconds > 0
            else cls._MEDIA_PROCESS_MIN_TIMEOUT_SECONDS
        )
        return min(
            cls._MEDIA_PROCESS_MAX_TIMEOUT_SECONDS,
            max(cls._MEDIA_PROCESS_MIN_TIMEOUT_SECONDS, scaled_timeout),
        )

    @classmethod
    async def _terminateProcessGroup(cls, process: asyncio.subprocess.Process) -> None:
        """native process groupを段階的に停止し、pipeと終了状態を回収する。

        Args:
            process: start_new_session=True で起動した子プロセス。

        Returns:
            None
        """

        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            # communicate() を再実行し、終了待機だけでなく未読の stdout / stderr も drain する。
            await asyncio.wait_for(
                process.communicate(),
                timeout=cls._PROCESS_TERMINATE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await process.communicate()

    def _buildEnvironment(self, request: CMAnalyzerRequest) -> dict[str, str]:
        """AviSynth/FFMS2用private libraryを優先するnative解析環境を返す。"""

        environment = self._buildMediaEnvironment(request)
        existing = environment.get('LD_LIBRARY_PATH')
        paths = [str(self.runtime_directory)]
        if existing:
            paths.append(existing)
        environment['LD_LIBRARY_PATH'] = ':'.join(paths)
        return environment

    @staticmethod
    def _buildMediaEnvironment(request: CMAnalyzerRequest) -> dict[str, str]:
        """playback FFmpeg8/FFprobe8が自身のRUNPATHを使う媒体処理環境を返す。"""

        environment = dict(os.environ)
        environment['LC_ALL'] = 'C.UTF-8'
        if request.hardware_environment is not None:
            environment.update(request.hardware_environment)
        return environment

    def _runtimeManifestFingerprint(self) -> tuple[str | None, str | None]:
        """固定manifestを検証し、attempt keyへ入れる内容SHA-256を返す。"""

        try:
            content = self.runtime_manifest_path.read_bytes()
        except OSError as ex:
            return None, f'{type(ex).__name__}: {ex}'
        digest = hashlib.sha256(content).hexdigest()
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as ex:
            return digest, f'{type(ex).__name__}: {ex}'
        if (
            not isinstance(payload, dict)
            or payload.get('schema_version') != 1
            or not isinstance(payload.get('components'), dict)
            or not isinstance(payload.get('patches'), dict)
        ):
            return digest, 'The CM runtime manifest schema is invalid.'
        return digest, None

    @staticmethod
    def _fileFingerprint(path: Path) -> tuple[str | None, str | None]:
        """外部実行ファイルの内容をattempt keyへ含める。"""

        try:
            return hashlib.sha256(path.read_bytes()).hexdigest(), None
        except OSError as ex:
            return None, f'{type(ex).__name__}: {ex}'

    @classmethod
    def _parseChapterFrameCount(cls, process: _ProcessResult, chapter_text: str) -> int:
        match = cls._VIDEO_FRAME_COUNT_PATTERN.search(f'{process.output}\n{process.error_output}')
        if match is None:
            raise ValueError('chapter_exe did not report its analysis frame count.')
        total_frames = int(match.group(1))
        if total_frames <= 0:
            raise ValueError('chapter_exe reported an invalid frame count.')
        final_scene = list(cls._FINAL_SCENE_PATTERN.finditer(chapter_text))
        if not final_scene:
            raise ValueError('chapter_exe output has no final SCPos marker.')
        if int(final_scene[-1].group(1)) != total_frames - 1:
            raise ValueError('chapter_exe frame count and final SCPos disagree.')
        return total_frames

    @staticmethod
    def _streamIndex(stream: dict[str, object] | None) -> int:
        if stream is None:
            raise ValueError('Stream is missing.')
        value = GenericCMAnalyzer._parseInteger(stream.get('index'))
        if value is None or value < 0:
            raise ValueError('Stream index is missing or invalid.')
        return value

    @staticmethod
    def _programStreamIndexes(program: dict[str, object]) -> set[int]:
        result: set[int] = set()
        streams = program.get('streams')
        if isinstance(streams, list):
            for stream in streams:
                if not isinstance(stream, dict):
                    continue
                index = GenericCMAnalyzer._parseInteger(stream.get('index'))
                if index is not None:
                    result.add(index)
        return result

    @classmethod
    def _videoSelectionKey(cls, stream: dict[str, object]) -> tuple[int, float, int, int]:
        width = cls._parseInteger(stream.get('width')) or 0
        height = cls._parseInteger(stream.get('height')) or 0
        return (
            cls._dispositionValue(stream, 'default'),
            cls._parsePositiveFloat(stream.get('duration')) or 0.0,
            width * height,
            -cls._streamIndex(stream),
        )

    @staticmethod
    def _dispositionValue(stream: dict[str, object], key: str) -> int:
        disposition = stream.get('disposition')
        if isinstance(disposition, dict):
            return 1 if disposition.get(key) in (1, '1', True) else 0
        return 0

    @classmethod
    def _isAttachedPicture(cls, stream: dict[str, object]) -> bool:
        return cls._dispositionValue(stream, 'attached_pic') == 1

    @staticmethod
    def _parseInteger(value: object) -> int | None:
        try:
            return int(cast(str | int, value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parsePositiveFloat(value: object) -> float | None:
        try:
            parsed = float(cast(str | int | float, value))
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed > 0 else None

    @staticmethod
    def _parseFraction(value: object) -> Fraction | None:
        if not isinstance(value, str) or value in ('', '0/0', 'N/A'):
            return None
        try:
            parsed = Fraction(value)
        except (ValueError, ZeroDivisionError):
            return None
        return parsed if parsed > 0 else None

    @classmethod
    def _streamStartSeconds(cls, stream: dict[str, object] | None) -> float | None:
        if stream is None:
            return None
        start_time = cls._parseFiniteFloat(stream.get('start_time'))
        if start_time is not None:
            return start_time
        start_pts = cls._parseInteger(stream.get('start_pts'))
        time_base = cls._parseFraction(stream.get('time_base'))
        if start_pts is None or time_base is None:
            return None
        return float(start_pts * time_base)

    @classmethod
    def _streamID(cls, stream: dict[str, object]) -> int | None:
        """FFprobeのdecimal/hex stream idを整数へ正規化する。"""

        value = stream.get('id')
        try:
            if isinstance(value, str):
                return int(value, 0)
            if isinstance(value, int):
                return value
        except ValueError:
            return None
        return None

    @staticmethod
    def _isMPEGTS(format_name: str | None) -> bool:
        """拡張子ではなくdemuxerのformat名でMPEG-TSを判定する。"""

        return format_name is not None and 'mpegts' in {
            item.strip().lower()
            for item in format_name.split(',')
        }

    @staticmethod
    def _parseFiniteFloat(value: object) -> float | None:
        try:
            parsed = float(cast(str | int | float, value))
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    @staticmethod
    def _inferBitDepth(pixel_format: str | None) -> int | None:
        if pixel_format is None:
            return None
        match = re.search(r'(9|10|12|14|16)(?:le|be)?$', pixel_format)
        return int(match.group(1)) if match is not None else 8

    @staticmethod
    def _chapterDimensions(width: int, height: int) -> tuple[int, int]:
        if width <= 1920 and height <= 1080 and width % 16 == 0 and height % 2 == 0:
            return width, height
        scale = min(1.0, 1920 / width, 1080 / height)
        target_width = max(16, math.floor(width * scale / 16) * 16)
        target_height = max(2, math.floor(height * scale / 2) * 2)
        return target_width, target_height

    @classmethod
    def _analysisFrameRate(cls, descriptor: CMInputDescriptor) -> Fraction:
        """Amatsukaze同様にsourceのCFRを保ち、不明時だけ従来値へ戻す。"""

        source_frame_rate = descriptor.source_frame_rate
        if source_frame_rate is None or source_frame_rate <= 0 or source_frame_rate > 120:
            return cls.ANALYSIS_FPS
        return source_frame_rate

    @classmethod
    def _isHardwareDecodeFailure(cls, result: CMAnalyzerResult) -> bool:
        if result.status != 'analysis_failed':
            return False
        text = ' '.join(filter(None, (result.error_code, result.error_message))).lower()
        # hardware試行中の固着もdecoderが応答しない失敗として、既存のCPU一回fallbackへ戻す。
        return cls._HARDWARE_FAILURE_MARKER in text or 'processtimeout:' in text

    @classmethod
    def _isDecoderUnavailable(cls, message: str | None) -> bool:
        text = (message or '').lower()
        return any(marker in text for marker in cls._DECODER_UNAVAILABLE_MARKERS)

    @classmethod
    def _temporaryStorageErrorCode(cls, message: str | None) -> str | None:
        text = (message or '').lower()
        if any(marker in text for marker in cls._STORAGE_INSUFFICIENT_MARKERS):
            return 'TemporaryStorageInsufficient'
        if any(marker in text for marker in cls._STORAGE_UNAVAILABLE_MARKERS):
            return 'TemporaryStorageUnavailable'
        return None

    @classmethod
    def _temporaryStorageExceptionCode(cls, exception: OSError) -> str | None:
        if exception.errno in (errno.ENOSPC, errno.EDQUOT):
            return 'TemporaryStorageInsufficient'
        if exception.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            return 'TemporaryStorageUnavailable'
        return cls._temporaryStorageErrorCode(str(exception))

    @staticmethod
    def _escapeAviSynthString(value: str) -> str:
        return value.replace('\\', '\\\\').replace('"', '\\"')

    def _failure(
        self,
        code: str,
        process: _ProcessResult,
        descriptor: CMInputDescriptor,
        decode_mode: CMDecodeMode,
        warnings: list[str],
    ) -> CMAnalyzerResult:
        diagnostic = process.diagnostic or f'Process exited with code {process.return_code}.'
        return CMAnalyzerResult(
            status='analysis_failed',
            chapter_file=None,
            analyzer_version=self.ANALYZER_VERSION,
            error_code=self._temporaryStorageErrorCode(diagnostic) or code,
            error_message=diagnostic,
            warnings=tuple(warnings),
            descriptor=descriptor,
            decode_mode=decode_mode,
        )


class UnavailableCMAnalyzer:
    """CM ランタイムを搭載しない環境向け。"""

    @property
    def runtimeFingerprint(self) -> dict[str, object]:
        """ランタイム非搭載状態を自動再試行keyへ含める。"""

        return {
            'analyzer_version': GenericCMAnalyzer.ANALYZER_VERSION,
            'available': False,
        }

    async def resolveInputDescriptor(self, request: CMAnalyzerRequest) -> CMInputDescriptor:
        """ランタイム非搭載をprobe前に確定する。"""

        del request
        raise CMInputUnsupportedError(
            'AnalyzerUnavailable',
            'The KonomiTV-BS4K CM analyzer runtime is not installed.',
        )

    async def analyze(self, request: CMAnalyzerRequest) -> CMAnalyzerResult:
        del request
        return CMAnalyzerResult(
            status='unsupported',
            chapter_file=None,
            analyzer_version=GenericCMAnalyzer.ANALYZER_VERSION,
            error_code='AnalyzerUnavailable',
            error_message='The KonomiTV-BS4K CM analyzer runtime is not installed.',
        )
