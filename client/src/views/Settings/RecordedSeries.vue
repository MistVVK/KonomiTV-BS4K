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
            録画のタイトルからシリーズ確定規則でシリーズを付けます。<br>
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
            <span v-else>ユーザー情報を取得できませんでした。</span>
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
                    有効時、Indexer が所属を付けられなかった同一 EPG タイトル群をまとめて Web 検索で補完します。<br>
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
                    AI が有効で Series が確定している話数不明録画は、設定されたバックエンドで Web 検索します。<br>
                    Web 検索・出力形式・公開 URL の根拠を確認できた結果は、AI の信頼度表示にかかわらず自動採用します。<br>
                    根拠不足・検索失敗・不正な応答では現在の正本を変更しません。<br>
                    既存録画はメンテナンス画面の一括話数判定から検索できます。<br>
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
                    OpenCode は「設定 → AIバックエンド」で登録した service ごとに選べます。<br>
                    2つの OpenAI 互換 API は OpenCode を経由せず、それぞれ保存済みの HTTP 接続を使います。<br>
                    ACP / Codex・Grok はコンテナ内の CLI を起動します。<br>
                    Web 検索の接続確認は AIバックエンド画面から行えます。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="primary_ai_backend_options" item-title="title" item-value="value"
                    :item-props="backendOptionItemProps"
                    :error-messages="primary_backend_error"
                    v-model="primary_backend_target" />
            </div>

            <!-- OpenCode の接続情報は AI バックエンドページだけで管理する -->
            <template v-if="settings.ai_backend === 'OpenCode'">
            <div class="settings__item">
                <div class="settings__item-heading">OpenCode service の接続</div>
                <div class="settings__item-label">
                    API キー・モデル・月次上限は「AIバックエンド」ページで管理します。<br>
                </div>
                <v-btn class="settings__save-button mt-3" variant="flat" to="/settings/server/ai-backends">
                    <Icon icon="fluent:settings-20-regular" class="mr-2" width="21px" />
                    AIバックエンド設定を開く
                </v-btn>
            </div>
            </template>

            <!-- 独立 OpenAI 互換 HTTP の接続情報は AI バックエンドページだけで管理する -->
            <template v-if="settings.ai_backend === 'OpenAICompatible' || settings.ai_backend === 'OpenAICompatible2'">
                <div class="settings__item">
                    <div class="settings__item-heading">{{openAICompatibleBackendTitle(settings.ai_backend)}} の接続</div>
                    <div class="settings__item-label">
                        API ベース URL・モデル・API キー・接続試験は「AIバックエンド」ページで設定します。<br>
                        設定状態: {{isOpenAICompatibleConfigured(settings.ai_backend) ? '設定済み' : '未設定'}}
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
                    主系 AI が技術的に失敗したとき、またはシリーズ未確定・話数の根拠不足のときにどうするかを決めます。<br>
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
                        :items="fallback_ai_backend_options" item-title="title" item-value="value"
                        :item-props="backendOptionItemProps"
                        :error-messages="fallback_backend_error || fallback_opencode_service_error ||
                            fallback_auth_error"
                        v-model="fallback_backend_target" />
                </div>
                <template v-if="settings.ai_fallback_backend === 'OpenCode'">
                    <div class="settings__item">
                        <div class="settings__item-heading">予備 OpenCode service の接続</div>
                        <div class="settings__item-label">
                            接続情報は「AIバックエンド」ページで管理します。参照中の service は削除できません。<br>
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
                            設定状態: {{fallback_auth_configured ? '設定済み' : '未設定'}}
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
            <div class="settings__content-heading mt-7">
                <Icon icon="fluent:cloud-20-filled" width="22px" />
                <span class="ml-2">外部メタデータソース</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">シリーズメタデータの外部ソース</div>
                <div class="settings__item-label">
                    シリーズの補完メタデータ（作品名・概要・画像・話数構造）を TMDb と Bangumi (bgm.tv) のどちらから取るかを選びます。<br>
                    併用時は両方を照合し、Bangumi 由来の話数構造があるシリーズでは Bangumi を優先します。<br>
                    外部ソースはシリーズ本体のタイトル・説明・ジャンルを上書きせず、取得した情報は別の補完データとして保持されます。<br>
                    「なし」にすると新しい照合・同期だけを止め、保存済みの照合データは残します。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="external_metadata_source_options" item-title="title" item-value="value"
                    v-model="settings.external_metadata_source" />
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
                    <div class="settings__item-heading">最終更新</div>
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

            <v-divider class="mt-7" />
            <div class="settings__item">
                <div class="settings__item-heading">シリーズデータベースの削除と一括操作</div>
                <div class="settings__item-label">
                    シリーズデータベースの削除・既存録画へ Indexer を再適用・既存録画の一括話数判定は、<br>
                    メンテナンス画面のシリーズ欄で行います。<br>
                </div>
            </div>
            <div class="settings__item">
                <v-btn class="settings__save-button mt-4" variant="flat"
                    to="/settings/maintenance">
                    <Icon icon="fluent:wrench-settings-20-filled" class="mr-2" width="22px" />メンテナンスを開く
                </v-btn>
            </div>

            </div>

            <div class="settings__content">
                <v-divider class="mt-7"></v-divider>
                <div class="settings__item">
                    <div class="settings__item-heading">録画シリーズ管理</div>
                    <div class="settings__item-label">
                    シリーズやその話数詳細、シリーズ未所属の録画を確認できます。<br>
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
                    <Icon icon="fluent:cloud-20-filled" width="22px" />
                    <span class="ml-2">TMDb 連携（API キー）</span>
                </div>
                <div class="settings__item-label mb-4">
                    TMDb の作品検索・詳細・話数構造の取得に使う v3 API キーです。<br>
                    キーはサーバー内で暗号化して保存され、画面にはマスク以外返りません。<br>
                    TMDb を使うかどうかは上の「外部メタデータソース」で選びます。<br>
                </div>
                <div class="settings__item">
                    <div class="settings__item-heading">保存済み API キー</div>
                    <div class="settings__item-label">
                        状態:
                        <template v-if="settings.tmdb_api_key_configured">
                            設定済み（{{settings.tmdb_api_key_masked}}）
                        </template>
                        <template v-else>
                            未設定
                        </template>
                        <br>
                        未設定でも TMDb 以外の照合は続行します。キーを保存すると、シリーズの照合をバックグラウンドで開始します。<br>
                        <template v-if="tmdb_connection_test_message !== ''">
                            接続試験: {{tmdb_connection_test_message}}<br>
                        </template>
                    </div>
                    <v-btn class="settings__save-button mt-3 mr-2" color="secondary" variant="flat"
                        :disabled="is_disabled" @click="openTmdbKeyDialog()">
                        <Icon icon="fluent:key-20-filled" class="mr-2" width="21px" />API キーを設定
                    </v-btn>
                    <v-btn class="settings__save-button mt-3" color="secondary" variant="outlined"
                        :loading="is_testing_tmdb_connection" :disabled="is_disabled" @click="testTmdbConnection()">
                        接続試験
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
                    管理者が登録した1件の個人アクセストークンを使い、各ユーザーの録画視聴完了を共有 Bangumi アカウントへ視聴済みとして反映します。<br>
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

        <v-dialog width="550" :model-value="tmdb_key_dialog"
            :persistent="is_saving_tmdb_key || is_deleting_tmdb_key"
            @update:model-value="value => { if (!value) closeTmdbKeyDialog(); }">
            <v-card class="px-2 py-2">
                <v-card-title class="d-flex justify-center pt-6 font-weight-bold">TMDb API キーを設定</v-card-title>
                <v-card-text class="px-6 pt-4 pb-2">
                    <div class="settings__item-label mb-3">
                        キー本体は保存後に再表示できません。変更時は新しいキーを入力してください。キーを削除しても取得済みのメタデータは保持されます。
                    </div>
                    <v-text-field color="primary" variant="outlined" label="v3 API キー" autocomplete="off"
                        :disabled="is_saving_tmdb_key || is_deleting_tmdb_key"
                        spellcheck="false" :type="tmdb_key_showing ? 'text' : 'password'"
                        :append-inner-icon="tmdb_key_showing ? 'fa-solid:eye-slash' : 'fa-solid:eye'"
                        v-model="tmdb_api_key" @click:appendInner="tmdb_key_showing = !tmdb_key_showing">
                    </v-text-field>
                </v-card-text>
                <v-card-actions class="px-6 pb-5">
                    <v-btn v-if="settings.tmdb_api_key_configured" color="error" variant="text"
                        :loading="is_deleting_tmdb_key" :disabled="is_saving_tmdb_key"
                        @click="deleteTmdbKey()">キー削除</v-btn>
                    <v-spacer></v-spacer>
                    <v-btn variant="text" :disabled="is_saving_tmdb_key || is_deleting_tmdb_key"
                        @click="closeTmdbKeyDialog()">キャンセル</v-btn>
                    <v-btn color="secondary" variant="flat" :loading="is_saving_tmdb_key"
                        :disabled="is_deleting_tmdb_key || tmdb_api_key.trim() === ''"
                        @click="saveTmdbKey()">保存する</v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>

    </SettingsBase>
