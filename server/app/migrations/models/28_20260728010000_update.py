from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    """手動話数レーンを AI 提案と分離して保持する列を追加する。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        recorded_episode_resolutions へ manual_* 列を追加する SQLite SQL。
    """

    del db
    return """
        ALTER TABLE "recorded_episode_resolutions"
            ADD COLUMN "manual_episode_id" INT REFERENCES "series_episodes" ("id") ON DELETE SET NULL;
        ALTER TABLE "recorded_episode_resolutions"
            ADD COLUMN "manual_season_number" INT;
        ALTER TABLE "recorded_episode_resolutions"
            ADD COLUMN "manual_episode_number" VARCHAR(40);
        ALTER TABLE "recorded_episode_resolutions"
            ADD COLUMN "manual_status" VARCHAR(32);

        -- 既存の Manual 正本だけを手動レーンへ複製する。AI 提案列は触らない。
        UPDATE "recorded_episode_resolutions"
        SET
            "manual_episode_id" = "episode_id",
            "manual_season_number" = (
                SELECT "season_number" FROM "series_episodes"
                WHERE "series_episodes"."id" = "recorded_episode_resolutions"."episode_id"
            ),
            "manual_episode_number" = (
                SELECT "episode_number" FROM "series_episodes"
                WHERE "series_episodes"."id" = "recorded_episode_resolutions"."episode_id"
            ),
            "manual_status" = CASE
                WHEN "status" IN ('Resolved', 'Unknown', 'NotNumbered') THEN "status"
                ELSE NULL
            END
        WHERE "source" = 'Manual';
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
