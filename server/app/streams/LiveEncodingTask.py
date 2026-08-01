
# Type Hints を指定できるように
# ref: https://stackoverflow.com/a/33533514/17124142
from __future__ import annotations

import asyncio
import gc
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Literal, cast

import aiofiles
import aiohttp
import anyio
import httpx
from aiofiles.threadpool.text import AsyncTextIOWrapper
from biim.mpeg2ts import ts

from app import logging
from app.config import Config
from app.constants import (
    API_REQUEST_HEADERS,
    HTTPX_CLIENT,
    LIBRARY_PATH,
    LOGS_DIR,
    QUALITY,
    QUALITY_TYPES,
)
from app.models.Channel import Channel
from app.streams.KonomiTVBS4KPlaybackCapabilities import (
    KonomiTVBS4KPlaybackCapabilityProbe,
)
from app.streams.KonomiTVBS4KPlaybackEncoding import (
    BuildKonomiTVBS4KLiveAspectPreservingScaleFilters,
    BuildKonomiTVBS4KLiveHardwareVideoFilters,
    CanUseKonomiTVBS4KLiveHardwareFilterPath,
    KonomiTVBS4KAudioCodec,
    KonomiTVBS4KVideoBitDepth,
    KonomiTVBS4KVideoCodec,
    ParseKonomiTVBS4KAdvancedLiveMuxrateKbps,
    ResolveKonomiTVBS4KAdvancedLiveMuxrate,
    ResolveKonomiTVBS4KLiveEncodePlan,
    ResolveKonomiTVBS4KLiveHwDownloadFormat,
    ResolveKonomiTVBS4KPlaybackVideoBitrate,
)
from app.streams.LivePSIDataArchiver import LivePSIDataArchiver
from app.streams.LiveSourceAspectMonitor import (
    EstimateDefaultLiveSourceGeometry,
    LiveSourceAspectMonitor,
    LiveSourceVideoGeometry,
)
from app.streams.LiveSourceCoordinator import (
    LIVE_SOURCE_COORDINATOR,
    LiveSourceDescriptor,
    LiveSourceError,
    LiveSourceKey,
    LiveSourcePreemptedError,
    LiveSourceSubscriberError,
    LiveSourceSubscription,
)
from app.streams.RecordedPlaybackCapabilities import (
    RecordedPlaybackBackend,
    RecordedPlaybackCapabilityProbe,
)
from app.streams.StreamEncodingOptions import GetEncoderForLiveChannel
from app.streams.TSCodecBridgeRuntime import TSCodecBridgeRuntimeVerifier
from app.utils import GetMirakurunAPIEndpointURL
from app.utils.edcb.EDCBTuner import EDCBTuner
from app.utils.edcb.PipeStreamReader import PipeStreamReader


if TYPE_CHECKING:
    from app.streams.LiveStream import LiveStream


@dataclass
class _LiveEncodingTaskCleanupContext:
    """1 回の LiveEncodingTask.run() が所有する後始末対象を保持する。"""

    # 親プロセスが所有する tsreadex の入出力パイプ
    tsreadex_read_pipe: int | None = None
    tsreadex_write_pipe: int | None = None

    # run() が起動した子プロセス
    tsreadex: asyncio.subprocess.Process | None = None
    encoder: asyncio.subprocess.Process | None = None
    bridge: asyncio.subprocess.Process | None = None

    # エンコーダーと TS Codec Bridge を直結する親プロセス所有のパイプ
    bridge_read_pipe: int | None = None
    bridge_write_pipe: int | None = None

    # Mirakurun Service Stream API の接続
    response: aiohttp.ClientResponse | None = None
    session: aiohttp.ClientSession | None = None

    # 通常の映像付きライブで共有する source subscription
    source_subscription: LiveSourceSubscription | None = None

    # run() が作成した PSI/SI アーカイバーと非同期タスク
    psi_data_archiver: LivePSIDataArchiver | None = None
    background_tasks: set[asyncio.Task[None]] = field(default_factory=set)

    # 後始末を一度だけ実行するためのフラグ
    is_cleanup_completed: bool = False
    # 回収できなかった資源。空集合のときだけ cleanup を確認済みと扱う。
    cleanup_failures: set[str] = field(default_factory=set)


