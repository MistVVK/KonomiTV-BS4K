import APIClient from '@/services/APIClient';


export interface ICMAnalysisSettings {
    enabled: boolean;
    logo_directory: string | null;
    excluded_directories: string[];
}

export interface ICMAnalysisCapabilities {
    automatic_logo_generation: 'Unavailable';
}

export interface ICMLogo {
    id: number;
    path: string;
    filename: string;
    service_id: number | null;
    logo_name: string;
    file_format: 'AviUtlV0.1' | 'AmatsukazeExtendedV1';
    enabled: boolean;
    file_hash: string;
    file_size: number;
    generated_from_recorded_video_id: number | null;
    last_used_at: string | null;
    missing: boolean;
    deleted_at: string | null;
    created_at: string;
    updated_at: string;
}

export interface ICMLogoServiceAssignment {
    id: number;
    logo_id: number | null;
    network_id: number;
    transport_stream_id: number;
    service_id: number;
    enabled: boolean;
    is_no_logo: boolean;
    valid_from: string | null;
    valid_until: string | null;
    created_at: string;
    updated_at: string;
}

export default class CMAnalysis {

    static async fetchCapabilities(): Promise<ICMAnalysisCapabilities | null> {
        const response = await APIClient.get<ICMAnalysisCapabilities>('/cm-analysis/capabilities');
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'CM 解析機能の対応状況を取得できませんでした。');
            return null;
        }
        return response.data;
    }

    static async fetchSettings(): Promise<ICMAnalysisSettings | null> {
        const response = await APIClient.get<ICMAnalysisSettings>('/cm-analysis/settings');
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'CM 解析設定を取得できませんでした。');
            return null;
        }
        return response.data;
    }

    static async updateSettings(settings: ICMAnalysisSettings): Promise<boolean> {
        const response = await APIClient.put('/cm-analysis/settings', settings);
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'CM 解析設定を更新できませんでした。');
            return false;
        }
        return true;
    }

    static async fetchLogos(rescan = false): Promise<ICMLogo[] | null> {
        const response = rescan
            ? await APIClient.post<ICMLogo[]>('/cm-analysis/logos/rescan')
            : await APIClient.get<ICMLogo[]>('/cm-analysis/logos');
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'CM ロゴを取得できませんでした。');
            return null;
        }
        return response.data;
    }

    static async updateLogo(logo_id: number, enabled: boolean): Promise<boolean> {
        const response = await APIClient.put(`/cm-analysis/logos/${logo_id}`, {enabled});
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'CM ロゴ設定を更新できませんでした。');
            return false;
        }
        return true;
    }

    static async uploadLogo(file: File): Promise<ICMLogo | null> {
        const form_data = new FormData();
        form_data.append('logo_file', file);
        const response = await APIClient.post<ICMLogo>('/cm-analysis/logos', form_data, {
            headers: {'Content-Type': 'multipart/form-data'},
        });
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'CM ロゴをアップロードできませんでした。');
            return null;
        }
        return response.data;
    }

    static async deleteLogo(logo_id: number): Promise<boolean> {
        const response = await APIClient.delete(`/cm-analysis/logos/${logo_id}`, {
            params: {confirm_shared_deletion: true},
        });
        if (response.type === 'error') {
            APIClient.showGenericError(response, '共有 CM ロゴを削除できませんでした。');
            return false;
        }
        return true;
    }

    static async fetchAssignments(): Promise<ICMLogoServiceAssignment[] | null> {
        const response = await APIClient.get<ICMLogoServiceAssignment[]>('/cm-analysis/logo-assignments');
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'CM ロゴのサービス割り当てを取得できませんでした。');
            return null;
        }
        return response.data;
    }

    static async createAssignment(
        assignment: Omit<ICMLogoServiceAssignment, 'id' | 'created_at' | 'updated_at'>,
    ): Promise<ICMLogoServiceAssignment | null> {
        const response = await APIClient.post<ICMLogoServiceAssignment>('/cm-analysis/logo-assignments', assignment);
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'CM ロゴのサービス割り当てを追加できませんでした。');
            return null;
        }
        return response.data;
    }

    static async deleteAssignment(assignment_id: number): Promise<boolean> {
        const response = await APIClient.delete(`/cm-analysis/logo-assignments/${assignment_id}`);
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'CM ロゴのサービス割り当てを削除できませんでした。');
            return false;
        }
        return true;
    }
}
