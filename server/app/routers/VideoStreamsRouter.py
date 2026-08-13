
import asyncio
import json
import math
import uuid
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask

from app import logging, schemas
from app.config import Config
from app.metadata.RecordedPlaybackIndex import IsRecordedPlaybackIndexReady
from app.metadata.RecordedPlaybackIndexer import RecordedPlaybackIndexer
from app.models.RecordedProgram import RecordedProgram
from app.streams.KonomiTVBS4KOfflineStream import KonomiTVBS4KOfflineStream
from app.streams.KonomiTVBS4KPlaybackCapabilities import (
    KonomiTVBS4KPlaybackCapabilityProbe,
)
from app.streams.KonomiTVBS4KPlaybackEncoding import (
    KonomiTVBS4KAudioCodec,
    KonomiTVBS4KPlaybackEncoder,
    KonomiTVBS4KPlaybackMode,
    KonomiTVBS4KVideoBitDepth,
    KonomiTVBS4KVideoBitDepthQuery,
    KonomiTVBS4KVideoCodec,
)
from app.streams.RecordedFMP4Stream import RecordedFMP4Stream
from app.streams.RecordedSubtitleStream import RecordedSubtitleStream
from app.streams.StreamEncodingOptions import (
    SplitQualityAndEncodingOptions,
    StreamQualityWithOptions,
)
from app.streams.TSCodecBridgeRuntime import TSCodecBridgeRuntimeVerifier


# ルーター
router = APIRouter(
    tags = ['Streams'],
    prefix = '/api/streams/video',
)

VideoBitDepthQuery = KonomiTVBS4KVideoBitDepthQuery

# 1要求が録画全体を生成するため、upstream と同じくオフライン保存はサーバー全体で3件に制限する。
OFFLINE_STREAM_SEMAPHORE = asyncio.Semaphore(3)


def SetTSCodecBridgeProcessCounterHeaders(response: Response) -> None:
    """互換API隔離の前後で比較するBridge process世代と用途別回数を設定する。"""

    if Config().general.konomitv_bs4k_acceptance_diagnostics_enabled is False:
        return
    generation, total_count, live_count, probe_count, verification_count = (
        TSCodecBridgeRuntimeVerifier.getProcessStartSnapshot()
    )
    prefix = 'X-KonomiTV-BS4K-TSCodecBridge'
    response.headers[f'{prefix}-Process-Generation'] = generation
    response.headers[f'{prefix}-Process-Start-Count'] = str(total_count)
    response.headers[f'{prefix}-Live-Process-Start-Count'] = str(live_count)
    response.headers[f'{prefix}-Probe-Process-Start-Count'] = str(probe_count)
    response.headers[f'{prefix}-Verification-Process-Start-Count'] = str(
        verification_count
    )


def GetRecordedStream(
    session_id: str,
    recorded_program: RecordedProgram,
    stream_quality: StreamQualityWithOptions,
    is_new_session_allowed: bool = False,
    client_key: str = 'unknown',
) -> RecordedFMP4Stream:
    """FFmpeg 8・fMP4録画視聴セッションを返す。"""

    if RecordedFMP4Stream.hasSession(session_id):
        return RecordedFMP4Stream(
            session_id,
            recorded_program,
            stream_quality.quality,
            encoding_options=stream_quality.encoding_options,
            is_new_session_allowed=False,
            client_key=client_key,
        )
    if IsRecordedPlaybackIndexReady(
        recorded_program.recorded_video.playback_index_status,
        recorded_program.recorded_video.playback_index_version,
    ) is False:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                'code': 'PlaybackIndexUnavailable',
                'message': 'The recorded playback index is unavailable.',
            },
        )
    return RecordedFMP4Stream(
        session_id,
        recorded_program,
        stream_quality.quality,
        stream_quality.encoding_options,
        is_new_session_allowed=is_new_session_allowed,
        client_key=client_key,
    )


def GetClientKey(request: Request) -> str:
    """
    admission control 用の接続元識別子を取得する。

    Args:
        request (Request): FastAPI リクエスト

    Returns:
        str: クライアント IP または unknown
    """

    if request.client is None:
        return 'unknown'
    return request.client.host or 'unknown'


