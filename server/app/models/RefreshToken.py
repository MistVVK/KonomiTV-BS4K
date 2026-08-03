from tortoise import fields, timezone
from tortoise.models import Model


class RefreshToken(Model):

    # データベース上のテーブル名
    class Meta(Model.Meta):
        table: str = 'refresh_tokens'

    id = fields.IntField(pk=True)
    token_hash = fields.CharField(max_length=64, unique=True)
    family_id = fields.CharField(max_length=64, index=True)
    user = fields.ForeignKeyField('models.User', related_name='refresh_tokens', on_delete=fields.CASCADE)
    expires_at = fields.DatetimeField()
    revoked_at = fields.DatetimeField(null=True)
    replaced_by_hash = fields.CharField(max_length=64, null=True)


    @classmethod
    async def cleanupExpired(cls) -> None:
        """期限切れの更新トークンを削除する。"""

        await cls.filter(expires_at__lte=timezone.now()).delete()
