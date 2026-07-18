import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from app.metadata.RecordedPlaybackIndex import (
    GetRecordedPlaybackIndexState,
    IsRecordedPlaybackIndexReady,
)
from app.metadata.RecordedPlaybackIndexer import RecordedPlaybackIndexer


def test_public_playback_index_state_exposes_stale_version() -> None:
    """DB上のReadyを維持したまま、旧VersionだけをStaleとして公開する。"""

    assert GetRecordedPlaybackIndexState('Ready', 5) == 'Stale'
    assert GetRecordedPlaybackIndexState('Ready', 6) == 'Ready'
    assert GetRecordedPlaybackIndexState('Pending', 5) == 'Pending'
    assert IsRecordedPlaybackIndexReady('Ready', 5) is False
    assert IsRecordedPlaybackIndexReady('Ready', 6) is True


def test_frame_pts_updates_monotonic_playback_index_progress() -> None:
    """全編走査のPTSを1～95%へ変換し、順序が前後しても進捗を巻き戻さない。"""

    update_progress = RecordedPlaybackIndexer._RecordedPlaybackIndexer__updateFrameProgress  # pyright: ignore[reportPrivateUsage]
    RecordedPlaybackIndexer._progress[16] = (0.01, 'Scanning')  # pyright: ignore[reportPrivateUsage]
    try:
        update_progress(16, '150.0', 100.0, 100.0)
        progress, stage = RecordedPlaybackIndexer.getProgress(16, 'Analyzing')
        assert progress == 0.48
        assert stage == 'Scanning'

        update_progress(16, '125.0', 100.0, 100.0)
        assert RecordedPlaybackIndexer.getProgress(16, 'Analyzing') == (0.48, 'Scanning')
        assert RecordedPlaybackIndexer.getProgress(16, 'Ready') == (1.0, 'Complete')
        assert RecordedPlaybackIndexer.getProgress(16, 'Failed') == (None, 'Failed')
    finally:
        RecordedPlaybackIndexer._progress.pop(16, None)  # pyright: ignore[reportPrivateUsage]


def test_backfill_includes_pending_and_stale_but_not_current_failed(monkeypatch) -> None:
    """バックフィルはPendingと旧Versionを対象にし、現行Versionの失敗を無限再試行しない。"""

    class FakeQuery:
        def order_by(self, *_fields):
            return self

        async def values_list(self, *_fields):
            return [
                (1, 'Pending', None),
                (2, 'Ready', 5),
                (3, 'Ready', 6),
                (4, 'Failed', 6),
            ]

    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.RecordedVideo.filter',
        lambda **_kwargs: FakeQuery(),
    )
    enqueued: list[tuple[int, int]] = []
    monkeypatch.setattr(
        RecordedPlaybackIndexer,
        'enqueue',
        lambda recorded_video_id, priority: enqueued.append((recorded_video_id, priority)),
    )

    asyncio.run(
        RecordedPlaybackIndexer._RecordedPlaybackIndexer__enqueuePendingBackfill()  # pyright: ignore[reportPrivateUsage]
    )

    assert enqueued == [(1, 2), (2, 2)]


def test_start_skips_only_pending_backfill_when_disabled(monkeypatch) -> None:
    """設定無効時は優先度2を投入せず、優先度0・1のenqueueは引き続き利用できる。"""

    class FakeQuery:
        async def update(self, **_kwargs) -> None:
            return None

    class RunningWorker:
        def done(self) -> bool:
            return False

    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.Config',
        lambda: SimpleNamespace(video=SimpleNamespace(recorded_playback_index_backfill_enabled=False)),
    )
    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.RecordedVideo.filter',
        lambda **_kwargs: FakeQuery(),
    )
    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.RecordedVideo.exclude',
        lambda **_kwargs: FakeQuery(),
    )
    enqueue_backfill = AsyncMock()
    monkeypatch.setattr(
        RecordedPlaybackIndexer,
        '_RecordedPlaybackIndexer__enqueuePendingBackfill',
        enqueue_backfill,
    )
    previous_worker = RecordedPlaybackIndexer._worker_task  # pyright: ignore[reportPrivateUsage]
    RecordedPlaybackIndexer._worker_task = cast(Any, RunningWorker())  # pyright: ignore[reportPrivateUsage]

    async def Run() -> None:
        await RecordedPlaybackIndexer.start()
        priority_zero = RecordedPlaybackIndexer.enqueue(1001, priority=0)
        priority_one = RecordedPlaybackIndexer.enqueue(1002, priority=1)
        assert RecordedPlaybackIndexer._queued_priorities[1001] == 0  # pyright: ignore[reportPrivateUsage]
        assert RecordedPlaybackIndexer._queued_priorities[1002] == 1  # pyright: ignore[reportPrivateUsage]
        priority_zero.cancel()
        priority_one.cancel()

    try:
        asyncio.run(Run())
        enqueue_backfill.assert_not_awaited()
    finally:
        RecordedPlaybackIndexer._worker_task = previous_worker  # pyright: ignore[reportPrivateUsage]
        RecordedPlaybackIndexer._queued_ids.clear()  # pyright: ignore[reportPrivateUsage]
        RecordedPlaybackIndexer._queued_priorities.clear()  # pyright: ignore[reportPrivateUsage]
        RecordedPlaybackIndexer._futures.clear()  # pyright: ignore[reportPrivateUsage]
        while RecordedPlaybackIndexer._queue.empty() is False:  # pyright: ignore[reportPrivateUsage]
            RecordedPlaybackIndexer._queue.get_nowait()  # pyright: ignore[reportPrivateUsage]
            RecordedPlaybackIndexer._queue.task_done()  # pyright: ignore[reportPrivateUsage]


