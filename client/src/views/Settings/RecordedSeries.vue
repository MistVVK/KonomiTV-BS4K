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
            録画のタイトルから HonomiTV と同じ確定規則でシリーズを付けます。<br>
            付かなかった録画は、AI が有効なら同一 EPG タイトルごとに Web 検索で補完します。<br>
            話数が単一の正整数で取れないときだけ、既存の話数 Web 検索を使います。<br>
            この設定と判定結果はすべてのユーザーと端末で共有されます。管理者だけが変更できます。<br>
        </div>

        <v-progress-linear v-if="is_loading" class="mt-5" color="primary" indeterminate rounded />

        <div v-else-if="authorization_error !== null" class="recorded-series-access-state">
            <Icon icon="fluent:shield-error-20-filled" width="28px" />
            <span v-if="authorization_error === 'LoginRequired'">
                この設定を表示するにはログインが必要です。
                <router-link class="link" to="/login/">ログイン</router-link>
            </span>
            <span v-else-if="authorization_error === 'AdminRequired'">この設定を表示するには管理者権限が必要です。</span>
            <span v-else>サーバーに接続できないため、ユーザー情報を取得できませんでした。</span>
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
                    有効にすると、新しい録画の登録後に Indexer がシリーズを自動判定します。<br>
                    無効にしても保存済みのシリーズ情報は維持されます。<br>
                </label>
                <v-switch id="recorded_series_enabled" class="settings__item-switch" color="primary" hide-details
                    v-model="settings.enabled" />
            </div>
            <div class="settings__item settings__item--switch">
                <label class="settings__item-heading" for="recorded_series_ai_enabled">
                    AI でシリーズ補完・話数 Web 検索・Bangumi 照合をする
                </label>
                <label class="settings__item-label" for="recorded_series_ai_enabled">
                    有効時、Indexer が所属を付けられなかった同一 EPG タイトル群を1回の Web 検索で補完します。<br>
                    所属後、話数が単一の正整数で取れない録画だけ話数を Web 検索します。<br>
                    Bangumi の作品候補選択にも同じバックエンドを使います。<br>
                    Indexer が付けた所属を AI で書き換えることはありません。<br>
                    無効時は Web 検索と Bangumi 候補の AI 選択をしません。<br>
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
                    既存録画は下の一括話数判定から検索できます。<br>
                </div>
            </div>

            <div class="settings__content-heading mt-7">
                <Icon icon="fluent:bot-20-filled" width="22px" />
                <span class="ml-2">AI バックエンド（主系）</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">バックエンド</div>
                <div class="settings__item-label">
                    シリーズ補完・話数 Web 検索・Bangumi 候補選択に最初に使うバックエンドを選びます。<br>
                    OpenCode は「設定 → AIバックエンド」で登録した service を使います。<br>
                    2つの OpenAI 互換 API は OpenCode を経由せず、それぞれ保存済みの HTTP 接続を使います。<br>
                    ACP / Codex・Grok はホスト上の CLI を起動します。<br>
                    Web 検索の接続確認は AIバックエンド画面から行えます。<br>
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

            <!-- 独立 OpenAI 互換 HTTP の接続情報は AI バックエンドページだけで管理する -->
            <template v-if="settings.ai_backend === 'OpenAICompatible' || settings.ai_backend === 'OpenAICompatible2'">
                <div class="settings__item">
                    <div class="settings__item-heading">{{openAICompatibleBackendTitle(settings.ai_backend)}} の接続</div>
                    <div class="settings__item-label">
                        API ベース URL・モデル・API キー・接続試験は「AIバックエンド」ページで設定します。<br>
                        接続状態: {{isOpenAICompatibleConfigured(settings.ai_backend) ? '設定済み' : '未設定'}}
                    </div>
                    <v-btn class="settings__save-button mt-3" variant="flat" to="/settings/server/ai-backends">
                        <Icon icon="fluent:settings-20-regular" class="mr-2" width="21px" />
                        AIバックエンド設定を開く
                    </v-btn>
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
                    主系 AI が技術的に失敗したとき、または InsufficientEvidence のときにどうするかを決めます。<br>
                    NoPublishedNumber / NotNumbered など正常な判定結果では切り替えません。<br>
                    失敗時ポリシーによる AI 試行は最大 2 回です。OpenCode 側の形式補修は使わず、サーバー側で応答を検証します。<br>
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
                <template v-if="settings.ai_fallback_backend === 'OpenAICompatible' ||
                    settings.ai_fallback_backend === 'OpenAICompatible2'">
                    <div class="settings__item">
                        <div class="settings__item-heading">
                            予備 {{openAICompatibleBackendTitle(settings.ai_fallback_backend)}} の接続
                        </div>
                        <div class="settings__item-label">
                            「AIバックエンド」ページの対応する独立接続設定を使います。<br>
                            接続状態: {{fallback_auth_configured ? '設定済み' : '未設定'}}
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
                            <span>シリーズ所属</span><strong>{{status.assigned.toLocaleString()}}</strong>
                        </div>
                        <div class="recorded-series-status-card">
                            <span>未所属</span><strong>{{status.unassigned.toLocaleString()}}</strong>
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
                    :disabled="is_backfill_running || is_episode_backfill_running ||
                        is_series_backfill_available === false"
                    @click="startBackfill()">
                    <Icon icon="fluent:arrow-sync-20-filled" class="mr-2" width="22px" />
                    既存録画へ規則を再適用
                </v-btn>
            </div>

            <v-divider class="mt-7" />
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
                    :disabled="is_episode_backfill_running || is_backfill_running"
                    @click="startEpisodeBackfill()">
                    <Icon icon="fluent:globe-search-20-filled" class="mr-2" width="22px" />
                    既存録画の話数判定を開始
                </v-btn>
            </div>

            </div>

            <div class="settings__content">
                <v-divider class="mt-7"></v-divider>
                <div class="settings__item">
                    <div class="settings__item-heading">録画シリーズ管理</div>
                    <div class="settings__item-label">
                    判定済みのシリーズを検索し、表示するタイトルと説明を確認できます。<br>
                    </div>
                    <v-btn class="settings__save-button mt-4" variant="flat"
                        to="/settings/server/recorded-series/series">
                        <Icon icon="fluent:collections-20-filled" class="mr-2" width="22px" />録画シリーズ管理を開く
                    </v-btn>
                </div>
            </div>

            <div class="settings__content">
                <v-divider class="mt-7"></v-divider>
                <div class="settings__content-heading">
                    <Icon icon="fluent:movies-and-tv-20-filled" width="22px" />
                    <span class="ml-2">Bangumi 連携（共有・管理者1件）</span>
                </div>
                <div class="settings__item-label mb-4">
                    管理者が登録した1件の個人アクセストークンを、全ユーザーの看過同期に使います。<br>
                    トークンはサーバーへ暗号化して保存され、クライアントには返りません。<br>
                </div>
                <div class="bangumi-account bangumi-account--anonymous" v-if="bangumi_profile === null || bangumi_profile.bangumi_user_id === null">
                    <div class="bangumi-account__info">
                        <div class="bangumi-account__info-name">Bangumi アカウントと連携していません</div>
                    </div>
                    <v-btn color="secondary" variant="flat" height="42" @click="openBangumiLinkDialog()">連携する</v-btn>
                </div>
                <div class="bangumi-account" v-else>
                    <div class="bangumi-account__info">
                        <div class="bangumi-account__info-name">{{bangumi_profile.bangumi_user_nickname}} と連携しています</div>
                        <span class="bangumi-account__info-description">@{{bangumi_profile.bangumi_user_name}}</span>
                    </div>
                    <div class="bangumi-account__actions">
                        <v-btn color="secondary" variant="outlined" height="42" @click="openBangumiLinkDialog()">再連携</v-btn>
                        <v-btn color="secondary" variant="flat" height="42" @click="logoutBangumiAccount()">連携解除</v-btn>
                    </div>
                </div>
            </div>
        </template>

        <v-dialog width="550" v-model="bangumi_link_dialog">
            <v-card class="px-2 py-2">
                <v-card-title class="d-flex justify-center pt-6 font-weight-bold">Bangumi アカウントと連携</v-card-title>
                <v-card-text class="px-6 pt-4 pb-2">
                    <v-text-field color="primary" variant="outlined" label="個人アクセストークン" autocomplete="off"
                        :type="bangumi_token_showing ? 'text' : 'password'"
                        :append-inner-icon="bangumi_token_showing ? 'fa-solid:eye-slash' : 'fa-solid:eye'"
                        v-model="bangumi_access_token" @click:appendInner="bangumi_token_showing = !bangumi_token_showing">
                    </v-text-field>
                </v-card-text>
                <v-card-actions class="px-6 pb-5">
                    <v-spacer></v-spacer>
                    <v-btn variant="text" @click="closeBangumiLinkDialog()">キャンセル</v-btn>
                    <v-btn color="secondary" variant="flat" :loading="bangumi_linking"
                        :disabled="bangumi_access_token.trim() === ''" @click="loginBangumiAccount()">連携する</v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>

        <v-dialog :model-value="backfill_confirmation_dialog" :persistent="is_starting_selected_backfill"
            max-width="560" @update:model-value="updateBackfillConfirmationDialog">
            <v-card>
                <v-card-title>{{backfill_confirmation_title}}</v-card-title>
                <v-card-text>{{backfill_confirmation_message}}</v-card-text>
                <v-card-actions>
                    <v-spacer />
                    <v-btn variant="text" :disabled="is_starting_selected_backfill"
                        @click="updateBackfillConfirmationDialog(false)">キャンセル</v-btn>
                    <v-btn color="primary" variant="flat" :loading="is_starting_selected_backfill"
                        :disabled="backfill_confirmation_action === null" @click="confirmBackfill()">
                        判定を開始
                    </v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>

    </SettingsBase>
