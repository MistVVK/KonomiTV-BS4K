from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        PRAGMA foreign_keys=off;
        CREATE TABLE "cm_logos_new" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "path" TEXT NOT NULL,
            "filename" TEXT NOT NULL,
            "service_id" INT,
            "logo_name" TEXT NOT NULL,
            "file_format" VARCHAR(32) NOT NULL DEFAULT 'AmatsukazeExtendedV1',
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
        INSERT INTO "cm_logos_new" (
            "id", "path", "filename", "service_id", "logo_name", "file_format", "enabled",
            "file_hash", "file_size", "last_used_at", "missing", "deleted_at", "created_at", "updated_at",
            "generated_from_recorded_video_id"
        ) SELECT
            "id", "path", "filename", "service_id", "logo_name", 'AmatsukazeExtendedV1', "enabled",
            "file_hash", "file_size", "last_used_at", "missing", "deleted_at", "created_at", "updated_at",
            "generated_from_recorded_video_id"
        FROM "cm_logos";
        DROP TABLE "cm_logos";
        ALTER TABLE "cm_logos_new" RENAME TO "cm_logos";
        CREATE INDEX "idx_cm_logos_service_id" ON "cm_logos" ("service_id");
        CREATE UNIQUE INDEX "uid_cm_logos_path" ON "cm_logos" ("path");
        PRAGMA foreign_keys=on;
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        PRAGMA foreign_keys=off;
        CREATE TABLE "cm_logos_old" (
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
        INSERT INTO "cm_logos_old" (
            "id", "path", "filename", "service_id", "logo_name", "enabled", "file_hash", "file_size",
            "last_used_at", "missing", "deleted_at", "created_at", "updated_at", "generated_from_recorded_video_id"
        ) SELECT
            l."id", l."path", l."filename",
            COALESCE(
                l."service_id",
                (SELECT a."service_id" FROM "cm_logo_service_assignments" a WHERE a."logo_id" = l."id" LIMIT 1),
                0
            ),
            l."logo_name", l."enabled", l."file_hash", l."file_size", l."last_used_at", l."missing",
            l."deleted_at", l."created_at", l."updated_at", l."generated_from_recorded_video_id"
        FROM "cm_logos" l;
        DROP TABLE "cm_logos";
        ALTER TABLE "cm_logos_old" RENAME TO "cm_logos";
        CREATE INDEX "idx_cm_logos_service_id" ON "cm_logos" ("service_id");
        CREATE UNIQUE INDEX "uid_cm_logos_path" ON "cm_logos" ("path");
        PRAGMA foreign_keys=on;
    """
