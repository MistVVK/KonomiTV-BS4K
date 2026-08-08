"""製品用 opencode serve 向け OpenAPI クライアント。

auth / health に加え、Phase 2 の session create → prompt → abort → delete を提供する。
秘密（API キー本体）はログに出さない。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import unicodedata
from types import TracebackType
from typing import Any
from urllib.parse import quote

import httpx

from app.constants import OPENCODE_HEALTH_TIMEOUT_SEC, OPENCODE_SERVE_BASE_URL
from app.metadata.ai.opencode_serve import IsOpenCodeAvailable
from app.metadata.ai.opencode_types import (
    NormalizeOpenCodeUsage,
    OpenCodeApiAuth,
    OpenCodeJsonSchemaFormat,
    OpenCodeModelRef,
    OpenCodeNormalizedUsage,
    OpenCodePromptRequest,
    OpenCodeTextPartInput,
    OpenCodeTokenUsage,
)


class OpenCodeClientError(Exception):
    """OpenCode HTTP クライアントの失敗。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        """エラーを初期化する。

        Args:
            message: ログ・API 向けの英語メッセージ（秘密を含めない）。
            status_code: 上流 HTTP ステータス。
        """

        super().__init__(message)
        self.status_code = status_code


class OpenCodeUnavailableError(OpenCodeClientError):
    """serve が available でないときの失敗。"""

    def __init__(self) -> None:
        super().__init__('OpenCode serve is unavailable.', status_code=503)


class _OpenCodeProviderState:
    """同一 OpenCode provider の認証変更と利用中 lease を直列化する。"""

    def __init__(self) -> None:
        """provider 状態を初期化する。"""

        # 認証変更・lease 取得・lease 解放を通知し合う Condition。
        self.condition = asyncio.Condition()
        # OpenCode へ最後に注入できた API キーの SHA-256。キー本体は保持しない。
        self.api_key_fingerprint: str | None = None
        # provider の認証状態を現在利用している AI セッション数。
        self.active_lease_count = 0
        # PUT / DELETE / OAuth callback と dispose を実行中または実行待ちなら True。
        self.auth_mutation_pending = False


class OpenCodeProviderLease:
    """provider 認証を AI セッションの終了まで保持する lease。"""

    def __init__(self, state: _OpenCodeProviderState) -> None:
        """取得済み lease を初期化する。

        Args:
            state: active_lease_count を加算済みの provider 状態。
        """

        # release 時に参照数を戻す provider 状態。
        self._state = state
        # finally と明示 release が重なっても参照数を二重に減らさないための状態。
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

        # 同じ lease の二重解放は何もしない。
        if self._released:
            return
        async with self._state.condition:
            if self._released:
                return
            self._released = True
            self._state.active_lease_count -= 1
            assert self._state.active_lease_count >= 0
            self._state.condition.notify_all()


# 同じ製品 serve と provider を使う全 OpenCodeClient instance で状態を共有する。
_provider_states: dict[tuple[str, str], _OpenCodeProviderState] = {}
_provider_states_lock = threading.Lock()


def _GetProviderState(base_url: str, provider_id: str) -> _OpenCodeProviderState:
    """serve URL と provider ID に対応する共有状態を返す。

    Args:
        base_url: OpenCode serve の base URL。
        provider_id: 正規化済み OpenCode provider ID。

    Returns:
        provider 共有状態。
    """

    state_key = (base_url, provider_id)
    # 複数 event loop thread から client が作られても状態生成を重複させない。
    with _provider_states_lock:
        state = _provider_states.get(state_key)
        if state is None:
            state = _OpenCodeProviderState()
            _provider_states[state_key] = state
        return state


