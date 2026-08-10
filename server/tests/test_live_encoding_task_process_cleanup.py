"""LiveEncodingTask.TerminateSubprocesses() と run() のサブプロセス回収契約を検証するテスト。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from app.streams.LiveEncodingTask import LiveEncodingTask


class FakeProcess:
    """asyncio.subprocess.Process 相当の振る舞いを再現する偽プロセス。

    kill / wait の呼び出し回数と発生させる例外だけを外部から観測できるようにする。
    """

    def __init__(
        self,
        *,
        terminated: bool = False,
        kill_error: BaseException | None = None,
        wait_error: BaseException | None = None,
        wait_blocker: asyncio.Event | None = None,
    ) -> None:
        # 既に終了済みかどうか (returncode が設定済みなら終了済み)
        self.returncode: int | None = 0 if terminated else None
        self.kill_count = 0
        self.wait_count = 0
        # wait() へ到達したことを外部から観測するためのイベント
        self.wait_started = asyncio.Event()
        self._kill_error = kill_error
        self._wait_error = wait_error
        self._wait_blocker = wait_blocker
        # run() は encoder/bridge の stdout が None でないことを期待する
        self.stdout = object()
        self.stdin = object()

    def kill(self) -> None:
        self.kill_count += 1
        if self._kill_error is not None:
            raise self._kill_error
        self.returncode = -9

    async def wait(self) -> int:
        self.wait_count += 1
        self.wait_started.set()
        # 終了しないプロセス (wait 停滞) を再現する
        if self._wait_blocker is not None:
            await self._wait_blocker.wait()
        if self._wait_error is not None:
            raise self._wait_error
        # kill されていないのに wait された場合は実プロセスと同様に待ち続ける
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode


def _BuildTask(monkeypatch: pytest.MonkeyPatch) -> tuple[LiveEncodingTask, dict[str, list[str]]]:
    """log_prefix を持つ偽 LiveStream とログ記録スタブを仕込んだタスクを返す。"""

    logs: dict[str, list[str]] = {'debug': [], 'warning': [], 'error': []}
    monkeypatch.setattr(
        'app.streams.LiveEncodingTask.logging',
        SimpleNamespace(
            debug=lambda message, exc_info=None: logs['debug'].append(str(message)),
            info=lambda message, exc_info=None: None,
            warning=lambda message, exc_info=None: logs['warning'].append(str(message)),
            error=lambda message, exc_info=None: logs['error'].append(str(message)),
        ),
    )
    task = object.__new__(LiveEncodingTask)
    task.live_stream = SimpleNamespace(log_prefix='[Test] ')
    return task, logs


class TestTerminateSubprocesses:
    """TerminateSubprocesses() 単体の回収契約を検証する。"""

    def test_running_processes_are_killed_and_awaited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """未終了のプロセスはすべて kill され、wait で回収されること。"""

        task, _ = _BuildTask(monkeypatch)
        tsreadex, encoder, bridge = FakeProcess(), FakeProcess(), FakeProcess()

        asyncio.run(task.TerminateSubprocesses(
            ('tsreadex', tsreadex), ('encoder', encoder), ('bridge', bridge),  # type: ignore[arg-type]
        ))

        for process in (tsreadex, encoder, bridge):
            assert process.kill_count == 1
            assert process.wait_count == 1

    def test_terminated_process_is_not_killed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """既に終了済みのプロセスへは不要な kill を行わないこと。"""

        task, _ = _BuildTask(monkeypatch)
        process = FakeProcess(terminated=True)

        asyncio.run(task.TerminateSubprocesses(('encoder', process)))  # type: ignore[arg-type]

        assert process.kill_count == 0
        assert process.wait_count == 0

    def test_none_process_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """bridge 未使用などで process が None の場合は何もしないこと。"""

        task, logs = _BuildTask(monkeypatch)

        # 例外なく完了すること
        asyncio.run(task.TerminateSubprocesses(('bridge', None)))

        assert logs['debug'] == []

    def test_first_wait_hang_still_kills_remaining_processes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """先頭プロセスの wait が停滞しても後続プロセスへ kill が送られること。

        あわせて、wait 停滞中の再キャンセルでも全プロセスが kill 済みのまま
        CancelledError が伝播することを検証する。
        """

        task, _ = _BuildTask(monkeypatch)
        first = FakeProcess(wait_blocker=asyncio.Event())
        second = FakeProcess()

        async def Run() -> None:
            cleanup = asyncio.create_task(task.TerminateSubprocesses(
                ('tsreadex', first), ('encoder', second),  # type: ignore[arg-type]
            ))
            # 先頭プロセスの wait へ到達するまで待つ
            await asyncio.sleep(0.05)
            # 先頭の wait が停滞していても、kill フェーズで後続プロセスへ kill が送信済み
            assert first.kill_count == 1
            assert second.kill_count == 1
            assert second.wait_count == 0  # wait フェーズには到達していない
            # wait 中の再キャンセルでも kill 済みのまま CancelledError が伝播する
            cleanup.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cleanup

        asyncio.run(Run())

    def test_kill_process_lookup_error_continues_remaining(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """kill 直前に終了した競合 (ProcessLookupError) は静かに無視し、残りを回収し続けること。"""

        task, logs = _BuildTask(monkeypatch)
        racing_process = FakeProcess(kill_error=ProcessLookupError())
        next_process = FakeProcess()

        asyncio.run(task.TerminateSubprocesses(
            ('tsreadex', racing_process), ('encoder', next_process),  # type: ignore[arg-type]
        ))

        # 競合したプロセスは kill 試行のみで wait には進まず、残りのプロセスは通常どおり回収される
        assert racing_process.kill_count == 1
        assert racing_process.wait_count == 0
        assert next_process.kill_count == 1
        assert next_process.wait_count == 1
        assert logs['debug'] == []

    def test_kill_os_error_is_logged_and_continues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """kill の OSError はサブプロセス名を含む debug ログに記録され、残りの回収を中断しないこと。"""

        task, logs = _BuildTask(monkeypatch)
        failing_process = FakeProcess(kill_error=OSError('simulated kill failure'))
        next_processes = (FakeProcess(), FakeProcess())

        asyncio.run(task.TerminateSubprocesses(
            ('tsreadex', failing_process), ('encoder', next_processes[0]), ('bridge', next_processes[1]),  # type: ignore[arg-type]
        ))

        assert failing_process.kill_count == 1
        for process in next_processes:
            assert process.kill_count == 1
            assert process.wait_count == 1
        assert len(logs['debug']) == 1
        assert 'tsreadex' in logs['debug'][0]

    def test_wait_os_error_is_logged_and_continues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """wait の OSError はサブプロセス名を含む debug ログに記録され、残りの回収を中断しないこと。"""

        task, logs = _BuildTask(monkeypatch)
        failing_process = FakeProcess(wait_error=OSError('simulated wait failure'))
        next_process = FakeProcess()

        asyncio.run(task.TerminateSubprocesses(
            ('tsreadex', failing_process), ('encoder', next_process),  # type: ignore[arg-type]
        ))

        assert failing_process.kill_count == 1
        assert next_process.kill_count == 1
        assert next_process.wait_count == 1
        assert len(logs['debug']) == 1
        assert 'tsreadex' in logs['debug'][0]

    def test_cancelled_error_is_not_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """wait 中の CancelledError は握り潰さず呼び出し元へ伝播すること。"""

        task, _ = _BuildTask(monkeypatch)
        # LiveStream.connect() からのキャンセルが wait() へ届いた状況を再現する
        process = FakeProcess(wait_error=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(task.TerminateSubprocesses(('tsreadex', process)))  # type: ignore[arg-type]

        # kill フェーズは完了している (キャンセル前に kill 済み)
        assert process.kill_count == 1

    def test_process_is_not_collected_twice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """同じプロセスを二度回収しても二重に kill されないこと。"""

        task, _ = _BuildTask(monkeypatch)
        process = FakeProcess()

        async def Run() -> None:
            await task.TerminateSubprocesses(('bridge', process))  # type: ignore[arg-type]
            # 1 回目の回収で returncode が設定されるため、2 回目は何もしない
            await task.TerminateSubprocesses(('bridge', process))  # type: ignore[arg-type]

        asyncio.run(Run())

        assert process.kill_count == 1
        assert process.wait_count == 1


class TestRunUnexpectedErrorCleanup:
    """run() のチューナー接続フェーズで想定外例外が発生しても、起動済みプロセスとセッションが回収されることを検証する。"""

    def _SetupRun(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        backend: str,
        bridge_required: bool,
    ) -> tuple[LiveEncodingTask, list[FakeProcess], SimpleNamespace]:
        """run() をチューナー接続直前まで進められる最小構成を組み立てる。"""

        spawned: list[FakeProcess] = []

        # app.logging.debug は内部で Config() を参照するため、run() 経路ではモジュールの logging をスタブ化する
        monkeypatch.setattr(
            'app.streams.LiveEncodingTask.logging',
            SimpleNamespace(
                debug=lambda message, exc_info=None: None,
                info=lambda message, exc_info=None: None,
                warning=lambda message, exc_info=None: None,
                error=lambda message, exc_info=None: None,
            ),
        )

        async def FakeCreateSubprocessExec(*args: Any, **kwargs: Any) -> FakeProcess:
            process = FakeProcess()
            spawned.append(process)
            return process

        monkeypatch.setattr('asyncio.subprocess.create_subprocess_exec', FakeCreateSubprocessExec)
        monkeypatch.setattr(
            'app.streams.LiveEncodingTask.Config',
            lambda: SimpleNamespace(
                general=SimpleNamespace(live_stream_backend=backend, backend=backend),
                tv=SimpleNamespace(debug_mode_ts_path=None),
            ),
        )
        channel = SimpleNamespace(
            network_id=32736,
            service_id=101,
            transport_stream_id=16400,
            type='BS4K',
            is_radiochannel=False,
            is_oneseg=False,
        )
        channel.getCurrentAndNextProgram = lambda: _AsyncReturn((None, None))

        async def First() -> Any:
            return channel

        monkeypatch.setattr(
            'app.streams.LiveEncodingTask.Channel',
            SimpleNamespace(filter=lambda **kwargs: SimpleNamespace(first=First)),
        )
        monkeypatch.setattr('app.streams.LiveEncodingTask.GetEncoderForLiveChannel', lambda channel_id: 'FFmpeg')
        monkeypatch.setattr(
            'app.streams.LiveEncodingTask.TSCodecBridgeRuntimeVerifier',
            SimpleNamespace(isAvailable=lambda path: True, recordProcessStart=lambda kind: None),
        )
        monkeypatch.setattr(
            'app.streams.LiveEncodingTask.RecordedPlaybackBackend',
            SimpleNamespace(getExecutable=lambda encoder: 'encoder', getEnvironment=lambda encoder: {}),
        )
        archiver_instances: list[SimpleNamespace] = []

        def FakeArchiver(service_id: int) -> SimpleNamespace:
            archiver = SimpleNamespace(destroy=lambda: None)
            archiver_instances.append(archiver)
            return archiver

        monkeypatch.setattr('app.streams.LiveEncodingTask.LivePSIDataArchiver', FakeArchiver)
        monkeypatch.setattr('app.streams.LiveEncodingTask.GetMirakurunAPIEndpointURL', lambda path: 'http://localhost/')

        status = SimpleNamespace(status='Standby', detail='エンコードタスクを起動しています…')
        live_stream = SimpleNamespace(
            log_prefix='[Test] ',
            display_channel_id='bs4k101',
            live_stream_id='bs4k101-1080p',
            quality='1080p',
            stream_anchor_enabled=False,
            tuner=None,
            psi_data_archiver=None,
            disconnected=0,
            current_status=status,
        )
        live_stream.getStatus = lambda: status
        live_stream.setStatus = lambda new_status, detail: setattr(status, 'status', new_status) or True
        live_stream.disconnectAll = lambda: setattr(live_stream, 'disconnected', live_stream.disconnected + 1)

        task = LiveEncodingTask(live_stream)  # type: ignore[arg-type]
        # エンコードオプション構築やチャンネル種別の外部依存を切り離す
        task.isFullHDChannel = lambda network_id, service_id: False  # type: ignore[method-assign]
        task.buildFFmpegOptions = lambda *args: ['-x']  # type: ignore[method-assign]
        task.IsTSCodecBridgeRequired = lambda is_radiochannel=False: bridge_required  # type: ignore[method-assign]
        task.BuildTSCodecBridgeOptions = lambda is_radiochannel=False, is_oneseg=False: ['--x']  # type: ignore[method-assign]
        return task, spawned, live_stream

    def test_mirakurun_unexpected_error_cleans_up_all_processes_and_session(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mirakurun 接続時の想定外例外 (ServerDisconnectedError) でも tsreadex / bridge / encoder と session が回収されること。"""

        task, spawned, live_stream = self._SetupRun(monkeypatch, backend='Mirakurun', bridge_required=True)

        class FakeSession:
            def __init__(self) -> None:
                self.closed = False
                self.close_count = 0

            async def get(self, *args: Any, **kwargs: Any) -> Any:
                raise aiohttp.ServerDisconnectedError()

            async def close(self) -> None:
                self.closed = True
                self.close_count += 1

        sessions: list[FakeSession] = []
        monkeypatch.setattr(
            'app.streams.LiveEncodingTask.aiohttp',
            SimpleNamespace(
                ClientSession=lambda: sessions.append(FakeSession()) or sessions[-1],
                ClientTimeout=aiohttp.ClientTimeout,
                ClientConnectorError=aiohttp.ClientConnectorError,
            ),
        )

        async def AcquireTuner(channel_type: str) -> bool:
            return True

        task.acquireMirakurunTuner = AcquireTuner  # type: ignore[method-assign]

        with pytest.raises(aiohttp.ServerDisconnectedError):
            asyncio.run(task.run())

        # tsreadex / bridge / encoder の 3 プロセスが生成され、すべて kill + wait で回収されている
        assert len(spawned) == 3
        for process in spawned:
            assert process.kill_count == 1
            assert process.wait_count == 1
        # HTTP セッションも閉じられている
        assert len(sessions) == 1
        assert sessions[0].closed is True
        # クライアント切断とアーカイバー破棄も行われている
        assert live_stream.disconnected >= 1
        assert live_stream.psi_data_archiver is None
        # Standby のまま残らず Offline へ遷移し、次回接続でエンコードタスクを再起動できる
        assert live_stream.current_status.status == 'Offline'

    def test_edcb_unexpected_error_cleans_up_all_processes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EDCB チューナー起動時の想外例外でも tsreadex / encoder が回収され、例外が伝播すること。"""

        task, spawned, live_stream = self._SetupRun(monkeypatch, backend='EDCB', bridge_required=False)

        tuner = SimpleNamespace(close_count=0)

        async def SetChannel(*args: Any) -> bool:
            raise RuntimeError('simulated EDCB failure')

        async def CloseTuner(live_stream_id: str) -> bool:
            tuner.close_count += 1
            return True

        tuner.getEDCBNetworkTVID = lambda: 0
        tuner.getState = lambda: 'Active'
        tuner.setChannel = SetChannel
        tuner.close = CloseTuner
        monkeypatch.setattr(
            'app.streams.LiveEncodingTask.EDCBTuner',
            SimpleNamespace(getOrCreate=lambda live_stream_id: tuner),
        )

        with pytest.raises(RuntimeError, match='simulated EDCB failure'):
            asyncio.run(task.run())

        # tsreadex / encoder (bridge 未使用) がすべて kill + wait で回収されている
        assert len(spawned) == 2
        for process in spawned:
            assert process.kill_count == 1
            assert process.wait_count == 1
        # 所有中の EDCB チューナーが閉じられ、LiveStream からも切り離されている
        assert tuner.close_count == 1
        assert live_stream.tuner is None
        assert live_stream.disconnected >= 1
        # Standby のまま残らず Offline へ遷移し、次回接続でエンコードタスクを再起動できる
        assert live_stream.current_status.status == 'Offline'

    def test_encoder_option_build_error_cleans_up_tsreadex_and_archiver(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """エンコーダーオプション構築の例外でも、生成済みの tsreadex と PSI アーカイバーが回収されること。"""

        task, spawned, live_stream = self._SetupRun(monkeypatch, backend='Mirakurun', bridge_required=False)

        def RaiseOptionBuild(*args: Any) -> list[str]:
            raise RuntimeError('simulated option build failure')

        task.buildFFmpegOptions = RaiseOptionBuild  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match='simulated option build failure'):
            asyncio.run(task.run())

        # tsreadex だけが生成され、kill + wait で回収されている
        assert len(spawned) == 1
        assert spawned[0].kill_count == 1
        assert spawned[0].wait_count == 1
        # クライアント切断とアーカイバー破棄も行われている
        assert live_stream.disconnected >= 1
        assert live_stream.psi_data_archiver is None
        # Offline へ遷移し、次回接続でエンコードタスクを再起動できる
        assert live_stream.current_status.status == 'Offline'

    def test_tsreadex_start_error_releases_archiver_and_marks_offline(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """tsreadex の起動失敗でも、PSI アーカイバーとクライアントを解放して Offline へ戻ること。"""

        task, spawned, live_stream = self._SetupRun(monkeypatch, backend='Mirakurun', bridge_required=False)

        async def RaiseTSReadexStartError(*args: Any, **kwargs: Any) -> FakeProcess:
            raise RuntimeError('simulated tsreadex startup failure')

        monkeypatch.setattr('asyncio.subprocess.create_subprocess_exec', RaiseTSReadexStartError)

        with pytest.raises(RuntimeError, match='simulated tsreadex startup failure'):
            asyncio.run(task.run())

        # プロセス生成前の失敗なので回収対象はないが、共有資源と状態は必ず回復する
        assert spawned == []
        assert live_stream.disconnected >= 1
        assert live_stream.psi_data_archiver is None
        assert live_stream.current_status.status == 'Offline'

    def test_tsreadex_pipe_creation_error_releases_archiver_and_marks_offline(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """tsreadex 用パイプの作成失敗でも、PSI アーカイバーとクライアントを解放して Offline へ戻ること。"""

        task, spawned, live_stream = self._SetupRun(monkeypatch, backend='Mirakurun', bridge_required=False)

        def RaisePipeCreationError() -> tuple[int, int]:
            raise OSError('simulated pipe creation failure')

        monkeypatch.setattr(
            'app.streams.LiveEncodingTask.os',
            SimpleNamespace(pipe=RaisePipeCreationError, close=lambda fd: None),
        )

        with pytest.raises(OSError, match='simulated pipe creation failure'):
            asyncio.run(task.run())

        assert spawned == []
        assert live_stream.disconnected >= 1
        assert live_stream.psi_data_archiver is None
        assert live_stream.current_status.status == 'Offline'

    def test_cleanup_releases_session_and_clients_before_wait_completes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """先頭プロセスの wait が停滞しても、session・クライアント・アーカイバーの解放は wait 前に完了すること。"""

        task, spawned, live_stream = self._SetupRun(monkeypatch, backend='Mirakurun', bridge_required=False)
        blocker = asyncio.Event()

        async def FakeCreateWithBlockingWait(*args: Any, **kwargs: Any) -> FakeProcess:
            # 先頭 (tsreadex) の wait だけを停滞させる
            process = FakeProcess(wait_blocker=blocker if len(spawned) == 0 else None)
            spawned.append(process)
            return process

        monkeypatch.setattr('asyncio.subprocess.create_subprocess_exec', FakeCreateWithBlockingWait)

        class FakeSession:
            def __init__(self) -> None:
                self.closed = False

            async def get(self, *args: Any, **kwargs: Any) -> Any:
                raise aiohttp.ServerDisconnectedError()

            async def close(self) -> None:
                self.closed = True

        sessions: list[FakeSession] = []
        monkeypatch.setattr(
            'app.streams.LiveEncodingTask.aiohttp',
            SimpleNamespace(
                ClientSession=lambda: sessions.append(FakeSession()) or sessions[-1],
                ClientTimeout=aiohttp.ClientTimeout,
                ClientConnectorError=aiohttp.ClientConnectorError,
            ),
        )

        async def AcquireTuner(channel_type: str) -> bool:
            return True

        task.acquireMirakurunTuner = AcquireTuner  # type: ignore[method-assign]

        async def Run() -> None:
            run_task = asyncio.create_task(task.run())
            # tsreadex の wait へ到達するまで待つ (回収処理が wait で停滞している状態)
            for _ in range(1000):
                if len(spawned) >= 2 and spawned[0].wait_started.is_set() is True:
                    break
                await asyncio.sleep(0.01)
            assert spawned[0].wait_started.is_set() is True

            # wait が完了していなくても、kill・session 切断・クライアント切断・アーカイバー破棄は完了している
            assert all(process.kill_count == 1 for process in spawned)
            assert len(sessions) == 1 and sessions[0].closed is True
            assert live_stream.disconnected >= 1
            assert live_stream.psi_data_archiver is None
            assert run_task.done() is False

            # wait の停滞を解消すると、例外が呼び出し元へ伝播する
            blocker.set()
            with pytest.raises(aiohttp.ServerDisconnectedError):
                await run_task

        asyncio.run(Run())

    def test_bridge_option_build_error_closes_all_pipes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bridge オプション構築の例外でも、tsreadex 用・Bridge 用の全パイプ FD が close されること。"""

        task, spawned, live_stream = self._SetupRun(monkeypatch, backend='Mirakurun', bridge_required=True)

        # os.pipe / os.close を記録するラッパー (他の os 属性はそのまま透過させる)
        import os as real_os

        class RecordingOS:
            def __init__(self) -> None:
                self.pipes: list[tuple[int, int]] = []
                self.closed: list[int] = []

            def pipe(self) -> tuple[int, int]:
                pipe = real_os.pipe()
                self.pipes.append(pipe)
                return pipe

            def close(self, fd: int) -> None:
                self.closed.append(fd)
                real_os.close(fd)

            def __getattr__(self, name: str) -> Any:
                return getattr(real_os, name)

        recording_os = RecordingOS()
        monkeypatch.setattr('app.streams.LiveEncodingTask.os', recording_os)

        def RaiseBridgeOptionBuild(**kwargs: Any) -> list[str]:
            raise RuntimeError('simulated bridge option build failure')

        task.BuildTSCodecBridgeOptions = RaiseBridgeOptionBuild  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match='simulated bridge option build failure'):
            asyncio.run(task.run())

        # tsreadex 用・Bridge 用の両パイプ (計 4 FD) がすべて close されている
        assert len(recording_os.pipes) == 2
        all_fds = {fd for pipe in recording_os.pipes for fd in pipe}
        assert all_fds <= set(recording_os.closed)
        # tsreadex は kill + wait で回収され、Offline へ遷移している
        assert len(spawned) == 1
        assert spawned[0].kill_count == 1
        assert spawned[0].wait_count == 1
        assert live_stream.current_status.status == 'Offline'

    def test_edcb_tuner_close_stall_still_kills_processes_first(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """EDCB tuner.close() が停滞しても、完了前に全プロセスへの kill と解放処理が済んでいること。"""

        task, spawned, live_stream = self._SetupRun(monkeypatch, backend='EDCB', bridge_required=False)

        close_started = asyncio.Event()
        close_blocker = asyncio.Event()
        tuner = SimpleNamespace(close_count=0)

        async def SetChannel(*args: Any) -> bool:
            raise RuntimeError('simulated EDCB failure')

        async def CloseTuner(live_stream_id: str) -> bool:
            close_started.set()
            await close_blocker.wait()
            tuner.close_count += 1
            return True

        tuner.getEDCBNetworkTVID = lambda: 0
        tuner.getState = lambda: 'Active'
        tuner.setChannel = SetChannel
        tuner.close = CloseTuner
        monkeypatch.setattr(
            'app.streams.LiveEncodingTask.EDCBTuner',
            SimpleNamespace(getOrCreate=lambda live_stream_id: tuner),
        )

        async def Run() -> None:
            run_task = asyncio.create_task(task.run())
            # tuner.close() の待機中状態になるまで待つ
            for _ in range(1000):
                if close_started.is_set() is True:
                    break
                await asyncio.sleep(0.01)
            assert close_started.is_set() is True

            # tuner.close() が完了していなくても、全プロセスへの kill と解放処理は完了している
            assert len(spawned) == 2
            assert all(process.kill_count == 1 for process in spawned)
            assert live_stream.disconnected >= 1
            assert live_stream.psi_data_archiver is None
            assert live_stream.current_status.status == 'Offline'
            assert run_task.done() is False

            # tuner.close() の停滞を解消すると、例外が呼び出し元へ伝播する
            close_blocker.set()
            with pytest.raises(RuntimeError, match='simulated EDCB failure'):
                await run_task
            assert tuner.close_count == 1
            assert live_stream.tuner is None

        asyncio.run(Run())

    def test_edcb_tuner_close_error_preserves_original_error_and_waits_processes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """EDCB tuner.close() が失敗しても、元の例外を維持して全プロセスの終了を待つこと。"""

        task, spawned, live_stream = self._SetupRun(monkeypatch, backend='EDCB', bridge_required=False)
        tuner = SimpleNamespace()

        async def SetChannel(*args: Any) -> bool:
            raise RuntimeError('original encoding failure')

        async def CloseTuner(live_stream_id: str) -> bool:
            raise OSError('simulated tuner close failure')

        tuner.getEDCBNetworkTVID = lambda: 0
        tuner.getState = lambda: 'Active'
        tuner.setChannel = SetChannel
        tuner.close = CloseTuner
        monkeypatch.setattr(
            'app.streams.LiveEncodingTask.EDCBTuner',
            SimpleNamespace(getOrCreate=lambda live_stream_id: tuner),
        )

        # cleanup 側の OSError ではなく、処理を開始した元の例外が呼び出し元へ伝播する
        with pytest.raises(RuntimeError, match='original encoding failure'):
            asyncio.run(task.run())

        assert len(spawned) == 2
        assert all(process.kill_count == 1 for process in spawned)
        assert all(process.wait_count == 1 for process in spawned)
        # close に失敗したチューナー参照は、閉じたものとして誤って破棄しない
        assert live_stream.tuner is tuner
        assert live_stream.current_status.status == 'Offline'

    def test_edcb_tuner_close_recancellation_still_waits_processes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """EDCB tuner.close() 中の再キャンセルでも、全プロセスの終了待機を完了してから伝播すること。"""

        task, spawned, live_stream = self._SetupRun(monkeypatch, backend='EDCB', bridge_required=False)
        close_started = asyncio.Event()
        close_blocker = asyncio.Event()
        tuner = SimpleNamespace()

        async def SetChannel(*args: Any) -> bool:
            raise RuntimeError('original encoding failure')

        async def CloseTuner(live_stream_id: str) -> bool:
            close_started.set()
            await close_blocker.wait()
            return True

        tuner.getEDCBNetworkTVID = lambda: 0
        tuner.getState = lambda: 'Active'
        tuner.setChannel = SetChannel
        tuner.close = CloseTuner
        monkeypatch.setattr(
            'app.streams.LiveEncodingTask.EDCBTuner',
            SimpleNamespace(getOrCreate=lambda live_stream_id: tuner),
        )

        async def Run() -> None:
            run_task = asyncio.create_task(task.run())
            for _ in range(1000):
                if close_started.is_set() is True:
                    break
                await asyncio.sleep(0.01)
            assert close_started.is_set() is True

            run_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run_task

            assert all(process.kill_count == 1 for process in spawned)
            assert all(process.wait_count == 1 for process in spawned)
            assert live_stream.tuner is tuner
            assert live_stream.current_status.status == 'Offline'

        asyncio.run(Run())

    def test_session_close_recancellation_still_completes_remaining_cleanup(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """session.close() 中の再キャンセルでも、チューナー解放とプロセス終了待機などの後続 cleanup が完了すること。"""

        task, spawned, live_stream = self._SetupRun(monkeypatch, backend='Mirakurun', bridge_required=False)

        close_started = asyncio.Event()
        close_blocker = asyncio.Event()

        class FakeSession:
            def __init__(self) -> None:
                self.closed = False

            async def get(self, *args: Any, **kwargs: Any) -> Any:
                raise aiohttp.ServerDisconnectedError()

            async def close(self) -> None:
                close_started.set()
                # 再キャンセルが届くまで停滞する (close は完了しない)
                await close_blocker.wait()
                self.closed = True

        sessions: list[FakeSession] = []
        monkeypatch.setattr(
            'app.streams.LiveEncodingTask.aiohttp',
            SimpleNamespace(
                ClientSession=lambda: sessions.append(FakeSession()) or sessions[-1],
                ClientTimeout=aiohttp.ClientTimeout,
                ClientConnectorError=aiohttp.ClientConnectorError,
            ),
        )

        async def AcquireTuner(channel_type: str) -> bool:
            return True

        task.acquireMirakurunTuner = AcquireTuner  # type: ignore[method-assign]

        async def Run() -> None:
            run_task = asyncio.create_task(task.run())
            # session.close() の待機中状態になるまで待つ
            for _ in range(1000):
                if close_started.is_set() is True:
                    break
                await asyncio.sleep(0.01)
            assert close_started.is_set() is True

            # session.close() の停滞中に run タスクへ再キャンセルを送る
            run_task.cancel()

            # CancelledError が伝播するが、後続 cleanup (終了待機・Offline 遷移) は完了している
            with pytest.raises(asyncio.CancelledError):
                await run_task
            assert all(process.kill_count == 1 for process in spawned)
            assert all(process.wait_count == 1 for process in spawned)
            # await を伴わない解放処理は session.close() より前に完了している
            assert live_stream.disconnected >= 1
            assert live_stream.psi_data_archiver is None
            assert live_stream.current_status.status == 'Offline'

        asyncio.run(Run())


async def _AsyncReturn(value: Any) -> Any:
    return value
