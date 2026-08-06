<template>
    <SettingsBase>
        <h2 class="settings__heading">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:bot-20-filled" width="24px" />
            <span class="ml-2">AIバックエンド</span>
        </h2>
        <div class="settings__description">
            録画シリーズ AI などで使う OpenCode 経由の外部モデル接続を管理します。<br>
            API キーはサーバー上に暗号化せず安全な秘密ストアへ保存され、画面には表示されません。<br>
            この設定はすべてのユーザーと端末で共有され、管理者だけが変更できます。<br>
        </div>

        <v-progress-linear v-if="is_loading" class="mt-5" color="primary" indeterminate rounded />

        <div v-else-if="authorization_error !== null" class="ai-backend-access-state">
            <Icon icon="fluent:shield-error-20-filled" width="28px" />
            <span v-if="authorization_error === 'AdminRequired'">この設定を表示するには管理者権限が必要です。</span>
            <span v-else>ユーザー情報を取得できませんでした。ページを再読み込みしてください。</span>
        </div>

        <template v-else>
            <div class="settings__content" :class="{'settings__content--disabled': is_busy}">
                <div class="settings__content-heading">
                    <Icon icon="fluent:heart-pulse-20-filled" width="22px" />
                    <span class="ml-2">OpenCode serve</span>
                </div>
                <div class="settings__item">
                    <div class="settings__item-heading">状態</div>
                    <div class="settings__item-label" v-if="health">
                        <span :class="health.available ? 'ai-backend-status--ok' : 'ai-backend-status--ng'">
                            {{ health.available ? '利用可能' : '利用不可' }}
                        </span>
                        <template v-if="health.available">
                            / version {{ health.version || '不明' }}
                            （pin {{ health.pinned_version }}） / {{ health.host }}:{{ health.port }}
                        </template>
                        <template v-else>
                            。サーバーログの opencode-serve を確認してください。
                        </template>
                    </div>
                    <div class="settings__item-label" v-else>状態を取得できませんでした。</div>
                    <v-btn class="mt-3" variant="tonal" color="primary" size="small"
                        :disabled="is_busy" @click="reloadAll()">
                        再読込
                    </v-btn>
                </div>
            </div>

            <div class="settings__content mt-6" :class="{'settings__content--disabled': is_busy}">
                <div class="settings__content-heading d-flex align-center justify-space-between">
                    <div class="d-flex align-center">
                        <Icon icon="fluent:cloud-20-filled" width="22px" />
                        <span class="ml-2">登録済み service</span>
                    </div>
                    <v-btn color="primary" variant="flat" size="small" :disabled="is_busy" @click="openCreateDialog()">
                        <Icon icon="fluent:add-20-filled" class="mr-1" width="18px" />
                        追加
                    </v-btn>
                </div>

                <div v-if="services.length === 0" class="settings__item-label mt-3">
                    まだ service がありません。「追加」から OpenCode provider を登録してください。
                </div>

                <div v-for="service in services" :key="service.service_id" class="ai-backend-card">
                    <div class="ai-backend-card__title">{{ service.service_name }}</div>
                    <div class="ai-backend-card__meta">
                        {{ service.opencode_provider_id }} / {{ service.opencode_model_id }}<br>
                        auth: {{ service.auth_mode }} · billing: {{ service.billing_mode }} ·
                        認証: {{ service.auth_configured ? '設定済み' : '未設定' }}
                        <template v-if="service.auth_mode === 'OAuthSubscription'">
                            · OAuth: {{ service.oauth_connected ? '接続中' : '未接続' }}
                        </template>
                    </div>
                    <div class="ai-backend-card__actions">
                        <v-btn size="small" variant="tonal" color="primary" :disabled="is_busy"
                            @click="openEditDialog(service)">編集</v-btn>
                        <v-btn v-if="service.auth_mode === 'ApiKey'" size="small" variant="tonal"
                            color="secondary" :disabled="is_busy" @click="openKeyDialog(service)">
                            APIキー
                        </v-btn>
                        <v-btn v-if="service.auth_mode === 'OAuthSubscription'" size="small" variant="tonal"
                            color="secondary" :disabled="is_busy" @click="runOAuthStart(service)">
                            OAuth開始
                        </v-btn>
                        <v-btn v-if="service.auth_mode === 'OAuthSubscription' && service.oauth_connected"
                            size="small" variant="tonal" color="warning" :disabled="is_busy"
                            @click="runOAuthDisconnect(service)">
                            OAuth切断
                        </v-btn>
                        <v-btn size="small" variant="tonal" color="primary" :disabled="is_busy || !health?.available"
                            :loading="testing_service_id === service.service_id"
                            @click="runConnectionTest(service)">
                            接続試験
                        </v-btn>
                        <v-btn size="small" variant="tonal" color="error" :disabled="is_busy"
                            @click="confirmDelete(service)">削除</v-btn>
                    </div>
                    <div v-if="test_results[service.service_id]" class="ai-backend-card__test"
                        :class="test_results[service.service_id]!.success
                            ? 'ai-backend-card__test--ok' : 'ai-backend-card__test--ng'">
                        {{ test_results[service.service_id]!.success ? '成功' : '失敗' }}:
                        {{ test_results[service.service_id]!.message }}
                        <template v-if="test_results[service.service_id]!.latency_ms">
                            （{{ test_results[service.service_id]!.latency_ms }} ms）
                        </template>
                    </div>
                </div>
            </div>
        </template>

        <!-- 追加・編集ダイアログ -->
        <v-dialog v-model="edit_dialog" max-width="560" persistent>
            <v-card>
                <v-card-title>{{ editing_service_id ? 'service を編集' : 'service を追加' }}</v-card-title>
                <v-card-text>
                    <v-text-field v-model="form.service_name" label="表示名" variant="outlined"
                        color="primary" :density="is_form_dense ? 'compact' : 'default'" class="mb-2" />
                    <v-text-field v-model="form.opencode_provider_id" label="OpenCode provider ID"
                        hint="例: deepseek / openai / xai" persistent-hint variant="outlined" color="primary"
                        :density="is_form_dense ? 'compact' : 'default'" class="mb-2" spellcheck="false" />
                    <v-text-field v-model="form.opencode_model_id" label="OpenCode model ID"
                        hint="例: deepseek-chat" persistent-hint variant="outlined" color="primary"
                        :density="is_form_dense ? 'compact' : 'default'" class="mb-2" spellcheck="false" />
                    <v-select v-model="form.auth_mode" :items="auth_mode_items" item-title="title" item-value="value"
                        label="認証方式" variant="outlined" color="primary"
                        :density="is_form_dense ? 'compact' : 'default'" class="mb-2" />
                    <v-select v-model="form.billing_mode" :items="billing_mode_items" item-title="title"
                        item-value="value" label="課金モード" variant="outlined" color="primary"
                        :density="is_form_dense ? 'compact' : 'default'" class="mb-2" />
                    <v-text-field v-if="form.auth_mode === 'NoneLocal' || form.api_base_url"
                        v-model="form.api_base_url" label="API ベース URL（任意 / ローカル時必須）"
                        variant="outlined" color="primary" :density="is_form_dense ? 'compact' : 'default'"
                        class="mb-2" spellcheck="false" />
                    <template v-if="form.auth_mode === 'VertexAdc'">
                        <v-text-field v-model="form.google_cloud_project" label="Google Cloud project"
                            variant="outlined" color="primary" :density="is_form_dense ? 'compact' : 'default'"
                            class="mb-2" />
                        <v-text-field v-model="form.google_cloud_location" label="Google Cloud location"
                            variant="outlined" color="primary" :density="is_form_dense ? 'compact' : 'default'"
                            class="mb-2" />
                    </template>
                    <v-text-field v-if="form.billing_mode === 'Metered'"
                        v-model="form.monthly_cost_limit_usd" label="月次推定料金上限 USD（空欄=なし）"
                        variant="outlined" color="primary" :density="is_form_dense ? 'compact' : 'default'"
                        class="mb-2" type="number" />
                    <v-text-field v-if="form.billing_mode === 'Metered' || form.billing_mode === 'Local'"
                        v-model.number="form.monthly_token_limit" label="月次 token 上限（空欄=なし）"
                        variant="outlined" color="primary" :density="is_form_dense ? 'compact' : 'default'"
                        type="number" />
                </v-card-text>
                <v-card-actions>
                    <v-spacer />
                    <v-btn variant="text" :disabled="is_busy" @click="edit_dialog = false">キャンセル</v-btn>
                    <v-btn color="primary" variant="flat" :loading="is_saving" :disabled="is_busy"
                        @click="saveService()">保存</v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>

        <!-- API キーダイアログ -->
        <v-dialog v-model="key_dialog" max-width="480" persistent>
            <v-card>
                <v-card-title>API キー</v-card-title>
                <v-card-text>
                    <div class="settings__item-label mb-3">
                        キー本体は保存後に再表示できません。ローテーション時は新しいキーを入力してください。
                    </div>
                    <v-text-field v-model="api_key_input" label="API キー" variant="outlined" color="primary"
                        :type="api_key_showing ? 'text' : 'password'"
                        :density="is_form_dense ? 'compact' : 'default'" spellcheck="false" autocomplete="off"
                        :append-inner-icon="api_key_showing ? 'mdi-eye-off' : 'mdi-eye'"
                        @click:append-inner="api_key_showing = !api_key_showing" />
                </v-card-text>
                <v-card-actions>
                    <v-btn v-if="key_target?.auth_configured" color="error" variant="text" :disabled="is_busy"
                        @click="deleteKey()">キー削除</v-btn>
                    <v-spacer />
                    <v-btn variant="text" :disabled="is_busy" @click="key_dialog = false">閉じる</v-btn>
                    <v-btn color="primary" variant="flat" :loading="is_saving_key" :disabled="is_busy || !api_key_input"
                        @click="saveKey()">設定</v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>

        <!-- 削除確認 -->
        <v-dialog v-model="delete_dialog" max-width="420">
            <v-card>
                <v-card-title>service を削除</v-card-title>
                <v-card-text>
                    「{{ delete_target?.service_name }}」を削除しますか？録画シリーズから参照中の場合は拒否されます。
                </v-card-text>
                <v-card-actions>
                    <v-spacer />
                    <v-btn variant="text" @click="delete_dialog = false">キャンセル</v-btn>
                    <v-btn color="error" variant="flat" :loading="is_deleting" @click="deleteService()">削除</v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>
    </SettingsBase>
