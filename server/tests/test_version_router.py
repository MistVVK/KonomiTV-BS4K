import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import app.routers.VersionRouter as version_router_module
from app.config import ServerSettings
from app.constants import BS4K_VERSION, VERSION


class TagsClient:
    """GitHub Tags API の成功レスポンスだけを返すテスト用クライアント。"""

    def __init__(self, tags: list[dict[str, str]]) -> None:
        """
        Args:
            tags (list[dict[str, str]]): GitHub Tags API から返すタグ一覧

        Returns:
            None
        """

        # get() が成功レスポンスとして返すタグ一覧を保持する
        self.tags = tags
        # キャッシュ検証で実際のリクエスト回数と URL を確認するために保持する
        self.requested_urls: list[str] = []

    async def __aenter__(self) -> 'TagsClient':
        """
        Returns:
            TagsClient: HTTPX_CLIENT の async context manager が返すクライアント
        """

        return self

    async def __aexit__(self, *_: object) -> None:
        """
        Args:
            *_ (object): async context manager から渡される終了情報

        Returns:
            None
        """

        return None

    async def get(self, url: str) -> SimpleNamespace:
        """
        Args:
            url (str): リクエスト先 URL

        Returns:
            SimpleNamespace: status_code と json() を持つ成功レスポンス
        """

        self.requested_urls.append(url)
        return SimpleNamespace(status_code=200, json=lambda: self.tags)


@pytest.mark.parametrize(
    ('tags', 'expected_latest_version'),
    [
        ([{'name': 'v1.2.3'}], '1.2.3'),
        ([], None),
    ],
)
def test_version_information_separates_bs4k_and_upstream_versions_and_caches_tags(
    monkeypatch: pytest.MonkeyPatch,
    tags: list[dict[str, str]],
    expected_latest_version: str | None,
) -> None:
    settings = ServerSettings()
    settings.general.backend = 'EDCB'
    settings.general.encoder = 'FFmpeg'
    settings.general.encoder_bs4k = 'NVENC'
    settings.general.bs4k_ignore_viewer_low_latency = False
    settings.general.jikkyo_enabled = False
    tags_client = TagsClient(tags)
    http_client_factory = Mock(return_value=tags_client)
    monkeypatch.setattr(version_router_module, 'Config', lambda: settings)
    monkeypatch.setattr(version_router_module, 'GetPlatformEnvironment', lambda: 'Linux')
    monkeypatch.setattr(version_router_module, 'GetGitCommit', AsyncMock(return_value='test-commit'))
    monkeypatch.setattr(version_router_module, 'HTTPX_CLIENT', http_client_factory)
    monkeypatch.setattr(version_router_module, 'latest_version', None)
    monkeypatch.setattr(version_router_module, 'latest_version_updated_at', 0)

    first_response = asyncio.run(version_router_module.VersionInformationAPI())
    second_response = asyncio.run(version_router_module.VersionInformationAPI())

    assert first_response['version'] == BS4K_VERSION
    assert first_response['upstream_version'] == VERSION
    assert first_response['git_commit'] == 'test-commit'
    assert first_response['latest_version'] == expected_latest_version
    assert first_response['encoder_bs4k'] == 'NVENC'
    assert first_response['bs4k_ignore_viewer_low_latency'] is False
    assert second_response['latest_version'] == expected_latest_version
    assert tags_client.requested_urls == ['https://api.github.com/repos/MistVVK/KonomiTV-BS4K/tags']
    http_client_factory.assert_called_once_with()
