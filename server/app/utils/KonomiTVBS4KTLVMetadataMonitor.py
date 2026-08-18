from __future__ import annotations

import asyncio
from typing import ClassVar

from app import logging
from app.constants import LIBRARY_PATH
from app.utils.KonomiTVBS4KTLVServiceResolver import KonomiTVBS4KTLVServiceResolver


class KonomiTVBS4KTLVMetadataMonitor:
    """生TLVを独立したmetadata helperへ複製し、サービス・映像メタデータを継続監視する。"""

    # 約100Mbpsで約0.7秒分だけ保持し、監視遅延をライブ再生側へ波及させない。
    MAX_BUFFERED_CHUNKS: ClassVar[int] = 128
    START_STABILITY_SECONDS: ClassVar[float] = 2.0
    END_STABILITY_SECONDS: ClassVar[float] = 5.0
    RESTART_DELAY_SECONDS: ClassVar[float] = 0.25
    WRITE_CHUNK_SIZE: ClassVar[int] = 64 * 1024

    def __init__(
        self,
        main_service_id: int | None,
        rain_service_id: int | None,
        log_prefix: str,
        initial_broadcasting_state: bool | None = None,
    ) -> None:
        """
        降雨対応放送の継続監視を初期化する。

        Args:
            main_service_id (int | None): 主階層サービスのSID。降雨監視対象がない場合はNone。
            rain_service_id (int | None): 降雨対応サービスのSID。対象がない場合はNone。
            log_prefix (str): ログへ付与するプレフィックス。
            initial_broadcasting_state (bool | None): 起動時Resolverが確定済みなら引き継ぐ送出状態。

        Returns:
            None
        """

        # helperへ渡す生TLVを保持する。offerChunk()は満杯でも待たず、ライブ入力を止めない。
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self.MAX_BUFFERED_CHUNKS)
        # SIDからcontext_idを解決するため、完全SDTが分割通知されても観測済み対応を保持する。
        self._service_contexts: dict[int, int] = {}
        # contextごとに完全MPTを受信済みかと、そのsnapshotにVideoが存在したかを保持する。
        # Falseのentryは「MPT受信済み・tracks=[]」であり、未受信とは明確に区別する。
        self._mpt_video_availability: dict[int, bool] = {}
        # Resolverが先に消費した連続TLVを最初のhelperへ直送し、有限live Queueのoverflowから保護する。
        self._initial_data: bytes | None = None
        self._initial_data_consumed = False
        # 安定待ち中の候補と、UI・Controllerへ公開済みの三値状態を分離する。
        self._candidate_state: bool | None = None
        self._broadcasting_state = initial_broadcasting_state
        # helperの再同期要求をprocess管理タスクへ通知する。Queueあふれ時に利用する。
        self._restart_event = asyncio.Event()
        # 入力EOFと明示キャンセルを区別し、自然終了時にhelperを再起動しないためのフラグ。
        self._input_finished = False
        self._cancelled = False
        # run・安定待ち・helper processの所有権をこのインスタンスへ集約する。
        self._task: asyncio.Task[None] | None = None
        self._stability_task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        # 対象SIDとログ識別子は全helper世代で共通して参照する。
        self._main_service_id = main_service_id
        self._rain_service_id = rain_service_id
        self._log_prefix = log_prefix

    @property
    def is_rain_fallback_broadcasting(self) -> bool | None:
        """
        安定確認済みの降雨対応放送送出状態を返す。

        Args:
            なし。

        Returns:
            bool | None: 実施中ならTrue、未実施ならFalse、監視不能・判定中ならNone。
        """

        return self._broadcasting_state

    def start(self) -> None:
        """
        metadata helperの管理タスクを開始する。

        Args:
            なし。

        Returns:
            None
        """

        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def offerInitialData(self, data: bytes) -> None:
        """
        Resolverが消費した先頭TLVを最初のmetadata helperへ連続したまま引き渡す。

        Args:
            data (bytes): Resolverがhead_bufferとして返した生TLV。

        Returns:
            None
        """

        if data == b'' or self._cancelled is True or self._input_finished is True:
            return
        # StreamPumpは同一event loop内でstart直後に本メソッドを呼ぶため、helper生成のawaitより先に設定される。
        # 万一writer開始後に呼ばれた場合も1chunkとしてQueueへ渡し、配信側を例外で停止させない。
        if self._initial_data_consumed is False:
            self._initial_data = data
            return
        self.offerChunk(data)

    def offerChunk(self, chunk: bytes) -> None:
        """
        ライブ入力をブロックせず監視QueueへTLVチャンクを追加する。

        Args:
            chunk (bytes): HTTP入力から受信した生TLVチャンク。

        Returns:
            None
        """

        if chunk == b'' or self._cancelled is True or self._input_finished is True:
            return

        # 監視が遅れた場合は古い不連続な解析状態を信用せず、最新チャンクからhelperを再同期する。
        if self._queue.full() is True:
            while self._queue.empty() is False:
                self._queue.get_nowait()
            self._setCandidateState(None)
            self._restart_event.set()
        self._queue.put_nowait(chunk)

    def finish(self) -> None:
        """
        TLV入力の自然EOFを監視タスクへ通知する。

        Args:
            なし。

        Returns:
            None
        """

        if self._input_finished is True:
            return
        self._input_finished = True
        if self._queue.full() is True:
            self._queue.get_nowait()
        self._queue.put_nowait(None)

    def cancel(self) -> None:
        """
        監視タスクと安定待ちへキャンセルを送る。

        Args:
            なし。

        Returns:
            None
        """

        self._cancelled = True
        if self._stability_task is not None and self._stability_task.done() is False:
            self._stability_task.cancel()
        if self._task is not None and self._task.done() is False:
            self._task.cancel()

    async def wait(self) -> None:
        """
        監視タスクとhelper processの終了を待つ。

        Args:
            なし。

        Returns:
            None
        """

        if self._task is None:
            return
        try:
            await self._task
        except asyncio.CancelledError:
            # 自身へ送ったcancelは正常終了だが、呼び出し元の選局キャンセルは握りつぶさない。
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling() > 0:
                raise

    async def _run(self) -> None:
        """
        helperを必要に応じて再起動しながらstdin writerとstdout readerを管理する。

        Args:
            なし。

        Returns:
            None
        """

        try:
            first_process = True
            while self._cancelled is False and self._input_finished is False:
                self._restart_event.clear()
                self._resetObservations(clear_published_state=first_process is False)
                first_process = False
                try:
                    process = await asyncio.subprocess.create_subprocess_exec(
                        LIBRARY_PATH['KonomiTVBS4KTLVMetadata'],
                        '-',
                        '--follow',
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                except OSError as ex:
                    logging.warning(f'{self._log_prefix} Failed to start MMT/TLV metadata monitor:', exc_info=ex)
                    await asyncio.sleep(self.RESTART_DELAY_SECONDS)
                    continue

                self._process = process
                stdin = process.stdin
                stdout = process.stdout
                if stdin is None or stdout is None:
                    await self._terminateProcess(process, ())
                    await asyncio.sleep(self.RESTART_DELAY_SECONDS)
                    continue

                writer_task = asyncio.create_task(self._writeInput(stdin))
                reader_task = asyncio.create_task(self._readMetadata(stdout))
                restart_task = asyncio.create_task(self._restart_event.wait())
                tasks = (writer_task, reader_task, restart_task)
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                # 終了した入出力タスクの例外を記録する。監視失敗は再生を止めず、次世代helperで再同期する。
                for task in done:
                    if task is restart_task or task.cancelled() is True:
                        continue
                    try:
                        task.result()
                    except Exception as ex:
                        # StreamReaderの行長超過など入出力Taskには複数の例外型があり、ここから漏れると
                        # LiveEncodingTaskのRestart後cleanupまで失敗する。監視はfail-openで再生成し、再生は継続する。
                        logging.warning(f'{self._log_prefix} MMT/TLV metadata monitor stopped:', exc_info=ex)

                await self._terminateProcess(process, tasks)
                if self._cancelled is True or self._input_finished is True:
                    return
                # helper異常終了から次世代の完全MPTを得るまでは、古い送出状態で自動切替しない。
                self._setCandidateState(None)
                await asyncio.sleep(self.RESTART_DELAY_SECONDS)
        finally:
            self._setCandidateState(None)
            if self._process is not None:
                await self._terminateProcess(self._process, ())

    async def _writeInput(self, stdin: asyncio.StreamWriter) -> None:
        """
        有限QueueのTLVチャンクをhelperのstdinへ書き込む。

        Args:
            stdin (asyncio.StreamWriter): helperの標準入力。

        Returns:
            None
        """

        # Resolverが読み取った先頭には初回SPSが含まれ得るため、live Queueより必ず先に書き込む。
        initial_data = self._initial_data
        self._initial_data = None
        self._initial_data_consumed = True
        if initial_data is not None:
            await self._writeData(stdin, initial_data)

        while True:
            chunk = await self._queue.get()
            if chunk is None:
                stdin.close()
                return
            await self._writeData(stdin, chunk)

    async def _writeData(self, stdin: asyncio.StreamWriter, data: bytes) -> None:
        """
        大きい先頭バッファも一定単位でhelperへ書き、pipeの無制限な膨張を防ぐ。

        Args:
            stdin (asyncio.StreamWriter): helperの標準入力。
            data (bytes): 書き込む連続TLV。

        Returns:
            None
        """

        for offset in range(0, len(data), self.WRITE_CHUNK_SIZE):
            stdin.write(data[offset:offset + self.WRITE_CHUNK_SIZE])
            await stdin.drain()

    async def _readMetadata(self, stdout: asyncio.StreamReader) -> None:
        """
        helperのJSON Linesを読み、SIDと完全MPTから送出状態候補を更新する。

        Args:
            stdout (asyncio.StreamReader): helperの標準出力。

        Returns:
            None
        """

        while True:
            line = await stdout.readline()
            if line == b'':
                return
            metadata = KonomiTVBS4KTLVServiceResolver.parseMetadataLine(
                line.decode('utf-8', errors='replace'),
            )
            if metadata is None:
                continue

            # 実入力ではSIDごとにSDTが順次通知されるため、helper世代内で観測した対応を蓄積する。
            self._service_contexts.update(metadata.service_contexts)
            if metadata.snapshot_type == 'MPT':
                # context未選択の空MPTはlibaribtlvの完全reset通知であり、特定contextのMPT未受信とは異なる。
                # helper側も全状態を消しているため、サービス対応・MPT受信実績を同時に破棄する。
                if metadata.snapshot_context_id is None:
                    self._service_contexts.clear()
                    self._mpt_video_availability.clear()
                else:
                    # MPTのtracksは全contextを含む完全snapshotなので、過去のtrackを累積せず丸ごと置き換える。
                    # 一方、snapshot_context_idはそのcontextのMPTを実際に受信した証拠として世代内で保持する。
                    received_context_ids = set(self._mpt_video_availability)
                    received_context_ids.add(metadata.snapshot_context_id)
                    self._mpt_video_availability = {
                        context_id: context_id in metadata.video_context_ids
                        for context_id in received_context_ids
                    }

            self._evaluateCandidateState()

    def _evaluateCandidateState(self) -> None:
        """
        主・降雨contextの完全MPTから三値の送出状態候補を算出する。

        Args:
            なし。

        Returns:
            None
        """

        if self._main_service_id is None or self._rain_service_id is None:
            return
        main_context_id = self._service_contexts.get(self._main_service_id)
        rain_context_id = self._service_contexts.get(self._rain_service_id)
        if main_context_id is None:
            return

        # 主映像を確認できない状態では、低階層の不在を放送終了として扱わない。
        if self._mpt_video_availability.get(main_context_id) is not True:
            if main_context_id in self._mpt_video_availability:
                self._setCandidateState(None)
            return

        # 主映像が正常な一方で降雨対応SIDまたはそのMPTが現れない状態は、終了側の5秒安定待ちを経て
        # 「未実施」と確定する。SDTがSIDごとに順次届く途中なら、後続MPTで候補が上書きされる。
        if rain_context_id is None or rain_context_id not in self._mpt_video_availability:
            self._setCandidateState(False)
            return
        self._setCandidateState(self._mpt_video_availability[rain_context_id])

    def _setCandidateState(self, state: bool | None) -> None:
        """
        状態候補を更新し、確定値の即時無効化または安定待ちを開始する。

        Args:
            state (bool | None): 新しい送出状態候補。

        Returns:
            None
        """

        if state == self._candidate_state:
            if state is None and self._broadcasting_state is None:
                return
            if state is not None and self._stability_task is not None:
                return
        self._candidate_state = state
        if self._stability_task is not None and self._stability_task.done() is False:
            self._stability_task.cancel()
        self._stability_task = None

        # 入力欠落や未判定は直ちに公開し、古い確定値による自動切替を禁止する。
        if state is None:
            self._broadcasting_state = None
            return
        if state == self._broadcasting_state:
            return

        delay = self.START_STABILITY_SECONDS if state is True else self.END_STABILITY_SECONDS
        self._stability_task = asyncio.create_task(self._publishStableState(state, delay))

    async def _publishStableState(self, state: bool, delay: float) -> None:
        """
        候補が所定時間変化しなかった場合だけ送出状態を公開する。

        Args:
            state (bool): 安定確認する送出状態候補。
            delay (float): 確定まで待つ秒数。

        Returns:
            None
        """

        await asyncio.sleep(delay)
        if self._candidate_state == state:
            self._broadcasting_state = state

    def _resetObservations(self, *, clear_published_state: bool) -> None:
        """
        helper世代に依存するSID・MPT観測結果を初期化する。

        Args:
            clear_published_state (bool): 再同期中として公開状態もNoneへ戻すか。

        Returns:
            None
        """

        self._service_contexts.clear()
        self._mpt_video_availability.clear()
        if clear_published_state is True:
            self._setCandidateState(None)
        else:
            self._candidate_state = self._broadcasting_state

    async def _terminateProcess(
        self,
        process: asyncio.subprocess.Process,
        tasks: tuple[asyncio.Task[object], ...],
    ) -> None:
        """
        helperの入出力タスクを止め、processへkillを送って終了を回収する。

        Args:
            process (asyncio.subprocess.Process): 回収対象のhelper process。
            tasks (tuple[asyncio.Task[object], ...]): processに付随する入出力・再起動待ちタスク。

        Returns:
            None
        """

        for task in tasks:
            if task.done() is False:
                task.cancel()
        if process.stdin is not None:
            try:
                process.stdin.close()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.shield(process.wait())
        except (ProcessLookupError, OSError) as ex:
            logging.debug(f'{self._log_prefix} Failed to wait for MMT/TLV metadata monitor:', exc_info=ex)
        finally:
            if len(tasks) > 0:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self._process is process:
                self._process = None
