
# Type Hints を指定できるように
# ref: https://stackoverflow.com/a/33533514/17124142
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal, cast

from tortoise import fields
from tortoise.fields import Field as TortoiseField
from tortoise.models import Model as TortoiseModel

from app.schemas import Genre


if TYPE_CHECKING:
    from app.models.RecordedEpisode import SeriesEpisode
    from app.models.SeriesBroadcastPeriod import SeriesBroadcastPeriod


# TMDb の作品種別。TV と映画で ID の採番空間が分かれているため、tmdb_id と対で保持する。
TmdbMediaType = Literal['tv', 'movie']


class Series(TortoiseModel):

    # データベース上のテーブル名
    class Meta(TortoiseModel.Meta):
        table: str = 'series'
        # TV と映画の同番号は別作品なので、作品種別と数値 ID の組で一意にする。
        unique_together = (('tmdb_media_type', 'tmdb_id'),)

    id = fields.IntField(pk=True)
    canonical_key = cast(TortoiseField[str | None], fields.CharField(64, null=True, unique=True))
    # SeriesIndexer の完全一致キー。既存 Series は NULL のまま Resolver が canonical_key を埋める。
    normalized_title = cast(TortoiseField[str | None], fields.CharField(512, null=True, unique=True))
    wikipedia_page_id = cast(TortoiseField[int | None], fields.IntField(null=True, unique=True))
    bangumi_subject_id = cast(TortoiseField[int | None], fields.IntField(null=True, unique=True))
    bangumi_subject_name = cast(TortoiseField[str | None], fields.TextField(null=True))
    bangumi_subject_name_cn = cast(TortoiseField[str | None], fields.TextField(null=True))
    bangumi_subject_summary = cast(TortoiseField[str | None], fields.TextField(null=True))
    bangumi_subject_image_url = cast(TortoiseField[str | None], fields.TextField(null=True))
    # TMDb 由来の補完メタデータ。description / genres は Wikipedia AI 生成が正本のため、
    # TMDb の値は必ず tmdb_ 接頭辞の専用カラムへだけ保存する。
    tmdb_id = cast(TortoiseField[int | None], fields.IntField(null=True))
    tmdb_media_type = cast(TortoiseField[TmdbMediaType | None], fields.CharField(8, null=True))
    tmdb_name = cast(TortoiseField[str | None], fields.TextField(null=True))
    tmdb_overview = cast(TortoiseField[str | None], fields.TextField(null=True))
    tmdb_poster_url = cast(TortoiseField[str | None], fields.TextField(null=True))
    tmdb_backdrop_url = cast(TortoiseField[str | None], fields.TextField(null=True))
    title = fields.TextField()
    description = fields.TextField()
    genres = cast(TortoiseField[list[Genre]], fields.JSONField(default=[], encoder=lambda x: json.dumps(x, ensure_ascii=False)))  # type: ignore
    episodes: fields.ReverseRelation[SeriesEpisode]
    broadcast_periods: fields.ReverseRelation[SeriesBroadcastPeriod]
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
