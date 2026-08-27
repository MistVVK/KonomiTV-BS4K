<template>
    <SettingsBase>
        <h2 class="settings__heading">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:code-20-regular" width="26px" />
            <span class="ml-3">コーデック対応</span>
        </h2>
        <div class="settings__description">
            この画面は、<strong>この端末 (ブラウザ) とこのサーバー</strong>でどの映像・音声コーデックの処理経路を利用できるかを確認します。<br>
            ブラウザ診断の結果は「このブラウザで利用できる映像処理経路」であり、GPU の能力の断定ではありません。<br>
            診断結果はこの画面の表示だけに使い、端末やサーバーへ保存しません (サーバー診断の結果はサーバーのメモリ内キャッシュに保持されます)。<br>
        </div>

        <div class="settings__content">
            <v-tabs v-model="active_tab" class="codec-support-tabs" color="primary" density="comfortable"
                @update:model-value="handleTabChange">
                <v-tab value="browser">ブラウザ</v-tab>
                <v-tab value="server">サーバー</v-tab>
            </v-tabs>

            <!-- スマートフォンで横スクロール可能な表を操作した際にタブ自体が切り替わらないよう、
                 v-window のタッチスワイプは無効化する。タブ切り替えは v-tabs のみで行う。 -->
            <v-window v-model="active_tab" :touch="false">
                <v-window-item value="browser">
                    <div class="codec-support-card">
                        <div class="settings__item-heading">このブラウザの診断</div>
                        <div class="settings__item-label">
                            MediaCapabilities・MediaSource / ManagedMediaSource・WebCodecs・WebGL をこの端末内で照会し、
                            KonomiTV-BS4K の映像・音声コーデックの対応を確認します。<br>
                            診断はブラウザにより数秒から数分かかります。応答しないブラウザ API は個別に打ち切り、残りの構成は最後まで診断します。結果はこの画面の表示だけに使います。
                        </div>
                        <div class="codec-support-actions">
                            <v-btn color="primary" variant="flat" :loading="browser_in_progress"
                                :disabled="browser_in_progress" @click="startBrowserDiagnostics">
                                診断を実行
                            </v-btn>
                            <v-btn v-if="browser_result !== null" color="secondary" variant="tonal"
                                @click="copyBrowserResult">
                                <Icon icon="fluent:copy-20-regular" width="16px" />
                                <span class="ml-2">{{ browser_copied ? 'コピーしました' : 'JSON をコピー' }}</span>
                            </v-btn>
                        </div>
                    </div>

                    <div v-if="browser_result !== null" class="codec-support-card">
                        <div class="settings__item-heading">環境</div>
                        <div class="codec-support-environment">
                            <div><span>エンジン</span><strong>{{ browser_result.environment.engine }}</strong></div>
                            <div><span>WebGL</span><strong>{{ browser_result.environment.webgl_version ?? '利用不可' }}</strong></div>
                            <div><span>GPU (参考)</span><strong>{{ browser_result.environment.webgl_renderer ?? '取得できず' }}</strong></div>
                            <div><span>WebGPU</span><strong>{{ browser_result.environment.webgpu === true ? '利用可能' : '利用不可' }}</strong></div>
                            <div class="codec-support-environment--wide">
                                <span>API</span>
                                <strong>
                                    <template v-for="(available, api_name) in browser_result.environment.apis" :key="api_name">
                                        <span :class="['codec-support-api', available === true ? 'codec-support-api--on' : 'codec-support-api--off']">{{ api_name }}</span>
                                    </template>
                                </strong>
                            </div>
                        </div>
                    </div>

                    <div v-if="browser_result !== null && konomitv_summary.length > 0" class="codec-support-card">
                        <div class="settings__item-heading">KonomiTV-BS4K 主要構成 (1080p60)</div>
                        <div class="settings__item-label">
                            KonomiTV-BS4K の再生で使う AVC / HEVC / VP9 / AV1 の 1080p60 8bit 構成の対応状況をまとめます。
                        </div>
                        <div class="codec-support-summary">
                            <div v-for="row in konomitv_summary" :key="row.codec" class="codec-support-summary__row">
                                <span>{{ row.codec }}</span>
                                <span :class="`codec-support-badge ${browserSupportClass(row.browser_support)}`">{{ browserSupportLabel(row.browser_support) }}</span>
                                <span :class="`codec-support-badge ${hardwareEstimateClass(row.hardware_estimate)}`">{{ hardwareEstimateLabel(row.hardware_estimate) }}</span>
                            </div>
                        </div>
                    </div>

                    <div v-if="browser_result !== null" class="codec-support-card">
                        <div class="settings__item-heading">映像 decode</div>
                        <div class="settings__item-label">
                            KonomiTV-BS4K 本線と比較対象の構成について、ブラウザでの再生可否と HW / SW の推定を表示します。<br>
                            「KonomiTV 再生経路」は実際に再生時に使う MediaSource / ManagedMediaSource 判定をそのまま表示しています。
                        </div>
                        <div class="codec-support-table-scroll">
                            <table class="codec-support-table">
                                <thead>
                                    <tr>
                                        <th>コーデック / プロファイル</th>
                                        <th>検査構成</th>
                                        <th>ブラウザ対応</th>
                                        <th>HW アクセラレーション</th>
                                        <th>Power Efficient</th>
                                        <th>WebCodecs</th>
                                        <th>KonomiTV 再生経路</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr v-for="row in browser_result.video_decode" :key="row.id">
                                        <td>
                                            <span :class="['codec-support-table__code', row.used_by_konomitv_bs4k === true ? 'codec-support-table__code--konomitv' : '']">{{ row.label }}</span>
                                            <span v-if="row.used_by_konomitv_bs4k === false" class="codec-support-table__note">比較用</span>
                                        </td>
                                        <td>{{ row.tested_configuration }} / {{ row.bit_depth }}bit</td>
                                        <td>
                                            <span :class="`codec-support-badge ${browserSupportClass(row.browser_support)}`" :title="evidenceLabel(row.browser_evidence)">{{ browserSupportLabel(row.browser_support) }}</span>
                                        </td>
                                        <td>
                                            <span :class="`codec-support-badge ${hardwareEstimateClass(row.hardware_estimate)}`">{{ hardwareEstimateLabel(row.hardware_estimate) }}</span>
                                        </td>
                                        <td>
                                            <template v-if="row.power_efficient === null">-</template>
                                            <template v-else>{{ row.power_efficient === true ? 'true' : 'false' }}</template>
                                        </td>
                                        <td>
                                            <span :class="`codec-support-badge ${webCodecsClass(row.web_codecs_prefer_hardware)}`" title="prefer-hardware">{{ webCodecsLabel(row.web_codecs_prefer_hardware) }}</span>
                                            <span :class="`codec-support-badge ${webCodecsClass(row.web_codecs_no_preference)}`" title="no preference">{{ webCodecsLabel(row.web_codecs_no_preference) }}</span>
                                        </td>
                                        <td>
                                            <template v-if="row.konomitv_playback === null">-</template>
                                            <span v-else :class="`codec-support-badge ${row.konomitv_playback === true ? 'codec-support-badge--success' : 'codec-support-badge--error'}`">{{ row.konomitv_playback === true ? '可能' : '不可' }}</span>
                                        </td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                    </div>

                    <div v-if="browser_result !== null" class="codec-support-card">
                        <div class="settings__item-heading">映像 encode (診断用)</div>
                        <div class="settings__item-label">
                            WebCodecs の VideoEncoder によるエンコード能力の診断です。KonomiTV-BS4K の再生には直接使われません。
                        </div>
                        <div class="codec-support-table-scroll">
                            <table class="codec-support-table">
                                <thead>
                                    <tr>
                                        <th>コーデック / プロファイル</th>
                                        <th>検査構成</th>
                                        <th>prefer-hardware</th>
                                        <th>prefer-software</th>
                                        <th>no preference</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr v-for="row in browser_result.video_encode" :key="row.id">
                                        <td>{{ row.label }}</td>
                                        <td>{{ row.tested_configuration }}</td>
                                        <td>
                                            <span :class="`codec-support-badge ${webCodecsClass(row.web_codecs_prefer_hardware)}`">{{ webCodecsLabel(row.web_codecs_prefer_hardware) }}</span>
                                        </td>
                                        <td>
                                            <span :class="`codec-support-badge ${webCodecsClass(row.web_codecs_prefer_software)}`">{{ webCodecsLabel(row.web_codecs_prefer_software) }}</span>
                                        </td>
                                        <td>
                                            <span :class="`codec-support-badge ${webCodecsClass(row.web_codecs_no_preference)}`">{{ webCodecsLabel(row.web_codecs_no_preference) }}</span>
                                        </td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                    </div>

                    <div v-if="browser_result !== null" class="codec-support-card">
                        <div class="settings__item-heading">音声</div>
                        <div class="settings__item-label">
                            音声はブラウザ対応と WebCodecs の decode / encode のみ表示します (HW / SW の推定は行いません)。<br>
                            KonomiTV-BS4K 本線は AAC-LC と Opus です。
                        </div>
                        <div class="codec-support-table-scroll">
                            <table class="codec-support-table">
                                <thead>
                                    <tr>
                                        <th>コーデック</th>
                                        <th>ブラウザ対応</th>
                                        <th>WebCodecs decode</th>
                                        <th>WebCodecs encode</th>
                                        <th>KonomiTV 再生経路</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr v-for="row in browser_result.audio" :key="row.id">
                                        <td>
                                            <span :class="['codec-support-table__code', row.used_by_konomitv_bs4k === true ? 'codec-support-table__code--konomitv' : '']">{{ row.label }}</span>
                                            <span v-if="row.used_by_konomitv_bs4k === false" class="codec-support-table__note">比較用</span>
                                        </td>
                                        <td>
                                            <span :class="`codec-support-badge ${browserSupportClass(row.browser_support)}`" :title="evidenceLabel(row.browser_evidence)">{{ browserSupportLabel(row.browser_support) }}</span>
                                        </td>
                                        <td>
                                            <span :class="`codec-support-badge ${webCodecsClass(row.web_codecs_decode)}`">{{ webCodecsLabel(row.web_codecs_decode) }}</span>
                                        </td>
                                        <td>
                                            <span :class="`codec-support-badge ${webCodecsClass(row.web_codecs_encode)}`">{{ webCodecsLabel(row.web_codecs_encode) }}</span>
                                        </td>
                                        <td>
                                            <template v-if="row.konomitv_playback === null">-</template>
                                            <span v-else :class="`codec-support-badge ${row.konomitv_playback === true ? 'codec-support-badge--success' : 'codec-support-badge--error'}`">{{ row.konomitv_playback === true ? '可能' : '不可' }}</span>
                                        </td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                </v-window-item>

                <v-window-item value="server">
                    <div v-if="user_loaded === false" class="codec-support-card">
                        <div class="settings__item-heading">サーバー診断</div>
                        <div class="settings__item-label">アカウント情報を読み込んでいます…</div>
                    </div>
                    <div v-else-if="is_admin === false" class="codec-support-card">
                        <div class="settings__item-heading">サーバー診断</div>
                        <div class="settings__item-label">
                            サーバー診断は FFmpeg / QSV / NVENC / AMF の実 probe を実行するため、
                            <strong>管理者アカウントのみ</strong>開始できます。<br>
                            管理者に、この画面のサーバータブから診断を開始してもらってください。
                        </div>
                    </div>

                    <template v-else>
                        <div class="codec-support-card">
                            <div class="settings__item-heading">サーバー診断</div>
                            <div class="settings__item-label">
                                サーバー上で FFmpeg / QSV / NVENC / AMF の実 probe を実行し、
                                各 CPU / GPU の映像・音声コーデックの decode / encode 能力を確認します。<br>
                                映像の実 probe は 1440x1080 60fps で行います。4K / 8K の最大解像度対応を判定するものではありません。<br>
                                外部プロセスを多数起動するため、数分かかることがあります。結果はサーバーのメモリ内キャッシュに保持されます。
                            </div>
                            <div class="codec-support-actions">
                                <v-btn color="primary" variant="flat" :loading="server_request_in_progress"
                                    :disabled="server_request_in_progress || server_job?.status === 'Running'"
                                    @click="startServerProbe(false)">
                                    診断を開始
                                </v-btn>
                                <v-btn color="secondary" variant="tonal" :loading="server_request_in_progress"
                                    :disabled="server_request_in_progress || server_job?.status === 'Running'"
                                    @click="startServerProbe(true)">
                                    再診断 (キャッシュを無視)
                                </v-btn>
                                <v-btn color="secondary" variant="tonal" :loading="server_cancel_in_progress"
                                    :disabled="server_cancel_in_progress || server_job?.status !== 'Running'"
                                    @click="cancelServerProbe">
                                    キャンセル
                                </v-btn>
                                <v-btn v-if="server_job !== null" color="secondary" variant="tonal"
                                    @click="copyServerResult">
                                    <Icon icon="fluent:copy-20-regular" width="16px" />
                                    <span class="ml-2">{{ server_copied ? 'コピーしました' : 'JSON をコピー' }}</span>
                                </v-btn>
                            </div>
                            <div v-if="server_job !== null" class="codec-support-progress">
                                <div class="codec-support-progress__label">
                                    <span v-if="server_job.status === 'Idle'">実行中の診断はありません</span>
                                    <span v-else-if="server_job.status === 'Running'">診断中 ({{ Math.round(server_job.progress * 100) }}%)</span>
                                    <span v-else-if="server_job.status === 'Completed'">診断完了</span>
                                    <span v-else-if="server_job.status === 'Failed'">診断中にエラーが発生しました</span>
                                </div>
                                <v-progress-linear v-if="server_job.status === 'Running'" class="mt-2" color="primary"
                                    :model-value="server_job.progress * 100" rounded />
                            </div>
                        </div>

                        <div v-for="device in server_job?.devices ?? []" :key="device.id" class="codec-support-card">
                            <div class="settings__item-heading">
                                {{ device.label }}
                                <span class="codec-support-device-kind">{{ device.kind === 'CPU' ? 'CPU' : 'GPU' }} / {{ device.vendor }}</span>
                            </div>
                            <div class="codec-support-table-scroll">
                                <table class="codec-support-table">
                                    <thead>
                                        <tr>
                                            <th>コーデック</th>
                                            <th>decode</th>
                                            <th>encode</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        <tr v-for="(capability, index) in device.capabilities" :key="index">
                                            <td>
                                                <span :class="['codec-support-table__code', capability.used_by_konomitv_bs4k === true ? 'codec-support-table__code--konomitv' : '']">
                                                    {{ formatServerCapability(capability) }}
                                                </span>
                                                <span v-if="capability.used_by_konomitv_bs4k === false" class="codec-support-table__note">比較用</span>
                                            </td>
                                            <td>
                                                <span :class="`codec-support-badge ${browserSupportClass(capability.decode.status)}`"
                                                    :title="`${evidenceLabel(capability.decode.evidence)}${capability.decode.reason_code !== null ? ` / ${capability.decode.reason_code}` : ''}${capability.decode.tested_configuration !== null ? ` / ${capability.decode.tested_configuration}` : ''}`">
                                                    {{ browserSupportLabel(capability.decode.status) }}
                                                </span>
                                                <span v-if="capability.decode.backend !== null" class="codec-support-table__backend">{{ capability.decode.backend }}</span>
                                                <span v-if="capability.decode.tested_configuration !== null" class="codec-support-table__configuration">
                                                    {{ capability.decode.tested_configuration }}
                                                </span>
                                            </td>
                                            <td>
                                                <span :class="`codec-support-badge ${browserSupportClass(capability.encode.status)}`"
                                                    :title="`${evidenceLabel(capability.encode.evidence)}${capability.encode.reason_code !== null ? ` / ${capability.encode.reason_code}` : ''}${capability.encode.tested_configuration !== null ? ` / ${capability.encode.tested_configuration}` : ''}`">
                                                    {{ browserSupportLabel(capability.encode.status) }}
                                                </span>
                                                <span v-if="capability.encode.backend !== null" class="codec-support-table__backend">{{ capability.encode.backend }}</span>
                                                <span v-if="capability.encode.tested_configuration !== null" class="codec-support-table__configuration">
                                                    {{ capability.encode.tested_configuration }}
                                                </span>
                                            </td>
                                        </tr>
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </template>
                </v-window-item>
            </v-window>
        </div>
    </SettingsBase>
