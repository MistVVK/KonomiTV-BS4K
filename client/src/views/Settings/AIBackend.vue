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
            <!-- 親幅が狭いと3タブ分の intrinsic 幅が溢れるため、超過時は左右矢印でスクロールする -->
            <v-tabs v-model="tab" color="primary" bg-color="transparent" class="mt-4 ai-backend-tabs"
                show-arrows :density="is_form_dense ? 'compact' : 'default'">
                <v-tab value="opencode">
                    <Icon icon="fluent:cloud-20-filled" width="17px" />
                    <span class="ml-1">OpenCode</span>
                </v-tab>
                <v-tab value="acp-codex">
                    <Icon icon="fluent:brain-circuit-20-filled" width="17px" />
                    <span class="ml-1">ACP / Codex</span>
                </v-tab>
                <v-tab value="acp-grok">
                    <Icon icon="fluent:sparkle-20-filled" width="17px" />
                    <span class="ml-1">ACP / Grok Build</span>
                </v-tab>
            </v-tabs>

            <v-window v-model="tab" class="mt-2">
            <v-window-item value="opencode">
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
                <div class="settings__item-label mt-2">
                    各 service カードに当月の利用状況を表示します。料金は OpenCode が返す
                    <strong>推定料金</strong>で、プロバイダの請求額そのものではありません。
                </div>

                <div v-if="services.length === 0" class="settings__item-label mt-3">
                    まだ service がありません。「追加」から OpenCode provider を登録してください。
                </div>

                <div v-for="service in services" :key="service.service_id" class="ai-backend-card">
                    <div class="ai-backend-card__header">
                        <div>
                            <div class="ai-backend-card__title">{{ service.service_name }}</div>
                            <div class="ai-backend-card__meta">
                                {{ service.opencode_provider_id }} / {{ service.opencode_model_id }}<br>
                                auth: {{ service.auth_mode }} · billing: {{ service.billing_mode }} ·
                                認証: {{ service.auth_configured ? '設定済み' : '未設定' }}
                                <template v-if="service.auth_mode === 'OAuthSubscription'">
                                    · OAuth: {{ service.oauth_connected ? '接続中' : '未接続' }}
                                </template>
                            </div>
                        </div>
                        <div v-if="usageByServiceId[service.service_id]" class="ai-backend-card__badges">
                            <span class="ai-backend-usage-badge">
                                {{ formatBillingMode(usageByServiceId[service.service_id]!.billing_mode) }}
                            </span>
                            <span v-if="usageByServiceId[service.service_id]!.token_limit_reached"
                                class="ai-backend-usage-badge ai-backend-usage-badge--warn">token 上限</span>
                            <span v-if="usageByServiceId[service.service_id]!.cost_limit_reached"
                                class="ai-backend-usage-badge ai-backend-usage-badge--warn">料金上限</span>
                        </div>
                    </div>

                    <!-- 当月利用状況（登録済み service に統合） -->
                    <div class="ai-backend-card__usage">
                        <div class="ai-backend-card__usage-title">
                            当月の利用状況
                            <template v-if="usageByServiceId[service.service_id]">
                                （{{ usageByServiceId[service.service_id]!.year_month }}）
                            </template>
                        </div>
                        <template v-if="usageByServiceId[service.service_id]">
                            <div class="ai-backend-usage-card__grid ai-backend-usage-card__grid--embedded">
                                <div>
                                    <span>確定 token</span>
                                    <strong>
                                        {{ formatTokenCount(usageByServiceId[service.service_id]!.settled_total_tokens) }}
                                    </strong>
                                    <small>
                                        in {{ formatTokenCount(usageByServiceId[service.service_id]!.settled_prompt_tokens) }}
                                        / out {{ formatTokenCount(usageByServiceId[service.service_id]!.settled_completion_tokens) }}
                                    </small>
                                </div>
                                <div>
                                    <span>推定料金（請求額ではない）</span>
                                    <strong>
                                        {{ formatUsd(usageByServiceId[service.service_id]!.settled_estimated_cost_usd) }}
                                    </strong>
                                    <small>
                                        確定リクエスト
                                        {{ usageByServiceId[service.service_id]!.settled_request_count.toLocaleString() }} 回
                                    </small>
                                </div>
                                <div>
                                    <span>予約中</span>
                                    <strong>
                                        {{ formatTokenCount(usageByServiceId[service.service_id]!.reserved_total_tokens) }}
                                    </strong>
                                    <small>
                                        推定 {{ formatUsd(usageByServiceId[service.service_id]!.reserved_estimated_cost_usd) }}
                                    </small>
                                </div>
                                <div>
                                    <span>月次上限</span>
                                    <strong>{{ formatUsageLimits(usageByServiceId[service.service_id]!) }}</strong>
                                    <small>{{ formatLimitFlags(usageByServiceId[service.service_id]!) }}</small>
                                </div>
                            </div>
                        </template>
                        <div v-else class="ai-backend-card__usage-body">
                            当月の利用はまだありません。
                        </div>
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
                            :loading="testing_service_id === `${service.service_id}:CandidateSelection`"
                            @click="runConnectionTest(service, 'CandidateSelection')">
                            生成試験
                        </v-btn>
                        <v-btn size="small" variant="tonal" color="primary"
                            :disabled="is_busy || !health?.available || service.auth_mode === 'NoneLocal'"
                            :loading="testing_service_id === `${service.service_id}:EpisodeLookup`"
                            @click="runConnectionTest(service, 'EpisodeLookup')">
                            話数試験
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

            <!-- 削除済み service の利用履歴のみ別枠（登録済みと重複しない） -->
            <div v-if="deleted_usage_list.length > 0"
                class="settings__content mt-6" :class="{'settings__content--disabled': is_busy}">
                <div class="settings__content-heading">
                    <Icon icon="fluent:data-usage-20-filled" width="22px" />
                    <span class="ml-2">削除済み service の利用履歴</span>
                </div>
                <div class="settings__item-label mt-2">
                    設定から削除した service の当月履歴です。上限 enforce の対象外で、表示のみ残ります。
                </div>
                <div v-for="usage in deleted_usage_list" :key="`${usage.service_id}-${usage.year_month}`"
                    class="ai-backend-usage-card">
                    <div class="ai-backend-usage-card__header">
                        <div class="ai-backend-usage-card__title">
                            {{ usage.service_name_snapshot || usage.service_id }}
                        </div>
                        <div class="ai-backend-usage-card__badges">
                            <span class="ai-backend-usage-badge">{{ formatBillingMode(usage.billing_mode) }}</span>
                            <span class="ai-backend-usage-badge ai-backend-usage-badge--deleted">削除済み</span>
                        </div>
                    </div>
                    <div class="ai-backend-usage-card__meta">
                        {{ usage.opencode_provider_id_snapshot }} / {{ usage.opencode_model_id_snapshot }}
                        · {{ usage.year_month }}
                    </div>
                    <div class="ai-backend-usage-card__grid">
                        <div>
                            <span>確定 token</span>
                            <strong>{{ formatTokenCount(usage.settled_total_tokens) }}</strong>
                            <small>
                                in {{ formatTokenCount(usage.settled_prompt_tokens) }}
                                / out {{ formatTokenCount(usage.settled_completion_tokens) }}
                            </small>
                        </div>
                        <div>
                            <span>推定料金（請求額ではない）</span>
                            <strong>{{ formatUsd(usage.settled_estimated_cost_usd) }}</strong>
                            <small>確定リクエスト {{ usage.settled_request_count.toLocaleString() }} 回</small>
                        </div>
                        <div>
                            <span>予約中</span>
                            <strong>{{ formatTokenCount(usage.reserved_total_tokens) }}</strong>
                            <small>推定 {{ formatUsd(usage.reserved_estimated_cost_usd) }}</small>
                        </div>
                        <div>
                            <span>月次上限</span>
                            <strong>{{ formatUsageLimits(usage) }}</strong>
                            <small>{{ formatLimitFlags(usage) }}</small>
                        </div>
                    </div>
                </div>
            </div>
            </v-window-item>

            <v-window-item value="acp-codex">
                <ACPBackendSection provider="codex"
                    :settings="acp_settings?.codex ?? empty_acp_settings"
                    :credential-status="acp_credential_status"
                    :saving="is_saving_acp"
                    @update:settings="updateACPDraft('codex', $event)"
                    @update:credential-status="acp_credential_status = $event"
                    @save="saveACPSettings()" />
            </v-window-item>

            <v-window-item value="acp-grok">
                <ACPBackendSection provider="grok"
                    :settings="acp_settings?.grok ?? empty_acp_settings"
                    :credential-status="acp_credential_status"
                    :saving="is_saving_acp"
                    @update:settings="updateACPDraft('grok', $event)"
                    @update:credential-status="acp_credential_status = $event"
                    @save="saveACPSettings()" />
            </v-window-item>
            </v-window>
        </template>

        <!-- 追加・編集ダイアログ（OpenCode Web 相当: プロバイダ → 認証方式） -->
        <v-dialog v-model="edit_dialog" max-width="680" persistent>
            <v-card>
                <v-card-title>{{ editing_service_id ? 'service を編集' : 'service を追加' }}</v-card-title>
                <v-card-text>
                    <div class="settings__item-label mb-3">
                        OpenCode Web と同じく、<strong>プロバイダ</strong>を選んだあと
                        <strong>認証方式</strong>（API キー / OAuth など）を選びます。<br>
                        API キーと OAuth はベストエフォート対応です。Vertex AI 以外の面倒な認証
                        （Azure / Bedrock / GitHub Enterprise 等）は非対応です。
                    </div>
                    <!-- 1. プロバイダ選択（全カタログ・検索可） -->
                    <v-autocomplete v-model="selected_provider_id" :items="provider_items" item-title="title"
                        item-value="value" label="プロバイダ" variant="outlined" color="primary"
                        :density="is_form_dense ? 'compact' : 'default'" class="mb-2" clearable
                        :item-props="providerItemProps"
                        no-data-text="provider カタログを取得できませんでした"
                        @update:model-value="onProviderSelected" />
                    <div v-if="selected_provider?.support_note" class="settings__item-label mb-2">
                        {{ selected_provider.support_note }}
                    </div>
                    <div v-if="selected_provider?.support_kind === 'UnsupportedComplex'"
                        class="ai-backend-warn mb-3">
                        このプロバイダは面倒な認証が必要なため選択できません（Vertex AI のみ例外対応）。
                    </div>
                    <!-- 2. 認証方式（OpenCode Web と同じラベル一覧） -->
                    <v-select v-model="selected_auth_method_key" :items="auth_method_items" item-title="title"
                        item-value="value" label="認証方式" variant="outlined" color="primary"
                        :density="is_form_dense ? 'compact' : 'default'" class="mb-2"
                        :disabled="!selected_provider || selected_provider.support_kind !== 'Supported'"
                        :no-data-text="selected_provider_id
                            ? '利用できる認証方式がありません'
                            : '先にプロバイダを選択してください'"
                        @update:model-value="onAuthMethodSelected" />
                    <!-- 3. モデル -->
                    <v-autocomplete v-model="selected_model_id" :items="model_items" item-title="title"
                        item-value="value" label="モデル" variant="outlined" color="primary"
                        :density="is_form_dense ? 'compact' : 'default'" class="mb-3" clearable
                        :disabled="!selected_provider_id"
                        :no-data-text="selected_provider_id ? 'モデルがありません' : '先にプロバイダを選択してください'"
                        @update:model-value="onModelSelected" />
                    <v-divider class="mb-3" />
                    <v-text-field v-model="form.service_name" label="表示名" variant="outlined"
                        color="primary" :density="is_form_dense ? 'compact' : 'default'" class="mb-2" />
                    <!-- 作成時: API キーをその場で入力（保存と同時に設定） -->
                    <v-text-field v-if="form.auth_mode === 'ApiKey' && editing_service_id === null"
                        v-model="create_api_key_input" label="API キー（任意・後から設定も可）"
                        variant="outlined" color="primary"
                        :type="create_api_key_showing ? 'text' : 'password'"
                        :density="is_form_dense ? 'compact' : 'default'" class="mb-2"
                        spellcheck="false" autocomplete="off"
                        :append-inner-icon="create_api_key_showing ? 'mdi-eye-off' : 'mdi-eye'"
                        @click:append-inner="create_api_key_showing = !create_api_key_showing" />
                    <template v-if="form.auth_mode === 'VertexAdc'">
                        <v-text-field v-model="form.google_cloud_project" label="Google Cloud project"
                            variant="outlined" color="primary" :density="is_form_dense ? 'compact' : 'default'"
                            class="mb-2" />
                        <v-text-field v-model="form.google_cloud_location" label="Google Cloud location"
                            variant="outlined" color="primary" :density="is_form_dense ? 'compact' : 'default'"
                            class="mb-2" />
                    </template>
                    <div v-if="form.auth_mode === 'OAuthSubscription'" class="settings__item-label mb-2">
                        保存後に「OAuth開始」からブラウザ認証画面を開いてください。
                        Docker 内 localhost リダイレクトが使えない場合は headless / device code 方式を選んでください。
                    </div>
                    <v-expansion-panels variant="accordion" class="mt-2">
                        <v-expansion-panel title="詳細設定（課金・上限・ローカル）">
                            <v-expansion-panel-text>
                                <v-select v-model="form.billing_mode" :items="billing_mode_items" item-title="title"
                                    item-value="value" label="課金モード" variant="outlined" color="primary"
                                    :density="is_form_dense ? 'compact' : 'default'" class="mb-2" />
                                <v-text-field v-if="form.auth_mode === 'NoneLocal' || form.api_base_url"
                                    v-model="form.api_base_url" label="API ベース URL（任意 / ローカル時必須）"
                                    variant="outlined" color="primary" :density="is_form_dense ? 'compact' : 'default'"
                                    class="mb-2" spellcheck="false" />
                                <v-text-field v-if="form.billing_mode === 'Metered'"
                                    v-model="form.monthly_cost_limit_usd" label="月次推定料金上限 USD（空欄=なし）"
                                    variant="outlined" color="primary" :density="is_form_dense ? 'compact' : 'default'"
                                    class="mb-2" type="number" />
                                <v-text-field v-if="form.billing_mode === 'Metered' || form.billing_mode === 'Local'"
                                    v-model.number="form.monthly_token_limit" label="月次 token 上限（空欄=なし）"
                                    variant="outlined" color="primary" :density="is_form_dense ? 'compact' : 'default'"
                                    type="number" />
                            </v-expansion-panel-text>
                        </v-expansion-panel>
                    </v-expansion-panels>
                </v-card-text>
                <v-card-actions>
                    <v-spacer />
                    <v-btn variant="text" :disabled="is_busy" @click="edit_dialog = false">キャンセル</v-btn>
                    <v-btn color="primary" variant="flat" :loading="is_saving" :disabled="is_busy || !canSaveService"
                        @click="saveService()">保存</v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>

        <!-- OAuth ダイアログ（URL 表示・browser 起動・code 入力） -->
        <v-dialog v-model="oauth_dialog" max-width="560" persistent>
            <v-card>
                <v-card-title>OAuth 認証</v-card-title>
                <v-card-text>
                    <div class="settings__item-label mb-2">
                        対象: {{ oauth_target?.service_name }}
                        （{{ oauth_target?.opencode_provider_id }}）
                    </div>
                    <div v-if="oauth_instructions" class="ai-backend-oauth-instructions mb-3">
                        {{ oauth_instructions }}
                    </div>
                    <div v-if="oauth_url" class="mb-3">
                        <v-btn color="primary" variant="flat" class="mb-2" :disabled="is_busy"
                            @click="openOAuthUrl()">
                            認証画面を開く
                        </v-btn>
                        <div class="ai-backend-oauth-url">{{ oauth_url }}</div>
                    </div>
                    <div v-else-if="oauth_started" class="settings__item-label mb-3">
                        認可 URL が返りませんでした。headless method を試すか、OpenCode ログを確認してください。
                    </div>
                    <v-text-field v-if="oauth_needs_code" v-model="oauth_code_input"
                        label="認可コード / device code" variant="outlined" color="primary"
                        :density="is_form_dense ? 'compact' : 'default'" class="mb-2"
                        spellcheck="false" autocomplete="off" />
                    <div class="settings__item-label">
                        ブラウザで認証が終わったら「完了する」を押してください。
                        device code 方式の場合はコードを入力してから完了します。
                    </div>
                </v-card-text>
                <v-card-actions>
                    <v-spacer />
                    <v-btn variant="text" :disabled="is_busy" @click="closeOAuthDialog()">閉じる</v-btn>
                    <v-btn color="primary" variant="flat" :loading="is_oauth_completing"
                        :disabled="is_busy || !oauth_started || (oauth_needs_code && !oauth_code_input.trim())"
                        @click="completeOAuthFlow()">
                        完了する
                    </v-btn>
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

