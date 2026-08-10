<template>
    <!--
        Vuetify の v-bottom-navigation は layout システムが bottom/transform を inline 制御し、
        Firefox Android の URL バー表示切替補正 (translate / bottom 上書き) を受け付けない。
        見た目に必要な stacked ボタンだけ Vuetify の v-btn を使い、容器は自前の fixed nav にする。
    -->
    <nav class="bottom-navigation-container elevation-12">
        <v-btn class="bottom-navigation-button" variant="text" stacked to="/tv/"
            :class="{'v-btn--active': $route.path.startsWith('/tv')}">
            <Icon icon="fluent:tv-20-regular" width="30px" />
            <span class="mt-1">テレビをみる</span>
        </v-btn>
        <v-btn class="bottom-navigation-button" variant="text" stacked to="/videos/"
            :class="{'v-btn--active': $route.path.startsWith('/videos')}">
            <Icon icon="fluent:movies-and-tv-20-regular" width="30px" />
            <span class="mt-1">ビデオをみる</span>
        </v-btn>
        <v-btn class="bottom-navigation-button" variant="text" stacked to="/reservations/"
            :class="{'v-btn--active': $route.path.startsWith('/reservations')}">
            <Icon icon="fluent:timer-16-regular" width="30px" />
            <span class="mt-1">録画予約</span>
        </v-btn>
        <v-btn class="bottom-navigation-button" variant="text" stacked to="/captures/"
            :class="{'v-btn--active': $route.path.startsWith('/captures')}">
            <Icon icon="fluent:image-multiple-24-regular" width="30px" />
            <span class="mt-1">キャプチャ</span>
        </v-btn>
        <v-btn class="bottom-navigation-button" variant="text" stacked to="/mypage/"
            :class="{'v-btn--active': $route.path.startsWith('/mypage')}">
            <Icon icon="fluent:person-20-regular" width="30px" />
            <span class="mt-1">マイページ</span>
        </v-btn>
    </nav>
</template>
<style lang="scss">

.bottom-navigation-container .v-btn--active > .v-btn__overlay {
    opacity: 0 !important;
}

</style>
<style lang="scss" scoped>

.bottom-navigation-container {
    display: none;
    position: fixed;
    bottom: 0;
    left: 0;
    // Firefox Android 等で URL バー表示切替後に下端がズレるのを --vv-bottom-offset で補正する
    // Utils.startVisualViewportBottomOffsetSync() が visualViewport に追従して更新する
    // 正の offset は上方向、負の offset は下方向 (translate は符号を反転)
    translate: 0 calc(var(--vv-bottom-offset, 0px) * -1);
    height: 56px;
    padding: 0 8px;
    background: rgb(var(--v-theme-background-lighten-1));
    // 旧 v-bottom-navigation の layout z-index (1004) に合わせ、FAB (1005) より下・コンテンツより上へ
    z-index: 1004;

    @include smartphone-vertical {
        display: flex;
        align-items: stretch;
        // iPhone X 以降の Home Indicator の高さ分
        width: calc(100% - 8px * 2);
        padding-bottom: env(safe-area-inset-bottom);
        box-sizing: content-box;
    }

    .v-btn.bottom-navigation-button {
        flex: 1 1 0;  // 均等に割り振る
        min-width: 0 !important;
        height: 100% !important;
        padding: 0 !important;
        border-radius: 0 !important;
        color: rgb(var(--v-theme-text-darken-1)) !important;
        font-weight: bold;
        font-size: 10.5px;
        text-transform: none;

        &.v-btn--active {
            color: rgb(var(--v-theme-primary)) !important;
        }
    }
}

</style>
