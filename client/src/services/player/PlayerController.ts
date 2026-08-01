
import assert from 'assert';

import CanvasRenderer from 'aribb24.js/src/canvas-renderer';
import DPlayer, { DPlayerType } from 'dplayer';
import Hls, { ErrorDetails, type ErrorData } from 'hls.js';
import mpegts from 'mpegts.js';
import { watch } from 'vue';

import Message from '@/message';
import APIClient from '@/services/APIClient';
import ARIBTTMLRenderer from '@/services/player/ARIBTTMLRenderer';
import {
    AUTO_PLAYBACK_LIVE_VIDEO_CODEC_ORDER,
    AUTO_PLAYBACK_RECORDED_VIDEO_CODEC_ORDER,
    estimateNetworkHeightCeiling,
    getAutoPlaybackQualityLadder,
    getNextLowerAutoPlaybackQuality,
    getPlaybackQualityHeight,
    readNetworkQualityMetrics,
    resolveAutoLowLatencyFromNetwork,
    resolveAutoPlaybackQuality,
    resolveEfficiencyFirstPlaybackCombination,
} from '@/services/player/AutoPlaybackProfile';
import CustomBufferController from '@/services/player/CustomBufferController';
import KonomiTVBS4KLiveMSEPipeline, {
    IKonomiTVBS4KLivePipelineSpec,
    IKonomiTVBS4KTimedID3HistoryEntry,
} from '@/services/player/KonomiTVBS4KLiveMSEPipeline';
import KonomiTVBS4KLiveQualitySwitchCoordinator, {
    isKonomiTVBS4KTwoPipelineLivePlaybackEligible,
} from '@/services/player/KonomiTVBS4KLiveQualitySwitchCoordinator';
import {
    createLivePlaybackPolicy,
    ILivePlaybackPolicy,
    reconfigureLivePlaybackPolicy,
    resolveInitialLivePlaybackPositionSeconds,
} from '@/services/player/LivePlaybackPolicy';
import CaptureManager from '@/services/player/managers/CaptureManager';
import DocumentPiPManager from '@/services/player/managers/DocumentPiPManager';
import KeyboardShortcutManager from '@/services/player/managers/KeyboardShortcutManager';
import LiveCommentManager from '@/services/player/managers/LiveCommentManager';
import LiveDataBroadcastingManager from '@/services/player/managers/LiveDataBroadcastingManager';
import LiveEventManager from '@/services/player/managers/LiveEventManager';
import MediaSessionManager from '@/services/player/managers/MediaSessionManager';
import RecordedCMSkipManager from '@/services/player/managers/RecordedCMSkipManager';
import PlayerManager from '@/services/player/PlayerManager';
import Videos, {
    type IKonomiTVBS4KPlaybackCapabilities,
    type IKonomiTVBS4KPlaybackEncoder,
} from '@/services/Videos';
import useChannelsStore from '@/stores/ChannelsStore';
import usePlayerStore from '@/stores/PlayerStore';
import useServerSettingsStore from '@/stores/ServerSettingsStore';
import useSettingsStore, {
    BS4KLiveStreamingQuality,
    BS4K_LIVE_STREAMING_QUALITIES,
    getKonomiTVBS4KPlaybackAudioCodecSettingKey,
    getKonomiTVBS4KPlaybackVideoCodecSettingKey,
    IKonomiTVBS4KPlaybackVideoProfile,
    LiveStreamingQuality,
    LIVE_STREAMING_QUALITIES,
    KonomiTVBS4KPlaybackAudioCodec,
    KonomiTVBS4KPlaybackVideoCodec,
    VIDEO_STREAMING_QUALITIES,
} from '@/stores/SettingsStore';
import Utils, { dayjs, type KonomiTVBS4KPlaybackSelectableQuality, PlayerUtils } from '@/utils';


// 1つの初期化世代で固定する Wi-Fi / Cellular の低遅延設定値
// 通常局と BS4K で別キーを持つが、解決後のタプル形状は同じにする。
type LiveLowLatencySettings = readonly [
    wifi_low_latency_mode: boolean,
    cellular_low_latency_mode: boolean,
];


/**
 * 動画プレイヤーである DPlayer に関連するロジックを丸ごとラップするクラスで、再生系ロジックの中核を担う
 * DPlayer の初期化後は DPlayer が発行するイベントなどに合わせ、各イベントハンドラーや PlayerManager を管理する
 *
 * このクラスはコンストラクタで指定されたチャンネル or 録画番組の再生に責任を持つ
 * await destroy() 後に再度 await init() すると、コンストラクタに渡したのと同じチャンネル or 録画番組のプレイヤーを再起動できる
 * 再生対象が他のチャンネル or 録画番組に切り替えられた際は、既存の PlayerController を破棄し、新たに PlayerController を作り直す必要がある
 * 実装上、このクラスのインスタンスは必ずアプリケーション上で1つだけ存在するように実装する必要がある
 */
class PlayerController {

    // 何秒視聴したら視聴履歴に追加するかの閾値 (秒)
    private static readonly WATCHED_HISTORY_THRESHOLD_SECONDS = 30;

    // 視聴履歴の更新間隔 (秒)
    private static readonly WATCHED_HISTORY_UPDATE_INTERVAL = 10;

    // 録画末尾への直接シークと自然完走を区別するための許容誤差 (秒)
    private static readonly RECORDED_PLAYBACK_END_TOLERANCE_SECONDS = 0.25;

    // CM 自動スキップ先は HLS のタイムライン補正で指定位置からわずかにずれるため、その許容誤差 (秒)
    private static readonly RECORDED_CM_SKIP_TARGET_TOLERANCE_SECONDS = 1.0;

    // 自動画質: 段階下げのクールダウン (ms)。連続切替でエンコード負荷・チラつきを抑える
    private static readonly AUTO_QUALITY_STEP_DOWN_COOLDOWN_MS = 12_000;

    // 自動画質: 初回再生開始前の waiting / バッファ不足がこの時間を超えたら 1 段下げる (ms)
    // コールドスタートのエンコード起動を誤検知しないようやや長め
    private static readonly AUTO_QUALITY_STARTUP_STALL_MS = 12_000;

    // 自動画質: 再生開始後の waiting 継続がこの時間を超えたら 1 段下げる (ms)
    private static readonly AUTO_QUALITY_MIDSTREAM_STALL_MS = 2_500;

    // 自動画質: 「ギリギリ安定」とみなすバッファ秒数の上限（未満が続くと下げる）
    private static readonly AUTO_QUALITY_MARGINAL_BUFFER_SECONDS = 1.2;

    // 自動画質: ギリギリバッファがこの時間続いたら 1 段下げる (ms)
    private static readonly AUTO_QUALITY_MARGINAL_BUFFER_STREAK_MS = 8_000;

    // 自動画質: 監視ポーリング間隔 (ms)
    private static readonly AUTO_QUALITY_MONITOR_INTERVAL_MS = 1_000;

    // DPlayer のインスタンス
    private player: DPlayer | null = null;

    // それぞれの PlayerManager のインスタンスのリスト
    private player_managers: PlayerManager[] = [];

    // A/B Commit と Rollback が近接しても、再初期化対象 Manager を重ねて起動しないための直列鎖
    private live_quality_manager_restart_chain: Promise<void> = Promise.resolve();
    private live_quality_manager_restart_generation = 0;
    private live_prepare_connection_slot_released = false;

    // 再生モード (Live: ライブ視聴, Video: ビデオ視聴)
    private readonly playback_mode: 'Live' | 'Video';

    // ARIB STD-B24 字幕の符号化プロファイル (A: フルセグ / C: ワンセグ)
    private aribb24_profile: 'A' | 'C' = 'A';

    // ISDB-S3 の字幕・文字スーパーを共通処理する ARIB-TTML レンダラー
    private arib_ttml_renderer: ARIBTTMLRenderer | null = null;

    // ライブの timed-ID3 listener を現在の mpegts.js インスタンスへ一度だけ登録するための記録
    private live_arib_ttml_source: {
        on: (event: string, listener: (data: {pts: number; original_pts?: number; data: Uint8Array}) => void) => void;
        off: (event: string, listener: (data: {pts: number; original_pts?: number; data: Uint8Array}) => void) => void;
    } | null = null;
    private readonly live_arib_ttml_handler = (data: {pts: number; original_pts?: number; data: Uint8Array}): void => {
        const aribb24_plugins = this.player?.plugins as unknown as {
            aribb24Caption?: CanvasRenderer;
            aribb24Superimpose?: CanvasRenderer;
        } | undefined;
        aribb24_plugins?.aribb24Caption?.pushID3v2Data(data.pts / 1000, data.data);
        aribb24_plugins?.aribb24Superimpose?.pushID3v2Data(data.pts / 1000, data.data);
        this.arib_ttml_renderer?.pushID3v2Data(
            data.pts / 1000,
            data.data,
            data.original_pts === undefined ? null : data.original_pts / 1000,
        );
    };

    // 画質プロファイル (Wi-Fi 回線時 / モバイル回線時)
    // デフォルトは自動判定だが、ユーザーによって手動変更されうる
    private quality_profile_type: 'Wi-Fi' | 'Cellular';

    // ライブ視聴: mpegts.js のバッファ詰まり対策で定期的に強制シークするインターバルをキャンセルする関数
    private live_force_seek_interval_timer_cancel: (() => void) | null = null;

    // ライブ視聴: 動的 PMT で音声トラック数が変化した場合に設定パネルを追従するためのインターバルをキャンセルする関数
    private live_audio_track_interval_timer_cancel: (() => void) | null = null;
    private live_selected_audio_track_index = 0;

    // ビデオ視聴: ビデオストリームのアクティブ状態を維持するために Keep-Alive API にリクエストを送るインターバルのキャンセルする関数
    private video_keep_alive_interval_timer_cancel: (() => void) | null = null;

    // 同じ hls.js インスタンスへ音声トラックイベントを重複登録しないための記録
    private readonly recorded_hls_audio_selector_instances = new WeakSet<Hls>();

    // シークやHLS再読み込みでhls.jsがTrack 1へ初期化しても、選択中の録画音声を復元するための安定キー
    private recorded_selected_audio_track_name: string | null = null;

    // この PlayerController 内で Opus の実再生に失敗した場合、この再生だけ AAC へフォールバックする
    // 保存設定は書き換えず、ユーザーがプレイヤーから明示的に選び直した場合だけ再試行する
    private konomitv_bs4k_force_recorded_aac_audio_codec = false;

    /**
     * 自動画質選択モード中のプレイヤー一時オーバーライド（SettingsStore には書かない）。
     * 視聴セッション（この Controller）の寿命に限定する。
     */
    private konomitv_bs4k_auto_session_override: {
        streaming_quality?: LiveStreamingQuality | BS4KLiveStreamingQuality;
        video_codec?: KonomiTVBS4KPlaybackVideoCodec;
        audio_codec?: KonomiTVBS4KPlaybackAudioCodec;
        low_latency?: boolean;
    } = {};

    /** 自動モードで実再生に失敗した映像 codec（段階的落としでスキップする） */
    private konomitv_bs4k_auto_failed_video_codecs =
        new Set<KonomiTVBS4KPlaybackVideoCodec>();

    /** 自動画質段階下げモニタの setInterval 解除 */
    private auto_quality_monitor_timer_id: number | null = null;

    /** 自動画質: 直近の段階下げ時刻 (ms, performance/Date) */
    private auto_quality_step_down_last_at_ms = 0;

    /** 自動画質: 段階下げ処理中（二重起動防止） */
    private auto_quality_step_down_in_progress = false;

    /** 自動画質: 現セッションで一度でも playing に到達したか */
    private auto_quality_has_reached_playing = false;

    /** 自動画質: waiting 開始時刻 (ms)。非 waiting 時は null */
    private auto_quality_waiting_started_at_ms: number | null = null;

    /** 自動画質: ギリギリバッファ状態が始まった時刻 (ms)。健全時は null */
    private auto_quality_marginal_buffer_started_at_ms: number | null = null;

    // live / recorded の実 append・decode が失敗した場合、同じ PlayerController では一度だけ
    // AVC / AAC へ再初期化する。再初期化を跨いで保持し、失敗ループを防止する。
    private konomitv_bs4k_compatibility_playback_fallback_attempted = false;

    // 現在の再生初期化で実際に要求した映像・音声コーデック
    private konomitv_bs4k_playback_video_codec_for_current_playback: KonomiTVBS4KPlaybackVideoCodec = 'avc';
    private konomitv_bs4k_playback_video_bit_depth_for_current_playback: 8 | 10 = 8;
    private konomitv_bs4k_playback_audio_codec_for_current_playback: KonomiTVBS4KPlaybackAudioCodec = 'aac';
    private konomitv_bs4k_current_playback_has_video = true;

    // 現在の初期化世代で取得した共通再生能力と、選択中の通常 / BS4K エンコーダー
    // 再生解決は targeted（現在候補+互換fallback）、設定パネルは full 行列を使う。
    // targeted だけでパネルを作ると、未 probe の codec が ProbeFailed と誤表示される。
    private konomitv_bs4k_playback_capabilities_for_current_playback: IKonomiTVBS4KPlaybackCapabilities = {
        video: [],
        audio: [],
        live_combinations: [],
    };
    private konomitv_bs4k_playback_capabilities_for_ui: IKonomiTVBS4KPlaybackCapabilities = {
        video: [],
        audio: [],
        live_combinations: [],
    };
    private konomitv_bs4k_playback_encoder_for_current_playback: IKonomiTVBS4KPlaybackEncoder = 'FFmpeg';
    private konomitv_bs4k_playback_video_profile_for_current_playback: IKonomiTVBS4KPlaybackVideoProfile = {
        is_bs4k: false,
        streaming_quality: '1080p',
    };

    // DPlayer が guard を経由せず非対応画質への切り替えを開始した場合も、旧画質への再起動要求を一度だけにする
    private konomitv_bs4k_unsupported_quality_restart_pending = false;

    // fMP4 録画のARIB字幕先読みハンドラーを解除する関数
    private recorded_arib_subtitle_cancel: (() => void) | null = null;

    // 画質切り替え後の video 要素へ fMP4 録画のARIB字幕先読みハンドラーを付け直す関数
    private recorded_arib_subtitle_restart: (() => void) | null = null;

    // fMP4 録画のARIB-TTML timed-ID3先読みハンドラーと再接続関数
    private recorded_arib_ttml_cancel: (() => void) | null = null;
    private recorded_arib_ttml_restart: (() => void) | null = null;

    // 録画を末尾まで自然に再生し終えたかどうか
    // ended の多重通知防止と、完走後の視聴履歴を先頭付近へ戻す判断に共用する
    private recorded_playback_ended = false;

    // シーク操作で直接末尾へ移動したときに、自然な完走として扱わないためのフラグ
    // シーク後に末尾より前へ戻った時点、または CM 自動スキップと確認できた時点で解除する
    private recorded_playback_end_blocked_by_seek = false;

    // RecordedCMSkipManager が開始したシーク先
    // 通常のユーザーシークと、自然再生中に跨いだ CM の自動スキップを区別するために保持する
    private recorded_auto_skip_cm_target: number | null = null;

    // setupPlayerContainerResizeHandler() で利用する ResizeObserver
    // 保持しておかないと disconnect() で ResizeObserver を止められない
    private player_container_resize_observer: ResizeObserver | null = null;

    // setControlDisplayTimer() で利用するタイマー ID
    // 保持しておかないと clearTimeout() でタイマーを止められない
    private player_control_ui_hide_timer_id: number = 0;

    // 視聴履歴に追加すべきかを判断するためのタイムアウトの ID
    private watched_history_threshold_timer_id: number = 0;

    // Screen Wake Lock API の WakeLockSentinel のインスタンス
    // 確保した起動ロックを解放するために保持しておく必要がある
    // Screen Wake Lock API がサポートされていない場合やリクエストに失敗した場合は null になる
    private screen_wake_lock: WakeLockSentinel | null = null;

    // RomSound の AudioContext と AudioBuffer のリスト
    private readonly romsounds_context: AudioContext = new AudioContext();
    private readonly romsounds_buffers = new Map<number, AudioBuffer>();
    private readonly romsounds_ready: Promise<void>;

    // L字画面のクロップ設定で使うウォッチャーを保持する配列
    private lshaped_screen_crop_watchers: (() => void)[] = [];

    // ライブ視聴中の実効低遅延設定を監視するウォッチャーの解除関数
    // setupLivePlaybackPolicyWatcher() で登録し、destroy() で旧世代から確実に切り離す
    private live_playback_policy_watcher_cancel: (() => void) | null = null;

    // 現在の再生世代で共有する低遅延ポリシー（ソフト切替で in-place 更新する）
    private session_live_playback_policy: ILivePlaybackPolicy | null = null;

    // 破棄中かどうか
    private destroying = false;

    // 進行中の破棄処理
    // 重複した destroy() 呼び出しを同じ完了待ちへ合流させるために保持する
    private destroy_promise: Promise<void> | null = null;
    // Server cleanup未確認で破棄済みになった後も、再destroyを成功へ反転させない。
    private destroy_error: unknown = null;

    // 進行中の再起動処理
    // 手動・自動の再起動要求を同じ再起動世代へ合流させるために保持する
    private restart_promise: Promise<void> | null = null;

    // 進行中の初期化処理
    // owner の破棄時は世代を中断した上で、この Promise の完了後にリソースを回収する
    private init_promise: Promise<void> | null = null;

    // View が所有するライフサイクル。route change / unmount 後の再初期化を禁止する
    private readonly owner_signal: AbortSignal | null;

    // 同じ controller を再初期化した場合も、旧 DPlayer の非同期処理を新世代から分離する
    private lifecycle_generation = 0;
    private lifecycle_abort_controller = new AbortController();

    // DPlayer の native error は quality_start ごとに再登録すると同じ実エラーを重複処理するため、
    // 同一 DPlayer / 初期化世代ですでに登録したかを保持する
    private konomitv_bs4k_native_playback_error_handler_registration: {
        konomitv_bs4k_player: DPlayer;
        konomitv_bs4k_lifecycle_generation: number;
    } | null = null;

    // 破棄済みかどうか
    private destroyed = false;

    // ライブ再生開始時の一時ミュートを、保存済みミュートと区別するフラグ
    // 一時ミュートで発火した volumechange を、ユーザー操作として保存しないために使う
    private is_live_startup_temporary_muted = false;

    // ライブ視聴: 直近の mpegts.js mediaInfo
    private live_media_info: {[key: string]: any} | null = null;

    // ライブStartup ProbeとA/B画質切替を単一所有するCoordinator
    private konomitv_bs4k_live_quality_switch_coordinator:
    KonomiTVBS4KLiveQualitySwitchCoordinator | null = null;

    // Server cleanup未確認後は同じcontrollerから新しいLive pipelineを作らない。
    private konomitv_bs4k_live_cleanup_blocked = false;


    /**
     * コンストラクタ
     * 実際の DPlayer の初期化処理は await init() で行われる
     */
    constructor(playback_mode: 'Live' | 'Video', owner_signal: AbortSignal | null = null) {

        // 再生モードをセット
        this.playback_mode = playback_mode;
        this.owner_signal = owner_signal;
        this.setupOwnerAbortCleanup();

        const player_store = usePlayerStore();
        if (player_store.selected_quality_profile_type !== null) {
            // 視聴画面内で手動変更済みなら、プレイヤー再作成後もその選択を使う
            // 視聴画面を離れると PlayerStore.reset() で null に戻り、次回は回線種別から選び直す
            this.quality_profile_type = player_store.selected_quality_profile_type;
        } else {
            // 手動変更がない場合は、現在の回線種別から画質プロファイルを選ぶ
            // Wi-Fi 回線の場合や回線種別を取得できなかった場合は、Wi-Fi 向けの画質プロファイルを使う
            const network_circuit_type = PlayerUtils.getNetworkCircuitType();
            if (network_circuit_type === 'Cellular') {
                this.quality_profile_type = 'Cellular';
            } else {
                this.quality_profile_type = 'Wi-Fi';
            }
        }

        // 01 ~ 14 を番号を崩さず並列取得し、初回再生は準備完了を待つ。
        this.romsounds_ready = Promise.all(Array.from({length: 14}, (_, index) => index + 1).map(async (index) => {
            try {
                // ArrayBuffer をデコードして AudioBuffer にし、すぐ呼び出せるように貯めておく
                // ref: https://ics.media/entry/200427/
                const romsound_url = `/assets/romsounds/${index.toString().padStart(2, '0')}.wav`;
                const romsound_response = await APIClient.get<ArrayBuffer>(romsound_url, {
                    baseURL: '',  // BaseURL を明示的にクライアントのルートに設定
                    responseType: 'arraybuffer',
                });
                if (romsound_response.type === 'success') {
                    this.romsounds_buffers.set(
                        index,
                        await this.romsounds_context.decodeAudioData(romsound_response.data),
                    );
                }
            } catch (error) {
                console.warn(`[PlayerController] Failed to preload RomSound ${index}.`, error);
            }
        })).then(() => undefined);
    }


    private setupOwnerAbortCleanup(): void {
        this.owner_signal?.addEventListener('abort', () => {
            void this.destroy().catch((error) => {
                console.error('[PlayerController] Owner abort cleanup failed.', error);
            });
        }, {once: true});
    }


    /** 現在の回線プロファイルで自動画質選択モードが有効か。 */
    private isKonomiTVBS4KAutoQualityModeEnabled(): boolean {
        const settings_store = useSettingsStore();
        return this.quality_profile_type === 'Cellular' ?
            settings_store.settings.konomitv_bs4k_playback_auto_quality_mode_cellular === true :
            settings_store.settings.konomitv_bs4k_playback_auto_quality_mode === true;
    }


    /** 自動モード用: ソース映像の高さ（不明時は null）。 */
    private getKonomiTVBS4KSourceVideoHeight(): number | null {
        if (this.playback_mode === 'Video') {
            const height = usePlayerStore().recorded_program.recorded_video.video_resolution_height;
            return typeof height === 'number' && height > 0 ? height : null;
        }
        // ライブは番組情報の解像度（"1080i" / "2160p" / "1920x1080" 等）から推定する
        const resolution = useChannelsStore().channel.current.program_present?.video_resolution;
        if (typeof resolution !== 'string') {
            return null;
        }
        const wh_match = resolution.match(/(\d+)\s*[x×]\s*(\d+)/i);
        if (wh_match !== null) {
            const height = Number(wh_match[2]);
            return Number.isFinite(height) && height > 0 ? height : null;
        }
        const p_match = resolution.match(/(\d{3,4})\s*[ip]/i);
        if (p_match !== null) {
            const height = Number(p_match[1]);
            return Number.isFinite(height) && height > 0 ? height : null;
        }
        return null;
    }


    /**
     * 現在の画質プロファイルタイプに応じた画質プロファイル
     */
    private get quality_profile(): {
        konomitv_bs4k_playback_streaming_quality: LiveStreamingQuality;
        konomitv_bs4k_playback_streaming_quality_for_bs4k: BS4KLiveStreamingQuality;
        konomitv_bs4k_playback_video_codec: KonomiTVBS4KPlaybackVideoCodec;
        konomitv_bs4k_playback_video_codec_for_bs4k: KonomiTVBS4KPlaybackVideoCodec;
        konomitv_bs4k_playback_audio_codec: KonomiTVBS4KPlaybackAudioCodec;
        konomitv_bs4k_playback_audio_codec_for_bs4k: KonomiTVBS4KPlaybackAudioCodec;
        konomitv_bs4k_playback_24fps_mode: boolean;
        konomitv_bs4k_playback_24fps_mode_for_bs4k: boolean;
        tv_low_latency_mode: boolean;
    } {
        const settings_store = useSettingsStore();
        const is_cellular = this.quality_profile_type === 'Cellular';
        const is_auto = this.isKonomiTVBS4KAutoQualityModeEnabled();
        const source_height = is_auto === true ? this.getKonomiTVBS4KSourceVideoHeight() : null;
        // セッションで既に段階下げ済みならその画質を優先。未固定時のみ回線推定で初期段を決める。
        const network_metrics = is_auto === true ? readNetworkQualityMetrics() : null;
        const session = this.konomitv_bs4k_auto_session_override;

        // モバイル回線向けの画質プロファイルを返す
        if (is_cellular === true) {
            let normal_quality = settings_store.settings.konomitv_bs4k_playback_streaming_quality_cellular;
            let bs4k_quality = settings_store.settings.konomitv_bs4k_playback_streaming_quality_for_bs4k_cellular;
            let video_codec = settings_store.settings.konomitv_bs4k_playback_video_codec_cellular;
            let video_codec_bs4k = settings_store.settings.konomitv_bs4k_playback_video_codec_for_bs4k_cellular;
            let audio_codec = settings_store.settings.konomitv_bs4k_playback_audio_codec_cellular;
            let audio_codec_bs4k = settings_store.settings.konomitv_bs4k_playback_audio_codec_for_bs4k_cellular;
            let low_latency = settings_store.settings.tv_low_latency_mode_cellular;
            if (is_auto === true) {
                normal_quality = resolveAutoPlaybackQuality(
                    getAutoPlaybackQualityLadder(false, this.playback_mode === 'Live') as readonly LiveStreamingQuality[],
                    settings_store.settings.konomitv_bs4k_playback_auto_quality_max_cellular,
                    source_height,
                    network_metrics,
                ) as LiveStreamingQuality;
                bs4k_quality = resolveAutoPlaybackQuality(
                    getAutoPlaybackQualityLadder(true, true) as readonly BS4KLiveStreamingQuality[],
                    settings_store.settings.konomitv_bs4k_playback_auto_quality_max_for_bs4k_cellular,
                    source_height,
                    network_metrics,
                ) as BS4KLiveStreamingQuality;
                // 初期シード。capabilities 取得後に効率優先で自動解決してセッションへ固定する。
                video_codec = session.video_codec ?? 'av1';
                video_codec_bs4k = session.video_codec ?? 'av1';
                // 自動モードのシードは Opus 優先（capabilities 解決後に exact で確定）
                audio_codec = session.audio_codec ?? 'opus';
                audio_codec_bs4k = session.audio_codec ?? 'opus';
                low_latency = session.low_latency ?? resolveAutoLowLatencyFromNetwork();
                if (session.streaming_quality !== undefined) {
                    // session override は通常/BS4K のどちらにも適用（現在の再生種別に合わせて使う）
                    if ((LIVE_STREAMING_QUALITIES as readonly string[]).includes(session.streaming_quality)) {
                        normal_quality = session.streaming_quality as LiveStreamingQuality;
                    }
                    if ((BS4K_LIVE_STREAMING_QUALITIES as readonly string[]).includes(session.streaming_quality)) {
                        bs4k_quality = session.streaming_quality as BS4KLiveStreamingQuality;
                    }
                }
            }
            return {
                konomitv_bs4k_playback_streaming_quality: normal_quality,
                konomitv_bs4k_playback_streaming_quality_for_bs4k: bs4k_quality,
                konomitv_bs4k_playback_video_codec: video_codec,
                konomitv_bs4k_playback_video_codec_for_bs4k: video_codec_bs4k,
                konomitv_bs4k_playback_audio_codec: audio_codec,
                konomitv_bs4k_playback_audio_codec_for_bs4k: audio_codec_bs4k,
                // 自動モードでは 24fps を使わない
                konomitv_bs4k_playback_24fps_mode: is_auto === true ? false :
                    settings_store.settings.konomitv_bs4k_playback_24fps_mode_cellular,
                konomitv_bs4k_playback_24fps_mode_for_bs4k: is_auto === true ? false :
                    settings_store.settings.konomitv_bs4k_playback_24fps_mode_for_bs4k_cellular,
                tv_low_latency_mode: low_latency,
            };
        }

        // Wi-Fi 回線向けの画質プロファイルを返す
        let normal_quality = settings_store.settings.konomitv_bs4k_playback_streaming_quality;
        let bs4k_quality = settings_store.settings.konomitv_bs4k_playback_streaming_quality_for_bs4k;
        let video_codec = settings_store.settings.konomitv_bs4k_playback_video_codec;
        let video_codec_bs4k = settings_store.settings.konomitv_bs4k_playback_video_codec_for_bs4k;
        let audio_codec = settings_store.settings.konomitv_bs4k_playback_audio_codec;
        let audio_codec_bs4k = settings_store.settings.konomitv_bs4k_playback_audio_codec_for_bs4k;
        let low_latency = settings_store.settings.tv_low_latency_mode;
        if (is_auto === true) {
            normal_quality = resolveAutoPlaybackQuality(
                getAutoPlaybackQualityLadder(false, this.playback_mode === 'Live') as readonly LiveStreamingQuality[],
                settings_store.settings.konomitv_bs4k_playback_auto_quality_max,
                source_height,
                network_metrics,
            ) as LiveStreamingQuality;
            bs4k_quality = resolveAutoPlaybackQuality(
                getAutoPlaybackQualityLadder(true, true) as readonly BS4KLiveStreamingQuality[],
                settings_store.settings.konomitv_bs4k_playback_auto_quality_max_for_bs4k,
                source_height,
                network_metrics,
            ) as BS4KLiveStreamingQuality;
            video_codec = session.video_codec ?? 'av1';
            video_codec_bs4k = session.video_codec ?? 'av1';
            // 自動モードのシードは Opus 優先（capabilities 解決後に exact で確定）
            audio_codec = session.audio_codec ?? 'opus';
            audio_codec_bs4k = session.audio_codec ?? 'opus';
            low_latency = session.low_latency ?? resolveAutoLowLatencyFromNetwork();
            if (session.streaming_quality !== undefined) {
                if ((LIVE_STREAMING_QUALITIES as readonly string[]).includes(session.streaming_quality)) {
                    normal_quality = session.streaming_quality as LiveStreamingQuality;
                }
                if ((BS4K_LIVE_STREAMING_QUALITIES as readonly string[]).includes(session.streaming_quality)) {
                    bs4k_quality = session.streaming_quality as BS4KLiveStreamingQuality;
                }
            }
        }
        return {
            konomitv_bs4k_playback_streaming_quality: normal_quality,
            konomitv_bs4k_playback_streaming_quality_for_bs4k: bs4k_quality,
            konomitv_bs4k_playback_video_codec: video_codec,
            konomitv_bs4k_playback_video_codec_for_bs4k: video_codec_bs4k,
            konomitv_bs4k_playback_audio_codec: audio_codec,
            konomitv_bs4k_playback_audio_codec_for_bs4k: audio_codec_bs4k,
            konomitv_bs4k_playback_24fps_mode: is_auto === true ? false :
                settings_store.settings.konomitv_bs4k_playback_24fps_mode,
            konomitv_bs4k_playback_24fps_mode_for_bs4k: is_auto === true ? false :
                settings_store.settings.konomitv_bs4k_playback_24fps_mode_for_bs4k,
            tv_low_latency_mode: low_latency,
        };
    }


    /** 現在の再生種別で DPlayer に表示する suffix 付与前の API 画質一覧を返す。 */
    private getKonomiTVBS4KPlaybackSelectableQualities(
        is_konomitv_bs4k: boolean,
    ): readonly KonomiTVBS4KPlaybackSelectableQuality[] {
        if (is_konomitv_bs4k === true) return BS4K_LIVE_STREAMING_QUALITIES;
        return this.playback_mode === 'Live' ? LIVE_STREAMING_QUALITIES : VIDEO_STREAMING_QUALITIES;
    }


    /**
     * 保存画質より options.default_quality を優先し、初期 MSE 判定と DPlayer の既定画質で共有する実効画質を返す。
     */
    private resolveKonomiTVBS4KEffectivePlaybackVideoProfile(
        is_konomitv_bs4k: boolean,
        konomitv_bs4k_default_quality: string | null,
    ): IKonomiTVBS4KPlaybackVideoProfile {
        const konomitv_bs4k_available_qualities =
            this.getKonomiTVBS4KPlaybackSelectableQualities(is_konomitv_bs4k);
        const konomitv_bs4k_saved_quality = is_konomitv_bs4k === true ?
            this.quality_profile.konomitv_bs4k_playback_streaming_quality_for_bs4k :
            this.quality_profile.konomitv_bs4k_playback_streaming_quality;
        const konomitv_bs4k_normalized_saved_quality =
            PlayerUtils.normalizeKonomiTVBS4KPlaybackAPIQuality(
                konomitv_bs4k_saved_quality,
                is_konomitv_bs4k,
                konomitv_bs4k_available_qualities,
            ) ?? konomitv_bs4k_available_qualities[0];
        const konomitv_bs4k_normalized_default_quality = konomitv_bs4k_default_quality === null ? null :
            PlayerUtils.normalizeKonomiTVBS4KPlaybackAPIQuality(
                konomitv_bs4k_default_quality,
                is_konomitv_bs4k,
                konomitv_bs4k_available_qualities,
            );
        return {
            is_bs4k: is_konomitv_bs4k,
            streaming_quality: konomitv_bs4k_normalized_default_quality ?? konomitv_bs4k_normalized_saved_quality,
        };
    }

    /** ラジオと音声のみ録画を、映像能力・SourceBuffer が不要な再生対象として判定する。 */
    private doesKonomiTVBS4KCurrentPlaybackHaveVideo(): boolean {
        if (this.playback_mode === 'Live') {
            return useChannelsStore().channel.current.is_radiochannel === false;
        }
        return usePlayerStore().recorded_program.recorded_video.has_video;
    }

    /**
     * 現在の再生対象について、共通能力とブラウザ MSE の積集合から実際に要求する codec を解決する。
     *
     * ライブ・ラジオと音声のみ録画は映像 SourceBuffer を作らないため、映像能力行がなくても
     * query 互換用の AVC / 8bit を保持して音声能力だけを検査する。
     */
    private resolveKonomiTVBS4KPlaybackCodecsForCurrentMedia(
        konomitv_bs4k_capabilities: IKonomiTVBS4KPlaybackCapabilities,
        konomitv_bs4k_encoder: IKonomiTVBS4KPlaybackEncoder,
        konomitv_bs4k_requested_video_codec: KonomiTVBS4KPlaybackVideoCodec,
        konomitv_bs4k_requested_audio_codec: KonomiTVBS4KPlaybackAudioCodec,
        konomitv_bs4k_video_profile: IKonomiTVBS4KPlaybackVideoProfile,
    ): {
            video_codec: KonomiTVBS4KPlaybackVideoCodec;
            video_bit_depth: 8 | 10;
            audio_codec: KonomiTVBS4KPlaybackAudioCodec;
        } {
        const konomitv_bs4k_video_codec_to_resolve: KonomiTVBS4KPlaybackVideoCodec =
            this.konomitv_bs4k_compatibility_playback_fallback_attempted === true ?
                'avc' : konomitv_bs4k_requested_video_codec;

        // 録画 Opus の実 append に失敗した後は、保存値を変えずにこの Controller だけ AAC を再要求する。
        const konomitv_bs4k_audio_codec_to_resolve: KonomiTVBS4KPlaybackAudioCodec = (
            this.konomitv_bs4k_compatibility_playback_fallback_attempted === true ||
            (this.playback_mode === 'Video' && this.konomitv_bs4k_force_recorded_aac_audio_codec === true)
        ) ? 'aac' : konomitv_bs4k_requested_audio_codec;

        // 音声のみの再生は映像 SourceBuffer も映像 backend も使わないため、音声の集約能力だけを検査する。
        if (this.konomitv_bs4k_current_playback_has_video === false) {
            const konomitv_bs4k_selected_audio_capability =
                Videos.resolveKonomiTVBS4KPlaybackAudioOnlyCapability(
                    konomitv_bs4k_capabilities,
                    konomitv_bs4k_audio_codec_to_resolve,
                );
            if (konomitv_bs4k_selected_audio_capability === null) {
                throw new Error('No playback audio codec is supported by live, recorded, and this browser.');
            }
            if (
                this.konomitv_bs4k_compatibility_playback_fallback_attempted === true &&
                konomitv_bs4k_selected_audio_capability.codec !== 'aac'
            ) {
                throw new Error('AAC is unavailable for the one-time compatibility playback fallback.');
            }
            return {
                video_codec: 'avc',
                video_bit_depth: 8,
                audio_codec: konomitv_bs4k_selected_audio_capability.codec,
            };
        }

        // 映像を含む再生は、映像と音声を別々に fallback せず同一の exact combination として解決する。
        // 自動モードかつ session override が無いときは効率優先 (AV1→…→AVC / Opus→AAC)。
        const konomitv_bs4k_use_efficiency_first =
            this.isKonomiTVBS4KAutoQualityModeEnabled() === true &&
            this.konomitv_bs4k_auto_session_override.video_codec === undefined &&
            this.konomitv_bs4k_auto_session_override.audio_codec === undefined &&
            this.konomitv_bs4k_compatibility_playback_fallback_attempted === false &&
            this.konomitv_bs4k_force_recorded_aac_audio_codec === false;
        const konomitv_bs4k_selected_combination = konomitv_bs4k_use_efficiency_first === true ?
            resolveEfficiencyFirstPlaybackCombination(
                konomitv_bs4k_capabilities,
                konomitv_bs4k_encoder,
                konomitv_bs4k_video_profile,
                this.playback_mode === 'Live',
                this.konomitv_bs4k_auto_failed_video_codecs,
            ) :
            Videos.resolveKonomiTVBS4KPlaybackCombination(
                konomitv_bs4k_capabilities,
                konomitv_bs4k_encoder,
                konomitv_bs4k_video_codec_to_resolve,
                konomitv_bs4k_audio_codec_to_resolve,
                konomitv_bs4k_video_profile,
            );
        if (konomitv_bs4k_selected_combination === null) {
            throw new Error('No playback video and audio codec combination is supported by live, recorded, and this browser.');
        }
        const konomitv_bs4k_video_codec = konomitv_bs4k_selected_combination.video.codec;
        const konomitv_bs4k_video_bit_depth = konomitv_bs4k_selected_combination.video.bit_depth;
        const konomitv_bs4k_audio_codec = konomitv_bs4k_selected_combination.audio.codec;
        // 自動モードで確定した codec をセッションに固定し、再起動のたびに別 codec へ飛ばないようにする。
        // ユーザーがプレイヤーから選び直した場合は、その override が優先される。
        if (
            this.isKonomiTVBS4KAutoQualityModeEnabled() === true &&
            this.konomitv_bs4k_auto_session_override.video_codec === undefined &&
            this.konomitv_bs4k_auto_session_override.audio_codec === undefined
        ) {
            this.konomitv_bs4k_auto_session_override = {
                ...this.konomitv_bs4k_auto_session_override,
                video_codec: konomitv_bs4k_video_codec,
                audio_codec: konomitv_bs4k_audio_codec,
            };
        }
        if (
            this.konomitv_bs4k_compatibility_playback_fallback_attempted === true &&
            konomitv_bs4k_video_codec !== 'avc' &&
            this.konomitv_bs4k_current_playback_has_video === true
        ) {
            throw new Error('AVC is unavailable for the one-time compatibility playback fallback.');
        }
        if (
            this.konomitv_bs4k_compatibility_playback_fallback_attempted === true &&
            konomitv_bs4k_audio_codec !== 'aac'
        ) {
            throw new Error('AAC is unavailable for the one-time compatibility playback fallback.');
        }
        return {
            video_codec: konomitv_bs4k_video_codec,
            video_bit_depth: konomitv_bs4k_video_bit_depth,
            audio_codec: konomitv_bs4k_audio_codec,
        };
    }


