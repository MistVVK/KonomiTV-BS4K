import json
import pathlib
import re
from collections.abc import Sequence
from dataclasses import replace
from typing import Annotated, Any, Literal, cast

import anyio
from fastapi import (
    APIRouter,
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import logging, schemas
from app.config import Config
from app.constants import QUALITY_TYPES, VERSION
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedVideo import RecordedVideo
from app.routers import (
    ChannelsRouter,
    LiveStreamsRouter,
    ProgramsRouter,
    ReservationConditionsRouter,
    ReservationsRouter,
    VideosRouter,
    VideoStreamsRouter,
)
from app.streams.StreamEncodingOptions import (
    StreamEncodingOptions,
    StreamQualityWithOptions,
)
from app.utils import GetPlatformEnvironment
from app.utils.edcb import ReserveDataRequired
from app.utils.edcb.CtrlCmdUtil import CtrlCmdUtil
from app.utils.HTTPS import ReverseProxyMiddleware
from app.utils.KonomiTVBS4KFastAPIRouteUtils import IterateKonomiTVBS4KAPIRouteContexts


_VIDEO_DETAIL_PATH_PATTERN = re.compile(r'^/api/videos/[0-9]+/?$')
_VIDEO_COLLECTION_PATHS = {
    '/api/videos',
    '/api/videos/',
    '/api/videos/search',
    '/api/videos/search/',
}
_RESERVATION_COLLECTION_PATHS = {
    '/api/recording/reservations',
    '/api/recording/reservations/',
}
_RESERVATION_DETAIL_PATH_PATTERN = re.compile(r'^/api/recording/reservations/[0-9]+/?$')
_KOMOREBI_V1_ENCODER_MAP: dict[
    Literal['FFmpeg', 'QSV', 'NVENC', 'AMF'],
    Literal['FFmpeg', 'QSVEncC', 'NVEncC', 'VCEEncC'],
] = {
    'FFmpeg': 'FFmpeg',
    'QSV': 'QSVEncC',
    'NVENC': 'NVEncC',
    'AMF': 'VCEEncC',
}

# Komorebi V1 が利用・宣言する API と、録画 HLS プレイリストから間接参照される API だけを公開する。
# 特に VideosRouter 全体を公開すると、再解析・CM 検出・サムネイル再生成などの重い POST API まで
# 無認証の互換ポートから実行できてしまうため、ルートテンプレートと HTTP メソッドの両方で固定する。
_KOMOREBI_V1_UPSTREAM_ROUTE_ALLOWLIST = frozenset({
    ('GET', '/api/channels'),
    ('GET', '/api/channels/{channel_id}/logo'),
    ('GET', '/api/channels/{channel_id}/jikkyo'),
    ('GET', '/api/programs/timetable'),
    ('GET', '/api/videos'),
    ('GET', '/api/videos/search'),
    ('GET', '/api/videos/{video_id}'),
    ('GET', '/api/videos/{video_id}/thumbnail'),
    ('GET', '/api/videos/{video_id}/thumbnail/tiled'),
    ('GET', '/api/videos/{video_id}/jikkyo'),
    ('GET', '/api/recording/reservations'),
    ('POST', '/api/recording/reservations'),
    ('DELETE', '/api/recording/reservations/{reservation_id}'),
    ('GET', '/api/recording/conditions'),
    ('POST', '/api/recording/conditions'),
    ('GET', '/api/recording/conditions/{reservation_condition_id}'),
    ('PUT', '/api/recording/conditions/{reservation_condition_id}'),
    ('DELETE', '/api/recording/conditions/{reservation_condition_id}'),
})


class CompatibilityHistoryUpdateRequest(BaseModel):
    """Komorebi が送信する視聴履歴更新リクエスト。"""

    program_id: str
    playback_position: float


class CompatibilityUser(BaseModel):
    """無認証で利用する Komorebi 向けユーザー情報。"""

    id: int
    name: str
    pinned_channel_ids: list[str]


class CompatibilityVersionInformation(BaseModel):
    """upstream KonomiTV と同じ形で公開する互換バージョン情報。"""

    version: str
    latest_version: str | None
    environment: Literal['Linux', 'Linux-Docker']
    backend: Literal['EDCB', 'Mirakurun']
    encoder: Literal['FFmpeg', 'QSVEncC', 'NVEncC', 'VCEEncC']


version_router = APIRouter(tags = ['Compatibility - Version'])


@version_router.get(
    '/api/version',
    summary = '互換バージョン情報 API',
    response_model = CompatibilityVersionInformation,
)
async def CompatibilityVersionInformationAPI() -> CompatibilityVersionInformation:
    """本線の BS4K 拡張情報を混ぜず、upstream KonomiTV のバージョン情報を返す。

    Returns:
        upstream KonomiTV と同じ形の互換バージョン情報。
    """

    general = Config().general
    return CompatibilityVersionInformation(
        version = VERSION,
        # BS4K 本体の更新先は upstream KonomiTV ではないため、BS4K の最新バージョンを混ぜない。
        latest_version = None,
        environment = GetPlatformEnvironment(),
        backend = general.backend,
        encoder = _KOMOREBI_V1_ENCODER_MAP[general.encoder],
    )


users_router = APIRouter(tags = ['Compatibility - Users'])


@users_router.get(
    '/api/users/me',
    summary = '互換ユーザー情報 API',
    response_model = CompatibilityUser,
)
async def UserMeAPI() -> CompatibilityUser:
    """認証設定を持たない Komorebi へ匿名の互換ユーザー情報を返す。"""

    return CompatibilityUser(
        id = 0,
        name = 'Komorebi',
        pinned_channel_ids = [],
    )


histories_router = APIRouter(
    tags = ['Compatibility - Histories'],
    prefix = '/api/histories',
)


@histories_router.get(
    '',
    summary = '互換視聴履歴一覧 API',
    response_model = list[dict[str, Any]],
)
async def HistoriesAPI() -> list[dict[str, Any]]:
    """サーバー側に履歴を保存しない互換動作として空の履歴を返す。"""

    return []


@histories_router.post(
    '',
    summary = '互換視聴履歴更新 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def UpdateHistoryAPI(
    history_update_request: Annotated[CompatibilityHistoryUpdateRequest, Body()],
) -> Response:
    """Komorebi の端末内履歴を正とし、更新要求だけを正常受理する。"""

    # リクエストモデルによる形式検証は行うが、KonomiTV の DB には保存しない。
    # Komorebi は端末内 Room DB を先に更新するため、ここでは同期先が空でも再生位置を失わない。
    del history_update_request
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def IncludeKomorebiV1UpstreamRoutes(
    compatibility_app: FastAPI,
    source_routers: Sequence[APIRouter],
) -> None:
    """既存ルーターから Komorebi V1 に必要な method + path の組み合わせだけを複製する。"""

    filtered_router = APIRouter()
    included_routes: set[tuple[str, str]] = set()
    for source_router in source_routers:
        for route_context in IterateKonomiTVBS4KAPIRouteContexts(source_router.routes):
            original_route = cast(APIRoute, route_context.original_route)
            path = route_context.path
            methods = route_context.methods
            endpoint = route_context.endpoint

            # APIRoute では常に揃う情報だが、不完全な route context を黙って除外すると
            # allowlist の不足を見逃すため、互換 API の構築を明示的に失敗させる。
            if path is None or methods is None or endpoint is None:
                raise RuntimeError('Komorebi V1 互換 API のルート情報が不完全です')

            route_keys = {(method, path) for method in methods}
            allowed_route_keys = route_keys & _KOMOREBI_V1_UPSTREAM_ROUTE_ALLOWLIST
            if not allowed_route_keys:
                continue
            if allowed_route_keys != route_keys:
                raise RuntimeError(
                    f'Komorebi V1 互換 API の複数メソッドルートを部分的に公開できません: {path}',
                )

            # nested include_router() の prefix・dependencies・レスポンス設定を含む有効な文脈を複製し、
            # allowlist に一致したルートだけを従来どおり互換 API へ公開する。
            filtered_router.add_api_route(
                path = path,
                endpoint = endpoint,
                response_model = route_context.response_model,
                status_code = route_context.status_code,
                tags = route_context.tags,
                dependencies = route_context.dependencies,
                summary = route_context.summary,
                description = route_context.description,
                response_description = route_context.response_description,
                responses = route_context.responses,
                deprecated = route_context.deprecated,
                methods = methods,
                operation_id = route_context.operation_id,
                response_model_include = route_context.response_model_include,
                response_model_exclude = route_context.response_model_exclude,
                response_model_by_alias = route_context.response_model_by_alias,
                response_model_exclude_unset = route_context.response_model_exclude_unset,
                response_model_exclude_defaults = route_context.response_model_exclude_defaults,
                response_model_exclude_none = route_context.response_model_exclude_none,
                include_in_schema = route_context.include_in_schema,
                response_class = route_context.response_class,
                name = route_context.name,
                route_class_override = type(original_route),
                callbacks = route_context.callbacks,
                openapi_extra = route_context.openapi_extra,
                generate_unique_id_function = route_context.generate_unique_id_function,
                strict_content_type = route_context.strict_content_type,
            )
            included_routes.update(route_keys)

    missing_routes = _KOMOREBI_V1_UPSTREAM_ROUTE_ALLOWLIST - included_routes
    if missing_routes:
        missing_routes_text = ', '.join(
            f'{method} {path}' for method, path in sorted(missing_routes)
        )
        raise RuntimeError(f'Komorebi V1 互換 API の実装ルートが見つかりません: {missing_routes_text}')

    compatibility_app.include_router(filtered_router)


async def ValidateCompatibilityLiveStreamQuality(
    quality: Annotated[str, Path(description='映像の品質。ex: 1080p')],
    display_channel_id: Annotated[str, Depends(LiveStreamsRouter.ValidateChannelID)],
) -> StreamQualityWithOptions:
    """互換 API のライブ出力を旧 AVC / HEVC + AAC 契約へ固定し、original だけ passthrough で開く。"""

    # original は GR/BS/CS フルセグ + BS4K + ワンセグに限り、本線と同じ MPEG-TS passthrough で開く。
    # ラジオと SKY/CATV などは 422 のままにする。
    # 解決済み quality を直接渡す現行構造のため、本線 ValidateQuality の BS4K/ワンセグ 422 ゲートは通らない。
    if quality == 'original':
        original_channel = await LiveStreamsRouter.Channel.filter(
            display_channel_id = display_channel_id,
        ).get_or_none()
        if (
            original_channel is None or
            original_channel.is_radiochannel is True or
            original_channel.type not in ('GR', 'BS', 'CS', 'BS4K')
        ):
            logging.error(
                f'[CompatibilityAPI][ValidateCompatibilityLiveStreamQuality] Original quality is not available for this channel. '
                f'[display_channel_id: {display_channel_id}]'
            )
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = 'Original quality is not available for this channel',
            )
        original_quality = LiveStreamsRouter.SplitQualityAndEncodingOptions(
            quality,
            LiveStreamsRouter.GetEncoderForLiveChannel(display_channel_id),
        )
        if original_quality is None:
            # SplitQualityAndEncodingOptions() は original を常に解決するため、通常ここには到達しない
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = 'Specified quality was not found',
            )
        return original_quality

    # 互換ルートはStream Anchorを使わないため、mainルートのBridge必須能力は検査しない。
    # 品質名から旧 AVC / HEVC + AAC tupleを正規化し、不正品質だけを422で拒否する。
    selected_encoder = LiveStreamsRouter.GetEncoderForLiveChannel(display_channel_id)
    stream_quality = LiveStreamsRouter.SplitQualityAndEncodingOptions(
        quality,
        selected_encoder,
    )
    if stream_quality is None:
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified quality was not found',
        )

    channel = await LiveStreamsRouter.Channel.filter(
        display_channel_id = display_channel_id,
    ).get_or_none()
    if channel is not None and channel.is_radiochannel is True:
        stream_quality = StreamQualityWithOptions(
            quality = cast(QUALITY_TYPES, stream_quality.quality.removesuffix('-hevc')),
            encoding_options = StreamEncodingOptions(audio_codec = 'aac'),
        )
    elif (
        stream_quality.encoding_options.video_codec == 'hevc'
        and stream_quality.encoding_options.video_bit_depth == 10
    ):
        # Komorebi V1 の旧 -10bit は可能なら10bitを使う希望指定であり、exact指定ではない。
        # main API の exact 契約は変えず、互換 API だけ選択エンコーダーの能力不足時に8bitへ戻す。
        capability = await LiveStreamsRouter.KonomiTVBS4KPlaybackCapabilityProbe.getLegacyLiveCombinationCapability(
            selected_encoder,
            'hevc',
            10,
            'aac',
        )
        if capability.available is False:
            stream_quality = replace(
                stream_quality,
                encoding_options = replace(
                    stream_quality.encoding_options,
                    is_hevc_10bit_enabled = False,
                    video_bit_depth = 8,
                ),
            )
    return stream_quality


