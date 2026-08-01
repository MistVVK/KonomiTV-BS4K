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
    RecordedPlaybackBitDepth,
    RecordedPlaybackCapability,
    RecordedPlaybackCapabilityProbe,
    RecordedPlaybackEncoder,
    RecordedPlaybackVideoCodec,
)


def _reset_recorded_playback_probe_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """能力probeのプロセス内状態を独立したasyncio loop向けに初期化する。"""

    monkeypatch.setattr(RecordedPlaybackCapabilityProbe, '_result', None)
    monkeypatch.setattr(RecordedPlaybackCapabilityProbe, '_individual_results', {})
    monkeypatch.setattr(RecordedPlaybackCapabilityProbe, '_signature', None)
    monkeypatch.setattr(RecordedPlaybackCapabilityProbe, '_signature_generation', 0)
    monkeypatch.setattr(RecordedPlaybackCapabilityProbe, '_inflight_tasks', {})
    monkeypatch.setattr(RecordedPlaybackCapabilityProbe, '_matrix_inflight_tasks', {})
    monkeypatch.setattr(RecordedPlaybackCapabilityProbe, '_selected_devices', {})
    monkeypatch.setattr(RecordedPlaybackCapabilityProbe, '_lock', asyncio.Lock())
    monkeypatch.setattr(RecordedPlaybackCapabilityProbe, '_probe_semaphore', asyncio.Semaphore(2))


def test_recorded_playback_capability_matrix() -> None:
    """能力キーの対応候補が設計どおりであることを確認する。"""

    assert RecordedPlaybackBackend.isCombinationSupported('FFmpeg', 'avc', 8) is True
    assert RecordedPlaybackBackend.isCombinationSupported('FFmpeg', 'avc', 10) is False
    assert RecordedPlaybackBackend.isCombinationSupported('QSV', 'vp9', 10) is True
    assert RecordedPlaybackBackend.isCombinationSupported('NVENC', 'vp9', 8) is False
    assert RecordedPlaybackBackend.isCombinationSupported('AMF', 'vp9', 10) is False


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
    assert RecordedPlaybackBackend.getTuningArguments('NVENC', 'hevc') == []


def test_recorded_playback_qsv_uses_bundled_libva() -> None:
    """QSV用FFmpeg 8が同梱libvaとiHD driverを同じABIで使うことを確認する。"""

    environment = RecordedPlaybackBackend.getEnvironment('QSV')
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

    environment = RecordedPlaybackBackend.getEnvironment('AMF')
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

    environment = RecordedPlaybackBackend.getEnvironment('AMF')
    assert environment['LIBVA_DRIVER_NAME'] == 'radeonsi'
    assert environment['LIBVA_DRIVERS_PATH'] == str(mesa)
    assert environment['LD_LIBRARY_PATH'].split(':')[0].endswith('/Library')


def test_recorded_playback_amd_probe_uses_vaapi_download() -> None:
    """AMF能力検査が実再生と同じVAAPIからsystem memoryへの境界を通ることを確認する。"""

    command = RecordedPlaybackBackend.buildProbeCommand(
        'AMF',
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
        'QSV',
        'hevc',
        10,
        Path('/tmp/probe.mp4'),
        '/dev/dri/renderD128',
    )
    assert '-pix_fmt' not in command

    vp9_command = RecordedPlaybackBackend.buildProbeCommand(
        'QSV',
        'vp9',
        10,
        Path('/tmp/probe.mp4'),
        '/dev/dri/renderD128',
    )
    assert vp9_command[vp9_command.index('-profile:v') + 1] == 'profile2'


