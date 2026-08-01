import logging
import os
import re
import stat
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from tortoise import timezone

import app.utils.LogRotation as LogRotation
from app.utils.LogRotation import (
    CleanupOldArchiveLogs,
    DailyRotatingFileHandler,
    OpenSecureLogDirectory,
    OpenSecureLogFile,
    RenameSecureLogFile,
    SecureFileHandler,
    SplitServerLogByDate,
    UnsafeLogPathError,
)


@pytest.fixture(autouse=True)
def _clear_reported_unsafe_paths() -> None:
    """
    不正パスの警告抑制状態をテストごとに初期化する。

    Returns:
        None
    """

    LogRotation._REPORTED_UNSAFE_LOG_PATHS.clear()


def test_secure_file_handler_appends_regular_file_and_repairs_modes(tmp_path: Path) -> None:
    """
    owner が一致する regular file には従来どおり追記し、file/dir mode を固定する。

    Args:
        tmp_path (Path): pytest が提供する一時ディレクトリ

    Returns:
        None
    """

    logs_directory = tmp_path / 'logs'
    logs_directory.mkdir(mode=0o777)
    logs_directory.chmod(0o777)
    log_path = logs_directory / 'access.log'

    handler = SecureFileHandler(log_path, encoding='utf-8')
    try:
        handler.emit(logging.LogRecord('access', logging.INFO, __file__, 1, 'request', (), None))
        handler.flush()
    finally:
        handler.close()

    assert log_path.read_text(encoding='utf-8') == 'request\n'
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o666
    assert stat.S_IMODE(logs_directory.stat().st_mode) == 0o750


@pytest.mark.parametrize(
    'unsafe_kind',
    ['symlink', 'dangling-symlink', 'hardlink', 'fifo'],
)
def test_secure_file_handler_rejects_unsafe_fixed_log_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    unsafe_kind: str,
) -> None:
    """
    symlink・dangling symlink・hardlink・FIFO を即時に拒否し、参照先を変更しない。

    Args:
        tmp_path (Path): pytest が提供する一時ディレクトリ
        capsys (pytest.CaptureFixture[str]): 標準エラー出力を検証する fixture
        unsafe_kind (str): 構築する不正パスの種類

    Returns:
        None
    """

    logs_directory = tmp_path / 'logs'
    logs_directory.mkdir()
    log_path = logs_directory / 'server.log'
    protected_path = tmp_path / 'protected'
    protected_path.write_text('protected', encoding='utf-8')
    protected_path.chmod(0o600)

    if unsafe_kind == 'symlink':
        log_path.symlink_to(protected_path)
    elif unsafe_kind == 'dangling-symlink':
        protected_path.unlink()
        log_path.symlink_to(protected_path)
    elif unsafe_kind == 'hardlink':
        os.link(protected_path, log_path)
    else:
        os.mkfifo(log_path)

    handler = SecureFileHandler(log_path, encoding='utf-8')
    try:
        assert handler._using_safe_sink is True
        handler.emit(logging.LogRecord('server', logging.INFO, __file__, 1, 'attacker', (), None))
        handler.flush()
    finally:
        handler.close()

    assert 'Refused unsafe log path' in capsys.readouterr().err
    if protected_path.exists():
        assert protected_path.read_text(encoding='utf-8') == 'protected'
        assert stat.S_IMODE(protected_path.stat().st_mode) == 0o600
    else:
        assert protected_path.exists() is False


