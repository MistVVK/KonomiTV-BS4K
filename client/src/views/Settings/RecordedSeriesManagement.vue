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
            判定済みの録画シリーズを検索し、シリーズ情報と録画ごとのシーズン・話数を編集します。<br>
            録画ごとの所属先は、各録画の再生画面にある「シリーズを訂正」から変更できます。
        </div>

        <v-progress-linear v-if="is_loading" class="mt-5" color="primary" indeterminate rounded />

        <div v-else-if="authorization_error !== null" class="recorded-series-access-state">
            <Icon icon="fluent:shield-error-20-filled" width="28px" />
            <span v-if="authorization_error === 'AdminRequired'">この画面を表示するには管理者権限が必要です。</span>
            <span v-else>ユーザー情報を取得できませんでした。ページを再読み込みしてください。</span>
        </div>

        <template v-else>
            <div class="recorded-series-list-toolbar mt-5">
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
                    class="recorded-series-list-item" role="listitem" @click="openSeriesEditDialog(series)">
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
                    <Icon icon="fluent:edit-20-filled" width="20px" />
                </button>
            </div>
            <v-pagination v-if="series_page_count > 1" class="mt-4" color="primary"
                v-model="series_page" :length="series_page_count" />
        </template>

        <v-dialog v-model="series_edit_dialog" :persistent="is_updating_series" max-width="760" scrollable>
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
                    <v-text-field class="mt-4" color="primary" variant="outlined" label="シリーズ名"
                        maxlength="255" counter hide-details="auto" :error-messages="series_title_error"
                        v-model="editing_series_title" />
                    <v-textarea class="mt-4" color="primary" variant="outlined" label="説明"
                        maxlength="10000" counter rows="4" no-resize hide-details="auto"
                        :error-messages="series_description_error" v-model="editing_series_description" />
                    <div class="settings__item-label mt-3">
                        シリーズ名を変更すると、このシリーズに所属する録画のシリーズ表示名もまとめて更新されます。<br>
                    </div>
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
                            同じ話を別の放送局で録画した場合も、同じシーズン・話数へ割り当てたまま個別の録画として残ります。
                        </div>
                        <div class="recorded-series-episodes__list" role="list">
                            <button v-for="program in episode_assignments.programs"
                                :key="program.recorded_program_id" v-ripple type="button"
                                class="recorded-series-episode-item" role="listitem"
                                @click="openEpisodeAssignmentDialog(program)">
                                <div class="recorded-series-episode-item__episode">
                                    {{formatProgramEpisode(program)}}
                                </div>
                                <div class="recorded-series-episode-item__body">
                                    <strong>{{program.subtitle || program.title}}</strong>
                                    <span>
                                        {{dayjs(program.start_time).format('YYYY/M/D (ddd) HH:mm')}}
                                        ・{{program.channel_name ?? 'チャンネル情報なし'}}
                                    </span>
                                    <small v-if="program.resolution !== null">
                                        {{episodeResolutionStatusLabel(program.resolution.status)}}
                                        <template v-if="program.resolution.source !== null">
                                            ・{{episodeResolutionSourceLabel(program.resolution.source)}}
                                        </template>
                                    </small>
                                </div>
                                <Icon icon="fluent:edit-20-filled" width="19px" />
                            </button>
                        </div>
                    </template>
                </v-card-text>
                <v-card-actions v-if="series_edit_tab === 'SeriesInfo'">
                    <v-spacer />
                    <v-btn variant="text" :disabled="is_updating_series" @click="series_edit_dialog = false">
                        キャンセル
                    </v-btn>
                    <v-btn color="primary" variant="flat" :loading="is_updating_series"
                        :disabled="series_edit_submit_disabled" @click="updateManagementSeries()">
                        更新
                    </v-btn>
                </v-card-actions>
                <v-card-actions v-else>
                    <v-spacer />
                    <v-btn variant="text" @click="series_edit_dialog = false">閉じる</v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>

        <RecordedEpisodeAssignmentDialog v-if="editing_series !== null"
            v-model="episode_assignment_dialog"
            :series-id="editing_series.id"
            :recorded-program-id="editing_episode_program_id"
            :assignment-list="episode_assignments"
            @saved="episodeAssignmentSaved" />
    </SettingsBase>
