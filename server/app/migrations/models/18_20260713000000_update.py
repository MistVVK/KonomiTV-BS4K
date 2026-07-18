from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "recorded_videos" ADD COLUMN "playback_index_status" VARCHAR(255) NOT NULL DEFAULT 'Pending';
        CREATE INDEX "idx_recorded__playbac_983ce4" ON "recorded_videos" ("playback_index_status");
        ALTER TABLE "recorded_videos" ADD COLUMN "playback_index_version" INT;
        ALTER TABLE "recorded_videos" ADD COLUMN "playback_indexed_at" TIMESTAMP;
        ALTER TABLE "recorded_videos" ADD COLUMN "playback_index_error_code" VARCHAR(255);
        ALTER TABLE "recorded_videos" ADD COLUMN "video_stream_timeline" JSON;
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP INDEX "idx_recorded__playbac_983ce4";
        ALTER TABLE "recorded_videos" DROP COLUMN "video_stream_timeline";
        ALTER TABLE "recorded_videos" DROP COLUMN "playback_index_error_code";
        ALTER TABLE "recorded_videos" DROP COLUMN "playback_indexed_at";
        ALTER TABLE "recorded_videos" DROP COLUMN "playback_index_version";
        ALTER TABLE "recorded_videos" DROP COLUMN "playback_index_status";
    """
