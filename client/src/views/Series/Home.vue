<template>
    <div class="route-container">
        <HeaderBar :searchQuery="search_query" @update:searchQuery="search_query = $event" @search="applySearch" />
        <main>
            <Navigation />
            <div class="series-home-wrapper">
                <SPHeaderBar :searchQuery="search_query" @update:searchQuery="search_query = $event" @search="applySearch" />
                <div class="series-home">
                    <Breadcrumbs :crumbs="[
                        { name: 'ホーム', path: '/' },
                        { name: 'シリーズ', path: '/series/', disabled: true },
                    ]" />
                    <div class="series-home__toolbar">
                        <h1 class="series-home__title">シリーズ</h1>
                        <v-select v-model="sort_order" class="series-home__sort" :items="sort_items"
                            item-title="title" item-value="value" density="compact" variant="outlined" hide-details
                            @update:modelValue="reloadFromFirstPage" />
                    </div>
                    <div v-if="is_loading" class="series-home__state">
                        <v-progress-circular color="primary" indeterminate size="30" width="3" />
                    </div>
                    <div v-else-if="series_list.length === 0" class="series-home__state">
                        表示できるシリーズがありません。
                    </div>
                    <div v-else class="series-home__grid" ref="grid_element">
                        <template v-for="(card, index) in series_list" :key="card.id">
                            <button type="button" class="series-card" :class="{'series-card--expanded': expanded_id === card.id}"
                                @click="toggleExpand(card.id)">
                                <img class="series-card__image" loading="lazy" :src="cardImage(card)" alt="">
                                <div class="series-card__body">
                                    <div class="series-card__name">{{card.title}}</div>
                                    <div class="series-card__meta">
                                        録画 {{card.recorded_count.toLocaleString()}} 件
                                        <span v-if="card.unrecorded_count > 0">・未録画 {{card.unrecorded_count}}</span>
                                        <span v-if="card.partial_count > 0">・一部録画 {{card.partial_count}}</span>
                                    </div>
                                </div>
                            </button>
                            <div v-if="shouldShowDetailAfter(index)" class="series-home__detail">
                                <SeriesEpisodeList :seriesId="expanded_id!" />
                            </div>
                        </template>
                    </div>
                    <div v-if="total_pages > 1" class="series-home__pagination">
                        <v-pagination v-model="current_page" :length="total_pages" density="comfortable"
                            @update:modelValue="changePage" />
                    </div>
                </div>
            </div>
        </main>
    </div>
</template>
<script lang="ts" setup>

import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import Breadcrumbs from '@/components/Breadcrumbs.vue';
import HeaderBar from '@/components/HeaderBar.vue';
import Navigation from '@/components/Navigation.vue';
import SeriesEpisodeList from '@/components/Series/SeriesEpisodeList.vue';
import SPHeaderBar from '@/components/SPHeaderBar.vue';
import Series, { type ISeriesSummary } from '@/services/Series';
import Utils from '@/utils';

const route = useRoute();
const router = useRouter();

const sort_items = [
    {title: '更新が新しい順', value: 'desc'},
    {title: '更新が古い順', value: 'asc'},
];

const series_list = ref<ISeriesSummary[]>([]);
const total = ref(0);
const page_size = ref(50);
const current_page = ref(1);
const sort_order = ref<'desc' | 'asc'>('desc');
const search_query = ref('');
const is_loading = ref(true);
const expanded_id = ref<number | null>(null);
const grid_element = ref<HTMLElement | null>(null);
const column_count = ref(2);

const total_pages = computed(() => Math.max(1, Math.ceil(total.value / page_size.value)));

const cardImage = (card: ISeriesSummary): string => {
    if (card.bangumi_subject_image_url) return card.bangumi_subject_image_url;
    if (card.latest_recorded_program_id !== null) {
        return `${Utils.api_base_url}/videos/${card.latest_recorded_program_id}/thumbnail`;
    }
    return '/assets/images/logo.svg';
};

const updateColumnCount = () => {
    const grid = grid_element.value;
    if (grid === null) return;
    const style = getComputedStyle(grid);
    const raw = style.gridTemplateColumns;
    column_count.value = Math.max(1, raw.split(' ').filter((part) => part !== '').length);
};

const shouldShowDetailAfter = (index: number): boolean => {
    if (expanded_id.value === null) return false;
    const expanded_index = series_list.value.findIndex((card) => card.id === expanded_id.value);
    if (expanded_index < 0) return false;
    const row_end = Math.min(
        series_list.value.length - 1,
        Math.floor(expanded_index / column_count.value) * column_count.value + column_count.value - 1,
    );
    return index === row_end;
};