</template>

<script setup lang="ts">

import { computed, onMounted, ref } from 'vue';

import Message from '@/message';
import AIBackend, {
    type AIAuthMode,
    type IACPBackendCredentialStatus,
    type IOpenAICompatibleSettings,
} from '@/services/AIBackend';
import Bangumi, { type IBangumiProfile } from '@/services/Bangumi';
import RecordedSeries, {
    type AIBackendKind,
    type AIFailureRecoveryStrategy,
    type ExternalMetadataSource,
    type IRecordedSeriesSettings,
    type IRecordedSeriesSettingsUpdate,
    type IRecordedSeriesStatus,
} from '@/services/RecordedSeries';
import useUserStore from '@/stores/UserStore';
import Utils, { dayjs } from '@/utils';
import SettingsBase from '@/views/Settings/Base.vue';


interface AIBackendOption {
    title: string;
    value: string;
    backend: AIBackendKind;
    serviceId: string | null;
}
const fixed_ai_backend_options: AIBackendOption[] = [
    {
        title: 'OpenAI 互換 API（直接 HTTP）',
        value: 'OpenAICompatible',
        backend: 'OpenAICompatible',
        serviceId: null,
    },
    {
        title: 'OpenAI 互換 API 2（直接 HTTP）',
        value: 'OpenAICompatible2',
        backend: 'OpenAICompatible2',
        serviceId: null,
    },
    {title: 'ACP / Codex', value: 'AcpCodex', backend: 'AcpCodex', serviceId: null},
    {title: 'ACP / Grok Build', value: 'AcpGrok', backend: 'AcpGrok', serviceId: null},
];
const supported_ai_backend_kinds: AIBackendKind[] = [
    'OpenCode',
    'OpenAICompatible',
    'OpenAICompatible2',
    'AcpCodex',
    'AcpGrok',
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
const external_metadata_source_options: {title: string; value: ExternalMetadataSource;}[] = [
    {title: 'TMDb + Bangumi（既定）', value: 'TmdbAndBangumi'},
    {title: 'TMDb のみ', value: 'TmdbOnly'},
    {title: 'Bangumi (bgm.tv) のみ', value: 'BangumiOnly'},
    {title: 'なし（新規の照合・同期を停止）', value: 'None'},
];

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
    external_metadata_source: 'TmdbAndBangumi',
    tmdb_api_key_configured: false,
    tmdb_api_key_masked: null,
});
const opencode_services = ref<{title: string; value: string; authConfigured: boolean;}[]>([]);
const ai_backend_options = computed<AIBackendOption[]>(() => [
    ...opencode_services.value.map(service => ({
        title: `OpenCode / ${service.title}`,
        value: `OpenCode:${service.value}`,
        backend: 'OpenCode' as const,
        serviceId: service.value,
    })),
    ...fixed_ai_backend_options,
]);
const openai_compatible_settings = ref<IOpenAICompatibleSettings | null>(null);
const openai_compatible_2_settings = ref<IOpenAICompatibleSettings | null>(null);
const acp_credential_status = ref<IACPBackendCredentialStatus | null>(null);
const status = ref<IRecordedSeriesStatus | null>(null);

