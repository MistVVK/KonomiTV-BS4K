import asyncio
import pathlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal

import anyio
import pytest
from watchfiles import Change

from app import logging
from app.constants import JST
from app.metadata.AnalysisTaskTracker import AnalysisTaskTracker
from app.metadata.CMAnalysisOrchestrator import CMAnalysisOrchestrator
from app.metadata.CMAnalysisWorkspace import CMAnalysisWorkspace
from app.metadata.RecordedScanTask import (
    FileRecordingInfo,
    RecordedScanTask,
    RecordedVideoSummary,
)
from app.metadata.ThumbnailGenerator import ThumbnailGenerator
from app.models.RecordedVideo import RecordedVideo
from app.streams.RecordedSubtitleStream import RecordedSubtitleStream
from app.utils.DriveIOLimiter import DriveIOLimiter
from app.utils.ProcessLimiter import ProcessLimiter


@pytest.fixture(autouse=True)
def RunRecordedScanIOInline(monkeypatch: pytest.MonkeyPatch) -> None:
    """終了不能なhost ThreadPoolExecutorを避け、scannerの同期I/O本体だけを検証する。"""

    async def RunInline(function, *args, **kwargs):  # type: ignore[no-untyped-def]
        return function(*args, **kwargs)

    monkeypatch.setattr('app.metadata.RecordedScanTask.asyncio.to_thread', RunInline)


def CreateSummary() -> RecordedVideoSummary:
    timestamp = datetime(2026, 7, 13, tzinfo=JST)
    return RecordedVideoSummary(
        id=1,
        file_path='/recorded/program.ts',
        created_at=timestamp,
        recorded_program_id=1,
        status='Recorded',
        file_created_at=timestamp,
        file_modified_at=timestamp,
        file_size=1024,
        file_hash='hash',
    )


@pytest.mark.parametrize(
    ('recorded_video_status', 'should_delete'),
    [('Deleting', False), ('DeleteFailed', False), ('Recorded', True)],
)
def test_file_deletion_handler_preserves_deletion_retry_states(
    monkeypatch: pytest.MonkeyPatch,
    recorded_video_status: Literal['Deleting', 'DeleteFailed', 'Recorded'],
    should_delete: bool,
) -> None:
    """watcherのファイル消失処理が削除再試行中のDBレコードを回収しないことを検証する。"""

    scan_task = object.__new__(RecordedScanTask)
    scan_task._file_locks = {}
    scan_task._file_locks_dict_lock = asyncio.Lock()
    scan_task._symlink_path_map = {}
    scan_task._symlink_path_map_lock = asyncio.Lock()
    scan_task._recording_files = {}
    file_path = anyio.Path('/recorded/deleted.ts')
    recorded_program_deleted = False

    async def DeleteRecordedProgram() -> None:
        nonlocal recorded_program_deleted
        recorded_program_deleted = True

    recorded_program = SimpleNamespace(delete=DeleteRecordedProgram)
    recorded_video = SimpleNamespace(status=recorded_video_status, recorded_program=recorded_program)

    async def GetRecordedVideoOrNone(**conditions: str) -> SimpleNamespace:
        assert conditions == {'file_path': str(file_path)}
        return recorded_video

    monkeypatch.setattr(RecordedVideo, 'get_or_none', GetRecordedVideoOrNone)

    asyncio.run(
        scan_task._RecordedScanTask__handleFileDeletion(file_path)  # pyright: ignore[reportPrivateUsage]
    )

    assert recorded_program_deleted is should_delete


