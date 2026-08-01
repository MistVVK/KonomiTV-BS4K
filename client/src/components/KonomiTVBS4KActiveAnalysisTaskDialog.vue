<template>
    <v-dialog :model-value="modelValue" max-width="720" scrollable
        @update:model-value="$emit('update:modelValue', $event)">
        <v-card class="active-analysis-dialog">
            <v-card-title class="active-analysis-dialog__title">
                <span>
                    <Icon icon="fluent:database-search-20-regular" width="25px" />
                    {{dialogTitle}}
                </span>
                <v-btn icon variant="text" size="small" aria-label="閉じる"
                    @click="$emit('update:modelValue', false)">
                    <Icon icon="fluent:dismiss-20-regular" width="22px" />
                </v-btn>
            </v-card-title>
            <v-card-text>
                <p class="active-analysis-dialog__description">
                    現在処理しているファイルと処理段階を表示しています。内容は自動更新されます。
                </p>
                <div v-if="detailItems.length > 0" class="active-analysis-dialog__items">
                    <article v-for="item in detailItems" :key="item.task.id" class="active-analysis-dialog__item">
                        <div class="active-analysis-dialog__item-heading">
                            <span class="active-analysis-dialog__status"
                                :class="`active-analysis-dialog__status--${item.task.status}`">
                                {{item.task.status === 'Running' ? '実行中' : '待機中'}}
                            </span>
                            <small v-if="item.root.id !== item.task.id && item.root.total_count > 0">
                                全体 {{item.root.current_count.toLocaleString()}} /
                                {{item.root.total_count.toLocaleString()}} 件
                            </small>
                        </div>
                        <h3>{{fileName(item.task.file_path) ?? item.task.title}}</h3>
                        <p v-if="item.task.file_path !== null" class="active-analysis-dialog__path">
                            {{item.task.file_path}}
                        </p>
                        <p v-else class="active-analysis-dialog__no-path">
                            対象ファイルはまだ確定していません。
                        </p>
                        <dl>
                            <dt>処理内容</dt>
                            <dd>{{taskTypeLabel(item.task.task_type)}}</dd>
                            <dt>現在の段階</dt>
                            <dd>{{stageLabel(item.task.stage)}}</dd>
                        </dl>
                        <v-progress-linear v-if="item.task.progress !== null" class="mt-3" color="primary" height="5"
                            rounded :model-value="item.task.progress * 100" />
                        <v-progress-linear v-else-if="item.task.status === 'Running'" class="mt-3" color="primary"
                            height="5" rounded indeterminate />
                    </article>
                </div>
                <div v-else class="active-analysis-dialog__completed">
                    <Icon icon="fluent:checkmark-circle-20-regular" width="28px" />
                    <span>この処理は完了しました。</span>
                </div>
            </v-card-text>
            <v-card-actions>
                <v-spacer />
                <v-btn variant="text" @click="$emit('update:modelValue', false)">閉じる</v-btn>
            </v-card-actions>
        </v-card>
    </v-dialog>
</template>

<script lang="ts" setup>

import { computed } from 'vue';

import { AnalysisTaskType, IAnalysisTaskExecution } from '@/services/AnalysisTasks';
import useAnalysisTasksStore, { stageLabel, taskTypeLabel } from '@/stores/AnalysisTasksStore';

interface IActiveAnalysisTaskDetailItem {
    root: IAnalysisTaskExecution;
    task: IAnalysisTaskExecution;
}

const props = defineProps<{
    modelValue: boolean;
    taskType: AnalysisTaskType | null;
}>();

defineEmits<{
    (event: 'update:modelValue', value: boolean): void;
}>();

const analysisTasksStore = useAnalysisTasksStore();

const dialogTitle = computed(() => props.taskType === null
    ? '実行中の処理'
    : taskTypeLabel(props.taskType));

const detailItems = computed<IActiveAnalysisTaskDetailItem[]>(() => {
    if (props.taskType === null) return [];

    // 一括処理ではルートにファイルパスがないため、現在実行中または待機中の子処理を優先して表示する。
    return analysisTasksStore.analysisOverview.active
        .filter(root => root.task_type === props.taskType)
        .flatMap((root) => {
            const activeChildren = analysisTasksStore.analysisOverview.active_children
                .filter(child => child.parent_id === root.id);
            const displayedTasks = activeChildren.length > 0 ? activeChildren : [root];
            return displayedTasks.map(task => ({root, task}));
        });
});

function fileName(filePath: string | null): string | null {
    if (filePath === null) return null;
    // Linux と Windows の両方で動作するため、両方のパス区切り文字を扱う。
    const segments = filePath.split(/[\\/]/).filter(segment => segment.length > 0);
    return segments[segments.length - 1] ?? filePath;
}

</script>

<style lang="scss" scoped>

.active-analysis-dialog {
    color: rgb(var(--v-theme-text));
    background: rgb(var(--v-theme-background-lighten-1));

    &__title {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;

        > span {
            display: flex;
            align-items: center;
            min-width: 0;
            gap: 9px;
            overflow: hidden;
            white-space: nowrap;
            text-overflow: ellipsis;
        }
    }

    &__description {
        margin-bottom: 14px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 13px;
    }

    &__items {
        display: flex;
        flex-direction: column;
        gap: 10px;
    }

    &__item {
        min-width: 0;
        padding: 14px;
        border-radius: 8px;
        background: rgb(var(--v-theme-background-lighten-2));

        h3 {
            margin: 9px 0 3px;
            overflow-wrap: anywhere;
            font-size: 16px;
        }

        dl {
            display: grid;
            grid-template-columns: 90px minmax(0, 1fr);
            gap: 5px 10px;
            margin-top: 12px;
            font-size: 13px;
        }

        dt {
            color: rgb(var(--v-theme-text-darken-1));
        }

        dd {
            min-width: 0;
            overflow-wrap: anywhere;
        }
    }

    &__item-heading {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;

        small {
            color: rgb(var(--v-theme-text-darken-1));
            font-size: 11px;
        }
    }

    &__status {
        padding: 3px 8px;
        border-radius: 999px;
        font-size: 11px;
        font-weight: 700;

        &--Running {
            color: rgb(var(--v-theme-primary-readable));
            background: rgb(var(--v-theme-primary) / 16%);
        }

        &--Queued {
            color: rgb(var(--v-theme-warning-readable));
            background: rgb(var(--v-theme-warning) / 16%);
        }
    }

    &__path {
        overflow-wrap: anywhere;
        word-break: break-all;
        color: rgb(var(--v-theme-text-darken-1));
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        font-size: 12px;
    }

    &__no-path {
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 12px;
    }

    &__completed {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 8px;
        padding: 36px 16px;
        color: rgb(var(--v-theme-text-darken-1));
    }
}

@include smartphone-vertical {
    .active-analysis-dialog {
        &__item {
            padding: 12px;

            dl {
                grid-template-columns: 80px minmax(0, 1fr);
            }
        }

        &__item-heading {
            align-items: flex-start;
            flex-direction: column;
            gap: 5px;
        }
    }
}

</style>
