import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';

import { createDeferred } from '../../../tests/helpers/deferred';

import { ILiveChannelDefault } from '@/services/Channels';
import { ILivePlaybackPolicy } from '@/services/player/LivePlaybackPolicy';
import PlayerController from '@/services/player/PlayerController';
import useChannelsStore from '@/stores/ChannelsStore';
import usePlayerStore from '@/stores/PlayerStore';
import useServerSettingsStore from '@/stores/ServerSettingsStore';
import useSettingsStore from '@/stores/SettingsStore';
import Utils from '@/utils';


type LiveLowLatencySettings = readonly [
    tv_low_latency_mode: boolean,
    tv_low_latency_mode_cellular: boolean,
];


type LifecycleTestController = {
    player: object | null;
    destroying: boolean;
    destroyed: boolean;
    destroy_promise: Promise<void> | null;
    restart_promise: Promise<void> | null;
    init_promise: Promise<void> | null;
    owner_signal: AbortSignal | null;
    lifecycle_generation: number;
    lifecycle_abort_controller: AbortController;
    playback_mode: 'Live' | 'Video';
    quality_profile_type: 'Wi-Fi' | 'Cellular';
    live_playback_policy_watcher_cancel: (() => void) | null;
    live_audio_track_interval_timer_cancel: (() => void) | null;
    live_media_info: {[key: string]: any} | null;
    is_live_startup_temporary_muted: boolean;
    live_arib_ttml_source: {
        on: (event: string, listener: (data: {pts: number; original_pts?: number; data: Uint8Array}) => void) => void;
        off: (event: string, listener: (data: {pts: number; original_pts?: number; data: Uint8Array}) => void) => void;
    } | null;
    konomitv_bs4k_live_quality_switch_coordinator: {destroy: () => Promise<void>} | null;
    konomitv_bs4k_live_cleanup_blocked: boolean;
    auto_quality_step_down_in_progress: boolean;
    auto_quality_step_down_last_at_ms: number;
    live_quality_manager_restart_chain: Promise<void>;
    player_managers: Array<{destroy: () => Promise<void>}>;
    screen_wake_lock: null;
    live_force_seek_interval_timer_cancel: (() => void) | null;
    video_keep_alive_interval_timer_cancel: (() => void) | null;
    recorded_arib_subtitle_cancel: (() => void) | null;
    recorded_arib_ttml_cancel: (() => void) | null;
    arib_ttml_renderer: {dispose: () => void} | null;
    player_container_resize_observer: null;
    lshaped_screen_crop_watchers: Array<() => void>;
    resolveLivePlaybackPolicy: (
        live_low_latency_settings?: LiveLowLatencySettings,
    ) => Readonly<ILivePlaybackPolicy>;
    setupLivePlaybackPolicyWatcher: (
        live_playback_policy: Readonly<ILivePlaybackPolicy>,
        initial_live_low_latency_settings: LiveLowLatencySettings,
    ) => void;
    initInternal: (
        options: {
            default_quality: string | null;
            playback_rate: number | null;
            seek_seconds: number | null;
        },
        lifecycle_generation: number,
        live_playback_policy: Readonly<ILivePlaybackPolicy>,
        initial_live_low_latency_settings: LiveLowLatencySettings,
    ) => Promise<void>;
    destroyAfterInitialization: () => Promise<void>;
    destroyInternal: () => Promise<void>;
    destroyKonomiTVBS4KLiveQualitySwitchCoordinator: () => Promise<void>;
    setupOwnerAbortCleanup: () => void;
    getPlaybackBufferSeconds: () => number;
    restartPlayer: (restart_player: () => Promise<void>) => Promise<void>;
    waitForLivePlaybackBuffer: (
        player: object,
        lifecycle_generation: number,
        target_seconds: number,
        signal: AbortSignal,
    ) => Promise<boolean>;
    setKonomiTVBS4KLiveABRFrozen: (frozen: boolean) => void;
    muteLiveStartupVideo: (video: HTMLVideoElement) => void;
    releaseLiveStartupVideoMute: (video: HTMLVideoElement) => void;
    setupLiveAudioTrackMonitor: () => void;
    getCurrentLiveAudioTrackMediaInfo: () => {[key: string]: any} | null;
    applyAudioTrackLabels: (media_info: {[key: string]: any}) => void;
    init: () => Promise<void>;
    destroy: () => Promise<void>;
};