class OpenCodeClient:
    """127.0.0.1:4097 の製品用 serve を叩く非同期クライアント。"""

    def __init__(
        self,
        *,
        base_url: str = OPENCODE_SERVE_BASE_URL,
        timeout_sec: float = 30.0,
    ) -> None:
        """クライアントを初期化する。

        Args:
            base_url: serve の base URL。
            timeout_sec: 既定 HTTP タイムアウト（prompt 以外）。
        """

        # serve の base URL。テストで差し替え可能。
        self._base_url = base_url.rstrip('/')
        # 既定タイムアウト秒。
        self._timeout_sec = timeout_sec

    @property
    def base_url(self) -> str:
        """接続先 base URL。"""

        return self._base_url

    def _requireAvailable(self) -> None:
        """製品 serve が available でなければ例外を送出する。"""

        if IsOpenCodeAvailable() is False and self._base_url == OPENCODE_SERVE_BASE_URL.rstrip('/'):
            raise OpenCodeUnavailableError()

    async def getHealth(self) -> dict[str, Any]:
        """GET /global/health。

        Returns:
            healthy/version を含む dict。

        Raises:
            OpenCodeClientError: 通信または応答不正。
        """

        self._requireAvailable()
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=OPENCODE_HEALTH_TIMEOUT_SEC,
            ) as client:
                response = await client.get('/global/health')
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as error:
            raise OpenCodeClientError(f'OpenCode health request failed: {error}') from error
        if isinstance(payload, dict) is False or payload.get('healthy') is not True:
            raise OpenCodeClientError('OpenCode health payload is invalid.')
        return payload

    async def _disposeInstance(self) -> None:
        """POST /instance/dispose でインスタンス状態を破棄し、auth 反映を促す。

        OpenCode 1.18.13 では PUT/DELETE /auth だけでは provider の connected
        状態が更新されず、Model not found になる。dispose 後の次回アクセスで
        auth.json が再読込されることを実測で確認した。
        呼び出し元は provider 状態の auth mutation を取得済みでなければならない。

        Raises:
            OpenCodeClientError: dispose 失敗。
        """

        self._requireAvailable()
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_sec,
            ) as client:
                response = await client.post('/instance/dispose', json={})
                response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise OpenCodeClientError(
                f'OpenCode instance dispose failed with HTTP {error.response.status_code}.',
                status_code=error.response.status_code,
            ) from error
        except httpx.HTTPError as error:
            raise OpenCodeClientError(
                f'OpenCode instance dispose request failed: {error}',
            ) from error

    async def acquireProviderLease(
        self,
        provider_id: str,
        *,
        api_key: str | None = None,
    ) -> OpenCodeProviderLease:
        """provider 認証を必要なら更新し、利用中 lease を取得する。

        同一キーの lease は並行利用を許可する。一方、異なるキーへの PUT と
        DELETE / OAuth callback は既存 lease がすべて解放されるまで待機する。

        Args:
            provider_id: OpenCode provider ID。
            api_key: 注入してから保持する API キー。非 API キー認証なら None。

        Returns:
            呼び出し側が AI セッション終了まで保持する lease。

        Raises:
            OpenCodeClientError: API キー注入または dispose 失敗。
            ValueError: provider_id または指定した api_key が空。
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

        state = _GetProviderState(self._base_url, normalized_provider)
        async with state.condition:
            # 先に待っている認証変更がある間は、新規 lease を増やさず既存 lease を drain させる。
            while state.auth_mutation_pending:
                await state.condition.wait()

            # API キーが異なる場合だけ、新規 lease を止めて active session の終了を待つ。
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

            # fingerprint 確認・必要な PUT/dispose と lease 加算を同じ Condition 内で完結させる。
            state.active_lease_count += 1
            return OpenCodeProviderLease(state)

    async def putApiKey(self, provider_id: str, api_key: str) -> None:
        """PUT /auth/{providerID} で API キーを注入する。

        注入後に instance dispose し、provider connected を auth.json と整合させる。
        直前と同じキー指紋なら PUT/dispose をスキップする（並行実行の破壊を抑える）。

        Args:
            provider_id: OpenCode provider ID（例: deepseek）。
            api_key: API キー本体（ログに出さない）。

        Returns:
            None

        Raises:
            OpenCodeClientError: 注入失敗。
            ValueError: provider_id / api_key が空。
        """

        # 単発 PUT も lease 取得経路へ通し、並行 session との認証交差を防ぐ。
        lease = await self.acquireProviderLease(provider_id, api_key=api_key)
        await lease.release()

    async def _putApiKeyUnlocked(self, normalized_provider: str, normalized_key: str) -> None:
        """provider mutation 取得中に API キーを PUT して instance を破棄する。

        Args:
            normalized_provider: 正規化済み provider ID。
            normalized_key: 正規化済み API キー。

        Returns:
            None

        Raises:
            OpenCodeClientError: 注入または dispose 失敗。
        """

        self._requireAvailable()
        body: OpenCodeApiAuth = {'type': 'api', 'key': normalized_key}
        path = f'/auth/{quote(normalized_provider, safe="")}'
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_sec,
            ) as client:
                response = await client.put(path, json=body)
                response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise OpenCodeClientError(
                f'OpenCode auth set failed with HTTP {error.response.status_code}.',
                status_code=error.response.status_code,
            ) from error
        except httpx.HTTPError as error:
            raise OpenCodeClientError(f'OpenCode auth set request failed: {error}') from error
        # auth.json は書けても in-memory provider が古いまま残るため dispose する。
        await self._disposeInstance()

    async def deleteAuth(self, provider_id: str) -> None:
        """DELETE /auth/{providerID} で資格情報を除去する。

        除去後に instance dispose し、幽霊認証を in-memory から落とす。

        Args:
            provider_id: OpenCode provider ID。

        Returns:
            None

        Raises:
            OpenCodeClientError: 削除失敗。
            ValueError: provider_id が空。
        """

        normalized_provider = provider_id.strip()
        if normalized_provider == '':
            raise ValueError('provider_id is empty.')
        state = _GetProviderState(self._base_url, normalized_provider)
        async with state.condition:
            # delete を待つ task がある時点から新規 lease を止め、既存 session を先に完了させる。
            while state.auth_mutation_pending:
                await state.condition.wait()
            state.auth_mutation_pending = True
            try:
                while state.active_lease_count > 0:
                    await state.condition.wait()
                await self._deleteAuthUnlocked(normalized_provider)
                state.api_key_fingerprint = None
            finally:
                state.auth_mutation_pending = False
                state.condition.notify_all()

    async def _deleteAuthUnlocked(self, normalized_provider: str) -> None:
        """provider mutation 取得中に資格情報を削除して instance を破棄する。

        Args:
            normalized_provider: 正規化済み provider ID。

        Returns:
            None

        Raises:
            OpenCodeClientError: 削除または dispose 失敗。
        """

        self._requireAvailable()
        path = f'/auth/{quote(normalized_provider, safe="")}'
        deleted = False
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_sec,
            ) as client:
                response = await client.delete(path)
                # 404 は既に無い状態として成功扱い（再試行可能掃除）。
                if response.status_code == 404:
                    deleted = True
                else:
                    response.raise_for_status()
                    deleted = True
        except httpx.HTTPStatusError as error:
            raise OpenCodeClientError(
                f'OpenCode auth delete failed with HTTP {error.response.status_code}.',
                status_code=error.response.status_code,
            ) from error
        except httpx.HTTPError as error:
            raise OpenCodeClientError(f'OpenCode auth delete request failed: {error}') from error
        if deleted:
            # auth ファイル削除後も connected が残るため dispose で揃える。
            await self._disposeInstance()

    async def createSession(self) -> str:
        """POST /session で新規 session を作成する。

        Returns:
            session ID。

        Raises:
            OpenCodeClientError: 作成失敗。
        """

        self._requireAvailable()
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_sec,
            ) as client:
                response = await client.post('/session', json={})
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as error:
            raise OpenCodeClientError(
                f'OpenCode session create failed with HTTP {error.response.status_code}.',
                status_code=error.response.status_code,
            ) from error
        except httpx.HTTPError as error:
            raise OpenCodeClientError(f'OpenCode session create request failed: {error}') from error
        if isinstance(payload, dict) is False:
            raise OpenCodeClientError('OpenCode session create payload is invalid.')
        session_id = payload.get('id')
        if isinstance(session_id, str) is False or session_id.strip() == '':
            raise OpenCodeClientError('OpenCode session create response missing id.')
        return session_id.strip()

    async def deleteSession(self, session_id: str) -> None:
        """DELETE /session/{id}。

        Args:
            session_id: 削除対象。

        Returns:
            None
        """

        normalized = session_id.strip()
        if normalized == '':
            raise ValueError('session_id is empty.')
        self._requireAvailable()
        path = f'/session/{quote(normalized, safe="")}'
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_sec,
            ) as client:
                response = await client.delete(path)
                if response.status_code == 404:
                    return
                response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise OpenCodeClientError(
                f'OpenCode session delete failed with HTTP {error.response.status_code}.',
                status_code=error.response.status_code,
            ) from error
        except httpx.HTTPError as error:
            raise OpenCodeClientError(f'OpenCode session delete request failed: {error}') from error

    async def listMessages(self, session_id: str) -> list[dict[str, Any]]:
        """GET /session/{id}/message で session の全メッセージを取得する。

        POST /session/{id}/message の応答は「最終メッセージ」しか含まれず、
        web ツール呼び出しのような途中の part が欠落する。話数検索のように
        tool telemetry から evidence を抽出する場合は、この一覧を併用する。

        Args:
            session_id: 対象 session。

        Returns:
            message のリスト（各要素は {info, parts}）。

        Raises:
            OpenCodeClientError: 通信または HTTP 失敗。
        """

        normalized_session = session_id.strip()
        if normalized_session == '':
            raise ValueError('session_id is empty.')
        self._requireAvailable()
        path = f'/session/{quote(normalized_session, safe="")}/message'
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_sec,
            ) as client:
                response = await client.get(path)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as error:
            raise OpenCodeClientError(
                f'OpenCode message list failed with HTTP {error.response.status_code}.',
                status_code=error.response.status_code,
            ) from error
        except httpx.HTTPError as error:
            raise OpenCodeClientError(f'OpenCode message list request failed: {error}') from error
        if isinstance(payload, list) is False:
            raise OpenCodeClientError('OpenCode message list payload is invalid.')
        return [message for message in payload if isinstance(message, dict)]

    async def listProviders(self) -> list[dict[str, Any]]:
        """GET /config/providers で接続済み provider 一覧を取得する。

        認証設定済み（auth 注入済み）provider と、そのモデル一覧を返す。
        未接続 provider は含まれない。

        Returns:
            provider のリスト。各要素は {id, models: {model_id: ModelInfo}, ...}。

        Raises:
            OpenCodeClientError: 通信または HTTP 失敗。
        """

        self._requireAvailable()
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_sec,
            ) as client:
                response = await client.get('/config/providers')
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as error:
            raise OpenCodeClientError(
                f'OpenCode provider list failed with HTTP {error.response.status_code}.',
                status_code=error.response.status_code,
            ) from error
        except httpx.HTTPError as error:
            raise OpenCodeClientError(f'OpenCode provider list request failed: {error}') from error
        if isinstance(payload, dict) is False:
            raise OpenCodeClientError('OpenCode provider list payload is invalid.')
        providers = payload.get('providers')
        if isinstance(providers, list) is False:
            raise OpenCodeClientError('OpenCode provider list payload missing providers.')
        return [provider for provider in providers if isinstance(provider, dict)]

    async def listAllProviders(self) -> dict[str, Any]:
        """GET /provider で全 provider カタログと connected を取得する。

        OpenCode Web の provider 選択 UI と同様に、未接続を含む全カタログを返す。
        models.dev 相当の一覧（約 180 件）と、現在 auth 済みの ID 配列を含む。

        Returns:
            {'all': [provider...], 'connected': [provider_id...], 'default': ...}。

        Raises:
            OpenCodeClientError: 通信または HTTP 失敗。
        """

        self._requireAvailable()
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_sec,
            ) as client:
                response = await client.get('/provider')
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as error:
            raise OpenCodeClientError(
                f'OpenCode provider catalog failed with HTTP {error.response.status_code}.',
                status_code=error.response.status_code,
            ) from error
        except httpx.HTTPError as error:
            raise OpenCodeClientError(
                f'OpenCode provider catalog request failed: {error}',
            ) from error
        if isinstance(payload, dict) is False:
            raise OpenCodeClientError('OpenCode provider catalog payload is invalid.')
        return payload

    async def listProviderAuthMethods(self) -> dict[str, list[dict[str, Any]]]:
        """GET /provider/auth で provider ごとの認証 method 一覧を取得する。

        OpenCode Web と同じく、例: openai → OAuth (browser) / OAuth (headless) / API Key。
        prompts 付きの面倒な method も生のまま返す（呼び出し側でフィルタする）。

        Returns:
            provider_id → method 配列。各 method は {type, label, prompts?}。

        Raises:
            OpenCodeClientError: 通信または HTTP 失敗。
        """

        self._requireAvailable()
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_sec,
            ) as client:
                response = await client.get('/provider/auth')
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as error:
            raise OpenCodeClientError(
                f'OpenCode provider auth methods failed with HTTP {error.response.status_code}.',
                status_code=error.response.status_code,
            ) from error
        except httpx.HTTPError as error:
            raise OpenCodeClientError(
                f'OpenCode provider auth methods request failed: {error}',
            ) from error
        if isinstance(payload, dict) is False:
            raise OpenCodeClientError('OpenCode provider auth methods payload is invalid.')
        result: dict[str, list[dict[str, Any]]] = {}
        for provider_id, methods in payload.items():
            if isinstance(provider_id, str) is False or provider_id.strip() == '':
                continue
            if isinstance(methods, list) is False:
                continue
            cleaned: list[dict[str, Any]] = [
                method for method in methods if isinstance(method, dict)
            ]
            if cleaned:
                result[provider_id.strip()] = cleaned
        return result

    async def startOAuthAuthorize(
        self,
        provider_id: str,
        *,
        method: int = 0,
        inputs: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST /provider/{id}/oauth/authorize で OAuth を開始する。

        Args:
            provider_id: OpenCode provider ID。
            method: GET /provider/auth が返す method 配列の 0 始まり index。
            inputs: method が要求する追加入力（通常は使わない。prompts 付きは非対応）。

        Returns:
            {url, method: 'auto'|'code', instructions}。秘密は含めない。

        Raises:
            OpenCodeClientError: 失敗。
            ValueError: provider_id が空、method が負。
        """

        normalized_provider = provider_id.strip()
        if normalized_provider == '':
            raise ValueError('provider_id is empty.')
        if method < 0:
            raise ValueError('method must be >= 0.')
        self._requireAvailable()
        path = f'/provider/{quote(normalized_provider, safe="")}/oauth/authorize'
        body: dict[str, Any] = {'method': int(method)}
        if inputs:
            # OpenCode が受け付ける追加 inputs（現状 UI では prompts 付き method を出さない）。
            body['inputs'] = inputs
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_sec,
            ) as client:
                response = await client.post(path, json=body)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as error:
            raise OpenCodeClientError(
                f'OpenCode OAuth authorize failed with HTTP {error.response.status_code}.',
                status_code=error.response.status_code,
            ) from error
        except httpx.HTTPError as error:
            raise OpenCodeClientError(
                f'OpenCode OAuth authorize request failed: {error}',
            ) from error
        if isinstance(payload, dict) is False:
            raise OpenCodeClientError('OpenCode OAuth authorize payload is invalid.')
        return payload

    async def completeOAuthCallback(
        self,
        provider_id: str,
        *,
        method: int = 0,
        code: str | None = None,
    ) -> bool:
        """POST /provider/{id}/oauth/callback で OAuth を完了する。

        browser (auto) では code 無し、headless/device では code が必要。
        成功後に instance dispose し、connected を auth.json と整合させる。

        Args:
            provider_id: OpenCode provider ID。
            method: authorize 時と同じ method index。
            code: 認可コード / device code（不要なら None）。

        Returns:
            OpenCode が true を返したら True。

        Raises:
            OpenCodeClientError: 失敗。
            ValueError: provider_id が空、method が負。
        """

        normalized_provider = provider_id.strip()
        if normalized_provider == '':
            raise ValueError('provider_id is empty.')
        if method < 0:
            raise ValueError('method must be >= 0.')
        state = _GetProviderState(self._base_url, normalized_provider)
        async with state.condition:
            # OAuth callback も auth.json と instance を変更するため、同じ provider の session を drain する。
            while state.auth_mutation_pending:
                await state.condition.wait()
            state.auth_mutation_pending = True
            try:
                while state.active_lease_count > 0:
                    await state.condition.wait()
                result = await self._completeOAuthCallbackUnlocked(
                    normalized_provider,
                    method=method,
                    code=code,
                )
                # OAuth 資格情報へ切り替わったため、API キー fingerprint は無効化する。
                state.api_key_fingerprint = None
                return result
            finally:
                state.auth_mutation_pending = False
                state.condition.notify_all()

    async def _completeOAuthCallbackUnlocked(
        self,
        normalized_provider: str,
        *,
        method: int,
        code: str | None,
    ) -> bool:
        """provider mutation 取得中に OAuth callback と instance dispose を行う。

        Args:
            normalized_provider: 正規化済み provider ID。
            method: authorize 時と同じ method index。
            code: 認可コード / device code（不要なら None）。

        Returns:
            OpenCode が true を返したら True。

        Raises:
            OpenCodeClientError: callback または dispose 失敗。
        """

        self._requireAvailable()
        path = f'/provider/{quote(normalized_provider, safe="")}/oauth/callback'
        body: dict[str, Any] = {'method': int(method)}
        if code is not None and code.strip() != '':
            body['code'] = code.strip()
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=max(self._timeout_sec, 60.0),
            ) as client:
                response = await client.post(path, json=body)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as error:
            raise OpenCodeClientError(
                f'OpenCode OAuth callback failed with HTTP {error.response.status_code}.',
                status_code=error.response.status_code,
            ) from error
        except httpx.HTTPError as error:
            raise OpenCodeClientError(
                f'OpenCode OAuth callback request failed: {error}',
            ) from error
        # 成功後は in-memory connected を auth.json に揃える。
        await self._disposeInstance()
        return payload is True

    async def abortSession(self, session_id: str) -> None:
        """POST /session/{id}/abort。

        Args:
            session_id: 中断対象。

        Returns:
            None
        """

        normalized = session_id.strip()
        if normalized == '':
            raise ValueError('session_id is empty.')
        self._requireAvailable()
        path = f'/session/{quote(normalized, safe="")}/abort'
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_sec,
            ) as client:
                response = await client.post(path, json={})
                # 既に完了済みでも 2xx/4xx を握りつぶして cleanup を優先する。
                if response.status_code >= 500:
                    response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise OpenCodeClientError(
                f'OpenCode session abort failed with HTTP {error.response.status_code}.',
                status_code=error.response.status_code,
            ) from error
        except httpx.HTTPError as error:
            raise OpenCodeClientError(f'OpenCode session abort request failed: {error}') from error

    async def promptJsonSchema(
        self,
        session_id: str,
        *,
        text: str,
        provider_id: str,
        model_id: str,
        agent: str,
        schema: dict[str, Any],
        retry_count: int = 1,
        timeout_sec: float = 120.0,
        tools: dict[str, bool] | None = None,
        include_format: bool = True,
    ) -> dict[str, Any]:
        """POST /session/{id}/message で json_schema 付き prompt を送る。

        Args:
            session_id: 対象 session。
            text: ユーザー入力本文。
            provider_id: OpenCode provider ID。
            model_id: OpenCode model ID。
            agent: 製品 agent 名。
            schema: JSON Schema 本体（include_format=True 時に送信）。
            retry_count: OpenCode 側の format.retryCount。
            timeout_sec: この prompt 専用の HTTP タイムアウト。
            tools: ツールの有効/無効指定（例: {'websearch': True}）。websearch は
                デフォルトのツールセットに含まれないため、web 検索を使う agent で
                明示的に有効化する必要がある。
            include_format: json_schema format を送るか。web ツール併用時は
                StructuredOutput モードでモデルが web ツールを呼ばなくなるため
                False にして、テキストから JSON を抽出する方式にする。

        Returns:
            生の message 応答 dict（info / parts）。

        Raises:
            OpenCodeClientError: 通信または HTTP 失敗。
        """

        normalized_session = session_id.strip()
        if normalized_session == '':
            raise ValueError('session_id is empty.')
        if text.strip() == '':
            raise ValueError('prompt text is empty.')
        self._requireAvailable()
        model_ref: OpenCodeModelRef = {
            'providerID': provider_id.strip(),
            'modelID': model_id.strip(),
        }
        part: OpenCodeTextPartInput = {'type': 'text', 'text': text}
        body: OpenCodePromptRequest = {
            'parts': [part],
            'model': model_ref,
            'agent': agent,
        }
        if include_format is False:
            # format を送らない場合は schema / retry_count は使用しない。
            pass
        else:
            format_body: OpenCodeJsonSchemaFormat = {
                'type': 'json_schema',
                'schema': schema,
                'retryCount': max(0, int(retry_count)),
            }
            body['format'] = format_body
        if tools is not None:
            body['tools'] = tools
        path = f'/session/{quote(normalized_session, safe="")}/message'
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=timeout_sec,
            ) as client:
                response = await client.post(path, json=body)
                response.raise_for_status()
                payload = response.json()
        except httpx.TimeoutException as error:
            raise OpenCodeClientError(
                'OpenCode prompt timed out.',
                status_code=408,
            ) from error
        except httpx.HTTPStatusError as error:
            raise OpenCodeClientError(
                f'OpenCode prompt failed with HTTP {error.response.status_code}.',
                status_code=error.response.status_code,
            ) from error
        except httpx.HTTPError as error:
            raise OpenCodeClientError(f'OpenCode prompt request failed: {error}') from error
        if isinstance(payload, dict) is False:
            raise OpenCodeClientError('OpenCode prompt payload is invalid.')
        return payload


