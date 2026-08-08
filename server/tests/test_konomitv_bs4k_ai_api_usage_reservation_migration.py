"""BS4K AI API 利用量 reservation migration の回帰テスト。"""

from __future__ import annotations

import asyncio
import sqlite3
from types import ModuleType
from typing import Any, cast

import pytest


def test_reservation_migration_preserves_existing_reserved_usage() -> None:
    """既存月次行の reserved 値を合成 Reserved reservation へ移行する。"""

    async def Run() -> None:
        migration = cast(
            ModuleType,
            __import__(
                'app.migrations.models.32_20260808000000_update',
                fromlist=['upgrade'],
            ),
        )
        migration_sql = await migration.upgrade(cast(Any, object()))

        connection = sqlite3.connect(':memory:')
        try:
            connection.executescript("""
                CREATE TABLE "ai_api_usage_months" (
                    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                    "service_id" VARCHAR(36) NOT NULL,
                    "year_month" VARCHAR(7) NOT NULL,
                    "billing_mode_snapshot" VARCHAR(32) NOT NULL,
                    "reserved_total_tokens" INT NOT NULL DEFAULT 0,
                    "reserved_estimated_cost_usd" VARCHAR(40) NOT NULL DEFAULT '0'
                );
                INSERT INTO "ai_api_usage_months" (
                    "service_id",
                    "year_month",
                    "billing_mode_snapshot",
                    "reserved_total_tokens",
                    "reserved_estimated_cost_usd"
                ) VALUES (
                    '00000000-0000-0000-0000-000000000001',
                    '2026-08',
                    'Metered',
                    321,
                    '0.75'
                );
            """)
            connection.executescript(migration_sql)

            migrated = connection.execute(
                'SELECT "reservation_id", "state", "reserved_total_tokens", '
                '"reserved_estimated_cost_usd" '
                'FROM "konomitv_bs4k_ai_api_usage_reservations"',
            ).fetchone()
            assert migrated is not None
            assert len(str(migrated[0])) == 36
            assert migrated[1:] == ('Reserved', 321, '0.75')

            # DB 境界でも一方向状態の文字列表現以外を受理しない。
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    'INSERT INTO "konomitv_bs4k_ai_api_usage_reservations" ('
                    '"reservation_id", "service_id", "year_month", '
                    '"billing_mode_snapshot", "state"'
                    ') VALUES (?, ?, ?, ?, ?)',
                    (
                        '00000000-0000-0000-0000-000000000002',
                        '00000000-0000-0000-0000-000000000001',
                        '2026-08',
                        'Metered',
                        'Invalid',
                    ),
                )
        finally:
            connection.close()

    asyncio.run(Run())
