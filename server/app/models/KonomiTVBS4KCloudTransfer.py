import json
from typing import Literal, cast
from uuid import UUID

from tortoise import fields
from tortoise.fields import Field as TortoiseField
from tortoise.models import Model


KonomiTVBS4KCloudTransferDirection = Literal['ToCloud', 'ToLocal']
KonomiTVBS4KCloudTransferStatus = Literal['Pending', 'Running', 'Failed', 'Completed', 'Cancelled']
KonomiTVBS4KCloudTransferPhase = Literal[
    'Preparing', 'Copying', 'Verifying', 'Publishing', 'Cleaning', 'Completed', 'Cancelling', 'Cancelled',
]


class KonomiTVBS4KCloudTransfer(Model):
    """転送先と処理段階を固定し、録画移動の要求・再試行・後処理を永続化する。"""

    class Meta(Model.Meta):
        table = 'konomitv_bs4k_cloud_transfers'

    # 要求IDそのものを主キーにし、再送を逆方向の新しい移動へ解釈しない。
    id = fields.UUIDField(primary_key=True)
    recorded_video_id = fields.IntField(index=True)
    recording_uuid = fields.UUIDField()
    requested_by = fields.IntField()
    # UIの確認revisionは再送の同一性用。実行時は再投入でも変わらない鍵内容のidentityで照合する。
    key_revision = cast(TortoiseField[str | None], fields.CharField(64, null=True))
    key_identity = cast(TortoiseField[str | None], fields.CharField(64, null=True))
    direction = cast(TortoiseField[KonomiTVBS4KCloudTransferDirection], fields.CharField(16))
    # 完了後も履歴は保持する。非NULLの占有値は録画IDに一致させ、一録画につき一移動に限定する。
    active_video_id = cast(TortoiseField[int | None], fields.IntField(null=True, unique=True))
    status = cast(TortoiseField[KonomiTVBS4KCloudTransferStatus], fields.CharField(16, default='Pending', index=True))
    phase = cast(TortoiseField[KonomiTVBS4KCloudTransferPhase], fields.CharField(16, default='Preparing'))
    # 公開開始と同じtransaction境界で記録し、再起動・清掃失敗後も元の転送へ戻さない。
    cancel_requested = fields.BooleanField(default=False)
    owner_id = fields.IntField()
    connection_id = fields.UUIDField()
    folder = fields.CharField(1024)
    # ローカル側の入出力パスも受付時に固定し、設定変更や再試行で別ファイルを清掃しない。
    local_path = fields.TextField()
    # 外部I/O前に確定する対象集合と目録。鍵・OAuth・rclone接続文字列は格納しない。
    manifest = cast(TortoiseField[dict[str, object] | None], fields.JSONField(
        null=True, encoder=lambda value: json.dumps(value, ensure_ascii=False),
    ))
    worker_token = cast(TortoiseField[UUID | None], fields.UUIDField(null=True))
    attempt = fields.IntField(default=0)
    error_code = cast(TortoiseField[str | None], fields.CharField(64, null=True))
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)


class KonomiTVBS4KCloudRecording(Model):
    """クラウドと共有する録画UUIDと、移動ジョブとは独立した現在の所在を保持する。"""

    class Meta(Model.Meta):
        table = 'konomitv_bs4k_cloud_recordings'

    id = fields.UUIDField(primary_key=True)
    # 録画の削除は目録・削除マーカーと協調する所有者が行う。CASCADEで所在だけを黙って失わない。
    recorded_video = fields.OneToOneField('models.RecordedVideo', related_name='cloud_recording', on_delete=fields.RESTRICT)
    recorded_video_id: int
    location = cast(TortoiseField[Literal['Local', 'Cloud']], fields.CharField(16, default='Local'))
    owner_id = fields.IntField()
    connection_id = fields.UUIDField()
    folder = fields.CharField(1024)
    # 初回移動の元パスを保持する。mountの一時的な読取りパスを復元先の正本にしない。
    original_path = fields.TextField()
    key_identity = cast(TortoiseField[str | None], fields.CharField(64, null=True))
    # 最後に受理・公開した共有目録。接続不能時も元の精密日時と解析結果の世代を失わない。
    manifest = cast(TortoiseField[dict[str, object] | None], fields.JSONField(
        null=True, encoder=lambda value: json.dumps(value, ensure_ascii=False),
    ))
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
