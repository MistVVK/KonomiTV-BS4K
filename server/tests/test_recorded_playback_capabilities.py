import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI, Query
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient

from app import schemas
from app.metadata.RecordedPlaybackIndexer import (
    RecordedPlaybackIndexAnalysisError,
    RecordedPlaybackIndexer,
)
from app.models.RecordedVideo import RecordedVideo
from app.routers.VideoStreamsRouter import VideoBitDepthQuery
from app.streams.RecordedFMP4Stream import RecordedFMP4Stream
from app.streams.RecordedPlaybackCapabilities import (
    RecordedPlaybackBackend,
    RecordedPlaybackCapabilityProbe,
)


def test_recorded_playback_capability_matrix() -> None:
    """能力キーの対応候補が設計どおりであることを確認する。"""

    assert RecordedPlaybackBackend.isCombinationSupported('FFmpeg', 'avc', 8) is True
    assert RecordedPlaybackBackend.isCombinationSupported('FFmpeg', 'avc', 10) is False
    assert RecordedPlaybackBackend.isCombinationSupported('QSVEncC', 'vp9', 10) is True
    assert RecordedPlaybackBackend.isCombinationSupported('NVEncC', 'vp9', 8) is False
    assert RecordedPlaybackBackend.isCombinationSupported('VCEEncC', 'vp9', 10) is False


def test_recorded_playback_fixed_profiles() -> None:
    """bit depthから固定profileとpixel formatが一意に決まることを確認する。"""

    assert RecordedPlaybackBackend.getCodecSpec('avc', 8).profile == 'High'
    assert RecordedPlaybackBackend.getCodecSpec('hevc', 10).profile == 'Main 10'
    assert RecordedPlaybackBackend.getCodecSpec('vp9', 10).profile == 'Profile 2'
    assert RecordedPlaybackBackend.getCodecSpec('av1', 10).pixel_format == 'yuv420p10le'


def test_recorded_playback_cpu_encoders_use_realtime_tuning() -> None:
    """CPUの各encoderがオンデマンド再生向け速度設定を共有することを確認する。"""

    assert RecordedPlaybackBackend.getTuningArguments('FFmpeg', 'avc') == ['-preset', 'veryfast']
    assert RecordedPlaybackBackend.getTuningArguments('FFmpeg', 'hevc') == ['-preset', 'veryfast']
    assert '-deadline' in RecordedPlaybackBackend.getTuningArguments('FFmpeg', 'vp9')
    assert RecordedPlaybackBackend.getTuningArguments('FFmpeg', 'av1') == [
        '-usage', 'realtime', '-cpu-used', '8', '-row-mt', '1',
    ]
    assert RecordedPlaybackBackend.getTuningArguments('NVEncC', 'hevc') == []


def test_recorded_playback_qsv_uses_bundled_libva() -> None:
    """QSV用FFmpeg 8が同梱libvaとiHD driverを同じABIで使うことを確認する。"""

    environment = RecordedPlaybackBackend.getEnvironment('QSVEncC')
    assert environment['LIBVA_DRIVER_NAME'] == 'iHD'
    assert environment['LIBVA_DRIVERS_PATH'].endswith('/Library/dri')
    assert environment['LD_LIBRARY_PATH'].split(':')[0].endswith('/Library')


