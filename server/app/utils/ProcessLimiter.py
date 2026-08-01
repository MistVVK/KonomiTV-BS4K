
import asyncio
from typing import ClassVar

import psutil


class ProcessLimiter:
    """
    外部プロセスの同時実行数を CPU コア数の 50% に制限するためのユーティリティクラス
    """

    # クラス変数として Semaphore の辞書を保持
    # key: プロセスを識別するキー
    # value: そのプロセス用の Semaphore
    _semaphores: ClassVar[dict[str, asyncio.Semaphore]] = {}


    @classmethod
    def getSemaphore(cls, process_key: str) -> asyncio.Semaphore:
        """
        指定されたプロセス用の Semaphore を取得する
        初回呼び出し時に CPU 論理コア数の 50% の Semaphore を作成する

        Args:
            process_key (str): プロセスを識別するキー

        Returns:
            asyncio.Semaphore: 指定されたプロセス用の Semaphore
        """

        if process_key not in cls._semaphores:
            # CPU 論理コア数を取得
            # 取得不能時は最小構成の1コアとして扱い、Semaphore(0) による永久待機を防ぐ
            cpu_count = psutil.cpu_count(logical=True) or 1
            # 同時実行数を CPU コア数の 50% に制限しつつ、最低1プロセスは進行可能にする
            cls._semaphores[process_key] = asyncio.Semaphore(max(1, cpu_count // 2))
        return cls._semaphores[process_key]
