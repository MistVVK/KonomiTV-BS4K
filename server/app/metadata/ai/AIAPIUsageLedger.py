"""OpenCode service 向け月次利用台帳（予約・精算・上限判定）。

Asia/Tokyo 暦月 × service_id で集約する。
Subscription は enforce せず no-op。Local は cost 上限を無視し token のみ。
Metered は cost（取得可能なとき）と token の両方を見る。
プロセス再起動時は reserved を 0 に戻す（進行中呼び出しは失効とみなす）。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from tortoise.transactions import in_transaction

from app import logging
from app.constants import JST
from app.metadata.ai.AIBackendSettings import AIBackendService
from app.metadata.ai.opencode_types import OpenCodeNormalizedUsage
from app.metadata.RecordedSeriesCandidates import RecordedSeriesAIError
from app.models.AIAPIUsage import AIAPIUsageMonth


# 1 回の OpenCode 呼び出しに対する安全な予約上界。
DEFAULT_RESERVE_TOTAL_TOKENS = 100_000
DEFAULT_RESERVE_COST_USD = Decimal('0.50')

_ZERO = Decimal('0')
_reserve_lock = asyncio.Lock()
# 起動時 reserved リセットを一度だけ行う。
_startup_reset_done = False
_startup_reset_lock = asyncio.Lock()


def CurrentYearMonth(*, now: datetime | None = None) -> str:
    """Asia/Tokyo の現在暦月を YYYY-MM で返す。

    Args:
        now: 省略時は現在時刻。

    Returns:
        例: '2026-08'。
    """

    moment = now.astimezone(JST) if now is not None else datetime.now(tz=JST)
    return f'{moment.year:04d}-{moment.month:02d}'


def IsCostLimitEffective(billing_mode: str, monthly_cost_limit_usd: Decimal | None) -> bool:
    """cost 上限を enforce すべきか。

    Metered かつ limit が設定されているときのみ True。
    実際の精算で cost が取れない呼び出しでは上限判定に cost を使わない。

    Args:
        billing_mode: Metered / Subscription / Local。
        monthly_cost_limit_usd: 設定上の料金上限。

    Returns:
        cost 上限が意味を持つか。
    """

    return billing_mode == 'Metered' and monthly_cost_limit_usd is not None


@dataclass(frozen=True, slots=True)
class AIAPIUsageReservation:
    """1 回の外部呼び出しに対する予約ハンドル。"""

    reservation_id: str
    service_id: str
    year_month: str
    billing_mode: str
    # Subscription 等で台帳を触らないとき True。
    skipped: bool
    reserved_total_tokens: int
    reserved_estimated_cost_usd: Decimal


@dataclass(frozen=True, slots=True)
class AIAPIUsageMonthSnapshot:
    """管理 API / UI 向けの当月スナップショット。"""

    service_id: str
    year_month: str
    billing_mode: str
    settled_prompt_tokens: int
    settled_completion_tokens: int
    settled_total_tokens: int
    settled_estimated_cost_usd: Decimal
    reserved_total_tokens: int
    reserved_estimated_cost_usd: Decimal
    settled_request_count: int
    monthly_token_limit: int | None
    monthly_cost_limit_usd: Decimal | None
    cost_limit_effective: bool
    token_limit_reached: bool
    cost_limit_reached: bool
    service_name_snapshot: str
    opencode_provider_id_snapshot: str
    opencode_model_id_snapshot: str
    # 登録設定から消えた service の履歴行か（5c 一覧用）。
    service_deleted: bool = False


def _asDecimal(value: Decimal | float | int | str | None) -> Decimal:
    """Decimal へ安全に変換する。"""

    if value is None:
        return _ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class AIAPIUsageLedger:
    """月次利用台帳の操作入口。"""

    @classmethod
    async def EnsureStartupReservedReset(cls) -> None:
        """起動時に全月の reserved を 0 へ戻す（1 プロセス 1 回）。"""

        global _startup_reset_done
        if _startup_reset_done:
            return
        async with _startup_reset_lock:
            if _startup_reset_done:
                return
            try:
                updated = await AIAPIUsageMonth.filter(
                    reserved_total_tokens__gt=0,
                ).update(
                    reserved_total_tokens=0,
                    reserved_estimated_cost_usd=_ZERO,
                )
                # cost だけ残っている行も掃除
                updated += await AIAPIUsageMonth.filter(
                    reserved_total_tokens=0,
                    reserved_estimated_cost_usd__gt=_ZERO,
                ).update(
                    reserved_estimated_cost_usd=_ZERO,
                )
                if updated:
                    logging.info(
                        f'[AIAPIUsageLedger] Cleared stale reserved usage on {updated} month row(s).',
                    )
            except Exception as error:
                # DB 未初期化などでは警告のみ（本体起動を止めない）。
                logging.warning(
                    f'[AIAPIUsageLedger] Failed to reset reserved usage: {error}',
                )
            _startup_reset_done = True

    @classmethod
    async def Reserve(
        cls,
        service: AIBackendService,
        *,
        estimate_total_tokens: int = DEFAULT_RESERVE_TOTAL_TOKENS,
        estimate_cost_usd: Decimal = DEFAULT_RESERVE_COST_USD,
        year_month: str | None = None,
    ) -> AIAPIUsageReservation:
        """外部呼び出し前に月次枠を予約する。

        Args:
            service: OpenCode service 定義。
            estimate_total_tokens: 予約する token 上界。
            estimate_cost_usd: 予約する推定 cost 上界。
            year_month: 省略時は当月。

        Returns:
            予約ハンドル。Subscription は skipped=True。

        Raises:
            RecordedSeriesAIError: 上限超過。
        """

        await cls.EnsureStartupReservedReset()
        month = year_month or CurrentYearMonth()
        billing = service.billing_mode
        if billing == 'Subscription':
            return AIAPIUsageReservation(
                reservation_id=str(uuid.uuid4()),
                service_id=service.service_id,
                year_month=month,
                billing_mode=billing,
                skipped=True,
                reserved_total_tokens=0,
                reserved_estimated_cost_usd=_ZERO,
            )

        tokens = max(0, int(estimate_total_tokens))
        cost = _asDecimal(estimate_cost_usd)
        if cost < _ZERO:
            cost = _ZERO
        # Local は cost 上限対象外。予約 cost も 0 にする。
        if billing == 'Local':
            cost = _ZERO
        # Metered で cost 上限が無いときも予約 cost は記録してよいが enforce しない。
        enforce_cost = IsCostLimitEffective(billing, service.monthly_cost_limit_usd)
        enforce_token = (
            billing in {'Metered', 'Local'} and service.monthly_token_limit is not None
        )

        async with _reserve_lock:
            async with in_transaction() as connection:
                row = await AIAPIUsageMonth.filter(
                    service_id=service.service_id,
                    year_month=month,
                ).using_db(connection).first()
                if row is None:
                    row = await AIAPIUsageMonth.create(
                        service_id=service.service_id,
                        year_month=month,
                        service_name_snapshot=service.service_name,
                        opencode_provider_id_snapshot=service.opencode_provider_id,
                        opencode_model_id_snapshot=service.opencode_model_id,
                        billing_mode_snapshot=billing,
                        using_db=connection,
                    )
                else:
                    # snapshot を最新表示名へ追随
                    row.service_name_snapshot = service.service_name
                    row.opencode_provider_id_snapshot = service.opencode_provider_id
                    row.opencode_model_id_snapshot = service.opencode_model_id
                    row.billing_mode_snapshot = billing

                projected_tokens = (
                    int(row.settled_total_tokens)
                    + int(row.reserved_total_tokens)
                    + tokens
                )
                projected_cost = (
                    _asDecimal(row.settled_estimated_cost_usd)
                    + _asDecimal(row.reserved_estimated_cost_usd)
                    + cost
                )

                if enforce_token and service.monthly_token_limit is not None:
                    if projected_tokens > int(service.monthly_token_limit):
                        raise RecordedSeriesAIError(
                            'MonthlyTokenLimitReached',
                            http_status=429,
                        )
                if enforce_cost and service.monthly_cost_limit_usd is not None:
                    if projected_cost > _asDecimal(service.monthly_cost_limit_usd):
                        raise RecordedSeriesAIError(
                            'MonthlyCostLimitReached',
                            http_status=429,
                        )

                row.reserved_total_tokens = int(row.reserved_total_tokens) + tokens
                row.reserved_estimated_cost_usd = (
                    _asDecimal(row.reserved_estimated_cost_usd) + cost
                )
                await row.save(
                    update_fields=[
                        'reserved_total_tokens',
                        'reserved_estimated_cost_usd',
                        'service_name_snapshot',
                        'opencode_provider_id_snapshot',
                        'opencode_model_id_snapshot',
                        'billing_mode_snapshot',
                        'updated_at',
                    ],
                    using_db=connection,
                )

        return AIAPIUsageReservation(
            reservation_id=str(uuid.uuid4()),
            service_id=service.service_id,
            year_month=month,
            billing_mode=billing,
            skipped=False,
            reserved_total_tokens=tokens,
            reserved_estimated_cost_usd=cost,
        )

    @classmethod
    async def Settle(
        cls,
        reservation: AIAPIUsageReservation,
        usage: OpenCodeNormalizedUsage | None = None,
        *,
        free_failure: bool = False,
        unknown_interrupt: bool = False,
    ) -> None:
        """予約を精算または解放する。

        Args:
            reservation: Reserve の戻り値。
            usage: 実 usage。free_failure 時は無視。
            free_failure: 課金なし確定（予約解放のみ）。
            unknown_interrupt: 不明中断。予約額を settled へ振り替える。
        """

        if reservation.skipped:
            return

        async with _reserve_lock:
            async with in_transaction() as connection:
                row = await AIAPIUsageMonth.filter(
                    service_id=reservation.service_id,
                    year_month=reservation.year_month,
                ).using_db(connection).first()
                if row is None:
                    logging.warning(
                        '[AIAPIUsageLedger] Settle target month row missing '
                        f'(service={reservation.service_id}, month={reservation.year_month}).',
                    )
                    return

                # 予約を戻す（下限 0）。
                row.reserved_total_tokens = max(
                    0,
                    int(row.reserved_total_tokens) - int(reservation.reserved_total_tokens),
                )
                row.reserved_estimated_cost_usd = max(
                    _ZERO,
                    _asDecimal(row.reserved_estimated_cost_usd)
                    - _asDecimal(reservation.reserved_estimated_cost_usd),
                )

                if free_failure:
                    await row.save(
                        update_fields=[
                            'reserved_total_tokens',
                            'reserved_estimated_cost_usd',
                            'updated_at',
                        ],
                        using_db=connection,
                    )
                    return

                if unknown_interrupt:
                    # 安全側: 予約見積を確定へ。
                    add_tokens = int(reservation.reserved_total_tokens)
                    add_cost = _asDecimal(reservation.reserved_estimated_cost_usd)
                    add_prompt = 0
                    add_completion = add_tokens
                elif usage is not None:
                    add_prompt = max(0, int(usage.get('prompt_tokens') or 0))
                    add_completion = max(0, int(usage.get('completion_tokens') or 0))
                    reasoning = max(0, int(usage.get('reasoning_tokens') or 0))
                    add_tokens = max(
                        0,
                        int(usage.get('total_tokens') or (add_prompt + add_completion + reasoning)),
                    )
                    raw_cost = usage.get('estimated_cost_usd')
                    add_cost = _asDecimal(raw_cost) if raw_cost is not None else _ZERO
                else:
                    # usage 無し・free でも unknown でもない → 予約解放のみ
                    await row.save(
                        update_fields=[
                            'reserved_total_tokens',
                            'reserved_estimated_cost_usd',
                            'updated_at',
                        ],
                        using_db=connection,
                    )
                    return

                if reservation.billing_mode == 'Local':
                    add_cost = _ZERO

                row.settled_prompt_tokens = int(row.settled_prompt_tokens) + add_prompt
                row.settled_completion_tokens = int(row.settled_completion_tokens) + add_completion
                row.settled_total_tokens = int(row.settled_total_tokens) + add_tokens
                row.settled_estimated_cost_usd = (
                    _asDecimal(row.settled_estimated_cost_usd) + add_cost
                )
                row.settled_request_count = int(row.settled_request_count) + 1
                await row.save(
                    update_fields=[
                        'reserved_total_tokens',
                        'reserved_estimated_cost_usd',
                        'settled_prompt_tokens',
                        'settled_completion_tokens',
                        'settled_total_tokens',
                        'settled_estimated_cost_usd',
                        'settled_request_count',
                        'updated_at',
                    ],
                    using_db=connection,
                )

    @classmethod
    async def GetMonthSnapshot(
        cls,
        service: AIBackendService,
        *,
        year_month: str | None = None,
    ) -> AIAPIUsageMonthSnapshot:
        """service の当月（または指定月）スナップショットを返す。

        Args:
            service: service 定義（上限判定に使用）。
            year_month: 省略時は当月。

        Returns:
            集約値と上限状態。行が無ければ 0 埋め。
        """

        await cls.EnsureStartupReservedReset()
        month = year_month or CurrentYearMonth()
        row = await AIAPIUsageMonth.filter(
            service_id=service.service_id,
            year_month=month,
        ).first()
        settled_tokens = int(row.settled_total_tokens) if row is not None else 0
        settled_cost = _asDecimal(row.settled_estimated_cost_usd) if row is not None else _ZERO
        reserved_tokens = int(row.reserved_total_tokens) if row is not None else 0
        reserved_cost = _asDecimal(row.reserved_estimated_cost_usd) if row is not None else _ZERO
        token_limit = service.monthly_token_limit
        cost_limit = service.monthly_cost_limit_usd
        cost_effective = IsCostLimitEffective(service.billing_mode, cost_limit)
        token_reached = (
            token_limit is not None
            and service.billing_mode in {'Metered', 'Local'}
            and (settled_tokens + reserved_tokens) >= int(token_limit)
        )
        cost_reached = (
            cost_effective
            and cost_limit is not None
            and (settled_cost + reserved_cost) >= _asDecimal(cost_limit)
        )
        return AIAPIUsageMonthSnapshot(
            service_id=service.service_id,
            year_month=month,
            billing_mode=service.billing_mode,
            settled_prompt_tokens=int(row.settled_prompt_tokens) if row is not None else 0,
            settled_completion_tokens=int(row.settled_completion_tokens) if row is not None else 0,
            settled_total_tokens=settled_tokens,
            settled_estimated_cost_usd=settled_cost,
            reserved_total_tokens=reserved_tokens,
            reserved_estimated_cost_usd=reserved_cost,
            settled_request_count=int(row.settled_request_count) if row is not None else 0,
            monthly_token_limit=token_limit,
            monthly_cost_limit_usd=cost_limit,
            cost_limit_effective=cost_effective,
            token_limit_reached=token_reached,
            cost_limit_reached=cost_reached,
            service_name_snapshot=(
                row.service_name_snapshot if row is not None else service.service_name
            ),
            opencode_provider_id_snapshot=(
                row.opencode_provider_id_snapshot
                if row is not None
                else service.opencode_provider_id
            ),
            opencode_model_id_snapshot=(
                row.opencode_model_id_snapshot
                if row is not None
                else service.opencode_model_id
            ),
            service_deleted=False,
        )

    @classmethod
    async def GetDeletedMonthSnapshot(
        cls,
        service_id: str,
        *,
        year_month: str | None = None,
    ) -> AIAPIUsageMonthSnapshot | None:
        """削除済み service の台帳行があればスナップショットを返す。

        Args:
            service_id: 削除された service UUID。
            year_month: 省略時は当月。

        Returns:
            行が無ければ None。
        """

        await cls.EnsureStartupReservedReset()
        month = year_month or CurrentYearMonth()
        row = await AIAPIUsageMonth.filter(
            service_id=service_id.strip().lower(),
            year_month=month,
        ).first()
        if row is None:
            return None
        return cls._SnapshotFromRow(row, service_deleted=True)

    @classmethod
    def _SnapshotFromRow(
        cls,
        row: AIAPIUsageMonth,
        *,
        service_deleted: bool,
        monthly_token_limit: int | None = None,
        monthly_cost_limit_usd: Decimal | None = None,
    ) -> AIAPIUsageMonthSnapshot:
        """DB 行からスナップショットを構築する（削除済み service 用）。"""

        billing = str(row.billing_mode_snapshot)
        settled_tokens = int(row.settled_total_tokens)
        settled_cost = _asDecimal(row.settled_estimated_cost_usd)
        reserved_tokens = int(row.reserved_total_tokens)
        reserved_cost = _asDecimal(row.reserved_estimated_cost_usd)
        cost_effective = (
            IsCostLimitEffective(billing, monthly_cost_limit_usd)
            if service_deleted is False
            else False
        )
        return AIAPIUsageMonthSnapshot(
            service_id=str(row.service_id),
            year_month=str(row.year_month),
            billing_mode=billing,
            settled_prompt_tokens=int(row.settled_prompt_tokens),
            settled_completion_tokens=int(row.settled_completion_tokens),
            settled_total_tokens=settled_tokens,
            settled_estimated_cost_usd=settled_cost,
            reserved_total_tokens=reserved_tokens,
            reserved_estimated_cost_usd=reserved_cost,
            settled_request_count=int(row.settled_request_count),
            monthly_token_limit=monthly_token_limit,
            monthly_cost_limit_usd=monthly_cost_limit_usd,
            cost_limit_effective=cost_effective,
            token_limit_reached=False,
            cost_limit_reached=False,
            service_name_snapshot=str(row.service_name_snapshot),
            opencode_provider_id_snapshot=str(row.opencode_provider_id_snapshot),
            opencode_model_id_snapshot=str(row.opencode_model_id_snapshot),
            service_deleted=service_deleted,
        )

    @classmethod
    async def ListMonthSnapshots(
        cls,
        services: list[AIBackendService],
        *,
        year_month: str | None = None,
        include_deleted: bool = True,
    ) -> list[AIAPIUsageMonthSnapshot]:
        """登録済み service の当月スナップショットを返す。

        include_deleted=True のとき、設定から消えた service の台帳行も
        snapshot 付きで末尾に追加する（service_deleted=True）。
        """

        month = year_month or CurrentYearMonth()
        result: list[AIAPIUsageMonthSnapshot] = []
        known_ids = {service.service_id for service in services}
        for service in services:
            result.append(await cls.GetMonthSnapshot(service, year_month=month))
        if include_deleted:
            await cls.EnsureStartupReservedReset()
            orphan_rows = await AIAPIUsageMonth.filter(year_month=month).exclude(
                service_id__in=list(known_ids) if known_ids else ['__none__'],
            )
            for row in orphan_rows:
                # 実績が一切無い行は出さない（予約だけ残ってリセット済み等）。
                if (
                    int(row.settled_total_tokens) == 0
                    and int(row.settled_request_count) == 0
                    and _asDecimal(row.settled_estimated_cost_usd) == _ZERO
                ):
                    continue
                result.append(cls._SnapshotFromRow(row, service_deleted=True))
        return result


def SnapshotToDict(snapshot: AIAPIUsageMonthSnapshot) -> dict[str, Any]:
    """API 応答用 dict（JSON シリアライズ可能）。"""

    return {
        'service_id': snapshot.service_id,
        'year_month': snapshot.year_month,
        'billing_mode': snapshot.billing_mode,
        'settled_prompt_tokens': snapshot.settled_prompt_tokens,
        'settled_completion_tokens': snapshot.settled_completion_tokens,
        'settled_total_tokens': snapshot.settled_total_tokens,
        'settled_estimated_cost_usd': str(snapshot.settled_estimated_cost_usd),
        'reserved_total_tokens': snapshot.reserved_total_tokens,
        'reserved_estimated_cost_usd': str(snapshot.reserved_estimated_cost_usd),
        'settled_request_count': snapshot.settled_request_count,
        'monthly_token_limit': snapshot.monthly_token_limit,
        'monthly_cost_limit_usd': (
            str(snapshot.monthly_cost_limit_usd)
            if snapshot.monthly_cost_limit_usd is not None
            else None
        ),
        'cost_limit_effective': snapshot.cost_limit_effective,
        'token_limit_reached': snapshot.token_limit_reached,
        'cost_limit_reached': snapshot.cost_limit_reached,
        'service_name_snapshot': snapshot.service_name_snapshot,
        'opencode_provider_id_snapshot': snapshot.opencode_provider_id_snapshot,
        'opencode_model_id_snapshot': snapshot.opencode_model_id_snapshot,
        'service_deleted': snapshot.service_deleted,
    }