def _NormalizeOpenCodeToolName(value: object) -> str:
    """OpenCode tool 名を比較用の英数字へ正規化する。"""

    if not isinstance(value, str):
        return ''
    return ''.join(character for character in value.lower() if character.isalnum())


def _ParseMaybeJSONObject(value: object) -> dict[str, Any] | None:
    """dict または JSON オブジェクト文字列を dict へ正規化する。"""

    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text == '' or text[0] != '{':
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def ExtractOpenCodeStructuredOutput(message: dict[str, Any]) -> dict[str, Any] | None:
    """message 応答から StructuredOutput tool の input（JSON 本体）を取り出す。

    Args:
        message: POST /session/{id}/message の応答。

    Returns:
        structured な dict。見つからなければ None。
    """

    parts = message.get('parts')
    if not isinstance(parts, list):
        return None
    typed_parts: list[Any] = parts
    for part in typed_parts:
        if isinstance(part, dict) is False:
            continue
        if part.get('type') != 'tool':
            continue
        tool_name = part.get('tool')
        if tool_name not in {'StructuredOutput', 'structured_output'}:
            continue
        state = part.get('state')
        if isinstance(state, dict) is False:
            continue
        structured = _ParseMaybeJSONObject(state.get('input'))
        if structured is not None:
            return structured
        # 一部実装は output に JSON を載せる。
        structured = _ParseMaybeJSONObject(state.get('output'))
        if structured is not None:
            return structured
    return None


