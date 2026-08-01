from pathlib import Path

import pytest

from app.utils.HostPath import (
    HostPathError,
    NormalizeHostPath,
    ToHostPath,
    ToRuntimePath,
    ToUserHostPathText,
)


def test_docker_host_path_round_trip_adds_prefix_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dockerではホストパスと内部パスを接頭辞1回で往復する。"""

    monkeypatch.setattr('app.utils.GetPlatformEnvironment', lambda: 'Linux-Docker')

    assert ToRuntimePath('/mnt/TV-Record') == Path('/host-rootfs/mnt/TV-Record')
    assert ToRuntimePath('/host-rootfs/mnt/TV-Record') == Path('/host-rootfs/mnt/TV-Record')
    assert ToHostPath('/host-rootfs/mnt/TV-Record') == Path('/mnt/TV-Record')


def test_docker_host_path_preserves_middle_directory_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """パス中間のhost-rootfsというディレクトリ名は変更しない。"""

    monkeypatch.setattr('app.utils.GetPlatformEnvironment', lambda: 'Linux-Docker')

    host_path = Path('/mnt/host-rootfs/archive')
    assert NormalizeHostPath(host_path) == host_path
    assert ToRuntimePath(host_path) == Path('/host-rootfs/mnt/host-rootfs/archive')
    assert ToHostPath('/host-rootfs/mnt/host-rootfs/archive') == host_path


@pytest.mark.parametrize(
    'path_value',
    [
        '/host-rootfs/host-rootfs/mnt/TV-Record',
        '/mnt/TV-Record/../Secret',
        '//mnt/TV-Record',
        'mnt/TV-Record',
    ],
)
def test_unsafe_host_paths_are_rejected_without_echoing_input(
    monkeypatch: pytest.MonkeyPatch,
    path_value: str,
) -> None:
    """二重接頭辞・親参照・相対パスを入力値を転載しないエラーで拒否する。"""

    monkeypatch.setattr('app.utils.GetPlatformEnvironment', lambda: 'Linux-Docker')

    with pytest.raises(HostPathError) as error:
        ToRuntimePath(path_value)
    assert path_value not in str(error.value)
    assert '/host-rootfs' not in str(error.value)


def test_non_docker_conversion_is_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """非Docker環境では既存パスを恒等変換する。"""

    monkeypatch.setattr('app.utils.GetPlatformEnvironment', lambda: 'Linux')

    path = Path('/host-rootfs/mnt/TV-Record')
    assert NormalizeHostPath(path) == path
    assert ToRuntimePath(path) == path
    assert ToHostPath(path) == path


def test_user_error_text_hides_only_path_token_prefix() -> None:
    """エラー本文の先頭接頭辞だけを除き、中間ディレクトリ名は保持する。"""

    error_text = (
        'Failed: /host-rootfs/host-rootfs/mnt/TV-Record; '
        'root: /host-rootfs; '
        'end: /host-rootfs\n'
        'kept: /mnt/host-rootfs/archive and /host-rootfs-backup'
    )

    assert ToUserHostPathText(error_text) == (
        'Failed: /mnt/TV-Record; root: /; end: /\n'
        'kept: /mnt/host-rootfs/archive and /host-rootfs-backup'
    )
