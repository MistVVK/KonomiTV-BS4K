import fcntl
import io
import os
import re
import stat
import sys
import tempfile
import time
import traceback
from contextlib import ExitStack
from datetime import datetime, timedelta
from logging import FileHandler
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Literal, TextIO

from tortoise import timezone

from app.constants import (
    KONOMITV_SERVER_LOG_PATH,
    LOGS_ARCHIVES_DIR,
    SERVER_LOG_ARCHIVE_RETENTION_DAYS,
)


# サーバーログの日付プレフィックスを抽出するための正規表現
# ログフォーマット例: [2025/12/17 16:33:58.880] INFO: Message
LOG_DATE_PATTERN = re.compile(r'^\[(\d{4}/\d{2}/\d{2}) \d{2}:\d{2}:\d{2}\.\d{3}\]')

# アーカイブファイル名から日付キー (YYYYMMDD) を抽出するための正規表現
ARCHIVE_FILE_PATTERN = re.compile(
    rf'^{re.escape(KONOMITV_SERVER_LOG_PATH.stem)}\.(\d{{8}}){re.escape(KONOMITV_SERVER_LOG_PATH.suffix)}$',
)

# 正常なログファイルは従来どおり誰でも読み書きできる mode を維持する
## 親ディレクトリを 0o750 に固定するため、ログディレクトリ外のユーザーからは到達できない
LOG_FILE_MODE = 0o666
LOG_DIRECTORY_MODE = 0o750

# 同じ不正パスでログ出力のたびに標準エラーを埋め尽くさないよう、通知済みパスを保持する
_REPORTED_UNSAFE_LOG_PATHS: set[Path] = set()


class UnsafeLogPathError(OSError):
    """
    ログファイルまたはログディレクトリがセキュリティ要件を満たさないことを示す例外。
    """


def ValidateSecureLogFileStat(file_stat: os.stat_result, expected_uid: int) -> None:
    """
    開いたログファイルの inode が安全な regular file であることを検証する。

    Args:
        file_stat (os.stat_result): 検証対象 FD に対する os.fstat() の結果
        expected_uid (int): ログファイルを所有している必要がある実効 UID

    Returns:
        None

    Raises:
        UnsafeLogPathError: regular file・owner・link count のいずれかが不正な場合
    """

    if stat.S_ISREG(file_stat.st_mode) is False:
        raise UnsafeLogPathError('The opened log path is not a regular file.')
    if file_stat.st_uid != expected_uid:
        raise UnsafeLogPathError(
            f'The opened log file owner UID {file_stat.st_uid} does not match effective UID {expected_uid}.',
        )
    if file_stat.st_nlink != 1:
        raise UnsafeLogPathError(f'The opened log file has an unsafe link count: {file_stat.st_nlink}.')


