<template>
    <SettingsBase>
        <h2 class="settings__heading">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:collections-20-filled" width="24px" />
            <span class="ml-2">録画シリーズ</span>
        </h2>
        <div class="settings__description">
            録画のタイトル・概要・既存録画・EPG・Wikipedia 候補を順に照合し、同じシリーズの録画をまとめます。<br>
            この設定と判定結果はすべてのユーザーと端末で共有されます。管理者だけが変更できます。<br>
        </div>

        <v-progress-linear v-if="is_loading" class="mt-5" color="primary" indeterminate rounded />

        <div v-else-if="authorization_error !== null" class="recorded-series-access-state">
            <Icon icon="fluent:shield-error-20-filled" width="28px" />
            <span v-if="authorization_error === 'AdminRequired'">この設定を表示するには管理者権限が必要です。</span>
            <span v-else>ユーザー情報を取得できませんでした。ページを再読み込みしてください。</span>
        </div>

        <template v-else>
            <div class="settings__content"
                :class="{'settings__content--disabled': is_disabled || is_settings_action_running}"
                :inert="is_settings_action_running">
            <div class="settings__content-heading">
                <Icon icon="fluent:wand-20-filled" width="22px" />
                <span class="ml-2">自動判定</span>
            </div>
            <div class="settings__item settings__item--switch">
                <label class="settings__item-heading" for="recorded_series_enabled">新しい録画を自動でシリーズ判定する</label>
                <label class="settings__item-label" for="recorded_series_enabled">
                    有効にすると、新しい録画の登録後にローカル情報からシリーズを自動判定します。<br>
                    無効にしても保存済みのシリーズ情報は維持され、下のボタンから既存録画を手動判定できます。<br>
                </label>
                <v-switch id="recorded_series_enabled" class="settings__item-switch" color="primary" hide-details
                    v-model="settings.enabled" />
            </div>
            <div class="settings__item settings__item--switch">
                <label class="settings__item-heading" for="recorded_series_ai_enabled">曖昧な候補の選択に AI API を使用する</label>
                <label class="settings__item-label" for="recorded_series_ai_enabled">
                    無効にすると、OpenAI 互換 API と Wikipedia へ一切接続せず、ローカル情報・EPG だけで判定します。<br>
                    API キーと保存済みの判定結果は削除されません。<br>
                </label>
                <v-switch id="recorded_series_ai_enabled" class="settings__item-switch" color="primary" hide-details
                    v-model="settings.ai_enabled" />
            </div>

            <div class="settings__content-heading mt-7">
                <Icon icon="fluent:bot-20-filled" width="22px" />
                <span class="ml-2">OpenAI 互換 API</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">API のベース URL</div>
                <div class="settings__item-label">
                    OpenAI または OpenAI 互換サービスの API URL を指定します。末尾のスラッシュはどちらでも構いません。<br>
                    インターネット上のサービスには HTTPS を使用してください。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :error-messages="api_base_url_error"
                    placeholder="https://api.openai.com/v1"
                    spellcheck="false" autocapitalize="off"
                    v-model="settings.api_base_url" />
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">モデル</div>
                <div class="settings__item-label">
                    推奨候補から選ぶか、利用する互換サービスのモデル ID を直接入力してください。<br>
                </div>
                <v-combobox class="settings__item-form" color="primary" variant="outlined"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="model_presets"
                    :error-messages="model_error"
                    hide-no-data spellcheck="false" autocapitalize="off"
                    v-model="settings.model" />
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">1 日の AI API リクエスト上限</div>
                <div class="settings__item-label">
                    想定外の大量呼び出しを防ぐ上限です。0 を指定すると無制限になります。<br>
                    Wikipedia の検索と AI を使わない判定は数えません。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" type="number"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :min="0" :max="1000" :step="1"
                    :error-messages="daily_ai_request_limit_error"
                    v-model.number="settings.daily_ai_request_limit" />
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">API キー</div>
                <div class="settings__item-label">
                    現在の状態: <strong>{{settings.api_key_configured ? '設定済み' : '未設定'}}</strong><br>
                    保存済みのキーはブラウザへ返しません。新しいキーを入力した場合だけサーバー上のキーを置き換えます。<br>
                    認証不要のローカル API を利用する場合は空欄のまま設定できます。<br>
                    接続テストで保存済みキーを使うのは、入力中のベース URL が保存済み設定と一致する場合だけです。<br>
                    保存済みキーがある状態で URL を変える場合は、新しいキーも入力するか、先に保存済みキーを削除してください。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :type="api_key_showing ? 'text' : 'password'"
                    :append-inner-icon="api_key_showing ? 'mdi-eye' : 'mdi-eye-off'"
                    :error-messages="api_key_error"
                    placeholder="新しい API キー"
                    autocomplete="new-password" spellcheck="false" autocapitalize="off"
                    v-model="api_key_input"
                    @click:appendInner="api_key_showing = !api_key_showing" />
                <div class="recorded-series-actions mt-3">
                    <v-btn class="settings__save-button" color="background-lighten-2" variant="flat"
                        :loading="is_testing_connection"
                        :disabled="has_connection_validation_error"
                        @click="testConnection()">
                        <Icon icon="fluent:plug-connected-checkmark-20-filled" class="mr-2" width="21px" />接続テスト
                    </v-btn>
                    <v-btn v-if="settings.api_key_configured" class="settings__save-button" color="error" variant="flat"
                        :loading="is_deleting_api_key" @click="api_key_delete_dialog = true">
                        <Icon icon="fluent:key-reset-20-filled" class="mr-2" width="21px" />保存済みキーを削除
                    </v-btn>
                </div>
                <div class="settings__item-label mt-2">
                    接続テストでは本番判定と同じ API へ最小リクエストを送るため、少量の API 利用が発生します。入力内容は保存されません。<br>
                </div>
            </div>
            <v-btn class="settings__save-button bg-secondary mt-6" variant="flat"
                :loading="is_saving"
                :disabled="has_settings_validation_error"
                @click="saveSettings()">
                <Icon icon="fluent:save-20-filled" class="mr-2" width="22px" />録画シリーズ設定を更新
            </v-btn>

            <div class="settings__content-heading mt-8">
                <Icon icon="fluent:data-usage-20-filled" width="22px" />
                <span class="ml-2">判定状況</span>
            </div>
            <template v-if="status !== null">
                <div class="recorded-series-status-grid mt-5">
                    <div class="recorded-series-status-card">
                        <span>録画総数</span><strong>{{status.total.toLocaleString()}}</strong>
                    </div>
                    <div class="recorded-series-status-card">
                        <span>シリーズ確定</span><strong>{{status.resolved.toLocaleString()}}</strong>
                    </div>
                    <div class="recorded-series-status-card">
                        <span>単発番組</span><strong>{{status.not_series.toLocaleString()}}</strong>
                    </div>
                    <div class="recorded-series-status-card">
                        <span>未判定</span><strong>{{status.pending.toLocaleString()}}</strong>
                    </div>
                    <div class="recorded-series-status-card">
                        <span>要確認</span><strong>{{status.needs_review.toLocaleString()}}</strong>
                    </div>
                    <div class="recorded-series-status-card" :class="{'recorded-series-status-card--error': status.failed > 0}">
                        <span>失敗</span><strong>{{status.failed.toLocaleString()}}</strong>
                    </div>
                </div>
                <div class="settings__item">
                    <div class="settings__item-heading">本日の AI API 利用</div>
                    <div class="settings__item-label">
                        {{status.ai_requests_today.toLocaleString()}} /
                        {{settings.daily_ai_request_limit === 0 ? '無制限' : settings.daily_ai_request_limit.toLocaleString()}} リクエスト<br>
                        最終実行: {{formatLastRunAt(status.last_run_at)}}<br>
                    </div>
                </div>
            </template>
            <div v-else-if="is_loading === false" class="settings__item-label mt-5">
                判定状況を取得できませんでした。
            </div>

            <div class="settings__item">
                <div class="settings__item-heading">既存録画をシリーズ判定</div>
                <div class="settings__item-label">
                    未判定の録画を対象に、ローカル照合から順にシリーズを判定します。確定済みの結果は再利用します。<br>
                    現在サーバーに保存されている設定を使用し、AI API が有効な場合でも曖昧な候補だけを送信します。<br>
                </div>
            </div>
            <div class="settings__item settings__item--switch">
                <label class="settings__item-heading" for="recorded_series_force_backfill">確定済みも再判定する</label>
                <label class="settings__item-label" for="recorded_series_force_backfill">
                    有効にすると、シリーズ確定・単発番組を含むすべての録画を最新の設定で再判定します。<br>
                    既存の判定結果が更新され、AI API の利用が再度発生する場合があります。<br>
                    新しい確定結果を得られない場合は、最後に確定したシリーズ分けを保持します。<br>
                </label>
                <v-switch id="recorded_series_force_backfill" class="settings__item-switch" color="primary" hide-details
                    :disabled="is_backfill_running"
                    v-model="force_backfill" />
            </div>
            <div class="settings__item">
                <v-progress-linear v-if="is_backfill_running" class="mt-4" color="primary" height="7" rounded
                    :indeterminate="backfill_progress === null"
                    :model-value="backfill_progress ?? undefined" />
                <div v-if="backfill_task !== null" class="settings__item-label mt-2">
                    {{stageLabel(backfill_task.stage)}}
                    <template v-if="backfill_progress !== null">・{{backfill_progress.toFixed(0)}}%</template>
                </div>
                <v-btn class="settings__save-button mt-4" color="background-lighten-2" variant="flat"
                    :loading="is_starting_backfill"
                    :disabled="is_backfill_running"
                    @click="startBackfill()">
                    <Icon icon="fluent:arrow-sync-20-filled" class="mr-2" width="22px" />
                    {{force_backfill ? 'すべての録画を再判定' : '既存録画の判定を開始'}}
                </v-btn>
            </div>

            </div>

            <div class="settings__content">
                <v-divider class="mt-7"></v-divider>
                <div class="settings__item">
                    <div class="settings__item-heading">録画シリーズ管理</div>
                    <div class="settings__item-label">
                        判定済みのシリーズを検索し、表示するタイトルと説明を編集できます。<br>
                        録画ごとの所属先は、各録画の再生画面にある「シリーズを訂正」から変更できます。<br>
                    </div>
                    <v-btn class="settings__save-button mt-4" variant="flat"
                        to="/settings/server/recorded-series/series">
                        <Icon icon="fluent:collections-20-filled" class="mr-2" width="22px" />録画シリーズ管理を開く
                    </v-btn>
                </div>
            </div>
        </template>

        <v-dialog v-model="api_key_delete_dialog" max-width="480">
            <v-card class="recorded-series-dialog">
                <v-card-title>保存済み API キーを削除</v-card-title>
                <v-card-text>
                    サーバーに保存されている API キーを削除します。認証が必要な API は、別のキーを保存するまで利用できなくなります。
                </v-card-text>
                <v-card-actions>
                    <v-spacer />
                    <v-btn variant="text" @click="api_key_delete_dialog = false">キャンセル</v-btn>
                    <v-btn color="error" variant="flat" :loading="is_deleting_api_key" @click="deleteAPIKey()">削除</v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>
    </SettingsBase>
