<template>
    <div class="route-container">
        <HeaderBar />
        <main>
            <Navigation />
            <div class="series-onair-wrapper">
                <SPHeaderBar />
                <KonomiTVBS4KVideoSectionTabs />
                <div class="series-onair">
                    <Breadcrumbs :crumbs="[
                        { name: 'ホーム', path: '/' },
                        { name: 'ビデオをみる', path: '/videos/' },
                        { name: '放送中', path: '/series/on-air', disabled: true },
                    ]" />
                    <h1 class="series-onair__title">放送中</h1>
                    <p class="series-onair__lead">EPG のいま放送中ではなく、録画から推定した今クール相当の週間レギュラーです。</p>
                    <div v-if="isLoading" class="series-onair__state">
                        <v-progress-circular color="primary" indeterminate size="30" width="3" />
                    </div>
                    <div v-else class="series-onair__grid">
                        <template v-for="day in displayDays" :key="day.weekday">
                            <h2 class="series-onair__weekday"
                                :style="{gridColumn: String(day.weekday + 1), gridRow: '1'}">
                                {{WEEKDAY_LABELS[day.weekday]}}
                            </h2>
                            <div v-if="day.slots.length === 0" class="series-onair__empty"
                                :style="{gridColumn: String(day.weekday + 1), gridRow: '2'}">
                                該当なし
                            </div>
                            <button v-for="(slot, slotIndex) in day.slots"
                                :key="`${slot.series.id}-${slot.hour}-${slot.minute}`"
                                type="button" class="series-onair__card"
                                :style="{
                                    gridColumn: String(day.weekday + 1),
                                    gridRow: String(cardGridRow(slotIndex)),
                                }"
                                :class="{
                                    'series-onair__card--featured': slot.is_featured,
                                    'series-onair__card--expanded': isSlotExpanded(slot.series.id, day.weekday, slotIndex),
                                }"
                                :aria-expanded="isSlotExpanded(slot.series.id, day.weekday, slotIndex)"
                                @click="toggleExpand(slot.series.id, day.weekday, slotIndex)">
                                <div class="series-onair__card-header">
                                    <span class="series-onair__time">{{formatSlotLabel(slot.hour, slot.minute)}}</span>
                                    <span v-if="slot.is_featured" class="series-onair__featured">注目</span>
                                </div>
                                <div class="series-onair__name">{{slot.series.title}}</div>
                                <div class="series-onair__meta">
                                    <span>未録画 {{slot.series.unrecorded_count}}</span>
                                    <span>部分録画 {{slot.series.partial_count}}</span>
                                </div>
                            </button>
                            <div v-if="expandedSlot?.weekday === day.weekday" class="series-onair__detail"
                                :style="{gridRow: String(expandedSlot.slotIndex + 3)}">
                                <SeriesEpisodeList :seriesId="expandedSlot.seriesId" />
                            </div>
                        </template>
                    </div>
                </div>
            </div>
        </main>
    </div>
</template>
<script lang="ts" setup>

