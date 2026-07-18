import asyncio
import os
from pathlib import Path


# 実行中ソースツリーの Git コミット情報
## 録画解析とバージョン API の双方から参照されるため、プロセス内で一度だけ取得する
git_commit: str | None = None
git_commit_lock = asyncio.Lock()


async def GetGitCommit() -> str:
    """実行中ソースツリーのコミットハッシュと dirty 状態を取得する。

    Returns:
        str: 8 桁のコミットハッシュと、必要に応じて付与された dirty 状態。
    """

    global git_commit

    if git_commit is not None:
        return git_commit

    async with git_commit_lock:
        # ロック待機中に別の呼び出しが取得を完了している可能性があるため、キャッシュを再確認する
        if git_commit is not None:
            return git_commit

        source_tree_candidates = [
            Path(os.environ.get('KONOMITV_SOURCE_TREE', '/code/source-tree')),
            Path(__file__).resolve().parents[3],
        ]
        source_tree = next(
            (candidate for candidate in source_tree_candidates if (candidate / '.git').exists()),
            None,
        )
        if source_tree is not None:
            try:
                process = await asyncio.create_subprocess_exec(
                    'git', '-c', f'safe.directory={source_tree!s}', '-C', str(source_tree),
                    'describe', '--always', '--dirty', '--abbrev=8',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await process.communicate()
                if process.returncode == 0 and stdout.strip():
                    git_commit = stdout.decode().strip()
                    return git_commit
            except (FileNotFoundError, OSError):
                pass

        git_commit = os.environ.get('KONOMITV_GIT_COMMIT', 'unknown')
        return git_commit
