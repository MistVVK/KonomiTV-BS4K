/**
 * AI バックエンド (OpenCode service) 管理 API クライアント。
 */

import APIClient from '@/services/APIClient';


export type AIAuthMode = 'ApiKey' | 'OAuthSubscription' | 'VertexAdc' | 'NoneLocal';
export type AIBillingMode = 'Metered' | 'Subscription' | 'Local';
export type AIBackendConnectionCapability = 'CandidateSelection' | 'EpisodeLookup';
export type AIProviderSupportKind = 'Supported' | 'UnsupportedComplex';
export type AIProviderAuthMethodType = 'api' | 'oauth' | 'vertex_adc';
export type OpenCodeProviderType = 'Catalog' | 'OpenAICompatible' | 'AnthropicCompatible';
export type StructuredOutputMode = 'Auto' | 'StructuredOutput' | 'JSONText';

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
    opencode_provider_type: OpenCodeProviderType;
    opencode_provider_id: string;
    opencode_model_id: string;
    opencode_model_variant: string | null;
    structured_output_mode: StructuredOutputMode;
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
    opencode_provider_type?: OpenCodeProviderType;
    opencode_provider_id?: string | null;
    opencode_model_id: string;
    opencode_model_variant?: string | null;
    structured_output_mode?: StructuredOutputMode;
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
    opencode_provider_type?: OpenCodeProviderType;
    opencode_provider_id?: string | null;
    opencode_model_id?: string;
    opencode_model_variant?: string | null;
    structured_output_mode?: StructuredOutputMode;
    auth_mode?: AIAuthMode;
    billing_mode?: AIBillingMode;
    api_base_url?: string | null;
    google_cloud_project?: string | null;
    google_cloud_location?: string | null;
    monthly_cost_limit_usd?: string | null;
    monthly_token_limit?: number | null;
    clear_opencode_model_variant?: boolean;
    clear_api_base_url?: boolean;
    clear_monthly_cost_limit_usd?: boolean;
    clear_monthly_token_limit?: boolean;
}

/** 接続試験の1能力チェック。 */
export interface IAIBackendConnectionTestCheck {
    status: 'Passed' | 'Failed' | 'NotRun' | 'NotApplicable';
    message: string;
}

/** EpisodeLookup 接続試験の固定6項目。 */
export interface IAIBackendEpisodeLookupConnectionChecks {
    backend_connection: IAIBackendConnectionTestCheck;
    web_search: IAIBackendConnectionTestCheck;
    source_url: IAIBackendConnectionTestCheck;
    strict_schema: IAIBackendConnectionTestCheck;
    timeout_cancel: IAIBackendConnectionTestCheck;
    permission_policy: IAIBackendConnectionTestCheck;
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
    /** EpisodeLookup 時のみ。生成試験では null。 */
    checks: IAIBackendEpisodeLookupConnectionChecks | null;
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

/** service 追加 UI 用の provider モデル 1 件。 */
export interface IAIBackendProviderModel {
    model_id: string;
    model_name: string;
    variants: string[];
    openai_fast_mode: boolean;
    openai_paired_model_id: string | null;
    capabilities: Record<string, unknown>;
}

/**
 * OpenCode Web 相当の認証方式 1 件。
 * provider 選択後に「API Key / OAuth (browser) / …」として並べる。
 */
export interface IAIBackendProviderAuthMethod {
    method_index: number | null;
    type: AIProviderAuthMethodType;
    label: string;
    auth_mode: Exclude<AIAuthMode, 'NoneLocal'>;
    billing_mode_default: 'Metered' | 'Subscription';
}

/** service 追加 UI 用の provider 1 件（全カタログ）。 */
export interface IAIBackendProvider {
    provider_id: string;
    provider_name: string;
    connected: boolean;
    support_kind: AIProviderSupportKind;
    support_note: string | null;
    auth_methods: IAIBackendProviderAuthMethod[];
    models: IAIBackendProviderModel[];
    default_model_id: string | null;
}

/** OAuth 開始応答（browser で URL を開く）。 */
export interface IAIBackendOAuthStartResult {
    provider_id: string;
    method: number;
    url: string | null;
    authorization_method: 'auto' | 'code' | null;
    instructions: string | null;
    authorize: Record<string, unknown>;
}


// ===== ACP 固定プリセット（Codex / Grok Build） =====

/** ACP の推論深さ。Codex は XHigh/Max/Ultra まで、Grok は Low〜High。 */
export type AcpReasoningEffort = 'Low' | 'Medium' | 'High' | 'XHigh' | 'Max' | 'Ultra';
/** ACP 固定プリセットのバックエンド種別。 */
export type ACPBackendKind = 'AcpCodex' | 'AcpGrok';

/** 1 プロバイダ（Codex / Grok）分の ACP 実行設定。 */
export interface IACPBackendSettings {
    model: string | null;
    reasoning_effort: AcpReasoningEffort | null;
    /** Codex 専用。Grok では常に false。 */
    codex_fast_mode_enabled: boolean;
    /** ACP stdio の無通信打ち切り秒数（30〜600）。 */
    timeout_sec: number;
}

/** ACP 固定プリセット全体の設定。 */
export interface IACPSettings {
    codex: IACPBackendSettings;
    grok: IACPBackendSettings;
}

/** 認証内容を含まない ACP 共有資格情報状態。 */
export interface IACPBackendCredentialStatus {
    acp_operation_running: boolean;
    codex_host_auth_available: boolean;
    codex_auth_imported: boolean;
    codex_auth_imported_at: string | null;
    codex_auth_in_use: boolean;
    grok_host_auth_available: boolean;
    grok_auth_imported: boolean;
    grok_auth_imported_at: string | null;
    grok_auth_in_use: boolean;
    google_adc_available: boolean;
}

/** ACP 接続試験の1能力の状態。 */
export interface IACPBackendConnectionTestCheck {
    status: 'Passed' | 'Failed' | 'NotRun' | 'NotApplicable';
    message: string;
}

/** ACP EpisodeLookup 接続試験の固定6項目。 */
export interface IACPBackendEpisodeLookupConnectionChecks {
    backend_connection: IACPBackendConnectionTestCheck;
    web_search: IACPBackendConnectionTestCheck;
    source_url: IACPBackendConnectionTestCheck;
    strict_schema: IACPBackendConnectionTestCheck;
    timeout_cancel: IACPBackendConnectionTestCheck;
    permission_policy: IACPBackendConnectionTestCheck;
}

/** ACP 接続試験結果。 */
export interface IACPBackendConnectionTestResult {
    success: boolean;
    latency_ms: number;
    model: string;
    message: string;
    checks: IACPBackendEpisodeLookupConnectionChecks | null;
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