def OpenSecureLogDirectory(directory_path: Path, create: bool = False) -> int:
    """
    ログディレクトリを各 path 要素ごとに O_NOFOLLOW で開き、最終ディレクトリの安全性を検証する。

    Args:
        directory_path (Path): 開くログディレクトリ
        create (bool): 存在しない場合に最終ディレクトリまでの各要素を作成するか

    Returns:
        int: 検証済みディレクトリを指す FD

    Raises:
        OSError: ディレクトリを作成またはオープンできない場合
        UnsafeLogPathError: directory・owner のいずれかが不正な場合
    """

    # 途中要素の symlink を追わないよう、絶対 path を component 単位で openat する
    ## Linux の O_NOFOLLOW は最終要素にしか効かないため、1 回の open では中間 symlink を防げない
    absolute_path = Path(os.path.abspath(directory_path))
    path_parts = absolute_path.parts
    if len(path_parts) < 1 or path_parts[0] != os.sep:
        raise UnsafeLogPathError(f'The log directory path is not absolute: {directory_path}')

    # root から辿り、各要素を O_NOFOLLOW で directory として開く
    directory_fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path_parts[1:]:
            if create is True:
                try:
                    os.mkdir(part, LOG_DIRECTORY_MODE, dir_fd=directory_fd)
                except FileExistsError:
                    pass

            try:
                next_directory_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                # create=False では欠落をそのまま伝播し、create=True では直前の mkdir 競合を吸収する
                if create is False:
                    raise
                os.mkdir(part, LOG_DIRECTORY_MODE, dir_fd=directory_fd)
                next_directory_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory_fd,
                )

            os.close(directory_fd)
            directory_fd = next_directory_fd

        directory_stat = os.fstat(directory_fd)
        if stat.S_ISDIR(directory_stat.st_mode) is False:
            raise UnsafeLogPathError('The opened log directory path is not a directory.')

        # 最終ログディレクトリだけを実行ユーザー所有に要求する（/ や /code など上位は root 所有でもよい）
        expected_uid = os.geteuid()
        if directory_stat.st_uid != expected_uid:
            raise UnsafeLogPathError(
                f'The log directory owner UID {directory_stat.st_uid} does not match effective UID {expected_uid}.',
            )

        # owner を検証した同じ FD に対して mode を是正し、world-writable な状態を残さない
        os.fchmod(directory_fd, LOG_DIRECTORY_MODE)
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def OpenSecureLogFile(
    file_path: Path,
    mode: Literal['a', 'r'],
    encoding: str,
    errors: str | None = None,
    file_mode: int | None = LOG_FILE_MODE,
) -> io.TextIOWrapper:
    """
    ログファイルを O_NOFOLLOW で開き、同じ FD を検証してから text stream に変換する。

    Args:
        file_path (Path): 開くログファイル
        mode (Literal['a', 'r']): append または read のオープンモード
        encoding (str): テキストエンコーディング
        errors (str | None): デコードエラー処理
        file_mode (int | None): 検証後に同じ FD へ設定する mode。None の場合は変更しない

    Returns:
        io.TextIOWrapper: 検証済み FD を所有するテキストストリーム

    Raises:
        OSError: ファイルをオープンできない場合
        UnsafeLogPathError: file type・owner・link count のいずれかが不正な場合
    """

    directory_fd = OpenSecureLogDirectory(file_path.parent, create=mode == 'a')
    file_fd: int | None = None
    try:
        # open 時だけ NONBLOCK を付け、FIFO などで reader 待ちに入って event loop を止めない
        ## 検証後は blocking に戻し、logging が EAGAIN を再試行しない前提と整合させる
        flags = os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        if mode == 'a':
            flags |= os.O_WRONLY | os.O_APPEND | os.O_CREAT
        else:
            flags |= os.O_RDONLY
        file_fd = os.open(file_path.name, flags, LOG_FILE_MODE, dir_fd=directory_fd)

        # path を再参照せず、open した同じ FD の inode を書き込み・mode 変更前に検証する
        ValidateSecureLogFileStat(os.fstat(file_fd), os.geteuid())
        if file_mode is not None:
            os.fchmod(file_fd, file_mode)

        # 安全な regular file だと確認できた後だけ blocking I/O に戻す
        current_flags = fcntl.fcntl(file_fd, fcntl.F_GETFL)
        fcntl.fcntl(file_fd, fcntl.F_SETFL, current_flags & ~os.O_NONBLOCK)

        stream = open(file_fd, mode=mode, encoding=encoding, errors=errors, closefd=True)
        file_fd = None
        return stream
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def RenameSecureLogFile(
    source_path: Path,
    destination_path: Path,
    expected_source_identity: tuple[int, int],
) -> None:
    """
    検証済み親ディレクトリ FD 上で、source inode を再確認してから rename する。

    Args:
        source_path (Path): 移動元のログファイル
        destination_path (Path): 移動先のアーカイブファイル
        expected_source_identity (tuple[int, int]): 移動前に観測した (st_dev, st_ino)

    Returns:
        None

    Raises:
        OSError: rename 自体が失敗した場合
        UnsafeLogPathError: source の type・owner・inode が期待と一致しない場合
    """

    source_directory_fd = OpenSecureLogDirectory(source_path.parent, create=False)
    try:
        destination_directory_fd = OpenSecureLogDirectory(destination_path.parent, create=True)
        try:
            # rename 直前に source を O_NOFOLLOW で開き直し、観測済み inode と一致するか確認する
            ## FIFO 差し替え時に reader 待ちで固まらないよう、ここでも NONBLOCK を付ける
            source_fd = os.open(
                source_path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=source_directory_fd,
            )
            try:
                source_stat = os.fstat(source_fd)
                ValidateSecureLogFileStat(source_stat, os.geteuid())
                if (source_stat.st_dev, source_stat.st_ino) != expected_source_identity:
                    raise UnsafeLogPathError('The active log path changed before rotation.')
            finally:
                os.close(source_fd)

            # path 文字列ではなく、検証済み dir_fd 上の相対名だけで rename する
            os.rename(
                source_path.name,
                destination_path.name,
                src_dir_fd=source_directory_fd,
                dst_dir_fd=destination_directory_fd,
            )
        finally:
            os.close(destination_directory_fd)
    finally:
        os.close(source_directory_fd)