    /** 現在の codec / bit depth を、指定された実画質で MSE SourceBuffer に追加できるか判定する。 */
    private isKonomiTVBS4KCurrentPlaybackVideoProfileSupported(
        konomitv_bs4k_profile: IKonomiTVBS4KPlaybackVideoProfile,
    ): boolean {
        if (this.konomitv_bs4k_current_playback_has_video === false) return true;
        const konomitv_bs4k_combination = Videos.resolveKonomiTVBS4KPlaybackCombination(
            this.konomitv_bs4k_playback_capabilities_for_current_playback,
            this.konomitv_bs4k_playback_encoder_for_current_playback,
            this.konomitv_bs4k_playback_video_codec_for_current_playback,
            this.konomitv_bs4k_playback_audio_codec_for_current_playback,
            konomitv_bs4k_profile,
        );
        return konomitv_bs4k_combination !== null &&
            konomitv_bs4k_combination.video.codec ===
                this.konomitv_bs4k_playback_video_codec_for_current_playback &&
            konomitv_bs4k_combination.video.bit_depth ===
                this.konomitv_bs4k_playback_video_bit_depth_for_current_playback &&
            konomitv_bs4k_combination.audio.codec ===
                this.konomitv_bs4k_playback_audio_codec_for_current_playback;
    }


    /** DPlayer の quality_start が渡す表示画質を、MSE 判定に用いる target profile へ変換する。 */
    private resolveKonomiTVBS4KPlaybackVideoProfileFromDPlayerQuality(
        konomitv_bs4k_target_quality: DPlayerType.VideoQuality,
    ): IKonomiTVBS4KPlaybackVideoProfile | null {
        const is_konomitv_bs4k =
            this.konomitv_bs4k_playback_video_profile_for_current_playback.is_bs4k;
        const konomitv_bs4k_target_api_quality =
            PlayerUtils.normalizeKonomiTVBS4KPlaybackAPIQuality(
                konomitv_bs4k_target_quality.name,
                is_konomitv_bs4k,
                this.getKonomiTVBS4KPlaybackSelectableQualities(is_konomitv_bs4k),
            );
        return konomitv_bs4k_target_api_quality === null ? null : {
            is_bs4k: is_konomitv_bs4k,
            streaming_quality: konomitv_bs4k_target_api_quality,
        };
    }


    /**
     * DPlayer が video 要素を差し替える前に切替先画質を検査し、現在の codec で非対応なら切り替え自体を開始しない。
     */
    private setupKonomiTVBS4KPlaybackQualitySwitchGuard(
        konomitv_bs4k_lifecycle_generation: number,
        konomitv_bs4k_setup_player: DPlayer,
    ): void {
        const konomitv_bs4k_original_switch_quality =
            konomitv_bs4k_setup_player.switchQuality.bind(konomitv_bs4k_setup_player);
        konomitv_bs4k_setup_player.switchQuality = (konomitv_bs4k_index: number): void => {
            if (
                this.isLifecycleCurrent(
                    konomitv_bs4k_lifecycle_generation,
                    konomitv_bs4k_setup_player,
                ) === false
            ) return;
            const konomitv_bs4k_target_quality =
                konomitv_bs4k_setup_player.options.video.quality?.[konomitv_bs4k_index];
            // DPlayer の想定外 index は undefined quality で plugin を作らせず、安全に拒否する。
            if (konomitv_bs4k_target_quality === undefined) return;
            const konomitv_bs4k_target_profile =
                this.resolveKonomiTVBS4KPlaybackVideoProfileFromDPlayerQuality(konomitv_bs4k_target_quality);
            // 自動モード中はプレイヤーでの画質変更をセッションへ即時記録する（再起動後も維持）。
            if (
                konomitv_bs4k_target_profile !== null &&
                this.isKonomiTVBS4KAutoQualityModeEnabled() === true &&
                this.playback_mode !== 'Live'
            ) {
                this.konomitv_bs4k_auto_session_override = {
                    ...this.konomitv_bs4k_auto_session_override,
                    streaming_quality: konomitv_bs4k_target_profile.streaming_quality,
                };
            }
            if (
                konomitv_bs4k_target_profile === null ||
                this.isKonomiTVBS4KCurrentPlaybackVideoProfileSupported(konomitv_bs4k_target_profile) === false
            ) {
                konomitv_bs4k_setup_player.notice(
                    `${konomitv_bs4k_target_quality.name} は現在の映像コーデックでは再生できません。`,
                    4000,
                    undefined,
                    'rgb(var(--v-theme-error-readable))',
                );
                return;
            }
            // Live coordinatorには「Prepare中のBを取り消して現Aを再選択」も渡す必要があるため、
            // DPlayer自身の同一index早期returnより先にroutingする。
            if (
                this.playback_mode === 'Live' &&
                this.konomitv_bs4k_live_quality_switch_coordinator !== null &&
                this.konomitv_bs4k_live_quality_switch_coordinator !== undefined
            ) {
                this.konomitv_bs4k_live_quality_switch_coordinator.requestQuality(
                    konomitv_bs4k_index,
                );
                return;
            }
            if (
                konomitv_bs4k_setup_player.qualityIndex === konomitv_bs4k_index ||
                konomitv_bs4k_setup_player.switchingQuality === true
            ) {
                konomitv_bs4k_original_switch_quality(konomitv_bs4k_index);
                return;
            }
            konomitv_bs4k_original_switch_quality(konomitv_bs4k_index);
        };
    }


    /**
     * quality_start の target profile を再検査し、guard を迂回した非対応切替は直前の対応画質へ一度だけ戻す。
     */
    private handleKonomiTVBS4KPlaybackQualityStart(
        konomitv_bs4k_lifecycle_generation: number,
        konomitv_bs4k_setup_player: DPlayer,
        konomitv_bs4k_target_quality: DPlayerType.VideoQuality,
    ): boolean {
        if (
            this.isLifecycleCurrent(
                konomitv_bs4k_lifecycle_generation,
                konomitv_bs4k_setup_player,
            ) === false
        ) return false;
        const konomitv_bs4k_previous_profile =
            this.konomitv_bs4k_playback_video_profile_for_current_playback;
        const konomitv_bs4k_target_profile =
            this.resolveKonomiTVBS4KPlaybackVideoProfileFromDPlayerQuality(
                konomitv_bs4k_target_quality,
            );
        if (
            konomitv_bs4k_target_profile !== null &&
            this.isKonomiTVBS4KCurrentPlaybackVideoProfileSupported(konomitv_bs4k_target_profile) === true
        ) {
            this.konomitv_bs4k_playback_video_profile_for_current_playback = konomitv_bs4k_target_profile;
            // 自動モード中の画質切替は設定へ保存せず、セッション一時オーバーライドのみ残す。
            if (this.isKonomiTVBS4KAutoQualityModeEnabled() === true) {
                this.konomitv_bs4k_auto_session_override = {
                    ...this.konomitv_bs4k_auto_session_override,
                    streaming_quality: konomitv_bs4k_target_profile.streaming_quality,
                };
            }
            return true;
        }
        konomitv_bs4k_setup_player.video.pause();
        if (this.konomitv_bs4k_unsupported_quality_restart_pending === true) return false;
        this.konomitv_bs4k_unsupported_quality_restart_pending = true;
        const konomitv_bs4k_resume_quality =
            PlayerUtils.getKonomiTVBS4KPlaybackQualityDisplayName(
                konomitv_bs4k_previous_profile.streaming_quality,
            );
        usePlayerStore().event_emitter.emit('PlayerRestartRequired', {
            message: `${konomitv_bs4k_target_quality.name} は現在の映像コーデックでは再生できないため、直前の画質へ戻します。`,
            is_error_message: false,
            should_resume_quality: false,
            konomitv_bs4k_resume_quality,
        });
        return false;
    }


    /** 現在の再生対象（通常 / BS4K）に対応する Wi-Fi / Cellular 低遅延設定を返す。 */
    private getLiveLowLatencySettingsForCurrentContext(): LiveLowLatencySettings {
        // 自動モード: 既存の低遅延設定を見ず、ネットワーク品質・速度のみで判定する。
        // session override があればそれを優先（プレイヤー一時変更・非保存）。
        if (this.isKonomiTVBS4KAutoQualityModeEnabled() === true) {
            const auto_low_latency = this.konomitv_bs4k_auto_session_override?.low_latency ??
                resolveAutoLowLatencyFromNetwork();
            return [auto_low_latency, auto_low_latency];
        }
        const settings_store = useSettingsStore();
        const is_bs4k_live = this.playback_mode === 'Live' &&
            useChannelsStore().channel.current.display_channel_id.startsWith('bs4k');
        if (is_bs4k_live === true) {
            return [
                settings_store.settings.tv_low_latency_mode_for_bs4k,
                settings_store.settings.tv_low_latency_mode_for_bs4k_cellular,
            ];
        }
        return [
            settings_store.settings.tv_low_latency_mode,
            settings_store.settings.tv_low_latency_mode_cellular,
        ];
    }


    /**
     * 現在の回線プロファイル・チャンネル・サーバー設定から、低遅延ポリシーを解決する
     *
     * 戻り値は初期化処理へ渡し、セッション中は同一参照を soft 切替で更新する。
     */
    private resolveLivePlaybackPolicy(
        live_low_latency_settings?: LiveLowLatencySettings,
    ): ILivePlaybackPolicy {

        // 初期化開始時の固定値が渡されていない呼び出しでは、現在の視聴者設定から実効値を判定する。
        const resolved_live_low_latency_settings = live_low_latency_settings ??
            this.getLiveLowLatencySettingsForCurrentContext();

        const channels_store = useChannelsStore();
        const server_settings_store = useServerSettingsStore();
        const force_stable = this.playback_mode !== 'Live' || (
            channels_store.channel.current.display_channel_id.startsWith('bs4k') &&
            server_settings_store.server_settings.general.bs4k_ignore_viewer_low_latency === true
        );
        const requested_low_latency = this.quality_profile_type === 'Cellular' ?
            resolved_live_low_latency_settings[1] : resolved_live_low_latency_settings[0];
        return createLivePlaybackPolicy({
            requested_low_latency: this.playback_mode === 'Live' && requested_low_latency,
            force_stable,
            is_safari: Utils.isSafari(),
        });
    }


    /**
     * mpegts.js の liveSync 関連 config を、destroy せずに更新する。
     */
    private applyMpegtsLiveSyncConfig(
        live_playback_policy: ILivePlaybackPolicy,
        on_active_applied: (video: HTMLVideoElement) => void,
    ): void {
        if (this.player === null) return;
        const mpegts_config = {
            liveSync: live_playback_policy.live_sync_enabled,
            liveSyncMaxLatency: live_playback_policy.max_latency_seconds ??
                live_playback_policy.target_buffer_seconds,
            liveSyncTargetLatency: live_playback_policy.target_buffer_seconds,
            liveSyncPlaybackRate: live_playback_policy.catch_up_rate,
        };
        // 次の画質 soft 切替で新しい mpegts が作られても同じ値を使う
        const plugin_options = this.player.options.pluginOptions as {
            mpegts?: {config?: Record<string, unknown>};
        } | undefined;
        if (plugin_options?.mpegts?.config !== undefined) {
            Object.assign(plugin_options.mpegts.config, mpegts_config);
        }
        // Prepare/Verify中のBを先にAbort/rollbackし、その後のActiveへ公開APIで反映する。
        if (this.konomitv_bs4k_live_quality_switch_coordinator !== null) {
            this.konomitv_bs4k_live_quality_switch_coordinator.applySoftLiveSync(
                mpegts_config,
                (pipeline) => on_active_applied(pipeline.video),
            );
            return;
        }
        const mpegts_player = this.player.plugins.mpegts as unknown as {
            configureLiveSync?: (config: typeof mpegts_config) => void;
        } | undefined;
        if (typeof mpegts_player?.configureLiveSync === 'function') {
            mpegts_player.configureLiveSync(mpegts_config);
        }
        on_active_applied(this.player.video);
    }


    /**
     * 低遅延 ON/OFF をプレイヤー再起動なしで切り替える。
     * 成功したら true。BS4K 強制安定化や破棄中は false。
     */
    private applySoftLiveLowLatencyMode(
        requested_low_latency: boolean,
        notice_message?: string,
    ): boolean {
        if (
            this.playback_mode !== 'Live' ||
            this.destroyed === true ||
            this.destroying === true ||
            this.isOwnerAborted() === true
        ) {
            return false;
        }
        const channels_store = useChannelsStore();
        const server_settings_store = useServerSettingsStore();
        const force_stable =
            channels_store.channel.current.display_channel_id.startsWith('bs4k') &&
            server_settings_store.server_settings.general.bs4k_ignore_viewer_low_latency === true;
        if (force_stable === true && requested_low_latency === true) {
            return false;
        }

        const policy = this.session_live_playback_policy ??
            this.resolveLivePlaybackPolicy();
        const previous_enabled = policy.live_sync_enabled;
        reconfigureLivePlaybackPolicy(policy, {
            requested_low_latency: requested_low_latency === true,
            force_stable,
            is_safari: Utils.isSafari(),
        });
        this.session_live_playback_policy = policy;
        if (policy.live_sync_enabled === previous_enabled) {
            // 実効値が変わらない（例: 強制安定化）場合は何もしない
            return force_stable === false;
        }

        // player 未初期化でもポリシーは更新する（ウォッチャー単体試験・設定同期用）
        if (this.player !== null) {
            this.applyMpegtsLiveSyncConfig(policy, (video) => {
                if (
                    this.player === null ||
                    this.destroyed === true ||
                    this.destroying === true
                ) {
                    return;
                }
                if (policy.live_sync_enabled === true && video.buffered.length > 0) {
                    const range_index = video.buffered.length - 1;
                    const target = resolveInitialLivePlaybackPositionSeconds(
                        policy,
                        video.buffered.start(range_index),
                        video.buffered.end(range_index),
                        video.currentTime,
                    );
                    if (target !== null) {
                        video.currentTime = target;
                    }
                } else if (
                    policy.live_sync_enabled === false &&
                    video.playbackRate !== 0 &&
                    video.playbackRate !== 1
                ) {
                    // 低遅延 OFF: 1.1 倍速追従を止め、バッファを自然に厚くする
                    video.playbackRate = 1;
                }
                if (notice_message !== undefined && notice_message !== '') {
                    this.player.notice(notice_message, 2000, undefined, undefined);
                }
            });
        }
        return true;
    }


    /**
     * ライブ視聴中に実効低遅延モードが変わった場合、destroy なしで soft 切替する
     *
     * Wi-Fi / Cellular の非アクティブ側や、BS4K上書きで打ち消される変更では、
     * 実効 live_sync_enabled が変わらないため何もしない。
     */
    private setupLivePlaybackPolicyWatcher(
        live_playback_policy: ILivePlaybackPolicy,
        initial_live_low_latency_settings: LiveLowLatencySettings,
    ): void {

        // 録画再生にはテレビ向け低遅延設定が関係しないため、ウォッチャーを登録しない。
        if (this.playback_mode !== 'Live') return;

        // 防御的に旧ウォッチャーを解除し、同じ PlayerController の再初期化でも必ず1つだけ登録する。
        if (this.live_playback_policy_watcher_cancel !== null) {
            this.live_playback_policy_watcher_cancel();
            this.live_playback_policy_watcher_cancel = null;
        }

        this.session_live_playback_policy = live_playback_policy;
        const settings_store = useSettingsStore();
        const apply_if_changed = (live_sync_enabled: boolean): void => {
            if (
                live_sync_enabled === live_playback_policy.live_sync_enabled ||
                this.destroyed === true ||
                this.destroying === true ||
                this.isOwnerAborted() === true
            ) {
                return;
            }
            this.applySoftLiveLowLatencyMode(
                live_sync_enabled,
                live_sync_enabled === true ?
                    '低遅延モードをオンにしました。' :
                    '低遅延モードをオフにしました。',
            );
        };

        // watch source は通常 / BS4K それぞれの Wi-Fi / Cellular 視聴者設定だけに限定する。
        // チャンネル切り替えや BS4K 上書き設定は追跡せず、旧 controller が新チャンネルの状態を見て復活する競合を防ぐ。
        this.live_playback_policy_watcher_cancel = watch(
            () => [
                settings_store.settings.tv_low_latency_mode,
                settings_store.settings.tv_low_latency_mode_cellular,
                settings_store.settings.tv_low_latency_mode_for_bs4k,
                settings_store.settings.tv_low_latency_mode_for_bs4k_cellular,
            ] as const,
            () => {
                // callback 内の参照は watch source の依存にならないため、現在のチャンネル状態を実効値の判定だけに利用できる。
                // 非アクティブな回線設定や BS4K 上書きで打ち消される変更は、開始時の実効値との比較で除外する。
                apply_if_changed(this.resolveLivePlaybackPolicy().live_sync_enabled);
            },
        );

        const current_live_low_latency_settings =
            this.getLiveLowLatencySettingsForCurrentContext();
        // 非同期初期化中に視聴者設定が変わり、登録時点ですでに異なる場合だけ実効値を再判定する。
        // チャンネル・BS4K状態だけが変わった場合は設定タプルが同じなので、旧 controller から再適用しない。
        if (
            current_live_low_latency_settings[0] !== initial_live_low_latency_settings[0] ||
            current_live_low_latency_settings[1] !== initial_live_low_latency_settings[1]
        ) {
            apply_if_changed(this.resolveLivePlaybackPolicy().live_sync_enabled);
        }
    }

    /**
     * DPlayer と PlayerManager を初期化し、再生準備を行う
     */
    public init(options: {
        default_quality: string | null;
        playback_rate: number | null;
        seek_seconds: number | null;
    } = {
        default_quality: null,
        playback_rate: null,
        seek_seconds: null,
    }): Promise<void> {

        // 同じ controller に対する重複初期化は、進行中の同じ Promise へ合流させる
        if (this.init_promise !== null) {
            return this.init_promise;
        }
        if (this.konomitv_bs4k_live_cleanup_blocked === true) {
            return Promise.reject(new Error(
                '前のライブ再生セッションのクリーンアップを確認できないため、再初期化を停止しました。',
            ));
        }
        if (this.owner_signal?.aborted === true) {
            return Promise.resolve();
        }

        // 再初期化時は旧世代の timer / loop を先に中断する
        this.lifecycle_abort_controller.abort();
        this.lifecycle_abort_controller = new AbortController();
        const lifecycle_generation = ++this.lifecycle_generation;
        // 低遅延 ON / OFF と関連値は、この初期化世代の開始時に解決する。
        // 以降は同じオブジェクト参照を各再生経路へ渡し、ソフト切替時は in-place 更新する。
        // 通常局と BS4K で別キーを読むが、解決後のタプルは同じ形状にする。
        const live_low_latency_settings = this.getLiveLowLatencySettingsForCurrentContext();
        const live_playback_policy = this.resolveLivePlaybackPolicy(live_low_latency_settings);
        this.session_live_playback_policy = live_playback_policy;
        const init_promise = this.initInternal(
            options,
            lifecycle_generation,
            live_playback_policy,
            live_low_latency_settings,
        ).finally(() => {
            if (this.init_promise === init_promise) {
                this.init_promise = null;
            }
        });
        this.init_promise = init_promise;
        return init_promise;
    }