</template>

<script lang="ts" setup>

import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import type { IKonomiTVBS4KCodecSupportServerCapability, IKonomiTVBS4KCodecSupportServerJob } from '@/services/KonomiTVBS4KCodecSupport';

import Message from '@/message';
import KonomiTVBS4KCodecSupport from '@/services/KonomiTVBS4KCodecSupport';
import useUserStore from '@/stores/UserStore';
import {
    runKonomiTVBS4KBrowserCodecSupportDiagnostics,
    type IKonomiTVBS4KBrowserCodecSupportResult,
    type KonomiTVBS4KBrowserEvidence,
    type KonomiTVBS4KBrowserHardwareEstimate,
    type KonomiTVBS4KBrowserSupportStatus,
    type KonomiTVBS4KBrowserWebCodecsProbe,
} from '@/utils/KonomiTVBS4KBrowserCodecSupport';
import SettingsBase from '@/views/Settings/Base.vue';


const route = useRoute();
const router = useRouter();
const user_store = useUserStore();
const user_loaded = ref(false);
const is_admin = computed(() => user_store.user?.is_admin === true);

// タブは route.hash の #browser / #server で同期し、URL だけを見て現在のタブが分かるようにする。
// 永続化しない (計画の仕様)。
const active_tab = ref<'browser' | 'server'>('browser');