def ReportUnsafeLogPath(file_path: Path, error: BaseException) -> None:
    """
    file logger を再帰させず、不正ログパスの拒否を標準エラーへ一度だけ記録する。

    Args:
        file_path (Path): 拒否したログパス
        error (BaseException): 拒否理由

    Returns:
        None
    """

    resolved_path = file_path.absolute()
    if resolved_path in _REPORTED_UNSAFE_LOG_PATHS:
        return
    _REPORTED_UNSAFE_LOG_PATHS.add(resolved_path)
    print(f'WARNING: Refused unsafe log path {file_path}: {error}', file=sys.stderr)


class SecureFileHandler(FileHandler):
    """
    不正な固定ログパスを拒否し、安全な破棄先へ切り替える FileHandler。
    """

    def __init__(
        self,
        filename: str | Path,
        mode: str = 'a',
        encoding: str | None = None,
        delay: bool = False,
        errors: str | None = None,
    ) -> None:
        """
        安全な固定ログハンドラーを初期化する。

        Args:
            filename (str | Path): 固定ログファイルのパス
            mode (str): ファイルオープンモード
            encoding (str | None): テキストエンコーディング
            delay (bool): 最初の emit() までオープンを遅延するか
            errors (str | None): エンコードエラー処理
        """

        # FileHandler.__init__() 内から _open() が呼ばれるため、先に破棄先利用状態を初期化する
        self._using_safe_sink = False
        super().__init__(filename, mode=mode, encoding=encoding, delay=delay, errors=errors)

    def _open(self) -> io.TextIOWrapper:
        """
        固定ログパスを安全に開き、拒否時は /dev/null へ出力を破棄する。

        Returns:
            io.TextIOWrapper: 検証済みログファイル、または安全な破棄先
        """

        try:
            stream = OpenSecureLogFile(
                Path(self.baseFilename),
                mode='a',
                encoding=self.encoding or 'utf-8',
                errors=self.errors,
            )
            self._using_safe_sink = False
            return stream
        except (OSError, UnsafeLogPathError) as ex:
            ReportUnsafeLogPath(Path(self.baseFilename), ex)
            self._using_safe_sink = True
            return open(os.devnull, mode='a', encoding=self.encoding or 'utf-8', errors=self.errors)


