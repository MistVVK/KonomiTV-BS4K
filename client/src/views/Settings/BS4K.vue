<template>
    <SettingsViewContainer :embedded="embedded">
        <h2 class="settings__heading" v-if="embedded === false">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:tv-20-filled" width="22px" />
            <span class="ml-2">{{section_title}}</span>
        </h2>
        <div class="settings__description" v-if="embedded === false">
            {{section_description}}<br>
        </div>
        <div class="settings__description mt-1" v-if="embedded === false">
            [BS4K設定を更新] ボタンを押さずにこのページから離れると、変更内容は破棄されます。<br>
            変更を反映するには KonomiTV-BS4K サーバーの再起動が必要です。<br>
        </div>
        <div class="settings__content" v-if="isSectionVisible('server')"
            :class="{'settings__content--disabled': is_disabled}">
            <div class="settings__content-heading">
                <Icon icon="fluent:video-settings-20-filled" width="22px" />
                <span class="ml-2">エンコーダ</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">BS4K</div>
                <div class="settings__item-label">
                    BS4K チャンネルのライブ再生と、BS4K 放送の録画再生で利用します。<br>
                    通常チャンネルのライブ再生と BS4K 以外の録画再生には、通常タブで選んだエンコーダーを利用します。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="encoder_options"
                    v-model="server_settings.general.encoder_bs4k">
                </v-select>
            </div>
            <div class="settings__item settings__item--switch">
                <label class="settings__item-heading" for="encoder_bs4k_input_analysis_enabled">BS4K ライブ用の入力解析設定を有効にする</label>
                <label class="settings__item-label" for="encoder_bs4k_input_analysis_enabled">
                    有効にすると、BS4K のライブ視聴時のみ、下の入力解析サイズと時間を基準値として入力ストリームを解析します。<br>
                    無効にすると、通常チャンネルと同じ入力解析設定を使います。<br>
                </label>
                <v-switch class="settings__item-switch" color="primary" id="encoder_bs4k_input_analysis_enabled" hide-details
                    v-model="server_settings.general.encoder_bs4k_input_analysis_enabled">
                </v-switch>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">BS4K 入力解析サイズ (KB)</div>
                <div class="settings__item-label">
                    BS4K ライブ用の入力解析設定が有効な場合に、エンコーダーがストリーム情報を解析するために読み込むデータ量の基準値を設定します。デフォルトは 3000 です。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" type="number" hide-details
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model.number="server_settings.general.encoder_bs4k_input_probesize">
                </v-text-field>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">BS4K 入力解析時間 (秒)</div>
                <div class="settings__item-label">
                    BS4K ライブ用の入力解析設定が有効な場合に、エンコーダーが入力ストリームを解析する時間の基準値を設定します。デフォルトは 1.5 です。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" type="number" step="0.1" hide-details
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model.number="server_settings.general.encoder_bs4k_input_analyze">
                </v-text-field>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">BS4K mux 待ち幅 (ミリ秒)</div>
                <div class="settings__item-label">
                    BS4K のライブ視聴時のみ、映像や音声の多重化（mux）で許容する待ち時間の基準値を設定します。デフォルトは 800 です。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" type="number" hide-details
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model.number="server_settings.general.encoder_bs4k_max_interleave_delta">
                </v-text-field>
            </div>
            <div class="settings__item settings__item--switch">
                <label class="settings__item-heading" for="encoder_bs4k_low_latency">
                    <span>BS4K エンコーダーを即時出力優先にする</span>
                    <span class="bs4k-warning-badge">注意</span>
                </label>
                <label class="settings__item-label" for="encoder_bs4k_low_latency">
                    有効にすると、BS4K のライブ視聴時のみ、エンコーダーのバッファを抑えて即時出力するオプションを適用します。<br>
                </label>
                <v-switch class="settings__item-switch" color="primary" id="encoder_bs4k_low_latency" hide-details
                    :model-value="server_settings.general.encoder_bs4k_low_latency"
                    @update:model-value="updateEncoderBS4KLowLatency($event)">
                </v-switch>
            </div>
            <div class="settings__item settings__item--switch">
                <label class="settings__item-heading" for="bs4k_live_startup_discard_enabled">BS4K ライブ開始時に先頭 TS を破棄する</label>
                <label class="settings__item-label" for="bs4k_live_startup_discard_enabled">
                    有効にすると、MPEG-TS での BS4K ライブ視聴開始時のみ、チューナー切り替え直後の TS を下の設定秒数だけエンコーダーへ渡さず破棄します（MMT/TLV は対象外）。<br>
                </label>
                <v-switch class="settings__item-switch" color="primary" id="bs4k_live_startup_discard_enabled" hide-details
                    v-model="server_settings.general.bs4k_live_startup_discard_enabled">
                </v-switch>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">BS4K ライブ先頭 TS 破棄秒数</div>
                <div class="settings__item-label">
                    BS4K ライブ開始時の先頭 TS 破棄が有効な場合に、エンコーダーへ渡さず破棄する秒数を設定します。0 に設定すると破棄しません。デフォルトは 2.0 です。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" type="number" step="0.1" hide-details
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model.number="server_settings.general.bs4k_live_startup_discard_seconds">
                </v-text-field>
            </div>
        </div>
        <div class="settings__content" v-if="isSectionVisible('server')"
            :class="{'settings__content--disabled': is_disabled}">
            <div class="settings__content-heading">
                <Icon icon="fluent:play-circle-20-filled" width="22px" />
                <span class="ml-2">プレイヤー</span>
            </div>
            <div class="settings__item settings__item--switch" :class="{'settings__item--disabled': is_disabled}">
                <label class="settings__item-heading" for="bs4k_ignore_viewer_low_latency">
                    <span>BS4K プレイヤーを通常バッファで再生する</span>
                    <span class="bs4k-warning-badge">注意</span>
                </label>
                <label class="settings__item-label" for="bs4k_ignore_viewer_low_latency">
                    有効にすると、BS4K のライブ視聴時だけユーザーの低遅延視聴設定を使わず、通常の再生バッファを使います。<br>
                    変更を反映するには配信・エンコーダー設定を更新し、KonomiTV-BS4K サーバーを再起動してください。<br>
                </label>
                <v-switch class="settings__item-switch" color="primary" id="bs4k_ignore_viewer_low_latency" hide-details
                    :model-value="server_settings.general.bs4k_ignore_viewer_low_latency" :disabled="is_disabled"
                    @update:model-value="updateBS4KIgnoreViewerLowLatency($event)">
                </v-switch>
            </div>
        </div>
        <div class="settings__content" v-if="isSectionVisible('server') && show_save_button"
            :class="{'settings__content--disabled': is_disabled}">
            <div class="settings__content-heading">
                <Icon icon="fluent:arrow-counterclockwise-20-filled" width="22px" />
                <span class="ml-2">反映</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">BS4K設定を更新</div>
                <div class="settings__item-label">
                    エンコーダ設定と一部のプレイヤー設定を config.yaml に保存します。<br>
                    保存した変更を反映するには KonomiTV-BS4K サーバーの再起動が必要です。<br>
                </div>
            </div>
            <v-btn class="settings__save-button bg-secondary mt-5" variant="flat" @click="updateServerSettings()">
                <Icon icon="fluent:save-16-filled" class="mr-2" height="23px" />BS4K設定を更新
            </v-btn>
        </div>
        <v-dialog v-model="encoder_bs4k_low_latency_warning_dialog" max-width="560">
            <v-card>
                <v-card-title>BS4K エンコーダーの即時出力優先を有効化</v-card-title>
                <v-card-text>
                    <v-alert class="mb-4" color="warning" variant="tonal">
                        BS4K のライブ視聴時に、プチフリーズのような再読み込みが多発するおそれがあります。
                    </v-alert>
                    再生の安定性よりもエンコーダーからの即時出力を優先する設定です。<br>
                    内容を理解した上で、それでも利用する場合だけ有効にしてください。
                </v-card-text>
                <v-card-actions>
                    <v-spacer />
                    <v-btn variant="text" @click="encoder_bs4k_low_latency_warning_dialog = false">キャンセル</v-btn>
                    <v-btn color="warning" variant="flat" @click="confirmEncoderBS4KLowLatency()">
                        それでも有効にする
                    </v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>
        <v-dialog v-model="bs4k_ignore_viewer_low_latency_warning_dialog" max-width="560">
            <v-card>
                <v-card-title>BS4K 通常バッファの強制を解除</v-card-title>
                <v-card-text>
                    <v-alert class="mb-4" color="warning" variant="tonal">
                        ユーザー側で低遅延視聴が有効な場合、BS4K のライブ視聴時にプチフリーズのような再読み込みが多発するおそれがあります。
                    </v-alert>
                    通常バッファの強制を解除し、ユーザーごとの低遅延視聴設定を優先します。<br>
                    内容を理解した上で、それでも利用する場合だけオフにしてください。
                </v-card-text>
                <v-card-actions>
                    <v-spacer />
                    <v-btn variant="text" @click="bs4k_ignore_viewer_low_latency_warning_dialog = false">キャンセル</v-btn>
                    <v-btn color="warning" variant="flat" @click="confirmBS4KIgnoreViewerLowLatencyDisabled()">
                        それでもオフにする
                    </v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>
    </SettingsViewContainer>
