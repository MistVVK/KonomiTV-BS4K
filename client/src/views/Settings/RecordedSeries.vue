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
            録画のタイトル・概要からシリーズを判定し、同じ作品の録画をまとめます。<br>
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
                <label class="settings__item-heading" for="recorded_series_ai_enabled">
                    AI でシリーズ名・話数・話名を生成する
                </label>
                <label class="settings__item-label" for="recorded_series_ai_enabled">
                    有効時、Manual / Rule 以外は AI が作品名・シーズン話数・話名を一括生成します。<br>
                    既存シリーズがある場合は、サーバーが ID・Wikipedia・正規化キー・類似度で合流します。<br>
                    既存 Series の表示名は AI では書き換えません。Wikipedia と既存シリーズは参考情報として渡します。<br>
                    無効時は Wikipedia と AI へ接続せず、従来のローカル情報・EPG 経路で判定します。<br>
                    API キーと保存済みの判定結果は削除されません。<br>
                </label>
                <v-switch id="recorded_series_ai_enabled" class="settings__item-switch" color="primary" hide-details
                    v-model="settings.ai_enabled" />
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">話数不明時の Web 検索</div>
                <div class="settings__item-label">
                    AI が有効で Series が確定している話数不明録画は、接続確認済みのバックエンドで Web 検索します。<br>
                    Web 検索・出力形式・公開 URL の根拠を確認できた結果は、AI の信頼度表示にかかわらず自動採用します。<br>
                    根拠不足・検索失敗・不正な応答では現在の正本を変更しません。<br>
                    既存録画は下の一括話数判定、または録画シリーズ管理から明示的に検索できます。<br>
                </div>
            </div>

            <div class="settings__content-heading mt-7">
                <Icon icon="fluent:bot-20-filled" width="22px" />
                <span class="ml-2">AI バックエンド（主系）</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">バックエンド</div>
                <div class="settings__item-label">
                    AI によるシリーズ情報生成と話数検索に最初に使用するバックエンドを選択します。<br>
                    OpenCode は「設定 → AIバックエンド」で登録した service を使います。<br>
                    ACP / Codex・Grok はホスト上の CLI を起動します。<br>
                    シリーズ情報生成と話数 Web 検索は、AIバックエンド画面から個別に接続確認できます。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="ai_backend_options" item-title="title" item-value="value"
                    v-model="settings.ai_backend" />
            </div>

            <!-- OpenCode service 選択 -->
            <template v-if="settings.ai_backend === 'OpenCode'">
            <div class="settings__item">
                <div class="settings__item-heading">OpenCode service</div>
                <div class="settings__item-label">
                    AI バックエンド画面で登録した service を選びます。API キーや月次上限はそちらで管理します。<br>
                </div>
                <v-btn class="settings__save-button mt-3" variant="flat" to="/settings/server/ai-backends">
                    <Icon icon="fluent:settings-20-regular" class="mr-2" width="21px" />
                    AIバックエンド設定を開く
                </v-btn>
                <v-select class="settings__item-form" color="primary" variant="outlined"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="opencode_services" item-title="title" item-value="value"
                    :error-messages="opencode_service_error"
                    no-data-text="登録済み service がありません"
                    v-model="settings.ai_backend_service_id" />
            </div>
            </template>

            <!-- ACP 選択時は AI バックエンドページへ案内（モデル・認証・接続試験はそちらで設定） -->
            <template v-if="settings.ai_backend === 'AcpCodex' || settings.ai_backend === 'AcpGrok'">
                <div class="settings__item">
                    <div class="settings__item-heading">モデル・認証・接続試験</div>
                    <div class="settings__item-label">
                        ACP / Codex・Grok Build のモデル・推論深さ・認証・接続試験は
                        「AIバックエンド」ページで設定します。
                    </div>
                    <v-btn class="settings__save-button mt-3" variant="flat" to="/settings/server/ai-backends">
                        <Icon icon="fluent:settings-20-regular" class="mr-2" width="21px" />
                        AIバックエンド設定を開く
                    </v-btn>
                </div>
            </template>

            <div class="settings__content-heading mt-7">
                <Icon icon="fluent:arrow-sync-20-filled" width="22px" />
                <span class="ml-2">AI バックエンド（予備）</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">失敗時ポリシー</div>
                <div class="settings__item-label">
                    主系 AI が技術的に失敗したとき、または Unresolved / InsufficientEvidence のときにどうするかを決めます。<br>
                    NotSeries / NoPublishedNumber / NotNumbered など正常な判定結果では切り替えません。<br>
                    失敗時ポリシーによる AI 試行は最大 2 回です。OpenCode 内部の出力形式補修は別に行われます。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="ai_failure_recovery_options" item-title="title" item-value="value"
                    v-model="settings.ai_failure_recovery_strategy" />
            </div>
            <template v-if="settings.ai_failure_recovery_strategy === 'FallbackBackend'">
                <div class="settings__item">
                    <div class="settings__item-heading">予備バックエンド</div>
                    <div class="settings__item-label">
                        主系とは独立したバックエンドを選びます。予備へは主系の回答や失敗理由を渡しません。<br>
                        主系と同じバックエンド（OpenCode なら同じ service）は選べません。<br>
                    </div>
                    <v-select class="settings__item-form" color="primary" variant="outlined"
                        :density="is_form_dense ? 'compact' : 'default'"
                        :items="ai_backend_options" item-title="title" item-value="value"
                        :error-messages="fallback_backend_error || (
                            settings.ai_fallback_backend !== 'OpenCode' ? fallback_auth_error : ''
                        )"
                        v-model="settings.ai_fallback_backend" />
                </div>
                <template v-if="settings.ai_fallback_backend === 'OpenCode'">
                    <div class="settings__item">
                        <div class="settings__item-heading">予備 OpenCode service</div>
                        <div class="settings__item-label">
                            予備として使う OpenCode service を選びます。参照中の service は削除できません。<br>
                        </div>
                        <v-select class="settings__item-form" color="primary" variant="outlined"
                            :density="is_form_dense ? 'compact' : 'default'"
                            :items="opencode_services" item-title="title" item-value="value"
                            :error-messages="fallback_opencode_service_error || fallback_auth_error"
                            no-data-text="登録済み service がありません"
                            v-model="settings.ai_fallback_backend_service_id" />
                        <div class="settings__item-label mt-2">
                            認証状態: {{ fallback_auth_configured ? '設定済み' : '未設定' }}
                        </div>
                    </div>
                </template>
                <template v-if="settings.ai_fallback_backend === 'AcpCodex' || settings.ai_fallback_backend === 'AcpGrok'">
                    <div class="settings__item">
                        <div class="settings__item-heading">予備 ACP のモデル・認証</div>
                        <div class="settings__item-label">
                            予備 ACP のモデル・推論深さ・認証も「AIバックエンド」ページの設定を共有します。<br>
                            認証状態: {{ fallback_auth_configured ? '設定済み' : '未設定' }}
                        </div>
                    </div>
                </template>
            </template>
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
                <div class="settings__item">
                    <div class="settings__item-heading">シリーズ判定</div>
                    <div class="recorded-series-status-grid mt-3">
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
                        <div class="recorded-series-status-card"
                            :class="{'recorded-series-status-card--error': status.failed > 0}">
                            <span>失敗</span><strong>{{status.failed.toLocaleString()}}</strong>
                        </div>
                    </div>
                </div>
                <div class="settings__item">
                    <div class="settings__item-heading">話数判定</div>
                    <div class="recorded-series-status-grid mt-3">
                        <div class="recorded-series-status-card">
                            <span>話数確定</span><strong>{{status.episode_resolved.toLocaleString()}}</strong>
                        </div>
                        <div class="recorded-series-status-card">
                            <span>話数不明</span><strong>{{status.episode_unknown.toLocaleString()}}</strong>
                        </div>
                        <div class="recorded-series-status-card">
                            <span>話数番号なし</span><strong>{{status.episode_not_numbered.toLocaleString()}}</strong>
                        </div>
                        <div class="recorded-series-status-card">
                            <span>公開話数なし</span><strong>{{status.episode_no_published_number.toLocaleString()}}</strong>
                        </div>
                        <div class="recorded-series-status-card">
                            <span>要確認</span><strong>{{status.episode_needs_review.toLocaleString()}}</strong>
                        </div>
                        <div class="recorded-series-status-card"
                            :class="{'recorded-series-status-card--error': status.episode_failed > 0}">
                            <span>失敗</span><strong>{{status.episode_failed.toLocaleString()}}</strong>
                        </div>
                    </div>
                </div>
                <div class="settings__item">
                    <div class="settings__item-heading">最終実行</div>
                    <div class="settings__item-label">
                        シリーズ判定: {{formatLastRunAt(status.last_run_at)}}<br>
                        話数判定: {{formatLastRunAt(status.episode_last_run_at)}}<br>
                        OpenCode の月次利用量は
                        <router-link to="/settings/server/ai-backends">AIバックエンド</router-link>
                        で確認します。<br>
                    </div>
                </div>
            </template>
            <div v-else-if="is_loading === false" class="settings__item-label mt-5">
                判定状況を取得できませんでした。
            </div>

            <div class="settings__item">
                <div class="settings__item-heading">既存録画をシリーズ判定</div>
                <div class="settings__item-label">
                    未判定の録画を対象に、ローカル照合と保存済みの AI バックエンドを利用してシリーズを判定します。<br>
                    Manual / Rule の確定結果は再利用し、それ以外の録画では AI による判定と利用量が発生します。<br>
                    実行前に <router-link to="/settings/server/ai-backends">AIバックエンド</router-link>を設定してください。<br>
                </div>
                <div v-if="series_backfill_unavailable_message !== null"
                    class="settings__item-label mt-2 text-warning">
                    {{series_backfill_unavailable_message}}
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
                    :disabled="is_backfill_running || is_episode_backfill_running"
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
                    :disabled="is_backfill_running || is_episode_backfill_running ||
                        is_series_backfill_available === false"
                    @click="startBackfill()">
                    <Icon icon="fluent:arrow-sync-20-filled" class="mr-2" width="22px" />
                    {{force_backfill ? 'すべての録画を再判定' : '既存録画の判定を開始'}}
                </v-btn>
            </div>

            <v-divider class="mt-7" />
            <div class="settings__item">
                <div class="settings__item-heading">既存録画の一括話数判定</div>
                <div class="settings__item-label">
                    Series 所属済みで、話数が未処理・ローカル判定で不明・移行データで要確認の既存録画を、<br>
                    保存済みの AI 設定で順番に Web 検索します。<br>
                    手動で訂正した話数は変更せず、月次の AI 利用上限に達した時点で残りを保留します。<br>
                </div>
            </div>
            <div class="settings__item settings__item--switch">
                <label class="settings__item-heading" for="recorded_series_force_episode_backfill">
                    判定済みの話数も再検索する
                </label>
                <label class="settings__item-label" for="recorded_series_force_episode_backfill">
                    有効にすると、AI・ローカル情報・EPG・移行データで確定済みの話数も最新の設定で再検索します。<br>
                    話数番号なし・要確認・失敗の結果も再検索しますが、手動で訂正した話数は対象外です。<br>
                </label>
                <v-switch id="recorded_series_force_episode_backfill" class="settings__item-switch"
                    color="primary" hide-details
                    :disabled="is_episode_backfill_running || is_backfill_running"
                    v-model="force_episode_backfill" />
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
                    :disabled="is_episode_backfill_running || is_backfill_running"
                    @click="startEpisodeBackfill()">
                    <Icon icon="fluent:globe-search-20-filled" class="mr-2" width="22px" />
                    {{force_episode_backfill ? '判定済みを含めて話数を再検索' : '既存録画の話数判定を開始'}}
                </v-btn>
            </div>

            </div>

            <div class="settings__content">
                <v-divider class="mt-7"></v-divider>
                <div class="settings__item">
                    <div class="settings__item-heading">録画シリーズ管理</div>
                    <div class="settings__item-label">
                        判定済みのシリーズを検索し、表示するタイトル・説明と、録画ごとのシーズン・話数を編集できます。<br>
                        録画ごとの所属先は、各録画の再生画面にある「シリーズを訂正」から変更できます。<br>
                    </div>
                    <v-btn class="settings__save-button mt-4" variant="flat"
                        to="/settings/server/recorded-series/series">
                        <Icon icon="fluent:collections-20-filled" class="mr-2" width="22px" />録画シリーズ管理を開く
                    </v-btn>
                </div>
            </div>
        </template>

    </SettingsBase>
