import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from app.utils.KonomiTVBS4KBangumiClient import KonomiTVBS4KBangumiClient


class BangumiClientTest(unittest.TestCase):
    """Bangumi 收藏候補の一括照合と視聴完了判定を検証する。"""

    def test_only_single_positive_integer_episode_is_accepted(self) -> None:
        """単一の正整数以外の話数は自動同期しない。"""

        self.assertEqual(KonomiTVBS4KBangumiClient.parseEpisodeNumber('12'), 12)
        self.assertEqual(KonomiTVBS4KBangumiClient.parseEpisodeNumber('#12'), 12)
        self.assertIsNone(KonomiTVBS4KBangumiClient.parseEpisodeNumber('0'))
        self.assertIsNone(KonomiTVBS4KBangumiClient.parseEpisodeNumber('1・2'))
        self.assertIsNone(KonomiTVBS4KBangumiClient.parseEpisodeNumber('4.5'))
        self.assertIsNone(KonomiTVBS4KBangumiClient.parseEpisodeNumber(None))


    def test_long_title_matches_collection_subject_without_search(self) -> None:
        """長い EPG 作品名でも收藏一覧内の同名条目へ完全一致できる。"""

        expected_subject: dict[str, Any] = {
            'id': 590786,
            'type': 2,
            'name': 'ここは俺に任せて先に行けと言ってから10年がたったら伝説になっていた。',
            'name_cn': '『你们先走我断后』，于是10年后我成为了传说',
        }
        unrelated_subject: dict[str, Any] = {
            'id': 8365,
            'type': 2,
            'name': 'ここはグリーン・ウッド',
            'name_cn': '绿林寮',
        }

        matched_subject = KonomiTVBS4KBangumiClient.findSubject(
            'ここは俺に任せて先に行けと言ってから10年がたったら伝説になっていた。',
            [unrelated_subject, expected_subject],
        )

        self.assertIsNotNone(matched_subject)
        assert matched_subject is not None
        self.assertEqual(matched_subject['id'], 590786)


    def test_prefix_score_is_eighty_and_ambiguous_titles_are_skipped(self) -> None:
        """收藏採点は完全一致 100 / 副題境界 80 とし、同点は見送る。"""

        exact = {'id': 1, 'type': 2, 'name': 'テスト作品', 'name_cn': ''}
        self.assertEqual(KonomiTVBS4KBangumiClient._scoreSubjectTitle('テスト作品', exact), 100)
        prefix = {'id': 2, 'type': 2, 'name': 'テスト作品：副題', 'name_cn': ''}
        self.assertEqual(KonomiTVBS4KBangumiClient._scoreSubjectTitle('テスト作品', prefix), 80)
        subjects = [
            {'id': 1, 'type': 2, 'name': '同名作品', 'name_cn': ''},
            {'id': 2, 'type': 2, 'name': '同名作品', 'name_cn': ''},
        ]
        self.assertIsNone(KonomiTVBS4KBangumiClient.findSubject('同名作品', subjects))


    def test_trailing_period_difference_is_ignored(self) -> None:
        """EPG だけが長い作品名の末尾句点を省略しても同じ收藏条目として扱う。"""

        subject = {
            'id': 590786,
            'type': 2,
            'name': 'ここは俺に任せて先に行けと言ってから10年がたったら伝説になっていた。',
            'name_cn': '',
        }
        matched_subject = KonomiTVBS4KBangumiClient.findSubject(
            'ここは俺に任せて先に行けと言ってから10年がたったら伝説になっていた',
            [subject],
        )

        self.assertIsNotNone(matched_subject)


    def test_playback_completion_is_decided_at_ninety_percent(self) -> None:
        """30 分番組は 27 分到達時点から完了と判定する。"""

        self.assertFalse(KonomiTVBS4KBangumiClient.isPlaybackCompleted(1619.9, 1800.0, None))
        self.assertTrue(KonomiTVBS4KBangumiClient.isPlaybackCompleted(1620.0, 1800.0, None))


    def test_playback_completion_uses_three_minutes_before_last_cm(self) -> None:
        """最後の CM が 25 分開始なら、その 3 分前の 22 分から完了と判定する。"""

        cm_sections = [
            {'start_time': 600.0, 'end_time': 720.0},
            {'start_time': 1500.0, 'end_time': 1800.0},
        ]

        self.assertFalse(KonomiTVBS4KBangumiClient.isPlaybackCompleted(1319.9, 1800.0, cm_sections))
        self.assertTrue(KonomiTVBS4KBangumiClient.isPlaybackCompleted(1320.0, 1800.0, cm_sections))


