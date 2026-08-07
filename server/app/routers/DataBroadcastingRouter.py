
import asyncio
import time
from typing import Annotated

import httpx
from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from fastapi.responses import StreamingResponse
from ping3 import ping

from app import logging, schemas
from app.constants import API_REQUEST_HEADERS
from app.utils.DataBroadcastingHTTPClient import (
    PROBE_TIMEOUT_MILLISECONDS_MAX,
    PROBE_TIMEOUT_MILLISECONDS_MIN,
    RequestWithSafeRedirects,
    ResolveProbeDestination,
    UpstreamBodyLimitExceeded,
    UpstreamURLRejected,
)


# ルーター
router = APIRouter(
    tags = ['Data Broadcasting'],
    prefix = '/api/data-broadcasting',
)

# 以下の API 実装は web-bml での実装を Python に移植したもの (with GPT-4)
# ref: https://github.com/tsukumijima/web-bml/blob/master/server/index.ts#L195-L296

# データ放送 POST の本文は Denbun 電文として最大 4096 バイトまでを byte 透過で転送する
_MAX_POST_BODY_BYTES = 4096


async def _IterResponseContent(content: bytes):
    """上流応答本文を非同期ストリームとして1回だけ返す。"""

    yield content


def _filterResponseHeaders(response_headers: httpx.Headers) -> dict[str, str]:
    """データ放送クライアントへ返す許可済み応答ヘッダだけを取り出す。"""

    allowed_response_headers = {
        'accept-ranges',
        'authentication-info',
        'last-modified',
        'pragma',
        'date',
        'cache-control',
        'age',
        'expire',
        'content-language',
        'content-location',
        'content-type',
    }
    return {
        key: value
        for key, value in response_headers.items()
        if key.lower() in allowed_response_headers
    }


async def _ReadLimitedPostBody(request: Request) -> bytes:
    """
    データ放送 POST 本文を Content-Length 先行拒否と stream 累積上限の両方で読み取る。

    Args:
        request: クライアントからの Starlette Request。

    Returns:
        上限以内の raw body。

    Raises:
        HTTPException: Content-Length または累積読取量が 4096 バイトを超えた場合。
    """

    # Content-Length が分かる場合は 1 バイトも読まずに拒否し、巨大 upload の spool を防ぐ。
    content_length_header = request.headers.get('content-length')
    if content_length_header is not None:
        try:
            content_length = int(content_length_header.strip())
        except ValueError:
            # 不正な Content-Length は巨大本文と同じく拒否する。
            content_length = _MAX_POST_BODY_BYTES + 1
        if content_length > _MAX_POST_BODY_BYTES:
            logging.warning(
                '[DataBroadcastingRouter][_ReadLimitedPostBody] Rejected POST by Content-Length. '
                f'[content_length: {content_length_header}]',
            )
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f'POST body must be at most {_MAX_POST_BODY_BYTES} bytes',
            )

    # Content-Length 欠如・偽装・chunked 転送に備え、stream で累積上限を検査する。
    # request.body() は全量を一度に保持するため使わない。
    body_chunks: list[bytes] = []
    total_bytes = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total_bytes += len(chunk)
        if total_bytes > _MAX_POST_BODY_BYTES:
            logging.warning(
                '[DataBroadcastingRouter][_ReadLimitedPostBody] Rejected POST by streamed body size. '
                f'[received_bytes: {total_bytes}]',
            )
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f'POST body must be at most {_MAX_POST_BODY_BYTES} bytes',
            )
        body_chunks.append(chunk)
    return b''.join(body_chunks)


