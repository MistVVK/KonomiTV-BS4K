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
            自動画質選択モードがオンのときは、効率の良いコーデックと上限内の画質・ネットワーク状況に応じた低遅延を自動で選びます。<br>
            視聴中はプレイヤーから一時的に変更できます（設定には保存されません）。
        </div>
        <v-tabs class="settings__tab" color="primary" bg-color="transparent" align-tabs="center" v-model="tab">
            <v-tab style="text-transform: none !important;" v-for="network_circuit in network_circuits" :key="network_circuit">
                {{network_circuit}}
            </v-tab>
        </v-tabs>
        <div v-show="tab === index" class="settings__content mt-0" v-for="(network_circuit, index) in network_circuits" :key="network_circuit">
            <div class="settings__content-heading mt-6">
                <Icon icon="fluent:play-circle-20-filled" width="22px" />
                <span class="ml-2">共通再生プロファイル</span>
            </div>
            <div class="settings__item settings__item--switch settings__item--sync-disabled">
                <label class="settings__item-heading" :for="`konomitv_bs4k_playback_auto_quality_mode${network_circuit === 'モバイル回線時' ? '_cellular' : ''}`">
                    自動画質選択モード
                </label>
                <label class="settings__item-label" :for="`konomitv_bs4k_playback_auto_quality_mode${network_circuit === 'モバイル回線時' ? '_cellular' : ''}`">
                    端末とサーバの対応状況・番組の元画質・ネットワーク品質に合わせて、画質・映像/音声コーデック・低遅延を自動で選びます。<br>
                    映像は AV1 → VP9 → HEVC → AVC、音声は Opus → AAC の順で効率を優先します。24fps モードは使いません。<br>
                </label>
                <v-switch class="settings__item-switch" color="primary" id="konomitv_bs4k_playback_auto_quality_mode" hide-details
                    v-if="network_circuit !== 'モバイル回線時'"
                    v-model="settingsStore.settings.konomitv_bs4k_playback_auto_quality_mode">
                </v-switch>
                <v-switch class="settings__item-switch" color="primary" id="konomitv_bs4k_playback_auto_quality_mode_cellular" hide-details
                    v-if="network_circuit === 'モバイル回線時'"
                    v-model="settingsStore.settings.konomitv_bs4k_playback_auto_quality_mode_cellular">
                </v-switch>
            </div>
            <div class="settings__item settings__item--sync-disabled"
                v-if="(network_circuit !== 'モバイル回線時' && settingsStore.settings.konomitv_bs4k_playback_auto_quality_mode) ||
                    (network_circuit === 'モバイル回線時' && settingsStore.settings.konomitv_bs4k_playback_auto_quality_mode_cellular)">
                <div class="settings__item-heading">自動モード時の上限画質</div>
                <div class="settings__item-label">
                    これより高い画質には上げません。番組の元画質より高い解像度にもしません。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'" v-if="network_circuit !== 'モバイル回線時'"
                    :items="konomitv_bs4k_auto_quality_max_options"
                    v-model="settingsStore.settings.konomitv_bs4k_playback_auto_quality_max">
                </v-select>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'" v-if="network_circuit === 'モバイル回線時'"
                    :items="konomitv_bs4k_auto_quality_max_cellular_options"
                    v-model="settingsStore.settings.konomitv_bs4k_playback_auto_quality_max_cellular">
                </v-select>
            </div>
            <div class="settings__item settings__item--sync-disabled">
                <div class="settings__item-heading">デフォルトのストリーミング画質</div>
                <div class="settings__item-label">
                    ライブ視聴と録画再生の両方で最初に適用する画質です。<br>
                    視聴中はプレイヤーの設定から変更できますが、次回はこの共通値に戻ります。<br>
                    <template v-if="isAutoQualityModeForCircuit(network_circuit)">
                        <b>自動モード中のため編集できません。</b>上限画質は上の項目で設定できます。<br>
                    </template>
                </div>
                <div class="settings__item-label mt-1" v-if="!isAutoQualityModeForCircuit(network_circuit)">
                    画質を [1080p (60fps)] に設定すると、<b>通常 30fps (60i) の映像を補間し、より滑らか（ぬるぬる）な映像で視聴できます！</b>ドラマやバラエティなどを視聴するときに特におすすめです。<br>
                </div>
                <div class="settings__item-label mt-1" v-if="Utils.isAndroid() && !isAutoQualityModeForCircuit(network_circuit)">
                    Fire HD 10 (2021) などの一部のローエンド Android (特に MediaTek SoC 搭載) デバイスでは、1080p 以上の映像描画が不安定なことが確認されています。その場合は 720p 以下の画質を選択することをおすすめします。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'" v-if="network_circuit !== 'モバイル回線時'"
                    :disabled="settingsStore.settings.konomitv_bs4k_playback_auto_quality_mode"
                    :items="konomitv_bs4k_playback_streaming_quality_options" v-model="settingsStore.settings.konomitv_bs4k_playback_streaming_quality">
                </v-select>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'" v-if="network_circuit === 'モバイル回線時'"
                    :disabled="settingsStore.settings.konomitv_bs4k_playback_auto_quality_mode_cellular"
                    :items="konomitv_bs4k_playback_streaming_quality_cellular_options" v-model="settingsStore.settings.konomitv_bs4k_playback_streaming_quality_cellular">
                </v-select>
            </div>
            <div class="settings__item settings__item--sync-disabled">
                <div class="settings__item-heading">映像コーデック</div>
                <div class="settings__item-label">
                    サーバーのライブ・録画能力と、このブラウザの MSE 対応の積集合だけを選択できます。<br>
                    VP9 は KonomiTV-BS4K WebUI 専用 TS の実験的機能です。
                    <template v-if="isAutoQualityModeForCircuit(network_circuit)">
                        <br><b>自動モード中のため編集できません。</b>
                    </template>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'" :items="konomitv_bs4k_playback_video_codecs"
                    v-if="network_circuit !== 'モバイル回線時'"
                    :disabled="settingsStore.settings.konomitv_bs4k_playback_auto_quality_mode"
                    :model-value="settingsStore.settings.konomitv_bs4k_playback_video_codec"
                    @update:model-value="updateKonomiTVBS4KPlaybackVideoCodec($event, false)">
                </v-select>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'" :items="konomitv_bs4k_playback_video_codecs_cellular"
                    v-if="network_circuit === 'モバイル回線時'"
                    :disabled="settingsStore.settings.konomitv_bs4k_playback_auto_quality_mode_cellular"
                    :model-value="settingsStore.settings.konomitv_bs4k_playback_video_codec_cellular"
                    @update:model-value="updateKonomiTVBS4KPlaybackVideoCodec($event, true)">
                </v-select>
            </div>
            <div class="settings__item settings__item--sync-disabled">
                <div class="settings__item-heading">音声コーデック</div>
                <div class="settings__item-label">
                    AAC は互換性を、Opus は音質と圧縮効率を優先します。ライブと録画で同じ設定を使います。<br>
                    <template v-if="isAutoQualityModeForCircuit(network_circuit)">
                        <b>自動モード中のため編集できません。</b>
                    </template>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'" :items="konomitv_bs4k_playback_audio_codecs"
                    v-if="network_circuit !== 'モバイル回線時'"
                    :disabled="settingsStore.settings.konomitv_bs4k_playback_auto_quality_mode"
                    :model-value="settingsStore.settings.konomitv_bs4k_playback_audio_codec"
                    @update:model-value="updateKonomiTVBS4KPlaybackAudioCodec($event, false)">
                </v-select>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'" :items="konomitv_bs4k_playback_audio_codecs_cellular"
                    v-if="network_circuit === 'モバイル回線時'"
                    :disabled="settingsStore.settings.konomitv_bs4k_playback_auto_quality_mode_cellular"
                    :model-value="settingsStore.settings.konomitv_bs4k_playback_audio_codec_cellular"
                    @update:model-value="updateKonomiTVBS4KPlaybackAudioCodec($event, true)">
                </v-select>
            </div>
            <div class="settings__item settings__item--switch settings__item--sync-disabled"
                v-if="!isAutoQualityModeForCircuit(network_circuit)">
                <label class="settings__item-heading" :for="`konomitv_bs4k_playback_24fps_mode${network_circuit === 'モバイル回線時' ? '_cellular' : ''}`">
                    24fps モードで再生する
                </label>
                <label class="settings__item-label" :for="`konomitv_bs4k_playback_24fps_mode${network_circuit === 'モバイル回線時' ? '_cellular' : ''}`">
                    映画やアニメなど 24fps で制作された映像を検出し、本来の動きに近づけます。<br>
                    画質で [1080p (60fps)] を選択している場合は、常に 60fps が優先されます。<br>
                </label>
                <div class="settings__item-label mt-1">
                    CM やニュースなど 30fps の区間は基本的にそのまま再生されます。テロップなど一部の映像では効果が安定しないことがあります。サーバーのエンコード設定によっては利用できません。<br>
                </div>
                <v-switch class="settings__item-switch" color="primary" id="konomitv_bs4k_playback_24fps_mode" hide-details v-if="network_circuit !== 'モバイル回線時'"
                    v-model="settingsStore.settings.konomitv_bs4k_playback_24fps_mode">
                </v-switch>
                <v-switch class="settings__item-switch" color="primary" id="konomitv_bs4k_playback_24fps_mode_cellular" hide-details v-if="network_circuit === 'モバイル回線時'"
                    v-model="settingsStore.settings.konomitv_bs4k_playback_24fps_mode_cellular">
                </v-switch>
            </div>
            <div class="settings__item settings__item--switch settings__item--sync-disabled">
                <label class="settings__item-heading" :for="`tv_low_latency_mode${network_circuit === 'モバイル回線時' ? '_cellular' : ''}`">
                    テレビを低遅延で視聴する
                </label>
                <label class="settings__item-label" :for="`tv_low_latency_mode${network_circuit === 'モバイル回線時' ? '_cellular' : ''}`">
                    <template v-if="isAutoQualityModeForCircuit(network_circuit)">
                        自動モード中は、ネットワーク品質と速度だけを見て低遅延の可否を決めます（このスイッチは使いません）。<br>
                        視聴中はプレイヤーから一時的に変更できます（設定には保存されません）。<br>
                    </template>
                    <template v-else>
                        低遅延ストリーミングをオンにすると、<b>放送波との遅延を最短 0.9 秒に抑えて視聴できます！</b><br>
                        また、約 3 秒以上遅延したときに少しだけ再生速度を早める (1.1x) ことで、滑らかにストリーミングの遅延を取り戻します。<br>
                    </template>
                </label>
                <div class="settings__item-label mt-1" v-if="!isAutoQualityModeForCircuit(network_circuit)">
                    映像がカクつきやすくなるため、<b>通信が不安定になりがちなモバイル回線やフリー Wi-Fi から視聴するときは、オフにすることをおすすめします。</b><br>
                </div>
                <v-switch class="settings__item-switch" color="primary" id="tv_low_latency_mode" hide-details v-if="network_circuit !== 'モバイル回線時'"
                    :disabled="settingsStore.settings.konomitv_bs4k_playback_auto_quality_mode"
                    v-model="settingsStore.settings.tv_low_latency_mode">
                </v-switch>
                <v-switch class="settings__item-switch" color="primary" id="tv_low_latency_mode_cellular" hide-details v-if="network_circuit === 'モバイル回線時'"
                    :disabled="settingsStore.settings.konomitv_bs4k_playback_auto_quality_mode_cellular"
                    v-model="settingsStore.settings.tv_low_latency_mode_cellular">
                </v-switch>
            </div>
        </div>
    </SettingsViewContainer>