    /**
     * provider カタログ（OpenCode Web 相当）を取得する。
     * 未接続を含み、各 provider の認証方式一覧を返す。
     */
    static async fetchProviders(): Promise<IAIBackendProvider[] | null> {
        const response = await APIClient.get<{providers: IAIBackendProvider[]}>('/ai-backends/providers');
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'OpenCode の provider 一覧を取得できませんでした。');
            return null;
        }
        return response.data.providers;
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

    /**
     * OAuth を開始する。返却 URL を browser で開くこと。
     * method は provider.auth_methods[].method_index。
     */
    static async startOAuth(
        service_id: string,
        method: number = 0,
    ): Promise<IAIBackendOAuthStartResult | null> {
        const response = await APIClient.post<IAIBackendOAuthStartResult>(
            `/ai-backends/${service_id}/oauth/start`,
            {method},
        );
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'OAuth を開始できませんでした。');
            return null;
        }
        return response.data;
    }

    /**
     * OAuth callback を完了する。
     * browser (auto) は code 無し、headless/device は code を渡す。
     */
    static async completeOAuth(
        service_id: string,
        method: number = 0,
        code: string | null = null,
    ): Promise<boolean> {
        const response = await APIClient.post(
            `/ai-backends/${service_id}/oauth/callback`,
            {method, code},
            // callback は provider 側との往復で時間がかかることがある
            {timeout: 120 * 1000},
        );
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'OAuth の完了処理に失敗しました。');
            return false;
        }
        return true;
    }

    /** OAuth を切断する。 */
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

    // ===== ACP 固定プリセット（Codex / Grok Build） =====

    /** ACP 固定プリセット設定を取得する。 */
    static async fetchACPSettings(): Promise<IACPSettings | null> {
        const response = await APIClient.get<IACPSettings>('/ai-backends/acp-settings');
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'ACP 設定を取得できませんでした。');
            return null;
        }
        return response.data;
    }

    /** ACP 固定プリセット設定を保存する（全体置き換え）。 */
    static async updateACPSettings(settings: IACPSettings): Promise<boolean> {
        const response = await APIClient.put('/ai-backends/acp-settings', settings);
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'ACP 設定を保存できませんでした。');
            return false;
        }
        return true;
    }

    /** ACP 認証状態を取得する。 */
    static async fetchACPCredentialStatus(): Promise<IACPBackendCredentialStatus | null> {
        const response = await APIClient.get<IACPBackendCredentialStatus>('/ai-backends/acp-credentials');
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'ACP 認証状態を取得できませんでした。');
            return null;
        }
        return response.data;
    }

    /** ホストの auth.json を KonomiTV-BS4K 専用プロファイルへ取り込む。 */
    static async importACPAuthentication(
        provider: 'codex' | 'grok',
    ): Promise<IACPBackendCredentialStatus | null> {
        const response = await APIClient.post<IACPBackendCredentialStatus>(
            `/ai-backends/acp-credentials/${provider}/import`,
            {},
        );
        if (response.type === 'error') {
            APIClient.showGenericError(
                response,
                'ACP 認証を取り込めませんでした。ホスト側の認証と Compose 設定を確認してください。',
            );
            return null;
        }
        return response.data;
    }

    /** KonomiTV-BS4K 専用プロファイルの auth.json コピーを削除する。 */
    static async deleteACPAuthentication(
        provider: 'codex' | 'grok',
    ): Promise<IACPBackendCredentialStatus | null> {
        const response = await APIClient.delete<IACPBackendCredentialStatus>(
            `/ai-backends/acp-credentials/${provider}`,
        );
        if (response.type === 'error') {
            APIClient.showGenericError(response, '取り込んだ ACP 認証を削除できませんでした。');
            return null;
        }
        return response.data;
    }

    /**
     * 保存済み ACP 設定で接続試験を実行する。
     * サーバー hard limit + 回収余裕で待つ。
     */
    static async testACPConnection(
        backend_kind: ACPBackendKind,
        capability: AIBackendConnectionCapability = 'CandidateSelection',
    ): Promise<IACPBackendConnectionTestResult | null> {
        const response = await APIClient.post<IACPBackendConnectionTestResult>(
            '/ai-backends/acp/test',
            {backend_kind, capability},
            // ACP はサーバー hard limit + 回収余裕で待つ（60分 + 10分）。
            {timeout: 70 * 60 * 1000},
        );
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'ACP 接続試験を実行できませんでした。');
            return null;
        }
        return response.data;
    }
}