def test_recorded_playback_amd_prefers_proprietary_vaapi_driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AMD runtime がある構成では /opt 側の radeonsi を選ぶ。"""

    proprietary = tmp_path / 'proprietary-dri'
    mesa = tmp_path / 'mesa-dri'
    proprietary.mkdir()
    mesa.mkdir()
    (proprietary / 'radeonsi_drv_video.so').write_bytes(b'driver')
    monkeypatch.setattr(RecordedPlaybackBackend, '_AMD_PROPRIETARY_VAAPI_DRIVER_DIRECTORY', proprietary)
    monkeypatch.setattr(RecordedPlaybackBackend, '_AMD_MESA_VAAPI_DRIVER_DIRECTORY', mesa)

    environment = RecordedPlaybackBackend.getEnvironment('VCEEncC')
    assert environment['LIBVA_DRIVER_NAME'] == 'radeonsi'
    assert environment['LIBVA_DRIVERS_PATH'] == str(proprietary)
    assert environment['LD_LIBRARY_PATH'].split(':')[0].endswith('/Library')


def test_recorded_playback_amd_uses_mesa_without_proprietary_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INSTALL_AMD=false 構成では Mesa radeonsi を選ぶ。"""

    proprietary = tmp_path / 'proprietary-dri'
    mesa = tmp_path / 'mesa-dri'
    proprietary.mkdir()
    mesa.mkdir()
    (mesa / 'radeonsi_drv_video.so').write_bytes(b'driver')
    monkeypatch.setattr(RecordedPlaybackBackend, '_AMD_PROPRIETARY_VAAPI_DRIVER_DIRECTORY', proprietary)
    monkeypatch.setattr(RecordedPlaybackBackend, '_AMD_MESA_VAAPI_DRIVER_DIRECTORY', mesa)

    environment = RecordedPlaybackBackend.getEnvironment('VCEEncC')
    assert environment['LIBVA_DRIVER_NAME'] == 'radeonsi'
    assert environment['LIBVA_DRIVERS_PATH'] == str(mesa)
    assert environment['LD_LIBRARY_PATH'].split(':')[0].endswith('/Library')


def test_recorded_playback_amd_probe_uses_vaapi_download() -> None:
    """AMF能力検査が実再生と同じVAAPIからsystem memoryへの境界を通ることを確認する。"""

    command = RecordedPlaybackBackend.buildProbeCommand(
        'VCEEncC',
        'hevc',
        10,
        Path('/tmp/probe.mp4'),
        '/dev/dri/renderD130',
    )
    command_text = ' '.join(command)
    assert command[0].endswith('/FFmpeg8/ffmpeg8-amd.sh')
    assert 'vaapi=recorded_vaapi:/dev/dri/renderD130' in command_text
    assert 'scale_vaapi' in command_text
    assert 'hwdownload' in command_text
    assert 'format=p010le' in command_text
    assert 'hevc_amf' in command_text


def test_recorded_playback_qsv_probe_keeps_hardware_pixel_format() -> None:
    """QSV frameをsoftware pixel formatへ戻す出力指定がないことを確認する。"""

    command = RecordedPlaybackBackend.buildProbeCommand(
        'QSVEncC',
        'hevc',
        10,
        Path('/tmp/probe.mp4'),
        '/dev/dri/renderD128',
    )
    assert '-pix_fmt' not in command

    vp9_command = RecordedPlaybackBackend.buildProbeCommand(
        'QSVEncC',
        'vp9',
        10,
        Path('/tmp/probe.mp4'),
        '/dev/dri/renderD128',
    )
    assert vp9_command[vp9_command.index('-profile:v') + 1] == 'profile2'


def test_recorded_playback_nvenc_probe_keeps_cuda_frames() -> None:
    """NVENCがhwupload後のCUDA frameをsoftware形式へ戻さないことを確認する。"""

    command = RecordedPlaybackBackend.buildProbeCommand(
        'NVEncC',
        'hevc',
        10,
        Path('/tmp/probe.mp4'),
        None,
    )
    assert command[command.index('-pix_fmt') + 1] == 'cuda'

    av1_command = RecordedPlaybackBackend.buildProbeCommand(
        'NVEncC',
        'av1',
        10,
        Path('/tmp/probe.mp4'),
        None,
    )
    assert '-profile:v' not in av1_command


def test_recorded_playback_non_ts_fallback_keeps_gpu_encoder() -> None:
    """software decode再試行がGPU encoderをCPUへ降格させないことを確認する。"""

    command = [
        '/opt/FFmpeg8/ffmpeg', '-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda',
        '-i', 'video.mkv', '-vf', 'scale_cuda=1920:1080', '-c:v', 'hevc_nvenc', 'pipe:1',
    ]
    fallback = RecordedFMP4Stream.buildSoftwareDecodeFallback(
        command,
        'NVEncC',
        'p010le',
    )
    assert '-hwaccel' not in fallback
    assert 'format=p010le,hwupload_cuda,scale_cuda=1920:1080' in fallback
    assert 'hevc_nvenc' in fallback
    assert 'libx265' not in fallback


