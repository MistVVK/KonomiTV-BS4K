
# Type Hints を指定できるように
# ref: https://stackoverflow.com/a/33533514/17124142
from __future__ import annotations

import json
from datetime import datetime
from typing import Literal, cast

from tortoise import fields
from tortoise.fields import Field as TortoiseField
from tortoise.models import Model as TortoiseModel

from app.models.RecordedProgram import RecordedProgram
from app.schemas import (
    AudioTrack,
    AudioTrackTimelineEntry,
    CMSection,
    KeyFrame,
    SegmentMapEntry,
    SubtitleTrack,
    ThumbnailInfo,
)


class RecordedVideo(TortoiseModel):

    # データベース上のテーブル名
    class Meta(TortoiseModel.Meta):
        table: str = 'recorded_videos'

    id = fields.IntField(pk=True)
    recorded_program: fields.OneToOneRelation[RecordedProgram] = \
        fields.OneToOneField('models.RecordedProgram', related_name='recorded_video', on_delete=fields.CASCADE)
    recorded_program_id: int
    status = cast(TortoiseField[Literal['Recording', 'Recorded', 'AnalysisFailed']], fields.CharField(255, db_index=True))
    file_path = fields.TextField()  # ファイルパスは可変長だが、TextField には unique 制約が付けられない
    file_hash = fields.TextField()
    file_size = fields.IntField()
    file_created_at = fields.DatetimeField()
    file_modified_at = fields.DatetimeField()
    analyzed_at = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    analysis_git_commit = cast(TortoiseField[str | None], fields.CharField(255, null=True))
    recording_start_time = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    recording_end_time = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    duration = fields.FloatField()
    container_format = fields.CharField(255)
    has_video = fields.BooleanField(default=True)
    has_audio = fields.BooleanField(default=True)
    video_codec = cast(TortoiseField[str | None], fields.CharField(255, null=True))
    video_codec_profile = cast(TortoiseField[str | None], fields.CharField(255, null=True))
    video_scan_type = cast(TortoiseField[Literal['Interlaced', 'Progressive'] | None], fields.CharField(255, null=True))
    video_frame_rate = cast(TortoiseField[float | None], fields.FloatField(null=True))
    video_resolution_width = cast(TortoiseField[int | None], fields.IntField(null=True))
    video_resolution_height = cast(TortoiseField[int | None], fields.IntField(null=True))
    has_video_stream_changes = fields.BooleanField(default=False)
    primary_audio_codec = cast(TortoiseField[str | None], fields.CharField(255, null=True))
    primary_audio_channel = cast(TortoiseField[str | None], fields.CharField(255, null=True))
    primary_audio_sampling_rate = cast(TortoiseField[int | None], fields.IntField(null=True))
    secondary_audio_codec = cast(TortoiseField[str | None], fields.CharField(255, null=True))
    secondary_audio_channel = cast(TortoiseField[str | None], fields.CharField(255, null=True))
    secondary_audio_sampling_rate = cast(TortoiseField[int | None], fields.IntField(null=True))
    audio_tracks = cast(TortoiseField[list[AudioTrack]],
        fields.JSONField(default=[], encoder=lambda x: json.dumps(x, ensure_ascii=False)))  # type: ignore
    audio_track_timeline = cast(TortoiseField[list[AudioTrackTimelineEntry]],
        fields.JSONField(default=[], encoder=lambda x: json.dumps(x, ensure_ascii=False)))  # type: ignore
    subtitle_tracks = cast(TortoiseField[list[SubtitleTrack]],
        fields.JSONField(default=[], encoder=lambda x: json.dumps(x, ensure_ascii=False)))  # type: ignore
    key_frames = cast(TortoiseField[list[KeyFrame]],
        fields.JSONField(default=[], encoder=lambda x: json.dumps(x, ensure_ascii=False)))  # type: ignore
    segment_map = cast(TortoiseField[list[SegmentMapEntry]],
        # segment_map は再生開始時刻から入力ファイル位置を引くためのキャッシュ
        ## 空配列は未キャッシュ状態を表し、再生可否の判定には使わない
        fields.JSONField(default=[], encoder=lambda x: json.dumps(x, ensure_ascii=False)))  # type: ignore
    # MPEG-TS の録画先頭にある映像基準 DTS。シークや音声レンディション生成のたびに再走査しないため保持する。
    ts_source_base_dts = cast(TortoiseField[int | None], fields.BigIntField(null=True))
    cm_sections = cast(TortoiseField[list[CMSection] | None],
        # None は未解析状態を表す ([] は解析したが CM 区間がなかった/検出に失敗したことを表す)
        fields.JSONField(default=None, encoder=lambda x: json.dumps(x, ensure_ascii=False), null=True))  # type: ignore
    thumbnail_info = cast(TortoiseField[ThumbnailInfo | None],
        # None はサムネイル未生成か、旧仕様から移行しきれていないことを表す
        fields.JSONField(default=None, encoder=lambda x: json.dumps(x, ensure_ascii=False), null=True))  # type: ignore
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
