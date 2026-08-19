
import asyncio
from collections.abc import AsyncGenerator, Coroutine
from typing import Any

from fastapi import Request

from app import logging
from app.constants import LIBRARY_PATH


class LivePSIDataArchiver:

    # 標準入力を閉じた psisiarc が自発終了するまで待つ上限 (秒)
    # 通常は EOF を受け取って即座に終了するが、停止した場合は kill へフォールバックする
    PROCESS_GRACEFUL_SHUTDOWN_TIMEOUT: float = 1.0

    def __init__(self, service_id: int) -> None:
        """
        ライブストリーミング (データ放送) 用 PSI/SI データアーカイバーを初期化する

        Args:
            service_id (int): 視聴対象のチャンネルのサービス ID
        """

        # 視聴対象のチャンネルのサービス ID
        self.service_id = service_id

        # psisiarc のプロセスリスト
        self._psisiarc_processes: list[asyncio.subprocess.Process] = []


    async def pushTSPacketData(self, packet: bytes) -> None:
        """
        LiveEncodingTask から生の放送波の MPEG2-TS パケットを受け取り、登録されている PSI/SI データアーカイバープロセスに送信する
        188 bytes ぴったりで送信する必要はない

        Args:
            packet (bytes): MPEG2-TS パケット
        """

        tasks: list[Coroutine[Any, Any, None]] = []
        for psisiarc_process in self._psisiarc_processes:
            assert type(psisiarc_process.stdin) is asyncio.StreamWriter

            # psisiarc が既に終了中の場合は何もしない
            if psisiarc_process.stdin.is_closing():
                continue

            # psisiarc に TS パケットを送信する
            psisiarc_process.stdin.write(packet)
            tasks.append(psisiarc_process.stdin.drain())

        # 複数のプロセス分並行して drain() する
        ## タスクのいずれかで例外が発生しても、ほかのタスクは継続する
        await asyncio.gather(*tasks, return_exceptions=True)


    async def getPSIArchivedData(self, request: Request) -> AsyncGenerator[bytes, None]:
        """
        PSI/SI データアーカイバープロセスを起動し、受信した PSI/SI アーカイブデータをジェネレーターとして返す
        FastAPI の StreamingResponse での利用を想定している

        Args:
            request (Request): FastAPI の Request オブジェクト

        Yields:
            AsyncGenerator[bytes, None]: PSI/SI アーカイブデータ
        """

        # psisiarc のオプション
        # ref: https://github.com/xtne6f/psisiarc
        psisiarc_options = [
            # 事前定義されたオプションを追加する
            # 番組情報とデータ放送を抽出する
            '-r', 'arib-data',
            # 特定サービスのみを選択して出力するフィルタを有効にする
            ## 有効にすると、特定のストリームのみ PID を固定して出力される
            ## 視聴対象のチャンネルのサービス ID を指定する
            '-n ', str(self.service_id),
            # PCR (Program Clock Reference) を基準に一定間隔でアーカイブデータを出力する
            # 1秒間隔でアーカイブデータを出力する
            '-i', '1',
            # 標準入力から放送波を入力する
            '-',
            # 標準出力にアーカイブデータを出力する
            '-',
        ]

        # psisiarc を起動する
        psisiarc_process = await asyncio.subprocess.create_subprocess_exec(
            *[LIBRARY_PATH['psisiarc'], *psisiarc_options],
            stdin = asyncio.subprocess.PIPE,  # 放送波を入力
            stdout = asyncio.subprocess.PIPE,  # ストリーム出力
            stderr = asyncio.subprocess.DEVNULL,
        )
        self._psisiarc_processes.append(psisiarc_process)
        logging.debug(f'[LivePSIDataArchiver] psisiarc started. (PID: {psisiarc_process.pid})')

        # consumer 切断・generator cancellation・読み取り例外のどの経路でも、
        # 起動した psisiarc とパイプを必ず回収できるよう generator 全体を finally で覆う
        try:
            # 受信した PSI/SI アーカイブデータを yield で返す
            trailer_size: int = 0
            while True:

                # HTTP リクエストが途中で切断された
                if await request.is_disconnected():
                    break

                # PSI/SI アーカイブデータを psisiarc から読み取る
                result = await self.__readPSIArchivedDataChunk(psisiarc_process, trailer_size)

                # LivePSIDataArchiver が破棄されたなどの理由で読み取り処理中に psisiarc が終了したか、データ構造が壊れている
                if result is None:
                    break

                # PSI/SI アーカイブデータを yield で返す
                psi_archive, trailer_size = result
                yield psi_archive
        finally:
            # cleanup 中に StreamingResponse 側のタスクがキャンセルされても、子プロセスの回収は中断させない
            ## shield() 自体は呼び出し元へ CancelledError を送出するため、cleanup 完了後に同じ例外を再送出する
            cleanup_task = asyncio.create_task(self.__cleanupProcess(psisiarc_process))
            cancellation_error: asyncio.CancelledError | None = None
            while True:
                try:
                    await asyncio.shield(cleanup_task)
                    break
                except asyncio.CancelledError as ex:
                    # cleanup task 自体がキャンセルされた場合は待ち直せないため、そのまま伝播する
                    if cleanup_task.cancelled():
                        raise
                    cancellation_error = ex
            logging.debug(f'[LivePSIDataArchiver] psisiarc terminated. (PID: {psisiarc_process.pid})')
            if cancellation_error is not None:
                raise cancellation_error


    def destroy(self) -> None:
        """
        PSI/SI データアーカイバーを破棄する
        """

        # 登録されているすべての psisiarc プロセスを終了する
        ## 基本ここに到達する前に HTTP リクエストが切断され psisiarc も終了されているはずだが、念のため
        ## タイミング次第では LivePSIDataArchiver.getPSIArchivedData() で psisiarc を終了する前に到達する可能性もある
        ## リストを先に切り離すことで二重 destroy() を no-op にし、generator 側の finally に終了待機を委ねる
        psisiarc_processes, self._psisiarc_processes = self._psisiarc_processes, []
        for psisiarc_process in psisiarc_processes:
            # generator が読み取り待機中でも EOF またはプロセス終了を検知して finally へ進めるようにする
            if psisiarc_process.stdin is not None and psisiarc_process.stdin.is_closing() is False:
                try:
                    psisiarc_process.stdin.close()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            # LiveEncodingTask の終了処理では待機できないため、ここでは即座に kill だけを送信する
            if psisiarc_process.returncode is None:
                try:
                    psisiarc_process.kill()
                except ProcessLookupError:
                    pass


    async def __cleanupProcess(self, psisiarc_process: asyncio.subprocess.Process) -> None:
        """
        psisiarc の標準入力を閉じ、自発終了または強制終了を待ってから管理リストから除外する

        Args:
            psisiarc_process (asyncio.subprocess.Process): 回収する psisiarc プロセス

        Returns:
            None
        """

        try:
            # まず標準入力を閉じて EOF を通知し、psisiarc が通常の終了処理を行えるようにする
            if psisiarc_process.stdin is not None and psisiarc_process.stdin.is_closing() is False:
                try:
                    psisiarc_process.stdin.close()
                    await asyncio.wait_for(
                        psisiarc_process.stdin.wait_closed(),
                        timeout=self.PROCESS_GRACEFUL_SHUTDOWN_TIMEOUT,
                    )
                except (BrokenPipeError, ConnectionResetError):
                    # 子プロセスが先に終了した場合に起きる正常な競合
                    pass
                except TimeoutError:
                    # pipe close の完了を待ち続けず、下のプロセス終了待機へ進む
                    pass

            # EOF で自発終了する正常経路を短時間待ち、応答しない場合だけ強制終了する
            if psisiarc_process.returncode is None:
                try:
                    await asyncio.wait_for(
                        psisiarc_process.wait(),
                        timeout=self.PROCESS_GRACEFUL_SHUTDOWN_TIMEOUT,
                    )
                except TimeoutError:
                    try:
                        psisiarc_process.kill()
                    except ProcessLookupError:
                        # タイムアウト判定と kill の間に終了した正常な競合
                        pass

            # kill 済み・自然終了済みのどちらも wait() し、子プロセスと pipe transport を完全に回収する
            await psisiarc_process.wait()
        finally:
            # destroy() が先にリストを切り離した場合もあるため、登録中の場合だけ除外する
            if psisiarc_process in self._psisiarc_processes:
                self._psisiarc_processes.remove(psisiarc_process)


    @staticmethod
    async def __readPSIArchivedDataChunk(
        psisiarc_process: asyncio.subprocess.Process,
        trailer_size: int,
    ) -> tuple[bytes, int] | None:
        """
        psisiarc からの出力ストリームから PSI/SI アーカイブデータ (.psc) を適切に読み取る
        EDCB Legacy WebUI の実装をそのまま Python に移植したもの (完全な形で移植してくれた ChatGPT (GPT-4) 先生に感謝！)
        ref: https://github.com/xtne6f/EDCB/blob/work-plus-s-230212/ini/HttpPublic/legacy/view.lua#L128-L160

        Args:
            psisiarc_process (asyncio.subprocess.Process): psisiarc のプロセス
            trailer_size (int): trailer のサイズ

        Returns:
            tuple[bytes, int] | None: アーカイブデータ、次の trailer のサイズ
        """

        def get_le_number(buffer: bytes, pos: int, len: int) -> int:
            return int.from_bytes(buffer[pos:pos+len], byteorder='little')

        assert type(psisiarc_process.stdout) is asyncio.StreamReader

        try:
            if trailer_size > 0:
                buffer = await psisiarc_process.stdout.readexactly(trailer_size)
                if len(buffer) != trailer_size:
                    return None

            buffer = await psisiarc_process.stdout.readexactly(32)
            if len(buffer) != 32:
                return None

            time_list_len = get_le_number(buffer, 10, 2)
            dictionary_len = get_le_number(buffer, 12, 2)
            dictionary_data_size = get_le_number(buffer, 16, 4)
            code_list_len = get_le_number(buffer, 24, 4)
            payload = b''
            payload_size = time_list_len * 4 + dictionary_len * 2 + ((dictionary_data_size + 1) // 2) * 2 + code_list_len * 2

            if payload_size > 0:
                payload = await psisiarc_process.stdout.readexactly(payload_size)
                if len(payload) != payload_size:
                    return None

            buffer = b'=' * trailer_size + buffer + payload
            return (buffer, 2 + (2 + len(payload)) % 4)

        # 読み取り処理中に psisiarc が終了した
        except asyncio.IncompleteReadError:
            return None