async def ValidateCompatibilityRecordedStreamQuality(
    quality: Annotated[str, Path(description='映像の品質。ex: 1080p')],
    recorded_program: Annotated[RecordedProgram, Depends(VideoStreamsRouter.ValidateVideoID)],
) -> StreamQualityWithOptions:
    """互換 API の録画出力を旧 AVC / HEVC + AAC 契約へ固定する。"""

    return await VideoStreamsRouter.ValidateQuality(
        quality,
        recorded_program,
        None,
        None,
        'aac',
        None,
    )


compatibility_live_streams_router = APIRouter(
    tags = ['Compatibility - Live Streams'],
    prefix = '/api/streams/live',
)


@compatibility_live_streams_router.get(
    '/{display_channel_id}/{quality}/events',
    response_class = Response,
)
async def CompatibilityLiveStreamEventAPI(
    request: Request,
    display_channel_id: Annotated[str, Depends(LiveStreamsRouter.ValidateChannelID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateCompatibilityLiveStreamQuality)],
):
    """
    従来 codec に固定したライブ状態イベントを返す。

    Args:
        request (Request): 処理対象の HTTP request。
        display_channel_id (Annotated[str, Depends(LiveStreamsRouter.ValidateChannelID)]): 対象チャンネルの表示用 ID。
        stream_quality (Annotated[StreamQualityWithOptions, Depends(ValidateCompatibilityLiveStreamQuality)]): codec option を解決済みのライブ画質。

    Returns:
        None
    """

    request.state.stream_anchor_enabled = False
    return await LiveStreamsRouter.LiveStreamEventAPI(request, display_channel_id, stream_quality)


