
# Type Hints を指定できるように
# ref: https://stackoverflow.com/a/33533514/17124142
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import ClassVar, Literal

from hashids import Hashids

from app import logging
from app.config import Config
from app.constants import QUALITY, QUALITY_TYPES
from app.schemas import LivePlaybackSessionRetireResponse, LiveStreamStatus
from app.streams.KonomiTVBS4KPlaybackEncoding import (
    KonomiTVBS4KAudioCodec,
    KonomiTVBS4KVideoBitDepth,
    KonomiTVBS4KVideoCodec,
)
from app.streams.LiveEncodingTask import LiveEncodingTask
from app.streams.LivePrepareCoordinator import (
    LIVE_PREPARE_COORDINATOR,
    LivePrepareFinalizeResult,
    LivePrepareLeaseError,
)
from app.streams.LivePSIDataArchiver import LivePSIDataArchiver
from app.streams.LiveSourceCoordinator import (
    LiveSourceSubscriberError,
    LiveSourceSubscription,
)
from app.streams.LiveStreamTelemetry import LiveStreamTelemetry
from app.streams.StreamEncodingOptions import StreamEncodingOptions
from app.utils.edcb.EDCBTuner import EDCBTuner


KonomiTVBS4KLiveStreamKey = tuple[
    str,
    QUALITY_TYPES,
    KonomiTVBS4KVideoCodec,
    KonomiTVBS4KVideoBitDepth,
    KonomiTVBS4KAudioCodec,
    bool,
]
KonomiTVBS4KLiveStreamInstanceKey = tuple[
    str,
    QUALITY_TYPES,
    KonomiTVBS4KVideoCodec,
    KonomiTVBS4KVideoBitDepth,
    KonomiTVBS4KAudioCodec,
    bool,
    bool,
]
LivePlaybackSessionRetireResult = Literal[
    'cleanup_completed',
    'shared_encoder_retained',
    'session_ambiguous',
    'cleanup_timeout',
    'cleanup_failed',
]


class LiveStreamClient:
    """ ライブストリームのクライアントを表すクラス """

    def __init__(
        self,
        live_stream: LiveStream,
        client_type: Literal['mpegts'],
        prepare_token: str | None = None,
        playback_session_id: str | None = None,
    ) -> None:
        """
        ライブストリーミングクライアントのインスタンスを初期化する
        LiveStreamClient は LiveStream クラス外から初期化してはいけない
        (必ず LiveStream.connect() で取得した LiveStreamClient を利用すること)

        Args:
            live_stream (LiveStream): クライアントが紐づくライブストリームのインスタンス
            client_type (Literal['mpegts']): クライアントの種別 (mpegts, ll-hls クライアントは廃止された)
            prepare_token (str | None): 二段階切り替え B に結び付いた Prepare lease token。
            playback_session_id (str | None): ブラウザ pipeline が生成した接続 UUID。

        Returns:
            None
        """

        # このクライアントが紐づくライブストリームのインスタンス
        self._live_stream: LiveStream = live_stream

        # クライアント ID
        ## ミリ秒単位のタイムスタンプをもとに、Hashids による10文字のユニーク ID が生成される
        self.client_id: str = 'MPEGTS-' + Hashids(min_length=10).encode(int(time.time() * 1000))

        # クライアントの種別 (mpegts)
        self.client_type: Literal['mpegts'] = client_type

        # 二段階 B を起動したクライアントだけに単回消費済み token を結び付ける
        self.prepare_token: str | None = prepare_token

        # 再接続をまたいで同じブラウザ pipeline を識別する UUID
        self.playback_session_id: str | None = playback_session_id

        # ストリームデータが入る Queue
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        # ストリームデータの最終読み取り時刻のタイミング
        ## 最終読み取り時刻から 10 秒経過したクライアントは LiveStream.writeStreamData() でタイムアウトと判断され、削除される
        self._stream_data_read_at: float = time.time()


    @property
    def stream_data_read_at(self) -> float:
        """ ストリームデータの最終読み取り時刻のタイミング (読み取り専用) """
        return self._stream_data_read_at


    async def readStreamData(self) -> bytes | None:
        """
        自分自身の Queue からストリームデータを読み取って返す
        Queue 内のストリームデータは writeStreamData() で書き込まれたもの

        Args:
            client (LiveStreamClient): ライブストリームクライアントのインスタンス

        Returns:
            bytes | None: ストリームデータ (エンコードタスクが終了した場合は None が返る)
        """

        # mpegts クライアント以外では実行しない
        if self.client_type != 'mpegts':
            return None

        # ストリームデータの最終読み取り時刻を更新
        self._stream_data_read_at = time.time()

        # Queue から読み取ったストリームデータを返す
        try:
            return await self._queue.get()
        except TypeError:
            return None


    def writeStreamData(self, stream_data: bytes | None) -> None:
        """
        自分自身の Queue にストリームデータを書き込む
        Queue 内のストリームデータは readStreamData() で読み取られる

        Args:
            stream_data (bytes): 書き込むストリームデータ (エンコードタスクが終了した場合は None を渡す)
        """

        # mpegts クライアント以外では実行しない
        if self.client_type != 'mpegts':
            return None

        # Queue にストリームデータを書き込む
        # EOF は滞留データの後ろへ積まず、待機中の StreamingResponse を直ちに終了させる。
        if stream_data is None:
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        self._queue.put_nowait(stream_data)


