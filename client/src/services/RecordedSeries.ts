import type { IAnalysisTaskAccepted } from '@/services/AnalysisTasks';

import APIClient from '@/services/APIClient';


/** 録画シリーズ判定のサーバー共有設定。API キーそのものは取得レスポンスに含めない。 */
export interface IRecordedSeriesSettings {
    enabled: boolean;
    ai_enabled: boolean;
    api_base_url: string;
    model: string;
    daily_ai_request_limit: number;
    api_key_configured: boolean;
}

/** 録画シリーズ判定設定の更新リクエスト。API キー省略時は保存済みの値を維持する。 */
export interface IRecordedSeriesSettingsUpdate {
    enabled: boolean;
    ai_enabled: boolean;
    api_base_url: string;
    model: string;
    daily_ai_request_limit: number;
    api_key?: string;
}

/** OpenAI 互換 API の接続テストリクエスト。 */
export interface IRecordedSeriesConnectionTestRequest {
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
    ai_requests_today: number;
    last_run_at: string | null;
    is_running: boolean;
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

    /** 既存録画のシリーズ判定を開始する。force 時は確定済みも再判定する。 */
    static async startBackfill(force = false): Promise<IAnalysisTaskAccepted | null> {
        const response = await APIClient.post<IAnalysisTaskAccepted>('/recorded-series/backfill', {force});
        if (response.type === 'error') {
            APIClient.showGenericError(response, '既存録画のシリーズ判定を開始できませんでした。');
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
