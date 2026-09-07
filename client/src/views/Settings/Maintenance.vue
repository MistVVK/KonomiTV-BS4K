<template>
    <component :is="embedded ? 'div' : SettingsBase">
        <h2 class="settings__heading" v-if="embedded === false">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:wrench-settings-20-filled" width="22px" />
            <span class="ml-2">{{section_title}}</span>
        </h2>
        <div class="settings__description" v-if="embedded === false">
            KonomiTV-BS4K サーバーの保守操作を実行します。管理者アカウントでログインしている必要があります。<br>
        </div>

        <div class="settings__content" v-if="isSectionVisible('logs')"
            :class="{'settings__content--disabled': is_disabled}">
            <div class="settings__content-heading">
                <Icon icon="fluent:document-text-16-regular" width="22px" />
                <span class="ml-2">ログ</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">サーバーログの表示</div>
                <div class="settings__item-label">
                    KonomiTV-BS4K サーバーの動作ログとアクセスログをリアルタイムで表示します。<br>
                    サーバーの動作状況の確認やトラブルシューティングに役立ちます。<br>
                </div>
            </div>
            <v-btn class="settings__save-button mt-5" color="background-lighten-2" variant="flat"
                @click="server_log_dialog = !server_log_dialog">
                <Icon icon="fluent:document-text-16-regular" height="20px" />
                <span class="ml-2">サーバーログを表示</span>
            </v-btn>
        </div>

        <div class="settings__content" v-if="isSectionVisible('database')"
            :class="{'settings__content--disabled': is_disabled}">
            <div class="settings__content-heading">
                <Icon icon="iconoir:database-backup" width="22px" />
                <span class="ml-2">DB・録画</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">KonomiTV-BS4K のデータベースを更新</div>
                <div class="settings__item-label">
                    KonomiTV-BS4K のデータベースに保存されている、チャンネル情報・番組情報・Twitter アカウント情報などの外部 API に依存するデータをすべて更新します。<br>
                    即座に外部 API からのデータ更新を反映させたいときに利用してください。<br>
                </div>
            </div>
            <v-btn class="settings__save-button mt-5" color="background-lighten-2" variant="flat"
                @click="updateDatabase()">
                <Icon icon="iconoir:database-backup" height="20px" />
                <span class="ml-2">データベースを更新</span>
            </v-btn>
            <div class="settings__item">
                <div class="settings__item-heading">録画フォルダの一括スキャンを手動実行</div>
                <div class="settings__item-label">
                    録画フォルダ内のファイルは、通常 KonomiTV-BS4K サーバーの起動時に自動的にスキャンされます。<br>
                    録画ファイルが KonomiTV-BS4K に正しく反映されていない場合にのみ実行してみてください。<br>
                </div>
                <div class="settings__item-label mt-1">
                    <strong>大量の録画ファイルが保存されている環境では、処理完了まで数時間〜数日以上かかることがあります。</strong><br>
                </div>
            </div>
            <v-btn class="settings__save-button mt-5" color="background-lighten-2" variant="flat"
                @click="runBatchScan()">
                <Icon icon="fluent:folder-sync-20-regular" height="20px" />
                <span class="ml-2">録画フォルダの一括スキャンを手動実行</span>
            </v-btn>
        </div>

        <div class="settings__content" v-if="isSectionVisible('analysis')"
            :class="{'settings__content--disabled': is_disabled}">
            <div class="settings__content-heading">
                <Icon icon="fluent:scan-dash-20-regular" width="22px" />
                <span class="ml-2">解析</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">すべての録画ファイルのメタデータを再解析</div>
                <div class="settings__item-label">
                    KonomiTV-BS4K に登録されているすべての録画ファイルのメタデータを強制的に再解析します。<br>
                    メタデータの解析方法が変更された後に、既存の録画ファイルにも新しい解析結果を反映したい場合に利用してください。<br>
                </div>
                <div class="settings__item-label mt-1">
                    <strong>すべての録画ファイルを読み込むため、処理完了まで数時間〜数日以上かかることがあります。</strong><br>
                </div>
            </div>
            <v-btn class="settings__save-button mt-5" color="background-lighten-2" variant="flat"
                @click="reanalyzeAllRecordedVideos()">
                <Icon icon="fluent:video-clip-20-filled" height="20px" />
                <span class="ml-2">すべての録画ファイルを再解析</span>
            </v-btn>
            <div class="settings__item">
                <div class="settings__item-heading">すべての録画ファイルの CM 区間を再判定</div>
                <div class="settings__item-label">
                    KonomiTV-BS4K に登録されているすべての録画ファイルについて、既存結果を上書きして CM 区間を再判定します。<br>
                    CM 判定方法が変更された後に、既存の録画ファイルにも新しい判定結果を反映したい場合に利用してください。<br>
                </div>
                <div class="settings__item-label mt-1">
                    <strong>すべての録画ファイルを読み込むため、処理完了まで数時間〜数日以上かかることがあります。</strong><br>
                </div>
            </div>
            <v-btn class="settings__save-button mt-5" color="background-lighten-2" variant="flat"
                @click="detectCMSectionsForAllRecordedVideos()">
                <Icon icon="fluent:scan-dash-20-regular" height="20px" />
                <span class="ml-2">すべての CM 区間を再判定</span>
            </v-btn>
            <div class="settings__item">
                <div class="settings__item-heading">録画ファイルのバックグラウンド解析タスクを再実行</div>
                <div class="settings__item-label">
                    録画ファイルのメタデータ解析やサムネイル作成が完了していない場合に、これらの処理を再度実行します。<br>
                    PC のシャットダウンなどで途中で中断してしまった場合は、このボタンから処理を再開できます。<br>
                </div>
                <div class="settings__item-label mt-1">
                    <strong>大量の録画ファイルが保存されている環境では、処理完了まで数時間〜数日以上かかることがあります。</strong><br>
                </div>
            </div>
            <v-btn class="settings__save-button mt-5" color="background-lighten-2" variant="flat"
                @click="startBackgroundAnalysis()">
                <Icon icon="fluent:book-arrow-clockwise-20-regular" height="20px" />
                <span class="ml-2">バックグラウンド解析タスクを再実行</span>
            </v-btn>
            <div class="settings__item">
                <div class="settings__item-heading">共有 CM ロゴを再スキャン</div>
                <div class="settings__item-label">
                    共有ロゴフォルダを再スキャンし、追加・変更・削除された .lgd ファイルを KonomiTV-BS4K に反映します。<br>
                </div>
            </div>
            <v-btn class="settings__save-button mt-5" color="background-lighten-2" variant="flat"
                @click="rescanCMLogos()">
                <Icon icon="fluent:image-sync-20-filled" height="20px" />
                <span class="ml-2">共有 CM ロゴを再スキャン</span>
            </v-btn>
        </div>

        <div class="settings__content" v-if="isSectionVisible('series')"
            :class="{'settings__content--disabled': is_disabled}">
            <div class="settings__content-heading">
                <Icon icon="fluent:movies-and-tv-20-filled" width="22px" />
                <span class="ml-2">シリーズ</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading text-error-readable">シリーズデータベースを削除</div>
                <div class="settings__item-label">
                    シリーズ・話数・AI 補完の判定データだけをすべて削除します。<br>
                    録画本体・サムネイル・CM 解析・視聴履歴は残り、番組のシリーズ関連付けだけ外れます。<br>
                    削除後に下の「既存録画へ Indexer を再適用」を実行してシリーズを再構築してください。<br>
                    シリーズ AI 補完バッチ・話数一括判定の実行中は削除できません。<br>
                </div>
            </div>
            <v-btn class="settings__save-button bg-error mt-5" variant="flat"
                :loading="is_deleting_series_database"
                :disabled="is_series_action_running"
                @click="startSeriesDatabaseDelete()">
                <Icon icon="fluent:delete-16-filled" height="20px" />
                <span class="ml-2">シリーズデータベースを削除</span>
            </v-btn>
            <div class="settings__item">
                <div class="settings__item-heading">既存録画へ Indexer を再適用</div>
                <div class="settings__item-label">
                    保存済みの全録画へ、HonomiTV と同じ確定規則を再適用します。<br>
                    AI が有効なら、付かなかった同一 EPG タイトル群をバックグラウンドで補完します。<br>
                    付かなければ所属を外します。自動判定が無効のときは実行できません。<br>
                </div>
                <div v-if="series_backfill_unavailable_message !== null"
                    class="settings__item-label mt-2 text-warning">
                    {{series_backfill_unavailable_message}}
                </div>
            </div>
            <div class="settings__item">
                <v-btn class="settings__save-button mt-4" color="background-lighten-2" variant="flat"
                    :loading="is_starting_backfill"
                    :disabled="is_series_action_running ||
                        is_series_backfill_available === false"
                    @click="startBackfill()">
                    <Icon icon="fluent:arrow-sync-20-filled" class="mr-2" width="22px" />
                    既存録画へ規則を再適用
                </v-btn>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">既存録画の一括話数判定</div>
                <div class="settings__item-label">
                    Series 所属済みで、話数が未処理・ローカル判定で不明・移行データで要確認の既存録画を、<br>
                    保存済みの AI 設定で順番に Web 検索します。<br>
                    Indexer が単一の正整数を取れた録画は検索しません。<br>
                </div>
            </div>
            <div class="settings__item">
                <v-progress-linear v-if="is_episode_backfill_running" class="mt-4" color="primary" height="7" rounded
                    :indeterminate="episode_backfill_progress === null"
                    :model-value="episode_backfill_progress ?? undefined" />
                <div v-if="episode_backfill_task !== null" class="settings__item-label mt-2">
                    {{stageLabel(episode_backfill_task.stage)}}
                    <template v-if="episode_backfill_progress !== null">
                        ・{{episode_backfill_progress.toFixed(0)}}%
                    </template>
                </div>
                <v-btn class="settings__save-button mt-4" color="background-lighten-2" variant="flat"
                    :loading="is_starting_episode_backfill"
                    :disabled="is_series_action_running"
                    @click="startEpisodeBackfill()">
                    <Icon icon="fluent:globe-search-20-filled" class="mr-2" width="22px" />
                    既存録画の話数判定を開始
                </v-btn>
            </div>
        </div>

        <div class="settings__content" v-if="isSectionVisible('server')"
            :class="{'settings__content--disabled': is_disabled}">
            <div class="settings__content-heading">
                <Icon icon="fluent:power-20-filled" width="22px" />
                <span class="ml-2">サーバー操作</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading text-error-readable">KonomiTV-BS4K サーバーを再起動</div>
                <div class="settings__item-label">
                    KonomiTV-BS4K サーバーを再起動します。サーバー設定の変更を反映するには再起動が必要です。<br>
                    <strong>再起動を実行すると、すべての視聴中セッションが切断されます。</strong>十分注意してください。<br>
                </div>
            </div>
            <v-btn class="settings__save-button bg-error mt-5" variant="flat" @click="restartServer()">
                <Icon icon="fluent:arrow-counterclockwise-20-filled" height="20px" />
                <span class="ml-2">KonomiTV-BS4K サーバーを再起動</span>
            </v-btn>
            <div class="settings__item">
                <div class="settings__item-heading text-error-readable">KonomiTV-BS4K サーバーをシャットダウン</div>
                <div class="settings__item-label">
                    KonomiTV-BS4K サーバーをシャットダウンします。<br>
                    <strong>シャットダウンを実行すると、再度手動で KonomiTV-BS4K サーバーを起動するまで KonomiTV-BS4K にアクセスできなくなります。</strong>十分注意してください。<br>
                </div>
                <div class="settings__item-label mt-1">
                    なお、Linux 版 KonomiTV-BS4K サーバーはプロセス管理を PM2 / Docker に委譲しているため、シャットダウン後は自動で再起動されます。完全にシャットダウンするには、PM2 / Docker 側でサービスを停止してください。<br>
                </div>
            </div>
            <v-btn class="settings__save-button bg-error mt-5" variant="flat" @click="shutdownServer()">
                <Icon icon="fluent:power-20-filled" height="20px" />
                <span class="ml-2">KonomiTV-BS4K サーバーをシャットダウン</span>
            </v-btn>
        </div>

        <v-dialog :model-value="series_confirmation_dialog" :persistent="is_starting_selected_series_action"
            max-width="560" @update:model-value="updateSeriesConfirmationDialog">
            <v-card>
                <v-card-title>{{series_confirmation_title}}</v-card-title>
                <v-card-text>{{series_confirmation_message}}</v-card-text>
                <v-card-actions>
                    <v-spacer />
                    <v-btn variant="text" :disabled="is_starting_selected_series_action"
                        @click="updateSeriesConfirmationDialog(false)">キャンセル</v-btn>
                    <v-btn :color="series_confirmation_action === 'DeleteDatabase' ? 'error' : 'primary'"
                        variant="flat" :loading="is_starting_selected_series_action"
                        :disabled="series_confirmation_action === null" @click="confirmSeriesAction()">
                        {{series_confirmation_action === 'DeleteDatabase' ? '削除を実行' : '判定を開始'}}
                    </v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>

        <ServerLogDialog :modelValue="server_log_dialog" @update:modelValue="server_log_dialog = $event" />
    </component>
