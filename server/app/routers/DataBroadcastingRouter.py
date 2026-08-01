
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


# ルーター
router = APIRouter(
    tags = ['Data Broadcasting'],
    prefix = '/api/data-broadcasting',
)

# 以下の API 実装は web-bml での実装を Python に移植したもの (with GPT-4)
# ref: https://github.com/tsukumijima/web-bml/blob/master/server/index.ts#L195-L296

# データ放送 POST の本文は Denbun 電文として最大 4096 バイトまでを byte 透過で転送する
_MAX_POST_BODY_BYTES = 4096


def _validateUpstreamURL(request_url: str) -> None:
    """上流プロキシ対象 URL が HTTP/HTTPS であることを検証する。"""

    if not (request_url.startswith('http://') or request_url.startswith('https://')):
        logging.error(f'[DataBroadcastingRouter] Request URL must be http or https URL: {request_url}')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Request URL must be http or https URL',
        )


def _filterResponseHeaders(response: httpx.Response) -> dict[str, str]:
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
        for key, value in response.headers.items()
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

    _validateUpstreamURL(request_url)
    logging.debug(f'Request URL: {request_url}')

    headers = {
        'Accept': '*/*',
        'Accept-Language': 'ja',
        'Pragma': 'no-cache',
    }
    allowed_request_headers = ['if-modified-since', 'cache-control']
    for key, value in request.headers.items():
        if key.lower() in allowed_request_headers:
            headers[key] = value

    # タイムアウトはデータ放送の動作を壊さないようにあえて設定しない
    # さらにデータ放送からアクセスされるサイトは HTTPS の場合でも証明書が切れていることが日常茶飯事なので、証明書の検証を行わない
    ## 正確には放送波経由で古い規格の HTTPS 証明書が降ってきているらしいが、どのみち実装困難なので証明書の状態は無視する
    async with httpx.AsyncClient(headers={**API_REQUEST_HEADERS, **headers}, follow_redirects=True, verify=False) as client:
        try:
            response = await client.get(request_url)
        except Exception as ex:
            # リクエスト中に例外が発生した場合は、エラーメッセージをログに出力して 500 エラーを返す
            ## HTTP リクエスト自体が DNS 名前解決エラーや接続エラーで失敗した場合に発生する
            logging.error('[DataBroadcastingRouter][BMLBrowserRequestGETProxyAPI] Failed to request:', exc_info=ex)
            raise HTTPException(
                status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail = f'Failed to request: {ex}',
            )

    return StreamingResponse(
        response.iter_bytes(),
        status_code = response.status_code,
        headers = _filterResponseHeaders(response),
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

    _validateUpstreamURL(request_url)
    logging.debug(f'Request URL: {request_url}')

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

    # タイムアウトはデータ放送の動作を壊さないようにあえて設定しない
    # さらにデータ放送からアクセスされるサイトは HTTPS の場合でも証明書が切れていることが日常茶飯事なので、証明書の検証を行わない
    ## 正確には放送波経由で古い規格の HTTPS 証明書が降ってきているらしいが、どのみち実装困難なので証明書の状態は無視する
    async with httpx.AsyncClient(headers={**API_REQUEST_HEADERS, **headers}, follow_redirects=True, verify=False) as client:
        try:
            response = await client.post(request_url, content=raw_body)
        except Exception as ex:
            # リクエスト中に例外が発生した場合は、エラーメッセージをログに出力して 500 エラーを返す
            ## HTTP リクエスト自体が DNS 名前解決エラーや接続エラーで失敗した場合に発生する
            logging.error('[DataBroadcastingRouter][BMLBrowserRequestPOSTProxyAPI] Failed to request:', exc_info=ex)
            raise HTTPException(
                status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail = f'Failed to request: {ex}',
            )

    return StreamingResponse(
        response.iter_bytes(),
        status_code = response.status_code,
        headers = _filterResponseHeaders(response),
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
    Web ブラウザからの HTTP リクエストには CORS の制限があるため、この API により KonomiTV-BS4K サーバー側がネットに接続できるかが確認される。<br>
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
