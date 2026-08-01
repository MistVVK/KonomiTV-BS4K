# pyright: reportPrivateUsage=false, reportUnknownLambdaType=false

import asyncio
import os
from concurrent.futures import Future
from contextlib import asynccontextmanager
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.metadata.AnalysisTaskTracker import AnalysisTaskTracker
from app.metadata.CMAnalysisOrchestrator import CMAnalysisOrchestrator
from app.metadata.CMAnalysisWorkspace import CMAnalysisWorkspace
from app.metadata.CMAnalyzer import (
    CMAnalyzerRequest,
    CMAnalyzerResult,
    CMInputDescriptor,
)
from app.metadata.CMChapterFile import CMChapterPathSelection, CMChapterReadResult
from app.metadata.CMLogoScanner import CMLogoScanner
from app.metadata.CMLogoSelector import CMLogoSelection, CMLogoSelector
from app.metadata.KonomiTVBS4KChapterFile import (
    KonomiTVBS4KChapterGenerator,
    KonomiTVBS4KChapterProvenance,
    KonomiTVBS4KChapterReadResult,
    KonomiTVBS4KChapterRecording,
)
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


def test_logical_audio_rebuild_preference_uses_only_same_input_failure_history() -> None:
    """媒体準備失敗とfallback実績は同一fingerprintだけに引き継ぐ。"""

    input_fingerprint = {'size': 100, 'mtime_ns': 200, 'sample_sha256': 'input-a'}
    failed = RecordedVideoCMAnalysis(
        status='Failed',
        error_code='MediaPreparationFailed',
        input_fingerprint=input_fingerprint,
    )
    assert CMAnalysisOrchestrator._konomiTVBS4KLogicalAudioRebuildPreference(
        failed,
        input_fingerprint,
    ) == 'FFmpegAfterPreviousFailure'
    assert CMAnalysisOrchestrator._konomiTVBS4KLogicalAudioRebuildPreference(
        failed,
        {**input_fingerprint, 'sample_sha256': 'input-b'},
    ) == 'PyAV'

    completed_after_fallback = RecordedVideoCMAnalysis(
        status='Completed',
        input_fingerprint=input_fingerprint,
        runtime_fingerprint={
            'analyzer_version': 'cm-9',
            'konomitv_bs4k_logical_audio_rebuild_strategy': 'FFmpegFallbackAfterSignal',
        },
    )
    assert CMAnalysisOrchestrator._konomiTVBS4KLogicalAudioRebuildPreference(
        completed_after_fallback,
        input_fingerprint,
    ) == 'FFmpegAfterPreviousFailure'

    pending_after_chapter_sync = RecordedVideoCMAnalysis(
        status='Pending',
        error_code='GeneratedChapterPipelineOutdated',
        input_fingerprint=input_fingerprint,
        runtime_fingerprint={
            'analyzer_version': 'cm-9',
            'konomitv_bs4k_logical_audio_rebuild_preference': 'FFmpegAfterPreviousFailure',
        },
    )
    assert CMAnalysisOrchestrator._konomiTVBS4KLogicalAudioRebuildPreference(
        pending_after_chapter_sync,
        input_fingerprint,
    ) == 'FFmpegAfterPreviousFailure'

    unrelated_failure = RecordedVideoCMAnalysis(
        status='Failed',
        error_code='ChapterExeFailed',
        input_fingerprint=input_fingerprint,
    )
    assert CMAnalysisOrchestrator._konomiTVBS4KLogicalAudioRebuildPreference(
        unrelated_failure,
        input_fingerprint,
    ) == 'PyAV'


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