function createController(): LifecycleTestController {
    const controller = Object.create(PlayerController.prototype) as LifecycleTestController;
    controller.player = null;
    controller.destroying = false;
    controller.destroyed = false;
    controller.destroy_promise = null;
    controller.restart_promise = null;
    controller.init_promise = null;
    controller.owner_signal = null;
    controller.lifecycle_generation = 1;
    controller.lifecycle_abort_controller = new AbortController();
    controller.playback_mode = 'Live';
    controller.quality_profile_type = 'Wi-Fi';
    controller.live_playback_policy_watcher_cancel = null;
    controller.live_audio_track_interval_timer_cancel = null;
    controller.live_media_info = null;
    controller.is_live_startup_temporary_muted = false;
    controller.live_arib_ttml_source = null;
    controller.konomitv_bs4k_live_quality_switch_coordinator = null;
    controller.konomitv_bs4k_live_cleanup_blocked = false;
    controller.auto_quality_step_down_in_progress = false;
    controller.auto_quality_step_down_last_at_ms = 0;
    controller.live_quality_manager_restart_chain = Promise.resolve();
    controller.player_managers = [];
    controller.screen_wake_lock = null;
    controller.live_force_seek_interval_timer_cancel = null;
    controller.video_keep_alive_interval_timer_cancel = null;
    controller.recorded_arib_subtitle_cancel = null;
    controller.recorded_arib_ttml_cancel = null;
    controller.arib_ttml_renderer = null;
    controller.player_container_resize_observer = null;
    controller.lshaped_screen_crop_watchers = [];
    // Object.create ではフィールド初期化子が走らないため、auto session を明示初期化する
    (controller as unknown as {konomitv_bs4k_auto_session_override: object})
        .konomitv_bs4k_auto_session_override = {};
    (controller as unknown as {session_live_playback_policy: null}).session_live_playback_policy = null;
    return controller;
}


function getLiveLowLatencySettings(): LiveLowLatencySettings {
    const settings_store = useSettingsStore();
    return [
        settings_store.settings.tv_low_latency_mode,
        settings_store.settings.tv_low_latency_mode_cellular,
    ];
}


function setCurrentBS4KChannel(): void {
    const channels_store = useChannelsStore();
    // 実環境では取得したチャンネル一覧を deep freeze しており、Vue の Proxy 化対象外になる。
    const bs4k_channel = Object.preventExtensions({
        ...structuredClone(ILiveChannelDefault),
        display_channel_id: 'bs4k101',
        type: 'BS4K' as const,
    });
    channels_store.channels_list.BS4K = [bs4k_channel];
    channels_store.is_channels_list_initial_updated = true;
    channels_store.display_channel_id = 'bs4k101';
}


