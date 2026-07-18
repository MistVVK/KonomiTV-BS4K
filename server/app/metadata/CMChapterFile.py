from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.schemas import CMSection


CMChapterReadStatus = Literal['Valid', 'ValidNoCM', 'Missing', 'Invalid', 'IOError']
CMChapterPathKind = Literal['Canonical', 'Legacy']
ChapterFingerprint = dict[str, int | str | bool]

_CHAPTER_TIME_PATTERN = re.compile(
    r'^CHAPTER(?P<number>\d+)=(?P<hours>\d+):(?P<minutes>\d{2}):(?P<seconds>\d{2}(?:\.\d+)?)$',
)
_CHAPTER_NAME_PATTERN = re.compile(r'^CHAPTER(?P<number>\d+)NAME=(?P<name>.*)$')


@dataclass(frozen=True, slots=True)
class CMChapterReadResult:
    """chapterファイルの読込状態、検証結果、CM区間をまとめる。"""

    status: CMChapterReadStatus
    sections: tuple[CMSection, ...]
    fingerprint: ChapterFingerprint
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class CMChapterPathSelection:
    """録画に対して採用するchapterパスと命名方式を表す。"""

    path: Path
    kind: CMChapterPathKind


@dataclass(frozen=True, slots=True)
class CMChapterRecordedPathSelection:
    """chapterイベントに対応付けられた録画パスと命名方式を表す。"""

    recorded_file_path: Path
    kind: CMChapterPathKind


class CMChapterConflictError(ValueError):
    """解析中に確定先chapterが変更され、原子的な公開を中止したことを表す。"""


def GetCMChapterPath(recorded_file_path: Path) -> Path:
    """録画ファイルに対応するcanonicalな.chapter.txtのパスを返す。

    Args:
        recorded_file_path: 元の録画ファイルパス。

    Returns:
        同じディレクトリにあるchapterファイルパス。
    """

    return recorded_file_path.with_name(f'{recorded_file_path.name}.chapter.txt')


def GetLegacyCMChapterPath(recorded_file_path: Path) -> Path:
    """旧実装や外部ツールが使用するstem基準のchapterパスを返す。"""

    return recorded_file_path.with_name(f'{recorded_file_path.stem}.chapter.txt')


def SelectCMChapterPath(
    recorded_file_path: Path,
    registered_recorded_file_paths: Iterable[Path],
) -> CMChapterPathSelection:
    """canonicalを優先し、安全な場合に限りlegacy chapterへフォールバックする。

    legacy名は ``foo.ts`` と ``foo.mkv`` のような同一stem録画を区別できない。
    そのため、同じディレクトリ・stemを持つ登録済み録画が対象録画だけの場合に限り採用する。
    chapterがまだ存在しない場合は、今後の生成先であるcanonicalパスを返す。
    """

    canonical_path = GetCMChapterPath(recorded_file_path)
    if canonical_path.is_file():
        return CMChapterPathSelection(canonical_path, 'Canonical')

    legacy_path = GetLegacyCMChapterPath(recorded_file_path)
    if legacy_path.is_file() is False:
        return CMChapterPathSelection(canonical_path, 'Canonical')

    registered_paths = set(registered_recorded_file_paths)
    # ``program.ts.chapter.txt`` は ``program.ts`` のcanonical名であると同時に、
    # ``program.ts.mkv`` のlegacy名にも見える。別録画のcanonicalをlegacyとして横取りしない。
    if any(
        candidate != recorded_file_path and GetCMChapterPath(candidate) == legacy_path
        for candidate in registered_paths
    ):
        return CMChapterPathSelection(canonical_path, 'Canonical')

    legacy_owners = {
        candidate
        for candidate in registered_paths
        if candidate.parent == recorded_file_path.parent and candidate.stem == recorded_file_path.stem
    }
    if legacy_owners == {recorded_file_path}:
        return CMChapterPathSelection(legacy_path, 'Legacy')
    return CMChapterPathSelection(canonical_path, 'Canonical')


