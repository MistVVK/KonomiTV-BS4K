
import asyncio
import os
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter

from app import schemas
from app.config import Config
from app.constants import HTTPX_CLIENT, VERSION
from app.utils import GetPlatformEnvironment


# ルーター
router = APIRouter(
    tags = ['Version'],
    prefix = '/api/version',
)


# GitHub API から取得した KonomiTV の最新バージョン (と最終更新日時)
latest_version: str | None = None
latest_version_updated_at: float = 0

# 実行中ソースツリーの Git コミット情報
## プロセス内で一度だけ取得し、複数クライアントからバージョン API が呼ばれても Git コマンドを繰り返さない
git_commit: str | None = None
git_commit_lock = asyncio.Lock()


async def GetGitCommit() -> str:
    """実行中ソースツリーのコミットハッシュと dirty 状態を取得する。"""

    global git_commit

    if git_commit is not None:
        return git_commit

    async with git_commit_lock:
        # ロック待機中に別のリクエストが取得を完了している可能性があるため、キャッシュを再確認する
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


@router.get(
    '',
    summary = 'バージョン情報取得 API',
    response_description = 'KonomiTV サーバーのバージョンなどの情報。',
    response_model = schemas.VersionInformation,
)
async def VersionInformationAPI():
    """
    KonomiTV サーバーのバージョン情報と、バックエンドの種類、稼働環境などを取得する。
    """

    global latest_version, latest_version_updated_at

    # GitHub API で KonomiTV の最新のタグ (=最新バージョン) を取得
    ## GitHub API は無認証だと60回/1時間までしかリクエストできないので、リクエスト結果を10分ほどキャッシュする
    if latest_version is None or (time.time() - latest_version_updated_at) > 60 * 10:
        try:
            async with HTTPX_CLIENT() as client:
                response = await client.get('https://api.github.com/repos/tsukumijima/KonomiTV/tags')
            if response.status_code == 200:
                latest_version = response.json()[0]['name'].replace('v', '')  # 先頭の v を取り除く
                latest_version_updated_at = time.time()
        except (httpx.NetworkError, httpx.TimeoutException):
            pass

    # サーバーが稼働している環境を取得
    environment = GetPlatformEnvironment()

    result: dict[str, Any] = {
        'version': VERSION,
        'git_commit': await GetGitCommit(),
        'latest_version': latest_version,
        'environment': environment,
        'backend': Config().general.backend,
        'encoder': Config().general.encoder,
    }
    return result
