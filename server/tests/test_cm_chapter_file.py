import hashlib
from pathlib import Path

import pytest

from app.metadata.CMChapterFile import (
    GetCMChapterPath,
    GetLegacyCMChapterPath,
    GetRecordedPathFromCMChapterPath,
    ReadCMChapterFile,
    SelectCMChapterPath,
    SelectRecordedPathForCMChapter,
)


def WriteChapter(path: Path, records: list[tuple[int, str, str]]) -> None:
    """テスト用chapterを書き込む。"""

    lines: list[str] = []
    for number, timestamp, name in records:
        lines.extend((f'CHAPTER{number:02d}={timestamp}', f'CHAPTER{number:02d}NAME={name}'))
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def test_get_chapter_path_and_reverse_candidates(tmp_path: Path) -> None:
    """基本名方式だけで録画とchapterのファイル名を相互に対応付ける。"""

    recorded_path = tmp_path / 'program.ts'
    chapter_path = GetCMChapterPath(recorded_path)

    assert chapter_path == tmp_path / 'program.chapter.txt'
    assert GetLegacyCMChapterPath(recorded_path) == tmp_path / 'program.chapter.txt'
    assert GetRecordedPathFromCMChapterPath(tmp_path / 'program.ts.chapter.txt', {'.ts', '.m2ts'}) == (
        tmp_path / 'program.ts.m2ts',
        tmp_path / 'program.ts.ts',
    )
    assert GetRecordedPathFromCMChapterPath(chapter_path, {'.ts', '.m2ts'}) == (
        tmp_path / 'program.m2ts',
        tmp_path / 'program.ts',
    )
    assert GetRecordedPathFromCMChapterPath(tmp_path / 'other.txt', {'.ts'}) == ()


def test_select_recorded_path_never_treats_filename_based_path_as_canonical(tmp_path: Path) -> None:
    """program.ts.chapter.txtをprogram.ts自身のsidecarとは解釈しない。"""

    chapter_path = tmp_path / 'program.ts.chapter.txt'
    selection = SelectRecordedPathForCMChapter(
        chapter_path,
        [tmp_path / 'program.ts'],
        {'.ts', '.mkv'},
    )

    assert selection is None


def test_select_recorded_path_for_dotted_legacy_name(tmp_path: Path) -> None:
    """stem末尾が録画拡張子に見えるlegacy名も、DB候補が一意なら復元する。"""

    selection = SelectRecordedPathForCMChapter(
        tmp_path / 'program.ts.chapter.txt',
        [tmp_path / 'program.ts', tmp_path / 'program.ts.mkv'],
        {'.ts', '.mkv'},
    )

    assert selection is not None
    assert selection.recorded_file_path == tmp_path / 'program.ts.mkv'
    assert selection.kind == 'Legacy'


def test_select_recorded_path_rejects_ambiguous_legacy_name(tmp_path: Path) -> None:
    """canonical録画がなくlegacy候補が複数ならwatcherは同期対象を選ばない。"""

    selection = SelectRecordedPathForCMChapter(
        tmp_path / 'program.chapter.txt',
        [tmp_path / 'program.ts', tmp_path / 'program.mkv'],
        {'.ts', '.mkv'},
    )

    assert selection is None


def test_select_recorded_path_accepts_uppercase_legacy_extension(tmp_path: Path) -> None:
    """DB保存パスの拡張子表記を保持したままlegacy chapterへ対応付ける。"""

    recorded_path = tmp_path / 'program.TS'
    selection = SelectRecordedPathForCMChapter(
        tmp_path / 'program.chapter.txt',
        [recorded_path],
        {'.ts', '.mkv'},
    )

    assert selection is not None
    assert selection.recorded_file_path == recorded_path
    assert selection.kind == 'Legacy'


def test_select_chapter_path_ignores_filename_based_path(tmp_path: Path) -> None:
    """録画ファイル名方式のchapterが存在しても外部入力として採用しない。"""

    recorded_path = tmp_path / 'program.ts'
    (tmp_path / 'program.ts.chapter.txt').touch()

    selection = SelectCMChapterPath(recorded_path, [recorded_path])

    assert selection is None


def test_select_chapter_path_uses_unique_basic_name(tmp_path: Path) -> None:
    """同一stemの登録録画が1件なら基本名方式の外部chapterを採用する。"""

    recorded_path = tmp_path / 'program.ts'
    legacy_path = GetLegacyCMChapterPath(recorded_path)
    legacy_path.touch()

    selection = SelectCMChapterPath(recorded_path, [recorded_path])

    assert selection is not None
    assert selection.path == legacy_path
    assert selection.kind == 'Legacy'


def test_select_chapter_path_rejects_ambiguous_legacy_fallback(tmp_path: Path) -> None:
    """同一stemの複数録画があればlegacy chapterをどちらにも割り当てない。"""

    recorded_path = tmp_path / 'program.ts'
    GetLegacyCMChapterPath(recorded_path).touch()

    selection = SelectCMChapterPath(recorded_path, [recorded_path, tmp_path / 'program.mkv'])

    assert selection is None