async def EnsurePlaybackIndexReady(recorded_program: RecordedProgram, session_id: str) -> None:
    """新規録画視聴セッションに必要な再生索引を最優先で生成する。

    Args:
        recorded_program: 再生対象の録画番組。
        session_id: マスタープレイリスト要求が作成する視聴セッションID。

    Returns:
        None
    """

    # 既存セッションの後続要求と、索引生成済みの録画は待機不要。
    if (
        RecordedFMP4Stream.hasSession(session_id) or
        IsRecordedPlaybackIndexReady(
            recorded_program.recorded_video.playback_index_status,
            recorded_program.recorded_video.playback_index_version,
        )
    ):
        return

    # 旧MPEG-TS経路の削除後もPending録画を再生できるよう、再生要求を最優先で解析する。
    # 同じ録画への複数要求はIndexer側の共有Futureへ合流する。クライアント切断で
    # 共有Futureまでキャンセルされないようshieldし、完了後は依存解決時の古いDB値を更新する。
    indexed = await asyncio.shield(RecordedPlaybackIndexer.enqueue(recorded_program.recorded_video.id, priority=0))
    await recorded_program.recorded_video.refresh_from_db()
    if indexed is False or IsRecordedPlaybackIndexReady(
        recorded_program.recorded_video.playback_index_status,
        recorded_program.recorded_video.playback_index_version,
    ) is False:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                'code': recorded_program.recorded_video.playback_index_error_code or 'PlaybackIndexUnavailable',
                'message': 'The recorded playback index is unavailable.',
            },
        )


@router.get(
    '/konomitv-bs4k-playback-capabilities',
    summary = 'KonomiTV-BS4K 共通再生エンコード能力 API',
    response_model = schemas.KonomiTVBS4KPlaybackCapabilities,
)
async def KonomiTVBS4KPlaybackCapabilitiesAPI(
    response: Response,
) -> schemas.KonomiTVBS4KPlaybackCapabilities:
    """ライブと録画の映像・音声能力と、ライブの厳密な組み合わせ行列を返す。"""

    capabilities = await KonomiTVBS4KPlaybackCapabilityProbe.getCapabilities()
    SetTSCodecBridgeProcessCounterHeaders(response)
    return schemas.KonomiTVBS4KPlaybackCapabilities(
        video = [
            schemas.KonomiTVBS4KPlaybackVideoCapability(
                encoder = capability.encoder,
                codec = capability.codec,
                bit_depth = capability.bit_depth,
                profile = capability.profile,
                live_available = capability.live_available,
                recorded_available = capability.recorded_available,
                live_reason_code = capability.live_reason_code,
                recorded_reason_code = capability.recorded_reason_code,
            )
            for capability in capabilities.video
        ],
        audio = [
            schemas.KonomiTVBS4KPlaybackAudioCapability(
                codec = capability.codec,
                live_available = capability.live_available,
                recorded_available = capability.recorded_available,
                live_reason_code = capability.live_reason_code,
                recorded_reason_code = capability.recorded_reason_code,
            )
            for capability in capabilities.audio
        ],
        live_combinations = [
            schemas.KonomiTVBS4KPlaybackLiveCombinationCapability(
                encoder = capability.encoder,
                video_codec = capability.video_codec,
                video_bit_depth = capability.video_bit_depth,
                audio_codec = capability.audio_codec,
                available = capability.available,
                reason_code = capability.reason_code,
            )
            for capability in capabilities.live_combinations
        ],
    )


@router.get(
    '/konomitv-bs4k-playback-capabilities/targeted',
    summary = 'KonomiTV-BS4K 再生開始用部分エンコード能力 API',
    response_model = schemas.KonomiTVBS4KPlaybackCapabilities,
)
async def KonomiTVBS4KTargetedPlaybackCapabilitiesAPI(
    response: Response,
    encoder: Annotated[
        KonomiTVBS4KPlaybackEncoder,
        Query(description='現在の再生で実際に使用するエンコーダー。'),
    ],
    playback_mode: Annotated[
        KonomiTVBS4KPlaybackMode,
        Query(description='ライブまたは録画のどちらの能力を部分検査するか。'),
    ],
    video_codec: Annotated[
        KonomiTVBS4KVideoCodec,
        Query(description='保存設定から選ばれた出力映像コーデック。'),
    ],
    video_bit_depths: Annotated[
        str,
        Query(
            pattern = r'^(8|10)(,(8|10))?$',
            description='現在の画質とブラウザで候補になるbit depthの優先順。',
        ),
    ],
    audio_codec: Annotated[
        KonomiTVBS4KAudioCodec,
        Query(description='保存設定から選ばれた出力音声コーデック。'),
    ],
    has_video: Annotated[
        bool,
        Query(description='映像SourceBufferを使う再生対象かどうか。'),
    ],
) -> schemas.KonomiTVBS4KPlaybackCapabilities:
    """再生開始に必要なexact行とAVC/AAC互換fallback行だけを返す。"""

    parsed_video_bit_depths = cast(
        tuple[KonomiTVBS4KVideoBitDepth, ...],
        tuple(int(value) for value in video_bit_depths.split(',')),
    )
    capabilities = await KonomiTVBS4KPlaybackCapabilityProbe.getTargetedCapabilities(
        encoder,
        playback_mode,
        video_codec,
        parsed_video_bit_depths,
        audio_codec,
        has_video,
    )
    SetTSCodecBridgeProcessCounterHeaders(response)
    return schemas.KonomiTVBS4KPlaybackCapabilities(
        video = [
            schemas.KonomiTVBS4KPlaybackVideoCapability(
                encoder = capability.encoder,
                codec = capability.codec,
                bit_depth = capability.bit_depth,
                profile = capability.profile,
                live_available = capability.live_available,
                recorded_available = capability.recorded_available,
                live_reason_code = capability.live_reason_code,
                recorded_reason_code = capability.recorded_reason_code,
            )
            for capability in capabilities.video
        ],
        audio = [
            schemas.KonomiTVBS4KPlaybackAudioCapability(
                codec = capability.codec,
                live_available = capability.live_available,
                recorded_available = capability.recorded_available,
                live_reason_code = capability.live_reason_code,
                recorded_reason_code = capability.recorded_reason_code,
            )
            for capability in capabilities.audio
        ],
        live_combinations = [
            schemas.KonomiTVBS4KPlaybackLiveCombinationCapability(
                encoder = capability.encoder,
                video_codec = capability.video_codec,
                video_bit_depth = capability.video_bit_depth,
                audio_codec = capability.audio_codec,
                available = capability.available,
                reason_code = capability.reason_code,
            )
            for capability in capabilities.live_combinations
        ],
    )


