<template>
    <v-dialog max-width="620" v-model="isShown">
        <v-card class="offline-download-dialog__card">
            <v-card-title class="d-flex justify-center pt-6 font-weight-bold">
                オフライン再生用に保存
            </v-card-title>
            <v-card-text class="pt-4 px-6 pb-0">
                <div class="offline-download-dialog__title mb-4">
                    <div class="text-h6 text-text mb-2"
                        v-html="ProgramUtils.decorateProgramInfo(program, 'title')"></div>
                    <div class="text-body-2 text-text-darken-1">
                        {{ ProgramUtils.getProgramTime(program) }}
                    </div>
                </div>
                <div v-if="activeDownloadJob !== null" class="warning-banner warning-banner--normal mt-4">
                    <Icon icon="fluent:info-16-regular" class="warning-banner__icon" />
                    <span class="warning-banner__text">
                        現在オフライン保存中です ({{OfflineVideos.formatQualityLabel(activeDownloadJob.quality)}}) 。<br>
                        完了するまで新しい保存は開始できません。
                    </span>
                </div>
                <div v-if="savedVideo !== null" class="warning-banner warning-banner--keyword mt-4">
                    <Icon icon="fluent:warning-16-filled" class="warning-banner__icon" />
                    <span class="warning-banner__text">
                        現在は {{OfflineVideos.formatQualityLabel(savedVideo.quality)}} / {{OfflineVideos.formatOfflineSize(savedVideo.size_bytes, false)}} で保存されています。<br>
                        新しい保存が完了するまで、現在のデータは削除されません。
                    </span>
                </div>
                <v-select class="offline-download-dialog__select settings__item-form mt-7" v-model="selectedQuality"
                    :items="qualityItems" label="保存画質" color="primary" variant="outlined" hide-details
                    :density="selectDensity" />
                <v-select class="offline-download-dialog__select settings__item-form mt-4" v-model="selectedVideoCodec"
                    :items="videoCodecItems" label="映像コーデック" color="primary" variant="outlined" hide-details
                    :density="selectDensity" />
                <v-select class="offline-download-dialog__select settings__item-form mt-4" v-model="selectedAudioCodec"
                    :items="audioCodecItems" label="音声コーデック" color="primary" variant="outlined" hide-details
                    :density="selectDensity" />
                <div class="offline-download-dialog__switch mt-3">
                    <div>
                        <div class="font-weight-bold mb-1" style="font-size: 15px;">24fps モード</div>
                        <div class="text-text-darken-1">映画やアニメなど 24fps で制作された映像を検出し、本来の動きに近づけます。</div>
                    </div>
                    <v-switch v-model="is24fpsMode" color="primary" hide-details :disabled="is24fpsUnavailable" />
                </div>
                <v-alert v-if="estimate !== null" class="mt-4" color="info" variant="tonal">
                    推定保存容量: {{OfflineVideos.formatOfflineSize(estimate.estimated_size_bytes, true)}}
                </v-alert>
                <v-alert v-else-if="isEstimateLoading" class="mt-4" color="info" variant="tonal">
                    保存容量を計算しています…
                </v-alert>
                <v-alert v-else-if="estimateError !== null" class="mt-4" color="error" variant="tonal">
                    {{estimateError}}
                </v-alert>
                <v-alert v-if="isMeteredConnection" class="mt-4" color="warning" variant="tonal">
                    従量制通信の可能性があります。通信量に注意してください。
                </v-alert>
            </v-card-text>
            <v-card-actions class="pt-7 px-6 pb-6">
                <v-spacer />
                <v-btn color="text" variant="text" @click="isShown = false">
                    <Icon icon="fluent:dismiss-16-filled" width="18px" height="18px" />
                    <span class="ml-1">キャンセル</span>
                </v-btn>
                <v-btn class="px-3" color="secondary" variant="flat"
                    :disabled="isPreparing || estimate === null || effectiveProfile === null || activeDownloadJob !== null"
                    :loading="isStarting" @click="startDownload">
                    <Icon icon="fluent:cloud-arrow-down-20-filled" width="18px" height="18px" />
                    <span class="ml-1">{{savedVideo === null ? 'ダウンロード開始' : '保存し直す'}}</span>
                </v-btn>
            </v-card-actions>
        </v-card>
    </v-dialog>

    <!-- 永続ストレージを利用できない場合の確認ダイアログ -->
    <v-dialog v-model="showPersistenceWarning" max-width="620" persistent>
        <v-card class="offline-download-dialog__card">
            <v-card-title class="d-flex justify-center pt-6 font-weight-bold">
                保存データが自動削除される可能性があります
            </v-card-title>
            <v-card-text class="pt-2 pb-0">
                <v-alert color="warning" variant="tonal">
                    ブラウザから永続ストレージの利用を許可されませんでした。空き容量が減った場合、ブラウザの判断でオフライン保存が削除されることがあります。
                </v-alert>
            </v-card-text>
            <v-card-actions class="pt-4 px-6 pb-6">
                <v-spacer />
                <v-btn color="text" variant="text" @click="cancelPersistenceWarning">
                    <Icon icon="fluent:dismiss-16-filled" width="18px" height="18px" />
                    <span class="ml-1">キャンセル</span>
                </v-btn>
                <v-btn class="px-3" color="secondary" variant="flat" :loading="isStarting" @click="continueAfterPersistenceWarning">
                    <Icon icon="fluent:arrow-download-16-filled" width="18px" height="18px" />
                    <span class="ml-1">そのまま保存</span>
                </v-btn>
            </v-card-actions>
        </v-card>
    </v-dialog>
