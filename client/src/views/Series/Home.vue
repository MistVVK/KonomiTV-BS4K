<template>
    <div class="route-container">
        <HeaderBar v-model:searchQuery="searchQuery" @search="applySearch" />
        <main>
            <Navigation />
            <div class="series-home-wrapper">
                <SPHeaderBar v-model:searchQuery="searchQuery" @search="applySearch" />
                <KonomiTVBS4KVideoSectionTabs />
                <div class="series-home">
                    <Breadcrumbs :crumbs="[
                        { name: 'ホーム', path: '/' },
                        { name: 'ビデオをみる', path: '/videos/' },
                        { name: 'シリーズ', path: '/series/', disabled: true },
                    ]" />
                    <div class="series-home__toolbar">
                        <h1 class="series-home__title">シリーズ</h1>
                        <v-select :model-value="selectedSortValue" class="series-home__sort" :items="sortItems"
                            item-title="title" item-value="value" density="compact" variant="outlined" hide-details
                            @update:modelValue="changeSort" />
                    </div>
                    <div v-if="isLoading" class="series-home__state">
                        <v-progress-circular color="primary" indeterminate size="30" width="3" />
                    </div>
                    <div v-else-if="seriesList.length === 0" class="series-home__state">
                        表示できるシリーズがありません。
                    </div>
                    <div v-else ref="gridElement" class="series-home__grid">
                        <template v-for="(card, index) in seriesList" :key="card.id">
                            <button type="button" class="series-card" :class="{'series-card--expanded': expandedId === card.id}"
                                :style="{gridColumn: String(cardGridColumn(index)), gridRow: String(cardGridRow(index))}"
                                :aria-expanded="expandedId === card.id"
                                @click="toggleExpand(card.id)">
                                <img class="series-card__image" loading="lazy" :src="cardImage(card)" alt=""
                                    @error="useCardImageFallback(card, $event)">
                                <div class="series-card__body">
                                    <div class="series-card__name">{{card.title}}</div>
                                    <div class="series-card__meta">
                                        録画 {{card.recorded_count.toLocaleString()}} 件
                                        <span v-if="card.unrecorded_count > 0">・未録画 {{card.unrecorded_count}}</span>
                                        <span v-if="card.partial_count > 0">・部分録画 {{card.partial_count}}</span>
                                    </div>
                                </div>
                            </button>
                        </template>
                        <div v-if="expandedId !== null" class="series-home__detail"
                            :style="{gridRow: String(detailGridRow)}">
                            <SeriesEpisodeList :seriesId="expandedId" />
                        </div>
                    </div>
                    <div v-if="total > 0" class="series-home__pagination">
                        <v-pagination
                            v-model="currentPage"
                            active-color="primary"
                            density="comfortable"
                            :length="totalPages"
                            :total-visible="Utils.isSmartphoneVertical() ? 5 : 7"
                            @update:model-value="changePage">
                        </v-pagination>
                    </div>
                </div>
            </div>
        </main>
    </div>
</template>
<script lang="ts" setup>

import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import type { SeriesHomeSortKey, VideoSeriesSortDirection } from '@/stores/SettingsStore';

import Breadcrumbs from '@/components/Breadcrumbs.vue';
import HeaderBar from '@/components/HeaderBar.vue';
import KonomiTVBS4KVideoSectionTabs from '@/components/KonomiTVBS4KVideoSectionTabs.vue';
import Navigation from '@/components/Navigation.vue';
import SeriesEpisodeList from '@/components/Series/SeriesEpisodeList.vue';
import SPHeaderBar from '@/components/SPHeaderBar.vue';
import { PRESERVE_SCROLL_POSITION_STATE_KEY } from '@/router';
import Series, { type ISeriesSummary } from '@/services/Series';
import useSettingsStore from '@/stores/SettingsStore';
import Utils from '@/utils';

const settingsStore = useSettingsStore();

// SettingsStore のソートキーと API の sort パラメータ (snake_case) の対応。
const SERIES_HOME_SORT_API_VALUES = {
    UpdatedAt: 'updated_at',
    TitleReading: 'title_reading',
    FirstAirDate: 'first_air_date',
    TmdbPopularity: 'tmdb_popularity',
    TmdbVoteAverage: 'tmdb_vote_average',
    BangumiRating: 'bangumi_rating',
} as const;
const SERIES_HOME_SORT_KEYS_BY_API_VALUE = Object.fromEntries(
    Object.entries(SERIES_HOME_SORT_API_VALUES).map(([key, api_value]) => [api_value, key]),
) as Record<string, SeriesHomeSortKey>;
const isSeriesHomeSortKey = (value: string): value is SeriesHomeSortKey => {
    return value in SERIES_HOME_SORT_API_VALUES;
};

