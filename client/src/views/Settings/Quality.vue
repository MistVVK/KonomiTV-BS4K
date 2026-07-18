<template>
    <!-- ベース画面の中にそれぞれの設定画面で異なる部分を記述する -->
    <SettingsViewContainer :embedded="embedded">
        <h2 class="settings__heading" v-if="embedded === false">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:video-clip-multiple-16-filled" width="26px" />
            <span class="ml-3">画質</span>
        </h2>
        <div class="settings__content-heading" v-if="embedded">
            <Icon icon="fluent:video-clip-multiple-16-filled" width="22px" />
            <span class="ml-2">通常画質</span>
        </div>
        <div class="settings__quote mt-5 pb-2">
            視聴開始時の画質プロファイルは、デバイスの回線状況に応じて自動的に選択されます (Android のみ) 。<br>
            画質プロファイルは、プレイヤー下にある設定アイコン ⚙️ から変更できます。<br>
        </div>
        <v-tabs class="settings__tab" color="primary" bg-color="transparent" align-tabs="center" v-model="tab">
            <v-tab style="text-transform: none !important;" v-for="network_circuit in network_circuits" :key="network_circuit">
                {{network_circuit}}
            </v-tab>
        </v-tabs>
        <div v-show="tab === index" class="settings__content mt-0" v-for="(network_circuit, index) in network_circuits" :key="network_circuit">
            <div class="settings__content-heading mt-6">
                <Icon icon="fluent:tv-20-filled" width="22px" />
                <span class="ml-2">テレビのライブストリーミング</span>
            </div>
            <div class="settings__item settings__item--sync-disabled">
                <div class="settings__item-heading">テレビのデフォルトのストリーミング画質</div>
                <div class="settings__item-label">
                    ライブ視聴時に最初に適用される、デフォルトの画質を設定します。<br>
                    視聴中はプレイヤーの設定からいつでも変更できますが、次回視聴時はここで設定した画質に戻ります。<br>
                </div>
                <div class="settings__item-label mt-1">
                    画質を [1080p (60fps)] に設定すると、<b>通常 30fps (60i) の映像を補間し、より滑らか（ぬるぬる）な映像で視聴できます！</b>ドラマやバラエティなどを視聴するときに特におすすめです。<br>
                </div>
                <div class="settings__item-label mt-1" v-if="Utils.isAndroid()">
                    Fire HD 10 (2021) などの一部のローエンド Android (特に MediaTek SoC 搭載) デバイスでは、1080p 以上の映像描画が不安定なことが確認されています。その場合は 720p 以下の画質を選択することをおすすめします。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'" v-if="network_circuit !== 'モバイル回線時'"
                    :items="tv_streaming_quality" v-model="settingsStore.settings.tv_streaming_quality">
                </v-select>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'" v-if="network_circuit === 'モバイル回線時'"
                    :items="tv_streaming_quality_cellular" v-model="settingsStore.settings.tv_streaming_quality_cellular">
                </v-select>
            </div>
            <div class="settings__item settings__item--sync-disabled">
                <div class="settings__item-heading">ライブ視聴の映像コーデック</div>
                <div class="settings__item-label">
                    AVC は互換性を、HEVC は通信量の削減を優先します。HEVC 非対応環境では再生時だけ AVC に戻します。<br>
                </div>
                <div class="settings__item-label mt-1">
                    <p class="mt-1 mb-0 text-error-lighten-1" v-if="PlayerUtils.isHEVCVideoSupported() === false && Utils.isFirefox() === false">
                        このデバイスでは HEVC がサポートされていません。
                    </p>
                    <p class="mt-1 mb-0 text-error-lighten-1" v-if="PlayerUtils.isHEVCVideoSupported() === false && Utils.isFirefox() === true">
                        お使いの Firefox ブラウザでは HEVC がサポートされていません。
                    </p>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'" :items="streaming_video_codecs"
                    v-if="network_circuit !== 'モバイル回線時'" v-model="settingsStore.settings.tv_encoding_codec">
                </v-select>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'" :items="streaming_video_codecs"
                    v-if="network_circuit === 'モバイル回線時'" v-model="settingsStore.settings.tv_encoding_codec_cellular">
                </v-select>
            </div>
            <div class="settings__item settings__item--switch settings__item--sync-disabled">
                <label class="settings__item-heading" :for="`tv_low_latency_mode${network_circuit === 'モバイル回線時' ? '_cellular' : ''}`">
                    テレビを低遅延で視聴する
                </label>
                <label class="settings__item-label" :for="`tv_low_latency_mode${network_circuit === 'モバイル回線時' ? '_cellular' : ''}`">
                    低遅延ストリーミングをオンにすると、<b>放送波との遅延を最短 0.9 秒に抑えて視聴できます！</b><br>
                    また、約 3 秒以上遅延したときに少しだけ再生速度を早める (1.1x) ことで、滑らかにストリーミングの遅延を取り戻します。<br>
                </label>
                <div class="settings__item-label mt-1">
                    映像がカクつきやすくなるため、<b>通信が不安定になりがちなモバイル回線やフリー Wi-Fi から視聴するときは、オフにすることをおすすめします。</b><br>
                </div>
                <v-switch class="settings__item-switch" color="primary" id="tv_low_latency_mode" hide-details v-if="network_circuit !== 'モバイル回線時'"
                    v-model="settingsStore.settings.tv_low_latency_mode">
                </v-switch>
                <v-switch class="settings__item-switch" color="primary" id="tv_low_latency_mode_cellular" hide-details v-if="network_circuit === 'モバイル回線時'"
                    v-model="settingsStore.settings.tv_low_latency_mode_cellular">
                </v-switch>
            </div>
            <div class="settings__item settings__item--switch settings__item--sync-disabled">
                <label class="settings__item-heading" :for="`tv_24fps_mode${network_circuit === 'モバイル回線時' ? '_cellular' : ''}`">
                    テレビを 24fps モードで視聴する
                </label>
                <label class="settings__item-label" :for="`tv_24fps_mode${network_circuit === 'モバイル回線時' ? '_cellular' : ''}`">
                    映画やアニメなど 24fps で制作された映像を検出し、本来の動きに近づけます。<br>
                    画質で [1080p (60fps)] を選択している場合は、常に 60fps が優先されます。<br>
                </label>
                <div class="settings__item-label mt-1">
                    CM やニュースなど 30fps の区間は基本的にそのまま再生されます。テロップなど一部の映像では効果が安定しないことがあります。サーバーのエンコード設定によっては利用できません。<br>
                </div>
                <v-switch class="settings__item-switch" color="primary" id="tv_24fps_mode" hide-details v-if="network_circuit !== 'モバイル回線時'"
                    v-model="settingsStore.settings.tv_24fps_mode">
                </v-switch>
                <v-switch class="settings__item-switch" color="primary" id="tv_24fps_mode_cellular" hide-details v-if="network_circuit === 'モバイル回線時'"
                    v-model="settingsStore.settings.tv_24fps_mode_cellular">
                </v-switch>
            </div>
            <div class="settings__content-heading mt-6">
                <Icon icon="fluent:movies-and-tv-20-filled" width="22px" />
                <span class="ml-2">ビデオのオンデマンドストリーミング</span>
            </div>
            <div class="settings__item settings__item--sync-disabled">
                <div class="settings__item-heading">ビデオのデフォルトのストリーミング画質</div>
                <div class="settings__item-label">
                    録画再生時に最初に適用される、デフォルトの画質を設定します。<br>
                    再生中はプレイヤーの設定からいつでも変更できますが、次回再生時はここで設定した画質に戻ります。<br>
                </div>
                <div class="settings__item-label mt-1">
                    画質を [1080p (60fps)] に設定すると、<b>通常 30fps (60i) の映像を補間し、より滑らか（ぬるぬる）な映像で再生できます！</b>ドラマやバラエティなどを再生するときに特におすすめです。<br>
                </div>
                <div class="settings__item-label mt-1" v-if="Utils.isAndroid()">
                    Fire HD 10 (2021) などの一部のローエンド Android (特に MediaTek SoC 搭載) デバイスでは、1080p 以上の映像描画が不安定なことが確認されています。その場合は 720p 以下の画質を選択することをおすすめします。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'" v-if="network_circuit !== 'モバイル回線時'"
                    :items="video_streaming_quality" v-model="settingsStore.settings.video_streaming_quality">
                </v-select>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'" v-if="network_circuit === 'モバイル回線時'"
                    :items="video_streaming_quality_cellular" v-model="settingsStore.settings.video_streaming_quality_cellular">
                </v-select>
            </div>
            <div class="settings__item settings__item--sync-disabled">
                <div class="settings__item-heading">録画再生の映像コーデック</div>
                <div class="settings__item-label">
                    AVC は互換性を、HEVC は通信量の削減を優先します。非対応環境では再生時だけ AVC に戻します。
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'" :items="recorded_streaming_video_codecs"
                    v-if="network_circuit !== 'モバイル回線時'" v-model="settingsStore.settings.video_encoding_codec">
                </v-select>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'" :items="recorded_streaming_video_codecs"
                    v-if="network_circuit === 'モバイル回線時'" v-model="settingsStore.settings.video_encoding_codec_cellular">
                </v-select>
            </div>
            <div class="settings__item settings__item--switch settings__item--sync-disabled">
                <label class="settings__item-heading" :for="`video_24fps_mode${network_circuit === 'モバイル回線時' ? '_cellular' : ''}`">
                    ビデオを 24fps モードで再生する
                </label>
                <label class="settings__item-label" :for="`video_24fps_mode${network_circuit === 'モバイル回線時' ? '_cellular' : ''}`">
                    映画やアニメなど 24fps で制作された映像を検出し、本来の動きに近づけます。<br>
                    画質で [1080p (60fps)] を選択している場合は、常に 60fps が優先されます。<br>
                </label>
                <div class="settings__item-label mt-1">
                    CM やニュースなど 30fps の区間は基本的にそのまま再生されます。テロップなど一部の映像では効果が安定しないことがあります。サーバーのエンコード設定によっては利用できません。<br>
                </div>
                <v-switch class="settings__item-switch" color="primary" id="video_24fps_mode" hide-details v-if="network_circuit !== 'モバイル回線時'"
                    v-model="settingsStore.settings.video_24fps_mode">
                </v-switch>
                <v-switch class="settings__item-switch" color="primary" id="video_24fps_mode_cellular" hide-details v-if="network_circuit === 'モバイル回線時'"
                    v-model="settingsStore.settings.video_24fps_mode_cellular">
                </v-switch>
            </div>
        </div>
    </SettingsViewContainer>
