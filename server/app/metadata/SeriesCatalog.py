from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from tortoise.expressions import Q

from app.constants import JST
from app.models.Program import Program
from app.models.RecordedEpisode import SeriesEpisode
from app.models.RecordedProgram import RecordedProgram
from app.models.Series import Series
from app.schemas import Genre


# カタログ一覧は HonomiTV と同じ 50 件ページングにする。
CATALOG_PAGE_SIZE = 50

# 放送中グリッドは EPG のいま放送中ではなく、今クール相当の週間レギュラーだけを載せる。
ON_AIR_MAJOR_GENRES = {'アニメ・特撮', 'ドラマ', 'バラエティ', '音楽'}
ON_AIR_EXCLUDED_MAJORS = {'映画'}
ON_AIR_EXCLUDED_MIDDLES = {'紀行', 'ドキュメンタリー', '映画'}
ON_AIR_LOOKBACK_DAYS = 21
ON_AIR_EPG_LOOKAHEAD_DAYS = 8
ON_AIR_SLOT_MINUTES = 5


@dataclass(frozen=True, slots=True)
class SeriesCatalogCounts:
    """カタログカードに出す未録画・部分録画の件数。"""

    unrecorded_count: int
    partial_count: int


def IsOnAirGenre(genres: list[Genre]) -> bool:
    """
    放送中グリッドへ載せるジャンルかどうかを返す。

    Args:
        genres (list[Genre]): Series に保存されたジャンル。

    Returns:
        bool: アニメ・ドラマ・バラエティ・音楽で、映画・紀行・ドキュメンタリーでないとき True。
    """

    if len(genres) == 0:
        return False
    if any(genre['major'] in ON_AIR_EXCLUDED_MAJORS for genre in genres):
        return False
    if any(genre['middle'] in ON_AIR_EXCLUDED_MIDDLES for genre in genres):
        return False
    return any(genre['major'] in ON_AIR_MAJOR_GENRES for genre in genres)


def IsRebroadcastTitle(title: str) -> bool:
    """
    再放送マーク付きのタイトルかを返す。

    Args:
        title (str): 録画または EPG の番組タイトル。

    Returns:
        bool: `[再]` を含むとき True。
    """

    return '[再]' in title


def BroadcastSlotKey(recorded_program: RecordedProgram) -> str:
    """
    構造化 Episode がない録画を、年・日付・開始時刻の放送枠として識別する。

    Args:
        recorded_program (RecordedProgram): 対象録画。

    Returns:
        str: 同日別番組を区別し、複数局の同一枠は同じになるキー。
    """

    local_time = recorded_program.start_time.astimezone(JST)
    return local_time.strftime('%Y-%m-%d %H:%M')


def CatalogCellKey(recorded_program: RecordedProgram) -> tuple[str, str] | None:
    """
    詳細行列と同じ局・話数または日付スロットのセル識別子を返す。

    Args:
        recorded_program (RecordedProgram): 対象録画。

    Returns:
        tuple[str, str] | None: (channel_id, column_key)。局が不明で表示セルを作れないとき None。
    """

    if recorded_program.channel_id is None:
        return None
    if recorded_program.series_episode_id is not None:
        return (recorded_program.channel_id, f'episode:{recorded_program.series_episode_id}')
    return (recorded_program.channel_id, f'date:{BroadcastSlotKey(recorded_program)}')