function applyRouteHash(): void {
    active_tab.value = route.hash === '#server' ? 'server' : 'browser';
}

function handleTabChange(tab: unknown): void {
    // v-tabs の update:model-value は unknown 型で届くため、既知の値以外は無視する。
    if (tab !== 'browser' && tab !== 'server') {
        return;
    }
    router.replace({path: route.path, hash: tab === 'server' ? '#server' : ''});
}

// ブラウザ診断の状態。結果はコンポーネント状態にだけ保持し、LocalStorage には保存しない。
const browser_result = ref<IKonomiTVBS4KBrowserCodecSupportResult | null>(null);
const browser_in_progress = ref(false);
const browser_copied = ref(false);
let browser_abort_controller: AbortController | null = null;
let browser_copy_timer: ReturnType<typeof setTimeout> | null = null;
let page_active = true;
let server_fetch_generation = 0;
let server_fetch_in_flight = false;
let server_polling_error_count = 0;

// サーバー診断の状態。実行中は 3 秒間隔で状態をポーリングして部分結果を表示する。
const server_job = ref<IKonomiTVBS4KCodecSupportServerJob | null>(null);
const server_request_in_progress = ref(false);
const server_cancel_in_progress = ref(false);
const server_copied = ref(false);
let server_polling_timer: ReturnType<typeof setInterval> | null = null;
let server_copy_timer: ReturnType<typeof setTimeout> | null = null;

