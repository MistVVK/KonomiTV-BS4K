from __future__ import annotations

from dataclasses import dataclass

from app.config import Config
from app.constants import QUALITY, QUALITY_TYPES


def GetEncoderForLiveChannel(display_channel_id: str) -> str:
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

    @classmethod
    def fromRequest(
        cls,
        quality: QUALITY_TYPES,
        is_hevc_10bit_requested: bool,
        is_24fps_mode_requested: bool,
        encoder: str | None = None,
        is_24fps_mode_allowed: bool = True,
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

        # HEVC 10bit は通信節約モードで使う HEVC 画質かつ QSVEncC / NVEncC の場合だけ有効化する
        ## VCEEncC は HEVC 10bit 対応の機種かを判定できず、rkmppenc は HEVC 10bit エンコード自体に非対応のため設定しない
        is_hevc_10bit_enabled = (
            is_hevc_10bit_requested is True and
            QUALITY[quality].is_hevc is True and
            encoder in ['QSVEncC', 'NVEncC']
        )

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
        )

    def buildSuffix(self) -> str:
        """
        ライブストリーム ID の末尾に付ける文字列を組み立てる

        Returns:
            str: ライブストリーム ID の末尾に付ける文字列
        """

        suffix = ''

        # -10bit は -24fps より先に付け、ビット深度からフレームレートの順で並べる
        if self.is_hevc_10bit_enabled is True:
            suffix += '-10bit'

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


def SplitQualityAndEncodingOptions(
    quality: str,
    encoder: str | None = None,
    is_24fps_mode_allowed: bool = True,
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

    # ベース画質が QUALITY に存在しない場合は、ルーター側で従来通り 422 を返す
    if base_quality not in QUALITY:
        return None

    # 画質とサーバー側の対応状況を見て、実際に使えるオプションだけを残す
    ## HEVC 10bit 非対応エンコーダーや 1080p-60fps の 24fps モード要求はここで無効化される
    encoding_options = StreamEncodingOptions.fromRequest(
        base_quality,
        is_hevc_10bit_requested,
        is_24fps_mode_requested,
        encoder,
        is_24fps_mode_allowed,
    )
    return StreamQualityWithOptions(
        quality = base_quality,
        encoding_options = encoding_options,
    )