</template>

<script lang="ts" setup>

import { computed, onMounted, onUnmounted, ref } from 'vue';

import ServerLogDialog from '@/components/Settings/ServerLogDialog.vue';
import Message from '@/message';
import AnalysisTasks, { type IAnalysisTaskExecution } from '@/services/AnalysisTasks';
import CMAnalysis from '@/services/CMAnalysis';
import Maintenance from '@/services/Maintenance';
import RecordedSeries, { type IRecordedSeriesStatus } from '@/services/RecordedSeries';
import Version from '@/services/Version';
import { stageLabel } from '@/stores/AnalysisTasksStore';
import useUserStore from '@/stores/UserStore';
import Utils from '@/utils';
import SettingsBase from '@/views/Settings/Base.vue';

type MaintenanceSection = 'maintenance' | 'logs' | 'database' | 'analysis' | 'series' | 'server' | 'all';

const props = withDefaults(defineProps<{
    section?: MaintenanceSection;
    embedded?: boolean;
}>(), {
    section: 'all',
    embedded: false,
});

const embedded = computed(() => props.embedded);
const section_title = computed(() => ({
    maintenance: '診断・データ保守',
    logs: 'ログ',
    database: 'DB・録画',
    analysis: '解析',
    series: 'シリーズ',
    server: 'サーバー操作',
    all: 'メンテナンス',
})[props.section]);