def test_recorded_playback_non_ts_fallback_only_retries_hardware_decode_failure() -> None:
    """GPU encode/filter失敗をsoftware decode再試行へ誤分類しないことを確認する。"""

    assert RecordedFMP4Stream.shouldRetryWithSoftwareDecode(
        b'Device setup failed for decoder on input stream #0:0',
    ) is True
    assert RecordedFMP4Stream.shouldRetryWithSoftwareDecode(
        b'No device available for decoder: device type cuda needed for codec hevc.',
    ) is True
    assert RecordedFMP4Stream.shouldRetryWithSoftwareDecode(
        b'Error while opening encoder - maybe incorrect parameters such as bit_rate, rate, width or height.',
    ) is False
    assert RecordedFMP4Stream.shouldRetryWithSoftwareDecode(
        b'Impossible to convert between the formats supported by the filter graph.',
    ) is False


def test_recorded_playback_capability_failure_reason() -> None:
    """GPU runtimeとfilterの失敗が安定理由コードへ分類されることを確認する。"""

    assert RecordedPlaybackCapabilityProbe.classifyFailure(
        'Cannot load libnvidia-encode.so.1',
    ) == 'DeviceUnavailable'
    assert RecordedPlaybackCapabilityProbe.classifyFailure(
        'Device creation failed: -5.',
    ) == 'DeviceInitializationFailed'
    assert RecordedPlaybackCapabilityProbe.classifyFailure(
        "No such filter: 'bwdif_cuda'",
    ) == 'FilterUnavailable'


def test_recorded_playback_timeline_normalizes_mpeg_ts_pts() -> None:
    """放送波由来の絶対PTSを録画先頭0秒基準へ正規化することを確認する。"""

    timeline = RecordedPlaybackIndexer._RecordedPlaybackIndexer__buildTimeline(  # pyright: ignore[reportPrivateUsage]
        {
            'format': {'start_time': '9782.970422', 'duration': '609.848745'},
            'streams': [{
                'index': 0,
                'id': '0x100',
                'codec_type': 'video',
                'codec_name': 'hevc',
                'profile': 'Main 10',
                'start_time': '9782.970422',
                'duration': '609.241967',
                'width': 1440,
                'height': 1080,
                'sample_aspect_ratio': '4:3',
                'display_aspect_ratio': '16:9',
                'pix_fmt': 'yuv420p10le',
                'avg_frame_rate': '60000/1001',
            }],
        },
        609.848745,
    )
    assert timeline[0]['start_time'] == 0.0
    assert timeline[0]['end_time'] == 609.241967
    assert timeline[0]['sample_aspect_ratio'] == '4:3'
    assert timeline[0]['display_aspect_ratio'] == '16:9'


def test_recorded_video_exposes_representative_aspect_ratios() -> None:
    """一覧・単体APIのどちらでも代表映像区間のSAR/DARを公開する。"""

    recorded_video = schemas.RecordedVideo.model_construct(video_stream_timeline=[{
        'start_time': 0.0,
        'end_time': 65.0,
        'sample_aspect_ratio': '1:1',
        'display_aspect_ratio': '16:9',
    }])

    assert recorded_video.video_sample_aspect_ratio == '1:1'
    assert recorded_video.video_display_aspect_ratio == '16:9'


def test_recorded_index_backfills_existing_arib_caption_track(monkeypatch) -> None:
    """既存録画のbin_data字幕をindex更新時にPMT情報から補完する。"""

    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.TSKeyFrameSeeker.findARIBCaptionPIDs',
        lambda _path: {0x0138},
    )
    recorded_video = cast(RecordedVideo, SimpleNamespace(
        container_format='MPEG-TS',
        subtitle_tracks=[],
    ))
    probe = {'streams': [
        {'index': 3, 'id': '0x138', 'codec_type': 'data', 'tags': {'language': 'jpn'}},
        {'index': 4, 'id': '0x139', 'codec_type': 'data'},
    ]}

    tracks = RecordedPlaybackIndexer._RecordedPlaybackIndexer__backfillARIBSubtitleTracks(  # pyright: ignore[reportPrivateUsage]
        recorded_video,
        probe,
        Path('/recording.ts'),
    )

    assert tracks == [{
        'index': 1,
        'stream_index': 3,
        'codec': 'arib_caption',
        'language': 'jpn',
        'title': None,
        'pid': 0x0138,
    }]


