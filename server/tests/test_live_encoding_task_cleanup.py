import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal

import aiohttp
import pytest

import app.streams.LiveEncodingTask as live_encoding_task_module
from app.constants import QUALITY_TYPES
from app.streams.LiveEncodingTask import LiveEncodingTask
from app.streams.LiveStream import LiveStream


class _FakeProgram:
    """Mirakurun 接続失敗時の E-01M 分岐に必要な番組情報。"""

    title = 'テスト番組'
    is_free = True
    video_resolution = '1080i'
    end_time = datetime.now(UTC) + timedelta(hours=1)

    def isOffTheAirProgram(self) -> bool:
        """
        放送休止ではない番組として扱う。

        Args:
            None

        Returns:
            bool: 常に False。
        """

        return False


class _FakeChannel:
    """LiveEncodingTask.run() の接続前処理に必要な最小チャンネル情報。"""

    display_channel_id = 'gr011'
    network_id = 1
    transport_stream_id = 1
    service_id = 1
    type: Literal['GR'] = 'GR'
    is_oneseg = False
    is_radiochannel = False

    async def getCurrentAndNextProgram(self) -> tuple[_FakeProgram, None]:
        """
        現在の番組情報を返す。

        Args:
            None

        Returns:
            tuple[_FakeProgram, None]: 現在番組と次番組。
        """

        return (_FakeProgram(), None)


class _FakeChannelQuery:
    """Channel.filter() のクエリ代替。"""

    async def first(self) -> _FakeChannel:
        """
        テスト用チャンネルを返す。

        Args:
            None

        Returns:
            _FakeChannel: テスト用チャンネル。
        """

        return _FakeChannel()


@pytest.mark.parametrize(
    ('previous_video_resolution', 'current_video_resolution', 'expected'),
    [
        ('1080i', '720p', True),
        ('1080i', '1080i', False),
        (None, '1080i', False),
        ('1080i', None, False),
    ],
)
def test_program_boundary_restart_requires_known_resolution_change(
    previous_video_resolution: str | None,
    current_video_resolution: str | None,
    expected: bool,
) -> None:
    """番組境界の計画再起動は、新旧の解像度が既知で変化した場合だけ実行する。"""

    assert LiveEncodingTask.shouldRestartEncoderForVideoResolutionChange(
        previous_video_resolution,
        current_video_resolution,
    ) is expected


class _FakeWriter:
    """tsreadex の標準入力に必要な最小 StreamWriter 代替。"""

    def __init__(self) -> None:
        """
        Writer の状態を初期化する。

        Args:
            None

        Returns:
            None
        """

        self.is_closed = False

    def close(self) -> None:
        """
        Writer を閉じたことを記録する。

        Args:
            None

        Returns:
            None
        """

        self.is_closed = True

    def is_closing(self) -> bool:
        """
        Writer が閉じられているかを返す。

        Args:
            None

        Returns:
            bool: close() 済みかどうか。
        """

        return self.is_closed

    def write(self, _data: bytes) -> None:
        """
        テストでは入力データを破棄する。

        Args:
            _data (bytes): 書き込みデータ。

        Returns:
            None
        """

    async def drain(self) -> None:
        """
        テストでは直ちに書き込み完了とする。

        Args:
            None

        Returns:
            None
        """

class _ClosedEncoderWriter(_FakeWriter):
    """終了済み encoder stdin の write / drain 競合を再現する Writer。"""

    def __init__(self, failure_phase: Literal['write', 'drain']) -> None:
        """
        例外を送出する段階を保持する。

        Args:
            failure_phase (Literal['write', 'drain']): RuntimeError を送出する段階。

        Returns:
            None
        """

        super().__init__()
        self.failure_phase = failure_phase
        self.failure_raised = asyncio.Event()

    def write(self, data: bytes) -> None:
        """
        指定段階が write なら終了済み transport の RuntimeError を再現する。

        Args:
            data (bytes): 書き込み対象の TS データ。

        Returns:
            None
        """

        if self.failure_phase == 'write':
            self.failure_raised.set()
            raise RuntimeError('simulated closed encoder stdin on write')
        super().write(data)

    async def drain(self) -> None:
        """
        指定段階が drain なら終了済み transport の RuntimeError を再現する。

        Args:
            None

        Returns:
            None
        """

        if self.failure_phase == 'drain':
            self.failure_raised.set()
            raise RuntimeError('simulated closed encoder stdin on drain')
        await super().drain()


class _FakeEncoderOutput:
    """エンコーダー終了を再現する標準出力。"""

    async def readexactly(self, expected_size: int) -> bytes:
        """
        エンコーダー出力の EOF を返す。

        Args:
            expected_size (int): 期待するデータサイズ。

        Returns:
            bytes: 常に返らない。
        """

        raise asyncio.IncompleteReadError(b'', expected_size)


class _FakeEncoderLog:
    """エンコーダー終了を再現する標準エラー出力。"""

    async def read(self, _size: int) -> bytes:
        """
        エンコーダーログの EOF を返す。

        Args:
            _size (int): 読み取りサイズ。

        Returns:
            bytes: EOF を示す空バイト列。
        """

        return b''

    async def readline(self) -> bytes:
        """
        Bridge の標準エラー出力の EOF を返す。

        Args:
            None

        Returns:
            bytes: EOF を示す空バイト列。
        """

        return b''


class _BlockingTSReader:
    """cleanup による cancel を待つ TS 入力。"""

    async def readexactly(self, _size: int) -> bytes:
        """
        キャンセルされるまで読み取りを待機する。

        Args:
            _size (int): 読み取りサイズ。

        Returns:
            bytes: キャンセルされるため返らない。
        """

        await asyncio.Event().wait()
        return b''


class _FakeProcess:
    """子プロセスの kill / wait を検証するための代替。"""

    def __init__(self, should_fail_to_kill: bool = False) -> None:
        """
        プロセス代替を初期化する。

        Args:
            should_fail_to_kill (bool): kill() を失敗させるか。

        Returns:
            None
        """

        self.stdin = _FakeWriter()
        self.stdout = _FakeEncoderOutput()
        self.stderr = _FakeEncoderLog()
        self.returncode: int | None = None
        self.should_fail_to_kill = should_fail_to_kill
        self.kill_count = 0
        self.wait_count = 0

    def kill(self) -> None:
        """
        プロセス終了を記録する。

        Args:
            None

        Returns:
            None
        """

        self.kill_count += 1
        if self.should_fail_to_kill is True:
            raise OSError('simulated kill failure')
        self.returncode = 0

    async def wait(self) -> int:
        """
        プロセス終了待機を記録する。

        Args:
            None

        Returns:
            int: 終了コード。
        """

        self.wait_count += 1
        self.returncode = 0
        return 0