def GetRecordedPathFromCMChapterPath(chapter_file_path: Path, candidate_extensions: set[str]) -> tuple[Path, ...]:
    """chapterファイル名から存在しうる元録画パスを列挙する。

    Args:
        chapter_file_path: 追加・変更・削除されたchapterファイル。
        candidate_extensions: 録画スキャン対象の拡張子。

    Returns:
        拡張子ごとの元録画候補。ファイル存在確認は呼び出し側で行う。
    """

    suffix = '.chapter.txt'
    if chapter_file_path.name.lower().endswith(suffix) is False:
        return ()
    base_name = chapter_file_path.name[:-len(suffix)]
    candidates: list[Path] = []
    canonical_candidate = chapter_file_path.with_name(base_name)
    normalized_extensions = {extension.lower() for extension in candidate_extensions}
    if canonical_candidate.suffix.lower() in normalized_extensions:
        candidates.append(canonical_candidate)
    candidates.extend(
        chapter_file_path.with_name(f'{base_name}{extension}')
        for extension in sorted(candidate_extensions)
    )
    # ``foo.ts.chapter.txt`` はcanonical名のほか、stemが ``foo.ts`` の録画に対する
    # legacy名でもあり得るため、両方の候補を残す。重複だけを順序を維持して除去する。
    return tuple(dict.fromkeys(candidates))


def SelectRecordedPathForCMChapter(
    chapter_file_path: Path,
    registered_recorded_file_paths: Iterable[Path],
    candidate_extensions: set[str],
) -> CMChapterRecordedPathSelection | None:
    """chapterイベントをcanonical優先・legacy一意制約で登録録画へ対応付ける。"""

    suffix = '.chapter.txt'
    if chapter_file_path.name.lower().endswith(suffix) is False:
        return None
    registered_paths = set(registered_recorded_file_paths)
    base_path = chapter_file_path.with_name(chapter_file_path.name[:-len(suffix)])
    normalized_extensions = {extension.lower() for extension in candidate_extensions}
    if base_path.suffix.lower() in normalized_extensions and base_path in registered_paths:
        return CMChapterRecordedPathSelection(base_path, 'Canonical')

    legacy_candidates = {
        candidate
        for candidate in registered_paths
        if (
            candidate.parent == chapter_file_path.parent
            and candidate.stem.casefold() == base_path.name.casefold()
            and candidate.suffix.lower() in normalized_extensions
        )
    }
    if len(legacy_candidates) != 1:
        return None
    return CMChapterRecordedPathSelection(legacy_candidates.pop(), 'Legacy')


