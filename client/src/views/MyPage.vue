<template>
    <div class="route-container">
        <HeaderBar />
        <main>
            <Navigation />
            <SPHeaderBar :hide-on-smartphone-vertical="true" />
            <v-card class="settings-container d-flex px-5 py-5 mx-auto" elevation="0" width="100%" max-width="1000">
                <nav class="settings-navigation">
                    <h1 class="mt-2" style="font-size: 24px;">マイページ</h1>
                    <router-link v-ripple to="/settings/account" class="account mt-6">
                        <div class="account-wrapper">
                            <img class="account__icon" :src="userStore.user ? (userStore.user_icon_url ?? '') : '/assets/images/account-icon-default.png'">
                            <div class="account__info">
                                <div class="account__info-name">
                                    <span class="account__info-name-text">{{ userStore.user ? userStore.user.name : 'ログインしていません' }}</span>
                                    <span class="account__info-admin" v-if="userStore.user?.is_admin">管理者</span>
                                </div>
                                <span class="account__info-id">{{ userStore.user ? `User ID: ${userStore.user.id}` : 'Not logged in' }}</span>
                            </div>
                        </div>
                    </router-link>
                    <section class="analysis-tasks mt-3">
                        <div class="analysis-tasks__header">
                            <div>
                                <Icon icon="fluent:database-search-20-regular" width="24px" />
                                <strong>バックグラウンド処理</strong>
                            </div>
                            <router-link to="/analysis-history/">すべての履歴</router-link>
                        </div>
                        <template v-if="activeTaskGroups.length > 0">
                            <div class="analysis-tasks__section-title">実行中</div>
                            <div v-for="group in activeTaskGroups" :key="group.task_type" class="analysis-task analysis-task--active">
                                <div class="analysis-task__line">
                                    <strong>{{taskTypeLabel(group.task_type)}}</strong>
                                    <small>{{group.running_count}}件実行中<span v-if="group.queued_count">・{{group.queued_count}}件待機</span></small>
                                </div>
                                <span class="analysis-task__title">{{group.first.title}}</span>
                                <small>{{stageLabel(group.first.stage)}}・{{elapsedLabel(group.first.started_at)}}</small>
                                <v-progress-linear v-if="group.progress !== null" class="mt-2" color="primary" height="5"
                                    rounded :model-value="group.progress * 100" />
                                <v-progress-linear v-else class="mt-2" color="primary" height="5" rounded indeterminate />
                            </div>
                        </template>
                        <div class="analysis-tasks__section-title">最近の実行履歴</div>
                        <router-link v-for="task in analysisOverview.recent" :key="task.id"
                            class="analysis-task analysis-task--history" :to="`/analysis-history/?task=${task.id}`">
                            <Icon :icon="statusIcon(task.status)" width="21px" :class="`analysis-task__status--${task.status}`" />
                            <div class="analysis-task__body">
                                <div class="analysis-task__line">
                                    <strong>{{taskTypeLabel(task.task_type)}}</strong>
                                    <small>{{dateLabel(task.completed_at)}}</small>
                                </div>
                                <span class="analysis-task__title">{{task.title}}</span>
                                <small>{{resultLabel(task)}}</small>
                            </div>
                        </router-link>
                        <div v-if="analysisOverview.active.length === 0 && analysisOverview.recent.length === 0"
                            class="analysis-tasks__empty">解析履歴はまだありません。</div>
                    </section>
                    <v-btn variant="flat" class="settings-navigation__button" to="/settings/">
                        <Icon icon="fluent:settings-20-regular" width="26px" />
                        <span class="ml-4">設定</span>
                    </v-btn>
                    <v-btn variant="flat" class="settings-navigation__button mt-3" to="/mylist/">
                        <Icon icon="ic:round-playlist-play" width="26px" />
                        <span class="ml-4">マイリスト</span>
                    </v-btn>
                    <v-btn variant="flat" class="settings-navigation__button" to="/watched-history/">
                        <Icon icon="fluent:history-20-regular" width="26px" />
                        <span class="ml-4">視聴履歴</span>
                    </v-btn>
                    <v-btn variant="flat" class="settings-navigation__button settings-navigation__button--version mt-3"
                        :class="{'settings-navigation__button--version-highlight': versionStore.is_update_available}"
                        href="https://github.com/tsukumijima/KonomiTV" target="_blank">
                        <Icon icon="fluent:info-20-regular" width="26px" />
                        <span class="ml-4">
                            version {{versionStore.client_version}}{{versionStore.is_update_available ? ' (Update Available)' : ''}}<br>
                            <small>{{versionStore.client_git_commit}}</small>
                        </span>
                    </v-btn>
                </nav>
            </v-card>
        </main>
    </div>
</template>
<script lang="ts" setup>

