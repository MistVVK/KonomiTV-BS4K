
import asyncio
import json
import math
from enum import IntEnum
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import Response
from sse_starlette.sse import EventSourceResponse

from app import logging, schemas
from app.config import Config
from app.metadata.RecordedPlaybackIndex import IsRecordedPlaybackIndexReady
from app.metadata.RecordedPlaybackIndexer import RecordedPlaybackIndexer
from app.models.RecordedProgram import RecordedProgram
from app.streams.RecordedEncodingCodecs import (
    AudioCodec,
    VideoCodec,
)
from app.streams.RecordedFMP4Stream import RecordedFMP4Stream
from app.streams.RecordedPlaybackCapabilities import (
    RecordedPlaybackCapabilityProbe,
)
from app.streams.RecordedSubtitleStream import RecordedSubtitleStream
from app.streams.StreamEncodingOptions import (
    SplitQualityAndEncodingOptions,
    StreamQualityWithOptions,
)


# ルーター
router = APIRouter(
    tags = ['Streams'],
    prefix = '/api/streams/video',
)

class VideoBitDepthQuery(IntEnum):
    """クエリ文字列から整数へ変換可能な録画映像bit depth。"""

    BIT_8 = 8
    BIT_10 = 10


def GetRecordedStream(
    session_id: str,
    recorded_program: RecordedProgram,
    stream_quality: StreamQualityWithOptions,
    is_new_session_allowed: bool = False,
) -> RecordedFMP4Stream:
    """FFmpeg 8・fMP4録画視聴セッションを返す。"""

    if RecordedFMP4Stream.hasSession(session_id):
        return RecordedFMP4Stream(
            session_id,
            recorded_program,
            stream_quality.quality,
            encoding_options=stream_quality.encoding_options,
            is_new_session_allowed=False,
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
    )


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
    '/capabilities',
    summary = '録画再生エンコード能力 API',
    response_model = list[schemas.RecordedPlaybackCapability],
)
async def RecordedPlaybackCapabilitiesAPI() -> list[schemas.RecordedPlaybackCapability]:
    """録画用FFmpeg 8で利用できるエンコーダー・コーデック・bit depthの組み合わせを返す。"""

    capabilities = await RecordedPlaybackCapabilityProbe.getCapabilities()
    return [
        schemas.RecordedPlaybackCapability(
            encoder = capability.encoder,
            codec = capability.codec,
            bit_depth = capability.bit_depth,
            available = capability.available,
            profile = capability.profile,
            reason_code = capability.reason_code,
        )
        for capability in capabilities
    ]


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


async def ValidateQuality(
    quality: Annotated[str, Path(description='映像の品質。ex: 1080p')],
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    video_codec: Annotated[VideoCodec | None, Query(description='出力映像コーデック。省略時は旧画質URLから判定。')] = None,
    video_bit_depth: Annotated[VideoBitDepthQuery | None, Query(description='出力映像bit depth。')] = None,
    audio_codec: Annotated[AudioCodec, Query(description='出力音声コーデック。')] = 'aac',
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
        video_bit_depth = cast(Literal[8, 10] | None, int(video_bit_depth) if video_bit_depth is not None else None),
        audio_codec = audio_codec,
        audio_rendition_id = audio_track,
    )
    if stream_quality is None:
        logging.error(f'[VideoStreamsRouter][ValidateQuality] Specified quality was not found. [quality: {quality}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified quality was not found',
        )

    # 新経路を明示した要求は、能力APIと同じ実検査結果で事前に拒否する。
    if (
        IsRecordedPlaybackIndexReady(
            recorded_program.recorded_video.playback_index_status,
            recorded_program.recorded_video.playback_index_version,
        ) and
        (video_codec is not None or video_bit_depth is not None)
    ):
        selected_encoder = Config().general.encoder_bs4k \
            if is_bs4k_recorded_video else Config().general.encoder
        capability = next(
            (
                item for item in await RecordedPlaybackCapabilityProbe.getCapabilities()
                if item.encoder == selected_encoder and
                item.codec == stream_quality.encoding_options.video_codec and
                item.bit_depth == stream_quality.encoding_options.video_bit_depth
            ),
            None,
        )
        if capability is None or capability.available is False:
            reason_code = capability.reason_code if capability is not None else 'ProbeFailed'
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    'code': reason_code,
                    'message': 'The requested recorded encoding is unavailable.',
                },
            )

    return stream_quality


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

    # 品質とオプション指定に対応する録画視聴セッションを作成または取得
    video_stream = GetRecordedStream(session_id, recorded_program, stream_quality, is_new_session_allowed = True)
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
