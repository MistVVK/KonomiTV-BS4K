
import type { IAnalysisTaskAccepted } from '@/services/AnalysisTasks';
import type {
    IKonomiTVBS4KPlaybackVideoProfile,
    KonomiTVBS4KPlaybackAudioCodec,
    KonomiTVBS4KPlaybackVideoCodec,
} from '@/stores/SettingsStore';

import APIClient from  '@/services/APIClient';
import { IChannel } from '@/services/Channels';
import { CommentUtils, type KonomiTVBS4KPlaybackSelectableQuality, PlayerUtils } from '@/utils';

/** ソート順序を表す型 */
export type SortOrder = 'desc' | 'asc';

/** マイリストのソート順序を表す型 */
export type MylistSortOrder = 'mylist_added_desc' | 'mylist_added_asc' | 'recorded_desc' | 'recorded_asc';

export type IKonomiTVBS4KPlaybackEncoder = 'FFmpeg' | 'QSV' | 'NVENC' | 'AMF';
export type IKonomiTVBS4KPlaybackMode = 'Live' | 'Video';
export type KonomiTVBS4KPlaybackCapabilityReason = (
    'BinaryUnavailable' |
    'BridgeUnavailable' |
    'FeatureDisabled' |
    'DeviceUnavailable' |
    'DeviceInitializationFailed' |
    'FilterUnavailable' |
    'EncodeFailed' |
    'ProbeFailed' |
    'CodecMismatch' |
    'BitDepthMismatch' |
    'ProfileMismatch' |
    'UnsupportedCombination'
);
type KonomiTVBS4KPlaybackOptionUnavailableReason = (
    KonomiTVBS4KPlaybackCapabilityReason |
    'BrowserMSEUnsupported'
);

export interface IKonomiTVBS4KPlaybackVideoCapability {
    encoder: 'FFmpeg' | 'QSV' | 'NVENC' | 'AMF';
    codec: KonomiTVBS4KPlaybackVideoCodec;
    bit_depth: 8 | 10;
    profile: string;
    live_available: boolean;
    recorded_available: boolean;
    live_reason_code: KonomiTVBS4KPlaybackCapabilityReason | null;
    recorded_reason_code: KonomiTVBS4KPlaybackCapabilityReason | null;
}

export interface IKonomiTVBS4KPlaybackAudioCapability {
    codec: KonomiTVBS4KPlaybackAudioCodec;
    live_available: boolean;
    recorded_available: boolean;
    live_reason_code: KonomiTVBS4KPlaybackCapabilityReason | null;
    recorded_reason_code: KonomiTVBS4KPlaybackCapabilityReason | null;
}

export interface IKonomiTVBS4KPlaybackLiveCombinationCapability {
    encoder: IKonomiTVBS4KPlaybackEncoder;
    video_codec: KonomiTVBS4KPlaybackVideoCodec;
    video_bit_depth: 8 | 10;
    audio_codec: KonomiTVBS4KPlaybackAudioCodec;
    available: boolean;
    reason_code: KonomiTVBS4KPlaybackCapabilityReason | null;
}

export interface IKonomiTVBS4KPlaybackCapabilities {
    video: IKonomiTVBS4KPlaybackVideoCapability[];
    audio: IKonomiTVBS4KPlaybackAudioCapability[];
    live_combinations: IKonomiTVBS4KPlaybackLiveCombinationCapability[];
}

export interface IKonomiTVBS4KResolvedPlaybackCombination {
    video: IKonomiTVBS4KPlaybackVideoCapability;
    audio: IKonomiTVBS4KPlaybackAudioCapability;
    live: IKonomiTVBS4KPlaybackLiveCombinationCapability | null;
}

export interface IKonomiTVBS4KPreflightPlaybackProfile {
    video_codec: KonomiTVBS4KPlaybackVideoCodec;
    video_bit_depth: 8 | 10;
    audio_codec: KonomiTVBS4KPlaybackAudioCodec;
    fallback_reason: 'None' | 'CapabilityFallback' | 'CapabilityAPIUnavailable';
}

interface IKonomiTVBS4KTargetedPlaybackCapabilitiesResult {
    capabilities: IKonomiTVBS4KPlaybackCapabilities;
    api_failed: boolean;
}

export interface IKonomiTVBS4KPlaybackVideoCodecOption {
    title: string;
    value: KonomiTVBS4KPlaybackVideoCodec;
    reason: string | null;
    props: {disabled: boolean};
}

export interface IKonomiTVBS4KPlaybackAudioCodecOption {
    title: string;
    value: KonomiTVBS4KPlaybackAudioCodec;
    reason: string | null;
    props: {disabled: boolean};
}

export interface IKonomiTVBS4KPlaybackQualityOption<
    KonomiTVBS4KQuality extends KonomiTVBS4KPlaybackSelectableQuality =
    KonomiTVBS4KPlaybackSelectableQuality,
> {
    title: string;
    value: KonomiTVBS4KQuality;
    reason: string | null;
    props: {disabled: boolean};
}

