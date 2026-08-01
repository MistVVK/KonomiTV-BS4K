"""旧録画codec importを共通KonomiTV-BS4K codec定義へ接続する互換shim。"""

from app.streams.KonomiTVBS4KPlaybackEncoding import (
    KONOMITV_BS4K_AUDIO_CODECS,
    KONOMITV_BS4K_VIDEO_CODECS,
    GetKonomiTVBS4KAudioCodecDefinition,
    GetKonomiTVBS4KVideoCodecDefinition,
    KonomiTVBS4KAudioCodec,
    KonomiTVBS4KAudioCodecDefinition,
    KonomiTVBS4KVideoCodec,
    KonomiTVBS4KVideoCodecDefinition,
)


AudioCodec = KonomiTVBS4KAudioCodec
VideoCodec = KonomiTVBS4KVideoCodec
AudioCodecDefinition = KonomiTVBS4KAudioCodecDefinition
VideoCodecDefinition = KonomiTVBS4KVideoCodecDefinition
AUDIO_CODECS = KONOMITV_BS4K_AUDIO_CODECS
VIDEO_CODECS = KONOMITV_BS4K_VIDEO_CODECS
getAudioCodecDefinition = GetKonomiTVBS4KAudioCodecDefinition
getVideoCodecDefinition = GetKonomiTVBS4KVideoCodecDefinition