const fetchPage = async () => {
    is_loading.value = true;
    const result = await Series.fetchSeriesSummaries(sort_order.value, current_page.value, search_query.value);
    if (result !== null) {
        series_list.value = result.series_list;
        total.value = result.total;
        page_size.value = result.page_size;
    }
    is_loading.value = false;
    await nextTick();
    updateColumnCount();
};

const applySearch = async (query: string) => {
    search_query.value = query;
    current_page.value = 1;
    expanded_id.value = null;
    await router.replace({
        path: '/series/',
        query: {
            ...(query !== '' ? {query} : {}),
            order: sort_order.value,
            page: '1',
        },
    });
    await fetchPage();
};

const reloadFromFirstPage = async () => {
    current_page.value = 1;
    expanded_id.value = null;
    await router.replace({
        path: '/series/',
        query: {
            ...(search_query.value !== '' ? {query: search_query.value} : {}),
            order: sort_order.value,
            page: '1',
        },
    });
    await fetchPage();
};

const changePage = async (page: number) => {
    current_page.value = page;
    expanded_id.value = null;
    await router.replace({
        path: '/series/',
        query: {
            ...(search_query.value !== '' ? {query: search_query.value} : {}),
            order: sort_order.value,
            page: String(page),
        },
    });
    await fetchPage();
};

const toggleExpand = async (series_id: number) => {
    const next_id = expanded_id.value === series_id ? null : series_id;
    expanded_id.value = next_id;
    const scroll_y = window.scrollY;
    await router.replace({
        path: next_id === null ? '/series/' : `/series/${next_id}`,
        query: {
            ...(search_query.value !== '' ? {query: search_query.value} : {}),
            order: sort_order.value,
            page: String(current_page.value),
        },
    });
    await nextTick();
    window.scrollTo({top: scroll_y});
};

const openDeepLink = async () => {
    const series_id_text = typeof route.params.id === 'string' ? route.params.id : '';
    search_query.value = typeof route.query.query === 'string' ? route.query.query : '';
    sort_order.value = route.query.order === 'asc' ? 'asc' : 'desc';
    if (series_id_text !== '') {
        const series_id = Number(series_id_text);
        const page = await Series.fetchSeriesListPosition(series_id, sort_order.value, search_query.value);
        if (page !== null) {
            current_page.value = page;
            await fetchPage();
            expanded_id.value = series_id;
            await nextTick();
            updateColumnCount();
            return;
        }
    }
    const page_text = typeof route.query.page === 'string' ? Number(route.query.page) : 1;
    current_page.value = Number.isFinite(page_text) && page_text >= 1 ? page_text : 1;
    await fetchPage();
};

onMounted(async () => {
    window.addEventListener('resize', updateColumnCount);
    await openDeepLink();
});

onUnmounted(() => {
    window.removeEventListener('resize', updateColumnCount);
});

watch(() => route.params.id, async (next_id, previous_id) => {
    if (next_id === previous_id) return;
    await openDeepLink();
});

</script>
<style lang="scss" scoped>

.series-home-wrapper {
    padding-top: 48px;
    @include smartphone-horizontal {
        padding-top: 0;
        padding-left: env(safe-area-inset-left);
    }
}

.series-home {
    max-width: 1100px;
    margin: 0 auto;
    padding: 16px 20px 80px;
}

.series-home__toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin: 8px 0 16px;
}

.series-home__title {
    margin: 0;
    font-size: 22px;
}

.series-home__sort {
    max-width: 220px;
}

.series-home__state {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 180px;
    color: rgb(var(--v-theme-text-darken-1));
}

.series-home__grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 14px;
}

.series-card {
    display: flex;
    flex-direction: column;
    padding: 0;
    border: 1px solid rgb(var(--v-theme-background-lighten-2));
    border-radius: 10px;
    background: rgb(var(--v-theme-background-lighten-1));
    text-align: left;
    cursor: pointer;
    overflow: hidden;

    &--expanded {
        outline: 2px solid rgb(var(--v-theme-primary));
    }
}

.series-card__image {
    width: 100%;
    height: 124px;
    object-fit: cover;
    background: rgb(var(--v-theme-background-lighten-2));
}

.series-card__body {
    padding: 10px 12px 12px;
}

.series-card__name {
    font-weight: bold;
    font-size: 14px;
    line-height: 1.4;
}

.series-card__meta {
    margin-top: 4px;
    color: rgb(var(--v-theme-text-darken-1));
    font-size: 11.5px;
}

.series-home__detail {
    grid-column: 1 / -1;
    min-height: 280px;
    padding: 4px 8px 12px;
    border: 1px solid rgb(var(--v-theme-background-lighten-2));
    border-radius: 10px;
    background: rgb(var(--v-theme-background));
}

.series-home__pagination {
    display: flex;
    justify-content: center;
    margin-top: 20px;
}

</style>
