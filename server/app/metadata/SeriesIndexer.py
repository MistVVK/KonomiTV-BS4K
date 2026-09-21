from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from tortoise import connections, transactions
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.expressions import Q

from app import logging
from app.constants import JST
from app.metadata.RecordedEpisodeResolver import (
    ParseLegacyEpisodeNumber,
    ParseSinglePositiveIntegerEpisode,
)
from app.metadata.RecordedSeriesSettings import RecordedSeriesSettingsStore
from app.metadata.SeriesTitleParser import (
    ContainsKanjiCharacters,
    DeriveTitleReadingFromTitle,
    ExtractMovieWorkTitle,
    NormalizeTitleReading,
)
from app.models.RecordedEpisode import RecordedEpisodeResolution
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedSeries import RecordedSeriesResolution
from app.models.Series import Series
from app.models.SeriesAlias import SeriesAlias
from app.models.SeriesBroadcastPeriod import SeriesBroadcastPeriod
from app.schemas import Genre


@dataclass(frozen=True, slots=True)
class ParsedSeriesTitle:
    """番組タイトルから確定的に抽出したシリーズ識別情報を保持する。"""

    # 利用者へ表示する作品名。Series.title と RecordedProgram.series_title に保存する。
    display_title: str
    # 表記揺れだけを吸収した完全一致用キー。異なる作品を fuzzy に統合する用途には使わない。
    normalized_title: str
    # 番組タイトルから明示的に抽出できた話数。無話数の定期番組では None。
    episode_number: str | None
    # 話数表記の後ろにある副題。取得できない場合は None。
    subtitle: str | None
    # 映画作品として識別できた場合だけ True。グルーピング identity の種別に使う。
    is_movie: bool = False


# 放送状態や短い汎用枠は、同名でも一つの作品を表さないため Series を自動生成しない。
GENERIC_SERIES_TITLES = {
    'bテレ',
    'musicアラカルト',
    'weatherreport',
    'ニュース',
    '紅白なび',
    '放送休止',
    '天気予報',
}

# バラエティ・音楽番組は話数を付けず、固定の番組名と毎回の企画名で EPG を構成することがある。
## ジャンルだけで単発番組を統合しないよう、実際に類似する別の録画がある場合に限り Series にする。
EPISODELESS_SERIES_GENRES = {'バラエティ', '音楽'}

# task で実資料が確認できた ★ 区切り運用のシリーズだけを列挙する。★ 直後の全文を話名にする形式は
## 宝塚カフェブレイクの受け入れ条件 2/3 に名指しされているため、正規化キー完全一致で対象を閉じ、
## VIVA! TOKYO MINA など他シリーズの話名契約へ一般化しない。値は NormalizeSeriesTitle() 済みキー。
STAR_EPISODELESS_SERIES_KEYS = {'宝塚カフェブレイク'}

# 話数マーカーより後ろの引用を公演行として残す対象。`NOW ON STAGE#NNN 公演行…` の形式に限る。
## これを一般化すると `作品(NN)第N週「副題」` 形式で副題が週表記ごと拡大する回帰が実在するため、
## 作品名 (話数より前) の正規化キー完全一致で発火を制限する。値は NormalizeSeriesTitle() 済みキー。
STAR_TRAILER_QUOTE_SERIES_KEYS = {'nowonstage'}

# EPG タイトルの先頭に付与される放送枠名。作品名そのものではないため除外する。
PROGRAM_SLOT_PREFIX_PATTERN = re.compile(
    r'^(?:(?:<[^<>]+>|＜[^＜＞]+＞)|(?:アニメギルド|アニメA[・･]|火アニバル))\s*',
    flags=re.IGNORECASE,
)
PROGRAM_TYPE_PREFIX_PATTERN = re.compile(r'^(?:(?:TV|テレビ)?アニメ)\s+', flags=re.IGNORECASE)

# 作品名を引用符で囲う放送枠。通常の各話副題と区別するため、副題抽出より先に作品名を外へ出す。
QUOTED_PROGRAM_SLOT_PATTERN = re.compile(
    r'^(?:日5)「(?P<title>[^」]+)」(?P<rest>.*)$',
    flags=re.IGNORECASE,
)
QUOTED_WORK_TITLE_PATTERN = re.compile(
    r'^(?:(?:TVアニメ|時代劇)\s*)?[「『](?P<title>[^」』]+)[」』](?P<rest>.*)$',
    flags=re.IGNORECASE,
)

# 作品名の前後に付く既知の放送枠名。作品名の装飾は汎用的に削除せず、実データで確認できた枠だけを列挙する。
PROGRAM_SLOT_MARK_PATTERN = re.compile(
    r'(?:【(?:ANiMAZiNG(?:2|²)?[!！]*|スーパーアニメイズムTURBO|イマニメーションW?)】|<\+Ultra>|\s+(?:AnichU|FRIDAY ANIME NIGHT)\s*$)',
    flags=re.IGNORECASE,
)

# KonomiTV が既に番組記号として扱っている角括弧表記だけを除去する。
PROGRAM_MARK_PATTERN = re.compile(
    r'\[(?:新|終|再|交|映|手|声|多|副|字|文|CC|OP|二|S|B|SS|無|無料|C|S1|S2|S3|MV|双|デ|D|N|W|P|H|HV|SD|天|解|料|前|後|初|生|販|吹|PPV|演|移|他|収|英|韓|中|字/日|字/日英|3D|2ndScr|2K|4K|8K|5\.1|7\.1|22\.2|60P|120P|d|HC|HDR|Hi-Res|Lossless|SHV|UHD|VOD|配)\]',
    flags=re.IGNORECASE,
)

