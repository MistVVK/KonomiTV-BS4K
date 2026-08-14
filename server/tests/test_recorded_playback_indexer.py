import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.metadata.RecordedPlaybackIndex import (
    RECORDED_PLAYBACK_INDEX_VERSION,
    GetRecordedPlaybackIndexState,
    IsRecordedPlaybackIndexReady,
)
from app.metadata.RecordedPlaybackIndexer import RecordedPlaybackIndexer
from app.utils.TSKeyFrameSeeker import ARIBTTMLStreamInfo


def test_public_playback_index_state_exposes_stale_version() -> None:
    """DB上のReadyを維持したまま、旧VersionだけをStaleとして公開する。"""

    assert GetRecordedPlaybackIndexState('Ready', RECORDED_PLAYBACK_INDEX_VERSION - 1) == 'Stale'
    assert GetRecordedPlaybackIndexState('Ready', RECORDED_PLAYBACK_INDEX_VERSION) == 'Ready'
    assert GetRecordedPlaybackIndexState('Pending', 5) == 'Pending'
    assert IsRecordedPlaybackIndexReady('Ready', 5) is False
    assert IsRecordedPlaybackIndexReady('Ready', RECORDED_PLAYBACK_INDEX_VERSION) is True


def test_initial_probe_cancel_kills_and_drains_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """initial FFprobe の communicate 待機中にworkerをcancelしても子processを残さない。"""

    class FakeQuery:
        async def update(self, **_kwargs) -> None:
            return None

    class BlockingProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.communicate_count = 0
            self.kill_count = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            self.communicate_count += 1
            if self.communicate_count == 1:
                await asyncio.Future()
            return b'', b''

        def kill(self) -> None:
            self.kill_count += 1
            self.returncode = -9

    process = BlockingProcess()
    recorded_video = SimpleNamespace(
        id=1,
        file_path='/recording.ts',
        container_format='MPEG-TS',
        playback_index_status='Pending',
        playback_index_version=None,
        duration=60.0,
        has_video=True,
    )
    monkeypatch.setattr(Path, 'is_file', lambda _path: True)
    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.RecordedVideo.get_or_none',
        AsyncMock(return_value=recorded_video),
    )
    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.RecordedVideo.filter',
        lambda **_kwargs: FakeQuery(),
    )
    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.asyncio.create_subprocess_exec',
        AsyncMock(return_value=process),
    )

    async def Verify() -> None:
        analysis = asyncio.create_task(
            RecordedPlaybackIndexer._RecordedPlaybackIndexer__analyze(  # pyright: ignore[reportPrivateUsage]
                1,
                0,
            ),
        )
        await asyncio.sleep(0)
        analysis.cancel()
        with pytest.raises(asyncio.CancelledError):
            await analysis
        assert process.kill_count == 1
        assert process.communicate_count == 2

    asyncio.run(Verify())


def test_missing_audio_probe_cancel_kills_drains_and_waits_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """missing-audio packet probe のreadline待機中もprocess・stderr taskを回収する。"""

    class BlockingStdout:
        def __init__(self) -> None:
            self.read_count = 0

        async def readline(self) -> bytes:
            await asyncio.Future()
            return b''

        async def read(self) -> bytes:
            self.read_count += 1
            return b''

    class EmptyStderr:
        async def read(self, _size: int = -1) -> bytes:
            return b''

    class BlockingProcess:
        def __init__(self) -> None:
            self.stdout = BlockingStdout()
            self.stderr = EmptyStderr()
            self.returncode: int | None = None
            self.kill_count = 0
            self.wait_count = 0

        def kill(self) -> None:
            self.kill_count += 1
            self.returncode = -9

        async def wait(self) -> int:
            self.wait_count += 1
            return self.returncode or 0

    process = BlockingProcess()
    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.asyncio.create_subprocess_exec',
        AsyncMock(return_value=process),
    )
    track = {
        'index': 1,
        'stream_index': 1,
        'codec': 'AAC-LC',
        'channel': 'Stereo',
        'sampling_rate': 48_000,
        'language': 'ja',
    }

    async def Verify() -> None:
        probe = asyncio.create_task(
            RecordedPlaybackIndexer._RecordedPlaybackIndexer__probeMissingAudioPacketRanges(  # pyright: ignore[reportPrivateUsage]
                1,
                Path('/recording.ts'),
                60.0,
                0.0,
                {1},
                {1: cast(Any, track)},
                0,
            ),
        )
        await asyncio.sleep(0)
        probe.cancel()
        with pytest.raises(asyncio.CancelledError):
            await probe
        assert process.kill_count == 1
        assert process.wait_count == 1
        assert process.stdout.read_count == 1

    asyncio.run(Verify())


