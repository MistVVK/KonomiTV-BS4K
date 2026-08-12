import asyncio
import os
from datetime import datetime
from pathlib import Path

from app.bs4k_version import BS4K_TAG_MATCH
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
            stdout が空の成功（例: clean な status）も None になる点に注意。
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


async def _run_git_exit_code(source_tree: Path, *args: str) -> int | None:
    """source_tree 上で git を実行し、終了コードだけを返す。

    Args:
        source_tree (Path): git リポジトリのルート
        *args (str): git に渡すサブコマンドと引数

    Returns:
        int | None: 終了コード。git 自体が起動できない場合は None。
    """

    try:
        process = await asyncio.create_subprocess_exec(
            'git', '-c', f'safe.directory={source_tree!s}', '-C', str(source_tree),
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await process.wait()
    except (FileNotFoundError, OSError):
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
        commit (str): コミットハッシュまたは完全一致タグ（と dirty 状態）
        commit_date (str | None): 表示用のコミット日時

    Returns:
        str: 日時がある場合は ``commit (date)``、なければ commit のみ。
    """

    if not commit_date:
        return commit
    return f'{commit} ({commit_date})'


def _with_dirty_suffix(label: str, is_dirty: bool) -> str:
    """dirty な作業ツリーならラベル末尾に -dirty を付ける。

    Args:
        label (str): タグ名または短縮コミットハッシュ
        is_dirty (bool): 作業ツリーに未コミット変更があるとき True

    Returns:
        str: dirty なら ``label-dirty``、そうでなければ label そのもの
    """

    if is_dirty is False:
        return label
    if label.endswith('-dirty'):
        return label
    return f'{label}-dirty'


async def _is_worktree_dirty(source_tree: Path) -> bool:
    """作業ツリーに HEAD との差分があるかを判定する。

    Args:
        source_tree (Path): git リポジトリのルート

    Returns:
        bool: 差分があれば True。判定不能なときは False。
    """

    # インデックスを更新してから diff-index する（通常の dirty 判定と同じ）
    await _run_git_exit_code(source_tree, 'update-index', '-q', '--refresh')
    exit_code = await _run_git_exit_code(source_tree, 'diff-index', '--quiet', 'HEAD', '--')
    # diff-index は差分ありで 1、エラーで 0 以外になり得る。1 のときだけ dirty とみなす
    return exit_code == 1


async def _resolve_commit_label(source_tree: Path) -> str | None:
    """表示用のコミットラベルを解決する。

    HEAD が bs4k-v* タグと完全一致するときだけタグ名を使い、
    一致しないときは短縮コミットハッシュを使う。
    どちらの場合も作業ツリー dirty なら -dirty を付ける。

    Args:
        source_tree (Path): git リポジトリのルート

    Returns:
        str | None: 例 ``bs4k-v1.1.0`` / ``bs4k-v1.1.0-dirty`` / ``b3d94ce3`` /
            ``b3d94ce3-dirty``。取得できない場合は None。
    """

    is_dirty = await _is_worktree_dirty(source_tree)

    # タグ完全一致のみタグ名を出す（近傍タグの bs4k-v1.1.0-5-g... は使わない）
    exact_tag = await _run_git(
        source_tree,
        'describe',
        '--tags',
        '--match', BS4K_TAG_MATCH,
        '--exact-match',
        'HEAD',
    )
    if exact_tag is not None:
        return _with_dirty_suffix(exact_tag, is_dirty)

    short_hash = await _run_git(source_tree, 'rev-parse', '--short=8', 'HEAD')
    if short_hash is None:
        return None
    return _with_dirty_suffix(short_hash, is_dirty)


async def GetGitCommit() -> str:
    """実行中ソースツリーのコミット表示文字列を取得する。

    Returns:
        str: 次のいずれかと、必要に応じて dirty・コミット日時。
            - HEAD が ``bs4k-v*`` タグと完全一致: ``bs4k-v1.1.0`` / ``bs4k-v1.1.0-dirty (日時)``
            - それ以外: ``abc12345`` / ``abc12345-dirty (日時)``
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
            label = await _resolve_commit_label(source_tree)
            if label is not None:
                commit_date_iso = await _run_git(source_tree, 'show', '-s', '--format=%cI')
                commit_date = (
                    _format_commit_date(commit_date_iso)
                    if commit_date_iso is not None else None
                )
                git_commit = _with_commit_date(label, commit_date)
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
