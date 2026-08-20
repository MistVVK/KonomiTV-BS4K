from __future__ import annotations

import asyncio
import json
import math
import re
import tempfile
import uuid
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, Literal

from fastapi import HTTPException, status

from app import logging
from app.config import Config
from app.constants import LIBRARY_PATH, QUALITY, QUALITY_TYPES
from app.models.RecordedProgram import RecordedProgram
from app.schemas import AudioTrack
from app.streams.KonomiTVBS4KPlaybackEncoding import (
    KONOMITV_BS4K_VIDEO_BITRATE_MINIMUM_GAP_KBPS,
    KONOMITV_BS4K_VIDEO_BITRATE_RATIOS_FROM_HEVC,
    KonomiTVBS4KPlaybackVideoBitrate,
    ResolveKonomiTVBS4KPlaybackVideoBitrate,
)
from app.streams.RecordedEncodingCodecs import AudioCodec, VideoCodec
from app.streams.RecordedFMP4Cache import (
    KonomiTVBS4KRecordedFMP4PathLock,
    RecordedFMP4CacheManager,
    RecordedFMP4Variant,
)
from app.streams.RecordedPlaybackCapabilities import (
    RecordedPlaybackBackend,
    RecordedPlaybackCapabilityProbe,
    RecordedPlaybackEncoder,
)
from app.streams.RecordedSubtitleStream import RecordedSubtitleStream
from app.streams.StreamEncodingOptions import StreamEncodingOptions
from app.streams.VideoSegmentPlanner import VideoSegmentPlanner
from app.utils.HLSText import sanitizeHLSQuotedString
from app.utils.KonomiTVBS4KMMTTLV import BuildKonomiTVBS4KMMTTLVInputArguments


@dataclass(frozen=True, slots=True)
class RecordedFMP4Segment:
    """録画先頭0秒基準のfMP4セグメント時間範囲を表す。"""

    sequence: int
    start_time: float
    duration: float
    generation: int
    # 元音声構成と映像DISCONTINUITYに対応する世代。
    audio_generation: int
    # 通常再生ではAAC/Opusの待ち時間を制限する最大6segmentのdelivery世代。
    # オフライン保存では同じ音声構成全体を1つのdelivery世代として扱う。
    transcoded_audio_generation: int | None = None
    transcoded_audio_start_sample: int | None = None
    transcoded_audio_sample_count: int | None = None


@dataclass(frozen=True, slots=True)
class RecordedPlaybackPrefetchRun:
    """通常再生で後続映像をまとめて生成する単一の先読み実行を表す。"""

    # この実行が生成する、同じ映像 generation の連続セグメント。
    segments: tuple[RecordedFMP4Segment, ...]
    # セッション破棄・シーク時にキャンセルし、対象要求から完了を待つ共有 task。
    task: asyncio.Task[bool]