import ACPBackendSection from '@/components/Settings/ACPBackendSection.vue';
import Message from '@/message';
import AIBackend, {
    type AIAuthMode,
    type AIBackendConnectionCapability,
    type AIBillingMode,
    type IAIBackendConnectionTestResult,
    type IAIBackendProvider,
    type IAIBackendProviderAuthMethod,
    type IAIBackendService,
    type IAIBackendUsage,
    type IACPBackendCredentialStatus,
    type IACPBackendSettings,
    type IACPSettings,
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
    opencode_provider_id: '',
    opencode_model_id: '',
    auth_mode: 'ApiKey',
    billing_mode: 'Metered',
    api_base_url: '',
    google_cloud_project: '',
    google_cloud_location: '',
    monthly_cost_limit_usd: '',
    monthly_token_limit: null,
});

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
const usage_list = ref<IAIBackendUsage[]>([]);
const test_results = ref<Record<string, IAIBackendConnectionTestResult | null>>({});
/** `${service_id}:${capability}` 形式。同時に1試験のみ。 */
const testing_service_id = ref<string | null>(null);

/** ACP タブの切り替え状態。 */
const tab = ref<'opencode' | 'acp-codex' | 'acp-grok'>('opencode');
/** ACP 固定プリセット設定（サーバー保存済み）。 */
const acp_settings = ref<IACPSettings | null>(null);
/** 認証内容を含まない ACP 資格情報状態。 */
const acp_credential_status = ref<IACPBackendCredentialStatus | null>(null);
/** ACP 設定全体の保存中フラグ（ACPBackendSection の保存ボタンと連動）。 */
const is_saving_acp = ref(false);
/** 取得前の ACPBackendSection へ渡す無害な初期値。 */
const empty_acp_settings = computed<IACPBackendSettings>(() => ({
    model: null,
    reasoning_effort: null,
    codex_fast_mode_enabled: false,
    timeout_sec: 120,
}));
/** ACPBackendSection のドラフト更新を ACP 設定へ反映する。 */
function updateACPDraft(provider: 'codex' | 'grok', settings: IACPBackendSettings): void {
    if (acp_settings.value === null) {
        // 取得前に編集が入った場合も、codex / grok で同じオブジェクト参照を共有しない。
        acp_settings.value = {
            codex: {...empty_acp_settings.value},
            grok: {...empty_acp_settings.value},
        };
    }
    acp_settings.value = {
        ...acp_settings.value,
        [provider]: settings,
    };
}

