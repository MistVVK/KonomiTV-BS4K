
import assert from 'assert';

import CanvasRenderer from 'aribb24.js/src/canvas-renderer';
import DPlayer, { DPlayerType } from 'dplayer';
import Hls, { ErrorDetails } from 'hls.js';
import mpegts from 'mpegts.js';
import { watch } from 'vue';

import APIClient from '@/services/APIClient';
import ARIBTTMLRenderer from '@/services/player/ARIBTTMLRenderer';
import CustomBufferController from '@/services/player/CustomBufferController';
import KonomiTVBS4KPlaybackRestartGuard from '@/services/player/KonomiTVBS4KPlaybackRestartGuard';
import CaptureManager from '@/services/player/managers/CaptureManager';
import DocumentPiPManager from '@/services/player/managers/DocumentPiPManager';
import KeyboardShortcutManager from '@/services/player/managers/KeyboardShortcutManager';
import LiveCommentManager from '@/services/player/managers/LiveCommentManager';
import LiveDataBroadcastingManager from '@/services/player/managers/LiveDataBroadcastingManager';
import LiveEventManager from '@/services/player/managers/LiveEventManager';
import MediaSessionManager from '@/services/player/managers/MediaSessionManager';
import RecordedCMSkipManager from '@/services/player/managers/RecordedCMSkipManager';
import PlayerManager from '@/services/player/PlayerManager';
import Videos from '@/services/Videos';
import useChannelsStore from '@/stores/ChannelsStore';
import usePlayerStore from '@/stores/PlayerStore';
import useServerSettingsStore from '@/stores/ServerSettingsStore';
import useSettingsStore, {
    BS4KLiveStreamingQuality,
    BS4K_LIVE_STREAMING_QUALITIES,
    getKonomiTVBS4KPlayback24FPSModeSettingKey,
    getKonomiTVBS4KPlaybackAudioCodecSettingKey,
    getKonomiTVBS4KPlaybackStreamingQualitySettingKey,
    getKonomiTVBS4KPlaybackVideoCodecSettingKey,
    KonomiTVBS4KPlaybackAudioCodec,
    KonomiTVBS4KPlaybackVideoCodec,
    LiveStreamingQuality,
    LIVE_STREAMING_QUALITIES,
    VideoStreamingQuality,
    VIDEO_STREAMING_QUALITIES,
} from '@/stores/SettingsStore';
import Utils, { dayjs, PlayerUtils } from '@/utils';


/**
 * 録画 HLS のセッション ID として使う8桁の16進文字列を生成する。
 * crypto.randomUUID() は非 HTTPS オリジンで公開されないため、非セキュアコンテキストでも
 * 利用可能な getRandomValues() だけを使う。
 */