def test_audio_only_probe_is_indexed_without_video_timeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """音声のみ録画は空の映像timelineを失敗扱いせず、音声索引をReadyとして保存する。"""

    track = {
        'index': 1,
        'stream_index': 0,
        'codec': 'AAC-LC',
        'channel': 'Stereo',
        'sampling_rate': 48_000,
        'language': 'ja',
        'channel_layout': 'stereo',
        'is_dual_mono': False,
    }
    audio_timeline = [{
        'start_time': 0.0,
        'end_time': 6.0,
        'tracks': [track],
    }]
    recorded_video = SimpleNamespace(
        id=1,
        recorded_program_id=None,
        playback_index_status='Pending',
        playback_index_version=None,
        file_path='/recording.m4a',
        duration=6.0,
        has_video=False,
        audio_tracks=[track],
        audio_track_timeline=audio_timeline,
        subtitle_tracks=[],
        container_format='MP4',
    )
    updates: list[dict[str, Any]] = []

    class FakeQuery:
        async def update(self, **kwargs) -> None:
            updates.append(kwargs)

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return json.dumps({
                'format': {'start_time': '0.0'},
                'programs': [],
                'streams': [{
                    'index': 0,
                    'codec_type': 'audio',
                    'codec_name': 'aac',
                    'profile': 'LC',
                    'channels': 2,
                    'channel_layout': 'stereo',
                    'sample_rate': '48000',
                    'tags': {'language': 'ja'},
                }],
            }).encode(), b''

    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.RecordedVideo.get_or_none',
        AsyncMock(return_value=recorded_video),
    )
    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.RecordedVideo.filter',
        lambda **_kwargs: FakeQuery(),
    )
    monkeypatch.setattr(Path, 'is_file', lambda _path: True)
    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.asyncio.create_subprocess_exec',
        AsyncMock(return_value=FakeProcess()),
    )
    monkeypatch.setattr(
        RecordedPlaybackIndexer,
        '_RecordedPlaybackIndexer__buildFrameTimelines',
        AsyncMock(return_value=([], audio_timeline, [track])),
    )

    result = asyncio.run(
        RecordedPlaybackIndexer._RecordedPlaybackIndexer__analyze(1, 0)  # pyright: ignore[reportPrivateUsage]
    )

    assert result is True
    assert updates[-1]['playback_index_status'] == 'Ready'
    assert updates[-1]['video_stream_timeline'] == []
    assert updates[-1]['video_codec'] is None
    assert updates[-1]['audio_tracks'] == [track]
    assert updates[-1]['audio_track_timeline'] == audio_timeline


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
                (2, 'Ready', RECORDED_PLAYBACK_INDEX_VERSION - 1),
                (3, 'Ready', RECORDED_PLAYBACK_INDEX_VERSION),
                (4, 'Failed', RECORDED_PLAYBACK_INDEX_VERSION),
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
    history_start = AsyncMock(return_value=None)
    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.AnalysisTaskTracker.start',
        history_start,
    )
    previous_workers = RecordedPlaybackIndexer._worker_tasks  # pyright: ignore[reportPrivateUsage]
    previous_worker_count = RecordedPlaybackIndexer.WORKER_COUNT
    RecordedPlaybackIndexer._worker_tasks = {cast(Any, RunningWorker())}  # pyright: ignore[reportPrivateUsage]
    RecordedPlaybackIndexer.WORKER_COUNT = 1

    async def Run() -> None:
        await RecordedPlaybackIndexer.start()
        priority_zero = RecordedPlaybackIndexer.enqueue(1001, priority=0)
        priority_one = RecordedPlaybackIndexer.enqueue(1002, priority=1)
        assert RecordedPlaybackIndexer._queued_priorities[1001] == 0  # pyright: ignore[reportPrivateUsage]
        assert RecordedPlaybackIndexer._queued_priorities[1002] == 1  # pyright: ignore[reportPrivateUsage]
        priority_zero.cancel()
        priority_one.cancel()
        # enqueue() が生成する履歴 Task は worker が回収する。本テストでは worker を fake に
        # 差し替えているため、テスト自身が join して event loop 終了後へ持ち越さない。
        history_tasks = [
            RecordedPlaybackIndexer._history_handle_tasks.pop(recorded_video_id)  # pyright: ignore[reportPrivateUsage]
            for recorded_video_id in (1001, 1002)
        ]
        await asyncio.gather(*history_tasks)

    try:
        asyncio.run(Run())
        enqueue_backfill.assert_not_awaited()
        assert history_start.await_count == 2
    finally:
        RecordedPlaybackIndexer._worker_tasks = previous_workers  # pyright: ignore[reportPrivateUsage]
        RecordedPlaybackIndexer.WORKER_COUNT = previous_worker_count
        RecordedPlaybackIndexer._queued_ids.clear()  # pyright: ignore[reportPrivateUsage]
        RecordedPlaybackIndexer._queued_priorities.clear()  # pyright: ignore[reportPrivateUsage]
        RecordedPlaybackIndexer._futures.clear()  # pyright: ignore[reportPrivateUsage]
        RecordedPlaybackIndexer._history_handle_tasks.clear()  # pyright: ignore[reportPrivateUsage]
        while RecordedPlaybackIndexer._queue.empty() is False:  # pyright: ignore[reportPrivateUsage]
            RecordedPlaybackIndexer._queue.get_nowait()  # pyright: ignore[reportPrivateUsage]
            RecordedPlaybackIndexer._queue.task_done()  # pyright: ignore[reportPrivateUsage]