@compatibility_live_streams_router.get(
    '/{display_channel_id}/{quality}/mpegts',
    response_class = Response,
)
async def CompatibilityLiveMPEGTSStreamAPI(
    request: Request,
    display_channel_id: Annotated[str, Depends(LiveStreamsRouter.ValidateChannelID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateCompatibilityLiveStreamQuality)],
):
    """
    Bridge を通らない従来 codec のライブ MPEG-TS を返す。

    Args:
        request (Request): 処理対象の HTTP request。
        display_channel_id (Annotated[str, Depends(LiveStreamsRouter.ValidateChannelID)]): 対象チャンネルの表示用 ID。
        stream_quality (Annotated[StreamQualityWithOptions, Depends(ValidateCompatibilityLiveStreamQuality)]): codec option を解決済みのライブ画質。

    Returns:
        None
    """

    request.state.stream_anchor_enabled = False
    return await LiveStreamsRouter.LiveMPEGTSStreamAPI(
        request,
        display_channel_id,
        stream_quality,
    )


compatibility_video_streams_router = APIRouter(
    tags = ['Compatibility - Video Streams'],
    prefix = '/api/streams/video',
)


@compatibility_video_streams_router.get('/{video_id}/{quality}/playlist', response_class = Response)
async def CompatibilityVideoHLSPlaylistAPI(
    request: Request,
    recorded_program: Annotated[RecordedProgram, Depends(VideoStreamsRouter.ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateCompatibilityRecordedStreamQuality)],
    session_id: Annotated[str, Query()],
    cache_key: VideoStreamsRouter.CacheKeyQuery = None,
):
    """従来 codec に固定した録画 HLS master playlist を返す。"""

    return await VideoStreamsRouter.VideoHLSPlaylistAPI(
        request,
        recorded_program,
        stream_quality,
        session_id,
        cache_key,
    )


@compatibility_video_streams_router.get('/{video_id}/{quality}/video/playlist', response_class = Response)
async def CompatibilityVideoHLSVideoPlaylistAPI(
    recorded_program: Annotated[RecordedProgram, Depends(VideoStreamsRouter.ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateCompatibilityRecordedStreamQuality)],
    session_id: Annotated[str, Query()],
    cache_key: VideoStreamsRouter.CacheKeyQuery = None,
):
    return await VideoStreamsRouter.VideoHLSVideoPlaylistAPI(
        recorded_program,
        stream_quality,
        session_id,
        cache_key,
    )


