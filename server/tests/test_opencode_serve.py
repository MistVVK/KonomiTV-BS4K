"""製品用 opencode serve 管理のユニットテスト（バイナリ起動なし）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.metadata.ai import opencode_serve


def test_ensure_runtime_directories_seeds_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """home/workspace を作り、config 雛形を seed する。"""

    home = tmp_path / 'opencode-home'
    config_home = home / 'config'
    data_home = home / 'data'
    workspace = tmp_path / 'opencode-workspace'
    log_path = tmp_path / 'logs' / 'opencode-serve.log'
    template = tmp_path / 'template-opencode.json'
    template.write_text('{"agent":{}}\n', encoding='utf-8')

    monkeypatch.setattr(opencode_serve, 'OPENCODE_HOME_ROOT', home)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_XDG_CONFIG_HOME', config_home)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_XDG_DATA_HOME', data_home)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_WORKSPACE_DIR', workspace)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_SERVE_LOG_PATH', log_path)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_BUNDLED_CONFIG_PATH', template)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_REPO_CONFIG_PATH', template)

    opencode_serve.EnsureOpenCodeRuntimeDirectories()

    seeded = config_home / 'opencode' / 'opencode.json'
    assert seeded.is_file()
    assert seeded.read_text(encoding='utf-8') == '{"agent":{}}\n'
    assert workspace.is_dir()


def test_probe_availability_shape() -> None:
    """availability スナップショットのキーが揃う。"""

    snapshot = opencode_serve.ProbeOpenCodeAvailability()
    assert set(snapshot.keys()) >= {
        'available',
        'base_url',
        'host',
        'port',
        'version',
        'pinned_version',
        'pid',
        'workspace',
    }
    assert snapshot['port'] == 4097
    assert snapshot['pinned_version'] == '1.18.13'


def test_reclaim_missing_pid_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PID ファイルが無い reclaim は例外にしない。"""

    monkeypatch.setattr(
        opencode_serve,
        'OPENCODE_SERVE_PID_PATH',
        tmp_path / 'missing.pid',
    )
    opencode_serve.ReclaimStaleOpenCodeServe()