/** ACP 固定プリセット設定を全体置き換えで保存し、サーバーの正規化値を再取得する。 */
async function saveACPSettings(): Promise<void> {
    if (is_saving_acp.value || acp_settings.value === null) return;
    is_saving_acp.value = true;
    try {
        const ok = await AIBackend.updateACPSettings(acp_settings.value);
        if (ok) {
            Message.success('ACP 設定を保存しました。');
            const fetched = await AIBackend.fetchACPSettings();
            if (fetched !== null) {
                acp_settings.value = fetched;
            }
        }
    } finally {
        is_saving_acp.value = false;
    }
}

/** service カードへ埋め込む当月 usage（削除済みは別枠）。 */
const usageByServiceId = computed(() => {
    const map: Record<string, IAIBackendUsage> = {};
    for (const usage of usage_list.value) {
        if (usage.service_deleted) {
            continue;
        }
        map[usage.service_id] = usage;
    }
    return map;
});

/** 削除済み service の当月履歴（登録済みカードと重複させない）。 */
const deleted_usage_list = computed(() =>
    usage_list.value.filter(usage => usage.service_deleted),
);

const edit_dialog = ref(false);
const editing_service_id = ref<string | null>(null);
const form = ref<ServiceForm>(emptyForm());
const is_saving = ref(false);
/** 新規作成時に同時設定する API キー（任意）。 */
const create_api_key_input = ref('');
const create_api_key_showing = ref(false);