</template>
<script lang="ts" setup>

import { computed, ref, watch } from 'vue';

import type {
    IKonomiTVBS4KPlaybackCapabilities,
    IKonomiTVBS4KResolvedPlaybackCombination,
    IRecordedProgram,
} from '@/services/Videos';
import type {
    BS4KLiveStreamingQuality,
    KonomiTVBS4KPlaybackAudioCodec,
    KonomiTVBS4KPlaybackVideoCodec,
    VideoStreamingQuality,
} from '@/stores/SettingsStore';

import Message from '@/message';
import OfflineVideos, {
    type IKonomiTVBS4KOfflineDownloadProfile,
    type IKonomiTVBS4KOfflineStreamEstimate,
    type IOfflineDownloadJob,
    type IOfflineVideo,
} from '@/services/OfflineVideos';
import Videos from '@/services/Videos';
import useSettingsStore, {
    BS4K_LIVE_STREAMING_QUALITIES,
    VIDEO_STREAMING_QUALITIES,
} from '@/stores/SettingsStore';
import useVersionStore from '@/stores/VersionStore';
import Utils, { PlayerUtils, ProgramUtils } from '@/utils';

const props = defineProps<{
    program: IRecordedProgram;
    show: boolean;
}>();

const emit = defineEmits<{
    (event: 'update:show', show: boolean): void;
}>();

const settingsStore = useSettingsStore();
const versionStore = useVersionStore();
const selectedQuality = ref<VideoStreamingQuality | BS4KLiveStreamingQuality>('720p');
const selectedVideoCodec = ref<KonomiTVBS4KPlaybackVideoCodec>('av1');
const selectedAudioCodec = ref<KonomiTVBS4KPlaybackAudioCodec>('opus');
const is24fpsMode = ref(false);
const isPreparing = ref(false);
const isStarting = ref(false);
const isEstimateLoading = ref(false);
const estimateError = ref<string | null>(null);
const savedVideo = ref<IOfflineVideo | null>(null);
const activeDownloadJob = ref<IOfflineDownloadJob | null>(null);
const capabilities = ref<IKonomiTVBS4KPlaybackCapabilities>({video: [], audio: [], live_combinations: []});
const estimate = ref<IKonomiTVBS4KOfflineStreamEstimate | null>(null);
const showPersistenceWarning = ref(false);
const pendingDownload = ref<{
    profile: IKonomiTVBS4KOfflineDownloadProfile;
    estimate: IKonomiTVBS4KOfflineStreamEstimate;
} | null>(null);
let estimateRequestSequence = 0;
let estimateAbortController: AbortController | null = null;

