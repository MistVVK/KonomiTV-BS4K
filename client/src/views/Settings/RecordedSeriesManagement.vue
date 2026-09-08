<template>
    <SettingsBase>
        <h2 class="settings__heading">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:collections-20-filled" width="25px" />
            <span class="ml-3">録画シリーズ管理</span>
        </h2>
        <div class="settings__description">
            録画シリーズとシリーズ未所属の録画を確認します。<br>
            所属や話数の訂正はこの画面では行いません。
        </div>

        <v-progress-linear v-if="is_loading" class="mt-5" color="primary" indeterminate rounded />

        <div v-else-if="authorization_error !== null" class="recorded-series-access-state">
            <Icon icon="fluent:shield-error-20-filled" width="28px" />
            <span v-if="authorization_error === 'LoginRequired'">
                この画面を表示するにはログインが必要です。
                <router-link class="link" to="/login/">ログイン</router-link>
            </span>
            <span v-else-if="authorization_error === 'AdminRequired'">この画面を表示するには管理者権限が必要です。</span>
            <span v-else>ユーザー情報を取得できませんでした。</span>
        </div>

        <template v-else>
            <v-tabs v-model="management_tab" class="mt-5" color="primary" grow>
                <v-tab value="Series">シリーズ一覧</v-tab>
                <v-tab value="Standalone">単発・未所属</v-tab>
            </v-tabs>

            <template v-if="management_tab === 'Series'">
                <div class="recorded-series-list-toolbar mt-4">
                    <v-text-field color="primary" variant="outlined" density="compact" hide-details clearable
                        prepend-inner-icon="mdi-magnify" placeholder="シリーズ名・説明を検索" maxlength="255"
                        v-model="series_search_query" />
                    <span>{{series_total.toLocaleString()}} シリーズ</span>
                    <v-btn color="background-lighten-2" variant="flat" :loading="is_loading_series"
                        @click="fetchManagementSeriesList()">
                        <Icon icon="fluent:arrow-sync-20-filled" class="mr-2" width="20px" />再読み込み
                    </v-btn>
                </div>

                <v-progress-linear v-if="is_loading_series" class="mt-4" color="primary" indeterminate rounded />
                <div v-else-if="series_load_failed" class="recorded-series-list-state">
                    <Icon icon="fluent:error-circle-20-regular" width="27px" />
                    <span>シリーズ一覧を取得できませんでした。</span>
                    <v-btn color="primary" size="small" variant="tonal" @click="fetchManagementSeriesList()">
                        再試行
                    </v-btn>
                </div>
                <div v-else-if="management_series.length === 0" class="recorded-series-list-state">
                    <Icon icon="fluent:search-info-20-regular" width="27px" />
                    <span>{{normalized_series_search_query === '' ? 'シリーズはまだありません。' : '一致するシリーズはありません。'}}</span>
                </div>
                <div v-else class="recorded-series-list mt-4" role="list">
                    <button v-for="series in management_series" :key="series.id" v-ripple type="button"
                        class="recorded-series-list-item" role="listitem" @click="openSeriesViewDialog(series)">
                        <div class="recorded-series-list-item__body">
                            <div class="recorded-series-list-item__heading">
                                <strong>{{series.title}}</strong>
                                <span>録画 {{series.recorded_program_count.toLocaleString()}} 件</span>
                            </div>
                            <p>{{series.description || '説明は設定されていません。'}}</p>
                            <div class="recorded-series-list-item__meta">
                                <span>{{formatSeriesPeriod(series.first_recorded_at, series.last_recorded_at)}}</span>
                                <span>更新 {{dayjs(series.updated_at).format('YYYY/M/D HH:mm')}}</span>
                                <span v-if="series.wikipedia_page_id !== null">
                                    Wikipedia page ID: {{series.wikipedia_page_id}}
                                </span>
                            </div>
                        </div>
                        <Icon icon="fluent:chevron-right-20-filled" width="20px" />
                    </button>
                </div>
                <v-pagination v-if="series_page_count > 1" class="mt-4" color="primary"
                    v-model="series_page" :length="series_page_count" />
            </template>

            <template v-else>
                <div class="recorded-series-list-toolbar mt-4">
                    <v-text-field color="primary" variant="outlined" density="compact" hide-details clearable
                        prepend-inner-icon="mdi-magnify" placeholder="録画タイトルを検索" maxlength="255"
                        v-model="standalone_search_query" />
                    <span>{{standalone_total.toLocaleString()}} 件</span>
                    <v-btn color="background-lighten-2" variant="flat" :loading="is_loading_standalone"
                        @click="fetchStandalonePrograms()">
                        <Icon icon="fluent:arrow-sync-20-filled" class="mr-2" width="20px" />再読み込み
                    </v-btn>
                </div>
                <div class="settings__item-label mt-3">
                    シリーズ未所属の録画です。所属の変更はこの画面では行いません。
                </div>

                <v-progress-linear v-if="is_loading_standalone" class="mt-4" color="primary" indeterminate rounded />
                <div v-else-if="standalone_load_failed" class="recorded-series-list-state">
                    <Icon icon="fluent:error-circle-20-regular" width="27px" />
                    <span>シリーズ未所属の録画一覧を取得できませんでした。</span>
                    <v-btn color="primary" size="small" variant="tonal" @click="fetchStandalonePrograms()">
                        再試行
                    </v-btn>
                </div>
                <div v-else-if="standalone_programs.length === 0" class="recorded-series-list-state">
                    <Icon icon="fluent:checkmark-circle-20-regular" width="27px" />
                    <span>{{normalized_standalone_search_query === '' ?
                        'シリーズ未所属の録画はありません。' : '一致する録画はありません。'}}</span>
                </div>
                <div v-else class="recorded-series-list mt-4" role="list">
                    <div v-for="program in standalone_programs" :key="program.recorded_program_id"
                        class="recorded-series-list-item recorded-series-list-item--static" role="listitem">
                        <div class="recorded-series-list-item__body">
                            <div class="recorded-series-list-item__heading">
                                <strong>{{program.subtitle || program.title}}</strong>
                                <span>未所属</span>
                            </div>
                            <p>{{program.title}}</p>
                            <div class="recorded-series-list-item__meta">
                                <span>{{dayjs(program.start_time).format('YYYY/M/D (ddd) HH:mm')}}</span>
                                <span>{{program.channel_name ?? 'チャンネル情報なし'}}</span>
                            </div>
                        </div>

                    </div>
                </div>
                <v-pagination v-if="standalone_page_count > 1" class="mt-4" color="primary"
                    v-model="standalone_page" :length="standalone_page_count" />
            </template>
        </template>

        <v-dialog v-model="series_view_dialog" max-width="760" scrollable>
            <v-card v-if="editing_series !== null" class="recorded-series-dialog recorded-series-edit-dialog">
                <v-card-title>{{editing_series.title}}</v-card-title>
                <v-tabs v-model="series_edit_tab" class="recorded-series-edit-dialog__tabs" color="primary" grow>
                    <v-tab value="SeriesInfo">シリーズ情報</v-tab>
                    <v-tab value="Episodes">録画・話数</v-tab>
                </v-tabs>
                <!-- `scrollable` は v-card 直下の v-card-text だけをスクロール領域にする。 -->
                <v-card-text v-if="series_edit_tab === 'SeriesInfo'">
                    <div class="recorded-series-edit-dialog__summary">
                        <span>シリーズ ID {{editing_series.id}}</span>
                        <span>録画 {{editing_series.recorded_program_count.toLocaleString()}} 件</span>
                        <span v-if="editing_series.wikipedia_page_id !== null">
                            Wikipedia page ID: {{editing_series.wikipedia_page_id}}
                        </span>
                    </div>
                    <div class="settings__item-heading mt-4">シリーズ名</div>
                    <div class="settings__item-label">{{editing_series.title}}</div>
                    <div class="settings__item-heading mt-4">説明</div>
                    <div class="settings__item-label">{{editing_series.description || '説明は設定されていません。'}}</div>
                </v-card-text>
                <v-card-text v-else class="recorded-series-episodes">
                    <v-progress-linear v-if="is_loading_episode_assignments" color="primary"
                        indeterminate rounded />
                    <div v-else-if="episode_assignments_load_failed"
                        class="recorded-series-episodes__state">
                        <Icon icon="fluent:error-circle-20-regular" width="27px" />
                        <span>録画と話数を取得できませんでした。</span>
                        <v-btn color="primary" size="small" variant="tonal"
                            @click="fetchEpisodeAssignments()">再試行</v-btn>
                    </div>
                    <div v-else-if="episode_assignments?.programs.length === 0"
                        class="recorded-series-episodes__state">
                        <Icon icon="fluent:video-clip-off-20-regular" width="27px" />
                        <span>再生可能な録画はありません。</span>
                    </div>
                    <template v-else-if="episode_assignments !== null">
                        <div class="recorded-series-episodes__description">
                            同じ話を別の放送局で録画した場合も、同じシーズン・話数の個別の録画として残ります。<br>
                            所属や話数の訂正はこの画面では行いません。
                        </div>
                        <div class="recorded-series-episodes__list" role="list">
                            <div v-for="program in episode_assignments.programs"
                                :key="program.recorded_program_id"
                                class="recorded-series-episode-item" role="listitem">
                                <div class="recorded-series-episode-item__episode">
                                    {{formatProgramEpisode(program)}}
                                </div>
                                <div class="recorded-series-episode-item__body">
                                    <strong>{{program.subtitle || program.title}}</strong>
                                    <span>
                                        {{dayjs(program.start_time).format('YYYY/M/D (ddd) HH:mm')}}
                                        ・{{program.channel_name ?? 'チャンネル情報なし'}}
                                    </span>
                                    <small v-if="program.resolution !== null"
                                        class="recorded-series-episode-item__resolution">
                                        {{episodeResolutionStatusLabel(program.resolution.status)}}
                                        <template v-if="program.resolution.lookup_outcome !== null">
                                            ・{{episodeLookupOutcomeLabel(program.resolution.lookup_outcome)}}
                                        </template>
                                        <template v-if="program.resolution.source !== null">
                                            ・{{episodeResolutionSourceLabel(program.resolution.source)}}
                                        </template>
                                        <template v-if="program.resolution.confidence !== null">
                                            ・信頼度 {{Math.round(program.resolution.confidence * 100)}}%
                                        </template>
                                    </small>
                                    <small v-if="program.resolution !== null"
                                        class="recorded-series-episode-item__reason"
                                        :title="recordedEpisodeResolutionReason(program.resolution)">
                                        {{recordedEpisodeResolutionReason(program.resolution)}}
                                    </small>
                                </div>

                            </div>
                        </div>
                    </template>
                </v-card-text>
                <v-card-actions>
                    <v-spacer />
                    <v-btn variant="text" @click="series_view_dialog = false">閉じる</v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>
    </SettingsBase>