</template>

<script lang="ts" setup>

import { computed, onMounted, ref } from 'vue';

import Message from '@/message';
import AIBackend, {
    type AIAuthMode,
    type AIBillingMode,
    type IAIBackendConnectionTestResult,
    type IAIBackendService,
    type IOpenCodeAvailability,
} from '@/services/AIBackend';
import useUserStore from '@/stores/UserStore';
import Utils from '@/utils';
import SettingsBase from '@/views/Settings/Base.vue';


interface ServiceForm {
    service_name: string;
    opencode_provider_id: string;
    opencode_model_id: string;
    auth_mode: AIAuthMode;
    billing_mode: AIBillingMode;
    api_base_url: string;
    google_cloud_project: string;
    google_cloud_location: string;
    monthly_cost_limit_usd: string;
    monthly_token_limit: number | null;
}

const emptyForm = (): ServiceForm => ({
    service_name: '',
    opencode_provider_id: 'deepseek',
    opencode_model_id: 'deepseek-chat',
    auth_mode: 'ApiKey',
    billing_mode: 'Metered',
    api_base_url: '',
    google_cloud_project: '',
    google_cloud_location: '',
    monthly_cost_limit_usd: '',
    monthly_token_limit: null,
});

const auth_mode_items = [
    {title: 'API キー', value: 'ApiKey'},
    {title: 'OAuth（サブスク）', value: 'OAuthSubscription'},
    {title: 'Vertex ADC', value: 'VertexAdc'},
    {title: 'ローカル（キーなし）', value: 'NoneLocal'},
];
const billing_mode_items = [
    {title: 'Metered（従量・月次上限対象）', value: 'Metered'},
    {title: 'Subscription（上限対象外）', value: 'Subscription'},
    {title: 'Local（token 上限のみ任意）', value: 'Local'},
];