class DailyRotatingFileHandler(TimedRotatingFileHandler):
    """
    JST 基準で日次ローテーションを行うログハンドラー。
    """

    def __init__(
        self,
        filename: str | Path,
        encoding: str | None = None,
        retention_days: int | None = None,
    ) -> None:
        """
        ログハンドラーを初期化する。

        Args:
            filename (str | Path): ログファイルのパス
            encoding (str | None): ファイルエンコーディング
            retention_days (int | None): アーカイブ保持日数 (None の場合は無期限保持)
        """

        # TimedRotatingFileHandler.__init__() 内から _open() が呼ばれるため、先に破棄先利用状態を初期化する
        self._using_safe_sink = False
        super().__init__(
            filename=filename,
            when='midnight',
            interval=1,
            backupCount=0,
            encoding=encoding,
            utc=False,
        )

        # 日次ローテーション用の命名規則を固定する
        ## TimedRotatingFileHandler の既定サフィックスではなく YYYYMMDD を使う
        self.suffix = '%Y%m%d'

        # ローテーション後の移動先を archives ディレクトリへ切り替える
        self.namer = self._namer

        # 後続の cleanup で使用する保持日数を保持しておく
        self.retention_days = retention_days

    def _open(self) -> io.TextIOWrapper:
        """
        ログファイルを安全に開き、拒否時は /dev/null へ出力を破棄する。

        Returns:
            io.TextIOWrapper: 開かれたログファイルストリーム
        """

        try:
            stream = OpenSecureLogFile(
                Path(self.baseFilename),
                mode='a',
                encoding=self.encoding or 'utf-8',
                errors=self.errors,
            )
            self._using_safe_sink = False
            return stream
        except (OSError, UnsafeLogPathError) as ex:
            ReportUnsafeLogPath(Path(self.baseFilename), ex)
            self._using_safe_sink = True
            return open(os.devnull, mode='a', encoding=self.encoding or 'utf-8', errors=self.errors)

    @staticmethod
    def _namer(default_name: str) -> str:
        """
        ローテーション後のファイル名を archives ディレクトリ配下に変換する。

        Args:
            default_name (str): TimedRotatingFileHandler が生成するファイル名

        Returns:
            str: 変換後のアーカイブファイル名
        """

        date_suffix = default_name.rsplit('.', 1)[1]
        return str(GetArchiveFilePath(date_suffix))

    def doRollover(self) -> None:
        """
        日付変更時のローテーション処理を実行する。

        NOTE: doRollover は TimedRotatingFileHandler の既存メソッドのオーバーライドのため、
              あえて snake_case() ではなく camelCase() で命名されている。
        """

        # ローテーション対象時刻と現在時刻を決定する
        ## rollover_time は「前回の切り替え境界」を示す時刻
        current_time = int(time.time())
        rollover_time = self.rolloverAt - self.interval

        # 不正パスを破棄先へ退避している間は、対象パスの rename や chmod を一切行わない
        ## 日付境界では安全なパスへ戻ったかだけを再検証し、次回ローテーション時刻を更新する
        if self._using_safe_sink is True:
            if self.stream is not None:
                self.stream.close()
                self.stream = None  # type: ignore[reportAttributeAccessIssue]
            if self.delay is False:
                self.stream = self._open()
            self.rolloverAt = self.computeRollover(current_time)
            CleanupOldArchiveLogs(self.retention_days)
            return

        # ハンドラー設定に応じて、ローテーション対象日時の time tuple を取得する
        if self.utc is True:
            time_tuple = time.gmtime(rollover_time)
        else:
            time_tuple = time.localtime(rollover_time)
            dst_now = time.localtime(current_time)[-1]
            dst_then = time_tuple[-1]

            # DST（夏時間）境界を跨いだ場合の 1 時間ズレを補正する
            ## 日本は DST を採用していないため本来不要だが、標準実装と同じ補正ロジックを維持して安全性を確保する
            if dst_now != dst_then:
                dst_offset = 3600 if dst_now else -3600
                time_tuple = time.localtime(rollover_time + dst_offset)

        # ローテーション後に配置するアーカイブファイルパスを確定する
        archive_name = self.rotation_filename(self.baseFilename + '.' + time.strftime(self.suffix, time_tuple))
        archive_path = Path(archive_name)

        # open 時に検証した active log の inode を、rename 直前の再検証に使う
        active_file_identity: tuple[int, int] | None = None
        if self.stream is not None:
            active_file_stat = os.fstat(self.stream.fileno())
            active_file_identity = (active_file_stat.st_dev, active_file_stat.st_ino)

        # 先に stream を閉じて、rename が失敗しにくい状態にする
        if self.stream is not None:
            self.stream.close()
            # close() 後は stream を未オープン状態に戻しておく（標準実装の doRollover() と同等の挙動）
            # pyright の型定義では stream が TextIOWrapper 扱いのため、None 代入は reportAttributeAccessIssue になる
            self.stream = None  # type: ignore[reportAttributeAccessIssue]

        # ローテーション先ディレクトリを同じ FD 上で安全な mode に是正する
        try:
            archive_directory_fd = OpenSecureLogDirectory(LOGS_ARCHIVES_DIR, create=True)
            os.close(archive_directory_fd)
        except (OSError, UnsafeLogPathError) as ex:
            ReportUnsafeLogPath(LOGS_ARCHIVES_DIR, ex)
        else:
            # 同名アーカイブが存在する場合は、regular file・owner・link count を検証してから重複扱いにする
            ## 不正な symlink / hardlink / FIFO は上書きも追記もせず、その日のローテーションだけをスキップする
            archive_exists = os.path.lexists(archive_path)
            archive_is_safe = False
            if archive_exists is True:
                try:
                    with OpenSecureLogFile(
                        archive_path,
                        mode='r',
                        encoding=self.encoding or 'utf-8',
                        errors=self.errors,
                        file_mode=None,
                    ):
                        archive_is_safe = True
                except (OSError, UnsafeLogPathError) as ex:
                    ReportUnsafeLogPath(archive_path, ex)

            # 既存の安全なアーカイブは上書きせず、未作成かつ同じ active inode の場合だけ移動する
            if archive_exists is False:
                # path ベースの os.rename を使わず、dir_fd 付き rename で TOCTOU 窓を狭める
                if active_file_identity is not None:
                    try:
                        RenameSecureLogFile(
                            Path(self.baseFilename),
                            archive_path,
                            active_file_identity,
                        )
                    except FileNotFoundError:
                        # 同一ログを共有する先行 handler が既に移動済み
                        pass
                    except FileExistsError:
                        # 先行 handler が同名アーカイブを作成済み
                        pass
                    except UnsafeLogPathError as ex:
                        ReportUnsafeLogPath(Path(self.baseFilename), ex)
                    except OSError:
                        # 共有違反など、複数 handler 競合時の一時的失敗は握り潰して継続する
                        pass
            elif archive_is_safe is False:
                ReportUnsafeLogPath(archive_path, UnsafeLogPathError('The archive log path is unsafe.'))

        # delay=False の場合は、次ログ書き込みに備えて stream を再オープンする
        if self.delay is False:
            self.stream = self._open()

        # 次回ローテーション時刻を再計算する
        self.rolloverAt = self.computeRollover(current_time)

        # ローテーション完了後に期限切れアーカイブを整理する
        CleanupOldArchiveLogs(self.retention_days)


