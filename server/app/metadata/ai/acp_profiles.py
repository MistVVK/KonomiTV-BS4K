"""録画シリーズ ACP の KonomiTV-BS4K 専用プロファイル。

Codex / Grok の各 CLI には、ユーザーの対話用 home と一切共有しない専用 HOME を
割り当てる。Codex / Grok の認証は資格情報専用モジュールが明示 import した
``auth.json`` だけを使用する。
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from typing import Literal

from app.constants import DATA_DIR


KonomiTVBS4KACPProfileBackend = Literal['codex', 'grok']

_ACP_PROFILES_ROOT = DATA_DIR / 'acp-profiles' / 'recorded-series'
_ISOLATED_DIRS = ['sessions', 'logs', 'memories', 'history', 'tmp', 'workspace', 'cache']
_CODEX_MANAGED_CONFIG = 'cli_auth_credentials_store = "file"\n'


class AcpProfileError(Exception):
    """ACP プロファイル構築に関するエラー。"""


def _buildKonomiTVBS4KCodexManagedConfig(
    *,
    konomitv_bs4k_fast_mode_enabled: bool,
) -> str:
    """KonomiTV-BS4K 管理下の Codex config.toml を生成する。"""

    if konomitv_bs4k_fast_mode_enabled is False:
        return _CODEX_MANAGED_CONFIG
    return (
        f'{_CODEX_MANAGED_CONFIG}'
        'service_tier = "fast"\n'
        '\n'
        '[features]\n'
        'fast_mode = true\n'
    )


def ensure_acp_profile(
    backend: KonomiTVBS4KACPProfileBackend,
    *,
    konomitv_bs4k_fast_mode_enabled: bool = False,
) -> Path:
    """KonomiTV-BS4K 管理下に provider 専用の隔離 home を構築する。

    Args:
        backend: ACP バックエンド種別。
        konomitv_bs4k_fast_mode_enabled: Codex の Fast service tier を有効にするか。

    Returns:
        Path: provider 専用 profile のランタイム絶対パス。

    Raises:
        AcpProfileError: profile または隔離対象が symlink・不正形式の場合。
        OSError: directory / Codex 管理設定を作成できない場合。
    """

    profile_dir = _ACP_PROFILES_ROOT / backend

    # profile root 自体が symlink なら、その配下の隔離保証を成立させられない。
    if profile_dir.is_symlink():
        raise AcpProfileError('ACP profile root must not be a symlink.')
    profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if profile_dir.is_dir() is False:
        raise AcpProfileError('ACP profile root must be a directory.')
    os.chmod(profile_dir, 0o700)

    # セッション・履歴・ログ・メモリは provider ごとの実 directory に固定する。
    for isolated_dir in _ISOLATED_DIRS:
        isolated_path = profile_dir / isolated_dir
        if isolated_path.is_symlink():
            raise AcpProfileError('Isolated ACP path must not be a symlink.')
        if isolated_path.exists() and isolated_path.is_dir() is False:
            raise AcpProfileError('Isolated ACP path must be a directory.')
        isolated_path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(isolated_path, 0o700)

    if backend in {'codex', 'grok'}:
        auth_path = profile_dir / 'auth.json'
        if auth_path.is_symlink():
            # 旧 home 共有 symlink は追跡しない。管理 UI の再取り込みが os.replace() で安全に置換する。
            raise AcpProfileError('Imported ACP auth must not be a symlink.')
        if auth_path.exists():
            auth_stat = auth_path.stat()
            if (
                stat.S_ISREG(auth_stat.st_mode) is False or
                stat.S_IMODE(auth_stat.st_mode) != 0o600
            ):
                raise AcpProfileError('Imported ACP auth must be a 0600 regular file.')

    # Codex はホスト config.toml を継承せず、認証方式と Fast 設定を KonomiTV-BS4K が固定する。
    if backend == 'codex':
        _writeManagedFile(
            profile_dir / 'config.toml',
            _buildKonomiTVBS4KCodexManagedConfig(
                konomitv_bs4k_fast_mode_enabled=konomitv_bs4k_fast_mode_enabled,
            ),
        )

    return profile_dir


def get_profile_environment(
    backend: KonomiTVBS4KACPProfileBackend,
    profile_dir: Path,
) -> dict[str, str]:
    """provider 専用 profile と固定認証経路を子プロセス環境へ設定する。

    Args:
        backend: ACP バックエンド種別。
        profile_dir: ``ensure_acp_profile()`` で構築した専用 profile。

    Returns:
        dict[str, str]: 親環境へ上書きする provider 固有の固定値。
    """

    profile_path = str(profile_dir)
    environment = {
        'HOME': profile_path,
        'TMPDIR': str(profile_dir / 'tmp'),
        'XDG_CACHE_HOME': str(profile_dir / 'cache'),
    }
    if backend == 'codex':
        environment.update({
            'CODEX_HOME': profile_path,
            'NO_BROWSER': '1',
            'APP_SERVER_LOGS': str(profile_dir / 'logs'),
        })
    elif backend == 'grok':
        environment['GROK_HOME'] = profile_path
    return environment


def _writeManagedFile(destination_path: Path, content: str) -> None:
    """KonomiTV-BS4K 管理ファイルを 0600 で atomic に置き換える。

    Args:
        destination_path: provider profile 配下の固定保存先。
        content: KonomiTV-BS4K が管理する非機密設定。

    Returns:
        None

    Raises:
        OSError: 一時ファイル作成・書込み・置換に失敗した場合。
    """

    encoded_content = content.encode('utf-8')
    temporary_path = destination_path.with_name(
        f'.{destination_path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp',
    )
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        written_bytes = 0
        while written_bytes < len(encoded_content):
            written_bytes += os.write(file_descriptor, encoded_content[written_bytes:])
        os.fsync(file_descriptor)
        os.fchmod(file_descriptor, 0o600)
        os.close(file_descriptor)
        file_descriptor = None
        os.replace(temporary_path, destination_path)
        directory_descriptor = os.open(
            destination_path.parent,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY,
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