class _FakeArchiver:
    """PSI/SI アーカイバーの破棄を検証するための代替。"""

    def __init__(self, should_fail_to_destroy: bool = False) -> None:
        """
        アーカイバー代替を初期化する。

        Args:
            should_fail_to_destroy (bool): destroy() を失敗させるか。

        Returns:
            None
        """

        self.should_fail_to_destroy = should_fail_to_destroy
        self.destroy_count = 0

    async def destroy(self) -> None:
        """
        アーカイバー破棄を記録する。

        Args:
            None

        Returns:
            None
        """

        self.destroy_count += 1
        if self.should_fail_to_destroy is True:
            raise OSError('simulated destroy failure')


class _FakeSession:
    """Mirakurun 接続結果を制御する aiohttp ClientSession 代替。"""

    def __init__(self, result: BaseException | object, should_fail_to_close: bool = False) -> None:
        """
        Session 代替を初期化する。

        Args:
            result (BaseException | object): get() で送出または返却する値。
            should_fail_to_close (bool): close() を失敗させるか。

        Returns:
            None
        """

        self.result = result
        self.should_fail_to_close = should_fail_to_close
        self.closed = False
        self.get_started = asyncio.Event()
        self.close_count = 0

    async def get(self, **_kwargs: object) -> object:
        """
        指定された接続結果を再現する。

        Args:
            _kwargs (object): aiohttp の接続引数。

        Returns:
            object: 成功時のレスポンス。
        """

        self.get_started.set()
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    async def close(self) -> None:
        """
        Session の close を記録する。

        Args:
            None

        Returns:
            None
        """

        self.close_count += 1
        self.closed = True
        if self.should_fail_to_close is True:
            raise OSError('simulated session close failure')


class _FakeResponse:
    """Mirakurun Service Stream API の HTTP 応答代替。"""

    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        """
        HTTP 応答代替を初期化する。

        Args:
            status (int): HTTP ステータス。
            headers (dict[str, str] | None): 応答ヘッダー。

        Returns:
            None
        """

        self.status = status
        self.headers = {} if headers is None else headers
        self.content = _BlockingTSReader()
        self.closed = False
        self.close_count = 0

    def close(self) -> None:
        """
        応答を閉じたことを記録する。

        Args:
            None

        Returns:
            None
        """

        self.close_count += 1
        self.closed = True


class _FakeTelemetry:
    """エンコーダー起動通知を記録するテレメトリ代替。"""

    def __init__(self) -> None:
        """
        エンコーダー起動回数を初期化する。

        Args:
            None

        Returns:
            None
        """

        self.encoder_started_count = 0

    def snapshot(self) -> SimpleNamespace:
        """
        共有 source 判定に必要な待機状態を返す。

        Args:
            None

        Returns:
            SimpleNamespace: 処理結果。
        """

        return SimpleNamespace(prepare_state='Idle')

    def encoderStarted(self) -> None:
        """
        エンコーダー起動回数を記録する。

        Args:
            None

        Returns:
            None
        """

        self.encoder_started_count += 1


class _FakeLiveStream:
    """LiveEncodingTask の状態遷移とクライアント切断を検証するための代替。"""

    def __init__(self) -> None:
        """
        ライブストリーム代替を初期化する。

        Args:
            None

        Returns:
            None
        """

        self.log_prefix = '[Live: test]'
        self.live_stream_id = 'gr011-1080p'
        self.display_channel_id = 'gr011'
        self.quality = '1080p'
        self.stream_anchor_enabled = False
        self.encoding_options = SimpleNamespace(
            is_hevc_10bit_enabled=False,
            is_24fps_mode_enabled=False,
            video_codec='avc',
            video_bit_depth=8,
            audio_codec='aac',
        )
        self.psi_data_archiver = None
        self.tuner = None
        self._status = 'Standby'
        self._detail = 'エンコードタスクを起動しています…'
        self.disconnect_all_count = 0
        self._telemetry = _FakeTelemetry()

    def getTelemetry(self) -> _FakeTelemetry:
        """
        型付きテレメトリ代替を返す。

        Args:
            None

        Returns:
            _FakeTelemetry: 処理結果。
        """

        return self._telemetry

    def getStatus(self) -> SimpleNamespace:
        """
        現在の状態を返す。

        Args:
            None

        Returns:
            SimpleNamespace: LiveEncodingTask が参照する状態。
        """

        return SimpleNamespace(status=self._status, detail=self._detail, client_count=0, updated_at=0.0)

    def setStatus(self, status: str, detail: str, quiet: bool = False) -> bool:
        """
        状態を更新する。

        Args:
            status (str): 新しい状態。
            detail (str): 新しい状態詳細。
            quiet (bool): ログ省略フラグ。

        Returns:
            bool: 常に True。
        """

        self._status = status
        self._detail = detail
        return True

    def disconnectAll(self) -> None:
        """
        クライアント終了通知を記録する。

        Args:
            None

        Returns:
            None
        """

        self.disconnect_all_count += 1


def _install_mirakurun_setup(
    monkeypatch: pytest.MonkeyPatch,
    session: _FakeSession,
    archiver: _FakeArchiver,
    processes: list[_FakeProcess],
    subprocess_calls: list[tuple[tuple[object, ...], dict[str, object]]] | None = None,
) -> _FakeLiveStream:
    """
    Mirakurun 接続直前まで実行するための依存関係を差し替える。

    Args:
        monkeypatch (pytest.MonkeyPatch): 依存関係を差し替える fixture。
        session (_FakeSession): 接続結果を返す Session 代替。
        archiver (_FakeArchiver): アーカイバー代替。
        processes (list[_FakeProcess]): 起動順に返す子プロセス代替。
        subprocess_calls (list[tuple[tuple[object, ...], dict[str, object]]] | None): 子プロセス起動記録。

    Returns:
        _FakeLiveStream: テスト対象のライブストリーム代替。
    """

    settings = SimpleNamespace(
        general=SimpleNamespace(
            backend='Mirakurun',
            live_stream_backend='Mirakurun',
            debug_encoder=False,
        ),
        tv=SimpleNamespace(debug_mode_ts_path=None, max_alive_time=60),
    )

    async def create_subprocess_exec(*args: object, **kwargs: object) -> _FakeProcess:
        """指定順に子プロセス代替を返す。"""

        if subprocess_calls is not None:
            subprocess_calls.append((args, kwargs))
        return processes.pop(0)

    async def acquire_mirakurun_tuner(_channel_type: str) -> bool:
        """チューナー待機を即時成功させる。"""

        return True

    async def get_live_combination_capability(
        _cls: type[object],
        _encoder: str,
        _video_codec: str,
        _bit_depth: int,
        _audio_codec: str,
    ) -> SimpleNamespace:
        """通常APIと同じ実行前組み合わせ能力を即時成功させる。"""

        return SimpleNamespace(available=True, reason_code=None)

    monkeypatch.setattr(live_encoding_task_module, 'Config', lambda: settings)
    monkeypatch.setattr(live_encoding_task_module.Channel, 'filter', lambda **_kwargs: _FakeChannelQuery())
    monkeypatch.setattr(live_encoding_task_module, 'GetEncoderForLiveChannel', lambda _channel_id: 'FFmpeg')
    monkeypatch.setattr(live_encoding_task_module, 'GetMirakurunAPIEndpointURL', lambda endpoint: endpoint)
    monkeypatch.setattr(live_encoding_task_module, 'LivePSIDataArchiver', lambda _service_id: archiver)
    monkeypatch.setattr(live_encoding_task_module.aiohttp, 'ClientSession', lambda: session)
    monkeypatch.setattr(live_encoding_task_module.asyncio.subprocess, 'create_subprocess_exec', create_subprocess_exec)
    monkeypatch.setattr(live_encoding_task_module.logging, 'debug', lambda _message: None)
    monkeypatch.setattr(
        live_encoding_task_module.KonomiTVBS4KPlaybackCapabilityProbe,
        'getLiveCombinationCapability',
        classmethod(get_live_combination_capability),
    )

    live_stream = _FakeLiveStream()
    task = LiveEncodingTask(live_stream)  # type: ignore[arg-type]
    task.buildFFmpeg8Options = lambda *_args: []  # type: ignore[method-assign]
    task.acquireMirakurunTuner = acquire_mirakurun_tuner  # type: ignore[method-assign]
    live_stream.task = task
    return live_stream