/** OpenCode Web 相当の全カタログ（未接続含む）。 */
const providers = ref<IAIBackendProvider[]>([]);
const selected_provider_id = ref<string | null>(null);
const selected_model_id = ref<string | null>(null);
/**
 * 選択中の認証方式キー。
 * `${type}:${method_index ?? 'x'}:${label}` で一意化する。
 */
const selected_auth_method_key = ref<string | null>(null);
/** 表示名の自動提案値。ユーザーが手入力した場合は上書きしない。 */
const auto_proposed_name = ref<string | null>(null);
/** 最後に選んだ OAuth method index（カードの OAuth開始で再利用）。 */
const oauth_method_by_service = ref<Record<string, number>>({});

/** provider セレクト。非対応はラベルで示し disabled。 */
const provider_items = computed(() => {
    const items = providers.value.map(provider => {
        const connected = provider.connected ? ' · 接続済' : '';
        const unsupported = provider.support_kind === 'UnsupportedComplex' ? ' · 未対応' : '';
        return {
            title: `${provider.provider_name}（${provider.provider_id}${connected}${unsupported}）`,
            value: provider.provider_id,
            props: {
                disabled: provider.support_kind === 'UnsupportedComplex',
            },
        };
    });
    const current = form.value.opencode_provider_id;
    if (current !== '' && items.some(item => item.value === current) === false) {
        items.unshift({
            title: `${current}（カタログ外）`,
            value: current,
            props: {disabled: false},
        });
    }
    return items;
});

