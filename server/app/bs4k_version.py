
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


# KonomiTV-BS4K のリリースタグ接頭辞（例: bs4k-v1.1.1）
BS4K_TAG_PREFIX = 'bs4k-v'

# git describe の --match に渡すタグ glob
BS4K_TAG_MATCH = f'{BS4K_TAG_PREFIX}*'

# イメージ単体起動など .git が無いときの上書き用環境変数
BS4K_VERSION_ENV = 'KONOMITV_BS4K_VERSION'

# ソースツリー探索に使う環境変数（GetGitCommit と同じ）
BS4K_SOURCE_TREE_ENV = 'KONOMITV_BS4K_SOURCE_TREE'

# タグも環境変数も無いときの最終フォールバック
BS4K_VERSION_FALLBACK = '0.0.0-dev'

# git describe の出力から semver と distance / dirty を取り出す
# 例: bs4k-v1.1.1 / bs4k-v1.1.1-dirty / bs4k-v1.1.1-5-gb3d94ce3-dirty
_DESCRIBE_PATTERN = re.compile(
    rf'^(?:{re.escape(BS4K_TAG_PREFIX)})?'
    r'(?P<version>\d+\.\d+\.\d+)'
    r'(?:-(?P<distance>\d+)-g(?P<commit>[0-9a-f]+))?'
    r'(?P<dirty>-dirty)?$',
    re.IGNORECASE,
)

# プロセス内キャッシュ（起動後にタグを付け替えても同じプロセスでは変えない）
_cached_bs4k_version: str | None = None


def parseBS4KDescribe(describe: str) -> str | None:
    """git describe の出力を KonomiTV-BS4K の version 文字列へ正規化する。

    Args:
        describe (str): `git describe --tags --match 'bs4k-v*' --dirty --abbrev=8` の出力。

    Returns:
        str | None: 正規化済みバージョン。解釈できない場合は None。
            - タグちょうど（clean）: `1.1.1`
            - タグより先 / dirty: `1.1.1-dev`
            - マッチタグ無しのハッシュのみ: `0.0.0-dev`
    """

    text = describe.strip()
    if text == '':
        return None

    match = _DESCRIBE_PATTERN.fullmatch(text)
    if match is None:
        # --always によりタグが無く短縮ハッシュだけ返った場合
        if re.fullmatch(r'[0-9a-f]+(?:-dirty)?', text, flags=re.IGNORECASE) is not None:
            return BS4K_VERSION_FALLBACK
        return None

    version = match.group('version')
    distance_text = match.group('distance')
    dirty = match.group('dirty') is not None
    distance = int(distance_text) if distance_text is not None else 0

    # タグ位置ぴったりかつ clean なときだけリリース版番号をそのまま使う
    if distance == 0 and dirty is False:
        return version

    # タグ無しコミット・作業ツリー dirty は開発版として -dev を付ける
    # 詳細なコミットは version API の git_commit 側で別表示する
    return f'{version}-dev'


def pickLatestBS4KVersionFromGitHubTagNames(tag_names: list[str]) -> str | None:
    """GitHub Tags API のタグ名一覧から BS4K 最新リリース版を選ぶ。

    Args:
        tag_names (list[str]): タグ名（`bs4k-v1.1.1` や upstream の `v0.14.1` が混在しうる）。

    Returns:
        str | None: 接頭辞を除いた最新 semver（例: `1.1.1`）。BS4K タグが無ければ None。
    """

    versions: list[tuple[tuple[int, int, int], str]] = []
    for name in tag_names:
        if name.startswith(BS4K_TAG_PREFIX) is False:
            continue
        version_text = name.removeprefix(BS4K_TAG_PREFIX)
        # リリース比較は x.y.z の厳密 3 セグメントだけを対象にする
        parts = version_text.split('.')
        if len(parts) != 3:
            continue
        try:
            key = (int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            continue
        versions.append((key, version_text))

    if len(versions) == 0:
        return None

    versions.sort(key=lambda item: item[0], reverse=True)
    return versions[0][1]


def _iterSourceTreeCandidates() -> list[Path]:
    """バージョン解決に使うソースツリー候補を優先度順で返す。

    Returns:
        list[Path]: 環境変数で指定されたパスと、このファイルから見たリポジトリルート。
    """

    candidates: list[Path] = []
    env_tree = os.environ.get(BS4K_SOURCE_TREE_ENV)
    if env_tree:
        candidates.append(Path(env_tree))
    # server/app/bs4k_version.py → リポジトリルートは parents[2]
    candidates.append(Path(__file__).resolve().parents[2])
    return candidates


def _runGitSync(source_tree: Path, *args: str) -> str | None:
    """source_tree 上で git を同期実行し、成功時のみ stdout を返す。

    Args:
        source_tree (Path): git リポジトリのルート。
        *args (str): git に渡すサブコマンドと引数。

    Returns:
        str | None: 成功時は trim 済み stdout。失敗時は None。
    """

    try:
        completed = subprocess.run(
            [
                'git',
                '-c', f'safe.directory={source_tree!s}',
                '-C', str(source_tree),
                *args,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None

    if completed.returncode != 0:
        return None
    stdout = completed.stdout.strip()
    return stdout if stdout != '' else None


def resolveBS4KVersion(*, force_refresh: bool = False) -> str:
    """KonomiTV-BS4K の version 文字列を解決する。

    優先順位:
    1. ソースツリー上の `git describe --tags --match 'bs4k-v*' --dirty --abbrev=8`
    2. 環境変数 `KONOMITV_BS4K_VERSION`（.git が無いイメージ向け）
    3. `0.0.0-dev`

    Args:
        force_refresh (bool): True のときプロセス内キャッシュを無視して再解決する。

    Returns:
        str: WebUI / version API / User-Agent などで使う BS4K バージョン。
    """

    global _cached_bs4k_version

    if force_refresh is False and _cached_bs4k_version is not None:
        return _cached_bs4k_version

    for source_tree in _iterSourceTreeCandidates():
        if (source_tree / '.git').exists() is False:
            continue
        describe = _runGitSync(
            source_tree,
            'describe',
            '--tags',
            '--match', BS4K_TAG_MATCH,
            '--dirty',
            '--always',
            '--abbrev=8',
        )
        if describe is None:
            continue
        parsed = parseBS4KDescribe(describe)
        if parsed is not None:
            _cached_bs4k_version = parsed
            return parsed

    env_version = os.environ.get(BS4K_VERSION_ENV)
    if env_version is not None and env_version.strip() != '':
        _cached_bs4k_version = env_version.strip()
        return _cached_bs4k_version

    _cached_bs4k_version = BS4K_VERSION_FALLBACK
    return _cached_bs4k_version
