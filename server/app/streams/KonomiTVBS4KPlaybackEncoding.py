from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum
from typing import Literal, cast

from app.constants import QUALITY, QUALITY_TYPES


# ライブ・録画の表示アスペクトは upstream 同様 16:9 を正とする。
# coded size から SPS VUI / AV1 render / VP9 render に載せる SAR を一意に決める。
KONOMITV_BS4K_LIVE_OUTPUT_DAR: tuple[int, int] = (16, 9)


@dataclass(frozen=True, slots=True)
class KonomiTVBS4KLiveOutputGeometry:
    """選択画質の coded size と、DAR 16:9 を満たす SAR / 正方画素サイズ。"""

    coded_width: int
    coded_height: int
    sample_aspect_ratio: tuple[int, int]
    square_pixel_width: int
    square_pixel_height: int

    @property
    def needs_non_square_sample_aspect_ratio(self) -> bool:
        return self.sample_aspect_ratio != (1, 1)


def ResolveKonomiTVBS4KLiveOutputGeometry(
    width: int,
    height: int,
    *,
    is_fullhd_channel: bool = False,
    dar: tuple[int, int] = KONOMITV_BS4K_LIVE_OUTPUT_DAR,
) -> KonomiTVBS4KLiveOutputGeometry:
    """
    画質解像度とフル HD 特例から、エンコード用の coded size と SAR を返す。

    AVC/HEVC は SPS VUI に SAR を載せられるため coded size のまま出力する。
    AV1/VP9 など bitstream に SAR を載せにくい codec は、
    square_pixel_* を coded size として使い SAR 1:1 で同じ DAR を実現する。
    """

    coded_width = width
    coded_height = height
    # フル HD 放送局の 1080p 品質だけ 1920x1080 へ上げる（upstream と同じ特例）。
    if coded_width == 1440 and coded_height == 1080 and is_fullhd_channel is True:
        coded_width = 1920

    if coded_width <= 0 or coded_height <= 0:
        raise ValueError(f'Invalid live output size: {coded_width}x{coded_height}')

    dar_width, dar_height = dar
    if dar_width <= 0 or dar_height <= 0:
        raise ValueError(f'Invalid live output DAR: {dar_width}:{dar_height}')

    # SAR = DAR / (coded_width/coded_height) = (dar_w * h) : (dar_h * w)
    sample_aspect_width = dar_width * coded_height
    sample_aspect_height = dar_height * coded_width
    divisor = math.gcd(sample_aspect_width, sample_aspect_height)
    sample_aspect_width //= divisor
    sample_aspect_height //= divisor

    if sample_aspect_width == 1 and sample_aspect_height == 1:
        square_pixel_width = coded_width - (coded_width % 2)
        square_pixel_height = coded_height - (coded_height % 2)
    else:
        # present width = coded_width * SAR
        square_pixel_width = (
            coded_width * sample_aspect_width + sample_aspect_height // 2
        ) // sample_aspect_height
        square_pixel_width -= square_pixel_width % 2
        square_pixel_height = coded_height - (coded_height % 2)

    square_pixel_width = max(square_pixel_width, 2)
    square_pixel_height = max(square_pixel_height, 2)

    return KonomiTVBS4KLiveOutputGeometry(
        coded_width = coded_width,
        coded_height = coded_height,
        sample_aspect_ratio = (sample_aspect_width, sample_aspect_height),
        square_pixel_width = square_pixel_width,
        square_pixel_height = square_pixel_height,
    )


@dataclass(frozen=True, slots=True)
class KonomiTVBS4KLiveEncodePlan:
    """ライブ1本分のエンコード解像度・SAR・16:9 表示枠。"""

    encode_width: int
    encode_height: int
    sample_aspect_ratio: tuple[int, int]
    # force_original_aspect_ratio + pad の目標枠（正方画素・DAR 16:9）
    display_width: int
    display_height: int