</template>
<script lang="ts">

import { mapStores } from 'pinia';
import { defineComponent } from 'vue';

import SettingsViewContainer from '@/components/Settings/SettingsViewContainer.vue';
import Videos, { IRecordedPlaybackCodecOption } from '@/services/Videos';
import useServerSettingsStore from '@/stores/ServerSettingsStore';
import useSettingsStore from '@/stores/SettingsStore';
import Utils, { PlayerUtils } from '@/utils';

const QUALITY_H264 = [
    {title: '1080p (60fps) (約4.95GB/h / 平均11.0Mbps)', value: '1080p-60fps'},
    {title: '1080p (約4.50GB/h / 平均10.0Mbps)', value: '1080p'},
    {title: '810p (約2.62GB/h / 平均5.8Mbps)', value: '810p'},
    {title: '720p (約2.18GB/h / 平均4.9Mbps)', value: '720p'},
    {title: '540p (約1.52GB/h / 平均3.4Mbps)', value: '540p'},
    {title: '480p (約1.06GB/h / 平均2.3Mbps)', value: '480p'},
    {title: '360p (約0.60GB/h / 平均1.3Mbps)', value: '360p'},
    {title: '240p (約0.35GB/h / 平均0.8Mbps)', value: '240p'},
];

const QUALITY_H265 = [
    {title: '1080p (60fps) (約1.58GB/h / 平均3.5Mbps)', value: '1080p-60fps'},
    {title: '1080p (約1.37GB/h / 平均3.0Mbps)', value: '1080p'},
    {title: '810p (約1.05GB/h / 平均2.3Mbps)', value: '810p'},
    {title: '720p (約0.82GB/h / 平均1.8Mbps)', value: '720p'},
    {title: '540p (約0.53GB/h / 平均1.2Mbps)', value: '540p'},
    {title: '480p (約0.46GB/h / 平均1.0Mbps)', value: '480p'},
    {title: '360p (約0.30GB/h / 平均0.7Mbps)', value: '360p'},
    {title: '240p (約0.20GB/h / 平均0.4Mbps)', value: '240p'},
];

