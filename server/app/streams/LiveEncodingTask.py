
# Type Hints を指定できるように
# ref: https://stackoverflow.com/a/33533514/17124142
from __future__ import annotations

import asyncio
import gc
import os
import re
import secrets
import time
from collections.abc import AsyncIterator
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
from app.streams.KonomiTVBS4KPlaybackEncoding import (
    BuildKonomiTVBS4KLiveAspectPreservingScaleFilters,
    KonomiTVBS4KAudioCodec,
    KonomiTVBS4KVideoBitDepth,
    KonomiTVBS4KVideoCodec,
    ParseKonomiTVBS4KAdvancedLiveMuxrateKbps,
    ResolveKonomiTVBS4KAdvancedLiveMuxrate,
    ResolveKonomiTVBS4KLiveEncodePlan,
    ResolveKonomiTVBS4KPlaybackVideoBitrate,
)
from app.streams.LivePSIDataArchiver import LivePSIDataArchiver
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


class LiveEncodingTask:

    # KonomiTV-BS4K TS Codec Bridge の LIBRARY_PATH キー
    TS_CODEC_BRIDGE_LIBRARY_PATH_KEY: ClassVar[str] = 'KonomiTVBS4KTSCodecBridge'

    # H.264 再生時のエンコード後のストリームの GOP 長 (秒)
    GOP_LENGTH_SECONDS_H264: ClassVar[float] = 0.5

    # H.265 再生時のエンコード後のストリームの GOP 長 (秒)
    GOP_LENGTH_SECONDS_H265: ClassVar[float] = float(2)

    # 放送入力の一時的な音声 PTS 欠落を無音で補完し、再エンコード後の時刻列を連続化する。
    # TS Codec Bridge の連続性検査を緩めず、ブラウザへ不連続な Opus/AAC を渡さないために使う。
    LIVE_TRANSCODE_AUDIO_FILTER: ClassVar[str] = 'aresample=48000:async=1'

    # エンコードタスクの最大リトライ回数
    ## この数を超えた場合はエンコードタスクを再起動しない（無限ループを避ける）
    MAX_RETRY_COUNT: ClassVar[int] = 10  # 10回まで

    # チューナーから放送波 TS を読み取る際のタイムアウト (秒)
    TUNER_TS_READ_TIMEOUT: ClassVar[int] = 15

    # エンコーダーの出力を読み取る際のタイムアウト (Standby 時) (秒)
    ENCODER_TS_READ_TIMEOUT_STANDBY: ClassVar[int] = 20

    # エンコーダーの出力を読み取る際のタイムアウト (ONAir 時) (秒)
    # VCEEncC 利用時のみ起動時に OpenCL シェーダーがコンパイルされる関係で起動が遅いため、10 秒に設定
    ENCODER_TS_READ_TIMEOUT_ONAIR: ClassVar[int] = 5
    ENCODER_TS_READ_TIMEOUT_ONAIR_VCEENCC: ClassVar[int] = 10


    def __init__(self, live_stream: LiveStream) -> None:
        """
        LiveStream のインスタンスに基づくライブエンコードタスクを初期化する
        このエンコードタスクが LiveStream を実質的に制御する形になる

        Args:
            live_stream (LiveStream): LiveStream のインスタンス
        """

        # ライブストリームのインスタンスをセット
        self.live_stream = live_stream

        # エンコードタスクのリトライ回数のカウント
        self._retry_count = 0


    @staticmethod
    def GenerateStreamAnchorGenerationID() -> int:
        """ライブ実行ごとに重複しない非ゼロの Stream Anchor generation ID を生成する。"""

        generation_id = 0
        while generation_id == 0:
            generation_id = secrets.randbits(64)
        return generation_id


    def ResolveStreamAnchorGenerationID(self, stream_anchor_enabled: bool) -> int | None:
        """source markerを最終化するAnchor経路でだけgeneration IDを生成する。"""

        return self.GenerateStreamAnchorGenerationID() if stream_anchor_enabled is True else None


    def IsStreamAnchorEnabled(self) -> bool:
        """このライブストリームで最終 Stream Anchor を確定するか返す。"""

        return self.live_stream.stream_anchor_enabled


    def GetRequestedVideoCodec(self) -> KonomiTVBS4KVideoCodec:
        """正規化済みの要求映像コーデックを返す。"""

        requested_codec = self.live_stream.encoding_options.video_codec
        if requested_codec in ('avc', 'hevc', 'vp9', 'av1'):
            return requested_codec
        raise ValueError(f'Unsupported live video codec: {requested_codec}')


    def GetRequestedVideoBitDepth(self) -> KonomiTVBS4KVideoBitDepth:
        """正規化済みの要求映像 bit depth を返す。"""

        requested_bit_depth = self.live_stream.encoding_options.video_bit_depth
        if requested_bit_depth in (8, 10):
            return requested_bit_depth
        raise ValueError(f'Unsupported live video bit depth: {requested_bit_depth}')


    def GetRequestedAudioCodec(self) -> KonomiTVBS4KAudioCodec:
        """正規化済みの要求音声コーデックを返す。"""

        requested_codec = self.live_stream.encoding_options.audio_codec
        if requested_codec in ('aac', 'opus'):
            return requested_codec
        raise ValueError(f'Unsupported live audio codec: {requested_codec}')


    def IsTSCodecBridgeRequired(self, is_radiochannel: bool = False) -> bool:
        """VP9 / AV1 または Opus を最終 TS へ規格化する必要があるか返す。"""

        return (
            (is_radiochannel is False and self.GetRequestedVideoCodec() in ('vp9', 'av1')) or
            self.GetRequestedAudioCodec() == 'opus'
        )


    def BuildTSCodecBridgeOptions(self, is_radiochannel: bool = False) -> list[str]:
        """確定 codec tuple と Stream Anchor 条件から Bridge オプションを返す。"""

        video_codec = self.GetRequestedVideoCodec()
        options = [
            '--video-codec',
            video_codec if is_radiochannel is False and video_codec in ('vp9', 'av1') else 'passthrough',
            '--audio-codec', self.GetRequestedAudioCodec(),
        ]
        if self.IsStreamAnchorEnabled() is True and is_radiochannel is False:
            options.append('--stream-anchor-v1')
        if is_radiochannel is False and video_codec == 'av1':
            muxrate = ResolveKonomiTVBS4KAdvancedLiveMuxrate(
                ResolveKonomiTVBS4KPlaybackVideoBitrate(
                    self.live_stream.quality,
                    video_codec,
                ).video_bitrate_max,
                quality = self.live_stream.quality,
                video_codec = video_codec,
            )
            options += [
                '--transport-rate-kbps',
                ParseKonomiTVBS4KAdvancedLiveMuxrateKbps(muxrate),
            ]
        return options


    def buildFFmpeg8HardwareOptions(
        self,
        quality: QUALITY_TYPES,
        encoder_type: Literal['QSV', 'NVENC', 'AMF'],
        channel_type: Literal['GR', 'BS', 'CS', 'CATV', 'SKY', 'BS4K'],
        is_fullhd_channel: bool,
        is_oneseg: bool = False,
    ) -> list[str]:
        """現 main の単一 pipeline 向け FFmpeg 8 HW エンコードオプションを返す。"""

        config = Config()
        codec = self.GetRequestedVideoCodec()
        bit_depth = self.GetRequestedVideoBitDepth()
        codec_spec = RecordedPlaybackBackend.getCodecSpec(codec, bit_depth)
        bitrate = ResolveKonomiTVBS4KPlaybackVideoBitrate(quality, codec)
        encoder_name = RecordedPlaybackBackend.getEncoderName(encoder_type, codec)
        if encoder_name is None:
            raise RuntimeError(f'Unsupported FFmpeg 8 live encoder: {encoder_type}/{codec}')

        # 現 main では source coordinator を持たないため、デコードと表示アスペクト処理は
        # system memory で行い、エンコード直前だけ選択 backend へ upload する。
        options: list[str] = []
        selected_device: str | None = None
        if encoder_type in ('QSV', 'AMF'):
            selected_device = RecordedPlaybackCapabilityProbe.getSelectedDevice(
                encoder_type,
                codec,
                bit_depth,
            )
            if selected_device is None:
                # legacy URLはtargeted probeを通らないため、その場合だけ同vendorの先頭候補へ退避する。
                render_devices = RecordedPlaybackBackend.discoverRenderDevices(encoder_type)
                if len(render_devices) == 0:
                    raise RuntimeError(f'No compatible render device was found for {encoder_type}.')
                selected_device = render_devices[0]
        if encoder_type == 'QSV':
            options += [
                '-init_hw_device', f'qsv=live_qsv:{selected_device}',
                '-filter_hw_device', 'live_qsv',
            ]
        elif encoder_type == 'NVENC':
            options += [
                '-init_hw_device', 'cuda=live_cuda:0',
                '-filter_hw_device', 'live_cuda',
            ]
        elif encoder_type == 'AMF':
            options += [
                '-init_hw_device', f'vaapi=live_vaapi:{selected_device}',
                '-filter_hw_device', 'live_vaapi',
            ]

        input_probesize: str | None = None
        if is_oneseg is True:
            analyzeduration = round(2_500_000 + (self._retry_count * 200_000))
        elif channel_type == 'BS4K' and config.general.encoder_bs4k_input_analysis_enabled is True:
            input_probesize = f'{round(config.general.encoder_bs4k_input_probesize + (self._retry_count * 500))}K'
            analyzeduration = round(
                (config.general.encoder_bs4k_input_analyze * 1_000_000) +
                (self._retry_count * 200_000)
            )
        elif channel_type == 'SKY':
            analyzeduration = round(700_000 + (self._retry_count * 200_000))
        else:
            analyzeduration = round(500_000 + (self._retry_count * 200_000))

        low_latency = channel_type != 'BS4K' or config.general.encoder_bs4k_low_latency is True
        if low_latency is True:
            # -flags low_delay は MPEG-2 の B フレームを復号順で出力し、表示 PTS を逆行させる。
            # demux の先読みだけを抑える -fflags nobuffer は維持する。
            options += ['-fflags', 'nobuffer']
        options += ['-f', 'mpegts']
        if input_probesize is not None:
            options += ['-probesize', input_probesize]
        options += [
            '-analyzeduration', str(analyzeduration), '-i', 'pipe:0',
            '-ignore_unknown',
            '-map', '0:v:0',
            '-map', '0:a?',
            '-map', '0:d?',
        ]

        encode_plan = ResolveKonomiTVBS4KLiveEncodePlan(
            QUALITY[quality].width,
            QUALITY[quality].height,
            video_codec = codec,
            is_fullhd_channel = is_fullhd_channel,
        )
        filters: list[str] = []
        if is_oneseg is False and channel_type != 'BS4K':
            if self.live_stream.encoding_options.is_24fps_mode_enabled is True:
                filters += ['pullup', 'dejudder']
            elif QUALITY[quality].is_60fps is True:
                # ISDB 1080i/480i は top-field-first。auto 判定は隣接 field を逆順にし得るため固定する。
                filters.append('yadif=mode=1:parity=0:deint=1')
            else:
                filters.append('yadif=mode=0:parity=0:deint=1')
        filters += BuildKonomiTVBS4KLiveAspectPreservingScaleFilters(encode_plan)
        # HW encoder は system-memory の YUV420P ではなく NV12 / P010 を受け取る。
        # QSV/CUDA は続く upload で hardware frame へ変換する。AMF はprobeと同じ
        # VAAPI境界で選択済みdeviceを通した後、system-memoryの同形式へ戻して渡す。
        filters.append(f'format={codec_spec.encoder_pixel_format}')
        if encoder_type == 'QSV':
            filters.append('hwupload=extra_hw_frames=64')
        elif encoder_type == 'NVENC':
            filters.append('hwupload_cuda')
        else:
            # probeで選ばれたAMD render nodeを実経路でも通し、AMFへはsystem-memoryで戻す。
            filters += [
                'hwupload',
                f'scale_vaapi=w={encode_plan.encode_width}:h={encode_plan.encode_height}:'
                f'format={codec_spec.encoder_pixel_format}',
                f'hwdownload,format={codec_spec.encoder_pixel_format}',
            ]
        options += ['-vf', ','.join(filters)]

        options += [
            '-c:v', encoder_name,
            '-b:v', bitrate.video_bitrate,
            '-maxrate', bitrate.video_bitrate_max,
            '-aspect', '16:9',
        ]
        if codec in ('vp9', 'av1'):
            options += ['-bufsize', bitrate.video_bitrate_max]
        # hwupload 後へ software pixel format を強制すると auto_scale が挿入される。
        if encoder_type == 'NVENC':
            options += ['-pix_fmt', 'cuda']
        elif encoder_type == 'AMF':
            options += ['-pix_fmt', codec_spec.encoder_pixel_format]
        if encoder_type == 'QSV':
            options += ['-preset', 'medium', '-async_depth', '1', '-bf', '0']
            # look_ahead は h264_qsv 固有。HEVC / VP9 / AV1 へ渡すと未使用警告になり、
            # 能力probeと実ライブ起動の引数契約も曖昧になるため AVC だけに限定する。
            if codec == 'avc':
                options += ['-look_ahead', '0']
        elif encoder_type == 'NVENC':
            options += [
                '-preset', 'p4', '-tune', 'll', '-rc', 'vbr', '-rc-lookahead', '0',
                '-spatial-aq', '1', '-temporal-aq', '1', '-zerolatency', '1', '-bf', '0',
            ]
        else:
            options += ['-quality', 'balanced', '-rc', 'vbr_latency', '-usage', 'lowlatency', '-bf', '0']

        if codec == 'hevc':
            options += [
                '-profile:v',
                'main10' if bit_depth == 10 else 'main',
            ]
        elif codec == 'avc':
            options += ['-profile:v', 'high']
        elif codec == 'vp9':
            # vp9_qsv は数値でなく profile0 / profile2 を受け付ける。
            options += ['-profile:v', 'profile2' if bit_depth == 10 else 'profile0']
        elif codec == 'av1' and encoder_type != 'NVENC':
            # av1_nvenc は profile オプション自体を公開しない。QSV / AMF は main を受け付ける。
            options += ['-profile:v', 'main']

        if is_oneseg is True:
            options += ['-fps_mode', 'vfr', '-g', '15' if codec in ('vp9', 'av1') else ('30' if codec == 'hevc' else '8')]
        elif channel_type == 'BS4K':
            frame_rate = 30 if '-30fps' in quality else 60
            options += [
                '-r', '30000/1001' if frame_rate == 30 else '60000/1001',
                '-g', str(frame_rate),
            ]
        elif self.live_stream.encoding_options.is_24fps_mode_enabled is True:
            options += ['-fps_mode', 'vfr', '-g', '30']
        elif QUALITY[quality].is_60fps is True:
            options += ['-r', '60000/1001', '-g', '30']
        else:
            options += ['-r', '30000/1001', '-g', '15']

        if self.GetRequestedAudioCodec() == 'opus':
            options += [
                '-af', self.LIVE_TRANSCODE_AUDIO_FILTER,
                '-c:a', 'libopus', '-application', 'audio', '-ac', '2', '-b:a', '192K', '-ar', '48000',
            ]
        elif is_oneseg is True:
            # ワンセグは従来どおりAAC stereoへ正規化する。
            options += [
                '-af', self.LIVE_TRANSCODE_AUDIO_FILTER,
                '-c:a', 'aac', '-aac_coder', 'twoloop', '-ac', '2', '-b:a', '96K', '-ar', '48000',
            ]
        else:
            # 通常放送とCompatibility APIは実在AACトラックをそのまま保持し、
            # 5.1ch・dual mono・複数音声をstereoへ黙って変換しない。
            options += ['-c:a', 'copy']
        max_interleave_delta = round(
            (
                config.general.encoder_bs4k_max_interleave_delta
                if channel_type == 'BS4K'
                else 500
            ) + (self._retry_count * 100)
        )
        options += [
            '-c:d', 'copy',
            '-max_delay', '250000',
            '-max_interleave_delta', f'{max_interleave_delta}K',
        ]
        if self.IsStreamAnchorEnabled() is True or self.IsTSCodecBridgeRequired() is True:
            options += [
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


    def buildFFmpegOptions(self,
        quality: QUALITY_TYPES,
        channel_type: Literal['GR', 'BS', 'CS', 'CATV', 'SKY', 'BS4K'],
        is_fullhd_channel: bool,
        is_oneseg: bool = False,
    ) -> list[str]:
        """
        FFmpeg に渡すオプションを組み立てる

        Args:
            quality (QUALITY_TYPES): 映像の品質
            channel_type (Literal['GR', 'BS', 'CS', 'CATV', 'SKY', 'BS4K']): チャンネルの種類
            is_fullhd_channel (bool): フル HD 放送が実施されているチャンネルかどうか
            is_oneseg (bool): ワンセグサービスかどうか

        Returns:
            list[str]: FFmpeg に渡すオプションが連なる配列
        """

        codec = self.GetRequestedVideoCodec()
        bit_depth = self.GetRequestedVideoBitDepth()
        codec_spec = RecordedPlaybackBackend.getCodecSpec(codec, bit_depth)
        encoder_name = RecordedPlaybackBackend.getEncoderName('FFmpeg', codec)
        if encoder_name is None:
            raise RuntimeError(f'Unsupported FFmpeg 8 live encoder: FFmpeg/{codec}')
        bitrate = ResolveKonomiTVBS4KPlaybackVideoBitrate(quality, codec)

        # オプションの入る配列
        options: list[str] = []

        # 入力ストリームの解析時間
        CONFIG = Config()
        input_probesize: str | None = None
        if is_oneseg is True:
            # ワンセグは低フレームレートの H.264 で、GOP の途中から受信を開始すると
            # SPS/PPS・IDR の検出まで時間がかかるため、初回から十分な解析時間を確保する
            analyzeduration = round(2_500_000 + (self._retry_count * 200_000))
        elif channel_type == 'BS4K' and CONFIG.general.encoder_bs4k_input_analysis_enabled is True:
            input_probesize = f'{round(CONFIG.general.encoder_bs4k_input_probesize + (self._retry_count * 500))}K'
            analyzeduration = round((CONFIG.general.encoder_bs4k_input_analyze * 1000000) + (self._retry_count * 200000))
        elif channel_type == 'SKY':
            # H.264 入力のスカパー！プレミアムサービスは入力ストリームの解析時間を長めにする
            analyzeduration = round(700_000 + (self._retry_count * 200_000))
        else:
            analyzeduration = round(500000 + (self._retry_count * 200000))  # リトライ回数に応じて少し増やす

        # 入力
        ## -analyzeduration をつけることで、ストリームの分析時間を短縮できる
        input_options = '-f mpegts'
        if input_probesize is not None:
            input_options += f' -probesize {input_probesize}'
        input_options += f' -analyzeduration {analyzeduration} -i pipe:0'
        options.append(input_options)

        # 実在する全ストリームの optional map は音声codec確定後に一度だけ追加する。
        # ここで旧個別mapも残すと映像・音声・dataが重複し、音声1本の番組は0:a:1で起動失敗する。
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
            # -flags low_delay は入力 B フレームの表示 PTS を逆行させるため指定しない。
            options.append(f'-fflags nobuffer -max_delay 250000 -max_interleave_delta {max_interleave_delta}K -threads auto')

        # 映像
        ## コーデック
        options.append(f'-vcodec {encoder_name}')

        ## ビットレートと品質
        options.append(f'-flags +cgop -vb {bitrate.video_bitrate} -maxrate {bitrate.video_bitrate_max}')
        if codec in ('vp9', 'av1'):
            # 固定muxrateへ大きなI-frame burstを流さないよう、能力probeと同じVBV上限を持たせる。
            options.append(f'-bufsize {bitrate.video_bitrate_max}')
        options.append('-aspect 16:9')
        options.append(' '.join(RecordedPlaybackBackend.getTuningArguments('FFmpeg', codec)))
        options.append(f'-pix_fmt {codec_spec.pixel_format}')
        if codec == 'hevc':
            options.append(f'-profile:v {"main10" if bit_depth == 10 else "main"}')
        elif codec == 'avc':
            options.append('-profile:v high')
        elif codec == 'vp9':
            options.append(f'-profile:v {2 if bit_depth == 10 else 0} -lag-in-frames 0 -auto-alt-ref 0')
        else:
            options.append('-profile:v 0 -lag-in-frames 0')

        ## フル HD 放送が行われているチャンネルかつ、指定された品質の解像度が 1440×1080 (1080p) の場合のみ、
        ## 特別に縦解像度を 1920 に変更してフル HD (1920×1080) でエンコードする
        encode_plan = ResolveKonomiTVBS4KLiveEncodePlan(
            QUALITY[quality].width,
            QUALITY[quality].height,
            video_codec = codec,
            is_fullhd_channel = is_fullhd_channel,
        )
        aspect_filters = BuildKonomiTVBS4KLiveAspectPreservingScaleFilters(encode_plan)
        aspect_filters.append(f'format={codec_spec.pixel_format}')

        ## 最大 GOP 長 (秒)
        ## 30fps なら ×30 、 60fps なら ×60 された値が --gop-len で使われる
        gop_length_second = self.GOP_LENGTH_SECONDS_H264
        if codec == 'hevc':
            ## H.265/HEVC では高圧縮化のため、最大 GOP 長を長くする
            gop_length_second = self.GOP_LENGTH_SECONDS_H265

        # ワンセグはプログレッシブかつ約 10～15fps の VFR で放送されているため、
        ## フレームレートの固定やインターレース解除を行わず、入力 PTS をそのまま維持する。
        if is_oneseg is True:
            options.append(f'-vf {",".join(aspect_filters)}')
            options.append(f'-fps_mode vfr -g {15 if codec in ("vp9", "av1") else (30 if codec == "hevc" else 8)}')
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
                options.append(f'-vf yadif=mode=1:parity=0:deint=1,{",".join(aspect_filters)}')
                options.append(f'-r 60000/1001 -g {int(gop_length_second * 60)}')
            ## インターレース解除 (60i → 30p (フレームレート: 30fps))
            else:
                # 24fps モードでは、テレシネ由来の重複フレームを取り除いて 24/30p 混合 VFR で出力する
                ## dejudder を併用すると、24fps 区間の PTS が 41.7ms 間隔に均されて本来の 24fps に近い時刻列になる
                if self.live_stream.encoding_options.is_24fps_mode_enabled is True:
                    options.append(f'-vf pullup,dejudder,{",".join(aspect_filters)}')
                    options.append(f'-fps_mode vfr -g {int(gop_length_second * 30)}')
                else:
                    options.append(f'-vf yadif=mode=0:parity=0:deint=1,{",".join(aspect_filters)}')
                    options.append(f'-r 30000/1001 -g {int(gop_length_second * 30)}')

        # 音声
        options.append('-map 0:v:0 -map 0:a? -map 0:d?')
        if self.GetRequestedAudioCodec() == 'opus':
            options.append(
                f'-af {self.LIVE_TRANSCODE_AUDIO_FILTER} '
                '-acodec libopus -application audio -ac 2 -ab 192K -ar 48000'
            )
        elif is_oneseg is True:
            # ワンセグは従来どおりAAC stereoへ正規化する。
            options.append(
                f'-af {self.LIVE_TRANSCODE_AUDIO_FILTER} '
                '-acodec aac -aac_coder twoloop -ac 2 -ab 96K -ar 48000'
            )
        else:
            # 通常放送とCompatibility APIは実在AACトラックをそのまま保持し、
            # 5.1ch・dual mono・複数音声をstereoへ黙って変換しない。
            options.append('-acodec copy')

        # Bridge は PCR gap を fail closed で検証するため、Anchor 経路の TS は
        # codec にかかわらず固定 muxrate と 20ms PCR 周期で出力する。
        if self.IsStreamAnchorEnabled() is True or self.IsTSCodecBridgeRequired() is True:
            options.append(
                f'-muxrate {ResolveKonomiTVBS4KAdvancedLiveMuxrate(bitrate.video_bitrate_max, quality=quality, video_codec=codec)} '
                '-pcr_period 20'
            )

        # 出力
        options.append('-y -f mpegts')  # MPEG-TS 出力ということを明示
        options.append('pipe:1')  # 標準出力へ出力

        # オプションをスペースで区切って配列にする
        result: list[str] = []
        for option in options:
            result += option.split(' ')

        return result


    def buildFFmpegOptionsForRadio(self) -> list[str]:
        """
        FFmpeg に渡すオプションを組み立てる（ラジオチャンネル向け）
        音声の品質は変えたところでほとんど差がないため、1つだけに固定されている
        品質が固定ならコードにする必要は基本ないんだけど、可読性を高めるために敢えてこうしてある

        Returns:
            list[str]: FFmpeg に渡すオプションが連なる配列
        """

        # オプションの入る配列
        options: list[str] = []

        # 入力
        ## -analyzeduration をつけることで、ストリームの分析時間を短縮できる
        analyzeduration = round(500000 + (self._retry_count * 200000))  # リトライ回数に応じて少し増やす
        options.append(f'-f mpegts -analyzeduration {analyzeduration} -i pipe:0')

        # ストリームのマッピング
        # 音声切り替えのため、主音声・副音声両方をエンコード後の TS に含む
        options.append('-map 0:a:0 -map 0:a:1 -map 0:d? -ignore_unknown')

        # フラグ
        ## 主に FFmpeg の起動を高速化するための設定
        ## max_interleave_delta: mux 時に影響するオプションで、増やしすぎると CM で詰まりがちになる
        ## リトライなしの場合は 500K (0.5秒) に設定し、リトライ回数に応じて 100K (0.1秒) ずつ増やす
        max_interleave_delta = round(500 + (self._retry_count * 100))
        options.append(f'-fflags nobuffer -max_delay 250000 -max_interleave_delta {max_interleave_delta}K -threads auto')

        # 音声が 5.1ch かどうかに関わらずステレオへ正規化する。
        if self.GetRequestedAudioCodec() == 'opus':
            options.append(
                '-acodec libopus -application audio -ac 2 -ab 192K -ar 48000 '
                f'-af {self.LIVE_TRANSCODE_AUDIO_FILTER},volume=2.0'
            )
        else:
            options.append(
                '-acodec aac -aac_coder twoloop -ac 2 -ab 192K -ar 48000 '
                f'-af {self.LIVE_TRANSCODE_AUDIO_FILTER},volume=2.0'
            )

        # 出力
        options.append('-y -f mpegts')  # MPEG-TS 出力ということを明示
        options.append('pipe:1')  # 標準出力へ出力

        # オプションをスペースで区切って配列にする
        result: list[str] = []
        for option in options:
            result += option.split(' ')

        return result


    def buildHWEncCOptions(self,
        quality: QUALITY_TYPES,
        encoder_type: Literal['QSVEncC', 'NVEncC', 'VCEEncC'],
        channel_type: Literal['GR', 'BS', 'CS', 'CATV', 'SKY', 'BS4K'],
        is_fullhd_channel: bool,
        is_oneseg: bool = False,
    ) -> list[str]:
        """
        QSVEncC・NVEncC・VCEEncC (便宜上 HWEncC と総称) に渡すオプションを組み立てる

        Args:
            quality (QUALITY_TYPES): 映像の品質
            encoder_type (Literal['QSVEncC', 'NVEncC', 'VCEEncC']): エンコーダー (QSVEncC or NVEncC or VCEEncC)
            channel_type (Literal['GR', 'BS', 'CS', 'CATV', 'SKY', 'BS4K']): チャンネルの種類
            is_fullhd_channel (bool): フル HD 放送が実施されているチャンネルかどうか
            is_oneseg (bool): ワンセグサービスかどうか

        Returns:
            list[str]: HWEncC に渡すオプションが連なる配列
        """

        # オプションの入る配列
        options: list[str] = []

        # 入力ストリームの解析時間
        CONFIG = Config()
        if is_oneseg is True:
            # ワンセグは低フレームレートの H.264 で、GOP の途中から受信を開始すると
            # SPS/PPS・IDR の検出まで時間がかかるため、初回から十分な解析量と時間を確保する
            input_probesize = f'{round(3000 + (self._retry_count * 500))}K'
            input_analyze = round(2.5 + (self._retry_count * 0.2), 1)
        elif (
            channel_type == 'BS4K' and
            CONFIG.general.encoder_bs4k_input_analysis_enabled is True and
            CONFIG.general.encoder_bs4k_low_latency is False
        ):
            input_probesize = '8M'
            input_analyze = 3
        elif channel_type == 'BS4K' and CONFIG.general.encoder_bs4k_input_analysis_enabled is True:
            input_probesize = f'{round(CONFIG.general.encoder_bs4k_input_probesize + (self._retry_count * 500))}K'
            input_analyze = round(CONFIG.general.encoder_bs4k_input_analyze + (self._retry_count * 0.2), 1)
        elif channel_type == 'SKY':
            # H.264 入力のスカパー！プレミアムサービスは入力ストリームの解析時間を長めにする
            input_probesize = f'{round(1500 + (self._retry_count * 500))}K'
            input_analyze = round(0.9 + (self._retry_count * 0.2), 1)
        else:
            input_probesize = f'{round(1000 + (self._retry_count * 500))}K'  # リトライ回数に応じて少し増やす
            input_analyze = round(0.7 + (self._retry_count * 0.2), 1)  # リトライ回数に応じて少し増やす

        # 入力
        ## --input-probesize, --input-analyze をつけることで、ストリームの分析時間を短縮できる
        ## 両方つけるのが重要で、--input-analyze だけだとエンコーダーがフリーズすることがある
        options.append(f'--input-format mpegts --input-probesize {input_probesize} --input-analyze {input_analyze}')
        ## BS4K とワンセグ以外では 29.97fps (59.94i) を指定する
        ## BS4K は MPEG-TS/avhw 入力の 59.94p をそのまま読ませ、30fps 品質は VPP で間引く
        ## ワンセグは約 10～15fps の入力 PTS をそのまま読ませる
        is_bs4k_30fps_quality = channel_type == 'BS4K' and '-30fps' in quality
        if channel_type != 'BS4K' and is_oneseg is False:
            options.append('--fps 30000/1001')
        ## 入力を指定する
        options.append('--input -')
        ## VCEEncC の HW デコーダーはエラー耐性が低く TS を扱う用途では不安定なので、SW デコーダーを利用する
        if encoder_type == 'VCEEncC':
            options.append('--avsw')
        ## QSVEncC・NVEncC は HW デコーダーを利用する
        else:
            options.append('--avhw')

        if is_oneseg is True:
            # ワンセグの PCE 依存 AAC を、ブラウザ互換の標準的なステレオ AAC へ正規化する。
            # 受信開始直後に PCE がまだ届いていない AAC フレームは無音に置換して継続する。
            options.append('--audio-codec aac --audio-bitrate 96 --audio-samplerate 48000')
            options.append('--audio-stream :stereo --audio-ignore-decode-error 100 --data-copy timed_id3')
        else:
            # tsreadex -A 1 が分離したデュアルモノを含む、すべての実音声をそのまま出力する
            options.append('--audio-copy --data-copy timed_id3')

        # フラグ
        ## 主に HWEncC の起動を高速化するための設定
        ## max_interleave_delta: mux 時に影響するオプションで、増やしすぎると CM で詰まりがちになる
        ## リトライなしの場合は 500K (0.5秒) に設定し、リトライ回数に応じて 100K (0.1秒) ずつ増やす
        if channel_type == 'BS4K':
            max_interleave_delta = round(CONFIG.general.encoder_bs4k_max_interleave_delta + (self._retry_count * 100))
        else:
            max_interleave_delta = round(500 + (self._retry_count * 100))
        if channel_type != 'BS4K' or CONFIG.general.encoder_bs4k_low_latency is True:
            options.append('-m avioflags:direct -m flush_packets:1 -m max_delay:250000')
            options.append('-m fflags:nobuffer+flush_packets')
        options.append(f'-m max_interleave_delta:{max_interleave_delta}K --output-thread 0')
        if channel_type != 'BS4K' or CONFIG.general.encoder_bs4k_low_latency is True:
            options.append('--lowlatency')
        ## QSVEncC では OpenCL を使用しない場合、無効化することで初期化フェーズを高速化する
        if (
            encoder_type == 'QSVEncC' and
            (is_oneseg is True or self.live_stream.encoding_options.is_24fps_mode_enabled is False)
        ):
            options.append('--disable-opencl')
        ## NVEncC では NVML によるモニタリングと DX11, Vulkan を無効化することで初期化フェーズを高速化する
        if encoder_type == 'NVEncC':
            options.append('--disable-nvml 1 --disable-dx11 --disable-vulkan')
        ## その他の設定
        options.append('--log-level debug')

        # 映像
        ## コーデック
        if QUALITY[quality].is_hevc is True:
            options.append('--codec hevc')  # H.265/HEVC
        else:
            options.append('--codec h264')  # H.264

        ## ビットレート
        ## H.265/HEVC かつ QSVEncC の場合のみ、--qvbr (品質ベース可変ビットレート) モードでエンコードする
        ## それ以外は --vbr (可変ビットレート) モードでエンコードする
        if QUALITY[quality].is_hevc is True and encoder_type == 'QSVEncC':
            options.append(f'--qvbr {QUALITY[quality].video_bitrate} --fallback-rc')
        else:
            options.append(f'--vbr {QUALITY[quality].video_bitrate}')
        options.append(f'--max-bitrate {QUALITY[quality].video_bitrate_max}')

        ## H.265/HEVC の高圧縮化調整
        if QUALITY[quality].is_hevc is True:
            if encoder_type == 'QSVEncC':
                options.append('--qvbr-quality 20 --extbrc --mbbrc --scenario-info game_streaming --tune perceptual')
                options.append('--i-adapt --b-adapt --b-pyramid --weightp --weightb --adapt-ref --adapt-ltr --adapt-cqm')
            elif encoder_type == 'NVEncC':
                # --weightp は過去の GPU 世代で不安定な場合があるので使用しない
                options.append('--qp-min 23:26:30 --lookahead 16 --multipass 2pass-full --bref-mode middle --aq --aq-temporal')

        ## ヘッダ情報制御 (GOP ごとにヘッダを再送する)
        ## VCEEncC ではデフォルトで有効であり、当該オプションは存在しない
        if encoder_type != 'VCEEncC':
            options.append('--repeat-headers')

        ## 品質
        if encoder_type == 'QSVEncC':
            options.append('--quality balanced')
        elif encoder_type == 'NVEncC':
            options.append('--preset default')
        elif encoder_type == 'VCEEncC':
            options.append('--preset balanced')
        if QUALITY[quality].is_hevc is True:
            options.append('--profile main')
        else:
            options.append('--profile high')
        options.append('--dar 16:9')

        ## バンディング軽減のためのオプション (速度低下を鑑みて当面 NVEncC でのみ有効にする)
        if encoder_type == 'NVEncC':
            options.append('--vpp-deband')
        # HEVC 選択時は、HEVC 10bit のデコードに対応したクライアント向けに HEVC 10bit でエンコードし、さらにバンディング耐性を高める
        ## VCEEncC は HEVC 10bit 対応の機種かを判定できないため設定しない
        ## --fallback-bitdepth により、GPU 側が HEVC 10bit 非対応の場合でも 8bit へフォールバックされる
        ## 末尾の -10bit は、HEVC 10bit でのエンコードを試すストリームであることだけを表す
        if QUALITY[quality].is_hevc is True and self.live_stream.encoding_options.is_hevc_10bit_enabled is True:
            options.append('--output-depth 10 --fallback-bitdepth')

        ## 最大 GOP 長 (秒)
        ## 30fps なら ×30 、 60fps なら ×60 された値が --gop-len で使われる
        gop_length_second = self.GOP_LENGTH_SECONDS_H264
        if QUALITY[quality].is_hevc is True:
            ## H.265/HEVC では高圧縮化のため、最大 GOP 長を長くする
            gop_length_second = self.GOP_LENGTH_SECONDS_H265

        # ワンセグはプログレッシブかつ約 10～15fps の VFR で放送されているため、
        ## フレームレートの固定やインターレース解除を行わず、入力 PTS をそのまま維持する。
        if is_oneseg is True:
            options.append(f'--avsync vfr --gop-len {30 if QUALITY[quality].is_hevc is True else 8}')
        ## BS4K は 60p (プログレッシブ) で放送されているので、インターレース解除を行わない
        elif channel_type == "BS4K":
            if is_bs4k_30fps_quality is True:
                options.append('--vpp-decimate cycle=2,drop=1')
                avsync_mode = 'vfr' if CONFIG.general.encoder_bs4k_low_latency is True else 'forcecfr'
                options.append(f'--avsync {avsync_mode} --gop-len {int(gop_length_second * 30)}')
            else:
                avsync_mode = 'vfr' if CONFIG.general.encoder_bs4k_low_latency is True else 'forcecfr'
                options.append(f'--avsync {avsync_mode} --gop-len {int(gop_length_second * 60)}')
        else:
            ## インターレース映像として読み込む
            options.append('--interlace tff')
            ## インターレース解除 (60i → 60p (フレームレート: 60fps))
            ## NVEncC の --vpp-deinterlace bob は品質が悪いので、代わりに --vpp-yadif を使う
            ## NVIDIA GPU は当然ながら Intel の内蔵 GPU よりも性能が高いので、GPU フィルタを使ってもパフォーマンスに問題はないと判断
            ## VCEEncC では --vpp-deinterlace 自体が使えないので、代わりに --vpp-yadif を使う
            if QUALITY[quality].is_60fps is True:
                if encoder_type == 'QSVEncC':
                    options.append('--vpp-deinterlace bob')
                elif encoder_type == 'NVEncC' or encoder_type == 'VCEEncC':
                    options.append('--vpp-yadif mode=bob')
                options.append(f'--avsync vfr --gop-len {int(gop_length_second * 60)}')
            ## インターレース解除 (60i → 30p (フレームレート: 30fps))
            ## NVEncC の --vpp-deinterlace normal は GPU 機種次第では稀に解除漏れのジャギーが入るらしいので、代わりに --vpp-afs を使う
            ## NVIDIA GPU は当然ながら Intel の内蔵 GPU よりも性能が高いので、GPU フィルタを使ってもパフォーマンスに問題はないと判断
            ## VCEEncC では --vpp-deinterlace 自体が使えないので、代わりに --vpp-afs を使う
            else:
                # 24fps モードでは --vpp-afs で 24fps 区間を検出し、24/30p 混合 VFR で出力する
                ## 1080p-60fps では上の bob 分岐を優先するため、この分岐には入らない
                if self.live_stream.encoding_options.is_24fps_mode_enabled is True:
                    options.append('--vpp-afs preset=default,drop=on,smooth=on')
                else:
                    if encoder_type == 'QSVEncC':
                        options.append('--vpp-deinterlace normal')
                    elif encoder_type == 'NVEncC' or encoder_type == 'VCEEncC':
                        options.append('--vpp-afs preset=default')
                options.append(f'--avsync vfr --gop-len {int(gop_length_second * 30)}')

        ## フル HD 放送が行われているチャンネルかつ、指定された品質の解像度が 1440×1080 (1080p) の場合のみ、
        ## 特別に縦解像度を 1920 に変更してフル HD (1920×1080) でエンコードする
        video_width = QUALITY[quality].width
        video_height = QUALITY[quality].height
        if video_width == 1440 and video_height == 1080 and is_fullhd_channel is True:
            video_width = 1920
        options.append(f'--output-res {video_width}x{video_height}')

        # HWEncC 内部の FFmpeg MPEG-TS muxer にも、Anchor 経路と同じ搬送契約を渡す。
        if self.IsStreamAnchorEnabled() is True:
            muxrate = ResolveKonomiTVBS4KAdvancedLiveMuxrate(QUALITY[quality].video_bitrate_max)
            options.append(f'-m muxrate:{muxrate} -m pcr_period:20')

        # 出力
        options.append('--output-format mpegts')  # MPEG-TS 出力ということを明示
        options.append('--output -')  # 標準出力へ出力

        # オプションをスペースで区切って配列にする
        result: list[str] = []
        for option in options:
            result += option.split(' ')

        return result


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


    def KillSubprocesses(self, *targets: tuple[str, asyncio.subprocess.Process | None]) -> list[tuple[str, asyncio.subprocess.Process]]:
        """
        起動済みのサブプロセス群へまとめて kill を送信する (このメソッドは一切 await しない)
        先に全プロセスへの kill を済ませておけば、後続の wait 中に再キャンセルされても SIGKILL は全プロセスへ送信済みになる

        Args:
            *targets (tuple[str, asyncio.subprocess.Process | None]): (ログ識別用のサブプロセス名, 終了させるプロセス) の組。
                未起動 (None) または終了済みのプロセスは無視する

        Returns:
            list[tuple[str, asyncio.subprocess.Process]]: kill に成功し、終了待機が必要なプロセスの組
        """

        killed: list[tuple[str, asyncio.subprocess.Process]] = []
        for name, process in targets:
            # 未起動 (TS Codec Bridge 未使用など) や既に終了済みのプロセスへは不要な kill を行わない
            if process is None or process.returncode is not None:
                continue
            try:
                process.kill()
            except ProcessLookupError:
                # kill 直前にプロセスが終了していた正常な競合
                continue
            except OSError as ex:
                # kill に失敗しても他のプロセスの回収を中断させない
                logging.debug(f'{self.live_stream.log_prefix} Failed to kill {name} subprocess:', exc_info=ex)
                continue
            killed.append((name, process))
        return killed


    async def WaitSubprocesses(self, killed: list[tuple[str, asyncio.subprocess.Process]]) -> None:
        """
        KillSubprocesses() で kill 済みのプロセスの終了を順に待機し、ゾンビプロセス化を防ぐ

        Args:
            killed (list[tuple[str, asyncio.subprocess.Process]]): (ログ識別用のサブプロセス名, kill 済みプロセス) の組
        """

        ## asyncio.CancelledError (BaseException) はここでは捕捉せず、LiveStream.connect() からの
        ## キャンセルを妨げないよう呼び出し元へそのまま伝播させる (この時点で全プロセスへの kill は完了済み)
        for name, process in killed:
            try:
                await process.wait()
            except (ProcessLookupError, OSError) as ex:
                # 終了待機に失敗しても他のプロセスの回収を中断させない
                logging.debug(f'{self.live_stream.log_prefix} Failed to wait for {name} subprocess termination:', exc_info=ex)


    async def TerminateSubprocesses(self, *targets: tuple[str, asyncio.subprocess.Process | None]) -> None:
        """
        起動済みのサブプロセス群をすべて kill してから、それぞれの終了を待機する
        起動失敗時など、kill 後に他の解放処理を挟む必要がない経路から呼び出す
        (run() の終了処理のように kill と wait の間に解放処理を挟む必要がある経路では、
        KillSubprocesses() / WaitSubprocesses() を直接呼び出す)

        Args:
            *targets (tuple[str, asyncio.subprocess.Process | None]): (ログ識別用のサブプロセス名, 終了させるプロセス) の組。
                未起動 (None) または終了済みのプロセスは無視する
        """

        await self.WaitSubprocesses(self.KillSubprocesses(*targets))


    async def run(self) -> None:
        """
        エンコードタスクを実行する
        """

        CONFIG = Config()

        # メタデータの取得元とは独立した、ライブ放送波の実際の受信元を取得する
        LIVE_STREAM_BACKEND = CONFIG.general.live_stream_backend

        # まだ Standby になっていなければ、ステータスを Standby に設定
        # 基本はエンコードタスクの呼び出し元である self.live_stream.connect() の方で Standby に設定されるが、再起動の場合はそこを経由しないため必要
        if not (self.live_stream.getStatus().status == 'Standby' and self.live_stream.getStatus().detail == 'エンコードタスクを起動しています…'):
            self.live_stream.setStatus('Standby', 'エンコードタスクを起動しています…')

        # チャンネル情報からサービス ID とネットワーク ID を取得する
        channel = cast(Channel, await Channel.filter(display_channel_id=self.live_stream.display_channel_id).first())

        # 3つのバックエンド構成のどれで動作しているかと、実際に選局するサービスを明示する
        ## 接続 URL は認証情報やローカル環境情報を含む可能性があるためログへ出力しない。
        logging.info(
            f'{self.live_stream.log_prefix} Backend: Metadata={CONFIG.general.backend} / Live={LIVE_STREAM_BACKEND}'
        )
        logging.info(
            f'{self.live_stream.log_prefix} Source: {LIVE_STREAM_BACKEND} / NID: {channel.network_id} / '
            f'TSID: {channel.transport_stream_id} / SID: {channel.service_id}'
        )

        # エンコーダーの種類を取得
        ENCODER_TYPE = GetEncoderForLiveChannel(self.live_stream.display_channel_id)

        # Stream Anchor は映像 access unit と対応付けるため、通常 API の映像付きライブだけで有効にする。
        # Compatibility API とラジオは従来 TS 経路を維持する。
        stream_anchor_enabled = (
            self.IsStreamAnchorEnabled() is True and
            channel.is_radiochannel is False
        )
        codec_bridge_required = self.IsTSCodecBridgeRequired(
            is_radiochannel = channel.is_radiochannel,
        )
        bridge_required = stream_anchor_enabled or codec_bridge_required
        bridge_path: str | None = None
        if bridge_required is True:
            bridge_path = LIBRARY_PATH[self.TS_CODEC_BRIDGE_LIBRARY_PATH_KEY]
            if TSCodecBridgeRuntimeVerifier.isAvailable(bridge_path) is False:
                self.live_stream.setStatus(
                    'Offline',
                    'TS Codec Bridge を利用できないためライブ配信を開始できません。(E-17)',
                )
                self.live_stream.disconnectAll()
                return
        stream_anchor_generation_id = self.ResolveStreamAnchorGenerationID(stream_anchor_enabled)

        # 現在の番組情報を取得する
        program_present = (await channel.getCurrentAndNextProgram())[0]
        if program_present is not None:
            logging.info(f'{self.live_stream.log_prefix} Title: {program_present.title}')
        else:
            logging.info(f'{self.live_stream.log_prefix} Title: 番組情報がありません')

        # PSI/SI データアーカイバーを初期化
        ## psisiarc は API リクエストがある度に都度起動される
        self.live_stream.psi_data_archiver = LivePSIDataArchiver(channel.service_id)

        # ***** tsreadex プロセスの作成と実行 *****

        # tsreadex のオプション
        ## 放送波の前処理を行い、エンコードを安定させるツール
        ## オプション内容は https://github.com/xtne6f/tsreadex を参照
        tsreadex_options = [
            # 取り除く TS パケットの10進数の PID
            ## EIT の PID を指定
            '-x', '18/38/39',
            # 特定サービスのみを選択して出力するフィルタを有効にする
            ## 有効にすると、特定のストリームのみ PID を固定して出力される
            ## 視聴対象のチャンネルのサービス ID を指定する
            '-n', f'{channel.service_id}' if CONFIG.tv.debug_mode_ts_path is None else '-1',
            # PMT 上に実在する音声ストリームをすべて保持する
            ## 無音補完・mono stereo 化・dual mono 分離・downmix は行わない
            '-A', '1',
            # 字幕ストリームが常に存在する状態にする
            ## ストリームが存在しない場合、PMT の項目が補われて出力される
            ## 実際の字幕データが現れない場合に5秒ごとに非表示の適当なデータを挿入する
            '-c', '5',
            # 文字スーパーストリームが常に存在する状態にする
            ## ストリームが存在しない場合、PMT の項目が補われて出力される
            '-u', '1',
            # 字幕と文字スーパーを aribb24.js が解釈できる ID3 timed-metadata に変換する
            ## +4: FFmpeg のバグを打ち消すため、変換後のストリームに規格外の5バイトのデータを追加する
            ## +8: FFmpeg のエラーを防ぐため、変換後のストリームの PTS が単調増加となるように調整する
            ## 以前は Linux 版 HWEncC が FFmpeg 4.4 系の共有ライブラリに依存していたため +4 を付与していたが、
            ## 現在の Linux 版 HWEncC は FFmpeg 8 系を静的リンクした最新版へ更新したため不要になった
            ## +4 を残すと FFmpeg 6.1 以降では字幕が表示されなくなるため、常に +8 のみを付与する
            '-d', '9',
        ]

        # Encoder 前の source marker は実行ごとの generation ID で識別し、
        # Encoder 後に TS Codec Bridge が実際の映像 AU 時刻へ確定する。
        if stream_anchor_generation_id is not None:
            tsreadex_options += ['-g', str(stream_anchor_generation_id)]

        if CONFIG.tv.debug_mode_ts_path is None:
            # 通常は標準入力を指定
            tsreadex_options.append('-')
        else:
            # デバッグモード: 指定された TS ファイルを読み込む
            ## 読み込み速度を 2350KB/s (18.8Mbps) に制限
            ## 1倍速に近い値だが、TS のビットレートはチャンネルや番組、シーンによって変動するため完全な1倍速にはならない
            tsreadex_options += [
                '-l', '2350',
                CONFIG.tv.debug_mode_ts_path
            ]

        # tsreadex のパイプ作成・プロセス起動の途中で失敗しても回収できるよう、すべて事前初期化する
        ## 各パイプは子プロセスへ引き渡した後に親プロセス側で閉じ、二重 close を防ぐため None に戻す
        tsreadex: asyncio.subprocess.Process | None = None
        tsreadex_read_pipe: int | None = None
        tsreadex_write_pipe: int | None = None
        try:
            # tsreadex の読み込み用パイプと書き込み用パイプを作成
            tsreadex_read_pipe, tsreadex_write_pipe = os.pipe()

            # tsreadex のプロセスを非同期で作成・実行
            try:
                tsreadex = await asyncio.subprocess.create_subprocess_exec(
                    *[LIBRARY_PATH['tsreadex'], *tsreadex_options],
                    stdin = asyncio.subprocess.PIPE,  # 受信した放送波を書き込む
                    stdout = tsreadex_write_pipe,  # エンコーダーに繋ぐ
                    stderr = asyncio.subprocess.DEVNULL,  # 利用しない
                )
            finally:
                # tsreadex の書き込み用パイプは子プロセスに渡したので、親プロセス側ではクローズする
                if tsreadex_write_pipe is not None:
                    os.close(tsreadex_write_pipe)
                    tsreadex_write_pipe = None
        except BaseException:
            # パイプ作成・tsreadex 起動中の例外やキャンセルでも、生成済みプロセスを先に停止する
            killed = self.KillSubprocesses(('tsreadex', tsreadex))
            # close 自体の失敗で後続の状態回復を妨げないよう、残っている親プロセス側 FD を個別に回収する
            for pipe_name, pipe in (
                ('tsreadex read', tsreadex_read_pipe),
                ('tsreadex write', tsreadex_write_pipe),
            ):
                if pipe is not None:
                    try:
                        os.close(pipe)
                    except OSError as ex:
                        logging.debug(f'{self.live_stream.log_prefix} Failed to close {pipe_name} pipe:', exc_info=ex)
            tsreadex_read_pipe = tsreadex_write_pipe = None
            # Standby のまま残すと次回接続でタスクを再起動できないため、共有資源を解放して Offline へ戻す
            self.live_stream.disconnectAll()
            if self.live_stream.psi_data_archiver is not None:
                self.live_stream.psi_data_archiver.destroy()
                self.live_stream.psi_data_archiver = None
            self.live_stream.setStatus('Offline', 'ライブストリームの処理中に予期しないエラーが発生しました。(E-18)')
            # tsreadex が起動済みだった場合は、元の例外を再送出する前に終了を確認する
            await self.WaitSubprocesses(killed)
            raise

        # ここまで到達した時点で tsreadex は起動済み
        assert tsreadex is not None

        # エンコーダー・Bridge の生成フェーズで例外やキャンセル (オプション構築・os.pipe・サブプロセス生成の失敗など) が
        # 発生しても、生成済みの tsreadex / bridge / encoder と親プロセス側パイプが残留しないよう、生成フェーズ全体を回収スコープで覆う
        bridge: asyncio.subprocess.Process | None = None
        encoder: asyncio.subprocess.Process | None = None
        # Bridge 用パイプも生成直後から外側の回収スコープで管理するため、None で事前初期化する
        ## 子プロセスへ引き渡してクローズした時点で None を代入し、外側の except での二重 close を防ぐ
        bridge_read_pipe: int | None = None
        bridge_write_pipe: int | None = None
        try:
            # ***** エンコーダープロセスの作成と実行 *****

            # エンコーダーの起動には時間がかかるので、先にエンコーダーを起動しておいた後、あとからチューナーを起動する
            # チューナーの起動後にエンコーダー (正確には tsreadex) に受信した放送波が書き込まれる
            # チューナーの起動にも時間がかかるが、エンコーダーの起動は非同期なのに対し、チューナーの起動は EDCB の場合は同期的

            # フル HD 放送が行われているチャンネルかを取得
            is_fullhd_channel = (
                channel.is_oneseg is False and
                self.isFullHDChannel(channel.network_id, channel.service_id)
            )

            ## ラジオチャンネルでは HW エンコードの意味がないため、FFmpeg に固定する
            if channel.is_radiochannel is True:
                ENCODER_TYPE = 'FFmpeg'

            # Anchor またはcodec変換でBridgeが必要な時だけ、Encoder 出力と Bridge 入力を OS pipe で直結する。
            # Python で TS を往復させず、最終的な Bridge stdout だけを配信側が読む。
            encoder_stdout: int = asyncio.subprocess.PIPE
            if bridge_required is True:
                assert bridge_path is not None
                bridge_read_pipe, bridge_write_pipe = os.pipe()
                bridge_options = self.BuildTSCodecBridgeOptions(
                    is_radiochannel = channel.is_radiochannel,
                )
                logging.info(
                    f'{self.live_stream.log_prefix} TS Codec Bridge Commands:\n'
                    f'{bridge_path} {" ".join(bridge_options)}'
                )
                try:
                    bridge = await asyncio.subprocess.create_subprocess_exec(
                        bridge_path,
                        *bridge_options,
                        stdin = bridge_read_pipe,
                        stdout = asyncio.subprocess.PIPE,
                        stderr = asyncio.subprocess.PIPE,
                    )
                    TSCodecBridgeRuntimeVerifier.recordProcessStart('live')
                    encoder_stdout = bridge_write_pipe
                except BaseException:
                    # パイプと生成済みプロセスの回収は、生成フェーズ全体を覆う外側の except で一括して行う
                    raise
                finally:
                    # Bridge の読み込み用パイプは子プロセスに渡したので、親プロセス側ではクローズする
                    ## 外側の except での再クローズを防ぐため、クローズ後は None を代入する
                    if bridge_read_pipe is not None:
                        os.close(bridge_read_pipe)
                        bridge_read_pipe = None

            # 現 main の公開設定 FFmpeg / QSV / NVENC / AMF は、すべて同梱 FFmpeg 8 で実行する。
            ffmpeg8_encoder_type = ENCODER_TYPE
            encoder_executable = RecordedPlaybackBackend.getExecutable(ffmpeg8_encoder_type)
            encoder_environment = RecordedPlaybackBackend.getEnvironment(ffmpeg8_encoder_type)

            # FFmpeg software backend
            if ENCODER_TYPE == 'FFmpeg':

                # オプションを取得
                # ラジオチャンネルかどうかでエンコードオプションを切り替え
                if channel.is_radiochannel is True:
                    encoder_options = self.buildFFmpegOptionsForRadio()
                else:
                    encoder_options = self.buildFFmpegOptions(
                        self.live_stream.quality, channel.type, is_fullhd_channel, channel.is_oneseg,
                    )
                logging.info(
                    f'{self.live_stream.log_prefix} FFmpeg 8 Commands:\n'
                    f'{encoder_executable} {" ".join(encoder_options)}'
                )

                # エンコーダープロセスを非同期で作成・実行
                try:
                    encoder = await asyncio.subprocess.create_subprocess_exec(
                        encoder_executable,
                        *encoder_options,
                        stdin = tsreadex_read_pipe,  # tsreadex からの入力
                        stdout = encoder_stdout,  # Bridge またはストリーム出力へ接続
                        stderr = asyncio.subprocess.PIPE,  # ログ出力
                        env = encoder_environment,
                    )
                finally:
                    # tsreadex の読み込み用パイプは子プロセスに渡したので、親プロセス側ではクローズする
                    ## 起動失敗時の再クローズを防ぐため、クローズ後は None を代入する
                    if tsreadex_read_pipe is not None:
                        os.close(tsreadex_read_pipe)
                        tsreadex_read_pipe = None
                    # Bridge の書き込み用パイプも子プロセスに渡したので、親プロセス側ではクローズする
                    if bridge_write_pipe is not None:
                        os.close(bridge_write_pipe)
                        bridge_write_pipe = None

            # FFmpeg 8 hardware backend
            else:

                # オプションを取得
                hw_encoder_type = ENCODER_TYPE
                encoder_options = self.buildFFmpeg8HardwareOptions(
                    self.live_stream.quality, hw_encoder_type, channel.type, is_fullhd_channel, channel.is_oneseg,
                )
                logging.info(
                    f'{self.live_stream.log_prefix} FFmpeg 8 ({ENCODER_TYPE}) Commands:\n'
                    f'{encoder_executable} {" ".join(encoder_options)}'
                )

                # エンコーダープロセスを非同期で作成・実行
                try:
                    encoder = await asyncio.subprocess.create_subprocess_exec(
                        encoder_executable,
                        *encoder_options,
                        stdin = tsreadex_read_pipe,  # tsreadex からの入力
                        stdout = encoder_stdout,  # Bridge またはストリーム出力へ接続
                        stderr = asyncio.subprocess.PIPE,  # ログ出力
                        env = encoder_environment,
                    )
                finally:
                    # tsreadex の読み込み用パイプは子プロセスに渡したので、親プロセス側ではクローズする
                    ## 起動失敗時の再クローズを防ぐため、クローズ後は None を代入する
                    if tsreadex_read_pipe is not None:
                        os.close(tsreadex_read_pipe)
                        tsreadex_read_pipe = None
                    # Bridge の書き込み用パイプも子プロセスに渡したので、親プロセス側ではクローズする
                    if bridge_write_pipe is not None:
                        os.close(bridge_write_pipe)
                        bridge_write_pipe = None

            pipeline_stdout = bridge.stdout if bridge is not None else encoder.stdout
            assert pipeline_stdout is not None

        except BaseException:
            # 生成フェーズ途中の例外・キャンセルでは、まず生成済みの全プロセスへ kill を送信する (この段階では await しない)
            killed = self.KillSubprocesses(('tsreadex', tsreadex), ('encoder', encoder), ('bridge', bridge))
            # 終了待機の停滞で解放処理がスキップされないよう、wait へ入る前に残っているパイプをすべて閉じる
            ## 子プロセスへ引き渡されてクローズ済みのパイプは None になっている
            for pipe in (tsreadex_read_pipe, bridge_read_pipe, bridge_write_pipe):
                if pipe is not None:
                    os.close(pipe)
            tsreadex_read_pipe = bridge_read_pipe = bridge_write_pipe = None
            # クライアント切断とアーカイバー破棄も wait 前に済ませる
            self.live_stream.disconnectAll()
            if self.live_stream.psi_data_archiver is not None:
                self.live_stream.psi_data_archiver.destroy()
                self.live_stream.psi_data_archiver = None
            # Standby のまま例外を投げると LiveStream.connect() が次回接続でエンコードタスクを起動せず
            # 配信不能に陥るため、主要資源の解放後に Offline へ遷移させて再試行可能にする
            self.live_stream.setStatus('Offline', 'ライブストリームの処理中に予期しないエラーが発生しました。(E-18)')
            # kill 済みプロセスの終了を待機してから再送出する
            await self.WaitSubprocesses(killed)
            raise

        # ここまで到達した時点で tsreadex / encoder は起動済み (bridge は未使用の場合 None)
        assert encoder is not None

        # ***** チューナーの起動と接続 *****

        # エンコードタスクが稼働中かどうか
        is_running: bool = True

        # 放送波の MPEG2-TS を受信する StreamReader
        stream_reader: asyncio.StreamReader | PipeStreamReader | aiohttp.StreamReader | None = None

        # Mirakurun の aiohttp セッション (EDCB バックエンド利用時は常に None)
        response: aiohttp.ClientResponse | None = None
        session: aiohttp.ClientSession | None = None

        # 実行中の非同期実行タスクへの参照を保持しておく
        ## run() の実行が完了するまで、ガベージコレクタにより非同期実行タスクが勝手に破棄されることを防ぐ
        ## ref: https://docs.astral.sh/ruff/rules/asyncio-dangling-task/
        background_tasks: set[asyncio.Task[None]] = set()

        # 想定外例外による脱出かどうか
        ## finally で主要資源の解放後に Offline 遷移と EDCB チューナー解放を行うためのフラグ
        ## (CancelledError によるチャンネル切り替えでは handoff との競合を避けるためこれらを行わない)
        unexpected_error = False

        # チューナー起動フェーズから Controller 実行までを CancelledError から保護する
        # チャンネル切り替え時に LiveStream.connect() からこのタスクがキャンセルされると、チューナー起動フェーズで
        # await している箇所 (EDCBTuner.setChannel() / EDCBTuner.connect() など) で CancelledError が発生する可能性がある
        # CancelledError をキャッチしないとエンコーダープロセスの終了処理に到達せず、プロセスがリークしてしまう
        try:
            # Mirakurun バックエンド
            if LIVE_STREAM_BACKEND == 'Mirakurun':

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
                except (TimeoutError, aiohttp.ClientConnectorError):

                    # 番組名に「放送休止」などが入っていれば停波によるものとみなし、そうでないならチューナーへの接続に失敗したものとする
                    if program_present is None or program_present.isOffTheAirProgram():
                        self.live_stream.setStatus('Offline', 'この時間は放送を休止しています。(E-01M)')
                    else:
                        self.live_stream.setStatus('Offline', 'チューナーへの接続に失敗しました。チューナー側に何らかの問題があるかもしれません。(E-01M)')

                    # すべての視聴中クライアントのライブストリームへの接続を切断する
                    self.live_stream.disconnectAll()

                    # PSI/SI データアーカイバーを終了・破棄する
                    if self.live_stream.psi_data_archiver is not None:
                        self.live_stream.psi_data_archiver.destroy()
                        self.live_stream.psi_data_archiver = None

                    # 明示的にエンコーダープロセスを終了する
                    ## エンコーダープロセスはチューナー接続よりも前に起動されているため、ここで終了しないとプロセスがリークする
                    await self.TerminateSubprocesses(('tsreadex', tsreadex), ('encoder', encoder), ('bridge', bridge))

                    # エンコードタスクを停止する
                    await session.close()
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

                    # チューナーを閉じる
                    await self.live_stream.tuner.close(self.live_stream.live_stream_id)

                    # すべての視聴中クライアントのライブストリームへの接続を切断する
                    self.live_stream.disconnectAll()

                    # PSI/SI データアーカイバーを終了・破棄する
                    if self.live_stream.psi_data_archiver is not None:
                        self.live_stream.psi_data_archiver.destroy()
                        self.live_stream.psi_data_archiver = None

                    # 明示的にエンコーダープロセスを終了する
                    ## エンコーダープロセスはチューナー接続よりも前に起動されているため、ここで終了しないとプロセスがリークする
                    await self.TerminateSubprocesses(('tsreadex', tsreadex), ('encoder', encoder), ('bridge', bridge))

                    # エンコードタスクを停止する
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

                    # チューナーを閉じる
                    await self.live_stream.tuner.close(self.live_stream.live_stream_id)

                    # すべての視聴中クライアントのライブストリームへの接続を切断する
                    self.live_stream.disconnectAll()

                    # PSI/SI データアーカイバーを終了・破棄する
                    if self.live_stream.psi_data_archiver is not None:
                        self.live_stream.psi_data_archiver.destroy()
                        self.live_stream.psi_data_archiver = None

                    # 明示的にエンコーダープロセスを終了する
                    ## エンコーダープロセスはチューナー接続よりも前に起動されているため、ここで終了しないとプロセスがリークする
                    await self.TerminateSubprocesses(('tsreadex', tsreadex), ('encoder', encoder), ('bridge', bridge))

                    # エンコードタスクを停止する
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

            async def Reader() -> None:
                nonlocal tuner_ts_read_at

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
                                background_tasks.add(asyncio.create_task(self.live_stream.psi_data_archiver.pushTSPacketData(chunk)))

                            # BS4K ライブ開始直後の不安定な TS はエンコーダーへ渡さず破棄する
                            if startup_discard_until > 0 and time.monotonic() < startup_discard_until:
                                if is_running is False or tsreadex.returncode is not None or encoder.returncode is not None:
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
                        if is_running is False or tsreadex.returncode is not None or encoder.returncode is not None:
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
            background_tasks.add(asyncio.create_task(Reader()))

            # ***** tsreadex・エンコーダーからの出力の読み込み → ライブストリームへの書き込み *****

            # エンコーダーの出力のチャンクが積み増されていくバッファ
            chunk_buffer: bytearray = bytearray()

            # チャンクの最終書き込み時刻 (単調増加時間)
            ## 単に時刻を比較する用途でしか使わないので、time.monotonic() から取得した単調増加時間が入る
            ## Unix Time とかではないので注意
            chunk_written_at: float = 0

            # Writer の排他ロック
            ## タスク間共有の変数を Writer() タスクと SubWriter() タスクの両方から読み書きするため、
            ## chunk_buffer / chunk_written_at にアクセスする際は排他ロックを掛けておく必要がある
            ## そうしないと稀にパケロスするらしく、ブラウザ側で突如再生できなくなることがある
            writer_lock = asyncio.Lock()

            async def Writer() -> None:

                nonlocal chunk_buffer, chunk_written_at, writer_lock

                while True:
                    try:

                        # 最終 pipeline (Bridge 有効時は Bridge、無効時は Encoder) から出力を読み取る
                        ## TS パケットのサイズが 188 bytes なので、1回の readexactly() で 188 bytes ずつ読み取る
                        ## read() ではなく厳密な readexactly() を使わないとぴったり 188 bytes にならない場合がある
                        chunk = await pipeline_stdout.readexactly(ts.PACKET_SIZE)

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
                    if is_running is False or tsreadex.returncode is not None or encoder.returncode is not None:
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
                    if is_running is False or tsreadex.returncode is not None or encoder.returncode is not None:
                        break

            # タスクを非同期で実行
            background_tasks.add(asyncio.create_task(Writer()))
            background_tasks.add(asyncio.create_task(SubWriter()))

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

                    # エンコード進捗のログだったら、正規表現で余計なゴミを取り除く
                    ## HWEncC は内部で使われている FFmpeg 側の大量に出るデバッグログと衝突してログがごちゃまぜになりがち…
                    ## FFmpeg 側のログ（ゴミ）と完全に混ざっていると完全に除去できずに frames: の数値が桁が飛んだような出力になるけどご愛嬌…
                    match1 = re.fullmatch(r'^.*?([1-9][0-9]+ frames: [0-9\.]+ fps, [0-9]+ kb/s, GPU [0-9]+%, VE [0-9]+%, VD [0-9]+%)$', line)
                    match2 = re.fullmatch(r'^.*?([1-9][0-9]+ frames: [0-9\.]+ fps, [0-9]+ kb/s, GPU [0-9]+%, VD [0-9]+%)$', line)
                    match3 = re.fullmatch(r'^.*?([1-9][0-9]+ frames: [0-9\.]+ fps, [0-9]+ kb/s)$', line)
                    if match1 is not None:
                        line = match1.groups()[0]
                    elif match2 is not None:
                        line = match2.groups()[0]
                    elif match3 is not None:
                        line = match3.groups()[0]

                    # 山ほど出力されるメッセージと空行をログから除外
                    ## 元は "Delay between the first packet and last packet in the muxing queue is xxxxxx > 1: forcing output" と
                    ## "removing 2 bytes from input bitstream not read by decoder." という2つのメッセージで、実害はない
                    ## FFmpeg と HWEncC のログが衝突して行の先頭が欠けることがあるので、できるだけ多く弾けるように部分一致にしている
                    if (('removing 2 bytes from input bitstream not read by decoder.' not in line) and
                        ('Delay between the' not in line) and
                        ('[h264_metadata' not in line) and
                        ('[hevc_metadata' not in line) and
                        ('packet in the muxing queue' not in line) and ('ing output' not in line) and
                        ('ng output' != line) and ('g output' != line) and (' output' != line) and ('output' != line) and
                        ('utput' != line) and ('tput' != line) and ('put' != line) and ('ut' != line) and ('t' != line) and
                        ('' != line)):

                        # ログリストに行単位で追加
                        lines.append(line)

                        # ストリーム関連のログを表示
                        ## エンコーダーのログ出力が有効なら、ストリーム関連に限らずすべてのログを出力する
                        if 'Stream #0:' in line or CONFIG.general.debug_encoder is True:
                            logging.debug(f'{self.live_stream.log_prefix} [{ENCODER_TYPE}] ' + line)

                        # エンコーダーのログ出力が有効なら、エンコーダーのログファイルに書き込む
                        if CONFIG.general.debug_encoder is True and encoder_log is not None:
                            await encoder_log.write(line.strip('\r\n') + '\n')
                            await encoder_log.flush()

                    # ライブストリームのステータスを取得
                    live_stream_status = self.live_stream.getStatus()

                    # 全 backend が FFmpeg 8 なので、同じ進捗形式で状態を更新する。
                    if live_stream_status.status == 'Standby':
                        if 'arib parser was created' in line or 'Invalid frame dimensions 0x0.' in line:
                            self.live_stream.setStatus('Standby', 'エンコードを開始しています…')
                        elif 'frame=    1 fps=0.0 q=0.0' in line or 'size=       0kB time=00:00' in line:
                            self.live_stream.setStatus('Standby', 'バッファリングしています…')
                        elif 'frame=' in line or 'bitrate=' in line:
                            self.live_stream.setStatus('ONAir', 'ライブストリームは ONAir です。')
                            if self._retry_count > 0:
                                self._retry_count = 0

                    # 全 backend の FFmpeg 8 ログを同じ経路で診断する。
                    if 'Stream map \'0:v:0\' matches no streams.' in line:
                        if program_present is None or program_present.isOffTheAirProgram():
                            self.live_stream.setStatus('Offline', 'この時間は放送を休止しています。(E-04F)')
                        else:
                            self.live_stream.setStatus('Offline', 'チューナーからの放送波の受信に失敗したため、エンコードを開始できません。(E-04F)')
                    elif ENCODER_TYPE == 'NVENC' and (
                        'No capable devices found' in line or 'Cannot load libcuda' in line
                    ):
                        self.live_stream.setStatus('Offline', 'お使いの PC 環境は NVENC エンコーダーに対応していません。(E-09HN)')
                    elif ENCODER_TYPE == 'QSV' and (
                        'Error initializing an MFX session' in line or 'No device available' in line
                    ):
                        self.live_stream.setStatus('Offline', 'お使いの PC 環境は QSV エンコーダーに対応していません。(E-07HQ)')
                    elif ENCODER_TYPE == 'AMF' and 'AMF failed to initialise' in line:
                        self.live_stream.setStatus('Offline', 'お使いの PC 環境は AMF エンコーダーに対応していません。(E-10HV)')
                    elif 'Conversion failed!' in line:
                        result = self.live_stream.setStatus('Restart', 'エンコード中に予期しないエラーが発生しました。エンコードタスクを再起動しています… (ER-01F)')
                        if result is True:
                            for log in lines[-51:-1]:
                                logging.warning(log)

                    # エンコードタスクが終了しているか既にエンコーダープロセスが終了していたら、タスクを終了
                    if is_running is False or tsreadex.returncode is not None or encoder.returncode is not None:
                        break

                # タスクを終える前にエンコーダーのログファイルを閉じる
                if CONFIG.general.debug_encoder is True and encoder_log is not None:
                    await encoder_log.close()

            # タスクを非同期で実行
            background_tasks.add(asyncio.create_task(EncoderObServer()))

            # Bridge の stderr を読み続け、pipe 満杯による停止を防ぎつつ失敗理由を保持する。
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
                background_tasks.add(asyncio.create_task(BridgeObserver()))

            # ***** エンコードタスク全体の制御 *****

            async def Controller() -> None:

                # 1つ上のスコープ (Enclosing Scope) の変数を書き替えるために必要
                # ref: https://excel-ubara.com/python/python014.html#sec04
                nonlocal lines, program_present

                while True:

                    # ライブストリームのステータスを取得
                    live_stream_status = self.live_stream.getStatus()

                    # 現在放送中の番組が終了した際に program_present に保存している現在の番組情報を新しいものに更新する
                    # TODO: 番組情報のない時間帯から番組情報のある時間帯に移行する場合の処理が考慮されていない
                    if program_present is not None and time.time() > program_present.end_time.timestamp():

                        # 新しい現在放送中の番組情報を取得する
                        program_following = (await channel.getCurrentAndNextProgram())[0]
                        if program_following is not None:

                            # 現在の番組のタイトルをログに出力
                            ## TODO: 番組の解像度が変わった際にエンコーダーがクラッシュorフリーズする可能性があるが、
                            ## その場合はここでエンコードタスクを再起動させる必要があるかも
                            logging.info(f'{self.live_stream.log_prefix} Title: {program_following.title}')

                        program_present = program_following
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
                        self.ENCODER_TS_READ_TIMEOUT_ONAIR_VCEENCC if ENCODER_TYPE == 'AMF' else self.ENCODER_TS_READ_TIMEOUT_ONAIR
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

                            # エンコーダーのログを表示 (FFmpeg は最後の50行、HWEncC は最後の150行を表示)
                            if result is True:
                                if ENCODER_TYPE == 'FFmpeg':
                                    for log in lines[-51:-1]:
                                        logging.warning(log)
                                else:
                                    for log in lines[-151:-1]:
                                        logging.warning(log)

                    # チューナーとの接続が切断された場合
                    ## ref: https://stackoverflow.com/a/45251241/17124142
                    if ((LIVE_STREAM_BACKEND == 'Mirakurun' and response is not None and response.closed is True) or
                        (LIVE_STREAM_BACKEND == 'EDCB' and self.live_stream.tuner is not None and self.live_stream.tuner.isDisconnected() is True)):

                        # エンコードタスクを再起動
                        self.live_stream.setStatus('Restart', 'チューナーとの接続が切断されました。エンコードタスクを再起動しています… (ER-05)')

                    # エンコーダーが意図せず終了した場合
                    if encoder.returncode is not None:

                        # 複数 GPU が搭載されていてかつ片方のみ H.265/HEVC でのエンコードに対応している環境も考えられるので、
                        # H.265/HEVC でのエンコードに非対応かは実際にエンコーダーが落ちた後に確認する
                        # もし H.265/HEVC 非対応なのが原因で落ちていた場合は復帰の見込みはないので、エンコードタスクを停止する
                        # 基本的にこれらのエラーでリトライが発生することはないので、初回のみチェックする (偽陽性を減らす意味合いもある)
                        if self._retry_count == 0:
                            for line in lines:
                                # QSV: H.265/HEVC でのエンコードに非対応の環境
                                if ENCODER_TYPE == 'QSV' and 'HEVC encoding is not supported on current platform.' in line:
                                    self.live_stream.setStatus('Offline', 'お使いの Intel GPU は H.265/HEVC でのエンコードに対応していません。(E-14HQ)')
                                    break
                                # NVENC: H.265/HEVC でのエンコードに非対応の環境
                                elif ENCODER_TYPE == 'NVENC' and 'does not support H.265/HEVC encoding.' in line:
                                    # 他の行に available for encode. という文字列が含まれている場合は除外
                                    available_for_encode = False
                                    for line2 in lines:
                                        if 'available for encode.' in line2:
                                            available_for_encode = True
                                            break
                                    if not available_for_encode:
                                        self.live_stream.setStatus('Offline', 'お使いの NVIDIA GPU は H.265/HEVC でのエンコードに対応していません。(E-15HN)')
                                        break
                                # AMF: H.265/HEVC でのエンコードに非対応の環境
                                elif ENCODER_TYPE == 'AMF' and 'HW Acceleration of H.265/HEVC is not supported on this platform.' in line:
                                    self.live_stream.setStatus('Offline', 'お使いの AMD GPU は H.265/HEVC でのエンコードに対応していません。(E-16HV)')
                                    break

                        # それ以外なら、エンコーダーの再起動で復帰できる可能性があるのでエンコードタスクを再起動する
                        if self.live_stream.getStatus().status != 'Offline':

                            # エンコードタスクを再起動
                            result = self.live_stream.setStatus('Restart', 'エンコーダーが強制終了されました。エンコードタスクを再起動しています… (ER-06)')

                            # エンコーダーのログを表示 (FFmpeg は最後の50行、HWEncC は最後の150行を表示)
                            if result is True:
                                if ENCODER_TYPE == 'FFmpeg':
                                    for log in lines[-51:-1]:
                                        logging.warning(log)
                                else:
                                    for log in lines[-151:-1]:
                                        logging.warning(log)

                        # エンコーダーが既に終了しているため、後続の異常検出処理を実行する意味がない
                        # この時点でステータスは Offline か Restart のいずれかに設定されているはずなので、
                        # 直接ループを抜けてエンコードタスクの終了処理に移る
                        break

                    # Bridge が意図せず終了した場合、未確定または欠落した Anchor を配信せず再起動する。
                    if bridge is not None and bridge.returncode is not None:
                        result = self.live_stream.setStatus(
                            'Restart',
                            'TS Codec Bridge が強制終了されました。エンコードタスクを再起動しています… (ER-07B)',
                        )
                        if result is True:
                            for line in bridge_lines:
                                logging.warning(line)
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
            ## CancelledError をキャッチしないとエンコーダープロセスの終了処理に到達せず、プロセスがリークしてしまう
            logging.debug(f'{self.live_stream.log_prefix} Encoding task was cancelled by channel switch.')

        except Exception as ex:
            # チューナー接続や Controller 内で想定外の例外が発生した場合でも、
            ## finally の回収処理へ必ず到達させるため、ここでは記録だけ行って例外をそのまま再送出する
            logging.error(f'{self.live_stream.log_prefix} Unexpected error in the encoding task:', exc_info=ex)
            # Offline 遷移と EDCB チューナー解放は finally で主要資源の解放後に行う
            ## ここで tuner.close() などを await すると、それが停滞した場合に finally のプロセス回収へ到達できない
            unexpected_error = True
            raise

        finally:
            # ***** エンコードタスクの終了処理 (パイプライン回収) *****
            ## 想定外の例外で脱出する経路でも起動済みプロセスや HTTP セッションを残留させないよう finally で覆う
            ## Restart / 通常終了のチューナー後処理 (handoff・Cancelling 判定を含む) はこの finally の外側で従来どおり行う

            # 稼働中フラグをオフにし、Reader・Writer・SubWriter・EncoderObServer のすべての非同期タスクを終了させる
            is_running = False

            # まず全プロセスへ kill を送信する (この段階では await しない)
            ## 終了待機の停滞や再キャンセルで後続の解放処理がスキップされないよう、kill 完了後に解放処理を行ってから wait する
            killed = self.KillSubprocesses(('tsreadex', tsreadex), ('encoder', encoder), ('bridge', bridge))

            # await を伴わない解放処理を最初に済ませる
            ## 以降の await (session.close() / tuner.close() / プロセス終了待機) が停滞・再キャンセルされても、
            ## レスポンス close・クライアント切断・アーカイバー破棄は確実に完了している
            if response is not None and response.closed is False:
                response.close()
            self.live_stream.disconnectAll()
            if self.live_stream.psi_data_archiver is not None:
                self.live_stream.psi_data_archiver.destroy()
                self.live_stream.psi_data_archiver = None

            # Mirakurun の HTTP セッションが残っていれば閉じる (正常経路では Writer 内で閉じ済み)
            ## 閉じる途中で再キャンセルされてもチューナー解放と終了待機へ到達できるよう try/finally で囲む
            try:
                if session is not None and session.closed is False:
                    await session.close()
            finally:
                try:
                    # 想定外例外で脱出した場合は、主要資源の解放後に Offline へ遷移させ、
                    # 所有中の EDCB チューナーを閉じて次回接続での再試行を可能にする
                    ## 二重操作防止の所有権チェックは close() 側のガードに委ね、handoff 中 (Cancelling) は閉じない
                    if unexpected_error is True:
                        self.live_stream.setStatus('Offline', 'ライブストリームの処理中に予期しないエラーが発生しました。(E-18)')
                        if LIVE_STREAM_BACKEND == 'EDCB' and self.live_stream.tuner is not None:
                            if self.live_stream.tuner.getState() != 'Cancelling':
                                try:
                                    if await self.live_stream.tuner.close(self.live_stream.live_stream_id) is True:
                                        self.live_stream.tuner = None
                                except Exception as ex:
                                    # close() は EDCB 通信・ストリーム破棄など複数の失敗要因を持つため例外型を限定できない
                                    ## cleanup 例外で本来のエンコード例外を上書きせず、調査可能なログを残して終了待機を続行する
                                    logging.error(
                                        f'{self.live_stream.log_prefix} Failed to close EDCB tuner during unexpected error cleanup:',
                                        exc_info=ex,
                                    )
                finally:
                    # tuner.close() の失敗や再キャンセルでも、kill 済みプロセスの終了待機へ必ず到達する
                    await self.WaitSubprocesses(killed)

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
                background_tasks.add(asyncio.create_task(self.run()))  # 新しいタスクを立ち上げる

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
