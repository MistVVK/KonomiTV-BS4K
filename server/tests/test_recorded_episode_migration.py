import asyncio
import importlib
import sqlite3


def _CreateLegacyDatabase() -> sqlite3.Connection:
    """Episode導入直前に必要な最小テーブル構造を作成する。"""

    connection = sqlite3.connect(':memory:')
    connection.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE series (
            id INTEGER PRIMARY KEY
        );
        CREATE TABLE recorded_programs (
            id INTEGER PRIMARY KEY,
            series_id INT REFERENCES series (id) ON DELETE CASCADE,
            episode_number VARCHAR(255)
        );
        CREATE TABLE recorded_series_ai_requests (
            id INTEGER PRIMARY KEY
        );
        INSERT INTO series VALUES (1);
        INSERT INTO recorded_programs VALUES (10, 1, '#1');
        INSERT INTO recorded_programs VALUES (11, 1, NULL);
        INSERT INTO recorded_programs VALUES (12, NULL, NULL);
        INSERT INTO recorded_series_ai_requests VALUES (20);
    """)
    return connection


def test_episode_migration_seeds_all_existing_recordings_as_legacy() -> None:
    """後からSeriesへ所属する録画も自動課金しないよう、既存録画を恒久的なlegacy対象としてseedする。"""

    connection = _CreateLegacyDatabase()
    migration = importlib.import_module('app.migrations.models.24_20260722000000_update')
    connection.executescript(asyncio.run(migration.upgrade(None)))

    columns = {
        row[1]
        for row in connection.execute('PRAGMA table_info(recorded_programs)').fetchall()
    }
    assert 'series_episode_id' in columns
    rows = connection.execute("""
        SELECT recorded_program_id, status, source, is_legacy_recording
        FROM recorded_episode_resolutions ORDER BY recorded_program_id
    """).fetchall()
    assert rows == [
        (10, 'Pending', 'Migration', 1),
        (11, 'Pending', 'Migration', 1),
        (12, 'Pending', 'Migration', 1),
    ]

    # seed条件はNOT EXISTSなので、起動処理とは別に再評価されても重複しない。
    connection.execute("""
        INSERT INTO recorded_episode_resolutions (
            status, source, web_search_performed, is_legacy_recording, citations,
            recorded_program_id, created_at, updated_at
        )
        SELECT
            'Pending', 'Migration', 0, 1, '[]', rp.id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM recorded_programs rp
        WHERE NOT EXISTS (
              SELECT 1 FROM recorded_episode_resolutions rer
              WHERE rer.recorded_program_id = rp.id
          )
    """)
    assert connection.execute('SELECT COUNT(*) FROM recorded_episode_resolutions').fetchone() == (3,)
    connection.close()


def test_episode_migration_downgrade_removes_only_episode_structures() -> None:
    """downgrade後も既存Series・録画・AI監査テーブルとデータを保持する。"""

    connection = _CreateLegacyDatabase()
    migration = importlib.import_module('app.migrations.models.24_20260722000000_update')
    connection.executescript(asyncio.run(migration.upgrade(None)))
    connection.executescript(asyncio.run(migration.downgrade(None)))

    recorded_program_columns = {
        row[1]
        for row in connection.execute('PRAGMA table_info(recorded_programs)').fetchall()
    }
    ai_request_columns = {
        row[1]
        for row in connection.execute('PRAGMA table_info(recorded_series_ai_requests)').fetchall()
    }
    assert 'series_episode_id' not in recorded_program_columns
    assert 'episode_resolution_id' not in ai_request_columns
    assert connection.execute('SELECT COUNT(*) FROM recorded_programs').fetchone() == (3,)
    assert connection.execute('SELECT COUNT(*) FROM recorded_series_ai_requests').fetchone() == (1,)
    assert connection.execute("""
        SELECT COUNT(*) FROM sqlite_master
        WHERE type = 'table' AND name IN ('series_episodes', 'recorded_episode_resolutions')
    """).fetchone() == (0,)
    connection.close()
