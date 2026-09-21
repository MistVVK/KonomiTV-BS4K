from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    del db
    return """
        CREATE TABLE IF NOT EXISTS "series_ai_fallbacks" (
            "grouping_key" VARCHAR(512) NOT NULL PRIMARY KEY,
            "input_fingerprint" VARCHAR(64) NOT NULL,
            "provider_fingerprint" VARCHAR(64) NOT NULL,
            "status" VARCHAR(32) NOT NULL,
            "series_title" TEXT,
            "normalized_title" VARCHAR(512),
            "web_search_performed" INT NOT NULL DEFAULT 0,
            "citations" JSON NOT NULL DEFAULT '[]',
            "rationale_short" TEXT,
            "ai_model" VARCHAR(255),
            "prompt_tokens" INT,
            "completion_tokens" INT,
            "http_status" INT,
            "latency_ms" INT,
            "attempt_summaries" JSON NOT NULL DEFAULT '[]',
            "error_code" VARCHAR(255),
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "series_id" INT REFERENCES "series" ("id") ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS "idx_series_ai_fallbacks_status" ON "series_ai_fallbacks" ("status");
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    del db
    return """
        DROP TABLE IF EXISTS "series_ai_fallbacks";
    """