</template>

<script setup lang="ts">

import { computed, onMounted, onUnmounted, ref } from 'vue';

import Message from '@/message';
import AnalysisTasks, { type IAnalysisTaskExecution } from '@/services/AnalysisTasks';
import RecordedSeries, {
    type IRecordedSeriesConnectionTestRequest,
    type IRecordedSeriesSettings,
    type IRecordedSeriesSettingsUpdate,
    type IRecordedSeriesStatus,
} from '@/services/RecordedSeries';
import { stageLabel } from '@/stores/AnalysisTasksStore';
import useUserStore from '@/stores/UserStore';
import Utils, { dayjs } from '@/utils';
import SettingsBase from '@/views/Settings/Base.vue';


const model_presets = [
    'gpt-5.6-luna',
    'gpt-5.4-nano',
    'gpt-5-nano',
];

// API 取得前は入力欄を操作できないため、安全側の無効値を初期値にする。
const settings = ref<IRecordedSeriesSettings>({
    enabled: true,
    ai_enabled: false,
    api_base_url: 'https://api.openai.com/v1',
    model: 'gpt-5.6-luna',
    daily_ai_request_limit: 20,
    api_key_configured: false,
});
const saved_api_base_url = ref(settings.value.api_base_url);
const status = ref<IRecordedSeriesStatus | null>(null);

