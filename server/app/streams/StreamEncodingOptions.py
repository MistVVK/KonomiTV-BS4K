from __future__ import annotations

from dataclasses import dataclass

from app.config import Config
from app.constants import QUALITY, QUALITY_TYPES
from app.streams.KonomiTVBS4KPlaybackEncoding import (
    IsKonomiTVBS4KVideoCodecBitDepthSupported,
    KonomiTVBS4KAudioCodec,
    KonomiTVBS4KPlaybackEncoder,
    KonomiTVBS4KVideoBitDepth,
    KonomiTVBS4KVideoCodec,
)


def GetEncoderForLiveChannel(display_channel_id: str) -> KonomiTVBS4KPlaybackEncoder:
    """
    ライブチャンネルで利用するエンコーダーを返す

    Args:
        display_channel_id (str): チャンネル ID

    Returns:
        str: 利用するエンコーダー
    """

    CONFIG = Config()
    if display_channel_id.startswith('bs4k'):
        return CONFIG.general.encoder_bs4k
    return CONFIG.general.encoder


@dataclass(frozen=True)
class StreamEncodingOptions:
    """
    ライブ/録画ストリームでベース画質に追加するエンコードオプションを表す

    Args:
        is_hevc_10bit_enabled (bool): HEVC 10bit を要求するクライアント向けのストリームかどうか
        is_24fps_mode_enabled (bool): 24fps モードを適用するストリームかどうか
    """

    # HEVC 10bit を要求するクライアント向けのストリームかどうか
    ## --fallback-bitdepth により、GPU 側が HEVC 10bit 非対応の場合でも 8bit へフォールバックされるため、
    ## 値の意味は「HEVC 10bit を要求」であり「HEVC 10bit 出力の保証」ではない
    is_hevc_10bit_enabled: bool = False

    # 24fps モードを適用するストリームかどうか
    ## 1080p-60fps では 60fps 化を優先し、24fps モードの要求があってもここでは無効にする
    is_24fps_mode_enabled: bool = False

    # 録画HLSの出力コーデック。ライブでは既存画質から導出した既定値を使用する。
    video_codec: KonomiTVBS4KVideoCodec = 'avc'
    video_bit_depth: KonomiTVBS4KVideoBitDepth = 8
    audio_codec: KonomiTVBS4KAudioCodec = 'aac'

    # 録画再生で映像と一緒に多重化する音声レンディション ID
    # 例: 1, 2, 1-main, 1-sub。None は先頭の利用可能な音声を表す。
    audio_rendition_id: str | None = None

    @classmethod
    def fromRequest(
        cls,
        quality: QUALITY_TYPES,
        is_hevc_10bit_requested: bool,
        is_24fps_mode_requested: bool,
        encoder: KonomiTVBS4KPlaybackEncoder | None = None,
        is_24fps_mode_allowed: bool = True,
        video_codec: KonomiTVBS4KVideoCodec | None = None,
        video_bit_depth: KonomiTVBS4KVideoBitDepth | None = None,
        audio_codec: KonomiTVBS4KAudioCodec = 'aac',
        audio_rendition_id: str | None = None,
    ) -> StreamEncodingOptions:
        """
        API で指定されたオプションから、実際に使うストリームオプションを作る

        Args:
            quality (QUALITY_TYPES): ベース画質
            is_hevc_10bit_requested (bool): クライアントが HEVC 10bit を要求しているかどうか
            is_24fps_mode_requested (bool): クライアントが 24fps モードを要求しているかどうか
            encoder (str | None): このストリームで利用するエンコーダー
            is_24fps_mode_allowed (bool): 24fps モードの適用を許可するかどうか

        Returns:
            StreamEncodingOptions: 実際のストリーム生成に使うエンコードオプション
        """

        if encoder is None:
            encoder = Config().general.encoder

        # 明示queryは共通能力検査で検証する。旧 -10bit URLだけは従来のQSV/NVENC境界を保つ。
        resolved_video_codec: KonomiTVBS4KVideoCodec = video_codec or \
            ('hevc' if QUALITY[quality].is_hevc else 'avc')
        resolved_video_bit_depth: KonomiTVBS4KVideoBitDepth
        if video_bit_depth is not None:
            resolved_video_bit_depth = video_bit_depth
        else:
            # 旧 -10bit URL は従来どおり、HEVCかつ既知の対応backendだけ10bitへ解決する。
            resolved_video_bit_depth = (
                10
                if is_hevc_10bit_requested and resolved_video_codec == 'hevc' and encoder in ('QSV', 'NVENC')
                else 8
            )
        if IsKonomiTVBS4KVideoCodecBitDepthSupported(
            resolved_video_codec,
            resolved_video_bit_depth,
        ) is False:
            raise ValueError(
                f'Unsupported video codec/bit depth combination: '
                f'{resolved_video_codec}/{resolved_video_bit_depth}'
            )
        is_hevc_10bit_enabled = resolved_video_codec == 'hevc' and resolved_video_bit_depth == 10

        # 24fps モードは 60fps 画質以外で有効化する
        ## 1080p-60fps では 60i を 60p 化するユーザー意図が明確なので、24fps モードより 60fps 化を優先する
        is_24fps_mode_enabled = (
            is_24fps_mode_allowed is True and
            is_24fps_mode_requested is True and
            QUALITY[quality].is_60fps is False
        )

        return cls(
            is_hevc_10bit_enabled = is_hevc_10bit_enabled,
            is_24fps_mode_enabled = is_24fps_mode_enabled,
            video_codec = resolved_video_codec,
            video_bit_depth = resolved_video_bit_depth,
            audio_codec = audio_codec,
            audio_rendition_id = audio_rendition_id,
        )

    def buildSuffix(self) -> str:
        """
        ライブストリーム ID の末尾に付ける文字列を組み立てる

        Returns:
            str: ライブストリーム ID の末尾に付ける文字列
        """

        suffix = ''

        # HEVCは既存QUALITYの -hevc で識別される。VP9/AV1だけ内部IDへ明示して衝突を防ぐ。
        if self.video_codec in ('vp9', 'av1'):
            suffix += f'-{self.video_codec}'

        # -10bit は -24fps より先に付け、ビット深度からフレームレートの順で並べる
        if self.video_bit_depth == 10:
            suffix += '-10bit'

        if self.audio_codec != 'aac':
            suffix += f'-{self.audio_codec}'

        # 24fps モードが有効なストリームだけ -24fps を付ける
        if self.is_24fps_mode_enabled is True:
            suffix += '-24fps'

        return suffix