/** v-autocomplete の item-props（disabled 反映）。 */
function providerItemProps(item: unknown): Record<string, unknown> {
    if (item === null || typeof item !== 'object') {
        return {};
    }
    const record = item as {raw?: {props?: Record<string, unknown>}; props?: Record<string, unknown>};
    if (record.raw?.props) {
        return record.raw.props;
    }
    return record.props ?? {};
}

const selected_provider = computed<IAIBackendProvider | null>(() =>
    providers.value.find(provider => provider.provider_id === selected_provider_id.value) ?? null,
);

/** OpenCode Web と同じ認証方式ラベル一覧。 */
const auth_method_items = computed(() => {
    const methods = selected_provider.value?.auth_methods ?? [];
    return methods.map(method => ({
        title: method.label,
        value: authMethodKey(method),
    }));
});

const selected_auth_method = computed<IAIBackendProviderAuthMethod | null>(() => {
    const methods = selected_provider.value?.auth_methods ?? [];
    return methods.find(method => authMethodKey(method) === selected_auth_method_key.value) ?? null;
});

/** モデルセレクトの選択肢（選択した provider のモデル）。 */
const model_items = computed(() => {
    const items = (selected_provider.value?.models ?? []).map(model => ({
        title: `${model.model_name}（${model.model_id}）`,
        value: model.model_id,
    }));
    const current = form.value.opencode_model_id;
    if (current !== '' && items.some(item => item.value === current) === false) {
        items.unshift({title: `${current}（一覧外）`, value: current});
    }
    return items;
});

const canSaveService = computed(() => {
    if (form.value.service_name.trim() === '') return false;
    if (form.value.opencode_provider_id.trim() === '') return false;
    if (form.value.opencode_model_id.trim() === '') return false;
    if (selected_provider.value?.support_kind === 'UnsupportedComplex') return false;
    return true;
});

function authMethodKey(method: IAIBackendProviderAuthMethod): string {
    // ラベルは表示専用。value は type+index のみにして v-select の表示を安定させる。
    return `${method.type}:${method.method_index ?? 'x'}`;
}

/** provider 選択時: 認証方式一覧を出し、先頭 method と既定モデルを提案する。 */
function onProviderSelected(): void {
    const provider = selected_provider.value;
    selected_model_id.value = null;
    selected_auth_method_key.value = null;
    if (provider === null) {
        form.value.opencode_provider_id = '';
        form.value.opencode_model_id = '';
        return;
    }
    form.value.opencode_provider_id = provider.provider_id;
    if (provider.support_kind !== 'Supported' || provider.auth_methods.length === 0) {
        return;
    }
    // OpenCode Web と同様、一覧の先頭認証方式を初期選択する。
    selected_auth_method_key.value = authMethodKey(provider.auth_methods[0]);
    onAuthMethodSelected();
    // 既定モデルをプリセット（モデル一覧に存在する場合のみ）。
    if (
        provider.default_model_id !== null &&
        provider.models.some(model => model.model_id === provider.default_model_id)
    ) {
        selected_model_id.value = provider.default_model_id;
    } else if (provider.models.length > 0) {
        selected_model_id.value = provider.models[0].model_id;
    }
    onModelSelected();
}

/** 認証方式選択時: auth_mode / billing を OpenCode method に合わせて設定する。 */
function onAuthMethodSelected(): void {
    const method = selected_auth_method.value;
    if (method === null) {
        return;
    }
    form.value.auth_mode = method.auth_mode;
    form.value.billing_mode = method.billing_mode_default;
    if (method.auth_mode === 'VertexAdc') {
        // project 未入力時のプレースホルダ。Docker env の値は別途管理。
        if (form.value.google_cloud_location.trim() === '') {
            form.value.google_cloud_location = 'global';
        }
    }
}

/** モデル選択時の form 反映（provider ID / model ID / 表示名の自動提案）。 */
function onModelSelected(): void {
    const provider = selected_provider.value;
    const model = provider?.models.find(item => item.model_id === selected_model_id.value) ?? null;
    if (provider === null) {
        return;
    }
    form.value.opencode_provider_id = provider.provider_id;
    if (model === null) {
        form.value.opencode_model_id = selected_model_id.value ?? '';
        return;
    }
    form.value.opencode_model_id = model.model_id;
    // 表示名は未入力か前回の自動提案値のときだけモデル名で上書きする。
    if (
        form.value.service_name === '' ||
        (auto_proposed_name.value !== null && form.value.service_name === auto_proposed_name.value)
    ) {
        form.value.service_name = model.model_name;
        auto_proposed_name.value = model.model_name;
    }
}