def test_playback_request_promotes_queued_backfill(monkeypatch: pytest.MonkeyPatch) -> None:
    """未生成・旧Versionを問わず、選択された待機中録画を再生要求の最優先へ昇格する。"""

    history_start = AsyncMock(return_value=None)
    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.AnalysisTaskTracker.start',
        history_start,
    )

    async def Run() -> None:
        backfill_future = RecordedPlaybackIndexer.enqueue(1001, priority=2)
        playback_future = RecordedPlaybackIndexer.enqueue(1001, priority=0)

        assert playback_future is backfill_future
        assert RecordedPlaybackIndexer._queued_priorities[1001] == 0  # pyright: ignore[reportPrivateUsage]
        playback_future.cancel()
        # worker を起動しないキュー操作単体テストなので、enqueue() が生成した履歴 Task は
        # ここで明示的に回収し、別テストの event loop へ副作用を漏らさない。
        history_task = RecordedPlaybackIndexer._history_handle_tasks.pop(1001)  # pyright: ignore[reportPrivateUsage]
        await history_task

    try:
        asyncio.run(Run())
        history_start.assert_awaited_once()
    finally:
        RecordedPlaybackIndexer._queued_ids.clear()  # pyright: ignore[reportPrivateUsage]
        RecordedPlaybackIndexer._queued_priorities.clear()  # pyright: ignore[reportPrivateUsage]
        RecordedPlaybackIndexer._futures.clear()  # pyright: ignore[reportPrivateUsage]
        RecordedPlaybackIndexer._history_handle_tasks.clear()  # pyright: ignore[reportPrivateUsage]
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
        return SimpleNamespace(
            playback_index_status='Ready',
            playback_index_version=RECORDED_PLAYBACK_INDEX_VERSION,
        )

    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.RecordedVideo.get_or_none',
        GetRecordedVideo,
    )

    result = asyncio.run(
        RecordedPlaybackIndexer._RecordedPlaybackIndexer__analyze(16, 0)  # pyright: ignore[reportPrivateUsage]
    )

    assert result is True


def test_initial_probe_timeout_marks_index_failed(monkeypatch) -> None:
    """軽量probeも総時間で打ち切り、全編走査やReady保存へ進めない。"""

    recorded_video = SimpleNamespace(
        playback_index_status='Pending',
        playback_index_version=None,
        file_path='/recording.ts',
        duration=120.0,
        has_video=True,
        audio_tracks=[],
        audio_track_timeline=[],
        subtitle_tracks=[],
        container_format='MPEG-TS',
    )

    async def GetRecordedVideo(**_kwargs):
        return recorded_video

    class FakeQuery:
        async def update(self, **_kwargs) -> None:
            return None

    class FakeProcess:
        communicate_count = 0

        async def communicate(self):
            self.communicate_count += 1
            if self.communicate_count == 1:
                await asyncio.sleep(1)
            return b'', b''

        def kill(self) -> None:
            return None

    async def CreateFakeProcess(*_args, **_kwargs):
        return FakeProcess()

    mark_failed = AsyncMock()
    monkeypatch.setattr('app.metadata.RecordedPlaybackIndexer.RecordedVideo.get_or_none', GetRecordedVideo)
    monkeypatch.setattr('app.metadata.RecordedPlaybackIndexer.RecordedVideo.filter', lambda **_kwargs: FakeQuery())
    monkeypatch.setattr('app.metadata.RecordedPlaybackIndexer.Path.is_file', lambda _path: True)
    monkeypatch.setattr('app.metadata.RecordedPlaybackIndexer.asyncio.create_subprocess_exec', CreateFakeProcess)
    monkeypatch.setattr(
        RecordedPlaybackIndexer,
        '_RecordedPlaybackIndexer__markFailed',
        mark_failed,
    )
    monkeypatch.setattr(RecordedPlaybackIndexer, 'INITIAL_PROBE_TIMEOUT_SECONDS', 0.001)

    result = asyncio.run(
        RecordedPlaybackIndexer._RecordedPlaybackIndexer__analyze(16, 0)  # pyright: ignore[reportPrivateUsage]
    )

    assert result is False
    mark_failed.assert_awaited_once_with(16, 'ProbeTimeout')


