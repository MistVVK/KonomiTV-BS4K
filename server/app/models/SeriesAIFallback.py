from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from tortoise import fields
from tortoise.fields import Field as TortoiseField
from tortoise.models import Model as TortoiseModel


if TYPE_CHECKING:
    from app.models.Series import Series


SeriesAIFallbackStatus = Literal[
    'Pending',
    'Resolved',
    'NotSeries',
    'InsufficientEvidence',
    'Failed',
    'Cancelled',
]


class SeriesAIFallback(TortoiseModel):
    """Indexer 未所属の同一 EPG タイトル群に対する AI 判定を保持する。"""

    class Meta(TortoiseModel.Meta):
        table: str = 'series_ai_fallbacks'

    # 同一 EPG タイトル系を1回の Web 検索へ束ねる完全一致キー。
    grouping_key = fields.CharField(512, pk=True)
    input_fingerprint = fields.CharField(64)
    provider_fingerprint = fields.CharField(64)
    status = cast(TortoiseField[SeriesAIFallbackStatus], fields.CharField(32, index=True))
    series: fields.ForeignKeyNullableRelation[Series] = fields.ForeignKeyField(
        'models.Series',
        related_name=None,
        null=True,
        on_delete=fields.SET_NULL,
    )
    series_id: int | None
    series_title = cast(TortoiseField[str | None], fields.TextField(null=True))
    normalized_title = cast(TortoiseField[str | None], fields.CharField(512, null=True))
    web_search_performed = fields.BooleanField(default=False)
    citations = cast(TortoiseField[list[dict[str, str]]], fields.JSONField(default=[]))
    rationale_short = cast(TortoiseField[str | None], fields.TextField(null=True))
    ai_model = cast(TortoiseField[str | None], fields.CharField(255, null=True))
    prompt_tokens = cast(TortoiseField[int | None], fields.IntField(null=True))
    completion_tokens = cast(TortoiseField[int | None], fields.IntField(null=True))
    http_status = cast(TortoiseField[int | None], fields.IntField(null=True))
    latency_ms = cast(TortoiseField[int | None], fields.IntField(null=True))
    # 主系・回復系を含む試行列だけを保持し、生プロンプトや応答は保存しない。
    attempt_summaries = cast(TortoiseField[list[str]], fields.JSONField(default=[]))
    error_code = cast(TortoiseField[str | None], fields.CharField(255, null=True))
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