@pytest.mark.parametrize(
    'connection_error',
    [
        aiohttp.ServerDisconnectedError(),
        aiohttp.ClientOSError(1, 'connection reset'),
    ],
)
def test_mirakurun_client_errors_cleanup_every_resource(
    monkeypatch: pytest.MonkeyPatch,
    connection_error: aiohttp.ClientError,
) -> None:
    """
    ServerDisconnectedError と ClientOSError を E-01M として終了し、全資源を回収する。

    Args:
        monkeypatch (pytest.MonkeyPatch): 依存関係を差し替える fixture。
        connection_error (aiohttp.ClientError): 再現する Mirakurun 接続例外。

    Returns:
        None
    """

    session = _FakeSession(connection_error)
    archiver = _FakeArchiver()
    tsreadex = _FakeProcess()
    encoder = _FakeProcess()
    processes = [tsreadex, encoder]
    live_stream = _install_mirakurun_setup(monkeypatch, session, archiver, processes)

    asyncio.run(live_stream.task.run())

    assert live_stream.getStatus().status == 'Offline'
    assert '(E-01M)' in live_stream.getStatus().detail
    assert live_stream.disconnect_all_count == 1
    assert session.closed is True
    assert archiver.destroy_count == 1
    assert tsreadex.kill_count == 1
    assert encoder.kill_count == 1
    assert tsreadex.wait_count == 0
    assert encoder.wait_count == 0


@pytest.mark.parametrize(
    ('video_codec', 'video_bit_depth'),
    [
        ('avc', 8),
        ('hevc', 8),
        ('hevc', 10),
    ],
)
def test_legacy_codecs_completely_bypass_ts_codec_bridge(
    monkeypatch: pytest.MonkeyPatch,
    video_codec: str,
    video_bit_depth: int,
) -> None:
    """AVC / HEVC + AAC は Bridge が存在しても従来二 process 構成と直接 stdout を維持する。"""

    session = _FakeSession(aiohttp.ServerDisconnectedError())
    archiver = _FakeArchiver()
    tsreadex = _FakeProcess()
    encoder = _FakeProcess()
    subprocess_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    live_stream = _install_mirakurun_setup(
        monkeypatch,
        session,
        archiver,
        [tsreadex, encoder],
        subprocess_calls,
    )
    live_stream.encoding_options = SimpleNamespace(
        video_codec=video_codec,
        video_bit_depth=video_bit_depth,
        audio_codec='aac',
        is_hevc_10bit_enabled=video_bit_depth == 10,
        is_24fps_mode_enabled=False,
    )
    bridge_path = '/runtime/ts-codec-bridge.elf'
    monkeypatch.setitem(
        live_encoding_task_module.LIBRARY_PATH,
        LiveEncodingTask.TS_CODEC_BRIDGE_LIBRARY_PATH_KEY,
        bridge_path,
    )

    asyncio.run(live_stream.task.run())

    assert len(subprocess_calls) == 2
    assert all(call_args[0] != bridge_path for call_args, _call_options in subprocess_calls)
    _encoder_args, encoder_options = subprocess_calls[1]
    assert encoder_options['stdout'] == asyncio.subprocess.PIPE
    assert tsreadex.kill_count == 1
    assert encoder.kill_count == 1