@dataclass(frozen=True)
class StreamQualityWithOptions:
    """
    API パスの品質指定を、ベース画質と追加エンコードオプションへ分解した結果を表す

    Args:
        quality (QUALITY_TYPES): ベース画質
        encoding_options (StreamEncodingOptions): ベース画質に追加するエンコードオプション
    """

    # QUALITY に定義されているベース画質
    ## API パスには 720p-hevc-10bit-24fps のようにオプション付きの品質が渡されるが、エンコード処理にはこの値だけを渡す
    quality: QUALITY_TYPES

    # ベース画質に追加するエンコードオプション
    ## HEVC 10bit や 24fps モードは、ベース画質から分けてストリーム ID やエンコード引数へ渡す
    encoding_options: StreamEncodingOptions

    # queryで映像codecまたはbit depthが明示されたかどうか
    ## 録画index生成前のmaster playlistでも、生成完了後に同じ能力検査を再実行するため保持する
    is_video_encoding_explicitly_requested: bool = False

    # queryで従来値以外の音声codecが明示されたかどうか
    ## codec無指定URLと互換APIのAAC既定経路は従来どおり能力probeを要求しない
    is_audio_encoding_explicitly_requested: bool = False


def SplitQualityAndEncodingOptions(
    quality: str,
    encoder: KonomiTVBS4KPlaybackEncoder | None = None,
    is_24fps_mode_allowed: bool = True,
    video_codec: KonomiTVBS4KVideoCodec | None = None,
    video_bit_depth: KonomiTVBS4KVideoBitDepth | None = None,
    audio_codec: KonomiTVBS4KAudioCodec = 'aac',
    audio_rendition_id: str | None = None,
) -> StreamQualityWithOptions | None:
    """
    API パスの品質指定 (例: 720p-hevc-10bit-24fps) を、ベース画質 (720p-hevc) と追加オプション (-10bit / -24fps) に分解する

    Args:
        quality (str): API パスで指定された品質
        encoder (str | None): このストリームで利用するエンコーダー
        is_24fps_mode_allowed (bool): 24fps モードの適用を許可するかどうか

    Returns:
        StreamQualityWithOptions | None: 分解結果 (不正な品質指定の場合は None)
    """

    # -10bit / -24fps は buildSuffix() と同じ順序でのみ受け付ける
    ## 末尾から剥がすことで、1080p-60fps-hevc のようにベース画質自体が -hevc を含むケースを安全に扱う
    base_quality = quality
    is_24fps_mode_requested = False
    if base_quality.endswith('-24fps') is True:
        base_quality = base_quality[:-len('-24fps')]
        is_24fps_mode_requested = True

    is_hevc_10bit_requested = False
    if base_quality.endswith('-10bit') is True:
        base_quality = base_quality[:-len('-10bit')]
        is_hevc_10bit_requested = True

    # 旧URLでは映像コーデックが画質名の -hevc 接尾辞に埋め込まれている。
    # 明示クエリがある場合はそちらを優先し、内部の既存QUALITYキーへ正規化する。
    legacy_video_codec: KonomiTVBS4KVideoCodec = 'hevc' if base_quality.endswith('-hevc') else 'avc'
    resolved_video_codec = video_codec or legacy_video_codec
    quality_without_codec = base_quality[:-len('-hevc')] if base_quality.endswith('-hevc') else base_quality
    normalized_quality = f'{quality_without_codec}-hevc' if resolved_video_codec == 'hevc' else quality_without_codec
    base_quality = normalized_quality

    # ベース画質が QUALITY に存在しない場合は、ルーター側で従来通り 422 を返す
    if base_quality not in QUALITY:
        return None

    # 明示bit depthと旧 -10bit suffixのどちらでも、AVC 10bitなど仕様外の組み合わせは拒否する。
    requested_video_bit_depth = video_bit_depth
    if requested_video_bit_depth is None and is_hevc_10bit_requested and resolved_video_codec != 'hevc':
        return None
    if (
        requested_video_bit_depth is not None and
        IsKonomiTVBS4KVideoCodecBitDepthSupported(resolved_video_codec, requested_video_bit_depth) is False
    ):
        return None

    # 画質とサーバー側の対応状況を見て、実際に使えるオプションだけを残す
    ## HEVC 10bit 非対応エンコーダーや 1080p-60fps の 24fps モード要求はここで無効化される
    encoding_options = StreamEncodingOptions.fromRequest(
        base_quality,
        is_hevc_10bit_requested,
        is_24fps_mode_requested,
        encoder,
        is_24fps_mode_allowed,
        resolved_video_codec,
        video_bit_depth,
        audio_codec,
        audio_rendition_id,
    )
    return StreamQualityWithOptions(
        quality = base_quality,
        encoding_options = encoding_options,
        is_video_encoding_explicitly_requested = (
            video_codec is not None or video_bit_depth is not None
        ),
        is_audio_encoding_explicitly_requested = audio_codec != 'aac',
    )
