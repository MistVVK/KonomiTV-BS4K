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
from app.streams.RecordedPlaybackCapabilities import (
    RecordedPlaybackBackend,
    RecordedPlaybackCapabilityProbe,
)
from app.streams.StreamEncodingOptions import (
    StreamEncodingOptions,
    StreamQualityWithOptions,
)


def BuildEncodingTask(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream_anchor_enabled: bool,
    is_24fps_mode_enabled: bool = True,
    video_codec: str = 'avc',
    video_bit_depth: int = 8,
    audio_codec: str = 'aac',
    bs4k_input_analysis_enabled: bool = True,
) -> LiveEncodingTask:
    """Anchor オプション生成に必要な最小構成のタスクを返す。"""

    settings = ServerSettings.model_validate({}, context={'bypass_validation': True})
    settings.general.encoder_bs4k_input_analysis_enabled = bs4k_input_analysis_enabled
    monkeypatch.setattr('app.streams.LiveEncodingTask.Config', lambda: settings)
    task = object.__new__(LiveEncodingTask)
    task._retry_count = 0
    task.live_stream = SimpleNamespace(
        quality='240p',
        stream_anchor_enabled=stream_anchor_enabled,
        encoding_options=SimpleNamespace(
            is_24fps_mode_enabled=is_24fps_mode_enabled,
            is_hevc_10bit_enabled=False,
            video_codec=video_codec,
            video_bit_depth=video_bit_depth,
            audio_codec=audio_codec,
        ),
    )
    return task


def test_stream_anchor_generation_id_is_nonzero_uint64(monkeypatch: pytest.MonkeyPatch) -> None:
    generated = iter((0, 0x123456789ABCDEF0))
    monkeypatch.setattr('app.streams.LiveEncodingTask.secrets.randbits', lambda _bits: next(generated))

    assert LiveEncodingTask.GenerateStreamAnchorGenerationID() == 0x123456789ABCDEF0


def test_radio_opus_uses_bridge_without_source_anchor_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """ラジオOpusはcodec Bridgeを使っても、最終化されないsource markerを付与しない。"""

    task = BuildEncodingTask(
        monkeypatch,
        stream_anchor_enabled=False,
        audio_codec='opus',
    )
    monkeypatch.setattr(task, 'GenerateStreamAnchorGenerationID', lambda: pytest.fail('must not generate'))

    assert task.IsTSCodecBridgeRequired(is_radiochannel=True) is True
    assert task.ResolveStreamAnchorGenerationID(False) is None
    assert '--stream-anchor-v1' not in task.BuildTSCodecBridgeOptions(is_radiochannel=True)


