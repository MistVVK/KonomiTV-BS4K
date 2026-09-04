"""listener-free な OpenCode CLI 実行と製品用認証ストアを管理する。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import threading
import unicodedata
from dataclasses import dataclass
from types import TracebackType
from typing import Any, cast

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

    async def release(self) -> None:
        """provider の active lease 数を1つ減らす。

        Returns:
            None
        """

        if self._released:
            return
        async with self._state.condition:
            if self._released:
                return
            self._released = True
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


# OpenCode は SQLite runtime を共有するため、複数 CLI process を同時起動すると
# database is locked になり得る。全 service の process 境界だけを直列化する。
_process_lock = asyncio.Lock()
# provider ごとに lease を分けつつ、単一 auth.json の read-modify-write は全 provider で直列化する。
_auth_file_lock = asyncio.Lock()
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
