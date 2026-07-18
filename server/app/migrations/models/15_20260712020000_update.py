from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        UPDATE "recorded_videos"
        SET "audio_tracks" = json_array(json_object(
            'index', 1,
            'codec', "primary_audio_codec",
            'channel', "primary_audio_channel",
            'sampling_rate', "primary_audio_sampling_rate",
            'language', NULL
        ))
        WHERE json_array_length("audio_tracks") = 0
          AND "primary_audio_codec" IS NOT NULL;

        UPDATE "recorded_videos"
        SET "audio_track_timeline" = json_array(json_object(
            'start_time', 0.0,
            'end_time', "duration",
            'tracks', json("audio_tracks")
        ))
        WHERE json_array_length("audio_track_timeline") = 0
           OR NOT EXISTS (
               SELECT 1 FROM json_each("audio_track_timeline") AS interval
               WHERE json_array_length(json_extract(interval.value, '$.tracks')) > 0
           );

        UPDATE "recorded_videos"
        SET "has_video" = CASE WHEN "video_codec" IS NULL THEN 0 ELSE 1 END,
            "has_audio" = CASE WHEN json_array_length("audio_tracks") > 0 THEN 1 ELSE 0 END;
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        SELECT 1;
    """