// API キーは共有ストアへ入れず、この画面が開いている間だけローカルメモリに保持する。
const api_key_input = ref('');
const api_key_showing = ref(false);
const api_key_delete_dialog = ref(false);

const is_loading = ref(true);
const is_disabled = ref(true);
const is_saving = ref(false);
const is_testing_connection = ref(false);
const is_deleting_api_key = ref(false);
const is_starting_backfill = ref(false);
const is_monitoring_backfill = ref(false);
const is_refreshing_status = ref(false);
const authorization_error = ref<'AdminRequired' | 'UserUnavailable' | null>(null);
const backfill_task = ref<IAnalysisTaskExecution | null>(null);
const force_backfill = ref(false);

const is_form_dense = Utils.isSmartphoneHorizontal();
const user_store = useUserStore();

let backfill_abort_controller: AbortController | null = null;
let status_polling_timer: number | null = null;


/** API の URL が OpenAI 互換 API の接続先として扱える HTTP(S) URL かを確認する。 */
function isValidAPIBaseURL(value: string): boolean {
    try {
        const parsed_url = new URL(value);
        return (
            (parsed_url.protocol === 'http:' || parsed_url.protocol === 'https:') &&
            parsed_url.username === '' &&
            parsed_url.password === '' &&
            parsed_url.search === '' &&
            parsed_url.hash === ''
        );
    } catch {
        return false;
    }
}

