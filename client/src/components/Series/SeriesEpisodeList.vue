<template>
    <div class="series-episode-list">
        <div v-if="isLoading" class="series-episode-list__state">
            <v-progress-circular color="primary" indeterminate size="28" width="3" />
            <span>シリーズ詳細を取得しています…</span>
        </div>
        <div v-else-if="loadFailed" class="series-episode-list__state">
            <span>シリーズ詳細を取得できませんでした。</span>
        </div>
        <template v-else-if="series !== null">
            <div v-if="series.bangumi_subject_id !== null || series.tmdb_id !== null" class="series-episode-list__bangumi">
                <img v-if="series.bangumi_subject_id !== null || series.tmdb_id !== null" :key="series.id"
                    class="series-episode-list__cover" :src="`${Utils.api_base_url}/series/${series.id}/poster`" alt=""
                    @error="hideBrokenSeriesCover">
                <div class="series-episode-list__bangumi-main">
                    <div class="series-episode-list__bangumi-title">
                        {{series.bangumi_subject_name || series.tmdb_name || series.title}}
                    </div>
                    <a v-if="series.bangumi_subject_id !== null" class="series-episode-list__bangumi-link"
                        :href="`https://bgm.tv/subject/${series.bangumi_subject_id}`" target="_blank" rel="noopener">
                        bgm.tv で開く
                    </a>
                    <a v-if="series.tmdb_id !== null && series.tmdb_media_type !== null"
                        class="series-episode-list__bangumi-link"
                        :href="`https://www.themoviedb.org/${series.tmdb_media_type}/${series.tmdb_id}`"
                        target="_blank" rel="noopener">
                        TMDb で開く
                    </a>
                    <p v-if="showExternalSummary && (series.bangumi_subject_summary || series.tmdb_overview)"
                        class="series-episode-list__summary">
                        {{series.bangumi_subject_summary || series.tmdb_overview}}
                    </p>
                    <button v-if="series.bangumi_subject_summary || series.tmdb_overview" type="button"
                        class="series-episode-list__summary-toggle" :aria-expanded="showExternalSummary"
                        @click="showExternalSummary = !showExternalSummary">
                        {{showExternalSummary ? '概要を閉じる' : '概要を表示'}}
                    </button>
                </div>
            </div>

            <div class="series-episode-list__table-wrap">
                <table class="series-episode-list__table">
                    <thead>
                        <tr>
                            <th class="series-episode-list__corner">放送局</th>
                            <template v-for="column in columns" :key="column.key">
                                <th v-if="isStructuredColumn(column)">
                                    <span class="series-episode-list__column-number">{{column.label}}</span>
                                    <span class="series-episode-list__column-title" :title="column.title ?? undefined">
                                        {{column.title}}
                                    </span>
                                </th>
                                <th v-else>{{column.label}}</th>
                            </template>
                        </tr>
                    </thead>
                    <tbody>
                        <tr v-for="row in rows" :key="row.channelId">
                            <th class="series-episode-list__channel"
                                :title="row.channelName" :aria-label="row.channelName">
                                <span class="series-episode-list__channel-logo">
                                    <img loading="lazy" :src="`${Utils.api_base_url}/channels/${row.channelId}/logo`"
                                        alt="" @error="hideBrokenChannelLogo">
                                </span>
                                <span class="series-episode-list__channel-name">{{row.channelName}}</span>
                            </th>
                            <td v-for="column in columns" :key="`${row.channelId}-${column.key}`">
                                <router-link v-if="cellProgram(row.channelId, column.key) !== null"
                                    class="series-episode-list__cell"
                                    :to="`/videos/watch/${cellProgram(row.channelId, column.key)!.id}`"
                                    :aria-label="`${cellProgram(row.channelId, column.key)!.title}を再生`">
                                    <img class="series-episode-list__thumb" loading="lazy"
                                        :src="`${Utils.api_base_url}/videos/${cellProgram(row.channelId, column.key)!.id}/thumbnail`"
                                        alt="">
                                    <span v-if="showPartialWarning(row.channelId, column.key)"
                                        class="series-episode-list__partial">部分録画</span>
                                </router-link>
                                <span v-else class="series-episode-list__missing">未録画</span>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </template>
    </div>
</template>
<script lang="ts" setup>

import { computed, ref, watch } from 'vue';

import Series, { type ISeries, type ISeriesRecordedProgram } from '@/services/Series';
import Utils, { dayjs } from '@/utils';
import { extractValidEpisodeSubtitle, formatRecordedEpisodeLabel, formatRecordedEpisodeNumber } from '@/utils/RecordedEpisode';

const props = defineProps<{
    seriesId: number;
}>();

const series = ref<ISeries | null>(null);
const isLoading = ref(true);
const loadFailed = ref(false);
const showExternalSummary = ref(false);
let fetchGeneration = 0;

type MatrixColumn = {
    key: string;
    label: string;
    /** 構造化話数に紐づく題名。無いときは null とし、題名段を空にする（話数段へ混ぜない）。 */
    title: string | null;
};

type MatrixRow = {
    channelId: string;
    channelName: string;
};

