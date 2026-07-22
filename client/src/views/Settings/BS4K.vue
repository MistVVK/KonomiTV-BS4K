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
        <div class="settings__description mt-1" v-if="embedded === false && section !== 'quality'">
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
                    BS4K のライブ再生と、BS4K（ONID=11）の録画再生で利用します。<br>
                    通常のライブ再生と録画再生には、通常側のタブで選んだエンコーダーを利用します。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="encoder_options"
                    v-model="server_settings.general.encoder_bs4k">
                </v-select>
            </div>
            <div class="settings__item settings__item--switch">
                <label class="settings__item-heading" for="encoder_bs4k_input_analysis_enabled">BS4K ライブ入力解析を強化する</label>
                <label class="settings__item-label" for="encoder_bs4k_input_analysis_enabled">
                    有効にすると、BS4K のライブ視聴時だけ下の入力解析サイズ・時間を使います。<br>
                    無効にすると、通常チャンネルと同じ入力解析設定を使います。<br>
                </label>
                <v-switch class="settings__item-switch" color="primary" id="encoder_bs4k_input_analysis_enabled" hide-details
                    v-model="server_settings.general.encoder_bs4k_input_analysis_enabled">
                </v-switch>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">BS4K 入力解析サイズ (KB)</div>
                <div class="settings__item-label">
                    BS4K ライブ入力解析を強化する場合に、エンコーダーが映像ヘッダーを探すために読むデータ量を設定します。デフォルトは 3000 です。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" type="number" hide-details
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model.number="server_settings.general.encoder_bs4k_input_probesize">
                </v-text-field>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">BS4K 入力解析時間 (秒)</div>
                <div class="settings__item-label">
                    BS4K ライブ入力解析を強化する場合に、エンコーダーが入力ストリームを解析する時間を設定します。デフォルトは 1.5 です。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" type="number" step="0.1" hide-details
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model.number="server_settings.general.encoder_bs4k_input_analyze">
                </v-text-field>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">BS4K mux 待ち幅 (KB)</div>
                <div class="settings__item-label">
                    BS4K のライブ視聴と ONID=11 の録画再生時だけ、映像と音声の多重化で許容する待ち幅を設定します。デフォルトは 800 です。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" type="number" hide-details
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model.number="server_settings.general.encoder_bs4k_max_interleave_delta">
                </v-text-field>
            </div>
            <div class="settings__item settings__item--switch">
                <label class="settings__item-heading" for="encoder_bs4k_low_latency">BS4K エンコーダーを即時出力優先にする</label>
                <label class="settings__item-label" for="encoder_bs4k_low_latency">
                    有効にすると、BS4K のライブ視聴と ONID=11 の録画再生時だけエンコーダーに即時出力系のオプションを付けます。<br>
                </label>
                <v-switch class="settings__item-switch" color="primary" id="encoder_bs4k_low_latency" hide-details
                    v-model="server_settings.general.encoder_bs4k_low_latency">
                </v-switch>
            </div>
            <div class="settings__item settings__item--switch">
                <label class="settings__item-heading" for="bs4k_live_startup_discard_enabled">BS4K ライブ開始時に先頭 TS を捨てる</label>
                <label class="settings__item-label" for="bs4k_live_startup_discard_enabled">
                    有効にすると、BS4K のライブ視聴開始時だけチューナー切替直後の TS をエンコーダーに渡さず破棄します。<br>
                </label>
                <v-switch class="settings__item-switch" color="primary" id="bs4k_live_startup_discard_enabled" hide-details
                    v-model="server_settings.general.bs4k_live_startup_discard_enabled">
                </v-switch>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">BS4K ライブ先頭 TS 破棄秒数</div>
                <div class="settings__item-label">
                    BS4K ライブ開始時に捨てる TS の秒数を設定します。デフォルトは 2.0 です。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" type="number" step="0.1" hide-details
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model.number="server_settings.general.bs4k_live_startup_discard_seconds">
                </v-text-field>
            </div>
        </div>
        <div class="settings__content" v-if="isSectionVisible('quality')">
            <div class="settings__content-heading">
                <Icon icon="fluent:video-clip-multiple-16-filled" width="22px" />
                <span class="ml-2">{{embedded ? 'BS4K画質' : '画質'}}</span>
            </div>
            <v-tabs class="settings__tab" color="primary" bg-color="transparent" align-tabs="center" v-model="player_tab">
                <v-tab style="text-transform: none !important;" v-for="network_circuit in network_circuits" :key="network_circuit">
                    {{network_circuit}}
                </v-tab>
            </v-tabs>
            <div v-show="player_tab === index" v-for="(network_circuit, index) in network_circuits" :key="network_circuit">
                <div class="settings__item settings__item--sync-disabled">
                    <div class="settings__item-heading">BS4K のデフォルトのストリーミング画質</div>
                    <div class="settings__item-label">
                        BS4K ライブ視聴時に最初に適用される、デフォルトの画質を設定します。<br>
                        通常のテレビ画質設定とは別に、BS4K 専用の画質リストから選択します。<br>
                    </div>
                    <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                        :density="is_form_dense ? 'compact' : 'default'" v-if="network_circuit !== 'モバイル回線時'"
                        :items="bs4k_streaming_quality" v-model="settings_store.settings.bs4k_streaming_quality">
                    </v-select>
                    <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                        :density="is_form_dense ? 'compact' : 'default'" v-if="network_circuit === 'モバイル回線時'"
                        :items="bs4k_streaming_quality_cellular" v-model="settings_store.settings.bs4k_streaming_quality_cellular">
                    </v-select>
                </div>
                <div class="settings__item settings__item--sync-disabled">
                    <div class="settings__item-heading">BS4K ライブ視聴の映像コーデック</div>
                    <div class="settings__item-label">
                        AVC は互換性を、HEVC は通信量の削減を優先します。HEVC 非対応環境では再生時だけ AVC に戻します。<br>
                    </div>
                    <div class="settings__item-label mt-1">
                        <p class="mt-1 mb-0 text-error-readable" v-if="PlayerUtils.isHEVCVideoSupported() === false && Utils.isFirefox() === false">
                            このデバイスでは HEVC がサポートされていません。
                        </p>
                        <p class="mt-1 mb-0 text-error-readable" v-if="PlayerUtils.isHEVCVideoSupported() === false && Utils.isFirefox() === true">
                            お使いの Firefox ブラウザでは HEVC がサポートされていません。
                        </p>
                    </div>
                    <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                        :density="is_form_dense ? 'compact' : 'default'" :items="streaming_video_codecs"
                        v-if="network_circuit !== 'モバイル回線時'" v-model="settings_store.settings.bs4k_tv_encoding_codec">
                    </v-select>
                    <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                        :density="is_form_dense ? 'compact' : 'default'" :items="streaming_video_codecs"
                        v-if="network_circuit === 'モバイル回線時'" v-model="settings_store.settings.bs4k_tv_encoding_codec_cellular">
                    </v-select>
                </div>
                <div class="settings__item settings__item--sync-disabled">
                    <div class="settings__item-heading">BS4K 録画再生のデフォルトのストリーミング画質</div>
                    <div class="settings__item-label">
                        ONID=11 の録画再生時に最初に適用される、デフォルトの画質を設定します。<br>
                        通常のビデオ画質設定とは別に、BS4K 専用の画質リストから選択します。<br>
                    </div>
                    <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                        :density="is_form_dense ? 'compact' : 'default'" v-if="network_circuit !== 'モバイル回線時'"
                        :items="bs4k_video_streaming_quality" v-model="settings_store.settings.bs4k_video_streaming_quality">
                    </v-select>
                    <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                        :density="is_form_dense ? 'compact' : 'default'" v-if="network_circuit === 'モバイル回線時'"
                        :items="bs4k_video_streaming_quality_cellular" v-model="settings_store.settings.bs4k_video_streaming_quality_cellular">
                    </v-select>
                </div>
                <div class="settings__item settings__item--sync-disabled">
                    <div class="settings__item-heading">BS4K 録画再生の映像コーデック</div>
                    <div class="settings__item-label">
                        AVC は互換性を優先し、HEVC、VP9、AV1 の順に映像ビットレートを抑えます。<br>
                        非対応環境では、保存設定を変えずに再生時だけ利用可能な別コーデックへ切り替えます。<br>
                    </div>
                    <div class="settings__item-label mt-1">
                        <p class="mt-1 mb-0 text-error-readable" v-if="PlayerUtils.isHEVCVideoSupported() === false && Utils.isFirefox() === false">
                            このデバイスでは HEVC がサポートされていません。
                        </p>
                        <p class="mt-1 mb-0 text-error-readable" v-if="PlayerUtils.isHEVCVideoSupported() === false && Utils.isFirefox() === true">
                            お使いの Firefox ブラウザでは HEVC がサポートされていません。
                        </p>
                    </div>
                    <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                        :density="is_form_dense ? 'compact' : 'default'" :items="recorded_streaming_video_codecs"
                        v-if="network_circuit !== 'モバイル回線時'" v-model="settings_store.settings.bs4k_video_encoding_codec">
                    </v-select>
                    <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                        :density="is_form_dense ? 'compact' : 'default'" :items="recorded_streaming_video_codecs"
                        v-if="network_circuit === 'モバイル回線時'" v-model="settings_store.settings.bs4k_video_encoding_codec_cellular">
                    </v-select>
                </div>
                <div class="settings__item settings__item--sync-disabled">
                    <div class="settings__item-heading">BS4K 録画再生の音声コーデック</div>
                    <div class="settings__item-label">
                        AAC は互換性を、Opus は音質と圧縮効率を優先します。<br>
                    </div>
                    <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                        :density="is_form_dense ? 'compact' : 'default'" :items="recorded_streaming_audio_codecs"
                        v-if="network_circuit !== 'モバイル回線時'" v-model="settings_store.settings.bs4k_video_audio_encoding_codec">
                    </v-select>
                    <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                        :density="is_form_dense ? 'compact' : 'default'" :items="recorded_streaming_audio_codecs"
                        v-if="network_circuit === 'モバイル回線時'" v-model="settings_store.settings.bs4k_video_audio_encoding_codec_cellular">
                    </v-select>
                </div>
            </div>
        </div>
        <div class="settings__content" v-if="isSectionVisible('server')"
            :class="{'settings__content--disabled': is_disabled}">
            <div class="settings__content-heading">
                <Icon icon="fluent:play-circle-20-filled" width="22px" />
                <span class="ml-2">プレイヤー</span>
            </div>
            <div class="settings__item settings__item--switch" :class="{'settings__item--disabled': is_disabled}">
                <label class="settings__item-heading" for="bs4k_ignore_viewer_low_latency">BS4K プレイヤーを通常バッファで再生する</label>
                <label class="settings__item-label" for="bs4k_ignore_viewer_low_latency">
                    有効にすると、BS4K のライブ視聴時だけユーザーの低遅延視聴設定を使わず、通常の再生バッファを使います。<br>
                    変更を反映するには BS4K設定を更新し、KonomiTV-BS4K サーバーを再起動してください。<br>
                </label>
                <v-switch class="settings__item-switch" color="primary" id="bs4k_ignore_viewer_low_latency" hide-details
                    v-model="server_settings.general.bs4k_ignore_viewer_low_latency" :disabled="is_disabled">
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
    </SettingsViewContainer>