const api_base_url_error = computed(() => {
    const value = settings.value.api_base_url.trim();
    if (value === '') return 'API のベース URL を入力してください。';
    if (value.length > 2048) return 'API のベース URL は 2048 文字以内で入力してください。';
    if (isValidAPIBaseURL(value) === false) return 'ユーザー情報・クエリ・フラグメントを含まない HTTP(S) URL を入力してください。';
    return '';
});
const model_error = computed(() => {
    const value = settings.value.model.trim();
    if (value === '') return 'モデル ID を入力してください。';
    if (value.length > 255) return 'モデル ID は 255 文字以内で入力してください。';
    return '';
});
const daily_ai_request_limit_error = computed(() => {
    if (String(settings.value.daily_ai_request_limit).trim() === '') return '0 ～ 1000 の整数を入力してください。';
    const value = Number(settings.value.daily_ai_request_limit);
    return Number.isInteger(value) && value >= 0 && value <= 1000 ? '' : '0 ～ 1000 の整数を入力してください。';
});
// OpenAI 互換 API はローカル運用などで認証不要の場合もあるため、API キーは必須にしない。
const api_key_length_error = computed(() =>
    api_key_input.value.length > 8192 ? 'API キーは 8192 文字以内で入力してください。' : '',
);
const provider_change_key_error = computed(() => {
    const draft_url = settings.value.api_base_url.trim().replace(/\/+$/, '');
    if (
        settings.value.api_key_configured &&
        draft_url !== saved_api_base_url.value &&
        api_key_input.value.trim() === ''
    ) {
        return 'URL を変更するときは新しい API キーを入力するか、保存済みキーを削除してください。';
    }
    return '';
});
const api_key_error = computed(() => api_key_length_error.value || provider_change_key_error.value);
const has_provider_validation_error = computed(() =>
    api_base_url_error.value !== '' || model_error.value !== '',
);
const has_connection_validation_error = computed(() =>
    has_provider_validation_error.value || api_key_length_error.value !== '',
);
const has_settings_validation_error = computed(() =>
    has_provider_validation_error.value ||
    daily_ai_request_limit_error.value !== '' ||
    api_key_error.value !== '',
);
const is_settings_action_running = computed(() =>
    is_saving.value || is_testing_connection.value || is_deleting_api_key.value,
);
const is_backfill_running = computed(() =>
    is_starting_backfill.value || is_monitoring_backfill.value || status.value?.is_running === true,
);
const backfill_progress = computed(() => {
    if (backfill_task.value?.progress === null || backfill_task.value?.progress === undefined) return null;
    return Math.max(0, Math.min(100, backfill_task.value.progress * 100));
});