@compatibility_video_streams_router.get('/{video_id}/{quality}/video/init', response_class = Response)
async def CompatibilityVideoHLSVideoInitSegmentAPI(
    recorded_program: Annotated[RecordedProgram, Depends(VideoStreamsRouter.ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateCompatibilityRecordedStreamQuality)],
    session_id: Annotated[str, Query()],
    generation: Annotated[int, Query()],
    sequence: Annotated[int, Query()] = 0,
    cache_key: VideoStreamsRouter.CacheKeyQuery = None,
):
    return await VideoStreamsRouter.VideoHLSVideoInitSegmentAPI(
        recorded_program,
        stream_quality,
        session_id,
        generation,
        sequence,
        cache_key,
    )


@compatibility_video_streams_router.get('/{video_id}/{quality}/video/segment', response_class = Response)
async def CompatibilityVideoHLSVideoSegmentAPI(
    recorded_program: Annotated[RecordedProgram, Depends(VideoStreamsRouter.ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateCompatibilityRecordedStreamQuality)],
    session_id: Annotated[str, Query()],
    sequence: Annotated[int, Query()],
    cache_key: VideoStreamsRouter.CacheKeyQuery = None,
):
    return await VideoStreamsRouter.VideoHLSVideoSegmentAPI(
        recorded_program,
        stream_quality,
        session_id,
        sequence,
        cache_key,
    )


@compatibility_video_streams_router.get(
    '/{video_id}/{quality}/audio/{rendition_id}/playlist',
    response_class = Response,
)
async def CompatibilityVideoHLSAudioPlaylistAPI(
    rendition_id: str,
    recorded_program: Annotated[RecordedProgram, Depends(VideoStreamsRouter.ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateCompatibilityRecordedStreamQuality)],
    session_id: Annotated[str, Query()],
    cache_key: VideoStreamsRouter.CacheKeyQuery = None,
):
    return await VideoStreamsRouter.VideoHLSAudioPlaylistAPI(
        rendition_id,
        recorded_program,
        stream_quality,
        session_id,
        cache_key,
    )


@compatibility_video_streams_router.get(
    '/{video_id}/{quality}/audio/{rendition_id}/init',
    response_class = Response,
)
async def CompatibilityVideoHLSAudioInitSegmentAPI(
    rendition_id: str,
    recorded_program: Annotated[RecordedProgram, Depends(VideoStreamsRouter.ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateCompatibilityRecordedStreamQuality)],
    session_id: Annotated[str, Query()],
    sequence: Annotated[int, Query()] = 0,
    cache_key: VideoStreamsRouter.CacheKeyQuery = None,
):
    return await VideoStreamsRouter.VideoHLSAudioInitSegmentAPI(
        rendition_id,
        recorded_program,
        stream_quality,
        session_id,
        sequence,
        cache_key,
    )


@compatibility_video_streams_router.get(
    '/{video_id}/{quality}/audio/{rendition_id}/segment',
    response_class = Response,
)
async def CompatibilityVideoHLSAudioSegmentAPI(
    rendition_id: str,
    recorded_program: Annotated[RecordedProgram, Depends(VideoStreamsRouter.ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateCompatibilityRecordedStreamQuality)],
    session_id: Annotated[str, Query()],
    sequence: Annotated[int, Query()],
    cache_key: VideoStreamsRouter.CacheKeyQuery = None,
):
    return await VideoStreamsRouter.VideoHLSAudioSegmentAPI(
        rendition_id,
        recorded_program,
        stream_quality,
        session_id,
        sequence,
        cache_key,
    )


@compatibility_video_streams_router.get(
    '/{video_id}/{quality}/subtitle/{subtitle_index}/playlist',
    response_class = Response,
)
async def CompatibilityVideoHLSSubtitlePlaylistAPI(
    subtitle_index: int,
    recorded_program: Annotated[RecordedProgram, Depends(VideoStreamsRouter.ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateCompatibilityRecordedStreamQuality)],
    session_id: Annotated[str, Query()],
    cache_key: VideoStreamsRouter.CacheKeyQuery = None,
):
    return await VideoStreamsRouter.VideoHLSSubtitlePlaylistAPI(
        subtitle_index,
        recorded_program,
        stream_quality,
        session_id,
        cache_key,
    )


@compatibility_video_streams_router.get(
    '/{video_id}/{quality}/subtitle/{subtitle_index}/webvtt',
    response_class = Response,
)
async def CompatibilityVideoSubtitleWebVTTAPI(
    subtitle_index: int,
    recorded_program: Annotated[RecordedProgram, Depends(VideoStreamsRouter.ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateCompatibilityRecordedStreamQuality)],
    session_id: Annotated[str, Query()],
):
    return await VideoStreamsRouter.VideoSubtitleWebVTTAPI(
        subtitle_index,
        recorded_program,
        stream_quality,
        session_id,
    )


