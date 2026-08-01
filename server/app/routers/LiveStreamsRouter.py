
import asyncio
import copy
import time
from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from fastapi.requests import Request
from fastapi.responses import Response, StreamingResponse
from sse_starlette.sse import EventSourceResponse
from starlette.types import Receive

from app import logging, schemas
from app.constants import QUALITY_TYPES
from app.models.Channel import Channel
from app.streams.KonomiTVBS4KPlaybackCapabilities import (
    KonomiTVBS4KPlaybackCapabilityProbe,
)
from app.streams.KonomiTVBS4KPlaybackEncoding import (
    KonomiTVBS4KAudioCodec,
    KonomiTVBS4KVideoBitDepth,
    KonomiTVBS4KVideoBitDepthQuery,
    KonomiTVBS4KVideoCodec,
)
from app.streams.LivePrepareCoordinator import (
    LIVE_PREPARE_COORDINATOR,
    LivePrepareFinalizeResult,
    LivePrepareLeaseError,
)
from app.streams.LiveSourceCoordinator import LiveSourceSubscriberError
from app.streams.LiveStream import LiveStream, LiveStreamStatus
from app.streams.StreamEncodingOptions import (
    GetEncoderForLiveChannel,
    SplitQualityAndEncodingOptions,
    StreamEncodingOptions,
    StreamQualityWithOptions,
)


# ルーター
router = APIRouter(
    tags = ['Streams'],
    prefix = '/api/streams/live',
)
_PLAYBACK_SESSION_RETIRE_FAILURE_RESULTS = frozenset({
    'cleanup_timeout',
    'cleanup_failed',
    'session_ambiguous',
})


async def ValidateChannelID(display_channel_id: Annotated[str, Path(description='チャンネル ID 。ex: gr011')]) -> str:
    """ チャンネル ID のバリデーション """

    # チャンネル ID が存在し、ライブ視聴可能か確認
    ## 録画ファイルから登録された録画専用チャンネルなどを URL の直接指定で視聴しようとすると、
    ## バックエンド側に存在しないサービスの選局へ進んでしまうため、ここで確実に拒否する。
    channel = await Channel.filter(display_channel_id=display_channel_id).get_or_none()
    if channel is None:
        logging.error(f'[LiveStreamsRouter][ValidateChannelID] Specified display_channel_id was not found. [display_channel_id: {display_channel_id}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified display_channel_id was not found',
        )
    if channel.is_watchable is False:
        logging.error(f'[LiveStreamsRouter][ValidateChannelID] Specified display_channel_id is not watchable. [display_channel_id: {display_channel_id}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified display_channel_id is not watchable',
        )

    return display_channel_id


async def ValidateQuality(
    quality: Annotated[str, Path(description='映像の品質。ex: 1080p')],
    display_channel_id: Annotated[str, Depends(ValidateChannelID)],
    video_codec: Annotated[
        KonomiTVBS4KVideoCodec | None,
        Query(description='出力映像コーデック。省略時は旧画質URLから判定。'),
    ] = None,
    video_bit_depth: Annotated[
        KonomiTVBS4KVideoBitDepthQuery | None,
        Query(description='出力映像bit depth。'),
    ] = None,
    audio_codec: Annotated[
        KonomiTVBS4KAudioCodec,
        Query(description='出力音声コーデック。'),
    ] = 'aac',
) -> StreamQualityWithOptions:
    """ 映像の品質のバリデーション """

    # 指定された品質が存在するか確認
    ## 品質の指定に -10bit や -24fps が付いていれば分解する
    selected_encoder = GetEncoderForLiveChannel(display_channel_id)
    stream_quality = SplitQualityAndEncodingOptions(
        quality,
        selected_encoder,
        video_codec = video_codec,
        video_bit_depth = cast(
            KonomiTVBS4KVideoBitDepth | None,
            int(video_bit_depth) if video_bit_depth is not None else None,
        ),
        audio_codec = audio_codec,
    )
    if stream_quality is None:
        logging.error(f'[LiveStreamsRouter][ValidateQuality] Specified quality was not found. [quality: {quality}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified quality was not found',
        )

    # ラジオは設定中の映像 HW backend を実際には使わず、LiveEncodingTask と同じく FFmpeg で処理する。
    # Opus は映像付きの組み合わせ行列ではなく、実経路と同じ audio-only probe の音声能力で検査する。
    capability_encoder = selected_encoder
    channel = await Channel.filter(display_channel_id = display_channel_id).get_or_none()
    is_radiochannel = channel is not None and channel.is_radiochannel is True
    if is_radiochannel is True:
        capability_encoder = 'FFmpeg'

        # ラジオ経路は映像を生成しない。映像codec・bit depth・24fps・旧HEVC品質を共有キーへ
        # 残すと、同じ音声codecでも無意味なquery差だけで別チューナーを起動するため正規化する。
        stream_quality = StreamQualityWithOptions(
            quality = cast(QUALITY_TYPES, stream_quality.quality.removesuffix('-hevc')),
            encoding_options = StreamEncodingOptions(audio_codec = audio_codec),
        )

    resolved_video_codec = stream_quality.encoding_options.video_codec
    resolved_video_bit_depth = stream_quality.encoding_options.video_bit_depth
    resolved_audio_codec = stream_quality.encoding_options.audio_codec
    is_advanced_combination = (
        resolved_video_codec in ('vp9', 'av1') or resolved_audio_codec == 'opus'
    )

    # ラジオだけは audio-only topology を検査する。映像付き高度構成はこの後の exact
    # combination 一本で映像・音声を同時に検査し、video+AAC や radio Opus を重複起動しない。
    if is_radiochannel is True and resolved_audio_codec != 'aac':
        audio_capability = await KonomiTVBS4KPlaybackCapabilityProbe.getAudioCapability(audio_codec)
        if audio_capability is None or audio_capability.live_available is False:
            reason_code = audio_capability.live_reason_code if audio_capability is not None else 'ProbeFailed'
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = {
                    'code': reason_code,
                    'message': 'The requested live audio encoding is unavailable.',
                },
            )
    elif is_radiochannel is False and is_advanced_combination is True:
        live_combination = (
            await KonomiTVBS4KPlaybackCapabilityProbe.getLiveCombinationCapability(
                capability_encoder,
                resolved_video_codec,
                resolved_video_bit_depth,
                resolved_audio_codec,
            )
        )
        if live_combination is None or live_combination.available is False:
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = {
                    'code': (
                        live_combination.reason_code
                        if live_combination is not None
                        and live_combination.reason_code is not None
                        else 'UnsupportedCombination'
                    ),
                    'message': 'The requested live video and audio encoding combination is unavailable.',
                },
            )
    elif (
        is_radiochannel is False
        and resolved_video_codec == 'hevc'
        and (video_codec is not None or video_bit_depth is not None)
    ):
        # HEVC/AAC は旧TS経路だが、明示query時だけ既存の映像能力検査を維持する。
        video_capability = await KonomiTVBS4KPlaybackCapabilityProbe.getVideoCapability(
            capability_encoder,
            resolved_video_codec,
            resolved_video_bit_depth,
        )
        if video_capability is None or video_capability.live_available is False:
            reason_code = (
                video_capability.live_reason_code
                if video_capability is not None
                else 'ProbeFailed'
            )
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = {
                    'code': reason_code,
                    'message': 'The requested live video encoding is unavailable.',
                },
            )

    return stream_quality