const route = useRoute();
const router = useRouter();

// ソート選択の値は "設定キー:順序" の組合せ。表示順は既定の更新日時を先頭にする。
const sortItems = [
    {title: '更新が新しい順', value: 'UpdatedAt:Desc'},
    {title: '更新が古い順', value: 'UpdatedAt:Asc'},
    {title: 'タイトル (あ→ん)', value: 'TitleReading:Asc'},
    {title: 'タイトル (ん→あ)', value: 'TitleReading:Desc'},
    {title: '初放送日が新しい順', value: 'FirstAirDate:Desc'},
    {title: '初放送日が古い順', value: 'FirstAirDate:Asc'},
    {title: 'TMDb 人気度が高い順', value: 'TmdbPopularity:Desc'},
    {title: 'TMDb 評価が高い順', value: 'TmdbVoteAverage:Desc'},
    {title: 'Bangumi レーティングが高い順', value: 'BangumiRating:Desc'},
];

const seriesList = ref<ISeriesSummary[]>([]);
const total = ref(0);
const pageSize = ref(50);
const currentPage = ref(1);
// ソートは URL query が正本。query に無いときは SettingsStore の前回選択を使う。
const sortKey = ref<SeriesHomeSortKey>(settingsStore.settings.series_home_sort_key);
const sortOrder = ref<VideoSeriesSortDirection>(settingsStore.settings.series_home_sort_direction);
const selectedSortValue = computed(() => `${sortKey.value}:${sortOrder.value}`);
const searchQuery = ref('');
const isLoading = ref(true);
const expandedId = ref<number | null>(null);
const gridElement = ref<HTMLElement | null>(null);
const columnCount = ref(1);
let gridResizeObserver: ResizeObserver | null = null;
let observedGridWidth = -1;
let routeSyncGeneration = 0;
let loadedListStateKey: string | null = null;

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)));
const expandedIndex = computed(() => seriesList.value.findIndex((card) => card.id === expandedId.value));
const expandedRow = computed(() => {
    if (expandedIndex.value < 0) return null;
    return Math.floor(expandedIndex.value / columnCount.value) + 1;
});
const detailGridRow = computed(() => (expandedRow.value ?? 0) + 1);

const cardImage = (card: ISeriesSummary): string => {
    return `${Utils.api_base_url}/series/${card.id}/poster`;
};

const useCardImageFallback = (card: ISeriesSummary, event: Event): void => {
    const image = event.currentTarget as HTMLImageElement;
    // ローカル表紙が 404 のときだけ、従来の録画サムネイルからロゴの順へフォールバックする。
    if (image.dataset.fallback === undefined && card.latest_recorded_program_id !== null) {
        image.dataset.fallback = 'thumbnail';
        image.src = `${Utils.api_base_url}/videos/${card.latest_recorded_program_id}/thumbnail`;
        return;
    }
    if (image.dataset.fallback !== 'logo') {
        image.dataset.fallback = 'logo';
        image.src = '/assets/images/logo.svg';
    }
};

const updateColumnCount = () => {
    const grid = gridElement.value;
    if (grid === null) return;
    const style = getComputedStyle(grid);
    const raw = style.gridTemplateColumns;
    columnCount.value = Math.max(1, raw.split(' ').filter((part) => part !== '').length);
};

const cardGridColumn = (index: number): number => {
    return index % columnCount.value + 1;
};

const cardGridRow = (index: number): number => {
    const row = Math.floor(index / columnCount.value) + 1;
    return expandedRow.value !== null && row > expandedRow.value ? row + 1 : row;
};

const buildListStateKey = (query: string, sort: SeriesHomeSortKey, order: 'desc' | 'asc', page: number): string => {
    return `${query}\0${sort}\0${order}\0${page}`;
};

