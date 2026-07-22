from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app.utils import GetPlatformEnvironment


_DOCKER_HOST_ROOT = Path('/host-rootfs')


def ResolveCMHostPath(path: Path) -> Path:
    """設定画面のホスト絶対パスを現在の実行環境でアクセスできるパスへ変換する。

    Args:
        path: UIとDBで保持するホスト側絶対パス。

    Returns:
        Dockerでは/host-rootfsを付けた実行時パス、それ以外では元のパス。
    """

    if GetPlatformEnvironment() == 'Linux-Docker' and path.is_relative_to(_DOCKER_HOST_ROOT) is False:
        return _DOCKER_HOST_ROOT / path.relative_to('/')
    return path


def ValidateCMLogoDirectory(host_path: Path) -> Path:
    """共有ロゴフォルダの作成・読書き・同一FS内rename可否を検証する。

    Args:
        host_path: UIへ保存するホスト側絶対パス。

    Returns:
        検証済みの実行時パス。

    Raises:
        ValueError: 絶対パスでない、または必要な操作を実行できない場合。
    """

    if host_path.is_absolute() is False:
        raise ValueError('CMロゴフォルダにはホスト側の絶対パスを指定してください。')
    runtime_path = ResolveCMHostPath(host_path)
    try:
        runtime_path.mkdir(parents=True, exist_ok=True)
        temporary_fd, temporary_name = tempfile.mkstemp(prefix='.konomitv-bs4k-cm-logo-', dir=runtime_path)
        temporary_path = Path(temporary_name)
        destination_path = temporary_path.with_suffix('.rename-test')
        try:
            with os.fdopen(temporary_fd, 'wb') as temporary_file:
                temporary_file.write(b'KonomiTV-BS4K CM logo directory test')
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            if temporary_path.read_bytes() == b'KonomiTV-BS4K CM logo directory test':
                os.replace(temporary_path, destination_path)
            else:
                raise OSError('Written data could not be read back.')
        finally:
            temporary_path.unlink(missing_ok=True)
            destination_path.unlink(missing_ok=True)
    except OSError as ex:
        raise ValueError(f'CMロゴフォルダを読み書きできないか、原子的renameを利用できません: {host_path}') from ex
    return runtime_path
