from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    del db
    return """
        PRAGMA foreign_keys=off;
        CREATE TABLE "konomitv_bs4k_bangumi_episode_completions_new" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "bangumi_episode_id" INT NOT NULL,
            "bangumi_user_id" INT NOT NULL,
            "completed_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
            "source_recorded_program_id" INT REFERENCES "recorded_programs" ("id") ON DELETE SET NULL,
            CONSTRAINT "uid_konomitv_bs4k_bangumi_episode_completions_account" UNIQUE ("user_id", "bangumi_user_id", "bangumi_episode_id")
        );
        DROP TABLE IF EXISTS "konomitv_bs4k_bangumi_episode_completions";
        ALTER TABLE "konomitv_bs4k_bangumi_episode_completions_new" RENAME TO "konomitv_bs4k_bangumi_episode_completions";
        PRAGMA foreign_keys=on;
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    del db
    return """
        PRAGMA foreign_keys=off;
        CREATE TABLE "konomitv_bs4k_bangumi_episode_completions_old" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "bangumi_episode_id" INT NOT NULL,
            "completed_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
            "source_recorded_program_id" INT REFERENCES "recorded_programs" ("id") ON DELETE SET NULL,
            CONSTRAINT "uid_konomitv_bs4k_bangumi_episode_completions_user_episode" UNIQUE ("user_id", "bangumi_episode_id")
        );
        DROP TABLE IF EXISTS "konomitv_bs4k_bangumi_episode_completions";
        ALTER TABLE "konomitv_bs4k_bangumi_episode_completions_old" RENAME TO "konomitv_bs4k_bangumi_episode_completions";
        PRAGMA foreign_keys=on;
    """
