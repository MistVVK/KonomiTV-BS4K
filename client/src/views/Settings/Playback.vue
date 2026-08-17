<template>
    <SettingsBase>
        <h2 class="settings__heading">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:video-clip-multiple-16-filled" width="26px" />
            <span class="ml-3">再生・画質</span>
        </h2>
        <div class="settings__description">
            ライブ視聴と録画再生は、通常放送 / BS4K × Wi-Fi / モバイルの4つの共通プロファイルを使います。<br>
            非対応のコーデックも希望値として保存でき、再生開始前だけ AV1 → VP9 → HEVC → AVC、Opus → AAC の順で利用可能な構成を選びます。<br>
        </div>

        <v-alert v-if="migration_conflict_notice" class="mx-4 mb-4" type="info" variant="tonal">
            旧ライブ・録画設定に異なる値があったため、ライブ側の値を共通プロファイルへ引き継ぎました。
        </v-alert>

        <v-tabs class="settings__tab" color="primary" bg-color="transparent" align-tabs="center" v-model="broadcast_tab">
            <v-tab style="text-transform: none !important;">通常放送</v-tab>
            <v-tab style="text-transform: none !important;">BS4K</v-tab>
        </v-tabs>
        <v-tabs class="settings__tab" color="primary" bg-color="transparent" align-tabs="center" v-model="network_tab">
            <v-tab style="text-transform: none !important;">Wi-Fi</v-tab>
            <v-tab style="text-transform: none !important;">モバイル回線</v-tab>
        </v-tabs>

        <div class="settings__content mt-0">
            <div class="settings__content-heading mt-6">
                <Icon :icon="is_bs4k ? 'fluent:tv-24-filled' : 'fluent:tv-20-filled'" width="22px" />
                <span class="ml-2">{{ profile_title }}</span>
            </div>

            <div class="settings__item settings__item--sync-disabled">
                <div class="settings__item-heading">画質</div>
                <div class="settings__item-label">
                    ライブ視聴と録画再生の開始時に同じ画質を使います。視聴中の一時変更は保存値を書き換えません。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :items="quality_options" :model-value="selected_quality"
                    @update:model-value="updateQuality">
                </v-select>
            </div>

            <div class="settings__item settings__item--sync-disabled">
                <div class="settings__item-heading">映像コーデック</div>
                <div class="settings__item-label">
                    AV1 を既定とし、再生前にブラウザ MSE とライブ・録画それぞれの能力から実効値を確定します。<br>
                    理由が付いた項目も将来の能力追加に備えた希望値として選択できます。
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :items="video_codec_options" :model-value="selected_video_codec"
                    @update:model-value="updateVideoCodec">
                </v-select>
            </div>

            <div class="settings__item settings__item--sync-disabled">
                <div class="settings__item-heading">音声コーデック</div>
                <div class="settings__item-label">
                    Opus を既定とし、利用できない場合だけ再生開始前に AAC を選びます。再生開始後の自動変更は行いません。
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :items="audio_codec_options" :model-value="selected_audio_codec"
                    @update:model-value="updateAudioCodec">
                </v-select>
            </div>

            <div class="settings__item settings__item--switch settings__item--sync-disabled">
                <label class="settings__item-heading" :for="`playback-low-latency-${broadcast_tab}-${network_tab}`">
                    ライブ視聴を低遅延にする
                </label>
                <label class="settings__item-label" :for="`playback-low-latency-${broadcast_tab}-${network_tab}`">
                    ライブ専用の再生バッファ設定です。録画再生の共通画質・コーデック設定には影響しません。<br>
                    <span v-if="is_bs4k_low_latency_forced" class="text-error-readable">
                        サーバー設定で BS4K の通常バッファが強制されているため、Wi-Fi / モバイルとも無効です。
                    </span>
                </label>
                <v-switch class="settings__item-switch" color="primary" hide-details
                    :id="`playback-low-latency-${broadcast_tab}-${network_tab}`"
                    :model-value="selected_low_latency" :disabled="is_bs4k_low_latency_forced"
                    @update:model-value="updateLowLatency">
                </v-switch>
            </div>
            <div v-if="is_bs4k_tlv === true" class="settings__item settings__item--switch settings__item--sync-disabled">
                <label class="settings__item-heading" for="playback-rain-fallback-bs4k">
                    BSP4K で降雨対応放送を自動で使用する
                </label>
                <label class="settings__item-label" for="playback-rain-fallback-bs4k">
                    1080p (60fps) 以下の画質では、降雨対応放送（1080p 低階層）の送出状態を継続して監視します。<br>
                    送出の開始・終了を検知するとライブストリームを再接続し、低階層・主階層を自動で切り替えます。<br>
                </label>
                <v-switch class="settings__item-switch" color="primary" hide-details id="playback-rain-fallback-bs4k"
                    :model-value="selected_rain_fallback_bs4k" @update:model-value="updateRainFallbackBS4K">
                </v-switch>
            </div>
            <div v-if="is_bs4k_tlv === true" class="settings__item settings__item--switch settings__item--sync-disabled">
                <label class="settings__item-heading" for="playback-rain-fallback-bs8k">
                    BS8K で降雨対応放送を自動で使用する
                </label>
                <label class="settings__item-label" for="playback-rain-fallback-bs8k">
                    1080p (60fps) 以下の画質では、降雨対応放送（1080p 低階層）の送出状態を継続して監視します。<br>
                    送出の開始・終了を検知するとライブストリームを再接続し、低階層・主階層を自動で切り替えます。<br>
                </label>
                <v-switch class="settings__item-switch" color="primary" hide-details id="playback-rain-fallback-bs8k"
                    :model-value="selected_rain_fallback_bs8k" @update:model-value="updateRainFallbackBS8K">
                </v-switch>
            </div>
            <div v-if="is_bs4k === true" class="settings__item settings__item--sync-disabled">
                <div class="settings__item-heading">24fpsモード</div>
                <div class="settings__item-label">
                    BS4K は 60p プログレッシブ放送のため、テレシネ解除を行う24fpsモードは適用しません。<br>
                    24fpsモードは通常放送プロファイルでのみ設定できます。
                </div>
            </div>

            <div v-else class="settings__item settings__item--switch settings__item--sync-disabled">
                <label class="settings__item-heading" :for="`playback-24fps-${broadcast_tab}-${network_tab}`">
                    24fpsモード
                </label>
                <label class="settings__item-label" :for="`playback-24fps-${broadcast_tab}-${network_tab}`">
                    映画やアニメのテレシネ由来フレームを検出し、本来の動きに近づけます。60fps画質では60fpsを優先します。<br>
                </label>
                <v-switch class="settings__item-switch" color="primary" hide-details
                    :id="`playback-24fps-${broadcast_tab}-${network_tab}`"
                    :model-value="selected_24fps" @update:model-value="update24FPS">
                </v-switch>
            </div>

            <div class="settings__item settings__item--sync-disabled">
                <div class="settings__item-heading">現在の能力と再生開始時の実効値</div>
                <div class="settings__item-label" v-if="capabilities_loading">
                    サーバーのライブ・録画能力を検査しています…
                </div>
                <div class="settings__item-label text-error-readable" v-else-if="capabilities_failed">
                    能力 API を取得できませんでした。再生時は既知の AVC 8bit / AAC 互換経路だけを試します。
                </div>
                <div class="settings__item-label" v-else>
                    希望: <b>{{selected_video_codec.toUpperCase()}} / {{selected_audio_codec.toUpperCase()}}</b><br>
                    ライブ実効候補:
                    <template v-if="effective_live_profile !== null">
                        <b>{{effective_live_profile.video.codec.toUpperCase()}}
                        {{effective_live_profile.video.bit_depth}}bit /
                        {{effective_live_profile.audio.codec.toUpperCase()}}</b>
                        <span v-if="is_effective_live_fallback">（再生開始前フォールバック）</span>
                    </template>
                    <span v-else class="text-error-readable">利用できる構成がありません</span><br>
                    録画実効候補:
                    <template v-if="effective_video_profile !== null">
                        <b>{{effective_video_profile.video.codec.toUpperCase()}}
                        {{effective_video_profile.video.bit_depth}}bit /
                        {{effective_video_profile.audio.codec.toUpperCase()}}</b>
                        <span v-if="is_effective_video_fallback">（再生開始前フォールバック）</span>
                    </template>
                    <span v-else class="text-error-readable">利用できる構成がありません</span>
                </div>
            </div>
        </div>
    </SettingsBase>