const isShown = computed({
    get: () => props.show,
    set: value => emit('update:show', value),
});
const isBS4K = computed(() => props.program.network_id === 0x000B);
const encoder = computed(() => isBS4K.value ?
    (versionStore.server_version_info?.encoder_bs4k ?? 'FFmpeg') :
    (versionStore.server_version_info?.encoder ?? 'FFmpeg'));
const isMeteredConnection = computed(() => {
    const connection = navigator.connection;
    return connection !== undefined && (connection.saveData === true || ['slow-2g', '2g', '3g'].includes(connection.effectiveType ?? ''));
});
const selectDensity = computed(() => Utils.isSmartphoneHorizontal() ? 'compact' : 'default');

/** 通常録画と BS4K 録画で許可された画質一覧を返す */
const qualityItems = computed(() => {
    const qualities = isBS4K.value ? BS4K_LIVE_STREAMING_QUALITIES : VIDEO_STREAMING_QUALITIES;
    return qualities.map(quality => ({
        title: PlayerUtils.getKonomiTVBS4KPlaybackQualityDisplayName(quality),
        value: quality,
        props: {disabled: resolveExactProfile(selectedVideoCodec.value, selectedAudioCodec.value, quality) === null},
    }));
});

const videoCodecItems = computed(() => {
    const labels: Record<KonomiTVBS4KPlaybackVideoCodec, string> = {
        av1: 'AV1', vp9: 'VP9', hevc: 'H.265 / HEVC', avc: 'H.264 / AVC',
    };
    return (['av1', 'vp9', 'hevc', 'avc'] as const).map(codec => ({
        title: labels[codec],
        value: codec,
        props: {disabled: resolveExactProfile(codec, selectedAudioCodec.value, selectedQuality.value) === null},
    }));
});

const audioCodecItems = computed(() => (['opus', 'aac'] as const).map(codec => ({
    title: codec === 'opus' ? 'Opus' : 'AAC',
    value: codec,
    props: {disabled: resolveExactProfile(selectedVideoCodec.value, codec, selectedQuality.value) === null},
})));

const effectiveProfile = computed(() =>
    resolveExactProfile(selectedVideoCodec.value, selectedAudioCodec.value, selectedQuality.value),
);
const is24fpsUnavailable = computed(() => isBS4K.value || selectedQuality.value.endsWith('-60fps'));

/** 現在のブラウザとサーバーの録画能力を満たす exact profile を返す */
function resolveExactProfile(
    videoCodec: KonomiTVBS4KPlaybackVideoCodec,
    audioCodec: KonomiTVBS4KPlaybackAudioCodec,
    quality: VideoStreamingQuality | BS4KLiveStreamingQuality,
): IKonomiTVBS4KResolvedPlaybackCombination | null {
    if (props.program.recorded_video.has_video === false) {
        const audio = capabilities.value.audio.find(capability =>
            capability.codec === audioCodec && capability.recorded_available === true,
        );
        if (audio === undefined || PlayerUtils.isKonomiTVBS4KPlaybackAudioCodecSupported(audioCodec) === false) return null;
        return {
            video: {
                encoder: encoder.value,
                codec: 'avc',
                bit_depth: 8,
                profile: 'High',
                live_available: false,
                recorded_available: true,
                live_reason_code: null,
                recorded_reason_code: null,
            },
            audio,
            live: null,
        };
    }
    return Videos.resolveKonomiTVBS4KExactPlaybackCombination(
        capabilities.value,
        encoder.value,
        videoCodec,
        audioCodec,
        {is_bs4k: isBS4K.value, streaming_quality: quality},
        'Video',
    );
}