</template>

<script setup lang="ts">

import { computed, onMounted, onUnmounted, ref } from 'vue';

import Message from '@/message';
import AIBackend, { type AIAuthMode, type IACPBackendCredentialStatus } from '@/services/AIBackend';
import AnalysisTasks, { type IAnalysisTaskExecution } from '@/services/AnalysisTasks';
import RecordedSeries, {
    type AIBackendKind,
    type AIFailureRecoveryStrategy,
    type IRecordedSeriesSettings,
    type IRecordedSeriesSettingsUpdate,
    type IRecordedSeriesStatus,
} from '@/services/RecordedSeries';
import { stageLabel } from '@/stores/AnalysisTasksStore';
import useUserStore from '@/stores/UserStore';
import Utils, { dayjs } from '@/utils';
import SettingsBase from '@/views/Settings/Base.vue';


const ai_backend_options: {title: string; value: AIBackendKind;}[] = [
    {title: 'OpenCode（AIバックエンド service）', value: 'OpenCode'},
    {title: 'ACP / Codex', value: 'AcpCodex'},
    {title: 'ACP / Grok Build', value: 'AcpGrok'},
];
const ai_failure_recovery_options: {title: string; value: AIFailureRecoveryStrategy;}[] = [
    {title: 'Fail（追加試行なし）', value: 'Fail'},
    {title: 'RetrySameBackend（主系を修正版プロンプトで再実行）', value: 'RetrySameBackend'},
    {title: 'FallbackBackend（予備 AI へ切り替え）', value: 'FallbackBackend'},
];
const ai_auth_mode_labels: Record<AIAuthMode, string> = {
    ApiKey: 'API キー',
    OAuthSubscription: 'OAuth',
    VertexAdc: 'Vertex ADC',
    NoneLocal: '認証なし',
};