def test_normal_video_shared_source_path_uses_anchor_bridge_and_cleans_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    通常映像は品質別tsreadexを起動せず、共有source→Anchor Bridgeを確実に回収する。

    Args:
        monkeypatch (pytest.MonkeyPatch): テスト対象の依存を差し替える fixture。

    Returns:
        None
    """

    class _SharedSubscription:

        role = 'Active'
        source_geometry = None
        last_input_at = 1.0

        def __init__(self, shared_archiver: _FakeArchiver) -> None:
            """
            利用する状態を初期化する。

            Args:
                shared_archiver (_FakeArchiver): shared_archiver に指定する値。

            Returns:
                None
            """
            self.psi_data_archiver = shared_archiver
            self.close_count = 0
            self.keep_source_alive_values: list[bool] = []

        async def read(self) -> bytes:
            """
            次のデータを読み取る。

            Args:
                None

            Returns:
                bytes: 処理結果のバイト列。
            """
            await asyncio.Event().wait()
            return b''

        async def close(self, *, keep_source_alive: bool = True) -> None:
            """
            保持するリソースを閉じる。

            Args:
                keep_source_alive (bool): keep_source_alive に指定する値。

            Returns:
                None
            """
            self.close_count += 1
            self.keep_source_alive_values.append(keep_source_alive)

    session = _FakeSession(AssertionError('shared source must not open a per-quality Mirakurun session'))
    shared_archiver = _FakeArchiver()
    bridge = _FakeProcess()
    encoder = _FakeProcess()
    subprocess_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    live_stream = _install_mirakurun_setup(
        monkeypatch,
        session,
        shared_archiver,
        [bridge, encoder],
        subprocess_calls,
    )
    live_stream.stream_anchor_enabled = True
    live_stream._clients = [object()]
    live_stream._source_subscription = None
    live_stream.hasSourceViewers = lambda: len(live_stream._clients) > 0  # type: ignore[method-assign]
    live_stream.setSourceSubscription = (  # type: ignore[method-assign]
        lambda subscription: setattr(live_stream, '_source_subscription', subscription)
    )
    live_stream.getSourceSubscription = (  # type: ignore[method-assign]
        lambda: live_stream._source_subscription
    )
    live_stream.writeStreamData = lambda _chunk: None  # type: ignore[method-assign]
    subscription = _SharedSubscription(shared_archiver)
    source_acquired = asyncio.Event()
    acquire_records: list[tuple[object, str, bool]] = []

    class _Coordinator:

        async def acquire(
            self,
            descriptor: object,
            *,
            role: str,
            is_in_use: object,
        ) -> _SharedSubscription:
            """
            acquire の処理を実行する。

            Args:
                descriptor (object): 共有するライブ入力の接続記述子。
                role (str): 共有 source 上での購読 role。
                is_in_use (object): 購読元に viewer が残るか判定する callback。

            Returns:
                _SharedSubscription: 処理結果。
            """
            acquire_records.append((descriptor, role, is_in_use()))  # type: ignore[operator]
            source_acquired.set()
            return subscription

    bridge_path = '/runtime/ts-codec-bridge.elf'
    monkeypatch.setattr(live_encoding_task_module, 'LIVE_SOURCE_COORDINATOR', _Coordinator())
    monkeypatch.setitem(
        live_encoding_task_module.LIBRARY_PATH,
        LiveEncodingTask.TS_CODEC_BRIDGE_LIBRARY_PATH_KEY,
        bridge_path,
    )
    monkeypatch.setattr(
        live_encoding_task_module.TSCodecBridgeRuntimeVerifier,
        'recordProcessStart',
        classmethod(lambda _cls, _kind: None),
    )

    async def verify() -> None:
        """
        非同期検証本体を実行する。

        Args:
            None

        Returns:
            None
        """
        running_task = asyncio.create_task(live_stream.task.run())
        await asyncio.wait_for(source_acquired.wait(), timeout=1)
        while live_stream._source_subscription is not subscription:
            await asyncio.sleep(0)
        running_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running_task

    asyncio.run(verify())

    assert len(subprocess_calls) == 2
    bridge_args, bridge_options = subprocess_calls[0]
    _encoder_args, encoder_options = subprocess_calls[1]
    assert bridge_args == (
        bridge_path,
        '--video-codec',
        'passthrough',
        '--audio-codec',
        'aac',
        '--stream-anchor-v1',
    )
    assert encoder_options['stdin'] == asyncio.subprocess.PIPE
    assert isinstance(bridge_options['stdin'], int)
    assert all(call_args[0] != live_encoding_task_module.LIBRARY_PATH['tsreadex'] for call_args, _ in subprocess_calls)
    assert session.get_started.is_set() is False
    assert acquire_records and acquire_records[0][1:] == ('Active', True)
    assert subscription.close_count == 1, (
        live_stream.task._cleanup_context.is_cleanup_completed,
        live_stream.task._cleanup_context.source_subscription,
        len(live_stream.task._cleanup_context.background_tasks),
        live_stream._source_subscription,
        bridge.kill_count,
        encoder.kill_count,
    )
    assert subscription.keep_source_alive_values == [False]
    assert shared_archiver.destroy_count == 0
    assert live_stream._telemetry.encoder_started_count == 1
    assert bridge.kill_count == 1
    assert encoder.kill_count == 1


@pytest.mark.parametrize('failure_phase', ['write', 'drain'])
def test_shared_source_reader_handles_closed_encoder_stdin_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: Literal['write', 'drain'],
) -> None:
    """
    共有 source feeder は終了済み encoder stdin の RuntimeError を未回収にしない。

    Args:
        monkeypatch (pytest.MonkeyPatch): テスト対象の依存を差し替える fixture。
        failure_phase (Literal['write', 'drain']): failure_phase に指定する値。

    Returns:
        None
    """

    class _SharedSubscription:

        role = 'Active'
        source_geometry = None
        last_input_at = 1.0

        def __init__(self, shared_archiver: _FakeArchiver) -> None:
            """
            利用する状態を初期化する。

            Args:
                shared_archiver (_FakeArchiver): shared_archiver に指定する値。

            Returns:
                None
            """
            self.psi_data_archiver = shared_archiver
            self.sent_first_chunk = False
            self.close_count = 0
            self.keep_source_alive_values: list[bool] = []

        async def read(self) -> bytes:
            """
            次のデータを読み取る。

            Args:
                None

            Returns:
                bytes: 処理結果のバイト列。
            """
            if self.sent_first_chunk is False:
                self.sent_first_chunk = True
                return b'\x47' * 188
            await asyncio.Event().wait()
            return b''

        async def close(self, *, keep_source_alive: bool = True) -> None:
            """
            保持するリソースを閉じる。

            Args:
                keep_source_alive (bool): keep_source_alive に指定する値。

            Returns:
                None
            """
            self.close_count += 1
            self.keep_source_alive_values.append(keep_source_alive)

    session = _FakeSession(AssertionError('shared source must not open a per-quality Mirakurun session'))
    shared_archiver = _FakeArchiver()
    bridge = _FakeProcess()
    encoder = _FakeProcess()
    closed_encoder_writer = _ClosedEncoderWriter(failure_phase)
    encoder.stdin = closed_encoder_writer
    live_stream = _install_mirakurun_setup(
        monkeypatch,
        session,
        shared_archiver,
        [bridge, encoder],
    )
    live_stream.stream_anchor_enabled = True
    live_stream._clients = [object()]
    live_stream._source_subscription = None
    live_stream.hasSourceViewers = lambda: len(live_stream._clients) > 0  # type: ignore[method-assign]
    live_stream.setSourceSubscription = (  # type: ignore[method-assign]
        lambda subscription: setattr(live_stream, '_source_subscription', subscription)
    )
    live_stream.getSourceSubscription = (  # type: ignore[method-assign]
        lambda: live_stream._source_subscription
    )
    live_stream.getStreamDataWrittenAt = lambda: time.time()  # type: ignore[method-assign]
    live_stream.writeStreamData = lambda _chunk: None  # type: ignore[method-assign]
    subscription = _SharedSubscription(shared_archiver)

    class _Coordinator:

        async def acquire(
            self,
            _descriptor: object,
            *,
            role: str,
            is_in_use: object,
        ) -> _SharedSubscription:
            """
            acquire の処理を実行する。

            Args:
                _descriptor (object): 共有するライブ入力の接続記述子。
                role (str): 共有 source 上での購読 role。
                is_in_use (object): 購読元に viewer が残るか判定する callback。

            Returns:
                _SharedSubscription: 処理結果。
            """
            assert role == 'Active'
            assert is_in_use() is True  # type: ignore[operator]
            return subscription

    bridge_path = '/runtime/ts-codec-bridge.elf'
    monkeypatch.setattr(live_encoding_task_module, 'LIVE_SOURCE_COORDINATOR', _Coordinator())
    monkeypatch.setitem(
        live_encoding_task_module.LIBRARY_PATH,
        LiveEncodingTask.TS_CODEC_BRIDGE_LIBRARY_PATH_KEY,
        bridge_path,
    )
    monkeypatch.setattr(
        live_encoding_task_module.TSCodecBridgeRuntimeVerifier,
        'recordProcessStart',
        classmethod(lambda _cls, _kind: None),
    )

    async def Verify() -> tuple[list[dict[str, Any]], str]:
        """
        非同期検証本体を実行する。

        Args:
            None

        Returns:
            tuple[list[dict[str, Any]], str]: 処理結果。
        """
        loop = asyncio.get_running_loop()
        unexpected_task_exceptions: list[dict[str, Any]] = []
        previous_exception_handler = loop.get_exception_handler()
        restart_detail = ''

        def CollectUnexpectedTaskException(
            _loop: asyncio.AbstractEventLoop,
            context: dict[str, Any],
        ) -> None:
            """
            未回収 task 例外を記録する。

            Args:
                _loop (asyncio.AbstractEventLoop): _loop に指定する値。
                context (dict[str, Any]): context に指定する値。

            Returns:
                None
            """
            unexpected_task_exceptions.append(context)

        loop.set_exception_handler(CollectUnexpectedTaskException)
        running_task = asyncio.create_task(live_stream.task.run())
        try:
            await asyncio.wait_for(closed_encoder_writer.failure_raised.wait(), timeout=1)
            async with asyncio.timeout(1):
                while live_stream.getStatus().status != 'Restart':
                    await asyncio.sleep(0)
            restart_detail = live_stream.getStatus().detail
            running_task.cancel()
            await asyncio.gather(running_task, return_exceptions=True)
            await asyncio.sleep(0)
        finally:
            if running_task.done() is False:
                running_task.cancel()
                await asyncio.gather(running_task, return_exceptions=True)
            loop.set_exception_handler(previous_exception_handler)
        return unexpected_task_exceptions, restart_detail

    unexpected_task_exceptions, restart_detail = asyncio.run(Verify())

    assert f'closed encoder stdin on {failure_phase}' in restart_detail
    assert unexpected_task_exceptions == []
    assert subscription.close_count == 1
    assert subscription.keep_source_alive_values == [False]
    assert bridge.kill_count == 1
    assert encoder.kill_count == 1


def test_advanced_codec_connects_encoder_to_bridge_with_direct_os_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """高度 codec は Python 中継を置かず、エンコーダー標準出力を Bridge 標準入力へ直結する。"""

    session = _FakeSession(aiohttp.ServerDisconnectedError())
    archiver = _FakeArchiver()
    tsreadex = _FakeProcess()
    bridge = _FakeProcess()
    encoder = _FakeProcess()
    subprocess_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    bridge_start_records: list[None] = []
    live_stream = _install_mirakurun_setup(
        monkeypatch,
        session,
        archiver,
        [tsreadex, bridge, encoder],
        subprocess_calls,
    )
    live_stream.encoding_options = SimpleNamespace(
        video_codec='av1',
        video_bit_depth=10,
        audio_codec='opus',
        is_hevc_10bit_enabled=False,
        is_24fps_mode_enabled=False,
    )
    live_stream.task.buildFFmpeg8Options = (  # type: ignore[method-assign]
        lambda *_args: ['-muxrate', '6600K']
    )
    monkeypatch.setitem(
        live_encoding_task_module.LIBRARY_PATH,
        LiveEncodingTask.TS_CODEC_BRIDGE_LIBRARY_PATH_KEY,
        '/runtime/ts-codec-bridge.elf',
    )
    monkeypatch.setattr(
        live_encoding_task_module.TSCodecBridgeRuntimeVerifier,
        'recordProcessStart',
        classmethod(lambda _cls, kind: bridge_start_records.append(kind)),
    )

    asyncio.run(live_stream.task.run())

    assert len(subprocess_calls) == 3
    assert bridge_start_records == ['live']
    bridge_args, bridge_kwargs = subprocess_calls[1]
    encoder_args, encoder_kwargs = subprocess_calls[2]
    assert bridge_args == (
        '/runtime/ts-codec-bridge.elf',
        '--video-codec',
        'av1',
        '--audio-codec',
        'opus',
        '--transport-rate-kbps',
        '6600',
    )
    assert isinstance(bridge_kwargs['stdin'], int)
    assert bridge_kwargs['stdout'] == asyncio.subprocess.PIPE
    assert isinstance(encoder_kwargs['stdout'], int)
    assert encoder_kwargs['stdout'] != asyncio.subprocess.PIPE
    assert encoder_args[0] == live_encoding_task_module.LIBRARY_PATH['FFmpeg8']
    assert tsreadex.kill_count == 1
    assert encoder.kill_count == 1
    assert bridge.kill_count == 1


def test_bridge_spawn_failure_exposes_codec_specific_offline_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge の起動 race は一般障害と混同せず、一回限り互換 fallback 用の理由を通知する。"""

    session = _FakeSession(aiohttp.ServerDisconnectedError())
    archiver = _FakeArchiver()
    tsreadex = _FakeProcess()
    subprocess_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    live_stream = _install_mirakurun_setup(
        monkeypatch,
        session,
        archiver,
        [tsreadex],
        subprocess_calls,
    )
    live_stream.encoding_options = SimpleNamespace(
        video_codec='av1',
        video_bit_depth=10,
        audio_codec='opus',
        is_hevc_10bit_enabled=False,
        is_24fps_mode_enabled=False,
    )
    live_stream.task.buildFFmpeg8Options = (  # type: ignore[method-assign]
        lambda *_args: ['-muxrate', '6600K']
    )
    bridge_path = '/runtime/ts-codec-bridge.elf'
    monkeypatch.setitem(
        live_encoding_task_module.LIBRARY_PATH,
        LiveEncodingTask.TS_CODEC_BRIDGE_LIBRARY_PATH_KEY,
        bridge_path,
    )

    async def create_subprocess_exec(
        *args: object,
        **kwargs: object,
    ) -> _FakeProcess:
        subprocess_calls.append((args, kwargs))
        if args[0] == bridge_path:
            raise OSError('simulated Bridge spawn race')
        return tsreadex

    monkeypatch.setattr(
        live_encoding_task_module.asyncio.subprocess,
        'create_subprocess_exec',
        create_subprocess_exec,
    )

    with pytest.raises(OSError, match='simulated Bridge spawn race'):
        asyncio.run(live_stream.task.run())

    assert live_stream.getStatus().status == 'Offline'
    assert live_stream.getStatus().detail == (
        'TS Codec Bridge の起動に失敗しました。(E-07B)'
    )
    assert [call_args[0] for call_args, _call_options in subprocess_calls] == [
        live_encoding_task_module.LIBRARY_PATH['tsreadex'],
        bridge_path,
    ]
    assert tsreadex.kill_count == 1


