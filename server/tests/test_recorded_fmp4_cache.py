import asyncio
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import app.routers.SettingsRouter as settings_router_module
import app.utils as utils_module
import app.utils.HostPath as host_path_module
from app.config import HostServerSettings, ServerSettings
from app.models.User import User
from app.streams.RecordedFMP4Cache import RecordedFMP4CacheManager, RecordedFMP4Variant


def test_recorded_fmp4_variant_digest_is_stable() -> None:
    """同じ生成条件から常に同じ短縮SHA-256が得られることを確認する。"""

    variant = RecordedFMP4Variant('1080p', 'hevc', 10, False, 'QSV', 1, 2)
    assert variant.digest() == RecordedFMP4Variant('1080p', 'hevc', 10, False, 'QSV', 1, 2).digest()
    assert len(variant.digest()) == 24


def test_recorded_fmp4_pipeline_revision_changes_digest_without_changing_layout() -> None:
    """内部パイプライン改訂時だけdigestが変わり、BS4K版v1キャッシュも管理対象に残ることを確認する。"""

    variant = RecordedFMP4Variant('1080p', 'hevc', 10, False, 'QSV', 1, 2)
    current_digest = variant.digest()

    with patch.object(RecordedFMP4Variant, 'PIPELINE_REVISION', RecordedFMP4Variant.PIPELINE_REVISION + 1):
        assert variant.digest() != current_digest

    assert RecordedFMP4CacheManager.LAYOUT_VERSION == 1
    layout_v1_cache_name = '.konomitv-bs4k-fmp4-v1-12-abcd-' + ('0' * 24) + '-video-3.m4s'
    assert RecordedFMP4CacheManager.isCacheFileName(layout_v1_cache_name) is True


def test_recorded_fmp4_variant_separates_playback_and_offline_delivery_modes() -> None:
    """同じ生成条件でも通常再生とオフライン保存のcache digestを分離する。"""

    playback = RecordedFMP4Variant('720p', 'av1', 10, False, 'FFmpeg', 0, 0)
    offline = RecordedFMP4Variant(
        '720p',
        'av1',
        10,
        False,
        'FFmpeg',
        0,
        0,
        delivery_mode='Offline',
    )

    assert playback.digest() != offline.digest()


def test_recorded_fmp4_reserved_file_name_is_strict() -> None:
    """予約形式だけを除外し、一般MP4や部分一致を誤除外しないことを確認する。"""

    valid = '.konomitv-bs4k-fmp4-v1-12-abcd-' + ('0' * 24) + '-video-3.m4s'
    assert RecordedFMP4CacheManager.isCacheFileName(valid) is True
    assert RecordedFMP4CacheManager.isCacheFileName(valid + '.tmp-123e4567-e89b-12d3-a456-426614174000') is True
    assert RecordedFMP4CacheManager.isCacheFileName(valid.replace('.konomitv-bs4k-', '.konomitv-')) is True
    assert RecordedFMP4CacheManager.isCacheFileName(valid.replace('.konomitv-bs4k-', '.third-party-')) is False
    assert RecordedFMP4CacheManager.isCacheFileName('program.mp4') is False
    assert RecordedFMP4CacheManager.isCacheFileName(f'copy-{valid}') is False


