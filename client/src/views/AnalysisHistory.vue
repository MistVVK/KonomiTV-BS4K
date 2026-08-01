<template>
    <div class="route-container">
        <HeaderBar />
        <main>
            <Navigation />
            <SPHeaderBar :hide-on-smartphone-vertical="true" />
            <div class="history-container mx-auto">
                <div class="history-header">
                    <h1>解析履歴</h1>
                    <router-link to="/mypage/">マイページへ戻る</router-link>
                </div>
                <div class="history-filters">
                    <v-select v-model="selectedType" label="処理種別" :items="typeOptions" clearable hide-details />
                    <v-select v-model="selectedStatus" label="状態" :items="statusOptions" clearable hide-details />
                    <v-text-field v-model="keyword" label="番組名・ファイルパスを検索" clearable hide-details />
                </div>
                <div v-if="tasks.length === 0" class="history-empty">該当する解析履歴はありません。</div>
                <button v-for="task in tasks" :key="task.id" class="history-row" @click="openDetail(task.id)">
                    <Icon :icon="statusIcon(task.status)" width="23px" :class="`status--${task.status}`" />
                    <div class="history-row__body">
                        <div class="history-row__heading">
                            <strong>{{taskTypeLabel(task.task_type)}}</strong>
                            <small>{{dateLabel(task.completed_at ?? task.started_at)}}</small>
                        </div>
                        <span>{{task.title}}</span>
                        <small v-if="task.file_path" class="history-row__path">{{task.file_path}}</small>
                        <small>{{resultLabel(task)}}・{{durationLabel(task)}}</small>
                        <v-progress-linear v-if="task.status === 'Running' && task.progress !== null" class="mt-2"
                            color="primary" height="4" rounded :model-value="task.progress * 100" />
                    </div>
                </button>
                <v-pagination v-if="pageCount > 1" v-model="page" class="mt-4" :length="pageCount" />
            </div>
        </main>

        <v-dialog v-model="detailDialog" max-width="760">
            <v-card v-if="detail !== null" class="detail-card">
                <v-card-title>{{taskTypeLabel(detail.execution.task_type)}}</v-card-title>
                <v-card-text>
                    <h3>{{detail.execution.title}}</h3>
                    <dl>
                        <dt>状態</dt><dd>{{statusLabel(detail.execution.status)}}</dd>
                        <dt>ファイル</dt>
                        <dd class="detail-path">{{detail.execution.file_path || '（パスなし）'}}</dd>
                        <dt>開始</dt><dd>{{dateLabel(detail.execution.started_at, true)}}</dd>
                        <dt>終了</dt><dd>{{dateLabel(detail.execution.completed_at, true) || '実行中'}}</dd>
                        <dt>所要時間</dt><dd>{{durationLabel(detail.execution)}}</dd>
                        <template v-if="detail.execution.error_code">
                            <dt>エラー</dt><dd>{{detail.execution.error_code}}</dd>
                        </template>
                    </dl>
                    <p v-if="detail.execution.error_message" class="detail-error">{{detail.execution.error_message}}</p>
                    <h4 v-if="detail.execution.stage_history.length">処理段階</h4>
                    <ol class="detail-stages">
                        <li v-for="(stage, index) in detail.execution.stage_history" :key="index">
                            {{stageLabel(stage.stage)}}
                            <small>{{dateLabel(stage.started_at, true)}}</small>
                        </li>
                    </ol>
                    <template v-if="detail.children.length">
                        <h4>録画ごとの結果</h4>
                        <div v-for="child in detail.children" :key="child.id" class="detail-child">
                            <div class="detail-child__body">
                                <span>{{child.title}}</span>
                                <small v-if="child.file_path" class="detail-path">{{child.file_path}}</small>
                            </div>
                            <small>{{statusLabel(child.status)}}{{child.error_code ? `・${child.error_code}` : ''}}</small>
                        </div>
                    </template>
                    <template v-if="detail.logo_attempts.length">
                        <h4>ロゴ生成試行</h4>
                        <div v-for="(attempt, index) in detail.logo_attempts" :key="index" class="detail-child">
                            <span>{{attempt.strategy}}・{{attempt.frame_count.toLocaleString()}}フレーム</span>
                            <small>{{attempt.match_ratio !== null ? `一致率 ${(attempt.match_ratio * 100).toFixed(1)}%` : ''}}
                                {{attempt.failure_reason ? `・${attempt.failure_reason}` : ''}}</small>
                        </div>
                    </template>
                </v-card-text>
                <v-card-actions><v-spacer /><v-btn @click="detailDialog = false">閉じる</v-btn></v-card-actions>
            </v-card>
        </v-dialog>
    </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import HeaderBar from '@/components/HeaderBar.vue';
