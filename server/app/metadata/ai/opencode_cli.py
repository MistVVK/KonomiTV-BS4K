"""listener-free な OpenCode CLI 実行と製品用認証ストアを管理する。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, cast

import httpx

from app import logging
from app.constants import (
    OPENCODE_AGENT_EPISODE,
    OPENCODE_AUTH_PATH,
    OPENCODE_MODELS_CACHE_PATH,
    OPENCODE_WORKSPACE_DIR,
)
from app.metadata.ai.ai_egress_policy import BuildOpenCodeEpisodeToolPermissions
from app.metadata.ai.opencode_runtime import (
    BuildOpenCodeProcessEnvironment,
    EnsureOpenCodeRuntimeDirectories,
    ReadOpenCodeRegularFileText,
    ResolveOpenCodeExecutable,
    WriteOpenCodeFileAtomically,
)
from app.metadata.ai.opencode_types import (
    NormalizeOpenCodeUsage,
    OpenCodeApiAuth,
    OpenCodeNormalizedUsage,
    OpenCodeTokenUsage,
)


_OPENCODE_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_OPENCODE_SESSION_CLEANUP_TIMEOUT_SEC = 10.0


class OpenCodeCLIError(Exception):
    """OpenCode CLI の秘密を含まない失敗。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        session_cleaned_up: bool = False,
    ) -> None:
        """エラーを初期化する。

        Args:
            message: ログ・API 向けの英語メッセージ。
            status_code: HTTP 応答へ対応付ける状態コード。
            session_cleaned_up: 生成された CLI session を削除できたか。

        Returns:
            None
        """

        super().__init__(message)
        self.status_code = status_code
        self.session_cleaned_up = session_cleaned_up


class OpenCodeUnavailableError(OpenCodeCLIError):
    """OpenCode CLI を起動できない場合の失敗。"""

    def __init__(self) -> None:
        """利用不能エラーを初期化する。

        Returns:
            None
        """

        super().__init__('OpenCode CLI is unavailable.', status_code=503)


class _OpenCodeProviderState:
    """同一 provider の認証変更と利用中 CLI 呼び出しを直列化する。"""

    def __init__(self) -> None:
        """provider 状態を初期化する。

        Returns:
            None
        """

        # 認証変更・lease 取得・lease 解放を通知し合う Condition。
        self.condition = asyncio.Condition()
        # 最後に auth.json へ保存した API キーの SHA-256。キー本体は保持しない。
        self.api_key_fingerprint: str | None = None
        # provider の認証状態を現在利用している CLI 呼び出し数。
        self.active_lease_count = 0
        # auth.json の同一 provider entry を変更中または変更待ちなら True。
        self.auth_mutation_pending = False


class OpenCodeProviderLease:
    """provider 認証を CLI 呼び出しの終了まで保持する lease。"""

    def __init__(self, state: _OpenCodeProviderState) -> None:
        """取得済み lease を初期化する。

        Args:
            state: active_lease_count を加算済みの provider 状態。

        Returns:
            None
        """

        # release 時に参照数を戻す provider 状態。
        self._state = state
        # finally と明示 release が重なっても二重解放しないための状態。
        self._released = False

    async def __aenter__(self) -> OpenCodeProviderLease:
        """取得済み lease を async with へ渡す。

        Returns:
            この lease 自身。
        """

        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """処理成否にかかわらず provider lease を解放する。

        Returns:
            None
        """

        await self.release()

    async def release(self, *, invalidate_api_key_fingerprint: bool = False) -> None:
        """provider の active lease 数を1つ減らす。

        Args:
            invalidate_api_key_fingerprint: 外部 OAuth 保存で API entry が置換された場合に、
                記憶済み API キー fingerprint を同じ同期境界で失効するか。

        Returns:
            None
        """

        if self._released:
            return
        async with self._state.condition:
            if self._released:
                return
            self._released = True
            # OAuth 成功時は auth.json の entry が外部 process から置換済みなので、
            # active lease の減算・待機者への通知と同じ condition 境界で fingerprint を
            # 失効する。待機中の API キー取得が古い fingerprint を観測して書き込みを
            # 省略する隙間を作らない。
            if invalidate_api_key_fingerprint:
                self._state.api_key_fingerprint = None
            self._state.active_lease_count -= 1
            assert self._state.active_lease_count >= 0
            self._state.condition.notify_all()


@dataclass(frozen=True)
class _OpenCodeProcessResult:
    """1回の CLI subprocess の有限な出力。"""

    return_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


@dataclass(frozen=True)
class _OpenCodeErrorEvent:
    """秘密を含まない OpenCode error event の分類。"""

    name: str | None
    status_code: int | None


class _OpenCodeProcessCancelled(Exception):
    """キャンセル時に session ID 回収用 stdout だけを内部伝播する。"""

    def __init__(self, stdout: bytes) -> None:
        """回収済み標準出力を保持する。

        Args:
            stdout: kill 後までに回収した NDJSON。

        Returns:
            None
        """

        super().__init__('OpenCode CLI invocation was cancelled.')
        self.stdout = stdout


class _OpenCodeProcessOutputLimitExceeded(OpenCodeCLIError):
    """出力上限到達時に session ID 回収用 stdout だけを内部伝播する。"""

    def __init__(self, stdout: bytes) -> None:
        """上限内で回収できた標準出力を保持する。

        Args:
            stdout: 最大 `_OPENCODE_MAX_OUTPUT_BYTES` の NDJSON。

        Returns:
            None
        """

        super().__init__('OpenCode CLI output exceeded the safety limit.')
        self.stdout = stdout


class _OpenCodeOutputLimitReached(Exception):
    """個々の pipe reader が出力上限へ達したことを示す内部 signal。"""


# OpenCode は SQLite runtime を共有するため、通常 CLI と ephemeral OAuth serve を含む
# 複数 process を同時起動すると database is locked になり得る。全 service の process
# 境界を spawn 前から停止・回収後まで直列化する。
_process_lock = asyncio.Lock()
# provider ごとに lease を分けつつ、単一 auth.json の read-modify-write は全 provider で直列化する。
_auth_file_lock = asyncio.Lock()
# auth.json を変更する操作全体を直列化する全 provider 共通の gate。
# OAuth の token 保存は別 process の opencode serve が auth.json 全体を read-modify-write
# するため _auth_file_lock を取らず、provider 別 lease も同一 provider にしか効かない。
# このままでは xai の OAuth 中に openai の API キー更新が旧 snapshot を読み、後勝ちの
# 全体書き戻しで片方の entry が消える (lost update)。OAuth は serve が entry を保存
# し終えるまで、API キー更新・切断は各 read-modify-write が終わるまでこの gate を保持し、
# 異なる provider 間も直列化する。read-only の catalog 取得は通さない。
# lock 順序は provider condition → _auth_mutation_gate → _auth_file_lock に固定し、
# gate 保持中に provider condition を待たない (デッドロック防止)。
# OAuth は両方の全体 lock が必要なので、provider condition → _auth_mutation_gate →
# _process_lock の順で取得する。
_auth_mutation_gate = asyncio.Lock()
_provider_states: dict[str, _OpenCodeProviderState] = {}
_provider_states_lock = threading.Lock()


