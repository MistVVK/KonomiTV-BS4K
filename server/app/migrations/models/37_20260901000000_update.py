from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    del db
    return """
        ALTER TABLE "series" ADD COLUMN "normalized_title" VARCHAR(512);
        CREATE UNIQUE INDEX IF NOT EXISTS "uid_series_normalized_title" ON "series" ("normalized_title");
        ALTER TABLE "series" ADD COLUMN "bangumi_subject_id" INT;
        CREATE UNIQUE INDEX IF NOT EXISTS "uid_series_bangumi_subject_id" ON "series" ("bangumi_subject_id");
        ALTER TABLE "series" ADD COLUMN "bangumi_subject_name" TEXT;
        ALTER TABLE "series" ADD COLUMN "bangumi_subject_name_cn" TEXT;
        ALTER TABLE "series" ADD COLUMN "bangumi_subject_summary" TEXT;
        ALTER TABLE "series" ADD COLUMN "bangumi_subject_image_url" TEXT;
        ALTER TABLE "series_episodes" ADD COLUMN "bangumi_episode_id" INT;
        ALTER TABLE "recorded_programs" ADD COLUMN "bangumi_subject_id" INT;
        ALTER TABLE "recorded_programs" ADD COLUMN "bangumi_episode_id" INT;
        CREATE TABLE IF NOT EXISTS "series_aliases" (
            "normalized_title" VARCHAR(512) NOT NULL PRIMARY KEY,
            "series_id" INT NOT NULL REFERENCES "series" ("id") ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS "konomitv_bs4k_bangumi_episode_completions" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "bangumi_episode_id" INT NOT NULL,
            "completed_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
            "source_recorded_program_id" INT REFERENCES "recorded_programs" ("id") ON DELETE SET NULL,
            CONSTRAINT "uid_konomitv_bs4k_bangumi_episode_completions_user_episode" UNIQUE ("user_id", "bangumi_episode_id")
        );
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    del db
    return """
        DROP TABLE IF EXISTS "konomitv_bs4k_bangumi_episode_completions";
        DROP TABLE IF EXISTS "series_aliases";
        ALTER TABLE "recorded_programs" DROP COLUMN "bangumi_episode_id";
        ALTER TABLE "recorded_programs" DROP COLUMN "bangumi_subject_id";
        ALTER TABLE "series_episodes" DROP COLUMN "bangumi_episode_id";
        DROP INDEX IF EXISTS "uid_series_bangumi_subject_id";
        ALTER TABLE "series" DROP COLUMN "bangumi_subject_image_url";
        ALTER TABLE "series" DROP COLUMN "bangumi_subject_summary";
        ALTER TABLE "series" DROP COLUMN "bangumi_subject_name_cn";
        ALTER TABLE "series" DROP COLUMN "bangumi_subject_name";
        ALTER TABLE "series" DROP COLUMN "bangumi_subject_id";
        DROP INDEX IF EXISTS "uid_series_normalized_title";
        ALTER TABLE "series" DROP COLUMN "normalized_title";
    """
