import { defineStore } from 'pinia';

import AnalysisTasks, { AnalysisTaskType, IAnalysisTaskExecution, IAnalysisTaskOverview } from '@/services/AnalysisTasks';
import Utils from '@/utils';


export interface IActiveAnalysisTaskGroup {
    task_type: AnalysisTaskType;
    first: IAnalysisTaskExecution;
    running_count: number;
    queued_count: number;
    progress: number | null;
}

export type ActiveAnalysisTaskStatus = 'Running' | 'Queued' | null;


// Navigation と MyPage の両方が同時にマウントされても API ポーリングを重複させないため、
// タイマーと利用コンポーネント数はストアのインスタンスではなく、このモジュールで共有する。
let overviewPollingTimer: number | null = null;
let overviewPollingConsumers = 0;


export function taskTypeLabel(type: AnalysisTaskType): string {
    return {
        RecordedScan: '録画フォルダスキャン', MetadataAnalysis: 'メタデータ解析', PlaybackIndex: '再生索引作成',
        ThumbnailGeneration: 'サムネイル生成', CMAnalysis: 'CM区間解析', CMLogoGeneration: 'CMロゴ生成',
        BatchScan: '録画フォルダ一括スキャン', BatchMetadataReanalysis: '全件メタデータ再解析',
        BatchCMAnalysis: '全件CM再判定', BatchSeriesResolution: '既存録画シリーズ一括判定',
        BatchEpisodeResolution: '既存録画話数一括判定',
        BackgroundAnalysis: 'バックグラウンド一括解析',
    }[type];
}


export function stageLabel(stage: string | null): string {
    if (stage === null) return '処理中';
    return ({
        Queued: '待機中', Probing: 'メディア確認中', Scanning: '全編走査中', Finalizing: '確定中',
        LogoScanning: 'ロゴ走査中', LogoMatching: 'ロゴ照合中', Analyzing: '解析中', Generating: '生成中',
        LogoCatalogScanning: 'ロゴ一覧更新中', ProbingMedia: '解析対象確認中', PreparingMedia: '解析媒体準備中',
        IndexingMedia: '共有索引作成中', ChapterAnalyzing: '無音・シーン解析中', LogoAnalyzing: 'ロゴ解析中',
        HardwareFallback: 'CPU解析へ切替中', CombiningCM: 'CM区間統合中',
        Committing: '結果確定中', Processing: '処理中', Saving: '保存中',
    } as Record<string, string>)[stage] ?? stage;
}


const useAnalysisTasksStore = defineStore('analysisTasks', {
    state: () => ({
        analysisOverview: {active: [], active_children: [], recent: []} as IAnalysisTaskOverview,
    }),
    getters: {
        activeTaskGroups(state): IActiveAnalysisTaskGroup[] {
            const groups = new Map<AnalysisTaskType, IAnalysisTaskExecution[]>();

            // 同じ処理種別のタスクをまとめ、スマホ版と PC 版で同じ集約結果を表示する。
            for (const task of state.analysisOverview.active) {
                groups.set(task.task_type, [...(groups.get(task.task_type) ?? []), task]);
            }

            return [...groups.entries()].map(([task_type, tasks]) => {
                const knownProgress = tasks.map(task => task.progress).filter((value): value is number => value !== null);
                return {
                    task_type,
                    first: tasks.find(task => task.status === 'Running') ?? tasks[0]!,
                    running_count: tasks.filter(task => task.status === 'Running').length,
                    queued_count: tasks.filter(task => task.status === 'Queued').length,
                    progress: knownProgress.length > 0
                        ? knownProgress.reduce((sum, value) => sum + value, 0) / knownProgress.length
                        : null,
                };
            });
        },
        activeTaskStatus(state): ActiveAnalysisTaskStatus {
            if (state.analysisOverview.active.some(task => task.status === 'Running')) return 'Running';
            if (state.analysisOverview.active.some(task => task.status === 'Queued')) return 'Queued';
            return null;
        },
    },
    actions: {
        async updateAnalysisOverview(showError = false): Promise<void> {
            const overview = await AnalysisTasks.fetchOverview(showError);
            // 一時的な API エラーで実行中表示が突然消えないよう、正常取得時だけ状態を差し替える。
            // Docker の段階更新中など旧 API から応答された場合も、ルート処理の表示自体は維持する。
            if (overview !== null) {
                this.analysisOverview = {
                    ...overview,
                    active_children: overview.active_children ?? [],
                };
            }
        },
        startOverviewPolling(showError = false): void {
            // 未ログイン画面では認証必須 API を定期呼び出ししない。
            if (Utils.getAccessToken() === null) return;

            overviewPollingConsumers += 1;
            // 既に別コンポーネントが開始済みなら、そのタイマーとストア状態を共有する。
            if (overviewPollingTimer !== null) return;

            void this.updateAnalysisOverview(showError);
            overviewPollingTimer = window.setInterval(() => void this.updateAnalysisOverview(), 3000);
        },
        stopOverviewPolling(): void {
            overviewPollingConsumers = Math.max(0, overviewPollingConsumers - 1);
            // まだ利用中のコンポーネントがある間は共有タイマーを止めない。
            if (overviewPollingConsumers > 0 || overviewPollingTimer === null) return;

            window.clearInterval(overviewPollingTimer);
            overviewPollingTimer = null;
            // ログアウト後に別ユーザーへ前ユーザー権限で取得した概要を一瞬見せないよう、認証情報がなければ破棄する。
            if (Utils.getAccessToken() === null) {
                this.analysisOverview = {active: [], active_children: [], recent: []};
            }
        },
    },
});

export default useAnalysisTasksStore;