def test_generated_yaml_requires_verified_provenance_and_current_recording_input() -> None:
    """KonomiTV-BS4K生成YAMLは検証済み出自と現在の録画fingerprintが揃った場合だけ自前生成とみなす。"""

    recording = KonomiTVBS4KChapterRecording(
        duration_ms=60_000,
        size=100,
        mtime_ns=200,
        sample_sha256='a' * 64,
    )
    generated = KonomiTVBS4KChapterReadResult(
        'ValidNoCM',
        (),
        {'exists': True, 'sha256': 'b' * 64},
        KonomiTVBS4KChapterProvenance(
            source='Generated',
            generator_present=True,
            generator_consistent=True,
            generator=None,
            recording=recording,
        ),
    )
    current_input = {'size': 100, 'mtime_ns': 200, 'sample_sha256': 'a' * 64}

    assert CMAnalysisOrchestrator._isGeneratedChapterForInput(generated, current_input) is True
    for key, changed_value in (
        ('size', 101),
        ('mtime_ns', 201),
        ('sample_sha256', 'c' * 64),
    ):
        changed_input = {**current_input, key: changed_value}
        assert CMAnalysisOrchestrator._isGeneratedChapterForInput(generated, changed_input) is False
    assert CMAnalysisOrchestrator._isGeneratedChapterForInput(generated, {'size': 100}) is False

    generator = KonomiTVBS4KChapterGenerator(
        name='KonomiTV-BS4K',
        application_version='0.14.1+bs4k.1',
        pipeline_version='cm-9',
        generated_at='2026-07-23T12:00:00+09:00',
        chapters_sha256='c' * 64,
    )
    generated = KonomiTVBS4KChapterReadResult(
        'ValidNoCM',
        (),
        {'exists': True, 'sha256': 'b' * 64},
        KonomiTVBS4KChapterProvenance(
            source='Generated',
            generator_present=True,
            generator_consistent=True,
            generator=generator,
            recording=recording,
        ),
    )
    assert CMAnalysisOrchestrator._isGeneratedChapterForInput(
        generated,
        current_input,
        pipeline_version='cm-9',
    ) is True
    assert CMAnalysisOrchestrator._isGeneratedChapterForInput(
        generated,
        current_input,
        pipeline_version='cm-8',
    ) is False


