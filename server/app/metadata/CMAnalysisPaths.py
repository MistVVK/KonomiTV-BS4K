from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app.utils.HostPath import NormalizeHostPath, ToRuntimePath


def ValidateCMLogoDirectory(host_path: Path) -> Path:
    """共有ロゴフォルダの作成・読書き・同一FS内rename可否を検証する。

    Args:
        host_path: UIへ保存するホスト側絶対パス。

    Returns:
        検証済みの実行時パス。

    Raises:
        ValueError: 絶対パスでない、または必要な操作を実行できない場合。
    """

    normalized_host_path = NormalizeHostPath(host_path)
    runtime_path = ToRuntimePath(normalized_host_path)
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
        raise ValueError('CMロゴフォルダを読み書きできないか、原子的renameを利用できません。') from ex
    return runtime_path