    /**
     * DPlayer と PlayerManager の実際の初期化処理
     */
    private async initInternal(
        options: {
            default_quality: string | null;
            playback_rate: number | null;
            seek_seconds: number | null;
        },
        lifecycle_generation: number,
        live_playback_policy: Readonly<ILivePlaybackPolicy>,
        initial_live_low_latency_settings: LiveLowLatencySettings,
    ): Promise<void> {
        const channels_store = useChannelsStore();
        const player_store = usePlayerStore();
        const server_settings_store = useServerSettingsStore();
        const settings_store = useSettingsStore();
        console.log('\u001b[31m[PlayerController] Initializing...');

        // 破棄済みかどうかのフラグを下ろす
        this.destroyed = false;
        this.destroying = false;
        if (this.isLifecycleCurrent(lifecycle_generation) === false) return;
        this.is_live_startup_temporary_muted = false;
        this.recorded_playback_ended = false;
        this.recorded_playback_end_blocked_by_seek = false;
        this.recorded_auto_skip_cm_target = null;
        this.konomitv_bs4k_unsupported_quality_restart_pending = false;

        // PlayerStore にプレイヤーを初期化したことを通知する
        // 実際にはこの時点ではプレイヤーの初期化は完了していないが、PlayerController.init() を実行したことが通知されることが重要
        // ライブ視聴かつザッピングを経てチャンネルが確定した場合、破棄を遅らせていた以前の PlayerController に紐づく
        // KeyboardShortcutManager がこのタイミングで破棄される
        player_store.is_player_initialized = true;

        // 通常 / BS4K と回線プロファイルごとの共通設定から映像・音声コーデックを決める。
        // サーバーの live / recorded 両方の能力とブラウザ MSE の積集合だけを採用し、
        // 非対応時は保存設定を書き換えず、この再生だけ互換コーデックへ戻す。
        const is_konomitv_bs4k_stream = this.playback_mode === 'Live' ?
            channels_store.channel.current.display_channel_id.startsWith('bs4k') :
            player_store.recorded_program.network_id === 0x000B;
        const konomitv_bs4k_requested_video_codec = is_konomitv_bs4k_stream ?
            this.quality_profile.konomitv_bs4k_playback_video_codec_for_bs4k :
            this.quality_profile.konomitv_bs4k_playback_video_codec;
        const konomitv_bs4k_requested_audio_codec = is_konomitv_bs4k_stream ?
            this.quality_profile.konomitv_bs4k_playback_audio_codec_for_bs4k :
            this.quality_profile.konomitv_bs4k_playback_audio_codec;
        const konomitv_bs4k_playback_video_profile =
            this.resolveKonomiTVBS4KEffectivePlaybackVideoProfile(
                is_konomitv_bs4k_stream,
                options.default_quality,
            );

        // ライブ側でも設定中の実エンコーダーを使って能力行を選ぶ必要があるため、
        // 設定ストアが未 hydrate ならここで取得する。
        if (server_settings_store.is_loaded === false) {
            const loaded_settings = await server_settings_store.fetchServerSettingsOnce();
            if (this.isLifecycleCurrent(lifecycle_generation) === false) return;
            if (loaded_settings === null) {
                throw new Error('Server settings must be loaded before playback initialization.');
            }
        }
        const konomitv_bs4k_playback_encoder: IKonomiTVBS4KPlaybackEncoder = is_konomitv_bs4k_stream === true ?
            server_settings_store.server_settings.general.encoder_bs4k :
            server_settings_store.server_settings.general.encoder;

        // ライブ・ラジオと音声のみ録画では映像 SourceBuffer を作らないため、
        // targeted能力APIにも映像backendをprobeする必要がないことを明示する。
        this.konomitv_bs4k_current_playback_has_video =
            this.doesKonomiTVBS4KCurrentPlaybackHaveVideo();
        const konomitv_bs4k_targeted_video_codec: KonomiTVBS4KPlaybackVideoCodec =
            this.konomitv_bs4k_compatibility_playback_fallback_attempted === true ?
                'avc' : konomitv_bs4k_requested_video_codec;
        const konomitv_bs4k_targeted_audio_codec: KonomiTVBS4KPlaybackAudioCodec = (
            this.konomitv_bs4k_compatibility_playback_fallback_attempted === true ||
            (
                this.playback_mode === 'Video' &&
                this.konomitv_bs4k_force_recorded_aac_audio_codec === true
            )
        ) ? 'aac' : konomitv_bs4k_requested_audio_codec;
        // 自動モード: full 行列で AV1→VP9→HEVC→AVC を段階的に試す（targeted は要求+AVC だけなので直行してしまう）。
        // 手動モード: targeted で exact 解決を高速化。full は設定パネル用。
        const konomitv_bs4k_use_auto_quality = this.isKonomiTVBS4KAutoQualityModeEnabled() === true &&
            this.konomitv_bs4k_compatibility_playback_fallback_attempted === false;
        const [
            konomitv_bs4k_playback_capabilities_targeted,
            konomitv_bs4k_playback_capabilities_for_ui,
        ] = await Promise.all([
            konomitv_bs4k_use_auto_quality === true ?
                Promise.resolve(null) :
                Videos.fetchKonomiTVBS4KTargetedPlaybackCapabilities(
                    konomitv_bs4k_playback_encoder,
                    konomitv_bs4k_targeted_video_codec,
                    konomitv_bs4k_targeted_audio_codec,
                    konomitv_bs4k_playback_video_profile,
                    this.konomitv_bs4k_current_playback_has_video,
                ),
            Videos.fetchKonomiTVBS4KPlaybackCapabilities(),
        ]);
        if (this.isLifecycleCurrent(lifecycle_generation) === false) return;
        this.konomitv_bs4k_playback_encoder_for_current_playback = konomitv_bs4k_playback_encoder;
        // full が空のときだけ targeted（または空）を流用する。
        const konomitv_bs4k_full_or_targeted =
            konomitv_bs4k_playback_capabilities_for_ui.live_combinations.length > 0 ||
            konomitv_bs4k_playback_capabilities_for_ui.audio.length > 0 ?
                konomitv_bs4k_playback_capabilities_for_ui :
                (konomitv_bs4k_playback_capabilities_targeted ?? {
                    video: [],
                    audio: [],
                    live_combinations: [],
                });
        this.konomitv_bs4k_playback_capabilities_for_current_playback =
            konomitv_bs4k_use_auto_quality === true ?
                konomitv_bs4k_full_or_targeted :
                (konomitv_bs4k_playback_capabilities_targeted ?? konomitv_bs4k_full_or_targeted);
        this.konomitv_bs4k_playback_capabilities_for_ui = konomitv_bs4k_full_or_targeted;
        this.konomitv_bs4k_playback_video_profile_for_current_playback = konomitv_bs4k_playback_video_profile;
        // 自動モード: 初期解決した画質をセッションへ固定し、Network API の揺らぎで再解決されないようにする。
        // 視聴中の段階下げは requestAutoQualityStepDown が同じ override を更新する。
        if (
            this.isKonomiTVBS4KAutoQualityModeEnabled() === true &&
            this.konomitv_bs4k_auto_session_override.streaming_quality === undefined
        ) {
            this.konomitv_bs4k_auto_session_override = {
                ...this.konomitv_bs4k_auto_session_override,
                streaming_quality: konomitv_bs4k_playback_video_profile.streaming_quality,
            };
        }

        // ラジオと音声のみ録画は映像能力がなくても、既定値を query に保持して音声だけを解決する。
        const konomitv_bs4k_playback_codecs = this.resolveKonomiTVBS4KPlaybackCodecsForCurrentMedia(
            this.konomitv_bs4k_playback_capabilities_for_current_playback,
            konomitv_bs4k_playback_encoder,
            konomitv_bs4k_requested_video_codec,
            konomitv_bs4k_requested_audio_codec,
            konomitv_bs4k_playback_video_profile,
        );
        const konomitv_bs4k_playback_video_codec = konomitv_bs4k_playback_codecs.video_codec;
        const konomitv_bs4k_playback_video_bit_depth = konomitv_bs4k_playback_codecs.video_bit_depth;
        const konomitv_bs4k_playback_audio_codec = konomitv_bs4k_playback_codecs.audio_codec;
        this.konomitv_bs4k_playback_video_codec_for_current_playback = konomitv_bs4k_playback_video_codec;
        this.konomitv_bs4k_playback_video_bit_depth_for_current_playback =
            konomitv_bs4k_playback_video_bit_depth;
        this.konomitv_bs4k_playback_audio_codec_for_current_playback = konomitv_bs4k_playback_audio_codec;
        const is_konomitv_bs4k_hevc_playback = konomitv_bs4k_playback_video_codec === 'hevc';
        const is_konomitv_bs4k_hevc_10bit_playback =
            is_konomitv_bs4k_hevc_playback === true && konomitv_bs4k_playback_video_bit_depth === 10;

        // ブラウザが MSE in Worker での H.265 / HEVC 再生に対応しているかどうか
        const is_hevc_video_supported_in_worker = await mpegts.supportWorkerForMSEH265Playback();
        if (this.isLifecycleCurrent(lifecycle_generation) === false) return;

        // ライブの MPEG-TS / events / PSI archived data は、この文字列と同じ codec query で
        // 同一の LiveStream 共有キーへ接続する。
        const konomitv_bs4k_live_playback_codec_query = PlayerUtils.buildKonomiTVBS4KLivePlaybackCodecQuery({
            video_codec: konomitv_bs4k_playback_video_codec,
            video_bit_depth: konomitv_bs4k_playback_video_bit_depth,
            audio_codec: konomitv_bs4k_playback_audio_codec,
        });

        const is_konomitv_bs4k_live_playback = (
            this.playback_mode === 'Live' &&
            channels_store.channel.current.display_channel_id.startsWith('bs4k')
        );
        const is_oneseg_playback = this.playback_mode === 'Live' ?
            channels_store.channel.current.is_oneseg === true :
            player_store.recorded_program.channel?.is_oneseg === true;
        this.aribb24_profile = is_oneseg_playback ? 'C' : 'A';

        // 文字スーパーの表示設定
        // ライブ視聴とビデオ視聴で設定キーが異なる
        const is_show_superimpose = this.playback_mode === 'Live' ?
            settings_store.settings.tv_show_superimpose : settings_store.settings.video_show_superimpose;

        // シーク秒数が指定されていない（初回ロード時）は、視聴履歴があればその位置から再生を開始する
        // なければ録画開始マージン + 2秒シークする
        // 2秒プラスしているのは、実際の放送波では EPG (EIT[p/f]) の変更より2〜4秒後に実際に番組が切り替わる場合が多いため
        // この誤差は放送局や TOT 精度によっておそらく異なるので、本編の最初が削れないように2秒のプラスに留めている
        // seek_seconds はこの後 DPlayer を初期化した後の初回シーク時に参照される
        let seek_seconds = options.seek_seconds;
        let is_initial_video_playback_without_history = false;
        if (seek_seconds === null) {
            if (this.playback_mode === 'Video') {
                const history = settings_store.settings.watched_history.find(
                    history => history.video_id === player_store.recorded_program.id
                );
                if (history) {
                    seek_seconds = history.last_playback_position;
                    console.log(`\u001b[31m[PlayerController] Seeking to ${seek_seconds} seconds. (Watched History)`);
                } else {
                    seek_seconds = player_store.recorded_program.recording_start_margin + 2;
                    is_initial_video_playback_without_history = true;
                    console.log(`\u001b[31m[PlayerController] Seeking to ${seek_seconds} seconds. (Recording Start Margin + 2)`);
                }
            } else {
                // ライブ再生時は使わない値だが、型エラー回避のために 0 を設定
                seek_seconds = 0;
            }
        }

        // 視聴履歴のない新規再生だけは、初期位置が CM 内なら HLS のロード開始前に CM 終了位置へ補正する
        // 視聴履歴・画質切り替え・プレイヤー再起動からの位置復元は、ユーザーが見ていた位置を優先して補正しない
        if (
            this.playback_mode === 'Video' &&
            is_initial_video_playback_without_history === true &&
            settings_store.settings.video_auto_skip_cm === true &&
            seek_seconds !== null
        ) {
            const cm_skip_target = RecordedCMSkipManager.getInitialSkipTarget(
                seek_seconds,
                player_store.recorded_program.recorded_video.cm_sections,
                player_store.recorded_program.recorded_video.duration,
            );
            if (cm_skip_target !== null) {
                seek_seconds = cm_skip_target;
                console.log(`\u001b[31m[PlayerController] Seeking to ${seek_seconds} seconds. (Initial CM Auto Skip)`);
            }
        }

        // この時点で LocalStorage に dplayer-danmaku-opacity キーが存在しなければ、コメントの透明度の既定値を設定する
        // DPlayer のデフォルトは 1.0 (全表示) だが映像が見づらくなるため、0.5 に設定する
        if (localStorage.getItem('dplayer-danmaku-opacity') === null) {
            localStorage.setItem('dplayer-danmaku-opacity', '0.5');
        }

        // CM 区間からハイライトマーカーを作成する
        // TODO: DPlayer のマーカー機能はまともに実装されていないため、将来的にはレコーダーのように CM 区間のシークバーを
        // 暗くし、現在の CM 開始位置マーカーよりも区間全体を把握しやすくしたい
        const highlights: Array<{text: string, time: number}> = [];
        if (this.playback_mode === 'Video' && player_store.recorded_program?.recorded_video?.cm_sections) {
            const cm_sections = player_store.recorded_program.recorded_video.cm_sections;
            const videoDuration = player_store.recorded_program.recorded_video.duration;
            const endThreshold = videoDuration - 2;

            for (const section of cm_sections) {
                // CM 開始位置に「CM」マーカーを追加（動画終了2秒以内は除外）
                if (section.start_time <= endThreshold) {
                    highlights.push({
                        text: 'CM',
                        time: section.start_time
                    });
                }

                // CM 終了位置に「本編」マーカーを追加（動画終了2秒以内は除外）
                if (section.end_time <= endThreshold) {
                    highlights.push({
                        text: '本編',
                        time: section.end_time
                    });
                }
            }
            console.log('\u001b[31m[PlayerController] Added CM section markers:', highlights);
        }

        // mpegts.js と hls.js を window 直下に入れる
        // こうしないと DPlayer が mpegts.js / hls.js を認識できない
        (window as any).mpegts = mpegts;
        (window as any).Hls = Hls;

        // 初期 MSE 判定と DPlayer の defaultQuality は同じ実効 API 画質を使う。
        // API URL 用 suffix の付与もここへ集約し、ライブ・録画の画質一覧で別々に正規化しない。
        const is_konomitv_bs4k_recorded_video = (
            this.playback_mode === 'Video' &&
            player_store.recorded_program.network_id === 0x000B
        );
        const konomitv_bs4k_playback_selectable_qualities =
            this.getKonomiTVBS4KPlaybackSelectableQualities(is_konomitv_bs4k_stream);
        const konomitv_bs4k_default_quality_display_name =
            PlayerUtils.getKonomiTVBS4KPlaybackQualityDisplayName(
                konomitv_bs4k_playback_video_profile.streaming_quality,
            );
        const konomitv_bs4k_hevc_suffix =
            is_konomitv_bs4k_hevc_playback === true && this.playback_mode === 'Live' ? '-hevc' : '';
        const buildKonomiTVBS4KPlaybackAPIQuality = (
            konomitv_bs4k_quality_name: KonomiTVBS4KPlaybackSelectableQuality,
        ): string => {
            let konomitv_bs4k_api_quality =
                `${konomitv_bs4k_quality_name}${konomitv_bs4k_hevc_suffix}`;
            if (is_konomitv_bs4k_hevc_10bit_playback === true) {
                konomitv_bs4k_api_quality += '-10bit';
            }
            if (
                is_konomitv_bs4k_live_playback === false &&
                is_konomitv_bs4k_recorded_video === false &&
                konomitv_bs4k_quality_name !== '1080p-60fps' &&
                (
                    is_konomitv_bs4k_stream ?
                        this.quality_profile.konomitv_bs4k_playback_24fps_mode_for_bs4k :
                        this.quality_profile.konomitv_bs4k_playback_24fps_mode
                ) === true
            ) {
                konomitv_bs4k_api_quality += '-24fps';
            }
            return konomitv_bs4k_api_quality;
        };
        // DPlayerには初期MPEG-TSを起動させず、Startup Probeだけが実接続を所有する。
        const konomitv_bs4k_live_pipeline_specs: IKonomiTVBS4KLivePipelineSpec[] = [];

        // DPlayer を初期化
        this.player = new DPlayer({
            // DPlayer を配置する要素
            container: document.querySelector<HTMLDivElement>('.watch-player__dplayer')!,
            // テーマカラー
            theme: 'rgb(var(--v-theme-primary))',
            // 言語 (日本語固定)
            lang: 'ja-jp',
            // ライブモード (ビデオ視聴では無効)
            live: this.playback_mode === 'Live' ? true : false,
            // ライブモードで同期する際の最小バッファサイズ
            liveSyncMinBufferSize: live_playback_policy.target_buffer_seconds - 0.1,
            // ループ再生 (既定では無効。DPlayer でユーザーが明示的に有効化した保存値は尊重する)
            loop: false,
            // 自動再生
            autoplay: (
                this.playback_mode !== 'Live' ||
                channels_store.channel.current.is_radiochannel === true
            ),
            // AirPlay 機能 (うまく動かないため無効化)
            airplay: false,
            // ショートカットキー（こちらで制御するため無効化）
            hotkey: false,
            // スクリーンショット (こちらで制御するため無効化)
            screenshot: false,
            // CORS を有効化
            crossOrigin: 'anonymous',
            // 音量の初期値
            volume: 1.0,
            // 再生速度の設定 (x1.1 を追加)
            playbackSpeed: [0.25, 0.5, 0.75, 1, 1.1, 1.25, 1.5, 1.75, 2],
            // シークバー上のハイライトマーカー（CM区間など）
            highlight: highlights.length > 0 ? highlights : undefined,

            // 動画の設定
            video: (() => {
                // 画質リスト
                const qualities: DPlayerType.VideoQuality[] = [];

                // ライブ視聴: チャンネル情報がセットされているはず
                if (this.playback_mode === 'Live') {
                    const append_live_quality = (
                        name: string,
                        api_quality: string,
                    ): void => {
                        const quality_index = qualities.length;
                        const stream_url = PlayerUtils.buildKonomiTVBS4KLiveAPIEndpointURL(
                            channels_store.channel.current.display_channel_id,
                            api_quality,
                            'mpegts',
                            konomitv_bs4k_live_playback_codec_query,
                        );
                        const events_url = PlayerUtils.buildKonomiTVBS4KLiveAPIEndpointURL(
                            channels_store.channel.current.display_channel_id,
                            api_quality,
                            'events',
                            konomitv_bs4k_live_playback_codec_query,
                        );
                        qualities.push({
                            name,
                            type: 'konomitv-bs4k-live-probe',
                            // 実URLはDPlayer constructor後に戻す。ここで渡すとcustomType実行前に
                            // browser native requestが始まり、Startup候補と二重起動し得る。
                            url: 'data:video/mp2t;base64,',
                        });
                        konomitv_bs4k_live_pipeline_specs.push({
                            quality_index,
                            quality_name: name,
                            api_quality,
                            codec_query: konomitv_bs4k_live_playback_codec_query,
                            stream_url,
                            events_url,
                            has_video: channels_store.channel.current.is_radiochannel === false,
                            has_audio: true,
                        });
                    };
                    // ラジオチャンネルの場合
                    // API が受け付ける画質の値は通常のチャンネルと同じだが (手抜き…)、実際の画質は 48KHz/192kbps で固定される
                    // ラジオチャンネルの場合は、1080p と渡しても 48kHz/192kbps 固定の音声だけの MPEG-TS が配信される
                    if (channels_store.channel.current.is_radiochannel === true) {
                        qualities.push({
                            name: '48kHz/192kbps',
                            type: 'mpegts',
                            url: PlayerUtils.buildKonomiTVBS4KLiveAPIEndpointURL(
                                channels_store.channel.current.display_channel_id,
                                '1080p',
                                'mpegts',
                                konomitv_bs4k_live_playback_codec_query,
                            ),
                        });
                    // 通常のチャンネルの場合
                    } else {
                        // 画質リストを作成
                        for (
                            const konomitv_bs4k_quality_name of
                            konomitv_bs4k_playback_selectable_qualities
                        ) {
                            append_live_quality(
                                PlayerUtils.getKonomiTVBS4KPlaybackQualityDisplayName(
                                    konomitv_bs4k_quality_name,
                                ),
                                buildKonomiTVBS4KPlaybackAPIQuality(konomitv_bs4k_quality_name),
                            );
                        }
                    }
                    // デフォルトの画質は、能力判定前に options.default_quality まで反映した実効値を使う。
                    let konomitv_bs4k_default_quality = konomitv_bs4k_default_quality_display_name;
                    // ラジオチャンネルのみ常に 48KHz/192kbps に固定する
                    if (channels_store.channel.current.is_radiochannel) {
                        konomitv_bs4k_default_quality = '48kHz/192kbps';
                    }
                    return {
                        quality: qualities,
                        defaultQuality: konomitv_bs4k_default_quality,
                        customType: channels_store.channel.current.is_radiochannel === true ? undefined : {
                            'konomitv-bs4k-live-probe': (video: HTMLVideoElement): void => {
                                // Startup Probe開始まではsrcなし・非表示・muteでActiveは存在しない。
                                video.removeAttribute('src');
                                video.muted = true;
                                video.classList.remove('dplayer-video-current');
                                video.style.opacity = '0';
                                video.style.pointerEvents = 'none';
                                video.setAttribute('aria-hidden', 'true');
                            },
                        },
                    };

                // ビデオ視聴: 録画番組情報がセットされているはず
                } else {
                    // ビデオストリーミング API のベース URL
                    const streaming_api_base_url = `${Utils.api_base_url}/streams/video/${player_store.recorded_program.id}`;
                    // 画質リストを作成
                    const first_audio_track = player_store.recorded_program.recorded_video.audio_tracks[0];
                    const initial_audio_rendition = first_audio_track === undefined ? null :
                        `${first_audio_track.index}${first_audio_track.is_dual_mono === true ? '-main' : ''}`;
                    for (
                        const konomitv_bs4k_quality_name of
                        konomitv_bs4k_playback_selectable_qualities
                    ) {
                        // 画質ごとに異なるセッション ID を生成 (セッション ID は UUID の - で区切って一番左側のみを使う)
                        const session_id = crypto.randomUUID().split('-')[0];
                        // 画質設定を追加
                        qualities.push({
                            name: PlayerUtils.getKonomiTVBS4KPlaybackQualityDisplayName(
                                konomitv_bs4k_quality_name,
                            ),
                            type: 'hls',
                            url: `${streaming_api_base_url}/${buildKonomiTVBS4KPlaybackAPIQuality(konomitv_bs4k_quality_name)}/playlist?session_id=${session_id}` +
                                `&video_codec=${konomitv_bs4k_playback_video_codec}` +
                                `&video_bit_depth=${konomitv_bs4k_playback_video_bit_depth}` +
                                `&audio_codec=${konomitv_bs4k_playback_audio_codec}` +
                                (initial_audio_rendition !== null ? `&audio_track=${initial_audio_rendition}` : ''),
                        });
                    }
                    if (player_store.recorded_program.recorded_video.has_video === false) {
                        return {
                            quality: qualities,
                            defaultQuality: konomitv_bs4k_default_quality_display_name,
                        };
                    }
                    const tile_info = player_store.recorded_program.recorded_video.thumbnail_info?.tile ?? null;
                    return {
                        quality: qualities,
                        defaultQuality: konomitv_bs4k_default_quality_display_name,
                        thumbnails: tile_info !== null ? {
                            url: `${Utils.api_base_url}/videos/${player_store.recorded_program.id}/thumbnail/tiled`,
                            interval: tile_info.interval_sec,
                            width: tile_info.tile_width,
                            height: tile_info.tile_height,
                            columnCount: tile_info.column_count,
                        } : {
                            url: `${Utils.api_base_url}/videos/${player_store.recorded_program.id}/thumbnail/tiled`,
                            interval: (() => {
                                // 以下のロジックは server/app/metadata/ThumbnailGenerator.py の旧仕様と同一
                                // 録画番組の長さ (分単位で切り捨て)
                                const duration_min = Math.floor(player_store.recorded_program.recorded_video.duration / 60);
                                // 基準となる動画の長さ (30分)
                                const BASE_DURATION_MIN = 30;
                                // 基準となる間隔 (5秒)
                                const BASE_INTERVAL_SEC = 5.0;
                                // 最大間隔 (30秒)
                                const MAX_INTERVAL_SEC = 30.0;
                                // 30分以下は一律5秒間隔
                                if (duration_min <= BASE_DURATION_MIN) {
                                    return BASE_INTERVAL_SEC;
                                }
                                // 30分超の場合は対数関数的に増加を抑制
                                // duration_ratio = 2 (1時間) の時に、increase_ratio が約1.5になるように調整
                                const duration_ratio = duration_min / BASE_DURATION_MIN;
                                // log(1 + x) の代わりに log(1 + x/2) を使うことで、1時間の時に1.5倍程度になるよう調整
                                return Math.min(
                                    MAX_INTERVAL_SEC,
                                    BASE_INTERVAL_SEC * duration_ratio / Math.log2(1 + duration_ratio/2)
                                );
                            })(),
                            width: 480,  // サムネイルの幅
                            height: 270,  // サムネイルの高さ
                            columnCount: 34,  // サムネイルの列数
                        }
                    };
                }
            })(),

            // コメントの設定
            danmaku: {
                // コメントするユーザー名: 便宜上 KonomiTV に固定 (実際には利用されない)
                user: 'KonomiTV',
                // コメントの流れる速度
                speedRate: settings_store.settings.comment_speed_rate,
                // コメントのフォントサイズ
                fontSize: settings_store.settings.comment_font_size,
                // コメント送信後にコメントフォームを閉じるかどうか
                closeCommentFormAfterSend: settings_store.settings.close_comment_form_after_sending,
            },

            // コメント API バックエンドの設定
            apiBackend: {
                // コメント取得時
                read: async (options) => {
                    // 実況機能が無効な場合も DPlayer のコメント機能自体は空の状態で初期化する
                    // DPlayer 内部の多くが danmaku の存在を前提にしているため、機能ごと削除せず通信だけを止める
                    if (settings_store.is_jikkyo_enabled === false) {
                        options.success([]);
                        return;
                    }
                    if (this.playback_mode === 'Live') {
                        // ライブ視聴: 空の配列を返す
                        // ライブ視聴では LiveCommentManager 側でリアルタイムにコメントを受信して直接描画するため、ここでは一旦コメント0件として認識させる
                        options.success([]);
                    } else {
                        // ビデオ視聴: 過去ログコメントを取得して返す
                        const jikkyo_comments = await Videos.fetchVideoJikkyoComments(player_store.recorded_program.id);
                        if (jikkyo_comments.is_success === false) {
                            // 取得に失敗した場合はコメントリストにエラーメッセージを表示する
                            // ただし「この録画番組の過去ログコメントは存在しないか、現在取得中です。」の場合はエラー扱いしない
                            player_store.video_comment_init_failed_message = jikkyo_comments.detail;
                            if (jikkyo_comments.detail !== 'この録画番組の過去ログコメントは存在しないか、現在取得中です。') {
                                options.error(jikkyo_comments.detail);
                            } else {
                                options.success([]);
                            }
                        } else {
                            // 過去ログコメントを取得できているということは、recording_start_time は null ではないはず
                            const recording_start_time = player_store.recorded_program.recorded_video.recording_start_time!;
                            // コメントリストに取得した過去ログコメントを送る
                            // コメ番は重複している可能性がないとも言い切れないので、別途連番を振る
                            let count = 0;
                            player_store.event_emitter.emit('CommentReceived', {
                                is_initial_comments: true,
                                comments: jikkyo_comments.comments.map((comment) => ({
                                    id: count++,
                                    text: comment.text,
                                    time: Utils.apply28HourClock(dayjs(recording_start_time).add(comment.time, 'seconds').format('MM/DD HH:mm:ss')),
                                    playback_position: comment.time,
                                    user_id: comment.author,
                                    premium: null,
                                    my_post: false,
                                })),
                            });
                            options.success(jikkyo_comments.comments);
                        }
                        // コメント表示をシーク状態に同期する
                        // ここでシークしておかないと、DPlayer の初期化直後にシークした際にシーク位置より前のコメントが一斉に描画されてしまう
                        this.player!.danmaku!.seek();
                        // コメントリストもシークバーに合わせてスクロールさせておく（コメントリストコンポーネントに通知）
                        // この時点ではまだ映像の読み込みが完了していない可能性が高いので、currentTime がまだ 0 か非数の場合は seek_seconds をそのまま使う
                        let comment_seek_seconds = this.player!.video.currentTime;
                        if (comment_seek_seconds === 0 || isNaN(comment_seek_seconds)) {
                            comment_seek_seconds = seek_seconds;
                        }
                        await Utils.sleep(0.1);  // 仮想スクローラーの準備ができるまで少し待つ
                        player_store.event_emitter.emit('PlaybackPositionChanged', {
                            playback_position: comment_seek_seconds,
                        });
                        console.log(`\u001b[31m[PlayerController] Comment list seeking to ${comment_seek_seconds} seconds.`);
                    }
                },
                // コメント送信時
                send: async (options) => {
                    // 非表示のコメントフォームが何らかの理由で呼び出された場合も、実況機能が無効なら送信しない
                    if (settings_store.is_jikkyo_enabled === false) {
                        options.error('実況機能は無効です。');
                        return;
                    }
                    if (this.playback_mode === 'Live') {
                        // ライブ視聴: コメントを送信する
                        // PlayerManager に登録されているはずの LiveCommentManager を探し、コメントを送信する
                        for (const player_manager of this.player_managers) {
                            if (player_manager instanceof LiveCommentManager) {
                                player_manager.sendComment(options);  // options.success() は LiveCommentManager 側で呼ばれる
                                return;
                            }
                        }
                        // 実況機能が有効でも LiveCommentManager が未初期化なら、送信処理を完了待ちのままにしない
                        options.error('コメント送信機能を初期化できませんでした。');
                    } else {
                        // ビデオ視聴: 過去ログにはコメントできないのでエラーを返す
                        options.error('録画番組にはコメントできません。');
                    }
                },
            },

            // 字幕の設定
            subtitle: {
                type: 'aribb24',  // aribb24.js を有効化
            },

            // 再生プラグインの設定
            pluginOptions: {
                // mpegts.js
                mpegts: {
                    config: {
                        // Web Worker を有効にする
                        enableWorker: true,
                        // Media Source Extensions API 向けの Web Worker を有効にする
                        // メインスレッドから再生処理を分離することで、低スペック端末で DOM 描画の遅延が影響して映像再生が詰まる問題が解消される
                        // MSE in Worker が使えない環境では自動的に mpegts.js 側でフォールバックされるため、基本的に true を設定する
                        // ただし Windows 版 Microsoft Edge では MSE in Worker 有効時のみ H.265 / HEVC 再生が動作しないため、この場合のみ無効化する
                        enableWorkerForMSE: (
                            is_konomitv_bs4k_hevc_playback === true &&
                            is_hevc_video_supported_in_worker === false
                        ) ? false : true,
                        // 再生開始まで 2048KB のバッファを貯める (?)
                        // あまり大きくしすぎてもどうも効果がないようだが、小さくしたり無効化すると特に Safari で不安定になる
                        enableStashBuffer: true,
                        stashInitialSize: Math.floor(2048 * 1024),
                        // HTMLMediaElement の内部バッファによるライブストリームの遅延を追跡する
                        // liveBufferLatencyChasing と異なり、いきなり再生時間をスキップするのではなく、
                        // 再生速度を少しだけ上げることで再生を途切れさせることなく遅延を追跡する
                        liveSync: live_playback_policy.live_sync_enabled,
                        // 許容する HTMLMediaElement の内部バッファの最大値 (秒単位, 3秒)
                        // 低遅延 OFF では liveSync 自体が無効なため参照されないが、型互換のためターゲット値を渡す
                        liveSyncMaxLatency: live_playback_policy.max_latency_seconds ??
                            live_playback_policy.target_buffer_seconds,
                        // HTMLMediaElement の内部バッファ (遅延) が liveSyncMaxLatency を超えたとき、ターゲットとする遅延時間 (秒単位)
                        liveSyncTargetLatency: live_playback_policy.target_buffer_seconds,
                        // ライブストリームの遅延の追跡に利用する再生速度 (x1.1)
                        // 遅延が 3 秒を超えたとき、遅延が playback_buffer_sec を下回るまで再生速度が x1.1 に設定される
                        liveSyncPlaybackRate: live_playback_policy.catch_up_rate,
                    }
                },
                // hls.js
                hls: {
                    ...Hls.DefaultConfig,
                    // Web Worker を有効にする
                    enableWorker: true,
                    // ManagedMediaSource が使える Safari では常に ManagedMediaSource を利用する
                    // iPadOS Safari や macOS Safari では通常の MediaSource も使えるが、Safari のシェアは iOS ユーザーが圧倒的なので、
                    // 動作確認上のパターンを iOS に揃えた方がバグなどの把握がしやすくなると考えられることから、ManagedMediaSource に統一する
                    preferManagedMediaSource: true,
                    // startPosition に視聴履歴などから求めた再生位置を渡し、ロード開始時点で正しい Media Sequence を選択させる
                    // これを指定しないと manifest 解析後に sequence=0 からフラグメント取得が始まってしまう
                    startPosition: seek_seconds,
                    // カスタムバッファコントローラーを設定
                    // @ts-ignore
                    bufferController: CustomBufferController,
                    // シーク前に開始済みだった古いFragment要求が、新しいエンコードタスクを
                    // キャンセルしないよう、全セグメント要求へ現在のシーク世代番号を付与する。
                    fetchSetup: (context: { url: string }, initParams: RequestInit) => {
                        const request_url = new URL(context.url);
                        if (request_url.pathname.includes('/api/streams/video/') &&
                            request_url.pathname.endsWith('/segment')) {
                            request_url.searchParams.set(
                                'request_generation',
                                String(CustomBufferController.getRequestGeneration()),
                            );
                        }
                        return new Request(request_url, initParams);
                    },
                    // Chromium などで既定の XHRLoader が選ばれた場合も、fetchSetup と同じ世代番号を付ける。
                    xhrSetup: (xhr: XMLHttpRequest, url: string) => {
                        const request_url = new URL(url);
                        if (request_url.pathname.includes('/api/streams/video/') &&
                            request_url.pathname.endsWith('/segment')) {
                            request_url.searchParams.set(
                                'request_generation',
                                String(CustomBufferController.getRequestGeneration()),
                            );
                        }
                        xhr.open('GET', request_url, true);
                    },
                    // プレイリスト / セグメントのリクエスト時のタイムアウトを回避する
                    manifestLoadPolicy: {
                        default: {
                            maxTimeToFirstByteMs: 1000000,  // 適当に大きな値を設定
                            maxLoadTimeMs: 1000000,  // 適当に大きな値を設定
                            timeoutRetry: {
                                maxNumRetry: 2,
                                retryDelayMs: 0,
                                maxRetryDelayMs: 0,
                            },
                            errorRetry: {
                                maxNumRetry: 1,
                                retryDelayMs: 1000,
                                maxRetryDelayMs: 8000,
                            },
                        },
                    },
                    playlistLoadPolicy: {
                        default: {
                            maxTimeToFirstByteMs: 1000000,  // 適当に大きな値を設定
                            maxLoadTimeMs: 1000000,  // 適当に大きな値を設定
                            timeoutRetry: {
                                maxNumRetry: 2,
                                retryDelayMs: 0,
                                maxRetryDelayMs: 0,
                            },
                            errorRetry: {
                                maxNumRetry: 2,
                                retryDelayMs: 1000,
                                maxRetryDelayMs: 8000,
                            }
                        }
                    },
                    fragLoadPolicy: {
                        default: {
                            maxTimeToFirstByteMs: 1000000,  // 適当に大きな値を設定
                            maxLoadTimeMs: 1000000,  // 適当に大きな値を設定
                            timeoutRetry: {
                                maxNumRetry: 4,
                                retryDelayMs: 0,
                                maxRetryDelayMs: 0,
                            },
                            errorRetry: {
                                maxNumRetry: 6,
                                retryDelayMs: 1000,
                                maxRetryDelayMs: 8000,
                            }
                        }
                    }
                },
                // aribb24.js
                aribb24: {
                    // 文字スーパーレンダラーを無効にするかどうか
                    disableSuperimposeRenderer: is_show_superimpose === false,
                    // 描画フォント
                    normalFont: (() => {
                        let font = settings_store.settings.caption_font;
                        if (font === 'sans-serif') {
                            return 'sans-serif';
                        }
                        if (font === 'Yu Gothic') {
                            // 游ゴシックのみ、Windows と Mac で名前が異なる
                            font = 'Yu Gothic Medium","Yu Gothic","YuGothic';
                        }
                        return `"${font}", "Rounded M+ 1m for ARIB", sans-serif`;
                    })(),
                    // 縁取りする色
                    forceStrokeColor: settings_store.settings.always_border_caption_text,
                    // 背景色
                    forceBackgroundColor: (() => {
                        if (settings_store.settings.specify_caption_opacity === true) {
                            const opacity = settings_store.settings.caption_opacity;
                            return `rgba(0, 0, 0, ${opacity})`;
                        } else {
                            return undefined;
                        }
                    })(),
                    // DRCS 文字を対応する Unicode 文字に置換
                    drcsReplacement: true,
                    // 高解像度の字幕 Canvas を取得できるように
                    enableRawCanvas: true,
                    // 縁取りに strokeText API を利用
                    useStroke: true,
                    // Unicode 領域の代わりに私用面の領域を利用 (Windows TV 系フォントのみ)
                    usePUA: (() => {
                        const font = settings_store.settings.caption_font;
                        const context = document.createElement('canvas').getContext('2d')!;
                        context.font = '10px "Rounded M+ 1m for ARIB"';
                        context.fillText('Test', 0, 0);
                        context.font = `10px "${font}"`;
                        context.fillText('Test', 0, 0);
                        if (font.startsWith('Windows TV')) {
                            return true;
                        } else {
                            return false;
                        }
                    })(),
                    // 文字スーパーの PRA (内蔵音再生コマンド) のコールバックを指定
                    PRACallback: async (index: number, loop: boolean = false, offset: number = 0) => {
                        // index に応じた内蔵音を鳴らす
                        // ref: https://ics.media/entry/200427/
                        // ref: https://www.ipentec.com/document/javascript-web-audio-api-change-volume
                        // 自動再生ポリシーに引っかかったなどで AudioContext が一時停止されている場合、一度 resume() する必要がある
                        // resume() するまでに何らかのユーザーのジェスチャーが行われているはず…
                        // なくても動くこともあるみたいだけど、念のため
                        await this.romsounds_ready;
                        if (this.romsounds_context.state === 'suspended') {
                            await this.romsounds_context.resume();
                        }
                        // index で指定された音声データを読み込み
                        const buffer = this.romsounds_buffers.get(index);
                        if (buffer === undefined || this.romsounds_context.state === 'closed') return;
                        const buffer_source_node = this.romsounds_context.createBufferSource();
                        buffer_source_node.buffer = buffer;
                        buffer_source_node.loop = loop;
                        // GainNode につなげる
                        const gain_node = this.romsounds_context.createGain();
                        buffer_source_node.connect(gain_node);
                        // 出力につなげる
                        gain_node.connect(this.romsounds_context.destination);
                        // 音量を元の wav の3倍にする (1倍だと結構小さめ)
                        gain_node.gain.value = 3 * (
                            this.player?.video.muted === true ? 0 : this.player?.video.volume ?? 1
                        );
                        // 再生開始
                        const start_offset = loop && buffer.duration > 0 ? offset % buffer.duration : 0;
                        buffer_source_node.start(0, start_offset);
                        let is_stopped = false;
                        const cleanup = () => {
                            if (is_stopped) return;
                            is_stopped = true;
                            buffer_source_node.disconnect();
                            gain_node.disconnect();
                        };
                        buffer_source_node.addEventListener('ended', cleanup, {once: true});
                        return () => {
                            if (is_stopped) return;
                            try {
                                buffer_source_node.stop();
                            } catch (error) {
                                // 既に終了済みならcleanupだけ行う。
                            }
                            cleanup();
                        };
                    }
                }
            }
        });

        if (
            this.playback_mode === 'Live' &&
            isKonomiTVBS4KTwoPipelineLivePlaybackEligible(
                channels_store.channel.current.is_radiochannel === false,
            )
        ) {
            assert(this.player.qualityIndex !== null);
            // Startup Probe は既存 DPlayer video を一時ミュートで再利用する。
            // volumechange が後から配送されてもユーザーの保存値として記録しないよう、
            // Coordinator が muted を変更する前に一時ミュートの所有権を確立する。
            this.muteLiveStartupVideo(this.player.video);
            // ManagerがCommit後に参照するURLは保持しつつ、constructor時のnative requestだけを防いだ。
            for (const spec of konomitv_bs4k_live_pipeline_specs) {
                const quality = this.player.options.video.quality?.[spec.quality_index];
                if (quality !== undefined) quality.url = spec.stream_url;
            }
            this.konomitv_bs4k_live_quality_switch_coordinator =
                new KonomiTVBS4KLiveQualitySwitchCoordinator({
                    player: this.player,
                    display_channel_id: channels_store.channel.current.display_channel_id,
                    specs: konomitv_bs4k_live_pipeline_specs,
                    initial_quality_index: this.player.qualityIndex,
                    is_auto_mode: this.isKonomiTVBS4KAutoQualityModeEnabled(),
                    // Startup Hard Gate後も既存のsetupVideoPlaybackHandlerが背景解除・
                    // 保存mute判定・音量fadeを終えるまでは可聴化しない。
                    initial_muted: true,
                    get_mpegts_config: () => ({
                        ...(this.player?.options.pluginOptions.mpegts?.config ?? {}),
                    }),
                    // 低遅延OFFでは既存安定モードと同じ4秒前後をBのHard Gateにも要求する。
                    get_start_buffer_seconds: () => live_playback_policy.target_buffer_seconds,
                    on_lowest_startup_low_latency_fallback: () => {
                        this.konomitv_bs4k_auto_session_override = {
                            ...this.konomitv_bs4k_auto_session_override,
                            low_latency: false,
                        };
                        reconfigureLivePlaybackPolicy(live_playback_policy, {
                            requested_low_latency: false,
                            force_stable: false,
                            is_safari: Utils.isSafari(),
                        });
                        this.session_live_playback_policy = live_playback_policy;
                        const config = this.player?.options.pluginOptions.mpegts?.config;
                        if (config !== undefined) {
                            Object.assign(config, {
                                liveSync: live_playback_policy.live_sync_enabled,
                                liveSyncMaxLatency: live_playback_policy.max_latency_seconds ??
                                    live_playback_policy.target_buffer_seconds,
                                liveSyncTargetLatency: live_playback_policy.target_buffer_seconds,
                                liveSyncPlaybackRate: live_playback_policy.catch_up_rate,
                            });
                        }
                    },
                    on_commit_subtitle_generation: (pipeline, history) => {
                        this.commitKonomiTVBS4KLiveSubtitleGeneration(pipeline, history);
                    },
                    on_before_prepare_request: () => {
                        this.releaseKonomiTVBS4KLivePrepareConnectionSlot();
                    },
                    on_after_request_settled: () => {
                        this.restoreKonomiTVBS4KLivePrepareConnectionSlot();
                    },
                    on_before_server_commit: () => {
                        this.releaseKonomiTVBS4KLiveServerCommitConnectionSlot();
                    },
                    on_after_commit: (pipeline) => {
                        this.handleKonomiTVBS4KLivePipelineCommitted(pipeline);
                    },
                    on_restart_required: () => {
                        player_store.event_emitter.emit('PlayerRestartRequired', {
                            message: 'ライブ画質切り替えを継続できないため、プレイヤーを再起動しています…',
                        });
                    },
                    on_outcome: (outcome, detail) => {
                        console.info('[PlayerController] Live quality switch outcome.', {outcome, detail});
                    },
                    on_abr_frozen: (frozen) => {
                        this.setKonomiTVBS4KLiveABRFrozen(frozen);
                    },
                });
            if (
                await this.konomitv_bs4k_live_quality_switch_coordinator.start(
                    this.lifecycle_abort_controller.signal,
                ) === false
            ) {
                throw new Error('Startup Probe failed for every live quality.');
            }
            const startup_pipeline =
                this.konomitv_bs4k_live_quality_switch_coordinator.active;
            if (startup_pipeline !== null) {
                this.updateKonomiTVBS4KLivePipelineQualityState(startup_pipeline);
            }
        }

        // DPlayer が新しい video / MSE plugin を作る前に、切替先の実画質を現在の codec 条件で検査する。
        this.setupKonomiTVBS4KPlaybackQualitySwitchGuard(lifecycle_generation, this.player);

        if (this.playback_mode === 'Video' && player_store.recorded_program.recorded_video.has_video === false) {
            this.player.container.classList.add('dplayer-audio-only');
        }

        // DPlayer の配布バンドルにはビルド時点の aribb24.js が内包されており、KonomiTV 側で適用した
        // aribb24.js のパッチはそのままでは字幕レンダラーへ反映されない。
        // DPlayer 内蔵レンダラーを破棄し、Profile C と JIS X 0213:2004 に対応した外部レンダラーへ差し替える。
        this.replaceARIBB24Renderers();

        // ARIB-TTML はライブと録画のどちらも同じレンダラーへ timed-ID3 を投入する。
        // 字幕ボタンは字幕層だけを切り替え、緊急情報にも使われる文字スーパー層は既存設定に従って独立表示する。
        this.attachARIBTTMLRenderer();
        const initial_live_pipeline =
            this.konomitv_bs4k_live_quality_switch_coordinator?.active ?? null;
        if (this.playback_mode === 'Live' && initial_live_pipeline !== null) {
            this.attachLiveARIBTTMLStream();
            for (const entry of initial_live_pipeline.getTimelinedID3History()) {
                this.live_arib_ttml_handler(entry.metadata);
            }
        }
        this.player.on('subtitle_show', () => this.arib_ttml_renderer?.showCaption());
        this.player.on('subtitle_hide', () => this.arib_ttml_renderer?.hideCaption());

        // fMP4 経路ではARIB生字幕を映像から分離し、シーク復元点と後続12秒を先読みする。
        if (
            this.playback_mode === 'Video' &&
            player_store.recorded_program.recorded_video.playback_index_status === 'Ready'
        ) {
            const arib_track = player_store.recorded_program.recorded_video.subtitle_tracks.find((track) =>
                track.codec.toLowerCase().includes('arib') && track.codec.toLowerCase() !== 'arib_ttml',
            );
            if (arib_track !== undefined) {
                const requested_ranges = new Set<number>();
                let is_fetching = false;
                let restore_after_fetch = false;
                let subtitle_generation = 0;
                const fetch_arib_subtitle = async (restore: boolean = false): Promise<void> => {
                    if (this.player === null) return;
                    if (is_fetching === true) {
                        // 画質切り替えやシーク中なら、進行中の取得完了後に新しい再生位置から状態を復元する。
                        restore_after_fetch ||= restore;
                        return;
                    }
                    const range_start = Math.max(0, Math.floor(this.player.video.currentTime / 6) * 6);
                    if (restore === true) requested_ranges.clear();
                    if (requested_ranges.has(range_start)) return;
                    is_fetching = true;
                    const fetch_generation = subtitle_generation;
                    try {
                        const response = await APIClient.get<{
                            restore_packets: {pts: number; data: string}[];
                            packets: {pts: number; data: string}[];
                        }>(`/streams/video/${player_store.recorded_program.id}/subtitle/${arib_track.index}/arib`, {
                            params: {start_time: range_start, end_time: range_start + 12},
                        });
                        if (
                            response.type === 'success' &&
                            this.player !== null &&
                            fetch_generation === subtitle_generation
                        ) {
                            const decode = (data: string): Uint8Array => Uint8Array.from(atob(data), (character) =>
                                character.charCodeAt(0),
                            );
                            // 復元packetは録画先頭からの管理データ・DRCS・表示状態を含むため、
                            // 初回とseek時だけ投入する。通常先読みで再投入すると字幕状態が巻き戻る。
                            const packets = restore === true ?
                                [...response.data.restore_packets, ...response.data.packets] : response.data.packets;
                            const aribb24_plugins = this.player.plugins as unknown as {aribb24Caption?: CanvasRenderer};
                            for (const packet of packets) {
                                aribb24_plugins.aribb24Caption?.pushRawData(packet.pts, decode(packet.data));
                            }
                            requested_ranges.add(range_start);
                        }
                    } finally {
                        is_fetching = false;
                        if (restore_after_fetch === true) {
                            restore_after_fetch = false;
                            void fetch_arib_subtitle(true);
                        }
                    }
                };
                const restore_arib_subtitle = () => {
                    // 進行中の旧再生位置向け取得が完了しても、新しいレンダラーへ投入しない。
                    subtitle_generation += 1;
                    void fetch_arib_subtitle(true);
                };
                const restart_arib_subtitle = () => {
                    if (this.player === null) return;
                    this.recorded_arib_subtitle_cancel?.();
                    const video = this.player.video;
                    const timeupdate_handler = () => void fetch_arib_subtitle(false);
                    const seeking_handler = restore_arib_subtitle;
                    video.addEventListener('timeupdate', timeupdate_handler);
                    video.addEventListener('seeking', seeking_handler);
                    this.recorded_arib_subtitle_cancel = () => {
                        video.removeEventListener('timeupdate', timeupdate_handler);
                        video.removeEventListener('seeking', seeking_handler);
                    };
                    restore_arib_subtitle();
                };
                this.recorded_arib_subtitle_restart = restart_arib_subtitle;
                restart_arib_subtitle();
            }

            const has_arib_ttml_track = player_store.recorded_program.recorded_video.subtitle_tracks.some((track) =>
                track.codec.toLowerCase() === 'arib_ttml',
            );
            if (has_arib_ttml_track === true) {
                const requested_ranges = new Set<number>();
                let is_fetching = false;
                let restore_after_fetch = false;
                let subtitle_generation = 0;
                const fetch_arib_ttml = async (restore: boolean = false): Promise<void> => {
                    if (this.player === null || this.arib_ttml_renderer === null) return;
                    if (is_fetching === true) {
                        restore_after_fetch ||= restore;
                        return;
                    }
                    const range_start = Math.max(0, Math.floor(this.player.video.currentTime / 6) * 6);
                    if (restore === true) requested_ranges.clear();
                    if (requested_ranges.has(range_start)) return;
                    is_fetching = true;
                    const fetch_generation = subtitle_generation;
                    try {
                        const response = await APIClient.get<{
                            restore_packets: {
                                pts: number;
                                transport_timestamp: number;
                                component_tag: number;
                                data: string;
                                is_restore_point: boolean;
                            }[];
                            packets: {
                                pts: number;
                                transport_timestamp: number;
                                component_tag: number;
                                data: string;
                                is_restore_point: boolean;
                            }[];
                        }>(`/streams/video/${player_store.recorded_program.id}/subtitle/arib-ttml`, {
                            params: {start_time: range_start, end_time: range_start + 12},
                        });
                        if (
                            response.type === 'success' &&
                            this.player !== null &&
                            this.arib_ttml_renderer !== null &&
                            fetch_generation === subtitle_generation
                        ) {
                            const decode = (data: string): Uint8Array => Uint8Array.from(atob(data), (character) =>
                                character.charCodeAt(0),
                            );
                            if (restore === true) this.arib_ttml_renderer.reset();
                            const packets = restore === true ?
                                [...response.data.restore_packets, ...response.data.packets] : response.data.packets;
                            // restore履歴も通常rangeも同じ raw ID3 + 録画相対PTS を共通デコーダーへ順番どおり投入する。
                            // component_tagの字幕/文字スーパー振り分けはJS内部だけで行い、録画アダプターには持ち込まない。
                            for (const packet of packets) {
                                this.arib_ttml_renderer.pushID3v2Data(
                                    packet.pts,
                                    decode(packet.data),
                                    packet.transport_timestamp,
                                );
                            }
                            requested_ranges.add(range_start);
                        }
                    } finally {
                        is_fetching = false;
                        if (restore_after_fetch === true) {
                            restore_after_fetch = false;
                            void fetch_arib_ttml(true);
                        }
                    }
                };
                const restore_arib_ttml = () => {
                    subtitle_generation += 1;
                    void fetch_arib_ttml(true);
                };
                const restart_arib_ttml = () => {
                    if (this.player === null) return;
                    this.recorded_arib_ttml_cancel?.();
                    const video = this.player.video;
                    const timeupdate_handler = () => void fetch_arib_ttml(false);
                    const seeking_handler = restore_arib_ttml;
                    video.addEventListener('timeupdate', timeupdate_handler);
                    video.addEventListener('seeking', seeking_handler);
                    this.recorded_arib_ttml_cancel = () => {
                        video.removeEventListener('timeupdate', timeupdate_handler);
                        video.removeEventListener('seeking', seeking_handler);
                    };
                    restore_arib_ttml();
                };
                this.recorded_arib_ttml_restart = restart_arib_ttml;
                restart_arib_ttml();
            }
        }

        // デバッグ用にプレイヤーインスタンスも window 直下に入れる
        (window as any).player = this.player;

        if (
            this.konomitv_bs4k_compatibility_playback_fallback_attempted === false &&
            this.konomitv_bs4k_current_playback_has_video === true &&
            konomitv_bs4k_requested_video_codec !== konomitv_bs4k_playback_video_codec
        ) {
            this.player.notice(
                `${konomitv_bs4k_requested_video_codec.toUpperCase()} は共通再生条件を満たさないため、` +
                `今回の再生では ${konomitv_bs4k_playback_video_codec.toUpperCase()} を使用します。`,
            );
        }
        if (
            this.konomitv_bs4k_compatibility_playback_fallback_attempted === false &&
            konomitv_bs4k_requested_audio_codec !== konomitv_bs4k_playback_audio_codec
        ) {
            this.player.notice(
                `${konomitv_bs4k_requested_audio_codec.toUpperCase()} は共通再生条件を満たさないため、` +
                `今回の再生では ${konomitv_bs4k_playback_audio_codec.toUpperCase()} を使用します。`,
            );
        }
        // この時点で DPlayer のコンテナ要素に dplayer-mobile クラスが付与されている場合、
        // DPlayer は音量コントロールがないスマホ向けの UI になっている
        // 通常の UI で DPlayer の音量を 1.0 以外に設定した後スマホ向け UI になった場合、DPlayer の音量を変更できず OS の音量を上げるしかなくなる
        // そこで、スマホ向けの UI が表示されている場合のみ常に音量を 1.0 に設定する
        const is_dplayer_mobile = this.player.container.classList.contains('dplayer-mobile');
        if (is_dplayer_mobile === true) {
            // player.volume() を用いることで、単に音量を変更するだけでなく LocalStorage に音量を保存する処理も実行される
            // 第3引数を true に設定すると、通知を表示せずに音量を変更できる
            this.player.volume(1.0, undefined, true);
        }

        // PC 向け UI では、前回のミュート状態を戻す
        // DPlayer は音量だけを保存するため、ミュート状態だけ KonomiTV 側で補う
        // スマホ向け UI には音量ボタンがなく、ミュートで開始すると画面内で解除できない
        const is_saved_muted = is_dplayer_mobile === false && localStorage.getItem('dplayer-is-muted') === 'true';
        if (is_saved_muted === true) {
            this.player.muted(true);
        }

        // DPlayer 側で音量やミュート状態が変わったとき、次回起動時に使うミュート状態を保存する
        // スマホ向け UI ではミュートを解除する音量ボタンが表示されないため、PC 向け UI の状態だけを保存する
        this.player.on('volumechange', () => {
            if (this.player === null || this.player.container.classList.contains('dplayer-mobile') === true) return;
            if (this.is_live_startup_temporary_muted === true) return;
            localStorage.setItem('dplayer-is-muted', this.player.video.muted ? 'true' : 'false');
        });

        // DPlayer 側のコントロール UI 非表示タイマーを無効化（上書き）
        // 無効化しておかないと、PlayerController.setControlDisplayTimer() の処理と競合してしまう
        // 上書き元のコードは https://github.com/tsukumijima/DPlayer/blob/v1.30.2/src/ts/controller.ts#L397-L405 にある
        this.player.controller.setAutoHide = (time: number) => {};

        // DPlayer に動画再生系のイベントハンドラーを登録する
        this.setupVideoPlaybackHandler(lifecycle_generation, live_playback_policy);

        // 自動画質: バッファ停滞・回線悪化に応じた段階下げモニタ
        this.startAutoQualityStepDownMonitor(lifecycle_generation);

        // DPlayer のフルスクリーン関係のメソッドを無理やり上書きし、KonomiTV の UI と統合する
        this.setupFullscreenHandler();

        // DPlayer の設定パネルを無理やり拡張し、KonomiTV 独自の項目を追加する
        this.setupSettingPanelHandler(live_playback_policy);

        // L字画面のクロップ設定が変更されたときのイベントハンドラーを登録する
        this.setupLShapedScreenCropHandler();

        // KonomiTV 本体の UI を含むプレイヤー全体のコンテナ要素がリサイズされたときのイベントハンドラーを登録する
        this.setupPlayerContainerResizeHandler();

        // プレイヤーのコントロール UI を表示する (初回実行)
        this.setControlDisplayTimer();

        // ビデオ視聴時のみ、指定されている場合は再生速度をレジュームし、指定秒数シークする
        if (this.playback_mode === 'Video') {

            // DPlayer の画質切り替え時にも現在の再生位置から HLS セグメントをロードさせるためのモンキーパッチを適用
            const dplayer_instance = this.player;
            const originalSwitchQuality = dplayer_instance.switchQuality.bind(dplayer_instance);
            dplayer_instance.switchQuality = (index: number): void => {
                if (dplayer_instance.options?.pluginOptions?.hls && dplayer_instance.video && dplayer_instance.options.live !== true) {
                    // 画質切り替え前の再生位置を hls.js の startPosition に指定して、無駄な HLS セグメントの取得を抑止する
                    dplayer_instance.options.pluginOptions.hls.startPosition = dplayer_instance.video.currentTime;
                }
                originalSwitchQuality(index);
            };

            this.setupRecordedHLSAudioTrackSelector();
            this.player.on('quality_end', () => this.setupRecordedHLSAudioTrackSelector());

            // 初期化前に算出しておいた秒数分初回シークを実行
            // 録画マージン分シークするケースと、プレイヤー再起動前の再生位置を復元するケースの2通りある
            this.player.seek(seek_seconds);

            // 指定されている場合はプレイヤー再起動前の再生速度を復元する
            if (options.playback_rate !== null) {
                this.player.speed(options.playback_rate);
            }

            // 初回シーク時は確実にエンコーダーの起動が発生するため、ロードに若干時間がかかる
            // このため DPlayer.seek() 内部で実行されているシークバーの更新処理は動作せず、再生が開始されるまで再生済み範囲は反映されない
            // ここで再生済み範囲がシークバー上反映されていないとユーザーの認知的不協和を招くため、手動で再生済み範囲をシーク地点に移動する
            // この時点ではまだ HLS プレイリストのロードが完了していないため、API から取得済みの動画長を用いて割合を計算する
            this.player.bar.set('played', seek_seconds / player_store.recorded_program.recorded_video.duration, 'width');

            // 視聴履歴から再生を再開する場合のみ通知を表示
            // そうでない場合は seek() 実行後に表示される通知を即座に非表示にする
            if (
                is_initial_video_playback_without_history === false &&
                seek_seconds > player_store.recorded_program.recording_start_margin + 2
            ) {
                this.player.notice('前回視聴した続きから再生します');
            } else {
                this.player.hideNotice();
            }
            this.player.play();
            console.log(`\u001b[31m[PlayerController] Seeking to ${seek_seconds} seconds.`);
        }

        // UI コンポーネントからプレイヤーに通知メッセージの送信を要求されたときのイベントハンドラーを登録する
        // このイベントは常にアプリケーション上で1つだけ登録されていなければならない
        player_store.event_emitter.off('SendNotification');  // SendNotification イベントの全てのイベントハンドラーを削除
        player_store.event_emitter.on('SendNotification', (event) => {
            if (this.destroyed === true || this.player === null) return;
            this.player.notice(event.message, event.duration, event.opacity, event.color);
        });

        // PlayerManager からプレイヤーの再起動が必要になったことを通知されたときのイベントハンドラーを登録する
        // このイベントは常にアプリケーション上で1つだけ登録されていなければならない
        // さもなければ使い終わった破棄済みの PlayerController が再起動イベントにより復活し、現在利用中の PlayerController と競合してしまう
        player_store.event_emitter.off('PlayerRestartRequired');  // PlayerRestartRequired イベントの全てのイベントハンドラーを削除
        player_store.event_emitter.on('PlayerRestartRequired', async (event) => {

            // すでに破棄済みであれば何もしない
            if (this.destroyed === true || this.player === null) return;
            console.warn('\u001b[31m[PlayerController] PlayerRestartRequired event received. Message: ', event.message);

            // ライブ視聴: iOS 17.0 以下で mpegts.js がサポートされていない場合は再起動できない
            if (this.playback_mode === 'Live' && mpegts.isSupported() !== true) {  // mpegts.js 非対応環境では undefined が返る
                console.warn('\u001b[31m[PlayerController] PlayerRestartRequired event received, but mpegts.js is not supported. Ignored.');
                // iOS 17.0 以下は mpegts.js がサポートされていないため、再生できない
                this.player?.notice('iOS (Safari) 17.0 以下での視聴には対応していません。速やかに iOS を 17.1 以降に更新してください。', -1, undefined, 'rgb(var(--v-theme-error-readable))');
                return;
            }

            await this.restartPlayer(async () => {

                // 現在の再生画質・再生速度・再生位置を取得
                // この情報がプレイヤー再起動後にレジュームされる
                const player = this.player;
                assert(player !== null);
                const should_resume_quality = event.should_resume_quality !== false;
                const quality_index = player.qualityIndex ?? null;
                // 画質プロファイルの既定値を優先する場合は直前の画質を引き継がない
                const current_quality = should_resume_quality === true && player.options.video.quality && typeof quality_index === 'number'
                    ? player.options.video.quality[quality_index]
                    : null;
                const konomitv_bs4k_resume_quality =
                    event.konomitv_bs4k_resume_quality ?? current_quality?.name ?? null;
                const current_playback_rate = player.video.playbackRate ?? null;
                const current_time = player.video.currentTime ?? null;

                // PlayerController 自身を破棄
                await this.destroy();

                // ライブ視聴時のみ即座に再起動すると諸々問題があるので、少し待つ
                if (this.playback_mode === 'Live') {
                    await Utils.sleep(0.5, this.owner_signal ?? undefined);
                }
                if (this.isOwnerAborted()) return;

                // PlayerController 自身を再初期化
                // 再起動完了時点でこの PlayerRestartRequired のイベントハンドラーは再登録されているはず
                await this.init({
                    // 現在の再生画質・再生速度 (ビデオ視聴時のみ)・再生位置 (ビデオ視聴時のみ) を引き継ぐ
                    default_quality: konomitv_bs4k_resume_quality,
                    playback_rate: this.playback_mode === 'Video' ? current_playback_rate : null,
                    seek_seconds: this.playback_mode === 'Video' ? current_time : null,
                });
                if (this.isOwnerAborted() || this.player === null) return;

                // プレイヤー側にイベントの発火元から送られたメッセージ (プレイヤーを再起動中である旨) を通知する
                // 再初期化により、作り直した DPlayer が再び this.player にセットされているはず
                // 通知を表示してから PlayerController を破棄すると DPlayer の DOM 要素ごと消えてしまうので、DPlayer を作り直した後に通知を表示する
                const restarted_player = this.player;
                if (event.message) {
                    // 遅延時間が指定されていれば待つ
                    await Utils.sleep(event.message_delay_seconds ?? 0, this.owner_signal ?? undefined);
                    if (this.isOwnerAborted() || this.player !== restarted_player) return;
                    // 明示的にエラーメッセージではないことが指定されていればデフォルトの色で通知を表示する
                    // デフォルトではメッセージは赤色で表示される
                    const color = event.is_error_message === false ? undefined : 'rgb(var(--v-theme-error-readable))';
                    restarted_player.notice(event.message, undefined, undefined, color);
                }
            });
        });

        // 低遅延設定の実効値が変わった場合は、上で登録した既存の安全な再起動経路を使って次世代へ反映する。
        this.setupLivePlaybackPolicyWatcher(live_playback_policy, initial_live_low_latency_settings);

        // PlayerController.setControlDisplayTimer() の呼び出しを要求されたときのイベントハンドラーを登録する
        // このイベントは常にアプリケーション上で1つだけ登録されていなければならない
        player_store.event_emitter.off('SetControlDisplayTimer');  // SetControlDisplayTimer イベントの全てのイベントハンドラーを削除
        player_store.event_emitter.on('SetControlDisplayTimer', (event) => {
            this.setControlDisplayTimer(event.event, event.is_player_region_event, event.timeout_seconds);
        });

        // 録画再生時のみ: UI コンポーネントから指定秒数へのシークを要求されたときのイベントハンドラーを登録する
        // コメントリストからコメントをクリックした際などに利用される
        if (this.playback_mode === 'Video') {
            player_store.event_emitter.off('SeekRequest');  // SeekRequest イベントの全てのイベントハンドラーを削除
            player_store.event_emitter.on('SeekRequest', (event) => {
                if (this.destroyed === true || this.player === null) return;
                this.player.seek(event.playback_position);
                this.player.play();
            });
        }

        // プレイヤー再起動ボタンを DPlayer の UI に追加する (再生が止まった際などに利用する想定)
        // insertAdjacentHTML で .dplayer-icons-right の一番左側に配置する
        this.player.container.querySelector('.dplayer-icons.dplayer-icons-right')!.insertAdjacentHTML('afterbegin', `
            <div class="dplayer-icon dplayer-player-restart-icon" aria-label="プレイヤーを再起動"
                data-balloon-nofocus="" data-balloon-pos="up">
                <span class="dplayer-icon-content">
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"><path fill="currentColor" d="M12 5V3.21c0-.45-.54-.67-.85-.35l-2.8 2.79c-.2.2-.2.51 0 .71l2.79 2.79c.32.31.86.09.86-.36V7c3.31 0 6 2.69 6 6c0 2.72-1.83 5.02-4.31 5.75c-.42.12-.69.52-.69.95c0 .65.62 1.16 1.25.97A7.991 7.991 0 0 0 20 13c0-4.42-3.58-8-8-8zm-6 8c0-1.34.44-2.58 1.19-3.59c.3-.4.26-.95-.09-1.31c-.42-.42-1.14-.38-1.5.1a7.991 7.991 0 0 0 4.15 12.47c.63.19 1.25-.32 1.25-.97c0-.43-.27-.83-.69-.95C7.83 18.02 6 15.72 6 13z"/></svg>
                </span>
            </div>
        `);
        // PlayerRestartRequired イベントとは異なり、通知メッセージなしで即座に PlayerController を再起動する
        this.player.container.querySelector('.dplayer-player-restart-icon')!.addEventListener('click', async () => {

            await this.restartPlayer(async () => {

                // 現在の再生画質・再生速度・再生位置を取得
                // この情報がプレイヤー再起動後にレジュームされる
                const current_quality = this.player?.qualityIndex ? this.player.options.video.quality![this.player.qualityIndex] : null;
                const current_playback_rate = this.player?.video.playbackRate ?? null;
                const current_time = this.player?.video.currentTime ?? null;

                // PlayerController 自身を破棄
                // このイベントは手動で再起動した際に実行されるものなので、再初期化までは待たずに即座に再初期化する
                await this.destroy();
                if (this.isOwnerAborted()) return;

                // PlayerController 自身を再初期化
                await this.init({
                    // 現在の再生画質・再生速度 (ビデオ視聴時のみ)・再生位置 (ビデオ視聴時のみ) を引き継ぐ
                    default_quality: current_quality ? current_quality.name : null,
                    playback_rate: this.playback_mode === 'Video' ? current_playback_rate : null,
                    seek_seconds: this.playback_mode === 'Video' ? current_time : null,
                });

                // 通知を表示してから PlayerController を破棄すると DPlayer の DOM 要素ごと消えてしまうので、DPlayer を作り直した後に通知を表示する
                this.player?.notice('プレイヤーを再起動しました。', undefined, undefined, undefined);
            });
        });

        // Screen Wake Lock API を利用して画面の自動スリープを抑制する
        // 待つ必要はないので非同期で実行
        if ('wakeLock' in navigator) {
            navigator.wakeLock.request('screen').then((wake_lock) => {
                this.screen_wake_lock = wake_lock;  // 後で解除するために WakeLockSentinel を保持
                console.log('\u001b[31m[PlayerController] Screen Wake Lock API: Screen Wake Lock acquired.');
            });
        }

        // 各 PlayerManager を初期化・登録
        // ライブ視聴とビデオ視聴で必要な PlayerManager が異なる
        // この初期化順序は意図的 (入れ替えても動作するものもあるが、CaptureManager は KeyboardShortcutManager より先に初期化する必要がある)
        if (this.playback_mode === 'Live') {
            // ライブ視聴時に設定する PlayerManager
            this.player_managers = [
                new LiveEventManager(
                    this.player,
                    (status, detail) =>
                        this.handleKonomiTVBS4KLivePlaybackPipelineStatus(status, detail),
                ),
                // 実況機能が無効な場合は接続情報 API へのアクセスも WebSocket 接続も開始しない
                ...(settings_store.is_jikkyo_enabled === true ?
                    [new LiveCommentManager(this.player, live_playback_policy)] : []),
                new LiveDataBroadcastingManager(this.player),
                new CaptureManager(this.player, this.playback_mode),
                new DocumentPiPManager(this.player, this.playback_mode),
                new KeyboardShortcutManager(this.player, this.playback_mode),
                new MediaSessionManager(this.player, this.playback_mode),
            ];
        } else {
            // ビデオ視聴時に設定する PlayerManager
            this.player_managers = [
                new RecordedCMSkipManager(this.player, (target_time) => {
                    // CM 自動スキップは自然再生を継続する操作なので、末尾への直接シーク扱いにはしない。
                    // seeking / seeked でブラウザ側の実際のシーク先を照合できるよう、先に目標位置を記録する。
                    this.recorded_auto_skip_cm_target = target_time;
                    this.recorded_playback_end_blocked_by_seek = false;
                }),
                new CaptureManager(this.player, this.playback_mode),
                new DocumentPiPManager(this.player, this.playback_mode),
                new KeyboardShortcutManager(this.player, this.playback_mode),
                new MediaSessionManager(this.player, this.playback_mode),
            ];
        }

        // 登録されている PlayerManager をすべて初期化
        // これにより各 PlayerManager での実際の処理が開始される
        // 同期処理すると時間が掛かるので、並行して実行する
        await Promise.all(this.player_managers.map((player_manager) => player_manager.init()));
        if (this.isLifecycleCurrent(lifecycle_generation, this.player) === false) return;

        console.log('\u001b[31m[PlayerController] Initialized.');
    }

