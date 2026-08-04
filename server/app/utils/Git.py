import asyncio
import os
from datetime import datetime
from pathlib import Path

from app.constants import JST


# 実行中ソースツリーの Git コミット情報
## 録画解析とバージョン API の双方から参照されるため、プロセス内で一度だけ取得する
git_commit: str | None = None
git_commit_lock = asyncio.Lock()


async def _run_git(source_tree: Path, *args: str) -> str | None:
    """source_tree 上で git サブコマンドを実行し、成功時のみ stdout を返す。

    Args:
        source_tree (Path): git リポジトリのルート
        *args (str): git に渡すサブコマンドと引数

    Returns:
        str | None: 成功時は stdout の trim 済み文字列。失敗時は None。
    """

    try:
        process = await asyncio.create_subprocess_exec(
            'git', '-c', f'safe.directory={source_tree!s}', '-C', str(source_tree),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        if process.returncode == 0 and stdout.strip():
            return stdout.decode().strip()
    except (FileNotFoundError, OSError):
        pass
    return None


def _format_commit_date(iso_timestamp: str) -> str | None:
    """git の %cI 形式など ISO 8601 日時を JST の表示用文字列へ変換する。

    Args:
        iso_timestamp (str): ISO 8601 形式のコミット日時

    Returns:
        str | None: ``YYYY-MM-DD HH:MM:SS`` 形式。解析できない場合は None。
    """

    try:
        commit_at = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return None

    if commit_at.tzinfo is None:
        commit_at = commit_at.replace(tzinfo=JST)
    else:
        commit_at = commit_at.astimezone(JST)
    return commit_at.strftime('%Y-%m-%d %H:%M:%S')


def _with_commit_date(commit: str, commit_date: str | None) -> str:
    """コミット識別子に表示用日時があれば括弧付きで連結する。

    Args:
        commit (str): コミットハッシュ（と dirty 状態）
        commit_date (str | None): 表示用のコミット日時

    Returns:
        str: 日時がある場合は ``commit (date)``、なければ commit のみ。
    """

    if not commit_date:
        return commit
    return f'{commit} ({commit_date})'


async def GetGitCommit() -> str:
    """実行中ソースツリーのコミットハッシュと dirty 状態、コミット日時を取得する。

    Returns:
        str: 8 桁のコミットハッシュと、必要に応じて付与された dirty 状態・コミット日時。
            例: ``abc12345-dirty (2026-07-30 17:04:47)``
    """

    global git_commit

    if git_commit is not None:
        return git_commit

    async with git_commit_lock:
        # ロック待機中に別の呼び出しが取得を完了している可能性があるため、キャッシュを再確認する
        if git_commit is not None:
            return git_commit

        source_tree_candidates = [
            Path(os.environ.get('KONOMITV_BS4K_SOURCE_TREE', '/code/source-tree')),
            Path(__file__).resolve().parents[3],
        ]
        source_tree = next(
            (candidate for candidate in source_tree_candidates if (candidate / '.git').exists()),
            None,
        )
        if source_tree is not None:
            describe = await _run_git(
                source_tree, 'describe', '--always', '--dirty', '--abbrev=8',
            )
            if describe is not None:
                commit_date_iso = await _run_git(source_tree, 'show', '-s', '--format=%cI')
                commit_date = (
                    _format_commit_date(commit_date_iso)
                    if commit_date_iso is not None else None
                )
                git_commit = _with_commit_date(describe, commit_date)
                return git_commit

        # ソースツリーが無い（イメージ単体起動など）場合は環境変数から取る
        # KONOMITV_BS4K_GIT_COMMIT_DATE は ISO 8601 または表示用文字列を受け付ける
        commit = os.environ.get('KONOMITV_BS4K_GIT_COMMIT', 'unknown')
        env_commit_date = os.environ.get('KONOMITV_BS4K_GIT_COMMIT_DATE')
        if env_commit_date:
            commit_date = _format_commit_date(env_commit_date) or env_commit_date
        else:
            commit_date = None
        git_commit = _with_commit_date(commit, commit_date)
        return git_commit
