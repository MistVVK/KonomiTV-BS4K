"""公式カタログ（Bangumi / TMDb）の話数へ録画を直接結び付ける。

EpisodeLookup（AI Web 検索）より前段の決定論的照合。同一日の放送日・air_date、
題名・副題と episode name の正規化、尺の順で絞り、独一のときだけ採用する。
カタログが無い・取得できない・独一でないときは None を返し、呼び出し側は
既存の EpisodeLookup 経路へ落とす。手動確定の上書きは行わない（適用可否の
判断は呼び出し側の RecordedEpisodeAutomation が持つ）。

権威の順序は Bangumi が先、TMDb は Bangumi カタログが無いときだけ使う。
両方ある Series の話数構造を TMDb 側へ切り替えることはしない。

照合結果は3状態で返す。Matched（独一採用）、Undecided（カタログ取得済み・
非独一）、Unknown（カタログ不在・取得失敗・無効）。Undecided と Unknown を
区別し、Undecided のときだけ呼び出し側は整数 fast path を抑止して
EpisodeLookup へ進める。Unknown は既存動作のままとする。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

import httpx

from app import logging
from app.constants import JST
from app.metadata.RecordedEpisodeResolver import (
    ParseLegacyEpisodeNumber,
    ParseSinglePositiveIntegerEpisode,
)
from app.metadata.RecordedSeriesSettings import (
    IsBangumiExternalMetadataEnabled,
    IsTmdbExternalMetadataEnabled,
)
from app.metadata.SeriesIndexer import NormalizeSeriesTitle, ParseJapaneseNumber
from app.models.RecordedEpisode import SeriesEpisode
from app.models.Series import Series
from app.utils.KonomiTVBS4KBangumiSharedStore import KonomiTVBS4KBangumiSharedStore
from app.utils.KonomiTVBS4KTmdbStore import KonomiTVBS4KTmdbStore


# 録画尺（CM・マージン込み）とカタログ尺（本編）の差の許容値。
## 日曜劇場の初回拡大枠など、放送枠延長と CM を吸収できる幅にする。
## 独一性は題名・放送日で担保し、尺は同点候補の打破と明らかな不一致の veto にだけ使う。
_DURATION_TOLERANCE_SECONDS = 900

# 話数トークンとして扱う表記。素の括弧数字や作品名内の数字（ラファエル6 など）は
## 通し番号の誤読になるため拾わない。
_EPISODE_TOKEN_PATTERN = re.compile(
    r'第\s*(?P<kanji>[0-9０-９一二三四五六七八九十百千〇零壱弐参拾貳肆伍陸漆玖]+)\s*[話回]'
    r'|#\s*(?P<hash>[0-9０-９]+)'
    r'|\bEPISODE\s*(?P<episode>[0-9]+)'
    r'|\bEP\.?\s*(?P<ep>[0-9]+)',
    re.IGNORECASE,
)
_CATALOG_AIR_DATE_PATTERN = re.compile(r'(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})')
_BANGUMI_DURATION_PATTERN = re.compile(r'(?P<minutes>\d+)\s*(?:分|min\.?|m)\b', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CatalogBoundEpisode:
    """カタログ照合で独一に決まったシーズン・話数。"""

    season_number: int
    episode_number: Decimal
    catalog: Literal['Bangumi', 'TMDb']
    catalog_episode_id: int
    # 採用話の所属 ID（Bangumi 条目 ID / TMDb TV 作品 ID）。後処理の ID 引き継ぎに使う。
    catalog_subject_id: int | None


@dataclass(frozen=True, slots=True)
class CatalogBindResult:
    """カタログ照合の3状態。呼び出し側の後続分岐を決める。"""

    status: Literal['Matched', 'Undecided', 'Unknown']
    bound: CatalogBoundEpisode | None = None


@dataclass(frozen=True, slots=True)
class _CatalogCandidate:
    """絞り込み対象のカタログ話数1件。"""

    season_number: int
    episode_number: int
    catalog_episode_id: int
    # 正規化済みの episode name（Bangumi は name_cn も含む）。
    name_keys: frozenset[str]
    # 名前に含まれる話数トークン（第十一話→11 など）。
    number_tokens: frozenset[int]
    # カタログ上の放送日。不明なときは None。
    air_date: date | None
    # カタログ上の本編尺（秒）。不明なときは None。
    runtime_seconds: int | None


def ExtractCatalogEpisodeNumbers(value: str | None) -> set[int]:
    """題名・副題・episode name から話数トークンだけを整数で抜き出す。

    Args:
        value: EPG 題名・副題、またはカタログの episode name。

    Returns:
        見つかった話数トークンの集合。素の括弧数字や作品名内の数字は含まない。
    """

    if not value:
        return set()
    normalized = unicodedata.normalize('NFKC', value)
    numbers: set[int] = set()
    for match in _EPISODE_TOKEN_PATTERN.finditer(normalized):
        raw_number = (
            match.group('kanji')
            or match.group('hash')
            or match.group('episode')
            or match.group('ep')
        )
        if raw_number is None:
            continue
        if raw_number.isdecimal():
            parsed_number = int(raw_number)
        else:
            parsed_number = ParseJapaneseNumber(raw_number)
        # 0 は話数トークンとして扱わない。
        if parsed_number is not None and parsed_number > 0:
            numbers.add(parsed_number)
    return numbers


def ParseCatalogAirDate(value: object) -> date | None:
    """カタログの放送日（YYYY-MM-DD）を日付へ変換する。

    Args:
        value: Bangumi の airdate または TMDb の air_date。

    Returns:
        変換できた日付。不明・不正な形式なら None。
    """

    if not isinstance(value, str) or value == '':
        return None
    match = _CATALOG_AIR_DATE_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    try:
        return date(
            int(match.group('year')),
            int(match.group('month')),
            int(match.group('day')),
        )
    except ValueError:
        return None


def ParseBangumiDurationSeconds(value: object) -> int | None:
    """Bangumi の duration 表記（24分など）を秒へ変換する。

    Args:
        value: Bangumi episode の duration。

    Returns:
        本編尺（秒）。読めない表記なら None。
    """

    if not isinstance(value, str) or value == '':
        return None
    match = _BANGUMI_DURATION_PATTERN.search(unicodedata.normalize('NFKC', value))
    if match is None:
        return None
    try:
        minutes = int(match.group('minutes'))
    except ValueError:
        return None
    return minutes * 60 if minutes > 0 else None


def RecordingDate(start_time: datetime) -> date:
    """録画開始時刻を JST の放送日へ変換する。

    Args:
        start_time: RecordedProgram の開始時刻。

    Returns:
        JST 日付。naive な時刻は JST として扱う。
    """

    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=JST)
    return start_time.astimezone(JST).date()


def NarrowCatalogCandidates(
    candidates: list[_CatalogCandidate],
    *,
    recording_date: date | None,
    title_keys: set[str],
    number_tokens: set[int],
    duration_seconds: float | None,
) -> _CatalogCandidate | None:
    """放送日・題名・尺の順で絞り、独一のときだけ候補を返す。

    放送日はカタログ側の誤記・吹替遅延で外れることがあるため、全滅時は
    捨てずに全候補へ戻す（backoff）。題名は必須とし、放送日だけの一致では
    採用しない。同点候補が残ったときだけ尺で打破し、それでも独一でなければ
    採用しない。

    Args:
        candidates: 同一 Series のカタログ話数一覧。
        recording_date: 録画の JST 放送日。不明なときは None。
        title_keys: 録画題名・副題の正規化キー。
        number_tokens: 録画題名・副題の話数トークン。
        duration_seconds: 録画尺（秒）。不明なときは None。

    Returns:
        独一に決まった候補。決まらなければ None。
    """

    if len(candidates) == 0:
        return None
    # 同一日の放送日で絞る。全滅時は日付情報を捨てて題名へ進む。
    if recording_date is not None:
        date_matched = [
            candidate for candidate in candidates
            if candidate.air_date == recording_date
        ]
        narrowed = date_matched or candidates
    else:
        narrowed = candidates
    # 題名・副題と episode name の正規化一致、または話数トークンの共有は必須。
    ## ここで空になれば EpisodeLookup へ落とす。
    titled = [
        candidate for candidate in narrowed
        if candidate.name_keys & title_keys or not candidate.number_tokens.isdisjoint(number_tokens)
    ]
    if len(titled) == 0:
        return None
    if len(titled) == 1:
        single = titled[0]
        # 尺が双方で判るときだけ、明らかな不一致を veto する。
        if (
            single.runtime_seconds is not None
            and duration_seconds is not None
            and abs(single.runtime_seconds - duration_seconds) > _DURATION_TOLERANCE_SECONDS
        ):
            return None
        return single
    # 複数候補の打破にだけ尺を使う。尺不明の候補が混ざると独一性を証明
    ## できないため、そのときは採用しない。
    if duration_seconds is None or any(
        candidate.runtime_seconds is None for candidate in titled
    ):
        return None
    duration_matched = [
        candidate for candidate in titled
        if abs(candidate.runtime_seconds - duration_seconds) <= _DURATION_TOLERANCE_SECONDS  # type: ignore[operator]
    ]
    if len(duration_matched) != 1:
        return None
    return duration_matched[0]


async def TryBindCatalogEpisode(
    *,
    series_id: int,
    title: str,
    subtitle: str | None,
    start_time: datetime,
    duration_seconds: float | None,
) -> CatalogBindResult:
    """Series の公式カタログへ録画を独一照合し、シーズン込み話数を返す。

    Bangumi 条目があり、取得できたカタログが空でなければ Bangumi を正とし、
    空カタログのときだけ TMDb へ進める。取得失敗時は権威を切り替えず Unknown
    とする。非独一は Undecided とし、呼び出し側で整数 fast path を抑止する。

    Args:
        series_id: 所属 Series ID。
        title: EPG 番組タイトル。
        subtitle: EPG 副題。
        start_time: 録画開始時刻。
        duration_seconds: 録画尺（秒）。不明なときは None。

    Returns:
        照合の3状態。Matched のときだけ bound を持つ。

    Raises:
        Exception: DB 接続などの基盤異常。呼び出し側は EpisodeLookup へ
            落とすために捕捉することを想定する。
    """

    series = await Series.get_or_none(id=series_id)
    if series is None:
        return CatalogBindResult('Unknown')
    title_keys = {
        normalized
        for normalized in (
            NormalizeSeriesTitle(title),
            NormalizeSeriesTitle(subtitle) if subtitle else '',
        )
        if normalized != ''
    }
    number_tokens = ExtractCatalogEpisodeNumbers(title) | ExtractCatalogEpisodeNumbers(subtitle)
    recording_date = RecordingDate(start_time)

    # Bangumi 条目がある Series は取得できたカタログが空でない限り Bangumi を正とする。
    if IsBangumiExternalMetadataEnabled() and series.bangumi_subject_id is not None:
        bangumi_subject_id = series.bangumi_subject_id
        bangumi_candidates = await _FetchBangumiCandidates(bangumi_subject_id)
        if bangumi_candidates is None:
            # 取得失敗時は権威を切り替えず、EpisodeLookup へ落とす。
            return CatalogBindResult('Unknown')
        if len(bangumi_candidates) > 0:
            matched = NarrowCatalogCandidates(
                bangumi_candidates,
                recording_date=recording_date,
                title_keys=title_keys,
                number_tokens=number_tokens,
                duration_seconds=duration_seconds,
            )
            if matched is None:
                return CatalogBindResult('Undecided')
            return CatalogBindResult(
                'Matched',
                CatalogBoundEpisode(
                    season_number=matched.season_number,
                    episode_number=Decimal(matched.episode_number),
                    catalog='Bangumi',
                    catalog_episode_id=matched.catalog_episode_id,
                    catalog_subject_id=bangumi_subject_id,
                ),
            )
        # 正常な空カタログは不在とみなし、TMDb へ進める。

    # Bangumi カタログが無い Series だけ TMDb を正とする。
    if (
        IsTmdbExternalMetadataEnabled()
        and series.tmdb_media_type == 'tv'
        and series.tmdb_id is not None
    ):
        tmdb_id = series.tmdb_id
        # season 付きバインドでは担当 Season の候補だけを作り、他 Season の話へ誤バインドしない。
        tmdb_candidates = await _FetchTmdbCandidates(tmdb_id, season_number=series.tmdb_season_number)
        if tmdb_candidates is None or len(tmdb_candidates) == 0:
            return CatalogBindResult('Unknown')
        matched = NarrowCatalogCandidates(
            tmdb_candidates,
            recording_date=recording_date,
            title_keys=title_keys,
            number_tokens=number_tokens,
            duration_seconds=duration_seconds,
        )
        if matched is None:
            return CatalogBindResult('Undecided')
        return CatalogBindResult(
            'Matched',
            CatalogBoundEpisode(
                season_number=matched.season_number,
                episode_number=Decimal(matched.episode_number),
                catalog='TMDb',
                catalog_episode_id=matched.catalog_episode_id,
                catalog_subject_id=tmdb_id,
            ),
        )
    return CatalogBindResult('Unknown')


async def ShouldProtectCatalogValue(
    *,
    series_id: int,
    resolution_source: str | None,
    series_episode_id: int | None,
    legacy_episode_number: str | None,
) -> bool:
    """Unknown 時に旧整数への書き戻しを抑止すべきかを DB 参照だけで判断する。

    カタログ権威あり（Bangumi 条目 / TMDb TV 結合の有無）・Local 確定・
    束縛値が整数導出と不一致のときだけ True を返す。現行 mode フラグには
    依存しない。mode は新規照合（外部通信）の可否だけを決め、保存済み確定の
    保持は解除しない。取得失敗を不在や旧整数の正しさの根拠にせず、確定済み
    構造を保持するための判定である。新規 Pending の整数 fast path、
    カタログ権威なし Series、Manual は対象外とする。

    Args:
        series_id: 所属 Series ID。
        resolution_source: 現在の resolution.source。
        series_episode_id: 現在束縛中の SeriesEpisode ID。
        legacy_episode_number: 録画の旧話数文字列。

    Returns:
        旧整数 fast path を抑止すべき場合は True。基盤異常時も False。
    """

    # 単一正整数が無い録画・未束縛・Local 以外の確定は整数 fast path の対象外か、
    ## Manual など別保護の対象のため、ここでは抑止しない。
    if ParseSinglePositiveIntegerEpisode(legacy_episode_number) is None:
        return False
    if series_episode_id is None or resolution_source != 'Local':
        return False
    parsed_episode = ParseLegacyEpisodeNumber(legacy_episode_number)
    if parsed_episode is None:
        return False
    try:
        series = await Series.get_or_none(id=series_id)
        if series is None:
            return False
        # 権威は保存済み結合欄の有無だけで判断し、現行 mode には依存しない。
        ## mode は TryBindCatalogEpisode 側の新規照合（外部通信）だけを止め、
        ## 確定済み構造の保持は解除しない。mode 切替で S1 へ戻さないためである。
        has_authority = series.bangumi_subject_id is not None or (
            series.tmdb_media_type == 'tv' and series.tmdb_id is not None
        )
        if has_authority is False:
            return False
        stored_episode = await SeriesEpisode.get_or_none(
            id=series_episode_id,
            series_id=series_id,
        )
        if stored_episode is None:
            return False
        # 束縛値が整数導出と一致する録画は整数 fast path が同値を書くだけのため抑止しない。
        ## StructuredCache と同じ一致条件を使い、不一致の確定値だけを保護する。
        return not (
            parsed_episode.season_number == stored_episode.season_number
            and parsed_episode.episode_number == stored_episode.episode_number
        )
    except Exception as ex:
        # 判定不能時は既存動作のままとする。失敗の記録は呼び出し側と同様に残さない。
        logging.warning(
            '[RecordedEpisodeCatalogBind] Catalog value protection check skipped. '
            f'[series_id: {series_id}, error: {type(ex).__name__}]',
        )
        return False


async def _FetchBangumiCandidates(subject_id: int) -> list[_CatalogCandidate] | None:
    """Bangumi 条目の通常エピソードを照合用候補へ変換する。

    Args:
        subject_id: 確定済み Bangumi 条目 ID。

    Returns:
        照合用候補。取得失敗時は None。
    """

    from app.utils.KonomiTVBS4KBangumiClient import KonomiTVBS4KBangumiClient

    access_token = KonomiTVBS4KBangumiSharedStore.decryptAccessToken()
    if access_token is None:
        return None
    # _getEpisodes() のページング実装を共有し、HTTP 取得を二重化しない。
    ## 改名すると既存テストの参照が変わるため、private のまま呼び出す。
    try:
        episodes = await KonomiTVBS4KBangumiClient._getEpisodes(subject_id, access_token)  # pyright: ignore[reportPrivateUsage]
    except httpx.HTTPError as ex:
        logging.warning(
            '[RecordedEpisodeCatalogBind] Failed to fetch Bangumi episodes. '
            f'[subject_id: {subject_id}, error: {type(ex).__name__}]',
        )
        return None
    candidates: list[_CatalogCandidate] = []
    for episode in episodes:
        # _getEpisodes() は dict の一覧を返すため、ここでは通常話数だけを選ぶ。
        if int(episode.get('type', -1)) != 0:
            continue
        try:
            episode_float = float(episode.get('ep', -1))
        except (TypeError, ValueError):
            continue
        episode_number = int(episode_float)
        # 番号の無い特別編などはシーズン込み話数へ結べない。
        if episode_float != episode_number or episode_number < 1:
            continue
        name_keys = frozenset(
            normalized
            for normalized in (
                NormalizeSeriesTitle(str(episode.get('name') or '')),
                NormalizeSeriesTitle(str(episode.get('name_cn') or '')),
            )
            if normalized != ''
        )
        candidates.append(_CatalogCandidate(
            # Bangumi の条目は放送シーズンごとに分かれるため、条目内番号を Season 1 とする。
            ## EPG 整数の既存構造化（Season 1 既定）とも一致する。
            season_number=1,
            episode_number=episode_number,
            catalog_episode_id=int(episode['id']),
            name_keys=name_keys,
            number_tokens=frozenset(ExtractCatalogEpisodeNumbers(str(episode.get('name') or ''))),
            air_date=ParseCatalogAirDate(episode.get('airdate')),
            runtime_seconds=ParseBangumiDurationSeconds(episode.get('duration')),
        ))
    return candidates


async def _FetchTmdbCandidates(
    tmdb_id: int,
    *,
    season_number: int | None = None,
) -> list[_CatalogCandidate] | None:
    """TMDb TV 作品のレギュラーシーズンを照合用候補へ変換する。

    Args:
        tmdb_id: 確定済み TMDb TV 作品 ID。
        season_number: 担当 Season 番号。指定時はその Season の話だけを候補にする。
            None は作品全体バインドで、従来どおり全レギュラーシーズンを候補にする。

    Returns:
        照合用候補。取得失敗時は None。
    """

    from app.utils.KonomiTVBS4KTmdbClient import KonomiTVBS4KTmdbClient

    api_key = KonomiTVBS4KTmdbStore.getAPIKey()
    if api_key is None:
        return None
    try:
        details = await KonomiTVBS4KTmdbClient.getDetails('tv', tmdb_id, api_key)
    except (httpx.HTTPError, ValueError) as ex:
        logging.warning(
            '[RecordedEpisodeCatalogBind] Failed to fetch TMDb details. '
            f'[tmdb_id: {tmdb_id}, error: {type(ex).__name__}]',
        )
        return None
    if details is None:
        return None
    season_numbers = [
        entry for entry in details['season_numbers']
        if entry >= 1 and (season_number is None or entry == season_number)
    ]
    if len(season_numbers) == 0:
        return None
    candidates: list[_CatalogCandidate] = []
    for season_number in season_numbers:
        try:
            season_episodes = await KonomiTVBS4KTmdbClient.getSeasonEpisodeDetails(
                tmdb_id, season_number, api_key,
            )
        except (httpx.HTTPError, ValueError) as ex:
            # 一部シーズンだけ欠けたカタログで独一性を主張しない。
            logging.warning(
                '[RecordedEpisodeCatalogBind] Failed to fetch TMDb season episodes. '
                f'[tmdb_id: {tmdb_id}, season_number: {season_number}, error: {type(ex).__name__}]',
            )
            return None
        for season_episode in season_episodes:
            name_key = NormalizeSeriesTitle(season_episode['name'])
            candidates.append(_CatalogCandidate(
                season_number=season_number,
                episode_number=season_episode['episode_number'],
                catalog_episode_id=season_episode['tmdb_episode_id'],
                name_keys=frozenset((name_key,)) if name_key != '' else frozenset(),
                number_tokens=frozenset(
                    ExtractCatalogEpisodeNumbers(season_episode['name']),
                ),
                air_date=ParseCatalogAirDate(season_episode['air_date']),
                runtime_seconds=(
                    season_episode['runtime'] * 60
                    if season_episode['runtime'] is not None else None
                ),
            ))
    return candidates