# 話数を明示する表記だけを Series の自動生成根拠として採用する。
# 「第 + 数字」は作品ごとに異なる一文字の助数詞を列挙せず、期・部・章など作品名側の番号だけを除外する。
EPISODE_PATTERN = re.compile(
    r'(?:'
    r'\(\s*第?\s*(?P<parenthesized>[0-9一二三四五六七八九十百千〇零壱弐参拾貳肆伍陸漆玖]+(?:\.[0-9]+)?)\s*(?:話|回|講|輪)?\s*\)?|'
    r'#\s*(?P<hash>[0-9]+(?:\.[0-9]+)?(?:\s*[・&／/\-～~]\s*#?\s*[0-9]+(?:\.[0-9]+)?)*)|'
    r'第\s*(?P<japanese>[0-9一二三四五六七八九十百千〇零壱弐参拾貳肆伍陸漆玖]+(?:\s*[・&／/\-～~]\s*#?\s*[0-9一二三四五六七八九十百千〇零壱弐参拾貳肆伍陸漆玖]+)*)'
    r'(?!\s*(?:期|シーズン|クール|部|章))(?:(?:\s*(?:話|回|講|輪))|[^\W\d_\s])?|'
    r'\b(?:Chapter|CH)\s*(?P<chapter>[0-9]+(?:\.[0-9]+)?)'
    r')',
    flags=re.IGNORECASE,
)
TRAILING_EPISODE_PATTERN = re.compile(r'\s+(?P<episode>[0-9]+(?:\.[0-9]+)?)\s*$')
# 一部放送局が装飾用の閉じ波線の直後へ付けるクール番号。
## 「作品名～2 17」の最初の 2 だけを作品名から外し、末尾の 17 は通常どおり話数として扱う。
BROADCASTER_COUR_SUFFIX_PATTERN = re.compile(r'^(?P<title>.+[～~])(?P<cour>[2-9])$')
# 同一作品を放送局ごとに区別する末尾の編集版表記。作品・話数は同一なので Series 識別名からだけ除外する。
## 「ver.」全般を削ると作品名そのものを壊すため、実データで同一話数の別局版を確認できた表記だけを列挙する。
BROADCAST_EDITION_SUFFIX_PATTERN = re.compile(
    r'\s*(?:CENSORED版|青藍島ver\.)$',
    flags=re.IGNORECASE,
)
QUOTED_SUBTITLE_PATTERN = re.compile(r'[「『](?P<subtitle>.*?)[」』]')
# 日曜劇場「VIVANT」のように枠名の直後に来る引用符は、各話副題ではなく作品名の一部を表す。
QUOTED_SLOT_WORK_PREFIX_PATTERN = re.compile(r'(?:劇場|ロードショー|シネマ|シアター)$')
QUOTED_LEVEL_EPISODE_PATTERN = re.compile(
    r'^Lv\s*(?P<episode>[0-9]+(?:\.[0-9]+)?)\s+(?P<subtitle>.+)$',
    flags=re.IGNORECASE,
)
JAPANESE_DIGITS = {
    '〇': 0,
    '零': 0,
    '一': 1,
    '壱': 1,
    '二': 2,
    '弐': 2,
    '三': 3,
    '参': 3,
    '貳': 3,
    '四': 4,
    '肆': 4,
    '五': 5,
    '伍': 5,
    '六': 6,
    '陸': 6,
    '七': 7,
    '漆': 7,
    '八': 8,
    '九': 9,
    '玖': 9,
}
JAPANESE_UNITS = {'十': 10, '拾': 10, '百': 100, '千': 1000}


def ParseJapaneseNumber(value: str) -> int | None:
    """
    EPG の話数に使われる漢数字を整数へ変換する。

    Args:
        value (str): 漢数字の話数。

    Returns:
        int | None: 変換できた整数。漢数字以外を含む場合は None。
    """

    if all(character in JAPANESE_DIGITS for character in value):
        return int(''.join(str(JAPANESE_DIGITS[character]) for character in value))

    total = 0
    current_digit = 0
    for character in value:
        if character in JAPANESE_DIGITS:
            current_digit = JAPANESE_DIGITS[character]
            continue
        unit = JAPANESE_UNITS.get(character)
        if unit is None:
            return None
        total += (current_digit or 1) * unit
        current_digit = 0
    return total + current_digit


def NormalizeEpisodeNumber(episode_number: str) -> str:
    """
    話数の数値表記を表示・比較しやすい形へ揃える。

    Args:
        episode_number (str): EPG タイトルから抽出した話数。

    Returns:
        str: 先頭ゼロと多話区切りの表記を正規化した話数。
    """

    # 漢数字は意味を変えず保持し、ASCII 数字だけ先頭ゼロを取り除く。
    parts = re.split(r'\s*[・&／/\-～~]\s*#?\s*', episode_number)
    normalized_parts: list[str] = []
    for part in parts:
        # EPG 更新で先頭の # が残ったまま話数だけ抽出される値があるため、比較前に外す。
        part = part.lstrip('#')
        japanese_number = ParseJapaneseNumber(part)
        if part.isdigit():
            normalized_parts.append(str(int(part)))
        elif re.fullmatch(r'\d+\.\d+', part):
            normalized_parts.append(str(float(part)).rstrip('0').rstrip('.'))
        elif japanese_number is not None:
            normalized_parts.append(str(japanese_number))
        else:
            normalized_parts.append(part)
    separator = '-' if re.search(r'[\-～~]', episode_number) is not None else '・'
    return separator.join(normalized_parts)


def NormalizeSeriesTitle(title: str) -> str:
    """
    シリーズ同一性の完全一致判定に使うタイトルを生成する。

    Args:
        title (str): 表示用に整形済みの作品名。

    Returns:
        str: Unicode・英字大小・空白だけを正規化した比較キー。
    """

    # NFKC で全角英数字や互換文字を揃え、空白差を作品同一性へ影響させない。
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', title)).casefold()


def AreAIFallbackSeriesTitlesClose(left: str, right: str) -> bool:
    """AI 補完名と既存 Series 名が完全一致または接尾辞拡張の関係か判定する。

    Args:
        left: NormalizeSeriesTitle() 済みの一方のタイトル。
        right: NormalizeSeriesTitle() 済みのもう一方のタイトル。

    Returns:
        同一名か、十分な長さの短い側へ接尾辞を加えた関係なら True。
    """

    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= 4 and longer.startswith(shorter)


async def SaveTitleReading(series_id: int, series_title: str, title_reading: str | None) -> None:
    """AI 応答などの読みを、未設定の Series へだけ保存する。

    既存値の上書きはしない (ひらがな化し切れない語の誤りは許容する仕様)。漢字を
    含む値はあ→んソートで整列しない無効な読みなので保存せず、未設定のまま再取得
    対象に残す。読みが得られない場合でも、カナのみのタイトルなら AI なしの読みを
    生成して保存する。

    Args:
        series_id (int): 保存先の Series ID。
        series_title (str): 保存先 Series の表示タイトル (カナのみ読みの生成元)。
        title_reading (str | None): AI 応答から取得した読み。無ければ None。
    """

    normalized_reading = NormalizeTitleReading(title_reading)
    if normalized_reading is None:
        # カナのみのタイトルは AI なしで読みを作れる。
        normalized_reading = DeriveTitleReadingFromTitle(series_title)
    if normalized_reading is None or ContainsKanjiCharacters(normalized_reading):
        # 漢字が残った読みはあ→んソートで正しい列へ並ばないため、有効な読みとして保存しない。
        ## 列は未設定のまま残るので、次の補完実行で再取得される。
        return
    # 未設定の行だけを原子的に更新する。同時実行の AI 結果で上書き競合しない。
    await Series.filter(id=series_id, title_reading__isnull=True).update(
        title_reading=normalized_reading,
        updated_at=datetime.now(tz=JST),
    )


