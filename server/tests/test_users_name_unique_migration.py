"""users.name の UNIQUE 制約追加 migration の回帰テスト。"""

import asyncio
import sqlite3
from types import ModuleType
from typing import Any, cast

import pytest


class _FakeDBClient:
    """upgrade 関数へ渡すための execute_query_dict のみを持つ偽の DB クライアント。"""

    def __init__(self, duplicates: list[dict[str, Any]] | None = None) -> None:
        self.duplicates = duplicates or []

    async def execute_query_dict(self, query: str, values: list[Any] | None = None) -> list[dict[str, Any]]:
        del query, values
        return self.duplicates


def test_username_unique_migration_rejects_existing_duplicates() -> None:
    """既存 DB に重複 username が存在する場合、migration はエラーで停止する。"""

    async def Run() -> None:
        migration = cast(ModuleType, __import__('app.migrations.models.31_20260807000000_update', fromlist=['upgrade']))
        fake_db = _FakeDBClient(duplicates=[{'name': 'duplicate-user', 'count': 2}])

        # 重複がある場合は UNIQUE INDEX を作成せず、分かりやすいエラーで停止する
        with pytest.raises(RuntimeError, match='duplicate-user'):
            await migration.upgrade(fake_db)

    asyncio.run(Run())


def test_username_unique_migration_creates_unique_index_without_duplicates() -> None:
    """既存 DB に重複 username が存在しない場合、UNIQUE INDEX 作成 SQL を返す。"""

    async def Run() -> None:
        migration = cast(ModuleType, __import__('app.migrations.models.31_20260807000000_update', fromlist=['upgrade']))
        fake_db = _FakeDBClient(duplicates=[])

        # 重複がない場合は CREATE UNIQUE INDEX の SQL を返す
        migration_sql = await migration.upgrade(fake_db)
        assert 'CREATE UNIQUE INDEX' in migration_sql
        assert '"uid_users_name"' in migration_sql

        # 返された SQL が実際の SQLite 上で実行可能であることも確認する
        connection = sqlite3.connect(':memory:')
        connection.executescript("""
            CREATE TABLE "users" (
                "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                "name" TEXT NOT NULL,
                "password" TEXT NOT NULL,
                "is_admin" INT NOT NULL,
                "client_settings" JSON NOT NULL,
                "niconico_user_id" INT,
                "niconico_user_name" TEXT,
                "niconico_user_premium" INT,
                "niconico_access_token" TEXT,
                "niconico_refresh_token" TEXT,
                "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO "users" ("name", "password", "is_admin", "client_settings") VALUES ('user-1', 'hash', 1, '{}');
        """)
        connection.executescript(migration_sql)
        # 重複を挿入すると UNIQUE 制約違反になる
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('INSERT INTO "users" ("name", "password", "is_admin") VALUES (\'user-1\', \'hash\', 0)')
        connection.close()

    asyncio.run(Run())
