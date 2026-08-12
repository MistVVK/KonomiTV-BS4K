
import asyncio
from pathlib import Path

import pytest

import app.utils.Git as git_module


@pytest.fixture(autouse=True)
def reset_git_commit_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """各テストでプロセス内キャッシュを空にし、取得処理を毎回走らせる。"""

    monkeypatch.setattr(git_module, 'git_commit', None)


def test_format_commit_date_converts_iso_to_jst_display() -> None:
    assert git_module._format_commit_date('2026-07-30T08:04:47+00:00') == '2026-07-30 17:04:47'
    assert git_module._format_commit_date('2026-07-30T17:04:47+09:00') == '2026-07-30 17:04:47'
    assert git_module._format_commit_date('not-a-date') is None


def test_with_commit_date_appends_only_when_present() -> None:
    assert git_module._with_commit_date('abc12345', None) == 'abc12345'
    assert git_module._with_commit_date('abc12345-dirty', '2026-07-30 17:04:47') == (
        'abc12345-dirty (2026-07-30 17:04:47)'
    )


def test_with_dirty_suffix() -> None:
    assert git_module._with_dirty_suffix('bs4k-v1.1.0', False) == 'bs4k-v1.1.0'
    assert git_module._with_dirty_suffix('bs4k-v1.1.0', True) == 'bs4k-v1.1.0-dirty'
    assert git_module._with_dirty_suffix('abc12345-dirty', True) == 'abc12345-dirty'


def test_get_git_commit_uses_exact_bs4k_tag_when_head_matches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / '.git').mkdir()
    monkeypatch.setenv('KONOMITV_BS4K_SOURCE_TREE', str(tmp_path))

    async def fake_run_git(source_tree: Path, *args: str) -> str | None:
        assert source_tree == tmp_path
        if args[:1] == ('describe',) and '--exact-match' in args:
            return 'bs4k-v1.1.0'
        if args == ('show', '-s', '--format=%cI'):
            return '2026-07-30T17:04:47+09:00'
        raise AssertionError(f'unexpected git args: {args}')

    async def fake_is_dirty(source_tree: Path) -> bool:
        assert source_tree == tmp_path
        return False

    monkeypatch.setattr(git_module, '_run_git', fake_run_git)
    monkeypatch.setattr(git_module, '_is_worktree_dirty', fake_is_dirty)

    result = asyncio.run(git_module.GetGitCommit())
    assert result == 'bs4k-v1.1.0 (2026-07-30 17:04:47)'
    assert asyncio.run(git_module.GetGitCommit()) == result


def test_get_git_commit_uses_short_hash_when_not_exact_tag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / '.git').mkdir()
    monkeypatch.setenv('KONOMITV_BS4K_SOURCE_TREE', str(tmp_path))

    async def fake_run_git(source_tree: Path, *args: str) -> str | None:
        assert source_tree == tmp_path
        if args[:1] == ('describe',) and '--exact-match' in args:
            # タグ近傍だが完全一致ではない
            return None
        if args[:1] == ('rev-parse',):
            return 'b3d94ce3'
        if args == ('show', '-s', '--format=%cI'):
            return '2026-07-30T17:04:47+09:00'
        raise AssertionError(f'unexpected git args: {args}')

    async def fake_is_dirty(source_tree: Path) -> bool:
        assert source_tree == tmp_path
        return True

    monkeypatch.setattr(git_module, '_run_git', fake_run_git)
    monkeypatch.setattr(git_module, '_is_worktree_dirty', fake_is_dirty)

    result = asyncio.run(git_module.GetGitCommit())
    assert result == 'b3d94ce3-dirty (2026-07-30 17:04:47)'


def test_get_git_commit_falls_back_to_env_with_optional_date(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # .git はあるがラベル解決に失敗した場合は環境変数へフォールバックする
    (tmp_path / '.git').mkdir()
    monkeypatch.setenv('KONOMITV_BS4K_SOURCE_TREE', str(tmp_path))

    async def fake_resolve_label(_: Path) -> str | None:
        return None

    monkeypatch.setattr(git_module, '_resolve_commit_label', fake_resolve_label)
    monkeypatch.setenv('KONOMITV_BS4K_GIT_COMMIT', 'deadbeef')
    monkeypatch.setenv('KONOMITV_BS4K_GIT_COMMIT_DATE', '2026-07-29T12:00:00+09:00')

    result = asyncio.run(git_module.GetGitCommit())
    assert result == 'deadbeef (2026-07-29 12:00:00)'


def test_get_git_commit_env_without_date(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / '.git').mkdir()
    monkeypatch.setenv('KONOMITV_BS4K_SOURCE_TREE', str(tmp_path))

    async def fake_resolve_label(_: Path) -> str | None:
        return None

    monkeypatch.setattr(git_module, '_resolve_commit_label', fake_resolve_label)
    monkeypatch.setenv('KONOMITV_BS4K_GIT_COMMIT', 'cafebabe')
    monkeypatch.delenv('KONOMITV_BS4K_GIT_COMMIT_DATE', raising=False)

    result = asyncio.run(git_module.GetGitCommit())
    assert result == 'cafebabe'


def test_get_git_commit_keeps_label_when_date_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / '.git').mkdir()
    monkeypatch.setenv('KONOMITV_BS4K_SOURCE_TREE', str(tmp_path))

    async def fake_resolve_label(_: Path) -> str:
        return 'abc12345'

    async def fake_run_git(*_: object) -> str | None:
        return None

    monkeypatch.setattr(git_module, '_resolve_commit_label', fake_resolve_label)
    monkeypatch.setattr(git_module, '_run_git', fake_run_git)

    result = asyncio.run(git_module.GetGitCommit())
    assert result == 'abc12345'