async def ValidateVideoID(video_id: Annotated[int, Path(description='録画番組の ID 。')]) -> RecordedProgram:
    """ 録画番組 ID のバリデーション """

    # 指定された video_id が存在するか確認
    recorded_program = await RecordedProgram.filter(id=video_id).get_or_none() \
        .select_related('recorded_video') \
        .select_related('channel')
    if recorded_program is None:
        logging.error(f'[VideoStreamsRouter][ValidateVideoID] Specified video_id was not found. [video_id: {video_id}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified video_id was not found',
        )

    return recorded_program


async def ValidateRecordedPlaybackCapabilities(
    recorded_program: RecordedProgram,
    stream_quality: StreamQualityWithOptions,
) -> None:
    """
    明示codec queryに対応する録画エンコード能力を、現在の録画メタデータで検証する。

    Args:
        recorded_program (RecordedProgram): 再生対象の録画番組。
        stream_quality (StreamQualityWithOptions): queryの明示状態を保持した生成条件。

    Returns:
        None
    """

    if (
        stream_quality.is_video_encoding_explicitly_requested is False
        and stream_quality.is_audio_encoding_explicitly_requested is False
    ):
        return

    is_bs4k_recorded_video = recorded_program.network_id == 0x000B
    selected_encoder = (
        Config().general.encoder_bs4k
        if is_bs4k_recorded_video is True
        else Config().general.encoder
    )
    if (
        recorded_program.recorded_video.has_video is True
        and stream_quality.is_video_encoding_explicitly_requested is True
    ):
        video_capability = (
            await KonomiTVBS4KPlaybackCapabilityProbe.getRecordedVideoCapability(
                selected_encoder,
                stream_quality.encoding_options.video_codec,
                stream_quality.encoding_options.video_bit_depth,
            )
        )
        if video_capability.recorded_available is False:
            reason_code = video_capability.recorded_reason_code or 'ProbeFailed'
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = {
                    'code': reason_code,
                    'message': 'The requested recorded encoding is unavailable.',
                },
            )

    if stream_quality.is_audio_encoding_explicitly_requested is True:
        # 録画Opusは固定FFmpegの可否だけで判定し、無関係なlive radio pipeを起動しない。
        audio_capability = (
            await KonomiTVBS4KPlaybackCapabilityProbe.getRecordedAudioCapability(
                stream_quality.encoding_options.audio_codec
            )
        )
        if audio_capability.recorded_available is False:
            reason_code = audio_capability.recorded_reason_code or 'ProbeFailed'
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = {
                    'code': reason_code,
                    'message': 'The requested recorded audio encoding is unavailable.',
                },
            )