def BuildSeriesAIFallbackGroupingKey(title: str) -> str:
    """AI Web 検索を束ねる、装飾除去済みの EPG タイトル完全一致キーを返す。

    Args:
        title: EPG 由来の番組タイトル。

    Returns:
        同じ EPG タイトル系だけを束ねる完全一致キー。
    """

    normalized_source = unicodedata.normalize('NFKC', title).strip()
    normalized_source = PROGRAM_MARK_PATTERN.sub('', normalized_source)
    normalized_source = re.sub(r'\((?:二|字|再)\)', '', normalized_source)
    normalized_source = PROGRAM_SLOT_PREFIX_PATTERN.sub('', normalized_source)
    normalized_source = PROGRAM_TYPE_PREFIX_PATTERN.sub('', normalized_source)
    normalized_source = PROGRAM_SLOT_MARK_PATTERN.sub('', normalized_source)
    return NormalizeSeriesTitle(normalized_source.strip())


def ParseEpisodeLessSeriesTitle(
    title: str,
    genres: list[Genre],
    similar_titles: list[str],
) -> ParsedSeriesTitle | None:
    """
    類似する過去タイトルから無話数の定期番組名を抽出する。

    Args:
        title (str): EPG 由来の番組タイトル。
        genres (list[Genre]): EPG 由来の番組ジャンル。
        similar_titles (list[str]): 同じ番組名候補で始まる別の録画タイトル。

    Returns:
        ParsedSeriesTitle | None: 別の録画で定期番組と確認できた場合の解析結果。
    """

    normalized_source = unicodedata.normalize('NFKC', title).strip()

    # task で運用を確認した `シリーズ名 ★話名` 形式のシリーズだけ、★ の後ろの全文を話名とする。
    ## 引用符を消費せず、複数録画が ★ 直前まで完全一致するときだけ定期番組として束ねるため、
    ## 単発特番は拾わない。ジャンルの gate は情報番組にも現れる形式なので要求しないが、
    ## 他シリーズへ一般化しないよう正規化キーの対象外ではこの分岐に入らない。
    star_source = PROGRAM_MARK_PATTERN.sub('', normalized_source)
    star_index = star_source.find('★')
    if star_index > 0:
        display_title = star_source[:star_index].strip()
        trailing_subtitle = star_source[star_index + 1:].strip()
        normalized_title = NormalizeSeriesTitle(display_title)
        if normalized_title in STAR_EPISODELESS_SERIES_KEYS and trailing_subtitle != '':
            # 別録画判定は現行側と同じく装飾マークを除去してから比べ、[再] だけの再放送対を
            ## 互いの根拠にしない (前字列の一致検査も同じ除去済み表記で行っている)。
            has_same_star_prefixed_title = False
            for similar_title in similar_titles:
                if '★' not in similar_title:
                    continue
                star_similar = PROGRAM_MARK_PATTERN.sub('', unicodedata.normalize('NFKC', similar_title))
                if (
                    NormalizeSeriesTitle(star_similar) != NormalizeSeriesTitle(star_source) and
                    star_similar.split('★', 1)[0].strip() == display_title
                ):
                    has_same_star_prefixed_title = True
                    break
            if has_same_star_prefixed_title:
                return ParsedSeriesTitle(
                    display_title = display_title,
                    normalized_title = normalized_title,
                    episode_number = None,
                    subtitle = trailing_subtitle,
                )

    # 話数を付けず毎回の企画名を入れる運用が確認できたジャンルだけを対象にする。
    if {genre['major'] for genre in genres}.isdisjoint(EPISODELESS_SERIES_GENRES):
        return None

    quote_index_candidates = [
        normalized_source.find(quote)
        for quote in ('「', '『')
        if normalized_source.find(quote) >= 0
    ]
    if len(quote_index_candidates) == 0:
        return None
    quote_index = min(quote_index_candidates)
    display_title = normalized_source[:quote_index].strip()
    trailing_subtitle = normalized_source[quote_index:].strip()
    normalized_title = NormalizeSeriesTitle(display_title)
    if len(normalized_title) < 2 or normalized_title in GENERIC_SERIES_TITLES:
        return None

    # 同じ固定名で始まり、かつ全体は異なる過去録画を定期番組の根拠にする。
    ## 引用符の境界まで一致させ、似た文字列を持つ別番組の誤統合を防ぐ。
    has_similar_title = any(
        NormalizeSeriesTitle(similar_title) != NormalizeSeriesTitle(normalized_source) and
        any(
            unicodedata.normalize('NFKC', similar_title).startswith(f'{display_title}{quote}')
            for quote in ('「', '『')
        )
        for similar_title in similar_titles
    )
    if has_similar_title is False:
        return None

    quoted_subtitle_match = re.fullmatch(r'[「『](?P<subtitle>.*)[」』]', trailing_subtitle)
    subtitle = (
        quoted_subtitle_match.group('subtitle').strip()
        if quoted_subtitle_match is not None
        else trailing_subtitle
    )
    return ParsedSeriesTitle(
        display_title = display_title,
        normalized_title = normalized_title,
        episode_number = None,
        subtitle = subtitle,
    )


def IsStrictSeriesTitlePrefix(short_title: str | None, long_title: str | None) -> bool:
    """
    短縮作品名と正式作品名を、安全な副題境界を持つ前方一致として比較する。

    Args:
        short_title (str | None): 短縮された作品名の正規化キー。
        long_title (str | None): 副題まで含む正式作品名の正規化キー。

    Returns:
        bool: 長い作品名が短い作品名に明示的な副題を加えた形なら True。
    """

    if short_title is None or long_title is None:
        return False
    if not long_title.startswith(short_title) or len(long_title) <= len(short_title) + 1:
        return False
    return long_title[len(short_title)] in {'~', '～', '-', '―', '—', ':', '：', '「', '『', '【'}