// API 取得前は入力欄を操作できないため、安全側の無効値を初期値にする。
const settings = ref<IRecordedSeriesSettings>({
    enabled: true,
    ai_enabled: false,
    ai_backend: 'AcpCodex',
    ai_backend_service_id: null,
    ai_backend_service_name: null,
    ai_backend_auth_configured: false,
    ai_failure_recovery_strategy: 'Fail',
    ai_fallback_backend: null,
    ai_fallback_backend_service_id: null,
    ai_fallback_backend_service_name: null,
    ai_fallback_backend_auth_configured: false,
});
// 一括判定は未保存のフォーム値ではなく、サーバーに保存済みの AI 設定だけを利用する。
const saved_settings = ref<IRecordedSeriesSettings | null>(null);
const opencode_services = ref<{title: string; value: string; authConfigured: boolean;}[]>([]);
const acp_credential_status = ref<IACPBackendCredentialStatus | null>(null);
const status = ref<IRecordedSeriesStatus | null>(null);

const is_loading = ref(true);
const is_disabled = ref(true);
const is_saving = ref(false);
const is_starting_backfill = ref(false);
const is_monitoring_backfill = ref(false);
const is_starting_episode_backfill = ref(false);
const is_monitoring_episode_backfill = ref(false);
const is_refreshing_status = ref(false);
const authorization_error = ref<'AdminRequired' | 'UserUnavailable' | null>(null);
const backfill_task = ref<IAnalysisTaskExecution | null>(null);
const episode_backfill_task = ref<IAnalysisTaskExecution | null>(null);
const force_backfill = ref(false);
const force_episode_backfill = ref(false);

