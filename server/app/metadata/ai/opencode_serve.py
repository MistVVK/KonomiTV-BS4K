"""製品用 opencode serve の常駐起動・health・orphan 回収。

KonomiTV.py の reload 親プロセスから 1 回だけ StartOpenCodeServe() を呼ぶ。
失敗しても本体は継続し、IsOpenCodeAvailable() が False を返す。
"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import httpx

from app.constants import (
    LIBRARY_PATH,
    OPENCODE_AGENT_EPISODE,
    OPENCODE_AGENT_GENERATE,
    OPENCODE_BUNDLED_CONFIG_PATH,
    OPENCODE_HEALTH_RETRY_ATTEMPTS,
    OPENCODE_HEALTH_RETRY_INTERVAL_SEC,
    OPENCODE_HEALTH_TIMEOUT_SEC,
    OPENCODE_HOME_ROOT,
    OPENCODE_PINNED_VERSION,
    OPENCODE_REPO_CONFIG_PATH,
    OPENCODE_SERVE_BASE_URL,
    OPENCODE_SERVE_HOST,
    OPENCODE_SERVE_LOG_PATH,
    OPENCODE_SERVE_PID_PATH,
    OPENCODE_SERVE_PORT,
    OPENCODE_WORKSPACE_DIR,
    OPENCODE_XDG_CONFIG_HOME,
    OPENCODE_XDG_DATA_HOME,
)


# モジュール import 時点では logging を初期化していない可能性があるため遅延 import する。
_process_lock = threading.RLock()
# 自プロセスが spawn した Popen。None は未起動または外部回収のみ。
_serve_process: subprocess.Popen[bytes] | None = None
# health 成功後に True。失敗・停止後は False。
_opencode_available = False
# atexit 登録は 1 回だけ。
_atexit_registered = False


def IsOpenCodeAvailable() -> bool:
    """製品用 opencode serve が health OK かを返す。

    Returns:
        health 成功済みかつプロセスが生存していれば True。
    """

    with _process_lock:
        if _opencode_available is False:
            return False
        if _serve_process is not None and _serve_process.poll() is not None:
            return False
        return True


def GetOpenCodeBaseURL() -> str:
    """製品用 serve の base URL を返す。

    Returns:
        http://127.0.0.1:4097 形式の URL。
    """

    return OPENCODE_SERVE_BASE_URL


def ResolveOpenCodeExecutable() -> Path | None:
    """opencode 実行ファイルのパスを解決する。

    Returns:
        実行可能な Path。見つからなければ None。
    """

    candidates = [
        Path(LIBRARY_PATH['OpenCode']),
        Path(shutil.which('opencode') or ''),
    ]
    for candidate in candidates:
        if candidate == Path(''):
            continue
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def EnsureOpenCodeRuntimeDirectories() -> None:
    """home / workspace / logs を作成し、製品用 config を seed する。

    Returns:
        None

    Raises:
        OSError: ディレクトリ作成や config 書き込みに失敗した場合。
    """

    for directory in (
        OPENCODE_HOME_ROOT,
        OPENCODE_XDG_CONFIG_HOME,
        OPENCODE_XDG_DATA_HOME,
        OPENCODE_WORKSPACE_DIR,
        OPENCODE_SERVE_LOG_PATH.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    # OpenCode は XDG_CONFIG_HOME/opencode/opencode.json を読む。
    config_dir = OPENCODE_XDG_CONFIG_HOME / 'opencode'
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / 'opencode.json'
    if config_path.is_file() is False:
        source = OPENCODE_BUNDLED_CONFIG_PATH
        if source.is_file() is False:
            source = OPENCODE_REPO_CONFIG_PATH
        if source.is_file() is False:
            raise FileNotFoundError(
                f'OpenCode product config template not found: {source}',
            )
        config_path.write_text(source.read_text(encoding='utf-8'), encoding='utf-8')
        os.chmod(config_path, 0o600)

    # workspace にソースを置かないことを保証する（.gitkeep のみ許可）。
    for child in OPENCODE_WORKSPACE_DIR.iterdir():
        if child.name in {'.gitkeep', '.gitignore'}:
            continue
        # 予期しないファイルがあっても削除はしない（ユーザーデータ保護）。警告は呼び出し側ログ。
        break


def _readPidFile() -> int | None:
    """PID ファイルから整数 PID を読む。

    Returns:
        PID。不正・不在時は None。
    """

    if OPENCODE_SERVE_PID_PATH.is_file() is False:
        return None
    try:
        raw = OPENCODE_SERVE_PID_PATH.read_text(encoding='utf-8').strip()
    except OSError:
        return None
    if raw.isdigit() is False:
        return None
    pid = int(raw)
    if pid <= 1:
        return None
    return pid


def _isProcessAlive(pid: int) -> bool:
    """PID が生存しているか（権限不足は生存扱い）を返す。"""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _isListeningOnProductPort() -> bool:
    """127.0.0.1:4097 が LISTEN しているかを /proc/net/tcp で判定する。"""

    # ポートを 16 進（ホストバイト順ではなくネットワークバイト順表記）で探す。
    port_hex = f'{OPENCODE_SERVE_PORT:04X}'
    try:
        lines = Path('/proc/net/tcp').read_text(encoding='utf-8').splitlines()
    except OSError:
        return False
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        local_address = parts[1]
        state = parts[3]
        # 0A = LISTEN
        if state != '0A':
            continue
        if local_address.endswith(f':{port_hex}') is False:
            continue
        ip_hex = local_address.split(':', 1)[0]
        # 127.0.0.1 = 0100007F（little-endian hex）
        if ip_hex.upper() in {'0100007F', '00000000'}:
            return True
    return False


def _terminatePid(pid: int, *, timeout_sec: float = 5.0) -> None:
    """PID に SIGTERM → 待機 → 必要なら SIGKILL。"""

    if _isProcessAlive(pid) is False:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        return
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if _isProcessAlive(pid) is False:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def ReclaimStaleOpenCodeServe() -> None:
    """旧 PID ファイルと 4097 の残留プロセスを回収する。

    他人の無関係なプロセスを殺さないよう、PID ファイルの PID を優先する。
    PID ファイルが無く port だけが開いている場合は、可能なら接続で health を見て
    自前 serve と判定できなければ触らない（警告は Start 側）。

    Returns:
        None
    """

    pid = _readPidFile()
    if pid is not None:
        if _isProcessAlive(pid):
            _terminatePid(pid)
        try:
            OPENCODE_SERVE_PID_PATH.unlink(missing_ok=True)
        except OSError:
            pass


def CheckOpenCodeHealth(*, timeout_sec: float = OPENCODE_HEALTH_TIMEOUT_SEC) -> tuple[bool, str | None]:
    """GET /global/health を同期的に叩く。

    Args:
        timeout_sec: httpx タイムアウト秒。

    Returns:
        (healthy, version_or_error)。healthy 時は version 文字列。
    """

    try:
        with httpx.Client(base_url=OPENCODE_SERVE_BASE_URL, timeout=timeout_sec) as client:
            response = client.get('/global/health')
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as error:
        return False, str(error)
    if isinstance(payload, dict) is False:
        return False, 'invalid health payload'
    if payload.get('healthy') is not True:
        return False, 'healthy is not true'
    version = payload.get('version')
    if isinstance(version, str) is False or version == '':
        return False, 'missing version'
    return True, version


def _writePidFile(pid: int) -> None:
    """PID を atomic に書き込む。"""

    OPENCODE_HOME_ROOT.mkdir(parents=True, exist_ok=True)
    temporary_path = OPENCODE_SERVE_PID_PATH.with_name(
        f'.{OPENCODE_SERVE_PID_PATH.name}.{os.getpid()}.tmp',
    )
    temporary_path.write_text(f'{pid}\n', encoding='utf-8')
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, OPENCODE_SERVE_PID_PATH)


def StopOpenCodeServe() -> None:
    """自前 spawn した serve を terminate+wait し、availability を落とす。

    Returns:
        None
    """

    global _serve_process, _opencode_available
    with _process_lock:
        process = _serve_process
        _serve_process = None
        _opencode_available = False
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
        pid = _readPidFile()
        if pid is not None:
            _terminatePid(pid)
        try:
            OPENCODE_SERVE_PID_PATH.unlink(missing_ok=True)
        except OSError:
            pass


def StartOpenCodeServe() -> bool:
    """製品用 opencode serve を起動し health を待つ。

    reload 親で 1 回だけ呼ぶ想定。失敗時は warning 相当を返し available=false。

    Returns:
        health 成功なら True。
    """

    global _serve_process, _opencode_available, _atexit_registered

    # logging は KonomiTV.py が SplitServerLog 後に import する。
    from app import logging

    with _process_lock:
        if _serve_process is not None and _serve_process.poll() is None and _opencode_available:
            return True

        executable = ResolveOpenCodeExecutable()
        if executable is None:
            logging.warning(
                'OpenCode binary not found. Recorded-series AI via OpenCode is unavailable.',
            )
            _opencode_available = False
            return False

        try:
            EnsureOpenCodeRuntimeDirectories()
        except OSError as error:
            logging.warning(
                f'Failed to prepare OpenCode runtime directories: {error}',
            )
            _opencode_available = False
            return False

        # orphan 回収（旧 PID）。port 残留は PID 経由で落とす。
        ReclaimStaleOpenCodeServe()
        if _isListeningOnProductPort():
            # 回収後も LISTEN なら、既存 health が取れれば流用せず落とす方針。
            # 製品 port は自前専用なので、PID 不明でも terminate はしないが available は false。
            healthy, detail = CheckOpenCodeHealth()
            if healthy:
                logging.warning(
                    'OpenCode product port is already in use by an unmanaged process '
                    f'(version={detail}). Refusing to attach. Set available=false.',
                )
            else:
                logging.warning(
                    'OpenCode product port is occupied and health failed. '
                    'Recorded-series AI via OpenCode is unavailable.',
                )
            _opencode_available = False
            return False

        environment = os.environ.copy()
        environment['XDG_CONFIG_HOME'] = str(OPENCODE_XDG_CONFIG_HOME)
        environment['XDG_DATA_HOME'] = str(OPENCODE_XDG_DATA_HOME)
        # 監査用やユーザー shell の OPENCODE_* を持ち込まない。
        # ただし OPENCODE_ENABLE_EXA は Exa AI のホスト型 websearch（API キー不要）を
        # 有効化する機能フラグのため、製品 serve へ明示的に維持する。
        allowed_opencode_env_keys = {'OPENCODE_ENABLE_EXA'}
        for key in list(environment):
            if key.startswith('OPENCODE_') and key not in allowed_opencode_env_keys:
                environment.pop(key, None)

        try:
            log_file = open(OPENCODE_SERVE_LOG_PATH, mode='a', encoding='utf-8')
        except OSError as error:
            logging.warning(f'Failed to open OpenCode serve log: {error}')
            _opencode_available = False
            return False

        command = [
            str(executable),
            'serve',
            '--hostname', OPENCODE_SERVE_HOST,
            '--port', str(OPENCODE_SERVE_PORT),
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=str(OPENCODE_WORKSPACE_DIR),
                env=environment,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
        except OSError as error:
            log_file.close()
            logging.warning(f'Failed to spawn OpenCode serve: {error}')
            _opencode_available = False
            return False
        finally:
            # Popen が FD を継承したあとは親側を閉じる。
            try:
                log_file.close()
            except OSError:
                pass

        _serve_process = process
        try:
            _writePidFile(process.pid)
        except OSError as error:
            logging.warning(f'Failed to write OpenCode PID file: {error}')

        if _atexit_registered is False:
            atexit.register(StopOpenCodeServe)
            _atexit_registered = True

        # health リトライ
        last_error = 'timeout'
        for _ in range(OPENCODE_HEALTH_RETRY_ATTEMPTS):
            if process.poll() is not None:
                last_error = f'process exited with code {process.returncode}'
                break
            healthy, detail = CheckOpenCodeHealth()
            if healthy:
                version = detail or ''
                if version != OPENCODE_PINNED_VERSION:
                    logging.warning(
                        f'OpenCode serve version mismatch: got {version!r}, '
                        f'expected {OPENCODE_PINNED_VERSION!r}. Continuing.',
                    )
                _opencode_available = True
                logging.info(
                    f'OpenCode serve is ready at {OPENCODE_SERVE_BASE_URL} '
                    f'(version={version}, agents={OPENCODE_AGENT_GENERATE}/'
                    f'{OPENCODE_AGENT_EPISODE}).',
                )
                return True
            last_error = detail or 'health failed'
            time.sleep(OPENCODE_HEALTH_RETRY_INTERVAL_SEC)

        logging.warning(
            f'OpenCode serve health check failed: {last_error}. '
            'Recorded-series AI via OpenCode is unavailable.',
        )
        StopOpenCodeServe()
        return False


def ProbeOpenCodeAvailability() -> dict[str, object]:
    """管理 API 向けに availability スナップショットを返す。

    Returns:
        available / base_url / version / pid を含む dict。秘密は含まない。
    """

    with _process_lock:
        available = IsOpenCodeAvailable()
        pid = _serve_process.pid if _serve_process is not None else _readPidFile()
    version: str | None = None
    if available:
        healthy, detail = CheckOpenCodeHealth()
        if healthy:
            version = detail
        else:
            available = False
    return {
        'available': available,
        'base_url': OPENCODE_SERVE_BASE_URL,
        'host': OPENCODE_SERVE_HOST,
        'port': OPENCODE_SERVE_PORT,
        'version': version,
        'pinned_version': OPENCODE_PINNED_VERSION,
        'pid': pid,
        'workspace': str(OPENCODE_WORKSPACE_DIR),
    }
