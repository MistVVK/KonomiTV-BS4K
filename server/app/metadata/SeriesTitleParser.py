from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from app.schemas import Genre


SERIES_TITLE_PARSER_VERSION = '3'


# ARIB の番組属性表示は作品名ではないため除去する。ただし、括弧そのものを一律で消すと
# 「映像の世紀バタフライエフェクト (3)」のような話数や、作品名の一部まで失うため、
# KonomiTV が実際に扱う属性だけを allowlist で除去する。
_PROGRAM_ATTRIBUTE_PATTERN = re.compile(
    r'(?:\[(?:字|デ|多|二|解|再|新|終|無|4K|8K|HDR|5\.1|SS)\]|'
    r'【(?:字|デ|多|二|解|再|新|終|無|4K|8K|HDR|5\.1|SS)】|'
    r'[🈑🈓🈔🈕🈖🈗🈘🈙🈚🈞🈟🈡])',
    flags=re.IGNORECASE,
)
_EPISODE_PATTERN = re.compile(
    r'(?:第\s*(?P<japanese>[0-9一二三四五六七八九十百千]+)\s*'
    r'(?P<unit>話|回|夜|週|巻|章|幕|局|戦)|'
    r'[#＃]\s*(?P<hash>[0-9]+(?:\.[0-9]+)?)|'
    r'(?i:(?:episode|ep)\.?\s*(?P<english>[0-9]+(?:\.[0-9]+)?)))',
)

_METADATA_EPISODE_PATTERN = re.compile(
    r'(?:第\s*(?P<japanese>[0-9一二三四五六七八九十百千]+)\s*(?P<unit>話)|'
    r'[#＃]\s*(?P<hash>[0-9]+(?:\.[0-9]+)?)|'
    r'(?i:(?:episode|ep)\.?\s*(?P<english>[0-9]+(?:\.[0-9]+)?)))',
)
_PARENTHESIZED_EPISODE_PATTERN = re.compile(
    r'[\s　]*[\(（](?P<number>[0-9]{1,4})[\)）][\s　]*'
    r'(?=(?:[「『＜<].+?[」』＞>])?[\s　]*$)',
)
_SEASON_PATTERN = re.compile(
    r'(?:第\s*(?P<japanese>[0-9一二三四五六七八九十百千]+)\s*期|'
    r'(?i:(?:season|シリーズ|シーズン)\s*(?P<number>[0-9]+)))',
)
_ATTACHED_SEASON_EPISODE_PATTERN = re.compile(
    # 2025 の末尾だけを Season 25 と解釈しないよう、シーズン番号の直前は数字以外に限定する。
    r'^(?P<title>.+?)(?<![0-9])(?P<season>[0-9]{1,2})[\s　]+.+?'
    r'[\(（](?P<episode>[0-9]{1,4})[\)）][\s　]*(?:[「『＜<].+?[」』＞>])?[\s　]*$',
)
_NAMED_SERIES_EPISODE_PATTERN = re.compile(
    r'^(?P<title>.+?)[\s　]*(?:シリーズ|シーズン|(?i:season))\s*'
    r'(?P<season>[0-9]{1,2})\s*[\(（](?P<episode>[0-9]{1,4})[\)）]',
)

# 放送枠や単発作品を「シリーズ」として束ねると、まったく異なる映画・公演が同じ一覧へ
# 混ざってしまう。固有の放送枠名は列挙せず、ジャンルと一般的な単発表現だけを使う。
_STANDALONE_TITLE_MARKERS = (
    '試験電波',
    '劇場版',
)
_STANDALONE_MAJOR_GENRES = {
    '映画',
    'ニュース・報道',
    '劇場・公演',
}

_LEADING_SUBTITLE_SEPARATOR_PATTERN = re.compile(r'^[\s　:：\-―▽▼／/「『]+')
_TRAILING_SUBTITLE_SEPARATOR_PATTERN = re.compile(r'[\s　」』＞>]+$')
_QUOTE_PAIRS = {'「': '」', '『': '』', '＜': '＞', '<': '>'}


@dataclass(frozen=True, slots=True)
class SeriesTitleParseResult:
    """録画メタデータから決定論的に抽出したシリーズ候補を表す。"""

    series_title: str
    normalized_key: str
    episode_number: str | None
    subtitle: str | None
    season_number: str | None
    episode_source: Literal['Title', 'Description', 'Detail'] | None
    has_explicit_episode: bool
    is_hard_standalone: bool
    is_soft_standalone: bool


