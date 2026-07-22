from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, cast

from tortoise import fields
from tortoise.fields import Field as TortoiseField
from tortoise.models import Model as TortoiseModel


if TYPE_CHECKING:
    from app.models.RecordedProgram import RecordedProgram
    from app.models.Series import Series


RecordedEpisodeResolutionStatus = Literal[
    'Pending',
    'Resolved',
    'Unknown',
    'NotNumbered',
    'NeedsReview',
    'Failed',
]
RecordedEpisodeSource = Literal['Local', 'EPG', 'WebSearch', 'Manual', 'Migration']


class SeriesEpisode(TortoiseModel):
    """Series内で共有する構造化済みのシーズン・話数を保持する。"""

    class Meta(TortoiseModel.Meta):
        table: str = 'series_episodes'
        unique_together = (('series', 'season_number', 'episode_number'),)

    id = fields.IntField(pk=True)
    series: fields.ForeignKeyRelation[Series] = fields.ForeignKeyField(
        'models.Series',
        related_name='episodes',
        on_delete=fields.CASCADE,
    )
    series_id: int
    season_number = fields.IntField()
    episode_number = fields.DecimalField(max_digits=10, decimal_places=3)
    recorded_programs: fields.ReverseRelation[RecordedProgram]
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)


class RecordedEpisodeResolution(TortoiseModel):
    """録画ごとの話数判定状態・提案・根拠をEpisode本体から分離して保持する。"""

    class Meta(TortoiseModel.Meta):
        table: str = 'recorded_episode_resolutions'

    id = fields.IntField(pk=True)
    recorded_program: fields.OneToOneRelation[RecordedProgram] = fields.OneToOneField(
        'models.RecordedProgram',
        related_name='episode_resolution',
        on_delete=fields.CASCADE,
    )
    recorded_program_id: int
    episode: fields.ForeignKeyNullableRelation[SeriesEpisode] = fields.ForeignKeyField(
        'models.SeriesEpisode',
        related_name='recorded_episode_resolutions',
        null=True,
        on_delete=fields.SET_NULL,
    )
    episode_id: int | None
    status = cast(TortoiseField[RecordedEpisodeResolutionStatus], fields.CharField(32, index=True))
    source = cast(TortoiseField[RecordedEpisodeSource | None], fields.CharField(32, null=True))
    input_fingerprint = cast(TortoiseField[str | None], fields.CharField(64, null=True, index=True))
    provider_fingerprint = cast(TortoiseField[str | None], fields.CharField(64, null=True))
    proposed_season_number = cast(TortoiseField[int | None], fields.IntField(null=True))
    proposed_episode_number = cast(
        TortoiseField[Decimal | None],
        fields.DecimalField(max_digits=10, decimal_places=3, null=True),
    )
    confidence = cast(TortoiseField[float | None], fields.FloatField(null=True))
    web_search_performed = fields.BooleanField(default=False)
    is_legacy_recording = fields.BooleanField(default=False)
    citations = cast(TortoiseField[list[dict[str, str]]], fields.JSONField(default=[]))
    ai_model = cast(TortoiseField[str | None], fields.TextField(null=True))
    error_code = cast(TortoiseField[str | None], fields.CharField(255, null=True))
    error_message = cast(TortoiseField[str | None], fields.TextField(null=True))
    resolved_at = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
