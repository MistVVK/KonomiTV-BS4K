import type { IAnalysisTaskAccepted } from '@/services/AnalysisTasks';

import APIClient from '@/services/APIClient';


export type RecordedEpisodeNumberAcceptanceMode = 'HighConfidenceOnly' | 'Always';
export type RecordedSeriesConnectionTestCapability = 'CandidateSelection' | 'EpisodeLookup';


/** 録画シリーズ判定のサーバー共有設定。API キーそのものは取得レスポンスに含めない。 */
export interface IRecordedSeriesSettings {
    enabled: boolean;
    ai_enabled: boolean;
    ai_candidate_selection_enabled: boolean;
    ai_episode_number_search_enabled: boolean;
    ai_episode_number_acceptance_mode: RecordedEpisodeNumberAcceptanceMode;
    api_base_url: string;
    model: string;
    daily_ai_request_limit: number;
    api_key_configured: boolean;
}

/** 録画シリーズ判定設定の更新リクエスト。API キー省略時は保存済みの値を維持する。 */
export interface IRecordedSeriesSettingsUpdate {
    enabled: boolean;
    ai_enabled: boolean;
    ai_candidate_selection_enabled: boolean;
    ai_episode_number_search_enabled: boolean;
    ai_episode_number_acceptance_mode: RecordedEpisodeNumberAcceptanceMode;
    api_base_url: string;
    model: string;
    daily_ai_request_limit: number;
    api_key?: string;
}

/** OpenAI 互換 API の接続テストリクエスト。 */
export interface IRecordedSeriesConnectionTestRequest {
    capability: RecordedSeriesConnectionTestCapability;
    api_base_url: string;
    model: string;
    api_key?: string;
}

/** OpenAI 互換 API の接続テスト結果。 */
export interface IRecordedSeriesConnectionTestResult {
    success: boolean;
    latency_ms: number;
    model: string;
    message: string;
}

/** 録画シリーズ判定の全体状況。 */
export interface IRecordedSeriesStatus {
    total: number;
    pending: number;
    resolved: number;
    not_series: number;
    needs_review: number;
    failed: number;
    episode_resolved: number;
    episode_unknown: number;
    episode_not_numbered: number;
    episode_needs_review: number;
    episode_failed: number;
    ai_requests_today: number;
    series_ai_requests_today: number;
    episode_ai_requests_today: number;
    last_run_at: string | null;
    episode_last_run_at: string | null;
    is_running: boolean;
    is_episode_running: boolean;
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

/** 録画シリーズの表示情報を管理者が編集するときのリクエスト。 */
export interface IRecordedSeriesManagementUpdate {
    title: string;
    description: string;
    expected_title: string;
    expected_description: string;
}

/** 録画シリーズの表示情報を更新した結果。 */
export type IRecordedSeriesManagementUpdateResult =
    | {type: 'Success'}
    | {type: 'Stale'}
    | {type: 'Conflict'}
    | {type: 'Busy'}
    | {type: 'Error'};

/** 管理者が録画番組のシリーズ割り当てを訂正するときのリクエスト。 */
export type IRecordedSeriesAssignment =
    | {decision: 'Series'; series_id: number}
    | {decision: 'Series'; series_title: string}
    | {decision: 'NotSeries'};

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
    status: 'Pending' | 'Resolved' | 'Unknown' | 'NotNumbered' | 'NeedsReview' | 'Failed';
    source: 'Local' | 'EPG' | 'WebSearch' | 'Manual' | 'Migration' | null;
    proposed_season_number: number | null;
    proposed_episode_number: string | null;
    confidence: number | null;
    web_search_performed: boolean;
    citations: {url: string; title: string;}[];
    error_code: string | null;
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

/** 管理者が録画の話数を手動訂正するときの、楽観ロック付きリクエスト。 */
export type IRecordedEpisodeAssignmentUpdate =
    | {
        decision: 'ExistingEpisode';
        expected_series_id: number;
        expected_series_episode_id: number | null;
        episode_id: number;
    }
    | {
        decision: 'StructuredEpisode';
        expected_series_id: number;
        expected_series_episode_id: number | null;
        season_number: number;
        episode_number: string;
    }
    | {
        decision: 'Unknown';
        expected_series_id: number;
        expected_series_episode_id: number | null;
    };

/** 手動話数訂正の更新結果。 */
export type IRecordedEpisodeAssignmentUpdateResult =
    | {type: 'Success'}
    | {type: 'Stale'}
    | {type: 'NotFound'}
    | {type: 'Error'};


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

