
import type { ServerEncoder } from '@/services/Settings';

import APIClient from '@/services/APIClient';




/** バージョン情報を表すインターフェイス */
export interface IVersionInformation {
    version: string;
    upstream_version: string;
    git_commit: string;
    latest_version: string | null;
    environment: 'Linux' | 'Linux-Docker';
    backend: 'EDCB' | 'Mirakurun';
    encoder: ServerEncoder;
    // フルの /settings/server はホストパスを含むため、視聴経路向けの非機密 runtime 情報をここに含める
    encoder_bs4k: ServerEncoder;
    bs4k_ignore_viewer_low_latency: boolean;
    jikkyo_enabled: boolean;
}


class Version {

    /**
     * バージョン情報を取得する
     * @param suppress_error エラーメッセージを表示しない場合は true
     * @param signal 呼び出し元のライフサイクル終了時にリクエストを中断する AbortSignal
     * @returns バージョン情報 or バージョン情報の取得に失敗した場合は null
     */
    static async fetchServerVersion(
        suppress_error: boolean = false,
        signal?: AbortSignal,
    ): Promise<IVersionInformation | null> {

        // API リクエストを実行
        const response = await APIClient.get<IVersionInformation>('/version', {signal});

        // エラー処理
        if (response.type === 'error') {
            if (suppress_error === false && signal?.aborted !== true) {
                APIClient.showGenericError(response, 'バージョン情報を取得できませんでした。');
            }
            return null;
        }

        return response.data;
    }
}

export default Version;
