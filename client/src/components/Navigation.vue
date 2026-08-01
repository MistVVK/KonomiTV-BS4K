<template>
    <div>
        <div class="navigation-container elevation-8" :class="{'navigation-container--icon-only': iconOnly}">
            <nav class="navigation" :class="{'navigation--icon-only': iconOnly}">
                <div class="navigation-scroll" :class="{'navigation-scroll--icon-only': iconOnly}">
                    <router-link v-ripple class="navigation__link" active-class="navigation__link--active" to="/tv/"
                        :class="{
                            'navigation__link--active': $route.path.startsWith('/tv'),
                            'navigation__link--icon-only': iconOnly,
                        }"
                        v-ftooltip.right="iconOnly ? 'テレビをみる' : ''">
                        <Icon class="navigation__link-icon" icon="fluent:tv-20-regular" width="26px" />
                        <span v-if="!iconOnly" class="navigation__link-text">テレビをみる</span>
                    </router-link>
                    <router-link v-ripple class="navigation__link" active-class="navigation__link--active" to="/videos/"
                        :class="{
                            'navigation__link--active': $route.path.startsWith('/videos'),
                            'navigation__link--icon-only': iconOnly,
                        }"
                        v-ftooltip.right="iconOnly ? 'ビデオをみる' : ''">
                        <Icon class="navigation__link-icon" icon="fluent:movies-and-tv-20-regular" width="26px" />
                        <span v-if="!iconOnly" class="navigation__link-text">ビデオをみる</span>
                    </router-link>
                    <router-link v-ripple class="navigation__link" active-class="navigation__link--active" to="/timetable/"
                        :class="{
                            'navigation__link--active': $route.path.startsWith('/timetable'),
                            'navigation__link--icon-only': iconOnly,
                        }"
                        v-ftooltip.right="iconOnly ? '番組表' : ''">
                        <Icon class="navigation__link-icon" icon="fluent:calendar-ltr-20-regular" width="26px" />
                        <span v-if="!iconOnly" class="navigation__link-text">番組表</span>
                    </router-link>
                    <router-link v-ripple class="navigation__link" active-class="navigation__link--active" to="/reservations/"
                        :class="{
                            'navigation__link--active': $route.path.startsWith('/reservations'),
                            'navigation__link--icon-only': iconOnly,
                        }"
                        v-ftooltip.right="iconOnly ? '録画予約' : ''">
                        <Icon class="navigation__link-icon" icon="fluent:timer-16-regular" width="26px" style="padding: 0.5px;" />
                        <span v-if="!iconOnly" class="navigation__link-text">録画予約</span>
                    </router-link>
                    <router-link v-ripple class="navigation__link" active-class="navigation__link--active" to="/captures/"
                        :class="{
                            'navigation__link--active': $route.path.startsWith('/captures'),
                            'navigation__link--icon-only': iconOnly,
                        }"
                        v-ftooltip.right="iconOnly ? 'キャプチャ' : ''">
                        <Icon class="navigation__link-icon" icon="fluent:image-multiple-24-regular" width="26px" />
                        <span v-if="!iconOnly" class="navigation__link-text">キャプチャ</span>
                    </router-link>
                    <router-link v-ripple class="navigation__link" active-class="navigation__link--active" to="/mylist/"
                        :class="{
                            'navigation__link--active': $route.path.startsWith('/mylist'),
                            'navigation__link--icon-only': iconOnly,
                        }"
                        v-ftooltip.right="iconOnly ? 'マイリスト' : ''">
                        <Icon class="navigation__link-icon" icon="ic:round-playlist-play" width="26px" />
                        <span v-if="!iconOnly" class="navigation__link-text">マイリスト</span>
                    </router-link>
                    <router-link v-ripple class="navigation__link" active-class="navigation__link--active" to="/watched-history/"
                        :class="{
                            'navigation__link--active': $route.path.startsWith('/watched-history'),
                            'navigation__link--icon-only': iconOnly,
                        }"
                        v-ftooltip.right="iconOnly ? '視聴履歴' : ''">
                        <Icon class="navigation__link-icon" icon="fluent:history-20-regular" width="26px" />
                        <span v-if="!iconOnly" class="navigation__link-text">視聴履歴</span>
                    </router-link>
                    <v-spacer></v-spacer>
                    <div class="navigation__link navigation__link--analysis"
                        :class="{
                            'navigation__link--active': $route.path.startsWith('/analysis-history'),
                            'navigation__link--analysis-active': analysisTasksStore.activeTaskStatus !== null,
                            'navigation__link--icon-only': iconOnly,
                        }"
                        v-ftooltip.right="iconOnly ? analysisTaskTooltip : ''">
                        <router-link v-ripple class="navigation-analysis__heading" to="/analysis-history/">
                            <span class="navigation__link-icon navigation__link-icon--status">
                                <Icon icon="fluent:database-search-20-regular" width="26px" />
                                <span v-if="analysisTasksStore.activeTaskStatus !== null"
                                    class="navigation-analysis__status-dot"
                                    :class="`navigation-analysis__status-dot--${analysisTasksStore.activeTaskStatus}`"></span>
                            </span>
                            <span v-if="!iconOnly" class="navigation__link-text navigation__link-text--utility">バックグラウンド処理</span>
                            <small v-if="!iconOnly && analysisTasksStore.activeTaskStatus !== null"
                                class="navigation-analysis__status-label">
                                {{analysisTasksStore.activeTaskStatus === 'Running' ? '実行中' : '待機中'}}
                            </small>
                        </router-link>
                        <div v-if="!iconOnly && analysisTasksStore.activeTaskGroups.length > 0"
                            class="navigation-analysis__tasks">
                            <button v-for="group in analysisTasksStore.activeTaskGroups" :key="group.task_type"
                                v-ripple type="button" class="navigation-analysis__task"
                                :aria-label="`${taskTypeLabel(group.task_type)}の現在の処理を表示`"
                                @click="openActiveAnalysisTaskDialog(group.task_type)">
                                <div class="navigation-analysis__task-line">
                                    <strong>{{taskTypeLabel(group.task_type)}}</strong>
                                    <small>{{group.running_count}}件実行中<span v-if="group.queued_count">・{{group.queued_count}}件待機</span></small>
                                </div>
                                <small class="navigation-analysis__stage">{{stageLabel(group.first.stage)}}</small>
                                <v-progress-linear v-if="group.progress !== null" class="mt-1" color="primary" height="4"
                                    rounded :model-value="group.progress * 100" />
                                <v-progress-linear v-else class="mt-1" color="primary" height="4" rounded indeterminate />
                            </button>
                        </div>
                    </div>
                    <router-link v-ripple class="navigation__link" active-class="navigation__link--active" to="/settings/"
                        :class="{
                            'navigation__link--active': $route.path.startsWith('/settings'),
                            'navigation__link--icon-only': iconOnly,
                        }"
                        v-ftooltip.right="iconOnly ? '設定' : ''">
                        <Icon class="navigation__link-icon" icon="fluent:settings-20-regular" width="26px" />
                        <span v-if="!iconOnly" class="navigation__link-text navigation__link-text--utility">設定</span>
                    </router-link>
                    <a v-ripple class="navigation__link navigation__link--version" active-class="navigation__link--active"
                        href="https://github.com/MistVVK/KonomiTV-BS4K" target="_blank"
                        :class="{
                            'navigation__link--develop-version': versionStore.is_client_develop_version,
                            'navigation__link--highlight': versionStore.is_update_available,
                            'navigation__link--icon-only': iconOnly,
                        }"
                        v-ftooltip.right="iconOnly ?
                            (versionStore.is_update_available ? `アップデートがあります (KonomiTV-BS4K ${versionStore.latest_version})` : `KonomiTV-BS4K ${versionStore.client_version}`) :
                            (versionStore.is_update_available ? `アップデートがあります (KonomiTV-BS4K ${versionStore.latest_version})` : '')">
                        <Icon class="navigation__link-icon" icon="fluent:info-16-regular" width="26px" />
                        <span v-if="!iconOnly" class="navigation__link-text navigation__link-version">
                            <span>KonomiTV-BS4K {{versionStore.client_version}}</span>
                            <span>upstream: KonomiTV {{versionStore.upstream_version ?? '-'}}</span>
                            <span class="navigation__link-commit">{{versionStore.client_git_commit}}</span>
                        </span>
                    </a>
                </div>
            </nav>
        </div>
        <BottomNavigation />
        <KonomiTVBS4KActiveAnalysisTaskDialog v-model="activeAnalysisTaskDialog"
            :task-type="selectedActiveTaskType" />
    </div>
