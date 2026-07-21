from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "series" ADD COLUMN "canonical_key" VARCHAR(64);
        ALTER TABLE "series" ADD COLUMN "wikipedia_page_id" INT;
        CREATE UNIQUE INDEX "uid_series_canonical_key" ON "series" ("canonical_key");
        CREATE UNIQUE INDEX "uid_series_wikipedia_page_id" ON "series" ("wikipedia_page_id");

        CREATE TABLE IF NOT EXISTS "recorded_series_rules" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "key_hash" VARCHAR(64) NOT NULL UNIQUE,
            "normalized_key" TEXT NOT NULL,
            "display_title" TEXT NOT NULL,
            "decision" VARCHAR(32) NOT NULL,
            "wikipedia_page_id" INT,
            "source" VARCHAR(32) NOT NULL,
            "confidence" REAL NOT NULL,
            "evidence_hash" VARCHAR(64) NOT NULL,
            "resolver_version" VARCHAR(32) NOT NULL,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "series_id" INT REFERENCES "series" ("id") ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS "recorded_series_resolutions" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "input_fingerprint" VARCHAR(64) NOT NULL,
            "evidence_hash" VARCHAR(64) NOT NULL,
            "resolver_version" VARCHAR(32) NOT NULL,
            "normalized_title" TEXT NOT NULL,
            "status" VARCHAR(32) NOT NULL,
            "source" VARCHAR(32),
            "wikipedia_page_id" INT,
            "candidate_set_hash" VARCHAR(64),
            "candidate_snapshot" JSON,
            "ai_model" TEXT,
            "error_code" VARCHAR(255),
            "error_message" TEXT,
            "resolved_at" TIMESTAMP,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "recorded_program_id" INT NOT NULL UNIQUE
                REFERENCES "recorded_programs" ("id") ON DELETE CASCADE,
            "series_id" INT REFERENCES "series" ("id") ON DELETE SET NULL
        );
        CREATE INDEX "idx_recorded_series_resolutions_input"
            ON "recorded_series_resolutions" ("input_fingerprint");
        CREATE INDEX "idx_recorded_series_resolutions_status"
            ON "recorded_series_resolutions" ("status");

        CREATE TABLE IF NOT EXISTS "recorded_series_ai_requests" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "purpose" VARCHAR(32) NOT NULL,
            "status" VARCHAR(32) NOT NULL,
            "model" TEXT NOT NULL,
            "input_fingerprint" VARCHAR(64),
            "candidate_set_hash" VARCHAR(64),
            "candidate_ids" JSON NOT NULL DEFAULT '[]',
            "selected_choice_id" TEXT,
            "prompt_tokens" INT,
            "completion_tokens" INT,
            "http_status" INT,
            "latency_ms" INT,
            "error_code" VARCHAR(255),
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "resolution_id" INT REFERENCES "recorded_series_resolutions" ("id") ON DELETE SET NULL
        );
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "recorded_series_ai_requests";
        DROP TABLE IF EXISTS "recorded_series_resolutions";
        DROP TABLE IF EXISTS "recorded_series_rules";
        DROP INDEX IF EXISTS "uid_series_wikipedia_page_id";
        DROP INDEX IF EXISTS "uid_series_canonical_key";
        ALTER TABLE "series" DROP COLUMN "wikipedia_page_id";
        ALTER TABLE "series" DROP COLUMN "canonical_key";
    """
