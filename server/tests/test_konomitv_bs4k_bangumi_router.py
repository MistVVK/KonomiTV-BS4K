import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import HTTPException

from app import schemas
from app.routers.KonomiTVBS4KBangumiRouter import (
    BangumiAccountLogoutAPI,
    BangumiAuthAPI,
    BangumiPlaybackProgressAPI,
)


class FakeBangumiResponse:
    """Bangumi API レスポンスのうち、認証ルーターが参照する情報だけを保持する。"""

    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.payload = payload

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeHTTPXClient:
    """Bangumi API への GET を固定レスポンスへ置き換える非同期クライアント。"""

    def __init__(self, response: FakeBangumiResponse) -> None:
        self.response = response
        self.request_url = ''
        self.request_headers: dict[str, str] = {}

    async def __aenter__(self) -> 'FakeHTTPXClient':
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback

    async def get(self, url: str, headers: dict[str, str]) -> FakeBangumiResponse:
        self.request_url = url
        self.request_headers = headers
        return self.response


class FakeUser:
    """Bangumi 認証 API が受け取る管理者ユーザーの最小再現。"""

    def __init__(self) -> None:
        self.id = 1
        self.saved = False

    async def save(self) -> None:
        self.saved = True


class BangumiRouterTest(unittest.IsolatedAsyncioTestCase):
    """Bangumi 個人アクセストークンの検証、保存、連携解除を検証する。"""

    async def test_access_token_is_encrypted_and_not_exposed_by_user_schema(self) -> None:
        """共有トークンは User API に出さず、個人 Bangumi 列も公開しない。"""

        self.assertNotIn('bangumi_access_token', schemas.User.model_fields)
        self.assertNotIn('bangumi_user_id', schemas.User.model_fields)
        self.assertNotIn('bangumi_user_name', schemas.User.model_fields)
        self.assertNotIn('bangumi_user_nickname', schemas.User.model_fields)
        self.assertNotIn('bangumi_user_avatar_url', schemas.User.model_fields)

    async def test_valid_access_token_links_profile_and_stores_encrypted_token(self) -> None:
        """有効なトークンでは公開プロフィールと暗号化済みトークンだけを保存する。"""

        response = FakeBangumiResponse(200, {
            'id': 123,
            'username': 'huggy',
            'nickname': 'Huggy',
            'avatar': {'large': 'https://lain.bgm.tv/avatar.jpg'},
        })
        httpx_client = FakeHTTPXClient(response)
        current_user = FakeUser()

        with (
            patch('app.routers.KonomiTVBS4KBangumiRouter.HTTPX_CLIENT', return_value=httpx_client),
            patch('app.routers.KonomiTVBS4KBangumiRouter.KonomiTVBS4KBangumiClient.scheduleUserCollectionSync') as schedule_sync,
            patch('app.routers.KonomiTVBS4KBangumiRouter.KonomiTVBS4KBangumiSharedStore.save') as save_shared,
        ):
            await BangumiAuthAPI(
                auth_request = schemas.BangumiAuthRequest(access_token='  personal-token\n'),
                current_user = current_user,  # type: ignore[arg-type]
            )

        self.assertEqual(httpx_client.request_url, 'https://api.bgm.tv/v0/me')
        self.assertEqual(httpx_client.request_headers['Authorization'], 'Bearer personal-token')
        save_shared.assert_called_once()
        self.assertEqual(save_shared.call_args.kwargs['bangumi_user_id'], 123)
        self.assertEqual(save_shared.call_args.kwargs['access_token'], 'personal-token')
        self.assertFalse(current_user.saved)
        schedule_sync.assert_called_once_with()

    async def test_invalid_access_token_does_not_replace_existing_link(self) -> None:
        """無効なトークンでは既存の連携情報を変更しない。"""

        httpx_client = FakeHTTPXClient(FakeBangumiResponse(401, {'detail': 'Unauthorized'}))
        current_user = FakeUser()

        with (
            patch('app.routers.KonomiTVBS4KBangumiRouter.HTTPX_CLIENT', return_value=httpx_client),
            patch('app.routers.KonomiTVBS4KBangumiRouter.KonomiTVBS4KBangumiSharedStore.save') as save_shared,
        ):
            with self.assertRaises(HTTPException) as raised:
                await BangumiAuthAPI(
                    auth_request = schemas.BangumiAuthRequest(access_token='invalid-token'),
                    current_user = current_user,  # type: ignore[arg-type]
                )

        self.assertEqual(raised.exception.status_code, 422)
        save_shared.assert_not_called()
        self.assertFalse(current_user.saved)

    async def test_network_error_does_not_replace_existing_link(self) -> None:
        """Bangumi API への接続失敗では既存の連携情報を変更しない。"""

        current_user = FakeUser()

        class NetworkErrorClient(FakeHTTPXClient):
            async def get(self, url: str, headers: dict[str, str]) -> FakeBangumiResponse:
                raise httpx.NetworkError('network error')

        httpx_client = NetworkErrorClient(FakeBangumiResponse(200, {}))
        with (
            patch('app.routers.KonomiTVBS4KBangumiRouter.HTTPX_CLIENT', return_value=httpx_client),
            patch('app.routers.KonomiTVBS4KBangumiRouter.KonomiTVBS4KBangumiSharedStore.save') as save_shared,
        ):
            with self.assertRaises(HTTPException) as raised:
                await BangumiAuthAPI(
                    auth_request = schemas.BangumiAuthRequest(access_token='personal-token'),
                    current_user = current_user,  # type: ignore[arg-type]
                )

        self.assertEqual(raised.exception.status_code, 503)
        save_shared.assert_not_called()
        self.assertFalse(current_user.saved)

    async def test_logout_clears_profile_and_access_token(self) -> None:
        """連携解除では公開プロフィールと認証情報をまとめて消去する。"""

        current_user = FakeUser()

        with patch('app.routers.KonomiTVBS4KBangumiRouter.KonomiTVBS4KBangumiSharedStore.clear') as clear_shared:
            await BangumiAccountLogoutAPI(current_user=current_user)  # type: ignore[arg-type]

        clear_shared.assert_called_once()
        self.assertFalse(current_user.saved)

    async def test_progress_does_not_call_bangumi_when_episode_is_unmapped(self) -> None:
        """未照合の録画は Bangumi API を呼ばず Pending を返す。"""

        current_user = FakeUser()
        recorded_video = MagicMock()
        recorded_video.duration = 1800.0
        recorded_video.cm_sections = None
        recorded_video.status = 'Recorded'
        recorded_program = MagicMock()
        recorded_program.id = 7
        recorded_program.episode_number = '12'
        recorded_program.bangumi_subject_id = None
        recorded_program.bangumi_episode_id = None
        recorded_program.recorded_video = recorded_video

        class Prefetch:
            async def prefetch_related(self, *_args: object) -> MagicMock:
                return recorded_program

        update_collection = AsyncMock()
        with (
            patch(
                'app.routers.KonomiTVBS4KBangumiRouter.RecordedProgram.get_or_none',
                return_value=Prefetch(),
            ),
            patch(
                'app.routers.KonomiTVBS4KBangumiRouter.KonomiTVBS4KBangumiSharedStore.getCredentials',
                return_value={
                    'access_token': 'shared-token',
                    'bangumi_user_id': 1,
                },
            ),
            patch(
                'app.routers.KonomiTVBS4KBangumiRouter.UpdateBangumiEpisodeCollection',
                update_collection,
            ),
        ):
            response = await BangumiPlaybackProgressAPI(
                video_id=7,
                progress_request=schemas.BangumiPlaybackProgressRequest(
                    playback_position=1620.0,
                    duration=1800.0,
                ),
                current_user=current_user,  # type: ignore[arg-type]
            )

        self.assertEqual(response.status, 'Pending')
        update_collection.assert_not_awaited()

    async def test_progress_is_idempotent_after_completion_record(self) -> None:
        """Completion 済みなら Bangumi API を再送せず AlreadyCompleted を返す。"""

        current_user = FakeUser()
        current_user.id = 1
        recorded_video = MagicMock()
        recorded_video.duration = 1800.0
        recorded_video.cm_sections = None
        recorded_video.status = 'Recorded'
        recorded_program = MagicMock()
        recorded_program.id = 7
        recorded_program.episode_number = '12'
        recorded_program.bangumi_subject_id = 10
        recorded_program.bangumi_episode_id = 20
        recorded_program.recorded_video = recorded_video

        class Prefetch:
            async def prefetch_related(self, *_args: object) -> MagicMock:
                return recorded_program

        exists_query = MagicMock()
        exists_query.exists = AsyncMock(return_value=True)
        update_collection = AsyncMock()
        with (
            patch(
                'app.routers.KonomiTVBS4KBangumiRouter.RecordedProgram.get_or_none',
                return_value=Prefetch(),
            ),
            patch(
                'app.routers.KonomiTVBS4KBangumiRouter.KonomiTVBS4KBangumiEpisodeCompletion.filter',
                return_value=exists_query,
            ),
            patch(
                'app.routers.KonomiTVBS4KBangumiRouter.KonomiTVBS4KBangumiSharedStore.getCredentials',
                return_value={
                    'access_token': 'shared-token',
                    'bangumi_user_id': 1,
                },
            ),
            patch(
                'app.routers.KonomiTVBS4KBangumiRouter.UpdateBangumiEpisodeCollection',
                update_collection,
            ),
        ):
            response = await BangumiPlaybackProgressAPI(
                video_id=7,
                progress_request=schemas.BangumiPlaybackProgressRequest(
                    playback_position=1620.0,
                    duration=1800.0,
                ),
                current_user=current_user,  # type: ignore[arg-type]
            )

        self.assertEqual(response.status, 'AlreadyCompleted')
        update_collection.assert_not_awaited()