const is_form_dense = Utils.isSmartphoneHorizontal();
const user_store = useUserStore();

let backfill_abort_controller: AbortController | null = null;
let episode_backfill_abort_controller: AbortController | null = null;
let status_polling_timer: number | null = null;


const opencode_service_error = computed(() => {
    if (settings.value.ai_backend !== 'OpenCode') return '';
    if (settings.value.ai_enabled && !settings.value.ai_backend_service_id) {
        return 'OpenCode service を選択してください。';
    }
    return '';
});
const fallback_backend_error = computed(() => {
    if (settings.value.ai_failure_recovery_strategy !== 'FallbackBackend') return '';
    if (!settings.value.ai_fallback_backend) {
        return '予備 AI バックエンドを選択してください。';
    }
    if (areBackendTargetsIdentical(
        settings.value.ai_backend,
        settings.value.ai_backend_service_id,
        settings.value.ai_fallback_backend,
        settings.value.ai_fallback_backend_service_id,
    )) {
        return '主系と同じバックエンドは予備に選べません。';
    }
    return '';
});
const fallback_opencode_service_error = computed(() => {
    if (settings.value.ai_failure_recovery_strategy !== 'FallbackBackend') return '';
    if (settings.value.ai_fallback_backend !== 'OpenCode') return '';
    if (settings.value.ai_enabled && !settings.value.ai_fallback_backend_service_id) {
        return '予備 OpenCode service を選択してください。';
    }
    if (
        settings.value.ai_backend === 'OpenCode'
        && settings.value.ai_backend_service_id
        && settings.value.ai_fallback_backend_service_id
        && settings.value.ai_backend_service_id.toLowerCase()
            === settings.value.ai_fallback_backend_service_id.toLowerCase()
    ) {
        return '主系と同じ OpenCode service は予備に選べません。';
    }
    return '';
});
const fallback_auth_configured = computed(() => {
    if (settings.value.ai_failure_recovery_strategy !== 'FallbackBackend') return false;
    const fallbackBackend = settings.value.ai_fallback_backend;
    if (fallbackBackend === 'OpenCode') {
        const selectedServiceID = settings.value.ai_fallback_backend_service_id?.toLowerCase() ?? null;
        const selectedService = opencode_services.value.find(service => service.value.toLowerCase() === selectedServiceID);
        return selectedService?.authConfigured === true;
    }
    if (fallbackBackend === 'AcpCodex') return acp_credential_status.value?.codex_auth_imported === true;
    if (fallbackBackend === 'AcpGrok') return acp_credential_status.value?.grok_auth_imported === true;
    return false;
});
const fallback_auth_error = computed(() => {
    if (settings.value.ai_enabled === false) return '';
    if (settings.value.ai_failure_recovery_strategy !== 'FallbackBackend') return '';
    return fallback_auth_configured.value ? '' : '予備 AI バックエンドの認証を設定してください。';
});
const has_settings_validation_error = computed(() =>
    opencode_service_error.value !== ''
    || fallback_backend_error.value !== ''
    || fallback_opencode_service_error.value !== ''
    || fallback_auth_error.value !== '',
);