@pytest.mark.parametrize(
    ('quality', 'expected_transport_rate_kbps'),
    [
        ('240p', '1650'),
        ('240p-30fps', '1650'),
        ('540p', '6600'),
        ('540p-30fps', '6600'),
    ],
)
def test_av1_runtime_passes_required_minimum_level_bounded_muxrate_to_bridge(
    monkeypatch: pytest.MonkeyPatch,
    quality: QUALITY_TYPES,
    expected_transport_rate_kbps: str,
) -> None:
    """
    AV1 は必要最小 Level 以下の FFmpeg muxrate を同値の Bridge 引数へ渡す。

    Args:
        monkeypatch (pytest.MonkeyPatch): テスト対象の依存を差し替える fixture。
        quality (QUALITY_TYPES): 処理または検証対象の画質。
        expected_transport_rate_kbps (str): 期待する Bridge 搬送レート Kbps。

    Returns:
        None
    """

    session = _FakeSession(aiohttp.ServerDisconnectedError())
    archiver = _FakeArchiver()
    tsreadex = _FakeProcess()
    bridge = _FakeProcess()
    encoder = _FakeProcess()
    subprocess_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    live_stream = _install_mirakurun_setup(
        monkeypatch,
        session,
        archiver,
        [tsreadex, bridge, encoder],
        subprocess_calls,
    )
    live_stream.quality = quality
    live_stream.encoding_options = SimpleNamespace(
        video_codec='av1',
        video_bit_depth=8,
        audio_codec='aac',
        is_hevc_10bit_enabled=False,
        is_24fps_mode_enabled=False,
    )
    live_stream.task.buildFFmpeg8Options = LiveEncodingTask.buildFFmpeg8Options.__get__(  # type: ignore[method-assign]
        live_stream.task,
        LiveEncodingTask,
    )
    monkeypatch.setitem(
        live_encoding_task_module.LIBRARY_PATH,
        LiveEncodingTask.TS_CODEC_BRIDGE_LIBRARY_PATH_KEY,
        '/runtime/ts-codec-bridge.elf',
    )

    asyncio.run(live_stream.task.run())

    assert len(subprocess_calls) == 3
    bridge_args, _bridge_kwargs = subprocess_calls[1]
    encoder_args, _encoder_kwargs = subprocess_calls[2]
    assert bridge_args == (
        '/runtime/ts-codec-bridge.elf',
        '--video-codec',
        'av1',
        '--audio-codec',
        'aac',
        '--transport-rate-kbps',
        expected_transport_rate_kbps,
    )
    assert encoder_args[encoder_args.index('-muxrate') + 1] == f'{expected_transport_rate_kbps}K'


