from tortoise import BaseDBAsyncClient


RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    """Twitter / Bluesky 連携テーブルを削除する。

    Args:
        db: migrationの接続。
    Returns:
        3テーブルのDROP DDL。
    """
    # account_links が twitter / bluesky 両方へ FK を張っているため、子テーブルから先に削除する
    return """
        DROP TABLE IF EXISTS "account_links";
        DROP TABLE IF EXISTS "bluesky_accounts";
        DROP TABLE IF EXISTS "twitter_accounts";
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    """削除した Twitter / Bluesky 連携テーブルを再作成する。

    Args:
        db: migrationの接続。
    Returns:
        3テーブルのCREATE DDL。
    """
    # 各テーブルのカラム構成は drop 直前の状態 (0_ の twitter_accounts 作成 + 8_ の bluesky /
    # account_links 作成 + 9_ の cookie_browser_info 追加) と一致させる。
    ## 既存連携データが消えることは設計時点で受け済みなので、 downgrade はスキーマの復元のみを行う
    return """
        CREATE TABLE IF NOT EXISTS "twitter_accounts" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
            "name" TEXT NOT NULL,
            "screen_name" TEXT NOT NULL,
            "icon_url" TEXT NOT NULL,
            "access_token" TEXT NOT NULL,
            "access_token_secret" TEXT NOT NULL,
            "cookie_browser_info" JSON,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS "bluesky_accounts" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
            "did" TEXT NOT NULL,
            "handle" TEXT NOT NULL,
            "name" TEXT NOT NULL,
            "icon_url" TEXT NOT NULL,
            "session_string" TEXT NOT NULL,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT "uid_bluesky_ac_user_id_did" UNIQUE ("user_id", "did")
        );
        CREATE TABLE IF NOT EXISTS "account_links" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
            "twitter_account_id" INT NOT NULL UNIQUE REFERENCES "twitter_accounts" ("id") ON DELETE CASCADE,
            "bluesky_account_id" INT NOT NULL UNIQUE REFERENCES "bluesky_accounts" ("id") ON DELETE CASCADE,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """
