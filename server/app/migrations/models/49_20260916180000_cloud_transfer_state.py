from tortoise import BaseDBAsyncClient


RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    """既存録画を変更せず、移動ジョブとクラウド所在の保存先を追加する。

    Args:
        db: migrationの接続。
    Returns:
        追加するテーブルと制約のDDL。
    """
    return """
        CREATE TABLE "konomitv_bs4k_cloud_recordings" (
            "id" CHAR(36) NOT NULL PRIMARY KEY,
            "recorded_video_id" INT NOT NULL UNIQUE REFERENCES "recorded_videos" ("id") ON DELETE RESTRICT,
            "location" VARCHAR(16) NOT NULL DEFAULT 'Local' CHECK ("location" IN ('Local', 'Cloud')),
            "owner_id" INT NOT NULL,
            "connection_id" CHAR(36) NOT NULL,
            "folder" VARCHAR(1024) NOT NULL,
            "original_path" TEXT NOT NULL,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE "konomitv_bs4k_cloud_transfers" (
            "id" CHAR(36) NOT NULL PRIMARY KEY,
            "recorded_video_id" INT NOT NULL,
            "recording_uuid" CHAR(36) NOT NULL,
            "requested_by" INT NOT NULL,
            "direction" VARCHAR(16) NOT NULL CHECK ("direction" IN ('ToCloud', 'ToLocal')),
            "active_video_id" INT UNIQUE CHECK ("active_video_id" IS NULL OR "active_video_id" = "recorded_video_id"),
            "status" VARCHAR(16) NOT NULL DEFAULT 'Pending' CHECK ("status" IN ('Pending', 'Running', 'Failed', 'Completed', 'Cancelled')),
            "phase" VARCHAR(16) NOT NULL DEFAULT 'Preparing' CHECK ("phase" IN ('Preparing', 'Copying', 'Verifying', 'Publishing', 'Cleaning', 'Completed', 'Cancelling', 'Cancelled')),
            "cancel_requested" INT NOT NULL DEFAULT 0 CHECK ("cancel_requested" IN (0, 1)),
            "owner_id" INT NOT NULL,
            "connection_id" CHAR(36) NOT NULL,
            "folder" VARCHAR(1024) NOT NULL,
            "local_path" TEXT NOT NULL,
            "manifest" JSON,
            "worker_token" CHAR(36),
            "attempt" INT NOT NULL DEFAULT 0,
            "error_code" VARCHAR(64),
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (("status" = 'Completed' AND "phase" = 'Completed' AND "active_video_id" IS NULL)
                OR ("status" = 'Cancelled' AND "phase" = 'Cancelled' AND "active_video_id" IS NULL)
                OR ("status" NOT IN ('Completed', 'Cancelled') AND "phase" NOT IN ('Completed', 'Cancelled') AND "active_video_id" IS NOT NULL)),
            CHECK ("cancel_requested" = 0 OR "phase" IN ('Preparing', 'Copying', 'Verifying', 'Cancelling', 'Cancelled')),
            CHECK ("phase" NOT IN ('Cancelling', 'Cancelled') OR "cancel_requested" = 1),
            CHECK (("status" = 'Running' AND "worker_token" IS NOT NULL)
                OR ("status" != 'Running' AND "worker_token" IS NULL))
        );
        CREATE INDEX "idx_cloud_transfer_video" ON "konomitv_bs4k_cloud_transfers" ("recorded_video_id");
        CREATE INDEX "idx_cloud_transfer_status" ON "konomitv_bs4k_cloud_transfers" ("status");
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    """移動履歴や所在が存在する場合は情報を失うdowngradeを拒否する。

    Args:
        db: migrationの接続。
    Returns:
        未使用の場合だけテーブルを削除するDDL。
    """
    rows = await db.execute_query_dict(
        'SELECT 1 FROM "konomitv_bs4k_cloud_transfers" UNION ALL SELECT 1 FROM "konomitv_bs4k_cloud_recordings" LIMIT 1',
    )
    if rows:
        raise RuntimeError('Cloud transfer state must not be discarded by downgrade.')
    return 'DROP TABLE "konomitv_bs4k_cloud_transfers"; DROP TABLE "konomitv_bs4k_cloud_recordings";'


MODELS_STATE = (
    "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"
    "x04g=="
)