/** 設定画面のドラフトから、保存または接続テストに使う API キーを必要な場合だけ追加する。 */
function appendDraftAPIKey<T extends object>(request: T): T & {api_key?: string} {
    const api_key = api_key_input.value.trim();
    if (api_key === '') return request;
    return {...request, api_key};
}

/** 判定状況を更新し、バックフィル終了後は定期取得を止める。 */
async function refreshStatus(show_error = false): Promise<void> {
    if (is_refreshing_status.value) return;
    is_refreshing_status.value = true;
    const fetched_status = await RecordedSeries.fetchStatus(show_error);
    is_refreshing_status.value = false;
    if (fetched_status === null) return;

    status.value = fetched_status;
    if (fetched_status.is_running === false && is_monitoring_backfill.value === false) {
        stopStatusPolling();
    }
}

/** 実行中の一括判定を、画面再読み込み後も状況 API から追跡する。 */
function startStatusPolling(): void {
    if (status_polling_timer !== null) return;
    status_polling_timer = window.setInterval(() => void refreshStatus(), 3000);
}

function stopStatusPolling(): void {
    if (status_polling_timer === null) return;
    window.clearInterval(status_polling_timer);
    status_polling_timer = null;
}

/** サーバー共有設定を保存する。API キー入力は保存成功後だけ破棄する。 */
async function saveSettings(): Promise<void> {
    if (has_settings_validation_error.value || is_settings_action_running.value) return;

    is_saving.value = true;
    const request = appendDraftAPIKey<IRecordedSeriesSettingsUpdate>({
        enabled: settings.value.enabled,
        ai_enabled: settings.value.ai_enabled,
        api_base_url: settings.value.api_base_url.trim().replace(/\/+$/, ''),
        model: settings.value.model.trim(),
        daily_ai_request_limit: Number(settings.value.daily_ai_request_limit),
    });
    const result = await RecordedSeries.updateSettings(request);
    if (result) {
        settings.value = {
            enabled: request.enabled,
            ai_enabled: request.ai_enabled,
            api_base_url: request.api_base_url,
            model: request.model,
            daily_ai_request_limit: request.daily_ai_request_limit,
            api_key_configured: settings.value.api_key_configured || request.api_key !== undefined,
        };
        saved_api_base_url.value = request.api_base_url;
        api_key_input.value = '';
        api_key_showing.value = false;
        Message.success('録画シリーズ判定設定を更新しました。');
    }
    is_saving.value = false;
}

/** 現在のドラフトを保存せず、本番と同じ OpenAI 互換 API への最小リクエストを試す。 */
async function testConnection(): Promise<void> {
    if (has_connection_validation_error.value || is_settings_action_running.value) return;

    is_testing_connection.value = true;
    const request = appendDraftAPIKey<IRecordedSeriesConnectionTestRequest>({
        api_base_url: settings.value.api_base_url.trim().replace(/\/+$/, ''),
        model: settings.value.model.trim(),
    });
    const result = await RecordedSeries.testConnection(request);
    if (result?.success) {
        Message.success(`OpenAI 互換 API へ接続できました。（${result.model} / ${result.latency_ms.toLocaleString()} ms）`);
    } else if (result !== null) {
        Message.error(`OpenAI 互換 API へ接続できませんでした。\n${result.message}`);
    }
    is_testing_connection.value = false;
}

/** 保存済み API キーを、確認ダイアログから明示的に削除する。 */
async function deleteAPIKey(): Promise<void> {
    if (is_settings_action_running.value) return;
    is_deleting_api_key.value = true;
    if (await RecordedSeries.deleteAPIKey()) {
        settings.value.api_key_configured = false;
        api_key_input.value = '';
        api_key_showing.value = false;
        api_key_delete_dialog.value = false;
        Message.success('保存済みの API キーを削除しました。');
    }
    is_deleting_api_key.value = false;
}

