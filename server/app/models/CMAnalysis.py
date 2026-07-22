from __future__ import annotations

from datetime import datetime
from typing import Literal, cast

from tortoise import fields
from tortoise.fields import Field as TortoiseField
from tortoise.models import Model as TortoiseModel

from app.models.RecordedVideo import RecordedVideo


CMAnalysisStatus = Literal[
    'Pending',
    'Analyzing',
    'Completed',
    'Failed',
    'Unsupported',
    'Excluded',
    'Interrupted',
]
CMChapterSource = Literal['Existing', 'Generated']
CMChapterPathKind = Literal['Canonical', 'Legacy']
CMResultSource = Literal['Existing', 'Generated', 'LegacyImported', 'Manual']
CMLogoFileFormat = Literal['AviUtlV0.1', 'AmatsukazeExtendedV1']
CMLogoGenerationBatchState = Literal['NotAttempted', 'Running', 'Succeeded', 'Exhausted', 'Interrupted']


class CMAnalysisSettings(TortoiseModel):
    """サーバー全体で共有するCM解析設定を保持する。"""

    class Meta(TortoiseModel.Meta):
        table: str = 'cm_analysis_settings'

    id = fields.IntField(pk=True)
    enabled = fields.BooleanField(default=False)
    logo_directory = cast(TortoiseField[str | None], fields.TextField(null=True))
    updated_at = fields.DatetimeField(auto_now=True)


class CMAnalysisExcludedDirectory(TortoiseModel):
    """新規CM解析だけを除外するディレクトリを保持する。"""

    class Meta(TortoiseModel.Meta):
        table: str = 'cm_analysis_excluded_directories'

    id = fields.IntField(pk=True)
    path = fields.TextField()
    resolved_path = fields.TextField()
    enabled = fields.BooleanField(default=True)
    created_at = fields.DatetimeField(auto_now_add=True)


class CMLogo(TortoiseModel):
    """共有ロゴフォルダで検出した.lgdファイルと利用履歴を保持する。"""

    class Meta(TortoiseModel.Meta):
        table: str = 'cm_logos'

    id = fields.IntField(pk=True)
    path = fields.TextField()
    filename = fields.TextField()
    service_id = cast(TortoiseField[int | None], fields.IntField(db_index=True, null=True))
    logo_name = fields.TextField()
    file_format = cast(TortoiseField[CMLogoFileFormat], fields.CharField(32, default='AmatsukazeExtendedV1'))
    enabled = fields.BooleanField(default=True)
    file_hash = fields.CharField(64)
    file_size = fields.BigIntField()
    generated_from_recorded_video: fields.ForeignKeyNullableRelation[RecordedVideo] = fields.ForeignKeyField(
        'models.RecordedVideo',
        related_name=None,
        null=True,
        on_delete=fields.SET_NULL,
    )
    generated_from_recorded_video_id: int | None
    last_used_at = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    missing = fields.BooleanField(default=False)
    deleted_at = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)


class CMLogoServiceAssignment(TortoiseModel):
    """SIDロゴをKonomiTV-BS4K固有のNID・TSIDサービスへ割り当てる。"""

    class Meta(TortoiseModel.Meta):
        table: str = 'cm_logo_service_assignments'
        unique_together = (('network_id', 'transport_stream_id', 'service_id', 'valid_from', 'valid_until'),)

    id = fields.IntField(pk=True)
    logo: fields.ForeignKeyNullableRelation[CMLogo] = fields.ForeignKeyField(
        'models.CMLogo',
        related_name='service_assignments',
        null=True,
        on_delete=fields.SET_NULL,
    )
    logo_id: int | None
    network_id = fields.IntField()
    transport_stream_id = fields.IntField()
    service_id = fields.IntField()
    enabled = fields.BooleanField(default=True)
    is_no_logo = fields.BooleanField(default=False)
    valid_from = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    valid_until = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)


class CMLogoGenerationBatch(TortoiseModel):
    """録画単位の有限ロゴ生成試行をまとめる。"""

    class Meta(TortoiseModel.Meta):
        table: str = 'cm_logo_generation_batches'

    id = fields.IntField(pk=True)
    recorded_video: fields.ForeignKeyRelation[RecordedVideo] = fields.ForeignKeyField(
        'models.RecordedVideo',
        related_name='cm_logo_generation_batches',
        on_delete=fields.CASCADE,
    )
    recorded_video_id: int
    state = cast(TortoiseField[CMLogoGenerationBatchState], fields.CharField(32, db_index=True))
    explicit = fields.BooleanField(default=False)
    created_at = fields.DatetimeField(auto_now_add=True)
    completed_at = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))


