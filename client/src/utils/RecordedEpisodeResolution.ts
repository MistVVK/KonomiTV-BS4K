import type {
    EpisodeLookupOutcome,
    IRecordedEpisodeAssignmentResolution,
} from '@/services/RecordedSeries';


type RecordedEpisodeResolutionStatus = IRecordedEpisodeAssignmentResolution['status'];
type RecordedEpisodeResolutionSource = NonNullable<IRecordedEpisodeAssignmentResolution['source']>;

export interface IRecordedEpisodeResolutionProposal {
    seasonNumber: number;
    episodeNumber: string;
}

const EPISODE_LOOKUP_OUTCOME_LABELS: Record<EpisodeLookupOutcome, string> = {
    Pending: 'Web 検索中',
    Resolved: '検索根拠から確定',
    NotNumbered: '検索根拠から公式話数なし',
    InsufficientEvidence: '検索済み・根拠不足',
    SearchFailed: '検索失敗',
    SearchNotRun: 'Web 検索未実行',
    InvalidModelOutput: 'AI 応答不正',
    Disabled: '検索無効',
    RateLimited: '利用上限',
    Cancelled: '検索中断',
};

const EPISODE_RESOLUTION_STATUS_LABELS: Record<RecordedEpisodeResolutionStatus, string> = {
    Pending: '判定待ち',
    Resolved: '確定',
    Unknown: '話数不明',
    NotNumbered: '公式話数なし',
    NeedsReview: '要確認',
    Failed: '判定失敗',
};

const EPISODE_RESOLUTION_SOURCE_LABELS: Record<RecordedEpisodeResolutionSource, string> = {
    Local: 'ローカル情報',
    EPG: 'EPG',
    WebSearch: 'AI Web 検索',
    Manual: '管理者の手動訂正',
    Migration: '既存データ移行',
    AI: 'AI 一括生成',
};

/**
 * server/app/metadata/RecordedEpisodeMessages.py の ACP_HARD_TIMEOUT_SEC と揃える。
 * credential lock / Semaphore 待ちを含む ACP 絶対実行上限（秒）。
 */
export const ACP_HARD_TIMEOUT_SEC = 60 * 60;
/** 接続試験 HTTP がサーバー hard stop 応答より先に切れないよう足す余裕（秒）。 */
export const ACP_CONNECTION_TEST_CLIENT_EXTRA_SEC = 10 * 60;

/** HardTimeout の利用者向け文言。上限分は ACP_HARD_TIMEOUT_SEC から導出する。 */
export function formatAcpHardTimeoutMessage(subject: string): string {
    const minutes = Math.floor(ACP_HARD_TIMEOUT_SEC / 60);
    return (
        `${subject}の総実行時間（他の ACP 実行待ちを含む）が` +
        `安全上限の ${minutes} 分を超えたため停止しました。`
    );
}