/**
 * KonomiTV-BS4K 主要構成のサマリー。
 * 各 codec の 1080p60 8bit 行 (再生の最低構成) を AVC / HEVC / VP9 / AV1 の順で表示する。
 */
const konomitv_summary = computed(() => {
    if (browser_result.value === null) {
        return [];
    }
    const codecs: readonly string[] = ['AVC', 'HEVC', 'VP9', 'AV1'];
    const summary: Array<{
        codec: string;
        browser_support: KonomiTVBS4KBrowserSupportStatus;
        hardware_estimate: KonomiTVBS4KBrowserHardwareEstimate;
    }> = [];
    for (const codec of codecs) {
        const row = browser_result.value.video_decode.find(entry =>
            entry.used_by_konomitv_bs4k === true &&
            entry.codec === codec &&
            entry.bit_depth === 8 &&
            entry.tested_configuration === '1440x1080 60fps',
        );
        if (row !== undefined) {
            // サマリーは実再生経路を優先する。MSE が不可なら「対応」と出さない。
            summary.push({
                codec,
                browser_support: row.konomitv_playback === false ? 'Unsupported' : row.browser_support,
                hardware_estimate: row.konomitv_playback === false ? 'NotApplicable' : row.hardware_estimate,
            });
        }
    }
    return summary;
});

