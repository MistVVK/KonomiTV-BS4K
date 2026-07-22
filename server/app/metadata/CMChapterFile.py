from __future__ import annotations

import asyncio
import hashlib
import math
import re
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


def GetCMChapterPath(recorded_file_path: Path) -> Path:
    """録画ファイルに対応する基本名方式の外部.chapter.txtパスを返す。

    Args:
        recorded_file_path: 元の録画ファイルパス。

    Returns:
        同じディレクトリにあるchapterファイルパス。
    """

    return recorded_file_path.with_name(f'{recorded_file_path.stem}.chapter.txt')


def GetLegacyCMChapterPath(recorded_file_path: Path) -> Path:
    """基本名方式のchapterパスを返す互換API。"""

    return GetCMChapterPath(recorded_file_path)


def SelectCMChapterPath(
    recorded_file_path: Path,
    registered_recorded_file_paths: Iterable[Path],
) -> CMChapterPathSelection | None:
    """基本名方式の外部chapterを、対応する登録録画が一意な場合だけ選択する。

    基本名方式は ``foo.ts`` と ``foo.mkv`` のような同一stem録画を区別できない。
    そのため、同じディレクトリ・stemを持つ登録済み録画が対象録画だけで、かつ
    chapterファイルが存在する場合に限り外部入力として採用する。
    """

    chapter_path = GetCMChapterPath(recorded_file_path)
    if chapter_path.is_file() is False:
        return None
    registered_paths = set(registered_recorded_file_paths)
    owners = {
        candidate
        for candidate in registered_paths
        if (
            candidate.parent == recorded_file_path.parent
            and candidate.stem.casefold() == recorded_file_path.stem.casefold()
        )
    }
    if owners != {recorded_file_path}:
        return None
    return CMChapterPathSelection(chapter_path, 'Legacy')


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
    candidates = [
        chapter_file_path.with_name(f'{base_name}{extension}')
        for extension in sorted(candidate_extensions)
    ]
    # ``foo.ts.chapter.txt`` は ``foo.ts`` 自体には対応しない。基本名が ``foo.ts`` の
    # ``foo.ts.mkv`` などにだけ対応する。重複だけを順序を維持して除去する。
    return tuple(dict.fromkeys(candidates))


def SelectRecordedPathForCMChapter(
    chapter_file_path: Path,
    registered_recorded_file_paths: Iterable[Path],
    candidate_extensions: set[str],
) -> CMChapterRecordedPathSelection | None:
    """基本名方式のchapterイベントを、一意な登録録画へ対応付ける。"""

    suffix = '.chapter.txt'
    if chapter_file_path.name.lower().endswith(suffix) is False:
        return None
    registered_paths = set(registered_recorded_file_paths)
    base_path = chapter_file_path.with_name(chapter_file_path.name[:-len(suffix)])
    normalized_extensions = {extension.lower() for extension in candidate_extensions}
    candidates = {
        candidate
        for candidate in registered_paths
        if (
            candidate.parent == chapter_file_path.parent
            and candidate.stem.casefold() == base_path.name.casefold()
            and candidate.suffix.lower() in normalized_extensions
        )
    }
    if len(candidates) != 1:
        return None
    return CMChapterRecordedPathSelection(candidates.pop(), 'Legacy')


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