import { computed, onMounted, onUnmounted, ref } from 'vue';

import HeaderBar from '@/components/HeaderBar.vue';
import Navigation from '@/components/Navigation.vue';
import SPHeaderBar from '@/components/SPHeaderBar.vue';
import AnalysisTasks, { AnalysisTaskStatus, AnalysisTaskType, IAnalysisTaskExecution, IAnalysisTaskOverview } from '@/services/AnalysisTasks';
import useUserStore from '@/stores/UserStore';
import useVersionStore from '@/stores/VersionStore';
import { dayjs } from '@/utils';

const userStore = useUserStore();
const versionStore = useVersionStore();
const analysisOverview = ref<IAnalysisTaskOverview>({active: [], recent: []});
let analysisPollingTimer: number | null = null;

const activeTaskGroups = computed(() => {
    const groups = new Map<AnalysisTaskType, IAnalysisTaskExecution[]>();
    for (const task of analysisOverview.value.active) {
        groups.set(task.task_type, [...(groups.get(task.task_type) ?? []), task]);
    }
    return [...groups.entries()].map(([task_type, tasks]) => {
        const known_progress = tasks.map(task => task.progress).filter((value): value is number => value !== null);
        return {
            task_type,
            first: tasks.find(task => task.status === 'Running') ?? tasks[0]!,
            running_count: tasks.filter(task => task.status === 'Running').length,
            queued_count: tasks.filter(task => task.status === 'Queued').length,
            progress: known_progress.length > 0
                ? known_progress.reduce((sum, value) => sum + value, 0) / known_progress.length
                : null,
        };
    });
});

function taskTypeLabel(type: AnalysisTaskType): string {
    return {
        RecordedScan: '録画フォルダスキャン', MetadataAnalysis: 'メタデータ解析', PlaybackIndex: '再生索引作成',
        ThumbnailGeneration: 'サムネイル生成', CMAnalysis: 'CM区間解析', CMLogoGeneration: 'CMロゴ生成',
        BatchScan: '録画フォルダ一括スキャン', BatchMetadataReanalysis: '全件メタデータ再解析',
        BatchCMAnalysis: '全件CM再判定', BackgroundAnalysis: 'バックグラウンド一括解析',
    }[type];
}

function stageLabel(stage: string | null): string {
    if (stage === null) return '処理中';
    return ({Queued: '待機中', Probing: 'メディア確認中', Scanning: '全編走査中', Finalizing: '確定中',
        LogoScanning: 'ロゴ走査中', LogoMatching: 'ロゴ照合中', Analyzing: '解析中', Generating: '生成中',
        LogoCatalogScanning: 'ロゴ一覧更新中', ProbingMedia: '解析対象確認中', PreparingMedia: '解析媒体準備中',
        IndexingMedia: '共有索引作成中', ChapterAnalyzing: '無音・シーン解析中', LogoAnalyzing: 'ロゴ解析中',
        HardwareFallback: 'CPU解析へ切替中', CombiningCM: 'CM区間統合中',
        Committing: '結果確定中', Processing: '処理中', Saving: '保存中'} as Record<string, string>)[stage] ?? stage;
}

function statusIcon(status: AnalysisTaskStatus): string {
    if (status === 'Succeeded') return 'fluent:checkmark-circle-20-filled';
    if (status === 'Failed') return 'fluent:error-circle-20-filled';
    if (status === 'Interrupted') return 'fluent:pause-circle-20-filled';
    if (status === 'Skipped') return 'fluent:arrow-skip-forward-20-filled';
    return 'fluent:clock-20-regular';
}

function elapsedLabel(started_at: string | null): string {
    if (started_at === null) return '開始待ち';
    const seconds = Math.max(0, dayjs().diff(dayjs(started_at), 'second'));
    return seconds >= 3600 ? `${Math.floor(seconds / 3600)}時間${Math.floor(seconds % 3600 / 60)}分経過` : `${Math.floor(seconds / 60)}分経過`;
}

function dateLabel(value: string | null): string {
    return value ? dayjs(value).format('M/D HH:mm') : '';
}

function resultLabel(task: IAnalysisTaskExecution): string {
    if (task.status === 'Failed') return `失敗${task.error_code ? `・${task.error_code}` : ''}`;
    if (task.status === 'Interrupted') return '中断';
    if (task.status === 'Skipped') return 'スキップ';
    if (task.total_count > 0) return `${task.total_count}件中 ${task.succeeded_count || task.current_count}件完了${task.failed_count ? `・${task.failed_count}件失敗` : ''}`;
    return '完了';
}

async function updateAnalysisOverview(show_error = false): Promise<void> {
    const overview = await AnalysisTasks.fetchOverview(show_error);
    if (overview !== null) analysisOverview.value = overview;
}

