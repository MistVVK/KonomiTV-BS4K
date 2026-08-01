from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    """
    CM設定・ロゴ・解析履歴に残ったDocker内部パスをホスト表現へ正規化する。

    Args:
        db (BaseDBAsyncClient): Aerichが渡すDBクライアント。

    Returns:
        str: SQLite向けマイグレーションSQL。
    """

    del db
    return """
        UPDATE "cm_analysis_settings"
        SET "logo_directory" = CASE
            WHEN "logo_directory" = '/host-rootfs' THEN '/'
            ELSE substr("logo_directory", 13)
        END
        WHERE "logo_directory" = '/host-rootfs'
           OR "logo_directory" LIKE '/host-rootfs/%';
        -- 過去の保存値に残り得る二重接頭辞も、先頭から2回目だけ除去する。
        UPDATE "cm_analysis_settings"
        SET "logo_directory" = CASE
            WHEN "logo_directory" = '/host-rootfs' THEN '/'
            ELSE substr("logo_directory", 13)
        END
        WHERE "logo_directory" = '/host-rootfs'
           OR "logo_directory" LIKE '/host-rootfs/%';

        UPDATE "cm_analysis_excluded_directories"
        SET "path" = CASE
                WHEN "path" = '/host-rootfs' THEN '/'
                WHEN "path" LIKE '/host-rootfs/%' THEN substr("path", 13)
                ELSE "path"
            END,
            "resolved_path" = CASE
                WHEN "resolved_path" = '/host-rootfs' THEN '/'
                WHEN "resolved_path" LIKE '/host-rootfs/%' THEN substr("resolved_path", 13)
                ELSE "resolved_path"
            END
        WHERE "path" = '/host-rootfs'
           OR "path" LIKE '/host-rootfs/%'
           OR "resolved_path" = '/host-rootfs'
           OR "resolved_path" LIKE '/host-rootfs/%';
        -- pathとresolved_pathの二重接頭辞を同じ境界で正規化する。
        UPDATE "cm_analysis_excluded_directories"
        SET "path" = CASE
                WHEN "path" = '/host-rootfs' THEN '/'
                WHEN "path" LIKE '/host-rootfs/%' THEN substr("path", 13)
                ELSE "path"
            END,
            "resolved_path" = CASE
                WHEN "resolved_path" = '/host-rootfs' THEN '/'
                WHEN "resolved_path" LIKE '/host-rootfs/%' THEN substr("resolved_path", 13)
                ELSE "resolved_path"
            END
        WHERE "path" = '/host-rootfs'
           OR "path" LIKE '/host-rootfs/%'
           OR "resolved_path" = '/host-rootfs'
           OR "resolved_path" LIKE '/host-rootfs/%';

        UPDATE "recorded_video_cm_analyses"
        SET "matched_exclusion_path" = CASE
            WHEN "matched_exclusion_path" = '/host-rootfs' THEN '/'
            ELSE substr("matched_exclusion_path", 13)
        END
        WHERE "matched_exclusion_path" = '/host-rootfs'
           OR "matched_exclusion_path" LIKE '/host-rootfs/%';
        -- 除外判定履歴に残った二重接頭辞も除去する。
        UPDATE "recorded_video_cm_analyses"
        SET "matched_exclusion_path" = CASE
            WHEN "matched_exclusion_path" = '/host-rootfs' THEN '/'
            ELSE substr("matched_exclusion_path", 13)
        END
        WHERE "matched_exclusion_path" = '/host-rootfs'
           OR "matched_exclusion_path" LIKE '/host-rootfs/%';

        CREATE TEMP TABLE "_cm_logo_host_path_map" AS
        WITH "once_normalized" AS (
            SELECT
                "id",
                "path" AS "original_path",
                CASE
                    WHEN "path" = '/host-rootfs' THEN '/'
                    WHEN "path" LIKE '/host-rootfs/%' THEN substr("path", 13)
                    ELSE "path"
                END AS "path"
            FROM "cm_logos"
        ),
        "twice_normalized" AS (
            SELECT
                "id",
                "original_path",
                CASE
                    WHEN "path" = '/host-rootfs' THEN '/'
                    WHEN "path" LIKE '/host-rootfs/%' THEN substr("path", 13)
                    ELSE "path"
                END AS "normalized_path"
            FROM "once_normalized"
        )
        SELECT
            "legacy"."id" AS "legacy_id",
            "legacy"."normalized_path",
            (
                SELECT "candidate"."id"
                FROM "twice_normalized" AS "candidate"
                WHERE "candidate"."normalized_path" = "legacy"."normalized_path"
                ORDER BY
                    CASE
                        WHEN "candidate"."original_path" = "candidate"."normalized_path" THEN 0
                        ELSE 1
                    END,
                    "candidate"."id"
                LIMIT 1
            ) AS "canonical_id"
        FROM "twice_normalized" AS "legacy";

        UPDATE "cm_logo_service_assignments"
        SET "logo_id" = (
            SELECT "canonical_id"
            FROM "_cm_logo_host_path_map"
            WHERE "legacy_id" = "cm_logo_service_assignments"."logo_id"
        )
        WHERE EXISTS (
            SELECT 1
            FROM "_cm_logo_host_path_map"
            WHERE "legacy_id" = "cm_logo_service_assignments"."logo_id"
        );
        UPDATE "recorded_video_cm_analyses"
        SET "used_logo_id" = (
            SELECT "canonical_id"
            FROM "_cm_logo_host_path_map"
            WHERE "legacy_id" = "recorded_video_cm_analyses"."used_logo_id"
        )
        WHERE EXISTS (
            SELECT 1
            FROM "_cm_logo_host_path_map"
            WHERE "legacy_id" = "recorded_video_cm_analyses"."used_logo_id"
        );
        UPDATE "recorded_video_cm_results"
        SET "used_logo_id" = (
            SELECT "canonical_id"
            FROM "_cm_logo_host_path_map"
            WHERE "legacy_id" = "recorded_video_cm_results"."used_logo_id"
        )
        WHERE EXISTS (
            SELECT 1
            FROM "_cm_logo_host_path_map"
            WHERE "legacy_id" = "recorded_video_cm_results"."used_logo_id"
        );

        DELETE FROM "cm_logos"
        WHERE "id" IN (
            SELECT "legacy_id"
            FROM "_cm_logo_host_path_map"
            WHERE "legacy_id" != "canonical_id"
        );
        UPDATE "cm_logos"
        SET "path" = (
            SELECT "normalized_path"
            FROM "_cm_logo_host_path_map"
            WHERE "legacy_id" = "cm_logos"."id"
        )
        WHERE "id" IN (
            SELECT "canonical_id"
            FROM "_cm_logo_host_path_map"
        );
        DROP TABLE "_cm_logo_host_path_map";

        UPDATE "recorded_video_cm_analyses"
        SET "error_message" = CASE
            WHEN "error_message" = '/host-rootfs' THEN '/'
            WHEN "error_message" LIKE '/host-rootfs/%' THEN substr("error_message", 13)
            ELSE replace(replace(replace(replace(replace(replace(
                "error_message",
                ' /host-rootfs/', ' /'),
                '"/host-rootfs/', '"/'),
                '''/host-rootfs/', '''/'),
                '(/host-rootfs/', '(/'),
                ':/host-rootfs/', ':/'),
                '=/host-rootfs/', '=/')
        END;
        UPDATE "analysis_task_executions"
        SET "error_message" = CASE
            WHEN "error_message" = '/host-rootfs' THEN '/'
            WHEN "error_message" LIKE '/host-rootfs/%' THEN substr("error_message", 13)
            ELSE replace(replace(replace(replace(replace(replace(
                "error_message",
                ' /host-rootfs/', ' /'),
                '"/host-rootfs/', '"/'),
                '''/host-rootfs/', '''/'),
                '(/host-rootfs/', '(/'),
                ':/host-rootfs/', ':/'),
                '=/host-rootfs/', '=/')
        END;
        UPDATE "cm_logo_generation_attempts"
        SET "failure_reason" = CASE
            WHEN "failure_reason" = '/host-rootfs' THEN '/'
            WHEN "failure_reason" LIKE '/host-rootfs/%' THEN substr("failure_reason", 13)
            ELSE replace(replace(replace(replace(replace(replace(
                "failure_reason",
                ' /host-rootfs/', ' /'),
                '"/host-rootfs/', '"/'),
                '''/host-rootfs/', '''/'),
                '(/host-rootfs/', '(/'),
                ':/host-rootfs/', ':/'),
                '=/host-rootfs/', '=/')
        END;

        -- エラー本文中の二重接頭辞は1回目の置換後にも残るため、同じ安全な置換をもう一度適用する。
        UPDATE "recorded_video_cm_analyses"
        SET "error_message" = CASE
            WHEN "error_message" = '/host-rootfs' THEN '/'
            WHEN "error_message" LIKE '/host-rootfs/%' THEN substr("error_message", 13)
            ELSE replace(replace(replace(replace(replace(replace(
                "error_message",
                ' /host-rootfs/', ' /'),
                '"/host-rootfs/', '"/'),
                '''/host-rootfs/', '''/'),
                '(/host-rootfs/', '(/'),
                ':/host-rootfs/', ':/'),
                '=/host-rootfs/', '=/')
        END;
        UPDATE "analysis_task_executions"
        SET "error_message" = CASE
            WHEN "error_message" = '/host-rootfs' THEN '/'
            WHEN "error_message" LIKE '/host-rootfs/%' THEN substr("error_message", 13)
            ELSE replace(replace(replace(replace(replace(replace(
                "error_message",
                ' /host-rootfs/', ' /'),
                '"/host-rootfs/', '"/'),
                '''/host-rootfs/', '''/'),
                '(/host-rootfs/', '(/'),
                ':/host-rootfs/', ':/'),
                '=/host-rootfs/', '=/')
        END;
        UPDATE "cm_logo_generation_attempts"
        SET "failure_reason" = CASE
            WHEN "failure_reason" = '/host-rootfs' THEN '/'
            WHEN "failure_reason" LIKE '/host-rootfs/%' THEN substr("failure_reason", 13)
            ELSE replace(replace(replace(replace(replace(replace(
                "failure_reason",
                ' /host-rootfs/', ' /'),
                '"/host-rootfs/', '"/'),
                '''/host-rootfs/', '''/'),
                '(/host-rootfs/', '(/'),
                ':/host-rootfs/', ':/'),
                '=/host-rootfs/', '=/')
        END;
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    """
    ホストパスへ正規化した値は旧内部表現へ戻さない。

    Args:
        db (BaseDBAsyncClient): Aerichが渡すDBクライアント。

    Returns:
        str: スキーマ変更がないため空のSQL。
    """

    del db
    return ''