def test_initial_probe_requests_program_membership(monkeypatch) -> None:
    """複数service TSの字幕を絞り込めるよう、実probeでprogram構成も取得する。"""

    recorded_video = SimpleNamespace(
        playback_index_status='Pending',
        playback_index_version=None,
        file_path='/recording.ts',
        container_format='MPEG-TS',
        duration=120.0,
        has_video=True,
    )

    async def GetRecordedVideo(**_kwargs):
        return recorded_video

    class FakeQuery:
        async def update(self, **_kwargs) -> None:
            return None

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            # probe引数の検証後は、不正JSONで後続の全編走査へ進ませない。
            return b'invalid json', b''

    command: tuple[str, ...] | None = None

    async def CreateFakeProcess(*args, **_kwargs):
        nonlocal command
        command = args
        return FakeProcess()

    mark_failed = AsyncMock()
    monkeypatch.setattr('app.metadata.RecordedPlaybackIndexer.RecordedVideo.get_or_none', GetRecordedVideo)
    monkeypatch.setattr('app.metadata.RecordedPlaybackIndexer.RecordedVideo.filter', lambda **_kwargs: FakeQuery())
    monkeypatch.setattr('app.metadata.RecordedPlaybackIndexer.Path.is_file', lambda _path: True)
    monkeypatch.setattr('app.metadata.RecordedPlaybackIndexer.asyncio.create_subprocess_exec', CreateFakeProcess)
    monkeypatch.setattr(
        RecordedPlaybackIndexer,
        '_RecordedPlaybackIndexer__markFailed',
        mark_failed,
    )

    result = asyncio.run(
        RecordedPlaybackIndexer._RecordedPlaybackIndexer__analyze(16, 0)  # pyright: ignore[reportPrivateUsage]
    )

    assert result is False
    assert command is not None
    assert command[command.index('-show_streams') + 1] == '-show_programs'
    mark_failed.assert_awaited_once_with(16, 'ProbeFailed')


def test_mmt_tlv_initial_probe_forces_libaribtlv(monkeypatch) -> None:
    """MMT/TLV 索引の全 probe 入口で libaribtlv demuxer を明示する。"""

    recorded_video = SimpleNamespace(
        playback_index_status='Pending',
        playback_index_version=None,
        file_path='/recording.tlv',
        container_format='MMT/TLV',
        duration=120.0,
        has_video=True,
    )

    class FakeQuery:
        async def update(self, **_kwargs) -> None:
            return None

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b'invalid json', b''

    command: tuple[str, ...] | None = None

    async def CreateFakeProcess(*args, **_kwargs):
        nonlocal command
        command = args
        return FakeProcess()

    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.RecordedVideo.get_or_none',
        AsyncMock(return_value=recorded_video),
    )
    monkeypatch.setattr('app.metadata.RecordedPlaybackIndexer.RecordedVideo.filter', lambda **_kwargs: FakeQuery())
    monkeypatch.setattr('app.metadata.RecordedPlaybackIndexer.Path.is_file', lambda _path: True)
    monkeypatch.setattr('app.metadata.RecordedPlaybackIndexer.asyncio.create_subprocess_exec', CreateFakeProcess)
    mark_failed = AsyncMock()
    monkeypatch.setattr(
        RecordedPlaybackIndexer,
        '_RecordedPlaybackIndexer__markFailed',
        mark_failed,
    )

    assert asyncio.run(
        RecordedPlaybackIndexer._RecordedPlaybackIndexer__analyze(16, 0)  # pyright: ignore[reportPrivateUsage]
    ) is False
    assert command is not None
    input_index = command.index('/recording.tlv')
    assert command[input_index - 2:input_index] == ('-f', 'libaribtlv')


def test_mmt_tlv_audio_tracks_use_stream_indexes_without_packet_id() -> None:
    """MMT packet_id を MPEG-TS PID と誤認せず、FFprobe stream index だけを保持する。"""

    normalize = RecordedPlaybackIndexer._RecordedPlaybackIndexer__normalizeAudioTracks  # pyright: ignore[reportPrivateUsage]
    tracks = normalize(
        [{
            'index': 1,
            'stream_index': 2,
            'pid': 0x111,
            'codec': 'AAC-LC',
            'channel': 'Stereo',
            'sampling_rate': 48_000,
            'language': 'jpn',
        }],
        {'streams': [{
            'index': 2,
            'id': '0x8001',
            'codec_type': 'audio',
            'codec_name': 'aac',
            'channels': 2,
            'sample_rate': '48000',
        }]},
        use_stream_ids_as_pids=False,
    )

    assert tracks[0]['stream_index'] == 2
    assert 'pid' not in tracks[0]


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


def test_audio_track_reconciliation_rejects_eit_only_dual_mono() -> None:
    """EITにDual Mono候補があっても、全実フレームがmonoならMonauralへ戻す。"""

    reconcile = RecordedPlaybackIndexer._RecordedPlaybackIndexer__reconcileAudioTracks  # pyright: ignore[reportPrivateUsage]
    audio_tracks = [{
        'index': 1,
        'codec': 'AAC-LC',
        'channel': 'Dual Mono',
        'sampling_rate': 48_000,
        'language': '日本語+英語',
        'stream_index': 1,
        'channel_layout': 'mono',
        'is_dual_mono': True,
    }]
    monaural_timeline = [{
        'start_time': 0.0,
        'end_time': 600.0,
        'tracks': [{
            **audio_tracks[0],
            'channel': 'Monaural',
            'language': '日本語',
            'is_dual_mono': False,
        }],
    }]

    reconciled = reconcile(audio_tracks, monaural_timeline)

    assert reconciled[0]['channel'] == 'Monaural'
    assert reconciled[0]['language'] == '日本語'
    assert reconciled[0]['is_dual_mono'] is False


