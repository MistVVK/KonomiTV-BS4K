
import type { IAnalysisTaskAccepted } from '@/services/AnalysisTasks';
import type { RecordedStreamingAudioCodec } from '@/stores/SettingsStore';

import APIClient from  '@/services/APIClient';
import { IChannel } from '@/services/Channels';
import { CommentUtils } from '@/utils';

/** ソート順序を表す型 */
export type SortOrder = 'desc' | 'asc';

/** マイリストのソート順序を表す型 */
export type MylistSortOrder = 'mylist_added_desc' | 'mylist_added_asc' | 'recorded_desc' | 'recorded_asc';

export interface IRecordedPlaybackCapability {
    encoder: 'FFmpeg' | 'QSVEncC' | 'NVEncC' | 'VCEEncC';
    codec: 'avc' | 'hevc' | 'vp9' | 'av1';
    bit_depth: 8 | 10;
    available: boolean;
    profile: string;
    reason_code: string | null;
}

export interface IRecordedPlaybackCodecOption {
    title: string;
    value: 'avc' | 'hevc' | 'vp9' | 'av1';
    props: {disabled: boolean};
}

export interface IRecordedPlaybackAudioCodecOption {
    title: string;
    value: RecordedStreamingAudioCodec;
    props: {disabled: boolean};
}

export interface IRecordedPlaybackIndex {
    status: 'Pending' | 'Analyzing' | 'Ready' | 'Failed';
    state: 'Pending' | 'Analyzing' | 'Ready' | 'Stale' | 'Failed';
    version: number | null;
    current_version: number;
    indexed_at: string | null;
    error_code: string | null;
    progress: number | null;
    stage: 'Queued' | 'Probing' | 'Scanning' | 'Finalizing' | 'Complete' | 'Failed';
}

/** 録画ファイル情報を表すインターフェース */
export interface IAudioTrack {
    index: number;
    codec: string;
    channel: string;
    sampling_rate: number | null;
    language: string | null;
    stream_index?: number;
    title?: string | null;
    channel_layout?: string | null;
    is_dual_mono?: boolean;
    pid?: number;
}

export interface IAudioTrackTimelineEntry {
    start_time: number;
    end_time: number;
    tracks: IAudioTrack[];
}

export interface ISubtitleTrack {
    index: number;
    codec: string;
    language: string | null;
    title: string | null;
    stream_index?: number;
    pid?: number;
    component_tag?: number;
    program_number?: number;
}

/** 録画ファイル情報を表すインターフェース */
export interface IRecordedVideo {
    id: number;
    status: 'Recording' | 'Analyzing' | 'Recorded' | 'AnalysisFailed';
    file_path: string;
    file_hash: string;
    file_size: number;
    file_created_at: string;
    file_modified_at: string;
    analyzed_at: string | null;
    analysis_git_commit: string | null;
    playback_index_status: 'Pending' | 'Analyzing' | 'Ready' | 'Failed';
    playback_index_state: 'Pending' | 'Analyzing' | 'Ready' | 'Stale' | 'Failed';
    playback_index_version: number | null;
    playback_index_current_version: number;
    playback_indexed_at: string | null;
    playback_index_error_code: string | null;
    recording_start_time: string | null;
    recording_end_time: string | null;
    duration: number;
    container_format: string;
    has_video: boolean;
    has_audio: boolean;
    video_codec: string | null;
    video_codec_profile: string | null;
    video_scan_type: 'Interlaced' | 'Progressive' | null;
    video_frame_rate: number | null;
    video_resolution_width: number | null;
    video_resolution_height: number | null;
    video_sample_aspect_ratio: string | null;
    video_display_aspect_ratio: string | null;
    has_video_stream_changes: boolean;
    primary_audio_codec: string | null;
    primary_audio_channel: string | null;
    primary_audio_sampling_rate: number | null;
    secondary_audio_codec: string | null;
    secondary_audio_channel: string | null;
    secondary_audio_sampling_rate: number | null;
    audio_tracks: IAudioTrack[];
    audio_track_timeline: IAudioTrackTimelineEntry[];
    subtitle_tracks: ISubtitleTrack[];
    cm_sections: { start_time: number; end_time: number; }[] | null;
    cm_analysis_status: 'Pending' | 'Analyzing' | 'Completed' | 'Failed' | 'Unsupported' | 'Excluded' | 'Interrupted' | null;
    cm_analysis_error_code: string | null;
    cm_analysis_finished_at: string | null;
    cm_result_source: 'Existing' | 'Generated' | 'LegacyImported' | 'Manual' | null;
    cm_result_verified: boolean | null;
    cm_result_chapter_path_kind: 'Canonical' | 'Legacy' | null;
    cm_result_pipeline_version: string | null;
    cm_result_published_at: string | null;
    thumbnail_info: IThumbnailInfo | null;
    created_at: string;
    updated_at: string;
}