@compatibility_video_streams_router.put(
    '/{video_id}/{quality}/keep-alive',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def CompatibilityVideoHLSKeepAliveAPI(
    recorded_program: Annotated[RecordedProgram, Depends(VideoStreamsRouter.ValidateVideoID)],
    stream_quality: Annotated[StreamQualityWithOptions, Depends(ValidateCompatibilityRecordedStreamQuality)],
    session_id: Annotated[str, Query()],
):
    return await VideoStreamsRouter.VideoHLSKeepAliveAPI(
        recorded_program,
        stream_quality,
        session_id,
    )


compatibility_videos_router = APIRouter(
    tags = ['Compatibility - Videos'],
    prefix = '/api/videos',
)


def CollectCompatibilityDownloadVideoCodecs(recorded_video: RecordedVideo) -> list[str]:
    """
    互換 download の判定用に、代表 codec と DB 列挙済みの映像 timeline 全区間の codec を集める。

    代表 codec の欠落・空文字も「不一致」として空文字で返すことで、呼び出し側の許可リスト検査を
    安全側 (422) に倒す。timeline が存在しない録画では代表 codec だけを返す。

    Args:
        recorded_video (RecordedVideo): 判定対象の録画ファイル DB レコード。

    Returns:
        list[str]: 代表 codec と列挙済み全映像区間の codec 値 (欠落は空文字)。
    """

    codecs: list[str] = []

    # 代表 codec (最長区間の codec)。欠落・空文字は不一致として空文字で返す。
    if recorded_video.video_codec is not None and str(recorded_video.video_codec).strip() != '':
        codecs.append(str(recorded_video.video_codec))
    else:
        codecs.append('')

    # DB 列挙済みの映像 timeline 全区間。codec の欠落・空文字は不一致として空文字で返す。
    for entry in recorded_video.video_stream_timeline or []:
        codec = entry.get('codec')
        codecs.append(str(codec) if codec is not None and str(codec).strip() != '' else '')

    return codecs


def CollectCompatibilityDownloadAudioCodecs(recorded_video: RecordedVideo) -> list[str]:
    """
    互換 download の判定用に、DB に列挙済みの副音声・全音声 track / timeline の codec を集める。

    主音声は呼び出し側で必須 AAC 検査済みのため、ここには含めない。
    値が欠落・空文字・想定外の形状の要素は「不一致」として空文字で返すことで、
    呼び出し側の AAC 系検査を安全側 (422) に倒す。

    Args:
        recorded_video (RecordedVideo): 判定対象の録画ファイル DB レコード。

    Returns:
        list[str]: 副音声と列挙済み全音声の codec 値 (欠落・不正は空文字)。
    """

    codecs: list[str] = []

    # 副音声 (存在する場合だけ)。None・空文字は「副音声なし」とみなして何も足さない。
    if recorded_video.secondary_audio_codec is not None and str(recorded_video.secondary_audio_codec).strip() != '':
        codecs.append(str(recorded_video.secondary_audio_codec))

    # DB 列挙済みの全音声 track。codec の欠落・空文字は不一致として空文字で返す。
    for track in recorded_video.audio_tracks or []:
        codec = track.get('codec')
        codecs.append(str(codec) if codec is not None and str(codec).strip() != '' else '')

    # DB 列挙済みの全音声 timeline 内の track。空の tracks (無音区間) は検査対象にしない。
    for entry in recorded_video.audio_track_timeline or []:
        for track in entry.get('tracks') or []:
            codec = track.get('codec')
            codecs.append(str(codec) if codec is not None and str(codec).strip() != '' else '')

    return codecs


@compatibility_videos_router.get(
    '/{video_id}/download',
    summary = '互換録画番組ダウンロード API',
    response_description = 'TS コンテナ + 放送波コーデックの録画番組ファイル。',
    response_class = FileResponse,
    responses = {
        200: {'content': {'video/mp2t': {}}},
        422: {'description': 'Specified video_id was not found or the recorded file is not a broadcast TS'},
    },
)
async def CompatibilityVideoDownloadAPI(
    recorded_program: Annotated[RecordedProgram, Depends(VideoStreamsRouter.ValidateVideoID)],
):
    """
    TS + 放送波コーデックの録画だけを生ファイルで配信する。

    配信可否は既存の DB メタデータだけで判定し、リクエスト時の ffprobe やファイル走査は行わない。
    メタデータの欠落・不一致がある録画は安全側で 422 にする。
    通過時は本線 VideoDownloadAPI と同じ生ファイル配信 (FileResponse、Range 対応) を行う。
    """

    recorded_video = recorded_program.recorded_video
    file_path = anyio.Path(recorded_video.file_path)
    filename = file_path.name

    # 録画ファイルが消えていると FileResponse が 500 になるため、通常ファイルの存在を先に確認する
    if await file_path.is_file() is False:
        logging.error(f'[CompatibilityAPI][CompatibilityVideoDownloadAPI] Recorded file was not found. path: {recorded_video.file_path}')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified video_id was not found',
        )

    # TS 系拡張子以外の録画は配信しない
    if pathlib.Path(filename).suffix.lower() not in ('.ts', '.m2ts'):
        logging.error(f'[CompatibilityAPI][CompatibilityVideoDownloadAPI] Recorded file is not a transport stream. path: {recorded_video.file_path}')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Recorded file is not a transport stream',
        )

    # 放送波の映像コーデック (MPEG-2 / AVC / HEVC) 以外の録画は配信しない
    ## 代表 codec だけでなく、DB 列挙済みの映像 timeline 全区間も確認する。
    ## 生ファイル配信では無視された条件外区間までレスポンスに含まれるため、
    ## 列挙済み codec が1つでも許可対象外・欠落なら安全側で 422 にする。
    ## DB には解析世代により 'H.265' 形式と 'hevc' 形式が混在するため、小文字正規化して両方を受け付ける。
    for video_codec in CollectCompatibilityDownloadVideoCodecs(recorded_video):
        if video_codec.strip().lower() not in (
            'mpeg-2', 'mpeg2video', 'mpeg2',
            'h.264', 'h264', 'avc',
            'h.265', 'h265', 'hevc',
        ):
            logging.error(f'[CompatibilityAPI][CompatibilityVideoDownloadAPI] Recorded video codec is not supported for direct download. codec: {video_codec}')
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = 'Recorded video codec is not supported for direct download',
            )

    # AAC 系以外の音声を持つ録画は配信しない
    ## 主音声だけでなく、副音声と DB 列挙済みの全音声 track / timeline も確認する。
    ## 生ファイル配信では無視された非 AAC stream までレスポンスに含まれるため、
    ## 列挙済み codec が1つでも AAC 系でなければ安全側で 422 にする。
    if (recorded_video.primary_audio_codec or '').strip().upper().startswith('AAC') is False:
        logging.error(f'[CompatibilityAPI][CompatibilityVideoDownloadAPI] Recorded audio codec is not supported for direct download. codec: {recorded_video.primary_audio_codec}')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Recorded audio codec is not supported for direct download',
        )
    for audio_codec in CollectCompatibilityDownloadAudioCodecs(recorded_video):
        if audio_codec.strip().upper().startswith('AAC') is False:
            logging.error(f'[CompatibilityAPI][CompatibilityVideoDownloadAPI] Recorded audio codec is not supported for direct download. codec: {audio_codec}')
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = 'Recorded audio codec is not supported for direct download',
            )

    return FileResponse(
        path = str(file_path),
        filename = filename,
        media_type = VideosRouter.GetRecordedFileDownloadMediaType(filename),
    )