const key_dialog = ref(false);
const key_target = ref<IAIBackendService | null>(null);
const api_key_input = ref('');
const api_key_showing = ref(false);
const is_saving_key = ref(false);

const oauth_dialog = ref(false);
const oauth_target = ref<IAIBackendService | null>(null);
const oauth_method_index = ref(0);
const oauth_url = ref<string | null>(null);
const oauth_instructions = ref<string | null>(null);
const oauth_needs_code = ref(false);
const oauth_code_input = ref('');
const oauth_started = ref(false);
const is_oauth_starting = ref(false);
const is_oauth_completing = ref(false);

const delete_dialog = ref(false);
const delete_target = ref<IAIBackendService | null>(null);
const is_deleting = ref(false);

const is_busy = computed(() =>
    is_saving.value ||
    is_saving_key.value ||
    is_deleting.value ||
    is_oauth_starting.value ||
    is_oauth_completing.value ||
    testing_service_id.value !== null,
);

function formatTokenCount(value: number): string {
    return value.toLocaleString();
}

function formatUsd(value: string | null | undefined): string {
    if (value === null || value === undefined || value.trim() === '') {
        return '$0';
    }
    const amount = Number(value);
    if (Number.isFinite(amount) === false) {
        return `$${value}`;
    }
    // 推定料金は小数が細かいことがあるため最大 6 桁まで残す
    return `$${amount.toLocaleString(undefined, {
        minimumFractionDigits: 0,
        maximumFractionDigits: 6,
    })}`;
}

function formatBillingMode(mode: string): string {
    if (mode === 'Metered') {
        return 'Metered（従量）';
    }
    if (mode === 'Subscription') {
        return 'Subscription';
    }
    if (mode === 'Local') {
        return 'Local';
    }
    return mode;
}

function formatUsageLimits(usage: IAIBackendUsage): string {
    if (usage.billing_mode === 'Subscription') {
        return '上限対象外';
    }
    const parts: string[] = [];
    if (usage.billing_mode === 'Metered') {
        parts.push(
            usage.monthly_cost_limit_usd
                ? `料金 ${formatUsd(usage.monthly_cost_limit_usd)}`
                : '料金 なし',
        );
    }
    if (usage.billing_mode === 'Metered' || usage.billing_mode === 'Local') {
        parts.push(
            usage.monthly_token_limit !== null
                ? `token ${formatTokenCount(usage.monthly_token_limit)}`
                : 'token なし',
        );
    }
    return parts.length > 0 ? parts.join(' / ') : 'なし';
}

function formatLimitFlags(usage: IAIBackendUsage): string {
    if (usage.service_deleted) {
        return '履歴のみ（上限 enforce 対象外）';
    }
    if (usage.billing_mode === 'Subscription') {
        return 'サブスク課金のため月次上限は適用しません';
    }
    const flags: string[] = [];
    if (usage.token_limit_reached) {
        flags.push('token 上限到達');
    }
    if (usage.cost_limit_reached) {
        flags.push('料金上限到達');
    }
    if (flags.length === 0) {
        return usage.cost_limit_effective ? '上限監視中' : '上限なし';
    }
    return flags.join(' · ');
}

async function reloadAll(): Promise<void> {
    // health / services / usage / providers / ACP 設定・認証を同時更新（再読込ボタンからも同じ経路）
    const [health_result, list, usage, provider_list, acp_settings_result, acp_credential_result] = await Promise.all([
        AIBackend.fetchHealth(),
        AIBackend.fetchServices(),
        AIBackend.fetchUsageList(),
        AIBackend.fetchProviders(),
        AIBackend.fetchACPSettings(),
        AIBackend.fetchACPCredentialStatus(),
    ]);
    health.value = health_result;
    if (list !== null) {
        services.value = list;
    }
    if (usage !== null) {
        usage_list.value = usage;
    }
    if (provider_list !== null) {
        providers.value = provider_list;
    }
    if (acp_settings_result !== null) {
        acp_settings.value = acp_settings_result;
    }
    if (acp_credential_result !== null) {
        acp_credential_status.value = acp_credential_result;
    }
}

function openCreateDialog(): void {
    editing_service_id.value = null;
    form.value = emptyForm();
    selected_provider_id.value = null;
    selected_model_id.value = null;
    selected_auth_method_key.value = null;
    create_api_key_input.value = '';
    create_api_key_showing.value = false;
    auto_proposed_name.value = null;
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
    // 既存 service の provider / model を選択状態へ復元（一覧外なら items に追加表示）。
    selected_provider_id.value = service.opencode_provider_id;
    selected_model_id.value = service.opencode_model_id;
    create_api_key_input.value = '';
    create_api_key_showing.value = false;
    auto_proposed_name.value = null;
    // 既存 auth_mode に合う method を復元（無ければ先頭）。
    const provider = providers.value.find(item => item.provider_id === service.opencode_provider_id) ?? null;
    const matched = provider?.auth_methods.find(method => method.auth_mode === service.auth_mode) ?? null;
    selected_auth_method_key.value = matched
        ? authMethodKey(matched)
        : (provider?.auth_methods[0] ? authMethodKey(provider.auth_methods[0]) : null);
    edit_dialog.value = true;
}

