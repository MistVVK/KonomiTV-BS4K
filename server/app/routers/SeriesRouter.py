
import hashlib
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response, status
from tortoise.expressions import Q
from tortoise.functions import Max

from app import logging, schemas
from app.metadata.KonomiTVBS4KSeriesImage import KonomiTVBS4KSeriesImage
from app.metadata.SeriesCatalog import (
    CATALOG_PAGE_SIZE,
    GetSeriesListPosition,
    ListOnAirDays,
    ListSeriesSummaries,
)
from app.models.Series import Series


# ルーター
router = APIRouter(
    tags = ['Series'],
    prefix = '/api/series',
)

# ページングで一度に取得するシリーズ番組の数
PAGE_SIZE = 30


@router.get(
    '',
    summary = 'シリーズ番組一覧 API',
    response_description = 'シリーズ番組のリスト。',
    response_model = schemas.SeriesList,
)
async def SeriesListAPI(
    order: Annotated[Literal['desc', 'asc'], Query(description='ソート順序 (desc or asc) 。')] = 'desc',
    page: Annotated[int, Query(description='ページ番号。')] = 1,
):
    """
    すべてのシリーズ番組を一度に 100 件ずつ取得する。<br>
    order には "desc" か "asc" を指定する。<br>
    page (ページ番号) には 1 以上の整数を指定する。
    """

    visible_series_query = Series.filter(
        broadcast_periods__recorded_programs__recorded_video__status='Recorded',
    ).distinct()
    series_list = await visible_series_query \
        .annotate(latest_recorded_start_time=Max(
            'recorded_programs__start_time',
            _filter=Q(recorded_programs__recorded_video__status='Recorded'),
        )) \
        .prefetch_related(
            'episodes',
            'broadcast_periods__channel',
            'broadcast_periods__recorded_programs__recorded_video',
            'broadcast_periods__recorded_programs__channel',
            'broadcast_periods__recorded_programs__series_episode',
            'broadcast_periods__recorded_programs__episode_resolution',
        ) \
        .order_by(
            '-latest_recorded_start_time' if order == 'desc' else 'latest_recorded_start_time',
            'id',
        ) \
        .offset((page - 1) * PAGE_SIZE) \
        .limit(PAGE_SIZE) \

    return {
        # QuerySet.count() は利用中の Tortoise ORM 版で distinct を引き継がないため、
        # reverse joinの録画数ではなくdistinctなSeries ID数を数える。
        'total': len(await visible_series_query.values_list('id', flat=True)),
        'series_list': series_list,
    }


@router.get(
    '/search',
    summary = 'シリーズ番組検索 API',
    response_description = '検索条件に一致するシリーズ番組のリスト。',
    response_model = schemas.SeriesList,
)
async def SeriesSearchAPI(
    query: Annotated[str, Query(description='検索キーワード。title または description のいずれかに部分一致するシリーズ番組を検索する。')] = '',
    order: Annotated[Literal['desc', 'asc'], Query(description='ソート順序 (desc or asc) 。')] = 'desc',
    page: Annotated[int, Query(description='ページ番号。')] = 1,
):
    """
    指定されたキーワードでシリーズ番組を一度に 100 件ずつ検索する。<br>
    キーワードは title または description のいずれかに部分一致するシリーズ番組を検索する。<br>
    order には "desc" か "asc" を指定する。<br>
    page (ページ番号) には 1 以上の整数を指定する。
    """

    # クエリが空の場合は全件取得と同じ挙動にする
    if not query:
        return await SeriesListAPI(order=order, page=page)

    # 検索条件を構築
    # title または description のいずれかに部分一致するレコードを検索
    visible_series_query = Series.filter(
        broadcast_periods__recorded_programs__recorded_video__status='Recorded',
    ).filter(
        Q(title__icontains=query) |
        Q(description__icontains=query)
    ).distinct()
    series_list = await visible_series_query \
        .annotate(latest_recorded_start_time=Max(
            'recorded_programs__start_time',
            _filter=Q(recorded_programs__recorded_video__status='Recorded'),
        )) \
        .prefetch_related(
            'episodes',
            'broadcast_periods__channel',
            'broadcast_periods__recorded_programs__recorded_video',
            'broadcast_periods__recorded_programs__channel',
            'broadcast_periods__recorded_programs__series_episode',
            'broadcast_periods__recorded_programs__episode_resolution',
        ) \
        .order_by(
            '-latest_recorded_start_time' if order == 'desc' else 'latest_recorded_start_time',
            'id',
        ) \
        .offset((page - 1) * PAGE_SIZE) \
        .limit(PAGE_SIZE)

    # 検索条件に一致する総件数を取得
    total = len(await visible_series_query.values_list('id', flat=True))

    return {
        'total': total,
        'series_list': series_list,
    }


@router.get(
    '/summary',
    summary = 'シリーズカタログ要約 API',
    response_description = 'カタログカード用のシリーズ要約リスト。',
    response_model = schemas.SeriesSummaryList,
)
async def SeriesSummaryListAPI(
    sort: Annotated[schemas.SeriesSummarySort, Query(description='ソートキー。既定は updated_at。')] = 'updated_at',
    order: Annotated[Literal['desc', 'asc'], Query(description='ソート順序 (desc or asc) 。')] = 'desc',
    page: Annotated[int, Query(description='ページ番号。')] = 1,
    query: Annotated[str, Query(description='title または description の部分一致。')] = '',
):
    """
    再生可能録画を持つシリーズを、カード一覧向けの要約だけ 50 件ずつ返す。
    """

    if page < 1:
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'page must be 1 or greater',
        )
    total, series_list = await ListSeriesSummaries(sort=sort, order=order, page=page, query=query)
    return schemas.SeriesSummaryList(
        total=total,
        page_size=CATALOG_PAGE_SIZE,
        series_list=[schemas.SeriesSummary.model_validate(summary) for summary in series_list],
    )