export default defineComponent({
    name: 'Settings-Quality',
    components: {
        SettingsViewContainer,
    },
    props: {
        embedded: {
            type: Boolean,
            default: false,
        },
    },
    data() {
        return {

            // ユーティリティをテンプレートで使えるように
            Utils: Object.freeze(Utils),
            PlayerUtils: Object.freeze(PlayerUtils),

            // フォームを小さくするかどうか
            is_form_dense: Utils.isSmartphoneHorizontal(),

            // タブの状態管理
            tab: null as number | null,

            // ネットワーク回線の種類
            network_circuits: ['Wi-Fi 回線時', 'モバイル回線時'],

            // テレビのデフォルトのストリーミング画質の選択肢
            tv_streaming_quality: QUALITY_H264,
            tv_streaming_quality_cellular: QUALITY_H264,

            // ビデオのデフォルトのストリーミング画質の選択肢
            video_streaming_quality: QUALITY_H264,
            video_streaming_quality_cellular: QUALITY_H264,

            streaming_video_codecs: [
                {title: 'H.264 / AVC（互換性優先）', value: 'avc'},
                {title: 'H.265 / HEVC（通信量優先）', value: 'hevc'},
            ],
            recorded_streaming_video_codecs: [] as IRecordedPlaybackCodecOption[],
        };
    },
    computed: {
        ...mapStores(useSettingsStore, useServerSettingsStore),
    },
    async mounted() {
        // サーバー設定の取得前の初期値で録画コーデック候補を固定しない
        const server_settings = await this.serverSettingsStore.fetchServerSettingsOnce();
        this.recorded_streaming_video_codecs = await Videos.buildRecordedPlaybackCodecOptions(
            server_settings?.general.encoder ?? this.serverSettingsStore.server_settings.general.encoder,
        );
    },
    watch: {
        'settingsStore.settings.tv_encoding_codec': {
            immediate: true,
            handler(value: 'avc' | 'hevc') {
                if (value === 'hevc') {
                    this.tv_streaming_quality = QUALITY_H265;
                } else {
                    this.tv_streaming_quality = QUALITY_H264;
                }
            },
        },
        'settingsStore.settings.tv_encoding_codec_cellular': {
            immediate: true,
            handler(value: 'avc' | 'hevc') {
                if (value === 'hevc') {
                    this.tv_streaming_quality_cellular = QUALITY_H265;
                } else {
                    this.tv_streaming_quality_cellular = QUALITY_H264;
                }
            },
        },
        'settingsStore.settings.video_encoding_codec': {
            immediate: true,
            handler(value: 'avc' | 'hevc') {
                if (value === 'hevc') {
                    this.video_streaming_quality = QUALITY_H265;
                } else {
                    this.video_streaming_quality = QUALITY_H264;
                }
            },
        },
        'settingsStore.settings.video_encoding_codec_cellular': {
            immediate: true,
            handler(value: 'avc' | 'hevc') {
                if (value === 'hevc') {
                    this.video_streaming_quality_cellular = QUALITY_H265;
                } else {
                    this.video_streaming_quality_cellular = QUALITY_H264;
                }
            },
        },
    },
});

</script>
<style lang="scss" scoped>

.settings__tab {
    position: sticky;
    top: 65px;
    z-index: 4;
    background-color: rgb(var(--v-theme-background-lighten-1)) !important;
    @include smartphone-horizontal {
        top: 0px;
    }
    @include smartphone-vertical {
        top: 60px;
        background-color: rgb(var(--v-theme-background)) !important;
    }

    .v-tab {
        letter-spacing: 0.0892857143em !important;
    }
}

</style>
