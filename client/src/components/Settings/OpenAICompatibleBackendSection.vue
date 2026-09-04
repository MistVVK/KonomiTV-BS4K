<template>
    <div class="openai-compatible-section">
        <div class="settings__content">
            <div class="settings__content-heading">
                <Icon icon="fluent:globe-20-filled" width="22px" />
                <span class="ml-2">{{backend_title}}</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-label">
                    OpenCode serve を経由せず、指定した OpenAI 互換 HTTP API へ直接接続します。<br>
                    シリーズ生成には Chat Completions、話数 Web 検索には Responses API の
                    <code>web_search</code> を使用します。<br>
                    Chat Completions だけを実装した互換サーバーではシリーズ生成だけ利用でき、
                    話数 Web 検索の接続試験は失敗します。<br>
                </div>
            </div>
            <v-progress-linear v-if="is_loading" class="mt-4" color="primary" indeterminate rounded />
            <v-alert v-else-if="settings_load_failed" class="mt-4" color="warning" variant="tonal">
                保存済み設定を取得できませんでした。空の設定で上書きしないよう編集を停止しています。
                <v-btn class="ml-2" size="small" variant="tonal" @click="loadSettings()">再試行</v-btn>
            </v-alert>
            <template v-else>
                <div class="settings__item">
                    <div class="settings__item-heading">API ベース URL</div>
                    <div class="settings__item-label">
                        プロバイダーの API root、または <code>/chat/completions</code> / <code>/responses</code>
                        まで含む URL を指定できます。HTTP(S) URL だけを受理します。<br>
                    </div>
                    <v-text-field v-model="draft.api_base_url" class="settings__item-form" color="primary"
                        variant="outlined" :density="is_form_dense ? 'compact' : 'default'"
                        label="https://api.example.com/v1" spellcheck="false"
                        :error-messages="api_base_url_error" />
                </div>
                <div class="settings__item">
                    <div class="settings__item-heading">モデル ID</div>
                    <div class="settings__item-label">
                        Chat Completions と Responses API の両方へ同じモデル ID を送信します。<br>
                    </div>
                    <v-text-field v-model="draft.model" class="settings__item-form" color="primary"
                        variant="outlined" :density="is_form_dense ? 'compact' : 'default'"
                        label="モデル ID" spellcheck="false" maxlength="237" :error-messages="model_error" />
                </div>
                <div class="settings__item">
                    <v-btn class="settings__save-button" color="background-lighten-2" variant="flat"
                        :loading="is_saving_settings" :disabled="settings_validation_error !== '' || is_busy"
                        @click="saveSettings()">
                        <Icon icon="fluent:save-20-filled" class="mr-2" width="21px" />設定を保存
                    </v-btn>
                </div>
            </template>
        </div>

        <div class="settings__content">
            <div class="settings__content-heading">
                <Icon icon="fluent:key-20-filled" width="22px" />
                <span class="ml-2">API キー</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-label">
                    API キーはサーバーの専用秘密ストアへ保存し、保存後は再表示しません。<br>
                    現在の状態: <strong :class="{'openai-compatible-auth--ok': settings.api_key_configured}">
                        {{settings.api_key_configured ? '設定済み' : '未設定'}}
                    </strong><br>
                </div>
                <v-text-field v-model="api_key_input" class="settings__item-form" color="primary"
                    variant="outlined" :density="is_form_dense ? 'compact' : 'default'" label="新しい API キー"
                    :type="api_key_showing ? 'text' : 'password'" spellcheck="false" autocomplete="off"
                    :disabled="settings_load_failed"
                    :append-inner-icon="api_key_showing ? 'mdi-eye-off' : 'mdi-eye'"
                    :error-messages="api_key_error"
                    @click:append-inner="api_key_showing = !api_key_showing" />
                <div class="openai-compatible-actions mt-3">
                    <v-btn class="settings__save-button" color="background-lighten-2" variant="flat"
                        :loading="is_saving_api_key"
                        :disabled="settings_load_failed || api_key_input.trim() === '' || api_key_error !== '' || is_busy"
                        @click="saveAPIKey()">
                        <Icon icon="fluent:key-20-filled" class="mr-2" width="21px" />API キーを設定
                    </v-btn>
                    <v-btn class="settings__save-button" color="error" variant="flat"
                        :loading="is_deleting_api_key"
                        :disabled="settings_load_failed || settings.api_key_configured === false || is_busy"
                        @click="deleteAPIKey()">
                        <Icon icon="fluent:key-reset-20-filled" class="mr-2" width="21px" />API キーを削除
                    </v-btn>
                </div>
            </div>
        </div>

        <div class="settings__content">
            <div class="settings__content-heading">
                <Icon icon="fluent:plug-connected-checkmark-20-filled" width="22px" />
                <span class="ml-2">接続試験</span>
            </div>
            <div class="settings__item">
                <v-alert v-if="connection_preflight_error !== ''" class="mb-3" color="warning" variant="tonal">
                    {{connection_preflight_error}}
                </v-alert>
                <div class="openai-compatible-actions">
                    <v-btn class="settings__save-button" color="background-lighten-2" variant="flat"
                        :loading="testing_connection_capability === 'CandidateSelection'"
                        :disabled="connection_preflight_error !== '' || testing_connection_capability !== null"
                        @click="testConnection('CandidateSelection')">
                        <Icon icon="fluent:plug-connected-checkmark-20-filled" class="mr-2" width="21px" />
                        シリーズ生成をテスト
                    </v-btn>
                    <v-btn class="settings__save-button" color="background-lighten-2" variant="flat"
                        :loading="testing_connection_capability === 'EpisodeLookup'"
                        :disabled="connection_preflight_error !== '' || testing_connection_capability !== null"
                        @click="testConnection('EpisodeLookup')">
                        <Icon icon="fluent:globe-search-20-filled" class="mr-2" width="21px" />
                        話数 Web 検索をテスト
                    </v-btn>
                </div>
                <div class="settings__item-label mt-2">
                    現在サーバーに保存されている設定で試験します。編集中の値は先に保存してください。<br>
                    話数試験では Responses API 接続、Web 検索、<code>web_search_call.action.sources</code> の
                    公開 URL、strict schema を一体で確認します。<br>
                </div>
            </div>
            <div v-if="has_connection_test_result" class="openai-compatible-results mt-3">
                <template v-for="capability in connection_test_capabilities" :key="capability.value">
                    <div v-if="connection_test_results[capability.value] !== null"
                        class="openai-compatible-result"
                        :class="{'openai-compatible-result--error':
                            connection_test_results[capability.value]?.success === false}">
                        <Icon :icon="connection_test_results[capability.value]?.success ?
                            'fluent:checkmark-circle-20-filled' : 'fluent:error-circle-20-filled'" width="21px" />
                        <div>
                            <strong>{{capability.title}}</strong>
                            <span>{{connection_test_results[capability.value]?.message}}</span>
                            <small>
                                {{connection_test_results[capability.value]?.model}} /
                                {{connection_test_results[capability.value]?.latency_ms.toLocaleString()}} ms
                            </small>
                            <ul v-if="connection_test_results[capability.value]?.checks !== null"
                                class="openai-compatible-checks">
                                <li v-for="check in episode_lookup_connection_checks" :key="check.value"
                                    :class="`openai-compatible-checks--${
                                        connectionCheck(connection_test_results[capability.value], check.value)?.status
                                            ?? 'NotRun'
                                    }`">
                                    <Icon :icon="connectionCheckIcon(
                                        connectionCheck(connection_test_results[capability.value], check.value)?.status,
                                    )" width="16px" />
                                    <span>
                                        <b>{{check.title}}</b>:
                                        {{connectionCheckStatusLabel(
                                            connectionCheck(connection_test_results[capability.value], check.value)?.status,
                                        )}} -
                                        {{connectionCheck(connection_test_results[capability.value], check.value)?.message}}
                                    </span>
                                </li>
                            </ul>
                        </div>
                    </div>
                </template>
            </div>
        </div>
    </div>
</template>

<script lang="ts" setup>

import { computed, onMounted, ref } from 'vue';

import Message from '@/message';
import AIBackend, {
    type AIBackendConnectionCapability,
    type IAIBackendConnectionTestCheck,
    type IAIBackendConnectionTestResult,
    type IAIBackendEpisodeLookupConnectionChecks,
    type IOpenAICompatibleSettings,
    type OpenAICompatibleSlot,
} from '@/services/AIBackend';
import Utils from '@/utils';


type ConnectionTestResults = Record<AIBackendConnectionCapability, IAIBackendConnectionTestResult | null>;

const props = withDefaults(defineProps<{
    backendSlot?: OpenAICompatibleSlot;
}>(), {
    backendSlot: 1,
});

const OPENAI_COMPATIBLE_MODEL_MAX_LENGTH = 237;
const backend_title = computed(() => props.backendSlot === 1 ? 'OpenAI 互換 API' : 'OpenAI 互換 API 2');
const connection_test_capabilities: {title: string; value: AIBackendConnectionCapability;}[] = [
    {title: 'シリーズ情報生成', value: 'CandidateSelection'},
    {title: '話数 Web 検索', value: 'EpisodeLookup'},
];
const episode_lookup_connection_checks: {
    title: string;
    value: keyof IAIBackendEpisodeLookupConnectionChecks;
}[] = [
    {title: 'バックエンド接続', value: 'backend_connection'},
    {title: 'Web 検索の実行', value: 'web_search'},
    {title: '検索元 URL', value: 'source_url'},
    {title: 'strict schema', value: 'strict_schema'},
    {title: 'timeout / cancel', value: 'timeout_cancel'},
    {title: 'permission policy', value: 'permission_policy'},
];
const connection_check_status_labels: Record<string, string> = {
    Passed: '確認済み',
    Failed: '失敗',
    NotRun: '未実行',
    NotApplicable: '対象外',
};

const is_form_dense = Utils.isSmartphoneHorizontal();
const is_loading = ref(true);
const settings_load_failed = ref(false);
const settings = ref<IOpenAICompatibleSettings>({
    api_base_url: null,
    model: null,
    api_key_configured: false,
});
const draft = ref({api_base_url: '', model: ''});
const api_key_input = ref('');
const api_key_showing = ref(false);
const is_saving_settings = ref(false);
const is_saving_api_key = ref(false);
const is_deleting_api_key = ref(false);
const testing_connection_capability = ref<AIBackendConnectionCapability | null>(null);
const connection_test_results = ref<ConnectionTestResults>({
    CandidateSelection: null,
    EpisodeLookup: null,
});

const is_busy = computed(() =>
    is_saving_settings.value || is_saving_api_key.value || is_deleting_api_key.value
    || testing_connection_capability.value !== null,
);
const api_base_url_error = computed(() => {
    const value = draft.value.api_base_url.trim();
    if (value.length > 2048) return 'API ベース URL は 2048 文字以内で入力してください。';
    if (value !== '' && /^https?:\/\//i.test(value) === false) return 'HTTP(S) URL を入力してください。';
    return '';
});
const model_error = computed(() =>
    draft.value.model.trim().length > OPENAI_COMPATIBLE_MODEL_MAX_LENGTH
        ? `モデル ID は ${OPENAI_COMPATIBLE_MODEL_MAX_LENGTH} 文字以内で入力してください。`
        : '',
);
const api_key_error = computed(() => {
    const value = api_key_input.value.trim();
    return value.length > 8192 ? 'API キーが長すぎます。' : '';
});
const settings_validation_error = computed(() => api_base_url_error.value || model_error.value);
const connection_preflight_error = computed(() => {
    if (settings_load_failed.value) return '保存済み設定を取得できませんでした。再試行してください。';
    if (settings.value.api_base_url === null || settings.value.model === null) {
        return 'API ベース URL とモデルを保存してください。';
    }
    if (settings.value.api_key_configured === false) return 'API キーを設定してください。';
    return '';
});
const has_connection_test_result = computed(() =>
    connection_test_results.value.CandidateSelection !== null
    || connection_test_results.value.EpisodeLookup !== null,
);

async function loadSettings(): Promise<void> {
    is_loading.value = true;
    const fetched = await AIBackend.fetchOpenAICompatibleSettings(props.backendSlot);
    if (fetched !== null) {
        settings.value = fetched;
        draft.value = {
            api_base_url: fetched.api_base_url ?? '',
            model: fetched.model ?? '',
        };
        settings_load_failed.value = false;
    } else {
        settings_load_failed.value = true;
    }
    is_loading.value = false;
}

function clearConnectionTestResults(): void {
    connection_test_results.value = {
        CandidateSelection: null,
        EpisodeLookup: null,
    };
}

async function saveSettings(): Promise<void> {
    if (settings_validation_error.value !== '' || is_busy.value) return;
    is_saving_settings.value = true;
    try {
        const saved = await AIBackend.updateOpenAICompatibleSettings({
            api_base_url: draft.value.api_base_url.trim() || null,
            model: draft.value.model.trim() || null,
        }, props.backendSlot);
        if (saved) {
            clearConnectionTestResults();
            Message.success(`${backend_title.value} 設定を保存しました。`);
            await loadSettings();
        }
    } finally {
        is_saving_settings.value = false;
    }
}

async function saveAPIKey(): Promise<void> {
    if (api_key_input.value.trim() === '' || api_key_error.value !== '' || is_busy.value) return;
    is_saving_api_key.value = true;
    try {
        const saved = await AIBackend.setOpenAICompatibleAPIKey(api_key_input.value.trim(), props.backendSlot);
        if (saved) {
            clearConnectionTestResults();
            api_key_input.value = '';
            api_key_showing.value = false;
            Message.success(`${backend_title.value} キーを設定しました。`);
            await loadSettings();
        }
    } finally {
        is_saving_api_key.value = false;
    }
}

async function deleteAPIKey(): Promise<void> {
    if (settings.value.api_key_configured === false || is_busy.value) return;
    is_deleting_api_key.value = true;
    try {
        const deleted = await AIBackend.deleteOpenAICompatibleAPIKey(props.backendSlot);
        if (deleted) {
            clearConnectionTestResults();
            api_key_input.value = '';
            api_key_showing.value = false;
            Message.success(`${backend_title.value} キーを削除しました。`);
            await loadSettings();
        }
    } finally {
        is_deleting_api_key.value = false;
    }
}

function connectionCheck(
    result: IAIBackendConnectionTestResult | null,
    check_name: keyof IAIBackendEpisodeLookupConnectionChecks,
): IAIBackendConnectionTestCheck | null {
    return result?.checks?.[check_name] ?? null;
}

function connectionCheckStatusLabel(status: string | undefined): string {
    return status === undefined ? '未実行' : connection_check_status_labels[status] ?? status;
}

function connectionCheckIcon(status: string | undefined): string {
    if (status === 'Passed') return 'fluent:checkmark-circle-20-filled';
    if (status === 'Failed') return 'fluent:error-circle-20-filled';
    if (status === 'NotApplicable') return 'fluent:subtract-circle-20-filled';
    return 'fluent:clock-20-filled';
}

async function testConnection(capability: AIBackendConnectionCapability): Promise<void> {
    if (connection_preflight_error.value !== '' || testing_connection_capability.value !== null) return;
    testing_connection_capability.value = capability;
    connection_test_results.value[capability] = null;
    try {
        const result = await AIBackend.testOpenAICompatibleConnection(capability, props.backendSlot);
        connection_test_results.value[capability] = result;
        const capability_title = connection_test_capabilities.find(item => item.value === capability)?.title
            ?? capability;
        if (result?.success) {
            Message.success(`${capability_title}の接続試験に成功しました。`);
        } else if (result !== null) {
            Message.error(`${capability_title}の接続試験に失敗しました。\n${result.message}`);
        }
    } finally {
        testing_connection_capability.value = null;
    }
}

onMounted(() => void loadSettings());

</script>

<style lang="scss" scoped>

.openai-compatible-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
}

.openai-compatible-auth--ok {
    color: rgb(var(--v-theme-success-readable));
}

.openai-compatible-results {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.openai-compatible-result {
    display: flex;
    align-items: flex-start;
    gap: 9px;
    padding: 10px 12px;
    border-radius: 6px;
    color: rgb(var(--v-theme-success-readable));
    background: rgba(var(--v-theme-success), 0.1);

    > div {
        display: flex;
        flex-direction: column;
        min-width: 0;
    }

    strong,
    span,
    small {
        overflow-wrap: anywhere;
    }

    strong {
        color: rgb(var(--v-theme-text));
        font-size: 12.5px;
    }

    span {
        margin-top: 2px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 11.5px;
    }

    small {
        margin-top: 3px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 10.5px;
    }

    &--error {
        color: rgb(var(--v-theme-error-readable));
        background: rgba(var(--v-theme-error), 0.1);
    }
}

.openai-compatible-checks {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin: 8px 0 0;
    padding: 0;
    list-style: none;

    li {
        display: flex;
        align-items: flex-start;
        gap: 5px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 11px;
    }

    &--Passed {
        color: rgb(var(--v-theme-success-readable)) !important;
    }

    &--Failed {
        color: rgb(var(--v-theme-error-readable)) !important;
    }
}

</style>