async def ValidateQuality(
    quality: Annotated[str, Path(description='映像の品質。ex: 1080p')],
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    video_codec: Annotated[KonomiTVBS4KVideoCodec | None, Query(description='出力映像コーデック。省略時は旧画質URLから判定。')] = None,
    video_bit_depth: Annotated[VideoBitDepthQuery | None, Query(description='出力映像bit depth。')] = None,
    audio_codec: Annotated[KonomiTVBS4KAudioCodec, Query(description='出力音声コーデック。')] = 'aac',
    audio_track: Annotated[str | None, Query(description='映像と多重化する音声レンディション ID。')] = None,
) -> StreamQualityWithOptions:
    """ 映像の品質のバリデーション """

    # 指定された品質が存在するか確認
    ## 品質の指定に -10bit や -24fps が付いていれば分解する
    is_bs4k_recorded_video = recorded_program.network_id == 0x000B
    stream_quality = SplitQualityAndEncodingOptions(
        quality,
        Config().general.encoder_bs4k if is_bs4k_recorded_video is True else None,
        is_24fps_mode_allowed = is_bs4k_recorded_video is False,
        video_codec = video_codec,
        video_bit_depth = cast(
            KonomiTVBS4KVideoBitDepth | None,
            int(video_bit_depth) if video_bit_depth is not None else None,
        ),
        audio_codec = audio_codec,
        audio_rendition_id = audio_track,
    )
    if stream_quality is None:
        logging.error(f'[VideoStreamsRouter][ValidateQuality] Specified quality was not found. [quality: {quality}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified quality was not found',
        )

    # Ready済み録画は依存解決時に早期拒否する。Pending/Staleはmaster handlerが
    # index生成後の最新メタデータで同じhelperを必ず再実行する。
    if IsRecordedPlaybackIndexReady(
        recorded_program.recorded_video.playback_index_status,
        recorded_program.recorded_video.playback_index_version,
    ):
        await ValidateRecordedPlaybackCapabilities(recorded_program, stream_quality)

    return stream_quality


async def CreateOfflineRecordedStream(
    request: Request,
    recorded_program: RecordedProgram,
    stream_quality: StreamQualityWithOptions,
) -> RecordedFMP4Stream:
    """能力検査済みの独立したオフライン保存セッションを作成する。

    Args:
        request: 接続元ごとの admission control に使う HTTP リクエスト。
        recorded_program: 保存対象の録画番組。
        stream_quality: 画質・映像 codec・bit depth・音声 codec の生成条件。

    Returns:
        RecordedFMP4Stream: 通常再生キャッシュを共有する保存専用セッション。
    """

    session_id = f'offline-{uuid.uuid4().hex}'
    await EnsurePlaybackIndexReady(recorded_program, session_id)
    await ValidateRecordedPlaybackCapabilities(recorded_program, stream_quality)
    return GetRecordedStream(
        session_id,
        recorded_program,
        stream_quality,
        is_new_session_allowed = True,
        client_key = GetClientKey(request),
    )


def BuildOfflineStreamEstimate(
    recorded_program: RecordedProgram,
    stream_quality: StreamQualityWithOptions,
) -> schemas.KonomiTVBS4KOfflineStreamEstimate:
    """録画メタデータ・生成条件・全音声レンディション数から保存容量を見積もる。

    Args:
        recorded_program: 再生索引で音声構成まで確定済みの録画番組。
        stream_quality: 能力検査済みの画質・映像・音声生成条件。

    Returns:
        schemas.KonomiTVBS4KOfflineStreamEstimate: 進捗表示用と空き容量判定用の概算値。
    """

    duration = recorded_program.recorded_video.duration
    video_bitrate = 0
    video_bitrate_max = 0
    if recorded_program.recorded_video.has_video is True:
        bitrate = RecordedFMP4Stream.getVideoBitrate(
            stream_quality.quality,
            stream_quality.encoding_options.video_codec,
        )
        video_bitrate = int(bitrate.video_bitrate.removesuffix('K'))
        video_bitrate_max = int(bitrate.video_bitrate_max.removesuffix('K'))

    # AAC は実装上192K固定、Opusは1～8chの最大320Kを使い、全レンディション保存を安全側で見積もる。
    effective_audio_codec = RecordedFMP4Stream.resolveEffectiveAudioCodec(
        recorded_program,
        stream_quality.encoding_options.audio_codec,
    )
    audio_bitrate_per_rendition = 320 if effective_audio_codec == 'opus' else 192
    audio_bitrate = audio_bitrate_per_rendition * len(
        RecordedFMP4Stream.getAudioRenditionsForProgram(recorded_program)
    )
    estimated_size_bytes = math.ceil((video_bitrate + audio_bitrate) * 1000 * duration / 8 * 1.05)
    required_size_bytes = math.ceil((video_bitrate_max + audio_bitrate) * 1000 * duration / 8 * 1.10)
    return schemas.KonomiTVBS4KOfflineStreamEstimate(
        estimated_size_bytes = estimated_size_bytes,
        required_size_bytes = required_size_bytes,
    )


