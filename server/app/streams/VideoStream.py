
# Type Hints を指定できるように
# ref: https://stackoverflow.com/a/33533514/17124142
from __future__ import annotations

import asyncio
import bisect
import math
import signal
import tempfile
import time
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from biim.mpeg2ts import ts
from fastapi import HTTPException, status
from tortoise import transactions

from app import logging
from app.config import Config
from app.constants import LIBRARY_PATH, QUALITY, QUALITY_TYPES, RECORDED_SUBTITLES_DIR
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedVideo import RecordedVideo
from app.schemas import KeyFrame, SegmentMapEntry
from app.streams.StreamEncodingOptions import StreamEncodingOptions
from app.streams.RecordedEncodingCodecs import getAudioCodecDefinition, getVideoCodecDefinition
from app.streams.VideoEncodingTask import VideoEncodingTask
from app.streams.VideoSegmentPlanner import VideoSegmentPlanner
from app.utils import SetTimeout
from app.utils.MP4KeyFrameParser import MP4KeyFrameParser
from app.utils.TSKeyFrameSeeker import TSKeyFrameSeeker, TSStreamInfo


@dataclass
class VideoStreamSegment:
    """
    ビデオストリームの HLS セグメントを表すデータクラス
    """

    # HLS セグメントのシーケンス番号
    ## リストのインデックスと一致する (0 から始まるので注意)
    sequence_index: int
    # HLS プレイリスト上の開始時刻 (秒)
    ## 入力ソース側の DTS とは別物で、仮想プレイリストを等間隔で作るための再生時刻
    playlist_start_seconds: float
    # エンコードを開始する入力ファイルの位置 (バイト)
    ## TS コンテナはオンデマンド探索または segment_map で解決し、MP4 は psisimux に時刻を渡すため None のまま扱う
    source_file_position: int | None
    # エンコードを開始する入力ソース側の DTS (90kHz)
    ## プレイリスト上の開始時刻以前で最も近いキーフレーム DTS が入り、未解決の間は None
    source_start_dts: int | None
    # HLS セグメント長 (秒単位)
    ## プレイリストはフレームレートから算出した固定長で作り、実際の入力開始位置は再生時に別途解決する
    duration_seconds: float
    # HLS セグメントのエンコードの状態
    encode_status: Literal['Pending', 'Encoding', 'Completed']
    # HLS セグメントのエンコード済み MPEG-TS データが返る asyncio.Future
    encoded_segment_ts_future: asyncio.Future[bytes]
    # HLS セグメントのエンコード済み MPEG-TS データが既にクライアントによって読み取られているかを表すフラグ
    ## このフラグが True の VideoStreamSegment は、メモリ節約のため順に破棄される (readed と意図的に過去形にしている)
    is_encoded_segment_ts_future_readed: bool = False

    async def resetState(self) -> None:
        """
        このセグメントの状態をリセットする
        リセットすると保持されている asyncio.Future は初期化され、ステータスも Pending に戻る
        実行中のイベントループ上でのみ呼び出される前提のため、呼び出し側でもその制約が見えるよう敢えて async def で定義している
        """
        if not self.encoded_segment_ts_future.done():
            self.encoded_segment_ts_future.set_result(b'')  # 前の Future がまだ完了していない場合は空のデータで完了させる
        self.encode_status = 'Pending'

        # 新しい Future は、現在実行中のイベントループに明示的に紐付けて再初期化する
        ## これにより「イベントループ上で呼び出す前提」がコード上でも明確になる
        self.encoded_segment_ts_future = asyncio.get_running_loop().create_future()
        self.is_encoded_segment_ts_future_readed = False