def GetArchiveFilePath(date_key: str) -> Path:
    """
    日付キー (YYYYMMDD) からアーカイブファイルのパスを生成する。

    Args:
        date_key (str): 日付キー（例: '20260212'）

    Returns:
        Path: アーカイブファイルのパス（例: logs/archives/KonomiTV-BS4K-Server.20260212.log）
    """

    # アーカイブ命名規則を 1 箇所に集約して、生成と解析の不一致を防ぐ
    ## 例: KonomiTV-BS4K-Server.log -> KonomiTV-BS4K-Server.20260212.log
    return LOGS_ARCHIVES_DIR / f'{KONOMITV_SERVER_LOG_PATH.stem}.{date_key}{KONOMITV_SERVER_LOG_PATH.suffix}'


def CleanupOldArchiveLogs(retention_days: int | None) -> None:
    """
    保存期間を超えたアーカイブログを削除する。

    Args:
        retention_days (int | None): アーカイブ保持日数。None の場合は無期限保持。
    """

    # 保持日数が無効（未設定または 0 以下）の場合は、安全のため削除処理を行わない
    if retention_days is None or retention_days < 1:
        return

    # JST 基準で削除閾値を算出する
    ## 例: retention_days=30 の場合、30 日より前の日付ファイルのみ削除する
    today_in_jst = timezone.now().date()
    delete_before_date = today_in_jst - timedelta(days=retention_days)

    # アーカイブディレクトリ自体を symlink 追跡なしで開き、相対 unlink の基準 FD とする
    try:
        archive_directory_fd = OpenSecureLogDirectory(LOGS_ARCHIVES_DIR)
    except FileNotFoundError:
        return
    except (OSError, UnsafeLogPathError) as ex:
        ReportUnsafeLogPath(LOGS_ARCHIVES_DIR, ex)
        return

    try:
        # 命名規則に一致し、安全性を確認できた regular file だけをクリーンアップする
        with os.scandir(archive_directory_fd) as entries:
            for entry in entries:
                match = ARCHIVE_FILE_PATTERN.match(entry.name)

                # 命名規則から外れるファイルは、他用途の可能性があるため触らない
                if match is None:
                    continue

                try:
                    archive_date = datetime.strptime(match.group(1), '%Y%m%d').date()
                except ValueError:
                    # 日付として解釈できないファイル名は安全のためスキップする
                    continue

                # 保存期間内のファイルは検証・削除対象外
                if archive_date >= delete_before_date:
                    continue

                try:
                    archive_stat = entry.stat(follow_symlinks=False)
                    ValidateSecureLogFileStat(archive_stat, os.geteuid())
                    os.unlink(entry.name, dir_fd=archive_directory_fd)
                # symlink / hardlink / FIFO / owner 不一致と削除失敗は拒否し、他ファイルの処理を継続する
                except (OSError, UnsafeLogPathError) as ex:
                    ReportUnsafeLogPath(LOGS_ARCHIVES_DIR / entry.name, ex)
                    continue
    finally:
        os.close(archive_directory_fd)