RecordedVideoBitrate = KonomiTVBS4KPlaybackVideoBitrate


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
class RecordedAudioPacket:
    """単一moofのtrunとmdatから対応付けた圧縮音声packetを表す。"""

    duration: int
    size: int
    payload_offset: int
    trun_entry: bytes


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
    # session_id -> 接続元識別子（IP 等）。per-client 上限判定に使う。
    _session_client_keys: ClassVar[dict[str, str]] = {}
    # encoder semaphore 待ち行列の長さ。concurrency 上限とは別の admission 用カウンタ。
    _encoder_waiters: ClassVar[int] = 0
    # クライアントが発行する session_id の形式・長さ（UUID 先頭 8 hex 以上を想定）。
    SESSION_ID_PATTERN: ClassVar[re.Pattern[str]] = re.compile(r'^[0-9A-Za-z_-]{8,64}$')
    MAX_GLOBAL_SESSIONS: ClassVar[int] = 64
    MAX_SESSIONS_PER_CLIENT: ClassVar[int] = 8
    MAX_ENCODER_WAITERS: ClassVar[int] = 32
    SESSION_TIMEOUT: ClassVar[float] = 30.0
    SEEK_PREROLL_SECONDS: ClassVar[float] = 10.0
    # 現在要求は既存の単発経路で確実に返し、その直後だけを短い連続encodeへまとめる。
    # 2セグメントなら約12秒で、6秒の再生中に完了しやすくしつつ、単発2回分のprerollを1回へ減らせる。
    PLAYBACK_PREFETCH_MAX_SEGMENTS: ClassVar[int] = 2
    PLAYBACK_PREFETCH_MAX_DURATION_SECONDS: ClassVar[float] = 18.0
    AAC_ENCODER_DELAY_SAMPLES: ClassVar[int] = 1024
    AAC_PACKET_SAMPLES: ClassVar[int] = 1024
    AUDIO_SAMPLE_RATE: ClassVar[int] = 48_000
    OPUS_ENCODER_DELAY_SAMPLES: ClassVar[int] = 312
    AUDIO_BOUNDARY_COALESCE_SECONDS: ClassVar[float] = 1024 / 48_000
    AUDIO_DELIVERY_GENERATION_SEGMENTS: ClassVar[int] = 6
    AUDIO_INPUT_TAIL_MARGIN_SECONDS: ClassVar[float] = 0.1
    # MPEG-TS の入力シーク先が新しい音声 PID の出現境界と一致すると、既定の probe size では
    # 直前の PMT だけを見て対象 PID を入力Streamへ登録できないことがある。録画48では
    # 24MB以下でPID 0x113を見失い、50MBなら開始時刻を動かさず検出できることを確認済み。
    AUDIO_MPEGTS_PROBE_SIZE_BYTES: ClassVar[int] = 50_000_000
    AUDIO_MPEGTS_ANALYZE_DURATION_MICROSECONDS: ClassVar[int] = 5_000_000
    OPUS_BITRATES: ClassVar[tuple[tuple[range, int], ...]] = (
        (range(1, 2), 64_000),
        (range(2, 3), 128_000),
        (range(3, 5), 192_000),
        (range(5, 7), 256_000),
        (range(7, 9), 320_000),
    )
    # AVC / HEVC の既存調整値を保ちながら、VP9 と AV1 は HEVC より段階的に帯域を抑える。
    # ユーザーが選んだコーデックごとの通信量の差を明確にしつつ、全画質で同じ比率を適用する。
    VIDEO_BITRATE_RATIOS_FROM_HEVC = KONOMITV_BS4K_VIDEO_BITRATE_RATIOS_FROM_HEVC
    # 240p の既存最大値は AVC / HEVC とも 650K のため、録画再生だけ最低 50K の差を確保する。
    VIDEO_BITRATE_MINIMUM_GAP_KBPS = KONOMITV_BS4K_VIDEO_BITRATE_MINIMUM_GAP_KBPS
    # 端末へ長時間保持するオフライン映像は、通常再生の77%へ抑えて保存容量を削減する。
    # 全codec・全画質で同じ比率を使い、1Kbps単位の四捨五入後も平均値と最大値の関係を維持する。
    OFFLINE_VIDEO_BITRATE_PERCENT: ClassVar[int] = 77

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
    # 通常再生で現在実行中の短い後続先読み。キャッシュヒットから自動継続しない。
    _playback_prefetch_run: RecordedPlaybackPrefetchRun | None
    # 先読みの起動・シーク時キャンセル・完了時の参照解除を直列化する。
    _playback_prefetch_lock: asyncio.Lock
    # オフライン保存セッションだけ、未キャッシュ映像区間を連続encodeする。
    _is_offline_continuous: bool
    # 連続encode中の sequence から実行中 Task への対応。同一区間の重複起動を防ぐ。
    _offline_video_sequence_tasks: dict[int, asyncio.Task[bool]]
    # 連続encodeが当該 sequence の cache 書き込みまたは失敗確定を知らせる。
    _offline_video_segment_events: dict[int, asyncio.Event]
    # 連続encodeの起動判定を直列化する。
    _offline_video_encode_lock: asyncio.Lock
    # オフライン保存全体へ通知する、媒体時間ベースのエンコード進捗コールバック。
    _offline_progress_callback: Callable[[float], None] | None
    # 映像世代・音声レンディション世代ごとの処理重み。媒体時間と媒体種別から計算する。
    _offline_work_weights: dict[tuple[str, str], float]
    # 各処理単位の0～1進捗。再試行や工程切替で後退しない値だけを保持する。
    _offline_work_progress: dict[tuple[str, str], float]

    def __new__(
        cls,
        session_id: str,
        recorded_program: RecordedProgram,
        quality: QUALITY_TYPES,
        encoding_options: StreamEncodingOptions | None = None,
        is_new_session_allowed: bool = False,
        client_key: str = 'unknown',
        is_offline_continuous: bool = False,
    ) -> RecordedFMP4Stream:
        """session ID単位で単一の新録画視聴セッションを返す。"""

        # 既存・新規を問わず session_id 形式を先に検査する
        cls.validateSessionId(session_id)

        if session_id not in cls._instances:
            if is_new_session_allowed is False or encoding_options is None:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Session does not exist')
            # 新規作成時だけ global / per-client 上限で admission control する
            cls.admitNewSession(session_id, client_key)
            instance = super().__new__(cls)
            instance.session_id = session_id
            instance.recorded_program = recorded_program
            instance.quality = quality
            instance.encoding_options = encoding_options
            # セグメント計画ではオフライン時だけ音声を構成世代全体へまとめるため、
            # __buildSegments() より前に保存専用フラグを確定する。
            instance._is_offline_continuous = is_offline_continuous
            instance._effective_audio_codec = cls.resolveEffectiveAudioCodec(
                recorded_program,
                encoding_options.audio_codec,
            )
            instance._segments = instance.__buildSegments()
            instance._referenced_paths = set()
            instance._completed_sequences = set()
            instance._active_operations = 0
            instance._playback_prefetch_run = None
            instance._playback_prefetch_lock = asyncio.Lock()
            instance._offline_video_sequence_tasks = {}
            instance._offline_video_segment_events = {}
            instance._offline_video_encode_lock = asyncio.Lock()
            instance._offline_progress_callback = None
            instance._offline_work_weights = {}
            instance._offline_work_progress = {}
            instance._destroy_handle = asyncio.get_running_loop().call_later(
                cls.SESSION_TIMEOUT,
                lambda: asyncio.create_task(instance.__destroyIfIdle()),
            )
            cls._instances[session_id] = instance
            cls._session_client_keys[session_id] = client_key
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

    def setOfflineProgressCallback(self, callback: Callable[[float], None] | None) -> None:
        """オフライン一括生成の媒体時間ベース進捗を受け取るコールバックを設定する。

        Args:
            callback: 0～1の全体進捗を受け取る同期コールバック。Noneで通知を解除する。

        Returns:
            None
        """

        self._offline_progress_callback = callback
        self._offline_work_weights = {}
        self._offline_work_progress = {}
        if callback is None or self._is_offline_continuous is False:
            return

        video_durations: dict[tuple[str, str], float] = {}
        audio_durations: dict[tuple[str, str], float] = {}

        # 映像は同じconfiguration generationを1回のGPUエンコードへまとめる。
        if self.recorded_program.recorded_video.has_video is True:
            for generation in sorted({segment.generation for segment in self._segments}):
                key = self.__getOfflineVideoWorkKey(generation)
                video_durations[key] = sum(
                    segment.duration for segment in self._segments if segment.generation == generation
                )

        # 音声は論理レンディションごとに、同じcodec/layoutのdelivery generationを一括生成する。
        for rendition in self.getAudioRenditions():
            for generation in sorted({self.__getTranscodedAudioGeneration(segment) for segment in self._segments}):
                generation_segments = [
                    segment for segment in self._segments
                    if self.__getTranscodedAudioGeneration(segment) == generation
                ]
                if len(generation_segments) == 0:
                    continue
                key = self.__getOfflineAudioWorkKey(rendition.id, generation)
                audio_durations[key] = sum(segment.duration for segment in generation_segments)

        # GPU映像処理が実時間の大半を占めるため、映像ありでは映像90%・全音声10%へ配分する。
        # 各媒体内では構成世代とレンディションの媒体時間比を維持し、短い世代を過大評価しない。
        video_share = 0.9 if len(video_durations) > 0 and len(audio_durations) > 0 else (
            1.0 if len(video_durations) > 0 else 0.0
        )
        audio_share = 1.0 - video_share
        for durations, share in ((video_durations, video_share), (audio_durations, audio_share)):
            total_duration = sum(durations.values())
            if total_duration <= 0:
                continue
            for key, duration in durations.items():
                self._offline_work_weights[key] = share * duration / total_duration
                self._offline_work_progress[key] = 0.0

        callback(0.0)

    @staticmethod
    def __getOfflineVideoWorkKey(generation: int) -> tuple[str, str]:
        """映像configuration generationの進捗キーを返す。"""

        return ('Video', str(generation))

    @staticmethod
    def __getOfflineAudioWorkKey(rendition_id: str, generation: int) -> tuple[str, str]:
        """音声レンディションとdelivery generationの進捗キーを返す。"""

        return ('Audio', f'{rendition_id}:{generation}')

    def __updateOfflineWorkProgress(self, key: tuple[str, str], progress: float) -> None:
        """指定処理単位の進捗を単調増加させ、媒体時間で重み付けした全体値を通知する。"""

        callback = getattr(self, '_offline_progress_callback', None)
        work_weights = getattr(self, '_offline_work_weights', {})
        work_progress = getattr(self, '_offline_work_progress', {})
        if callback is None or key not in work_weights:
            return
        normalized = max(0.0, min(1.0, progress))
        work_progress[key] = max(work_progress.get(key, 0.0), normalized)
        total_weight = sum(work_weights.values())
        if total_weight <= 0:
            callback(1.0)
            return
        completed_weight = sum(
            weight * work_progress.get(work_key, 0.0)
            for work_key, weight in work_weights.items()
        )
        callback(min(1.0, completed_weight / total_weight))

    def __updateOfflineVideoGenerationProgressFromCompletedSegments(self, generation: int) -> None:
        """一括生成失敗時も、個別生成済みの媒体時間から映像世代の進捗を補完する。"""

        generation_segments = [segment for segment in self._segments if segment.generation == generation]
        total_duration = sum(segment.duration for segment in generation_segments)
        if total_duration <= 0:
            return
        completed_duration = sum(
            segment.duration for segment in generation_segments
            if segment.sequence in self._completed_sequences
        )
        self.__updateOfflineWorkProgress(
            self.__getOfflineVideoWorkKey(generation),
            completed_duration / total_duration,
        )

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

    def getSegments(self) -> tuple[RecordedFMP4Segment, ...]:
        """このセッションで固定したセグメント計画を読み取り専用で返す。

        Returns:
            tuple[RecordedFMP4Segment, ...]: 録画先頭から順に並んだセグメント計画。
        """

        # オフライン保存は通常 HLS と同じ時間境界を使う必要があるため、コピーした immutable tuple を公開する。
        return tuple(self._segments)

    @property
    def effective_audio_codec(self) -> AudioCodec:
        """録画の実音声構成を反映した配信音声コーデックを返す。

        Returns:
            AudioCodec: Opus 非対応構成の AAC fallback も反映した実効値。
        """

        return self._effective_audio_codec

    @classmethod
    def getVideoBitrate(cls, quality: QUALITY_TYPES, codec: VideoCodec) -> RecordedVideoBitrate:
        """共通画質と映像コーデックから指定値・最大値を解決する。

        Args:
            quality: 解像度・フレームレートを表す既存画質キー。
            codec: 録画再生で出力する映像コーデック。

        Returns:
            FFmpeg と HLS マスターで共有するコーデック別ビットレート。
        """

        return ResolveKonomiTVBS4KPlaybackVideoBitrate(quality, codec)

    @classmethod
    def getOfflineVideoBitrate(cls, quality: QUALITY_TYPES, codec: VideoCodec) -> RecordedVideoBitrate:
        """通常再生値の77%へ抑えたオフライン保存用映像ビットレートを返す。

        Args:
            quality: 解像度・フレームレートを表す既存画質キー。
            codec: オフライン保存で出力する映像コーデック。

        Returns:
            1Kbps単位で四捨五入したオフライン保存専用の指定値・最大値。
        """

        playback_bitrate = cls.getVideoBitrate(quality, codec)
        video_bitrate_kbps = int(playback_bitrate.video_bitrate.removesuffix('K'))
        video_bitrate_max_kbps = int(playback_bitrate.video_bitrate_max.removesuffix('K'))
        offline_video_bitrate_kbps = (video_bitrate_kbps * cls.OFFLINE_VIDEO_BITRATE_PERCENT + 50) // 100
        offline_video_bitrate_max_kbps = \
            (video_bitrate_max_kbps * cls.OFFLINE_VIDEO_BITRATE_PERCENT + 50) // 100
        return RecordedVideoBitrate(
            f'{offline_video_bitrate_kbps}K',
            f'{offline_video_bitrate_max_kbps}K',
        )

    def __getEffectiveVideoBitrate(self) -> RecordedVideoBitrate:
        """現在のセッション用途に対応する映像ビットレートを返す。

        Returns:
            通常再生では既存値、オフライン保存ではその77%の指定値・最大値。
        """

        if self._is_offline_continuous is True:
            return self.getOfflineVideoBitrate(self.quality, self.encoding_options.video_codec)
        return self.getVideoBitrate(self.quality, self.encoding_options.video_codec)

    @classmethod
    def validateSessionId(cls, session_id: str) -> None:
        """
        session_id の形式と長さを検査し、不正なら 422 を送出する。

        Args:
            session_id (str): クライアントが発行した視聴セッション ID

        Returns:
            None
        """

        if cls.SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail='Invalid session_id format or length',
            )

    @classmethod
    def admitNewSession(cls, session_id: str, client_key: str) -> None:
        """
        新規 session の global / per-client 上限を検査し、超過なら 429 を送出する。

        Args:
            session_id (str): これから作成する視聴セッション ID
            client_key (str): 接続元識別子（通常はクライアント IP）

        Returns:
            None
        """

        if len(cls._instances) >= cls.MAX_GLOBAL_SESSIONS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail='Too many active recorded playback sessions',
            )
        client_session_count = sum(1 for key in cls._session_client_keys.values() if key == client_key)
        if client_session_count >= cls.MAX_SESSIONS_PER_CLIENT:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail='Too many active recorded playback sessions for this client',
            )

    @classmethod
    @asynccontextmanager
    async def acquireEncoderSlot(cls, semaphore: asyncio.Semaphore) -> AsyncGenerator[None]:
        """
        encoder 用 semaphore を取得する。待ち行列が上限を超える場合は 429 にする。

        Args:
            semaphore (asyncio.Semaphore): CPU/GPU エンコード同時実行制限

        Returns:
            AsyncGenerator[None]: 取得中コンテキスト
        """

        # locked かつ waiters が上限以上なら、これ以上待たせず拒否する
        if semaphore.locked() is True and cls._encoder_waiters >= cls.MAX_ENCODER_WAITERS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail='Recorded encoder wait queue is full',
            )
        cls._encoder_waiters += 1
        try:
            await semaphore.acquire()
        finally:
            cls._encoder_waiters -= 1
        try:
            yield
        finally:
            semaphore.release()

    @staticmethod
    async def __communicateSubprocess(
        process: asyncio.subprocess.Process,
    ) -> tuple[bytes, bytes]:
        """子プロセスをdrainし、キャンセルや例外時も必ず終了・回収する。"""

        try:
            stdout, stderr = await process.communicate()
            return stdout or b'', stderr or b''
        except BaseException:
            if process.returncode is None:
                try:
                    process.kill()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    await process.wait()
                except (ProcessLookupError, OSError):
                    pass
            raise

    async def destroy(self) -> None:
        """セッション参照を解放し、キャッシュの60秒削除猶予を開始する。"""

        if self._instances.get(self.session_id) is not self:
            return
        self._destroy_handle.cancel()
        # 通常再生の先読みはオフライン保存taskと独立しているため、先に参照から外して回収する。
        # taskのfinallyも同じlockを取得するので、lock外へ出てから完了を待つ。
        playback_prefetch_task: asyncio.Task[bool] | None = None
        async with self._playback_prefetch_lock:
            if self._playback_prefetch_run is not None:
                playback_prefetch_task = self._playback_prefetch_run.task
                self._playback_prefetch_run = None
                playback_prefetch_task.cancel()
        if playback_prefetch_task is not None:
            await asyncio.gather(playback_prefetch_task, return_exceptions=True)
        offline_video_tasks = set(self._offline_video_sequence_tasks.values())
        for task in offline_video_tasks:
            task.cancel()
        if len(offline_video_tasks) > 0:
            await asyncio.gather(*offline_video_tasks, return_exceptions=True)
        for event in self._offline_video_segment_events.values():
            event.set()
        self._offline_video_sequence_tasks.clear()
        self._instances.pop(self.session_id, None)
        self._session_client_keys.pop(self.session_id, None)
        for cache_path in self._referenced_paths:
            await RecordedFMP4CacheManager.release(cache_path, self.session_id)
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
        lines = ['#EXTM3U', '#EXT-X-VERSION:7']
        renditions = self.getAudioRenditions()
        for index, rendition in enumerate(renditions):
            default = 'YES' if index == 0 else 'NO'
            language = sanitizeHLSQuotedString(rendition.language, default='und')
            name = sanitizeHLSQuotedString(rendition.name, default=f'Audio {index + 1}')
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
            name = sanitizeHLSQuotedString(
                track.get('title') or track.get('language') or f'Subtitle {track["index"]}',
                default=f'Subtitle {track["index"]}',
            )
            language = sanitizeHLSQuotedString(track.get('language'), default='und')
            lines.append(
                '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subtitles",'
                f'NAME="{name}",DEFAULT=NO,AUTOSELECT=YES,LANGUAGE="{language}",'
                f'URI="subtitle/{track["index"]}/playlist?session_id={self.session_id}&cache_key={cache_key}'
                f'&{self.getCodecQuery()}"'
            )

        # 音声のみ録画は黒映像を生成せず、主音声のfMP4 playlistをmaster variantとして直接返す。
        # URL queryの映像条件は既存APIとの互換placeholderとして維持するが、映像init / FFmpegは呼ばない。
        if getattr(self.recorded_program.recorded_video, 'has_video', True) is False:
            if len(renditions) == 0 or len(self._segments) == 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail='Audio rendition could not be generated',
                )
            primary_rendition = renditions[0]
            audio_init = await self.getAudioInitSegment(primary_rendition.id, self._segments[0].sequence)
            audio_codec_string = self.extractAudioCodecString(audio_init) if audio_init is not None else None
            if audio_codec_string is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        'code': 'CodecMismatch',
                        'message': 'The generated audio initialization segment has no supported codec configuration.',
                    },
                )
            stream_attributes = [
                f'BANDWIDTH={self.__getAudioBandwidth(renditions)}',
                f'CODECS="{audio_codec_string}"',
                'AUDIO="audio"',
            ]
            if subtitle_group_enabled:
                stream_attributes.append('SUBTITLES="subtitles"')
            lines.append('#EXT-X-STREAM-INF:' + ','.join(stream_attributes))
            lines.append(
                f'audio/{primary_rendition.id}/playlist?session_id={self.session_id}'
                f'&cache_key={cache_key}&{self.getCodecQuery()}'
            )
            return '\n'.join(lines) + '\n'

        video_bitrate = self.__getEffectiveVideoBitrate()
        bandwidth = int(video_bitrate.video_bitrate_max.removesuffix('K')) * 1000
        # CODECSは要求条件から推測せず、実際に配信するinit内のconfiguration boxから得る。
        # エンコーダーが選択したlevelやconstraintも実データと一致する値をブラウザーへ渡す。
        generation_segments: dict[int, RecordedFMP4Segment] = {}
        for segment in self._segments:
            # 初期化情報は同じ映像世代で共通なので、最初のセグメントを代表にする。
            # 最後のセグメントを選ぶと、オフライン連続エンコードが世代全体を完了するまで
            # master playlist を返せず、保存進捗も長時間 0% のままになる。
            generation_segments.setdefault(segment.generation, segment)
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

        return self.getAudioRenditionsForProgram(self.recorded_program)

    @classmethod
    def getAudioRenditionsForProgram(cls, recorded_program: RecordedProgram) -> list[RecordedAudioRendition]:
        """録画メタデータからDual Monoを展開したHLSレンディションを返す。

        Args:
            recorded_program: 音声トラックが再生索引で確定済みの録画番組。

        Returns:
            HLSへ公開する全論理音声レンディション。
        """

        renditions: list[RecordedAudioRendition] = []
        legacy_stream_offset = 1 if recorded_program.recorded_video.container_format == 'MPEG-TS' else 0
        for fallback_index, track in enumerate(recorded_program.recorded_video.audio_tracks, start=1):
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

    @classmethod
    def __getAudioSourceTrackForProgram(
        cls,
        recorded_program: RecordedProgram,
        rendition: RecordedAudioRendition,
        start_time: float | None = None,
    ) -> AudioTrack | None:
        """録画メタデータから指定時刻または代表構成に対応する索引Trackを返す。"""

        timeline_track = next(
            (
                track
                for interval in recorded_program.recorded_video.audio_track_timeline
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
                track for track in recorded_program.recorded_video.audio_tracks
                if int(track.get('index', 0)) == rendition.track_index
            ),
            None,
        )

    def __getAudioSourceTrack(
        self,
        rendition: RecordedAudioRendition,
        start_time: float | None = None,
    ) -> AudioTrack | None:
        """このセッションの指定時刻または代表構成に対応する索引Trackを返す。"""

        return self.__getAudioSourceTrackForProgram(self.recorded_program, rendition, start_time)

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

    @classmethod
    def __getAudioRenditionChannelCountsForProgram(
        cls,
        recorded_program: RecordedProgram,
        rendition: RecordedAudioRendition,
    ) -> list[int] | None:
        """録画メタデータから全区間に出現するレンディションのチャンネル数を返す。"""

        if rendition.channel in ('main', 'sub'):
            return [1]
        source_tracks = [
            track
            for interval in recorded_program.recorded_video.audio_track_timeline
            for track in interval['tracks']
            if int(track.get('index', 0)) == rendition.track_index
        ]
        if len(source_tracks) == 0:
            source_track = cls.__getAudioSourceTrackForProgram(recorded_program, rendition)
            source_tracks = [source_track] if source_track is not None else []
        channel_counts = [cls.getAudioChannelCount(track) for track in source_tracks]
        if len(channel_counts) == 0 or any(channels is None for channels in channel_counts):
            return None
        return sorted({channels for channels in channel_counts if channels is not None})

    def __getAudioRenditionChannelCounts(self, rendition: RecordedAudioRendition) -> list[int] | None:
        """このセッションの録画全区間に出現するレンディションのチャンネル数を返す。"""

        return self.__getAudioRenditionChannelCountsForProgram(self.recorded_program, rendition)

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

    @classmethod
    def resolveEffectiveAudioCodec(
        cls,
        recorded_program: RecordedProgram,
        requested_codec: AudioCodec,
    ) -> AudioCodec:
        """録画の実音声構成からセッション全体の実効音声方式を決定する。

        Args:
            recorded_program: 音声トラックと構成タイムラインが確定済みの録画番組。
            requested_codec: クライアントが要求した音声コーデック。

        Returns:
            Opus非対応構成のAAC fallbackを反映した実効音声コーデック。
        """

        if requested_codec == 'opus':
            # Opusは既知の1～8ch構成だけを保持してエンコードする。未知layoutを暗黙に
            # stereoへdownmixせず、セッション全体を互換性の高いAACへ切り替える。
            renditions = cls.getAudioRenditionsForProgram(recorded_program)
            if len(renditions) == 0 or any(
                channel_counts is None or
                any(cls.getOpusBitrate(channels) is None for channels in channel_counts)
                for rendition in renditions
                for channel_counts in [cls.__getAudioRenditionChannelCountsForProgram(recorded_program, rendition)]
            ):
                logging.warning(
                    '[RecordedFMP4Stream] Opus channel layout is unsupported; falling back to AAC. '
                    f'[recorded_video_id: {recorded_program.recorded_video.id}]'
                )
                return 'aac'
        return requested_codec

    @classmethod
    def getEstimatedAudioBitrateKbps(
        cls,
        recorded_program: RecordedProgram,
        requested_codec: AudioCodec,
    ) -> int:
        """オフライン保存容量見積もり用の全レンディション合計音声ビットレートを返す。

        Args:
            recorded_program: 音声トラックと構成タイムラインが確定済みの録画番組。
            requested_codec: クライアントが要求した音声コーデック。

        Returns:
            実効音声方式と各レンディションの実チャンネル数から求めた合計 kbps。
        """

        # 未知 layout は AAC へ落とすので、見積もりも実エンコードと同じ実効方式を使う。
        effective_codec = cls.resolveEffectiveAudioCodec(recorded_program, requested_codec)
        renditions = cls.getAudioRenditionsForProgram(recorded_program)
        if effective_codec != 'opus':
            return 192 * len(renditions)

        total_bitrate_kbps = 0
        for rendition in renditions:
            # Dual Mono の main/sub は 1ch、通常トラックは全区間の最大 ch を使う。
            channel_counts = cls.__getAudioRenditionChannelCountsForProgram(recorded_program, rendition)
            bitrate = cls.getOpusBitrate(max(channel_counts)) if channel_counts is not None else None
            if bitrate is None:
                # resolveEffectiveAudioCodec が opus を返した時点では到達しない。
                # 見積もりだけを止めないため、そのレンディションだけ 8ch 相当で積む。
                total_bitrate_kbps += 320
                continue
            total_bitrate_kbps += bitrate // 1000
        return total_bitrate_kbps

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
        segment_data = await self.__getTranscodedAudioSegment(segment, rendition, self._effective_audio_codec)
        if segment_data is not None and getattr(self.recorded_program.recorded_video, 'has_video', True) is False:
            self._completed_sequences.add(sequence)
        return segment_data

    async def getVideoInitSegment(self, generation: int, sequence: int) -> bytes | None:
        """指定世代の実エンコード結果から得た初期化セグメントを返す。"""

        segment = self.__getSegment(sequence)
        if segment is None or segment.generation != generation:
            return None
        init_path = self.__buildCachePath(segment, is_init=True)
        # 既存initも読み取り前にセッション参照へ登録し、別セッション終了後の遅延削除と競合させない。
        # master playlist生成で一度読んだinitが、オフライン本体で再取得するまでに消えることを防ぐ。
        await self.__acquire(init_path)
        # master生成や同じ映像世代の先行segmentですでにinitを確定済みなら、音声だけの
        # DISCONTINUITY境界に対応するsegment encodeを待たず即座に返す。
        if init_path.is_file():
            # initは数KB以下の小さな固定データなので、executorへ渡すより同期読込の方が
            # 境界MAPへの応答を確実に即時化できる。
            return init_path.read_bytes()
        # MAP取得は通常のmedia要求ではない。ここから先読みを開始すると、プレイリストにある
        # 別generationのMAP取得同士が先読みをキャンセルし合うため、現在segmentだけを生成する。
        async with self.__activeOperation():
            await self.__getVideoSegment(sequence, should_start_playback_prefetch=False)
        if init_path.is_file() is False:
            return None
        return await asyncio.to_thread(init_path.read_bytes)

    async def getVideoSegment(self, sequence: int) -> bytes | None:
        """要求シーケンスの映像fragmentを生成またはキャッシュから返す。"""

        async with self.__activeOperation():
            return await self.__getVideoSegment(sequence, should_start_playback_prefetch=True)

    async def __getVideoSegment(
        self,
        sequence: int,
        should_start_playback_prefetch: bool = False,
    ) -> bytes | None:
        """映像fragment生成の本体処理を行う。

        Args:
            sequence: 取得するHLSメディアシーケンス。
            should_start_playback_prefetch: 通常のmedia要求として後続先読みを許可するか。

        Returns:
            生成済み映像fragment。生成できない場合はNone。
        """

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
        is_offline_continuous = getattr(self, '_is_offline_continuous', False)

        # 先読み対象なら共有taskの完了を待ち、範囲外要求なら古い方向の先読みを先に回収する。
        # path lockを保持したまま待つと、先読み側のatomic公開と相互待ちになるため必ず先に行う。
        if is_offline_continuous is False and should_start_playback_prefetch is True:
            playback_prefetch_task = await self.__preparePlaybackPrefetchRequest(sequence)
            if playback_prefetch_task is not None:
                try:
                    await asyncio.shield(playback_prefetch_task)
                except asyncio.CancelledError:
                    # シークで共有先読みだけがcancelされた場合は単発生成へフォールバックする。
                    # HTTP要求自体がcancelされた場合は、そのキャンセルを握り潰さず呼び出し元へ返す。
                    current_task = asyncio.current_task()
                    if current_task is not None and current_task.cancelling() > 0:
                        raise

        segment_path = self.__buildCachePath(segment, is_init=False)
        init_path = self.__buildCachePath(segment, is_init=True)
        segment_lock = await self.__acquire(segment_path)
        await self.__acquire(init_path)
        segment_data: bytes | None = None
        did_encode_current_segment = False
        async with segment_lock:
            # fragmentだけが残り、対になるinitが遅延削除済みなら再生成する。
            # 片方だけをキャッシュヒットとして返すと、呼び出し元は再生不能なfragmentを受け取ってしまう。
            if segment_path.is_file() and init_path.is_file():
                self._completed_sequences.add(sequence)
                generation_segments = [
                    item for item in self._segments if item.generation == segment.generation
                ]
                if all(self.__buildCachePath(item, is_init=False).is_file() for item in generation_segments):
                    self.__updateOfflineWorkProgress(
                        self.__getOfflineVideoWorkKey(segment.generation),
                        1.0,
                    )
                segment_data = await asyncio.to_thread(segment_path.read_bytes)
            elif is_offline_continuous is False:
                # 現在必要なfragmentは実績のある単発経路で確定し、後続先読みの成否へ依存させない。
                await self.__encodeSegment(segment, init_path, segment_path)
                # fragment 単体を成功扱いすると、直後の MAP 取得だけが失敗するため、
                # 通常再生でも対応する init と fragment の両方が揃った場合だけ返す。
                if segment_path.is_file() is False or init_path.is_file() is False:
                    return None
                self._completed_sequences.add(sequence)
                self.__updateOfflineVideoGenerationProgressFromCompletedSegments(segment.generation)
                segment_data = await asyncio.to_thread(segment_path.read_bytes)
                did_encode_current_segment = True
            else:
                # 連続encodeの書き込みは別taskがpath lockを取るため、待ちに入る前に解放する。
                await self.__startOfflineVideoEncodeIfNeeded(segment)

        if is_offline_continuous is False:
            # キャッシュヒットや先読みtaskの成果からは次を起動しない。直接単発生成した要求だけを
            # 起点にすることで、短い先読みがファイル末尾まで自動連鎖することを防ぐ。
            if (
                segment_data is not None and
                did_encode_current_segment is True and
                should_start_playback_prefetch is True
            ):
                await self.__startPlaybackPrefetchAfter(segment)
            return segment_data

        if is_offline_continuous is True:
            await self.__waitOfflineVideoSegment(segment)
            async with segment_lock:
                # 連続生成 task の完了通知後にも、必ず init / fragment の組を再検査する。
                # fragment だけが残った状態を返すと getVideoInitSegment() が直後に None となり、
                # 長時間生成の全成果を破棄してしまうため、不完全な組は個別生成で復旧する。
                if segment_path.is_file() and init_path.is_file():
                    self._completed_sequences.add(sequence)
                    return await asyncio.to_thread(segment_path.read_bytes)
                # 連続encodeがこのsequenceを確定できなければ、現行の1本encodeへ落とす。
                await self.__encodeSegment(segment, init_path, segment_path)
                if segment_path.is_file() is False or init_path.is_file() is False:
                    return None
                self._completed_sequences.add(sequence)
                self.__updateOfflineVideoGenerationProgressFromCompletedSegments(segment.generation)
                return await asyncio.to_thread(segment_path.read_bytes)
        return None

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
            # 通常再生は先頭segmentの待ち時間を制限するため最大6segmentに分ける。一方、
            # 完成後にしか再生しないオフライン保存は構成世代全体を1回のencodeへまとめ、
            # encoder primingと一時ファイル生成を最小化する。
            delivery_generation_segments = len(configuration_segments) \
                if getattr(self, '_is_offline_continuous', False) is True \
                else self.AUDIO_DELIVERY_GENERATION_SEGMENTS
            for group_start in range(0, len(configuration_segments), delivery_generation_segments):
                generation_segments = configuration_segments[
                    group_start:group_start + delivery_generation_segments
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

    def __buildVideoEncodeCommand(
        self,
        start_time: float,
        duration: float,
        output_arguments: list[str],
        *,
        force_keyframe_times: list[float] | None = None,
        sequence_for_warning: int | None = None,
    ) -> tuple[list[str], RecordedPlaybackEncoder, str | None, str] | None:
        """映像filter・encoder・入出力引数を組み立てる。

        Args:
            start_time: 録画先頭基準の要求開始時刻。
            duration: 切り出す映像の長さ。
            output_arguments: pipe または segment muxer など、出力側のFFmpeg引数。
            force_keyframe_times: 出力時刻0起点の強制キーフレーム時刻。連続encode用。
            sequence_for_warning: 走査方式不明時のログに出す代表sequence。

        Returns:
            コマンド、backend、render device、encoder pixel format。encoderが無い場合はNone。
        """

        quality = QUALITY[self.quality]
        video_bitrate = self.__getEffectiveVideoBitrate()
        video_bitrate_max_kbps = int(video_bitrate.video_bitrate_max.removesuffix('K'))
        backend = self.__getBackend()
        codec = self.encoding_options.video_codec
        bit_depth = self.encoding_options.video_bit_depth
        spec = RecordedPlaybackBackend.getCodecSpec(codec, bit_depth)
        encoder_name = RecordedPlaybackBackend.getEncoderName(backend, codec)
        if encoder_name is None:
            return None
        input_seek, trim_start, input_duration = self.computeInputSeekWindow(
            start_time,
            duration,
        )
        filters: list[str] = []
        timeline = self.recorded_program.recorded_video.video_stream_timeline or []
        entry = next(
            (item for item in timeline if float(item['start_time']) <= start_time < float(item['end_time'])),
            None,
        )
        scan_type = entry.get('scan_type') if entry else self.recorded_program.recorded_video.video_scan_type
        is_interlaced = scan_type == 'Interlaced'
        if is_interlaced:
            if backend == 'FFmpeg':
                filters.append(f'bwdif=mode={"send_field" if quality.is_60fps else "send_frame"}:parity=auto:deint=interlaced')
        elif scan_type not in ('Progressive', None):
            sequence_label = sequence_for_warning if sequence_for_warning is not None else -1
            logging.warning(
                '[RecordedFMP4Stream] video scan type is unknown; deinterlace is disabled. '
                f'[recorded_video_id: {self.recorded_program.recorded_video.id}, sequence: {sequence_label}]'
            )
        if backend == 'FFmpeg':
            filters += [
                f'scale=w={quality.width}:h={quality.height}:force_original_aspect_ratio=decrease',
                f'pad={quality.width}:{quality.height}:(ow-iw)/2:(oh-ih)/2',
            ]
            if is_interlaced and self.encoding_options.is_24fps_mode_enabled:
                filters += ['pullup', 'dejudder']
            filters += [
                f'trim=start={trim_start:.6f}:duration={duration:.6f}',
                'setpts=PTS-STARTPTS',
                f'format={spec.pixel_format}',
            ]

        command = [
            RecordedPlaybackBackend.getExecutable(backend), '-hide_banner', '-loglevel', 'error',
        ]
        device: str | None = None
        if backend in ('QSV', 'AMF'):
            # 能力検査で同じ画質を完走したrender nodeを使い、低解像度だけ成功する旧GPUへ戻さない。
            selected_device = RecordedPlaybackCapabilityProbe.getSelectedDevice(
                backend,
                codec,
                bit_depth,
                self.quality,
            )
            devices = [selected_device] if selected_device is not None else \
                RecordedPlaybackBackend.discoverRenderDevices(backend)
            if len(devices) == 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={'code': 'DeviceUnavailable', 'message': 'A compatible render device was not found.'},
                )
            device = devices[0]
        if backend == 'QSV':
            command += [
                '-init_hw_device', f'qsv=recorded_qsv:{device}', '-filter_hw_device', 'recorded_qsv',
                '-hwaccel', 'qsv', '-hwaccel_output_format', 'qsv',
            ]
        elif backend == 'NVENC':
            command += [
                '-init_hw_device', 'cuda=recorded_cuda:0', '-filter_hw_device', 'recorded_cuda',
                '-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda',
            ]
        elif backend == 'AMF':
            command += [
                '-init_hw_device', f'vaapi=recorded_vaapi:{device}', '-filter_hw_device', 'recorded_vaapi',
                '-hwaccel', 'vaapi', '-hwaccel_device', 'recorded_vaapi', '-hwaccel_output_format', 'vaapi',
            ]
        command += [
            *BuildKonomiTVBS4KMMTTLVInputArguments(
                self.recorded_program.recorded_video.container_format,
            ),
            # TSを要求位置から直接復号すると、直前キーフレームの参照画像を失い、次の
            # 復号可能なキーフレームまで数秒飛ぶ。手前から復号してfilterで要求位置へ切る。
            '-ss', f'{input_seek:.6f}',
            '-i', self.recorded_program.recorded_video.file_path,
            '-t', f'{input_duration:.6f}',
            '-map', '0:v:0', '-an',
        ]
        if backend == 'QSV':
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
                f'trim=start={trim_start:.6f}:duration={duration:.6f}',
                'setpts=PTS-STARTPTS',
            ]
        elif backend == 'NVENC':
            if is_interlaced:
                filters.append(f'bwdif_cuda=mode={"send_field" if quality.is_60fps else "send_frame"}:parity=auto')
            filters.append(f'scale_cuda=w={quality.width}:h={quality.height}:format={spec.encoder_pixel_format}')
            if is_interlaced and self.encoding_options.is_24fps_mode_enabled:
                filters += [
                    f'hwdownload,format={spec.encoder_pixel_format}', 'pullup', 'dejudder',
                    f'format={spec.encoder_pixel_format}', 'hwupload_cuda',
                ]
            filters += [
                f'trim=start={trim_start:.6f}:duration={duration:.6f}',
                'setpts=PTS-STARTPTS',
            ]
        elif backend == 'AMF':
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
                f'trim=start={trim_start:.6f}:duration={duration:.6f}',
                'setpts=PTS-STARTPTS',
            ]
        command += [
            '-vf', ','.join(filters),
            '-c:v', encoder_name,
        ]
        if backend == 'QSV' and force_keyframe_times:
            # 連続encodeでは区間境界をIDRにし、HLS fragmentを独立復号できるようにする。
            command += ['-forced_idr', '1']
        if backend == 'FFmpeg':
            command += ['-pix_fmt', spec.pixel_format]
        elif backend == 'NVENC':
            command += ['-pix_fmt', 'cuda']
        elif backend == 'AMF':
            command += ['-pix_fmt', spec.encoder_pixel_format]
        command += self.__getProfileArguments(backend, codec, bit_depth)
        command += RecordedPlaybackBackend.getTuningArguments(backend, codec)
        if codec == 'hevc':
            command += ['-tag:v', 'hvc1']
        # 録画 MPEG-TS の PTS には局側の欠落や seek 直後の不連続が含まれることがあり、
        # FFmpeg の自動同期へ任せると同じ約6秒の fragmentでも出力枚数が大きく変動する。
        # 通常画質は画質名の契約どおり 29.97 / 59.94fps CFR へ正規化し、Chrome の
        # decoder queueへ疎な timestamp列を渡さない。逆テレシネ時だけは24/30p混在を
        # 保つ必要があるため、ライブ再生と同様に VFR を明示して -r を付けない。
        if self.encoding_options.is_24fps_mode_enabled is True:
            command += ['-fps_mode', 'vfr']
        else:
            command += [
                '-r', '60000/1001' if quality.is_60fps is True else '30000/1001',
                '-fps_mode', 'cfr',
            ]
        command += [
            '-b:v', video_bitrate.video_bitrate, '-maxrate', video_bitrate.video_bitrate_max,
            '-bufsize', f'{video_bitrate_max_kbps * 2}K',
            # 1080p 品質は帯域削減のため 1440x1080 の anamorphic 映像として出力する。
            # 入力が square pixel の 1920x1080 / 3840x2160 でも 4:3 と解釈されないよう、
            # encoder / MP4 muxer へ表示アスペクト比を明示する。
            '-aspect', '16:9',
        ]
        if force_keyframe_times:
            command += ['-force_key_frames', ','.join(f'{time:.6f}' for time in force_keyframe_times)]
        command += output_arguments
        return command, backend, device, spec.encoder_pixel_format

    async def __encodeSegment(
        self,
        segment: RecordedFMP4Segment,
        init_path: Path,
        segment_path: Path,
    ) -> None:
        """FFmpeg 8で自己完結fMP4を生成し、initとfragmentへ分離する。"""

        plan = self.__buildVideoEncodeCommand(
            segment.start_time,
            segment.duration,
            [
                # libaom-av1は最初のpacketでsequence headerを返すため、empty_moovでは空のav1Cが
                # 出力される。delay_moovで全codecの実configurationを含むinitを確定してから書く。
                '-movflags', '+frag_keyframe+delay_moov+default_base_moof+negative_cts_offsets',
                '-f', 'mp4', 'pipe:1',
            ],
            sequence_for_warning=segment.sequence,
        )
        if plan is None:
            return
        command, backend, device, encoder_pixel_format = plan
        stdout, stderr, returncode = await self.__runVideoEncodeProcess(command, backend, device, encoder_pixel_format)
        if returncode != 0:
            logging.error(
                '[RecordedFMP4Stream] FFmpeg 8 video segment failed. '
                f'[recorded_video_id: {self.recorded_program.recorded_video.id}, sequence: {segment.sequence}, '
                f'stderr: {stderr.decode(errors="ignore").strip()}]'
            )
            return
        if await self.__storeEncodedVideoFragment(segment, stdout, init_path, segment_path) is False:
            logging.error('[RecordedFMP4Stream] FFmpeg 8 output did not contain init and media fragments.')

    async def __runVideoEncodeProcess(
        self,
        command: list[str],
        backend: RecordedPlaybackEncoder,
        device: str | None,
        encoder_pixel_format: str,
    ) -> tuple[bytes, bytes, int]:
        """映像encoderを1回実行し、HW decode失敗時だけ同じGPUへ再試行する。

        Args:
            command: 組み立て済みのFFmpegコマンド。
            backend: 実行する録画再生バックエンド。
            device: GPU render node。CPU encodeではNone。
            encoder_pixel_format: software decode再試行時のupload先pixel format。

        Returns:
            stdout、stderr、終了コード。
        """

        semaphore_key = 'CPU' if backend == 'FFmpeg' else f'{backend}:{device or 0}'
        semaphore = self._cpu_semaphore if backend == 'FFmpeg' else self._gpu_semaphores.setdefault(
            semaphore_key,
            asyncio.Semaphore(1),
        )
        async with self.acquireEncoderSlot(semaphore):
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout = asyncio.subprocess.PIPE,
                stderr = asyncio.subprocess.PIPE,
                env = RecordedPlaybackBackend.getEnvironment(backend),
            )
            stdout, stderr = await self.__communicateSubprocess(process)
            returncode = process.returncode if process.returncode is not None else 1
            if (
                returncode != 0 and backend != 'FFmpeg' and
                self.recorded_program.recorded_video.container_format != 'MPEG-TS' and
                self.shouldRetryWithSoftwareDecode(stderr)
            ):
                # MP4/MKV/WebM でHW decodeだけが失敗した場合は、同じGPU encoderへ
                # system-memoryフレームをuploadし直す。エンコーダーのCPU降格は行わない。
                fallback_command = self.buildSoftwareDecodeFallback(command, backend, encoder_pixel_format)
                process = await asyncio.create_subprocess_exec(
                    *fallback_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=RecordedPlaybackBackend.getEnvironment(backend),
                )
                stdout, stderr = await self.__communicateSubprocess(process)
                returncode = process.returncode if process.returncode is not None else 1
        return stdout, stderr, returncode

    async def __storeEncodedVideoFragment(
        self,
        segment: RecordedFMP4Segment,
        encoded_data: bytes,
        init_path: Path,
        segment_path: Path,
    ) -> bool:
        """自己完結fMP4をinitとHLS fragmentへ分けてcacheへ書く。

        Args:
            segment: 書き込む映像セグメント。
            encoded_data: FFmpegが出力した自己完結fMP4。
            init_path: generation共有のinitキャッシュ。
            segment_path: このsequenceのfragmentキャッシュ。

        Returns:
            initとmediaを分離できた場合はTrue。
        """

        init_data, media_data = self.splitFragmentedMP4(encoded_data)
        if len(init_data) == 0 or len(media_data) == 0:
            return False
        media_data = self.normalizeFragmentTimeline(
            init_data,
            media_data,
            segment.start_time,
            segment.sequence,
        )
        if init_path.is_file() is False:
            await RecordedFMP4CacheManager.writeAtomic(init_path, init_data)
        await RecordedFMP4CacheManager.writeAtomic(segment_path, media_data)
        return True

    def __getPlaybackPrefetchSegments(
        self,
        current_segment: RecordedFMP4Segment,
    ) -> list[RecordedFMP4Segment]:
        """直接生成した現在fragmentの直後から、短い未キャッシュ連続区間を返す。

        Args:
            current_segment: browserへ返すため単発生成した現在のセグメント。

        Returns:
            同じ映像generation内で本数・媒体時間上限に収まる後続セグメント。
        """

        segments: list[RecordedFMP4Segment] = []
        total_duration = 0.0
        sequence = current_segment.sequence + 1
        while sequence < len(self._segments):
            segment = self._segments[sequence]
            # codec configurationが変わる境界を単一の連続encodeへ混在させない。
            if segment.generation != current_segment.generation:
                break
            # cache済み区間を飛び越して遠方のholeを生成すると、cache hitだけで先読みが
            # ファイル末尾まで連鎖するため、直後からの連続holeだけを対象にする。
            if self.__buildCachePath(segment, is_init=False).is_file():
                break
            if len(segments) >= self.PLAYBACK_PREFETCH_MAX_SEGMENTS:
                break
            if total_duration + segment.duration > self.PLAYBACK_PREFETCH_MAX_DURATION_SECONDS:
                break
            segments.append(segment)
            total_duration += segment.duration
            sequence += 1
        return segments

    async def __preparePlaybackPrefetchRequest(self, sequence: int) -> asyncio.Task[bool] | None:
        """現在要求を実行中の先読みに接続し、範囲外なら古い先読みを回収する。

        Args:
            sequence: browserが現在必要としているシーケンス。

        Returns:
            このsequenceを生成中なら共有task。それ以外はNone。
        """

        task_to_cancel: asyncio.Task[bool] | None = None
        async with self._playback_prefetch_lock:
            run = self._playback_prefetch_run
            if run is None:
                return None
            if any(segment.sequence == sequence for segment in run.segments):
                return run.task
            # シーク先を含まない実行は参照から先に外す。taskのfinallyも同じlockを取るため、
            # cancel完了はlock外で待たなければデッドロックする。
            self._playback_prefetch_run = None
            if run.task.done() is False:
                task_to_cancel = run.task
                task_to_cancel.cancel()
        if task_to_cancel is not None:
            await asyncio.gather(task_to_cancel, return_exceptions=True)
            logging.info(
                '[RecordedFMP4Stream] Cancelled playback prefetch for a priority segment request. '
                f'[recorded_video_id: {self.recorded_program.recorded_video.id}, sequence: {sequence}]'
            )
        return None

    async def __startPlaybackPrefetchAfter(self, current_segment: RecordedFMP4Segment) -> None:
        """現在fragmentの直後だけを単一FFmpegで短く先読みする。

        Args:
            current_segment: browserへ返すため単発生成した現在のセグメント。

        Returns:
            None
        """

        async with self._playback_prefetch_lock:
            # 1セッションにつき1本だけを許可し、完了後もcache hitから自動継続しない。
            if self._playback_prefetch_run is not None:
                return
            segments = self.__getPlaybackPrefetchSegments(current_segment)
            if len(segments) == 0:
                return
            immutable_segments = tuple(segments)
            task = asyncio.create_task(self.__encodePlaybackPrefetchRun(immutable_segments))
            self._playback_prefetch_run = RecordedPlaybackPrefetchRun(
                segments=immutable_segments,
                task=task,
            )
            logging.info(
                '[RecordedFMP4Stream] Started bounded playback prefetch. '
                f'[recorded_video_id: {self.recorded_program.recorded_video.id}, '
                f'start_sequence: {segments[0].sequence}, end_sequence: {segments[-1].sequence}, '
                f'count: {len(segments)}]'
            )

    async def __encodePlaybackPrefetchRun(
        self,
        segments: tuple[RecordedFMP4Segment, ...],
    ) -> bool:
        """短い後続区間を連続encodeし、実行参照を必ず解除する。

        Args:
            segments: 同じ映像generationの後続セグメント。

        Returns:
            全fragmentをatomic公開できた場合はTrue。
        """

        succeeded = False
        try:
            # background task自体をactive operationとして数え、Keep-Alive間隔の直前に
            # セッションとcache参照が破棄されることを防ぐ。
            async with self.__activeOperation():
                succeeded = await self.__encodeContinuousVideoRun(
                    list(segments),
                    purpose='PlaybackPrefetch',
                )
            return succeeded
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.error(
                '[RecordedFMP4Stream] Playback prefetch failed unexpectedly. '
                f'[recorded_video_id: {self.recorded_program.recorded_video.id}, '
                f'start_sequence: {segments[0].sequence}, end_sequence: {segments[-1].sequence}]',
                exc_info=True,
            )
            return False
        finally:
            current_task = asyncio.current_task()
            async with self._playback_prefetch_lock:
                run = self._playback_prefetch_run
                if run is not None and run.task is current_task:
                    self._playback_prefetch_run = None
            if succeeded is False and current_task is not None and current_task.cancelling() == 0:
                logging.warning(
                    '[RecordedFMP4Stream] Bounded playback prefetch did not publish every fragment. '
                    f'[recorded_video_id: {self.recorded_program.recorded_video.id}, '
                    f'start_sequence: {segments[0].sequence}, end_sequence: {segments[-1].sequence}]'
                )

    def __getUncachedVideoRun(self, segment: RecordedFMP4Segment) -> list[RecordedFMP4Segment]:
        """同じ映像generation内で、指定segmentを含む未キャッシュ連続区間を返す。

        Args:
            segment: 要求された映像セグメント。

        Returns:
            未キャッシュの連続セグメント。左右はキャッシュ済みまたはgeneration境界で止める。
        """

        # 完成後にしか再生しないオフライン保存は、途中キャッシュの有無で一括処理を分断しない。
        # 1片でも欠けていればgeneration全体を単一fMP4へ再生成し、正確な媒体時間進捗を維持する。
        if self._is_offline_continuous is True:
            return [item for item in self._segments if item.generation == segment.generation]

        start = segment.sequence
        while start > 0:
            previous = self._segments[start - 1]
            if previous.generation != segment.generation:
                break
            if self.__buildCachePath(previous, is_init=False).is_file():
                break
            start -= 1
        end = segment.sequence
        while end + 1 < len(self._segments):
            following = self._segments[end + 1]
            if following.generation != segment.generation:
                break
            if self.__buildCachePath(following, is_init=False).is_file():
                break
            end += 1
        return self._segments[start:end + 1]

    @staticmethod
    def __getVideoRunSplitTimes(segments: list[RecordedFMP4Segment]) -> list[float]:
        """連続encode出力の0起点で、内部セグメント境界時刻を返す。

        Args:
            segments: 同じ連続encodeに載せる未キャッシュ区間。

        Returns:
            `-force_key_frames` と `-segment_times` に渡す内部境界。
        """

        split_times: list[float] = []
        elapsed = 0.0
        for item in segments[:-1]:
            elapsed += item.duration
            split_times.append(elapsed)
        return split_times

    async def __startOfflineVideoEncodeIfNeeded(self, segment: RecordedFMP4Segment) -> None:
        """未キャッシュ連続区間の連続encodeを、未起動なら開始する。

        Args:
            segment: 要求された映像セグメント。

        Returns:
            None
        """

        async with self._offline_video_encode_lock:
            if segment.sequence in self._offline_video_sequence_tasks:
                return
            run = self.__getUncachedVideoRun(segment)
            # 1segmentだけの短いgenerationも同じ単一fMP4経路へ載せ、媒体時間進捗を維持する。
            task = asyncio.create_task(self.__encodeOfflineVideoRun(run))
            for item in run:
                self._offline_video_sequence_tasks[item.sequence] = task
                self._offline_video_segment_events.setdefault(item.sequence, asyncio.Event())

    async def __waitOfflineVideoSegment(self, segment: RecordedFMP4Segment) -> None:
        """連続encodeが当該sequenceを確定するまで待つ。

        Args:
            segment: 待ち対象の映像セグメント。

        Returns:
            None
        """

        event = self._offline_video_segment_events.get(segment.sequence)
        if event is None:
            return
        await event.wait()

    async def __encodeOfflineVideoRun(self, segments: list[RecordedFMP4Segment]) -> bool:
        """未キャッシュ連続区間を単一fMP4へencodeし、完了後に無劣化分割してcacheへ書く。

        Args:
            segments: 同じ映像generationの未キャッシュ連続区間。

        Returns:
            全fragmentを書けた場合はTrue。
        """

        first_segment = segments[0]
        try:
            return await self.__encodeContinuousVideoRun(segments, purpose='Offline')
        except asyncio.CancelledError:
            raise
        except Exception:
            # 独立 task の想定外例外も未回収にせず、必ず原因を記録して待ち側の
            # 1セグメントencodeへ落とす。CancelledError は直前で再送出している。
            logging.error(
                '[RecordedFMP4Stream] Offline continuous video encode failed. '
                f'[recorded_video_id: {self.recorded_program.recorded_video.id}, '
                f'start_sequence: {first_segment.sequence}, end_sequence: {segments[-1].sequence}]',
                exc_info=True,
            )
            return False
        finally:
            for item in segments:
                self._offline_video_segment_events.setdefault(item.sequence, asyncio.Event()).set()
                self._offline_video_sequence_tasks.pop(item.sequence, None)

    async def __encodeContinuousVideoRun(
        self,
        segments: list[RecordedFMP4Segment],
        purpose: Literal['Offline', 'PlaybackPrefetch'],
    ) -> bool:
        """連続区間を1回の映像encodeへまとめ、完成後にHLS fragmentへ分割する。

        Args:
            segments: 同じ映像generationの連続セグメント。
            purpose: オフライン保存または通常再生の短い先読み。

        Returns:
            全fragmentを書けた場合はTrue。
        """

        first_segment = segments[0]
        total_duration = sum(item.duration for item in segments)
        split_times = self.__getVideoRunSplitTimes(segments)
        temporary_prefix = 'konomitv-bs4k-offline-video-' \
            if purpose == 'Offline' else 'konomitv-bs4k-playback-prefetch-video-'
        with tempfile.TemporaryDirectory(prefix=temporary_prefix) as temporary_directory:
            temporary_directory_path = Path(temporary_directory)
            encoded_path = temporary_directory_path / 'encoded.mp4'
            output_arguments = [
                '-movflags', '+frag_keyframe+delay_moov+default_base_moof+negative_cts_offsets',
                '-progress', 'pipe:1', '-nostats',
                '-f', 'mp4', str(encoded_path),
            ]
            plan = self.__buildVideoEncodeCommand(
                first_segment.start_time,
                total_duration,
                output_arguments,
                force_keyframe_times=split_times,
                sequence_for_warning=first_segment.sequence,
            )
            if plan is None:
                return False
            command, backend, device, encoder_pixel_format = plan
            return await self.__runContinuousVideoEncodeProcess(
                command,
                backend,
                device,
                encoder_pixel_format,
                temporary_directory_path,
                segments,
                encoded_path,
                split_times,
                purpose,
            )

    async def __runContinuousVideoEncodeProcess(
        self,
        command: list[str],
        backend: RecordedPlaybackEncoder,
        device: str | None,
        encoder_pixel_format: str,
        temporary_directory: Path,
        segments: list[RecordedFMP4Segment],
        encoded_path: Path,
        split_times: list[float],
        purpose: Literal['Offline', 'PlaybackPrefetch'],
    ) -> bool:
        """単一fMP4を生成してからstream copyで分割し、全fragmentをcacheへ書く。

        Args:
            command: 組み立て済みのFFmpegコマンド。
            backend: 実行する録画再生バックエンド。
            device: GPU render node。CPU encodeではNone。
            encoder_pixel_format: software decode再試行時のupload先pixel format。
            temporary_directory: segment muxerの出力先。
            segments: 書き込む映像セグメント。
            encoded_path: 一括エンコード結果の単一fMP4パス。
            split_times: 単一fMP4の先頭を0秒としたHLS分割境界。
            purpose: オフライン保存または通常再生の短い先読み。

        Returns:
            全fragmentを書けた場合はTrue。
        """

        semaphore_key = 'CPU' if backend == 'FFmpeg' else f'{backend}:{device or 0}'
        semaphore = self._cpu_semaphore if backend == 'FFmpeg' else self._gpu_semaphores.setdefault(
            semaphore_key,
            asyncio.Semaphore(1),
        )
        work_key = self.__getOfflineVideoWorkKey(segments[0].generation)
        total_duration = sum(segment.duration for segment in segments)
        async with self.acquireEncoderSlot(semaphore):
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=RecordedPlaybackBackend.getEnvironment(backend),
            )
            stderr, returncode = await self.__communicateSubprocessWithProgress(
                process,
                total_duration,
                lambda progress: self.__updateOfflineWorkProgress(work_key, progress * 0.95),
            )
            if (
                returncode != 0 and
                backend != 'FFmpeg' and
                self.recorded_program.recorded_video.container_format != 'MPEG-TS' and
                self.shouldRetryWithSoftwareDecode(stderr)
            ):
                # 同じencoder slot内で再試行し、semaphoreを二重取得しない。
                encoded_path.unlink(missing_ok=True)
                fallback_command = self.buildSoftwareDecodeFallback(command, backend, encoder_pixel_format)
                process = await asyncio.create_subprocess_exec(
                    *fallback_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=RecordedPlaybackBackend.getEnvironment(backend),
                )
                stderr, returncode = await self.__communicateSubprocessWithProgress(
                    process,
                    total_duration,
                    lambda progress: self.__updateOfflineWorkProgress(work_key, progress * 0.95),
                )
            if returncode != 0 or encoded_path.is_file() is False or encoded_path.stat().st_size == 0:
                logging.error(
                    f'[RecordedFMP4Stream] {purpose} continuous video encode produced no media. '
                    f'[recorded_video_id: {self.recorded_program.recorded_video.id}, '
                    f'start_sequence: {segments[0].sequence}, '
                    f'stderr: {stderr.decode(errors="ignore").strip()}]'
                )
                return False

        # エンコード完了後にstream copyでHLS fragmentへ分割する。再エンコードは行わない。
        output_pattern = str(temporary_directory / 'segment-%06d.mp4')
        split_command = [
            LIBRARY_PATH['FFmpeg8'], '-hide_banner', '-loglevel', 'error', '-y',
            '-i', str(encoded_path), '-map', '0:v:0', '-an', '-c:v', 'copy',
        ]
        if len(split_times) > 0:
            split_command += ['-segment_times', ','.join(f'{time:.6f}' for time in split_times)]
            split_command += [
                '-f', 'segment', '-segment_format', 'mp4',
                '-segment_format_options',
                'movflags=+frag_keyframe+delay_moov+default_base_moof+negative_cts_offsets',
                '-reset_timestamps', '0', '-progress', 'pipe:1', '-nostats', output_pattern,
            ]
        else:
            # segment muxerは境界未指定時に既定の2秒で分割するため、1本だけなら通常のMP4へ直接remuxする。
            split_command += [
                '-movflags', '+frag_keyframe+delay_moov+default_base_moof+negative_cts_offsets',
                '-progress', 'pipe:1', '-nostats',
                '-f', 'mp4', str(temporary_directory / 'segment-000000.mp4'),
            ]
        split_process = await asyncio.create_subprocess_exec(
            *split_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        split_stderr, split_returncode = await self.__communicateSubprocessWithProgress(
            split_process,
            total_duration,
            # 分割コマンドが成功しても本数・fragment検証が残るため、検証完了までは99%に留める。
            lambda progress: self.__updateOfflineWorkProgress(work_key, 0.95 + progress * 0.04),
        )
        output_paths = sorted(temporary_directory.glob('segment-*.mp4'))
        if split_returncode != 0 or len(output_paths) != len(segments):
            logging.error(
                f'[RecordedFMP4Stream] {purpose} continuous video stream-copy split failed. '
                f'[recorded_video_id: {self.recorded_program.recorded_video.id}, '
                f'start_sequence: {segments[0].sequence}, expected: {len(segments)}, '
                f'actual: {len(output_paths)}, stderr: {split_stderr.decode(errors="ignore").strip()}]'
            )
            return False

        # 完成済みファイルだけを順番に検証・atomic writeし、途中状態をcacheへ公開しない。
        for segment, output_path in zip(segments, output_paths, strict=True):
            encoded_data = await asyncio.to_thread(output_path.read_bytes)
            init_path = self.__buildCachePath(segment, is_init=True)
            segment_path = self.__buildCachePath(segment, is_init=False)
            await self.__acquire(init_path)
            await self.__acquire(segment_path)
            if await self.__storeEncodedVideoFragment(segment, encoded_data, init_path, segment_path) is False:
                logging.error(
                    f'[RecordedFMP4Stream] {purpose} continuous video fragment is incomplete. '
                    f'[recorded_video_id: {self.recorded_program.recorded_video.id}, '
                    f'sequence: {segment.sequence}]'
                )
                return False
            self._completed_sequences.add(segment.sequence)
            if purpose == 'Offline':
                self._offline_video_segment_events.setdefault(segment.sequence, asyncio.Event()).set()
        self.__updateOfflineWorkProgress(work_key, 1.0)
        return True

    async def __communicateSubprocessWithProgress(
        self,
        process: asyncio.subprocess.Process,
        duration: float,
        callback: Callable[[float], None],
    ) -> tuple[bytes, int]:
        """FFmpegのprogress出力を読みながら終了を待ち、キャンセル時は確実に回収する。

        Args:
            process: 実行中のFFmpeg。
            duration: このプロセスが生成する媒体時間。
            callback: 0～1の媒体時間進捗を受け取る同期コールバック。

        Returns:
            stderrと終了コード。
        """

        # loglevel errorでもstderr pipeが埋まるとFFmpegが止まるため、progressと並行してdrainする。
        stderr_task = asyncio.create_task(process.stderr.read()) if process.stderr is not None else None
        stderr = b''
        latest_progress = 0.0
        try:
            if process.stdout is not None:
                while True:
                    line = await process.stdout.readline()
                    if line == b'':
                        break
                    key, separator, value = line.decode(errors='ignore').strip().partition('=')
                    if separator == '' or key not in ('out_time_us', 'out_time_ms'):
                        continue
                    try:
                        out_time = int(value) / 1_000_000
                    except ValueError:
                        continue
                    if duration > 0:
                        latest_progress = max(latest_progress, min(1.0, out_time / duration))
                        callback(latest_progress)
            returncode = await process.wait()
            if returncode == 0:
                callback(1.0)
        except asyncio.CancelledError:
            if process.returncode is None:
                try:
                    process.kill()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    await process.wait()
                except (ProcessLookupError, OSError):
                    pass
            raise
        finally:
            if stderr_task is not None:
                stderr_result = await asyncio.gather(stderr_task, return_exceptions=True)
                if len(stderr_result) == 1 and isinstance(stderr_result[0], bytes):
                    stderr = stderr_result[0]
        return stderr, process.returncode if process.returncode is not None else 1

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
            'QSV': f'format={pixel_format},hwupload=extra_hw_frames=32',
            'NVENC': f'format={pixel_format},hwupload_cuda',
            'AMF': f'format={pixel_format},hwupload',
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
            if backend == 'QSV':
                return ['-profile:v', 'profile2' if bit_depth == 10 else 'profile0']
            return ['-profile:v', '2' if bit_depth == 10 else '0']
        if codec == 'av1':
            if backend == 'NVENC':
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
                self.__updateOfflineWorkProgress(
                    self.__getOfflineAudioWorkKey(
                        rendition.id,
                        self.__getTranscodedAudioGeneration(segment),
                    ),
                    1.0,
                )
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
        # 索引が不在を明示した区間だけ無音を生成する。存在を明示したTrackの抽出失敗まで
        # 無音へ置換すると、オフライン一括生成では残り全区間が成功扱いの無音になる。
        # タイムラインを持たない旧索引だけは、従来どおり実音声失敗時の無音fallbackを残す。
        source_attempts = [False] if availability is False else (
            [True] if availability is True else [True, False]
        )
        timings = [self.__getAudioSegmentTiming(item) for item in generation_segments]
        total_input_samples = sum(timing.sample_count for timing in timings)
        if total_input_samples <= 0:
            return False

        stderr = b''
        for use_recorded_audio in source_attempts:
            # 実音声失敗後の無音再試行で同じ一時パスを安全に置換できるよう上書きを明示する。
            command = [LIBRARY_PATH['FFmpeg8'], '-hide_banner', '-loglevel', 'error', '-y']
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
                source_input_arguments = BuildKonomiTVBS4KMMTTLVInputArguments(
                    self.recorded_program.recorded_video.container_format,
                )
                if input_seek > 0:
                    source_input_arguments += ['-ss', f'{input_seek:.6f}']
                if self.recorded_program.recorded_video.container_format == 'MPEG-TS':
                    # 新PIDの構成境界へexact seekしても、後続PMTとAAC packetまでprobeして
                    # PID指定mapを確定できるようにする。seek時刻自体は後ろへずらさないため、
                    # 境界先頭の音声sampleとPTSを失わず既存の厳密timeline検証へ渡せる。
                    source_input_arguments += [
                        '-probesize', str(self.AUDIO_MPEGTS_PROBE_SIZE_BYTES),
                        '-analyzeduration', str(self.AUDIO_MPEGTS_ANALYZE_DURATION_MICROSECONDS),
                    ]
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
                        LIBRARY_PATH['FFmpeg8'], '-hide_banner', '-loglevel', 'error', '-y',
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
                # 索引がTrack不在を明示した区間、または旧索引で入力Trackを確認できなかった場合は、
                # generation全体を同じcodec/layoutの無音で作る。一部segmentだけを差し替えると
                # encoder stateが切れて継ぎ目が生じるため禁止する。
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
            fragment_sample_counts = [timing.sample_count for timing in timings]
            fragment_sample_counts[0] += encoder_delay

            with tempfile.TemporaryDirectory(prefix='konomitv-bs4k-audio-generation-') as temporary_directory:
                temporary_directory_path = Path(temporary_directory)
                normalized_audio_path = temporary_directory_path / 'normalized-audio.nut'
                encoded_audio_path = temporary_directory_path / 'encoded-audio.mp4'
                work_key = self.__getOfflineAudioWorkKey(
                    rendition.id,
                    self.__getTranscodedAudioGeneration(first_segment),
                )
                total_duration = total_input_samples / self.AUDIO_SAMPLE_RATE
                normalization_share = 0.10 if normalization_command is not None else 0.0
                if normalization_command is not None:
                    assert expected_channel_layout is not None
                    normalization_command += [
                        '-progress', 'pipe:1', '-nostats',
                        str(normalized_audio_path),
                    ]
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
                command += [
                    '-initial_offset', f'{-encoder_delay / self.AUDIO_SAMPLE_RATE:.9f}',
                    '-movflags', '+frag_keyframe+delay_moov+default_base_moof+negative_cts_offsets',
                    '-progress', 'pipe:1', '-nostats',
                    '-f', 'mp4', str(encoded_audio_path),
                ]
                async with self.acquireEncoderSlot(self._cpu_semaphore):
                    if normalization_command is not None:
                        normalization_process = await asyncio.create_subprocess_exec(
                            *normalization_command,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        stderr, normalization_returncode = await self.__communicateSubprocessWithProgress(
                            normalization_process,
                            total_duration,
                            lambda progress: self.__updateOfflineWorkProgress(
                                work_key,
                                progress * normalization_share,
                            ),
                        )
                        if normalization_returncode != 0 or normalized_audio_path.is_file() is False:
                            log_message = (
                                '[RecordedFMP4Stream] FFmpeg 8 audio normalization source failed; ' +
                                ('retrying the entire generation with silence. ' if availability is None else
                                 'refusing to replace an indexed audio track with silence. ')
                            )
                            (logging.warning if availability is None else logging.error)(
                                log_message +
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
                    stderr, returncode = await self.__communicateSubprocessWithProgress(
                        process,
                        total_duration,
                        lambda progress: self.__updateOfflineWorkProgress(
                            work_key,
                            normalization_share + progress * (0.95 - normalization_share),
                        ),
                    )
                if returncode != 0 or encoded_audio_path.is_file() is False or encoded_audio_path.stat().st_size == 0:
                    if use_recorded_audio:
                        log_message = (
                            '[RecordedFMP4Stream] FFmpeg 8 audio generation source failed; ' +
                            ('retrying the entire generation with silence. ' if availability is None else
                             'refusing to replace an indexed audio track with silence. ')
                        )
                        (logging.warning if availability is None else logging.error)(
                            log_message +
                            f'[recorded_video_id: {self.recorded_program.recorded_video.id}, '
                            f'rendition: {rendition.id}, audio_generation: '
                            f'{self.__getTranscodedAudioGeneration(first_segment)}, '
                            f'stderr: {stderr.decode(errors="ignore").strip()}]'
                        )
                    continue

                # 一括生成したmoof/trun/mdatを直接解析し、計画境界に対応するpacket群へ分ける。
                # segment muxerへ再投入するとdurationが再量子化されるため、圧縮payloadはbyte単位で保持する。
                try:
                    encoded_data = await asyncio.to_thread(encoded_audio_path.read_bytes)
                    init_data, media_fragments = await asyncio.to_thread(
                        self.splitTranscodedAudioFMP4,
                        encoded_data,
                        fragment_sample_counts,
                    )
                except (OSError, ValueError) as ex:
                    logging.error(
                        '[RecordedFMP4Stream] FFmpeg 8 audio packet split failed. '
                        f'[recorded_video_id: {self.recorded_program.recorded_video.id}, '
                        f'rendition: {rendition.id}, audio_generation: '
                        f'{self.__getTranscodedAudioGeneration(first_segment)}, '
                        f'expected_segments: {len(generation_segments)}, error: {ex}]'
                    )
                    return False
                # 厳密timeline検証とatomic cache writeが完了するまでは100%にしない。
                self.__updateOfflineWorkProgress(work_key, 0.99)

            normalized_media_fragments: list[bytes] = []
            common_init_data = b''
            next_decode_sample = first_timing.start_sample
            is_valid = True

            def LogTimelineValidationFailure(
                media_data: bytes,
                sequence: int,
                phase: str,
                expected_start_sample: int,
                expected_sample_count: int,
            ) -> None:
                """音声fragment検証失敗時に予定値と解析可能な実測値を記録する。

                Args:
                    media_data: 検証に失敗したメディアfragment。
                    sequence: 対象HLS sequence。
                    phase: 正規化前後を識別する英語ラベル。
                    expected_start_sample: 期待する48kHz decode開始sample。
                    expected_sample_count: 期待する48kHz sample数。

                Returns:
                    None
                """

                actual_info = self.inspectAudioFragment(init_data, media_data)
                actual_start_sample = actual_info.first_decode_time if actual_info is not None else None
                actual_sample_count = actual_info.total_duration if actual_info is not None else None
                actual_packet_count = actual_info.sample_count if actual_info is not None else None
                expected_packet_count = math.ceil(expected_sample_count / frame_samples)
                logging.error(
                    '[RecordedFMP4Stream] FFmpeg 8 audio fragment timeline validation failed. '
                    f'[recorded_video_id: {self.recorded_program.recorded_video.id}, '
                    f'rendition: {rendition.id}, audio_generation: '
                    f'{self.__getTranscodedAudioGeneration(first_segment)}, sequence: {sequence}, '
                    f'phase: {phase}, expected_start_sample: {expected_start_sample}, '
                    f'actual_start_sample: {actual_start_sample}, '
                    f'expected_sample_count: {expected_sample_count}, actual_sample_count: {actual_sample_count}, '
                    f'expected_packet_count: {expected_packet_count}, actual_packet_count: {actual_packet_count}]'
                )

            for index, (generation_segment, media_data) in enumerate(zip(
                generation_segments,
                media_fragments,
                strict=True,
            )):
                expected_duration = fragment_sample_counts[index]
                if self.validateTranscodedAudioFragmentTimeline(
                    init_data,
                    media_data,
                    audio_codec,
                    expected_start_sample=0,
                    expected_sample_count=expected_duration,
                    expected_channel_count=expected_channel_count,
                    validate_packet_timeline=False,
                ) is False:
                    LogTimelineValidationFailure(
                        media_data,
                        generation_segment.sequence,
                        'BeforeNormalization',
                        0,
                        expected_duration,
                    )
                    is_valid = False
                    break
                try:
                    normalized_media = self.normalizeFragmentTimeline(
                        init_data,
                        media_data,
                        next_decode_sample / self.AUDIO_SAMPLE_RATE,
                        generation_segment.sequence,
                    )
                except ValueError as ex:
                    logging.error(
                        '[RecordedFMP4Stream] FFmpeg 8 audio fragment timeline normalization failed. '
                        f'[recorded_video_id: {self.recorded_program.recorded_video.id}, '
                        f'rendition: {rendition.id}, audio_generation: '
                        f'{self.__getTranscodedAudioGeneration(first_segment)}, '
                        f'sequence: {generation_segment.sequence}, '
                        f'expected_start_sample: {next_decode_sample}, error: {ex}]'
                    )
                    is_valid = False
                    break
                if self.validateTranscodedAudioFragmentTimeline(
                    init_data,
                    normalized_media,
                    audio_codec,
                    expected_start_sample=next_decode_sample,
                    expected_sample_count=expected_duration,
                    expected_channel_count=expected_channel_count,
                    validate_packet_timeline=False,
                ) is False:
                    LogTimelineValidationFailure(
                        normalized_media,
                        generation_segment.sequence,
                        'AfterNormalization',
                        next_decode_sample,
                        expected_duration,
                    )
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
                normalized_media_fragments.append(normalized_media)
                next_decode_sample += expected_duration

            # MP4 muxer の 959/961 samples のような丸め補償は、間に960-sample packetを挟んだり
            # HLS fragment境界をまたいだりすることがある。各fragmentの開始・総時間は上で厳密に
            # 確認し、packet境界の累積丸め誤差と最終partial packetはgeneration全体で検証する。
            expected_generation_sample_count = sum(fragment_sample_counts)
            for phase, fragments in (
                ('BeforeNormalization', media_fragments),
                ('AfterNormalization', normalized_media_fragments),
            ):
                if is_valid is False:
                    break
                if self.validateTranscodedAudioGenerationPacketTimeline(
                    init_data,
                    fragments,
                    audio_codec,
                    expected_generation_sample_count,
                ) is True:
                    continue
                fragment_infos = [self.inspectAudioFragment(init_data, fragment) for fragment in fragments]
                actual_generation_sample_count = sum(
                    info.total_duration for info in fragment_infos if info is not None
                )
                actual_generation_packet_count = sum(
                    info.sample_count for info in fragment_infos if info is not None
                )
                expected_generation_packet_count = math.ceil(expected_generation_sample_count / frame_samples)
                duration_anomalies: list[str] = []
                for generation_segment, info in zip(generation_segments, fragment_infos, strict=True):
                    if info is None:
                        duration_anomalies.append(f'{generation_segment.sequence}:Unparseable')
                        continue
                    for packet_index, duration in enumerate(info.sample_durations):
                        if duration != frame_samples:
                            duration_anomalies.append(
                                f'{generation_segment.sequence}:{packet_index}:{duration}'
                            )
                            if len(duration_anomalies) >= 8:
                                break
                    if len(duration_anomalies) >= 8:
                        break
                logging.error(
                    '[RecordedFMP4Stream] FFmpeg 8 audio generation packet timeline validation failed. '
                    f'[recorded_video_id: {self.recorded_program.recorded_video.id}, '
                    f'rendition: {rendition.id}, audio_generation: '
                    f'{self.__getTranscodedAudioGeneration(first_segment)}, '
                    f'start_sequence: {generation_segments[0].sequence}, '
                    f'end_sequence: {generation_segments[-1].sequence}, phase: {phase}, '
                    f'expected_sample_count: {expected_generation_sample_count}, '
                    f'actual_sample_count: {actual_generation_sample_count}, '
                    f'expected_packet_count: {expected_generation_packet_count}, '
                    f'actual_packet_count: {actual_generation_packet_count}, '
                    f'duration_anomalies: {duration_anomalies}]'
                )
                is_valid = False

            if (
                is_valid and
                len(common_init_data) > 0 and
                len(normalized_media_fragments) == len(generation_segments)
            ):
                await RecordedFMP4CacheManager.writeAtomic(init_path, common_init_data)
                for generation_segment, media_data in zip(
                    generation_segments,
                    normalized_media_fragments,
                    strict=True,
                ):
                    await RecordedFMP4CacheManager.writeAtomic(segment_paths[generation_segment.sequence], media_data)
                self.__updateOfflineWorkProgress(work_key, 1.0)
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
            delivery_mode = 'Offline' if self._is_offline_continuous is True else 'Playback',
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
            delivery_mode = 'Offline' if self._is_offline_continuous is True else 'Playback',
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

    async def __acquire(
        self,
        cache_path: Path,
    ) -> KonomiTVBS4KRecordedFMP4PathLock:
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
    def __buildMP4Box(box_type: bytes, payload: bytes) -> bytes:
        """32bit sizeを持つISO BMFF boxを構築する。

        Args:
            box_type: 4byteのbox type。
            payload: box header直後へ格納するpayload。

        Returns:
            size、type、payloadを連結したbox。

        Raises:
            ValueError: box typeまたは32bit box sizeの条件を満たさない場合。
        """

        if len(box_type) != 4:
            raise ValueError('The ISO BMFF box type must be exactly four bytes.')
        box_size = 8 + len(payload)
        if box_size >= 1 << 32:
            raise ValueError('The ISO BMFF box exceeds the 32-bit box size field.')
        return box_size.to_bytes(4, 'big') + box_type + payload

    @classmethod
    def splitTranscodedAudioFMP4(
        cls,
        data: bytes,
        segment_sample_counts: list[int],
    ) -> tuple[bytes, list[bytes]]:
        """単一fragmentの音声fMP4を圧縮packet境界で複数fragmentへ厳密に分割する。

        Args:
            data: FFmpegが一括生成した自己完結音声fMP4。
            segment_sample_counts: codec delayを含めた各出力fragmentの48kHz sample数。

        Returns:
            共通初期化セグメントと、入力packetをそのまま保持したメディアfragment列。

        Raises:
            ValueError: 未対応box構造、矛盾したsample情報、packet途中の境界を検出した場合。
        """

        # 空の出力や0 sampleのfragmentはHLS側で表現できないため、box解析より先に拒否する。
        if len(segment_sample_counts) == 0 or any(sample_count <= 0 for sample_count in segment_sample_counts):
            raise ValueError('The audio fMP4 split plan must contain only positive sample counts.')

        # この処理は自身が起動したFFmpegの既知出力だけを対象にする。将来movencの構造が
        # 変わった場合にpacketを誤対応させないよう、既知のmfra以外を暗黙に読み飛ばさない。
        top_level_boxes = list(cls.__iterateMP4Boxes(data, 0, len(data)))
        top_level_types = tuple(box_type for _, _, _, box_type in top_level_boxes)
        if top_level_types not in (
            (b'ftyp', b'moov', b'moof', b'mdat'),
            (b'ftyp', b'moov', b'moof', b'mdat', b'mfra'),
        ):
            raise ValueError(
                'The audio fMP4 must contain only ftyp, moov, one moof/mdat pair, and optional mfra boxes.'
            )
        ftyp_box, moov_box, moof_box, mdat_box = top_level_boxes[:4]
        ftyp_offset, ftyp_size, _, _ = ftyp_box
        moov_offset, moov_size, _, _ = moov_box
        moof_offset, moof_size, moof_header_size, _ = moof_box
        mdat_offset, mdat_size, mdat_header_size, _ = mdat_box
        init_data = data[ftyp_offset:ftyp_offset + ftyp_size] + data[moov_offset:moov_offset + moov_size]
        mdat_payload_offset = mdat_offset + mdat_header_size
        mdat_payload_end = mdat_offset + mdat_size
        mdat_payload = data[mdat_payload_offset:mdat_payload_end]

        # 音声専用出力ではmoof直下をmfhd + traf、traf直下をtfhd + tfdt + trunへ限定する。
        # 複数track、複数run、暗号化補助boxを誤って単一mdatへ対応付けることを防ぐ。
        moof_children = list(cls.__iterateMP4Boxes(
            data,
            moof_offset + moof_header_size,
            moof_offset + moof_size,
        ))
        if tuple(box_type for _, _, _, box_type in moof_children) != (b'mfhd', b'traf'):
            raise ValueError('The audio fMP4 moof must contain exactly one mfhd and one traf box.')
        mfhd_offset, mfhd_size, mfhd_header_size, _ = moof_children[0]
        traf_offset, traf_size, traf_header_size, _ = moof_children[1]
        if mfhd_size != mfhd_header_size + 8:
            raise ValueError('The audio fMP4 contains an invalid mfhd box.')
        mfhd_payload_offset = mfhd_offset + mfhd_header_size
        if data[mfhd_payload_offset:mfhd_payload_offset + 4] != b'\x00\x00\x00\x00':
            raise ValueError('The audio fMP4 contains an unsupported mfhd version or flags.')
        mfhd_data = data[mfhd_offset:mfhd_offset + mfhd_size]

        traf_children = list(cls.__iterateMP4Boxes(
            data,
            traf_offset + traf_header_size,
            traf_offset + traf_size,
        ))
        if tuple(box_type for _, _, _, box_type in traf_children) != (b'tfhd', b'tfdt', b'trun'):
            raise ValueError('The audio fMP4 traf must contain exactly one tfhd, tfdt, and trun box.')
        tfhd_offset, tfhd_size, tfhd_header_size, _ = traf_children[0]
        tfdt_offset, tfdt_size, tfdt_header_size, _ = traf_children[1]
        trun_offset, trun_size, trun_header_size, _ = traf_children[2]

        # tfhdの既定duration/sizeは、movencが全packetで同じ値を省略した場合に必要になる。
        # base-data-offsetやduration-is-emptyは、この分割処理の単一mdat契約と両立しない。
        tfhd_payload_offset = tfhd_offset + tfhd_header_size
        tfhd_end = tfhd_offset + tfhd_size
        if tfhd_payload_offset + 8 > tfhd_end or data[tfhd_payload_offset] != 0:
            raise ValueError('The audio fMP4 contains an invalid tfhd box.')
        tfhd_flags = int.from_bytes(data[tfhd_payload_offset + 1:tfhd_payload_offset + 4], 'big')
        known_tfhd_flags = 0x000001 | 0x000002 | 0x000008 | 0x000010 | 0x000020 | 0x010000 | 0x020000
        if (
            tfhd_flags & ~known_tfhd_flags != 0 or
            tfhd_flags & 0x000001 != 0 or
            tfhd_flags & 0x010000 != 0 or
            tfhd_flags & 0x020000 == 0
        ):
            raise ValueError('The audio fMP4 tfhd must use default-base-is-moof without an explicit base offset.')
        tfhd_cursor = tfhd_payload_offset + 8
        if tfhd_flags & 0x000002:
            tfhd_cursor += 4
        default_sample_duration: int | None = None
        if tfhd_flags & 0x000008:
            if tfhd_cursor + 4 > tfhd_end:
                raise ValueError('The audio fMP4 contains a truncated tfhd default sample duration.')
            default_sample_duration = int.from_bytes(data[tfhd_cursor:tfhd_cursor + 4], 'big')
            tfhd_cursor += 4
        default_sample_size: int | None = None
        if tfhd_flags & 0x000010:
            if tfhd_cursor + 4 > tfhd_end:
                raise ValueError('The audio fMP4 contains a truncated tfhd default sample size.')
            default_sample_size = int.from_bytes(data[tfhd_cursor:tfhd_cursor + 4], 'big')
            tfhd_cursor += 4
        if tfhd_flags & 0x000020:
            tfhd_cursor += 4
        if tfhd_cursor != tfhd_end:
            raise ValueError('The audio fMP4 tfhd fields do not match its box size.')
        tfhd_data = data[tfhd_offset:tfhd_offset + tfhd_size]

        # 各出力fragmentは既存normalizeFragmentTimeline()へ渡すため、元tfdtが0起点の
        # 一括生成物であることを確認し、同じversionの0起点tfdtを後で再構築する。
        tfdt_payload_offset = tfdt_offset + tfdt_header_size
        tfdt_end = tfdt_offset + tfdt_size
        if tfdt_payload_offset + 8 > tfdt_end:
            raise ValueError('The audio fMP4 contains a truncated tfdt box.')
        tfdt_version = data[tfdt_payload_offset]
        tfdt_flags = int.from_bytes(data[tfdt_payload_offset + 1:tfdt_payload_offset + 4], 'big')
        tfdt_decode_time_size = 8 if tfdt_version == 1 else 4
        if tfdt_version not in (0, 1) or tfdt_flags != 0 or tfdt_payload_offset + 4 + tfdt_decode_time_size != tfdt_end:
            raise ValueError('The audio fMP4 contains an unsupported tfdt version, flags, or size.')
        source_decode_time = int.from_bytes(
            data[tfdt_payload_offset + 4:tfdt_payload_offset + 4 + tfdt_decode_time_size],
            'big',
        )
        if source_decode_time != 0:
            raise ValueError('The audio fMP4 source tfdt must start at decode time zero.')
        tfdt_fullbox = data[tfdt_payload_offset:tfdt_payload_offset + 4]

        # trunの各entryをduration/sizeと対応付ける。entryのflagsやcomposition offsetは
        # 解釈を変えずraw bytesのまま保持し、sample_countとdata_offsetだけを再構築する。
        trun_payload_offset = trun_offset + trun_header_size
        trun_end = trun_offset + trun_size
        if trun_payload_offset + 8 > trun_end:
            raise ValueError('The audio fMP4 contains a truncated trun box.')
        trun_version = data[trun_payload_offset]
        trun_flags = int.from_bytes(data[trun_payload_offset + 1:trun_payload_offset + 4], 'big')
        known_trun_flags = 0x000001 | 0x000004 | 0x000100 | 0x000200 | 0x000400 | 0x000800
        if trun_version not in (0, 1) or trun_flags & ~known_trun_flags != 0:
            raise ValueError('The audio fMP4 contains unsupported trun version or flags.')
        if trun_flags & 0x000001 == 0:
            raise ValueError('The audio fMP4 trun must contain a data_offset field.')
        # first_sample_flagsを各分割先へ複製すると2本目以降の意味が変わるため、未知構造として拒否する。
        if trun_flags & 0x000004:
            raise ValueError('The audio fMP4 trun first_sample_flags field cannot be split safely.')
        sample_count = int.from_bytes(data[trun_payload_offset + 4:trun_payload_offset + 8], 'big')
        if sample_count == 0 or sample_count > len(mdat_payload):
            raise ValueError('The audio fMP4 trun contains an invalid sample count.')
        trun_cursor = trun_payload_offset + 8
        source_data_offset = int.from_bytes(data[trun_cursor:trun_cursor + 4], 'big', signed=True)
        trun_cursor += 4
        expected_source_data_offset = mdat_payload_offset - moof_offset
        if source_data_offset != expected_source_data_offset:
            raise ValueError('The audio fMP4 trun data_offset does not point to the mdat payload.')

        packets: list[RecordedAudioPacket] = []
        packet_payload_offset = 0
        for packet_index in range(sample_count):
            entry_offset = trun_cursor
            duration = default_sample_duration
            if trun_flags & 0x000100:
                if trun_cursor + 4 > trun_end:
                    raise ValueError(f'The audio fMP4 trun duration is truncated at packet {packet_index}.')
                duration = int.from_bytes(data[trun_cursor:trun_cursor + 4], 'big')
                trun_cursor += 4
            size = default_sample_size
            if trun_flags & 0x000200:
                if trun_cursor + 4 > trun_end:
                    raise ValueError(f'The audio fMP4 trun size is truncated at packet {packet_index}.')
                size = int.from_bytes(data[trun_cursor:trun_cursor + 4], 'big')
                trun_cursor += 4
            if trun_flags & 0x000400:
                trun_cursor += 4
            if trun_flags & 0x000800:
                trun_cursor += 4
            if trun_cursor > trun_end:
                raise ValueError(f'The audio fMP4 trun entry is truncated at packet {packet_index}.')
            if duration is None or duration <= 0 or size is None or size <= 0:
                raise ValueError(f'The audio fMP4 packet {packet_index} has no positive duration or size.')
            packets.append(RecordedAudioPacket(
                duration=duration,
                size=size,
                payload_offset=packet_payload_offset,
                trun_entry=data[entry_offset:trun_cursor],
            ))
            packet_payload_offset += size
        if trun_cursor != trun_end:
            raise ValueError('The audio fMP4 trun fields do not match its box size.')
        if packet_payload_offset != len(mdat_payload):
            raise ValueError(
                'The audio fMP4 packet sizes do not exactly cover the mdat payload. '
                f'expected: {packet_payload_offset}, actual: {len(mdat_payload)}.'
            )

        # 計画境界をpacket durationの累積値と突き合わせる。1 sampleでもpacket途中へ入る
        # 境界は、圧縮payloadを再エンコードなしで分割できないため即座に失敗させる。
        expected_total_duration = sum(segment_sample_counts)
        actual_total_duration = sum(packet.duration for packet in packets)
        if expected_total_duration != actual_total_duration:
            raise ValueError(
                'The audio fMP4 split plan does not match the packet timeline. '
                f'expected: {expected_total_duration}, actual: {actual_total_duration}.'
            )
        packet_ranges: list[tuple[int, int]] = []
        packet_index = 0
        decode_time = 0
        for segment_index, segment_sample_count in enumerate(segment_sample_counts):
            segment_packet_start = packet_index
            expected_decode_end = decode_time + segment_sample_count
            while decode_time < expected_decode_end and packet_index < len(packets):
                decode_time += packets[packet_index].duration
                packet_index += 1
            if decode_time != expected_decode_end:
                raise ValueError(
                    f'The audio fMP4 boundary for segment {segment_index} falls inside a packet. '
                    f'expected: {expected_decode_end}, actual: {decode_time}.'
                )
            if packet_index == segment_packet_start:
                raise ValueError(f'The audio fMP4 segment {segment_index} contains no packets.')
            packet_ranges.append((segment_packet_start, packet_index))
        if packet_index != len(packets):
            raise ValueError('The audio fMP4 split plan leaves unassigned packets.')

        # 各fragmentではtfdtを0へ戻し、後段の既存normalizeFragmentTimeline()が録画先頭基準の
        # tfdtとmfhd sequenceを設定する。mdat payloadは元packetのbyte列を一切変更せずコピーする。
        media_fragments: list[bytes] = []
        for packet_start, packet_end in packet_ranges:
            segment_packets = packets[packet_start:packet_end]
            trun_entries = b''.join(packet.trun_entry for packet in segment_packets)
            tfdt_data = cls.__buildMP4Box(
                b'tfdt',
                tfdt_fullbox + bytes(tfdt_decode_time_size),
            )

            def BuildMoof(data_offset: int) -> bytes:
                """新しいtrun data_offsetを持つ単一音声moofを構築する。

                Args:
                    data_offset: moof先頭からmdat payload先頭までの相対位置。

                Returns:
                    box sizeを再計算したmoof。
                """

                trun_data = cls.__buildMP4Box(
                    b'trun',
                    data[trun_payload_offset:trun_payload_offset + 4] +
                    len(segment_packets).to_bytes(4, 'big') +
                    data_offset.to_bytes(4, 'big', signed=True) +
                    trun_entries,
                )
                traf_data = cls.__buildMP4Box(b'traf', tfhd_data + tfdt_data + trun_data)
                return cls.__buildMP4Box(b'moof', mfhd_data + traf_data)

            provisional_moof = BuildMoof(0)
            output_data_offset = len(provisional_moof) + 8
            if output_data_offset >= 1 << 31:
                raise ValueError('The audio fMP4 output data_offset exceeds the signed 32-bit field.')
            output_moof = BuildMoof(output_data_offset)
            if len(output_moof) + 8 != output_data_offset:
                raise ValueError('The audio fMP4 output moof size changed while rebuilding data_offset.')
            payload_start = segment_packets[0].payload_offset
            payload_end = segment_packets[-1].payload_offset + segment_packets[-1].size
            output_mdat = cls.__buildMP4Box(b'mdat', mdat_payload[payload_start:payload_end])
            media_fragments.append(output_moof + output_mdat)

        return init_data, media_fragments

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
            init_data: 一括生成した音声fMP4から分離した共通初期化セグメント。
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
        validate_packet_timeline: bool = True,
    ) -> bool:
        """AAC/Opus変換fragmentが48kHzの予定timelineを連続して覆うか判定する。

        Args:
            init_data: codec構成とtimescaleを含む初期化セグメント。
            media_data: 検証する単一メディアfragment。
            audio_codec: 期待する音声codec。
            expected_start_sample: 期待するdecode開始sample。
            expected_sample_count: 期待するfragment総sample数。
            expected_channel_count: 期待するチャンネル数。Noneなら検査しない。
            validate_packet_timeline: packet数・duration配列までfragment単体で完結検証する場合はTrue。

        Returns:
            開始・総時間・codec構成と、要求時はpacket timelineも一致する場合はTrue。
        """

        info = cls.inspectAudioFragment(init_data, media_data)
        expected_codec = 'mp4a.40.2' if audio_codec == 'aac' else 'opus'
        frame_samples = cls.AAC_PACKET_SAMPLES if audio_codec == 'aac' else 960
        if info is None or not (
            cls.extractAudioCodecString(init_data) == expected_codec and
            (
                expected_channel_count is None or
                cls.extractAudioInitializationChannelCount(init_data, audio_codec) == expected_channel_count
            ) and
            info.timescale == cls.AUDIO_SAMPLE_RATE and
            info.first_decode_time == expected_start_sample and
            info.total_duration == expected_sample_count and
            info.sample_count > 0
        ):
            return False
        if validate_packet_timeline is False:
            return True

        return cls.__validateTranscodedAudioPacketDurations(
            info.sample_durations,
            expected_sample_count,
            frame_samples,
        )

    @staticmethod
    def __validateTranscodedAudioPacketDurations(
        sample_durations: tuple[int, ...],
        expected_sample_count: int,
        frame_samples: int,
    ) -> bool:
        """連結済みpacket durationの累積丸め誤差と最終partialを厳密に検証する。

        Args:
            sample_durations: decode順のpacket duration。
            expected_sample_count: 全packetが覆うべき総sample数。
            frame_samples: codecの通常packet sample数。

        Returns:
            packet数・総時間・各境界の丸め誤差が厳密なcodec packet列ならTrue。
        """

        if expected_sample_count <= 0 or frame_samples <= 0:
            return False
        expected_packet_count = math.ceil(expected_sample_count / frame_samples)
        if len(sample_durations) != expected_packet_count or sum(sample_durations) != expected_sample_count:
            return False
        expected_last_duration = expected_sample_count % frame_samples or frame_samples

        # FFmpegのMP4 muxerはpacket境界時刻をTrack timebaseへ丸めるため、通常durationから
        # ±1 sampleのpacketが生じる。丸められた各境界は理想境界から最大1 sampleしか離れず、
        # 959/960/961のように通常packetを挟んで補償される場合もある。したがって個々の差を
        # 隣接ペアとして扱わず、先頭からの累積差が常に±1 sample以内で最後に0へ戻ることを
        # 検証する。これなら正当な量子化だけを許容し、±2 sampleのpacketや累積driftを拒否できる。
        # 末尾partial packetは丸め補償の対象外として、計画した残りsample数との完全一致を要求する。
        if expected_last_duration != frame_samples and sample_durations[-1] != expected_last_duration:
            return False
        full_frame_durations = sample_durations[:-1] \
            if expected_last_duration != frame_samples else sample_durations
        cumulative_rounding_error = 0
        for duration in full_frame_durations:
            rounding_error = duration - frame_samples
            if rounding_error not in (-1, 0, 1):
                return False
            cumulative_rounding_error += rounding_error
            if abs(cumulative_rounding_error) > 1:
                return False

        return cumulative_rounding_error == 0

    @classmethod
    def validateTranscodedAudioGenerationPacketTimeline(
        cls,
        init_data: bytes,
        media_fragments: list[bytes],
        audio_codec: Literal['aac', 'opus'],
        expected_sample_count: int,
    ) -> bool:
        """fragment境界を越えて連結したAAC/Opus packet timelineを厳密に検証する。

        Args:
            init_data: 全fragmentで共有する初期化セグメント。
            media_fragments: generationのdecode順に並んだメディアfragment。
            audio_codec: 期待する音声codec。
            expected_sample_count: generation全体の期待sample数。

        Returns:
            fragment境界をまたぐ丸め補償を含め、packet列全体が正しい場合はTrue。
        """

        if len(media_fragments) == 0:
            return False
        sample_durations: list[int] = []
        for media_data in media_fragments:
            info = cls.inspectAudioFragment(init_data, media_data)
            if info is None or info.timescale != cls.AUDIO_SAMPLE_RATE:
                return False
            sample_durations.extend(info.sample_durations)
        frame_samples = cls.AAC_PACKET_SAMPLES if audio_codec == 'aac' else 960
        return cls.__validateTranscodedAudioPacketDurations(
            tuple(sample_durations),
            expected_sample_count,
            frame_samples,
        )

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