@router.get(
    '/{video_id}/{quality}/offline-estimate',
    summary = 'KonomiTV-BS4K 録画番組オフライン保存容量見積もり API',
    response_model = schemas.KonomiTVBS4KOfflineStreamEstimate,
)
async def KonomiTVBS4KOfflineStreamEstimateAPI(
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
) -> schemas.KonomiTVBS4KOfflineStreamEstimate:
    """全音声レンディションを含む fMP4 オフライン保存容量を返す。"""

    if recorded_program.recorded_video.status == 'Recording':
        raise HTTPException(
            status_code = status.HTTP_409_CONFLICT,
            detail = 'Recording video cannot be saved for offline playback',
        )
    # 見積もりにも本保存と同じ索引・能力契約を適用するが、セッション登録や全セグメント計画は行わない。
    estimate_request_id = f'offline-estimate-{uuid.uuid4().hex}'
    await EnsurePlaybackIndexReady(recorded_program, estimate_request_id)
    await ValidateRecordedPlaybackCapabilities(recorded_program, stream_quality)
    return BuildOfflineStreamEstimate(recorded_program, stream_quality)


@router.get(
    '/{video_id}/{quality}/offline-stream',
    summary = 'KonomiTV-BS4K 録画番組オフライン保存ストリーム API',
    response_class = StreamingResponse,
    responses = {
        status.HTTP_200_OK: {
            'description': '保存用プレイリスト・init・fMP4 fragmentを格納したバイナリストリーム。',
            'content': {'application/octet-stream': {}},
        },
        status.HTTP_409_CONFLICT: {
            'description': '録画中のためオフライン保存を開始できない。',
        },
    },
)
async def KonomiTVBS4KOfflineStreamAPI(
    request: Request,
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
    quality: Annotated[str, Path(description='映像の品質。ex: 720p-24fps')],
) -> StreamingResponse:
    """RecordedFMP4Stream の全再生資産を先頭から生成し、単一応答として返す。"""

    if recorded_program.recorded_video.status == 'Recording':
        logging.error(
            '[KonomiTVBS4KOfflineStreamAPI] Recording video cannot be saved for offline playback. '
            f'[video_id: {recorded_program.id}]'
        )
        raise HTTPException(
            status_code = status.HTTP_409_CONFLICT,
            detail = 'Recording video cannot be saved for offline playback',
        )

    # Waiting 表示を維持できるよう、実行枠を取得するまでHTTP応答を開始しない。
    await OFFLINE_STREAM_SEMAPHORE.acquire()
    video_stream: RecordedFMP4Stream | None = None
    try:
        video_stream = await CreateOfflineRecordedStream(request, recorded_program, stream_quality)
        metadata = schemas.KonomiTVBS4KOfflineStreamMetadata(
            video_id = recorded_program.id,
            file_hash = recorded_program.recorded_video.file_hash,
            quality = quality,
            video_codec = stream_quality.encoding_options.video_codec,
            video_bit_depth = stream_quality.encoding_options.video_bit_depth,
            requested_audio_codec = stream_quality.encoding_options.audio_codec,
            audio_codec = video_stream.effective_audio_codec,
        )
        offline_stream = KonomiTVBS4KOfflineStream(video_stream, metadata)
    except BaseException:
        if video_stream is not None:
            await video_stream.destroy()
        OFFLINE_STREAM_SEMAPHORE.release()
        raise

    is_cleaned_up = False

    async def CleanupOfflineStream() -> None:
        """保存セッションと全体実行枠を最初の1回だけ解放する。"""

        nonlocal is_cleaned_up
        if is_cleaned_up is True:
            return
        is_cleaned_up = True
        await video_stream.destroy()
        OFFLINE_STREAM_SEMAPHORE.release()

    async def GenerateOfflineStream():
        """応答の backpressure 待ち中もセッションを維持してバイナリ本体を生成する。"""

        async def KeepAlive() -> None:
            """長時間ダウンロード中のセッション破棄タイマーを延長する。"""

            while True:
                await asyncio.sleep(5)
                video_stream.keepAlive()

        keep_alive_task = asyncio.create_task(KeepAlive())
        try:
            async for chunk in offline_stream.Generate():
                yield chunk
        finally:
            keep_alive_task.cancel()
            await asyncio.gather(keep_alive_task, return_exceptions=True)
            await CleanupOfflineStream()

    return StreamingResponse(
        GenerateOfflineStream(),
        media_type = 'application/octet-stream',
        headers = {
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
        },
        background = BackgroundTask(CleanupOfflineStream),
    )


