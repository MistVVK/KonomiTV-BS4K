from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "series_episodes" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "season_number" INT NOT NULL CHECK ("season_number" >= 0),
            "episode_number" VARCHAR(40) NOT NULL,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "series_id" INT NOT NULL REFERENCES "series" ("id") ON DELETE CASCADE,
            CONSTRAINT "uid_series_episodes_number" UNIQUE (
                "series_id", "season_number", "episode_number"
            )
        );
        CREATE INDEX IF NOT EXISTS "idx_series_episodes_series" ON "series_episodes" ("series_id");

        ALTER TABLE "recorded_programs" ADD COLUMN "series_episode_id" INT
            REFERENCES "series_episodes" ("id") ON DELETE SET NULL;
        CREATE INDEX "idx_recorded_programs_episode" ON "recorded_programs" ("series_episode_id");

        CREATE TABLE IF NOT EXISTS "recorded_episode_resolutions" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "status" VARCHAR(32) NOT NULL,
            "source" VARCHAR(32),
            "input_fingerprint" VARCHAR(64),
            "provider_fingerprint" VARCHAR(64),
            "proposed_season_number" INT,
            "proposed_episode_number" VARCHAR(40),
            "confidence" REAL,
            "web_search_performed" INT NOT NULL DEFAULT 0,
            "is_legacy_recording" INT NOT NULL DEFAULT 0,
            "citations" JSON NOT NULL DEFAULT '[]',
            "ai_model" TEXT,
            "error_code" VARCHAR(255),
            "error_message" TEXT,
            "resolved_at" TIMESTAMP,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "recorded_program_id" INT NOT NULL UNIQUE
                REFERENCES "recorded_programs" ("id") ON DELETE CASCADE,
            "episode_id" INT REFERENCES "series_episodes" ("id") ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS "idx_recorded_episode_status"
            ON "recorded_episode_resolutions" ("status");
        CREATE INDEX IF NOT EXISTS "idx_recorded_episode_input"
            ON "recorded_episode_resolutions" ("input_fingerprint");
        CREATE INDEX IF NOT EXISTS "idx_recorded_episode_episode"
            ON "recorded_episode_resolutions" ("episode_id");

        INSERT INTO "recorded_episode_resolutions" (
            "status", "source", "web_search_performed", "is_legacy_recording", "citations",
            "recorded_program_id", "created_at", "updated_at"
        )
        SELECT
            'Pending', 'Migration', 0, 1, '[]', rp."id", CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM "recorded_programs" rp
        WHERE NOT EXISTS (
              SELECT 1 FROM "recorded_episode_resolutions" rer
              WHERE rer."recorded_program_id" = rp."id"
          );

        ALTER TABLE "recorded_series_ai_requests" ADD COLUMN "episode_resolution_id" INT
            REFERENCES "recorded_episode_resolutions" ("id") ON DELETE SET NULL;
        CREATE INDEX "idx_series_ai_episode_resolution"
            ON "recorded_series_ai_requests" ("episode_resolution_id");
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP INDEX IF EXISTS "idx_series_ai_episode_resolution";
        ALTER TABLE "recorded_series_ai_requests" DROP COLUMN "episode_resolution_id";
        DROP TABLE IF EXISTS "recorded_episode_resolutions";
        DROP INDEX IF EXISTS "idx_recorded_programs_episode";
        ALTER TABLE "recorded_programs" DROP COLUMN "series_episode_id";
        DROP TABLE IF EXISTS "series_episodes";
    """
