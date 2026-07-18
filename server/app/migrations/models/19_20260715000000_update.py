from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "cm_analysis_settings" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "enabled" INT NOT NULL DEFAULT 0,
            "logo_directory" TEXT,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO "cm_analysis_settings" ("id", "enabled") VALUES (1, 0);

        CREATE TABLE IF NOT EXISTS "cm_analysis_excluded_directories" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "path" TEXT NOT NULL,
            "resolved_path" TEXT NOT NULL,
            "enabled" INT NOT NULL DEFAULT 1,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS "cm_logos" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "path" TEXT NOT NULL,
            "filename" TEXT NOT NULL,
            "service_id" INT NOT NULL,
            "logo_name" TEXT NOT NULL,
            "enabled" INT NOT NULL DEFAULT 1,
            "file_hash" VARCHAR(64) NOT NULL,
            "file_size" BIGINT NOT NULL,
            "last_used_at" TIMESTAMP,
            "missing" INT NOT NULL DEFAULT 0,
            "deleted_at" TIMESTAMP,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "generated_from_recorded_video_id" INT REFERENCES "recorded_videos" ("id") ON DELETE SET NULL
        );
        CREATE INDEX "idx_cm_logos_service_id" ON "cm_logos" ("service_id");
        CREATE UNIQUE INDEX "uid_cm_logos_path" ON "cm_logos" ("path");

        CREATE TABLE IF NOT EXISTS "cm_logo_service_assignments" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "network_id" INT NOT NULL,
            "transport_stream_id" INT NOT NULL,
            "service_id" INT NOT NULL,
            "enabled" INT NOT NULL DEFAULT 1,
            "is_no_logo" INT NOT NULL DEFAULT 0,
            "valid_from" TIMESTAMP,
            "valid_until" TIMESTAMP,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "logo_id" INT REFERENCES "cm_logos" ("id") ON DELETE SET NULL,
            CONSTRAINT "uid_cm_logo_assignment_period" UNIQUE (
                "network_id", "transport_stream_id", "service_id", "valid_from", "valid_until"
            )
        );

        CREATE TABLE IF NOT EXISTS "cm_logo_generation_batches" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "state" VARCHAR(32) NOT NULL,
            "explicit" INT NOT NULL DEFAULT 0,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "completed_at" TIMESTAMP,
            "recorded_video_id" INT NOT NULL REFERENCES "recorded_videos" ("id") ON DELETE CASCADE
        );
        CREATE INDEX "idx_cm_logo_batches_state" ON "cm_logo_generation_batches" ("state");

        CREATE TABLE IF NOT EXISTS "cm_logo_generation_attempts" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "strategy" VARCHAR(32) NOT NULL,
            "start_frame" INT,
            "start_seconds" REAL,
            "frame_count" INT NOT NULL,
            "exit_code" INT,
            "match_ratio" REAL,
            "failure_reason" TEXT,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "completed_at" TIMESTAMP,
            "batch_id" INT NOT NULL REFERENCES "cm_logo_generation_batches" ("id") ON DELETE CASCADE,
            CONSTRAINT "uid_cm_logo_attempt" UNIQUE (
                "batch_id", "strategy", "start_frame", "start_seconds", "frame_count"
            )
        );

        CREATE TABLE IF NOT EXISTS "recorded_video_cm_analyses" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "status" VARCHAR(32) NOT NULL DEFAULT 'Pending',
            "chapter_source" VARCHAR(32),
            "input_fingerprint" JSON,
            "chapter_fingerprint" JSON,
            "chapter_last_read_at" TIMESTAMP,
            "analyzer_version" VARCHAR(255),
            "started_at" TIMESTAMP,
            "completed_at" TIMESTAMP,
            "error_code" VARCHAR(255),
            "error_message" TEXT,
            "matched_exclusion_path" TEXT,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "recorded_video_id" INT NOT NULL UNIQUE REFERENCES "recorded_videos" ("id") ON DELETE CASCADE,
            "used_logo_id" INT REFERENCES "cm_logos" ("id") ON DELETE SET NULL,
            "last_explicit_batch_id" INT REFERENCES "cm_logo_generation_batches" ("id") ON DELETE SET NULL
        );
        CREATE INDEX "idx_recorded_cm_status" ON "recorded_video_cm_analyses" ("status");
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "recorded_video_cm_analyses";
        DROP TABLE IF EXISTS "cm_logo_generation_attempts";
        DROP TABLE IF EXISTS "cm_logo_generation_batches";
        DROP TABLE IF EXISTS "cm_logo_service_assignments";
        DROP TABLE IF EXISTS "cm_logos";
        DROP TABLE IF EXISTS "cm_analysis_excluded_directories";
        DROP TABLE IF EXISTS "cm_analysis_settings";
    """
