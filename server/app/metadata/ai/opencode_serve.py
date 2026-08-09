"""製品用 opencode serve の常駐起動・health・orphan 回収。

KonomiTV.py の reload 親プロセスから 1 回だけ StartOpenCodeServe() を呼ぶ。
失敗しても本体は継続し、IsOpenCodeAvailable() が False を返す。
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
from app.metadata.ai.opencode_types import (
    KonomiTVBS4KOpenCodeProviderConfig,
    KonomiTVBS4KOpenCodeProviderModel,
    KonomiTVBS4KOpenCodeProviderOptions,
)


# モジュール import 時点では logging を初期化していない可能性があるため遅延 import する。
_process_lock = threading.RLock()
# 自プロセスが spawn した Popen。None は未起動または外部回収のみ。
_serve_process: subprocess.Popen[bytes] | None = None
# 自プロセスが spawn した PID と Linux starttime。PID 再利用時の誤 kill 防止に使う。
_serve_process_identity: _OpenCodeProcessIdentity | None = None
# health 成功後に True。失敗・停止後は False。
_opencode_available = False
# atexit 登録は 1 回だけ。
_atexit_registered = False


@dataclass(frozen=True)
class _OpenCodeProcessIdentity:
    """OpenCode serve の PID と Linux 起動時刻を組にした identity。"""

    pid: int
    start_time_ticks: int


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


def _ReadRegularFileText(path: Path) -> str | None:
    """symlink を辿らず、通常ファイルだけを読む。

    Args:
        path: 読み取り対象パス。

    Returns:
        ファイル本文。不在なら None。

    Raises:
        OSError: symlink または通常ファイル以外の場合。
    """

    # open 前に lstat で判定し、symlink を明確なメッセージで拒否する。
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(path_stat.st_mode):
        raise OSError(f'OpenCode product config path must not be a symlink: {path}')
    if stat.S_ISREG(path_stat.st_mode) is False:
        raise OSError(f'OpenCode product config must be a regular file: {path}')

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        file_stat = os.fstat(fd)
        if stat.S_ISREG(file_stat.st_mode) is False:
            raise OSError(f'OpenCode product config must be a regular file: {path}')
        return os.read(fd, file_stat.st_size + 1).decode('utf-8')
    finally:
        os.close(fd)


def _WriteOpenCodeConfigAtomically(path: Path, content: str) -> None:
    """製品用 opencode.json を一時ファイル + fsync + replace で原子的に書く。

    Args:
        path: 最終 config パス。
        content: テンプレート本文。

    Returns:
        None

    Raises:
        OSError: symlink 拒否、書き込み失敗、権限固定失敗。
    """

    # 最終パスが symlink のときは追跡せず拒否する。権限境界を差し替えられないようにする。
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        path_stat = None
    if path_stat is not None and stat.S_ISLNK(path_stat.st_mode):
        raise OSError(f'OpenCode product config path must not be a symlink: {path}')
    if path_stat is not None and stat.S_ISREG(path_stat.st_mode) is False:
        raise OSError(f'OpenCode product config must be a regular file: {path}')

    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    temporary_fd, temporary_path = tempfile.mkstemp(
        prefix='.opencode.json.',
        suffix='.tmp',
        dir=directory,
    )
    try:
        with os.fdopen(temporary_fd, 'w', encoding='utf-8') as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fchmod(temporary_file.fileno(), 0o600)
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        # rename 後の directory entry も永続化し、再起動後に旧 allow 設定へ戻ることを防ぐ。
        directory_fd = os.open(
            directory,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY,
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary_path)
        except OSError:
            # 原因例外を隠さない。ランダム名の一時ファイルは製品configとして読み込まれない。
            pass
        raise

    # replace 後の最終 inode を O_NOFOLLOW で開き、umask に依らず 0600 へ固定する。
    final_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fchmod(final_fd, 0o600)
    finally:
        os.close(final_fd)


def _BuildKonomiTVBS4KOpenCodeRuntimeConfig(template_text: str) -> str:
    """正本テンプレートへ登録済みカスタム provider を合成する。

    API キーは OpenCode の auth ストアへ別経路で注入するため、この設定には
    provider 種別・base URL・model ID 以外を含めない。

    Args:
        template_text: docker/opencode/opencode.json の本文。

    Returns:
        カスタム provider を合成した製品用 OpenCode 設定 JSON。

    Raises:
        OSError: テンプレートまたは AI service 設定が不正な場合。
    """

    from app.metadata.ai.AIBackendSettings import AIBackendSettingsStore

    try:
        config = json.loads(template_text)
        services = AIBackendSettingsStore.listServices()
    except (json.JSONDecodeError, TypeError, ValueError, OSError) as error:
        raise OSError('Failed to build OpenCode product config.') from error
    if isinstance(config, dict) is False:
        raise OSError('OpenCode product config template must be a JSON object.')

    custom_services = [
        service for service in services
        if service.opencode_provider_type != 'Catalog'
    ]
    if not custom_services:
        return template_text

    raw_providers = config.get('provider')
    providers: dict[str, object] = dict(raw_providers) if isinstance(raw_providers, dict) else {}
    for service in custom_services:
        assert service.api_base_url is not None
        npm_package: Literal['@ai-sdk/openai-compatible', '@ai-sdk/anthropic']
        if service.opencode_provider_type == 'OpenAICompatible':
            npm_package = '@ai-sdk/openai-compatible'
        else:
            npm_package = '@ai-sdk/anthropic'
        model = KonomiTVBS4KOpenCodeProviderModel(name=service.opencode_model_id)
        provider = KonomiTVBS4KOpenCodeProviderConfig(
            npm=npm_package,
            name=service.service_name,
            options=KonomiTVBS4KOpenCodeProviderOptions(baseURL=service.api_base_url),
            models={service.opencode_model_id: model},
        )
        providers[service.opencode_provider_id] = provider

    if providers:
        config['provider'] = providers
    else:
        config.pop('provider', None)
    return json.dumps(config, ensure_ascii=False, indent=2) + '\n'


def SyncKonomiTVBS4KOpenCodeRuntimeConfig() -> bool:
    """正本テンプレートと AI service から runtime config を再生成する。

    Returns:
        ファイル内容を更新した場合は True、既に同一なら False。

    Raises:
        OSError: テンプレート読込・設定生成・書込に失敗した場合。
    """

    config_dir = OPENCODE_XDG_CONFIG_HOME / 'opencode'
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / 'opencode.json'
    # bind source tree がある開発環境では変更中の repo template を優先する。
    # 本番イメージには repo path が無いため、immutable な bundled template へフォールバックする。
    source = OPENCODE_REPO_CONFIG_PATH
    if source.is_file() is False:
        source = OPENCODE_BUNDLED_CONFIG_PATH
    if source.is_file() is False:
        raise FileNotFoundError(
            f'OpenCode product config template not found: {source}',
        )
    runtime_text = _BuildKonomiTVBS4KOpenCodeRuntimeConfig(source.read_text(encoding='utf-8'))
    current_text = _ReadRegularFileText(config_path)
    if current_text != runtime_text:
        _WriteOpenCodeConfigAtomically(config_path, runtime_text)
        return True

    # 内容が同じでも権限だけ劣化するケースを修復する。
    config_fd = os.open(config_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fchmod(config_fd, 0o600)
    finally:
        os.close(config_fd)
    return False


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

    # OpenCode は XDG_CONFIG_HOME/opencode/opencode.json を読む。製品用 agent permission
    # と登録済みカスタム provider を起動のたびに正本から再生成する。
    # R-08 の検索専用モード (websearch allow / webfetch deny) も既存 home へ適用する。
    SyncKonomiTVBS4KOpenCodeRuntimeConfig()

    # workspace にソースを置かないことを保証する（.gitkeep のみ許可）。
    for child in OPENCODE_WORKSPACE_DIR.iterdir():
        if child.name in {'.gitkeep', '.gitignore'}:
            continue
        # 予期しないファイルがあっても削除はしない（ユーザーデータ保護）。警告は呼び出し側ログ。
        break


def _ReadProcessStartTime(pid: int) -> int | None:
    """Linux /proc から PID の starttime tick を読む。

    Args:
        pid: 読み取り対象 PID。

    Returns:
        /proc/<pid>/stat field 22 の starttime。不在・不正なら None。
    """

    try:
        process_stat = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8')
    except OSError:
        return None
    # comm field は空白や ')' を含み得るため、最後の ')' より後ろから field 3 以降を分割する。
    _prefix, separator, remaining_fields_text = process_stat.rpartition(')')
    if separator == '':
        return None
    remaining_fields = remaining_fields_text.strip().split()
    # remaining_fields[0] が field 3 (state) なので field 22 (starttime) は index 19。
    if len(remaining_fields) <= 19 or remaining_fields[19].isdigit() is False:
        return None
    start_time_ticks = int(remaining_fields[19])
    if start_time_ticks <= 0:
        return None
    return start_time_ticks


def _GetProcessIdentity(pid: int) -> _OpenCodeProcessIdentity | None:
    """現在の PID から再利用検証用 identity を構築する。

    Args:
        pid: 対象 PID。

    Returns:
        有効な PID/starttime。対象が不在・不正なら None。
    """

    if pid <= 1:
        return None
    start_time_ticks = _ReadProcessStartTime(pid)
    if start_time_ticks is None:
        return None
    return _OpenCodeProcessIdentity(pid=pid, start_time_ticks=start_time_ticks)


def _readPidFile() -> _OpenCodeProcessIdentity | None:
    """PID ファイルから PID と starttime の組を読む。

    Returns:
        process identity。不正・不在・旧 PID 単独形式なら None。
    """

    if OPENCODE_SERVE_PID_PATH.is_file() is False:
        return None
    try:
        raw = OPENCODE_SERVE_PID_PATH.read_text(encoding='utf-8').strip()
    except OSError:
        return None
    fields = raw.split()
    # PID 単独の旧形式は starttime を証明できず、PID 再利用時の誤 kill になるため受理しない。
    if len(fields) != 2 or any(field.isdigit() is False for field in fields):
        return None
    pid = int(fields[0])
    start_time_ticks = int(fields[1])
    if pid <= 1 or start_time_ticks <= 0:
        return None
    return _OpenCodeProcessIdentity(pid=pid, start_time_ticks=start_time_ticks)


def _IsProcessIdentityCurrent(identity: _OpenCodeProcessIdentity) -> bool:
    """PID が現在も保存済み starttime と一致するかを返す。

    Args:
        identity: 検証する PID/starttime。

    Returns:
        同じプロセスが現在も存在すれば True。
    """

    return _ReadProcessStartTime(identity.pid) == identity.start_time_ticks


def _IsProcessGroupAlive(process_group_id: int) -> bool:
    """process group に signal 可能なプロセスが残っているかを返す。

    Args:
        process_group_id: 確認する process group ID。

    Returns:
        group が存在すれば True。権限不足も存在扱い。
    """

    try:
        os.killpg(process_group_id, 0)
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


def _TerminateProcessGroup(
    identity: _OpenCodeProcessIdentity,
    *,
    process: subprocess.Popen[bytes] | subprocess.Popen[str] | None = None,
    timeout_sec: float = 5.0,
) -> None:
    """identity を検証して process group へ TERM、待機、KILL の順で停止する。

    Args:
        identity: 起動時に記録した PID/starttime。
        process: 自プロセスが spawn した場合の Popen。poll で leader を reap する。
        timeout_sec: SIGTERM 後に group の終了を待つ秒数。

    Returns:
        None
    """

    # PID が再利用済みなら無関係な process group へ signal を送らない。
    if _IsProcessIdentityCurrent(identity) is False:
        return
    try:
        process_group_id = os.getpgid(identity.pid)
    except (ProcessLookupError, PermissionError):
        return
    # start_new_session=True の session leader だけを回収対象にし、呼び出し元 group を巻き込まない。
    if process_group_id != identity.pid or _IsProcessIdentityCurrent(identity) is False:
        return
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return

    # leader が zombie のままだと group 生存判定に残るため、管理中 Popen は poll で随時 reap する。
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if process is not None:
            process.poll()
        if _IsProcessGroupAlive(process_group_id) is False:
            return
        time.sleep(0.1)

    if process is not None:
        process.poll()
    if _IsProcessGroupAlive(process_group_id) is False:
        return
    # TERM 待機中に同じ PID が別プロセスへ再利用された場合は KILL を中止する。
    current_start_time = _ReadProcessStartTime(identity.pid)
    if current_start_time is not None and current_start_time != identity.start_time_ticks:
        return
    try:
        os.killpg(process_group_id, signal.SIGKILL)
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

    identity = _readPidFile()
    if identity is not None:
        _TerminateProcessGroup(identity)
    # identity を証明できない旧形式・破損ファイルも signal は送らず破棄する。
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


def _writePidFile(identity: _OpenCodeProcessIdentity) -> None:
    """PID と starttime を atomic に書き込む。

    Args:
        identity: spawn 直後に取得した PID/starttime。

    Returns:
        None
    """

    OPENCODE_HOME_ROOT.mkdir(parents=True, exist_ok=True)
    temporary_path = OPENCODE_SERVE_PID_PATH.with_name(
        f'.{OPENCODE_SERVE_PID_PATH.name}.{os.getpid()}.tmp',
    )
    temporary_path.write_text(
        f'{identity.pid} {identity.start_time_ticks}\n',
        encoding='utf-8',
    )
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, OPENCODE_SERVE_PID_PATH)


def StopOpenCodeServe() -> None:
    """自前 spawn した serve を terminate+wait し、availability を落とす。

    Returns:
        None
    """

    global _serve_process, _serve_process_identity, _opencode_available
    with _process_lock:
        process = _serve_process
        managed_identity = _serve_process_identity
        _serve_process = None
        _serve_process_identity = None
        _opencode_available = False
        if process is not None:
            if process.poll() is None:
                if managed_identity is not None:
                    _TerminateProcessGroup(managed_identity, process=process)
                else:
                    # /proc identity 取得前の起動失敗だけは Popen が指す直接の子を最低限回収する。
                    process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # identity が無い例外経路では group を安全に特定できないため、直接の子だけを KILL する。
                if managed_identity is None:
                    process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        persisted_identity = _readPidFile()
        if persisted_identity is not None and persisted_identity != managed_identity:
            _TerminateProcessGroup(persisted_identity)
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

    global _serve_process, _serve_process_identity, _opencode_available, _atexit_registered

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
        process_identity = _GetProcessIdentity(process.pid)
        if process_identity is None:
            logging.warning(
                'Failed to capture OpenCode process identity. Stopping unmanaged child.',
            )
            StopOpenCodeServe()
            return False
        _serve_process_identity = process_identity
        try:
            _writePidFile(process_identity)
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
        persisted_identity = _readPidFile()
        pid = (
            _serve_process.pid
            if _serve_process is not None
            else persisted_identity.pid
            if persisted_identity is not None
            else None
        )
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