/** サムネイル情報を表すインターフェース */
export interface IThumbnailInfo {
    version: number;
    representative: IThumbnailImageInfo;
    tile: IThumbnailTileInfo;
}

/** 代表サムネイル情報を表すインターフェース */
export interface IThumbnailImageInfo {
    format: 'WebP';
    width: number;
    height: number;
}

/** サムネイルタイル情報を表すインターフェース */
export interface IThumbnailTileInfo {
    format: 'WebP';
    image_width: number;
    image_height: number;
    tile_width: number;
    tile_height: number;
    total_tiles: number;
    column_count: number;
    row_count: number;
    interval_sec: number;
}

/** 録画ファイル情報を表すインターフェースのデフォルト値 */
export const IRecordedVideoDefault: IRecordedVideo = {
    id: -1,
    status: 'Recorded',
    file_path: '',
    file_hash: '',
    file_size: 0,
    file_created_at: '2000-01-01T00:00:00+09:00',
    file_modified_at: '2000-01-01T00:00:00+09:00',
    analyzed_at: null,
    analysis_git_commit: null,
    playback_index_status: 'Pending',
    playback_index_state: 'Pending',
    playback_index_version: null,
    playback_index_current_version: 11,
    playback_indexed_at: null,
    playback_index_error_code: null,
    recording_start_time: null,
    recording_end_time: null,
    duration: 0,
    container_format: 'MPEG-TS',
    has_video: true,
    has_audio: true,
    video_codec: 'MPEG-2',
    video_codec_profile: 'High',
    video_scan_type: 'Interlaced',
    video_frame_rate: 29.97,
    video_resolution_width: 1440,
    video_resolution_height: 1080,
    video_sample_aspect_ratio: '4:3',
    video_display_aspect_ratio: '16:9',
    has_video_stream_changes: false,
    primary_audio_codec: 'AAC-LC',
    primary_audio_channel: 'Stereo',
    primary_audio_sampling_rate: 48000,
    secondary_audio_codec: null,
    secondary_audio_channel: null,
    secondary_audio_sampling_rate: null,
    audio_tracks: [],
    audio_track_timeline: [],
    subtitle_tracks: [],
    cm_sections: null,
    cm_analysis_status: null,
    cm_analysis_error_code: null,
    cm_analysis_finished_at: null,
    cm_result_source: null,
    cm_result_verified: null,
    cm_result_chapter_path_kind: null,
    cm_result_pipeline_version: null,
    cm_result_published_at: null,
    thumbnail_info: null,
    created_at: '2000-01-01T00:00:00+09:00',
    updated_at: '2000-01-01T00:00:00+09:00',
};

/** 録画番組情報を表すインターフェース */
export interface IRecordedProgram {
    id: number;
    recorded_video: IRecordedVideo;
    recording_start_margin: number;
    recording_end_margin: number;
    is_partially_recorded: boolean;
    channel: IChannel | null;
    network_id: number | null;
    service_id: number | null;
    event_id: number | null;
    series_id: number | null;
    series_broadcast_period_id: number | null;
    title: string;
    series_title: string | null;
    episode_number: string | null;
    subtitle: string | null;
    description: string;
    detail: { [key: string]: string };
    start_time: string;
    end_time: string;
    duration: number;
    is_free: boolean;
    genres: { major: string; middle: string; }[];
    primary_audio_type: string;
    primary_audio_language: string;
    secondary_audio_type: string | null;
    secondary_audio_language: string | null;
    created_at: string;
    updated_at: string;
}