@router.get(
    '/on-air',
    summary = 'シリーズ放送中グリッド API',
    response_description = '今クール相当の週間レギュラー。',
    response_model = schemas.SeriesOnAirResponse,
)
async def SeriesOnAirAPI():
    """
    ローカル録画から推定した今クール相当の週間レギュラーを、月曜始まりで返す。<br>
    EPG のいま放送中一覧ではない。曜日と時刻は自然時刻のまま。
    """

    days = await ListOnAirDays()
    return schemas.SeriesOnAirResponse(
        days=[schemas.SeriesOnAirDay.model_validate(day) for day in days],
    )


@router.get(
    '/list-position',
    summary = 'シリーズカタログ位置 API',
    response_description = '指定シリーズが載るカタログページ番号。',
    response_model = schemas.SeriesListPosition,
)
async def SeriesListPositionAPI(
    series_id: Annotated[int, Query(description='展開したいシリーズ ID。')],
    sort: Annotated[schemas.SeriesSummarySort, Query(description='ソートキー。既定は updated_at。')] = 'updated_at',
    order: Annotated[Literal['desc', 'asc'], Query(description='ソート順序 (desc or asc) 。')] = 'desc',
    query: Annotated[str, Query(description='title または description の部分一致。')] = '',
):
    """
    `/series/:id` の深いリンクから、同じ検索・並びのカタログ何ページ目かを返す。
    """

    page = await GetSeriesListPosition(series_id=series_id, sort=sort, order=order, query=query)
    if page is None:
        logging.warning(
            f'[SeriesRouter][SeriesListPositionAPI] Specified series_id was not found. [series_id: {series_id}]',
        )
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified series_id was not found',
        )
    return schemas.SeriesListPosition(page=page)


@router.get(
    '/{series_id}/poster',
    summary = 'シリーズ表紙 API',
    response_description = 'ローカル保存済みの WebP 表紙。',
    response_model = None,
)
async def SeriesPosterAPI(
    series_id: Annotated[int, Path(description='シリーズ番組の ID 。')],
    request: Request,
) -> Response:
    """
    Bangumi、TMDb の順で WebP 表紙を返し、同じ URL の差し替えを ETag で再検証する。

    Args:
        series_id (int): シリーズ番組の ID。
        request (Request): If-None-Match を含む HTTP リクエスト。

    Returns:
        Response: WebP、または ETag が一致する場合の 304。

    Raises:
        HTTPException: Series または取得可能な表紙が存在しない場合の 404。
    """

    series = await Series.get_or_none(id=series_id)
    if series is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Series poster was not found')
    # パスではなくバイト列を受け取るため、invalidation の unlink と配信が競合しない。
    poster_bytes = await KonomiTVBS4KSeriesImage.ensurePoster(series)
    if poster_bytes is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Series poster was not found')

    # WebP は同じ API URL で差し替えるため immutable にせず、内容ハッシュの ETag を毎回再検証させる。
    etag = f'"{hashlib.sha256(poster_bytes).hexdigest()}"'
    response_headers = {
        'Cache-Control': 'public, max-age=0, must-revalidate',
        'ETag': etag,
    }
    if _IsETagMatched(request.headers.get('If-None-Match'), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=response_headers)
    return Response(
        content=poster_bytes,
        media_type='image/webp',
        headers=response_headers,
    )


def _IsETagMatched(if_none_match: str | None, current_etag: str) -> bool:
    """
    GET の If-None-Match を現在の表紙と比較し、一致するか返す。

    Args:
        if_none_match (str | None): ブラウザが送った If-None-Match。
        current_etag (str): 現在の表紙に付与する ETag。

    Returns:
        bool: `*` またはいずれかのタグが現在値と一致する場合は True。
    """

    if if_none_match is None:
        return False
    normalized_current = current_etag.removeprefix('W/').strip()
    for raw_etag in if_none_match.split(','):
        candidate = raw_etag.strip()
        if candidate == '*':
            return True
        if candidate.removeprefix('W/').strip() == normalized_current:
            return True
    return False


@router.get(
    '/{series_id}',
    summary = 'シリーズ番組 API',
    response_description = 'シリーズ番組。',
    response_model = schemas.Series,
)
async def SeriesAPI(
    series_id: Annotated[int, Path(description='シリーズ番組の ID 。')],
):
    """
    指定されたシリーズ番組を取得する。
    """

    series = await Series.filter(
        broadcast_periods__recorded_programs__recorded_video__status='Recorded',
    ).distinct() \
        .prefetch_related(
            'episodes',
            'broadcast_periods__channel',
            'broadcast_periods__recorded_programs__recorded_video',
            'broadcast_periods__recorded_programs__channel',
            'broadcast_periods__recorded_programs__series_episode',
            'broadcast_periods__recorded_programs__episode_resolution',
        ) \
        .get_or_none(id=series_id)
    if series is None:
        logging.warning(f'[SeriesRouter][SeriesAPI] Specified series_id was not found. [series_id: {series_id}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified series_id was not found',
        )

    return series