def test_recorded_playback_nvenc_probe_keeps_cuda_frames() -> None:
    """NVENCがhwupload後のCUDA frameをsoftware形式へ戻さないことを確認する。"""

    command = RecordedPlaybackBackend.buildProbeCommand(
        'NVENC',
        'hevc',
        10,
        Path('/tmp/probe.mp4'),
        None,
    )
    assert command[command.index('-pix_fmt') + 1] == 'cuda'

    av1_command = RecordedPlaybackBackend.buildProbeCommand(
        'NVENC',
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
        'NVENC',
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


def test_recorded_playback_capability_selects_device_per_codec_and_bit_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧GPUのAVC成功後も全deviceを試し、AV1は新GPUへ割り当てる。"""

    selected_codec = 'avc'

    class FakeProcess:
        def __init__(self, returncode: int, stdout: bytes = b'', stderr: bytes = b'') -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

        async def communicate(self) -> tuple[bytes, bytes]:
            return self.stdout, self.stderr

    async def CreateProcess(*args, **_kwargs):
        nonlocal selected_codec
        if args[0] == 'encode':
            device = str(args[1])
            selected_codec = str(args[2])
            if device == '/dev/dri/renderD128' and selected_codec == 'av1':
                return FakeProcess(1, stderr=b'unsupported codec')
            return FakeProcess(0)
        codec_name = {'avc': 'h264', 'av1': 'av1'}[selected_codec]
        profile = 'High' if selected_codec == 'avc' else 'Main'
        return FakeProcess(0, stdout=(
            f'{{"streams":[{{"codec_name":"{codec_name}","nb_read_frames":"6",'
            f'"pix_fmt":"yuv420p","profile":"{profile}"}}]}}'
        ).encode())

    monkeypatch.setattr(Path, 'is_file', lambda _path: True)
    monkeypatch.setattr(
        RecordedPlaybackBackend,
        'discoverRenderDevices',
        lambda _encoder: ['/dev/dri/renderD128', '/dev/dri/renderD129'],
    )
    monkeypatch.setattr(
        RecordedPlaybackBackend,
        'buildProbeCommand',
        lambda _encoder, codec, _bit_depth, _output_path, device: ['encode', str(device), codec],
    )
    monkeypatch.setattr(
        'app.streams.RecordedPlaybackCapabilities.asyncio.create_subprocess_exec',
        CreateProcess,
    )
    RecordedPlaybackCapabilityProbe._selected_devices.clear()  # pyright: ignore[reportPrivateUsage]

    async def Verify() -> None:
        avc = await RecordedPlaybackCapabilityProbe._RecordedPlaybackCapabilityProbe__probeOne(  # pyright: ignore[reportPrivateUsage]
            'QSV',
            'avc',
            8,
        )
        av1 = await RecordedPlaybackCapabilityProbe._RecordedPlaybackCapabilityProbe__probeOne(  # pyright: ignore[reportPrivateUsage]
            'QSV',
            'av1',
            8,
        )
        assert avc.available is True
        assert av1.available is True
        assert RecordedPlaybackCapabilityProbe.getSelectedDevice('QSV', 'avc', 8) == \
            '/dev/dri/renderD128'
        assert RecordedPlaybackCapabilityProbe.getSelectedDevice('QSV', 'av1', 8) == \
            '/dev/dri/renderD129'

    asyncio.run(Verify())


def test_recorded_playback_exact_probe_is_reused_by_full_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """要求した1組だけを先行probeし、後続の全行列でも同じ結果を再実行しない。"""

    calls: list[tuple[str, str, int]] = []

    async def GetSignature(
        _cls: type[RecordedPlaybackCapabilityProbe],
    ) -> str:
        """固定したテスト署名を返す。"""

        return 'exact-probe-signature'

    async def ProbeOne(
        _cls: type[RecordedPlaybackCapabilityProbe],
        encoder: str,
        codec: str,
        bit_depth: int,
    ) -> RecordedPlaybackCapability:
        """呼び出しキーを記録して利用可能な能力を返す。"""

        calls.append((encoder, codec, bit_depth))
        return RecordedPlaybackCapability(
            cast(RecordedPlaybackEncoder, encoder),
            cast(RecordedPlaybackVideoCodec, codec),
            cast(RecordedPlaybackBitDepth, bit_depth),
            True,
            'test',
            None,
        )

    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        '_RecordedPlaybackCapabilityProbe__getSignature',
        classmethod(GetSignature),
    )
    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        '_RecordedPlaybackCapabilityProbe__probeOne',
        classmethod(ProbeOne),
    )
    _reset_recorded_playback_probe_state(monkeypatch)

    async def Verify() -> None:
        exact = await RecordedPlaybackCapabilityProbe.getCapability(
            'QSV',
            'av1',
            10,
        )
        matrix = await RecordedPlaybackCapabilityProbe.getCapabilities()
        assert exact is next(
            capability
            for capability in matrix
            if (
                capability.encoder,
                capability.codec,
                capability.bit_depth,
            ) == ('QSV', 'av1', 10)
        )

    asyncio.run(Verify())

    assert calls.count(('QSV', 'av1', 10)) == 1
    assert len(calls) == 32


def test_recorded_playback_same_key_shares_inflight_probe_without_holding_state_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一署名・同一キーの同時要求はlock外の1実probeだけを共有する。"""

    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def GetSignature(
        _cls: type[RecordedPlaybackCapabilityProbe],
    ) -> str:
        return 'shared-inflight-signature'

    async def ProbeOne(
        _cls: type[RecordedPlaybackCapabilityProbe],
        encoder: RecordedPlaybackEncoder,
        codec: RecordedPlaybackVideoCodec,
        bit_depth: RecordedPlaybackBitDepth,
    ) -> RecordedPlaybackCapability:
        nonlocal calls
        calls += 1
        assert RecordedPlaybackCapabilityProbe._lock.locked() is False  # pyright: ignore[reportPrivateUsage]
        started.set()
        await release.wait()
        return RecordedPlaybackCapability(encoder, codec, bit_depth, True, 'Main', None)

    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        '_RecordedPlaybackCapabilityProbe__getSignature',
        classmethod(GetSignature),
    )
    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        '_RecordedPlaybackCapabilityProbe__probeOne',
        classmethod(ProbeOne),
    )
    _reset_recorded_playback_probe_state(monkeypatch)

    async def Verify() -> None:
        first = asyncio.create_task(RecordedPlaybackCapabilityProbe.getCapability('QSV', 'av1', 10))
        await asyncio.wait_for(started.wait(), timeout=1)
        second = asyncio.create_task(RecordedPlaybackCapabilityProbe.getCapability('QSV', 'av1', 10))
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result is second_result

    asyncio.run(Verify())
    assert calls == 1


