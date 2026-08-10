import pytest

from app.metadata.RecordedSeriesCandidates import (
    RecordedSeriesAIError,
    RecordedSeriesProgramDetailItem,
    RecordedSeriesProgramPrompt,
)
from app.metadata.RecordedSeriesGeneration import (
    BuildSeriesMetadataSystemPrompt,
    ParseStrictSeriesMetadataJSONObject,
    SeriesMetadataClusterHint,
    SeriesMetadataClusterProgramHint,
    SeriesMetadataExistingSeriesHint,
    SeriesMetadataHints,
    SeriesMetadataLocalParseHint,
    SeriesMetadataWikipediaHint,
    ValidateSeriesMetadataOutput,
)


def Program() -> RecordedSeriesProgramPrompt:
    """生成契約テスト用の録画入力を返す。"""

    return RecordedSeriesProgramPrompt(
        title='ドラマ「汚れた番組表タイトル」第3話',
        description='第三話の概要。',
        detail_items=[RecordedSeriesProgramDetailItem(name='番組内容', value='第三話の詳細。')],
        genres=['ドラマ'],
        channel_id='test011',
        channel_name='テスト局',
        broadcast_datetime='2026-07-28T21:00:00+09:00',
        duration_seconds=2700.0,
    )


def Hints() -> SeriesMetadataHints:
    """ID 制約を含む最小 hints を返す。"""

    return SeriesMetadataHints(
        local_parse=SeriesMetadataLocalParseHint(
            series_title='ドラマ「汚れた番組表タイトル」',
            season_number='1',
            episode_number='#3',
            subtitle=None,
        ),
        cluster=SeriesMetadataClusterHint(
            display_title='ドラマ「汚れた番組表タイトル」',
            normalized_key='ドラマ汚れた番組表タイトル',
            member_count=1,
            representative_programs=[
                SeriesMetadataClusterProgramHint(
                    title='ドラマ「汚れた番組表タイトル」第3話',
                    description='第三話の概要。',
                    broadcast_datetime='2026-07-28T21:00:00+09:00',
                    duration_seconds=2700.0,
                ),
            ],
        ),
        existing_series=[
            SeriesMetadataExistingSeriesHint(
                id=42,
                title='正規作品名',
                description='既存 Series。',
                wikipedia_page_id=123,
                similarity=0.95,
                match_reason='FuzzySimilarity',
            ),
        ],
        wikipedia=[
            SeriesMetadataWikipediaHint(
                page_id=123,
                title='正規作品名',
                extract='Wikipedia の参考情報。',
            ),
        ],
    )


def Validate(output: dict[str, object]):
    """監査値を固定して共通 validator を呼ぶ。"""

    return ValidateSeriesMetadataOutput(
        output,
        hints=Hints(),
        model='test-model',
        prompt_tokens=10,
        completion_tokens=5,
        http_status=200,
        latency_ms=20,
    )


def test_free_generated_title_and_decimal_episode_are_accepted() -> None:
    result = Validate({
        'decision': 'Series',
        'series_title': ' 正規作品名 ',
        'season_number': 1,
        'episode_number': '3.50',
        'subtitle': ' 第三話 ',
        'confidence': 0.01,
        'existing_series_id': None,
        'wikipedia_page_id': None,
        'rationale_short': '短い根拠',
    })

    assert result.series_title == '正規作品名'
    assert str(result.episode_number) == '3.5'
    assert result.subtitle == '第三話'
    # 自由生成は confidence 閾値を設けず、schema 範囲内なら採用する。
    assert result.confidence == 0.01


def test_ids_outside_hints_are_ignored_without_rejecting_generated_title() -> None:
    result = Validate({
        'decision': 'Series',
        'series_title': '新規作品名',
        'season_number': 1,
        'episode_number': 3,
        'subtitle': None,
        'confidence': 0.9,
        'existing_series_id': 999,
        'wikipedia_page_id': 999,
        'rationale_short': None,
    })

    assert result.existing_series_id is None
    assert result.wikipedia_page_id is None
    assert str(result.episode_number) == '3'


