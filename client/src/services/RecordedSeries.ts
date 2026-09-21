import type { IAnalysisTaskAccepted } from '@/services/AnalysisTasks';

import APIClient from '@/services/APIClient';


/** 録画シリーズが選択できる AI バックエンド。OpenCode は AIBackend service_id を参照する。 */
export type AIBackendKind = 'OpenCode' | 'OpenAICompatible' | 'OpenAICompatible2' | 'AcpCodex' | 'AcpGrok';
/** 主系 AI 失敗後の回復方針。既定は追加試行なしの Fail。 */
export type AIFailureRecoveryStrategy = 'FallbackBackend' | 'RetrySameBackend' | 'Fail';
/** シリーズメタデータの外部ソース。TMDb と Bangumi (bgm.tv) の併用可否を選ぶ。 */
export type ExternalMetadataSource = 'TmdbAndBangumi' | 'TmdbOnly' | 'BangumiOnly' | 'None';
export type EpisodeLookupOutcome =
    'Pending' | 'Resolved' | 'NotNumbered' | 'NoPublishedNumber' | 'InsufficientEvidence' | 'SearchFailed' |
    'SearchNotRun' | 'InvalidModelOutput' | 'Disabled' | 'RateLimited' | 'Cancelled';


/** TMDb 接続試験の結果。API キー本体は含まれない。 */
export interface ITmdbConnectionTestResult {
    success: boolean;
    latency_ms: number;
    message: string;
    http_status: number | null;
    error_code: string | null;
}


/** 録画シリーズ判定のサーバー共有設定。AI 接続・認証・モデルは AI バックエンド側が正本。 */
export interface IRecordedSeriesSettings {
    enabled: boolean;
    ai_enabled: boolean;
    ai_backend: AIBackendKind;
    // OpenCode 時の AIBackend service UUID
    ai_backend_service_id: string | null;
    // 選択中の OpenCode service 表示名。ACP または未設定時は null。
    ai_backend_service_name: string | null;
    // 選択中の OpenCode / ACP バックエンドに利用可能な認証があるか。
    ai_backend_auth_configured: boolean;
    // 主系失敗後の回復方針。
    ai_failure_recovery_strategy: AIFailureRecoveryStrategy;
    // FallbackBackend 時のみ使う予備 AI バックエンド。
    ai_fallback_backend: AIBackendKind | null;
    // 予備が OpenCode のときの service UUID。
    ai_fallback_backend_service_id: string | null;
    // 予備 OpenCode service 表示名。
    ai_fallback_backend_service_name: string | null;
    // 予備 OpenCode / ACP バックエンドに利用可能な認証があるか。
    ai_fallback_backend_auth_configured: boolean;
    // シリーズメタデータの外部ソース。既定は TMDb と Bangumi の併用。
    external_metadata_source: ExternalMetadataSource;
    // TMDb API キーがサーバーの Fernet ストアに保存済みか。キー本体は返らない。
    tmdb_api_key_configured: boolean;
    // TMDb API キーのマスク表示。未設定時は null。
    tmdb_api_key_masked: string | null;
}

/** 録画シリーズ判定設定の更新リクエスト。 */
export interface IRecordedSeriesSettingsUpdate {
    enabled: boolean;
    ai_enabled: boolean;
    ai_backend: AIBackendKind;
    ai_backend_service_id: string | null;
    ai_failure_recovery_strategy: AIFailureRecoveryStrategy;
    ai_fallback_backend: AIBackendKind | null;
    ai_fallback_backend_service_id: string | null;
    external_metadata_source: ExternalMetadataSource;
}

/** 録画シリーズ判定の全体状況。 */
export interface IRecordedSeriesStatus {
    total: number;
    assigned: number;
    unassigned: number;
    episode_resolved: number;
    episode_unknown: number;
    episode_not_numbered: number;
    episode_no_published_number: number;
    episode_needs_review: number;
    episode_failed: number;
    episode_last_run_at: string | null;
    is_running: boolean;
    is_episode_running: boolean;
}

/** シリーズ DB 削除で消した表ごとの行数。録画本体は含まない。 */
export interface IRecordedSeriesDatabaseDeleteResult {
    series: number;
    series_episodes: number;
    series_aliases: number;
    series_broadcast_periods: number;
    series_ai_fallbacks: number;
    recorded_series_rules: number;
    recorded_series_resolutions: number;
    recorded_series_ai_requests: number;
    recorded_episode_resolutions: number;
    bangumi_episode_completions: number;
}

/** 管理画面の一覧に表示する、録画シリーズの軽量な集計情報。 */
export interface IRecordedSeriesManagementItem {
    id: number;
    title: string;
    description: string;
    wikipedia_page_id: number | null;
    recorded_program_count: number;
    first_recorded_at: string | null;
    last_recorded_at: string | null;
    updated_at: string;
}