</template>
<script lang="ts" setup>

import { storeToRefs } from 'pinia';
import { computed, ref, toRaw, watch } from 'vue';

import type { IServerSettings } from '@/services/Settings';

import SettingsViewContainer from '@/components/Settings/SettingsViewContainer.vue';
import Message from '@/message';
import Videos, { IRecordedPlaybackCodecOption } from '@/services/Videos';
import useServerSettingsStore from '@/stores/ServerSettingsStore';
import useSettingsStore, {
    type BS4KLiveStreamingQuality,
    type RecordedStreamingVideoCodec,
} from '@/stores/SettingsStore';
import useUserStore from '@/stores/UserStore';
import Utils, { PlayerUtils } from '@/utils';

type BS4KSettingsSection = 'quality' | 'server' | 'all';

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
    quality: 'BS4K再生画質',
    server: 'BS4K配信・エンコーダー',
    all: 'BS4K設定',
})[props.section]);
const section_description = computed(() => {
    if (props.section === 'quality') {
        return 'この端末での BS4K ライブ視聴と、ONID=11 の録画再生に使う画質・映像コーデックを設定します。';
    }
    return 'BS4K のライブ視聴と、ONID=11 の録画再生に関するサーバー設定です。';
});

function isSectionVisible(target_section: Exclude<BS4KSettingsSection, 'all'>): boolean {
    return props.section === 'all' || props.section === target_section;
}

