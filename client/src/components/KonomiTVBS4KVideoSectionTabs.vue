<template>
    <v-tabs class="konomitv-bs4k-video-section-tabs" :model-value="activeTab" color="primary" height="48">
        <v-tab class="konomitv-bs4k-video-section-tabs__tab" value="Videos" to="/videos/">
            <Icon icon="fluent:movies-and-tv-20-regular" width="18px" />
            <span>ビデオ</span>
        </v-tab>
        <v-tab class="konomitv-bs4k-video-section-tabs__tab" value="Series" to="/series/">
            <Icon icon="fluent:collections-20-regular" width="18px" />
            <span>シリーズ</span>
        </v-tab>
        <v-tab class="konomitv-bs4k-video-section-tabs__tab" value="OnAir" to="/series/on-air">
            <Icon icon="fluent:live-20-regular" width="18px" />
            <span>放送中</span>
        </v-tab>
    </v-tabs>
</template>
<script lang="ts" setup>

import { computed } from 'vue';
import { useRoute } from 'vue-router';

type KonomiTVBS4KVideoSectionTab = 'Videos' | 'Series' | 'OnAir';

const route = useRoute();

// 詳細 URL でも、その画面が属するセクションのタブを選択状態に保つ。
const activeTab = computed<KonomiTVBS4KVideoSectionTab>(() => {
    if (route.path.startsWith('/series/on-air')) return 'OnAir';
    if (route.path.startsWith('/series')) return 'Series';
    return 'Videos';
});

</script>
<style lang="scss" scoped>

.konomitv-bs4k-video-section-tabs {
    flex-shrink: 0;
    border-bottom: 1px solid rgb(var(--v-theme-background-lighten-2));
    background: rgb(var(--v-theme-background-lighten-1));

    :deep(.v-slide-group__content) {
        padding: 0 16px;
    }

    &__tab {
        min-width: 128px;
        padding: 0 16px;
        gap: 7px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 13.5px;
        font-weight: 500;
        letter-spacing: normal;
        text-transform: none;

        &.v-tab--selected {
            color: rgb(var(--v-theme-primary));
            font-weight: bold;
        }
    }

    @include smartphone-vertical {
        :deep(.v-slide-group__content) {
            padding: 0;
        }

        &__tab {
            flex: 1 1 0;
            min-width: 0;
            padding: 0 8px;
        }
    }
}

</style>