def test_secure_file_handler_rejects_owner_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    open 後の fstat() で owner 不一致になった regular file を拒否する。

    Args:
        tmp_path (Path): pytest が提供する一時ディレクトリ
        monkeypatch (pytest.MonkeyPatch): file FD の owner だけを差し替える fixture

    Returns:
        None
    """

    logs_directory = tmp_path / 'logs'
    logs_directory.mkdir()
    log_path = logs_directory / 'server.log'
    log_path.write_text('protected', encoding='utf-8')
    real_fstat = os.fstat

    def OwnerMismatchFstat(file_descriptor: int) -> os.stat_result | SimpleNamespace:
        file_stat = real_fstat(file_descriptor)
        if stat.S_ISREG(file_stat.st_mode):
            return SimpleNamespace(
                st_mode=file_stat.st_mode,
                st_uid=file_stat.st_uid + 1,
                st_nlink=file_stat.st_nlink,
            )
        return file_stat

    monkeypatch.setattr(LogRotation.os, 'fstat', OwnerMismatchFstat)
    handler = SecureFileHandler(log_path, encoding='utf-8')
    try:
        assert handler._using_safe_sink is True
    finally:
        handler.close()
    assert log_path.read_text(encoding='utf-8') == 'protected'


def test_open_secure_log_file_uses_opened_fd_across_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    open 直後に path を差し替えても、書き込みと fchmod は元の FD にだけ作用する。

    Args:
        tmp_path (Path): pytest が提供する一時ディレクトリ
        monkeypatch (pytest.MonkeyPatch): fstat 直前の path 差し替えを注入する fixture

    Returns:
        None
    """

    logs_directory = tmp_path / 'logs'
    logs_directory.mkdir()
    log_path = logs_directory / 'server.log'
    original_inode_path = logs_directory / 'original-inode.log'
    log_path.write_text('original\n', encoding='utf-8')
    log_path.chmod(0o600)
    real_fstat = os.fstat
    path_replaced = False

    def ReplacePathBeforeFstat(file_descriptor: int) -> os.stat_result:
        nonlocal path_replaced
        file_stat = real_fstat(file_descriptor)
        if stat.S_ISREG(file_stat.st_mode) and path_replaced is False:
            os.replace(log_path, original_inode_path)
            log_path.write_text('replacement\n', encoding='utf-8')
            log_path.chmod(0o600)
            path_replaced = True
        return file_stat

    monkeypatch.setattr(LogRotation.os, 'fstat', ReplacePathBeforeFstat)
    with OpenSecureLogFile(log_path, mode='a', encoding='utf-8') as log_file:
        log_file.write('appended\n')

    assert original_inode_path.read_text(encoding='utf-8') == 'original\nappended\n'
    assert stat.S_IMODE(original_inode_path.stat().st_mode) == 0o666
    assert log_path.read_text(encoding='utf-8') == 'replacement\n'
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


def _configure_log_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """
    起動時分割と cleanup が一時ログディレクトリを使うよう module 定数を差し替える。

    Args:
        monkeypatch (pytest.MonkeyPatch): module 定数を差し替える fixture
        tmp_path (Path): pytest が提供する一時ディレクトリ

    Returns:
        tuple[Path, Path]: server log path と archives directory
    """

    logs_directory = tmp_path / 'logs'
    logs_directory.mkdir()
    server_log_path = logs_directory / 'KonomiTV-BS4K-Server.log'
    archives_directory = logs_directory / 'archives'
    monkeypatch.setattr(LogRotation, 'KONOMITV_SERVER_LOG_PATH', server_log_path)
    monkeypatch.setattr(LogRotation, 'LOGS_ARCHIVES_DIR', archives_directory)
    monkeypatch.setattr(
        LogRotation,
        'ARCHIVE_FILE_PATTERN',
        re.compile(
            rf'^{re.escape(server_log_path.stem)}\.(\d{{8}}){re.escape(server_log_path.suffix)}$',
        ),
    )
    return server_log_path, archives_directory


