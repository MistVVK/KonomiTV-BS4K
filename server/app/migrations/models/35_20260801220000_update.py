from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    del db
    return """
        ALTER TABLE "users" ADD COLUMN "token_version" INT NOT NULL DEFAULT 0;
        CREATE TABLE "refresh_tokens" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "token_hash" VARCHAR(64) NOT NULL UNIQUE,
            "family_id" VARCHAR(64) NOT NULL,
            "expires_at" TIMESTAMP NOT NULL,
            "revoked_at" TIMESTAMP,
            "replaced_by_hash" VARCHAR(64),
            "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
        );
        CREATE INDEX "refresh_tokens_family_id" ON "refresh_tokens" ("family_id");
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    del db
    return """
        DROP TABLE IF EXISTS "refresh_tokens";
        ALTER TABLE "users" DROP COLUMN "token_version";
    """