/** 主系と予備の実行ターゲットが同一かを判定する。 */
function areBackendTargetsIdentical(
    primary_backend: AIBackendKind,
    primary_service_id: string | null,
    fallback_backend: AIBackendKind | null,
    fallback_service_id: string | null,
): boolean {
    if (fallback_backend === null) return false;
    if (primary_backend !== fallback_backend) return false;
    if (primary_backend === 'OpenCode') {
        return (
            primary_service_id !== null
            && fallback_service_id !== null
            && primary_service_id.toLowerCase() === fallback_service_id.toLowerCase()
        );
    }
    return true;
}
const is_settings_action_running = computed(() =>
    is_saving.value,
);
const is_backfill_running = computed(() =>
    is_starting_backfill.value || is_monitoring_backfill.value || status.value?.is_running === true,
);
const is_episode_backfill_running = computed(() =>
    is_starting_episode_backfill.value ||
    is_monitoring_episode_backfill.value ||
    status.value?.is_episode_running === true,
);
const series_backfill_unavailable_message = computed(() => {
    if (saved_settings.value?.ai_backend_auth_configured !== true) {
        return 'AIバックエンドが未設定のため、シリーズ判定を開始できません。';
    }
    if (saved_settings.value.ai_enabled === false) {
        return '「AI でシリーズ名・話数・話名を生成する」を有効にして設定を保存してください。';
    }
    return null;
});
const is_series_backfill_available = computed(() => series_backfill_unavailable_message.value === null);
const saved_ai_backend_label = computed(() => {
    const savedSettings = saved_settings.value;
    if (savedSettings === null) return '保存済みの AI バックエンド';
    if (savedSettings.ai_backend === 'OpenCode') {
        return savedSettings.ai_backend_service_name ?? 'OpenCode';
    }
    return savedSettings.ai_backend === 'AcpCodex' ? 'ACP / Codex' : 'ACP / Grok Build';
});
const backfill_progress = computed(() => {
    if (backfill_task.value?.progress === null || backfill_task.value?.progress === undefined) return null;
    return Math.max(0, Math.min(100, backfill_task.value.progress * 100));
});
const episode_backfill_progress = computed(() => {
    if (episode_backfill_task.value?.progress === null || episode_backfill_task.value?.progress === undefined) {
        return null;
    }
    return Math.max(0, Math.min(100, episode_backfill_task.value.progress * 100));
});

