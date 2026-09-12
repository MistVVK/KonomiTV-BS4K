from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import cast

from tortoise.expressions import Q
from tortoise.functions import Max

from app.constants import JST
from app.models.Program import Program
from app.models.RecordedEpisode import SeriesEpisode
from app.models.RecordedProgram import RecordedProgram
from app.models.Series import Series
from app.schemas import Genre, SeriesSummarySort


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


async def FetchCatalogRows(query: str) -> list[dict[str, object]]:
    """
    カタログカード構築用の Series 行を返す。

    検索キーワードによる title / description の行単位照合は従来どおり維持するが、
    その結果はどのカードを採用するかの判定にだけ使う。TMDb バインド行は 1 成员でも
    一致すればその作品 (tmdb_media_type, tmdb_id) のカードを採用し、成员・集約値は
    query なしの全 visible 行から構築する (検索に一致しない Season をカードから
    脱落させないため)。tmdb_id NULL の行は従来どおり行単位の採用。

    Args:
        query (str): 検索キーワード。空なら全 visible 行をそのまま返す。

    Returns:
        list[dict[str, object]]: カード単位への集約 (GroupCatalogRows) に渡す行。
    """

    catalog_values = (
        'id',
        'updated_at',
        'latest_recorded_start_time',
        'title_reading',
        'first_air_date',
        'tmdb_popularity',
        'tmdb_vote_average',
        'bangumi_rating',
        'tmdb_id',
        'tmdb_media_type',
        'tmdb_season_number',
    )

    async def _fetch_rows(search_query: str) -> list[dict[str, object]]:
        # 最新録画日時は updated_at とは別物 (録画追加で updated_at は更新されない) で、
        # サムネイル成员の選択に使う。ソートには使わない。
        return await VisibleSeriesQuery(search_query).annotate(
            latest_recorded_start_time=Max(
                'recorded_programs__start_time',
                _filter=Q(recorded_programs__recorded_video__status='Recorded'),
            ),
        ).values(*catalog_values)

    matched_rows = await _fetch_rows(query)
    if query.strip() == '':
        return matched_rows
    # 採用キー: バインド行は作品 key、非バインド行は行 id。
    adopted_works = {
        (row['tmdb_media_type'], row['tmdb_id'])
        for row in matched_rows
        if row['tmdb_id'] is not None
    }
    adopted_ids = {
        cast(int, row['id'])
        for row in matched_rows
        if row['tmdb_id'] is None
    }
    all_rows = await _fetch_rows('')
    return [
        row
        for row in all_rows
        if (
            row['tmdb_id'] is not None
            and (row['tmdb_media_type'], row['tmdb_id']) in adopted_works
        ) or (
            row['tmdb_id'] is None and cast(int, row['id']) in adopted_ids
        )
    ]


async def ListSeriesSummaries(
    *,
    sort: SeriesSummarySort,
    order: str,
    page: int,
    query: str = '',
) -> tuple[int, list[dict[str, object]]]:
    """
    カタログカード用の Series 要約をページングして返す。

    Args:
        sort (SeriesSummarySort): ソートキー。
        order (str): desc なら降順、asc なら昇順。
        page (int): 1 以上のページ番号。
        query (str): 検索キーワード。

    Returns:
        tuple[int, list[dict[str, object]]]: 総件数と要約辞書のリスト。
    """

    # ソートは全 ID 行を Python 側で行う。NULL の扱いと id の第 2 キーを
    ## DB の方言に寄せず同一条件で保証するため、values() の小さな行集合で完結させる。
    ## 検索キーワードはカード採用判定にだけ使い、成员・集約は全 visible 行から構築する。
    rows = await FetchCatalogRows(query)
    ordered_rows = SortSeriesRows(GroupCatalogRows(rows), sort=sort, order=order)
    total = len(ordered_rows)
    page_rows = ordered_rows[(page - 1) * CATALOG_PAGE_SIZE:page * CATALOG_PAGE_SIZE]
    # ページが空でも返却値の形状は同じでよい (total からページ数は算出できる)。
    member_ids = [
        member_id
        for group_row in page_rows
        for member_id in cast(list[int], group_row['member_ids'])
    ]
    series_by_id = {
        series.id: series
        for series in await Series.filter(id__in=member_ids)
    }
    summaries = [
        await BuildSeriesGroupSummary(group_row, series_by_id)
        for group_row in page_rows
        if any(
            member_id in series_by_id
            for member_id in cast(list[int], group_row['member_ids'])
        )
    ]
    return total, summaries


def GroupCatalogRows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """
    カタログ行を TMDb 作品単位のカード行へ集約する。

    tmdb_id が非 NULL の行は (tmdb_media_type, tmdb_id) で 1 カードにまとめ、
    それ以外 (bgm のみ / 外部なし) は 1 行 = 1 カードのまま返す。
    集約値は updated_at = max、first_air_date = min、tmdb_popularity / tmdb_vote_average =
    代表行の値 (グループ共通)、bangumi_rating = max、title_reading = 代表行の値とする。
    代表行は最小 season_number (NULL は末尾) → id 昇順で選び、行の id として残す。
    各行には成员の 'member_ids' (season 昇順)・'member_seasons'・'member_latest' を添える。

    Args:
        rows (list[dict[str, object]]): values() で取得した Series 行。

    Returns:
        list[dict[str, object]]: カード単位に集約した行。SortSeriesRows へそのまま渡せる形。
    """

    grouped_members: dict[tuple[object, object], list[dict[str, object]]] = defaultdict(list)
    single_rows: list[dict[str, object]] = []
    for row in rows:
        if row['tmdb_id'] is not None:
            grouped_members[(row['tmdb_media_type'], row['tmdb_id'])].append(row)
        else:
            single_rows.append(row)

    def _decorate_single(row: dict[str, object]) -> dict[str, object]:
        # 非グループ行も同じ形にそろえ、下流が成员の有無を意識しないで済むようにする。
        series_id = cast(int, row['id'])
        decorated = dict(row)
        decorated['member_ids'] = [series_id]
        decorated['member_seasons'] = {series_id: cast(int | None, row['tmdb_season_number'])}
        decorated['member_latest'] = {series_id: row['latest_recorded_start_time']}
        return decorated

    result = [_decorate_single(row) for row in single_rows]
    for members in grouped_members.values():
        # 代表行は最小の Season (未バインドは末尾) を持つ成员とし、カードの id と
        # title_reading / popularity / vote の出所をこの行に固定する。
        ordered_members = sorted(
            members,
            key=lambda member: (
                member['tmdb_season_number'] is None,
                cast(int | None, member['tmdb_season_number']) or 0,
                cast(int, member['id']),
            ),
        )
        representative = ordered_members[0]
        aggregated = dict(representative)
        aggregated['updated_at'] = max(
            cast(datetime, member['updated_at']) for member in members
        )
        latest_values = [
            cast(datetime, member['latest_recorded_start_time'])
            for member in members
            if member['latest_recorded_start_time'] is not None
        ]
        aggregated['latest_recorded_start_time'] = max(latest_values) if len(latest_values) > 0 else None
        first_air_values = [
            cast(date, member['first_air_date'])
            for member in members
            if member['first_air_date'] is not None
        ]
        aggregated['first_air_date'] = min(first_air_values) if len(first_air_values) > 0 else None
        rating_values = [
            cast(float, member['bangumi_rating'])
            for member in members
            if member['bangumi_rating'] is not None
        ]
        aggregated['bangumi_rating'] = max(rating_values) if len(rating_values) > 0 else None
        aggregated['member_ids'] = [cast(int, member['id']) for member in ordered_members]
        aggregated['member_seasons'] = {
            cast(int, member['id']): cast(int | None, member['tmdb_season_number'])
            for member in members
        }
        aggregated['member_latest'] = {
            cast(int, member['id']): member['latest_recorded_start_time']
            for member in members
        }
        result.append(aggregated)
    return result


def SortSeriesRows(
    rows: list[dict[str, object]],
    *,
    sort: SeriesSummarySort,
    order: str,
) -> list[dict[str, object]]:
    """
    ソートキー・順序に従って Series の行を並べ替える。

    ソート共通仕様: 第 2 キーは常に id (昇順) でページング越しの順序を安定化し、
    NULL は方向に関係なく常に末尾に置く。

    Args:
        rows (list[dict[str, object]]): id と各ソートカラムを含む Series の行。
        sort (SeriesSummarySort): ソートキー。
        order (str): desc なら降順、asc なら昇順。

    Returns:
        list[dict[str, object]]: ソート済みの行。
    """

    def _id(row: dict[str, object]) -> int:
        return cast(int, row['id'])

    # sort=updated_at は集約済み updated_at (グループでは成员の max) をそのまま使う。
    ## 応答値とソートキーを一致させるため、従来の latest_recorded_start_time への置換はしない。
    sort_field = sort
    null_rows = [row for row in rows if row[sort_field] is None]
    non_null_rows = [row for row in rows if row[sort_field] is not None]
    # 同一キーの行は常に id 昇順に固定し、安定ソートでページング越しの順序を保つ。
    null_rows.sort(key=_id)
    non_null_rows.sort(key=_id)
    # Python の安定ソートで primary だけを反転し、第2キーの id は常に昇順を保つ。
    non_null_rows.sort(
        key=lambda row: cast(date | datetime | float | str, row[sort_field]),
        reverse=order == 'desc',
    )
    return non_null_rows + null_rows


async def GetSeriesListPosition(
    *,
    series_id: int,
    sort: SeriesSummarySort,
    order: str,
    query: str = '',
) -> int | None:
    """
    カタログ一覧で指定 Series が載るページ番号を返す。

    Args:
        series_id (int): 展開したい Series ID。
        sort (SeriesSummarySort): 一覧と同じソートキー。
        order (str): 一覧と同じソート順序。
        query (str): 一覧と同じ検索キーワード。

    Returns:
        int | None: 1 以上のページ番号。一覧に無いとき None。
    """

    rows = await FetchCatalogRows(query)
    group_rows = SortSeriesRows(GroupCatalogRows(rows), sort=sort, order=order)
    # 成员 id が属するグループのページを返す (グループ化で代表行以外の id でも辿れる)。
    for index, group_row in enumerate(group_rows):
        if series_id in cast(list[int], group_row['member_ids']):
            return index // CATALOG_PAGE_SIZE + 1
    return None


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
        'tmdb_id': series.tmdb_id,
        'tmdb_media_type': series.tmdb_media_type,
        'tmdb_season_number': series.tmdb_season_number,
        'tmdb_name': series.tmdb_name,
        'tmdb_overview': series.tmdb_overview,
        'recorded_count': len(recorded_programs),
        'unrecorded_count': counts.unrecorded_count,
        'partial_count': counts.partial_count,
        'latest_recorded_program_id': latest_program.id if latest_program is not None else None,
        'updated_at': series.updated_at,
        # 非グループ経路 (放送中など) でも応答形状をそろえるため、自分自身1件の成员を入れる。
        # 一覧のグループ化では BuildSeriesGroupSummary がこの値を実メンバーで上書きする。
        'season_members': [{'series_id': series.id, 'season_number': series.tmdb_season_number}],
    }


async def BuildSeriesGroupSummary(
    group_row: dict[str, object],
    series_by_id: dict[int, Series],
) -> dict[str, object]:
    """
    カタログの 1 カード分 (TMDb 作品グループまたは単独 Series) の要約を作る。

    成员ごとの件数を合算し、updated_at は最大、サムネイルは最新録画を持つ成员の
    latest_recorded_program_id とする。表示名などの作品フィールドは代表行
    (group_row['id']) の成员由来のまま残す。

    Args:
        group_row (dict[str, object]): GroupCatalogRows が返す集約行。
        series_by_id (dict[int, Series]): 成员 Series の実体。

    Returns:
        dict[str, object]: カード表示に必要なフィールド。
    """

    member_ids = [
        member_id
        for member_id in cast(list[int], group_row['member_ids'])
        if member_id in series_by_id
    ]
    member_summaries = [
        await BuildSeriesSummary(series_by_id[member_id])
        for member_id in member_ids
    ]
    # 代表行の成员要約を土台にし、件数・更新日時・サムネイルだけをグループ集約で上書きする。
    summary = dict(member_summaries[0])
    summary['recorded_count'] = sum(
        cast(int, member['recorded_count']) for member in member_summaries
    )
    summary['unrecorded_count'] = sum(
        cast(int, member['unrecorded_count']) for member in member_summaries
    )
    summary['partial_count'] = sum(
        cast(int, member['partial_count']) for member in member_summaries
    )
    summary['updated_at'] = max(
        cast(datetime, member['updated_at']) for member in member_summaries
    )
    # サムネイルはグループ内で最新の録画を持つ成员から取る。全成员に録画が無いときは
    # 代表行の値 (None) のままになる。
    member_latest = cast(dict[int, object], group_row['member_latest'])
    thumbnail_member_id = max(
        member_ids,
        key=lambda member_id: (
            member_latest[member_id] is not None,
            cast(datetime, member_latest[member_id])
            if member_latest[member_id] is not None else datetime.min.replace(tzinfo=JST),
        ),
    )
    thumbnail_summary = member_summaries[member_ids.index(thumbnail_member_id)]
    summary['latest_recorded_program_id'] = thumbnail_summary['latest_recorded_program_id']
    summary['season_members'] = [
        {'series_id': member_id, 'season_number': cast(dict[int, int | None], group_row['member_seasons'])[member_id]}
        for member_id in member_ids
    ]
    return summary


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