const fetchPage = async (
    generation: number,
    query: string,
    sort: SeriesHomeSortKey,
    order: 'desc' | 'asc',
    page: number,
): Promise<boolean> => {
    if (generation !== routeSyncGeneration) return false;
    isLoading.value = true;
    const result = await Series.fetchSeriesSummaries(SERIES_HOME_SORT_API_VALUES[sort], order, page, query);
    if (generation !== routeSyncGeneration) return false;
    if (result !== null) {
        seriesList.value = result.series_list;
        total.value = result.total;
        pageSize.value = result.page_size;
        loadedListStateKey = buildListStateKey(query, sort, order, page);
    } else {
        seriesList.value = [];
        total.value = 0;
        loadedListStateKey = null;
    }
    isLoading.value = false;
    await nextTick();
    if (generation !== routeSyncGeneration) return false;
    gridResizeObserver?.disconnect();
    observedGridWidth = -1;
    if (gridElement.value !== null) {
        gridResizeObserver?.observe(gridElement.value);
    }
    updateColumnCount();
    return true;
};

const applySearch = async (query: string) => {
    const previousFullPath = route.fullPath;
    routeSyncGeneration++;
    searchQuery.value = query;
    currentPage.value = 1;
    expandedId.value = null;
    await router.replace({
        path: '/series/',
        query: {
            ...(query !== '' ? {query} : {}),
            sort: SERIES_HOME_SORT_API_VALUES[sortKey.value],
            order: sortOrder.value.toLowerCase(),
            page: '1',
        },
    });
    if (route.fullPath === previousFullPath) await syncRouteState();
};

const changeSort = async (value: unknown) => {
    // 選択値は "設定キー:順序" の組合せ。両方を URL query と SettingsStore へ永続化する。
    const [key_text, direction_text] = String(value).split(':');
    if (isSeriesHomeSortKey(key_text) === false) return;
    if (direction_text !== 'Asc' && direction_text !== 'Desc') return;
    settingsStore.settings.series_home_sort_key = key_text;
    settingsStore.settings.series_home_sort_direction = direction_text;
    sortKey.value = key_text;
    sortOrder.value = direction_text;
    await reloadFromFirstPage();
};

const reloadFromFirstPage = async () => {
    const previousFullPath = route.fullPath;
    routeSyncGeneration++;
    currentPage.value = 1;
    expandedId.value = null;
    await router.replace({
        path: '/series/',
        query: {
            ...(searchQuery.value !== '' ? {query: searchQuery.value} : {}),
            sort: SERIES_HOME_SORT_API_VALUES[sortKey.value],
            order: sortOrder.value.toLowerCase(),
            page: '1',
        },
    });
    if (route.fullPath === previousFullPath) await syncRouteState();
};

const changePage = async (page: number) => {
    const previousFullPath = route.fullPath;
    routeSyncGeneration++;
    currentPage.value = page;
    expandedId.value = null;
    await router.replace({
        path: '/series/',
        query: {
            ...(searchQuery.value !== '' ? {query: searchQuery.value} : {}),
            sort: SERIES_HOME_SORT_API_VALUES[sortKey.value],
            order: sortOrder.value.toLowerCase(),
            page: String(page),
        },
    });
    if (route.fullPath === previousFullPath) await syncRouteState();
};

const toggleExpand = async (seriesId: number) => {
    const previousFullPath = route.fullPath;
    routeSyncGeneration++;
    const nextId = expandedId.value === seriesId ? null : seriesId;
    expandedId.value = nextId;
    await router.replace({
        path: nextId === null ? '/series/' : `/series/${nextId}`,
        query: {
            ...(searchQuery.value !== '' ? {query: searchQuery.value} : {}),
            sort: SERIES_HOME_SORT_API_VALUES[sortKey.value],
            order: sortOrder.value.toLowerCase(),
            page: String(currentPage.value),
        },
        state: {[PRESERVE_SCROLL_POSITION_STATE_KEY]: true},
    });
    if (route.fullPath === previousFullPath) await syncRouteState();
};