/** 録画番組情報を表すインターフェースのデフォルト値 */
export const IRecordedProgramDefault: IRecordedProgram = {
    id: -1,
    recorded_video: IRecordedVideoDefault,
    recording_start_margin: 0,
    recording_end_margin: 0,
    is_partially_recorded: false,
    channel: null,
    network_id: null,
    service_id: null,
    event_id: null,
    series_id: null,
    series_broadcast_period_id: null,
    title: '取得中…',
    series_title: null,
    episode_number: null,
    subtitle: null,
    description: '取得中…',
    detail: {},
    start_time: '2000-01-01T00:00:00+09:00',
    end_time: '2000-01-01T00:00:00+09:00',
    duration: 0,
    is_free: true,
    genres: [],
    primary_audio_type: '2/0モード(ステレオ)',
    primary_audio_language: '日本語',
    secondary_audio_type: null,
    secondary_audio_language: null,
    created_at: '2000-01-01T00:00:00+09:00',
    updated_at: '2000-01-01T00:00:00+09:00',
};

/** 録画番組情報リストを表すインターフェース */
export interface IRecordedPrograms {
    total: number;
    recorded_programs: IRecordedProgram[];
}

/** 過去ログコメントを表すインターフェース */
export interface IJikkyoComment {
    time: number;
    type: 'top' | 'right' | 'bottom';
    size: 'big' | 'medium' | 'small';
    color: string;
    author: string;
    text: string;
}

/** 過去ログコメントのリストを表すインターフェース */
export interface IJikkyoComments {
    is_success: boolean;
    comments: IJikkyoComment[];
    detail: string;
}


class Videos {

    /** 画面遷移時に即座に解除できるポーリング間隔。 */
    private static async waitForPollingInterval(signal: AbortSignal): Promise<boolean> {
        return await new Promise<boolean>((resolve) => {
            const on_abort = () => {
                window.clearTimeout(timeout_id);
                resolve(false);
            };
            const timeout_id = window.setTimeout(() => {
                signal.removeEventListener('abort', on_abort);
                resolve(true);
            }, 1000);
            signal.addEventListener('abort', on_abort, {once: true});
        });
    }

    /** 軽量メタデータ解析が完了または失敗するまで録画情報を監視する。 */
    static async waitForRecordedMetadata(
        recorded_program: IRecordedProgram,
        on_update: (program: IRecordedProgram) => void,
        signal: AbortSignal,
    ): Promise<IRecordedProgram | null> {
        let current_program = recorded_program;
        while (
            current_program.recorded_video.status !== 'Recorded' &&
            current_program.recorded_video.status !== 'AnalysisFailed'
        ) {
            if (await this.waitForPollingInterval(signal) === false) return null;
            const fetched_program = await this.fetchVideo(current_program.id, signal, false);
            if (fetched_program === null) continue;
            current_program = fetched_program;
            on_update(current_program);
        }
        return current_program;
    }

    /** 録画再生索引の生成を最優先で要求する。 */
    static async requestRecordedPlaybackIndex(
        video_id: number,
        signal?: AbortSignal,
    ): Promise<IRecordedPlaybackIndex | null> {
        const response = await APIClient.post<IRecordedPlaybackIndex>(`/videos/${video_id}/playback-index`, undefined, {
            signal,
        });
        return response.type === 'success' ? response.data : null;
    }

    /** 録画再生索引の現在状態を取得する。 */
    static async fetchRecordedPlaybackIndex(
        video_id: number,
        signal?: AbortSignal,
    ): Promise<IRecordedPlaybackIndex | null> {
        const response = await APIClient.get<IRecordedPlaybackIndex>(`/videos/${video_id}/playback-index`, {signal});
        return response.type === 'success' ? response.data : null;
    }

