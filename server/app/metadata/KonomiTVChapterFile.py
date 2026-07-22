from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError  # type: ignore[reportMissingTypeStubs]

from app.schemas import CMSection


KonomiTVChapterReadStatus = Literal['Valid', 'ValidNoCM', 'Missing', 'Invalid', 'IOError']
KonomiTVChapterSource = Literal['Generated', 'Manual']
KonomiTVChapterFingerprint = dict[str, int | str | bool]

_SCHEMA_VERSION = 'konomitv/chapters/v1'
_SIDECAR_SUFFIX = '.konomitv-chapters.yaml'
_MAX_FILE_SIZE = 4 * 1024 * 1024
_TIMESTAMP_PATTERN = re.compile(
    r'^(?P<hours>\d{2,}):(?P<minutes>[0-5]\d):(?P<seconds>[0-5]\d)\.(?P<milliseconds>\d{3})$',
)


class KonomiTVChapterEntry(BaseModel):
    """一つのチャプター境界と、次の境界までの区間種別を表す。"""

    model_config = ConfigDict(extra='forbid', strict=True)

    at: Annotated[str, Field(min_length=12, max_length=32)]
    type: Literal['program', 'cm']
    title: Annotated[str, Field(min_length=1, max_length=512)] | None = None

    @field_validator('at')
    @classmethod
    def ValidateTimestamp(cls, value: str) -> str:
        """ミリ秒精度の固定形式だけを受け入れる。"""

        if _TIMESTAMP_PATTERN.fullmatch(value) is None:
            raise ValueError('Chapter timestamp must use HH:MM:SS.mmm format.')
        return value

    @model_validator(mode='before')
    @classmethod
    def RejectExplicitNullTitle(cls, value: object) -> object:
        """titleの省略とnullを区別し、手書きミスを黙って受け入れない。"""

        if isinstance(value, Mapping) and 'title' in value and value['title'] is None:
            raise ValueError('Chapter title must be omitted instead of null.')
        return value


class KonomiTVChapterGenerator(BaseModel):
    """KonomiTVがsidecarを生成したときの由来情報を表す。"""

    model_config = ConfigDict(extra='forbid', strict=True)

    name: Literal['KonomiTV']
    application_version: Annotated[str, Field(min_length=1, max_length=128)]
    pipeline_version: Annotated[str, Field(min_length=1, max_length=128)]
    generated_at: Annotated[str, Field(min_length=1, max_length=64)]
    chapters_sha256: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]

    @field_validator('generated_at')
    @classmethod
    def ValidateGeneratedAt(cls, value: str) -> str:
        """生成時刻にはタイムゾーン付きISO 8601だけを許可する。"""

        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError as ex:
            raise ValueError('generated_at must be an ISO 8601 timestamp.') from ex
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('generated_at must include a timezone offset.')
        return value


class KonomiTVChapterRecording(BaseModel):
    """生成元録画を識別し、古いsidecarの誤採用を防ぐ情報を表す。"""

    model_config = ConfigDict(extra='forbid', strict=True)

    duration_ms: Annotated[int, Field(ge=0)]
    size: Annotated[int, Field(ge=0)]
    mtime_ns: Annotated[int, Field(ge=0)]
    sample_sha256: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]


class KonomiTVChapterDocument(BaseModel):
    """KonomiTV chapter sidecar v1の文書全体を表す。"""

    model_config = ConfigDict(extra='forbid', populate_by_name=True, strict=True)

    schema_version: Literal['konomitv/chapters/v1'] = Field(alias='schema', serialization_alias='schema')
    chapters: Annotated[list[KonomiTVChapterEntry], Field(min_length=1, max_length=100_000)]
    generator: KonomiTVChapterGenerator | None = None
    recording: KonomiTVChapterRecording | None = None

    @model_validator(mode='after')
    def ValidateDocument(self) -> KonomiTVChapterDocument:
        """境界順序と生成メタデータの内部整合性を検証する。"""

        timestamps_ms = [_ParseTimestampMilliseconds(chapter.at) for chapter in self.chapters]
        if timestamps_ms[0] != 0:
            raise ValueError('The first chapter must start at 00:00:00.000.')
        if any(current <= previous for previous, current in pairwise(timestamps_ms)):
            raise ValueError('Chapter timestamps must be strictly increasing.')

        # generatorとrecordingは一組で初めてKonomiTV生成物の出自検証に使える。
        if (self.generator is None) != (self.recording is None):
            raise ValueError('generator and recording must either both be present or both be omitted.')
        return self