</template>
<script lang="ts">

import { mapStores } from 'pinia';
import { defineComponent } from 'vue';

import SettingsViewContainer from '@/components/Settings/SettingsViewContainer.vue';
import Message from '@/message';
import Videos, {
    IKonomiTVBS4KPlaybackAudioCodecOption,
    IKonomiTVBS4KPlaybackCapabilities,
    IKonomiTVBS4KPlaybackEncoder,
    IKonomiTVBS4KPlaybackQualityOption,
    IKonomiTVBS4KPlaybackVideoCodecOption,
} from '@/services/Videos';
import useServerSettingsStore from '@/stores/ServerSettingsStore';
import useSettingsStore, {
    type IKonomiTVBS4KPlaybackVideoProfile,
    type KonomiTVBS4KPlaybackAudioCodec,
    type KonomiTVBS4KPlaybackStreamingQuality,
    type KonomiTVBS4KPlaybackVideoCodec,
} from '@/stores/SettingsStore';
import Utils from '@/utils';

const QUALITY_H264: Array<{title: string; value: KonomiTVBS4KPlaybackStreamingQuality}> = [
    {title: '1080p (60fps) (約4.95GB/h / 平均11.0Mbps)', value: '1080p-60fps'},
    {title: '1080p (約4.50GB/h / 平均10.0Mbps)', value: '1080p'},
    {title: '810p (約2.62GB/h / 平均5.8Mbps)', value: '810p'},
    {title: '720p (約2.18GB/h / 平均4.9Mbps)', value: '720p'},
    {title: '540p (約1.52GB/h / 平均3.4Mbps)', value: '540p'},
    {title: '480p (約1.06GB/h / 平均2.3Mbps)', value: '480p'},
    {title: '360p (約0.60GB/h / 平均1.3Mbps)', value: '360p'},
    {title: '240p (約0.35GB/h / 平均0.8Mbps)', value: '240p'},
];

