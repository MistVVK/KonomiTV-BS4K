from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal, cast

from tortoise import fields
from tortoise.fields import Field as TortoiseField
from tortoise.models import Model as TortoiseModel


if TYPE_CHECKING:
    from app.models.RecordedProgram import RecordedProgram
    from app.models.Series import Series


RecordedSeriesDecision = Literal['Series', 'NotSeries']
RecordedSeriesResolutionStatus = Literal['Pending', 'Resolved', 'NotSeries', 'NeedsReview', 'Failed']
RecordedSeriesSource = Literal['Rule', 'Local', 'EPG', 'MediaWiki', 'AI', 'Manual']
RecordedSeriesAIRequestPurpose = Literal['Resolution', 'ConnectionTest']
RecordedSeriesAIRequestStatus = Literal['Pending', 'Succeeded', 'Failed', 'Rejected']


class RecordedSeriesRule(TortoiseModel):
    """同一の正規化キーを持つ録画へ再利用するシリーズ判定ルール。"""

    class Meta(TortoiseModel.Meta):
        table: str = 'recorded_series_rules'

    id = fields.IntField(pk=True)
    key_hash = fields.CharField(64, unique=True)
    normalized_key = fields.TextField()
    display_title = fields.TextField()
    decision = cast(TortoiseField[RecordedSeriesDecision], fields.CharField(32))
    series: fields.ForeignKeyNullableRelation[Series] = fields.ForeignKeyField(
        'models.Series',
        related_name='recorded_series_rules',
        null=True,
        on_delete=fields.SET_NULL,
    )
    series_id: int | None
    wikipedia_page_id = cast(TortoiseField[int | None], fields.IntField(null=True))
    source = cast(TortoiseField[RecordedSeriesSource], fields.CharField(32))
    confidence = fields.FloatField()
    evidence_hash = fields.CharField(64)
    resolver_version = fields.CharField(32)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)


class RecordedSeriesResolution(TortoiseModel):
    """録画番組ごとのシリーズ判定状態と判定根拠を保持する。"""

    class Meta(TortoiseModel.Meta):
        table: str = 'recorded_series_resolutions'

    id = fields.IntField(pk=True)
    recorded_program: fields.OneToOneRelation[RecordedProgram] = fields.OneToOneField(
        'models.RecordedProgram',
        related_name='series_resolution',
        on_delete=fields.CASCADE,
    )
    recorded_program_id: int
    series: fields.ForeignKeyNullableRelation[Series] = fields.ForeignKeyField(
        'models.Series',
        related_name='recorded_series_resolutions',
        null=True,
        on_delete=fields.SET_NULL,
    )
    series_id: int | None
    input_fingerprint = fields.CharField(64, index=True)
    evidence_hash = fields.CharField(64)
    resolver_version = fields.CharField(32)
    normalized_title = fields.TextField()
    status = cast(TortoiseField[RecordedSeriesResolutionStatus], fields.CharField(32, index=True))
    source = cast(TortoiseField[RecordedSeriesSource | None], fields.CharField(32, null=True))
    wikipedia_page_id = cast(TortoiseField[int | None], fields.IntField(null=True))
    candidate_set_hash = cast(TortoiseField[str | None], fields.CharField(64, null=True))
    candidate_snapshot = cast(
        TortoiseField[dict[str, object] | list[object] | None],
        fields.JSONField(null=True),
    )
    ai_model = cast(TortoiseField[str | None], fields.TextField(null=True))
    error_code = cast(TortoiseField[str | None], fields.CharField(255, null=True))
    error_message = cast(TortoiseField[str | None], fields.TextField(null=True))
    resolved_at = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)


class RecordedSeriesAIRequest(TortoiseModel):
    """OpenAI 互換 API の呼び出し回数と結果の監査情報を保持する。"""

    class Meta(TortoiseModel.Meta):
        table: str = 'recorded_series_ai_requests'

    id = fields.IntField(pk=True)
    resolution: fields.ForeignKeyNullableRelation[RecordedSeriesResolution] = fields.ForeignKeyField(
        'models.RecordedSeriesResolution',
        related_name='ai_requests',
        null=True,
        on_delete=fields.SET_NULL,
    )
    resolution_id: int | None
    purpose = cast(TortoiseField[RecordedSeriesAIRequestPurpose], fields.CharField(32))
    status = cast(TortoiseField[RecordedSeriesAIRequestStatus], fields.CharField(32))
    model = fields.TextField()
    input_fingerprint = cast(TortoiseField[str | None], fields.CharField(64, null=True))
    candidate_set_hash = cast(TortoiseField[str | None], fields.CharField(64, null=True))
    candidate_ids = cast(TortoiseField[list[str]], fields.JSONField(default=[]))
    selected_choice_id = cast(TortoiseField[str | None], fields.TextField(null=True))
    prompt_tokens = cast(TortoiseField[int | None], fields.IntField(null=True))
    completion_tokens = cast(TortoiseField[int | None], fields.IntField(null=True))
    http_status = cast(TortoiseField[int | None], fields.IntField(null=True))
    latency_ms = cast(TortoiseField[int | None], fields.IntField(null=True))
    error_code = cast(TortoiseField[str | None], fields.CharField(255, null=True))
    created_at = fields.DatetimeField(auto_now_add=True)
