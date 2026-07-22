from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal, cast

from tortoise import fields
from tortoise.fields import Field as TortoiseField
from tortoise.models import Model as TortoiseModel

from app.models.RecordedVideo import RecordedVideo


if TYPE_CHECKING:
    from app.models.CMAnalysis import CMLogoGenerationBatch


AnalysisTaskType = Literal[
    'RecordedScan',
    'MetadataAnalysis',
    'PlaybackIndex',
    'ThumbnailGeneration',
    'CMAnalysis',
    'CMLogoGeneration',
    'BatchScan',
    'BatchMetadataReanalysis',
    'BatchCMAnalysis',
    'BatchSeriesResolution',
    'BatchEpisodeResolution',
    'BackgroundAnalysis',
]
AnalysisTaskStatus = Literal['Queued', 'Running', 'Succeeded', 'Failed', 'Interrupted', 'Skipped']
AnalysisTaskTrigger = Literal['Automatic', 'Manual', 'Maintenance', 'StartupBackfill']


class AnalysisTaskExecution(TortoiseModel):
    """録画解析とメンテナンス処理の構造化された実行履歴を保持する。"""

    class Meta(TortoiseModel.Meta):
        table: str = 'analysis_task_executions'

    id = fields.IntField(pk=True)
    parent: fields.ForeignKeyNullableRelation[AnalysisTaskExecution] = fields.ForeignKeyField(
        'models.AnalysisTaskExecution',
        related_name='children',
        null=True,
        on_delete=fields.CASCADE,
    )
    parent_id: int | None
    recorded_video: fields.ForeignKeyNullableRelation[RecordedVideo] = fields.ForeignKeyField(
        'models.RecordedVideo',
        related_name=None,
        null=True,
        on_delete=fields.SET_NULL,
    )
    recorded_video_id: int | None
    cm_logo_generation_batch: fields.ForeignKeyNullableRelation[CMLogoGenerationBatch] = fields.ForeignKeyField(
        'models.CMLogoGenerationBatch',
        related_name=None,
        null=True,
        on_delete=fields.SET_NULL,
    )
    cm_logo_generation_batch_id: int | None
    task_type = cast(TortoiseField[AnalysisTaskType], fields.CharField(64, db_index=True))
    status = cast(TortoiseField[AnalysisTaskStatus], fields.CharField(32, db_index=True))
    trigger = cast(TortoiseField[AnalysisTaskTrigger], fields.CharField(32))
    title = fields.TextField()
    stage = cast(TortoiseField[str | None], fields.CharField(64, null=True))
    progress = cast(TortoiseField[float | None], fields.FloatField(null=True))
    stage_history = cast(TortoiseField[list[dict[str, object]]], fields.JSONField(default=[]))
    current_count = fields.IntField(default=0)
    total_count = fields.IntField(default=0)
    succeeded_count = fields.IntField(default=0)
    failed_count = fields.IntField(default=0)
    skipped_count = fields.IntField(default=0)
    summary = cast(TortoiseField[dict[str, object] | None], fields.JSONField(null=True))
    error_code = cast(TortoiseField[str | None], fields.CharField(255, null=True))
    error_message = cast(TortoiseField[str | None], fields.TextField(null=True))
    started_at = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    completed_at = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