def ResolveKonomiTVBS4KLiveEncodePlan(
    width: int,
    height: int,
    *,
    video_codec: KonomiTVBS4KVideoCodec,
    is_fullhd_channel: bool = False,
) -> KonomiTVBS4KLiveEncodePlan:
    """
    codec ごとのエンコード解像度・SAR・表示枠を返す。

    - AVC/HEVC: anamorphic coded size + SPS VUI 用 SAR
    - VP9/AV1: bitstream に SAR を載せにくいため、必要なら正方画素へ展開し SAR 1:1
    - display_* は常に 16:9 の正方画素枠（4:3 入力はここに pillarbox する）
    """

    geometry = ResolveKonomiTVBS4KLiveOutputGeometry(
        width,
        height,
        is_fullhd_channel = is_fullhd_channel,
    )
    if video_codec in ('vp9', 'av1') and geometry.needs_non_square_sample_aspect_ratio is True:
        return KonomiTVBS4KLiveEncodePlan(
            encode_width = geometry.square_pixel_width,
            encode_height = geometry.square_pixel_height,
            sample_aspect_ratio = (1, 1),
            display_width = geometry.square_pixel_width,
            display_height = geometry.square_pixel_height,
        )
    return KonomiTVBS4KLiveEncodePlan(
        encode_width = geometry.coded_width,
        encode_height = geometry.coded_height,
        sample_aspect_ratio = geometry.sample_aspect_ratio,
        display_width = geometry.square_pixel_width,
        display_height = geometry.square_pixel_height,
    )


def ResolveKonomiTVBS4KLiveEncodeSize(
    width: int,
    height: int,
    *,
    video_codec: KonomiTVBS4KVideoCodec,
    is_fullhd_channel: bool = False,
) -> tuple[int, int, tuple[int, int]]:
    """
    codec ごとのエンコード解像度と SAR を返す（後方互換ラッパ）。

    Returns:
        (encode_width, encode_height, (sar_w, sar_h))
    """

    plan = ResolveKonomiTVBS4KLiveEncodePlan(
        width,
        height,
        video_codec = video_codec,
        is_fullhd_channel = is_fullhd_channel,
    )
    return plan.encode_width, plan.encode_height, plan.sample_aspect_ratio


def BuildKonomiTVBS4KLiveAspectPreservingScaleFilters(
    plan: KonomiTVBS4KLiveEncodePlan,
) -> list[str]:
    """
    ISDB-T/S 映像 ES の SAR（→DAR）をフレームごとに使い、16:9 表示枠へ fit する。

    地デジ HD は 1440x1080 + SAR 4:3 = DAR 16:9 が典型。SAR を落とすと
    画素比だけで 4:3 と誤判定される。呼び出し側は vpp 等で SAR を潰す前に
    システムメモリへ落としたフレームへこの列を適用すること。

    手順:
      1. 入力 SAR で正方画素へ展開（iw*sar）… 放送の DAR を動的に反映
      2. 16:9 表示枠へ force_original_aspect_ratio + pad
      3. 必要なら anamorphic coded size へ戻し setsar
    """

    display_width = plan.display_width
    display_height = plan.display_height
    encode_width = plan.encode_width
    encode_height = plan.encode_height
    sar_width, sar_height = plan.sample_aspect_ratio

    filters = [
        # ISDB の sample_aspect_ratio を使い正方画素へ（eval=frame で途中切替にも追従）
        # カンマは filtergraph 区切りと衝突するため \, でエスケープする
        "scale=w='max(2\\,trunc(iw*sar/2)*2)':h='max(2\\,trunc(ih/2)*2)':eval=frame",
        'setsar=1/1',
        # 正方画素になった映像を 16:9 出力枠へ（4:3 なら左右黒帯）
        f'scale=w={display_width}:h={display_height}:force_original_aspect_ratio=decrease:eval=frame',
        f'pad={display_width}:{display_height}:(ow-iw)/2:(oh-ih)/2:black',
    ]
    # AVC/HEVC の anamorphic coded size へ戻す（表示比は setsar で維持）
    if (encode_width, encode_height) != (display_width, display_height):
        filters.append(f'scale={encode_width}:{encode_height}')
    filters.append(f'setsar={sar_width}/{sar_height}')
    return filters


def ResolveKonomiTVBS4KLiveHwDownloadFormat(
    encoder_type: KonomiTVBS4KPlaybackEncoder,
    *,
    channel_type: str,
    encoder_pixel_format: str,
) -> str:
    """
    hwdownload 直後のシステムメモリ画素形式を返す。

    HW 面の実フォーマットと不一致だと
    "Invalid output format … for hwframe download" でフィルタが落ちる。
    """

    if encoder_type == 'QSV':
        # 地デジ/BS の QSV デコード面はほぼ nv12 (8bit)。10bit 出力は download 後に format 変換する。
        return 'nv12'
    if encoder_type == 'NVENC':
        # BS4K は HEVC Main10 が多く p010le。それ以外は nv12。
        if channel_type == 'BS4K' or encoder_pixel_format == 'p010le':
            return 'p010le'
        return 'nv12'
    # VAAPI
    if encoder_pixel_format == 'p010le':
        return 'p010le'
    return 'nv12'