def test_split_server_log_by_date_preserves_normal_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    検証済み FD だけを使って過去日ログを分割し、今日分と mode を維持する。

    Args:
        tmp_path (Path): pytest が提供する一時ディレクトリ
        monkeypatch (pytest.MonkeyPatch): module 定数を差し替える fixture

    Returns:
        None
    """

    server_log_path, archives_directory = _configure_log_paths(monkeypatch, tmp_path)
    today = timezone.now().date()
    yesterday = today - timedelta(days=1)
    server_log_path.write_text(
        f'[{yesterday:%Y/%m/%d} 23:59:59.000] INFO: old\n'
        f'[{today:%Y/%m/%d} 00:00:00.000] INFO: today\n',
        encoding='utf-8',
    )

    SplitServerLogByDate()

    archive_path = archives_directory / f'KonomiTV-BS4K-Server.{yesterday:%Y%m%d}.log'
    assert server_log_path.read_text(encoding='utf-8') == f'[{today:%Y/%m/%d} 00:00:00.000] INFO: today\n'
    assert archive_path.read_text(encoding='utf-8') == f'[{yesterday:%Y/%m/%d} 23:59:59.000] INFO: old\n'
    assert stat.S_IMODE(server_log_path.stat().st_mode) == 0o666
    assert stat.S_IMODE(archive_path.stat().st_mode) == 0o666
    assert stat.S_IMODE(archives_directory.stat().st_mode) == 0o750


def test_cleanup_rejects_symlink_and_hardlink_archives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    保存期間を超えていても symlink と hardlink は削除せず、安全な regular file だけを削除する。

    Args:
        tmp_path (Path): pytest が提供する一時ディレクトリ
        monkeypatch (pytest.MonkeyPatch): module 定数を差し替える fixture

    Returns:
        None
    """

    _, archives_directory = _configure_log_paths(monkeypatch, tmp_path)
    archives_directory.mkdir()
    old_date = timezone.now().date() - timedelta(days=60)
    regular_archive = archives_directory / f'KonomiTV-BS4K-Server.{old_date:%Y%m%d}.log'
    symlink_archive = archives_directory / f'KonomiTV-BS4K-Server.{old_date - timedelta(days=1):%Y%m%d}.log'
    hardlink_archive = archives_directory / f'KonomiTV-BS4K-Server.{old_date - timedelta(days=2):%Y%m%d}.log'
    protected_path = tmp_path / 'protected.log'
    protected_path.write_text('protected', encoding='utf-8')
    regular_archive.write_text('expired', encoding='utf-8')
    symlink_archive.symlink_to(protected_path)
    os.link(protected_path, hardlink_archive)

    CleanupOldArchiveLogs(30)

    assert regular_archive.exists() is False
    assert symlink_archive.is_symlink() is True
    assert hardlink_archive.exists() is True
    assert protected_path.read_text(encoding='utf-8') == 'protected'


def test_daily_handler_does_not_rotate_unsafe_archive_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    日次ローテーション先が symlink の場合は追跡も上書きもせず、active log を維持する。

    Args:
        tmp_path (Path): pytest が提供する一時ディレクトリ
        monkeypatch (pytest.MonkeyPatch): archive directory を一時パスへ差し替える fixture

    Returns:
        None
    """

    logs_directory = tmp_path / 'logs'
    logs_directory.mkdir()
    log_path = logs_directory / 'server.log'
    protected_path = tmp_path / 'protected.log'
    protected_path.write_text('protected', encoding='utf-8')
    monkeypatch.setattr(LogRotation, 'LOGS_ARCHIVES_DIR', logs_directory / 'archives')
    handler = DailyRotatingFileHandler(log_path, encoding='utf-8')
    try:
        handler.suffix = 'fixed'
        handler.namer = lambda _: str(logs_directory / 'archives' / 'server.fixed.log')
        archive_path = Path(handler.rotation_filename(handler.baseFilename + '.fixed'))
        archive_path.parent.mkdir()
        archive_path.symlink_to(protected_path)
        handler.doRollover()
        handler.emit(logging.LogRecord('server', logging.INFO, __file__, 1, 'continued', (), None))
        handler.flush()
    finally:
        handler.close()

    assert protected_path.read_text(encoding='utf-8') == 'protected'
    assert archive_path.is_symlink() is True
    assert log_path.read_text(encoding='utf-8') == 'continued\n'


def test_dual_daily_handlers_share_one_safe_rollover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    default/debug の2 handler が同じ固定ログを扱っても、先行 handler の rename を不正扱いしない。

    Args:
        tmp_path (Path): pytest が提供する一時ディレクトリ
        monkeypatch (pytest.MonkeyPatch): archive directory を一時パスへ差し替える fixture
        capsys (pytest.CaptureFixture[str]): 不正パス警告が出ないことを検証する fixture

    Returns:
        None
    """

    logs_directory = tmp_path / 'logs'
    logs_directory.mkdir()
    archives_directory = logs_directory / 'archives'
    log_path = logs_directory / 'server.log'
    monkeypatch.setattr(LogRotation, 'LOGS_ARCHIVES_DIR', archives_directory)
    handlers = [
        DailyRotatingFileHandler(log_path, encoding='utf-8'),
        DailyRotatingFileHandler(log_path, encoding='utf-8'),
    ]
    archive_path = archives_directory / 'server.fixed.log'
    try:
        for handler in handlers:
            handler.suffix = 'fixed'
            handler.namer = lambda _: str(archive_path)
        handlers[0].emit(logging.LogRecord('server', logging.INFO, __file__, 1, 'before', (), None))
        handlers[0].flush()

        for handler in handlers:
            handler.doRollover()

        handlers[1].emit(logging.LogRecord('server', logging.INFO, __file__, 1, 'after', (), None))
        handlers[1].flush()
    finally:
        for handler in handlers:
            handler.close()

    assert archive_path.read_text(encoding='utf-8') == 'before\n'
    assert log_path.read_text(encoding='utf-8') == 'after\n'
    assert stat.S_IMODE(archive_path.stat().st_mode) == 0o666
    assert 'Refused unsafe log path' not in capsys.readouterr().err