def _GetProviderState(provider_id: str) -> _OpenCodeProviderState:
    """provider ID に対応する共有状態を返す。

    Args:
        provider_id: 正規化済み OpenCode provider ID。

    Returns:
        provider 共有状態。
    """

    with _provider_states_lock:
        state = _provider_states.get(provider_id)
        if state is None:
            state = _OpenCodeProviderState()
            _provider_states[provider_id] = state
        return state


async def _StopOpenCodeProcess(process: asyncio.subprocess.Process) -> None:
    """CLI process group を TERM、必要なら KILL で有限時間内に停止する。

    Args:
        process: 停止する subprocess。

    Returns:
        None
    """

    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=3.0)
        return
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=3.0)
    except TimeoutError:
        # SIGKILL 後に wait が戻らない異常でも、呼び出し元の回復を無期限に止めない。
        pass


async def _ReadOpenCodeProcessStream(
    stream: asyncio.StreamReader,
    buffer: bytearray,
) -> None:
    """subprocess pipe を逐次読みし、累積出力を固定上限内に保つ。

    Args:
        stream: stdout または stderr の reader。
        buffer: 呼び出し元が保持する bounded buffer。

    Returns:
        None

    Raises:
        _OpenCodeOutputLimitReached: stream が安全上限を超えた場合。
    """

    while True:
        remaining = _OPENCODE_MAX_OUTPUT_BYTES - len(buffer)
        # 上限到達後も1 byteだけ読んで、上限と上限超過を区別する。
        chunk = await stream.read(min(64 * 1024, remaining + 1))
        if chunk == b'':
            return
        if len(chunk) > remaining:
            if remaining > 0:
                buffer.extend(chunk[:remaining])
            raise _OpenCodeOutputLimitReached()
        buffer.extend(chunk)