const QUALITY_BS4K_H264 = [
    {title: '8K (約18.00GB/h / 平均40.0Mbps)', value: '4320p'},
    {title: '4K (約8.10GB/h / 平均18.0Mbps)', value: '2160p'},
    {title: '1440p (約5.85GB/h / 平均13.0Mbps)', value: '1440p'},
    {title: '1080p (60fps) (約4.95GB/h / 平均11.0Mbps)', value: '1080p-60fps'},
    {title: '1080p (30fps) (約4.28GB/h / 平均9.5Mbps)', value: '1080p-30fps'},
    {title: '810p (60fps) (約2.93GB/h / 平均6.5Mbps)', value: '810p-60fps'},
    {title: '810p (30fps) (約2.48GB/h / 平均5.5Mbps)', value: '810p-30fps'},
    {title: '720p (60fps) (約2.43GB/h / 平均5.4Mbps)', value: '720p-60fps'},
    {title: '720p (30fps) (約2.03GB/h / 平均4.5Mbps)', value: '720p-30fps'},
    {title: '540p (30fps) (約1.35GB/h / 平均3.0Mbps)', value: '540p-30fps'},
    {title: '480p (30fps) (約0.90GB/h / 平均2.0Mbps)', value: '480p-30fps'},
    {title: '360p (30fps) (約0.50GB/h / 平均1.1Mbps)', value: '360p-30fps'},
    {title: '240p (30fps) (約0.25GB/h / 平均0.6Mbps)', value: '240p-30fps'},
];