def test_legacy_fmp4_cache_is_cleaned_without_allowing_legacy_writes(tmp_path: Path) -> None:
    """旧・現行の完全一致だけを回収し、似非file保持と旧prefix書込み拒否を両立する。"""

    suffix = '12-abcd-' + ('0' * 24) + '-video-3.m4s'
    current = tmp_path / f'.konomitv-bs4k-fmp4-v1-{suffix}'
    legacy = tmp_path / f'.konomitv-fmp4-v1-{suffix}'
    similar_prefix = tmp_path / f'.konomitv-fmp4-v2-{suffix}'
    similar_suffix = tmp_path / f'.konomitv-fmp4-v1-{suffix}.backup'
    for path in (current, legacy, similar_prefix, similar_suffix):
        path.write_bytes(b'fragment')

    async def RunSynchronously(function, *args, **kwargs):  # type: ignore[no-untyped-def]
        """終了不能なhost ThreadPoolExecutorを避け、cleanupの同期I/O本体だけを検証する。"""

        return function(*args, **kwargs)

    async def Run() -> None:
        assert RecordedFMP4CacheManager.isCacheFileName(current.name) is True
        assert RecordedFMP4CacheManager.isCacheFileName(legacy.name) is True
        assert RecordedFMP4CacheManager.isCacheFileName(similar_prefix.name) is False
        assert RecordedFMP4CacheManager.isCacheFileName(similar_suffix.name) is False
        for path in (current, legacy, similar_prefix, similar_suffix):
            await RecordedFMP4CacheManager.cleanupDiscovered(path)
        with pytest.raises(ValueError, match='Unmanaged fMP4 cache path'):
            await RecordedFMP4CacheManager.writeAtomic(legacy, b'new-fragment')

    with patch('app.streams.RecordedFMP4Cache.asyncio.to_thread', side_effect=RunSynchronously):
        asyncio.run(Run())

    assert current.exists() is False
    assert legacy.exists() is False
    assert similar_prefix.is_file() is True
    assert similar_suffix.is_file() is True


def test_recorded_fmp4_reference_delays_deletion_and_reuse_cancels_it(tmp_path: Path) -> None:
    """最後の参照後に遅延削除し、猶予中の再参照で削除を中止することを確認する。"""

    async def Run() -> None:
        path = tmp_path / ('.konomitv-bs4k-fmp4-v1-12-abcd-' + ('0' * 24) + '-video-3.m4s')
        path.write_bytes(b'fragment')
        original_delay = RecordedFMP4CacheManager.RELEASE_DELAY_SECONDS
        RecordedFMP4CacheManager.RELEASE_DELAY_SECONDS = 0.02
        try:
            await RecordedFMP4CacheManager.acquire(path, 'session-1')
            await RecordedFMP4CacheManager.release(path, 'session-1')
            await asyncio.sleep(0.005)
            await RecordedFMP4CacheManager.acquire(path, 'session-2')
            await asyncio.sleep(0.03)
            assert path.is_file()
            await RecordedFMP4CacheManager.release(path, 'session-2')
            await asyncio.sleep(0.03)
            assert path.exists() is False
        finally:
            RecordedFMP4CacheManager.RELEASE_DELAY_SECONDS = original_delay

    async def RunSynchronously(function, *args, **kwargs):
        """ホストPythonの終了不能なThreadPoolExecutorを使わずI/O結果だけを再現する。"""

        return function(*args, **kwargs)

    with patch('app.streams.RecordedFMP4Cache.asyncio.to_thread', side_effect=RunSynchronously):
        asyncio.run(asyncio.wait_for(Run(), timeout=1.0))