/** 管理画面向け録画シリーズ一覧。 */
export interface IRecordedSeriesManagementList {
    total: number;
    page: number;
    page_size: number;
    items: IRecordedSeriesManagementItem[];
}

/** 管理画面でシリーズへ割り当て直せる、シリーズ未所属の再生可能録画。 */
export interface IRecordedSeriesStandaloneProgram {
    recorded_program_id: number;
    title: string;
    subtitle: string | null;
    start_time: string;
    channel_id: string | null;
    channel_name: string | null;
}

/** シリーズ未所属録画のページング一覧。 */
export interface IRecordedSeriesStandaloneProgramList {
    total: number;
    page: number;
    page_size: number;
    items: IRecordedSeriesStandaloneProgram[];
}

/** 同じシリーズ内で、現在の録画より後に始まる次の録画。 */
export interface IRecordedSeriesNextProgram {
    recorded_program_id: number | null;
}

/** Series 内で手動割当先として選べる構造化済みの話数。 */
export interface IRecordedEpisodeAssignmentEpisode {
    id: number;
    season_number: number;
    episode_number: string;
}

/** 録画ごとの話数判定状態と、AI Web 検索を含む判断根拠。 */
export interface IRecordedEpisodeAssignmentResolution {
    status: 'Pending' | 'Resolved' | 'Unknown' | 'NotNumbered' | 'NoPublishedNumber' | 'NeedsReview' | 'Failed';
    /** 現在の正本シーズン。番号付き回以外では null の場合がある。 */
    season_number: number | null;
    /** 現在の正本（採用中のレーン）。 */
    source: 'Local' | 'EPG' | 'WebSearch' | 'Manual' | 'Migration' | 'AI' | null;
    lookup_outcome: EpisodeLookupOutcome | null;
    /** AI レーン。 */
    proposed_outcome: 'Resolved' | 'NotNumbered' | 'NoPublishedNumber' | 'InsufficientEvidence' | null;
    proposed_season_number: number | null;
    proposed_episode_number: string | null;
    confidence: number | null;
    web_search_performed: boolean;
    citations: {url: string; title: string;}[];
    rationale_short: string | null;
    /** 手動レーン。 */
    manual_season_number: number | null;
    manual_episode_number: string | null;
    manual_status: 'Resolved' | 'Unknown' | 'NotNumbered' | 'NoPublishedNumber' | null;
    error_code: string | null;
    error_message: string | null;
}

/** Series 管理画面で話数を訂正できる録画。 */
export interface IRecordedEpisodeAssignmentProgram {
    recorded_program_id: number;
    title: string;
    subtitle: string | null;
    legacy_episode_number: string | null;
    start_time: string;
    channel_id: string | null;
    channel_name: string | null;
    series_episode_id: number | null;
    resolution: IRecordedEpisodeAssignmentResolution | null;
}

/** Series 内の構造化話数候補と、録画ごとの現在割当。 */
export interface IRecordedEpisodeAssignmentList {
    series_id: number;
    episodes: IRecordedEpisodeAssignmentEpisode[];
    programs: IRecordedEpisodeAssignmentProgram[];
}




/** 録画シリーズ判定の設定・接続確認・一括判定 API。 */
export default class RecordedSeries {

    /** 管理者向けの録画シリーズ判定設定を取得する。 */
    static async fetchSettings(): Promise<IRecordedSeriesSettings | null> {
        const response = await APIClient.get<IRecordedSeriesSettings>('/recorded-series/settings');
        if (response.type === 'error') {
            APIClient.showGenericError(response, '録画シリーズ判定設定を取得できませんでした。');
            return null;
        }
        return response.data;
    }

    /** 録画シリーズ判定設定を更新する。 */
    static async updateSettings(settings: IRecordedSeriesSettingsUpdate): Promise<boolean> {
        const response = await APIClient.put('/recorded-series/settings', settings);
        if (response.type === 'error') {
            APIClient.showGenericError(response, '録画シリーズ判定設定を更新できませんでした。');
            return false;
        }
        return true;
    }

