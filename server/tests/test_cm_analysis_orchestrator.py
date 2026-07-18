# pyright: reportPrivateUsage=false, reportUnknownLambdaType=false

import asyncio
import os
from concurrent.futures import Future
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.metadata.CMAnalysisOrchestrator import CMAnalysisOrchestrator
from app.metadata.CMAnalyzer import CMInputDescriptor
from app.metadata.CMChapterFile import CMChapterReadResult
from app.metadata.CMLogoSelector import CMLogoSelection
from app.models.CMAnalysis import CMLogo, RecordedVideoCMAnalysis
from app.schemas import AudioTrackTimelineEntry
from app.streams.RecordedPlaybackCapabilities import RecordedPlaybackBackend


def test_analyzer_stage_callback_never_decreases_progress() -> None:
    """HW fallbackの進捗未指定や遅い通知でも履歴progressを後退させない。"""

    class FakeHistory:
        def __init__(self) -> None:
            self.execution = SimpleNamespace(progress=0.05, stage='ProbingMedia')
            self.updates: list[tuple[str, float]] = []

        async def setStage(self, stage: str, progress: float | None = None) -> None:
            assert progress is not None
            self.execution.stage = stage
            self.execution.progress = progress
            self.updates.append((stage, progress))

        async def setProgress(self, progress: float) -> None:
            self.execution.progress = progress
            self.updates.append((self.execution.stage, progress))

    async def Run() -> list[tuple[str, float]]:
        history = FakeHistory()
        callback = CMAnalysisOrchestrator._buildAnalyzerStageCallback(history)  # type: ignore[arg-type]
        await callback('PreparingMedia', 0.10)
        await callback('IndexingMedia', 0.25)
        await callback('HardwareFallback', None)
        await callback('ChapterAnalyzing', 0.30)
        await callback('LogoAnalyzing', 0.20)
        await callback('LogoAnalyzing', 0.45)
        return history.updates

    assert asyncio.run(Run()) == [
        ('PreparingMedia', 0.10),
        ('IndexingMedia', 0.25),
        ('HardwareFallback', 0.25),
        ('ChapterAnalyzing', 0.30),
        ('LogoAnalyzing', 0.30),
        ('LogoAnalyzing', 0.45),
    ]


@pytest.mark.parametrize(
    ('path', 'directory', 'expected'),
    [
        ('/recordings/excluded/program.ts', '/recordings/excluded', True),
        ('/recordings/excluded/sub/program.ts', '/recordings/excluded', True),
        ('/recordings/excluded-2/program.ts', '/recordings/excluded', False),
        ('/recordings/other/program.ts', '/recordings/excluded', False),
    ],
)
def test_exclusion_uses_directory_boundaries(path: str, directory: str, expected: bool) -> None:
    assert CMAnalysisOrchestrator.isPathWithinDirectory(Path(path), Path(directory)) is expected


def test_attempt_key_covers_input_runtime_and_logo_content() -> None:
    logo = CMLogo(id=10, file_hash='a' * 64, path='/logos/logo.lgd')
    selection = CMLogoSelection(status='Selected', logos=(logo,), runtime_paths=(Path('/runtime/logo.lgd'),))
    base = CMAnalysisOrchestrator.buildAttemptKey(
        {'size': 100, 'mtime_ns': 200, 'sample_sha256': 'input-a'},
        {'analyzer_version': 'CM-3'},
        selection,
    )

    assert base == CMAnalysisOrchestrator.buildAttemptKey(
        {'sample_sha256': 'input-a', 'mtime_ns': 200, 'size': 100},
        {'analyzer_version': 'CM-3'},
        selection,
    )
    assert base != CMAnalysisOrchestrator.buildAttemptKey(
        {'size': 100, 'mtime_ns': 200, 'sample_sha256': 'input-b'},
        {'analyzer_version': 'CM-3'},
        selection,
    )
    assert base != CMAnalysisOrchestrator.buildAttemptKey(
        {'size': 100, 'mtime_ns': 200, 'sample_sha256': 'input-a'},
        {'analyzer_version': 'CM-4'},
        selection,
    )
    logo.file_hash = 'b' * 64
    assert base != CMAnalysisOrchestrator.buildAttemptKey(
        {'size': 100, 'mtime_ns': 200, 'sample_sha256': 'input-a'},
        {'analyzer_version': 'CM-3'},
        selection,
    )


