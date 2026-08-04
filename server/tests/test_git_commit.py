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


def test_get_git_commit_includes_commit_date_from_source_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / '.git').mkdir()
    monkeypatch.setenv('KONOMITV_BS4K_SOURCE_TREE', str(tmp_path))

    async def fake_run_git(source_tree: Path, *args: str) -> str | None:
        assert source_tree == tmp_path
        if args[:1] == ('describe',):
            return 'abc12345-dirty'
        if args == ('show', '-s', '--format=%cI'):
            return '2026-07-30T17:04:47+09:00'
        raise AssertionError(f'unexpected git args: {args}')

    monkeypatch.setattr(git_module, '_run_git', fake_run_git)

    result = asyncio.run(git_module.GetGitCommit())
    assert result == 'abc12345-dirty (2026-07-30 17:04:47)'
    # 2 回目はキャッシュを返す
    assert asyncio.run(git_module.GetGitCommit()) == result


def test_get_git_commit_falls_back_to_env_with_optional_date(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # .git はあるが describe に失敗した場合は環境変数へフォールバックする
    (tmp_path / '.git').mkdir()
    monkeypatch.setenv('KONOMITV_BS4K_SOURCE_TREE', str(tmp_path))

    async def fake_run_git(*_: object) -> str | None:
        return None

    monkeypatch.setattr(git_module, '_run_git', fake_run_git)
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

    async def fake_run_git(*_: object) -> str | None:
        return None

    monkeypatch.setattr(git_module, '_run_git', fake_run_git)
    monkeypatch.setenv('KONOMITV_BS4K_GIT_COMMIT', 'cafebabe')
    monkeypatch.delenv('KONOMITV_BS4K_GIT_COMMIT_DATE', raising=False)

    result = asyncio.run(git_module.GetGitCommit())
    assert result == 'cafebabe'


def test_get_git_commit_keeps_hash_when_date_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / '.git').mkdir()
    monkeypatch.setenv('KONOMITV_BS4K_SOURCE_TREE', str(tmp_path))

    async def fake_run_git(source_tree: Path, *args: str) -> str | None:
        if args[:1] == ('describe',):
            return 'abc12345'
        return None

    monkeypatch.setattr(git_module, '_run_git', fake_run_git)

    result = asyncio.run(git_module.GetGitCommit())
    assert result == 'abc12345'
