<template>
    <div class="acp-backend-section">
        <div class="settings__content">
            <div class="settings__content-heading">
                <Icon :icon="section_icon" width="22px" />
                <span class="ml-2">ACP / {{provider_display_name}}</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-label">
                    <template v-if="provider === 'grok'">
                        ホスト上の Grok Build CLI を起動して AI 処理を行います。<br>
                        モデルは <code>grok-4.5</code> 固定で、推論の深さだけを指定できます。初期値は <strong>High</strong> です。<br>
                    </template>
                    <template v-else>
                        ホスト上の Codex CLI を起動して AI 処理を行います。<br>
                        モデルと推論の深さは分離して指定できます。初期値は <strong>GPT-5.6 Luna</strong> / <strong>Medium</strong> です。<br>
                    </template>
                </div>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">モデル</div>
                <div class="settings__item-label">
                    <template v-if="provider === 'grok'">
                        Grok Build の ACP モデルは <code>grok-4.5</code> 固定です。<br>
                    </template>
                    <template v-else>
                        Codex のモデル系統を選びます。深さは下の「推論の深さ」で別指定します。<br>
                    </template>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="acp_model_preset_items"
                    item-title="title"
                    item-value="value"
                    :disabled="provider === 'grok'"
                    :error-messages="acp_model_error"
                    :model-value="acp_model_selection"
                    @update:model-value="setAcpModelSelection" />
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">推論の深さ</div>
                <div class="settings__item-label">
                    モデル名とは別に、思考の深さを選びます。<br>
                    <template v-if="provider === 'codex'">
                        Codex は Low〜Ultra。ただし <strong>Ultra</strong> は GPT-5.6 Sol 系統だけ選べます。<br>
                        Sol 以外で保存済みの Ultra は、サーバー側で <strong>Max</strong> に自動補正します。<br>
                    </template>
                    <template v-else>
                        Grok は Low / Medium / High。<br>
                    </template>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="acp_reasoning_effort_options"
                    item-title="title"
                    item-value="value"
                    :model-value="acp_reasoning_effort_selection"
                    @update:model-value="setAcpReasoningEffortSelection" />
                <div class="settings__item-label mt-2" v-if="acp_wire_preview">
                    適用プレビュー: <code>{{acp_wire_preview}}</code>
                </div>
            </div>
            <div class="settings__item settings__item--switch" v-if="provider === 'codex'">
                <label class="settings__item-heading" :for="`acp_codex_fast_mode_${provider}`">
                    Codex Fast モードを有効化
                </label>
                <label class="settings__item-label" :for="`acp_codex_fast_mode_${provider}`">
                    Codex の Fast service tier を使い、対応モデルの処理速度を上げます。<br>
                    通常よりクレジット消費が増えるため、必要な場合だけ有効にしてください。<br>
                    Fast モードは Codex 専用 profile の設定として適用されます。<br>
                </label>
                <v-switch :id="`acp_codex_fast_mode_${provider}`" class="settings__item-switch" color="primary"
                    hide-details :model-value="draft_settings.codex_fast_mode_enabled"
                    @update:model-value="updateDraft({codex_fast_mode_enabled: $event === true})" />
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">無通信タイムアウト（秒）</div>
                <div class="settings__item-label">
                    シリーズ生成・話数 Web 検索・接続試験で、ACP からの進捗や応答が途絶えてから打ち切るまでの秒数です。<br>
                    推論や結果整形の途中でも、thought / tool update などが届いている間は延長されます。30〜600 秒の間で指定してください。<br>
                    進捗が継続している場合でも、サーバー保護のため総実行時間（他の ACP 実行待ちを含む）が {{ acp_hard_timeout_minutes }} 分を超えると停止します。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" type="number"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :min="30" :max="600" :step="1"
                    :error-messages="acp_timeout_error"
                    :model-value="draft_settings.timeout_sec"
                    @update:model-value="updateDraft({timeout_sec: Number($event)})" />
            </div>
            <div class="settings__item">
                <v-alert v-if="validation_error" class="mb-3" color="warning" variant="tonal">
                    {{validation_error}}
                </v-alert>
                <div class="acp-backend-actions mt-3">
                    <v-btn class="settings__save-button" color="background-lighten-2" variant="flat"
                        :loading="saving"
                        :disabled="validation_error !== ''"
                        @click="saveSettings()">
                        <Icon icon="fluent:save-20-filled" class="mr-2" width="21px" />設定を保存
                    </v-btn>
                </div>
            </div>
        </div>

        <div class="settings__content">
            <div class="settings__content-heading">
                <Icon icon="fluent:key-20-filled" width="22px" />
                <span class="ml-2">認証</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-label">
                    <template v-if="provider === 'codex'">
                        ホストで <code>codex login</code> を実行し、Compose override に auth.json の絶対パスを指定します。<br>
                        OS keyring だけを利用している場合は <code>cli_auth_credentials_store = "file"</code> を設定してください。<br>
                    </template>
                    <template v-else>
                        ホストで <code>grok login</code> を実行します。ブラウザーを使えない場合は
                        <code>grok login --device-auth</code> を使用できます。<br>
                        Compose override には生成された auth.json の絶対パスを指定します。<br>
                    </template>
                </div>
                <div class="acp-backend-auth-status mt-3">
                    <span>
                        ホスト認証ファイル:
                        <strong :class="{'acp-backend-auth-status--ok': host_auth_available}">
                            {{host_auth_available ? '検出済み' : '未検出'}}
                        </strong>
                    </span>
                    <span>
                        KonomiTV-BS4K への取り込み:
                        <strong :class="{'acp-backend-auth-status--ok': auth_imported}">
                            {{auth_imported ? '取り込み済み' : '未取り込み'}}
                        </strong>
                    </span>
                    <small v-if="auth_imported_at !== null">
                        最終取り込み: {{formatAuthImportedAt(auth_imported_at)}}
                    </small>
                    <small v-if="auth_in_use">
                        {{provider_display_name}} の AI 処理が認証を使用中です。完了するまで再取り込み・削除はできません。
                    </small>
                </div>
                <div class="acp-backend-actions mt-3">
                    <v-btn class="settings__save-button" color="background-lighten-2" variant="flat"
                        :disabled="host_auth_available !== true || auth_in_use === true"
                        :loading="is_updating_acp_authentication"
                        @click="openACPAuthenticationDialog('Import')">
                        <Icon icon="fluent:key-20-filled" class="mr-2" width="21px" />
                        {{auth_imported ? '認証を再取り込み' : '認証を取り込む'}}
                    </v-btn>
                    <v-btn class="settings__save-button" color="error" variant="flat"
                        :disabled="auth_imported !== true || auth_in_use === true"
                        :loading="is_updating_acp_authentication"
                        @click="openACPAuthenticationDialog('Delete')">
                        <Icon icon="fluent:key-reset-20-filled" class="mr-2" width="21px" />取り込んだ認証を削除
                    </v-btn>
                </div>
                <v-alert class="mb-4 mt-4" color="warning" variant="tonal">
                    この認証はKonomiTV-BS4KのバックグラウンドAI処理全体で共有する管理者資格情報です。
                    KonomiTV-BS4K の一般ユーザーごとの認証ではありません。<br>
                    ホストで再ログインしても自動反映されないため、Codex / Grok は再取り込みが必要です。
                    ホストで logout しても取り込み済みコピーは自動削除されません。直ちに無効化する場合は、
                    この画面でコピーを削除し、プロバイダー側でもセッションを失効してください。
                </v-alert>
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
                <div class="acp-backend-actions mt-3">
                    <v-btn class="settings__save-button" color="background-lighten-2" variant="flat"
                        :loading="testing_connection_capability === 'CandidateSelection'"
                        :disabled="connection_preflight_error !== '' || testing_connection_capability !== null"
                        @click="testConnection('CandidateSelection')">
                        <Icon icon="fluent:plug-connected-checkmark-20-filled" class="mr-2" width="21px" />シリーズ生成をテスト
                    </v-btn>
                    <v-btn class="settings__save-button" color="background-lighten-2" variant="flat"
                        :loading="testing_connection_capability === 'EpisodeLookup'"
                        :disabled="connection_preflight_error !== '' || testing_connection_capability !== null"
                        @click="testConnection('EpisodeLookup')">
                        <Icon icon="fluent:globe-search-20-filled" class="mr-2" width="21px" />話数 Web 検索をテスト
                    </v-btn>
                </div>
                <div class="settings__item-label mt-2">
                    現在サーバーに保存されている設定で接続を確認します。編集内容を反映するには、先に「設定を保存」してください。<br>
                    話数検索の接続テストでは、Web 検索の実行・出典取得・構造化結果を一体で確認します。<br>
                    シリーズ生成だけ成功した場合でも、話数検索を利用できるとは限りません。<br>
                </div>
            </div>
            <div v-if="has_connection_test_result" class="acp-backend-connection-results mt-3">
                <template v-for="capability in connection_test_capabilities" :key="capability.value">
                    <div v-if="connection_test_results[capability.value] !== null"
                        class="acp-backend-connection-result"
                        :class="{'acp-backend-connection-result--error':
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
                                class="acp-backend-connection-checks">
                                <li v-for="check in episode_lookup_connection_checks" :key="check.value"
                                    :class="`acp-backend-connection-checks--${
                                        connectionCheck(connection_test_results[capability.value], check.value)?.status ?? 'NotRun'
                                    }`">
                                    <Icon :icon="connectionCheckIcon(
                                        connectionCheck(connection_test_results[capability.value], check.value)?.status,
                                    )" width="16px" />
                                    <span>
                                        <b>{{check.title}}</b>:
                                        {{connectionCheckStatusLabel(
                                            connectionCheck(connection_test_results[capability.value], check.value)?.status,
                                        )}} —
                                        {{connectionCheck(connection_test_results[capability.value], check.value)?.message}}
                                    </span>
                                </li>
                            </ul>
                        </div>
                    </div>
                </template>
            </div>
        </div>

        <v-dialog v-model="acp_authentication_dialog" max-width="560">
            <v-card class="acp-backend-dialog">
                <v-card-title>
                    {{pending_acp_authentication_action === 'Delete' ?
                        '取り込んだ ACP 認証を削除' :
                        'ホストの ACP 認証を取り込む'}}
                </v-card-title>
                <v-card-text>
                    <template v-if="pending_acp_authentication_action === 'Import'">
                        Compose で読み取り専用 mount した
                        {{provider_display_name}}
                        の auth.json を、KonomiTV-BS4K 専用プロファイルへ取り込みます。<br>
                        既存の取り込み済みコピーがある場合は atomic に置き換えます。続行しますか？
                    </template>
                    <template v-else>
                        KonomiTV-BS4K 専用プロファイルの
                        {{provider_display_name}}
                        auth.json コピーだけを削除します。ホスト側ファイルは変更しません。続行しますか？
                    </template>
                </v-card-text>
                <v-card-actions>
                    <v-spacer />
                    <v-btn variant="text" :disabled="is_updating_acp_authentication"
                        @click="closeACPAuthenticationDialog()">キャンセル</v-btn>
                    <v-btn :color="pending_acp_authentication_action === 'Delete' ? 'error' : 'primary'"
                        variant="flat" :loading="is_updating_acp_authentication"
                        @click="confirmACPAuthenticationAction()">
                        {{pending_acp_authentication_action === 'Delete' ? '削除' : '取り込む'}}
                    </v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>
    </div>
</template>

<script lang="ts" setup>

import { computed, onMounted, ref, watch } from 'vue';

import Message from '@/message';
import AIBackend, {
    type AcpReasoningEffort,
    type ACPBackendKind,
    type IACPBackendConnectionTestCheck,
    type IACPBackendConnectionTestResult,
    type IACPBackendCredentialStatus,
    type IACPBackendSettings,
    type IACPBackendEpisodeLookupConnectionChecks,
} from '@/services/AIBackend';
import Utils, { dayjs } from '@/utils';
import { ACP_HARD_TIMEOUT_SEC } from '@/utils/RecordedEpisodeResolution';


const props = defineProps<{
    /** 'codex' または 'grok'。表示名と API の backend_kind を導出する。 */
    provider: 'codex' | 'grok';
    /** サーバーから取得した 1 プロバイダ分の ACP 設定。 */
    settings: IACPBackendSettings;
    /** 認証内容を含まない ACP 資格情報状態。 */
    credentialStatus: IACPBackendCredentialStatus | null;
    /** 親が ACP 設定全体を保存中か。 */
    saving: boolean;
}>();
const emit = defineEmits<{
    (event: 'update:settings', settings: IACPBackendSettings): void;
    (event: 'update:credential-status', status: IACPBackendCredentialStatus): void;
    (event: 'save'): void;
}>();

/** サーバー側 ACP hard timeout の表示用分。ACP_HARD_TIMEOUT_SEC から導出する。 */
const acp_hard_timeout_minutes = Math.floor(ACP_HARD_TIMEOUT_SEC / 60);
/** プロバイダ別のモデル候補（角括弧なし）。Grok は 4.5 固定表示。 */
const acp_model_presets_by_backend: Record<'codex' | 'grok', {title: string; value: string;}[]> = {
    codex: [
        {title: 'GPT-5.6 Luna', value: 'gpt-5.6-luna'},
        {title: 'GPT-5.6 Terra', value: 'gpt-5.6-terra'},
        {title: 'GPT-5.6 Sol', value: 'gpt-5.6-sol'},
        {title: 'GPT-5.5', value: 'gpt-5.5'},
        {title: 'GPT-5.4', value: 'gpt-5.4'},
        {title: 'GPT-5.4 Mini', value: 'gpt-5.4-mini'},
        {title: 'GPT-5.3 Codex Spark', value: 'gpt-5.3-codex-spark'},
    ],
    grok: [
        {title: 'Grok 4.5（固定）', value: 'grok-4.5'},
    ],
};
/** プロバイダ別の推論深さ候補。 */
const acp_reasoning_effort_presets_by_backend: Record<'codex' | 'grok', {title: string; value: AcpReasoningEffort;}[]> = {
    codex: [
        {title: 'Low（速い）', value: 'Low'},
        {title: 'Medium', value: 'Medium'},
        {title: 'High（深い）', value: 'High'},
        {title: 'XHigh', value: 'XHigh'},
        {title: 'Max', value: 'Max'},
        {title: 'Ultra', value: 'Ultra'},
    ],
    grok: [
        {title: 'Low（速い）', value: 'Low'},
        {title: 'Medium', value: 'Medium'},
        {title: 'High（深い）', value: 'High'},
    ],
};
/** プロバイダ別の未設定時デフォルト。 */
const acp_defaults_by_backend: Record<'codex' | 'grok', {model: string | null; effort: AcpReasoningEffort | null;}> = {
    codex: {model: 'gpt-5.6-luna', effort: 'Medium'},
    grok: {model: null, effort: 'High'},
};
const connection_test_capabilities: {title: string; value: 'CandidateSelection' | 'EpisodeLookup';}[] = [
    {title: 'シリーズ情報生成', value: 'CandidateSelection'},
    {title: '話数 Web 検索', value: 'EpisodeLookup'},
];
const episode_lookup_connection_checks: {
    title: string;
    value: keyof IACPBackendEpisodeLookupConnectionChecks;
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
type ConnectionTestResults = Record<'CandidateSelection' | 'EpisodeLookup', IACPBackendConnectionTestResult | null>;

/** 編集用ドラフト。親の settings 変更を watch して同期する。 */
const draft_settings = ref<IACPBackendSettings>({...props.settings});
const testing_connection_capability = ref<'CandidateSelection' | 'EpisodeLookup' | null>(null);
const connection_test_results = ref<ConnectionTestResults>({
    CandidateSelection: null,
    EpisodeLookup: null,
});
const is_updating_acp_authentication = ref(false);
const acp_authentication_dialog = ref(false);
const pending_acp_authentication_action = ref<'Import' | 'Delete' | null>(null);

const is_form_dense = Utils.isSmartphoneHorizontal();

const backend_kind: ACPBackendKind = props.provider === 'codex' ? 'AcpCodex' : 'AcpGrok';
const provider_display_name = props.provider === 'codex' ? 'Codex' : 'Grok Build';
const section_icon = props.provider === 'codex' ? 'fluent:brain-circuit-20-filled' : 'fluent:sparkle-20-filled';

/** props.settings の差し替え（保存後の再取得など）をドラフトへ反映する。 */
watch(() => props.settings, (settings) => {
    draft_settings.value = {...settings};
});

/** ドラフトの一部を更新し、親へ emit する。 */
function updateDraft(patch: Partial<IACPBackendSettings>): void {
    draft_settings.value = {...draft_settings.value, ...patch};
    emit('update:settings', draft_settings.value);
}

function isKonomiTVBS4KCodexSolModel(model: string | null): boolean {
    const normalized = model?.trim().toLowerCase() ?? '';
    return normalized === 'sol' || normalized.endsWith('-sol');
}

/** 現在のプロバイダ向けモデル候補。 */
const acp_model_preset_items = computed(() => {
    return acp_model_presets_by_backend[props.provider] ?? [];
});

const acp_model_error = computed(() =>
    (draft_settings.value.model?.trim().length ?? 0) > 255 ? 'ACP モデル ID は 255 文字以内で入力してください。' : '',
);

const acp_effective_model = computed(() => {
    const current = draft_settings.value.model?.trim() ?? '';
    return current || acp_defaults_by_backend[props.provider]?.model || '';
});
const is_codex_sol_model = computed(() =>
    props.provider === 'codex' && isKonomiTVBS4KCodexSolModel(acp_effective_model.value),
);

/** 現在のプロバイダ向け推論深さ候補。 */
const acp_reasoning_effort_options = computed(() => {
    const options = acp_reasoning_effort_presets_by_backend[props.provider] ?? [];
    if (props.provider !== 'codex' || is_codex_sol_model.value) {
        return options;
    }
    return options.filter(option => option.value !== 'Ultra');
});

function normalizeCodexReasoningEffort(
    effort: AcpReasoningEffort | null,
    model: string | null,
    backend: 'codex' | 'grok',
): AcpReasoningEffort | null {
    if (
        backend === 'codex' &&
        effort === 'Ultra' &&
        isKonomiTVBS4KCodexSolModel(model) === false
    ) {
        return 'Max';
    }
    return effort;
}

/** モデル選択。Grok は保存値が null でも表示上 grok-4.5 を出す。 */
const acp_model_selection = computed<string | null>(() => {
    if (props.provider === 'grok') {
        return 'grok-4.5';
    }
    const current = draft_settings.value.model?.trim() ?? '';
    const matched = acp_model_preset_items.value.find(item => item.value === current);
    if (matched) return matched.value;
    if (current === '') {
        // 未設定時はプロバイダ既定の先頭候補を表示する。
        return acp_model_preset_items.value[0]?.value ?? null;
    }
    // 旧バージョンなどで保存された一覧外 ID は、そのまま文字列で返す。
    return current;
});

function setAcpModelSelection(value: string | null): void {
    if (props.provider === 'grok') {
        // Grok は常にモデル固定。保存は null。
        updateDraft({model: null});
        return;
    }
    if (value === null || value === undefined) {
        updateDraft({
            model: null,
            reasoning_effort: normalizeCodexReasoningEffort(
                draft_settings.value.reasoning_effort,
                draft_settings.value.model,
                props.provider,
            ),
        });
        return;
    }
    const trimmed = value.trim();
    updateDraft({
        model: trimmed === '' ? null : trimmed,
        reasoning_effort: normalizeCodexReasoningEffort(
            draft_settings.value.reasoning_effort,
            trimmed === '' ? null : trimmed,
            props.provider,
        ),
    });
}

/** 推論深さ。未設定時はプロバイダ既定を表示し、set でドラフトへ書き戻す。 */
const acp_reasoning_effort_selection = computed<AcpReasoningEffort | null>(() => {
    if (draft_settings.value.reasoning_effort !== null) {
        return normalizeCodexReasoningEffort(
            draft_settings.value.reasoning_effort,
            acp_effective_model.value,
            props.provider,
        );
    }
    return acp_defaults_by_backend[props.provider]?.effort ?? null;
});

function setAcpReasoningEffortSelection(value: AcpReasoningEffort | null): void {
    updateDraft({
        reasoning_effort: normalizeCodexReasoningEffort(
            value,
            acp_effective_model.value,
            props.provider,
        ),
    });
}

/** 実際に ACP agent へ適用するモデルと推論深さ、または Grok CLI 引数を表示する。 */
const acp_wire_preview = computed(() => {
    const effort = acp_reasoning_effort_selection.value;
    if (props.provider === 'grok') {
        const effort_cli = (effort ?? 'High').toLowerCase();
        return `grok --reasoning-effort ${effort_cli} agent stdio`;
    }
    const model = acp_effective_model.value;
    if (model === '') return '';
    if (effort) {
        const fast_mode_suffix = draft_settings.value.codex_fast_mode_enabled ? ' / fast' : '';
        return `${model} / reasoning_effort=${effort.toLowerCase()}${fast_mode_suffix}`;
    }
    return model;
});

const acp_timeout_error = computed(() => {
    const timeout = Number(draft_settings.value.timeout_sec);
    return Number.isInteger(timeout) && timeout >= 30 && timeout <= 600 ?
        '' :
        'ACP 無通信タイムアウトは 30 ～ 600 秒の整数で入力してください。';
});

const validation_error = computed(() => {
    const errors: string[] = [];
    if (acp_model_error.value !== '') errors.push(acp_model_error.value);
    if (acp_timeout_error.value !== '') errors.push(acp_timeout_error.value);
    return errors.join('\n');
});

/** 認証状態のプロバイダ別切り出し。 */
const host_auth_available = computed(() => {
    if (props.credentialStatus === null) return false;
    return props.provider === 'codex' ?
        props.credentialStatus.codex_host_auth_available :
        props.credentialStatus.grok_host_auth_available;
});
const auth_imported = computed(() => {
    if (props.credentialStatus === null) return false;
    return props.provider === 'codex' ?
        props.credentialStatus.codex_auth_imported :
        props.credentialStatus.grok_auth_imported;
});
const auth_imported_at = computed(() => {
    if (props.credentialStatus === null) return null;
    return props.provider === 'codex' ?
        props.credentialStatus.codex_auth_imported_at :
        props.credentialStatus.grok_auth_imported_at;
});
const auth_in_use = computed(() => {
    if (props.credentialStatus === null) return false;
    return props.provider === 'codex' ?
        props.credentialStatus.codex_auth_in_use :
        props.credentialStatus.grok_auth_in_use;
});

/** 選択中 ACP を新規起動せず、先に解消すべき認証・直列実行状態を説明する。 */
const connection_preflight_error = computed(() => {
    if (props.credentialStatus === null) return 'ACP 認証状態を取得できませんでした。';
    if (auth_imported.value === false) {
        return `${provider_display_name} の認証が未取り込みです。接続テストの前にホスト auth.json を取り込んでください。`;
    }
    if (props.credentialStatus.acp_operation_running) {
        return '別の ACP AI 処理を実行中です。完了後に接続テストを実行してください。';
    }
    return '';
});

const has_connection_test_result = computed(() =>
    connection_test_results.value.CandidateSelection !== null ||
    connection_test_results.value.EpisodeLookup !== null,
);

/** ドラフトを親へ通知し、親が ACP 設定全体を保存する。 */
async function saveSettings(): Promise<void> {
    if (validation_error.value || props.saving) return;
    emit('save');
}

/** 固定6項目のうち指定した接続試験結果を安全に取得する。 */
function connectionCheck(
    result: IACPBackendConnectionTestResult | null,
    check_name: keyof IACPBackendEpisodeLookupConnectionChecks,
): IACPBackendConnectionTestCheck | null {
    return result?.checks?.[check_name] ?? null;
}

/** 接続試験項目の状態を日本語表示へ変換する。 */
function connectionCheckStatusLabel(status: string | undefined): string {
    return status === undefined ? '未実行' : connection_check_status_labels[status] ?? status;
}

/** 接続試験項目の状態に対応するアイコンを返す。 */
function connectionCheckIcon(status: string | undefined): string {
    if (status === 'Passed') return 'fluent:checkmark-circle-20-filled';
    if (status === 'Failed') return 'fluent:error-circle-20-filled';
    if (status === 'NotApplicable') return 'fluent:subtract-circle-20-filled';
    return 'fluent:clock-20-filled';
}

/** 保存済み ACP 設定で、指定した本番能力と同じ最小リクエストを試す。 */
async function testConnection(capability: 'CandidateSelection' | 'EpisodeLookup'): Promise<void> {
    if (connection_preflight_error.value !== '' || testing_connection_capability.value !== null) return;

    testing_connection_capability.value = capability;
    connection_test_results.value[capability] = null;
    const result = await AIBackend.testACPConnection(backend_kind, capability);
    const capability_title = connection_test_capabilities.find(item => item.value === capability)?.title ?? capability;
    connection_test_results.value[capability] = result;
    if (result?.success) {
        Message.success(`${capability_title}の接続テストに成功しました。（${result.model} / ${result.latency_ms.toLocaleString()} ms）`);
    } else if (result !== null) {
        Message.error(`${capability_title}の接続テストに失敗しました。\n${result.message}`);
    }
    testing_connection_capability.value = null;
}

/** Codex / Grok の import / delete を確認ダイアログで明示する。 */
function openACPAuthenticationDialog(action: 'Import' | 'Delete'): void {
    if (is_updating_acp_authentication.value) return;
    pending_acp_authentication_action.value = action;
    acp_authentication_dialog.value = true;
}

/** 実行中でない認証確認ダイアログを閉じ、対象アクションを破棄する。 */
function closeACPAuthenticationDialog(): void {
    if (is_updating_acp_authentication.value) return;
    acp_authentication_dialog.value = false;
    pending_acp_authentication_action.value = null;
}

/** 確認済みの auth.json import / 専用コピー delete を管理 API へ送信する。 */
async function confirmACPAuthenticationAction(): Promise<void> {
    const pending_action = pending_acp_authentication_action.value;
    if (pending_action === null || is_updating_acp_authentication.value) return;

    is_updating_acp_authentication.value = true;
    const updated_status = pending_action === 'Import' ?
        await AIBackend.importACPAuthentication(props.provider) :
        await AIBackend.deleteACPAuthentication(props.provider);
    is_updating_acp_authentication.value = false;
    if (updated_status === null) return;

    emit('update:credential-status', updated_status);
    acp_authentication_dialog.value = false;
    pending_acp_authentication_action.value = null;
    if (pending_action === 'Import') {
        Message.success(`${provider_display_name} 認証を KonomiTV-BS4K 専用プロファイルへ取り込みました。`);
    } else {
        Message.success(`KonomiTV-BS4K の ${provider_display_name} 認証コピーを削除しました。`);
    }
}

/** サーバー側で記録した認証取り込み日時をローカル表示へ変換する。 */
function formatAuthImportedAt(imported_at: string): string {
    return dayjs(imported_at).format('YYYY/M/D HH:mm:ss');
}

onMounted(() => {
    // 親から取得済みの settings をドラフトへ初期反映する（初回 watch が走らないため）。
    draft_settings.value = {...props.settings};
});

</script>

<style lang="scss" scoped>

.acp-backend-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
}

.acp-backend-auth-status {
    display: flex;
    flex-direction: column;
    gap: 5px;
    padding: 11px 13px;
    border-radius: 6px;
    background: rgb(var(--v-theme-background-lighten-1));
    color: rgb(var(--v-theme-text-darken-1));
    font-size: 12px;

    strong {
        color: rgb(var(--v-theme-error-readable));
    }

    &--ok {
        color: rgb(var(--v-theme-success-readable)) !important;
    }
}

.acp-backend-connection-results {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.acp-backend-connection-result {
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

.acp-backend-connection-checks {
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

        svg {
            flex-shrink: 0;
            margin-top: 1px;
        }
    }

    &--Passed,
    &--Passed span {
        color: rgb(var(--v-theme-success-readable)) !important;
    }

    &--Failed,
    &--Failed span {
        color: rgb(var(--v-theme-error-readable)) !important;
    }
}

.acp-backend-dialog {
    background: rgb(var(--v-theme-background-lighten-1));
}

</style>
