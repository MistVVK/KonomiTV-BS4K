from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    """解析履歴に録画ファイルパスを追加し、既存の録画紐付けから埋める。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        analysis_task_executions へ file_path を追加する SQLite SQL。
    """

    del db
    return """
        ALTER TABLE "analysis_task_executions"
            ADD COLUMN "file_path" TEXT;

        -- 録画に紐づく既存履歴は、削除済み録画でも見られるよう当時のパスを複製する。
        -- Docker 内部接頭辞はユーザー表示向けに落として保存する。
        UPDATE "analysis_task_executions"
        SET "file_path" = (
            SELECT CASE
                WHEN "recorded_videos"."file_path" LIKE '/host-rootfs/%'
                    THEN substr("recorded_videos"."file_path", 13)
                ELSE "recorded_videos"."file_path"
            END
            FROM "recorded_videos"
            WHERE "recorded_videos"."id" = "analysis_task_executions"."recorded_video_id"
        )
        WHERE "recorded_video_id" IS NOT NULL
          AND ("file_path" IS NULL OR "file_path" = '');
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    """SQLite では列削除が難しいため、ダウングレードは空操作とする。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        変更を行わない SQL。
    """

    del db
    return ''