@router.get(
    '',
    summary = 'ライブストリーム一覧 API',
    response_description = 'ステータスごとに分類された、すべてのライブストリームの状態。',
    response_model = schemas.LiveStreamStatuses,
)
async def LiveStreamsAPI():
    """
    すべてのライブストリームの状態を Offline・Standby・ONAir・Idling・Restart の各ステータスごとに取得する。
    """

    # 返却するデータ
    # 逆順になっているのは、デバッグ時に全体の大半を占める Offline なストリームが邪魔なため
    result: dict[str, dict[str, LiveStreamStatus]] = {
        'Restart': {},
        'Idling' : {},
        'ONAir'  : {},
        'Standby': {},
        'Offline': {},
    }

    # すべてのストリームごとに
    for live_stream in LiveStream.getAllLiveStreams():
        live_stream_status = live_stream.getStatus()
        result[live_stream_status.status][live_stream.live_stream_id] = live_stream_status

    # すべてのライブストリームの状態を返す
    return result


@router.get(
    '/{display_channel_id}/{quality}',
    summary = 'ライブストリーム API',
    response_description = 'ライブストリームの状態。',
    response_model = schemas.LiveStreamStatus,
)
async def LiveStreamAPI(
    display_channel_id: Annotated[str, Depends(ValidateChannelID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
):
    """
    ライブストリームの状態を取得する。<br>
    ライブストリーム イベント API にて配信されるイベントと同一のデータだが、一回限りの取得である点が異なる。
    """

    # 品質とオプション指定に対応する LiveStream を取得する
    # ステータスを取得したいだけなので、接続はしない
    live_stream = LiveStream(display_channel_id, stream_quality.quality, stream_quality.encoding_options)

    # 取得してきた値をそのまま返す
    return live_stream.getStatus()


async def _ResolvePrepareBackend(display_channel_id: str) -> str:
    """
    実際に target stream が利用する encoder backend を返す。

    Args:
        display_channel_id (str): 対象チャンネルの表示用 ID。

    Returns:
        str: 処理結果の文字列。
    """

    channel = await Channel.filter(display_channel_id=display_channel_id).get_or_none()
    if channel is not None and channel.is_radiochannel is True:
        return 'FFmpeg'
    return GetEncoderForLiveChannel(display_channel_id)


@router.post(
    '/{display_channel_id}/{quality}/prepare',
    summary = 'ライブストリーム Prepare lease 取得 API',
    response_model = schemas.LivePrepareLeaseResponse,
)
async def AcquireLivePrepareLeaseAPI(
    prepare_request: Annotated[schemas.LivePrepareLeaseAcquireRequest, Body()],
    display_channel_id: Annotated[str, Depends(ValidateChannelID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
) -> schemas.LivePrepareLeaseResponse:
    """
    二段階 B の encoder 起動前に、backend別の短期単回 lease を取得する。

    Args:
        prepare_request (Annotated[schemas.LivePrepareLeaseAcquireRequest, Body()]): 切り替え元 A の接続 UUID。
        display_channel_id (Annotated[str, Depends(ValidateChannelID)]): 対象チャンネルの表示用 ID。
        stream_quality (Annotated[StreamQualityWithOptions, Depends(ValidateQuality)]): codec option を解決済みのライブ画質。

    Returns:
        schemas.LivePrepareLeaseResponse: lease 状態と token を含む API 応答。
    """

    live_stream = LiveStream(
        display_channel_id,
        stream_quality.quality,
        stream_quality.encoding_options,
    )
    source_playback_session_id = str(prepare_request.source_playback_session_id)
    prepared_playback_session_id = str(prepare_request.prepared_playback_session_id)
    if source_playback_session_id == prepared_playback_session_id:
        return schemas.LivePrepareLeaseResponse(
            accepted = False,
            token = None,
            expires_at = None,
            reason = 'prepared_playback_session_matches_source',
        )
    source_matches = LiveStream.findPlaybackSession(source_playback_session_id)
    if len(source_matches) != 1:
        return schemas.LivePrepareLeaseResponse(
            accepted = False,
            token = None,
            expires_at = None,
            reason = (
                'source_playback_session_not_connected'
                if len(source_matches) == 0
                else 'source_playback_session_ambiguous'
            ),
        )
    source_live_stream, source_client = source_matches[0]
    if (
        source_live_stream.display_channel_id != display_channel_id or
        source_live_stream.stream_anchor_enabled is False or
        source_client.prepare_token is not None
    ):
        return schemas.LivePrepareLeaseResponse(
            accepted = False,
            token = None,
            expires_at = None,
            reason = 'source_playback_session_target_mismatch',
        )

    backend = await _ResolvePrepareBackend(display_channel_id)
    grant = await LIVE_PREPARE_COORDINATOR.acquire(
        backend,
        live_stream.getPrepareLeaseKey(),
        source_stream_key = source_live_stream.getPrepareLeaseKey(),
        source_playback_session_id = source_playback_session_id,
        prepared_playback_session_id = prepared_playback_session_id,
    )
    if grant.accepted is False:
        # rejection は個々の Prepare 要求の結果であり、既存 Active viewer の共有 SSE 状態を
        # fault にしてはならない。未起動 target の観測値に限って反映する。
        live_stream_status = live_stream.getStatus()
        if live_stream_status.status == 'Offline' and live_stream_status.client_count == 0:
            live_stream.setPrepareRejected()
        return schemas.LivePrepareLeaseResponse(
            accepted = False,
            token = None,
            expires_at = None,
            reason = grant.reason,
        )

    assert grant.token is not None
    assert grant.expires_at is not None
    assert grant.lease is not None
    live_stream.setPrepareLease(
        grant.token,
        expires_in_seconds = max(0.0, grant.lease.expires_at - time.monotonic()),
    )
    expires_at = datetime.fromtimestamp(grant.expires_at, tz=UTC).isoformat().replace('+00:00', 'Z')
    return schemas.LivePrepareLeaseResponse(
        accepted = True,
        token = grant.token,
        expires_at = expires_at,
        reason = None,
    )


@router.delete(
    '/{display_channel_id}/{quality}/prepare',
    summary = 'ライブストリーム Prepare lease 解放 API',
    response_model = schemas.LivePrepareLeaseResponse,
)
async def ReleaseLivePrepareLeaseAPI(
    prepare_request: Annotated[schemas.LivePrepareLeaseRequest, Body()],
    display_channel_id: Annotated[str, Depends(ValidateChannelID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
) -> schemas.LivePrepareLeaseResponse:
    """
    Abort または Commit に応じて B の lease と source role を確定する。

    Args:
        prepare_request (Annotated[schemas.LivePrepareLeaseRequest, Body()]): Commit または Abort と解放理由を含む要求。
        display_channel_id (Annotated[str, Depends(ValidateChannelID)]): 対象チャンネルの表示用 ID。
        stream_quality (Annotated[StreamQualityWithOptions, Depends(ValidateQuality)]): codec option を解決済みのライブ画質。

    Returns:
        schemas.LivePrepareLeaseResponse: lease 状態と token を含む API 応答。
    """

    live_stream = LiveStream(
        display_channel_id,
        stream_quality.quality,
        stream_quality.encoding_options,
    )
    snapshot = await LIVE_PREPARE_COORDINATOR.get(prepare_request.token)
    if snapshot is None or snapshot.stream_key != live_stream.getPrepareLeaseKey():
        return schemas.LivePrepareLeaseResponse(
            accepted = False,
            token = None,
            expires_at = None,
            reason = 'prepare_token_target_mismatch',
        )

    async def finalize_operation() -> LivePrepareFinalizeResult:
        """
        Commit / Abort の不可逆処理を token ごとの単一 task で実行する。

        Args:
            None

        Returns:
            LivePrepareFinalizeResult: cleanup・rollback 可否まで確定した結果。
        """

        current = await LIVE_PREPARE_COORDINATOR.get(prepare_request.token)
        if current is None or current.stream_key != live_stream.getPrepareLeaseKey():
            return LivePrepareFinalizeResult(
                accepted = False,
                reason = 'prepare_token_target_mismatch',
                cleanup_confirmed = False,
                rollback_allowed = True,
                terminal_restart_required = False,
            )

        if prepare_request.disposition == 'abort':
            prepared_session_id = current.prepared_playback_session_id
            if prepared_session_id is not None:
                retire_result = await live_stream.retirePlaybackSession(prepared_session_id)
                if retire_result in _PLAYBACK_SESSION_RETIRE_FAILURE_RESULTS:
                    return LivePrepareFinalizeResult(
                        accepted = False,
                        reason = f'prepare_abort_{retire_result}',
                        cleanup_confirmed = False,
                        rollback_allowed = False,
                        terminal_restart_required = True,
                        can_converge_after_cleanup = retire_result == 'cleanup_timeout',
                    )
            # tokenを消費したBだけを外した後、残存viewerのsource roleをActiveへ戻し、
            # viewerが0なら上のretireでencoder cleanup済みの状態からleaseを解放する。
            await live_stream.finalizePrepareClientRemoval(
                prepare_request.token,
                reason = 'abort',
            )
            latest = await LIVE_PREPARE_COORDINATOR.get(prepare_request.token)
            accepted = (
                latest is not None and
                latest.stream_key == live_stream.getPrepareLeaseKey() and
                latest.state in ('Released', 'Expired')
            )
            return LivePrepareFinalizeResult(
                accepted = accepted,
                reason = None if accepted else 'prepare_abort_not_released',
                cleanup_confirmed = accepted,
                rollback_allowed = True,
                terminal_restart_required = False,
            )

        if current.state == 'Released' and current.release_reason == 'commit':
            return LivePrepareFinalizeResult(
                accepted = True,
                reason = None,
                cleanup_confirmed = True,
                rollback_allowed = False,
                terminal_restart_required = False,
            )
        if current.state != 'Consumed':
            return LivePrepareFinalizeResult(
                accepted = False,
                reason = 'prepare_token_not_consumed',
                cleanup_confirmed = False,
                rollback_allowed = True,
                terminal_restart_required = False,
            )

        source_stream_key = current.source_stream_key
        source_session_id = current.source_playback_session_id
        prepared_session_id = current.prepared_playback_session_id
        source_live_stream = (
            LiveStream.getByPrepareLeaseKey(source_stream_key)
            if source_stream_key is not None
            else None
        )
        source_matches = (
            LiveStream.findPlaybackSession(source_session_id)
            if source_session_id is not None
            else []
        )
        prepare_client = live_stream.getPrepareClient(prepare_request.token)
        # A/B両接続を不可逆操作前に厳密に再検証する。ここでの拒否ならAへrollbackできる。
        if (
            source_live_stream is None or
            len(source_matches) != 1 or
            source_matches[0][0] is not source_live_stream
        ):
            return LivePrepareFinalizeResult(
                accepted = False,
                reason = 'source_playback_session_not_connected',
                cleanup_confirmed = False,
                rollback_allowed = True,
                terminal_restart_required = False,
            )
        if (
            prepare_client is None or
            prepared_session_id is None or
            prepare_client.playback_session_id != prepared_session_id
        ):
            prepared_retire_result = (
                await live_stream.retirePlaybackSession(prepared_session_id)
                if prepared_session_id is not None
                else 'cleanup_completed'
            )
            if prepared_retire_result not in _PLAYBACK_SESSION_RETIRE_FAILURE_RESULTS:
                await live_stream.finalizePrepareClientRemoval(
                    prepare_request.token,
                    reason = 'commit_without_connected_client',
                )
            return LivePrepareFinalizeResult(
                accepted = False,
                reason = (
                    f'prepare_playback_session_{prepared_retire_result}'
                    if prepared_retire_result in _PLAYBACK_SESSION_RETIRE_FAILURE_RESULTS
                    else 'prepare_client_not_connected'
                ),
                cleanup_confirmed = prepared_retire_result not in _PLAYBACK_SESSION_RETIRE_FAILURE_RESULTS,
                rollback_allowed = prepared_retire_result not in _PLAYBACK_SESSION_RETIRE_FAILURE_RESULTS,
                terminal_restart_required = prepared_retire_result in _PLAYBACK_SESSION_RETIRE_FAILURE_RESULTS,
            )

        try:
            await live_stream.promotePreparedSourceToActive()
        except (LivePrepareLeaseError, LiveSourceSubscriberError) as ex:
            retire_result = await live_stream.retirePlaybackSession(prepared_session_id)
            if retire_result not in _PLAYBACK_SESSION_RETIRE_FAILURE_RESULTS:
                await live_stream.finalizePrepareClientRemoval(
                    prepare_request.token,
                    reason = 'commit_failed',
                )
            return LivePrepareFinalizeResult(
                accepted = False,
                reason = (
                    f'prepare_commit_{retire_result}'
                    if retire_result in _PLAYBACK_SESSION_RETIRE_FAILURE_RESULTS
                    else str(ex)
                ),
                cleanup_confirmed = retire_result not in _PLAYBACK_SESSION_RETIRE_FAILURE_RESULTS,
                rollback_allowed = retire_result not in _PLAYBACK_SESSION_RETIRE_FAILURE_RESULTS,
                terminal_restart_required = retire_result in _PLAYBACK_SESSION_RETIRE_FAILURE_RESULTS,
            )

        # promotion待機中にBが消えた場合はAをまだ触っていないためrollback可能。
        if live_stream.getPrepareClient(prepare_request.token) is not prepare_client:
            await live_stream.finalizePrepareClientRemoval(
                prepare_request.token,
                reason = 'commit_client_disconnected',
            )
            return LivePrepareFinalizeResult(
                accepted = False,
                reason = 'prepare_client_not_connected',
                cleanup_confirmed = True,
                rollback_allowed = True,
                terminal_restart_required = False,
            )

        # ここからAは不可逆。対象UUIDだけをEOF終了し、他viewerが0の場合だけencoderを停止する。
        assert source_session_id is not None
        source_retire_result = await source_live_stream.retirePlaybackSession(source_session_id)
        if source_retire_result in _PLAYBACK_SESSION_RETIRE_FAILURE_RESULTS:
            return LivePrepareFinalizeResult(
                accepted = False,
                reason = f'source_playback_session_{source_retire_result}',
                cleanup_confirmed = False,
                rollback_allowed = False,
                terminal_restart_required = True,
            )

        # A cleanup中にBが消えた場合、Aへは戻せないため完全再起動を要求する。
        if live_stream.getPrepareClient(prepare_request.token) is not prepare_client:
            prepared_retire_result = await live_stream.retirePlaybackSession(prepared_session_id)
            if prepared_retire_result not in _PLAYBACK_SESSION_RETIRE_FAILURE_RESULTS:
                await live_stream.finalizePrepareClientRemoval(
                    prepare_request.token,
                    reason = 'commit_client_disconnected_after_source_retire',
                )
            return LivePrepareFinalizeResult(
                accepted = False,
                reason = (
                    f'prepare_playback_session_{prepared_retire_result}'
                    if prepared_retire_result in _PLAYBACK_SESSION_RETIRE_FAILURE_RESULTS
                    else 'prepare_client_not_connected_after_source_retire'
                ),
                cleanup_confirmed = prepared_retire_result not in _PLAYBACK_SESSION_RETIRE_FAILURE_RESULTS,
                rollback_allowed = False,
                terminal_restart_required = True,
            )

        await live_stream.releasePrepareLease(
            prepare_request.token,
            reason = 'commit',
        )
        latest = await LIVE_PREPARE_COORDINATOR.get(prepare_request.token)
        # 最後の await 後にB接続を同期再取得し、判定との間へ切断raceを挟まない。
        prepared_matches_after_release = LiveStream.findPlaybackSession(prepared_session_id)
        accepted = (
            latest is not None and
            latest.stream_key == live_stream.getPrepareLeaseKey() and
            latest.state == 'Released' and
            latest.release_reason == 'commit' and
            len(prepared_matches_after_release) == 1 and
            prepared_matches_after_release[0][0] is live_stream and
            prepared_matches_after_release[0][1] is prepare_client
        )
        return LivePrepareFinalizeResult(
            accepted = accepted,
            reason = None if accepted else 'prepare_commit_not_settled',
            cleanup_confirmed = accepted,
            rollback_allowed = False,
            terminal_restart_required = accepted is False,
        )

    try:
        finalize_result = await LIVE_PREPARE_COORDINATOR.finalize(
            prepare_request.token,
            prepare_request.disposition,
            finalize_operation,
        )
    except Exception as ex:
        logging.error(
            f'{live_stream.log_prefix} Prepare {prepare_request.disposition} task failed unexpectedly.',
            exc_info = ex,
        )
        finalize_result = LivePrepareFinalizeResult(
            accepted = False,
            reason = 'prepare_finalize_task_failed',
            cleanup_confirmed = False,
            rollback_allowed = False,
            terminal_restart_required = True,
        )
    return schemas.LivePrepareLeaseResponse(
        accepted = finalize_result.accepted,
        token = None,
        expires_at = None,
        reason = finalize_result.reason,
        cleanup_confirmed = finalize_result.cleanup_confirmed,
        rollback_allowed = finalize_result.rollback_allowed,
        terminal_restart_required = finalize_result.terminal_restart_required,
    )


async def _RetireLivePlaybackSession(
    retire_request: schemas.LivePlaybackSessionRetireRequest,
    display_channel_id: str,
    stream_quality: StreamQualityWithOptions,
) -> schemas.LivePlaybackSessionRetireResponse:
    """
    request切断から分離したtask内でsession終了とPrepare finalizeを一体実行する。

    Args:
        retire_request (schemas.LivePlaybackSessionRetireRequest): 終了するpipeline UUID。
        display_channel_id (str): 対象チャンネルの表示用ID。
        stream_quality (StreamQualityWithOptions): codec option解決済みの画質。

    Returns:
        schemas.LivePlaybackSessionRetireResponse: 接続・encoder cleanup の確定結果。
    """

    live_stream = LiveStream(
        display_channel_id,
        stream_quality.quality,
        stream_quality.encoding_options,
    )
    session_id = str(retire_request.playback_session_id)
    # POST応答消失後のRetireが遅着POST acquireを追い越しても、同じB candidate UUIDを
    # 新しいPrepared leaseとして復活させない。Coordinator lockが両操作の線形化点になる。
    await LIVE_PREPARE_COORDINATOR.retirePreparedPlaybackSessionID(session_id)
    matches = LiveStream.findPlaybackSession(session_id)
    if len(matches) > 1 or (len(matches) == 1 and matches[0][0] is not live_stream):
        return schemas.LivePlaybackSessionRetireResponse(
            accepted = False,
            result = 'session_ambiguous',
            reason = 'playback_session_ambiguous',
            cleanup_confirmed = False,
            terminal_restart_required = True,
        )

    prepare_tokens = {
        client.prepare_token
        for _owner_stream, client in matches
        if client.prepare_token is not None
    }
    pinned_leases = await LIVE_PREPARE_COORDINATOR.findByPreparedPlaybackSessionID(
        stream_key = live_stream.getPrepareLeaseKey(),
        playback_session_id = session_id,
    )
    prepare_tokens.update(token for token, _snapshot in pinned_leases)
    if len(prepare_tokens) > 1:
        return schemas.LivePlaybackSessionRetireResponse(
            accepted = False,
            result = 'session_ambiguous',
            reason = 'playback_session_ambiguous',
            cleanup_confirmed = False,
            terminal_restart_required = True,
        )

    retire_result = await live_stream.retirePlaybackSession(
        session_id,
        cleanup_timeout_seconds = live_stream.PLAYBACK_SESSION_CLEANUP_TIMEOUT_SECONDS,
    )
    if retire_result == 'cleanup_timeout':
        # HTTP waiterへは別途bounded timeoutを返すが、一体settlement taskは実cleanupと
        # Prepare finalizeが確定するまで継続する。
        retire_result = await live_stream.waitForPlaybackSessionCleanup()
    cleanup_confirmed = retire_result in ('cleanup_completed', 'shared_encoder_retained')
    if cleanup_confirmed is True and len(prepare_tokens) == 1:
        prepare_token = next(iter(prepare_tokens))
        async def abort_operation() -> LivePrepareFinalizeResult:
            """
            明示Retire後のPrepare lease解放を単一Abort taskへ合流する。

            Returns:
                LivePrepareFinalizeResult: lease解放の確定結果。
            """

            await live_stream.finalizePrepareClientRemoval(
                prepare_token,
                reason = 'retire',
            )
            released_snapshot = await LIVE_PREPARE_COORDINATOR.get(prepare_token)
            accepted = released_snapshot is not None and released_snapshot.state == 'Released'
            return LivePrepareFinalizeResult(
                accepted = accepted,
                reason = None if accepted else 'prepare_lease_not_released_after_retire',
                cleanup_confirmed = accepted,
                rollback_allowed = accepted,
                terminal_restart_required = accepted is False,
            )

        finalize_result = await LIVE_PREPARE_COORDINATOR.finalize(
            prepare_token,
            'abort',
            abort_operation,
        )
        if finalize_result.accepted is False:
            latest_snapshot = await LIVE_PREPARE_COORDINATOR.get(prepare_token)
            # 物理Retireと同時にCommitが先に成功した場合、Prepare上のAbortは競合だが
            # session資源の終了自体は確認済みなのでRetire APIまで失敗へ巻き戻さない。
            committed_before_abort = (
                latest_snapshot is not None
                and latest_snapshot.state == 'Released'
                and latest_snapshot.release_reason == 'commit'
            )
            if committed_before_abort is False:
                return schemas.LivePlaybackSessionRetireResponse(
                    accepted = False,
                    result = 'cleanup_failed',
                    reason = finalize_result.reason,
                    cleanup_confirmed = False,
                    terminal_restart_required = finalize_result.terminal_restart_required,
                )
    return schemas.LivePlaybackSessionRetireResponse(
        accepted = cleanup_confirmed,
        result = retire_result,
        reason = None if cleanup_confirmed is True else f'playback_session_{retire_result}',
        cleanup_confirmed = cleanup_confirmed,
        terminal_restart_required = cleanup_confirmed is False,
    )


@router.delete(
    '/{display_channel_id}/{quality}/mpegts',
    summary = 'ライブ再生 session 終了 API',
    response_model = schemas.LivePlaybackSessionRetireResponse,
)
async def RetireLivePlaybackSessionAPI(
    retire_request: Annotated[schemas.LivePlaybackSessionRetireRequest, Body()],
    display_channel_id: Annotated[str, Depends(ValidateChannelID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
) -> schemas.LivePlaybackSessionRetireResponse:
    """
    指定UUIDのRetireとPrepare finalizeを一体taskへ合流し、request切断後も完了させる。

    Args:
        retire_request (Annotated[schemas.LivePlaybackSessionRetireRequest, Body()]): 終了するpipeline UUID。
        display_channel_id (Annotated[str, Depends(ValidateChannelID)]): 対象チャンネルの表示用ID。
        stream_quality (Annotated[StreamQualityWithOptions, Depends(ValidateQuality)]): codec option解決済みの画質。

    Returns:
        schemas.LivePlaybackSessionRetireResponse: session・encoder・lease cleanupの確定結果。
    """

    live_stream = LiveStream(
        display_channel_id,
        stream_quality.quality,
        stream_quality.encoding_options,
    )
    session_id = str(retire_request.playback_session_id)

    async def retire_operation() -> schemas.LivePlaybackSessionRetireResponse:
        """
        UUID単位で共有する物理Retire・Prepare finalize処理を実行する。

        Returns:
            schemas.LivePlaybackSessionRetireResponse: cleanupの確定結果。
        """

        return await _RetireLivePlaybackSession(
            retire_request,
            display_channel_id,
            stream_quality,
        )

    retire_task = live_stream.schedulePlaybackSessionRetireAPI(
        session_id,
        retire_operation,
    )

    # HTTP waiterだけを有界化する。一体settlement taskはcancelせず、retryが同じtaskへ合流する。
    done, _pending = await asyncio.wait(
        {retire_task},
        timeout = live_stream.PLAYBACK_SESSION_CLEANUP_TIMEOUT_SECONDS,
    )
    if len(done) == 0:
        return schemas.LivePlaybackSessionRetireResponse(
            accepted = False,
            result = 'cleanup_timeout',
            reason = 'playback_session_cleanup_timeout',
            cleanup_confirmed = False,
            terminal_restart_required = True,
        )

    # HTTP request taskだけがcancelされても、一体cleanup taskは継続する。
    return await asyncio.shield(retire_task)


@router.get(
    '/{display_channel_id}/{quality}/events',
    summary = 'ライブストリーム イベント API',
    response_class = Response,
    responses = {
        status.HTTP_200_OK: {
            'description': 'ライブストリームのイベントが随時配信されるイベントストリーム。',
            'content': {'text/event-stream': {}},
        }
    }
)
async def LiveStreamEventAPI(
    request: Request,
    display_channel_id: Annotated[str, Depends(ValidateChannelID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
):
    """
    ライブストリームのイベントを Server-Sent Events で随時配信する。

    イベントには、
    - 初回接続時に現在のステータスを示す **initial_update**
    - ステータスの更新を示す **status_update**
    - ステータス詳細の更新を示す **detail_update**
    - クライアント数の更新を示す **clients_update**
    の4種類がある。

    どのイベントでも配信される JSON 構造は同じ。<br>
    ステータスが Offline になった、あるいは既にそうなっている時は、status_update イベントが配信された後に接続を終了する。

    Args:
        request (Request): 処理対象の HTTP request。
        display_channel_id (Annotated[str, Depends(ValidateChannelID)]): 対象チャンネルの表示用 ID。
        stream_quality (Annotated[StreamQualityWithOptions, Depends(ValidateQuality)]): codec option を解決済みのライブ画質。

    Returns:
        None
    """

    # 品質とオプション指定に対応する LiveStream を取得する
    # ステータスを取得したいだけなので、接続はしない
    request_state = request.scope.get('state', {})
    stream_anchor_enabled = request_state.get('stream_anchor_enabled', True)
    live_stream = (
        LiveStream(display_channel_id, stream_quality.quality, stream_quality.encoding_options)
        if stream_anchor_enabled is True
        else LiveStream(display_channel_id, stream_quality.quality, stream_quality.encoding_options, False)
    )

    def serialize_status(live_stream_status: LiveStreamStatus) -> str:
        """
        互換APIでは従来5フィールドだけを返す。

        Args:
            live_stream_status (LiveStreamStatus): 直列化または比較対象のライブ状態。

        Returns:
            str: 処理結果の文字列。
        """

        if stream_anchor_enabled is True:
            return live_stream_status.model_dump_json()
        return live_stream_status.model_dump_json(include={
            'status',
            'detail',
            'started_at',
            'updated_at',
            'client_count',
        })

    def legacy_signature(live_stream_status: LiveStreamStatus) -> tuple[object, ...]:
        """
        互換APIが従来から比較していた5フィールドだけを返す。

        Args:
            live_stream_status (LiveStreamStatus): 直列化または比較対象のライブ状態。

        Returns:
            tuple[object, ...]: 互換 SSE の変更検出に使う状態値。
        """

        return (
            live_stream_status.status,
            live_stream_status.detail,
            live_stream_status.started_at,
            live_stream_status.updated_at,
            live_stream_status.client_count,
        )

    # ステータスの変更を監視し、変更があればステータスをイベントストリームとして出力する
    async def generator():
        """
        イベントストリームを出力するジェネレーター

        Args:
            None

        Returns:
            None
        """

        # 終了済み target へ新しく監視接続した場合は、前 encoder 世代の stale age / Anchor /
        # failure state を initial_update に持ち越さない。進行中の Prepare だけは維持する。
        initial_status = live_stream.getStatus()
        if (
            initial_status.status == 'Offline' and
            initial_status.prepare_state not in ('Prepared', 'Consumed')
        ):
            live_stream.getTelemetry().reset()

        # 初期値
        previous_status = live_stream.getStatus()

        # 取得できたクライアント数はあくまで同じチャンネル+同じ画質で視聴中のクライアントをカウントしたものなので、
        # 同じチャンネル+すべての画質で視聴中のクライアント数を別途取得して上書きする
        previous_status.client_count = LiveStream.getViewerCount(display_channel_id)

        # 初回接続時に必ず現在のステータスを返す
        yield {
            'event': 'initial_update',  # initial_update イベントを設定
            'data': serialize_status(previous_status),
        }

        while True:

            # 現在のライブストリームのステータスを取得
            status = live_stream.getStatus()

            # 取得できたクライアント数はあくまで同じチャンネル+同じ画質で視聴中のクライアントをカウントしたものなので、
            # 同じチャンネル+すべての画質で視聴中のクライアント数を別途取得して上書きする
            status.client_count = LiveStream.getViewerCount(display_channel_id)

            # 以前の結果と異なっている場合のみレスポンスを返す
            status_changed = (
                previous_status != status
                if stream_anchor_enabled is True
                else legacy_signature(previous_status) != legacy_signature(status)
            )
            if status_changed is True:

                # ステータスが以前と異なる
                if previous_status.status != status.status:
                    yield {
                        'event': 'status_update',  # status_update イベントを設定
                        'data': serialize_status(status),
                    }
                # 詳細が以前と異なる
                elif previous_status.detail != status.detail:
                    yield {
                        'event': 'detail_update',  # detail_update イベントを設定
                        'data': serialize_status(status),
                    }
                # クライアント数が以前と異なる
                elif previous_status.client_count != status.client_count:
                    yield {
                        'event': 'clients_update',  # clients_update イベントを設定
                        'data': serialize_status(status),
                    }
                # Prepare/起動時間/出力age/Anchor のいずれかが以前と異なる
                elif stream_anchor_enabled is True:
                    yield {
                        'event': 'telemetry_update',
                        'data': serialize_status(status),
                    }

                # 取得結果を保存
                previous_status = copy.copy(status)

            # 一応スリープを入れておく
            await asyncio.sleep(0.05)

    # EventSourceResponse でイベントストリームを配信する
    return EventSourceResponse(generator())


# ***** ライブ PSI/SI アーカイブデータストリーミング API *****


@router.get(
    '/{display_channel_id}/{quality}/psi-archived-data',
    summary = 'ライブ PSI/SI アーカイブデータストリーミング API',
    response_class = Response,
    responses = {
        status.HTTP_200_OK: {
            'description': 'ライブ PSI/SI アーカイブデータストリーム。',
            'content': {'application/octet-stream': {}},
        }
    }
)
async def LivePSIArchivedDataAPI(
    request: Request,
    display_channel_id: Annotated[str, Depends(ValidateChannelID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
):
    """
    ライブ PSI/SI アーカイブデータストリームを配信する。

    何らかの理由でライブストリームが終了しない限り、継続的にレスポンスが出力される（ストリーミング）。
    """

    # 品質とオプション指定に対応する LiveStream を取得する
    # PSI/SI アーカイブデータを取得したいだけなので、接続はしない
    live_stream = LiveStream(display_channel_id, stream_quality.quality, stream_quality.encoding_options)

    # LivePSIDataArchiver がまだ初期化されていない場合は、起動するまで最大10秒待つ
    ## LivePSIDataArchiver は LiveEncodingTask が起動次第自動的に初期化されるので、ここでは待つだけ
    for _ in range(20):
        if live_stream.psi_data_archiver is not None:
            break
        await asyncio.sleep(0.5)

    # 10秒待っても起動しなかった場合はエラー
    if live_stream.psi_data_archiver is None:
        logging.error(f'{live_stream.log_prefix} PSI/SI Data Archiver is not running.')
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail = 'PSI/SI Data Archiver is not running',
        )

    # StreamingResponse で読み取ったストリームデータをストリーミングする
    # LivePSIDataArchiver.getPSIArchivedData() は AsyncGenerator なので、そのまま渡せる
    response = StreamingResponse(live_stream.psi_data_archiver.getPSIArchivedData(request), media_type='application/octet-stream')

    # HTTP リクエストがキャンセルされたときに psisiarc を終了できるよう、StreamingResponse のインスタンスにモンキーパッチを当てる
    # モンキーパッチしている理由は LiveMPEGTSStreamAPI と同じ
    # ref: https://github.com/encode/starlette/pull/839
    async def listen_for_disconnect_monkeypatch(receive: Receive) -> None:
        try:
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    # 上のループで HTTP リクエストの切断を検知できるようにしばらく待つ
                    await asyncio.sleep(5)
                    break
        except asyncio.CancelledError:
            pass
    response.listen_for_disconnect = listen_for_disconnect_monkeypatch

    return response


# ***** MPEG-TS ストリーミング API *****


@router.get(
    '/{display_channel_id}/{quality}/mpegts',
    summary = 'ライブ MPEG-TS ストリーム API',
    response_class = Response,
    responses = {
        status.HTTP_200_OK: {
            'description': 'ライブ MPEG-TS ストリーム。',
            'content': {'video/mp2t': {}},
        }
    }
)
async def LiveMPEGTSStreamAPI(
    request: Request,
    display_channel_id: Annotated[str, Depends(ValidateChannelID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
):
    """
    ライブ MPEG-TS ストリームを配信する。

    同じチャンネル ID 、同じ画質のライブストリームが Offline 状態のときは、新たにエンコードタスクを立ち上げて、
    ONAir 状態になるのを待機してからストリームデータを配信する。<br>
    同じチャンネル ID 、同じ画質のライブストリームが ONAir や Idling 状態のときは、新たにエンコードタスクを立ち上げることなく、他のクライアントとストリームデータを共有して配信する。

    何らかの理由でライブストリームが終了しない限り、継続的にレスポンスが出力される（ストリーミング）。

    Args:
        request (Request): 処理対象の HTTP request。
        display_channel_id (Annotated[str, Depends(ValidateChannelID)]): 対象チャンネルの表示用 ID。
        stream_quality (Annotated[StreamQualityWithOptions, Depends(ValidateQuality)]): codec option を解決済みのライブ画質。

    Returns:
        None
    """

    # 品質とオプション指定に対応する LiveStream に接続し、ライブストリームクライアントを取得する
    ## 接続時に Offline だった場合は自動的にエンコードタスクが起動される
    request_state = request.scope.get('state', {})
    stream_anchor_enabled = request_state.get('stream_anchor_enabled', True)
    live_stream = (
        LiveStream(display_channel_id, stream_quality.quality, stream_quality.encoding_options)
        if stream_anchor_enabled is True
        else LiveStream(display_channel_id, stream_quality.quality, stream_quality.encoding_options, False)
    )
    prepare_token = (
        request.headers.get('X-KonomiTV-Prepare-Token')
        if stream_anchor_enabled is True
        else None
    )
    playback_session_header = (
        request.headers.get('X-KonomiTV-Playback-Session-Id')
        if stream_anchor_enabled is True
        else None
    )
    playback_session_id: str | None = None
    if playback_session_header is not None:
        try:
            playback_session_id = str(UUID(playback_session_header))
        except ValueError as ex:
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = {
                    'code': 'playback_session_invalid',
                    'message': 'The playback session ID must be a UUID.',
                },
            ) from ex
    try:
        live_stream_client = await live_stream.connect(
            'mpegts',
            prepare_token = prepare_token,
            prepare_backend = (
                await _ResolvePrepareBackend(display_channel_id)
                if prepare_token is not None
                else None
            ),
            playback_session_id = playback_session_id,
        )
    except LivePrepareLeaseError as ex:
        raise HTTPException(
            status_code = status.HTTP_409_CONFLICT,
            detail = {
                'code': str(ex),
                'message': 'The Prepare lease could not be consumed.',
            },
        ) from ex

    # ライブストリームを出力するジェネレーター
    async def generator():
        while True:

            # リクエストがキャンセル（切断）されている場合
            ## エンコードに失敗とかしない限り基本エンドレスで配信されるので、
            ## チャンネル変えたりやタブの再読み込みで必然的にリクエストがキャンセルされる
            if await request.is_disconnected():

                # ライブストリームへの接続を切断し、ループを終了する
                logging.debug(f'{live_stream.log_prefix} Request is disconnected.')
                live_stream.disconnect(live_stream_client)
                break

            if live_stream.getStatus().status != 'Offline':

                # クライアントが持つ Queue から読み取ったストリームデータ
                stream_data: bytes | None = await live_stream_client.readStreamData()

                # 読み取ったストリームデータを yield で随時出力する
                if stream_data is not None:
                    yield stream_data

                # stream_data に None が入った場合はエンコードタスクが終了し、接続が切断されたものとみなす
                else:

                    # ライブストリームへの接続を切断し、ループを終了する
                    logging.debug(f'{live_stream.log_prefix} Encode task is finished.')
                    live_stream.disconnect(live_stream_client)  # 必要ないとは思うけど念のため
                    break

            # ライブストリームが Offline になった場合もエンコードタスクが終了し、接続が切断されたものとみなす
            else:

                # ライブストリームへの接続を切断し、ループを終了する
                logging.debug(f'{live_stream.log_prefix} LiveStream is currently Offline.')
                live_stream.disconnect(live_stream_client)  # 必要ないとは思うけど念のため
                break

    # StreamingResponse で読み取ったストリームデータをストリーミングする
    response = StreamingResponse(generator(), media_type='video/mp2t')

    # HTTP リクエストがキャンセルされたときに自前でライブストリームの接続を切断できるよう、StreamingResponse のインスタンスにモンキーパッチを当てる
    ## Starlette の StreamingResponse は stream_response() と listen_for_disconnect() を TaskGroup で並行実行し、
    ## listen_for_disconnect() が完了すると cancel_scope.cancel() で stream_response() (ジェネレーター) を強制終了する
    ## デフォルトの listen_for_disconnect() は http.disconnect を受け取ると即座に完了するため、
    ## ジェネレーターが強制終了されて disconnect() が呼ばれず、client_count が減少しない問題があった
    ## これを避けるため listen_for_disconnect() を書き換え、http.disconnect の受信時点で即座に LiveStream.disconnect() を呼び出す
    ## LiveStream.disconnect() は二重呼び出しに安全なので、ジェネレーター側で重複して呼ばれても問題ない
    # ref: https://github.com/encode/starlette/pull/839
    async def listen_for_disconnect_monkeypatch(receive: Receive) -> None:
        try:
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    # HTTP リクエストの切断を検知したら即座にライブストリームへの接続を切断する
                    ## こうすることで client_count が即座に減少し、チューナー再利用の判定が高速化される
                    logging.debug(f'{live_stream.log_prefix} Request is disconnected.')
                    live_stream.disconnect(live_stream_client)
                    break
        except asyncio.CancelledError:
            pass
    response.listen_for_disconnect = listen_for_disconnect_monkeypatch

    return response
