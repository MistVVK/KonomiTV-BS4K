from tortoise import fields
from tortoise.models import Model

from app.models.User import User


class KonomiTVBS4KCloudConnectionCheck(Model):
    """rcloneが更新する認証ファイルとは別に、接続確認結果を保持する。"""

    class Meta(Model.Meta):
        table = 'konomitv_bs4k_cloud_connection_checks'

    connection_id = fields.UUIDField(primary_key=True)
    owner: fields.ForeignKeyRelation[User] = fields.ForeignKeyField(
        'models.User', related_name=None, on_delete=fields.CASCADE,
    )
    owner_id: int
    connected = fields.BooleanField()
    checked_at = fields.DatetimeField()