</template>

<script lang="ts" setup>

import { computed, onMounted, ref } from 'vue';

import Videos, {
    type IKonomiTVBS4KPlaybackCapabilities,
    type IKonomiTVBS4KResolvedPlaybackCombination,
} from '@/services/Videos';
import useSettingsStore, {
    BS4K_LIVE_STREAMING_QUALITIES,
    getKonomiTVBS4KPlayback24FPSModeSettingKey,
    getKonomiTVBS4KPlaybackAudioCodecSettingKey,
    getKonomiTVBS4KPlaybackStreamingQualitySettingKey,
    getKonomiTVBS4KPlaybackVideoCodecSettingKey,
    type KonomiTVBS4KPlaybackAudioCodec,
    type KonomiTVBS4KPlaybackVideoCodec,
    LIVE_STREAMING_QUALITIES,
} from '@/stores/SettingsStore';
import useVersionStore from '@/stores/VersionStore';
import SettingsBase from '@/views/Settings/Base.vue';

const settingsStore = useSettingsStore();
const versionStore = useVersionStore();
const broadcast_tab = ref(0);
const network_tab = ref(0);
const migration_conflict_notice = ref(false);
const capabilities_loading = ref(true);
const capabilities_failed = ref(false);
const capabilities = ref<IKonomiTVBS4KPlaybackCapabilities>({video: [], audio: [], live_combinations: []});