function isSectionVisible(target_section: Exclude<MaintenanceSection, 'all'>): boolean {
    if (props.section === 'all' || props.section === target_section) {
        return true;
    }
    return props.section === 'maintenance' && ['logs', 'database', 'analysis', 'series'].includes(target_section);
}

// 管理者権限が確認できるまでは、保守操作を無効化する
const is_disabled = ref(true);
const user_store = useUserStore();
user_store.fetchUser().then((user) => {
    if (user && user.is_admin) {
        is_disabled.value = false;
    }
});

// サーバーログダイアログの表示状態
const server_log_dialog = ref(false);

// シリーズ欄の状態。RecordedSeries 画面から移設した一括操作と DB 削除を束ねる。
const series_indexing_enabled = ref<boolean | null>(null);
const is_deleting_series_database = ref(false);
const is_starting_backfill = ref(false);
const is_starting_episode_backfill = ref(false);
const is_monitoring_episode_backfill = ref(false);
const is_refreshing_series_status = ref(false);
const series_status = ref<IRecordedSeriesStatus | null>(null);
const episode_backfill_task = ref<IAnalysisTaskExecution | null>(null);

type SeriesConfirmationAction = 'Series' | 'Episode' | 'DeleteDatabase';
const series_confirmation_dialog = ref(false);
const series_confirmation_action = ref<SeriesConfirmationAction | null>(null);
const series_confirmation_message = ref('');

