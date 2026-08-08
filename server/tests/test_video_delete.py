from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from fastapi import HTTPException

from app.metadata.RecordedScanTask import RecordedScanTask
from app.routers import VideosRouter


@pytest.mark.parametrize(
    'failure_stage',
    ['thumbnail', 'tile-thumbnail', 'program-information', 'recording-error', 'recorded-video', 'database'],
)
def test_video_delete_failure_preserves_record_for_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_stage: str,
) -> None:
    """各削除段階の一時的な失敗後もDBレコードを保持し、同じAPIで再試行できることを検証する。"""

    thumbnails_dir = tmp_path / 'thumbnails'
    thumbnails_dir.mkdir()
    video_path = tmp_path / 'recorded.ts'
    file_hash = 'recorded-file-hash'
    deletion_paths = {
        'thumbnail': thumbnails_dir / f'{file_hash}.webp',
        'tile-thumbnail': thumbnails_dir / f'{file_hash}_tile.webp',
        'program-information': Path(f'{video_path}.program.txt'),
        'recording-error': Path(f'{video_path}.err'),
        'recorded-video': video_path,
    }
    for deletion_path in deletion_paths.values():
        deletion_path.write_bytes(b'test')

    recorded_video = SimpleNamespace(
        id=10,
        status='Recorded',
        file_path=str(video_path),
        file_hash=file_hash,
    )
    status_history: list[str] = []
    database_failure_injected = False

    class FakeRecordedProgram:
        def __init__(self) -> None:
            self.id = 20
            self.recorded_video = recorded_video
            self.deleted = False

        async def delete(self) -> None:
            nonlocal database_failure_injected
            if failure_stage == 'database' and database_failure_injected is False:
                database_failure_injected = True
                raise OSError('injected database failure')
            self.deleted = True

    recorded_program = FakeRecordedProgram()

    class FakeRecordedVideoQuery:
        excluded_status: str | None = None

        def exclude(self, *, status: str) -> FakeRecordedVideoQuery:
            self.excluded_status = status
            return self

        async def update(self, *, status: str) -> int:
            if self.excluded_status is not None and recorded_video.status == self.excluded_status:
                return 0
            recorded_video.status = status
            status_history.append(status)
            return 1

    class FakeDuplicateQuery:
        def exclude(self, *, id: int) -> FakeDuplicateQuery:
            assert id == recorded_program.id
            return self

        async def count(self) -> int:
            return 0

    class FakeRecordedScanTask:
        async def resolveRecordedPath(self, file_path: anyio.Path) -> anyio.Path:
            return file_path

        @asynccontextmanager
        async def fileLock(self, file_path: anyio.Path) -> AsyncGenerator[None, None]:
            del file_path
            yield

    monkeypatch.setattr(VideosRouter, 'THUMBNAILS_DIR', thumbnails_dir)
    monkeypatch.setattr(VideosRouter, 'RecordedScanTask', FakeRecordedScanTask)
    monkeypatch.setattr(VideosRouter.RecordedVideo, 'filter', lambda **_conditions: FakeRecordedVideoQuery())
    monkeypatch.setattr(VideosRouter.RecordedProgram, 'filter', lambda **_conditions: FakeDuplicateQuery())

    original_unlink = anyio.Path.unlink
    unlink_failure_injected = False

    async def Unlink(path: anyio.Path, *args, **kwargs) -> None:
        nonlocal unlink_failure_injected
        failure_path = deletion_paths.get(failure_stage)
        if failure_path is not None and Path(str(path)) == failure_path and unlink_failure_injected is False:
            unlink_failure_injected = True
            raise OSError('injected unlink failure')
        await original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(anyio.Path, 'unlink', Unlink)

    with pytest.raises(HTTPException) as ex_info:
        asyncio.run(VideosRouter.VideoDeleteAPI(recorded_program, SimpleNamespace()))  # type: ignore[arg-type]

    assert ex_info.value.status_code == 500
    assert recorded_program.deleted is False
    assert recorded_video.status == 'DeleteFailed'

    asyncio.run(VideosRouter.VideoDeleteAPI(recorded_program, SimpleNamespace()))  # type: ignore[arg-type]

    assert recorded_program.deleted is True
    assert all(deletion_path.exists() is False for deletion_path in deletion_paths.values())
    assert status_history == ['Deleting', 'DeleteFailed', 'Deleting']


def test_recorded_scan_start_recovers_interrupted_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    """前回プロセスのDeleting状態を起動時に再試行可能なDeleteFailedへ戻すことを検証する。"""

    recovered_statuses: list[str] = []

    class FakeRecordedVideoQuery:
        async def update(self, *, status: str) -> int:
            recovered_statuses.append(status)
            return 2

    monkeypatch.setattr(
        'app.metadata.RecordedScanTask.RecordedVideo.filter',
        lambda **conditions: FakeRecordedVideoQuery() if conditions == {'status': 'Deleting'} else pytest.fail(),
    )

    task = object.__new__(RecordedScanTask)
    task._is_running = False

    async def Run() -> None:
        return None

    task.run = Run  # type: ignore[method-assign]

    asyncio.run(task.start())

    assert recovered_statuses == ['DeleteFailed']
