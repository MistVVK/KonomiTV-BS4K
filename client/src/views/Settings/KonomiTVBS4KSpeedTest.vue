<template>
    <SettingsBase>
        <h2 class="settings__heading">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:top-speed-20-filled" width="26px" />
            <span class="ml-3">サーバー接続速度</span>
        </h2>
        <div class="settings__description">
            この画面はインターネット速度ではなく、<strong>この端末からこの KonomiTV-BS4K サーバーまで</strong>の速度を測ります。<br>
            測定は視聴と同じ同一オリジン HTTPS 経路を使い、計測本体は下り最大約 10 秒・5 本、上り最大約 10 秒・3 本です。<br>
            下りは約 100MiB、上りは約 20MiB の塊を使い、高ビットレート映像の連続受信と約 6 秒の 4K 相当セグメントに近づけています。<br>
            計測本体の前には、接続を安定させるため下り約 1.5 秒・上り約 3 秒の準備時間があります。<br>
            下り測定中はブラウザが最大約 500MiB を同時に保持することがあります。視聴中に測ると帯域が競合します。<br>
            逆プロキシが圧縮や buffering を行うと、実測値が視聴時とずれることがあります。<br>
        </div>

        <div class="settings__content">
            <div class="speed-test-card">
                <div class="settings__item-heading">測定開始</div>
                <div class="settings__item-label">
                    モバイル通信では短時間に数百 MiB 以上を送受信することがあります。<br>
                    測定結果はこの画面の表示だけに使い、サーバーや端末へ保存しません。
                </div>
                <div class="speed-test-actions">
                    <v-btn color="primary" variant="flat" :disabled="is_busy || session_request_in_progress || cleanup_in_progress"
                        :loading="ui_state === 'Preparing'"
                        @click="startMeasurement">
                        測定開始
                    </v-btn>
                    <v-btn v-if="is_busy" color="secondary" variant="tonal" @click="cancelMeasurement">
                        キャンセル
                    </v-btn>
                </div>
            </div>

            <div v-if="ui_state !== 'Idle'" class="speed-test-card">
                <div class="settings__item-heading">測定状況</div>
                <div class="speed-test-phase">{{ phase_label }}</div>
                <v-progress-linear class="mt-3" color="primary" :model-value="progress_percent" :indeterminate="ui_state === 'Preparing'" rounded />
            </div>

            <div v-if="result !== null" class="speed-test-card">
                <div class="settings__item-heading">測定結果</div>
                <div class="speed-test-metrics">
                    <div><span>下り</span><strong>{{ formatMbps(result.download_mbps) }}</strong></div>
                    <div><span>上り</span><strong>{{ formatMbps(result.upload_mbps) }}</strong></div>
                    <div><span>RTT</span><strong>{{ formatMs(result.rtt_ms) }}</strong></div>
                    <div><span>jitter</span><strong>{{ formatMs(result.jitter_ms) }}</strong></div>
                </div>
            </div>

            <div v-if="result !== null && quality_thresholds.length > 0" class="speed-test-card">
                <div class="settings__item-heading">推奨画質</div>
                <div class="settings__item-label">
                    下り実測値と、再生映像 bitrate 正本（映像最大 + 音声）から作った必要 Mbps をこの端末内で比較しています。<br>
                    設定コーデックは再生・画質の希望値であり、再生開始時の実効 codec とは限りません。測定後も設定は自動では変わりません。<br>
                    測定値が十分でも、サーバーの encoder 能力や端末の decode 能力は別条件です。
                </div>
                <div class="speed-test-quality-grid">
                    <section>
                        <h3>通常放送</h3>
                        <p class="speed-test-configured-codec">設定コーデック {{ configuredCodecLabel('Terrestrial') }}</p>
                        <ul>
                            <li v-for="codec in codecs" :key="`terrestrial-${codec}`">
                                <span>{{ codec }}</span>
                                <strong>{{ recommendedLabel('Terrestrial', codec) }}</strong>
                            </li>
                        </ul>
                    </section>
                    <section>
                        <h3>BS4K</h3>
                        <p class="speed-test-configured-codec">設定コーデック {{ configuredCodecLabel('BS4K') }}</p>
                        <ul>
                            <li v-for="codec in codecs" :key="`bs4k-${codec}`">
                                <span>{{ codec }}</span>
                                <strong>{{ recommendedLabel('BS4K', codec) }}</strong>
                            </li>
                        </ul>
                    </section>
                </div>
            </div>

            <div class="speed-test-card speed-test-card--license">
                <div class="settings__item-heading">LibreSpeed</div>
                <div class="settings__item-label">
                    測定エンジンは LibreSpeed の固定 Worker です。UI と backend は KonomiTV-BS4K の独自実装です。<br>
                    作者: Federico Dossena / ライセンス: GNU LGPL-3.0 / commit:
                    <code>{{ worker_commit }}</code><br>
                    ライセンス全文はサードパーティーライセンスに含めています。
                </div>
                <div class="speed-test-license-links">
                    <a class="link" href="/vendor/librespeed/speedtest_worker.js" target="_blank" rel="noopener noreferrer">Worker</a>
                    <a class="link" href="/api/version/third-party-licenses" target="_blank" rel="noopener noreferrer">サードパーティーライセンス</a>
                    <a class="link" :href="upstream_source_url" target="_blank" rel="noopener noreferrer">upstream source</a>
                </div>
            </div>
        </div>
    </SettingsBase>