export function generateRecordedPlaybackSessionID(
    random_source: Pick<Crypto, 'getRandomValues'> = crypto,
): string {
    const random_bytes = random_source.getRandomValues(new Uint8Array(4));
    return Array.from(random_bytes, byte => byte.toString(16).padStart(2, '0')).join('');
}


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

    // ライブ視聴: 低遅延モードオンでの再生バッファ (秒単位)
    // 0.9 秒程度余裕を持たせる
    private static readonly LIVE_PLAYBACK_BUFFER_SECONDS_LOW_LATENCY = 0.9;

    // ライブ視聴: 低遅延モードオフでの再生バッファ (秒単位)
    // 4 秒程度の遅延を許容する
    private static readonly LIVE_PLAYBACK_BUFFER_SECONDS = 4.0;

    // 何秒視聴したら視聴履歴に追加するかの閾値 (秒)
    private static readonly WATCHED_HISTORY_THRESHOLD_SECONDS = 30;

    // 視聴履歴の更新間隔 (秒)
    private static readonly WATCHED_HISTORY_UPDATE_INTERVAL = 10;

    // 録画末尾への直接シークと自然完走を区別するための許容誤差 (秒)
    private static readonly RECORDED_PLAYBACK_END_TOLERANCE_SECONDS = 0.25;

    // CM 自動スキップ先は HLS のタイムライン補正で指定位置からわずかにずれるため、その許容誤差 (秒)
    private static readonly RECORDED_CM_SKIP_TARGET_TOLERANCE_SECONDS = 1.0;

    // DPlayer のインスタンス
    private player: DPlayer | null = null;

    // それぞれの PlayerManager のインスタンスのリスト
    private player_managers: PlayerManager[] = [];

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

    // 破棄中かどうか
    // 破棄中は destroy() が呼ばれても何もしない
    private destroying = false;

    // 破棄済みかどうか
    private destroyed = false;

    // 視聴画面の寿命。route離脱後の能力API応答が共有StoreやDOMを書き戻さないように使う。
    private readonly owner_signal: AbortSignal | null;

    // init()/destroy()ごとに更新する世代。同じcontrollerの再起動中でも古いpreflightを無効化する。
    private initialization_generation = 0;

    // 同一再生対象・同一 codec tuple の短時間再起動ループを、init() をまたいで検出する。
    // codec を自動変更せず、最初の同一 codec 再起動でも復旧できなかった場合はユーザーの明示変更へ委ねる。
    private readonly konomitv_bs4k_playback_restart_guard = new KonomiTVBS4KPlaybackRestartGuard();

    // ライブ再生開始時の一時ミュートを、保存済みミュートと区別するフラグ
    // 一時ミュートで発火した volumechange を、ユーザー操作として保存しないために使う
    private is_live_startup_temporary_muted = false;

    // ライブ視聴: 直近の mpegts.js mediaInfo
    private live_media_info: {[key: string]: any} | null = null;


    /**
     * コンストラクタ
     * 実際の DPlayer の初期化処理は await init() で行われる
     */
    constructor(playback_mode: 'Live' | 'Video', owner_signal: AbortSignal | null = null) {

        // 再生モードをセット
        this.playback_mode = playback_mode;
        this.owner_signal = owner_signal;

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


    /**
     * 現在の画質プロファイルタイプに応じた画質プロファイル
     */
    private get quality_profile(): {
        playback_streaming_quality: LiveStreamingQuality;
        bs4k_playback_streaming_quality: BS4KLiveStreamingQuality;
        playback_video_codec: KonomiTVBS4KPlaybackVideoCodec;
        bs4k_playback_video_codec: KonomiTVBS4KPlaybackVideoCodec;
        playback_audio_codec: KonomiTVBS4KPlaybackAudioCodec;
        bs4k_playback_audio_codec: KonomiTVBS4KPlaybackAudioCodec;
        playback_24fps_mode: boolean;
        tv_low_latency_mode: boolean;
    } {
        const settings_store = useSettingsStore();
        const is_cellular = this.quality_profile_type === 'Cellular';
        return {
            playback_streaming_quality: settings_store.settings[
                getKonomiTVBS4KPlaybackStreamingQualitySettingKey(false, is_cellular)
            ] as LiveStreamingQuality,
            bs4k_playback_streaming_quality: settings_store.settings[
                getKonomiTVBS4KPlaybackStreamingQualitySettingKey(true, is_cellular)
            ] as BS4KLiveStreamingQuality,
            playback_video_codec: settings_store.settings[
                getKonomiTVBS4KPlaybackVideoCodecSettingKey(false, is_cellular)
            ],
            bs4k_playback_video_codec: settings_store.settings[
                getKonomiTVBS4KPlaybackVideoCodecSettingKey(true, is_cellular)
            ],
            playback_audio_codec: settings_store.settings[
                getKonomiTVBS4KPlaybackAudioCodecSettingKey(false, is_cellular)
            ],
            bs4k_playback_audio_codec: settings_store.settings[
                getKonomiTVBS4KPlaybackAudioCodecSettingKey(true, is_cellular)
            ],
            playback_24fps_mode: settings_store.settings[
                getKonomiTVBS4KPlayback24FPSModeSettingKey(false, is_cellular)
            ],
            // 低遅延はライブ固有機能であり、旧 live/recorded 重複設定ではないため既存キーを維持する。
            tv_low_latency_mode: is_cellular ?
                settings_store.settings.tv_low_latency_mode_cellular :
                settings_store.settings.tv_low_latency_mode,
        };
    }


    /**
     * ライブ視聴: 許容する HTMLMediaElement の内部再生バッファの秒数
     */
    private get live_playback_buffer_seconds(): number {
        // 低遅延モードであれば低遅延向けの再生バッファを、そうでなければ通常の再生バッファ (秒単位)
        let live_playback_buffer_seconds = this.tv_low_latency_mode ?
            PlayerController.LIVE_PLAYBACK_BUFFER_SECONDS_LOW_LATENCY : PlayerController.LIVE_PLAYBACK_BUFFER_SECONDS;
        // Safari の Media Source Extensions API の実装はどうもバッファの揺らぎが大きい (?) ようなので、バッファ詰まり対策で
        // さらに 0.3 秒程度余裕を持たせる
        if (Utils.isSafari() === true) {
            live_playback_buffer_seconds += 0.3;
        }
        return live_playback_buffer_seconds;
    }


    /**
     * ライブ視聴で低遅延モードを有効扱いにするか
     */
    private get tv_low_latency_mode(): boolean {
        if (this.playback_mode !== 'Live') {
            return false;
        }

        const channels_store = useChannelsStore();
        const server_settings_store = useServerSettingsStore();
        if (channels_store.channel.current.display_channel_id.startsWith('bs4k')) {
            if (server_settings_store.server_settings.general.bs4k_ignore_viewer_low_latency === true) {
                return false;
            }
            const settings_store = useSettingsStore();
            return this.quality_profile_type === 'Cellular' ?
                settings_store.settings.tv_low_latency_mode_for_bs4k_cellular :
                settings_store.settings.tv_low_latency_mode_for_bs4k;
        }

        return this.quality_profile.tv_low_latency_mode;
    }


    /** 現在の再生対象と回線プロファイルを、pinを分離する安定キーにする。 */
    private getPlaybackTargetKey(): string {
        const channels_store = useChannelsStore();
        const player_store = usePlayerStore();
        const playback_target_id = this.playback_mode === 'Live' ?
            channels_store.channel.current.display_channel_id :
            player_store.recorded_program.id.toString();
        return `${this.playback_mode}:${playback_target_id}:${this.quality_profile_type}`;
    }


    /** 現在の再生対象と実効 codec pipeline を、短時間再起動ループの判定キーにする。 */
    private getKonomiTVBS4KPlaybackRestartKey(): string {
        const effective_profile = usePlayerStore().konomitv_bs4k_effective_playback_profile;

        // 通常は DPlayer 構築前の preflight で実効 profile が確定している。
        // 初期化途中の例外などで未確定の場合も、同じ対象の Unknown pipeline として無制限再起動を避ける。
        if (effective_profile === null) {
            return `${this.getPlaybackTargetKey()}:Unknown`;
        }
        return [
            effective_profile.target_key,
            effective_profile.encoder,
            effective_profile.video_codec,
            effective_profile.video_bit_depth,
            effective_profile.audio_codec,
        ].join(':');
    }


    /** 非同期初期化が現在のroute/再生対象に属することを確実にする。 */
    private assertInitializationIsCurrent(generation: number, playback_target_key: string): void {
        if (
            this.owner_signal?.aborted === true ||
            generation !== this.initialization_generation ||
            this.destroying === true ||
            this.destroyed === true ||
            this.getPlaybackTargetKey() !== playback_target_key
        ) {
            throw new DOMException('Player initialization was superseded.', 'AbortError');
        }
    }

    /**
     * DPlayer と PlayerManager を初期化し、再生準備を行う
     */
    public async init(options: {
        default_quality: string | null;
        playback_rate: number | null;
        seek_seconds: number | null;
    } = {
        default_quality: null,
        playback_rate: null,
        seek_seconds: null,
    }): Promise<void> {
        const channels_store = useChannelsStore();
        const player_store = usePlayerStore();
        const server_settings_store = useServerSettingsStore();
        const settings_store = useSettingsStore();
        console.log('\u001b[31m[PlayerController] Initializing...');

        // 破棄済みかどうかのフラグを下ろす
        this.destroyed = false;
        const initialization_generation = ++this.initialization_generation;
        const playback_target_key = this.getPlaybackTargetKey();
        this.assertInitializationIsCurrent(initialization_generation, playback_target_key);
        this.is_live_startup_temporary_muted = false;
        this.recorded_playback_ended = false;
        this.recorded_playback_end_blocked_by_seek = false;
        this.recorded_auto_skip_cm_target = null;

        // PlayerStore にプレイヤーを初期化したことを通知する
        // 実際にはこの時点ではプレイヤーの初期化は完了していないが、PlayerController.init() を実行したことが通知されることが重要
        // ライブ視聴かつザッピングを経てチャンネルが確定した場合、破棄を遅らせていた以前の PlayerController に紐づく
        // KeyboardShortcutManager がこのタイミングで破棄される
        player_store.is_player_initialized = true;

        // 通常 / BS4K × Wi-Fi / モバイルの共通設定だけを永続的な希望値として参照する。
        // プレイヤー内で明示された一時 override は SettingsStore を変更せず、視聴画面を離れるまで優先する。
        const is_bs4k_stream = this.playback_mode === 'Live' ?
            channels_store.channel.current.display_channel_id.startsWith('bs4k') :
            player_store.recorded_program.network_id === 0x000B;
        const saved_video_codec = is_bs4k_stream ?
            this.quality_profile.bs4k_playback_video_codec :
            this.quality_profile.playback_video_codec;
        const saved_audio_codec = is_bs4k_stream ?
            this.quality_profile.bs4k_playback_audio_codec :
            this.quality_profile.playback_audio_codec;
        const requested_video_codec =
            player_store.konomitv_bs4k_playback_codec_override?.video_codec ?? saved_video_codec;
        const requested_audio_codec =
            player_store.konomitv_bs4k_playback_codec_override?.audio_codec ?? saved_audio_codec;
        const encoder = is_bs4k_stream === true ?
            server_settings_store.server_settings.general.encoder_bs4k :
            server_settings_store.server_settings.general.encoder;
        const has_video = this.playback_mode === 'Live' ?
            channels_store.channel.current.is_radiochannel === false :
            player_store.recorded_program.recorded_video.has_video;
        const saved_streaming_quality = is_bs4k_stream ?
            this.quality_profile.bs4k_playback_streaming_quality :
            this.quality_profile.playback_streaming_quality;
        const available_streaming_qualities = is_bs4k_stream ?
            BS4K_LIVE_STREAMING_QUALITIES :
            (this.playback_mode === 'Live' ? LIVE_STREAMING_QUALITIES : VIDEO_STREAMING_QUALITIES);
        const preflight_streaming_quality = has_video === false ? saved_streaming_quality :
            PlayerUtils.normalizeKonomiTVBS4KPlaybackAPIQuality(
                options.default_quality ?? saved_streaming_quality,
                is_bs4k_stream,
                available_streaming_qualities,
            );
        if (preflight_streaming_quality === null) {
            throw new Error(`Unknown playback quality for codec preflight: ${options.default_quality}`);
        }

        let effective_playback_profile = player_store.konomitv_bs4k_effective_playback_profile;
        if (
            effective_playback_profile === null ||
            effective_playback_profile.target_key !== playback_target_key ||
            effective_playback_profile.encoder !== encoder ||
            effective_playback_profile.requested_video_codec !== requested_video_codec ||
            effective_playback_profile.requested_audio_codec !== requested_audio_codec
        ) {
            const preflight_profile = await Videos.preflightKonomiTVBS4KPlaybackProfile(
                encoder,
                requested_video_codec,
                requested_audio_codec,
                {
                    is_bs4k: is_bs4k_stream,
                    streaming_quality: preflight_streaming_quality,
                },
                this.playback_mode,
                has_video,
                this.owner_signal ?? undefined,
            );
            this.assertInitializationIsCurrent(initialization_generation, playback_target_key);
            if (preflight_profile === null) {
                throw new Error('No playback codec is supported by the server and this browser.');
            }
            effective_playback_profile = {
                target_key: playback_target_key,
                encoder,
                requested_video_codec,
                requested_audio_codec,
                video_codec: preflight_profile.video_codec,
                video_bit_depth: preflight_profile.video_bit_depth,
                audio_codec: preflight_profile.audio_codec,
            };
            player_store.konomitv_bs4k_effective_playback_profile = effective_playback_profile;
        }
        const effective_video_codec = effective_playback_profile.video_codec;
        const effective_video_bit_depth = effective_playback_profile.video_bit_depth;
        const effective_audio_codec = effective_playback_profile.audio_codec;
        const is_hevc_playback = effective_video_codec === 'hevc';
        const is_hevc_10bit_playback =
            effective_video_codec === 'hevc' && effective_video_bit_depth === 10;

        // ブラウザが MSE in Worker での H.265 / HEVC 再生に対応しているかどうか
        const is_hevc_video_supported_in_worker = await mpegts.supportWorkerForMSEH265Playback();
        this.assertInitializationIsCurrent(initialization_generation, playback_target_key);

        const is_bs4k_live_playback = (
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
            liveSyncMinBufferSize: this.live_playback_buffer_seconds - 0.1,
            // ループ再生 (既定では無効。DPlayer でユーザーが明示的に有効化した保存値は尊重する)
            loop: false,
            // 自動再生
            autoplay: true,
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
                // H.265 / HEVC 再生時のみ、API に渡す画質の末尾に -hevc を付ける
                const hevc_suffix = is_hevc_playback === true && this.playback_mode === 'Live' ? '-hevc' : '';
                // -10bit や -24fps は品質名の末尾に付けて API パスに含める
                // 録画再生では session_id が同じでも画質が違うリクエストはサーバー側でエラーになる
                const is_bs4k_live = is_bs4k_live_playback;
                const is_bs4k_recorded_video = (
                    this.playback_mode === 'Video' &&
                    player_store.recorded_program.network_id === 0x000B
                );
                const build_api_quality = (quality_name: LiveStreamingQuality | BS4KLiveStreamingQuality | VideoStreamingQuality): string => {
                    let api_quality = `${quality_name}${hevc_suffix}`;
                    if (is_hevc_10bit_playback === true) {
                        api_quality += '-10bit';
                    }
                    if (
                        is_bs4k_live === false &&
                        is_bs4k_recorded_video === false &&
                        quality_name !== '1080p-60fps' &&
                        this.quality_profile.playback_24fps_mode === true
                    ) {
                        api_quality += '-24fps';
                    }
                    return api_quality;
                };
                const get_quality_display_name = (quality_name: LiveStreamingQuality | BS4KLiveStreamingQuality | VideoStreamingQuality): string => {
                    if (quality_name === '4320p') {
                        return '8K';
                    }
                    if (quality_name === '2160p') {
                        return '4K';
                    }
                    if (quality_name.endsWith('-60fps')) {
                        return `${quality_name.replace('-60fps', '')} (60fps)`;
                    }
                    if (quality_name.endsWith('-30fps')) {
                        return `${quality_name.replace('-30fps', '')} (30fps)`;
                    }
                    return quality_name;
                };
                const normalize_default_quality = (
                    default_quality: string,
                    quality_names: (LiveStreamingQuality | BS4KLiveStreamingQuality | VideoStreamingQuality)[],
                ): string => {
                    const legacy_bs4k_quality_map: Record<string, BS4KLiveStreamingQuality> = {
                        '1080p': '1080p-30fps',
                        '810p': '810p-30fps',
                        '720p': '720p-30fps',
                        '540p': '540p-30fps',
                        '480p': '480p-30fps',
                        '360p': '360p-30fps',
                        '240p': '240p-30fps',
                    };
                    const normalized_default_quality = (
                        is_bs4k_live === true || is_bs4k_recorded_video === true ?
                            legacy_bs4k_quality_map[default_quality] ?? default_quality :
                            default_quality
                    );
                    if (quality_names.includes(normalized_default_quality as LiveStreamingQuality | BS4KLiveStreamingQuality | VideoStreamingQuality)) {
                        return get_quality_display_name(normalized_default_quality as LiveStreamingQuality | BS4KLiveStreamingQuality | VideoStreamingQuality);
                    }
                    return normalized_default_quality;
                };

                // ライブ視聴: チャンネル情報がセットされているはず
                if (this.playback_mode === 'Live') {
                    // ライブストリーミング API のベース URL
                    const live_playback_codec_query =
                        PlayerUtils.buildKonomiTVBS4KLivePlaybackCodecQuery({
                            video_codec: effective_video_codec,
                            video_bit_depth: effective_video_bit_depth,
                            audio_codec: effective_audio_codec,
                        });
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
                                live_playback_codec_query,
                            ),
                        });
                    // 通常のチャンネルの場合
                    } else {
                        // 画質リストを作成
                        const live_streaming_qualities = is_bs4k_live === true ? BS4K_LIVE_STREAMING_QUALITIES : LIVE_STREAMING_QUALITIES;
                        for (const quality_name of live_streaming_qualities) {
                            qualities.push({
                                name: get_quality_display_name(quality_name),
                                type: 'mpegts',
                                url: PlayerUtils.buildKonomiTVBS4KLiveAPIEndpointURL(
                                    channels_store.channel.current.display_channel_id,
                                    build_api_quality(quality_name),
                                    'mpegts',
                                    live_playback_codec_query,
                                ),
                            });
                        }
                    }
                    // デフォルトの画質
                    const live_streaming_qualities = is_bs4k_live === true ? BS4K_LIVE_STREAMING_QUALITIES : LIVE_STREAMING_QUALITIES;
                    let default_quality: string = is_bs4k_live === true ?
                        this.quality_profile.bs4k_playback_streaming_quality :
                        this.quality_profile.playback_streaming_quality;
                    if (options.default_quality !== null) {
                        // PlayerController.init() のオプションでデフォルト画質が指定されている場合は
                        // 画質プロファイルに記載の画質ではなく、指定された（前回再生時の）画質を使ってレジュームする
                        default_quality = options.default_quality;
                    }
                    default_quality = normalize_default_quality(default_quality, live_streaming_qualities);
                    // ラジオチャンネルのみ常に 48KHz/192kbps に固定する
                    if (channels_store.channel.current.is_radiochannel) {
                        default_quality = '48kHz/192kbps';
                    }
                    return {
                        quality: qualities,
                        defaultQuality: default_quality,
                    };

                // ビデオ視聴: 録画番組情報がセットされているはず
                } else {
                    // ビデオストリーミング API のベース URL
                    const streaming_api_base_url = `${Utils.api_base_url}/streams/video/${player_store.recorded_program.id}`;
                    // 画質リストを作成
                    const video_streaming_qualities = is_bs4k_recorded_video === true ? BS4K_LIVE_STREAMING_QUALITIES : VIDEO_STREAMING_QUALITIES;
                    const first_audio_track = player_store.recorded_program.recorded_video.audio_tracks[0];
                    const initial_audio_rendition = first_audio_track === undefined ? null :
                        `${first_audio_track.index}${first_audio_track.is_dual_mono === true ? '-main' : ''}`;
                    for (const quality_name of video_streaming_qualities) {
                        // 画質ごとに異なる8桁のセッション ID を生成する。
                        const session_id = generateRecordedPlaybackSessionID();
                        // 画質設定を追加
                        qualities.push({
                            name: get_quality_display_name(quality_name),
                            type: 'hls',
                            url: `${streaming_api_base_url}/${build_api_quality(quality_name)}/playlist?session_id=${session_id}` +
                                `&video_codec=${effective_video_codec}&video_bit_depth=${effective_video_bit_depth}` +
                                `&audio_codec=${effective_audio_codec}` +
                                (initial_audio_rendition !== null ? `&audio_track=${initial_audio_rendition}` : ''),
                        });
                    }
                    // デフォルトの画質
                    // ビデオ視聴時はラジオは考慮しない
                    let default_quality: string = is_bs4k_recorded_video === true ?
                        this.quality_profile.bs4k_playback_streaming_quality :
                        this.quality_profile.playback_streaming_quality;
                    if (options.default_quality !== null) {
                        // PlayerController.init() のオプションでデフォルト画質が指定されている場合は
                        // 画質プロファイルに記載の画質ではなく、指定された（前回再生時の）画質を使ってレジュームする
                        default_quality = options.default_quality;
                    }
                    default_quality = normalize_default_quality(default_quality, video_streaming_qualities);
                    if (player_store.recorded_program.recorded_video.has_video === false) {
                        return {
                            quality: qualities,
                            defaultQuality: default_quality,
                        };
                    }
                    const tile_info = player_store.recorded_program.recorded_video.thumbnail_info?.tile ?? null;
                    return {
                        quality: qualities,
                        defaultQuality: default_quality,
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
                        enableWorkerForMSE: (is_hevc_playback === true && is_hevc_video_supported_in_worker === false) ? false : true,
                        // 再生開始まで 2048KB のバッファを貯める (?)
                        // あまり大きくしすぎてもどうも効果がないようだが、小さくしたり無効化すると特に Safari で不安定になる
                        enableStashBuffer: true,
                        stashInitialSize: Math.floor(2048 * 1024),
                        // HTMLMediaElement の内部バッファによるライブストリームの遅延を追跡する
                        // liveBufferLatencyChasing と異なり、いきなり再生時間をスキップするのではなく、
                        // 再生速度を少しだけ上げることで再生を途切れさせることなく遅延を追跡する
                        liveSync: this.tv_low_latency_mode,
                        // 許容する HTMLMediaElement の内部バッファの最大値 (秒単位, 3秒)
                        liveSyncMaxLatency: 3,
                        // HTMLMediaElement の内部バッファ (遅延) が liveSyncMaxLatency を超えたとき、ターゲットとする遅延時間 (秒単位)
                        liveSyncTargetLatency: this.live_playback_buffer_seconds,
                        // ライブストリームの遅延の追跡に利用する再生速度 (x1.1)
                        // 遅延が 3 秒を超えたとき、遅延が playback_buffer_sec を下回るまで再生速度が x1.1 に設定される
                        liveSyncPlaybackRate: 1.1,
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
            requested_video_codec !== effective_video_codec ||
            requested_audio_codec !== effective_audio_codec
        ) {
            this.player.notice(
                `希望設定 ${requested_video_codec.toUpperCase()} / ${requested_audio_codec.toUpperCase()} は` +
                `利用できないため、再生開始前に ${effective_video_codec.toUpperCase()} / ` +
                `${effective_audio_codec.toUpperCase()} を選択しました。`,
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
        this.setupVideoPlaybackHandler();

        // DPlayer のフルスクリーン関係のメソッドを無理やり上書きし、KonomiTV の UI と統合する
        this.setupFullscreenHandler();

        // DPlayer の設定パネルを無理やり拡張し、KonomiTV 独自の項目を追加する
        this.setupSettingPanelHandler();

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
        let is_player_restarting = false;  // 現在再起動中かどうか
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

            // 既に再起動中であれば何もしない (再起動が重複して行われるのを防ぐ)
            if (is_player_restarting === true) {
                console.warn('\u001b[31m[PlayerController] PlayerRestartRequired event received, but already restarting. Ignored.');
                return;
            }

            // codec pipeline の実行時失敗だけは、同一再生対象・同一 codec tuple で最初の1回だけ自動再起動する。
            // MEDIA_ERR_DECODE や SourceBuffer エラーだけでは codec 非対応と断定できないため、pin や override は変更しない。
            if (
                event.konomitv_bs4k_restart_reason === 'RuntimeCodecPipelineError' &&
                this.konomitv_bs4k_playback_restart_guard.requestAutomaticRestart(
                    this.getKonomiTVBS4KPlaybackRestartKey(),
                ) === false
            ) {
                console.error('[PlayerController] Repeated runtime codec pipeline error. Automatic restart stopped.');
                this.player.video.pause();
                player_store.is_loading = false;
                player_store.is_video_buffering = false;
                this.player.notice(
                    '同じコーデック構成で再生エラーが繰り返されました。プレイヤーの設定から映像・音声コーデックを変更してください。',
                    -1,
                    undefined,
                    'rgb(var(--v-theme-error-readable))',
                );
                return;
            }
            is_player_restarting = true;

            // 現在の再生画質・再生速度・再生位置を取得
            // この情報がプレイヤー再起動後にレジュームされる
            const should_resume_quality = event.should_resume_quality !== false;
            const quality_index = this.player.qualityIndex ?? null;
            // 画質プロファイルの既定値を優先する場合は直前の画質を引き継がない
            const current_quality = should_resume_quality === true && this.player.options.video.quality && typeof quality_index === 'number'
                ? this.player.options.video.quality[quality_index]
                : null;
            const current_playback_rate = this.player.video.playbackRate ?? null;
            const current_time = this.player.video.currentTime ?? null;

            try {
                // PlayerController 自身を破棄
                await this.destroy();

                // ライブ視聴時のみ即座に再起動すると諸々問題があるので、少し待つ
                if (this.playback_mode === 'Live') {
                    await Utils.sleep(0.5);
                }

                // PlayerController 自身を再初期化
                await this.init({
                    default_quality: current_quality ? current_quality.name : null,
                    playback_rate: this.playback_mode === 'Video' ? current_playback_rate : null,
                    seek_seconds: this.playback_mode === 'Video' ? current_time : null,
                });
            } catch (error) {
                if (!(error instanceof DOMException && error.name === 'AbortError')) {
                    console.error('[PlayerController] Failed to restart player.', error);
                    this.player?.notice('プレイヤーの再起動に失敗しました。', -1);
                }
                return;
            } finally {
                is_player_restarting = false;
            }

            // プレイヤー側にイベントの発火元から送られたメッセージ (プレイヤーを再起動中である旨) を通知する
            // 再初期化により、作り直した DPlayer が再び this.player にセットされているはず
            // 通知を表示してから PlayerController を破棄すると DPlayer の DOM 要素ごと消えてしまうので、DPlayer を作り直した後に通知を表示する
            assert(this.player !== null);
            if (event.message) {
                // 遅延時間が指定されていれば待つ
                await Utils.sleep(event.message_delay_seconds ?? 0);
                // 明示的にエラーメッセージではないことが指定されていればデフォルトの色で通知を表示する
                // デフォルトではメッセージは赤色で表示される
                const color = event.is_error_message === false ? undefined : 'rgb(var(--v-theme-error-readable))';
                this.player.notice(event.message, undefined, undefined, color);
            }
        });

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

            // 現在の再生画質・再生速度・再生位置を取得
            // この情報がプレイヤー再起動後にレジュームされる
            const current_quality = typeof this.player?.qualityIndex === 'number' ?
                this.player.options.video.quality?.[this.player.qualityIndex] ?? null : null;
            const current_playback_rate = this.player?.video.playbackRate ?? null;
            const current_time = this.player?.video.currentTime ?? null;

            // PlayerController 自身を破棄
            // このイベントは手動で再起動した際に実行されるものなので、再初期化までは待たずに即座に再初期化する
            await this.destroy();

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
                new LiveEventManager(this.player),
                // 実況機能が無効な場合は接続情報 API へのアクセスも WebSocket 接続も開始しない
                ...(settings_store.is_jikkyo_enabled === true ? [new LiveCommentManager(this.player)] : []),
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

        console.log('\u001b[31m[PlayerController] Initialized.');
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
        // 画質切り替え前の mpegts.js は DPlayer が既に破棄しているため off() は呼ばず、参照だけを更新する。
        // 現在のインスタンスは PlayerController.destroy() で明示的に解除する。
        this.live_arib_ttml_source = source;
        source.on(mpegts.Events.TIMED_ID3_METADATA_ARRIVED, this.live_arib_ttml_handler);
    }


    /**
     * DPlayer に動画再生系のイベントハンドラーを登録する
     * 特にライブ視聴ではここで適切に再生状態の管理 (再生可能かどうか、エラーが発生していないかなど) を行う必要がある
     */
    private setupVideoPlaybackHandler(): void {
        assert(this.player !== null);
        const channels_store = useChannelsStore();
        const player_store = usePlayerStore();
        const settings_store = useSettingsStore();

        // ライブ視聴: 再生停止状態かつ現在の再生位置からバッファが 30 秒以上離れていないかを 60 秒おきに監視し、そうなっていたら強制的にシークする
        // mpegts.js の仕様上、MSE 側に未再生のバッファが貯まり過ぎると新規に SourceBuffer が追加できなくなるため、強制的に接続が切断されてしまう
        // 再生停止状態でも定期的にシークすることで、バッファが貯まりすぎないように調節する
        if (this.playback_mode === 'Live') {
            this.live_force_seek_interval_timer_cancel = Utils.setIntervalInWorker(() => {
                if (this.player === null) return;
                if ((this.player.video.paused && this.player.video.buffered.length >= 1) &&
                    (this.player.video.buffered.end(0) - this.player.video.currentTime > 30)) {
                    this.player.sync();
                }
            }, 60 * 1000);
        }

        // ビデオ視聴: ビデオストリームのアクティブ状態を維持するために 5 秒おきに Keep-Alive API にリクエストを送る
        // HLS プレイリストやセグメントのリクエストが行われたタイミングでも Keep-Alive が行われるが、
        // それだけではタイミング次第では十分ではないため、定期的に Keep-Alive を行う
        // Keep-Alive が行われなくなったタイミングで、サーバー側で自動的にビデオストリームの終了処理 (エンコードタスクの停止) が行われる
        if (this.playback_mode === 'Video') {
            this.video_keep_alive_interval_timer_cancel = Utils.setIntervalInWorker(async () => {
                // 画質切り替えでベース URL が変わることも想定し、あえて毎回 API URL を取得している
                if (this.player === null) return;
                const player = this.player;
                if (player.quality === null) return;
                const api_quality = PlayerUtils.extractVideoAPIQualityFromDPlayer(player);
                const source_url = new URL(player.quality.url);
                const query = source_url.searchParams.toString();
                await APIClient.put(`${Utils.api_base_url}/streams/video/${player_store.recorded_program.id}/${api_quality}/keep-alive?${query}`);
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
        });
        this.player.on('playing', () => {
            // ロード中 (映像が表示されていない) でなければ Progress Circular を非表示にする
            if (player_store.is_loading === false) {
                player_store.is_video_buffering = false;
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

                // HTMLVideoElement ネイティブの再生時エラーのイベントハンドラーを登録
                // mpegts.js が予期せずクラッシュした場合など、意図せず発生してしまうことがある
                // Offline 以外であれば PlayerController の再起動を要求する
                this.player.on('error', async (event: MediaError) => {

                    // DPlayer がすでに破棄されているか、現在ライブストリームが Offline であれば何もしない
                    if (this.player === null || player_store.live_stream_status === 'Offline') {
                        return;
                    }

                    // すぐ再起動すると問題があるケースがあるので、少し待機する
                    await Utils.sleep(1);

                    const media_error = this.player.video.error;
                    if (media_error) {
                        console.error('\u001b[31m[PlayerController] HTMLVideoElement error event:', media_error);
                        player_store.event_emitter.emit('PlayerRestartRequired', {
                            message: `再生中にエラーが発生しました。(Native: ${media_error.code}: ${media_error.message}) プレイヤーを再起動しています…`,
                            konomitv_bs4k_restart_reason: media_error.code === media_error.MEDIA_ERR_DECODE ?
                                'RuntimeCodecPipelineError' : undefined,
                        });
                    } else {
                        // MediaError オブジェクトは場合によっては存在しないことがあるらしい…
                        // 存在しない場合は unknown error として扱う
                        player_store.event_emitter.emit('PlayerRestartRequired', {
                            message: '再生中にエラーが発生しました。(Native: unknown error) プレイヤーを再起動しています…',
                        });
                    }
                });

                // 必ず最初はローディング状態とする
                player_store.is_loading = true;

                // DPlayer のスマホ向け UI ではミュート解除用の音量ボタンがないため、PC 向け UI の保存値だけ参照する
                const should_keep_muted_after_live_startup =
                    this.player.container.classList.contains('dplayer-mobile') === false &&
                    localStorage.getItem('dplayer-is-muted') === 'true';

                // 再生準備中の音声を出さないため、一時的にミュートする
                // 保存済みミュートと区別し、volumechange 側で保存値を上書きしないようにする
                this.is_live_startup_temporary_muted = this.player.video.muted === false;
                this.player.video.muted = true;

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
                const on_canplay = async () => {

                    // 重複実行を回避する
                    if (this.player === null) return;
                    if (on_canplay_called === true) return;
                    this.player.video.oncanplay = null;
                    this.player.video.oncanplaythrough = null;
                    on_canplay_called = true;

                    // 再生バッファ調整のため、一旦停止させる
                    // this.player.video.pause() を使うとプレイヤーの UI アイコンが停止してしまうので、代わりに playbackRate を使う
                    console.log('\u001b[31m[PlayerController] Buffering...');
                    this.player.video.playbackRate = 0;

                    // 再生バッファが live_playback_buffer_seconds を超えるまで 0.1 秒おきに再生バッファをチェックする
                    // 再生バッファが live_playback_buffer_seconds を切ると再生が途切れやすくなるので (特に動きの激しい映像)、
                    // 再生開始までの時間を若干犠牲にして、再生バッファの調整と同期に時間を割く
                    // live_playback_buffer_seconds の値は mpegts.js の liveSyncTargetLatency 設定に渡す値と共通
                    const live_playback_buffer_seconds = this.live_playback_buffer_seconds;  // 毎回取得すると負荷が掛かるのでキャッシュする
                    let current_playback_buffer_sec = this.getPlaybackBufferSeconds();
                    while (current_playback_buffer_sec < live_playback_buffer_seconds) {
                        await Utils.sleep(0.1);
                        current_playback_buffer_sec = this.getPlaybackBufferSeconds();
                    }

                    // 再生バッファ調整のため一旦停止していた再生を再び開始
                    this.player.video.playbackRate = 1;
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
                        this.is_live_startup_temporary_muted = false;
                        this.player.video.muted = false;
                        // ミュート中でない場合だけフェードインする (いきなり再生されるよりも体験が良い)
                        // 開始音量を 0 に下げてから、保存されている音量まで徐々に上げる
                        this.player.video.volume = 0;
                        // 0.5 秒間かけて 0 から current_volume まで音量を上げる
                        const current_volume = this.player.user.get('volume');  // 0.0 ~ 1.0 の範囲
                        const volume_step = current_volume / 10;
                        for (let i = 0; i < 10; i++) {  // 10 回に分けて音量を上げる
                            await Utils.sleep(0.5 / 10);
                            // 音量が current_volume を超えないようにする
                            // 浮動小数点絡みの問題 (丸め誤差) が出るため小数第3位で切り捨てる
                            this.player.video.volume = Math.min(Utils.mathFloor(this.player.video.volume + volume_step, 3), current_volume);
                        }
                        // 最後に current_volume に設定し直す
                        // 上記ロジックでは丸め誤差の関係で完全に current_volume とは一致しないことがあるため
                        this.player.video.volume = current_volume;
                    }
                };
                this.player.video.oncanplay = on_canplay;
                this.player.video.oncanplaythrough = on_canplay;

                // 万が一 canplay(through) が発火しなかった場合のために (ほぼ Safari 向け) 、
                // mpegts.js 側でメディア情報が取得できたタイミングでも再生開始を試みる
                // 特に Safari 18 以降では MSE の canplay(through) が場合によっては発火しなかったり、発火が異常に遅かったりする…
                // Safari 18 以降、MSE において canplay(through) の発火タイミングと readyState の値は信頼できない
                this.player.plugins.mpegts?.on(mpegts.Events.MEDIA_INFO, async (info: {[key: string]: any}) => {
                    console.log('\u001b[31m[PlayerController] mpegts.js media info:', info);
                    this.live_media_info = info;
                    this.applyAudioTrackLabels(info);
                    // 一応ブラウザネイティブの canplay(through) を優先したいので、0.25 秒待ってから再生開始を試みる
                    // 既に再生開始処理を実行済みの場合は実行しない
                    await Utils.sleep(0.25);
                    if (on_canplay_called === false) {
                        console.warn('\u001b[31m[PlayerController] mpegts.js media info fired, but canplay(through) event not fired. Trying to manually start playback.');
                        on_canplay();
                    }
                });

                this.setupLiveAudioTrackMonitor();

                // 万が一 canplay(through) が発火しなかった場合のために (ほぼ Safari 向け) 、
                // 非同期で 0.05 秒おきに直接 readyState === HAVE_ENOUGH_DATA かどうかを確認する
                // ほとんどのケースでは 先に上記 mpegts.js の MEDIA_INFO イベントが発火するため、この処理は実行されない
                (async () => {
                    let have_future_data_count = 0;
                    while (this.player !== null && this.player.video.readyState < 4) {
                        // プレイヤーが充分と判断する基準はまちまちでブラウザによっては HAVE_FUTURE_DATA のままタイムアウトするので
                        // HAVE_FUTURE_DATA がおおむね 5 秒つづけば HAVE_ENOUGH_DATA 扱いする
                        if (this.player.video.readyState < 3) {
                            have_future_data_count = 0;
                        } else if (++have_future_data_count > 100) {
                            break;
                        }
                        await Utils.sleep(0.05);
                    }
                    // ループを終えた時点で readyState === HAVE_ENOUGH_DATA になっているので、再生開始を試みる
                    // 既に再生開始処理を実行済みの場合は実行しない
                    await Utils.sleep(0.1);
                    if (on_canplay_called === false) {
                        console.warn('\u001b[31m[PlayerController] canplay(through) event not fired. Trying to manually start playback.');
                        on_canplay();
                    }
                })();

                // もしライブストリームのステータスが ONAir にも関わらず 15 秒以上バッファリング中で canplaythrough が発火しない場合、
                // ロードに失敗したとみなし PlayerController の再起動を要求する
                await Utils.sleep(15);
                if (this.destroyed === true || this.player === null) return;
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

                // HTMLVideoElement ネイティブの再生時エラーのイベントハンドラーを登録
                // HLS 再生時にブラウザが呼び出す HW デコーダーがクラッシュした場合など、意図せず発生してしまうことがある
                // プレイヤー自体の破棄・再生成以外では基本復旧できないので、PlayerController の再起動を要求する
                this.player.on('error', async (event: MediaError) => {

                    // DPlayer がすでに破棄されていれば何もしない
                    if (this.player === null) {
                        return;
                    }

                    // ライブ視聴時とは異なり、録画なので待たなくても再起動できる
                    const media_error = this.player.video.error;
                    if (media_error) {
                        console.error('\u001b[31m[PlayerController] HTMLVideoElement error event:', media_error);
                        player_store.event_emitter.emit('PlayerRestartRequired', {
                            message: `再生中にエラーが発生しました。(Native: ${media_error.code}: ${media_error.message}) プレイヤーを再起動しています…`,
                            konomitv_bs4k_restart_reason: media_error.code === media_error.MEDIA_ERR_DECODE ?
                                'RuntimeCodecPipelineError' : undefined,
                        });
                    } else {
                        // MediaError オブジェクトは場合によっては存在しないことがあるらしい…
                        // 存在しない場合は unknown error として扱う
                        player_store.event_emitter.emit('PlayerRestartRequired', {
                            message: '再生中にエラーが発生しました。(Native: unknown error) プレイヤーを再起動しています…',
                        });
                    }
                });

            }
        };

        // 初回実行
        on_init_or_quality_change();

        // 画質切り替え開始時のイベント
        this.player.on('quality_start', () => on_init_or_quality_change(true));

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
            while (audio_items.length > Math.max(audio_track_count, 1) && audio_items.length > 2) {
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

        const mpegts_media_info = (this.player.plugins.mpegts as any)?.mediaInfo;
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


    /**
     * ライブ視聴中の動的音声トラック変化を監視する
     */
    private setupLiveAudioTrackMonitor(): void {

        if (this.playback_mode !== 'Live') return;
        if (this.live_audio_track_interval_timer_cancel !== null) return;

        this.live_audio_track_interval_timer_cancel = Utils.setIntervalInWorker(() => {
            if (this.player === null) return;
            const media_info = this.getCurrentLiveAudioTrackMediaInfo();
            if (media_info !== null) {
                this.applyAudioTrackLabels(media_info);
            }
        }, 1000);
    }


    /** 録画HLSの代替音声レンディションを、映像を維持したまま切り替える。 */
    private setupRecordedHLSAudioTrackSelector(): void {

        if (this.playback_mode !== 'Video' || this.player === null) return;
        const hls = this.player.plugins.hls as Hls | undefined;
        if (hls === undefined) return;  // 録画再生はhls.js必須。非対応表示は初期化処理側で行う。
        if (this.recorded_hls_audio_selector_instances.has(hls)) return;
        this.recorded_hls_audio_selector_instances.add(hls);

        const player_store = usePlayerStore();

        // MSE 型判定を通過していても、実際の SourceBuffer 追加・append 時に Opus が拒否されることがある。
        // 自動で AAC へ変更せず、同一 codec での一度限りの再起動と、再発時の恒久通知へ送る。
        hls.on(Hls.Events.ERROR, (_event, data) => {
            const is_audio_source_buffer_error = data.sourceBufferName === 'audio' && [
                ErrorDetails.BUFFER_ADD_CODEC_ERROR,
                ErrorDetails.BUFFER_APPEND_ERROR,
                ErrorDetails.BUFFER_APPENDING_ERROR,
            ].includes(data.details);
            if (
                is_audio_source_buffer_error === true &&
                player_store.konomitv_bs4k_effective_playback_profile?.audio_codec === 'opus'
            ) {
                player_store.event_emitter.emit('PlayerRestartRequired', {
                    message: 'Opus 音声の再生処理でエラーが発生しました。プレイヤーを再起動しています…',
                    konomitv_bs4k_restart_reason: 'RuntimeCodecPipelineError',
                });
            }
        });

        const recorded_video = player_store.recorded_program.recorded_video;
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
    private setupSettingPanelHandler(): void {
        assert(this.player !== null);
        const player_store = usePlayerStore();
        const channels_store = useChannelsStore();
        const server_settings_store = useServerSettingsStore();
        const settings_store = useSettingsStore();

        // 独自サブパネルの表示は modifier class だけで制御し、DPlayer が元パネル用に設定した
        // inline clip-path / height は変更しない。閉じる際に全 modifier を外すことで、
        // 戻る操作・外側クリック・別サブパネルへの移動のいずれでも元の寸法へ確実に戻す。
        const setting_box = this.player.template.settingBox;
        const codec_panel_class_names = [
            'dplayer-konomitv-bs4k-setting-box-video-codec',
            'dplayer-konomitv-bs4k-setting-box-audio-codec',
        ];
        const close_codec_panel = () => {
            setting_box.classList.remove(...codec_panel_class_names);
        };
        const open_codec_panel = (panel_name: 'video-codec' | 'audio-codec') => {
            close_codec_panel();
            // DPlayer 標準サブパネルの modifier が残っていると clip-path の優先順位が競合する。
            setting_box.classList.remove(
                'dplayer-setting-box-quality',
                'dplayer-setting-box-speed',
                'dplayer-setting-box-audio',
            );
            setting_box.classList.add(`dplayer-konomitv-bs4k-setting-box-${panel_name}`);
        };
        const consume_codec_panel_event = (event: Event) => {
            // 設定パネル外の click/tap handler へ伝播させず、DPlayer の mask/hide と競合させない。
            event.preventDefault();
            event.stopPropagation();
        };
        const register_codec_panel_activation_handler = (
            element: HTMLElement,
            handler: (event: Event) => void,
        ) => {
            element.addEventListener('click', handler);
            element.addEventListener('keydown', (event) => {
                // role=button の独自要素をネイティブ button と同じ Enter / Space で起動する。
                // Space のページスクロールとキーリピートによる多重 preflight はここで抑止する。
                if ((event.key !== 'Enter' && event.key !== ' ') || event.repeat === true) return;
                consume_codec_panel_event(event);
                element.click();
            });
        };

        // 設定パネルの開閉を把握するためモンキーパッチを追加し、PlayerStore に通知する
        const original_hide = this.player.setting.hide;
        const original_show = this.player.setting.show;
        this.player.setting.hide = () => {
            if (this.player === null) return;
            original_hide.call(this.player.setting);
            // 独自サブパネルを開いたまま外側を押して閉じた場合も、次回は必ず元パネルから表示する。
            close_codec_panel();
            player_store.is_player_setting_panel_open = false;
        };
        this.player.setting.show = () => {
            if (this.player === null) return;
            close_codec_panel();
            original_show.call(this.player.setting);
            player_store.is_player_setting_panel_open = true;
        };

        // ライブ・録画で同じ4映像 codec / 2音声 codec を一時 override として提供する。
        // 非対応候補も希望値として選択でき、再起動前の preflight が lower-only 順で実効 tuple を決める。
        const selectable_video_codecs: KonomiTVBS4KPlaybackVideoCodec[] = ['av1', 'vp9', 'hevc', 'avc'];
        const video_codec_item_html = selectable_video_codecs.map((codec) => `
            <div class="dplayer-konomitv-bs4k-setting-video-codec-item" data-codec="${codec}"
                role="button" tabindex="0"
                style="display:flex; align-items:center; height:30px; padding:5px 10px; box-sizing:border-box; cursor:pointer; touch-action:manipulation;">
                <div class="dplayer-toggle dplayer-konomitv-bs4k-setting-video-codec-check" style="display:inline-block; position:static; width:22px; margin-right:6px;"></div>
                <span class="dplayer-label">${codec.toUpperCase()}</span>
            </div>
        `).join('');
        const video_codec_panel_height = 54 + selectable_video_codecs.length * 30;
        const audio_codec_labels: Record<KonomiTVBS4KPlaybackAudioCodec, string> = {
            aac: 'AAC',
            opus: 'Opus',
        };
        const selectable_audio_codecs: KonomiTVBS4KPlaybackAudioCodec[] = ['opus', 'aac'];
        const audio_codec_item_html = selectable_audio_codecs.map((codec) => `
            <div class="dplayer-konomitv-bs4k-setting-audio-codec-item" data-codec="${codec}"
                role="button" tabindex="0"
                style="display:flex; align-items:center; height:30px; padding:5px 10px; box-sizing:border-box; cursor:pointer; touch-action:manipulation;">
                <div class="dplayer-toggle dplayer-konomitv-bs4k-setting-audio-codec-check" style="display:inline-block; position:static; width:22px; margin-right:6px;"></div>
                <span class="dplayer-label">${audio_codec_labels[codec]}</span>
            </div>
        `).join('');
        const audio_codec_panel_height = 54 + selectable_audio_codecs.length * 30;
        const audio_codec_setting_item_html = `
            <div class="dplayer-setting-item dplayer-konomitv-bs4k-setting-audio-codec"
                role="button" tabindex="0" style="touch-action:manipulation;">
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
        this.player.template.audio.insertAdjacentHTML('afterend', `
            <div class="dplayer-setting-item dplayer-konomitv-bs4k-setting-video-codec"
                role="button" tabindex="0" style="touch-action:manipulation;">
                <span class="dplayer-label">映像コーデック</span>
                <span class="dplayer-label-value dplayer-konomitv-bs4k-setting-video-codec-value"></span>
                <div class="dplayer-toggle dplayer-konomitv-bs4k-setting-video-codec-arrow"></div>
            </div>
            ${audio_codec_setting_item_html}
            ${auto_skip_cm_setting_item_html}
            <div class="dplayer-setting-item dplayer-setting-mobile-profile">
                <span class="dplayer-label">モバイル回線向け画質</span>
                <div class="dplayer-toggle">
                    <input class="dplayer-mobile-profile-setting-input" type="checkbox" name="dplayer-toggle-mobile-profile">
                    <label for="dplayer-toggle-mobile-profile" style="--theme-color:rgb(var(--v-theme-primary))"></label>
                </div>
            </div>
        `);

        // DPlayer の音声トラックと同じ構成の独自サブパネルを追加する。
        const audio_codec_panel_html = `
            <div class="dplayer-konomitv-bs4k-setting-audio-codec-panel"
                style="display:block; position:absolute; bottom:0; width:100%; padding:7px 0; box-sizing:border-box; transform:translateX(100%); transition:transform .25s ease;">
                <div class="dplayer-setting-header dplayer-konomitv-bs4k-setting-audio-codec-header"
                    role="button" tabindex="0"
                    style="display:flex; align-items:center; height:33px; padding:0 5px 5px; margin-bottom:7px; border-bottom:2px solid rgba(255,255,255,.15); box-sizing:border-box; cursor:pointer; touch-action:manipulation;">
                    <div class="dplayer-toggle dplayer-konomitv-bs4k-setting-audio-codec-back"
                        style="display:inline-block; position:static; width:22px; margin-right:6px;"></div>
                    <span class="dplayer-label">音声コーデック</span>
                </div>
                ${audio_codec_item_html}
            </div>
        `;
        setting_box.insertAdjacentHTML('beforeend', `
            <div class="dplayer-konomitv-bs4k-setting-video-codec-panel"
                style="display:block; position:absolute; bottom:0; width:100%; padding:7px 0; box-sizing:border-box; transform:translateX(100%); transition:transform .25s ease;">
                <div class="dplayer-setting-header dplayer-konomitv-bs4k-setting-video-codec-header"
                    role="button" tabindex="0"
                    style="display:flex; align-items:center; height:33px; padding:0 5px 5px; margin-bottom:7px; border-bottom:2px solid rgba(255,255,255,.15); box-sizing:border-box; cursor:pointer; touch-action:manipulation;">
                    <div class="dplayer-toggle dplayer-konomitv-bs4k-setting-video-codec-back"
                        style="display:inline-block; position:static; width:22px; margin-right:6px;"></div>
                    <span class="dplayer-label">映像コーデック</span>
                </div>
                ${video_codec_item_html}
            </div>
            ${audio_codec_panel_html}
        `);

        // DPlayer が持つ音声トラック用の矢印・戻る・チェックアイコンを流用して見た目を揃える
        const audio_arrow_html = this.player.template.audio.querySelector<HTMLElement>('.dplayer-toggle')?.innerHTML ?? '';
        const audio_back_html = this.player.container.querySelector<HTMLElement>('.dplayer-setting-audio-header .dplayer-toggle')?.innerHTML ?? '';
        const audio_check_html = this.player.container.querySelector<HTMLElement>('.dplayer-setting-audio-item .dplayer-toggle')?.innerHTML ?? '';
        this.player.container.querySelector<HTMLElement>('.dplayer-konomitv-bs4k-setting-video-codec-arrow')!.innerHTML = audio_arrow_html;
        this.player.container.querySelector<HTMLElement>('.dplayer-konomitv-bs4k-setting-video-codec-back')!.innerHTML = audio_back_html;
        this.player.container.querySelectorAll<HTMLElement>('.dplayer-konomitv-bs4k-setting-video-codec-check')
            .forEach((element) => element.innerHTML = audio_check_html);
        this.player.container.querySelector<HTMLElement>('.dplayer-konomitv-bs4k-setting-audio-codec-arrow')!.innerHTML = audio_arrow_html;
        this.player.container.querySelector<HTMLElement>('.dplayer-konomitv-bs4k-setting-audio-codec-back')!.innerHTML = audio_back_html;
        this.player.container.querySelectorAll<HTMLElement>('.dplayer-konomitv-bs4k-setting-audio-codec-check')
            .forEach((element) => element.innerHTML = audio_check_html);

        // 現在の通常 / BS4K と回線プロファイルに対応する共通設定を、override がない場合だけ参照する。
        const is_bs4k = this.playback_mode === 'Live' ?
            channels_store.channel.current.display_channel_id.startsWith('bs4k') :
            player_store.recorded_program.network_id === 0x000B;
        const get_requested_video_codec = (): KonomiTVBS4KPlaybackVideoCodec =>
            player_store.konomitv_bs4k_playback_codec_override?.video_codec ??
            settings_store.settings[getKonomiTVBS4KPlaybackVideoCodecSettingKey(
                is_bs4k,
                this.quality_profile_type === 'Cellular',
            )];
        const get_requested_audio_codec = (): KonomiTVBS4KPlaybackAudioCodec =>
            player_store.konomitv_bs4k_playback_codec_override?.audio_codec ??
            settings_store.settings[getKonomiTVBS4KPlaybackAudioCodecSettingKey(
                is_bs4k,
                this.quality_profile_type === 'Cellular',
            )];
        let is_codec_preflight_in_progress = false;
        const prepare_codec_override = async (
            requested_video_codec: KonomiTVBS4KPlaybackVideoCodec,
            requested_audio_codec: KonomiTVBS4KPlaybackAudioCodec,
        ): Promise<boolean> => {
            if (is_codec_preflight_in_progress === true || this.player === null) return false;
            is_codec_preflight_in_progress = true;
            const selection_generation = this.initialization_generation;
            const playback_target_key = this.getPlaybackTargetKey();
            const current_player = this.player;
            try {
                const has_video = this.playback_mode === 'Live' ?
                    channels_store.channel.current.is_radiochannel === false :
                    player_store.recorded_program.recorded_video.has_video;
                const available_streaming_qualities = is_bs4k ?
                    BS4K_LIVE_STREAMING_QUALITIES :
                    (this.playback_mode === 'Live' ? LIVE_STREAMING_QUALITIES : VIDEO_STREAMING_QUALITIES);
                const saved_streaming_quality = is_bs4k ?
                    this.quality_profile.bs4k_playback_streaming_quality :
                    this.quality_profile.playback_streaming_quality;
                const current_quality_index = current_player.qualityIndex;
                const current_quality_name = (
                    typeof current_quality_index === 'number' && current_player.options.video.quality !== undefined
                ) ? current_player.options.video.quality[current_quality_index]?.name : null;
                const preflight_streaming_quality = has_video === false ? saved_streaming_quality :
                    PlayerUtils.normalizeKonomiTVBS4KPlaybackAPIQuality(
                        current_quality_name ?? saved_streaming_quality,
                        is_bs4k,
                        available_streaming_qualities,
                    );
                if (preflight_streaming_quality === null) {
                    current_player.notice('現在の画質を能力検査できないため、コーデックを変更しませんでした。');
                    return false;
                }

                const encoder = is_bs4k ?
                    server_settings_store.server_settings.general.encoder_bs4k :
                    server_settings_store.server_settings.general.encoder;
                const preflight_profile = await Videos.preflightKonomiTVBS4KPlaybackProfile(
                    encoder,
                    requested_video_codec,
                    requested_audio_codec,
                    {
                        is_bs4k,
                        streaming_quality: preflight_streaming_quality,
                    },
                    this.playback_mode,
                    has_video,
                    this.owner_signal ?? undefined,
                );
                this.assertInitializationIsCurrent(selection_generation, playback_target_key);
                if (this.player !== current_player) {
                    throw new DOMException('Codec selection was superseded.', 'AbortError');
                }
                if (preflight_profile === null) {
                    current_player.notice('この画質で利用できる下位互換コーデックがないため、現在の再生を継続します。');
                    return false;
                }

                // 新tupleを確定できた後にだけoverride/pinを入れ替える。
                // 失敗時は既存playerも旧pinもそのまま残る。
                player_store.konomitv_bs4k_playback_codec_override = {
                    video_codec: requested_video_codec,
                    audio_codec: requested_audio_codec,
                };
                player_store.konomitv_bs4k_effective_playback_profile = {
                    target_key: playback_target_key,
                    encoder,
                    requested_video_codec,
                    requested_audio_codec,
                    video_codec: preflight_profile.video_codec,
                    video_bit_depth: preflight_profile.video_bit_depth,
                    audio_codec: preflight_profile.audio_codec,
                };
                return true;
            } catch (error) {
                if (error instanceof DOMException && error.name === 'AbortError') return false;
                console.error('[PlayerController] Codec preflight failed.', error);
                current_player.notice('コーデックの能力検査に失敗したため、現在の再生を継続します。');
                return false;
            } finally {
                is_codec_preflight_in_progress = false;
            }
        };

        const video_codec_button = this.player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-video-codec',
        )!;
        const video_codec_value = this.player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-video-codec-value',
        )!;
        const video_codec_items = Array.from(this.player.container.querySelectorAll<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-video-codec-item',
        ));
        setting_box.style.setProperty('--konomitv-bs4k-video-codec-panel-height', `${video_codec_panel_height}px`);
        const update_video_codec_display = () => {
            const selected_codec = get_requested_video_codec();
            video_codec_value.textContent = selected_codec.toUpperCase();
            video_codec_items.forEach((item) => {
                const check = item.querySelector<HTMLElement>(
                    '.dplayer-konomitv-bs4k-setting-video-codec-check',
                );
                if (check !== null) check.style.visibility = item.dataset.codec === selected_codec ? 'visible' : 'hidden';
            });
        };
        update_video_codec_display();
        register_codec_panel_activation_handler(video_codec_button, (event) => {
            consume_codec_panel_event(event);
            update_video_codec_display();
            // DPlayer が計測した元パネル用の inline clip-path は上書きせず、
            // サブパネル表示中だけ専用クラスで切り替える。
            open_codec_panel('video-codec');
        });
        const video_codec_header = this.player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-video-codec-header',
        )!;
        register_codec_panel_activation_handler(video_codec_header, (event) => {
            consume_codec_panel_event(event);
            close_codec_panel();
        });
        video_codec_items.forEach((item) => {
            register_codec_panel_activation_handler(item, async (event) => {
                consume_codec_panel_event(event);
                const codec = item.dataset.codec as KonomiTVBS4KPlaybackVideoCodec;
                if (await prepare_codec_override(codec, get_requested_audio_codec()) === false) return;
                update_video_codec_display();
                close_codec_panel();
                // プレイヤー再起動が始まる前に設定パネル全体を閉じ、黒画面上へ一瞬残ることを防ぐ
                this.player?.setting.hide();
                player_store.event_emitter.emit('PlayerRestartRequired', {
                    message: `映像コーデックを ${codec.toUpperCase()} に変更しました。`,
                    message_delay_seconds: this.tv_low_latency_mode || this.playback_mode === 'Video' ? 2 : 4.5,
                    is_error_message: false,
                    should_resume_quality: true,
                });
            });
        });

        const audio_codec_button = this.player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-audio-codec',
        )!;
        const audio_codec_value = this.player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-audio-codec-value',
        )!;
        const audio_codec_items = Array.from(
            this.player.container.querySelectorAll<HTMLElement>('.dplayer-konomitv-bs4k-setting-audio-codec-item'),
        );
        setting_box.style.setProperty('--konomitv-bs4k-audio-codec-panel-height', `${audio_codec_panel_height}px`);
        const update_audio_codec_display = () => {
            const selected_codec = get_requested_audio_codec();
            audio_codec_value.textContent = audio_codec_labels[selected_codec];
            audio_codec_items.forEach((item) => {
                const check = item.querySelector<HTMLElement>(
                    '.dplayer-konomitv-bs4k-setting-audio-codec-check',
                );
                if (check !== null) check.style.visibility = item.dataset.codec === selected_codec ? 'visible' : 'hidden';
            });
        };
        update_audio_codec_display();
        register_codec_panel_activation_handler(audio_codec_button, (event) => {
            consume_codec_panel_event(event);
            update_audio_codec_display();
            open_codec_panel('audio-codec');
        });
        const audio_codec_header = this.player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-audio-codec-header',
        )!;
        register_codec_panel_activation_handler(audio_codec_header, (event) => {
            consume_codec_panel_event(event);
            close_codec_panel();
        });
        audio_codec_items.forEach((item) => {
            register_codec_panel_activation_handler(item, async (event) => {
                consume_codec_panel_event(event);
                const codec = item.dataset.codec as KonomiTVBS4KPlaybackAudioCodec;
                if (await prepare_codec_override(get_requested_video_codec(), codec) === false) return;
                update_audio_codec_display();
                close_codec_panel();
                this.player?.setting.hide();
                player_store.event_emitter.emit('PlayerRestartRequired', {
                    message: `音声コーデックを ${audio_codec_labels[codec]} に変更しました。`,
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

        // デフォルトのチェック状態を画質プロファイルタイプに合わせる
        const toggle_mobile_profile_input = this.player.container.querySelector<HTMLInputElement>('.dplayer-mobile-profile-setting-input')!;
        toggle_mobile_profile_input.checked = this.quality_profile_type === 'Cellular';

        // モバイル回線プロファイルに切り替えるボタンがクリックされた時のイベントハンドラーを登録
        const toggle_mobile_profile_button = this.player.container.querySelector('.dplayer-setting-mobile-profile')!;
        toggle_mobile_profile_button.addEventListener('click', () => {
            // チェックボックスの状態を切り替える
            toggle_mobile_profile_input.checked = !toggle_mobile_profile_input.checked;
            // 回線プロファイルが変わった場合は、同じ一時希望値で切り替え先の能力を改めて評価する。
            player_store.konomitv_bs4k_effective_playback_profile = null;
            // 画質プロファイルをモバイル回線向けに切り替えてから、プレイヤーを再起動
            if (toggle_mobile_profile_input.checked) {
                this.quality_profile_type = 'Cellular';
                player_store.selected_quality_profile_type = this.quality_profile_type;
                update_video_codec_display();
                update_audio_codec_display();
                player_store.event_emitter.emit('PlayerRestartRequired', {
                    message: 'モバイル回線向けの画質プロファイルに切り替えました。',
                    // 他の通知と被らないように、メッセージを遅らせて表示する
                    message_delay_seconds: this.tv_low_latency_mode || this.playback_mode === 'Video' ? 2 : 4.5,
                    is_error_message: false,
                    // モバイル回線プロファイル切り替え時、切り替え後の画質プロファイルのデフォルト画質を優先する
                    should_resume_quality: false,
                });
            // 画質プロファイルを Wi-Fi 回線向けに切り替えてから、プレイヤーを再起動
            } else {
                this.quality_profile_type = 'Wi-Fi';
                player_store.selected_quality_profile_type = this.quality_profile_type;
                update_video_codec_display();
                update_audio_codec_display();
                player_store.event_emitter.emit('PlayerRestartRequired', {
                    message: 'Wi-Fi 回線向けの画質プロファイルに切り替えました。',
                    // 他の通知と被らないように、メッセージを遅らせて表示する
                    message_delay_seconds: this.tv_low_latency_mode || this.playback_mode === 'Video' ? 2 : 4.5,
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
     * DPlayer と PlayerManager を破棄し、再生を終了する
     * 常に init() で作成したものが destroy() ですべてクリーンアップされるように実装すべき
     * PlayerController の再起動を行う場合、基本外部から直接 await destroy() と await init() は呼び出さず、代わりに
     * player_store.event_emitter.emit('PlayerRestartRequired', 'プレイヤーを再起動しています…') のようにイベントを発火させるべき
     */
    public async destroy(): Promise<void> {
        const settings_store = useSettingsStore();
        const player_store = usePlayerStore();

        // すでに破棄されているのに再度実行してはならない
        if (this.destroyed === true) {
            return;
        }
        // すでに破棄中なら何もしない
        if (this.destroying === true) {
            return;
        }
        this.destroying = true;
        // この後の非同期cleanup中に古いpreflightが完了しても、Store/DOMを書き戻せない。
        this.initialization_generation += 1;

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
        if (
            this.live_arib_ttml_source !== null &&
            this.player?.plugins.mpegts === this.live_arib_ttml_source
        ) {
            this.live_arib_ttml_source.off(
                mpegts.Events.TIMED_ID3_METADATA_ARRIVED,
                this.live_arib_ttml_handler,
            );
        }
        this.live_arib_ttml_source = null;
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
        }

        // 破棄済みかどうかのフラグを立てる
        this.destroying = false;
        this.destroyed = true;

        // PlayerStore にプレイヤーを破棄したことを通知
        player_store.is_player_initialized = false;

        console.log('\u001b[31m[PlayerController] Destroyed.');
    }
}

export default PlayerController;
