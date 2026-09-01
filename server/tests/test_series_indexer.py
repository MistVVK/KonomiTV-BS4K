import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.metadata.SeriesIndexer import ParseSeriesTitle, SeriesIndexer
from app.schemas import Genre


ANIME_GENRE: Genre = {'major': 'アニメ・特撮', 'middle': '国内アニメ'}


def test_explicit_episode_number_is_parsed() -> None:
    """明示話数のあるタイトルから作品名と話数を取り出す。"""

    parsed = ParseSeriesTitle('テスト作品 第12話「副題」', [ANIME_GENRE])
    assert parsed is not None
    assert parsed.display_title == 'テスト作品'
    assert parsed.episode_number == '12'
    assert parsed.subtitle == '副題'


def test_episodeless_regular_title_is_not_indexed_from_title_alone() -> None:
    """無話数の定期番組はタイトル単独では Series にしない。"""

    parsed = ParseSeriesTitle('バラエティ番組「今回の企画」', [{'major': 'バラエティ', 'middle': 'お笑い・バラエティ'}])
    assert parsed is None


def test_generic_slot_titles_are_excluded() -> None:
    """汎用枠は Series を自動生成しない。"""

    parsed = ParseSeriesTitle('ニュース 第1話', [ANIME_GENRE])
    assert parsed is None
    parsed_weather = ParseSeriesTitle('天気予報 第2話', [ANIME_GENRE])
    assert parsed_weather is None


def test_unparsed_title_clears_series_assignment() -> None:
    """Indexer が解析できない録画は series_id を外す。"""

    async def Run() -> None:
        recorded_program = MagicMock()
        recorded_program.id = 1
        recorded_program.series_id = 9
        recorded_program.series_broadcast_period_id = 3
        recorded_program.series_title = '旧シリーズ'
        recorded_program.title = '単発番組'
        recorded_program.genres = [ANIME_GENRE]
        recorded_program.description = ''
        recorded_program.save = AsyncMock()
        with patch('app.metadata.SeriesIndexer.ParseSeriesTitle', return_value=None):
            linked = await SeriesIndexer.linkRecordedProgram(recorded_program)
        assert linked is False
        assert recorded_program.series_id is None
        recorded_program.save.assert_awaited()

    asyncio.run(Run())
