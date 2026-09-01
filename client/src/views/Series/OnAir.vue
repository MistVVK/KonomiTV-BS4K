<template>
    <div class="route-container">
        <HeaderBar />
        <main>
            <Navigation />
            <div class="series-onair-wrapper">
                <SPHeaderBar />
                <div class="series-onair">
                    <Breadcrumbs :crumbs="[
                        { name: 'ホーム', path: '/' },
                        { name: '放送中', path: '/series/on-air', disabled: true },
                    ]" />
                    <h1 class="series-onair__title">放送中</h1>
                    <p class="series-onair__lead">EPG のいま放送中ではなく、録画から推定した今クール相当の週間レギュラーです。</p>
                    <div v-if="is_loading" class="series-onair__state">
                        <v-progress-circular color="primary" indeterminate size="30" width="3" />
                    </div>
                    <div v-else class="series-onair__grid">
                        <section v-for="day in display_days" :key="day.weekday" class="series-onair__day"
                            :style="{'--skeleton-count': String(Math.max(3, day.slots.length))}">
                            <h2 class="series-onair__weekday">{{WEEKDAY_LABELS[day.weekday]}}</h2>
                            <button v-for="slot in day.slots" :key="`${slot.series.id}-${slot.hour}-${slot.minute}`"
                                type="button" class="series-onair__card"
                                :class="{
                                    'series-onair__card--featured': slot.is_featured,
                                    'series-onair__card--expanded': expanded_id === slot.series.id,
                                }"
                                @click="toggleExpand(slot.series.id)">
                                <div class="series-onair__time">{{formatSlotLabel(slot.hour, slot.minute)}}</div>
                                <div class="series-onair__name">{{slot.series.title}}</div>
                                <div class="series-onair__meta">
                                    未録画 {{slot.series.unrecorded_count}}
                                    ・一部 {{slot.series.partial_count}}
                                </div>
                            </button>
                            <div v-if="expandedDayId(day.weekday) !== null" class="series-onair__detail">
                                <SeriesEpisodeList :seriesId="expandedDayId(day.weekday)!" />
                            </div>
                        </section>
                    </div>
                </div>
            </div>
        </main>
    </div>
</template>
<script lang="ts" setup>

import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import Breadcrumbs from '@/components/Breadcrumbs.vue';
import HeaderBar from '@/components/HeaderBar.vue';
import Navigation from '@/components/Navigation.vue';
import SeriesEpisodeList from '@/components/Series/SeriesEpisodeList.vue';
import SPHeaderBar from '@/components/SPHeaderBar.vue';
import Series, { type ISeriesOnAirDay, type ISeriesOnAirSlot } from '@/services/Series';
import { WEEKDAY_LABELS, formatSlotLabel, toDisplayHour, toDisplayWeekday } from '@/views/Series/OnAirUtils';

const route = useRoute();
const router = useRouter();

const days = ref<ISeriesOnAirDay[]>([]);
const is_loading = ref(true);
const expanded_id = ref<number | null>(null);

type DisplayDay = {
    weekday: number;
    slots: ISeriesOnAirSlot[];
};

const display_days = computed((): DisplayDay[] => {
    const grouped: DisplayDay[] = WEEKDAY_LABELS.map((_, weekday) => ({weekday, slots: []}));
    for (const day of days.value) {
        for (const slot of day.slots) {
            const display_weekday = toDisplayWeekday(slot.weekday, slot.hour);
            grouped[display_weekday].slots.push(slot);
        }
    }
    for (const day of grouped) {
        day.slots.sort((left, right) => {
            const left_hour = toDisplayHour(left.hour);
            const right_hour = toDisplayHour(right.hour);
            if (left_hour !== right_hour) return left_hour - right_hour;
            return left.minute - right.minute;
        });
    }
    return grouped;
});

const expandedDayId = (weekday: number): number | null => {
    if (expanded_id.value === null) return null;
    const day = display_days.value.find((item) => item.weekday === weekday);
    if (day === undefined) return null;
    return day.slots.some((slot) => slot.series.id === expanded_id.value) ? expanded_id.value : null;
};

const fetchOnAir = async () => {
    is_loading.value = true;
    const result = await Series.fetchOnAirSeries();
    days.value = result?.days ?? [];
    is_loading.value = false;
};

const toggleExpand = async (series_id: number) => {
    const next_id = expanded_id.value === series_id ? null : series_id;
    expanded_id.value = next_id;
    const scroll_y = window.scrollY;
    await router.replace({
        path: next_id === null ? '/series/on-air' : `/series/on-air/${next_id}`,
    });
    await nextTick();
    window.scrollTo({top: scroll_y});
};

onMounted(async () => {
    await fetchOnAir();
    const series_id_text = typeof route.params.id === 'string' ? route.params.id : '';
    if (series_id_text !== '') {
        expanded_id.value = Number(series_id_text);
    }
});

watch(() => route.params.id, (next_id) => {
    if (typeof next_id === 'string' && next_id !== '') {
        expanded_id.value = Number(next_id);
        return;
    }
    expanded_id.value = null;
});

</script>
<style lang="scss" scoped>

.series-onair-wrapper {
    padding-top: 48px;
    @include smartphone-horizontal {
        padding-top: 0;
        padding-left: env(safe-area-inset-left);
    }
}

.series-onair {
    max-width: 1280px;
    margin: 0 auto;
    padding: 16px 16px 80px;
}

.series-onair__title {
    margin: 8px 0 4px;
    font-size: 22px;
}

.series-onair__lead {
    margin: 0 0 16px;
    color: rgb(var(--v-theme-text-darken-1));
    font-size: 12.5px;
}

.series-onair__state {
    display: flex;
    justify-content: center;
    min-height: 180px;
    align-items: center;
}

.series-onair__grid {
    display: grid;
    grid-template-columns: repeat(7, minmax(0, 1fr));
    gap: 10px;
    @include tablet-vertical {
        grid-template-columns: 1fr;
    }
    @include smartphone-vertical {
        grid-template-columns: 1fr;
    }
}

.series-onair__day {
    min-height: calc(72px * var(--skeleton-count, 3));
    @include tablet-vertical {
        min-height: 0;
    }
    @include smartphone-vertical {
        min-height: 0;
    }
}

.series-onair__weekday {
    margin: 0 0 8px;
    font-size: 15px;
}

.series-onair__card {
    display: block;
    width: 100%;
    margin-bottom: 8px;
    padding: 8px;
    border: 1px solid rgb(var(--v-theme-background-lighten-2));
    border-radius: 8px;
    background: rgb(var(--v-theme-background-lighten-1));
    text-align: left;
    cursor: pointer;

    &--featured {
        outline: 2px solid rgb(var(--v-theme-primary));
    }

    &--expanded {
        background: rgb(var(--v-theme-background-lighten-2));
    }
}

.series-onair__time {
    color: rgb(var(--v-theme-primary));
    font-size: 12px;
    font-weight: bold;
}

.series-onair__name {
    margin-top: 2px;
    font-size: 13px;
    font-weight: bold;
    line-height: 1.35;
}

.series-onair__meta {
    margin-top: 2px;
    color: rgb(var(--v-theme-text-darken-1));
    font-size: 11px;
}

.series-onair__detail {
    min-height: 280px;
    margin: 8px 0 12px;
    padding: 4px;
    border: 1px solid rgb(var(--v-theme-background-lighten-2));
    border-radius: 8px;
    @include tablet-vertical {
        grid-column: auto;
    }
}

</style>