import Navigation from '@/components/Navigation.vue';
import SPHeaderBar from '@/components/SPHeaderBar.vue';
import AnalysisTasks, { AnalysisTaskStatus, AnalysisTaskType, IAnalysisTaskDetail, IAnalysisTaskExecution } from '@/services/AnalysisTasks';
import { dayjs } from '@/utils';

const route = useRoute();
const router = useRouter();
const tasks = ref<IAnalysisTaskExecution[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = 30;
const selectedType = ref<AnalysisTaskType | null>(null);
const selectedStatus = ref<AnalysisTaskStatus | null>(null);
const keyword = ref('');
const detail = ref<IAnalysisTaskDetail | null>(null);
const detailDialog = ref(false);
let pollingTimer: number | null = null;
let keywordTimer: number | null = null;

const pageCount = computed(() => Math.ceil(total.value / pageSize));
const typeOptions = [
    {title: '録画フォルダスキャン', value: 'RecordedScan'}, {title: 'メタデータ解析', value: 'MetadataAnalysis'},
    {title: '再生索引作成', value: 'PlaybackIndex'}, {title: 'サムネイル生成', value: 'ThumbnailGeneration'},
    {title: 'CM区間解析', value: 'CMAnalysis'}, {title: 'CMロゴ生成', value: 'CMLogoGeneration'},
    {title: '全件メタデータ再解析', value: 'BatchMetadataReanalysis'}, {title: '全件CM再判定', value: 'BatchCMAnalysis'},
    {title: '既存録画シリーズ一括判定', value: 'BatchSeriesResolution'},
    {title: '既存録画話数一括判定', value: 'BatchEpisodeResolution'},
    {title: 'バックグラウンド一括解析', value: 'BackgroundAnalysis'},
];
const statusOptions = [
    {title: '待機中', value: 'Queued'}, {title: '実行中', value: 'Running'}, {title: '成功', value: 'Succeeded'},
    {title: '失敗', value: 'Failed'}, {title: '中断', value: 'Interrupted'}, {title: 'スキップ', value: 'Skipped'},
];

function taskTypeLabel(type: AnalysisTaskType): string {
    return typeOptions.find(item => item.value === type)?.title ?? type;
}
function statusLabel(status: AnalysisTaskStatus): string {
    return statusOptions.find(item => item.value === status)?.title ?? status;
}
function statusIcon(status: AnalysisTaskStatus): string {
    if (status === 'Succeeded') return 'fluent:checkmark-circle-20-filled';
    if (status === 'Failed') return 'fluent:error-circle-20-filled';
    if (status === 'Interrupted') return 'fluent:pause-circle-20-filled';
    if (status === 'Skipped') return 'fluent:arrow-skip-forward-20-filled';
    return 'fluent:clock-20-regular';
}
function stageLabel(stage: string): string {
    return ({Probing: 'メディア確認', Scanning: '全編走査', Finalizing: '結果確定', LogoScanning: 'ロゴ走査',
        LogoMatching: 'ロゴ照合', Analyzing: 'CM解析', Generating: '生成', Committing: 'chapter確定',
        LogoCatalogScanning: 'ロゴ一覧更新', ProbingMedia: '解析対象確認', PreparingMedia: '解析媒体準備',
        IndexingMedia: '共有索引作成', ChapterAnalyzing: '無音・シーン解析', LogoAnalyzing: 'ロゴ解析',
        HardwareFallback: 'CPU解析へ切替', CombiningCM: 'CM区間統合',
        Processing: '一括処理', Saving: '保存', LegacyImport: '既存履歴'} as Record<string, string>)[stage] ?? stage;
}
function dateLabel(value: string | null, seconds = false): string {
    if (!value) return '';
    return dayjs(value).format(seconds ? 'YYYY/M/D HH:mm:ss' : 'YYYY/M/D HH:mm');
}
function durationLabel(task: IAnalysisTaskExecution): string {
    if (!task.started_at) return '開始待ち';
    const end = task.completed_at ? dayjs(task.completed_at) : dayjs();
    const seconds = Math.max(0, end.diff(dayjs(task.started_at), 'second'));
    return seconds >= 3600 ? `${Math.floor(seconds / 3600)}時間${Math.floor(seconds % 3600 / 60)}分` : `${Math.floor(seconds / 60)}分${seconds % 60}秒`;
}
function summaryCount(task: IAnalysisTaskExecution, key: string): number | null {
    const value = task.summary?.[key];
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}
function episodeResolutionResultLabel(task: IAnalysisTaskExecution): string {
    const resolved_count = summaryCount(task, 'resolved');
    const not_numbered_count = summaryCount(task, 'not_numbered');
    const needs_review_count = summaryCount(task, 'needs_review');
    if (resolved_count !== null && not_numbered_count !== null && needs_review_count !== null) {
        const labels = [
            `${task.current_count}/${task.total_count}件`,
            `話数確定${resolved_count}`,
            `公式話数なし${not_numbered_count}`,
            `要確認${needs_review_count}`,
        ];
        const failed_count = summaryCount(task, 'failed') ?? task.failed_count;
        const skipped_count = summaryCount(task, 'skipped') ?? task.skipped_count;
        const preserved_count = summaryCount(task, 'preserved') ?? 0;
        if (failed_count > 0) labels.push(`失敗${failed_count}`);
        if (skipped_count > 0) labels.push(`スキップ${skipped_count}`);
        if (preserved_count > 0) labels.push(`旧値維持${preserved_count}`);
        return labels.join('・');
    }

    // 旧履歴と実行中の履歴は内訳を持たないため、要確認を成功と誤表示しない表現で件数を表示する。
    if (task.total_count > 0) {
        return `${task.current_count}/${task.total_count}件・処理済み${task.succeeded_count}・失敗${task.failed_count}` +
            (task.skipped_count > 0 ? `・スキップ${task.skipped_count}` : '');
    }
    return statusLabel(task.status);
}
function resultLabel(task: IAnalysisTaskExecution): string {
    if (task.task_type === 'BatchEpisodeResolution') return episodeResolutionResultLabel(task);
    if (task.status === 'Failed') return `失敗${task.error_code ? `・${task.error_code}` : ''}`;
    if (task.total_count > 0) return `${task.current_count}/${task.total_count}件・成功${task.succeeded_count}・失敗${task.failed_count}`;
    return statusLabel(task.status);
}
async function fetchTasks(): Promise<void> {
    const result = await AnalysisTasks.fetchTasks({page: page.value, page_size: pageSize,
        task_type: selectedType.value ?? undefined, status: selectedStatus.value ?? undefined,
        keyword: keyword.value || undefined});
    if (result !== null) { tasks.value = result.items; total.value = result.total; }
}
async function openDetail(id: number): Promise<void> {
    const result = await AnalysisTasks.fetchDetail(id);
    if (result === null) return;
    detail.value = result; detailDialog.value = true;
    await router.replace({query: {...route.query, task: String(id)}});
}
watch(page, () => void fetchTasks());
watch([selectedType, selectedStatus], () => {
    if (page.value !== 1) page.value = 1;
    else void fetchTasks();
});
watch(keyword, () => {
    if (keywordTimer !== null) window.clearTimeout(keywordTimer);
    keywordTimer = window.setTimeout(() => { page.value = 1; void fetchTasks(); }, 300);
});
watch(detailDialog, value => { if (!value && route.query.task) void router.replace({query: {}}); });
onMounted(async () => {
    await fetchTasks();
    const task = Number(route.query.task);
    if (Number.isInteger(task) && task > 0) await openDetail(task);
    pollingTimer = window.setInterval(() => void fetchTasks(), 10000);
});
onUnmounted(() => {
    if (pollingTimer !== null) window.clearInterval(pollingTimer);
    if (keywordTimer !== null) window.clearTimeout(keywordTimer);
});
</script>

<style lang="scss" scoped>
.history-container { width: calc(100% - 40px); max-width: 960px; padding: 24px 0 50px; color: rgb(var(--v-theme-text)); }
.history-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 18px; }
.history-header h1 { font-size: 24px; }
.history-header a { color: rgb(var(--v-theme-primary)); font-size: 13px; }
.history-filters { display: grid; grid-template-columns: 1fr 1fr 2fr; gap: 10px; margin-bottom: 14px; }
.history-row { display: flex; width: 100%; gap: 10px; padding: 13px; margin-bottom: 8px; border-radius: 8px;
    text-align: left; color: rgb(var(--v-theme-text)); background: rgb(var(--v-theme-background-lighten-1)); }