def CanUseKonomiTVBS4KLiveHardwareFilterPath(
    *,
    source_dar_width: int,
    source_dar_height: int,
    is_24fps_mode_enabled: bool,
) -> bool:
    """
    full-GPU フィルタ経路を使ってよいか返す。

    - 24fps (pullup/dejudder) は SW 必須のため不可
    - 表示が 16:9 近傍なら encode 枠へ stretch するだけで足りる
    - 4:3 など pad が必要な DAR は SW aspect 経路へ落とす

    Args:
        source_dar_width (int): 入力映像の表示アスペクト比の幅。
        source_dar_height (int): 入力映像の表示アスペクト比の高さ。
        is_24fps_mode_enabled (bool): 24fps 逆テレシネ処理を有効にするか。

    Returns:
        bool: 判定結果。
    """

    if is_24fps_mode_enabled is True:
        return False
    if source_dar_width <= 0 or source_dar_height <= 0:
        return False
    return abs((source_dar_width / source_dar_height) - (16 / 9)) <= 0.02


def BuildKonomiTVBS4KLiveHardwareVideoFilters(
    encoder_type: KonomiTVBS4KPlaybackEncoder,
    *,
    encode_width: int,
    encode_height: int,
    encoder_pixel_format: str,
    is_interlaced: bool,
    is_60fps: bool,
    low_latency: bool,
) -> list[str]:
    """
    SAR 監視で 16:9 と判明した（または既定）入力向けの full-GPU フィルタ列を返す。

    vpp は入力 SAR を落とすが、事前に DAR 16:9 と分かっている場合は
    coded size → encode size への stretch が正しい正方画素/アナモルフィック出力になる。

    Args:
        encoder_type (KonomiTVBS4KPlaybackEncoder): 処理または検証対象の encoder backend。
        encode_width (int): encoder が出力する映像の幅。
        encode_height (int): encoder が出力する映像の高さ。
        encoder_pixel_format (str): encoder に入力する pixel format。
        is_interlaced (bool): 入力映像がインターレースか。
        is_60fps (bool): 60fps 出力を生成するか。
        low_latency (bool): 低遅延向けの浅い filter queue を使うか。

    Returns:
        list[str]: FFmpeg へ渡す filter または command option 群。
    """

    if encode_width < 2 or encode_height < 2:
        raise ValueError(f'Invalid encode size: {encode_width}x{encode_height}')

    filters: list[str] = []
    if encoder_type == 'QSV':
        qsv_filter = 'vpp_qsv='
        if is_interlaced is True:
            qsv_filter += (
                f'deinterlace=advanced:rate={"field" if is_60fps is True else "frame"}:'
            )
        qsv_filter += (
            f'w={encode_width}:h={encode_height}:format={encoder_pixel_format}:'
            f'async_depth={1 if low_latency is True else 4}'
        )
        filters.append(qsv_filter)
    elif encoder_type == 'NVENC':
        if is_interlaced is True:
            # ISDB 1080i/480i は top-field-first。録画再生と異なり parity=auto にせず固定する。
            filters.append(
                f'bwdif_cuda=mode={"send_field" if is_60fps is True else "send_frame"}:'
                'parity=0:deint=interlaced'
            )
        filters.append(
            f'scale_cuda=w={encode_width}:h={encode_height}:format={encoder_pixel_format}'
        )
    else:
        if is_interlaced is True:
            # deinterlace_vaapi に parity 指定は無いため auto=0 で常に解除する。
            # auto=1 はプログレッシブ誤判定時に解除を飛ばし得るので使わない。フィールド順は bitstream に従う。
            filters.append(
                f'deinterlace_vaapi=rate={"field" if is_60fps is True else "frame"}:auto=0'
            )
        filters.append(
            f'scale_vaapi=w={encode_width}:h={encode_height}:format={encoder_pixel_format}'
        )
        # AMF は system memory の NV12/P010 を受け取る
        filters.append(f'hwdownload,format={encoder_pixel_format}')
    return filters