def ExtractOpenCodeJSONObjectFromText(text: object) -> dict[str, Any] | None:
    """テキスト応答の末尾にある単一 JSON object を抽出する。

    json_schema format を送らない agent（web ツール併用）では、モデルが JSON
    の前後に短い平文を残すことがある。検索済みの安全な prefix を無視して
    末尾の JSON object を取り出し、それ以降に説明文や Markdown が残る場合は
    受理しない（ACP の EpisodeLookup 抽出契約に合わせる）。

    Args:
        text: モデルの text part。

    Returns:
        抽出できた JSON object。不正な場合は None。
    """

    if not isinstance(text, str):
        return None
    if text.strip() == '':
        return None
    stripped = text.strip()
    try:
        decoded, object_end = json.JSONDecoder().raw_decode(stripped)
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = None
        object_end = 0
    if (
        isinstance(decoded, dict) and
        object_end == len(stripped)
    ):
        return decoded
    # 先頭の平文 prefix（最大 512 bytes）をスキップして末尾の JSON object を探す。
    object_start = text.find('{')
    if object_start <= 0:
        return None
    prefix = text[:object_start]
    if len(prefix.encode('utf-8')) > 512:
        return None
    # Markdown 記法や不可視制御文字を含む prefix は受理しない。
    for marker in ('```', 'json', 'JSON'):
        if marker in prefix:
            return None
    if any(
        unicodedata.category(character).startswith('C') and
        character not in {'\t', '\n', '\r'}
        for character in prefix
    ):
        return None
    try:
        decoded, object_end = json.JSONDecoder().raw_decode(text, idx=object_start)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(decoded, dict) is False:
        return None
    # JSON 以降に説明文が残る場合は受理しない（厳格契約の維持）。
    if any(
        character not in {' ', '\t', '\n', '\r'}
        for character in text[object_end:]
    ):
        return None
    return decoded


