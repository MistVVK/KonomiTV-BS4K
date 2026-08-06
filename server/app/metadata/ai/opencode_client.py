"""製品用 opencode serve 向け OpenAPI クライアント。

auth / health に加え、Phase 2 の session create → prompt → abort → delete を提供する。
秘密（API キー本体）はログに出さない。
"""

from __future__ import annotations

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

    async def disposeInstance(self) -> None:
        """POST /instance/dispose でインスタンス状態を破棄し、auth 反映を促す。

        OpenCode 1.18.13 では PUT/DELETE /auth だけでは provider の connected
        状態が更新されず、Model not found になる。dispose 後の次回アクセスで
        auth.json が再読込されることを実測で確認した。

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

    async def putApiKey(self, provider_id: str, api_key: str) -> None:
        """PUT /auth/{providerID} で API キーを注入する。

        注入後に instance dispose し、provider connected を auth.json と整合させる。

        Args:
            provider_id: OpenCode provider ID（例: deepseek）。
            api_key: API キー本体（ログに出さない）。

        Returns:
            None

        Raises:
            OpenCodeClientError: 注入失敗。
            ValueError: provider_id / api_key が空。
        """

        normalized_provider = provider_id.strip()
        normalized_key = api_key.strip()
        if normalized_provider == '':
            raise ValueError('provider_id is empty.')
        if normalized_key == '':
            raise ValueError('api_key is empty.')
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
        await self.disposeInstance()

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
            await self.disposeInstance()

    async def startOAuthAuthorize(self, provider_id: str) -> dict[str, Any]:
        """POST /provider/{id}/oauth/authorize（仮実装・Phase 7b）。

        Args:
            provider_id: OpenCode provider ID。

        Returns:
            OpenCode が返す authorize 応答（URL 等）。秘密は呼び出し側で扱わない。

        Raises:
            OpenCodeClientError: 失敗。
        """

        normalized_provider = provider_id.strip()
        if normalized_provider == '':
            raise ValueError('provider_id is empty.')
        self._requireAvailable()
        path = f'/provider/{quote(normalized_provider, safe="")}/oauth/authorize'
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_sec,
            ) as client:
                response = await client.post(path, json={})
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as error:
            raise OpenCodeClientError(
                f'OpenCode OAuth authorize failed with HTTP {error.response.status_code}.',
                status_code=error.response.status_code,
            ) from error
        except httpx.HTTPError as error:
            raise OpenCodeClientError(f'OpenCode OAuth authorize request failed: {error}') from error
        if isinstance(payload, dict) is False:
            raise OpenCodeClientError('OpenCode OAuth authorize payload is invalid.')
        return payload

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
    ) -> dict[str, Any]:
        """POST /session/{id}/message で json_schema 付き prompt を送る。

        Args:
            session_id: 対象 session。
            text: ユーザー入力本文。
            provider_id: OpenCode provider ID。
            model_id: OpenCode model ID。
            agent: 製品 agent 名。
            schema: JSON Schema 本体。
            retry_count: OpenCode 側の format.retryCount。
            timeout_sec: この prompt 専用の HTTP タイムアウト。

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
        format_body: OpenCodeJsonSchemaFormat = {
            'type': 'json_schema',
            'schema': schema,
            'retryCount': max(0, int(retry_count)),
        }
        part: OpenCodeTextPartInput = {'type': 'text', 'text': text}
        body: OpenCodePromptRequest = {
            'parts': [part],
            'model': model_ref,
            'agent': agent,
            'format': format_body,
        }
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
        structured = state.get('input')
        if isinstance(structured, dict):
            return structured
    return None


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