async function startBrowserDiagnostics(): Promise<void> {
    if (browser_in_progress.value === true) {
        browser_abort_controller?.abort();
    }
    browser_abort_controller?.abort();
    const abort_controller = new AbortController();
    browser_abort_controller = abort_controller;
    browser_in_progress.value = true;
    browser_copied.value = false;
    try {
        browser_result.value = await runKonomiTVBS4KBrowserCodecSupportDiagnostics(abort_controller.signal);
    } catch (error) {
        if (abort_controller.signal.aborted === true || (error instanceof DOMException && error.name === 'AbortError')) {
            return;
        }
        // 診断は best-effort。失敗してもページ全体は使う。
        Message.error('ブラウザのコーデック対応診断に失敗しました。');
    } finally {
        if (browser_abort_controller === abort_controller) {
            browser_in_progress.value = false;
        }
    }
}

async function copyBrowserResult(): Promise<void> {
    if (browser_result.value === null) {
        return;
    }
    if (await copyToClipboard(JSON.stringify(browser_result.value, null, 2)) === false || page_active === false) {
        return;
    }
    browser_copied.value = true;
    if (browser_copy_timer !== null) {
        clearTimeout(browser_copy_timer);
    }
    browser_copy_timer = setTimeout(() => {
        browser_copied.value = false;
        browser_copy_timer = null;
    }, 2000);
}

