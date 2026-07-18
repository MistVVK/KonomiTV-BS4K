import asyncio
import importlib
import sqlite3


def test_cm_result_migration_preserves_published_results_and_attempt_history() -> None:
    """公開済みCM区間を結果テーブルへ移し、従来の解析・ロゴ履歴を保持する。"""

    connection = sqlite3.connect(':memory:')
    connection.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE recorded_videos (
            id INTEGER PRIMARY KEY,
            cm_sections JSON,
            updated_at TIMESTAMP NOT NULL
        );
        CREATE TABLE cm_logos (id INTEGER PRIMARY KEY);
        CREATE TABLE recorded_video_cm_analyses (
            id INTEGER PRIMARY KEY,
            recorded_video_id INTEGER NOT NULL UNIQUE REFERENCES recorded_videos (id) ON DELETE CASCADE,
            status VARCHAR(32) NOT NULL,
            chapter_source VARCHAR(32),
            input_fingerprint JSON,
            chapter_fingerprint JSON,
            analyzer_version VARCHAR(255),
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            error_code VARCHAR(255),
            updated_at TIMESTAMP NOT NULL,
            used_logo_id INTEGER REFERENCES cm_logos (id) ON DELETE SET NULL
        );
        CREATE TABLE cm_logo_generation_batches (
            id INTEGER PRIMARY KEY,
            recorded_video_id INTEGER NOT NULL REFERENCES recorded_videos (id) ON DELETE CASCADE
        );
        INSERT INTO cm_logos VALUES (7);
        INSERT INTO recorded_videos VALUES (1, '[{"start_time":10,"end_time":25}]', '2026-07-16 10:00:00');
        INSERT INTO recorded_videos VALUES (2, '[]', '2026-07-16 11:00:00');
        INSERT INTO recorded_videos VALUES (3, NULL, '2026-07-16 12:00:00');
        INSERT INTO recorded_videos VALUES (4, '[{"start_time":30,"end_time":45}]', '2026-07-16 13:00:00');
        INSERT INTO recorded_video_cm_analyses VALUES (
            10, 1, 'Completed', 'Generated', '{"size":100}', '{"exists":true}',
            'KonomiTV-CM-2', '2026-07-16 09:00:00', '2026-07-16 09:05:00', NULL,
            '2026-07-16 09:05:00', 7
        );
        INSERT INTO recorded_video_cm_analyses VALUES (
            11, 3, 'Failed', NULL, '{"size":200}', '{"exists":false}',
            NULL, '2026-07-16 12:00:00', NULL, 'ProcessFailed',
            '2026-07-16 12:05:00', NULL
        );
        INSERT INTO recorded_video_cm_analyses VALUES (
            12, 4, 'Completed', 'Existing', '{"size":300}', '{"exists":true}',
            NULL, '2026-07-16 13:00:00', '2026-07-16 13:01:00', NULL,
            '2026-07-16 13:01:00', NULL
        );
        INSERT INTO cm_logo_generation_batches VALUES (20, 1);
    """)
    migration = importlib.import_module('app.migrations.models.21_20260717000000_update')
    migration_sql = asyncio.run(migration.upgrade(None))
    connection.executescript(migration_sql)

    rows = connection.execute("""
        SELECT recorded_video_id, source, verified, chapter_path_kind, pipeline_version, used_logo_id
        FROM recorded_video_cm_results ORDER BY recorded_video_id
    """).fetchall()
    assert rows == [
        (1, 'Generated', 1, 'Legacy', 'KonomiTV-CM-2', 7),
        (2, 'LegacyImported', 0, None, None, None),
        (4, 'LegacyImported', 1, 'Legacy', None, None),
    ]
    attempt = connection.execute("""
        SELECT chapter_path_kind, finished_at, error_code
        FROM recorded_video_cm_analyses WHERE id = 10
    """).fetchone()
    assert attempt == ('Legacy', '2026-07-16 09:05:00', None)
    assert connection.execute('SELECT COUNT(*) FROM cm_logo_generation_batches').fetchone() == (1,)
    connection.close()


def test_cm_result_migration_backfills_terminal_failure_finished_at_without_result() -> None:
    """公開結果のない失敗試行には結果行を作らず、試行終了時刻だけ復元する。"""

    connection = sqlite3.connect(':memory:')
    connection.executescript("""
        CREATE TABLE recorded_videos (id INTEGER PRIMARY KEY, cm_sections JSON, updated_at TIMESTAMP NOT NULL);
        CREATE TABLE cm_logos (id INTEGER PRIMARY KEY);
        CREATE TABLE recorded_video_cm_analyses (
            id INTEGER PRIMARY KEY,
            recorded_video_id INTEGER NOT NULL UNIQUE,
            status VARCHAR(32) NOT NULL,
            chapter_source VARCHAR(32),
            input_fingerprint JSON,
            chapter_fingerprint JSON,
            analyzer_version VARCHAR(255),
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            error_code VARCHAR(255),
            updated_at TIMESTAMP NOT NULL,
            used_logo_id INTEGER
        );
        INSERT INTO recorded_videos VALUES (3, NULL, '2026-07-16 12:00:00');
        INSERT INTO recorded_video_cm_analyses VALUES (
            11, 3, 'Failed', NULL, '{"size":200}', '{"exists":false}',
            NULL, '2026-07-16 12:00:00', NULL, 'ProcessFailed',
            '2026-07-16 12:05:00', NULL
        );
    """)
    migration = importlib.import_module('app.migrations.models.21_20260717000000_update')
    connection.executescript(asyncio.run(migration.upgrade(None)))

    assert connection.execute('SELECT COUNT(*) FROM recorded_video_cm_results').fetchone() == (0,)
    assert connection.execute(
        'SELECT finished_at FROM recorded_video_cm_analyses WHERE id = 11',
    ).fetchone() == ('2026-07-16 12:05:00',)
    connection.close()
