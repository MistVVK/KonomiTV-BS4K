# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import importlib
import re
from pathlib import Path

import psutil
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient
from tortoise import Tortoise

from app import config as config_module
from app.constants import LIBRARY_PATH, PASSWORD_CONTEXT
from app.models.User import User
from app.routers import KonomiTVBS4KCodecSupportRouter
from app.routers.UsersRouter import GenerateAccessToken
from app.schemas import KonomiTVBS4KCodecSupportJob
from app.streams.RecordedPlaybackCapabilities import (
    RecordedPlaybackBackend,
    RecordedRenderDevice,
)
from app.utils.KonomiTVBS4KCodecSupport import (
    KonomiTVBS4KCodecSupportJobManager,
    _CodecSupportDevice,
    _CodecSupportJobState,
    _NvidiaGpu,
)


# app.app は通常 KonomiTV.py で初期化済みの設定を参照するため、単体テストでは安全な既定値を先に設定する。
if config_module._CONFIG is None:
    config_module._CONFIG = config_module.HostServerSettings().toServerSettings(bypass_validation=True)
app_module = importlib.import_module('app.app')
app_module._shutdown_completed = True

API_BASE = '/api/maintenance/konomitv-bs4k-codec-support'
manager = KonomiTVBS4KCodecSupportJobManager

# 映像 8 コーデック + 音声 11 コーデックの FFmpeg トークンを網羅するバイナリ一覧 (実 probe 計画のテスト用)。
ALL_BINARY_DECODERS = {
    'h264', 'hevc', 'vp9', 'av1', 'mpeg1video', 'mpeg2video', 'mpeg4', 'vc1',
    'aac', 'aac_latm', 'mp2', 'mp3', 'ac3', 'eac3', 'opus', 'vorbis', 'flac', 'pcm_s16le',
}
ALL_BINARY_ENCODERS = {
    'libx264', 'libx265', 'libvpx-vp9', 'libaom-av1', 'mpeg1video', 'mpeg2video', 'mpeg4', 'wmv2',
    'aac', 'mp2', 'libmp3lame', 'ac3', 'eac3', 'libopus', 'libvorbis', 'flac', 'pcm_s16le',
}


def _ResetJobManager() -> None:
    """共有ジョブの ClassVar 状態を初期値へ戻す (テストの event loop 内で呼ぶ)。"""

    manager._state = _CodecSupportJobState(
        status = 'Idle',
        progress = 0.0,
        environment_signature = None,
        devices = [],
        total_operations = 0,
        completed_operations = 0,
    )
    manager._task = None
    manager._results_cache = {}
    manager._lock = asyncio.Lock()


async def _InitializeDatabase() -> None:
    await Tortoise.init(
        db_url = 'sqlite://:memory:',
        modules = {
            'models': [
                'app.models.User',
                'app.models.RefreshToken',
                'app.models.TwitterAccount',
                'app.models.BlueskyAccount',
                'app.models.AccountLink',
            ],
        },
        timezone = 'Asia/Tokyo',
    )
    await Tortoise.generate_schemas()


async def _CloseDatabase() -> None:
    await Tortoise.close_connections()


async def _CreateUser(*, is_admin: bool) -> User:
    return await User.create(
        name = 'codec-admin' if is_admin else 'codec-normal',
        password = PASSWORD_CONTEXT.hash('correct-password'),
        is_admin = is_admin,
        client_settings = {},
    )


def _CreateApp() -> FastAPI:
    app = FastAPI()
    app.include_router(KonomiTVBS4KCodecSupportRouter.router)
    return app


def _Headers(user: User) -> dict[str, str]:
    return {'Authorization': f'Bearer {GenerateAccessToken(user.id, user.token_version)}'}


async def _WaitForTerminalStatus(timeout: float = 10.0) -> KonomiTVBS4KCodecSupportJob:
    """ジョブが Completed / Failed / Idle になるまでポーリングして状態を返す。"""

    async with asyncio.timeout(timeout):
        while True:
            state = await manager.getState()
            if state.status in ('Completed', 'Failed', 'Idle'):
                return state
            await asyncio.sleep(0.01)