    /**
     * 非同期処理が現在の controller / init / DPlayer 世代に属しているかを返す
     */
    private isLifecycleCurrent(lifecycle_generation: number, player?: DPlayer | null): boolean {
        return (
            this.lifecycle_generation === lifecycle_generation &&
            this.lifecycle_abort_controller.signal.aborted === false &&
            this.owner_signal?.aborted !== true &&
            this.destroying === false &&
            this.destroyed === false &&
            (player === undefined || this.player === player)
        );
    }

    /** View owner が route change / unmount 済みかを返す。 */
    private isOwnerAborted(): boolean {
        return this.owner_signal?.aborted === true;
    }

    /**
     * ライブ開始バッファが目標値へ達するまで、破棄可能な待機を行う
     */
    private async waitForLivePlaybackBuffer(
        player: DPlayer,
        lifecycle_generation: number,
        target_seconds: number,
        signal: AbortSignal,
    ): Promise<boolean> {
        while (this.getPlaybackBufferSeconds() < target_seconds) {
            if (this.isLifecycleCurrent(lifecycle_generation, player) === false) return false;
            await Utils.sleep(0.1, signal);
            if (this.isLifecycleCurrent(lifecycle_generation, player) === false) return false;
        }
        return this.isLifecycleCurrent(lifecycle_generation, player);
    }


    /**
     * HTMLVideoElement / DPlayer の native error listener を同一 DPlayer / 初期化世代で一度だけ登録する
     *
     * DPlayer の native listener は mpegts.js / hls.js plugin と異なり quality_start で破棄されない。
     * 画質切り替えのたびに重ねると、1回の実エラーから fallback / restart を複数回要求してしまう。
     */
    private setupKonomiTVBS4KNativePlaybackErrorHandler(
        konomitv_bs4k_lifecycle_generation: number,
        konomitv_bs4k_setup_player: DPlayer,
    ): void {

        if (
            this.isLifecycleCurrent(
                konomitv_bs4k_lifecycle_generation,
                konomitv_bs4k_setup_player,
            ) === false
        ) return;
        if (
            this.konomitv_bs4k_native_playback_error_handler_registration?.konomitv_bs4k_player ===
                konomitv_bs4k_setup_player &&
            this.konomitv_bs4k_native_playback_error_handler_registration
                .konomitv_bs4k_lifecycle_generation === konomitv_bs4k_lifecycle_generation
        ) {
            return;
        }

        // listener 登録前に記録し、同期的に quality_start が重なっても二重登録しない。
        this.konomitv_bs4k_native_playback_error_handler_registration = {
            konomitv_bs4k_player: konomitv_bs4k_setup_player,
            konomitv_bs4k_lifecycle_generation,
        };
        const konomitv_bs4k_lifecycle_signal = this.lifecycle_abort_controller.signal;
        const konomitv_bs4k_player_store = usePlayerStore();

        konomitv_bs4k_setup_player.on('error', async () => {
            // 旧 DPlayer / 旧初期化世代から遅れて届いた error は共有 Store や fallback 状態へ触れない。
            if (
                this.isLifecycleCurrent(
                    konomitv_bs4k_lifecycle_generation,
                    konomitv_bs4k_setup_player,
                ) === false
            ) return;
            if (
                this.playback_mode === 'Live' &&
                konomitv_bs4k_player_store.live_stream_status === 'Offline'
            ) return;

            // ライブは一時的な切断から自然復旧する余地を残すため、従来どおり少し待つ。
            if (this.playback_mode === 'Live') {
                await Utils.sleep(1, konomitv_bs4k_lifecycle_signal);
                if (
                    this.isLifecycleCurrent(
                        konomitv_bs4k_lifecycle_generation,
                        konomitv_bs4k_setup_player,
                    ) === false
                ) return;
            }

            const konomitv_bs4k_media_error = konomitv_bs4k_setup_player.video.error;
            if (
                konomitv_bs4k_media_error !== null &&
                konomitv_bs4k_media_error.code === konomitv_bs4k_media_error.MEDIA_ERR_DECODE
            ) {
                if (
                    this.requestKonomiTVBS4KCompatibilityPlaybackFallback(
                        `HTMLVideoElement ${konomitv_bs4k_media_error.code}: ` +
                        konomitv_bs4k_media_error.message,
                    ) === true ||
                    this.isKonomiTVBS4KCompatibilityPlaybackFallbackPending() === true
                ) {
                    return;
                }
            }

            if (konomitv_bs4k_media_error !== null) {
                console.error(
                    '\u001b[31m[PlayerController] HTMLVideoElement error event:',
                    konomitv_bs4k_media_error,
                );
                konomitv_bs4k_player_store.event_emitter.emit('PlayerRestartRequired', {
                    message:
                        '再生中にエラーが発生しました。' +
                        `(Native: ${konomitv_bs4k_media_error.code}: ` +
                        `${konomitv_bs4k_media_error.message}) プレイヤーを再起動しています…`,
                });
            } else {
                // MediaError オブジェクトは場合によっては存在しないことがあるため unknown error として扱う。
                konomitv_bs4k_player_store.event_emitter.emit('PlayerRestartRequired', {
                    message: '再生中にエラーが発生しました。(Native: unknown error) プレイヤーを再起動しています…',
                });
            }
        });
    }


    /**
     * ライブ視聴: 現在の DPlayer の再生バッファを再生位置とバッファ秒数の差から取得する
     * ビデオ視聴時と、取得に失敗した場合は 0 を返す
     * @returns バッファ秒数
     */
    private getPlaybackBufferSeconds(): number {
        if (this.player === null) return 0;
        if (this.playback_mode === 'Live') {
            try {
                const buffered_range_count = this.player.video.buffered.length;
                const buffer_remain = this.player.video.buffered.end(buffered_range_count - 1) - this.player.video.currentTime;
                return Utils.mathFloor(buffer_remain, 3);
            } catch (error) {
                return 0;
            }
        } else {
            return 0;
        }
    }


    /**
     * ライブ / 録画共通: currentTime を含む buffered 区間の前方残り秒数。
     * 自動画質のバッファ監視用。取得失敗時は 0。
     */
    private getPlaybackBufferedAheadSeconds(): number {
        if (this.player === null || this.player.video === null) return 0;
        try {
            const buffered = this.player.video.buffered;
            if (buffered.length === 0) return 0;
            const current_time = this.player.video.currentTime;
            for (let index = 0; index < buffered.length; index++) {
                const start = buffered.start(index);
                const end = buffered.end(index);
                if (start <= current_time + 0.25 && current_time < end) {
                    return Utils.mathFloor(Math.max(0, end - current_time), 3);
                }
            }
            // どの区間にも含まれない場合は末尾との差（負なら 0）
            const last_end = buffered.end(buffered.length - 1);
            return Utils.mathFloor(Math.max(0, last_end - current_time), 3);
        } catch {
            return 0;
        }
    }


    /** 自動画質段階下げモニタを停止する。 */
    private stopAutoQualityStepDownMonitor(): void {
        if (this.auto_quality_monitor_timer_id !== null) {
            window.clearInterval(this.auto_quality_monitor_timer_id);
            this.auto_quality_monitor_timer_id = null;
        }
        this.auto_quality_waiting_started_at_ms = null;
        this.auto_quality_marginal_buffer_started_at_ms = null;
        this.auto_quality_step_down_in_progress = false;
    }


    /**
     * 自動画質モード時のみ、バッファ停滞に応じて 1 段ずつ画質を下げるモニタを開始する。
     * 既存モニタがあれば張り替える。
     */
    private startAutoQualityStepDownMonitor(lifecycle_generation: number): void {
        this.stopAutoQualityStepDownMonitor();
        if (this.isKonomiTVBS4KAutoQualityModeEnabled() === false) {
            return;
        }
        this.auto_quality_has_reached_playing = false;
        // waiting 開始はイベント / 評価ループで測る（開始直後から時計を回さない）
        this.auto_quality_waiting_started_at_ms = null;
        this.auto_quality_marginal_buffer_started_at_ms = null;
        this.auto_quality_step_down_last_at_ms = 0;

        this.auto_quality_monitor_timer_id = window.setInterval(() => {
            if (
                this.isLifecycleCurrent(lifecycle_generation, this.player) === false ||
                this.isKonomiTVBS4KAutoQualityModeEnabled() === false
            ) {
                this.stopAutoQualityStepDownMonitor();
                return;
            }
            this.evaluateAutoQualityStepDown(lifecycle_generation);
        }, PlayerController.AUTO_QUALITY_MONITOR_INTERVAL_MS);
    }


    /**
     * waiting / バッファ / 回線指標を見て、必要なら 1 段下げる。
     */
    private evaluateAutoQualityStepDown(lifecycle_generation: number): void {
        if (
            this.isLifecycleCurrent(lifecycle_generation, this.player) === false ||
            this.player === null ||
            this.player.switchingQuality === true ||
            this.auto_quality_step_down_in_progress === true
        ) {
            return;
        }

        const now = dayjs().valueOf();
        const player_store = usePlayerStore();
        const is_waiting =
            player_store.is_video_buffering === true ||
            player_store.is_loading === true ||
            this.player.video?.readyState < 3;

        if (is_waiting === true) {
            if (this.auto_quality_waiting_started_at_ms === null) {
                this.auto_quality_waiting_started_at_ms = now;
            }
        } else {
            this.auto_quality_waiting_started_at_ms = null;
        }

        // 1) 開始不能・中断の waiting 継続
        if (this.auto_quality_waiting_started_at_ms !== null) {
            const waited_ms = now - this.auto_quality_waiting_started_at_ms;
            const stall_threshold_ms = this.auto_quality_has_reached_playing === true ?
                PlayerController.AUTO_QUALITY_MIDSTREAM_STALL_MS :
                PlayerController.AUTO_QUALITY_STARTUP_STALL_MS;
            if (waited_ms >= stall_threshold_ms) {
                this.requestAutoQualityStepDown(
                    this.auto_quality_has_reached_playing === true ?
                        'midstream_stall' :
                        'startup_stall',
                );
                return;
            }
        }

        // 2) 再生中だがバッファがギリギリで続いている（課題3）
        if (
            this.auto_quality_has_reached_playing === true &&
            is_waiting === false &&
            this.player.video?.paused !== true
        ) {
            const buffered_ahead = this.getPlaybackBufferedAheadSeconds();
            if (buffered_ahead < PlayerController.AUTO_QUALITY_MARGINAL_BUFFER_SECONDS) {
                if (this.auto_quality_marginal_buffer_started_at_ms === null) {
                    this.auto_quality_marginal_buffer_started_at_ms = now;
                } else if (
                    now - this.auto_quality_marginal_buffer_started_at_ms >=
                    PlayerController.AUTO_QUALITY_MARGINAL_BUFFER_STREAK_MS
                ) {
                    this.requestAutoQualityStepDown('marginal_buffer');
                    return;
                }
            } else {
                this.auto_quality_marginal_buffer_started_at_ms = null;
            }
        }

        // 3) Network Information が悪化し、現在高さが回線上限を大きく超える
        const network_ceiling = estimateNetworkHeightCeiling(readNetworkQualityMetrics());
        if (network_ceiling !== null) {
            const current_height = getPlaybackQualityHeight(
                this.konomitv_bs4k_playback_video_profile_for_current_playback.streaming_quality,
            );
            if (current_height > network_ceiling) {
                this.requestAutoQualityStepDown('network_ceiling');
            }
        }
    }


    /** Live切替の全cleanup完了をABR再開とCooldownの起点にする。 */
    private setKonomiTVBS4KLiveABRFrozen(frozen: boolean): void {
        const was_frozen = this.auto_quality_step_down_in_progress;
        this.auto_quality_step_down_in_progress = frozen;
        // Prepare/Verify/finalize/lease解放を終えてABRを再開した時点から
        // Cooldownを数える。要求発行時から数えると長い切替中に満了してしまう。
        if (frozen === false && was_frozen === true) {
            this.auto_quality_step_down_last_at_ms = dayjs().valueOf();
        }
    }


    /**
     * 自動モードで現在画質の 1 段下へ DPlayer.switchQuality する。
     * 成功時 true。クールダウン中・最低段・切替中は false。
     */
    private requestAutoQualityStepDown(reason: string): boolean {
        if (this.isKonomiTVBS4KAutoQualityModeEnabled() === false) {
            return false;
        }
        if (this.player === null || this.destroyed === true || this.destroying === true) {
            return false;
        }
        if (this.player.switchingQuality === true || this.auto_quality_step_down_in_progress === true) {
            return false;
        }
        const now = dayjs().valueOf();
        if (
            this.auto_quality_step_down_last_at_ms > 0 &&
            now - this.auto_quality_step_down_last_at_ms < PlayerController.AUTO_QUALITY_STEP_DOWN_COOLDOWN_MS
        ) {
            return false;
        }

        const qualities = this.player.options.video.quality;
        if (qualities === undefined || qualities.length === 0) {
            return false;
        }
        const current_index = typeof this.player.qualityIndex === 'number' ?
            this.player.qualityIndex :
            0;
        const is_bs4k = this.konomitv_bs4k_playback_video_profile_for_current_playback.is_bs4k === true;
        const ladder = this.getKonomiTVBS4KPlaybackSelectableQualities(is_bs4k);
        const current_api =
            this.konomitv_bs4k_auto_session_override.streaming_quality ??
            this.konomitv_bs4k_playback_video_profile_for_current_playback.streaming_quality;
        const next_api = getNextLowerAutoPlaybackQuality(ladder, current_api);
        if (next_api === null) {
            return false;
        }

        // ラダー上の次段に対応する DPlayer 画質 index を探す（非対応段はスキップ）
        let target_index: number | null = null;
        for (let index = current_index + 1; index < qualities.length; index++) {
            const profile = this.resolveKonomiTVBS4KPlaybackVideoProfileFromDPlayerQuality(
                qualities[index],
            );
            if (profile === null) {
                continue;
            }
            if (this.isKonomiTVBS4KCurrentPlaybackVideoProfileSupported(profile) === false) {
                continue;
            }
            // 次段以下（同高の 60→30 含む）なら採用
            const candidate = profile.streaming_quality;
            if (
                candidate === next_api ||
                getPlaybackQualityHeight(candidate) <= getPlaybackQualityHeight(next_api)
            ) {
                target_index = index;
                break;
            }
        }
        // current_index 以降で見つからない場合はラダー全体から next_api を探す
        if (target_index === null) {
            for (let index = 0; index < qualities.length; index++) {
                if (index === current_index) continue;
                const profile = this.resolveKonomiTVBS4KPlaybackVideoProfileFromDPlayerQuality(
                    qualities[index],
                );
                if (profile === null) continue;
                if (profile.streaming_quality !== next_api) continue;
                if (this.isKonomiTVBS4KCurrentPlaybackVideoProfileSupported(profile) === false) {
                    continue;
                }
                target_index = index;
                break;
            }
        }
        if (target_index === null) {
            return false;
        }

        const waits_for_live_coordinator = (
            this.playback_mode === 'Live' &&
            this.konomitv_bs4k_live_quality_switch_coordinator !== null
        );
        this.auto_quality_step_down_in_progress = true;
        if (waits_for_live_coordinator === false) {
            this.auto_quality_step_down_last_at_ms = now;
        }
        this.auto_quality_waiting_started_at_ms = null;
        this.auto_quality_marginal_buffer_started_at_ms = null;

        // LiveはHard Gate/Verify後のCommit callbackだけがsession overrideを確定する。
        // 録画は従来のDPlayer切替なので、この時点で一時設定へ反映する。
        if (this.playback_mode !== 'Live') {
            this.konomitv_bs4k_auto_session_override = {
                ...this.konomitv_bs4k_auto_session_override,
                streaming_quality: next_api as LiveStreamingQuality | BS4KLiveStreamingQuality,
            };
        }

        console.info(
            `\u001b[31m[PlayerController] Auto quality step-down (${reason}): ` +
            `${current_api} → ${next_api}`,
        );

        try {
            this.player.switchQuality(target_index);
            if (this.playback_mode !== 'Live') {
                this.player.notice(
                    `回線状況に合わせて画質を ${PlayerUtils.getKonomiTVBS4KPlaybackQualityDisplayName(next_api)} に下げました`,
                    3000,
                    undefined,
                    undefined,
                );
            }
        } catch (error) {
            console.warn('[PlayerController] Auto quality step-down failed.', error);
            this.auto_quality_step_down_in_progress = false;
            return false;
        }

        // 二重pipeline LiveだけはCoordinatorの全cleanup完了 callbackでABR freezeを解除する。
        // radio/audio-onlyを含む従来の単一pipelineと録画は短時間後に解除する。
        if (waits_for_live_coordinator === false) {
            window.setTimeout(() => {
                this.auto_quality_step_down_in_progress = false;
            }, 2_000);
        }
        return true;
    }


    /**
     * まだ再生が開始できていない場合 (HTMLVideoElement.readyState < HAVE_FUTURE_DATA) に再生状態の復旧を試みる
     * 処理の完了を待つ必要はないので、基本 await せず非同期で実行すべき
     * 基本 Safari だとなぜか再生開始がうまく行かないことが多いので（自動再生まわりが影響してる？）、その対策として用意した処理
     */
    private async recoverPlayback(): Promise<void> {
        assert(this.player !== null);
        const player_store = usePlayerStore();

        // 1 秒待つ
        await Utils.sleep(1);

        // この時点で映像が停止していて、かつ readyState が HAVE_FUTURE_DATA な場合、復旧を試みる
        // Safari ではタイミングによっては this.player.video が null になる場合があるらしいので ? を付ける
        if (player_store.is_video_buffering === true && this.player?.video?.readyState < 3) {
            console.warn('\u001b[31m[PlayerController] Video still buffering. (HTMLVideoElement.readyState < HAVE_FUTURE_DATA) Trying to recover.');

            // 一旦停止して、0.25 秒間を置く
            this.player.video.pause();
            await Utils.sleep(0.25);

            // 再度再生を試みる
            try {
                await this.player.video.play();
            } catch (error) {
                assert(this.player !== null);
                console.warn('\u001b[31m[PlayerController] HTMLVideoElement.play() rejected. paused.');
                this.player.pause();
                return;  // 再生開始がリジェクトされた場合はここで終了
            }

            // さらに 0.5 秒待った時点で映像が停止している場合、復旧を試みる
            await Utils.sleep(0.5);
            if (player_store.is_video_buffering === true && this.player?.video?.readyState < 3) {
                console.warn('\u001b[31m[PlayerController] Video still buffering. (HTMLVideoElement.readyState < HAVE_FUTURE_DATA) Trying to recover.');

                // 一旦停止して、0.25 秒間を置く
                this.player.video.pause();
                await Utils.sleep(0.25);

                // 再度再生を試みる
                try {
                    await this.player.video.play();
                } catch (error) {
                    assert(this.player !== null);
                    console.warn('\u001b[31m[PlayerController] (retry) HTMLVideoElement.play() rejected. paused.');
                    this.player.pause();
                }
            }
        }
    }


    /**
     * DPlayer 内蔵の aribb24.js レンダラーを、KonomiTV 側でパッチしたソース版へ差し替える。
     * ワンセグでは ARIB STD-B24 Profile C、それ以外では Profile A を指定する。
     */
    private replaceARIBB24Renderers(): void {
        assert(this.player !== null);

        const aribb24_options = this.player.options.pluginOptions.aribb24!;
        const aribb24_plugins = this.player.plugins as unknown as {
            aribb24Caption?: CanvasRenderer;
            aribb24Superimpose?: CanvasRenderer;
        };
        const is_caption_hidden = this.player.subtitle?.container.classList.contains('dplayer-subtitle-hide') ?? false;

        aribb24_plugins.aribb24Caption?.dispose();
        const aribb24_caption = new CanvasRenderer({
            ...aribb24_options,
            profile: this.aribb24_profile,
            data_identifier: 0x80,
        });
        aribb24_caption.attachMedia(this.player.video);
        if (is_caption_hidden) {
            aribb24_caption.hide();
        } else {
            aribb24_caption.show();
        }
        aribb24_plugins.aribb24Caption = aribb24_caption;

        aribb24_plugins.aribb24Superimpose?.dispose();
        if (aribb24_options.disableSuperimposeRenderer !== true) {
            const aribb24_superimpose = new CanvasRenderer({
                ...aribb24_options,
                profile: this.aribb24_profile,
                data_identifier: 0x81,
            });
            aribb24_superimpose.attachMedia(this.player.video);
            aribb24_superimpose.show();
            aribb24_plugins.aribb24Superimpose = aribb24_superimpose;
        } else {
            delete aribb24_plugins.aribb24Superimpose;
        }

    }