class LiveStream:
    """ ライブストリームを管理するクラス """

    # codec・bit depth・音声まで含む内部キーからライブストリームを一意に引く。
    # この辞書にライブストリームに関する全てのデータが格納されている
    __instances: ClassVar[dict[KonomiTVBS4KLiveStreamInstanceKey, LiveStream]] = {}
    PLAYBACK_SESSION_RETIRE_TTL_SECONDS = 60.0
    PLAYBACK_SESSION_RETIRE_MAX_ENTRIES = 256
    PLAYBACK_SESSION_RETIRE_MAX_INFLIGHT = 16
    PLAYBACK_SESSION_CLEANUP_TIMEOUT_SECONDS = 20.0
    PLAYBACK_SESSION_PENDING_WAIT_SECONDS = 1.0


    # 必ずライブストリーム ID ごとに1つのインスタンスになるように (Singleton)
    def __new__(
        cls,
        display_channel_id: str,
        quality: QUALITY_TYPES,
        encoding_options: StreamEncodingOptions | None = None,
        stream_anchor_enabled: bool = True,
    ) -> LiveStream:

        # まだ同じライブストリーム ID のインスタンスがないときだけ、インスタンスを生成する
        # (チャンネル ID)-(映像の品質)-(追加エンコードオプション) で一意な ID になる
        """
        条件に対応するインスタンスを取得または生成する。

        Args:
            display_channel_id (str): 対象チャンネルの表示用 ID。
            quality (QUALITY_TYPES): 処理または検証対象の画質。
            encoding_options (StreamEncodingOptions | None): codec・bit depth・低遅延設定を含むエンコード条件。
            stream_anchor_enabled (bool): 最終 TS に Stream Anchor v1 を付与するか。

        Returns:
            LiveStream: 同一条件で再利用または新規生成した stream。
        """
        if encoding_options is None:
            encoding_options = StreamEncodingOptions(
                video_codec = 'hevc' if QUALITY[quality].is_hevc else 'avc',
            )
        live_stream_key: KonomiTVBS4KLiveStreamKey = (
            display_channel_id,
            quality,
            encoding_options.video_codec,
            encoding_options.video_bit_depth,
            encoding_options.audio_codec,
            encoding_options.is_24fps_mode_enabled,
        )
        instance_key: KonomiTVBS4KLiveStreamInstanceKey = (
            *live_stream_key,
            stream_anchor_enabled,
        )
        if instance_key not in cls.__instances:

            # 新しいライブストリームのインスタンスを生成する
            instance = super().__new__(cls)

            # 外向けIDは既定AVC/AACの従来表記を保ち、高度codecだけ可読suffixで区別する。
            live_stream_id = (
                f'{display_channel_id}-{quality}{encoding_options.buildSuffix()}'
                f'{"-compat" if stream_anchor_enabled is False else ""}'
            )
            instance.live_stream_id = live_stream_id
            instance.live_stream_key = live_stream_key

            # チャンネル ID と映像の品質を設定
            instance.display_channel_id = display_channel_id
            instance.quality = quality
            instance.encoding_options = encoding_options
            instance.stream_anchor_enabled = stream_anchor_enabled

            # ライブストリームクライアントが入るリスト
            ## クライアントの接続が切断された場合、このリストからも削除される
            ## したがって、クライアントの数はこのリストの長さで求められる
            instance._clients = []

            # ストリームのステータス
            ## Offline, Standby, ONAir, Idling, Restart のいずれか
            instance._status = 'Offline'

            # ストリームのステータス詳細
            instance._detail = 'ライブストリームは Offline です。'

            # ストリームの開始時刻
            instance._started_at = 0

            # ストリームのステータスの最終更新時刻のタイムスタンプ
            instance._updated_at = 0

            # ストリームデータの最終書き込み時刻のタイムスタンプ
            ## 最終書き込み時刻が 5 秒 (ONAir 時) 20 秒 (Standby 時) 以上更新されていない場合は、
            ## エンコーダーがフリーズしたものとみなしてエンコードタスクを再起動する
            instance._stream_data_written_at = 0

            # Prepare lease・エンコーダー起動・最終出力 Anchor の観測値
            instance._telemetry = LiveStreamTelemetry()
            instance._prepare_token = None
            instance._prepare_expiry_task = None
            instance._prepare_release_tasks = set()
            instance._prepare_cleanup_followup_tasks = {}
            instance._source_subscription = None
            instance._retired_playback_sessions = {}
            instance._retirement_cleanup_task_ref = None
            instance._last_live_encoding_cleanup_confirmed = None
            instance._playback_session_retire_tasks = {}
            instance._playback_session_retire_api_tasks = {}
            instance._playback_session_retire_api_completed_at = {}
            instance._is_shutting_down = False

            # 実行中の LiveEncodingTask のタスクへの参照
            ## ライブ再生では LiveEncodingTask.run() 自体が再起動制御まで内包しており、
            ## self._live_encoding_task_ref.cancel() は停止が間に合わなかった場合の保険としての非同期タスクキャンセルなので、
            ## VideoStream と異なり LiveEncodingTask インスタンス自体の参照は保持しない設計としている
            # ref: https://docs.astral.sh/ruff/rules/asyncio-dangling-task/
            instance._live_encoding_task_ref = None
            instance._live_encoding_task_instance_ref = None
            # 終了待機がタイムアウトした古い LiveEncodingTask のタスクへの参照
            # イベントループ上の Task は弱参照で管理されるため、自然終了するまでここで強参照を保持する
            instance._detached_live_encoding_task_refs = set()

            # PSI/SI データアーカイバーのインスタンス
            ## LiveStreamsRouter からアクセスする必要があるためここに設置している
            instance.psi_data_archiver = None

            # EDCB バックエンドのチューナーインスタンス
            ## Mirakurun バックエンドを使っている場合は None のまま
            instance.tuner = None

            # チューナー再利用時の排他ロック
            ## チューナー再利用の競合を避けるため、LiveStream ごとにロックを持つ
            instance._tuner_lock = asyncio.Lock()
            # session retire と新規接続の status 判定・task 起動・client 登録を世代単位で直列化する
            instance._retirement_lock = asyncio.Lock()

            # 生成したインスタンスを登録する
            cls.__instances[instance_key] = instance

        # 登録されているインスタンスを返す
        return cls.__instances[instance_key]


    def __init__(
        self,
        display_channel_id: str,
        quality: QUALITY_TYPES,
        encoding_options: StreamEncodingOptions | None = None,
        stream_anchor_enabled: bool = True,
    ) -> None:
        """
        ライブストリームのインスタンスを取得する

        Args:
            display_channel_id (str): チャンネルID
            quality (QUALITY_TYPES): 映像の品質 (1080p-60fps ~ 240p)
            encoding_options (StreamEncodingOptions | None): ベース画質に追加するエンコードオプション
            stream_anchor_enabled (bool): 最終 Anchor v1 を Bridge で確定するかどうか

        Returns:
            None
        """

        # インスタンス変数の型ヒントを定義
        # Singleton のためインスタンスの生成は __new__() で行うが、__init__() も定義しておかないと補完がうまく効かない
        self.live_stream_id: str
        self.live_stream_key: KonomiTVBS4KLiveStreamKey
        self.display_channel_id: str
        self.quality: QUALITY_TYPES
        self.encoding_options: StreamEncodingOptions
        self.stream_anchor_enabled: bool
        self._clients: list[LiveStreamClient]
        self._status: Literal['Offline', 'Standby', 'ONAir', 'Idling', 'Restart']
        self._detail: str
        self._started_at: float
        self._updated_at: float
        self._stream_data_written_at: float
        self._telemetry: LiveStreamTelemetry
        self._prepare_token: str | None
        self._prepare_expiry_task: asyncio.Task[None] | None
        self._prepare_release_tasks: set[asyncio.Task[bool]]
        self._prepare_cleanup_followup_tasks: dict[str, asyncio.Task[bool]]
        self._source_subscription: LiveSourceSubscription | None
        self._retired_playback_sessions: dict[str, float]
        self._retirement_cleanup_task_ref: asyncio.Task[None] | None
        self._last_live_encoding_cleanup_confirmed: bool | None
        self._playback_session_retire_tasks: dict[str, asyncio.Task[LivePlaybackSessionRetireResult]]
        self._playback_session_retire_api_tasks: dict[
            str,
            asyncio.Task[LivePlaybackSessionRetireResponse],
        ]
        self._playback_session_retire_api_completed_at: dict[str, float]
        self._is_shutting_down: bool
        self._live_encoding_task_ref: asyncio.Task[None] | None
        self._live_encoding_task_instance_ref: LiveEncodingTask | None
        self._detached_live_encoding_task_refs: set[asyncio.Task[None]]
        self.psi_data_archiver: LivePSIDataArchiver | None
        self.tuner: EDCBTuner | None
        self._tuner_lock: asyncio.Lock
        self._retirement_lock: asyncio.Lock


    @property
    def log_prefix(self) -> str:
        """
        ログのプレフィックス
        """

        return f'[Live: {self.live_stream_id}]'


    def __registerLiveEncodingTaskRef(
        self,
        live_encoding_task_ref: asyncio.Task[None],
        live_encoding_task: LiveEncodingTask | None = None,
    ) -> None:
        """
        LiveEncodingTask の完了時に不要な参照を解放するコールバックを登録する

        Args:
            live_encoding_task_ref (asyncio.Task[None]): 参照管理対象の LiveEncodingTask
            live_encoding_task (LiveEncodingTask | None): cleanup 結果を公開する実行インスタンス。
        """

        def OnLiveEncodingTaskDone(done_task: asyncio.Task[None]) -> None:
            """
            task終了時にcleanup結果と参照を確定する。

            Args:
                done_task (asyncio.Task[None]): 完了したLiveEncodingTask。

            Returns:
                None
            """

            if live_encoding_task is not None:
                cleanup_confirmed = live_encoding_task.isCleanupConfirmed()
                if self._last_live_encoding_cleanup_confirmed is not False:
                    self._last_live_encoding_cleanup_confirmed = cleanup_confirmed
                if cleanup_confirmed is False:
                    logging.warning(
                        f'{self.log_prefix} Encoding task cleanup was incomplete: '
                        f'{", ".join(live_encoding_task.getCleanupFailures()) or "unknown"}.'
                    )
            self._detached_live_encoding_task_refs.discard(done_task)
            # 現在実行中の LiveEncodingTask のタスクが終了待機を打ち切ったタスクだった場合は、その参照を None にする
            if self._live_encoding_task_ref == done_task:
                self._live_encoding_task_ref = None

        live_encoding_task_ref.add_done_callback(OnLiveEncodingTaskDone)


    def __detachLiveEncodingTaskRef(self, live_encoding_task_ref: asyncio.Task[None]) -> None:
        """
        終了待機を打ち切った LiveEncodingTask のタスクへの参照を保持する

        Args:
            live_encoding_task_ref (asyncio.Task[None]): 自然終了待ちに移行する LiveEncodingTask
        """

        # asyncio.wait() のtimeout判定直後にtaskだけが完了し、cleanup結果を反映するdone callbackが
        # 次tick待ちになる競合もある。完了済みでも一度setへ入れ、connect()側にcallback反映tickを要求する。
        self._detached_live_encoding_task_refs.add(live_encoding_task_ref)
        live_encoding_task_ref.add_done_callback(self._detached_live_encoding_task_refs.discard)


    @classmethod
    def getAllLiveStreams(cls) -> list[LiveStream]:
        """
        全てのライブストリームのインスタンスを取得する

        Returns:
            list[LiveStream]: ライブストリームのインスタンスの入ったリスト
        """

        # __instances 辞書を values() で値だけのリストにしたものを返す
        return list(cls.__instances.values())


    async def shutdown(self) -> bool:
        """
        全 client に EOF を送り、実行中 encoder と分離済み cleanup task を終了待ちする。

        Returns:
            bool: encoder と background cleanup をすべて確認できた場合は True。
        """

        async with self._retirement_lock:
            self._is_shutting_down = True
            self.setStatus('Offline', 'サーバー終了のためライブストリームを停止します。')

            prepare_expiry_task = self._prepare_expiry_task
            self._prepare_expiry_task = None
            if prepare_expiry_task is not None:
                prepare_expiry_task.cancel()
                try:
                    await prepare_expiry_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logging.warning(f'{self.log_prefix} Prepare expiry task failed during shutdown.')

            self.disconnectAll()

            live_encoding_task = self._live_encoding_task_ref
            live_encoding_task_instance = self._live_encoding_task_instance_ref
            encoding_tasks = {
                task
                for task in (
                    live_encoding_task,
                    *self._detached_live_encoding_task_refs,
                )
                if task is not None
            }
            for encoding_task in encoding_tasks:
                if encoding_task.done() is False:
                    encoding_task.cancel()
            if live_encoding_task is not None:
                self._retirement_cleanup_task_ref = live_encoding_task
            if len(encoding_tasks) > 0:
                done, _pending = await asyncio.wait(
                    encoding_tasks,
                    timeout = self.PLAYBACK_SESSION_CLEANUP_TIMEOUT_SECONDS,
                )
                if len(done) != len(encoding_tasks):
                    logging.warning(
                        f'{self.log_prefix} One or more encoding task cleanups timed out during shutdown.'
                    )
                for done_task in done:
                    try:
                        done_task.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        logging.warning(
                            f'{self.log_prefix} Encoding task ended with an error during shutdown.'
                        )
                if live_encoding_task is not None and live_encoding_task.done() is True:
                    if live_encoding_task_instance is not None:
                        if self._last_live_encoding_cleanup_confirmed is not False:
                            self._last_live_encoding_cleanup_confirmed = (
                                live_encoding_task_instance.isCleanupConfirmed()
                            )
                    if self._live_encoding_task_ref is live_encoding_task:
                        self._live_encoding_task_ref = None
                    if self._retirement_cleanup_task_ref is live_encoding_task:
                        self._retirement_cleanup_task_ref = None

            # task参照が既に消えた異常状態でも、共有source購読だけを最後に必ず閉じる。
            if live_encoding_task is None or live_encoding_task.done() is True:
                source_subscription = self._source_subscription
                if source_subscription is not None:
                    try:
                        await source_subscription.close(keep_source_alive=False)
                    except Exception:
                        self._last_live_encoding_cleanup_confirmed = False
                        logging.warning(
                            f'{self.log_prefix} Failed to close source subscription during shutdown fallback.'
                        )
                    finally:
                        if self._source_subscription is source_subscription:
                            self._source_subscription = None

        cleanup_tasks_drained = await self.drainCleanupTasks()
        return (
            cleanup_tasks_drained is True
            and self._last_live_encoding_cleanup_confirmed is not False
        )


    async def drainCleanupTasks(self, *, timeout_seconds: float = 30.0) -> bool:
        """
        request から分離した session Retire・Prepare・encoder cleanup task を終了待ちする。

        Args:
            timeout_seconds (float): 新たに派生した task も含めて待つ最大秒数。

        Returns:
            bool: 待機対象がすべて完了した場合は True。
        """

        if timeout_seconds <= 0:
            raise ValueError('timeout_seconds must be greater than zero.')
        deadline = time.monotonic() + timeout_seconds
        current_task = asyncio.current_task()
        while True:
            tasks = {
                task
                for task in (
                    *self._prepare_release_tasks,
                    *self._prepare_cleanup_followup_tasks.values(),
                    *self._playback_session_retire_tasks.values(),
                    *self._playback_session_retire_api_tasks.values(),
                    *self._detached_live_encoding_task_refs,
                    self._prepare_expiry_task,
                    self._retirement_cleanup_task_ref,
                    self._live_encoding_task_ref,
                )
                if task is not None and task is not current_task and task.done() is False
            }
            if len(tasks) == 0:
                await asyncio.sleep(0)
                self._prepare_release_tasks = {
                    task for task in self._prepare_release_tasks if task.done() is False
                }
                for token, followup_task in tuple(self._prepare_cleanup_followup_tasks.items()):
                    if followup_task.done() is True:
                        self._prepare_cleanup_followup_tasks.pop(token, None)
                for playback_session_id, retire_task in tuple(self._playback_session_retire_tasks.items()):
                    if retire_task.done() is True:
                        self._playback_session_retire_tasks.pop(playback_session_id, None)
                self.__prunePlaybackSessionRetireAPITasks()
                remaining = (
                    len(self._prepare_release_tasks) > 0
                    or any(
                        task.done() is False
                        for task in self._prepare_cleanup_followup_tasks.values()
                    )
                    or any(
                        task.done() is False
                        for task in self._playback_session_retire_tasks.values()
                    )
                    or any(
                        task.done() is False
                        for task in self._playback_session_retire_api_tasks.values()
                    )
                    or (
                        self._prepare_expiry_task is not None
                        and self._prepare_expiry_task.done() is False
                    )
                    or any(
                        task.done() is False
                        for task in self._detached_live_encoding_task_refs
                    )
                    or (
                        self._retirement_cleanup_task_ref is not None
                        and self._retirement_cleanup_task_ref.done() is False
                    )
                    or (
                        self._live_encoding_task_ref is not None
                        and self._live_encoding_task_ref.done() is False
                    )
                )
                if remaining is False:
                    return True
                continue

            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                logging.warning(f'{self.log_prefix} Background cleanup tasks did not drain before timeout.')
                return False
            done, pending = await asyncio.wait(tasks, timeout=remaining_seconds)
            for task in done:
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            if len(pending) > 0:
                logging.warning(f'{self.log_prefix} Background cleanup tasks did not drain before timeout.')
                return False


    @classmethod
    def findPlaybackSession(
        cls,
        playback_session_id: str,
    ) -> list[tuple[LiveStream, LiveStreamClient]]:
        """
        接続中の playback session と所有 stream の組を返す。

        Args:
            playback_session_id (str): ブラウザ pipeline が生成した接続 UUID。

        Returns:
            list[tuple[LiveStream, LiveStreamClient]]: UUID が一致する接続の一覧。
        """

        return [
            (live_stream, client)
            for live_stream in cls.getAllLiveStreams()
            for client in live_stream._clients
            if client.playback_session_id == playback_session_id
        ]


    @classmethod
    def getByPrepareLeaseKey(cls, stream_key: str) -> LiveStream | None:
        """
        Prepare lease に保存した完全 stream key から既存 instance を返す。

        Args:
            stream_key (str): codec・互換境界まで含む Prepare lease stream key。

        Returns:
            LiveStream | None: 一致する既存 stream。
        """

        matches = [
            live_stream
            for live_stream in cls.getAllLiveStreams()
            if live_stream.getPrepareLeaseKey() == stream_key
        ]
        return matches[0] if len(matches) == 1 else None


    @classmethod
    def getONAirLiveStreams(cls) -> list[LiveStream]:
        """
        現在 ONAir 状態のライブストリームのインスタンスを取得する

        Returns:
            list[LiveStream]: 現在 ONAir 状態のライブストリームのインスタンスのリスト
        """

        result: list[LiveStream] = []

        # 現在 ONAir 状態のライブストリームに絞り込む
        for live_stream in LiveStream.getAllLiveStreams():
            if live_stream.getStatus().status == 'ONAir':
                result.append(live_stream)

        return result


    @classmethod
    def getIdlingLiveStreams(cls) -> list[LiveStream]:
        """
        現在 Idling 状態のライブストリームのインスタンスを取得する

        Returns:
            list[LiveStream]: 現在 Idling 状態のライブストリームのインスタンスのリスト
        """

        result: list[LiveStream] = []

        # 現在 Idling 状態のライブストリームに絞り込む
        for live_stream in LiveStream.getAllLiveStreams():
            if live_stream.getStatus().status == 'Idling':
                result.append(live_stream)

        return result


    @classmethod
    def getViewerCount(cls, display_channel_id: str) -> int:
        """
        指定されたチャンネルのライブストリームの現在の視聴者数を取得する

        Args:
            display_channel_id (str): チャンネルID

        Returns:
            int: 視聴者数
        """

        # 指定されたチャンネル ID に紐づくライブストリームを探して視聴者数を集計
        viewer_count = 0
        for live_stream in LiveStream.getAllLiveStreams():
            if live_stream.display_channel_id == display_channel_id:
                viewer_count += live_stream.getStatus().client_count

        return viewer_count


    def getTelemetry(self) -> LiveStreamTelemetry:
        """
        ライブパイプラインの観測値を返す。

        Args:
            None

        Returns:
            LiveStreamTelemetry: この stream の観測状態。
        """

        return self._telemetry


    def getPrepareLeaseKey(self) -> str:
        """
        codec・bit depth・音声・24fps・互換境界を含む lease target key を返す。

        Args:
            None

        Returns:
            str: 処理結果の文字列。
        """

        return repr((*self.live_stream_key, self.stream_anchor_enabled))


    def setSourceSubscription(self, subscription: LiveSourceSubscription | None) -> None:
        """
        現在の品質 encoder が利用する共有 source subscription を関連付ける。

        Args:
            subscription (LiveSourceSubscription | None): 関連付けまたは通知対象の共有 source subscription。

        Returns:
            None
        """

        self._source_subscription = subscription


    def getSourceSubscription(self) -> LiveSourceSubscription | None:
        """
        現在の品質 encoder が利用する共有 source subscription を返す。

        Args:
            None

        Returns:
            LiveSourceSubscription | None: 関連付け済みなら共有 source subscription。
        """

        return self._source_subscription


    async def promotePreparedSourceToActive(self) -> None:
        """
        Commit 済み B の共有 source role を Active へ昇格する。

        Args:
            None

        Returns:
            None
        """

        subscription = self._source_subscription
        if subscription is None:
            raise LivePrepareLeaseError('prepare_source_subscription_missing')
        # 既に同じ target の Active encoder が存在し、B client がそこへ相乗りした場合は
        # 新しい Preparing subscription 自体が存在しないため、Commit はそのまま成功とする。
        if subscription.role == 'Active':
            return
        await subscription.promoteToActive()


    def getPrepareClient(self, token: str) -> LiveStreamClient | None:
        """
        単回消費済み token に結び付いた接続中 B client を返す。

        Args:
            token (str): 対象 Prepare lease を識別する token。

        Returns:
            LiveStreamClient | None: token に対応する接続中 client。
        """

        return next(
            (
                client
                for client in self._clients
                if client.prepare_token == token
            ),
            None,
        )


    def hasSourceViewers(self) -> bool:
        """
        共有 source を維持すべき接続または起動中 client が存在するか返す。

        Args:
            None

        Returns:
            bool: 判定結果。
        """

        # connect() は LiveEncodingTask を起動してから client を登録するため、その短い期間だけは
        # Standby を viewer 有りとして扱い、別 source の同時起動に誤って preempt されないようにする。
        return len(self._clients) > 0 or self._status == 'Standby'


    def setPrepareLease(
        self,
        token: str,
        *,
        expires_in_seconds: float,
    ) -> None:
        """
        取得済み Prepare token と TTL 観測をこの target stream へ関連付ける。

        Args:
            token (str): 対象 Prepare lease を識別する token。
            expires_in_seconds (float): Prepare lease が失効するまでの秒数。

        Returns:
            None
        """

        previous_expiry_task = self._prepare_expiry_task
        if previous_expiry_task is not None:
            previous_expiry_task.cancel()
        self._prepare_token = token
        telemetry = self.getTelemetry()
        if self._status == 'Offline' and len(self._clients) == 0:
            telemetry.resetPipeline()
        telemetry.setPrepareState('Prepared')

        async def expire() -> None:
            """
            期限切れ状態を確定する。

            Args:
                None

            Returns:
                None
            """
            try:
                await asyncio.sleep(max(0.0, expires_in_seconds))
                snapshot = await LIVE_PREPARE_COORDINATOR.get(token)
                if (
                    self._prepare_token == token and
                    snapshot is not None and
                    snapshot.state == 'Expired'
                ):
                    self._prepare_token = None
                    self.getTelemetry().setPrepareState('Expired')
            except asyncio.CancelledError:
                return

        self._prepare_expiry_task = asyncio.create_task(expire())


    def setPrepareRejected(self) -> None:
        """
        Prepare lease の拒否を SSE 観測へ反映する。

        Args:
            None

        Returns:
            None
        """

        telemetry = self.getTelemetry()
        if self._status == 'Offline' and len(self._clients) == 0:
            telemetry.resetPipeline()
        telemetry.setPrepareState('Rejected')


    async def releasePrepareLease(self, token: str, *, reason: str) -> bool:
        """
        この stream に関連付いた Prepare lease を冪等に解放する。

        Args:
            token (str): 対象 Prepare lease を識別する token。
            reason (str): 状態変更または解放の理由。

        Returns:
            bool: 判定結果。
        """

        released = await LIVE_PREPARE_COORDINATOR.release(token, reason=reason)
        prepare_client = self.getPrepareClient(token)
        if prepare_client is not None:
            # Commit 後は通常 viewer として存続するため、将来の切断を Prepare cleanup と誤認しない。
            prepare_client.prepare_token = None
        if self._prepare_token == token:
            self._prepare_token = None
            expiry_task = self._prepare_expiry_task
            if expiry_task is not None:
                expiry_task.cancel()
                self._prepare_expiry_task = None
            if self.getTelemetry().snapshot().prepare_state != 'Expired':
                self.getTelemetry().setPrepareState('Released')
        return released


    async def finalizePrepareClientRemoval(
        self,
        token: str,
        *,
        reason: str,
        retirement_lock_held: bool = False,
    ) -> bool:
        """
        B client 離脱後の source role と lease を一貫して確定する。

        Args:
            token (str): 対象 Prepare lease を識別する token。
            reason (str): 状態変更または解放の理由。
            retirement_lock_held (bool): 呼び出し元がretirement gateを保持中か。

        Returns:
            bool: 判定結果。
        """

        try:
            if len(self._clients) > 0:
                subscription = self._source_subscription
                if subscription is not None:
                    await subscription.promoteToActive()
            elif self.getStatus().status != 'Offline':
                self.setStatus('Offline', 'Prepare client が終了したため B パイプラインを停止します。')
        except LiveSourceSubscriberError:
            # 残存 viewer を Preparing のまま配信し続けることはできないため、安全側で全体を終了する。
            self.setStatus('Offline', 'Prepare source role の確定に失敗したため配信を終了します。')
            remaining_clients = tuple(self._clients)
            playback_session_ids = [
                client.playback_session_id
                for client in remaining_clients
                if client.playback_session_id is not None
            ]
            for client in remaining_clients:
                try:
                    client.writeStreamData(None)
                except Exception:
                    logging.warning(
                        f'{self.log_prefix} Failed to notify client after Prepare role promotion failure.'
                    )
                self.disconnect(
                    client,
                    cleanup_prepare = False,
                    retire_playback_session = False,
                )

            # UUIDなしのlegacy viewerしかいない場合も、内部markerでencoder cleanupを実行する。
            cleanup_session_ids = (
                playback_session_ids
                if len(playback_session_ids) > 0
                else [f'prepare-cleanup:{token}']
            )
            for playback_session_id in cleanup_session_ids:
                retire_result = (
                    await self.__retirePlaybackSessionLocked(
                        playback_session_id,
                        cleanup_timeout_seconds = self.PLAYBACK_SESSION_CLEANUP_TIMEOUT_SECONDS,
                    )
                    if retirement_lock_held is True
                    else await self.retirePlaybackSession(playback_session_id)
                )
                if retire_result in ('cleanup_timeout', 'cleanup_failed', 'session_ambiguous'):
                    logging.warning(
                        f'{self.log_prefix} Prepare role failure cleanup was not confirmed; '
                        f'lease release was withheld: {retire_result}.'
                    )
                    return False
        return await self.releasePrepareLease(token, reason=reason)


    async def disconnectPrepareClient(self, token: str, *, reason: str) -> bool:
        """
        token 所有 client だけを切断し、残存 viewer を維持して lease を解放する。

        Args:
            token (str): 対象 Prepare lease を識別する token。
            reason (str): 状態変更または解放の理由。

        Returns:
            bool: 判定結果。
        """

        prepare_client = self.getPrepareClient(token)
        if prepare_client is not None:
            self.disconnect(
                prepare_client,
                cleanup_prepare = False,
                retire_playback_session = False,
            )
        return await self.finalizePrepareClientRemoval(token, reason=reason)


    def __finalizePrepareClientRemovalSoon(
        self,
        token: str,
        *,
        playback_session_id: str | None,
        reason: str,
    ) -> None:
        """
        同期 disconnect 境界から B session retire 後の Prepare 確定を予約する。

        Args:
            token (str): 対象 Prepare lease を識別する token。
            playback_session_id (str | None): token に束縛された B pipeline の UUID。
            reason (str): 状態変更または解放の理由。

        Returns:
            None
        """

        async def retire_then_finalize() -> bool:
            """
            B cleanup を確認できた場合だけ source role と lease を解放する。

            Returns:
                bool: cleanup 後に lease を解放できた場合は True。
            """

            async def abort_operation() -> LivePrepareFinalizeResult:
                """
                B cleanup・source role確定・lease解放を単一Abort taskで実行する。

                Returns:
                    LivePrepareFinalizeResult: cleanupとlease解放の確定結果。
                """

                if playback_session_id is None:
                    logging.warning(
                        f'{self.log_prefix} Prepare lease has no playback session; '
                        'automatic release was withheld.'
                    )
                    return LivePrepareFinalizeResult(
                        accepted = False,
                        reason = 'prepare_playback_session_missing',
                        cleanup_confirmed = False,
                        rollback_allowed = False,
                        terminal_restart_required = True,
                    )
                retire_result = await self.retirePlaybackSession(playback_session_id)
                if retire_result in ('cleanup_timeout', 'cleanup_failed', 'session_ambiguous'):
                    if retire_result == 'cleanup_timeout':
                        self.__schedulePrepareCleanupFollowup(
                            token,
                            reason = reason,
                        )
                    logging.warning(
                        f'{self.log_prefix} Prepare session cleanup was not confirmed; '
                        f'lease release was withheld: {retire_result}.'
                    )
                    return LivePrepareFinalizeResult(
                        accepted = False,
                        reason = f'prepare_abort_{retire_result}',
                        cleanup_confirmed = False,
                        rollback_allowed = False,
                        terminal_restart_required = True,
                        can_converge_after_cleanup = retire_result == 'cleanup_timeout',
                    )
                await self.finalizePrepareClientRemoval(token, reason=reason)
                latest = await LIVE_PREPARE_COORDINATOR.get(token)
                accepted = latest is not None and latest.state == 'Released'
                return LivePrepareFinalizeResult(
                    accepted = accepted,
                    reason = None if accepted else 'prepare_abort_not_released',
                    cleanup_confirmed = accepted,
                    rollback_allowed = accepted,
                    terminal_restart_required = accepted is False,
                )

            result = await LIVE_PREPARE_COORDINATOR.finalize(
                token,
                'abort',
                abort_operation,
            )
            return result.accepted

        try:
            release_task = asyncio.create_task(retire_then_finalize())
        except RuntimeError:
            # イベントループ終了後は app shutdown の releaseAll() が回収する
            return
        self._prepare_release_tasks.add(release_task)

        def OnReleaseDone(done_task: asyncio.Task[bool]) -> None:
            """
            自動Prepare cleanup taskの参照と例外を回収する。

            Args:
                done_task (asyncio.Task[bool]): 完了したcleanup task。

            Returns:
                None
            """

            self._prepare_release_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as ex:
                logging.error(
                    f'{self.log_prefix} Automatic Prepare cleanup task failed.',
                    exc_info = ex,
                )

        release_task.add_done_callback(OnReleaseDone)


    def __schedulePrepareCleanupFollowup(
        self,
        token: str,
        *,
        reason: str,
    ) -> asyncio.Task[bool] | None:
        """
        Retire待機上限後も実体cleanupを強参照し、成功時だけ同じPrepare leaseを解放する。

        Args:
            token (str): cleanup完了後に解放するPrepare lease token。
            reason (str): lease解放理由。

        Returns:
            asyncio.Task[bool] | None: 自動収束task。待機対象がなければNone。
        """

        existing_task = self._prepare_cleanup_followup_tasks.get(token)
        if existing_task is not None and existing_task.done() is False:
            return existing_task

        retirement_cleanup_task = self._retirement_cleanup_task_ref
        live_encoding_task_instance = self._live_encoding_task_instance_ref
        if retirement_cleanup_task is None:
            logging.warning(
                f'{self.log_prefix} Prepare cleanup timed out without a tracked encoding task; '
                'lease release remains withheld.'
            )
            return None

        async def converge_after_cleanup() -> bool:
            """
            分離済みencoder taskの実終了を待ち、cleanup成功時だけleaseを解放する。

            Returns:
                bool: cleanup確認後にleaseがReleasedへ収束した場合はTrue。
            """

            task_failed = False
            try:
                await asyncio.shield(retirement_cleanup_task)
            except asyncio.CancelledError:
                # LiveEncodingTask 自体の cancellation は通常の停止結果。followup側だけが
                # cancelされた場合は実体taskを巻き込まず、呼び出し元へ伝播する。
                if retirement_cleanup_task.done() is False:
                    raise
            except Exception:
                task_failed = True
                logging.warning(
                    f'{self.log_prefix} Encoding task ended with an error after Prepare cleanup timeout.'
                )

            # LiveEncodingTaskのdone callbackがsticky cleanup結果を反映するtickを許す。
            await asyncio.sleep(0)
            if live_encoding_task_instance is not None:
                cleanup_confirmed = live_encoding_task_instance.isCleanupConfirmed()
                if self._last_live_encoding_cleanup_confirmed is not False:
                    self._last_live_encoding_cleanup_confirmed = cleanup_confirmed
            else:
                cleanup_confirmed = task_failed is False
            cleanup_confirmed = (
                cleanup_confirmed is True
                and self._last_live_encoding_cleanup_confirmed is not False
            )

            if self._live_encoding_task_ref is retirement_cleanup_task:
                self._live_encoding_task_ref = None
            if self._retirement_cleanup_task_ref is retirement_cleanup_task:
                self._retirement_cleanup_task_ref = None
            if cleanup_confirmed is False:
                logging.warning(
                    f'{self.log_prefix} Prepare lease release remains withheld because '
                    'the eventual encoding cleanup was not confirmed.'
                )
                return False

            snapshot = await LIVE_PREPARE_COORDINATOR.get(token)
            if snapshot is None:
                return False
            if snapshot.state == 'Released':
                await LIVE_PREPARE_COORDINATOR.recordFinalizeConvergence(token, 'abort')
                return True
            if snapshot.state == 'Expired':
                return True
            await self.finalizePrepareClientRemoval(token, reason=reason)
            latest = await LIVE_PREPARE_COORDINATOR.get(token)
            converged = latest is not None and latest.state in ('Released', 'Expired')
            if latest is not None and latest.state == 'Released':
                await LIVE_PREPARE_COORDINATOR.recordFinalizeConvergence(token, 'abort')
            return converged

        followup_task = asyncio.create_task(converge_after_cleanup())
        self._prepare_cleanup_followup_tasks[token] = followup_task
        self._prepare_release_tasks.add(followup_task)

        def OnFollowupDone(done_task: asyncio.Task[bool]) -> None:
            """
            cleanup timeout後の自動収束task参照と例外を回収する。

            Args:
                done_task (asyncio.Task[bool]): 完了した自動収束task。

            Returns:
                None
            """

            if self._prepare_cleanup_followup_tasks.get(token) is done_task:
                self._prepare_cleanup_followup_tasks.pop(token, None)
            self._prepare_release_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as ex:
                logging.error(
                    f'{self.log_prefix} Prepare cleanup follow-up task failed.',
                    exc_info = ex,
                )

        followup_task.add_done_callback(OnFollowupDone)
        return followup_task


    def __pruneRetiredPlaybackSessions(self) -> None:
        """
        再接続拒否用 playback session tombstone を TTL・最大件数で有界化する。

        Args:
            None

        Returns:
            None
        """

        now = time.monotonic()
        expired = [
            playback_session_id
            for playback_session_id, retired_at in self._retired_playback_sessions.items()
            if now - retired_at >= self.PLAYBACK_SESSION_RETIRE_TTL_SECONDS
        ]
        for playback_session_id in expired:
            self._retired_playback_sessions.pop(playback_session_id, None)
        excess = len(self._retired_playback_sessions) - self.PLAYBACK_SESSION_RETIRE_MAX_ENTRIES
        if excess > 0:
            oldest = sorted(
                self._retired_playback_sessions.items(),
                key=lambda item: item[1],
            )
            for playback_session_id, _retired_at in oldest[:excess]:
                self._retired_playback_sessions.pop(playback_session_id, None)


    def __markPlaybackSessionRetired(self, playback_session_id: str) -> None:
        """
        強制 EOF 後の同一 pipeline 自動再接続を拒否する tombstone を記録する。

        Args:
            playback_session_id (str): ブラウザ pipeline が生成した接続 UUID。

        Returns:
            None
        """

        self.__pruneRetiredPlaybackSessions()
        self._retired_playback_sessions[playback_session_id] = time.monotonic()
        self.__pruneRetiredPlaybackSessions()


    def __prunePlaybackSessionRetireAPITasks(self) -> None:
        """
        Retire APIの完了結果を再送猶予と最大件数で有界保持する。

        Returns:
            None
        """

        now = time.monotonic()
        expired_session_ids = [
            playback_session_id
            for playback_session_id, completed_at in self._playback_session_retire_api_completed_at.items()
            if now - completed_at >= self.PLAYBACK_SESSION_RETIRE_TTL_SECONDS
        ]
        for playback_session_id in expired_session_ids:
            self._playback_session_retire_api_tasks.pop(playback_session_id, None)
            self._playback_session_retire_api_completed_at.pop(playback_session_id, None)

        excess = (
            len(self._playback_session_retire_api_completed_at)
            - self.PLAYBACK_SESSION_RETIRE_MAX_ENTRIES
        )
        if excess > 0:
            oldest = sorted(
                self._playback_session_retire_api_completed_at.items(),
                key=lambda item: item[1],
            )
            for playback_session_id, _completed_at in oldest[:excess]:
                self._playback_session_retire_api_tasks.pop(playback_session_id, None)
                self._playback_session_retire_api_completed_at.pop(playback_session_id, None)


    def schedulePlaybackSessionRetireAPI(
        self,
        playback_session_id: str,
        operation: Callable[[], Awaitable[LivePlaybackSessionRetireResponse]],
    ) -> asyncio.Task[LivePlaybackSessionRetireResponse]:
        """
        session UUID単位でRetire・Prepare finalize一体taskを作り、完了結果も再送窓中共有する。

        Args:
            playback_session_id (str): 終了するbrowser pipeline UUID。
            operation (Callable[[], Awaitable[LivePlaybackSessionRetireResponse]]): 一体cleanup処理。

        Returns:
            asyncio.Task[LivePlaybackSessionRetireResponse]: request切断から独立して継続するtask。
        """

        self.__prunePlaybackSessionRetireAPITasks()
        existing_task = self._playback_session_retire_api_tasks.get(playback_session_id)
        if existing_task is not None:
            return existing_task

        inflight_count = sum(
            retire_task.done() is False
            for retire_task in self._playback_session_retire_api_tasks.values()
        )
        if inflight_count >= self.PLAYBACK_SESSION_RETIRE_MAX_INFLIGHT:
            async def RejectRetireAPI() -> LivePlaybackSessionRetireResponse:
                """
                保留中 Retire 上限超過を terminal cleanup failure として即時確定する。

                Returns:
                    LivePlaybackSessionRetireResponse: サーバー再起動が必要な fail-close 結果。
                """

                return LivePlaybackSessionRetireResponse(
                    accepted = False,
                    result = 'cleanup_failed',
                    reason = 'playback_session_retire_capacity_exceeded',
                    cleanup_confirmed = False,
                    terminal_restart_required = True,
                )

            return asyncio.create_task(RejectRetireAPI())

        async def RunRetireAPI() -> LivePlaybackSessionRetireResponse:
            """
            任意のAwaitableをasyncio Task化できるcoroutineとして実行する。

            Returns:
                LivePlaybackSessionRetireResponse: Retire・Prepare finalizeの確定結果。
            """

            return await operation()

        retire_api_task = asyncio.create_task(RunRetireAPI())
        self._playback_session_retire_api_tasks[playback_session_id] = retire_api_task

        def OnRetireAPIDone(
            done_task: asyncio.Task[LivePlaybackSessionRetireResponse],
        ) -> None:
            """
            一体taskの完了時刻を記録し、例外を回収する。

            Args:
                done_task (asyncio.Task[LivePlaybackSessionRetireResponse]): 完了したRetire API task。

            Returns:
                None
            """

            if self._playback_session_retire_api_tasks.get(playback_session_id) is done_task:
                self._playback_session_retire_api_completed_at[playback_session_id] = time.monotonic()
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as ex:
                logging.error(
                    f'{self.log_prefix} Playback session Retire API task failed.',
                    exc_info = ex,
                )

        retire_api_task.add_done_callback(OnRetireAPIDone)
        return retire_api_task


    async def retirePlaybackSession(
        self,
        playback_session_id: str,
        *,
        cleanup_timeout_seconds: float = PLAYBACK_SESSION_CLEANUP_TIMEOUT_SECONDS,
    ) -> LivePlaybackSessionRetireResult:
        """
        対象 playback session だけを EOF 終了し、最後の viewer なら encoder cleanup を待つ。

        新規接続は retirement gate で cleanup 完了後まで待たせる。同じ旧 UUID は tombstone で
        拒否するが、別 viewer が残る stream の encoder と共有 source role は維持する。

        Args:
            playback_session_id (str): 強制終了するブラウザ pipeline の接続 UUID。
            cleanup_timeout_seconds (float): LiveEncodingTask の資源回収を待つ最大秒数。

        Returns:
            Literal[
                'cleanup_completed',
                'shared_encoder_retained',
                'session_ambiguous',
                'cleanup_timeout',
                'cleanup_failed',
            ]:
                接続・encoder cleanup の確定結果。
        """

        if cleanup_timeout_seconds <= 0:
            raise ValueError('cleanup_timeout_seconds must be greater than zero.')

        retire_task = self.__schedulePlaybackSessionRetirement(
            playback_session_id,
            cleanup_timeout_seconds = cleanup_timeout_seconds,
        )
        # ASGI request の切断で明示 Retire 待機だけがキャンセルされても、同一 UUID の
        # cleanup task は継続し、自動 disconnect と後続 retry が同じ結果へ収束する。
        return await asyncio.shield(retire_task)


    async def waitForPlaybackSessionCleanup(self) -> LivePlaybackSessionRetireResult:
        """
        Retire API待機上限後も旧encoder taskの実終了を待ち、最終cleanup結果を返す。

        Returns:
            LivePlaybackSessionRetireResult: cleanup_completedまたはcleanup_failed。
        """

        retirement_cleanup_task = self._retirement_cleanup_task_ref
        live_encoding_task_instance = self._live_encoding_task_instance_ref
        if retirement_cleanup_task is None:
            return (
                'cleanup_failed'
                if self._last_live_encoding_cleanup_confirmed is False
                else 'cleanup_completed'
            )

        task_failed = False
        try:
            await asyncio.shield(retirement_cleanup_task)
        except asyncio.CancelledError:
            if retirement_cleanup_task.done() is False:
                raise
        except Exception:
            task_failed = True
            logging.warning(
                f'{self.log_prefix} Encoding task ended with an error after Retire API timeout.'
            )

        await asyncio.sleep(0)
        if live_encoding_task_instance is not None:
            cleanup_confirmed = live_encoding_task_instance.isCleanupConfirmed()
            if self._last_live_encoding_cleanup_confirmed is not False:
                self._last_live_encoding_cleanup_confirmed = cleanup_confirmed
        else:
            cleanup_confirmed = task_failed is False
        cleanup_confirmed = (
            cleanup_confirmed is True
            and self._last_live_encoding_cleanup_confirmed is not False
        )
        if self._live_encoding_task_ref is retirement_cleanup_task:
            self._live_encoding_task_ref = None
        if self._retirement_cleanup_task_ref is retirement_cleanup_task:
            self._retirement_cleanup_task_ref = None
        return 'cleanup_completed' if cleanup_confirmed is True else 'cleanup_failed'


    def __schedulePlaybackSessionRetirement(
        self,
        playback_session_id: str,
        *,
        cleanup_timeout_seconds: float = PLAYBACK_SESSION_CLEANUP_TIMEOUT_SECONDS,
    ) -> asyncio.Task[LivePlaybackSessionRetireResult]:
        """
        playback session UUID ごとに一つだけ cleanup task を作成する。

        Args:
            playback_session_id (str): 強制終了するブラウザ pipeline の接続 UUID。
            cleanup_timeout_seconds (float): LiveEncodingTask の資源回収を待つ最大秒数。

        Returns:
            asyncio.Task[LivePlaybackSessionRetireResult]: 自動切断と明示 Retire が共有する task。
        """

        existing_task = self._playback_session_retire_tasks.get(playback_session_id)
        if existing_task is not None and existing_task.done() is False:
            return existing_task

        # task が retirement gate を取得する前の自動再接続 race も同期的に拒否する。
        self.__markPlaybackSessionRetired(playback_session_id)

        async def retire() -> LivePlaybackSessionRetireResult:
            """
            retirement gate 内で対象 session の cleanup を実行する。

            Returns:
                LivePlaybackSessionRetireResult: session・encoder cleanup結果。
            """

            return await self.__retirePlaybackSession(
                playback_session_id,
                cleanup_timeout_seconds = cleanup_timeout_seconds,
            )

        retire_task = asyncio.create_task(retire())
        self._playback_session_retire_tasks[playback_session_id] = retire_task

        def OnRetireDone(done_task: asyncio.Task[LivePlaybackSessionRetireResult]) -> None:
            """
            session Retire taskの参照と例外を回収する。

            Args:
                done_task (asyncio.Task[LivePlaybackSessionRetireResult]): 完了したRetire task。

            Returns:
                None
            """

            if self._playback_session_retire_tasks.get(playback_session_id) is done_task:
                self._playback_session_retire_tasks.pop(playback_session_id, None)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as ex:
                logging.error(
                    f'{self.log_prefix} Playback session retirement task failed.',
                    exc_info = ex,
                )

        retire_task.add_done_callback(OnRetireDone)
        return retire_task


    async def __retirePlaybackSession(
        self,
        playback_session_id: str,
        *,
        cleanup_timeout_seconds: float,
    ) -> LivePlaybackSessionRetireResult:
        """
        retirement gate 内で対象 playback session を終了する。

        Args:
            playback_session_id (str): 強制終了するブラウザ pipeline の接続 UUID。
            cleanup_timeout_seconds (float): LiveEncodingTask の資源回収を待つ最大秒数。

        Returns:
            LivePlaybackSessionRetireResult: 接続・encoder cleanup の確定結果。
        """

        async with self._retirement_lock:
            return await self.__retirePlaybackSessionLocked(
                playback_session_id,
                cleanup_timeout_seconds = cleanup_timeout_seconds,
            )


    async def __retirePlaybackSessionLocked(
        self,
        playback_session_id: str,
        *,
        cleanup_timeout_seconds: float,
    ) -> LivePlaybackSessionRetireResult:
        """
        retirement gate を保持した呼び出し元から対象 playback session を終了する。

        Args:
            playback_session_id (str): 強制終了するブラウザ pipeline の接続 UUID。
            cleanup_timeout_seconds (float): LiveEncodingTask の資源回収を待つ最大秒数。

        Returns:
            Literal[
                'cleanup_completed',
                'shared_encoder_retained',
                'session_ambiguous',
                'cleanup_timeout',
                'cleanup_failed',
            ]:
                接続・encoder cleanup の確定結果。
        """

        matching_clients = [
            client
            for client in self._clients
            if client.playback_session_id == playback_session_id
        ]
        if len(matching_clients) > 1:
            return 'session_ambiguous'

        # EOF を受けた mpegts.js が Commit 応答前に再接続しても、旧 A を復活させない。
        self.__markPlaybackSessionRetired(playback_session_id)
        if len(matching_clients) == 1:
            client = matching_clients[0]
            client.writeStreamData(None)
            self.disconnect(
                client,
                cleanup_prepare = False,
                retire_playback_session = False,
            )

        # 対象 session が promotion 待機中に先に切断されていても、別 viewer が所有する
        # encoder / Active source subscription は終了しない。
        if len(self._clients) > 0:
            return 'shared_encoder_retained'

        self.setStatus('Offline', '画質切り替え元の接続を終了し、エンコーダーを停止します。')
        live_encoding_task = self._live_encoding_task_ref
        live_encoding_task_instance = self._live_encoding_task_instance_ref
        if live_encoding_task is None:
            return (
                'cleanup_failed'
                if self._last_live_encoding_cleanup_confirmed is False
                else 'cleanup_completed'
            )
        if live_encoding_task.done() is False:
            live_encoding_task.cancel()
        self._retirement_cleanup_task_ref = live_encoding_task

        done, _pending = await asyncio.wait(
            {live_encoding_task},
            timeout = cleanup_timeout_seconds,
        )
        if len(done) == 0:
            logging.warning(
                f'{self.log_prefix} Playback session retire cleanup did not complete within '
                f'{cleanup_timeout_seconds:.1f} seconds.'
            )
            return 'cleanup_timeout'

        # run() の cancellation / 例外は cleanup 完了後に Task へ反映される。
        try:
            live_encoding_task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logging.warning(f'{self.log_prefix} Encoding task ended with an error during playback session retire.')
        if live_encoding_task_instance is not None:
            if self._last_live_encoding_cleanup_confirmed is not False:
                self._last_live_encoding_cleanup_confirmed = live_encoding_task_instance.isCleanupConfirmed()
        cleanup_confirmed = self._last_live_encoding_cleanup_confirmed is not False
        if self._live_encoding_task_ref is live_encoding_task:
            self._live_encoding_task_ref = None
        if self._retirement_cleanup_task_ref is live_encoding_task:
            self._retirement_cleanup_task_ref = None
        return 'cleanup_completed' if cleanup_confirmed is True else 'cleanup_failed'


    async def connect(
        self,
        client_type: Literal['mpegts'],
        *,
        prepare_token: str | None = None,
        prepare_backend: str | None = None,
        playback_session_id: str | None = None,
    ) -> LiveStreamClient:
        """
        ライブストリームに接続して、新しくライブストリームに登録されたクライアントを返す
        この時点でライブストリームが Offline ならば、新たにエンコードタスクが起動される

        Args:
            client_type (Literal['mpegts']): クライアントの種別 (mpegts, ll-hls クライアントは廃止された)
            prepare_token (str | None): 二段階切り替え B の単回使用 token。
            prepare_backend (str | None): Prepare 枠を所有する encoder backend。
            playback_session_id (str | None): ブラウザ pipeline が生成した接続 UUID。

        Returns:
            LiveStreamClient: ライブストリームクライアントのインスタンス
        """

        async with self._retirement_lock:
            if self._is_shutting_down is True:
                raise LivePrepareLeaseError('live_stream_shutting_down')

            # 前回 retire が API 待機上限を超えても、旧 task の cleanup が終わるまでは
            # 新しい encoder 世代を起動せず同じ gate 内で完了を待つ。
            retirement_cleanup_task = self._retirement_cleanup_task_ref
            if retirement_cleanup_task is not None:
                done, _pending = await asyncio.wait(
                    {retirement_cleanup_task},
                    timeout = self.PLAYBACK_SESSION_PENDING_WAIT_SECONDS,
                )
                if len(done) == 0:
                    raise LivePrepareLeaseError('playback_session_cleanup_pending')
                try:
                    retirement_cleanup_task.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                if self._retirement_cleanup_task_ref is retirement_cleanup_task:
                    self._retirement_cleanup_task_ref = None
            if self._last_live_encoding_cleanup_confirmed is False:
                raise LivePrepareLeaseError('playback_session_cleanup_failed')
            if len(self._detached_live_encoding_task_refs) > 0:
                detached_cleanup_tasks = {
                    task
                    for task in self._detached_live_encoding_task_refs
                    if task.done() is False
                }
                if len(detached_cleanup_tasks) > 0:
                    _done, pending = await asyncio.wait(
                        detached_cleanup_tasks,
                        timeout = self.PLAYBACK_SESSION_PENDING_WAIT_SECONDS,
                    )
                    if len(pending) > 0:
                        raise LivePrepareLeaseError('playback_session_cleanup_pending')
                # done callbackが世代cleanup結果をsticky状態へ反映するtickを必ず挟む。
                await asyncio.sleep(0)
                self._detached_live_encoding_task_refs = {
                    task
                    for task in self._detached_live_encoding_task_refs
                    if task.done() is False
                }
                if self._last_live_encoding_cleanup_confirmed is False:
                    raise LivePrepareLeaseError('playback_session_cleanup_failed')

            if playback_session_id is not None:
                self.__pruneRetiredPlaybackSessions()
                if playback_session_id in self._retired_playback_sessions:
                    raise LivePrepareLeaseError('playback_session_retired')
                if any(
                    client.playback_session_id == playback_session_id
                    for client in self._clients
                ):
                    raise LivePrepareLeaseError('playback_session_already_connected')

            # ***** Prepare token の単回消費・B session 再接続 *****

            effective_prepare_token = prepare_token
            if effective_prepare_token is None and playback_session_id is not None:
                resolved_prepare = await LIVE_PREPARE_COORDINATOR.consumeOrResolveByPlaybackSessionID(
                    stream_key = self.getPrepareLeaseKey(),
                    playback_session_id = playback_session_id,
                )
                if resolved_prepare is not None:
                    effective_prepare_token, _consumed_now = resolved_prepare
            if effective_prepare_token is not None:
                if prepare_token is not None:
                    if prepare_backend is None:
                        raise LivePrepareLeaseError('prepare_backend_missing')
                    if playback_session_id is None:
                        raise LivePrepareLeaseError('prepare_playback_session_missing')
                    await LIVE_PREPARE_COORDINATOR.consume(
                        effective_prepare_token,
                        backend = prepare_backend,
                        stream_key = self.getPrepareLeaseKey(),
                        playback_session_id = playback_session_id,
                    )
                expiry_task = self._prepare_expiry_task
                if expiry_task is not None:
                    expiry_task.cancel()
                    self._prepare_expiry_task = None
                self._prepare_token = effective_prepare_token
                self.getTelemetry().setPrepareState('Consumed')

            try:
                client = await self.__connect(
                    client_type,
                    prepare_token = effective_prepare_token,
                    playback_session_id = playback_session_id,
                )
                if effective_prepare_token is not None:
                    snapshot = await LIVE_PREPARE_COORDINATOR.get(effective_prepare_token)
                    if snapshot is None or snapshot.state != 'Consumed':
                        raise LivePrepareLeaseError('prepare_token_released_during_connect')
                return client
            except BaseException:
                # consume() 後は、encoder 起動・tuner 再利用・client 登録のどこで失敗しても
                # sessionを先にretireしてencoder cleanupを待ち、その後でbackend枠を解放する。
                if effective_prepare_token is not None:
                    async def cleanup_failed_connect() -> None:
                        """失敗したB接続のsession・encoder・leaseを順番に回収する。"""

                        cleanup_confirmed = False
                        if playback_session_id is not None:
                            retire_result = await self.__retirePlaybackSessionLocked(
                                playback_session_id,
                                cleanup_timeout_seconds = self.PLAYBACK_SESSION_CLEANUP_TIMEOUT_SECONDS,
                            )
                            cleanup_confirmed = retire_result not in (
                                'cleanup_timeout',
                                'cleanup_failed',
                                'session_ambiguous',
                            )
                            if retire_result == 'cleanup_timeout':
                                self.__schedulePrepareCleanupFollowup(
                                    effective_prepare_token,
                                    reason = 'connect_failure',
                                )
                            if cleanup_confirmed is False:
                                logging.warning(
                                    f'{self.log_prefix} Failed Prepare connection cleanup was incomplete: '
                                    f'{retire_result}.'
                                )
                        prepare_client = self.getPrepareClient(effective_prepare_token)
                        if prepare_client is not None:
                            self.disconnect(
                                prepare_client,
                                cleanup_prepare = False,
                                retire_playback_session = False,
                            )
                        if cleanup_confirmed is True:
                            await self.finalizePrepareClientRemoval(
                                effective_prepare_token,
                                reason = 'connect_failure',
                                retirement_lock_held = True,
                            )
                        else:
                            logging.warning(
                                f'{self.log_prefix} Prepare lease release was withheld after failed connection cleanup.'
                            )

                    cleanup_task = asyncio.create_task(cleanup_failed_connect())
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        await cleanup_task
                    except Exception as ex:
                        logging.error(
                            f'{self.log_prefix} Failed to clean up rejected Prepare connection.',
                            exc_info = ex,
                        )
                raise


    async def __connect(
        self,
        client_type: Literal['mpegts'],
        *,
        prepare_token: str | None,
        playback_session_id: str | None,
    ) -> LiveStreamClient:
        """
        token 検証後の既存接続処理を実行する。

        Args:
            client_type (Literal['mpegts']): 接続するライブ配信クライアント種別。
            prepare_token (str | None): 二段階切替の Prepare lease token。
            playback_session_id (str | None): ブラウザ pipeline が生成した接続 UUID。

        Returns:
            LiveStreamClient: 登録した MPEG-TS 配信 client。
        """

        # ***** ステータスの切り替え *****

        current_status = self._status
        should_start_task: bool = False

        # ライブストリームが Offline な場合、新たにエンコードタスクを起動する
        if current_status == 'Offline':
            # 前回 encoder の output age / Anchor を新しい Startup の initial SSE へ持ち越さない。
            # Prepare token 接続では consume 済み状態を維持し、通常接続では前世代の終端状態も破棄する。
            telemetry = self.getTelemetry()
            if prepare_token is not None:
                telemetry.resetPipeline()
            else:
                telemetry.reset()

            # ステータスを Standby に設定
            # 現在 Idling 状態のライブストリームを探す前に設定しないと多重に LiveEncodingTask が起動しかねず、重篤な不具合につながる
            async with self._tuner_lock:
                if self._status == 'Offline':
                    self.setStatus('Standby', 'エンコードタスクを起動しています…')
                    should_start_task = True

            # 一般にチューナーリソースは無尽蔵にあるわけではないので、現在 Idling（=つまり誰も見ていない）ライブストリームがあるのなら
            # それを Offline にしてチューナーリソースを解放し、新しいライブストリームがチューナーを使えるようにする
            ## EDCB バックエンドの場合はチューナーインスタンスを直接移譲して再利用できるため、より高度なチューナー再利用ロジックを実行する
            ## Mirakurun バックエンドの場合はチューナー管理が Mirakurun/mirakc 側で行われるため、
            ## Idling ストリームを Offline にしてチューナーを解放するだけでよい (チューナーインスタンスの移譲は不要)
            is_edcb_backend = Config().general.live_stream_backend == 'EDCB'

            # EDCB バックエンドの場合は、再利用できるチューナーがあれば取得しておく
            if should_start_task is True and is_edcb_backend is True:

                # チューナー再利用の対象になりうる Standby / ONAir / Idling のストリームを探す
                # (クライアントが 0 のもののみを対象にする)
                ## Idling への移行は非同期で遅れて発生するため、短時間リトライする
                for _ in range(15):
                    found_reusable_tuner = False
                    should_wait_next_retry = False

                    for live_stream in LiveStream.getAllLiveStreams():
                        # 自分自身は対象外
                        if live_stream is self:
                            continue

                        # ステータスを取得
                        async with live_stream._tuner_lock:
                            live_stream_status = live_stream.getStatus()

                        # クライアントが接続されている場合は対象外
                        # ただし Standby 状態のストリームはまだクライアントに有意なデータを配信していないため、
                        # client_count に関係なくチューナー再利用の対象にする (disconnectAll() で安全に切断できる)
                        if live_stream_status.client_count != 0 and live_stream_status.status != 'Standby':
                            # 近いタイミングで Idling に遷移する可能性があるため、リトライ対象とする
                            if (live_stream_status.status == 'ONAir' or
                                live_stream_status.status == 'Idling'):
                                should_wait_next_retry = True
                            continue

                        # Standby / ONAir / Idling 状態でない場合は対象外
                        if live_stream_status.status not in ('Standby', 'ONAir', 'Idling'):
                            continue

                        # チューナーが割り当てられていない場合は対象外
                        if live_stream.tuner is None:
                            continue

                        # チューナーが既にキャンセル中の場合は対象外
                        if live_stream.tuner.getState() == 'Cancelling':
                            continue

                        # チューナー再利用のため、チューナー状態をキャンセル中に切り替える
                        live_stream.tuner.setState('Cancelling')

                        # ステータスを Offline に設定
                        live_stream.setStatus('Offline', '新しいライブストリームが開始されたため、チューナーリソースを再利用します。')

                        # すべての視聴中クライアントのライブストリームへの接続を切断する
                        live_stream.disconnectAll()

                        # PSI/SI データアーカイバーを終了・破棄する
                        if live_stream.psi_data_archiver is not None:
                            await live_stream.psi_data_archiver.destroy()
                            live_stream.psi_data_archiver = None

                        # チューナーとのストリーミング接続を明示的に閉じる
                        await live_stream.tuner.disconnect(live_stream.live_stream_id)

                        # チューナーの制御権限を移譲する
                        if live_stream.tuner.handoff(live_stream.live_stream_id, self.live_stream_id) is False:
                            continue

                        # 実行中のタスクがあればキャンセルする
                        if live_stream._live_encoding_task_ref is not None:
                            old_live_encoding_task = live_stream._live_encoding_task_ref
                            old_live_encoding_task.cancel()

                            # タスクの完了を最大 10 秒待つ
                            ## エンコーダープロセスの kill とバックグラウンドタスクの完了を含め、通常は 0.5 秒程度で完了する
                            ## EDCB との通信ハングなどで無期限にブロックされることを防ぐためにタイムアウトを設ける
                            ## asyncio.wait() はタスクの状態を変更しないため、タイムアウトしても旧タスクは自然終了を続ける
                            done, _ = await asyncio.wait(
                                {old_live_encoding_task},
                                timeout=10.0,
                            )
                            if not done:
                                live_stream.__detachLiveEncodingTaskRef(old_live_encoding_task)
                                logging.warning(f'{live_stream.log_prefix} Encoding task cleanup did not complete within 10 seconds.')

                            if live_stream._live_encoding_task_ref == old_live_encoding_task:
                                live_stream._live_encoding_task_ref = None

                        # チューナーインスタンスを移譲する
                        self.tuner = live_stream.tuner
                        live_stream.tuner = None
                        found_reusable_tuner = True
                        break

                    if found_reusable_tuner is True:
                        break
                    if should_wait_next_retry is False:
                        break

                    await asyncio.sleep(0.1)

            # Mirakurun バックエンドの場合は、現在 Idling 状態のライブストリームを Offline にしてチューナーリソースを解放する
            ## Mirakurun バックエンドではチューナーインスタンスの直接移譲はできないため、
            ## Idling ストリームを Offline にして Controller の自然終了 → Reader 内での HTTP セッション切断を通じて
            ## Mirakurun/mirakc 側でチューナーが解放されるのを待つ形になる
            elif should_start_task is True and is_edcb_backend is False:

                # 画質切り替えなどタイミングの問題で Idling なストリームがない事もあるので、リトライする
                ## ONAir (client_count == 0) のストリームが存在する場合、近いタイミングで Idling に遷移する可能性があるため
                for _ in range(15):

                    # 現在 Idling 状態のライブストリームがあれば
                    idling_live_streams = self.getIdlingLiveStreams()
                    if len(idling_live_streams) > 0:
                        # チューナーリソースを解放する
                        idling_live_streams[0].setStatus('Offline', '新しいライブストリームが開始されたため、チューナーリソースを解放しました。')
                        break

                    # 現在 ONAir 状態のライブストリームがなく、リトライしたところで Idling なライブストリームが取得できる見込みがない
                    onair_live_streams = self.getONAirLiveStreams()
                    if len(onair_live_streams) == 0:
                        break

                    await asyncio.sleep(0.1)

            # エンコードタスクを非同期で実行
            if should_start_task is True:
                instance = LiveEncodingTask(self)
                self._live_encoding_task_instance_ref = instance
                self._live_encoding_task_ref = asyncio.create_task(instance.run())
                self.__registerLiveEncodingTaskRef(self._live_encoding_task_ref, instance)

        # ***** クライアントの登録 *****

        # ライブストリームクライアントのインスタンスを生成・登録する
        async with self._tuner_lock:
            client = LiveStreamClient(self, client_type, prepare_token, playback_session_id)
            self._clients.append(client)
            logging.info(f'{self.log_prefix} Client Connected. Client ID: {client.client_id}')

        # ***** アイドリングからの復帰 *****

        # ライブストリームが Idling 状態な場合、ONAir 状態に戻す（アイドリングから復帰）
        if current_status == 'Idling':
            self.setStatus('ONAir', 'ライブストリームは ONAir です。')

        # ライブストリームクライアントのインスタンスを返す
        return client


    def disconnect(
        self,
        client: LiveStreamClient,
        *,
        cleanup_prepare: bool = True,
        retire_playback_session: bool = True,
    ) -> None:
        """
        指定されたクライアントのライブストリームへの接続を切断する
        このメソッドを実行すると LiveStreamClient インスタンスはライブストリームのクライアントリストから削除され、それ以降機能しなくなる
        LiveStreamClient を使い終わったら必ず呼び出すこと (さもなければ誰も見てないのに視聴中扱いでエンコードタスクが実行され続けてしまう)

        Args:
            client (LiveStreamClient): ライブストリームクライアントのインスタンス
            cleanup_prepare (bool): Prepare lease の自動 finalize を予約するか。
            retire_playback_session (bool): 通常 session の encoder cleanup を予約するか。

        Returns:
            None
        """

        # 指定されたライブストリームクライアントを削除する
        ## すでにタイムアウトなどで削除されていたら何もしない
        prepare_token = client.prepare_token
        playback_session_id = client.playback_session_id
        disconnected = False
        try:
            self._clients.remove(client)
            disconnected = True
            logging.info(f'{self.log_prefix} Client Disconnected. Client ID: {client.client_id}')
        except ValueError:
            pass
        del client
        if disconnected is False:
            return
        if prepare_token is not None and cleanup_prepare is True:
            self.__finalizePrepareClientRemovalSoon(
                prepare_token,
                playback_session_id = playback_session_id,
                reason = 'disconnect',
            )
        elif (
            prepare_token is None and
            playback_session_id is not None and
            retire_playback_session is True
        ):
            self.__schedulePlaybackSessionRetirement(playback_session_id)


    def disconnectAll(self) -> None:
        """
        すべてのクライアントのライブストリームへの接続を切断する
        disconnect() とは違い、LiveStreamClient の操作元ではなくエンコードタスク側から操作することを想定している

        Args:
            None

        Returns:
            None
        """

        # 先に管理リストを空にしてからスナップショットを反復する
        ## 反復中に disconnect() で同じリストを破壊すると、複数クライアントの一部へ EOF が届かない
        ## 先に切り離すことで、二重呼び出しも何もしない安全な操作になる
        clients, self._clients = self._clients, []

        # すべての MPEG-TS クライアントを best-effort で終了させる
        for client in clients:
            prepare_token = client.prepare_token
            try:
                # Queue 待機中のクライアントへストリーム終了を通知する
                if client.client_type == 'mpegts':
                    client.writeStreamData(None)
                logging.info(f'{self.log_prefix} Client Disconnected. Client ID: {client.client_id}')
            except Exception:
                # 一部のクライアントへの通知失敗で、後続クライアントの Queue を残留させない
                logging.warning(f'{self.log_prefix} Failed to notify client disconnection during cleanup.')
            if prepare_token is not None:
                self.__finalizePrepareClientRemovalSoon(
                    prepare_token,
                    playback_session_id = client.playback_session_id,
                    reason = 'disconnect_all',
                )


    def getStatus(self) -> LiveStreamStatus:
        """
        ライブストリームのステータスを取得する

        Args:
            None

        Returns:
            LiveStreamStatus: ライブストリームのステータス
        """

        telemetry = self.getTelemetry().snapshot()
        return LiveStreamStatus(
            status = self._status,  # ライブストリームの現在のステータス
            detail = self._detail,  # ライブストリームの現在のステータスの詳細情報
            started_at = self._started_at,  # ライブストリームが開始された (ステータスが Offline or Restart → Standby に移行した) 時刻
            updated_at = self._updated_at,  # ライブストリームのステータスが最後に更新された時刻
            client_count = len(self._clients),  # ライブストリームに接続中のクライアント数
            prepare_state = telemetry.prepare_state,
            encoder_startup_elapsed_ms = telemetry.encoder_startup_elapsed_ms,
            last_output_age_ms = telemetry.last_output_age_ms,
            anchor_generation_id = telemetry.anchor_generation_id,
            anchor_sequence = telemetry.anchor_sequence,
        )


    def setStatus(self, status: Literal['Offline', 'Standby', 'ONAir', 'Idling', 'Restart'], detail: str, quiet: bool = False) -> bool:
        """
        ライブストリームのステータスを設定する

        Args:
            status (Literal['Offline', 'Standby', 'ONAir', 'Idling', 'Restart']): ライブストリームのステータス
            detail (str): ステータスの詳細
            quiet (bool): ステータス設定のログを出力するかどうか

        Returns:
            bool: ステータスが更新されたかどうか (更新が実際には行われなかった場合は False を返す)
        """

        # ステータスも詳細も現在の状態と重複しているなら、更新を行わない（同じ内容のイベントが複数発生するのを防ぐ）
        if self._status == status and self._detail == detail:
            return False

        # ステータスが Offline or Restart かつ現在の状態と重複している場合は、更新を行わない
        ## Offline や Restart は Standby に移行しない限り同じステータスで詳細が変化することはありえないので、
        ## ステータス詳細が上書きできてしまう状態は不適切
        ## ただ LiveEncodingTask で非同期的にステータスをセットしている関係で上書きしてしまう可能性があるため、ここで上書きを防ぐ
        if (status == 'Offline' or status == 'Restart') and status == self._status:
            return False

        # ステータスは Offline から Restart に移行してはならない
        if self._status == 'Offline' and status == 'Restart':
            return False

        # ストリーム開始 (Offline or Restart → Standby) 時、started_at と stream_data_written_at を更新する
        # ここで更新しておかないと、いつまで経っても初期化時の古いタイムスタンプが使われてしまう
        if ((self._status == 'Offline' or self._status == 'Restart') and status == 'Standby'):
            self._started_at = time.time()
            self._stream_data_written_at = time.time()

        # ステータス変更のログを出力
        if quiet is False:
            logging.info(f'{self.log_prefix} [Status: {status}] {detail}')

        # ストリーム起動完了時 (Standby → ONAir) 時のみ、ストリームの起動にかかった時間も出力
        if self._status == 'Standby' and status == 'ONAir':
            logging.info(f'{self.log_prefix} Startup complete. ({round(time.time() - self._started_at, 2)} sec)')

        # ログ出力を待ってからステータスと詳細をライブストリームにセット
        self._status = status
        self._detail = detail

        # 最終更新のタイムスタンプを更新
        self._updated_at = time.time()

        # チューナーインスタンスが存在する場合 (= EDCB バックエンド利用時) のみ
        if self.tuner is not None:

            # Idling への切り替え時、チューナーをアンロックして再利用できるように
            if self._status == 'Idling':
                self.tuner.unlock(self.live_stream_id)

            # ONAir への切り替え（復帰）時、再びチューナーをロックして制御を横取りされないように
            if self._status == 'ONAir':
                self.tuner.lock(self.live_stream_id)

        return True


    def getStreamDataWrittenAt(self) -> float:
        """
        ストリームデータの最終書き込み時刻を取得する

        Returns:
            float: ストリームデータの最終書き込み時刻
        """

        return self._stream_data_written_at


    def writeStreamData(self, stream_data: bytes) -> None:
        """
        接続している全ての mpegts クライアントの Queue にストリームデータを書き込む
        同時にストリームデータの最終書き込み時刻を更新し、クライアントがタイムアウトしていたら削除する

        Args:
            stream_data (bytes): 書き込むストリームデータ

        Returns:
            None
        """

        # ストリームデータの書き込み時刻
        now = time.time()

        # 接続している全てのクライアントの Queue にストリームデータを書き込む
        for client in self._clients.copy():

            # タイムアウト秒数は通常 10 秒
            # SMB400 + DMirakurun 経路の BS4K は初回起動に 10 秒以上かかることがあるため長めに待つ
            timeout = 30 if self.live_stream_id.startswith('bs4k') else 10

            # 最終読み取り時刻を指定秒数過ぎたクライアントはタイムアウトと判断し、クライアントを削除する
            ## 主にネットワークが切断されたなどの理由で発生する
            if now - client.stream_data_read_at > timeout:
                try:
                    client.writeStreamData(None)
                except Exception:
                    logging.warning(
                        f'{self.log_prefix} Failed to notify timed-out client before disconnection.'
                    )
                self._clients.remove(client)
                logging.info(f'{self.log_prefix} Client Disconnected (Timeout). Client ID: {client.client_id}')
                if client.prepare_token is not None:
                    self.__finalizePrepareClientRemovalSoon(
                        client.prepare_token,
                        playback_session_id = client.playback_session_id,
                        reason = 'client_timeout',
                    )
                elif client.playback_session_id is not None:
                    self.__schedulePlaybackSessionRetirement(client.playback_session_id)
                continue

            # ストリームデータを書き込む (クライアント種別が mpegts の場合のみ)
            if client.client_type == 'mpegts':
                client.writeStreamData(stream_data)

        # ストリームデータが空でなければ、最終書き込み時刻を更新
        if stream_data != b'':
            self._stream_data_written_at = now
            self.getTelemetry().observeOutput(stream_data)