@router.get(
    '/request/{request_url:path}',
    summary = 'データ放送ブラウザ HTTP (GET) リクエストプロキシ API',
    response_description = 'リクエスト URL に対する GET リクエストのレスポンス。',
)
async def BMLBrowserRequestGETProxyAPI(
    request_url: Annotated[str, Path(description='リクエスト URL 。')],
    request: Request,
):
    """
    データ放送ブラウザ (web-bml) のネット接続機能から利用される、HTTP (GET) プロキシ。<br>
    Web ブラウザからの HTTP リクエストには CORS の制限があるため、この API を経由してリクエストを送信する。<br>
    web-bml のネット接続機能専用の API で、web-bml 以外からは利用されない。<br>
    request_url は client 側で encodeURIComponent 済みの target URL を path に載せたものであり、
    サーバーは追加の unquote を行わず FastAPI が path として復元した URL をそのまま上流へ送る。
    """

    logging.debug('Data broadcast upstream GET request received.')

    headers = {
        'Accept': '*/*',
        'Accept-Language': 'ja',
        'Pragma': 'no-cache',
    }
    allowed_request_headers = ['if-modified-since', 'cache-control']
    for key, value in request.headers.items():
        if key.lower() in allowed_request_headers:
            headers[key] = value

    # scheme、接続先IP、DNS rebinding、redirect先を共通ポリシーで検証する。
    # 期限切れ証明書を使う放送局との互換性はHTTP client側で維持するが、
    # 検証済みIP以外へ接続できないことをこの時点で保証する。
    try:
        response = await RequestWithSafeRedirects(
            'GET',
            request_url,
            headers={**API_REQUEST_HEADERS, **headers},
        )
    except UpstreamURLRejected as ex:
        logging.warning('[DataBroadcastingRouter][BMLBrowserRequestGETProxyAPI] Upstream URL rejected by egress policy.')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Request URL is not allowed',
        ) from ex
    except UpstreamBodyLimitExceeded as ex:
        # 巨大な上流応答はメモリ枯渇を防ぐため接続を切ったうえで 502 にする。
        logging.warning('[DataBroadcastingRouter][BMLBrowserRequestGETProxyAPI] Upstream response exceeded body limit.')
        raise HTTPException(
            status_code = status.HTTP_502_BAD_GATEWAY,
            detail = 'Upstream response is too large',
        ) from ex
    except Exception as ex:
        # 上流の接続失敗理由やURLを外部へ返さず、詳細はサーバーログだけに残す。
        logging.error('[DataBroadcastingRouter][BMLBrowserRequestGETProxyAPI] Failed to request:', exc_info=ex)
        raise HTTPException(
            status_code = status.HTTP_502_BAD_GATEWAY,
            detail = 'Failed to request upstream resource',
        ) from ex

    return StreamingResponse(
        _IterResponseContent(response.content),
        status_code = response.status_code,
        headers = _filterResponseHeaders(response.headers),
    )


@router.post(
    '/request/{request_url:path}',
    summary = 'データ放送ブラウザ HTTP (POST) リクエストプロキシ API',
    response_description = 'リクエスト URL に対する POST リクエストのレスポンス。',
)
async def BMLBrowserRequestPOSTProxyAPI(
    request_url: Annotated[str, Path(description='リクエスト URL 。')],
    request: Request,
):
    """
    データ放送ブラウザ (web-bml) のネット接続機能から利用される、HTTP (POST) プロキシ。<br>
    Web ブラウザからの HTTP リクエストには CORS の制限があるため、この API を経由してリクエストを送信する。<br>
    web-bml のネット接続機能専用の API で、web-bml 以外からは利用されない。<br>
    本文は Form 解釈せず raw body を最大 4096 バイトまで byte-for-byte で上流へ転送する
    （Shift_JIS / EUC-JP の Denbun 電文を UTF-8 として壊さないため）。
    """

    logging.debug('Data broadcast upstream POST request received.')

    # Content-Length 先行拒否と stream 累積上限の両方で raw body を透過する。
    raw_body = await _ReadLimitedPostBody(request)

    content_type = request.headers.get('content-type') or 'application/x-www-form-urlencoded'
    headers = {
        'Accept': '*/*',
        'Pragma': 'no-cache',
        'Content-Type': content_type,
    }

    # GETと同じ接続先ポリシーをPOSTにも適用し、redirect時もraw bodyの扱いを共通化する。
    try:
        response = await RequestWithSafeRedirects(
            'POST',
            request_url,
            headers={**API_REQUEST_HEADERS, **headers},
            content=raw_body,
        )
    except UpstreamURLRejected as ex:
        logging.warning('[DataBroadcastingRouter][BMLBrowserRequestPOSTProxyAPI] Upstream URL rejected by egress policy.')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Request URL is not allowed',
        ) from ex
    except UpstreamBodyLimitExceeded as ex:
        # 巨大な上流応答はメモリ枯渇を防ぐため接続を切ったうえで 502 にする。
        logging.warning('[DataBroadcastingRouter][BMLBrowserRequestPOSTProxyAPI] Upstream response exceeded body limit.')
        raise HTTPException(
            status_code = status.HTTP_502_BAD_GATEWAY,
            detail = 'Upstream response is too large',
        ) from ex
    except Exception as ex:
        # 上流の接続失敗理由やURLを外部へ返さず、詳細はサーバーログだけに残す。
        logging.error('[DataBroadcastingRouter][BMLBrowserRequestPOSTProxyAPI] Failed to request:', exc_info=ex)
        raise HTTPException(
            status_code = status.HTTP_502_BAD_GATEWAY,
            detail = 'Failed to request upstream resource',
        ) from ex

    return StreamingResponse(
        _IterResponseContent(response.content),
        status_code = response.status_code,
        headers = _filterResponseHeaders(response.headers),
    )


