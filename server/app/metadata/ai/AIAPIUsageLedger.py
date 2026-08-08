"""OpenCode service 向け月次利用台帳（予約・精算・上限判定）。

Asia/Tokyo 暦月 × service_id で集約する。
Subscription は enforce せず no-op。Local は cost 上限を無視し token のみ。
Metered は cost（取得可能なとき）と token の両方を見る。
プロセス再起動時は永続 Reserved reservation を予約見積額で一度だけ安全側精算する。
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from weakref import WeakKeyDictionary

from tortoise.transactions import in_transaction

from app import logging
from app.constants import JST
from app.metadata.ai.AIBackendSettings import AIBackendService
from app.metadata.ai.opencode_types import OpenCodeNormalizedUsage
from app.metadata.RecordedSeriesCandidates import RecordedSeriesAIError
from app.models.AIAPIUsage import (
    AIAPIUsageMonth,
    KonomiTVBS4KAIAPIUsageReservation,
)


# 1 回の OpenCode 呼び出しに対する安全な予約上界。
DEFAULT_RESERVE_TOTAL_TOKENS = 100_000
DEFAULT_RESERVE_COST_USD = Decimal('0.50')

_ZERO = Decimal('0')
# 起動時の未精算 reservation 回復を一度だけ行う。
_startup_recovery_done = False

# pytest の asyncio.run() やプロセス内の event loop 再生成でも Lock の loop affinity を交差させない。
_reserve_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()
_startup_recovery_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()
_loop_locks_guard = threading.Lock()

_FinalReservationState = Literal['Settled', 'Released']


def _GetLoopLock(
    lock_map: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock],
) -> asyncio.Lock:
    """現在の event loop 専用 Lock を返す。

    Args:
        lock_map: 用途別の event loop → Lock map。

    Returns:
        現在の event loop にだけ bind される Lock。
    """

    event_loop = asyncio.get_running_loop()
    with _loop_locks_guard:
        lock = lock_map.get(event_loop)
        if lock is None:
            lock = asyncio.Lock()
            lock_map[event_loop] = lock
        return lock


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
    async def EnsureStartupReservationRecovery(cls) -> None:
        """起動時に未精算 Reserved reservation を一度だけ安全側精算する。

        Returns:
            None
        """

        global _startup_recovery_done
        if _startup_recovery_done:
            return
        async with _GetLoopLock(_startup_recovery_locks):
            if _startup_recovery_done:
                return
            try:
                reservation_ids = await KonomiTVBS4KAIAPIUsageReservation.filter(
                    state='Reserved',
                ).values_list(
                    'reservation_id',
                    flat=True,
                )
                recovered_count = 0
                # crash 前に課金が発生した可能性を捨てず、各予約上界を Settled へ一度だけ振り替える。
                for reservation_id in reservation_ids:
                    recovered = await cls._FinalizeReservation(
                        str(reservation_id),
                        final_state='Settled',
                        unknown_interrupt=True,
                    )
                    if recovered:
                        recovered_count += 1
                if recovered_count > 0:
                    logging.info(
                        f'[AIAPIUsageLedger] Recovered {recovered_count} pending reservation(s).',
                    )
            except Exception as error:
                # DB 未初期化などでは警告し、done にせず次回の Reserve / snapshot で再試行する。
                logging.warning(
                    f'[AIAPIUsageLedger] Failed to recover pending reservations: {error}',
                )
                return
            _startup_recovery_done = True

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

        await cls.EnsureStartupReservationRecovery()
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

        reservation_id = str(uuid.uuid4())
        async with _GetLoopLock(_reserve_locks):
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

                # reservation 行と月次予約値を同じ transaction で作り、片方だけ残る状態を防ぐ。
                await KonomiTVBS4KAIAPIUsageReservation.create(
                    reservation_id=reservation_id,
                    service_id=service.service_id,
                    year_month=month,
                    billing_mode_snapshot=billing,
                    state='Reserved',
                    reserved_total_tokens=tokens,
                    reserved_estimated_cost_usd=cost,
                    using_db=connection,
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
            reservation_id=reservation_id,
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
        unknown_interrupt: bool = False,
    ) -> bool:
        """Reserved reservation を実 usage または予約上界で精算する。

        Args:
            reservation: Reserve の戻り値。
            usage: 実 usage。
            unknown_interrupt: 不明中断。予約額を settled へ振り替える。

        Returns:
            Reserved から Settled へ遷移できた場合だけ True。

        Raises:
            ValueError: usage なしで通常精算しようとした場合。
        """

        if reservation.skipped:
            return False
        if usage is None and unknown_interrupt is False:
            raise ValueError('usage is required unless unknown_interrupt is true.')
        return await cls._FinalizeReservation(
            reservation.reservation_id,
            final_state='Settled',
            usage=usage,
            unknown_interrupt=unknown_interrupt,
        )

    @classmethod
    async def Release(cls, reservation: AIAPIUsageReservation) -> bool:
        """Reserved reservation を課金なしとして解放する。

        Args:
            reservation: Reserve の戻り値。

        Returns:
            Reserved から Released へ遷移できた場合だけ True。
        """

        if reservation.skipped:
            return False
        return await cls._FinalizeReservation(
            reservation.reservation_id,
            final_state='Released',
        )

    @classmethod
    async def _FinalizeReservation(
        cls,
        reservation_id: str,
        *,
        final_state: _FinalReservationState,
        usage: OpenCodeNormalizedUsage | None = None,
        unknown_interrupt: bool = False,
    ) -> bool:
        """reservation を compare-and-set し、勝者だけが月次集約を更新する。

        Args:
            reservation_id: 永続 reservation UUID。
            final_state: Settled または Released。
            usage: Settled へ反映する実 usage。
            unknown_interrupt: 実 usage 不明のため予約上界を確定値にするか。

        Returns:
            Reserved から final_state へ遷移できた場合だけ True。
        """

        async with _GetLoopLock(_reserve_locks):
            async with in_transaction() as connection:
                reservation_row = await KonomiTVBS4KAIAPIUsageReservation.filter(
                    reservation_id=reservation_id,
                ).using_db(connection).first()
                if reservation_row is None:
                    logging.warning(
                        '[AIAPIUsageLedger] Reservation not found during finalization '
                        f'(reservation_id={reservation_id}).',
                    )
                    return False
                if reservation_row.state != 'Reserved':
                    return False

                month_row = await AIAPIUsageMonth.filter(
                    service_id=reservation_row.service_id,
                    year_month=reservation_row.year_month,
                ).using_db(connection).first()
                if month_row is None:
                    logging.warning(
                        '[AIAPIUsageLedger] Reservation month row missing '
                        f'(reservation_id={reservation_id}, service={reservation_row.service_id}, '
                        f'month={reservation_row.year_month}).',
                    )
                    return False

                add_prompt = 0
                add_completion = 0
                add_tokens = 0
                add_cost = _ZERO
                if final_state == 'Settled':
                    if unknown_interrupt:
                        # crash / timeout は課金済みの可能性があるため、予約上界を安全側で確定する。
                        add_tokens = max(0, int(reservation_row.reserved_total_tokens))
                        add_completion = add_tokens
                        add_cost = max(
                            _ZERO,
                            _asDecimal(reservation_row.reserved_estimated_cost_usd),
                        )
                    elif usage is not None:
                        add_prompt = max(0, int(usage.get('prompt_tokens') or 0))
                        add_completion = max(0, int(usage.get('completion_tokens') or 0))
                        reasoning = max(0, int(usage.get('reasoning_tokens') or 0))
                        add_tokens = max(
                            0,
                            int(usage.get('total_tokens') or (add_prompt + add_completion + reasoning)),
                        )
                        raw_cost = usage.get('estimated_cost_usd')
                        add_cost = max(
                            _ZERO,
                            _asDecimal(raw_cost) if raw_cost is not None else _ZERO,
                        )
                    else:
                        raise ValueError('usage is required to settle a reservation.')

                    # Local は実 usage に料金が含まれても cost 集計へ加えない。
                    if reservation_row.billing_mode_snapshot == 'Local':
                        add_cost = _ZERO

                # 状態がまだ Reserved の場合だけ更新する。0 rows は別 retry が先に確定済み。
                updated = await KonomiTVBS4KAIAPIUsageReservation.filter(
                    reservation_id=reservation_id,
                    state='Reserved',
                ).using_db(connection).update(
                    state=final_state,
                    settled_prompt_tokens=add_prompt,
                    settled_completion_tokens=add_completion,
                    settled_total_tokens=add_tokens,
                    settled_estimated_cost_usd=add_cost,
                    updated_at=datetime.now(tz=JST),
                )
                if updated != 1:
                    return False

                # CAS 勝者だけが予約値を戻し、Settled の場合だけ確定実績を1回加算する。
                month_row.reserved_total_tokens = max(
                    0,
                    int(month_row.reserved_total_tokens)
                    - int(reservation_row.reserved_total_tokens),
                )
                month_row.reserved_estimated_cost_usd = max(
                    _ZERO,
                    _asDecimal(month_row.reserved_estimated_cost_usd)
                    - _asDecimal(reservation_row.reserved_estimated_cost_usd),
                )
                if final_state == 'Settled':
                    month_row.settled_prompt_tokens = (
                        int(month_row.settled_prompt_tokens) + add_prompt
                    )
                    month_row.settled_completion_tokens = (
                        int(month_row.settled_completion_tokens) + add_completion
                    )
                    month_row.settled_total_tokens = int(month_row.settled_total_tokens) + add_tokens
                    month_row.settled_estimated_cost_usd = (
                        _asDecimal(month_row.settled_estimated_cost_usd) + add_cost
                    )
                    month_row.settled_request_count = int(month_row.settled_request_count) + 1
                await month_row.save(
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
                return True

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

        await cls.EnsureStartupReservationRecovery()
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

        await cls.EnsureStartupReservationRecovery()
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
            await cls.EnsureStartupReservationRecovery()
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