let episode_backfill_abort_controller: AbortController | null = null;
let series_status_polling_timer: number | null = null;

const is_backfill_running = computed(() =>
    is_starting_backfill.value || series_status.value?.is_running === true,
);
const is_episode_backfill_running = computed(() =>
    is_starting_episode_backfill.value ||
    is_monitoring_episode_backfill.value ||
    series_status.value?.is_episode_running === true,
);
const is_series_action_running = computed(() =>
    is_deleting_series_database.value ||
    is_backfill_running.value ||
    is_episode_backfill_running.value,
);
const is_starting_selected_series_action = computed(() => {
    if (series_confirmation_action.value === 'Series') return is_starting_backfill.value;
    if (series_confirmation_action.value === 'Episode') return is_starting_episode_backfill.value;
    if (series_confirmation_action.value === 'DeleteDatabase') return is_deleting_series_database.value;
    return false;
});
const series_confirmation_title = computed(() =>
    series_confirmation_action.value === 'Episode' ?
        '既存録画の一括話数判定' :
        series_confirmation_action.value === 'DeleteDatabase' ?
            'シリーズデータベースを削除' :
            '既存録画へ Indexer を再適用',
);
const series_backfill_unavailable_message = computed(() => {
    if (series_indexing_enabled.value === false) {
        return '「新しい録画を自動でシリーズ判定する」が無効なため、既存録画への再適用を開始できません。';
    }
    return null;
});
const is_series_backfill_available = computed(() => series_backfill_unavailable_message.value === null);
const episode_backfill_progress = computed(() => {
    if (episode_backfill_task.value?.progress === null || episode_backfill_task.value?.progress === undefined) {
        return null;
    }
    return Math.max(0, Math.min(100, episode_backfill_task.value.progress * 100));
});

