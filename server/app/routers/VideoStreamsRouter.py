
import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import Response
from sse_starlette.sse import EventSourceResponse

from app import logging
from app.config import Config
from app.models.RecordedProgram import RecordedProgram
from app.streams.StreamEncodingOptions import (
    SplitQualityAndEncodingOptions,
    StreamQualityWithOptions,
)
from app.streams.RecordedEncodingCodecs import AudioCodec, VideoCodec
from app.streams.VideoStream import VideoStream
from app.streams.RecordedEncodingCodecs import getAudioCodecDefinition


# ルーター
router = APIRouter(
    tags = ['Streams'],
    prefix = '/api/streams/video',
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


async def ValidateQuality(
    quality: Annotated[str, Path(description='映像の品質。ex: 1080p')],
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    video_codec: Annotated[VideoCodec | None, Query(description='出力映像コーデック。省略時は旧画質URLから判定。')] = None,
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
        audio_codec = audio_codec,
        audio_rendition_id = audio_track,
    )
    if stream_quality is None:
        logging.error(f'[VideoStreamsRouter][ValidateQuality] Specified quality was not found. [quality: {quality}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified quality was not found',
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

    # 品質とオプション指定に対応する録画視聴セッションを作成または取得
    video_stream = VideoStream(
        session_id,
        recorded_program,
        stream_quality.quality,
        stream_quality.encoding_options,
        is_new_session_allowed = True,
    )

    # 映像と選択音声を同じ MPEG-TS に多重化した HLS プレイリストを取得
    virtual_playlist = video_stream.getMasterPlaylist(cache_key)
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
    video_stream = VideoStream(session_id, recorded_program, stream_quality.quality, stream_quality.encoding_options)
    return Response(
        content=video_stream.getVirtualPlaylist(cache_key),
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
    request_generation: Annotated[int | None, Query()] = None,
):
    video_stream = VideoStream(session_id, recorded_program, stream_quality.quality, stream_quality.encoding_options)
    segment_data = await video_stream.getMuxedSegment(sequence, request_generation)
    if segment_data is None:
        raise HTTPException(status_code=422, detail='Video segment was not found')
    return Response(content=segment_data, media_type='video/mp2t', headers={'Cache-Control': 'max-age=10800'})


@router.get('/{video_id}/{quality}/audio/{rendition_id}/playlist', response_class=Response)
async def VideoHLSAudioPlaylistAPI(
    rendition_id: str,
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
    session_id: Annotated[str, Query()],
    cache_key: Annotated[str | None, Query()] = None,
):
    video_stream = VideoStream(session_id, recorded_program, stream_quality.quality, stream_quality.encoding_options)
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
    request_generation: Annotated[int | None, Query()] = None,
):
    video_stream = VideoStream(session_id, recorded_program, stream_quality.quality, stream_quality.encoding_options)
    segment_data = await video_stream.getAudioSegment(rendition_id, sequence, request_generation)
    if segment_data is None:
        raise HTTPException(status_code=422, detail='Audio segment was not found')
    return Response(
        content=segment_data,
        media_type=getAudioCodecDefinition(video_stream.encoding_options.audio_codec).mime_type,
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
    video_stream = VideoStream(session_id, recorded_program, stream_quality.quality, stream_quality.encoding_options)
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
    video_stream = VideoStream(session_id, recorded_program, stream_quality.quality, stream_quality.encoding_options)
    return Response(
        content=video_stream.getSubtitlePlaylist(subtitle_index, cache_key),
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
    video_stream = VideoStream(session_id, recorded_program, stream_quality.quality, stream_quality.encoding_options)
    segment_data = await video_stream.getSubtitleSegment(subtitle_index)
    if segment_data is None:
        raise HTTPException(status_code=422, detail='Subtitle segment was not found')
    return Response(content=segment_data, media_type='text/vtt', headers={'Cache-Control': 'max-age=10800'})


@router.get(
    '/{video_id}/{quality}/segment',
    summary = '録画番組 HLS セグメント API',
    response_class = Response,
    responses = {
        status.HTTP_200_OK: {
            'description': 'HLS セグメントとして分割された MPEG-TS データ。',
            'content': {'video/mp2t': {}},
        }
    }
)
async def VideoHLSSegmentAPI(
    recorded_program: Annotated[RecordedProgram, Depends(ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateQuality)],
    session_id: Annotated[str, Query(description='セッション ID（クライアント側で適宜生成したランダム値を指定する）。')],
    sequence: Annotated[int, Query(description='HLS セグメントの 0 スタートのシーケンス番号。')],
    cache_key: Annotated[str | None, Query(description='キャッシュ制御用のキー。')],
):
    """
    指定された画質に対応する、録画番組のストリーミング用 HLS セグメントを返す。<br>
    呼び出された時点でエンコードされていない場合は既存のエンコードタスクが終了され、<br>
    sequence の HLS セグメントが含まれる範囲から新たにエンコードタスクが開始される。
    """

    # 品質とオプション指定に対応する録画視聴セッションを取得
    video_stream = VideoStream(session_id, recorded_program, stream_quality.quality, stream_quality.encoding_options)

    # セグメントを取得（キャッシュキーはブラウザキャッシュ避けのための ID なので特に使わない）
    segment_data = await video_stream.getMuxedSegment(sequence)
    if segment_data is None:
        logging.error(
            f'{video_stream.log_prefix} Specified sequence segment was not found. '
            f'[sequence: {sequence}]'
        )
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified sequence segment was not found',
        )

    # 取得した MPEG-TS データを返す
    return Response(
        content = segment_data,
        media_type = 'video/mp2t',
        headers = {
            # キャッシュ有効期間を3時間に設定
            'Cache-Control': 'max-age=10800',
        },
    )


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
    video_stream = VideoStream(session_id, recorded_program, stream_quality.quality, stream_quality.encoding_options)

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
    video_stream = VideoStream(session_id, recorded_program, stream_quality.quality, stream_quality.encoding_options)

    # セッションのアクティブ状態を維持する
    video_stream.keepAlive()