</template>
<script lang="ts" setup>

import { storeToRefs } from 'pinia';
import { computed, ref, toRaw, watch } from 'vue';

import type { IServerSettings } from '@/services/Settings';

import SettingsViewContainer from '@/components/Settings/SettingsViewContainer.vue';
import Message from '@/message';
import useServerSettingsStore from '@/stores/ServerSettingsStore';
import useUserStore from '@/stores/UserStore';
import Utils from '@/utils';

type BS4KSettingsSection = 'server' | 'all';

const props = withDefaults(defineProps<{
    section?: BS4KSettingsSection;
    embedded?: boolean;
    showSaveButton?: boolean;
}>(), {
    section: 'all',
    embedded: false,
    showSaveButton: true,
});
const shared_server_settings = defineModel<IServerSettings>('serverSettings');

// BS4K の端末別再生設定とサーバー共有設定を別ルートから表示できるようにする
const section = computed(() => props.section);
const embedded = computed(() => props.embedded);
const show_save_button = computed(() => props.showSaveButton);
const section_title = computed(() => ({
    server: 'BS4K配信・エンコーダー',
    all: 'BS4K設定',
})[props.section]);
const section_description = computed(() =>
    'BS4K のライブ視聴と、ONID=11 の録画再生に関するサーバー設定です。'
);