def test_cleanup_discovered_skips_in_progress_tmp_and_destination(tmp_path: Path) -> None:
    """
    atomic write 進行中の tmp / 完成 path は cleanupDiscovered で削除されない。

    Args:
        tmp_path (Path): 一時キャッシュディレクトリ。

    Returns:
        None
    """

    async def Run() -> None:
        """
        in-progress set に載せた path が保護され、解除後は削除できることを確認する。

        Args:
            None

        Returns:
            None
        """

        destination = tmp_path / ('.konomitv-bs4k-fmp4-v1-12-abcd-' + ('0' * 24) + '-video-9.m4s')
        temporary_path = destination.with_name(f'{destination.name}.tmp-123e4567-e89b-12d3-a456-426614174000')
        temporary_path.write_bytes(b'partial')
        destination.write_bytes(b'complete')

        RecordedFMP4CacheManager._in_progress_paths.add(str(temporary_path))  # pyright: ignore[reportPrivateUsage]
        RecordedFMP4CacheManager._in_progress_paths.add(str(destination))  # pyright: ignore[reportPrivateUsage]
        try:
            await RecordedFMP4CacheManager.cleanupDiscovered(temporary_path)
            await RecordedFMP4CacheManager.cleanupDiscovered(destination)
            assert temporary_path.is_file() is True
            assert destination.is_file() is True
        finally:
            RecordedFMP4CacheManager._in_progress_paths.discard(str(temporary_path))  # pyright: ignore[reportPrivateUsage]
            RecordedFMP4CacheManager._in_progress_paths.discard(str(destination))  # pyright: ignore[reportPrivateUsage]

        await RecordedFMP4CacheManager.cleanupDiscovered(temporary_path)
        await RecordedFMP4CacheManager.cleanupDiscovered(destination)
        assert temporary_path.exists() is False
        assert destination.exists() is False

    async def RunSynchronously(function, *args, **kwargs):
        """ホスト Python の終了不能な ThreadPoolExecutor を使わず同期実行する。"""

        return function(*args, **kwargs)

    with patch('app.streams.RecordedFMP4Cache.asyncio.to_thread', side_effect=RunSynchronously):
        asyncio.run(asyncio.wait_for(Run(), timeout=1.0))


def test_write_atomic_registers_and_clears_in_progress_paths(tmp_path: Path) -> None:
    """
    writeAtomic が書き込み中に in-progress 登録し、完了後に必ず解除することを確認する。

    Args:
        tmp_path (Path): 一時キャッシュディレクトリ。

    Returns:
        None
    """

    async def Run() -> None:
        """
        WriteAndReplace 実行中だけ in-progress に載ることを観察する。

        Args:
            None

        Returns:
            None
        """

        destination = tmp_path / ('.konomitv-bs4k-fmp4-v1-12-abcd-' + ('0' * 24) + '-video-10.m4s')
        seen_in_progress: list[bool] = []

        def ObservingToThread(function, *args, **kwargs):
            """
            実際の書き込み関数実行中に in-progress set を観察する。

            Args:
                function: to_thread に渡された同期関数。
                *args: 位置引数。
                **kwargs: キーワード引数。

            Returns:
                object: 同期関数の戻り値。
            """

            if getattr(function, '__name__', '') == 'WriteAndReplace':
                # writeAtomic が一時 path を登録した直後の同期書き込み中だけ観察する
                seen_in_progress.append(
                    any(str(destination) in path or path.startswith(str(destination) + '.tmp-')
                        for path in RecordedFMP4CacheManager._in_progress_paths),  # pyright: ignore[reportPrivateUsage]
                )
            return function(*args, **kwargs)

        with patch('app.streams.RecordedFMP4Cache.asyncio.to_thread', side_effect=ObservingToThread):
            await RecordedFMP4CacheManager.writeAtomic(destination, b'fragment-data')

        assert destination.read_bytes() == b'fragment-data'
        assert True in seen_in_progress
        assert str(destination) not in RecordedFMP4CacheManager._in_progress_paths  # pyright: ignore[reportPrivateUsage]
        assert all(not path.startswith(str(destination) + '.tmp-') for path in RecordedFMP4CacheManager._in_progress_paths)  # pyright: ignore[reportPrivateUsage]

    asyncio.run(asyncio.wait_for(Run(), timeout=1.0))


def test_recorded_scan_cleanup_preserves_referenced_cache(tmp_path: Path) -> None:
    """録画スキャンが未参照残骸だけを削除し、再生中キャッシュを保護することを確認する。"""

    async def Run() -> None:
        path = tmp_path / ('.konomitv-bs4k-fmp4-v1-12-abcd-' + ('0' * 24) + '-video-4.m4s')
        path.write_bytes(b'fragment')
        await RecordedFMP4CacheManager.acquire(path, 'session')
        await RecordedFMP4CacheManager.cleanupDiscovered(path)
        assert path.is_file()
        await RecordedFMP4CacheManager.release(path, 'session')
        await RecordedFMP4CacheManager.cleanupDiscovered(path)
        assert path.exists() is False

    async def RunSynchronously(function, *args, **kwargs):
        """ホストPythonの終了不能なThreadPoolExecutorを使わずI/O結果だけを再現する。"""

        return function(*args, **kwargs)

    with patch('app.streams.RecordedFMP4Cache.asyncio.to_thread', side_effect=RunSynchronously):
        asyncio.run(asyncio.wait_for(Run(), timeout=1.0))