KonomiTVBS4KPlaybackEncoder = Literal['FFmpeg', 'QSV', 'NVENC', 'AMF']
KonomiTVBS4KPlaybackMode = Literal['Live', 'Video']
KonomiTVBS4KVideoCodec = Literal['avc', 'hevc', 'vp9', 'av1']
KonomiTVBS4KAudioCodec = Literal['aac', 'opus']
KonomiTVBS4KVideoBitDepth = Literal[8, 10]
KonomiTVBS4KPlaybackCapabilityReason = Literal[
    'BinaryUnavailable',
    'BridgeUnavailable',
    'FeatureDisabled',
    'DeviceUnavailable',
    'DeviceInitializationFailed',
    'FilterUnavailable',
    'EncodeFailed',
    'ProbeFailed',
    'CodecMismatch',
    'BitDepthMismatch',
    'ProfileMismatch',
    'UnsupportedCombination',
]


class KonomiTVBS4KVideoBitDepthQuery(IntEnum):
    """公開APIのquery文字列から整数へ変換する映像bit depth。"""

    BIT_8 = 8
    BIT_10 = 10


@dataclass(frozen=True, slots=True)
class KonomiTVBS4KVideoCodecDefinition:
    """ライブMPEG-TSと録画fMP4で共有する映像コーデック定義。"""

    id: KonomiTVBS4KVideoCodec
    display_name: str
    hls_codec: str
    mime_type: str
    container: Literal['mpegts', 'fmp4']
    ffmpeg_encoder: str
    hwenc_codec: str
    supports_10bit: bool


@dataclass(frozen=True, slots=True)
class KonomiTVBS4KAudioCodecDefinition:
    """ライブMPEG-TSと録画fMP4で共有する音声コーデック定義。"""

    id: KonomiTVBS4KAudioCodec
    display_name: str
    hls_codec: str
    mime_type: str
    container: Literal['mpegts', 'fmp4']
    ffmpeg_encoder: str


@dataclass(frozen=True, slots=True)
class KonomiTVBS4KPlaybackVideoBitrate:
    """ライブ・録画で共有する映像の指定値と最大ビットレート。"""

    video_bitrate: str
    video_bitrate_max: str


@dataclass(frozen=True, slots=True)
class KonomiTVBS4KPlaybackVideoCapability:
    """映像codec能力をライブ・録画で独立して表す内部契約。"""

    encoder: KonomiTVBS4KPlaybackEncoder
    codec: KonomiTVBS4KVideoCodec
    bit_depth: KonomiTVBS4KVideoBitDepth
    profile: str
    live_available: bool
    recorded_available: bool
    live_reason_code: KonomiTVBS4KPlaybackCapabilityReason | None
    recorded_reason_code: KonomiTVBS4KPlaybackCapabilityReason | None


@dataclass(frozen=True, slots=True)
class KonomiTVBS4KPlaybackAudioCapability:
    """音声codec能力をライブ・録画で独立して表す内部契約。"""

    codec: KonomiTVBS4KAudioCodec
    live_available: bool
    recorded_available: bool
    live_reason_code: KonomiTVBS4KPlaybackCapabilityReason | None
    recorded_reason_code: KonomiTVBS4KPlaybackCapabilityReason | None


@dataclass(frozen=True, slots=True)
class KonomiTVBS4KPlaybackLiveCombinationCapability:
    """ライブで実際に利用できる映像・音声・backendの組み合わせを表す内部契約。"""

    encoder: KonomiTVBS4KPlaybackEncoder
    video_codec: KonomiTVBS4KVideoCodec
    video_bit_depth: KonomiTVBS4KVideoBitDepth
    audio_codec: KonomiTVBS4KAudioCodec
    available: bool
    reason_code: KonomiTVBS4KPlaybackCapabilityReason | None


@dataclass(frozen=True, slots=True)
class KonomiTVBS4KPlaybackCapabilities:
    """通常APIが公開する映像・音声codec能力の全体。"""

    video: tuple[KonomiTVBS4KPlaybackVideoCapability, ...]
    audio: tuple[KonomiTVBS4KPlaybackAudioCapability, ...]
    live_combinations: tuple[KonomiTVBS4KPlaybackLiveCombinationCapability, ...]


