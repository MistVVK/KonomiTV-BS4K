"""話数判定の内部エラーを安全な日本語表示へ変換する。"""

from __future__ import annotations

from app.models.RecordedEpisode import (
    RecordedEpisodeResolutionStatus,
    RecordedEpisodeSource,
)


# ACP 実行のサーバー側最終安全上限（秒）。acp_client の hard timeout と同一値。
# 無通信タイムアウト (acp_timeout_sec) とは独立し、Semaphore 待ち時間も含む。
ACP_HARD_TIMEOUT_SEC = 60 * 60


def FormatAcpHardTimeoutMessage(*, subject: str = 'AI プロバイダー') -> str:
    """HardTimeout の利用者向け文言を上限秒数から導出する。

    Args:
        subject: 文頭の主語（例: AI プロバイダー / ACP / AI バックエンド）。

    Returns:
        キュー待ちを含む総実行上限の説明文。
    """

    minutes = ACP_HARD_TIMEOUT_SEC // 60
    return (
        f'{subject}の総実行時間（他の ACP 実行待ちを含む）が'
        f'安全上限の {minutes} 分を超えたため停止しました。'
    )


_ERROR_MESSAGES: dict[str, str] = {
    'RecordedSeriesIsDisabled': '録画シリーズ判定機能が無効です。',
    'AIIsDisabled': 'AI 判定機能が無効です。',
    'AIEpisodeNumberSearchIsDisabled': '話数 Web 検索が無効です。',
    'AISettingsChangedBeforeRequest': '検索開始前に AI バックエンド設定が変更されたため、処理を中止しました。',
    # 接続試験を必須としていた旧バージョンの保存済みデータとの互換表示用。
    'EpisodeLookupCapabilityNotVerified': '選択中の AI バックエンドでは話数 Web 検索の接続試験が完了していません。AI バックエンド設定で接続試験を実行してください。',
    # 旧日次制限コード（互換表示用。新規発生はしない）
    'DailyAIRequestLimitReached': '本日の AI 利用上限に達しました。',
    'MonthlyTokenLimitReached': '今月の AI トークン利用上限に達しました。',
    'MonthlyCostLimitReached': '今月の AI 推定料金上限に達しました。',
    'ProviderRateLimited': 'AI プロバイダーの利用上限に達しました。',
    'LegacyEpisodeMissing': '既存録画に話数情報がありません。',
    'LegacyEpisodeUnknown': '既存の話数表記を構造化できませんでした。',
    'LegacyRequiresManualBackfill': '既存録画は明示的な再検索が必要です。',
    'AcceptancePolicyRejected': '検索結果の根拠または信頼度が受理条件を満たしませんでした。',
    'LowConfidence': 'Web 検索結果の信頼度が受理条件を満たしませんでした。',
    'ManualAssignmentWonRace': '検索中に手動の話数判定が行われたため、検索結果を反映しませんでした。',
    'InsufficientEvidence': 'Web 検索は完了しましたが、確定できる根拠が不足しています。',
    'EpisodeLookupFailed': '話数 Web 検索に失敗しました。',
    'MissingWebSearchCall': 'AI が Web 検索を実行しませんでした。',
    'SearchNotRun': 'AI が Web 検索を実行しませんでした。',
    'InvalidJSON': 'AI の応答が正しい JSON ではありません。',
    'InvalidJSONType': 'AI の応答形式が正しくありません。',
    'InvalidOutputSchema': 'AI の応答が話数判定の形式と一致しません。',
    'InvalidModelOutput': 'AI の応答内容に矛盾があります。',
    'InvalidResponse': 'AI プロバイダーの応答形式が正しくありません。',
    'InvalidResponseJSON': 'AI プロバイダーの応答を読み取れませんでした。',
    'IncompleteResponse': 'AI プロバイダーの応答が完了しませんでした。',
    'MissingOutput': 'AI プロバイダーから判定結果が返りませんでした。',
    'MissingOutputText': 'AI プロバイダーから判定本文が返りませんでした。',
    'MultipleOutputTexts': 'AI プロバイダーから複数の判定本文が返りました。',
    'NetworkError': 'AI プロバイダーとの通信に失敗しました。',
    'Timeout': 'AI プロバイダーからの応答が一定時間途絶えたためタイムアウトしました。',
    'HardTimeout': FormatAcpHardTimeoutMessage(subject='AI プロバイダー'),
    'RedirectRejected': '安全のため AI プロバイダーのリダイレクトを拒否しました。',
    'InvalidURL': 'AI プロバイダーの接続先 URL が正しくありません。',
    'ACPAuthenticationFailed': 'ACP agent の認証が無効または期限切れです。設定画面から認証を再取り込みしてください。',
    'ACPProtocolError': 'ACP agent との通信手順を検証できませんでした。',
    'ACPUnsafeToolRequested': 'ACP agent が許可されていない操作を要求しました。',
    'ACPWebSearchNotObserved': 'ACP agent の Web 検索実行を確認できませんでした。',
    'ACPSourceURLMissing': 'ACP agent の検索元 URL を確認できませんでした。',
    'AcpEpisodeLookupUnsupported': '選択した ACP agent は話数 Web 検索能力を確認できません。',
    'HostCLINotFound': 'ACP コマンドが見つかりません。',
    'HostCLIPermissionDenied': 'ACP コマンドを実行する権限がありません。',
    'HostCLIStartFailed': 'ACP コマンドを安全に起動できませんでした。',
    'AIRequestInterrupted': '前回の話数検索はサーバー停止により中断されました。',
    'Cancelled': '話数検索が中断されました。再検索できます。',
    'InputChangedBeforeRequest': '番組情報が更新されたため、検索を開始せず再判定します。',
    'InputChangedAfterRequest': '検索中に番組情報が更新されたため、結果を反映しませんでした。',
}

