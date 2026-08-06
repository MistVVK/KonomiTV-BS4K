from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    """OpenCode 月次利用台帳テーブルを追加する。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        ai_api_usage_months を作成する SQLite SQL。
    """

    del db
    return """
        CREATE TABLE IF NOT EXISTS "ai_api_usage_months" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "service_id" VARCHAR(36) NOT NULL,
            "year_month" VARCHAR(7) NOT NULL,
            "settled_prompt_tokens" INT NOT NULL DEFAULT 0,
            "settled_completion_tokens" INT NOT NULL DEFAULT 0,
            "settled_total_tokens" INT NOT NULL DEFAULT 0,
            "settled_estimated_cost_usd" VARCHAR(40) NOT NULL DEFAULT '0',
            "reserved_total_tokens" INT NOT NULL DEFAULT 0,
            "reserved_estimated_cost_usd" VARCHAR(40) NOT NULL DEFAULT '0',
            "settled_request_count" INT NOT NULL DEFAULT 0,
            "service_name_snapshot" VARCHAR(128) NOT NULL,
            "opencode_provider_id_snapshot" VARCHAR(64) NOT NULL,
            "opencode_model_id_snapshot" VARCHAR(255) NOT NULL,
            "billing_mode_snapshot" VARCHAR(32) NOT NULL,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS "idx_ai_api_usage_months_service_id"
            ON "ai_api_usage_months" ("service_id");
        CREATE INDEX IF NOT EXISTS "idx_ai_api_usage_months_year_month"
            ON "ai_api_usage_months" ("year_month");
        CREATE UNIQUE INDEX IF NOT EXISTS "uid_ai_api_usage_months_service_year"
            ON "ai_api_usage_months" ("service_id", "year_month");
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    """月次利用台帳テーブルを削除する。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        DROP TABLE SQL。
    """

    del db
    return """
        DROP TABLE IF EXISTS "ai_api_usage_months";
    """