def test_recorded_playback_full_matrix_allows_exact_probe_and_limits_parallelism(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全行列中も同backend末尾キーのexact要求を先行させ、実probeを2件に制限する。"""

    first_key = ('FFmpeg', 'avc', 8)
    exact_key = ('FFmpeg', 'av1', 10)
    calls: list[tuple[str, str, int]] = []
    active = 0
    maximum_active = 0
    first_started = asyncio.Event()
    exact_started = asyncio.Event()
    release_first = asyncio.Event()

    async def GetSignature(
        _cls: type[RecordedPlaybackCapabilityProbe],
    ) -> str:
        return 'bounded-matrix-signature'

    async def ProbeOne(
        _cls: type[RecordedPlaybackCapabilityProbe],
        encoder: RecordedPlaybackEncoder,
        codec: RecordedPlaybackVideoCodec,
        bit_depth: RecordedPlaybackBitDepth,
    ) -> RecordedPlaybackCapability:
        nonlocal active, maximum_active
        key = (encoder, codec, bit_depth)
        calls.append(key)
        active += 1
        maximum_active = max(maximum_active, active)
        try:
            if key == first_key:
                first_started.set()
                await release_first.wait()
            if key == exact_key:
                exact_started.set()
            await asyncio.sleep(0)
            return RecordedPlaybackCapability(encoder, codec, bit_depth, True, 'test', None)
        finally:
            active -= 1

    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        '_RecordedPlaybackCapabilityProbe__getSignature',
        classmethod(GetSignature),
    )
    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        '_RecordedPlaybackCapabilityProbe__probeOne',
        classmethod(ProbeOne),
    )
    _reset_recorded_playback_probe_state(monkeypatch)

    async def Verify() -> None:
        first_matrix = asyncio.create_task(RecordedPlaybackCapabilityProbe.getCapabilities())
        await asyncio.wait_for(first_started.wait(), timeout=1)
        second_matrix = asyncio.create_task(RecordedPlaybackCapabilityProbe.getCapabilities())
        exact = asyncio.create_task(RecordedPlaybackCapabilityProbe.getCapability(*exact_key))
        await asyncio.wait_for(exact_started.wait(), timeout=1)
        exact_result = await asyncio.wait_for(exact, timeout=1)
        assert (exact_result.encoder, exact_result.codec, exact_result.bit_depth) == exact_key
        assert first_matrix.done() is False
        release_first.set()
        first_result, second_result = await asyncio.gather(first_matrix, second_matrix)
        assert first_result is second_result
        assert len(first_result) == 32

    asyncio.run(Verify())

    assert calls.count(exact_key) == 1
    assert len(calls) == 32
    assert maximum_active <= 2


def test_recorded_playback_signature_change_discards_stale_inflight_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧署名probe完了と新署名要求が競合しても旧結果を現世代cacheへ混入させない。"""

    signature = 'signature-1'
    calls: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def GetSignature(
        _cls: type[RecordedPlaybackCapabilityProbe],
    ) -> str:
        return signature

    async def ProbeOne(
        _cls: type[RecordedPlaybackCapabilityProbe],
        encoder: RecordedPlaybackEncoder,
        codec: RecordedPlaybackVideoCodec,
        bit_depth: RecordedPlaybackBitDepth,
    ) -> RecordedPlaybackCapability:
        probe_signature = signature
        calls.append(probe_signature)
        context = RecordedPlaybackCapabilityProbe._probe_context.get()  # pyright: ignore[reportPrivateUsage]
        assert context is not None
        context.selected_device = f'/dev/dri/{probe_signature}'
        if probe_signature == 'signature-1':
            first_started.set()
            await release_first.wait()
        return RecordedPlaybackCapability(
            encoder,
            codec,
            bit_depth,
            True,
            probe_signature,
            None,
        )

    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        '_RecordedPlaybackCapabilityProbe__getSignature',
        classmethod(GetSignature),
    )
    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        '_RecordedPlaybackCapabilityProbe__probeOne',
        classmethod(ProbeOne),
    )
    _reset_recorded_playback_probe_state(monkeypatch)

    async def Verify() -> None:
        nonlocal signature
        first = asyncio.create_task(RecordedPlaybackCapabilityProbe.getCapability('QSV', 'av1', 10))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        signature = 'signature-2'
        second = asyncio.create_task(RecordedPlaybackCapabilityProbe.getCapability('QSV', 'av1', 10))
        await asyncio.sleep(0)
        release_first.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result is second_result
        assert first_result.profile == 'signature-2'
        assert RecordedPlaybackCapabilityProbe._signature == 'signature-2'  # pyright: ignore[reportPrivateUsage]
        cached = RecordedPlaybackCapabilityProbe._individual_results[('QSV', 'av1', 10)]  # pyright: ignore[reportPrivateUsage]
        assert cached is first_result
        assert RecordedPlaybackCapabilityProbe.getSelectedDevice('QSV', 'av1', 10) == \
            '/dev/dri/signature-2'

    asyncio.run(Verify())
    assert calls == ['signature-1', 'signature-2']


