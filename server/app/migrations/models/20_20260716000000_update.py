from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "analysis_task_executions" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "task_type" VARCHAR(64) NOT NULL,
            "status" VARCHAR(32) NOT NULL,
            "trigger" VARCHAR(32) NOT NULL,
            "title" TEXT NOT NULL,
            "stage" VARCHAR(64),
            "progress" REAL,
            "stage_history" JSON NOT NULL DEFAULT '[]',
            "current_count" INT NOT NULL DEFAULT 0,
            "total_count" INT NOT NULL DEFAULT 0,
            "succeeded_count" INT NOT NULL DEFAULT 0,
            "failed_count" INT NOT NULL DEFAULT 0,
            "skipped_count" INT NOT NULL DEFAULT 0,
            "summary" JSON,
            "error_code" VARCHAR(255),
            "error_message" TEXT,
            "started_at" TIMESTAMP,
            "completed_at" TIMESTAMP,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "parent_id" INT REFERENCES "analysis_task_executions" ("id") ON DELETE CASCADE,
            "recorded_video_id" INT REFERENCES "recorded_videos" ("id") ON DELETE SET NULL,
            "cm_logo_generation_batch_id" INT REFERENCES "cm_logo_generation_batches" ("id") ON DELETE SET NULL
        );
        CREATE INDEX "idx_analysis_tasks_type" ON "analysis_task_executions" ("task_type");
        CREATE INDEX "idx_analysis_tasks_status" ON "analysis_task_executions" ("status");
        CREATE INDEX "idx_analysis_tasks_created" ON "analysis_task_executions" ("created_at");
        CREATE INDEX "idx_analysis_tasks_parent" ON "analysis_task_executions" ("parent_id");

        INSERT INTO "analysis_task_executions" (
            "task_type", "status", "trigger", "title", "stage", "progress",
            "started_at", "completed_at", "created_at", "updated_at",
            "recorded_video_id", "cm_logo_generation_batch_id"
        )
        SELECT
            'CMLogoGeneration',
            CASE b."state"
                WHEN 'Succeeded' THEN 'Succeeded'
                WHEN 'Exhausted' THEN 'Failed'
                WHEN 'Interrupted' THEN 'Interrupted'
                WHEN 'Running' THEN 'Interrupted'
                ELSE 'Skipped'
            END,
            CASE WHEN b."explicit" = 1 THEN 'Manual' ELSE 'Automatic' END,
            rp."title",
            'LegacyImport',
            CASE WHEN b."state" = 'Succeeded' THEN 1.0 ELSE NULL END,
            b."created_at",
            COALESCE(b."completed_at", b."created_at"),
            b."created_at",
            COALESCE(b."completed_at", b."created_at"),
            b."recorded_video_id",
            b."id"
        FROM "cm_logo_generation_batches" b
        JOIN "recorded_videos" rv ON rv."id" = b."recorded_video_id"
        JOIN "recorded_programs" rp ON rp."id" = rv."recorded_program_id";
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return 'DROP TABLE IF EXISTS "analysis_task_executions";'
