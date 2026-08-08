from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    """BS4K AI API 利用量の永続 reservation テーブルを追加する。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        reservation テーブル作成と既存予約値移行を行う SQLite SQL。
    """

    del db
    return """
        CREATE TABLE IF NOT EXISTS "konomitv_bs4k_ai_api_usage_reservations" (
            "reservation_id" VARCHAR(36) PRIMARY KEY NOT NULL,
            "service_id" VARCHAR(36) NOT NULL,
            "year_month" VARCHAR(7) NOT NULL,
            "billing_mode_snapshot" VARCHAR(32) NOT NULL,
            "state" VARCHAR(16) NOT NULL DEFAULT 'Reserved'
                CHECK ("state" IN ('Reserved', 'Settled', 'Released')),
            "reserved_total_tokens" INT NOT NULL DEFAULT 0,
            "reserved_estimated_cost_usd" VARCHAR(40) NOT NULL DEFAULT '0',
            "settled_prompt_tokens" INT NOT NULL DEFAULT 0,
            "settled_completion_tokens" INT NOT NULL DEFAULT 0,
            "settled_total_tokens" INT NOT NULL DEFAULT 0,
            "settled_estimated_cost_usd" VARCHAR(40) NOT NULL DEFAULT '0',
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS "idx_bs4k_ai_usage_reservation_service"
            ON "konomitv_bs4k_ai_api_usage_reservations" ("service_id");
        CREATE INDEX IF NOT EXISTS "idx_bs4k_ai_usage_reservation_month"
            ON "konomitv_bs4k_ai_api_usage_reservations" ("year_month");
        CREATE INDEX IF NOT EXISTS "idx_bs4k_ai_usage_reservation_state"
            ON "konomitv_bs4k_ai_api_usage_reservations" ("state");

        INSERT OR IGNORE INTO "konomitv_bs4k_ai_api_usage_reservations" (
            "reservation_id",
            "service_id",
            "year_month",
            "billing_mode_snapshot",
            "state",
            "reserved_total_tokens",
            "reserved_estimated_cost_usd"
        )
        SELECT
            printf('legacy-%029d', "id"),
            "service_id",
            "year_month",
            "billing_mode_snapshot",
            'Reserved',
            "reserved_total_tokens",
            "reserved_estimated_cost_usd"
        FROM "ai_api_usage_months"
        WHERE "reserved_total_tokens" > 0
           OR CAST("reserved_estimated_cost_usd" AS NUMERIC) > 0;
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    """BS4K AI API 利用量の永続 reservation テーブルを削除する。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        reservation テーブルを削除する SQLite SQL。
    """

    del db
    return 'DROP TABLE IF EXISTS "konomitv_bs4k_ai_api_usage_reservations";'