def test_recorded_playback_probe_timeout_kills_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停止したencoder/ffprobeを能力APIへ無期限に残さず強制回収する。"""

    class HangingProcess:
        """終了しないprobe subprocessを再現する。"""

        returncode: int | None = None
        killed = False
        waited = False

        async def communicate(self) -> tuple[bytes, bytes]:
            """呼び出し元のtimeoutまで終了しない。"""

            await asyncio.Event().wait()
            return b'', b''

        def kill(self) -> None:
            """強制終了を記録する。"""

            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            """回収待ちを記録する。"""

            self.waited = True
            return -9

    process = HangingProcess()
    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        '_probe_timeout_seconds',
        0.001,
    )

    async def Verify() -> None:
        with pytest.raises(TimeoutError):
            await RecordedPlaybackCapabilityProbe.\
                _RecordedPlaybackCapabilityProbe__communicateWithTimeout(  # pyright: ignore[reportPrivateUsage]
                    cast(asyncio.subprocess.Process, process),
                )

    asyncio.run(Verify())

    assert process.killed is True
    assert process.waited is True


def test_recorded_playback_probe_cancellation_kills_and_reaps_process() -> None:
    """呼び出し元 Task のキャンセルでも能力 probe subprocess を強制回収する。"""

    class HangingProcess:
        """キャンセルされるまで communicate() が終了しない probe subprocess を再現する。"""

        def __init__(self) -> None:
            """
            subprocess の状態と communicate() 開始通知を初期化する。

            Args:
                None

            Returns:
                None
            """

            self.returncode: int | None = None
            self.killed = False
            self.waited = False
            self.communicate_started = asyncio.Event()

        async def communicate(self) -> tuple[bytes, bytes]:
            """
            communicate() の開始を通知し、呼び出し元からのキャンセルを待つ。

            Args:
                None

            Returns:
                tuple[bytes, bytes]: 通常はキャンセルされるため返らない空出力。
            """

            self.communicate_started.set()
            await asyncio.Event().wait()
            return b'', b''

        def kill(self) -> None:
            """
            subprocess の強制終了を記録する。

            Args:
                None

            Returns:
                None
            """

            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            """
            subprocess の回収待ちを記録する。

            Args:
                None

            Returns:
                int: SIGKILL 相当の終了コード。
            """

            self.waited = True
            return -9

    process = HangingProcess()

    async def Verify() -> None:
        """
        communicate() 待機中の Task をキャンセルし、例外伝播と回収を検証する。

        Args:
            None

        Returns:
            None
        """

        probe_task = asyncio.create_task(
            RecordedPlaybackCapabilityProbe.
                _RecordedPlaybackCapabilityProbe__communicateWithTimeout(  # pyright: ignore[reportPrivateUsage]
                    cast(asyncio.subprocess.Process, process),
                ),
        )
        await asyncio.wait_for(process.communicate_started.wait(), timeout=1)
        probe_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await probe_task

    asyncio.run(Verify())

    assert process.killed is True
    assert process.waited is True


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
