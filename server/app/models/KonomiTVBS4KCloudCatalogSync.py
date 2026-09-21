import json
from typing import Literal, cast
from uuid import uuid4

from tortoise import fields
from tortoise.fields import Field as TortoiseField
from tortoise.models import Model


class KonomiTVBS4KCloudCatalogSync(Model):
    """接続と領域ごとの目録同期要求・再起動復旧・最終結果を永続化する。"""

    class Meta(Model.Meta):
        table = 'konomitv_bs4k_cloud_catalog_syncs'
        unique_together = (('owner_id', 'connection_id', 'folder'),)

    id = fields.UUIDField(primary_key=True, default=uuid4)
    owner_id = fields.IntField()
    connection_id = fields.UUIDField()
    folder = fields.CharField(1024)
    status = cast(TortoiseField[Literal['Pending', 'Running', 'Completed', 'Failed']], fields.CharField(16, default='Pending'))
    error_code = cast(TortoiseField[str | None], fields.CharField(64, null=True))
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)


class KonomiTVBS4KCloudDeletedRecording(Model):
    """一度観測した削除UUIDを保持し、不完全な後続一覧で録画が復活することを防ぐ。"""

    class Meta(Model.Meta):
        table = 'konomitv_bs4k_cloud_deleted_recordings'

    id = fields.UUIDField(primary_key=True)
    created_at = fields.DatetimeField(auto_now_add=True)


class KonomiTVBS4KCloudPublication(Model):
    """解析履歴IDを冪等キーにし、完成結果の公開要求と固定した公開内容を保持する。"""

    class Meta(Model.Meta):
        table = 'konomitv_bs4k_cloud_publications'

    id = fields.IntField(primary_key=True)
    recorded_video_id = fields.IntField()
    recording_uuid = fields.UUIDField()
    owner_id = fields.IntField()
    connection_id = fields.UUIDField()
    folder = fields.CharField(1024)
    key_identity = fields.CharField(64)
    status = cast(TortoiseField[Literal['Pending', 'Publishing', 'Completed', 'Failed', 'Superseded']],
                  fields.CharField(16, default='Pending'))
    plan = cast(TortoiseField[dict[str, object] | None], fields.JSONField(
        null=True, encoder=lambda value: json.dumps(value, ensure_ascii=False),
    ))
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