async function copyServerResult(): Promise<void> {
    if (server_job.value === null) {
        return;
    }
    if (await copyToClipboard(JSON.stringify(server_job.value, null, 2)) === false || page_active === false) {
        return;
    }
    server_copied.value = true;
    if (server_copy_timer !== null) {
        clearTimeout(server_copy_timer);
    }
    server_copy_timer = setTimeout(() => {
        server_copied.value = false;
        server_copy_timer = null;
    }, 2000);
}

async function copyToClipboard(text: string): Promise<boolean> {
    try {
        await navigator.clipboard.writeText(text);
        Message.success('JSON をコピーしました。');
        return true;
    } catch {
        // clipboard API は Secure Context が必要。KonomiTV は HTTPS で動くため通常はここには来ない。
        Message.error('コピーに失敗しました。');
        return false;
    }
}

async function fetchServerState(options: {force?: boolean; is_polling?: boolean} = {}): Promise<void> {
    if (page_active === false || is_admin.value === false) { // page_active は unmount で false になる
        return;
    }
    if (server_fetch_in_flight === true) {
        if (options.force !== true) {
            // ポーリングなどの通常 GET は前の GET が終わるまで重ねない
            return;
        }
        // mutation 直後の強制取得は、実行中の古い GET の結果適用を世代を進めて破棄する。
        // これがないと、開始直前に発行した GET が POST 済みの Running 状態を Idle で上書きする。
        server_fetch_generation += 1;
    }
    const generation = server_fetch_generation;
    server_fetch_in_flight = true;
    try {
        // ポーリング中の失敗で Snackbar を積み上げないよう、通知は通常取得時だけ表示する
        const job = await KonomiTVBS4KCodecSupport.getState(options.is_polling !== true);
        // await 中に mutation / unmount があると generation が進む。古い結果は適用しない。
        if (generation !== server_fetch_generation) {
            return;
        }
        if (job === null) {
            if (options.is_polling === true) {
                server_polling_error_count += 1;
                if (server_polling_error_count >= 3) {
                    // ネットワーク障害などで状態取得が続けて失敗した場合は自動更新を止め、1 回だけ通知する
                    stopServerPolling();
                    Message.warning('サーバー診断の状態取得に失敗し続けているため、自動更新を停止しました。再診断で再開できます。');
                }
            }
            return;
        }
        server_polling_error_count = 0;
        server_job.value = job;
        syncServerPolling();
    } finally {
        if (generation === server_fetch_generation) {
            server_fetch_in_flight = false;
        }
    }
}

function stopServerPolling(): void {
    if (server_polling_timer !== null) {
        clearInterval(server_polling_timer);
        server_polling_timer = null;
    }
}

function syncServerPolling(): void {
    // 実行中ジョブがある場合だけポーリングを回す。ページを離れるときにタイマーを止める。
    if (page_active === false) {
        return;
    }
    if (server_job.value?.status === 'Running' && server_polling_timer === null) {
        server_polling_timer = setInterval(() => {
            void fetchServerState({is_polling: true});
        }, 3000);
    } else if (server_job.value?.status !== 'Running' && server_polling_timer !== null) {
        stopServerPolling();
    }
}

async function startServerProbe(force: boolean): Promise<void> {
    if (server_request_in_progress.value === true) {
        return;
    }
    // mutation の開始時点で世代を進め、先行する GET の古い結果が後から適用されないようにする
    server_fetch_generation += 1;
    server_request_in_progress.value = true;
    server_copied.value = false;
    try {
        const job = await KonomiTVBS4KCodecSupport.startProbe(force);
        if (job !== null) {
            server_job.value = job;
            // POST 応答だけでポーリングを開始する。直後の GET が他の GET と重なって
            // skip されても Running 状態がタイマーなしで残らないようにするため。
            syncServerPolling();
        }
        // 202 (新規開始 / 合流) でも 200 (キャッシュ再利用) でも、最新状態を強制取得する。
        await fetchServerState({force: true});
    } finally {
        server_request_in_progress.value = false;
    }
}

async function cancelServerProbe(): Promise<void> {
    if (server_cancel_in_progress.value === true) {
        return;
    }
    server_fetch_generation += 1;
    server_cancel_in_progress.value = true;
    try {
        await KonomiTVBS4KCodecSupport.cancelProbe();
        await fetchServerState({force: true});
    } finally {
        server_cancel_in_progress.value = false;
    }
}

onMounted(async () => {
    applyRouteHash();
    // 直接 URL を開くと UserStore.user は null のままなので、管理者判定の前に取得する。
    // fetchUser() はアイコン取得だけが失敗した場合も null を返すが、ユーザー本体は Store に残る。
    await user_store.fetchUser();
    if (page_active === false) {
        return;
    }
    // 取得完了までは user_loaded が false のため、管理者へ「管理者のみ」と誤表示しない
    user_loaded.value = true;
    void fetchServerState();
});