def _StubEnvironment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    signature: str = 'test-signature',
    render_devices: list[RecordedRenderDevice] | None = None,
    nvidia_gpus: list[_NvidiaGpu] | None = None,
    binary_encoders: set[str] | None = ALL_BINARY_ENCODERS,
    binary_decoders: set[str] | None = ALL_BINARY_DECODERS,
) -> None:
    """環境署名・GPU 列挙・バイナリ一覧をテスト値へ差し替える。"""

    async def FakeGetEnvironmentSignature(cls: object) -> str:
        return signature

    async def FakeQueryNvidiaGpus(cls: object) -> list[_NvidiaGpu] | None:
        return nvidia_gpus

    async def FakeListBinaryCodecNames(cls: object, kind: str) -> set[str] | None:
        return binary_encoders if kind == 'encoders' else binary_decoders

    monkeypatch.setattr(manager, 'getEnvironmentSignature', classmethod(FakeGetEnvironmentSignature))
    monkeypatch.setattr(manager, 'queryNvidiaGpus', classmethod(FakeQueryNvidiaGpus))
    monkeypatch.setattr(manager, 'listBinaryCodecNames', classmethod(FakeListBinaryCodecNames))
    if render_devices is not None:
        def FakeListRenderDevices(cls: object) -> list[RecordedRenderDevice]:
            return render_devices

        monkeypatch.setattr(RecordedPlaybackBackend, 'listRenderDevices', classmethod(FakeListRenderDevices))
    def FakeReadPciAddress(render_node_path: str) -> str | None:
        return {
            '/dev/dri/renderD128': '0000:00:02.0',
            '/dev/dri/renderD129': '0000:01:00.0',
            '/dev/dri/renderD130': '0000:41:00.0',
        }.get(render_node_path)

    monkeypatch.setattr(manager, '_KonomiTVBS4KCodecSupportJobManager__readPciAddress', staticmethod(FakeReadPciAddress))


def _StubProbeRunners(
    monkeypatch: pytest.MonkeyPatch,
    *,
    delay: float = 0.005,
    fail_video_encodings: dict[tuple[str, str], str] | None = None,
    fail_video_decodes: dict[tuple[str, str], str] | None = None,
) -> list[int]:
    """6 つの実 probe 実行関数をスタブし、同時実行ピークを記録する list を返す。"""

    peak: list[int] = [0]
    active = 0

    def Track() -> None:
        nonlocal active
        active += 1
        peak[0] = max(peak[0], active)

    def Untrack() -> None:
        nonlocal active
        active -= 1

    async def FakeRunVideoClip(cls: object, codec: str, bit_depth: object, output_path: object) -> bool:
        Track()
        try:
            await asyncio.sleep(delay)
            # decode probe はクリップファイルの存在を確認するため、空ファイルを作成する
            Path(output_path).touch()
            return True
        finally:
            Untrack()

    async def FakeRunAudioClip(cls: object, codec: str, output_path: object) -> bool:
        Track()
        try:
            await asyncio.sleep(delay)
            Path(output_path).touch()
            return True
        finally:
            Untrack()

    async def FakeRunVideoEncodeProbe(cls: object, device: _CodecSupportDevice, codec: str, bit_depth: object, output_path: object) -> tuple[bool, str]:
        Track()
        try:
            await asyncio.sleep(delay)
            key = (device.id, codec)
            if fail_video_encodings is not None and key in fail_video_encodings:
                return False, fail_video_encodings[key]
            return True, ''
        finally:
            Untrack()

    async def FakeRunVideoDecodeProbe(cls: object, device: _CodecSupportDevice, codec: str, bit_depth: object, clip_path: object) -> tuple[bool, str]:
        Track()
        try:
            await asyncio.sleep(delay)
            key = (device.id, codec)
            if fail_video_decodes is not None and key in fail_video_decodes:
                return False, fail_video_decodes[key]
            return True, ''
        finally:
            Untrack()

    async def FakeRunAudioEncodeProbe(cls: object, codec: str, output_path: object) -> tuple[bool, str]:
        Track()
        try:
            await asyncio.sleep(delay)
            return True, ''
        finally:
            Untrack()

    async def FakeRunAudioDecodeProbe(cls: object, codec: str, clip_path: object) -> tuple[bool, str]:
        Track()
        try:
            await asyncio.sleep(delay)
            return True, ''
        finally:
            Untrack()

    prefix = '_KonomiTVBS4KCodecSupportJobManager__'
    monkeypatch.setattr(manager, f'{prefix}runVideoClip', classmethod(FakeRunVideoClip))
    monkeypatch.setattr(manager, f'{prefix}runAudioClip', classmethod(FakeRunAudioClip))
    monkeypatch.setattr(manager, f'{prefix}runVideoEncodeProbe', classmethod(FakeRunVideoEncodeProbe))
    monkeypatch.setattr(manager, f'{prefix}runVideoDecodeProbe', classmethod(FakeRunVideoDecodeProbe))
    monkeypatch.setattr(manager, f'{prefix}runAudioEncodeProbe', classmethod(FakeRunAudioEncodeProbe))
    monkeypatch.setattr(manager, f'{prefix}runAudioDecodeProbe', classmethod(FakeRunAudioDecodeProbe))
    return peak