    /** 保存済みの OpenAI 互換 API キーを削除する。 */
    static async deleteAPIKey(): Promise<boolean> {
        const response = await APIClient.delete('/recorded-series/settings/api-key');
        if (response.type === 'error') {
            APIClient.showGenericError(response, '保存済みの API キーを削除できませんでした。');
            return false;
        }
        return true;
    }

    /** 現在の入力内容を保存せずに OpenAI 互換 API への接続を確認する。 */
    static async testConnection(
        request: IRecordedSeriesConnectionTestRequest,
    ): Promise<IRecordedSeriesConnectionTestResult | null> {
        const response = await APIClient.post<IRecordedSeriesConnectionTestResult>(
            '/recorded-series/settings/test',
            request,
        );
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'OpenAI 互換 API への接続を確認できませんでした。');
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

    /** 管理者向けの録画シリーズ1件を最新状態で取得する。 */
    static async fetchManagementSeries(
        series_id: number,
        show_error = true,
    ): Promise<IRecordedSeriesManagementItem | null> {
        const response = await APIClient.get<IRecordedSeriesManagementItem>(`/recorded-series/series/${series_id}`);
        if (response.type === 'error') {
            if (show_error) {
                APIClient.showGenericError(response, '録画シリーズ情報を取得できませんでした。');
            }
            return null;
        }
        return response.data;
    }

    /** 録画シリーズのタイトルと説明を更新する。 */
    static async updateManagementSeries(
        series_id: number,
        update: IRecordedSeriesManagementUpdate,
    ): Promise<IRecordedSeriesManagementUpdateResult> {
        const response = await APIClient.put(
            `/recorded-series/series/${series_id}`,
            update,
        );
        if (response.type === 'error') {
            if (response.status === 409) {
                if (response.data.detail === 'Recorded series metadata was updated by another request.') {
                    return {type: 'Stale'};
                }
                return {type: 'Conflict'};
            }
            if (response.status === 503) return {type: 'Busy'};

            APIClient.showGenericError(response, '録画シリーズを更新できませんでした。');
            return {type: 'Error'};
        }
        return {type: 'Success'};
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

    /** 録画の構造化話数を、読み込み時点の割当を確認して手動更新する。 */
    static async updateEpisodeAssignment(
        recorded_program_id: number,
        update: IRecordedEpisodeAssignmentUpdate,
    ): Promise<IRecordedEpisodeAssignmentUpdateResult> {
        const response = await APIClient.put(
            `/recorded-series/programs/${recorded_program_id}/episode-assignment`,
            update,
        );
        if (response.type === 'error') {
            if (response.status === 404) return {type: 'NotFound'};
            if (response.status === 409) return {type: 'Stale'};

            APIClient.showGenericError(response, '録画の話数を訂正できませんでした。');
            return {type: 'Error'};
        }
        return {type: 'Success'};
    }

    /** 既存録画のシリーズ判定を開始する。force 時は確定済みも再判定する。 */
    static async startBackfill(force = false): Promise<IAnalysisTaskAccepted | null> {
        const response = await APIClient.post<IAnalysisTaskAccepted>('/recorded-series/backfill', {force});
        if (response.type === 'error') {
            APIClient.showGenericError(response, '既存録画のシリーズ判定を開始できませんでした。');
            return null;
        }
        return response.data;
    }

    /** 既存録画の話数判定を開始する。force 時は自動判定済みの結果も再判定する。 */
    static async startEpisodeBackfill(force = false): Promise<IAnalysisTaskAccepted | null> {
        const response = await APIClient.post<IAnalysisTaskAccepted>('/recorded-series/episodes/backfill', {force});
        if (response.type === 'error') {
            APIClient.showGenericError(response, '既存録画の話数判定を開始できませんでした。');
            return null;
        }
        return response.data;
    }

    /** 録画番組を既存・新規シリーズへ割り当てるか、単発番組へ訂正する。 */
    static async updateProgramAssignment(
        recorded_program_id: number,
        assignment: IRecordedSeriesAssignment,
    ): Promise<boolean> {
        const response = await APIClient.put(
            `/recorded-series/programs/${recorded_program_id}/assignment`,
            assignment,
        );
        if (response.type === 'error') {
            APIClient.showGenericError(response, '録画番組のシリーズを訂正できませんでした。');
            return false;
        }
        return true;
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