    /**
     * 録画再生索引を要求し、再生可能または解析失敗になるまで状態を監視する。
     * HTTPプレイリスト要求を待たせず、呼び出し側がプレイヤー生成前に待機表示できるようにする。
     */
    static async waitForRecordedPlaybackIndex(
        video_id: number,
        on_update: (index: IRecordedPlaybackIndex) => void,
        signal: AbortSignal,
    ): Promise<IRecordedPlaybackIndex | null> {
        let index = await this.requestRecordedPlaybackIndex(video_id, signal);
        if (index === null || signal.aborted === true) return null;
        on_update(index);
        let poll_count = 0;

        while (index.state !== 'Ready' && index.state !== 'Failed') {
            // 画面遷移時はタイマーも直ちに解除し、別録画の状態をStoreへ書き戻さない。
            const should_continue = await this.waitForPollingInterval(signal);
            if (should_continue === false) return null;

            const fetched_index = await this.fetchRecordedPlaybackIndex(video_id, signal);
            if (fetched_index === null) {
                // 一時的な通信失敗では解析ジョブ自体を止めず、次回ポーリングで復旧を試みる。
                continue;
            }
            index = fetched_index;
            on_update(index);
            poll_count += 1;

            // サーバー再起動でメモリ上の優先度付きキューが失われても待機画面が止まらないよう、
            // Pending・Staleが続く間だけ10秒ごとに冪等な生成要求を再送する。
            if (
                poll_count % 10 === 0 &&
                (index.state === 'Pending' || index.state === 'Stale')
            ) {
                const requested_index = await this.requestRecordedPlaybackIndex(video_id, signal);
                if (requested_index !== null) {
                    index = requested_index;
                    on_update(index);
                }
            }
        }
        return index;
    }

    /** 録画用 FFmpeg 8 の実機能力行列を取得する。 */
    static async fetchRecordedPlaybackCapabilities(): Promise<IRecordedPlaybackCapability[]> {
        const response = await APIClient.get<IRecordedPlaybackCapability[]>('/streams/video/capabilities');
        if (response.type === 'error') {
            return [];
        }
        return response.data;
    }

    /** サーバーとブラウザの積集合から、理由付きの録画コーデック選択肢を作る。 */
    static async buildRecordedPlaybackCodecOptions(
        encoder: IRecordedPlaybackCapability['encoder'],
    ): Promise<IRecordedPlaybackCodecOption[]> {
        const capabilities = await this.fetchRecordedPlaybackCapabilities();
        const media_source = (
            window as Window & {ManagedMediaSource?: {isTypeSupported: (mime_type: string) => boolean}}
        ).ManagedMediaSource ?? window.MediaSource;
        const definitions: Record<
        IRecordedPlaybackCodecOption['value'],
        {title: string; mime_types: Partial<Record<8 | 10, string>>}
        > = {
            avc: {title: 'H.264 / AVC（互換性優先）', mime_types: {8: 'video/mp4; codecs="avc1.640028"'}},
            hevc: {title: 'H.265 / HEVC', mime_types: {
                8: 'video/mp4; codecs="hvc1.1.6.L153.B0"',
                10: 'video/mp4; codecs="hvc1.2.4.L153.B0"',
            }},
            vp9: {title: 'Google VP9', mime_types: {
                8: 'video/mp4; codecs="vp09.00.40.08"',
                10: 'video/mp4; codecs="vp09.02.40.10"',
            }},
            av1: {title: 'Alliance for Open Media AV1', mime_types: {
                8: 'video/mp4; codecs="av01.0.08M.08"',
                10: 'video/mp4; codecs="av01.0.10M.10"',
            }},
        };
        return (Object.keys(definitions) as IRecordedPlaybackCodecOption['value'][]).map((codec) => {
            const server_capabilities = capabilities.filter((capability) =>
                capability.encoder === encoder && capability.codec === codec,
            );
            const server_available = server_capabilities.some((capability) => capability.available === true);
            const jointly_available = server_capabilities.some((capability) =>
                capability.available === true &&
                media_source?.isTypeSupported(definitions[codec].mime_types[capability.bit_depth] ?? '') === true,
            );
            const reason = server_available === false ?
                (server_capabilities.find((capability) => capability.reason_code !== null)?.reason_code ?? 'ProbeFailed') :
                (jointly_available === false ? 'BrowserUnsupported' : null);
            return {
                title: `${definitions[codec].title}${reason !== null ? `（${reason}）` : ''}`,
                value: codec,
                props: {disabled: reason !== null},
            };
        });
    }

