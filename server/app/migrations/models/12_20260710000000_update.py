from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "recorded_videos" ADD "audio_tracks" JSON NOT NULL DEFAULT '[]';
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "recorded_videos" DROP COLUMN "audio_tracks";
    """