def test_attempt_key_covers_service_metadata_and_resolved_streams() -> None:
    selection = CMLogoSelection(status='Missing')
    descriptor = CMInputDescriptor(
        format_name='mpegts',
        video_stream_index=3,
        audio_stream_index=4,
        video_codec_name='av1',
        pixel_format='yuv420p10le',
        bit_depth=10,
        width=1920,
        height=1080,
        field_order='progressive',
        time_base=Fraction(1, 90_000),
        source_frame_rate=Fraction(30_000, 1001),
        duration_seconds=60.0,
        program_id=101,
        service_id=101,
    )
    base = CMAnalysisOrchestrator.buildAttemptKey(
        {'sample_sha256': 'input'},
        {'analyzer_version': 'CM-3'},
        selection,
        descriptor=descriptor,
        service_id=101,
    )

    assert base != CMAnalysisOrchestrator.buildAttemptKey(
        {'sample_sha256': 'input'},
        {'analyzer_version': 'CM-3'},
        selection,
        descriptor=descriptor,
        service_id=102,
    )
    assert base != CMAnalysisOrchestrator.buildAttemptKey(
        {'sample_sha256': 'input'},
        {'analyzer_version': 'CM-3'},
        selection,
        descriptor=descriptor,
        service_id=101,
        has_variable_video_format=True,
    )
    assert base != CMAnalysisOrchestrator.buildAttemptKey(
        {'sample_sha256': 'input'},
        {'analyzer_version': 'CM-3'},
        selection,
        descriptor=replace(descriptor, has_variable_audio_stream=True),
        service_id=101,
    )


def test_variable_selected_audio_stream_requires_a_real_alternative_interval() -> None:
    timeline: list[AudioTrackTimelineEntry] = [
        {
            'start_time': 0.0,
            'end_time': 30.0,
            'tracks': [{'index': 1, 'stream_index': 4, 'codec': 'AAC', 'channel': 'Stereo',
                        'sampling_rate': 48_000, 'language': 'ja'}],
        },
        {
            'start_time': 30.0,
            'end_time': 60.0,
            'tracks': [{'index': 2, 'stream_index': 7, 'codec': 'AAC', 'channel': 'Stereo',
                        'sampling_rate': 48_000, 'language': 'ja'}],
        },
    ]
    assert CMAnalysisOrchestrator._hasVariableSelectedAudioStream(timeline, 4) is True

    # 同じPID/stream内の2ch→5.1ch変更はFFmpegの固定WAV正規化で受け入れる。
    timeline[1]['tracks'][0]['stream_index'] = 4
    timeline[1]['tracks'][0]['channel'] = '5.1ch'
    assert CMAnalysisOrchestrator._hasVariableSelectedAudioStream(timeline, 4) is False

    # 無音区間やstream_indexを持たない旧メタデータだけでは途中交代と推測しない。
    timeline[1]['tracks'] = []
    assert CMAnalysisOrchestrator._hasVariableSelectedAudioStream(timeline, 4) is False


@pytest.mark.parametrize(('status', 'same_key', 'expected'), [
    ('Completed', True, True),
    ('Failed', True, True),
    ('Unsupported', True, True),
    ('Interrupted', True, False),
    ('Pending', True, False),
    ('Completed', False, False),
])
def test_automatic_retry_contract(status: str, same_key: bool, expected: bool) -> None:
    state = RecordedVideoCMAnalysis(status=status, attempt_key_sha256='same' if same_key else 'other')

    assert CMAnalysisOrchestrator._isStableAutomaticResult(state, 'same') is expected


def test_generated_result_requires_matching_chapter_hash() -> None:
    saved = {
        'exists': True,
        'size': 100,
        'mtime_ns': 200,
        'device': 1,
        'inode': 10,
        'sha256': 'same',
    }
    assert CMAnalysisOrchestrator._sameChapterFingerprint(
        saved,
        dict(saved),
    ) is True
    assert CMAnalysisOrchestrator._sameChapterFingerprint(
        saved,
        {**saved, 'sha256': 'new'},
    ) is False
    assert CMAnalysisOrchestrator._sameChapterFingerprint(
        saved,
        {**saved, 'mtime_ns': 201},
    ) is False
    assert CMAnalysisOrchestrator._sameChapterFingerprint(
        saved,
        {**saved, 'inode': 11},
    ) is False
    assert CMAnalysisOrchestrator._sameChapterFingerprint(None, {'sha256': 'new'}) is False