function isSectionVisible(target_section: Exclude<BS4KSettingsSection, 'all'>): boolean {
    return props.section === 'all' || props.section === target_section;
}

// フォームを小さくするかどうか
const is_form_dense = Utils.isSmartphoneHorizontal();
const encoder_bs4k_low_latency_warning_dialog = ref(false);
const bs4k_ignore_viewer_low_latency_warning_dialog = ref(false);

// 表示名だけを利用者向けに短縮し、value はサーバー API との既存契約を維持する
const encoder_options = [
    {title: 'CPU', value: 'FFmpeg'},
    {title: 'QSV (Intel Graphics 搭載 CPU / Intel Arc GPU で利用可能)', value: 'QSVEncC'},
    {title: 'NVENC (NVIDIA GPU で利用可能)', value: 'NVEncC'},
    {title: 'VCE (AMD GPU で利用可能)', value: 'VCEEncC'},
];

// ユーザー情報を取得し、もし管理者権限であれば無効化を解除
const is_disabled = ref(true);
const user_store = useUserStore();
user_store.fetchUser().then((user) => {
    if (user && user.is_admin) {
        is_disabled.value = false;
    }
});

// ストアには最後に取得・保存した基準値だけを保持し、この画面では section ごとのローカルドラフトを編集する
const server_settings_store = useServerSettingsStore();
const { server_settings: base_server_settings } = storeToRefs(server_settings_store);
const local_server_settings = ref<IServerSettings>(structuredClone(toRaw(base_server_settings.value)));
const server_settings = computed<IServerSettings>({
    get: () => shared_server_settings.value ?? local_server_settings.value,
    set: (settings) => {
        if (shared_server_settings.value !== undefined) {
            shared_server_settings.value = settings;
        } else {
            local_server_settings.value = settings;
        }
    },
});