const QUALITY_BS4K_H265 = [
    {title: '8K (約9.00GB/h / 平均20.0Mbps)', value: '4320p'},
    {title: '4K (約4.05GB/h / 平均9.0Mbps)', value: '2160p'},
    {title: '1440p (約2.48GB/h / 平均5.5Mbps)', value: '1440p'},
    {title: '1080p (60fps) (約1.58GB/h / 平均3.5Mbps)', value: '1080p-60fps'},
    {title: '1080p (30fps) (約1.35GB/h / 平均3.0Mbps)', value: '1080p-30fps'},
    {title: '810p (60fps) (約1.35GB/h / 平均3.0Mbps)', value: '810p-60fps'},
    {title: '810p (30fps) (約1.13GB/h / 平均2.5Mbps)', value: '810p-30fps'},
    {title: '720p (60fps) (約1.08GB/h / 平均2.4Mbps)', value: '720p-60fps'},
    {title: '720p (30fps) (約0.90GB/h / 平均2.0Mbps)', value: '720p-30fps'},
    {title: '540p (30fps) (約0.63GB/h / 平均1.4Mbps)', value: '540p-30fps'},
    {title: '480p (30fps) (約0.47GB/h / 平均1.1Mbps)', value: '480p-30fps'},
    {title: '360p (30fps) (約0.34GB/h / 平均0.8Mbps)', value: '360p-30fps'},
    {title: '240p (30fps) (約0.20GB/h / 平均0.5Mbps)', value: '240p-30fps'},
];

// 録画再生では既存の HEVC 指定値を基準に、VP9 は 90%、AV1 は 70% へ下げる。
// 表示は小数第1位へ丸めるが、実際の FFmpeg 指定値はサーバー側で Kbps 単位に計算される。
const QUALITY_BS4K_VP9 = [
    {title: '8K (約8.10GB/h / 平均18.0Mbps)', value: '4320p'},
    {title: '4K (約3.65GB/h / 平均8.1Mbps)', value: '2160p'},
    {title: '1440p (約2.23GB/h / 平均5.0Mbps)', value: '1440p'},
    {title: '1080p (60fps) (約1.42GB/h / 平均3.2Mbps)', value: '1080p-60fps'},
    {title: '1080p (30fps) (約1.22GB/h / 平均2.7Mbps)', value: '1080p-30fps'},
    {title: '810p (60fps) (約1.22GB/h / 平均2.7Mbps)', value: '810p-60fps'},
    {title: '810p (30fps) (約1.01GB/h / 平均2.3Mbps)', value: '810p-30fps'},
    {title: '720p (60fps) (約0.97GB/h / 平均2.2Mbps)', value: '720p-60fps'},
    {title: '720p (30fps) (約0.81GB/h / 平均1.8Mbps)', value: '720p-30fps'},
    {title: '540p (30fps) (約0.57GB/h / 平均1.3Mbps)', value: '540p-30fps'},
    {title: '480p (30fps) (約0.43GB/h / 平均0.9Mbps)', value: '480p-30fps'},
    {title: '360p (30fps) (約0.30GB/h / 平均0.7Mbps)', value: '360p-30fps'},
    {title: '240p (30fps) (約0.18GB/h / 平均0.4Mbps)', value: '240p-30fps'},
];

const QUALITY_BS4K_AV1 = [
    {title: '8K (約6.30GB/h / 平均14.0Mbps)', value: '4320p'},
    {title: '4K (約2.84GB/h / 平均6.3Mbps)', value: '2160p'},
    {title: '1440p (約1.73GB/h / 平均3.9Mbps)', value: '1440p'},
    {title: '1080p (60fps) (約1.10GB/h / 平均2.5Mbps)', value: '1080p-60fps'},
    {title: '1080p (30fps) (約0.95GB/h / 平均2.1Mbps)', value: '1080p-30fps'},
    {title: '810p (60fps) (約0.95GB/h / 平均2.1Mbps)', value: '810p-60fps'},
    {title: '810p (30fps) (約0.79GB/h / 平均1.8Mbps)', value: '810p-30fps'},
    {title: '720p (60fps) (約0.76GB/h / 平均1.7Mbps)', value: '720p-60fps'},
    {title: '720p (30fps) (約0.63GB/h / 平均1.4Mbps)', value: '720p-30fps'},
    {title: '540p (30fps) (約0.44GB/h / 平均1.0Mbps)', value: '540p-30fps'},
    {title: '480p (30fps) (約0.33GB/h / 平均0.7Mbps)', value: '480p-30fps'},
    {title: '360p (30fps) (約0.24GB/h / 平均0.5Mbps)', value: '360p-30fps'},
    {title: '240p (30fps) (約0.14GB/h / 平均0.3Mbps)', value: '240p-30fps'},
];

