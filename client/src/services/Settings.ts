
import type { VideoSeriesSortDirection, VideoSeriesSortKey } from '@/stores/SettingsStore';
import type { KonomiTVBS4KTheme } from '@/themes';

import APIClient from '@/services/APIClient';
import { getSyncableClientSettings, ITimeTableGenreColors, TimeTableSizeOption } from '@/stores/SettingsStore';


/**
 * ミュート対象のコメントのキーワードのインターフェイス
 */
export interface IMutedCommentKeywords {
    match: 'partial' | 'forward' | 'backward' | 'exact' | 'regex';
    pattern: string;
}

/**
 * サーバーに保存されるクライアント設定を表すインターフェース
 * サーバー側の app.config.ClientSettings で定義されているものと同じ
 */
export interface IClientSettings {
    last_synced_at: number;
    // showed_panel_last_time: 同期無効
    // selected_twitter_panel_account: 同期無効
    // twitter_panel_post_targets: 同期無効
    saved_twitter_hashtags: string[];
    mylist: {
        type: 'Series' | 'RecordedProgram';
        id: number;
        created_at: number;
    }[];
    watched_history: {
        video_id: number;
        last_playback_position: number;
        created_at: number;
        updated_at: number;
    }[];
    // lshaped_screen_crop_enabled: 同期無効
    // lshaped_screen_crop_zoom_level: 同期無効
    // lshaped_screen_crop_x_position: 同期無効
    // lshaped_screen_crop_y_position: 同期無効
    // lshaped_screen_crop_zoom_origin: 同期無効
    pinned_channel_ids: string[];
    timetable_channel_width: TimeTableSizeOption;
    timetable_hour_height: TimeTableSizeOption;
    timetable_hover_expand: boolean;
    timetable_dim_shopping_programs: boolean;
    timetable_genre_colors: ITimeTableGenreColors;
    show_gr_channels: boolean;
    show_oneseg_channels: boolean;
    show_bs_channels: boolean;
    show_cs_channels: boolean;
    show_catv_channels: boolean;
    show_sky_channels: boolean;
    show_bs4k_channels: boolean;
    ui_theme: KonomiTVBS4KTheme;
    show_player_background_image: boolean;
    use_pure_black_player_background: boolean;
    tv_channel_sort_by_jikkyo_force: boolean;
    tv_channel_up_down_buttons_reverse: boolean;
    tv_channel_selection_requires_alt_key: boolean;
    use_28hour_clock: boolean;
    show_original_broadcast_time_during_playback: boolean;
    panel_display_state: 'RestorePreviousState' | 'AlwaysDisplay' | 'AlwaysFold';
    tv_panel_active_tab: 'Program' | 'Channel' | 'Comment' | 'Twitter';
    video_panel_active_tab: 'RecordedProgram' | 'Series' | 'Comment' | 'Twitter';
    video_series_sort_key: VideoSeriesSortKey;
    video_series_sort_direction: VideoSeriesSortDirection;
    video_watched_history_max_count: number;
    // konomitv_bs4k_offline_video_streaming_quality: 同期無効
    // konomitv_bs4k_offline_video_streaming_quality_for_bs4k: 同期無効
    // konomitv_bs4k_offline_video_codec: 同期無効
    // konomitv_bs4k_offline_video_codec_for_bs4k: 同期無効
    // konomitv_bs4k_offline_audio_codec: 同期無効
    // konomitv_bs4k_offline_audio_codec_for_bs4k: 同期無効
    // konomitv_bs4k_offline_video_24fps_mode: 同期無効
    // tv_streaming_quality: 同期無効
    // tv_streaming_quality_cellular: 同期無効
    // bs4k_streaming_quality: 同期無効
    // bs4k_streaming_quality_cellular: 同期無効
    // bs4k_video_streaming_quality: 同期無効
    // bs4k_video_streaming_quality_cellular: 同期無効
    // tv_encoding_codec: 同期無効
    // tv_encoding_codec_cellular: 同期無効
    // bs4k_tv_encoding_codec: 同期無効
    // bs4k_tv_encoding_codec_cellular: 同期無効
    // tv_low_latency_mode: 同期無効
    // tv_low_latency_mode_cellular: 同期無効
    // tv_low_latency_mode_for_bs4k: 同期無効
    // tv_low_latency_mode_for_bs4k_cellular: 同期無効
    // tv_24fps_mode: 同期無効
    // tv_24fps_mode_cellular: 同期無効
    // video_streaming_quality: 同期無効
    // video_streaming_quality_cellular: 同期無効
    // video_encoding_codec: 同期無効
    // video_encoding_codec_cellular: 同期無効
    // bs4k_video_encoding_codec: 同期無効
    // bs4k_video_encoding_codec_cellular: 同期無効
    // video_24fps_mode: 同期無効
    // video_24fps_mode_cellular: 同期無効
    caption_font: string;
    always_border_caption_text: boolean;
    specify_caption_opacity: boolean;
    caption_opacity: number;
    tv_show_superimpose: boolean;
    video_show_superimpose: boolean;
    // tv_show_data_broadcasting: 同期無効
    // enable_internet_access_from_data_broadcasting: 同期無効
    capture_save_mode: 'Browser' | 'UploadServer' | 'Both';
    capture_caption_mode: 'VideoOnly' | 'CompositingCaption' | 'Both';
    capture_filename_pattern: string;
    // capture_copy_to_clipboard: 同期無効
    // sync_settings: 同期無効
    jikkyo_enabled: boolean;
    prefer_posting_to_nicolive: boolean;
    comment_speed_rate: number;
    comment_font_size: number;
    close_comment_form_after_sending: boolean;
    mute_vulgar_comments: boolean;
    mute_abusive_discriminatory_prejudiced_comments: boolean;
    mute_big_size_comments: boolean;
    mute_fixed_comments: boolean;
    mute_colored_comments: boolean;
    mute_consecutive_same_characters_comments: boolean;
    mute_comment_keywords_normalize_alphanumeric_width_case: boolean;
    muted_comment_keywords: IMutedCommentKeywords[];
    muted_niconico_user_ids: string[];
    fold_panel_after_sending_tweet: boolean;
    reset_hashtag_when_program_switches: boolean;
    auto_add_watching_channel_hashtag: boolean;
    twitter_reply_thread_mode: 'PerHashtag' | 'PerDay' | 'Disabled';
    bluesky_reply_thread_mode: 'PerHashtag' | 'PerDay' | 'Disabled';
    twitter_active_tab: 'Search' | 'Timeline' | 'Capture';
    tweet_hashtag_position: 'Prepend' | 'Append' | 'PrependWithLineBreak' | 'AppendWithLineBreak';
    tweet_capture_watermark_position: 'None' | 'TopLeft' | 'TopRight' | 'BottomLeft' | 'BottomRight';
}

