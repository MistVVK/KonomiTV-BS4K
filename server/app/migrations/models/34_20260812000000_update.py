from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    """録画シリーズ AI 監査へ全試行サマリを追加する。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        秘密を含まない試行列を保存する SQLite SQL。
    """

    del db
    return """
        ALTER TABLE "recorded_series_ai_requests"
            ADD COLUMN "attempt_summaries" JSON NOT NULL DEFAULT '[]';
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    """録画シリーズ AI 監査の試行サマリ列を除去する。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        追加列だけを除去する SQLite SQL。
    """

    del db
    return 'ALTER TABLE "recorded_series_ai_requests" DROP COLUMN "attempt_summaries";'