def test_radio_opus_runtime_uses_audio_only_ffmpeg_and_passthrough_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ラジオ Opus は映像を生成せず、全音声を passthrough 映像指定の Bridge へ直結する。"""

    monkeypatch.setattr(_FakeChannel, 'is_radiochannel', True)
    session = _FakeSession(aiohttp.ServerDisconnectedError())
    archiver = _FakeArchiver()
    tsreadex = _FakeProcess()
    bridge = _FakeProcess()
    encoder = _FakeProcess()
    subprocess_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    live_stream = _install_mirakurun_setup(
        monkeypatch,
        session,
        archiver,
        [tsreadex, bridge, encoder],
        subprocess_calls,
    )
    live_stream.encoding_options = SimpleNamespace(
        video_codec='av1',
        video_bit_depth=10,
        audio_codec='opus',
        is_hevc_10bit_enabled=False,
        is_24fps_mode_enabled=False,
    )

    async def GetAudioCapability(
        _cls: type[object],
        _audio_codec: str,
    ) -> SimpleNamespace:
        """audio-only probe による利用可能な音声能力を返す。"""

        return SimpleNamespace(live_available=True, live_reason_code=None)

    monkeypatch.setattr(
        live_encoding_task_module.KonomiTVBS4KPlaybackCapabilityProbe,
        'getAudioCapability',
        classmethod(GetAudioCapability),
    )
    monkeypatch.setitem(
        live_encoding_task_module.LIBRARY_PATH,
        LiveEncodingTask.TS_CODEC_BRIDGE_LIBRARY_PATH_KEY,
        '/runtime/ts-codec-bridge.elf',
    )

    asyncio.run(live_stream.task.run())

    assert len(subprocess_calls) == 3
    bridge_args, bridge_kwargs = subprocess_calls[1]
    encoder_args, encoder_kwargs = subprocess_calls[2]
    assert bridge_args == (
        '/runtime/ts-codec-bridge.elf',
        '--video-codec',
        'passthrough',
        '--audio-codec',
        'opus',
    )
    assert [
        encoder_args[index + 1]
        for index, option in enumerate(encoder_args)
        if option == '-map'
    ] == ['0:a?', '0:d?']
    assert encoder_args[encoder_args.index('-acodec') + 1] == 'libopus'
    assert '-vcodec' not in encoder_args
    assert '-c:v' not in encoder_args
    assert isinstance(bridge_kwargs['stdin'], int)
    assert isinstance(encoder_kwargs['stdout'], int)
    assert encoder_kwargs['stdout'] != asyncio.subprocess.PIPE


@pytest.mark.parametrize(
    'reason_code',
    ['BridgeUnavailable', 'ProbeFailed', 'UnsupportedCombination'],
)
def test_advanced_codec_execution_boundary_rejects_unavailable_exact_combination(
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
) -> None:
    """Router後の依存変化も理由を保持し、子process起動前でfail closedにする。"""

    session = _FakeSession(aiohttp.ServerDisconnectedError())
    archiver = _FakeArchiver()
    subprocess_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    live_stream = _install_mirakurun_setup(
        monkeypatch,
        session,
        archiver,
        [],
        subprocess_calls,
    )
    live_stream.encoding_options = SimpleNamespace(
        video_codec='av1',
        video_bit_depth=10,
        audio_codec='opus',
        is_hevc_10bit_enabled=False,
        is_24fps_mode_enabled=False,
    )

    async def get_live_combination_capability(
        _cls: type[object],
        _encoder: str,
        _video_codec: str,
        _bit_depth: int,
        _audio_codec: str,
    ) -> SimpleNamespace:
        return SimpleNamespace(available=False, reason_code=reason_code)

    monkeypatch.setattr(
        live_encoding_task_module.KonomiTVBS4KPlaybackCapabilityProbe,
        'getLiveCombinationCapability',
        classmethod(get_live_combination_capability),
    )

    with pytest.raises(RuntimeError, match='encoding combination is unavailable'):
        asyncio.run(live_stream.task.run())

    assert subprocess_calls == []
    assert live_stream.getStatus().status == 'Offline'
    assert live_stream.getStatus().detail == (
        'TS Codec Bridge の映像・音声コーデック組み合わせを利用できません。'
        f' ({reason_code})'
    )
    assert live_stream.disconnect_all_count == 1
    assert session.close_count == 0
    assert archiver.destroy_count == 0


def test_radio_opus_execution_boundary_rejects_unavailable_audio_only_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ラジオ実行境界は映像付き行列へ逃げず、audio-only probe 失敗時に子process起動前で拒否する。"""

    monkeypatch.setattr(_FakeChannel, 'is_radiochannel', True)
    session = _FakeSession(aiohttp.ServerDisconnectedError())
    archiver = _FakeArchiver()
    subprocess_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    live_stream = _install_mirakurun_setup(
        monkeypatch,
        session,
        archiver,
        [],
        subprocess_calls,
    )
    live_stream.encoding_options = SimpleNamespace(
        video_codec='avc',
        video_bit_depth=8,
        audio_codec='opus',
        is_hevc_10bit_enabled=False,
        is_24fps_mode_enabled=False,
    )

    async def GetAudioCapability(
        _cls: type[object],
        _audio_codec: str,
    ) -> SimpleNamespace:
        """audio-only probe 失敗を表す音声能力を返す。"""

        return SimpleNamespace(available=False, live_available=False, live_reason_code='ProbeFailed')

    async def UnexpectedLiveCombination(*_args: object, **_kwargs: object) -> None:
        """ラジオ経路が映像付き組み合わせへ退行した場合にテストを失敗させる。"""

        raise AssertionError('ラジオ経路は映像付き組み合わせを参照してはならない')

    monkeypatch.setattr(
        live_encoding_task_module.KonomiTVBS4KPlaybackCapabilityProbe,
        'getAudioCapability',
        classmethod(GetAudioCapability),
    )
    monkeypatch.setattr(
        live_encoding_task_module.KonomiTVBS4KPlaybackCapabilityProbe,
        'getLiveCombinationCapability',
        classmethod(UnexpectedLiveCombination),
    )

    with pytest.raises(RuntimeError, match=r'radio audio encoding is unavailable.*ProbeFailed'):
        asyncio.run(live_stream.task.run())

    assert subprocess_calls == []
    assert live_stream.getStatus().status == 'Offline'
    assert live_stream.getStatus().detail == (
        'TS Codec Bridge のラジオ音声能力を利用できません。 (ProbeFailed)'
    )
    assert live_stream.disconnect_all_count == 1
    assert session.close_count == 0
    assert archiver.destroy_count == 0