    /** このブラウザが fMP4 コンテナ内の Opus 音声を再生できるかどうかを返す。 */
    static isOpusAudioSupported(): boolean {
        const media_source = (
            window as Window & {ManagedMediaSource?: {isTypeSupported: (mime_type: string) => boolean}}
        ).ManagedMediaSource ?? window.MediaSource;
        return media_source?.isTypeSupported('audio/mp4; codecs="opus"') === true;
    }

    /** ブラウザの対応状況を反映した録画音声コーデックの選択肢を作る。 */
    static buildRecordedPlaybackAudioCodecOptions(): IRecordedPlaybackAudioCodecOption[] {
        const is_opus_supported = this.isOpusAudioSupported();
        return [
            {title: 'AAC（互換性優先）', value: 'aac', props: {disabled: false}},
            {
                title: `Opus（音質・圧縮効率優先）${is_opus_supported ? '' : '（ブラウザ非対応）'}`,
                value: 'opus',
                props: {disabled: is_opus_supported === false},
            },
        ];
    }

    /**
     * 録画番組一覧を取得する
     * @param order ソート順序 ('desc' or 'asc' or 'ids')
     * @param page ページ番号
     * @param ids 録画番組の ID のリスト
     * @returns 録画番組一覧情報 or 録画番組一覧情報の取得に失敗した場合は null
     */
    static async fetchVideos(order: 'desc' | 'asc' | 'ids' = 'desc', page: number = 1, ids: number[] | null = null): Promise<IRecordedPrograms | null> {

        // API リクエストを実行
        const response = await APIClient.get<IRecordedPrograms>('/videos', {
            params: {
                order,
                page,
                ids,
            },
            // 録画番組の ID のリストを FastAPI が受け付ける &ids=1&ids=2&ids=3&... の形式にエンコードする
            // ref: https://github.com/axios/axios/issues/5058#issuecomment-1272107602
            paramsSerializer: {
                indexes: null,
            },
        });

        // エラー処理
        if (response.type === 'error') {
            APIClient.showGenericError(response, '録画番組一覧を取得できませんでした。');
            return null;
        }

        return response.data;
    }


    /**
     * 録画番組を検索する
     * @param query 検索キーワード
     * @param order ソート順序 ('desc' or 'asc')
     * @param page ページ番号
     * @returns 検索結果の録画番組一覧情報 or 検索に失敗した場合は null
     */
    static async searchVideos(query: string, order: 'desc' | 'asc' = 'desc', page: number = 1): Promise<IRecordedPrograms | null> {

        // API リクエストを実行
        const response = await APIClient.get<IRecordedPrograms>('/videos/search', {
            params: {
                query,
                order,
                page,
            },
        });

        // エラー処理
        if (response.type === 'error') {
            APIClient.showGenericError(response, '録画番組の検索に失敗しました。');
            return null;
        }

        return response.data;
    }


    /**
     * 録画番組情報を取得する
     * @param video_id 録画番組の ID
     * @returns 録画番組情報 or 録画番組情報の取得に失敗した場合は null
     */
    static async fetchVideo(
        video_id: number,
        signal?: AbortSignal,
        show_error: boolean = true,
    ): Promise<IRecordedProgram | null> {

        // API リクエストを実行
        const response = await APIClient.get<IRecordedProgram>(`/videos/${video_id}`, {signal});

        // エラー処理
        if (response.type === 'error') {
            if (show_error) APIClient.showGenericError(response, '録画番組情報を取得できませんでした。');
            return null;
        }

        return response.data;
    }


