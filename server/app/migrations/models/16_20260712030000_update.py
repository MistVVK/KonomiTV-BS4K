from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "recorded_videos" ADD COLUMN "ts_source_base_dts" BIGINT;
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "recorded_videos" DROP COLUMN "ts_source_base_dts";
    """