def ReadCMChapterFile(chapter_file_path: Path, duration_sec: float) -> CMChapterReadResult:
    """chapterファイルを厳格に検証してCM区間へ変換する。

    Args:
        chapter_file_path: 読み込むchapterファイル。
        duration_sec: 対応する録画の秒数。

    Returns:
        読込状態とCM区間。壊れたファイルは変更しない。
    """

    missing_fingerprint: ChapterFingerprint = {'exists': False}
    try:
        stat_before = chapter_file_path.stat()
    except FileNotFoundError:
        return CMChapterReadResult('Missing', (), missing_fingerprint)
    except OSError as ex:
        return CMChapterReadResult('IOError', (), missing_fingerprint, type(ex).__name__, str(ex))

    try:
        raw = chapter_file_path.read_bytes()
        stat_after = chapter_file_path.stat()
    except FileNotFoundError:
        return CMChapterReadResult('Missing', (), missing_fingerprint)
    except OSError as ex:
        fingerprint: ChapterFingerprint = {
            'exists': True,
            'size': stat_before.st_size,
            'mtime_ns': stat_before.st_mtime_ns,
            'device': stat_before.st_dev,
            'inode': stat_before.st_ino,
        }
        return CMChapterReadResult('IOError', (), fingerprint, type(ex).__name__, str(ex))

    fingerprint = {
        'exists': True,
        'size': stat_after.st_size,
        'mtime_ns': stat_after.st_mtime_ns,
        'device': stat_after.st_dev,
        'inode': stat_after.st_ino,
        'sha256': hashlib.sha256(raw).hexdigest(),
    }
    if (
        stat_before.st_size != stat_after.st_size
        or stat_before.st_mtime_ns != stat_after.st_mtime_ns
        or stat_before.st_dev != stat_after.st_dev
        or stat_before.st_ino != stat_after.st_ino
    ):
        return CMChapterReadResult(
            'IOError',
            (),
            fingerprint,
            'ChapterChangedDuringRead',
            'The chapter file changed while it was being read.',
        )

    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as ex:
        return CMChapterReadResult('Invalid', (), fingerprint, 'InvalidEncoding', str(ex))

    if math.isfinite(duration_sec) is False or duration_sec < 0:
        return CMChapterReadResult('Invalid', (), fingerprint, 'InvalidDuration', 'Recording duration is invalid.')

    lines = text.splitlines()
    if len(lines) == 0:
        return CMChapterReadResult('Invalid', (), fingerprint, 'EmptyChapter', 'The chapter file is empty.')
    if len(lines) % 2 != 0:
        return CMChapterReadResult('Invalid', (), fingerprint, 'OddLineCount', 'The chapter file has an odd number of lines.')

    chapters: list[tuple[int, str, float]] = []
    for line_index in range(0, len(lines), 2):
        time_match = _CHAPTER_TIME_PATTERN.fullmatch(lines[line_index].strip())
        name_match = _CHAPTER_NAME_PATTERN.fullmatch(lines[line_index + 1].strip())
        if time_match is None or name_match is None:
            return CMChapterReadResult(
                'Invalid',
                (),
                fingerprint,
                'InvalidFormat',
                f'Invalid chapter record at lines {line_index + 1}-{line_index + 2}.',
            )

        chapter_number = int(time_match.group('number'))
        name_number = int(name_match.group('number'))
        if chapter_number != name_number:
            return CMChapterReadResult('Invalid', (), fingerprint, 'MismatchedNumber', 'Chapter numbers do not match.')
        if chapters and chapter_number != chapters[-1][0] + 1:
            return CMChapterReadResult('Invalid', (), fingerprint, 'NonSequentialNumber', 'Chapter numbers are not sequential.')

        minutes = int(time_match.group('minutes'))
        seconds = float(time_match.group('seconds'))
        if minutes >= 60 or seconds >= 60 or math.isfinite(seconds) is False:
            return CMChapterReadResult('Invalid', (), fingerprint, 'InvalidTimestamp', 'Chapter timestamp is invalid.')
        timestamp = int(time_match.group('hours')) * 3600 + minutes * 60 + seconds
        if timestamp < 0 or timestamp > duration_sec:
            return CMChapterReadResult('Invalid', (), fingerprint, 'TimestampOutOfRange', 'Chapter timestamp is outside the recording.')
        if chapters and timestamp <= chapters[-1][2]:
            return CMChapterReadResult('Invalid', (), fingerprint, 'NonIncreasingTimestamp', 'Chapter timestamps are not increasing.')

        chapters.append((chapter_number, name_match.group('name'), timestamp))

    sections: list[CMSection] = []
    cm_start: float | None = None
    for _, chapter_name, timestamp in chapters:
        if chapter_name.startswith('CM') and cm_start is None:
            cm_start = timestamp
        elif chapter_name.startswith('CM') is False and cm_start is not None:
            sections.append(CMSection(start_time=cm_start, end_time=timestamp))
            cm_start = None
    if cm_start is not None and cm_start < duration_sec:
        sections.append(CMSection(start_time=cm_start, end_time=duration_sec))

    status: CMChapterReadStatus = 'Valid' if sections else 'ValidNoCM'
    return CMChapterReadResult(status, tuple(sections), fingerprint)