/**
 * WebUI・設定 API・config.yaml で保持するホスト側の絶対パス
 */
export type HostAbsolutePath = string;

export type ServerEncoder = 'FFmpeg' | 'QSV' | 'NVENC' | 'AMF';

/**
 * サーバー設定を表すインターフェース
 * サーバー側の app.config.HostServerSettings で定義されているものと同じ
 */
export interface IServerSettings {
    general: {
        backend: 'EDCB' | 'Mirakurun';
        jikkyo_enabled: boolean;
        always_receive_tv_from_mirakurun: boolean;
        edcb_url: string;
        mirakurun_url: string;
        konomitv_bs4k_live_transport: 'MpegTs' | 'Tlv';
        konomitv_bs4k_tlv_mirakurun_url: string | null;
        encoder: ServerEncoder;
        konomitv_bs4k_live_sar_mode: 'CPU' | 'GPU';
        encoder_bs4k: ServerEncoder;
        encoder_bs4k_input_probesize: number;
        encoder_bs4k_input_analyze: number;
        encoder_bs4k_input_analysis_enabled: boolean;
        encoder_bs4k_max_interleave_delta: number;
        encoder_bs4k_low_latency: boolean;
        bs4k_ignore_viewer_low_latency: boolean;
        bs4k_live_startup_discard_enabled: boolean;
        bs4k_live_startup_discard_seconds: number;
        program_update_interval: number;
        debug: boolean;
        debug_encoder: boolean;
    };
    server: {
        https_mode: 'akebi' | 'certificate' | 'reverse_proxy';
        port: number;
        opencode_serve_port: number;
        custom_https_certificate: HostAbsolutePath | null;
        custom_https_private_key: HostAbsolutePath | null;
        reverse_proxy_listen_address: string;
        trusted_proxy_cidrs: string[];
    };
    compatibility_api: {
        enabled: boolean;
        port: number;
        profile: 'KomorebiV1';
        https_mode: 'inherit' | 'akebi' | 'certificate' | 'reverse_proxy';
        custom_https_certificate: HostAbsolutePath | null;
        custom_https_private_key: HostAbsolutePath | null;
        reverse_proxy_listen_address: string;
        trusted_proxy_cidrs: string[];
    };
    tv: {
        preferred_terrestrial_region: string | null;
        max_alive_time: number;
        debug_mode_ts_path: HostAbsolutePath | null;
    };
    video: {
        recorded_folders: HostAbsolutePath[];
        exclude_scan_paths: HostAbsolutePath[];
        recorded_fmp4_cache_folder: HostAbsolutePath | null;
        recorded_playback_index_backfill_enabled: boolean;
    };
    capture: {
        upload_folders: HostAbsolutePath[];
    };
    /** CM 解析のサーバー全体設定（config.yaml の cm_analysis）。専用 API でも更新する。 */
    cm_analysis: {
        enabled: boolean;
        logo_directory: HostAbsolutePath | null;
        excluded_directories: HostAbsolutePath[];
    };
}

