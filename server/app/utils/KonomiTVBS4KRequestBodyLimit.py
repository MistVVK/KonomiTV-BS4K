import re
from collections.abc import Sequence
from dataclasses import dataclass

from fastapi import status
from starlette.datastructures import Headers
from starlette.middleware.body_limit import (
    RequestBodyLimitMiddleware as StarletteRequestBodyLimitMiddleware,
)
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import logging


# 通常の multipart/form-data に含まれる boundary・part header・短いテキストフィールド用の余裕。
# ファイル本体の上限とは分け、ブラウザが生成する boundary 長やファイル名の差を吸収する。
MULTIPART_FORM_DATA_OVERHEAD_BYTES = 256 * 1024


@dataclass(frozen=True)
class KonomiTVBS4KRequestBodyLimit:
    """HTTP method と path に対応するリクエスト本文上限を表す。"""

    # 上限を適用する大文字の HTTP method。
    method: str
    # Starlette がデコードした scope path 全体に適用する正規表現。
    path_pattern: re.Pattern[str]
    # multipart などの framing を含む HTTP request body 全体の最大バイト数。
    max_body_bytes: int
    # Content-Length 先行拒否と stream 途中超過の両方で返す利用者向けエラー詳細。
    detail: str


class KonomiTVBS4KRequestBodyLimitMiddleware:
    """指定した method と path だけの本文を FastAPI の解析前に制限する。"""

    def __init__(
        self,
        app: ASGIApp,
        limits: Sequence[KonomiTVBS4KRequestBodyLimit],
    ) -> None:
        """
        ルート別上限を適用する ASGI middleware を初期化する。

        Args:
            app: 後段の ASGI アプリケーション。
            limits: method・path・最大バイト数を定義した上限設定。

        Returns:
            None
        """

        # 上限に一致しないリクエストをそのまま渡す後段 ASGI アプリケーション。
        self.app = app
        # 各設定と Starlette 標準の body limit を組にして保持する。
        ## 標準実装は chunked body の超過チャンクを後段へ渡さず、残りも drain せずに 413 を返す。
        self.limited_apps = tuple(
            (
                limit,
                StarletteRequestBodyLimitMiddleware(app, max_body_size=limit.max_body_bytes),
            )
            for limit in limits
        )

    @staticmethod
    async def _emptyReceive() -> Message:
        """
        Content-Length 先行拒否レスポンスへ空の request body を渡す。

        Returns:
            本文終端を表す ASGI message。
        """

        return {
            'type': 'http.request',
            'body': b'',
            'more_body': False,
        }

    @classmethod
    async def _sendTooLargeResponse(
        cls,
        scope: Scope,
        send: Send,
        detail: str,
    ) -> None:
        """
        本文を読まずに 413 JSON レスポンスを返す。

        Args:
            scope: 対象リクエストの ASGI scope。
            send: 上流へレスポンスを送る ASGI send callable。
            detail: JSON レスポンスへ含めるエラー詳細。

        Returns:
            None
        """

        response = JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content={'detail': detail},
        )
        await response(scope, cls._emptyReceive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        一致したルートだけを Content-Length と実受信バイト数の両方で制限する。

        Args:
            scope: 対象リクエストの ASGI scope。
            receive: リクエスト本文を受信する ASGI receive callable。
            send: レスポンスを送信する ASGI send callable。

        Returns:
            None
        """

        # HTTP 以外と設定に一致しないルートは receive / send を一切変更しない。
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        method = scope.get('method', '')
        path = scope.get('path', '')
        for limit, limited_app in self.limited_apps:
            if method != limit.method or limit.path_pattern.fullmatch(path) is None:
                continue

            # Content-Length が上限超過または不正なら、後段アプリも本文 stream も起動せず拒否する。
            ## Starlette 標準 middleware も receive 前に検査するが、ここで先行拒否することで
            ## body を読まない認証失敗・redirect 経路でも一貫して 413 にする。
            content_length_headers = Headers(scope=scope).getlist('content-length')
            if len(content_length_headers) > 0:
                content_length_header = content_length_headers[0].strip()
                try:
                    # Content-Length は RFC 9110 上 1 桁以上の数字だけを許す。
                    ## int() だけでは符号やアンダースコアも受理するため、変換前に文字種を限定する。
                    if len(content_length_headers) != 1 or content_length_header.isascii() is False or content_length_header.isdecimal() is False:
                        raise ValueError
                    content_length = int(content_length_header)
                except ValueError:
                    content_length = -1
                if content_length < 0 or content_length > limit.max_body_bytes:
                    logging.warning(
                        '[KonomiTVBS4KRequestBodyLimitMiddleware] Rejected request by Content-Length. '
                        f'[method: {method}, path: {path}, content_length: {content_length_headers}, '
                        f'max_body_bytes: {limit.max_body_bytes}]',
                    )
                    await self._sendTooLargeResponse(scope, send, limit.detail)
                    return

            # Content-Length 欠如・過少申告・chunked 転送は Starlette 標準の純 ASGI 実装で累積制限する。
            ## 標準実装の途中超過レスポンスは plain text 固定なので、Content-Length 先行拒否と同じ
            ## FastAPI 形式の JSON detail へ差し替え、超過の検出経路で API 応答を変えない。
            replacement_response = JSONResponse(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                content={'detail': limit.detail},
            )
            replace_starlette_response = False
            replacement_body_sent = False

            async def SendWithConsistentDetail(message: Message) -> None:
                nonlocal replace_starlette_response, replacement_body_sent

                # Starlette 1.6.0 の RequestBodyLimitMiddleware が生成する plain-text 413 だけを置換する。
                ## endpoint 自身が返す JSON 413 はそのまま通し、既存 API の detail を保持する。
                if message['type'] == 'http.response.start':
                    response_headers = Headers(raw=message.get('headers', []))
                    if (
                        message['status'] == status.HTTP_413_CONTENT_TOO_LARGE and
                        response_headers.get('content-type') == 'text/plain; charset=utf-8'
                    ):
                        replace_starlette_response = True
                        await send({
                            'type': 'http.response.start',
                            'status': status.HTTP_413_CONTENT_TOO_LARGE,
                            'headers': replacement_response.raw_headers,
                        })
                        return

                # 標準実装の本文を route 固有の JSON detail へ置換し、追加 body message は抑止する。
                if replace_starlette_response is True and message['type'] == 'http.response.body':
                    if replacement_body_sent is False:
                        replacement_body_sent = True
                        await send({
                            'type': 'http.response.body',
                            'body': replacement_response.body,
                            'more_body': False,
                        })
                    return

                await send(message)

            await limited_app(scope, receive, SendWithConsistentDetail)
            return

        await self.app(scope, receive, send)