/** 注意事項への明示的な同意が得られるまで、即時出力優先を有効化しない。 */
function updateEncoderBS4KLowLatency(enabled: boolean | null): void {
    if (enabled !== true) {
        server_settings.value.general.encoder_bs4k_low_latency = false;
        encoder_bs4k_low_latency_warning_dialog.value = false;
        return;
    }
    if (server_settings.value.general.encoder_bs4k_low_latency === false) {
        encoder_bs4k_low_latency_warning_dialog.value = true;
    }
}

/** 警告を確認して利用継続を選んだ場合だけ、即時出力優先を有効化する。 */
function confirmEncoderBS4KLowLatency(): void {
    server_settings.value.general.encoder_bs4k_low_latency = true;
    encoder_bs4k_low_latency_warning_dialog.value = false;
}

/** 通常バッファをオフにする場合だけ警告し、オンへの切り替えはそのまま反映する。 */
function updateBS4KIgnoreViewerLowLatency(enabled: boolean | null): void {
    if (enabled === true) {
        server_settings.value.general.bs4k_ignore_viewer_low_latency = true;
        bs4k_ignore_viewer_low_latency_warning_dialog.value = false;
        return;
    }
    if (server_settings.value.general.bs4k_ignore_viewer_low_latency === true) {
        bs4k_ignore_viewer_low_latency_warning_dialog.value = true;
    }
}

/** 警告を確認して利用継続を選んだ場合だけ、通常バッファの強制を解除する。 */
function confirmBS4KIgnoreViewerLowLatencyDisabled(): void {
    server_settings.value.general.bs4k_ignore_viewer_low_latency = false;
    bs4k_ignore_viewer_low_latency_warning_dialog.value = false;
}

function resetServerSettingsDraft(): void {
    local_server_settings.value = structuredClone(toRaw(base_server_settings.value));
}

// 配信・エンコーダーページから共有ドラフトを受け取った場合、取得と保存は親画面へ集約する
if (shared_server_settings.value === undefined) {
    server_settings_store.fetchServerSettingsOnce().then((settings) => {
        if (settings !== null) {
            resetServerSettingsDraft();
        }
    });
}

// 同じコンポーネントを使う端末設定・サーバー設定間の移動時に、未保存のサーバー設定を破棄する
watch(() => props.section, () => {
    if (shared_server_settings.value === undefined) {
        resetServerSettingsDraft();
    }
});

// サーバー設定を更新する関数
async function updateServerSettings() {
    // サーバー管理の各 section と同じ正規化・更新経路を利用する
    const result = await server_settings_store.updateServerSettings(server_settings.value);

    // 成功した場合のみメッセージを表示
    // エラー処理は Services 層で行われるため、ここではエラー処理は不要
    // 再起動するまでは設定データは反映されないため、再起動せずにページをリロードすると反映されてないように見える点に注意
    if (result === true) {
        resetServerSettingsDraft();
        Message.success('BS4K設定を更新しました。\n変更を反映するためには、KonomiTV-BS4K サーバーを再起動してください。');
    }
}

</script>

<style lang="scss" scoped>

.bs4k-warning-badge {
    flex-shrink: 0;
    margin-left: 8px;
    padding: 1px 6px;
    border-radius: 4px;
    background: rgb(var(--v-theme-warning));
    color: rgb(var(--v-theme-on-warning));
    font-size: 10px;
    font-weight: 700;
    line-height: 18px;
    letter-spacing: 0.08em;
}

</style>