const is_bs4k = computed(() => broadcast_tab.value === 1);
const is_cellular = computed(() => network_tab.value === 1);
const profile_title = computed(() =>
    `${is_bs4k.value ? 'BS4K' : '通常放送'} / ${is_cellular.value ? 'モバイル' : 'Wi-Fi'}`,
);
const selected_quality = computed(() => settingsStore.settings[
    getKonomiTVBS4KPlaybackStreamingQualitySettingKey(is_bs4k.value, is_cellular.value)
]);
const selected_video_codec = computed(() => settingsStore.settings[
    getKonomiTVBS4KPlaybackVideoCodecSettingKey(is_bs4k.value, is_cellular.value)
]);
const selected_audio_codec = computed(() => settingsStore.settings[
    getKonomiTVBS4KPlaybackAudioCodecSettingKey(is_bs4k.value, is_cellular.value)
]);
const selected_24fps = computed(() => is_bs4k.value === true ? false : settingsStore.settings[
    getKonomiTVBS4KPlayback24FPSModeSettingKey(false, is_cellular.value)
]);
const selected_low_latency = computed(() => {
    if (
        is_bs4k.value &&
        versionStore.server_version_info?.bs4k_ignore_viewer_low_latency === true
    ) return false;
    if (is_bs4k.value) {
        return is_cellular.value ?
            settingsStore.settings.tv_low_latency_mode_for_bs4k_cellular :
            settingsStore.settings.tv_low_latency_mode_for_bs4k;
    }
    return is_cellular.value ?
        settingsStore.settings.tv_low_latency_mode_cellular :
        settingsStore.settings.tv_low_latency_mode;
});
const is_bs4k_low_latency_forced = computed(() =>
    is_bs4k.value &&
    versionStore.server_version_info?.bs4k_ignore_viewer_low_latency === true,
);
// 降雨対応放送の設定は TLV (MMT/TLV 専用経路) を使う BS4K でのみ意味を持つため、その条件を満たす場合だけ表示する。
const is_bs4k_tlv = computed(() =>
    is_bs4k.value &&
    versionStore.server_version_info?.konomitv_bs4k_live_transport === 'Tlv',
);