</template>

<script lang="ts" setup>

import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { onBeforeRouteLeave } from 'vue-router';

import KonomiTVBS4KSpeedTest from '@/services/KonomiTVBS4KSpeedTest';
import useSettingsStore from '@/stores/SettingsStore';
import Utils from '@/utils';
import {
    KONOMITV_BS4K_SPEED_TEST_WORKER_COMMIT,
    KonomiTVBS4KSpeedTestWorkerController,
    MapKonomiTVBS4KSpeedTestState,
    ResolveKonomiTVBS4KSpeedTestResult,
    SelectKonomiTVBS4KSpeedTestRecommendedQuality,
    type IKonomiTVBS4KSpeedTestQualityThreshold,
    type IKonomiTVBS4KSpeedTestResult,
    type KonomiTVBS4KSpeedTestBroadcastType,
    type KonomiTVBS4KSpeedTestCodec,
    type KonomiTVBS4KSpeedTestUiState,
} from '@/utils/KonomiTVBS4KSpeedTestWorker';
import SettingsBase from '@/views/Settings/Base.vue';


const codecs: readonly KonomiTVBS4KSpeedTestCodec[] = ['AVC', 'HEVC', 'VP9', 'AV1'];
const worker_commit = KONOMITV_BS4K_SPEED_TEST_WORKER_COMMIT;
const upstream_source_url =
    `https://github.com/librespeed/speedtest/blob/${KONOMITV_BS4K_SPEED_TEST_WORKER_COMMIT}/speedtest_worker.js`;

const settings_store = useSettingsStore();
const ui_state = ref<KonomiTVBS4KSpeedTestUiState>('Idle');
const progress_percent = ref(0);
const result = ref<IKonomiTVBS4KSpeedTestResult | null>(null);
const quality_thresholds = ref<IKonomiTVBS4KSpeedTestQualityThreshold[]>([]);
const session_active = ref(false);
const session_request_in_progress = ref(false);
const cleanup_in_progress = ref(false);
const measurement_generation = ref(0);
const worker_controller = new KonomiTVBS4KSpeedTestWorkerController();
let cleanup_promise: Promise<void> | null = null;

const is_busy = computed(() =>
    ui_state.value === 'Preparing' ||
    ui_state.value === 'Pinging' ||
    ui_state.value === 'Downloading' ||
    ui_state.value === 'Uploading',
);

const phase_label = computed(() => {
    switch (ui_state.value) {
        case 'Preparing':
            return '準備中';
        case 'Pinging':
            return 'Ping / jitter を測定しています';
        case 'Downloading':
            return '下りを測定しています';
        case 'Uploading':
            return '上りを測定しています';
        case 'Completed':
            return '測定が完了しました';
        case 'Cancelled':
            return '測定をキャンセルしました';
        case 'Error':
            return '測定に失敗しました';
        default:
            return '';
    }
});

function formatMbps(value: number): string {
    return `${value.toFixed(2)} Mbps`;
}

function formatMs(value: number): string {
    return `${value.toFixed(2)} ms`;
}

function recommendedLabel(
    broadcast_type: KonomiTVBS4KSpeedTestBroadcastType,
    codec: KonomiTVBS4KSpeedTestCodec,
): string {
    if (result.value === null) {
        return '未測定';
    }
    const recommended = SelectKonomiTVBS4KSpeedTestRecommendedQuality(
        quality_thresholds.value,
        broadcast_type,
        codec,
        result.value.download_mbps,
    );
    return recommended?.quality ?? '安定視聴が難しい可能性';
}

function configuredCodecLabel(broadcast_type: KonomiTVBS4KSpeedTestBroadcastType): string {
    const settings = settings_store.settings;
    const current_codec = broadcast_type === 'BS4K' ?
        settings.konomitv_bs4k_playback_video_codec_for_bs4k :
        settings.konomitv_bs4k_playback_video_codec;
    return current_codec.toUpperCase();
}

function cleanupMeasurement(): Promise<void> {
    worker_controller.cleanup();
    if (cleanup_promise !== null) {
        return cleanup_promise;
    }
    if (session_active.value === false) {
        return Promise.resolve();
    }
    cleanup_in_progress.value = true;
    cleanup_promise = KonomiTVBS4KSpeedTest.deleteSession().finally(() => {
        session_active.value = false;
        cleanup_in_progress.value = false;
        cleanup_promise = null;
    });
    return cleanup_promise;
}

