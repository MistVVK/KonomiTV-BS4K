from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType


def _LoadMigration() -> ModuleType:
    """
    CMホストパス正規化マイグレーションをファイルからロードする。

    Returns:
        ModuleType: ロード済みマイグレーションモジュール。
    """

    migration_path = (
        Path(__file__).parents[1]
        / 'app/migrations/models/25_20260725000000_update.py'
    )
    spec = importlib.util.spec_from_file_location('cm_host_path_migration', migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cm_host_path_migration_normalizes_paths_errors_and_duplicate_logos() -> None:
    """CMのDBパスとエラーをホスト表現へ直し、重複ロゴの参照を維持する。"""

    connection = sqlite3.connect(':memory:')
    connection.executescript(
        """
        CREATE TABLE cm_analysis_settings (
            id INTEGER PRIMARY KEY,
            logo_directory TEXT
        );
        CREATE TABLE cm_analysis_excluded_directories (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            resolved_path TEXT NOT NULL
        );
        CREATE TABLE cm_logos (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE
        );
        CREATE TABLE cm_logo_service_assignments (
            id INTEGER PRIMARY KEY,
            logo_id INTEGER
        );
        CREATE TABLE recorded_video_cm_analyses (
            id INTEGER PRIMARY KEY,
            matched_exclusion_path TEXT,
            error_message TEXT,
            used_logo_id INTEGER
        );
        CREATE TABLE recorded_video_cm_results (
            id INTEGER PRIMARY KEY,
            used_logo_id INTEGER
        );
        CREATE TABLE analysis_task_executions (
            id INTEGER PRIMARY KEY,
            error_message TEXT
        );
        CREATE TABLE cm_logo_generation_attempts (
            id INTEGER PRIMARY KEY,
            failure_reason TEXT
        );

        INSERT INTO cm_analysis_settings VALUES
            (1, '/host-rootfs/host-rootfs/mnt/CM-Logos');
        INSERT INTO cm_analysis_excluded_directories VALUES
            (1, '/host-rootfs/mnt/TV-Record/Temp', '/host-rootfs/host-rootfs/mnt/TV-Record/Temp'),
            (2, '/mnt/host-rootfs/archive', '/mnt/host-rootfs/archive');
        INSERT INTO cm_logos VALUES
            (1, '/mnt/CM-Logos/logo.lgd'),
            (2, '/host-rootfs/mnt/CM-Logos/logo.lgd'),
            (3, '/host-rootfs/host-rootfs/mnt/CM-Logos/logo.lgd');
        INSERT INTO cm_logo_service_assignments VALUES (1, 3);
        INSERT INTO recorded_video_cm_analyses VALUES (
            1,
            '/host-rootfs/host-rootfs/mnt/TV-Record/Temp',
            'Failure: /host-rootfs/host-rootfs/mnt/TV-Record/input.ts; kept /mnt/host-rootfs/archive',
            2
        );
        INSERT INTO recorded_video_cm_results VALUES (1, 3);
        INSERT INTO analysis_task_executions VALUES (
            1,
            'Failed at "/host-rootfs/mnt/TV-Record/input.ts"'
        );
        INSERT INTO cm_logo_generation_attempts VALUES (
            1,
            'Input=/host-rootfs/mnt/TV-Record/input.ts'
        );
        """
    )

    migration = _LoadMigration()
    migration_sql = asyncio.run(migration.upgrade(None))
    connection.executescript(migration_sql)

    assert connection.execute(
        'SELECT logo_directory FROM cm_analysis_settings WHERE id = 1',
    ).fetchone() == ('/mnt/CM-Logos',)
    assert connection.execute(
        'SELECT path, resolved_path FROM cm_analysis_excluded_directories ORDER BY id',
    ).fetchall() == [
        ('/mnt/TV-Record/Temp', '/mnt/TV-Record/Temp'),
        ('/mnt/host-rootfs/archive', '/mnt/host-rootfs/archive'),
    ]
    assert connection.execute('SELECT id, path FROM cm_logos').fetchall() == [
        (1, '/mnt/CM-Logos/logo.lgd'),
    ]
    assert connection.execute('SELECT logo_id FROM cm_logo_service_assignments').fetchone() == (1,)
    assert connection.execute(
        'SELECT matched_exclusion_path, error_message, used_logo_id FROM recorded_video_cm_analyses',
    ).fetchone() == (
        '/mnt/TV-Record/Temp',
        'Failure: /mnt/TV-Record/input.ts; kept /mnt/host-rootfs/archive',
        1,
    )
    assert connection.execute('SELECT used_logo_id FROM recorded_video_cm_results').fetchone() == (1,)
    assert connection.execute('SELECT error_message FROM analysis_task_executions').fetchone() == (
        'Failed at "/mnt/TV-Record/input.ts"',
    )
    assert connection.execute('SELECT failure_reason FROM cm_logo_generation_attempts').fetchone() == (
        'Input=/mnt/TV-Record/input.ts',
    )