describe('PlayerController lifecycle', () => {
    beforeEach(() => {
        localStorage.clear();
        setActivePinia(createPinia());
        // 低遅延設定ウォッチャー試験では自動画質（ネットワーク判定）を切る
        const settings_store = useSettingsStore();
        settings_store.settings.konomitv_bs4k_playback_auto_quality_mode = false;
        settings_store.settings.konomitv_bs4k_playback_auto_quality_mode_cellular = false;
    });

    it('Live ABR Cooldownは要求時でなく全cleanup後のfreeze解除時から数える', () => {
        vi.useFakeTimers();
        vi.setSystemTime(12_345);
        const controller = createController();
        controller.setKonomiTVBS4KLiveABRFrozen(true);
        expect(controller.auto_quality_step_down_last_at_ms).toBe(0);
        controller.setKonomiTVBS4KLiveABRFrozen(false);
        expect(controller.auto_quality_step_down_last_at_ms).toBe(12_345);

        vi.setSystemTime(99_999);
        controller.setKonomiTVBS4KLiveABRFrozen(false);
        expect(controller.auto_quality_step_down_last_at_ms).toBe(12_345);
    });

    it('Coordinator対象外の単一pipeline LiveはABR freezeを短時間後に解除する', () => {
        vi.useFakeTimers();
        const switch_quality = vi.fn();
        const controller = createController() as unknown as {
            player: {
                switchingQuality: boolean;
                qualityIndex: number;
                options: {
                    video: {
                        quality: Array<{name: string; url: string}>;
                    };
                };
                switchQuality: (index: number) => void;
                notice: () => void;
            };
            playback_mode: 'Live';
            destroyed: boolean;
            destroying: boolean;
            auto_quality_step_down_in_progress: boolean;
            auto_quality_step_down_last_at_ms: number;
            auto_quality_waiting_started_at_ms: number | null;
            auto_quality_marginal_buffer_started_at_ms: number | null;
            konomitv_bs4k_live_quality_switch_coordinator: null;
            konomitv_bs4k_auto_session_override: Record<string, unknown>;
            konomitv_bs4k_playback_video_profile_for_current_playback: {
                is_bs4k: false;
                streaming_quality: '1080p';
            };
            isKonomiTVBS4KAutoQualityModeEnabled: () => boolean;
            getKonomiTVBS4KPlaybackSelectableQualities: () => readonly ['1080p', '720p'];
            resolveKonomiTVBS4KPlaybackVideoProfileFromDPlayerQuality: (
                quality: {name: string; url: string},
            ) => {
                is_bs4k: false;
                streaming_quality: '1080p' | '720p';
            };
            isKonomiTVBS4KCurrentPlaybackVideoProfileSupported: () => boolean;
            requestAutoQualityStepDown: (reason: string) => boolean;
        };
        controller.player = {
            switchingQuality: false,
            qualityIndex: 0,
            options: {
                video: {
                    quality: [
                        {name: '1080p', url: '/1080p'},
                        {name: '720p', url: '/720p'},
                    ],
                },
            },
            switchQuality: switch_quality,
            notice: vi.fn(),
        };
        controller.konomitv_bs4k_live_quality_switch_coordinator = null;
        controller.konomitv_bs4k_auto_session_override = {};
        controller.konomitv_bs4k_playback_video_profile_for_current_playback = {
            is_bs4k: false,
            streaming_quality: '1080p',
        };
        controller.auto_quality_waiting_started_at_ms = null;
        controller.auto_quality_marginal_buffer_started_at_ms = null;
        controller.isKonomiTVBS4KAutoQualityModeEnabled = () => true;
        controller.getKonomiTVBS4KPlaybackSelectableQualities = () => ['1080p', '720p'];
        controller.resolveKonomiTVBS4KPlaybackVideoProfileFromDPlayerQuality = (quality) => ({
            is_bs4k: false,
            streaming_quality: quality.name === '720p' ? '720p' : '1080p',
        });
        controller.isKonomiTVBS4KCurrentPlaybackVideoProfileSupported = () => true;

        expect(controller.requestAutoQualityStepDown('radio')).toBe(true);
        expect(switch_quality).toHaveBeenCalledWith(1);
        expect(controller.auto_quality_step_down_in_progress).toBe(true);
        vi.advanceTimersByTime(2_000);
        expect(controller.auto_quality_step_down_in_progress).toBe(false);
    });

    afterEach(() => {
        vi.useRealTimers();
    });

    it('重複 destroy 呼び出しを同じ完了 Promise へ合流させる', async () => {
        const controller = createController();
        const deferred = createDeferred();
        const cleanup = vi.fn(() => deferred.promise);
        controller.destroyAfterInitialization = cleanup;

        const first = controller.destroy();
        const second = controller.destroy();

        expect(second).toBe(first);
        expect(cleanup).toHaveBeenCalledTimes(1);
        deferred.resolve();
        await first;
        expect(controller.destroy_promise).toBeNull();
    });

    it('destroyはStartupを含む進行中init完了後にだけcleanup barrier段階へ進む', async () => {
        const controller = createController();
        const init = createDeferred();
        const destroy_internal = vi.fn(async () => {});
        controller.init_promise = init.promise;
        controller.destroyInternal = destroy_internal;

        const destroy = controller.destroyAfterInitialization();
        await Promise.resolve();
        expect(destroy_internal).not.toHaveBeenCalled();

        init.resolve();
        await destroy;
        expect(destroy_internal).toHaveBeenCalledTimes(1);
    });

    it('destroy は active mpegts.js を破棄する前に字幕 listener を解除する', async () => {
        const controller = createController();
        const call_order: string[] = [];
        const off = vi.fn(() => call_order.push('off'));
        controller.live_arib_ttml_source = {on: vi.fn(), off};
        controller.konomitv_bs4k_live_quality_switch_coordinator = {
            destroy: vi.fn(async () => {
                call_order.push('destroy');
            }),
        };
        controller.destroyAfterInitialization = vi.fn(async () => {});

        await controller.destroy();

        expect(off).toHaveBeenCalledTimes(1);
        expect(call_order).toEqual(['off', 'destroy']);
        expect(controller.live_arib_ttml_source).toBeNull();
    });

    it('Coordinator cleanup完了前は参照とmpegts pluginを手放さない', async () => {
        const controller = createController();
        const deferred = createDeferred();
        const coordinator = {destroy: vi.fn(async () => await deferred.promise)};
        const plugins = {mpegts: {}};
        controller.player = {plugins};
        controller.konomitv_bs4k_live_quality_switch_coordinator = coordinator;

        const cleanup = controller.destroyKonomiTVBS4KLiveQualitySwitchCoordinator();
        await Promise.resolve();
        expect(controller.konomitv_bs4k_live_quality_switch_coordinator).toBe(coordinator);
        expect(plugins).toHaveProperty('mpegts');

        deferred.resolve();
        await cleanup;
        expect(controller.konomitv_bs4k_live_quality_switch_coordinator).toBeNull();
        expect(plugins).not.toHaveProperty('mpegts');
    });

    it('Coordinator cleanup未確認時は同じcontrollerの再初期化を永続的に拒否する', async () => {
        const controller = createController();
        const cleanup_error = new Error('retire cleanup failed');
        controller.konomitv_bs4k_live_quality_switch_coordinator = {
            destroy: vi.fn(async () => {
                throw cleanup_error;
            }),
        };
        const init_internal = vi.fn(async () => {});
        controller.initInternal = init_internal;

        await expect(
            controller.destroyKonomiTVBS4KLiveQualitySwitchCoordinator(),
        ).rejects.toBe(cleanup_error);
        expect(controller.konomitv_bs4k_live_cleanup_blocked).toBe(true);
        await expect(controller.init()).rejects.toThrow(
            '前のライブ再生セッションのクリーンアップを確認できない',
        );
        expect(init_internal).not.toHaveBeenCalled();
    });

    it('Coordinator cleanup失敗後も残りのlocal resourceを回収してから同じerrorを返す', async () => {
        const controller = createController();
        const cleanup_error = new Error('retire cleanup failed');
        const renderer_dispose = vi.fn();
        controller.konomitv_bs4k_live_quality_switch_coordinator = {
            destroy: vi.fn(async () => {
                throw cleanup_error;
            }),
        };
        controller.arib_ttml_renderer = {dispose: renderer_dispose};
        const console_error = vi.spyOn(console, 'error').mockImplementation(() => undefined);

        await expect(controller.destroyInternal()).rejects.toBe(cleanup_error);

        expect(renderer_dispose).toHaveBeenCalledTimes(1);
        expect(controller.arib_ttml_renderer).toBeNull();
        expect(controller.player).toBeNull();
        expect(controller.destroyed).toBe(true);
        expect(controller.destroying).toBe(false);
        expect(controller.konomitv_bs4k_live_cleanup_blocked).toBe(true);
        console_error.mockRestore();
    });

    it('Coordinator cleanup失敗後の再destroyも同じerrorを返して成功へ反転しない', async () => {
        const controller = createController();
        const cleanup_error = new Error('retire cleanup failed');
        controller.konomitv_bs4k_live_quality_switch_coordinator = {
            destroy: vi.fn(async () => {
                throw cleanup_error;
            }),
        };
        const console_error = vi.spyOn(console, 'error').mockImplementation(() => undefined);

        await expect(controller.destroy()).rejects.toBe(cleanup_error);
        expect(controller.destroyed).toBe(true);
        await expect(controller.destroy()).rejects.toBe(cleanup_error);

        console_error.mockRestore();
    });

    it('再起動処理はCoordinator cleanup未確認ならinitを一度も呼ばない', async () => {
        const controller = createController();
        controller.konomitv_bs4k_live_quality_switch_coordinator = {
            destroy: vi.fn(async () => {
                throw new Error('retire failed');
            }),
        };
        const init = vi.fn(async () => {});
        controller.init = init;

        await expect(controller.restartPlayer(async () => {
            await controller.destroyKonomiTVBS4KLiveQualitySwitchCoordinator();
            await controller.init();
        })).rejects.toThrow('retire failed');

        expect(init).not.toHaveBeenCalled();
        expect(controller.konomitv_bs4k_live_cleanup_blocked).toBe(true);
    });

    it('owner abortのfire-and-forget cleanup失敗を捕捉してunhandled rejectionにしない', async () => {
        const controller = createController();
        const owner_abort_controller = new AbortController();
        controller.owner_signal = owner_abort_controller.signal;
        const cleanup_error = new Error('owner cleanup failed');
        controller.destroy = vi.fn(async () => {
            throw cleanup_error;
        });
        const console_error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
        controller.setupOwnerAbortCleanup();

        owner_abort_controller.abort();
        await Promise.resolve();
        await Promise.resolve();

        expect(controller.destroy).toHaveBeenCalledTimes(1);
        expect(console_error).toHaveBeenCalledWith(
            '[PlayerController] Owner abort cleanup failed.',
            cleanup_error,
        );
        console_error.mockRestore();
    });

    it('Coordinator が先に video をミュート済みでも Startup 一時ミュートとして所有する', () => {
        const controller = createController();
        const video = document.createElement('video');
        video.muted = true;

        controller.muteLiveStartupVideo(video);

        expect(video.muted).toBe(true);
        expect(controller.is_live_startup_temporary_muted).toBe(true);
    });

    it('Startup 一時ミュートは muted=false を反映してから所有を解除する', () => {
        const controller = createController();
        const ownership_during_muted_assignment: boolean[] = [];
        const video = {
            set muted(value: boolean) {
                if (value === false) {
                    ownership_during_muted_assignment.push(controller.is_live_startup_temporary_muted);
                }
            },
        } as HTMLVideoElement;
        controller.is_live_startup_temporary_muted = true;

        controller.releaseLiveStartupVideoMute(video);

        expect(ownership_during_muted_assignment).toEqual([true]);
        expect(controller.is_live_startup_temporary_muted).toBe(false);
    });

    it('旧世代の音声トラック監視 callback は破棄済み mpegts.js を参照しない', () => {
        const controller = createController();
        const interval_callbacks: Array<() => void> = [];
        vi.spyOn(Utils, 'setIntervalInWorker').mockImplementation((callback) => {
            interval_callbacks.push(callback);
            return vi.fn();
        });
        const media_info_getter = vi.fn(() => ({audioTracks: []}));
        const mpegts_player = Object.defineProperty({}, 'mediaInfo', {
            get: media_info_getter,
        });
        controller.player = {
            plugins: {mpegts: mpegts_player},
            video: document.createElement('video'),
        };
        controller.applyAudioTrackLabels = vi.fn();

        controller.setupLiveAudioTrackMonitor();
        expect(interval_callbacks).toHaveLength(1);
        interval_callbacks[0]();
        expect(media_info_getter).toHaveBeenCalledTimes(1);
        expect(controller.applyAudioTrackLabels).toHaveBeenCalledTimes(1);

        controller.lifecycle_generation += 1;
        interval_callbacks[0]();
        expect(media_info_getter).toHaveBeenCalledTimes(1);
        expect(controller.applyAudioTrackLabels).toHaveBeenCalledTimes(1);
    });

    it('単一pipeline下降中は破棄済みmpegts.jsのmediaInfoを参照しない', () => {
        const controller = createController();
        const retained_media_info = {audioTrackCount: 2};
        const media_info_getter = vi.fn(() => {
            throw new Error('destroy済みengineのmediaInfoが参照された');
        });
        const mpegts_player = Object.defineProperty(
            {_player_engine: null},
            'mediaInfo',
            {get: media_info_getter},
        );
        controller.player = {
            plugins: {mpegts: mpegts_player},
            video: document.createElement('video'),
        };
        controller.live_media_info = retained_media_info;

        expect(controller.getCurrentLiveAudioTrackMediaInfo()).toBe(retained_media_info);
        expect(media_info_getter).not.toHaveBeenCalled();
    });

    it('destroy 待機中の二重 restart を一つの世代へ合流させる', async () => {
        const controller = createController();
        const deferred = createDeferred();
        const destroy = vi.fn(() => deferred.promise);
        const init = vi.fn(async () => {});
        const restart = vi.fn(async () => {
            await destroy();
            await init();
        });

        const first = controller.restartPlayer(restart);
        const second = controller.restartPlayer(restart);

        expect(second).toBe(first);
        expect(restart).toHaveBeenCalledTimes(1);
        expect(destroy).toHaveBeenCalledTimes(1);
        expect(init).not.toHaveBeenCalled();
        deferred.resolve();
        await first;
        expect(init).toHaveBeenCalledTimes(1);
        expect(controller.restart_promise).toBeNull();
    });

    it('live buffer 待機を destroy 世代の abort で終了し timer を残さない', async () => {
        vi.useFakeTimers();
        const controller = createController();
        const player = {};
        controller.player = player;
        controller.getPlaybackBufferSeconds = () => 0;

        const waiting = controller.waitForLivePlaybackBuffer(
            player,
            controller.lifecycle_generation,
            4,
            controller.lifecycle_abort_controller.signal,
        );
        expect(vi.getTimerCount()).toBe(1);

        controller.lifecycle_abort_controller.abort();
        controller.lifecycle_generation += 1;

        await expect(waiting).resolves.toBe(false);
        expect(vi.getTimerCount()).toBe(0);
    });

    it('旧 DPlayer の待機は新しい player identity へ書き込めない', async () => {
        vi.useFakeTimers();
        const controller = createController();
        const old_player = {};
        controller.player = old_player;
        controller.getPlaybackBufferSeconds = () => 0;

        const waiting = controller.waitForLivePlaybackBuffer(
            old_player,
            controller.lifecycle_generation,
            4,
            controller.lifecycle_abort_controller.signal,
        );
        controller.player = {};
        await vi.advanceTimersByTimeAsync(100);

        await expect(waiting).resolves.toBe(false);
    });

    it('低遅延ポリシーを init 世代ごとに一度だけ解決し、初期化処理へ不変値として渡す', async () => {
        const controller = createController();
        const live_playback_policy = Object.freeze<ILivePlaybackPolicy>({
            mode: 'LowLatency',
            target_buffer_seconds: 0.9,
            live_sync_enabled: true,
            max_latency_seconds: 3,
            catch_up_rate: 1.1,
        });
        const resolve_live_playback_policy = vi.fn(() => live_playback_policy);
        const init_internal = vi.fn(async (
            _options: {
                default_quality: string | null;
                playback_rate: number | null;
                seek_seconds: number | null;
            },
            _lifecycle_generation: number,
            received_policy: Readonly<ILivePlaybackPolicy>,
            received_live_low_latency_settings: LiveLowLatencySettings,
        ) => {
            expect(received_policy).toBe(live_playback_policy);
            expect(received_live_low_latency_settings).toEqual([true, false]);
        });
        controller.resolveLivePlaybackPolicy = resolve_live_playback_policy;
        controller.initInternal = init_internal;

        await controller.init();

        expect(resolve_live_playback_policy).toHaveBeenCalledTimes(1);
        expect(resolve_live_playback_policy).toHaveBeenCalledWith([true, false]);
        expect(init_internal).toHaveBeenCalledTimes(1);
    });

    it('通常ライブではアクティブ回線の実効低遅延変更だけを soft 切替する', async () => {
        const controller = createController();
        const settings_store = useSettingsStore();
        const restart_required = vi.fn();
        usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);
        const live_playback_policy = controller.resolveLivePlaybackPolicy();
        expect(live_playback_policy.live_sync_enabled).toBe(true);

        controller.setupLivePlaybackPolicyWatcher(live_playback_policy, getLiveLowLatencySettings());

        // Wi-Fi プロファイルで視聴中は Cellular 側だけを変更しても、現セッションの実効値は変化しない。
        settings_store.settings.tv_low_latency_mode_cellular = true;
        await nextTick();
        expect(restart_required).not.toHaveBeenCalled();
        expect(live_playback_policy.live_sync_enabled).toBe(true);

        // アクティブな Wi-Fi 側の変更は destroy せず soft 切替する。
        settings_store.settings.tv_low_latency_mode = false;
        await nextTick();
        expect(restart_required).not.toHaveBeenCalled();
        expect(live_playback_policy.live_sync_enabled).toBe(false);

        settings_store.settings.tv_low_latency_mode = true;
        await nextTick();
        expect(restart_required).not.toHaveBeenCalled();
        expect(live_playback_policy.live_sync_enabled).toBe(true);
    });

    it('BS4Kライブは通常側と独立した低遅延設定を使う', () => {
        const controller = createController();
        setCurrentBS4KChannel();
        const settings_store = useSettingsStore();
        const server_settings_store = useServerSettingsStore();
        server_settings_store.server_settings.general.bs4k_ignore_viewer_low_latency = false;

        // 通常側は ON、BS4K 側は OFF → BS4K 視聴では OFF になる。
        settings_store.settings.tv_low_latency_mode = true;
        settings_store.settings.tv_low_latency_mode_for_bs4k = false;
        expect(controller.resolveLivePlaybackPolicy().live_sync_enabled).toBe(false);

        // BS4K 側だけ ON にすると、通常側が OFF でも BS4K は ON になる。
        settings_store.settings.tv_low_latency_mode = false;
        settings_store.settings.tv_low_latency_mode_for_bs4k = true;
        expect(controller.resolveLivePlaybackPolicy().live_sync_enabled).toBe(true);
    });

    it('BS4K強制安定化中は視聴者設定の変更で実効値が変わらないため再起動しない', async () => {
        const controller = createController();
        setCurrentBS4KChannel();

        const server_settings_store = useServerSettingsStore();
        server_settings_store.server_settings.general.bs4k_ignore_viewer_low_latency = true;
        const settings_store = useSettingsStore();
        const restart_required = vi.fn();
        usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);
        const live_playback_policy = controller.resolveLivePlaybackPolicy();
        expect(live_playback_policy.live_sync_enabled).toBe(false);

        controller.setupLivePlaybackPolicyWatcher(
            live_playback_policy,
            [
                settings_store.settings.tv_low_latency_mode_for_bs4k,
                settings_store.settings.tv_low_latency_mode_for_bs4k_cellular,
            ],
        );

        // 強制安定化が有効な間は、通常側・BS4K 側どちらの視聴者設定が変わっても実効値は OFF のまま維持される。
        settings_store.settings.tv_low_latency_mode = false;
        await nextTick();
        settings_store.settings.tv_low_latency_mode = true;
        await nextTick();
        settings_store.settings.tv_low_latency_mode_for_bs4k = false;
        await nextTick();
        settings_store.settings.tv_low_latency_mode_for_bs4k = true;
        await nextTick();
        expect(restart_required).not.toHaveBeenCalled();
    });

    it('BS4KライブではBS4K用低遅延設定の変更だけを soft 切替する', async () => {
        const controller = createController();
        setCurrentBS4KChannel();
        const settings_store = useSettingsStore();
        const server_settings_store = useServerSettingsStore();
        server_settings_store.server_settings.general.bs4k_ignore_viewer_low_latency = false;
        settings_store.settings.tv_low_latency_mode_for_bs4k = true;
        const restart_required = vi.fn();
        usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);
        const live_playback_policy = controller.resolveLivePlaybackPolicy();
        expect(live_playback_policy.live_sync_enabled).toBe(true);

        controller.setupLivePlaybackPolicyWatcher(
            live_playback_policy,
            [
                settings_store.settings.tv_low_latency_mode_for_bs4k,
                settings_store.settings.tv_low_latency_mode_for_bs4k_cellular,
            ],
        );

        // 通常側の変更は BS4K 視聴中の実効値に影響しない。
        settings_store.settings.tv_low_latency_mode = false;
        await nextTick();
        expect(restart_required).not.toHaveBeenCalled();
        expect(live_playback_policy.live_sync_enabled).toBe(true);

        // BS4K 側の変更は soft 切替する。
        settings_store.settings.tv_low_latency_mode_for_bs4k = false;
        await nextTick();
        expect(restart_required).not.toHaveBeenCalled();
        expect(live_playback_policy.live_sync_enabled).toBe(false);
    });

    it('チャンネル切り替えとBS4K上書き状態だけの変化を旧controllerの再起動契機にしない', async () => {
        const controller = createController();
        const restart_required = vi.fn();
        usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);
        const live_playback_policy = controller.resolveLivePlaybackPolicy();
        const initial_live_low_latency_settings = getLiveLowLatencySettings();
        expect(live_playback_policy.live_sync_enabled).toBe(true);

        // 通常局のポリシー解決後に BS4K へ切り替わっても、視聴者設定が同じなら登録時に再起動を要求しない。
        setCurrentBS4KChannel();
        const server_settings_store = useServerSettingsStore();
        server_settings_store.server_settings.general.bs4k_ignore_viewer_low_latency = true;
        controller.setupLivePlaybackPolicyWatcher(live_playback_policy, initial_live_low_latency_settings);
        expect(restart_required).not.toHaveBeenCalled();

        // 登録後に BS4K 上書き状態だけを変更しても、watch source ではないため旧 controller は反応しない。
        server_settings_store.server_settings.general.bs4k_ignore_viewer_low_latency = false;
        await nextTick();
        expect(restart_required).not.toHaveBeenCalled();
    });

    it('ポリシー解決後からウォッチャー登録までの設定変更も soft 切替で取りこぼさない', () => {
        const controller = createController();
        const settings_store = useSettingsStore();
        const restart_required = vi.fn();
        usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);
        const live_playback_policy = controller.resolveLivePlaybackPolicy();
        expect(live_playback_policy.live_sync_enabled).toBe(true);
        const initial_live_low_latency_settings = getLiveLowLatencySettings();

        // 非同期初期化の途中で設定が変わった状況を再現する。
        settings_store.settings.tv_low_latency_mode = false;
        controller.setupLivePlaybackPolicyWatcher(live_playback_policy, initial_live_low_latency_settings);

        expect(restart_required).not.toHaveBeenCalled();
        expect(live_playback_policy.live_sync_enabled).toBe(false);
        expect(controller.live_playback_policy_watcher_cancel).not.toBeNull();
    });

    it('録画再生ではライブ低遅延設定ウォッチャーを登録しない', async () => {
        const controller = createController();
        controller.playback_mode = 'Video';
        const settings_store = useSettingsStore();
        const restart_required = vi.fn();
        usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);

        controller.setupLivePlaybackPolicyWatcher(
            controller.resolveLivePlaybackPolicy(),
            getLiveLowLatencySettings(),
        );
        expect(controller.live_playback_policy_watcher_cancel).toBeNull();

        settings_store.settings.tv_low_latency_mode = false;
        await nextTick();
        expect(restart_required).not.toHaveBeenCalled();
    });

    it('destroy 時に低遅延設定ウォッチャーを解除し、旧世代から再起動要求を出さない', async () => {
        const controller = createController();
        const settings_store = useSettingsStore();
        const restart_required = vi.fn();
        usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);
        controller.setupLivePlaybackPolicyWatcher(
            controller.resolveLivePlaybackPolicy(),
            getLiveLowLatencySettings(),
        );
        controller.destroyAfterInitialization = vi.fn(async () => {});

        await controller.destroy();
        expect(controller.live_playback_policy_watcher_cancel).toBeNull();

        settings_store.settings.tv_low_latency_mode = false;
        await nextTick();
        expect(restart_required).not.toHaveBeenCalled();
    });
});
