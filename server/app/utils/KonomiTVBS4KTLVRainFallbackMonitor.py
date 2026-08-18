"""旧クラス名から汎用 TLV metadata monitor への import 互換を提供する。"""

from app.utils.KonomiTVBS4KTLVMetadataMonitor import KonomiTVBS4KTLVMetadataMonitor


# LiveEncodingTask と外部テストが移行するまで、旧名を同じ実装へ直接結び付ける。
KonomiTVBS4KTLVRainFallbackMonitor = KonomiTVBS4KTLVMetadataMonitor