const is_form_dense = Utils.isSmartphoneHorizontal();
const user_store = useUserStore();

const is_loading = ref(true);
const authorization_error = ref<'AdminRequired' | 'UserUnavailable' | null>(null);
const health = ref<IOpenCodeAvailability | null>(null);
const services = ref<IAIBackendService[]>([]);
const test_results = ref<Record<string, IAIBackendConnectionTestResult | null>>({});
const testing_service_id = ref<string | null>(null);

const edit_dialog = ref(false);
const editing_service_id = ref<string | null>(null);
const form = ref<ServiceForm>(emptyForm());
const is_saving = ref(false);

const key_dialog = ref(false);
const key_target = ref<IAIBackendService | null>(null);
const api_key_input = ref('');
const api_key_showing = ref(false);
const is_saving_key = ref(false);

const delete_dialog = ref(false);
const delete_target = ref<IAIBackendService | null>(null);
const is_deleting = ref(false);

const is_busy = computed(() =>
    is_saving.value || is_saving_key.value || is_deleting.value || testing_service_id.value !== null,
);

async function reloadAll(): Promise<void> {
    const [health_result, list] = await Promise.all([
        AIBackend.fetchHealth(),
        AIBackend.fetchServices(),
    ]);
    health.value = health_result;
    if (list !== null) {
        services.value = list;
    }
}

