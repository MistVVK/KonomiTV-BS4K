from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterable
from dataclasses import dataclass
from typing import Any, Literal, cast

from app import logging
from app.constants import LIBRARY_PATH


@dataclass(frozen=True)
class KonomiTVBS4KTLVTrackSnapshot:
    """TLV metadata helper が通知した1トラックの識別情報。"""

    track_id: int
    context_id: int
    packet_id: int
    component_tag: int | None
    kind: Literal['Video', 'Audio', 'Subtitle']


@dataclass(frozen=True)
class KonomiTVBS4KTLVMetadataSnapshot:
    """TLV metadata helper が通知したサービス・トラックのスナップショット。"""

    snapshot_type: Literal['MPT', 'MHSDT'] | None
    snapshot_context_id: int | None
    service_contexts: dict[int, int]
    tracks: tuple[KonomiTVBS4KTLVTrackSnapshot, ...]


@dataclass(frozen=True)
class KonomiTVBS4KTLVServiceResolution:
    """起動時プローブで解決したTLVサービス情報と先頭バッファ。"""

    main_context_id: int | None
    rain_context_id: int | None
    main_video_packet_id: int | None
    main_audio_packet_id: int | None
    rain_video_packet_id: int | None
    is_rain_fallback_broadcasting: bool | None
    head_buffer: bytes


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
    def parseMetadataLine(line: str) -> KonomiTVBS4KTLVMetadataSnapshot | None:
        """
        メタデータツールが出力した 1 行からサービスとトラックの識別情報を抽出する。

        Args:
            line (str): ストリーミング出力された JSON 行。

        Returns:
            KonomiTVBS4KTLVMetadataSnapshot | None:
                サービス・トラックと完全 snapshot の識別情報。解析不能なら None。
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

        snapshot_type_raw = payload.get('snapshot_type')
        snapshot_type: Literal['MPT', 'MHSDT'] | None = None
        if snapshot_type_raw == 'MPT' or snapshot_type_raw == 'MHSDT':
            snapshot_type = snapshot_type_raw

        snapshot_context_id_raw = payload.get('snapshot_context_id')
        snapshot_context_id: int | None = None
        if not isinstance(snapshot_context_id_raw, bool):
            try:
                snapshot_context_id = int(cast(Any, snapshot_context_id_raw))
            except (TypeError, ValueError):
                pass

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

        track_snapshots: list[KonomiTVBS4KTLVTrackSnapshot] = []
        for item in tracks:
            if not isinstance(item, dict):
                continue
            kind_raw = item.get('kind')
            if kind_raw not in ('Video', 'Audio', 'Subtitle'):
                continue
            track_id = item.get('track_id')
            context_id = item.get('context_id')
            packet_id = item.get('packet_id')
            # bool は int のサブクラスのため、誤って数値扱いしないよう先に除外する。
            if (
                isinstance(track_id, bool) or
                isinstance(context_id, bool) or
                isinstance(packet_id, bool)
            ):
                continue
            try:
                parsed_track_id = int(cast(Any, track_id))
                parsed_context_id = int(cast(Any, context_id))
                parsed_packet_id = int(cast(Any, packet_id))
            except (TypeError, ValueError):
                continue
            component_tag_raw = item.get('component_tag')
            component_tag: int | None = None
            if not isinstance(component_tag_raw, bool):
                try:
                    component_tag = int(cast(Any, component_tag_raw))
                except (TypeError, ValueError):
                    pass
            track_snapshots.append(KonomiTVBS4KTLVTrackSnapshot(
                track_id=parsed_track_id,
                context_id=parsed_context_id,
                packet_id=parsed_packet_id,
                component_tag=component_tag,
                kind=kind_raw,
            ))

        return KonomiTVBS4KTLVMetadataSnapshot(
            snapshot_type=snapshot_type,
            snapshot_context_id=snapshot_context_id,
            service_contexts=service_contexts,
            tracks=tuple(track_snapshots),
        )

    @staticmethod
    def selectTrackPacketIds(
        tracks: tuple[KonomiTVBS4KTLVTrackSnapshot, ...],
        main_context_id: int | None,
        rain_context_id: int | None,
    ) -> tuple[int | None, int | None, int | None]:
        """
        MPT のトラック一覧から FFmpeg へ渡す主/降雨対応トラックの packet_id を選択する。

        context_id は TLV 上の文脈識別子であり、主サービスと降雨対応の低階層が同じ context に
        多重化される実放送がある。FFmpeg の map を一意にするため、component_tag と track_id の順で
        主トラックを決め、降雨対応は主トラックと異なる Video packet_id を選ぶ。

        Args:
            tracks (tuple[KonomiTVBS4KTLVTrackSnapshot, ...]): 最新 snapshot の全トラック。
            main_context_id (int | None): 主サービスの context_id。
            rain_context_id (int | None): 降雨対応サービスの context_id。

        Returns:
            tuple[int | None, int | None, int | None]:
                (主 Video packet_id, 主 Audio packet_id, 降雨対応 Video packet_id)。
        """

        if main_context_id is None:
            return (None, None, None)

        def trackSortKey(track: KonomiTVBS4KTLVTrackSnapshot) -> tuple[int, int]:
            # ISDB-S3 の主トラックは低い component_tag が割り当てられる。タグが無い入力では
            # libaribtlv が MPT から採番する安定 track_id の順をそのまま使う。
            return (
                track.component_tag if track.component_tag is not None else 0xFFFF,
                track.track_id,
            )

        main_video_tracks = [
            track for track in tracks
            if track.context_id == main_context_id and track.kind == 'Video'
        ]
        main_audio_tracks = [
            track for track in tracks
            if track.context_id == main_context_id and track.kind == 'Audio'
        ]
        main_video_track = min(main_video_tracks, key=trackSortKey, default=None)
        main_audio_track = min(main_audio_tracks, key=trackSortKey, default=None)

        rain_video_track: KonomiTVBS4KTLVTrackSnapshot | None = None
        if rain_context_id is not None:
            rain_video_tracks = [
                track for track in tracks
                if track.context_id == rain_context_id and track.kind == 'Video'
            ]
            if rain_context_id == main_context_id and main_video_track is not None:
                # NHK BSP4K / BS8K では主・低階層が同じ context_id に並ぶため、主映像と
                # 異なる packet_id を低階層として選択する。component_tag も異なる候補を優先する。
                distinct_tracks = [
                    track for track in rain_video_tracks
                    if track.packet_id != main_video_track.packet_id
                ]
                distinct_component_tracks = [
                    track for track in distinct_tracks
                    if track.component_tag is not None and
                    track.component_tag != main_video_track.component_tag
                ]
                rain_video_tracks = distinct_component_tracks or distinct_tracks
            rain_video_track = min(rain_video_tracks, key=trackSortKey, default=None)

        return (
            main_video_track.packet_id if main_video_track is not None else None,
            main_audio_track.packet_id if main_audio_track is not None else None,
            rain_video_track.packet_id if rain_video_track is not None else None,
        )


    @classmethod
    async def resolve(
        cls,
        stream: AsyncIterable[bytes],
        main_service_id: int,
        rain_service_id: int | None,
        *,
        need_rain_fallback: bool,
        log_prefix: str,
    ) -> KonomiTVBS4KTLVServiceResolution:
        """
        生 TLV ストリームの先頭を読み、主 SID / 降雨対応 SID の context_id と packet_id を解決する。

        Args:
            stream (AsyncIterable[bytes]): 生 TLV のバイト列イテレータ (HTTP は持たない)。
            main_service_id (int): 主サービス ID (channel.service_id)。
            rain_service_id (int | None): 対象局だけに設定する降雨対応サービス ID。
            need_rain_fallback (bool): 降雨対応トラックまで待つか (画質 1080p 以下で True)。
            log_prefix (str): ログへ付与するプレフィックス。

        Returns:
            KonomiTVBS4KTLVServiceResolution:
                主・降雨対応 context、FFmpeg map 用の packet_id、降雨対応 Video の送出状態、
                先頭付加用の raw データ。
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
            return KonomiTVBS4KTLVServiceResolution(None, None, None, None, None, None, b'')

        head_buffer = bytearray()
        observed_service_contexts: dict[int, int] = {}
        received_mpt_contexts: set[int] = set()
        latest_tracks: tuple[KonomiTVBS4KTLVTrackSnapshot, ...] = ()
        main_context_id: int | None = None
        rain_context_id: int | None = None
        main_video_packet_id: int | None = None
        main_audio_packet_id: int | None = None
        rain_video_packet_id: int | None = None
        is_rain_fallback_broadcasting: bool | None = None

        # stdout 読取は独立タスクにし、stdin 書込と並行させる。
        # 先に全バイトを書いてから読むと、子プロセスの stdout pipe が詰まってデッドロックする。
        async def readMetadata() -> None:
            nonlocal latest_tracks
            nonlocal main_context_id, rain_context_id
            nonlocal main_video_packet_id, main_audio_packet_id, rain_video_packet_id
            nonlocal is_rain_fallback_broadcasting
            while True:
                line = await stdout.readline()
                if line == b'':
                    return
                metadata = cls.parseMetadataLine(line.decode('utf-8', errors='replace'))
                if metadata is None:
                    continue

                # 実入力では同じ context_id の SDT が SID ごとに順次通知されるため、各行だけを見ると
                # 直前に解決した主 SID が消える。プローブ期間内に観測したサービス対応は SID ごとに蓄積する。
                observed_service_contexts.update(metadata.service_contexts)
                # helper の tracks は SDT 行でも現在保持している全件を含む。MPT の完全 snapshot を
                # 待つ送出判定とは分け、FFmpeg map 用の候補だけは最新行へ追従する。
                latest_tracks = metadata.tracks
                if metadata.snapshot_type == 'MPT':
                    # context未選択の空MPTはhelper全体のreset通知なので、再通知前のSID・MPT・trackを残さない。
                    # context IDが再利用されても、reset前のVideo送出状態を新しいサービスへ誤適用させない。
                    if metadata.snapshot_context_id is None:
                        observed_service_contexts.clear()
                        received_mpt_contexts.clear()
                        latest_tracks = ()
                    else:
                        # MPTのtracksは完全snapshotとして置換する。受信済みcontextは別に保持することで、
                        # tracks=[]を「MPT未受信」と混同せずVideoなしと確定できる。
                        received_mpt_contexts.add(metadata.snapshot_context_id)

                main_context_id = observed_service_contexts.get(main_service_id)
                if rain_service_id is not None:
                    rain_context_id = observed_service_contexts.get(rain_service_id)
                (
                    main_video_packet_id,
                    main_audio_packet_id,
                    rain_video_packet_id,
                ) = cls.selectTrackPacketIds(latest_tracks, main_context_id, rain_context_id)
                if rain_service_id is not None:
                    # MPT は対象 context の完全なトラック一覧なので、観測済みなら Video の有無を確定できる。
                    # SDT だけを見て低階層を採用すると FFmpeg の必須 map を満たせないため、未観測は None のままにする。
                    if rain_context_id is not None and rain_context_id in received_mpt_contexts:
                        is_rain_fallback_broadcasting = rain_video_packet_id is not None
                    else:
                        is_rain_fallback_broadcasting = None

        reader_task = asyncio.create_task(readMetadata())
        loop = asyncio.get_running_loop()
        first_byte_deadline = loop.time() + cls.FIRST_BYTE_TIMEOUT_SECONDS
        main_deadline: float | None = None
        rain_deadline: float | None = None

        def isResolved() -> bool:
            # 主 SID と FFmpeg へ一意指定する主 Video / Audio packet_id は必須。
            # 降雨対応 SID は need_rain_fallback のときだけ待つ。
            if main_context_id is None or main_video_packet_id is None or main_audio_packet_id is None:
                return False
            return (need_rain_fallback is False) or (is_rain_fallback_broadcasting is not None)

        iterator = stream.__aiter__()
        try:
            while True:
                if isResolved() is True:
                    break
                if len(head_buffer) >= cls.MAX_PROBE_BYTES:
                    break
                # 主トラックが解決済みなら、降雨対応 SID は短い追加窓だけ待つ。
                # 降雨の無い通常運用では毎回フル待機して選局を遅らせない。
                if (
                    need_rain_fallback is True and
                    main_video_packet_id is not None and
                    main_audio_packet_id is not None and
                    rain_deadline is None
                ):
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

        return KonomiTVBS4KTLVServiceResolution(
            main_context_id=main_context_id,
            rain_context_id=rain_context_id,
            main_video_packet_id=main_video_packet_id,
            main_audio_packet_id=main_audio_packet_id,
            rain_video_packet_id=rain_video_packet_id,
            is_rain_fallback_broadcasting=is_rain_fallback_broadcasting,
            head_buffer=bytes(head_buffer),
        )

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
