
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status
from tortoise.expressions import Q

from app import logging, schemas
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
        .prefetch_related(
            'broadcast_periods__channel',
            'broadcast_periods__recorded_programs__recorded_video',
            'broadcast_periods__recorded_programs__channel',
            'broadcast_periods__recorded_programs__series_episode',
            'broadcast_periods__recorded_programs__episode_resolution',
        ) \
        .order_by('-updated_at' if order == 'desc' else 'updated_at') \
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
        .prefetch_related(
            'broadcast_periods__channel',
            'broadcast_periods__recorded_programs__recorded_video',
            'broadcast_periods__recorded_programs__channel',
            'broadcast_periods__recorded_programs__series_episode',
            'broadcast_periods__recorded_programs__episode_resolution',
        ) \
        .order_by('-updated_at' if order == 'desc' else 'updated_at') \
        .offset((page - 1) * PAGE_SIZE) \
        .limit(PAGE_SIZE)

    # 検索条件に一致する総件数を取得
    total = len(await visible_series_query.values_list('id', flat=True))

    return {
        'total': total,
        'series_list': series_list,
    }


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