// フルサーバー設定ではなく、公開 runtime 情報を参照する
const encoder = computed(() => is_bs4k.value ?
    (versionStore.server_version_info?.encoder_bs4k ?? 'FFmpeg') :
    (versionStore.server_version_info?.encoder ?? 'FFmpeg'),
);
const base_quality_options = computed(() => (
    is_bs4k.value ? BS4K_LIVE_STREAMING_QUALITIES : LIVE_STREAMING_QUALITIES
).map((quality) => ({
    title: quality === '4320p' ? '8K' : quality === '2160p' ? '4K' :
        quality.replace('-60fps', ' (60fps)').replace('-30fps', ' (30fps)'),
    value: quality,
})));
const quality_options = computed(() => Videos.buildKonomiTVBS4KPlaybackQualityOptions(
    encoder.value,
    capabilities.value,
    selected_video_codec.value,
    is_bs4k.value,
    base_quality_options.value,
    selected_audio_codec.value,
));
const playback_profile = computed(() => ({
    is_bs4k: is_bs4k.value,
    streaming_quality: selected_quality.value,
}));
const video_codec_options = computed(() => {
    const order: readonly KonomiTVBS4KPlaybackVideoCodec[] = ['av1', 'vp9', 'hevc', 'avc'];
    const options = Videos.buildKonomiTVBS4KPlaybackVideoCodecOptions(
        encoder.value,
        capabilities.value,
        playback_profile.value,
        selected_audio_codec.value,
    );
    return order.map((codec) => options.find((option) => option.value === codec)!);
});
const audio_codec_options = computed(() => {
    const order: readonly KonomiTVBS4KPlaybackAudioCodec[] = ['opus', 'aac'];
    const options = Videos.buildKonomiTVBS4KPlaybackAudioCodecOptions(
        capabilities.value,
        encoder.value,
        selected_video_codec.value,
        playback_profile.value,
    );
    return order.map((codec) => options.find((option) => option.value === codec)!);
});
const effective_live_profile = computed<IKonomiTVBS4KResolvedPlaybackCombination | null>(() =>
    Videos.resolveKonomiTVBS4KPlaybackCombination(
        capabilities.value,
        encoder.value,
        selected_video_codec.value,
        selected_audio_codec.value,
        playback_profile.value,
        'Live',
    ),
);
const effective_video_profile = computed<IKonomiTVBS4KResolvedPlaybackCombination | null>(() =>
    Videos.resolveKonomiTVBS4KPlaybackCombination(
        capabilities.value,
        encoder.value,
        selected_video_codec.value,
        selected_audio_codec.value,
        playback_profile.value,
        'Video',
    ),
);
const is_effective_live_fallback = computed(() => effective_live_profile.value !== null && (
    effective_live_profile.value.video.codec !== selected_video_codec.value ||
    effective_live_profile.value.audio.codec !== selected_audio_codec.value
));
const is_effective_video_fallback = computed(() => effective_video_profile.value !== null && (
    effective_video_profile.value.video.codec !== selected_video_codec.value ||
    effective_video_profile.value.audio.codec !== selected_audio_codec.value
));

const patchSetting = (key: string, value: unknown): void => {
    settingsStore.$patch({settings: {...settingsStore.settings, [key]: value}});
};
const updateQuality = (value: string): void => {
    patchSetting(getKonomiTVBS4KPlaybackStreamingQualitySettingKey(is_bs4k.value, is_cellular.value), value);
};
const updateVideoCodec = (value: KonomiTVBS4KPlaybackVideoCodec): void => {
    settingsStore.updateKonomiTVBS4KPlaybackCodecPair(
        is_bs4k.value,
        is_cellular.value,
        value,
        selected_audio_codec.value,
    );
};
const updateAudioCodec = (value: KonomiTVBS4KPlaybackAudioCodec): void => {
    settingsStore.updateKonomiTVBS4KPlaybackCodecPair(
        is_bs4k.value,
        is_cellular.value,
        selected_video_codec.value,
        value,
    );
};
const update24FPS = (value: boolean | null): void => {
    if (is_bs4k.value === true) return;
    patchSetting(getKonomiTVBS4KPlayback24FPSModeSettingKey(false, is_cellular.value), value === true);
};
const updateLowLatency = (value: boolean | null): void => {
    // サーバー強制中は実効値だけを false にし、強制解除後に復元する端末側の希望値を変更しない。
    if (is_bs4k_low_latency_forced.value === true) return;
    const key = is_bs4k.value ?
        (is_cellular.value ? 'tv_low_latency_mode_for_bs4k_cellular' : 'tv_low_latency_mode_for_bs4k') :
        (is_cellular.value ? 'tv_low_latency_mode_cellular' : 'tv_low_latency_mode');
    patchSetting(key, value === true);
};
const selected_rain_fallback_bs4k = computed(() => settingsStore.settings.tv_use_rain_fallback_for_bs4k);
const selected_rain_fallback_bs8k = computed(() => settingsStore.settings.tv_use_rain_fallback_for_bs8k);
const updateRainFallbackBS4K = (value: boolean | null): void => {
    patchSetting('tv_use_rain_fallback_for_bs4k', value === true);
};
const updateRainFallbackBS8K = (value: boolean | null): void => {
    patchSetting('tv_use_rain_fallback_for_bs8k', value === true);
};

onMounted(async () => {
    migration_conflict_notice.value =
        settingsStore.consumeKonomiTVBS4KPlaybackProfileMigrationConflictNotice();
    // encoder と BS4K 通常バッファ強制値を公開 runtime 情報から確定してから能力表示を解決する。
    await versionStore.fetchServerVersion(true);
    capabilities.value = await Videos.fetchKonomiTVBS4KPlaybackCapabilities();
    capabilities_failed.value =
        capabilities.value.video.length === 0 && capabilities.value.audio.length === 0;
    capabilities_loading.value = false;
});

</script>
