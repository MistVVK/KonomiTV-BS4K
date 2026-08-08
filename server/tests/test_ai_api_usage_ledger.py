"""月次利用台帳 (AIAPIUsageLedger) のユニットテスト。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from tortoise import Tortoise

import app.metadata.ai.AIAPIUsageLedger as ledger_mod
from app.metadata.ai.AIAPIUsageLedger import (
    DEFAULT_RESERVE_TOTAL_TOKENS,
    AIAPIUsageLedger,
    CurrentYearMonth,
)
from app.metadata.ai.AIBackendSettings import AIBackendService
from app.metadata.RecordedSeriesCandidates import RecordedSeriesAIError
from app.models.AIAPIUsage import (
    AIAPIUsageMonth,
    KonomiTVBS4KAIAPIUsageReservation,
)


def _service(**overrides: object) -> AIBackendService:
    base: dict[str, object] = {
        'service_id': str(uuid4()),
        'service_name': 'DeepSeek Metered',
        'opencode_provider_id': 'deepseek',
        'opencode_model_id': 'deepseek-chat',
        'auth_mode': 'ApiKey',
        'billing_mode': 'Metered',
        'monthly_token_limit': 1_000,
        'monthly_cost_limit_usd': Decimal('1.00'),
    }
    base.update(overrides)
    return AIBackendService.model_validate(base)


async def _InitDB() -> None:
    await Tortoise.init(
        db_url='sqlite://:memory:',
        modules={'models': ['app.models.AIAPIUsage']},
        timezone='Asia/Tokyo',
    )
    await Tortoise.generate_schemas()
    ledger_mod._startup_recovery_done = False


async def _CloseDB() -> None:
    await Tortoise.close_connections()


def test_reserve_and_settle_success() -> None:
    """予約後に実 usage で精算する。"""

    async def Run() -> None:
        await _InitDB()
        try:
            service = _service()
            reservation = await AIAPIUsageLedger.Reserve(
                service,
                estimate_total_tokens=100,
                estimate_cost_usd=Decimal('0.10'),
            )
            assert reservation.skipped is False
            row = await AIAPIUsageMonth.get(service_id=service.service_id)
            assert row.reserved_total_tokens == 100

            await AIAPIUsageLedger.Settle(
                reservation,
                {
                    'prompt_tokens': 10,
                    'completion_tokens': 20,
                    'reasoning_tokens': 0,
                    'total_tokens': 30,
                    'estimated_cost_usd': 0.02,
                },
            )
            await row.refresh_from_db()
            assert row.reserved_total_tokens == 0
            assert row.settled_total_tokens == 30
            assert row.settled_prompt_tokens == 10
            assert row.settled_completion_tokens == 20
            assert Decimal(str(row.settled_estimated_cost_usd)) == Decimal('0.02')
            assert row.settled_request_count == 1
            reservation_row = await KonomiTVBS4KAIAPIUsageReservation.get(
                reservation_id=reservation.reservation_id,
            )
            assert reservation_row.state == 'Settled'
            assert reservation_row.settled_total_tokens == 30
        finally:
            await _CloseDB()

    asyncio.run(Run())


def test_token_limit_rejects_before_call() -> None:
    """token 上限超過は予約時点で拒否する。"""

    async def Run() -> None:
        await _InitDB()
        try:
            service = _service(monthly_token_limit=50)
            with pytest.raises(RecordedSeriesAIError) as exc_info:
                await AIAPIUsageLedger.Reserve(
                    service,
                    estimate_total_tokens=100,
                    estimate_cost_usd=Decimal('0'),
                )
            assert exc_info.value.code == 'MonthlyTokenLimitReached'
        finally:
            await _CloseDB()

    asyncio.run(Run())


def test_cost_limit_rejects_metered() -> None:
    """Metered の cost 上限超過を拒否する。"""

    async def Run() -> None:
        await _InitDB()
        try:
            service = _service(monthly_cost_limit_usd=Decimal('0.05'), monthly_token_limit=None)
            with pytest.raises(RecordedSeriesAIError) as exc_info:
                await AIAPIUsageLedger.Reserve(
                    service,
                    estimate_total_tokens=1,
                    estimate_cost_usd=Decimal('0.50'),
                )
            assert exc_info.value.code == 'MonthlyCostLimitReached'
        finally:
            await _CloseDB()

    asyncio.run(Run())


def test_subscription_is_noop() -> None:
    """Subscription は台帳を触らない。"""

    async def Run() -> None:
        await _InitDB()
        try:
            service = _service(
                auth_mode='OAuthSubscription',
                billing_mode='Subscription',
                monthly_token_limit=None,
                monthly_cost_limit_usd=None,
            )
            reservation = await AIAPIUsageLedger.Reserve(service)
            assert reservation.skipped is True
            assert await AIAPIUsageMonth.all().count() == 0
            await AIAPIUsageLedger.Settle(
                reservation,
                {
                    'prompt_tokens': 1,
                    'completion_tokens': 1,
                    'reasoning_tokens': 0,
                    'total_tokens': 2,
                    'estimated_cost_usd': 0.01,
                },
            )
            assert await AIAPIUsageMonth.all().count() == 0
        finally:
            await _CloseDB()

    asyncio.run(Run())


def test_local_ignores_cost_limit() -> None:
    """Local は cost 上限を無視し token のみ見る。"""

    async def Run() -> None:
        await _InitDB()
        try:
            service = _service(
                auth_mode='NoneLocal',
                billing_mode='Local',
                api_base_url='http://127.0.0.1:11434/v1',
                monthly_cost_limit_usd=None,
                monthly_token_limit=200,
            )
            reservation = await AIAPIUsageLedger.Reserve(
                service,
                estimate_total_tokens=50,
                estimate_cost_usd=Decimal('99'),
            )
            assert reservation.reserved_estimated_cost_usd == Decimal('0')
            await AIAPIUsageLedger.Settle(
                reservation,
                {
                    'prompt_tokens': 5,
                    'completion_tokens': 5,
                    'reasoning_tokens': 0,
                    'total_tokens': 10,
                    'estimated_cost_usd': 9.99,
                },
            )
            row = await AIAPIUsageMonth.get(service_id=service.service_id)
            assert Decimal(str(row.settled_estimated_cost_usd)) == Decimal('0')
            assert row.settled_total_tokens == 10
        finally:
            await _CloseDB()

    asyncio.run(Run())


def test_free_failure_releases_reservation() -> None:
    """課金なし失敗は予約のみ解放する。"""

    async def Run() -> None:
        await _InitDB()
        try:
            service = _service()
            reservation = await AIAPIUsageLedger.Reserve(
                service,
                estimate_total_tokens=80,
                estimate_cost_usd=Decimal('0.1'),
            )
            await AIAPIUsageLedger.Release(reservation)
            row = await AIAPIUsageMonth.get(service_id=service.service_id)
            assert row.reserved_total_tokens == 0
            assert row.settled_total_tokens == 0
            assert row.settled_request_count == 0
            reservation_row = await KonomiTVBS4KAIAPIUsageReservation.get(
                reservation_id=reservation.reservation_id,
            )
            assert reservation_row.state == 'Released'
        finally:
            await _CloseDB()

    asyncio.run(Run())


def test_unknown_interrupt_settles_estimate() -> None:
    """不明中断は予約見積を確定へ振り替える。"""

    async def Run() -> None:
        await _InitDB()
        try:
            service = _service()
            reservation = await AIAPIUsageLedger.Reserve(
                service,
                estimate_total_tokens=40,
                estimate_cost_usd=Decimal('0.2'),
            )
            await AIAPIUsageLedger.Settle(reservation, unknown_interrupt=True)
            row = await AIAPIUsageMonth.get(service_id=service.service_id)
            assert row.reserved_total_tokens == 0
            assert row.settled_total_tokens == 40
            assert Decimal(str(row.settled_estimated_cost_usd)) == Decimal('0.2')
            assert row.settled_request_count == 1
        finally:
            await _CloseDB()

    asyncio.run(Run())


def test_concurrent_reserve_respects_limit() -> None:
    """並行予約でも token 上限を超えない。"""

    async def Run() -> None:
        await _InitDB()
        try:
            service = _service(monthly_token_limit=150, monthly_cost_limit_usd=None)

            async def TryReserve() -> str:
                try:
                    await AIAPIUsageLedger.Reserve(
                        service,
                        estimate_total_tokens=100,
                        estimate_cost_usd=Decimal('0'),
                    )
                    return 'ok'
                except RecordedSeriesAIError as error:
                    return error.code

            results = await asyncio.gather(TryReserve(), TryReserve(), TryReserve())
            assert results.count('ok') == 1
            assert results.count('MonthlyTokenLimitReached') == 2
        finally:
            await _CloseDB()

    asyncio.run(Run())


def test_startup_recovers_reserved_once() -> None:
    """再起動時は未精算予約を予約上界で一度だけ Settled へ回復する。"""

    async def Run() -> None:
        await _InitDB()
        try:
            service = _service()
            reservation = await AIAPIUsageLedger.Reserve(
                service,
                estimate_total_tokens=40,
                estimate_cost_usd=Decimal('0.2'),
            )
            # Reserve 済みの DB を残してプロセスだけ再起動した状態を再現する。
            ledger_mod._startup_recovery_done = False
            await AIAPIUsageLedger.EnsureStartupReservationRecovery()
            row = await AIAPIUsageMonth.get(service_id=service.service_id)
            assert row.reserved_total_tokens == 0
            assert row.settled_total_tokens == 40
            assert row.settled_request_count == 1
            assert Decimal(str(row.reserved_estimated_cost_usd)) == Decimal('0')
            assert Decimal(str(row.settled_estimated_cost_usd)) == Decimal('0.2')
            reservation_row = await KonomiTVBS4KAIAPIUsageReservation.get(
                reservation_id=reservation.reservation_id,
            )
            assert reservation_row.state == 'Settled'

            # 回復処理自体が再実行されても同じ reservation は二重計上しない。
            ledger_mod._startup_recovery_done = False
            await AIAPIUsageLedger.EnsureStartupReservationRecovery()
            await row.refresh_from_db()
            assert row.settled_total_tokens == 40
            assert row.settled_request_count == 1
        finally:
            await _CloseDB()

    asyncio.run(Run())


def test_double_settle_is_idempotent() -> None:
    """同じ reservation の並行 Settle は token と request を1回だけ計上する。"""

    async def Run() -> None:
        await _InitDB()
        try:
            service = _service()
            reservation = await AIAPIUsageLedger.Reserve(
                service,
                estimate_total_tokens=100,
                estimate_cost_usd=Decimal('0.1'),
            )
            usage = {
                'prompt_tokens': 20,
                'completion_tokens': 10,
                'reasoning_tokens': 0,
                'total_tokens': 30,
                'estimated_cost_usd': 0.03,
            }

            results = await asyncio.gather(
                AIAPIUsageLedger.Settle(reservation, usage),
                AIAPIUsageLedger.Settle(reservation, usage),
            )

            assert results.count(True) == 1
            assert results.count(False) == 1
            row = await AIAPIUsageMonth.get(service_id=service.service_id)
            assert row.reserved_total_tokens == 0
            assert row.settled_total_tokens == 30
            assert row.settled_request_count == 1
        finally:
            await _CloseDB()

    asyncio.run(Run())


def test_release_then_retry_uses_new_reservation_only() -> None:
    """cancel 解放直後の再試行でも旧 reservation を確定実績へ二重計上しない。"""

    async def Run() -> None:
        await _InitDB()
        try:
            service = _service()
            cancelled = await AIAPIUsageLedger.Reserve(
                service,
                estimate_total_tokens=50,
                estimate_cost_usd=Decimal('0.05'),
            )
            assert await AIAPIUsageLedger.Release(cancelled) is True
            assert await AIAPIUsageLedger.Release(cancelled) is False
            assert await AIAPIUsageLedger.Settle(
                cancelled,
                {
                    'prompt_tokens': 100,
                    'completion_tokens': 100,
                    'reasoning_tokens': 0,
                    'total_tokens': 200,
                    'estimated_cost_usd': 0.2,
                },
            ) is False

            retried = await AIAPIUsageLedger.Reserve(
                service,
                estimate_total_tokens=50,
                estimate_cost_usd=Decimal('0.05'),
            )
            assert await AIAPIUsageLedger.Settle(
                retried,
                {
                    'prompt_tokens': 7,
                    'completion_tokens': 5,
                    'reasoning_tokens': 0,
                    'total_tokens': 12,
                    'estimated_cost_usd': 0.01,
                },
            ) is True

            row = await AIAPIUsageMonth.get(service_id=service.service_id)
            assert row.reserved_total_tokens == 0
            assert row.settled_total_tokens == 12
            assert row.settled_request_count == 1
        finally:
            await _CloseDB()

    asyncio.run(Run())


def test_settle_keeps_original_month_across_month_boundary() -> None:
    """月境界後の Settle も永続 reservation が指す予約月へ1回だけ計上する。"""

    async def Run() -> None:
        await _InitDB()
        try:
            service = _service()
            reservation = await AIAPIUsageLedger.Reserve(
                service,
                estimate_total_tokens=50,
                estimate_cost_usd=Decimal('0.05'),
                year_month='2026-08',
            )
            assert await AIAPIUsageLedger.Settle(
                reservation,
                {
                    'prompt_tokens': 6,
                    'completion_tokens': 4,
                    'reasoning_tokens': 0,
                    'total_tokens': 10,
                    'estimated_cost_usd': 0.01,
                },
            ) is True
            assert await AIAPIUsageLedger.Settle(
                reservation,
                {
                    'prompt_tokens': 6,
                    'completion_tokens': 4,
                    'reasoning_tokens': 0,
                    'total_tokens': 10,
                    'estimated_cost_usd': 0.01,
                },
            ) is False

            august = await AIAPIUsageMonth.get(
                service_id=service.service_id,
                year_month='2026-08',
            )
            assert august.settled_total_tokens == 10
            assert august.settled_request_count == 1
            assert await AIAPIUsageMonth.filter(
                service_id=service.service_id,
                year_month='2026-09',
            ).exists() is False
        finally:
            await _CloseDB()

    asyncio.run(Run())


def test_default_reserve_constants() -> None:
    """予約デフォルトが計画どおり正の値である。"""

    assert DEFAULT_RESERVE_TOTAL_TOKENS == 100_000


def test_list_includes_deleted_service_history() -> None:
    """削除済み service の実績行が一覧に service_deleted=True で載る。"""

    async def Run() -> None:
        await _InitDB()
        try:
            active = _service()
            deleted_id = str(uuid4())
            await AIAPIUsageMonth.create(
                service_id=deleted_id,
                year_month=CurrentYearMonth(),
                settled_total_tokens=42,
                settled_request_count=1,
                settled_estimated_cost_usd=Decimal('0.01'),
                service_name_snapshot='Deleted Svc',
                opencode_provider_id_snapshot='deepseek',
                opencode_model_id_snapshot='deepseek-chat',
                billing_mode_snapshot='Metered',
            )
            snaps = await AIAPIUsageLedger.ListMonthSnapshots(
                [active],
                include_deleted=True,
            )
            deleted = [s for s in snaps if s.service_id == deleted_id]
            assert len(deleted) == 1
            assert deleted[0].service_deleted is True
            assert deleted[0].settled_total_tokens == 42
            assert deleted[0].service_name_snapshot == 'Deleted Svc'
        finally:
            await _CloseDB()

    asyncio.run(Run())