    /**
     * ARIB-TTML レンダラーを現在の video 要素へ接続する。
     * 画質切り替えでは video 要素自体が作り直されるため、同じデコーダーを新しい要素へ付け直す。
     */
    private attachARIBTTMLRenderer(): void {
        assert(this.player !== null);
        const aribb24_options = this.player.options.pluginOptions.aribb24!;
        if (this.arib_ttml_renderer === null) {
            this.arib_ttml_renderer = new ARIBTTMLRenderer({
                normal_font: aribb24_options.normalFont,
                force_stroke_color: aribb24_options.forceStrokeColor,
                force_background_color: aribb24_options.forceBackgroundColor,
                show_superimpose: aribb24_options.disableSuperimposeRenderer !== true,
                playback_mode: this.playback_mode === 'Live' ? 'Live' : 'Playback',
                rom_sound_callback: aribb24_options.PRACallback,
            });
        }
        (this.player.plugins as unknown as {aribTTML?: ARIBTTMLRenderer}).aribTTML = this.arib_ttml_renderer;
        this.arib_ttml_renderer.attachMedia(this.player.video);
        const is_caption_hidden = this.player.subtitle?.container.classList.contains('dplayer-subtitle-hide') ?? false;
        if (is_caption_hidden) this.arib_ttml_renderer.hideCaption();
        else this.arib_ttml_renderer.showCaption();
        this.arib_ttml_renderer.setSuperimposeVisibility(aribb24_options.disableSuperimposeRenderer !== true);
    }


    /** 現在のライブ mpegts.js から共通 ARIB-TTML レンダラーへ timed-ID3 を渡す。 */
    private attachLiveARIBTTMLStream(): void {
        assert(this.player !== null);
        const source = this.player.plugins.mpegts as unknown as typeof this.live_arib_ttml_source | undefined;
        if (source === null || source === undefined || source === this.live_arib_ttml_source) return;
        // A/B Verify中は旧Aも進行し続けるため、旧世代listenerを明示的に外して字幕混在を防ぐ。
        this.detachLiveARIBTTMLStream();
        source.on(mpegts.Events.TIMED_ID3_METADATA_ARRIVED, this.live_arib_ttml_handler);
        this.live_arib_ttml_source = source;
    }

    /**
     * 現在のライブ mpegts.js から timed-ID3 listener を外す。
     * Coordinator は active pipeline と mpegts.js を同期的に破棄するため、必ずその前に呼び出す。
     */
    private detachLiveARIBTTMLStream(): void {
        const source = this.live_arib_ttml_source;
        this.live_arib_ttml_source = null;
        if (source === null || source === undefined) return;
        try {
            source.off(
                mpegts.Events.TIMED_ID3_METADATA_ARRIVED,
                this.live_arib_ttml_handler,
            );
        } catch (error) {
            // 破棄要求と mpegts.js 自身の異常終了が同時に起きても、残りのリソース回収を継続する。
            console.warn('[PlayerController] Failed to detach the live ARIB-TTML stream.', error);
        }
    }

    /**
     * Commit critical section内で字幕世代だけを同期更新する。Network/Manager初期化は行わない。
     */
    private commitKonomiTVBS4KLiveSubtitleGeneration(
        pipeline: KonomiTVBS4KLiveMSEPipeline,
        history: readonly IKonomiTVBS4KTimedID3HistoryEntry[],
    ): void {
        if (this.player === null || this.player.plugins.mpegts !== pipeline.mpegts_player) return;
        this.replaceARIBB24Renderers();
        this.attachARIBTTMLRenderer();
        this.attachLiveARIBTTMLStream();
        for (const entry of history) {
            this.live_arib_ttml_handler(entry.metadata);
        }
    }


    /**
     * A/B の長寿命接続が Firefox の同一 host 接続枠を占有するため、
     * finalize DELETE の直前だけ UI 用の重複 SSE を同期的に閉じる。
     * Pipeline 自身の telemetry SSE は維持し、Server finalize後に通常のManager再起動で戻す。
     */
    private releaseKonomiTVBS4KLiveServerCommitConnectionSlot(): void {
        this.releaseKonomiTVBS4KLiveControlConnectionSlot(false);
    }


    /** Firefox で prepare POST を送る直前に UI 用 SSE の接続枠を一時解放する。 */
    private releaseKonomiTVBS4KLivePrepareConnectionSlot(): void {
        this.releaseKonomiTVBS4KLiveControlConnectionSlot(true);
    }


    /** 切替が Commit 前に終わった場合、現画質を監視する UI 用 SSE を再開する。 */
    private restoreKonomiTVBS4KLivePrepareConnectionSlot(): void {
        if (
            this.live_prepare_connection_slot_released === false ||
            this.player === null ||
            this.destroyed === true ||
            this.destroying === true ||
            this.lifecycle_abort_controller.signal.aborted === true
        ) {
            return;
        }
        this.live_prepare_connection_slot_released = false;
        const live_event_manager = this.player_managers.find(
            (player_manager) => player_manager instanceof LiveEventManager,
        );
        if (live_event_manager === undefined) return;
        void live_event_manager.init().catch((error) => {
            console.warn(
                '[PlayerController] Failed to restore a server prepare connection slot.',
                error,
            );
        });
    }


    /** UI 用の重複 SSE を同期的に閉じ、Firefox の同一 host 接続枠を解放する。 */
    private releaseKonomiTVBS4KLiveControlConnectionSlot(
        restore_after_prepare: boolean,
    ): void {
        if (
            this.player === null ||
            this.destroyed === true ||
            this.destroying === true ||
            this.lifecycle_abort_controller.signal.aborted === true
        ) {
            return;
        }
        // 進行中の旧画質Manager再起動が、close直後にSSEを再接続しないよう無効化する。
        this.live_quality_manager_restart_generation += 1;
        const live_event_manager = this.player_managers.find(
            (player_manager) => player_manager instanceof LiveEventManager,
        );
        if (live_event_manager === undefined) return;
        if (restore_after_prepare === true) {
            this.live_prepare_connection_slot_released = true;
        }
        // LiveEventManager.destroy() は最初のawaitより前に EventSource.close() を実行する。
        void live_event_manager.destroy().catch((error) => {
            console.warn(
                '[PlayerController] Failed to release a live control connection slot.',
                error,
            );
        });
    }


    /** 所有権確定後にだけ画質状態とManager購読を切り替える。 */
    private updateKonomiTVBS4KLivePipelineQualityState(
        pipeline: KonomiTVBS4KLiveMSEPipeline,
    ): void {
        if (this.player === null) return;
        const target_quality = this.player.options.video.quality?.[pipeline.spec.quality_index];
        if (target_quality !== undefined) {
            const target_profile =
                this.resolveKonomiTVBS4KPlaybackVideoProfileFromDPlayerQuality(target_quality);
            if (target_profile !== null) {
                this.konomitv_bs4k_playback_video_profile_for_current_playback = target_profile;
                if (this.isKonomiTVBS4KAutoQualityModeEnabled() === true) {
                    this.konomitv_bs4k_auto_session_override = {
                        ...this.konomitv_bs4k_auto_session_override,
                        streaming_quality: target_profile.streaming_quality,
                    };
                }
            }
        }
    }


    /** 所有権確定後にだけ画質状態とManager購読を切り替える。 */
    private handleKonomiTVBS4KLivePipelineCommitted(
        pipeline: KonomiTVBS4KLiveMSEPipeline,
    ): void {
        if (
            this.player === null ||
            this.destroyed === true ||
            this.destroying === true ||
            this.lifecycle_abort_controller.signal.aborted === true ||
            this.player.plugins.mpegts !== pipeline.mpegts_player
        ) {
            return;
        }
        // この後の Manager 再起動が LiveEventManager も再接続するため、個別復元は不要。
        this.live_prepare_connection_slot_released = false;
        const mpegts_player = pipeline.mpegts_player as unknown as {
            switchAudioTrack?: (index: number) => void;
        };
        if (typeof mpegts_player.switchAudioTrack === 'function') {
            try {
                mpegts_player.switchAudioTrack(this.live_selected_audio_track_index);
            } catch (error) {
                console.warn(
                    '[PlayerController] Failed to restore live audio track after quality commit.',
                    error,
                );
            }
        }
        this.updateKonomiTVBS4KLivePipelineQualityState(pipeline);
        const player = this.player;
        const lifecycle_generation = this.lifecycle_generation;
        const restart_generation = ++this.live_quality_manager_restart_generation;
        const restart_managers = this.player_managers.filter(
            (player_manager) =>
                player_manager.restart_required_when_quality_switched === true,
        );
        this.live_quality_manager_restart_chain =
            this.live_quality_manager_restart_chain
                .catch((error) => {
                    console.warn(
                        '[PlayerController] Previous live quality manager restart failed.',
                        error,
                    );
                })
                .then(async () => {
                    const is_current = (): boolean => (
                        restart_generation === this.live_quality_manager_restart_generation &&
                        this.isLifecycleCurrent(lifecycle_generation, player) &&
                        player.plugins.mpegts === pipeline.mpegts_player
                    );
                    if (is_current() === false) return;
                    await Promise.all(restart_managers.map(
                        async (player_manager) => player_manager.destroy(),
                    ));
                    if (is_current() === false) return;
                    await Promise.all(restart_managers.map(
                        async (player_manager) => player_manager.init(),
                    ));
                })
                .catch((error) => {
                    console.warn(
                        '[PlayerController] Live quality manager restart failed.',
                        error,
                    );
                });
        this.setupLiveAudioTrackMonitor();
    }


    /**
     * DPlayer に動画再生系のイベントハンドラーを登録する
     * 特にライブ視聴ではここで適切に再生状態の管理 (再生可能かどうか、エラーが発生していないかなど) を行う必要がある
     */
    private setupVideoPlaybackHandler(
        lifecycle_generation: number,
        live_playback_policy: Readonly<ILivePlaybackPolicy>,
    ): void {
        assert(this.player !== null);
        const setup_player = this.player;
        const channels_store = useChannelsStore();
        const player_store = usePlayerStore();
        const settings_store = useSettingsStore();

        // ライブ視聴: 再生停止状態かつ現在の再生位置からバッファが 30 秒以上離れていないかを 60 秒おきに監視し、そうなっていたら強制的にシークする
        // mpegts.js の仕様上、MSE 側に未再生のバッファが貯まり過ぎると新規に SourceBuffer が追加できなくなるため、強制的に接続が切断されてしまう
        // 再生停止状態でも定期的にシークすることで、バッファが貯まりすぎないように調節する
        if (this.playback_mode === 'Live') {
            this.live_force_seek_interval_timer_cancel = Utils.setIntervalInWorker(() => {
                // terminate() 前に main thread へ配送済みの callback もあり得るため、
                // controller 世代と DPlayer identity の両方が一致する場合だけ旧バッファへ触れる。
                if (this.isLifecycleCurrent(lifecycle_generation, setup_player) === false) return;
                if ((setup_player.video.paused && setup_player.video.buffered.length >= 1) &&
                    (setup_player.video.buffered.end(0) - setup_player.video.currentTime > 30)) {
                    setup_player.sync();
                }
            }, 60 * 1000);
        }

        // ビデオ視聴: ビデオストリームのアクティブ状態を維持するために 5 秒おきに Keep-Alive API にリクエストを送る
        // HLS プレイリストやセグメントのリクエストが行われたタイミングでも Keep-Alive が行われるが、
        // それだけではタイミング次第では十分ではないため、定期的に Keep-Alive を行う
        // Keep-Alive が行われなくなったタイミングで、サーバー側で自動的にビデオストリームの終了処理 (エンコードタスクの停止) が行われる
        if (this.playback_mode === 'Video') {
            // View の共有 Store は unmount 時にダミー録画へ reset されるため、この再生世代の録画 ID を固定する。
            const recorded_program_id = player_store.recorded_program.id;
            this.video_keep_alive_interval_timer_cancel = Utils.setIntervalInWorker(async () => {
                // Worker の terminate() 前に配送済みの callback でも、destroy / restart 後は API を呼ばない。
                if (this.isLifecycleCurrent(lifecycle_generation, setup_player) === false) return;

                // 画質切り替えでベース URL が変わることも想定し、あえて毎回 API URL を取得している
                if (setup_player.quality === null) return;
                const api_quality = PlayerUtils.extractVideoAPIQualityFromDPlayer(setup_player);
                const source_url = new URL(setup_player.quality.url);
                const query = source_url.searchParams.toString();
                await APIClient.put(`${Utils.api_base_url}/streams/video/${recorded_program_id}/${api_quality}/keep-alive?${query}`);
            }, 5 * 1000);
        }

        // 再生/停止されたときのイベント
        // デバイスの通知バーからの制御など、ブラウザの画面以外から動画の再生/停止が行われる事もあるため必要
        const on_play_or_pause = () => {
            if (this.player === null) return;
            player_store.is_video_paused = this.player.video.paused;
            // 停止された場合、ロード中でなければ Progress Circular を非表示にする
            if (this.player.video.paused === true && player_store.is_loading === false) {
                player_store.is_video_buffering = false;
            }
            // まだ設定パネルが表示されていたら非表示にする
            this.player.setting.hide();
            // プレイヤーのコントロール UI を表示する
            this.setControlDisplayTimer();
        };
        this.player.on('play', on_play_or_pause);
        this.player.on('pause', on_play_or_pause);

        // 再生が一時的に止まってバッファリングしているとき/再び再生されはじめたときのイベント
        // バッファリングの Progress Circular の表示を制御する
        this.player.on('waiting', () => {
            // Progress Circular を表示する
            player_store.is_video_buffering = true;
            // 自動画質: waiting 開始を記録（モニタが継続時間を見て段階下げ）
            if (
                this.isKonomiTVBS4KAutoQualityModeEnabled() === true &&
                this.auto_quality_waiting_started_at_ms === null
            ) {
                this.auto_quality_waiting_started_at_ms = dayjs().valueOf();
            }
        });
        this.player.on('playing', () => {
            // ロード中 (映像が表示されていない) でなければ Progress Circular を非表示にする
            if (player_store.is_loading === false) {
                player_store.is_video_buffering = false;
            }
            // 自動画質: 初回 playing 到達と waiting 解除
            if (this.isKonomiTVBS4KAutoQualityModeEnabled() === true) {
                this.auto_quality_has_reached_playing = true;
                this.auto_quality_waiting_started_at_ms = null;
            }
            // 完走後に末尾より前へ戻して実際の再生を再開した場合は、再び通常の視聴中として扱う。
            // シーク操作だけでは解除せず、playing まで到達したことをもって「再生を続けた」と判断する。
            if (this.playback_mode === 'Video' && this.player !== null) {
                const video = this.player.video;
                const is_before_end = Number.isFinite(video.duration) &&
                    video.currentTime < video.duration - PlayerController.RECORDED_PLAYBACK_END_TOLERANCE_SECONDS;
                if (is_before_end) {
                    this.recorded_playback_end_blocked_by_seek = false;
                    this.recorded_playback_ended = false;
                }
            }
            // ライブ視聴: 再生が開始できていない場合に再生状態の復旧を試みる
            if (this.playback_mode === 'Live') {
                this.recoverPlayback();
            }
        });

        // 今回 (DPlayer 初期化直後) と画質切り替え開始時の両方のタイミングで実行する必要がある処理
        // mpegts.js などの DPlayer のプラグインは画質切り替え時に一旦破棄されるため、再度イベントハンドラーを登録する必要がある
        const on_init_or_quality_change = async (is_quality_change: boolean = false) => {
            assert(this.player !== null);
            const konomitv_bs4k_quality_change_player = this.player;
            if (
                this.isLifecycleCurrent(
                    lifecycle_generation,
                    konomitv_bs4k_quality_change_player,
                ) === false
            ) return;

            // DPlayer 本体の native error listener はプラグインと異なり画質切り替えで破棄されない。
            // quality_start のたびに呼ばれても、同一 DPlayer / 初期化世代では一度だけ登録する。
            this.setupKonomiTVBS4KNativePlaybackErrorHandler(
                lifecycle_generation,
                konomitv_bs4k_quality_change_player,
            );

            // 画質切り替え時は DPlayer が内蔵字幕レンダラーを再生成するため、再度パッチ済み版へ差し替える。
            if (is_quality_change) {
                this.replaceARIBB24Renderers();
                this.attachARIBTTMLRenderer();
                this.recorded_arib_subtitle_restart?.();
                this.recorded_arib_ttml_restart?.();
            }

            // ローディング中の背景写真をランダムに変更
            player_store.background_url = PlayerUtils.generatePlayerBackgroundURL();

            // 実装上画質切り替え後にそのまま対応できない PlayerManager (LiveDataBroadcastingManager など) をここで再起動する
            // 初回実行時はそもそもまだ PlayerManager が一つも初期化されていないので、何も起こらない
            for (const player_manager of this.player_managers) {
                if (player_manager.restart_required_when_quality_switched === true) {
                    player_manager.destroy().then(() => player_manager.init());  // 非同期で実行
                }
            }

            // ライブ視聴時のみ
            if (this.playback_mode === 'Live') {

                // DPlayer と aribb24.js が購読するものと同じ timed-ID3 event を、KonomiTV の
                // ARIB-TTML 共通デコーダーにも並列で接続する。
                this.attachLiveARIBTTMLStream();

                // mpegts.js のエラーログハンドラーを登録
                // 再生中に mpegts.js 内部でエラーが発生した際 (例: デバイスの通信が一時的に切断され、API からのストリーミングが途切れた際) に呼び出される
                // このエラーハンドラーでエラーをキャッチして、PlayerController の再起動を要求する
                // PlayerController 内部なので直接再起動してもいいのだが、PlayerController を再起動させる処理は共通化しておきたい
                this.player.plugins.mpegts?.on(mpegts.Events.ERROR, async (error_type: string, detail: string) => {

                    // DPlayer がすでに破棄されている場合は何もしない
                    if (this.player === null) {
                        return;
                    }

                    // MSE append / codec decode の失敗時は、保存設定や能力probe結果を変えず、
                    // この PlayerController で一度だけ AVC / AAC へ再初期化する。
                    const is_codec_media_error =
                        error_type === mpegts.ErrorTypes.MEDIA_ERROR &&
                        [
                            mpegts.ErrorDetails.MEDIA_MSE_ERROR,
                            mpegts.ErrorDetails.MEDIA_CODEC_UNSUPPORTED,
                            mpegts.ErrorDetails.MEDIA_FORMAT_UNSUPPORTED,
                        ].includes(detail);
                    if (is_codec_media_error === true) {
                        if (
                            this.requestKonomiTVBS4KCompatibilityPlaybackFallback(`mpegts.js ${error_type}: ${detail}`) === true ||
                            this.isKonomiTVBS4KCompatibilityPlaybackFallbackPending() === true
                        ) return;
                    }

                    // すぐ再起動すると問題があるケースがあるので、少し待機する
                    await Utils.sleep(1);

                    // もしこの時点でオフラインの場合、ネットワーク接続の変更による接続切断の可能性が高いので、オンラインになるまで待機する
                    if (navigator.onLine === false) {
                        this.player.notice('現在ネットワーク接続がありません。オンラインになるまで待機しています…', undefined, undefined, 'rgb(var(--v-theme-error-readable))');
                        console.warn('\u001b[31m[PlayerController] mpegts.js error event: Network error. Waiting for online...');
                        await Utils.waitUntilOnline();
                    }

                    // PlayerController の再起動を要求する
                    console.error('\u001b[31m[PlayerController] mpegts.js error event:', error_type, detail);
                    player_store.event_emitter.emit('PlayerRestartRequired', {
                        message: `再生中にエラーが発生しました。(${error_type}: ${detail}) プレイヤーを再起動しています…`,
                    });
                });

                // 必ず最初はローディング状態とする
                player_store.is_loading = true;

                // DPlayer のスマホ向け UI ではミュート解除用の音量ボタンがないため、PC 向け UI の保存値だけ参照する
                const should_keep_muted_after_live_startup =
                    this.player.container.classList.contains('dplayer-mobile') === false &&
                    localStorage.getItem('dplayer-is-muted') === 'true';

                // 再生準備中の音声を出さないため、一時的にミュートする
                // 保存済みミュートと区別し、volumechange 側で保存値を上書きしないようにする
                this.muteLiveStartupVideo(this.player.video);

                // この時点で HTMLVideoElement.paused が true のとき、再生できるようになるまで 0.05 秒間を開けて 5 回試す
                if (this.player.video.paused === true) {
                    let attempts = 0;
                    const maxAttempts = 5;  // 試行回数
                    const attemptInterval = 0.05;  // 試行間隔 (秒)
                    const attemptPlay = async (): Promise<void> => {
                        if (attempts >= maxAttempts) {
                            console.warn(`\u001b[31m[PlayerController] Failed to start playback after ${maxAttempts} attempts.`);
                            return;
                        }
                        try {
                            await this.player?.video.play();
                            console.log('\u001b[31m[PlayerController] Playback started successfully.');
                        } catch (error) {
                            console.warn(`\u001b[31m[PlayerController] Attempt ${attempts + 1} to start playback failed:`, error);
                            attempts++;
                            await Utils.sleep(attemptInterval);
                            await attemptPlay();
                        }
                    };
                    await attemptPlay();
                }

                // 再生準備ができた段階で再生バッファを調整し、再生準備ができた段階でローディング中の背景写真を非表示にするイベントハンドラーを登録
                let on_canplay_called = false;
                const canplay_player = this.player;
                const canplay_signal = this.lifecycle_abort_controller.signal;
                const on_canplay = async () => {

                    // 重複実行を回避する
                    if (this.isLifecycleCurrent(lifecycle_generation, canplay_player) === false) return;
                    if (on_canplay_called === true) return;
                    canplay_player.video.oncanplay = null;
                    canplay_player.video.oncanplaythrough = null;
                    on_canplay_called = true;

                    // 再生バッファ調整のため、一旦停止させる
                    // this.player.video.pause() を使うとプレイヤーの UI アイコンが停止してしまうので、代わりに playbackRate を使う
                    console.log('\u001b[31m[PlayerController] Buffering...');
                    canplay_player.video.playbackRate = 0;

                    // 再生バッファが target_buffer_seconds を超えるまで 0.1 秒おきに再生バッファをチェックする
                    // 再生バッファが target_buffer_seconds を切ると再生が途切れやすくなるので (特に動きの激しい映像)、
                    // 再生開始までの時間を若干犠牲にして、再生バッファの調整と同期に時間を割く
                    // target_buffer_seconds は DPlayer と mpegts.js に渡したものと同じセッション固定値
                    if (await this.waitForLivePlaybackBuffer(
                        canplay_player,
                        lifecycle_generation,
                        live_playback_policy.target_buffer_seconds,
                        canplay_signal,
                    ) === false) return;

                    // FFmpeg は ONAir 直後に入力パイプへ蓄積した TS を実時間より速く出力する場合がある。
                    // canplay の瞬間だけで判定すると、その直後にバッファが増えて1.1倍速追従が始まるため、
                    // 低遅延モードだけローディング画面内で1秒間待ち、起動直後の出力を先に受け取る。
                    // 受信済み範囲内を目標バッファ位置まで一度だけ進めるので、表示開始時の遅延は増えない。
                    if (live_playback_policy.live_sync_enabled === true) {
                        await Utils.sleep(1, canplay_signal);
                        if (this.isLifecycleCurrent(lifecycle_generation, canplay_player) === false) return;
                    }

                    // 初期化リトライや放送局ごとの PMT 到着差で、表示開始前のバッファが積み上がることがある。
                    // 60fps 映像を1.1倍速で長時間追従すると描画可能フレーム数を超えてカクつくため、
                    // ローディング画面を解除する前に一度だけ受信済み範囲内を移動する。
                    // mpegts.js の継続的な liveBufferLatencyChasing は視聴中も直接シークする非推奨機能なので使わない。
                    const buffered_range_count = canplay_player.video.buffered.length;
                    if (buffered_range_count > 0) {
                        const buffered_range_index = buffered_range_count - 1;
                        const initial_playback_position = resolveInitialLivePlaybackPositionSeconds(
                            live_playback_policy,
                            canplay_player.video.buffered.start(buffered_range_index),
                            canplay_player.video.buffered.end(buffered_range_index),
                            canplay_player.video.currentTime,
                        );
                        if (initial_playback_position !== null) {
                            console.log(
                                '\u001b[31m[PlayerController] Adjusting the initial live playback position. ' +
                                `(current=${canplay_player.video.currentTime.toFixed(3)}, ` +
                                `target=${initial_playback_position.toFixed(3)})`,
                            );
                            canplay_player.video.currentTime = initial_playback_position;
                        }
                    }

                    // 再生バッファ調整のため一旦停止していた再生を再び開始
                    canplay_player.video.playbackRate = 1;
                    console.log('\u001b[31m[PlayerController] Buffering completed.');

                    // ローディング状態を解除し、映像を表示する
                    player_store.is_loading = false;

                    // バッファリング中の Progress Circular を非表示にする
                    player_store.is_video_buffering = false;

                    // この時点で再生が開始できていない場合、再生状態の復旧を試みる
                    this.recoverPlayback();

                    if (channels_store.channel.current.is_radiochannel === true) {
                        // ラジオチャンネルでは引き続き映像の代わりとしてローディング中の背景写真を表示し続ける
                        player_store.is_background_display = true;
                    } else {
                        // ローディング中の背景写真をフェードアウト
                        player_store.is_background_display = false;
                    }

                    // ユーザーがミュートを保存している場合は、再生開始時のフェードインでミュートを解除しない
                    if (should_keep_muted_after_live_startup === true) {
                        this.is_live_startup_temporary_muted = false;
                    } else {
                        // ミュート中でない場合だけフェードインする (いきなり再生されるよりも体験が良い)
                        // 開始音量を 0 に下げてから、保存されている音量まで徐々に上げる
                        // フェード中は Startup 一時ミュートの所有を維持する。
                        // DPlayer は volume=0 の volumechange で video.muted を true に戻すことがあるため、
                        // 先に所有を解除すると一時状態がユーザーの保存値として永続化されてしまう。
                        canplay_player.video.volume = 0;
                        // 0.5 秒間かけて 0 から current_volume まで音量を上げる
                        const current_volume = canplay_player.user.get('volume');  // 0.0 ~ 1.0 の範囲
                        const volume_step = current_volume / 10;
                        for (let i = 0; i < 10; i++) {  // 10 回に分けて音量を上げる
                            await Utils.sleep(0.5 / 10, canplay_signal);
                            if (this.isLifecycleCurrent(lifecycle_generation, canplay_player) === false) return;
                            // 音量が current_volume を超えないようにする
                            // 浮動小数点絡みの問題 (丸め誤差) が出るため小数第3位で切り捨てる
                            canplay_player.video.volume = Math.min(
                                Utils.mathFloor(canplay_player.video.volume + volume_step, 3),
                                current_volume,
                            );
                        }
                        // 最後に current_volume に設定し直す
                        // 上記ロジックでは丸め誤差の関係で完全に current_volume とは一致しないことがあるため
                        canplay_player.video.volume = current_volume;
                        // 保存音量へ戻してから最後にミュートを解除する。
                        // 遅延した volumechange が発火しても、その時点の muted=false だけが保存される。
                        this.releaseLiveStartupVideoMute(canplay_player.video);
                    }
                };
                canplay_player.video.oncanplay = on_canplay;
                canplay_player.video.oncanplaythrough = on_canplay;

                // 万が一 canplay(through) が発火しなかった場合のために (ほぼ Safari 向け) 、
                // mpegts.js 側でメディア情報が取得できたタイミングでも再生開始を試みる
                // 特に Safari 18 以降では MSE の canplay(through) が場合によっては発火しなかったり、発火が異常に遅かったりする…
                // Safari 18 以降、MSE において canplay(through) の発火タイミングと readyState の値は信頼できない
                canplay_player.plugins.mpegts?.on(mpegts.Events.MEDIA_INFO, async (info: {[key: string]: any}) => {
                    if (this.isLifecycleCurrent(lifecycle_generation, canplay_player) === false) return;
                    console.log('\u001b[31m[PlayerController] mpegts.js media info:', info);
                    this.live_media_info = info;
                    this.applyAudioTrackLabels(info);
                    // 一応ブラウザネイティブの canplay(through) を優先したいので、0.25 秒待ってから再生開始を試みる
                    // 既に再生開始処理を実行済みの場合は実行しない
                    await Utils.sleep(0.25, canplay_signal);
                    if (
                        this.isLifecycleCurrent(lifecycle_generation, canplay_player) &&
                        on_canplay_called === false &&
                        this.isMediaReadyToStartPlayback(canplay_player.video) === true
                    ) {
                        console.info('[PlayerController] Native canplay event was omitted; starting from confirmed media readiness.');
                        void on_canplay();
                    }
                });

                this.setupLiveAudioTrackMonitor();

                // 万が一 canplay(through) が発火しなかった場合のために (ほぼ Safari 向け) 、
                // 非同期で 0.05 秒おきに readyState と実バッファ範囲を確認する
                // ほとんどのケースでは 先に上記 mpegts.js の MEDIA_INFO イベントが発火するため、この処理は実行されない
                (async () => {
                    while (
                        this.isLifecycleCurrent(lifecycle_generation, canplay_player) &&
                        this.isMediaReadyToStartPlayback(canplay_player.video) === false
                    ) {
                        await Utils.sleep(0.05, canplay_signal);
                    }
                    // HAVE_FUTURE_DATA または currentTime より先の実バッファを確認できたら、再生開始を試みる
                    // 既に再生開始処理を実行済みの場合は実行しない
                    await Utils.sleep(0.1, canplay_signal);
                    if (
                        this.isLifecycleCurrent(lifecycle_generation, canplay_player) &&
                        on_canplay_called === false
                    ) {
                        console.info('[PlayerController] Native canplay event was omitted; starting from confirmed media readiness.');
                        void on_canplay();
                    }
                })();

                // もしライブストリームのステータスが ONAir にも関わらず 15 秒以上バッファリング中で canplaythrough が発火しない場合、
                // ロードに失敗したとみなし PlayerController の再起動を要求する
                await Utils.sleep(15, canplay_signal);
                if (this.isLifecycleCurrent(lifecycle_generation, canplay_player) === false) return;
                if (player_store.live_stream_status === 'ONAir' && player_store.is_video_buffering === true && on_canplay_called === false) {
                    player_store.event_emitter.emit('PlayerRestartRequired', {
                        message: '再生開始までに時間が掛かっています。プレイヤーを再起動しています…',
                    });
                }

            // ビデオ視聴のみ
            } else {

                // hls.js の初期化時に startPosition を指定したことで、シーク時に常に startPosition に対応する HLS セグメントが
                // ロードされるようになってしまうため、画質切り替えが完了する前に startPosition をデフォルト値の -1 に無理やり戻す
                // こうすることで startPosition を指定しつつ、シーク時は従来通りシーク先のセグメントから先読みが開始されるようになる
                const hls_plugin = this.player.plugins.hls;
                if (hls_plugin !== undefined) {
                    const resetStartPosition = () => {
                        hls_plugin.off(Hls.Events.FRAG_BUFFERED, resetStartPosition);
                        hls_plugin.config.startPosition = -1;
                        const internal_hls = hls_plugin as unknown as {
                            streamController?: {
                                startPosition?: number;
                                nextLoadPosition?: number;
                            };
                        };
                        if (internal_hls.streamController) {
                            internal_hls.streamController.startPosition = -1;
                            if (hls_plugin.media) {
                                internal_hls.streamController.nextLoadPosition = hls_plugin.media.currentTime;
                            }
                        }
                    };
                    hls_plugin.on(Hls.Events.FRAG_BUFFERED, resetStartPosition);
                } else {
                    console.error('\u001b[31m[PlayerController] hls.js plugin not found. Recorded playback requires MSE / ManagedMediaSource.');
                    this.player.video.pause();
                    this.player.notice(
                        'このブラウザは録画再生に必要な Media Source Extensions に対応していません。',
                        -1, undefined, 'rgb(var(--v-theme-warning-readable))',
                    );
                }

                // 必ず最初はローディング状態で、背景写真を表示する
                player_store.is_loading = true;
                player_store.is_background_display = true;

                // 再生準備ができた段階でローディング中の背景写真を非表示にするイベントハンドラーを登録
                let on_canplay_called = false;
                const on_canplay = async () => {

                    // 重複実行を回避する
                    if (this.player === null) return;
                    if (on_canplay_called === true) return;
                    this.player.video.oncanplaythrough = null;
                    on_canplay_called = true;

                    // ローディング状態を解除し、映像を表示する
                    player_store.is_loading = false;

                    // バッファリング中の Progress Circular を非表示にする
                    player_store.is_video_buffering = false;

                    // ローディング中の背景写真をフェードアウト
                    player_store.is_background_display = false;
                };
                this.player.video.oncanplaythrough = on_canplay;

            }
        };

        // 初回実行
        on_init_or_quality_change();

        // 画質切り替え開始時のイベント。DPlayer が渡す切替先の表示画質を API 画質へ戻して再検査し、
        // guard を迂回した非対応切替では新しい video / plugin を継続利用しない。
        this.player.on(
            'quality_start',
            (konomitv_bs4k_target_quality?: DPlayerType.VideoQuality) => {
                if (
                    konomitv_bs4k_target_quality === undefined ||
                this.player === null ||
                this.handleKonomiTVBS4KPlaybackQualityStart(
                    lifecycle_generation,
                    this.player,
                    konomitv_bs4k_target_quality,
                ) === false
                ) {
                    return;
                }
                void on_init_or_quality_change(true);
            },
        );

        // 動画の統計情報の表示/非表示を切り替える隠しコマンドのイベントハンドラーを登録
        // iOS / iPadOS Safari では DPlayer 側の contextmenu が長押ししても発火しないため、代替の表示手段として用意
        // 番組情報タブ内の NEXT >> を 500ms 以内に3回連続でタップすると統計情報の表示/非表示が切り替わる
        // イベントを重複定義しないように、あえて ontouchstart を使う
        let tap_count = 0;
        let last_tap = 0;
        const element = document.querySelector<HTMLDivElement>('.program-info__next');
        if (element !== null) {
            element.ontouchstart = () => {
                if (this.player === null) return;
                const current_time = new Date().getTime();
                const time_difference = current_time - last_tap;
                if (time_difference < 500 && time_difference > 0) {
                    tap_count++;
                    if (tap_count === 3) {
                        this.player.infoPanel.toggle();
                        tap_count = 0;
                    }
                }
                last_tap = current_time;
            };
        }

        // ビデオ視聴時のみ実行する処理
        if (this.playback_mode === 'Video') {

            // シークで直接末尾へ移動した場合は、HTMLMediaElement が ended を発火しても次話へ進めない。
            // CM 自動スキップのシークは自然再生の続きなので除外する。
            this.player.on('seeking', () => {
                if (this.player === null) return;
                const cm_skip_target = this.recorded_auto_skip_cm_target;
                const is_auto_skip_cm_seek = cm_skip_target !== null &&
                    Number.isFinite(this.player.video.currentTime) &&
                    Math.abs(this.player.video.currentTime - cm_skip_target) <=
                        PlayerController.RECORDED_CM_SKIP_TARGET_TOLERANCE_SECONDS;
                if (is_auto_skip_cm_seek === false) {
                    this.recorded_auto_skip_cm_target = null;
                    this.recorded_playback_end_blocked_by_seek = true;
                }
            });

            // バッファ済み範囲へのシークでは playing が再発火しないことがあるため、seeked でも解除を判断する。
            // 末尾へ直接移動した通常シークだけはブロックを維持し、CM 自動スキップは末尾でも完走を許可する。
            this.player.on('seeked', () => {
                if (this.player === null) return;
                const video = this.player.video;
                const cm_skip_target = this.recorded_auto_skip_cm_target;
                const completed_auto_skip_cm_seek = cm_skip_target !== null &&
                    Number.isFinite(video.currentTime) &&
                    Math.abs(video.currentTime - cm_skip_target) <=
                        PlayerController.RECORDED_CM_SKIP_TARGET_TOLERANCE_SECONDS;
                const is_before_end = Number.isFinite(video.duration) &&
                    video.currentTime < video.duration - PlayerController.RECORDED_PLAYBACK_END_TOLERANCE_SECONDS;
                if (completed_auto_skip_cm_seek || is_before_end) {
                    this.recorded_playback_end_blocked_by_seek = false;
                    this.recorded_playback_ended = false;
                }
                this.recorded_auto_skip_cm_target = null;
            });

            // DPlayer 自身のループを無効にした通常再生だけ、録画の自然な完走を View へ通知する。
            // DPlayer の保存済みループ設定が有効なら同一録画の再生を優先し、次話へは進めない。
            const recorded_program_id = player_store.recorded_program.id;
            this.player.on('ended', () => {
                if (this.destroyed || this.destroying || this.player === null) return;
                if (this.player.setting.loop === true) return;
                if (
                    this.player.video.ended === false ||
                    this.player.video.seeking === true ||
                    this.recorded_playback_end_blocked_by_seek === true ||
                    this.recorded_playback_ended === true
                ) {
                    return;
                }
                this.recorded_playback_ended = true;
                player_store.event_emitter.emit('RecordedPlaybackEnded', {recorded_program_id});
            });

            // 再生位置の変更（再生の進行状況）を Comment.vue にイベントとして通知する
            this.player.on('timeupdate', () => {
                if (!this.player || !this.player.video) {
                    return;
                }
                player_store.event_emitter.emit('PlaybackPositionChanged', {
                    playback_position: this.player.video.currentTime,
                });
            });

            // 視聴履歴の更新処理
            // timeupdate イベントを間引いて処理
            // ここで登録したイベントは、destroy() を実行した際にプレイヤーごと破棄される
            let last_timeupdate_fired_at = 0;
            this.player.on('timeupdate', () => {
                if (!this.player || !this.player.video) {
                    return;
                }
                // 前回 timeupdate イベントが発火した時刻から WATCHED_HISTORY_UPDATE_INTERVAL 秒間は処理を実行しない（間引く）
                const now = new Date().getTime();
                if (now - last_timeupdate_fired_at < PlayerController.WATCHED_HISTORY_UPDATE_INTERVAL * 1000) {
                    return;
                }
                last_timeupdate_fired_at = now;
                const current_time = this.player.video.currentTime;
                const video_id = player_store.recorded_program.id;
                const history_index = settings_store.settings.watched_history.findIndex(
                    history => history.video_id === video_id
                );
                // 視聴履歴が既に登録されている場合のみ、現在の再生位置を更新
                if (history_index !== -1) {
                    settings_store.settings.watched_history[history_index].last_playback_position = current_time;
                    settings_store.settings.watched_history[history_index].updated_at = Utils.time();
                    console.log(`\u001b[31m[PlayerController] Last playback position updated. (Video ID: ${video_id}, last_playback_position: ${current_time})`);
                }
            });

            // 視聴開始から WATCHED_HISTORY_THRESHOLD_SECONDS 秒間このページが開かれ続けていたら、視聴履歴に追加する
            this.watched_history_threshold_timer_id = window.setTimeout(() => {
                if (!this.player || !this.player.video) {
                    return;
                }
                const video_id = player_store.recorded_program.id;
                const history_index = settings_store.settings.watched_history.findIndex(
                    history => history.video_id === video_id
                );
                // まだ視聴履歴に存在しない場合のみ追加
                if (history_index === -1) {
                    // 視聴履歴が最大件数に達している場合は、最も古い履歴を削除
                    if (settings_store.settings.watched_history.length >= settings_store.settings.video_watched_history_max_count) {
                        // 最も古い created_at のタイムスタンプを持つ履歴のインデックスを探す
                        const oldest_index = settings_store.settings.watched_history.reduce((oldest_idx, current, idx, arr) => {
                            return current.created_at < arr[oldest_idx].created_at ? idx : oldest_idx;
                        }, 0);
                        // 最も古い履歴を削除
                        settings_store.settings.watched_history.splice(oldest_index, 1);
                    }
                    settings_store.settings.watched_history.push({
                        video_id: video_id,
                        last_playback_position: this.player.video.currentTime,
                        created_at: Utils.time(),  // 秒単位
                        updated_at: Utils.time(),  // 秒単位
                    });
                    console.log(`\u001b[31m[PlayerController] Watched history added. (Video ID: ${video_id}, last_playback_position: ${this.player.video.currentTime})`);
                }
            }, PlayerController.WATCHED_HISTORY_THRESHOLD_SECONDS * 1000);
        }
    }