def test_recorded_video_timeline_detects_same_stream_configuration_change(monkeypatch) -> None:
    """単一FFprobeから映像構成変化と音声消失区間を同時に区間化する。"""

    class FakeStream:
        """FFprobeの非同期stdout/stderrを最小限再現する。"""

        def __init__(self, lines: list[bytes]) -> None:
            self.lines = iter(lines)

        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            try:
                return next(self.lines)
            except StopIteration as ex:
                raise StopAsyncIteration from ex

        async def read(self, _size: int = -1) -> bytes:
            return b''

        async def readline(self) -> bytes:
            try:
                return next(self.lines)
            except StopIteration:
                return b''

    class FakeProcess:
        """成功するFFprobe子プロセスを再現する。"""

        stdout = FakeStream([
            b'frame|media_type=video|stream_index=0|pts_time=100.0|width=320|height=180|pix_fmt=yuv420p'
            b'|interlaced_frame=0\n',
            b'frame|media_type=audio|stream_index=1|pts_time=100.0|channels=1|channel_layout=mono\n',
            b'frame|media_type=audio|stream_index=1|pts_time=100.021333|channels=1|channel_layout=mono\n',
            b'frame|media_type=video|stream_index=0|pts_time=102.0|width=640|height=360|pix_fmt=yuv420p'
            b'|interlaced_frame=0'
            b'|side_data|side_data_type=Mastering display metadata|max_luminance=1000/1\n',
            b'frame|media_type=audio|stream_index=1|pts_time=102.0|channels=2|channel_layout=stereo\n',
        ])
        stderr = FakeStream([])

        async def wait(self) -> int:
            return 0

    command: list[str] = []

    async def CreateFakeProcess(*args, **_kwargs):
        """テスト用FFprobeプロセスを返す。"""

        command.extend(cast(tuple[str, ...], args))
        return FakeProcess()

    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.asyncio.create_subprocess_exec',
        CreateFakeProcess,
    )
    base_timeline = [{
        'start_time': 0.0,
        'end_time': 4.0,
        'pid': 256,
        'stream_index': 0,
        'codec': 'h264',
        'profile': 'High',
        'width': 320,
        'height': 180,
        'frame_rate': 30.0,
        'scan_type': 'Progressive',
        'bit_depth': 8,
        'color_range': None,
        'color_space': None,
        'color_primaries': None,
        'color_transfer': None,
        'mastering_display_metadata': None,
        'content_light_level': None,
    }]

    audio_tracks = [{
        'index': 1,
        'codec': 'AAC-LC',
        'channel': 'Dual Mono',
        'sampling_rate': 48_000,
        'language': '日本語+英語',
        'stream_index': 1,
        'channel_layout': 'stereo',
        'is_dual_mono': True,
    }]
    timeline, audio_timeline, discovered_audio_tracks = asyncio.run(
        RecordedPlaybackIndexer._RecordedPlaybackIndexer__buildFrameTimelines(  # pyright: ignore[reportPrivateUsage]
            999,
            Path('/recording.ts'),
            4.0,
            100.0,
            base_timeline,
            audio_tracks,
            {},
            0,
        )
    )

    assert '-skip_frame:v' in command
    assert command[command.index('-skip_frame:v') + 1] == 'nokey'
    assert '-select_streams' not in command
    assert timeline is not None
    assert [(entry['width'], entry['height']) for entry in timeline] == [(320, 180), (640, 360)]
    assert timeline[0]['end_time'] == 2.0
    assert timeline[1]['start_time'] == 2.0
    assert timeline[1]['mastering_display_metadata'] == {
        'side_data_type': 'Mastering display metadata',
        'max_luminance': '1000/1',
    }
    assert audio_timeline is not None
    assert audio_timeline[0]['tracks'][0]['channel'] == 'Monaural'
    assert audio_timeline[1]['tracks'] == []
    assert audio_timeline[2]['tracks'][0]['channel'] == 'Dual Mono'
    assert discovered_audio_tracks == audio_tracks