_STATUS_FALLBACKS: dict[RecordedEpisodeResolutionStatus, str] = {
    "Pending": "話数判定の処理待ちです。",
    "Resolved": "話数を確定しました。",
    "Unknown": "ローカル情報だけでは話数を確定できません。",
    "NotNumbered": "話数番号を使わない番組として判定しました。",
    'NoPublishedNumber': 'この録画には公開された話数番号がないと判定しました。',
    "NeedsReview": "自動判定だけでは確定できないため確認が必要です。",
    "Failed": "話数判定処理に失敗しました。",
}

_SOURCE_FALLBACKS: dict[RecordedEpisodeSource, str] = {
    "Local": "録画ファイル内の情報から判定しました。",
    "EPG": "EPG の番組情報から判定しました。",
    "WebSearch": "AI の Web 検索結果から判定しました。",
    "Manual": "管理者が手動で確定しました。",
    "Migration": "既存の話数情報を移行しました。",
    "AI": "AI の一括生成結果から判定しました。",
}


def GetRecordedEpisodeErrorMessage(
    error_code: str | None,
) -> str | None:
    """内部エラーコードに対応する安全な日本語メッセージを返す。

    DB に残った未知コードの ``error_message`` は、生例外・パス・資格情報を
    含む可能性があるため API へ転送しない。未知コード自体は別フィールドで
    小さく表示し、本文は固定メッセージへ閉じる。

    Args:
        error_code: 永続化された固定エラーコード。

    Returns:
        既知コードの日本語メッセージ。コードがない場合は None。
    """

    if error_code is None:
        return None
    if error_code == "HTTP429":
        return _ERROR_MESSAGES["ProviderRateLimited"]
    if error_code.startswith("HTTP") and error_code[4:].isdigit():
        return "AI プロバイダーが通信エラーを返しました。"
    return _ERROR_MESSAGES.get(error_code, "話数判定の詳細を安全に表示できません。")


def GetRecordedEpisodeReason(
    *,
    status: RecordedEpisodeResolutionStatus,
    source: RecordedEpisodeSource | None,
    error_code: str | None,
    rationale_short: str | None,
) -> str:
    """管理 UI の1行理由として使える安全な表示文を決定する。

    Args:
        status: 録画全体の話数解決状態。
        source: 最後に確定値を決めた判定元。
        error_code: 最後の失敗を表す固定コード。
        rationale_short: 検証済みモデル結果の短い根拠。

    Returns:
        根拠、既知エラー、status/source fallback の順で選んだ日本語表示。
    """

    if rationale_short is not None and rationale_short.strip() != "":
        return rationale_short.strip()
    error_message = GetRecordedEpisodeErrorMessage(error_code)
    if error_message is not None:
        return error_message
    if status in {'Resolved', 'NotNumbered', 'NoPublishedNumber'} and source is not None:
        return _SOURCE_FALLBACKS[source]
    return _STATUS_FALLBACKS[status]