def ExtractOpenCodeWebToolEvidence(message: dict[str, Any]) -> dict[str, Any]:
    """message parts から websearch/webfetch の完了証明と public citation を取り出す。

    モデル JSON 本文の URL は使わない。tool state（input/output/metadata）と
    構造化 sources/citations からのみ抽出する。

    Args:
        message: POST /session/{id}/message の応答。

    Returns:
        {
            'web_search_performed': bool,  # completed web tool が1件以上
            'web_search_failed': bool,     # failed web tool のみで completed が無い
            'citations': list[dict],       # {url, title}（呼び出し側で EpisodeLookupCitation 化）
            'completed_web_calls': int,
            'failed_web_calls': int,
        }
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
        if normalized == '' or IsPublicHTTPURL(normalized) is False:
            return
        if normalized in seen_urls:
            return
        seen_urls.add(normalized)
        title_text = title.strip() if isinstance(title, str) else ''
        if title_text == '':
            title_text = normalized
        citations.append({'url': normalized, 'title': title_text[:300]})

    def WalkForURLs(value: object, *, depth: int = 0) -> None:
        if depth > 6 or value is None:
            return
        if isinstance(value, str):
            text = value.strip()
            # 単純な URL 文字列、または JSON 内の URL を拾う。
            if text.startswith('http://') or text.startswith('https://'):
                # 空白や引用で終わる場合を軽く切る
                candidate = text.split()[0].rstrip(')>,]"\'')
                AddCitation(candidate)
            else:
                # Exa の検索結果は "Title: ...\nURL: https://..." のような
                # テキスト形式で返るため、行頭以外の URL トークンも拾う。
                for token in text.split():
                    if token.startswith('http://') or token.startswith('https://'):
                        candidate = token.rstrip(')>,]"\'')
                        AddCitation(candidate)
            if text.startswith('{') or text.startswith('['):
                parsed = None
                try:
                    parsed = json.loads(text)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = None
                if parsed is not None:
                    WalkForURLs(parsed, depth=depth + 1)
            return
        if isinstance(value, dict):
            typed = value
            # 構造化 container を優先（本文の偶然 URL より明確なキー）。
            for key in ('url', 'href', 'link', 'uri'):
                if key in typed:
                    AddCitation(typed.get(key), typed.get('title') or typed.get('name') or '')
            for key in ('sources', 'citations', 'results', 'items', 'locations'):
                if key in typed:
                    WalkForURLs(typed.get(key), depth=depth + 1)
            # webfetch の input.url など
            for key, child in typed.items():
                if key in {'url', 'href', 'link', 'uri', 'sources', 'citations', 'results', 'items', 'locations'}:
                    continue
                if key in {'input', 'output', 'metadata', 'data', 'content', 'result'}:
                    WalkForURLs(child, depth=depth + 1)
            return
        if isinstance(value, list):
            for item in value[:50]:
                WalkForURLs(item, depth=depth + 1)

    parts = message.get('parts')
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) is False:
                continue
            if part.get('type') != 'tool':
                continue
            normalized_tool = _NormalizeOpenCodeToolName(part.get('tool'))
            is_websearch = (
                'websearch' in normalized_tool
                or normalized_tool in {'search', 'googlesearch'}
            )
            is_webfetch = (
                'webfetch' in normalized_tool
                or 'webbrowse' in normalized_tool
                or normalized_tool in {'fetch', 'browse', 'openpage'}
            )
            if is_websearch is False and is_webfetch is False:
                continue
            state = part.get('state')
            status = ''
            if isinstance(state, dict):
                raw_status = state.get('status')
                status = raw_status.strip().lower() if isinstance(raw_status, str) else ''
            if status in {'completed', 'complete', 'success', 'succeeded', 'done'}:
                completed_web_calls += 1
                WalkForURLs(state)
                # title があれば先頭 citation の補助
                title = state.get('title') if isinstance(state, dict) else None
                if isinstance(title, str) and citations:
                    # 既に URL がある場合は触らない
                    pass
            elif status in {'error', 'failed', 'failure', 'rejected', 'cancelled', 'canceled'}:
                failed_web_calls += 1
            elif status in {'running', 'pending', 'in_progress', 'inprogress'}:
                # 未完了は performed に数えない
                pass
            else:
                # status 不明でも output がある場合は完了扱いしない（甘くしない）
                failed_web_calls += 0

    web_search_performed = completed_web_calls > 0
    web_search_failed = web_search_performed is False and failed_web_calls > 0
    return {
        'web_search_performed': web_search_performed,
        'web_search_failed': web_search_failed,
        'citations': citations,
        'completed_web_calls': completed_web_calls,
        'failed_web_calls': failed_web_calls,
    }


def ExtractOpenCodeUsage(message: dict[str, Any]) -> OpenCodeNormalizedUsage:
    """message 応答から usage を正規化する。

    優先順位: info.tokens/cost → 最後の step-finish。

    Args:
        message: prompt 応答。

    Returns:
        正規化済み usage。
    """

    tokens: OpenCodeTokenUsage | None = None
    cost: float | None = None
    info = message.get('info')
    if isinstance(info, dict):
        raw_tokens = info.get('tokens')
        if isinstance(raw_tokens, dict):
            tokens = raw_tokens  # type: ignore[assignment]
        raw_cost = info.get('cost')
        if isinstance(raw_cost, (int, float)):
            cost = float(raw_cost)
    if tokens is None:
        parts = message.get('parts')
        if isinstance(parts, list):
            for part in reversed(parts):
                if isinstance(part, dict) is False:
                    continue
                if part.get('type') != 'step-finish':
                    continue
                raw_tokens = part.get('tokens')
                if isinstance(raw_tokens, dict):
                    tokens = raw_tokens  # type: ignore[assignment]
                raw_cost = part.get('cost')
                if isinstance(raw_cost, (int, float)):
                    cost = float(raw_cost)
                break
    return NormalizeOpenCodeUsage(tokens, cost)