</template>

<script setup lang="ts">

import { computed, onMounted, onUnmounted, ref, watch } from 'vue';

import RecordedEpisodeAssignmentDialog from '@/components/Videos/Dialogs/RecordedEpisodeAssignmentDialog.vue';
import Message from '@/message';
import RecordedSeries, {
    type IRecordedEpisodeAssignmentList,
    type IRecordedEpisodeAssignmentProgram,
    type IRecordedEpisodeAssignmentResolution,
    type IRecordedSeriesManagementItem,
    type IRecordedSeriesManagementUpdate,
} from '@/services/RecordedSeries';
import useUserStore from '@/stores/UserStore';
import { dayjs } from '@/utils';
import { formatRecordedEpisodeNumber } from '@/utils/RecordedEpisode';
import SettingsBase from '@/views/Settings/Base.vue';


const SERIES_PAGE_SIZE = 30;

const management_series = ref<IRecordedSeriesManagementItem[]>([]);
const series_total = ref(0);
const series_page = ref(1);
const series_search_query = ref<string | null>('');
const is_loading = ref(true);
const is_loading_series = ref(false);
const series_load_failed = ref(false);
const authorization_error = ref<'AdminRequired' | 'UserUnavailable' | null>(null);

// 編集ダイアログでは一覧オブジェクトを直接書き換えず、更新成功後だけサーバーの結果を反映する。
const series_edit_dialog = ref(false);
const editing_series = ref<IRecordedSeriesManagementItem | null>(null);
const editing_series_title = ref('');
const editing_series_description = ref('');
const is_updating_series = ref(false);
const series_edit_tab = ref<'SeriesInfo' | 'Episodes'>('SeriesInfo');

// 録画件数が多い Series もあるため、「録画・話数」タブを初めて開くまで詳細一覧は取得しない。
const episode_assignments = ref<IRecordedEpisodeAssignmentList | null>(null);
const is_loading_episode_assignments = ref(false);
const episode_assignments_load_failed = ref(false);
const episode_assignment_dialog = ref(false);
const editing_episode_program_id = ref<number | null>(null);

const user_store = useUserStore();
let series_search_timer: number | null = null;
let status_polling_timer: number | null = null;
let series_request_sequence = 0;
let episode_assignments_request_sequence = 0;
let is_backfill_running = false;

const series_page_count = computed(() => Math.max(1, Math.ceil(series_total.value / SERIES_PAGE_SIZE)));
const normalized_series_search_query = computed(() => series_search_query.value?.trim() ?? '');
const series_title_error = computed(() => {
    const title = editing_series_title.value.trim();
    if (title === '') return 'シリーズ名を入力してください。';
    if (title.length > 255) return 'シリーズ名は 255 文字以内で入力してください。';
    return '';
});
const series_description_error = computed(() =>
    editing_series_description.value.length > 10000 ? '説明は 10000 文字以内で入力してください。' : '',
);
const series_edit_submit_disabled = computed(() => {
    if (editing_series.value === null || is_updating_series.value) return true;
    if (series_title_error.value !== '' || series_description_error.value !== '') return true;
    return (
        editing_series_title.value.trim() === editing_series.value.title &&
        editing_series_description.value.trim() === editing_series.value.description
    );
});


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

