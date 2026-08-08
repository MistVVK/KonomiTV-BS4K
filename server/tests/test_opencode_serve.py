"""製品用 opencode serve 管理のユニットテスト（バイナリ起動なし）。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
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


def test_ensure_runtime_directories_resyncs_stale_episode_web_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既存 home の episode webfetch allow を検索専用テンプレートへ上書きする。"""

    home = tmp_path / 'opencode-home'
    config_home = home / 'config'
    data_home = home / 'data'
    workspace = tmp_path / 'opencode-workspace'
    log_path = tmp_path / 'logs' / 'opencode-serve.log'
    template = tmp_path / 'template-opencode.json'
    template.write_text(
        (
            '{\n'
            '  "agent": {\n'
            '    "recorded-series-episode": {\n'
            '      "permission": {\n'
            '        "webfetch": "deny",\n'
            '        "websearch": "allow"\n'
            '      }\n'
            '    }\n'
            '  }\n'
            '}\n'
        ),
        encoding='utf-8',
    )
    config_dir = config_home / 'opencode'
    config_dir.mkdir(parents=True)
    stale = config_dir / 'opencode.json'
    stale.write_text(
        (
            '{\n'
            '  "agent": {\n'
            '    "recorded-series-episode": {\n'
            '      "permission": {\n'
            '        "webfetch": "allow",\n'
            '        "websearch": "allow"\n'
            '      }\n'
            '    }\n'
            '  }\n'
            '}\n'
        ),
        encoding='utf-8',
    )

    monkeypatch.setattr(opencode_serve, 'OPENCODE_HOME_ROOT', home)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_XDG_CONFIG_HOME', config_home)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_XDG_DATA_HOME', data_home)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_WORKSPACE_DIR', workspace)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_SERVE_LOG_PATH', log_path)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_BUNDLED_CONFIG_PATH', template)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_REPO_CONFIG_PATH', template)

    opencode_serve.EnsureOpenCodeRuntimeDirectories()

    synced = stale.read_text(encoding='utf-8')
    assert '"webfetch": "deny"' in synced
    assert '"websearch": "allow"' in synced
    assert '"webfetch": "allow"' not in synced
    assert stale.stat().st_mode & 0o777 == 0o600


def test_ensure_runtime_directories_prefers_repo_template_during_development(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """repo template がある開発環境では古い bundled config を再同期しない。"""

    home = tmp_path / 'opencode-home'
    config_home = home / 'config'
    data_home = home / 'data'
    workspace = tmp_path / 'opencode-workspace'
    log_path = tmp_path / 'logs' / 'opencode-serve.log'
    bundled_template = tmp_path / 'bundled-opencode.json'
    bundled_template.write_text('{"webfetch":"allow"}\n', encoding='utf-8')
    repo_template = tmp_path / 'repo-opencode.json'
    repo_template.write_text('{"webfetch":"deny"}\n', encoding='utf-8')

    monkeypatch.setattr(opencode_serve, 'OPENCODE_HOME_ROOT', home)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_XDG_CONFIG_HOME', config_home)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_XDG_DATA_HOME', data_home)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_WORKSPACE_DIR', workspace)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_SERVE_LOG_PATH', log_path)
    monkeypatch.setattr(
        opencode_serve,
        'OPENCODE_BUNDLED_CONFIG_PATH',
        bundled_template,
    )
    monkeypatch.setattr(opencode_serve, 'OPENCODE_REPO_CONFIG_PATH', repo_template)

    opencode_serve.EnsureOpenCodeRuntimeDirectories()

    synced = config_home / 'opencode' / 'opencode.json'
    assert synced.read_text(encoding='utf-8') == '{"webfetch":"deny"}\n'