def test_bs4k_input_probe_size_is_symmetric_for_software_and_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BS4K強化解析のprobe sizeとretry増分を両FFmpeg8 builderで一致させる。"""

    enabled = BuildEncodingTask(monkeypatch, stream_anchor_enabled=True)
    enabled._retry_count = 2
    monkeypatch.setattr(RecordedPlaybackBackend, 'discoverRenderDevices', lambda _encoder: ['/dev/dri/renderD128'])
    software_options = enabled.buildFFmpegOptions('240p', 'BS4K', False)
    hardware_options = enabled.buildFFmpeg8HardwareOptions('240p', 'QSV', 'BS4K', False)

    assert software_options[software_options.index('-probesize') + 1] == '4000K'
    assert hardware_options[hardware_options.index('-probesize') + 1] == '4000K'
    assert software_options[software_options.index('-analyzeduration') + 1] == '1900000'
    assert hardware_options[hardware_options.index('-analyzeduration') + 1] == '1900000'

    disabled = BuildEncodingTask(
        monkeypatch,
        stream_anchor_enabled=True,
        bs4k_input_analysis_enabled=False,
    )
    assert all('-probesize' not in option for option in disabled.buildFFmpegOptions('240p', 'BS4K', False))
    assert '-probesize' not in disabled.buildFFmpeg8HardwareOptions('240p', 'QSV', 'BS4K', False)


@pytest.mark.parametrize('encoder_type', ['QSV', 'AMF'])
def test_ffmpeg8_hardware_reuses_exact_probe_selected_device(
    monkeypatch: pytest.MonkeyPatch,
    encoder_type: str,
) -> None:
    """targeted capabilityで成功したrender nodeをlive実行経路でも使う。"""

    task = BuildEncodingTask(monkeypatch, stream_anchor_enabled=True)
    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getSelectedDevice',
        lambda *_args: '/dev/dri/renderD130',
    )
    monkeypatch.setattr(
        RecordedPlaybackBackend,
        'discoverRenderDevices',
        lambda _encoder: pytest.fail('must reuse exact selected device'),
    )

    options = task.buildFFmpeg8HardwareOptions('240p', encoder_type, 'GR', False)  # type: ignore[arg-type]
    init_device = options[options.index('-init_hw_device') + 1]
    assert init_device.endswith(':/dev/dri/renderD130')


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


def test_ffmpeg8_input_keeps_decoder_frame_reordering(monkeypatch: pytest.MonkeyPatch) -> None:
    """低遅延入力でも decoder の B フレーム並べ替えを無効化しない。"""

    task = BuildEncodingTask(monkeypatch, stream_anchor_enabled=True)
    monkeypatch.setattr(RecordedPlaybackBackend, 'discoverRenderDevices', lambda _encoder: ['/dev/dri/renderD128'])

    software_options = task.buildFFmpegOptions('240p', 'GR', False)
    hardware_options = task.buildFFmpeg8HardwareOptions('240p', 'QSV', 'GR', False)
    radio_options = task.buildFFmpegOptionsForRadio()

    for options in (software_options, hardware_options, radio_options):
        assert 'nobuffer' in options
        assert 'low_delay' not in options
    assert software_options.count('0:v:0') == 1
    assert software_options.count('0:a?') == 1
    assert '0:a:1' not in software_options


def test_ffmpeg8_interlaced_filter_uses_top_field_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """ISDB のインターレース解除で top-field-first を明示する。"""

    task = BuildEncodingTask(
        monkeypatch,
        stream_anchor_enabled=True,
        is_24fps_mode_enabled=False,
    )
    monkeypatch.setattr(RecordedPlaybackBackend, 'discoverRenderDevices', lambda _encoder: ['/dev/dri/renderD128'])

    software_options = task.buildFFmpegOptions('240p', 'GR', False)
    hardware_options = task.buildFFmpeg8HardwareOptions('240p', 'QSV', 'GR', False)

    assert any('yadif=mode=0:parity=0:deint=1' in option for option in software_options)
    hardware_filter = hardware_options[hardware_options.index('-vf') + 1]
    assert 'yadif=mode=0:parity=0:deint=1' in hardware_filter


@pytest.mark.parametrize(
    ('encoder_type', 'video_codec', 'expected_encoder', 'expected_profile'),
    [
        ('QSV', 'avc', 'h264_qsv', 'high'),
        ('QSV', 'hevc', 'hevc_qsv', 'main'),
        ('QSV', 'vp9', 'vp9_qsv', 'profile0'),
        ('QSV', 'av1', 'av1_qsv', 'main'),
        ('NVENC', 'avc', 'h264_nvenc', 'high'),
        ('NVENC', 'hevc', 'hevc_nvenc', 'main'),
        ('NVENC', 'av1', 'av1_nvenc', None),
        ('AMF', 'avc', 'h264_amf', 'high'),
        ('AMF', 'hevc', 'hevc_amf', 'main'),
        ('AMF', 'av1', 'av1_amf', 'main'),
    ],
)
@pytest.mark.parametrize(('audio_codec', 'expected_audio_encoder'), [('aac', 'copy'), ('opus', 'libopus')])
def test_ffmpeg8_hardware_codec_tuple_options(
    monkeypatch: pytest.MonkeyPatch,
    encoder_type: str,
    video_codec: str,
    expected_encoder: str,
    expected_profile: str | None,
    audio_codec: str,
    expected_audio_encoder: str,
) -> None:
    """HW FFmpeg8 の対応tupleをexact codec・profile・reorder抑止で生成する。"""

    task = BuildEncodingTask(
        monkeypatch,
        stream_anchor_enabled=False,
        is_24fps_mode_enabled=False,
        video_codec=video_codec,
        audio_codec=audio_codec,
    )
    monkeypatch.setattr(RecordedPlaybackBackend, 'discoverRenderDevices', lambda _encoder: ['/dev/dri/renderD128'])

    options = task.buildFFmpeg8HardwareOptions('240p', encoder_type, 'GR', False)  # type: ignore[arg-type]

    assert options[options.index('-c:v') + 1] == expected_encoder
    assert options[options.index('-c:a') + 1] == expected_audio_encoder
    assert options[options.index('-bf') + 1] == '0'
    assert 'low_delay' not in options
    if audio_codec == 'opus':
        assert options[options.index('-af') + 1] == LiveEncodingTask.LIVE_TRANSCODE_AUDIO_FILTER
    else:
        assert '-af' not in options
    if encoder_type == 'QSV' and video_codec == 'avc':
        assert options[options.index('-look_ahead') + 1] == '0'
    elif encoder_type == 'QSV':
        assert '-look_ahead' not in options
    if expected_profile is None:
        assert '-profile:v' not in options
    else:
        assert options[options.index('-profile:v') + 1] == expected_profile
    hardware_filter = options[options.index('-vf') + 1]
    assert 'format=nv12' in hardware_filter
    if video_codec in ('vp9', 'av1') or audio_codec == 'opus':
        assert '-muxrate' in options
        assert '-pcr_period' in options


@pytest.mark.parametrize('encoder_type', ['QSV', 'NVENC', 'AMF'])
def test_ffmpeg8_hardware_oneseg_keeps_aac_stereo_normalization(
    monkeypatch: pytest.MonkeyPatch,
    encoder_type: str,
) -> None:
    """FFmpeg8 HW経路でもワンセグだけは従来のAAC stereo正規化を維持する。"""

    task = BuildEncodingTask(
        monkeypatch,
        stream_anchor_enabled=False,
        is_24fps_mode_enabled=False,
        video_codec='avc',
        audio_codec='aac',
    )
    monkeypatch.setattr(RecordedPlaybackBackend, 'discoverRenderDevices', lambda _encoder: ['/dev/dri/renderD128'])

    options = task.buildFFmpeg8HardwareOptions(  # type: ignore[arg-type]
        '240p',
        encoder_type,
        'GR',
        False,
        is_oneseg=True,
    )

    assert options[options.index('-c:a') + 1] == 'aac'
    assert options[options.index('-ac') + 1] == '2'
    assert options[options.index('-b:a') + 1] == '96K'
    assert options[options.index('-af') + 1] == LiveEncodingTask.LIVE_TRANSCODE_AUDIO_FILTER
    assert options[options.index('-r') + 1] == '15'
    assert '-copyts' in options
    assert '-fps_mode' not in options
    assert '-muxrate' not in options


def test_oneseg_disables_stream_anchor_bridge_option_even_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ワンセグは Stream Anchor 有効設定でも Bridge に --stream-anchor-v1 を渡さない。"""

    task = BuildEncodingTask(
        monkeypatch,
        stream_anchor_enabled=True,
        video_codec='av1',
        audio_codec='opus',
    )

    oneseg_options = task.BuildTSCodecBridgeOptions(is_oneseg=True)
    fullseg_options = task.BuildTSCodecBridgeOptions(is_oneseg=False)

    assert task.IsLiveStreamAnchorActive(is_oneseg=True) is False
    assert oneseg_options[:4] == ['--video-codec', 'av1', '--audio-codec', 'opus']
    assert '--stream-anchor-v1' not in oneseg_options
    assert '--transport-rate-kbps' in oneseg_options
    # 通常地デジでは従来どおり Anchor を付ける。
    assert '--stream-anchor-v1' in fullseg_options


