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


def _CreateEpisodeResolutionDatabase() -> sqlite3.Connection:
    """migration 26 直前の話数判定と監査データを作成する。"""

    connection = _CreateLegacyDatabase()
    episode_migration = importlib.import_module('app.migrations.models.24_20260722000000_update')
    connection.executescript(asyncio.run(episode_migration.upgrade(None)))
    connection.executescript("""
        INSERT INTO series_episodes (
            id, season_number, episode_number, series_id
        ) VALUES (
            100, 1, '12', 1
        );
        UPDATE recorded_programs
        SET series_episode_id = 100
        WHERE id = 10;
        UPDATE recorded_programs
        SET series_id = 1
        WHERE id = 12;
        INSERT INTO recorded_programs (id, series_id, episode_number)
        VALUES
            (13, 1, NULL),
            (14, 1, NULL),
            (15, 1, NULL),
            (16, 1, NULL),
            (17, 1, NULL),
            (18, 1, NULL),
            (19, 1, NULL),
            (20, 1, NULL),
            (21, 1, '#21'),
            (22, 1, NULL),
            (23, 1, NULL),
            (24, 1, NULL),
            (25, 1, NULL);

        UPDATE recorded_episode_resolutions
        SET
            status = 'Resolved',
            source = 'Manual',
            proposed_season_number = 1,
            proposed_episode_number = '12',
            confidence = 1.0,
            citations = '[{"url":"https://manual.example/kept","title":"manual"}]',
            ai_model = 'manual-model-kept',
            error_code = 'ManualOnlyCode',
            error_message = '手動行のメッセージ',
            episode_id = 100
        WHERE recorded_program_id = 10;
        UPDATE recorded_episode_resolutions
        SET
            status = 'Resolved',
            source = 'WebSearch',
            proposed_season_number = 1,
            proposed_episode_number = '13',
            confidence = 0.9,
            web_search_performed = 1,
            citations = '[{"url":"https://example.com/episode-13","title":"第13話"}]',
            ai_model = 'lookup-model',
            error_code = NULL,
            error_message = NULL
        WHERE recorded_program_id = 11;
        UPDATE recorded_episode_resolutions
        SET
            status = 'NotNumbered',
            source = 'WebSearch',
            web_search_performed = 1,
            error_code = NULL,
            error_message = NULL
        WHERE recorded_program_id = 12;

        INSERT INTO recorded_episode_resolutions (
            status, source, web_search_performed, is_legacy_recording, citations,
            error_code, error_message, recorded_program_id
        ) VALUES
            (
                'NeedsReview', 'WebSearch', 1, 1, '[]',
                'AcceptancePolicyRejected', NULL, 13
            ),
            (
                'NeedsReview', 'WebSearch', 0, 1, '[]',
                'MissingWebSearchCall', NULL, 14
            ),
            (
                'NeedsReview', 'WebSearch', 1, 1, '[]',
                'InvalidOutputSchema', NULL, 15
            ),
            (
                'Failed', 'WebSearch', 0, 1, '[]',
                'HTTP500', NULL, 16
            ),
            (
                'Failed', 'WebSearch', 0, 1, '[]',
                'HTTP429', NULL, 17
            ),
            (
                'Failed', 'WebSearch', 0, 1, '[]',
                'AIRequestInterrupted', NULL, 18
            ),
            (
                'Unknown', 'WebSearch', 0, 1, '[]',
                'AIEpisodeNumberSearchIsDisabled', NULL, 19
            ),
            (
                'Pending', 'WebSearch', 0, 1, '[]',
                NULL, NULL, 20
            ),
            (
                'NeedsReview', 'WebSearch', 0, 1, '[]',
                'InvalidOutputSchema', NULL, 22
            ),
            (
                'Resolved', 'Local', 0, 1, '[]',
                NULL, NULL, 21
            ),
            (
                'NeedsReview', NULL, 1, 1, '[]',
                'LowConfidence', NULL, 23
            ),
            (
                'Failed', NULL, 0, 1, '[]',
                'EpisodeLookupFailed', NULL, 24
            ),
            (
                'Resolved', 'Manual', 0, 1, '[]',
                'LowConfidence', NULL, 25
            );
        UPDATE recorded_series_ai_requests
        SET episode_resolution_id = (
            SELECT id
            FROM recorded_episode_resolutions
            WHERE recorded_program_id = 11
        )
        WHERE id = 20;
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


def test_episode_lookup_outcome_migration_preserves_existing_episode_data() -> None:
    """一意な旧状態だけを outcome へ移し、Manual・Episode・引用・AI監査を保持する。"""

    connection = _CreateEpisodeResolutionDatabase()
    migration = importlib.import_module('app.migrations.models.26_20260727000000_update')
    connection.executescript(asyncio.run(migration.upgrade(None)))

    columns = {
        row[1]
        for row in connection.execute('PRAGMA table_info(recorded_episode_resolutions)').fetchall()
    }
    assert {'lookup_outcome', 'rationale_short'} <= columns
    outcomes = connection.execute("""
        SELECT recorded_program_id, lookup_outcome
        FROM recorded_episode_resolutions
        ORDER BY recorded_program_id
    """).fetchall()
    assert outcomes == [
        (10, None),
        (11, 'Resolved'),
        (12, 'NotNumbered'),
        (13, 'InsufficientEvidence'),
        (14, 'SearchNotRun'),
        (15, 'InvalidModelOutput'),
        (16, 'SearchFailed'),
        (17, 'RateLimited'),
        (18, 'Cancelled'),
        (19, 'Disabled'),
        (20, 'Pending'),
        (21, None),
        (22, None),
        (23, None),
        (24, None),
        (25, None),
    ]

    manual_row = connection.execute("""
        SELECT
            status, source, proposed_season_number, proposed_episode_number,
            confidence, citations, ai_model, error_code, error_message, episode_id
        FROM recorded_episode_resolutions
        WHERE recorded_program_id = 10
    """).fetchone()
    assert manual_row == (
        'Resolved',
        'Manual',
        1,
        '12',
        1.0,
        '[{"url":"https://manual.example/kept","title":"manual"}]',
        'manual-model-kept',
        'ManualOnlyCode',
        '手動行のメッセージ',
        100,
    )
    web_search_row = connection.execute("""
        SELECT citations, ai_model
        FROM recorded_episode_resolutions
        WHERE recorded_program_id = 11
    """).fetchone()
    assert web_search_row == (
        '[{"url":"https://example.com/episode-13","title":"第13話"}]',
        'lookup-model',
    )
    assert connection.execute("""
        SELECT series_episode_id
        FROM recorded_programs
        WHERE id = 10
    """).fetchone() == (100,)
    assert connection.execute("""
        SELECT episode_resolution_id
        FROM recorded_series_ai_requests
        WHERE id = 20
    """).fetchone() == (
        connection.execute("""
            SELECT id
            FROM recorded_episode_resolutions
            WHERE recorded_program_id = 11
        """).fetchone()[0],
    )
    safe_messages = dict(connection.execute("""
        SELECT error_code, error_message
        FROM recorded_episode_resolutions
        WHERE recorded_program_id IN (13, 14, 15, 17, 18, 19)
    """).fetchall())
    assert safe_messages == {
        'AcceptancePolicyRejected': '検索結果の根拠または信頼度が受理条件を満たしませんでした。',
        'MissingWebSearchCall': 'AI が Web 検索を実行しませんでした。',
        'InvalidOutputSchema': 'AI の応答が話数判定の形式と一致しません。',
        'HTTP429': 'AI プロバイダーの利用上限に達しました。',
        'AIRequestInterrupted': '前回の話数検索はサーバー停止により中断されました。',
        'AIEpisodeNumberSearchIsDisabled': '話数 Web 検索が無効です。',
    }
    source_null_messages = dict(connection.execute("""
        SELECT error_code, error_message
        FROM recorded_episode_resolutions
        WHERE recorded_program_id IN (23, 24)
    """).fetchall())
    assert source_null_messages == {
        'LowConfidence': 'Web 検索結果の信頼度が受理条件を満たしませんでした。',
        'EpisodeLookupFailed': '話数 Web 検索に失敗しました。',
    }
    assert connection.execute("""
        SELECT source, error_code, error_message
        FROM recorded_episode_resolutions
        WHERE recorded_program_id = 25
    """).fetchone() == ('Manual', 'LowConfidence', None)
    connection.close()


def test_episode_lookup_outcome_downgrade_removes_only_new_columns() -> None:
    """migration 26 downgrade後も既存話数・引用・Episode・AI監査を保持する。"""

    connection = _CreateEpisodeResolutionDatabase()
    migration = importlib.import_module('app.migrations.models.26_20260727000000_update')
    connection.executescript(asyncio.run(migration.upgrade(None)))
    resolution_id = connection.execute("""
        SELECT id
        FROM recorded_episode_resolutions
        WHERE recorded_program_id = 11
    """).fetchone()[0]
    connection.executescript(asyncio.run(migration.downgrade(None)))

    columns = {
        row[1]
        for row in connection.execute('PRAGMA table_info(recorded_episode_resolutions)').fetchall()
    }
    assert 'lookup_outcome' not in columns
    assert 'rationale_short' not in columns
    assert connection.execute("""
        SELECT status, source, citations, ai_model
        FROM recorded_episode_resolutions
        WHERE recorded_program_id = 11
    """).fetchone() == (
        'Resolved',
        'WebSearch',
        '[{"url":"https://example.com/episode-13","title":"第13話"}]',
        'lookup-model',
    )
    assert connection.execute("""
        SELECT series_episode_id
        FROM recorded_programs
        WHERE id = 10
    """).fetchone() == (100,)
    assert connection.execute("""
        SELECT episode_resolution_id
        FROM recorded_series_ai_requests
        WHERE id = 20
    """).fetchone() == (resolution_id,)
    connection.close()
