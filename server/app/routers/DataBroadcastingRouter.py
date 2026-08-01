
import asyncio
import socket
import time
from typing import Annotated

import httpx
from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from fastapi.responses import StreamingResponse
from ping3 import ping

from app import logging, schemas
from app.constants import API_REQUEST_HEADERS
from app.utils.DataBroadcastingHTTPClient import (
    RequestWithSafeRedirects,
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

    # Starlette FormParser を経由せず raw body を透過する
    raw_body = await request.body()
    if len(raw_body) > _MAX_POST_BODY_BYTES:
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = f'POST body must be at most {_MAX_POST_BODY_BYTES} bytes',
        )

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
    timeout_milliseconds: Annotated[int, Query(description='タイムアウト時間 (ミリ秒) 。')] = 3000,
):
    """
    データ放送ブラウザ (web-bml) のネット接続機能から利用される、ネット接続状態確認 API。<br>
    Web ブラウザからの HTTP リクエストには CORS の制限があるため、この API により KonomiTV サーバー側がネットに接続できるかが確認される。<br>
    web-bml のネット接続機能専用の API で、web-bml 以外からは利用されない。
    """

    # ICMP を使用する場合は ping3 ライブラリで ICMP パケットのレスポンス時間を取得
    if is_icmp is True:
        response_time = ping(destination, timeout=int(timeout_milliseconds / 1000))
        success = response_time is not None

    # ICMP を使用しない場合は asyncio.open_connection() でレスポンス時間を取得
    else:
        start = time.time()
        try:
            await asyncio.wait_for(asyncio.open_connection(destination, 80), timeout=timeout_milliseconds / 1000)
            success = True
            response_time = time.time() - start
        except Exception:
            success = False
            response_time = None

    # ミリ秒単位のレスポンス時間
    response_time_milliseconds = int(response_time * 1000) if response_time is not None else None

    return schemas.DataBroadcastingInternetStatus(
        success = success,
        ip_address = socket.gethostbyname(destination) if success else None,
        response_time_milliseconds = response_time_milliseconds,
    )