/** 旧データに error_message がない場合だけ使う、安全な決定論的表示文。 */
const RECORDED_EPISODE_ERROR_MESSAGES: Record<string, string> = {
    RecordedSeriesIsDisabled: '録画シリーズの自動判定が無効です。',
    AIIsDisabled: 'AI API の利用が無効です。',
    AIEpisodeNumberSearchIsDisabled: '話数 Web 検索が無効です。',
    AISettingsChangedBeforeRequest: '検索開始前に AI バックエンド設定が変更されたため、処理を中止しました。',
    // 接続試験を必須としていた旧実装の永続データに対する互換表示用。
    EpisodeLookupCapabilityNotVerified: 'この話数検索は、旧バージョンで接続試験の確認前に中止されました。再検索できます。',
    AcpEpisodeLookupUnsupported: '選択中の ACP バックエンドでは話数 Web 検索を検証できません。',
    // 旧日次制限コード（互換表示用。新規発生はしない）
    DailyAIRequestLimitReached: '本日の AI API 利用上限に達しました。',
    MonthlyTokenLimitReached: '今月の AI トークン利用上限に達しました。',
    MonthlyCostLimitReached: '今月の AI 推定料金上限に達しました。',
    ProviderRateLimited: 'AI プロバイダーの利用上限に達しました。',
    LegacyEpisodeMissing: '元の番組情報に話数がありません。',
    LegacyEpisodeUnknown: '元の番組情報から話数を読み取れませんでした。',
    LegacyRequiresManualBackfill: '既存録画のため、自動では Web 検索されていません。',
    LegacyBackfillFailed: '既存の話数情報を構造化できませんでした。',
    ProgramNotInSeries: '録画がシリーズに所属していないため、話数を判定できません。',
    AcceptancePolicyRejected: 'Web 検索結果が設定された受理条件を満たしませんでした。',
    LowConfidence: 'Web 検索結果の信頼度が受理条件を満たしませんでした。',
    InsufficientEvidence: 'Web 検索は完了しましたが、確定できる根拠が不足しています。',
    EpisodeLookupFailed: '話数 Web 検索に失敗しました。',
    MissingWebSearchCall: 'AI の応答から Web 検索の実行を確認できませんでした。',
    InvalidJSON: 'AI の応答を JSON として読み取れませんでした。',
    InvalidJSONType: 'AI の応答形式が正しくありません。',
    InvalidResponse: 'AI プロバイダーの応答形式が正しくありません。',
    InvalidResponseJSON: 'AI バックエンドの応答を読み取れませんでした。',
    IncompleteResponse: 'AI プロバイダーの応答が完了しませんでした。',
    MissingOutput: 'AI プロバイダーから判定結果が返りませんでした。',
    MissingOutputText: 'AI プロバイダーから判定本文が返りませんでした。',
    MultipleOutputTexts: 'AI プロバイダーから複数の判定本文が返りました。',
    InvalidOutputSchema: 'AI の応答が必要な形式を満たしていません。',
    InvalidModelOutput: 'AI の応答内容が必要な条件を満たしていません。',
    Timeout: 'AI バックエンドからの応答が一定時間途絶えたためタイムアウトしました。',
    HardTimeout: formatAcpHardTimeoutMessage('AI バックエンド'),
    NetworkError: 'AI バックエンドへ接続できませんでした。',
    InvalidURL: 'AI バックエンドの接続先 URL が正しくありません。',
    RedirectRejected: '安全のため、AI バックエンドからのリダイレクトを拒否しました。',
    AIRequestInterrupted: 'サーバーの停止または中断により、AI 検索を完了できませんでした。',
    InputChangedBeforeRequest: '検索開始前に録画情報が変更されたため、処理を中止しました。',
    InputChangedAfterRequest: '検索中に録画情報が変更されたため、結果を反映しませんでした。',
    SearchNotRun: 'AI の応答から Web 検索の実行を確認できませんでした。',
    UnsafePermissionRequested: '安全でない操作権限が要求されたため、検索を中止しました。',
    ACPProtocolError: 'ACP agent との通信手順を検証できませんでした。',
    ACPUnsafeToolRequested: 'ACP agent が許可されていない操作を要求しました。',
    ACPWebSearchNotObserved: 'ACP agent の Web 検索実行を確認できませんでした。',
    ACPSourceURLMissing: 'ACP agent の検索元 URL を確認できませんでした。',
    HostCLINotFound: 'ACP コマンドが見つかりません。',
    HostCLIPermissionDenied: 'ACP コマンドを実行する権限がありません。',
    HostCLIStartFailed: 'ACP コマンドを安全に起動できませんでした。',
    Cancelled: '話数 Web 検索が中断されました。',
};


export function episodeLookupOutcomeLabel(outcome: EpisodeLookupOutcome | null | undefined): string {
    return outcome === null || outcome === undefined ? '未実行' : EPISODE_LOOKUP_OUTCOME_LABELS[outcome];
}

/**
 * lookup_outcome 導入前の行では、録画全体の status・source と検索実行記録を組み合わせて表示する。
 * web_search_performed=true の旧行を「未実行」と誤表示しないことが、このフォールバックの主目的。
 */
export function episodeLookupOutcomeDisplayLabel(resolution: IRecordedEpisodeAssignmentResolution): string {
    if (resolution.lookup_outcome !== null && resolution.lookup_outcome !== undefined) {
        return episodeLookupOutcomeLabel(resolution.lookup_outcome);
    }

    const statusLabel = episodeResolutionStatusLabel(resolution.status);
    const sourceLabel = resolution.source === null ? '判定元不明' : episodeResolutionSourceLabel(resolution.source);
    const searchLabel = resolution.web_search_performed ? 'Web 検索実行済み' : 'Web 検索記録なし';
    return `${statusLabel}（${sourceLabel}・${searchLabel}）`;
}

export function episodeResolutionStatusLabel(status: RecordedEpisodeResolutionStatus): string {
    return EPISODE_RESOLUTION_STATUS_LABELS[status];
}

export function episodeResolutionSourceLabel(source: RecordedEpisodeResolutionSource): string {
    return EPISODE_RESOLUTION_SOURCE_LABELS[source];
}

function knownRecordedEpisodeErrorMessage(errorCode: string | null): string | null {
    if (errorCode === null) return null;
    if (RECORDED_EPISODE_ERROR_MESSAGES[errorCode] !== undefined) {
        return RECORDED_EPISODE_ERROR_MESSAGES[errorCode];
    }
    if (errorCode === 'HTTP401' || errorCode === 'HTTP403') {
        return 'AI バックエンドの認証に失敗しました。';
    }
    if (errorCode === 'HTTP404') {
        return '話数 Web 検索 API またはモデルが見つかりませんでした。';
    }
    if (errorCode === 'HTTP429') {
        return 'AI プロバイダーの利用上限に達しました。';
    }
    if (/^HTTP[45]\d{2}$/.test(errorCode)) {
        return 'AI バックエンドがエラーを返しました。';
    }
    return null;
}

/** API が返す安全なメッセージを優先し、旧行は固定のエラーコード map だけから補完する。 */
export function recordedEpisodeResolutionErrorMessage(
    resolution: IRecordedEpisodeAssignmentResolution,
): string | null {
    const errorMessage = resolution.error_message?.trim() ?? '';
    if (errorMessage !== '') return errorMessage;

    const knownErrorMessage = knownRecordedEpisodeErrorMessage(resolution.error_code);
    if (knownErrorMessage !== null) return knownErrorMessage;
    return resolution.error_code === null ? null : '話数判定の詳細を安全に表示できません。';
}