@dataclass(frozen=True, slots=True)
class KonomiTVChapterProvenance:
    """手書きとKonomiTV生成物を区別するための検証済み由来情報をまとめる。"""

    source: KonomiTVChapterSource
    generator_present: bool
    generator_consistent: bool | None
    generator: KonomiTVChapterGenerator | None
    recording: KonomiTVChapterRecording | None


@dataclass(frozen=True, slots=True)
class KonomiTVChapterReadResult:
    """YAML sidecarの読込状態、CM区間、fingerprint、由来をまとめる。"""

    status: KonomiTVChapterReadStatus
    sections: tuple[CMSection, ...]
    fingerprint: KonomiTVChapterFingerprint
    provenance: KonomiTVChapterProvenance | None
    error_code: str | None = None
    error_message: str | None = None


class KonomiTVChapterConflictError(ValueError):
    """解析中に確定先sidecarが変更され、原子的な公開を中止したことを表す。"""


def GetKonomiTVChapterPath(recorded_file_path: Path) -> Path:
    """録画ファイルに対応するKonomiTV YAML sidecarのパスを返す。"""

    return recorded_file_path.with_name(f'{recorded_file_path.name}{_SIDECAR_SUFFIX}')


def GetRecordedPathFromKonomiTVChapterPath(
    chapter_file_path: Path,
    candidate_extensions: set[str],
) -> Path | None:
    """YAML sidecarのファイル名から、対応する録画パスを一意に復元する。"""

    if chapter_file_path.name.lower().endswith(_SIDECAR_SUFFIX) is False:
        return None
    recorded_name = chapter_file_path.name[:-len(_SIDECAR_SUFFIX)]
    recorded_path = chapter_file_path.with_name(recorded_name)
    normalized_extensions = {extension.lower() for extension in candidate_extensions}
    if recorded_path.suffix.lower() not in normalized_extensions:
        return None
    return recorded_path


def BuildKonomiTVChapterFile(
    sections: Iterable[CMSection],
    duration_sec: float,
    *,
    application_version: str,
    pipeline_version: str,
    generated_at: datetime | str,
    input_fingerprint: Mapping[str, int | str],
) -> bytes:
    """CM区間と生成情報から、可読なKonomiTV YAML sidecarを構築する。

    Args:
        sections: KonomiTVの解析器が検出したCM区間。
        duration_sec: 対応する録画の秒数。
        application_version: KonomiTV本体のバージョン。
        pipeline_version: CM解析パイプラインのバージョン。
        generated_at: タイムゾーン付きの生成日時。
        input_fingerprint: size・mtime_ns・sample_sha256を持つ録画fingerprint。

    Returns:
        UTF-8でエンコードされたYAML文書。
    """

    duration_ms = _DurationToMilliseconds(duration_sec)
    chapters = _BuildChaptersFromSections(sections, duration_sec)
    generated_at_text = generated_at.isoformat() if isinstance(generated_at, datetime) else generated_at
    if isinstance(generated_at, datetime) and (generated_at.tzinfo is None or generated_at.utcoffset() is None):
        raise ValueError('generated_at must include a timezone offset.')

    try:
        recording = KonomiTVChapterRecording.model_validate({
            'duration_ms': duration_ms,
            'size': input_fingerprint['size'],
            'mtime_ns': input_fingerprint['mtime_ns'],
            'sample_sha256': input_fingerprint['sample_sha256'],
        })
    except KeyError as ex:
        raise ValueError(f'input_fingerprint is missing {ex.args[0]}.') from ex

    generator = KonomiTVChapterGenerator(
        name='KonomiTV',
        application_version=application_version,
        pipeline_version=pipeline_version,
        generated_at=generated_at_text,
        chapters_sha256=_CalculateChaptersSHA256(chapters),
    )
    document = KonomiTVChapterDocument.model_validate({
        'schema': _SCHEMA_VERSION,
        'chapters': chapters,
        'generator': generator,
        'recording': recording,
    })
    return _SerializeDocument(document)