@pytest.mark.parametrize(
    ('status', 'headers', 'expected_detail'),
    [
        (404, {}, '現在このチャンネルは受信できません。Mirakurun 側に問題があるかもしれません。(HTTP Error 404) (E-12M)'),
        (404, {'server': 'mirakc/1.0'}, 'チューナーの起動に失敗しました。空きチューナーが不足している可能性があります。(E-12M)'),
        (503, {}, 'チューナーの起動に失敗しました。空きチューナーが不足している可能性があります。(E-12M)'),
        (500, {}, 'チューナーで不明なエラーが発生しました。Mirakurun 側に問題があるかもしれません。(HTTP Error 500) (E-12M)'),
    ],
)
def test_mirakurun_non_200_responses_cleanup_and_keep_existing_e12m_details(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    headers: dict[str, str],
    expected_detail: str,
) -> None:
    """
    非 200 応答を既存 E-12M 分類のまま終了し、起動済み資源をすべて回収する。

    Args:
        monkeypatch (pytest.MonkeyPatch): 依存関係を差し替える fixture。
        status (int): 再現する HTTP ステータス。
        headers (dict[str, str]): 再現する HTTP 応答ヘッダー。
        expected_detail (str): 維持すべき既存状態詳細。

    Returns:
        None
    """

    response = _FakeResponse(status, headers)
    session = _FakeSession(response)
    archiver = _FakeArchiver()
    tsreadex = _FakeProcess()
    encoder = _FakeProcess()
    live_stream = _install_mirakurun_setup(monkeypatch, session, archiver, [tsreadex, encoder])

    asyncio.run(live_stream.task.run())

    assert live_stream.getStatus().status == 'Offline'
    assert live_stream.getStatus().detail == expected_detail
    assert live_stream.disconnect_all_count == 1
    assert response.closed is True
    assert session.closed is True
    assert archiver.destroy_count == 1
    assert tsreadex.kill_count == 1
    assert encoder.kill_count == 1


def test_cleanup_continues_after_individual_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Session・子プロセス・アーカイバーの一部が失敗しても、接続失敗の結果をマスクせず後続を回収する。

    Args:
        monkeypatch (pytest.MonkeyPatch): 依存関係を差し替える fixture。

    Returns:
        None
    """

    session = _FakeSession(aiohttp.ServerDisconnectedError(), should_fail_to_close=True)
    archiver = _FakeArchiver(should_fail_to_destroy=True)
    failing_process = _FakeProcess(should_fail_to_kill=True)
    succeeding_process = _FakeProcess()
    processes = [failing_process, succeeding_process]
    live_stream = _install_mirakurun_setup(monkeypatch, session, archiver, processes)

    asyncio.run(live_stream.task.run())

    assert live_stream.getStatus().status == 'Offline'
    assert session.close_count == 1
    assert failing_process.kill_count == 1
    assert failing_process.wait_count == 1
    assert succeeding_process.kill_count == 1
    assert archiver.destroy_count == 1


def test_process_wait_failure_is_exposed_as_unconfirmed_cleanup() -> None:
    """
    子processをwaitできない場合はtask終了だけでcleanup成功扱いしない。

    Returns:
        None
    """

    class _UnreapedProcess(_FakeProcess):
        """kill後も終了せず、wait失敗を返すprocess代替。"""

        def kill(self) -> None:
            """
            終了要求だけを記録し、returncodeを確定しない。

            Returns:
                None
            """

            self.kill_count += 1

        async def wait(self) -> int:
            """
            process終了待ち失敗を模擬する。

            Returns:
                int: 常に例外となるため返らない。
            """

            self.wait_count += 1
            raise TimeoutError

    async def run() -> None:
        """
        cleanup結果APIがprocess残留を公開することを検証する。

        Returns:
            None
        """

        live_stream = _FakeLiveStream()
        task = LiveEncodingTask(live_stream)  # type: ignore[arg-type]
        cleanup_context = live_encoding_task_module._LiveEncodingTaskCleanupContext()
        cleanup_context.encoder = _UnreapedProcess()  # type: ignore[assignment]
        task._cleanup_context = cleanup_context

        await task._LiveEncodingTask__cleanupResources(cleanup_context)  # pyright: ignore[reportPrivateUsage]
        task._cleanup_failures.update(cleanup_context.cleanup_failures)

        assert cleanup_context.is_cleanup_completed is True
        assert task.isCleanupConfirmed() is False
        assert task.getCleanupFailures() == ('process:encoder',)

    asyncio.run(run())


def test_cleanup_failure_is_accumulated_across_internal_restart_generations() -> None:
    """
    内部Restart後の最終世代が成功しても、過去世代のprocess残留を失わない。

    Returns:
        None
    """

    live_stream = _FakeLiveStream()
    task = LiveEncodingTask(live_stream)  # type: ignore[arg-type]
    first_context = live_encoding_task_module._LiveEncodingTaskCleanupContext(
        is_cleanup_completed = True,
        cleanup_failures = {'process:encoder'},
    )
    second_context = live_encoding_task_module._LiveEncodingTaskCleanupContext(
        is_cleanup_completed = True,
    )
    task._cleanup_failures.update(first_context.cleanup_failures)
    task._cleanup_context = second_context

    assert task.isCleanupConfirmed() is False
    assert task.getCleanupFailures() == ('process:encoder',)


def test_restart_stops_after_cleanup_and_late_tuner_close_failures() -> None:
    """
    Restart世代のprocess回収失敗と後続tuner close失敗を累積し、次世代を起動しない。

    Returns:
        None
    """

    class _FailingTuner:
        """close失敗を返すEDCB tuner代替。"""

        def __init__(self) -> None:
            """
            close回数を初期化する。

            Returns:
                None
            """

            self.close_count = 0

        def getState(self) -> str:
            """
            通常所有中のtuner状態を返す。

            Returns:
                str: Cancellingではない通常状態。
            """

            return 'Watching'

        async def close(self, _live_stream_id: str) -> bool:
            """
            tuner close失敗を返す。

            Args:
                _live_stream_id (str): tuner所有stream ID。

            Returns:
                bool: 常にFalse。
            """

            self.close_count += 1
            return False

    async def verify() -> None:
        """
        cleanup失敗世代が一度だけ実行されOfflineで終了することを検証する。

        Returns:
            None
        """

        live_stream = _FakeLiveStream()
        tuner = _FailingTuner()
        live_stream.tuner = tuner
        task = LiveEncodingTask(live_stream)  # type: ignore[arg-type]
        run_count = 0

        async def run_body() -> None:
            """
            一世代目でRestartを要求する。

            Returns:
                None
            """

            nonlocal run_count
            run_count += 1
            live_stream.setStatus('Restart', 'テスト用の再起動要求です。')

        async def cleanup_with_process_failure(
            cleanup_context: live_encoding_task_module._LiveEncodingTaskCleanupContext,
        ) -> None:
            """
            process残留を記録してcleanup本体完了を模擬する。

            Args:
                cleanup_context (_LiveEncodingTaskCleanupContext): 世代cleanup状態。

            Returns:
                None
            """

            cleanup_context.cleanup_failures.add('process:encoder')
            cleanup_context.is_cleanup_completed = True

        task._LiveEncodingTask__run = run_body  # type: ignore[method-assign]
        task._LiveEncodingTask__cleanupResources = cleanup_with_process_failure  # type: ignore[method-assign]

        await task.run()

        assert run_count == 1
        assert live_stream.getStatus().status == 'Offline'
        assert tuner.close_count == 1
        assert task.isCleanupConfirmed() is False
        assert task.getCleanupFailures() == ('edcb_tuner', 'process:encoder')

    asyncio.run(verify())


def test_cancellation_re_raises_after_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    session.get() 待機中のキャンセルで、全資源を回収して CancelledError を再送出する。

    Args:
        monkeypatch (pytest.MonkeyPatch): 依存関係を差し替える fixture。

    Returns:
        None
    """

    class _BlockingResult:
        """get() をキャンセルされるまで待機させる目印。"""

    session = _FakeSession(_BlockingResult())
    archiver = _FakeArchiver()
    tsreadex = _FakeProcess()
    encoder = _FakeProcess()
    processes = [tsreadex, encoder]
    live_stream = _install_mirakurun_setup(monkeypatch, session, archiver, processes)

    async def cancel_task() -> None:
        """Mirakurun 接続待機中の run() をキャンセルする。"""

        original_get = session.get

        async def blocking_get(**kwargs: object) -> object:
            """キャンセルされるまで戻らない get() を実装する。"""

            session.get_started.set()
            await asyncio.Event().wait()
            return await original_get(**kwargs)

        session.get = blocking_get  # type: ignore[method-assign]
        running_task = asyncio.create_task(live_stream.task.run())
        await session.get_started.wait()
        running_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running_task

    asyncio.run(cancel_task())

    assert live_stream.getStatus().status == 'Offline'
    assert live_stream.disconnect_all_count == 1
    assert session.closed is True
    assert archiver.destroy_count == 1
    assert tsreadex.kill_count == 1
    assert encoder.kill_count == 1