/** モデルの短い判定根拠を、空白だけの値を除外して詳細表示へ渡す。 */
export function recordedEpisodeResolutionRationale(
    resolution: IRecordedEpisodeAssignmentResolution,
): string | null {
    const rationale = resolution.rationale_short?.trim() ?? '';
    return rationale === '' ? null : rationale;
}

/** 保存可能なシーズン・話数が両方そろった AI 提案だけを、編集フォーム向けに返す。 */
export function recordedEpisodeResolutionProposal(
    resolution: IRecordedEpisodeAssignmentResolution | null | undefined,
): IRecordedEpisodeResolutionProposal | null {
    if (
        resolution?.proposed_season_number === null ||
        resolution?.proposed_season_number === undefined ||
        resolution.proposed_episode_number === null
    ) {
        return null;
    }
    return {
        seasonNumber: resolution.proposed_season_number,
        episodeNumber: resolution.proposed_episode_number,
    };
}

/**
 * 話数訂正ダイアログで「AI 検索結果を採用」を選べるかを返す。
 * 数値提案がある場合、または公式話数なし outcome の場合に true。
 */
export function canAdoptAIEpisodeResolution(
    resolution: IRecordedEpisodeAssignmentResolution | null | undefined,
): boolean {
    if (resolution === null || resolution === undefined) return false;
    if (resolution.lookup_outcome === 'NotNumbered') return true;
    return recordedEpisodeResolutionProposal(resolution) !== null;
}

/** 再検索の開始・監視中はポーリングを失わないよう、話数訂正ダイアログを閉じさせない。 */
export function canCloseEpisodeAssignmentDialog(
    isSaving: boolean,
    isStartingRelookup: boolean,
    isMonitoringRelookup: boolean,
): boolean {
    return isSaving === false && isStartingRelookup === false && isMonitoringRelookup === false;
}

/**
 * 202 直後の楽観表示をサーバー開始時と同じく正規化する。
 * lookup は Pending、直前試行の error / 根拠は消し、確定値と前回成功 evidence
 * （web_search_performed / citations / proposed_* / confidence）は維持する。
 */
export function markResolutionLookupPending(
    resolution: IRecordedEpisodeAssignmentResolution,
): IRecordedEpisodeAssignmentResolution {
    return {
        ...resolution,
        lookup_outcome: 'Pending',
        error_code: null,
        error_message: null,
        rationale_short: null,
    };
}

function episodeLookupOutcomeFallback(outcome: EpisodeLookupOutcome): string {
    return {
        Pending: '話数 Web 検索を実行しています。',
        Resolved: 'Web 検索の根拠から話数を確定しました。',
        NotNumbered: 'Web 検索の根拠から公式話数がないと判定しました。',
        InsufficientEvidence: 'Web 検索は実行されましたが、確定できる根拠が不足しています。',
        SearchFailed: '話数 Web 検索に失敗しました。',
        SearchNotRun: 'AI の応答はありましたが、Web 検索の実行を確認できませんでした。',
        InvalidModelOutput: 'AI の応答内容が必要な条件を満たしていません。',
        Disabled: '話数 Web 検索が無効です。',
        RateLimited: 'AI API の利用上限に達しました。',
        Cancelled: '話数 Web 検索が中断されました。',
    }[outcome];
}

function episodeResolutionStatusFallback(resolution: IRecordedEpisodeAssignmentResolution): string {
    const sourceLabel = resolution.source === null ? null : episodeResolutionSourceLabel(resolution.source);
    return {
        Pending: '話数判定を待っています。',
        Resolved: sourceLabel === null ? '話数を確定しました。' : `${sourceLabel}から話数を確定しました。`,
        Unknown: '話数を確定できませんでした。',
        NotNumbered: sourceLabel === null ?
            '公式の話数がない番組として確定しました。' :
            `${sourceLabel}から公式の話数がない番組として確定しました。`,
        NeedsReview: '話数を確定するには確認が必要です。',
        Failed: '話数判定を完了できませんでした。',
    }[resolution.status];
}

/**
 * 一覧とダイアログへ表示する理由を、安全な保存済み情報だけから決定する。
 * 生の provider 応答や未知の error_code は展開せず、最後は状態・判定元へフォールバックする。
 */
export function recordedEpisodeResolutionReason(resolution: IRecordedEpisodeAssignmentResolution): string {
    const rationale = recordedEpisodeResolutionRationale(resolution);
    if (rationale !== null) return rationale;

    const errorMessage = recordedEpisodeResolutionErrorMessage(resolution);
    if (errorMessage !== null) return errorMessage;

    if (resolution.lookup_outcome !== null && resolution.lookup_outcome !== undefined) {
        return episodeLookupOutcomeFallback(resolution.lookup_outcome);
    }
    return episodeResolutionStatusFallback(resolution);
}