const syncRouteState = async () => {
    const generation = ++routeSyncGeneration;
    const seriesIdText = typeof route.params.id === 'string' ? route.params.id : '';
    const query = typeof route.query.query === 'string' ? route.query.query : '';
    // URL query の order が有効なときだけ正本。無い・不正なときは SettingsStore の保存方向へ寄せる。
    const queryOrderText = typeof route.query.order === 'string' ? route.query.order : '';
    const settingsOrder: 'desc' | 'asc' =
        settingsStore.settings.series_home_sort_direction === 'Asc' ? 'asc' : 'desc';
    const order: 'desc' | 'asc' = queryOrderText === 'asc' || queryOrderText === 'desc'
        ? queryOrderText
        : settingsOrder;
    const pageText = typeof route.query.page === 'string' ? Number(route.query.page) : 1;
    const routePage = Number.isInteger(pageText) && pageText >= 1 ? pageText : 1;
    // URL query の sort が正本。無い・不正なときは SettingsStore の前回選択へ寄せる。
    const querySortText = typeof route.query.sort === 'string' ? route.query.sort : '';
    const querySortKey = querySortText !== '' ? SERIES_HOME_SORT_KEYS_BY_API_VALUE[querySortText] : undefined;
    const routeSortKey = querySortKey !== undefined && isSeriesHomeSortKey(querySortKey)
        ? querySortKey
        : settingsStore.settings.series_home_sort_key;
    const routeStateKey = buildListStateKey(query, routeSortKey, order, routePage);

    // URL を唯一の正本として、最新世代の同期だけが画面状態を更新する。
    searchQuery.value = query;
    sortKey.value = routeSortKey;
    sortOrder.value = order === 'asc' ? 'Asc' : 'Desc';
    const seriesId = Number(seriesIdText);
    if (seriesIdText !== '' && Number.isInteger(seriesId) && seriesId >= 1) {
        // 現在の一覧にあるカードの開閉では、list-position と一覧を再取得しない。
        if (loadedListStateKey === routeStateKey && seriesList.value.some((card) => card.id === seriesId)) {
            currentPage.value = routePage;
            expandedId.value = seriesId;
            isLoading.value = false;
            return;
        }

        const page = await Series.fetchSeriesListPosition(seriesId, SERIES_HOME_SORT_API_VALUES[routeSortKey], order, query);
        if (generation !== routeSyncGeneration) return;
        if (page !== null) {
            currentPage.value = page;
            const targetStateKey = buildListStateKey(query, routeSortKey, order, page);
            if (loadedListStateKey !== targetStateKey) {
                const loaded = await fetchPage(generation, query, routeSortKey, order, page);
                if (loaded === false) return;
            } else {
                isLoading.value = false;
            }
            if (generation !== routeSyncGeneration) return;
            expandedId.value = seriesList.value.some((card) => card.id === seriesId) ? seriesId : null;
            await nextTick();
            if (generation !== routeSyncGeneration) return;
            updateColumnCount();
            return;
        }
    }

    currentPage.value = routePage;
    expandedId.value = null;
    if (loadedListStateKey !== routeStateKey) {
        const loaded = await fetchPage(generation, query, routeSortKey, order, routePage);
        if (loaded === false) return;
    } else {
        isLoading.value = false;
    }
    if (generation !== routeSyncGeneration) return;
    if (seriesIdText !== '') {
        await router.replace({
            path: '/series/',
            query: {
                ...(query !== '' ? {query} : {}),
                sort: SERIES_HOME_SORT_API_VALUES[routeSortKey],
                order,
                page: String(routePage),
            },
        });
    }
};

onMounted(async () => {
    gridResizeObserver = new ResizeObserver((entries) => {
        const width = entries[0]?.contentRect.width;
        if (width === undefined || Math.abs(width - observedGridWidth) < 0.5) return;
        observedGridWidth = width;
        updateColumnCount();
    });
    await syncRouteState();
});

onUnmounted(() => {
    gridResizeObserver?.disconnect();
});

// params と query を 1 つの世代境界で同期し、古い応答を画面へ反映しない。
watch(() => route.fullPath, async () => {
    await syncRouteState();
}, {flush: 'sync'});

</script>
<style lang="scss" scoped>

.series-home-wrapper {
    display: flex;
    flex-direction: column;
    width: 100%;
    min-width: 0;
    @include smartphone-horizontal {
        padding-left: env(safe-area-inset-left);
    }
}

.series-home {
    width: 100%;
    min-width: 0;
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
    // ポスターウォールとして列を細くする。2:3 の縦画像でカードが高くなっても、
    // 1画面あたりの件数が旧横長カード (min 220px・画像高さ 124px) と同程度に収まる幅。
    grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
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
    // Bangumi / TMDb の縦長ポスターを切らない 2:3 枠。録画サムネ (16:9) やロゴ fallback も
    // 同じ枠へ object-fit: cover でトリミングし、ポスター有無でカードの縦横を混ぜない。
    aspect-ratio: 2 / 3;
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
    height: clamp(320px, 50vh, 480px);
    padding: 4px 8px 12px;
    border: 1px solid rgb(var(--v-theme-background-lighten-2));
    border-radius: 10px;
    background: rgb(var(--v-theme-background));
    overflow-y: auto;
}

.series-home__pagination {
    display: flex;
    justify-content: center;
    margin-top: 20px;
}

</style>