</template>

<script setup lang="ts">

import { computed, onMounted, onUnmounted, ref, watch } from 'vue';

import RecordedSeries, {
    type IRecordedEpisodeAssignmentList,
    type IRecordedEpisodeAssignmentProgram,
    type IRecordedSeriesManagementItem,
    type IRecordedSeriesStandaloneProgram,
} from '@/services/RecordedSeries';
import useUserStore from '@/stores/UserStore';
import Utils, { dayjs } from '@/utils';
import {
    formatRecordedEpisodeLabel,
    formatRecordedUnnumberedEpisodeLabel,
} from '@/utils/RecordedEpisode';
import {
    episodeLookupOutcomeLabel,
    episodeResolutionSourceLabel,
    episodeResolutionStatusLabel,
    recordedEpisodeResolutionReason,
} from '@/utils/RecordedEpisodeResolution';
import SettingsBase from '@/views/Settings/Base.vue';


const SERIES_PAGE_SIZE = 30;

const management_tab = ref<'Series' | 'Standalone'>('Series');
const management_series = ref<IRecordedSeriesManagementItem[]>([]);
const series_total = ref(0);
const series_page = ref(1);
const series_search_query = ref<string | null>('');
const is_loading = ref(true);
const is_loading_series = ref(false);
const series_load_failed = ref(false);
const authorization_error = ref<'LoginRequired' | 'AdminRequired' | 'UserUnavailable' | null>(null);