def test_startup_cleanup_preserves_referenced_cache(monkeypatch, tmp_path: Path) -> None:
    """起動時 cleanup も scanner と同じ path lock 規約で active reference を保護する。"""

    class EmptyRecordedVideoQuery:
        """起動時 cleanup へ空の録画ファイル一覧を返す。"""

        async def values_list(self, _field: str, *, flat: bool) -> list[str]:
            assert flat is True
            return []

    async def Run() -> None:
        path = tmp_path / ('.konomitv-bs4k-fmp4-v1-12-abcd-' + ('0' * 24) + '-video-16.m4s')
        path.write_bytes(b'fragment')
        settings = ServerSettings()
        settings.video.recorded_fmp4_cache_folder = tmp_path
        monkeypatch.setattr('app.streams.RecordedFMP4Cache.Config', lambda: settings)

        with patch(
            'app.streams.RecordedFMP4Cache.RecordedVideo.all',
            return_value=EmptyRecordedVideoQuery(),
        ):
            await RecordedFMP4CacheManager.acquire(path, 'session-startup')
            await RecordedFMP4CacheManager.cleanupStale()
            assert path.read_bytes() == b'fragment'
            await RecordedFMP4CacheManager.release(path, 'session-startup')
            await RecordedFMP4CacheManager.cleanupDiscovered(path)

    asyncio.run(asyncio.wait_for(Run(), timeout=1.0))


def test_cleanup_discovered_and_acquire_are_serialized_by_path_lock(tmp_path: Path) -> None:
    """cleanup の参照確認後に acquire が割り込んで参照中ファイルを消す競合を防ぐ。"""

    async def Run() -> None:
        path = tmp_path / ('.konomitv-bs4k-fmp4-v1-12-abcd-' + ('0' * 24) + '-video-11.m4s')
        path.write_bytes(b'old-fragment')
        unlink_started = asyncio.Event()
        allow_unlink = asyncio.Event()

        async def ControlledToThread(function, *args, **kwargs):  # type: ignore[no-untyped-def]
            unlink_started.set()
            await allow_unlink.wait()
            return function(*args, **kwargs)

        with patch('app.streams.RecordedFMP4Cache.asyncio.to_thread', side_effect=ControlledToThread):
            cleanup_task = asyncio.create_task(RecordedFMP4CacheManager.cleanupDiscovered(path))
            await unlink_started.wait()
            acquire_task = asyncio.create_task(RecordedFMP4CacheManager.acquire(path, 'session-race'))
            await asyncio.sleep(0)
            # cleanup が unlink worker を待つ間は acquire が path lock の外へ出ない。
            assert acquire_task.done() is False
            allow_unlink.set()
            await cleanup_task
            await acquire_task

            # acquire 後に再生成したファイルは active reference 中の scanner cleanup から保護される。
            path.write_bytes(b'new-fragment')
            await RecordedFMP4CacheManager.cleanupDiscovered(path)
            assert path.read_bytes() == b'new-fragment'
            await RecordedFMP4CacheManager.release(path, 'session-race')
            await RecordedFMP4CacheManager.cleanupDiscovered(path)

    asyncio.run(asyncio.wait_for(Run(), timeout=1.0))