def ReadKonomiTVChapterFile(chapter_file_path: Path, duration_sec: float) -> KonomiTVChapterReadResult:
    """KonomiTV YAML sidecarを厳格に検証してCM区間へ変換する。"""

    missing_fingerprint: KonomiTVChapterFingerprint = {'exists': False}
    try:
        stat_before = chapter_file_path.stat()
    except FileNotFoundError:
        return KonomiTVChapterReadResult('Missing', (), missing_fingerprint, None)
    except OSError as ex:
        return KonomiTVChapterReadResult('IOError', (), missing_fingerprint, None, type(ex).__name__, str(ex))

    try:
        raw = chapter_file_path.read_bytes()
        stat_after = chapter_file_path.stat()
    except FileNotFoundError:
        return KonomiTVChapterReadResult('Missing', (), missing_fingerprint, None)
    except OSError as ex:
        fingerprint: KonomiTVChapterFingerprint = {
            'exists': True,
            'size': stat_before.st_size,
            'mtime_ns': stat_before.st_mtime_ns,
            'device': stat_before.st_dev,
            'inode': stat_before.st_ino,
        }
        return KonomiTVChapterReadResult('IOError', (), fingerprint, None, type(ex).__name__, str(ex))

    fingerprint: KonomiTVChapterFingerprint = {
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
        return KonomiTVChapterReadResult(
            'IOError',
            (),
            fingerprint,
            None,
            'ChapterChangedDuringRead',
            'The KonomiTV chapter file changed while it was being read.',
        )

    try:
        duration_ms = _DurationToMilliseconds(duration_sec)
    except ValueError as ex:
        return KonomiTVChapterReadResult('Invalid', (), fingerprint, None, 'InvalidDuration', str(ex))

    # safe loaderへ巨大入力を渡さず、手書きsidecarとして十分な余裕を残しながら資源消費を制限する。
    if len(raw) > _MAX_FILE_SIZE:
        return KonomiTVChapterReadResult(
            'Invalid',
            (),
            fingerprint,
            None,
            'FileTooLarge',
            'The KonomiTV chapter file is too large.',
        )

    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as ex:
        return KonomiTVChapterReadResult('Invalid', (), fingerprint, None, 'InvalidEncoding', str(ex))

    try:
        yaml = _CreateSafeYAML()
        loaded = yaml.load(text)
    except YAMLError as ex:
        return KonomiTVChapterReadResult('Invalid', (), fingerprint, None, 'InvalidYAML', str(ex))

    try:
        document = KonomiTVChapterDocument.model_validate(loaded)
    except ValidationError as ex:
        return KonomiTVChapterReadResult('Invalid', (), fingerprint, None, 'InvalidDocument', str(ex))

    timestamps_ms = [_ParseTimestampMilliseconds(chapter.at) for chapter in document.chapters]
    if any(timestamp_ms / 1000 > duration_sec for timestamp_ms in timestamps_ms):
        return KonomiTVChapterReadResult(
            'Invalid',
            (),
            fingerprint,
            None,
            'TimestampOutOfRange',
            'A chapter timestamp is outside the recording.',
        )

    sections = _BuildSectionsFromDocument(document, duration_sec)
    generator_consistent: bool | None = None
    if document.generator is not None and document.recording is not None:
        # 人が生成済みYAMLのchaptersだけを編集した場合もCM区間自体は採用できる。
        # hashまたは録画時間が一致しなければGeneratedではなくManualとして明示する。
        generator_consistent = (
            document.generator.chapters_sha256 == _CalculateChaptersSHA256(document.chapters)
            and document.recording.duration_ms == duration_ms
        )
    provenance = KonomiTVChapterProvenance(
        source='Generated' if generator_consistent is True else 'Manual',
        generator_present=document.generator is not None,
        generator_consistent=generator_consistent,
        generator=document.generator,
        recording=document.recording,
    )
    status: KonomiTVChapterReadStatus = 'Valid' if sections else 'ValidNoCM'
    return KonomiTVChapterReadResult(status, sections, fingerprint, provenance)


async def ReadKonomiTVChapterFileAsync(
    chapter_file_path: Path,
    duration_sec: float,
) -> KonomiTVChapterReadResult:
    """YAMLの同期I/O・構文検証・ハッシュ計算をワーカースレッドで実行する。"""

    return await asyncio.to_thread(ReadKonomiTVChapterFile, chapter_file_path, duration_sec)


def CommitKonomiTVChapterFile(
    destination_path: Path,
    sections: Iterable[CMSection],
    duration_sec: float,
    *,
    application_version: str,
    pipeline_version: str,
    generated_at: datetime | str,
    input_fingerprint: Mapping[str, int | str],
    expected_destination_fingerprint: KonomiTVChapterFingerprint | None = None,
) -> KonomiTVChapterReadResult:
    """KonomiTV生成YAMLをCAS検証して録画横へ原子的に配置する。"""

    content = BuildKonomiTVChapterFile(
        sections,
        duration_sec,
        application_version=application_version,
        pipeline_version=pipeline_version,
        generated_at=generated_at,
        input_fingerprint=input_fingerprint,
    )

    def CheckDestinationFingerprint() -> None:
        if expected_destination_fingerprint is None:
            return
        current = ReadKonomiTVChapterFile(destination_path, duration_sec)
        if current.fingerprint != expected_destination_fingerprint:
            raise KonomiTVChapterConflictError('The destination KonomiTV chapter file changed during CM analysis.')

    CheckDestinationFingerprint()
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f'.{destination_path.name}.',
        suffix='.tmp',
        dir=destination_path.parent,
    )
    temporary_path = Path(temporary_name)
    source_result: KonomiTVChapterReadResult | None = None
    try:
        with os.fdopen(temporary_fd, 'wb') as temporary_file:
            os.fchmod(temporary_file.fileno(), 0o644)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        source_result = ReadKonomiTVChapterFile(temporary_path, duration_sec)
        if source_result.status not in ('Valid', 'ValidNoCM'):
            raise ValueError(f'Generated KonomiTV chapter is not valid: {source_result.error_code}')

        # 欠落状態からの公開にはhard linkを使い、解析中に手書きYAMLが作られても上書きしない。
        # 既存のKonomiTV生成物を置換するときもrename直前にfingerprintを再確認する。
        CheckDestinationFingerprint()
        if expected_destination_fingerprint is not None and expected_destination_fingerprint.get('exists') is False:
            try:
                os.link(temporary_path, destination_path)
            except FileExistsError as ex:
                raise KonomiTVChapterConflictError(
                    'The destination KonomiTV chapter file was created during CM analysis.',
                ) from ex
        else:
            os.replace(temporary_path, destination_path)

        # rename/linkと内容の永続化を一つのcommitとして扱うため、親ディレクトリも同期する。
        parent_fd = os.open(destination_path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        temporary_path.unlink(missing_ok=True)

    committed_result = ReadKonomiTVChapterFile(destination_path, duration_sec)
    if committed_result.status not in ('Valid', 'ValidNoCM'):
        raise OSError(f'Committed KonomiTV chapter validation failed: {committed_result.error_code}')
    if source_result is None or committed_result.fingerprint.get('sha256') != source_result.fingerprint.get('sha256'):
        raise KonomiTVChapterConflictError(
            'The destination KonomiTV chapter file changed immediately after CM analysis commit.',
        )
    return committed_result


async def CommitKonomiTVChapterFileAsync(
    destination_path: Path,
    sections: Iterable[CMSection],
    duration_sec: float,
    *,
    application_version: str,
    pipeline_version: str,
    generated_at: datetime | str,
    input_fingerprint: Mapping[str, int | str],
    expected_destination_fingerprint: KonomiTVChapterFingerprint | None = None,
) -> KonomiTVChapterReadResult:
    """KonomiTV生成YAMLの構築・確定処理をワーカースレッドで実行する。"""

    return await asyncio.to_thread(
        CommitKonomiTVChapterFile,
        destination_path,
        sections,
        duration_sec,
        application_version=application_version,
        pipeline_version=pipeline_version,
        generated_at=generated_at,
        input_fingerprint=input_fingerprint,
        expected_destination_fingerprint=expected_destination_fingerprint,
    )


def _CreateSafeYAML() -> YAML:
    """呼び出しごとにduplicate key拒否のsafe YAMLインスタンスを作る。"""

    # pure emitterを使うとシーケンスをmapping配下へインデントでき、手編集しやすい出力になる。
    yaml = YAML(typ='safe', pure=True)
    yaml.allow_duplicate_keys = False
    yaml.default_flow_style = False
    yaml.sort_base_mapping_type_on_output = False  # pyright: ignore[reportAttributeAccessIssue]
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    return yaml


def _SerializeDocument(document: KonomiTVChapterDocument) -> bytes:
    """検証済み文書を人間が編集しやすいblock-style YAMLへ直列化する。"""

    data = document.model_dump(mode='json', by_alias=True, exclude_none=True)
    stream = io.StringIO()
    _CreateSafeYAML().dump(data, stream)
    return stream.getvalue().encode('utf-8')


def _CalculateChaptersSHA256(chapters: Iterable[KonomiTVChapterEntry]) -> str:
    """YAMLの表記差に影響されない正規化chapterハッシュを計算する。"""

    normalized = [chapter.model_dump(mode='json', exclude_none=True) for chapter in chapters]
    content = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(content).hexdigest()


def _ParseTimestampMilliseconds(timestamp: str) -> int:
    """検証済みHH:MM:SS.mmmを整数ミリ秒へ変換する。"""

    match = _TIMESTAMP_PATTERN.fullmatch(timestamp)
    if match is None:
        raise ValueError('Chapter timestamp must use HH:MM:SS.mmm format.')
    return (
        int(match.group('hours')) * 3_600_000
        + int(match.group('minutes')) * 60_000
        + int(match.group('seconds')) * 1_000
        + int(match.group('milliseconds'))
    )


def _FormatTimestampMilliseconds(timestamp_ms: int) -> str:
    """整数ミリ秒をHH:MM:SS.mmmへ変換する。"""

    hours, remainder = divmod(timestamp_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}'


def _DurationToMilliseconds(duration_sec: float) -> int:
    """録画時間を検証し、sidecarに保存する整数ミリ秒へ丸める。"""

    duration_value = _RequireFiniteNumber(
        duration_sec,
        'Recording duration must be a finite non-negative number.',
    )
    if duration_value < 0:
        raise ValueError('Recording duration must be a finite non-negative number.')
    return round(duration_value * 1000)


def _RequireFiniteNumber(value: object, error_message: str) -> float:
    """boolや文字列への暗黙変換を行わず、有限の数値だけをfloatとして返す。"""

    if isinstance(value, bool):
        raise ValueError(error_message)
    if not isinstance(value, (int, float)):
        raise ValueError(error_message)
    if math.isfinite(value) is False:
        raise ValueError(error_message)
    return float(value)


def _BuildChaptersFromSections(
    sections: Iterable[CMSection],
    duration_sec: float,
) -> list[KonomiTVChapterEntry]:
    """CM区間列を全タイムラインを表すprogram/cm境界列へ変換する。"""

    duration_ms = _DurationToMilliseconds(duration_sec)
    normalized_sections: list[tuple[int, int]] = []
    previous_end_sec = 0.0
    for section in sections:
        try:
            start_value = section['start_time']
            end_value = section['end_time']
        except KeyError as ex:
            raise ValueError(f'CM section is missing {ex.args[0]}.') from ex
        start_sec = _RequireFiniteNumber(start_value, 'CM section timestamps must be finite numbers.')
        end_sec = _RequireFiniteNumber(end_value, 'CM section timestamps must be finite numbers.')
        if start_sec < 0 or end_sec <= start_sec or end_sec > duration_sec:
            raise ValueError('CM section is outside the recording or has no duration.')
        if normalized_sections and start_sec < previous_end_sec:
            raise ValueError('CM sections must be ordered and must not overlap.')

        start_ms = round(start_sec * 1000)
        end_ms = round(end_sec * 1000)
        if start_ms < 0 or end_ms > duration_ms or end_ms <= start_ms:
            raise ValueError('CM section cannot be represented at millisecond precision.')
        if normalized_sections and start_ms < normalized_sections[-1][1]:
            raise ValueError('CM sections overlap after millisecond rounding.')

        # 隣接したCM区間は一つへまとめ、同時刻の重複境界を出力しない。
        if normalized_sections and start_ms == normalized_sections[-1][1]:
            normalized_sections[-1] = (normalized_sections[-1][0], end_ms)
        else:
            normalized_sections.append((start_ms, end_ms))
        previous_end_sec = end_sec

    chapters: list[KonomiTVChapterEntry] = []
    if normalized_sections and normalized_sections[0][0] == 0:
        chapters.append(KonomiTVChapterEntry(at='00:00:00.000', type='cm'))
    else:
        chapters.append(KonomiTVChapterEntry(at='00:00:00.000', type='program'))

    for start_ms, end_ms in normalized_sections:
        if start_ms > 0:
            chapters.append(KonomiTVChapterEntry(at=_FormatTimestampMilliseconds(start_ms), type='cm'))
        if end_ms < duration_ms:
            chapters.append(KonomiTVChapterEntry(at=_FormatTimestampMilliseconds(end_ms), type='program'))
    return chapters


def _BuildSectionsFromDocument(
    document: KonomiTVChapterDocument,
    duration_sec: float,
) -> tuple[CMSection, ...]:
    """各境界から次境界・録画末尾までを区間とみなし、連続CMをまとめる。"""

    timestamps_sec = [_ParseTimestampMilliseconds(chapter.at) / 1000 for chapter in document.chapters]
    sections: list[CMSection] = []
    cm_start: float | None = None
    for index, chapter in enumerate(document.chapters):
        start_sec = timestamps_sec[index]
        end_sec = timestamps_sec[index + 1] if index + 1 < len(timestamps_sec) else duration_sec
        if chapter.type == 'cm' and start_sec < end_sec and cm_start is None:
            cm_start = start_sec
        if chapter.type != 'cm' and cm_start is not None:
            sections.append(CMSection(start_time=cm_start, end_time=start_sec))
            cm_start = None
    if cm_start is not None and cm_start < duration_sec:
        sections.append(CMSection(start_time=cm_start, end_time=duration_sec))
    return tuple(sections)