def test_late_secondary_audio_track_receives_eit_language_after_full_scan() -> None:
    """先頭probe後に見つかったTrack 2へ、索引確定時に副音声言語を補完する。"""

    apply_languages = (
        RecordedPlaybackIndexer._RecordedPlaybackIndexer__applyRecordedProgramAudioLanguages  # pyright: ignore[reportPrivateUsage]
    )
    audio_tracks = [
        {
            'index': 1,
            'codec': 'AAC-LC',
            'channel': 'Stereo',
            'sampling_rate': 48_000,
            'language': '日本語',
            'stream_index': 2,
            'pid': 0x112,
        },
        {
            'index': 2,
            'codec': 'AAC-LC',
            'channel': 'Stereo',
            'sampling_rate': 48_000,
            'language': None,
            'stream_index': 3,
            'pid': 0x113,
        },
    ]
    audio_timeline = [
        {'start_time': 0.0, 'end_time': 97.0, 'tracks': [audio_tracks[0]]},
        {'start_time': 97.0, 'end_time': 1746.0, 'tracks': audio_tracks},
        {'start_time': 1746.0, 'end_time': 1810.0, 'tracks': [audio_tracks[0]]},
    ]

    completed_tracks, completed_timeline = apply_languages(
        audio_tracks,
        audio_timeline,
        '日本語',
        '日本語',
    )

    assert completed_tracks[1]['language'] == '日本語'
    assert completed_timeline[1]['tracks'][1]['language'] == '日本語'
    assert completed_timeline[1]['start_time'] == 97.0
    assert completed_timeline[1]['end_time'] == 1746.0
    assert completed_timeline[1]['tracks'][1]['pid'] == 0x113


def test_explicit_stream_language_takes_priority_over_eit_language() -> None:
    """FFprobeが返した言語タグは、EITの主・副音声言語で上書きしない。"""

    apply_languages = (
        RecordedPlaybackIndexer._RecordedPlaybackIndexer__applyRecordedProgramAudioLanguages  # pyright: ignore[reportPrivateUsage]
    )
    audio_tracks = [
        {
            'index': 1,
            'codec': 'AAC-LC',
            'channel': 'Stereo',
            'sampling_rate': 48_000,
            'language': '日本語',
        },
        {
            'index': 2,
            'codec': 'AAC-LC',
            'channel': 'Stereo',
            'sampling_rate': 48_000,
            'language': '英語',
        },
    ]
    audio_timeline = [{
        'start_time': 0.0,
        'end_time': 60.0,
        'tracks': audio_tracks,
    }]

    completed_tracks, completed_timeline = apply_languages(
        audio_tracks,
        audio_timeline,
        '日本語',
        '日本語',
    )

    assert completed_tracks[1]['language'] == '英語'
    assert completed_timeline[0]['tracks'][1]['language'] == '英語'


def test_audio_track_reconciliation_keeps_real_dual_mono_interval() -> None:
    """番組内または番組境界で実際に2chへ変わったDual Mono区間は維持する。"""

    reconcile = RecordedPlaybackIndexer._RecordedPlaybackIndexer__reconcileAudioTracks  # pyright: ignore[reportPrivateUsage]
    audio_track = {
        'index': 1,
        'codec': 'AAC-LC',
        'channel': 'Dual Mono',
        'sampling_rate': 48_000,
        'language': '日本語+英語',
        'stream_index': 1,
        'channel_layout': 'stereo',
        'is_dual_mono': True,
    }
    transition_timeline = [
        {
            'start_time': 0.0,
            'end_time': 6.0,
            'tracks': [{**audio_track, 'channel': 'Monaural', 'language': '日本語', 'is_dual_mono': False}],
        },
        {'start_time': 6.0, 'end_time': 600.0, 'tracks': [audio_track]},
    ]

    reconciled = reconcile([audio_track], transition_timeline)

    assert reconciled[0]['channel'] == 'Dual Mono'
    assert reconciled[0]['language'] == '日本語+英語'
    assert reconciled[0]['is_dual_mono'] is True


def test_transient_audio_change_returning_within_five_seconds_is_suppressed() -> None:
    """前後が同じ音声状態なら、5秒以下の異常構成や欠落を受信ミスとして埋める。"""

    suppress = RecordedPlaybackIndexer._RecordedPlaybackIndexer__suppressTransientAudioTimelineChanges  # pyright: ignore[reportPrivateUsage]
    stereo_track = {'index': 1, 'channel': 'Stereo', 'stream_index': 2, 'pid': 0x104E}
    invalid_track = {'index': 1, 'channel': '24 Channels', 'stream_index': 2, 'pid': 0x104E}
    timeline = [
        {'start_time': 0.0, 'end_time': 10.0, 'tracks': [stereo_track]},
        {'start_time': 10.0, 'end_time': 10.02, 'tracks': [invalid_track]},
        {'start_time': 10.02, 'end_time': 20.0, 'tracks': [stereo_track]},
        {'start_time': 20.0, 'end_time': 22.0, 'tracks': []},
        {'start_time': 22.0, 'end_time': 30.0, 'tracks': [stereo_track]},
    ]

    assert suppress(timeline) == [{
        'start_time': 0.0,
        'end_time': 30.0,
        'tracks': [stereo_track],
    }]


