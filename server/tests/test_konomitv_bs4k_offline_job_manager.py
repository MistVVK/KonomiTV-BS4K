import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

import pytest

from app import schemas
from app.streams import KonomiTVBS4KOfflineJobManager as offline_job_manager_module
from app.streams.KonomiTVBS4KOfflineJobManager import (
    KonomiTVBS4KOfflineJobManager,
    KonomiTVBS4KOfflineJobNotFoundError,
    KonomiTVBS4KOfflineJobNotReadyError,
)


class _FakeRecordedStream:
    """ジョブ所有権と cleanup だけを検証する最小録画ストリーム。"""

    def __init__(self) -> None:
        self.destroy_count = 0
        self.keep_alive_count = 0

    def keepAlive(self) -> None:
        self.keep_alive_count += 1

    async def destroy(self) -> None:
        self.destroy_count += 1


class _FakeOfflineStream:
    """テストごとに差し替えた生成関数を独自パッケージとして公開する。"""

    generate: Callable[[], AsyncGenerator[bytes]]

    def __init__(self, _video_stream, _metadata, progress_callback=None) -> None:
        self.progress_callback = progress_callback

    async def Generate(self) -> AsyncGenerator[bytes]:
        if self.progress_callback is not None:
            self.progress_callback(schemas.KonomiTVBS4KOfflineStreamProgress(
                active=True,
                completed_assets=0,
                total_assets=2,
                completed_bytes=0,
                progress=0.25,
            ))
        async for chunk in _FakeOfflineStream.generate():
            yield chunk
        if self.progress_callback is not None:
            self.progress_callback(schemas.KonomiTVBS4KOfflineStreamProgress(
                active=True,
                completed_assets=2,
                total_assets=2,
                completed_bytes=7,
                progress=1.0,
            ))


@pytest.fixture
def offline_jobs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """各テストを独立した永続ディレクトリと manager 状態で実行する。"""

    async def RunSynchronously(function, *args, **kwargs):  # type: ignore[no-untyped-def]
        """終了不能な host ThreadPoolExecutor を避け、同期 I/O 本体だけを検証する。"""

        return function(*args, **kwargs)

    monkeypatch.setattr(KonomiTVBS4KOfflineJobManager, 'JOBS_DIR', tmp_path)
    monkeypatch.setattr(offline_job_manager_module, 'KonomiTVBS4KOfflineStream', _FakeOfflineStream)
    monkeypatch.setattr(offline_job_manager_module.asyncio, 'to_thread', RunSynchronously)
    KonomiTVBS4KOfflineJobManager._jobs = {}
    KonomiTVBS4KOfflineJobManager._tasks = {}
    KonomiTVBS4KOfflineJobManager._semaphore = None
    KonomiTVBS4KOfflineJobManager._mutation_lock = None
    KonomiTVBS4KOfflineJobManager._is_initialized = False
    KonomiTVBS4KOfflineJobManager._is_stopping = False
    return tmp_path


def _BuildMetadata(video_id: int) -> schemas.KonomiTVBS4KOfflineStreamMetadata:
    """manager テストで共通利用する exact 生成条件を返す。"""

    return schemas.KonomiTVBS4KOfflineStreamMetadata(
        video_id=video_id,
        file_hash='0123456789abcdef',
        quality='240p',
        video_codec='av1',
        video_bit_depth=10,
        requested_audio_codec='opus',
        audio_codec='opus',
    )


def test_offline_job_publishes_only_complete_atomic_package(offline_jobs_dir: Path) -> None:
    """終端生成後の fsync・rename が終わるまで download パスを公開しない。"""

    async def Run() -> None:
        first_chunk_written = asyncio.Event()
        continue_generation = asyncio.Event()
        recorded_stream = _FakeRecordedStream()

        async def Generate() -> AsyncGenerator[bytes]:
            yield b'header-'
            first_chunk_written.set()
            await continue_generation.wait()
            yield b'payload'

        _FakeOfflineStream.generate = Generate

        async def CreateStream(_job_id: str):
            return recorded_stream, _BuildMetadata(42)

        job = await KonomiTVBS4KOfflineJobManager.createJob(42, CreateStream)
        await first_chunk_written.wait()
        with pytest.raises(KonomiTVBS4KOfflineJobNotReadyError):
            await KonomiTVBS4KOfflineJobManager.getDownloadPath(42, job.job_id)
        assert (offline_jobs_dir / f'{job.job_id}.package').exists() is False

        continue_generation.set()
        await asyncio.gather(*list(KonomiTVBS4KOfflineJobManager._tasks.values()))
        ready = await KonomiTVBS4KOfflineJobManager.getJob(42, job.job_id)
        package_path, package_size = await KonomiTVBS4KOfflineJobManager.getDownloadPath(42, job.job_id)

        assert ready.state == 'Ready'
        assert ready.phase == 'Ready'
        assert ready.progress == 1.0
        assert ready.total_assets == 2
        assert ready.completed_assets == 2
        assert package_size == len(b'header-payload')
        assert package_path.read_bytes() == b'header-payload'
        assert recorded_stream.destroy_count == 1

    asyncio.run(Run())