type LegacyServerEncoder = 'QSVEncC' | 'NVEncC' | 'VCEEncC';
type ServerSettingsResponse = Omit<IServerSettings, 'general'> & {
    general: Omit<IServerSettings['general'], 'encoder' | 'encoder_bs4k'> & {
        encoder: ServerEncoder | LegacyServerEncoder;
        encoder_bs4k: ServerEncoder | LegacyServerEncoder;
    };
};

/**
 * FFmpeg 8 移行前のエンコーダー名を現在の正規識別子へ変換する
 * @param encoder API から取得したエンコーダー名
 * @return 現在の正規識別子
 */
function normalizeServerEncoder(encoder: ServerEncoder | LegacyServerEncoder): ServerEncoder {
    switch (encoder) {
        case 'QSVEncC':
            return 'QSV';
        case 'NVEncC':
            return 'NVENC';
        case 'VCEEncC':
            return 'AMF';
        default:
            return encoder;
    }
}

/* サーバー設定を表すインターフェースのデフォルト値 */
export const IServerSettingsDefault: IServerSettings = {
    general: {
        backend: 'EDCB',
        jikkyo_enabled: false,
        always_receive_tv_from_mirakurun: false,
        edcb_url: 'tcp://127.0.0.1:4510/',
        mirakurun_url: 'http://127.0.0.1:40772/',
        konomitv_bs4k_live_transport: 'MpegTs',
        konomitv_bs4k_tlv_mirakurun_url: null,
        encoder: 'FFmpeg',
        konomitv_bs4k_live_sar_mode: 'CPU',
        encoder_bs4k: 'FFmpeg',
        encoder_bs4k_input_probesize: 3000,
        encoder_bs4k_input_analyze: 1.5,
        encoder_bs4k_input_analysis_enabled: true,
        encoder_bs4k_max_interleave_delta: 800,
        encoder_bs4k_low_latency: false,
        bs4k_ignore_viewer_low_latency: true,
        bs4k_live_startup_discard_enabled: true,
        bs4k_live_startup_discard_seconds: 2.0,
        program_update_interval: 5.0,
        debug: false,
        debug_encoder: false,
    },
    server: {
        https_mode: 'akebi',
        port: 7000,
        opencode_serve_port: 4097,
        custom_https_certificate: null,
        custom_https_private_key: null,
        reverse_proxy_listen_address: '0.0.0.0',
        trusted_proxy_cidrs: [],
    },
    compatibility_api: {
        enabled: false,
        port: 7200,
        profile: 'KomorebiV1',
        https_mode: 'inherit',
        custom_https_certificate: null,
        custom_https_private_key: null,
        reverse_proxy_listen_address: '0.0.0.0',
        trusted_proxy_cidrs: [],
    },
    tv: {
        preferred_terrestrial_region: null,
        max_alive_time: 10,
        debug_mode_ts_path: null,
    },
    video: {
        recorded_folders: [],
        exclude_scan_paths: [],
        recorded_fmp4_cache_folder: null,
        recorded_playback_index_backfill_enabled: true,
    },
    capture: {
        upload_folders: [],
    },
    cm_analysis: {
        enabled: false,
        logo_directory: null,
        excluded_directories: [],
    },
};


