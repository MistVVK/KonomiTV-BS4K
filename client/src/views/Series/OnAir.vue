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
                        <div v-for="day in displayDays" :key="day.weekday" class="series-onair__day">
                            <h2 class="series-onair__weekday">
                                {{WEEKDAY_LABELS[day.weekday]}}
                            </h2>
                            <div v-if="day.slots.length === 0" class="series-onair__empty">
                                該当なし
                            </div>
                            <template v-for="(slot, slotIndex) in day.slots"
                                :key="`${slot.series.id}-${slot.hour}-${slot.minute}`">
                                <button type="button" class="series-onair__card"
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
                                    <!-- 表示名は詳細ヘッダと同じ外部名優先の共有ヘルパーで出す。 -->
                                    <div class="series-onair__name">{{formatSeriesDisplayName(slot.series)}}</div>
                                    <div class="series-onair__meta">
                                        <span>未録画 {{slot.series.unrecorded_count}}</span>
                                        <span>部分録画 {{slot.series.partial_count}}</span>
                                    </div>
                                </button>
                                <div v-if="isSlotExpanded(slot.series.id, day.weekday, slotIndex)"
                                    class="series-onair__detail-slot"
                                    :class="{'series-onair__detail-slot--summary': summaryExpanded}"
                                    :ref="bindDetailSlotRef">
                                    <div class="series-onair__detail" :ref="bindDetailFrameRef">
                                        <SeriesEpisodeList :seriesId="slot.series.id" v-model:summary-expanded="summaryExpanded" />
                                    </div>
                                </div>
                            </template>
                        </div>
                    </div>
                </div>
            </div>
        </main>
    </div>
</template>
<script lang="ts" setup>

import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import Breadcrumbs from '@/components/Breadcrumbs.vue';
import HeaderBar from '@/components/HeaderBar.vue';
import KonomiTVBS4KVideoSectionTabs from '@/components/KonomiTVBS4KVideoSectionTabs.vue';
import Navigation from '@/components/Navigation.vue';
import SeriesEpisodeList from '@/components/Series/SeriesEpisodeList.vue';
import SPHeaderBar from '@/components/SPHeaderBar.vue';
import { PRESERVE_SCROLL_POSITION_STATE_KEY } from '@/router';
import Series, { type ISeriesOnAirDay, type ISeriesOnAirSlot } from '@/services/Series';
import { formatSeriesDisplayName } from '@/utils/SeriesUtils';
import { WEEKDAY_LABELS, formatSlotLabel, toDisplayHour, toDisplayWeekday } from '@/views/Series/OnAirUtils';

const route = useRoute();
const router = useRouter();

const days = ref<ISeriesOnAirDay[]>([]);
const isLoading = ref(true);
const expandedSlot = ref<{seriesId: number; weekday: number; slotIndex: number} | null>(null);
// SeriesEpisodeList の概要開閉を受け取り、概要全文が見えるよう詳細枠の固定高さを外す。
const summaryExpanded = ref(false);

// 概要展開中の詳細枠は absolute のため slot の高さを作らない。枠の実測高さを slot の
// min-height へ同期し、slot が曜日列 flow で概要ぶん場所を確保して後続ボタンと重ならないようにする。
const detailSlotEl = shallowRef<HTMLElement | null>(null);
const detailFrameEl = shallowRef<HTMLElement | null>(null);
let detailHeightObserver: ResizeObserver | null = null;

const bindDetailSlotRef = (el: unknown) => {
    detailSlotEl.value = el as HTMLElement | null;
};

const bindDetailFrameRef = (el: unknown) => {
    detailFrameEl.value = el as HTMLElement | null;
};

const syncDetailHeightToSlot = () => {
    if (detailSlotEl.value === null || detailFrameEl.value === null) return;
    detailSlotEl.value.style.minHeight = `${detailFrameEl.value.offsetHeight}px`;
};

const stopDetailHeightSync = () => {
    if (detailHeightObserver !== null) {
        detailHeightObserver.disconnect();
        detailHeightObserver = null;
    }
};

const clearDetailHeightSync = () => {
    stopDetailHeightSync();
    if (detailSlotEl.value !== null) {
        detailSlotEl.value.style.minHeight = '';
    }
};

watch(summaryExpanded, async (expanded) => {
    if (expanded === false) {
        clearDetailHeightSync();
        return;
    }
    await nextTick();
    syncDetailHeightToSlot();
    // ビューポート変化による概要の再行折返しで高さが動いても slot と枠の同期を保つ。
    detailHeightObserver = new ResizeObserver(syncDetailHeightToSlot);
    if (detailFrameEl.value !== null) {
        detailHeightObserver.observe(detailFrameEl.value);
    }
});

// 別のカードへ展開先が移ったら、前のカードで開いた概要の状態を引き継がない。
watch(expandedSlot, () => {
    summaryExpanded.value = false;
});

onUnmounted(stopDetailHeightSync);

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
    position: relative;
    display: grid;
    grid-template-columns: repeat(7, minmax(0, 1fr));
    align-items: start;
    gap: 10px;
    @include tablet-vertical {
        grid-template-columns: 1fr;
    }
    @include smartphone-vertical {
        grid-template-columns: 1fr;
    }
}

// 曜日ごとに独立した縦列にして、展開した詳細の高さをほかの曜日へ波及させない。
.series-onair__day {
    display: flex;
    flex-direction: column;
    min-width: 0;
    gap: 10px;
}

.series-onair__weekday {
    margin: 0;
    font-size: 15px;
    @include tablet-vertical {
        margin-top: 6px;
    }
    @include smartphone-vertical {
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
        min-height: 0;
    }
    @include smartphone-vertical {
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

// 詳細の高さは選択した曜日だけに確保し、同じ曜日の後続ボタンとの重なりを避ける。
.series-onair__detail-slot {
    height: clamp(320px, 50vh, 480px);
}

.series-onair__detail {
    // 横幅は grid を基準に広げ、top を指定しないことで縦位置はボタン直下の slot に従う。
    position: absolute;
    z-index: 1;
    right: 0;
    left: 0;
    height: inherit;
    padding: 4px 8px 12px;
    border: 1px solid rgb(var(--v-theme-background-lighten-2));
    border-radius: 8px;
    background: rgb(var(--v-theme-background));
    overflow-y: auto;
}

// 概要を開いたときは枠を本文ぶんまで伸ばし、概要を枠内スクロールで読ませない。
// 閉じると上の固定高さへ戻る。slot の min-height は JS で枠の高さへ同期され、
// full-width の absolute のまま後続ボタンとも重ならない。
.series-onair__detail-slot--summary {
    height: auto;

    .series-onair__detail {
        height: auto;
        overflow-y: visible;
    }
}

</style>
