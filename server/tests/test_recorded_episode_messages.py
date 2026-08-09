import pytest

from app.metadata.RecordedEpisodeMessages import (
    FormatAcpHardTimeoutMessage,
    GetRecordedEpisodeErrorMessage,
)


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
        (
            'Timeout',
            'AI プロバイダーからの応答が一定時間途絶えたためタイムアウトしました。',
        ),
        (
            'HardTimeout',
            FormatAcpHardTimeoutMessage(subject='AI プロバイダー'),
        ),
        (
            'EpisodeLookupCapabilityNotVerified',
            '選択中の AI バックエンドでは話数 Web 検索の接続試験が完了していません。'
            'AI バックエンド設定で接続試験を実行してください。',
        ),
    ],
)
def test_episode_lookup_error_codes_have_safe_japanese_messages(
    error_code: str,
    expected: str,
) -> None:
    """旧経路を含む既知コードを、生例外へ展開せず安全な日本語へ変換する。"""

    assert GetRecordedEpisodeErrorMessage(error_code) == expected