/** 画面上の全ドラフトを保存 payload へ変換する。 */
function buildSettingsRequest(): IRecordedSeriesSettingsUpdate {
    const strategy = settings.value.ai_failure_recovery_strategy;
    const use_fallback = strategy === 'FallbackBackend';
    const fallback_backend = use_fallback ? settings.value.ai_fallback_backend : null;
    return {
        enabled: settings.value.enabled,
        ai_enabled: settings.value.ai_enabled,
        ai_backend: settings.value.ai_backend,
        ai_backend_service_id: settings.value.ai_backend === 'OpenCode'
            ? settings.value.ai_backend_service_id
            : null,
        ai_failure_recovery_strategy: strategy,
        ai_fallback_backend: fallback_backend,
        ai_fallback_backend_service_id: (
            use_fallback && fallback_backend === 'OpenCode'
        ) ? settings.value.ai_fallback_backend_service_id : null,
    };
}

/** GET の保存済み snapshot を画面へ反映する。 */
function applyFetchedSettings(fetched_settings: IRecordedSeriesSettings): void {
    // ローリング更新中の旧サーバーや古い mock が廃止済み backend を返しても、
    // 一覧外の値を表示したり任意コマンド設定へ戻ったりしないようクライアントでも fail-closed にする。
    const is_supported_backend = ai_backend_options.some(option => option.value === fetched_settings.ai_backend);
    const is_supported_fallback = (
        fetched_settings.ai_fallback_backend === null
        || ai_backend_options.some(option => option.value === fetched_settings.ai_fallback_backend)
    );
    const is_supported_strategy = ai_failure_recovery_options.some(
        option => option.value === fetched_settings.ai_failure_recovery_strategy,
    );
    const normalized_settings: IRecordedSeriesSettings = {
        ...fetched_settings,
        ai_backend: is_supported_backend ? fetched_settings.ai_backend : 'AcpCodex',
        ai_enabled: is_supported_backend ? fetched_settings.ai_enabled : false,
        ai_failure_recovery_strategy: is_supported_strategy
            ? fetched_settings.ai_failure_recovery_strategy
            : 'Fail',
        ai_fallback_backend: is_supported_fallback
            ? fetched_settings.ai_fallback_backend
            : null,
        ai_fallback_backend_service_id: (
            is_supported_fallback
            && fetched_settings.ai_fallback_backend === 'OpenCode'
        ) ? fetched_settings.ai_fallback_backend_service_id : null,
        ai_fallback_backend_service_name: (
            is_supported_fallback
            && fetched_settings.ai_fallback_backend === 'OpenCode'
        ) ? fetched_settings.ai_fallback_backend_service_name : null,
        ai_fallback_backend_auth_configured: fetched_settings.ai_fallback_backend_auth_configured,
    };
    settings.value = {...normalized_settings};
    saved_settings.value = {...normalized_settings};
}