def test_ensure_runtime_directories_rejects_config_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """製品 config パスが symlink のとき追跡せず拒否する。"""

    home = tmp_path / 'opencode-home'
    config_home = home / 'config'
    data_home = home / 'data'
    workspace = tmp_path / 'opencode-workspace'
    log_path = tmp_path / 'logs' / 'opencode-serve.log'
    template = tmp_path / 'template-opencode.json'
    template.write_text('{"agent":{}}\n', encoding='utf-8')
    outside = tmp_path / 'outside-opencode.json'
    outside.write_text('{"agent":{"evil":true}}\n', encoding='utf-8')
    config_dir = config_home / 'opencode'
    config_dir.mkdir(parents=True)
    (config_dir / 'opencode.json').symlink_to(outside)

    monkeypatch.setattr(opencode_serve, 'OPENCODE_HOME_ROOT', home)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_XDG_CONFIG_HOME', config_home)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_XDG_DATA_HOME', data_home)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_WORKSPACE_DIR', workspace)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_SERVE_LOG_PATH', log_path)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_BUNDLED_CONFIG_PATH', template)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_REPO_CONFIG_PATH', template)

    with pytest.raises(OSError, match=r'symlink|regular file'):
        opencode_serve.EnsureOpenCodeRuntimeDirectories()
    assert outside.read_text(encoding='utf-8') == '{"agent":{"evil":true}}\n'


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


def test_start_opencode_serve_keeps_exa_websearch_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """検索専用モードでも OPENCODE_ENABLE_EXA は製品 serve へ渡す。"""

    home = tmp_path / 'opencode-home'
    config_home = home / 'config'
    data_home = home / 'data'
    workspace = tmp_path / 'opencode-workspace'
    log_path = tmp_path / 'logs' / 'opencode-serve.log'
    template = tmp_path / 'template-opencode.json'
    template.write_text('{"agent":{}}\n', encoding='utf-8')
    binary = tmp_path / 'opencode'
    binary.write_text('#!/bin/sh\n', encoding='utf-8')
    binary.chmod(0o755)

    monkeypatch.setattr(opencode_serve, 'OPENCODE_HOME_ROOT', home)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_XDG_CONFIG_HOME', config_home)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_XDG_DATA_HOME', data_home)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_WORKSPACE_DIR', workspace)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_SERVE_LOG_PATH', log_path)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_BUNDLED_CONFIG_PATH', template)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_REPO_CONFIG_PATH', template)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_SERVE_PID_PATH', home / 'opencode-serve.pid')
    monkeypatch.setattr(opencode_serve, 'ResolveOpenCodeExecutable', lambda: binary)
    monkeypatch.setattr(opencode_serve, 'ReclaimStaleOpenCodeServe', lambda: None)
    monkeypatch.setattr(opencode_serve, '_isListeningOnProductPort', lambda: False)
    monkeypatch.setattr(
        opencode_serve,
        '_GetProcessIdentity',
        lambda pid: opencode_serve._OpenCodeProcessIdentity(
            pid=pid,
            start_time_ticks=12345,
        ),
    )

    def FakeTerminateProcessGroup(
        _identity: opencode_serve._OpenCodeProcessIdentity,
        *,
        process: subprocess.Popen[bytes] | None = None,
        timeout_sec: float = 5.0,
    ) -> None:
        _ = timeout_sec
        if process is not None:
            process.terminate()

    monkeypatch.setattr(opencode_serve, '_TerminateProcessGroup', FakeTerminateProcessGroup)
    monkeypatch.setenv('OPENCODE_ENABLE_EXA', '1')
    monkeypatch.setenv('OPENCODE_OTHER', 'drop-me')

    captured: dict[str, object] = {}

    class FakePopen:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            captured['env'] = kwargs['env']
            self.pid = 4242
            self.returncode = None

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            _ = timeout
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(opencode_serve.subprocess, 'Popen', FakePopen)
    monkeypatch.setattr(
        opencode_serve,
        'CheckOpenCodeHealth',
        lambda: (True, opencode_serve.OPENCODE_PINNED_VERSION),
    )
    monkeypatch.setattr(opencode_serve, 'OPENCODE_HEALTH_RETRY_ATTEMPTS', 1)
    monkeypatch.setattr(opencode_serve, 'OPENCODE_HEALTH_RETRY_INTERVAL_SEC', 0)

    assert opencode_serve.StartOpenCodeServe() is True
    env = captured['env']
    assert isinstance(env, dict)
    assert env.get('OPENCODE_ENABLE_EXA') == '1'
    assert 'OPENCODE_OTHER' not in env
    opencode_serve.StopOpenCodeServe()


