
import { defineStore } from 'pinia';

import Version, { IVersionInformation } from '@/services/Version';
import Utils from '@/utils';


/**
 * バージョン情報を共有するストア
 */
const useVersionStore = defineStore('version', {
    state: () => ({

        // サーバーのバージョン情報
        server_version_info: null as IVersionInformation | null,

        // 最終更新日時 (UNIX タイムスタンプ、秒単位)
        last_updated_at: 0,

        // バージョン情報 API の直近の取得に失敗したかどうか
        // 以前の取得結果が残っていても、通信状態を確認できない間は実況機能を Fail Closed にするために使う
        is_server_version_fetch_failed: false,
    }),
    getters: {
        client_version(): string {
            // ビルド時埋め込み（フォールバック・リクエストヘッダ用）。UI 表示は display_version を使う
            return Utils.version;
        },
        // WebUI 表示用。サーバーが bs4k-v* タグから解決した version を優先する
        display_version(): string {
            return this.server_version ?? this.client_version;
        },
        client_git_commit(): string {
            return this.server_version_info?.git_commit ?? Utils.git_commit;
        },
        server_version(): string | null {
            return this.server_version_info?.version ?? null;
        },
        upstream_version(): string | null {
            return this.server_version_info?.upstream_version ?? null;
        },
        latest_version(): string | null {
            return this.server_version_info?.latest_version ?? null;
        },
        is_client_develop_version(): boolean {
            return this.display_version.includes('-dev');
        },
        is_server_develop_version(): boolean {
            return this.server_version?.includes('-dev') ?? false;
        },
        is_update_available(): boolean {
            // -dev / dirty などの接尾辞は無視し、ベース版番号 (例: 1.1.2) だけを比較する
            // 1.1.2 と 1.1.2-dev は同じ版として扱い、1.1.1 と 1.1.2 のように番号が違うときだけ更新ありとする
            if (this.server_version === null || this.latest_version === null) return false;
            const stripDevSuffix = (version: string): string => {
                return version.replace(/-dev(?:\.|$|\+).*$|-dev$/, '');
            };
            const server_base_version = stripDevSuffix(this.server_version);
            const latest_base_version = stripDevSuffix(this.latest_version);
            return server_base_version !== latest_base_version;
        },
        is_version_mismatch(): boolean {
            if (this.server_version === null) return false;
            return this.client_version !== this.server_version;
        },
        server_environment(): 'Linux' | 'Linux-Docker' | null {
            return this.server_version_info?.environment ?? null;
        },
        is_linux_environment(): boolean {
            const env = this.server_environment;
            return env === 'Linux' || env === 'Linux-Docker';
        },
        is_jikkyo_enabled_on_server(): boolean {
            // 未取得時・取得失敗時・旧サーバーからフィールドが返らない場合は、外部サービスへ接続しないよう必ず無効として扱う
            return this.is_server_version_fetch_failed === false && this.server_version_info?.jikkyo_enabled === true;
        }
    },
    actions: {

        /**
         * バージョン情報を取得する
         * すでに取得済みの情報がある場合は API リクエストを行わずにそれを返す
         * @param force 強制的に API リクエストを行う場合は true
         * @param signal 呼び出し元のライフサイクル終了時にストア更新を中断する AbortSignal
         * @returns バージョン情報 or バージョン情報の取得に失敗した場合は null
         */
        async fetchServerVersion(force: boolean = false, signal?: AbortSignal): Promise<IVersionInformation | null> {

            const isCallerActive = () => signal?.aborted !== true;
            if (isCallerActive() === false) {
                return null;
            }

            // バージョン情報がある場合はそれを返す
            // force が true の場合は無視される
            if (this.server_version_info !== null && force === false) {
                // ただし、最終更新日時が1分以上前の場合は非同期で更新する
                if (Utils.time() - this.last_updated_at > 60) {
                    this.fetchServerVersion(true);
                }
                return this.server_version_info;
            }

            // サーバーのバージョン情報を取得する
            // Store の force と Service の suppress_error は意味が異なる。
            // AbortSignal 起因の失敗は Service 側で抑止されるため、通常の通信失敗は従来どおり通知する。
            const version_info = await Version.fetchServerVersion(false, signal);
            if (isCallerActive() === false) {
                return null;
            }
            if (version_info === null) {
                this.is_server_version_fetch_failed = true;
                return null;
            }
            this.server_version_info = version_info;
            this.last_updated_at = Utils.time();
            this.is_server_version_fetch_failed = false;

            return this.server_version_info;
        },
    }
});

export default useVersionStore;
