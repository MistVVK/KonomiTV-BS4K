from tortoise import fields
from tortoise.models import Model


class KonomiTVBS4KCloudDestination(Model):
    """認証と分離した保存先と、サーバー全体で一つの新規アップロード先を保持する。"""

    class Meta(Model.Meta):
        table = 'konomitv_bs4k_cloud_destinations'

    # 接続のUUIDを使う。認証解除後も所在の参照先を消さず、再利用もしない。
    connection_id = fields.UUIDField(primary_key=True)
    owner_id = fields.IntField(index=True)
    folder = fields.CharField(max_length=1024)
    # NULLは未選択。選択時の値を1に限定し、unique制約で複数workerからも一つに保つ。
    upload_slot = fields.IntField(null=True, unique=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