.history-row__body { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.history-row__body > span { margin: 2px 0; overflow-wrap: anywhere; }
.history-row__body small { color: rgb(var(--v-theme-text-darken-1)); }
.history-row__path, .detail-path {
    display: block;
    overflow-wrap: anywhere;
    word-break: break-all;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12px;
    opacity: 0.9;
}
.history-row__heading { display: flex; justify-content: space-between; gap: 8px; }
.history-row__heading small { flex-shrink: 0; }
.history-empty { padding: 40px; text-align: center; opacity: 0.65; }
.status--Succeeded { color: rgb(var(--v-theme-success-readable)); }
.status--Failed { color: rgb(var(--v-theme-error-readable)); }
.status--Interrupted, .status--Skipped { color: rgb(var(--v-theme-warning-readable)); }
.detail-card { background: rgb(var(--v-theme-background-lighten-1)); }
.detail-card h3 { margin-bottom: 12px; overflow-wrap: anywhere; }
.detail-card h4 { margin: 18px 0 8px; }
.detail-card dl { display: grid; grid-template-columns: 90px minmax(0, 1fr); gap: 6px; }
.detail-card dt { color: rgb(var(--v-theme-text-darken-1)); }
.detail-error { padding: 10px; margin-top: 12px; border-radius: 5px; overflow-wrap: anywhere; white-space: pre-wrap; background: rgb(var(--v-theme-error) / 12%); }
.detail-stages { padding-left: 22px; } .detail-stages li { margin: 5px 0; }
.detail-stages small { margin-left: 8px; color: rgb(var(--v-theme-text-darken-1)); }
.detail-child { display: flex; justify-content: space-between; gap: 10px; padding: 7px 0; border-bottom: 1px solid rgb(var(--v-theme-background-lighten-2)); }
.detail-child__body { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.detail-child span { overflow-wrap: anywhere; } .detail-child small { flex-shrink: 0; color: rgb(var(--v-theme-text-darken-1)); }
@include smartphone-vertical { .history-container { width: calc(100% - 28px); padding-top: 18px; } .history-filters { grid-template-columns: 1fr; }
    .detail-child { flex-direction: column; gap: 2px; } }
</style>