/** 保存 API へ渡す exact profile を現在の選択値から組み立てる */
const buildProfile = (): IKonomiTVBS4KOfflineDownloadProfile | null => {
    if (effectiveProfile.value === null) return null;
    return {
        quality: selectedQuality.value,
        video_codec: effectiveProfile.value.video.codec,
        video_bit_depth: effectiveProfile.value.video.bit_depth,
        audio_codec: effectiveProfile.value.audio.codec,
        is_24fps_mode: is24fpsUnavailable.value === false && is24fpsMode.value,
    };
};

/** 選択中の全音声レンディション込み容量を非同期更新する */
const refreshEstimate = async (): Promise<void> => {
    const profile = buildProfile();
    const requestSequence = ++estimateRequestSequence;
    estimateAbortController?.abort();
    estimateAbortController = null;
    estimate.value = null;
    estimateError.value = null;
    if (profile === null || isPreparing.value === true) return;
    const abortController = new AbortController();
    estimateAbortController = abortController;
    isEstimateLoading.value = true;
    try {
        const result = await OfflineVideos.fetchEstimate(props.program.id, profile, abortController.signal);
        if (requestSequence === estimateRequestSequence) estimate.value = result;
    } catch (error) {
        // 新しい選択やダイアログ終了による中断は利用者へ見せる失敗ではない
        if (abortController.signal.aborted === true) return;
        if (requestSequence === estimateRequestSequence) {
            console.warn('[OfflineVideoDownloadDialog] Failed to estimate offline stream size:', error);
            const detail = error instanceof Error ? `（${error.message}）` : '';
            estimateError.value = `保存容量を計算できませんでした。設定を選び直すか、しばらく待ってからもう一度お試しください。${detail}`;
        }
    } finally {
        if (estimateAbortController === abortController) estimateAbortController = null;
        if (requestSequence === estimateRequestSequence) isEstimateLoading.value = false;
    }
};

/** 前回値が現在の能力で使えない場合だけ、最も希望に近い利用可能 tuple へ下げる */
const normalizeSelections = (): void => {
    const requestedProfile = Videos.resolveKonomiTVBS4KPlaybackCombination(
        capabilities.value,
        encoder.value,
        selectedVideoCodec.value,
        selectedAudioCodec.value,
        {is_bs4k: isBS4K.value, streaming_quality: selectedQuality.value},
        'Video',
    );
    if (requestedProfile !== null) {
        selectedVideoCodec.value = requestedProfile.video.codec;
        selectedAudioCodec.value = requestedProfile.audio.codec;
        return;
    }
    const qualities = isBS4K.value ? BS4K_LIVE_STREAMING_QUALITIES : VIDEO_STREAMING_QUALITIES;
    for (const quality of qualities) {
        const profile = Videos.resolveKonomiTVBS4KPlaybackCombination(
            capabilities.value,
            encoder.value,
            selectedVideoCodec.value,
            selectedAudioCodec.value,
            {is_bs4k: isBS4K.value, streaming_quality: quality},
            'Video',
        );
        if (profile === null) continue;
        selectedQuality.value = quality;
        selectedVideoCodec.value = profile.video.codec;
        selectedAudioCodec.value = profile.audio.codec;
        return;
    }
};

