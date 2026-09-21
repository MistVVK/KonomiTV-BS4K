"""製品用 OpenCode CLI の隔離 runtime を管理する。"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from app.constants import (
    LIBRARY_PATH,
    OPENCODE_BUNDLED_CONFIG_PATH,
    OPENCODE_HOME_ROOT,
    OPENCODE_PINNED_VERSION,
    OPENCODE_REPO_CONFIG_PATH,
    OPENCODE_WORKSPACE_DIR,
    OPENCODE_XDG_CACHE_HOME,
    OPENCODE_XDG_CONFIG_HOME,
    OPENCODE_XDG_DATA_HOME,
    OPENCODE_XDG_STATE_HOME,
)
from app.metadata.ai.opencode_types import (
    KonomiTVBS4KOpenCodeProviderConfig,
    KonomiTVBS4KOpenCodeProviderModel,
    KonomiTVBS4KOpenCodeProviderOptions,
)


def ResolveOpenCodeExecutable() -> Path | None:
    """OpenCode 実行ファイルのパスを解決する。

    Returns:
        実行可能な Path。見つからなければ None。
    """

    candidates = [
        Path(LIBRARY_PATH['OpenCode']),
        Path(shutil.which('opencode') or ''),
    ]
    for candidate in candidates:
        if candidate == Path(''):
            continue
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def ReadOpenCodeRegularFileText(path: Path) -> str | None:
    """symlink を辿らず、通常ファイルだけを読む。

    Args:
        path: 読み取り対象パス。

    Returns:
        ファイル本文。不在なら None。

    Raises:
        OSError: symlink または通常ファイル以外の場合。
    """

    # 認証・設定ファイルを別 inode へ誘導されないよう、lstat と O_NOFOLLOW の両方で守る。
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(path_stat.st_mode):
        raise OSError(f'OpenCode runtime file must not be a symlink: {path}')
    if stat.S_ISREG(path_stat.st_mode) is False:
        raise OSError(f'OpenCode runtime path must be a regular file: {path}')

    file_descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        file_stat = os.fstat(file_descriptor)
        if stat.S_ISREG(file_stat.st_mode) is False:
            raise OSError(f'OpenCode runtime path must be a regular file: {path}')
        return os.read(file_descriptor, file_stat.st_size + 1).decode('utf-8')
    finally:
        os.close(file_descriptor)


def WriteOpenCodeFileAtomically(path: Path, content: str) -> None:
    """製品用 OpenCode ファイルを 0600 で原子的に書く。

    Args:
        path: 最終ファイルパス。
        content: 書き込む UTF-8 本文。

    Returns:
        None

    Raises:
        OSError: symlink 拒否、書き込み失敗、権限固定失敗。
    """

    # 最終パスが symlink の場合は置換せず、認証情報を製品領域外へ書かない。
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        path_stat = None
    if path_stat is not None and stat.S_ISLNK(path_stat.st_mode):
        raise OSError(f'OpenCode runtime file must not be a symlink: {path}')
    if path_stat is not None and stat.S_ISREG(path_stat.st_mode) is False:
        raise OSError(f'OpenCode runtime path must be a regular file: {path}')

    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    temporary_fd, temporary_path = tempfile.mkstemp(
        prefix=f'.{path.name}.',
        suffix='.tmp',
        dir=directory,
    )
    try:
        with os.fdopen(temporary_fd, 'w', encoding='utf-8') as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fchmod(temporary_file.fileno(), 0o600)
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(
            directory,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY,
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise

    final_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fchmod(final_fd, 0o600)
    finally:
        os.close(final_fd)


def _BuildKonomiTVBS4KOpenCodeRuntimeConfig(template_text: str) -> str:
    """正本テンプレートへ登録済みカスタム provider を合成する。

    Args:
        template_text: docker/opencode/opencode.json の本文。

    Returns:
        カスタム provider を合成した製品用 OpenCode 設定 JSON。

    Raises:
        OSError: テンプレートまたは AI service 設定が不正な場合。
    """

    from app.metadata.ai.AIBackendSettings import AIBackendSettingsStore

    try:
        config = json.loads(template_text)
        services = AIBackendSettingsStore.listServices()
    except (json.JSONDecodeError, TypeError, ValueError, OSError) as error:
        raise OSError('Failed to build OpenCode product config.') from error
    if isinstance(config, dict) is False:
        raise OSError('OpenCode product config template must be a JSON object.')

    custom_services = [
        service for service in services
        if service.opencode_provider_type != 'Catalog'
    ]
    if not custom_services:
        return template_text

    raw_providers = config.get('provider')
    providers: dict[str, object] = dict(raw_providers) if isinstance(raw_providers, dict) else {}
    for service in custom_services:
        assert service.api_base_url is not None
        npm_package: Literal['@ai-sdk/openai-compatible', '@ai-sdk/anthropic']
        if service.opencode_provider_type == 'OpenAICompatible':
            npm_package = '@ai-sdk/openai-compatible'
        else:
            npm_package = '@ai-sdk/anthropic'
        model = KonomiTVBS4KOpenCodeProviderModel(name=service.opencode_model_id)
        provider = KonomiTVBS4KOpenCodeProviderConfig(
            npm=npm_package,
            name=service.service_name,
            options=KonomiTVBS4KOpenCodeProviderOptions(baseURL=service.api_base_url),
            models={service.opencode_model_id: model},
        )
        providers[service.opencode_provider_id] = provider

    config['provider'] = providers
    return json.dumps(config, ensure_ascii=False, indent=2) + '\n'


def SyncKonomiTVBS4KOpenCodeRuntimeConfig() -> bool:
    """正本テンプレートと AI service から runtime config を再生成する。

    Returns:
        ファイル内容を更新した場合は True、既に同一なら False。

    Raises:
        OSError: テンプレート読込・設定生成・書込に失敗した場合。
    """

    config_dir = OPENCODE_XDG_CONFIG_HOME / 'opencode'
    config_path = config_dir / 'opencode.json'
    # bind source tree がある開発環境では変更中の repo template を優先する。
    source = OPENCODE_REPO_CONFIG_PATH
    if source.is_file() is False:
        source = OPENCODE_BUNDLED_CONFIG_PATH
    if source.is_file() is False:
        raise FileNotFoundError(f'OpenCode product config template not found: {source}')

    runtime_text = _BuildKonomiTVBS4KOpenCodeRuntimeConfig(source.read_text(encoding='utf-8'))
    current_text = ReadOpenCodeRegularFileText(config_path)
    if current_text != runtime_text:
        WriteOpenCodeFileAtomically(config_path, runtime_text)
        return True

    config_fd = os.open(config_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fchmod(config_fd, 0o600)
    finally:
        os.close(config_fd)
    return False


def EnsureOpenCodeRuntimeDirectories() -> None:
    """隔離した XDG home / workspace を作成し、製品 config を同期する。

    Returns:
        None

    Raises:
        OSError: ディレクトリ作成や config 書き込みに失敗した場合。
    """

    for directory in (
        OPENCODE_HOME_ROOT,
        OPENCODE_HOME_ROOT / 'home',
        OPENCODE_XDG_CONFIG_HOME,
        OPENCODE_XDG_DATA_HOME,
        OPENCODE_XDG_CACHE_HOME,
        OPENCODE_XDG_STATE_HOME,
        OPENCODE_WORKSPACE_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    SyncKonomiTVBS4KOpenCodeRuntimeConfig()


def BuildOpenCodeProcessEnvironment() -> dict[str, str]:
    """製品用 CLI だけが使う隔離環境変数を返す。

    Returns:
        subprocess へ渡す環境変数。
    """

    environment = os.environ.copy()
    environment['HOME'] = str(OPENCODE_HOME_ROOT / 'home')
    environment['XDG_CONFIG_HOME'] = str(OPENCODE_XDG_CONFIG_HOME)
    environment['XDG_DATA_HOME'] = str(OPENCODE_XDG_DATA_HOME)
    environment['XDG_CACHE_HOME'] = str(OPENCODE_XDG_CACHE_HOME)
    environment['XDG_STATE_HOME'] = str(OPENCODE_XDG_STATE_HOME)

    # 監査用やユーザー shell の OpenCode 設定を製品 CLI へ混在させない。
    allowed_opencode_env_keys = {'OPENCODE_ENABLE_EXA'}
    for key in list(environment):
        if key.startswith('OPENCODE_') and key not in allowed_opencode_env_keys:
            environment.pop(key, None)
    environment['OPENCODE_DISABLE_AUTOUPDATE'] = 'true'
    environment['OPENCODE_DISABLE_CLAUDE_CODE'] = 'true'
    environment['OPENCODE_DISABLE_LSP_DOWNLOAD'] = 'true'
    return environment


def IsOpenCodeCLIAvailable() -> bool:
    """製品用 OpenCode CLI を起動可能か返す。

    Returns:
        実行ファイルと runtime config を準備できる場合は True。
    """

    if ResolveOpenCodeExecutable() is None:
        return False
    try:
        EnsureOpenCodeRuntimeDirectories()
    except OSError:
        return False
    return True


def ProbeOpenCodeCLIAvailability() -> dict[str, object]:
    """管理 API 向けに listener-free CLI の availability を返す。

    Returns:
        availability、version、transport、workspace を含む辞書。
    """

    executable = ResolveOpenCodeExecutable()
    version: str | None = None
    available = False
    if executable is not None:
        try:
            EnsureOpenCodeRuntimeDirectories()
            result = subprocess.run(
                [str(executable), '--version'],
                cwd=OPENCODE_WORKSPACE_DIR,
                env=BuildOpenCodeProcessEnvironment(),
                capture_output=True,
                check=False,
                timeout=5.0,
            )
            if result.returncode == 0:
                version_text = result.stdout.decode('utf-8', errors='replace').strip()
                if version_text != '':
                    version = version_text.splitlines()[0].strip()
                    available = True
        except (OSError, subprocess.SubprocessError):
            available = False
    return {
        'available': available,
        'transport': 'CLI',
        'version': version,
        'pinned_version': OPENCODE_PINNED_VERSION,
        'workspace': str(OPENCODE_WORKSPACE_DIR),
    }