function openCreateDialog(): void {
    editing_service_id.value = null;
    form.value = emptyForm();
    edit_dialog.value = true;
}

function openEditDialog(service: IAIBackendService): void {
    editing_service_id.value = service.service_id;
    form.value = {
        service_name: service.service_name,
        opencode_provider_id: service.opencode_provider_id,
        opencode_model_id: service.opencode_model_id,
        auth_mode: service.auth_mode,
        billing_mode: service.billing_mode,
        api_base_url: service.api_base_url || '',
        google_cloud_project: service.google_cloud_project || '',
        google_cloud_location: service.google_cloud_location || '',
        monthly_cost_limit_usd: service.monthly_cost_limit_usd || '',
        monthly_token_limit: service.monthly_token_limit,
    };
    edit_dialog.value = true;
}

async function saveService(): Promise<void> {
    const f = form.value;
    if (f.service_name.trim() === '' || f.opencode_provider_id.trim() === '' || f.opencode_model_id.trim() === '') {
        Message.warning('表示名・provider ID・model ID は必須です。');
        return;
    }
    is_saving.value = true;
    try {
        const body = {
            service_name: f.service_name.trim(),
            opencode_provider_id: f.opencode_provider_id.trim(),
            opencode_model_id: f.opencode_model_id.trim(),
            auth_mode: f.auth_mode,
            billing_mode: f.billing_mode,
            api_base_url: f.api_base_url.trim() || null,
            google_cloud_project: f.google_cloud_project.trim() || null,
            google_cloud_location: f.google_cloud_location.trim() || null,
            monthly_cost_limit_usd: f.monthly_cost_limit_usd.trim() || null,
            // v-model.number の空欄は '' になり Number('')===0 になるため、
            // 空文字・0 は「上限なし」として null を送る。
            monthly_token_limit: (
                f.monthly_token_limit === null ||
                f.monthly_token_limit === ('' as unknown as number) ||
                String(f.monthly_token_limit).trim() === '' ||
                Number.isNaN(Number(f.monthly_token_limit)) ||
                Number(f.monthly_token_limit) === 0
            ) ? null : Number(f.monthly_token_limit),
        };
        let result: IAIBackendService | null = null;
        if (editing_service_id.value === null) {
            result = await AIBackend.createService(body);
        } else {
            result = await AIBackend.updateService(editing_service_id.value, {
                ...body,
                clear_api_base_url: body.api_base_url === null,
                clear_monthly_cost_limit_usd: body.monthly_cost_limit_usd === null,
                clear_monthly_token_limit: body.monthly_token_limit === null,
            });
        }
        if (result !== null) {
            Message.success(editing_service_id.value ? 'service を更新しました。' : 'service を追加しました。');
            edit_dialog.value = false;
            await reloadAll();
        }
    } finally {
        is_saving.value = false;
    }
}