@router.get(
    '/{video_id}/{quality}/playlist',
    summary = '録画番組 HLS M3U8 プレイリスト API',
    response_class = Response,
    responses = {
        status.HTTP_200_OK: {
            'description': '録画番組の HLS M3U8 プレイリスト。',
            'content': {'application/vnd.apple.mpegurl': {}},
        }
    }
)
async def VideoHLSPlaylistAPI(
    request: Request,
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
    session_id: Annotated[str, Query(description='セッション ID（クライアント側で適宜生成したランダム値を指定する）。')],
    cache_key: Annotated[str | None, Query(description='キャッシュ制御用のキー。')] = None,
):
    """
    指定された画質に対応する、録画番組のストリーミング用 HLS M3U8 プレイリストを返す。<br>
    この M3U8 プレイリストは仮想的なもので、すべてのセグメントデータがエンコード済みとは限らない。セグメントはリクエストされ次第随時生成される。
    """

    # 旧録画経路の削除後は、未解析の録画もオンデマンド索引が完了し次第そのまま再生を開始する。
    await EnsurePlaybackIndexReady(recorded_program, session_id)

    # Pending/Staleだった要求も、index生成後のhas_videoと同じ明示query契約で必ず検査する。
    ## Ready要求ではcache済みのexact結果を再利用し、codec無指定の従来URLはhelper内で即時returnする。
    await ValidateRecordedPlaybackCapabilities(recorded_program, stream_quality)

    # 品質とオプション指定に対応する録画視聴セッションを作成または取得
    ## 新規 session は接続元 IP 単位の admission control を通す
    video_stream = GetRecordedStream(
        session_id,
        recorded_program,
        stream_quality,
        is_new_session_allowed=True,
        client_key=GetClientKey(request),
    )
    virtual_playlist = await video_stream.getMasterPlaylist(cache_key)
    return Response(
        content = virtual_playlist,
        media_type = 'application/vnd.apple.mpegurl',
        headers = {
            'Cache-Control': 'max-age=0',
        },
    )


@router.get('/{video_id}/{quality}/video/playlist', response_class=Response)
async def VideoHLSVideoPlaylistAPI(
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
    session_id: Annotated[str, Query()],
    cache_key: Annotated[str | None, Query()] = None,
):
    video_stream = GetRecordedStream(session_id, recorded_program, stream_quality)
    return Response(
        content=video_stream.getVideoPlaylist(cache_key),
        media_type='application/vnd.apple.mpegurl',
        headers={'Cache-Control': 'max-age=0'},
    )


@router.get('/{video_id}/{quality}/video/segment', response_class=Response)
async def VideoHLSVideoSegmentAPI(
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
    session_id: Annotated[str, Query()],
    sequence: Annotated[int, Query()],
    cache_key: Annotated[str | None, Query()] = None,
):
    video_stream = GetRecordedStream(session_id, recorded_program, stream_quality)
    segment_data = await video_stream.getVideoSegment(sequence)
    if segment_data is None:
        raise HTTPException(status_code=422, detail='Video segment was not found')
    return Response(content=segment_data, media_type='video/mp4', headers={'Cache-Control': 'max-age=10800'})


@router.get('/{video_id}/{quality}/video/init', response_class=Response)
async def VideoHLSVideoInitSegmentAPI(
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
    session_id: Annotated[str, Query()],
    generation: Annotated[int, Query()],
    sequence: Annotated[int, Query()] = 0,
    cache_key: Annotated[str | None, Query()] = None,
):
    """録画映像の初期化セグメントを返す。"""

    video_stream = GetRecordedStream(session_id, recorded_program, stream_quality)
    init_segment = await video_stream.getVideoInitSegment(generation, sequence)
    if init_segment is None:
        raise HTTPException(status_code=422, detail='Video initialization segment was not found')
    return Response(content=init_segment, media_type='video/mp4', headers={'Cache-Control': 'max-age=10800'})


@router.get('/{video_id}/{quality}/audio/{rendition_id}/playlist', response_class=Response)
async def VideoHLSAudioPlaylistAPI(
    rendition_id: str,
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
    session_id: Annotated[str, Query()],
    cache_key: Annotated[str | None, Query()] = None,
):
    video_stream = GetRecordedStream(session_id, recorded_program, stream_quality)
    return Response(
        content=video_stream.getAudioPlaylist(rendition_id, cache_key),
        media_type='application/vnd.apple.mpegurl',
        headers={'Cache-Control': 'max-age=0'},
    )


@router.get('/{video_id}/{quality}/audio/{rendition_id}/segment', response_class=Response)
async def VideoHLSAudioSegmentAPI(
    rendition_id: str,
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
    session_id: Annotated[str, Query()],
    sequence: Annotated[int, Query()],
    cache_key: Annotated[str | None, Query()] = None,
):
    video_stream = GetRecordedStream(session_id, recorded_program, stream_quality)
    segment_data = await video_stream.getAudioSegment(rendition_id, sequence)
    if segment_data is None:
        raise HTTPException(status_code=422, detail='Audio segment was not found')
    return Response(
        content=segment_data,
        media_type='audio/mp4',
        headers={'Cache-Control': 'max-age=10800'},
    )


