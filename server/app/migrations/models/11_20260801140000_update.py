from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    """ニコニコ OAuth 用の短命 state テーブルを追加する。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        niconico_oauth_states を作成する SQLite SQL。
    """

    del db
    return """
        CREATE TABLE IF NOT EXISTS "niconico_oauth_states" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "state_hash" VARCHAR(64) NOT NULL UNIQUE,
            "code_verifier" TEXT,
            "client_url" TEXT NOT NULL,
            "expires_at" TIMESTAMP NOT NULL,
            "consumed_at" TIMESTAMP,
            "issued_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "user_id" INT NOT NULL UNIQUE REFERENCES "users" ("id") ON DELETE CASCADE
        );
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    """niconico_oauth_states テーブルを削除する。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        DROP TABLE SQL。
    """

    del db
    return """
        DROP TABLE IF EXISTS "niconico_oauth_states";
    """
