import APIClient from '@/services/APIClient';


export interface IKonomiTVBS4KCloudTransfer {
    id: string;
    recorded_video_id: number;
    direction: 'ToCloud' | 'ToLocal';
    status: 'Pending' | 'Running' | 'Failed' | 'Completed' | 'Cancelled';
    phase: 'Preparing' | 'Copying' | 'Verifying' | 'Publishing' | 'Cleaning' | 'Completed' | 'Cancelling' | 'Cancelled';
    cancel_requested: boolean;
    attempt: number;
    error_code: string | null;
    created_at: string;
    updated_at: string;
}

export interface IKonomiTVBS4KCloudTransferRequest {
    request_id: string;
    recorded_program_id: number;
    direction: IKonomiTVBS4KCloudTransfer['direction'];
    key_backup_confirmed: boolean;
    key_revision: string;
    restore_folder: string | null;
}

export interface IKonomiTVBS4KCloudCatalogSync {
    id: string;
    connection_id: string;
    connection_name: string;
    folder: string;
    status: 'Pending' | 'Running' | 'Completed' | 'Failed';
    error_code: string | null;
    updated_at: string;
}

/** 通常の移動APIへ鍵・OAuth・内部目録を渡さない。要求UUIDは再送でも維持する。 */
export default class KonomiTVBS4KCloudTransfers {
    static readonly base = '/konomitv-bs4k/cloud-storage/transfers';

    static async list() {
        return await APIClient.get<IKonomiTVBS4KCloudTransfer[]>(this.base);
    }

    static async restoreFolders() {
        return await APIClient.get<string[]>(`${this.base}/restore-folders`);
    }

    static async create(request: IKonomiTVBS4KCloudTransferRequest) {
        return await APIClient.post<IKonomiTVBS4KCloudTransfer>(this.base, request);
    }

    static async cancel(id: string) {
        return await APIClient.post<IKonomiTVBS4KCloudTransfer>(`${this.base}/${id}/cancel`);
    }

    static async retry(id: string) {
        return await APIClient.post<IKonomiTVBS4KCloudTransfer>(`${this.base}/${id}/retry`);
    }

    static async syncStatus() {
        return await APIClient.get<IKonomiTVBS4KCloudCatalogSync[]>(`${this.base}/catalog-sync`);
    }

    static async sync() {
        return await APIClient.post<IKonomiTVBS4KCloudCatalogSync[]>(`${this.base}/catalog-sync`);
    }
}
