import Message from '@/message';
import APIClient from '@/services/APIClient';


export interface IKonomiTVBS4KCloudConnection {
    id: string;
    name: string;
    provider: 'GoogleDrive' | 'PCloud';
    region: 'US' | 'EU';
    status: 'Connected' | 'NeedsCheck' | 'Error';
    checked_at: string | null;
    expires_at: string | null;
}

export interface IKonomiTVBS4KCloudImport {
    name: string;
    provider: 'GoogleDrive' | 'PCloud';
    region: 'US' | 'EU';
    token_json: string;
    client_id: string;
    client_secret: string;
}

/** 秘密の取り込み入力を通知・ログ・永続設定に渡さないクラウド連携API。 */
export default class KonomiTVBS4KCloudStorage {
    static readonly base = '/konomitv-bs4k/cloud-storage';

    static async list(): Promise<IKonomiTVBS4KCloudConnection[] | null> {
        const result = await APIClient.get<IKonomiTVBS4KCloudConnection[]>(this.base);
        if (result.type === 'error') {
            Message.error('クラウド接続一覧を取得できませんでした。管理者でログインしてください。');
            return null;
        }
        return result.data;
    }

    static async importKey(data: IKonomiTVBS4KCloudImport, id: string | null): Promise<boolean> {
        const result = await APIClient.request<IKonomiTVBS4KCloudConnection>({
            method: id === null ? 'POST' : 'PUT',
            url: id === null ? this.base : `${this.base}/${id}`,
            data,
        });
        if (result.type === 'error') {
            Message.error('キーを取り込めませんでした。JSON・期限・リージョンとネットワークを確認してください。');
            return false;
        }
        return true;
    }

    static async check(id: string): Promise<string[] | null> {
        const result = await APIClient.post<string[]>(`${this.base}/${id}/check`);
        if (result.type === 'error') {
            Message.error('接続を確認できませんでした。時間をおいて再試行し、必要ならキーを再取り込みしてください。');
            return null;
        }
        return result.data;
    }

    static async disconnect(id: string): Promise<boolean> {
        const result = await APIClient.delete(`${this.base}/${id}`);
        if (result.type === 'error') {
            Message.error('接続を解除できませんでした。');
            return false;
        }
        return true;
    }
}
