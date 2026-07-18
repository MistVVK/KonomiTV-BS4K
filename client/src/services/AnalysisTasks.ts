import APIClient from '@/services/APIClient';


export type AnalysisTaskType =
    'RecordedScan' | 'MetadataAnalysis' | 'PlaybackIndex' | 'ThumbnailGeneration' | 'CMAnalysis' |
    'CMLogoGeneration' | 'BatchScan' | 'BatchMetadataReanalysis' | 'BatchCMAnalysis' | 'BackgroundAnalysis';
export type AnalysisTaskStatus = 'Queued' | 'Running' | 'Succeeded' | 'Failed' | 'Interrupted' | 'Skipped';

export interface IAnalysisTaskExecution {
    id: number;
    parent_id: number | null;
    recorded_video_id: number | null;
    task_type: AnalysisTaskType;
    status: AnalysisTaskStatus;
    trigger: 'Automatic' | 'Manual' | 'Maintenance' | 'StartupBackfill';
    title: string;
    stage: string | null;
    progress: number | null;
    stage_history: {stage: string; started_at: string; completed_at: string | null}[];
    current_count: number;
    total_count: number;
    succeeded_count: number;
    failed_count: number;
    skipped_count: number;
    summary: Record<string, unknown> | null;
    error_code: string | null;
    error_message: string | null;
    started_at: string | null;
    completed_at: string | null;
    created_at: string;
    updated_at: string;
}

export interface IAnalysisTaskOverview {
    active: IAnalysisTaskExecution[];
    recent: IAnalysisTaskExecution[];
}

export interface IAnalysisTaskList {
    total: number;
    page: number;
    page_size: number;
    items: IAnalysisTaskExecution[];
}

export interface IAnalysisTaskDetail {
    execution: IAnalysisTaskExecution;
    children: IAnalysisTaskExecution[];
    logo_attempts: {
        strategy: string;
        start_frame: number | null;
        start_seconds: number | null;
        frame_count: number;
        exit_code: number | null;
        match_ratio: number | null;
        failure_reason: string | null;
        created_at: string;
        completed_at: string | null;
    }[];
}

export default class AnalysisTasks {

    static async fetchOverview(show_error = false): Promise<IAnalysisTaskOverview | null> {
        const response = await APIClient.get<IAnalysisTaskOverview>('/analysis-tasks/overview');
        if (response.type === 'error') {
            if (show_error) APIClient.showGenericError(response, 'バックグラウンド処理を取得できませんでした。');
            return null;
        }
        return response.data;
    }

    static async fetchTasks(params: Record<string, string | number | undefined>): Promise<IAnalysisTaskList | null> {
        const response = await APIClient.get<IAnalysisTaskList>('/analysis-tasks', {params});
        if (response.type === 'error') {
            APIClient.showGenericError(response, '解析履歴を取得できませんでした。');
            return null;
        }
        return response.data;
    }

    static async fetchDetail(execution_id: number): Promise<IAnalysisTaskDetail | null> {
        const response = await APIClient.get<IAnalysisTaskDetail>(`/analysis-tasks/${execution_id}`);
        if (response.type === 'error') {
            APIClient.showGenericError(response, '解析履歴の詳細を取得できませんでした。');
            return null;
        }
        return response.data;
    }
}
