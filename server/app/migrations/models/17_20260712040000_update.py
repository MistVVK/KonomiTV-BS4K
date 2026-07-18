from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "recorded_videos" ADD COLUMN "analyzed_at" TIMESTAMP;
        ALTER TABLE "recorded_videos" ADD COLUMN "analysis_git_commit" VARCHAR(255);
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "recorded_videos" DROP COLUMN "analysis_git_commit";
        ALTER TABLE "recorded_videos" DROP COLUMN "analyzed_at";
    """
