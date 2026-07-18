# pyright: reportPrivateUsage=false

import asyncio
import errno
import fcntl
import json
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.metadata.CMAnalysisWorkspace import (
    CMAnalysisWorkspace,
    TemporaryStorageInsufficientError,
    TemporaryStorageUnavailableError,
)


@pytest.fixture(autouse=True)
def RunWorkspaceIOInline(monkeypatch: pytest.MonkeyPatch) -> None:
    """終了不能なhost ThreadPoolExecutorを避け、workspace I/O本体だけを検証する。"""

    async def RunInline(function, *args, **kwargs):  # type: ignore[no-untyped-def]
        return function(*args, **kwargs)

    monkeypatch.setattr('app.metadata.CMAnalysisWorkspace.asyncio.to_thread', RunInline)


def SetAvailableBytes(monkeypatch: pytest.MonkeyPatch, available_bytes: int) -> None:
    """statvfsの一般ユーザー向け空き容量を決定的な値へ差し替える。"""

    def StatVFS(path: Path) -> SimpleNamespace:
        del path
        return SimpleNamespace(f_bavail=available_bytes, f_frsize=1)

    monkeypatch.setattr(
        'app.metadata.CMAnalysisWorkspace.os.statvfs',
        StatVFS,
    )


def test_required_bytes_include_pcm_variable_headroom_and_fixed_reserve() -> None:
    source_size = 10 * 1024 * 1024 * 1024
    duration_seconds = 3600.25

    assert CMAnalysisWorkspace.calculateRequiredBytes(source_size, duration_seconds) == (
        source_size
        + 345_624_000
        + 512 * 1024 * 1024
        + 2 * 1024 * 1024 * 1024
    )
    assert CMAnalysisWorkspace.calculateRequiredBytes(1024, 0.0) == (
        1024
        + 128 * 1024 * 1024
        + 2 * 1024 * 1024 * 1024
    )