def NormalizeProgramText(value: str) -> str:
    """EPG由来テキストを比較可能な形へ正規化する。

    Args:
        value: 正規化するタイトル・概要・詳細テキスト。

    Returns:
        NFKC、属性除去、連続空白の統一を適用した文字列。
    """

    normalized = unicodedata.normalize('NFKC', value)
    normalized = _PROGRAM_ATTRIBUTE_PATTERN.sub('', normalized)
    normalized = re.sub(r'[\s　]+', ' ', normalized)
    return normalized.strip()


def BuildSeriesGroupingKey(value: str) -> str:
    """表示用タイトルから表記揺れ比較用のキーを生成する。

    Args:
        value: シリーズ表示用タイトル。

    Returns:
        空白・句読点・装飾記号を除いたcasefold済みキー。
    """

    normalized = NormalizeProgramText(value).casefold()
    return ''.join(character for character in normalized if character.isalnum())


def _cleanSeriesTitle(value: str) -> str:
    """抽出途中のシリーズ名から限定的な装飾だけを除去する。"""

    value = re.sub(r'[\s　:：\-―▽▼／/]+$', '', value)
    value = re.sub(r'^[\s　:：\-―▽▼／/]+', '', value)
    return value.strip()


def _cleanSubtitle(value: str) -> str | None:
    """話数表記の後ろにある副題を画面表示用に整える。"""

    value = _LEADING_SUBTITLE_SEPARATOR_PATTERN.sub('', value)
    value = _TRAILING_SUBTITLE_SEPARATOR_PATTERN.sub('', value)
    value = value.strip()
    return value or None


def _formatEpisodeNumber(match: re.Match[str]) -> str:
    """話数正規表現の一致を保存用文字列へ変換する。"""

    if match.group('hash') is not None:
        return f'#{match.group("hash")}'
    if match.group('english') is not None:
        return f'#{match.group("english")}'
    return f'第{match.group("japanese")}{match.group("unit")}'


def _findQuotedSegment(value: str) -> tuple[int, int, str] | None:
    """入れ子を含む最初の対応済み引用区間を返す。

    Args:
        value: 引用符を検索する正規化済み文字列。

    Returns:
        開始位置、閉じ引用符の直後、外側引用符を除いた本文。対応しない場合は None。
    """

    for start, character in enumerate(value):
        if character not in _QUOTE_PAIRS:
            continue
        stack = [_QUOTE_PAIRS[character]]
        for index in range(start + 1, len(value)):
            current = value[index]
            if current in _QUOTE_PAIRS:
                stack.append(_QUOTE_PAIRS[current])
                continue
            if current == stack[-1]:
                stack.pop()
                if len(stack) == 0:
                    return start, index + 1, value[start + 1:index]
        return None
    return None


def _isInsideQuotedSegment(value: str, position: int) -> bool:
    """指定位置が対応済み引用区間内にあるかを返す。

    Args:
        value: 引用符を検査する正規化済み文字列。
        position: 検査する文字位置。

    Returns:
        対応済み引用区間内なら True。
    """

    remaining = value
    offset = 0
    while (segment := _findQuotedSegment(remaining)) is not None:
        start, end, _content = segment
        absolute_start = offset + start
        absolute_end = offset + end
        if absolute_start <= position < absolute_end:
            return True
        offset = absolute_end
        remaining = value[offset:]
    return False


def _stripLeadingStructuralDescriptor(value: str) -> str:
    """明示話数を伴う作品名の前に付いた短い括弧属性を構造だけから除去する。

    括弧直後が話数表記なら括弧内を作品名とみなし、除去しない。これにより
    固有の放送枠名リストを持たず、先頭属性と括弧付き作品名を区別する。

    Args:
        value: 正規化済みタイトル。

    Returns:
        先頭の構造属性を除去できた場合は残りのタイトル。それ以外は元の値。
    """

    if value.startswith('【') is False:
        return value
    closing_index = value.find('】', 1)
    if closing_index <= 1:
        return value
    remainder = value[closing_index + 1:].lstrip()
    episode_match = _EPISODE_PATTERN.search(remainder) or _PARENTHESIZED_EPISODE_PATTERN.search(remainder)
    if episode_match is None or episode_match.start() < 2:
        return value
    return remainder