def test_no_published_number_preserves_identified_season() -> None:
    """シリーズAIの公開話数なし判定では、特定できたシーズンだけを保持する。"""

    result = Validate({
        'decision': 'Series',
        'series_title': '正規作品名',
        'season_number': 2,
        'episode_number': 'NoPublishedNumber',
        'subtitle': '総集編',
        'confidence': 0.8,
        'existing_series_id': 42,
        'wikipedia_page_id': 123,
        'rationale_short': '同作品の総集編',
    })

    assert result.season_number == 2
    assert result.episode_number is None
    assert result.episode_not_numbered is False
    assert result.episode_no_published_number is True


def test_numbered_episode_without_explicit_season_defaults_to_one() -> None:
    """明示シーズンのない番号付き番組を Season 1 へ正規化する。"""

    result = Validate({
        'decision': 'Series',
        'series_title': '宝塚カフェブレイク',
        'season_number': None,
        'episode_number': '1266',
        'subtitle': None,
        'confidence': 0.95,
        'existing_series_id': 42,
        'wikipedia_page_id': None,
        'rationale_short': '長期継続番組の第1266回',
    })

    assert result.season_number == 1
    assert str(result.episode_number) == '1266'


def test_system_prompt_defaults_numbered_program_without_explicit_season_to_one() -> None:
    """生成指示とサーバー側の Season 1 正規化契約を一致させる。"""

    assert (
        'Use season_number 1 when a numbered program has no explicit seasons.'
        in BuildSeriesMetadataSystemPrompt()
    )


@pytest.mark.parametrize(
    'output',
    [
        {
            'decision': 'Series',
            'series_title': '   ',
            'season_number': 1,
            'episode_number': '3',
            'subtitle': None,
            'confidence': 0.9,
            'existing_series_id': None,
            'wikipedia_page_id': None,
            'rationale_short': None,
        },
        {
            'decision': 'Series',
            'series_title': '作品名',
            'season_number': 100,
            'episode_number': '3',
            'subtitle': None,
            'confidence': 0.9,
            'existing_series_id': None,
            'wikipedia_page_id': None,
            'rationale_short': None,
        },
        {
            'decision': 'Series',
            'series_title': '作品名',
            'season_number': 1,
            'episode_number': 3.5,
            'subtitle': None,
            'confidence': 0.9,
            'existing_series_id': None,
            'wikipedia_page_id': None,
            'rationale_short': None,
        },
        {
            'decision': 'NotSeries',
            'series_title': '部分採用禁止',
            'season_number': None,
            'episode_number': None,
            'subtitle': None,
            'confidence': 0.9,
            'existing_series_id': None,
            'wikipedia_page_id': None,
            'rationale_short': None,
        },
    ],
)
def test_schema_or_correlation_violation_rejects_the_whole_output(
    output: dict[str, object],
) -> None:
    with pytest.raises(RecordedSeriesAIError) as error:
        Validate(output)
    assert error.value.code == 'InvalidSeriesMetadataSchema'


def test_strict_json_parser_rejects_markdown_wrapper() -> None:
    with pytest.raises(RecordedSeriesAIError) as error:
        ParseStrictSeriesMetadataJSONObject(
            '```json\n'
            '{"decision":"Unresolved","series_title":null,'
            '"season_number":null,"episode_number":null,"subtitle":null,'
            '"confidence":0.2,"existing_series_id":null,'
            '"wikipedia_page_id":null,"rationale_short":null}\n```',
        )
    assert error.value.code == 'InvalidJSON'


def test_program_fixture_is_available_for_local_imports() -> None:
    # Program() は他テストから流用されることがあるため、生成できることを保証する。
    assert Program()['channel_name'] == 'テスト局'