/** 既存録画を対象に、バックグラウンドでシリーズ判定を実行する。 */
async function startBackfill(): Promise<void> {
    const confirmation_message = force_backfill.value ?
        'シリーズ確定・単発番組を含むすべての録画を再判定します。既存の判定結果が更新され、AI API が有効な場合は API 利用が再度発生することがあります。続行しますか？' :
        '確定済みの結果を再利用して、未判定の既存録画をシリーズ判定します。AI API が有効な場合は、曖昧な候補に限り API 利用が発生します。続行しますか？';
    const confirmed = window.confirm(confirmation_message);
    if (confirmed === false) return;

    is_starting_backfill.value = true;
    const accepted = await RecordedSeries.startBackfill(force_backfill.value);
    is_starting_backfill.value = false;
    if (accepted === null) return;

    status.value = status.value === null ? null : {...status.value, is_running: true};
    is_monitoring_backfill.value = true;
    startStatusPolling();
    Message.info(accepted.reused ? '実行中のシリーズ判定を引き続き監視します。' : '既存録画のシリーズ判定を開始しました。');

    // 同じ画面から再実行した場合に古い監視を残さない。
    backfill_abort_controller?.abort();
    backfill_abort_controller = new AbortController();
    const execution = await AnalysisTasks.waitForCompletion(
        accepted.execution_id,
        backfill_abort_controller.signal,
        task => backfill_task.value = task,
    );
    if (backfill_abort_controller.signal.aborted) return;

    is_monitoring_backfill.value = false;
    await refreshStatus();
    if (execution?.status === 'Succeeded') {
        Message.success('既存録画のシリーズ判定が完了しました。');
    } else if (execution?.status === 'Interrupted') {
        Message.warning('既存録画のシリーズ判定が中断されました。');
    } else if (execution !== null) {
        Message.error(`既存録画のシリーズ判定に失敗しました。${execution.error_message ? `\n${execution.error_message}` : ''}`);
    }
}

function formatLastRunAt(value: string | null): string {
    return value === null ? '未実行' : dayjs(value).format('YYYY/M/D HH:mm:ss');
}


onMounted(async () => {
    // API キーの設定状態を含むため、管理者と確認できるまでは設定 API へアクセスしない。
    const fetched_user = await user_store.fetchUser();
    // fetchUser() はアイコン取得だけが失敗した場合も null を返すが、ユーザー本体は Store に残る。
    const user = fetched_user ?? user_store.user;
    if (user === null) {
        authorization_error.value = 'UserUnavailable';
        is_loading.value = false;
        return;
    }
    if (user.is_admin !== true) {
        authorization_error.value = 'AdminRequired';
        is_loading.value = false;
        return;
    }
    const [fetched_settings, fetched_status] = await Promise.all([
        RecordedSeries.fetchSettings(),
        RecordedSeries.fetchStatus(),
    ]);
    if (fetched_settings !== null) {
        settings.value = fetched_settings;
        saved_api_base_url.value = fetched_settings.api_base_url;
        is_disabled.value = false;
    }
    if (fetched_status !== null) {
        status.value = fetched_status;
        if (fetched_status.is_running) startStatusPolling();
    }
    is_loading.value = false;
});

onUnmounted(() => {
    // 画面を離れたら API キー入力とポーリングを即座に破棄する。
    api_key_input.value = '';
    backfill_abort_controller?.abort();
    stopStatusPolling();
});

</script>

<style lang="scss" scoped>

.recorded-series-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
}

.recorded-series-access-state {
    display: flex;
    align-items: center;
    min-height: 150px;
    gap: 10px;
    color: rgb(var(--v-theme-text-darken-1));
    font-size: 13px;
}

.recorded-series-status-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
}

.recorded-series-status-card {
    display: flex;
    flex-direction: column;
    min-width: 0;
    padding: 13px 15px;
    border-radius: 7px;
    background: rgb(var(--v-theme-background-lighten-1));

    span {
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 12.5px;
    }

    strong {
        margin-top: 2px;
        color: rgb(var(--v-theme-text));
        font-size: 21px;
    }

    &--error strong {
        color: rgb(var(--v-theme-error-readable));
    }
}

.recorded-series-dialog {
    background: rgb(var(--v-theme-background-lighten-1));
}

@include smartphone-vertical {
    .recorded-series-status-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
}

</style>