def PreserveReservationSettingsForKomorebi(
    record_settings: schemas.RecordSettings,
    existing_record_settings: schemas.RecordSettings,
) -> schemas.RecordSettings:
    """Komorebi の単発予約モデルで表現できない既存設定を更新時に保持する。"""

    preserved_record_settings = record_settings.model_copy(deep=True)

    # Komorebi は録画フォルダをパス文字列だけで保持する。GET 時に落ちるファイル名テンプレートと
    # ワンセグ別フォルダ属性を、同じパスの既存フォルダから順番に復元する。
    existing_folders_by_path: dict[str, list[schemas.RecordingFolder]] = {}
    for recording_folder in existing_record_settings.recording_folders:
        existing_folders_by_path.setdefault(recording_folder.recording_folder_path, []).append(recording_folder)
    for recording_folder in preserved_record_settings.recording_folders:
        existing_folders = existing_folders_by_path.get(recording_folder.recording_folder_path, [])
        if not existing_folders:
            continue
        existing_folder = existing_folders.pop(0)
        recording_folder.recording_file_name_template = existing_folder.recording_file_name_template
        recording_folder.is_oneseg_separate_recording_folder = existing_folder.is_oneseg_separate_recording_folder

    # Komorebi の non-null Int では EDCB の「デフォルトに従う」(null) と明示 0 を区別できない。
    # 既存値が null かつ受信値が 0 の場合は、意図しない設定上書きを避けて null を維持する。
    if existing_record_settings.recording_start_margin is None and preserved_record_settings.recording_start_margin == 0:
        preserved_record_settings.recording_start_margin = None
    if existing_record_settings.recording_end_margin is None and preserved_record_settings.recording_end_margin == 0:
        preserved_record_settings.recording_end_margin = None
    if existing_record_settings.forced_tuner_id is None and preserved_record_settings.forced_tuner_id == 0:
        preserved_record_settings.forced_tuner_id = None

    return preserved_record_settings


compatibility_reservations_router = APIRouter(
    tags = ['Compatibility - Reservations'],
    prefix = '/api/recording/reservations',
)


@compatibility_reservations_router.put(
    '/{reservation_id}',
    summary = 'Komorebi 互換録画予約設定更新 API',
    response_model = schemas.Reservation,
)
async def CompatibilityUpdateReservationAPI(
    reserve_data: Annotated[ReserveDataRequired, Depends(ReservationsRouter.GetReserveData)],
    reserve_update_request: Annotated[schemas.ReservationUpdateRequest, Body()],
    edcb: Annotated[CtrlCmdUtil, Depends(ReservationsRouter.GetCtrlCmdUtil)],
):
    """Komorebi が表現できない既存値を保持してから、通常の予約更新処理へ引き渡す。"""

    existing_record_settings = ReservationsRouter.DecodeEDCBRecSettingData(reserve_data['rec_setting'])
    reserve_update_request.record_settings = PreserveReservationSettingsForKomorebi(
        reserve_update_request.record_settings,
        existing_record_settings,
    )
    return await ReservationsRouter.UpdateReservationAPI(
        reserve_data,
        reserve_update_request,
        edcb,
    )


def IsRecordedProgramResponsePath(path: str) -> bool:
    """Komorebi 向け録画番組変換を適用する API パスかどうかを返す。"""

    return path in _VIDEO_COLLECTION_PATHS or _VIDEO_DETAIL_PATH_PATTERN.fullmatch(path) is not None


def IsReservationResponsePath(path: str) -> bool:
    """Komorebi 向け単発録画予約変換を適用する API パスかどうかを返す。"""

    return path in _RESERVATION_COLLECTION_PATHS or _RESERVATION_DETAIL_PATH_PATTERN.fullmatch(path) is not None