function openKeyDialog(service: IAIBackendService): void {
    key_target.value = service;
    api_key_input.value = '';
    api_key_showing.value = false;
    key_dialog.value = true;
}

async function saveKey(): Promise<void> {
    if (key_target.value === null || api_key_input.value.trim() === '') return;
    is_saving_key.value = true;
    try {
        const ok = await AIBackend.setAPIKey(key_target.value.service_id, api_key_input.value.trim());
        if (ok) {
            Message.success('API キーを設定しました。');
            api_key_input.value = '';
            key_dialog.value = false;
            await reloadAll();
        }
    } finally {
        is_saving_key.value = false;
    }
}

async function deleteKey(): Promise<void> {
    if (key_target.value === null) return;
    is_saving_key.value = true;
    try {
        const ok = await AIBackend.deleteAPIKey(key_target.value.service_id);
        if (ok) {
            Message.success('API キーを削除しました。');
            key_dialog.value = false;
            await reloadAll();
        }
    } finally {
        is_saving_key.value = false;
    }
}

async function runOAuthStart(service: IAIBackendService): Promise<void> {
    const authorize = await AIBackend.startOAuth(service.service_id);
    if (authorize !== null) {
        Message.info('OAuth 開始を要求しました（headless 未対応 provider は後日対応）。');
        await reloadAll();
    }
}

async function runOAuthDisconnect(service: IAIBackendService): Promise<void> {
    const ok = await AIBackend.disconnectOAuth(service.service_id);
    if (ok) {
        Message.success('OAuth を切断しました。');
        await reloadAll();
    }
}

async function runConnectionTest(service: IAIBackendService): Promise<void> {
    testing_service_id.value = service.service_id;
    try {
        const result = await AIBackend.testConnection(service.service_id, 'CandidateSelection');
        if (result !== null) {
            test_results.value = {...test_results.value, [service.service_id]: result};
            if (result.success) {
                Message.success('接続試験に成功しました。');
            } else {
                Message.warning(result.message || '接続試験に失敗しました。');
            }
        }
    } finally {
        testing_service_id.value = null;
    }
}

function confirmDelete(service: IAIBackendService): void {
    delete_target.value = service;
    delete_dialog.value = true;
}

async function deleteService(): Promise<void> {
    if (delete_target.value === null) return;
    is_deleting.value = true;
    try {
        const ok = await AIBackend.deleteService(delete_target.value.service_id);
        if (ok) {
            Message.success('service を削除しました。');
            delete_dialog.value = false;
            await reloadAll();
        }
    } finally {
        is_deleting.value = false;
    }
}

onMounted(async () => {
    const user = await user_store.fetchUser();
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
    await reloadAll();
    is_loading.value = false;
});

</script>

<style lang="scss" scoped>
.ai-backend-access-state {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-top: 24px;
    color: rgb(var(--v-theme-text-darken-1));
}
.ai-backend-status--ok { color: #176b4d; font-weight: 700; }
.ai-backend-status--ng { color: #9b2c2c; font-weight: 700; }
.ai-backend-card {
    margin-top: 14px;
    padding: 14px 16px;
    border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
    border-radius: 10px;
    background: rgba(var(--v-theme-surface-light), 0.35);
}
.ai-backend-card__title {
    font-weight: 700;
    font-size: 1.02rem;
}
.ai-backend-card__meta {
    margin-top: 4px;
    font-size: 0.9rem;
    color: rgb(var(--v-theme-text-darken-1));
    line-height: 1.55;
}
.ai-backend-card__actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 12px;
}
.ai-backend-card__test {
    margin-top: 10px;
    font-size: 0.88rem;
    line-height: 1.45;
}
.ai-backend-card__test--ok { color: #176b4d; }
.ai-backend-card__test--ng { color: #9b2c2c; }
</style>