KONOMITV_BS4K_VIDEO_CODECS: dict[KonomiTVBS4KVideoCodec, KonomiTVBS4KVideoCodecDefinition] = {
    'avc': KonomiTVBS4KVideoCodecDefinition(
        id = 'avc',
        display_name = 'H.264 / AVC',
        hls_codec = 'avc1.640028',
        mime_type = 'video/mp2t',
        container = 'mpegts',
        ffmpeg_encoder = 'libx264',
        hwenc_codec = 'h264',
        supports_10bit = False,
    ),
    'hevc': KonomiTVBS4KVideoCodecDefinition(
        id = 'hevc',
        display_name = 'H.265 / HEVC',
        hls_codec = 'hvc1.2.4.L153.B0',
        mime_type = 'video/mp2t',
        container = 'mpegts',
        ffmpeg_encoder = 'libx265',
        hwenc_codec = 'hevc',
        supports_10bit = True,
    ),
    'vp9': KonomiTVBS4KVideoCodecDefinition(
        id = 'vp9',
        display_name = 'Google VP9',
        hls_codec = 'vp09.02.10.10',
        mime_type = 'video/mp4',
        container = 'fmp4',
        ffmpeg_encoder = 'libvpx-vp9',
        hwenc_codec = 'vp9',
        supports_10bit = True,
    ),
    'av1': KonomiTVBS4KVideoCodecDefinition(
        id = 'av1',
        display_name = 'Alliance for Open Media AV1',
        hls_codec = 'av01.0.10M.10',
        mime_type = 'video/mp4',
        container = 'fmp4',
        ffmpeg_encoder = 'libaom-av1',
        hwenc_codec = 'av1',
        supports_10bit = True,
    ),
}

KONOMITV_BS4K_AUDIO_CODECS: dict[KonomiTVBS4KAudioCodec, KonomiTVBS4KAudioCodecDefinition] = {
    'aac': KonomiTVBS4KAudioCodecDefinition(
        id = 'aac',
        display_name = 'AAC',
        hls_codec = 'mp4a.40.2',
        mime_type = 'audio/mp4',
        container = 'fmp4',
        ffmpeg_encoder = 'aac',
    ),
    'opus': KonomiTVBS4KAudioCodecDefinition(
        id = 'opus',
        display_name = 'Opus',
        hls_codec = 'opus',
        mime_type = 'audio/mp4',
        container = 'fmp4',
        ffmpeg_encoder = 'libopus',
    ),
}

# HEVCを基準にVP9とAV1の帯域を段階的に抑える既存方針を、ライブと録画で共用する。
KONOMITV_BS4K_VIDEO_BITRATE_RATIOS_FROM_HEVC: dict[KonomiTVBS4KVideoCodec, tuple[int, int]] = {
    'hevc': (100, 100),
    'vp9': (90, 100),
    'av1': (70, 100),
}
KONOMITV_BS4K_VIDEO_BITRATE_MINIMUM_GAP_KBPS = 50
KONOMITV_BS4K_ADVANCED_LIVE_MUXRATE_MINIMUM_HEADROOM_KBPS = 1400
KONOMITV_BS4K_ADVANCED_LIVE_MUXRATE_HEADROOM_PERCENT = 25
# AV1 ライブは QSV 等の I フレームが平均 rate の十倍超になることがあり、
# VP9 と同じ 25%+1400Kbps 余力だと固定 muxrate 上で 1 枚の配送に数百 ms かかり
# クライアント側で断続的な映像停止（バッファ underrun）になる。
# まず広い配送余力を計算し、その後に固定 signal Level の T-STD Rx で TS 全体を cap する。
KONOMITV_BS4K_ADVANCED_LIVE_MUXRATE_AV1_MINIMUM_HEADROOM_KBPS = 9000
KONOMITV_BS4K_ADVANCED_LIVE_MUXRATE_AV1_HEADROOM_PERCENT = 200


@dataclass(frozen = True, slots = True)
class KonomiTVBS4KAV1MainTierTSTDLimit:
    """AV1 Main profile / Main tier の必要最小 Level に対応する T-STD 受信上限。"""

    minimum_level: str
    minimum_sequence_level_index: int
    maximum_bitrate_kbps: int
    maximum_transport_rate_kbps: int