// データベースを更新する関数
async function updateDatabase() {
    Message.show('データベースを更新しています...');
    await Maintenance.updateDatabase();
    Message.success('データベースを更新しました。');
}

// 録画フォルダの一括スキャンを実行する関数
async function runBatchScan() {
    Message.info(
        '録画フォルダの一括スキャンを開始しています...\n' +
        '大量の録画ファイルが保存されている環境では、処理完了まで数時間〜数日以上かかることがあります。'
    );
    const result = await Maintenance.runBatchScan();
    if (result === true) {
        Message.success(
            '録画フォルダの一括スキャンが完了しました。\n' +
            'すべての録画ファイルがデータベースに同期されているはずです。'
        );
    }
}

// すべての録画ファイルのメタデータを再解析する関数
async function reanalyzeAllRecordedVideos() {
    Message.info(
        'すべての録画ファイルのメタデータ再解析を開始しています...\n' +
        '大量の録画ファイルが保存されている環境では、処理完了まで数時間〜数日以上かかることがあります。'
    );
    const result = await Maintenance.reanalyzeAllRecordedVideos();
    if (result === true) {
        Message.success('すべての録画ファイルのメタデータ再解析が完了しました。');
    }
}

// すべての録画ファイルの CM 区間を再判定する関数
async function detectCMSectionsForAllRecordedVideos() {
    Message.info(
        'すべての録画ファイルの CM 区間判定を開始しています...\n' +
        '大量の録画ファイルが保存されている環境では、処理完了まで数時間〜数日以上かかることがあります。'
    );
    const result = await Maintenance.detectCMSectionsForAllRecordedVideos();
    if (result === true) {
        Message.success('すべての録画ファイルの CM 区間判定が完了しました。');
    }
}

// バックグラウンド解析タスクを開始する関数
async function startBackgroundAnalysis() {
    Message.info(
        'バックグラウンド解析タスクを開始しています...\n' +
        '大量の録画ファイルが保存されている環境では、処理完了まで数時間〜数日以上かかることがあります。'
    );
    const result = await Maintenance.startBackgroundAnalysis();
    if (result === true) {
        Message.success(
            'バックグラウンド解析タスクの実行が完了しました。\n' +
            'すべての録画番組のメタデータ解析/サムネイル生成が完了しているはずです。'
        );
    }
}