def test_backfill_ffprobe_uses_low_io_priority_only_for_priority_two(monkeypatch) -> None:
    """nice・ioniceは既存録画バックフィルだけへ付与する。"""

    monkeypatch.setattr('app.metadata.RecordedPlaybackIndexer.shutil.which', lambda _command: '/usr/bin/tool')
    get_prefix = RecordedPlaybackIndexer._RecordedPlaybackIndexer__getFFprobeCommandPrefix  # pyright: ignore[reportPrivateUsage]
    assert get_prefix(0)[0].endswith('ffprobe8.elf')
    assert get_prefix(1)[0].endswith('ffprobe8.elf')
    assert get_prefix(2)[:8] == ['ionice', '-c', '2', '-n', '7', 'nice', '-n', '15']


def test_backfill_cache_discard_failure_is_nonfatal(monkeypatch) -> None:
    """posix_fadvise失敗を索引処理の失敗として伝播しない。"""

    async def GetRecordedVideo(**_kwargs):
        return SimpleNamespace(file_path='/recording.ts')

    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.RecordedVideo.get_or_none',
        GetRecordedVideo,
    )
    monkeypatch.setattr(os, 'open', lambda *_args: 10)
    monkeypatch.setattr(os, 'close', lambda _fd: None)

    def RaiseFadvise(*_args) -> None:
        raise OSError('test failure')

    monkeypatch.setattr(os, 'posix_fadvise', RaiseFadvise)
    asyncio.run(
        RecordedPlaybackIndexer._RecordedPlaybackIndexer__discardRecordingFileCache(1)  # pyright: ignore[reportPrivateUsage]
    )


def test_queued_analysis_skips_recording_that_became_current_ready(monkeypatch) -> None:
    """キュー待機中に現行索引が完成した録画は重複して全編走査しない。"""

    async def GetRecordedVideo(**_kwargs):
        return SimpleNamespace(playback_index_status='Ready', playback_index_version=6)

    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.RecordedVideo.get_or_none',
        GetRecordedVideo,
    )

    result = asyncio.run(
        RecordedPlaybackIndexer._RecordedPlaybackIndexer__analyze(16, 0)  # pyright: ignore[reportPrivateUsage]
    )

    assert result is True


def test_audio_timeline_keeps_pid_presence_and_applies_frame_configuration() -> None:
    """途中だけ存在するTrackを維持し、同じTrackのフレーム構成変化だけを反映する。"""

    merge = RecordedPlaybackIndexer._RecordedPlaybackIndexer__mergeAudioTimelines  # pyright: ignore[reportPrivateUsage]
    presence_timeline = [
        {'start_time': 0.0, 'end_time': 6.0, 'tracks': [{'index': 1, 'channel': 'Stereo'}]},
        {
            'start_time': 6.0,
            'end_time': 12.0,
            'tracks': [{'index': 1, 'channel': 'Stereo'}, {'index': 2, 'channel': 'Stereo', 'pid': 0x113}],
        },
        {'start_time': 12.0, 'end_time': 18.0, 'tracks': [{'index': 1, 'channel': 'Stereo'}]},
    ]
    frame_timeline = [
        {'start_time': 0.0, 'end_time': 9.0, 'tracks': [{'index': 1, 'channel': 'Monaural'}]},
        {'start_time': 9.0, 'end_time': 18.0, 'tracks': [{'index': 1, 'channel': 'Dual Mono'}]},
    ]

    assert merge(18.0, presence_timeline, frame_timeline) == [
        {'start_time': 0.0, 'end_time': 6.0, 'tracks': [{'index': 1, 'channel': 'Monaural'}]},
        {
            'start_time': 6.0,
            'end_time': 9.0,
            'tracks': [{'index': 1, 'channel': 'Monaural'}, {'index': 2, 'channel': 'Stereo', 'pid': 0x113}],
        },
        {
            'start_time': 9.0,
            'end_time': 12.0,
            'tracks': [{'index': 1, 'channel': 'Dual Mono'}, {'index': 2, 'channel': 'Stereo', 'pid': 0x113}],
        },
        {'start_time': 12.0, 'end_time': 18.0, 'tracks': [{'index': 1, 'channel': 'Dual Mono'}]},
    ]


def test_subtitle_backfill_includes_probed_and_bin_data_arib_tracks(monkeypatch) -> None:
    """既存DBが空でも通常字幕とPMT確認済みbin_data字幕を両方補完する。"""

    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.TSKeyFrameSeeker.findARIBCaptionPIDs',
        lambda _path: {0x0138},
    )
    recorded_video = SimpleNamespace(subtitle_tracks=[], container_format='MPEG-TS')
    probe = {'streams': [
        {
            'index': 2,
            'id': '0x130',
            'codec_type': 'subtitle',
            'codec_name': 'arib_caption',
            'tags': {'language': 'jpn'},
        },
        {
            'index': 3,
            'id': '0x138',
            'codec_type': 'data',
            'codec_name': 'bin_data',
        },
    ]}

    tracks = RecordedPlaybackIndexer._RecordedPlaybackIndexer__backfillARIBSubtitleTracks(  # pyright: ignore[reportPrivateUsage]
        recorded_video,
        probe,
        Path('/recording.ts'),
    )

    assert tracks == [
        {
            'index': 1,
            'stream_index': 2,
            'codec': 'arib_caption',
            'language': 'jpn',
            'title': None,
            'pid': 0x0130,
        },
        {
            'index': 2,
            'stream_index': 3,
            'codec': 'arib_caption',
            'language': None,
            'title': None,
            'pid': 0x0138,
        },
    ]