const is_loading = ref(true);
const is_disabled = ref(true);
const is_saving = ref(false);
const is_refreshing_status = ref(false);
const authorization_error = ref<'LoginRequired' | 'AdminRequired' | 'UserUnavailable' | null>(null);
const bangumi_profile = ref<IBangumiProfile | null>(null);
const bangumi_link_dialog = ref(false);
const bangumi_access_token = ref('');
const bangumi_token_showing = ref(false);
const bangumi_linking = ref(false);
// TMDb API キーは本体を取得できないため、入力ダイアログの値とマスク表示だけを画面で持つ。
const tmdb_key_dialog = ref(false);
const tmdb_api_key = ref('');
const tmdb_key_showing = ref(false);
const is_saving_tmdb_key = ref(false);
const is_deleting_tmdb_key = ref(false);
const is_testing_tmdb_connection = ref(false);
const tmdb_connection_test_message = ref('');

const is_form_dense = Utils.isSmartphoneHorizontal();
const user_store = useUserStore();


/** API の2フィールドを、画面上の1つの選択値へ変換する。 */
function backendTargetValue(backend: AIBackendKind | null, serviceId: string | null): string {
    if (backend === null) return '';
    if (backend === 'OpenCode') return serviceId === null ? '' : `OpenCode:${serviceId}`;
    return backend;
}