class BangumiClientAsyncTest(unittest.IsolatedAsyncioTestCase):
    """Bangumi API を呼び出す前のローカル対象判定を検証する。"""

    async def test_collection_api_is_not_called_without_anime_series(self) -> None:
        """アニメ・特撮の Series がない環境では收藏一覧を取得しない。"""

        series_query: asyncio.Future[list[Any]] = asyncio.Future()
        series_query.set_result([])
        get_collection_subjects = AsyncMock()
        with (
            patch('app.utils.KonomiTVBS4KBangumiClient.Series.all', return_value=series_query),
            patch.object(KonomiTVBS4KBangumiClient, '_getCollectionSubjects', get_collection_subjects),
        ):
            matched_count = await KonomiTVBS4KBangumiClient.syncUserCollections(AsyncMock())

        self.assertEqual(matched_count, 0)
        get_collection_subjects.assert_not_awaited()


    async def test_nsfw_episode_request_uses_bearer_token(self) -> None:
        """NSFW 条目の episode 取得にも連携済みユーザーの Bearer token を渡す。"""

        response = MagicMock()
        response.status_code = 404
        httpx_client = AsyncMock()
        httpx_client.get.return_value = response
        httpx_client.__aenter__.return_value = httpx_client
        httpx_client.__aexit__.return_value = None
        with patch('app.utils.KonomiTVBS4KBangumiClient.HTTPX_CLIENT', return_value=httpx_client):
            episodes = await KonomiTVBS4KBangumiClient._getEpisodes(295001, 'test-token')  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(episodes, [])
        self.assertEqual(
            httpx_client.get.await_args.kwargs['headers']['Authorization'],
            'Bearer test-token',
        )
        response.raise_for_status.assert_not_called()

    async def test_bind_recorded_program_sets_episode_id_after_lookup(self) -> None:
        """話数確定後の再結合は、既存 subject から episode ID を保存する。"""

        recorded_program = MagicMock()
        recorded_program.series_id = 2
        recorded_program.bangumi_subject_id = 10
        recorded_program.bangumi_episode_id = None
        recorded_program.episode_number = '3'
        recorded_program.save = AsyncMock()
        series = MagicMock()
        series.bangumi_subject_id = 10

        with (
            patch(
                'app.utils.KonomiTVBS4KBangumiClient.RecordedProgram.get_or_none',
                new=AsyncMock(return_value=recorded_program),
            ),
            patch(
                'app.utils.KonomiTVBS4KBangumiClient.Series.get_or_none',
                new=AsyncMock(return_value=series),
            ),
            patch(
                'app.utils.KonomiTVBS4KBangumiClient.KonomiTVBS4KBangumiSharedStore.decryptAccessToken',
                return_value='test-token',
            ),
            patch.object(
                KonomiTVBS4KBangumiClient,
                '_getEpisodes',
                new=AsyncMock(return_value=[{'id': 99, 'type': 0, 'ep': 3, 'sort': 2}]),
            ),
        ):
            await KonomiTVBS4KBangumiClient.bindRecordedProgramById(1)

        self.assertEqual(recorded_program.bangumi_episode_id, 99)
        recorded_program.save.assert_awaited()


class _ExcludeChain:
    """User.all().exclude().exclude() を固定ユーザー一覧へ解決する。"""

    def __init__(self, users: list[Any]) -> None:
        self.users = users

    def exclude(self, **_kwargs: object) -> _ExcludeChain:
        return self

    def __await__(self) -> Any:
        async def Resolve() -> list[Any]:
            return self.users
        return Resolve().__await__()
