/**
 * AI バックエンド (OpenCode service) 管理 API クライアント。
 */

import APIClient from '@/services/APIClient';


export type AIAuthMode = 'ApiKey' | 'OAuthSubscription' | 'VertexAdc' | 'NoneLocal';
export type AIBillingMode = 'Metered' | 'Subscription' | 'Local';
export type AIBackendConnectionCapability = 'CandidateSelection' | 'EpisodeLookup';

/** OpenCode serve の availability。 */
export interface IOpenCodeAvailability {
    available: boolean;
    base_url: string;
    host: string;
    port: number;
    version: string | null;
    pinned_version: string;
    pid: number | null;
    workspace: string;
}

/** AI バックエンド service（キー本体は含まない）。 */
export interface IAIBackendService {
    service_id: string;
    service_name: string;
    backend_kind: 'OpenCode';
    opencode_provider_id: string;
    opencode_model_id: string;
    auth_mode: AIAuthMode;
    billing_mode: AIBillingMode;
    api_base_url: string | null;
    google_cloud_project: string | null;
    google_cloud_location: string | null;
    monthly_cost_limit_usd: string | null;
    monthly_token_limit: number | null;
    oauth_connected: boolean;
    auth_configured: boolean;
    episode_lookup_ready: boolean;
}

/** service 作成リクエスト。 */
export interface IAIBackendServiceCreate {
    service_name: string;
    opencode_provider_id: string;
    opencode_model_id: string;
    auth_mode: AIAuthMode;
    billing_mode: AIBillingMode;
    api_base_url?: string | null;
    google_cloud_project?: string | null;
    google_cloud_location?: string | null;
    monthly_cost_limit_usd?: string | null;
    monthly_token_limit?: number | null;
}

/** service 更新リクエスト。 */
export interface IAIBackendServiceUpdate {
    service_name?: string;
    opencode_provider_id?: string;
    opencode_model_id?: string;
    auth_mode?: AIAuthMode;
    billing_mode?: AIBillingMode;
    api_base_url?: string | null;
    google_cloud_project?: string | null;
    google_cloud_location?: string | null;
    monthly_cost_limit_usd?: string | null;
    monthly_token_limit?: number | null;
    clear_api_base_url?: boolean;
    clear_monthly_cost_limit_usd?: boolean;
    clear_monthly_token_limit?: boolean;
}

/** 接続試験結果。 */
export interface IAIBackendConnectionTestResult {
    success: boolean;
    latency_ms: number;
    model: string;
    message: string;
    prompt_tokens: number | null;
    completion_tokens: number | null;
    http_status: number | null;
    error_code: string | null;
}

/** 月次利用量（推定料金）。 */
export interface IAIBackendUsage {
    service_id: string;
    year_month: string;
    billing_mode: string;
    settled_prompt_tokens: number;
    settled_completion_tokens: number;
    settled_total_tokens: number;
    settled_estimated_cost_usd: string;
    reserved_total_tokens: number;
    reserved_estimated_cost_usd: string;
    settled_request_count: number;
    monthly_token_limit: number | null;
    monthly_cost_limit_usd: string | null;
    cost_limit_effective: boolean;
    token_limit_reached: boolean;
    cost_limit_reached: boolean;
    service_name_snapshot: string;
    opencode_provider_id_snapshot: string;
    opencode_model_id_snapshot: string;
    /** 設定から削除済みの service 履歴行（上限 enforce 対象外）。 */
    service_deleted: boolean;
}


export default class AIBackend {

    /** OpenCode serve の health を取得する。 */
    static async fetchHealth(): Promise<IOpenCodeAvailability | null> {
        const response = await APIClient.get<IOpenCodeAvailability>('/ai-backends/health');
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'OpenCode の状態を取得できませんでした。');
            return null;
        }
        return response.data;
    }

    /** service 一覧を取得する。 */
    static async fetchServices(): Promise<IAIBackendService[] | null> {
        const response = await APIClient.get<IAIBackendService[]>('/ai-backends');
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'AI バックエンド一覧を取得できませんでした。');
            return null;
        }
        return response.data;
    }

    /** service を作成する。 */
    static async createService(body: IAIBackendServiceCreate): Promise<IAIBackendService | null> {
        const response = await APIClient.post<IAIBackendService>('/ai-backends', body);
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'AI バックエンドを追加できませんでした。');
            return null;
        }
        return response.data;
    }

    /** service を更新する。 */
    static async updateService(
        service_id: string,
        body: IAIBackendServiceUpdate,
    ): Promise<IAIBackendService | null> {
        const response = await APIClient.put<IAIBackendService>(`/ai-backends/${service_id}`, body);
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'AI バックエンドを更新できませんでした。');
            return null;
        }
        return response.data;
    }

    /** service を削除する。 */
    static async deleteService(service_id: string): Promise<boolean> {
        const response = await APIClient.delete(`/ai-backends/${service_id}`);
        if (response.type === 'error') {
            if (response.status === 409) {
                APIClient.showGenericError(response, '録画シリーズから参照中のため削除できません。');
            } else {
                APIClient.showGenericError(response, 'AI バックエンドを削除できませんでした。');
            }
            return false;
        }
        return true;
    }

    /** API キーを設定する。 */
    static async setAPIKey(service_id: string, api_key: string): Promise<boolean> {
        const response = await APIClient.put(`/ai-backends/${service_id}/api-key`, {api_key});
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'API キーを設定できませんでした。');
            return false;
        }
        return true;
    }

    /** API キーを削除する。 */
    static async deleteAPIKey(service_id: string): Promise<boolean> {
        const response = await APIClient.delete(`/ai-backends/${service_id}/api-key`);
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'API キーを削除できませんでした。');
            return false;
        }
        return true;
    }

    /** OAuth 開始（仮）。 */
    static async startOAuth(service_id: string): Promise<Record<string, unknown> | null> {
        const response = await APIClient.post<{provider_id: string; authorize: Record<string, unknown>;}>(
            `/ai-backends/${service_id}/oauth/start`,
            {},
        );
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'OAuth を開始できませんでした。');
            return null;
        }
        return response.data.authorize;
    }

    /** OAuth 切断（仮）。 */
    static async disconnectOAuth(service_id: string): Promise<boolean> {
        const response = await APIClient.post(`/ai-backends/${service_id}/oauth/disconnect`, {});
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'OAuth を切断できませんでした。');
            return false;
        }
        return true;
    }

    /** 保存済み service の接続試験。 */
    static async testConnection(
        service_id: string,
        capability: AIBackendConnectionCapability = 'CandidateSelection',
    ): Promise<IAIBackendConnectionTestResult | null> {
        const response = await APIClient.post<IAIBackendConnectionTestResult>(
            '/ai-backends/connection-test',
            {service_id, capability},
            // 接続試験は時間がかかることがある
            {timeout: 180 * 1000},
        );
        if (response.type === 'error') {
            if (response.status === 503) {
                APIClient.showGenericError(response, 'OpenCode serve が利用できません。');
            } else {
                APIClient.showGenericError(response, '接続試験を実行できませんでした。');
            }
            return null;
        }
        return response.data;
    }

    /**
     * 月次利用量一覧（当月カード用。削除済み履歴も含む）。
     */
    static async fetchUsageList(year_month?: string): Promise<IAIBackendUsage[] | null> {
        const response = await APIClient.get<IAIBackendUsage[]>('/ai-backends/usage', {
            params: year_month ? {year_month} : undefined,
        });
        if (response.type === 'error') {
            APIClient.showGenericError(response, '月次利用量を取得できませんでした。');
            return null;
        }
        return response.data;
    }
}
