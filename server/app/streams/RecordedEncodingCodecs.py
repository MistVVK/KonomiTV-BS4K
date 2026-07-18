from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


VideoCodec = Literal['avc', 'hevc']
AudioCodec = Literal['aac']


@dataclass(frozen=True)
class VideoCodecDefinition:
    """録画HLSで使用する映像コーデックの宣言的な定義。"""

    id: VideoCodec
    display_name: str
    hls_codec: str
    mime_type: str
    container: Literal['mpegts', 'fmp4']
    ffmpeg_encoder: str
    hwenc_codec: str
    supports_10bit: bool


@dataclass(frozen=True)
class AudioCodecDefinition:
    """録画HLSで使用する音声コーデックの宣言的な定義。"""

    id: AudioCodec
    display_name: str
    hls_codec: str
    mime_type: str
    container: Literal['mpegts', 'fmp4']
    ffmpeg_encoder: str


VIDEO_CODECS: dict[VideoCodec, VideoCodecDefinition] = {
    'avc': VideoCodecDefinition(
        id='avc', display_name='H.264 / AVC', hls_codec='avc1.640028',
        mime_type='video/mp2t', container='mpegts', ffmpeg_encoder='libx264',
        hwenc_codec='h264', supports_10bit=False,
    ),
    'hevc': VideoCodecDefinition(
        id='hevc', display_name='H.265 / HEVC', hls_codec='hvc1.2.4.L153.B0',
        mime_type='video/mp2t', container='mpegts', ffmpeg_encoder='libx265',
        hwenc_codec='hevc', supports_10bit=True,
    ),
}

AUDIO_CODECS: dict[AudioCodec, AudioCodecDefinition] = {
    'aac': AudioCodecDefinition(
        id='aac', display_name='AAC', hls_codec='mp4a.40.2',
        mime_type='video/mp2t', container='mpegts', ffmpeg_encoder='aac',
    ),
}


def getVideoCodecDefinition(codec: VideoCodec) -> VideoCodecDefinition:
    return VIDEO_CODECS[codec]


def getAudioCodecDefinition(codec: AudioCodec) -> AudioCodecDefinition:
    return AUDIO_CODECS[codec]