def TransformRecordedProgramForKomorebi(recorded_program: dict[str, Any]) -> None:
    """録画番組レスポンスを Komorebi が安全に読み取れる形式へインプレース変換する。"""

    recorded_video = recorded_program.get('recorded_video')
    if not isinstance(recorded_video, dict):
        return

    # Komorebi は Kotlin 側で camelCase のプロパティ名をそのまま Gson に渡している。
    # KonomiTV 固有の snake_case 名は残し、互換名を追加することで既存 API の情報量も維持する。
    recorded_program['seriesName'] = recorded_program.get('series_title')
    recorded_program['isEpisodic'] = bool(
        recorded_program.get('series_id') is not None or
        recorded_program.get('episode_number')
    )
    recorded_program['isRecording'] = recorded_video.get('status') == 'Recording'
    recorded_program['playbackPosition'] = 0.0

    # Komorebi は録画再生・サムネイル・実況の URL に recorded_video.id を使う。
    # KonomiTV の各 API は RecordedProgram.id を受け取るため、互換レスポンス上の ID を揃える。
    recorded_program_id = recorded_program.get('id')
    if isinstance(recorded_program_id, int):
        recorded_video['id'] = recorded_program_id

    # Komorebi の RecordedVideo では以下が non-null String のため、解析中の録画でも null を返さない。
    recorded_video['recording_start_time'] = (
        recorded_video.get('recording_start_time') or recorded_program.get('start_time') or ''
    )
    recorded_video['recording_end_time'] = (
        recorded_video.get('recording_end_time') or recorded_program.get('end_time') or ''
    )
    recorded_video['video_codec'] = recorded_video.get('video_codec') or ''
    recorded_video['audio_codec'] = recorded_video.get('primary_audio_codec') or ''

    # Pending/Stale の再生索引は初回再生要求時に生成・更新されるため再生可能として扱う。
    # 解析中・録画中・索引失敗だけを一覧上で再生不可にする。
    recorded_video['has_key_frames'] = bool(
        recorded_video.get('status') == 'Recorded' and
        recorded_video.get('has_video') is True and
        recorded_video.get('playback_index_state') in ('Pending', 'Ready', 'Stale')
    )


def TransformRecordedProgramResponseForKomorebi(response_data: Any) -> Any:
    """録画番組一覧・検索・詳細レスポンス内の各録画番組を互換形式へ変換する。"""

    if not isinstance(response_data, dict):
        return response_data

    recorded_programs = response_data.get('recorded_programs')
    if isinstance(recorded_programs, list):
        for recorded_program in recorded_programs:
            if isinstance(recorded_program, dict):
                TransformRecordedProgramForKomorebi(recorded_program)
        return response_data

    # 詳細 API は RecordedProgram 自体をトップレベルに返す。
    if isinstance(response_data.get('recorded_video'), dict):
        TransformRecordedProgramForKomorebi(response_data)
    return response_data


def TransformReservationResponseForKomorebi(response_data: Any) -> Any:
    """単発録画予約の録画フォルダを Komorebi の文字列配列形式へ変換する。"""

    if not isinstance(response_data, dict):
        return response_data
    reservations = response_data.get('reservations')
    if isinstance(reservations, list):
        reservation_items = reservations
    elif isinstance(response_data.get('record_settings'), dict):
        reservation_items = [response_data]
    else:
        return response_data

    for reservation in reservation_items:
        if not isinstance(reservation, dict):
            continue
        record_settings = reservation.get('record_settings')
        if not isinstance(record_settings, dict):
            continue

        # Komorebi は単発予約だけ recording_folders を List<String> として定義している。
        # 自動予約条件側は KonomiTV と同じ RecordingFolder オブジェクトなので、ここでは変換しない。
        recording_folders = record_settings.get('recording_folders')
        if isinstance(recording_folders, list):
            record_settings['recording_folders'] = [
                str(recording_folder.get('recording_folder_path') or '')
                if isinstance(recording_folder, dict) else recording_folder
                for recording_folder in recording_folders
            ]
        elif recording_folders is None:
            record_settings['recording_folders'] = []

        # Komorebi の単発予約モデルでは nullable ではないため、KonomiTV のデフォルト指定を数値へ正規化する。
        for field_name in ('recording_start_margin', 'recording_end_margin', 'forced_tuner_id'):
            if record_settings.get(field_name) is None:
                record_settings[field_name] = 0
    return response_data


def TransformCompatibilityResponseForKomorebi(path: str, response_data: Any) -> Any:
    """API パスに対応する Komorebi 互換レスポンス変換を適用する。"""

    if IsRecordedProgramResponsePath(path):
        return TransformRecordedProgramResponseForKomorebi(response_data)
    if IsReservationResponsePath(path):
        return TransformReservationResponseForKomorebi(response_data)
    return response_data