    /**
     * ライブ Startup 中のミュートをユーザー操作と区別して所有する。
     * すでに Coordinator が video をミュート済みでもフラグを必ず立て、
     * 遅延した volumechange が保存済みミュート値を上書きしないようにする。
     */
    private muteLiveStartupVideo(video: HTMLVideoElement): void {
        this.is_live_startup_temporary_muted = true;
        video.muted = true;
    }

    /**
     * 保存音量へ戻した後、Startup 一時ミュートを最後に解除する。
     * muted=false の代入時点までは所有フラグを維持し、同期的な volumechange も保存対象から除外する。
     */
    private releaseLiveStartupVideoMute(video: HTMLVideoElement): void {
        video.muted = false;
        this.is_live_startup_temporary_muted = false;
    }


    /**
     * DPlayer のフルスクリーン関係のメソッドを無理やり上書きし、KonomiTV の UI と統合する
     * 上書き元のコードは https://github.com/tsukumijima/DPlayer/blob/master/src/ts/fullscreen.ts にある
     */
    private setupFullscreenHandler(): void {
        assert(this.player !== null);
        const player_store = usePlayerStore();

        // フルスクリーンにするコンテナ要素 (ページ全体)
        const fullscreen_container = document.body;

        // フルスクリーンかどうか
        this.player.fullScreen.isFullScreen = (type?: DPlayerType.FullscreenType) => {
            return !!(document.fullscreenElement || document.webkitFullscreenElement);
        };

        // フルスクリーンをリクエスト
        this.player.fullScreen.request = (type?: DPlayerType.FullscreenType) => {
            assert(this.player !== null);
            // すでにフルスクリーンだったらキャンセルする
            if (this.player.fullScreen.isFullScreen()) {
                this.player.fullScreen.cancel();
                return;
            }
            // フルスクリーンをリクエスト
            // Safari は webkit のベンダープレフィックスが必要
            fullscreen_container.requestFullscreen = fullscreen_container.requestFullscreen || fullscreen_container.webkitRequestFullscreen;
            if (fullscreen_container.requestFullscreen) {
                fullscreen_container.requestFullscreen();
            } else {
                // フルスクリーンがサポートされていない場合はエラーを表示
                this.player.notice('iPhone Safari は動画のフルスクリーン表示に対応していません。', undefined, undefined, 'rgb(var(--v-theme-error-readable))');
                return;
            }
            // 画面の向きを横に固定 (Screen Orientation API がサポートされている場合)
            if (screen.orientation) {
                screen.orientation.lock('landscape').catch(() => {});
            }
        };

        // フルスクリーンをキャンセル
        this.player.fullScreen.cancel = (type?: DPlayerType.FullscreenType) => {
            // フルスクリーンを終了
            // Safari は webkit のベンダープレフィックスが必要
            document.exitFullscreen = document.exitFullscreen || document.webkitExitFullscreen;
            if (document.exitFullscreen) {
                document.exitFullscreen();
            }
            // 画面の向きの固定を解除
            if (screen.orientation) {
                screen.orientation.unlock();
            }
        };

        // フルスクリーン状態が変化した時のイベントハンドラーを登録
        // 複数のイベントを重複登録しないよう、あえて onfullscreenchange を使う
        const fullscreen_handler = () => {
            assert(this.player !== null);
            player_store.is_fullscreen = this.player.fullScreen.isFullScreen() === true;
        };
        if (fullscreen_container.onfullscreenchange !== undefined) {
            fullscreen_container.onfullscreenchange = fullscreen_handler;
        } else if (fullscreen_container.onwebkitfullscreenchange !== undefined) {
            fullscreen_container.onwebkitfullscreenchange = fullscreen_handler;
        }
    }


    /**
     * DPlayer の音声トラック表示を、元放送/録画メタデータ由来の表記に差し替える
     */
    private applyAudioTrackLabels(media_info: {[key: string]: any} | null = null): void {
        if (this.player === null) return;

        const labels = this.buildAudioTrackLabels(media_info);
        const audio_track_count = this.getSelectableAudioTrackCount(media_info, labels);
        const audio_items = Array.from(this.player.container.querySelectorAll<HTMLElement>('.dplayer-setting-audio-item'));

        // DPlayer は音声項目を主・副の2件に固定しているため、ライブTSのPMTに3本以上の音声PIDがある場合は
        // 実トラック数に合わせて項目を増減する。録画側はHLSレンディション同期処理で同様に管理する。
        if (this.playback_mode === 'Live' && audio_items.length > 0) {
            const audio_panel = audio_items[0].parentElement;
            while (audio_panel !== null && audio_items.length < Math.max(audio_track_count, 1)) {
                const index = audio_items.length;
                const audio_item = audio_items[0].cloneNode(true) as HTMLElement;
                audio_item.classList.remove('dplayer-setting-audio-current');
                audio_item.dataset.audio = `track-${index}`;
                delete audio_item.dataset.konomitvBs4kAudioLabelHandlerBound;
                audio_panel.appendChild(audio_item);
                audio_items.push(audio_item);
            }
            while (audio_items.length > Math.max(audio_track_count, 1) && audio_items.length > 1) {
                audio_items.pop()?.remove();
            }
            audio_items.forEach((audio_item, index) => {
                audio_item.dataset.audio = `track-${index}`;
            });
        }
        const setting_box = this.player.container.querySelector<HTMLElement>('.dplayer-setting-box');
        const audio_setting_item = this.player.container.querySelector<HTMLElement>('.dplayer-setting-audio');
        const audio_setting_value = audio_setting_item?.querySelector<HTMLElement>('.dplayer-label-value') ?? null;
        const current_audio_item = this.player.container.querySelector<HTMLElement>('.dplayer-setting-audio-current');
        const current_audio_index = current_audio_item !== null ? audio_items.indexOf(current_audio_item) : 0;
        const is_live_audio_known_absent = this.playback_mode === 'Live' && media_info?.hasAudio === false;
        const current_audio_label = audio_track_count === 0 ?
            (this.playback_mode === 'Live' && is_live_audio_known_absent === false ? '音声不明' : '音声なし') :
            labels[current_audio_index >= 0 && current_audio_index < audio_track_count ? current_audio_index : 0] ??
                'Track1 音声不明';

        if (audio_setting_value !== null) {
            audio_setting_value.textContent = current_audio_label;
        }

        if (audio_setting_item !== null) {
            // 音声が不明・存在しない場合も、状態表示用のサブメニューを開けるようにする
            const has_audio_menu = audio_items.length > 0;
            audio_setting_item.classList.toggle('dplayer-setting-audio--disabled', has_audio_menu === false);
            audio_setting_item.setAttribute('aria-disabled', String(has_audio_menu === false));
        }

        // 非表示にした音声項目の分だけサブメニュー上部に空白が残らないよう、表示高さを実トラック数に同期する
        // 音声が不明・存在しない場合は、状態表示用に1行分を確保する
        const visible_audio_item_count = Math.max(audio_track_count, 1);
        setting_box?.style.setProperty('--audio-panel-height', `${visible_audio_item_count * 30 + 54}px`);

        if (audio_items.length === 0) return;

        audio_items.forEach((audio_item, index) => {
            const is_audio_status_item = audio_track_count === 0 && index === 0;
            const label = is_audio_status_item ? current_audio_label : labels[index] ?? `Track${index + 1} 音声不明`;
            const label_element = audio_item.querySelector<HTMLElement>('.dplayer-label') ?? audio_item;
            label_element.textContent = label;
            if (audio_item.dataset.konomitvBs4kAudioLabelHandlerBound !== 'true') {
                audio_item.dataset.konomitvBs4kAudioLabelHandlerBound = 'true';
                audio_item.addEventListener('click', () => {
                    window.setTimeout(() => this.applyAudioTrackLabels(media_info), 0);
                });
            }
            if (this.playback_mode === 'Live' && audio_item.dataset.konomitvBs4kAudioSwitchHandlerBound !== 'true') {
                audio_item.dataset.konomitvBs4kAudioSwitchHandlerBound = 'true';
                audio_item.addEventListener('click', () => {
                    if (audio_item.classList.contains('dplayer-setting-audio-item--disabled')) return;
                    const mpegts_player = this.player?.plugins.mpegts as any;
                    if (typeof mpegts_player?.switchAudioTrack !== 'function') return;
                    mpegts_player.switchAudioTrack(index);
                    this.live_selected_audio_track_index = index;
                    audio_items.forEach((item, item_index) => {
                        item.classList.toggle('dplayer-setting-audio-current', item_index === index);
                    });
                    if (audio_setting_value !== null) {
                        audio_setting_value.textContent = labels[index] ?? `Track${index + 1} 音声不明`;
                    }
                    setting_box?.classList.remove('dplayer-setting-box-audio');
                });
            }
            const is_audio_item_unavailable = audio_track_count === 0 || index >= audio_track_count;
            audio_item.classList.toggle('dplayer-setting-audio-item--disabled', is_audio_item_unavailable);
            audio_item.classList.toggle('dplayer-setting-audio-item--status', is_audio_status_item);
            audio_item.setAttribute('aria-disabled', String(is_audio_item_unavailable));
        });

        this.ensureLiveAudioTrackSelection(media_info);

        if (audio_track_count > 0 && audio_items.length !== audio_track_count) {
            console.warn(
                '\u001b[31m[PlayerController] Audio track count mismatch. ' +
                `(player=${audio_items.length}, metadata=${audio_track_count})`
            );
        }
    }


    /**
     * 現在選択中の音声トラック番号を取得する
     */
    private getCurrentAudioTrackIndex(): number {

        if (this.player === null) return 0;
        if (this.playback_mode === 'Live') return this.live_selected_audio_track_index;

        const audio_items = Array.from(this.player.container.querySelectorAll<HTMLElement>('.dplayer-setting-audio-item'));
        const current_audio_item = this.player.container.querySelector<HTMLElement>('.dplayer-setting-audio-current');
        return current_audio_item !== null ? audio_items.indexOf(current_audio_item) : 0;
    }


    /**
     * DPlayer の音声トラックを切り替える
     */
    private switchAudioTrackTo(index: number): boolean {

        if (this.player === null) return false;

        const audio_items = Array.from(this.player.container.querySelectorAll<HTMLElement>('.dplayer-setting-audio-item'));
        const audio_item = audio_items[index] ?? null;
        if (audio_item === null) return false;
        if (audio_item.classList.contains('dplayer-setting-audio-item--disabled')) return false;

        audio_item.click();
        return true;
    }


    /**
     * ライブ視聴で選択中の音声トラックが消えた場合、Track1 に自動復帰する
     */
    private ensureLiveAudioTrackSelection(media_info: {[key: string]: any} | null): void {

        if (this.playback_mode !== 'Live' || this.player === null) return;

        const audio_track_count = this.getLiveSelectableAudioTrackCount(media_info);
        if (audio_track_count <= 0) return;

        const current_audio_index = this.getCurrentAudioTrackIndex();
        if (current_audio_index >= 0 && current_audio_index < audio_track_count) return;
        if (this.switchAudioTrackTo(0) === true) {
            this.live_selected_audio_track_index = 0;
            console.warn(
                '\u001b[31m[PlayerController] Selected live audio track is no longer available. ' +
                `Fallback to Track1. (selected=${current_audio_index + 1}, tracks=${audio_track_count})`
            );
            window.setTimeout(() => this.applyAudioTrackLabels(media_info), 0);
        }
    }


    /**
     * mpegts.js が解析した配信 TS の PMT から、実在する音声 PID 数を取得する
     */
    private getLiveTSAudioTrackCount(media_info: {[key: string]: any} | null): number {

        if (this.playback_mode !== 'Live' || this.player === null) return 0;

        // mpegts.js の Worker から MediaInfo 経由で通知された音声 PID 数を最優先で使う。
        const media_info_audio_track_count = media_info?.audioTrackCount;
        if (typeof media_info_audio_track_count === 'number' &&
            Number.isInteger(media_info_audio_track_count) && media_info_audio_track_count >= 0) {
            return media_info_audio_track_count;
        }

        // KonomiTV の mpegts.js は DPlayer の主音声・副音声切替用に全音声 PID を PMT 内へ保持している。
        // mediaInfo.audioTracks や HTMLMediaElement.audioTracks は音声 PID 数ではないため使用しない。
        const mpegts_player = this.player.plugins.mpegts as any;
        const demuxer = mpegts_player?._player_engine?._transmuxer?._controller?._demuxer;
        const program_pmt_map = demuxer?.program_pmt_map_;
        if (program_pmt_map !== null && typeof program_pmt_map === 'object') {
            const current_program = demuxer.current_program_;
            const current_pmt = program_pmt_map[current_program] ?? Object.values(program_pmt_map)[0];
            const pid_stream_type = (current_pmt as any)?.pid_stream_type;
            if (pid_stream_type !== null && typeof pid_stream_type === 'object') {
                // MPEG-1/2 Audio, AAC (ADTS/LOAS), AC-3, E-AC-3
                const audio_stream_types = new Set([0x03, 0x04, 0x0F, 0x11, 0x81, 0x87]);
                return Object.values(pid_stream_type).filter((stream_type) => {
                    return typeof stream_type === 'number' && audio_stream_types.has(stream_type);
                }).length;
            }
        }

        // 旧版の mpegts.js や PMT をまだ参照できない場合は、mediaInfo の音声有無だけを使う。
        // audioChannelCount は1音声ストリーム内のチャンネル数であり、音声トラック数ではない。
        return media_info?.hasAudio === true || Number(media_info?.audioChannelCount) > 0 ? 1 : 0;
    }


    /**
     * 配信 TS と現在番組の音声構成から、ライブ視聴で実際に選択可能な音声トラック数を取得する
     */
    private getLiveSelectableAudioTrackCount(media_info: {[key: string]: any} | null): number {
        // EPG は放送中の実ストリームと一致しない場合があるため、選択可否には一切使わない。
        // mpegts.js がPMTから数えた音声PID数だけを正とする。
        return this.getLiveTSAudioTrackCount(media_info);
    }


    /**
     * ライブ視聴中の現在の音声トラック情報を取得する
     */
    private getCurrentLiveAudioTrackMediaInfo(): {[key: string]: any} | null {

        if (this.playback_mode !== 'Live' || this.player === null) return null;

        const mpegts_player = this.player.plugins.mpegts as any;
        // Prepare lease を取得できない下降では、旧 Active の mpegts.js を先に破棄してから
        // 同じ video 要素で下位品質を起動する。候補を adopt するまで DPlayer の plugin 参照は
        // 旧 MSEPlayer を指すため、destroy() 済みの null engine へ mediaInfo を問い合わせない。
        if (mpegts_player?._player_engine === null) return this.live_media_info;
        const mpegts_media_info = mpegts_player?.mediaInfo;
        if (Array.isArray(mpegts_media_info?.audioTracks)) {
            this.live_media_info = mpegts_media_info;
            return mpegts_media_info;
        }

        const native_audio_tracks = (this.player.video as HTMLVideoElement & {audioTracks?: any}).audioTracks;
        if (native_audio_tracks !== undefined && typeof native_audio_tracks.length === 'number' && native_audio_tracks.length > 0) {
            const audio_tracks = Array.from({length: native_audio_tracks.length}, (_, index) => {
                const track = native_audio_tracks[index];
                const label = typeof track?.label === 'string' && track.label.length > 0 ?
                    track.label : `Track${index + 1} 音声不明`;
                const language = typeof track?.language === 'string' && track.language.length > 0 ? track.language : undefined;
                return language !== undefined ? {name: label, language} : {name: label};
            });
            const media_info = {...(this.live_media_info ?? {}), audioTracks: audio_tracks};
            this.live_media_info = media_info;
            return media_info;
        }

        return this.live_media_info;
    }

    /** 現在の再生がTS Codec Bridgeを必要とする高度codecの組み合わせか返す。 */
    private isKonomiTVBS4KAdvancedPlaybackProfileActive(): boolean {
        return (
            this.konomitv_bs4k_playback_video_codec_for_current_playback === 'vp9' ||
            this.konomitv_bs4k_playback_video_codec_for_current_playback === 'av1' ||
            this.konomitv_bs4k_playback_audio_codec_for_current_playback === 'opus'
        );
    }


    /**
     * ライブSSEが通知したサーバー側失敗のうち、高度codec固有のものだけを互換fallbackへ渡す。
     *
     * チューナー不足・放送休止・受信断・一般ネットワーク障害は従来の再起動/Offline表示へ残し、
     * Bridge名または能力判定の理由コードが明示された場合だけ今回の再生をAVC/AACへ切り替える。
     */
    private handleKonomiTVBS4KLivePlaybackPipelineStatus(
        konomitv_bs4k_status: 'Offline' | 'Standby' | 'ONAir' | 'Idling' | 'Restart',
        konomitv_bs4k_detail: string,
    ): boolean {
        if (
            this.playback_mode !== 'Live' ||
            this.isKonomiTVBS4KAdvancedPlaybackProfileActive() === false ||
            (konomitv_bs4k_status !== 'Offline' && konomitv_bs4k_status !== 'Restart')
        ) {
            return false;
        }

        // サーバーが公開する能力理由コードとBridge専用再起動コードだけを対象にする。
        // 「エンコーダー」「エンコード」だけでは入力・GPU・受信障害まで含むため判定に使わない。
        const is_konomitv_bs4k_codec_pipeline_error = [
            /TS Codec Bridge/i,
            /\bER-07B\b/i,
            /\bBridgeUnavailable\b/i,
            /\bUnsupportedCombination\b/i,
            /\bProbeFailed\b/i,
            /\bEncoderUnavailable\b/i,
            /\bBitDepthUnsupported\b/i,
            /\badvanced live codecs?\b/i,
            /\brequested KonomiTV-BS4K (?:live|radio).+ encoding\b/i,
            /映像・音声コーデック/,
        ].some((pattern) => pattern.test(konomitv_bs4k_detail));
        if (is_konomitv_bs4k_codec_pipeline_error === false) return false;

        return (
            this.requestKonomiTVBS4KCompatibilityPlaybackFallback(
                `Live ${konomitv_bs4k_status}: ${konomitv_bs4k_detail}`,
            ) === true ||
            this.isKonomiTVBS4KCompatibilityPlaybackFallbackPending() === true
        );
    }


    /**
     * 録画HLSのplaylist/segmentが高度codec要求をHTTP 422/5xxで拒否した場合だけ互換fallbackへ渡す。
     *
     * status 0の回線断、timeout、404、認証エラーはcodecを変えても直らないため対象外とする。
     */
    private handleKonomiTVBS4KRecordedHLSLoadError(
        konomitv_bs4k_error_data: ErrorData,
    ): boolean {
        if (
            this.playback_mode !== 'Video' ||
            this.isKonomiTVBS4KAdvancedPlaybackProfileActive() === false
        ) {
            return false;
        }

        const konomitv_bs4k_http_status = konomitv_bs4k_error_data.response?.code;
        if (
            konomitv_bs4k_http_status !== 422 &&
            (
                konomitv_bs4k_http_status === undefined ||
                konomitv_bs4k_http_status < 500 ||
                konomitv_bs4k_http_status > 599
            )
        ) {
            return false;
        }
        const is_konomitv_bs4k_hls_load_error = [
            ErrorDetails.MANIFEST_LOAD_ERROR,
            ErrorDetails.LEVEL_LOAD_ERROR,
            ErrorDetails.AUDIO_TRACK_LOAD_ERROR,
            ErrorDetails.FRAG_LOAD_ERROR,
            ErrorDetails.KEY_LOAD_ERROR,
        ].includes(konomitv_bs4k_error_data.details);
        if (is_konomitv_bs4k_hls_load_error === false) return false;

        const konomitv_bs4k_request_url =
            konomitv_bs4k_error_data.response?.url ??
            konomitv_bs4k_error_data.url ??
            konomitv_bs4k_error_data.context?.url ??
            konomitv_bs4k_error_data.frag?.url;
        if (konomitv_bs4k_request_url === undefined) return false;
        let konomitv_bs4k_request_pathname: string;
        try {
            konomitv_bs4k_request_pathname =
                new URL(konomitv_bs4k_request_url, window.location.href).pathname;
        } catch {
            return false;
        }
        if (
            konomitv_bs4k_request_pathname.includes('/api/streams/video/') === false ||
            (
                konomitv_bs4k_request_pathname.endsWith('/playlist') === false &&
                konomitv_bs4k_request_pathname.endsWith('/segment') === false
            )
        ) {
            return false;
        }

        return this.requestKonomiTVBS4KCompatibilityPlaybackFallback(
            `hls.js ${konomitv_bs4k_error_data.details}: ` +
            `HTTP ${konomitv_bs4k_http_status}`,
        );
    }


    /**
     * ライブ視聴中の動的音声トラック変化を監視する
     */
    private setupLiveAudioTrackMonitor(): void {

        if (this.playback_mode !== 'Live') return;
        if (this.live_audio_track_interval_timer_cancel !== null) return;
        if (this.player === null) return;
        const monitor_player = this.player;
        const monitor_lifecycle_generation = this.lifecycle_generation;

        this.live_audio_track_interval_timer_cancel = Utils.setIntervalInWorker(() => {
            // clearInterval 相当の通知と Worker からの message は競合しうる。
            // 旧世代の最後の queued callback から、すでに destroy 済みの mpegts.js を参照しない。
            if (
                this.isLifecycleCurrent(
                    monitor_lifecycle_generation,
                    monitor_player,
                ) === false
            ) {
                return;
            }
            const media_info = this.getCurrentLiveAudioTrackMediaInfo();
            if (media_info !== null) {
                this.applyAudioTrackLabels(media_info);
            }
        }, 1000);
    }


    /**
     * native canplay 通知が欠落しても、メディア要素が再生を開始できる状態か判定する。
     * Safari の MSE で readyState が遅れる場合は、currentTime を含む実バッファ範囲を根拠にする。
     */
    private isMediaReadyToStartPlayback(video: HTMLVideoElement): boolean {
        if (video.readyState >= 3) return true;  // HAVE_FUTURE_DATA

        for (let index = 0; index < video.buffered.length; index++) {
            try {
                if (
                    video.buffered.start(index) <= video.currentTime + 0.05 &&
                    video.buffered.end(index) > video.currentTime + 0.05
                ) {
                    return true;
                }
            } catch {
                // SourceBuffer 更新と TimeRanges 参照が競合したら次の poll で再評価する。
            }
        }
        return false;
    }


    /**
     * 実際の MSE append / decode が失敗したとき、この PlayerController だけ一度限りで AVC / AAC へ切り替える。
     *
     * 能力 API の probe 結果や保存設定は変更しない。再初期化を跨いで attempted を保持するため、
     * 互換profile自体が失敗しても同じフォールバックを繰り返さない。
     */
    private requestKonomiTVBS4KCompatibilityPlaybackFallback(
        konomitv_bs4k_reason: string,
    ): boolean {

        const current_video = this.konomitv_bs4k_playback_video_codec_for_current_playback;
        const current_audio = this.konomitv_bs4k_playback_audio_codec_for_current_playback;
        if (
            this.konomitv_bs4k_compatibility_playback_fallback_attempted === true ||
            (current_video === 'avc' && current_audio === 'aac')
        ) {
            return false;
        }

        // 自動モード: AV1→VP9→HEVC→AVC を、能力 API で実在する exact combination だけを候補に落とす。
        // full 行列を見ずに順序だけで選ぶと、ProbeFailed の AV1 へ切り替えて再失敗する。
        if (
            this.isKonomiTVBS4KAutoQualityModeEnabled() === true &&
            this.konomitv_bs4k_current_playback_has_video === true
        ) {
            this.konomitv_bs4k_auto_failed_video_codecs.add(current_video);
            const video_order = this.playback_mode === 'Live' ?
                AUTO_PLAYBACK_LIVE_VIDEO_CODEC_ORDER :
                AUTO_PLAYBACK_RECORDED_VIDEO_CODEC_ORDER;
            const capabilities =
                this.konomitv_bs4k_playback_capabilities_for_ui.live_combinations.length > 0 ||
                this.konomitv_bs4k_playback_capabilities_for_ui.audio.length > 0 ||
                this.konomitv_bs4k_playback_capabilities_for_ui.video.length > 0 ?
                    this.konomitv_bs4k_playback_capabilities_for_ui :
                    this.konomitv_bs4k_playback_capabilities_for_current_playback;
            const next_video = video_order.find(
                (codec) =>
                    this.konomitv_bs4k_auto_failed_video_codecs.has(codec) === false &&
                    Videos.resolveKonomiTVBS4KExactPlaybackCombination(
                        capabilities,
                        this.konomitv_bs4k_playback_encoder_for_current_playback,
                        codec,
                        'aac',
                        this.konomitv_bs4k_playback_video_profile_for_current_playback,
                    ) !== null,
            );
            if (next_video !== undefined && next_video !== 'avc') {
                this.konomitv_bs4k_auto_session_override = {
                    ...this.konomitv_bs4k_auto_session_override,
                    video_codec: next_video,
                    // 段階落とし中は音声を安定寄り AAC に寄せる
                    audio_codec: 'aac',
                };
                const message =
                    `${current_video.toUpperCase()} の実再生に失敗したため、` +
                    `${next_video.toUpperCase()} へ段階的に切り替えます。` +
                    `理由: ${konomitv_bs4k_reason}`;
                console.warn('\u001b[31m[PlayerController]', message);
                Message.warning(message);
                usePlayerStore().event_emitter.emit('PlayerRestartRequired', {
                    message,
                    message_delay_seconds: 2,
                    is_error_message: false,
                    should_resume_quality: true,
                });
                return true;
            }
        }

        // 最終手段: AVC / AAC（従来の互換 fallback）
        this.konomitv_bs4k_compatibility_playback_fallback_attempted = true;
        this.konomitv_bs4k_force_recorded_aac_audio_codec = true;
        if (this.isKonomiTVBS4KAutoQualityModeEnabled() === true) {
            this.konomitv_bs4k_auto_session_override = {
                ...this.konomitv_bs4k_auto_session_override,
                video_codec: 'avc',
                audio_codec: 'aac',
            };
        }
        console.warn(
            '\u001b[31m[PlayerController] Playback profile failed. Falling back to AVC/AAC once.',
            konomitv_bs4k_reason,
        );
        const konomitv_bs4k_fallback_message =
            `${current_video.toUpperCase()} / ${current_audio.toUpperCase()} の実再生に失敗しました。` +
            `理由: ${konomitv_bs4k_reason}。` +
            '能力判定結果や保存設定は変更せず、今回だけ AVC / AAC へ切り替えます。' +
            'フォールバックできても、失敗した組み合わせの実再生 PASS とは扱いません。';

        Message.warning(konomitv_bs4k_fallback_message);
        usePlayerStore().event_emitter.emit('PlayerRestartRequired', {
            message: konomitv_bs4k_fallback_message,
            message_delay_seconds: 2,
            is_error_message: false,
            should_resume_quality: true,
        });
        return true;
    }


    /** 非互換profileの破棄待ち中に後続エラー通知を重ねないための判定。 */
    private isKonomiTVBS4KCompatibilityPlaybackFallbackPending(): boolean {

        return this.konomitv_bs4k_compatibility_playback_fallback_attempted === true &&
            (
                this.konomitv_bs4k_playback_video_codec_for_current_playback !== 'avc' ||
                this.konomitv_bs4k_playback_audio_codec_for_current_playback !== 'aac'
            );
    }


    /** 録画HLSの代替音声レンディションを、映像を維持したまま切り替える。 */
    private setupRecordedHLSAudioTrackSelector(): void {

        if (this.playback_mode !== 'Video' || this.player === null) return;
        const hls = this.player.plugins.hls as Hls | undefined;
        if (hls === undefined) return;  // 録画再生はhls.js必須。非対応表示は初期化処理側で行う。
        if (this.recorded_hls_audio_selector_instances.has(hls)) return;
        this.recorded_hls_audio_selector_instances.add(hls);

        // MSE 型判定を通過していても、実際の SourceBuffer 追加・append 時に映像・音声codecが拒否されることがある。
        // HTTP応答はplaylist/segmentの422・5xxだけに限定し、回線断や認証失敗をcodec障害と誤認しない。
        // そのほかはSourceBuffer codec / append失敗だけを互換profileフォールバックの対象にする。
        hls.on(Hls.Events.ERROR, (_event, data) => {
            if (
                this.handleKonomiTVBS4KRecordedHLSLoadError(data) === true ||
                this.isKonomiTVBS4KCompatibilityPlaybackFallbackPending() === true
            ) return;
            const is_source_buffer_codec_error = [
                ErrorDetails.BUFFER_ADD_CODEC_ERROR,
                ErrorDetails.BUFFER_APPEND_ERROR,
                ErrorDetails.BUFFER_APPENDING_ERROR,
            ].includes(data.details);
            if (is_source_buffer_codec_error === true) {
                this.requestKonomiTVBS4KCompatibilityPlaybackFallback(
                    `hls.js ${data.sourceBufferName ?? 'unknown'}: ${data.details}`,
                );
            }
        });
        const recorded_video = usePlayerStore().recorded_program.recorded_video;
        const renditions = recorded_video.audio_tracks.flatMap((track) => {
            const language_parts = (track.language ?? '').split('+').map((value) => value.trim()).filter(Boolean);
            if (track.is_dual_mono === true) {
                const main_display_index = recorded_video.audio_tracks.slice(0,
                    recorded_video.audio_tracks.indexOf(track)).reduce(
                    (count, item) => count + (item.is_dual_mono === true ? 2 : 1), 0) + 1;
                return [
                    {id: `${track.index}-main`, logicalIndex: track.index,
                        isDualMonoSub: false,
                        label: `Track${main_display_index}${language_parts[0] ? ` ${language_parts[0]}` : ''} (Monaural) 主音声`},
                    {id: `${track.index}-sub`, logicalIndex: track.index,
                        isDualMonoSub: true,
                        label: `Track${main_display_index + 1} ${language_parts[1] || '副音声'} (Monaural) 副音声`},
                ];
            }
            return [{
                id: String(track.index),
                logicalIndex: track.index,
                isDualMonoSub: false,
                label: track.title || `Track${track.index}${track.language ? ` ${track.language}` : ''}` +
                    `${track.channel ? ` (${track.channel})` : ''}`,
            }];
        });
        if (this.recorded_selected_audio_track_name === null && renditions.length > 0) {
            this.recorded_selected_audio_track_name = renditions[0].id;
        }
        let last_timeline_key = '';

        const switchAudioTrack = (index: number): void => {
            if (index < 0 || index >= hls.audioTracks.length || hls.audioTrack === index) return;
            // hls.jsの代替音声切替だけを使い、マスター・映像SourceBuffer・映像fragmentを維持する。
            hls.audioTrack = index;
        };

        const syncAudioTracks = () => {
            if (this.player === null) return;
            const panel = this.player.container.querySelector<HTMLElement>('.dplayer-setting-audio-panel');
            const header = panel?.querySelector<HTMLElement>('.dplayer-setting-audio-header');
            const settingItem = this.player.container.querySelector<HTMLElement>('.dplayer-setting-audio');
            const settingValue = settingItem?.querySelector<HTMLElement>('.dplayer-label-value');
            const settingBox = this.player.container.querySelector<HTMLElement>('.dplayer-setting-box');
            if (panel === null || panel === undefined || header === null || header === undefined) return;
            const current_time = this.player.video.currentTime;
            const timeline_interval = recorded_video.audio_track_timeline.find((interval) =>
                interval.start_time <= current_time && current_time < interval.end_time);
            const available_tracks = timeline_interval?.tracks ?? null;
            const isTrackAvailable = (index: number): boolean => {
                // タイムラインが不明な区間は全Trackを選択可能にし、サーバー側の無音補完で再生を継続する。
                if (available_tracks === null) return true;
                const rendition = renditions[index];
                const timeline_track = available_tracks.find((track) => track.index === rendition.logicalIndex);
                if (timeline_track === undefined) return false;
                // 同じ論理Trackでも、モノラル区間にDual Monoの副音声channelは存在しない。
                return rendition.isDualMonoSub === false || timeline_track.is_dual_mono === true;
            };
            const preferred_index = renditions.findIndex((track) => track.id === this.recorded_selected_audio_track_name);
            const is_preferred_available = preferred_index >= 0 && isTrackAvailable(preferred_index);
            const active_index = is_preferred_available ? preferred_index :
                renditions.findIndex((_, index) => isTrackAvailable(index));
            const active_rendition_id = active_index >= 0 ? renditions[active_index].id : null;
            // 初期表示・シーク・HLS再読み込みのいずれでも、実際のHLS Trackを現在区間の選択結果と同期する。
            // 希望Trackが消えた区間では希望値を保持したままTrack 1相当へ退避し、
            // 再出現時は同じ処理で希望Trackへ自動復帰する。タイムライン不明時は全Trackを利用可能とする。
            if (active_rendition_id !== null) {
                switchAudioTrack(active_index);
            }
            // 論理Track番号が同じままモノラルとDual Monoが切り替わる場合もUIを更新する。
            const timeline_key = available_tracks === null ? 'unknown' : available_tracks
                .map((track) => `${track.index}:${track.is_dual_mono === true ? 'dual' : 'single'}`)
                .join(',');
            const sync_key = `${timeline_key}/${this.recorded_selected_audio_track_name}/${active_rendition_id}`;
            if (sync_key === last_timeline_key) return;
            last_timeline_key = sync_key;

            // DPlayer は 0/1 トラック時に項目自体を隠すが、録画では状態表示として常に残す。
            this.player.container.classList.remove('dplayer-no-audio-switching');
            panel.querySelectorAll('.dplayer-setting-audio-item').forEach((item) => item.remove());
            const visible_track_count = renditions.filter((_, index) => isTrackAvailable(index)).length;
            if (visible_track_count === 0) {
                const item = document.createElement('div');
                item.className = 'dplayer-setting-audio-item dplayer-setting-audio-item--status';
                item.innerHTML = '<div class="dplayer-toggle"></div><span class="dplayer-label">音声なし</span>';
                panel.appendChild(item);
            }
            renditions.forEach((track, index) => {
                const item = document.createElement('div');
                item.className = 'dplayer-setting-audio-item';
                item.dataset.audio = String(index);
                item.innerHTML = `
                    <div class="dplayer-toggle">
                        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
                            <path d="M13 24l-9-9 2-2 7 7L27 6l2 2z"></path>
                        </svg>
                    </div>
                    <span class="dplayer-label"></span>
                `;
                const label = track.label;
                item.querySelector<HTMLElement>('.dplayer-label')!.textContent = label;
                const is_available = isTrackAvailable(index);
                item.classList.toggle('dplayer-setting-audio-item--disabled', is_available === false);
                item.classList.toggle('dplayer-setting-audio-current', active_rendition_id === track.id);
                item.setAttribute('aria-disabled', String(is_available === false));
                item.addEventListener('click', (event) => {
                    event.stopImmediatePropagation();
                    if (is_available === false || this.player === null || this.player.quality === null) return;
                    if (this.recorded_selected_audio_track_name === track.id) return;
                    this.recorded_selected_audio_track_name = track.id;
                    switchAudioTrack(index);
                    if (settingValue !== null && settingValue !== undefined) settingValue.textContent = label;
                    this.player?.container.querySelector('.dplayer-setting-box')?.classList.remove('dplayer-setting-box-audio');
                    last_timeline_key = '';
                    syncAudioTracks();
                });
                panel.appendChild(item);
            });

            const currentTrack = active_index >= 0 ? renditions[active_index] : undefined;
            if (settingValue !== null && settingValue !== undefined) {
                settingValue.textContent = visible_track_count === 0 ? '音声なし' :
                    currentTrack?.label || 'Track1 音声不明';
            }
            settingItem?.classList.toggle('dplayer-setting-audio--disabled', visible_track_count === 0);
            settingItem?.setAttribute('aria-disabled', String(visible_track_count === 0));
            const audio_panel_row_count = renditions.length + (visible_track_count === 0 ? 1 : 0);
            settingBox?.style.setProperty('--audio-panel-height', `${Math.max(audio_panel_row_count, 1) * 30 + 54}px`);
        };

        hls.on(Hls.Events.MANIFEST_PARSED, syncAudioTracks);
        hls.on(Hls.Events.AUDIO_TRACKS_UPDATED, syncAudioTracks);
        this.player.video.addEventListener('timeupdate', syncAudioTracks);
        this.player.video.addEventListener('seeking', syncAudioTracks);
        syncAudioTracks();
    }