class Settings {

    /**
     * クライアント設定を取得する
     * @return クライアント設定 (取得に失敗した場合は null)
     */
    static async fetchClientSettings(): Promise<IClientSettings | null> {

        // API リクエストを実行
        const response = await APIClient.get<IClientSettings>('/settings/client');

        // エラー処理 (基本起こらないはず & 実行できなくても後続の処理に影響しないため何もしない)
        if (response.type === 'error') {
            return null;
        }

        // クライアント側の IClientSettings とサーバー側の app.config.ClientSettings は、バージョン差などで微妙に並び替え順序などが異なることがある
        // ハッシュ化時の文字列比較を正しく行うため、厳密にクライアント側の IClientSettings と一致するように変換する
        return getSyncableClientSettings(response.data);
    }


    /**
     * クライアント設定を更新する
     * @param settings クライアント設定
     * @return 成功した場合は true
     */
    static async updateClientSettings(settings: IClientSettings): Promise<boolean> {

        // API リクエストを実行
        const response = await APIClient.put<IClientSettings>('/settings/client', settings);

        // エラー処理
        if (response.type === 'error') {
            switch (response.data.detail) {
                default:
                    APIClient.showGenericError(response, 'クライアント設定を更新できませんでした。');
                    break;
            }
            return false;
        }

        return true;
    }


    /**
     * サーバー設定を取得する
     * @return サーバー設定 (取得に失敗した場合は null)
     */
    static async fetchServerSettings(): Promise<IServerSettings | null> {

        // API リクエストを実行
        const response = await APIClient.get<ServerSettingsResponse>('/settings/server');

        // エラー処理
        if (response.type === 'error') {
            switch (response.data.detail) {
                default:
                    APIClient.showGenericError(response, 'サーバー設定を取得できませんでした。');
                    break;
            }
            return null;
        }

        // 再起動前の旧サーバープロセスから旧名が返っても、画面状態と次回保存値には正規識別子だけを使う
        return {
            ...response.data,
            general: {
                ...response.data.general,
                encoder: normalizeServerEncoder(response.data.general.encoder),
                encoder_bs4k: normalizeServerEncoder(response.data.general.encoder_bs4k),
            },
        };
    }

    /**
     * サーバー設定を更新する
     * @param settings サーバー設定
     * @return 成功した場合は true
     */
    static async updateServerSettings(settings: IServerSettings): Promise<boolean> {

        // API リクエストを実行
        const response = await APIClient.put<IServerSettings>('/settings/server', settings);

        // エラー処理
        if (response.type === 'error') {
            switch (response.data.detail) {
                default:
                    APIClient.showGenericError(response, 'サーバー設定を更新できませんでした。');
                    break;
            }
            return false;
        }

        return true;
    }
}

export default Settings;
