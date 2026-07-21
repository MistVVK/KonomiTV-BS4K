import pytest

from app.metadata.SeriesTitleParser import BuildSeriesGroupingKey, ParseSeriesTitle
from app.schemas import Genre


ANIME_GENRE: list[Genre] = [{'major': 'アニメ・特撮', 'middle': '国内アニメ'}]
DRAMA_GENRE: list[Genre] = [{'major': 'ドラマ', 'middle': '国内ドラマ'}]


@pytest.mark.parametrize(
    ('title', 'description', 'genres', 'series_title', 'episode_number'),
    [
        ('美少女戦士セーラームーン', '第2話「おしおきよ! 占いハウスは妖魔の館」', ANIME_GENRE,
         '美少女戦士セーラームーン', '第2話'),
        ('最強の王様、二度目の人生は何をする? Season2 #12', '最終話', ANIME_GENRE,
         '最強の王様、二度目の人生は何をする?', 'Season 2 #12'),
        ('アストリッドとラファエル6 文書係の事件録 (3)', '事件概要', DRAMA_GENRE,
         'アストリッドとラファエル', 'Season 6 #3'),
        ('[4K]銀二貫(7)', '七話の概要', DRAMA_GENRE, '銀二貫', '#7'),
        ('[4K]【BS時代劇】銀二貫(7)「糸寒天の味」', '七話の概要', DRAMA_GENRE, '銀二貫', '#7'),
        ('ヨーロッパ トラムの旅(8)「ドイツ・ドレスデン」', '旅の概要',
         [{'major': 'ドキュメンタリー・教養', 'middle': '歴史・紀行'}], 'ヨーロッパ トラムの旅', '#8'),
        ('事件は、その周りで起きている シリーズ3(3)小芝風花主演・警察コメディー', '番組概要',
         DRAMA_GENRE, '事件は、その周りで起きている', 'Season 3 #3'),
    ],
)
def test_parse_explicit_episode_formats(
    title: str,
    description: str,
    genres: list[Genre],
    series_title: str,
    episode_number: str,
) -> None:
    result = ParseSeriesTitle(title, description, {}, genres)
    assert result.series_title == series_title
    assert result.episode_number == episode_number
    assert result.has_explicit_episode is True
    assert result.is_hard_standalone is False


def test_description_episode_keeps_subtitle() -> None:
    result = ParseSeriesTitle(
        '美少女戦士セーラームーン',
        '第2話「おしおきよ! 占いハウスは妖魔の館」',
        {},
        ANIME_GENRE,
    )
    assert result.subtitle == 'おしおきよ! 占いハウスは妖魔の館'
    assert result.episode_source == 'Description'


def test_meaningful_parentheses_are_not_removed() -> None:
    result = ParseSeriesTitle('ドラマ「0.5の男」特別編', '番組概要', {}, DRAMA_GENRE)
    assert result.subtitle is not None
    assert '0.5の男' in result.subtitle  # 引用符の中身を失わず副題候補として保持する
    assert result.series_title == 'ドラマ'


@pytest.mark.parametrize(
    ('title', 'genres'),
    [
        ('金曜ロードショー「となりのトトロ」', [{'major': '映画', 'middle': '邦画'}]),
        ('劇場版 AKIRA', ANIME_GENRE),
        ('試験電波', [{'major': 'その他', 'middle': 'その他'}]),
        ('宝塚歌劇 花組公演', [{'major': '劇場・公演', 'middle': '国内ダンス・バレエ'}]),
    ],
)
def test_standalone_programs_are_not_promoted_by_genre(title: str, genres: list[Genre]) -> None:
    result = ParseSeriesTitle(title, '番組概要', {}, genres)
    assert result.has_explicit_episode is False
    assert result.is_hard_standalone or result.is_soft_standalone


def test_stage_genre_is_soft_evidence_so_repeated_programs_can_still_group() -> None:
    result = ParseSeriesTitle(
        '宝塚カフェブレイク',
        'ゲストを迎えるトーク番組',
        {},
        [{'major': '劇場・公演', 'middle': '国内ダンス・バレエ'}],
    )
    assert result.is_hard_standalone is False
    assert result.is_soft_standalone is True


def test_secondary_stage_genre_does_not_make_a_music_program_standalone() -> None:
    result = ParseSeriesTitle(
        'クラシックTV「ザ・メイキング・オブ・モーツァルト」',
        'クラシック音楽を掘り下げるレギュラー番組',
        {},
        [
            {'major': '音楽', 'middle': 'クラシック・オペラ'},
            {'major': '劇場・公演', 'middle': 'その他'},
        ],
    )
    assert result.is_soft_standalone is False


@pytest.mark.parametrize('title', ['映画「作品A」', '映画「作品B」'])
def test_primary_movie_genre_without_an_episode_is_hard_standalone(title: str) -> None:
    """汎用の映画枠 root が同じでも、異なる作品をシリーズ候補へ昇格させない。"""

    result = ParseSeriesTitle(
        title,
        '映画の概要',
        {},
        [{'major': '映画', 'middle': '邦画'}],
    )

    assert result.series_title == '映画'
    assert result.has_explicit_episode is False
    assert result.is_hard_standalone is True
    assert result.is_soft_standalone is False


def test_year_suffix_is_not_mistaken_for_an_attached_season() -> None:
    result = ParseSeriesTitle(
        'プロジェクトX2025 特別編 (3)',
        '番組概要',
        {},
        DRAMA_GENRE,
    )

    assert result.series_title == 'プロジェクトX2025 特別編'
    assert result.episode_number == '#3'
    assert result.season_number is None


def test_grouping_key_normalizes_width_space_and_case() -> None:
    assert BuildSeriesGroupingKey('ＡＢＣ　シリーズ') == BuildSeriesGroupingKey('abc シリーズ')


def test_japanese_long_vowel_mark_is_not_trimmed_as_a_separator() -> None:
    result = ParseSeriesTitle(
        '金曜ロードショー「となりのトトロ」',
        '映画紹介',
        {},
        [{'major': '映画', 'middle': '邦画'}],
    )
    assert result.series_title == '金曜ロードショー'


def test_movie_second_installment_in_description_is_not_an_episode() -> None:
    result = ParseSeriesTitle(
        'プレミアムシネマ「架空映画」',
        '大ヒットシリーズ第二弾を放送',
        {},
        [{'major': '映画', 'middle': '洋画'}],
    )
    assert result.has_explicit_episode is False
    assert result.is_hard_standalone is True


def test_past_installment_mention_in_music_detail_is_not_current_episode() -> None:
    result = ParseSeriesTitle(
        'うたコン 特別編',
        '音楽番組',
        {'番組内容': '名場面とともに第1回を振り返る'},
        [{'major': '音楽', 'middle': '国内ロック・ポップス'}],
    )
    assert result.has_explicit_episode is False
