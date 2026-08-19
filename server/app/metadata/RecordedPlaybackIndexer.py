from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal, cast

from app import logging, schemas
from app.config import Config
from app.constants import JST, LIBRARY_PATH
from app.metadata.AnalysisTaskTracker import AnalysisTaskHandle, AnalysisTaskTracker
from app.metadata.RecordedPlaybackIndex import (
    RECORDED_PLAYBACK_INDEX_VERSION,
    IsRecordedPlaybackIndexReady,
    RecordedPlaybackIndexStage,
    RecordedPlaybackIndexState,
)
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedVideo import RecordedVideo
from app.schemas import (
    AudioTrack,
    AudioTrackTimelineEntry,
    AudioTrackTimelineTrack,
    SubtitleTrack,
    VideoStreamTimelineEntry,
)
from app.utils.KonomiTVBS4KMMTTLV import (
    MMT_TLV_CONTAINER_FORMAT,
    MMT_TLV_FILE_EXTENSIONS,
    BuildKonomiTVBS4KMMTTLVInputArguments,
)
from app.utils.TSKeyFrameSeeker import TSKeyFrameSeeker


class RecordedPlaybackIndexAnalysisError(Exception):
    """索引をReadyにせず終了すべき、機械判定可能な解析失敗。"""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


# FFprobe compact writer が使う C 風 escape (\\| / \\\\ / \\n / \\r / \\t など)
_FFPROBE_COMPACT_ESCAPES: dict[str, str] = {
    'n': '\n',
    'r': '\r',
    't': '\t',
    '\\': '\\',
    '|': '|',
    "'": "'",
    '"': '"',
    'b': '\b',
    'f': '\f',
}


def splitFFprobeCompactFields(line: str) -> list[str]:
    """
    FFprobe compact writer の escape を解釈して field へ分割する。

    compact 形式では `|` と `\\` が `\\|` / `\\\\` として escape され、
    改行は `\\n` / `\\r` として埋め込まれる。単純な split('|') では壊れる。

    Args:
        line (str): compact 1 行 (または side_data セクション)

    Returns:
        list[str]: unescape 済みの field 一覧
    """

    fields: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(line):
        char = line[index]
        if char == '\\' and index + 1 < len(line):
            next_char = line[index + 1]
            current.append(_FFPROBE_COMPACT_ESCAPES.get(next_char, next_char))
            index += 2
            continue
        if char == '|':
            fields.append(''.join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    fields.append(''.join(current))
    return fields


def parseFFprobeCompactKeyValues(line: str) -> dict[str, str]:
    """
    compact 行を key=value 辞書へ変換する。

    Args:
        line (str): compact 形式の 1 セクション

    Returns:
        dict[str, str]: key と unescape 済み value
    """

    values: dict[str, str] = {}
    for item in splitFFprobeCompactFields(line):
        if '=' not in item:
            continue
        key, value = item.split('=', maxsplit=1)
        values[key] = value
    return values


def _getAudioChannelLabel(channels: int, channel_layout: str | None) -> str:
    """実チャンネル数とFFprobeのlayoutから画面表示用ラベルを返す。"""

    normalized_layout = (channel_layout or '').lower()
    if normalized_layout == 'mono' or channels == 1:
        return 'Monaural'
    if normalized_layout == 'stereo' or channels == 2:
        return 'Stereo'
    if normalized_layout.startswith('5.1'):
        return '5.1ch'
    return f'{channels} Channels'


def _normalizeAspectRatio(value: object) -> str | None:
    """FFprobeの比率表記を既約な ``横:縦`` へ正規化する。"""

    normalized = str(value or '').replace('/', ':')
    try:
        numerator_text, denominator_text = normalized.split(':', maxsplit=1)
        numerator = int(numerator_text)
        denominator = int(denominator_text)
    except (ValueError, TypeError):
        return None
    if numerator <= 0 or denominator <= 0:
        return None
    divisor = math.gcd(numerator, denominator)
    return f'{numerator // divisor}:{denominator // divisor}'


def _calculateDisplayAspectRatio(width: int, height: int, sample_aspect_ratio: str | None) -> str | None:
    """解像度とSARからDARを算出する。"""

    normalized_sar = _normalizeAspectRatio(sample_aspect_ratio)
    if width <= 0 or height <= 0 or normalized_sar is None:
        return None
    sar_width, sar_height = (int(value) for value in normalized_sar.split(':'))
    divisor = math.gcd(width * sar_width, height * sar_height)
    return f'{width * sar_width // divisor}:{height * sar_height // divisor}'


class _TSAudioPIDCollector:
    """FFprobeへTSを供給する同じ走査で、遅延追加音声PIDの範囲とADTS構成を収集する。"""

    _SAMPLING_RATES = (96_000, 88_200, 64_000, 48_000, 44_100, 32_000, 24_000, 22_050,
                       16_000, 12_000, 11_025, 8_000, 7_350)
    _CHANNELS = {
        1: ('Monaural', 'mono'),
        2: ('Stereo', 'stereo'),
        3: ('3 Channels', '3.0'),
        4: ('4 Channels', '4.0'),
        5: ('5 Channels', '5.0'),
        6: ('5.1ch', '5.1'),
        7: ('8 Channels', '7.1'),
    }

    def __init__(
        self,
        duration: float,
        source_start_time: float,
        tracks_by_stream_index: dict[int, AudioTrack],
        target_stream_indexes: set[int],
    ) -> None:
        self.duration = duration
        self.source_start_time = source_start_time
        self.targets_by_pid: dict[int, tuple[int, AudioTrack]] = {}
        for stream_index in target_stream_indexes:
            track = tracks_by_stream_index.get(stream_index)
            pid = track.get('pid') if track is not None else None
            if track is not None and pid is not None:
                self.targets_by_pid[int(pid)] = (stream_index, track)
        self.states: dict[int, tuple[float, float, float, AudioTrackTimelineTrack]] = {}
        self.ranges: list[tuple[float, float, AudioTrackTimelineTrack]] = []
        self.unwrap_targets = {
            pid: int(source_start_time * 90_000)
            for pid in self.targets_by_pid
        }

    @staticmethod
    def _payload(packet: bytes) -> bytes:
        adaptation_field_control = (packet[3] >> 4) & 0x03
        if adaptation_field_control not in (1, 3):
            return b''
        payload_offset = 4
        if adaptation_field_control == 3:
            payload_offset += 1 + packet[4]
        return packet[payload_offset:]

    @classmethod
    def _applyADTSMetadata(cls, track: AudioTrack, payload: bytes) -> None:
        """AAC ADTSヘッダーからプロファイル・周波数・チャンネル構成を補完する。"""

        for offset in range(max(0, len(payload) - 6)):
            header = payload[offset:offset + 7]
            if header[0] != 0xFF or (header[1] & 0xF6) != 0xF0:
                continue
            sampling_index = (header[2] >> 2) & 0x0F
            channel_configuration = ((header[2] & 0x01) << 2) | (header[3] >> 6)
            frame_length = ((header[3] & 0x03) << 11) | (header[4] << 3) | (header[5] >> 5)
            if sampling_index >= len(cls._SAMPLING_RATES) or frame_length < 7:
                continue
            channel = cls._CHANNELS.get(channel_configuration)
            if channel is None:
                continue
            profile = (header[2] >> 6) + 1
            track['codec'] = {1: 'AAC-Main', 2: 'AAC-LC', 3: 'AAC-SSR', 4: 'AAC-LTP'}.get(profile, 'AAC')
            track['sampling_rate'] = cls._SAMPLING_RATES[sampling_index]
            track['channel'], track['channel_layout'] = channel
            return

    def push(self, packet: bytes) -> None:
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        target = self.targets_by_pid.get(pid)
        if target is None:
            return
        stream_index, base_track = target
        payload = self._payload(packet)
        if payload == b'':
            return
        self._applyADTSMetadata(base_track, payload)
        previous = self.states.get(stream_index)
        if previous is not None:
            self._applyADTSMetadata(previous[3], payload)
        if (packet[1] & 0x40) == 0:
            return
        if len(payload) < 14 or payload[:3] != b'\x00\x00\x01' or (payload[7] & 0x80) == 0:
            return
        raw_pts = (
            ((payload[9] & 0x0E) << 29)
            | (payload[10] << 22)
            | ((payload[11] & 0xFE) << 14)
            | (payload[12] << 7)
            | (payload[13] >> 1)
        )
        unwrapped_pts = TSKeyFrameSeeker.unwrapNear(raw_pts, self.unwrap_targets[pid])
        self.unwrap_targets[pid] = unwrapped_pts
        relative_time = max(0.0, min(self.duration, (unwrapped_pts / 90_000) - self.source_start_time))
        previous = self.states.get(stream_index)
        if previous is None:
            self.states[stream_index] = (
                relative_time, relative_time, 0.0, AudioTrackTimelineTrack(**base_track),
            )
            return
        interval_start, previous_time, typical_interval, timeline_track = previous
        current_interval = max(0.0, relative_time - previous_time)
        gap_threshold = max(2.0, typical_interval * 3)
        if current_interval > gap_threshold:
            interval_end = min(self.duration, previous_time + max(typical_interval, 0.05))
            self.ranges.append((interval_start, interval_end, timeline_track))
            self.states[stream_index] = (
                relative_time, relative_time, 0.0, AudioTrackTimelineTrack(**base_track),
            )
        else:
            next_interval = current_interval if current_interval > 0 else typical_interval
            self.states[stream_index] = (interval_start, relative_time, next_interval, timeline_track)

    def finish(self) -> list[tuple[float, float, AudioTrackTimelineTrack]]:
        for interval_start, previous_time, typical_interval, timeline_track in self.states.values():
            interval_end = min(self.duration, previous_time + max(typical_interval, 0.05))
            if self.duration - interval_end < 0.5:
                interval_end = self.duration
            self.ranges.append((interval_start, interval_end, timeline_track))
        return self.ranges


class RecordedPlaybackIndexer:
    """録画再生用の映像・音声タイムラインを優先度付きワーカープールで生成する。"""

    INDEX_VERSION: ClassVar[int] = RECORDED_PLAYBACK_INDEX_VERSION
    FRAME_PROBE_INACTIVITY_TIMEOUT_SECONDS: ClassVar[float] = 60.0
    INITIAL_PROBE_TIMEOUT_SECONDS: ClassVar[float] = 60.0
    TRANSIENT_AUDIO_CHANGE_MAX_SECONDS: ClassVar[float] = 5.0
    WORKER_COUNT: ClassVar[int] = 4
    _queue: ClassVar[asyncio.PriorityQueue[tuple[Literal[0, 1, 2], int, int]]] = asyncio.PriorityQueue()
    _queued_ids: ClassVar[set[int]] = set()
    _queued_priorities: ClassVar[dict[int, int]] = {}
    _futures: ClassVar[dict[int, asyncio.Future[bool]]] = {}
    _sequence: ClassVar[int] = 0
    _worker_tasks: ClassVar[set[asyncio.Task[None]]] = set()
    _progress: ClassVar[dict[int, tuple[float, RecordedPlaybackIndexStage]]] = {}
    _metadata_seeds: ClassVar[dict[int, schemas.RecordedVideo]] = {}
    _force_rebuild_ids: ClassVar[set[int]] = set()
    _history_handle_tasks: ClassVar[dict[int, asyncio.Task[AnalysisTaskHandle]]] = {}

    @classmethod
    def getProgress(
        cls,
        recorded_video_id: int,
        index_state: RecordedPlaybackIndexState,
    ) -> tuple[float | None, RecordedPlaybackIndexStage]:
        """録画再生索引の現在進捗をクライアント表示用に返す。

        Args:
            recorded_video_id: 進捗を取得する録画ID。
            index_state: DB状態とVersionから計算済みの公開状態。

        Returns:
            0.0～1.0の進捗率と処理段階。失敗時の進捗率だけはNone。
        """

        if index_state == 'Ready':
            return 1.0, 'Complete'
        if index_state == 'Failed':
            return None, 'Failed'
        # 旧Versionを公開したまま再生成する場合、DB状態はStaleのままなのでメモリ上の進捗を優先する。
        if index_state == 'Stale' and recorded_video_id in cls._progress:
            return cls._progress[recorded_video_id]
        if index_state in ('Pending', 'Stale'):
            return 0.0, 'Queued'
        return cls._progress.get(recorded_video_id, (0.0, 'Probing'))

    @classmethod
    async def start(cls) -> None:
        """中断状態を復旧し、既存録画のバックフィルを開始する。"""

        await RecordedVideo.filter(playback_index_status='Analyzing').update(playback_index_status='Pending')
        cls._worker_tasks = {task for task in cls._worker_tasks if task.done() is False}
        while len(cls._worker_tasks) < cls.WORKER_COUNT:
            cls._worker_tasks.add(asyncio.create_task(cls.__worker()))
        if Config().video.recorded_playback_index_backfill_enabled is True:
            await cls.__enqueuePendingBackfill()

    @classmethod
    async def __enqueuePendingBackfill(cls) -> None:
        """未解析の既存録画を最低優先度でバックフィルへ投入する。"""

        index_rows = await RecordedVideo.filter(status='Recorded').order_by('id').values_list(
            'id',
            'playback_index_status',
            'playback_index_version',
        )
        for recorded_video_id, index_status, index_version in cast(
            list[tuple[int, str, int | None]],
            index_rows,
        ):
            # Pendingに加えて、DB上はReadyのまま保持している旧Versionも更新対象にする。
            # 現行Versionで失敗済みの録画は無限に再試行せず、利用者の再解析操作を待つ。
            if index_status == 'Pending' or (index_status == 'Ready' and index_version != cls.INDEX_VERSION):
                cls.enqueue(recorded_video_id, priority=2)

    @classmethod
    async def stop(cls) -> None:
        """バックグラウンドワーカーを停止する。"""

        if len(cls._worker_tasks) == 0:
            return
        for worker_task in cls._worker_tasks:
            worker_task.cancel()
        await asyncio.gather(*cls._worker_tasks, return_exceptions=True)
        cls._worker_tasks.clear()

    @classmethod
    def enqueue(
        cls,
        recorded_video_id: int,
        priority: Literal[0, 1, 2],
        metadata_seed: schemas.RecordedVideo | None = None,
        force_rebuild: bool = False,
    ) -> asyncio.Future[bool]:
        """録画を解析キューへ追加し、同一録画の要求を既存Futureへ合流する。

        Args:
            recorded_video_id: RecordedVideoのID。
            priority: 0=再生要求、1=録画完了、2=バックフィル。
            metadata_seed: 軽量解析から今回の索引だけへ引き渡す暫定メディア情報。
            force_rebuild: 現行Readyでも手動メタデータ再解析後に再構築するか。

        Returns:
            解析完了時に成功可否を返す共有Future。
        """

        if metadata_seed is not None:
            cls._metadata_seeds[recorded_video_id] = metadata_seed
        if force_rebuild is True:
            cls._force_rebuild_ids.add(recorded_video_id)

        existing_future = cls._futures.get(recorded_video_id)
        if existing_future is not None and existing_future.done() is False:
            current_priority = cls._queued_priorities.get(recorded_video_id)
            if current_priority is not None and priority < current_priority:
                # PriorityQueueの既存要素は更新できないため、新優先度で再投入し
                # 古い要素はworker側で無効化する。Futureは同じものを共有する。
                cls._sequence += 1
                cls._queued_priorities[recorded_video_id] = priority
                cls._queue.put_nowait((priority, cls._sequence, recorded_video_id))
            return existing_future
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        cls._futures[recorded_video_id] = future
        cls._sequence += 1
        cls._queued_ids.add(recorded_video_id)
        cls._queued_priorities[recorded_video_id] = priority
        trigger = 'StartupBackfill' if priority == 2 else ('Automatic' if priority == 1 else 'Manual')
        cls._history_handle_tasks[recorded_video_id] = asyncio.create_task(AnalysisTaskTracker.start(
            'PlaybackIndex',
            recorded_video_id=recorded_video_id,
            trigger=trigger,
            initial_status='Queued',
            inherit_parent=False,
        ))
        cls._queue.put_nowait((priority, cls._sequence, recorded_video_id))
        return future

    @classmethod
    async def __worker(cls) -> None:
        """優先度順に録画を1件ずつ解析する。"""

        while True:
            priority, _, recorded_video_id = await cls._queue.get()
            if cls._queued_priorities.get(recorded_video_id) != priority:
                cls._queue.task_done()
                continue
            if priority == 2 and cls.__hasActivePlaybackSessions():
                # 視聴中は新規録画と再生要求を妨げないよう、既存録画バックフィルだけを後ろへ戻す。
                cls._sequence += 1
                cls._queue.put_nowait((priority, cls._sequence, recorded_video_id))
                cls._queue.task_done()
                await asyncio.sleep(1)
                continue
            cls._queued_ids.discard(recorded_video_id)
            cls._queued_priorities.pop(recorded_video_id, None)
            future = cls._futures.get(recorded_video_id)
            try:
                trigger = 'StartupBackfill' if priority == 2 else ('Automatic' if priority == 1 else 'Manual')
                history_handle_task = cls._history_handle_tasks.pop(recorded_video_id, None)
                history_handle = await history_handle_task if history_handle_task is not None else None
                async with AnalysisTaskTracker.track(
                    'PlaybackIndex',
                    recorded_video_id=recorded_video_id,
                    trigger=trigger,
                    existing_handle=history_handle,
                ) as history:
                    await history.setStage('Probing', 0.0)
                    metadata_seed = cls._metadata_seeds.get(recorded_video_id)
                    force_rebuild = recorded_video_id in cls._force_rebuild_ids
                    result = await cls.__analyze(recorded_video_id, priority, metadata_seed, force_rebuild)
                    if result is False:
                        failed_video = await RecordedVideo.get_or_none(id=recorded_video_id)
                        await history.finish(
                            'Failed',
                            error_code=(
                                failed_video.playback_index_error_code
                                if failed_video is not None
                                else 'RecordedVideoUnavailable'
                            ),
                        )
                if future is not None and future.done() is False:
                    future.set_result(result)
            except asyncio.CancelledError:
                if future is not None and future.done() is False:
                    future.cancel()
                raise
            except Exception:
                logging.error(
                    '[RecordedPlaybackIndexer] Unexpected playback index failure. '
                    f'[recorded_video_id: {recorded_video_id}]',
                    exc_info=True,
                )
                await cls.__markFailed(recorded_video_id, 'UnexpectedError')
                if future is not None and future.done() is False:
                    future.set_result(False)
            finally:
                if priority == 2:
                    await cls.__discardRecordingFileCache(recorded_video_id)
                cls._queue.task_done()
                # キャッシュ返却中に同じ録画が再投入される場合があるため、旧workerが新しい世代の
                # Futureと付随状態を削除しないよう、取り出し時のFutureとの同一性を確認する。
                if future is not None and cls._futures.get(recorded_video_id) is future:
                    cls._progress.pop(recorded_video_id, None)
                    cls._futures.pop(recorded_video_id, None)
                    cls._metadata_seeds.pop(recorded_video_id, None)
                    cls._force_rebuild_ids.discard(recorded_video_id)

    @staticmethod
    def __hasActivePlaybackSessions() -> bool:
        """録画視聴セッションがあるかを返す。"""

        # ルーターからIndexerが読まれるため、循環importを避けて実行時に解決する。
        from app.streams.RecordedFMP4Stream import RecordedFMP4Stream

        return RecordedFMP4Stream.hasActiveSessions()

    @staticmethod
    def __getFFprobeCommandPrefix(priority: Literal[0, 1, 2]) -> list[str]:
        """解析優先度に応じたFFprobe 8の実行コマンド先頭を返す。"""

        ffprobe_path = str(LIBRARY_PATH['FFprobe8'])
        if priority == 2 and shutil.which('ionice') is not None and shutil.which('nice') is not None:
            return ['ionice', '-c', '2', '-n', '7', 'nice', '-n', '15', ffprobe_path]
        return [ffprobe_path]

    @classmethod
    async def __discardRecordingFileCache(cls, recorded_video_id: int) -> None:
        """バックフィルで読み終えた録画だけをページキャッシュから返却する。

        キャッシュ返却は性能上の後処理であり、失敗しても生成済み索引の状態は変更しない。
        """

        recorded_video = await RecordedVideo.get_or_none(id=recorded_video_id)
        if recorded_video is None:
            return
        posix_fadvise = getattr(os, 'posix_fadvise', None)
        fadvise_dontneed = getattr(os, 'POSIX_FADV_DONTNEED', None)
        if posix_fadvise is None or fadvise_dontneed is None:
            return

        file_descriptor: int | None = None
        try:
            file_descriptor = os.open(recorded_video.file_path, os.O_RDONLY)
            posix_fadvise(file_descriptor, 0, 0, fadvise_dontneed)
        except OSError as ex:
            logging.warning(
                '[RecordedPlaybackIndexer] Failed to discard recording file cache. '
                f'[recorded_video_id: {recorded_video_id}, error: {ex}]'
            )
        finally:
            if file_descriptor is not None:
                try:
                    os.close(file_descriptor)
                except OSError as ex:
                    logging.warning(
                        '[RecordedPlaybackIndexer] Failed to close recording after cache discard. '
                        f'[recorded_video_id: {recorded_video_id}, error: {ex}]'
                    )

    @classmethod
    async def __analyze(
        cls,
        recorded_video_id: int,
        priority: Literal[0, 1, 2],
        metadata_seed: schemas.RecordedVideo | None = None,
        force_rebuild: bool = False,
    ) -> bool:
        """FFprobe 8から録画1件の映像タイムラインを生成する。"""

        recorded_video = await RecordedVideo.get_or_none(id=recorded_video_id)
        if recorded_video is None:
            return False
        # キュー投入後に別要求や手動復旧で現行索引が完成している場合は、同じ録画を再走査しない。
        # 優先度昇格時の古いキュー要素はworker側で除外するが、DB更新との競合にもここで備える。
        if force_rebuild is False and IsRecordedPlaybackIndexReady(
            recorded_video.playback_index_status,
            recorded_video.playback_index_version,
        ):
            return True
        index_source = metadata_seed or recorded_video
        cls._progress[recorded_video_id] = (0.0, 'Probing')
        # 旧VersionのReady索引は成功時まで公開値を一切変更しない。
        # 索引が存在しない録画だけAnalyzingへ遷移させる。
        if recorded_video.playback_index_status != 'Ready':
            await RecordedVideo.filter(id=recorded_video_id).update(
                playback_index_status = 'Analyzing',
                playback_index_error_code = None,
            )
        file_path = Path(recorded_video.file_path)
        ffprobe_path = Path(LIBRARY_PATH['FFprobe8'])
        if file_path.is_file() is False:
            await cls.__markFailed(recorded_video_id, 'FileUnavailable')
            return False
        if ffprobe_path.is_file() is False:
            await cls.__markFailed(recorded_video_id, 'BinaryUnavailable')
            return False

        process = await asyncio.create_subprocess_exec(
            *cls.__getFFprobeCommandPrefix(priority),
            '-v',
            'error',
            '-show_streams',
            '-show_programs',
            '-show_format',
            '-of',
            'json',
            *BuildKonomiTVBS4KMMTTLVInputArguments(recorded_video.container_format),
            str(file_path),
            stdout = asyncio.subprocess.PIPE,
            stderr = asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=cls.INITIAL_PROBE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            process.kill()
            _, stderr = await process.communicate()
            logging.warning(
                '[RecordedPlaybackIndexer] Initial FFprobe timed out. '
                f'[recorded_video_id: {recorded_video_id}, '
                f'timeout: {cls.INITIAL_PROBE_TIMEOUT_SECONDS:.0f}s, '
                f'stderr: {stderr.decode(errors="ignore").strip()}]'
            )
            await cls.__markFailed(recorded_video_id, 'ProbeTimeout')
            return False
        except asyncio.CancelledError:
            # worker の cancel で communicate() だけを消すと FFprobe と pipe が残る。
            # process を終了して stdout / stderr と wait を最後まで回収してから cancel を再送出する。
            if process.returncode is None:
                process.kill()
            try:
                await process.communicate()
            except Exception:
                pass
            raise
        if process.returncode != 0:
            logging.warning(
                '[RecordedPlaybackIndexer] FFprobe 8 failed. '
                f'[recorded_video_id: {recorded_video_id}, stderr: {stderr.decode(errors="ignore").strip()}]'
            )
            await cls.__markFailed(recorded_video_id, 'ProbeFailed')
            return False
        try:
            probe = cast(dict[str, Any], json.loads(stdout))
            timeline = cls.__buildTimeline(
                probe,
                index_source.duration,
                use_stream_ids_as_pids=recorded_video.container_format == 'MPEG-TS',
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            await cls.__markFailed(recorded_video_id, 'ProbeFailed')
            return False
        if index_source.has_video is True and len(timeline) == 0:
            await cls.__markFailed(recorded_video_id, 'VideoStreamUnavailable')
            return False

        # show_streamsだけでは同じPID/stream index内の解像度・走査方式・色特性変更を
        # 時系列化できない。キーフレームを順次読み、構成が変わる境界だけを区間として残す。
        source_start_time = float(probe.get('format', {}).get('start_time') or 0.0)
        detected_audio_stream_types: dict[int, int] = {}
        if file_path.suffix.lower() in ('.ts', '.mts', '.m2ts'):
            audio_pid_groups = await asyncio.to_thread(TSKeyFrameSeeker.findAudioPIDGroups, file_path)
            known_audio_pids: set[int] = set()
            for track in index_source.audio_tracks:
                track_pid = track.get('pid')
                if track_pid is not None:
                    known_audio_pids.add(int(track_pid))
            for stream in cast(list[dict[str, Any]], probe.get('streams', [])):
                if stream.get('codec_type') != 'audio' or stream.get('id') is None:
                    continue
                try:
                    known_audio_pids.add(int(str(stream['id']), 0))
                except ValueError:
                    pass
            matching_groups = [
                group for group in audio_pid_groups
                if known_audio_pids.intersection(group.keys())
            ]
            if len(matching_groups) == 0 and len(audio_pid_groups) == 1:
                matching_groups = audio_pid_groups
            for group in matching_groups:
                detected_audio_stream_types.update(group)
        normalized_audio_tracks = cls.__normalizeAudioTracks(
            index_source.audio_tracks,
            probe,
            detected_audio_stream_types,
            use_stream_ids_as_pids=recorded_video.container_format == 'MPEG-TS',
        )
        try:
            refined_timeline, audio_timeline, discovered_audio_tracks = await cls.__buildFrameTimelines(
                recorded_video_id,
                file_path,
                index_source.duration,
                source_start_time,
                timeline,
                normalized_audio_tracks,
                probe,
                priority,
            )
        except RecordedPlaybackIndexAnalysisError as ex:
            await cls.__markFailed(recorded_video_id, ex.error_code)
            return False
        if refined_timeline is not None:
            timeline = refined_timeline
        cls._progress[recorded_video_id] = (0.96, 'Finalizing')
        history = AnalysisTaskTracker.currentHandle()
        if history is not None:
            await history.setStage('Finalizing', 0.96)
        # 音声PIDの存在範囲と構成は同じ全編フレーム走査を正とする。
        # 走査自体が失敗した場合だけ、軽量解析の暫定値を保持する。
        if audio_timeline is None:
            audio_timeline = list(index_source.audio_track_timeline)
        audio_timeline = cls.__suppressTransientAudioTimelineChanges(audio_timeline)
        audio_tracks = cls.__reconcileAudioTracks(
            discovered_audio_tracks or index_source.audio_tracks,
            audio_timeline,
        )
        recorded_program_id = getattr(recorded_video, 'recorded_program_id', None)
        if recorded_program_id is not None:
            recorded_program = await RecordedProgram.get_or_none(id=recorded_program_id)
            if recorded_program is not None:
                audio_tracks, audio_timeline = cls.__applyRecordedProgramAudioLanguages(
                    audio_tracks,
                    audio_timeline,
                    recorded_program.primary_audio_language,
                    recorded_program.secondary_audio_language,
                )
        subtitle_tracks = cls.__backfillARIBSubtitleTracks(index_source, probe, file_path)
        representative_video = max(
            timeline,
            key=lambda entry: entry['end_time'] - entry['start_time'],
            default=None,
        )

        await RecordedVideo.filter(id=recorded_video_id).update(
            playback_index_status = 'Ready',
            playback_index_version = cls.INDEX_VERSION,
            playback_indexed_at = datetime.now(tz=JST),
            playback_index_error_code = None,
            video_stream_timeline = timeline,
            audio_tracks = audio_tracks,
            audio_track_timeline = audio_timeline,
            primary_audio_channel = audio_tracks[0]['channel'] if len(audio_tracks) >= 1 else None,
            secondary_audio_channel = audio_tracks[1]['channel'] if len(audio_tracks) >= 2 else None,
            primary_audio_codec = audio_tracks[0]['codec'] if len(audio_tracks) >= 1 else None,
            primary_audio_sampling_rate = audio_tracks[0]['sampling_rate'] if len(audio_tracks) >= 1 else None,
            secondary_audio_codec = audio_tracks[1]['codec'] if len(audio_tracks) >= 2 else None,
            secondary_audio_sampling_rate = audio_tracks[1]['sampling_rate'] if len(audio_tracks) >= 2 else None,
            subtitle_tracks = subtitle_tracks,
            video_codec = representative_video['codec'] if representative_video is not None else None,
            video_codec_profile = representative_video['profile'] if representative_video is not None else None,
            video_scan_type = (
                representative_video['scan_type']
                if representative_video is not None and representative_video['scan_type'] != 'Unknown'
                else None
            ),
            video_frame_rate = representative_video['frame_rate'] if representative_video is not None else None,
            video_resolution_width = representative_video['width'] if representative_video is not None else None,
            video_resolution_height = representative_video['height'] if representative_video is not None else None,
            has_video_stream_changes = len({
                (
                    entry['stream_index'], entry['codec'], entry['profile'], entry['width'], entry['height'],
                    entry['frame_rate'], entry['scan_type'], entry['bit_depth'],
                )
                for entry in timeline
            }) > 1,
        )
        return True

    @staticmethod
    def __normalizeAudioTracks(
        audio_tracks: Sequence[AudioTrack],
        probe: dict[str, Any],
        detected_audio_stream_types: dict[int, int] | None = None,
        use_stream_ids_as_pids: bool = True,
    ) -> list[AudioTrack]:
        """実ファイルの音声stream/PIDを基準に暫定Trackのstream indexを正規化する。"""

        audio_streams = [
            stream for stream in cast(list[dict[str, Any]], probe.get('streams', []))
            if stream.get('codec_type') == 'audio'
        ]
        streams_by_index = {int(stream['index']): stream for stream in audio_streams}
        streams_by_pid: dict[int, dict[str, Any]] = {}
        if use_stream_ids_as_pids is True:
            for stream in audio_streams:
                try:
                    if stream.get('id') is not None:
                        streams_by_pid[int(str(stream['id']), 0)] = stream
                except ValueError:
                    pass

        normalized: list[AudioTrack] = []
        used_stream_indexes: set[int] = set()
        used_pids: set[int] = set()
        for source_track in audio_tracks:
            track = AudioTrack(**source_track)
            matched_stream: dict[str, Any] | None = None
            track_pid = track.get('pid')
            track_stream_index = track.get('stream_index')
            if track_pid is not None:
                matched_stream = streams_by_pid.get(int(track_pid))
            if matched_stream is None and track_stream_index is not None:
                matched_stream = streams_by_index.get(int(track_stream_index))
            if matched_stream is None:
                # FFprobeがAACをstreamとして列挙できない場合でも、軽量解析が実データから得た
                # PID候補は捨てない。後段の生TS packet走査で実在を確認してから採否を決める。
                if track_pid is not None and int(track_pid) in used_pids:
                    continue
                if track_stream_index is None:
                    track['stream_index'] = max(used_stream_indexes, default=-1) + 1
                normalized.append(track)
                used_stream_indexes.add(int(track.get('stream_index', -1)))
                if track_pid is not None:
                    used_pids.add(int(track_pid))
                continue
            stream_index = int(matched_stream['index'])
            if stream_index in used_stream_indexes:
                continue
            track['stream_index'] = stream_index
            if use_stream_ids_as_pids is True:
                try:
                    if matched_stream.get('id') is not None:
                        track['pid'] = int(str(matched_stream['id']), 0)
                except ValueError:
                    pass
            else:
                track.pop('pid', None)
            normalized.append(track)
            used_stream_indexes.add(stream_index)
            normalized_pid = track.get('pid')
            if normalized_pid is not None:
                used_pids.add(int(normalized_pid))

        # 軽量解析で見えていなくても、同じ実ファイルprobeが列挙した音声streamは候補へ追加する。
        for stream in audio_streams:
            stream_index = int(stream['index'])
            if stream_index in used_stream_indexes:
                continue
            codec_name = str(stream.get('codec_name') or 'unknown')
            profile = stream.get('profile')
            if codec_name == 'aac':
                codec = 'AAC-LC' if profile in (None, 'LC', 'unknown') else f'AAC-{profile}'
            elif codec_name in ('ac3', 'eac3'):
                codec = 'AC-3' if codec_name == 'ac3' else 'E-AC-3'
            else:
                codec = codec_name.replace('_', ' ').upper()
            try:
                channels = int(stream.get('channels') or 0)
            except (TypeError, ValueError):
                channels = 0
            try:
                sampling_rate = int(stream.get('sample_rate') or 48_000)
            except (TypeError, ValueError):
                sampling_rate = 48_000
            channel_layout = stream.get('channel_layout')
            track = AudioTrack(
                index=max((int(item['index']) for item in normalized), default=0) + 1,
                stream_index=stream_index,
                codec=codec,
                channel=_getAudioChannelLabel(channels, channel_layout),
                sampling_rate=sampling_rate,
                language=cast(dict[str, str], stream.get('tags') or {}).get('language'),
                title=cast(dict[str, str], stream.get('tags') or {}).get('title'),
                channel_layout=channel_layout,
                is_dual_mono=False,
            )
            if use_stream_ids_as_pids is True:
                try:
                    if stream.get('id') is not None:
                        track['pid'] = int(str(stream['id']), 0)
                except ValueError:
                    pass
            normalized.append(track)
            used_stream_indexes.add(stream_index)
            normalized_pid = track.get('pid')
            if normalized_pid is not None:
                used_pids.add(int(normalized_pid))

        # PMTに音声として定義され、同一programの実PID集合に含まれる一方でFFprobeが
        # stream化できないPIDも候補へ加える。採用は後段の実packet存在範囲走査で確定する。
        next_stream_index = max(
            [*streams_by_index.keys(), *used_stream_indexes],
            default=-1,
        ) + 1
        for pid, stream_type in (detected_audio_stream_types or {}).items():
            if pid in used_pids:
                continue
            codec = {
                0x03: 'MPEG-1 Audio',
                0x04: 'MPEG-2 Audio',
                0x0F: 'AAC-LC',
                0x11: 'AAC-LC',
                0x81: 'AC-3',
                0x87: 'E-AC-3',
            }.get(stream_type, 'Unknown')
            normalized.append(AudioTrack(
                index=max((int(item['index']) for item in normalized), default=0) + 1,
                stream_index=next_stream_index,
                pid=pid,
                codec=codec,
                channel='Unknown',
                sampling_rate=48_000,
                language=None,
                title=None,
                channel_layout=None,
                is_dual_mono=False,
            ))
            used_pids.add(pid)
            used_stream_indexes.add(next_stream_index)
            next_stream_index += 1
        return normalized

    @staticmethod
    def __mergeAudioTimelines(
        duration: float,
        presence_timeline: Sequence[AudioTrackTimelineEntry],
        frame_timeline: Sequence[AudioTrackTimelineEntry] | None,
    ) -> list[AudioTrackTimelineEntry]:
        """PID由来の存在区間へ、音声フレーム由来のチャンネル構成を重ねる。

        Args:
            duration: 録画全体の再生時間。
            presence_timeline: 録画スキャン時にTS PIDなどから得たTrack存在区間。
            frame_timeline: FFprobeの音声フレームから得た構成区間。

        Returns:
            Track存在区間を失わず、構成変化も反映した音声タイムライン。
        """

        # 録画スキャン時の存在区間がない旧データでは、フレーム解析結果をそのまま利用する。
        if len(presence_timeline) == 0:
            return list(frame_timeline or [])
        if frame_timeline is None or len(frame_timeline) == 0:
            return list(presence_timeline)

        boundaries = sorted({
            0.0,
            duration,
            *(value for entry in presence_timeline for value in (entry['start_time'], entry['end_time'])),
            *(value for entry in frame_timeline for value in (entry['start_time'], entry['end_time'])),
        })
        merged_timeline: list[AudioTrackTimelineEntry] = []
        for boundary_index, start_time in enumerate(boundaries[:-1]):
            end_time = boundaries[boundary_index + 1]
            presence_entry = next((
                entry for entry in presence_timeline
                if entry['start_time'] <= start_time < entry['end_time']
            ), None)
            frame_entry = next((
                entry for entry in frame_timeline
                if entry['start_time'] <= start_time < entry['end_time']
            ), None)
            frame_tracks_by_index = {
                int(track['index']): track
                for track in frame_entry['tracks']
            } if frame_entry is not None else {}
            tracks = [
                AudioTrackTimelineTrack(**frame_tracks_by_index.get(int(track['index']), track))
                for track in presence_entry['tracks']
            ] if presence_entry is not None else []
            if merged_timeline and merged_timeline[-1]['tracks'] == tracks:
                merged_timeline[-1]['end_time'] = end_time
            else:
                merged_timeline.append(AudioTrackTimelineEntry(
                    start_time=start_time,
                    end_time=end_time,
                    tracks=tracks,
                ))
        return merged_timeline

    @staticmethod
    def __reconcileAudioTracks(
        audio_tracks: Sequence[AudioTrack],
        audio_timeline: Sequence[AudioTrackTimelineEntry],
    ) -> list[AudioTrack]:
        """全編フレーム走査結果から論理Trackの代表チャンネル構成を確定する。

        EITはDual Monoの候補を与えるだけに留め、実フレームが全区間monoなら
        番組境界のfollowing情報などによる誤ったDual Mono判定を取り消す。
        """

        reconciled_tracks = [AudioTrack(**track) for track in audio_tracks]
        for audio_track in reconciled_tracks:
            timeline_tracks = [
                timeline_track
                for interval in audio_timeline
                for timeline_track in interval['tracks']
                if int(timeline_track['index']) == int(audio_track['index'])
            ]
            if len(timeline_tracks) == 0:
                continue

            dual_mono_track = next((
                track for track in timeline_tracks
                if track.get('is_dual_mono') is True or track['channel'] == 'Dual Mono'
            ), None)
            stereo_track = next((track for track in timeline_tracks if track['channel'] == 'Stereo'), None)
            monaural_track = next((track for track in timeline_tracks if track['channel'] == 'Monaural'), None)
            representative_track = dual_mono_track or stereo_track or monaural_track or timeline_tracks[0]

            audio_track['channel'] = representative_track['channel']
            audio_track['is_dual_mono'] = dual_mono_track is not None
            representative_channel_layout = representative_track.get('channel_layout')
            if representative_channel_layout is not None:
                audio_track['channel_layout'] = representative_channel_layout
            if representative_track.get('language') is not None:
                audio_track['language'] = representative_track['language']

        return reconciled_tracks

    @staticmethod
    def __applyRecordedProgramAudioLanguages(
        audio_tracks: Sequence[AudioTrack],
        audio_timeline: Sequence[AudioTrackTimelineEntry],
        primary_language: str | None,
        secondary_language: str | None,
    ) -> tuple[list[AudioTrack], list[AudioTrackTimelineEntry]]:
        """全編走査後に確定した実TrackへEIT由来の主・副音声言語を補完する。

        FFprobeが返した明示的な言語を最優先し、未設定のTrackだけを補完する。
        Trackの存在・期間・チャンネル構成は実フレーム走査結果を維持し、番組名は判定に使わない。
        """

        completed_tracks = [AudioTrack(**track) for track in audio_tracks]
        language_hints = [primary_language, secondary_language]
        languages_by_track_index: dict[int, str] = {}
        for track, language_hint in zip(completed_tracks, language_hints):
            if track.get('language') is None and language_hint is not None:
                track['language'] = language_hint
            if track.get('language') is not None:
                languages_by_track_index[int(track['index'])] = cast(str, track['language'])

        completed_timeline: list[AudioTrackTimelineEntry] = []
        for interval in audio_timeline:
            timeline_tracks: list[AudioTrackTimelineTrack] = []
            for source_track in interval['tracks']:
                timeline_track = AudioTrackTimelineTrack(**source_track)
                language = languages_by_track_index.get(int(timeline_track['index']))
                if timeline_track.get('language') is None and language is not None:
                    timeline_track['language'] = language
                timeline_tracks.append(timeline_track)
            completed_timeline.append(AudioTrackTimelineEntry(
                start_time=interval['start_time'],
                end_time=interval['end_time'],
                tracks=timeline_tracks,
            ))

        return completed_tracks, completed_timeline

    @staticmethod
    def __backfillARIBSubtitleTracks(
        recorded_video: RecordedVideo | schemas.RecordedVideo,
        probe: dict[str, Any],
        file_path: Path,
    ) -> list[SubtitleTrack]:
        """既存TSでbin_data扱いされたARIB字幕トラックをPMTから補完する。

        Args:
            recorded_video: 補完対象の録画レコード。
            probe: 同じ解析で得たFFprobe JSON。
            file_path: PMTを走査する録画ファイル。

        Returns:
            既存情報を保持し、未登録ARIB字幕だけを加えたトラック一覧。
        """

        subtitle_tracks = [SubtitleTrack(**track) for track in recorded_video.subtitle_tracks]
        if recorded_video.container_format != 'MPEG-TS':
            return subtitle_tracks
        selected_program_number: int | None = None
        representative_stream = next((
            stream for stream in probe.get('streams', [])
            if stream.get('codec_type') == 'video' and stream.get('index') is not None
        ), None) or next((
            stream for stream in probe.get('streams', [])
            if stream.get('codec_type') == 'audio' and stream.get('index') is not None
        ), None)
        if representative_stream is not None:
            representative_stream_index = int(representative_stream['index'])
            for program in probe.get('programs', []):
                program_stream_indices = {
                    int(stream['index'])
                    for stream in program.get('streams', [])
                    if stream.get('index') is not None
                }
                if representative_stream_index not in program_stream_indices:
                    continue
                raw_program_number = program.get('program_num')
                if raw_program_number is None:
                    raw_program_number = program.get('program_id')
                if raw_program_number is not None:
                    selected_program_number = int(raw_program_number)
                break
        if selected_program_number is not None:
            # 旧索引がprogram識別を持たないTTML trackや別serviceのtrackを引き継がない。
            subtitle_tracks = [
                track for track in subtitle_tracks
                if track['codec'].lower() != 'arib_ttml' or track.get('program_number') == selected_program_number
            ]
        try:
            caption_pids = TSKeyFrameSeeker.findARIBCaptionPIDs(file_path)
        except (OSError, ValueError):
            caption_pids = set()
        known_stream_indexes = {
            stream_index
            for track in subtitle_tracks
            if (stream_index := track.get('stream_index')) is not None
        }
        next_track_index = max((track['index'] for track in subtitle_tracks), default=0) + 1
        for stream in probe.get('streams', []):
            if stream.get('codec_type') != 'subtitle' or int(stream.get('index', -1)) in known_stream_indexes:
                continue
            tags = stream.get('tags') if isinstance(stream.get('tags'), dict) else {}
            try:
                pid = int(str(stream['id']), 0) if stream.get('id') is not None else None
            except ValueError:
                pid = None
            stream_index = int(stream['index'])
            subtitle_track = SubtitleTrack(
                index=next_track_index,
                stream_index=stream_index,
                codec=str(stream.get('codec_name') or 'unknown'),
                language=tags.get('language'),
                title=tags.get('title'),
            )
            if pid is not None:
                subtitle_track['pid'] = pid
            subtitle_tracks.append(subtitle_track)
            known_stream_indexes.add(stream_index)
            next_track_index += 1
        for stream in probe.get('streams', []):
            if stream.get('codec_type') != 'data' or int(stream.get('index', -1)) in known_stream_indexes:
                continue
            try:
                pid = int(str(stream['id']), 0) if stream.get('id') is not None else None
            except ValueError:
                pid = None
            if pid is None or pid not in caption_pids:
                continue
            tags = stream.get('tags') if isinstance(stream.get('tags'), dict) else {}
            stream_index = int(stream['index'])
            subtitle_tracks.append(SubtitleTrack(
                index=next_track_index,
                stream_index=stream_index,
                codec='arib_caption',
                language=tags.get('language'),
                title=tags.get('title'),
                pid=pid,
            ))
            known_stream_indexes.add(stream_index)
            next_track_index += 1

        # ARIB-TTMLはmetadata streamとして記録されるため、通常のsubtitle/bin_data経路には
        # 現れない。PMTのID3 metadata_descriptorと実PRIV ownerを確認し、既存Ready録画にも
        # 論理字幕トラックとしてバックフィルする。途中追加PIDではstream_indexを省略できる。
        try:
            arib_ttml_streams = TSKeyFrameSeeker.findARIBTTMLStreams(file_path)
        except (OSError, ValueError):
            arib_ttml_streams = []
        probe_data_streams_by_pid: dict[int, dict[str, Any]] = {}
        for stream in probe.get('streams', []):
            if stream.get('codec_type') != 'data':
                continue
            try:
                pid = int(str(stream['id']), 0) if stream.get('id') is not None else None
            except ValueError:
                pid = None
            if pid is not None:
                probe_data_streams_by_pid[pid] = stream
        known_arib_ttml_streams = {
            (track.get('program_number'), track.get('pid'), track.get('component_tag'))
            for track in subtitle_tracks
            if track['codec'].lower() == 'arib_ttml'
        }
        for stream_info in arib_ttml_streams:
            if (
                selected_program_number is not None and
                stream_info.program_number != selected_program_number
            ):
                continue
            stream_key = (stream_info.program_number, stream_info.pid, stream_info.component_tag)
            if stream_key in known_arib_ttml_streams or (None, stream_info.pid, stream_info.component_tag) in known_arib_ttml_streams:
                continue
            probe_stream = probe_data_streams_by_pid.get(stream_info.pid)
            tags: dict[str, Any] = {}
            if probe_stream is not None and isinstance(probe_stream.get('tags'), dict):
                tags = probe_stream['tags']
            subtitle_track = SubtitleTrack(
                index=next_track_index,
                codec='arib_ttml',
                language=tags.get('language') or 'jpn',
                title=tags.get('title'),
                pid=stream_info.pid,
                component_tag=stream_info.component_tag,
                program_number=stream_info.program_number,
            )
            if probe_stream is not None:
                stream_index = int(probe_stream['index'])
                subtitle_track['stream_index'] = stream_index
                known_stream_indexes.add(stream_index)
            subtitle_tracks.append(subtitle_track)
            known_arib_ttml_streams.add(stream_key)
            next_track_index += 1
        return subtitle_tracks

    @staticmethod
    def __buildTimeline(
        probe: dict[str, Any],
        fallback_duration: float,
        use_stream_ids_as_pids: bool = True,
    ) -> list[VideoStreamTimelineEntry]:
        """FFprobe JSONから映像ストリームごとの時間範囲を構築する。"""

        # MPEG-TS の start_time は録画先頭からの相対時刻ではなく、放送波由来の大きな PTS になる。
        # format.start_time を録画先頭として差し引き、利用できない場合は最初の映像ストリームを基準にする。
        video_streams = [
            stream
            for stream in probe.get('streams', [])
            if stream.get('codec_type') == 'video'
        ]
        if len(video_streams) == 0:
            return []
        stream_start_times = [float(stream.get('start_time') or 0.0) for stream in video_streams]
        format_start_time = probe.get('format', {}).get('start_time')
        source_start_time = float(format_start_time) if format_start_time is not None else min(stream_start_times)

        timeline: list[VideoStreamTimelineEntry] = []
        for stream in video_streams:
            source_stream_start_time = float(stream.get('start_time') or source_start_time)
            start_time = max(0.0, min(fallback_duration, source_stream_start_time - source_start_time))
            duration = float(stream.get('duration') or probe.get('format', {}).get('duration') or fallback_duration)
            field_order = str(stream.get('field_order') or '').lower()
            if field_order == 'progressive':
                scan_type: Literal['Interlaced', 'Progressive', 'Unknown'] = 'Progressive'
            elif field_order in ('tt', 'bb', 'tb', 'bt'):
                scan_type = 'Interlaced'
            else:
                scan_type = 'Unknown'
            pixel_format = str(stream.get('pix_fmt') or '')
            bit_depth = int(stream.get('bits_per_raw_sample') or (10 if '10' in pixel_format or pixel_format.startswith('p010') else 8))
            frame_rate_text = str(stream.get('avg_frame_rate') or stream.get('r_frame_rate') or '0/1')
            numerator, denominator = frame_rate_text.split('/', maxsplit=1)
            frame_rate = float(numerator) / float(denominator) if float(denominator) != 0 else 0.0
            pid: int | None = None
            if use_stream_ids_as_pids is True:
                try:
                    pid = int(str(stream['id']), 0) if stream.get('id') is not None else None
                except ValueError:
                    pid = None
            width = int(stream.get('width') or 0)
            height = int(stream.get('height') or 0)
            sample_aspect_ratio = _normalizeAspectRatio(stream.get('sample_aspect_ratio'))
            display_aspect_ratio = _normalizeAspectRatio(stream.get('display_aspect_ratio')) or \
                _calculateDisplayAspectRatio(width, height, sample_aspect_ratio)
            mastering_display_metadata: dict[str, object] | None = None
            content_light_level: dict[str, object] | None = None
            for side_data in stream.get('side_data_list', []):
                side_data_type = str(side_data.get('side_data_type') or '')
                if side_data_type == 'Mastering display metadata':
                    mastering_display_metadata = dict(side_data)
                elif side_data_type == 'Content light level metadata':
                    content_light_level = dict(side_data)
            timeline.append(VideoStreamTimelineEntry(
                start_time = start_time,
                end_time = max(start_time, min(fallback_duration, start_time + duration)),
                pid = pid,
                stream_index = int(stream['index']),
                codec = str(stream.get('codec_name') or 'unknown'),
                profile = str(stream.get('profile') or 'Unknown'),
                width = width,
                height = height,
                sample_aspect_ratio = sample_aspect_ratio,
                display_aspect_ratio = display_aspect_ratio,
                frame_rate = frame_rate,
                scan_type = scan_type,
                bit_depth = bit_depth,
                color_range = stream.get('color_range'),
                color_space = stream.get('color_space'),
                color_primaries = stream.get('color_primaries'),
                color_transfer = stream.get('color_transfer'),
                mastering_display_metadata = mastering_display_metadata,
                content_light_level = content_light_level,
            ))
        return sorted(timeline, key=lambda entry: (entry['start_time'], entry['stream_index']))

    @classmethod
    async def __buildFrameTimelines(
        cls,
        recorded_video_id: int,
        file_path: Path,
        duration: float,
        source_start_time: float,
        stream_timeline: Sequence[VideoStreamTimelineEntry],
        audio_tracks: Sequence[AudioTrack],
        probe: dict[str, Any],
        priority: Literal[0, 1, 2],
    ) -> tuple[
        list[VideoStreamTimelineEntry] | None,
        list[AudioTrackTimelineEntry] | None,
        list[AudioTrack] | None,
    ]:
        """1回の全編走査から映像キーフレームと全音声フレームを索引化する。

        ``-skip_frame:v nokey`` は映像だけへ適用されるため、映像はキーフレームに
        絞りながら、音声構成変化の検出に必要な全音声フレームを同時に取得できる。

        Returns:
            映像タイムラインと音声タイムライン。解析不能な場合は両方None。
        """

        indexed_audio_tracks = [AudioTrack(**track) for track in audio_tracks]
        tracks_by_stream_index = {
            int(track.get('stream_index', track['index'])): track
            for track in indexed_audio_tracks
        }
        probe_audio_pids: set[int] = set()
        for stream in cast(list[dict[str, Any]], probe.get('streams', [])):
            if stream.get('codec_type') != 'audio' or stream.get('id') is None:
                continue
            try:
                probe_audio_pids.add(int(str(stream['id']), 0))
            except ValueError:
                pass
        hidden_audio_stream_indexes: set[int] = set()
        for stream_index, track in tracks_by_stream_index.items():
            track_pid = track.get('pid')
            if track_pid is not None and int(track_pid) not in probe_audio_pids:
                hidden_audio_stream_indexes.add(stream_index)
        is_mpeg_ts = file_path.suffix.lower() in ('.ts', '.mts', '.m2ts')
        ts_audio_collector = _TSAudioPIDCollector(
            duration,
            source_start_time,
            tracks_by_stream_index,
            hidden_audio_stream_indexes,
        ) if is_mpeg_ts and hidden_audio_stream_indexes else None
        input_args = ['-f', 'mpegts', 'pipe:0'] if ts_audio_collector is not None else [
            *BuildKonomiTVBS4KMMTTLVInputArguments(
                MMT_TLV_CONTAINER_FORMAT if file_path.suffix.lower() in MMT_TLV_FILE_EXTENSIONS else '',
            ),
            str(file_path),
        ]

        process = await asyncio.create_subprocess_exec(
            *cls.__getFFprobeCommandPrefix(priority),
            '-v', 'error',
            '-skip_frame:v', 'nokey',
            '-show_frames',
            '-show_streams',
            '-show_entries',
            'frame=media_type,stream_index,pts_time,width,height,sample_aspect_ratio,pix_fmt,interlaced_frame,'
            'color_range,color_space,color_primaries,color_transfer,channels,channel_layout:'
            'frame_side_data=side_data_type,red_x,red_y,green_x,green_y,blue_x,blue_y,'
            'white_point_x,white_point_y,min_luminance,max_luminance,max_content,max_average:'
            'stream=index,id,codec_type,codec_name,profile,width,height,sample_aspect_ratio,display_aspect_ratio,'
            'pix_fmt,field_order,'
            'avg_frame_rate,r_frame_rate,bits_per_raw_sample,sample_rate,channels,channel_layout,'
            'start_time,duration,color_range,color_space,color_primaries,color_transfer:'
            'stream_tags=language,title',
            '-of', 'compact=p=1:nk=0',
            *input_args,
            stdin=asyncio.subprocess.PIPE if ts_audio_collector is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        stderr_reader = process.stderr
        assert stderr_reader is not None

        async def DrainStderr() -> bytes:
            stderr_tail = bytearray()
            while chunk := await stderr_reader.read(64 * 1024):
                stderr_tail.extend(chunk)
                if len(stderr_tail) > 64 * 1024:
                    del stderr_tail[:-64 * 1024]
            return bytes(stderr_tail)

        stderr_task = asyncio.create_task(DrainStderr())

        async def FeedTSInput() -> list[tuple[float, float, AudioTrackTimelineTrack]]:
            assert process.stdin is not None
            assert ts_audio_collector is not None
            with file_path.open('rb') as file:
                head = file.read(192 * 5)
                packet_size: int | None = None
                for candidate_size, sync_offset in ((188, 0), (192, 4)):
                    if all(
                        sync_offset + candidate_size * index < len(head)
                        and head[sync_offset + candidate_size * index] == 0x47
                        for index in range(5)
                    ):
                        packet_size = candidate_size
                        break
                if packet_size is None:
                    raise RecordedPlaybackIndexAnalysisError('InvalidTSPacketSize')
                file.seek(0)
                while chunk := file.read(packet_size * 4096):
                    for offset in range(0, len(chunk) - packet_size + 1, packet_size):
                        packet = TSKeyFrameSeeker.normalizePacket(chunk[offset:offset + packet_size], packet_size)
                        if packet is not None:
                            ts_audio_collector.push(packet)
                    process.stdin.write(chunk)
                    await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()
            return ts_audio_collector.finish()

        ts_feed_task = asyncio.create_task(FeedTSInput()) if ts_audio_collector is not None else None

        base_entries = {entry['stream_index']: entry for entry in stream_timeline}
        video_active_states: dict[int, tuple[tuple[object, ...], VideoStreamTimelineEntry]] = {}
        video_ranges: list[VideoStreamTimelineEntry] = []
        audio_active_states: dict[int, tuple[tuple[int, str], float, float, AudioTrackTimelineTrack]] = {}
        audio_ranges: list[tuple[float, float, AudioTrackTimelineTrack]] = []
        cls._progress[recorded_video_id] = (0.01, 'Scanning')
        history = AnalysisTaskTracker.currentHandle()
        if history is not None:
            await history.setStage('Scanning', 0.01)

        try:
            while True:
                # 破損TSの末尾などで FFprobe が無期限に停止すると単一ワーカー全体が詰まるため、
                # 総実行時間ではなく出力行の無通信時間を監視する。正常な長尺録画は走査中も継続的に行を出力する。
                try:
                    raw_line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=cls.FRAME_PROBE_INACTIVITY_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    stderr = await stderr_task
                    logging.warning(
                        '[RecordedPlaybackIndexer] FFprobe 8 frame scan timed out after no output. '
                        f'[recorded_video_id: {recorded_video_id}, '
                        f'timeout: {cls.FRAME_PROBE_INACTIVITY_TIMEOUT_SECONDS:.0f}s, '
                        f'stderr: {stderr.decode(errors="ignore").strip()}]'
                    )
                    raise RecordedPlaybackIndexAnalysisError('FrameProbeTimeout')
                if raw_line == b'':
                    break

                decoded_line = raw_line.decode(errors='ignore').strip()
                # side_data 区切り自体も escape 対象になりうるが、FFprobe は固定トークンで出力する
                sections = decoded_line.split('|side_data|')
                section_fields = splitFFprobeCompactFields(sections[0])
                section_kind = section_fields[0] if len(section_fields) > 0 else ''
                values = parseFFprobeCompactKeyValues(sections[0])
                if section_kind == 'stream':
                    cls.__applyDiscoveredStreamMetadata(
                        values,
                        duration,
                        base_entries,
                        video_active_states,
                        video_ranges,
                        indexed_audio_tracks,
                        tracks_by_stream_index,
                        audio_active_states,
                        audio_ranges,
                        probe,
                    )
                    continue
                media_type = values.get('media_type')
                cls.__updateFrameProgress(
                    recorded_video_id,
                    values.get('pts_time'),
                    source_start_time,
                    duration,
                )
                if media_type == 'video':
                    cls.__appendVideoFrameTimelineEntry(
                        values,
                        sections[1:],
                        source_start_time,
                        duration,
                        base_entries,
                        video_active_states,
                        video_ranges,
                    )
                elif media_type == 'audio':
                    cls.__appendAudioFrameTimelineEntry(
                        values,
                        duration,
                        source_start_time,
                        tracks_by_stream_index,
                        indexed_audio_tracks,
                        audio_active_states,
                        audio_ranges,
                    )
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            if ts_feed_task is not None:
                ts_feed_task.cancel()
                await asyncio.gather(ts_feed_task, return_exceptions=True)
            await stderr_task
            raise
        except RecordedPlaybackIndexAnalysisError:
            if ts_feed_task is not None:
                ts_feed_task.cancel()
                await asyncio.gather(ts_feed_task, return_exceptions=True)
            raise

        return_code = await process.wait()
        stderr = await stderr_task
        collected_ts_audio_ranges = await ts_feed_task if ts_feed_task is not None else []
        if return_code != 0:
            logging.warning(
                '[RecordedPlaybackIndexer] FFprobe 8 frame analysis failed. '
                f'[file_path: {file_path}, stderr: {stderr.decode(errors="ignore").strip()}]'
            )
            raise RecordedPlaybackIndexAnalysisError('FrameProbeFailed')

        observed_audio_stream_indexes = set(audio_active_states.keys())
        for _, _, track in audio_ranges:
            stream_index = track.get('stream_index')
            if stream_index is not None:
                observed_audio_stream_indexes.add(int(stream_index))
        missing_audio_stream_indexes: set[int] = set()
        for track in indexed_audio_tracks:
            stream_index = track.get('stream_index')
            if stream_index is not None and int(stream_index) not in observed_audio_stream_indexes:
                missing_audio_stream_indexes.add(int(stream_index))
        if missing_audio_stream_indexes:
            collected_stream_indexes = {
                int(track.get('stream_index', -1))
                for _, _, track in collected_ts_audio_ranges
            }
            audio_ranges.extend(
                item for item in collected_ts_audio_ranges
                if int(item[2].get('stream_index', -1)) in missing_audio_stream_indexes
            )
            missing_audio_stream_indexes -= collected_stream_indexes
        if missing_audio_stream_indexes:
            # AACが破損していてframeへデコードできない副音声でも、実パケットが存在するなら
            # 複数音声Trackから落としてはならない。欠落streamがある場合だけ音声packetを補助走査する。
            packet_ranges = await cls.__probeMissingAudioPacketRanges(
                recorded_video_id,
                file_path,
                duration,
                source_start_time,
                missing_audio_stream_indexes,
                tracks_by_stream_index,
                priority,
            )
            audio_ranges.extend(packet_ranges)

        video_ranges.extend(entry for _, entry in video_active_states.values())
        video_timeline = sorted(
            video_ranges,
            key=lambda entry: (entry['start_time'], entry['stream_index']),
        ) if video_ranges else None
        audio_timeline = cls.__finalizeAudioTimeline(
            duration,
            indexed_audio_tracks,
            audio_active_states,
            audio_ranges,
        )
        present_audio_track_indexes = {
            int(track['index'])
            for interval in audio_timeline or []
            for track in interval['tracks']
        }
        indexed_audio_tracks = [
            track for track in indexed_audio_tracks
            if int(track['index']) in present_audio_track_indexes
        ]
        return video_timeline, audio_timeline, indexed_audio_tracks

    @classmethod
    async def __probeMissingAudioPacketRanges(
        cls,
        recorded_video_id: int,
        file_path: Path,
        duration: float,
        source_start_time: float,
        missing_stream_indexes: set[int],
        tracks_by_stream_index: dict[int, AudioTrack],
        priority: Literal[0, 1, 2],
    ) -> list[tuple[float, float, AudioTrackTimelineTrack]]:
        """frameへデコードできない音声streamの実パケット存在範囲を補助走査する。"""

        process = await asyncio.create_subprocess_exec(
            *cls.__getFFprobeCommandPrefix(priority),
            '-v', 'error',
            '-select_streams', 'a',
            '-show_packets',
            '-show_entries', 'packet=stream_index,pts_time,duration_time',
            '-of', 'compact=p=1:nk=0',
            *BuildKonomiTVBS4KMMTTLVInputArguments(
                MMT_TLV_CONTAINER_FORMAT if file_path.suffix.lower() in MMT_TLV_FILE_EXTENSIONS else '',
            ),
            str(file_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        stdout_reader = process.stdout
        stderr_reader = process.stderr
        assert stderr_reader is not None

        async def DrainStderr() -> bytes:
            stderr_tail = bytearray()
            while chunk := await stderr_reader.read():
                stderr_tail.extend(chunk)
                if len(stderr_tail) > 64 * 1024:
                    del stderr_tail[:-64 * 1024]
            return bytes(stderr_tail)

        stderr_task = asyncio.create_task(DrainStderr())

        async def TerminateAndReapProcess() -> bytes:
            """packet probeを終了し、両pipeをdrainしてから子processを回収する。"""

            if process.returncode is None:
                process.kill()
            _, stderr_result = await asyncio.gather(
                stdout_reader.read(),
                stderr_task,
                return_exceptions=True,
            )
            await process.wait()
            return stderr_result if isinstance(stderr_result, bytes) else b''

        states: dict[int, tuple[float, float, AudioTrackTimelineTrack]] = {}
        ranges: list[tuple[float, float, AudioTrackTimelineTrack]] = []
        try:
            while True:
                try:
                    raw_line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=cls.FRAME_PROBE_INACTIVITY_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    stderr = await TerminateAndReapProcess()
                    logging.warning(
                        '[RecordedPlaybackIndexer] FFprobe 8 audio packet scan timed out after no output. '
                        f'[recorded_video_id: {recorded_video_id}, '
                        f'stderr: {stderr.decode(errors="ignore").strip()}]'
                    )
                    raise RecordedPlaybackIndexAnalysisError('AudioPacketProbeTimeout')
                if raw_line == b'':
                    break
                values = parseFFprobeCompactKeyValues(raw_line.decode(errors='ignore').strip())
                try:
                    stream_index = int(values['stream_index'])
                    pts_time = float(values['pts_time'])
                except (KeyError, ValueError):
                    continue
                if stream_index not in missing_stream_indexes:
                    continue
                base_track = tracks_by_stream_index.get(stream_index)
                if base_track is None:
                    continue
                try:
                    packet_duration = max(0.0, float(values.get('duration_time') or 0.0))
                except ValueError:
                    packet_duration = 0.0
                if packet_duration == 0.0:
                    packet_duration = 1024 / int(base_track.get('sampling_rate') or 48_000)
                packet_start = max(0.0, min(duration, pts_time - source_start_time))
                packet_end = max(packet_start, min(duration, packet_start + packet_duration))
                previous = states.get(stream_index)
                if previous is None:
                    states[stream_index] = (
                        packet_start,
                        packet_end,
                        AudioTrackTimelineTrack(**base_track),
                    )
                    continue
                interval_start, previous_end, timeline_track = previous
                if packet_start - previous_end > 1.0:
                    ranges.append((interval_start, previous_end, timeline_track))
                    states[stream_index] = (packet_start, packet_end, timeline_track)
                else:
                    states[stream_index] = (interval_start, max(previous_end, packet_end), timeline_track)
        except asyncio.CancelledError:
            await TerminateAndReapProcess()
            raise

        return_code = await process.wait()
        stderr = await stderr_task
        if return_code != 0:
            logging.warning(
                '[RecordedPlaybackIndexer] FFprobe 8 audio packet analysis failed. '
                f'[file_path: {file_path}, stderr: {stderr.decode(errors="ignore").strip()}]'
            )
            raise RecordedPlaybackIndexAnalysisError('AudioPacketProbeFailed')
        ranges.extend(states.values())
        unresolved_stream_indexes = missing_stream_indexes - states.keys()
        if unresolved_stream_indexes and file_path.suffix.lower() in ('.ts', '.mts', '.m2ts'):
            ranges.extend(await asyncio.to_thread(
                cls.__scanTSAudioPIDRanges,
                file_path,
                duration,
                source_start_time,
                unresolved_stream_indexes,
                tracks_by_stream_index,
            ))
        return ranges

    @staticmethod
    def __scanTSAudioPIDRanges(
        file_path: Path,
        duration: float,
        source_start_time: float,
        target_stream_indexes: set[int],
        tracks_by_stream_index: dict[int, AudioTrack],
    ) -> list[tuple[float, float, AudioTrackTimelineTrack]]:
        """FFprobeがstream化できない音声PIDのPES PTSを生TS packetから走査する。"""

        collector = _TSAudioPIDCollector(
            duration,
            source_start_time,
            tracks_by_stream_index,
            target_stream_indexes,
        )
        if not collector.targets_by_pid:
            return []

        with file_path.open('rb') as file:
            head = file.read(192 * 5)
            packet_size: int | None = None
            for candidate_size, sync_offset in ((188, 0), (192, 4)):
                if all(
                    sync_offset + candidate_size * index < len(head)
                    and head[sync_offset + candidate_size * index] == 0x47
                    for index in range(5)
                ):
                    packet_size = candidate_size
                    break
            if packet_size is None:
                return []
            file.seek(0)
            while True:
                packet = TSKeyFrameSeeker.normalizePacket(file.read(packet_size), packet_size)
                if packet is None:
                    break
                collector.push(packet)

        return collector.finish()

    @staticmethod
    def __applyDiscoveredStreamMetadata(
        values: dict[str, str],
        duration: float,
        base_entries: dict[int, VideoStreamTimelineEntry],
        video_active_states: dict[int, tuple[tuple[object, ...], VideoStreamTimelineEntry]],
        video_ranges: list[VideoStreamTimelineEntry],
        audio_tracks: list[AudioTrack],
        tracks_by_stream_index: dict[int, AudioTrack],
        audio_active_states: dict[int, tuple[tuple[int, str], float, float, AudioTrackTimelineTrack]],
        audio_ranges: list[tuple[float, float, AudioTrackTimelineTrack]],
        probe: dict[str, Any],
    ) -> None:
        """全編走査末尾のstream情報を途中追加Trackと確定区間へ反映する。

        Args:
            values: compact形式のstreamセクション。
            duration: 録画全体の秒数。
            base_entries: 映像streamごとの基礎情報。
            video_active_states: 現在継続中の映像区間。
            video_ranges: 確定済み映像区間。
            audio_tracks: 全編走査で発見した音声Track一覧。
            tracks_by_stream_index: 音声stream indexからTrackへの対応。
            audio_active_states: 現在継続中の音声区間。
            audio_ranges: 確定済み音声区間。
            probe: 字幕確定にも再利用するFFprobe結果。

        Returns:
            None
        """

        try:
            stream_index = int(values['index'])
        except (KeyError, ValueError):
            return
        codec_type = values.get('codec_type')
        try:
            pid = int(values['id'], 0) if values.get('id') is not None else None
        except ValueError:
            pid = None

        # 同じ全編走査で発見したstreamは字幕確定処理にも渡す。
        probe_streams = cast(list[dict[str, Any]], probe.setdefault('streams', []))
        existing_probe_stream = next((stream for stream in probe_streams if int(stream.get('index', -1)) == stream_index), None)
        if existing_probe_stream is None:
            probe_streams.append(dict(values))
        else:
            existing_probe_stream.update(values)

        if codec_type == 'video':
            base_entry = base_entries.get(stream_index)
            if base_entry is None:
                return
            frame_rate_text = values.get('avg_frame_rate') or values.get('r_frame_rate') or '0/1'
            try:
                numerator, denominator = frame_rate_text.split('/', maxsplit=1)
                frame_rate = float(numerator) / float(denominator) if float(denominator) != 0 else 0.0
            except (ValueError, ZeroDivisionError):
                frame_rate = 0.0
            video_entries = [base_entry, *video_ranges]
            active_video = video_active_states.get(stream_index)
            if active_video is not None:
                video_entries.append(active_video[1])
            for entry in video_entries:
                if entry['stream_index'] != stream_index:
                    continue
                entry['pid'] = pid
                entry['codec'] = values.get('codec_name') or entry['codec']
                entry['profile'] = values.get('profile') or entry['profile']
                entry['frame_rate'] = frame_rate or entry['frame_rate']
                entry['end_time'] = min(duration, entry['end_time'])
            return

        if codec_type != 'audio':
            return
        base_track = tracks_by_stream_index.get(stream_index)
        if base_track is None:
            return
        codec_name = values.get('codec_name') or 'unknown'
        profile = values.get('profile')
        if codec_name == 'aac':
            codec = 'AAC-LC' if profile in (None, 'LC', 'unknown') else f'AAC-{profile}'
        elif codec_name in ('ac3', 'eac3'):
            codec = 'AC-3' if codec_name == 'ac3' else 'E-AC-3'
        else:
            codec = codec_name.replace('_', ' ').upper()
        try:
            sampling_rate = int(values.get('sample_rate') or 48_000)
        except ValueError:
            sampling_rate = 48_000
        if sampling_rate <= 0:
            sampling_rate = 48_000
        audio_entries: list[AudioTrack] = [*audio_tracks]
        audio_entries.extend(state[3] for state in audio_active_states.values())
        audio_entries.extend(track for _, _, track in audio_ranges)
        for track in audio_entries:
            if int(track.get('stream_index', -1)) != stream_index:
                continue
            track['codec'] = codec
            track['sampling_rate'] = sampling_rate
            if values.get('tag:language') is not None:
                track['language'] = values['tag:language']
            if values.get('tag:title') is not None:
                track['title'] = values['tag:title']
            if pid is not None:
                track['pid'] = pid

    @classmethod
    def __updateFrameProgress(
        cls,
        recorded_video_id: int,
        pts_time_text: str | None,
        source_start_time: float,
        duration: float,
    ) -> None:
        """FFprobeが出力したフレーム時刻を全編走査の進捗へ反映する。

        Args:
            recorded_video_id: 解析中の録画ID。
            pts_time_text: FFprobeのフレームPTS文字列。
            source_start_time: 入力コンテナの先頭時刻。
            duration: 録画全体の秒数。

        Returns:
            None
        """

        if pts_time_text is None or duration <= 0:
            return
        try:
            frame_ratio = (float(pts_time_text) - source_start_time) / duration
        except ValueError:
            return
        # 初期probeを1%、走査後の索引確定を5%として確保し、表示が100%のまま長時間止まらないようにする。
        progress = 0.01 + max(0.0, min(1.0, frame_ratio)) * 0.94
        current_progress, _ = cls._progress.get(recorded_video_id, (0.01, 'Scanning'))
        cls._progress[recorded_video_id] = (max(current_progress, progress), 'Scanning')
        AnalysisTaskTracker.updateProgressSoon(progress)

    @staticmethod
    def __appendVideoFrameTimelineEntry(
        values: dict[str, str],
        side_data_sections: Sequence[str],
        source_start_time: float,
        duration: float,
        base_entries: dict[int, VideoStreamTimelineEntry],
        active_states: dict[int, tuple[tuple[object, ...], VideoStreamTimelineEntry]],
        ranges: list[VideoStreamTimelineEntry],
    ) -> None:
        """映像frame 1件を現在の映像構成区間へ反映する。"""

        mastering_display_metadata: dict[str, object] | None = None
        content_light_level: dict[str, object] | None = None
        for side_data_section in side_data_sections:
            side_data = parseFFprobeCompactKeyValues(side_data_section)
            if side_data.get('side_data_type') == 'Mastering display metadata':
                mastering_display_metadata = dict(side_data)
            elif side_data.get('side_data_type') == 'Content light level metadata':
                content_light_level = dict(side_data)
        try:
            stream_index = int(values['stream_index'])
            pts_time = float(values['pts_time'])
            width = int(values['width'])
            height = int(values['height'])
        except (KeyError, ValueError):
            return
        base_entry = base_entries.get(stream_index)
        if base_entry is None:
            # 先頭probeに存在しない途中追加映像PIDも、全編フレームで見つけた時点から暫定区間を作る。
            base_entry = VideoStreamTimelineEntry(
                start_time=max(0.0, min(duration, pts_time - source_start_time)),
                end_time=duration,
                pid=None,
                stream_index=stream_index,
                codec='unknown',
                profile='Unknown',
                width=width,
                height=height,
                sample_aspect_ratio=_normalizeAspectRatio(values.get('sample_aspect_ratio')),
                display_aspect_ratio=_calculateDisplayAspectRatio(
                    width,
                    height,
                    _normalizeAspectRatio(values.get('sample_aspect_ratio')),
                ),
                frame_rate=0.0,
                scan_type='Unknown',
                bit_depth=8,
                color_range=None,
                color_space=None,
                color_primaries=None,
                color_transfer=None,
                mastering_display_metadata=None,
                content_light_level=None,
            )
            base_entries[stream_index] = base_entry

        relative_time = max(base_entry['start_time'], min(base_entry['end_time'], pts_time - source_start_time))
        interlaced_frame = values.get('interlaced_frame')
        scan_type: Literal['Interlaced', 'Progressive', 'Unknown']
        if interlaced_frame == '1':
            scan_type = 'Interlaced'
        elif interlaced_frame == '0':
            scan_type = 'Progressive'
        else:
            scan_type = base_entry['scan_type']
        pixel_format = values.get('pix_fmt') or ''
        sample_aspect_ratio = _normalizeAspectRatio(values.get('sample_aspect_ratio')) or \
            base_entry.get('sample_aspect_ratio')
        display_aspect_ratio = _calculateDisplayAspectRatio(width, height, sample_aspect_ratio) or \
            base_entry.get('display_aspect_ratio')
        bit_depth = 10 if '10' in pixel_format or pixel_format.startswith('p010') else 8
        color_range = values.get('color_range') or base_entry['color_range']
        color_space = values.get('color_space') or base_entry['color_space']
        color_primaries = values.get('color_primaries') or base_entry['color_primaries']
        color_transfer = values.get('color_transfer') or base_entry['color_transfer']
        mastering_display_metadata = mastering_display_metadata or base_entry['mastering_display_metadata']
        content_light_level = content_light_level or base_entry['content_light_level']
        signature = (
            width, height, sample_aspect_ratio, display_aspect_ratio, pixel_format, scan_type,
            color_range, color_space, color_primaries, color_transfer,
            json.dumps(mastering_display_metadata, sort_keys=True),
            json.dumps(content_light_level, sort_keys=True),
        )
        previous = active_states.get(stream_index)
        if previous is not None and previous[0] == signature:
            return
        if previous is not None:
            previous_entry = previous[1]
            previous_entry['end_time'] = max(previous_entry['start_time'], relative_time)
            ranges.append(previous_entry)

        entry = VideoStreamTimelineEntry(**base_entry)
        entry.update(
            start_time=base_entry['start_time'] if previous is None else relative_time,
            end_time=base_entry['end_time'],
            width=width,
            height=height,
            sample_aspect_ratio=sample_aspect_ratio,
            display_aspect_ratio=display_aspect_ratio,
            scan_type=scan_type,
            bit_depth=bit_depth,
            color_range=color_range,
            color_space=color_space,
            color_primaries=color_primaries,
            color_transfer=color_transfer,
            mastering_display_metadata=mastering_display_metadata,
            content_light_level=content_light_level,
        )
        active_states[stream_index] = (signature, entry)

    @classmethod
    def __appendAudioFrameTimelineEntry(
        cls,
        values: dict[str, str],
        duration: float,
        source_start_time: float,
        tracks_by_stream_index: dict[int, AudioTrack],
        audio_tracks: list[AudioTrack],
        active_states: dict[int, tuple[tuple[int, str], float, float, AudioTrackTimelineTrack]],
        ranges: list[tuple[float, float, AudioTrackTimelineTrack]],
    ) -> None:
        """音声frame 1件を現在の音声構成区間へ反映する。"""

        try:
            stream_index = int(values['stream_index'])
            pts_time = float(values['pts_time'])
            channels = int(values['channels'])
        except (KeyError, ValueError):
            return
        base_track = tracks_by_stream_index.get(stream_index)
        if base_track is None:
            # 先頭PMTにない途中追加音声PIDもフレーム自体から存在範囲を確定する。
            channel_layout = values.get('channel_layout')
            base_track = AudioTrack(
                index=max((int(track['index']) for track in audio_tracks), default=0) + 1,
                stream_index=stream_index,
                codec='Unknown',
                channel=_getAudioChannelLabel(channels, channel_layout),
                sampling_rate=48_000,
                language=None,
                channel_layout=channel_layout,
                is_dual_mono=False,
            )
            audio_tracks.append(base_track)
            tracks_by_stream_index[stream_index] = base_track
        previous = active_states.get(stream_index)
        raw_relative_time = pts_time - source_start_time
        pts_wrap_seconds = (1 << 33) / 90_000
        unwrap_target = previous[2] if previous is not None else 0.0
        wrap_count = round((unwrap_target - raw_relative_time) / pts_wrap_seconds)
        relative_time = min(
            (
                raw_relative_time + (wrap_count + offset) * pts_wrap_seconds
                for offset in (-1, 0, 1)
            ),
            key=lambda candidate: abs(candidate - unwrap_target),
        )
        relative_time = max(0.0, min(duration, relative_time))
        channel_layout = values.get('channel_layout') or ('mono' if channels == 1 else 'stereo')
        signature = (channels, channel_layout)
        timeline_track = cls.__buildTimelineAudioTrack(base_track, channels, channel_layout)
        if previous is None:
            active_states[stream_index] = (signature, relative_time, relative_time, timeline_track)
            return

        previous_signature, interval_start, previous_time, previous_track = previous
        sampling_rate = int(previous_track.get('sampling_rate') or 48_000)
        frame_duration = 1024 / sampling_rate
        has_gap = relative_time - previous_time > max(1.0, frame_duration * 10)
        if signature != previous_signature or has_gap:
            interval_end = min(duration, previous_time + frame_duration) if has_gap else relative_time
            ranges.append((interval_start, max(interval_start, interval_end), previous_track))
            active_states[stream_index] = (signature, relative_time, relative_time, timeline_track)
        else:
            active_states[stream_index] = (signature, interval_start, relative_time, previous_track)

    @staticmethod
    def __finalizeAudioTimeline(
        duration: float,
        audio_tracks: Sequence[AudioTrack],
        active_states: dict[int, tuple[tuple[int, str], float, float, AudioTrackTimelineTrack]],
        ranges: list[tuple[float, float, AudioTrackTimelineTrack]],
    ) -> list[AudioTrackTimelineEntry] | None:
        """音声stream別区間をTrack一覧のタイムラインへ合成する。"""

        if len(audio_tracks) == 0:
            return [AudioTrackTimelineEntry(start_time=0.0, end_time=duration, tracks=[])]
        for _, interval_start, previous_time, timeline_track in active_states.values():
            sampling_rate = int(timeline_track.get('sampling_rate') or 48_000)
            interval_end = min(duration, previous_time + (1024 / sampling_rate))
            if duration - interval_end < 0.5:
                interval_end = duration
            ranges.append((interval_start, max(interval_start, interval_end), timeline_track))
        if len(ranges) == 0:
            return None

        boundaries = sorted({0.0, duration, *(value for item in ranges for value in item[:2])})
        timeline: list[AudioTrackTimelineEntry] = []
        for index, start_time in enumerate(boundaries[:-1]):
            end_time = boundaries[index + 1]
            active_tracks = [
                track for track_start, track_end, track in ranges
                if track_start <= start_time < track_end
            ]
            active_tracks.sort(key=lambda track: int(track['index']))
            if timeline and timeline[-1]['tracks'] == active_tracks:
                timeline[-1]['end_time'] = end_time
            else:
                timeline.append(AudioTrackTimelineEntry(
                    start_time=start_time,
                    end_time=end_time,
                    tracks=active_tracks,
                ))
        return timeline

    @classmethod
    def __suppressTransientAudioTimelineChanges(
        cls,
        audio_timeline: Sequence[AudioTrackTimelineEntry],
    ) -> list[AudioTrackTimelineEntry]:
        """前後が同一で5秒以下だけ変化した音声状態を受信破損として除外する。

        実際の音声フレームだけを判定材料とし、番組名・局・EITには依存しない。
        A -> B -> A のように元の状態へ戻った場合だけ B を A で埋めるため、
        番組境界や末尾まで継続する本物の構成変更は保持する。
        """

        smoothed: list[AudioTrackTimelineEntry] = []
        for entry in audio_timeline:
            smoothed.append(AudioTrackTimelineEntry(
                start_time = entry['start_time'],
                end_time = entry['end_time'],
                tracks = list(entry['tracks']),
            ))
            # 連続する短い欠落が A-B-A-B-A の形になっても、stack上で順次畳み込む。
            while len(smoothed) >= 3:
                before, transient, after = smoothed[-3:]
                transient_duration = transient['end_time'] - transient['start_time']
                if (
                    before['tracks'] != after['tracks'] or
                    transient_duration > cls.TRANSIENT_AUDIO_CHANGE_MAX_SECONDS
                ):
                    break
                smoothed[-3:] = [AudioTrackTimelineEntry(
                    start_time = before['start_time'],
                    end_time = after['end_time'],
                    tracks = list(before['tracks']),
                )]
        return smoothed

    @staticmethod
    def __buildTimelineAudioTrack(
        base_track: AudioTrack,
        channels: int,
        channel_layout: str,
    ) -> AudioTrackTimelineTrack:
        """実フレームのchannel構成を反映した音声タイムラインTrackを作る。

        Args:
            base_track: MetadataAnalyzerが作成した論理Track。
            channels: FFprobeフレームのchannel数。
            channel_layout: FFprobeフレームのchannel layout。

        Returns:
            構成区間用に複製・補正したTrack。
        """

        timeline_track = AudioTrackTimelineTrack(**base_track)
        timeline_track['channel_layout'] = channel_layout
        if channels == 1:
            timeline_track['channel'] = 'Monaural'
            timeline_track['is_dual_mono'] = False
            language = timeline_track.get('language')
            if language is not None and '+' in language:
                timeline_track['language'] = language.split('+', maxsplit=1)[0]
        elif channels == 2 and base_track.get('is_dual_mono') is True:
            timeline_track['channel'] = 'Dual Mono'
            timeline_track['is_dual_mono'] = True
        elif channels == 2:
            timeline_track['channel'] = 'Stereo'
            timeline_track['is_dual_mono'] = False
        else:
            timeline_track['channel'] = _getAudioChannelLabel(channels, channel_layout)
            timeline_track['is_dual_mono'] = False
        return timeline_track

    @staticmethod
    async def __markFailed(recorded_video_id: int, error_code: str) -> None:
        """旧索引を壊さず、解析失敗状態と機械判定可能な理由コードを保存する。"""

        recorded_video = await RecordedVideo.get_or_none(id=recorded_video_id)
        if recorded_video is None:
            return
        # Readyの公開索引があるVersion更新では、旧Versionと全データを成功時まで維持する。
        if recorded_video.playback_index_status == 'Ready':
            await RecordedVideo.filter(id=recorded_video_id).update(
                playback_index_error_code=error_code,
            )
            return
        await RecordedVideo.filter(id=recorded_video_id).update(
            playback_index_status='Failed',
            playback_index_error_code=error_code,
        )