async function startMeasurement(): Promise<void> {
    // 二重クリックや測定中の再入場では session を増やさない。
    if (
        is_busy.value === true ||
        session_request_in_progress.value === true ||
        cleanup_in_progress.value === true ||
        worker_controller.isRunning() === true
    ) {
        return;
    }
    const generation = measurement_generation.value + 1;
    measurement_generation.value = generation;
    result.value = null;
    progress_percent.value = 0;
    ui_state.value = 'Preparing';
    session_request_in_progress.value = true;
    const session = await KonomiTVBS4KSpeedTest.createSession();
    session_request_in_progress.value = false;
    // 画面離脱後に届いた session は即座に破棄し、枠を残さない。
    if (generation !== measurement_generation.value) {
        if (session !== null) {
            session_active.value = true;
            await cleanupMeasurement();
        }
        return;
    }
    if (session === null) {
        ui_state.value = 'Error';
        return;
    }
    session_active.value = true;
    quality_thresholds.value = session.quality_thresholds;
    const started = worker_controller.start((status) => {
        if (generation !== measurement_generation.value) {
            return;
        }
        const mapped = MapKonomiTVBS4KSpeedTestState(status.testState);
        if (mapped === null) {
            ui_state.value = 'Error';
            void cleanupMeasurement();
            return;
        }
        if (mapped === 'Pinging') {
            progress_percent.value = status.pingProgress * 100;
        } else if (mapped === 'Downloading') {
            progress_percent.value = status.dlProgress * 100;
        } else if (mapped === 'Uploading') {
            progress_percent.value = status.ulProgress * 100;
        }
        if (mapped === 'Completed') {
            const resolved = ResolveKonomiTVBS4KSpeedTestResult(status);
            if (resolved === null) {
                ui_state.value = 'Error';
            } else {
                result.value = resolved;
                ui_state.value = 'Completed';
            }
            void cleanupMeasurement();
            return;
        }
        if (mapped === 'Cancelled') {
            ui_state.value = 'Cancelled';
            void cleanupMeasurement();
            return;
        }
        ui_state.value = mapped;
    }, () => {
        if (generation !== measurement_generation.value) {
            return;
        }
        ui_state.value = 'Error';
        void cleanupMeasurement();
    });
    if (started === false) {
        ui_state.value = 'Error';
        await cleanupMeasurement();
    }
}

function cancelMeasurement(): void {
    // 準備中は Worker がまだ無いので、世代を進めて createSession 完了後の開始を捨てる。
    measurement_generation.value += 1;
    worker_controller.abort();
    void cleanupMeasurement();
    ui_state.value = 'Cancelled';
}

function releaseSessionKeepAlive(): void {
    // このタブが測定枠を持っているときだけ DELETE する。別タブの測定を落とさない。
    if (session_active.value === false) {
        return;
    }
    void fetch(`${Utils.api_base_url}/konomitv-bs4k/speed-test/session`, {
        method: 'DELETE',
        credentials: 'include',
        keepalive: true,
    });
}

function abandonMeasurement(): void {
    // 進行中の startMeasurement() が後から session を受け取っても破棄する。
    measurement_generation.value += 1;
    worker_controller.cleanup();
    if (session_active.value === true) {
        releaseSessionKeepAlive();
        session_active.value = false;
    }
}

function handlePageHide(): void {
    const was_busy = is_busy.value;
    // BFCache では component が unmount されないため、このタブの世代と Worker も明示的に終了する。
    abandonMeasurement();
    if (was_busy) {
        ui_state.value = 'Cancelled';
    }
}

onMounted(() => {
    window.addEventListener('pagehide', handlePageHide);
});

onBeforeRouteLeave(() => {
    abandonMeasurement();
});

onBeforeUnmount(() => {
    window.removeEventListener('pagehide', handlePageHide);
    abandonMeasurement();
});

</script>

<style lang="scss" scoped>

.speed-test-card {
    padding: 16px 0 8px;
    border-bottom: 1px solid rgb(var(--v-theme-background-lighten-2));
}

.speed-test-actions {
    display: flex;
    gap: 12px;
    margin-top: 16px;
}

.speed-test-phase {
    margin-top: 8px;
    font-size: 14px;
}

.speed-test-metrics {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px 16px;
    margin-top: 12px;
    span {
        display: block;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 13px;
    }
    strong {
        font-size: 20px;
    }
}

.speed-test-quality-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
    margin-top: 16px;
    @include smartphone-vertical {
        grid-template-columns: 1fr;
    }
    h3 {
        margin: 0 0 4px;
        font-size: 15px;
    }
    .speed-test-configured-codec {
        margin: 0 0 10px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 13px;
    }
    ul {
        margin: 0;
        padding: 0;
        list-style: none;
    }
    li {
        display: flex;
        align-items: baseline;
        gap: 8px;
        margin-bottom: 6px;
        span {
            width: 4.5em;
            color: rgb(var(--v-theme-text-darken-1));
        }
    }
}

.speed-test-license-links {
    display: flex;
    flex-wrap: wrap;
    gap: 12px 16px;
    margin-top: 12px;
}

</style>