async function saveService(): Promise<void> {
    const f = form.value;
    if (f.service_name.trim() === '' || f.opencode_provider_id.trim() === '' || f.opencode_model_id.trim() === '') {
        Message.warning('表示名・プロバイダ・モデルは必須です。');
        return;
    }
    if (selected_provider.value?.support_kind === 'UnsupportedComplex') {
        Message.warning('このプロバイダは未対応です。');
        return;
    }
    // OAuth 選択時は billing を Subscription に揃える（保存 422 防止）。
    if (f.auth_mode === 'OAuthSubscription') {
        f.billing_mode = 'Subscription';
    }
    if (f.auth_mode === 'ApiKey' && f.billing_mode === 'Subscription') {
        f.billing_mode = 'Metered';
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
            // 新規 + API キー入力済みなら続けて注入する。
            if (result !== null && body.auth_mode === 'ApiKey' && create_api_key_input.value.trim() !== '') {
                const key_ok = await AIBackend.setAPIKey(result.service_id, create_api_key_input.value.trim());
                if (key_ok === false) {
                    Message.warning('service は追加しましたが、API キーの設定に失敗しました。');
                }
            }
            // OAuth method index を記憶（カードの OAuth開始で使う）。
            if (result !== null && body.auth_mode === 'OAuthSubscription') {
                const method_index = selected_auth_method.value?.method_index;
                if (method_index !== null && method_index !== undefined) {
                    oauth_method_by_service.value = {
                        ...oauth_method_by_service.value,
                        [result.service_id]: method_index,
                    };
                }
            }
        } else {
            result = await AIBackend.updateService(editing_service_id.value, {
                ...body,
                clear_api_base_url: body.api_base_url === null,
                clear_monthly_cost_limit_usd: body.monthly_cost_limit_usd === null,
                clear_monthly_token_limit: body.monthly_token_limit === null,
            });
            if (result !== null && body.auth_mode === 'OAuthSubscription') {
                const method_index = selected_auth_method.value?.method_index;
                if (method_index !== null && method_index !== undefined) {
                    oauth_method_by_service.value = {
                        ...oauth_method_by_service.value,
                        [result.service_id]: method_index,
                    };
                }
            }
        }
        if (result !== null) {
            Message.success(editing_service_id.value ? 'service を更新しました。' : 'service を追加しました。');
            edit_dialog.value = false;
            // OAuth 新規作成直後は続けて認証ダイアログを開く。
            const should_start_oauth = (
                editing_service_id.value === null &&
                result.auth_mode === 'OAuthSubscription'
            );
            await reloadAll();
            if (should_start_oauth) {
                await runOAuthStart(result);
            }
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

/**
 * OAuth 開始: OpenCode から URL を受け取り、ダイアログで認証画面を開く。
 * method index は service 作成時に選んだ方式、無ければ 0。
 */
async function runOAuthStart(service: IAIBackendService): Promise<void> {
    oauth_target.value = service;
    oauth_url.value = null;
    oauth_instructions.value = null;
    oauth_code_input.value = '';
    oauth_needs_code.value = false;
    oauth_started.value = false;
    oauth_method_index.value = oauth_method_by_service.value[service.service_id] ?? 0;
    // カタログから OAuth method が1件ならそれを優先（記憶が無い場合）。
    if (oauth_method_by_service.value[service.service_id] === undefined) {
        const provider = providers.value.find(item => item.provider_id === service.opencode_provider_id);
        const oauth_method = provider?.auth_methods.find(method => method.type === 'oauth' && method.method_index !== null);
        if (oauth_method?.method_index !== null && oauth_method?.method_index !== undefined) {
            oauth_method_index.value = oauth_method.method_index;
        }
    }
    oauth_dialog.value = true;
    is_oauth_starting.value = true;
    try {
        const result = await AIBackend.startOAuth(service.service_id, oauth_method_index.value);
        if (result === null) {
            return;
        }
        oauth_started.value = true;
        oauth_method_index.value = result.method;
        oauth_url.value = result.url;
        oauth_instructions.value = result.instructions;
        // code 方式、または instructions に code 入力を促す文言がある場合は入力欄を出す。
        oauth_needs_code.value = (
            result.authorization_method === 'code' ||
            (result.instructions !== null && /enter code|code:/i.test(result.instructions))
        );
        // browser 方式なら即座に認証画面を開く（ユーザーが OAuth 画面を目視確認できる）。
        if (result.url) {
            openOAuthUrl();
        }
    } finally {
        is_oauth_starting.value = false;
    }
}

function openOAuthUrl(): void {
    if (oauth_url.value === null || oauth_url.value === '') {
        return;
    }
    // 新しいタブで認可画面を開く（ポップアップブロックされにくい _blank）。
    window.open(oauth_url.value, '_blank', 'noopener,noreferrer');
}

function closeOAuthDialog(): void {
    oauth_dialog.value = false;
    oauth_target.value = null;
    oauth_url.value = null;
    oauth_instructions.value = null;
    oauth_code_input.value = '';
    oauth_started.value = false;
}

async function completeOAuthFlow(): Promise<void> {
    if (oauth_target.value === null) {
        return;
    }
    is_oauth_completing.value = true;
    try {
        const code = oauth_needs_code.value ? oauth_code_input.value.trim() : null;
        const ok = await AIBackend.completeOAuth(
            oauth_target.value.service_id,
            oauth_method_index.value,
            code === '' ? null : code,
        );
        if (ok) {
            Message.success('OAuth 接続が完了しました。');
            closeOAuthDialog();
            await reloadAll();
        }
    } finally {
        is_oauth_completing.value = false;
    }
}

async function runOAuthDisconnect(service: IAIBackendService): Promise<void> {
    const ok = await AIBackend.disconnectOAuth(service.service_id);
    if (ok) {
        Message.success('OAuth を切断しました。');
        await reloadAll();
    }
}

async function runConnectionTest(
    service: IAIBackendService,
    capability: AIBackendConnectionCapability = 'CandidateSelection',
): Promise<void> {
    const test_key = `${service.service_id}:${capability}`;
    testing_service_id.value = test_key;
    try {
        const result = await AIBackend.testConnection(service.service_id, capability);
        if (result !== null) {
            // 同一 service の最新結果を表示（capability はメッセージに含まれる）
            const labeled: IAIBackendConnectionTestResult = {
                ...result,
                message: `[${capability === 'EpisodeLookup' ? '話数' : '生成'}] ${result.message}`,
            };
            test_results.value = {...test_results.value, [service.service_id]: labeled};
            if (result.success) {
                Message.success(
                    capability === 'EpisodeLookup'
                        ? '話数 Web 検索の接続試験に成功しました。'
                        : 'シリーズ生成の接続試験に成功しました。',
                );
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
// 設定本文の幅に収まるようタブバー自体を親幅に拘束する
// （拘束しないと show-arrows でも intrinsic 幅のまま親外へはみ出す）
.ai-backend-tabs {
    width: 100%;
    max-width: 100%;
    min-width: 0;
}
.ai-backend-access-state {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-top: 24px;
    color: rgb(var(--v-theme-text-darken-1));
}
.ai-backend-status--ok { color: #176b4d; font-weight: 700; }
.ai-backend-status--ng { color: #9b2c2c; font-weight: 700; }
.ai-backend-warn {
    padding: 10px 12px;
    border-radius: 8px;
    background: rgba(155, 44, 44, 0.1);
    color: #9b2c2c;
    font-size: 0.9rem;
    line-height: 1.45;
}
.ai-backend-oauth-instructions {
    padding: 10px 12px;
    border-radius: 8px;
    background: rgba(var(--v-theme-primary), 0.08);
    border: 1px solid rgba(var(--v-theme-primary), 0.16);
    font-size: 0.92rem;
    line-height: 1.5;
    white-space: pre-wrap;
}
.ai-backend-oauth-url {
    font-size: 0.78rem;
    word-break: break-all;
    color: rgb(var(--v-theme-text-darken-1));
    line-height: 1.4;
}
.ai-backend-card {
    margin-top: 14px;
    padding: 14px 16px;
    border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
    border-radius: 10px;
    background: rgba(var(--v-theme-surface-light), 0.35);
}
.ai-backend-card__header {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
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
.ai-backend-card__badges {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
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
.ai-backend-card__usage {
    margin-top: 12px;
    padding: 12px 14px;
    border-radius: 8px;
    background: rgba(var(--v-theme-primary), 0.06);
    border: 1px solid rgba(var(--v-theme-primary), 0.14);
}
.ai-backend-card__usage-title {
    font-size: 0.84rem;
    font-weight: 700;
    color: rgb(var(--v-theme-text-darken-1));
}
.ai-backend-card__usage-body {
    margin-top: 6px;
    font-size: 0.88rem;
    line-height: 1.5;
    color: rgb(var(--v-theme-text-darken-1));
}
.ai-backend-usage-card__grid--embedded {
    margin-top: 10px;
}
.ai-backend-usage-card {
    margin-top: 14px;
    padding: 14px 16px;
    border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
    border-radius: 10px;
    background: rgba(var(--v-theme-surface-light), 0.35);
}
.ai-backend-usage-card__header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
}
.ai-backend-usage-card__title {
    font-weight: 700;
    font-size: 1.02rem;
}
.ai-backend-usage-card__badges {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
}
.ai-backend-usage-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 700;
    background: rgba(var(--v-theme-primary), 0.12);
    color: rgb(var(--v-theme-primary));
}
.ai-backend-usage-badge--deleted {
    background: rgba(155, 44, 44, 0.12);
    color: #9b2c2c;
}
.ai-backend-usage-badge--warn {
    background: rgba(138, 88, 0, 0.14);
    color: #8a5800;
}
.ai-backend-usage-card__meta {
    margin-top: 4px;
    font-size: 0.88rem;
    color: rgb(var(--v-theme-text-darken-1));
}
.ai-backend-usage-card__grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
    margin-top: 12px;
}
.ai-backend-usage-card__grid > div {
    display: flex;
    flex-direction: column;
    gap: 2px;
}
.ai-backend-usage-card__grid span {
    font-size: 0.78rem;
    color: rgb(var(--v-theme-text-darken-1));
}
.ai-backend-usage-card__grid strong {
    font-size: 1.02rem;
}
.ai-backend-usage-card__grid small {
    font-size: 0.78rem;
    color: rgb(var(--v-theme-text-darken-1));
    line-height: 1.4;
}
</style>