type BroadcastSlot = MatrixColumn & {
    startTime: number;
    hasSubtitleLabel: boolean;
};

const recordedPrograms = computed(() => {
    if (series.value === null) return [] as ISeriesRecordedProgram[];
    return series.value.broadcast_periods.flatMap((period) => period.recorded_programs);
});

const columns = computed((): MatrixColumn[] => {
    // 紐づく録画があるシーズンだけを列にする。録画が1本もないシーズンは枠ごと出さない。
    // 録画があるシーズン内の未録画話は既存の未録画セルのまま残す。
    const recordedSeasonNumbers = new Set<number>();
    for (const program of recordedPrograms.value) {
        const seasonNumber = program.series_episode?.season_number;
        if (seasonNumber !== null && seasonNumber !== undefined) {
            recordedSeasonNumbers.add(seasonNumber);
        }
    }
    // シーズン数は除外前の全話数で数え、残った1シーズンだけでも既存の S 接頭辞を維持する。
    const seasonCount = new Set((series.value?.episodes ?? []).map((episode) => episode.season_number)).size;
    const structured = [...(series.value?.episodes ?? [])]
        .filter((episode) => recordedSeasonNumbers.has(episode.season_number))
        .sort((left, right) => {
            if (left.season_number !== right.season_number) {
                return left.season_number - right.season_number;
            }
            return left.episode_number.localeCompare(right.episode_number, 'en', {numeric: true});
        });
    const structuredColumns: MatrixColumn[] = structured.map((episode) => ({
        key: `episode:${episode.id}`,
        label: seasonCount > 1
            ? formatRecordedEpisodeLabel(episode.season_number, episode.episode_number)
            : `第${formatRecordedEpisodeNumber(episode.episode_number)}話`,
        title: episodeTitle(episode.id),
    }));

    // 構造化話数が一部だけ存在する Series でも、話数未確定録画を放送 slot 列として残す。
    // 同じ slot が複数局にある場合は、いずれかの妥当なサブタイトルを時刻より優先する。
    const slots = new Map<string, BroadcastSlot>();
    const unstructuredPrograms = recordedPrograms.value
        .filter((program) => program.series_episode == null)
        .sort((left, right) => dayjs(left.start_time).valueOf() - dayjs(right.start_time).valueOf() || left.id - right.id);
    for (const program of unstructuredPrograms) {
        const key = broadcastSlotKey(program);
        const subtitleLabel = validSubtitleLabel(program);
        const current = slots.get(key);
        if (current === undefined) {
            slots.set(key, {
                key,
                label: subtitleLabel ?? dayjs(program.start_time).format('M/D HH:mm'),
                // 話数未確定の列は既存どおり副題優先の単独ラベルとし、話数を捏造しない。
                title: null,
                startTime: dayjs(program.start_time).valueOf(),
                hasSubtitleLabel: subtitleLabel !== null,
            });
        } else if (current.hasSubtitleLabel === false && subtitleLabel !== null) {
            current.label = subtitleLabel;
            current.hasSubtitleLabel = true;
        }
    }
    const unstructuredColumns: MatrixColumn[] = [...slots.values()]
        .sort((left, right) => left.startTime - right.startTime || left.key.localeCompare(right.key))
        .map(({key, label}) => ({key, label, title: null}));

    return [...structuredColumns, ...unstructuredColumns];
});

function isStructuredColumn(column: MatrixColumn): boolean {
    // 構造化話数列だけを見出し2段（話数・題名）にし、未確定 slot 列は既存の単独ラベルのままにする。
    return column.key.startsWith('episode:');
}

function episodeTitle(episodeId: number): string | null {
    // 構造化話数に紐づく録画の副題を列の題名にする。複数局にある場合はいずれかの妥当な副題を使う。
    // 紐づく録画がない列は題名なしとし、話数ラベルへ混ぜない。
    for (const program of recordedPrograms.value) {
        if (program.series_episode?.id !== episodeId) continue;
        const subtitleLabel = validSubtitleLabel(program);
        if (subtitleLabel !== null) return subtitleLabel;
    }
    return null;
}

function validSubtitleLabel(program: ISeriesRecordedProgram): string | null {
    // Series 表示名と録画側の枠名・作品名を除外し、内容を表す副題だけを列ラベルへ使う。
    return extractValidEpisodeSubtitle(program.subtitle, [
        series.value?.bangumi_subject_name,
        series.value?.title,
        program.series_title,
        program.title,
    ]);
}

const rows = computed((): MatrixRow[] => {
    const channels = new Map<string, string>();
    if (series.value === null) return [];
    for (const period of series.value.broadcast_periods) {
        channels.set(period.channel.id, period.channel.name);
    }
    return [...channels.entries()].map(([channelId, channelName]) => ({channelId, channelName}));
});

function broadcastSlotKey(program: ISeriesRecordedProgram): string {
    return `date:${dayjs(program.start_time).format('YYYY-MM-DD HH:mm')}`;
}