def test_pending_generated_content_can_recover_after_file_db_commit_gap() -> None:
    pending = {'exists': True, 'size': 100, 'sha256': 'generated'}
    committed = {
        'exists': True,
        'size': 100,
        'mtime_ns': 200,
        'device': 1,
        'inode': 10,
        'sha256': 'generated',
    }

    assert CMAnalysisOrchestrator._samePendingChapterContent(pending, committed) is True
    assert CMAnalysisOrchestrator._samePendingChapterContent(pending, {**committed, 'sha256': 'external'}) is False
    assert CMAnalysisOrchestrator._samePendingChapterContent(committed, committed) is False
    assert CMAnalysisOrchestrator._isPendingMarkerFingerprint(pending) is True
    assert CMAnalysisOrchestrator._isPendingMarkerFingerprint(committed) is False
    saved_input = {'size': 1, 'mtime_ns': 2, 'sample_sha256': 'input-a'}
    current_input = {'size': 1, 'mtime_ns': 3, 'sample_sha256': 'input-b'}
    assert CMAnalysisOrchestrator._sameInputFingerprint(saved_input, dict(saved_input)) is True
    assert CMAnalysisOrchestrator._sameInputFingerprint(saved_input, current_input) is False

    assert CMAnalysisOrchestrator._isRecoverablePendingState(
        RecordedVideoCMAnalysis(status='Failed', error_code='ChapterCommitFailed')
    ) is True
    assert CMAnalysisOrchestrator._isRecoverablePendingState(
        RecordedVideoCMAnalysis(status='Failed', error_code='ChapterChangedDuringCommit')
    ) is False


def test_content_fingerprint_detects_rewrite_even_when_size_and_mtime_are_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def RunInline(function, *args):  # type: ignore[no-untyped-def]
        return function(*args)

    # ホストPythonの終了不能なThreadPoolExecutorを避け、I/O本体だけを検証する。
    monkeypatch.setattr('app.metadata.CMAnalysisOrchestrator.asyncio.to_thread', RunInline)
    recorded_path = tmp_path / 'recording.mkv'
    recorded_path.write_bytes(b'a' * 200_000)
    original_stat = recorded_path.stat()
    before = asyncio.run(CMAnalysisOrchestrator.buildInputFingerprint(recorded_path))
    recorded_path.write_bytes(b'b' * 200_000)
    os.utime(recorded_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    after = asyncio.run(CMAnalysisOrchestrator.buildInputFingerprint(recorded_path))

    assert before['size'] == after['size']
    assert before['mtime_ns'] == after['mtime_ns']
    assert before['sample_sha256'] != after['sample_sha256']


def test_commit_executor_future_is_joined_before_cancellation_is_propagated() -> None:
    async def Run() -> None:
        # run_in_executor() と同じ concurrent Future -> asyncio Future の橋渡しだけを再現する。
        # このホストの Python は default ThreadPoolExecutor の終了が不能なため、実スレッドを
        # 起動すると helper の成否に関係なく asyncio.run() の shutdown でテストが停止する。
        worker_future: Future[CMChapterReadResult] = Future()
        commit_future = asyncio.wrap_future(worker_future)
        waiter = asyncio.create_task(CMAnalysisOrchestrator._awaitCommitFuture(commit_future))
        # helper が最初の wait へ入ってからキャンセルし、本番の commit 中断を再現する。
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        assert waiter.done() is False
        assert commit_future.cancelled() is False
        assert worker_future.cancelled() is False
        worker_future.set_result(CMChapterReadResult('Missing', (), {'exists': False}))
        result, cancellation_requested = await waiter
        assert result.status == 'Missing'
        assert cancellation_requested is True

    asyncio.run(Run())


def test_cm_hardware_decode_uses_available_device_without_codec_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        RecordedPlaybackBackend,
        'discoverRenderDevices',
        lambda encoder: ['/dev/dri/renderD128' if encoder == 'QSVEncC' else '/dev/dri/renderD129'],
    )
    monkeypatch.setattr(
        'app.metadata.CMAnalysisOrchestrator.RecordedPlaybackCapabilityProbe.getSelectedDevice',
        lambda encoder: None,
    )

    assert CMAnalysisOrchestrator._resolveHardwareDecodeDevice('QSVEncC') == 'vaapi:/dev/dri/renderD128'
    assert CMAnalysisOrchestrator._resolveHardwareDecodeDevice('NVEncC') == 'cuda:0'
    assert CMAnalysisOrchestrator._resolveHardwareDecodeDevice('VCEEncC') == 'vaapi:/dev/dri/renderD129'
    assert CMAnalysisOrchestrator._resolveHardwareDecodeDevice('FFmpeg') is None


def test_cm_hardware_decode_prefers_capability_probed_render_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        'app.metadata.CMAnalysisOrchestrator.RecordedPlaybackCapabilityProbe.getSelectedDevice',
        lambda encoder: '/dev/dri/renderD132',
    )
    monkeypatch.setattr(RecordedPlaybackBackend, 'discoverRenderDevices', lambda encoder: [])

    assert CMAnalysisOrchestrator._resolveHardwareDecodeDevice('QSVEncC') == 'vaapi:/dev/dri/renderD132'
