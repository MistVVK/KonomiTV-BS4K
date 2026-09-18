import Message from '@/message';
import APIClient from '@/services/APIClient';


export interface IKonomiTVBS4KCloudCryptKeyInfo {
    key_present: boolean;
    backup_confirmed: boolean;
    legacy_migration_required: boolean;
}

export interface IKonomiTVBS4KCloudCryptKeySnapshot extends IKonomiTVBS4KCloudCryptKeyInfo {
    revision: string;
}

export default class KonomiTVBS4KCloudCryptKeys {
    static async request<T>(method: 'GET' | 'POST' | 'DELETE', path = '', data?: unknown, revision?: string): Promise<T | null> {
        const result = await APIClient.request<T>({method, url: `/konomitv-bs4k/cloud-crypt-keys${path}`, data,
            // 文字列の投入もJSONとして送信し、Axiosのフォーム形式への既定変換を避ける。
            headers: {'Content-Type': 'application/json', ...(revision === undefined ? {} : {'If-Match': `"${revision}"`})}});
        if (result.type === 'error') {
            // サーバー側も秘密を含まない固定エラーに変換する。AxiosError本体や入力値は渡さない。
            Message.error(typeof result.data.detail === 'string' ? result.data.detail :
                '暗号化鍵の形式が不正です。取り出した Base64 文字列が正しく入力されているか確認してください。');
            return null;
        }
        return result.data;
    }
}