def test_mmt_tlv_uses_libaribtlv_and_disables_source_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TLV は libaribtlv へ直結し、timed ID3 を含む全 data stream を維持する。"""

    task = BuildEncodingTask(
        monkeypatch,
        stream_anchor_enabled=True,
        video_codec='avc',
        audio_codec='aac',
    )
    monkeypatch.setattr(RecordedPlaybackBackend, 'discoverRenderDevices', lambda _encoder: ['/dev/dri/renderD128'])

    software_options = task.buildFFmpegOptions('240p', 'BS4K', False, is_mmt_tlv=True)
    hardware_options = task.buildFFmpeg8HardwareOptions(
        '240p',
        'QSV',
        'BS4K',
        False,
        is_mmt_tlv=True,
    )
    bridge_options = task.BuildTSCodecBridgeOptions(is_mmt_tlv=True)

    for options in (software_options, hardware_options):
        assert options[options.index('-f') + 1] == 'libaribtlv'
        assert options[options.index('-max_audio_channels') + 1] == '8'
        assert options.count('0:a?') == 1
        assert options.count('0:d?') == 1
        assert '-ac' not in options
    assert software_options[software_options.index('-acodec') + 1] == 'aac'
    assert hardware_options[hardware_options.index('-c:a') + 1] == 'aac'
    assert task.IsLiveStreamAnchorActive(is_mmt_tlv=True) is False
    assert '--stream-anchor-v1' not in bridge_options


def test_mpeg_ts_does_not_apply_isdb_s3_audio_channel_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """従来 MPEG-TS 入力へ libaribtlv 専用の音声上限を適用しない。"""

    task = BuildEncodingTask(
        monkeypatch,
        stream_anchor_enabled=True,
        video_codec='avc',
        audio_codec='aac',
    )
    monkeypatch.setattr(RecordedPlaybackBackend, 'discoverRenderDevices', lambda _encoder: ['/dev/dri/renderD128'])

    software_options = task.buildFFmpegOptions('240p', 'BS4K', False, is_mmt_tlv=False)
    hardware_options = task.buildFFmpeg8HardwareOptions(
        '240p',
        'QSV',
        'BS4K',
        False,
        is_mmt_tlv=False,
    )

    assert '-max_audio_channels' not in software_options
    assert '-max_audio_channels' not in hardware_options


def test_ffmpeg8_software_advanced_codec_uses_single_map_and_vbv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """software AV1/VP9は重複mapせず固定muxrate向けVBVを持つ。"""

    task = BuildEncodingTask(
        monkeypatch,
        stream_anchor_enabled=False,
        is_24fps_mode_enabled=False,
        video_codec='av1',
        audio_codec='opus',
    )

    options = task.buildFFmpegOptions('240p', 'GR', False)

    assert options.count('0:v:0') == 1
    assert options.count('0:a?') == 1
    assert options.count('0:d?') == 1
    assert '-bufsize' in options
    assert '-muxrate' in options
    assert options[options.index('-acodec') + 1] == 'libopus'
    assert options[options.index('-af') + 1] == LiveEncodingTask.LIVE_TRANSCODE_AUDIO_FILTER


def test_live_opus_preserves_input_channel_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    """通常・HW・ラジオの全ライブOpus経路で入力チャンネル数を固定しない。"""

    task = BuildEncodingTask(
        monkeypatch,
        stream_anchor_enabled=False,
        is_24fps_mode_enabled=False,
        video_codec='avc',
        audio_codec='opus',
    )
    monkeypatch.setattr(RecordedPlaybackBackend, 'discoverRenderDevices', lambda _encoder: ['/dev/dri/renderD128'])

    software_options = task.buildFFmpegOptions('240p', 'GR', False)
    hardware_options = task.buildFFmpeg8HardwareOptions('240p', 'QSV', 'GR', False)
    radio_options = task.buildFFmpegOptionsForRadio()

    for options in (software_options, hardware_options, radio_options):
        assert '-ac' not in options


@pytest.mark.parametrize('encoder_type', ['NVENC', 'AMF'])
def test_ffmpeg8_hardware_rejects_unsupported_vp9_backend(
    monkeypatch: pytest.MonkeyPatch,
    encoder_type: str,
) -> None:
    """backendが公開しないVP9は実行境界でも黙って別codecへ変更しない。"""

    task = BuildEncodingTask(
        monkeypatch,
        stream_anchor_enabled=False,
        video_codec='vp9',
    )

    with pytest.raises(RuntimeError, match='Unsupported FFmpeg 8 live encoder'):
        task.buildFFmpeg8HardwareOptions('240p', encoder_type, 'GR', False)  # type: ignore[arg-type]


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
    task.live_stream = SimpleNamespace(
        stream_anchor_enabled=True,
        encoding_options=StreamEncodingOptions(),
    )

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