def test_real_or_unconfirmed_audio_change_is_not_suppressed() -> None:
    """5秒超の変化と、元へ戻らない先頭・末尾の変化は保持する。"""

    suppress = RecordedPlaybackIndexer._RecordedPlaybackIndexer__suppressTransientAudioTimelineChanges  # pyright: ignore[reportPrivateUsage]
    stereo_track = {'index': 1, 'channel': 'Stereo'}
    monaural_track = {'index': 1, 'channel': 'Monaural'}
    timeline = [
        {'start_time': 0.0, 'end_time': 10.0, 'tracks': [stereo_track]},
        {'start_time': 10.0, 'end_time': 15.01, 'tracks': [monaural_track]},
        {'start_time': 15.01, 'end_time': 20.0, 'tracks': [stereo_track]},
        {'start_time': 20.0, 'end_time': 21.0, 'tracks': []},
    ]

    assert suppress(timeline) == timeline


def test_audio_tracks_are_normalized_by_actual_pid_without_duplicates() -> None:
    """部分probeでstream indexが振り直されても、実ファイルのPIDでTrackを一意に対応付ける。"""

    normalize = RecordedPlaybackIndexer._RecordedPlaybackIndexer__normalizeAudioTracks  # pyright: ignore[reportPrivateUsage]
    tracks = [
        {
            'index': 1, 'stream_index': 1, 'pid': 0x112, 'codec': 'AAC-LC', 'channel': 'Stereo',
            'sampling_rate': 48_000, 'language': '日本語',
        },
        {
            'index': 2, 'stream_index': 2, 'pid': 0x112, 'codec': 'AAC-LC', 'channel': 'Stereo',
            'sampling_rate': 48_000, 'language': None,
        },
        {
            'index': 3, 'stream_index': 3, 'pid': 0x113, 'codec': 'AAC-LC', 'channel': 'Stereo',
            'sampling_rate': 48_000, 'language': None,
        },
    ]
    probe = {'streams': [
        {'index': 2, 'id': '0x112', 'codec_type': 'audio', 'codec_name': 'aac', 'channels': 2},
        {'index': 3, 'id': '0x113', 'codec_type': 'audio', 'codec_name': 'aac', 'channels': 2},
    ]}

    normalized = normalize(tracks, probe)

    assert [(track['stream_index'], track['pid']) for track in normalized] == [(2, 0x112), (3, 0x113)]


def test_pmt_audio_pid_missing_from_ffprobe_is_added_without_title_rules() -> None:
    """同一programのPMT音声PIDはタイトルに依存せず候補化し、実packet走査へ渡す。"""

    normalize = RecordedPlaybackIndexer._RecordedPlaybackIndexer__normalizeAudioTracks  # pyright: ignore[reportPrivateUsage]
    primary = {
        'index': 1, 'stream_index': 2, 'pid': 0x112, 'codec': 'AAC-LC', 'channel': 'Stereo',
        'sampling_rate': 48_000, 'language': '日本語',
    }
    probe = {'streams': [
        {'index': 2, 'id': '0x112', 'codec_type': 'audio', 'codec_name': 'aac', 'channels': 2},
        {'index': 3, 'id': '0x114', 'codec_type': 'subtitle', 'codec_name': 'arib_caption'},
    ]}

    normalized = normalize([primary], probe, {0x112: 0x0F, 0x113: 0x0F})

    assert len(normalized) == 2
    assert normalized[1]['pid'] == 0x113
    assert normalized[1]['codec'] == 'AAC-LC'
    assert normalized[1]['channel'] == 'Unknown'


def test_packet_only_audio_stream_is_kept_by_actual_packet_range(monkeypatch) -> None:
    """デコード不能でframeがなくても、実音声packetがある副音声は存在区間へ残す。"""

    class FakeStdout:
        def __init__(self) -> None:
            self.lines = [
                b'packet|stream_index=3|pts_time=100.000000|duration_time=1.152000\n',
                b'packet|stream_index=3|pts_time=101.152000|duration_time=1.152000\n',
                b'',
            ]

        async def readline(self):
            return self.lines.pop(0)

    class FakeStderr:
        async def read(self):
            return b''

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = FakeStdout()
            self.stderr = FakeStderr()
            self.returncode = 0

        async def wait(self):
            return self.returncode

    async def CreateFakeProcess(*_args, **_kwargs):
        return FakeProcess()

    monkeypatch.setattr('app.metadata.RecordedPlaybackIndexer.asyncio.create_subprocess_exec', CreateFakeProcess)
    probe_packets = RecordedPlaybackIndexer._RecordedPlaybackIndexer__probeMissingAudioPacketRanges  # pyright: ignore[reportPrivateUsage]
    track = {
        'index': 2, 'stream_index': 3, 'pid': 0x113, 'codec': 'AAC-LC', 'channel': 'Stereo',
        'sampling_rate': 48_000, 'language': None,
    }

    ranges = asyncio.run(probe_packets(1, Path('/recording.ts'), 20.0, 95.0, {3}, {3: track}, 1))

    assert len(ranges) == 1
    assert ranges[0][0] == 5.0
    assert ranges[0][1] == pytest.approx(7.304)
    assert ranges[0][2]['stream_index'] == 3


