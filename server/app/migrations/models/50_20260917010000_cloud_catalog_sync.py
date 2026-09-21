from tortoise import BaseDBAsyncClient


RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    """接続別・領域別の同期要求を追加する。

    Args:
        db: migrationの接続。
    Returns:
        同期状態のDDL。
    """
    return """
        CREATE TABLE "konomitv_bs4k_cloud_publications" (
            "id" INTEGER NOT NULL PRIMARY KEY,
            "recorded_video_id" INT NOT NULL,
            "recording_uuid" CHAR(36) NOT NULL,
            "owner_id" INT NOT NULL,
            "connection_id" CHAR(36) NOT NULL,
            "folder" VARCHAR(1024) NOT NULL,
            "key_identity" VARCHAR(64) NOT NULL,
            "status" VARCHAR(16) NOT NULL DEFAULT 'Pending'
                CHECK ("status" IN ('Pending', 'Publishing', 'Completed', 'Failed', 'Superseded')),
            "plan" JSON,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        ALTER TABLE "konomitv_bs4k_cloud_recordings" ADD "manifest" JSON;
        ALTER TABLE "konomitv_bs4k_cloud_recordings" ADD "key_identity" VARCHAR(64);
        ALTER TABLE "konomitv_bs4k_cloud_transfers" ADD "key_identity" VARCHAR(64);
        ALTER TABLE "konomitv_bs4k_cloud_transfers" ADD "key_revision" VARCHAR(64);
        CREATE TABLE "konomitv_bs4k_cloud_deleted_recordings" (
            "id" CHAR(36) NOT NULL PRIMARY KEY,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE "konomitv_bs4k_cloud_catalog_syncs" (
            "id" CHAR(36) NOT NULL PRIMARY KEY,
            "owner_id" INT NOT NULL,
            "connection_id" CHAR(36) NOT NULL,
            "folder" VARCHAR(1024) NOT NULL,
            "status" VARCHAR(16) NOT NULL DEFAULT 'Pending' CHECK ("status" IN ('Pending', 'Running', 'Completed', 'Failed')),
            "error_code" VARCHAR(64),
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE ("owner_id", "connection_id", "folder")
        );
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    """実行中・受付済み同期を破棄しない場合だけ同期履歴を戻す。

    Args:
        db: migrationの接続。
    Returns:
        同期履歴テーブルの削除DDL。
    """
    pending = await db.execute_query_dict(
        'SELECT 1 FROM "konomitv_bs4k_cloud_catalog_syncs" WHERE "status" IN (\'Pending\', \'Running\') '
        'UNION ALL SELECT 1 FROM "konomitv_bs4k_cloud_deleted_recordings" '
        'UNION ALL SELECT 1 FROM "konomitv_bs4k_cloud_transfers" '
        'UNION ALL SELECT 1 FROM "konomitv_bs4k_cloud_publications" '
        'UNION ALL SELECT 1 FROM "konomitv_bs4k_cloud_recordings" LIMIT 1',
    )
    if pending:
        raise RuntimeError('Pending cloud catalog requests must not be discarded.')
    return '''DROP TABLE "konomitv_bs4k_cloud_catalog_syncs";
        DROP TABLE "konomitv_bs4k_cloud_publications";
        DROP TABLE "konomitv_bs4k_cloud_deleted_recordings";
        ALTER TABLE "konomitv_bs4k_cloud_recordings" DROP COLUMN "manifest";
        ALTER TABLE "konomitv_bs4k_cloud_recordings" DROP COLUMN "key_identity";
        ALTER TABLE "konomitv_bs4k_cloud_transfers" DROP COLUMN "key_identity";
        ALTER TABLE "konomitv_bs4k_cloud_transfers" DROP COLUMN "key_revision";'''


MODELS_STATE = (
    "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"
    "fSrcjpc5Z6fhatWFOnpIP9+f8B/tl7zQ=="
)
