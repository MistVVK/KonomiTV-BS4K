<template>
    <div class="series-episode-list">
        <div v-if="is_loading" class="series-episode-list__state">
            <v-progress-circular color="primary" indeterminate size="28" width="3" />
            <span>シリーズ詳細を取得しています…</span>
        </div>
        <div v-else-if="load_failed" class="series-episode-list__state">
            <span>シリーズ詳細を取得できませんでした。</span>
        </div>
        <template v-else-if="series !== null">
            <div v-if="series.bangumi_subject_id !== null" class="series-episode-list__bangumi">
                <img v-if="series.bangumi_subject_image_url"
                    class="series-episode-list__cover" :src="series.bangumi_subject_image_url" alt="">
                <div class="series-episode-list__bangumi-main">
                    <div class="series-episode-list__bangumi-title">
                        {{series.bangumi_subject_name_cn || series.bangumi_subject_name || series.title}}
                    </div>
                    <a class="series-episode-list__bangumi-link"
                        :href="`https://bgm.tv/subject/${series.bangumi_subject_id}`" target="_blank" rel="noopener">
                        bgm.tv で開く
                    </a>
                    <p v-if="show_original_summary && series.bangumi_subject_summary" class="series-episode-list__summary">
                        {{series.bangumi_subject_summary}}
                    </p>
                    <button v-if="series.bangumi_subject_summary" type="button"
                        class="series-episode-list__summary-toggle" @click="show_original_summary = !show_original_summary">
                        {{show_original_summary ? '簡介を畳む' : '簡介を表示'}}
                    </button>
                </div>
            </div>

            <div class="series-episode-list__table-wrap">
                <table class="series-episode-list__table">
                    <thead>
                        <tr>
                            <th class="series-episode-list__corner">放送局</th>
                            <th v-for="column in columns" :key="column.key">{{column.label}}</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr v-for="row in rows" :key="row.channel_id">
                            <th>{{row.channel_name}}</th>
                            <td v-for="column in columns" :key="`${row.channel_id}-${column.key}`">
                                <router-link v-if="cellProgram(row.channel_id, column.key) !== null"
                                    class="series-episode-list__cell"
                                    :to="`/videos/watch/${cellProgram(row.channel_id, column.key)!.id}`">
                                    <img class="series-episode-list__thumb" loading="lazy"
                                        :src="`${Utils.api_base_url}/videos/${cellProgram(row.channel_id, column.key)!.id}/thumbnail`"
                                        alt="">
                                    <span v-if="showPartialWarning(row.channel_id, column.key)"
                                        class="series-episode-list__partial">一部のみ録画</span>
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

import { computed, onMounted, ref, watch } from 'vue';

import Series, { type ISeries, type ISeriesRecordedProgram } from '@/services/Series';
import Utils, { dayjs } from '@/utils';
import { formatRecordedEpisodeLabel, formatRecordedEpisodeNumber } from '@/utils/RecordedEpisode';

const props = defineProps<{
    seriesId: number;
}>();

const series = ref<ISeries | null>(null);
const is_loading = ref(true);
const load_failed = ref(false);
const show_original_summary = ref(false);

type MatrixColumn = {
    key: string;
    label: string;
};

type MatrixRow = {
    channel_id: string;
    channel_name: string;
};

const recordedPrograms = computed(() => {
    if (series.value === null) return [] as ISeriesRecordedProgram[];
    return series.value.broadcast_periods.flatMap((period) => period.recorded_programs);
});

const columns = computed((): MatrixColumn[] => {
    const structured = [...(series.value?.episodes ?? [])].sort((left, right) => {
        if (left.season_number !== right.season_number) {
            return left.season_number - right.season_number;
        }
        return left.episode_number.localeCompare(right.episode_number, 'en', {numeric: true});
    });
    if (structured.length > 0) {
        const season_count = new Set(structured.map((episode) => episode.season_number)).size;
        return structured.map((episode) => ({
            key: `episode:${episode.id}`,
            label: season_count > 1
                ? formatRecordedEpisodeLabel(episode.season_number, episode.episode_number)
                : `第${formatRecordedEpisodeNumber(episode.episode_number)}話`,
        }));
    }
    const slots = new Map<string, string>();
    for (const program of recordedPrograms.value) {
        const key = broadcastSlotKey(program);
        if (slots.has(key) === false) {
            slots.set(key, dayjs(program.start_time).format('M/D HH:mm'));
        }
    }
    return [...slots.entries()]
        .sort((left, right) => left[0].localeCompare(right[0]))
        .map(([key, label]) => ({key, label}));
});

const rows = computed((): MatrixRow[] => {
    const channels = new Map<string, string>();
    if (series.value === null) return [];
    for (const period of series.value.broadcast_periods) {
        channels.set(period.channel.id, period.channel.name);
    }
    return [...channels.entries()].map(([channel_id, channel_name]) => ({channel_id, channel_name}));
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

function cellProgram(channel_id: string, column_key: string): ISeriesRecordedProgram | null {
    const candidates = recordedPrograms.value.filter((program) => {
        return program.channel?.id === channel_id && cellKey(program) === column_key;
    });
    if (candidates.length === 0) return null;
    const complete = candidates.find((program) => program.is_partially_recorded === false);
    return complete ?? candidates[0];
}

function showPartialWarning(channel_id: string, column_key: string): boolean {
    const program = cellProgram(channel_id, column_key);
    return program !== null && program.is_partially_recorded;
}

const fetchSeries = async () => {
    is_loading.value = true;
    load_failed.value = false;
    const result = await Series.fetchSeries(props.seriesId);
    series.value = result;
    load_failed.value = result === null;
    is_loading.value = false;
};

onMounted(fetchSeries);
watch(() => props.seriesId, fetchSeries);

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

.series-episode-list__corner {
    min-width: 88px;
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