/** 一覧から選んだ1ターゲットを既存の backend / service_id フィールドへ分離する。 */
function resolveBackendTarget(target: string): AIBackendOption | null {
    return ai_backend_options.value.find(option => option.value === target) ?? null;
}

/** Vuetify がラップした選択肢から、主系・予備の重複禁止 props を取り出す。 */
function backendOptionItemProps(item: unknown): Record<string, unknown> {
    if (item === null || typeof item !== 'object') return {};
    const record = item as {raw?: {props?: Record<string, unknown>}; props?: Record<string, unknown>};
    return record.raw?.props ?? record.props ?? {};
}

const primary_backend_target = computed<string>({
    get: () => backendTargetValue(settings.value.ai_backend, settings.value.ai_backend_service_id),
    set: (target) => {
        const option = resolveBackendTarget(target);
        if (option === null) return;
        settings.value.ai_backend = option.backend;
        settings.value.ai_backend_service_id = option.serviceId;
    },
});
const fallback_backend_target = computed<string>({
    get: () => backendTargetValue(
        settings.value.ai_fallback_backend,
        settings.value.ai_fallback_backend_service_id,
    ),
    set: (target) => {
        const option = resolveBackendTarget(target);
        if (option === null) return;
        settings.value.ai_fallback_backend = option.backend;
        settings.value.ai_fallback_backend_service_id = option.serviceId;
    },
});
const primary_ai_backend_options = computed(() => ai_backend_options.value.map(option => ({
    ...option,
    props: {
        disabled: (
            settings.value.ai_failure_recovery_strategy === 'FallbackBackend'
            && areBackendTargetsIdentical(
                option.backend,
                option.serviceId,
                settings.value.ai_fallback_backend,
                settings.value.ai_fallback_backend_service_id,
            )
        ),
    },
})));
const fallback_ai_backend_options = computed(() => ai_backend_options.value.map(option => ({
    ...option,
    props: {
        disabled: areBackendTargetsIdentical(
            settings.value.ai_backend,
            settings.value.ai_backend_service_id,
            option.backend,
            option.serviceId,
        ),
    },
})));

