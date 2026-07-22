import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.metadata.KonomiTVBS4KChapterFile import (
    BuildKonomiTVBS4KChapterFile,
    CommitKonomiTVBS4KChapterFile,
    CommitKonomiTVBS4KChapterFileAsync,
    GetKonomiTVBS4KChapterPath,
    GetRecordedPathFromKonomiTVBS4KChapterPath,
    KonomiTVBS4KChapterConflictError,
    ReadKonomiTVBS4KChapterFile,
    ReadKonomiTVBS4KChapterFileAsync,
)


INPUT_FINGERPRINT = {
    'size': 123_456,
    'mtime_ns': 1_750_000_000_000_000_000,
    'sample_sha256': 'a' * 64,
}
GENERATED_AT = datetime(2026, 7, 22, 15, 0, tzinfo=UTC)


def WriteManualYAML(path: Path, chapters: str) -> None:
    """テスト用の手書きKonomiTV-BS4K chapter YAMLを書き込む。"""

    path.write_text(
        'schema: konomitv-bs4k/chapters/v1\n'
        'chapters:\n'
        f'{chapters}',
        encoding='utf-8',
    )


def BuildGeneratedFile(path: Path, duration_sec: float = 60.0) -> None:
    """テスト用のKonomiTV-BS4K生成YAMLを書き込む。"""

    path.write_bytes(
        BuildKonomiTVBS4KChapterFile(
            [
                {'start_time': 10.0, 'end_time': 20.0},
                {'start_time': 30.0, 'end_time': 40.0},
            ],
            duration_sec,
            application_version='1.0.0',
            pipeline_version='cm-7',
            generated_at=GENERATED_AT,
            input_fingerprint=INPUT_FINGERPRINT,
        ),
    )


def test_get_path_and_reverse_path_are_unambiguous(tmp_path: Path) -> None:
    """録画名を拡張子ごと保持した専用suffixで相互変換する。"""

    recorded_path = tmp_path / 'program.name.ts'
    chapter_path = GetKonomiTVBS4KChapterPath(recorded_path)

    assert chapter_path == tmp_path / 'program.name.ts.konomitv-bs4k-chapters.yaml'
    assert GetRecordedPathFromKonomiTVBS4KChapterPath(chapter_path, {'.ts', '.mkv'}) == recorded_path
    assert GetRecordedPathFromKonomiTVBS4KChapterPath(tmp_path / 'program.konomitv-bs4k-chapters.yaml', {'.ts'}) is None
    assert GetRecordedPathFromKonomiTVBS4KChapterPath(tmp_path / 'program.ts.konomitv-chapters.yaml', {'.ts'}) is None
    assert GetRecordedPathFromKonomiTVBS4KChapterPath(tmp_path / 'program.ts.chapter.txt', {'.ts'}) is None