def _extractExplicitEpisode(
    value: str,
    *,
    allow_parenthesized_episode: bool,
    metadata: bool = False,
) -> tuple[re.Match[str] | None, str | None]:
    """1つのメタデータ文字列から明示的な話数を抽出する。"""

    match = None
    for candidate in (_METADATA_EPISODE_PATTERN if metadata else _EPISODE_PATTERN).finditer(value):
        # 作品名・副題の引用内にある「EP.1」などを現在回の話数として扱わない。
        if _isInsideQuotedSegment(value, candidate.start()):
            continue
        match = candidate
        break
    if match is not None:
        # 概要・詳細の本文中に現れる過去回への言及を、現在回の話数として誤採用しない。
        if metadata and value[:match.start()].strip(' \t:：-―ー▽▼「『＜<【[') != '':
            return None, None
        return match, _formatEpisodeNumber(match)

    if allow_parenthesized_episode is True:
        parenthesized_match = _PARENTHESIZED_EPISODE_PATTERN.search(value)
        if parenthesized_match is not None:
            return parenthesized_match, f'#{parenthesized_match.group("number")}'
    return None, None


def ParseSeriesTitle(
    title: str,
    description: str,
    detail: dict[str, str],
    genres: list[Genre],
) -> SeriesTitleParseResult:
    """録画のタイトル・概要・詳細からシリーズ名と話数を決定論的に抽出する。

    Args:
        title: 録画番組タイトル。
        description: 録画番組概要。
        detail: 録画番組詳細。
        genres: 録画番組ジャンル。

    Returns:
        ローカル解析で得られたシリーズ候補、話数、副題、単発除外判定。
    """

    normalized_title = _stripLeadingStructuralDescriptor(NormalizeProgramText(title))
    normalized_description = NormalizeProgramText(description)
    normalized_details = [NormalizeProgramText(value) for value in detail.values() if value.strip() != '']
    major_genres = {genre['major'] for genre in genres}
    primary_major_genre = genres[0]['major'] if len(genres) > 0 else None
    allow_parenthesized_episode = len(major_genres & {'アニメ・特撮', 'ドラマ', 'ドキュメンタリー・教養'}) > 0
    is_primary_movie = primary_major_genre == '映画'
    is_hard_standalone = (
        any(marker in normalized_title for marker in _STANDALONE_TITLE_MARKERS)
    )
    is_soft_standalone = primary_major_genre in _STANDALONE_MAJOR_GENRES

    # 「作品名 シリーズ3(4)出演者…」は括弧話数の後ろが副題で終わらないが、
    # seasonキーワードとepisodeが直結するため曖昧性なく抽出できる。
    named_series_match = _NAMED_SERIES_EPISODE_PATTERN.match(normalized_title)
    if named_series_match is not None:
        series_title = _cleanSeriesTitle(named_series_match.group('title'))
        season_number = named_series_match.group('season')
        return SeriesTitleParseResult(
            series_title=series_title,
            normalized_key=BuildSeriesGroupingKey(series_title),
            episode_number=f'Season {season_number} #{named_series_match.group("episode")}',
            subtitle=None,
            season_number=season_number,
            episode_source='Title',
            has_explicit_episode=True,
            is_hard_standalone=False,
            is_soft_standalone=False,
        )

    # 「アストリッドとラファエル6 文書係の事件録 (3)」のように、シーズン番号が
    # 作品名へ直結し、話数が末尾括弧にある形式を通常の末尾話数より先に扱う。
    attached_match = _ATTACHED_SEASON_EPISODE_PATTERN.match(normalized_title)
    if attached_match is not None and int(attached_match.group('season')) <= 30:
        series_title = _cleanSeriesTitle(attached_match.group('title'))
        season_number = attached_match.group('season')
        episode_number = f'Season {season_number} #{attached_match.group("episode")}'
        subtitle_start = normalized_title.find(' ', attached_match.end('season'))
        subtitle_end_match = _PARENTHESIZED_EPISODE_PATTERN.search(normalized_title)
        subtitle = None
        if subtitle_start >= 0 and subtitle_end_match is not None:
            subtitle = _cleanSubtitle(normalized_title[subtitle_start:subtitle_end_match.start()])
            suffix_subtitle = _cleanSubtitle(normalized_title[subtitle_end_match.end():])
            if suffix_subtitle is not None:
                subtitle = suffix_subtitle
        return SeriesTitleParseResult(
            series_title=series_title,
            normalized_key=BuildSeriesGroupingKey(series_title),
            episode_number=episode_number,
            subtitle=subtitle,
            season_number=season_number,
            episode_source='Title',
            has_explicit_episode=True,
            is_hard_standalone=False,
            is_soft_standalone=False,
        )

    title_episode_match, title_episode_number = _extractExplicitEpisode(
        normalized_title,
        allow_parenthesized_episode=allow_parenthesized_episode,
    )
    season_match = _SEASON_PATTERN.search(normalized_title)

    # タイトル自身に話数があれば、シーズン・話数のうち早い位置をシリーズ名の終端にする。
    if title_episode_match is not None and title_episode_number is not None:
        title_end = title_episode_match.start()
        if season_match is not None:
            title_end = min(title_end, season_match.start())
        series_title = _cleanSeriesTitle(normalized_title[:title_end])
        season_number = None
        if season_match is not None:
            season_number = season_match.group('japanese') or season_match.group('number')
        episode_number = title_episode_number
        if season_number is not None:
            episode_number = f'Season {season_number} {episode_number}'
        subtitle = _cleanSubtitle(normalized_title[title_episode_match.end():])

        # 末尾 (N) 形式では、その直前にある「作品名『副題』」を安全に分離する。
        if title_episode_match.re is _PARENTHESIZED_EPISODE_PATTERN:
            title_before_episode = normalized_title[:title_episode_match.start()].rstrip()
            subtitle_segment = _findQuotedSegment(title_before_episode)
            if subtitle_segment is not None and subtitle_segment[0] >= 2:
                series_title = _cleanSeriesTitle(title_before_episode[:subtitle_segment[0]])
                subtitle = _cleanSubtitle(subtitle_segment[2])
            else:
                series_title = _cleanSeriesTitle(title_before_episode)
        return SeriesTitleParseResult(
            series_title=series_title or normalized_title,
            normalized_key=BuildSeriesGroupingKey(series_title or normalized_title),
            episode_number=episode_number,
            subtitle=subtitle,
            season_number=season_number,
            episode_source='Title',
            has_explicit_episode=True,
            is_hard_standalone=False,
            is_soft_standalone=False,
        )

    # タイトルに話数がない場合は概要、次に詳細を調べる。概要だけが話名になっている
    # 録画はここでは確定せず、後段のEPG一意照合へ委ねる。
    metadata_sources: list[tuple[Literal['Description', 'Detail'], str]] = [('Description', normalized_description)]
    metadata_sources.extend(('Detail', value) for value in normalized_details)
    for source, value in metadata_sources if not is_hard_standalone and not is_soft_standalone else []:
        episode_match, episode_number = _extractExplicitEpisode(
            value,
            allow_parenthesized_episode=False,
            metadata=True,
        )
        if episode_match is None or episode_number is None:
            continue
        series_title = _cleanSeriesTitle(normalized_title)
        season_number = None
        metadata_season_match = _SEASON_PATTERN.search(value)
        if metadata_season_match is not None:
            season_number = metadata_season_match.group('japanese') or metadata_season_match.group('number')
            episode_number = f'Season {season_number} {episode_number}'
        subtitle = _cleanSubtitle(value[episode_match.end():])
        return SeriesTitleParseResult(
            series_title=series_title,
            normalized_key=BuildSeriesGroupingKey(series_title),
            episode_number=episode_number,
            subtitle=subtitle,
            season_number=season_number,
            episode_source=source,
            has_explicit_episode=True,
            is_hard_standalone=False,
            is_soft_standalone=False,
        )

    # 話数を伴わない引用符以降は、副題候補として分離できる。ただしシリーズ確定の
    # 根拠にはせず、同じrootを持つ別録画の存在をResolver側で確認する。
    series_title = _cleanSeriesTitle(normalized_title)
    subtitle = None
    subtitle_segment = _findQuotedSegment(series_title)
    if subtitle_segment is not None and subtitle_segment[0] >= 2:
        subtitle = _cleanSubtitle(subtitle_segment[2])
        series_title = _cleanSeriesTitle(series_title[:subtitle_segment[0]])
    if subtitle is None:
        description_subtitle_segment = _findQuotedSegment(normalized_description)
        if (
            description_subtitle_segment is not None
            and description_subtitle_segment[0] == 0
            and description_subtitle_segment[1] == len(normalized_description)
        ):
            subtitle = _cleanSubtitle(description_subtitle_segment[2])

    return SeriesTitleParseResult(
        series_title=series_title,
        normalized_key=BuildSeriesGroupingKey(series_title),
        episode_number=None,
        subtitle=subtitle,
        season_number=None,
        episode_source=None,
        has_explicit_episode=False,
        # 「映画『作品名』」のような汎用枠タイトルは引用符前が同じ root になるため、
        # 内容の異なる映画が2本揃っただけでシリーズへ昇格しないよう絶対除外にする。
        # 明示話数がある場合は上の分岐ですでに返しており、この除外より優先される。
        is_hard_standalone=is_hard_standalone or is_primary_movie,
        is_soft_standalone=is_soft_standalone and not is_primary_movie,
    )