    /**
     * 元放送の音声チャンネル構成を優先して、音声トラックの表示名を組み立てる
     */
    private buildAudioTrackLabels(media_info: {[key: string]: any} | null = null): string[] {
        const player_store = usePlayerStore();
        const channels_store = useChannelsStore();
        const media_audio_tracks = Array.isArray(media_info?.audioTracks) ? media_info.audioTracks : [];
        const media_audio_track_labels = media_audio_tracks.map((track, index) => {
            return typeof track?.name === 'string' && track.name.length > 0 ? track.name : `Track${index + 1}`;
        });

        if (this.playback_mode === 'Live') {
            const actual_audio_track_count = this.getLiveTSAudioTrackCount(media_info);
            const program = channels_store.current_program_present ?? channels_store.channel.current.program_present;
            const audio_components = program?.audio_components ?? [];
            const ordered_audio_components = [...audio_components].sort((a, b) => a.component_tag - b.component_tag);
            const expanded_audio_components = ordered_audio_components.flatMap((component) => {
                if (this.isDualMonoAudioType(component.audio_type) === false) return [component];
                const languages = component.language.split('+').map((language) => language.trim()).filter(Boolean);
                return [
                    {...component, language: languages[0] || '言語不明', audio_type: '1/0モード(シングルモノ)'},
                    {...component, language: languages[1] || '副音声', audio_type: '1/0モード(シングルモノ)'},
                ];
            });
            const component_tags = Array.isArray(media_info?.audioTrackComponentTags) ?
                media_info.audioTrackComponentTags as number[] : [];

            // トラック数と順序はPMTだけを正とし、EITは同じcomponent tagの表示名補完に限って使う。
            return Array.from({length: actual_audio_track_count}, (_, index) => {
                const component_tag = component_tags[index];
                // HWEncC の再muxでStream Identifier Descriptorが失われる場合は、PMT音声順と同じ
                // component tag昇順でEIT記述子を対応させる。件数はPMT側から増やさない。
                const component = (expanded_audio_components.length === actual_audio_track_count ?
                    expanded_audio_components[index] :
                    (typeof component_tag === 'number' ?
                        audio_components.find((item) => item.component_tag === component_tag) :
                        ordered_audio_components[index])) ?? null;
                if (component === null) return `Track${index + 1} 言語不明`;
                return this.formatAudioTrackLabel(index + 1, component.language, component.audio_type);
            });
        }

        if (this.playback_mode === 'Video') {
            if (media_audio_track_labels.length > 0) {
                return media_audio_track_labels;
            }

            const recorded_program = player_store.recorded_program;
            const audio_tracks = player_store.recorded_program.recorded_video.audio_tracks;
            if (audio_tracks.length > 0) {
                const allow_dual_mono_expansion = media_audio_tracks.length >= 2 || audio_tracks.length >= 2;
                const labels = audio_tracks.flatMap((track, index) => {
                    if (allow_dual_mono_expansion) {
                        const expanded_labels = this.expandDualMonoAudioTrackLabel(
                            track.index || index + 1,
                            track.language,
                            track.channel,
                        );
                        if (expanded_labels.length > 0) {
                            return expanded_labels;
                        }
                    }
                    const language = track.language ? ` ${track.language}` : '';
                    const channel = track.channel ? ` (${track.channel})` : '';
                    return [`Track${track.index || index + 1}${language}${channel}`];
                });
                return labels.map((label, index) => label.replace(/^Track\d+/, `Track${index + 1}`));
            }

            const labels = this.buildProgramAudioTrackLabels(
                recorded_program.primary_audio_language,
                recorded_program.primary_audio_type,
                recorded_program.secondary_audio_language,
                recorded_program.secondary_audio_type,
                media_audio_tracks.length >= 2,
            );
            if (labels.length > 0) {
                return labels;
            }
        }

        const program = channels_store.current_program_present ?? channels_store.channel.current.program_present;
        const labels = program !== null ? this.buildProgramAudioTrackLabels(
            program.primary_audio_language,
            program.primary_audio_type,
            program.secondary_audio_language,
            program.secondary_audio_type,
            media_audio_tracks.length >= 2,
        ) : [];
        if (labels.length > 0) {
            return labels;
        }

        return media_audio_track_labels;
    }


    /**
     * 実際に選択可能な音声トラック数を取得する
     */
    private getSelectableAudioTrackCount(media_info: {[key: string]: any} | null, labels: string[]): number {
        const player_store = usePlayerStore();
        const channels_store = useChannelsStore();
        const media_audio_tracks = Array.isArray(media_info?.audioTracks) ? media_info.audioTracks : [];

        if (this.playback_mode === 'Live') {
            // 配信 TS の実トラック数を上限とし、現在番組の音声構成から消滅したトラックを除外する。
            return this.getLiveSelectableAudioTrackCount(media_info);
        }

        if (this.playback_mode === 'Video') {
            if (media_audio_tracks.length > 0) {
                return Math.max(media_audio_tracks.length, labels.length);
            }

            const audio_tracks = player_store.recorded_program.recorded_video.audio_tracks;
            if (audio_tracks.length > 0) {
                return audio_tracks.length;
            }

            let fallback_audio_track_count = 0;
            if (player_store.recorded_program.primary_audio_type) {
                fallback_audio_track_count += 1;
            }
            if (player_store.recorded_program.secondary_audio_type) {
                fallback_audio_track_count += 1;
            }
            return fallback_audio_track_count;
        }

        const program = channels_store.current_program_present ?? channels_store.channel.current.program_present;
        if (program !== null) {
            let fallback_audio_track_count = 0;
            if (program.primary_audio_type) {
                fallback_audio_track_count += 1;
            }
            if (program.secondary_audio_type) {
                fallback_audio_track_count += 1;
            }
            if (fallback_audio_track_count > 0) {
                return fallback_audio_track_count;
            }
        }

        return labels.length;
    }


    /**
     * ISDB の音声 component type を短いチャンネル構成表記に寄せる
     */
    private formatAudioTrackLabel(index: number, language: string | null, audio_type: string): string {
        let channel = audio_type;
        if (audio_type.includes('22.2') || audio_type.includes('3/3/3-5/2/3-3/0/0.2')) {
            channel = '22.2ch';
        } else if (audio_type.includes('5.1') || audio_type.includes('3/2+LFE')) {
            channel = '5.1ch';
        } else if (audio_type.includes('ステレオ') || audio_type.includes('2/0')) {
            channel = 'Stereo';
        } else if (audio_type.includes('モノ') || audio_type.includes('1/0')) {
            channel = 'Monaural';
        }
        const language_label = language ? ` ${language}` : '';
        return `Track${index}${language_label} (${channel})`;
    }


    /**
     * 主音声/副音声メタデータから、実際の音声トラック表示名を組み立てる
     */
    private buildProgramAudioTrackLabels(
        primary_audio_language: string | null,
        primary_audio_type: string | null,
        secondary_audio_language: string | null,
        secondary_audio_type: string | null,
        allow_dual_mono_expansion: boolean,
    ): string[] {

        const labels: string[] = [];
        if (primary_audio_type) {
            labels.push(...this.buildAudioTrackLabelEntries(
                labels.length + 1,
                primary_audio_language,
                primary_audio_type,
                allow_dual_mono_expansion,
            ));
        }
        if (secondary_audio_type) {
            labels.push(...this.buildAudioTrackLabelEntries(
                labels.length + 1,
                secondary_audio_language,
                secondary_audio_type,
                allow_dual_mono_expansion,
            ));
        }
        return labels.map((label, index) => label.replace(/^Track\d+/, `Track${index + 1}`));
    }


    /**
     * dual mono は tsreadex 側で2本の mono 音声に分離されるため、表示も2トラックに展開する
     */
    private buildAudioTrackLabelEntries(
        index: number,
        language: string | null,
        audio_type: string,
        allow_dual_mono_expansion: boolean,
    ): string[] {

        if (allow_dual_mono_expansion) {
            const dual_mono_labels = this.expandDualMonoAudioTrackLabel(index, language, audio_type);
            if (dual_mono_labels.length > 0) {
                return dual_mono_labels;
            }
        }
        return [this.formatAudioTrackLabel(index, language, audio_type)];
    }


    /**
     * `日本語+英語` のような dual mono 言語表記を、TrackN 日本語 (Monaural) / TrackN+1 英語 (Monaural) に展開する
     */
    private expandDualMonoAudioTrackLabel(index: number, language: string | null, audio_type_or_channel: string | null): string[] {

        if (!audio_type_or_channel || this.isDualMonoAudioTrack(language, audio_type_or_channel) === false) {
            return [];
        }

        const languages = (language ?? '')
            .split('+')
            .map((language_) => language_.trim())
            .filter((language_) => language_.length > 0);
        const primary_language = languages[0] ?? null;
        const secondary_language = languages[1] ?? '副音声';

        return [
            this.formatAudioTrackLabel(index, primary_language, '1/0モード(シングルモノ)'),
            this.formatAudioTrackLabel(index + 1, secondary_language, '1/0モード(シングルモノ)'),
        ];
    }


    /**
     * ISDB の dual mono 表記かどうかを判定する
     */
    private isDualMonoAudioType(audio_type: string): boolean {

        return audio_type.includes('デュアルモノ') || audio_type.includes('1/0+1/0');
    }


    /**
     * audio_tracks 側では channel がすでに mono 表記になっている場合があるため、言語表記も併せて dual mono を判定する
     */
    private isDualMonoAudioTrack(language: string | null, audio_type_or_channel: string): boolean {

        if (this.isDualMonoAudioType(audio_type_or_channel)) {
            return true;
        }
        const normalized_channel = audio_type_or_channel.toLowerCase();
        return (language ?? '').includes('+') &&
            (normalized_channel.includes('mono') || audio_type_or_channel.includes('モノ') || audio_type_or_channel.includes('1/0'));
    }


    /**
     * DPlayer の設定パネルを無理やり拡張し、KonomiTV 独自の項目を追加する
     */
    private setupSettingPanelHandler(live_playback_policy: Readonly<ILivePlaybackPolicy>): void {
        assert(this.player !== null);
        const player_store = usePlayerStore();
        const channels_store = useChannelsStore();
        const settings_store = useSettingsStore();
        const is_konomitv_bs4k = this.playback_mode === 'Live' ?
            channels_store.channel.current.display_channel_id.startsWith('bs4k') :
            player_store.recorded_program.network_id === 0x000B;

        // 独自サブパネルの表示は modifier class だけで制御し、DPlayer が元パネル用に設定した
        // inline clip-path / height は変更しない。閉じる際に全 modifier を外すことで、
        // 戻る操作・外側クリック・別サブパネルへの移動のいずれでも元の寸法へ確実に戻す。
        const setting_box = this.player.template.settingBox;
        const konomitv_bs4k_codec_panel_class_names = [
            'dplayer-konomitv-bs4k-setting-box-video-codec',
            'dplayer-konomitv-bs4k-setting-box-audio-codec',
        ];
        const closeKonomiTVBS4KCodecPanel = () => {
            setting_box.classList.remove(...konomitv_bs4k_codec_panel_class_names);
        };
        const openKonomiTVBS4KCodecPanel = (
            konomitv_bs4k_panel_name: 'video-codec' | 'audio-codec',
        ) => {
            closeKonomiTVBS4KCodecPanel();
            setting_box.classList.add(
                `dplayer-konomitv-bs4k-setting-box-${konomitv_bs4k_panel_name}`,
            );
        };

        // 設定パネルの開閉を把握するためモンキーパッチを追加し、PlayerStore に通知する
        const original_hide = this.player.setting.hide;
        const original_show = this.player.setting.show;
        this.player.setting.hide = () => {
            if (this.player === null) return;
            original_hide.call(this.player.setting);
            // 独自サブパネルを開いたまま外側を押して閉じた場合も、次回は必ず元パネルから表示する。
            closeKonomiTVBS4KCodecPanel();
            player_store.is_player_setting_panel_open = false;
        };
        this.player.setting.show = () => {
            if (this.player === null) return;
            original_show.call(this.player.setting);
            player_store.is_player_setting_panel_open = true;
        };

        // 設定画面と同じ full 能力行列で選択肢を作る。
        // targeted（現在再生候補だけ）だと、未 probe の Opus/VP9/AV1 が ProbeFailed と誤表示される。
        // full 未取得時（テストや通信失敗）は targeted を流用する。
        const konomitv_bs4k_ui_capabilities =
            this.konomitv_bs4k_playback_capabilities_for_ui.live_combinations.length > 0 ||
            this.konomitv_bs4k_playback_capabilities_for_ui.audio.length > 0 ||
            this.konomitv_bs4k_playback_capabilities_for_ui.video.length > 0 ?
                this.konomitv_bs4k_playback_capabilities_for_ui :
                this.konomitv_bs4k_playback_capabilities_for_current_playback;
        const konomitv_bs4k_video_codec_options = Videos.buildKonomiTVBS4KPlaybackVideoCodecOptions(
            this.konomitv_bs4k_playback_encoder_for_current_playback,
            konomitv_bs4k_ui_capabilities,
            this.konomitv_bs4k_playback_video_profile_for_current_playback,
            this.konomitv_bs4k_playback_audio_codec_for_current_playback,
        );
        const konomitv_bs4k_video_codec_item_html =
            konomitv_bs4k_video_codec_options.map((konomitv_bs4k_option) => `
            <div class="dplayer-konomitv-bs4k-setting-video-codec-item${konomitv_bs4k_option.props.disabled ? ' dplayer-konomitv-bs4k-setting-video-codec-item--disabled' : ''}"
                data-konomitv-bs4k-codec="${konomitv_bs4k_option.value}" aria-disabled="${konomitv_bs4k_option.props.disabled}"
                style="display:flex; align-items:center; height:30px; padding:5px 10px; box-sizing:border-box; cursor:pointer;">
                <div class="dplayer-toggle dplayer-konomitv-bs4k-setting-video-codec-check" style="display:inline-block; position:static; width:22px; margin-right:6px;"></div>
                <span class="dplayer-label">${konomitv_bs4k_option.value.toUpperCase()}${konomitv_bs4k_option.props.disabled ? `（${konomitv_bs4k_option.reason ?? '非対応'}）` : ''}</span>
            </div>
        `).join('');
        const konomitv_bs4k_video_codec_panel_height =
            54 + konomitv_bs4k_video_codec_options.length * 30;
        const konomitv_bs4k_audio_codec_labels: Record<KonomiTVBS4KPlaybackAudioCodec, string> = {
            aac: 'AAC',
            opus: 'Opus',
        };
        const konomitv_bs4k_audio_codec_options = Videos.buildKonomiTVBS4KPlaybackAudioCodecOptions(
            konomitv_bs4k_ui_capabilities,
            this.konomitv_bs4k_playback_encoder_for_current_playback,
            this.konomitv_bs4k_playback_video_codec_for_current_playback,
            this.konomitv_bs4k_playback_video_profile_for_current_playback,
        );
        const konomitv_bs4k_audio_codec_item_html =
            konomitv_bs4k_audio_codec_options.map((konomitv_bs4k_option) => `
            <div class="dplayer-konomitv-bs4k-setting-audio-codec-item${konomitv_bs4k_option.props.disabled ? ' dplayer-konomitv-bs4k-setting-audio-codec-item--disabled' : ''}"
                data-konomitv-bs4k-codec="${konomitv_bs4k_option.value}" aria-disabled="${konomitv_bs4k_option.props.disabled}"
                style="display:flex; align-items:center; height:30px; padding:5px 10px; box-sizing:border-box; cursor:pointer;">
                <div class="dplayer-toggle dplayer-konomitv-bs4k-setting-audio-codec-check" style="display:inline-block; position:static; width:22px; margin-right:6px;"></div>
                <span class="dplayer-label">${konomitv_bs4k_audio_codec_labels[konomitv_bs4k_option.value]}${konomitv_bs4k_option.props.disabled ? `（${konomitv_bs4k_option.reason ?? '非対応'}）` : ''}</span>
            </div>
        `).join('');
        const konomitv_bs4k_audio_codec_panel_height =
            54 + konomitv_bs4k_audio_codec_options.length * 30;
        const konomitv_bs4k_audio_codec_setting_item_html = `
            <div class="dplayer-setting-item dplayer-konomitv-bs4k-setting-audio-codec">
                <span class="dplayer-label">音声コーデック</span>
                <span class="dplayer-label-value dplayer-konomitv-bs4k-setting-audio-codec-value"></span>
                <div class="dplayer-toggle dplayer-konomitv-bs4k-setting-audio-codec-arrow"></div>
            </div>
        `;
        const auto_skip_cm_setting_item_html = this.playback_mode === 'Video' ? `
            <div class="dplayer-setting-item dplayer-setting-auto-skip-cm">
                <span class="dplayer-label">CM自動スキップ</span>
                <div class="dplayer-toggle">
                    <input class="dplayer-auto-skip-cm-setting-input" type="checkbox" name="dplayer-toggle-auto-skip-cm">
                    <label for="dplayer-toggle-auto-skip-cm" style="--theme-color:rgb(var(--v-theme-primary))"></label>
                </div>
            </div>
        ` : '';
        // ライブのみ: 設定画面の低遅延キーをプレイヤーから直接切り替える
        const low_latency_setting_item_html = this.playback_mode === 'Live' ? `
            <div class="dplayer-setting-item dplayer-setting-low-latency">
                <span class="dplayer-label">低遅延モード</span>
                <div class="dplayer-toggle">
                    <input class="dplayer-low-latency-setting-input" type="checkbox" name="dplayer-toggle-low-latency">
                    <label for="dplayer-toggle-low-latency" style="--theme-color:rgb(var(--v-theme-primary))"></label>
                </div>
            </div>
        ` : '';
        this.player.template.audio.insertAdjacentHTML('afterend', `
            <div class="dplayer-setting-item dplayer-konomitv-bs4k-setting-auto-quality">
                <span class="dplayer-label">自動画質選択</span>
                <div class="dplayer-toggle">
                    <input class="dplayer-konomitv-bs4k-auto-quality-setting-input" type="checkbox"
                        id="dplayer-konomitv-bs4k-toggle-auto-quality" name="dplayer-konomitv-bs4k-toggle-auto-quality">
                    <label for="dplayer-konomitv-bs4k-toggle-auto-quality" style="--theme-color:rgb(var(--v-theme-primary))"></label>
                </div>
            </div>
            <div class="dplayer-setting-item dplayer-konomitv-bs4k-setting-video-codec">
                <span class="dplayer-label">映像コーデック</span>
                <span class="dplayer-label-value dplayer-konomitv-bs4k-setting-video-codec-value"></span>
                <div class="dplayer-toggle dplayer-konomitv-bs4k-setting-video-codec-arrow"></div>
            </div>
            ${konomitv_bs4k_audio_codec_setting_item_html}
            ${auto_skip_cm_setting_item_html}
            ${low_latency_setting_item_html}
            <div class="dplayer-setting-item dplayer-konomitv-bs4k-setting-mobile-profile">
                <span class="dplayer-label">モバイル回線向け画質</span>
                <div class="dplayer-toggle">
                    <input class="dplayer-konomitv-bs4k-mobile-profile-setting-input" type="checkbox"
                        id="dplayer-konomitv-bs4k-toggle-mobile-profile" name="dplayer-konomitv-bs4k-toggle-mobile-profile">
                    <label for="dplayer-konomitv-bs4k-toggle-mobile-profile" style="--theme-color:rgb(var(--v-theme-primary))"></label>
                </div>
            </div>
        `);

        // DPlayer の音声トラックと同じ構成の独自サブパネルを追加する。
        const konomitv_bs4k_audio_codec_panel_html = `
            <div class="dplayer-konomitv-bs4k-setting-audio-codec-panel"
                style="display:block; position:absolute; bottom:0; width:100%; padding:7px 0; box-sizing:border-box; transform:translateX(100%); transition:transform .25s ease;">
                <div class="dplayer-setting-header dplayer-konomitv-bs4k-setting-audio-codec-header"
                    style="display:flex; align-items:center; height:33px; padding:0 5px 5px; margin-bottom:7px; border-bottom:2px solid rgba(255,255,255,.15); box-sizing:border-box; cursor:pointer;">
                    <div class="dplayer-toggle dplayer-konomitv-bs4k-setting-audio-codec-back"
                        style="display:inline-block; position:static; width:22px; margin-right:6px;"></div>
                    <span class="dplayer-label">音声コーデック</span>
                </div>
                ${konomitv_bs4k_audio_codec_item_html}
            </div>
        `;
        setting_box.insertAdjacentHTML('beforeend', `
            <div class="dplayer-konomitv-bs4k-setting-video-codec-panel"
                style="display:block; position:absolute; bottom:0; width:100%; padding:7px 0; box-sizing:border-box; transform:translateX(100%); transition:transform .25s ease;">
                <div class="dplayer-setting-header dplayer-konomitv-bs4k-setting-video-codec-header"
                    style="display:flex; align-items:center; height:33px; padding:0 5px 5px; margin-bottom:7px; border-bottom:2px solid rgba(255,255,255,.15); box-sizing:border-box; cursor:pointer;">
                    <div class="dplayer-toggle dplayer-konomitv-bs4k-setting-video-codec-back"
                        style="display:inline-block; position:static; width:22px; margin-right:6px;"></div>
                    <span class="dplayer-label">映像コーデック</span>
                </div>
                ${konomitv_bs4k_video_codec_item_html}
            </div>
            ${konomitv_bs4k_audio_codec_panel_html}
        `);

        // DPlayer が持つ音声トラック用の矢印・戻る・チェックアイコンを流用して見た目を揃える
        const konomitv_bs4k_audio_arrow_html =
            this.player.template.audio.querySelector<HTMLElement>('.dplayer-toggle')?.innerHTML ?? '';
        const konomitv_bs4k_audio_back_html =
            this.player.container.querySelector<HTMLElement>('.dplayer-setting-audio-header .dplayer-toggle')?.innerHTML ?? '';
        const konomitv_bs4k_audio_check_html =
            this.player.container.querySelector<HTMLElement>('.dplayer-setting-audio-item .dplayer-toggle')?.innerHTML ?? '';
        this.player.container.querySelector<HTMLElement>('.dplayer-konomitv-bs4k-setting-video-codec-arrow')!.innerHTML =
            konomitv_bs4k_audio_arrow_html;
        this.player.container.querySelector<HTMLElement>('.dplayer-konomitv-bs4k-setting-video-codec-back')!.innerHTML =
            konomitv_bs4k_audio_back_html;
        this.player.container.querySelectorAll<HTMLElement>('.dplayer-konomitv-bs4k-setting-video-codec-check')
            .forEach((konomitv_bs4k_element) =>
                konomitv_bs4k_element.innerHTML = konomitv_bs4k_audio_check_html);
        this.player.container.querySelector<HTMLElement>('.dplayer-konomitv-bs4k-setting-audio-codec-arrow')!.innerHTML =
            konomitv_bs4k_audio_arrow_html;
        this.player.container.querySelector<HTMLElement>('.dplayer-konomitv-bs4k-setting-audio-codec-back')!.innerHTML =
            konomitv_bs4k_audio_back_html;
        this.player.container.querySelectorAll<HTMLElement>('.dplayer-konomitv-bs4k-setting-audio-codec-check')
            .forEach((konomitv_bs4k_element) =>
                konomitv_bs4k_element.innerHTML = konomitv_bs4k_audio_check_html);

        // 現在の再生種別・放送種別・回線プロファイルに対応する設定キーを取得する
        const getKonomiTVBS4KCurrentVideoCodecSettingKey = () =>
            getKonomiTVBS4KPlaybackVideoCodecSettingKey(
                is_konomitv_bs4k,
                this.quality_profile_type === 'Cellular',
            );
        const getKonomiTVBS4KCurrentAudioCodecSettingKey = () =>
            getKonomiTVBS4KPlaybackAudioCodecSettingKey(
                is_konomitv_bs4k,
                this.quality_profile_type === 'Cellular',
            );

        // サブパネルは設定画面と同じ SettingsStore の値を直接読み書きする
        const konomitv_bs4k_video_codec_button =
            this.player.container.querySelector<HTMLElement>('.dplayer-konomitv-bs4k-setting-video-codec')!;
        const konomitv_bs4k_video_codec_value =
            this.player.container.querySelector<HTMLElement>('.dplayer-konomitv-bs4k-setting-video-codec-value')!;
        const konomitv_bs4k_video_codec_items = Array.from(
            this.player.container.querySelectorAll<HTMLElement>('.dplayer-konomitv-bs4k-setting-video-codec-item'),
        );
        setting_box.style.setProperty(
            '--konomitv-bs4k-video-codec-panel-height',
            `${konomitv_bs4k_video_codec_panel_height}px`,
        );
        const updateKonomiTVBS4KVideoCodecDisplay = () => {
            const konomitv_bs4k_selected_codec =
                this.konomitv_bs4k_auto_session_override.video_codec ??
                (this.isKonomiTVBS4KAutoQualityModeEnabled() === true ?
                    this.konomitv_bs4k_playback_video_codec_for_current_playback :
                    settings_store.settings[getKonomiTVBS4KCurrentVideoCodecSettingKey()]);
            konomitv_bs4k_video_codec_value.textContent = konomitv_bs4k_selected_codec.toUpperCase();
            konomitv_bs4k_video_codec_items.forEach((konomitv_bs4k_item) => {
                const konomitv_bs4k_check =
                    konomitv_bs4k_item.querySelector<HTMLElement>(
                        '.dplayer-konomitv-bs4k-setting-video-codec-check',
                    );
                if (konomitv_bs4k_check !== null) {
                    konomitv_bs4k_check.style.visibility =
                        konomitv_bs4k_item.dataset.konomitvBs4kCodec ===
                            konomitv_bs4k_selected_codec ?
                            'visible' :
                            'hidden';
                }
            });
        };
        updateKonomiTVBS4KVideoCodecDisplay();
        konomitv_bs4k_video_codec_button.addEventListener('click', () => {
            updateKonomiTVBS4KVideoCodecDisplay();
            // DPlayer が計測した元パネル用の inline clip-path は上書きせず、
            // サブパネル表示中だけ専用クラスで切り替える。
            openKonomiTVBS4KCodecPanel('video-codec');
        });
        this.player.container.querySelector('.dplayer-konomitv-bs4k-setting-video-codec-header')!
            .addEventListener('click', closeKonomiTVBS4KCodecPanel);
        konomitv_bs4k_video_codec_items.forEach((konomitv_bs4k_item) => {
            konomitv_bs4k_item.addEventListener('click', () => {
                if (
                    konomitv_bs4k_item.classList.contains(
                        'dplayer-konomitv-bs4k-setting-video-codec-item--disabled',
                    )
                ) return;
                const konomitv_bs4k_selected_video_codec =
                    konomitv_bs4k_item.dataset.konomitvBs4kCodec as KonomiTVBS4KPlaybackVideoCodec;
                const konomitv_bs4k_current_audio_codec =
                    this.konomitv_bs4k_auto_session_override.audio_codec ??
                    (this.isKonomiTVBS4KAutoQualityModeEnabled() === true ?
                        this.konomitv_bs4k_playback_audio_codec_for_current_playback :
                        settings_store.settings[getKonomiTVBS4KCurrentAudioCodecSettingKey()]);

                // 選択した映像を固定して exact combination を再解決し、必要なら音声も同じ mutation で合わせる。
                // 項目生成後に設定値が同期更新されていても、非対応な中間 tuple を永続化しない。
                const konomitv_bs4k_combination =
                    Videos.resolveKonomiTVBS4KPlaybackCombinationForVideoCodecChange(
                        konomitv_bs4k_ui_capabilities,
                        this.konomitv_bs4k_playback_encoder_for_current_playback,
                        konomitv_bs4k_selected_video_codec,
                        konomitv_bs4k_current_audio_codec,
                        this.konomitv_bs4k_playback_video_profile_for_current_playback,
                    );
                if (konomitv_bs4k_combination === null) return;
                // 自動モード中は設定へ保存せず、セッション一時オーバーライドのみ更新する。
                if (this.isKonomiTVBS4KAutoQualityModeEnabled() === true) {
                    this.konomitv_bs4k_auto_session_override = {
                        ...this.konomitv_bs4k_auto_session_override,
                        video_codec: konomitv_bs4k_combination.video.codec,
                        audio_codec: konomitv_bs4k_combination.audio.codec,
                    };
                } else {
                    settings_store.updateKonomiTVBS4KPlaybackCodecPair(
                        is_konomitv_bs4k,
                        this.quality_profile_type === 'Cellular',
                        konomitv_bs4k_combination.video.codec,
                        konomitv_bs4k_combination.audio.codec,
                    );
                }
                this.konomitv_bs4k_compatibility_playback_fallback_attempted = false;
                this.konomitv_bs4k_force_recorded_aac_audio_codec = false;
                updateKonomiTVBS4KVideoCodecDisplay();
                closeKonomiTVBS4KCodecPanel();
                // プレイヤー再起動が始まる前に設定パネル全体を閉じ、黒画面上へ一瞬残ることを防ぐ
                this.player?.setting.hide();
                player_store.event_emitter.emit('PlayerRestartRequired', {
                    message:
                        `映像コーデックを ${konomitv_bs4k_combination.video.codec.toUpperCase()} に変更しました。` +
                        (this.isKonomiTVBS4KAutoQualityModeEnabled() === true ? '（一時変更・非保存）' : ''),
                    message_delay_seconds:
                        live_playback_policy.live_sync_enabled || this.playback_mode === 'Video' ? 2 : 4.5,
                    is_error_message: false,
                    should_resume_quality: true,
                });
            });
        });

        // 音声コーデックもライブ・録画共通で、同じ回線プロファイル単位に保存する。
        // 非対応項目は理由を残して表示するが、クリックは受け付けない。
        const konomitv_bs4k_audio_codec_button =
            this.player.container.querySelector<HTMLElement>('.dplayer-konomitv-bs4k-setting-audio-codec')!;
        const konomitv_bs4k_audio_codec_value =
            this.player.container.querySelector<HTMLElement>('.dplayer-konomitv-bs4k-setting-audio-codec-value')!;
        const konomitv_bs4k_audio_codec_items = Array.from(
            this.player.container.querySelectorAll<HTMLElement>('.dplayer-konomitv-bs4k-setting-audio-codec-item'),
        );
        setting_box.style.setProperty(
            '--konomitv-bs4k-audio-codec-panel-height',
            `${konomitv_bs4k_audio_codec_panel_height}px`,
        );
        const updateKonomiTVBS4KAudioCodecDisplay = () => {
            const konomitv_bs4k_selected_codec =
                this.konomitv_bs4k_auto_session_override.audio_codec ??
                (this.isKonomiTVBS4KAutoQualityModeEnabled() === true ?
                    this.konomitv_bs4k_playback_audio_codec_for_current_playback :
                    settings_store.settings[getKonomiTVBS4KCurrentAudioCodecSettingKey()]);
            konomitv_bs4k_audio_codec_value.textContent =
                konomitv_bs4k_audio_codec_labels[konomitv_bs4k_selected_codec];
            konomitv_bs4k_audio_codec_items.forEach((konomitv_bs4k_item) => {
                const konomitv_bs4k_check =
                    konomitv_bs4k_item.querySelector<HTMLElement>(
                        '.dplayer-konomitv-bs4k-setting-audio-codec-check',
                    );
                if (konomitv_bs4k_check !== null) {
                    konomitv_bs4k_check.style.visibility =
                        konomitv_bs4k_item.dataset.konomitvBs4kCodec ===
                            konomitv_bs4k_selected_codec ?
                            'visible' :
                            'hidden';
                }
            });
        };
        updateKonomiTVBS4KAudioCodecDisplay();
        konomitv_bs4k_audio_codec_button.addEventListener('click', () => {
            updateKonomiTVBS4KAudioCodecDisplay();
            openKonomiTVBS4KCodecPanel('audio-codec');
        });
        this.player.container.querySelector('.dplayer-konomitv-bs4k-setting-audio-codec-header')!
            .addEventListener('click', closeKonomiTVBS4KCodecPanel);
        konomitv_bs4k_audio_codec_items.forEach((konomitv_bs4k_item) => {
            konomitv_bs4k_item.addEventListener('click', () => {
                if (
                    konomitv_bs4k_item.classList.contains(
                        'dplayer-konomitv-bs4k-setting-audio-codec-item--disabled',
                    )
                ) return;
                const konomitv_bs4k_selected_audio_codec =
                    konomitv_bs4k_item.dataset.konomitvBs4kCodec as KonomiTVBS4KPlaybackAudioCodec;
                const konomitv_bs4k_current_video_codec =
                    this.konomitv_bs4k_auto_session_override.video_codec ??
                    (this.isKonomiTVBS4KAutoQualityModeEnabled() === true ?
                        this.konomitv_bs4k_playback_video_codec_for_current_playback :
                        settings_store.settings[getKonomiTVBS4KCurrentVideoCodecSettingKey()]);

                // 選択した音声を固定して exact combination を再解決し、必要なら映像も同じ mutation で合わせる。
                // 映像と音声を別々に保存すると購読側が非対応な中間 tuple を観測するため、必ず pair action を使う。
                const konomitv_bs4k_combination =
                    Videos.resolveKonomiTVBS4KPlaybackCombinationForAudioCodecChange(
                        konomitv_bs4k_ui_capabilities,
                        this.konomitv_bs4k_playback_encoder_for_current_playback,
                        konomitv_bs4k_current_video_codec,
                        konomitv_bs4k_selected_audio_codec,
                        this.konomitv_bs4k_playback_video_profile_for_current_playback,
                    );
                if (konomitv_bs4k_combination === null) return;
                if (this.isKonomiTVBS4KAutoQualityModeEnabled() === true) {
                    this.konomitv_bs4k_auto_session_override = {
                        ...this.konomitv_bs4k_auto_session_override,
                        video_codec: konomitv_bs4k_combination.video.codec,
                        audio_codec: konomitv_bs4k_combination.audio.codec,
                    };
                } else {
                    settings_store.updateKonomiTVBS4KPlaybackCodecPair(
                        is_konomitv_bs4k,
                        this.quality_profile_type === 'Cellular',
                        konomitv_bs4k_combination.video.codec,
                        konomitv_bs4k_combination.audio.codec,
                    );
                }

                // ユーザーが明示的に選び直したときは、過去の実再生エラーによる一時フォールバックを解除する。
                // Opus を再選択した場合も、この操作をもって再試行を許可する。
                this.konomitv_bs4k_force_recorded_aac_audio_codec = false;
                this.konomitv_bs4k_compatibility_playback_fallback_attempted = false;
                updateKonomiTVBS4KAudioCodecDisplay();
                closeKonomiTVBS4KCodecPanel();
                this.player?.setting.hide();
                player_store.event_emitter.emit('PlayerRestartRequired', {
                    message:
                        '音声コーデックを ' +
                        `${konomitv_bs4k_audio_codec_labels[konomitv_bs4k_combination.audio.codec]} に変更しました。` +
                        (this.isKonomiTVBS4KAutoQualityModeEnabled() === true ? '（一時変更・非保存）' : ''),
                    message_delay_seconds: 2,
                    is_error_message: false,
                    should_resume_quality: true,
                });
            });
        });

        // 録画再生時のみ、CM 自動スキップの有効状態を端末ローカル設定へ保存する
        // CM 区間が未解析・0件でも、今後再生する録画へ向けて常に切り替えられるようにする
        if (this.playback_mode === 'Video') {
            const auto_skip_cm_button = this.player.container.querySelector<HTMLElement>('.dplayer-setting-auto-skip-cm')!;
            const auto_skip_cm_input = auto_skip_cm_button.querySelector<HTMLInputElement>(
                '.dplayer-auto-skip-cm-setting-input',
            )!;
            auto_skip_cm_input.checked = settings_store.settings.video_auto_skip_cm;
            auto_skip_cm_button.addEventListener('click', () => {
                auto_skip_cm_input.checked = !auto_skip_cm_input.checked;
                settings_store.settings.video_auto_skip_cm = auto_skip_cm_input.checked;
            });
        }

        // ライブのみ: 低遅延モードを現在の回線・チャンネル種別の設定キーへ保存する
        // setupLivePlaybackPolicyWatcher が設定変更を検知して PlayerRestartRequired を出す
        if (this.playback_mode === 'Live') {
            const server_settings_store = useServerSettingsStore();
            const low_latency_button = this.player.container.querySelector<HTMLElement>(
                '.dplayer-setting-low-latency',
            )!;
            const low_latency_input = low_latency_button.querySelector<HTMLInputElement>(
                '.dplayer-low-latency-setting-input',
            )!;
            const is_bs4k_live = channels_store.channel.current.display_channel_id.startsWith('bs4k');
            const force_stable = is_bs4k_live === true &&
                server_settings_store.server_settings.general.bs4k_ignore_viewer_low_latency === true;
            const is_cellular = this.quality_profile_type === 'Cellular';
            const getActiveLowLatencySettingKey = ():
            | 'tv_low_latency_mode'
            | 'tv_low_latency_mode_cellular'
            | 'tv_low_latency_mode_for_bs4k'
            | 'tv_low_latency_mode_for_bs4k_cellular' => {
                if (is_bs4k_live === true) {
                    return is_cellular === true ?
                        'tv_low_latency_mode_for_bs4k_cellular' :
                        'tv_low_latency_mode_for_bs4k';
                }
                return is_cellular === true ?
                    'tv_low_latency_mode_cellular' :
                    'tv_low_latency_mode';
            };
            const syncLowLatencyCheckbox = (): void => {
                // サーバー強制の通常バッファ時は実効値（OFF）を表示する
                if (force_stable === true) {
                    low_latency_input.checked = false;
                    return;
                }
                if (this.isKonomiTVBS4KAutoQualityModeEnabled() === true) {
                    low_latency_input.checked =
                        (this.konomitv_bs4k_auto_session_override.low_latency ??
                            resolveAutoLowLatencyFromNetwork()) === true;
                    return;
                }
                low_latency_input.checked =
                    settings_store.settings[getActiveLowLatencySettingKey()] === true;
            };
            syncLowLatencyCheckbox();
            if (force_stable === true) {
                low_latency_button.classList.add('dplayer-setting-item--disabled');
                low_latency_button.style.opacity = '0.45';
                low_latency_button.style.cursor = 'default';
                low_latency_button.addEventListener('click', () => {
                    Message.warning(
                        'サーバー設定により BS4K は通常バッファで再生されています。',
                    );
                });
            } else {
                low_latency_button.addEventListener('click', () => {
                    // 自動モード中は設定キーへ書かず、セッション一時オーバーライドのみ更新する。
                    if (this.isKonomiTVBS4KAutoQualityModeEnabled() === true) {
                        const current = this.konomitv_bs4k_auto_session_override.low_latency ??
                            resolveAutoLowLatencyFromNetwork();
                        const next_enabled = current !== true;
                        this.konomitv_bs4k_auto_session_override = {
                            ...this.konomitv_bs4k_auto_session_override,
                            low_latency: next_enabled,
                        };
                        low_latency_input.checked = next_enabled;
                        this.player?.setting.hide();
                        // destroy なしで liveSync / バッファ位置だけ切り替える
                        this.applySoftLiveLowLatencyMode(
                            next_enabled,
                            next_enabled === true ?
                                '低遅延モードをオンにしました。（一時変更・非保存）' :
                                '低遅延モードをオフにしました。（一時変更・非保存）',
                        );
                        return;
                    }
                    const setting_key = getActiveLowLatencySettingKey();
                    const next_enabled = settings_store.settings[setting_key] !== true;
                    settings_store.settings[setting_key] = next_enabled;
                    low_latency_input.checked = next_enabled;
                    // soft 切替は setupLivePlaybackPolicyWatcher 側で行う
                });
            }
        }

        // 自動画質選択モードを現在の回線プロファイルの設定キーへ保存する
        const konomitv_bs4k_auto_quality_button = this.player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-auto-quality',
        )!;
        const konomitv_bs4k_auto_quality_input = konomitv_bs4k_auto_quality_button.querySelector<HTMLInputElement>(
            '.dplayer-konomitv-bs4k-auto-quality-setting-input',
        )!;
        const getActiveAutoQualityModeSettingKey = ():
        'konomitv_bs4k_playback_auto_quality_mode' |
        'konomitv_bs4k_playback_auto_quality_mode_cellular' => {
            return this.quality_profile_type === 'Cellular' ?
                'konomitv_bs4k_playback_auto_quality_mode_cellular' :
                'konomitv_bs4k_playback_auto_quality_mode';
        };
        const syncAutoQualityModeCheckbox = (): void => {
            konomitv_bs4k_auto_quality_input.checked =
                settings_store.settings[getActiveAutoQualityModeSettingKey()] === true;
        };
        syncAutoQualityModeCheckbox();
        konomitv_bs4k_auto_quality_button.addEventListener('click', () => {
            const setting_key = getActiveAutoQualityModeSettingKey();
            const next_enabled = settings_store.settings[setting_key] !== true;
            settings_store.settings[setting_key] = next_enabled;
            konomitv_bs4k_auto_quality_input.checked = next_enabled;
            // 自動へ戻すときは一時変更を破棄し、OFF にしたときも同じ Controller の override を空にする
            this.konomitv_bs4k_auto_session_override = {};
            this.konomitv_bs4k_force_recorded_aac_audio_codec = false;
            this.konomitv_bs4k_compatibility_playback_fallback_attempted = false;
            this.player?.setting.hide();
            player_store.event_emitter.emit('PlayerRestartRequired', {
                message: next_enabled === true ?
                    '自動画質選択をオンにしました。' :
                    '自動画質選択をオフにしました。手動の画質・コーデック設定を使います。',
                message_delay_seconds:
                    this.resolveLivePlaybackPolicy().live_sync_enabled || this.playback_mode === 'Video' ? 2 : 4.5,
                is_error_message: false,
                should_resume_quality: false,
            });
        });