@router.get('/{video_id}/{quality}/audio/{rendition_id}/init', response_class=Response)
async def VideoHLSAudioInitSegmentAPI(
    rendition_id: str,
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
    session_id: Annotated[str, Query()],
    sequence: Annotated[int, Query()] = 0,
    cache_key: Annotated[str | None, Query()] = None,
):
    video_stream = GetRecordedStream(session_id, recorded_program, stream_quality)
    init_segment = await video_stream.getAudioInitSegment(rendition_id, sequence)
    if init_segment is None:
        raise HTTPException(status_code=422, detail='Audio initialization segment was not found')
    return Response(content=init_segment, media_type='audio/mp4', headers={'Cache-Control': 'max-age=10800'})


@router.get('/{video_id}/{quality}/subtitle/{subtitle_index}/playlist', response_class=Response)
async def VideoHLSSubtitlePlaylistAPI(
    subtitle_index: int,
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
    session_id: Annotated[str, Query()],
    cache_key: Annotated[str | None, Query()] = None,
):
    video_stream = GetRecordedStream(session_id, recorded_program, stream_quality)
    subtitle_stream = RecordedSubtitleStream(recorded_program.recorded_video)
    if subtitle_stream.getTrackKind(subtitle_index) != 'Text':
        raise HTTPException(status_code=422, detail='This subtitle track cannot be converted to WebVTT')
    return Response(
        content=(
            '#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-PLAYLIST-TYPE:VOD\n'
            f'#EXT-X-TARGETDURATION:{math.ceil(recorded_program.recorded_video.duration)}\n'
            f'#EXTINF:{recorded_program.recorded_video.duration:.6f},\n'
            f'webvtt?session_id={session_id}&cache_key={cache_key or ""}&{video_stream.getCodecQuery()}\n'
            '#EXT-X-ENDLIST\n'
        ),
        media_type='application/vnd.apple.mpegurl',
        headers={'Cache-Control': 'max-age=0'},
    )


@router.get('/{video_id}/{quality}/subtitle/{subtitle_index}/segment', response_class=Response)
async def VideoHLSSubtitleSegmentAPI(
    subtitle_index: int,
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
    session_id: Annotated[str, Query()],
    cache_key: Annotated[str | None, Query()] = None,
):
    GetRecordedStream(session_id, recorded_program, stream_quality).keepAlive()
    segment_data = await RecordedSubtitleStream(recorded_program.recorded_video).getWebVTT(subtitle_index)
    if segment_data is None:
        raise HTTPException(status_code=422, detail='Subtitle conversion failed')
    return Response(content=segment_data, media_type='text/vtt', headers={'Cache-Control': 'max-age=10800'})


@router.get('/{video_id}/{quality}/subtitle/{subtitle_index}/webvtt', response_class=Response)
async def VideoSubtitleWebVTTAPI(
    subtitle_index: int,
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
    session_id: Annotated[str, Query()],
):
    """映像セッションと独立したWebVTT字幕を返す。"""

    GetRecordedStream(session_id, recorded_program, stream_quality).keepAlive()
    data = await RecordedSubtitleStream(recorded_program.recorded_video).getWebVTT(subtitle_index)
    if data is None:
        raise HTTPException(status_code=422, detail='Subtitle conversion failed or the track is disabled')
    return Response(content=data, media_type='text/vtt', headers={'Cache-Control': 'max-age=10800'})


@router.get('/{video_id}/{quality}/subtitle/{subtitle_index}/arib')
async def VideoSubtitleARIBAPI(
    subtitle_index: int,
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
    session_id: Annotated[str, Query()],
    start_time: Annotated[float, Query(ge=0)],
    end_time: Annotated[float, Query(gt=0)],
):
    """指定時間範囲のPTS付きARIB生字幕とシーク復元情報を返す。"""

    GetRecordedStream(session_id, recorded_program, stream_quality).keepAlive()
    result = await RecordedSubtitleStream(recorded_program.recorded_video).getARIBRange(
        subtitle_index,
        start_time,
        end_time,
    )
    if result is None:
        raise HTTPException(status_code=422, detail='ARIB subtitle track was not found')
    return result


@router.get('/{video_id}/subtitle/{subtitle_index}/webvtt', response_class=Response)
async def RecordedSubtitleWebVTTAPI(
    subtitle_index: int,
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
):
    """録画視聴セッションに依存しないWebVTT字幕を返す。"""

    data = await RecordedSubtitleStream(recorded_program.recorded_video).getWebVTT(subtitle_index)
    if data is None:
        raise HTTPException(status_code=422, detail='Subtitle conversion failed or the track is disabled')
    return Response(content=data, media_type='text/vtt', headers={'Cache-Control': 'max-age=10800'})