/** 判定状況を更新し、バックフィル終了後は定期取得を止める。 */
async function refreshStatus(show_error = false): Promise<void> {
    if (is_refreshing_status.value) return;
    is_refreshing_status.value = true;
    const fetched_status = await RecordedSeries.fetchStatus(show_error);
    is_refreshing_status.value = false;
    if (fetched_status === null) return;

    status.value = fetched_status;
    if (
        fetched_status.is_running === false &&
        fetched_status.is_episode_running === false &&
        is_monitoring_backfill.value === false &&
        is_monitoring_episode_backfill.value === false
    ) {
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
    const request = buildSettingsRequest();
    const result = await RecordedSeries.updateSettings(request);
    if (result) {
        const fetched_settings = await RecordedSeries.fetchSettings();
        if (fetched_settings !== null) {
            applyFetchedSettings(fetched_settings);
        }
        Message.success('録画シリーズ判定設定を更新しました。');
    }
    is_saving.value = false;
}


/** 既存録画を対象に、バックグラウンドでシリーズ判定を実行する。 */
async function startBackfill(): Promise<void> {
    if (is_episode_backfill_running.value) return;
    if (is_series_backfill_available.value === false) {
        Message.warning('AIバックエンドを設定してからシリーズ判定を実行してください。');
        return;
    }
    const confirmation_message = force_backfill.value ?
        `「${saved_ai_backend_label.value}」AIバックエンドを利用し、シリーズ確定・単発番組を含むすべての録画を再判定します。既存の判定結果が更新され、録画件数に応じた AI 利用が発生します。続行しますか？` :
        `「${saved_ai_backend_label.value}」AIバックエンドを利用し、未判定の既存録画をシリーズ判定します。Manual / Rule 以外では、録画件数に応じた AI 利用が発生します。続行しますか？`;
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

/** Series所属済みの既存録画を対象に、バックグラウンドで話数Web検索を実行する。 */
async function startEpisodeBackfill(): Promise<void> {
    if (is_backfill_running.value) return;
    const confirmation_message = force_episode_backfill.value ?
        '手動訂正を除く判定済みの話数も、最新の AI 設定で再検索します。録画件数に応じて AI API 利用が発生します。続行しますか？' :
        '話数が未確定の既存録画を、保存済みの AI 設定で Web 検索します。録画件数に応じて AI API 利用が発生します。続行しますか？';
    const confirmed = window.confirm(confirmation_message);
    if (confirmed === false) return;

    is_starting_episode_backfill.value = true;
    const accepted = await RecordedSeries.startEpisodeBackfill(force_episode_backfill.value);
    is_starting_episode_backfill.value = false;
    if (accepted === null) return;

    status.value = status.value === null ? null : {...status.value, is_episode_running: true};
    is_monitoring_episode_backfill.value = true;
    startStatusPolling();
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
    await refreshStatus();
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

function formatLastRunAt(value: string | null): string {
    return value === null ? '未実行' : dayjs(value).format('YYYY/M/D HH:mm:ss');
}


onMounted(async () => {
    // 管理者と確認できるまでは設定 API へアクセスしない。
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
    const [fetched_settings, fetched_status, fetched_services, fetched_acp_credentials] = await Promise.all([
        RecordedSeries.fetchSettings(),
        RecordedSeries.fetchStatus(),
        AIBackend.fetchServices(),
        AIBackend.fetchACPCredentialStatus(),
    ]);
    if (fetched_services !== null) {
        opencode_services.value = fetched_services.map(service => ({
            title: `${service.service_name} (${ai_auth_mode_labels[service.auth_mode]} · ${
                service.opencode_provider_type === 'Catalog' ? service.opencode_provider_id : 'カスタムAPI'
            }/${service.opencode_model_id}${
                service.opencode_model_variant ? `[${service.opencode_model_variant}]` : ''
            } · ${service.structured_output_mode})`,
            value: service.service_id,
            authConfigured: service.auth_configured,
        }));
    }
    if (fetched_acp_credentials !== null) {
        acp_credential_status.value = fetched_acp_credentials;
    }
    if (fetched_settings !== null) {
        applyFetchedSettings(fetched_settings);
        is_disabled.value = false;
    }
    if (fetched_status !== null) {
        status.value = fetched_status;
        if (fetched_status.is_running || fetched_status.is_episode_running) startStatusPolling();
    }
    is_loading.value = false;
});

onUnmounted(() => {
    backfill_abort_controller?.abort();
    episode_backfill_abort_controller?.abort();
    stopStatusPolling();
});

</script>

<style lang="scss" scoped>

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

@include smartphone-vertical {
    .recorded-series-status-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
}

</style>