def test_ffprobe_invisible_audio_pid_is_found_from_ts_pes_pts(tmp_path: Path) -> None:
    """FFprobeがstream化しない音声も、生TSに実在するPID/PESだけを存在区間へ採用する。"""

    def EncodePTS(pts: int) -> bytes:
        return bytes((
            0x20 | (((pts >> 30) & 0x07) << 1) | 1,
            (pts >> 22) & 0xFF,
            (((pts >> 15) & 0x7F) << 1) | 1,
            (pts >> 7) & 0xFF,
            ((pts & 0x7F) << 1) | 1,
        ))

    packets = []
    for seconds in range(100, 105):
        pid = 0x113
        header = bytes((0x47, 0x40 | ((pid >> 8) & 0x1F), pid & 0xFF, 0x10))
        # ADTS: AAC-LC / 48kHz / channel_configuration=2 (Stereo)
        adts = bytes.fromhex('fff84ca0400200')
        pes = b'\x00\x00\x01\xc0\x00\x00\x80\x80\x05' + EncodePTS(seconds * 90_000) + adts
        packets.append(header + pes + (b'\xff' * (188 - len(header) - len(pes))))
    ts_path = tmp_path / 'packet-only-audio.ts'
    ts_path.write_bytes(b''.join(packets))
    scan = RecordedPlaybackIndexer._RecordedPlaybackIndexer__scanTSAudioPIDRanges  # pyright: ignore[reportPrivateUsage]
    track = {
        'index': 2, 'stream_index': 3, 'pid': 0x113, 'codec': 'AAC-LC', 'channel': 'Unknown',
        'sampling_rate': 48_000, 'language': None,
    }

    ranges = scan(ts_path, 20.0, 95.0, {3}, {3: track})

    assert len(ranges) == 1
    assert ranges[0][0] == 5.0
    assert ranges[0][1] == 10.0
    assert ranges[0][2]['pid'] == 0x113
    assert ranges[0][2]['codec'] == 'AAC-LC'
    assert ranges[0][2]['sampling_rate'] == 48_000
    assert ranges[0][2]['channel'] == 'Stereo'
    assert ranges[0][2]['channel_layout'] == 'stereo'


def test_whole_file_frames_discover_late_audio_pid_at_exact_pts() -> None:
    """先頭probeにない音声PIDを全編フレームから追加し、最初のPTSを開始境界にする。"""

    append_frame = RecordedPlaybackIndexer._RecordedPlaybackIndexer__appendAudioFrameTimelineEntry  # pyright: ignore[reportPrivateUsage]
    apply_metadata = RecordedPlaybackIndexer._RecordedPlaybackIndexer__applyDiscoveredStreamMetadata  # pyright: ignore[reportPrivateUsage]
    finalize = RecordedPlaybackIndexer._RecordedPlaybackIndexer__finalizeAudioTimeline  # pyright: ignore[reportPrivateUsage]
    audio_tracks = []
    tracks_by_stream_index = {}
    active_states = {}
    ranges = []
    source_start_time = 6182.640978
    duration = 608.746666

    append_frame(
        {'stream_index': '5', 'pts_time': '6307.867644', 'channels': '2', 'channel_layout': 'stereo'},
        duration,
        source_start_time,
        tracks_by_stream_index,
        audio_tracks,
        active_states,
        ranges,
    )
    append_frame(
        {'stream_index': '5', 'pts_time': '6791.387644', 'channels': '2', 'channel_layout': 'stereo'},
        duration,
        source_start_time,
        tracks_by_stream_index,
        audio_tracks,
        active_states,
        ranges,
    )
    apply_metadata(
        {'index': '5', 'id': '0x111', 'codec_type': 'audio', 'codec_name': 'aac', 'profile': 'unknown'},
        duration,
        {},
        {},
        [],
        audio_tracks,
        tracks_by_stream_index,
        active_states,
        ranges,
        {},
    )
    timeline = finalize(duration, audio_tracks, active_states, ranges)

    assert audio_tracks[0]['pid'] == 0x111
    assert audio_tracks[0]['codec'] == 'AAC-LC'
    assert timeline is not None
    first_active_interval = next(interval for interval in timeline if interval['tracks'])
    assert first_active_interval['start_time'] == pytest.approx(125.226666)
    assert first_active_interval['tracks'][0]['stream_index'] == 5


