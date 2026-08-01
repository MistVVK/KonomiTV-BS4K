from __future__ import annotations

import json
import subprocess
from pathlib import Path


PYTHON_COMMAND = '/usr/bin/python3'


def _RunSandbox(
    launcher: Path,
    *,
    profile: Path,
    working_directory: Path,
    command: list[str],
    read_files: tuple[Path, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """実 Landlock launcher で command を実行する。

    Args:
        launcher: コンパイル済み launcher。
        profile: 書込みを許可する provider profile。
        working_directory: 読取りだけを許可する cwd。
        command: ``--`` 以降へ渡す絶対 command と引数。
        read_files: provider 固有で読取りだけを許可する資格情報ファイル。

    Returns:
        subprocess.CompletedProcess[str]: launcher の実行結果。
    """

    launcher_arguments = [
        str(launcher),
        '--profile',
        str(profile),
        '--working-directory',
        str(working_directory),
    ]
    for read_file in read_files:
        launcher_arguments.extend(['--read-file', str(read_file)])
    launcher_arguments.extend(['--', *command])
    return subprocess.run(
        launcher_arguments,
        cwd=working_directory,
        capture_output=True,
        check=False,
        text=True,
    )


def test_acp_sandbox_separates_provider_files_and_write_rights(
    AcpSandboxLauncher: Path,
    tmp_path: Path,
) -> None:
    """own profile 以外の秘密・書込み・実行・親 process 操作を kernel が拒否する。"""

    profile = tmp_path / 'codex'
    other_profile = tmp_path / 'grok'
    protected_data = tmp_path / 'data-secrets'
    working_directory = tmp_path / 'workspace'
    for directory in (profile, other_profile, protected_data, working_directory):
        directory.mkdir()

    own_auth = profile / 'auth.json'
    other_auth = other_profile / 'auth.json'
    api_key = protected_data / 'recorded-series-api.key'
    cwd_input = working_directory / 'input.txt'
    adc = tmp_path / 'application_default_credentials.json'
    profile_symlink = profile / 'other-auth-link'
    generated_executable = profile / 'generated-agent'
    own_auth.write_text('own-auth', encoding='utf-8')
    other_auth.write_text('other-auth', encoding='utf-8')
    api_key.write_text('api-key-secret', encoding='utf-8')
    cwd_input.write_text('cwd-input', encoding='utf-8')
    adc.write_text('adc-secret', encoding='utf-8')
    profile_symlink.symlink_to(other_auth)
    generated_executable.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
    generated_executable.chmod(0o755)

    probe_code = """
import json
import os
from pathlib import Path
import subprocess
import sys

def read(path):
    try:
        return {'ok': True, 'value': Path(path).read_text()}
    except OSError as ex:
        return {'ok': False, 'errno': ex.errno}

def write(path):
    try:
        Path(path).write_text('changed')
        return {'ok': True}
    except OSError as ex:
        return {'ok': False, 'errno': ex.errno}

def execute(path):
    try:
        result = subprocess.run([path], check=False)
        return {'ok': True, 'returncode': result.returncode}
    except OSError as ex:
        return {'ok': False, 'errno': ex.errno}

def signal_parent():
    try:
        os.kill(os.getppid(), 0)
        return {'ok': True}
    except OSError as ex:
        return {'ok': False, 'errno': ex.errno}

def hardlink(source, destination):
    try:
        os.link(source, destination)
        return {'ok': True}
    except OSError as ex:
        return {'ok': False, 'errno': ex.errno}

paths = sys.argv[1:]
print(json.dumps({
    'own_read': read(paths[0]),
    'own_write': write(paths[0]),
    'other_read': read(paths[1]),
    'other_write': write(paths[1]),
    'api_key_read': read(paths[2]),
    'api_key_write': write(paths[2]),
    'cwd_read': read(paths[3]),
    'cwd_write': write(paths[4]),
    'adc_read': read(paths[5]),
    'adc_write': write(paths[5]),
    'symlink_read': read(paths[6]),
    'profile_exec': execute(paths[7]),
    'external_hardlink': hardlink(paths[1], paths[8]),
    'proc_environ_read': read('/proc/1/environ'),
    'signal_parent': signal_parent(),
}))
"""
    cwd_output = working_directory / 'output.txt'
    external_hardlink = profile / 'new-other-auth-link'
    result = _RunSandbox(
        AcpSandboxLauncher,
        profile=profile,
        working_directory=working_directory,
        read_files=(adc,),
        command=[
            PYTHON_COMMAND,
            '-c',
            probe_code,
            str(own_auth),
            str(other_auth),
            str(api_key),
            str(cwd_input),
            str(cwd_output),
            str(adc),
            str(profile_symlink),
            str(generated_executable),
            str(external_hardlink),
        ],
    )

    assert result.returncode == 0, result.stderr
    probe = json.loads(result.stdout)
    assert probe['own_read'] == {'ok': True, 'value': 'own-auth'}
    assert probe['own_write'] == {'ok': True}
    assert probe['cwd_read'] == {'ok': True, 'value': 'cwd-input'}
    assert probe['adc_read'] == {'ok': True, 'value': 'adc-secret'}
    for denied_operation in (
        'other_read',
        'other_write',
        'api_key_read',
        'api_key_write',
        'cwd_write',
        'adc_write',
        'symlink_read',
        'profile_exec',
        'external_hardlink',
        'proc_environ_read',
        'signal_parent',
    ):
        assert probe[denied_operation]['ok'] is False, denied_operation

    assert own_auth.read_text(encoding='utf-8') == 'changed'
    assert other_auth.read_text(encoding='utf-8') == 'other-auth'
    assert api_key.read_text(encoding='utf-8') == 'api-key-secret'
    assert adc.read_text(encoding='utf-8') == 'adc-secret'
    assert cwd_output.exists() is False
    assert external_hardlink.exists() is False


def test_acp_sandbox_only_exposes_explicit_read_file(
    AcpSandboxLauncher: Path,
    tmp_path: Path,
) -> None:
    """Gemini ADC 相当のファイルは明示 rule がある provider だけが読める。"""

    profile = tmp_path / 'provider'
    workspace = profile / 'workspace'
    profile.mkdir()
    workspace.mkdir()
    credential = tmp_path / 'credential.json'
    credential.write_text('provider-credential', encoding='utf-8')
    probe_code = (
        'from pathlib import Path; import sys; '
        'print(Path(sys.argv[1]).read_text())'
    )

    denied = _RunSandbox(
        AcpSandboxLauncher,
        profile=profile,
        working_directory=workspace,
        command=[PYTHON_COMMAND, '-c', probe_code, str(credential)],
    )
    allowed = _RunSandbox(
        AcpSandboxLauncher,
        profile=profile,
        working_directory=workspace,
        read_files=(credential,),
        command=[PYTHON_COMMAND, '-c', probe_code, str(credential)],
    )

    assert denied.returncode != 0
    assert 'provider-credential' not in denied.stdout
    assert allowed.returncode == 0
    assert allowed.stdout.strip() == 'provider-credential'


def test_acp_sandbox_rejects_symlink_policy_roots_before_exec(
    AcpSandboxLauncher: Path,
    tmp_path: Path,
) -> None:
    """profile / cwd の symlink は provider 起動前に fail-closed になる。"""

    profile = tmp_path / 'profile'
    workspace = tmp_path / 'workspace'
    profile.mkdir()
    workspace.mkdir()
    profile_link = tmp_path / 'profile-link'
    cwd_link = tmp_path / 'workspace-link'
    profile_link.symlink_to(profile, target_is_directory=True)
    cwd_link.symlink_to(workspace, target_is_directory=True)

    sentinel = profile / 'started'
    command = [
        PYTHON_COMMAND,
        '-c',
        'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("started")',
        str(sentinel),
    ]

    for policy_profile, policy_cwd in (
        (profile_link, workspace),
        (profile, cwd_link),
    ):
        result = _RunSandbox(
            AcpSandboxLauncher,
            profile=policy_profile,
            working_directory=policy_cwd,
            command=command,
        )
        assert result.returncode == 126
        assert result.stderr == 'ACP sandbox setup failed.\n'
        assert sentinel.exists() is False


def test_acp_sandbox_rejects_removed_custom_command_option(
    AcpSandboxLauncher: Path,
    tmp_path: Path,
) -> None:
    """廃止済み --custom-command を旧呼び出し側が渡しても agent を起動しない。"""

    profile = tmp_path / 'profile'
    workspace = tmp_path / 'workspace'
    profile.mkdir()
    workspace.mkdir()
    sentinel = profile / 'started'
    result = subprocess.run(
        [
            str(AcpSandboxLauncher),
            '--profile',
            str(profile),
            '--working-directory',
            str(workspace),
            '--custom-command',
            '--',
            PYTHON_COMMAND,
            '-c',
            'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("started")',
            str(sentinel),
        ],
        cwd=workspace,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 126
    assert result.stderr == 'ACP sandbox arguments are invalid.\n'
    assert sentinel.exists() is False


def test_acp_sandbox_rejects_preexisting_profile_hardlink_before_exec(
    AcpSandboxLauncher: Path,
    tmp_path: Path,
) -> None:
    """旧未隔離 provider が profile に残した外部秘密への hardlink を起動前に拒否する。"""

    profile = tmp_path / 'profile'
    workspace = profile / 'workspace'
    protected_data = tmp_path / 'protected'
    profile.mkdir()
    workspace.mkdir()
    protected_data.mkdir()

    protected_secret = protected_data / 'secret.json'
    protected_secret.write_text('protected-secret', encoding='utf-8')
    stale_hardlink = profile / 'stale-secret-link'
    stale_hardlink.hardlink_to(protected_secret)
    sentinel = tmp_path / 'provider-started'
    command = [
        PYTHON_COMMAND,
        '-c',
        'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("started")',
        str(sentinel),
    ]

    result = _RunSandbox(
        AcpSandboxLauncher,
        profile=profile,
        working_directory=workspace,
        command=command,
    )

    assert result.returncode == 126
    assert result.stderr == 'ACP sandbox setup failed.\n'
    assert result.stdout == ''
    assert sentinel.exists() is False
    assert protected_secret.read_text(encoding='utf-8') == 'protected-secret'