// 閲覧ダイアログは一覧オブジェクトを参照するだけにし、サーバーへ書き戻さない。
const series_view_dialog = ref(false);
const editing_series = ref<IRecordedSeriesManagementItem | null>(null);
const series_edit_tab = ref<'SeriesInfo' | 'Episodes'>('SeriesInfo');

// 録画件数が多い Series もあるため、「録画・話数」タブを初めて開くまで詳細一覧は取得しない。
const episode_assignments = ref<IRecordedEpisodeAssignmentList | null>(null);
const is_loading_episode_assignments = ref(false);
const episode_assignments_load_failed = ref(false);

const standalone_programs = ref<IRecordedSeriesStandaloneProgram[]>([]);
const standalone_total = ref(0);
const standalone_page = ref(1);
const standalone_search_query = ref<string | null>('');
const is_loading_standalone = ref(false);
const standalone_load_failed = ref(false);

const user_store = useUserStore();
let series_search_timer: number | null = null;
let standalone_search_timer: number | null = null;
let status_polling_timer: number | null = null;
let series_request_sequence = 0;
let standalone_request_sequence = 0;
let episode_assignments_request_sequence = 0;
let is_backfill_running = false;

const series_page_count = computed(() => Math.max(1, Math.ceil(series_total.value / SERIES_PAGE_SIZE)));
const standalone_page_count = computed(() => Math.max(1, Math.ceil(standalone_total.value / SERIES_PAGE_SIZE)));
const normalized_series_search_query = computed(() => series_search_query.value?.trim() ?? '');
const normalized_standalone_search_query = computed(() => standalone_search_query.value?.trim() ?? '');