</template>
<script lang="ts">

import { mapStores } from 'pinia';
import { defineComponent } from 'vue';

import BottomNavigation from '@/components/BottomNavigation.vue';
import KonomiTVBS4KActiveAnalysisTaskDialog from '@/components/KonomiTVBS4KActiveAnalysisTaskDialog.vue';
import { AnalysisTaskType } from '@/services/AnalysisTasks';
import useAnalysisTasksStore, { stageLabel, taskTypeLabel } from '@/stores/AnalysisTasksStore';
import useVersionStore from '@/stores/VersionStore';

export default defineComponent({
    name: 'Navigation',
    components: {
        BottomNavigation,
        KonomiTVBS4KActiveAnalysisTaskDialog,
    },
    props: {
        // アイコンのみモード: テキストを非表示にし、幅を縮小する
        // 番組表ページでは番組表の表示領域を広く取るためにこのモードを使用する
        iconOnly: {
            type: Boolean,
            default: false,
        },
    },
    data() {
        return {
            activeAnalysisTaskDialog: false,
            selectedActiveTaskType: null as AnalysisTaskType | null,
        };
    },
    computed: {
        ...mapStores(useAnalysisTasksStore),
        ...mapStores(useVersionStore),
        analysisTaskTooltip(): string {
            if (this.analysisTasksStore.activeTaskStatus === 'Running') return 'バックグラウンド処理 (実行中)';
            if (this.analysisTasksStore.activeTaskStatus === 'Queued') return 'バックグラウンド処理 (待機中)';
            return 'バックグラウンド処理';
        },
    },
    methods: {
        stageLabel,
        taskTypeLabel,
        openActiveAnalysisTaskDialog(taskType: AnalysisTaskType): void {
            this.selectedActiveTaskType = taskType;
            this.activeAnalysisTaskDialog = true;
        },
    },
    async created() {
        this.analysisTasksStore.startOverviewPolling();
        await this.versionStore.fetchServerVersion();
    },
    beforeUnmount() {
        this.analysisTasksStore.stopOverviewPolling();
    }
});

