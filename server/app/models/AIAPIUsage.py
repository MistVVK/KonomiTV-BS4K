"""OpenCode Metered / Local 向けの月次利用台帳モデル。

service_id × Asia/Tokyo 暦月ごとの確定値と予約値を保持する。
service 削除後も履歴を残すため FK は張らず、表示用 snapshot を持つ。
"""

from __future__ import annotations

from decimal import Decimal

from tortoise import fields
from tortoise.models import Model as TortoiseModel


class AIAPIUsageMonth(TortoiseModel):
    """1 service・1 暦月の利用量集約行。"""

    class Meta(TortoiseModel.Meta):
        table: str = 'ai_api_usage_months'
        unique_together = (('service_id', 'year_month'),)

    id = fields.IntField(pk=True)
    # AIBackendSettings の service_id（UUID）。削除後も台帳に残す。
    service_id = fields.CharField(36, index=True)
    # Asia/Tokyo の YYYY-MM。
    year_month = fields.CharField(7, index=True)
    # 精算済み token / 推定 cost。
    settled_prompt_tokens = fields.IntField(default=0)
    settled_completion_tokens = fields.IntField(default=0)
    settled_total_tokens = fields.IntField(default=0)
    settled_estimated_cost_usd = fields.DecimalField(
        max_digits=16,
        decimal_places=8,
        default=Decimal('0'),
    )
    # 進行中呼び出しの予約。プロセス再起動時に Ledger が 0 へ戻す。
    reserved_total_tokens = fields.IntField(default=0)
    reserved_estimated_cost_usd = fields.DecimalField(
        max_digits=16,
        decimal_places=8,
        default=Decimal('0'),
    )
    settled_request_count = fields.IntField(default=0)
    # service 削除後の一覧表示用 snapshot（秘密を含めない）。
    service_name_snapshot = fields.CharField(128)
    opencode_provider_id_snapshot = fields.CharField(64)
    opencode_model_id_snapshot = fields.CharField(255)
    billing_mode_snapshot = fields.CharField(32)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
