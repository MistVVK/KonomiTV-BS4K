
import { defineStore } from 'pinia';
import { ref, toRaw } from 'vue';

import Settings, { type IServerSettings, IServerSettingsDefault } from '@/services/Settings';


/**
 * サーバー設定を共有・キャッシュするストア
 */
const useServerSettingsStore = defineStore('serverSettings', () => {

    // 最後に取得、または保存に成功したサーバー設定の基準値
    // 設定画面はこの値を直接編集せず、画面ごとのローカルドラフトを作成して利用する
    const server_settings = ref<IServerSettings>(structuredClone(IServerSettingsDefault));

    // 読み込み状態
    const is_loaded = ref(false);
    const is_loading = ref(false);

    // 進行中の取得 Promise を保持
    let fetch_promise: Promise<IServerSettings | null> | null = null;


    /**
     * サーバー設定を一度だけ取得する
     * @returns 取得結果のサーバー設定、取得失敗時は null
     */
    async function fetchServerSettingsOnce(): Promise<IServerSettings | null> {
        if (is_loaded.value) {
            return server_settings.value;
        }

        if (fetch_promise !== null) {
            return await fetch_promise;
        }

        is_loading.value = true;
        fetch_promise = Settings.fetchServerSettings()
            .then((settings) => {
                if (settings !== null) {
                    server_settings.value = settings;
                    is_loaded.value = true;
                }
                return settings;
            })
            .finally(() => {
                is_loading.value = false;
                fetch_promise = null;
            });

        return await fetch_promise;
    }


    /**
     * 画面のローカルドラフトを正規化してサーバー設定を更新する
     *
     * 保存に失敗した場合は基準値を変更しない。保存に成功した場合だけ正規化済みの値を新しい基準値にすることで、
     * 再起動前の API が古い稼働中設定を返しても、次の section は直前に保存した内容を引き継げる。
     *
     * @param settings 更新する画面ローカルのサーバー設定ドラフト
     * @returns 更新に成功した場合は true
     */
    async function updateServerSettings(settings: IServerSettings): Promise<boolean> {

        // Vue の Reactive Proxy をストアの基準値へ持ち込まないよう、正規化前に独立した値へ複製する
        const normalized_settings = structuredClone(toRaw(settings));

        // モードと無関係な設定は送信前に明示的に初期化する
        if (normalized_settings.server.https_mode !== 'certificate') {
            normalized_settings.server.custom_https_certificate = null;
            normalized_settings.server.custom_https_private_key = null;
        }
        if (normalized_settings.server.https_mode !== 'reverse_proxy') {
            normalized_settings.server.reverse_proxy_listen_address = '0.0.0.0';
            normalized_settings.server.trusted_proxy_cidrs = [];
        } else {
            normalized_settings.server.trusted_proxy_cidrs = normalized_settings.server.trusted_proxy_cidrs
                .map(cidr => cidr.trim())
                .filter(cidr => cidr !== '');
        }

        // certificate モードの空文字列は null に変換し、サーバー側で必須対として検証する
        if (normalized_settings.server.custom_https_certificate === '') {
            normalized_settings.server.custom_https_certificate = null;
        }
        if (normalized_settings.server.custom_https_private_key === '') {
            normalized_settings.server.custom_https_private_key = null;
        }

        // 保存に成功した場合だけ、次の section が複製する基準値を更新する
        const result = await Settings.updateServerSettings(normalized_settings);
        if (result === true) {
            server_settings.value = normalized_settings;
            is_loaded.value = true;
        }
        return result;
    }


    return {
        server_settings,
        is_loaded,
        is_loading,
        fetchServerSettingsOnce,
        updateServerSettings,
    };
});

export default useServerSettingsStore;