def test_api_requires_admin() -> None:
    """未認証 401 / 一般ユーザー 403 / 管理者 200 を確認する。"""

    async def Run() -> None:
        _ResetJobManager()
        await _InitializeDatabase()
        try:
            admin = await _CreateUser(is_admin=True)
            normal = await _CreateUser(is_admin=False)
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=_CreateApp()),
                base_url = 'https://testserver',
            ) as client:
                response = await client.post(f'{API_BASE}/probe')
                assert response.status_code == 401
                response = await client.post(f'{API_BASE}/probe', headers=_Headers(normal))
                assert response.status_code == 403
                response = await client.get(API_BASE, headers=_Headers(admin))
                assert response.status_code == 200
                assert response.json()['status'] == 'Idle'
                assert response.json()['devices'] == []
                response = await client.delete(f'{API_BASE}/probe', headers=_Headers(admin))
                assert response.status_code == 204
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_probe_caching_and_force(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一署名は 200 キャッシュ、新規・署名変更・force は 202 を確認する。"""

    _StubEnvironment(
        monkeypatch,
        render_devices = [],
        nvidia_gpus = None,
        binary_encoders = None,
        binary_decoders = None,
    )

    async def Run() -> None:
        _ResetJobManager()
        await _InitializeDatabase()
        try:
            admin = await _CreateUser(is_admin=True)
            headers = _Headers(admin)
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=_CreateApp()),
                base_url = 'https://testserver',
            ) as client:
                response = await client.post(f'{API_BASE}/probe', headers=headers)
                assert response.status_code == 202
                state = await _WaitForTerminalStatus()
                assert state.status == 'Completed'

                # 同一環境署名のキャッシュ再利用
                response = await client.post(f'{API_BASE}/probe', headers=headers)
                assert response.status_code == 200
                assert response.json()['status'] == 'Completed'

                # force 指定で再実行
                response = await client.post(f'{API_BASE}/probe', headers=headers, params={'force': 'true'})
                assert response.status_code == 202
                assert (await _WaitForTerminalStatus()).status == 'Completed'
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_inflight_join_returns_202(monkeypatch: pytest.MonkeyPatch) -> None:
    """実行中ジョブへの合流はキャッシュ再利用 (200) ではなく 202 を確認する。"""

    _StubEnvironment(monkeypatch, render_devices=[], nvidia_gpus=None)
    _StubProbeRunners(monkeypatch, delay=0.2)

    async def Run() -> None:
        _ResetJobManager()
        await _InitializeDatabase()
        try:
            admin = await _CreateUser(is_admin=True)
            headers = _Headers(admin)
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=_CreateApp()),
                base_url = 'https://testserver',
            ) as client:
                response = await client.post(f'{API_BASE}/probe', headers=headers)
                assert response.status_code == 202
                # 2 件目の POST は実行中ジョブへ合流する
                response = await client.post(f'{API_BASE}/probe', headers=headers)
                assert response.status_code == 202
                assert response.json()['status'] == 'Running'
                assert (await _WaitForTerminalStatus()).status == 'Completed'
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_cancel_returns_idle_and_keeps_no_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """キャンセル後 Idle へ戻り、キャッシュされないため再実行は 202 になる。"""

    _StubEnvironment(monkeypatch, render_devices=[], nvidia_gpus=None)

    async def Run() -> None:
        _ResetJobManager()
        await _InitializeDatabase()
        release = asyncio.Event()
        prefix = '_KonomiTVBS4KCodecSupportJobManager__'

        async def BlockingProbe(cls: object, *args: object) -> tuple[bool, str]:
            # キャンセル (CancelledError) までブロックし続ける
            await release.wait()
            return True, ''

        async def ImmediateClip(cls: object, *args: object) -> bool:
            # output_path は常に最後の引数
            Path(args[-1]).touch()
            return True

        monkeypatch.setattr(manager, f'{prefix}runVideoClip', classmethod(ImmediateClip))
        monkeypatch.setattr(manager, f'{prefix}runAudioClip', classmethod(ImmediateClip))
        monkeypatch.setattr(manager, f'{prefix}runVideoEncodeProbe', classmethod(BlockingProbe))
        monkeypatch.setattr(manager, f'{prefix}runVideoDecodeProbe', classmethod(BlockingProbe))
        monkeypatch.setattr(manager, f'{prefix}runAudioEncodeProbe', classmethod(BlockingProbe))
        monkeypatch.setattr(manager, f'{prefix}runAudioDecodeProbe', classmethod(BlockingProbe))

        admin = await _CreateUser(is_admin=True)
        headers = _Headers(admin)
        try:
            async with HTTPXAsyncClient(
                transport = ASGITransport(app=_CreateApp()),
                base_url = 'https://testserver',
            ) as client:
                response = await client.post(f'{API_BASE}/probe', headers=headers)
                assert response.status_code == 202
                async with asyncio.timeout(5.0):
                    while (await manager.getState()).status != 'Running':
                        await asyncio.sleep(0.01)

                # 実行中ジョブのキャンセル
                response = await client.delete(f'{API_BASE}/probe', headers=headers)
                assert response.status_code == 204
                assert (await manager.getState()).status == 'Idle'

                # 実行中ジョブがない場合も冪等に 204
                response = await client.delete(f'{API_BASE}/probe', headers=headers)
                assert response.status_code == 204

                # キャンセル済みの診断はキャッシュに残らないため再実行は 202
                response = await client.post(f'{API_BASE}/probe', headers=headers)
                assert response.status_code == 202
                response = await client.delete(f'{API_BASE}/probe', headers=headers)
                assert response.status_code == 204
                assert (await manager.getState()).status == 'Idle'
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_run_subprocess_reclaims_child_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """個別タイムアウト・キャンセルのいずれでも子プロセスを必ず回収する。"""

    monkeypatch.setattr(manager, '_probe_timeout_seconds', 0.2)

    async def Run() -> None:
        with pytest.raises(TimeoutError):
            await manager.runSubprocess(['sleep', '1000'])
        task = asyncio.create_task(manager.runSubprocess(['sleep', '1000']))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # sleep 1000 の子プロセスが残っていないことを確認する
        remaining = [
            process for process in psutil.process_iter(['cmdline'])
            if process.info['cmdline'] == ['sleep', '1000']
        ]
        assert remaining == []

    asyncio.run(Run())


def test_nvidia_dedup_and_cuda_only_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """DRM render node と nvidia-smi を PCI BDF で統合し、render node のない CUDA GPU も列挙する。"""

    _StubEnvironment(
        monkeypatch,
        render_devices = [
            RecordedRenderDevice(path='/dev/dri/renderD129', vendor_id='10de', vendor_name='NVIDIA'),
        ],
        nvidia_gpus = [
            _NvidiaGpu(name='NVIDIA GeForce RTX 4090', pci_bus_id='00000000:01:00.0'),
            _NvidiaGpu(name='NVIDIA A100', pci_bus_id='00000000:41:00.0'),
        ],
        binary_encoders = None,
        binary_decoders = None,
    )

    async def Run() -> None:
        _ResetJobManager()
        await _InitializeDatabase()
        try:
            started, _state = await manager.startProbe(force=False)
            assert started is True
            state = await _WaitForTerminalStatus()
            assert state.status == 'Completed'
            gpu_devices = [device for device in state.devices if device.kind == 'GPU']
            assert [device.id for device in gpu_devices] == ['gpu-0', 'gpu-1']
            # render node と nvidia-smi が PCI BDF で一致して重複排除され、CUDA ordinal が付く
            assert gpu_devices[0].label == 'NVIDIA GeForce RTX 4090'
            # render node に見えない CUDA のみ GPU も CUDA ordinal 付きで列挙される
            assert gpu_devices[1].label == 'NVIDIA A100'
            # 応答には render node パス・PCI BDF が含まれない
            assert 'renderD' not in state.model_dump_json()
            assert re.search(r'\b[0-9A-Fa-f]{4}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.[0-7A-Fa-f]', state.model_dump_json()) is None
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_amd_nodes_probe_encode_individually(monkeypatch: pytest.MonkeyPatch) -> None:
    """複数 AMD render node でも各 node の encode を実再生と同じ経路で検査する。"""

    _StubEnvironment(
        monkeypatch,
        render_devices = [
            RecordedRenderDevice(path='/dev/dri/renderD128', vendor_id='1002', vendor_name='AMD'),
            RecordedRenderDevice(path='/dev/dri/renderD130', vendor_id='1002', vendor_name='AMD'),
        ],
        nvidia_gpus = None,
    )
    _StubProbeRunners(monkeypatch)

    async def Run() -> None:
        _ResetJobManager()
        await _InitializeDatabase()
        try:
            await manager.startProbe(force=False)
            state = await _WaitForTerminalStatus()
            assert state.status == 'Completed'
            assert [device.id for device in state.devices] == ['cpu', 'gpu-0', 'gpu-1']
            for node in state.devices[1:]:
                assert node.vendor == 'AMD'
                probed_encodes = [
                    capability.encode
                    for capability in node.capabilities
                    if (
                        capability.media_type == 'Video' and
                        capability.used_by_konomitv_bs4k and
                        capability.codec in ('avc', 'hevc')
                    )
                ]
                # AMF が実再生で持つ AVC / HEVC は node ごとに VerifiedProbe する
                assert probed_encodes
                assert all(cell.status == 'Supported' for cell in probed_encodes)
                assert all(cell.evidence == 'VerifiedProbe' for cell in probed_encodes)
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_response_sanitization(monkeypatch: pytest.MonkeyPatch) -> None:
    """render node パス・PCI BDF・内部識別子が応答 JSON へ漏れない。"""

    _StubEnvironment(
        monkeypatch,
        render_devices = [
            RecordedRenderDevice(path='/dev/dri/renderD129', vendor_id='10de', vendor_name='NVIDIA'),
        ],
        nvidia_gpus = [
            _NvidiaGpu(name='NVIDIA GeForce RTX 4090', pci_bus_id='0000:01:00.0'),
        ],
    )
    _StubProbeRunners(monkeypatch)

    async def Run() -> None:
        _ResetJobManager()
        await _InitializeDatabase()
        try:
            await manager.startProbe(force=False)
            state = await _WaitForTerminalStatus()
            payload = state.model_dump_json()
            assert '/dev/dri' not in payload
            assert 'renderD' not in payload
            assert '0000:01:00.0' not in payload
            for device in state.devices:
                assert set(device.model_dump().keys()) == {'id', 'label', 'kind', 'vendor', 'capabilities'}
                for capability in device.capabilities:
                    assert set(capability.model_dump().keys()) == {
                        'media_type', 'codec', 'profile', 'bit_depth', 'decode', 'encode', 'used_by_konomitv_bs4k',
                    }
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_probe_results_are_independent_per_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    """operation ごとに status / evidence / reason が独立して記録される。"""

    _StubEnvironment(monkeypatch, render_devices=[], nvidia_gpus=None)
    _StubProbeRunners(
        monkeypatch,
        fail_video_encodings = {
            ('cpu', 'hevc'): 'EncodeFailed',
            ('cpu', 'vp9'): 'ProbeTimeout',
        },
    )

    async def Run() -> None:
        _ResetJobManager()
        await _InitializeDatabase()
        try:
            await manager.startProbe(force=False)
            state = await _WaitForTerminalStatus()
            assert state.status == 'Completed'
            cpu = state.devices[0]
            capabilities = {capability.codec: capability for capability in cpu.capabilities}
            # EncodeFailed は一時障害も含むため Unknown。タイムアウトも Unknown。
            assert capabilities['hevc'].encode.status == 'Unknown'
            assert capabilities['hevc'].encode.evidence == 'ProbeFailed'
            assert capabilities['hevc'].encode.reason_code == 'EncodeFailed'
            assert capabilities['vp9'].encode.status == 'Unknown'
            assert capabilities['vp9'].encode.evidence == 'ProbeFailed'
            assert capabilities['vp9'].encode.reason_code == 'ProbeTimeout'
            # 成功した cell は実 probe の根拠で確定し、他の cell へ影響しない
            assert capabilities['avc'].encode.status == 'Supported'
            assert capabilities['avc'].encode.evidence == 'VerifiedProbe'
            assert capabilities['avc'].encode.reason_code is None
            assert capabilities['avc'].encode.backend == 'FFmpeg'
            assert capabilities['avc'].encode.tested_configuration is not None
            assert capabilities['avc'].decode.status == 'Supported'
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_external_process_concurrency_is_limited_to_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """外部プロセスの同時実行は全 probe 共有の上限 2 で抑えられる。"""

    _StubEnvironment(monkeypatch, render_devices=[], nvidia_gpus=None)
    peak = _StubProbeRunners(monkeypatch, delay=0.05)

    async def Run() -> None:
        _ResetJobManager()
        await _InitializeDatabase()
        try:
            await manager.startProbe(force=False)
            state = await _WaitForTerminalStatus()
            assert state.status == 'Completed'
            # 20 個以上の operation を 2 並行で実行していること
            assert peak[0] == 2
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_job_timeout_marks_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """ジョブ全体のタイムアウト超過は部分結果を保持した Failed になる。"""

    _StubEnvironment(monkeypatch, render_devices=[], nvidia_gpus=None)
    _StubProbeRunners(monkeypatch, delay=0.3)
    monkeypatch.setattr(manager, '_job_timeout_seconds', 0.2)

    async def Run() -> None:
        _ResetJobManager()
        await _InitializeDatabase()
        try:
            await manager.startProbe(force=False)
            state = await _WaitForTerminalStatus()
            assert state.status == 'Failed'
            # 失敗しても再実行 (202) できる
            monkeypatch.setattr(manager, '_job_timeout_seconds', 600.0)
            started, _ = await manager.startProbe(force=False)
            assert started is True
            assert (await _WaitForTerminalStatus()).status == 'Completed'
        finally:
            await _CloseDatabase()

    asyncio.run(Run())


def test_route_exposure_between_main_and_compatibility_api() -> None:
    """サーバー診断 API は本線 app へだけ登録され、互換 API には露出しない。"""

    main_paths = list(app_module.app.openapi()['paths'].keys())
    assert f'{API_BASE}/probe' in main_paths
    assert API_BASE in main_paths

    from app.CompatibilityAPI import CreateCompatibilityAPI
    compatibility_app = CreateCompatibilityAPI()
    compatibility_paths = list(compatibility_app.openapi()['paths'].keys())
    assert not any('konomitv-bs4k-codec-support' in path for path in compatibility_paths)


def test_list_binary_codec_names_uses_dashed_flag_and_parses_hyphenated_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """FFmpeg の一覧は -encoders / -decoders で取り、ハイフン付き codec 名も拾う。"""

    ffmpeg = tmp_path / 'ffmpeg8.elf'
    ffmpeg.write_bytes(b'')
    captured: list[list[str]] = []

    async def FakeRunSubprocess(
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes, bytes]:
        del env, timeout
        captured.append(args)
        return 0, (
            b'Encoders:\n'
            b' V....D libx264              H.264 / AVC\n'
            b' V....D libaom-av1           libaom AV1 (codec av1)\n'
            b' V....D libvpx-vp9           libvpx VP9 (codec vp9)\n'
        ), b''

    monkeypatch.setitem(LIBRARY_PATH, 'FFmpeg8', str(ffmpeg))
    monkeypatch.setattr(manager, 'runSubprocess', staticmethod(FakeRunSubprocess))

    async def Run() -> None:
        names = await manager.listBinaryCodecNames('encoders')
        assert captured[0][-2:] == ['-hide_banner', '-encoders']
        assert names == {'libx264', 'libaom-av1', 'libvpx-vp9'}

    asyncio.run(Run())