const primary_backend_error = computed(() => {
    if (settings.value.ai_backend !== 'OpenCode') return '';
    if (!settings.value.ai_backend_service_id) {
        return 'OpenCode service を選択してください。';
    }
    if (opencode_services.value.some(service => service.value === settings.value.ai_backend_service_id) === false) {
        return '選択中の OpenCode service は登録されていません。';
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
    if (!settings.value.ai_fallback_backend_service_id) {
        return '予備 OpenCode service を選択してください。';
    }
    if (
        opencode_services.value.some(
            service => service.value === settings.value.ai_fallback_backend_service_id,
        ) === false
    ) {
        return '選択中の予備 OpenCode service は登録されていません。';
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
    primary_backend_error.value !== ''
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
        external_metadata_source: settings.value.external_metadata_source,
    };
}

/** GET の保存済み snapshot を画面へ反映する。 */
function applyFetchedSettings(fetched_settings: IRecordedSeriesSettings): void {
    // ローリング更新中の旧サーバーや古い mock が廃止済み backend を返しても、
    // 一覧外の値を表示したり任意コマンド設定へ戻ったりしないようクライアントでも fail-closed にする。
    const is_supported_backend = supported_ai_backend_kinds.includes(fetched_settings.ai_backend);
    const is_supported_fallback = (
        fetched_settings.ai_fallback_backend === null
        || supported_ai_backend_kinds.includes(fetched_settings.ai_fallback_backend)
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
}

/** 判定状況を更新する。一括操作はメンテナンス画面へ移設したため追跡しない。 */
async function refreshStatus(show_error = false): Promise<void> {
    if (is_refreshing_status.value) return;
    is_refreshing_status.value = true;
    const fetched_status = await RecordedSeries.fetchStatus(show_error);
    is_refreshing_status.value = false;
    if (fetched_status === null) return;

    status.value = fetched_status;
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
        Message.success('録画シリーズ設定を更新しました。');
    }
    is_saving.value = false;
}


function formatLastRunAt(value: string | null): string {
    return value === null ? '記録なし' : dayjs(value).format('YYYY/M/D HH:mm:ss');
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

function openTmdbKeyDialog(): void {
    tmdb_key_dialog.value = true;
}

function closeTmdbKeyDialog(): void {
    tmdb_key_dialog.value = false;
    tmdb_api_key.value = '';
    tmdb_key_showing.value = false;
}

/** キー保存・削除後のマスク表示だけを設定 GET から取り直す。 */
async function refreshTmdbKeyState(): Promise<void> {
    const fetched_settings = await RecordedSeries.fetchSettings();
    if (fetched_settings === null) return;
    // 未保存のドラフト（AI バックエンドや外部メタデータソース）を巻き戻さないよう、キー表示だけ更新する。
    settings.value.tmdb_api_key_configured = fetched_settings.tmdb_api_key_configured;
    settings.value.tmdb_api_key_masked = fetched_settings.tmdb_api_key_masked;
}

/** TMDb API キーを Fernet ストアへ保存する。 */
async function saveTmdbKey(): Promise<void> {
    if (is_saving_tmdb_key.value || is_deleting_tmdb_key.value || tmdb_api_key.value.trim() === '') return;
    is_saving_tmdb_key.value = true;
    try {
        const result = await RecordedSeries.setTmdbAPIKey(tmdb_api_key.value);
        if (result === false) return;
        await refreshTmdbKeyState();
        closeTmdbKeyDialog();
        tmdb_connection_test_message.value = '';
        Message.success('TMDb API キーを保存しました。');
    } finally {
        is_saving_tmdb_key.value = false;
    }
}

/** 保存済みの TMDb API キーを削除する。既存の TMDb メタデータは残る。 */
async function deleteTmdbKey(): Promise<void> {
    if (is_deleting_tmdb_key.value || is_saving_tmdb_key.value) return;
    is_deleting_tmdb_key.value = true;
    try {
        const result = await RecordedSeries.deleteTmdbAPIKey();
        if (result === false) return;
        await refreshTmdbKeyState();
        closeTmdbKeyDialog();
        tmdb_connection_test_message.value = '';
        Message.success('TMDb API キーを削除しました。');
    } finally {
        is_deleting_tmdb_key.value = false;
    }
}

/** 保存済みキーで TMDb へ実通信し、成否を画面とトーストへ出す。 */
async function testTmdbConnection(): Promise<void> {
    if (is_testing_tmdb_connection.value === true) return;
    is_testing_tmdb_connection.value = true;
    try {
        const result = await RecordedSeries.testTmdbConnection();
        if (result === null) return;
        tmdb_connection_test_message.value = result.success
            ? `成功（${result.latency_ms}ms）`
            : `${result.message}（${result.latency_ms}ms）`;
        if (result.success) {
            Message.success('TMDb の接続試験に成功しました。');
        } else {
            Message.warning(result.message);
        }
    } finally {
        is_testing_tmdb_connection.value = false;
    }
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
    }
    bangumi_profile.value = fetched_bangumi;
    is_loading.value = false;
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