import { computed, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import Breadcrumbs from '@/components/Breadcrumbs.vue';
import HeaderBar from '@/components/HeaderBar.vue';
import KonomiTVBS4KVideoSectionTabs from '@/components/KonomiTVBS4KVideoSectionTabs.vue';
import Navigation from '@/components/Navigation.vue';
import SeriesEpisodeList from '@/components/Series/SeriesEpisodeList.vue';
import SPHeaderBar from '@/components/SPHeaderBar.vue';
import { PRESERVE_SCROLL_POSITION_STATE_KEY } from '@/router';
import Series, { type ISeriesOnAirDay, type ISeriesOnAirSlot } from '@/services/Series';
import { WEEKDAY_LABELS, formatSlotLabel, toDisplayHour, toDisplayWeekday } from '@/views/Series/OnAirUtils';

const route = useRoute();
const router = useRouter();

const days = ref<ISeriesOnAirDay[]>([]);
const isLoading = ref(true);
const expandedSlot = ref<{seriesId: number; weekday: number; slotIndex: number} | null>(null);

type DisplayDay = {
    weekday: number;
    slots: ISeriesOnAirSlot[];
};

const displayDays = computed((): DisplayDay[] => {
    const grouped: DisplayDay[] = WEEKDAY_LABELS.map((_, weekday) => ({weekday, slots: []}));
    for (const day of days.value) {
        for (const slot of day.slots) {
            const displayWeekday = toDisplayWeekday(slot.weekday, slot.hour);
            grouped[displayWeekday].slots.push(slot);
        }
    }
    for (const day of grouped) {
        day.slots.sort((left, right) => {
            const leftHour = toDisplayHour(left.hour);
            const rightHour = toDisplayHour(right.hour);
            if (leftHour !== rightHour) return leftHour - rightHour;
            return left.minute - right.minute;
        });
    }
    return grouped;
});

// 詳細行より下のスロットだけを 1 行送って、7 曜日すべての行位置を揃える。
const cardGridRow = (slotIndex: number): number => {
    const detailOffset = expandedSlot.value !== null && slotIndex > expandedSlot.value.slotIndex ? 1 : 0;
    return slotIndex + 2 + detailOffset;
};

const isSlotExpanded = (seriesId: number, weekday: number, slotIndex: number): boolean => {
    return expandedSlot.value?.seriesId === seriesId
        && expandedSlot.value.weekday === weekday
        && expandedSlot.value.slotIndex === slotIndex;
};

const fetchOnAir = async () => {
    isLoading.value = true;
    const result = await Series.fetchOnAirSeries();
    days.value = result?.days ?? [];
    isLoading.value = false;
};

const syncExpandedFromRoute = async () => {
    const seriesIdText = typeof route.params.id === 'string' ? route.params.id : '';
    const seriesId = Number(seriesIdText);
    if (Number.isInteger(seriesId) && seriesId >= 1) {
        // 同じシリーズが複数曜日にある場合、カード操作で選んだ位置は URL 更新後も維持する。
        if (expandedSlot.value?.seriesId === seriesId) return;
        for (const day of displayDays.value) {
            const slotIndex = day.slots.findIndex((slot) => slot.series.id === seriesId);
            if (slotIndex >= 0) {
                expandedSlot.value = {seriesId, weekday: day.weekday, slotIndex};
                return;
            }
        }
    }
    expandedSlot.value = null;
    if (seriesIdText !== '') {
        await router.replace('/series/on-air');
    }
};

const toggleExpand = async (seriesId: number, weekday: number, slotIndex: number) => {
    const nextSlot = isSlotExpanded(seriesId, weekday, slotIndex) ? null : {seriesId, weekday, slotIndex};
    expandedSlot.value = nextSlot;
    // 同じシリーズの別曜日 slot は URL が変わらないため、不要な navigation と scroll を起こさない。
    if (nextSlot !== null && route.params.id === String(nextSlot.seriesId)) return;
    await router.replace({
        path: nextSlot === null ? '/series/on-air' : `/series/on-air/${nextSlot.seriesId}`,
        state: {[PRESERVE_SCROLL_POSITION_STATE_KEY]: true},
    });
};

onMounted(async () => {
    await fetchOnAir();
    await syncExpandedFromRoute();
});

watch(() => route.params.id, async () => {
    await syncExpandedFromRoute();
});

</script>
<style lang="scss" scoped>

.series-onair-wrapper {
    display: flex;
    flex-direction: column;
    width: 100%;
    min-width: 0;
    @include smartphone-horizontal {
        padding-left: env(safe-area-inset-left);
    }
}

.series-onair {
    width: 100%;
    min-width: 0;
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
    grid-auto-flow: row;
    align-items: start;
    gap: 10px;
    @include tablet-vertical {
        grid-template-columns: 1fr;
    }
    @include smartphone-vertical {
        grid-template-columns: 1fr;
    }
}

.series-onair__weekday {
    margin: 0;
    font-size: 15px;
    @include tablet-vertical {
        grid-column: 1 !important;
        grid-row: auto !important;
        margin-top: 6px;
    }
    @include smartphone-vertical {
        grid-column: 1 !important;
        grid-row: auto !important;
        margin-top: 6px;
    }
}

.series-onair__empty {
    min-height: 82px;
    padding: 12px 8px;
    border: 1px dashed rgb(var(--v-theme-background-lighten-2));
    border-radius: 8px;
    color: rgb(var(--v-theme-text-darken-1));
    font-size: 11px;
    @include tablet-vertical {
        grid-column: 1 !important;
        grid-row: auto !important;
        min-height: 0;
    }
    @include smartphone-vertical {
        grid-column: 1 !important;
        grid-row: auto !important;
        min-height: 0;
    }
}

.series-onair__card {
    display: block;
    width: 100%;
    min-height: 82px;
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

    @include tablet-vertical {
        grid-column: 1 !important;
        grid-row: auto !important;
    }
    @include smartphone-vertical {
        grid-column: 1 !important;
        grid-row: auto !important;
    }
}

.series-onair__card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 4px;
}

.series-onair__time {
    color: rgb(var(--v-theme-primary));
    font-size: 12px;
    font-weight: bold;
}

.series-onair__featured {
    padding: 1px 5px;
    border-radius: 999px;
    background: rgb(var(--v-theme-primary));
    color: rgb(var(--v-theme-on-primary));
    font-size: 9.5px;
    font-weight: bold;
}

.series-onair__name {
    margin-top: 2px;
    font-size: 13px;
    font-weight: bold;
    line-height: 1.35;
}

.series-onair__meta {
    display: flex;
    flex-direction: column;
    margin-top: 2px;
    color: rgb(var(--v-theme-text-darken-1));
    font-size: 11px;
}

.series-onair__detail {
    grid-column: 1 / -1;
    height: clamp(320px, 50vh, 480px);
    padding: 4px 8px 12px;
    border: 1px solid rgb(var(--v-theme-background-lighten-2));
    border-radius: 8px;
    overflow-y: auto;
    @include tablet-vertical {
        grid-column: 1 !important;
        grid-row: auto !important;
    }
    @include smartphone-vertical {
        grid-column: 1 !important;
        grid-row: auto !important;
    }
}

</style>
