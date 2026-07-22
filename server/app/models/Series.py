
# Type Hints を指定できるように
# ref: https://stackoverflow.com/a/33533514/17124142
from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from tortoise import fields
from tortoise.fields import Field as TortoiseField
from tortoise.models import Model as TortoiseModel

from app.schemas import Genre


if TYPE_CHECKING:
    from app.models.RecordedEpisode import SeriesEpisode
    from app.models.SeriesBroadcastPeriod import SeriesBroadcastPeriod


class Series(TortoiseModel):

    # データベース上のテーブル名
    class Meta(TortoiseModel.Meta):
        table: str = 'series'

    id = fields.IntField(pk=True)
    canonical_key = cast(TortoiseField[str | None], fields.CharField(64, null=True, unique=True))
    wikipedia_page_id = cast(TortoiseField[int | None], fields.IntField(null=True, unique=True))
    title = fields.TextField()
    description = fields.TextField()
    genres = cast(TortoiseField[list[Genre]], fields.JSONField(default=[], encoder=lambda x: json.dumps(x, ensure_ascii=False)))  # type: ignore
    episodes: fields.ReverseRelation[SeriesEpisode]
    broadcast_periods: fields.ReverseRelation[SeriesBroadcastPeriod]
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