/** 検索条件とページに対応する管理用シリーズ一覧を取得する。 */
async function fetchManagementSeriesList(show_error = true): Promise<void> {
    const request_sequence = ++series_request_sequence;
    is_loading_series.value = true;
    series_load_failed.value = false;
    const result = await RecordedSeries.fetchManagementSeriesList(
        normalized_series_search_query.value,
        series_page.value,
        SERIES_PAGE_SIZE,
        show_error,
    );
    if (request_sequence !== series_request_sequence) return;

    is_loading_series.value = false;
    if (result === null) {
        series_load_failed.value = true;
        return;
    }
    management_series.value = result.items;
    series_total.value = result.total;

    const last_page = Math.max(1, Math.ceil(result.total / SERIES_PAGE_SIZE));
    if (series_page.value > last_page) series_page.value = last_page;
}

/** 一覧行から閲覧ダイアログを開く。 */
function openSeriesViewDialog(series: IRecordedSeriesManagementItem): void {
    editing_series.value = series;
    series_edit_tab.value = 'SeriesInfo';
    episode_assignments.value = null;
    episode_assignments_load_failed.value = false;
    series_view_dialog.value = true;
}

/** 選択中の Series に属する録画と構造化話数だけを、タブ表示時に遅延取得する。 */
async function fetchEpisodeAssignments(show_error = true): Promise<void> {
    const series_id = editing_series.value?.id;
    if (series_id === undefined) return;

    const request_sequence = ++episode_assignments_request_sequence;
    is_loading_episode_assignments.value = true;
    episode_assignments_load_failed.value = false;
    const result = await RecordedSeries.fetchEpisodeAssignments(series_id, show_error);
    if (
        request_sequence !== episode_assignments_request_sequence ||
        editing_series.value?.id !== series_id
    ) {
        return;
    }

    is_loading_episode_assignments.value = false;
    episode_assignments.value = result;
    episode_assignments_load_failed.value = result === null;
}

/** 管理画面向けに、シリーズ未所属の再生可能録画を取得する。 */
async function fetchStandalonePrograms(show_error = true): Promise<void> {
    const request_sequence = ++standalone_request_sequence;
    is_loading_standalone.value = true;
    standalone_load_failed.value = false;
    const result = await RecordedSeries.fetchStandalonePrograms(
        normalized_standalone_search_query.value,
        standalone_page.value,
        SERIES_PAGE_SIZE,
        show_error,
    );
    if (request_sequence !== standalone_request_sequence) return;

    is_loading_standalone.value = false;
    if (result === null) {
        standalone_load_failed.value = true;
        return;
    }
    standalone_programs.value = result.items;
    standalone_total.value = result.total;
    const last_page = Math.max(1, Math.ceil(result.total / SERIES_PAGE_SIZE));
    if (standalone_page.value > last_page) standalone_page.value = last_page;
}