async def ReadCMChapterFileAsync(chapter_file_path: Path, duration_sec: float) -> CMChapterReadResult:
    """chapterファイルの同期I/Oとハッシュ計算をワーカースレッドで実行する。

    Args:
        chapter_file_path: 読み込むchapterファイル。
        duration_sec: 対応する録画の秒数。

    Returns:
        読込状態とCM区間。
    """

    return await asyncio.to_thread(ReadCMChapterFile, chapter_file_path, duration_sec)


def CommitCMChapterFile(
    source_path: Path,
    destination_path: Path,
    duration_sec: float,
    expected_destination_fingerprint: ChapterFingerprint | None = None,
) -> CMChapterReadResult:
    """検証済みchapterを録画横へ原子的に配置し、配置後に再検証する。

    Args:
        source_path: 解析器が作業フォルダへ生成したchapter。
        destination_path: 録画横の確定先chapter。
        duration_sec: 対応する録画の秒数。

    Returns:
        配置後のchapter読込結果。

    Raises:
        ValueError: 作業フォルダのchapterが正常でない場合。
        OSError: 確定先へ安全に書き込めない場合。
    """

    source_result = ReadCMChapterFile(source_path, duration_sec)
    if source_result.status not in ('Valid', 'ValidNoCM'):
        raise ValueError(f'Generated chapter is not valid: {source_result.error_code}')
    content = source_path.read_bytes()

    def CheckDestinationFingerprint() -> None:
        if expected_destination_fingerprint is None:
            return
        current = ReadCMChapterFile(destination_path, duration_sec)
        if current.fingerprint != expected_destination_fingerprint:
            raise CMChapterConflictError('The destination chapter changed during CM analysis.')

    CheckDestinationFingerprint()

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f'.{destination_path.name}.',
        suffix='.tmp',
        dir=destination_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(temporary_fd, 'wb') as temporary_file:
            os.fchmod(temporary_file.fileno(), 0o644)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        # 欠落状態からの公開はhard linkでno-clobberにし、解析中に作成された外部chapterを
        # 競合窓なしで保護する。既存の自前chapter置換もrename直前に再度CAS確認する。
        CheckDestinationFingerprint()
        if expected_destination_fingerprint is not None and expected_destination_fingerprint.get('exists') is False:
            try:
                os.link(temporary_path, destination_path)
            except FileExistsError as ex:
                raise CMChapterConflictError('The destination chapter was created during CM analysis.') from ex
        else:
            os.replace(temporary_path, destination_path)

        # Linuxのローカルファイルシステムではrename自体の永続化にも親ディレクトリのfsyncが必要。
        # SMB/NFSなど未対応のファイルシステムでは失敗しうるため、その場合も成功として握りつぶさない。
        parent_fd = os.open(destination_path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        temporary_path.unlink(missing_ok=True)

    committed_result = ReadCMChapterFile(destination_path, duration_sec)
    if committed_result.status not in ('Valid', 'ValidNoCM'):
        raise OSError(f'Committed chapter validation failed: {committed_result.error_code}')
    if committed_result.fingerprint.get('sha256') != source_result.fingerprint.get('sha256'):
        raise CMChapterConflictError('The destination chapter changed immediately after CM analysis commit.')
    return committed_result


async def CommitCMChapterFileAsync(
    source_path: Path,
    destination_path: Path,
    duration_sec: float,
    expected_destination_fingerprint: ChapterFingerprint | None = None,
) -> CMChapterReadResult:
    """chapter確定処理をワーカースレッドで実行する。

    Args:
        source_path: 作業フォルダのchapter。
        destination_path: 録画横の確定先。
        duration_sec: 対応する録画の秒数。

    Returns:
        配置後のchapter読込結果。
    """

    return await asyncio.to_thread(
        CommitCMChapterFile,
        source_path,
        destination_path,
        duration_sec,
        expected_destination_fingerprint,
    )
