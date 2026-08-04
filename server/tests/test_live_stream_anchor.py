import asyncio
from types import SimpleNamespace

import pytest
from fastapi import Request

from app.config import ServerSettings
from app.constants import QUALITY
from app.routers import LiveStreamsRouter
from app.streams.KonomiTVBS4KPlaybackEncoding import (
    ResolveKonomiTVBS4KAdvancedLiveMuxrate,
)
from app.streams.LiveEncodingTask import LiveEncodingTask
from app.streams.LiveStream import LiveStream
from app.streams.RecordedPlaybackCapabilities import RecordedPlaybackBackend
from app.streams.StreamEncodingOptions import (
    StreamEncodingOptions,
    StreamQualityWithOptions,
)


def BuildEncodingTask(monkeypatch: pytest.MonkeyPatch, *, stream_anchor_enabled: bool) -> LiveEncodingTask:
    """Anchor オプション生成に必要な最小構成のタスクを返す。"""

    settings = ServerSettings.model_validate({}, context={'bypass_validation': True})
    monkeypatch.setattr('app.streams.LiveEncodingTask.Config', lambda: settings)
    task = object.__new__(LiveEncodingTask)
    task._retry_count = 0
    task.live_stream = SimpleNamespace(
        stream_anchor_enabled=stream_anchor_enabled,
        encoding_options=SimpleNamespace(
            is_24fps_mode_enabled=True,
            is_hevc_10bit_enabled=False,
        ),
    )
    return task


def test_stream_anchor_generation_id_is_nonzero_uint64(monkeypatch: pytest.MonkeyPatch) -> None:
    generated = iter((0, 0x123456789ABCDEF0))
    monkeypatch.setattr('app.streams.LiveEncodingTask.secrets.randbits', lambda _bits: next(generated))

    assert LiveEncodingTask.GenerateStreamAnchorGenerationID() == 0x123456789ABCDEF0


def test_anchor_enabled_ffmpeg8_backends_use_fixed_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    task = BuildEncodingTask(monkeypatch, stream_anchor_enabled=True)
    expected_muxrate = ResolveKonomiTVBS4KAdvancedLiveMuxrate(QUALITY['240p'].video_bitrate_max)
    monkeypatch.setattr(RecordedPlaybackBackend, 'discoverRenderDevices', lambda _encoder: ['/dev/dri/renderD128'])

    ffmpeg_options = task.buildFFmpegOptions('240p', 'GR', False)
    assert ffmpeg_options[ffmpeg_options.index('-muxrate') + 1] == expected_muxrate
    assert ffmpeg_options[ffmpeg_options.index('-pcr_period') + 1] == '20'

    qsv_options = task.buildFFmpeg8HardwareOptions('240p', 'QSV', 'GR', False)
    assert qsv_options[qsv_options.index('-muxrate') + 1] == expected_muxrate
    assert qsv_options[qsv_options.index('-pcr_period') + 1] == '20'
    assert qsv_options[qsv_options.index('-c:v') + 1] == 'h264_qsv'


def test_compatibility_transport_does_not_add_anchor_options(monkeypatch: pytest.MonkeyPatch) -> None:
    task = BuildEncodingTask(monkeypatch, stream_anchor_enabled=False)
    monkeypatch.setattr(RecordedPlaybackBackend, 'discoverRenderDevices', lambda _encoder: ['/dev/dri/renderD128'])

    ffmpeg_options = task.buildFFmpegOptions('240p', 'GR', False)
    qsv_options = task.buildFFmpeg8HardwareOptions('240p', 'QSV', 'GR', False)

    assert '-muxrate' not in ffmpeg_options
    assert '-pcr_period' not in ffmpeg_options
    assert '-muxrate' not in qsv_options
    assert '-pcr_period' not in qsv_options


def test_bridge_options_finalize_stream_anchor() -> None:
    task = object.__new__(LiveEncodingTask)

    assert task.BuildTSCodecBridgeOptions() == [
        '--video-codec', 'passthrough',
        '--audio-codec', 'aac',
        '--stream-anchor-v1',
    ]


def test_main_and_compatibility_live_streams_are_separate_singletons() -> None:
    options = StreamEncodingOptions()

    main_stream = LiveStream('gr991', '240p', options, True)
    compatibility_stream = LiveStream('gr991', '240p', options, False)

    assert main_stream is not compatibility_stream
    assert main_stream.stream_anchor_enabled is True
    assert compatibility_stream.stream_anchor_enabled is False
    assert compatibility_stream.live_stream_id == f'{main_stream.live_stream_id}-compat'


def test_live_routes_propagate_compatibility_anchor_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    observed_flags: list[bool] = []

    class FakeLiveStream:
        def __init__(self, *_args, **_kwargs) -> None:
            observed_flags.append(_args[3])

        async def connect(self, _client_type: str) -> SimpleNamespace:
            return SimpleNamespace()

    monkeypatch.setattr(LiveStreamsRouter, 'LiveStream', FakeLiveStream)
    request = Request({'type': 'http'})
    request.state.stream_anchor_enabled = False
    stream_quality = StreamQualityWithOptions('240p', StreamEncodingOptions())

    async def RunRoutes() -> None:
        await LiveStreamsRouter.LiveStreamEventAPI(request, 'gr991', stream_quality)
        await LiveStreamsRouter.LiveMPEGTSStreamAPI(request, 'gr991', stream_quality)

    asyncio.run(RunRoutes())

    assert observed_flags == [False, False]
