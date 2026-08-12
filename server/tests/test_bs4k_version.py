
from pathlib import Path

import pytest

import app.bs4k_version as bs4k_version_module
from app.bs4k_version import (
    BS4K_VERSION_FALLBACK,
    parseBS4KDescribe,
    pickLatestBS4KVersionFromGitHubTagNames,
    resolveBS4KVersion,
)


@pytest.fixture(autouse=True)
def reset_bs4k_version_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """各テストでプロセス内キャッシュを空にし、解決処理を毎回走らせる。"""

    monkeypatch.setattr(bs4k_version_module, '_cached_bs4k_version', None)


@pytest.mark.parametrize(
    ('describe', 'expected'),
    [
        ('bs4k-v1.1.1', '1.1.1'),
        ('bs4k-v1.1.1-dirty', '1.1.1-dev'),
        ('bs4k-v1.1.1-5-gb3d94ce3', '1.1.1-dev'),
        ('bs4k-v1.1.1-5-gb3d94ce3-dirty', '1.1.1-dev'),
        ('bs4k-v1.1.1-0-gb3d94ce3', '1.1.1'),
        ('bs4k-v1.1.1-0-gb3d94ce3-dirty', '1.1.1-dev'),
        ('1.2.3', '1.2.3'),
        ('b3d94ce3', BS4K_VERSION_FALLBACK),
        ('b3d94ce3-dirty', BS4K_VERSION_FALLBACK),
        ('', None),
        ('not-a-version', None),
    ],
)
def test_parse_bs4k_describe(describe: str, expected: str | None) -> None:
    assert parseBS4KDescribe(describe) == expected


def test_pick_latest_bs4k_version_ignores_upstream_tags_and_picks_semver_max() -> None:
    latest = pickLatestBS4KVersionFromGitHubTagNames([
        'v0.14.1',
        'bs4k-v1.0.0',
        'bs4k-v1.1.1',
        'bs4k-v1.1.0',
        'not-a-tag',
        'bs4k-v2.0.0-beta',
    ])
    assert latest == '1.1.1'


def test_pick_latest_bs4k_version_returns_none_without_bs4k_tags() -> None:
    assert pickLatestBS4KVersionFromGitHubTagNames(['v0.14.1', 'v0.13.0']) is None


def test_resolve_bs4k_version_from_git_describe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / '.git').mkdir()
    monkeypatch.setenv('KONOMITV_BS4K_SOURCE_TREE', str(tmp_path))
    monkeypatch.delenv('KONOMITV_BS4K_VERSION', raising=False)

    def fake_run_git(source_tree: Path, *args: str) -> str | None:
        assert source_tree == tmp_path
        assert args[:1] == ('describe',)
        return 'bs4k-v1.1.1-3-gabcd1234-dirty'

    monkeypatch.setattr(bs4k_version_module, '_runGitSync', fake_run_git)

    assert resolveBS4KVersion() == '1.1.1-dev'
    # 2 回目はキャッシュ
    assert resolveBS4KVersion() == '1.1.1-dev'


def test_resolve_bs4k_version_falls_back_to_env_without_git(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # .git が無いツリーしか見えないようにする
    monkeypatch.setenv('KONOMITV_BS4K_SOURCE_TREE', str(tmp_path))
    monkeypatch.setenv('KONOMITV_BS4K_VERSION', '9.9.9')
    monkeypatch.setattr(
        bs4k_version_module,
        '_iterSourceTreeCandidates',
        lambda: [tmp_path],
    )

    assert resolveBS4KVersion() == '9.9.9'


def test_resolve_bs4k_version_final_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv('KONOMITV_BS4K_SOURCE_TREE', str(tmp_path))
    monkeypatch.delenv('KONOMITV_BS4K_VERSION', raising=False)
    monkeypatch.setattr(
        bs4k_version_module,
        '_iterSourceTreeCandidates',
        lambda: [tmp_path],
    )

    assert resolveBS4KVersion() == BS4K_VERSION_FALLBACK