def test_read_accepts_minimal_handwritten_yaml_and_optional_titles(tmp_path: Path) -> None:
    """generatorなしの簡潔な手書きYAMLをManualとしてCM区間へ変換する。"""

    path = tmp_path / 'program.ts.konomitv-bs4k-chapters.yaml'
    WriteManualYAML(
        path,
        '- at: 00:00:00.000\n'
        '  type: program\n'
        '  title: 本編\n'
        '- at: 00:00:10.000\n'
        '  type: cm\n'
        '- at: 00:00:20.000\n'
        '  type: program\n'
        '  title: Aパート\n',
    )

    result = ReadKonomiTVBS4KChapterFile(path, 60.0)

    assert result.status == 'Valid'
    assert result.sections == ({'start_time': 10.0, 'end_time': 20.0},)
    assert result.fingerprint['exists'] is True
    assert result.fingerprint['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result.provenance is not None
    assert result.provenance.source == 'Manual'
    assert result.provenance.generator_present is False
    assert result.provenance.generator_consistent is None
    assert result.provenance.generator is None
    assert result.provenance.recording is None


def test_read_reports_valid_no_cm_for_program_only_file(tmp_path: Path) -> None:
    """CM境界のない正常な手書きYAMLを欠落・異常と区別する。"""

    path = tmp_path / 'program.ts.konomitv-bs4k-chapters.yaml'
    WriteManualYAML(path, '- at: 00:00:00.000\n  type: program\n')

    result = ReadKonomiTVBS4KChapterFile(path, 60.0)

    assert result.status == 'ValidNoCM'
    assert result.sections == ()


def test_build_outputs_readable_generated_yaml_and_round_trips_sections(tmp_path: Path) -> None:
    """隣接CMを正規化し、生成元情報とchapter hashを含むYAMLを出力する。"""

    path = tmp_path / 'program.ts.konomitv-bs4k-chapters.yaml'
    content = BuildKonomiTVBS4KChapterFile(
        [
            {'start_time': 0.0, 'end_time': 5.0},
            {'start_time': 5.0, 'end_time': 10.0},
            {'start_time': 20.0, 'end_time': 30.0},
            {'start_time': 50.0, 'end_time': 60.0},
        ],
        60.0,
        application_version='1.0.0',
        pipeline_version='cm-7',
        generated_at=GENERATED_AT,
        input_fingerprint=INPUT_FINGERPRINT,
    )
    path.write_bytes(content)

    text = content.decode('utf-8')
    assert text.startswith('schema: konomitv-bs4k/chapters/v1\nchapters:\n')
    assert 'generator:\n  name: KonomiTV-BS4K\n' in text
    assert 'application_version: 1.0.0' in text
    assert 'pipeline_version: cm-7' in text
    assert "generated_at: '2026-07-22T15:00:00+00:00'" in text
    assert 'chapters_sha256: ' in text
    assert 'recording:\n  duration_ms: 60000\n' in text
    assert f'  sample_sha256: {"a" * 64}\n' in text
    result = ReadKonomiTVBS4KChapterFile(path, 60.0)
    assert result.status == 'Valid'
    assert result.sections == (
        {'start_time': 0.0, 'end_time': 10.0},
        {'start_time': 20.0, 'end_time': 30.0},
        {'start_time': 50.0, 'end_time': 60.0},
    )
    assert result.provenance is not None
    assert result.provenance.source == 'Generated'
    assert result.provenance.generator_present is True
    assert result.provenance.generator_consistent is True
    assert result.provenance.generator is not None
    assert result.provenance.generator.pipeline_version == 'cm-7'
    assert result.provenance.recording is not None
    assert result.provenance.recording.size == INPUT_FINGERPRINT['size']


@pytest.mark.parametrize(
    ('current_value', 'obsolete_value'),
    [
        ('schema: konomitv-bs4k/chapters/v1', 'schema: konomitv/chapters/v1'),
        ('name: KonomiTV-BS4K', 'name: KonomiTV'),
        ('pipeline_version: cm-7', 'pipeline_version: KonomiTV-CM-7'),
        ('pipeline_version: cm-7', 'pipeline_version: cm-0'),
        ('pipeline_version: cm-7', 'pipeline_version: cm-07'),
        ('pipeline_version: cm-7', 'pipeline_version: cm-7-beta'),
    ],
)
def test_read_rejects_obsolete_or_invalid_generated_identifiers(
    tmp_path: Path,
    current_value: str,
    obsolete_value: str,
) -> None:
    """旧schema・旧generator名・製品名付きまたは不正なpipelineを受理しない。"""

    path = tmp_path / 'program.ts.konomitv-bs4k-chapters.yaml'
    BuildGeneratedFile(path)
    content = path.read_text(encoding='utf-8')
    assert current_value in content
    path.write_text(content.replace(current_value, obsolete_value, 1), encoding='utf-8')

    result = ReadKonomiTVBS4KChapterFile(path, 60.0)

    assert result.status == 'Invalid'
    assert result.error_code == 'InvalidDocument'
    assert result.provenance is None


def test_edited_generated_yaml_remains_valid_but_loses_generated_provenance(tmp_path: Path) -> None:
    """人がchaptersを編集してhashがずれたYAMLはManualへ降格して採用する。"""

    path = tmp_path / 'program.ts.konomitv-bs4k-chapters.yaml'
    BuildGeneratedFile(path)
    text = path.read_text(encoding='utf-8').replace('  type: cm\n', '  type: program\n', 1)
    path.write_text(text, encoding='utf-8')

    result = ReadKonomiTVBS4KChapterFile(path, 60.0)

    assert result.status == 'Valid'
    assert result.sections == ({'start_time': 30.0, 'end_time': 40.0},)
    assert result.provenance is not None
    assert result.provenance.source == 'Manual'
    assert result.provenance.generator_present is True
    assert result.provenance.generator_consistent is False


def test_generated_recording_duration_mismatch_loses_generated_provenance(tmp_path: Path) -> None:
    """別録画由来の可能性がある生成メタデータをGeneratedとして復元しない。"""

    path = tmp_path / 'program.ts.konomitv-bs4k-chapters.yaml'
    BuildGeneratedFile(path)
    path.write_text(
        path.read_text(encoding='utf-8').replace('duration_ms: 60000', 'duration_ms: 59000'),
        encoding='utf-8',
    )

    result = ReadKonomiTVBS4KChapterFile(path, 60.0)

    assert result.status == 'Valid'
    assert result.provenance is not None
    assert result.provenance.source == 'Manual'
    assert result.provenance.generator_consistent is False


@pytest.mark.parametrize(
    ('body', 'error_code'),
    [
        (
            'schema: konomitv-bs4k/chapters/v1\n'
            'schema: konomitv-bs4k/chapters/v1\n'
            'chapters:\n- at: 00:00:00.000\n  type: program\n',
            'InvalidYAML',
        ),
        ('schema: !!python/object/apply:os.system [echo]\nchapters: []\n', 'InvalidYAML'),
        ('schema: konomitv/chapters/v2\nchapters:\n- at: 00:00:00.000\n  type: program\n', 'InvalidDocument'),
        ('schema: konomitv-bs4k/chapters/v1\nchapters:\n- at: 00:00:01.000\n  type: program\n', 'InvalidDocument'),
        (
            'schema: konomitv-bs4k/chapters/v1\nchapters:\n'
            '- at: 00:00:00.000\n  type: program\n'
            '- at: 00:00:00.000\n  type: cm\n',
            'InvalidDocument',
        ),
        ('schema: konomitv-bs4k/chapters/v1\nchapters:\n- at: 00:60:00.000\n  type: program\n', 'InvalidDocument'),
        ('schema: konomitv-bs4k/chapters/v1\nchapters:\n- at: 00:00:00.000\n  type: commercial\n', 'InvalidDocument'),
        ('schema: konomitv-bs4k/chapters/v1\nchapters:\n- at: 00:00:00.000\n  type: program\n  title: null\n', 'InvalidDocument'),
        ('schema: konomitv-bs4k/chapters/v1\nchapters:\n- at: 00:00:00.000\n  type: program\nextra: true\n', 'InvalidDocument'),
        (
            'schema: konomitv-bs4k/chapters/v1\nchapters:\n- at: 00:00:00.000\n  type: program\n'
            'generator:\n  name: KonomiTV-BS4K\n  application_version: 1.0.0\n'
            '  pipeline_version: cm-7\n  generated_at: "2026-07-22T15:00:00+09:00"\n'
            f'  chapters_sha256: {"a" * 64}\n',
            'InvalidDocument',
        ),
    ],
)
def test_read_rejects_unsafe_or_structurally_invalid_yaml(
    tmp_path: Path,
    body: str,
    error_code: str,
) -> None:
    """safe YAMLとextra-forbidの厳格schemaから外れる入力を拒否する。"""

    path = tmp_path / 'program.ts.konomitv-bs4k-chapters.yaml'
    path.write_text(body, encoding='utf-8')

    result = ReadKonomiTVBS4KChapterFile(path, 60.0)

    assert result.status == 'Invalid'
    assert result.error_code == error_code
    assert result.provenance is None


def test_read_rejects_timestamp_outside_recording(tmp_path: Path) -> None:
    """文書として正常でも録画時間を超える境界は採用しない。"""

    path = tmp_path / 'program.ts.konomitv-bs4k-chapters.yaml'
    WriteManualYAML(
        path,
        '- at: 00:00:00.000\n  type: program\n- at: 00:01:00.001\n  type: cm\n',
    )

    result = ReadKonomiTVBS4KChapterFile(path, 60.0)

    assert result.status == 'Invalid'
    assert result.error_code == 'TimestampOutOfRange'


def test_read_rejects_invalid_encoding_and_oversized_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UTF-8以外と上限超過をYAML parserへ渡さず拒否する。"""

    import app.metadata.KonomiTVBS4KChapterFile as chapter_module

    path = tmp_path / 'program.ts.konomitv-bs4k-chapters.yaml'
    path.write_bytes(b'\xff\xfe')
    assert ReadKonomiTVBS4KChapterFile(path, 60.0).error_code == 'InvalidEncoding'

    monkeypatch.setattr(chapter_module, '_MAX_FILE_SIZE', 16)
    path.write_text('schema: konomitv-bs4k/chapters/v1\nchapters: []\n', encoding='utf-8')
    assert ReadKonomiTVBS4KChapterFile(path, 60.0).error_code == 'FileTooLarge'


@pytest.mark.parametrize(
    'sections',
    [
        [{'start_time': -1.0, 'end_time': 10.0}],
        [{'start_time': 10.0, 'end_time': 10.0}],
        [{'start_time': 10.0, 'end_time': 61.0}],
        [{'start_time': 10.0, 'end_time': 20.0}, {'start_time': 19.0, 'end_time': 30.0}],
        [{'start_time': float('nan'), 'end_time': 10.0}],
        [{'start_time': 0.0001, 'end_time': 0.0002}],
    ],
)
def test_build_rejects_invalid_cm_sections(sections: list[dict[str, float]]) -> None:
    """範囲外・重複・非有限・ミリ秒で表現不能なCM区間を生成しない。"""

    with pytest.raises(ValueError):
        BuildKonomiTVBS4KChapterFile(
            sections,  # type: ignore[arg-type]
            60.0,
            application_version='1.0.0',
            pipeline_version='cm-7',
            generated_at=GENERATED_AT,
            input_fingerprint=INPUT_FINGERPRINT,
        )


def test_build_rejects_incomplete_input_fingerprint_and_naive_datetime() -> None:
    """Generated復元に必要な録画fingerprintとタイムゾーンを必須にする。"""

    with pytest.raises(ValueError, match='sample_sha256'):
        BuildKonomiTVBS4KChapterFile(
            [],
            60.0,
            application_version='1.0.0',
            pipeline_version='cm-7',
            generated_at=GENERATED_AT,
            input_fingerprint={'size': 1, 'mtime_ns': 2},
        )
    with pytest.raises(ValueError, match='timezone'):
        BuildKonomiTVBS4KChapterFile(
            [],
            60.0,
            application_version='1.0.0',
            pipeline_version='cm-7',
            generated_at=datetime(2026, 7, 22, 15, 0),
            input_fingerprint=INPUT_FINGERPRINT,
        )


def test_commit_publishes_generated_yaml_with_missing_fingerprint_cas(tmp_path: Path) -> None:
    """欠落を確認した確定先へno-clobberで原子的にGenerated YAMLを公開する。"""

    destination_path = tmp_path / 'program.ts.konomitv-bs4k-chapters.yaml'

    result = CommitKonomiTVBS4KChapterFile(
        destination_path,
        [{'start_time': 10.0, 'end_time': 20.0}],
        60.0,
        application_version='1.0.0',
        pipeline_version='cm-7',
        generated_at=GENERATED_AT,
        input_fingerprint=INPUT_FINGERPRINT,
        expected_destination_fingerprint={'exists': False},
    )

    assert result.status == 'Valid'
    assert result.sections == ({'start_time': 10.0, 'end_time': 20.0},)
    assert result.provenance is not None
    assert result.provenance.source == 'Generated'
    assert destination_path.stat().st_mode & 0o777 == 0o644
    assert list(tmp_path.glob('.*.tmp')) == []


def test_commit_rejects_destination_changed_after_expected_fingerprint(tmp_path: Path) -> None:
    """解析開始後に手書きYAMLへ変わった確定先を上書きしない。"""

    destination_path = tmp_path / 'program.ts.konomitv-bs4k-chapters.yaml'
    BuildGeneratedFile(destination_path)
    expected = ReadKonomiTVBS4KChapterFile(destination_path, 60.0).fingerprint
    WriteManualYAML(destination_path, '- at: 00:00:00.000\n  type: program\n  title: External\n')
    original = destination_path.read_bytes()

    with pytest.raises(KonomiTVBS4KChapterConflictError):
        CommitKonomiTVBS4KChapterFile(
            destination_path,
            [{'start_time': 10.0, 'end_time': 20.0}],
            60.0,
            application_version='1.0.0',
            pipeline_version='cm-7',
            generated_at=GENERATED_AT,
            input_fingerprint=INPUT_FINGERPRINT,
            expected_destination_fingerprint=expected,
        )

    assert destination_path.read_bytes() == original


def test_commit_rejects_file_created_between_missing_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最初の欠落確認直後に作られた手書きYAMLも二回目のCASで保護する。"""

    import app.metadata.KonomiTVBS4KChapterFile as chapter_module

    destination_path = tmp_path / 'program.ts.konomitv-bs4k-chapters.yaml'
    original_read = chapter_module.ReadKonomiTVBS4KChapterFile
    injected = False

    def ReadWithConcurrentCreation(path: Path, duration_sec: float):  # type: ignore[no-untyped-def]
        nonlocal injected
        result = original_read(path, duration_sec)
        if path == destination_path and result.status == 'Missing' and injected is False:
            WriteManualYAML(path, '- at: 00:00:00.000\n  type: program\n  title: External\n')
            injected = True
        return result

    monkeypatch.setattr(chapter_module, 'ReadKonomiTVBS4KChapterFile', ReadWithConcurrentCreation)

    with pytest.raises(KonomiTVBS4KChapterConflictError):
        CommitKonomiTVBS4KChapterFile(
            destination_path,
            [{'start_time': 10.0, 'end_time': 20.0}],
            60.0,
            application_version='1.0.0',
            pipeline_version='cm-7',
            generated_at=GENERATED_AT,
            input_fingerprint=INPUT_FINGERPRINT,
            expected_destination_fingerprint={'exists': False},
        )

    assert 'External' in destination_path.read_text(encoding='utf-8')


def test_async_read_and_commit_wrap_blocking_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Orchestratorから利用する非同期APIも同期APIと同じ結果を返す。"""

    destination_path = tmp_path / 'program.ts.konomitv-bs4k-chapters.yaml'

    async def RunInline(function, *args, **kwargs):  # type: ignore[no-untyped-def]
        return function(*args, **kwargs)

    # このunit testではスレッド機構自体ではなく、同期APIへの委譲引数と結果の一致を検証する。
    monkeypatch.setattr(asyncio, 'to_thread', RunInline)

    committed = asyncio.run(
        CommitKonomiTVBS4KChapterFileAsync(
            destination_path,
            [{'start_time': 10.0, 'end_time': 20.0}],
            60.0,
            application_version='1.0.0',
            pipeline_version='cm-7',
            generated_at=GENERATED_AT,
            input_fingerprint=INPUT_FINGERPRINT,
            expected_destination_fingerprint={'exists': False},
        ),
    )
    read = asyncio.run(ReadKonomiTVBS4KChapterFileAsync(destination_path, 60.0))

    assert committed.status == 'Valid'
    assert read.sections == committed.sections
    assert read.fingerprint == committed.fingerprint
    assert read.provenance == committed.provenance