const QUALITY_H265: Array<{title: string; value: KonomiTVBS4KPlaybackStreamingQuality}> = [
    {title: '1080p (60fps) (約1.58GB/h / 平均3.5Mbps)', value: '1080p-60fps'},
    {title: '1080p (約1.37GB/h / 平均3.0Mbps)', value: '1080p'},
    {title: '810p (約1.05GB/h / 平均2.3Mbps)', value: '810p'},
    {title: '720p (約0.82GB/h / 平均1.8Mbps)', value: '720p'},
    {title: '540p (約0.53GB/h / 平均1.2Mbps)', value: '540p'},
    {title: '480p (約0.46GB/h / 平均1.0Mbps)', value: '480p'},
    {title: '360p (約0.30GB/h / 平均0.7Mbps)', value: '360p'},
    {title: '240p (約0.20GB/h / 平均0.4Mbps)', value: '240p'},
];

// 共通再生では既存の HEVC 指定値を基準に、VP9 は 90%、AV1 は 70% へ下げる。
// 表示は小数第1位へ丸めるが、実際の FFmpeg 指定値はサーバー側で Kbps 単位に計算される。
const QUALITY_VP9: Array<{title: string; value: KonomiTVBS4KPlaybackStreamingQuality}> = [
    {title: '1080p (60fps) (約1.42GB/h / 平均3.2Mbps)', value: '1080p-60fps'},
    {title: '1080p (約1.22GB/h / 平均2.7Mbps)', value: '1080p'},
    {title: '810p (約1.01GB/h / 平均2.3Mbps)', value: '810p'},
    {title: '720p (約0.81GB/h / 平均1.8Mbps)', value: '720p'},
    {title: '540p (約0.57GB/h / 平均1.3Mbps)', value: '540p'},
    {title: '480p (約0.43GB/h / 平均0.9Mbps)', value: '480p'},
    {title: '360p (約0.30GB/h / 平均0.7Mbps)', value: '360p'},
    {title: '240p (約0.18GB/h / 平均0.4Mbps)', value: '240p'},
];

