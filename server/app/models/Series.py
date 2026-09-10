
# Type Hints を指定できるように
# ref: https://stackoverflow.com/a/33533514/17124142
from __future__ import annotations

import json
from datetime import date, datetime
from typing import TYPE_CHECKING, Literal, cast

from tortoise import fields
from tortoise.fields import Field as TortoiseField
from tortoise.models import Model as TortoiseModel

from app.schemas import Genre


if TYPE_CHECKING:
    from app.models.RecordedEpisode import SeriesEpisode
    from app.models.RecordedProgram import RecordedProgram
    from app.models.SeriesBroadcastPeriod import SeriesBroadcastPeriod


# TMDb の作品種別。TV と映画で ID の採番空間が分かれているため、tmdb_id と対で保持する。
TmdbMediaType = Literal['tv', 'movie']
# 初放送日 (first_air_date) の由来。TMDb → Bangumi → ローカル放送開始日の優先順を判定するために使う。
FirstAirDateSource = Literal['Tmdb', 'Bangumi', 'Local']


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
    # 未照合 Series の有限バッチを未試行・最久試行順へ回すため、直近の試行開始時刻を保持する。
    bangumi_last_attempt_at = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    bangumi_subject_name = cast(TortoiseField[str | None], fields.TextField(null=True))
    bangumi_subject_name_cn = cast(TortoiseField[str | None], fields.TextField(null=True))
    bangumi_subject_summary = cast(TortoiseField[str | None], fields.TextField(null=True))
    bangumi_subject_image_url = cast(TortoiseField[str | None], fields.TextField(null=True))
    # Bangumi 条目のレーティング (rating.score)。照合確定時に一度だけ保存する。
    bangumi_rating = cast(TortoiseField[float | None], fields.FloatField(null=True))
    # TMDb 由来の補完メタデータ。description / genres は Wikipedia AI 生成が正本のため、
    # TMDb の値は必ず tmdb_ 接頭辞の専用カラムへだけ保存する。
    tmdb_id = cast(TortoiseField[int | None], fields.IntField(null=True))
    # 照合不能な先頭 Series に滞留せず、次の有限バッチで対象を交代するために使う。
    tmdb_last_attempt_at = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    tmdb_media_type = cast(TortoiseField[TmdbMediaType | None], fields.CharField(8, null=True))
    # binding 後に中断した enrich を、未照合キューと分けて通常同期が再試行するために保持する。
    tmdb_enrichment_pending = fields.BooleanField(default=False)
    tmdb_name = cast(TortoiseField[str | None], fields.TextField(null=True))
    tmdb_overview = cast(TortoiseField[str | None], fields.TextField(null=True))
    tmdb_poster_url = cast(TortoiseField[str | None], fields.TextField(null=True))
    tmdb_backdrop_url = cast(TortoiseField[str | None], fields.TextField(null=True))
    # TMDb の人気度と評価。enrich 時に一度だけ保存し、定期再取得はしない。
    tmdb_popularity = cast(TortoiseField[float | None], fields.FloatField(null=True))
    tmdb_vote_average = cast(TortoiseField[float | None], fields.FloatField(null=True))
    # 初放送日 (日付のみ)。TMDb → Bangumi → ローカル最古放送日の優先順で保存する。
    first_air_date = cast(TortoiseField[date | None], fields.DateField(null=True))
    # 初放送日の由来。binding や非 NULL の推測ではなく実際の書き込み元を記録する (NULL は未確定)。
    first_air_date_source = cast(TortoiseField[FirstAirDateSource | None], fields.CharField(8, null=True))
    # タイトルのかな読み (ソートキー)。AI 生成をひらがなへ正規化して保存し、既存値は上書きしない。
    title_reading = cast(TortoiseField[str | None], fields.TextField(null=True))
    title = fields.TextField()
    description = fields.TextField()
    genres = cast(TortoiseField[list[Genre]], fields.JSONField(default=[], encoder=lambda x: json.dumps(x, ensure_ascii=False)))  # type: ignore
    recorded_programs: fields.ReverseRelation[RecordedProgram]
    episodes: fields.ReverseRelation[SeriesEpisode]
    broadcast_periods: fields.ReverseRelation[SeriesBroadcastPeriod]
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
