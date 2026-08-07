from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    """users テーブルのユーザー名に UNIQUE 制約を追加する。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        users.name に UNIQUE INDEX を作成する SQLite SQL。
    """

    # 既存データに重複したユーザー名が存在しないかを確認する
    ## 重複がある状態で UNIQUE INDEX を作成しようとすると失敗するため、migration 前に検出して分かりやすいエラーで停止する
    duplicates = await db.execute_query_dict(
        'SELECT "name", COUNT(*) AS "count" FROM "users" GROUP BY "name" HAVING COUNT(*) > 1'
    )
    if duplicates:
        names = ', '.join(repr(row['name']) for row in duplicates)
        raise RuntimeError(f'Duplicate usernames found in users table: {names}')

    return 'CREATE UNIQUE INDEX IF NOT EXISTS "uid_users_name" ON "users" ("name");'


async def downgrade(db: BaseDBAsyncClient) -> str:
    """ユーザー名の UNIQUE 制約を削除する。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        UNIQUE INDEX を削除する SQLite SQL。
    """

    del db
    return 'DROP INDEX IF EXISTS "uid_users_name";'
