from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterable
from typing import Any, cast

from app import logging
from app.constants import LIBRARY_PATH


class KonomiTVBS4KTLVServiceResolver:
    """生 TLV の先頭バッファをプローブし、service_id → context_id を解決する。

    HTTP 接続は持たず、呼び出し元から受け取った AsyncIterable[bytes] を読み込む。
    メタデータツール (KonomiTVBS4KTLVMetadata.elf) のストリーミング JSON 行を監視し、
    目的の service_id と映像トラックが得られた時点で早期終了する。単体テストが可能な純粋な解析器。
    """

    # プローブで読み取る生バイトの上限 (32 MiB)。8K でも SDT を確実に含みつつ、無制限読みを防ぐ。
    MAX_PROBE_BYTES: int = 32 * 1024 * 1024

    # HTTP response header の受信後、最初の TLV バイトが到着するまで待つ予算 (秒)。
    # Mirakurun のチューナー起動後に本体が遅れて届く場合も、従来経路と同じ 40 秒までは待機する。
    FIRST_BYTE_TIMEOUT_SECONDS: float = 40.0

    # first-byte 到着後、主 SID の context_id を解決するまで待つ解析予算 (秒)。
    PROBE_TIMEOUT_SECONDS: float = 3.0

    # 主 SID 解決後に降雨対応 SID と Video トラックを待つ追加時間 (秒)。
    # 実入力では主 Video が first-byte から最大約 1.6 秒後に現れたため、BS8K の低階層にも 2 秒を確保する。
    RAIN_FALLBACK_WAIT_SECONDS: float = 2.0

    @staticmethod
    def parseMetadataLine(line: str) -> tuple[dict[int, int], set[int]] | None:
        """
        メタデータツールが出力した 1 行からサービスと映像トラックの context_id を抽出する。

        Args:
            line (str): ストリーミング出力された JSON 行。

        Returns:
            tuple[dict[int, int], set[int]] | None:
                service_id → context_id と、映像トラックを持つ context_id の集合。解析不能なら None。
        """

        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        services = payload.get('services')
        tracks = payload.get('tracks')
        if not isinstance(services, list) or not isinstance(tracks, list):
            return None

        service_contexts: dict[int, int] = {}
        for item in services:
            if not isinstance(item, dict):
                continue
            service_id = item.get('service_id')
            context_id = item.get('context_id')
            # bool は int のサブクラスのため、誤って数値扱いしないよう先に除外する。
            if isinstance(service_id, bool) or isinstance(context_id, bool):
                continue
            try:
                sid = int(cast(Any, service_id))
                context = int(cast(Any, context_id))
            except (TypeError, ValueError):
                continue
            service_contexts[sid] = context

        video_context_ids: set[int] = set()
        for item in tracks:
            if not isinstance(item, dict) or item.get('kind') != 'Video':
                continue
            context_id = item.get('context_id')
            # bool は int のサブクラスのため、誤って数値扱いしないよう先に除外する。
            if isinstance(context_id, bool):
                continue
            try:
                video_context_ids.add(int(cast(Any, context_id)))
            except (TypeError, ValueError):
                continue

        return (service_contexts, video_context_ids)

    @classmethod
    async def resolve(
        cls,
        stream: AsyncIterable[bytes],
        main_service_id: int,
        rain_service_id: int | None,
        *,
        need_rain_fallback: bool,
        log_prefix: str,
    ) -> tuple[int | None, int | None, bytes]:
        """
        生 TLV ストリームの先頭を読み、主 SID / 降雨対応 SID の context_id を解決する。

        Args:
            stream (AsyncIterable[bytes]): 生 TLV のバイト列イテレータ (HTTP は持たない)。
            main_service_id (int): 主サービス ID (channel.service_id)。
            rain_service_id (int | None): 対象局だけに設定する降雨対応サービス ID。
            need_rain_fallback (bool): 降雨対応 context_id まで待つか (画質 1080p 以下で True)。
            log_prefix (str): ログへ付与するプレフィックス。

        Returns:
            tuple[int | None, int | None, bytes]:
                (主 context_id, 降雨対応 context_id, 読み取った生バイト)。
                context_id が未観測の場合は None、生バイトは先頭付加用の raw データ (同期前)。
        """

        process = await asyncio.subprocess.create_subprocess_exec(
            LIBRARY_PATH['KonomiTVBS4KTLVMetadata'],
            '-',
            str(cls.MAX_PROBE_BYTES),
            stdin = asyncio.subprocess.PIPE,
            stdout = asyncio.subprocess.PIPE,
            stderr = asyncio.subprocess.DEVNULL,
        )
        stdin = process.stdin
        stdout = process.stdout
        if stdin is None or stdout is None:
            await cls.__terminate(process, log_prefix)
            return (None, None, b'')

        head_buffer = bytearray()
        observed_service_contexts: dict[int, int] = {}
        main_context_id: int | None = None
        rain_context_id: int | None = None

        # stdout 読取は独立タスクにし、stdin 書込と並行させる。
        # 先に全バイトを書いてから読むと、子プロセスの stdout pipe が詰まってデッドロックする。
        async def readMetadata() -> None:
            nonlocal main_context_id, rain_context_id
            while True:
                line = await stdout.readline()
                if line == b'':
                    return
                metadata = cls.parseMetadataLine(line.decode('utf-8', errors='replace'))
                if metadata is None:
                    continue
                service_contexts, video_context_ids = metadata

                # 実入力では同じ context_id の SDT が SID ごとに順次通知されるため、各行だけを見ると
                # 直前に解決した主 SID が消える。プローブ期間内に観測したサービス対応は SID ごとに蓄積する。
                observed_service_contexts.update(service_contexts)
                if main_context_id is None:
                    main_context_id = observed_service_contexts.get(main_service_id)
                if need_rain_fallback is True and rain_service_id is not None and rain_context_id is None:
                    candidate_context_id = observed_service_contexts.get(rain_service_id)
                    # SDT に SID があるだけでは FFmpeg の必須 map を満たせない。
                    # 現在の snapshot で同じ context に映像トラックが存在する場合だけ低階層を採用する。
                    if candidate_context_id in video_context_ids:
                        rain_context_id = candidate_context_id

        reader_task = asyncio.create_task(readMetadata())
        loop = asyncio.get_running_loop()
        first_byte_deadline = loop.time() + cls.FIRST_BYTE_TIMEOUT_SECONDS
        main_deadline: float | None = None
        rain_deadline: float | None = None

        def isResolved() -> bool:
            # 主 SID は必須。降雨対応 SID は need_rain_fallback のときだけ待つ。
            if main_context_id is None:
                return False
            return (need_rain_fallback is False) or (rain_context_id is not None)

        iterator = stream.__aiter__()
        try:
            while True:
                if isResolved() is True:
                    break
                if len(head_buffer) >= cls.MAX_PROBE_BYTES:
                    break
                # 主 SID が解決済みなら、降雨対応 SID は短い追加窓だけ待つ。
                # 降雨の無い通常運用では毎回フル待機して選局を遅らせない。
                if need_rain_fallback is True and main_context_id is not None and rain_deadline is None:
                    rain_deadline = loop.time() + cls.RAIN_FALLBACK_WAIT_SECONDS
                deadline = rain_deadline or main_deadline or first_byte_deadline
                # first-byte までは従来と同じ 40 秒、その後の metadata 解析は 3 秒に分離する。
                # チャンク到着後ではなく読み待ち自体を制限し、各予算を超えた場合は確実に打ち切る。
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    chunk = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
                except (TimeoutError, StopAsyncIteration):
                    break
                # first-byte の待機時間を metadata 解析予算へ含めず、最初の入力を受け取った時点から計測する。
                if main_deadline is None:
                    main_deadline = loop.time() + cls.PROBE_TIMEOUT_SECONDS
                head_buffer.extend(chunk)
                stdin.write(chunk)
                await stdin.drain()
                # pipe に余裕がある drain() は即時完了し得るため、stdout reader へ明示的に実行機会を渡す。
                # これがないと解決済みでも入力を読み進め、最大 32MiB の不要な先頭遅延を作り得る。
                await asyncio.sleep(0)
        finally:
            # 早期終了・失敗・キャンセルのいずれでも metadata 子プロセスを必ず終了する。
            await cls.__terminate(process, log_prefix, stdin, reader_task)

        return (main_context_id, rain_context_id, bytes(head_buffer))

    @staticmethod
    async def __terminate(
        process: asyncio.subprocess.Process,
        log_prefix: str,
        stdin: asyncio.StreamWriter | None = None,
        reader_task: asyncio.Task[None] | None = None,
    ) -> None:
        """
        metadata 子プロセスと付随する入出力を回収する。

        Args:
            process (asyncio.subprocess.Process): 終了させるメタデータツール。
            log_prefix (str): ログへ付与するプレフィックス。
            stdin (asyncio.StreamWriter | None): 未クローズなら閉じる標準入力。
            reader_task (asyncio.Task[None] | None): 未完了ならキャンセルする stdout 読取タスク。
        """

        if stdin is not None:
            try:
                stdin.close()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        # 子プロセスは必ず kill + wait で回収する。後段の reader_task 待機で親タスクのキャンセルを
        # 再送出する経路があっても、ここまで完了していれば metadata 子プロセスが残留しない。
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            # 親タスクのキャンセルで回収 (wait) を中断させないよう shield する。
            # shield されていれば、親がキャンセルされても子プロセスの回収はバックグラウンドで継続する。
            await asyncio.shield(process.wait())
        except asyncio.CancelledError:
            # shield により wait 自体は保護されるが、親タスクに pending なキャンセルが残っていれば
            # ここで再送出して選局キャンセルを握りつぶさない。後段の reader_task 待機へは到達しない。
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling() > 0:
                raise
        except (ProcessLookupError, OSError) as ex:
            logging.debug(f'{log_prefix} Failed to wait for MMT/TLV metadata probe:', exc_info=ex)
        if reader_task is not None and reader_task.done() is False:
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                # reader_task のキャンセルと親タスク (選局切替) のキャンセルは区別できないため、
                # 親タスクに pending なキャンセルが残っていれば再送出し、選局キャンセルを握りつぶさない。
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling() > 0:
                    raise