    /** TMDb API キーをサーバーの Fernet ストアへ保存する。 */
    static async setTmdbAPIKey(api_key: string): Promise<boolean> {
        const response = await APIClient.put('/recorded-series/settings/tmdb-api-key', {api_key});
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'TMDb API キーを設定できませんでした。');
            return false;
        }
        return true;
    }

    /** 保存済みの TMDb API キーを削除する。既存の TMDb メタデータは残る。 */
    static async deleteTmdbAPIKey(): Promise<boolean> {
        const response = await APIClient.delete('/recorded-series/settings/tmdb-api-key');
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'TMDb API キーを削除できませんでした。');
            return false;
        }
        return true;
    }

    /** 保存済み TMDb API キーで TMDb へ実通信し、成否を取得する。 */
    static async testTmdbConnection(): Promise<ITmdbConnectionTestResult | null> {
        const response = await APIClient.post<ITmdbConnectionTestResult>(
            '/recorded-series/settings/tmdb-connection-test',
            undefined,
            // サーバー側の TMDb タイムアウト 10 秒に余裕を持たせる。
            {timeout: 30 * 1000},
        );
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'TMDb の接続試験を実行できませんでした。');
            return null;
        }
        return response.data;
    }

    /** 録画シリーズ判定の件数・実行状況を取得する。 */
    static async fetchStatus(show_error = true): Promise<IRecordedSeriesStatus | null> {
        const response = await APIClient.get<IRecordedSeriesStatus>('/recorded-series/status');
        if (response.type === 'error') {
            if (show_error) {
                APIClient.showGenericError(response, '録画シリーズ判定の状況を取得できませんでした。');
            }
            return null;
        }
        return response.data;
    }

    /** 管理者向けの軽量な録画シリーズ一覧を、検索・ページング付きで取得する。 */
    static async fetchManagementSeriesList(
        query = '',
        page = 1,
        page_size = 30,
        show_error = true,
    ): Promise<IRecordedSeriesManagementList | null> {
        const response = await APIClient.get<IRecordedSeriesManagementList>('/recorded-series/series', {
            params: {
                query,
                page,
                page_size,
            },
        });
        if (response.type === 'error') {
            if (show_error) {
                APIClient.showGenericError(response, '録画シリーズ一覧を取得できませんでした。');
            }
            return null;
        }
        return response.data;
    }

    /** 管理者向けに、Series 内の構造化話数候補と録画ごとの割当を取得する。 */
    static async fetchEpisodeAssignments(
        series_id: number,
        show_error = true,
    ): Promise<IRecordedEpisodeAssignmentList | null> {
        const response = await APIClient.get<IRecordedEpisodeAssignmentList>(
            `/recorded-series/series/${series_id}/episode-assignments`,
        );
        if (response.type === 'error') {
            if (show_error) {
                APIClient.showGenericError(response, '録画の話数割当を取得できませんでした。');
            }
            return null;
        }
        return response.data;
    }

    /** 指定範囲の Indexer・AI 補完・外部同期・話数判定を単一履歴で開始する。 */
    static async startPipeline(scope: 'Unresolved' | 'All'): Promise<IAnalysisTaskAccepted | null> {
        const response = await APIClient.post<IAnalysisTaskAccepted>('/recorded-series/pipeline', {scope});
        if (response.type === 'error') {
            APIClient.showGenericError(response, '録画のシリーズ・話数一括判定を開始できませんでした。');
            return null;
        }
        return response.data;
    }

    /** シリーズ関連データだけを削除する。録画本体・サムネイル・CM・履歴は残る。 */
    static async deleteSeriesDatabase(): Promise<IRecordedSeriesDatabaseDeleteResult | null> {
        const response = await APIClient.delete<IRecordedSeriesDatabaseDeleteResult>('/recorded-series/database');
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'シリーズデータベースを削除できませんでした。');
            return null;
        }
        return response.data;
    }

    /** 管理画面向けに、シリーズ未所属の再生可能録画をページング取得する。 */
    static async fetchStandalonePrograms(
        query = '',
        page = 1,
        page_size = 30,
        show_error = true,
    ): Promise<IRecordedSeriesStandaloneProgramList | null> {
        const response = await APIClient.get<IRecordedSeriesStandaloneProgramList>(
            '/recorded-series/standalone-programs',
            {
                params: {
                    query: query === '' ? undefined : query,
                    page,
                    page_size,
                },
            },
        );
        if (response.type === 'error') {
            if (show_error) {
                APIClient.showGenericError(response, 'シリーズ未所属の録画一覧を取得できませんでした。');
            }
            return null;
        }
        return response.data;
    }

    /** 同じシリーズ内の次の録画 ID を取得する。次がない場合はフィールドが null になる。 */
    static async fetchNextProgram(recorded_program_id: number): Promise<IRecordedSeriesNextProgram | null> {
        const response = await APIClient.get<IRecordedSeriesNextProgram>(
            `/recorded-series/programs/${recorded_program_id}/next`,
        );
        if (response.type === 'error') {
            APIClient.showGenericError(response, '次の録画番組を取得できませんでした。');
            return null;
        }
        return response.data;
    }
}