def test_delayed_delete_reacquire_waits_for_unlink_worker(tmp_path: Path) -> None:
    """遅延 unlink 開始後の再取得は worker 完了を待ち、新しいキャッシュを消されない。"""

    async def Run() -> None:
        path = tmp_path / ('.konomitv-bs4k-fmp4-v1-12-abcd-' + ('0' * 24) + '-video-12.m4s')
        path.write_bytes(b'old-fragment')
        original_delay = RecordedFMP4CacheManager.RELEASE_DELAY_SECONDS
        RecordedFMP4CacheManager.RELEASE_DELAY_SECONDS = 0
        unlink_started = asyncio.Event()
        allow_unlink = asyncio.Event()

        async def ControlledToThread(function, *args, **kwargs):  # type: ignore[no-untyped-def]
            unlink_started.set()
            await allow_unlink.wait()
            return function(*args, **kwargs)

        try:
            with patch('app.streams.RecordedFMP4Cache.asyncio.to_thread', side_effect=ControlledToThread):
                await RecordedFMP4CacheManager.acquire(path, 'session-old')
                await RecordedFMP4CacheManager.release(path, 'session-old')
                await unlink_started.wait()

                acquire_task = asyncio.create_task(
                    RecordedFMP4CacheManager.acquire(path, 'session-new'),
                )
                await asyncio.sleep(0)
                assert acquire_task.done() is False
                allow_unlink.set()
                await acquire_task

                path.write_bytes(b'new-fragment')
                await asyncio.sleep(0)
                assert path.read_bytes() == b'new-fragment'
                await RecordedFMP4CacheManager.release(path, 'session-new')
                await asyncio.sleep(0)
        finally:
            allow_unlink.set()
            RecordedFMP4CacheManager.RELEASE_DELAY_SECONDS = original_delay

    asyncio.run(asyncio.wait_for(Run(), timeout=1.0))


def test_acquire_waits_for_cancelled_release_task_before_propagating_caller_cancel(tmp_path: Path) -> None:
    """acquire 自身の cancel 時も旧遅延削除 task の完了前に呼び出し元へ戻らない。"""

    async def Run() -> None:
        path = tmp_path / ('.konomitv-bs4k-fmp4-v1-12-abcd-' + ('0' * 24) + '-video-15.m4s')
        release_cancelled = asyncio.Event()
        allow_release_finish = asyncio.Event()

        async def PendingRelease() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                release_cancelled.set()
                await allow_release_finish.wait()
                raise

        cache_key = str(path)
        release_task = asyncio.create_task(PendingRelease())
        RecordedFMP4CacheManager._release_tasks[cache_key] = release_task  # pyright: ignore[reportPrivateUsage]
        acquire_task = asyncio.create_task(
            RecordedFMP4CacheManager.acquire(path, 'session-cancelled'),
        )
        try:
            await release_cancelled.wait()
            acquire_task.cancel()
            await asyncio.sleep(0)
            assert acquire_task.done() is False

            allow_release_finish.set()
            with pytest.raises(asyncio.CancelledError):
                await acquire_task
            assert release_task.done() is True
        finally:
            allow_release_finish.set()
            if acquire_task.done() is False:
                acquire_task.cancel()
            if release_task.done() is False:
                release_task.cancel()
            await asyncio.gather(acquire_task, release_task, return_exceptions=True)
            RecordedFMP4CacheManager._release_tasks.pop(cache_key, None)  # pyright: ignore[reportPrivateUsage]
            RecordedFMP4CacheManager._references.pop(cache_key, None)  # pyright: ignore[reportPrivateUsage]
            RecordedFMP4CacheManager._locks.pop(cache_key, None)  # pyright: ignore[reportPrivateUsage]
            RecordedFMP4CacheManager._lock_users.pop(cache_key, None)  # pyright: ignore[reportPrivateUsage]

    asyncio.run(asyncio.wait_for(Run(), timeout=1.0))


