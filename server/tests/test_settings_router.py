import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.config import ClientSettings
from app.routers.SettingsRouter import ClientSettingsUpdateAPI


class _FakeUserQuery:
    """User.filter(...).select_for_update().get() のチェーンを模す。"""

    def __init__(self, user: SimpleNamespace) -> None:
        """
        返すユーザー行を保持する。

        Args:
            user (SimpleNamespace): select_for_update 後に返す User 代替。

        Returns:
            None
        """

        self._user = user

    def select_for_update(self) -> '_FakeUserQuery':
        """
        行ロック付きクエリを装い、自身を返す。

        Args:
            None

        Returns:
            _FakeUserQuery: 同一クエリ代替。
        """

        return self

    async def get(self) -> SimpleNamespace:
        """
        最新 row として保持中のユーザーを返す。

        Args:
            None

        Returns:
            SimpleNamespace: ユーザー行代替。
        """

        return self._user


def test_client_settings_update_rejects_outdated_snapshot_with_409() -> None:
    """
    サーバー上より古い last_synced_at の PUT は 409 となり、DB 値を上書きしない。

    Args:
        None

    Returns:
        None
    """

    async def Run() -> None:
        """
        古い snapshot を送った場合の拒否と非保存を検証する。

        Args:
            None

        Returns:
            None
        """

        saved_payloads: list[dict[str, Any]] = []
        server_settings = {
            'last_synced_at': 200.0,
            'comment_font_size': 40,
            'ui_theme': 'KonomiClassic',
        }
        user = SimpleNamespace(
            id=1,
            client_settings=dict(server_settings),
            save=AsyncMock(side_effect=lambda: saved_payloads.append(dict(user.client_settings))),
        )
        # dependency 経由の古い snapshot（並行 PUT で先に読んだ状態を再現）
        dependency_user = SimpleNamespace(id=1, client_settings={'last_synced_at': 100.0})

        outdated = ClientSettings.model_validate({
            'last_synced_at': 100.0,
            'comment_font_size': 10,
            'ui_theme': 'KonomiClassic',
        })

        @asynccontextmanager
        async def FakeTransaction():
            """
            Tortoise の in_transaction を no-op コンテキストに差し替える。

            Yields:
                None
            """

            yield None

        with (
            patch('app.routers.SettingsRouter.in_transaction', FakeTransaction),
            patch('app.routers.SettingsRouter.User.filter', return_value=_FakeUserQuery(user)),
        ):
            with pytest.raises(HTTPException) as ex_info:
                await ClientSettingsUpdateAPI(outdated, dependency_user)  # type: ignore[arg-type]

        assert ex_info.value.status_code == 409
        assert 'outdated' in str(ex_info.value.detail).lower()
        assert user.save.await_count == 0
        assert saved_payloads == []
        # DB 上の新しい値は巻き戻っていない
        assert user.client_settings['last_synced_at'] == 200.0
        assert user.client_settings['comment_font_size'] == 40

    asyncio.run(Run())


def test_client_settings_update_accepts_newer_snapshot() -> None:
    """
    サーバー上以上の last_synced_at を持つ PUT は保存される。

    Args:
        None

    Returns:
        None
    """

    async def Run() -> None:
        """
        新しい snapshot が CAS を通過して save されることを検証する。

        Args:
            None

        Returns:
            None
        """

        user = SimpleNamespace(
            id=1,
            client_settings={
                'last_synced_at': 100.0,
                'comment_font_size': 34,
                'ui_theme': 'KonomiClassic',
            },
            save=AsyncMock(),
        )
        newer = ClientSettings.model_validate({
            'last_synced_at': 150.0,
            'comment_font_size': 48,
            'ui_theme': 'KonomiClassic',
        })

        @asynccontextmanager
        async def FakeTransaction():
            """
            Tortoise の in_transaction を no-op コンテキストに差し替える。

            Yields:
                None
            """

            yield None

        with (
            patch('app.routers.SettingsRouter.in_transaction', FakeTransaction),
            patch('app.routers.SettingsRouter.User.filter', return_value=_FakeUserQuery(user)),
        ):
            await ClientSettingsUpdateAPI(newer, SimpleNamespace(id=1))  # type: ignore[arg-type]

        assert user.save.await_count == 1
        assert user.client_settings['last_synced_at'] == 150.0
        assert user.client_settings['comment_font_size'] == 48

    asyncio.run(Run())


def test_client_settings_get_self_heals_corrupted_nan() -> None:
    """既存汚染 (NaN) があっても GET が default へ自己修復すること。"""

    from app.routers.SettingsRouter import ClientSettingsAPI

    async def Run() -> None:
        user = SimpleNamespace(
            id=7,
            client_settings={
                'last_synced_at': float('nan'),
                'comment_speed_rate': float('inf'),
                'comment_font_size': 34,
            },
            save=AsyncMock(),
        )
        repaired = await ClientSettingsAPI(user)  # type: ignore[arg-type]
        assert repaired.last_synced_at == 0.0
        assert repaired.comment_speed_rate == 1.0
        assert user.save.await_count == 1
        assert user.client_settings['last_synced_at'] == 0.0

    asyncio.run(Run())


def test_client_settings_rejects_nested_nan_inf() -> None:
    """mylist / watched_history など任意 dict 内の NaN/Inf も 422 相当で拒否すること。"""

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ClientSettings.model_validate({
            'mylist': [{'id': 'x', 'score': float('nan')}],
        })
    with pytest.raises(ValidationError):
        ClientSettings.model_validate({
            'watched_history': [{'progress': float('inf')}],
        })
    # JSON 文字列経由でも nested NaN を拒否する
    with pytest.raises(ValidationError):
        ClientSettings.model_validate_json('{"mylist":[{"score":NaN}]}')


def test_client_settings_get_self_heals_nested_nan() -> None:
    """既存の nested NaN 汚染も GET で default へ自己修復すること。"""

    from app.routers.SettingsRouter import ClientSettingsAPI

    async def Run() -> None:
        user = SimpleNamespace(
            id=8,
            client_settings={
                'mylist': [{'id': 'a', 'score': float('nan')}],
                'comment_font_size': 34,
            },
            save=AsyncMock(),
        )
        repaired = await ClientSettingsAPI(user)  # type: ignore[arg-type]
        assert repaired.mylist == []
        assert user.save.await_count == 1

    asyncio.run(Run())