class VideoStream:
    """ 録画視聴セッションを管理するクラス """

    # 録画視聴セッションが再生されていない場合にタイムアウトするまでの時間 (秒)
    # この時間が経過すると、録画視聴セッションのインスタンスは自動的に破棄される
    # 初回セグメントのキーフレーム探索・HWエンコーダー起動が10秒を超える録画でも、
    # プレイヤーの定期Keep-Aliveが始まる前にセッションを破棄しない。
    SESSION_TIMEOUT: ClassVar[float] = float(30)

    # 一度でも読み取られた HLS セグメントの最大保持数
    MAX_READED_SEGMENTS: ClassVar[int] = 10

    # QSVEncC でエンコードを開始する際、入力 DTS が 33bit ラップアラウンド直前だと時刻補正でフレーム間隔がズレる問題を回避するための余裕
    DTS_WRAP_AVOIDANCE_SECONDS: ClassVar[int] = 60

    # DTS ラップ回避で遡る最大セグメント数
    ## 通常は 10 セグメント前後で抜けるが、万が一 segment_map が壊れている場合に無制限にソース位置解決が走る事態を防ぐ
    DTS_WRAP_AVOIDANCE_MAX_BACKTRACK_SEGMENTS: ClassVar[int] = 40

    # 再生しながら見つけたキーフレーム位置を segment_map として保存する最小件数
    ## DB 書き込みを HLS セグメントごとに発生させず、再生済み範囲をある程度まとめて保存する
    SEGMENT_MAP_SAVE_BATCH_SIZE: ClassVar[int] = 16

    # 音声FFmpegを再生要求より何セグメント先まで走らせるか。超えたらSIGSTOPで一時停止する。
    AUDIO_PREFETCH_SEGMENTS: ClassVar[int] = 3

    # 録画視聴セッションのインスタンスが入る、セッション ID をキーとした辞書
    # この辞書に録画視聴セッションに関する全てのデータが格納されている
    __instances: ClassVar[dict[str, VideoStream]] = {}

    # recorded_videos.segment_map の JSON 更新を録画ファイル単位で直列化するロック
    ## SQLite の select_for_update() だけでは同一プロセス内の別視聴セッション同士の読み直し競合を十分に避けられない
    ## ロックを使い終えた録画 ID は自動的に辞書から消し、長期稼働時に録画 ID 分だけ残り続ける状態を避ける
    __segment_map_save_locks: ClassVar[weakref.WeakValueDictionary[int, asyncio.Lock]] = weakref.WeakValueDictionary()


    # 必ずセッション ID ごとに1つのインスタンスになるように (Singleton)
    def __new__(
        cls,
        session_id: str,
        recorded_program: RecordedProgram,
        quality: QUALITY_TYPES,
        encoding_options: StreamEncodingOptions | None = None,
        is_new_session_allowed: bool = False,
    ) -> VideoStream:

        # まだ同じセッション ID のインスタンスがないときだけ、インスタンスを生成する
        if session_id not in cls.__instances:

            # 録画視聴セッションは、プレイリスト API での初回作成時だけ新規作成を許可する
            ## セグメント取得や Keep-Alive が未知の session_id を指定した場合に、初期化情報の欠けたセッションを作らない
            if is_new_session_allowed is False or encoding_options is None:
                # まだ VideoStream インスタンスが存在しないため、ここだけは要求された品質からログの接頭辞を組み立てる
                ## 追加オプションが渡されている場合は、セッション不在のエラーログにも同じ品質文字列を出す
                requested_encoding_options = encoding_options or StreamEncodingOptions()
                logging.error(
                    f'[Video: {recorded_program.id}/{session_id}/{quality}{requested_encoding_options.buildSuffix()}] '
                    f'Session does not exist.'
                )
                raise HTTPException(
                    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail = 'Session does not exist',
                )

            # 新しい録画視聴セッションのインスタンスを生成する
            instance = super().__new__(cls)

            # セッション ID を設定
            instance.session_id = session_id

            # 録画番組の情報と映像の品質を設定
            instance.recorded_program = recorded_program
            instance.quality = quality
            instance.encoding_options = encoding_options

            # HLS セグメントの基準長 (秒)
            ## 録画ファイルのフレームレートから算出し、プレイリスト生成とオンデマンド探索で共通利用する
            instance._segment_duration_seconds = VideoSegmentPlanner.computeSegmentDurationSeconds(
                recorded_program.recorded_video.video_frame_rate or 0,
            )

            # HLS セグメントを格納するリスト
            instance._segments = []
            instance._audio_segment_cache = {}
            instance._audio_init_cache = {}
            instance._video_only_segment_cache = {}
            instance._muxed_segment_cache = {}
            instance._video_only_segment_locks = {}
            instance._audio_segment_locks = {}
            instance._audio_segment_futures = {}
            instance._audio_encoding_task_refs = {}
            instance._audio_encoding_processes = {}
            instance._audio_requested_sequences = {}
            instance._opus_encoding_lock = asyncio.Lock()
            instance._opus_encoding_process = None
            instance._opus_encoding_task_ref = None
            instance._subtitle_cache = {}
            instance._subtitle_locks = {}

            # segment_map はシーケンス番号で参照するため、視聴セッション内では辞書として保持する
            ## DB には JSON 配列のまま保存し、検索時だけ辞書化することで保存形式を増やさずに参照コストを下げる
            instance._segment_map_by_sequence = {
                entry['sequence_index']: entry
                for entry in recorded_program.recorded_video.segment_map
            }

            # 入力ソース位置のオンデマンド解決で使うキャッシュ
            ## TS コンテナでは PAT/PMT から得た PID 情報と先頭 DTS を、MP4 では moov 由来の同期サンプル DTS 一覧を保持する
            instance._ts_stream_info = None
            instance._ts_source_base_dts = recorded_program.recorded_video.ts_source_base_dts
            instance._mp4_keyframe_dts_list = None
            instance._source_position_lock = asyncio.Lock()

            # 現在実行中の VideoEncodingTask のインスタンス
            ## 録画再生時は、シークによりエンコーダーの再起動が必要になる度に、新しい VideoEncodingTask を都度作り直す
            ## なお、エンコードタスクの停止時は Task.cancel() ではなく VideoEncodingTask.cancel() を使い
            ## 外部プロセスを即座に停止させる必要があるため、Task への参照とは別に、VideoEncodingTask のインスタンス自体も保持している
            instance._video_encoding_task = VideoEncodingTask(instance)
            # セグメント要求の多重実行時に、エンコードタスクの停止と起動が競合しないよう直列化する
            instance._video_encoding_task_lock = asyncio.Lock()
            # 現在実行中の VideoEncodingTask のタスクへの参照
            # ref: https://docs.astral.sh/ruff/rules/asyncio-dangling-task/
            instance._video_encoding_task_ref = None
            # 終了待機がタイムアウトした古い VideoEncodingTask のタスクへの参照
            # イベントループ上の Task は弱参照で管理されるため、自然終了するまでここで強参照を保持する
            instance._detached_video_encoding_task_refs = set()
            # クライアントが最後に開始したシークの世代番号
            # stopLoad() 前に発行済みだった古いセグメント要求で、新タスクを再起動しないために使う
            instance._latest_request_generation = 0
            instance._latest_requested_sequence = 0

            # キャンセルされない限り SESSION_TIMEOUT 秒後にインスタンスを破棄するタイマー
            # cancel_destroy_timer() を呼び出すことでタイマーをキャンセルできる
            instance._cancel_destroy_timer = SetTimeout(lambda: asyncio.create_task(instance.destroy()), cls.SESSION_TIMEOUT)

            # 生成したインスタンスを登録する
            cls.__instances[session_id] = instance

            logging.info(f'{instance.log_prefix} Streaming Session Started.')

        else:
            # 既存のインスタンスを取得
            instance = cls.__instances[session_id]

            # 録画番組 ID と画質が一致するか確認
            if instance.recorded_program.id != recorded_program.id or instance.quality != quality:
                # 既存セッション側と要求側の品質を両方出し、session_id 再利用ミスをログから判別しやすくする
                requested_encoding_options = encoding_options or StreamEncodingOptions()
                logging.error(
                    f'{instance.log_prefix} Session exists but program_id or quality mismatch. '
                    f'[program_id: {recorded_program.id}, '
                    f'quality: {quality}{requested_encoding_options.buildSuffix()}]'
                )
                raise HTTPException(
                    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail = 'Session exists but program_id or quality mismatch',
                )

            # プレイリスト再取得時に同じ session_id で別の追加オプションが指定された場合は異常として扱う
            ## session_id は録画視聴セッションを識別する値なので、初回作成時と異なるオプションを後から混ぜるとエンコード状態が曖昧になる
            if encoding_options is not None and instance.encoding_options != encoding_options:
                logging.error(
                    f'{instance.log_prefix} Session exists but encoding options mismatch. '
                    f'[is_hevc_10bit_enabled: {encoding_options.is_hevc_10bit_enabled}, '
                    f'is_24fps_mode_enabled: {encoding_options.is_24fps_mode_enabled}, '
                    f'video_codec: {encoding_options.video_codec}, audio_codec: {encoding_options.audio_codec}]'
                )
                raise HTTPException(
                    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail = 'Session exists but encoding options mismatch',
                )

        # 登録されているインスタンスを返す
        return cls.__instances[session_id]


    def __init__(
        self,
        session_id: str,
        recorded_program: RecordedProgram,
        quality: QUALITY_TYPES,
        encoding_options: StreamEncodingOptions | None = None,
        is_new_session_allowed: bool = False,
    ) -> None:
        """
        録画視聴セッションのインスタンスを取得する

        Args:
            session_id (str): セッション ID
            recorded_program (RecordedProgram): 録画番組の情報
            quality (QUALITY_TYPES): 映像の品質 (1080p-60fps ~ 240p)
            encoding_options (StreamEncodingOptions | None): ベース画質に追加するエンコードオプション
            is_new_session_allowed (bool): セッションが存在しない場合に新規作成を許可するかどうか
        """

        # インスタンス変数の型ヒントを定義
        # Singleton のためインスタンスの生成は __new__() で行うが、__init__() も定義しておかないと補完がうまく効かない
        self.session_id: str
        self.recorded_program: RecordedProgram
        self.quality: QUALITY_TYPES
        self.encoding_options: StreamEncodingOptions
        self._segment_duration_seconds: float
        self._segments: list[VideoStreamSegment]
        self._audio_segment_cache: dict[tuple[str, int], bytes]
        self._audio_init_cache: dict[str, bytes]
        self._video_only_segment_cache: dict[int, bytes]
        self._muxed_segment_cache: dict[int, bytes]
        self._video_only_segment_locks: dict[int, asyncio.Lock]
        self._audio_segment_locks: dict[tuple[str, int], asyncio.Lock]
        self._audio_segment_futures: dict[tuple[str, int], asyncio.Future[bytes]]
        self._audio_encoding_task_refs: dict[str, asyncio.Task[None]]
        self._audio_encoding_processes: dict[str, asyncio.subprocess.Process]
        self._audio_requested_sequences: dict[str, int]
        self._opus_encoding_lock: asyncio.Lock
        self._opus_encoding_process: asyncio.subprocess.Process | None
        self._opus_encoding_task_ref: asyncio.Task[None] | None
        self._subtitle_cache: dict[int, bytes]
        self._subtitle_locks: dict[int, asyncio.Lock]
        self._segment_map_by_sequence: dict[int, SegmentMapEntry]
        self._ts_stream_info: TSStreamInfo | None
        self._ts_source_base_dts: int | None
        self._mp4_keyframe_dts_list: list[int] | None
        self._source_position_lock: asyncio.Lock
        self._video_encoding_task: VideoEncodingTask
        self._video_encoding_task_lock: asyncio.Lock
        self._video_encoding_task_ref: asyncio.Task[None] | None
        self._detached_video_encoding_task_refs: set[asyncio.Task[None]]
        self._latest_request_generation: int
        self._latest_requested_sequence: int
        self._cancel_destroy_timer: Callable[[], None]


    @property
    def log_prefix(self) -> str:
        """
        ログのプレフィックス
        """
        return (
            f'[Video: {self.recorded_program.id}/{self.session_id}/{self.quality}'
            f'{self.encoding_options.buildSuffix()}/{self.encoding_options.video_codec}/'
            f'{self.encoding_options.audio_codec}/{self.encoding_options.audio_rendition_id or "default"}]'
        )


    @property
    def segments(self) -> tuple[VideoStreamSegment, ...]:
        """
        HLS セグメントを格納するリスト (読み取り専用)
        一旦録画データの長さすべての VideoStreamSegment を作成したあと、必要に応じてエンコードしていく
        基本一度 VideoStream 内部でセットされたら外部から変更されるべきではないので、読み取り専用にしている
        """
        return tuple(self._segments)


    @property
    def ts_stream_info(self) -> TSStreamInfo | None:
        """
        MPEG-TS のオンデマンド探索で得たストリーム情報

        Returns:
            TSStreamInfo | None: 映像 PID などのストリーム情報 (未解決の場合は None)
        """

        return self._ts_stream_info


    @property
    def ts_source_base_dts(self) -> int | None:
        """
        MPEG-TS の先頭キーフレーム DTS

        Returns:
            int | None: 先頭キーフレーム DTS (未解決の場合は None)
        """

        return self._ts_source_base_dts


    @property
    def latest_requested_sequence(self) -> int:
        """クライアントが現在要求している最新のセグメント番号。"""

        return self._latest_requested_sequence


    async def ensureTSKeyFrameContext(self) -> None:
        """
        MPEG-TS のキーフレーム解析に必要なストリーム情報と先頭 DTS を用意する
        """

        recorded_video = self.recorded_program.recorded_video
        if recorded_video.container_format != 'MPEG-TS':
            return

        async with self._source_position_lock:
            file_path = Path(recorded_video.file_path)

            # segment_map キャッシュから再生を開始した場合、ソース位置は即時解決できても PID 情報が未取得のままになる
            ## 再生中のキーフレーム収集は入力 TS の PES を読むため、必要になった時点で一度だけ PAT/PMT を読む
            if self._ts_stream_info is None:
                self._ts_stream_info = await asyncio.to_thread(
                    TSKeyFrameSeeker.findStreamInfo,
                    file_path,
                )

            # segment_map をプレイリスト時刻へ対応させるには、録画先頭の DTS が基準として必要になる
            ## ここも同一セッション内で変わらないため、未取得のときだけ読み込む
            if self._ts_source_base_dts is None:
                self._ts_source_base_dts = await asyncio.to_thread(
                    TSKeyFrameSeeker.findBaseDTS,
                    file_path,
                    self._ts_stream_info,
                )
                await RecordedVideo.filter(id=recorded_video.id).update(
                    ts_source_base_dts=self._ts_source_base_dts,
                )
                recorded_video.ts_source_base_dts = self._ts_source_base_dts


    def __registerVideoEncodingTaskRef(self, video_encoding_task_ref: asyncio.Task[None]) -> None:
        """
        VideoEncodingTask の完了時に不要な参照を解放するコールバックを登録する

        Args:
            video_encoding_task_ref (asyncio.Task[None]): 参照管理対象の VideoEncodingTask
        """

        def OnVideoEncodingTaskDone(done_task: asyncio.Task[None]) -> None:
            self._detached_video_encoding_task_refs.discard(done_task)
            # 現在実行中の VideoEncodingTask のタスクが終了待機を打ち切ったタスクだった場合は、その参照を None にする
            if self._video_encoding_task_ref == done_task:
                self._video_encoding_task_ref = None

        video_encoding_task_ref.add_done_callback(OnVideoEncodingTaskDone)


    def __detachVideoEncodingTaskRef(self, video_encoding_task_ref: asyncio.Task[None]) -> None:
        """
        終了待機を打ち切った VideoEncodingTask のタスクへの参照を保持する

        Args:
            video_encoding_task_ref (asyncio.Task[None]): 自然終了待ちに移行する VideoEncodingTask
        """

        # すでに完了済みのタスクなら done callback 側で参照は解放済みなので、保持し直す必要はない
        if video_encoding_task_ref.done() is True:
            return

        # 終了待機を打ち切った VideoEncodingTask のタスクへの参照を保持する
        self._detached_video_encoding_task_refs.add(video_encoding_task_ref)


    def keepAlive(self) -> None:
        """
        録画視聴セッションのアクティブ状態を維持する
        番組の視聴中は定期的にこのメソッドを呼び出す必要があり、呼び出されなくなった場合は自動的に終了処理が行われる
        """

        # 前回のタイマーをキャンセルする
        self._cancel_destroy_timer()

        # キャンセルされない限り SESSION_TIMEOUT 秒後にインスタンスを破棄するタイマーを設定する
        self._cancel_destroy_timer = SetTimeout(lambda: asyncio.create_task(self.destroy()), self.SESSION_TIMEOUT)


    def getBufferRange(self) -> tuple[float, float]:
        """
        エンコード完了済みの HLS セグメントのバッファ範囲 (秒) を返す

        Returns:
            tuple[float, float]: バッファ範囲 (開始時刻, 終了時刻)
        """

        # エンコード済みの全セグメントの範囲を計算する
        # エンコード済み (Completed) のセグメントのみを対象とする
        encoded_segments = [s for s in self._segments if s.encode_status == 'Completed']
        if encoded_segments:
            # エンコード済みの最初のセグメントの開始時刻から最後のセグメントの終了時刻までを計算
            first_segment = encoded_segments[0]
            last_segment = encoded_segments[-1]
            buffer_start = first_segment.playlist_start_seconds
            buffer_end = last_segment.playlist_start_seconds + last_segment.duration_seconds
            return (buffer_start, buffer_end)
        else:
            # エンコード済みのセグメントがない場合は (0, 0) を返す
            return (0, 0)


    def getVirtualPlaylist(self, cache_key: str | None = None) -> str:
        """
        仮想 HLS M3U8 プレイリストを取得する
        返却時点では仮想 HLS M3U8 プレイリストに記載されているセグメントのデータは存在せず (「仮想」のゆえん)、随時エンコードされる

        Args:
            cache_key (str | None): キャッシュ制御用のキー (None の場合は新しいキーを生成する)

        Returns:
            str: 仮想 HLS M3U8 プレイリスト
        """

        # セッションのアクティブ状態を維持する
        self.keepAlive()

        # まだ HLS セグメントリストが空なら、録画時間とフレームレートから仮想セグメントを作成する
        if len(self._segments) == 0:
            duration = self.recorded_program.recorded_video.duration
            boundaries = {0.0, duration}
            boundaries.update(
                min(duration, sequence * self._segment_duration_seconds)
                for sequence in range(1, max(1, math.ceil(duration / self._segment_duration_seconds)))
            )
            # 音声構成の変化点を必ずセグメント境界にする。これによりレンディション切替時に
            # 1つのセグメントへ異なる PID / 言語構成が混在しない。
            for interval in self.recorded_program.recorded_video.audio_track_timeline:
                boundaries.add(max(0.0, min(duration, float(interval['start_time']))))
                boundaries.add(max(0.0, min(duration, float(interval['end_time']))))
            ordered_boundaries = sorted(boundaries)
            for segment_sequence, playlist_start_seconds in enumerate(ordered_boundaries[:-1]):
                duration_seconds = max(ordered_boundaries[segment_sequence + 1] - playlist_start_seconds, 0.001)
                self._segments.append(VideoStreamSegment(
                    sequence_index = segment_sequence,
                    playlist_start_seconds = playlist_start_seconds,
                    source_file_position = None,
                    source_start_dts = None,
                    duration_seconds = duration_seconds,
                    encode_status = 'Pending',
                    encoded_segment_ts_future = asyncio.Future(),
                ))

            logging.info(
                f'{self.log_prefix} Total {len(self._segments)} virtual segments '
                f'(segment_duration: {self._segment_duration_seconds:.6f}s).'
            )

        # キャッシュキーが指定されていない場合は UUID の - で区切って一番左側のみを使う
        if cache_key is None:
            cache_key = uuid.uuid4().hex.split('-')[0]

        # 仮想 HLS M3U8 プレイリストを生成
        virtual_playlist = ''
        virtual_playlist += '#EXTM3U\n'
        virtual_playlist += '#EXT-X-VERSION:6\n'
        virtual_playlist += '#EXT-X-PLAYLIST-TYPE:VOD\n'

        # HLS セグメントの実時間の最大値を指定する (小数点以下は切り上げ)
        target_duration = max(s.duration_seconds for s in self._segments)
        virtual_playlist += f'#EXT-X-TARGETDURATION:{math.ceil(target_duration)}\n'

        # 事前に算出したセグメントをすべて記述する
        for segment in self._segments:
            # セグメントの長さ (秒, 小数点以下6桁まで)
            virtual_playlist += f'#EXTINF:{segment.duration_seconds:.6f},\n'
            # キャッシュ避けのためにキャッシュキーを付与する
            virtual_playlist += (
                f'segment?session_id={self.session_id}&sequence={segment.sequence_index}&cache_key={cache_key}'
                f'&{self.__getCodecQuery()}\n'
            )

        virtual_playlist += '#EXT-X-ENDLIST\n'
        return virtual_playlist


    def getAudioRenditions(self) -> list[dict[str, str | int]]:
        """録画メタデータから HLS 代替音声レンディション一覧を構築する。"""

        renditions: list[dict[str, str | int]] = []
        legacy_stream_offset = 1 if self.recorded_program.recorded_video.container_format == 'MPEG-TS' else 0
        for fallback_index, track in enumerate(self.recorded_program.recorded_video.audio_tracks, start=1):
            track_index = int(track.get('index', fallback_index))
            stream_index = int(track.get('stream_index', track_index + legacy_stream_offset))
            language = track.get('language') or ''
            title = track.get('title') or ''
            channel = track.get('channel') or ''
            if track.get('is_dual_mono') is True:
                languages = [item.strip() for item in language.split('+') if item.strip()]
                main_language = languages[0] if languages else ''
                sub_language = languages[1] if len(languages) >= 2 else '副音声'
                main_display_index = len(renditions) + 1
                sub_display_index = main_display_index + 1
                main_label = (
                    f'Track{main_display_index}{f" {main_language}" if main_language else ""} (Monaural) 主音声'
                )
                sub_label = f'Track{sub_display_index} {sub_language} (Monaural) 副音声'
                renditions.extend([
                    {'id': f'{track_index}-main', 'track_index': track_index, 'stream_index': stream_index,
                     'channel': 'main', 'name': main_label, 'language': main_language},
                    {'id': f'{track_index}-sub', 'track_index': track_index, 'stream_index': stream_index,
                     'channel': 'sub', 'name': sub_label, 'language': sub_language},
                ])
            else:
                display_index = len(renditions) + 1
                base_label = title or (
                    f'Track{display_index}{f" {language}" if language else ""}{f" ({channel})" if channel else ""}'
                )
                renditions.append({
                    'id': str(track_index), 'track_index': track_index, 'stream_index': stream_index,
                    'channel': 'all', 'name': base_label, 'language': language,
                })
        if len(renditions) == 0 and self.recorded_program.recorded_video.has_audio:
            # 旧DBレコードとの後方互換: 従来の主音声フィールドしかない場合は先頭音声を公開する
            renditions.append({
                'id': '1', 'track_index': 1, 'stream_index': 1 + legacy_stream_offset, 'channel': 'all',
                'name': f'Track1 ({self.recorded_program.recorded_video.primary_audio_channel})',
                'language': self.recorded_program.primary_audio_language or '',
            })
        return renditions


    def getEffectiveAudioRendition(self, sequence: int) -> dict[str, str | int] | None:
        """指定セグメントで実在する希望音声、またはTrack 1を返す。"""

        renditions = self.getAudioRenditions()
        if not renditions:
            return None
        start = self._segments[sequence].playlist_start_seconds
        interval = next((item for item in self.recorded_program.recorded_video.audio_track_timeline
            if float(item['start_time']) <= start < float(item['end_time'])), None)
        available_indexes = None if interval is None else {
            int(item.get('index', 0)) for item in interval['tracks']
        }
        requested = next((item for item in renditions
            if str(item['id']) == self.encoding_options.audio_rendition_id), None)
        if requested is not None and (
            available_indexes is None or int(requested['track_index']) in available_indexes
        ):
            return requested
        return next((item for item in renditions
            if available_indexes is None or int(item['track_index']) in available_indexes), None)


    def getMasterPlaylist(self, cache_key: str | None = None) -> str:
        """映像と選択音声を同じTSに多重化した HLS マスタープレイリストを返す。"""

        def escape_attribute(value: object) -> str:
            """HLS の quoted-string 用に改行・バックスラッシュ・引用符をエスケープする。"""

            return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\r', ' ').replace('\n', ' ')

        def normalize_language(value: object) -> str:
            """放送メタデータの日本語表記を HLS LANGUAGE 用の言語タグへ正規化する。"""

            language = str(value)
            return {'日本語': 'ja', '英語': 'en', 'その他の言語': 'und'}.get(language, language)

        self.getVirtualPlaylist(cache_key)
        if cache_key is None:
            cache_key = uuid.uuid4().hex.split('-')[0]

        lines = ['#EXTM3U', '#EXT-X-VERSION:6']
        recorded_video = self.recorded_program.recorded_video
        audio_renditions = self.getAudioRenditions()
        subtitle_tracks = recorded_video.subtitle_tracks \
            if recorded_video.container_format != 'MPEG-TS' and recorded_video.has_video else []
        for track in subtitle_tracks:
            name = escape_attribute(track.get('title') or track.get('language') or f'Subtitle {track["index"]}')
            language = escape_attribute(normalize_language(track.get('language') or ''))
            attributes = [
                'TYPE=SUBTITLES', 'GROUP-ID="subs"', f'NAME="{name}"',
                'DEFAULT=NO', 'AUTOSELECT=YES',
                f'URI="subtitle/{track["index"]}/playlist?session_id={self.session_id}&cache_key={cache_key}&{self.__getCodecQuery()}"',
            ]
            if language:
                attributes.append(f'LANGUAGE="{language}"')
            lines.append('#EXT-X-MEDIA:' + ','.join(attributes))

        if recorded_video.has_video:
            max_bitrate_text = QUALITY[self.quality].video_bitrate_max.upper()
            max_bitrate = int(float(max_bitrate_text[:-1]) * 1000) if max_bitrate_text.endswith('K') else int(max_bitrate_text)
            codecs = [getVideoCodecDefinition(self.encoding_options.video_codec).hls_codec]
            if recorded_video.has_audio:
                codecs.append(getAudioCodecDefinition(self.encoding_options.audio_codec).hls_codec)
            stream_attributes = [
                f'BANDWIDTH={max_bitrate + (768_000 if recorded_video.has_audio else 0)}',
                f'CODECS="{",".join(codecs)}"',
            ]
            media_playlist_uri = (
                f'video/playlist?session_id={self.session_id}&cache_key={cache_key}&{self.__getCodecQuery()}'
            )
        else:
            # 音声のみでも選択音声を含む同じmuxedセグメントAPIを使う。
            stream_attributes = [
                'BANDWIDTH=512000',
                f'CODECS="{getAudioCodecDefinition(self.encoding_options.audio_codec).hls_codec}"',
            ]
            media_playlist_uri = (
                f'video/playlist?session_id={self.session_id}&cache_key={cache_key}&{self.__getCodecQuery()}'
            )
        if subtitle_tracks:
            stream_attributes.append('SUBTITLES="subs"')
        lines.append('#EXT-X-STREAM-INF:' + ','.join(stream_attributes))
        if media_playlist_uri:
            lines.append(media_playlist_uri)
        return '\n'.join(lines) + '\n'


    def getAudioPlaylist(self, rendition_id: str, cache_key: str | None = None) -> str:
        """指定音声レンディションの仮想メディアプレイリストを返す。"""

        if not any(str(item['id']) == rendition_id for item in self.getAudioRenditions()):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Audio rendition was not found')
        self.getVirtualPlaylist(cache_key)
        cache_key = cache_key or uuid.uuid4().hex.split('-')[0]
        lines = ['#EXTM3U', '#EXT-X-VERSION:6', '#EXT-X-PLAYLIST-TYPE:VOD']
        lines.append(f'#EXT-X-TARGETDURATION:{math.ceil(max(s.duration_seconds for s in self._segments))}')
        for segment in self._segments:
            if self.encoding_options.audio_codec == 'opus':
                # シーク先の実音声エンコーダーが生成したinitを使う。
                # 仮のanullsrc由来initはdOpsが実fragmentと一致せずMSEへappendできない。
                lines.append(
                    f'#EXT-X-MAP:URI="init?session_id={self.session_id}&sequence={segment.sequence_index}'
                    f'&cache_key={cache_key}&{self.__getCodecQuery()}"'
                )
            lines.append(f'#EXTINF:{segment.duration_seconds:.6f},')
            lines.append(
                f'segment?session_id={self.session_id}&sequence={segment.sequence_index}&cache_key={cache_key}'
                f'&{self.__getCodecQuery()}'
            )
        lines.append('#EXT-X-ENDLIST')
        return '\n'.join(lines) + '\n'


    def __getCodecQuery(self) -> str:
        """子プレイリスト/APIへ録画セッションのコーデック指定を引き継ぐ。"""

        query = f'video_codec={self.encoding_options.video_codec}&audio_codec={self.encoding_options.audio_codec}'
        if self.encoding_options.audio_rendition_id is not None:
            query += f'&audio_track={self.encoding_options.audio_rendition_id}'
        return query


    def __isSegmentRequestCurrent(self, request_generation: int | None, *, update: bool = False) -> bool:
        """セグメント要求が現在のシーク世代に属するかを判定する。"""

        if request_generation is None:
            return True
        if request_generation < self._latest_request_generation:
            return False
        if update and request_generation > self._latest_request_generation:
            self._latest_request_generation = request_generation
        return True


    async def getAudioSegment(
        self,
        rendition_id: str,
        sequence: int,
        request_generation: int | None = None,
    ) -> bytes | None:
        """選択された元音声をAAC/Opusへ変換したHLSセグメントを生成する。"""

        self.keepAlive()
        if self.__isSegmentRequestCurrent(request_generation, update=True) is False:
            logging.debug(
                f'{self.log_prefix}[Segment {sequence}] Ignored stale audio segment request. '
                f'[request_generation: {request_generation}, latest: {self._latest_request_generation}]'
            )
            return b''
        rendition = next((item for item in self.getAudioRenditions() if str(item['id']) == rendition_id), None)
        if rendition is None or sequence < 0 or sequence >= len(self._segments):
            return None

        key = (rendition_id, sequence)
        previous_requested_sequence = self._audio_requested_sequences.get(rendition_id)
        is_distant_seek = previous_requested_sequence is not None and (
            sequence > previous_requested_sequence + self.AUDIO_PREFETCH_SEGMENTS + 1 or
            sequence < previous_requested_sequence - self.MAX_READED_SEGMENTS
        )
        self._audio_requested_sequences[rendition_id] = sequence
        # 再生位置より十分後ろの音声だけ破棄し、長時間視聴でもメモリ使用量を一定に保つ。
        for cached_key in [
            cached_key for cached_key in self._audio_segment_cache
            if cached_key[0] == rendition_id and cached_key[1] < sequence - self.MAX_READED_SEGMENTS
        ]:
            self._audio_segment_cache.pop(cached_key, None)
            self._audio_segment_futures.pop(cached_key, None)
        if key in self._audio_segment_cache and is_distant_seek is False:
            return self._audio_segment_cache[key]
        if key in self._audio_segment_futures and is_distant_seek is False:
            return (await asyncio.shield(self._audio_segment_futures[key])) or None

        if self.encoding_options.audio_codec == 'opus':
            return await self.__getSharedOpusSegment(rendition_id, sequence, is_distant_seek)

        # AACは映像と全音声を同じエンコーダーで多重化している。映像側の連続AACを
        # 再エンコードせず音声専用TSへremuxし、独立FFmpeg経路は映像生成前のフォールバックに限定する。
        segment = self._segments[sequence]
        if self.encoding_options.audio_codec == 'aac' and segment.encode_status in ['Encoding', 'Completed']:
            try:
                multiplexed_segment = await asyncio.wait_for(
                    asyncio.shield(segment.encoded_segment_ts_future), timeout=10.0,
                )
                extracted = await self.__extractAudioFromMultiplexedSegment(
                    rendition_id,
                    multiplexed_segment,
                    segment.playlist_start_seconds,
                    segment.duration_seconds,
                )
                if extracted is not None:
                    self._audio_segment_cache[key] = extracted
                    return extracted
            except TimeoutError:
                pass
        # 同じレンディションは連続した複数セグメントを一括生成するため、sequenceをまたいで直列化する。
        lock = self._audio_segment_locks.setdefault((rendition_id, -1), asyncio.Lock())
        async with lock:
            if key in self._audio_segment_cache and is_distant_seek is False:
                return self._audio_segment_cache[key]
            if key in self._audio_segment_futures and is_distant_seek is False:
                return (await asyncio.shield(self._audio_segment_futures[key])) or None
            # キャッシュ外へのシークでは、同じレンディションの旧プロセスを終了して新しい位置から開始する。
            previous_process = self._audio_encoding_processes.get(rendition_id)
            previous_task = self._audio_encoding_task_refs.get(rendition_id)
            if previous_process is not None and previous_process.returncode is None:
                previous_process.kill()
            if previous_task is not None and previous_task.done() is False:
                previous_task.cancel()
            # 旧タスクが予約した未生成Futureを除去し、シーク先の新タスクが同じキーを予約できるようにする。
            for pending_key, pending_future in list(self._audio_segment_futures.items()):
                if pending_key[0] == rendition_id and pending_future.done() is False:
                    pending_future.set_result(b'')
                    self._audio_segment_futures.pop(pending_key, None)
            segment = self._segments[sequence]
            start = segment.playlist_start_seconds
            # HLSへ公開するPTSは入力TSの絶対時刻ではなくプレイリスト時刻へ統一する。
            timestamp_offset = start
            source_target_timestamp = start
            if self.recorded_program.recorded_video.container_format == 'MPEG-TS':
                # 音声のみ TS にはキーフレームがないため、映像用 seeker を通さず音声 PTS を FFmpeg に解決させる。
                if self.recorded_program.recorded_video.has_video:
                    await self.resolveSegmentSourcePosition(sequence)
                    # segment_map からソース位置を即時復元できた場合でも、基準 DTS はまだ未取得のことがある。
                    # 音声リクエストは映像より先に到着し得るため、映像エンコーダー任せにせずここで必ず確定する。
                    await self.ensureTSKeyFrameContext()
                    assert self._ts_source_base_dts is not None
                    # 映像側はシーク先のキーフレーム DTS を出力基準にするため、音声も同じ値へ合わせる。
                    # 基準 DTS + プレイリスト時刻では、長い GOP で映像と音声の PTS がずれる。
                    source_target_timestamp = (self._ts_source_base_dts / ts.HZ) + start
            timeline_interval = next((interval for interval in self.recorded_program.recorded_video.audio_track_timeline
                if float(interval['start_time']) <= start < float(interval['end_time'])), None)
            available_tracks = timeline_interval['tracks'] if timeline_interval is not None else \
                self.recorded_program.recorded_video.audio_tracks
            track = next((
                item for item in available_tracks
                if int(item.get('index', 0)) == int(rendition['track_index'])
            ), None)
            # 選択 Track が消えた区間は Track 1（なければ区間先頭）へ戻す。
            if track is None and available_tracks:
                track = next((item for item in available_tracks if int(item.get('index', 0)) == 1), available_tracks[0])
            # 一時的な完全無音区間は同じ時間・PTS の無音 AAC を生成する。
            if not available_tracks:
                return await self.__generateSilentAudioSegment(rendition_id, segment, timestamp_offset)
            channels = (track.get('channel', '') if track is not None else \
                self.recorded_program.recorded_video.primary_audio_channel) or ''
            bitrate = '512k' if channels in ['7.1ch', '8ch'] else '384k' if channels == '5.1ch' else '192k'
            # 新形式は FFprobe の絶対 stream index を使う。旧 audio_tracks には stream_index がないため、
            # TS の data/subtitle stream 数を推測せず FFmpeg の音声相対 index で安全に選択する。
            track_pid = track.get('pid') if track is not None else None
            explicit_stream_index = track.get('stream_index') if track is not None else None
            if self.recorded_program.recorded_video.container_format == 'MPEG-TS' and track_pid is not None:
                # 途中で追加されたPIDはファイル先頭のPMTだけを読んだFFmpegから見えない。
                # セグメント付近から入力を開いて現在のPMTを再取得し、TS PIDで音声を選択する。
                if segment.source_file_position is not None:
                    source_file_position = segment.source_file_position
                else:
                    source_file_position = round(
                        self.recorded_program.recorded_video.file_size * start /
                        max(self.recorded_program.recorded_video.duration, 0.001) /
                        ts.PACKET_SIZE
                    ) * ts.PACKET_SIZE
                input_args = [
                    '-skip_initial_bytes', str(max(source_file_position, 0)),
                    '-i', self.recorded_program.recorded_video.file_path,
                ]
                map_specifier = f'0:i:{int(track_pid):#x}'
                # キーフレーム位置が目標時刻より手前の場合、その差だけ出力側で読み飛ばして境界を揃える。
                trim_seconds = 0.0
                if segment.source_start_dts is not None:
                    trim_seconds = max(source_target_timestamp - segment.source_start_dts / ts.HZ, 0.0)
                post_input_seek_args = ['-ss', f'{trim_seconds:.6f}'] if trim_seconds > 0.001 else []
            else:
                input_args = [
                    '-ss', f'{start:.6f}',
                    '-i', self.recorded_program.recorded_video.file_path,
                ]
                map_specifier = f'0:{int(explicit_stream_index)}' if explicit_stream_index is not None else \
                    f'0:a:{max(int(rendition["track_index"]) - 1, 0)}'
                post_input_seek_args = []
            dual_mono_decoder_args = []
            if rendition['channel'] in ['main', 'sub']:
                dual_mono_decoder_args = ['-dual_mono_mode', str(rendition['channel'])]
            args = [
                '-hide_banner', '-loglevel', 'error', *dual_mono_decoder_args, *input_args, *post_input_seek_args,
                '-map', map_specifier, '-vn', '-sn', '-dn',
                # TS途中位置の音声PTSには大きな飛びや基準差があり得るため、デコード後の連続サンプル数から
                # 0起点の時刻を再構成する。AACエンコーダーは止めず、この後output_ts_offsetで録画時刻へ移す。
                '-af', 'asetpts=N/SR/TB',
            ]
            if rendition['channel'] == 'main':
                args += ['-ac', '2']
            elif rendition['channel'] == 'sub':
                args += ['-ac', '2']
            elif channels == '22.2ch' or (
                channels.endswith('ch') and channels not in ['5.1ch', '7.1ch', '8ch']
            ):
                args += ['-ac', '2']
            audio_codec = getAudioCodecDefinition(self.encoding_options.audio_codec)
            args += ['-c:a', audio_codec.ffmpeg_encoder, '-b:a', bitrate, '-ar', '48000']
            if self.encoding_options.audio_codec == 'opus':
                args += ['-application', 'audio', '-frame_duration', '20']
            else:
                args += [
                    '-output_ts_offset', f'{timestamp_offset:.6f}', '-mpegts_flags', '+resend_headers',
                    '-muxdelay', '0', '-muxpreload', '0',
                ]

            # PID / Track構成が変化する境界まで、同じFFmpegを終了せず連続生成する。
            interval_end = float(timeline_interval['end_time']) if timeline_interval is not None else \
                self.recorded_program.recorded_video.duration
            batch_segments: list[VideoStreamSegment] = []
            for candidate in self._segments[sequence:]:
                if candidate.playlist_start_seconds >= interval_end - 0.0005:
                    break
                batch_segments.append(candidate)
            if len(batch_segments) == 0:
                batch_segments = [segment]
            batch_duration = sum(candidate.duration_seconds for candidate in batch_segments)

            # segment muxerへ処理開始からの相対分割点を渡し、AACエンコーダーを止めずにHLS TSだけを分割する。
            split_times: list[str] = []
            cumulative_duration = 0.0
            for candidate in batch_segments[:-1]:
                cumulative_duration += candidate.duration_seconds
                # segment muxer は入力先頭PTSを内部基準として扱うため、分割点は処理開始からの相対値で渡す。
                # -break_non_keyframes と組み合わせ、AACパケットを映像と同じ境界で確実に分割する。
                split_times.append(f'{cumulative_duration:.6f}')

            temporary_directory = tempfile.TemporaryDirectory(prefix=f'konomitv-audio-{self.session_id}-')
            extension = 'm4s' if self.encoding_options.audio_codec == 'opus' else 'ts'
            output_pattern = str(Path(temporary_directory.name) / f'%09d.{extension}')
            segment_args = [
                '-t', f'{batch_duration:.6f}', '-f', 'segment',
                '-segment_format', 'mp4' if self.encoding_options.audio_codec == 'opus' else 'mpegts',
                '-segment_start_number', str(sequence),
                # 音声パケットには映像のランダムアクセスフラグがないため、指定PTSで必ず分割する。
                '-break_non_keyframes', '1',
            ]
            if self.encoding_options.audio_codec == 'opus':
                segment_args += [
                    '-segment_format_options', 'movflags=+frag_keyframe+empty_moov+default_base_moof',
                ]
            if split_times:
                segment_args += ['-segment_times', ','.join(split_times)]
            segment_args += ['-y', output_pattern]
            process = await asyncio.create_subprocess_exec(
                LIBRARY_PATH['FFmpeg'], *args, *segment_args,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            for candidate in batch_segments:
                candidate_key = (rendition_id, candidate.sequence_index)
                self._audio_segment_futures.setdefault(candidate_key, asyncio.get_running_loop().create_future())
            self._audio_encoding_processes[rendition_id] = process
            monitor_task = asyncio.create_task(self.__monitorContinuousAudioEncoding(
                rendition_id, process, temporary_directory, batch_segments,
            ))
            self._audio_encoding_task_refs[rendition_id] = monitor_task
            return (await asyncio.shield(self._audio_segment_futures[key])) or None


    async def __getSharedOpusSegment(
        self,
        rendition_id: str,
        sequence: int,
        is_distant_seek: bool,
    ) -> bytes | None:
        """全Opusレンディションを単一入力FFmpegで同時生成する。"""

        key = (rendition_id, sequence)
        async with self._opus_encoding_lock:
            if key in self._audio_segment_cache and is_distant_seek is False:
                return self._audio_segment_cache[key]
            if key in self._audio_segment_futures and is_distant_seek is False:
                future = self._audio_segment_futures[key]
            else:
                if self._opus_encoding_process is not None and self._opus_encoding_process.returncode is None:
                    self._opus_encoding_process.kill()
                if self._opus_encoding_task_ref is not None and self._opus_encoding_task_ref.done() is False:
                    self._opus_encoding_task_ref.cancel()
                    await asyncio.gather(self._opus_encoding_task_ref, return_exceptions=True)
                for pending_key, pending_future in list(self._audio_segment_futures.items()):
                    if pending_future.done() is False:
                        pending_future.set_result(b'')
                    self._audio_segment_futures.pop(pending_key, None)

                segment = self._segments[sequence]
                start = segment.playlist_start_seconds
                # hls.jsはメイン映像MPEG-TSの先頭PTSを全レンディションの共通基準にする。
                # Opus fMP4も同じ絶対PTSから開始しないと、大きな負時刻へ補正されappendされない。
                multiplexed_video_segment = await self.getSegment(sequence)
                video_pts_base = await self.__probeFirstVideoPTS(multiplexed_video_segment) \
                    if multiplexed_video_segment is not None else start
                timeline_interval = next((interval for interval in self.recorded_program.recorded_video.audio_track_timeline
                    if float(interval['start_time']) <= start < float(interval['end_time'])), None)
                available_tracks = timeline_interval['tracks'] if timeline_interval is not None else \
                    self.recorded_program.recorded_video.audio_tracks
                interval_end = float(timeline_interval['end_time']) if timeline_interval is not None else \
                    self.recorded_program.recorded_video.duration
                batch_segments = [candidate for candidate in self._segments[sequence:]
                    if candidate.playlist_start_seconds < interval_end - 0.0005]
                if not batch_segments:
                    batch_segments = [segment]
                batch_duration = sum(candidate.duration_seconds for candidate in batch_segments)

                # 音声のみではFFmpegが極端に先行し得るため、監視側が3セグメントで
                # SIGSTOPを掛けられる速度に制限する。16倍速なら初期約18秒分を約1秒で生成できる。
                input_args = [
                    '-readrate', '16', '-ss', f'{start:.6f}',
                    '-i', self.recorded_program.recorded_video.file_path,
                ]
                args = [LIBRARY_PATH['FFmpeg'], '-hide_banner', '-loglevel', 'error', *input_args]
                temporary_directory = tempfile.TemporaryDirectory(prefix=f'konomitv-opus-{self.session_id}-')
                renditions = self.getAudioRenditions()
                for item in renditions:
                    track = next((track for track in available_tracks
                        if int(track.get('index', 0)) == int(item['track_index'])), None)
                    if track is None and available_tracks:
                        track = next((track for track in available_tracks if int(track.get('index', 0)) == 1), available_tracks[0])
                    if track is None:
                        map_specifier = '0:a:0?'
                    elif self.recorded_program.recorded_video.container_format == 'MPEG-TS' and track.get('pid') is not None:
                        map_specifier = f'0:i:{int(track["pid"]):#x}'
                    elif track.get('stream_index') is not None:
                        map_specifier = f'0:{int(track["stream_index"])}'
                    else:
                        map_specifier = f'0:a:{max(int(item["track_index"]) - 1, 0)}'
                    output_directory = Path(temporary_directory.name) / str(item['id'])
                    output_directory.mkdir()
                    args += ['-map', map_specifier, '-vn', '-sn', '-dn']
                    if item['channel'] == 'main':
                        audio_filter = 'pan=stereo|c0=c0|c1=c0'
                    elif item['channel'] == 'sub':
                        audio_filter = 'pan=stereo|c0=c1|c1=c1'
                    else:
                        audio_filter = 'anull'
                    # 長さ制限は絶対PTSを付与する前に行う。-tを出力側へ置くと、映像に合わせた
                    # 約9万秒台のPTSを終了時刻と誤認し、最初のfragmentだけで終了してしまう。
                    args += [
                        '-af',
                        f'{audio_filter},atrim=duration={batch_duration:.6f},'
                        f'asetpts=PTS-STARTPTS+{video_pts_base:.6f}/TB',
                    ]
                    args += [
                        '-c:a', 'libopus', '-b:a', '192k', '-ar', '48000', '-application', 'audio',
                        '-frame_duration', '20', '-f', 'hls',
                        '-hls_time', f'{self._segment_duration_seconds:.6f}', '-hls_segment_type', 'fmp4',
                        # 音声だけの出力には映像キーフレームがないため、通常のHLS分割では
                        # 録画全体が1セグメントにまとまる。指定時刻で強制的にfragmentを閉じる。
                        '-hls_flags', 'split_by_time',
                        '-hls_fmp4_init_filename', 'init.mp4', '-start_number', str(sequence),
                        '-hls_segment_filename', str(output_directory / '%09d.m4s'),
                        '-hls_playlist_type', 'vod', '-hls_list_size', '0',
                    ]
                    args += ['-y', str(output_directory / 'index.m3u8')]
                    self._audio_requested_sequences[str(item['id'])] = sequence
                for candidate in batch_segments:
                    for item in renditions:
                        candidate_key = (str(item['id']), candidate.sequence_index)
                        self._audio_segment_futures[candidate_key] = asyncio.get_running_loop().create_future()
                process = await asyncio.create_subprocess_exec(
                    *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                )
                self._opus_encoding_process = process
                monitor = asyncio.create_task(self.__monitorSharedOpusEncoding(
                    process, temporary_directory, renditions, batch_segments,
                ))
                self._opus_encoding_task_ref = monitor
                future = self._audio_segment_futures[key]
        return (await asyncio.shield(future)) or None


    async def __probeFirstVideoPTS(self, segment: bytes) -> float:
        """エンコード済み多重TSの先頭映像PTSを取得する。"""

        pts = await self.__probeFirstStreamPTS(segment, 'v:0')
        if pts is not None:
            return pts
        return 0.0


    async def __probeFirstStreamPTS(self, segment: bytes, stream_specifier: str) -> float | None:
        """エンコード済みTSから指定ストリームの先頭PTSを取得する。"""

        process = await asyncio.create_subprocess_exec(
            LIBRARY_PATH['FFprobe'], '-v', 'error', '-show_packets', '-select_streams', stream_specifier,
            '-show_entries', 'packet=pts_time', '-of', 'csv=p=0', '-i', 'pipe:0',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(segment), timeout=5.0)
        except TimeoutError:
            process.kill()
            await process.wait()
            logging.warning(
                f'{self.log_prefix} Timed out while probing first PTS. [stream: {stream_specifier}]'
            )
            return None
        if process.returncode == 0:
            for line in stdout.decode(errors='ignore').splitlines():
                value = line.split(',', maxsplit=1)[0].strip()
                try:
                    return float(value)
                except ValueError:
                    continue
        logging.warning(
            f'{self.log_prefix} Failed to probe first PTS. '
            f'[stream: {stream_specifier}] {stderr.decode(errors="ignore")}'
        )
        return None


    async def __monitorSharedOpusEncoding(
        self,
        process: asyncio.subprocess.Process,
        temporary_directory: tempfile.TemporaryDirectory[str],
        renditions: list[dict[str, str | int]],
        segments: list[VideoStreamSegment],
    ) -> None:
        """単一FFmpegの全Opus出力をキャッシュへ移し、先行量を制御する。"""

        wait_task = asyncio.create_task(process.wait())
        is_stopped = False
        try:
            for index, segment in enumerate(segments):
                for rendition in renditions:
                    rendition_id = str(rendition['id'])
                    output_path = Path(temporary_directory.name) / rendition_id / f'{segment.sequence_index:09d}.m4s'
                    next_path = Path(temporary_directory.name) / rendition_id / f'{segment.sequence_index + 1:09d}.m4s'
                    while output_path.exists() is False or (index < len(segments) - 1 and next_path.exists() is False):
                        if wait_task.done():
                            break
                        await asyncio.sleep(0.02)
                    if output_path.exists() is False:
                        continue
                    media = await asyncio.to_thread(output_path.read_bytes)
                    init_path = Path(temporary_directory.name) / rendition_id / 'init.mp4'
                    if rendition_id not in self._audio_init_cache and init_path.exists():
                        self._audio_init_cache[rendition_id] = await asyncio.to_thread(init_path.read_bytes)
                    key = (rendition_id, segment.sequence_index)
                    self._audio_segment_cache[key] = media
                    future = self._audio_segment_futures.get(key)
                    if future is not None and future.done() is False:
                        future.set_result(media)
                    await asyncio.to_thread(output_path.unlink, missing_ok=True)
                while segment.sequence_index >= max(self._audio_requested_sequences.values(), default=0) + self.AUDIO_PREFETCH_SEGMENTS:
                    if process.returncode is not None:
                        break
                    if is_stopped is False:
                        process.send_signal(signal.SIGSTOP)
                        is_stopped = True
                    await asyncio.sleep(0.05)
                if is_stopped and process.returncode is None:
                    process.send_signal(signal.SIGCONT)
                    is_stopped = False
            await wait_task
            stderr = await process.stderr.read() if process.stderr is not None else b''
            if process.returncode not in (0, None):
                logging.error(f'{self.log_prefix} Shared Opus encoding failed: {stderr.decode(errors="ignore")}')
        except asyncio.CancelledError:
            if is_stopped and process.returncode is None:
                process.send_signal(signal.SIGCONT)
            if process.returncode is None:
                process.kill()
            raise
        finally:
            for segment in segments:
                for rendition in renditions:
                    future = self._audio_segment_futures.get((str(rendition['id']), segment.sequence_index))
                    if future is not None and future.done() is False:
                        future.set_result(b'')
            temporary_directory.cleanup()
            if self._opus_encoding_process is process:
                self._opus_encoding_process = None
            if self._opus_encoding_task_ref is asyncio.current_task():
                self._opus_encoding_task_ref = None


    async def __extractAudioFromMultiplexedSegment(
        self,
        rendition_id: str,
        segment: bytes,
        playlist_start_seconds: float,
        duration_seconds: float,
    ) -> bytes | None:
        """共有エンコーダーの多重TSから指定音声を再エンコードなしで抽出する。"""

        renditions = self.getAudioRenditions()
        rendition_index = next(
            (index for index, item in enumerate(renditions) if str(item['id']) == rendition_id),
            None,
        )
        if rendition_index is None:
            return None
        rendition = renditions[rendition_index]
        # 現行HWEncCの多重出力で安定してコピーできるのは先頭の通常音声だけ。
        # 追加Trackは出力に存在しない場合があり、Dual Monoは主副分離も必要なので、
        # 存在しないstreamのprobeを待たず元PIDの連続AAC経路へ直ちにフォールバックする。
        if rendition_index != 0 or rendition['channel'] != 'all':
            return None

        # HWEncCは再起動位置ごとに異なるPTS基準を使うため、そのまま代替音声へ分離すると
        # hls.jsが目的時刻を探して先読みし続ける。各セグメントをHLSプレイリスト時刻へ揃える。
        audio_pts = await self.__probeFirstStreamPTS(segment, f'a:{rendition_index}')
        # 共有HWEncC出力に対象音声が含まれない場合は、元TSのPIDを直接読むフォールバックへ移る。
        # 存在しないstreamを-mapするとFFmpegが入力終端まで待ち続け、シーク全体が停止する。
        if audio_pts is None:
            return None
        timestamp_offset = playlist_start_seconds - audio_pts
        playlist_end_seconds = playlist_start_seconds + duration_seconds
        process = await asyncio.create_subprocess_exec(
            LIBRARY_PATH['FFmpeg'], '-hide_banner', '-loglevel', 'error',
            '-copyts', '-itsoffset', f'{timestamp_offset:.6f}', '-f', 'mpegts', '-i', 'pipe:0',
            '-map', f'0:a:{rendition_index}', '-c', 'copy', '-mpegts_flags', '+resend_headers',
            '-to', f'{playlist_end_seconds:.6f}',
            '-avoid_negative_ts', 'disabled', '-mpegts_copyts', '1',
            '-muxdelay', '0', '-muxpreload', '0', '-f', 'mpegts', 'pipe:1',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(segment), timeout=5.0)
        except TimeoutError:
            process.kill()
            await process.wait()
            logging.warning(
                f'{self.log_prefix} Timed out while extracting shared audio rendition {rendition_id}.'
            )
            return None
        if process.returncode != 0 or not stdout:
            logging.warning(
                f'{self.log_prefix} Failed to extract shared audio rendition {rendition_id}: '
                f'{stderr.decode(errors="ignore")}'
            )
            return None
        normalized_audio_pts = await self.__probeFirstStreamPTS(stdout, 'a:0')
        if normalized_audio_pts is None or abs(normalized_audio_pts - playlist_start_seconds) > 0.1:
            logging.error(
                f'{self.log_prefix} Normalized audio PTS is out of range. '
                f'[rendition: {rendition_id}, expected: {playlist_start_seconds:.6f}, '
                f'actual: {normalized_audio_pts}]'
            )
            return None
        return stdout


    async def getVideoOnlySegment(
        self,
        sequence: int,
        request_generation: int | None = None,
    ) -> bytes | None:
        """共有エンコーダーの多重TSから映像とtimed ID3だけを抽出する。"""

        request_started_at = time.perf_counter()
        if self.__isSegmentRequestCurrent(request_generation, update=True) is False:
            logging.debug(
                f'{self.log_prefix}[Segment {sequence}] Ignored stale video segment request. '
                f'[request_generation: {request_generation}, latest: {self._latest_request_generation}]'
            )
            return b''
        for cached_sequence in list(self._video_only_segment_cache):
            if cached_sequence < sequence - self.MAX_READED_SEGMENTS:
                self._video_only_segment_cache.pop(cached_sequence, None)
                self._video_only_segment_locks.pop(cached_sequence, None)
        if sequence in self._video_only_segment_cache:
            return self._video_only_segment_cache[sequence]
        lock = self._video_only_segment_locks.setdefault(sequence, asyncio.Lock())
        async with lock:
            # 同一Fragmentのリトライが重なっても、probe/remuxは1回だけ実行する。
            if sequence in self._video_only_segment_cache:
                return self._video_only_segment_cache[sequence]
            if self.__isSegmentRequestCurrent(request_generation) is False:
                return b''
            multiplexed_segment = await self.getSegment(sequence, request_generation)
            multiplexed_ready_at = time.perf_counter()
            if multiplexed_segment is None:
                return None
            if multiplexed_segment == b'':
                return b''
            video_pts = await self.__probeFirstStreamPTS(multiplexed_segment, 'v:0')
            timestamp_offset = (
                self._segments[sequence].playlist_start_seconds - video_pts
                if video_pts is not None else 0.0
            )
            playlist_end_seconds = (
                self._segments[sequence].playlist_start_seconds + self._segments[sequence].duration_seconds
            )
            process = await asyncio.create_subprocess_exec(
                LIBRARY_PATH['FFmpeg'], '-hide_banner', '-loglevel', 'error',
                '-copyts', '-itsoffset', f'{timestamp_offset:.6f}', '-f', 'mpegts', '-i', 'pipe:0',
                '-map', '0:v:0', '-map', '0:d?', '-c', 'copy', '-ignore_unknown',
                '-to', f'{playlist_end_seconds:.6f}',
                '-mpegts_flags', '+resend_headers', '-avoid_negative_ts', 'disabled', '-mpegts_copyts', '1',
                '-muxdelay', '0', '-muxpreload', '0', '-f', 'mpegts', 'pipe:1',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate(multiplexed_segment)
            if process.returncode != 0 or not stdout:
                logging.error(f'{self.log_prefix} Failed to extract video rendition: {stderr.decode(errors="ignore")}')
                return None
            normalized_video_pts = await self.__probeFirstStreamPTS(stdout, 'v:0')
            playlist_start_seconds = self._segments[sequence].playlist_start_seconds
            if normalized_video_pts is None or abs(normalized_video_pts - playlist_start_seconds) > 0.1:
                logging.error(
                    f'{self.log_prefix} Normalized video PTS is out of range. '
                    f'[sequence: {sequence}, expected: {playlist_start_seconds:.6f}, '
                    f'actual: {normalized_video_pts}]'
                )
                return None
            request_finished_at = time.perf_counter()
            if request_finished_at - request_started_at > 2.0:
                logging.warning(
                    f'{self.log_prefix}[Segment {sequence}] Slow video rendition response. '
                    f'[multiplexed_wait: {multiplexed_ready_at - request_started_at:.3f}s, '
                    f'normalize: {request_finished_at - multiplexed_ready_at:.3f}s]'
                )
            self._video_only_segment_cache[sequence] = stdout
            return stdout


    async def getMuxedSegment(
        self,
        sequence: int,
        request_generation: int | None = None,
    ) -> bytes | None:
        """映像と現在選択中の音声1本を同じMPEG-TSとして返す。"""

        if self.__isSegmentRequestCurrent(request_generation, update=True) is False:
            return b''
        if sequence < 0 or sequence >= len(self._segments):
            return None
        for cached_sequence in list(self._muxed_segment_cache):
            if cached_sequence < sequence - self.MAX_READED_SEGMENTS or cached_sequence > sequence + 3:
                self._muxed_segment_cache.pop(cached_sequence, None)
        if sequence in self._muxed_segment_cache:
            return self._muxed_segment_cache[sequence]
        source = await self.getSegment(sequence, request_generation)
        if source in (None, b''):
            return source
        # エンコーダー自身が映像と選択音声1本を同じタイムラインでmuxしているため、
        # 後段FFmpegで分離・再muxせず、その出力をそのまま配信する。
        self._muxed_segment_cache[sequence] = source
        return source


    async def __monitorContinuousAudioEncoding(
        self,
        rendition_id: str,
        process: asyncio.subprocess.Process,
        temporary_directory: tempfile.TemporaryDirectory[str],
        segments: list[VideoStreamSegment],
    ) -> None:
        """segment muxerの完成ファイルを配信キャッシュへ移し、先行しすぎたFFmpegへバックプレッシャーを掛ける。"""

        process_wait_task = asyncio.create_task(process.wait())
        is_stopped = False
        generated_count = 0
        try:
            for index, segment in enumerate(segments):
                extension = 'm4s' if self.encoding_options.audio_codec == 'opus' else 'ts'
                output_path = Path(temporary_directory.name) / f'{segment.sequence_index:09d}.{extension}'
                next_output_path = Path(temporary_directory.name) / f'{segment.sequence_index + 1:09d}.{extension}'
                # 次ファイルが作られた時点で現在ファイルはclose済み。最終ファイルだけプロセス終了を待つ。
                while output_path.exists() is False or (index < len(segments) - 1 and next_output_path.exists() is False):
                    if process_wait_task.done():
                        break
                    await asyncio.sleep(0.02)
                if output_path.exists() is False:
                    break
                encoded_audio = await asyncio.to_thread(output_path.read_bytes)
                if self.encoding_options.audio_codec == 'opus':
                    init_segment, encoded_audio = self.__splitFragmentedMP4(encoded_audio)
                    if init_segment:
                        self._audio_init_cache[rendition_id] = init_segment
                key = (rendition_id, segment.sequence_index)
                self._audio_segment_cache[key] = encoded_audio
                future = self._audio_segment_futures.get(key)
                if future is not None and future.done() is False:
                    future.set_result(encoded_audio)
                generated_count += 1
                await asyncio.to_thread(output_path.unlink, missing_ok=True)

                # 再生要求より先行しすぎたらプロセス自体を停止し、要求が追いついたら同じプロセスを再開する。
                while segment.sequence_index >= self._audio_requested_sequences.get(rendition_id, 0) + \
                        self.AUDIO_PREFETCH_SEGMENTS:
                    if process.returncode is not None:
                        break
                    if is_stopped is False:
                        process.send_signal(signal.SIGSTOP)
                        is_stopped = True
                    await asyncio.sleep(0.05)
                if is_stopped and process.returncode is None:
                    process.send_signal(signal.SIGCONT)
                    is_stopped = False

            await process_wait_task
            stderr = await process.stderr.read() if process.stderr is not None else b''
            if process.returncode not in (0, None):
                logging.error(f'{self.log_prefix} Continuous audio rendition failed: '
                              f'{stderr.decode(errors="ignore")}')
            logging.debug(f'{self.log_prefix} Continuous audio task finished. '
                          f'[rendition: {rendition_id}, segments: {generated_count}]')
        except asyncio.CancelledError:
            if is_stopped and process.returncode is None:
                process.send_signal(signal.SIGCONT)
            if process.returncode is None:
                process.kill()
            raise
        finally:
            for segment in segments:
                key = (rendition_id, segment.sequence_index)
                future = self._audio_segment_futures.get(key)
                if future is not None and future.done() is False:
                    future.set_result(b'')
            temporary_directory.cleanup()
            if self._audio_encoding_processes.get(rendition_id) is process:
                self._audio_encoding_processes.pop(rendition_id, None)
            if self._audio_encoding_task_refs.get(rendition_id) is asyncio.current_task():
                self._audio_encoding_task_refs.pop(rendition_id, None)


    @staticmethod
    def __splitFragmentedMP4(data: bytes) -> tuple[bytes, bytes]:
        """自己完結fMP4から初期化ボックスとmedia fragmentを分離する。"""

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


    async def getAudioInitSegment(self, rendition_id: str, sequence: int) -> bytes | None:
        """Opus fMP4の初期化セグメントを返す。"""

        if self.encoding_options.audio_codec != 'opus':
            return None
        if rendition_id not in self._audio_init_cache:
            media = await self.getAudioSegment(rendition_id, sequence)
            if media is None:
                return None
        return self._audio_init_cache.get(rendition_id)


    async def __generateSilentAudioSegment(
        self,
        rendition_id: str,
        segment: VideoStreamSegment,
        timestamp_offset: float,
    ) -> bytes | None:
        """一時的な音声なし区間を同じ時間のAAC/Opus無音で埋める。"""

        codec = getAudioCodecDefinition(self.encoding_options.audio_codec)
        args = [
            LIBRARY_PATH['FFmpeg'], '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-t', f'{segment.duration_seconds:.6f}',
            '-c:a', codec.ffmpeg_encoder, '-b:a', '192k',
        ]
        if self.encoding_options.audio_codec == 'opus':
            args += ['-application', 'audio', '-movflags', '+frag_keyframe+empty_moov+default_base_moof', '-f', 'mp4']
        else:
            args += [
                '-output_ts_offset', f'{timestamp_offset:.6f}', '-mpegts_flags', '+resend_headers',
                '-muxdelay', '0', '-muxpreload', '0', '-f', 'mpegts',
            ]
        args.append('pipe:1')
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            logging.error(f'{self.log_prefix} Silent audio generation failed: {stderr.decode(errors="ignore")}')
            return None
        if self.encoding_options.audio_codec == 'opus':
            init_segment, stdout = self.__splitFragmentedMP4(stdout)
            if init_segment:
                self._audio_init_cache[rendition_id] = init_segment
        return stdout


    def getSubtitlePlaylist(self, subtitle_index: int, cache_key: str | None = None) -> str:
        """字幕1本を単一WebVTTセグメントとして公開するプレイリストを返す。"""

        if not any(track['index'] == subtitle_index for track in self.recorded_program.recorded_video.subtitle_tracks):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Subtitle track was not found')
        cache_key = cache_key or uuid.uuid4().hex.split('-')[0]
        duration = self.recorded_program.recorded_video.duration
        return (
            '#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-PLAYLIST-TYPE:VOD\n'
            f'#EXT-X-TARGETDURATION:{math.ceil(duration)}\n#EXTINF:{duration:.6f},\n'
            f'segment?session_id={self.session_id}&cache_key={cache_key}&{self.__getCodecQuery()}\n#EXT-X-ENDLIST\n'
        )


    async def getSubtitleSegment(self, subtitle_index: int) -> bytes | None:
        """任意のテキスト字幕をWebVTTへ変換する。"""

        track = next(
            (item for item in self.recorded_program.recorded_video.subtitle_tracks if item['index'] == subtitle_index),
            None,
        )
        if track is None:
            return None
        if subtitle_index in self._subtitle_cache:
            return self._subtitle_cache[subtitle_index]
        cache_path = RECORDED_SUBTITLES_DIR / f'{self.recorded_program.recorded_video.file_hash}-{subtitle_index}.vtt'
        if cache_path.is_file():
            cached_data = await asyncio.to_thread(cache_path.read_bytes)
            self._subtitle_cache[subtitle_index] = cached_data
            return cached_data
        lock = self._subtitle_locks.setdefault(subtitle_index, asyncio.Lock())
        async with lock:
            if subtitle_index in self._subtitle_cache:
                return self._subtitle_cache[subtitle_index]
            if cache_path.is_file():
                cached_data = await asyncio.to_thread(cache_path.read_bytes)
                self._subtitle_cache[subtitle_index] = cached_data
                return cached_data
            process = await asyncio.create_subprocess_exec(
                LIBRARY_PATH['FFmpeg'], '-hide_banner', '-loglevel', 'error',
                '-i', self.recorded_program.recorded_video.file_path,
                '-map', f'0:{track["stream_index"]}', '-f', 'webvtt', 'pipe:1',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                logging.error(f'{self.log_prefix} Subtitle conversion failed: {stderr.decode(errors="ignore")}')
                return None
            await asyncio.to_thread(RECORDED_SUBTITLES_DIR.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(cache_path.write_bytes, stdout)
            self._subtitle_cache[subtitle_index] = stdout
            return stdout


    async def resolveSegmentSourcePosition(self, segment_sequence: int) -> None:
        """
        指定セグメントをエンコード開始点として使えるよう、入力ソース側の位置と DTS を解決する

        Args:
            segment_sequence (int): 解決対象セグメントのシーケンス番号
        """

        resolve_start_time = time.perf_counter()
        segment = self._segments[segment_sequence]

        # 既に解決済みなら、エンコードタスク側でそのまま利用できる
        if segment.source_start_dts is not None:
            logging.debug(
                f'{self.log_prefix}[Segment {segment_sequence}] '
                f'Segment source position already resolved. '
                f'[elapsed: {(time.perf_counter() - resolve_start_time) * 1000:.1f}ms]'
            )
            return

        async with self._source_position_lock:
            # 多重リクエストでロック待ちの間に別リクエストが解決している可能性がある
            segment = self._segments[segment_sequence]
            if segment.source_start_dts is not None:
                logging.debug(
                    f'{self.log_prefix}[Segment {segment_sequence}] '
                    f'Segment source position resolved while waiting for lock. '
                    f'[elapsed: {(time.perf_counter() - resolve_start_time) * 1000:.1f}ms]'
                )
                return

            recorded_video = self.recorded_program.recorded_video
            file_path = Path(recorded_video.file_path)

            if recorded_video.container_format == 'MPEG-TS':
                # segment_map は再生開始位置のキャッシュなので、見つかればファイル I/O なしで即座に使う
                segment_map_entry = self._segment_map_by_sequence.get(segment_sequence)
                if segment_map_entry is not None:
                    segment.source_file_position = segment_map_entry['source_file_position']
                    segment.source_start_dts = segment_map_entry['source_start_dts']
                    logging.debug(
                        f'{self.log_prefix}[Segment {segment_sequence}] '
                        f'Segment source position resolved from segment_map. '
                        f'[elapsed: {(time.perf_counter() - resolve_start_time) * 1000:.1f}ms]'
                    )
                    return

                # PAT/PMT と先頭 DTS は同一視聴セッション内で変わらないため、最初の探索時だけ読む
                if self._ts_stream_info is None:
                    self._ts_stream_info = await asyncio.to_thread(
                        TSKeyFrameSeeker.findStreamInfo,
                        file_path,
                    )
                if self._ts_source_base_dts is None:
                    self._ts_source_base_dts = await asyncio.to_thread(
                        TSKeyFrameSeeker.findBaseDTS,
                        file_path,
                        self._ts_stream_info,
                    )
                    await RecordedVideo.filter(id=recorded_video.id).update(
                        ts_source_base_dts=self._ts_source_base_dts,
                    )
                    recorded_video.ts_source_base_dts = self._ts_source_base_dts

                source_position = await asyncio.to_thread(
                    TSKeyFrameSeeker.seek,
                    file_path,
                    self._ts_stream_info,
                    segment.playlist_start_seconds,
                    self._ts_source_base_dts,
                    round(self._segment_duration_seconds * ts.HZ),
                )
                segment.source_file_position = source_position.source_file_position
                segment.source_start_dts = source_position.source_start_dts

                # オンデマンド探索の結果は次回以降のシークを軽くするため DB に保存する
                ## 保存失敗は再生失敗に直結しないため、ログだけ残してエンコード開始は続行する
                assert source_position.source_file_position is not None
                segment_map_entry = SegmentMapEntry(
                    sequence_index = segment_sequence,
                    source_file_position = source_position.source_file_position,
                    source_start_dts = source_position.source_start_dts,
                )
                await self.saveSegmentMapEntries([segment_map_entry])
                logging.info(
                    f'{self.log_prefix}[Segment {segment_sequence}] '
                    f'Segment source position resolved by TS seek. '
                    f'[elapsed: {(time.perf_counter() - resolve_start_time) * 1000:.1f}ms]'
                )
                return

            # MP4 は moov 内テーブルから同期サンプル DTS を短時間で復元できるため、DB キャッシュを作らない
            if recorded_video.container_format == 'MPEG-4' and self._mp4_keyframe_dts_list is None:
                self._mp4_keyframe_dts_list = await asyncio.to_thread(
                    MP4KeyFrameParser.readVideoKeyFrameDTS,
                    file_path,
                )
            if recorded_video.container_format == 'MPEG-4':
                assert self._mp4_keyframe_dts_list is not None
                source_start_dts = MP4KeyFrameParser.findKeyFrameDTSBefore(
                    self._mp4_keyframe_dts_list,
                    segment.playlist_start_seconds,
                )
            else:
                # Matroska / WebM / Ogg などはエンコーダー側の時刻シークを利用する
                source_start_dts = round(segment.playlist_start_seconds * ts.HZ)
            segment.source_file_position = None
            segment.source_start_dts = source_start_dts
            logging.debug(
                f'{self.log_prefix}[Segment {segment_sequence}] '
                f'Segment source position resolved from MP4 keyframe table. '
                f'[elapsed: {(time.perf_counter() - resolve_start_time) * 1000:.1f}ms]'
            )


    def createSegmentMapEntriesFromKeyFrames(self, key_frames: list[KeyFrame]) -> list[SegmentMapEntry]:
        """
        再生中に見つかった入力 TS キーフレームから segment_map 保存候補を作る

        Args:
            key_frames (list[KeyFrame]): 入力 TS から収集したキーフレーム一覧

        Returns:
            list[SegmentMapEntry]: まだ保存されていないセグメント開始位置キャッシュ
        """

        # 2つ目のキーフレームまで読めて初めて、先頭キーフレームを使える区間の終端が分かる
        ## 最後に見つかったキーフレームは、後続キーフレームが来るまで未来の全セグメントに誤割当される可能性があるため保存しない
        if len(key_frames) < 2 or self._ts_source_base_dts is None:
            return []

        segment_duration_ticks = round(self._segment_duration_seconds * ts.HZ)
        # bisect で二分探索するために DTS だけを昇順リストとして抜き出す
        dts_list = [key_frame['dts'] for key_frame in key_frames]
        segment_map_entries: list[SegmentMapEntry] = []

        # 全セグメントを走査し、キーフレーム一覧から対応する開始位置を割り当てる
        for segment in self._segments:
            # 既存 cache にあるシーケンスは、人間のシークや別セッションで既に検証済みの値として優先する
            if segment.sequence_index in self._segment_map_by_sequence:
                continue

            # プレイリスト上の時刻に対し、その時刻以前で最も近い入力キーフレームを採用する
            ## 最後の収集キーフレームは終端未確認なので、候補に使うのは次のキーフレームが存在する場合だけに限定する
            target_dts = self._ts_source_base_dts + round(segment.playlist_start_seconds * ts.HZ)
            key_frame_index = bisect.bisect_right(dts_list, target_dts) - 1
            if key_frame_index < 0 or key_frame_index >= len(key_frames) - 1:
                continue

            key_frame = key_frames[key_frame_index]
            # キーフレームがセグメント目標 DTS からどれだけ手前にあるかを算出する
            ## 1セグメント分以上離れている場合は、そのキーフレームは別セグメントの管轄なのでスキップする
            keyframe_age_ticks = target_dts - key_frame['dts']
            if keyframe_age_ticks < 0 or keyframe_age_ticks >= segment_duration_ticks:
                continue

            segment_map_entry = SegmentMapEntry(
                sequence_index = segment.sequence_index,
                source_file_position = key_frame['offset'],
                source_start_dts = key_frame['dts'],
            )

            # 同じ入力位置を複数セグメントへ保存すると、再シーク時に同じ範囲を何度も再エンコードする
            ## 既存値と今回候補の両方を見て重複を避け、長い GOP 区間は従来どおりオンデマンド探索へ任せる
            if (
                any(
                    saved_segment_map_entry['source_file_position'] == segment_map_entry['source_file_position'] and
                    saved_segment_map_entry['source_start_dts'] == segment_map_entry['source_start_dts']
                    for saved_segment_map_entry in self._segment_map_by_sequence.values()
                ) is True or
                any(
                    saved_segment_map_entry['source_file_position'] == segment_map_entry['source_file_position'] and
                    saved_segment_map_entry['source_start_dts'] == segment_map_entry['source_start_dts']
                    for saved_segment_map_entry in segment_map_entries
                ) is True
            ):
                continue

            segment_map_entries.append(segment_map_entry)

        return segment_map_entries


    async def saveSegmentMapEntries(self, segment_map_entries: list[SegmentMapEntry]) -> None:
        """
        オンデマンド探索や再生中解析で得た segment_map エントリを録画レコードへまとめて保存する

        Args:
            segment_map_entries (list[SegmentMapEntry]): 保存対象のセグメント開始位置キャッシュ
        """

        if len(segment_map_entries) == 0:
            return

        try:
            # セッション生成時に受け取った ORM インスタンスへ、保存後の最新 JSON を反映する
            ## 実際のマージは DB から読み直した RecordedVideo に対して行う
            recorded_video = self.recorded_program.recorded_video

            # 同じ録画への segment_map 保存は、DB からの読み直しから JSON 保存までを1本ずつ処理する
            ## 同一プロセス内の競合をここで止めることで、別視聴セッションが直前に保存したエントリを取りこぼさない
            ## WeakValueDictionary なので、ロックへの参照がなくなった録画 ID のエントリは自動的に消える
            segment_map_save_lock = self.__segment_map_save_locks.setdefault(recorded_video.id, asyncio.Lock())
            async with segment_map_save_lock:
                # 同一バッチ内で同じシーケンスが重複した場合は、先に見つけた値を採用する
                ## オンデマンド探索結果と再生中解析結果が混ざっても、既存値を不用意に置き換えない
                deduplicated_entries_by_sequence: dict[int, SegmentMapEntry] = {}
                for segment_map_entry in segment_map_entries:
                    if segment_map_entry['sequence_index'] in deduplicated_entries_by_sequence:
                        continue
                    deduplicated_entries_by_sequence[segment_map_entry['sequence_index']] = segment_map_entry

                if len(deduplicated_entries_by_sequence) == 0:
                    return

                # 別セッションのオンデマンド探索結果を上書きしないよう、保存直前に DB から最新値を読み直す
                ## SQLite では行ロックの効き方に制約があるため、プロセス内ロックと組み合わせて JSON 更新の取りこぼしを避ける
                async with transactions.in_transaction():
                    latest_recorded_video = await RecordedVideo.select_for_update().get_or_none(id=recorded_video.id)
                    if latest_recorded_video is None:
                        logging.warning(f'{self.log_prefix} RecordedVideo was not found while saving segment map entries.')
                        return

                    # DB から読み直した最新の segment_map をベースに、今回の候補をマージしていく
                    updated_segment_map = [
                        entry
                        for entry in latest_recorded_video.segment_map
                    ]
                    # 既存エントリのシーケンス番号と入力位置のセットを作り、重複判定に使う
                    existing_sequences = {
                        entry['sequence_index']
                        for entry in updated_segment_map
                    }
                    existing_positions = {
                        (entry['source_file_position'], entry['source_start_dts'])
                        for entry in updated_segment_map
                    }
                    saved_count = 0
                    for segment_map_entry in deduplicated_entries_by_sequence.values():
                        # 最新 DB に同じシーケンスがある場合は、別セッションが先に保存した値をそのまま使う
                        ## ついで解析の候補はローカルの古いスナップショットから作られるため、保存直前に読んだ DB の値を優先する
                        if segment_map_entry['sequence_index'] in existing_sequences:
                            continue

                        # 既存の別シーケンスと同じ入力位置を指す候補は保存しない
                        ## 長い GOP や局所的な解析不足で同一開始位置が複数セグメントに割り当たると、次回以降のシーク精度が落ちる
                        position_key = (
                            segment_map_entry['source_file_position'],
                            segment_map_entry['source_start_dts'],
                        )
                        if position_key in existing_positions:
                            continue
                        updated_segment_map.append(segment_map_entry)
                        existing_sequences.add(segment_map_entry['sequence_index'])
                        existing_positions.add(position_key)
                        saved_count += 1

                    if saved_count == 0:
                        # 追加保存する候補がなくても、最新 DB には別セッションが保存した値が含まれている可能性がある
                        ## このセッション側のキャッシュだけ古いままだと、同じ視聴中に不要なオンデマンド探索を繰り返してしまう
                        recorded_video.segment_map = updated_segment_map
                        self._segment_map_by_sequence = {
                            entry['sequence_index']: entry
                            for entry in updated_segment_map
                        }
                        return

                    updated_segment_map.sort(key=lambda entry: entry['sequence_index'])
                    latest_recorded_video.segment_map = updated_segment_map
                    await latest_recorded_video.save(update_fields=['segment_map'])

            # 保存済みの最新値を現在の視聴セッションにも反映し、次回の同じセグメント要求を DB なしで返す
            recorded_video.segment_map = updated_segment_map
            self._segment_map_by_sequence = {
                entry['sequence_index']: entry
                for entry in updated_segment_map
            }
            logging.info(f'{self.log_prefix} Segment map entries saved. [count: {saved_count}]')
        except Exception as ex:
            logging.warning(f'{self.log_prefix} Failed to save segment map entries:', exc_info=ex)


    async def getSegment(
        self,
        segment_sequence: int,
        request_generation: int | None = None,
    ) -> bytes | None:
        """
        エンコードされた HLS セグメントを取得する
        呼び出された時点でエンコードされていない場合は既存のエンコードタスクを終了し、
        segment_sequence の HLS セグメントが含まれる範囲から新たにエンコードタスクを開始する

        Args:
            segment_sequence (int): HLS セグメントのシーケンス番号 (self.segments のインデックスと一致する)

        Returns:
            bytes | None: HLS セグメントとしてエンコードされた MPEG-TS ストリーム (シーケンス番号が不正な場合は None)
        """

        # セッションのアクティブ状態を維持する
        self.keepAlive()
        if self.__isSegmentRequestCurrent(request_generation, update=True) is False:
            logging.debug(
                f'{self.log_prefix}[Segment {segment_sequence}] Ignored stale multiplexed segment request. '
                f'[request_generation: {request_generation}, latest: {self._latest_request_generation}]'
            )
            return b''

        # セグメントのシーケンス番号が不正な場合は None を返す
        if segment_sequence < 0 or segment_sequence >= len(self._segments):
            return None
        self._latest_requested_sequence = segment_sequence

        if self.recorded_program.recorded_video.has_video is False:
            renditions = self.getAudioRenditions()
            selected_id = self.encoding_options.audio_rendition_id
            if not any(str(item['id']) == selected_id for item in renditions):
                selected_id = str(renditions[0]['id']) if renditions else None
            return await self.getAudioSegment(selected_id, segment_sequence) if selected_id is not None else None

        # シーケンス番号に対応する HLS セグメントを取得する
        segment = self._segments[segment_sequence]

        # 非TSコンテナは libavformat で直接シークし、HWEncC -> SW decode -> FFmpeg の順で試す
        if self.recorded_program.recorded_video.container_format != 'MPEG-TS':
            if segment.encode_status == 'Pending':
                async with self._video_encoding_task_lock:
                    if segment.encode_status == 'Pending':
                        segment.encode_status = 'Encoding'
                        encoded = await self.__encodeDirectInputSegment(segment)
                        segment.encode_status = 'Completed'
                        if not segment.encoded_segment_ts_future.done():
                            segment.encoded_segment_ts_future.set_result(encoded or b'')
            encoded_segment_ts = await asyncio.shield(segment.encoded_segment_ts_future)
            segment.is_encoded_segment_ts_future_readed = True
            self.keepAlive()
            return encoded_segment_ts or None

        # 当該セグメントのエンコードがまだ完了していない場合は、エンコードタスクを非同期で開始する
        if segment.encode_status == 'Pending':
            async with self._video_encoding_task_lock:
                # ロック待ちの間に新しいシーク世代が到着した場合、この古い要求ではタスクを触らない。
                if self.__isSegmentRequestCurrent(request_generation) is False:
                    return b''
                # ロック待ちの間に他のリクエストがすでにエンコードを開始している可能性があるため再確認する
                if segment.encode_status == 'Pending':
                    # シークでは旧エンコーダーが同じ録画ファイルを読み続けていると、未キャッシュ区間の探索と I/O が競合する
                    ## そのため source position 解決より前に旧タスクへキャンセルを投げ、探索が録画ファイルを読みやすい状態へ寄せる
                    if self._video_encoding_task_ref is not None:
                        await self.__cancelVideoEncodingTask()
                        logging.info(
                            f'{self.log_prefix}[Segment {segment_sequence}] '
                            f'Previous Encoding Task Canceled before source position resolution.'
                        )

                    if self.__isSegmentRequestCurrent(request_generation) is False:
                        return b''

                    # 新タスク作成後にFutureを交換すると、この要求が古いFutureを待ち続ける。
                    # 旧タスクの終了後、新タスクを起動する前に全セグメント状態を同期的に初期化する。
                    for reset_target in self._segments:
                        if reset_target.encode_status != 'Pending':
                            await reset_target.resetState()
                    segment = self._segments[segment_sequence]

                    # このセグメントからエンコーダーを起動するため、入力ソース上の開始位置を先に確定する
                    await self.resolveSegmentSourcePosition(segment_sequence)
                    if self.__isSegmentRequestCurrent(request_generation) is False:
                        return b''
                    encoding_start_sequence = segment_sequence

                    # QSVEncC では MPEG-TS の入力 DTS が 33bit ラップ直前にある状態で起動すると、
                    ## `check_pts()` が後続フレームの時刻を逆行扱いして小刻みな PTS 補正を入れてしまい、結果盛大に音ズレする既知の問題がある
                    ## 同一ファイルでも FFmpeg / NVEncC では正常な間隔でエンコードできているため、QSVEncC のみ少し手前から連続エンコードする
                    ## 映像ストリーム構成が途中で変わる録画は VideoEncodingTask 側で FFmpeg に固定されるため、この QSVEncC 専用の回避策は適用不要
                    if (
                        Config().general.encoder == 'QSVEncC' and
                        self.recorded_program.recorded_video.container_format == 'MPEG-TS' and
                        self.recorded_program.recorded_video.has_video_stream_changes is False
                    ):
                        wrap_avoidance_ticks = self.DTS_WRAP_AVOIDANCE_SECONDS * ts.HZ
                        backtrack_iterations = 0
                        while encoding_start_sequence > 0:
                            backtrack_iterations += 1
                            if backtrack_iterations > self.DTS_WRAP_AVOIDANCE_MAX_BACKTRACK_SEGMENTS:
                                logging.warning(
                                    f'{self.log_prefix}[Segment {segment_sequence}] '
                                    f'QSVEncC DTS wrap avoidance reached the backtrack limit. '
                                    f'[encoding_start_sequence: {encoding_start_sequence}]'
                                )
                                break
                            encoding_start_segment = self._segments[encoding_start_sequence]
                            if encoding_start_segment.source_start_dts is None:
                                await self.resolveSegmentSourcePosition(encoding_start_sequence)
                                encoding_start_segment = self._segments[encoding_start_sequence]
                            assert encoding_start_segment.source_start_dts is not None
                            distance_to_wrap = ts.PCR_CYCLE - (encoding_start_segment.source_start_dts % ts.PCR_CYCLE)
                            if distance_to_wrap > wrap_avoidance_ticks:
                                break
                            encoding_start_sequence -= 1

                        if encoding_start_sequence != segment_sequence:
                            logging.info(
                                f'{self.log_prefix}[Segment {segment_sequence}] '
                                f'QSVEncC start adjusted to Segment {encoding_start_sequence} to avoid DTS wrap.',
                            )

                    # 新しいエンコードタスクのインスタンスを初期化
                    ## エンコードタスクは基本使い回せないので、再度新しく初期化する
                    self._video_encoding_task = VideoEncodingTask(self)

                    # 新しいエンコードタスクを開始
                    self._video_encoding_task_ref = asyncio.create_task(self._video_encoding_task.run(encoding_start_sequence))
                    self.__registerVideoEncodingTaskRef(self._video_encoding_task_ref)
                    logging.info(f'{self.log_prefix}[Segment {encoding_start_sequence}] New Encoding Task Started.')

        # セグメントデータの Future が完了したらそのデータを返す
        encoded_segment_ts = await asyncio.shield(segment.encoded_segment_ts_future)
        segment.is_encoded_segment_ts_future_readed = True

        # 読み取り済みのセグメントが MAX_READED_SEGMENTS 個以上ある場合、一番古いセグメントのデータを初期化する
        readed_segments = [s for s in self._segments if s.is_encoded_segment_ts_future_readed]
        if len(readed_segments) >= self.MAX_READED_SEGMENTS:
            # 一番古いセグメントを取得し、状態をリセットする
            oldest_segment = readed_segments[0]
            await oldest_segment.resetState()
            logging.info(f'{self.log_prefix}[Segment {oldest_segment.sequence_index}] Reset segment data to free memory.')

        self.keepAlive()
        return encoded_segment_ts


    async def __encodeDirectInputSegment(self, segment: VideoStreamSegment) -> bytes | None:
        """MP4/Matroska/WebM/Ogg 等をエンコーダーへ直接入力して1セグメント生成する。"""

        encoding_task = VideoEncodingTask(self)
        encoding_task.selectAudioRenditionForSequence(segment.sequence_index)
        profile = encoding_task.getEncodingProfile()
        encoder_type = Config().general.encoder_bs4k if profile.is_bs4k else Config().general.encoder
        start = segment.playlist_start_seconds
        duration = segment.duration_seconds
        output_offset = start

        async def Run(args: list[str], executable: str, label: str) -> bytes | None:
            logging.info(f'{self.log_prefix}[Segment {segment.sequence_index}] {label} direct input command started.')
            process = await asyncio.create_subprocess_exec(
                executable, *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
            except TimeoutError:
                process.kill()
                await process.wait()
                logging.warning(f'{self.log_prefix} {label} direct input timed out.')
                return None
            if process.returncode != 0 or len(stdout) == 0:
                logging.warning(
                    f'{self.log_prefix} {label} direct input failed: '
                    f'{stderr.decode("utf-8", errors="ignore")[-4000:]}'
                )
                return None
            return stdout

        if encoder_type != 'FFmpeg':
            base_args = encoding_task.buildHWEncCOptions(self.quality, encoder_type, output_offset)
            input_index = base_args.index('--input')
            base_args = base_args[input_index + 2:]
            frame_count = max(1, math.ceil(duration * max(self.recorded_program.recorded_video.video_frame_rate or 0, 1)))
            for decode_mode in ['--avhw', '--avsw']:
                args = [decode_mode, '--seek', f'{start:.6f}', '--input', self.recorded_program.recorded_video.file_path]
                args += [item for item in base_args if item not in ['--avhw', '--avsw']]
                if '--data-copy' in args:
                    data_copy_index = args.index('--data-copy')
                    del args[data_copy_index:data_copy_index + 2]
                output_index = args.index('--output')
                args[output_index:output_index] = ['--frames', str(frame_count)]
                encoded = await Run(args, LIBRARY_PATH[encoder_type], f'{encoder_type} {decode_mode}')
                if encoded is not None:
                    return encoded

        ffmpeg_args = encoding_task.buildFFmpegOptions(self.quality, output_offset)
        input_index = ffmpeg_args.index('-i')
        ffmpeg_args = ffmpeg_args[input_index + 2:]
        ffmpeg_args = [
            '-hide_banner', '-loglevel', 'error', '-ss', f'{start:.6f}',
            '-i', self.recorded_program.recorded_video.file_path,
            *ffmpeg_args,
        ]
        output_index = ffmpeg_args.index('-y')
        ffmpeg_args[output_index:output_index] = ['-t', f'{duration:.6f}']
        return await Run(ffmpeg_args, LIBRARY_PATH['FFmpeg'], 'FFmpeg')


    async def __cancelVideoEncodingTask(
        self,
        should_wait_for_runner: bool = True,
        timeout_seconds: float = 0.5,
    ) -> None:
        """
        現在のエンコードタスクをキャンセルし、必要に応じて短時間だけ終了を待機する

        Args:
            should_wait_for_runner (bool): エンコードタスク本体の終了を待機するかどうか
            timeout_seconds (float): エンコードタスクの終了を待機する最大時間 (秒)
        """

        # 現在実行中のエンコードタスクをキャンセルする
        self._video_encoding_task.cancel()

        # この時点で既にエンコードタスクが実行中でない場合は何もしない
        if self._video_encoding_task_ref is None:
            return

        # 待機対象をローカル変数に退避しておく
        ## cancelEncodingTask() 完了後に新しいタスクへ差し替えられても、旧タスクの参照を見失わないようにする
        encoding_task_runner = self._video_encoding_task_ref

        # 録画再生の critical path では旧タスクの完全終了を待たず、
        ## 強参照だけ detached set に移して自然終了に任せる
        if should_wait_for_runner is False:
            self.__detachVideoEncodingTaskRef(encoding_task_runner)
            if self._video_encoding_task_ref == encoding_task_runner:
                self._video_encoding_task_ref = None
            return

        # エンコードタスクの終了を待機する
        try:
            await asyncio.wait_for(asyncio.shield(encoding_task_runner), timeout=timeout_seconds)
        except TimeoutError:
            # タイムアウト後も旧タスクは自然終了を続ける可能性があるため、
            # 新しいタスクを起動する前に強参照を別管理へ移し、完了時に解放する
            self.__detachVideoEncodingTaskRef(encoding_task_runner)
            if self._video_encoding_task_ref == encoding_task_runner:
                self._video_encoding_task_ref = None
            logging.warning(f'{self.log_prefix} Encoding task shutdown timed out. Proceeding with restart.')
        except Exception as ex:
            # 予期しない例外で終了待機に失敗した場合も、旧タスクの参照を見失うと dangling-task が再発する
            self.__detachVideoEncodingTaskRef(encoding_task_runner)
            if self._video_encoding_task_ref == encoding_task_runner:
                self._video_encoding_task_ref = None
            logging.error(f'{self.log_prefix} Error while waiting for encoding task shutdown:', exc_info=ex)
        finally:
            if encoding_task_runner.done() and self._video_encoding_task_ref == encoding_task_runner:
                self._video_encoding_task_ref = None


    async def destroy(self) -> None:
        """
        録画視聴セッションで実行中のエンコードなどの処理を終了し、録画視聴セッションを破棄する
        ユーザーが番組の視聴を終了した (keepAlive() が呼び出されなくなった) 場合に自動的に呼び出される
        """

        # 起動中のエンコードタスクがあればキャンセルする
        # この時点ですでにエンコードを完了して終了している場合もある
        async with self._video_encoding_task_lock:
            await self.__cancelVideoEncodingTask()

        # 音声レンディションの継続FFmpegと監視タスクもすべて終了する。
        for process in self._audio_encoding_processes.values():
            if process.returncode is None:
                process.kill()
        if self._opus_encoding_process is not None and self._opus_encoding_process.returncode is None:
            self._opus_encoding_process.kill()
        if self._opus_encoding_task_ref is not None and self._opus_encoding_task_ref.done() is False:
            self._opus_encoding_task_ref.cancel()
        audio_tasks = list(self._audio_encoding_task_refs.values())
        for task in audio_tasks:
            if task.done() is False:
                task.cancel()
        if audio_tasks:
            await asyncio.gather(*audio_tasks, return_exceptions=True)
        if self._opus_encoding_task_ref is not None:
            await asyncio.gather(self._opus_encoding_task_ref, return_exceptions=True)
            self._opus_encoding_task_ref = None
        self._audio_encoding_processes.clear()
        self._audio_encoding_task_refs.clear()

        # すべての HLS セグメントを削除する
        self._segments = []

        # アクティブな間保持されていたインスタンスを削除する
        ## これにより、このインスタンスには誰も参照できなくなるため、ガベージコレクションによりメモリから解放される (はず)
        ## 今後同じセッション ID が指定された場合は新たに別のインスタンスが生成される
        self.__instances.pop(self.session_id)

        logging.info(f'{self.log_prefix} Streaming Session Finished.')