def ParseSeriesTitle(
    title: str,
    genres: list[Genre],
    description: str | None = None,
) -> ParsedSeriesTitle | None:
    """
    EPG タイトルから誤統合しにくい確定的なシリーズ情報を抽出する。

    Args:
        title (str): EPG 由来の番組タイトル。
        genres (list[Genre]): 末尾数字を話数として扱える番組ジャンル。
        description (str | None): 放送局が話数をタイトルでなく先頭行に入れる番組概要。

    Returns:
        ParsedSeriesTitle | None: 明示的な話数と十分な作品名を抽出できた場合のみ結果を返す。
    """

    # 全角英数字などを先に揃え、放送枠・番組種別・技術マークを作品名から分離する。
    normalized_source = unicodedata.normalize('NFKC', title).strip()
    normalized_source = PROGRAM_MARK_PATTERN.sub('', normalized_source)
    normalized_source = re.sub(r'\((?:二|字|再)\)', '', normalized_source)
    normalized_source = PROGRAM_SLOT_PREFIX_PATTERN.sub('', normalized_source)
    normalized_source = PROGRAM_TYPE_PREFIX_PATTERN.sub('', normalized_source)
    normalized_source = PROGRAM_SLOT_MARK_PATTERN.sub('', normalized_source)
    normalized_source = normalized_source.strip()

    # 映画枠・劇場版は作品単位で Series を作る。枠名で束ねると別作品が混ざり、
    ## 話数が無いため AI 側へ落ちると枠シリーズへ誤統合される。引用作品名と
    ## 後続 installment を作品名にし、監督カット版は本編と同一キーにする。
    movie_work = ExtractMovieWorkTitle(
        normalized_source,
        genres[0]['major'] if len(genres) > 0 else None,
    )
    if movie_work is not None:
        movie_title, has_quoted_movie = movie_work
        normalized_movie_title = NormalizeSeriesTitle(movie_title)
        if len(normalized_movie_title) >= 2 and normalized_movie_title not in GENERIC_SERIES_TITLES:
            return ParsedSeriesTitle(
                display_title = movie_title,
                normalized_title = normalized_movie_title,
                episode_number = None,
                subtitle = movie_title if has_quoted_movie else None,
                is_movie = True,
            )
        # 抽出できた映画名が汎用枠相当のときは、通常解析へ委ねず除外する。
        return None

    # 「日5『作品名』 #2」の引用符は副題ではないため、作品名と話数を通常の配置に戻す。
    quoted_program_slot_match = QUOTED_PROGRAM_SLOT_PATTERN.fullmatch(normalized_source)
    if quoted_program_slot_match is not None:
        normalized_source = (
            quoted_program_slot_match.group('title') + quoted_program_slot_match.group('rest')
        ).strip()

    # 「TVアニメ『作品名』第2話」などは引用部分自体が作品名なので、副題抽出の前に外へ出す。
    quoted_work_title_match = QUOTED_WORK_TITLE_PATTERN.fullmatch(normalized_source)
    if quoted_work_title_match is not None:
        normalized_source = (
            quoted_work_title_match.group('title') + quoted_work_title_match.group('rest')
        ).strip()

    # 「…」内は各話副題として先に保持し、作品名の比較キーからは除外する。
    subtitle_match = QUOTED_SUBTITLE_PATTERN.search(normalized_source)
    # 枠名 (日曜劇場・金曜ロードショーなど) の直後にある引用符は作品名の一部なので副題にしない。
    ## 例: 日曜劇場「VIVANT」第11話 → 引用符内を副題にすると枠名だけのシリーズへ誤統合する。
    if (
        subtitle_match is not None
        and QUOTED_SLOT_WORK_PREFIX_PATTERN.search(normalized_source[:subtitle_match.start()].rstrip()) is not None
    ):
        subtitle_match = None
    # 対象シリーズでは話数マーカーより後ろの引用符を公演行の一部として残す。
    ## 例: NOW ON STAGE#733 花組 宝塚バウホール公演『赤と黒』 → 最初の引用だけ副題にすると
    ## 公演行全体を失い、続けて並ぶ引用も消える。引用を残して話数検索へ渡せば、末尾が
    ## 引用単体なら従来どおり中身だけ副題になり、それ以外は話数より後ろの全文が副題になる。
    ## 他シリーズ (作品(NN)第N週「副題」など) へ一般化すると既存の週形式が壊れるためキー限定。
    if subtitle_match is not None:
        explicit_episode_probe = EPISODE_PATTERN.search(normalized_source)
        probe_work_key = (
            NormalizeSeriesTitle(normalized_source[:explicit_episode_probe.start()])
            if explicit_episode_probe is not None
            else ''
        )
        if (
            explicit_episode_probe is not None
            and subtitle_match.start() >= explicit_episode_probe.end()
            and probe_work_key in STAR_TRAILER_QUOTE_SERIES_KEYS
        ):
            subtitle_match = None
    subtitle = subtitle_match.group('subtitle').strip() if subtitle_match is not None else None
    title_without_quoted_subtitle = (
        QUOTED_SUBTITLE_PATTERN.sub('', normalized_source).strip()
        if subtitle_match is not None
        else normalized_source
    )

    # #6 / 第6話 / Chapter 6 など、話数だと断定できる位置より前を作品名として採用する。
    episode_source = title_without_quoted_subtitle
    episode_match = EPISODE_PATTERN.search(episode_source)
    is_episode_from_description = False
    is_episode_from_trailing_title = False
    quoted_level_match = QUOTED_LEVEL_EPISODE_PATTERN.fullmatch(subtitle) if subtitle is not None else None
    if episode_match is None:
        # 一部アニメ局は「Lv2 副題」のように話数を引用符内へ入れるため、引用符の先頭だけを追加で認識する。
        ## 作品名本体の LV999 などを話数と誤認しないよう、タイトル本体では Lv 表記を検索しない。
        if quoted_level_match is not None:
            episode_number = NormalizeEpisodeNumber(quoted_level_match.group('episode'))
            display_title = title_without_quoted_subtitle.strip(' 　・:-')
            subtitle = quoted_level_match.group('subtitle').strip()
            normalized_title = NormalizeSeriesTitle(display_title)
            if len(normalized_title) < 2 or normalized_title in GENERIC_SERIES_TITLES:
                return None
            return ParsedSeriesTitle(
                display_title = display_title,
                normalized_title = normalized_title,
                episode_number = episode_number,
                subtitle = subtitle,
            )

        # アニメ EPG では末尾の単独数字が話数として使われるため、このジャンルに限り追加で認識する。
        is_anime = any(genre['major'] == 'アニメ・特撮' for genre in genres)
        episode_match = TRAILING_EPISODE_PATTERN.search(title_without_quoted_subtitle) if is_anime else None
        is_episode_from_trailing_title = episode_match is not None
        if episode_match is None and is_anime and description is not None:
            # 放送局によっては title を毎回同じ作品名にし、description の独立行先頭へ話数を入れる。
            ## あらすじ本文に現れる数字を話数と誤認しないよう、各行の先頭一致だけを採用する。
            for description_line in description.splitlines():
                description_line = description_line.strip()
                description_episode_match = EPISODE_PATTERN.match(description_line)
                if description_episode_match is not None:
                    episode_source = description_line
                    episode_match = description_episode_match
                    is_episode_from_description = True
                    break
    if episode_match is None:
        return None

    # 正規表現のうち一致した表記から話数文字列を取り出す。
    episode_number = NormalizeEpisodeNumber(next(
        value
        for value in episode_match.groupdict().values()
        if value is not None
    ))
    display_title = (
        title_without_quoted_subtitle
        if is_episode_from_description
        else title_without_quoted_subtitle[:episode_match.start()]
    ).strip(' 　・:-(（')

    # BS 日テレ 4K の「ヘルモード ...～2 17」のように、閉じ波線と末尾話数の間へ
    ## クール番号を挿入する表記だけを補正する。通常の「作品2 17」は作品名の数字を保持する。
    if is_episode_from_trailing_title:
        broadcaster_cour_suffix_match = BROADCASTER_COUR_SUFFIX_PATTERN.fullmatch(display_title)
        if broadcaster_cour_suffix_match is not None:
            display_title = broadcaster_cour_suffix_match.group('title')

    # AT-X はタイトル欄を短縮し、description の先頭行へ「■短縮名 + 正式な副題」を置くことがある。
    ## 先頭行が解析済みの作品名から始まり、かつ実際に長い場合だけ正式名として採用することで、
    ## テレビ東京の「■各話サブタイトル」のような同じ記号を使う本文は作品名へ混入させない。
    if description is not None:
        description_first_line = unicodedata.normalize('NFKC', next(iter(description.splitlines()), '')).strip()
        if description_first_line.startswith('■'):
            description_series_title = description_first_line.removeprefix('■').strip()
            normalized_display_title = NormalizeSeriesTitle(display_title)
            normalized_description_series_title = NormalizeSeriesTitle(description_series_title)
            # AT-X の表示上限で末尾が「…」になった場合は、NFKC 後の三点を外した前方一致で補完する。
            ## 省略記号がない短い作品名には適用せず、別作品を説明文へ引っ張る範囲を限定する。
            normalized_truncated_title = normalized_display_title.removesuffix('...')
            if (
                len(normalized_description_series_title) > len(normalized_display_title) and
                (
                    normalized_description_series_title.startswith(normalized_display_title) or
                    (
                        normalized_truncated_title != normalized_display_title and
                        normalized_description_series_title.startswith(normalized_truncated_title)
                    )
                )
            ):
                display_title = description_series_title

    # AT-X の独自編集版や他局の CENSORED 版は、同じ自然話数を持つ同一作品として扱う。
    ## 録画タイトル自体は変更せず、Series の表示・比較に使う作品名からだけ既知の版表記を外す。
    display_title = BROADCAST_EDITION_SUFFIX_PATTERN.sub('', display_title).strip()

    # 話数の後ろに残る語句は放送枠名を除き、副題が別途なければ副題として保存する。
    trailing_text = episode_source[episode_match.end():].strip()
    trailing_text = re.sub(r'^\s*[◆◇].*$', '', trailing_text).strip()
    if subtitle is None and trailing_text:
        trailing_subtitle_match = QUOTED_SUBTITLE_PATTERN.fullmatch(trailing_text)
        subtitle = (
            trailing_subtitle_match.group('subtitle').strip()
            if trailing_subtitle_match is not None
            else trailing_text
        )

    normalized_title = NormalizeSeriesTitle(display_title)
    if len(normalized_title) < 2 or normalized_title in GENERIC_SERIES_TITLES:
        return None

    return ParsedSeriesTitle(
        display_title = display_title,
        normalized_title = normalized_title,
        episode_number = episode_number,
        subtitle = subtitle,
    )