def test_cancelled_atomic_write_holds_path_lock_until_worker_finishes(tmp_path: Path) -> None:
    """writeAtomic cancel 後も thread worker 完了まで path lock と in-progress を保持する。"""

    async def Run() -> None:
        destination = tmp_path / ('.konomitv-bs4k-fmp4-v1-12-abcd-' + ('0' * 24) + '-video-13.m4s')
        real_to_thread = asyncio.to_thread
        worker_started = threading.Event()
        allow_worker = threading.Event()

        async def ControlledToThread(function, *args, **kwargs):  # type: ignore[no-untyped-def]
            if getattr(function, '__name__', '') == 'WriteAndReplace':
                def BlockingWorker():  # type: ignore[no-untyped-def]
                    worker_started.set()
                    allow_worker.wait(timeout=1)
                    return function(*args, **kwargs)

                return await real_to_thread(BlockingWorker)
            return await real_to_thread(function, *args, **kwargs)

        try:
            with patch('app.streams.RecordedFMP4Cache.asyncio.to_thread', side_effect=ControlledToThread):
                write_task = asyncio.create_task(
                    RecordedFMP4CacheManager.writeAtomic(destination, b'fragment-data'),
                )
                while worker_started.is_set() is False:
                    await asyncio.sleep(0.001)
                write_task.cancel()
                cleanup_task = asyncio.create_task(
                    RecordedFMP4CacheManager.cleanupDiscovered(destination),
                )
                await asyncio.sleep(0.01)
                assert write_task.done() is False
                assert cleanup_task.done() is False

                allow_worker.set()
                with pytest.raises(asyncio.CancelledError):
                    await write_task
                await cleanup_task
                assert destination.exists() is False
        finally:
            allow_worker.set()

    asyncio.run(asyncio.wait_for(Run(), timeout=2.0))


def test_write_atomic_reenters_generation_path_lock(tmp_path: Path) -> None:
    """生成 lock 保持 task から同じ path の writeAtomic を呼んでも自己 deadlock しない。"""

    async def Run() -> None:
        path = tmp_path / ('.konomitv-bs4k-fmp4-v1-12-abcd-' + ('0' * 24) + '-video-14.m4s')
        generation_lock = await RecordedFMP4CacheManager.acquire(path, 'session-generation')
        async with generation_lock:
            await RecordedFMP4CacheManager.writeAtomic(path, b'fragment-data')
        assert path.read_bytes() == b'fragment-data'
        await RecordedFMP4CacheManager.release(path, 'session-generation')
        await RecordedFMP4CacheManager.cleanupDiscovered(path)

    asyncio.run(asyncio.wait_for(Run(), timeout=1.0))


def test_recorded_fmp4_cache_host_path_is_prefixed_for_docker(monkeypatch, tmp_path: Path) -> None:
    """設定画面のホスト側パスをDocker内部の書き込み先へ変換する。"""

    docker_root = tmp_path / 'host-rootfs'
    monkeypatch.setattr(host_path_module, 'DOCKER_HOST_ROOT', docker_root)
    monkeypatch.setattr(utils_module, 'GetPlatformEnvironment', lambda: 'Linux-Docker')

    host_settings = HostServerSettings(video={'recorded_fmp4_cache_folder': '/recorded-cache'})
    settings = host_settings.toServerSettings(bypass_validation=True)

    assert settings.video.recorded_fmp4_cache_folder == docker_root / 'recorded-cache'
    assert settings.video.recorded_fmp4_cache_folder.is_dir()


def test_recorded_fmp4_cache_api_returns_host_path(monkeypatch) -> None:
    """Docker内部Prefixを設定画面へ露出せずホスト側パスで返す。"""

    settings = ServerSettings()
    settings.video.recorded_fmp4_cache_folder = Path('/host-rootfs/mnt/recorded-cache')
    monkeypatch.setattr(settings_router_module, 'Config', lambda: settings)
    monkeypatch.setattr(utils_module, 'GetPlatformEnvironment', lambda: 'Linux-Docker')

    response = asyncio.run(settings_router_module.ServerSettingsAPI(
        User(name='test-admin', password='unused', is_admin=True),
    ))

    assert response.video.recorded_fmp4_cache_folder == Path('/mnt/recorded-cache')