async def _FinishOpenCodeProcessTasks(tasks: set[asyncio.Task[Any]]) -> None:
    """process 停止後の wait / pipe reader task を有限時間で回収する。

    Args:
        tasks: process wait と stdout / stderr reader の task。

    Returns:
        None
    """

    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=3.0,
        )
    except TimeoutError:
        # 子孫 process が pipe を保持し続ける異常でも、cleanup を無期限に止めない。
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _RunOpenCodeProcessUnlocked(
    arguments: list[str],
    *,
    timeout_sec: float,
) -> _OpenCodeProcessResult:
    """process lock 取得済みの呼び出し元から OpenCode CLI を1回実行する。

    Args:
        arguments: executable より後ろの引数。
        timeout_sec: process 全体のタイムアウト秒。

    Returns:
        return code と有限な stdout / stderr。

    Raises:
        OpenCodeUnavailableError: binary または runtime を準備できない場合。
        OpenCodeCLIError: pipe の読み取りに失敗した場合。
        _OpenCodeProcessOutputLimitExceeded: 出力上限を超えた場合。
        _OpenCodeProcessCancelled: 呼び出し task がキャンセルされた場合。
    """

    executable = ResolveOpenCodeExecutable()
    if executable is None:
        raise OpenCodeUnavailableError()
    try:
        EnsureOpenCodeRuntimeDirectories()
        process = await asyncio.create_subprocess_exec(
            str(executable),
            *arguments,
            cwd=OPENCODE_WORKSPACE_DIR,
            env=BuildOpenCodeProcessEnvironment(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise OpenCodeUnavailableError() from error

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    stdout_task = asyncio.create_task(_ReadOpenCodeProcessStream(process.stdout, stdout_buffer))
    stderr_task = asyncio.create_task(_ReadOpenCodeProcessStream(process.stderr, stderr_buffer))
    process_task = asyncio.create_task(process.wait())
    process_tasks = {process_task, stdout_task, stderr_task}
    timed_out = False
    try:
        done, _pending = await asyncio.wait(
            process_tasks,
            timeout=timeout_sec,
            return_when=asyncio.FIRST_EXCEPTION,
        )
        task_errors = [
            task.exception()
            for task in done
            if task.cancelled() is False and task.exception() is not None
        ]
        if task_errors:
            await _StopOpenCodeProcess(process)
            await _FinishOpenCodeProcessTasks(process_tasks)
            if any(isinstance(error, _OpenCodeOutputLimitReached) for error in task_errors):
                raise _OpenCodeProcessOutputLimitExceeded(bytes(stdout_buffer))
            raise OpenCodeCLIError('Failed to read OpenCode CLI output.') from task_errors[0]
        if len(done) != len(process_tasks):
            timed_out = True
            await _StopOpenCodeProcess(process)
            await _FinishOpenCodeProcessTasks(process_tasks)
    except asyncio.CancelledError:
        await _StopOpenCodeProcess(process)
        await _FinishOpenCodeProcessTasks(process_tasks)
        raise _OpenCodeProcessCancelled(bytes(stdout_buffer)) from None

    return _OpenCodeProcessResult(
        return_code=process.returncode if process.returncode is not None else -1,
        stdout=bytes(stdout_buffer),
        stderr=bytes(stderr_buffer),
        timed_out=timed_out,
    )


def _StatusCodeFromCLIError(stderr: bytes) -> int | None:
    """秘密を返さず CLI stderr の定型語から状態コードだけを推定する。

    Args:
        stderr: CLI の標準エラー出力。

    Returns:
        対応する状態コード。不明なら None。
    """

    text = stderr.decode('utf-8', errors='replace').lower()
    if 'rate limit' in text or 'too many requests' in text:
        return 429
    if 'unauthorized' in text or 'invalid api key' in text or 'authentication' in text:
        return 401
    if 'forbidden' in text or 'permission denied' in text:
        return 403
    if 'model not found' in text or 'provider not found' in text:
        return 404
    return None


def _ParseOpenCodeEvents(
    stdout: bytes,
) -> tuple[list[dict[str, Any]], str | None, _OpenCodeErrorEvent | None]:
    """CLI NDJSON から parts、session ID、秘密を含まない error 分類を取り出す。

    Args:
        stdout: `opencode run --format json` の標準出力。

    Returns:
        (parts, session_id, error event 分類)。

    Raises:
        OpenCodeCLIError: NDJSON が壊れている場合。
    """

    parts: list[dict[str, Any]] = []
    session_id: str | None = None
    error_event: _OpenCodeErrorEvent | None = None
    for raw_line in stdout.splitlines():
        if raw_line.strip() == b'':
            continue
        try:
            event = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OpenCodeCLIError('OpenCode CLI emitted invalid JSON events.') from error
        if isinstance(event, dict) is False:
            raise OpenCodeCLIError('OpenCode CLI emitted an invalid event.')
        event_type = event.get('type')
        if event_type == 'error':
            # provider response の本文・message・headers は保持せず、失敗分類に必要な
            # allowlist 済みフィールドだけを内部値へ写す。
            error_name: str | None = None
            error_status_code: int | None = None
            raw_error = event.get('error')
            if isinstance(raw_error, dict):
                raw_error_name = raw_error.get('name')
                if isinstance(raw_error_name, str) and len(raw_error_name) <= 128:
                    error_name = raw_error_name
                raw_error_data = raw_error.get('data')
                if isinstance(raw_error_data, dict):
                    raw_status_code = raw_error_data.get('statusCode')
                    if (
                        isinstance(raw_status_code, int)
                        and isinstance(raw_status_code, bool) is False
                        and 400 <= raw_status_code <= 599
                    ):
                        error_status_code = raw_status_code
            if error_event is None or (
                error_event.status_code is None and error_status_code is not None
            ):
                error_event = _OpenCodeErrorEvent(
                    name=error_name,
                    status_code=error_status_code,
                )
        raw_session_id = event.get('sessionID')
        if isinstance(raw_session_id, str) and raw_session_id.strip() != '':
            normalized_session_id = raw_session_id.strip()
            if session_id is not None and session_id != normalized_session_id:
                raise OpenCodeCLIError('OpenCode CLI emitted multiple session identifiers.')
            session_id = normalized_session_id
        part = event.get('part')
        if isinstance(part, dict):
            parts.append(part)
    return parts, session_id, error_event


async def _DeleteOpenCodeSessionUnlocked(session_id: str | None) -> bool:
    """process lock 内で生成済み CLI session を削除する。

    Args:
        session_id: NDJSON から得た session ID。

    Returns:
        session ID を取得でき、削除 command が成功した場合は True。
    """

    if session_id is None:
        return False
    result = await _RunOpenCodeProcessUnlocked(
        ['session', 'delete', session_id, '--pure'],
        timeout_sec=_OPENCODE_SESSION_CLEANUP_TIMEOUT_SEC,
    )
    return result.timed_out is False and result.return_code == 0


async def _TryDeleteOpenCodeSessionUnlocked(session_id: str | None) -> bool:
    """session cleanup の失敗を元の推論結果より優先せず安全に記録する。

    Args:
        session_id: NDJSON から得た session ID。

    Returns:
        session を削除できた場合は True。
    """

    try:
        return await _DeleteOpenCodeSessionUnlocked(session_id)
    except OpenCodeCLIError as error:
        logging.warning(f'[OpenCodeCLI] Failed to delete CLI session: {error}')
        return False


def _ExtractOpenCodeSessionIDBestEffort(stdout: bytes) -> str | None:
    """timeout/cancel 後の完全な NDJSON 行だけから session ID を回収する。

    Args:
        stdout: kill 前までに回収できた部分的な標準出力。

    Returns:
        一意に確定できた session ID。不明なら None。
    """

    session_id: str | None = None
    for raw_line in stdout.splitlines():
        try:
            event = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(event, dict) is False:
            continue
        raw_session_id = event.get('sessionID')
        if isinstance(raw_session_id, str) is False or raw_session_id.strip() == '':
            continue
        normalized = raw_session_id.strip()
        if session_id is not None and session_id != normalized:
            return None
        session_id = normalized
    return session_id


def _LoadOpenCodeAuthDocument() -> dict[str, Any]:
    """製品用 auth.json を不透明な provider entry の辞書として読む。

    Returns:
        provider ID をキーにした認証辞書。不在なら空辞書。

    Raises:
        OpenCodeCLIError: ファイルが不正な場合。
    """

    try:
        text = ReadOpenCodeRegularFileText(OPENCODE_AUTH_PATH)
        payload = {} if text is None else json.loads(text)
    except (OSError, json.JSONDecodeError) as error:
        raise OpenCodeCLIError('OpenCode auth store is invalid.') from error
    if isinstance(payload, dict) is False:
        raise OpenCodeCLIError('OpenCode auth store is invalid.')
    return payload


def _WriteOpenCodeAuthDocument(payload: dict[str, Any]) -> None:
    """認証辞書を製品用 auth.json へ原子的に書く。

    Args:
        payload: provider ごとの不透明な認証 entry。

    Returns:
        None

    Raises:
        OpenCodeCLIError: 書き込みに失敗した場合。
    """

    try:
        EnsureOpenCodeRuntimeDirectories()
        WriteOpenCodeFileAtomically(
            OPENCODE_AUTH_PATH,
            json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        )
    except OSError as error:
        raise OpenCodeCLIError('Failed to update the OpenCode auth store.') from error


def HasStoredOpenCodeAuth(provider_id: str, *, auth_type: str | None = None) -> bool:
    """auth.json に provider の認証 entry が存在するか値を露出せず確認する。

    Args:
        provider_id: OpenCode provider ID。
        auth_type: `oauth` など期待する entry type。None は種類を問わない。

    Returns:
        条件に合う entry が存在する場合は True。
    """

    normalized_provider = provider_id.strip()
    if normalized_provider == '':
        return False
    try:
        auth_entry = _LoadOpenCodeAuthDocument().get(normalized_provider)
    except OpenCodeCLIError:
        return False
    if isinstance(auth_entry, dict) is False:
        return False
    typed_auth_entry = cast(dict[str, Any], auth_entry)
    return auth_type is None or typed_auth_entry.get('type') == auth_type


# ----- 新規 OAuth device authorization (認可中のみ ephemeral serve) -----

# 新規 OAuth 開始を許可する provider と必須 method ラベルの一元 allowlist。
# openai は headless のみ (browser 方式は localhost:1455 への帰還が必要でコンテナでは成立しない)。
# gitlab / poe / digitalocean / snowflake-cortex などの非 device flow は出さない。
OPENCODE_OAUTH_DEVICE_FLOW_ALLOWED: dict[str, str] = {
    'openai': 'ChatGPT Pro/Plus (headless)',
    'xai': 'SuperGrok Subscription',
    'github-copilot': 'Login with GitHub Copilot',
}

# 認可の人間操作を待つ上限。完了・失敗・タイムアウトのいずれでも serve は必ず terminate する。
_OAUTH_AUTHORIZATION_TIMEOUT_SEC = 15 * 60
# serve 起動から listen 行が出るまでの上限。
_OAUTH_SERVE_READY_TIMEOUT_SEC = 15.0
# serve の起動ログからエフェメラルポートを拾う行。
_OAUTH_SERVE_LISTEN_PATTERN = re.compile(r'listening on (http://127\.0\.0\.1:(\d+))')

# 認可セッションの状態。client は status を poll して完了を検知する。
OpenCodeOAuthAuthorizationStatus = Literal['InProgress', 'Succeeded', 'Failed', 'Timeout']


@dataclass(frozen=True)
class OpenCodeOAuthAuthorizationInfo:
    """router へ返す OAuth 認可セッションの快照 (秘密値を含まない)。"""

    provider_id: str
    method_index: int
    url: str | None
    instructions: str | None
    status: OpenCodeOAuthAuthorizationStatus
    error: str | None


@dataclass
class _OpenCodeOAuthSession:
    """進行中の ephemeral serve による OAuth 認可の内部状態。"""

    # 認可対象の provider ID (allowlist 済み)。
    provider_id: str
    # ラベル解決した /provider/auth の method index。
    method_index: int
    # ユーザーへ見せる認可 URL と手順 (device code を含む。token ではない)。
    url: str | None
    instructions: str | None
    # 認可中だけ生きている ephemeral serve process。完了・失敗・タイムアウトで必ず terminate する。
    process: asyncio.subprocess.Process
    # ephemeral serve の loopback base URL。callback 呼び出しに使う
    # (authorize の pending state は serve のメモリ上にあるため、同一 process に打つ必要がある)。
    base_url: str
    # auth.json への API キー書き込み・切断と直列化するため、認可中保持する provider lease。
    lease: OpenCodeProviderLease
    # 現在の状態と失敗理由 (秘密を含まない)。
    status: OpenCodeOAuthAuthorizationStatus
    error: str | None
    # 成功時に router 層の capability proof 失効を呼ぶコールバック。
    on_success: Callable[[], None] | None
    # auth.json を poll する backgroud task。
    poll_task: asyncio.Task[None] | None


# provider ID ごとの認可セッション。同時に 1 provider 1 本だけ許可する。
_oauth_sessions: dict[str, _OpenCodeOAuthSession] = {}
_oauth_sessions_lock = asyncio.Lock()
# serve 起動〜セッション登録までの await 区間に 2 本目の開始が割り込むと、両方が
# 「既存なし」を通過して serve が 2 本立つ。開始処理中の provider を最初の lock 区間で
# 予約し、予約中の開始も既存 InProgress と同じ 409 で弾く。
_oauth_starting: set[str] = set()


class OpenCodeCLI:
    """`opencode run` と製品用 runtime ファイルを扱う非同期 facade。"""

    async def acquireProviderLease(
        self,
        provider_id: str,
        *,
        api_key: str | None = None,
    ) -> OpenCodeProviderLease:
        """provider 認証を必要なら更新し、利用中 lease を取得する。

        Args:
            provider_id: OpenCode provider ID。
            api_key: auth.json へ保存する API キー。非 API キー認証なら None。

        Returns:
            CLI 呼び出し終了まで保持する lease。

        Raises:
            OpenCodeCLIError: auth.json 更新失敗。
            ValueError: provider ID または API キーが空の場合。
        """

        normalized_provider = provider_id.strip()
        if normalized_provider == '':
            raise ValueError('provider_id is empty.')
        normalized_key: str | None = None
        key_fingerprint: str | None = None
        if api_key is not None:
            normalized_key = api_key.strip()
            if normalized_key == '':
                raise ValueError('api_key is empty.')
            key_fingerprint = hashlib.sha256(normalized_key.encode('utf-8')).hexdigest()

        state = _GetProviderState(normalized_provider)
        async with state.condition:
            while state.auth_mutation_pending:
                await state.condition.wait()
            if key_fingerprint is not None and state.api_key_fingerprint != key_fingerprint:
                state.auth_mutation_pending = True
                try:
                    while state.active_lease_count > 0:
                        await state.condition.wait()
                    assert normalized_key is not None
                    await self._putApiKeyUnlocked(normalized_provider, normalized_key)
                    state.api_key_fingerprint = key_fingerprint
                finally:
                    state.auth_mutation_pending = False
                    state.condition.notify_all()
            state.active_lease_count += 1
            return OpenCodeProviderLease(state)

    async def putApiKey(self, provider_id: str, api_key: str) -> None:
        """API キーを auth.json へ保存する。

        Args:
            provider_id: OpenCode provider ID。
            api_key: API キー本体。

        Returns:
            None
        """

        lease = await self.acquireProviderLease(provider_id, api_key=api_key)
        await lease.release()

    async def _putApiKeyUnlocked(self, provider_id: str, api_key: str) -> None:
        """provider mutation 中に API キー entry を原子的に置換する。

        Args:
            provider_id: 正規化済み provider ID。
            api_key: 正規化済み API キー。

        Returns:
            None
        """

        # 別 process の serve が行う OAuth entry 保存との lost update を防ぐため、
        # read-modify-write 全体を全 provider 共通の mutation gate の内側で行う。
        async with _auth_mutation_gate:
            async with _auth_file_lock:
                payload = _LoadOpenCodeAuthDocument()
                payload[provider_id] = OpenCodeApiAuth(type='api', key=api_key)
                _WriteOpenCodeAuthDocument(payload)

    async def deleteAuth(self, provider_id: str) -> None:
        """provider の auth.json entry を削除する。

        Args:
            provider_id: OpenCode provider ID。

        Returns:
            None
        """

        normalized_provider = provider_id.strip()
        if normalized_provider == '':
            raise ValueError('provider_id is empty.')
        state = _GetProviderState(normalized_provider)
        async with state.condition:
            while state.auth_mutation_pending:
                await state.condition.wait()
            state.auth_mutation_pending = True
            try:
                while state.active_lease_count > 0:
                    await state.condition.wait()
                # putApiKey と同じく、OAuth の entry 保存との lost update を防ぐため
                # read-modify-write 全体を mutation gate の内側で行う。
                async with _auth_mutation_gate:
                    async with _auth_file_lock:
                        payload = _LoadOpenCodeAuthDocument()
                        if normalized_provider in payload:
                            payload.pop(normalized_provider)
                            _WriteOpenCodeAuthDocument(payload)
                state.api_key_fingerprint = None
            finally:
                state.auth_mutation_pending = False
                state.condition.notify_all()

    async def runPrompt(
        self,
        *,
        text: str,
        provider_id: str,
        model_id: str,
        variant: str | None,
        agent: str,
        timeout_sec: float,
        tools: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        """`opencode run --format json` を実行して message 互換 parts を返す。

        Args:
            text: モデルへ渡す本文。
            provider_id: OpenCode provider ID。
            model_id: OpenCode model ID。
            variant: 推論深さなどの variant。未指定なら None。
            agent: 製品 agent 名。
            timeout_sec: CLI 全体のタイムアウト秒。
            tools: 呼び出し側が期待する Web tool 権限。agent 設定との不一致を拒否する。

        Returns:
            parts と session cleanup 結果を持つ辞書。

        Raises:
            OpenCodeCLIError: CLI の起動・出力・終了が不正な場合。
            asyncio.CancelledError: 呼び出し task がキャンセルされた場合。
        """

        normalized_text = text.strip()
        normalized_provider = provider_id.strip()
        normalized_model = model_id.strip()
        normalized_agent = agent.strip()
        if normalized_text == '':
            raise ValueError('prompt text is empty.')
        if normalized_provider == '' or normalized_model == '':
            raise ValueError('provider_id and model_id must not be empty.')
        if normalized_agent == '':
            raise ValueError('agent is empty.')
        if (
            normalized_agent == OPENCODE_AGENT_EPISODE
            and tools != BuildOpenCodeEpisodeToolPermissions()
        ):
            raise ValueError('Episode agent tool permissions do not match the product policy.')

        arguments = [
            'run',
            '--pure',
            '--format', 'json',
            '--agent', normalized_agent,
            '--model', f'{normalized_provider}/{normalized_model}',
            '--port', '0',
        ]
        if variant is not None and variant.strip() != '':
            arguments.extend(['--variant', variant.strip()])
        arguments.append(normalized_text)

        async with _process_lock:
            try:
                process_result = await _RunOpenCodeProcessUnlocked(
                    arguments,
                    timeout_sec=timeout_sec,
                )
            except _OpenCodeProcessOutputLimitExceeded as error:
                session_id = _ExtractOpenCodeSessionIDBestEffort(error.stdout)
                session_cleaned_up = await _TryDeleteOpenCodeSessionUnlocked(session_id)
                raise OpenCodeCLIError(
                    str(error),
                    session_cleaned_up=session_cleaned_up,
                ) from error
            except _OpenCodeProcessCancelled as error:
                session_id = _ExtractOpenCodeSessionIDBestEffort(error.stdout)
                await _TryDeleteOpenCodeSessionUnlocked(session_id)
                raise asyncio.CancelledError() from None

            if process_result.timed_out:
                session_id = _ExtractOpenCodeSessionIDBestEffort(process_result.stdout)
                session_cleaned_up = await _TryDeleteOpenCodeSessionUnlocked(session_id)
                raise OpenCodeCLIError(
                    'OpenCode CLI invocation timed out.',
                    status_code=408,
                    session_cleaned_up=session_cleaned_up,
                )
            try:
                parts, session_id, error_observed = _ParseOpenCodeEvents(process_result.stdout)
            except OpenCodeCLIError as error:
                session_id = _ExtractOpenCodeSessionIDBestEffort(process_result.stdout)
                session_cleaned_up = await _TryDeleteOpenCodeSessionUnlocked(session_id)
                raise OpenCodeCLIError(
                    str(error),
                    session_cleaned_up=session_cleaned_up,
                ) from error
            session_cleaned_up = await _TryDeleteOpenCodeSessionUnlocked(session_id)
            if process_result.return_code != 0 or error_observed:
                raise OpenCodeCLIError(
                    'OpenCode CLI invocation failed.',
                    status_code=(
                        error_observed.status_code
                        if error_observed is not None and error_observed.status_code is not None
                        else _StatusCodeFromCLIError(process_result.stderr)
                    ),
                    session_cleaned_up=session_cleaned_up,
                )
            if len(parts) == 0:
                raise OpenCodeCLIError(
                    'OpenCode CLI emitted no result parts.',
                    session_cleaned_up=session_cleaned_up,
                )
            return {
                'parts': parts,
                'session_cleaned_up': session_cleaned_up,
            }

    async def listAllProviders(self) -> dict[str, Any]:
        """OpenCode の models cache から全 provider カタログを返す。

        Returns:
            `all`、`connected`、`default` を持つ provider payload。

        Raises:
            OpenCodeCLIError: CLI または models cache が不正な場合。
        """

        # models command は cache の初回取得と、現在利用可能な provider 判定を同時に担う。
        async with _process_lock:
            result = await _RunOpenCodeProcessUnlocked(
                ['models', '--pure', '--verbose'],
                timeout_sec=60.0,
            )
        if result.timed_out:
            raise OpenCodeCLIError('OpenCode model catalog timed out.', status_code=408)
        if result.return_code != 0:
            raise OpenCodeCLIError(
                'OpenCode model catalog command failed.',
                status_code=_StatusCodeFromCLIError(result.stderr),
            )

        connected_ids: set[str] = set()
        output_text = result.stdout.decode('utf-8', errors='replace')
        for line in output_text.splitlines():
            model_match = re.fullmatch(r'([^/\s]+)/[^\s]+', line.strip())
            if model_match is not None:
                connected_ids.add(model_match.group(1))

        try:
            cache_text = ReadOpenCodeRegularFileText(OPENCODE_MODELS_CACHE_PATH)
            cache_payload: object = json.loads(cache_text) if cache_text is not None else None
        except (OSError, json.JSONDecodeError) as error:
            raise OpenCodeCLIError('OpenCode model cache is invalid.') from error
        if isinstance(cache_payload, dict) is False:
            raise OpenCodeCLIError('OpenCode model cache is unavailable.')
        cache_dict = cast(dict[str, Any], cache_payload)

        providers = [
            provider
            for provider in cache_dict.values()
            if isinstance(provider, dict)
        ]
        async with _auth_file_lock:
            auth_payload = _LoadOpenCodeAuthDocument()
        connected_ids.update(
            provider_id
            for provider_id in auth_payload
            if provider_id.strip() != ''
        )
        return {
            'all': providers,
            'connected': sorted(connected_ids),
            'default': {},
        }

    async def listProviderAuthMethods(self) -> dict[str, list[dict[str, Any]]]:
        """auth.json に保存済みの方式だけを値を露出せず返す。

        Returns:
            provider ID ごとの認証 method。未接続 provider は呼び出し側で API key を補う。
        """

        async with _auth_file_lock:
            payload = _LoadOpenCodeAuthDocument()
        methods: dict[str, list[dict[str, Any]]] = {}
        for provider_id, auth_entry in payload.items():
            if isinstance(auth_entry, dict) is False:
                continue
            auth_type = auth_entry.get('type')
            if auth_type == 'oauth':
                methods[provider_id] = [
                    {'type': 'oauth', 'label': 'Connected OAuth subscription'},
                ]
            elif auth_type == 'api':
                methods[provider_id] = [
                    {'type': 'api', 'label': 'Manually enter API Key'},
                ]
        return methods

    async def startOAuthDeviceAuthorization(
        self,
        provider_id: str,
        *,
        on_success: Callable[[], None] | None = None,
    ) -> OpenCodeOAuthAuthorizationInfo:
        """allowlist 上の provider の新規 OAuth device flow を ephemeral serve で開始する。

        Args:
            provider_id: OpenCode provider ID。allowlist 外は拒否する。
            on_success: 認可完了時に1度だけ呼ぶコールバック (capability proof 失効など)。

        Returns:
            認可 URL と手順を含むセッション情報。

        Raises:
            ValueError: allowlist 外の provider ID の場合。
            OpenCodeUnavailableError: CLI binary または runtime を準備できない場合。
            OpenCodeCLIError: 同 provider の認可が進行中、serve 起動・authorize に失敗した場合。
        """

        normalized_provider = provider_id.strip()
        if normalized_provider not in OPENCODE_OAUTH_DEVICE_FLOW_ALLOWED:
            raise ValueError(f'OAuth start is not allowed for provider: {normalized_provider}')

        # 同じ provider の認可は同時に1本だけ。既存 InProgress の確認と開始予約の追加を
        # 同じ lock 区間で行い、serve 起動中の割り込み開始も 409 で弾く。
        # 終了済みセッションは poll task の finally で serve 停止済みなので、ここでは参照の破棄だけを行う。
        async with _oauth_sessions_lock:
            existing = _oauth_sessions.get(normalized_provider)
            if (
                normalized_provider in _oauth_starting or
                (existing is not None and existing.status == 'InProgress')
            ):
                raise OpenCodeCLIError(
                    f'OAuth authorization is already in progress for provider: {normalized_provider}',
                    status_code=409,
                )
            _oauth_sessions.pop(normalized_provider, None)
            _oauth_starting.add(normalized_provider)

        # 予約は成功 (セッション登録)・失敗のいずれでも必ず解除する。
        try:
            executable = ResolveOpenCodeExecutable()
            if executable is None:
                raise OpenCodeUnavailableError()
            EnsureOpenCodeRuntimeDirectories()

            # auth.json への API キー書き込み・切断と直列化するため、認可が終わるまで lease を保持する。
            lease = await self.acquireProviderLease(normalized_provider)
            # OAuth の token 保存は別 process の serve が auth.json 全体を read-modify-write
            # するため、serve が entry を保存し終えるまで (poll task の finally) 全 provider
            # 共通の mutation gate を保持する。これで別 provider の API キー更新・切断が
            # 旧 snapshot を後勝ちで書き戻す lost update を防ぐ。
            process: asyncio.subprocess.Process | None = None
            session: _OpenCodeOAuthSession | None = None
            gate_acquired = False
            process_lock_acquired = False
            cleanup_transferred_to_poll_task = False
            try:
                await _auth_mutation_gate.acquire()
                gate_acquired = True
                # 通常の推論・catalog process と同じ SQLite runtime を共有するため、
                # serve の spawn 前から停止・回収後まで全 process 排他も保持する。
                await _process_lock.acquire()
                process_lock_acquired = True

                # 認可専用の ephemeral serve。loopback のみ・ポート 0 (自動割当) で、
                # mDNS 広告も CORS 許可も付けない。無認証 listener の露出は寿命と
                # エフェメラルポートで抑える (完了・失敗・タイムアウトで必ず terminate)。
                try:
                    process = await asyncio.create_subprocess_exec(
                        str(executable),
                        'serve',
                        '--hostname', '127.0.0.1',
                        '--port', '0',
                        cwd=OPENCODE_WORKSPACE_DIR,
                        env=BuildOpenCodeProcessEnvironment(),
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        start_new_session=True,
                    )
                except OSError as error:
                    raise OpenCodeUnavailableError() from error

                base_url = await self._waitOAuthServeReady(process)
                method_index, url, instructions = await self._requestOAuthAuthorize(
                    base_url,
                    normalized_provider,
                )

                session = _OpenCodeOAuthSession(
                    provider_id=normalized_provider,
                    method_index=method_index,
                    url=url,
                    instructions=instructions,
                    process=process,
                    base_url=base_url,
                    lease=lease,
                    status='InProgress',
                    error=None,
                    on_success=on_success,
                    poll_task=None,
                )
                # session を登録してから poll task を作り、作成直後に cleanup 所有権を移す。
                # create_task() から flag 更新までは await がないため、poll task と開始側が
                # process・gate・lease を二重解放する隙間はない。
                async with _oauth_sessions_lock:
                    _oauth_sessions[normalized_provider] = session
                session.poll_task = asyncio.create_task(self._pollOAuthAuthorization(session))
                cleanup_transferred_to_poll_task = True
                logging.info(
                    f'[OpenCodeCLI] Started OAuth device authorization. [provider: {normalized_provider}, '
                    f'method_index: {method_index}]'
                )
                return self._buildOAuthAuthorizationInfo(session)
            finally:
                # poll task へ所有権を渡す前の BaseException は、subprocess 生成中の
                # CancelledError を含め、開始側だけで全 resource を回収する。
                if cleanup_transferred_to_poll_task is False:
                    try:
                        if session is not None:
                            async with _oauth_sessions_lock:
                                if _oauth_sessions.get(normalized_provider) is session:
                                    _oauth_sessions.pop(normalized_provider, None)
                    finally:
                        try:
                            if process is not None:
                                await _StopOpenCodeProcess(process)
                        finally:
                            try:
                                if process_lock_acquired:
                                    _process_lock.release()
                            finally:
                                try:
                                    if gate_acquired:
                                        _auth_mutation_gate.release()
                                finally:
                                    await lease.release()
        finally:
            _oauth_starting.discard(normalized_provider)

    def getOAuthAuthorizationStatus(self, provider_id: str) -> OpenCodeOAuthAuthorizationInfo | None:
        """provider の OAuth 認可セッションの快照を返す。

        Args:
            provider_id: OpenCode provider ID。

        Returns:
            セッション情報。開始されていなければ None。
        """

        session = _oauth_sessions.get(provider_id.strip())
        if session is None:
            return None
        return self._buildOAuthAuthorizationInfo(session)

    def _buildOAuthAuthorizationInfo(self, session: _OpenCodeOAuthSession) -> OpenCodeOAuthAuthorizationInfo:
        """内部セッション状態を router 向けの快照へ変換する。

        Args:
            session: 内部セッション状態。

        Returns:
            秘密値を含まないセッション情報。
        """

        return OpenCodeOAuthAuthorizationInfo(
            provider_id=session.provider_id,
            method_index=session.method_index,
            url=session.url,
            instructions=session.instructions,
            status=session.status,
            error=session.error,
        )

    async def _waitOAuthServeReady(self, process: asyncio.subprocess.Process) -> str:
        """ephemeral serve の listen 行から loopback base URL を拾う。

        Args:
            process: 起動直後の serve process。

        Returns:
            `http://127.0.0.1:<port>` 形式の base URL。

        Raises:
            OpenCodeCLIError: タイムアウト・早期終了・listen 行を読めなかった場合。
        """

        assert process.stdout is not None
        deadline = time.monotonic() + _OAUTH_SERVE_READY_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if process.returncode is not None:
                raise OpenCodeCLIError('OpenCode serve exited before listening.')
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=1.0)
            except TimeoutError:
                continue
            match = _OAUTH_SERVE_LISTEN_PATTERN.search(line.decode('utf-8', errors='replace'))
            if match is not None:
                return match.group(1)
        raise OpenCodeCLIError('OpenCode serve did not start listening in time.')

    async def _requestOAuthAuthorize(
        self,
        base_url: str,
        provider_id: str,
    ) -> tuple[int, str | None, str | None]:
        """/provider/auth でラベル解決した method で authorize を開始する。

        Args:
            base_url: ephemeral serve の loopback base URL。
            provider_id: allowlist 済みの OpenCode provider ID。

        Returns:
            (method index, 認可 URL, 手順テキスト)。

        Raises:
            OpenCodeCLIError: 方法一覧の取得・ラベル解決・authorize に失敗した場合。
        """

        expected_label = OPENCODE_OAUTH_DEVICE_FLOW_ALLOWED[provider_id]
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            try:
                auth_response = await client.get('/provider/auth')
                auth_response.raise_for_status()
                auth_methods = auth_response.json()
            except (httpx.HTTPError, json.JSONDecodeError) as error:
                raise OpenCodeCLIError('Failed to list OpenCode provider auth methods.') from error
            if isinstance(auth_methods, dict) is False:
                raise OpenCodeCLIError('OpenCode provider auth methods response is invalid.')
            provider_methods = auth_methods.get(provider_id)
            if isinstance(provider_methods, list) is False:
                raise OpenCodeCLIError(f'OpenCode provider has no auth methods: {provider_id}')
            method_index: int | None = None
            for index, raw_method in enumerate(provider_methods):
                if isinstance(raw_method, dict) is False:
                    continue
                if raw_method.get('type') != 'oauth':
                    continue
                # github-copilot の method には deployment 選択の prompts が付くが、既定の
                # GitHub.com を使うため method index 解決に prompts 回答は不要
                # (実測: serve API は method index だけで device flow を開始できる)。
                if raw_method.get('label') == expected_label:
                    method_index = index
                    break
            if method_index is None:
                raise OpenCodeCLIError(
                    f'OAuth method is not available: {provider_id} / {expected_label}',
                )
            try:
                authorize_response = await client.post(
                    f'/provider/{provider_id}/oauth/authorize',
                    json={'method': method_index},
                )
                authorize_response.raise_for_status()
                authorize = authorize_response.json()
            except (httpx.HTTPError, json.JSONDecodeError) as error:
                raise OpenCodeCLIError('Failed to start OpenCode OAuth authorization.') from error
        if isinstance(authorize, dict) is False:
            raise OpenCodeCLIError('OpenCode OAuth authorize response is invalid.')
        # allowlist は全て device flow (method='auto') のため、code 返送型はここで弾く。
        if authorize.get('method') != 'auto':
            raise OpenCodeCLIError('OpenCode OAuth method is not a device flow.')
        url = authorize.get('url')
        instructions = authorize.get('instructions')
        return (
            method_index,
            url if isinstance(url, str) else None,
            instructions if isinstance(instructions, str) else None,
        )

    async def _pollOAuthAuthorization(self, session: _OpenCodeOAuthSession) -> None:
        """serve の callback を呼んで認可完了を待ち、終了時に serve を必ず terminate する。

        Args:
            session: 監視する認可セッション。

        Returns:
            None
        """

        try:
            # OpenCode serve は method='auto' でも token exchange と auth.json への保存を
            # /provider/:id/oauth/callback の呼び出し内でしか行わない (authorize は pending
            # state を serve のメモリに置いて URL を返すだけ)。callback はユーザーの認可
            # 完了まで server 側でブロックするため、認可タイムアウト全体を上限として待つ。
            callback_succeeded = False
            timed_out = False
            try:
                async with httpx.AsyncClient(
                    base_url=session.base_url,
                    timeout=_OAUTH_AUTHORIZATION_TIMEOUT_SEC,
                ) as client:
                    callback_response = await client.post(
                        f'/provider/{session.provider_id}/oauth/callback',
                        json={'method': session.method_index},
                    )
                callback_succeeded = (
                    callback_response.status_code == 200 and
                    callback_response.json() is True
                )
            except httpx.TimeoutException:
                timed_out = True
            except (httpx.HTTPError, json.JSONDecodeError):
                # serve 早期終了・応答不正など。下の分岐で理由を付ける。
                pass
            if callback_succeeded:
                # callback 成功時は serve 側が auth.set で entry を書き済み。
                # 実 entry の存在を確認できたときだけ成功とみなす (再認可の置換も含む)。
                if HasStoredOpenCodeAuth(session.provider_id, auth_type='oauth'):
                    session.status = 'Succeeded'
                    logging.info(
                        f'[OpenCodeCLI] OAuth device authorization succeeded. [provider: {session.provider_id}]'
                    )
                    if session.on_success is not None:
                        session.on_success()
                    return
                session.status = 'Failed'
                session.error = 'OpenCode OAuth callback succeeded but no auth entry was stored.'
                logging.warning(
                    f'[OpenCodeCLI] OAuth callback succeeded but auth entry is missing. [provider: {session.provider_id}]'
                )
                return
            if timed_out:
                session.status = 'Timeout'
                session.error = 'OAuth authorization timed out.'
                logging.warning(
                    f'[OpenCodeCLI] OAuth device authorization timed out. [provider: {session.provider_id}]'
                )
                return
            session.status = 'Failed'
            session.error = (
                'OpenCode serve exited before authorization completed.'
                if session.process.returncode is not None
                else 'OpenCode OAuth authorization was rejected.'
            )
            logging.warning(
                f'[OpenCodeCLI] OAuth device authorization failed. [provider: {session.provider_id}]'
            )
        except asyncio.CancelledError:
            session.status = 'Failed'
            session.error = 'OAuth authorization was cancelled.'
            raise
        finally:
            # 完了・失敗・タイムアウト・キャンセルのいずれでも serve と lease を確実に解放する
            try:
                await _StopOpenCodeProcess(session.process)
            finally:
                # callback 応答が timeout・切断・cancel でも、serve は応答前に OAuth entry を
                # 保存済みの場合がある。process 停止後かつ mutation gate 保持中の実 entry を
                # 正本にして、古い API キー fingerprint を失効するか決める。
                oauth_entry_is_stored = False
                try:
                    oauth_entry_is_stored = HasStoredOpenCodeAuth(
                        session.provider_id,
                        auth_type='oauth',
                    )
                finally:
                    # serve を停止・回収して実 entry を確認してから process lock と mutation
                    # gate を逆順で解放する。lease は最後に同じ provider condition 内で
                    # fingerprint 失効と active count 減算をまとめて行う。
                    try:
                        _process_lock.release()
                    finally:
                        try:
                            _auth_mutation_gate.release()
                        finally:
                            await session.lease.release(
                                invalidate_api_key_fingerprint=oauth_entry_is_stored,
                            )


def _NormalizeOpenCodeToolName(value: object) -> str:
    """OpenCode tool 名を比較用の英数字へ正規化する。

    Args:
        value: 生の tool 名。

    Returns:
        小文字英数字だけの tool 名。
    """

    if not isinstance(value, str):
        return ''
    return ''.join(character for character in value.lower() if character.isalnum())


def ExtractOpenCodeJSONObjectFromText(text: object) -> dict[str, Any] | None:
    """テキスト応答の末尾にある単一 JSON object を抽出する。

    Args:
        text: モデルの text part。

    Returns:
        抽出できた JSON object。不正な場合は None。
    """

    if not isinstance(text, str) or text.strip() == '':
        return None
    stripped = text.strip()
    try:
        decoded, object_end = json.JSONDecoder().raw_decode(stripped)
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = None
        object_end = 0
    if isinstance(decoded, dict) and object_end == len(stripped):
        return decoded

    object_start = text.find('{')
    if object_start <= 0:
        return None
    prefix = text[:object_start]
    if len(prefix.encode('utf-8')) > 512:
        return None
    if any(marker in prefix for marker in ('```', 'json', 'JSON')):
        return None
    if any(
        unicodedata.category(character).startswith('C')
        and character not in {'\t', '\n', '\r'}
        for character in prefix
    ):
        return None
    try:
        decoded, object_end = json.JSONDecoder().raw_decode(text, idx=object_start)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(decoded, dict) is False:
        return None
    if any(character not in {' ', '\t', '\n', '\r'} for character in text[object_end:]):
        return None
    return decoded


def ExtractOpenCodeWebToolEvidence(message: dict[str, Any]) -> dict[str, Any]:
    """CLI parts から Web tool 完了証明と public citation を取り出す。

    Args:
        message: `runPrompt()` が返す parts 辞書。

    Returns:
        検索成否、citation、完了・失敗回数を持つ辞書。
    """

    from app.metadata.ai.episode_lookup import IsPublicHTTPURL

    completed_web_calls = 0
    failed_web_calls = 0
    citations: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    def AddCitation(url: object, title: object = '') -> None:
        if not isinstance(url, str):
            return
        normalized = url.strip()
        if normalized == '' or IsPublicHTTPURL(normalized) is False or normalized in seen_urls:
            return
        seen_urls.add(normalized)
        title_text = title.strip() if isinstance(title, str) else ''
        citations.append({'url': normalized, 'title': (title_text or normalized)[:300]})

    def WalkForURLs(value: object, *, depth: int = 0) -> None:
        if depth > 6 or value is None:
            return
        if isinstance(value, str):
            text = value.strip()
            for token in text.split():
                if token.startswith('http://') or token.startswith('https://'):
                    AddCitation(token.rstrip(')>,]"\''))
            if text.startswith('{') or text.startswith('['):
                try:
                    parsed = json.loads(text)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = None
                if parsed is not None:
                    WalkForURLs(parsed, depth=depth + 1)
            return
        if isinstance(value, dict):
            for key in ('url', 'href', 'link', 'uri'):
                if key in value:
                    AddCitation(value.get(key), value.get('title') or value.get('name') or '')
            for key, child in value.items():
                if key in {
                    'sources', 'citations', 'results', 'items', 'locations',
                    'input', 'output', 'metadata', 'data', 'content', 'result',
                }:
                    WalkForURLs(child, depth=depth + 1)
            return
        if isinstance(value, list):
            for item in value[:50]:
                WalkForURLs(item, depth=depth + 1)

    parts = message.get('parts')
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict) or part.get('type') != 'tool':
                continue
            normalized_tool = _NormalizeOpenCodeToolName(part.get('tool'))
            is_websearch = 'websearch' in normalized_tool or normalized_tool in {'search', 'googlesearch'}
            is_webfetch = (
                'webfetch' in normalized_tool
                or 'webbrowse' in normalized_tool
                or normalized_tool in {'fetch', 'browse', 'openpage'}
            )
            if is_websearch is False and is_webfetch is False:
                continue
            state = part.get('state')
            raw_status = state.get('status') if isinstance(state, dict) else None
            status = raw_status.strip().lower() if isinstance(raw_status, str) else ''
            if status in {'completed', 'complete', 'success', 'succeeded', 'done'}:
                completed_web_calls += 1
                WalkForURLs(state)
            elif status in {'error', 'failed', 'failure', 'rejected', 'cancelled', 'canceled'}:
                failed_web_calls += 1

    web_search_performed = completed_web_calls > 0
    return {
        'web_search_performed': web_search_performed,
        'web_search_failed': web_search_performed is False and failed_web_calls > 0,
        'citations': citations,
        'completed_web_calls': completed_web_calls,
        'failed_web_calls': failed_web_calls,
    }


def ExtractOpenCodeUsage(message: dict[str, Any]) -> OpenCodeNormalizedUsage:
    """CLI parts の最後の step-finish から usage を正規化する。

    Args:
        message: `runPrompt()` が返す parts 辞書。

    Returns:
        正規化済み usage。
    """

    tokens: OpenCodeTokenUsage | None = None
    cost: float | None = None
    parts = message.get('parts')
    if isinstance(parts, list):
        for part in reversed(parts):
            if not isinstance(part, dict) or part.get('type') != 'step-finish':
                continue
            raw_tokens = part.get('tokens')
            if isinstance(raw_tokens, dict):
                tokens = raw_tokens  # type: ignore[assignment]
            raw_cost = part.get('cost')
            if isinstance(raw_cost, (int, float)):
                cost = float(raw_cost)
            break
    return NormalizeOpenCodeUsage(tokens, cost)