class CMLogoGenerationAttempt(TortoiseModel):
    """ロゴ生成バッチ内で実行した区間・設定と結果を保持する。"""

    class Meta(TortoiseModel.Meta):
        table: str = 'cm_logo_generation_attempts'
        unique_together = (('batch_id', 'strategy', 'start_frame', 'start_seconds', 'frame_count'),)

    id = fields.IntField(pk=True)
    batch: fields.ForeignKeyRelation[CMLogoGenerationBatch] = fields.ForeignKeyField(
        'models.CMLogoGenerationBatch',
        related_name='attempts',
        on_delete=fields.CASCADE,
    )
    batch_id: int
    strategy = fields.CharField(32)
    start_frame = cast(TortoiseField[int | None], fields.IntField(null=True))
    start_seconds = cast(TortoiseField[float | None], fields.FloatField(null=True))
    frame_count = fields.IntField()
    exit_code = cast(TortoiseField[int | None], fields.IntField(null=True))
    match_ratio = cast(TortoiseField[float | None], fields.FloatField(null=True))
    failure_reason = cast(TortoiseField[str | None], fields.TextField(null=True))
    created_at = fields.DatetimeField(auto_now_add=True)
    completed_at = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))


class RecordedVideoCMAnalysis(TortoiseModel):
    """録画本体とは独立したCM解析状態とchapter同期状態を保持する。"""

    class Meta(TortoiseModel.Meta):
        table: str = 'recorded_video_cm_analyses'

    id = fields.IntField(pk=True)
    recorded_video: fields.OneToOneRelation[RecordedVideo] = fields.OneToOneField(
        'models.RecordedVideo',
        related_name='cm_analysis',
        on_delete=fields.CASCADE,
    )
    recorded_video_id: int
    status = cast(TortoiseField[CMAnalysisStatus], fields.CharField(32, default='Pending', db_index=True))
    chapter_source = cast(TortoiseField[CMChapterSource | None], fields.CharField(32, null=True))
    chapter_path_kind = cast(TortoiseField[CMChapterPathKind | None], fields.CharField(32, null=True))
    input_fingerprint = cast(TortoiseField[dict[str, int | str] | None], fields.JSONField(null=True))
    chapter_fingerprint = cast(TortoiseField[dict[str, int | str | bool] | None], fields.JSONField(null=True))
    chapter_last_read_at = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    used_logo: fields.ForeignKeyNullableRelation[CMLogo] = fields.ForeignKeyField(
        'models.CMLogo',
        related_name=None,
        null=True,
        on_delete=fields.SET_NULL,
    )
    used_logo_id: int | None
    analyzer_version = cast(TortoiseField[str | None], fields.CharField(255, null=True))
    attempt_key_sha256 = cast(TortoiseField[str | None], fields.CharField(64, null=True))
    runtime_fingerprint = cast(TortoiseField[dict[str, object] | None], fields.JSONField(null=True))
    started_at = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    completed_at = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    finished_at = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    error_code = cast(TortoiseField[str | None], fields.CharField(255, null=True))
    error_message = cast(TortoiseField[str | None], fields.TextField(null=True))
    matched_exclusion_path = cast(TortoiseField[str | None], fields.TextField(null=True))
    last_explicit_batch: fields.ForeignKeyNullableRelation[CMLogoGenerationBatch] = fields.ForeignKeyField(
        'models.CMLogoGenerationBatch',
        related_name=None,
        null=True,
        on_delete=fields.SET_NULL,
    )
    last_explicit_batch_id: int | None
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)


class RecordedVideoCMResult(TortoiseModel):
    """最新試行とは独立して、最後に公開したCM解析結果の由来を保持する。"""

    class Meta(TortoiseModel.Meta):
        table: str = 'recorded_video_cm_results'

    id = fields.IntField(pk=True)
    recorded_video: fields.OneToOneRelation[RecordedVideo] = fields.OneToOneField(
        'models.RecordedVideo',
        related_name='cm_analysis_result',
        on_delete=fields.CASCADE,
    )
    recorded_video_id: int
    source = cast(TortoiseField[CMResultSource], fields.CharField(32))
    verified = fields.BooleanField(default=True)
    input_fingerprint = cast(TortoiseField[dict[str, int | str] | None], fields.JSONField(null=True))
    chapter_fingerprint = cast(TortoiseField[dict[str, int | str | bool] | None], fields.JSONField(null=True))
    chapter_path_kind = cast(TortoiseField[CMChapterPathKind | None], fields.CharField(32, null=True))
    pipeline_version = cast(TortoiseField[str | None], fields.CharField(255, null=True))
    runtime_fingerprint = cast(TortoiseField[dict[str, object] | None], fields.JSONField(null=True))
    used_logo: fields.ForeignKeyNullableRelation[CMLogo] = fields.ForeignKeyField(
        'models.CMLogo',
        related_name=None,
        null=True,
        on_delete=fields.SET_NULL,
    )
    used_logo_id: int | None
    published_at = fields.DatetimeField()
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