def RoundStartMinutes(start_time: datetime) -> tuple[int, int, int]:
    """
    自然時刻の曜日と、5 分単位に丸めた時分を返す。

    Args:
        start_time (datetime): 放送開始時刻。

    Returns:
        tuple[int, int, int]: (weekday, hour, minute)。weekday は月曜=0。
    """

    local_time = start_time.astimezone(JST)
    rounded_minute = (local_time.minute // ON_AIR_SLOT_MINUTES) * ON_AIR_SLOT_MINUTES
    return (local_time.weekday(), local_time.hour, rounded_minute)


def VisibleSeriesQuery(query: str = ''):
    """
    再生可能録画を持つ Series の QuerySet を返す。

    Args:
        query (str): title / description の部分一致。空なら絞らない。

    Returns:
        QuerySet: distinct 済みの Series。
    """

    series_query = Series.filter(
        broadcast_periods__recorded_programs__recorded_video__status='Recorded',
    )
    stripped_query = query.strip()
    if stripped_query != '':
        series_query = series_query.filter(
            Q(title__icontains=stripped_query) | Q(description__icontains=stripped_query),
        )
    return series_query.distinct()


async def ListSeriesSummaries(
    *,
    order: str,
    page: int,
    query: str = '',
) -> tuple[int, list[dict[str, object]]]:
    """
    カタログカード用の Series 要約をページングして返す。

    Args:
        order (str): desc なら更新が新しい順、asc なら古い順。
        page (int): 1 以上のページ番号。
        query (str): 検索キーワード。

    Returns:
        tuple[int, list[dict[str, object]]]: 総件数と要約辞書のリスト。
    """

    series_query = VisibleSeriesQuery(query)
    ordered_query = series_query.order_by('-updated_at' if order == 'desc' else 'updated_at')
    total = len(await series_query.values_list('id', flat=True))
    series_rows = await ordered_query.offset((page - 1) * CATALOG_PAGE_SIZE).limit(CATALOG_PAGE_SIZE)
    summaries = [await BuildSeriesSummary(series) for series in series_rows]
    return total, summaries


async def GetSeriesListPosition(
    *,
    series_id: int,
    order: str,
    query: str = '',
) -> int | None:
    """
    カタログ一覧で指定 Series が載るページ番号を返す。

    Args:
        series_id (int): 展開したい Series ID。
        order (str): 一覧と同じソート。
        query (str): 一覧と同じ検索キーワード。

    Returns:
        int | None: 1 以上のページ番号。一覧に無いとき None。
    """

    series_ids = cast(
        list[int],
        await VisibleSeriesQuery(query).order_by(
            '-updated_at' if order == 'desc' else 'updated_at',
        ).values_list('id', flat=True),
    )
    try:
        index = series_ids.index(series_id)
    except ValueError:
        return None
    return index // CATALOG_PAGE_SIZE + 1


async def BuildSeriesSummary(series: Series) -> dict[str, object]:
    """
    1 件の Series からカタログカード用の要約を作る。

    Args:
        series (Series): 再生可能録画を持つ Series。

    Returns:
        dict[str, object]: カード表示に必要なフィールド。
    """

    recorded_programs = await RecordedProgram.filter(
        series_id=series.id,
        recorded_video__status='Recorded',
    ).order_by('-start_time')
    counts = await CountCatalogGaps(series.id, recorded_programs)
    latest_program = recorded_programs[0] if len(recorded_programs) > 0 else None
    return {
        'id': series.id,
        'title': series.title,
        'description': series.description,
        'genres': series.genres,
        'bangumi_subject_id': series.bangumi_subject_id,
        'bangumi_subject_name': series.bangumi_subject_name,
        'bangumi_subject_name_cn': series.bangumi_subject_name_cn,
        'bangumi_subject_summary': series.bangumi_subject_summary,
        'bangumi_subject_image_url': series.bangumi_subject_image_url,
        'tmdb_id': series.tmdb_id,
        'tmdb_media_type': series.tmdb_media_type,
        'tmdb_name': series.tmdb_name,
        'tmdb_overview': series.tmdb_overview,
        'tmdb_poster_url': series.tmdb_poster_url,
        'tmdb_backdrop_url': series.tmdb_backdrop_url,
        'recorded_count': len(recorded_programs),
        'unrecorded_count': counts.unrecorded_count,
        'partial_count': counts.partial_count,
        'latest_recorded_program_id': latest_program.id if latest_program is not None else None,
        'updated_at': series.updated_at,
    }


async def CountCatalogGaps(
    series_id: int,
    recorded_programs: list[RecordedProgram],
) -> SeriesCatalogCounts:
    """
    構造化 Episode を正本にした欠番と、完全版が無い部分録画の件数を数える。

    Args:
        series_id (int): 対象 Series ID。
        recorded_programs (list[RecordedProgram]): 同一 Series の録画。

    Returns:
        SeriesCatalogCounts: 未録画件数と部分録画件数。
    """

    # 第1話から全件を欠番にしない。Indexer が作った SeriesEpisode だけを列の正本にする。
    structured_episodes = await SeriesEpisode.filter(series_id=series_id).all()
    recorded_episode_ids = {
        recorded_program.series_episode_id
        for recorded_program in recorded_programs
        if recorded_program.series_episode_id is not None
    }
    unrecorded_count = 0
    if len(structured_episodes) > 0:
        unrecorded_count = sum(
            1 for episode in structured_episodes if episode.id not in recorded_episode_ids
        )

    complete_cell_keys: set[tuple[str, str]] = set()
    for recorded_program in recorded_programs:
        if recorded_program.is_partially_recorded:
            continue
        cell_key = CatalogCellKey(recorded_program)
        if cell_key is not None:
            complete_cell_keys.add(cell_key)

    # 詳細行列と同じ局×話数・日付スロットを 1 セルとして、部分録画の警告数を数える。
    partial_without_complete_keys: set[tuple[str, str]] = set()
    for recorded_program in recorded_programs:
        if recorded_program.is_partially_recorded is False:
            continue
        cell_key = CatalogCellKey(recorded_program)
        if cell_key is None or cell_key in complete_cell_keys:
            continue
        partial_without_complete_keys.add(cell_key)
    return SeriesCatalogCounts(
        unrecorded_count=unrecorded_count,
        partial_count=len(partial_without_complete_keys),
    )


async def ListOnAirDays() -> list[dict[str, object]]:
    """
    今クール相当の週間レギュラーを、月曜始まりの 7 列へまとめる。

    Returns:
        list[dict[str, object]]: 曜日ごとのスロット。時刻は自然時刻のまま。
    """

    now = datetime.now(tz=JST)
    lookback_start = now - timedelta(days=ON_AIR_LOOKBACK_DAYS)
    recorded_programs = await RecordedProgram.filter(
        series_id__not_isnull=True,
        recorded_video__status='Recorded',
        start_time__gte=lookback_start,
    ).prefetch_related('series', 'channel')

    slot_programs: dict[tuple[int, int, int, int], list[RecordedProgram]] = defaultdict(list)
    for recorded_program in recorded_programs:
        if recorded_program.series is None:
            continue
        if IsOnAirGenre(recorded_program.series.genres) is False:
            continue
        if IsRebroadcastTitle(recorded_program.title):
            continue
        weekday, hour, minute = RoundStartMinutes(recorded_program.start_time)
        assert recorded_program.series_id is not None
        slot_programs[(recorded_program.series_id, weekday, hour, minute)].append(recorded_program)

    epg_programs = await Program.filter(
        start_time__gte=now,
        start_time__lte=now + timedelta(days=ON_AIR_EPG_LOOKAHEAD_DAYS),
    ).all()
    # 深夜に翌日の夜枠まで注目扱いにしないよう、夜間は当該夜の 20:00〜翌 05:00 に固定する。
    if now.hour >= 20 or now.hour < 5:
        featured_start = now.replace(hour=20, minute=0, second=0, microsecond=0)
        # 日付が変わった後も、前日 20:00 に始まった放送日の窓を使う。
        if now.hour < 5:
            featured_start -= timedelta(days=1)
        featured_end = featured_start + timedelta(hours=9)
    else:
        # 日中は従来どおり、直近 3 時間から翌 05:00 までを注目扱いにする。
        featured_start = now - timedelta(hours=3)
        featured_end = (now + timedelta(days=1)).replace(hour=5, minute=0, second=0, microsecond=0)

    days: list[dict[str, object]] = [{'weekday': weekday, 'slots': []} for weekday in range(7)]
    seen_series_weekdays: set[tuple[int, int]] = set()
    for (series_id, weekday, hour, minute), programs in sorted(
        slot_programs.items(),
        key=lambda item: (item[0][1], item[0][2], item[0][3], item[0][0]),
    ):
        series = programs[0].series
        if series is None:
            continue
        if (series_id, weekday) in seen_series_weekdays:
            continue
        if len(programs) < 2 and HasUpcomingEpisode(series, programs, epg_programs, now) is False:
            continue
        seen_series_weekdays.add((series_id, weekday))
        summary = await BuildSeriesSummary(series)
        # 前日分を含む当該夜の枠を翌週へ送らないよう、注目窓の開始からスロット時刻を求める。
        slot_datetime = NextSlotDatetime(featured_start, weekday, hour, minute)
        is_featured = featured_start <= slot_datetime < featured_end
        day_slots = days[weekday]['slots']
        assert isinstance(day_slots, list)
        day_slots.append({
            'weekday': weekday,
            'hour': hour,
            'minute': minute,
            'is_featured': is_featured,
            'series': summary,
        })
    return days


def HasUpcomingEpisode(
    series: Series,
    recent_programs: list[RecordedProgram],
    epg_programs: list[Program],
    now: datetime,
) -> bool:
    """
    録画が 1 件でも、未来 8 日の EPG に次回話があるかを返す。

    Args:
        series (Series): 対象 Series。
        recent_programs (list[RecordedProgram]): 同じスロットの直近録画。
        epg_programs (list[Program]): 未来 8 日の番組。
        now (datetime): 現在時刻。

    Returns:
        bool: 同じチャンネルでタイトルが作品名から始まる番組があれば True。
    """

    channel_ids = {
        recorded_program.channel_id
        for recorded_program in recent_programs
        if recorded_program.channel_id is not None
    }
    if len(channel_ids) == 0:
        return False
    title_prefix = series.title
    for program in epg_programs:
        if program.start_time < now:
            continue
        if program.channel_id not in channel_ids:
            continue
        if IsRebroadcastTitle(program.title):
            continue
        if program.title.startswith(title_prefix):
            return True
    return False


def NextSlotDatetime(now: datetime, weekday: int, hour: int, minute: int) -> datetime:
    """
    今週以降で最初に訪れるスロット時刻を返す。

    Args:
        now (datetime): 現在時刻。
        weekday (int): 月曜=0。
        hour (int): 自然時刻の時。
        minute (int): 自然時刻の分。

    Returns:
        datetime: タイムゾーン付きの次回スロット。
    """

    days_ahead = (weekday - now.weekday()) % 7
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
    if candidate < now - timedelta(hours=3):
        candidate += timedelta(days=7)
    return candidate
