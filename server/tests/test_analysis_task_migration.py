import asyncio
import importlib
import sqlite3
from types import ModuleType
from typing import Any, cast


def test_analysis_task_migration_creates_history_and_imports_logo_batches() -> None:
    """履歴テーブル作成SQLがSQLiteで実行でき、既存ロゴ生成履歴を引き継ぐ。"""

    connection = sqlite3.connect(':memory:')
    connection.executescript("""
        CREATE TABLE recorded_programs (id INTEGER PRIMARY KEY, title TEXT NOT NULL);
        CREATE TABLE recorded_videos (
            id INTEGER PRIMARY KEY,
            recorded_program_id INTEGER NOT NULL REFERENCES recorded_programs (id)
        );
        CREATE TABLE cm_logo_generation_batches (
            id INTEGER PRIMARY KEY,
            recorded_video_id INTEGER NOT NULL REFERENCES recorded_videos (id),
            state VARCHAR(32) NOT NULL,
            explicit INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL,
            completed_at TIMESTAMP
        );
        INSERT INTO recorded_programs VALUES (1, 'サンプル番組');
        INSERT INTO recorded_videos VALUES (10, 1);
        INSERT INTO cm_logo_generation_batches VALUES (
            20, 10, 'Exhausted', 1, '2026-07-16 10:00:00', '2026-07-16 10:05:00'
        );
    """)
    migration = cast(ModuleType, importlib.import_module('app.migrations.models.20_20260716000000_update'))
    migration_sql = asyncio.run(cast(Any, migration.upgrade)(None))
    connection.executescript(migration_sql)

    row = connection.execute("""
        SELECT task_type, status, trigger, title, recorded_video_id, cm_logo_generation_batch_id
        FROM analysis_task_executions
    """).fetchone()
    assert row == ('CMLogoGeneration', 'Failed', 'Manual', 'サンプル番組', 10, 20)
    connection.close()
