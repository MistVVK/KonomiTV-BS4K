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