def test_recorded_video_frame_probe_failure_does_not_publish_ready(monkeypatch) -> None:
    """全編フレーム走査失敗を暫定値へフォールバックせず、機械判定可能な失敗として返す。"""

    class FakeStream:
        def __init__(self) -> None:
            self.has_read = False

        async def readline(self) -> bytes:
            return b''

        async def read(self, _size: int = -1) -> bytes:
            if self.has_read:
                return b''
            self.has_read = True
            return b'broken transport stream'

    class FakeProcess:
        stdout = FakeStream()
        stderr = FakeStream()

        async def wait(self) -> int:
            return 1

    async def CreateFakeProcess(*_args, **_kwargs):
        return FakeProcess()

    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.asyncio.create_subprocess_exec',
        CreateFakeProcess,
    )

    with pytest.raises(RecordedPlaybackIndexAnalysisError) as ex_info:
        asyncio.run(
            RecordedPlaybackIndexer._RecordedPlaybackIndexer__buildFrameTimelines(  # pyright: ignore[reportPrivateUsage]
                999,
                Path('/recording.ts'),
                4.0,
                100.0,
                [],
                [],
                {},
                0,
            )
        )

    assert ex_info.value.error_code == 'FrameProbeFailed'


def test_recorded_audio_timeline_detects_dual_mono_transition() -> None:
    """同一streamのmonoからstereoへの変化をDual Mono構成として反映することを確認する。"""

    base_track = {
        'index': 1,
        'codec': 'AAC-LC',
        'channel': 'Dual Mono',
        'sampling_rate': 48_000,
        'language': '日本語+英語',
        'stream_index': 1,
        'channel_layout': 'stereo',
        'is_dual_mono': True,
    }
    mono = RecordedPlaybackIndexer._RecordedPlaybackIndexer__buildTimelineAudioTrack(  # pyright: ignore[reportPrivateUsage]
        base_track,
        1,
        'mono',
    )
    dual_mono = RecordedPlaybackIndexer._RecordedPlaybackIndexer__buildTimelineAudioTrack(  # pyright: ignore[reportPrivateUsage]
        base_track,
        2,
        'stereo',
    )
    assert mono['channel'] == 'Monaural'
    assert mono['language'] == '日本語'
    assert mono['is_dual_mono'] is False
    assert dual_mono['channel'] == 'Dual Mono'
    assert dual_mono['language'] == '日本語+英語'
    assert dual_mono['is_dual_mono'] is True


def test_recorded_audio_timeline_labels_51_layout() -> None:
    """6チャンネルの5.1 layoutを汎用的なチャンネル数ではなく5.1chと表示する。"""

    base_track = {
        'index': 1,
        'codec': 'AAC-LC',
        'channel': '6 Channels',
        'sampling_rate': 48_000,
        'language': '日本語',
        'stream_index': 2,
        'channel_layout': '5.1',
        'is_dual_mono': False,
    }
    surround = RecordedPlaybackIndexer._RecordedPlaybackIndexer__buildTimelineAudioTrack(  # pyright: ignore[reportPrivateUsage]
        base_track,
        6,
        '5.1',
    )

    assert surround['channel'] == '5.1ch'
    assert surround['channel_layout'] == '5.1'


def test_video_bit_depth_query_accepts_browser_query_string() -> None:
    """ブラウザが送る文字列の8/10を整数enumへ変換できることを確認する。"""

    app = FastAPI()

    @app.get('/bit-depth')
    async def GetBitDepth(bit_depth: VideoBitDepthQuery = Query()) -> int:
        return int(bit_depth)

    async def Request(bit_depth: str):
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            return await client.get('/bit-depth', params={'bit_depth': bit_depth})

    assert asyncio.run(Request('8')).json() == 8
    assert asyncio.run(Request('10')).json() == 10
    assert asyncio.run(Request('9')).status_code == 422