def SplitServerLogByDate() -> None:
    """
    起動時にサーバーログを日付単位で分割する。

    サーバーログ内の各行を、行頭の日付プレフィックスに基づいて振り分ける。
    スタックトレースなど日付を持たない行は、直前に出現した日付と同じファイルへ書き出す。
    分割の成否にかかわらず、処理の最後に期限切れアーカイブのクリーンアップを実行する。
    """

    def PrintBootstrapLog(level: Literal['INFO', 'WARNING', 'ERROR'], message: str) -> None:
        """
        ロガー初期化前のフェーズで、Uvicorn 形式に寄せたログを標準エラー出力に出力する。

        Args:
            level (Literal['INFO', 'WARNING', 'ERROR']): ログレベル
            message (str): 出力するログメッセージ
        """

        # ロガー初期化前でも既存ログに近い見た目で観測できるよう、JST タイムスタンプ付きで出力する
        now_in_jst = timezone.now()
        date_time_str = now_in_jst.strftime('%Y/%m/%d %H:%M:%S')
        timestamp = f'{date_time_str}.{now_in_jst.microsecond // 1000:03d}'
        level_prefix = f'{level.upper()}:'.ljust(10)
        print(f'[{timestamp}] {level_prefix} {message}', file=sys.stderr)

    # 分割処理で使用する一時ファイルのパス
    ## 例外時のクリーンアップで参照するため、try ブロックの外で宣言する
    today_temp_path: Path | None = None
    source_file: TextIO | None = None

    try:
        # ======== Phase 1: 分割が必要かの事前判定 ========

        # 元ログを symlink 追跡なしで開き、同じ FD の type・owner・link count を検証する
        try:
            source_file = OpenSecureLogFile(
                KONOMITV_SERVER_LOG_PATH,
                mode='r',
                encoding='utf-8',
                errors='replace',
                file_mode=None,
            )
            file_size = os.fstat(source_file.fileno()).st_size
        except FileNotFoundError:
            return
        except (OSError, UnsafeLogPathError) as ex:
            PrintBootstrapLog('ERROR', f'Failed to securely open server log: {ex}. Skip log split.')
            return

        # 空ファイルであれば分割対象は存在しない
        if file_size == 0:
            return

        # ======== Phase 2: 高速パス判定 ========

        # ログ本文に含まれる日付表記に合わせて、比較用の今日の日付文字列を JST で生成する
        today_str = timezone.now().date().strftime('%Y/%m/%d')

        # ログの先頭で最初に見つかる日付行が今日であれば、全行が今日のログとみなして分割をスキップする
        ## 通常の運用ではほとんどの起動がこのパスを通るため、全行パースのコストを回避できる
        for source_line in source_file:
            match = LOG_DATE_PATTERN.match(source_line)
            # 最初に見つかった日付行のみで判定し、今日の日付であれば分割不要と判断する
            if match is not None:
                if match.group(1) == today_str:
                    return
                break

        # 分割本体でも、検証済みの同じ FD を先頭から読み直す
        source_file.seek(0)

        # ======== Phase 3: 日付別分割の実行 ========

        PrintBootstrapLog(
            'INFO',
            f'Splitting server log by date (file size: {file_size / 1024 / 1024:.1f} MB)...',
        )

        archive_entry_counts: dict[str, int] = {}
        archived_paths: set[Path] = set()

        try:
            # 複数ファイル（入力ログ・一時ファイル・複数アーカイブ）を安全に扱うため ExitStack を使う
            with ExitStack() as exit_stack:
                # 高速判定で検証済みの同じ元ログ FD を ExitStack の管理下へ移す
                source_file = exit_stack.enter_context(source_file)

                # 今日分のログを書き出す一時ファイルを作成する
                ## 分割完了後にアトミックに本体ログへ置換するため、同じディレクトリに一時ファイルを生成する
                today_temp_file = exit_stack.enter_context(
                    tempfile.NamedTemporaryFile(
                        mode='w',
                        encoding='utf-8',
                        dir=KONOMITV_SERVER_LOG_PATH.parent,
                        prefix='.KonomiTV-BS4K-Server.today.',
                        suffix='.log.tmp',
                        delete=False,
                    ),
                )
                today_temp_path = Path(today_temp_file.name)
                ValidateSecureLogFileStat(os.fstat(today_temp_file.fileno()), os.geteuid())
                os.fchmod(today_temp_file.fileno(), LOG_FILE_MODE)

                # 日付ごとのアーカイブファイルハンドルを遅延生成するための辞書
                archive_files: dict[str, TextIO] = {}

                # current_date は「直近で観測した日付プレフィックス」を保持する
                ## スタックトレース行のような日付無し行も直前日付へ帰属できるようにする
                current_date: str | None = None

                # -------- ログ行の振り分けループ --------

                for source_line in source_file:
                    match = LOG_DATE_PATTERN.match(source_line)

                    # 日付プレフィックスを持つ行で帰属先日付を更新する
                    if match is not None:
                        current_date = match.group(1)

                    # 今日の日付の行、または日付が未確定の先頭行は今日分として扱う
                    if current_date is None or current_date == today_str:
                        today_temp_file.write(source_line)
                        continue

                    # 過去日付の行をアーカイブファイルに書き出す
                    date_key = current_date.replace('/', '')
                    archive_file = archive_files.get(date_key)

                    # 日付ごとのアーカイブファイルハンドルは初回アクセス時に遅延生成する
                    if archive_file is None:
                        archive_file_path = GetArchiveFilePath(date_key)
                        archive_file = exit_stack.enter_context(
                            OpenSecureLogFile(
                                archive_file_path,
                                mode='a',
                                encoding='utf-8',
                            ),
                        )
                        archive_files[date_key] = archive_file
                        archived_paths.add(archive_file_path)

                    # アーカイブファイルに行を書き出す
                    archive_file.write(source_line)

                    # 日付プレフィックスを持つ行のみエントリとしてカウントする
                    ## スタックトレース等の継続行はカウントに含めない
                    if match is not None:
                        archive_entry_counts[date_key] = archive_entry_counts.get(date_key, 0) + 1

            # -------- ExitStack を抜けてすべてのファイルハンドルが閉じられた後の後処理 --------

            # 分割対象がなかった場合は一時ファイルを削除して終了
            if len(archived_paths) == 0:
                if today_temp_path is not None and today_temp_path.exists() is True:
                    today_temp_path.unlink()
                return

            # 今日分の一時ファイルを本体ログへアトミックに置換する
            if today_temp_path is not None:
                log_directory_fd = OpenSecureLogDirectory(KONOMITV_SERVER_LOG_PATH.parent)
                try:
                    os.replace(
                        today_temp_path.name,
                        KONOMITV_SERVER_LOG_PATH.name,
                        src_dir_fd=log_directory_fd,
                        dst_dir_fd=log_directory_fd,
                    )
                    today_temp_path = None
                finally:
                    os.close(log_directory_fd)

            # 分割結果のサマリーをログ出力する
            archived_dates = sorted(archive_entry_counts.keys())
            if len(archived_dates) > 0:
                archived_summary = ', '.join(
                    f'{date_key} ({archive_entry_counts[date_key]} entries)' for date_key in archived_dates
                )
                PrintBootstrapLog(
                    'INFO',
                    f'Successfully split server log into {len(archived_dates)} archive(s): {archived_summary}',
                )

        # 分割全体で予期しない例外が起きた場合は、起動継続を優先してエラーのみ出力する
        ## 途中生成した一時ファイルが残ると次回処理のノイズになるため、可能な限り削除する
        except Exception as ex:
            PrintBootstrapLog('ERROR', f'Failed to split server log by date: {ex}')
            traceback.print_exc()
            if today_temp_path is not None and today_temp_path.exists() is True:
                try:
                    today_temp_path.unlink()
                except OSError:
                    pass

    # 分割の成否にかかわらず、期限切れアーカイブのクリーンアップを最後に実行する
    finally:
        if source_file is not None and source_file.closed is False:
            source_file.close()
        CleanupOldArchiveLogs(SERVER_LOG_ARCHIVE_RETENTION_DAYS)