def test_select_chapter_path_rejects_missing_basic_name(tmp_path: Path) -> None:
    """基本名方式の外部chapterがなければ選択結果を返さない。"""

    recorded_path = tmp_path / 'program.ts'

    assert SelectCMChapterPath(recorded_path, [recorded_path]) is None


def test_read_valid_chapter_with_cm(tmp_path: Path) -> None:
    """CM名のchapter境界を重複しないCM区間へ変換する。"""

    chapter_path = tmp_path / 'program.chapter.txt'
    WriteChapter(chapter_path, [
        (1, '00:00:00.000', 'A'),
        (2, '00:01:00.000', 'CM'),
        (3, '00:01:30.000', 'B'),
        (4, '00:02:00.000', 'CM?'),
    ])

    result = ReadCMChapterFile(chapter_path, 150.0)

    assert result.status == 'Valid'
    assert result.sections == (
        {'start_time': 60.0, 'end_time': 90.0},
        {'start_time': 120.0, 'end_time': 150.0},
    )
    assert result.fingerprint['exists'] is True
    assert result.fingerprint['size'] == chapter_path.stat().st_size
    assert result.fingerprint['mtime_ns'] == chapter_path.stat().st_mtime_ns
    assert result.fingerprint['sha256'] == hashlib.sha256(chapter_path.read_bytes()).hexdigest()


def test_read_valid_chapter_without_cm(tmp_path: Path) -> None:
    """正常なchapterにCM名がなければ正常な0件として返す。"""

    chapter_path = tmp_path / 'program.chapter.txt'
    WriteChapter(chapter_path, [(1, '00:00:00.000', 'A'), (2, '00:01:00.000', 'B')])

    result = ReadCMChapterFile(chapter_path, 120.0)

    assert result.status == 'ValidNoCM'
    assert result.sections == ()


@pytest.mark.parametrize(
    ('content', 'duration', 'error_code'),
    [
        ('', 60.0, 'EmptyChapter'),
        ('CHAPTER01=00:00:00.000\n', 60.0, 'OddLineCount'),
        ('CHAPTER01 00:00:00.000\nCHAPTER01NAME=A\n', 60.0, 'InvalidFormat'),
        ('CHAPTER01=00:00:00.000\nCHAPTER02NAME=A\n', 60.0, 'MismatchedNumber'),
        (
            'CHAPTER01=00:00:00.000\nCHAPTER01NAME=A\nCHAPTER03=00:00:10.000\nCHAPTER03NAME=B\n',
            60.0,
            'NonSequentialNumber',
        ),
        ('CHAPTER01=00:60:00.000\nCHAPTER01NAME=A\n', 3601.0, 'InvalidTimestamp'),
        ('CHAPTER01=00:00:60.000\nCHAPTER01NAME=A\n', 61.0, 'InvalidTimestamp'),
        (
            'CHAPTER01=00:00:10.000\nCHAPTER01NAME=A\nCHAPTER02=00:00:09.000\nCHAPTER02NAME=B\n',
            60.0,
            'NonIncreasingTimestamp',
        ),
        ('CHAPTER01=00:01:01.000\nCHAPTER01NAME=A\n', 60.0, 'TimestampOutOfRange'),
        ('CHAPTER01=00:00:00.000\nCHAPTER01NAME=A\n', float('nan'), 'InvalidDuration'),
        ('CHAPTER01=00:00:00.000\nCHAPTER01NAME=A\n', -1.0, 'InvalidDuration'),
    ],
)
def test_reject_invalid_chapter(tmp_path: Path, content: str, duration: float, error_code: str) -> None:
    """壊れたchapterを部分採用せずInvalidとして返す。"""

    chapter_path = tmp_path / 'program.chapter.txt'
    chapter_path.write_text(content, encoding='utf-8')

    result = ReadCMChapterFile(chapter_path, duration)

    assert result.status == 'Invalid'
    assert result.error_code == error_code


def test_missing_chapter(tmp_path: Path) -> None:
    """存在しないchapterを失敗や正常0件と区別する。"""

    result = ReadCMChapterFile(tmp_path / 'missing.chapter.txt', 60.0)

    assert result.status == 'Missing'
    assert result.fingerprint == {'exists': False}


def test_reject_non_utf8_chapter_with_content_fingerprint(tmp_path: Path) -> None:
    """UTF-8でない外部chapterを拒否しつつ、調査用fingerprintを保持する。"""

    chapter_path = tmp_path / 'program.chapter.txt'
    chapter_path.write_bytes(b'\xff\xfe')

    result = ReadCMChapterFile(chapter_path, 60.0)

    assert result.status == 'Invalid'
    assert result.error_code == 'InvalidEncoding'
    assert result.fingerprint['exists'] is True
    assert result.fingerprint['sha256'] == hashlib.sha256(b'\xff\xfe').hexdigest()