@router.get('/{video_id}/subtitle/{subtitle_index}/arib')
async def RecordedSubtitleARIBAPI(
    subtitle_index: int,
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    start_time: Annotated[float, Query(ge=0)],
    end_time: Annotated[float, Query(gt=0)],
):
    """録画視聴セッションに依存しないARIB字幕範囲を返す。"""

    result = await RecordedSubtitleStream(recorded_program.recorded_video).getARIBRange(
        subtitle_index,
        start_time,
        end_time,
    )
    if result is None:
        raise HTTPException(status_code=422, detail='ARIB subtitle track was not found')
    return result


@router.get('/{video_id}/subtitle/arib-ttml')
async def RecordedSubtitleARIBTTMLAPI(
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    start_time: Annotated[float, Query(ge=0)],
    end_time: Annotated[float, Query(gt=0)],
):
    """ライブと同じdecoderへ渡す録画ARIB-TTML timed ID3範囲を返す。"""

    result = await RecordedSubtitleStream(recorded_program.recorded_video).getARIBTTMLRange(
        start_time,
        end_time,
    )
    if result is None:
        raise HTTPException(status_code=422, detail='ARIB-TTML subtitle track was not found')
    return result


@router.get(
    '/{video_id}/{quality}/buffer',
    summary = '録画番組 HLS バッファ範囲 API',
    response_class = Response,
    responses = {
        status.HTTP_200_OK: {
            'description': '録画番組の HLS バッファ範囲が随時配信されるイベントストリーム。',
            'content': {'text/event-stream': {}},
        }
    }
)
async def VideoHLSBufferAPI(
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
    session_id: Annotated[str, Query(description='セッション ID（クライアント側で適宜生成したランダム値を指定する）。')],
):
    """
    録画番組の HLS バッファ範囲を Server-Sent Events で随時配信する。

    イベントには、
    - バッファ範囲の更新を示す **buffer_range_update**
    の1種類がある。

    どのイベントでも配信される JSON 構造は同じ。<br>
    エンコードタスクが終了した場合は、接続を終了する。
    """

    # 品質とオプション指定に対応する録画視聴セッションを取得
    video_stream = GetRecordedStream(session_id, recorded_program, stream_quality)

    # バッファ範囲の変更を監視し、変更があればバッファ範囲をイベントストリームとして出力する
    async def generator():
        """イベントストリームを出力するジェネレーター"""

        # 初期値
        previous_buffer_range = video_stream.getBufferRange()

        # 初回接続時に必ず現在のバッファ範囲を返す
        yield {
            'event': 'buffer_range_update',  # buffer_range_update イベントを設定
            'data': json.dumps({
                'begin': previous_buffer_range[0],
                'end': previous_buffer_range[1],
            }),
        }

        while True:

            # 現在のバッファ範囲を取得
            buffer_range = video_stream.getBufferRange()

            # 以前の結果と異なっている場合のみレスポンスを返す
            if previous_buffer_range != buffer_range:
                logging.info(f'{video_stream.log_prefix} Buffer range updated. [begin: {buffer_range[0]}, end: {buffer_range[1]}]')
                yield {
                    'event': 'buffer_range_update',  # buffer_range_update イベントを設定
                    'data': json.dumps({
                        'begin': buffer_range[0],
                        'end': buffer_range[1],
                    }),
                }

                # 取得結果を保存
                previous_buffer_range = buffer_range

            # ビジーにならないように、0.1秒ごとにチェックする
            await asyncio.sleep(0.1)

    # EventSourceResponse でイベントストリームを配信する
    return EventSourceResponse(generator())


@router.put(
    '/{video_id}/{quality}/keep-alive',
    summary = '録画番組 HLS Keep-Alive API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def VideoHLSKeepAliveAPI(
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
    session_id: Annotated[str, Query(description='セッション ID（クライアント側で適宜生成したランダム値を指定する）。')],
):
    """
    録画番組のストリーミング用 HLS セグメントの生成を継続するための API 。<br>
    ストリーミングセッションを維持するために、この API は録画番組の視聴を続けている間、定期的に呼び出さなければならない。<br>
    この API が定期的に呼び出されなくなった場合、一定時間後にストリーミング用 HLS セグメントの生成が停止され、メモリ上のデータが破棄される。
    """

    # 品質とオプション指定に対応する録画視聴セッションを取得
    video_stream = GetRecordedStream(session_id, recorded_program, stream_quality)

    # セッションのアクティブ状態を維持する
    video_stream.keepAlive()