def test_open_secure_log_directory_rejects_intermediate_symlink(tmp_path: Path) -> None:
    """
    途中 path 要素が symlink の場合、最終要素だけでなく中間も O_NOFOLLOW で拒否する。

    Args:
        tmp_path (Path): pytest が提供する一時ディレクトリ

    Returns:
        None
    """

    base_directory = tmp_path / 'base'
    elsewhere_directory = tmp_path / 'elsewhere'
    base_directory.mkdir()
    elsewhere_directory.mkdir()
    (base_directory / 'server').symlink_to(elsewhere_directory)
    logs_directory = base_directory / 'server' / 'logs'

    with pytest.raises((OSError, UnsafeLogPathError)):
        OpenSecureLogDirectory(logs_directory, create=True)


def test_rename_secure_log_file_moves_verified_inode_with_dir_fd(tmp_path: Path) -> None:
    """
    RenameSecureLogFile が dir_fd 上で inode 再検証後にだけ rename することを検証する。

    Args:
        tmp_path (Path): pytest が提供する一時ディレクトリ

    Returns:
        None
    """

    logs_directory = tmp_path / 'logs'
    archives_directory = logs_directory / 'archives'
    logs_directory.mkdir()
    source_path = logs_directory / 'server.log'
    destination_path = archives_directory / 'server.20260101.log'
    source_path.write_text('rotated\n', encoding='utf-8')
    source_stat = source_path.stat()
    expected_identity = (source_stat.st_dev, source_stat.st_ino)

    RenameSecureLogFile(source_path, destination_path, expected_identity)

    assert source_path.exists() is False
    assert destination_path.read_text(encoding='utf-8') == 'rotated\n'
    assert (destination_path.stat().st_dev, destination_path.stat().st_ino) == expected_identity


def test_rename_secure_log_file_rejects_inode_mismatch(tmp_path: Path) -> None:
    """
    rename 直前に source inode が差し替わっていたら移動せず拒否する。

    Args:
        tmp_path (Path): pytest が提供する一時ディレクトリ

    Returns:
        None
    """

    logs_directory = tmp_path / 'logs'
    archives_directory = logs_directory / 'archives'
    logs_directory.mkdir()
    source_path = logs_directory / 'server.log'
    destination_path = archives_directory / 'server.20260101.log'
    source_path.write_text('original\n', encoding='utf-8')
    stale_identity = (0, 0)

    with pytest.raises(UnsafeLogPathError, match='changed before rotation'):
        RenameSecureLogFile(source_path, destination_path, stale_identity)

    assert source_path.read_text(encoding='utf-8') == 'original\n'
    assert destination_path.exists() is False