function cellKey(program: ISeriesRecordedProgram): string | null {
    if (program.series_episode != null) {
        return `episode:${program.series_episode.id}`;
    }
    return broadcastSlotKey(program);
}

const programsByCell = computed((): Map<string, ISeriesRecordedProgram> => {
    const programs = new Map<string, ISeriesRecordedProgram>();
    for (const program of recordedPrograms.value) {
        const columnKey = cellKey(program);
        if (program.channel === null || columnKey === null) continue;
        const key = `${program.channel.id}:${columnKey}`;
        const current = programs.get(key);
        if (current === undefined || (current.is_partially_recorded && program.is_partially_recorded === false)) {
            programs.set(key, program);
        }
    }
    return programs;
});

function cellProgram(channelId: string, columnKey: string): ISeriesRecordedProgram | null {
    return programsByCell.value.get(`${channelId}:${columnKey}`) ?? null;
}

function showPartialWarning(channelId: string, columnKey: string): boolean {
    const program = cellProgram(channelId, columnKey);
    return program !== null && program.is_partially_recorded;
}

function hideBrokenChannelLogo(event: Event): void {
    // ロゴを取得できなくても局名テキストへ置き換えず、同じ寸法の空枠を維持する。
    (event.currentTarget as HTMLImageElement).hidden = true;
}

function hideBrokenSeriesCover(event: Event): void {
    // 外部 CDN へフォールバックせず、取得不能なローカル表紙だけを非表示にする。
    (event.currentTarget as HTMLImageElement).hidden = true;
}

const fetchSeries = async () => {
    // カードを素早く切り替えたとき、古い応答で新しい詳細を上書きしない。
    const generation = ++fetchGeneration;
    isLoading.value = true;
    loadFailed.value = false;
    showExternalSummary.value = false;
    const result = await Series.fetchSeries(props.seriesId);
    if (generation !== fetchGeneration) return;
    series.value = result;
    loadFailed.value = result === null;
    isLoading.value = false;
};

watch(() => props.seriesId, fetchSeries, {immediate: true});

</script>
<style lang="scss" scoped>

.series-episode-list {
    min-height: 280px;
    padding: 12px 4px 8px;
}

.series-episode-list__state {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    min-height: 160px;
    color: rgb(var(--v-theme-text-darken-1));
    font-size: 13px;
}

.series-episode-list__bangumi {
    display: flex;
    gap: 14px;
    margin-bottom: 16px;
}

.series-episode-list__bangumi-main {
    min-width: 0;
}

.series-episode-list__cover {
    width: 72px;
    height: 102px;
    border-radius: 6px;
    object-fit: cover;
    background: rgb(var(--v-theme-background-lighten-2));
}

.series-episode-list__bangumi-title {
    font-weight: bold;
    font-size: 15px;
}

.series-episode-list__bangumi-link {
    display: inline-block;
    margin-top: 4px;
    color: rgb(var(--v-theme-primary)) !important;
    font-size: 12px;
}

.series-episode-list__summary {
    margin: 8px 0 0;
    color: rgb(var(--v-theme-text-darken-1));
    font-size: 12px;
    line-height: 1.6;
    white-space: pre-wrap;
}

.series-episode-list__summary-toggle {
    margin-top: 6px;
    padding: 0;
    border: 0;
    background: transparent;
    color: rgb(var(--v-theme-primary));
    font-size: 12px;
    cursor: pointer;
}

.series-episode-list__table-wrap {
    overflow-x: auto;
}

.series-episode-list__table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;

    th, td {
        border: 1px solid rgb(var(--v-theme-background-lighten-2));
        padding: 6px;
        text-align: center;
        vertical-align: middle;
    }

    th {
        background: rgb(var(--v-theme-background-lighten-1));
        white-space: nowrap;
    }
}

.series-episode-list__corner,
.series-episode-list__channel {
    width: 88px;
    min-width: 88px;
    max-width: 88px;
}

.series-episode-list__column-number {
    display: block;
}

.series-episode-list__column-title {
    display: block;
    max-width: 140px;
    margin: 2px auto 0;
    overflow: hidden;
    color: rgb(var(--v-theme-text-darken-1));
    font-size: 11px;
    font-weight: normal;
    white-space: nowrap;
    text-overflow: ellipsis;
}

.series-episode-list__channel-logo {
    display: block;
    width: 32px;
    height: 18px;
    margin: 0 auto 4px;
    overflow: hidden;
    border-radius: 2px;
    background: rgb(var(--v-theme-background-lighten-2));

    img {
        display: block;
        width: 100%;
        height: 100%;
        object-fit: cover;

        &[hidden] {
            display: none;
        }
    }
}

.series-episode-list__channel-name {
    display: block;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
}

.series-episode-list__cell {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
}

.series-episode-list__thumb {
    width: 96px;
    height: 54px;
    object-fit: cover;
    border-radius: 4px;
    background: rgb(var(--v-theme-background-lighten-2));
}

.series-episode-list__partial {
    color: rgb(var(--v-theme-warning));
    font-size: 10.5px;
}

.series-episode-list__missing {
    color: rgb(var(--v-theme-text-darken-1));
}

</style>