watch(() => route.hash, () => {
    applyRouteHash();
});

onBeforeUnmount(() => {
    page_active = false;
    server_fetch_generation += 1;
    browser_abort_controller?.abort();
    browser_abort_controller = null;
    stopServerPolling();
    if (browser_copy_timer !== null) {
        clearTimeout(browser_copy_timer);
        browser_copy_timer = null;
    }
    if (server_copy_timer !== null) {
        clearTimeout(server_copy_timer);
        server_copy_timer = null;
    }
});

function browserSupportLabel(status: KonomiTVBS4KBrowserSupportStatus): string {
    switch (status) {
        case 'Supported':
            return '対応';
        case 'Likely':
            return '推定対応';
        case 'Unsupported':
            return '非対応';
        case 'Unknown':
            return '不明';
    }
}

function browserSupportClass(status: KonomiTVBS4KBrowserSupportStatus): string {
    switch (status) {
        case 'Supported':
            return 'codec-support-badge--success';
        case 'Likely':
            return 'codec-support-badge--info';
        case 'Unsupported':
            return 'codec-support-badge--error';
        case 'Unknown':
            return 'codec-support-badge--muted';
    }
}

function hardwareEstimateLabel(estimate: KonomiTVBS4KBrowserHardwareEstimate): string {
    switch (estimate) {
        case 'HardwareLikely':
            return 'HW 推定';
        case 'SoftwareLikely':
            return 'SW 推定';
        case 'Unknown':
            return '不明';
        case 'NotApplicable':
            return '対象外';
    }
}

function hardwareEstimateClass(estimate: KonomiTVBS4KBrowserHardwareEstimate): string {
    switch (estimate) {
        case 'HardwareLikely':
            return 'codec-support-badge--success';
        case 'SoftwareLikely':
            return 'codec-support-badge--warning';
        case 'Unknown':
            return 'codec-support-badge--muted';
        case 'NotApplicable':
            return 'codec-support-badge--muted';
    }
}

function webCodecsLabel(probe: KonomiTVBS4KBrowserWebCodecsProbe): string {
    switch (probe) {
        case 'Supported':
            return '対応';
        case 'Unsupported':
            return '非対応';
        case 'Unavailable':
            return 'N/A';
    }
}

function webCodecsClass(probe: KonomiTVBS4KBrowserWebCodecsProbe): string {
    switch (probe) {
        case 'Supported':
            return 'codec-support-badge--success';
        case 'Unsupported':
            return 'codec-support-badge--error';
        case 'Unavailable':
            return 'codec-support-badge--muted';
    }
}

function evidenceLabel(evidence: KonomiTVBS4KBrowserEvidence | 'VerifiedProbe' | 'Driver' | 'Binary' | 'ProbeFailed' | 'Unavailable'): string {
    switch (evidence) {
        case 'MediaCapabilities':
            return 'MediaCapabilities';
        case 'MediaSource':
            return 'MediaSource / ManagedMediaSource';
        case 'CanPlayType':
            return 'canPlayType';
        case 'None':
            return '判定材料なし';
        case 'VerifiedProbe':
            return '実 probe 確認';
        case 'Driver':
            return 'ドライバ情報';
        case 'Binary':
            return 'バイナリ情報';
        case 'ProbeFailed':
            return 'probe 失敗';
        case 'Unavailable':
            return '利用不可';
    }
}

function formatServerCapability(capability: IKonomiTVBS4KCodecSupportServerCapability): string {
    const codec_labels: Readonly<Record<string, string>> = {
        avc: 'AVC (H.264)',
        hevc: 'HEVC (H.265)',
        vp9: 'VP9',
        av1: 'AV1',
        mpeg1: 'MPEG-1 Video',
        mpeg2: 'MPEG-2 Video',
        mpeg4: 'MPEG-4 Part 2',
        vc1: 'VC-1',
        aac_lc: 'AAC-LC',
        he_aac: 'HE-AAC',
        aac_latm: 'AAC LATM',
        mp2: 'MP2',
        mp3: 'MP3',
        ac3: 'AC-3',
        eac3: 'E-AC-3',
        opus: 'Opus',
        vorbis: 'Vorbis',
        flac: 'FLAC',
        lpcm: 'LPCM',
    };
    const parts = [codec_labels[capability.codec] ?? capability.codec];
    if (capability.profile !== null) {
        parts.push(capability.profile);
    }
    if (capability.bit_depth !== null) {
        parts.push(`${capability.bit_depth}bit`);
    }
    return parts.join(' ');
}