_KONOMITV_BS4K_AV1_LEVEL_20_MAIN_TIER_TSTD_LIMIT = KonomiTVBS4KAV1MainTierTSTDLimit(
    minimum_level = '2.0',
    minimum_sequence_level_index = 0,
    maximum_bitrate_kbps = 1500,
    maximum_transport_rate_kbps = 1650,
)
_KONOMITV_BS4K_AV1_LEVEL_21_MAIN_TIER_TSTD_LIMIT = KonomiTVBS4KAV1MainTierTSTDLimit(
    minimum_level = '2.1',
    minimum_sequence_level_index = 1,
    maximum_bitrate_kbps = 3000,
    maximum_transport_rate_kbps = 3300,
)
_KONOMITV_BS4K_AV1_LEVEL_30_MAIN_TIER_TSTD_LIMIT = KonomiTVBS4KAV1MainTierTSTDLimit(
    minimum_level = '3.0',
    minimum_sequence_level_index = 4,
    maximum_bitrate_kbps = 6000,
    maximum_transport_rate_kbps = 6600,
)
_KONOMITV_BS4K_AV1_LEVEL_31_MAIN_TIER_TSTD_LIMIT = KonomiTVBS4KAV1MainTierTSTDLimit(
    minimum_level = '3.1',
    minimum_sequence_level_index = 5,
    maximum_bitrate_kbps = 10000,
    maximum_transport_rate_kbps = 11000,
)
_KONOMITV_BS4K_AV1_LEVEL_40_MAIN_TIER_TSTD_LIMIT = KonomiTVBS4KAV1MainTierTSTDLimit(
    minimum_level = '4.0',
    minimum_sequence_level_index = 8,
    maximum_bitrate_kbps = 12000,
    maximum_transport_rate_kbps = 13200,
)
_KONOMITV_BS4K_AV1_LEVEL_41_MAIN_TIER_TSTD_LIMIT = KonomiTVBS4KAV1MainTierTSTDLimit(
    minimum_level = '4.1',
    minimum_sequence_level_index = 9,
    maximum_bitrate_kbps = 20000,
    maximum_transport_rate_kbps = 22000,
)
_KONOMITV_BS4K_AV1_LEVEL_50_MAIN_TIER_TSTD_LIMIT = KonomiTVBS4KAV1MainTierTSTDLimit(
    minimum_level = '5.0',
    minimum_sequence_level_index = 12,
    maximum_bitrate_kbps = 30000,
    maximum_transport_rate_kbps = 33000,
)
_KONOMITV_BS4K_AV1_LEVEL_51_MAIN_TIER_TSTD_LIMIT = KonomiTVBS4KAV1MainTierTSTDLimit(
    minimum_level = '5.1',
    minimum_sequence_level_index = 13,
    maximum_bitrate_kbps = 40000,
    maximum_transport_rate_kbps = 44000,
)
_KONOMITV_BS4K_AV1_LEVEL_61_MAIN_TIER_TSTD_LIMIT = KonomiTVBS4KAV1MainTierTSTDLimit(
    minimum_level = '6.1',
    minimum_sequence_level_index = 17,
    maximum_bitrate_kbps = 100000,
    maximum_transport_rate_kbps = 110000,
)

# AV1 Annex A の画素数・表示レート制約から求めた、各固定解像度・fpsの必要最小 Level。
# 実運用 FFmpeg 8/libaom 出力では 240p～2160p の全代表画質、QSV 実 TS では 540p で
# この sequence_level_index と一致することを確認済み。未実測 backend がより高い
# Level を signal しても、最小 Level の Rx を使うため cap は安全側になる。
# T-STD Rx は Main tier Max BitRate の 1.1 倍であり、音声・data・PSI を含む
# 固定 TS muxrate 全体をこの値以下にする必要がある。
KONOMITV_BS4K_AV1_MAIN_TIER_TSTD_LIMITS_BY_QUALITY: dict[
    str,
    KonomiTVBS4KAV1MainTierTSTDLimit,
] = {
    '4320p': _KONOMITV_BS4K_AV1_LEVEL_61_MAIN_TIER_TSTD_LIMIT,
    '2160p': _KONOMITV_BS4K_AV1_LEVEL_51_MAIN_TIER_TSTD_LIMIT,
    '1440p': _KONOMITV_BS4K_AV1_LEVEL_50_MAIN_TIER_TSTD_LIMIT,
    '1080p-60fps': _KONOMITV_BS4K_AV1_LEVEL_41_MAIN_TIER_TSTD_LIMIT,
    '1080p-30fps': _KONOMITV_BS4K_AV1_LEVEL_40_MAIN_TIER_TSTD_LIMIT,
    '1080p': _KONOMITV_BS4K_AV1_LEVEL_40_MAIN_TIER_TSTD_LIMIT,
    '810p-60fps': _KONOMITV_BS4K_AV1_LEVEL_40_MAIN_TIER_TSTD_LIMIT,
    '810p-30fps': _KONOMITV_BS4K_AV1_LEVEL_40_MAIN_TIER_TSTD_LIMIT,
    '810p': _KONOMITV_BS4K_AV1_LEVEL_40_MAIN_TIER_TSTD_LIMIT,
    '720p-60fps': _KONOMITV_BS4K_AV1_LEVEL_40_MAIN_TIER_TSTD_LIMIT,
    '720p': _KONOMITV_BS4K_AV1_LEVEL_31_MAIN_TIER_TSTD_LIMIT,
    '720p-30fps': _KONOMITV_BS4K_AV1_LEVEL_31_MAIN_TIER_TSTD_LIMIT,
    '540p': _KONOMITV_BS4K_AV1_LEVEL_30_MAIN_TIER_TSTD_LIMIT,
    '540p-30fps': _KONOMITV_BS4K_AV1_LEVEL_30_MAIN_TIER_TSTD_LIMIT,
    '480p': _KONOMITV_BS4K_AV1_LEVEL_30_MAIN_TIER_TSTD_LIMIT,
    '480p-30fps': _KONOMITV_BS4K_AV1_LEVEL_30_MAIN_TIER_TSTD_LIMIT,
    '360p': _KONOMITV_BS4K_AV1_LEVEL_21_MAIN_TIER_TSTD_LIMIT,
    '360p-30fps': _KONOMITV_BS4K_AV1_LEVEL_21_MAIN_TIER_TSTD_LIMIT,
    '240p': _KONOMITV_BS4K_AV1_LEVEL_20_MAIN_TIER_TSTD_LIMIT,
    '240p-30fps': _KONOMITV_BS4K_AV1_LEVEL_20_MAIN_TIER_TSTD_LIMIT,
}