def test_offline_job_failure_never_publishes_partial_package(offline_jobs_dir: Path) -> None:
    """生成途中の例外を Failed へ確定し、書きかけのパッケージを回収する。"""

    async def Run() -> None:
        async def Generate() -> AsyncGenerator[bytes]:
            yield b'partial'
            raise RuntimeError('synthetic generation failure')

        _FakeOfflineStream.generate = Generate

        async def CreateStream(_job_id: str):
            return _FakeRecordedStream(), _BuildMetadata(7)

        job = await KonomiTVBS4KOfflineJobManager.createJob(7, CreateStream)
        await asyncio.gather(*list(KonomiTVBS4KOfflineJobManager._tasks.values()))
        failed = await KonomiTVBS4KOfflineJobManager.getJob(7, job.job_id)

        assert failed.state == 'Failed'
        assert failed.phase == 'Failed'
        assert failed.error is not None
        assert (offline_jobs_dir / f'{job.job_id}.package').exists() is False
        assert list(offline_jobs_dir.glob('.*.tmp')) == []

    asyncio.run(Run())


def test_offline_job_delete_cancels_generation_and_removes_state(offline_jobs_dir: Path) -> None:
    """DELETE は生成 Task と一時世代を止め、同じジョブを以後参照不能にする。"""

    async def Run() -> None:
        generation_started = asyncio.Event()

        async def Generate() -> AsyncGenerator[bytes]:
            generation_started.set()
            await asyncio.Event().wait()
            yield b'unreachable'

        _FakeOfflineStream.generate = Generate

        async def CreateStream(_job_id: str):
            return _FakeRecordedStream(), _BuildMetadata(8)

        job = await KonomiTVBS4KOfflineJobManager.createJob(8, CreateStream)
        await generation_started.wait()
        await KonomiTVBS4KOfflineJobManager.deleteJob(8, job.job_id)

        with pytest.raises(KonomiTVBS4KOfflineJobNotFoundError):
            await KonomiTVBS4KOfflineJobManager.getJob(8, job.job_id)
        assert list(offline_jobs_dir.iterdir()) == []

    asyncio.run(Run())


def test_offline_job_startup_marks_interrupted_generation_failed(offline_jobs_dir: Path) -> None:
    """再起動時は生成途中を再開せず Failed とし、一時ファイルだけを削除する。"""

    job_id = 'a' * 32
    current_time = time.time()
    interrupted = schemas.KonomiTVBS4KOfflineJob(
        job_id=job_id,
        video_id=9,
        state='Generating',
        phase='Encoding',
        progress=0.4,
        completed_assets=4,
        total_assets=10,
        completed_bytes=100,
        package_size_bytes=None,
        error=None,
        created_at=current_time,
        updated_at=current_time,
    )
    KonomiTVBS4KOfflineJobManager._writeStatusSync(interrupted)
    temporary_path = offline_jobs_dir / f'.{job_id}.package-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.tmp'
    temporary_path.write_bytes(b'partial')
    unpublished_package_path = offline_jobs_dir / f'{job_id}.package'
    unpublished_package_path.write_bytes(b'unpublished')

    async def Run() -> None:
        await KonomiTVBS4KOfflineJobManager.initialize()
        recovered = await KonomiTVBS4KOfflineJobManager.getJob(9, job_id)
        assert recovered.state == 'Failed'
        assert recovered.phase == 'Failed'
        assert recovered.error is not None

    asyncio.run(Run())
    assert temporary_path.exists() is False
    assert unpublished_package_path.exists() is False


def test_offline_job_rejects_video_id_mismatch(offline_jobs_dir: Path) -> None:
    """推測した job_id を別録画 ID の status/download パスから参照できない。"""

    async def Run() -> None:
        async def Generate() -> AsyncGenerator[bytes]:
            yield b'complete'

        _FakeOfflineStream.generate = Generate

        async def CreateStream(_job_id: str):
            return _FakeRecordedStream(), _BuildMetadata(10)

        job = await KonomiTVBS4KOfflineJobManager.createJob(10, CreateStream)
        await asyncio.gather(*list(KonomiTVBS4KOfflineJobManager._tasks.values()))
        with pytest.raises(KonomiTVBS4KOfflineJobNotFoundError):
            await KonomiTVBS4KOfflineJobManager.getJob(11, job.job_id)

    asyncio.run(Run())


def test_offline_job_rejects_changed_ready_package_size(offline_jobs_dir: Path) -> None:
    """Ready 後にサイズが変わったパッケージを配信せず、ジョブを Failed へ確定する。"""

    async def Run() -> None:
        async def Generate() -> AsyncGenerator[bytes]:
            yield b'complete'

        _FakeOfflineStream.generate = Generate

        async def CreateStream(_job_id: str):
            return _FakeRecordedStream(), _BuildMetadata(12)

        job = await KonomiTVBS4KOfflineJobManager.createJob(12, CreateStream)
        await asyncio.gather(*list(KonomiTVBS4KOfflineJobManager._tasks.values()))
        package_path = offline_jobs_dir / f'{job.job_id}.package'
        package_path.write_bytes(b'changed-size')

        with pytest.raises(KonomiTVBS4KOfflineJobNotReadyError):
            await KonomiTVBS4KOfflineJobManager.getDownloadPath(12, job.job_id)
        failed = await KonomiTVBS4KOfflineJobManager.getJob(12, job.job_id)
        assert failed.state == 'Failed'
        assert package_path.exists() is False

    asyncio.run(Run())