        // デフォルトのチェック状態を画質プロファイルタイプに合わせる
        const konomitv_bs4k_toggle_mobile_profile_input = this.player.container.querySelector<HTMLInputElement>(
            '.dplayer-konomitv-bs4k-mobile-profile-setting-input',
        )!;
        konomitv_bs4k_toggle_mobile_profile_input.checked = this.quality_profile_type === 'Cellular';
        // モバイル回線プロファイルに切り替えるボタンがクリックされた時のイベントハンドラーを登録
        const konomitv_bs4k_toggle_mobile_profile_button = this.player.container.querySelector(
            '.dplayer-konomitv-bs4k-setting-mobile-profile',
        )!;
        konomitv_bs4k_toggle_mobile_profile_button.addEventListener('click', () => {
            // チェックボックスの状態を切り替える
            konomitv_bs4k_toggle_mobile_profile_input.checked =
                !konomitv_bs4k_toggle_mobile_profile_input.checked;
            // 回線プロファイルが変わった場合は、切り替え先の保存設定を改めて評価する。
            this.konomitv_bs4k_force_recorded_aac_audio_codec = false;
            this.konomitv_bs4k_compatibility_playback_fallback_attempted = false;
            // 画質プロファイルをモバイル回線向けに切り替えてから、プレイヤーを再起動
            if (konomitv_bs4k_toggle_mobile_profile_input.checked) {
                this.quality_profile_type = 'Cellular';
                player_store.selected_quality_profile_type = this.quality_profile_type;
                // 回線プロファイル切替時は一時変更を破棄し、切替先の自動/手動設定を読み直す
                this.konomitv_bs4k_auto_session_override = {};
                syncAutoQualityModeCheckbox();
                updateKonomiTVBS4KVideoCodecDisplay();
                updateKonomiTVBS4KAudioCodecDisplay();
                player_store.event_emitter.emit('PlayerRestartRequired', {
                    message: 'モバイル回線向けの画質プロファイルに切り替えました。',
                    // 他の通知と被らないように、メッセージを遅らせて表示する
                    // プロファイル変更後に開始する次セッションの低遅延ポリシーを基準にする
                    message_delay_seconds:
                        this.resolveLivePlaybackPolicy().live_sync_enabled || this.playback_mode === 'Video' ? 2 : 4.5,
                    is_error_message: false,
                    // モバイル回線プロファイル切り替え時、切り替え後の画質プロファイルのデフォルト画質を優先する
                    should_resume_quality: false,
                });
            // 画質プロファイルを Wi-Fi 回線向けに切り替えてから、プレイヤーを再起動
            } else {
                this.quality_profile_type = 'Wi-Fi';
                player_store.selected_quality_profile_type = this.quality_profile_type;
                this.konomitv_bs4k_auto_session_override = {};
                syncAutoQualityModeCheckbox();
                updateKonomiTVBS4KVideoCodecDisplay();
                updateKonomiTVBS4KAudioCodecDisplay();
                player_store.event_emitter.emit('PlayerRestartRequired', {
                    message: 'Wi-Fi 回線向けの画質プロファイルに切り替えました。',
                    // 他の通知と被らないように、メッセージを遅らせて表示する
                    // プロファイル変更後に開始する次セッションの低遅延ポリシーを基準にする
                    message_delay_seconds:
                        this.resolveLivePlaybackPolicy().live_sync_enabled || this.playback_mode === 'Video' ? 2 : 4.5,
                    is_error_message: false,
                    // Wi-Fi プロファイル切り替え時、切り替え後の画質プロファイルのデフォルト画質を優先する
                    should_resume_quality: false,
                });
            }
        });

        // 設定パネルにL字画面のクロップ設定を表示するボタンを動的に追加する
        this.player.template.settingOriginPanel.insertAdjacentHTML('beforeend', `
            <div class="dplayer-setting-item dplayer-setting-lshaped-screen-crop">
                <span class="dplayer-label">Ｌ字画面のクロップ</span>
                <div class="dplayer-toggle">
                    <svg xmlns="http://www.w3.org/2000/svg" version="1.1" viewBox="0 0 32 32">
                        <path d="M22 16l-10.105-10.6-1.895 1.987 8.211 8.613-8.211 8.612 1.895 1.988 8.211-8.613z"></path>
                    </svg>
                </div>
            </div>
        `);

        // L字画面のクロップ設定モーダルを表示するボタンがクリックされたときのイベントハンドラーを登録
        this.player.template.settingOriginPanel.querySelector('.dplayer-setting-lshaped-screen-crop')!.addEventListener('click', () => {
            assert(this.player !== null);
            // 設定パネルを閉じる
            this.player.setting.hide();
            // L字画面のクロップ設定モーダルを表示する
            player_store.lshaped_screen_crop_settings_modal = true;
        });

        // 設定パネルにショートカット一覧を表示するボタンを動的に追加する
        // スマホなどのタッチデバイスでは基本キーボードが使えないため、タッチデバイスの場合はボタンを表示しない
        if (Utils.isTouchDevice() === false) {
            this.player.template.settingOriginPanel.insertAdjacentHTML('beforeend', `
                <div class="dplayer-setting-item dplayer-setting-keyboard-shortcut">
                    <span class="dplayer-label">キーボードショートカット</span>
                    <div class="dplayer-toggle">
                        <svg xmlns="http://www.w3.org/2000/svg" version="1.1" viewBox="0 0 32 32">
                            <path d="M22 16l-10.105-10.6-1.895 1.987 8.211 8.613-8.211 8.612 1.895 1.988 8.211-8.613z"></path>
                        </svg>
                    </div>
                </div>
            `);

            // ショートカット一覧モーダルを表示するボタンがクリックされたときのイベントハンドラーを登録
            this.player.template.settingOriginPanel.querySelector('.dplayer-setting-keyboard-shortcut')!.addEventListener('click', () => {
                assert(this.player !== null);
                // 設定パネルを閉じる
                this.player.setting.hide();
                // ショートカットキー一覧モーダルを表示する
                player_store.shortcut_key_modal = true;
            });
        }

        this.applyAudioTrackLabels();
    }


    /*
     * L字画面のクロップ設定に応じて映像のクロップを変更する
     */
    private setupLShapedScreenCropHandler(): void {
        assert(this.player !== null);
        const settings_store = useSettingsStore();

        // リサイズ対象の映像要素
        let video_element = this.player.video;
        // 画質切り替え後に新しい映像要素が生成されるため、画質切り替え後にリサイズ対象を更新する
        this.player.on('quality_end', () => {
            if (!this.player || !this.player.video) {
                return;
            }
            video_element = this.player.video;
            crop();
        });

        // 現在の設定状態を DOM に反映する関数
        // 以前 TVRemotePlus で実装した際のコードをほぼそのまま移植した
        // ref: https://github.com/tsukumijima/TVRemotePlus/blob/master/htdocs/files/index.js#L410-L536
        const crop = () => {

            // L字画面のクロップが無効なときはスタイルを削除
            if (settings_store.settings.lshaped_screen_crop_enabled === false) {
                video_element.style.position = '';
                video_element.style.transform = '';
                video_element.style.transformOrigin = '';
                return;
            }

            // 現在の設定値を取得
            const lshaped_screen_crop_zoom_level = settings_store.settings.lshaped_screen_crop_zoom_level;
            const lshaped_screen_crop_x_position = settings_store.settings.lshaped_screen_crop_x_position;
            const lshaped_screen_crop_y_position = settings_store.settings.lshaped_screen_crop_y_position;
            const lshaped_screen_crop_zoom_origin = settings_store.settings.lshaped_screen_crop_zoom_origin;

            // 全てデフォルト（オフ）状態ならスタイルを削除
            // 空文字を入れると style 属性から当該スタイルが除去される
            if (lshaped_screen_crop_zoom_level === 100 && lshaped_screen_crop_x_position === 0 && lshaped_screen_crop_y_position === 0) {
                video_element.style.position = '';
                video_element.style.transform = '';
                video_element.style.transformOrigin = '';
            } else {
                // transform をクリア
                video_element.style.position = 'relative';
                video_element.style.transform = '';

                // 拡大起点別に
                switch (lshaped_screen_crop_zoom_origin) {
                    // 右上
                    case 'TopRight': {
                        // 拡大起点を右上に設定
                        video_element.style.transformOrigin = 'right top';
                        // 動画の表示サイズを 100% として、拡大率を超えない範囲で座標をずらす
                        video_element.style.transform += `translateX(${(lshaped_screen_crop_zoom_level - 100) * (lshaped_screen_crop_x_position / 100)}%) `;
                        video_element.style.transform += `translateY(-${(lshaped_screen_crop_zoom_level - 100) * (lshaped_screen_crop_y_position / 100)}%) `;
                        break;
                    }
                    // 右下
                    case 'BottomRight': {
                        // 拡大起点を右下に設定
                        video_element.style.transformOrigin = 'right bottom';
                        // 動画の表示サイズを 100% として、拡大率を超えない範囲で座標をずらす
                        video_element.style.transform += `translateX(${(lshaped_screen_crop_zoom_level - 100) * (lshaped_screen_crop_x_position / 100)}%) `;
                        video_element.style.transform += `translateY(${(lshaped_screen_crop_zoom_level - 100) * (lshaped_screen_crop_y_position / 100)}%) `;
                        break;
                    }
                    // 左上
                    case 'TopLeft': {
                        // 拡大起点を左上に設定
                        video_element.style.transformOrigin = 'left top';
                        // 動画の表示サイズを 100% として、拡大率を超えない範囲で座標をずらす
                        video_element.style.transform += `translateX(-${(lshaped_screen_crop_zoom_level - 100) * (lshaped_screen_crop_x_position / 100)}%) `;
                        video_element.style.transform += `translateY(-${(lshaped_screen_crop_zoom_level - 100) * (lshaped_screen_crop_y_position / 100)}%) `;
                        break;
                    }
                    // 左下
                    case 'BottomLeft': {
                        // 拡大起点を左下に設定
                        video_element.style.transformOrigin = 'left bottom';
                        // 動画の表示サイズを 100% として、拡大率を超えない範囲で座標をずらす
                        video_element.style.transform += `translateX(-${(lshaped_screen_crop_zoom_level - 100) * (lshaped_screen_crop_x_position / 100)}%) `;
                        video_element.style.transform += `translateY(${(lshaped_screen_crop_zoom_level - 100) * (lshaped_screen_crop_y_position / 100)}%) `;
                        break;
                    }
                }

                // video 要素を拡大
                // transform は後ろから適用されるため、先にリサイズしておかないと正しく座標をずらせない
                // ref: https://techblog.kayac.com/css-transform-tips
                video_element.style.transform += `scale(${lshaped_screen_crop_zoom_level / 100})`;
            }
        };

        // 初回実行
        crop();

        // 設定値が変更されたときに実行
        this.lshaped_screen_crop_watchers = [
            watch(() => settings_store.settings.lshaped_screen_crop_enabled, crop, { immediate: true }),
            watch(() => settings_store.settings.lshaped_screen_crop_zoom_level, crop, { immediate: true }),
            watch(() => settings_store.settings.lshaped_screen_crop_x_position, crop, { immediate: true }),
            watch(() => settings_store.settings.lshaped_screen_crop_y_position, crop, { immediate: true }),
            watch(() => settings_store.settings.lshaped_screen_crop_zoom_origin, crop, { immediate: true }),
        ];
    }


    /**
     * KonomiTV 本体の UI を含むプレイヤー全体のコンテナ要素がリサイズされたときのイベントハンドラーを登録する
     */
    private setupPlayerContainerResizeHandler(): void {

        // 監視対象のプレイヤー全体のコンテナ要素
        const player_container_element = document.querySelector('.watch-player')!;

        // プレイヤー全体のコンテナ要素がリサイズされた際に発火するイベント
        const resize_handler = () => {

            // コメント描画領域の要素
            if (this.player === null) return;
            const comment_area_element = this.player.danmaku!.container;

            // コメント描画領域の幅から算出した、映像の要素の幅/高さ (px)
            // 実際の映像の要素の幅は BML ブラウザの ShadowDOM 内に入ると正確な算出ができないため、代わりにコメント描画領域の幅を使って算出する
            const video_element_width = comment_area_element.clientWidth;
            const video_element_height = comment_area_element.clientWidth * (9 / 16);

            // プレイヤー全体と映像の高さの差（レターボックス）から、コメント描画領域の高さを狭める必要があるかを判定する
            // 2で割っているのは単体の差を測るため
            if (player_container_element === null || player_container_element.clientHeight === null) return;
            const letter_box_height = (player_container_element.clientHeight - video_element_height) / 2;

            // コメント描画領域の高さがしきい値より小さい場合、コメント描画領域のアスペクト比を狭める
            // しきい値はデバイスの画面サイズや向きによって異なる
            // スマホ縦画面ではコメント描画領域を狭める必要がある上部のヘッダーがないため、しきい値を 0 にする
            const threshold = Utils.isSmartphoneVertical() ? 0 : Utils.isSmartphoneHorizontal() ? 50 : 66;
            if (letter_box_height < threshold) {

                // コメント描画領域に必要な上下マージン
                const comment_area_vertical_margin = (threshold - letter_box_height) * 2;

                // 狭めるコメント描画領域の幅
                // 映像の要素の幅をそのまま利用する
                const comment_area_width = video_element_width;

                // 狭めるコメント描画領域の高さ
                const comment_area_height = video_element_height - comment_area_vertical_margin;

                // 狭めるコメント描画領域のアスペクト比を求める
                // https://tech.arc-one.jp/asepct-ratio/
                const gcd = (x: number, y: number) => {  // 最大公約数を求める関数
                    if (y === 0) return x;
                    return gcd(y, x % y);
                };
                // 幅と高さの最大公約数を求める
                const gcd_result = gcd(comment_area_width, comment_area_height);
                // 幅と高さをそれぞれ最大公約数で割ってアスペクト比を算出
                const comment_area_height_aspect = `${comment_area_width / gcd_result} / ${comment_area_height / gcd_result}`;

                // 一時的に transition を無効化する
                // アスペクト比の設定は連続して行われるが、その際に transition が適用されるとワンテンポ遅れたアニメーションになってしまう
                comment_area_element.style.transition = 'none';

                // コメント描画領域に算出したアスペクト比を設定する
                comment_area_element.style.setProperty('--comment-area-aspect-ratio', comment_area_height_aspect);

                // コメント描画領域に必要な上下マージンを設定する
                comment_area_element.style.setProperty('--comment-area-vertical-margin', `${comment_area_vertical_margin}px`);

                // 0.2秒後に再び transition を有効化する
                // 0.2秒より前にもう一度リサイズイベントが来た場合はタイマーがクリアされるため実行されない
                window.setTimeout(() => comment_area_element.style.transition = '', 0.2 * 1000);

            } else {

                // コメント描画領域に設定したアスペクト比・上下マージンを削除する
                comment_area_element.style.removeProperty('--comment-area-aspect-ratio');
                comment_area_element.style.removeProperty('--comment-area-vertical-margin');
            }
        };

        // 初回実行
        resize_handler();

        // 要素の監視を開始
        this.player_container_resize_observer = new ResizeObserver(resize_handler);
        this.player_container_resize_observer.observe(player_container_element);
    }


    /**
     * 一定の条件に基づいてプレイヤーのコントロール UI の表示状態を切り替える
     * マウスが動いたりタップされた時に実行するタイマー関数で、3秒間何も操作がなければプレイヤーのコントロール UI を非表示にする
     * 本来は View 側に実装すべきだが、プレイヤー側のロジックとも密接に関連しているため PlayerController に実装した
     * @param event マウスやタッチイベント (手動実行する際は省略する)
     * @param is_player_region_event プレイヤー画面の中で発火したイベントなら true に設定する
     * @param timeout_seconds 何も操作がない場合にコントロール UI を非表示にするまでの秒数
     */
    private setControlDisplayTimer(
        event: Event | null = null,
        is_player_region_event: boolean = false,
        timeout_seconds: number = 3,
    ): void {
        const player_store = usePlayerStore();

        // タッチデバイスで mousemove 、あるいはタッチデバイス以外で touchmove か click が発火した時は実行じない
        if (Utils.isTouchDevice() === true  && event !== null && (event.type === 'mousemove')) return;
        if (Utils.isTouchDevice() === false && event !== null && (event.type === 'touchmove' || event.type === 'click')) return;

        // 以前セットされたタイマーを止める
        window.clearTimeout(this.player_control_ui_hide_timer_id);

        // 実行された際にプレイヤーのコントロール UI を非表示にするタイマー関数 (setTimeout に渡すコールバック関数)
        const player_control_ui_hide_timer = () => {

            // 万が一実行されたタイミングですでに DPlayer が破棄されていたら何もしない
            if (this.player === null) return;

            // コメント入力フォームが表示されているときは実行しない
            // タイマーを掛け直してから抜ける
            if (this.player.template.controller.classList.contains('dplayer-controller-comment')) {
                this.player_control_ui_hide_timer_id =
                    window.setTimeout(player_control_ui_hide_timer, timeout_seconds * 1000);  // 3秒後に再実行
                return;
            }

            // 設定パネルが開いている間は、操作中にコントロール UI を閉じない
            // 画質や音声などの設定パネルはマウス移動やタッチ操作が止まっても、ユーザーが閉じるまで表示を続ける
            if (player_store.is_player_setting_panel_open === true) {
                this.player_control_ui_hide_timer_id =
                    window.setTimeout(player_control_ui_hide_timer, timeout_seconds * 1000);
                return;
            }

            // コントロールを非表示にする
            player_store.is_control_display = false;

            // プレイヤーのコントロールと設定パネルを非表示にする
            this.player.controller.hide();
            this.player.setting.hide();
        };

        // 万が一実行されたタイミングですでに DPlayer が破棄されていたら何もしない
        if (this.player === null) return;

        // タッチデバイスかつプレイヤー画面の中がタップされたとき
        if (Utils.isTouchDevice() === true && is_player_region_event === true) {

            // DPlayer 側のコントロール UI の表示状態に合わせる
            if (this.player.controller.isShow()) {

                // コントロールを表示する
                player_store.is_control_display = true;

                // プレイヤーのコントロールを表示する
                this.player.controller.show();

                // 3秒間何も操作がなければコントロールを非表示にする
                // 3秒間の間一度でもタッチされればタイマーが解除されてやり直しになる
                this.player_control_ui_hide_timer_id =
                    window.setTimeout(player_control_ui_hide_timer, timeout_seconds * 1000);

            } else {

                // コントロール UI を非表示にする
                player_store.is_control_display = false;

                // DPlayer 側のコントロール UI と設定パネルを非表示にする
                this.player.controller.hide();
                this.player.setting.hide();
            }

        // それ以外の画面がクリックされたとき
        } else {

            // コントロール UI を表示する
            player_store.is_control_display = true;

            // DPlayer 側のコントロール UI を表示する
            this.player.controller.show();

            // 3秒間何も操作がなければコントロールを非表示にする
            // 3秒間の間一度でもマウスが動けばタイマーが解除されてやり直しになる
            this.player_control_ui_hide_timer_id =
                window.setTimeout(player_control_ui_hide_timer, timeout_seconds * 1000);
        }
    }


    /**
     * 手動・自動のプレイヤー再起動要求を、進行中の同じ再起動処理へ合流させる
     * @param restart_player 実際の再起動処理
     */
    private restartPlayer(restart_player: () => Promise<void>): Promise<void> {

        // すでに再起動中なら、完了まで同じ Promise を待つ
        if (this.restart_promise !== null) {
            return this.restart_promise;
        }

        // 成功・失敗にかかわらず、完了後は次の再起動要求を受け付けられるようにする
        const restart_promise = restart_player().finally(() => {
            if (this.restart_promise === restart_promise) {
                this.restart_promise = null;
            }
        });
        this.restart_promise = restart_promise;
        return restart_promise;
    }

    /**
     * DPlayer と PlayerManager を破棄し、再生を終了する
     * 常に init() で作成したものが destroy() ですべてクリーンアップされるように実装すべき
     * PlayerController の再起動を行う場合、基本外部から直接 await destroy() と await init() は呼び出さず、代わりに
     * player_store.event_emitter.emit('PlayerRestartRequired', 'プレイヤーを再起動しています…') のようにイベントを発火させるべき
     */
    public destroy(): Promise<void> {

        // すでに進行中の破棄処理があれば、完了まで同じ Promise を待つ
        if (this.destroy_promise !== null) {
            return this.destroy_promise;
        }

        // すでに破棄されている場合は何もしない
        if (this.destroyed === true) {
            if (this.destroy_error !== null) {
                return Promise.reject(this.destroy_error);
            }
            return Promise.resolve();
        }

        // destroy() を呼んだ時点で低遅延設定ウォッチャーを解除し、破棄開始後の変更から再起動要求が発生しないようにする。
        if (this.live_playback_policy_watcher_cancel !== null) {
            this.live_playback_policy_watcher_cancel();
            this.live_playback_policy_watcher_cancel = null;
        }

        // destroy() を呼んだ時点で旧世代の timer / loop を同期的に中断する。
        // 実リソース回収は進行中 init の停止確認後に行う。
        this.live_quality_manager_restart_generation += 1;
        // Manager の全破棄やフェードを待つ間にも B が Commit しないよう、Coordinator は同期的に止める。
        // active mpegts.js の破棄後に off() しないよう、字幕 listener の所有権を先に解放する。
        this.detachLiveARIBTTMLStream();
        const coordinator_cleanup =
            this.konomitv_bs4k_live_quality_switch_coordinator?.destroy();
        // 実際の成否はdestroyInternal()で同じPromiseをawaitする。ここでは同期開始した
        // Promiseへhandlerを付け、fire-and-forget区間のunhandled rejectionだけを防ぐ。
        if (coordinator_cleanup !== undefined) {
            void coordinator_cleanup.catch(() => undefined);
        }
        this.lifecycle_abort_controller.abort();
        ++this.lifecycle_generation;

        // 成功・失敗にかかわらず、破棄中フラグと Promise を解除する
        const destroy_promise = this.destroyAfterInitialization()
            .catch((error: unknown) => {
                if (this.konomitv_bs4k_live_cleanup_blocked === true) {
                    this.destroy_error = error;
                }
                throw error;
            })
            .finally(() => {
                this.destroying = false;
                if (this.destroy_promise === destroy_promise) {
                    this.destroy_promise = null;
                }
            });
        this.destroy_promise = destroy_promise;
        return destroy_promise;
    }

    /**
     * 進行中の初期化が世代無効化を確認するまで待ってから、実リソースを回収する
     */
    private async destroyAfterInitialization(): Promise<void> {
        const init_promise = this.init_promise;
        if (init_promise !== null) {
            try {
                await init_promise;
            } catch {
                // 初期化失敗時も、途中まで作成済みのリソース回収を続行する
            }
        }
        await this.destroyInternal();
    }

    /**
     * Coordinator のlocal停止は public destroy() から同期開始済み。
     * ここではServer cleanup barrier完了を待ってから参照とDPlayer pluginを手放す。
     */
    private async destroyKonomiTVBS4KLiveQualitySwitchCoordinator(): Promise<void> {
        const coordinator = this.konomitv_bs4k_live_quality_switch_coordinator;
        if (coordinator === null) return;
        try {
            await coordinator.destroy();
        } catch (error) {
            this.konomitv_bs4k_live_cleanup_blocked = true;
            throw error;
        }
        if (this.konomitv_bs4k_live_quality_switch_coordinator !== coordinator) return;
        this.konomitv_bs4k_live_quality_switch_coordinator = null;
        if (this.player !== null) {
            delete this.player.plugins.mpegts;
        }
    }

    /**
     * DPlayer と PlayerManager の実際の破棄処理
     */
    private async destroyInternal(): Promise<void> {
        const settings_store = useSettingsStore();
        const player_store = usePlayerStore();
        let live_cleanup_error: unknown = null;

        this.destroying = true;

        // 視聴履歴の最終位置を更新
        // 現在の再生位置を取得するため、プレイヤーの破棄前に実行する必要がある
        if (this.playback_mode === 'Video' && this.player && this.player.video) {
            const history_index = settings_store.settings.watched_history.findIndex(
                history => history.video_id === player_store.recorded_program.id
            );
            if (history_index !== -1) {
                // 完走済みなら次回は本編開始付近から再生する。末尾を保存すると、再訪時に即座に
                // ended が再発火して次話へ飛ぶため、録画マージン直後かつ動画長の内側へ収める。
                // 通常の離脱では従来どおり、再開しやすいよう現在位置の10秒前を保存する。
                const video_duration = player_store.recorded_program.recorded_video.duration;
                const completed_resume_position = Math.min(
                    Math.max(player_store.recorded_program.recording_start_margin + 2, 0),
                    Math.max(video_duration - 0.1, 0),
                );
                const current_time = this.recorded_playback_ended ?
                    completed_resume_position : this.player.video.currentTime - 10;
                settings_store.settings.watched_history[history_index].last_playback_position = current_time;
                settings_store.settings.watched_history[history_index].updated_at = Utils.time();
                console.log(`\u001b[31m[PlayerController] Last playback position updated. (Video ID: ${player_store.recorded_program.id}, last_playback_position: ${current_time})`);
            }
        }

        console.log('\u001b[31m[PlayerController] Destroying...');

        // Commit/Rollback 後の Manager 再初期化鎖を止めてから、全 Manager を最終破棄する。
        try {
            await this.live_quality_manager_restart_chain;
        } catch (error) {
            console.warn('[PlayerController] Waiting for live quality manager restart failed.', error);
        }

        // 登録されている PlayerManager をすべて破棄
        // CSS アニメーションの関係上、ローディング状態にする前に破棄する必要がある (特に LiveDataBroadcastingManager)
        // 同期処理すると時間が掛かるので、並行して実行する
        await Promise.all(this.player_managers.map(async (player_manager) => player_manager.destroy()));
        this.player_managers = [];

        // Screen Wake Lock API で確保した起動ロックを解放
        // 起動ロックが確保できていない場合は何もしない
        if (this.screen_wake_lock !== null) {
            this.screen_wake_lock.release();
            this.screen_wake_lock = null;
            console.log('\u001b[31m[PlayerController] Screen Wake Lock API: Screen Wake Lock released.');
        }

        // ローディング中の背景写真を隠す
        player_store.is_background_display = false;

        // 再びローディング状態にする
        player_store.is_loading = true;

        // コメントの取得に失敗した際のエラーメッセージを削除
        player_store.live_comment_init_failed_message = null;
        player_store.video_comment_init_failed_message = null;

        // 映像がフェードアウトするアニメーション (0.2秒) 分待ってから実行
        // この 0.2 秒の間に音量をフェードアウトさせる
        if (this.player !== null) {
            // 0.2 秒間かけて current_volume から 0 まで音量を下げる
            const current_volume = this.player.user.get('volume');  // 0.0 ~ 1.0 の範囲
            const volume_step = current_volume / 10;
            for (let i = 0; i < 10; i++) {  // 10 回に分けて音量を下げる
                await Utils.sleep(0.2 / 10);
                // ごく稀に映像が既に破棄されている or まだ再生開始されていない場合がある (?) ので、その場合は実行しない
                if (this.player && this.player.video) {
                    // 音量が 0 より小さくならないようにする
                    // 浮動小数点絡みの問題 (丸め誤差) が出るため小数第3位で切り捨てる
                    this.player.video.volume = Math.max(Utils.mathFloor(this.player.video.volume - volume_step, 3), 0);
                }
            }
            // 最後に音量を 0 に設定
            // 上記ロジックでは丸め誤差の関係で完全に 0 とは一致しないことがあるため
            this.player.video.volume = 0;
        }

        // タイマーを破棄
        if (this.live_force_seek_interval_timer_cancel !== null) {
            this.live_force_seek_interval_timer_cancel();
            this.live_force_seek_interval_timer_cancel = null;
        }
        if (this.live_audio_track_interval_timer_cancel !== null) {
            this.live_audio_track_interval_timer_cancel();
            this.live_audio_track_interval_timer_cancel = null;
        }
        if (this.video_keep_alive_interval_timer_cancel !== null) {
            this.video_keep_alive_interval_timer_cancel();
            this.video_keep_alive_interval_timer_cancel = null;
        }
        this.stopAutoQualityStepDownMonitor();
        // destroy() と並行していた init() が同期解除後にウォッチャーを登録した場合も、実リソース回収時に確実に解除する。
        if (this.live_playback_policy_watcher_cancel !== null) {
            this.live_playback_policy_watcher_cancel();
            this.live_playback_policy_watcher_cancel = null;
        }
        if (this.recorded_arib_subtitle_cancel !== null) {
            this.recorded_arib_subtitle_cancel();
            this.recorded_arib_subtitle_cancel = null;
        }
        if (this.recorded_arib_ttml_cancel !== null) {
            this.recorded_arib_ttml_cancel();
            this.recorded_arib_ttml_cancel = null;
        }
        this.recorded_arib_subtitle_restart = null;
        this.recorded_arib_ttml_restart = null;
        // init() と destroy() が競合して listener が後から登録された場合も、Coordinator より先に解除する。
        this.detachLiveARIBTTMLStream();
        try {
            await this.destroyKonomiTVBS4KLiveQualitySwitchCoordinator();
        } catch (error) {
            // Server cleanup未確認は新init禁止として保持する一方、他のlocal resource回収は最後まで続行する。
            live_cleanup_error = error;
            console.error('[PlayerController] Live playback cleanup barrier failed.', error);
        }
        this.arib_ttml_renderer?.dispose();
        this.arib_ttml_renderer = null;
        if (this.player !== null) {
            delete (this.player.plugins as unknown as {aribTTML?: ARIBTTMLRenderer}).aribTTML;
        }
        this.live_media_info = null;
        window.clearTimeout(this.watched_history_threshold_timer_id);
        window.clearTimeout(this.player_control_ui_hide_timer_id);

        // プレイヤー全体のコンテナ要素の監視を停止
        if (this.player_container_resize_observer !== null) {
            this.player_container_resize_observer.disconnect();
            this.player_container_resize_observer = null;
        }

        // L字画面のクロップ設定で使うウォッチャーを破棄
        if (this.lshaped_screen_crop_watchers.length > 0) {
            this.lshaped_screen_crop_watchers.forEach((unwatcher) => unwatcher());
            this.lshaped_screen_crop_watchers = [];
        }

        // DPlayer 本体を破棄
        // なぜか例外が出ることがあるので try-catch で囲む
        if (this.player !== null) {
            // プレイヤーの破棄を実行する前に、DPlayer 側に登録された HTMLVideoElement の error イベントハンドラーを全て削除
            // Safari のみ、削除しておかないと「動画の読み込みに失敗しました」というエラーが発生する
            if (this.player.events.events['error']) {
                this.player.events.events['error'] = [];
            }
            // 通常 this.player.destroy() が実行された後 mpegts.js も自動的に破棄されるのだが、Safari のみ
            // なぜか video.src = '' を実行した後に mpegts.js を破棄するとエラーというか挙動不審になるので、
            // あえて mpegts.js を明示的に先に破棄しておいて Safari の地雷を回避する
            if (this.player.plugins.mpegts) {
                try {
                    this.player.plugins.mpegts.unload();
                    this.player.plugins.mpegts.detachMediaElement();
                    this.player.plugins.mpegts.destroy();
                } catch (e) {
                    // 何もしない
                }
            }
            // 引数に true を指定して、破棄後も DPlayer 側の HTML 要素を保持する
            // これにより、チャンネルを切り替えるなどして再度初期化されるまでの僅かな間もプレイヤーのコントロール UI が表示される (動作はしない)
            // ここで HTML 要素を削除してしまうと、プレイヤーのコントロール UI が一瞬削除されることでちらつきが発生して見栄えが悪い
            // HTML 要素を保持する分、破棄中に描画されていたコメントも残ってしまうので、破棄前にコメントを全て削除する
            this.player.danmaku!.clear();
            try {
                this.player.destroy(true);
            } catch (e) {
                // 何もしない
            }
            this.player = null;
            this.konomitv_bs4k_native_playback_error_handler_registration = null;
        }

        // 破棄済みかどうかのフラグを立てる
        this.destroying = false;
        this.destroyed = true;

        // PlayerStore にプレイヤーを破棄したことを通知
        player_store.is_player_initialized = false;

        console.log('\u001b[31m[PlayerController] Destroyed.');
        if (live_cleanup_error !== null) throw live_cleanup_error;
    }
}

export default PlayerController;