class LiveEncodingTask:

    # H.264 再生時のエンコード後のストリームの GOP 長 (秒)
    GOP_LENGTH_SECONDS_H264: ClassVar[float] = 0.5

    # H.265 再生時のエンコード後のストリームの GOP 長 (秒)
    GOP_LENGTH_SECONDS_H265: ClassVar[float] = float(2)

    # エンコードタスクの最大リトライ回数
    ## この数を超えた場合はエンコードタスクを再起動しない（無限ループを避ける）
    MAX_RETRY_COUNT: ClassVar[int] = 10  # 10回まで

    # 最終TS出力がこの時間継続してから、再起動回数を新しい障害系列としてリセットする
    PIPELINE_RETRY_RESET_STABLE_SECONDS: ClassVar[float] = 10.0

    # 異常時の診断へ残すFFmpeg stderrの最大行数
    ENCODER_LOG_HISTORY_LIMIT: ClassVar[int] = 100

    # チューナーから放送波 TS を読み取る際のタイムアウト (秒)
    TUNER_TS_READ_TIMEOUT: ClassVar[int] = 15

    # エンコーダーの出力を読み取る際のタイムアウト (Standby 時) (秒)
    ENCODER_TS_READ_TIMEOUT_STANDBY: ClassVar[int] = 20

    # エンコーダーの出力を読み取る際のタイムアウト (ONAir 時) (秒)
    # AMF バックエンドは GPU パイプラインの初期化に時間がかかるため、10 秒に設定
    ENCODER_TS_READ_TIMEOUT_ONAIR: ClassVar[int] = 5
    ENCODER_TS_READ_TIMEOUT_ONAIR_AMF: ClassVar[int] = 10

    # 高度コーデック用 TS を規格化する Bridge の固定 runtime key
    TS_CODEC_BRIDGE_LIBRARY_PATH_KEY: ClassVar[str] = 'KonomiTVBS4KTSCodecBridge'

    # EPG が一時的に空の時間帯での現在番組再取得間隔 (秒)
    PROGRAM_REFRESH_INTERVAL_SECONDS: ClassVar[float] = 5.0


    def __init__(self, live_stream: LiveStream) -> None:
        """
        LiveStream のインスタンスに基づくライブエンコードタスクを初期化する
        このエンコードタスクが LiveStream を実質的に制御する形になる

        Args:
            live_stream (LiveStream): LiveStream のインスタンス

        Returns:
            None
        """

        # ライブストリームのインスタンスをセット
        self.live_stream = live_stream

        # エンコードタスクのリトライ回数のカウント
        self._retry_count = 0

        # 現在の run() が所有するリソース
        ## run() の finally から、通常終了・例外・キャンセルのいずれでも回収する
        self._cleanup_context: _LiveEncodingTaskCleanupContext | None = None
        # 同一管理 Task 内の内部再起動をまたいで、過去世代の回収失敗も保持する。
        self._cleanup_failures: set[str] = set()

        # 放送波の薄い SAR 監視（Reader が更新、エンコード option 構築が参照）
        self._live_source_aspect_monitor = LiveSourceAspectMonitor()
        self._live_source_geometry: LiveSourceVideoGeometry | None = None


    @staticmethod
    def shouldRestartEncoderForVideoResolutionChange(
        previous_video_resolution: str | None,
        current_video_resolution: str | None,
    ) -> bool:
        """
        番組境界で映像解像度が変化したとき、エンコーダーの再起動が必要か返す。

        メタデータ欠落と実際の解像度変化を混同しないよう、両方が既知の場合だけ判定する。

        Args:
            previous_video_resolution (str | None): 切り替え前番組の映像解像度。
            current_video_resolution (str | None): 切り替え後番組の映像解像度。

        Returns:
            bool: 両方が既知で異なる場合は True。
        """

        return (
            previous_video_resolution is not None and
            current_video_resolution is not None and
            previous_video_resolution != current_video_resolution
        )


    def isCleanupConfirmed(self) -> bool:
        """
        最終実行世代の全資源が回収できたか返す。

        Returns:
            bool: cleanup が完了し、回収失敗が一つもなければ True。
        """

        cleanup_context = self._cleanup_context
        return (
            cleanup_context is not None
            and cleanup_context.is_cleanup_completed is True
            and len(self._cleanup_failures) == 0
            and len(cleanup_context.cleanup_failures) == 0
        )


    def getCleanupFailures(self) -> tuple[str, ...]:
        """
        最終実行世代で回収できなかった資源名を返す。

        Returns:
            tuple[str, ...]: 安定した順序に並べた cleanup failure 名。
        """

        cleanup_context = self._cleanup_context
        if cleanup_context is None:
            return tuple(sorted(self._cleanup_failures))
        return tuple(sorted(self._cleanup_failures | cleanup_context.cleanup_failures))


    def getRequestedVideoCodec(self) -> KonomiTVBS4KVideoCodec:
        """
        現在のライブストリームが要求する映像コーデックを返す。

        Returns:
            KonomiTVBS4KVideoCodec: 正規化済みの要求映像コーデック。
        """

        return self.live_stream.encoding_options.video_codec


    def getRequestedVideoBitDepth(self) -> KonomiTVBS4KVideoBitDepth:
        """
        現在のライブストリームが要求する映像 bit depth を返す。

        Returns:
            KonomiTVBS4KVideoBitDepth: 正規化済みの要求映像 bit depth。
        """

        return self.live_stream.encoding_options.video_bit_depth


    def getRequestedAudioCodec(self) -> KonomiTVBS4KAudioCodec:
        """
        現在のライブストリームが要求する音声コーデックを返す。

        Returns:
            KonomiTVBS4KAudioCodec: 正規化済みの要求音声コーデック。
        """

        return self.live_stream.encoding_options.audio_codec


    def isTSCodecBridgeRequired(
        self,
        is_radiochannel: bool = False,
    ) -> bool:
        """
        FFmpeg の TS 出力を TS Codec Bridge へ通す必要があるか返す。

        Args:
            is_radiochannel (bool): 映像を持たないラジオチャンネルかどうか。

        Returns:
            bool: VP9 / AV1 映像または Opus 音声を Bridge で整形する必要がある場合は True。
        """

        return (
            (
                is_radiochannel is False and
                self.getRequestedVideoCodec() in ('vp9', 'av1')
            ) or
            self.getRequestedAudioCodec() == 'opus'
        )

    @classmethod
    def canResetRetryCount(
        cls,
        pipeline_output_started_at: float | None,
        bridge_returncode: int | None,
        *,
        bridge_required: bool,
        now: float,
    ) -> bool:
        """最終TS出力が安定し、必要なBridgeも生存している場合だけretry系列をリセットする。"""

        return (
            pipeline_output_started_at is not None
            and now - pipeline_output_started_at >= cls.PIPELINE_RETRY_RESET_STABLE_SECONDS
            and (bridge_required is False or bridge_returncode is None)
        )


    def handleEncoderProgressLine(
        self,
        line: str,
        pipeline_output_started_at: float | None,
        bridge_returncode: int | None,
        *,
        bridge_required: bool,
        now: float,
    ) -> bool:
        """
        FFmpeg進捗行から配信状態を更新し、安定した最終TSだけでretry系列をリセットする。

        Args:
            line (str): FFmpegのstderrから取得した1行。
            pipeline_output_started_at (float | None): 最終TSの先頭packetを取得した単調増加時刻。
            bridge_returncode (int | None): Bridgeの終了コード。実行中または未使用ならNone。
            bridge_required (bool): 最終TS経路にBridgeが必要ならTrue。
            now (float): 判定時点の単調増加時刻。

        Returns:
            bool: この進捗行でretry系列をリセットした場合はTrue。
        """

        is_starting_line = (
            'arib parser was created' in line
            or 'Invalid frame dimensions 0x0.' in line
        )
        is_buffering_line = (
            'frame=    1 fps=0.0 q=0.0' in line
            or 'size=       0kB time=00:00' in line
        )
        is_output_progress_line = (
            is_starting_line is False
            and is_buffering_line is False
            and ('frame=' in line or 'bitrate=' in line)
        )

        # Standby中だけ進捗表示から配信状態を遷移させる。
        live_stream_status = self.live_stream.getStatus()
        if live_stream_status.status == 'Standby':
            if is_starting_line is True:
                self.live_stream.setStatus('Standby', 'エンコードを開始しています…')
            elif is_buffering_line is True:
                self.live_stream.setStatus('Standby', 'バッファリングしています…')
            elif is_output_progress_line is True:
                self.live_stream.setStatus('ONAir', 'ライブストリームは ONAir です。')

        # ONAirへ遷移した最初の進捗行だけで判定を終えると、10秒経過後に二度と
        # resetできない。以後の全進捗行でも判定し、実際にresetした後は_count=0で一度だけにする。
        if (
            is_output_progress_line is True
            and live_stream_status.status not in ('Offline', 'Restart')
            and self._retry_count > 0
            and self.canResetRetryCount(
                pipeline_output_started_at,
                bridge_returncode,
                bridge_required = bridge_required,
                now = now,
            )
        ):
            self._retry_count = 0
            return True
        return False


    @classmethod
    def appendBoundedEncoderLogLine(cls, lines: list[str], line: str) -> None:
        """
        FFmpegの最新stderrだけを、長時間ライブで増え続けない固定長履歴へ追加する。

        Args:
            lines (list[str]): 異常時の診断へ残すstderr履歴。
            line (str): 追加する最新stderr行。

        Returns:
            None
        """

        lines.append(line)
        if len(lines) > cls.ENCODER_LOG_HISTORY_LIMIT:
            del lines[:-cls.ENCODER_LOG_HISTORY_LIMIT]


    @staticmethod
    def trackBackgroundTask(
        task: asyncio.Task[None],
        background_tasks: set[asyncio.Task[None]],
    ) -> asyncio.Task[None]:
        """
        バックグラウンド Task を set に登録し、完了時に自動で取り除く。

        Args:
            task (asyncio.Task[None]): 追跡対象の Task
            background_tasks (set[asyncio.Task[None]]): 実行中 Task を保持する set

        Returns:
            asyncio.Task[None]: 登録した Task そのもの
        """

        # 完了済み Task を set に残すと長時間ライブで無界にメモリが増えるため、done callback で discard する
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return task


    def isFullHDChannel(self, network_id: int, service_id: int) -> bool:
        """
        ネットワーク ID とサービス ID から、そのチャンネルでフル HD 放送が行われているかを返す
        放送波の PSI/SI から映像の横解像度を取得する手段がないので、現状 ID 決め打ちになっている
        ref: https://twitter.com/highwaymovies/status/1201282179390562305
        ref: https://twitter.com/fkcb222/status/1630877111677485056
        ref: https://scrapbox.io/ci7lus/%E5%9C%B0%E4%B8%8A%E6%B3%A2%E3%81%AA%E3%81%AE%E3%81%ABFHD%E3%81%AE%E6%94%BE%E9%80%81%E5%B1%80%E6%83%85%E5%A0%B1

        Args:
            network_id (int): ネットワーク ID
            service_id (int): サービス ID

        Returns:
            bool: フル HD 放送が行われているチャンネルかどうか
        """

        # 地デジでフル HD 放送を行っているチャンネルのネットワーク ID と一致する
        ## テレビ宮崎, あいテレビ, びわ湖放送, KNB北日本放送, とちぎテレビ, ABS秋田放送
        if network_id in [31811, 31940, 32038, 32162, 32311, 32466]:
            return True

        # BS でフル HD 放送を行っているチャンネルのサービス ID と一致する
        ## NHK BSプレミアム・WOWOWプライム・WOWOWライブ・WOWOWシネマ・BS11
        if network_id == 0x0004 and service_id in [103, 191, 192, 193, 211]:
            return True

        # BS4K・CS4K (放送終了) は 4K 放送なのでフル HD 扱いとする
        # 現在の KonomiTV-BS4K は 1920×1080 以上の解像度へのエンコードをサポートしていない
        if network_id == 0x000B or network_id == 0x000C:
            return True

        return False


    def buildFFmpeg8SoftwareOptions(self,
        quality: QUALITY_TYPES,
        channel_type: Literal['GR', 'BS', 'CS', 'CATV', 'SKY', 'BS4K'],
        is_fullhd_channel: bool,
        is_oneseg: bool = False,
    ) -> list[str]:
        """
        FFmpeg 8 のソフトウェアライブエンコードオプションを組み立てる

        Args:
            quality (QUALITY_TYPES): 映像の品質
            channel_type (Literal['GR', 'BS', 'CS', 'CATV', 'SKY', 'BS4K']): チャンネルの種類
            is_fullhd_channel (bool): フル HD 放送が実施されているチャンネルかどうか
            is_oneseg (bool): ワンセグサービスかどうか

        Returns:
            list[str]: FFmpeg 8 に渡すオプションが連なる配列
        """

        # オプションの入る配列
        options: list[str] = []

        # 入力ストリームの解析時間
        CONFIG = Config()
        if is_oneseg is True:
            # ワンセグは低フレームレートの H.264 で、GOP の途中から受信を開始すると
            # SPS/PPS・IDR の検出まで時間がかかるため、初回から十分な解析時間を確保する
            analyzeduration = round(2_500_000 + (self._retry_count * 200_000))
        elif channel_type == 'BS4K' and CONFIG.general.encoder_bs4k_input_analysis_enabled is True:
            analyzeduration = round((CONFIG.general.encoder_bs4k_input_analyze * 1000000) + (self._retry_count * 200000))
        elif channel_type == 'SKY':
            # H.264 入力のスカパー！プレミアムサービスは入力ストリームの解析時間を長めにする
            analyzeduration = round(700_000 + (self._retry_count * 200_000))
        else:
            analyzeduration = round(500000 + (self._retry_count * 200000))  # リトライ回数に応じて少し増やす

        # 入力
        ## -analyzeduration をつけることで、ストリームの分析時間を短縮できる
        options.append(f'-f mpegts -analyzeduration {analyzeduration} -i pipe:0')

        # ストリームのマッピング
        ## 実在する映像・音声・データストリームは、各コーデックの設定箇所で一度だけマッピングする
        options.append('-ignore_unknown')

        # フラグ
        ## 主に FFmpeg の起動を高速化するための設定
        ## max_interleave_delta: mux 時に影響するオプションで、増やしすぎると CM で詰まりがちになる
        ## リトライなしの場合は 500K (0.5秒) に設定し、リトライ回数に応じて 100K (0.1秒) ずつ増やす
        if channel_type == 'BS4K':
            max_interleave_delta = round(CONFIG.general.encoder_bs4k_max_interleave_delta + (self._retry_count * 100))
        else:
            max_interleave_delta = round(500 + (self._retry_count * 100))
        if channel_type == 'BS4K' and CONFIG.general.encoder_bs4k_low_latency is False:
            options.append(f'-max_delay 250000 -max_interleave_delta {max_interleave_delta}K -threads auto')
        else:
            # 非 BS4K は視聴クライアントの低遅延 ON / OFF に関係なく、同じ即時出力寄り TS を共有する。
            ## OFF 側の安定性はクライアントの再生バッファで確保し、サーバーのエンコードタスクは分岐させない。
            options.append(
                f'-fflags nobuffer -flags low_delay -max_delay 250000 '
                f'-max_interleave_delta {max_interleave_delta}K -flush_packets 1 -threads auto'
            )

        # 映像
        ## コーデック
        if QUALITY[quality].is_hevc is True:
            options.append('-vcodec libx265')  # H.265/HEVC
        else:
            options.append('-vcodec libx264')  # H.264

        ## ビットレートと品質
        options.append(f'-flags +cgop -vb {QUALITY[quality].video_bitrate} -maxrate {QUALITY[quality].video_bitrate_max}')
        options.append('-preset veryfast -aspect 16:9')
        is_hevc_10bit = (
            self.getRequestedVideoCodec() == 'hevc' and
            self.getRequestedVideoBitDepth() == 10
        )
        if QUALITY[quality].is_hevc is True:
            options.append(f'-profile:v {"main10" if is_hevc_10bit is True else "main"}')
        else:
            options.append('-profile:v high')
        # 明示 query の HEVC 10bit は録画能力と同じ libx265 Main10 契約で出力する。
        ## 旧 -10bit URL は StreamEncodingOptions.fromRequest() が従来の backend 境界で
        ## 8bitへ正規化するため、ここでは解決済みの exact bit depth を黙って変更しない。
        options.append(f'-pix_fmt {"yuv420p10le" if is_hevc_10bit is True else "yuv420p"}')

        # 選択画質の coded size / SAR。入力 4:3 は 16:9 枠へ pillarbox する。
        video_codec: KonomiTVBS4KVideoCodec = 'hevc' if QUALITY[quality].is_hevc is True else 'avc'
        encode_plan = ResolveKonomiTVBS4KLiveEncodePlan(
            QUALITY[quality].width,
            QUALITY[quality].height,
            video_codec = video_codec,
            is_fullhd_channel = is_fullhd_channel,
        )
        aspect_filters = BuildKonomiTVBS4KLiveAspectPreservingScaleFilters(encode_plan)

        ## 最大 GOP 長 (秒)
        ## 30fps なら ×30 、 60fps なら ×60 された値が --gop-len で使われる
        gop_length_second = self.GOP_LENGTH_SECONDS_H264
        if QUALITY[quality].is_hevc is True:
            ## H.265/HEVC では高圧縮化のため、最大 GOP 長を長くする
            gop_length_second = self.GOP_LENGTH_SECONDS_H265

        # ワンセグはプログレッシブかつ約 10～15fps の VFR で放送されているため、
        ## フレームレートの固定やインターレース解除を行わず、入力 PTS をそのまま維持する。
        if is_oneseg is True:
            options.append(f'-vf {",".join(aspect_filters)}')
            options.append(f'-fps_mode vfr -g {30 if QUALITY[quality].is_hevc is True else 8}')
        ## BS4K は 60p (プログレッシブ) で放送されているので、インターレース解除を行わない
        elif channel_type == "BS4K":
            options.append(f'-vf {",".join(aspect_filters)}')
            if '-30fps' in quality:
                options.append(f'-r 30000/1001 -g {int(gop_length_second * 30)}')
            else:
                options.append(f'-r 60000/1001 -g {int(gop_length_second * 60)}')
        else:
            ## インターレース解除 (60i → 60p (フレームレート: 60fps))
            if QUALITY[quality].is_60fps is True:
                options.append(
                    f'-vf yadif=mode=1:parity=-1:deint=1,{",".join(aspect_filters)}'
                )
                options.append(f'-r 60000/1001 -g {int(gop_length_second * 60)}')
            ## インターレース解除 (60i → 30p (フレームレート: 30fps))
            else:
                # 24fps モードでは、テレシネ由来の重複フレームを取り除いて 24/30p 混合 VFR で出力する
                ## dejudder を併用すると、24fps 区間の PTS が 41.7ms 間隔に均されて本来の 24fps に近い時刻列になる
                if self.live_stream.encoding_options.is_24fps_mode_enabled is True:
                    options.append(f'-vf pullup,dejudder,{",".join(aspect_filters)}')
                    options.append(f'-fps_mode vfr -g {int(gop_length_second * 30)}')
                else:
                    options.append(
                        f'-vf yadif=mode=0:parity=-1:deint=1,{",".join(aspect_filters)}'
                    )
                    options.append(f'-r 30000/1001 -g {int(gop_length_second * 30)}')
        # 音声
        # 実在する全音声を同じ順序で出力する。
        # ブラウザ MSE 向けに AAC は常にステレオ AAC-LC へ再エンコードする（copy しない）。
        # 放送波の ADTS/PCE・デュアルモノ分離後 mono・高ビットレート ADTS をそのまま渡すと
        # 奇妙な再生音・片チャンネル化・ノイズになることがある。
        options.append('-map 0:v:0 -map 0:a? -map 0:d?')
        if self.getRequestedAudioCodec() == 'opus':
            options.append('-acodec libopus -application audio -ac 2 -ab 192K -ar 48000')
        else:
            # ワンセグは帯域を抑え、通常は品質定義の音声ビットレートを使う
            audio_bitrate = '96K' if is_oneseg is True else QUALITY[quality].audio_bitrate
            options.append(
                f'-acodec aac -aac_coder twoloop -ac 2 -ab {audio_bitrate} -ar 48000'
            )

        # Bridge の SELECTED_PCR_GAP を避けるため、Opus 付き AVC/HEVC でも固定 muxrate を使う。
        if self.getRequestedAudioCodec() == 'opus':
            options.append(
                f'-muxrate {ResolveKonomiTVBS4KAdvancedLiveMuxrate(QUALITY[quality].video_bitrate_max)} '
                f'-pcr_period 20'
            )

        # 出力
        options.append('-y -f mpegts')  # MPEG-TS 出力ということを明示
        options.append('pipe:1')  # 標準出力へ出力

        # オプションをスペースで区切って配列にする
        result: list[str] = []
        for option in options:
            result += option.split(' ')

        return result


    def buildFFmpeg8AdvancedSoftwareOptions(
        self,
        quality: QUALITY_TYPES,
        channel_type: Literal['GR', 'BS', 'CS', 'CATV', 'SKY', 'BS4K'],
        is_fullhd_channel: bool,
        is_oneseg: bool = False,
    ) -> list[str]:
        """
        VP9 / AV1 を TS Codec Bridge 前提で生成するソフトウェア FFmpeg 8 オプションを組み立てる。

        FFmpeg の MPEG-TS muxer が生成した private PES は Bridge が規格化するため、
        この段階では access unit の再解釈や PSI 書き換えを行わない。

        Args:
            quality (QUALITY_TYPES): 処理または検証対象の画質。
            channel_type (Literal['GR', 'BS', 'CS', 'CATV', 'SKY', 'BS4K']): 入力チャンネルの放送種別。
            is_fullhd_channel (bool): 入力が 1920x1080 のフル HD チャンネルか。
            is_oneseg (bool): 入力がワンセグ放送か。

        Returns:
            list[str]: FFmpeg へ渡す filter または command option 群。
        """

        CONFIG = Config()
        codec = self.getRequestedVideoCodec()
        if codec not in ('vp9', 'av1'):
            raise ValueError(f'Advanced software live codec is unsupported: {codec}')
        bit_depth = self.getRequestedVideoBitDepth()
        codec_spec = RecordedPlaybackBackend.getCodecSpec(codec, bit_depth)
        encoder_name = RecordedPlaybackBackend.getEncoderName('FFmpeg', codec)
        if encoder_name is None:
            raise RuntimeError(f'Unsupported FFmpeg 8 live encoder: FFmpeg/{codec}')

        # 入力解析量と mux の即時出力方針は従来ライブ経路と揃える。
        if is_oneseg is True:
            analyzeduration = round(2_500_000 + (self._retry_count * 200_000))
        elif channel_type == 'BS4K' and CONFIG.general.encoder_bs4k_input_analysis_enabled is True:
            analyzeduration = round(
                (CONFIG.general.encoder_bs4k_input_analyze * 1_000_000) +
                (self._retry_count * 200_000)
            )
        elif channel_type == 'SKY':
            analyzeduration = round(700_000 + (self._retry_count * 200_000))
        else:
            analyzeduration = round(500_000 + (self._retry_count * 200_000))
        low_latency = channel_type != 'BS4K' or CONFIG.general.encoder_bs4k_low_latency is True

        options = ['-f', 'mpegts', '-analyzeduration', str(analyzeduration), '-i', 'pipe:0']
        if low_latency is True:
            options += ['-fflags', 'nobuffer', '-flags', 'low_delay']
        options += [
            '-ignore_unknown',
            '-map', '0:v:0',
            '-map', '0:a?',
            '-map', '0:d?',
        ]

        # VP9/AV1 は bitstream に SAR を載せにくいため、非正方 SAR なら正方画素へ展開する。
        # 入力 4:3 は display 枠へ pillarbox する。
        encode_plan = ResolveKonomiTVBS4KLiveEncodePlan(
            QUALITY[quality].width,
            QUALITY[quality].height,
            video_codec = codec,
            is_fullhd_channel = is_fullhd_channel,
        )
        aspect_filters = BuildKonomiTVBS4KLiveAspectPreservingScaleFilters(encode_plan)

        # 高度コーデックでも従来のインターレース解除・24fps・BS4K progressive 境界を維持する。
        filters: list[str] = []
        if is_oneseg is True or channel_type == 'BS4K':
            filters += aspect_filters
        elif self.live_stream.encoding_options.is_24fps_mode_enabled is True:
            filters += ['pullup', 'dejudder', *aspect_filters]
        elif QUALITY[quality].is_60fps is True:
            filters += ['yadif=mode=1:parity=-1:deint=1', *aspect_filters]
        else:
            filters += ['yadif=mode=0:parity=-1:deint=1', *aspect_filters]
        filters.append(f'format={codec_spec.pixel_format}')
        options += ['-vf', ','.join(filters)]

        bitrate = ResolveKonomiTVBS4KPlaybackVideoBitrate(quality, codec)
        options += [
            '-c:v', encoder_name,
            '-b:v', bitrate.video_bitrate,
            '-maxrate', bitrate.video_bitrate_max,
            '-bufsize', bitrate.video_bitrate_max,
            '-pix_fmt', codec_spec.pixel_format,
            '-aspect', '16:9',
            *RecordedPlaybackBackend.getTuningArguments('FFmpeg', codec),
            '-lag-in-frames', '0',
        ]
        if codec == 'vp9':
            options += [
                '-profile:v', '2' if bit_depth == 10 else '0',
                '-auto-alt-ref', '0',
            ]
        else:
            options += ['-profile:v', '0']

        # 先読みを使わず、各 GOP の先頭を Bridge が random_access_indicator として扱えるようにする。
        if is_oneseg is True:
            options += ['-fps_mode', 'vfr', '-g', '15']
        elif channel_type == 'BS4K':
            frame_rate = 30 if '-30fps' in quality else 60
            options += [
                '-r', '30000/1001' if frame_rate == 30 else '60000/1001',
                '-g', str(frame_rate),
            ]
        elif self.live_stream.encoding_options.is_24fps_mode_enabled is True:
            options += ['-fps_mode', 'vfr', '-g', '30']
        elif QUALITY[quality].is_60fps is True:
            options += ['-r', '60000/1001', '-g', '60']
        else:
            options += ['-r', '30000/1001', '-g', '30']

        # ブラウザ MSE 向け: AAC も Opus もステレオへ正規化して再エンコードする（AAC copy 禁止）
        if self.getRequestedAudioCodec() == 'opus':
            options += [
                '-c:a', 'libopus',
                '-application', 'audio',
                '-ac', '2',
                '-b:a', '192K',
                '-ar', '48000',
            ]
        else:
            audio_bitrate = '96K' if is_oneseg is True else QUALITY[quality].audio_bitrate
            options += [
                '-c:a', 'aac',
                '-aac_coder', 'twoloop',
                '-ac', '2',
                '-b:a', audio_bitrate,
                '-ar', '48000',
            ]
        options += ['-c:d', 'copy']

        max_interleave_delta = (
            round(CONFIG.general.encoder_bs4k_max_interleave_delta + (self._retry_count * 100))
            if channel_type == 'BS4K' else
            round(500 + (self._retry_count * 100))
        )
        options += [
            '-max_delay', '250000',
            '-max_interleave_delta', f'{max_interleave_delta}K',
            '-muxrate', ResolveKonomiTVBS4KAdvancedLiveMuxrate(
                bitrate.video_bitrate_max,
                quality = quality,
                video_codec = codec,
            ),
            '-pcr_period', '20',
        ]
        if low_latency is True:
            options += ['-flush_packets', '1']
        options += ['-y', '-f', 'mpegts', 'pipe:1']
        return options


    def buildFFmpeg8RadioOptions(self) -> list[str]:
        """
        FFmpeg 8 に渡すラジオチャンネル向けオプションを組み立てる
        音声の品質は変えたところでほとんど差がないため、1つだけに固定されている
        品質が固定ならコードにする必要は基本ないんだけど、可読性を高めるために敢えてこうしてある

        Returns:
            list[str]: FFmpeg 8 に渡すオプションが連なる配列
        """

        # オプションの入る配列
        options: list[str] = []

        # 入力
        ## -analyzeduration をつけることで、ストリームの分析時間を短縮できる
        analyzeduration = round(500000 + (self._retry_count * 200000))  # リトライ回数に応じて少し増やす
        options.append(f'-f mpegts -analyzeduration {analyzeduration} -i pipe:0')

        # ストリームのマッピング
        # 音声切り替えのため、実在する音声ストリームをすべてエンコード後の TS に含む
        options.append('-map 0:a? -map 0:d? -ignore_unknown')

        # フラグ
        ## 主に FFmpeg の起動を高速化するための設定
        ## max_interleave_delta: mux 時に影響するオプションで、増やしすぎると CM で詰まりがちになる
        ## リトライなしの場合は 500K (0.5秒) に設定し、リトライ回数に応じて 100K (0.1秒) ずつ増やす
        max_interleave_delta = round(500 + (self._retry_count * 100))
        # ラジオも通常ライブと同じ共有ストリームなので、クライアントモード別に出力を分岐させない。
        options.append(
            f'-fflags nobuffer -flags low_delay -max_delay 250000 '
            f'-max_interleave_delta {max_interleave_delta}K -flush_packets 1 -threads auto'
        )

        # 音声
        ## 音声が 5.1ch かどうかに関わらず、ステレオにダウンミックスする
        if self.getRequestedAudioCodec() == 'opus':
            options.append('-acodec libopus -application audio -ac 2 -ab 192K -ar 48000 -af volume=2.0')
        else:
            options.append('-acodec aac -aac_coder twoloop -ac 2 -ab 192K -ar 48000 -af volume=2.0')

        # 出力
        options.append('-y -f mpegts')  # MPEG-TS 出力ということを明示
        options.append('pipe:1')  # 標準出力へ出力

        # オプションをスペースで区切って配列にする
        result: list[str] = []
        for option in options:
            result += option.split(' ')

        return result


    def buildFFmpeg8Options(
        self,
        quality: QUALITY_TYPES,
        encoder_type: Literal['FFmpeg', 'QSV', 'NVENC', 'AMF'],
        channel_type: Literal['GR', 'BS', 'CS', 'CATV', 'SKY', 'BS4K'],
        is_fullhd_channel: bool,
        is_oneseg: bool = False,
    ) -> list[str]:
        """
        公開設定上のエンコーダー名を FFmpeg 8 の SW/HW 実装へ変換する

        Args:
            quality (QUALITY_TYPES): 映像の品質
            encoder_type (Literal['FFmpeg', 'QSV', 'NVENC', 'AMF']): 公開設定上のエンコーダー名
            channel_type (Literal['GR', 'BS', 'CS', 'CATV', 'SKY', 'BS4K']): チャンネルの種類
            is_fullhd_channel (bool): フル HD 放送が実施されているチャンネルかどうか
            is_oneseg (bool): ワンセグサービスかどうか

        Returns:
            list[str]: FFmpeg 8 に渡すオプションが連なる配列
        """

        # 設定名 FFmpeg はソフトウェアエンコードを表す。
        ## ラジオは映像 HW API を使わないため呼び出し側で buildFFmpeg8RadioOptions() を選択する。
        if encoder_type == 'FFmpeg' and self.getRequestedVideoCodec() in ('vp9', 'av1'):
            return self.buildFFmpeg8AdvancedSoftwareOptions(
                quality,
                channel_type,
                is_fullhd_channel,
                is_oneseg,
            )
        if encoder_type == 'FFmpeg':
            return self.buildFFmpeg8SoftwareOptions(quality, channel_type, is_fullhd_channel, is_oneseg)

        # QSV / NVENC / AMF は FFmpeg 8 の h264_qsv / h264_nvenc / h264_amf などへ対応する。
        # 型検査を迂回した不正な設定値を AMF として扱わないよう、実行境界でも明示的に拒否する。
        if encoder_type not in ('QSV', 'NVENC', 'AMF'):
            raise ValueError(f'Unsupported FFmpeg 8 live backend: {encoder_type}')
        return self.buildFFmpeg8HardwareOptions(
            quality,
            encoder_type,
            channel_type,
            is_fullhd_channel,
            is_oneseg,
        )


    def buildFFmpeg8HardwareOptions(
        self,
        quality: QUALITY_TYPES,
        encoder_type: Literal['QSV', 'NVENC', 'AMF'],
        channel_type: Literal['GR', 'BS', 'CS', 'CATV', 'SKY', 'BS4K'],
        is_fullhd_channel: bool,
        is_oneseg: bool = False,
    ) -> list[str]:
        """
        FFmpeg 8 の QSV / NVENC / AMF ライブエンコードオプションを組み立てる

        Args:
            quality (QUALITY_TYPES): 映像の品質
            encoder_type (Literal['QSV', 'NVENC', 'AMF']): 公開設定上の HW エンコーダー名
            channel_type (Literal['GR', 'BS', 'CS', 'CATV', 'SKY', 'BS4K']): チャンネルの種類
            is_fullhd_channel (bool): フル HD 放送が実施されているチャンネルかどうか
            is_oneseg (bool): ワンセグサービスかどうか

        Returns:
            list[str]: FFmpeg 8 に渡すオプションが連なる配列
        """

        CONFIG = Config()

        # 入力解析量はライブ視聴で検証済みの放送種別ごとの値を維持する。
        ## BS4K で即時出力優先が無効な実機設定では、破損パケットを含む HEVC Main10 を
        ## 安定して認識できる 8MiB / 3 秒を使う。
        if is_oneseg is True:
            input_probesize = f'{round(3000 + (self._retry_count * 500))}K'
            input_analyze = round(2.5 + (self._retry_count * 0.2), 1)
        elif (
            channel_type == 'BS4K' and
            CONFIG.general.encoder_bs4k_input_analysis_enabled is True and
            CONFIG.general.encoder_bs4k_low_latency is False
        ):
            input_probesize = '8M'
            input_analyze = 3.0
        elif channel_type == 'BS4K' and CONFIG.general.encoder_bs4k_input_analysis_enabled is True:
            input_probesize = f'{round(CONFIG.general.encoder_bs4k_input_probesize + (self._retry_count * 500))}K'
            input_analyze = round(CONFIG.general.encoder_bs4k_input_analyze + (self._retry_count * 0.2), 1)
        elif channel_type == 'SKY':
            input_probesize = f'{round(1500 + (self._retry_count * 500))}K'
            input_analyze = round(0.9 + (self._retry_count * 0.2), 1)
        else:
            input_probesize = f'{round(1000 + (self._retry_count * 500))}K'
            input_analyze = round(0.7 + (self._retry_count * 0.2), 1)

        # 明示された codec / bit depth を基準に、FFmpeg 8 の実エンコーダーと frame format を解決する。
        codec = self.getRequestedVideoCodec()
        requested_bit_depth = self.getRequestedVideoBitDepth()
        is_hevc = codec == 'hevc'
        # 明示 query と旧 URL の互換判定は StreamEncodingOptions.fromRequest() で完了している。
        ## 実行境界では解決済みの exact bit depth を backend ごとに黙って変更しない。
        bit_depth: KonomiTVBS4KVideoBitDepth = requested_bit_depth
        codec_spec = RecordedPlaybackBackend.getCodecSpec(codec, bit_depth)
        encoder_name = RecordedPlaybackBackend.getEncoderName(encoder_type, codec)
        if encoder_name is None:
            raise RuntimeError(f'Unsupported FFmpeg 8 live encoder: {encoder_type}/{codec}')

        # 能力検査で選択済みの render node を優先し、未検査なら vendor ID から列挙する。
        ## QSV / AMF は明示した同一デバイスをデコード・フィルター・エンコードで共有する。
        selected_device: str | None = None
        if encoder_type in ('QSV', 'AMF'):
            selected_device = RecordedPlaybackCapabilityProbe.getSelectedDevice(encoder_type, codec, bit_depth)
            if selected_device is None:
                devices = RecordedPlaybackBackend.discoverRenderDevices(encoder_type)
                if len(devices) == 0:
                    raise RuntimeError(f'No compatible render device was found for {encoder_type}.')
                selected_device = devices[0]

        # HW デバイス初期化オプションは入力より前に配置する。
        options: list[str] = []
        if encoder_type == 'QSV':
            options += [
                '-init_hw_device', f'qsv=live_qsv:{selected_device}',
                '-filter_hw_device', 'live_qsv',
                '-hwaccel', 'qsv',
                '-hwaccel_output_format', 'qsv',
            ]
        elif encoder_type == 'NVENC':
            options += [
                '-init_hw_device', 'cuda=live_cuda:0',
                '-filter_hw_device', 'live_cuda',
                '-hwaccel', 'cuda',
                '-hwaccel_output_format', 'cuda',
            ]
        else:
            options += [
                '-init_hw_device', f'vaapi=live_vaapi:{selected_device}',
                '-filter_hw_device', 'live_vaapi',
                '-hwaccel', 'vaapi',
                '-hwaccel_device', 'live_vaapi',
                '-hwaccel_output_format', 'vaapi',
            ]

        # 非 BS4K はクライアントの低遅延 ON / OFF に関係なく、同じ即時出力寄り TS を共有する。
        ## BS4K 即時出力優先 OFF だけは入力解析と安定性を優先し、nobuffer / low_delay を付けない。
        low_latency = channel_type != 'BS4K' or CONFIG.general.encoder_bs4k_low_latency is True
        if low_latency is True:
            options += ['-fflags', 'nobuffer', '-flags', 'low_delay']

        options += [
            '-f', 'mpegts',
            '-probesize', input_probesize,
            '-analyzeduration', str(round(input_analyze * 1_000_000)),
            '-i', 'pipe:0',
            '-ignore_unknown',
            '-map', '0:v:0',
            '-map', '0:a?',
            '-map', '0:d?',
        ]

        # 選択画質の解像度（AV1/VP9 は非正方 SAR 時に正方画素へ展開済み）。
        encode_plan = ResolveKonomiTVBS4KLiveEncodePlan(
            QUALITY[quality].width,
            QUALITY[quality].height,
            video_codec = codec,
            is_fullhd_channel = is_fullhd_channel,
        )
        aspect_filters = BuildKonomiTVBS4KLiveAspectPreservingScaleFilters(encode_plan)

        is_hevc_10bit = is_hevc is True and bit_depth == 10
        is_interlaced = channel_type != 'BS4K' and is_oneseg is False
        encoder_pixel_format = codec_spec.encoder_pixel_format
        is_24fps = self.live_stream.encoding_options.is_24fps_mode_enabled is True

        # 案1: 薄い SAR 監視で 16:9 と分かっている入力は full-GPU（vpp_qsv 等）。
        ## 4:3 や 24fps など pad / pullup が必要なときだけ従来の hwdownload + SW 経路。
        source_geometry = self._live_source_geometry
        if source_geometry is None:
            source_geometry = EstimateDefaultLiveSourceGeometry(
                channel_type,
                is_fullhd_channel = is_fullhd_channel,
                is_oneseg = is_oneseg,
            )
        use_hardware_filters = CanUseKonomiTVBS4KLiveHardwareFilterPath(
            source_dar_width = source_geometry.dar_width,
            source_dar_height = source_geometry.dar_height,
            is_24fps_mode_enabled = is_24fps,
        )

        filters: list[str] = []
        if use_hardware_filters is True:
            filters = BuildKonomiTVBS4KLiveHardwareVideoFilters(
                encoder_type,
                encode_width = encode_plan.encode_width,
                encode_height = encode_plan.encode_height,
                encoder_pixel_format = encoder_pixel_format,
                is_interlaced = is_interlaced,
                is_60fps = QUALITY[quality].is_60fps is True,
                low_latency = low_latency,
            )
            log_prefix = self.live_stream.log_prefix
            logging.info(
                f'{log_prefix} Live video filters: hardware path '
                f'({source_geometry.source}, '
                f'{source_geometry.coded_width}x{source_geometry.coded_height} '
                f'DAR {source_geometry.dar_width}:{source_geometry.dar_height} → '
                f'{encode_plan.encode_width}x{encode_plan.encode_height})'
            )
        else:
            # ISDB の SAR をフレームごとに使い 4:3/16:9 を動的判定する SW 経路。
            ## vpp は SAR を落とすため、pad が必要な DAR では hwdownload する。
            download_format = ResolveKonomiTVBS4KLiveHwDownloadFormat(
                encoder_type,
                channel_type = channel_type,
                encoder_pixel_format = encoder_pixel_format,
            )
            filters.append(f'hwdownload,format={download_format}')
            if download_format != encoder_pixel_format:
                filters.append(f'format={encoder_pixel_format}')
            if is_interlaced is True:
                if is_24fps is True:
                    filters += ['pullup', 'dejudder']
                elif QUALITY[quality].is_60fps is True:
                    filters.append('yadif=mode=1:parity=-1:deint=1')
                else:
                    filters.append('yadif=mode=0:parity=-1:deint=1')
            filters += aspect_filters
            filters.append(f'format={encoder_pixel_format}')
            if encoder_type == 'QSV':
                filters.append('hwupload=extra_hw_frames=64')
            elif encoder_type == 'NVENC':
                filters.append('hwupload_cuda')
            else:
                filters.append('hwupload')
            log_prefix = self.live_stream.log_prefix
            logging.info(
                f'{log_prefix} Live video filters: software path '
                f'({source_geometry.source}, '
                f'DAR {source_geometry.dar_width}:{source_geometry.dar_height}, '
                f'24fps={is_24fps})'
            )
        options += ['-vf', ','.join(filters)]

        # 高度コーデックは共通 bitrate domain、AVC / HEVC は旧 QUALITY 定義を利用する。
        if codec in ('vp9', 'av1'):
            bitrate = ResolveKonomiTVBS4KPlaybackVideoBitrate(quality, codec)
            video_bitrate = bitrate.video_bitrate
            video_bitrate_max = bitrate.video_bitrate_max
        else:
            video_bitrate = QUALITY[quality].video_bitrate
            video_bitrate_max = QUALITY[quality].video_bitrate_max
        options += [
            '-c:v', encoder_name,
            '-b:v', video_bitrate,
            '-maxrate', video_bitrate_max,
            '-aspect', '16:9',
        ]
        # VP9/AV1 は固定 muxrate 上の I フレーム突発がクライアント underrun の主因になるため、
        ## SW 経路と同じく VBV 相当の bufsize でピークを抑える。
        if codec in ('vp9', 'av1'):
            options += ['-bufsize', video_bitrate_max]
        if encoder_type == 'QSV':
            options += [
                '-preset', 'medium',
                '-scenario', 'livestreaming',
                '-async_depth', '1' if low_latency is True else '4',
            ]
            if low_latency is True or codec in ('vp9', 'av1'):
                options += ['-look_ahead', '0', '-bf', '0']
            if is_hevc is True:
                options += ['-extbrc', '1', '-mbbrc', '1']
        elif encoder_type == 'NVENC':
            options += [
                '-preset', 'p4',
                '-tune', 'll' if low_latency is True else 'hq',
                '-rc', 'vbr',
                '-rc-lookahead', '0' if low_latency is True or codec in ('vp9', 'av1') else '16',
                '-spatial-aq', '1',
                '-temporal-aq', '1',
            ]
            if low_latency is True or codec in ('vp9', 'av1'):
                options += ['-zerolatency', '1', '-bf', '0']
        else:
            options += [
                '-quality', 'balanced',
                '-rc', 'vbr_latency' if low_latency is True else 'vbr_peak',
                '-async_depth', '1' if low_latency is True else '16',
            ]
            if low_latency is True or codec in ('vp9', 'av1'):
                options += ['-usage', 'lowlatency', '-latency', '1', '-bf', '0']

        # hardware frame を渡す QSV/CUDA は SW pixel format を強制しない。
        ## AMF は VAAPI から system memory へ戻した NV12/P010 を受け取る。
        if encoder_type == 'NVENC':
            options += ['-pix_fmt', 'cuda']
        elif encoder_type == 'AMF':
            options += ['-pix_fmt', codec_spec.encoder_pixel_format]
        if codec == 'hevc':
            options += ['-profile:v', 'main10' if is_hevc_10bit is True else 'main']
        elif codec == 'avc':
            options += ['-profile:v', 'high']
        elif codec == 'vp9':
            options += ['-profile:v', 'profile2' if bit_depth == 10 else 'profile0']
        elif encoder_type != 'NVENC':
            options += ['-profile:v', 'main']

        # ワンセグは入力 PTS を維持し、それ以外は既存の品質別フレームレートへ揃える。
        gop_length_second = (
            float(1)
            if codec in ('vp9', 'av1') else
            (self.GOP_LENGTH_SECONDS_H265 if is_hevc is True else self.GOP_LENGTH_SECONDS_H264)
        )
        if is_oneseg is True:
            options += ['-fps_mode', 'vfr', '-g', '30' if is_hevc is True else '8']
        elif channel_type == 'BS4K':
            if '-30fps' in quality:
                options += ['-r', '30000/1001', '-g', str(int(gop_length_second * 30))]
            else:
                options += ['-r', '60000/1001', '-g', str(int(gop_length_second * 60))]
        elif self.live_stream.encoding_options.is_24fps_mode_enabled is True:
            options += ['-fps_mode', 'vfr', '-g', str(int(gop_length_second * 30))]
        elif QUALITY[quality].is_60fps is True:
            options += ['-r', '60000/1001', '-g', str(int(gop_length_second * 60))]
        else:
            options += ['-r', '30000/1001', '-g', str(int(gop_length_second * 30))]

        # 実在する全音声を同じ順序で変換する。
        # ブラウザ MSE 向けに AAC も Opus もステレオへ正規化して再エンコードする（AAC copy 禁止）。
        # 放送波 ADTS の copy は PCE・デュアルモノ分離後 mono などが原因で奇妙な再生音になる。
        if self.getRequestedAudioCodec() == 'opus':
            options += [
                '-c:a', 'libopus',
                '-application', 'audio',
                '-ac', '2',
                '-b:a', '192K',
                '-ar', '48000',
            ]
        else:
            audio_bitrate = '96K' if is_oneseg is True else QUALITY[quality].audio_bitrate
            options += [
                '-c:a', 'aac',
                '-aac_coder', 'twoloop',
                '-ac', '2',
                '-b:a', audio_bitrate,
                '-ar', '48000',
            ]
        options += ['-c:d', 'copy']

        # mux 待ち時間と出力先は旧ライブ経路を維持する。
        max_interleave_delta = (
            round(CONFIG.general.encoder_bs4k_max_interleave_delta + (self._retry_count * 100))
            if channel_type == 'BS4K' else
            round(500 + (self._retry_count * 100))
        )
        options += [
            '-max_delay', '250000',
            '-max_interleave_delta', f'{max_interleave_delta}K',
        ]
        # Bridge 経由の Opus も PCR 間隔を保証するため、VP9/AV1 と同様に固定 muxrate を付ける。
        if codec in ('vp9', 'av1') or self.getRequestedAudioCodec() == 'opus':
            options += [
                '-muxrate', ResolveKonomiTVBS4KAdvancedLiveMuxrate(
                    video_bitrate_max,
                    quality = quality,
                    video_codec = codec,
                ),
                '-pcr_period', '20',
            ]
        if low_latency is True:
            options += ['-flush_packets', '1']
        options += ['-y', '-f', 'mpegts', 'pipe:1']
        return options


    async def acquireMirakurunTuner(self, channel_type: Literal['GR', 'BS', 'CS', 'CATV', 'SKY', 'BS4K']) -> bool:
        """
        Mirakurun / mirakc で空きチューナーを確保できるまで待機する
        mirakc は空きチューナーがない場合に 404 を返すので (バグ？) 、それを避けるために予め空きチューナーがあるかどうかを確認する
        0.5 秒間待機しても空きチューナーがなければ False を返す (共聴できる場合もあるので、受信できないとは限らない)

        Args:
            channel_type (Literal['GR', 'BS', 'CS', 'CATV', 'SKY', 'BS4K']): チャンネルタイプ

        Returns:
            bool: チューナーを確保できたかどうか
        """

        CONFIG = Config()
        assert CONFIG.general.live_stream_backend == 'Mirakurun', 'This method is only for Mirakurun backend.'

        # Mirakurun / mirakc は通常チャンネルタイプが GR, BS, CS, SKY しかないので、
        # フォールバックとして BS4K を BS に、CATV を CS に変換する
        ## ただし、BS4K などのチャンネルタイプに対応したチューナーが登録されている場合は、
        ## フォールバック先のチューナーを先に拾わないよう、必ず本来のチャンネルタイプを優先する
        fallback_channel_type = channel_type
        if channel_type == 'BS4K':
            fallback_channel_type = 'BS'
        elif channel_type == 'CATV':
            fallback_channel_type = 'CS'

        mirakurun_or_mirakc = 'Mirakurun'
        async with HTTPX_CLIENT() as client:

            # 0.1 秒間隔で最大 0.5 秒間チューナーの空きを確認する
            ## 空きチューナーがなくても利用状況によっては共聴できるので、あまり待ちすぎると無駄な時間がかかる
            ## Mirakurun / mirakc はチャンネル切り替え時に 1 秒弱使い終わった前チャンネルのチューナープロセスが残るので、シングルチューナー環境では
            ## それを解放し終わってからチューナーを起動できるようにする (実際はだいたい 0.25 秒程度で空きチューナーを確保できる)
            ## 複数チューナーがある場合は他の空きチューナーを使って起動できるため、待ち時間はほとんどかからない
            start_time = time.time()
            for _ in range(int(0.5 / 0.1)):

                # Mirakurun / mirakc からチューナーの状態を取得
                try:
                    response = await client.get(GetMirakurunAPIEndpointURL('/api/tuners'), timeout=5)
                    # レスポンスヘッダーの server が mirakc であれば mirakc と判定できる
                    if ('server' in response.headers) and ('mirakc' in response.headers['server']):
                        mirakurun_or_mirakc = 'mirakc'
                    tuners = response.json()
                except httpx.NetworkError:
                    logging.error(f'{self.live_stream.log_prefix} Failed to get tuner statuses from Mirakurun / mirakc. (Network Error)')
                    return False
                except httpx.TimeoutException:
                    logging.error(f'{self.live_stream.log_prefix} Failed to get tuner statuses from Mirakurun / mirakc. (Connection Timeout)')
                    return False

                # 指定されたチャンネルタイプが受信可能なチューナーが1つでも利用可能であれば True を返す
                available_tuners = [tuner for tuner in tuners if tuner['isAvailable'] is True]
                exact_type_tuners = [tuner for tuner in available_tuners if channel_type in tuner['types']]
                exact_free_tuners = [tuner for tuner in exact_type_tuners if tuner['isFree'] is True]
                if len(exact_free_tuners) > 0:
                    tuner = exact_free_tuners[0]
                    logging.info(f'{self.live_stream.log_prefix} Acquired a tuner from {mirakurun_or_mirakc}.')
                    logging.info(
                        f'{self.live_stream.log_prefix} Tuner: {tuner["name"]} / '
                        f'Type: {channel_type} / Acquired in {round(time.time() - start_time, 2)} seconds'
                    )
                    return True

                # 本来のチャンネルタイプのチューナーが登録されている場合、空きがなくてもフォールバック先は使わない
                ## Service Stream API は最終的に Mirakurun / mirakc 側でチューナーを選ぶため、
                ## ここでフォールバック先の空きチューナーを「確保できた」と扱うとログと実際の挙動がずれてしまう
                if len(exact_type_tuners) > 0:
                    await asyncio.sleep(0.1)
                    continue

                # 本来のチャンネルタイプのチューナーが登録されていない環境のみ、互換用のフォールバックタイプを確認する
                if fallback_channel_type != channel_type:
                    fallback_free_tuners = [
                        tuner for tuner in available_tuners
                        if tuner['isFree'] is True and fallback_channel_type in tuner['types']
                    ]
                    if len(fallback_free_tuners) > 0:
                        tuner = fallback_free_tuners[0]
                        logging.info(
                            f'{self.live_stream.log_prefix} Acquired a tuner from {mirakurun_or_mirakc}. '
                            f'({channel_type} tuner is not registered, using {fallback_channel_type})'
                        )
                        logging.info(
                            f'{self.live_stream.log_prefix} Tuner: {tuner["name"]} / '
                            f'Type: {fallback_channel_type} / Acquired in {round(time.time() - start_time, 2)} seconds'
                        )
                        return True

                await asyncio.sleep(0.1)

        # 空きチューナーは確保できなかったが、同じチャンネルが受信中であれば共聴することは可能なので warning に留める
        logging.warning(f'{self.live_stream.log_prefix} Failed to acquire a tuner from {mirakurun_or_mirakc}.')
        logging.warning(f'{self.live_stream.log_prefix} If the same channel is being received, it can be shared with the same tuner.')
        return False


    async def run(self) -> None:
        """
        エンコードタスクを実行し、終了理由にかかわらず所有リソースを回収する。

        Args:
            None

        Returns:
            None
        """

        # Restart ごとに別 Task を生成すると LiveStream が保持する参照から新しい Task が外れ、
        ## チューナー移譲時の cancel() が再起動後の処理へ届かなくなる。
        ## そのため LiveStream.connect() が登録した単一 Task の中で、各実行世代を直列に反復する。
        while True:

            # 実行世代ごとに後始末対象を作り直す
            ## finally ではこのローカル参照だけを回収し、次の実行世代の資源と混同しない
            cleanup_context = _LiveEncodingTaskCleanupContext()
            self._cleanup_context = cleanup_context
            is_abnormal_exit = False

            try:
                await self.__run()

            # チャンネル切り替えなどによるキャンセルでも、finally で回収してから呼び出し元へ伝播する
            except asyncio.CancelledError:
                is_abnormal_exit = True
                logging.debug(f'{self.live_stream.log_prefix} Encoding task was cancelled by channel switch.')
                if self.live_stream.getStatus().status != 'Offline':
                    self.live_stream.setStatus('Offline', 'エンコードタスクがキャンセルされました。')
                raise

            # 実装上の予期しない例外を隠さず、Standby 固着だけは防いでから呼び出し元へ伝播する
            except Exception:
                is_abnormal_exit = True
                if self.live_stream.getStatus().status != 'Offline':
                    self.live_stream.setStatus('Offline', 'エンコードタスクで予期しないエラーが発生しました。')
                raise

            finally:
                async def cleanup_generation() -> None:
                    # 個々の回収失敗を隔離して全資源の回収を試み、元の例外を上書きしない
                    """
                    指定世代の cleanup を完了する。

                    Args:
                        None

                    Returns:
                        None
                    """
                    try:
                        await self.__cleanupResources(cleanup_context)

                        # 旧processの回収を確認できない世代から新しいencoder世代を重ねない。
                        if (
                            len(cleanup_context.cleanup_failures) > 0
                            and self.live_stream.getStatus().status == 'Restart'
                        ):
                            self.live_stream.setStatus(
                                'Offline',
                                '前世代のエンコーダー資源を回収できなかったため再起動を中止しました。',
                            )

                        # 既存の通常終了経路では再起動・handoff の判定を __run() 側で行う
                        # 例外・キャンセル、および早期 Offline 終了ではここでチューナーを閉じ、Standby のまま残さない
                        if is_abnormal_exit is True or self.live_stream.getStatus().status == 'Offline':
                            await self.__cleanupTunerAfterAbnormalExit(cleanup_context)
                    finally:
                        # 内部 Restart で次世代 context に切り替わっても、残留 process を成功扱いしない。
                        self._cleanup_failures.update(cleanup_context.cleanup_failures)

                # task.cancel() が資源回収の途中へ到着しても cleanup 本体だけは最後まで実行し、
                # 完了後に元の CancelledError を呼び出し元へ伝播する。
                cleanup_task = asyncio.create_task(cleanup_generation())
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    await cleanup_task
                    raise

            # Restart の場合だけ同じ管理 Task 内で次の実行世代へ進む
            ## Offline・通常終了・チューナー移譲時は、LiveStream の done callback に参照解放を委ねる
            if len(self._cleanup_failures) > 0:
                if self.live_stream.getStatus().status != 'Offline':
                    self.live_stream.setStatus(
                        'Offline',
                        '前世代のエンコーダー資源を回収できなかったため再起動を中止しました。',
                    )
                return
            if self.live_stream.getStatus().status != 'Restart':
                return


    async def __cleanupResources(self, cleanup_context: _LiveEncodingTaskCleanupContext) -> None:
        """
        現在の run() が所有するストリーミング資源を best-effort で回収する。

        Args:
            cleanup_context (_LiveEncodingTaskCleanupContext): 回収対象資源と encoder 世代を保持する cleanup context。

        Returns:
            None
        """

        if cleanup_context.is_cleanup_completed is True:
            return

        # Queue 待機中のすべてのクライアントを先に終了させる
        try:
            self.live_stream.disconnectAll()
        except Exception:
            cleanup_context.cleanup_failures.add('live_stream_clients')
            logging.warning(f'{self.live_stream.log_prefix} Failed to disconnect live stream clients during cleanup.')

        # Mirakurun 側から新しい TS が届かないよう、response と session を明示的に閉じる
        if cleanup_context.response is not None:
            try:
                cleanup_context.response.close()
            except Exception:
                cleanup_context.cleanup_failures.add('mirakurun_response')
                logging.warning(f'{self.live_stream.log_prefix} Failed to close Mirakurun response during cleanup.')
            cleanup_context.response = None

        if cleanup_context.session is not None:
            try:
                if cleanup_context.session.closed is False:
                    await cleanup_context.session.close()
            except Exception:
                cleanup_context.cleanup_failures.add('mirakurun_session')
                logging.warning(f'{self.live_stream.log_prefix} Failed to close Mirakurun session during cleanup.')
            cleanup_context.session = None

        # EDCB のストリーム接続はチューナープロセスを再利用する場合にも明示的に閉じる
        if self.live_stream.tuner is not None:
            try:
                await self.live_stream.tuner.disconnect(self.live_stream.live_stream_id)
            except Exception:
                cleanup_context.cleanup_failures.add('edcb_tuner_stream')
                logging.warning(f'{self.live_stream.log_prefix} Failed to disconnect EDCB tuner during cleanup.')

        # 子プロセスへ渡し終えた親側 FD は、再利用された別 FD を閉じないよう回収後ただちに None 化する
        for pipe in [
            cleanup_context.tsreadex_read_pipe,
            cleanup_context.tsreadex_write_pipe,
            cleanup_context.bridge_read_pipe,
            cleanup_context.bridge_write_pipe,
        ]:
            if pipe is None:
                continue
            try:
                os.close(pipe)
            except OSError:
                pass
        cleanup_context.tsreadex_read_pipe = None
        cleanup_context.tsreadex_write_pipe = None
        cleanup_context.bridge_read_pipe = None
        cleanup_context.bridge_write_pipe = None

        # Reader の drain() を止めてから子プロセスを終了させる
        if cleanup_context.tsreadex is not None and cleanup_context.tsreadex.stdin is not None:
            try:
                cleanup_context.tsreadex.stdin.close()
            except Exception:
                logging.warning(f'{self.live_stream.log_prefix} Failed to close tsreadex stdin during cleanup.')

        # ループ中の Reader / Writer / SubWriter / EncoderObServer と PSI/SI push をすべて終了待ちする
        background_tasks = tuple(cleanup_context.background_tasks)
        cleanup_context.background_tasks.clear()
        for background_task in background_tasks:
            if background_task.done() is False:
                background_task.cancel()
        if len(background_tasks) > 0:
            try:
                await asyncio.gather(*background_tasks, return_exceptions=True)
            except Exception:
                logging.warning(f'{self.live_stream.log_prefix} Failed to await background tasks during cleanup.')

        # 共有 source の購読だけを外す。producer/tsreadex/PSI archiver は最後の subscriber まで source が所有する。
        if cleanup_context.source_subscription is not None:
            source_subscription = cleanup_context.source_subscription
            cleanup_context.source_subscription = None
            try:
                await source_subscription.close(
                    keep_source_alive = self.live_stream.getStatus().status == 'Restart',
                )
            except Exception:
                cleanup_context.cleanup_failures.add('live_source_subscription')
                logging.warning(f'{self.live_stream.log_prefix} Failed to close shared live source subscription.')
            if self.live_stream.getSourceSubscription() is source_subscription:
                self.live_stream.setSourceSubscription(None)

        # 子プロセスを個別に kill / wait することで、片方の失敗がもう片方の回収を妨げないようにする
        for process_name, process in [
            ('tsreadex', cleanup_context.tsreadex),
            ('encoder', cleanup_context.encoder),
            ('bridge', cleanup_context.bridge),
        ]:
            if process is None:
                continue
            try:
                if process.returncode is None:
                    process.kill()
            except (ProcessLookupError, OSError):
                pass
            except Exception:
                logging.warning(f'{self.live_stream.log_prefix} Failed to terminate {process_name} during cleanup.')
            try:
                if process.returncode is None:
                    async with asyncio.timeout(5):
                        await process.wait()
            except TimeoutError:
                cleanup_context.cleanup_failures.add(f'process:{process_name}')
                logging.warning(f'{self.live_stream.log_prefix} Timed out waiting for {process_name} during cleanup.')
            except Exception:
                cleanup_context.cleanup_failures.add(f'process:{process_name}')
                logging.warning(f'{self.live_stream.log_prefix} Failed to wait for {process_name} during cleanup.')
            if process.returncode is None:
                cleanup_context.cleanup_failures.add(f'process:{process_name}')
        cleanup_context.tsreadex = None
        cleanup_context.encoder = None
        cleanup_context.bridge = None

        # この run() が作ったアーカイバーだけを破棄し、次の run() が作った共有参照は消さない
        if cleanup_context.psi_data_archiver is not None:
            try:
                await cleanup_context.psi_data_archiver.destroy()
            except Exception:
                cleanup_context.cleanup_failures.add('psi_data_archiver')
                logging.warning(f'{self.live_stream.log_prefix} Failed to destroy PSI/SI archiver during cleanup.')
            if self.live_stream.psi_data_archiver is cleanup_context.psi_data_archiver:
                self.live_stream.psi_data_archiver = None
            cleanup_context.psi_data_archiver = None

        # cleanup 自体が CancelledError で中断された場合は外側 finally から再試行できるよう、
        # 全資源の回収が完了した時点でのみ完了扱いにする。
        cleanup_context.is_cleanup_completed = True


    async def __cleanupTunerAfterAbnormalExit(
        self,
        cleanup_context: _LiveEncodingTaskCleanupContext,
    ) -> None:
        """
        例外・キャンセルで通常の終了処理を迂回した EDCB チューナーを閉じる。

        Args:
            cleanup_context (_LiveEncodingTaskCleanupContext): cleanup failure の記録先。

        Returns:
            None
        """

        tuner = self.live_stream.tuner
        if tuner is None or tuner.getState() == 'Cancelling':
            return

        try:
            closed = await tuner.close(self.live_stream.live_stream_id)
            if closed is True and self.live_stream.tuner is tuner:
                self.live_stream.tuner = None
            elif closed is False:
                cleanup_context.cleanup_failures.add('edcb_tuner')
        except Exception:
            cleanup_context.cleanup_failures.add('edcb_tuner')
            logging.warning(f'{self.live_stream.log_prefix} Failed to close EDCB tuner after abnormal task exit.')


    async def __run(self) -> None:
        """
        エンコードタスクを実行する

        Args:
            None

        Returns:
            None
        """

        # run() のラッパーが初期化した、この実行だけの後始末対象を取得する
        cleanup_context = self._cleanup_context
        assert cleanup_context is not None

        # connect() 直後、実タスクが最初に schedule される前に Prepare が Abort / 失敗した場合は、
        # cleanup 側で確定した Offline を Standby へ戻して B を復活させない。
        if self.live_stream.getStatus().status == 'Offline':
            return

        CONFIG = Config()

        # メタデータの取得元とは独立した、ライブ放送波の実際の受信元を取得する
        LIVE_STREAM_BACKEND = CONFIG.general.live_stream_backend

        # まだ Standby になっていなければ、ステータスを Standby に設定
        # 基本はエンコードタスクの呼び出し元である self.live_stream.connect() の方で Standby に設定されるが、再起動の場合はそこを経由しないため必要
        if not (self.live_stream.getStatus().status == 'Standby' and self.live_stream.getStatus().detail == 'エンコードタスクを起動しています…'):
            self.live_stream.setStatus('Standby', 'エンコードタスクを起動しています…')

        # チャンネル情報からサービス ID とネットワーク ID を取得する
        channel = cast(Channel, await Channel.filter(display_channel_id=self.live_stream.display_channel_id).first())

        # 実際のライブ backend を実行前能力行列と後段のエンコーダー構築で共有する。
        ## ラジオチャンネルでは映像 HW API を使わないため、従来どおり FFmpeg に固定する。
        ENCODER_TYPE = (
            'FFmpeg'
            if channel.is_radiochannel is True
            else GetEncoderForLiveChannel(self.live_stream.display_channel_id)
        )

        # Anchor v1 は映像 access unit と対応付けるため、映像付き通常 API だけ共有 source を利用する。
        # ラジオとCompatibility APIは従来の個別tsreadex/Bridge条件を維持する。
        shared_source_enabled = (
            self.live_stream.stream_anchor_enabled is True and
            channel.is_radiochannel is False
        )

        # 高度codecの能力検査と、実際にBridgeを置く条件を分離する。
        codec_bridge_required = self.isTSCodecBridgeRequired(is_radiochannel=channel.is_radiochannel)
        bridge_required = codec_bridge_required or shared_source_enabled
        if codec_bridge_required is True:
            if channel.is_radiochannel is True:
                # ラジオは映像 ES を生成しないため、映像付き exact combination ではなく audio-only 実 probe を再検査する。
                audio_capability = await KonomiTVBS4KPlaybackCapabilityProbe.getAudioCapability(
                    self.getRequestedAudioCodec()
                )
                if audio_capability is None or audio_capability.live_available is False:
                    reason_code = (
                        audio_capability.live_reason_code
                        if audio_capability is not None and audio_capability.live_reason_code is not None
                        else 'ProbeFailed'
                    )
                    self.live_stream.setStatus(
                        'Offline',
                        'TS Codec Bridge のラジオ音声能力を利用できません。'
                        f' ({reason_code})',
                    )
                    raise RuntimeError(
                        'The requested KonomiTV-BS4K radio audio encoding is unavailable. '
                        f'Reason: {reason_code}'
                    )
            else:
                live_combination = await KonomiTVBS4KPlaybackCapabilityProbe.getLiveCombinationCapability(
                    ENCODER_TYPE,
                    self.getRequestedVideoCodec(),
                    self.getRequestedVideoBitDepth(),
                    self.getRequestedAudioCodec(),
                )
                if live_combination is None or live_combination.available is False:
                    reason_code = (
                        live_combination.reason_code
                        if live_combination is not None and live_combination.reason_code is not None
                        else 'UnsupportedCombination'
                    )
                    self.live_stream.setStatus(
                        'Offline',
                        'TS Codec Bridge の映像・音声コーデック組み合わせを利用できません。'
                        f' ({reason_code})',
                    )
                    raise RuntimeError(
                        'The requested KonomiTV-BS4K live video and audio encoding combination is unavailable. '
                        f'Reason: {reason_code}'
                    )

        # 3つのバックエンド構成のどれで動作しているかと、実際に選局するサービスを明示する
        ## 接続 URL は認証情報やローカル環境情報を含む可能性があるためログへ出力しない。
        logging.info(
            f'{self.live_stream.log_prefix} Backend: Metadata={CONFIG.general.backend} / Live={LIVE_STREAM_BACKEND}'
        )
        logging.info(
            f'{self.live_stream.log_prefix} Source: {LIVE_STREAM_BACKEND} / NID: {channel.network_id} / '
            f'TSID: {channel.transport_stream_id} / SID: {channel.service_id}'
        )

        # 現在の番組情報を取得する
        program_present = (await channel.getCurrentAndNextProgram())[0]
        if program_present is not None:
            logging.info(f'{self.live_stream.log_prefix} Title: {program_present.title}')
        else:
            logging.info(f'{self.live_stream.log_prefix} Title: 番組情報がありません')

        tsreadex: asyncio.subprocess.Process | None = None
        tsreadex_read_pipe: int | None = None

        # Compatibility/radio は従来どおり品質別の PSI archiver と tsreadex を所有する。
        # 通常の映像付きライブでは LiveSourceCoordinator が両方を source epoch 単位で共有する。
        if shared_source_enabled is False:
            psi_data_archiver = LivePSIDataArchiver(channel.service_id)
            self.live_stream.psi_data_archiver = psi_data_archiver
            cleanup_context.psi_data_archiver = psi_data_archiver

            tsreadex_options = [
                '-x', '18/38/39',
                '-n', f'{channel.service_id}' if CONFIG.tv.debug_mode_ts_path is None else '-1',
                '-A', '1',
                '-c', '5',
                '-u', '1',
                '-d', '9',
            ]
            if CONFIG.tv.debug_mode_ts_path is None:
                tsreadex_options.append('-')
            else:
                tsreadex_options += ['-l', '2350', CONFIG.tv.debug_mode_ts_path]

            tsreadex_read_pipe, tsreadex_write_pipe = os.pipe()
            cleanup_context.tsreadex_read_pipe = tsreadex_read_pipe
            cleanup_context.tsreadex_write_pipe = tsreadex_write_pipe
            try:
                tsreadex = await asyncio.subprocess.create_subprocess_exec(
                    *[LIBRARY_PATH['tsreadex'], *tsreadex_options],
                    stdin = asyncio.subprocess.PIPE,
                    stdout = tsreadex_write_pipe,
                    stderr = asyncio.subprocess.DEVNULL,
                )
                cleanup_context.tsreadex = tsreadex
            except BaseException:
                try:
                    os.close(tsreadex_read_pipe)
                except OSError:
                    pass
                cleanup_context.tsreadex_read_pipe = None
                raise
            finally:
                try:
                    os.close(tsreadex_write_pipe)
                finally:
                    cleanup_context.tsreadex_write_pipe = None

        # ***** エンコーダープロセスの作成と実行 *****

        # エンコーダーの起動には時間がかかるので、先にエンコーダーを起動しておいた後、あとからチューナーを起動する
        # チューナーの起動後にエンコーダー (正確には tsreadex) に受信した放送波が書き込まれる
        # チューナーの起動にも時間がかかるが、エンコーダーの起動は非同期なのに対し、チューナーの起動は EDCB の場合は同期的

        # フル HD 放送が行われているチャンネルかを取得
        is_fullhd_channel = (
            channel.is_oneseg is False and
            self.isFullHDChannel(channel.network_id, channel.service_id)
        )

        # 公開設定名を FFmpeg 8 の各ライブ実行バックエンドへ対応付ける
        # ラジオは映像エンコーダーを使わないため、従来通りソフトウェア FFmpeg の経路として扱う
        ffmpeg8_encoder_type = ENCODER_TYPE
        ffmpeg8_log_label = {
            'FFmpeg': 'FFmpeg8/SW',
            'QSV': 'FFmpeg8/QSV',
            'NVENC': 'FFmpeg8/NVENC',
            'AMF': 'FFmpeg8/AMF',
        }[ffmpeg8_encoder_type]
        if channel.is_radiochannel is True:
            encoder_options = self.buildFFmpeg8RadioOptions()
        else:
            encoder_options = self.buildFFmpeg8Options(
                self.live_stream.quality,
                ffmpeg8_encoder_type,
                channel.type,
                is_fullhd_channel,
                channel.is_oneseg,
            )

        # AMD AMF だけは Mesa VAAPI と AMD AMF runtime を同時利用するラッパーを使う
        # QSV では同梱 libva/iHD driver を選択し、NVENC とソフトウェアでは通常の FFmpeg 8 を直接起動する
        encoder_executable = RecordedPlaybackBackend.getExecutable(ffmpeg8_encoder_type)
        encoder_environment = RecordedPlaybackBackend.getEnvironment(ffmpeg8_encoder_type)
        logging.info(
            f'{self.live_stream.log_prefix} {ffmpeg8_log_label} Commands:\n'
            f'{encoder_executable} {" ".join(encoder_options)}'
        )

        # 通常映像は最終 Anchor 確定のため全 codec を Bridge へ通し、
        # Compatibility / radio は高度 codec が必要な場合だけ従来どおり Bridge を置く。
        # Python で TS を往復させず OS pipe で直結する。
        bridge: asyncio.subprocess.Process | None = None
        encoder_stdout: int = asyncio.subprocess.PIPE
        if bridge_required is True:
            bridge_read_pipe, bridge_write_pipe = os.pipe()
            cleanup_context.bridge_read_pipe = bridge_read_pipe
            cleanup_context.bridge_write_pipe = bridge_write_pipe
            bridge_video_codec = self.getRequestedVideoCodec()
            bridge_options = [
                '--video-codec',
                (
                    bridge_video_codec
                    if channel.is_radiochannel is False and bridge_video_codec in ('vp9', 'av1')
                    else 'passthrough'
                ),
                '--audio-codec',
                self.getRequestedAudioCodec(),
            ]
            if shared_source_enabled is True:
                bridge_options.append('--stream-anchor-v1')
            if channel.is_radiochannel is False and bridge_video_codec == 'av1':
                try:
                    encoder_muxrate = encoder_options[encoder_options.index('-muxrate') + 1]
                    transport_rate_kbps = ParseKonomiTVBS4KAdvancedLiveMuxrateKbps(encoder_muxrate)
                except (ValueError, IndexError) as ex:
                    raise RuntimeError('AV1 live encoder does not define a fixed transport muxrate.') from ex
                bridge_options += [
                    '--transport-rate-kbps',
                    transport_rate_kbps,
                ]
            bridge_executable = LIBRARY_PATH[self.TS_CODEC_BRIDGE_LIBRARY_PATH_KEY]
            logging.info(
                f'{self.live_stream.log_prefix} TS Codec Bridge Commands:\n'
                f'{bridge_executable} {" ".join(bridge_options)}'
            )
            try:
                try:
                    bridge = await asyncio.subprocess.create_subprocess_exec(
                        bridge_executable,
                        *bridge_options,
                        stdin = bridge_read_pipe,
                        stdout = asyncio.subprocess.PIPE,
                        stderr = asyncio.subprocess.PIPE,
                    )
                    TSCodecBridgeRuntimeVerifier.recordProcessStart('live')
                except OSError as ex:
                    # 起動競合・実体差し替えなど能力検査後の race も、一般的な
                    ## エンコード障害と混同せずクライアントの一回限り互換 fallback へ伝える。
                    logging.error(
                        f'{self.live_stream.log_prefix} Failed to start TS Codec Bridge: '
                        f'{type(ex).__name__}: {ex}'
                    )
                    self.live_stream.setStatus(
                        'Offline',
                        'TS Codec Bridge の起動に失敗しました。(E-07B)',
                    )
                    raise
                cleanup_context.bridge = bridge
                encoder_stdout = bridge_write_pipe
            finally:
                # read 側は Bridge へ渡したため、親プロセスでは保持しない。
                try:
                    os.close(bridge_read_pipe)
                finally:
                    cleanup_context.bridge_read_pipe = None

        # エンコーダープロセスを非同期で作成・実行
        encoder_stdin: int = (
            asyncio.subprocess.PIPE
            if shared_source_enabled is True
            else cast(int, tsreadex_read_pipe)
        )
        self.live_stream.getTelemetry().encoderStarted()
        try:
            encoder = await asyncio.subprocess.create_subprocess_exec(
                *[encoder_executable, *encoder_options],
                stdin = encoder_stdin,  # 共有sourceまたは従来tsreadexからの入力
                stdout = encoder_stdout,  # 高度 codec では Bridge へ、従来 codec では Writer へ出力
                stderr = asyncio.subprocess.PIPE,  # ログ出力
                env = encoder_environment,
            )
            cleanup_context.encoder = encoder
        except BaseException:
            # tsreadex の起動後にエンコーダーの起動に失敗した場合、
            ## このままでは親プロセスが例外で脱出して tsreadex だけ残留するため、ここで回収する
            if tsreadex is not None:
                try:
                    tsreadex.kill()
                except Exception:
                    pass
            raise
        finally:
            # Bridge へ渡した write 側も、エンコーダー起動後は親プロセスで保持しない。
            if cleanup_context.bridge_write_pipe is not None:
                try:
                    os.close(cleanup_context.bridge_write_pipe)
                finally:
                    cleanup_context.bridge_write_pipe = None
            # tsreadex の読み込み用パイプは子プロセスに渡したので、親プロセス側ではクローズする
            if tsreadex_read_pipe is not None:
                try:
                    os.close(tsreadex_read_pipe)
                finally:
                    cleanup_context.tsreadex_read_pipe = None

        # ***** チューナーの起動と接続 *****

        # エンコードタスクが稼働中かどうか
        is_running: bool = True

        # 高度 codec では Bridge の標準出力を、従来 codec ではエンコーダーの標準出力を配信元にする。
        stream_output_process = bridge if bridge is not None else encoder
        assert stream_output_process.stdout is not None

        def IsPipelineTerminated() -> bool:
            """
            tsreadex・エンコーダー・Bridge のいずれかが終了済みか返す。

            Args:
                None

            Returns:
                bool: 判定結果。
            """

            return (
                (tsreadex is not None and tsreadex.returncode is not None) or
                encoder.returncode is not None or
                (bridge is not None and bridge.returncode is not None)
            )

        # 放送波の MPEG2-TS を受信する StreamReader
        stream_reader: asyncio.StreamReader | PipeStreamReader | aiohttp.StreamReader | None = None

        # Mirakurun の aiohttp セッション (EDCB バックエンド利用時は常に None)
        response: aiohttp.ClientResponse | None = None
        session: aiohttp.ClientSession | None = None

        # 実行中の非同期実行タスクへの参照を保持しておく
        ## run() の実行が完了するまで、ガベージコレクタにより非同期実行タスクが勝手に破棄されることを防ぐ
        ## ref: https://docs.astral.sh/ruff/rules/asyncio-dangling-task/
        background_tasks = cleanup_context.background_tasks

        # 通常の映像付きライブだけ、source epoch共有の tuner/Mirakurun入力 + tsreadex を購読する。
        source_subscription: LiveSourceSubscription | None = None
        if shared_source_enabled is True:
            source_descriptor = LiveSourceDescriptor(
                key = LiveSourceKey(
                    backend = LIVE_STREAM_BACKEND,
                    network_id = channel.network_id,
                    transport_stream_id = cast(int, channel.transport_stream_id),
                    service_id = channel.service_id,
                ),
                channel_type = channel.type,
                debug_mode_ts_path = CONFIG.tv.debug_mode_ts_path,
                startup_discard_seconds = (
                    CONFIG.general.bs4k_live_startup_discard_seconds
                    if (
                        channel.type == 'BS4K' and
                        CONFIG.general.bs4k_live_startup_discard_enabled is True
                    )
                    else 0.0
                ),
            )
            source_role: Literal['Active', 'Preparing'] = (
                'Preparing'
                if self.live_stream.getTelemetry().snapshot().prepare_state == 'Consumed'
                else 'Active'
            )
            try:
                source_subscription = await LIVE_SOURCE_COORDINATOR.acquire(
                    source_descriptor,
                    role = source_role,
                    is_in_use = self.live_stream.hasSourceViewers,
                )
            except LiveSourceError as ex:
                self.live_stream.setStatus(
                    'Offline',
                    f'共有ライブ入力を開始できませんでした。 ({ex})',
                )
                return
            cleanup_context.source_subscription = source_subscription
            self.live_stream.setSourceSubscription(source_subscription)
            self.live_stream.psi_data_archiver = source_subscription.psi_data_archiver

        # チューナー起動フェーズから Controller 実行までを CancelledError から保護する
        # チャンネル切り替え時に LiveStream.connect() からこのタスクがキャンセルされると、チューナー起動フェーズで
        # await している箇所 (EDCBTuner.setChannel() / EDCBTuner.connect() など) で CancelledError が発生する可能性がある
        # CancelledError をキャッチしないとエンコーダープロセスの終了処理に到達せず、プロセスがリークしてしまう
        try:
            # Mirakurun バックエンド
            if shared_source_enabled is True:
                # backend接続は共有source producerが所有する
                pass

            elif LIVE_STREAM_BACKEND == 'Mirakurun':

                # チューナーを確保できるまで待機する
                ## 確保できなかった場合でも共聴で受信できる可能性があるので、戻り値は無視する
                self.live_stream.setStatus('Standby', 'チューナーを確保しています…')
                await self.acquireMirakurunTuner(channel.type)

                # Mirakurun 形式のサービス ID
                # NID と SID を 5 桁でゼロ埋めした上で int に変換する
                mirakurun_service_id = int(str(channel.network_id).zfill(5) + str(channel.service_id).zfill(5))

                # Mirakurun の Service Stream API へ HTTP リクエストを開始
                self.live_stream.setStatus('Standby', 'チューナーを起動しています…')
                session = aiohttp.ClientSession()
                cleanup_context.session = session
                mirakurun_stream_timeout = 40 if channel.type == 'BS4K' else 15
                try:
                    response = await session.get(
                        url = GetMirakurunAPIEndpointURL(f'/api/services/{mirakurun_service_id}/stream'),
                        headers = {**API_REQUEST_HEADERS, 'X-Mirakurun-Priority': '0'},
                        timeout = aiohttp.ClientTimeout(
                            connect=mirakurun_stream_timeout,
                            sock_connect=mirakurun_stream_timeout,
                            sock_read=mirakurun_stream_timeout,
                        )
                    )
                    cleanup_context.response = response
                except (TimeoutError, aiohttp.ClientError):

                    # 番組名に「放送休止」などが入っていれば停波によるものとみなし、そうでないならチューナーへの接続に失敗したものとする
                    if program_present is None or program_present.isOffTheAirProgram():
                        self.live_stream.setStatus('Offline', 'この時間は放送を休止しています。(E-01M)')
                    else:
                        self.live_stream.setStatus('Offline', 'チューナーへの接続に失敗しました。チューナー側に何らかの問題があるかもしれません。(E-01M)')

                    # run() の finally で session・子プロセス・クライアント・アーカイバーをまとめて回収する
                    return

                # 放送波の MPEG2-TS の受信元の StreamReader として設定
                stream_reader = response.content

            # EDCB バックエンド
            elif LIVE_STREAM_BACKEND == 'EDCB':

                # チューナーインスタンスを取得する
                ## Idling への切り替え、ONAir への復帰時に LiveStream 側でチューナーのアンロック/ロックが行われる
                if self.live_stream.tuner is None:
                    self.live_stream.tuner = EDCBTuner.getOrCreate(self.live_stream.live_stream_id)

                # チューナーを起動する
                logging.debug(f'{self.live_stream.log_prefix} EDCB NetworkTV ID: {self.live_stream.tuner.getEDCBNetworkTVID()}')
                self.live_stream.setStatus('Standby', 'チューナーを起動しています…')
                is_tuner_opened = await self.live_stream.tuner.setChannel(
                    channel.network_id,
                    channel.service_id,
                    cast(int, channel.transport_stream_id),
                    self.live_stream.live_stream_id,
                )

                # チューナーの起動に失敗した
                # ほとんどがチューナー不足によるものなので、ステータス詳細でもそのように表示する
                # 成功時は tuner.close() するか予約などに割り込まれるまで起動しつづけるので注意
                if is_tuner_opened is False:
                    self.live_stream.setStatus('Offline', 'チューナーの起動に失敗しました。空きチューナーが不足していると考えられます。(E-02E)')

                    # run() の finally でチューナーを含む資源をまとめて回収する
                    return

                # チューナーをロックする
                # ロックしないと途中でチューナーの制御を横取りされてしまう
                self.live_stream.tuner.lock(self.live_stream.live_stream_id)

                # チューナーに接続する
                # 放送波が送信される TCP ソケットまたは名前付きパイプを取得する
                self.live_stream.setStatus('Standby', 'チューナーに接続しています…')
                reader = await self.live_stream.tuner.connect(self.live_stream.live_stream_id)

                # チューナーへの接続に失敗した
                if reader is None:
                    self.live_stream.setStatus('Offline', 'チューナーへの接続に失敗しました。チューナー側に何らかの問題があるかもしれません。(E-03E)')

                    # run() の finally でチューナーを含む資源をまとめて回収する
                    return

                # 放送波の MPEG2-TS の受信元の StreamReader として設定
                stream_reader = reader

            # ***** チューナーからの出力の読み込み → tsreadex・エンコーダーへの書き込み *****

            # チューナーからの放送波 TS の最終読み取り時刻 (単調増加時間)
            ## 単に時刻を比較する用途でしか使わないので、time.monotonic() から取得した単調増加時間が入る
            ## Unix Time とかではないので注意
            tuner_ts_read_at: float = time.monotonic()
            tuner_ts_read_at_lock = asyncio.Lock()
            bs4k_startup_discard_seconds = (
                CONFIG.general.bs4k_live_startup_discard_seconds
                if channel.type == 'BS4K' and CONFIG.general.bs4k_live_startup_discard_enabled is True
                else 0.0
            )

            async def SharedSourceReader() -> None:
                """
                共有 tsreadex 出力をこの品質 encoder の stdin へ送る。

                Args:
                    None

                Returns:
                    None
                """

                nonlocal tuner_ts_read_at, is_running
                assert source_subscription is not None
                assert encoder.stdin is not None
                try:
                    while True:
                        chunk = await source_subscription.read()
                        if chunk == b'':
                            raise LiveSourceError('live_source_ended')
                        async with tuner_ts_read_at_lock:
                            tuner_ts_read_at = source_subscription.last_input_at

                        updated_geometry = source_subscription.source_geometry
                        if updated_geometry is not None and updated_geometry != self._live_source_geometry:
                            previous_geometry = self._live_source_geometry
                            self._live_source_geometry = updated_geometry
                            if (
                                previous_geometry is not None and
                                (
                                    previous_geometry.is_approximately_16_9
                                    != updated_geometry.is_approximately_16_9
                                )
                            ):
                                self.live_stream.setStatus(
                                    'Restart',
                                    '放送波の表示アスペクトが変わったため、エンコード経路を切り替えます…',
                                )
                                is_running = False
                                break

                        try:
                            encoder.stdin.write(chunk)
                            await encoder.stdin.drain()
                        except RuntimeError as ex:
                            # Controller や cleanup が encoder を先に停止すると、同時実行中の
                            # StreamWriter.write() / drain() は RuntimeError を送出することがある。
                            # 終了順序の競合だけを既存の入力停止経路へ合流させ、subscription
                            # やアスペクト監視由来の RuntimeError までは握り潰さない。
                            raise LiveSourceError(f'encoder_input_closed:{ex}') from ex
                        if is_running is False or IsPipelineTerminated() is True:
                            break
                except LiveSourcePreemptedError:
                    # viewer 0 の旧 source は新しい channel 用に明示回収された。
                    # Restart にすると同じ旧 source を再取得して新規 channel と競合するため、必ず終了する。
                    if self.live_stream.getStatus().status != 'Offline':
                        self.live_stream.setStatus(
                            'Offline',
                            '新しいライブストリームのため、未視聴の共有入力を終了しました。',
                        )
                    is_running = False
                except (
                    LiveSourceSubscriberError,
                    LiveSourceError,
                    BrokenPipeError,
                    ConnectionError,
                ) as ex:
                    if self.live_stream.getStatus().status not in ('Offline', 'Restart'):
                        if source_subscription.role == 'Preparing':
                            self.live_stream.setStatus(
                                'Offline',
                                f'Prepare 中の共有ライブ入力が停止しました。 ({ex})',
                            )
                        else:
                            self.live_stream.setStatus(
                                'Restart',
                                f'共有ライブ入力が停止したため再起動します。 ({ex})',
                            )
                    is_running = False
                finally:
                    try:
                        encoder.stdin.close()
                    except Exception:
                        pass

            async def Reader() -> None:
                """
                入力ストリームを処理する。

                Args:
                    None

                Returns:
                    None
                """
                nonlocal tuner_ts_read_at, is_running
                assert tsreadex is not None

                # 受信した放送波が入るイテレータを作成
                # R/W バッファ: 188B (TS Packet Size) * 256 = 48128B
                async def GetIterator(
                        stream_reader: asyncio.StreamReader | PipeStreamReader | aiohttp.StreamReader,
                        chunk_size: int = ts.PACKET_SIZE * 256,
                    ) -> AsyncIterator[bytes]:
                    while True:
                        try:
                            yield await stream_reader.readexactly(chunk_size)
                        except asyncio.IncompleteReadError as ex:
                            # もし残りのバイトがあれば、 break 前にそれらを yield する
                            if ex.partial:
                                yield ex.partial
                            break

                assert stream_reader is not None
                stream_iterator = GetIterator(stream_reader)
                startup_discard_until = (
                    time.monotonic() + bs4k_startup_discard_seconds
                    if bs4k_startup_discard_seconds > 0
                    else 0.0
                )
                startup_discard_finished_logged = startup_discard_until == 0.0
                if startup_discard_until > 0:
                    logging.info(
                        f'{self.live_stream.log_prefix} BS4K startup TS discard started. '
                        f'Discarding tuner TS for {bs4k_startup_discard_seconds:.1f} seconds.'
                    )

                # EDCB / Mirakurun から受信した放送波を随時 tsreadex の入力に書き込む
                try:
                    async for chunk in stream_iterator:

                        # チューナーからの放送波 TS の最終読み取り時刻を更新
                        async with tuner_ts_read_at_lock:
                            tuner_ts_read_at = time.monotonic()

                        # tsreadex の標準入力が閉じられていたら、タスクを終了
                        if cast(asyncio.StreamWriter, tsreadex.stdin).is_closing():
                            break

                        try:
                            # 生の放送波の TS パケットを PSI/SI データアーカイバーに送信する
                            ## 放送波の tsreadex への書き込みを最優先で行うため、非同期タスクとして実行する
                            ## ここで tsreadex への書き込みがブロックされると放送波の受信ループが止まり、ライブストリームの異常終了に繋がりかねない
                            if self.live_stream.psi_data_archiver is not None:
                                # chunk ごとの短命 Task は完了後に set から外し、完了済み参照を溜めない
                                self.trackBackgroundTask(
                                    asyncio.create_task(self.live_stream.psi_data_archiver.pushTSPacketData(chunk)),
                                    background_tasks,
                                )

                            # 薄い SAR 監視: フルデコードせず sequence header だけ見る
                            updated_geometry = self._live_source_aspect_monitor.push(chunk)
                            if updated_geometry is not None:
                                previous_geometry = self._live_source_geometry
                                self._live_source_geometry = updated_geometry
                                logging.info(
                                    f'{self.live_stream.log_prefix} Live source geometry: '
                                    f'{updated_geometry.coded_width}x{updated_geometry.coded_height} '
                                    f'DAR {updated_geometry.dar_width}:{updated_geometry.dar_height} '
                                    f'(via {updated_geometry.source})'
                                )
                                # GPU/SW 経路の切り替えが必要ならエンコードを組み直す
                                if (
                                    previous_geometry is not None and
                                    (
                                        previous_geometry.is_approximately_16_9
                                        != updated_geometry.is_approximately_16_9
                                    )
                                ):
                                    self.live_stream.setStatus(
                                        'Restart',
                                        '放送波の表示アスペクトが変わったため、'
                                        'エンコード経路を切り替えます…',
                                    )
                                    is_running = False
                                    break

                            # BS4K ライブ開始直後の不安定な TS はエンコーダーへ渡さず破棄する
                            if startup_discard_until > 0 and time.monotonic() < startup_discard_until:
                                if is_running is False or IsPipelineTerminated() is True:
                                    break
                                continue
                            if startup_discard_finished_logged is False:
                                logging.info(f'{self.live_stream.log_prefix} BS4K startup TS discard finished.')
                                startup_discard_finished_logged = True

                            # ストリームデータを tsreadex の標準入力に書き込む
                            cast(asyncio.StreamWriter, tsreadex.stdin).write(chunk)
                            await cast(asyncio.StreamWriter, tsreadex.stdin).drain()

                        # 並列タスク処理中に何らかの例外が発生した
                        # BrokenPipeError・asyncio.TimeoutError などが想定されるが、何が発生するかわからないためすべての例外をキャッチする
                        except Exception:
                            break

                        # エンコードタスクが終了しているか既にエンコーダープロセスが終了していたら、タスクを終了
                        if is_running is False or IsPipelineTerminated() is True:
                            break

                except OSError:
                    pass

                # タスクを終える前に、チューナーとの接続を明示的に閉じる
                try:
                    cast(asyncio.StreamWriter, tsreadex.stdin).close()
                except OSError:
                    pass

                ## 並行している別の非同期タスクとのタイミングの関係で 0.1 秒待ってからクリーンアップする
                await asyncio.sleep(0.1)

                # EDCB バックエンド: チューナーとのストリーミング接続を閉じる
                ## チャンネル切り替え時に再利用するため、ここではチューナー自体は閉じない
                if LIVE_STREAM_BACKEND == 'EDCB' and self.live_stream.tuner is not None:
                    await self.live_stream.tuner.disconnect(self.live_stream.live_stream_id)

                # Mirakurun バックエンド: Service Stream API とのストリーミング接続を閉じる
                if LIVE_STREAM_BACKEND == 'Mirakurun' and response is not None and session is not None:
                    await session.close()
                    response.close()

            # タスクを非同期で実行
            if shared_source_enabled is True:
                self.trackBackgroundTask(asyncio.create_task(SharedSourceReader()), background_tasks)
            else:
                self.trackBackgroundTask(asyncio.create_task(Reader()), background_tasks)

            # ***** tsreadex・エンコーダーからの出力の読み込み → ライブストリームへの書き込み *****

            # エンコーダーの出力のチャンクが積み増されていくバッファ
            chunk_buffer: bytearray = bytearray()

            # チャンクの最終書き込み時刻 (単調増加時間)
            ## 単に時刻を比較する用途でしか使わないので、time.monotonic() から取得した単調増加時間が入る
            ## Unix Time とかではないので注意
            chunk_written_at: float = 0
            pipeline_output_started_at: float | None = None

            # Writer の排他ロック
            ## タスク間共有の変数を Writer() タスクと SubWriter() タスクの両方から読み書きするため、
            ## chunk_buffer / chunk_written_at にアクセスする際は排他ロックを掛けておく必要がある
            ## そうしないと稀にパケロスするらしく、ブラウザ側で突如再生できなくなることがある
            writer_lock = asyncio.Lock()

            async def Writer() -> None:

                nonlocal chunk_buffer, chunk_written_at, pipeline_output_started_at, writer_lock

                while True:
                    try:

                        # エンコーダーからの出力を読み取る
                        ## TS パケットのサイズが 188 bytes なので、1回の readexactly() で 188 bytes ずつ読み取る
                        ## read() ではなく厳密な readexactly() を使わないとぴったり 188 bytes にならない場合がある
                        chunk = await cast(asyncio.StreamReader, stream_output_process.stdout).readexactly(ts.PACKET_SIZE)
                        if pipeline_output_started_at is None:
                            pipeline_output_started_at = time.monotonic()

                        # 同時に chunk_buffer / chunk_written_at にアクセスするタスクが1つだけであることを保証する (排他ロック)
                        async with writer_lock:

                            # 188 bytes ごとに区切られた、エンコーダーの出力のチャンクをバッファに貯める
                            chunk_buffer += chunk

                            # チャンクバッファが 65536 bytes (64KB) 以上になった時のみ
                            if len(chunk_buffer) >= 65536:

                                # エンコーダーからの出力をライブストリームの Queue に書き込む
                                self.live_stream.writeStreamData(bytes(chunk_buffer))
                                # print(f'Writer:    Chunk size: {len(chunk_buffer):05} / Time: {time.time()}')

                                # チャンクバッファを空にする（重要）
                                chunk_buffer = bytearray()

                                # チャンクの最終書き込み時刻を更新
                                chunk_written_at = time.monotonic()

                    # もし 188 bytes に満たないデータが返ってきたら、エンコーダーが終了したと判断してタスクを終了
                    except asyncio.IncompleteReadError:
                        break

                    # エンコードタスクが終了しているか既にエンコーダープロセスが終了していたら、タスクを終了
                    if is_running is False or IsPipelineTerminated() is True:
                        break

            # 前回のチャンク書き込みから 0.025 秒以上経ったもののチャンクが 64KB に達していない際に Writer に代わってチャンク書き込みを行うタスク
            ## ラジオチャンネルは通常のチャンネルと比べてデータ量が圧倒的に少ないため、64KB に達することは稀で SubWriter でのチャンク書き込みがメインになる
            async def SubWriter() -> None:

                nonlocal tuner_ts_read_at, tuner_ts_read_at_lock, chunk_buffer, chunk_written_at, writer_lock

                while True:

                    # チャンクバッファを 0.025 秒間隔でチェックする
                    await asyncio.sleep(0.025)

                    # 同時に chunk_buffer / chunk_written_at にアクセスするタスクが1つだけであることを保証する (排他ロック)
                    async with writer_lock:

                        # 前回チャンクを書き込んでから 0.025 秒以上経過している & チャンクバッファに何かしらデータが入っている時のみ
                        # チャンクをできるだけ等間隔でクライアントに送信するために、バッファが 64KB 分溜まるのを待たずに送信する
                        if (time.monotonic() - chunk_written_at) > 0.025 and (len(chunk_buffer) > 0):

                            # エンコーダーからの出力をライブストリームの Queue に書き込む
                            self.live_stream.writeStreamData(bytes(chunk_buffer))
                            # print(f'SubWriter: Chunk size: {len(chunk_buffer):05} / Time: {time.time()}')

                            # チャンクバッファを空にする（重要）
                            chunk_buffer = bytearray()

                            # チャンクの最終書き込み時刻を更新
                            chunk_written_at = time.monotonic()

                    # エンコードタスクが終了しているか既にエンコーダープロセスが終了していたら、タスクを終了
                    if is_running is False or IsPipelineTerminated() is True:
                        break

            # タスクを非同期で実行
            self.trackBackgroundTask(asyncio.create_task(Writer()), background_tasks)
            self.trackBackgroundTask(asyncio.create_task(SubWriter()), background_tasks)

            # ***** エンコーダーの状態監視 *****

            # エンコーダーの出力ログのリスト
            lines: list[str] = []

            async def EncoderObServer() -> None:

                # 1つ上のスコープ (Enclosing Scope) の変数を書き替えるために必要
                # ref: https://excel-ubara.com/python/python014.html#sec04
                nonlocal lines, program_present

                # 既にエンコーダーのログファイルが存在していた場合は上書きしないようにリネーム
                ## ref: https://note.nkmk.me/python-pathlib-name-suffix-parent/
                count = 1
                encoder_log_path = LOGS_DIR / f'KonomiTV-BS4K-Encoder-{self.live_stream.live_stream_id}.log'
                while await anyio.Path(str(encoder_log_path)).exists():
                    encoder_log_path = LOGS_DIR / f'KonomiTV-BS4K-Encoder-{self.live_stream.live_stream_id}-{count}.log'
                    count += 1

                # エンコーダーのログファイルを開く (エンコーダーログ有効時のみ)
                encoder_log: AsyncTextIOWrapper | None = None
                if CONFIG.general.debug_encoder is True:
                    encoder_log = await aiofiles.open(encoder_log_path, mode='w', encoding='utf-8')

                # エンコーダーの出力結果を取得
                while True:

                    # 行ごとに随時読み込む
                    ## 1バイトずつ読み込み、\r か \n が来たら行としてデコード
                    ## FFmpeg はコンソールの行を上書きするために frame= の進捗ログで \r しか出力しないため、readline() を使うと
                    ## 進捗ログを取得できずに永遠に Standby から ONAir に移行しない不具合が発生する
                    buffer = bytearray()
                    while True:
                        byte = await cast(asyncio.StreamReader, encoder.stderr).read(1)
                        buffer += byte
                        if byte == b'\r' or byte == b'\n':
                            break
                        if byte == b'':
                            break

                    # 空のデータが返ってきたら、エンコーダーが終了したと判断してタスクを終了
                    if len(buffer) == 0:
                        break

                    try:
                        line = buffer.decode('utf-8').strip()
                    except UnicodeDecodeError:
                        continue

                    # 山ほど出力されるメッセージと空行をログから除外
                    ## 元は "Delay between the first packet and last packet in the muxing queue is xxxxxx > 1: forcing output" と
                    ## "removing 2 bytes from input bitstream not read by decoder." という2つのメッセージで、実害はない
                    ## FFmpeg 内部の複数スレッドから同時出力され行頭が欠けることがあるため、部分一致で除外する
                    if (('removing 2 bytes from input bitstream not read by decoder.' not in line) and
                        ('Delay between the' not in line) and
                        ('[h264_metadata' not in line) and
                        ('[hevc_metadata' not in line) and
                        ('packet in the muxing queue' not in line) and ('ing output' not in line) and
                        ('ng output' != line) and ('g output' != line) and (' output' != line) and ('output' != line) and
                        ('utput' != line) and ('tput' != line) and ('put' != line) and ('ut' != line) and ('t' != line) and
                        ('' != line)):

                        # 異常時に必要な最新行だけを固定長で保持し、長時間ライブでRSSを増やさない。
                        self.appendBoundedEncoderLogLine(lines, line)

                        # ストリーム関連のログを表示
                        ## エンコーダーのログ出力が有効なら、ストリーム関連に限らずすべてのログを出力する
                        if 'Stream #0:' in line or CONFIG.general.debug_encoder is True:
                            logging.debug(f'{self.live_stream.log_prefix} [{ffmpeg8_log_label}] ' + line)

                        # エンコーダーのログ出力が有効なら、エンコーダーのログファイルに書き込む
                        if CONFIG.general.debug_encoder is True and encoder_log is not None:
                            await encoder_log.write(line.strip('\r\n') + '\n')
                            await encoder_log.flush()

                    # Standbyからの状態遷移と、ONAir後も継続するretry reset判定を同じ進捗経路で行う。
                    self.handleEncoderProgressLine(
                        line,
                        pipeline_output_started_at,
                        bridge.returncode if bridge is not None else None,
                        bridge_required = bridge_required,
                        now = time.monotonic(),
                    )

                    # 特定のエラーログが出力されている場合は回復が見込めないため、エンコーダーを終了する
                    ## エンコーダーを再起動することで回復が期待できる場合は、ステータスを Restart に設定しエンコードタスクを再起動する
                    if 'Stream map \'0:v:0\' matches no streams.' in line:
                        # 何らかの要因で tsreadex から放送波を受信できなかったため、エンコーダーの再起動は行わない
                        ## 番組名に「放送休止」などが入っていれば停波によるものとみなし、そうでないなら放送波の受信失敗とする
                        if program_present is None or program_present.isOffTheAirProgram():
                            self.live_stream.setStatus('Offline', 'この時間は放送を休止しています。(E-04F)')
                        else:
                            self.live_stream.setStatus('Offline', 'チューナーからの放送波の受信に失敗したため、エンコードを開始できません。(E-04F)')
                    elif ENCODER_TYPE == 'NVENC' and (
                        'No capable devices found' in line or
                        'OpenEncodeSessionEx failed' in line or
                        'Cannot load libcuda' in line
                    ):
                        # FFmpeg 8 の NVENC 初期化に失敗した場合は再起動を繰り返しても回復しない
                        self.live_stream.setStatus('Offline', 'お使いの PC 環境は NVENC エンコーダーに対応していません。(E-09HN)')
                    elif ENCODER_TYPE == 'QSV' and (
                        'Error initializing an MFX session' in line or
                        'No device available for decoder' in line
                    ):
                        # FFmpeg 8 の QSV 初期化に失敗した場合は再起動を繰り返しても回復しない
                        self.live_stream.setStatus('Offline', 'お使いの PC 環境は QSV エンコーダーに対応していません。(E-07HQ)')
                    elif ENCODER_TYPE == 'AMF' and (
                        'AMF failed to initialise' in line or
                        'CreateComponent' in line
                    ):
                        # FFmpeg 8 の AMF 初期化に失敗した場合は再起動を繰り返しても回復しない
                        self.live_stream.setStatus('Offline', 'お使いの PC 環境は AMF エンコーダーに対応していません。(E-10HV)')
                    elif 'Conversion failed!' in line:
                        # 捕捉されないエラーはエンコーダーの再起動で復帰できる可能性がある
                        result = self.live_stream.setStatus('Restart', 'エンコード中に予期しないエラーが発生しました。エンコードタスクを再起動しています… (ER-01F)')
                        # FFmpeg 8 の直近 100 件のログを表示する
                        if result is True:
                            for log in lines[-101:-1]:
                                logging.warning(log)

                    # エンコードタスクが終了しているか既にエンコーダープロセスが終了していたら、タスクを終了
                    if is_running is False or IsPipelineTerminated() is True:
                        break

                # タスクを終える前にエンコーダーのログファイルを閉じる
                if CONFIG.general.debug_encoder is True and encoder_log is not None:
                    await encoder_log.close()

            # タスクを非同期で実行
            self.trackBackgroundTask(asyncio.create_task(EncoderObServer()), background_tasks)

            # Bridge の stderr を継続的に読み、pipe の満杯による停止を防ぎつつ失敗理由を保持する。
            bridge_lines: list[str] = []

            async def BridgeObserver() -> None:
                if bridge is None or bridge.stderr is None:
                    return
                while True:
                    line_bytes = await bridge.stderr.readline()
                    if line_bytes == b'':
                        break
                    line = line_bytes.decode('utf-8', errors='replace').strip()
                    if line == '':
                        continue
                    bridge_lines.append(line)
                    if len(bridge_lines) > 100:
                        del bridge_lines[:-100]
                    logging.warning(f'{self.live_stream.log_prefix} [TSCodecBridge] {line}')

            if bridge is not None:
                self.trackBackgroundTask(asyncio.create_task(BridgeObserver()), background_tasks)

            # ***** エンコードタスク全体の制御 *****

            async def Controller() -> None:

                # 1つ上のスコープ (Enclosing Scope) の変数を書き替えるために必要
                # ref: https://excel-ubara.com/python/python014.html#sec04
                nonlocal lines, program_present

                next_program_refresh_at = 0.0

                while True:

                    # ライブストリームのステータスを取得
                    live_stream_status = self.live_stream.getStatus()

                    # 番組境界、または EPG が空の時間帯で定期的に現在番組を再取得する。
                    # 既知の映像解像度が変化した場合は、HW デコーダーが旧入力形式のまま
                    # フリーズすることを避けるため、異常検知を待たず計画的に再起動する。
                    should_refresh_program = (
                        program_present is not None and
                        time.time() > program_present.end_time.timestamp()
                    ) or (
                        program_present is None and
                        time.monotonic() >= next_program_refresh_at
                    )
                    if should_refresh_program is True:

                        previous_video_resolution = (
                            program_present.video_resolution
                            if program_present is not None else None
                        )

                        # 新しい現在放送中の番組情報を取得する
                        program_following = (await channel.getCurrentAndNextProgram())[0]
                        if program_following is not None:

                            # 現在の番組のタイトルをログに出力
                            logging.info(f'{self.live_stream.log_prefix} Title: {program_following.title}')

                            if self.shouldRestartEncoderForVideoResolutionChange(
                                previous_video_resolution,
                                program_following.video_resolution,
                            ) is True:
                                program_present = program_following
                                self.live_stream.setStatus(
                                    'Restart',
                                    '番組境界で映像解像度が変化したため、'
                                    'エンコードタスクを再起動しています… (ER-08)',
                                )
                                break

                        program_present = program_following
                        if program_present is None:
                            next_program_refresh_at = (
                                time.monotonic() + self.PROGRAM_REFRESH_INTERVAL_SECONDS
                            )
                        del program_following

                    # 現在 ONAir でかつクライアント数が 0 なら Idling（アイドリング状態）に移行
                    if live_stream_status.status == 'ONAir' and live_stream_status.client_count == 0:
                        self.live_stream.setStatus('Idling', 'ライブストリームは Idling です。')

                    # 現在 Idling でかつ最終更新から max_alive_time 秒以上経っていたらエンコーダーを終了し、Offline 状態に移行
                    if ((live_stream_status.status == 'Idling') and
                        (time.time() - live_stream_status.updated_at > CONFIG.tv.max_alive_time)):
                        self.live_stream.setStatus('Offline', 'ライブストリームは Offline です。')

                    # ***** 異常処理 (エンコードタスク再起動による回復が不可能) *****

                    # 前回チューナーからの放送波 TS を読み取ってから TUNER_TS_READ_TIMEOUT 秒以上経過していたら、
                    # 停波中もしくはチューナーからの放送波 TS の送信が停止したと判断して Offline に移行
                    async with tuner_ts_read_at_lock:
                        if (time.monotonic() - tuner_ts_read_at) > self.TUNER_TS_READ_TIMEOUT:

                            # 番組名に「放送休止」などが入っていれば停波の可能性が高い
                            if program_present is None or program_present.isOffTheAirProgram():
                                self.live_stream.setStatus('Offline', 'この時間は放送を休止しています。(E-11)')

                            # それ以外は受信エラーとする
                            else:
                                self.live_stream.setStatus('Offline', 'チューナーからの放送波の受信がタイムアウトしました。チューナー側に何らかの問題があるかもしれません。(E-11)')

                    # Mirakurun の Service Stream API からエラーが返された場合
                    if LIVE_STREAM_BACKEND == 'Mirakurun' and response is not None and response.status != 200:
                        # レスポンスヘッダーの server が mirakc であれば mirakc と判定できる
                        if ('server' in response.headers) and ('mirakc' in response.headers['server']):
                            mirakurun_or_mirakc = 'mirakc'
                        else:
                            mirakurun_or_mirakc = 'Mirakurun'
                        # Offline にしてエンコードタスクを停止する
                        ## mirakc はなぜかチューナー不足時に 503 ではなく 404 を返すことがある (バグ?)
                        if response.status == 503 or (response.status == 404 and mirakurun_or_mirakc == 'mirakc'):
                            self.live_stream.setStatus('Offline', 'チューナーの起動に失敗しました。空きチューナーが不足している可能性があります。(E-12M)')
                        elif response.status == 404:
                            self.live_stream.setStatus('Offline', f'現在このチャンネルは受信できません。{mirakurun_or_mirakc} 側に問題があるかもしれません。(HTTP Error {response.status}) (E-12M)')
                        else:
                            self.live_stream.setStatus('Offline', f'チューナーで不明なエラーが発生しました。{mirakurun_or_mirakc} 側に問題があるかもしれません。(HTTP Error {response.status}) (E-12M)')
                        break

                    # ***** 異常処理 (エンコードタスク再起動による回復が可能) *****

                    # 現在 Standby でかつストリームデータの最終書き込み時刻から
                    # ENCODER_TS_READ_TIMEOUT_STANDBY 秒以上が経過しているなら、エンコーダーがフリーズしたものとみなす
                    # 現在 ONAir でかつストリームデータの最終書き込み時刻から
                    # ENCODER_TS_READ_TIMEOUT_ONAIR 秒以上が経過している場合も、エンコーダーがフリーズしたものとみなす
                    ## 何らかの理由でエンコードが途中で停止した場合、live_stream.write() が実行されなくなることを利用している
                    encoder_ts_read_timeout_onair = \
                        self.ENCODER_TS_READ_TIMEOUT_ONAIR_AMF if ENCODER_TYPE == 'AMF' else self.ENCODER_TS_READ_TIMEOUT_ONAIR
                    stream_data_last_write_time = time.time() - self.live_stream.getStreamDataWrittenAt()
                    if ((live_stream_status.status == 'Standby' and stream_data_last_write_time > self.ENCODER_TS_READ_TIMEOUT_STANDBY) or
                        (live_stream_status.status == 'ONAir' and stream_data_last_write_time > encoder_ts_read_timeout_onair)):

                        # 番組名に「放送休止」などが入っている場合、チューナーから出力された放送波 TS に映像/音声ストリームが
                        # 含まれていない可能性が高いので、ここでエンコードタスクを停止する
                        ## 映像/音声ストリームが含まれていない場合は当然ながらエンコーダーはフリーズする
                        if program_present is None or program_present.isOffTheAirProgram():
                            self.live_stream.setStatus('Offline', 'この時間は放送を休止しています。(E-13)')

                        # それ以外なら、エンコーダーの再起動で復帰できる可能性があるのでエンコードタスクを再起動する
                        else:

                            # できるだけエンコーダーのエラーメッセージを拾ってログを出力してから終了したいので、1秒間実行を待機する
                            await asyncio.sleep(1)

                            # エンコードタスクを再起動
                            result = self.live_stream.setStatus('Restart', 'エンコードが途中で停止しました。エンコードタスクを再起動しています… (ER-04)')

                            # FFmpeg 8 の直近 100 件のログを表示する
                            if result is True:
                                for log in lines[-101:-1]:
                                    logging.warning(log)

                    # チューナーとの接続が切断された場合
                    ## ref: https://stackoverflow.com/a/45251241/17124142
                    if ((LIVE_STREAM_BACKEND == 'Mirakurun' and response is not None and response.closed is True) or
                        (LIVE_STREAM_BACKEND == 'EDCB' and self.live_stream.tuner is not None and self.live_stream.tuner.isDisconnected() is True)):

                        # エンコードタスクを再起動
                        self.live_stream.setStatus('Restart', 'チューナーとの接続が切断されました。エンコードタスクを再起動しています… (ER-05)')

                    # TS Codec Bridge が意図せず終了した場合
                    if bridge is not None and bridge.returncode is not None:
                        if self.live_stream.getStatus().status != 'Offline':
                            result = self.live_stream.setStatus(
                                'Restart',
                                'TS Codec Bridge が停止したため、エンコードタスクを再起動しています… (ER-07B)',
                            )
                            if result is True:
                                for log in bridge_lines[-100:]:
                                    logging.warning(log)
                        break

                    # エンコーダーが意図せず終了した場合
                    if encoder.returncode is not None:

                        # 起動時の FFmpeg 8 能力検査を通過した後の異常終了は、一時的な入力・デバイス障害として再起動を試みる
                        if self.live_stream.getStatus().status != 'Offline':

                            # エンコードタスクを再起動
                            result = self.live_stream.setStatus('Restart', 'エンコーダーが強制終了されました。エンコードタスクを再起動しています… (ER-06)')

                            # FFmpeg 8 の直近 100 件のログを表示する
                            if result is True:
                                for log in lines[-101:-1]:
                                    logging.warning(log)

                        # エンコーダーが既に終了しているため、後続の異常検出処理を実行する意味がない
                        # この時点でステータスは Offline か Restart のいずれかに設定されているはずなので、
                        # 直接ループを抜けてエンコードタスクの終了処理に移る
                        break

                    # この時点で最新のライブストリームのステータスが Offline か Restart に変更されていたら、エンコードタスクの終了処理に移る
                    live_stream_status = self.live_stream.getStatus()  # 更新されているかもしれないので再取得
                    if live_stream_status.status == 'Offline' or live_stream_status.status == 'Restart':
                        break

                    # ビジーにならないように 0.1 秒待機
                    await asyncio.sleep(0.1)


            # エンコードタスクのメインループを実行する
            await Controller()

        except asyncio.CancelledError:
            # チャンネル切り替え時に LiveStream.connect() からこのタスクがキャンセルされる場合がある
            ## CancelledError はチューナー起動フェーズまたは Controller 内の await で発生しうる
            ## run() の finally で資源を回収するため、ここで握り潰さずに呼び出し元へ再送出する
            raise

        # ***** エンコードタスクの終了処理 *****

        # 稼働中フラグをオフにし、Reader・Writer・SubWriter・EncoderObServer のすべての非同期タスクを終了させる
        is_running = False

        # response / session・子プロセス・FD・非同期タスク・アーカイバー・クライアントをすべて回収する
        ## 再起動開始前に完了させるため、次の run() と資源所有権が重ならない
        await self.__cleanupResources(cleanup_context)

        # エンコードタスクを再起動する（エンコーダーの再起動が必要な場合）
        if self.live_stream.getStatus().status == 'Restart':

            # チューナーをアンロックする (EDCB バックエンドのみ)
            ## 新しいエンコードタスクが今回立ち上げたチューナーを再利用できるようにする
            ## エンコーダーの再起動が必要なだけでチューナー自体はそのまま使えるし、わざわざ閉じてからもう一度開くのは無駄
            if LIVE_STREAM_BACKEND == 'EDCB' and self.live_stream.tuner is not None:
                self.live_stream.tuner.unlock(self.live_stream.live_stream_id)

            # 再起動回数が最大再起動回数に達していなければ、再起動する
            if self._retry_count < self.MAX_RETRY_COUNT:
                self._retry_count += 1  # カウントを増やす
                await asyncio.sleep(0.1)  # 少し待つ
                # run() の外側ループが同じ管理 Task のまま次の実行世代を開始する
                ## LiveStream._live_encoding_task_ref を差し替えないため、チューナー移譲時の cancel() が確実に届く

            # 最大再起動回数を使い果たしたので、Offline にする
            else:

                # Offline に設定
                if program_present is None or program_present.is_free is True:
                    # 無料番組
                    self.live_stream.setStatus('Offline', 'ライブストリームの再起動に失敗しました。(E-17)')
                else:
                    # 有料番組（契約されていないことが原因の可能性が高いため、そのように表示する）
                    self.live_stream.setStatus('Offline', 'ライブストリームの再起動に失敗しました。契約されていないため視聴できません。(E-17)')

                # チューナーを終了する (EDCB バックエンドのみ)
                ## tuner.close() した時点でそのチューナーインスタンスは意味をなさなくなるので、LiveStream インスタンスのプロパティからも削除する
                if LIVE_STREAM_BACKEND == 'EDCB' and self.live_stream.tuner is not None:
                    if await self.live_stream.tuner.close(self.live_stream.live_stream_id) is True:
                        self.live_stream.tuner = None

        # 通常終了
        else:

            # EDCB バックエンドのみ
            if LIVE_STREAM_BACKEND == 'EDCB' and self.live_stream.tuner is not None:

                # 再利用中ならチューナーを閉じない
                ## LiveStream 側の handoff と競合しないようにする
                if self.live_stream.tuner.getState() == 'Cancelling':
                    # 強制的にガベージコレクションを実行してから早期 return する
                    gc.collect()
                    return

                # チューナーを終了する（まだ制御をこのライブストリームが保持している場合のみ）
                ## tuner.close() した時点でそのチューナーインスタンスは意味をなさなくなるので、LiveStream インスタンスのプロパティからも削除する
                if await self.live_stream.tuner.close(self.live_stream.live_stream_id) is True:
                    self.live_stream.tuner = None

        # 強制的にガベージコレクションを実行する
        gc.collect()
