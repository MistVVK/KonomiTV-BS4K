import asyncio
import pathlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from watchfiles import Change

from app import logging
from app.constants import JST
from app.metadata.AnalysisTaskTracker import AnalysisTaskTracker
from app.metadata.CMAnalysisOrchestrator import CMAnalysisOrchestrator
from app.metadata.CMAnalysisWorkspace import CMAnalysisWorkspace
from app.metadata.RecordedScanTask import RecordedScanTask, RecordedVideoSummary
from app.metadata.ThumbnailGenerator import ThumbnailGenerator
from app.models.RecordedVideo import RecordedVideo
from app.utils.DriveIOLimiter import DriveIOLimiter
from app.utils.ProcessLimiter import ProcessLimiter


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
    assert cleaned_parents == [{nested_folder}]


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
        # canonical録画と、同じ名前をlegacyとして解釈できる録画が共存してもcanonicalを優先する。
        ('program.ts.chapter.txt', ('program.ts', 'program.ts.mkv'), 1),
        # canonical候補がDBになく、legacy候補が1件だけなら互換読込する。
        ('program.chapter.txt', ('program.mkv',), 1),
        # DB保存パスの拡張子が大文字でも、legacy名をcase-insensitiveに対応付ける。
        ('program.chapter.txt', ('program.TS',), 1),
        # chapter削除イベントでも同じ対応付けを行い、存在確認はOrchestratorへ委ねる。
        ('deleted.ts.chapter.txt', ('deleted.ts',), 1),
    ],
)
def test_chapter_watcher_selects_canonical_or_unique_legacy(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    chapter_name: str,
    recorded_paths: tuple[str, ...],
    expected_recorded_video_id: int,
) -> None:
    """chapter watcherはcanonicalを優先し、一意なlegacyと削除イベントだけを同期する。"""

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


def test_chapter_watcher_ignores_ambiguous_legacy(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同じlegacy chapterに対応する登録録画が複数あれば、どちらにも同期しない。"""

    recorded_videos = [
        SimpleNamespace(id=1, file_path=str(tmp_path / 'program.ts')),
        SimpleNamespace(id=2, file_path=str(tmp_path / 'program.mkv')),
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
        handle_chapter_file_change(tmp_path / 'program.chapter.txt'),
        timeout=1.0,
    ))

    assert called == []