@pytest.mark.parametrize(
    ('summary_status', 'current_status', 'should_delete'),
    [
        ('Deleting', 'Deleting', False),
        ('DeleteFailed', 'DeleteFailed', False),
        ('Recorded', 'Recorded', True),
        ('Recorded', 'DeleteFailed', False),
    ],
)
def test_batch_non_existent_cleanup_preserves_deletion_retry_states(
    monkeypatch: pytest.MonkeyPatch,
    summary_status: Literal['Deleting', 'DeleteFailed', 'Recorded'],
    current_status: Literal['Deleting', 'DeleteFailed', 'Recorded'],
    should_delete: bool,
) -> None:
    """batch scanの消失レコード回収が削除再試行中のDBレコードを削除しないことを検証する。"""

    scan_task = object.__new__(RecordedScanTask)
    file_path = anyio.Path('/recorded/deleted.ts')
    summary = CreateSummary()
    summary.status = summary_status
    recorded_program_deleted = False

    async def Stat(_file_path: anyio.Path) -> None:
        raise FileNotFoundError

    class FakeRecordedProgramQuery:
        excluded_statuses: list[str] = []

        def exclude(self, *, recorded_video__status__in: list[str]) -> 'FakeRecordedProgramQuery':
            self.excluded_statuses = recorded_video__status__in
            return self

        async def delete(self) -> int:
            nonlocal recorded_program_deleted
            if current_status in self.excluded_statuses:
                return 0
            recorded_program_deleted = True
            return 1

    @asynccontextmanager
    async def InTransaction() -> AsyncGenerator[None, None]:
        yield

    monkeypatch.setattr(anyio.Path, 'stat', Stat)
    monkeypatch.setattr(
        'app.metadata.RecordedScanTask.RecordedProgram.filter',
        lambda **conditions: FakeRecordedProgramQuery()
        if conditions == {'id': summary.recorded_program_id}
        else pytest.fail(),
    )
    monkeypatch.setattr('app.metadata.RecordedScanTask.transactions.in_transaction', InTransaction)

    asyncio.run(
        scan_task._RecordedScanTask__cleanupNonExistentRecordedVideoRecords(  # pyright: ignore[reportPrivateUsage]
            {file_path: summary},
            {pathlib.Path('/recorded')},
        )
    )

    assert recorded_program_deleted is should_delete


