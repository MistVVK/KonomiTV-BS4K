from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from app.streams.TSCodecBridgeRuntime import TSCodecBridgeRuntimeVerifier


@pytest.fixture(autouse = True)
def ResetVerifierCache(monkeypatch: pytest.MonkeyPatch) -> None:
    """各ケースでruntime cacheを独立させる。"""

    monkeypatch.setattr(TSCodecBridgeRuntimeVerifier, '_cached_signature', None)
    monkeypatch.setattr(TSCodecBridgeRuntimeVerifier, '_cached_result', False)
    monkeypatch.setattr(TSCodecBridgeRuntimeVerifier, '_process_generation', 'test-generation')
    monkeypatch.setattr(TSCodecBridgeRuntimeVerifier, '_process_start_count', 0)
    monkeypatch.setattr(TSCodecBridgeRuntimeVerifier, '_live_process_start_count', 0)
    monkeypatch.setattr(TSCodecBridgeRuntimeVerifier, '_probe_process_start_count', 0)
    monkeypatch.setattr(TSCodecBridgeRuntimeVerifier, '_verification_process_start_count', 0)


def _writeRuntime(
    tmp_path: Path,
    *,
    cli_version: str = '0.1.0',
    mapping_version: str = '1',
    executable_sha256: str | None = None,
    duplicate_cli_version: bool = False,
) -> tuple[Path, str]:
    """最小runtime実体とmanifestを作る。"""

    executable = tmp_path / 'ts-codec-bridge.elf'
    executable.write_bytes(b'fixed bridge executable')
    executable.chmod(0o755)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    manifest_lines = [
        f'CLI_VERSION\t{cli_version}',
        f'TS_MAPPING_VERSION\t{mapping_version}',
        f'EXECUTABLE_SHA256\t{executable_sha256 or digest}',
        'BUILDER_PACKAGE\tsbcl\t2:2.1.11-1\thttps://archive.ubuntu.com/ubuntu jammy/universe',
    ]
    if duplicate_cli_version:
        manifest_lines.append(f'CLI_VERSION\t{cli_version}')
    executable.with_name('Runtime-Manifest.tsv').write_text(
        '\n'.join(manifest_lines) + '\n',
        encoding = 'utf-8',
    )
    return executable, digest


def testRuntimeManifestVersionAndDigestAreVerifiedOncePerGeneration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SHA・CLI版・mapping版が一致し、同一世代の二回目はprocessを起動しない。"""

    executable, _digest = _writeRuntime(tmp_path)
    calls: list[str] = []

    def Run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """固定版表示を返すBridge代替。"""

        option = command[1]
        calls.append(option)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout = '0.1.0\n' if option == '--version' else '1\n',
            stderr = '',
        )

    monkeypatch.setattr(subprocess, 'run', Run)

    assert TSCodecBridgeRuntimeVerifier.isAvailable(executable) is True
    assert TSCodecBridgeRuntimeVerifier.isAvailable(executable) is True
    assert calls == ['--version', '--mapping-version']
    assert TSCodecBridgeRuntimeVerifier.getProcessStartSnapshot() == (
        'test-generation',
        2,
        0,
        0,
        2,
    )


def testProcessStartCountIsMonotonicAndSeparatedByKind() -> None:
    """同じprocess世代でliveとprobeの起動成功回数を分離して返す。"""

    assert TSCodecBridgeRuntimeVerifier.getProcessStartSnapshot() == (
        'test-generation',
        0,
        0,
        0,
        0,
    )
    TSCodecBridgeRuntimeVerifier.recordProcessStart('probe')
    TSCodecBridgeRuntimeVerifier.recordProcessStart('live')
    TSCodecBridgeRuntimeVerifier.recordProcessStart('verification')
    assert TSCodecBridgeRuntimeVerifier.getProcessStartSnapshot() == (
        'test-generation',
        3,
        1,
        1,
        1,
    )


@pytest.mark.parametrize(
    'failure',
    [
        subprocess.CalledProcessError(2, ['bridge', '--version']),
        subprocess.TimeoutExpired(['bridge', '--version'], 2),
        UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte'),
    ],
)
def testFailedRuntimeVerificationProcessStartIsCounted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    """起動後に失敗した版検査processも互換API隔離counterへ残す。"""

    executable, _digest = _writeRuntime(tmp_path)

    def Run(
        _command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        raise failure

    monkeypatch.setattr(subprocess, 'run', Run)
    assert TSCodecBridgeRuntimeVerifier.isAvailable(executable) is False
    assert TSCodecBridgeRuntimeVerifier.getProcessStartSnapshot() == (
        'test-generation',
        1,
        0,
        0,
        1,
    )


@pytest.mark.parametrize(
    ('manifest_case', 'expected_process_calls'),
    [
        ('digest-mismatch', 0),
        ('duplicate-version', 0),
        ('cli-version-mismatch', 1),
        ('mapping-version-mismatch', 2),
        ('stderr', 1),
    ],
)
def testRuntimeContractMismatchFailsClosed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_case: str,
    expected_process_calls: int,
) -> None:
    """manifestまたは実版表示の不一致をBridge利用可能として公開しない。"""

    executable, _digest = _writeRuntime(
        tmp_path,
        executable_sha256 = '0' * 64 if manifest_case == 'digest-mismatch' else None,
        duplicate_cli_version = manifest_case == 'duplicate-version',
    )
    calls: list[str] = []

    def Run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """指定ケースだけ版表示を壊すBridge代替。"""

        option = command[1]
        calls.append(option)
        stdout = '0.1.0\n' if option == '--version' else '1\n'
        stderr = ''
        if manifest_case == 'cli-version-mismatch' and option == '--version':
            stdout = '9.9.9\n'
        elif (
            manifest_case == 'mapping-version-mismatch'
            and option == '--mapping-version'
        ):
            stdout = '2\n'
        elif manifest_case == 'stderr' and option == '--version':
            stderr = 'unexpected warning\n'
        return subprocess.CompletedProcess(
            command,
            0,
            stdout = stdout,
            stderr = stderr,
        )

    monkeypatch.setattr(subprocess, 'run', Run)

    assert TSCodecBridgeRuntimeVerifier.isAvailable(executable) is False
    assert len(calls) == expected_process_calls
    assert TSCodecBridgeRuntimeVerifier.getProcessStartSnapshot() == (
        'test-generation',
        expected_process_calls,
        0,
        0,
        expected_process_calls,
    )


def testRuntimeReplacementInvalidatesCachedResult(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """検証後の実行形式差替えをstat署名で検出し、古い成功cacheを返さない。"""

    executable, _digest = _writeRuntime(tmp_path)
    calls: list[str] = []

    def Run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """正常版表示を返し、起動回数を記録する。"""

        calls.append(command[1])
        return subprocess.CompletedProcess(
            command,
            0,
            stdout = '0.1.0\n' if command[1] == '--version' else '1\n',
            stderr = '',
        )

    monkeypatch.setattr(subprocess, 'run', Run)
    assert TSCodecBridgeRuntimeVerifier.isAvailable(executable) is True

    executable.write_bytes(b'replaced bridge executable with a different digest')
    executable.chmod(0o755)

    assert TSCodecBridgeRuntimeVerifier.isAvailable(executable) is False
    assert calls == ['--version', '--mapping-version']