class KomorebiResponseMiddleware:
    """既存ルーターの JSON レスポンスを Komorebi 互換形式へ変換する ASGI ミドルウェア。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope['type'] != 'http' or
            scope.get('method') not in ('GET', 'PUT') or
            (
                IsRecordedProgramResponsePath(scope.get('path', '')) is False and
                IsReservationResponsePath(scope.get('path', '')) is False
            )
        ):
            await self.app(scope, receive, send)
            return

        response_start: Message | None = None
        response_body_parts: list[bytes] = []

        async def SendCompatibilityResponse(message: Message) -> None:
            nonlocal response_start

            if message['type'] == 'http.response.start':
                response_start = message
                return

            if message['type'] != 'http.response.body' or response_start is None:
                await send(message)
                return

            response_body_parts.append(message.get('body', b''))
            if message.get('more_body', False):
                return

            body = b''.join(response_body_parts)
            status_code = response_start['status']
            headers = list(response_start.get('headers', []))
            content_type = next(
                (value for key, value in headers if key.lower() == b'content-type'),
                b'',
            )

            if status_code == status.HTTP_200_OK and content_type.lower().startswith(b'application/json'):
                try:
                    response_data = json.loads(body)
                    body = json.dumps(
                        TransformCompatibilityResponseForKomorebi(scope.get('path', ''), response_data),
                        ensure_ascii = False,
                        separators = (',', ':'),
                    ).encode('utf-8')
                    headers = [
                        (key, value)
                        for key, value in headers
                        if key.lower() != b'content-length'
                    ]
                    headers.append((b'content-length', str(len(body)).encode('ascii')))
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                    # 想定外の JSON は壊さず、そのままクライアントへ返す。
                    pass

            await send({**response_start, 'headers': headers})
            await send({
                'type': 'http.response.body',
                'body': body,
                'more_body': False,
            })

        await self.app(scope, receive, SendCompatibilityResponse)


class KomorebiReservationRequestMiddleware:
    """Komorebi の文字列録画フォルダを KonomiTV の RecordingFolder 形式へ変換する。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def IsReservationWriteRequest(scope: Scope) -> bool:
        """変換対象の単発録画予約追加・更新リクエストかどうかを返す。"""

        if scope['type'] != 'http' or scope.get('method') not in ('POST', 'PUT'):
            return False
        path = scope.get('path', '').rstrip('/')
        if path == '/api/recording/reservations':
            return True
        return re.fullmatch(r'/api/recording/reservations/[0-9]+', path) is not None

    @staticmethod
    def TransformRequestData(request_data: Any) -> Any:
        """文字列配列の recording_folders を RecordingFolder オブジェクト配列へ変換する。"""

        if not isinstance(request_data, dict):
            return request_data
        record_settings = request_data.get('record_settings')
        if not isinstance(record_settings, dict):
            return request_data

        recording_folders = record_settings.get('recording_folders')
        if recording_folders is None:
            record_settings['recording_folders'] = []
            return request_data
        if not isinstance(recording_folders, list):
            return request_data

        record_settings['recording_folders'] = [
            {
                'recording_folder_path': recording_folder,
                'recording_file_name_template': None,
                'is_oneseg_separate_recording_folder': False,
            }
            if isinstance(recording_folder, str) else recording_folder
            for recording_folder in recording_folders
        ]
        return request_data

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.IsReservationWriteRequest(scope) is False:
            await self.app(scope, receive, send)
            return

        request_body_parts: list[bytes] = []
        while True:
            message = await receive()
            if message['type'] != 'http.request':
                await self.app(scope, receive, send)
                return
            request_body_parts.append(message.get('body', b''))
            if message.get('more_body', False) is False:
                break

        request_body = b''.join(request_body_parts)
        try:
            request_data = json.loads(request_body)
            request_body = json.dumps(
                self.TransformRequestData(request_data),
                ensure_ascii = False,
                separators = (',', ':'),
            ).encode('utf-8')
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            # FastAPI 本来の 422 / JSON decode エラーを維持するため、解釈できない本文はそのまま渡す。
            pass

        is_request_body_sent = False

        async def ReceiveCompatibilityRequest() -> Message:
            nonlocal is_request_body_sent
            if is_request_body_sent is False:
                is_request_body_sent = True
                return {
                    'type': 'http.request',
                    'body': request_body,
                    'more_body': False,
                }
            return await receive()

        await self.app(scope, ReceiveCompatibilityRequest, send)


class PortDispatchApplication:
    """接続先ポートに応じて通常 API と互換 API を振り分ける ASGI アプリケーション。"""

    def __init__(
        self,
        main_app: ASGIApp,
        compatibility_app: ASGIApp,
        compatibility_port: int,
    ) -> None:
        self.main_app = main_app
        self.compatibility_app = compatibility_app
        self.compatibility_port = compatibility_port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Uvicorn は複数ソケットでも lifespan を一度だけ送る。
        # DB・定期更新・ストリーム共有状態の初期化と終了は通常アプリだけで実行する。
        if scope['type'] == 'lifespan':
            await self.main_app(scope, receive, send)
            return

        server_address = scope.get('server')
        if (
            scope['type'] in ('http', 'websocket') and
            server_address is not None and
            server_address[1] == self.compatibility_port
        ):
            await self.compatibility_app(scope, receive, send)
            return

        await self.main_app(scope, receive, send)


def CreateCompatibilityAPI(
    profile: Literal['KomorebiV1'] = 'KomorebiV1',
    trusted_proxy_cidrs: Sequence[str] | None = None,
) -> FastAPI:
    """既存の状態を共有しつつ UI を配信しない Komorebi 互換 API アプリを構築する。"""

    compatibility_app = FastAPI(
        title = 'KonomiTV Compatibility API',
        description = f'KonomiTV compatibility API profile: {profile}',
        version = VERSION,
        openapi_url = '/api/openapi.json',
        docs_url = '/api/docs',
        redoc_url = '/api/redoc',
    )

    # Komorebi が利用・宣言する method + path の組み合わせだけを公開する。
    # Web UI・サーバー管理 API に加え、同じルーターに含まれる再解析などの不要な操作 API も公開しない。
    IncludeKomorebiV1UpstreamRoutes(
        compatibility_app,
        (
            ChannelsRouter.router,
            ProgramsRouter.router,
            VideosRouter.router,
            LiveStreamsRouter.router,
            VideoStreamsRouter.router,
            ReservationsRouter.router,
            ReservationConditionsRouter.router,
        ),
    )
    compatibility_app.include_router(compatibility_live_streams_router)
    compatibility_app.include_router(compatibility_video_streams_router)
    compatibility_app.include_router(compatibility_videos_router)
    compatibility_app.include_router(compatibility_reservations_router)
    compatibility_app.include_router(version_router)
    compatibility_app.include_router(users_router)
    compatibility_app.include_router(histories_router)

    compatibility_app.add_middleware(KomorebiResponseMiddleware)
    compatibility_app.add_middleware(KomorebiReservationRequestMiddleware)

    # reverse_proxy モード時は、通常 API と同じ送信元検証を経た転送ヘッダーだけを信頼する。
    if trusted_proxy_cidrs is not None:
        compatibility_app.add_middleware(
            ReverseProxyMiddleware,
            trusted_proxy_cidrs = list(trusted_proxy_cidrs),
        )

    return compatibility_app