@pytest.mark.parametrize('cpu_count', [1, 2, None])
def test_process_limiter_always_grants_at_least_one_permit(
    cpu_count: int | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1論理CPU・CPU数不明でも外部処理を永久待機させない。"""

    ProcessLimiter._semaphores.clear()  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr('app.utils.ProcessLimiter.psutil.cpu_count', lambda logical: cpu_count)

    async def AcquirePermit() -> None:
        semaphore = ProcessLimiter.getSemaphore(f'cpu-{cpu_count}')
        await asyncio.wait_for(semaphore.acquire(), timeout=0.1)
        semaphore.release()

    asyncio.run(AcquirePermit())
    ProcessLimiter._semaphores.clear()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize('failure', ['exception', 'cancel'])
def test_batch_scan_releases_running_flag_and_joins_pipeline_tasks(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB例外・cancelのどちらでもflagを解除し、次の一括スキャンを受け付ける。"""

    scan_task = object.__new__(RecordedScanTask)
    scan_task._is_batch_scan_running = False
    scan_task._batch_scan_pipeline_tasks = set()
    pipeline_cancelled = False
    invocation_count = 0
    subtitle_cleanup_count = 0

    async def RunPipeline() -> None:
        nonlocal pipeline_cancelled
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            pipeline_cancelled = True
            raise

    async def RunBatchScanInternal(_self: RecordedScanTask) -> None:
        nonlocal invocation_count
        invocation_count += 1
        if invocation_count == 1:
            _self._batch_scan_pipeline_tasks.add(asyncio.create_task(RunPipeline()))
            await asyncio.sleep(0)
            if failure == 'cancel':
                raise asyncio.CancelledError
            raise OSError('simulated scan failure')

    async def CleanupOrphanedSubtitleCaches() -> None:
        nonlocal subtitle_cleanup_count
        subtitle_cleanup_count += 1

    monkeypatch.setattr(RecordedScanTask, '_RecordedScanTask__runBatchScan', RunBatchScanInternal)
    monkeypatch.setattr(RecordedSubtitleStream, 'cleanupOrphanedCaches', CleanupOrphanedSubtitleCaches)

    async def Verify() -> None:
        expected_error = asyncio.CancelledError if failure == 'cancel' else OSError
        with pytest.raises(expected_error):
            await scan_task.runBatchScan()
        assert scan_task._is_batch_scan_running is False
        assert scan_task._batch_scan_pipeline_tasks == set()
        assert pipeline_cancelled is True
        await scan_task.runBatchScan()
        assert subtitle_cleanup_count == 1

    asyncio.run(Verify())


def test_recorded_folder_watch_filter_ignores_cm_workspace_exact_component() -> None:
    """watchfiles段階でCM workspaceの大量イベントを落とし、似た名前は監視対象に残す。"""

    watch_filter = RecordedScanTask.buildRecordedFolderWatchFilter()

    assert watch_filter(
        Change.modified,
        f'/recordings/{CMAnalysisWorkspace.ROOT_DIRECTORY_NAME}/157-token/normalized-media.cmwork',
    ) is False
    assert watch_filter(
        Change.modified,
        f'/recordings/{CMAnalysisWorkspace.ROOT_DIRECTORY_NAME}-copy/program.mkv',
    ) is True
    assert watch_filter(
        Change.modified,
        '/recordings/.konomitv-cm-analysis/157-token/normalized-media.cmwork',
    ) is False


def test_batch_path_iterator_prunes_and_recovers_workspace_roots(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_folder = tmp_path / 'recordings'
    nested_folder = recorded_folder / 'nested'
    workspace_root = nested_folder / CMAnalysisWorkspace.ROOT_DIRECTORY_NAME
    workspace_job = workspace_root / '1-deadbeef'
    workspace_job.mkdir(parents=True)
    temporary_media = workspace_job / 'prepared.cmwork.mkv'
    temporary_media.write_bytes(b'temporary')
    legacy_workspace_root = nested_folder / '.konomitv-cm-analysis'
    legacy_workspace_job = legacy_workspace_root / '2-deadbeef'
    legacy_workspace_job.mkdir(parents=True)
    legacy_temporary_media = legacy_workspace_job / 'legacy-prepared.cmwork.mkv'
    legacy_temporary_media.write_bytes(b'temporary')
    recording = nested_folder / 'program.ts'
    recording.write_bytes(b'recording')
    cleaned_parents: list[set[pathlib.Path]] = []

    async def CleanupStale(parents: set[pathlib.Path]) -> None:
        cleaned_parents.append(parents)

    async def RunInline(function, *args, **kwargs):  # type: ignore[no-untyped-def]
        return function(*args, **kwargs)

    monkeypatch.setattr(CMAnalysisWorkspace, 'cleanupStaleInParents', CleanupStale)
    monkeypatch.setattr('app.metadata.RecordedScanTask.asyncio.to_thread', RunInline)

    async def CollectPaths() -> list[pathlib.Path]:
        return [
            pathlib.Path(str(path))
            async for path in RecordedScanTask.iterRecordedFolderPaths(anyio.Path(recorded_folder))
        ]

    paths = asyncio.run(CollectPaths())

    assert recording in paths
    assert workspace_root not in paths
    assert workspace_job not in paths
    assert temporary_media not in paths
    assert legacy_workspace_root not in paths
    assert legacy_workspace_job not in paths
    assert legacy_temporary_media not in paths
    assert cleaned_parents == [{nested_folder}, {nested_folder}]


def test_batch_scan_recovers_workspace_next_to_external_symlink_target_once(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB行がなくても録画symlinkの実体親を1回だけstale cleanupする。"""

    recorded_folder = tmp_path / 'recordings'
    external_folder = tmp_path / 'external'
    recorded_folder.mkdir()
    external_folder.mkdir()
    first_target = external_folder / 'first.ts'
    second_target = external_folder / 'second.ts'
    first_target.write_bytes(b'first')
    second_target.write_bytes(b'second')
    first_symlink = recorded_folder / 'first.ts'
    second_symlink = recorded_folder / 'second.ts'
    first_symlink.symlink_to(first_target)
    second_symlink.symlink_to(second_target)
    cleaned_parents: list[set[pathlib.Path]] = []

    async def CleanupStale(parents: set[pathlib.Path]) -> None:
        cleaned_parents.append(parents)

    monkeypatch.setattr(CMAnalysisWorkspace, 'cleanupStaleInParents', CleanupStale)
    parents_seen: set[pathlib.Path] = set()

    async def CleanupResolvedTargets() -> None:
        await RecordedScanTask.cleanupStaleWorkspaceForResolvedSymlink(
            str(first_symlink),
            first_target.resolve(),
            parents_seen,
        )
        await RecordedScanTask.cleanupStaleWorkspaceForResolvedSymlink(
            str(second_symlink),
            second_target.resolve(),
            parents_seen,
        )
        # 通常ファイルはsymlink実体親の回収対象ではない。
        await RecordedScanTask.cleanupStaleWorkspaceForResolvedSymlink(
            str(first_target),
            first_target.resolve(),
            parents_seen,
        )

    asyncio.run(CleanupResolvedTargets())

    assert parents_seen == {external_folder}
    assert cleaned_parents == [{external_folder}]


def test_batch_scan_does_not_recover_workspace_through_workspace_symlink(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """workspaceを指すsymlinkは録画とみなさず、実体job内からcleanupを開始しない。"""

    workspace_file = (
        tmp_path
        / CMAnalysisWorkspace.ROOT_DIRECTORY_NAME
        / '157-token'
        / 'prepared.cmwork.mkv'
    )
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_bytes(b'temporary')
    workspace_symlink = tmp_path / 'recordings' / 'program.mkv'
    workspace_symlink.parent.mkdir()
    workspace_symlink.symlink_to(workspace_file)
    cleaned_parents: list[set[pathlib.Path]] = []

    async def CleanupStale(parents: set[pathlib.Path]) -> None:
        cleaned_parents.append(parents)

    monkeypatch.setattr(CMAnalysisWorkspace, 'cleanupStaleInParents', CleanupStale)
    parents_seen: set[pathlib.Path] = set()

    asyncio.run(RecordedScanTask.cleanupStaleWorkspaceForResolvedSymlink(
        str(workspace_symlink),
        workspace_file.resolve(),
        parents_seen,
    ))

    assert parents_seen == set()
    assert cleaned_parents == []


def test_recorded_scan_ignores_ctime_only_changes() -> None:
    """権限変更などでctimeだけが変わっても、録画本体を再解析しない。"""

    summary = CreateSummary()
    summary.file_created_at += timedelta(days=1)

    assert summary.isFileContentUnchanged(summary.file_modified_at, summary.file_size) is True


def test_recorded_scan_detects_content_changes_and_recording() -> None:
    """mtime・サイズ変更と録画中のファイルは従来どおり解析対象にする。"""

    summary = CreateSummary()
    assert summary.isFileContentUnchanged(summary.file_modified_at + timedelta(seconds=1), summary.file_size) is False
    assert summary.isFileContentUnchanged(summary.file_modified_at, summary.file_size + 1) is False

    summary.status = 'Recording'
    assert summary.isFileContentUnchanged(summary.file_modified_at, summary.file_size) is False

    summary.status = 'Analyzing'
    assert summary.isFileContentUnchanged(summary.file_modified_at, summary.file_size) is False


def test_thumbnail_history_uses_persisted_recorded_video_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """メタデータ解析用の仮 ID ではなく、DB 保存後の確定 ID を履歴へ渡す。"""

    captured: dict[str, Any] = {}

    class FakeHistory:
        async def setStage(self, stage: str, progress: float | None = None) -> None:
            del stage, progress

    class FakeThumbnailGenerator:
        async def generateAndSave(self) -> None:
            captured['generated'] = True

    class AsyncNullContext:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *args: Any) -> None:
            del args

    @asynccontextmanager
    async def Track(
        cls: type[AnalysisTaskTracker],
        task_type: str,
        **kwargs: Any,
    ) -> AsyncGenerator[FakeHistory, None]:
        del cls
        captured['task_type'] = task_type
        captured.update(kwargs)
        yield FakeHistory()

    recorded_program = SimpleNamespace(
        title='サンプル番組',
        recorded_video=SimpleNamespace(
            id=-1,
            file_path='/recorded/sample.ts',
            has_video=True,
        ),
    )
    scan_task = object.__new__(RecordedScanTask)
    scan_task._background_tasks = {}  # type: ignore[attr-defined]

    def FromRecordedProgram(cls: type[ThumbnailGenerator], program: Any) -> FakeThumbnailGenerator:
        del cls, program
        return FakeThumbnailGenerator()

    def GetProcessSemaphore(cls: type[ProcessLimiter], key: str) -> AsyncNullContext:
        del cls, key
        return AsyncNullContext()

    def GetDriveSemaphore(cls: type[DriveIOLimiter], path: Any) -> AsyncNullContext:
        del cls, path
        return AsyncNullContext()

    monkeypatch.setattr(AnalysisTaskTracker, 'track', classmethod(Track))
    monkeypatch.setattr(ThumbnailGenerator, 'fromRecordedProgram', classmethod(FromRecordedProgram))
    monkeypatch.setattr(ProcessLimiter, 'getSemaphore', classmethod(GetProcessSemaphore))
    monkeypatch.setattr(DriveIOLimiter, 'getSemaphore', classmethod(GetDriveSemaphore))

    run_thumbnail_generation = getattr(scan_task, '_RecordedScanTask__runThumbnailGeneration')
    asyncio.run(run_thumbnail_generation(recorded_program, 157))

    assert captured['task_type'] == 'ThumbnailGeneration'
    assert captured['recorded_video_id'] == 157
    assert captured['recorded_video_id'] != recorded_program.recorded_video.id
    assert captured['generated'] is True


@pytest.mark.parametrize(
    ('chapter_name', 'recorded_paths', 'expected_recorded_video_id'),
    [
        # 完全ファイル名方式とは解釈せず、基本名が program.ts の録画だけへ対応付ける。
        ('program.ts.chapter.txt', ('program.ts', 'program.ts.mkv'), 2),
        # 基本名が一致する録画が1件だけなら外部入力として同期する。
        ('program.chapter.txt', ('program.mkv',), 1),
        # DB保存パスの拡張子が大文字でも、基本名をcase-insensitiveに対応付ける。
        ('program.chapter.txt', ('program.TS',), 1),
        # chapter削除イベントでも同じ対応付けを行い、存在確認はOrchestratorへ委ねる。
        ('deleted.chapter.txt', ('deleted.ts',), 1),
    ],
)
def test_chapter_watcher_selects_unique_basic_name(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    chapter_name: str,
    recorded_paths: tuple[str, ...],
    expected_recorded_video_id: int,
) -> None:
    """chapter watcherは基本名が一意な.chapter.txtと、その削除イベントだけを同期する。"""

    recorded_videos = [
        SimpleNamespace(id=index, file_path=str(tmp_path / recorded_path))
        for index, recorded_path in enumerate(recorded_paths, start=1)
    ]
    called: list[tuple[int, str]] = []

    class FakeQuerySet:
        async def all(self) -> list[SimpleNamespace]:
            return recorded_videos

    def Filter(cls: type[RecordedVideo], **kwargs: Any) -> FakeQuerySet:
        del cls, kwargs
        return FakeQuerySet()

    async def GetOrNone(cls: type[RecordedVideo], **kwargs: Any) -> None:
        del cls, kwargs
        return None

    async def ToThread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    async def Run(self: CMAnalysisOrchestrator, recorded_video_id: int, intent: str) -> None:
        del self
        called.append((recorded_video_id, intent))

    def Debug(message: str) -> None:
        del message

    monkeypatch.setattr(RecordedVideo, 'filter', classmethod(Filter))
    monkeypatch.setattr(RecordedVideo, 'get_or_none', classmethod(GetOrNone))
    monkeypatch.setattr(CMAnalysisOrchestrator, 'run', Run)
    monkeypatch.setattr(asyncio, 'to_thread', ToThread)
    monkeypatch.setattr(logging, 'debug', Debug)
    scan_task = object.__new__(RecordedScanTask)

    handle_chapter_file_change = getattr(scan_task, '_RecordedScanTask__handleChapterFileChange')
    asyncio.run(asyncio.wait_for(
        handle_chapter_file_change(tmp_path / chapter_name),
        timeout=1.0,
    ))

    assert called == [(expected_recorded_video_id, 'CMChapterSync')]


@pytest.mark.parametrize(
    ('chapter_name', 'recorded_paths'),
    [
        # 同じ基本名の録画が複数ある場合は曖昧なので同期しない。
        ('program.chapter.txt', ('program.ts', 'program.mkv')),
        # 旧完全ファイル名方式は program.ts 自体へ対応付けない。
        ('program.ts.chapter.txt', ('program.ts',)),
        # 旧KonomiTV suffixは新形式へ暗黙変換せず同期しない。
        ('program.ts.konomitv-chapters.yaml', ('program.ts',)),
    ],
)
def test_chapter_watcher_ignores_ambiguous_or_full_filename_chapter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    chapter_name: str,
    recorded_paths: tuple[str, ...],
) -> None:
    """曖昧な基本名と旧完全ファイル名方式の.chapter.txtは同期しない。"""

    recorded_videos = [
        SimpleNamespace(id=index, file_path=str(tmp_path / recorded_path))
        for index, recorded_path in enumerate(recorded_paths, start=1)
    ]
    called: list[tuple[int, str]] = []

    class FakeQuerySet:
        async def all(self) -> list[SimpleNamespace]:
            return recorded_videos

    def Filter(cls: type[RecordedVideo], **kwargs: Any) -> FakeQuerySet:
        del cls, kwargs
        return FakeQuerySet()

    async def GetOrNone(cls: type[RecordedVideo], **kwargs: Any) -> None:
        del cls, kwargs
        return None

    async def ToThread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    async def Run(self: CMAnalysisOrchestrator, recorded_video_id: int, intent: str) -> None:
        del self
        called.append((recorded_video_id, intent))

    def Debug(message: str) -> None:
        del message

    monkeypatch.setattr(RecordedVideo, 'filter', classmethod(Filter))
    monkeypatch.setattr(RecordedVideo, 'get_or_none', classmethod(GetOrNone))
    monkeypatch.setattr(CMAnalysisOrchestrator, 'run', Run)
    monkeypatch.setattr(asyncio, 'to_thread', ToThread)
    monkeypatch.setattr(logging, 'debug', Debug)
    scan_task = object.__new__(RecordedScanTask)

    handle_chapter_file_change = getattr(scan_task, '_RecordedScanTask__handleChapterFileChange')
    asyncio.run(asyncio.wait_for(
        handle_chapter_file_change(tmp_path / chapter_name),
        timeout=1.0,
    ))

    assert called == []


def test_chapter_watcher_maps_konomitv_bs4k_yaml_by_complete_filename(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KonomiTV-BS4K YAMLは同じ基本名の別録画があっても完全ファイル名一致で同期する。"""

    recorded_videos = {
        str(tmp_path / 'program.ts'): SimpleNamespace(id=1, file_path=str(tmp_path / 'program.ts')),
        str(tmp_path / 'program.ts.mkv'): SimpleNamespace(id=2, file_path=str(tmp_path / 'program.ts.mkv')),
    }
    queried_paths: list[str] = []
    called: list[tuple[int, str]] = []

    async def GetOrNone(cls: type[RecordedVideo], **kwargs: Any) -> SimpleNamespace | None:
        del cls
        queried_paths.append(kwargs['file_path'])
        return recorded_videos.get(kwargs['file_path'])

    async def Run(self: CMAnalysisOrchestrator, recorded_video_id: int, intent: str) -> None:
        del self
        called.append((recorded_video_id, intent))

    monkeypatch.setattr(RecordedVideo, 'get_or_none', classmethod(GetOrNone))
    monkeypatch.setattr(CMAnalysisOrchestrator, 'run', Run)
    scan_task = object.__new__(RecordedScanTask)

    handle_chapter_file_change = getattr(scan_task, '_RecordedScanTask__handleChapterFileChange')
    asyncio.run(asyncio.wait_for(
        handle_chapter_file_change(tmp_path / 'program.ts.konomitv-bs4k-chapters.yaml'),
        timeout=1.0,
    ))

    assert queried_paths == [str(tmp_path / 'program.ts')]
    assert called == [(1, 'CMChapterSync')]


@pytest.mark.parametrize('change_type', [Change.added, Change.modified, Change.deleted])
def test_recorded_folder_watcher_routes_all_konomitv_bs4k_yaml_events(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    change_type: Change,
) -> None:
    """KonomiTV-BS4K YAMLの追加・変更・削除を録画拡張子フィルターより先にchapter処理へ渡す。"""

    chapter_path = tmp_path / 'program.ts.konomitv-bs4k-chapters.yaml'
    handled_paths: list[pathlib.Path] = []

    async def Watch(*args: Any, **kwargs: Any) -> AsyncGenerator[set[tuple[Change, str]], None]:
        del args, kwargs
        yield {(change_type, str(chapter_path))}
        scan_task._is_running = False  # type: ignore[attr-defined]

    async def CheckRecordingCompletion(self: RecordedScanTask) -> None:
        del self
        await asyncio.Event().wait()

    async def HandleChapterFileChange(self: RecordedScanTask, file_path: anyio.Path) -> None:
        del self
        handled_paths.append(pathlib.Path(str(file_path)))

    async def IsDirectory(path: anyio.Path) -> bool:
        del path
        return False

    async def ResolveRecordedPath(path: anyio.Path) -> anyio.Path:
        """ThreadPoolExecutorへ依存せず、watcher routingだけを検証する。"""

        return path

    monkeypatch.setattr('app.metadata.RecordedScanTask.awatch', Watch)
    monkeypatch.setattr(anyio.Path, 'is_dir', IsDirectory)
    monkeypatch.setattr(RecordedScanTask, 'resolveRecordedPath', staticmethod(ResolveRecordedPath))
    monkeypatch.setattr(
        RecordedScanTask,
        '_RecordedScanTask__checkRecordingCompletion',
        CheckRecordingCompletion,
    )
    monkeypatch.setattr(
        RecordedScanTask,
        '_RecordedScanTask__handleChapterFileChange',
        HandleChapterFileChange,
    )
    scan_task = object.__new__(RecordedScanTask)
    scan_task.recorded_folders = [anyio.Path(tmp_path)]  # type: ignore[attr-defined]
    scan_task.config = SimpleNamespace(video=SimpleNamespace(exclude_scan_paths=[]))  # type: ignore[attr-defined]
    scan_task._is_running = True  # type: ignore[attr-defined]

    asyncio.run(asyncio.wait_for(scan_task.watchRecordedFolders(), timeout=1.0))

    assert handled_paths == [chapter_path]


@pytest.mark.parametrize(
    ('path', 'pattern', 'expected'),
    [
        ('/recordings/temp', '/recordings/temp', True),
        ('/recordings/temp/', '/recordings/temp', True),
        ('/recordings/temp/file.ts', '/recordings/temp', True),
        ('/recordings/temporary', '/recordings/temp', False),
        ('/recordings/temporary/file.ts', '/recordings/temp', False),
        ('/recordings/temp-backup', '/recordings/temp', False),
        ('/recordings/other', '/recordings/temp', False),
    ],
)
def test_exclude_scan_paths_respect_path_component_boundary(path: str, pattern: str, expected: bool) -> None:
    """exact / child / prefix sibling / trailing slash を境界付き除外で検証する。"""

    assert RecordedScanTask.isPathExcludedByPatterns(path, [pattern]) is expected


def test_exclude_scan_paths_empty_pattern_never_matches() -> None:
    """空パターンは全パス除外にならないこと。"""

    assert RecordedScanTask.isPathExcludedByPatterns('/recordings/a.ts', ['']) is False
    assert RecordedScanTask.isPathExcludedByPatterns('/recordings/a.ts', ['   ']) is False


def test_resolve_recorded_path_rebases_host_absolute_symlink(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker-like namespace で host absolute symlink を /host-rootfs へ rebase する。"""

    host_root = tmp_path / 'host-rootfs'
    real_dir = host_root / 'mnt' / 'archive'
    real_dir.mkdir(parents=True)
    real_file = real_dir / 'program.ts'
    real_file.write_bytes(b'ts')

    link_dir = tmp_path / 'recorded'
    link_dir.mkdir()
    link_path = link_dir / 'program.ts'
    # host 絶対 path を指す symlink（コンテナ root 基準だと存在しない）
    link_path.symlink_to('/mnt/archive/program.ts')

    monkeypatch.setattr(RecordedScanTask, 'DOCKER_HOST_ROOTFS', host_root)

    resolved = asyncio.run(RecordedScanTask.resolveRecordedPath(anyio.Path(link_path)))
    assert pathlib.Path(str(resolved)).resolve() == real_file.resolve()


def test_resolve_recorded_path_keeps_broken_and_relative_symlink(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """broken / relative / cycle を安全に扱うこと。"""

    host_root = tmp_path / 'host-rootfs'
    host_root.mkdir()
    monkeypatch.setattr(RecordedScanTask, 'DOCKER_HOST_ROOTFS', host_root)

    base = tmp_path / 'links'
    base.mkdir()
    relative_target = base / 'real.ts'
    relative_target.write_bytes(b'x')
    relative_link = base / 'relative.ts'
    relative_link.symlink_to('real.ts')

    broken_link = base / 'broken.ts'
    broken_link.symlink_to('/does/not/exist.ts')

    cycle_a = base / 'cycle-a.ts'
    cycle_b = base / 'cycle-b.ts'
    cycle_a.symlink_to(cycle_b)
    cycle_b.symlink_to(cycle_a)

    relative_resolved = asyncio.run(RecordedScanTask.resolveRecordedPath(anyio.Path(relative_link)))
    assert pathlib.Path(str(relative_resolved)).resolve() == relative_target.resolve()

    broken_resolved = asyncio.run(RecordedScanTask.resolveRecordedPath(anyio.Path(broken_link)))
    assert str(broken_resolved) == str(broken_link) or not pathlib.Path(str(broken_resolved)).exists()

    cycle_resolved = asyncio.run(RecordedScanTask.resolveRecordedPath(anyio.Path(cycle_a)))
    # cycle は例外にせず何らかの path を返す
    assert str(cycle_resolved)


def test_periodic_reconciliation_registers_without_watcher_and_skips_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """watcher event なしでも次周期の reconciliation で batch scan が走り、batch 中は skip すること。"""

    task = RecordedScanTask.__new__(RecordedScanTask)
    task._is_running = True
    task._is_batch_scan_running = False
    scan_calls: list[str] = []
    skip_logs: list[str] = []
    sleep_count = 0

    async def FakeSleep(_seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        # 1 周期目: batch 中で skip、2 周期目: scan 実行、3 周期目で停止
        if sleep_count == 1:
            task._is_batch_scan_running = True
        elif sleep_count == 2:
            task._is_batch_scan_running = False
        elif sleep_count >= 3:
            task._is_running = False

    async def FakeBatchScan() -> None:
        scan_calls.append('scan')

    def CaptureDebug(message: str, *args: Any, **kwargs: Any) -> None:
        skip_logs.append(message % args if args else message)

    monkeypatch.setattr(asyncio, 'sleep', FakeSleep)
    monkeypatch.setattr(task, 'runBatchScan', FakeBatchScan)
    # Config 未初期化でも通るよう logging を差し替える
    monkeypatch.setattr(logging, 'info', lambda *args, **kwargs: None)
    monkeypatch.setattr(logging, 'debug', CaptureDebug)
    monkeypatch.setattr(logging, 'error', lambda *args, **kwargs: None)

    asyncio.run(task._RecordedScanTask__runPeriodicReconciliation())  # pyright: ignore[reportPrivateUsage]

    # batch 中の 1 周期は skip され、batch 終了後の 1 回だけ scan される
    assert scan_calls == ['scan']
    assert any('Skipping periodic reconciliation' in message for message in skip_logs)
    assert sleep_count >= 3


def test_recording_completion_uses_snapshot_and_defers_replaced_or_added_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stat()中の追加・削除・差し替えで巡回を壊さず、新しいentryを次周期に処理する。"""

    scan_task = object.__new__(RecordedScanTask)
    first_path = anyio.Path('/recordings/first.ts')
    removed_path = anyio.Path('/recordings/removed.ts')
    added_path = anyio.Path('/recordings/added.ts')
    old_timestamp = datetime.now(tz=JST) - timedelta(minutes=5)
    first_original = FileRecordingInfo(old_timestamp, old_timestamp, 100, None)
    removed_info = FileRecordingInfo(old_timestamp, old_timestamp, 200, None)
    first_replacement = FileRecordingInfo(old_timestamp, old_timestamp, 101, None)
    added_info = FileRecordingInfo(old_timestamp, old_timestamp, 300, None)
    scan_task._recording_files = {
        first_path: first_original,
        removed_path: removed_info,
    }
    scan_task._is_running = True
    mutation_performed = False
    sleep_count = 0
    processed_paths: list[anyio.Path] = []

    async def Stat(path: anyio.Path) -> SimpleNamespace:
        nonlocal mutation_performed
        if path == first_path and mutation_performed is False:
            mutation_performed = True
            # await stat()中にwatcherが既存entryを差し替え、別entryを削除・追加する競合を再現する。
            scan_task._recording_files[first_path] = first_replacement
            scan_task._recording_files.pop(removed_path)
            scan_task._recording_files[added_path] = added_info
        expected_sizes = {
            first_path: 101,
            removed_path: 200,
            added_path: 300,
        }
        return SimpleNamespace(st_mtime=old_timestamp.timestamp(), st_size=expected_sizes[path])

    async def IsFileExists(_path: anyio.Path) -> bool:
        return True

    async def ProcessRecordedFile(path: anyio.Path) -> None:
        processed_paths.append(path)

    async def Sleep(_seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        # 1周期目はsnapshot競合を処理し、2周期目で新しいentryを完了させて停止する。
        if sleep_count >= 2:
            scan_task._is_running = False

    monkeypatch.setattr(anyio.Path, 'stat', Stat)
    monkeypatch.setattr(scan_task, 'isFileExists', IsFileExists)
    monkeypatch.setattr(scan_task, 'processRecordedFile', ProcessRecordedFile)
    monkeypatch.setattr(asyncio, 'sleep', Sleep)

    asyncio.run(scan_task._RecordedScanTask__checkRecordingCompletion())  # pyright: ignore[reportPrivateUsage]

    assert mutation_performed is True
    assert processed_paths == [first_path, added_path]
    assert removed_path not in processed_paths
    assert scan_task._recording_files == {}
    assert sleep_count == 2


def test_file_lock_registry_tracks_holder_waiters_cancel_and_unique_paths() -> None:
    """holder・waiter・cancel後に同一entryを保ち、最後の解放後はregistryを空へ戻す。"""

    scan_task = object.__new__(RecordedScanTask)
    target_path = anyio.Path('/recordings/shared.ts')

    async def Run() -> None:
        scan_task._file_locks = {}
        scan_task._file_locks_dict_lock = asyncio.Lock()
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()
        successful_waiter_entered = asyncio.Event()

        async def Holder() -> None:
            async with scan_task.fileLock(target_path):
                holder_entered.set()
                await release_holder.wait()

        async def CancelledWaiter() -> None:
            async with scan_task.fileLock(target_path):
                raise AssertionError('cancelled waiter acquired the lock unexpectedly')

        async def SuccessfulWaiter() -> None:
            async with scan_task.fileLock(target_path):
                successful_waiter_entered.set()

        holder_task = asyncio.create_task(Holder())
        await holder_entered.wait()
        cancelled_waiter_task = asyncio.create_task(CancelledWaiter())
        successful_waiter_task = asyncio.create_task(SuccessfulWaiter())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        entry = scan_task._file_locks[target_path]
        assert entry.reference_count == 3
        assert entry.lock.locked() is True

        cancelled_waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter_task
        assert scan_task._file_locks[target_path] is entry
        assert entry.reference_count == 2

        release_holder.set()
        await holder_task
        await successful_waiter_task
        assert successful_waiter_entered.is_set() is True
        assert scan_task._file_locks == {}

        # 大量の一意pathを順次処理してもregistryが単調増加しないことを確認する。
        for index in range(100):
            unique_path = anyio.Path(f'/recordings/unique-{index}.ts')
            async with scan_task.fileLock(unique_path):
                assert scan_task._file_locks[unique_path].reference_count == 1
            assert scan_task._file_locks == {}

    asyncio.run(Run())