@router.get(
    '/internet-status',
    summary = 'データ放送ブラウザネット接続状態確認 API',
    response_description = 'データ放送ブラウザ向けのネット接続状態。',
    response_model = schemas.DataBroadcastingInternetStatus,
)
async def BMLBrowserInternetStatusAPI(
    destination: Annotated[str, Query(description='接続先のホスト名または IP アドレス。')],
    is_icmp: Annotated[bool, Query(description='HTTP の代わりに ICMP (Ping) を使用するかどうか。')] = False,
    timeout_milliseconds: Annotated[
        int,
        Query(
            ge=PROBE_TIMEOUT_MILLISECONDS_MIN,
            le=PROBE_TIMEOUT_MILLISECONDS_MAX,
            description='タイムアウト時間 (ミリ秒) 。100〜5000 に制限する。',
        ),
    ] = 3000,
):
    """
    データ放送ブラウザ (web-bml) のネット接続機能から利用される、ネット接続状態確認 API。<br>
    Web ブラウザからの HTTP リクエストには CORS の制限があるため、この API により KonomiTV-BS4K サーバー側がネットに接続できるかが確認される。<br>
    web-bml のネット接続機能専用の API で、web-bml 以外からは利用されない。
    """

    # HTTP proxy と同じ公開IPポリシーで destination を検証する。
    # DNS 解決結果を固定し、loopback / private / link-local への probe を拒否する。
    try:
        probe_addresses = await ResolveProbeDestination(destination)
    except UpstreamURLRejected as ex:
        logging.warning('[DataBroadcastingRouter][BMLBrowserInternetStatusAPI] Probe destination rejected by egress policy.')
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Probe destination is not allowed',
        ) from ex

    timeout_seconds = timeout_milliseconds / 1000
    # 応答に載せる IP は検証済み一覧の先頭。接続もこの一覧の範囲だけを試す。
    selected_address = probe_addresses[0]
    success = False
    response_time: float | None = None

    # ICMP は同期の ping3 呼び出しのため thread へ退避し、event loop を塞がない。
    # ping3 の型スタブは timeout を int としているが、実装は秒単位の float を受け付ける。
    if is_icmp is True:
        try:
            ping_result = await asyncio.to_thread(
                ping,
                selected_address,
                timeout=timeout_seconds,  # type: ignore[arg-type]
            )
            # ping3 は timeout で None、その他の送受信エラーで False を返すことがある。
            if ping_result is not None and ping_result is not False:
                response_time = ping_result
                success = True
        except Exception:
            success = False
            response_time = None

    # TCP は検証済み公開IPへ直接接続し、hostname の再解決による DNS rebinding を避ける。
    else:
        start = time.monotonic()
        last_error: BaseException | None = None
        for address in probe_addresses:
            remaining = timeout_seconds - (time.monotonic() - start)
            if remaining <= 0:
                break
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(address, 80),
                    timeout=remaining,
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    # 接続確認後の後始末失敗は probe 成功結果を上書きしない。
                    pass
                success = True
                selected_address = address
                response_time = time.monotonic() - start
                break
            except Exception as ex:
                last_error = ex
                continue
        if success is False and last_error is not None:
            response_time = None

    # ミリ秒単位のレスポンス時間
    response_time_milliseconds = int(response_time * 1000) if response_time is not None else None

    return schemas.DataBroadcastingInternetStatus(
        success=success,
        # 成功時のみ検証済み公開IPを返す。失敗時に内部解決結果を漏らさない。
        ip_address=selected_address if success else None,
        response_time_milliseconds=response_time_milliseconds,
    )
