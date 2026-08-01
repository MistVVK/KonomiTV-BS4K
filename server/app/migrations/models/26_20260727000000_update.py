from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    """話数 Web 検索の排他的 outcome と短い判定根拠を追加する。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        既存データを破壊せず追加列だけを導入する SQLite SQL。
    """

    del db
    return """
        ALTER TABLE "recorded_episode_resolutions"
            ADD COLUMN "lookup_outcome" VARCHAR(32);
        ALTER TABLE "recorded_episode_resolutions"
            ADD COLUMN "rationale_short" TEXT;

        -- Manual と検索未実行の決定論的結果は触らない。一意に判定できる
        -- 既存 WebSearch 行だけを最後の lookup outcome へ移行する。
        UPDATE "recorded_episode_resolutions"
        SET "lookup_outcome" = CASE
            WHEN "status" = 'Pending' THEN 'Pending'
            WHEN "status" = 'Resolved'
                 AND "web_search_performed" = 1
                 AND "error_code" IS NULL THEN 'Resolved'
            WHEN "status" = 'NotNumbered'
                 AND "web_search_performed" = 1
                 AND "error_code" IS NULL THEN 'NotNumbered'
            WHEN "status" = 'NeedsReview'
                 AND "web_search_performed" = 1
                 AND "error_code" IN ('AcceptancePolicyRejected', 'LowConfidence')
                THEN 'InsufficientEvidence'
            WHEN "status" = 'NeedsReview'
                 AND "web_search_performed" = 0
                 AND "error_code" = 'MissingWebSearchCall'
                THEN 'SearchNotRun'
            WHEN "status" = 'NeedsReview'
                 AND "web_search_performed" = 1
                 AND "error_code" IN (
                    'InvalidJSON',
                    'InvalidJSONType',
                    'InvalidOutputSchema',
                    'InvalidModelOutput'
                 )
                THEN 'InvalidModelOutput'
            WHEN "error_code" IN (
                    'DailyAIRequestLimitReached',
                    'ProviderRateLimited',
                    'HTTP429'
                 )
                THEN 'RateLimited'
            WHEN "error_code" IN ('AIRequestInterrupted', 'Cancelled')
                THEN 'Cancelled'
            WHEN "status" = 'Failed' THEN 'SearchFailed'
            WHEN "status" = 'Unknown'
                 AND "web_search_performed" = 0
                 AND "error_code" IN (
                    'RecordedSeriesIsDisabled',
                    'AIIsDisabled',
                    'AIEpisodeNumberSearchIsDisabled'
                 )
                THEN 'Disabled'
            ELSE NULL
        END
        WHERE "source" = 'WebSearch';

        -- 既知コードだけを安全な日本語へ決定論的に補完する。
        -- 未知コードの生例外は移行時にも推測・展開しない。
        UPDATE "recorded_episode_resolutions"
        SET "error_message" = CASE "error_code"
            WHEN 'RecordedSeriesIsDisabled' THEN '録画シリーズ判定機能が無効です。'
            WHEN 'AIIsDisabled' THEN 'AI 判定機能が無効です。'
            WHEN 'AIEpisodeNumberSearchIsDisabled' THEN '話数 Web 検索が無効です。'
            WHEN 'DailyAIRequestLimitReached' THEN '本日の AI 利用上限に達しました。'
            WHEN 'ProviderRateLimited' THEN 'AI プロバイダーの利用上限に達しました。'
            WHEN 'HTTP429' THEN 'AI プロバイダーの利用上限に達しました。'
            WHEN 'AcceptancePolicyRejected'
                THEN '検索結果の根拠または信頼度が受理条件を満たしませんでした。'
            WHEN 'LowConfidence'
                THEN 'Web 検索結果の信頼度が受理条件を満たしませんでした。'
            WHEN 'EpisodeLookupFailed' THEN '話数 Web 検索に失敗しました。'
            WHEN 'MissingWebSearchCall' THEN 'AI が Web 検索を実行しませんでした。'
            WHEN 'InvalidJSON' THEN 'AI の応答が正しい JSON ではありません。'
            WHEN 'InvalidJSONType' THEN 'AI の応答形式が正しくありません。'
            WHEN 'InvalidOutputSchema' THEN 'AI の応答が話数判定の形式と一致しません。'
            WHEN 'NetworkError' THEN 'AI プロバイダーとの通信に失敗しました。'
            WHEN 'Timeout' THEN 'AI プロバイダーとの通信がタイムアウトしました。'
            WHEN 'AIRequestInterrupted'
                THEN '前回の話数検索はサーバー停止により中断されました。'
            ELSE "error_message"
        END
        WHERE ("source" IS NULL OR "source" != 'Manual')
          AND "error_message" IS NULL
          AND "error_code" IS NOT NULL;
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    """追加した outcome と短い判定根拠だけを除去する。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        既存の話数・Episode・引用・AI 監査を維持する SQLite SQL。
    """

    del db
    return """
        ALTER TABLE "recorded_episode_resolutions" DROP COLUMN "rationale_short";
        ALTER TABLE "recorded_episode_resolutions" DROP COLUMN "lookup_outcome";
    """
