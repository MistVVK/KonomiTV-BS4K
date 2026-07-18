from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal, cast

from app import logging
from app.config import Config
from app.constants import JST, LIBRARY_PATH
from app.metadata.RecordedPlaybackIndex import (
    RECORDED_PLAYBACK_INDEX_VERSION,
    IsRecordedPlaybackIndexReady,
    RecordedPlaybackIndexStage,
    RecordedPlaybackIndexState,
)
from app.models.RecordedVideo import RecordedVideo
from app.schemas import (
    AudioTrack,
    AudioTrackTimelineEntry,
    AudioTrackTimelineTrack,
    SubtitleTrack,
    VideoStreamTimelineEntry,
)
from app.utils.TSKeyFrameSeeker import TSKeyFrameSeeker


class RecordedPlaybackIndexer:
    """録画再生用の映像・音声タイムラインを優先度付き単一ワーカーで生成する。"""

    INDEX_VERSION: ClassVar[int] = RECORDED_PLAYBACK_INDEX_VERSION
    _queue: ClassVar[asyncio.PriorityQueue[tuple[Literal[0, 1, 2], int, int]]] = asyncio.PriorityQueue()
    _queued_ids: ClassVar[set[int]] = set()
    _queued_priorities: ClassVar[dict[int, int]] = {}
    _futures: ClassVar[dict[int, asyncio.Future[bool]]] = {}
    _sequence: ClassVar[int] = 0
    _worker_task: ClassVar[asyncio.Task[None] | None] = None
    _progress: ClassVar[dict[int, tuple[float, RecordedPlaybackIndexStage]]] = {}

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
        if index_state in ('Pending', 'Stale'):
            return 0.0, 'Queued'
        return cls._progress.get(recorded_video_id, (0.0, 'Probing'))

    @classmethod
    async def start(cls) -> None:
        """中断状態を復旧し、既存録画のバックフィルを開始する。"""

        await RecordedVideo.filter(playback_index_status='Analyzing').update(playback_index_status='Pending')
        if cls._worker_task is None or cls._worker_task.done():
            cls._worker_task = asyncio.create_task(cls.__worker())
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
            if index_status == 'Pending' or index_version != cls.INDEX_VERSION:
                cls.enqueue(recorded_video_id, priority=2)

    @classmethod
    async def stop(cls) -> None:
        """バックグラウンドワーカーを停止する。"""

        if cls._worker_task is None:
            return
        cls._worker_task.cancel()
        try:
            await cls._worker_task
        except asyncio.CancelledError:
            pass
        cls._worker_task = None

    @classmethod
    def enqueue(cls, recorded_video_id: int, priority: Literal[0, 1, 2]) -> asyncio.Future[bool]:
        """録画を解析キューへ追加し、同一録画の要求を既存Futureへ合流する。

        Args:
            recorded_video_id: RecordedVideoのID。
            priority: 0=再生要求、1=録画完了、2=バックフィル。

        Returns:
            解析完了時に成功可否を返す共有Future。
        """

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
                result = await cls.__analyze(recorded_video_id, priority)
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
                await RecordedVideo.filter(id=recorded_video_id).update(
                    playback_index_status = 'Failed',
                    playback_index_error_code = 'UnexpectedError',
                )
                if future is not None and future.done() is False:
                    future.set_result(False)
            finally:
                cls._progress.pop(recorded_video_id, None)
                if priority == 2:
                    await cls.__discardRecordingFileCache(recorded_video_id)
                cls._queue.task_done()
                cls._futures.pop(recorded_video_id, None)

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
    async def __analyze(cls, recorded_video_id: int, priority: Literal[0, 1, 2]) -> bool:
        """FFprobe 8から録画1件の映像タイムラインを生成する。"""

        recorded_video = await RecordedVideo.get_or_none(id=recorded_video_id)
        if recorded_video is None:
            return False
        # キュー投入後に別要求や手動復旧で現行索引が完成している場合は、同じ録画を再走査しない。
        # 優先度昇格時の古いキュー要素はworker側で除外するが、DB更新との競合にもここで備える。
        if IsRecordedPlaybackIndexReady(
            recorded_video.playback_index_status,
            recorded_video.playback_index_version,
        ):
            return True
        cls._progress[recorded_video_id] = (0.0, 'Probing')
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
            '-show_format',
            '-of',
            'json',
            str(file_path),
            stdout = asyncio.subprocess.PIPE,
            stderr = asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            logging.warning(
                '[RecordedPlaybackIndexer] FFprobe 8 failed. '
                f'[recorded_video_id: {recorded_video_id}, stderr: {stderr.decode(errors="ignore").strip()}]'
            )
            await cls.__markFailed(recorded_video_id, 'ProbeFailed')
            return False
        try:
            probe = cast(dict[str, Any], json.loads(stdout))
            timeline = cls.__buildTimeline(probe, recorded_video.duration)
        except (json.JSONDecodeError, TypeError, ValueError):
            await cls.__markFailed(recorded_video_id, 'ProbeFailed')
            return False
        if recorded_video.has_video is True and len(timeline) == 0:
            await cls.__markFailed(recorded_video_id, 'VideoStreamUnavailable')
            return False

        # show_streamsだけでは同じPID/stream index内の解像度・走査方式・色特性変更を
        # 時系列化できない。キーフレームを順次読み、構成が変わる境界だけを区間として残す。
        source_start_time = float(probe.get('format', {}).get('start_time') or 0.0)
        refined_timeline, audio_timeline = await cls.__buildFrameTimelines(
            recorded_video_id,
            file_path,
            recorded_video.duration,
            source_start_time,
            timeline,
            recorded_video.audio_tracks,
            priority,
        )
        if refined_timeline is not None:
            timeline = refined_timeline
        cls._progress[recorded_video_id] = (0.96, 'Finalizing')
        # 録画スキャン時の音声タイムラインは TS パケットの PID を全編走査しており、
        # FFprobe が先頭の PMT に存在しない途中追加 PID を列挙できない場合も存在区間を保持している。
        # その区間を正として、FFprobe のフレーム解析結果はチャンネル構成の補正だけに利用する。
        audio_timeline = cls.__mergeAudioTimelines(
            recorded_video.duration,
            recorded_video.audio_track_timeline,
            audio_timeline,
        )
        subtitle_tracks = cls.__backfillARIBSubtitleTracks(recorded_video, probe, file_path)

        await RecordedVideo.filter(id=recorded_video_id).update(
            playback_index_status = 'Ready',
            playback_index_version = cls.INDEX_VERSION,
            playback_indexed_at = datetime.now(tz=JST),
            playback_index_error_code = None,
            video_stream_timeline = timeline,
            audio_track_timeline = audio_timeline,
            subtitle_tracks = subtitle_tracks,
        )
        return True

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
    def __backfillARIBSubtitleTracks(
        recorded_video: RecordedVideo,
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
        try:
            caption_pids = TSKeyFrameSeeker.findARIBCaptionPIDs(file_path)
        except (OSError, ValueError):
            return subtitle_tracks
        known_stream_indexes = {track['stream_index'] for track in subtitle_tracks}
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
            if pid not in caption_pids:
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
        return subtitle_tracks

    @staticmethod
    def __buildTimeline(probe: dict[str, Any], fallback_duration: float) -> list[VideoStreamTimelineEntry]:
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
            try:
                pid = int(str(stream['id']), 0) if stream.get('id') is not None else None
            except ValueError:
                pid = None
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
                width = int(stream.get('width') or 0),
                height = int(stream.get('height') or 0),
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
        priority: Literal[0, 1, 2],
    ) -> tuple[list[VideoStreamTimelineEntry] | None, list[AudioTrackTimelineEntry] | None]:
        """1回の全編走査から映像キーフレームと全音声フレームを索引化する。

        ``-skip_frame:v nokey`` は映像だけへ適用されるため、映像はキーフレームに
        絞りながら、音声構成変化の検出に必要な全音声フレームを同時に取得できる。

        Returns:
            映像タイムラインと音声タイムライン。解析不能な場合は両方None。
        """

        process = await asyncio.create_subprocess_exec(
            *cls.__getFFprobeCommandPrefix(priority),
            '-v', 'error',
            '-skip_frame:v', 'nokey',
            '-show_frames',
            '-show_entries',
            'frame=media_type,stream_index,pts_time,width,height,pix_fmt,interlaced_frame,'
            'color_range,color_space,color_primaries,color_transfer,channels,channel_layout:'
            'frame_side_data=side_data_type,red_x,red_y,green_x,green_y,blue_x,blue_y,'
            'white_point_x,white_point_y,min_luminance,max_luminance,max_content,max_average',
            '-of', 'compact=p=1:nk=0',
            str(file_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None

        base_entries = {entry['stream_index']: entry for entry in stream_timeline}
        video_active_states: dict[int, tuple[tuple[object, ...], VideoStreamTimelineEntry]] = {}
        video_ranges: list[VideoStreamTimelineEntry] = []
        tracks_by_stream_index = {
            int(track.get('stream_index', track['index'])): track
            for track in audio_tracks
        }
        audio_active_states: dict[int, tuple[tuple[int, str], float, float, AudioTrackTimelineTrack]] = {}
        audio_ranges: list[tuple[float, float, AudioTrackTimelineTrack]] = []
        cls._progress[recorded_video_id] = (0.01, 'Scanning')

        async for raw_line in process.stdout:
            sections = raw_line.decode(errors='ignore').strip().split('|side_data|')
            values = {
                key: value
                for item in sections[0].split('|')
                if '=' in item
                for key, value in [item.split('=', maxsplit=1)]
            }
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
                    audio_active_states,
                    audio_ranges,
                )

        stderr = await process.stderr.read() if process.stderr is not None else b''
        return_code = await process.wait()
        if return_code != 0:
            logging.warning(
                '[RecordedPlaybackIndexer] FFprobe 8 frame analysis failed. '
                f'[file_path: {file_path}, stderr: {stderr.decode(errors="ignore").strip()}]'
            )
            return None, None

        video_ranges.extend(entry for _, entry in video_active_states.values())
        video_timeline = sorted(
            video_ranges,
            key=lambda entry: (entry['start_time'], entry['stream_index']),
        ) if video_ranges else None
        audio_timeline = cls.__finalizeAudioTimeline(
            duration,
            audio_tracks,
            audio_active_states,
            audio_ranges,
        )
        return video_timeline, audio_timeline

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

    @staticmethod
    def __appendVideoFrameTimelineEntry(
        values: dict[str, str],
        side_data_sections: Sequence[str],
        source_start_time: float,
        base_entries: dict[int, VideoStreamTimelineEntry],
        active_states: dict[int, tuple[tuple[object, ...], VideoStreamTimelineEntry]],
        ranges: list[VideoStreamTimelineEntry],
    ) -> None:
        """映像frame 1件を現在の映像構成区間へ反映する。"""

        mastering_display_metadata: dict[str, object] | None = None
        content_light_level: dict[str, object] | None = None
        for side_data_section in side_data_sections:
            side_data = {
                key: value
                for item in side_data_section.split('|')
                if '=' in item
                for key, value in [item.split('=', maxsplit=1)]
            }
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
            return

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
        bit_depth = 10 if '10' in pixel_format or pixel_format.startswith('p010') else 8
        color_range = values.get('color_range') or base_entry['color_range']
        color_space = values.get('color_space') or base_entry['color_space']
        color_primaries = values.get('color_primaries') or base_entry['color_primaries']
        color_transfer = values.get('color_transfer') or base_entry['color_transfer']
        mastering_display_metadata = mastering_display_metadata or base_entry['mastering_display_metadata']
        content_light_level = content_light_level or base_entry['content_light_level']
        signature = (
            width, height, pixel_format, scan_type,
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
            return
        relative_time = max(0.0, min(duration, pts_time - source_start_time))
        channel_layout = values.get('channel_layout') or ('mono' if channels == 1 else 'stereo')
        signature = (channels, channel_layout)
        timeline_track = cls.__buildTimelineAudioTrack(base_track, channels, channel_layout)
        previous = active_states.get(stream_index)
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
            timeline_track['channel'] = f'{channels} Channels'
            timeline_track['is_dual_mono'] = False
        return timeline_track

    @staticmethod
    async def __markFailed(recorded_video_id: int, error_code: str) -> None:
        """解析失敗状態と機械判定可能な理由コードを保存する。"""

        await RecordedVideo.filter(id=recorded_video_id).update(
            playback_index_status = 'Failed',
            playback_index_version = RecordedPlaybackIndexer.INDEX_VERSION,
            playback_indexed_at = datetime.now(tz=JST),
            playback_index_error_code = error_code,
            video_stream_timeline = None,
        )