def test_reclaim_missing_pid_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PID ファイルが無い reclaim は例外にしない。"""

    monkeypatch.setattr(
        opencode_serve,
        'OPENCODE_SERVE_PID_PATH',
        tmp_path / 'missing.pid',
    )
    opencode_serve.ReclaimStaleOpenCodeServe()


def test_reclaim_legacy_pid_file_does_not_signal_reused_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """starttime の無い旧 PID ファイルは signal を送らず破棄する。"""

    pid_path = tmp_path / 'legacy.pid'
    pid_path.write_text('4242\n', encoding='utf-8')
    terminated: list[opencode_serve._OpenCodeProcessIdentity] = []
    monkeypatch.setattr(opencode_serve, 'OPENCODE_SERVE_PID_PATH', pid_path)
    monkeypatch.setattr(
        opencode_serve,
        '_TerminateProcessGroup',
        lambda identity: terminated.append(identity),
    )

    opencode_serve.ReclaimStaleOpenCodeServe()

    assert terminated == []
    assert pid_path.exists() is False


def test_terminate_process_group_uses_term_then_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """猶予後も group が残る場合は process group へ TERM、KILL の順で送る。"""

    identity = opencode_serve._OpenCodeProcessIdentity(pid=4242, start_time_ticks=100)
    sent_signals: list[signal.Signals] = []
    monkeypatch.setattr(opencode_serve, '_ReadProcessStartTime', lambda _pid: 100)
    monkeypatch.setattr(opencode_serve.os, 'getpgid', lambda pid: pid)
    monkeypatch.setattr(opencode_serve, '_IsProcessGroupAlive', lambda _pgid: True)
    monkeypatch.setattr(
        opencode_serve.os,
        'killpg',
        lambda _pgid, sent_signal: sent_signals.append(sent_signal),
    )

    opencode_serve._TerminateProcessGroup(identity, timeout_sec=0)

    assert sent_signals == [signal.SIGTERM, signal.SIGKILL]


def test_terminate_process_group_stops_before_kill_after_pid_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TERM 待機中に PID starttime が変わった場合は別 group への KILL を中止する。"""

    identity = opencode_serve._OpenCodeProcessIdentity(pid=4242, start_time_ticks=100)
    observed_start_times = iter([100, 100, 200])
    sent_signals: list[signal.Signals] = []
    monkeypatch.setattr(
        opencode_serve,
        '_ReadProcessStartTime',
        lambda _pid: next(observed_start_times),
    )
    monkeypatch.setattr(opencode_serve.os, 'getpgid', lambda pid: pid)
    monkeypatch.setattr(opencode_serve, '_IsProcessGroupAlive', lambda _pgid: True)
    monkeypatch.setattr(
        opencode_serve.os,
        'killpg',
        lambda _pgid, sent_signal: sent_signals.append(sent_signal),
    )

    opencode_serve._TerminateProcessGroup(identity, timeout_sec=0)

    assert sent_signals == [signal.SIGTERM]


def test_terminate_process_group_kills_term_ignoring_child() -> None:
    """serve の子 process が TERM を無視しても group KILL で残留させない。"""

    parent_script = (
        'import signal, subprocess, sys, time; '
        'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
        'child = subprocess.Popen([sys.executable, "-c", '
        '"import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"]); '
        'print(child.pid, flush=True); time.sleep(60)'
    )
    process = subprocess.Popen(
        [sys.executable, '-c', parent_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        text=True,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline().strip())
    parent_identity = opencode_serve._GetProcessIdentity(process.pid)
    child_identity = opencode_serve._GetProcessIdentity(child_pid)
    assert parent_identity is not None
    assert child_identity is not None

    try:
        opencode_serve._TerminateProcessGroup(
            parent_identity,
            process=process,
            timeout_sec=0.2,
        )
        process.wait(timeout=2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and opencode_serve._IsProcessIdentityCurrent(child_identity):
            time.sleep(0.05)
        assert opencode_serve._IsProcessIdentityCurrent(child_identity) is False
    finally:
        if opencode_serve._IsProcessGroupAlive(process.pid):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
