import pytest

from app.metadata.RecordedEpisodeMessages import GetRecordedEpisodeErrorMessage


@pytest.mark.parametrize(
    ('error_code', 'expected'),
    [
        (
            'LowConfidence',
            'Web 検索結果の信頼度が受理条件を満たしませんでした。',
        ),
        (
            'EpisodeLookupFailed',
            '話数 Web 検索に失敗しました。',
        ),
    ],
)
def test_episode_lookup_error_codes_have_safe_japanese_messages(
    error_code: str,
    expected: str,
) -> None:
    """旧経路を含む既知コードを、生例外へ展開せず安全な日本語へ変換する。"""

    assert GetRecordedEpisodeErrorMessage(error_code) == expected