def GetKonomiTVBS4KVideoCodecDefinition(
    codec: KonomiTVBS4KVideoCodec,
) -> KonomiTVBS4KVideoCodecDefinition:
    """指定映像codecの共通定義を返す。"""

    return KONOMITV_BS4K_VIDEO_CODECS[codec]


def GetKonomiTVBS4KAudioCodecDefinition(
    codec: KonomiTVBS4KAudioCodec,
) -> KonomiTVBS4KAudioCodecDefinition:
    """指定音声codecの共通定義を返す。"""

    return KONOMITV_BS4K_AUDIO_CODECS[codec]


def IsKonomiTVBS4KVideoCodecBitDepthSupported(
    codec: KonomiTVBS4KVideoCodec,
    bit_depth: KonomiTVBS4KVideoBitDepth,
) -> bool:
    """codec仕様としてbit depthを表現可能か返す。"""

    return bit_depth == 8 or KONOMITV_BS4K_VIDEO_CODECS[codec].supports_10bit


def ResolveKonomiTVBS4KPlaybackVideoBitrate(
    quality: QUALITY_TYPES,
    codec: KonomiTVBS4KVideoCodec,
) -> KonomiTVBS4KPlaybackVideoBitrate:
    """画質とcodecからライブ・録画共通の映像ビットレートを解決する。"""

    quality_without_codec = quality.removesuffix('-hevc')
    avc_quality_key = cast(QUALITY_TYPES, quality_without_codec)
    hevc_quality_key = cast(QUALITY_TYPES, f'{quality_without_codec}-hevc')
    if avc_quality_key not in QUALITY or hevc_quality_key not in QUALITY:
        raise ValueError(f'Video bitrate is not defined for quality: {quality}')
    avc_quality = QUALITY[avc_quality_key]
    hevc_quality = QUALITY[hevc_quality_key]

    def ParseKbps(value: str) -> int:
        if value.endswith('K') is False:
            raise ValueError(f'Invalid video bitrate: {value}')
        return int(value[:-1])

    avc_bitrate_kbps = ParseKbps(avc_quality.video_bitrate)
    avc_bitrate_max_kbps = ParseKbps(avc_quality.video_bitrate_max)
    if codec == 'avc':
        return KonomiTVBS4KPlaybackVideoBitrate(
            video_bitrate = f'{avc_bitrate_kbps}K',
            video_bitrate_max = f'{avc_bitrate_max_kbps}K',
        )

    # AVCと同値になる端点にも最低差を設け、AV1 < VP9 < HEVC < AVCを維持する。
    hevc_bitrate_kbps = min(
        ParseKbps(hevc_quality.video_bitrate),
        avc_bitrate_kbps - KONOMITV_BS4K_VIDEO_BITRATE_MINIMUM_GAP_KBPS,
    )
    hevc_bitrate_max_kbps = min(
        ParseKbps(hevc_quality.video_bitrate_max),
        avc_bitrate_max_kbps - KONOMITV_BS4K_VIDEO_BITRATE_MINIMUM_GAP_KBPS,
    )
    ratio_numerator, ratio_denominator = KONOMITV_BS4K_VIDEO_BITRATE_RATIOS_FROM_HEVC[codec]
    return KonomiTVBS4KPlaybackVideoBitrate(
        video_bitrate = f'{hevc_bitrate_kbps * ratio_numerator // ratio_denominator}K',
        video_bitrate_max = f'{hevc_bitrate_max_kbps * ratio_numerator // ratio_denominator}K',
    )