def test_workspace_uses_resolved_source_parent_and_cleans_empty_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_parent = tmp_path / 'real'
    real_parent.mkdir()
    source_path = real_parent / 'recording.ts'
    source_path.write_bytes(b'recording')
    linked_parent = tmp_path / 'linked'
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    SetAvailableBytes(monkeypatch, 16 * 1024 * 1024 * 1024)

    workspace = asyncio.run(CMAnalysisWorkspace.create(linked_parent / source_path.name, 157, 3600.0))

    assert workspace.path.parent == real_parent / CMAnalysisWorkspace.ROOT_DIRECTORY_NAME
    assert workspace.path.name.startswith('157-')
    assert stat.S_IMODE(workspace.path.stat().st_mode) == 0o700
    assert stat.S_IMODE(workspace.path.parent.stat().st_mode) == 0o700
    marker = json.loads((workspace.path / CMAnalysisWorkspace.JOB_MARKER_NAME).read_text())
    assert marker['recorded_video_id'] == 157
    assert marker['name'] == workspace.path.name

    second_lock_fd = os.open(workspace.path / CMAnalysisWorkspace.LOCK_FILE_NAME, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(second_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(second_lock_fd)

    root_path = workspace.path.parent
    asyncio.run(workspace.cleanup())
    assert workspace.path.exists() is False
    assert root_path.exists() is False


def test_startup_parent_resolution_follows_recording_file_symlink(tmp_path: Path) -> None:
    stored_parent = tmp_path / 'stored'
    stored_parent.mkdir()
    real_parent = tmp_path / 'real'
    real_parent.mkdir()
    source_path = real_parent / 'recording.ts'
    source_path.write_bytes(b'recording')
    stored_path = stored_parent / source_path.name
    stored_path.symlink_to(source_path)

    assert CMAnalysisWorkspace._resolveWorkspaceParents([str(stored_path)]) == {real_parent}


def test_workspace_creation_follows_recording_file_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored_parent = tmp_path / 'stored'
    stored_parent.mkdir()
    real_parent = tmp_path / 'real'
    real_parent.mkdir()
    real_source = real_parent / 'recording.mkv'
    real_source.write_bytes(b'recording')
    stored_source = stored_parent / real_source.name
    stored_source.symlink_to(real_source)
    SetAvailableBytes(monkeypatch, 16 * 1024 * 1024 * 1024)

    workspace = asyncio.run(CMAnalysisWorkspace.create(stored_source, 158, 60.0))

    assert workspace.path.parent == real_parent / CMAnalysisWorkspace.ROOT_DIRECTORY_NAME
    assert (stored_parent / CMAnalysisWorkspace.ROOT_DIRECTORY_NAME).exists() is False
    asyncio.run(workspace.cleanup())


def test_workspace_rejects_unowned_reserved_root_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / 'recording.mkv'
    source_path.write_bytes(b'recording')
    reserved_root = tmp_path / CMAnalysisWorkspace.ROOT_DIRECTORY_NAME
    reserved_root.mkdir()
    (reserved_root / 'user-data').write_text('preserve', encoding='utf-8')
    SetAvailableBytes(monkeypatch, 16 * 1024 * 1024 * 1024)

    with pytest.raises(TemporaryStorageUnavailableError):
        asyncio.run(CMAnalysisWorkspace.create(source_path, 159, 60.0))

    assert (reserved_root / 'user-data').read_text(encoding='utf-8') == 'preserve'
    assert list(tmp_path.glob('159-*')) == []


def test_workspace_rejects_insufficient_f_bavail_before_creating_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / 'recording.mkv'
    source_path.write_bytes(b'x' * 1024)
    SetAvailableBytes(monkeypatch, 1024)

    with pytest.raises(TemporaryStorageInsufficientError) as exception_info:
        asyncio.run(CMAnalysisWorkspace.create(source_path, 1, 60.0))

    assert exception_info.value.error_code == 'TemporaryStorageInsufficient'
    assert exception_info.value.available_bytes == 1024
    assert (tmp_path / CMAnalysisWorkspace.ROOT_DIRECTORY_NAME).exists() is False


def test_workspace_rechecks_f_bavail_while_holding_parent_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / 'recording.mkv'
    source_path.write_bytes(b'x' * 1024)
    available_values = iter((16 * 1024 * 1024 * 1024, 1024))

    def StatVFS(path: Path) -> SimpleNamespace:
        del path
        return SimpleNamespace(f_bavail=next(available_values), f_frsize=1)

    monkeypatch.setattr('app.metadata.CMAnalysisWorkspace.os.statvfs', StatVFS)

    with pytest.raises(TemporaryStorageInsufficientError) as exception_info:
        asyncio.run(CMAnalysisWorkspace.create(source_path, 1, 60.0))

    assert exception_info.value.available_bytes == 1024
    assert (tmp_path / CMAnalysisWorkspace.ROOT_DIRECTORY_NAME).exists() is False


def test_workspace_reports_unavailable_storage_without_tmp_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / 'recording.mkv'
    source_path.write_bytes(b'x')

    def RaiseUnavailable(path: Path) -> None:
        del path
        raise OSError('unavailable')

    monkeypatch.setattr('app.metadata.CMAnalysisWorkspace.os.statvfs', RaiseUnavailable)

    with pytest.raises(TemporaryStorageUnavailableError) as exception_info:
        asyncio.run(CMAnalysisWorkspace.create(source_path, 1, 60.0))

    assert exception_info.value.error_code == 'TemporaryStorageUnavailable'
    assert (tmp_path / CMAnalysisWorkspace.ROOT_DIRECTORY_NAME).exists() is False


def test_windows_reports_workspace_unavailable_without_import_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / 'recording.mkv'
    source_path.write_bytes(b'x')
    monkeypatch.setattr('app.metadata.CMAnalysisWorkspace._fcntl', None)

    with pytest.raises(TemporaryStorageUnavailableError, match='requires POSIX advisory locking'):
        asyncio.run(CMAnalysisWorkspace.create(source_path, 1, 60.0))

    # Startup/scanner cleanup is a no-op on the unsupported platform.
    asyncio.run(CMAnalysisWorkspace.cleanupStaleInParents({tmp_path}))
    assert (tmp_path / CMAnalysisWorkspace.ROOT_DIRECTORY_NAME).exists() is False


def test_workspace_creation_enospc_is_reported_as_insufficient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / 'recording.mkv'
    source_path.write_bytes(b'x')
    SetAvailableBytes(monkeypatch, 16 * 1024 * 1024 * 1024)

    def RaiseENOSPC(*args: object) -> None:
        del args
        raise OSError(errno.ENOSPC, 'No space left on device')

    monkeypatch.setattr(CMAnalysisWorkspace, '_createSynchronous', RaiseENOSPC)

    with pytest.raises(TemporaryStorageInsufficientError) as exception_info:
        asyncio.run(CMAnalysisWorkspace.create(source_path, 1, 60.0))

    assert exception_info.value.error_code == 'TemporaryStorageInsufficient'


def test_cancelled_creation_joins_worker_and_removes_returned_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / 'recording.ts'
    source_path.write_bytes(b'x')
    SetAvailableBytes(monkeypatch, 16 * 1024 * 1024 * 1024)
    created_workspaces: list[CMAnalysisWorkspace] = []

    async def RunScenario() -> None:
        creation_finished = asyncio.Event()
        permit_worker_return = asyncio.Event()

        async def DeferredToThread(function, *args, **kwargs):  # type: ignore[no-untyped-def]
            result = function(*args, **kwargs)
            if getattr(function, '__name__', '') == '_createSynchronous':
                assert isinstance(result, CMAnalysisWorkspace)
                created_workspaces.append(result)
                creation_finished.set()
                await permit_worker_return.wait()
            return result

        monkeypatch.setattr('app.metadata.CMAnalysisWorkspace.asyncio.to_thread', DeferredToThread)
        create_task = asyncio.create_task(CMAnalysisWorkspace.create(source_path, 4, 60.0))
        await creation_finished.wait()
        create_task.cancel()
        await asyncio.sleep(0)
        create_task.cancel()
        await asyncio.sleep(0)
        permit_worker_return.set()
        with pytest.raises(asyncio.CancelledError):
            await create_task

    asyncio.run(RunScenario())
    assert len(created_workspaces) == 1
    assert created_workspaces[0].path.exists() is False
    assert created_workspaces[0].path.parent.exists() is False
    with pytest.raises(OSError):
        os.fstat(created_workspaces[0]._lock_fd)


def test_startup_cleanup_preserves_live_job_and_removes_unlocked_stale_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / 'recording.ts'
    source_path.write_bytes(b'x')
    SetAvailableBytes(monkeypatch, 16 * 1024 * 1024 * 1024)
    workspace = asyncio.run(CMAnalysisWorkspace.create(source_path, 9, 60.0))
    root_path = workspace.path.parent

    asyncio.run(CMAnalysisWorkspace.cleanupStaleInParents({tmp_path}))
    assert workspace.path.is_dir() is True

    # 前プロセスが異常終了してfdだけ閉じられた状態を再現する。
    fcntl.flock(workspace._lock_fd, fcntl.LOCK_UN)
    os.close(workspace._lock_fd)
    workspace._closed = True
    asyncio.run(CMAnalysisWorkspace.cleanupStaleInParents({tmp_path}))

    assert workspace.path.exists() is False
    assert root_path.exists() is False


def test_normal_cleanup_keeps_root_while_another_job_is_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / 'recording.ts'
    source_path.write_bytes(b'x')
    SetAvailableBytes(monkeypatch, 16 * 1024 * 1024 * 1024)
    first = asyncio.run(CMAnalysisWorkspace.create(source_path, 1, 60.0))
    second = asyncio.run(CMAnalysisWorkspace.create(source_path, 2, 60.0))
    root_path = first.path.parent

    asyncio.run(first.cleanup())
    assert first.path.exists() is False
    assert second.path.is_dir() is True
    assert root_path.is_dir() is True

    asyncio.run(second.cleanup())
    assert root_path.exists() is False


def test_workspace_creation_waits_for_parent_directory_lock(tmp_path: Path) -> None:
    source_path = tmp_path / 'recording.ts'
    source_path.write_bytes(b'x')
    parent_lock_fd = CMAnalysisWorkspace._lockDirectory(tmp_path)
    worker_started = threading.Event()
    worker_finished = threading.Event()
    workspaces: list[CMAnalysisWorkspace] = []
    worker_errors: list[BaseException] = []

    def CreateWorkspace() -> None:
        worker_started.set()
        try:
            workspaces.append(CMAnalysisWorkspace._createSynchronous(tmp_path, 5, 1))
        except BaseException as ex:
            worker_errors.append(ex)
        finally:
            worker_finished.set()

    worker = threading.Thread(target=CreateWorkspace, daemon=True)
    worker.start()
    assert worker_started.wait(timeout=1.0) is True
    assert worker_finished.wait(timeout=0.05) is False
    CMAnalysisWorkspace._unlockDirectory(parent_lock_fd)
    assert worker_finished.wait(timeout=1.0) is True
    worker.join(timeout=1.0)

    assert worker_errors == []
    assert len(workspaces) == 1
    asyncio.run(workspaces[0].cleanup())


def test_cleanup_holds_lock_until_deletion_finishes_after_repeated_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / 'recording.ts'
    source_path.write_bytes(b'x')
    SetAvailableBytes(monkeypatch, 16 * 1024 * 1024 * 1024)
    workspace = asyncio.run(CMAnalysisWorkspace.create(source_path, 3, 60.0))

    async def RunScenario() -> None:
        deletion_started = asyncio.Event()
        permit_deletion = asyncio.Event()

        async def DeferredToThread(function, *args, **kwargs):  # type: ignore[no-untyped-def]
            if function is os.statvfs:
                return function(*args, **kwargs)
            if function.__module__ == 'shutil' and function.__name__ == 'rmtree':
                deletion_started.set()
                await permit_deletion.wait()
            return function(*args, **kwargs)

        monkeypatch.setattr('app.metadata.CMAnalysisWorkspace.asyncio.to_thread', DeferredToThread)
        cleanup_task = asyncio.create_task(workspace.cleanup())
        await deletion_started.wait()
        cleanup_task.cancel()
        await asyncio.sleep(0)
        cleanup_task.cancel()
        await asyncio.sleep(0)

        second_lock_fd = os.open(workspace.path / CMAnalysisWorkspace.LOCK_FILE_NAME, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(second_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(second_lock_fd)

        permit_deletion.set()
        with pytest.raises(asyncio.CancelledError):
            await cleanup_task

    asyncio.run(RunScenario())
    assert workspace.path.exists() is False


def test_startup_cleanup_does_not_follow_workspace_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / 'recording.ts'
    source_path.write_bytes(b'x')
    SetAvailableBytes(monkeypatch, 16 * 1024 * 1024 * 1024)
    workspace = asyncio.run(CMAnalysisWorkspace.create(source_path, 9, 60.0))
    root_path = workspace.path.parent
    asyncio.run(workspace.cleanup())

    external = tmp_path / 'external'
    external.mkdir()
    sentinel = external / 'sentinel'
    sentinel.write_text('keep')
    root_path.symlink_to(external, target_is_directory=True)

    asyncio.run(CMAnalysisWorkspace.cleanupStaleInParents({tmp_path}))

    assert root_path.is_symlink() is True
    assert sentinel.read_text() == 'keep'


@pytest.mark.parametrize(
    ('path', 'expected'),
    [
        ('/recordings/.konomitv-cm-analysis', True),
        ('/recordings/.konomitv-cm-analysis/1-token/media.cmwork', True),
        ('/recordings/.konomitv-cm-analysis-copy/media.mkv', False),
        ('/recordings/program.mkv', False),
    ],
)
def test_workspace_path_detection_uses_exact_path_component(path: str, expected: bool) -> None:
    assert CMAnalysisWorkspace.isWorkspacePath(Path(path)) is expected