const QUALITY_AV1: Array<{title: string; value: KonomiTVBS4KPlaybackStreamingQuality}> = [
    {title: '1080p (60fps) (約1.10GB/h / 平均2.5Mbps)', value: '1080p-60fps'},
    {title: '1080p (約0.95GB/h / 平均2.1Mbps)', value: '1080p'},
    {title: '810p (約0.79GB/h / 平均1.8Mbps)', value: '810p'},
    {title: '720p (約0.63GB/h / 平均1.4Mbps)', value: '720p'},
    {title: '540p (約0.44GB/h / 平均1.0Mbps)', value: '540p'},
    {title: '480p (約0.33GB/h / 平均0.7Mbps)', value: '480p'},
    {title: '360p (約0.24GB/h / 平均0.5Mbps)', value: '360p'},
    {title: '240p (約0.14GB/h / 平均0.3Mbps)', value: '240p'},
];

const KONOMITV_BS4K_PLAYBACK_QUALITY_BY_CODEC: Record<KonomiTVBS4KPlaybackVideoCodec, typeof QUALITY_H264> = {
    avc: QUALITY_H264,
    hevc: QUALITY_H265,
    vp9: QUALITY_VP9,
    av1: QUALITY_AV1,
};

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
            // フォームを小さくするかどうか
            is_form_dense: Utils.isSmartphoneHorizontal(),

            // タブの状態管理
            tab: null as number | null,

            // ネットワーク回線の種類
            network_circuits: ['Wi-Fi 回線時', 'モバイル回線時'],

            konomitv_bs4k_playback_streaming_quality: QUALITY_H264,
            konomitv_bs4k_playback_streaming_quality_cellular: QUALITY_H264,
            konomitv_bs4k_playback_encoder: 'FFmpeg' as IKonomiTVBS4KPlaybackEncoder,
            konomitv_bs4k_playback_capabilities: {
                video: [],
                audio: [],
                live_combinations: [],
            } as IKonomiTVBS4KPlaybackCapabilities,
        };
    },
    computed: {
        ...mapStores(useSettingsStore, useServerSettingsStore),
        konomitv_bs4k_playback_profile(): IKonomiTVBS4KPlaybackVideoProfile {
            return {
                is_bs4k: false,
                streaming_quality: this.settingsStore.settings.konomitv_bs4k_playback_streaming_quality,
            };
        },
        konomitv_bs4k_playback_profile_cellular(): IKonomiTVBS4KPlaybackVideoProfile {
            return {
                is_bs4k: false,
                streaming_quality: this.settingsStore.settings.konomitv_bs4k_playback_streaming_quality_cellular,
            };
        },
        konomitv_bs4k_playback_video_codecs(): IKonomiTVBS4KPlaybackVideoCodecOption[] {
            return Videos.buildKonomiTVBS4KPlaybackVideoCodecOptions(
                this.konomitv_bs4k_playback_encoder,
                this.konomitv_bs4k_playback_capabilities,
                this.konomitv_bs4k_playback_profile,
                this.settingsStore.settings.konomitv_bs4k_playback_audio_codec,
            );
        },
        konomitv_bs4k_playback_video_codecs_cellular(): IKonomiTVBS4KPlaybackVideoCodecOption[] {
            return Videos.buildKonomiTVBS4KPlaybackVideoCodecOptions(
                this.konomitv_bs4k_playback_encoder,
                this.konomitv_bs4k_playback_capabilities,
                this.konomitv_bs4k_playback_profile_cellular,
                this.settingsStore.settings.konomitv_bs4k_playback_audio_codec_cellular,
            );
        },
        konomitv_bs4k_playback_audio_codecs(): IKonomiTVBS4KPlaybackAudioCodecOption[] {
            return Videos.buildKonomiTVBS4KPlaybackAudioCodecOptions(
                this.konomitv_bs4k_playback_capabilities,
                this.konomitv_bs4k_playback_encoder,
                this.settingsStore.settings.konomitv_bs4k_playback_video_codec,
                this.konomitv_bs4k_playback_profile,
            );
        },
        konomitv_bs4k_playback_audio_codecs_cellular(): IKonomiTVBS4KPlaybackAudioCodecOption[] {
            return Videos.buildKonomiTVBS4KPlaybackAudioCodecOptions(
                this.konomitv_bs4k_playback_capabilities,
                this.konomitv_bs4k_playback_encoder,
                this.settingsStore.settings.konomitv_bs4k_playback_video_codec_cellular,
                this.konomitv_bs4k_playback_profile_cellular,
            );
        },
        konomitv_bs4k_playback_streaming_quality_options():
        IKonomiTVBS4KPlaybackQualityOption<KonomiTVBS4KPlaybackStreamingQuality>[] {
            return Videos.buildKonomiTVBS4KPlaybackQualityOptions(
                this.konomitv_bs4k_playback_encoder,
                this.konomitv_bs4k_playback_capabilities,
                this.settingsStore.settings.konomitv_bs4k_playback_video_codec,
                false,
                this.konomitv_bs4k_playback_streaming_quality,
                this.settingsStore.settings.konomitv_bs4k_playback_audio_codec,
            );
        },
        konomitv_bs4k_playback_streaming_quality_cellular_options():
        IKonomiTVBS4KPlaybackQualityOption<KonomiTVBS4KPlaybackStreamingQuality>[] {
            return Videos.buildKonomiTVBS4KPlaybackQualityOptions(
                this.konomitv_bs4k_playback_encoder,
                this.konomitv_bs4k_playback_capabilities,
                this.settingsStore.settings.konomitv_bs4k_playback_video_codec_cellular,
                false,
                this.konomitv_bs4k_playback_streaming_quality_cellular,
                this.settingsStore.settings.konomitv_bs4k_playback_audio_codec_cellular,
            );
        },
        konomitv_bs4k_auto_quality_max_options():
        IKonomiTVBS4KPlaybackQualityOption<KonomiTVBS4KPlaybackStreamingQuality>[] {
            return Videos.buildKonomiTVBS4KPlaybackQualityOptions(
                this.konomitv_bs4k_playback_encoder,
                this.konomitv_bs4k_playback_capabilities,
                'avc',
                false,
                this.konomitv_bs4k_playback_streaming_quality,
                'aac',
            );
        },
        konomitv_bs4k_auto_quality_max_cellular_options():
        IKonomiTVBS4KPlaybackQualityOption<KonomiTVBS4KPlaybackStreamingQuality>[] {
            return Videos.buildKonomiTVBS4KPlaybackQualityOptions(
                this.konomitv_bs4k_playback_encoder,
                this.konomitv_bs4k_playback_capabilities,
                'avc',
                false,
                this.konomitv_bs4k_playback_streaming_quality_cellular,
                'aac',
            );
        },
    },
    methods: {
        isAutoQualityModeForCircuit(network_circuit: string): boolean {
            return network_circuit === 'モバイル回線時' ?
                this.settingsStore.settings.konomitv_bs4k_playback_auto_quality_mode_cellular === true :
                this.settingsStore.settings.konomitv_bs4k_playback_auto_quality_mode === true;
        },
        updateKonomiTVBS4KPlaybackVideoCodec(
            konomitv_bs4k_selected_video_codec: KonomiTVBS4KPlaybackVideoCodec,
            is_konomitv_bs4k_cellular: boolean,
        ): void {
            const konomitv_bs4k_current_audio_codec = is_konomitv_bs4k_cellular === true ?
                this.settingsStore.settings.konomitv_bs4k_playback_audio_codec_cellular :
                this.settingsStore.settings.konomitv_bs4k_playback_audio_codec;
            const konomitv_bs4k_profile = is_konomitv_bs4k_cellular === true ?
                this.konomitv_bs4k_playback_profile_cellular :
                this.konomitv_bs4k_playback_profile;
            const konomitv_bs4k_combination =
                Videos.resolveKonomiTVBS4KPlaybackCombinationForVideoCodecChange(
                    this.konomitv_bs4k_playback_capabilities,
                    this.konomitv_bs4k_playback_encoder,
                    konomitv_bs4k_selected_video_codec,
                    konomitv_bs4k_current_audio_codec,
                    konomitv_bs4k_profile,
                );
            if (konomitv_bs4k_combination === null) return;

            this.settingsStore.updateKonomiTVBS4KPlaybackCodecPair(
                false,
                is_konomitv_bs4k_cellular,
                konomitv_bs4k_combination.video.codec,
                konomitv_bs4k_combination.audio.codec,
            );
        },
        updateKonomiTVBS4KPlaybackAudioCodec(
            konomitv_bs4k_selected_audio_codec: KonomiTVBS4KPlaybackAudioCodec,
            is_konomitv_bs4k_cellular: boolean,
        ): void {
            const konomitv_bs4k_current_video_codec = is_konomitv_bs4k_cellular === true ?
                this.settingsStore.settings.konomitv_bs4k_playback_video_codec_cellular :
                this.settingsStore.settings.konomitv_bs4k_playback_video_codec;
            const konomitv_bs4k_profile = is_konomitv_bs4k_cellular === true ?
                this.konomitv_bs4k_playback_profile_cellular :
                this.konomitv_bs4k_playback_profile;
            const konomitv_bs4k_combination =
                Videos.resolveKonomiTVBS4KPlaybackCombinationForAudioCodecChange(
                    this.konomitv_bs4k_playback_capabilities,
                    this.konomitv_bs4k_playback_encoder,
                    konomitv_bs4k_current_video_codec,
                    konomitv_bs4k_selected_audio_codec,
                    konomitv_bs4k_profile,
                );
            if (konomitv_bs4k_combination === null) return;

            this.settingsStore.updateKonomiTVBS4KPlaybackCodecPair(
                false,
                is_konomitv_bs4k_cellular,
                konomitv_bs4k_combination.video.codec,
                konomitv_bs4k_combination.audio.codec,
            );
        },
    },
    async mounted() {
        // 利用中encoderのlive/recorded能力とブラウザMSEの積集合を1回の能力応答から作る。
        const [konomitv_bs4k_server_settings, konomitv_bs4k_capabilities] = await Promise.all([
            this.serverSettingsStore.fetchServerSettingsOnce(),
            Videos.fetchKonomiTVBS4KPlaybackCapabilities(),
        ]);
        const konomitv_bs4k_encoder =
            konomitv_bs4k_server_settings?.general.encoder ??
            this.serverSettingsStore.server_settings.general.encoder;
        this.konomitv_bs4k_playback_encoder = konomitv_bs4k_encoder;
        this.konomitv_bs4k_playback_capabilities = konomitv_bs4k_capabilities;

        if (this.settingsStore.consumeKonomiTVBS4KPlaybackProfileMigrationConflictNotice()) {
            Message.info(
                '旧ライブ・録画設定が競合していた項目は、映像・画質と通常放送の24fpsは旧ライブ側、' +
                '音声は旧録画側を引き継ぎました。必要に応じて現在の値を確認してください。',
            );
        }
    },
    watch: {
        'settingsStore.settings.konomitv_bs4k_playback_video_codec': {
            immediate: true,
            handler(konomitv_bs4k_value: KonomiTVBS4KPlaybackVideoCodec) {
                this.konomitv_bs4k_playback_streaming_quality =
                    KONOMITV_BS4K_PLAYBACK_QUALITY_BY_CODEC[konomitv_bs4k_value];
            },
        },
        'settingsStore.settings.konomitv_bs4k_playback_video_codec_cellular': {
            immediate: true,
            handler(konomitv_bs4k_value: KonomiTVBS4KPlaybackVideoCodec) {
                this.konomitv_bs4k_playback_streaming_quality_cellular =
                    KONOMITV_BS4K_PLAYBACK_QUALITY_BY_CODEC[konomitv_bs4k_value];
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