@pytest.mark.parametrize(
    ('appeared_pipeline', 'expected_status', 'expected_publish_count'),
    [
        ('cm-8', 'Interrupted', 0),
        ('cm-9', 'Completed', 1),
    ],
)
def test_generated_sidecar_appearing_during_analysis_requires_current_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    appeared_pipeline: str,
    expected_status: str,
    expected_publish_count: int,
) -> None:
    """解析中に現れた旧pipeline生成物を新解析結果より優先しない。"""

    input_fingerprint = {
        'size': 100,
        'mtime_ns': 200,
        'sample_sha256': 'a' * 64,
    }
    recording = KonomiTVBS4KChapterRecording(
        duration_ms=60_000,
        size=100,
        mtime_ns=200,
        sample_sha256='a' * 64,
    )
    appeared_result = KonomiTVBS4KChapterReadResult(
        'ValidNoCM',
        (),
        {'exists': True, 'sha256': 'b' * 64},
        KonomiTVBS4KChapterProvenance(
            source='Generated',
            generator_present=True,
            generator_consistent=True,
            generator=KonomiTVBS4KChapterGenerator(
                name='KonomiTV-BS4K',
                application_version='0.14.1+bs4k.1',
                pipeline_version=appeared_pipeline,
                generated_at='2026-07-23T12:00:00+09:00',
                chapters_sha256='c' * 64,
            ),
            recording=recording,
        ),
    )
    descriptor = CMInputDescriptor(
        format_name='matroska,webm',
        video_stream_index=0,
        audio_stream_index=1,
        video_codec_name='hevc',
        pixel_format='yuv420p10le',
        bit_depth=10,
        width=1920,
        height=1080,
        field_order='progressive',
        time_base=Fraction(1, 1000),
        source_frame_rate=Fraction(30_000, 1001),
        duration_seconds=60.0,
        program_id=None,
        service_id=None,
    )

    class FakeAnalyzer:
        runtimeFingerprint = {'analyzer_version': 'cm-9'}

        async def resolveInputDescriptor(self, request: CMAnalyzerRequest) -> CMInputDescriptor:
            del request
            return descriptor

        async def analyze(self, request: CMAnalyzerRequest) -> CMAnalyzerResult:
            raise AssertionError(f'Unexpected direct analyzer call: {request}')

    class FakeHistory:
        def __init__(self) -> None:
            self.execution = SimpleNamespace(progress=0.0, stage='Queued')
            self.finishes: list[tuple[str, str | None]] = []

        async def setStage(self, stage: str, progress: float | None = None) -> None:
            self.execution.stage = stage
            self.execution.progress = progress

        async def finish(
            self,
            status: str,
            *,
            error_code: str | None = None,
            **kwargs: object,
        ) -> None:
            del kwargs
            self.finishes.append((status, error_code))

    class FakeWorkspace:
        path = tmp_path / 'work'

        async def cleanup(self) -> None:
            return None

    history = FakeHistory()

    @asynccontextmanager
    async def Track(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        del args, kwargs
        yield history

    async def Scan(cls: type[CMLogoScanner]) -> list[object]:
        del cls
        return []

    async def Select(
        recorded_video: object,
        settings: object,
        *,
        resolved_video_size: tuple[int, int] | None = None,
    ) -> CMLogoSelection:
        del recorded_video, settings, resolved_video_size
        return CMLogoSelection(status='Missing')

    async def CreateWorkspace(
        cls: type[CMAnalysisWorkspace],
        source_path: Path,
        recorded_video_id: int,
        duration_seconds: float,
    ) -> FakeWorkspace:
        del cls, source_path, recorded_video_id, duration_seconds
        return FakeWorkspace()

    monkeypatch.setattr(AnalysisTaskTracker, 'track', classmethod(Track))
    monkeypatch.setattr(CMLogoScanner, 'scan', classmethod(Scan))
    monkeypatch.setattr(CMLogoSelector, 'select', staticmethod(Select))
    monkeypatch.setattr(CMAnalysisWorkspace, 'create', classmethod(CreateWorkspace))
    monkeypatch.setattr(
        CMAnalysisOrchestrator,
        '_resolveHardwareDecodeContext',
        staticmethod(lambda: (None, None)),
    )

    orchestrator = CMAnalysisOrchestrator(FakeAnalyzer())  # type: ignore[arg-type]

    async def BuildInputFingerprint(path: Path) -> dict[str, int | str]:
        del path
        return input_fingerprint

    async def SelectChapterPath(path: Path) -> CMChapterPathSelection:
        del path
        return CMChapterPathSelection(tmp_path / 'recording.mkv.konomi-chapters.yaml', 'Canonical')

    async def ReadChapterFile(
        selection: CMChapterPathSelection,
        duration_seconds: float,
    ) -> KonomiTVBS4KChapterReadResult:
        del selection, duration_seconds
        return appeared_result

    async def Analyze(
        state: object,
        request: CMAnalyzerRequest,
        current_input: dict[str, int | str],
        attempt_key: str,
        runtime_fingerprint: dict[str, object],
    ) -> CMAnalyzerResult:
        del state, request, current_input, attempt_key, runtime_fingerprint
        return CMAnalyzerResult(
            status='completed',
            chapter_file=None,
            analyzer_version='cm-9',
            descriptor=descriptor,
        )

    publish_calls: list[dict[str, object]] = []

    async def Publish(
        recorded_video: object,
        state: SimpleNamespace,
        chapter_result: KonomiTVBS4KChapterReadResult,
        **kwargs: object,
    ) -> SimpleNamespace:
        del recorded_video, chapter_result
        publish_calls.append(kwargs)
        state.status = 'Completed'
        state.chapter_source = kwargs['source']
        return state

    async def SaveFailure(
        state: SimpleNamespace,
        status: str,
        error_code: str,
        error_message: str | None,
        **kwargs: object,
    ) -> SimpleNamespace:
        del error_message, kwargs
        state.status = status
        state.error_code = error_code
        return state

    orchestrator.buildInputFingerprint = BuildInputFingerprint  # type: ignore[method-assign]
    orchestrator._selectChapterPath = SelectChapterPath  # type: ignore[method-assign]
    orchestrator._readChapterFile = ReadChapterFile  # type: ignore[method-assign]
    orchestrator._analyze = Analyze  # type: ignore[method-assign]
    orchestrator._publishChapterResult = Publish  # type: ignore[method-assign]
    orchestrator._saveAttemptFailure = SaveFailure  # type: ignore[method-assign]

    async def SaveState() -> None:
        return None

    state = SimpleNamespace(
        status='Pending',
        attempt_key_sha256=None,
        error_code=None,
        error_message=None,
        input_fingerprint=None,
        runtime_fingerprint=None,
        save=SaveState,
    )
    recorded_video = SimpleNamespace(
        id=87,
        file_path=str(tmp_path / 'recording.mkv'),
        duration=60.0,
        has_video_stream_changes=False,
        audio_track_timeline=[],
        recorded_program=SimpleNamespace(service_id=101, title='MUSIC FAIR'),
    )
    initial_chapter = CMChapterReadResult('Missing', (), {'exists': False})

    result = asyncio.run(orchestrator._runTrackedAnalysis(
        recorded_video,  # type: ignore[arg-type]
        state,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        initial_chapter,
        'Canonical',
        input_fingerprint,
        'PyAV',
        'CMDetection',
    ))

    assert result.status == expected_status
    assert len(publish_calls) == expected_publish_count
    if appeared_pipeline == 'cm-8':
        assert result.error_code == 'ChapterChangedDuringAnalysis'
        assert history.finishes[-1] == ('Interrupted', 'ChapterChangedDuringAnalysis')
    else:
        assert publish_calls[0]['source'] == 'Generated'
        assert publish_calls[0]['pipeline_version'] == 'cm-9'
        assert history.finishes[-1] == ('Skipped', 'ChapterChangedDuringAnalysis')


def test_manual_yaml_is_never_treated_as_generated_for_current_input() -> None:
    """生成情報を持たない手書きYAMLは上書き対象にしない。"""

    manual = KonomiTVBS4KChapterReadResult(
        'ValidNoCM',
        (),
        {'exists': True, 'sha256': 'b' * 64},
        KonomiTVBS4KChapterProvenance(
            source='Manual',
            generator_present=False,
            generator_consistent=None,
            generator=None,
            recording=None,
        ),
    )

    assert CMAnalysisOrchestrator._isGeneratedChapterForInput(
        manual,
        {'size': 100, 'mtime_ns': 200, 'sample_sha256': 'a' * 64},
    ) is False


def test_pending_generated_marker_and_commit_failure_recovery_contract() -> None:
    pending = {'exists': True, 'size': 100, 'sha256': 'generated'}
    assert CMAnalysisOrchestrator._isPendingMarkerFingerprint(pending) is True
    assert CMAnalysisOrchestrator._isPendingMarkerFingerprint({**pending, 'mtime_ns': 200}) is False

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
        worker_future: Future[KonomiTVBS4KChapterReadResult] = Future()
        commit_future = asyncio.wrap_future(worker_future)
        waiter = asyncio.create_task(CMAnalysisOrchestrator._awaitCommitFuture(commit_future))
        # helper が最初の wait へ入ってからキャンセルし、本番の commit 中断を再現する。
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        assert waiter.done() is False
        assert commit_future.cancelled() is False
        assert worker_future.cancelled() is False
        worker_future.set_result(KonomiTVBS4KChapterReadResult('Missing', (), {'exists': False}, None))
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
        lambda encoder: ['/dev/dri/renderD128' if encoder == 'QSV' else '/dev/dri/renderD129'],
    )
    monkeypatch.setattr(
        'app.metadata.CMAnalysisOrchestrator.RecordedPlaybackCapabilityProbe.getSelectedDevice',
        lambda encoder: None,
    )

    assert CMAnalysisOrchestrator._resolveHardwareDecodeDevice('QSV') == 'vaapi:/dev/dri/renderD128'
    assert CMAnalysisOrchestrator._resolveHardwareDecodeDevice('NVENC') == 'cuda:0'
    assert CMAnalysisOrchestrator._resolveHardwareDecodeDevice('AMF') == 'vaapi:/dev/dri/renderD129'
    assert CMAnalysisOrchestrator._resolveHardwareDecodeDevice('FFmpeg') is None


def test_cm_hardware_decode_prefers_capability_probed_render_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        'app.metadata.CMAnalysisOrchestrator.RecordedPlaybackCapabilityProbe.getSelectedDevice',
        lambda encoder: '/dev/dri/renderD132',
    )
    monkeypatch.setattr(RecordedPlaybackBackend, 'discoverRenderDevices', lambda encoder: [])

    assert CMAnalysisOrchestrator._resolveHardwareDecodeDevice('QSV') == 'vaapi:/dev/dri/renderD132'