def test_restart_reuses_managed_task_and_handoff_cancel_reaches_current_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Restart 後も単一の管理 Task を維持し、チューナー移譲相当の cancel を現世代へ届ける。

    Args:
        monkeypatch (pytest.MonkeyPatch): ログ設定への依存を差し替える fixture。

    Returns:
        None
    """

    live_stream = _FakeLiveStream()
    task = LiveEncodingTask(live_stream)  # type: ignore[arg-type]
    monkeypatch.setattr(live_encoding_task_module.logging, 'debug', lambda _message: None)
    current_iteration_started = asyncio.Event()
    block_current_iteration = asyncio.Event()
    cleanup_contexts: list[object] = []
    managed_task: asyncio.Task[None] | None = None
    run_count = 0

    async def run_body() -> None:
        """
        一度目は Restart を要求し、二度目は移譲側からキャンセルされるまで待機する。

        Args:
            None

        Returns:
            None
        """

        nonlocal run_count

        run_count += 1
        cleanup_context = task._cleanup_context
        assert cleanup_context is not None
        cleanup_contexts.append(cleanup_context)
        if run_count == 1:
            live_stream.setStatus('Restart', 'テスト用の再起動要求です。')
            return

        # 二世代目も LiveStream が最初に登録した Task の中で実行されていることを確認する
        assert asyncio.current_task() is managed_task
        current_iteration_started.set()
        await block_current_iteration.wait()

    task._LiveEncodingTask__run = run_body  # type: ignore[method-assign]

    async def verify() -> None:
        """
        管理 Task の同一性とキャンセル伝播を検証する。

        Args:
            None

        Returns:
            None
        """

        nonlocal managed_task

        # LiveStream.connect() が _live_encoding_task_ref へ登録する Task を再現する
        managed_task = asyncio.create_task(task.run())
        await asyncio.wait_for(current_iteration_started.wait(), timeout=1)
        assert managed_task.done() is False

        # チューナー移譲処理と同じく、LiveStream が保持する Task 参照からキャンセルする
        managed_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await managed_task

    asyncio.run(verify())

    assert run_count == 2
    assert len(cleanup_contexts) == 2
    assert cleanup_contexts[0] is not cleanup_contexts[1]
    assert live_stream.getStatus().status == 'Offline'


def test_old_done_callback_does_not_clear_new_live_encoding_task_ref() -> None:
    """
    旧 Task の done callback が、後から登録された現行 Task の参照を消さない。

    Args:
        None

    Returns:
        None
    """

    async def verify() -> None:
        """
        新旧 Task の完了順を競合させて参照の世代ガードを検証する。

        Args:
            None

        Returns:
            None
        """

        old_release = asyncio.Event()
        current_release = asyncio.Event()

        async def wait_for_release(release: asyncio.Event) -> None:
            """
            指定されたイベントが通知されるまで Task を生存させる。

            Args:
                release (asyncio.Event): Task の終了を許可するイベント。

            Returns:
                None
            """

            await release.wait()

        live_stream = object.__new__(LiveStream)
        live_stream._detached_live_encoding_task_refs = set()  # pyright: ignore[reportPrivateUsage]
        old_task = asyncio.create_task(wait_for_release(old_release))
        current_task = asyncio.create_task(wait_for_release(current_release))
        live_stream._live_encoding_task_ref = old_task  # pyright: ignore[reportPrivateUsage]
        live_stream._LiveStream__registerLiveEncodingTaskRef(old_task)  # pyright: ignore[reportPrivateUsage]

        # 旧 callback が呼ばれる前に現行 Task へ参照が移った状況を再現する
        live_stream._live_encoding_task_ref = current_task  # pyright: ignore[reportPrivateUsage]
        old_release.set()
        await old_task
        await asyncio.sleep(0)

        assert live_stream._live_encoding_task_ref is current_task  # pyright: ignore[reportPrivateUsage]

        current_release.set()
        await current_task

    asyncio.run(verify())