</template>

<script setup lang="ts">

import { computed, onMounted, onUnmounted, ref } from 'vue';

import Message from '@/message';
import AIBackend, {
    type AIAuthMode,
    type IACPBackendCredentialStatus,
    type IOpenAICompatibleSettings,
} from '@/services/AIBackend';
import AnalysisTasks, { type IAnalysisTaskExecution } from '@/services/AnalysisTasks';
import Bangumi, { type IBangumiProfile } from '@/services/Bangumi';
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
    {title: 'OpenAI 互換 API（直接 HTTP）', value: 'OpenAICompatible'},
    {title: 'OpenAI 互換 API 2（直接 HTTP）', value: 'OpenAICompatible2'},
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
const openai_compatible_settings = ref<IOpenAICompatibleSettings | null>(null);
const openai_compatible_2_settings = ref<IOpenAICompatibleSettings | null>(null);
const acp_credential_status = ref<IACPBackendCredentialStatus | null>(null);
const status = ref<IRecordedSeriesStatus | null>(null);

const is_loading = ref(true);
const is_disabled = ref(true);
const is_saving = ref(false);
const is_starting_backfill = ref(false);
const is_starting_episode_backfill = ref(false);
const is_monitoring_episode_backfill = ref(false);
const is_refreshing_status = ref(false);
const authorization_error = ref<'LoginRequired' | 'AdminRequired' | 'UserUnavailable' | null>(null);
const bangumi_profile = ref<IBangumiProfile | null>(null);
const bangumi_link_dialog = ref(false);
const bangumi_access_token = ref('');
const bangumi_token_showing = ref(false);
const bangumi_linking = ref(false);
const episode_backfill_task = ref<IAnalysisTaskExecution | null>(null);

