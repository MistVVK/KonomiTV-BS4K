import asyncio
import importlib
import sqlite3


def test_logo_format_migration_keeps_references_and_allows_unassigned_sid() -> None:
    """cm_logos再構築後も参照整合性を保ち、標準ロゴのNULL SIDを保存できる。"""

    connection = sqlite3.connect(':memory:')
    connection.executescript(
        """
        PRAGMA foreign_keys=on;
        CREATE TABLE recorded_videos (id INTEGER PRIMARY KEY);
        CREATE TABLE cm_logos (
            id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            path TEXT NOT NULL,
            filename TEXT NOT NULL,
            service_id INT NOT NULL,
            logo_name TEXT NOT NULL,
            enabled INT NOT NULL DEFAULT 1,
            file_hash VARCHAR(64) NOT NULL,
            file_size BIGINT NOT NULL,
            last_used_at TIMESTAMP,
            missing INT NOT NULL DEFAULT 0,
            deleted_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            generated_from_recorded_video_id INT REFERENCES recorded_videos (id) ON DELETE SET NULL
        );
        CREATE INDEX idx_cm_logos_service_id ON cm_logos (service_id);
        CREATE UNIQUE INDEX uid_cm_logos_path ON cm_logos (path);
        CREATE TABLE cm_logo_service_assignments (
            id INTEGER PRIMARY KEY,
            service_id INT NOT NULL,
            logo_id INT REFERENCES cm_logos (id) ON DELETE SET NULL
        );
        INSERT INTO cm_logos (path, filename, service_id, logo_name, file_hash, file_size)
        VALUES ('existing.lgd', 'existing.lgd', 101, 'existing', 'hash', 1);
        INSERT INTO cm_logo_service_assignments (id, service_id, logo_id) VALUES (1, 101, 1);
        """,
    )
    migration = importlib.import_module('app.migrations.models.22_20260717010000_update')

    connection.executescript(asyncio.run(migration.upgrade(None)))

    columns = {row[1]: row for row in connection.execute('PRAGMA table_info(cm_logos)')}
    assert columns['service_id'][3] == 0
    assert columns['file_format'][3] == 1
    assert connection.execute('SELECT file_format FROM cm_logos WHERE id = 1').fetchone() == (
        'AmatsukazeExtendedV1',
    )
    connection.execute(
        """
        INSERT INTO cm_logos (path, filename, service_id, logo_name, file_format, file_hash, file_size)
        VALUES ('standard.lgd', 'standard.lgd', NULL, 'standard', 'AviUtlV0.1', 'standard-hash', 2)
        """,
    )
    assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
    assert connection.execute('SELECT logo_id FROM cm_logo_service_assignments WHERE id = 1').fetchone() == (1,)