def ResolveKonomiTVBS4KAdvancedLiveMuxrate(
    video_bitrate_max: str,
    *,
    quality: QUALITY_TYPES | None = None,
    video_codec: KonomiTVBS4KVideoCodec | None = None,
) -> str:
    """
    Bridge が同一 TS packet 内の stuffing で映像 PES を整形できる固定 muxrate を返す。

    VP9 / 汎用: 映像最大値の 25% と 1400Kbps の大きい方を、音声・data・PSI と
    start-code / emulation prevention の余力として確保する。
    AV1: I フレーム突発が大きいため 200% または 9000Kbps の大きい方を余力にする。
    その計算値は、固定解像度・fpsに必要な AV1 Main profile / Main tier の最小 Level の
    T-STD Rx を超えないよう、音声・data・PSI 込みの TS 全体で cap する。

    Args:
        video_bitrate_max (str): Kbps 単位で表した最大映像ビットレート。
        quality (QUALITY_TYPES | None): 処理または検証対象の画質。
        video_codec (KonomiTVBS4KVideoCodec | None): 処理または検証対象の映像 codec。

    Returns:
        str: 処理結果の文字列。
    """

    if (quality is None) != (video_codec is None):
        raise ValueError('Advanced live quality and video codec must be specified together.')
    if video_bitrate_max.endswith('K') is False:
        raise ValueError(f'Invalid advanced live maximum bitrate: {video_bitrate_max}')
    try:
        video_bitrate_max_kbps = int(video_bitrate_max[:-1])
    except ValueError as ex:
        raise ValueError(f'Invalid advanced live maximum bitrate: {video_bitrate_max}') from ex
    if video_bitrate_max_kbps <= 0:
        raise ValueError(f'Invalid advanced live maximum bitrate: {video_bitrate_max}')
    if video_codec == 'av1':
        headroom_percent = KONOMITV_BS4K_ADVANCED_LIVE_MUXRATE_AV1_HEADROOM_PERCENT
        minimum_headroom_kbps = KONOMITV_BS4K_ADVANCED_LIVE_MUXRATE_AV1_MINIMUM_HEADROOM_KBPS
    else:
        headroom_percent = KONOMITV_BS4K_ADVANCED_LIVE_MUXRATE_HEADROOM_PERCENT
        minimum_headroom_kbps = KONOMITV_BS4K_ADVANCED_LIVE_MUXRATE_MINIMUM_HEADROOM_KBPS
    proportional_headroom_kbps = (
        video_bitrate_max_kbps *
        headroom_percent +
        99
    ) // 100
    headroom_kbps = max(
        minimum_headroom_kbps,
        proportional_headroom_kbps,
    )
    muxrate_kbps = video_bitrate_max_kbps + headroom_kbps
    if quality is not None and video_codec == 'av1':
        quality_without_codec = quality.removesuffix('-hevc')
        tstd_limit = KONOMITV_BS4K_AV1_MAIN_TIER_TSTD_LIMITS_BY_QUALITY.get(
            quality_without_codec
        )
        if tstd_limit is None:
            raise ValueError(f'AV1 T-STD limit is not defined for quality: {quality}')
        muxrate_kbps = min(
            muxrate_kbps,
            tstd_limit.maximum_transport_rate_kbps,
        )
    if muxrate_kbps <= video_bitrate_max_kbps:
        raise ValueError('Advanced live muxrate must exceed the maximum video bitrate.')
    return f'{muxrate_kbps}K'


def ParseKonomiTVBS4KAdvancedLiveMuxrateKbps(muxrate: str) -> str:
    """
    FFmpeg の固定 muxrate 表記を、Bridge の正整数 Kbps 表記へ変換する。

    T-STD 検証には実際の TS 搬送レートが必要なため、単位の省略や
    非正整数を黙って受理しない。
    """

    if muxrate.endswith('K') is False:
        raise ValueError(f'Invalid advanced live muxrate: {muxrate}')
    try:
        muxrate_kbps = int(muxrate[:-1])
    except ValueError as ex:
        raise ValueError(f'Invalid advanced live muxrate: {muxrate}') from ex
    if muxrate_kbps <= 0:
        raise ValueError(f'Invalid advanced live muxrate: {muxrate}')
    return str(muxrate_kbps)
