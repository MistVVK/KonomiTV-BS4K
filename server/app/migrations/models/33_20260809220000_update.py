from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    """シーズンのみの正本と AI 提案 outcome を追加する。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        既存の番号付き回と AI 提案を新しい列へ補完する SQLite SQL。
    """

    del db
    return """
        ALTER TABLE "recorded_episode_resolutions"
            ADD COLUMN "season_number" INT;
        ALTER TABLE "recorded_episode_resolutions"
            ADD COLUMN "proposed_outcome" VARCHAR(32);

        UPDATE "recorded_episode_resolutions"
        SET "season_number" = (
            SELECT "season_number" FROM "series_episodes"
            WHERE "series_episodes"."id" = "recorded_episode_resolutions"."episode_id"
        )
        WHERE "episode_id" IS NOT NULL;

        UPDATE "recorded_episode_resolutions"
        SET "proposed_outcome" = CASE
            WHEN "lookup_outcome" = 'InsufficientEvidence' THEN 'InsufficientEvidence'
            WHEN "lookup_outcome" = 'Resolved'
             AND "web_search_performed" = 1
             AND "error_code" IS NULL
             AND "proposed_season_number" IS NOT NULL
             AND "proposed_episode_number" IS NOT NULL THEN 'Resolved'
            WHEN "lookup_outcome" = 'NotNumbered'
             AND "web_search_performed" = 1
             AND "error_code" IS NULL THEN 'NotNumbered'
            ELSE NULL
        END;
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    """追加した正本シーズンと AI 提案 outcome だけを除去する。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        既存の話数・Episode・引用・AI 監査を維持する SQLite SQL。
    """

    del db
    return """
        ALTER TABLE "recorded_episode_resolutions" DROP COLUMN "proposed_outcome";
        ALTER TABLE "recorded_episode_resolutions" DROP COLUMN "season_number";
    """
