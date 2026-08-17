from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import ClassVar

import aiohttp

from app import logging
from app.utils.KonomiTVBS4KTLVRainFallbackMonitor import (
    KonomiTVBS4KTLVRainFallbackMonitor,
)


class KonomiTVBS4KTLVStreamPump:
    """TLV の HTTP 入力を単一タスクで読み、エンコーダー起動までの有限バッファを管理する。"""

    # Metadata helper / Resolver と同じ単位で読み、起動時バッファの個数上限をバイト上限へ対応させる。
    CHUNK_SIZE: ClassVar[int] = 64 * 1024
    # 約 100Mbps の TLV で約 1.3 秒分を保持する。古い開始位置による再生遅延を増やさず、
    # FFmpeg サブプロセスの生成中も Mirakurun から読み続けるための最新 16MiB の窓になる。
    MAX_BUFFERED_CHUNKS: ClassVar[int] = 256

    def __init__(
        self,
        stream_reader: aiohttp.StreamReader,
        initial_data: bytes,
        log_prefix: str,
        rain_fallback_monitor: KonomiTVBS4KTLVRainFallbackMonitor | None = None,
    ) -> None:
        """
        TLV stream pump を初期化する。

        Args:
            stream_reader (aiohttp.StreamReader): Channel Stream API の HTTP 応答本文。
            initial_data (bytes): context_id のプローブ中に読み取った生 TLV データ。
            log_prefix (str): ログへ付与するプレフィックス。
            rain_fallback_monitor (KonomiTVBS4KTLVRainFallbackMonitor | None): 生TLVを複製する継続監視。

        Returns:
            None
        """

        # HTTP 応答本文の唯一の読者。start() 後は LiveEncodingTask から直接読まない。
        self._stream_reader = stream_reader
        # 起動中の最新窓と、起動後の lossless 入力を同じ順序で Reader へ渡す有限 Queue。
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self.MAX_BUFFERED_CHUNKS)
        # FFmpeg 起動後は古いチャンク破棄を止める一方向のモード切替。
        self._lossless_mode = asyncio.Event()
        # pump タスクを明示的に回収するために保持する参照。start() までは None。
        self._task: asyncio.Task[None] | None = None
        # HTTP 入力例外を診断用に保持する。Reader 側では EOF として扱い、既存の再起動判定へ委ねる。
        self._error: BaseException | None = None
        # 入力終了の debug ログに利用するストリーム固有プレフィックス。
        self._log_prefix = log_prefix
        # 監視遅延をprimary Queueへ波及させず、生TLVを複製する降雨対応放送monitor。
        self._rain_fallback_monitor = rain_fallback_monitor

        # Resolver が読み取ったデータも64KiB以下へ分割し、後続データと同じ上限・順序で管理する。
        # monitor は Resolver の確定値を初期状態として引き継ぐため、最大32MiBの先頭バッファを
        # 8MiBの監視Queueへ再投入しない。再投入すると選局のたびにoverflowして確定値を失う。
        for offset in range(0, len(initial_data), self.CHUNK_SIZE):
            chunk = initial_data[offset:offset + self.CHUNK_SIZE]
            self._putStartupChunk(chunk)

    @property
    def error(self) -> BaseException | None:
        """
        HTTP 入力タスクで発生した例外を取得する。

        Args:
            なし。

        Returns:
            BaseException | None: 入力例外。正常終了・キャンセル・実行中なら None。
        """

        return self._error

    def start(self) -> None:
        """
        HTTP 応答本文の読み取りを開始する。

        Args:
            なし。

        Returns:
            None
        """

        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    def switchToLosslessMode(self) -> None:
        """
        起動時の古いチャンク破棄を止め、Queue のバックプレッシャーを有効にする。

        Args:
            なし。

        Returns:
            None
        """

        self._lossless_mode.set()

    async def iterChunks(self) -> AsyncIterator[bytes]:
        """
        保持中の TLV チャンクを受信順に返す。

        Args:
            なし。

        Returns:
            AsyncIterator[bytes]: 生 TLV チャンクの非同期イテレーター。
        """

        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            yield chunk

    def cancel(self) -> None:
        """
        実行中の HTTP 読み取りタスクへキャンセルを送る。

        Args:
            なし。

        Returns:
            None
        """

        if self._task is not None and self._task.done() is False:
            self._task.cancel()

    async def wait(self) -> None:
        """
        HTTP 読み取りタスクの終了を待つ。

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
            # pump 自身へ送ったキャンセルは正常な回収だが、選局元タスクのキャンセルは握りつぶさない。
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling() > 0:
                raise

    def _putStartupChunk(self, chunk: bytes) -> None:
        """
        起動時バッファへチャンクを追加し、上限超過時は最古チャンクを破棄する。

        Args:
            chunk (bytes): 追加する生 TLV チャンク。

        Returns:
            None
        """

        if chunk == b'':
            return
        if self._queue.full() is True:
            self._queue.get_nowait()
        self._queue.put_nowait(chunk)

    async def _run(self) -> None:
        """
        HTTP 応答を読み、現在のモードに応じて有限 Queue へ追加する。

        Args:
            なし。

        Returns:
            None
        """

        was_cancelled = False
        try:
            async for chunk in self._stream_reader.iter_chunked(self.CHUNK_SIZE):
                # metadata監視は有限Queueへ非blockingで複製し、遅延・再同期をエンコーダー入力から分離する。
                if self._rain_fallback_monitor is not None:
                    self._rain_fallback_monitor.offerChunk(chunk)
                # startup mode の Queue 操作間には await がないため、lossless 切替との順序が曖昧にならない。
                if self._lossless_mode.is_set() is False:
                    self._putStartupChunk(chunk)
                    continue
                # FFmpeg 起動後は一切破棄せず、Queue 満杯時は HTTP 読み取りへバックプレッシャーを返す。
                await self._queue.put(chunk)
        except asyncio.CancelledError:
            was_cancelled = True
            raise
        except (aiohttp.ClientError, OSError, TimeoutError) as ex:
            self._error = ex
            logging.debug(f'{self._log_prefix} MMT/TLV input pump stopped:', exc_info=ex)
        finally:
            if self._rain_fallback_monitor is not None:
                self._rain_fallback_monitor.finish()
            # startup 中またはキャンセル時は consumer がいない可能性があるため、終了通知でブロックしない。
            if self._lossless_mode.is_set() is False or was_cancelled is True:
                if self._queue.full() is True:
                    self._queue.get_nowait()
                self._queue.put_nowait(None)
            else:
                await self._queue.put(None)
