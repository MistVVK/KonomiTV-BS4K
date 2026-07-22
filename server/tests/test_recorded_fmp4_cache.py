import asyncio
from pathlib import Path
from unittest.mock import patch

import app.config as config_module
import app.routers.SettingsRouter as settings_router_module
import app.utils as utils_module
from app.config import ServerSettings
from app.streams.RecordedFMP4Cache import RecordedFMP4CacheManager, RecordedFMP4Variant


def test_recorded_fmp4_variant_digest_is_stable() -> None:
    """同じ生成条件から常に同じ短縮SHA-256が得られることを確認する。"""

    variant = RecordedFMP4Variant('1080p', 'hevc', 10, False, 'QSVEncC', 1, 2)
    assert variant.digest() == RecordedFMP4Variant('1080p', 'hevc', 10, False, 'QSVEncC', 1, 2).digest()
    assert len(variant.digest()) == 24


def test_recorded_fmp4_pipeline_revision_changes_digest_without_changing_layout() -> None:
    """内部パイプライン改訂時だけdigestが変わり、BS4K版v1キャッシュも管理対象に残ることを確認する。"""

    variant = RecordedFMP4Variant('1080p', 'hevc', 10, False, 'QSVEncC', 1, 2)
    current_digest = variant.digest()

    with patch.object(RecordedFMP4Variant, 'PIPELINE_REVISION', RecordedFMP4Variant.PIPELINE_REVISION + 1):
        assert variant.digest() != current_digest

    assert RecordedFMP4CacheManager.LAYOUT_VERSION == 1
    layout_v1_cache_name = '.konomitv-bs4k-fmp4-v1-12-abcd-' + ('0' * 24) + '-video-3.m4s'
    assert RecordedFMP4CacheManager.isCacheFileName(layout_v1_cache_name) is True


def test_recorded_fmp4_reserved_file_name_is_strict() -> None:
    """予約形式だけを除外し、一般MP4や部分一致を誤除外しないことを確認する。"""

    valid = '.konomitv-bs4k-fmp4-v1-12-abcd-' + ('0' * 24) + '-video-3.m4s'
    assert RecordedFMP4CacheManager.isCacheFileName(valid) is True
    assert RecordedFMP4CacheManager.isCacheFileName(valid + '.tmp-123e4567-e89b-12d3-a456-426614174000') is True
    assert RecordedFMP4CacheManager.isCacheFileName(valid.replace('.konomitv-bs4k-', '.konomitv-')) is False
    assert RecordedFMP4CacheManager.isCacheFileName('program.mp4') is False
    assert RecordedFMP4CacheManager.isCacheFileName(f'copy-{valid}') is False


def test_recorded_fmp4_reference_delays_deletion_and_reuse_cancels_it(tmp_path: Path) -> None:
    """最後の参照後に遅延削除し、猶予中の再参照で削除を中止することを確認する。"""

    async def Run() -> None:
        path = tmp_path / ('.konomitv-bs4k-fmp4-v1-12-abcd-' + ('0' * 24) + '-video-3.m4s')
        path.write_bytes(b'fragment')
        original_delay = RecordedFMP4CacheManager.RELEASE_DELAY_SECONDS
        RecordedFMP4CacheManager.RELEASE_DELAY_SECONDS = 0.02
        try:
            await RecordedFMP4CacheManager.acquire(path, 'session-1')
            RecordedFMP4CacheManager.release(path, 'session-1')
            await asyncio.sleep(0.005)
            await RecordedFMP4CacheManager.acquire(path, 'session-2')
            await asyncio.sleep(0.03)
            assert path.is_file()
            RecordedFMP4CacheManager.release(path, 'session-2')
            await asyncio.sleep(0.03)
            assert path.exists() is False
        finally:
            RecordedFMP4CacheManager.RELEASE_DELAY_SECONDS = original_delay

    async def RunSynchronously(function, *args, **kwargs):
        """ホストPythonの終了不能なThreadPoolExecutorを使わずI/O結果だけを再現する。"""

        return function(*args, **kwargs)

    with patch('app.streams.RecordedFMP4Cache.asyncio.to_thread', side_effect=RunSynchronously):
        asyncio.run(asyncio.wait_for(Run(), timeout=1.0))


def test_recorded_scan_cleanup_preserves_referenced_cache(tmp_path: Path) -> None:
    """録画スキャンが未参照残骸だけを削除し、再生中キャッシュを保護することを確認する。"""

    async def Run() -> None:
        path = tmp_path / ('.konomitv-bs4k-fmp4-v1-12-abcd-' + ('0' * 24) + '-video-4.m4s')
        path.write_bytes(b'fragment')
        await RecordedFMP4CacheManager.acquire(path, 'session')
        await RecordedFMP4CacheManager.cleanupDiscovered(path)
        assert path.is_file()
        RecordedFMP4CacheManager.release(path, 'session')
        release_task = RecordedFMP4CacheManager._release_tasks.pop(str(path))  # pyright: ignore[reportPrivateUsage]
        release_task.cancel()
        await RecordedFMP4CacheManager.cleanupDiscovered(path)
        assert path.exists() is False

    async def RunSynchronously(function, *args, **kwargs):
        """ホストPythonの終了不能なThreadPoolExecutorを使わずI/O結果だけを再現する。"""

        return function(*args, **kwargs)

    with patch('app.streams.RecordedFMP4Cache.asyncio.to_thread', side_effect=RunSynchronously):
        asyncio.run(asyncio.wait_for(Run(), timeout=1.0))


def test_recorded_fmp4_cache_host_path_is_prefixed_for_docker(monkeypatch, tmp_path: Path) -> None:
    """設定画面のホスト側パスをDocker内部の書き込み先へ変換する。"""

    docker_root = tmp_path / 'host-rootfs'
    monkeypatch.setattr(config_module, '_DOCKER_PATH_PREFIX', str(docker_root))
    monkeypatch.setattr(utils_module, 'GetPlatformEnvironment', lambda: 'Linux-Docker')

    settings = ServerSettings(video={'recorded_fmp4_cache_folder': '/recorded-cache'})

    assert settings.video.recorded_fmp4_cache_folder == docker_root / 'recorded-cache'
    assert settings.video.recorded_fmp4_cache_folder.is_dir()


def test_recorded_fmp4_cache_api_returns_host_path(monkeypatch) -> None:
    """Docker内部Prefixを設定画面へ露出せずホスト側パスで返す。"""

    settings = ServerSettings()
    settings.video.recorded_fmp4_cache_folder = Path('/host-rootfs/mnt/recorded-cache')
    monkeypatch.setattr(settings_router_module, 'Config', lambda: settings)
    monkeypatch.setattr(settings_router_module, 'GetPlatformEnvironment', lambda: 'Linux-Docker')

    response = asyncio.run(settings_router_module.ServerSettingsAPI())

    assert response.video.recorded_fmp4_cache_folder == Path('/mnt/recorded-cache')