function formatProgramEpisode(program: IRecordedEpisodeAssignmentProgram): string {
    const episode = episode_assignments.value?.episodes.find(candidate => candidate.id === program.series_episode_id);
    if (episode !== undefined) return formatRecordedEpisodeLabel(episode.season_number, episode.episode_number);
    const resolution = program.resolution;
    if (resolution?.status === 'NotNumbered' || resolution?.status === 'NoPublishedNumber') {
        return formatRecordedUnnumberedEpisodeLabel(resolution.season_number, resolution.status);
    }
    return '話数未設定';
}

/** 管理画面を開いた時点で実行中の一括判定だけを追跡し、完了後に一覧を更新する。 */
async function refreshBackfillStatus(): Promise<void> {
    const status = await RecordedSeries.fetchStatus(false);
    if (status === null) return;
    if (is_backfill_running && status.is_running === false) {
        is_backfill_running = false;
        stopStatusPolling();
        await fetchManagementSeriesList(false);
    }
}

function startStatusPolling(): void {
    if (status_polling_timer !== null) return;
    status_polling_timer = window.setInterval(() => void refreshBackfillStatus(), 3000);
}

function stopStatusPolling(): void {
    if (status_polling_timer === null) return;
    window.clearInterval(status_polling_timer);
    status_polling_timer = null;
}

watch(series_search_query, () => {
    if (series_search_timer !== null) window.clearTimeout(series_search_timer);
    series_search_timer = window.setTimeout(() => {
        series_search_timer = null;
        if (series_page.value !== 1) {
            series_page.value = 1;
        } else {
            void fetchManagementSeriesList();
        }
    }, 300);
});
watch(standalone_search_query, () => {
    if (standalone_search_timer !== null) window.clearTimeout(standalone_search_timer);
    standalone_search_timer = window.setTimeout(() => {
        standalone_search_timer = null;
        if (standalone_page.value !== 1) {
            standalone_page.value = 1;
        } else {
            void fetchStandalonePrograms();
        }
    }, 300);
});
watch(series_page, () => void fetchManagementSeriesList());
watch(standalone_page, () => void fetchStandalonePrograms());
watch(management_tab, (tab) => {
    if (tab === 'Standalone' && standalone_programs.value.length === 0 && is_loading_standalone.value === false) {
        void fetchStandalonePrograms();
    }
});
watch(series_edit_tab, (tab) => {
    if (tab === 'Episodes' && episode_assignments.value === null && is_loading_episode_assignments.value === false) {
        void fetchEpisodeAssignments();
    }
});
watch(series_view_dialog, (is_open) => {
    if (is_open) return;
    episode_assignments_request_sequence += 1;
    is_loading_episode_assignments.value = false;
});

function formatSeriesPeriod(first_recorded_at: string | null, last_recorded_at: string | null): string {
    if (first_recorded_at === null || last_recorded_at === null) return '録画期間なし';
    const first_date = dayjs(first_recorded_at);
    const last_date = dayjs(last_recorded_at);
    if (first_date.isSame(last_date, 'day')) return first_date.format('YYYY/M/D');
    return `${first_date.format('YYYY/M/D')} ～ ${last_date.format('YYYY/M/D')}`;
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

    // 実行中かを先に確定させてから一覧を取得し、完了直前の古い一覧を取り逃がさない。
    const status = await RecordedSeries.fetchStatus(false);
    if (status?.is_running === true) {
        is_backfill_running = true;
    }
    await fetchManagementSeriesList();
    if (is_backfill_running) startStatusPolling();
    is_loading.value = false;
});