type BackfillConfirmationAction = 'Series' | 'Episode';
const backfill_confirmation_dialog = ref(false);
const backfill_confirmation_action = ref<BackfillConfirmationAction | null>(null);
const backfill_confirmation_force = ref(false);
const backfill_confirmation_message = ref('');

const is_form_dense = Utils.isSmartphoneHorizontal();
const user_store = useUserStore();

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
/** 指定した OpenAI 互換 HTTP スロットの接続情報が揃っているかを返す。 */
function isOpenAICompatibleConfigured(backend: AIBackendKind | null): boolean {
    const current = backend === 'OpenAICompatible2'
        ? openai_compatible_2_settings.value
        : backend === 'OpenAICompatible'
            ? openai_compatible_settings.value
            : null;
    return current !== null
        && current.api_base_url !== null
        && current.model !== null
        && current.api_key_configured;
}

/** 録画シリーズ選択欄で使う OpenAI 互換 HTTP スロット名を返す。 */
function openAICompatibleBackendTitle(backend: AIBackendKind): string {
    return backend === 'OpenAICompatible2' ? 'OpenAI 互換 API 2' : 'OpenAI 互換 API';
}

const fallback_auth_configured = computed(() => {
    if (settings.value.ai_failure_recovery_strategy !== 'FallbackBackend') return false;
    const fallbackBackend = settings.value.ai_fallback_backend;
    if (fallbackBackend === 'OpenCode') {
        const selectedServiceID = settings.value.ai_fallback_backend_service_id?.toLowerCase() ?? null;
        const selectedService = opencode_services.value.find(service => service.value.toLowerCase() === selectedServiceID);
        return selectedService?.authConfigured === true;
    }
    if (fallbackBackend === 'OpenAICompatible' || fallbackBackend === 'OpenAICompatible2') {
        return isOpenAICompatibleConfigured(fallbackBackend);
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
    is_starting_backfill.value || status.value?.is_running === true,
);
const is_episode_backfill_running = computed(() =>
    is_starting_episode_backfill.value ||
    is_monitoring_episode_backfill.value ||
    status.value?.is_episode_running === true,
);
const is_starting_selected_backfill = computed(() => {
    if (backfill_confirmation_action.value === 'Series') return is_starting_backfill.value;
    if (backfill_confirmation_action.value === 'Episode') return is_starting_episode_backfill.value;
    return false;
});
const backfill_confirmation_title = computed(() =>
    backfill_confirmation_action.value === 'Episode' ?
        '既存録画の一括話数判定' :
        '既存録画へ Indexer を再適用',
);
const series_backfill_unavailable_message = computed(() => {
    if (saved_settings.value?.enabled === false) {
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


/** 既存録画へ Indexer を再適用する前に、範囲を確認する。 */
function startBackfill(): void {
    if (is_backfill_running.value || is_episode_backfill_running.value) return;
    if (is_series_backfill_available.value === false) {
        Message.warning('自動判定を有効にしてから既存録画へ規則を再適用してください。');
        return;
    }
    backfill_confirmation_action.value = 'Series';
    backfill_confirmation_force.value = false;
    backfill_confirmation_message.value =
        'HonomiTV と同じ確定規則を保存済みの全録画へ再適用します。AI が有効なら、付かなかった同一 EPG タイトル群をバックグラウンドで Web 検索します。続行しますか？';
    backfill_confirmation_dialog.value = true;
}

/** 確認ダイアログを閉じ、次に開いた操作へ古い対象を持ち越さない。 */
function updateBackfillConfirmationDialog(value: boolean): void {
    if (value === false && is_starting_selected_backfill.value) return;
    backfill_confirmation_dialog.value = value;
    if (value === false) {
        backfill_confirmation_action.value = null;
        backfill_confirmation_message.value = '';
    }
}

/** 確認時に固定した操作種別と force 値で、対応する一括判定を開始する。 */
async function confirmBackfill(): Promise<void> {
    const action = backfill_confirmation_action.value;
    const force = backfill_confirmation_force.value;
    if (action === null || is_starting_selected_backfill.value) return;

    if (action === 'Series') {
        await runBackfill(force);
    } else {
        await runEpisodeBackfill(force);
    }
}

/** 既存録画へ Indexer の確定規則を再適用する。完了まで API 応答を待つ。 */
async function runBackfill(_force: boolean): Promise<void> {

    is_starting_backfill.value = true;
    const accepted = await RecordedSeries.startBackfill(false);
    is_starting_backfill.value = false;
    if (accepted === null) return;
    updateBackfillConfirmationDialog(false);
    await refreshStatus();
    Message.success('既存録画へ Indexer の規則を再適用しました。');
}

/** 既存録画の一括話数判定を開始する前に、利用量と検索範囲を確認する。 */
function startEpisodeBackfill(): void {
    if (is_episode_backfill_running.value || is_backfill_running.value) return;
    backfill_confirmation_action.value = 'Episode';
    backfill_confirmation_force.value = false;
    backfill_confirmation_message.value =
        '話数が未確定の既存録画を、保存済みの AI 設定で Web 検索します。Indexer が単一の正整数を取れた録画は検索しません。続行しますか？';
    backfill_confirmation_dialog.value = true;
}

/** Series所属済みの既存録画を対象に、バックグラウンドで話数Web検索を実行する。 */
async function runEpisodeBackfill(force: boolean): Promise<void> {

    is_starting_episode_backfill.value = true;
    const accepted = await RecordedSeries.startEpisodeBackfill(false);
    is_starting_episode_backfill.value = false;
    if (accepted === null) return;
    updateBackfillConfirmationDialog(false);

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

function openBangumiLinkDialog(): void {
    bangumi_link_dialog.value = true;
}

function closeBangumiLinkDialog(): void {
    bangumi_link_dialog.value = false;
    bangumi_access_token.value = '';
    bangumi_token_showing.value = false;
}

async function loginBangumiAccount(): Promise<void> {
    if (bangumi_linking.value === true || bangumi_access_token.value.trim() === '') return;
    bangumi_linking.value = true;
    try {
        const result = await Bangumi.loginAccount({access_token: bangumi_access_token.value});
        if (result === false) return;
        bangumi_profile.value = await Bangumi.fetchProfile();
        closeBangumiLinkDialog();
        Message.success('Bangumi アカウントと連携しました。');
    } finally {
        bangumi_linking.value = false;
    }
}

async function logoutBangumiAccount(): Promise<void> {
    const result = await Bangumi.logoutAccount();
    if (result === false) return;
    bangumi_profile.value = await Bangumi.fetchProfile();
    Message.success('Bangumi アカウントとの連携を解除しました。');
}


onMounted(async () => {
    // 管理者と確認できるまでは設定 API へアクセスしない。
    const fetched_user = await user_store.fetchUser();
    // fetchUser() はアイコン取得だけが失敗した場合も null を返すが、ユーザー本体は Store に残る。
    const user = fetched_user ?? user_store.user;
    if (user === null) {
        authorization_error.value = Utils.getAccessToken() === null ? 'LoginRequired' : 'UserUnavailable';
        is_loading.value = false;
        return;
    }
    if (user.is_admin !== true) {
        authorization_error.value = 'AdminRequired';
        is_loading.value = false;
        return;
    }
    const [
        fetched_settings,
        fetched_status,
        fetched_services,
        fetched_openai_compatible_settings,
        fetched_openai_compatible_2_settings,
        fetched_acp_credentials,
        fetched_bangumi,
    ] = await Promise.all([
        RecordedSeries.fetchSettings(),
        RecordedSeries.fetchStatus(),
        AIBackend.fetchServices(),
        AIBackend.fetchOpenAICompatibleSettings(),
        AIBackend.fetchOpenAICompatibleSettings(2),
        AIBackend.fetchACPCredentialStatus(),
        Bangumi.fetchProfile(),
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
    if (fetched_openai_compatible_settings !== null) {
        openai_compatible_settings.value = fetched_openai_compatible_settings;
    }
    if (fetched_openai_compatible_2_settings !== null) {
        openai_compatible_2_settings.value = fetched_openai_compatible_2_settings;
    }
    if (fetched_settings !== null) {
        applyFetchedSettings(fetched_settings);
        is_disabled.value = false;
    }
    if (fetched_status !== null) {
        status.value = fetched_status;
        if (fetched_status.is_running || fetched_status.is_episode_running) startStatusPolling();
    }
    bangumi_profile.value = fetched_bangumi;
    is_loading.value = false;
});

onUnmounted(() => {
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

.bangumi-account {
    display: flex;
    align-items: center;
    gap: 12px;
    min-height: 72px;
    padding: 16px;
    border-radius: 12px;
    background: rgb(var(--v-theme-background-lighten-2));

    &__info {
        min-width: 0;
        flex-grow: 1;
    }

    &__info-name {
        font-weight: bold;
    }

    &__actions {
        display: flex;
        gap: 8px;
    }
}

</style>