def test_audio_frame_pts_is_unwrapped_per_stream_across_33bit_wrap() -> None:
    """途中追加音声のPTSが33bitラップしても最終時刻を0へ巻き戻さない。"""

    append_frame = RecordedPlaybackIndexer._RecordedPlaybackIndexer__appendAudioFrameTimelineEntry  # pyright: ignore[reportPrivateUsage]
    track = {
        'index': 2, 'stream_index': 5, 'pid': 0x113, 'codec': 'AAC-LC', 'channel': 'Stereo',
        'sampling_rate': 48_000, 'language': None,
    }
    tracks = [track]
    tracks_by_stream_index = {5: track}
    active_states = {}
    ranges = []
    source_start_time = 95_079.412878
    pts_wrap_seconds = (1 << 33) / 90_000

    append_frame(
        {'stream_index': '5', 'pts_time': str(source_start_time + 5.994666), 'channels': '2', 'channel_layout': 'stereo'},
        8_349.0,
        source_start_time,
        tracks_by_stream_index,
        tracks,
        active_states,
        ranges,
    )
    signature, interval_start, _, timeline_track = active_states[5]
    active_states[5] = (signature, interval_start, 363.9, timeline_track)
    append_frame(
        {
            'stream_index': '5',
            'pts_time': str(source_start_time + 364.1 - pts_wrap_seconds),
            'channels': '2',
            'channel_layout': 'stereo',
        },
        8_349.0,
        source_start_time,
        tracks_by_stream_index,
        tracks,
        active_states,
        ranges,
    )

    assert active_states[5][1] == pytest.approx(5.994666)
    assert active_states[5][2] == pytest.approx(364.1)


def test_stale_index_failure_preserves_published_index(monkeypatch) -> None:
    """旧VersionのReady索引が失敗しても状態・Version・索引本体を更新対象に含めない。"""

    async def GetRecordedVideo(**_kwargs):
        return SimpleNamespace(playback_index_status='Ready', playback_index_version=7)

    updates: list[dict[str, object]] = []

    class FakeQuery:
        async def update(self, **values) -> None:
            updates.append(values)

    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.RecordedVideo.get_or_none',
        GetRecordedVideo,
    )
    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.RecordedVideo.filter',
        lambda **_kwargs: FakeQuery(),
    )

    asyncio.run(
        RecordedPlaybackIndexer._RecordedPlaybackIndexer__markFailed(307, 'FrameProbeFailed')  # pyright: ignore[reportPrivateUsage]
    )

    assert updates == [{'playback_index_error_code': 'FrameProbeFailed'}]


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


def test_subtitle_backfill_selects_dynamic_arib_ttml_track_from_recorded_program(monkeypatch) -> None:
    """FFprobeにない途中追加PIDも対象programだけcomponent tag付きtrackとして補完する。"""

    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.TSKeyFrameSeeker.findARIBCaptionPIDs',
        lambda _path: set(),
    )
    monkeypatch.setattr(
        'app.metadata.RecordedPlaybackIndexer.TSKeyFrameSeeker.findARIBTTMLStreams',
        lambda _path: [
            ARIBTTMLStreamInfo(program_number=1, pid=0x0130, component_tag=0x30),
            ARIBTTMLStreamInfo(program_number=2, pid=0x0230, component_tag=0x30),
        ],
    )
    recorded_video = SimpleNamespace(subtitle_tracks=[], container_format='MPEG-TS')

    tracks = RecordedPlaybackIndexer._RecordedPlaybackIndexer__backfillARIBSubtitleTracks(  # pyright: ignore[reportPrivateUsage]
        recorded_video,
        {
            'streams': [{'index': 0, 'codec_type': 'video'}],
            'programs': [{
                'program_num': 2,
                'streams': [{'index': 0}],
            }],
        },
        Path('/recording.ts'),
    )

    assert tracks == [{
        'index': 1,
        'codec': 'arib_ttml',
        'language': 'jpn',
        'title': None,
        'pid': 0x0230,
        'component_tag': 0x30,
        'program_number': 2,
    }]


def test_ffprobe_compact_escape_split_and_unescape() -> None:
    """pipe / backslash / CR/LF 入り compact field を escape 解釈して split/unescape する。"""

    from app.metadata.RecordedPlaybackIndexer import (
        parseFFprobeCompactKeyValues,
        splitFFprobeCompactFields,
    )

    line = r'frame|media_type=video|tag:title=a\|b\\c\nline\rret|pts_time=1.0'
    fields = splitFFprobeCompactFields(line)
    assert fields[0] == 'frame'
    values = parseFFprobeCompactKeyValues(line)
    assert values['media_type'] == 'video'
    assert values['tag:title'] == 'a|b\\c\nline\rret'
    assert values['pts_time'] == '1.0'


def test_ffprobe_compact_matches_json_style_metadata() -> None:
    """compact 解釈結果が JSON writer 相当の key/value と一致すること。"""

    from app.metadata.RecordedPlaybackIndexer import parseFFprobeCompactKeyValues

    # FFprobe compact: tag:title=hello\|world と \n / \r を JSON 相当へ復元する
    compact = r'stream|index=1|codec_type=audio|tag:language=jpn|tag:title=hello\|world\nnext\rline'
    values = parseFFprobeCompactKeyValues(compact)
    json_equivalent = {
        'index': '1',
        'codec_type': 'audio',
        'tag:language': 'jpn',
        'tag:title': 'hello|world\nnext\rline',
    }
    for key, expected in json_equivalent.items():
        assert values[key] == expected