    /**
     * 録画番組の放送中に投稿されたニコニコ実況の過去ログコメントを取得する
     * @param video_id 録画番組の ID
     * @returns 過去ログコメントのリスト
     */
    static async fetchVideoJikkyoComments(video_id: number): Promise<IJikkyoComments> {

        // API リクエストを実行
        const response = await APIClient.get<IJikkyoComments>(`/videos/${video_id}/jikkyo`);

        // エラー処理
        if (response.type === 'error') {
            APIClient.showGenericError(response, '過去ログコメントを取得できませんでした。');
            return {
                is_success: false,
                comments: [],
                detail: '過去ログコメントを取得できませんでした。',
            };
        }

        // ミュート対象のコメントを除外して返す
        response.data.comments = response.data.comments.filter((comment) => {
            return CommentUtils.isMutedComment(comment.text, comment.author, comment.color, comment.type, comment.size) === false;
        });
        return response.data;
    }


    /**
     * 録画番組のメタデータを再解析する
     * @param video_id 録画番組の ID
     * @returns メタデータ再解析に成功した場合は true
     */
    static async reanalyzeVideo(video_id: number): Promise<boolean> {

        // API リクエストを実行
        const response = await APIClient.post(`/videos/${video_id}/reanalyze`, undefined, {
            // 数分以上かかるのでタイムアウトを 10 分に設定
            timeout: 10 * 60 * 1000,
        });

        // エラー処理
        if (response.type === 'error') {
            switch (response.data.detail) {
                default:
                    APIClient.showGenericError(response, 'メタデータの再解析に失敗しました。');
                    break;
            }
            return false;
        }

        return true;
    }


    /**
     * 録画番組の CM 区間を再判定する
     * @param video_id 録画番組の ID
     * @param signal 画面遷移時に受付リクエストだけを中断する AbortSignal
     * @returns 受け付けた解析履歴、または受付に失敗した場合は null
     */
    static async detectCMSections(
        video_id: number,
        signal?: AbortSignal,
    ): Promise<IAnalysisTaskAccepted | null> {

        const response = await APIClient.post<IAnalysisTaskAccepted>(
            `/videos/${video_id}/detect-cm-sections?replace_existing_chapter=true`,
            undefined,
            {signal},
        );

        if (response.type === 'error') {
            if (signal?.aborted !== true) {
                APIClient.showGenericError(response, 'CM 区間の再判定を開始できませんでした。');
            }
            return null;
        }
        return response.data;
    }


    /**
     * 録画番組のサムネイルを再生成する
     * @param video_id 録画番組の ID
     * @returns サムネイルの再生成に成功した場合は true
     */
    static async regenerateThumbnail(video_id: number): Promise<boolean> {

        // API リクエストを実行
        const response = await APIClient.post(`/videos/${video_id}/thumbnail/regenerate`, undefined, {
            // 数分以上かかるのでタイムアウトを 30 分に設定
            timeout: 30 * 60 * 1000,
        });

        // エラー処理
        if (response.type === 'error') {
            switch (response.data.detail) {
                default:
                    APIClient.showGenericError(response, 'サムネイルの再生成に失敗しました。');
                    break;
            }
            return false;
        }

        return true;
    }

    /**
     * 録画番組を削除する
     * @param video_id 録画番組の ID
     * @returns 削除に成功した場合は true
     */
    static async deleteVideo(video_id: number): Promise<boolean> {

        // API リクエストを実行
        const response = await APIClient.delete(`/videos/${video_id}`);

        // エラー処理
        if (response.type === 'error') {
            switch (response.data.detail) {
                default:
                    APIClient.showGenericError(response, '録画ファイルの削除に失敗しました。');
                    break;
            }
            return false;
        }

        return true;
    }
}

export default Videos;