const RECORDED_BS4K_QUALITY_BY_CODEC: Record<RecordedStreamingVideoCodec, typeof QUALITY_BS4K_H264> = {
    avc: QUALITY_BS4K_H264,
    hevc: QUALITY_BS4K_H265,
    vp9: QUALITY_BS4K_VP9,
    av1: QUALITY_BS4K_AV1,
};

// フォームを小さくするかどうか
const is_form_dense = Utils.isSmartphoneHorizontal();
const settings_store = useSettingsStore();
const legacy_bs4k_quality_map: Record<string, BS4KLiveStreamingQuality> = {
    '1080p': '1080p-30fps',
    '810p': '810p-30fps',
    '720p': '720p-30fps',
    '540p': '540p-30fps',
    '480p': '480p-30fps',
    '360p': '360p-30fps',
    '240p': '240p-30fps',
};
settings_store.settings.bs4k_streaming_quality =
    legacy_bs4k_quality_map[settings_store.settings.bs4k_streaming_quality] ?? settings_store.settings.bs4k_streaming_quality;
settings_store.settings.bs4k_streaming_quality_cellular =
    legacy_bs4k_quality_map[settings_store.settings.bs4k_streaming_quality_cellular] ?? settings_store.settings.bs4k_streaming_quality_cellular;
settings_store.settings.bs4k_video_streaming_quality =
    legacy_bs4k_quality_map[settings_store.settings.bs4k_video_streaming_quality] ?? settings_store.settings.bs4k_video_streaming_quality;
settings_store.settings.bs4k_video_streaming_quality_cellular =
    legacy_bs4k_quality_map[settings_store.settings.bs4k_video_streaming_quality_cellular] ?? settings_store.settings.bs4k_video_streaming_quality_cellular;
const player_tab = ref<number | null>(0);
const network_circuits = ['Wi-Fi 回線時', 'モバイル回線時'];
const streaming_video_codecs = [
    {title: 'H.264 / AVC（互換性優先）', value: 'avc'},
    {title: 'H.265 / HEVC（通信量優先）', value: 'hevc'},
];
const recorded_streaming_video_codecs = ref<IRecordedPlaybackCodecOption[]>([]);
const recorded_streaming_audio_codecs = Videos.buildRecordedPlaybackAudioCodecOptions();
const bs4k_streaming_quality = computed(() => {
    return settings_store.settings.bs4k_tv_encoding_codec === 'hevc' ? QUALITY_BS4K_H265 : QUALITY_BS4K_H264;
});
const bs4k_streaming_quality_cellular = computed(() => {
    return settings_store.settings.bs4k_tv_encoding_codec_cellular === 'hevc' ? QUALITY_BS4K_H265 : QUALITY_BS4K_H264;
});
const bs4k_video_streaming_quality = computed(() => {
    return RECORDED_BS4K_QUALITY_BY_CODEC[settings_store.settings.bs4k_video_encoding_codec];
});
const bs4k_video_streaming_quality_cellular = computed(() => {
    return RECORDED_BS4K_QUALITY_BY_CODEC[settings_store.settings.bs4k_video_encoding_codec_cellular];
});

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
// 録画再生のコーデック候補は、ローカルドラフトにある BS4K エンコーダーから生成する
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

// エンコーダーが連続して切り替わったとき、遅く返った古い応答で候補を上書きしない
let recorded_codec_options_request_id = 0;
watch([
    () => server_settings.value.general.encoder_bs4k,
    () => props.section,
], async ([encoder, current_section]) => {
    const request_id = ++recorded_codec_options_request_id;
    if (current_section === 'server') {
        recorded_streaming_video_codecs.value = [];
        return;
    }
    const options = await Videos.buildRecordedPlaybackCodecOptions(encoder);
    if (request_id === recorded_codec_options_request_id) {
        recorded_streaming_video_codecs.value = options;
    }
}, {
    immediate: true,
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