// 共有 CM ロゴフォルダを再スキャンする関数
async function rescanCMLogos() {
    Message.show('共有 CM ロゴを再スキャンしています...');
    const logos = await CMAnalysis.fetchLogos(true);
    if (logos !== null) {
        Message.success(`共有 CM ロゴを再スキャンしました。（${logos.length} 件）`);
    }
}

/** シリーズ判定状況を更新し、一括判定の実行が無ければ定期取得を止める。 */
async function refreshSeriesStatus(show_error = false): Promise<void> {
    if (is_refreshing_series_status.value) return;
    is_refreshing_series_status.value = true;
    const fetched_status = await RecordedSeries.fetchStatus(show_error);
    is_refreshing_series_status.value = false;
    if (fetched_status === null) return;

    series_status.value = fetched_status;
    if (
        fetched_status.is_running === false &&
        fetched_status.is_episode_running === false
    ) {
        // 完了・中断を polling で検知したら、監視表示を通常状態へ戻す。
        is_monitoring_episode_backfill.value = false;
        stopSeriesStatusPolling();
    }
}

/** 実行中の一括判定を、画面再読み込み後も状況 API から追跡する。 */
function startSeriesStatusPolling(): void {
    if (series_status_polling_timer !== null) return;
    series_status_polling_timer = window.setInterval(() => void refreshSeriesStatus(), 3000);
}

function stopSeriesStatusPolling(): void {
    if (series_status_polling_timer === null) return;
    window.clearInterval(series_status_polling_timer);
    series_status_polling_timer = null;
}

/** シリーズデータベースを削除する前に、削除範囲を確認する。 */
function startSeriesDatabaseDelete(): void {
    if (is_series_action_running.value) return;
    series_confirmation_action.value = 'DeleteDatabase';
    series_confirmation_message.value =
        'シリーズ・話数・AI 補完の判定データをすべて削除します。録画本体・サムネイル・CM 解析・視聴履歴は残ります。削除後に「既存録画へ Indexer を再適用」を実行してください。続行しますか？';
    series_confirmation_dialog.value = true;
}

/** シリーズデータベースを削除する。完了後に Indexer 再適用を促す。 */
async function runSeriesDatabaseDelete(): Promise<void> {
    is_deleting_series_database.value = true;
    const result = await RecordedSeries.deleteSeriesDatabase();
    is_deleting_series_database.value = false;
    if (result === null) return;
    updateSeriesConfirmationDialog(false);
    await refreshSeriesStatus();
    Message.success(
        `シリーズデータベースを削除しました。（シリーズ ${result.series.toLocaleString()} 件）\n` +
        '「既存録画へ Indexer を再適用」を実行してシリーズを再構築してください。',
    );
}

/** 既存録画へ Indexer を再適用する前に、範囲を確認する。 */
function startBackfill(): void {
    if (is_series_action_running.value) return;
    if (is_series_backfill_available.value === false) {
        Message.warning('自動判定を有効にしてから既存録画へ規則を再適用してください。');
        return;
    }
    series_confirmation_action.value = 'Series';
    series_confirmation_message.value =
        'HonomiTV と同じ確定規則を保存済みの全録画へ再適用します。AI が有効なら、付かなかった同一 EPG タイトル群をバックグラウンドで Web 検索します。続行しますか？';
    series_confirmation_dialog.value = true;
}

/** 確認ダイアログを閉じ、次に開いた操作へ古い対象を持ち越さない。 */
function updateSeriesConfirmationDialog(value: boolean): void {
    if (value === false && is_starting_selected_series_action.value) return;
    series_confirmation_dialog.value = value;
    if (value === false) {
        series_confirmation_action.value = null;
        series_confirmation_message.value = '';
    }
}

/** 確認時に固定した操作種別で、対応するシリーズ操作を開始する。 */
async function confirmSeriesAction(): Promise<void> {
    const action = series_confirmation_action.value;
    if (action === null || is_starting_selected_series_action.value) return;

    if (action === 'Series') {
        await runBackfill();
    } else if (action === 'Episode') {
        await runEpisodeBackfill();
    } else {
        await runSeriesDatabaseDelete();
    }
}

