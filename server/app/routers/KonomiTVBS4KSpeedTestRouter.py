from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from app import schemas
from app.utils.KonomiTVBS4KSpeedTest import (
    KONOMITV_BS4K_SPEED_TEST_DOWNLOAD_SECONDS,
    KONOMITV_BS4K_SPEED_TEST_MAX_DOWNLOAD_STREAMS,
    KONOMITV_BS4K_SPEED_TEST_MAX_UPLOAD_STREAMS,
    KONOMITV_BS4K_SPEED_TEST_SESSION_TTL_SECONDS,
    KONOMITV_BS4K_SPEED_TEST_UPLOAD_SECONDS,
    SPEED_TEST_SESSION_MANAGER,
    BuildKonomiTVBS4KSpeedTestQualityThresholds,
    BuildSpeedTestTransferHeaders,
    DrainSpeedTestUpload,
    GenerateKonomiTVBS4KSpeedTestSessionToken,
    IterateSpeedTestGarbage,
    RequireSpeedTestFetchMetadata,
    RequireSpeedTestOrigin,
    ResolveGarbageChunkCount,
    ResolveSpeedTestSessionFromCookie,
    SetKonomiTVBS4KSpeedTestSessionCookie,
)


router = APIRouter(
    tags = ['KonomiTV-BS4K Speed Test'],
    prefix = '/api/konomitv-bs4k/speed-test',
)


@router.post(
    '/session',
    summary = 'サーバー接続速度測定セッション作成 API',
    response_description = '測定枠の制限と推奨画質閾値。session ID は HttpOnly Cookie のみ。',
    response_model = schemas.KonomiTVBS4KSpeedTestSession,
)
async def KonomiTVBS4KSpeedTestSessionCreateAPI(
    request: Request,
    response: Response,
) -> schemas.KonomiTVBS4KSpeedTestSession:
    """
    測定枠を確保し、短命 Cookie と推奨画質閾値を返す。

    Args:
        request: Fetch Metadata 検証に使うリクエスト。
        response: 専用 Cookie を付ける応答。

    Returns:
        有効期限、stream 制限、推奨画質閾値。JWT 自体は返さない。
    """

    RequireSpeedTestFetchMetadata(request)
    session = await SPEED_TEST_SESSION_MANAGER.createSession()
    token = GenerateKonomiTVBS4KSpeedTestSessionToken(session.session_id)
    SetKonomiTVBS4KSpeedTestSessionCookie(response, token)
    thresholds = BuildKonomiTVBS4KSpeedTestQualityThresholds()
    return schemas.KonomiTVBS4KSpeedTestSession(
        expires_in_seconds = KONOMITV_BS4K_SPEED_TEST_SESSION_TTL_SECONDS,
        limits = schemas.KonomiTVBS4KSpeedTestLimits(
            download_streams = KONOMITV_BS4K_SPEED_TEST_MAX_DOWNLOAD_STREAMS,
            upload_streams = KONOMITV_BS4K_SPEED_TEST_MAX_UPLOAD_STREAMS,
            download_seconds = KONOMITV_BS4K_SPEED_TEST_DOWNLOAD_SECONDS,
            upload_seconds = KONOMITV_BS4K_SPEED_TEST_UPLOAD_SECONDS,
        ),
        quality_thresholds = [
            schemas.KonomiTVBS4KSpeedTestQualityThreshold(
                broadcast_type = threshold.broadcast_type,
                codec = threshold.codec,
                quality = threshold.quality,
                required_mbps = threshold.required_mbps,
                basis = threshold.basis,
            )
            for threshold in thresholds
        ],
    )


@router.delete(
    '/session',
    summary = 'サーバー接続速度測定セッション削除 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def KonomiTVBS4KSpeedTestSessionDeleteAPI(
    request: Request,
) -> Response:
    """
    Cookie が指す測定枠を解放する。

    Args:
        request: Origin / Fetch Metadata / Cookie 検証に使うリクエスト。

    Returns:
        本文なしの 204。
    """

    RequireSpeedTestFetchMetadata(request)
    RequireSpeedTestOrigin(request)
    # Cookie が指す枠だけを解放する。別タブの測定を落とさない。
    try:
        session = await ResolveSpeedTestSessionFromCookie(request)
    except HTTPException:
        session = None
    if session is not None:
        await SPEED_TEST_SESSION_MANAGER.releaseSession(session.session_id)
    # HttpOnly Cookie は短命で、次の session 作成時に上書きされる。
    ## DELETE 応答で消すと、遅延した旧応答が新しい session Cookie を消すため、ここでは変更しない。
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    '/garbage',
    summary = 'サーバー接続速度測定下り API',
    response_class = StreamingResponse,
)
async def KonomiTVBS4KSpeedTestGarbageAPI(
    request: Request,
    ckSize: Annotated[str | None, Query(alias='ckSize')] = None,
) -> StreamingResponse:
    """
    非圧縮の乱数を StreamingResponse で送る。

    Args:
        request: Cookie と Fetch Metadata の検証、切断検知に使う。
        ckSize: LibreSpeed が付ける 1MiB chunk 数。欠如や不正値は 4、上限は 100。

    Returns:
        最大 100MiB の application/octet-stream。
    """

    RequireSpeedTestFetchMetadata(request)
    session = await ResolveSpeedTestSessionFromCookie(request)
    chunk_count = ResolveGarbageChunkCount(ckSize)
    # 枠確保を StreamingResponse 開始前に行い、6本目は 200 ではなく 429 を返す。
    await SPEED_TEST_SESSION_MANAGER.beginTransfer(session.session_id, 'download')

    async def Generate() -> AsyncIterator[bytes]:
        async for chunk in IterateSpeedTestGarbage(request, session.session_id, chunk_count):
            yield chunk

    return StreamingResponse(
        Generate(),
        media_type = 'application/octet-stream',
        headers = BuildSpeedTestTransferHeaders(),
    )


@router.get(
    '/empty',
    summary = 'サーバー接続速度測定 ping API',
)
async def KonomiTVBS4KSpeedTestEmptyGetAPI(request: Request) -> Response:
    """
    ping / jitter 用の空応答を返す。

    Args:
        request: Cookie と Fetch Metadata の検証に使う。

    Returns:
        本文なしの 200。
    """

    RequireSpeedTestFetchMetadata(request)
    session = await ResolveSpeedTestSessionFromCookie(request)
    async with SPEED_TEST_SESSION_MANAGER.acquireTransfer(session.session_id, 'ping'):
        return Response(status_code=status.HTTP_200_OK, headers=BuildSpeedTestTransferHeaders('text/plain'))


@router.post(
    '/empty',
    summary = 'サーバー接続速度測定上り API',
)
async def KonomiTVBS4KSpeedTestEmptyPostAPI(request: Request) -> Response:
    """
    上り本文をメモリへ保持せず drain する。

    Args:
        request: Cookie・Origin・Fetch Metadata の検証と本文 drain に使う。

    Returns:
        本文なしの 200。
    """

    RequireSpeedTestFetchMetadata(request)
    RequireSpeedTestOrigin(request)
    session = await ResolveSpeedTestSessionFromCookie(request)
    async with SPEED_TEST_SESSION_MANAGER.acquireTransfer(session.session_id, 'upload'):
        await DrainSpeedTestUpload(request, session.session_id)
    return Response(status_code=status.HTTP_200_OK, headers=BuildSpeedTestTransferHeaders('text/plain'))
