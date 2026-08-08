"""R-07: BS4K の JWT シークレット生成と既存ファイル権限を回帰テストする。"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import os
import stat
import string
import threading
from pathlib import Path

import pytest

from app import constants as constants_module
from app.constants import _LoadOrCreateJWTSecretKey


def test_CreateNewFileWithMode0600(tmp_path: Path) -> None:
    secret_path = tmp_path / 'jwt_secret.dat'
    secret_key = _LoadOrCreateJWTSecretKey(secret_path)

    assert len(secret_key) == 64
    assert all(char in string.hexdigits for char in secret_key)
    assert stat.S_IMODE(os.stat(secret_path).st_mode) == 0o600
    assert secret_path.read_text(encoding='utf-8').strip() == secret_key


def test_CreateNewFileIgnoresUmask(tmp_path: Path) -> None:
    secret_path = tmp_path / 'jwt_secret.dat'
    previous_umask = os.umask(0o777)
    try:
        secret_key = _LoadOrCreateJWTSecretKey(secret_path)
    finally:
        os.umask(previous_umask)

    assert len(secret_key) == 64
    assert stat.S_IMODE(os.stat(secret_path).st_mode) == 0o600


def test_RepairUnsafeExistingMode(tmp_path: Path) -> None:
    secret_path = tmp_path / 'jwt_secret.dat'
    secret_path.write_text('a' * 64, encoding='utf-8')
    os.chmod(secret_path, 0o4777)

    assert _LoadOrCreateJWTSecretKey(secret_path) == 'a' * 64
    assert stat.S_IMODE(os.stat(secret_path).st_mode) == 0o600


def test_RejectSymlink(tmp_path: Path) -> None:
    secret_path = tmp_path / 'jwt_secret.dat'
    target_path = tmp_path / 'target.dat'
    target_path.write_text('b' * 64, encoding='utf-8')
    secret_path.symlink_to(target_path)

    with pytest.raises(OSError):
        _LoadOrCreateJWTSecretKey(secret_path)


def test_RejectNonRegularFile(tmp_path: Path) -> None:
    secret_path = tmp_path / 'jwt_secret.dat'
    secret_path.mkdir()

    with pytest.raises(RuntimeError, match='regular file'):
        _LoadOrCreateJWTSecretKey(secret_path)


def test_RejectMultipleHardLinks(tmp_path: Path) -> None:
    secret_path = tmp_path / 'jwt_secret.dat'
    secret_path.write_text('c' * 64, encoding='utf-8')
    os.link(secret_path, tmp_path / 'other.dat')

    with pytest.raises(RuntimeError, match='hard links'):
        _LoadOrCreateJWTSecretKey(secret_path)


def test_RejectDifferentOwner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_path = tmp_path / 'jwt_secret.dat'
    secret_path.write_text('d' * 64, encoding='utf-8')
    real_uid = os.stat(secret_path).st_uid
    monkeypatch.setattr(constants_module.os, 'getuid', lambda: real_uid + 1)

    with pytest.raises(RuntimeError, match='owned by'):
        _LoadOrCreateJWTSecretKey(secret_path)


@pytest.mark.parametrize('secret_key', ['a', 'g' * 64, 'a' * 65])
def test_RejectInvalidExistingSecret(tmp_path: Path, secret_key: str) -> None:
    secret_path = tmp_path / 'jwt_secret.dat'
    secret_path.write_text(secret_key, encoding='utf-8')

    with pytest.raises(RuntimeError, match=r'invalid|incomplete'):
        _LoadOrCreateJWTSecretKey(secret_path)


def test_WriteAllBytesHandlesShortWrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_path = tmp_path / 'jwt_secret.dat'
    real_write = os.write
    call_count = 0

    def ShortWrite(fd: int, data: bytes | bytearray | memoryview) -> int:
        nonlocal call_count
        call_count += 1
        if call_count <= 3 and len(data) > 1:
            return real_write(fd, data[:1])
        return real_write(fd, data)

    monkeypatch.setattr(constants_module.os, 'write', ShortWrite)

    secret_key = _LoadOrCreateJWTSecretKey(secret_path)

    assert len(secret_key) == 64
    assert secret_path.read_text(encoding='utf-8').strip() == secret_key
    assert call_count > 1


def test_RemoveIncompleteFileAfterWriteFailure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_path = tmp_path / 'jwt_secret.dat'
    monkeypatch.setattr(constants_module.os, 'write', lambda _fd, _data: 0)

    with pytest.raises(RuntimeError, match='Failed to write'):
        _LoadOrCreateJWTSecretKey(secret_path)

    assert secret_path.exists() is False


def test_SimultaneousInitialization(tmp_path: Path) -> None:
    secret_path = tmp_path / 'jwt_secret.dat'
    results: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def Initialize() -> None:
        try:
            barrier.wait()
            results.append(_LoadOrCreateJWTSecretKey(secret_path))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=Initialize) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 8
    assert len(set(results)) == 1
    assert len(results[0]) == 64


def test_ExceptionDoesNotContainSecret(tmp_path: Path) -> None:
    secret_path = tmp_path / 'jwt_secret.dat'
    secret_path.write_text('x' * 64, encoding='utf-8')
    os.link(secret_path, tmp_path / 'other.dat')

    with pytest.raises(RuntimeError) as exc_info:
        _LoadOrCreateJWTSecretKey(secret_path)

    assert 'x' * 64 not in str(exc_info.value)
