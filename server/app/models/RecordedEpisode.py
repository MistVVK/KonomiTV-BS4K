from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, cast

from tortoise import fields
from tortoise.fields import Field as TortoiseField
from tortoise.models import Model as TortoiseModel

from app.metadata.ai.episode_lookup import (
    EpisodeLookupOutcome,
    ModelEpisodeLookupOutcome,
)


if TYPE_CHECKING:
    from app.models.RecordedProgram import RecordedProgram
    from app.models.Series import Series


RecordedEpisodeResolutionStatus = Literal[
    'Pending',
    'Resolved',
    'Unknown',
    'NotNumbered',
    'NoPublishedNumber',
    'NeedsReview',
    'Failed',
]
RecordedEpisodeSource = Literal['Local', 'EPG', 'WebSearch', 'Manual', 'Migration', 'AI']


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
    # 現在の正本シーズン。番号付き回では episode.season_number と一致させ、
    # 公開話数なしでは episode_id を作らずシーズン所属だけを保持する。
    season_number = cast(TortoiseField[int | None], fields.IntField(null=True))
    status = cast(
        TortoiseField[RecordedEpisodeResolutionStatus], fields.CharField(32, index=True)
    )
    # 現在の正本（再生・一覧で使う採用元）。Manual / WebSearch など。
    source = cast(
        TortoiseField[RecordedEpisodeSource | None], fields.CharField(32, null=True)
    )
    lookup_outcome = cast(
        TortoiseField[EpisodeLookupOutcome | None], fields.CharField(32, null=True)
    )
    input_fingerprint = cast(
        TortoiseField[str | None], fields.CharField(64, null=True, index=True)
    )
    provider_fingerprint = cast(
        TortoiseField[str | None], fields.CharField(64, null=True)
    )
    # AI レーン。手動確定しても残し、単票再検索や「AI を採用」で参照する。
    proposed_outcome = cast(
        TortoiseField[ModelEpisodeLookupOutcome | None], fields.CharField(32, null=True)
    )
    proposed_season_number = cast(TortoiseField[int | None], fields.IntField(null=True))
    proposed_episode_number = cast(
        TortoiseField[Decimal | None],
        fields.DecimalField(max_digits=10, decimal_places=3, null=True),
    )
    # 手動レーン。AI 採用後も残し、再び手動へ戻せる。
    manual_episode: fields.ForeignKeyNullableRelation[SeriesEpisode] = (
        fields.ForeignKeyField(
            'models.SeriesEpisode',
            related_name='manual_recorded_episode_resolutions',
            null=True,
            on_delete=fields.SET_NULL,
        )
    )
    manual_episode_id: int | None
    manual_season_number = cast(TortoiseField[int | None], fields.IntField(null=True))
    manual_episode_number = cast(
        TortoiseField[Decimal | None],
        fields.DecimalField(max_digits=10, decimal_places=3, null=True),
    )
    manual_status = cast(
        TortoiseField[
            Literal['Resolved', 'Unknown', 'NotNumbered', 'NoPublishedNumber'] | None
        ],
        fields.CharField(32, null=True),
    )
    confidence = cast(TortoiseField[float | None], fields.FloatField(null=True))
    web_search_performed = fields.BooleanField(default=False)
    rationale_short = cast(TortoiseField[str | None], fields.TextField(null=True))
    is_legacy_recording = fields.BooleanField(default=False)
    citations = cast(TortoiseField[list[dict[str, str]]], fields.JSONField(default=[]))
    ai_model = cast(TortoiseField[str | None], fields.TextField(null=True))
    error_code = cast(TortoiseField[str | None], fields.CharField(255, null=True))
    error_message = cast(TortoiseField[str | None], fields.TextField(null=True))
    resolved_at = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