/** 保存成功時に、通常録画・BS4K 録画ごとの前回値を端末設定へ保持する */
const persistSelections = (): void => {
    settingsStore.$patch({settings: {
        ...settingsStore.settings,
        ...(isBS4K.value ? {
            konomitv_bs4k_offline_video_streaming_quality_for_bs4k:
                selectedQuality.value as BS4KLiveStreamingQuality,
            konomitv_bs4k_offline_video_codec_for_bs4k: selectedVideoCodec.value,
            konomitv_bs4k_offline_audio_codec_for_bs4k: selectedAudioCodec.value,
        } : {
            konomitv_bs4k_offline_video_streaming_quality: selectedQuality.value as VideoStreamingQuality,
            konomitv_bs4k_offline_video_codec: selectedVideoCodec.value,
            konomitv_bs4k_offline_audio_codec: selectedAudioCodec.value,
            konomitv_bs4k_offline_video_24fps_mode: is24fpsMode.value,
        }),
    }});
};

/** 選択した画質でオフライン保存を開始する */
const startDownload = async (): Promise<void> => {
    if (isStarting.value === true || activeDownloadJob.value !== null) return;
    isStarting.value = true;
    try {
        const profile = buildProfile();
        if (profile === null || estimate.value === null) throw new Error('利用可能な保存形式を選択してください。');

        // 永続化を拒否された環境では、専用ダイアログでブラウザによる削除可能性を伝える
        if (await OfflineVideos.requestPersistentStorage() === false) {
            pendingDownload.value = {profile, estimate: estimate.value};
            showPersistenceWarning.value = true;
            return;
        }
        await OfflineVideos.start(props.program, profile, estimate.value);
        persistSelections();
        Message.success('オフライン保存を開始しました。');
        isShown.value = false;
    } catch (error) {
        Message.error(error instanceof Error ? error.message : 'オフライン保存を開始できませんでした。');
    } finally {
        isStarting.value = false;
    }
};

/** 永続ストレージを利用できない条件を了承して保存を開始する */
const continueAfterPersistenceWarning = async (): Promise<void> => {
    if (pendingDownload.value === null || isStarting.value === true || activeDownloadJob.value !== null) return;
    isStarting.value = true;
    try {
        await OfflineVideos.start(props.program, pendingDownload.value.profile, pendingDownload.value.estimate);
        persistSelections();
        Message.success('オフライン保存を開始しました。');
        showPersistenceWarning.value = false;
        pendingDownload.value = null;
        isShown.value = false;
    } catch (error) {
        Message.error(error instanceof Error ? error.message : 'オフライン保存を開始できませんでした。');
    } finally {
        isStarting.value = false;
    }
};

// 永続ストレージの警告を閉じ、保留した画質を破棄
const cancelPersistenceWarning = (): void => {
    showPersistenceWarning.value = false;
    pendingDownload.value = null;
};

watch([selectedQuality, selectedVideoCodec, selectedAudioCodec, is24fpsMode], () => {
    if (is24fpsUnavailable.value === true) is24fpsMode.value = false;
    void refreshEstimate();
});

watch(() => props.show, async (show) => {
    if (show === false) {
        estimateAbortController?.abort();
        estimateAbortController = null;
        estimateRequestSequence++;
        isEstimateLoading.value = false;
        return;
    }
    isPreparing.value = true;
    estimateRequestSequence++;
    estimate.value = null;
    estimateError.value = null;
    selectedQuality.value = isBS4K.value ?
        settingsStore.settings.konomitv_bs4k_offline_video_streaming_quality_for_bs4k :
        settingsStore.settings.konomitv_bs4k_offline_video_streaming_quality;
    selectedVideoCodec.value = isBS4K.value ?
        settingsStore.settings.konomitv_bs4k_offline_video_codec_for_bs4k :
        settingsStore.settings.konomitv_bs4k_offline_video_codec;
    selectedAudioCodec.value = isBS4K.value ?
        settingsStore.settings.konomitv_bs4k_offline_audio_codec_for_bs4k :
        settingsStore.settings.konomitv_bs4k_offline_audio_codec;
    is24fpsMode.value = isBS4K.value ? false :
        settingsStore.settings.konomitv_bs4k_offline_video_24fps_mode;
    try {
        const [, capabilitiesResult, savedVideoResult, activeJobResult] = await Promise.all([
            versionStore.fetchServerVersion(),
            Videos.fetchKonomiTVBS4KPlaybackCapabilities(),
            OfflineVideos.getVideo(props.program.id),
            OfflineVideos.getActiveJobForVideo(props.program.id),
        ]);
        capabilities.value = capabilitiesResult;
        savedVideo.value = savedVideoResult;
        activeDownloadJob.value = activeJobResult;
        normalizeSelections();
        isPreparing.value = false;
        await refreshEstimate();
    } catch (error) {
        Message.error(error instanceof Error ? error.message : 'オフライン保存の準備に失敗しました。');
        isShown.value = false;
    } finally {
        isPreparing.value = false;
    }
});