</script>
<style lang="scss" scoped>

.navigation-container {
    flex-shrink: 0;
    width: 220px;  // .navigation を fixed にするため、浮いた分の幅を確保する
    background: rgb(var(--v-theme-background-lighten-1));
    @include smartphone-horizontal {
        width: 210px;
    }
    @include smartphone-horizontal-short {
        width: 190px;
    }
    @include smartphone-vertical {
        display: none;
    }

    // アイコンのみモード: 幅を68pxに縮小
    &--icon-only {
        width: 68px;
        @include smartphone-horizontal {
            width: 60px;
        }
        @include smartphone-horizontal-short {
            width: 56px;
        }
    }

    .navigation {
        position: fixed;
        width: 220px;
        top: 65px;  // ヘッダーの高さ分
        left: 0px;
        // スマホ・タブレットのブラウザでアドレスバーが完全に引っ込むまでビューポートの高さが更新されず、
        // その間下に何も背景がない部分ができてしまうのを防ぐ
        bottom: -100px;
        padding-bottom: 100px;
        background: rgb(var(--v-theme-background-lighten-1));
        z-index: 1;
        @include smartphone-horizontal {
            top: 48px;
            width: 210px;
        }
        @include smartphone-horizontal-short {
            width: 190px;
        }

        // アイコンのみモード: 幅を68pxに縮小
        &--icon-only {
            width: 68px;
            @include smartphone-horizontal {
                width: 60px;
            }
            @include smartphone-horizontal-short {
                width: 56px;
            }
        }

        .navigation-scroll {
            display: flex;
            flex-direction: column;
            height: 100%;
            padding: 22px 12px;
            overflow-x: hidden;
            overflow-y: auto;
            @include smartphone-horizontal {
                padding: 10px 12px;
            }
            @include smartphone-horizontal-short {
                padding: 10px 8px;
            }
            &::-webkit-scrollbar-track {
                background: rgb(var(--v-theme-background-lighten-1));
            }

            // アイコンのみモード: パディングを調整
            &--icon-only {
                padding: 22px 8px;
                align-items: center;
                @include smartphone-horizontal {
                    padding: 10px 6px;
                }
                @include smartphone-horizontal-short {
                    padding: 10px 4px;
                }
            }

            .navigation__link {
                display: flex;
                align-items: center;
                flex-shrink: 0;
                height: 52px;
                padding-left: 16px;
                margin-top: 4px;
                border-radius: 11px;
                font-size: 16px;
                color: rgb(var(--v-theme-text));
                transition: background-color 0.15s;
                text-decoration: none;
                user-select: none;
                @include smartphone-horizontal {
                    height: 40px;
                    padding-left: 12px;
                    border-radius: 9px;
                    font-size: 15px;
                }

                &:hover {
                    background: rgb(var(--v-theme-background-lighten-2));
                }
                &:first-of-type {
                    margin-top: 0;
                }
                &--active {
                    color: rgb(var(--v-theme-primary));
                    background: rgb(var(--v-theme-navigation-active));

                    // アイコンのアクセント色は維持し、ラベルだけ背景に対して十分読める色へ分ける
                    .navigation__link-text--utility {
                        color: rgb(var(--v-theme-navigation-active-text));
                    }
                    &:hover {
                        background: rgb(var(--v-theme-navigation-active));
                    }
                }
                &--highlight {
                    color: rgb(var(--v-theme-secondary-readable));

                    // 更新通知時は version とコミット表示も、親リンクの強調色に合わせる
                    .navigation__link-version,
                    .navigation__link-commit {
                        color: inherit;
                    }
                }
                &--develop-version {
                    font-size: 15px;
                    @include smartphone-horizontal {
                        font-size: 14.5px;
                    }
                }
                &--version {
                    height: 64px;
                    @include smartphone-horizontal {
                        height: 58px;
                    }
                }

                .navigation__link-icon {
                    margin-right: 14px;
                    @include smartphone-horizontal {
                        margin-right: 10px;
                    }
                }

                &--analysis {
                    flex-direction: column;
                    align-items: stretch;
                    justify-content: center;
                    height: 52px;
                    padding: 0 12px 0 16px;
                    overflow: hidden;
                    @include smartphone-horizontal {
                        height: 40px;
                        padding-left: 12px;
                    }

                    &-active {
                        justify-content: flex-start;
                        height: auto;
                        min-height: 52px;
                        padding-top: 13px;
                        padding-bottom: 10px;
                        @include smartphone-horizontal {
                            min-height: 40px;
                            padding-top: 7px;
                            padding-bottom: 8px;
                        }
                    }

                    .navigation-analysis__heading {
                        display: flex;
                        align-items: center;
                        flex-shrink: 0;
                        width: 100%;
                        min-height: 26px;
                        color: inherit;
                        text-decoration: none;
                    }

                    .navigation-analysis__status-label {
                        flex-shrink: 0;
                        margin-left: auto;
                        color: rgb(var(--v-theme-primary));
                        font-size: 11px;
                    }

                    .navigation-analysis__tasks {
                        display: flex;
                        flex-direction: column;
                        gap: 8px;
                        width: 100%;
                        padding-top: 8px;
                    }

                    .navigation-analysis__task {
                        width: 100%;
                        min-width: 0;
                        padding: 7px 3px 2px;
                        border: 0;
                        border-top: 1px solid rgb(var(--v-theme-background-lighten-2));
                        border-radius: 4px;
                        cursor: pointer;
                        color: inherit;
                        text-align: left;
                        background: transparent;
                        transition: background-color 0.15s ease;

                        &:hover,
                        &:focus-visible {
                            background: rgb(var(--v-theme-background-lighten-2));
                        }
                    }

                    .navigation-analysis__task-line {
                        display: flex;
                        align-items: center;
                        justify-content: space-between;
                        gap: 6px;

                        strong {
                            min-width: 0;
                            overflow: hidden;
                            white-space: nowrap;
                            text-overflow: ellipsis;
                            font-size: 11px;
                        }

                        small {
                            flex-shrink: 0;
                            font-size: 9px;
                        }
                    }

                    .navigation-analysis__stage {
                        display: block;
                        margin-top: 2px;
                        overflow: hidden;
                        color: rgb(var(--v-theme-text-darken-1));
                        white-space: nowrap;
                        text-overflow: ellipsis;
                        font-size: 10px;
                    }
                }

                .navigation__link-icon--status {
                    display: flex;
                    position: relative;
                }

                .navigation-analysis__status-dot {
                    position: absolute;
                    width: 8px;
                    height: 8px;
                    right: -2px;
                    bottom: -1px;
                    border: 2px solid rgb(var(--v-theme-background-lighten-1));
                    border-radius: 50%;

                    &--Running {
                        background: rgb(var(--v-theme-primary));
                    }

                    &--Queued {
                        background: rgb(var(--v-theme-secondary-lighten-1));
                    }
                }

                .navigation__link-version {
                    display: flex;
                    flex-direction: column;
                    color: rgb(var(--v-theme-text-darken-1));
                    font-size: 12px;
                    line-height: 1.15;
                    white-space: nowrap;
                    @include smartphone-horizontal {
                        font-size: 11px;
                    }
                    @include smartphone-horizontal-short {
                        font-size: 10px;
                    }
                }

                .navigation__link-commit {
                    margin-top: 3px;
                    color: rgb(var(--v-theme-text-darken-2));
                    font-family: monospace;
                    font-size: 9px;
                }

                // アイコンのみモード: 正方形のアイコンボタンに変更
                &--icon-only {
                    width: 52px;
                    height: 52px;
                    padding-left: 0;
                    justify-content: center;
                    @include smartphone-horizontal {
                        width: 44px;
                        height: 44px;
                    }
                    @include smartphone-horizontal-short {
                        width: 40px;
                        height: 40px;
                    }

                    .navigation__link-icon {
                        margin-right: 0;
                    }

                    &.navigation__link--analysis {
                        align-items: center;
                        width: 52px;
                        height: 52px;
                        min-height: 0;
                        padding: 0;
                        @include smartphone-horizontal {
                            width: 44px;
                            height: 44px;
                        }
                        @include smartphone-horizontal-short {
                            width: 40px;
                            height: 40px;
                        }

                        .navigation-analysis__heading {
                            justify-content: center;
                            width: auto;
                            min-height: 0;
                        }
                    }
                }
            }
        }
    }
}

</style>
