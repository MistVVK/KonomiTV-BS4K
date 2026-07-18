from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "recorded_video_cm_analyses" ADD COLUMN "attempt_key_sha256" VARCHAR(64);
        ALTER TABLE "recorded_video_cm_analyses" ADD COLUMN "runtime_fingerprint" JSON;
        ALTER TABLE "recorded_video_cm_analyses" ADD COLUMN "chapter_path_kind" VARCHAR(32);
        ALTER TABLE "recorded_video_cm_analyses" ADD COLUMN "finished_at" TIMESTAMP;

        UPDATE "recorded_video_cm_analyses"
        SET
            "chapter_path_kind" = CASE
                WHEN "chapter_source" IS NOT NULL THEN 'Legacy'
                ELSE NULL
            END,
            "finished_at" = CASE
                WHEN "status" IN ('Completed', 'Failed', 'Unsupported', 'Excluded', 'Interrupted')
                    THEN COALESCE("completed_at", "updated_at")
                ELSE NULL
            END;

        CREATE TABLE IF NOT EXISTS "recorded_video_cm_results" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "source" VARCHAR(32) NOT NULL,
            "verified" INT NOT NULL DEFAULT 1,
            "input_fingerprint" JSON,
            "chapter_fingerprint" JSON,
            "chapter_path_kind" VARCHAR(32),
            "pipeline_version" VARCHAR(255),
            "runtime_fingerprint" JSON,
            "published_at" TIMESTAMP NOT NULL,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "recorded_video_id" INT NOT NULL UNIQUE REFERENCES "recorded_videos" ("id") ON DELETE CASCADE,
            "used_logo_id" INT REFERENCES "cm_logos" ("id") ON DELETE SET NULL
        );

        INSERT INTO "recorded_video_cm_results" (
            "source", "verified", "input_fingerprint", "chapter_fingerprint",
            "chapter_path_kind", "pipeline_version", "published_at",
            "created_at", "updated_at", "recorded_video_id", "used_logo_id"
        )
        SELECT
            CASE
                WHEN a."status" = 'Completed' AND a."chapter_source" = 'Generated'
                    THEN 'Generated'
                ELSE 'LegacyImported'
            END,
            CASE WHEN a."status" = 'Completed' THEN 1 ELSE 0 END,
            a."input_fingerprint",
            a."chapter_fingerprint",
            CASE WHEN a."chapter_source" IS NOT NULL THEN 'Legacy' ELSE NULL END,
            a."analyzer_version",
            COALESCE(a."completed_at", a."updated_at", rv."updated_at", CURRENT_TIMESTAMP),
            COALESCE(a."completed_at", a."updated_at", rv."updated_at", CURRENT_TIMESTAMP),
            COALESCE(a."completed_at", a."updated_at", rv."updated_at", CURRENT_TIMESTAMP),
            rv."id",
            a."used_logo_id"
        FROM "recorded_videos" rv
        LEFT JOIN "recorded_video_cm_analyses" a ON a."recorded_video_id" = rv."id"
        WHERE rv."cm_sections" IS NOT NULL;
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "recorded_video_cm_results";
        ALTER TABLE "recorded_video_cm_analyses" DROP COLUMN "finished_at";
        ALTER TABLE "recorded_video_cm_analyses" DROP COLUMN "chapter_path_kind";
        ALTER TABLE "recorded_video_cm_analyses" DROP COLUMN "runtime_fingerprint";
        ALTER TABLE "recorded_video_cm_analyses" DROP COLUMN "attempt_key_sha256";
    """
