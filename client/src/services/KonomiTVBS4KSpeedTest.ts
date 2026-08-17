import type {
    IKonomiTVBS4KSpeedTestQualityThreshold,
} from '@/utils/KonomiTVBS4KSpeedTestWorker';

import APIClient from '@/services/APIClient';


export interface IKonomiTVBS4KSpeedTestLimits {
    download_streams: number;
    upload_streams: number;
    download_seconds: number;
    upload_seconds: number;
}

export interface IKonomiTVBS4KSpeedTestSession {
    expires_in_seconds: number;
    limits: IKonomiTVBS4KSpeedTestLimits;
    quality_thresholds: IKonomiTVBS4KSpeedTestQualityThreshold[];
}


class KonomiTVBS4KSpeedTest {

    /**
     * 測定枠を確保し、推奨画質閾値を受け取る。
     * @returns session 応答。失敗時は null
     */
    static async createSession(): Promise<IKonomiTVBS4KSpeedTestSession | null> {
        const response = await APIClient.post<IKonomiTVBS4KSpeedTestSession>(
            '/konomitv-bs4k/speed-test/session',
        );
        if (response.type === 'error') {
            if (response.status === 429) {
                APIClient.showGenericError(response, '別の測定が進行中のため、少し待ってからやり直してください。');
                return null;
            }
            APIClient.showGenericError(response, 'サーバー接続速度の測定を開始できませんでした。');
            return null;
        }
        return response.data;
    }

    /**
     * 測定枠を解放する。短命 Cookie は次の session 作成時に上書きされる。
     */
    static async deleteSession(): Promise<void> {
        const response = await APIClient.delete('/konomitv-bs4k/speed-test/session');
        if (response.type === 'error' && response.status !== 401) {
            APIClient.showGenericError(response, '速度測定セッションを終了できませんでした。');
        }
    }
}

export default KonomiTVBS4KSpeedTest;