// 旧録画専用名は既存import互換のため最低1リリース維持する。
export type IRecordedPlaybackCapability = IKonomiTVBS4KPlaybackVideoCapability;
export type IRecordedPlaybackCodecOption = IKonomiTVBS4KPlaybackVideoCodecOption;
export type IRecordedPlaybackAudioCodecOption = IKonomiTVBS4KPlaybackAudioCodecOption;

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
    status: 'Recording' | 'Analyzing' | 'Recorded' | 'AnalysisFailed' | 'Deleting' | 'DeleteFailed';
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

    /** ライブ・録画共通の映像・音声能力行列を取得する。 */
    static async fetchKonomiTVBS4KPlaybackCapabilities(): Promise<IKonomiTVBS4KPlaybackCapabilities> {
        const konomitv_bs4k_response = await APIClient.get<IKonomiTVBS4KPlaybackCapabilities>(
            '/streams/video/konomitv-bs4k-playback-capabilities',
        );
        if (konomitv_bs4k_response.type === 'error') {
            return {video: [], audio: [], live_combinations: []};
        }
        return konomitv_bs4k_response.data;
    }

    /**
     * 現在の再生候補とAVC/AAC互換fallbackだけを取得し、設定画面用の全行列probeを避ける。
     *
     * 能力API自体へ到達できない場合も旧AVC/AAC再生を一度試せるようにするが、
     * 未確認の高度codecを利用可能として合成することはない。
     */
    static async fetchKonomiTVBS4KTargetedPlaybackCapabilities(
        konomitv_bs4k_encoder: IKonomiTVBS4KPlaybackEncoder,
        konomitv_bs4k_playback_mode: IKonomiTVBS4KPlaybackMode,
        konomitv_bs4k_requested_video_codec: KonomiTVBS4KPlaybackVideoCodec,
        konomitv_bs4k_requested_audio_codec: KonomiTVBS4KPlaybackAudioCodec,
        konomitv_bs4k_video_profile: IKonomiTVBS4KPlaybackVideoProfile,
        konomitv_bs4k_has_video: boolean,
        signal?: AbortSignal,
    ): Promise<IKonomiTVBS4KPlaybackCapabilities> {
        return (await this.fetchKonomiTVBS4KTargetedPlaybackCapabilitiesResult(
            konomitv_bs4k_encoder,
            konomitv_bs4k_playback_mode,
            konomitv_bs4k_requested_video_codec,
            konomitv_bs4k_requested_audio_codec,
            konomitv_bs4k_video_profile,
            konomitv_bs4k_has_video,
            undefined,
            signal,
        )).capabilities;
    }

    /** targeted 能力 API の失敗と、正常応答内の非対応を区別して取得する。 */
    private static async fetchKonomiTVBS4KTargetedPlaybackCapabilitiesResult(
        konomitv_bs4k_encoder: IKonomiTVBS4KPlaybackEncoder,
        konomitv_bs4k_playback_mode: IKonomiTVBS4KPlaybackMode,
        konomitv_bs4k_requested_video_codec: KonomiTVBS4KPlaybackVideoCodec,
        konomitv_bs4k_requested_audio_codec: KonomiTVBS4KPlaybackAudioCodec,
        konomitv_bs4k_video_profile: IKonomiTVBS4KPlaybackVideoProfile,
        konomitv_bs4k_has_video: boolean,
        konomitv_bs4k_browser_video_bit_depths?: readonly (8 | 10)[],
        signal?: AbortSignal,
    ): Promise<IKonomiTVBS4KTargetedPlaybackCapabilitiesResult> {
        const konomitv_bs4k_video_bit_depths = this.getKonomiTVBS4KPlaybackBitDepthOrder(
            konomitv_bs4k_requested_video_codec,
            konomitv_bs4k_video_profile,
        ).filter((konomitv_bs4k_video_bit_depth) =>
            konomitv_bs4k_browser_video_bit_depths === undefined ||
            konomitv_bs4k_browser_video_bit_depths.includes(konomitv_bs4k_video_bit_depth)
        );
        const konomitv_bs4k_response = await APIClient.get<IKonomiTVBS4KPlaybackCapabilities>(
            '/streams/video/konomitv-bs4k-playback-capabilities/targeted',
            {
                signal,
                params: {
                    encoder: konomitv_bs4k_encoder,
                    playback_mode: konomitv_bs4k_playback_mode,
                    video_codec: konomitv_bs4k_requested_video_codec,
                    video_bit_depths: konomitv_bs4k_video_bit_depths.join(','),
                    audio_codec: konomitv_bs4k_requested_audio_codec,
                    has_video: konomitv_bs4k_has_video,
                },
            },
        );
        // APIClient がAbortErrorを共通のerror応答へ変換しても、離脱をAPI障害と誤認して
        // AVC/AAC互換tupleを合成しない。
        if (signal?.aborted === true) {
            throw new DOMException('Playback capability preflight was aborted.', 'AbortError');
        }
        if (konomitv_bs4k_response.type === 'error') {
            return {
                capabilities: this.buildKonomiTVBS4KCompatibilityPlaybackCapabilities(
                    konomitv_bs4k_encoder,
                    konomitv_bs4k_has_video,
                ),
                api_failed: true,
            };
        }
        return {capabilities: konomitv_bs4k_response.data, api_failed: false};
    }

    /** 保存された映像 codec から、より互換性の低い側へは戻らない候補順を返す。 */
    static getKonomiTVBS4KLowerVideoCodecOrder(
        konomitv_bs4k_requested_video_codec: KonomiTVBS4KPlaybackVideoCodec,
    ): KonomiTVBS4KPlaybackVideoCodec[] {
        const konomitv_bs4k_ladder: readonly KonomiTVBS4KPlaybackVideoCodec[] =
            ['av1', 'vp9', 'hevc', 'avc'];
        return konomitv_bs4k_ladder.slice(
            konomitv_bs4k_ladder.indexOf(konomitv_bs4k_requested_video_codec),
        );
    }

    /** 保存された音声 codec から、Opus→AAC だけを許す候補順を返す。 */
    static getKonomiTVBS4KLowerAudioCodecOrder(
        konomitv_bs4k_requested_audio_codec: KonomiTVBS4KPlaybackAudioCodec,
    ): KonomiTVBS4KPlaybackAudioCodec[] {
        return konomitv_bs4k_requested_audio_codec === 'opus' ? ['opus', 'aac'] : ['aac'];
    }

    /**
     * 再生開始前だけ、ブラウザ MSE → targeted API → exact tuple の順に能力を検査する。
     * 確定後の再生エラーではこの処理を呼ばず、PlayerStore に pin した同一 tuple を再利用する。
     */
    static async preflightKonomiTVBS4KPlaybackProfile(
        konomitv_bs4k_encoder: IKonomiTVBS4KPlaybackEncoder,
        konomitv_bs4k_requested_video_codec: KonomiTVBS4KPlaybackVideoCodec,
        konomitv_bs4k_requested_audio_codec: KonomiTVBS4KPlaybackAudioCodec,
        konomitv_bs4k_video_profile: IKonomiTVBS4KPlaybackVideoProfile,
        konomitv_bs4k_playback_mode: IKonomiTVBS4KPlaybackMode,
        konomitv_bs4k_has_video: boolean,
        signal?: AbortSignal,
    ): Promise<IKonomiTVBS4KPreflightPlaybackProfile | null> {
        if (signal?.aborted === true) {
            throw new DOMException('Playback capability preflight was aborted.', 'AbortError');
        }
        const konomitv_bs4k_requested_tuple =
            `${konomitv_bs4k_requested_video_codec}/${konomitv_bs4k_requested_audio_codec}`;
        const konomitv_bs4k_audio_codec_order = this.getKonomiTVBS4KLowerAudioCodecOrder(
            konomitv_bs4k_requested_audio_codec,
        );

        // ラジオ・音声のみ録画でも URL / セッション契約上は AVC 8bit を中立な映像値として固定する。
        if (konomitv_bs4k_has_video === false) {
            for (const konomitv_bs4k_audio_codec of konomitv_bs4k_audio_codec_order) {
                if (PlayerUtils.isKonomiTVBS4KPlaybackAudioCodecSupported(konomitv_bs4k_audio_codec) === false) {
                    continue;
                }
                const konomitv_bs4k_result =
                    await this.fetchKonomiTVBS4KTargetedPlaybackCapabilitiesResult(
                        konomitv_bs4k_encoder,
                        konomitv_bs4k_playback_mode,
                        'avc',
                        konomitv_bs4k_audio_codec,
                        konomitv_bs4k_video_profile,
                        false,
                        undefined,
                        signal,
                    );
                const konomitv_bs4k_audio = konomitv_bs4k_result.capabilities.audio.find(
                    (konomitv_bs4k_item) =>
                        konomitv_bs4k_item.codec === konomitv_bs4k_audio_codec &&
                        (konomitv_bs4k_playback_mode === 'Live' ?
                            konomitv_bs4k_item.live_available :
                            konomitv_bs4k_item.recorded_available) === true,
                );
                if (konomitv_bs4k_audio !== undefined) {
                    return {
                        video_codec: 'avc',
                        video_bit_depth: 8,
                        audio_codec: konomitv_bs4k_audio_codec,
                        fallback_reason: konomitv_bs4k_result.api_failed ?
                            'CapabilityAPIUnavailable' :
                            (`avc/${konomitv_bs4k_audio_codec}` === konomitv_bs4k_requested_tuple ?
                                'None' : 'CapabilityFallback'),
                    };
                }
            }
            return null;
        }

        for (
            const konomitv_bs4k_video_codec of
            this.getKonomiTVBS4KLowerVideoCodecOrder(konomitv_bs4k_requested_video_codec)
        ) {
            // browser 判定を先に行い、非対応 codec のためにサーバー実 probe を起動しない。
            const konomitv_bs4k_browser_video_bit_depths =
                this.getKonomiTVBS4KPlaybackBitDepthOrder(
                    konomitv_bs4k_video_codec,
                    konomitv_bs4k_video_profile,
                ).filter((konomitv_bs4k_video_bit_depth) =>
                    PlayerUtils.isKonomiTVBS4KPlaybackVideoCodecSupported(
                        konomitv_bs4k_video_codec,
                        konomitv_bs4k_video_bit_depth,
                        konomitv_bs4k_video_profile,
                    )
                );
            if (konomitv_bs4k_browser_video_bit_depths.length === 0) continue;

            for (const konomitv_bs4k_audio_codec of konomitv_bs4k_audio_codec_order) {
                if (PlayerUtils.isKonomiTVBS4KPlaybackAudioCodecSupported(konomitv_bs4k_audio_codec) === false) {
                    continue;
                }
                const konomitv_bs4k_result =
                    await this.fetchKonomiTVBS4KTargetedPlaybackCapabilitiesResult(
                        konomitv_bs4k_encoder,
                        konomitv_bs4k_playback_mode,
                        konomitv_bs4k_video_codec,
                        konomitv_bs4k_audio_codec,
                        konomitv_bs4k_video_profile,
                        true,
                        konomitv_bs4k_browser_video_bit_depths,
                        signal,
                    );

                // API 障害時だけ既知の AVC 8bit/AAC 互換経路へ直接固定する。
                if (konomitv_bs4k_result.api_failed === true) {
                    const konomitv_bs4k_compatibility =
                        this.resolveKonomiTVBS4KExactPlaybackCombination(
                            konomitv_bs4k_result.capabilities,
                            konomitv_bs4k_encoder,
                            'avc',
                            'aac',
                            konomitv_bs4k_video_profile,
                            konomitv_bs4k_playback_mode,
                        );
                    return konomitv_bs4k_compatibility === null ? null : {
                        video_codec: 'avc',
                        video_bit_depth: 8,
                        audio_codec: 'aac',
                        fallback_reason: 'CapabilityAPIUnavailable',
                    };
                }

                const konomitv_bs4k_combination =
                    this.resolveKonomiTVBS4KExactPlaybackCombination(
                        konomitv_bs4k_result.capabilities,
                        konomitv_bs4k_encoder,
                        konomitv_bs4k_video_codec,
                        konomitv_bs4k_audio_codec,
                        konomitv_bs4k_video_profile,
                        konomitv_bs4k_playback_mode,
                    );
                if (konomitv_bs4k_combination !== null) {
                    return {
                        video_codec: konomitv_bs4k_combination.video.codec,
                        video_bit_depth: konomitv_bs4k_combination.video.bit_depth,
                        audio_codec: konomitv_bs4k_combination.audio.codec,
                        fallback_reason:
                            `${konomitv_bs4k_video_codec}/${konomitv_bs4k_audio_codec}` ===
                            konomitv_bs4k_requested_tuple ? 'None' : 'CapabilityFallback',
                    };
                }
            }
        }
        return null;
    }

    /** 能力API障害時に限り、旧再生経路と同じAVC 8bit/AACだけをfallback候補として表現する。 */
    private static buildKonomiTVBS4KCompatibilityPlaybackCapabilities(
        konomitv_bs4k_encoder: IKonomiTVBS4KPlaybackEncoder,
        konomitv_bs4k_has_video: boolean,
    ): IKonomiTVBS4KPlaybackCapabilities {
        return {
            video: konomitv_bs4k_has_video === false ? [] : [{
                encoder: konomitv_bs4k_encoder,
                codec: 'avc',
                bit_depth: 8,
                profile: 'High',
                live_available: true,
                recorded_available: true,
                live_reason_code: null,
                recorded_reason_code: null,
            }],
            audio: [{
                codec: 'aac',
                live_available: true,
                recorded_available: true,
                live_reason_code: null,
                recorded_reason_code: null,
            }],
            live_combinations: konomitv_bs4k_has_video === false ? [] : [{
                encoder: konomitv_bs4k_encoder,
                video_codec: 'avc',
                video_bit_depth: 8,
                audio_codec: 'aac',
                available: true,
                reason_code: null,
            }],
        };
    }

    /** 旧録画専用呼び出し向けに、共通能力の映像行だけを返す。 */
    static async fetchRecordedPlaybackCapabilities(): Promise<IRecordedPlaybackCapability[]> {
        return (await this.fetchKonomiTVBS4KPlaybackCapabilities()).video;
    }

    /** 能力 API の機械可読 reason code を、利用者が判断できる日本語へ変換する。 */
    static getKonomiTVBS4KPlaybackUnavailableReasonLabel(
        konomitv_bs4k_reason: KonomiTVBS4KPlaybackOptionUnavailableReason,
    ): string {
        const konomitv_bs4k_labels: Record<KonomiTVBS4KPlaybackOptionUnavailableReason, string> = {
            BinaryUnavailable: '固定 FFmpeg が利用できません',
            BridgeUnavailable: 'TS Codec Bridge が利用できません',
            FeatureDisabled: '高度コーデック機能が無効です',
            DeviceUnavailable: '対応するエンコーダーデバイスがありません',
            DeviceInitializationFailed: 'エンコーダーデバイスを初期化できません',
            FilterUnavailable: '必要な映像フィルターが利用できません',
            EncodeFailed: '実エンコードに失敗しました',
            ProbeFailed: '実再生能力の検証に失敗しました',
            CodecMismatch: '出力コーデックを確認できません',
            BitDepthMismatch: '出力ビット深度を確認できません',
            ProfileMismatch: '出力プロファイルを確認できません',
            UnsupportedCombination: 'このコーデック構成は利用できません',
            BrowserMSEUnsupported: 'このブラウザの MSE が対応していません',
        };
        return konomitv_bs4k_labels[konomitv_bs4k_reason];
    }

    /** codecと画質から、実際に優先するbit depth順を返す。 */
    private static getKonomiTVBS4KPlaybackBitDepthOrder(
        konomitv_bs4k_codec: KonomiTVBS4KPlaybackVideoCodec,
        konomitv_bs4k_profile: IKonomiTVBS4KPlaybackVideoProfile,
    ): (8 | 10)[] {
        return konomitv_bs4k_codec === 'avc' ||
            (konomitv_bs4k_codec === 'hevc' && konomitv_bs4k_profile.is_bs4k) ?
            [8] : [10, 8];
    }

    /** backend・映像・深度・音声が一致するライブ組み合わせ能力を返す。 */
    private static findKonomiTVBS4KPlaybackLiveCombination(
        konomitv_bs4k_capabilities: IKonomiTVBS4KPlaybackCapabilities,
        konomitv_bs4k_encoder: IKonomiTVBS4KPlaybackEncoder,
        konomitv_bs4k_video_codec: KonomiTVBS4KPlaybackVideoCodec,
        konomitv_bs4k_video_bit_depth: 8 | 10,
        konomitv_bs4k_audio_codec: KonomiTVBS4KPlaybackAudioCodec,
    ): IKonomiTVBS4KPlaybackLiveCombinationCapability | undefined {
        return konomitv_bs4k_capabilities.live_combinations.find((konomitv_bs4k_combination) =>
            konomitv_bs4k_combination.encoder === konomitv_bs4k_encoder &&
            konomitv_bs4k_combination.video_codec === konomitv_bs4k_video_codec &&
            konomitv_bs4k_combination.video_bit_depth === konomitv_bs4k_video_bit_depth &&
            konomitv_bs4k_combination.audio_codec === konomitv_bs4k_audio_codec
        );
    }

    /** 指定した1組が対象再生モードのサーバー能力とブラウザ能力を満たす場合だけ返す。 */
    private static resolveKonomiTVBS4KPlaybackCombinationCandidate(
        konomitv_bs4k_capabilities: IKonomiTVBS4KPlaybackCapabilities,
        konomitv_bs4k_encoder: IKonomiTVBS4KPlaybackEncoder,
        konomitv_bs4k_video_codec: KonomiTVBS4KPlaybackVideoCodec,
        konomitv_bs4k_video_bit_depth: 8 | 10,
        konomitv_bs4k_audio_codec: KonomiTVBS4KPlaybackAudioCodec,
        konomitv_bs4k_profile: IKonomiTVBS4KPlaybackVideoProfile,
        konomitv_bs4k_playback_mode: IKonomiTVBS4KPlaybackMode | 'LiveAndVideo' = 'LiveAndVideo',
    ): IKonomiTVBS4KResolvedPlaybackCombination | null {
        const konomitv_bs4k_video = konomitv_bs4k_capabilities.video.find((konomitv_bs4k_item) =>
            konomitv_bs4k_item.encoder === konomitv_bs4k_encoder &&
            konomitv_bs4k_item.codec === konomitv_bs4k_video_codec &&
            konomitv_bs4k_item.bit_depth === konomitv_bs4k_video_bit_depth
        );
        const konomitv_bs4k_audio = konomitv_bs4k_capabilities.audio.find((konomitv_bs4k_item) =>
            konomitv_bs4k_item.codec === konomitv_bs4k_audio_codec
        );
        const konomitv_bs4k_live = this.findKonomiTVBS4KPlaybackLiveCombination(
            konomitv_bs4k_capabilities,
            konomitv_bs4k_encoder,
            konomitv_bs4k_video_codec,
            konomitv_bs4k_video_bit_depth,
            konomitv_bs4k_audio_codec,
        );
        const konomitv_bs4k_server_available = konomitv_bs4k_playback_mode === 'Live' ?
            konomitv_bs4k_live?.available === true :
            konomitv_bs4k_playback_mode === 'Video' ?
                konomitv_bs4k_video?.recorded_available === true &&
                konomitv_bs4k_audio?.recorded_available === true :
                konomitv_bs4k_live?.available === true &&
                konomitv_bs4k_video?.recorded_available === true &&
                konomitv_bs4k_audio?.recorded_available === true;
        if (
            konomitv_bs4k_video === undefined ||
            konomitv_bs4k_audio === undefined ||
            konomitv_bs4k_server_available === false ||
            PlayerUtils.isKonomiTVBS4KPlaybackVideoCodecSupported(
                konomitv_bs4k_video_codec,
                konomitv_bs4k_video_bit_depth,
                konomitv_bs4k_profile,
            ) === false ||
            PlayerUtils.isKonomiTVBS4KPlaybackAudioCodecSupported(konomitv_bs4k_audio_codec) === false
        ) {
            return null;
        }
        return {
            video: konomitv_bs4k_video,
            audio: konomitv_bs4k_audio,
            live: konomitv_bs4k_live ?? null,
        };
    }

    /**
     * 指定した映像・音声 codec の exact combination だけを bit depth 順に試す。
     * 汎用 resolver のように別映像 codec へ fallback しない。
     */
    static resolveKonomiTVBS4KExactPlaybackCombination(
        konomitv_bs4k_capabilities: IKonomiTVBS4KPlaybackCapabilities,
        konomitv_bs4k_encoder: IKonomiTVBS4KPlaybackEncoder,
        konomitv_bs4k_video_codec: KonomiTVBS4KPlaybackVideoCodec,
        konomitv_bs4k_audio_codec: KonomiTVBS4KPlaybackAudioCodec,
        konomitv_bs4k_profile: IKonomiTVBS4KPlaybackVideoProfile,
        konomitv_bs4k_playback_mode: IKonomiTVBS4KPlaybackMode | 'LiveAndVideo' = 'LiveAndVideo',
    ): IKonomiTVBS4KResolvedPlaybackCombination | null {
        for (
            const konomitv_bs4k_video_bit_depth of
            this.getKonomiTVBS4KPlaybackBitDepthOrder(konomitv_bs4k_video_codec, konomitv_bs4k_profile)
        ) {
            const konomitv_bs4k_combination = this.resolveKonomiTVBS4KPlaybackCombinationCandidate(
                konomitv_bs4k_capabilities,
                konomitv_bs4k_encoder,
                konomitv_bs4k_video_codec,
                konomitv_bs4k_video_bit_depth,
                konomitv_bs4k_audio_codec,
                konomitv_bs4k_profile,
                konomitv_bs4k_playback_mode,
            );
            if (konomitv_bs4k_combination !== null) {
                return konomitv_bs4k_combination;
            }
        }
        return null;
    }

    /**
     * 保存した映像・音声を対象再生モードの同一 combination として解決する。
     *
     * 映像を優先してから音声を互換順に試し、互いに別の組み合わせへfallbackする状態を作らない。
     */
    static resolveKonomiTVBS4KPlaybackCombination(
        konomitv_bs4k_capabilities: IKonomiTVBS4KPlaybackCapabilities,
        konomitv_bs4k_encoder: IKonomiTVBS4KPlaybackEncoder,
        konomitv_bs4k_requested_video_codec: KonomiTVBS4KPlaybackVideoCodec,
        konomitv_bs4k_requested_audio_codec: KonomiTVBS4KPlaybackAudioCodec,
        konomitv_bs4k_profile: IKonomiTVBS4KPlaybackVideoProfile,
        konomitv_bs4k_playback_mode: IKonomiTVBS4KPlaybackMode | 'LiveAndVideo' = 'LiveAndVideo',
    ): IKonomiTVBS4KResolvedPlaybackCombination | null {
        const konomitv_bs4k_video_codec_order =
            this.getKonomiTVBS4KLowerVideoCodecOrder(konomitv_bs4k_requested_video_codec);
        const konomitv_bs4k_audio_codec_order =
            this.getKonomiTVBS4KLowerAudioCodecOrder(konomitv_bs4k_requested_audio_codec);
        for (const konomitv_bs4k_video_codec of konomitv_bs4k_video_codec_order) {
            for (const konomitv_bs4k_audio_codec of konomitv_bs4k_audio_codec_order) {
                const konomitv_bs4k_combination = this.resolveKonomiTVBS4KExactPlaybackCombination(
                    konomitv_bs4k_capabilities,
                    konomitv_bs4k_encoder,
                    konomitv_bs4k_video_codec,
                    konomitv_bs4k_audio_codec,
                    konomitv_bs4k_profile,
                    konomitv_bs4k_playback_mode,
                );
                if (konomitv_bs4k_combination !== null) return konomitv_bs4k_combination;
            }
        }
        return null;
    }

    /**
     * ユーザーが映像codecを変更したとき、選択した映像を固定したまま音声だけを互換順で解決する。
     *
     * 汎用resolverの映像fallbackを使うと、選択した映像が利用できない場合に別の映像を返してしまうため、
     * 設定画面の片軸変更ではこのresolverを使って映像・音声の2値を同時に確定させる。
     */
    static resolveKonomiTVBS4KPlaybackCombinationForVideoCodecChange(
        konomitv_bs4k_capabilities: IKonomiTVBS4KPlaybackCapabilities,
        konomitv_bs4k_encoder: IKonomiTVBS4KPlaybackEncoder,
        konomitv_bs4k_selected_video_codec: KonomiTVBS4KPlaybackVideoCodec,
        konomitv_bs4k_current_audio_codec: KonomiTVBS4KPlaybackAudioCodec,
        konomitv_bs4k_profile: IKonomiTVBS4KPlaybackVideoProfile,
        konomitv_bs4k_playback_mode: IKonomiTVBS4KPlaybackMode | 'LiveAndVideo' = 'LiveAndVideo',
    ): IKonomiTVBS4KResolvedPlaybackCombination | null {
        const konomitv_bs4k_audio_codec_order =
            this.getKonomiTVBS4KLowerAudioCodecOrder(konomitv_bs4k_current_audio_codec);
        for (
            const konomitv_bs4k_video_bit_depth of
            this.getKonomiTVBS4KPlaybackBitDepthOrder(
                konomitv_bs4k_selected_video_codec,
                konomitv_bs4k_profile,
            )
        ) {
            for (const konomitv_bs4k_audio_codec of konomitv_bs4k_audio_codec_order) {
                const konomitv_bs4k_combination =
                    this.resolveKonomiTVBS4KPlaybackCombinationCandidate(
                        konomitv_bs4k_capabilities,
                        konomitv_bs4k_encoder,
                        konomitv_bs4k_selected_video_codec,
                        konomitv_bs4k_video_bit_depth,
                        konomitv_bs4k_audio_codec,
                        konomitv_bs4k_profile,
                        konomitv_bs4k_playback_mode,
                    );
                if (konomitv_bs4k_combination !== null) return konomitv_bs4k_combination;
            }
        }
        return null;
    }

    /**
     * ユーザーが音声codecを変更したとき、選択した音声を固定したまま映像だけを互換順で解決する。
     *
     * 現在の映像との組み合わせを最優先し、成立しない場合だけ互換映像へ切り替えることで、
     * 選択した音声と不整合な映像値が LocalStorage に残ることを防ぐ。
     */
    static resolveKonomiTVBS4KPlaybackCombinationForAudioCodecChange(
        konomitv_bs4k_capabilities: IKonomiTVBS4KPlaybackCapabilities,
        konomitv_bs4k_encoder: IKonomiTVBS4KPlaybackEncoder,
        konomitv_bs4k_current_video_codec: KonomiTVBS4KPlaybackVideoCodec,
        konomitv_bs4k_selected_audio_codec: KonomiTVBS4KPlaybackAudioCodec,
        konomitv_bs4k_profile: IKonomiTVBS4KPlaybackVideoProfile,
        konomitv_bs4k_playback_mode: IKonomiTVBS4KPlaybackMode | 'LiveAndVideo' = 'LiveAndVideo',
    ): IKonomiTVBS4KResolvedPlaybackCombination | null {
        const konomitv_bs4k_video_codec_order =
            this.getKonomiTVBS4KLowerVideoCodecOrder(konomitv_bs4k_current_video_codec);
        for (const konomitv_bs4k_video_codec of konomitv_bs4k_video_codec_order) {
            for (
                const konomitv_bs4k_video_bit_depth of
                this.getKonomiTVBS4KPlaybackBitDepthOrder(
                    konomitv_bs4k_video_codec,
                    konomitv_bs4k_profile,
                )
            ) {
                const konomitv_bs4k_combination =
                    this.resolveKonomiTVBS4KPlaybackCombinationCandidate(
                        konomitv_bs4k_capabilities,
                        konomitv_bs4k_encoder,
                        konomitv_bs4k_video_codec,
                        konomitv_bs4k_video_bit_depth,
                        konomitv_bs4k_selected_audio_codec,
                        konomitv_bs4k_profile,
                        konomitv_bs4k_playback_mode,
                    );
                if (konomitv_bs4k_combination !== null) return konomitv_bs4k_combination;
            }
        }
        return null;
    }

    /** 共通profileに利用できる映像能力を、指定音声との厳密な組み合わせで解決する。 */
    static resolveKonomiTVBS4KPlaybackVideoCapability(
        konomitv_bs4k_capabilities: IKonomiTVBS4KPlaybackCapabilities,
        konomitv_bs4k_encoder: IKonomiTVBS4KPlaybackEncoder,
        konomitv_bs4k_requested_codec: KonomiTVBS4KPlaybackVideoCodec,
        konomitv_bs4k_profile: IKonomiTVBS4KPlaybackVideoProfile,
        konomitv_bs4k_audio_codec: KonomiTVBS4KPlaybackAudioCodec = 'aac',
    ): IKonomiTVBS4KPlaybackVideoCapability | null {
        return this.resolveKonomiTVBS4KPlaybackCombination(
            konomitv_bs4k_capabilities,
            konomitv_bs4k_encoder,
            konomitv_bs4k_requested_codec,
            konomitv_bs4k_audio_codec,
            konomitv_bs4k_profile,
        )?.video ?? null;
    }

    /** 共通profileに利用できる音声能力を、指定映像との厳密な組み合わせで解決する。 */
    static resolveKonomiTVBS4KPlaybackAudioCapability(
        konomitv_bs4k_capabilities: IKonomiTVBS4KPlaybackCapabilities,
        konomitv_bs4k_requested_codec: KonomiTVBS4KPlaybackAudioCodec,
        konomitv_bs4k_encoder: IKonomiTVBS4KPlaybackEncoder = 'FFmpeg',
        konomitv_bs4k_video_codec: KonomiTVBS4KPlaybackVideoCodec = 'avc',
        konomitv_bs4k_profile: IKonomiTVBS4KPlaybackVideoProfile = {
            is_bs4k: false,
            streaming_quality: '1080p',
        },
    ): IKonomiTVBS4KPlaybackAudioCapability | null {
        return this.resolveKonomiTVBS4KPlaybackCombination(
            konomitv_bs4k_capabilities,
            konomitv_bs4k_encoder,
            konomitv_bs4k_video_codec,
            konomitv_bs4k_requested_codec,
            konomitv_bs4k_profile,
        )?.audio ?? null;
    }

    /** 映像 SourceBuffer を持たないラジオ・音声のみ録画の音声能力を解決する。 */
    static resolveKonomiTVBS4KPlaybackAudioOnlyCapability(
        konomitv_bs4k_capabilities: IKonomiTVBS4KPlaybackCapabilities,
        konomitv_bs4k_requested_codec: KonomiTVBS4KPlaybackAudioCodec,
    ): IKonomiTVBS4KPlaybackAudioCapability | null {
        const konomitv_bs4k_codec_order =
            this.getKonomiTVBS4KLowerAudioCodecOrder(konomitv_bs4k_requested_codec);
        for (const konomitv_bs4k_codec of konomitv_bs4k_codec_order) {
            const konomitv_bs4k_capability =
                konomitv_bs4k_capabilities.audio.find((konomitv_bs4k_item) =>
                    konomitv_bs4k_item.codec === konomitv_bs4k_codec &&
                    konomitv_bs4k_item.live_available === true &&
                    konomitv_bs4k_item.recorded_available === true
                );
            if (
                konomitv_bs4k_capability !== undefined &&
                PlayerUtils.isKonomiTVBS4KPlaybackAudioCodecSupported(konomitv_bs4k_codec)
            ) {
                return konomitv_bs4k_capability;
            }
        }
        return null;
    }

    /** 1組の設定項目を無効化する理由を、exact combinationを正本として返す。 */
    private static getKonomiTVBS4KPlaybackCombinationUnavailableReason(
        konomitv_bs4k_encoder: IKonomiTVBS4KPlaybackEncoder,
        konomitv_bs4k_capabilities: IKonomiTVBS4KPlaybackCapabilities,
        konomitv_bs4k_video_codec: KonomiTVBS4KPlaybackVideoCodec,
        konomitv_bs4k_audio_codec: KonomiTVBS4KPlaybackAudioCodec,
        konomitv_bs4k_profile: IKonomiTVBS4KPlaybackVideoProfile,
    ): KonomiTVBS4KPlaybackOptionUnavailableReason | null {
        const konomitv_bs4k_video_capabilities =
            this.getKonomiTVBS4KPlaybackBitDepthOrder(
                konomitv_bs4k_video_codec,
                konomitv_bs4k_profile,
            ).map((konomitv_bs4k_video_bit_depth) =>
                konomitv_bs4k_capabilities.video.find((konomitv_bs4k_item) =>
                    konomitv_bs4k_item.encoder === konomitv_bs4k_encoder &&
                    konomitv_bs4k_item.codec === konomitv_bs4k_video_codec &&
                    konomitv_bs4k_item.bit_depth === konomitv_bs4k_video_bit_depth
                )
            ).filter((konomitv_bs4k_item) =>
                konomitv_bs4k_item !== undefined
            );
        const konomitv_bs4k_audio_capability =
            konomitv_bs4k_capabilities.audio.find((konomitv_bs4k_item) =>
                konomitv_bs4k_item.codec === konomitv_bs4k_audio_codec
            );
        // 能力行そのものが無い（targeted 応答の切り詰めなど）ときは ProbeFailed と誤表示しない。
        if (konomitv_bs4k_audio_capability === undefined) {
            return 'UnsupportedCombination';
        }
        if (konomitv_bs4k_audio_capability.recorded_available !== true) {
            return konomitv_bs4k_audio_capability.recorded_reason_code ?? 'ProbeFailed';
        }
        if (konomitv_bs4k_video_capabilities.length === 0) {
            return 'UnsupportedCombination';
        }
        const konomitv_bs4k_recorded_video_capabilities =
            konomitv_bs4k_video_capabilities.filter((konomitv_bs4k_item) =>
                konomitv_bs4k_item.recorded_available === true
            );
        if (konomitv_bs4k_recorded_video_capabilities.length === 0) {
            return konomitv_bs4k_video_capabilities.find((konomitv_bs4k_item) =>
                konomitv_bs4k_item.recorded_reason_code !== null
            )?.recorded_reason_code ?? 'ProbeFailed';
        }
        const konomitv_bs4k_server_combinations =
            konomitv_bs4k_recorded_video_capabilities.map((konomitv_bs4k_video_capability) => ({
                video: konomitv_bs4k_video_capability,
                live: this.findKonomiTVBS4KPlaybackLiveCombination(
                    konomitv_bs4k_capabilities,
                    konomitv_bs4k_encoder,
                    konomitv_bs4k_video_codec,
                    konomitv_bs4k_video_capability.bit_depth,
                    konomitv_bs4k_audio_codec,
                ),
            }));
        const konomitv_bs4k_available_server_combinations =
            konomitv_bs4k_server_combinations.filter((konomitv_bs4k_combination) =>
                konomitv_bs4k_combination.live?.available === true
            );
        if (konomitv_bs4k_available_server_combinations.length === 0) {
            return konomitv_bs4k_server_combinations.find((konomitv_bs4k_combination) =>
                konomitv_bs4k_combination.live?.reason_code !== null &&
                konomitv_bs4k_combination.live?.reason_code !== undefined
            )?.live?.reason_code ?? 'UnsupportedCombination';
        }
        const konomitv_bs4k_browser_available =
            PlayerUtils.isKonomiTVBS4KPlaybackAudioCodecSupported(konomitv_bs4k_audio_codec) &&
            konomitv_bs4k_available_server_combinations.some((konomitv_bs4k_combination) =>
                PlayerUtils.isKonomiTVBS4KPlaybackVideoCodecSupported(
                    konomitv_bs4k_video_codec,
                    konomitv_bs4k_combination.video.bit_depth,
                    konomitv_bs4k_profile,
                )
            );
        return konomitv_bs4k_browser_available ? null : 'BrowserMSEUnsupported';
    }

    /** ライブまたは録画のどちらかで成立する映像候補を、ブラウザ MSE 能力と合わせて表示する。 */
    static buildKonomiTVBS4KPlaybackVideoCodecOptions(
        konomitv_bs4k_encoder: IKonomiTVBS4KPlaybackEncoder,
        konomitv_bs4k_capabilities: IKonomiTVBS4KPlaybackCapabilities,
        konomitv_bs4k_profile: IKonomiTVBS4KPlaybackVideoProfile,
        konomitv_bs4k_audio_codec: KonomiTVBS4KPlaybackAudioCodec = 'aac',
    ): IKonomiTVBS4KPlaybackVideoCodecOption[] {
        const konomitv_bs4k_definitions: Record<KonomiTVBS4KPlaybackVideoCodec, string> = {
            avc: 'H.264 / AVC（互換性優先）',
            hevc: 'H.265 / HEVC',
            vp9: 'Google VP9（WebUI 専用 TS・実験的）',
            av1: `Alliance for Open Media AV1${konomitv_bs4k_encoder === 'FFmpeg' ? '（実験的）' : ''}`,
        };
        return (Object.keys(konomitv_bs4k_definitions) as KonomiTVBS4KPlaybackVideoCodec[]).map(
            (konomitv_bs4k_codec) => {
                const konomitv_bs4k_resolved_combination =
                    this.resolveKonomiTVBS4KPlaybackCombinationForVideoCodecChange(
                        konomitv_bs4k_capabilities,
                        konomitv_bs4k_encoder,
                        konomitv_bs4k_codec,
                        konomitv_bs4k_audio_codec,
                        konomitv_bs4k_profile,
                        'Live',
                    ) ?? this.resolveKonomiTVBS4KPlaybackCombinationForVideoCodecChange(
                        konomitv_bs4k_capabilities,
                        konomitv_bs4k_encoder,
                        konomitv_bs4k_codec,
                        konomitv_bs4k_audio_codec,
                        konomitv_bs4k_profile,
                        'Video',
                    );
                const konomitv_bs4k_reason_code = konomitv_bs4k_resolved_combination !== null ?
                    null :
                    this.getKonomiTVBS4KPlaybackCombinationUnavailableReason(
                        konomitv_bs4k_encoder,
                        konomitv_bs4k_capabilities,
                        konomitv_bs4k_codec,
                        konomitv_bs4k_audio_codec,
                        konomitv_bs4k_profile,
                    );
                const konomitv_bs4k_reason = konomitv_bs4k_reason_code === null ?
                    null : this.getKonomiTVBS4KPlaybackUnavailableReasonLabel(konomitv_bs4k_reason_code);
                return {
                    title: `${konomitv_bs4k_definitions[konomitv_bs4k_codec]}` +
                    `${konomitv_bs4k_reason !== null ? `（${konomitv_bs4k_reason}）` : ''}`,
                    value: konomitv_bs4k_codec,
                    reason: konomitv_bs4k_reason,
                    // 非対応でも「希望値」として保存でき、再生開始時の lower-only fallback を確認できる。
                    props: {disabled: false},
                };
            },
        );
    }

    /**
     * 画質ごとにライブまたは録画で成立するか評価し、どちらでも成立しない場合の理由を表示する。
     * 非対応でも将来の能力追加に備えた希望値として保存できるよう、選択自体は禁止しない。
     */
    static buildKonomiTVBS4KPlaybackQualityOptions<
        KonomiTVBS4KQuality extends KonomiTVBS4KPlaybackSelectableQuality,
    >(
        konomitv_bs4k_encoder: IKonomiTVBS4KPlaybackEncoder,
        konomitv_bs4k_capabilities: IKonomiTVBS4KPlaybackCapabilities,
        konomitv_bs4k_video_codec: KonomiTVBS4KPlaybackVideoCodec,
        is_konomitv_bs4k: boolean,
        konomitv_bs4k_quality_options: readonly {
            title: string;
            value: KonomiTVBS4KQuality;
        }[],
        konomitv_bs4k_audio_codec: KonomiTVBS4KPlaybackAudioCodec = 'aac',
    ): IKonomiTVBS4KPlaybackQualityOption<KonomiTVBS4KQuality>[] {
        return konomitv_bs4k_quality_options.map((konomitv_bs4k_quality_option) => {
            const konomitv_bs4k_video_codec_option =
                this.buildKonomiTVBS4KPlaybackVideoCodecOptions(
                    konomitv_bs4k_encoder,
                    konomitv_bs4k_capabilities,
                    {
                        is_bs4k: is_konomitv_bs4k,
                        streaming_quality: konomitv_bs4k_quality_option.value,
                    },
                    konomitv_bs4k_audio_codec,
                ).find((konomitv_bs4k_option) =>
                    konomitv_bs4k_option.value === konomitv_bs4k_video_codec
                );
            const konomitv_bs4k_reason = konomitv_bs4k_video_codec_option === undefined ?
                this.getKonomiTVBS4KPlaybackUnavailableReasonLabel('ProbeFailed') :
                konomitv_bs4k_video_codec_option.reason;
            return {
                title: konomitv_bs4k_reason === null ?
                    konomitv_bs4k_quality_option.title :
                    `${konomitv_bs4k_quality_option.title}（${konomitv_bs4k_reason}）`,
                value: konomitv_bs4k_quality_option.value,
                reason: konomitv_bs4k_reason,
                props: {disabled: false},
            };
        });
    }

    /** 旧録画専用名からも共通profile用の選択肢を返す。 */
    static async buildRecordedPlaybackCodecOptions(
        encoder: IKonomiTVBS4KPlaybackEncoder,
    ): Promise<IRecordedPlaybackCodecOption[]> {
        return this.buildKonomiTVBS4KPlaybackVideoCodecOptions(
            encoder,
            await this.fetchKonomiTVBS4KPlaybackCapabilities(),
            {is_bs4k: false, streaming_quality: '1080p'},
        );
    }

    /** このブラウザが fMP4 コンテナ内の Opus 音声を再生できるかどうかを返す。 */
    static isOpusAudioSupported(): boolean {
        return PlayerUtils.isKonomiTVBS4KPlaybackAudioCodecSupported('opus');
    }

    /** 現在の映像とライブまたは録画で成立する音声候補を、ブラウザ MSE 能力と合わせて表示する。 */
    static buildKonomiTVBS4KPlaybackAudioCodecOptions(
        konomitv_bs4k_capabilities: IKonomiTVBS4KPlaybackCapabilities,
        konomitv_bs4k_encoder: IKonomiTVBS4KPlaybackEncoder = 'FFmpeg',
        konomitv_bs4k_video_codec: KonomiTVBS4KPlaybackVideoCodec = 'avc',
        konomitv_bs4k_profile: IKonomiTVBS4KPlaybackVideoProfile = {
            is_bs4k: false,
            streaming_quality: '1080p',
        },
    ): IKonomiTVBS4KPlaybackAudioCodecOption[] {
        const konomitv_bs4k_definitions: Record<KonomiTVBS4KPlaybackAudioCodec, string> = {
            aac: 'AAC（互換性優先）',
            opus: 'Opus（音質・圧縮効率優先）',
        };
        return (Object.keys(konomitv_bs4k_definitions) as KonomiTVBS4KPlaybackAudioCodec[]).map((konomitv_bs4k_codec) => {
            const konomitv_bs4k_resolved_combination =
                this.resolveKonomiTVBS4KPlaybackCombinationForAudioCodecChange(
                    konomitv_bs4k_capabilities,
                    konomitv_bs4k_encoder,
                    konomitv_bs4k_video_codec,
                    konomitv_bs4k_codec,
                    konomitv_bs4k_profile,
                    'Live',
                ) ?? this.resolveKonomiTVBS4KPlaybackCombinationForAudioCodecChange(
                    konomitv_bs4k_capabilities,
                    konomitv_bs4k_encoder,
                    konomitv_bs4k_video_codec,
                    konomitv_bs4k_codec,
                    konomitv_bs4k_profile,
                    'Video',
                );
            const konomitv_bs4k_reason_code = konomitv_bs4k_resolved_combination !== null ?
                null :
                this.getKonomiTVBS4KPlaybackCombinationUnavailableReason(
                    konomitv_bs4k_encoder,
                    konomitv_bs4k_capabilities,
                    konomitv_bs4k_video_codec,
                    konomitv_bs4k_codec,
                    konomitv_bs4k_profile,
                );
            const konomitv_bs4k_reason = konomitv_bs4k_reason_code === null ?
                null : this.getKonomiTVBS4KPlaybackUnavailableReasonLabel(konomitv_bs4k_reason_code);
            return {
                title: `${konomitv_bs4k_definitions[konomitv_bs4k_codec]}` +
                    `${konomitv_bs4k_reason !== null ? `（${konomitv_bs4k_reason}）` : ''}`,
                value: konomitv_bs4k_codec,
                reason: konomitv_bs4k_reason,
                props: {disabled: false},
            };
        });
    }

    /** 旧録画専用UI向けの同期API。サーバー能力は再生開始時に改めて検証する。 */
    static buildRecordedPlaybackAudioCodecOptions(): IRecordedPlaybackAudioCodecOption[] {
        const is_opus_supported = this.isOpusAudioSupported();
        return [
            {title: 'AAC（互換性優先）', value: 'aac', reason: null, props: {disabled: false}},
            {
                title: `Opus（音質・圧縮効率優先）${is_opus_supported ? '' : '（ブラウザ非対応）'}`,
                value: 'opus',
                reason: is_opus_supported ? null : 'このブラウザの MSE が対応していません',
                props: {disabled: false},
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