onMounted(async () => {
    await userStore.fetchUser();
    await versionStore.fetchServerVersion();
    await updateAnalysisOverview(true);
    analysisPollingTimer = window.setInterval(() => void updateAnalysisOverview(), 3000);
});

onUnmounted(() => {
    if (analysisPollingTimer !== null) window.clearInterval(analysisPollingTimer);
});

</script>
<style lang="scss" scoped>

.settings-container {
    background: rgb(var(--v-theme-background)) !important;
    width: 100%;
    min-width: 0;
    @include smartphone-horizontal {
        padding: 16px 20px !important;
    }
    @include smartphone-horizontal-short {
        padding: 16px 16px !important;
    }
    @include smartphone-vertical {
        padding: 16px 16px !important;
    }

    .settings-navigation {
        display: flex;
        flex-direction: column;
        flex-shrink: 0;
        width: 100%;
        transform: none !important;
        visibility: visible !important;

        .settings-navigation__button {
            justify-content: left !important;
            width: 100%;
            height: 54px;
            margin-bottom: 6px;
            border-radius: 6px;
            font-size: 16px;
            color: rgb(var(--v-theme-text)) !important;
            background: rgb(var(--v-theme-background-lighten-1)) !important;

            &--version {
                display: none;
                @include smartphone-vertical {
                    display: flex;
                }
                &-highlight {
                    color: rgb(var(--v-theme-secondary-lighten-1)) !important;
                }
            }
        }

        h1 {
            @include smartphone-horizontal {
                font-size: 22px !important;
            }
        }
    }
}

.account {
    display: flex;
    align-items: center;
    height: 130px;
    align-items: normal;
    flex-direction: column;
    height: auto;
    padding: 16px 12px;
    margin-bottom: 6px;
    border-radius: 6px;
    background: rgb(var(--v-theme-background-lighten-1));

    &-wrapper {
        display: flex;
        align-items: center;
        min-width: 0;
        height: 70px;
    }

    &__icon {
        flex-shrink: 0;
        min-width: 70px;
        height: 100%;
        border-radius: 50%;
        object-fit: cover;
        // 読み込まれるまでのアイコンの背景
        background: linear-gradient(150deg, rgb(var(--v-theme-gray)), rgb(var(--v-theme-background-lighten-2)));
        // 低解像度で表示する画像がぼやけないようにする
        // ref: https://sho-log.com/chrome-image-blurred/
        image-rendering: -webkit-optimize-contrast;
    }

    &__info {
        display: flex;
        flex-direction: column;
        min-width: 0;
        margin-left: 12px;
        margin-right: 0px;

        &-name {
            display: inline-flex;
            align-items: center;
            height: 33px;

            &-text {
                display: inline-block;
                font-size: 20px;
                color: rgb(var(--v-theme-text));
                font-weight: bold;
                overflow: hidden;
                white-space: nowrap;
                text-overflow: ellipsis;  // はみ出た部分を … で省略
            }
        }

        &-admin {
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
                width: 45px;
                height: 24px;
            margin-left: 10px;
            border-radius: 4px;
            background: rgb(var(--v-theme-secondary));
            font-size: 11.5px;
            font-weight: 500;
            line-height: 2;
        }

        &-id {
            display: inline-block;
            margin-top: 2px;
            color: rgb(var(--v-theme-text-darken-1));
            font-size: 14.5px;
        }
    }
}

.analysis-tasks {
    padding: 14px 12px;
    margin-bottom: 6px;
    border-radius: 6px;
    color: rgb(var(--v-theme-text));
    background: rgb(var(--v-theme-background-lighten-1));
    &__header, &__header > div, .analysis-task__line { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    &__header > div { justify-content: flex-start; }
    &__header a { color: rgb(var(--v-theme-primary)); font-size: 12px; }
    &__section-title { margin: 13px 2px 6px; color: rgb(var(--v-theme-text-darken-1)); font-size: 11px; }
    &__empty { padding: 16px 4px 4px; text-align: center; opacity: 0.65; font-size: 12px; }
}
.analysis-task {
    display: block; min-width: 0; padding: 10px; margin-top: 6px; border-radius: 6px;
    color: rgb(var(--v-theme-text)); background: rgb(var(--v-theme-background-lighten-2));
    &--history { display: flex; align-items: flex-start; gap: 8px; }
    &__body { flex: 1; min-width: 0; }
    &__line { width: 100%; }
    &__line small { flex-shrink: 0; }
    &__title { display: block; margin: 2px 0; overflow-wrap: anywhere; font-size: 13px; }
    small { color: rgb(var(--v-theme-text-darken-1)); font-size: 11px; }
    &__status--Succeeded { color: #66bb6a; }
    &__status--Failed { color: rgb(var(--v-theme-error)); }
    &__status--Interrupted, &__status--Skipped { color: #ffa726; }
}

</style>