/** 既存録画へ Indexer の確定規則を再適用する。完了まで API 応答を待つ。 */
async function runBackfill(): Promise<void> {

    is_starting_backfill.value = true;
    const accepted = await RecordedSeries.startBackfill(false);
    is_starting_backfill.value = false;
    if (accepted === null) return;
    updateSeriesConfirmationDialog(false);
    await refreshSeriesStatus();
    Message.success('既存録画へ Indexer の規則を再適用しました。');
}

/** 既存録画の一括話数判定を開始する前に、利用量と検索範囲を確認する。 */
function startEpisodeBackfill(): void {
    if (is_series_action_running.value) return;
    series_confirmation_action.value = 'Episode';
    series_confirmation_message.value =
        '話数が未確定の既存録画を、保存済みの AI 設定で Web 検索します。Indexer が単一の正整数を取れた録画は検索しません。続行しますか？';
    series_confirmation_dialog.value = true;
}

/** Series所属済みの既存録画を対象に、バックグラウンドで話数Web検索を実行する。 */
async function runEpisodeBackfill(): Promise<void> {

    is_starting_episode_backfill.value = true;
    const accepted = await RecordedSeries.startEpisodeBackfill(false);
    is_starting_episode_backfill.value = false;
    if (accepted === null) return;
    updateSeriesConfirmationDialog(false);
    series_status.value = series_status.value === null ? null : {...series_status.value, is_episode_running: true};
    is_monitoring_episode_backfill.value = true;
    startSeriesStatusPolling();
    Message.info(
        accepted.reused ?
            '実行中の一括話数判定を引き続き監視します。' :
            '既存録画の一括話数判定を開始しました。',
    );

    // シリーズ一括判定とは別の履歴IDを追跡し、進捗と完了通知を混同しない。
    episode_backfill_abort_controller?.abort();
    episode_backfill_abort_controller = new AbortController();
    const execution = await AnalysisTasks.waitForCompletion(
        accepted.execution_id,
        episode_backfill_abort_controller.signal,
        task => episode_backfill_task.value = task,
    );
    if (episode_backfill_abort_controller.signal.aborted) return;

    is_monitoring_episode_backfill.value = false;
    await refreshSeriesStatus();
    if (execution?.status === 'Succeeded') {
        Message.success('既存録画の一括話数判定が完了しました。');
    } else if (execution?.status === 'Interrupted') {
        Message.warning('既存録画の一括話数判定が中断されました。');
    } else if (execution !== null) {
        Message.error(
            `既存録画の一括話数判定に失敗しました。${execution.error_message ? `\n${execution.error_message}` : ''}`,
        );
    }
}

onMounted(async () => {
    // シリーズ欄を表示しない区分では、判定設定・状況を取得しない。
    if (isSectionVisible('series') === false) return;
    const fetched_settings = await RecordedSeries.fetchSettings();
    series_indexing_enabled.value = fetched_settings?.enabled ?? null;
    await refreshSeriesStatus();
    // 開き直し時点で一括判定が実行中なら、既存の polling で追跡を再開する。
    if (series_status.value?.is_running === true || series_status.value?.is_episode_running === true) {
        is_monitoring_episode_backfill.value = series_status.value.is_episode_running;
        startSeriesStatusPolling();
    }
});

onUnmounted(() => {
    episode_backfill_abort_controller?.abort();
    stopSeriesStatusPolling();
});

// KonomiTV-BS4K サーバーの再起動を行う関数
async function restartServer() {
    const result = await Maintenance.restartServer();
    if (result === true) {
        Message.show('KonomiTV-BS4K サーバーを再起動しています...');
        // バージョン情報が取得できるようになるまで待つ
        await Utils.sleep(1.0);
        while (await Version.fetchServerVersion(true) === null) {
            await Utils.sleep(1.0);
        }
        Message.success('KonomiTV-BS4K サーバーを再起動しました。');
    }
}

// KonomiTV-BS4K サーバーのシャットダウンを行う関数
async function shutdownServer() {
    const result = await Maintenance.shutdownServer();
    if (result === true) {
        Message.success('KonomiTV-BS4K サーバーをシャットダウンしました。');
    }
}

</script>
