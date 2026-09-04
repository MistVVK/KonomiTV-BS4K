"""KonomiTV-BS4K の probe 系 subprocess の同時実行数を全 probe 系で共有する。

録画再生能力 probe・ライブ TS 搬送 probe・コーデック対応のサーバー診断は、
いずれも FFmpeg などの外部プロセスを起動する。
個別に limiter を持たせると複数 probe 系が並行した時点で外部プロセス総数が増えるため、
全 probe 系で一つの上限を共有する。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import ClassVar


class KonomiTVBS4KExternalProcessLimiter:
    """KonomiTV-BS4K の probe 系 subprocess の同時実行数を上限する。"""

    MAX_CONCURRENT_PROCESSES: ClassVar[int] = 2

    # semaphore は初めて await された event loop へ束ねられるため、
    # loop が変わったら (テストの asyncio.run ごとに) 作り直す
    _event_loop: ClassVar[asyncio.AbstractEventLoop | None] = None
    _semaphore: ClassVar[asyncio.Semaphore | None] = None

    @classmethod
    def getSemaphore(cls) -> asyncio.Semaphore:
        """現在実行中の event loop 向けの共有 semaphore を返す。

        Args:
            None

        Returns:
            共有 semaphore。event loop が変わった場合は新しいものへ作り直す。
        """

        event_loop = asyncio.get_running_loop()
        if cls._event_loop is not event_loop or cls._semaphore is None:
            cls._event_loop = event_loop
            cls._semaphore = asyncio.Semaphore(cls.MAX_CONCURRENT_PROCESSES)
        return cls._semaphore

    @classmethod
    @asynccontextmanager
    async def acquireSlot(cls) -> AsyncGenerator[None]:
        """外部プロセス同時実行枠を1つ取得して返す。

        Args:
            None

        Returns:
            枠を取得してから yield し、exit で必ず解放する context manager。
        """

        async with cls.getSemaphore():
            yield