</script>
<style lang="scss" scoped>

.offline-download-dialog {
    &__card {
        // スマホ縦画面ではダイアログ幅が狭いため、カード内の左右 padding を 4px だけ削る (24px → 20px)
        @include smartphone-vertical {
            :deep(.v-card-title),
            :deep(.v-card-text),
            :deep(.v-card-actions) {
                padding-left: 20px !important;
                padding-right: 20px !important;
            }
        }
    }

    &__title {
        font-size: 17px;
        font-weight: bold;
        line-height: 1.5;
    }

    &__select {
        // v-card-text の 14px / text-darken-1 指定を打ち消し、設定画面の outlined セレクトと同じサイズ感に揃える
        :deep(.v-field) {
            font-size: 16px !important;
            color: rgb(var(--v-theme-text)) !important;
            text-autospace: normal;
        }

        :deep(.v-field__input) {
            min-height: 56px;
            padding-top: 16px;
            padding-bottom: 16px;
            color: rgb(var(--v-theme-text)) !important;
            font-size: 16px !important;
            line-height: 1.5 !important;
            text-autospace: normal;
        }

        :deep(.v-select__selection-text) {
            color: rgb(var(--v-theme-text)) !important;
            text-autospace: normal;
        }

        @include smartphone-horizontal {
            :deep(.v-field),
            :deep(.v-field__input) {
                font-size: 13.5px !important;
            }

            :deep(.v-field__input) {
                min-height: 40px;
                padding-top: 8px;
                padding-bottom: 8px;
            }
        }
    }

    &__switch {
        // 説明文とスイッチを横並びにしつつ、v-switch の余白分を padding-right で確保してカード内に収める
        position: relative;
        padding-right: 56px;

        @include smartphone-vertical {
            padding-right: 52px;
        }

        > div:first-child {
            min-width: 0;
        }

        :deep(.v-switch) {
            position: absolute;
            top: 4px;
            right: 0;
            margin: 0;

            .v-selection-control {
                // flex 配置時のデフォルト margin が右端からはみ出すので打ち消す
                margin-inline-start: 0;
                min-height: 40px;
            }
        }

        &--disabled {
            opacity: 0.5;
        }
    }
}

// 既存保存の警告バナー (ReservationDetailDrawer.vue と同じスタイル)
.warning-banner {
    display: flex;
    align-items: center;
    padding: 12px 16px;
    border-radius: 6px;

    &__icon {
        width: 22px;
        height: 22px;
        margin-right: 8px;
        flex-shrink: 0;
    }

    &__text {
        font-size: 13px;
        font-weight: 500;
        line-height: 1.55;
    }

    &--keyword {
        background-color: rgb(var(--v-theme-warning-darken-3), 0.5);

        .warning-banner__icon {
            color: rgb(var(--v-theme-warning));
        }

        .warning-banner__text {
            color: rgb(var(--v-theme-warning-lighten-1));
        }
    }

    &--normal {
        background-color: rgb(var(--v-theme-info-darken-3), 0.5);

        .warning-banner__icon {
            color: rgb(var(--v-theme-info));
        }

        .warning-banner__text {
            color: rgb(var(--v-theme-info-lighten-1));
        }
    }
}

</style>
