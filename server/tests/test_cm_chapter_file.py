from dataclasses import replace
from pathlib import Path

import pytest

from app.metadata.CMChapterFile import (
    CMChapterConflictError,
    CommitCMChapterFile,
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
    """録画とchapterのファイル名を相互に対応付ける。"""

    recorded_path = tmp_path / 'program.ts'
    chapter_path = GetCMChapterPath(recorded_path)

    assert chapter_path == tmp_path / 'program.ts.chapter.txt'
    assert GetLegacyCMChapterPath(recorded_path) == tmp_path / 'program.chapter.txt'
    assert GetRecordedPathFromCMChapterPath(chapter_path, {'.ts', '.m2ts'}) == (
        recorded_path,
        tmp_path / 'program.ts.m2ts',
        tmp_path / 'program.ts.ts',
    )
    legacy_path = GetLegacyCMChapterPath(recorded_path)
    assert GetRecordedPathFromCMChapterPath(legacy_path, {'.ts', '.m2ts'}) == (
        tmp_path / 'program.m2ts',
        tmp_path / 'program.ts',
    )
    assert GetRecordedPathFromCMChapterPath(tmp_path / 'other.txt', {'.ts'}) == ()


def test_select_recorded_path_for_chapter_prefers_registered_canonical(tmp_path: Path) -> None:
    """名前上legacyとも解釈できても、登録済みcanonical録画を優先する。"""

    chapter_path = tmp_path / 'program.ts.chapter.txt'
    selection = SelectRecordedPathForCMChapter(
        chapter_path,
        [tmp_path / 'program.ts', tmp_path / 'program.ts.mkv'],
        {'.ts', '.mkv'},
    )

    assert selection is not None
    assert selection.recorded_file_path == tmp_path / 'program.ts'
    assert selection.kind == 'Canonical'


def test_select_recorded_path_for_dotted_legacy_name(tmp_path: Path) -> None:
    """stem末尾が録画拡張子に見えるlegacy名も、DB候補が一意なら復元する。"""

    selection = SelectRecordedPathForCMChapter(
        tmp_path / 'program.ts.chapter.txt',
        [tmp_path / 'program.ts.mkv'],
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


def test_select_chapter_path_prefers_canonical(tmp_path: Path) -> None:
    """legacyが存在してもcanonicalがあれば必ずcanonicalを採用する。"""

    recorded_path = tmp_path / 'program.ts'
    canonical_path = GetCMChapterPath(recorded_path)
    legacy_path = GetLegacyCMChapterPath(recorded_path)
    canonical_path.touch()
    legacy_path.touch()

    selection = SelectCMChapterPath(recorded_path, [recorded_path])

    assert selection.path == canonical_path
    assert selection.kind == 'Canonical'


def test_select_chapter_path_uses_unique_legacy_fallback(tmp_path: Path) -> None:
    """同一stemの登録録画が1件なら既存legacy chapterを互換読込する。"""

    recorded_path = tmp_path / 'program.ts'
    legacy_path = GetLegacyCMChapterPath(recorded_path)
    legacy_path.touch()

    selection = SelectCMChapterPath(recorded_path, [recorded_path])

    assert selection.path == legacy_path
    assert selection.kind == 'Legacy'


def test_select_chapter_path_rejects_ambiguous_legacy_fallback(tmp_path: Path) -> None:
    """同一stemの複数録画があればlegacy chapterをどちらにも割り当てない。"""

    recorded_path = tmp_path / 'program.ts'
    GetLegacyCMChapterPath(recorded_path).touch()

    selection = SelectCMChapterPath(recorded_path, [recorded_path, tmp_path / 'program.mkv'])

    assert selection.path == GetCMChapterPath(recorded_path)
    assert selection.kind == 'Canonical'


def test_select_chapter_path_never_uses_another_recordings_canonical_as_legacy(tmp_path: Path) -> None:
    """拡張子を含むstemが衝突しても、別録画のcanonical chapterを横取りしない。"""

    canonical_owner = tmp_path / 'program.ts'
    recorded_path = tmp_path / 'program.ts.mkv'
    GetCMChapterPath(canonical_owner).touch()

    selection = SelectCMChapterPath(recorded_path, [canonical_owner, recorded_path])

    assert selection.path == GetCMChapterPath(recorded_path)
    assert selection.kind == 'Canonical'


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
    assert len(str(result.fingerprint['sha256'])) == 64


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
        ('CHAPTER01=00:00:00.000\nCHAPTER02NAME=A\n', 60.0, 'MismatchedNumber'),
        (
            'CHAPTER01=00:00:10.000\nCHAPTER01NAME=A\nCHAPTER02=00:00:09.000\nCHAPTER02NAME=B\n',
            60.0,
            'NonIncreasingTimestamp',
        ),
        ('CHAPTER01=00:01:01.000\nCHAPTER01NAME=A\n', 60.0, 'TimestampOutOfRange'),
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


def test_commit_chapter_atomically(tmp_path: Path) -> None:
    """作業chapterを確定先へ配置して配置後も再検証する。"""

    source_path = tmp_path / 'work' / 'result.chapter.txt'
    source_path.parent.mkdir()
    WriteChapter(source_path, [(1, '00:00:00.000', 'CM'), (2, '00:00:30.000', 'A')])
    destination_path = tmp_path / 'recordings' / 'program.chapter.txt'

    result = CommitCMChapterFile(source_path, destination_path, 60.0)

    assert result.status == 'Valid'
    assert destination_path.read_bytes() == source_path.read_bytes()
    assert destination_path.stat().st_mode & 0o777 == 0o644
    assert list(destination_path.parent.glob('.*.tmp')) == []


def test_invalid_generated_chapter_does_not_replace_existing(tmp_path: Path) -> None:
    """作業chapterが壊れていれば既存chapterを変更しない。"""

    source_path = tmp_path / 'invalid.chapter.txt'
    source_path.write_text('broken\n', encoding='utf-8')
    destination_path = tmp_path / 'program.chapter.txt'
    destination_path.write_text('original\n', encoding='utf-8')

    with pytest.raises(ValueError):
        CommitCMChapterFile(source_path, destination_path, 60.0)

    assert destination_path.read_text(encoding='utf-8') == 'original\n'


def test_commit_does_not_clobber_chapter_created_during_analysis(tmp_path: Path) -> None:
    """解析開始時に欠落していた確定先が作られた場合はno-clobberで中止する。"""

    source_path = tmp_path / 'generated.chapter.txt'
    destination_path = tmp_path / 'program.chapter.txt'
    WriteChapter(source_path, [(1, '00:00:00.000', 'A')])
    WriteChapter(destination_path, [(1, '00:00:00.000', 'External')])
    original = destination_path.read_bytes()

    with pytest.raises(CMChapterConflictError):
        CommitCMChapterFile(source_path, destination_path, 60.0, {'exists': False})

    assert destination_path.read_bytes() == original


def test_commit_rejects_valid_but_different_content_observed_after_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置直後に別の正常chapterへ差し替えられてもGeneratedとして返さない。"""

    import app.metadata.CMChapterFile as chapter_module

    source_path = tmp_path / 'generated.chapter.txt'
    destination_path = tmp_path / 'program.chapter.txt'
    WriteChapter(source_path, [(1, '00:00:00.000', 'A')])
    original_read = chapter_module.ReadCMChapterFile

    def ReadWithConcurrentReplacement(path: Path, duration: float):  # type: ignore[no-untyped-def]
        result = original_read(path, duration)
        if path == destination_path and result.status in ('Valid', 'ValidNoCM'):
            return replace(result, fingerprint={**result.fingerprint, 'sha256': 'external'})
        return result

    monkeypatch.setattr(chapter_module, 'ReadCMChapterFile', ReadWithConcurrentReplacement)

    with pytest.raises(CMChapterConflictError):
        CommitCMChapterFile(source_path, destination_path, 60.0)


def test_commit_rejects_existing_chapter_changed_after_fingerprint(tmp_path: Path) -> None:
    """既存chapterの内容が解析中に変わった場合は生成結果を公開しない。"""

    source_path = tmp_path / 'generated.chapter.txt'
    destination_path = tmp_path / 'program.chapter.txt'
    WriteChapter(source_path, [(1, '00:00:00.000', 'Generated')])
    WriteChapter(destination_path, [(1, '00:00:00.000', 'Old')])
    expected = ReadCMChapterFile(destination_path, 60.0).fingerprint
    WriteChapter(destination_path, [(1, '00:00:00.000', 'External')])
    original = destination_path.read_bytes()

    with pytest.raises(CMChapterConflictError):
        CommitCMChapterFile(source_path, destination_path, 60.0, expected)

    assert destination_path.read_bytes() == original