onUnmounted(() => {
    if (series_search_timer !== null) window.clearTimeout(series_search_timer);
    if (standalone_search_timer !== null) window.clearTimeout(standalone_search_timer);
    stopStatusPolling();
    series_request_sequence += 1;
    standalone_request_sequence += 1;
    episode_assignments_request_sequence += 1;
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

.recorded-series-list-toolbar {
    display: grid;
    align-items: center;
    grid-template-columns: minmax(0, 1fr) auto auto;
    gap: 12px;

    > span {
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 13px;
        white-space: nowrap;
    }
}

.recorded-series-list-state {
    display: flex;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    min-height: 180px;
    gap: 10px;
    color: rgb(var(--v-theme-text-darken-1));
    font-size: 13px;
    text-align: center;
}

.recorded-series-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.recorded-series-list-item {
    display: flex;
    align-items: center;
    width: 100%;
    min-width: 0;
    padding: 13px 14px;
    gap: 12px;
    border-radius: 8px;
    color: rgb(var(--v-theme-text));
    text-align: left;
    background: rgb(var(--v-theme-background-lighten-2));
    transition: background-color 0.15s ease;
    cursor: pointer;

    &:hover {
        background: rgb(var(--v-theme-primary) / 13%);
    }

    &--static {
        cursor: default;

        &:hover {
            background: rgb(var(--v-theme-background-lighten-2));
        }
    }

    > svg {
        flex-shrink: 0;
        color: rgb(var(--v-theme-text-darken-1));
    }

    &__body {
        min-width: 0;
        flex: 1;
    }

    &__heading {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;

        strong {
            overflow-wrap: anywhere;
        }

        span {
            flex-shrink: 0;
            padding: 2px 7px;
            border-radius: 999px;
            color: rgb(var(--v-theme-text-darken-1));
            background: rgb(var(--v-theme-background-lighten-1));
            font-size: 11.5px;
        }
    }

    p {
        display: -webkit-box;
        margin: 5px 0 0;
        overflow: hidden;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 12.5px;
        line-height: 1.5;
        overflow-wrap: anywhere;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
    }

    &__meta {
        display: flex;
        flex-wrap: wrap;
        margin-top: 6px;
        gap: 4px 14px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 11.5px;
    }
}

.recorded-series-dialog {
    background: rgb(var(--v-theme-background-lighten-1));
}

.recorded-series-edit-dialog {
    &__tabs {
        flex: none;
    }

    &__summary {
        display: flex;
        flex-wrap: wrap;
        gap: 6px 14px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 12.5px;
    }
}

.recorded-series-episodes {
    min-height: 0;

    &__state {
        display: flex;
        align-items: center;
        justify-content: center;
        flex-direction: column;
        min-height: 220px;
        gap: 10px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 13px;
        text-align: center;
    }

    &__description {
        margin-bottom: 12px;
        padding: 9px 11px;
        border-radius: 6px;
        color: rgb(var(--v-theme-text-darken-1));
        background: rgba(var(--v-theme-primary), 0.09);
        font-size: 11.5px;
        line-height: 1.55;
    }

    &__list {
        display: flex;
        flex-direction: column;
        gap: 7px;
    }
}

.recorded-series-episode-item {
    display: flex;
    align-items: center;
    width: 100%;
    min-width: 0;
    padding: 10px 11px;
    gap: 11px;
    border-radius: 7px;
    color: rgb(var(--v-theme-text));
    text-align: left;
    background: rgb(var(--v-theme-background));

    &__actions {
        display: flex;
        flex-shrink: 0;
        flex-direction: column;
        gap: 6px;
    }

    &__episode {
        display: flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
        width: 105px;
        min-height: 38px;
        padding: 5px 7px;
        border-radius: 5px;
        color: rgb(var(--v-theme-primary-readable));
        background: rgba(var(--v-theme-primary), 0.1);
        font-size: 11.5px;
        font-weight: bold;
        text-align: center;
    }

    &__body {
        display: flex;
        flex-direction: column;
        min-width: 0;
        flex-grow: 1;

        strong,
        span,
        small {
            overflow: hidden;
            white-space: nowrap;
            text-overflow: ellipsis;
        }

        strong {
            font-size: 13px;
        }

        span,
        small {
            margin-top: 2px;
            color: rgb(var(--v-theme-text-darken-1));
            font-size: 11px;
        }

        .recorded-series-episode-item__reason {
            color: rgb(var(--v-theme-text));
        }
    }
}

@include smartphone-vertical {
    .recorded-series-list-toolbar {
        grid-template-columns: minmax(0, 1fr) auto;

        > :first-child {
            grid-column: 1 / -1;
        }
    }

    .recorded-series-list-item {
        padding: 12px;

        &__heading {
            align-items: flex-start;
            flex-direction: column;
            gap: 6px;
        }
    }

    .recorded-series-episode-item {
        align-items: flex-start;
        flex-wrap: wrap;

        &__episode {
            width: auto;
            min-height: 0;
        }

        &__body {
            width: calc(100% - 90px);
        }

        &__actions {
            width: 100%;
            flex-direction: row;
            justify-content: flex-end;
        }
    }
}

</style>