def _episodeNumbersMatch(left: str | None, right: str | None) -> bool:
    """表記が違っても同じシーズン・話数なら一致とみなす。"""

    if left == right:
        return True
    left_parsed = ParseLegacyEpisodeNumber(left)
    right_parsed = ParseLegacyEpisodeNumber(right)
    if left_parsed is None or right_parsed is None:
        return False
    return (
        left_parsed.season_number == right_parsed.season_number
        and left_parsed.episode_number == right_parsed.episode_number
    )


class SeriesIndexer:
    """録画番組を確定的な作品タイトル単位で Series へ関連付ける。"""

    @staticmethod
    async def resolveAIFallbackSeries(
        *,
        existing_series_id: int | None,
        normalized_title: str,
    ) -> tuple[Series, str] | None:
        """AI が選んだ既存 Series ID の実在とタイトル関係を検証する。

        Args:
            existing_series_id: AI が同一作品として選んだ hints 内の既存 Series ID。
            normalized_title: AI が返した NormalizeSeriesTitle() 済みタイトル。

        Returns:
            検証済み Series とその正規化名。再利用できない場合は None。
        """

        if existing_series_id is None:
            return None
        candidate = await Series.get_or_none(id=existing_series_id)
        candidate_normalized_title = (
            candidate.normalized_title or NormalizeSeriesTitle(candidate.title)
            if candidate is not None
            else None
        )
        if (
            candidate is not None
            and candidate_normalized_title is not None
            and AreAIFallbackSeriesTitlesClose(normalized_title, candidate_normalized_title)
        ):
            return (candidate, candidate_normalized_title)
        logging.warning(
            '[SeriesIndexer] Ignored an AI fallback existing Series ID because its title did not match.',
        )
        return None

    @classmethod
    async def linkRecordedProgram(
        cls,
        recorded_program: RecordedProgram,
        *,
        _fallback_title: ParsedSeriesTitle | None = None,
        _fallback_series: Series | None = None,
        schedule_background_tasks: bool = True,
    ) -> bool:
        """
        1 件の録画番組を Series と放送期間へ関連付ける。

        Args:
            recorded_program (RecordedProgram): DB 保存済みの録画番組。
            _fallback_title (ParsedSeriesTitle | None): Web 根拠を検証済みの内部入力。
            _fallback_series (Series | None): ID とタイトルの整合性を検証済みの再利用対象。
            schedule_background_tasks: AI 補完・話数判定を非同期予約するか。

        Returns:
            bool: Series へ関連付けられた場合は True。
        """

        # 無効時は既存の所属を維持し、付与も剥奪もしない。
        if RecordedSeriesSettingsStore.getSettings().enabled is False:
            return False

        parsed_title = _fallback_title or ParseSeriesTitle(
            recorded_program.title,
            recorded_program.genres,
            recorded_program.description,
        )
        episode_less_similar_programs: list[RecordedProgram] = []
        if parsed_title is None and _fallback_title is None:
            # 無話数の定期番組は、同じ固定タイトルで始まる別の録画を根拠にする。
            ## 引用符の境界まで一致させ、似た文字列を持つ別番組の誤統合を防ぐ。
            ### ★ 区切りはシリーズ名と宣伝文の境界なので、装飾マーク除去後に空でない前字列のときだけ検索に使う。
            quote_indexes = [
                recorded_program.title.find(quote)
                for quote in ('「', '『')
                if recorded_program.title.find(quote) >= 0
            ]
            search_prefixes: list[str] = []
            if quote_indexes:
                search_prefixes.append(recorded_program.title[:min(quote_indexes)].strip())
            star_index = recorded_program.title.find('★')
            if star_index >= 0:
                star_prefix = PROGRAM_MARK_PATTERN.sub('', recorded_program.title[:star_index]).strip()
                if star_prefix != '':
                    search_prefixes.append(star_prefix)
            if search_prefixes:
                # 短い前字列は同じ録画由来の長い前字列を含むので、最短 1 本で両経路の候補を拾える。
                episode_less_title_prefix = min(search_prefixes, key=len)
                # DB の全角記号と一致させるため、検索には NFKC 前の EPG 原文を使う。
                episode_less_similar_programs = await RecordedProgram.filter(
                    title__startswith = episode_less_title_prefix,
                ).exclude(id=recorded_program.id).all()
                parsed_title = ParseEpisodeLessSeriesTitle(
                    recorded_program.title,
                    recorded_program.genres,
                    [similar_program.title for similar_program in episode_less_similar_programs],
                )
        if parsed_title is None:
            # AI で確定済みの完全一致 cache は rebuild でも再適用する。
            # cache がなければ所属を外し、未所属集合の非同期 Web 検索だけ予約する。
            from app.metadata.SeriesAIFallbackTask import SeriesAIFallbackTask
            cached_assignment = await SeriesAIFallbackTask.getCachedAssignment(recorded_program)
            if cached_assignment is not None:
                return await cls.applyAIFallback(
                    recorded_program,
                    display_title=cached_assignment[0],
                    normalized_title=cached_assignment[1],
                    existing_series_id=cached_assignment[2],
                )
            await cls._clearSeriesAssignment(recorded_program)
            if schedule_background_tasks:
                await SeriesAIFallbackTask.schedule()
            return False

        # 映画は作品種別をグルーピング identity に含め、同名 TV と混ざらないようにする。
        ## 識別キー・alias・canonical key を同じ区別で通し、表示名は作品名のままにする。
        ## AI fallback が検証した別 Series は、映画キーと不一致なら再利用しない。
        identity_title = (
            f'映画:{parsed_title.normalized_title}'
            if parsed_title.is_movie
            else parsed_title.normalized_title
        )
        if parsed_title.is_movie:
            if _fallback_series is not None and _fallback_series.normalized_title != identity_title:
                _fallback_series = None
            # 管理者の手動判断は映画の自動所属より優先し、所属も解除もせず維持する。
            manual_resolution = await RecordedSeriesResolution.filter(
                recorded_program_id=recorded_program.id,
                source='Manual',
                status__in=['Resolved', 'NotSeries'],
            ).first()
            if manual_resolution is not None:
                return False

        # 原則は normalized_title の完全一致だけで Series を再利用し、fuzzy 類似度による誤統合を防ぐ。
        ## Bangumi 条目で統合済みの放送局別表記は alias に残るため、主タイトルの次に完全一致で解決する。
        series = _fallback_series
        if series is None:
            series = await Series.get_or_none(normalized_title=identity_title)
            if series is None:
                series_alias = await SeriesAlias.get_or_none(
                    normalized_title = identity_title,
                ).select_related('series')
                if series_alias is not None:
                    series = series_alias.series
        is_series_created = False

        # 一部放送局は副題を丸ごと省略するため、同じ話数が別局の正式作品名へ既に存在する場合に限り、
        ## 「短縮名 + 明示的な副題境界」の前方一致を作品名 alias として扱う。
        if (
            _fallback_series is None
            and recorded_program.channel_id is not None
            and parsed_title.episode_number is not None
        ):
            longer_series_candidates = await Series.filter(
                normalized_title__startswith = parsed_title.normalized_title,
            ).all()
            for candidate in longer_series_candidates:
                if candidate.id == (series.id if series is not None else None):
                    continue
                if not IsStrictSeriesTitlePrefix(parsed_title.normalized_title, candidate.normalized_title):
                    continue
                has_same_episode_on_another_channel = await RecordedProgram.filter(
                    series_id = candidate.id,
                    episode_number = parsed_title.episode_number,
                ).exclude(channel_id=recorded_program.channel_id).exists()
                if has_same_episode_on_another_channel:
                    series = candidate
                    break

        if series is None:
            canonical_key = hashlib.sha256(identity_title.encode('utf-8')).hexdigest()
            canonical_key_owner = await Series.get_or_none(canonical_key=canonical_key)
            if canonical_key_owner is not None:
                series = canonical_key_owner
                is_series_created = False
                if series.normalized_title is None:
                    series.normalized_title = identity_title
                    await series.save(update_fields=['normalized_title', 'updated_at'])
            else:
                series, is_series_created = await Series.get_or_create(
                    normalized_title = identity_title,
                    defaults = {
                        'title': parsed_title.display_title,
                        # カナのみのタイトルは作成時に AI なしで読みを作る。
                        'title_reading': DeriveTitleReadingFromTitle(parsed_title.display_title),
                        'description': '',
                        'genres': recorded_program.genres,
                        'canonical_key': canonical_key,
                    },
                )
                if series.canonical_key is None:
                    series.canonical_key = canonical_key
                    await series.save(update_fields=['canonical_key', 'updated_at'])

        # 主タイトルも alias テーブルへ常に登録し、統合時にタイトル所有者を原子的に移せるようにする。
        await SeriesAlias.update_or_create(
            normalized_title = identity_title,
            defaults = {'series_id': series.id},
        )

        series_broadcast_period: SeriesBroadcastPeriod | None = None
        is_period_changed = False
        if recorded_program.channel_id is not None:
            broadcast_date = recorded_program.start_time.date()
            series_broadcast_period, is_period_created = await SeriesBroadcastPeriod.get_or_create(
                series_id = series.id,
                channel_id = recorded_program.channel_id,
                defaults = {
                    'start_date': broadcast_date,
                    'end_date': broadcast_date,
                },
            )

            # 同じ作品・チャンネルの放送日範囲を、実際に保存された録画に合わせて外側へ広げる。
            update_fields: list[str] = []
            if broadcast_date < series_broadcast_period.start_date:
                series_broadcast_period.start_date = broadcast_date
                update_fields.append('start_date')
            if broadcast_date > series_broadcast_period.end_date:
                series_broadcast_period.end_date = broadcast_date
                update_fields.append('end_date')
            if update_fields:
                await series_broadcast_period.save(update_fields=update_fields)
            is_period_changed = is_period_created or len(update_fields) > 0

            # 初放送日は TMDb → Bangumi → ローカル放送開始日の優先順。
            ## first_air_date_source へ記録した実際の由来で判定し、ローカル由来 (または未確定) の
            ## 日付は放送期間の追加・拡大に合わせて Series 全体の最古開始日へ更新する。
            ## 外部由来 ('Tmdb' / 'Bangumi') の日付は維持する。
            if series.first_air_date_source in (None, 'Local'):
                earliest_period = await SeriesBroadcastPeriod.filter(series_id=series.id).order_by('start_date').first()
                if earliest_period is not None and earliest_period.start_date != series.first_air_date:
                    # 保存条件も由来で絞り、待機中に enrich 等へ上書きされた外部由来の日付を壊さない。
                    await Series.filter(
                        Q(first_air_date_source__isnull=True) | Q(first_air_date_source='Local'),
                    ).filter(id=series.id).update(
                        first_air_date=earliest_period.start_date,
                        first_air_date_source='Local',
                        updated_at=datetime.now(tz=JST),
                    )

        # Program の identity と Resolution は片方だけを公開できないため、同じ transaction で更新する。
        resolution_invalidated = False
        async with transactions.in_transaction() as connection:
            resolution = await RecordedEpisodeResolution.filter(
                recorded_program_id=recorded_program.id,
            ).select_for_update().using_db(connection).first()
            previous_series_id = recorded_program.series_id
            previous_episode_number = recorded_program.episode_number
            series_changed = previous_series_id != series.id
            # 単一正整数を取れないときは、同じ Series の Web 検索 / AI 確定値を消さない。
            keep_confirmed_episode = False
            if (
                series_changed is False
                and ParseSinglePositiveIntegerEpisode(parsed_title.episode_number) is None
                and resolution is not None
                and resolution.status == 'Resolved'
                and resolution.source in ('WebSearch', 'AI')
            ):
                keep_confirmed_episode = True
            episode_identity_changed = (
                keep_confirmed_episode is False
                and _episodeNumbersMatch(previous_episode_number, parsed_title.episode_number) is False
            )
            # 旧実装の途中終了で Program だけ更新済みでも、Resolution との不一致から再無効化へ収束させる。
            # 同一 Series の非正整数で AI / WebSearch 確定を残すときは、この不一致検出で消さない。
            resolution_identity_mismatched = (
                keep_confirmed_episode is False
                and resolution is not None
                and resolution.episode_id != recorded_program.series_episode_id
            )
            is_recorded_program_changed = (
                series_changed or
                recorded_program.series_broadcast_period_id != (
                    series_broadcast_period.id if series_broadcast_period is not None else None
                ) or
                recorded_program.series_title != series.title or
                (
                    keep_confirmed_episode is False
                    and previous_episode_number != parsed_title.episode_number
                ) or
                recorded_program.subtitle != parsed_title.subtitle
            )
            # 所属 Series が変わるときは旧話数・Bangumi 照合を原子的に無効化する。
            if series_changed:
                recorded_program.series_episode_id = None
                recorded_program.bangumi_subject_id = None
                recorded_program.bangumi_episode_id = None
            elif episode_identity_changed:
                # 同じ Series でも確定話数が変わったら旧構造化 Episode と Bangumi episode を捨てる。
                recorded_program.series_episode_id = None
                recorded_program.bangumi_episode_id = None
            recorded_program.series_id = series.id
            recorded_program.series_broadcast_period_id = (
                series_broadcast_period.id if series_broadcast_period is not None else None
            )
            recorded_program.series_title = series.title
            if keep_confirmed_episode is False:
                recorded_program.episode_number = parsed_title.episode_number
            recorded_program.subtitle = parsed_title.subtitle
            if is_recorded_program_changed:
                update_fields = [
                    'series_id',
                    'series_broadcast_period_id',
                    'series_title',
                    'subtitle',
                ]
                if keep_confirmed_episode is False:
                    update_fields.append('episode_number')
                if series_changed:
                    update_fields.extend([
                        'series_episode_id',
                        'bangumi_subject_id',
                        'bangumi_episode_id',
                    ])
                elif episode_identity_changed:
                    update_fields.extend([
                        'series_episode_id',
                        'bangumi_episode_id',
                    ])
                await recorded_program.save(update_fields=update_fields, using_db=connection)
            # RecordedProgram の従属参照と同じ transaction で、旧 Resolved を再判定可能に戻す。
            if (
                resolution is not None
                and (series_changed or episode_identity_changed or resolution_identity_mismatched)
            ):
                await cls._invalidateEpisodeResolution(resolution, connection)
                resolution_invalidated = True

        # DB commit 後にだけワーカーへ投入する。起動前 rebuild は start() の Pending 復旧へ委ねる。
        if resolution_invalidated and schedule_background_tasks:
            from app.metadata.RecordedEpisodeAutomation import RecordedEpisodeAutomation
            await RecordedEpisodeAutomation.enqueue(
                recorded_program.id,
                start_if_needed=False,
            )

        # 2 件目の録画で無話数の定期番組と確定した場合は、根拠になった過去録画もすぐに同じ Series へ関連付ける。
        ## 未関連付けの録画だけを再評価するため、再帰先では現在の録画が根拠となり 1 回で収束する。
        if parsed_title.episode_number is None:
            for episode_less_program in episode_less_similar_programs:
                if episode_less_program.series_id is None:
                    await cls.linkRecordedProgram(
                        episode_less_program,
                        schedule_background_tasks=schedule_background_tasks,
                    )

        # 新しい録画や放送期間が加わったときだけ更新日時を進め、一覧の「更新が新しい順」へ反映する。
        if is_series_created or is_period_changed or is_recorded_program_changed:
            series.updated_at = datetime.now(tz=JST)
            await series.save(update_fields=['updated_at'])
        return True


    @classmethod
    async def applyAIFallback(
        cls,
        recorded_program: RecordedProgram,
        *,
        display_title: str,
        normalized_title: str,
        existing_series_id: int | None = None,
    ) -> bool:
        """Indexer 未所属の録画だけへ、Web 根拠付き完全一致タイトルを適用する。

        Args:
            recorded_program: 現在も未所属の録画番組。
            display_title: AI 検索で確定した作品表示名。
            normalized_title: NormalizeSeriesTitle() 済みの完全一致キー。
            existing_series_id: AI が同一作品として選んだ hints 内の既存 Series ID。

        Returns:
            AI 確定 Series へ関連付けた場合は True。
        """

        # 検索待機中に Indexer が確定可能になった録画は AI で上書きしない。
        if ParseSeriesTitle(
            recorded_program.title,
            recorded_program.genres,
            recorded_program.description,
        ) is not None:
            return False
        if (
            recorded_program.series_id is not None
            or normalized_title == ''
            or normalized_title in GENERIC_SERIES_TITLES
        ):
            return False

        # hints 内 ID でも削除 race や AI の候補誤選択はあり得るため、実在とタイトル関係を再検証する。
        # 検証を通った場合だけ既存側のタイトルを正本にし、返却された年号付き変名などを残さない。
        fallback_series: Series | None = None
        resolved_series = await cls.resolveAIFallbackSeries(
            existing_series_id=existing_series_id,
            normalized_title=normalized_title,
        )
        if resolved_series is not None:
            fallback_series, normalized_title = resolved_series
            display_title = fallback_series.title

        # 既存の所属・放送期間・Episode 無効化境界を再利用するため、検証済みタイトルを
        # 一時的な ParsedSeriesTitle として同じ本線へ渡す。
        parsed_title = ParsedSeriesTitle(
            display_title=display_title,
            normalized_title=normalized_title,
            episode_number=None,
            subtitle=recorded_program.subtitle,
        )
        return await cls.linkRecordedProgram(
            recorded_program,
            _fallback_title=parsed_title,
            _fallback_series=fallback_series,
        )


    @classmethod
    async def _invalidateEpisodeResolution(
        cls,
        resolution: RecordedEpisodeResolution,
        connection: BaseDBAsyncClient,
    ) -> None:
        """
        Series または話数 identity が変わった録画の旧 Resolution を再評価可能な状態へ戻す。

        Args:
            resolution (RecordedEpisodeResolution): Program と同じ transaction で無効化する判定状態。
            connection (BaseDBAsyncClient): Program 更新と共有する DB connection。

        Returns:
            None
        """

        # 起動復旧は Resolved / Manual / Cancelled をキューへ入れない。
        # 旧 episode 参照と確定状態を捨て、単一正整数なら Local、それ以外は Web 検索へ進める。
        resolution.episode_id = None
        resolution.season_number = None
        resolution.status = 'Pending'
        resolution.source = None
        resolution.lookup_outcome = None
        resolution.input_fingerprint = None
        resolution.provider_fingerprint = None
        resolution.proposed_outcome = None
        resolution.proposed_season_number = None
        resolution.proposed_episode_number = None
        resolution.web_search_performed = False
        resolution.error_code = None
        resolution.error_message = None
        resolution.resolved_at = None
        await resolution.save(
            update_fields=[
                'episode_id',
                'season_number',
                'status',
                'source',
                'lookup_outcome',
                'input_fingerprint',
                'provider_fingerprint',
                'proposed_outcome',
                'proposed_season_number',
                'proposed_episode_number',
                'web_search_performed',
                'error_code',
                'error_message',
                'resolved_at',
                'updated_at',
            ],
            using_db=connection,
        )


    @classmethod
    async def _clearSeriesAssignment(cls, recorded_program: RecordedProgram) -> None:
        """
        Indexer が所属を確定できない録画から Series 参照を外す。

        Args:
            recorded_program (RecordedProgram): 所属を解除する録画番組。

        Returns:
            None
        """

        if (
            recorded_program.series_id is None and
            recorded_program.series_broadcast_period_id is None and
            recorded_program.series_title is None and
            recorded_program.series_episode_id is None and
            recorded_program.bangumi_subject_id is None and
            recorded_program.bangumi_episode_id is None
        ):
            return
        # 未所属化でも旧話数・Bangumi 照合を残さない。
        recorded_program.series_id = None
        recorded_program.series_broadcast_period_id = None
        recorded_program.series_title = None
        recorded_program.series_episode_id = None
        recorded_program.bangumi_subject_id = None
        recorded_program.bangumi_episode_id = None
        await recorded_program.save(update_fields=[
            'series_id',
            'series_broadcast_period_id',
            'series_title',
            'series_episode_id',
            'bangumi_subject_id',
            'bangumi_episode_id',
        ])

    @classmethod
    async def rebuild(
        cls,
        *,
        scope: Literal['Unresolved', 'All'] = 'All',
        schedule_background_tasks: bool = True,
    ) -> int:
        """
        指定範囲の録画番組へ現在の確定的な Series 解析規則を適用する。

        Args:
            scope: 未所属・話数未確定だけ、または全録画を処理する範囲。
            schedule_background_tasks: AI 補完・話数判定を非同期予約するか。

        Returns:
            Series へ関連付けられた録画数。
        """

        # スイッチオフでは全件再適用せず、保存済みの所属を残す。
        if RecordedSeriesSettingsStore.getSettings().enabled is False:
            logging.info('Series index rebuild skipped because recorded series indexing is disabled.')
            return 0

        # 永続化済みの AI exact title を1回だけ復元し、全録画ループではメモリ snapshot を参照する。
        from app.metadata.SeriesAIFallbackTask import SeriesAIFallbackTask
        await SeriesAIFallbackTask.restoreResolvedAssignments()

        logging.info('Series index rebuild has started.')
        linked_count = 0
        last_seen_id = 0

        # 大量の録画を一括ロードせず、ID 順に小さなバッチで処理する。
        while True:
            recorded_programs_query = RecordedProgram.filter(id__gt=last_seen_id)
            if scope == 'Unresolved':
                recorded_programs_query = recorded_programs_query.filter(
                    Q(series_id__isnull=True) | Q(series_episode_id__isnull=True),
                )
            recorded_programs = await recorded_programs_query.order_by('id').limit(100)
            if len(recorded_programs) == 0:
                break
            for recorded_program in recorded_programs:
                if await cls.linkRecordedProgram(
                    recorded_program,
                    schedule_background_tasks=schedule_background_tasks,
                ):
                    linked_count += 1
                last_seen_id = recorded_program.id

        # 短縮タイトルが正式タイトルより先に登録された場合でも同じ結果へ収束させる。
        ## 全録画を再走査せず、厳密な前方一致となる長い Series が実在する短い Series の録画だけを再評価する。
        if scope == 'All':
            all_series = await Series.all()
            for short_series in all_series:
                has_longer_candidate = any(
                    candidate.id != short_series.id and
                    IsStrictSeriesTitlePrefix(short_series.normalized_title, candidate.normalized_title)
                    for candidate in all_series
                )
                if not has_longer_candidate:
                    continue
                short_series_programs = await RecordedProgram.filter(series_id=short_series.id).all()
                for recorded_program in short_series_programs:
                    await cls.linkRecordedProgram(
                        recorded_program,
                        schedule_background_tasks=schedule_background_tasks,
                    )

        # ルール改善で別 Series へ移った録画の古い放送期間とカードだけを後始末する。
        ## RecordedProgram がまだ参照する Series は消さないため、CASCADE で録画自体が失われることはない。
        connection = connections.get('default')
        await connection.execute_query(
            'DELETE FROM series_broadcast_periods '
            'WHERE NOT EXISTS ('
            'SELECT 1 FROM recorded_programs '
            'WHERE recorded_programs.series_broadcast_period_id = series_broadcast_periods.id'
            ')',
        )
        await connection.execute_query(
            'DELETE FROM series '
            'WHERE NOT EXISTS ('
            'SELECT 1 FROM recorded_programs WHERE recorded_programs.series_id = series.id'
            ')',
        )

        logging.info(f'Series index rebuild has completed. linked_recorded_programs: {linked_count}')
        return linked_count
