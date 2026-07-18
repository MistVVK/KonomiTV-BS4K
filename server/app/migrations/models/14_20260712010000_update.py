from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        PRAGMA foreign_keys=off;
        CREATE TABLE "recorded_videos_new" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "recorded_program_id" INT NOT NULL REFERENCES "recorded_programs" ("id") ON DELETE CASCADE,
            "status" VARCHAR(255) NOT NULL, "file_path" TEXT NOT NULL, "file_hash" TEXT NOT NULL,
            "file_size" INT NOT NULL, "file_created_at" TIMESTAMP NOT NULL, "file_modified_at" TIMESTAMP NOT NULL,
            "recording_start_time" TIMESTAMP, "recording_end_time" TIMESTAMP, "duration" REAL NOT NULL,
            "container_format" VARCHAR(255) NOT NULL, "has_video" INT NOT NULL DEFAULT 1,
            "has_audio" INT NOT NULL DEFAULT 1, "video_codec" VARCHAR(255), "video_codec_profile" VARCHAR(255),
            "video_scan_type" VARCHAR(255), "video_frame_rate" REAL, "video_resolution_width" INT,
            "video_resolution_height" INT, "has_video_stream_changes" INT NOT NULL DEFAULT 0,
            "primary_audio_codec" VARCHAR(255), "primary_audio_channel" VARCHAR(255),
            "primary_audio_sampling_rate" INT, "secondary_audio_codec" VARCHAR(255),
            "secondary_audio_channel" VARCHAR(255), "secondary_audio_sampling_rate" INT,
            "audio_tracks" JSON NOT NULL DEFAULT '[]', "audio_track_timeline" JSON NOT NULL DEFAULT '[]',
            "subtitle_tracks" JSON NOT NULL DEFAULT '[]', "key_frames" JSON NOT NULL,
            "segment_map" JSON NOT NULL DEFAULT '[]', "cm_sections" JSON, "thumbnail_info" JSON,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO "recorded_videos_new" (
            "id", "recorded_program_id", "status", "file_path", "file_hash", "file_size",
            "file_created_at", "file_modified_at", "recording_start_time", "recording_end_time", "duration",
            "container_format", "has_video", "has_audio", "video_codec", "video_codec_profile", "video_scan_type",
            "video_frame_rate", "video_resolution_width", "video_resolution_height", "has_video_stream_changes",
            "primary_audio_codec", "primary_audio_channel", "primary_audio_sampling_rate", "secondary_audio_codec",
            "secondary_audio_channel", "secondary_audio_sampling_rate", "audio_tracks", "audio_track_timeline",
            "subtitle_tracks", "key_frames", "segment_map", "cm_sections", "thumbnail_info", "created_at", "updated_at"
        ) SELECT
            "id", "recorded_program_id", "status", "file_path", "file_hash", "file_size",
            "file_created_at", "file_modified_at", "recording_start_time", "recording_end_time", "duration",
            "container_format", 1, 1, "video_codec", "video_codec_profile", "video_scan_type",
            "video_frame_rate", "video_resolution_width", "video_resolution_height", "has_video_stream_changes",
            "primary_audio_codec", "primary_audio_channel", "primary_audio_sampling_rate", "secondary_audio_codec",
            "secondary_audio_channel", "secondary_audio_sampling_rate", "audio_tracks",
            json_array(json_object('start_time', 0.0, 'end_time', "duration", 'tracks', json("audio_tracks"))),
            "subtitle_tracks", "key_frames", "segment_map", "cm_sections", "thumbnail_info", "created_at", "updated_at"
        FROM "recorded_videos";
        DROP TABLE "recorded_videos";
        ALTER TABLE "recorded_videos_new" RENAME TO "recorded_videos";
        CREATE INDEX "recorded_videos_file_path" ON "recorded_videos" ("file_path");
        CREATE INDEX "recorded_videos_file_hash" ON "recorded_videos" ("file_hash");
        CREATE INDEX "recorded_videos_recorded_program_id" ON "recorded_videos" ("recorded_program_id");
        CREATE INDEX "recorded_videos_status" ON "recorded_videos" ("status");
        PRAGMA foreign_keys=on;
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    # Nullable AV metadata cannot be represented by the previous schema without losing audio/video-only rows.
    return """
        DELETE FROM "recorded_videos" WHERE "has_video" = 0 OR "has_audio" = 0;
        ALTER TABLE "recorded_videos" DROP COLUMN "audio_track_timeline";
        ALTER TABLE "recorded_videos" DROP COLUMN "has_audio";
        ALTER TABLE "recorded_videos" DROP COLUMN "has_video";
    """