/** 一覧行から編集ダイアログを開き、保存前の入力を独立して保持する。 */
function openSeriesEditDialog(series: IRecordedSeriesManagementItem): void {
    editing_series.value = series;
    editing_series_title.value = series.title;
    editing_series_description.value = series.description;
    series_edit_tab.value = 'SeriesInfo';
    episode_assignments.value = null;
    episode_assignments_load_failed.value = false;
    editing_episode_program_id.value = null;
    series_edit_dialog.value = true;
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

function openEpisodeAssignmentDialog(program: IRecordedEpisodeAssignmentProgram): void {
    editing_episode_program_id.value = program.recorded_program_id;
    episode_assignment_dialog.value = true;
}

/** 共通ダイアログが再取得した一覧をそのまま反映し、失敗時だけ管理画面側で再取得する。 */
function episodeAssignmentSaved(refreshed_assignments: IRecordedEpisodeAssignmentList | null): void {
    if (refreshed_assignments?.series_id === editing_series.value?.id) {
        episode_assignments.value = refreshed_assignments;
    } else {
        void fetchEpisodeAssignments(false);
    }
}

function formatProgramEpisode(program: IRecordedEpisodeAssignmentProgram): string {
    const episode = episode_assignments.value?.episodes.find(candidate => candidate.id === program.series_episode_id);
    if (episode === undefined) return '話数未設定';
    return `S${episode.season_number}・第 ${formatRecordedEpisodeNumber(episode.episode_number)} 話`;
}

function episodeResolutionStatusLabel(status: IRecordedEpisodeAssignmentResolution['status']): string {
    return {
        Pending: '判定待ち',
        Resolved: '確定',
        Unknown: '話数不明',
        NotNumbered: '公式話数なし',
        NeedsReview: '要確認',
        Failed: '判定失敗',
    }[status];
}

function episodeResolutionSourceLabel(
    source: NonNullable<IRecordedEpisodeAssignmentResolution['source']>,
): string {
    return {
        Local: 'ローカル情報',
        EPG: 'EPG',
        WebSearch: 'AI Web 検索',
        Manual: '手動訂正',
        Migration: '既存データ移行',
    }[source];
}

/** シリーズの表示情報を更新し、検索結果と件数をサーバーから同期し直す。 */
async function updateManagementSeries(): Promise<void> {
    if (editing_series.value === null || series_edit_submit_disabled.value) return;

    const update: IRecordedSeriesManagementUpdate = {
        title: editing_series_title.value.trim(),
        description: editing_series_description.value.trim(),
        expected_title: editing_series.value.title,
        expected_description: editing_series.value.description,
    };
    is_updating_series.value = true;
    const result = await RecordedSeries.updateManagementSeries(editing_series.value.id, update);
    is_updating_series.value = false;
    switch (result.type) {
        case 'Success': {
            series_edit_dialog.value = false;
            Message.success('シリーズ情報を更新しました。');
            await fetchManagementSeriesList(false);
            return;
        }
        case 'Stale': {
            const editing_series_id = editing_series.value?.id;
            const latest_series = editing_series_id === undefined ? null :
                await RecordedSeries.fetchManagementSeries(editing_series_id, false);
            if (latest_series !== null && editing_series.value?.id === latest_series.id) {
                editing_series.value = latest_series;
                Message.error('別のタブでシリーズ情報が更新されました。入力内容は保持しています。内容を確認して再度更新してください。');
            } else {
                Message.error('別のタブでシリーズ情報が更新されました。最新情報を取得できないため、入力内容をコピーしてから開き直してください。');
            }
            await fetchManagementSeriesList(false);
            return;
        }
        case 'Conflict': {
            Message.error('同じ名前として扱われる別のシリーズがあるため更新できません。別の名前を入力してください。');
            return;
        }
        case 'Busy': {
            Message.error('シリーズ判定処理の実行中は更新できません。判定処理が完了してから再試行してください。');
            return;
        }
        case 'Error': {
            return;
        }
    }
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
watch(series_page, () => void fetchManagementSeriesList());
watch(series_edit_tab, (tab) => {
    if (tab === 'Episodes' && episode_assignments.value === null && is_loading_episode_assignments.value === false) {
        void fetchEpisodeAssignments();
    }
});
watch(series_edit_dialog, (is_open) => {
    if (is_open) return;
    episode_assignment_dialog.value = false;
    editing_episode_program_id.value = null;
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
    const fetched_user = await user_store.fetchUser();
    const user = fetched_user ?? user_store.user;
    if (user === null) {
        authorization_error.value = 'UserUnavailable';
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
    stopStatusPolling();
    series_request_sequence += 1;
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
    transition: background-color 0.15s ease;
    cursor: pointer;

    &:hover {
        background: rgba(var(--v-theme-primary), 0.13);
    }

    > svg {
        flex-shrink: 0;
        color: rgb(var(--v-theme-text-darken-1));
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
    }
}

</style>