</script>

<style lang="scss" scoped>

.codec-support-tabs {
    margin-top: 24px;
    background: transparent !important;
    @include smartphone-vertical {
        margin-top: 16px;
    }
}

.codec-support-card {
    padding: 16px 0 8px;
    border-bottom: 1px solid rgb(var(--v-theme-background-lighten-2));
}

.codec-support-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-top: 16px;
}

.codec-support-progress {
    margin-top: 16px;
    .codec-support-progress__label {
        font-size: 14px;
    }
}

.codec-support-environment {
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
        font-size: 15px;
        word-break: break-all;
    }
    .codec-support-environment--wide {
        grid-column: span 2;
    }
    @include smartphone-vertical {
        grid-template-columns: 1fr;
        .codec-support-environment--wide {
            grid-column: span 1;
        }
    }
}

.codec-support-api {
    display: inline-block;
    padding: 2px 6px;
    margin: 2px 4px 2px 0;
    border-radius: 4px;
    font-size: 11px;
    font-weight: normal;
    &.codec-support-api--on {
        background: rgb(var(--v-theme-success) / 15%);
        color: rgb(var(--v-theme-success));
    }
    &.codec-support-api--off {
        background: rgb(var(--v-theme-background-lighten-2));
        color: rgb(var(--v-theme-text-darken-1));
    }
}

.codec-support-summary {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px 16px;
    margin-top: 12px;
    @include smartphone-vertical {
        grid-template-columns: 1fr;
    }
    .codec-support-summary__row {
        display: flex;
        align-items: center;
        gap: 8px;
        span:first-child {
            width: 5em;
            font-weight: bold;
        }
    }
}

.codec-support-device-kind {
    margin-left: 8px;
    padding: 2px 8px;
    border-radius: 4px;
    background: rgb(var(--v-theme-background-lighten-2));
    font-size: 12px;
    font-weight: normal;
    color: rgb(var(--v-theme-text-darken-1));
}

.codec-support-table-scroll {
    margin-top: 12px;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
}

.codec-support-table {
    width: 100%;
    min-width: 720px;
    border-collapse: collapse;
    font-size: 13px;
    thead {
        th {
            padding: 8px 10px;
            border-bottom: 2px solid rgb(var(--v-theme-background-lighten-2));
            color: rgb(var(--v-theme-text-darken-1));
            font-size: 12px;
            font-weight: bold;
            text-align: left;
            white-space: nowrap;
        }
    }
    tbody {
        td {
            padding: 8px 10px;
            border-bottom: 1px solid rgb(var(--v-theme-background-lighten-2));
            vertical-align: middle;
        }
    }
}

.codec-support-table__code {
    font-weight: bold;
    &.codec-support-table__code--konomitv {
        color: rgb(var(--v-theme-primary));
    }
}

.codec-support-table__note {
    margin-left: 6px;
    padding: 1px 6px;
    border-radius: 4px;
    background: rgb(var(--v-theme-background-lighten-2));
    font-size: 11px;
    color: rgb(var(--v-theme-text-darken-1));
}

.codec-support-table__backend {
    margin-left: 6px;
    font-size: 11px;
    color: rgb(var(--v-theme-text-darken-1));
}

.codec-support-table__configuration {
    display: block;
    margin-top: 3px;
    font-size: 11px;
    color: rgb(var(--v-theme-text-darken-1));
}

.codec-support-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 12px;
    font-weight: bold;
    margin-right: 4px;
    &.codec-support-badge--success {
        background: rgb(var(--v-theme-success) / 15%);
        color: rgb(var(--v-theme-success));
    }
    &.codec-support-badge--info {
        background: rgb(var(--v-theme-info) / 15%);
        color: rgb(var(--v-theme-info));
    }
    &.codec-support-badge--warning {
        background: rgb(var(--v-theme-warning) / 15%);
        color: rgb(var(--v-theme-warning));
    }
    &.codec-support-badge--error {
        background: rgb(var(--v-theme-error) / 15%);
        color: rgb(var(--v-theme-error));
    }
    &.codec-support-badge--muted {
        background: rgb(var(--v-theme-background-lighten-2));
        color: rgb(var(--v-theme-text-darken-1));
    }
}

</style>
