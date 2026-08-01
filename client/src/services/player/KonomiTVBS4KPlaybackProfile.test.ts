import { AxiosError } from 'axios';
import { ErrorDetails, ErrorTypes, type ErrorData } from 'hls.js';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createDeferred } from '../../../tests/helpers/deferred';

import type DPlayer from 'dplayer';


import APIClient from '@/services/APIClient';
import PlayerController from '@/services/player/PlayerController';
import Videos, { type IKonomiTVBS4KPlaybackCapabilities } from '@/services/Videos';
import useChannelsStore from '@/stores/ChannelsStore';
import usePlayerStore from '@/stores/PlayerStore';
import useSettingsStore, {
    BS4K_LIVE_STREAMING_QUALITIES,
    getKonomiTVBS4KPlaybackAudioCodecSettingKey,
    getKonomiTVBS4KPlaybackVideoCodecSettingKey,
    type IKonomiTVBS4KPlaybackVideoProfile,
    type KonomiTVBS4KPlaybackAudioCodec,
    type KonomiTVBS4KPlaybackVideoCodec,
} from '@/stores/SettingsStore';
import { useSnackbarsStore } from '@/stores/SnackbarsStore';
import Utils, { PlayerUtils } from '@/utils';


const KONOMITV_BS4K_PLAYBACK_CAPABILITIES: IKonomiTVBS4KPlaybackCapabilities = {
    video: [
        {
            encoder: 'FFmpeg',
            codec: 'avc',
            bit_depth: 8,
            profile: 'libx264',
            live_available: true,
            recorded_available: true,
            live_reason_code: null,
            recorded_reason_code: null,
        },
        {
            encoder: 'FFmpeg',
            codec: 'vp9',
            bit_depth: 10,
            profile: 'libvpx-vp9',
            live_available: true,
            recorded_available: true,
            live_reason_code: null,
            recorded_reason_code: null,
        },
    ],
    audio: [
        {
            codec: 'aac',
            live_available: true,
            recorded_available: true,
            live_reason_code: null,
            recorded_reason_code: null,
        },
        {
            codec: 'opus',
            live_available: true,
            recorded_available: false,
            live_reason_code: null,
            recorded_reason_code: 'ProbeFailed',
        },
    ],
    live_combinations: [
        {
            encoder: 'FFmpeg',
            video_codec: 'avc',
            video_bit_depth: 8,
            audio_codec: 'aac',
            available: true,
            reason_code: null,
        },
        {
            encoder: 'FFmpeg',
            video_codec: 'avc',
            video_bit_depth: 8,
            audio_codec: 'opus',
            available: true,
            reason_code: null,
        },
        {
            encoder: 'FFmpeg',
            video_codec: 'vp9',
            video_bit_depth: 10,
            audio_codec: 'aac',
            available: true,
            reason_code: null,
        },
        {
            encoder: 'FFmpeg',
            video_codec: 'vp9',
            video_bit_depth: 10,
            audio_codec: 'opus',
            available: true,
            reason_code: null,
        },
    ],
};


type KonomiTVBS4KCompatibilityFallbackController = {
    playback_mode: 'Live' | 'Video';
    konomitv_bs4k_compatibility_playback_fallback_attempted: boolean;
    konomitv_bs4k_force_recorded_aac_audio_codec: boolean;
    konomitv_bs4k_playback_video_codec_for_current_playback: KonomiTVBS4KPlaybackVideoCodec;
    konomitv_bs4k_playback_audio_codec_for_current_playback: KonomiTVBS4KPlaybackAudioCodec;
    requestKonomiTVBS4KCompatibilityPlaybackFallback: (reason: string) => boolean;
    isKonomiTVBS4KCompatibilityPlaybackFallbackPending: () => boolean;
};

type KonomiTVBS4KAutoCompatibilityFallbackController =
    KonomiTVBS4KCompatibilityFallbackController & {
        quality_profile_type: 'Wi-Fi' | 'Cellular';
        konomitv_bs4k_auto_session_override: {
            video_codec?: KonomiTVBS4KPlaybackVideoCodec;
            audio_codec?: KonomiTVBS4KPlaybackAudioCodec;
        };
        konomitv_bs4k_auto_failed_video_codecs: Set<KonomiTVBS4KPlaybackVideoCodec>;
        konomitv_bs4k_current_playback_has_video: boolean;
        konomitv_bs4k_playback_capabilities_for_current_playback: IKonomiTVBS4KPlaybackCapabilities;
        konomitv_bs4k_playback_capabilities_for_ui: IKonomiTVBS4KPlaybackCapabilities;
        konomitv_bs4k_playback_encoder_for_current_playback: 'FFmpeg';
        konomitv_bs4k_playback_video_profile_for_current_playback: IKonomiTVBS4KPlaybackVideoProfile;
    };

type LiveAudioTrackLabelController = {
    playback_mode: 'Live';
    player: DPlayer;
    live_selected_audio_track_index: number;
    applyAudioTrackLabels: (media_info: {[key: string]: unknown}) => void;
};

type MediaReadinessController = {
    isMediaReadyToStartPlayback: (video: HTMLVideoElement) => boolean;
};

type KonomiTVBS4KServerPipelineFallbackController =
    KonomiTVBS4KCompatibilityFallbackController & {
        handleKonomiTVBS4KLivePlaybackPipelineStatus: (
            status: 'Offline' | 'Standby' | 'ONAir' | 'Idling' | 'Restart',
            detail: string,
        ) => boolean;
        handleKonomiTVBS4KRecordedHLSLoadError: (data: ErrorData) => boolean;
    };

type KonomiTVBS4KQualityProfileController = {
    playback_mode: 'Live' | 'Video';
    quality_profile_type: 'Wi-Fi' | 'Cellular';
    resolveKonomiTVBS4KEffectivePlaybackVideoProfile: (
        is_bs4k: boolean,
        default_quality: string | null,
    ) => IKonomiTVBS4KPlaybackVideoProfile;
};

type KonomiTVBS4KPlaybackCodecResolverController =
    KonomiTVBS4KCompatibilityFallbackController & {
        konomitv_bs4k_current_playback_has_video: boolean;
        doesKonomiTVBS4KCurrentPlaybackHaveVideo: () => boolean;
        resolveKonomiTVBS4KPlaybackCodecsForCurrentMedia: (
            capabilities: IKonomiTVBS4KPlaybackCapabilities,
            encoder: 'FFmpeg' | 'QSV' | 'NVENC' | 'AMF',
            requested_video_codec: KonomiTVBS4KPlaybackVideoCodec,
            requested_audio_codec: KonomiTVBS4KPlaybackAudioCodec,
            video_profile: IKonomiTVBS4KPlaybackVideoProfile,
        ) => {
            video_codec: KonomiTVBS4KPlaybackVideoCodec;
            video_bit_depth: 8 | 10;
            audio_codec: KonomiTVBS4KPlaybackAudioCodec;
        };
    };

type KonomiTVBS4KNativeErrorController = KonomiTVBS4KCompatibilityFallbackController & {
    player: DPlayer;
    owner_signal: AbortSignal | null;
    destroying: boolean;
    destroyed: boolean;
    lifecycle_generation: number;
    lifecycle_abort_controller: AbortController;
    konomitv_bs4k_native_playback_error_handler_registration: {
        player: DPlayer;
        lifecycle_generation: number;
    } | null;
    setupKonomiTVBS4KNativePlaybackErrorHandler: (
        lifecycle_generation: number,
        setup_player: DPlayer,
    ) => void;
};

type KonomiTVBS4KQualitySwitchGuardController = KonomiTVBS4KCompatibilityFallbackController & {
    player: DPlayer;
    quality_profile_type: 'Wi-Fi' | 'Cellular';
    owner_signal: AbortSignal | null;
    destroying: boolean;
    destroyed: boolean;
    lifecycle_generation: number;
    lifecycle_abort_controller: AbortController;
    konomitv_bs4k_playback_capabilities_for_current_playback: IKonomiTVBS4KPlaybackCapabilities;
    konomitv_bs4k_playback_encoder_for_current_playback: 'FFmpeg';
    konomitv_bs4k_playback_video_bit_depth_for_current_playback: 8 | 10;
    konomitv_bs4k_current_playback_has_video: boolean;
    konomitv_bs4k_playback_video_profile_for_current_playback: IKonomiTVBS4KPlaybackVideoProfile;
    konomitv_bs4k_unsupported_quality_restart_pending: boolean;
    setupKonomiTVBS4KPlaybackQualitySwitchGuard: (
        lifecycle_generation: number,
        setup_player: DPlayer,
    ) => void;
    handleKonomiTVBS4KPlaybackQualityStart: (
        lifecycle_generation: number,
        setup_player: DPlayer,
        target_quality: {name: string; url: string},
    ) => boolean;
};

type KonomiTVBS4KSettingPanelController = KonomiTVBS4KCompatibilityFallbackController & {
    player: DPlayer;
    quality_profile_type: 'Wi-Fi' | 'Cellular';
    konomitv_bs4k_playback_capabilities_for_current_playback: IKonomiTVBS4KPlaybackCapabilities;
    konomitv_bs4k_playback_capabilities_for_ui: IKonomiTVBS4KPlaybackCapabilities;
    konomitv_bs4k_playback_encoder_for_current_playback: 'FFmpeg';
    konomitv_bs4k_playback_video_profile_for_current_playback: IKonomiTVBS4KPlaybackVideoProfile;
    setupSettingPanelHandler: (live_playback_policy: Readonly<{live_sync_enabled: boolean}>) => void;
};


function createKonomiTVBS4KCompatibilityFallbackController(
    playback_mode: 'Live' | 'Video',
): KonomiTVBS4KCompatibilityFallbackController {
    const controller = Object.create(PlayerController.prototype) as KonomiTVBS4KCompatibilityFallbackController & {
        konomitv_bs4k_auto_session_override: Record<string, unknown>;
        konomitv_bs4k_auto_failed_video_codecs: Set<string>;
    };
    controller.playback_mode = playback_mode;
    controller.konomitv_bs4k_compatibility_playback_fallback_attempted = false;
    controller.konomitv_bs4k_force_recorded_aac_audio_codec = false;
    controller.konomitv_bs4k_playback_video_codec_for_current_playback = 'vp9';
    controller.konomitv_bs4k_playback_audio_codec_for_current_playback = 'opus';
    // Object.create ではフィールド初期化子が走らないため、自動画質パスが触る状態を明示する
    controller.konomitv_bs4k_auto_session_override = {};
    controller.konomitv_bs4k_auto_failed_video_codecs = new Set();
    return controller;
}

/** 手動プロファイル試験では自動画質（ネットワーク判定）を切る */
function disableKonomiTVBS4KAutoQualityMode(): void {
    const settings_store = useSettingsStore();
    settings_store.settings.konomitv_bs4k_playback_auto_quality_mode = false;
    settings_store.settings.konomitv_bs4k_playback_auto_quality_mode_cellular = false;
}

function createKonomiTVBS4KHLSHTTPError(
    details: ErrorDetails,
    url: string,
    code: number,
): ErrorData {
    return {
        type: ErrorTypes.NETWORK_ERROR,
        details,
        error: new Error(`HTTP ${code}`),
        fatal: true,
        response: {url, code, text: `HTTP ${code}`},
    };
}

function getKonomiTVBS4KQualityProfile(
    playback_mode: 'Live' | 'Video',
    quality_profile_type: 'Wi-Fi' | 'Cellular',
): Record<string, unknown> {
    const controller = Object.create(PlayerController.prototype) as KonomiTVBS4KQualityProfileController & {
        konomitv_bs4k_auto_session_override: Record<string, unknown>;
    };
    controller.playback_mode = playback_mode;
    controller.quality_profile_type = quality_profile_type;
    controller.konomitv_bs4k_auto_session_override = {};
    const getter = Object.getOwnPropertyDescriptor(PlayerController.prototype, 'quality_profile')?.get;
    if (getter === undefined) throw new Error('KonomiTV-BS4K quality profile getter is unavailable.');
    return getter.call(controller) as Record<string, unknown>;
}

function createKonomiTVBS4KNativeErrorController(
    playback_mode: 'Live' | 'Video',
): {
        controller: KonomiTVBS4KNativeErrorController;
        player: DPlayer;
        on: ReturnType<typeof vi.fn>;
        getErrorHandler: () => (() => Promise<void>) | null;
    } {
    let error_handler: (() => Promise<void>) | null = null;
    const on = vi.fn((event_name: string, handler: () => Promise<void>) => {
        if (event_name === 'error') error_handler = handler;
    });
    const media_error = {
        code: 3,
        message: 'decode failed',
        MEDIA_ERR_DECODE: 3,
    } as MediaError;
    const player = {
        on,
        video: {error: media_error},
    } as unknown as DPlayer;
    const controller = createKonomiTVBS4KCompatibilityFallbackController(
        playback_mode,
    ) as KonomiTVBS4KNativeErrorController;
    controller.player = player;
    controller.owner_signal = null;
    controller.destroying = false;
    controller.destroyed = false;
    controller.lifecycle_generation = 1;
    controller.lifecycle_abort_controller = new AbortController();
    controller.konomitv_bs4k_native_playback_error_handler_registration = null;
    return {
        controller,
        player,
        on,
        getErrorHandler: () => error_handler,
    };
}


function createKonomiTVBS4KQualitySwitchGuardController(): {
    controller: KonomiTVBS4KQualitySwitchGuardController;
    player: DPlayer;
    originalSwitchQuality: ReturnType<typeof vi.fn>;
    notice: ReturnType<typeof vi.fn>;
    pause: ReturnType<typeof vi.fn>;
} {
    const originalSwitchQuality = vi.fn();
    const notice = vi.fn();
    const pause = vi.fn();
    const player = {
        switchQuality: originalSwitchQuality,
        options: {
            video: {
                quality: [
                    {name: '8K', url: '/streams/live/bs4k/4320p/mpegts'},
                    {name: '4K', url: '/streams/live/bs4k/2160p/mpegts'},
                ],
            },
        },
        qualityIndex: 1,
        switchingQuality: false,
        notice,
        video: {pause},
    } as unknown as DPlayer;
    const controller = createKonomiTVBS4KCompatibilityFallbackController(
        'Live',
    ) as KonomiTVBS4KQualitySwitchGuardController;
    controller.player = player;
    controller.quality_profile_type = 'Wi-Fi';
    controller.owner_signal = null;
    controller.destroying = false;
    controller.destroyed = false;
    controller.lifecycle_generation = 1;
    controller.lifecycle_abort_controller = new AbortController();
    controller.konomitv_bs4k_playback_capabilities_for_current_playback =
        structuredClone(KONOMITV_BS4K_PLAYBACK_CAPABILITIES);
    controller.konomitv_bs4k_playback_encoder_for_current_playback = 'FFmpeg';
    controller.konomitv_bs4k_playback_video_codec_for_current_playback = 'vp9';
    controller.konomitv_bs4k_playback_video_bit_depth_for_current_playback = 10;
    controller.konomitv_bs4k_playback_audio_codec_for_current_playback = 'aac';
    controller.konomitv_bs4k_current_playback_has_video = true;
    controller.konomitv_bs4k_playback_video_profile_for_current_playback = {
        is_bs4k: true,
        streaming_quality: '2160p',
    };
    controller.konomitv_bs4k_unsupported_quality_restart_pending = false;
    return {controller, player, originalSwitchQuality, notice, pause};
}


function createKonomiTVBS4KSettingPanelController(
    capabilities: IKonomiTVBS4KPlaybackCapabilities,
    current_video_codec: KonomiTVBS4KPlaybackVideoCodec,
    current_audio_codec: KonomiTVBS4KPlaybackAudioCodec,
): {
        controller: KonomiTVBS4KSettingPanelController;
        container: HTMLElement;
        hide: ReturnType<typeof vi.fn>;
    } {
    const container = document.createElement('div');
    container.innerHTML = `
        <div class="dplayer-setting-box">
            <div class="dplayer-setting-origin-panel">
                <div class="dplayer-setting-item dplayer-setting-audio">
                    <span class="dplayer-label-value"></span>
                    <div class="dplayer-toggle"></div>
                </div>
            </div>
        </div>
    `;
    const setting_box = container.querySelector<HTMLElement>('.dplayer-setting-box');
    const setting_origin_panel = container.querySelector<HTMLElement>('.dplayer-setting-origin-panel');
    const audio = container.querySelector<HTMLElement>('.dplayer-setting-audio');
    if (setting_box === null || setting_origin_panel === null || audio === null) {
        throw new Error('DPlayer setting panel fixture is incomplete.');
    }
    const hide = vi.fn();
    const player = {
        container,
        plugins: {},
        setting: {
            hide,
            show: vi.fn(),
        },
        template: {
            audio,
            settingBox: setting_box,
            settingOriginPanel: setting_origin_panel,
        },
    } as unknown as DPlayer;
    const controller = createKonomiTVBS4KCompatibilityFallbackController(
        'Live',
    ) as KonomiTVBS4KSettingPanelController;
    controller.player = player;
    controller.quality_profile_type = 'Wi-Fi';
    controller.konomitv_bs4k_playback_capabilities_for_current_playback = capabilities;
    // 設定パネルは full 行列を参照する。テストは fixture を UI 側にも渡す。
    controller.konomitv_bs4k_playback_capabilities_for_ui = capabilities;
    controller.konomitv_bs4k_playback_encoder_for_current_playback = 'FFmpeg';
    controller.konomitv_bs4k_playback_video_codec_for_current_playback = current_video_codec;
    controller.konomitv_bs4k_playback_audio_codec_for_current_playback = current_audio_codec;
    controller.konomitv_bs4k_playback_video_profile_for_current_playback = {
        is_bs4k: false,
        streaming_quality: '1080p',
    };
    controller.setupSettingPanelHandler({live_sync_enabled: true});
    return {controller, container, hide};
}


describe('共通再生プロファイル能力判定', () => {
    beforeEach(() => {
        localStorage.clear();
        setActivePinia(createPinia());
        disableKonomiTVBS4KAutoQualityMode();
        vi.stubGlobal('ManagedMediaSource', undefined);
    });

    afterEach(() => {
        vi.restoreAllMocks();
        vi.unstubAllGlobals();
    });

    it('KonomiTV-BS4K名前空間の能力APIだけを参照する', async () => {
        const get = vi.spyOn(APIClient, 'get').mockResolvedValue({
            type: 'success',
            status: 200,
            headers: {},
            data: KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
        });

        await expect(Videos.fetchKonomiTVBS4KPlaybackCapabilities())
            .resolves.toEqual(KONOMITV_BS4K_PLAYBACK_CAPABILITIES);
        expect(get).toHaveBeenCalledOnce();
        expect(get).toHaveBeenCalledWith('/streams/video/konomitv-bs4k-playback-capabilities');
    });

    it('再生開始は保存codecのdepth候補と実encoderだけをtargeted能力APIへ要求する', async () => {
        const get = vi.spyOn(APIClient, 'get').mockResolvedValue({
            type: 'success',
            status: 200,
            headers: {},
            data: KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
        });

        await expect(Videos.fetchKonomiTVBS4KTargetedPlaybackCapabilities(
            'QSV',
            'av1',
            'opus',
            {is_bs4k: false, streaming_quality: '1080p'},
            true,
        )).resolves.toEqual(KONOMITV_BS4K_PLAYBACK_CAPABILITIES);
        expect(get).toHaveBeenCalledOnce();
        expect(get).toHaveBeenCalledWith(
            '/streams/video/konomitv-bs4k-playback-capabilities/targeted',
            {
                params: {
                    encoder: 'QSV',
                    video_codec: 'av1',
                    video_bit_depths: '10,8',
                    audio_codec: 'opus',
                    has_video: true,
                },
            },
        );
    });

    it('targeted能力API障害時はAVC/AACだけを互換fallbackにし、高度codecをtrueにしない', async () => {
        vi.spyOn(APIClient, 'get').mockResolvedValue({
            type: 'error',
            status: 503,
            headers: {},
            data: {detail: 'temporary failure'},
            error: new AxiosError('temporary failure'),
        });
        vi.stubGlobal('MediaSource', {isTypeSupported: () => true});

        const capabilities = await Videos.fetchKonomiTVBS4KTargetedPlaybackCapabilities(
            'NVENC',
            'av1',
            'opus',
            {is_bs4k: false, streaming_quality: '1080p'},
            true,
        );
        const resolved = Videos.resolveKonomiTVBS4KPlaybackCombination(
            capabilities,
            'NVENC',
            'av1',
            'opus',
            {is_bs4k: false, streaming_quality: '1080p'},
        );

        expect(capabilities.video).toEqual([expect.objectContaining({
            encoder: 'NVENC',
            codec: 'avc',
            bit_depth: 8,
            live_available: true,
            recorded_available: true,
        })]);
        expect(capabilities.audio).toEqual([expect.objectContaining({
            codec: 'aac',
            live_available: true,
            recorded_available: true,
        })]);
        expect(capabilities.live_combinations).toEqual([expect.objectContaining({
            encoder: 'NVENC',
            video_codec: 'avc',
            video_bit_depth: 8,
            audio_codec: 'aac',
            available: true,
        })]);
        expect(capabilities.video.some((item) =>
            item.codec === 'av1' && item.live_available === true
        )).toBe(false);
        expect(resolved).toMatchObject({
            video: {codec: 'avc', bit_depth: 8},
            audio: {codec: 'aac'},
            live: {video_codec: 'avc', audio_codec: 'aac'},
        });
    });

    it('音声のみのtargeted能力API障害時は映像行を合成せずAAC再生を維持する', async () => {
        vi.spyOn(APIClient, 'get').mockResolvedValue({
            type: 'error',
            status: 503,
            headers: {},
            data: {detail: 'temporary failure'},
            error: new AxiosError('temporary failure'),
        });

        const capabilities = await Videos.fetchKonomiTVBS4KTargetedPlaybackCapabilities(
            'QSV',
            'av1',
            'opus',
            {is_bs4k: false, streaming_quality: '1080p'},
            false,
        );

        expect(capabilities.video).toEqual([]);
        expect(capabilities.live_combinations).toEqual([]);
        expect(capabilities.audio).toEqual([expect.objectContaining({
            codec: 'aac',
            live_available: true,
            recorded_available: true,
        })]);
    });

    it('live/recorded両方とMSEを満たさない保存値は、保存値を変えずに互換codecへ解決する', () => {
        const is_type_supported = vi.fn(() => true);
        vi.stubGlobal('MediaSource', {isTypeSupported: is_type_supported});
        const settings_store = useSettingsStore();
        settings_store.settings.konomitv_bs4k_playback_video_codec = 'vp9';
        settings_store.settings.konomitv_bs4k_playback_audio_codec = 'opus';

        const capabilities = structuredClone(KONOMITV_BS4K_PLAYBACK_CAPABILITIES);
        capabilities.video[1].recorded_available = false;
        const video = Videos.resolveKonomiTVBS4KPlaybackVideoCapability(
            capabilities,
            'FFmpeg',
            'vp9',
            {is_bs4k: false, streaming_quality: '1080p'},
        );
        const audio = Videos.resolveKonomiTVBS4KPlaybackAudioCapability(capabilities, 'opus');

        expect(video).toMatchObject({codec: 'avc', bit_depth: 8});
        expect(audio).toMatchObject({codec: 'aac'});
        expect(settings_store.settings.konomitv_bs4k_playback_video_codec).toBe('vp9');
        expect(settings_store.settings.konomitv_bs4k_playback_audio_codec).toBe('opus');
    });

    it('片軸変更では選択軸を固定し、反対軸を同じ利用可能な組み合わせへ収束する', () => {
        vi.stubGlobal('MediaSource', {isTypeSupported: () => true});
        const capabilities = structuredClone(KONOMITV_BS4K_PLAYBACK_CAPABILITIES);
        capabilities.audio[1].recorded_available = true;
        capabilities.audio[1].recorded_reason_code = null;
        const vp9_opus_combination = capabilities.live_combinations.find(
            (combination) => combination.video_codec === 'vp9' && combination.audio_codec === 'opus',
        );
        if (vp9_opus_combination === undefined) throw new Error('VP9 + Opus fixture is unavailable.');
        vp9_opus_combination.available = false;
        vp9_opus_combination.reason_code = 'UnsupportedCombination';

        const resolved = Videos.resolveKonomiTVBS4KPlaybackCombination(
            capabilities,
            'FFmpeg',
            'vp9',
            'opus',
            {is_bs4k: false, streaming_quality: '1080p'},
        );
        const video_options_with_opus = Videos.buildKonomiTVBS4KPlaybackVideoCodecOptions(
            'FFmpeg',
            capabilities,
            {is_bs4k: false, streaming_quality: '1080p'},
            'opus',
        );
        const audio_options_with_vp9 = Videos.buildKonomiTVBS4KPlaybackAudioCodecOptions(
            capabilities,
            'FFmpeg',
            'vp9',
            {is_bs4k: false, streaming_quality: '1080p'},
        );
        const video_change = Videos.resolveKonomiTVBS4KPlaybackCombinationForVideoCodecChange(
            capabilities,
            'FFmpeg',
            'vp9',
            'opus',
            {is_bs4k: false, streaming_quality: '1080p'},
        );
        const audio_change = Videos.resolveKonomiTVBS4KPlaybackCombinationForAudioCodecChange(
            capabilities,
            'FFmpeg',
            'vp9',
            'opus',
            {is_bs4k: false, streaming_quality: '1080p'},
        );

        expect(resolved).toMatchObject({
            video: {codec: 'vp9', bit_depth: 10},
            audio: {codec: 'aac'},
            live: {video_codec: 'vp9', audio_codec: 'aac', available: true},
        });
        expect(video_change).toMatchObject({
            video: {codec: 'vp9', bit_depth: 10},
            audio: {codec: 'aac'},
            live: {video_codec: 'vp9', audio_codec: 'aac', available: true},
        });
        expect(audio_change).toMatchObject({
            video: {codec: 'avc', bit_depth: 8},
            audio: {codec: 'opus'},
            live: {video_codec: 'avc', audio_codec: 'opus', available: true},
        });
        expect(video_options_with_opus.find((option) => option.value === 'vp9')).toMatchObject({
            reason: null,
            props: {disabled: false},
        });
        expect(audio_options_with_vp9.find((option) => option.value === 'opus')).toMatchObject({
            reason: null,
            props: {disabled: false},
        });
    });

    it('設定画面の映像・音声片軸変更は完成済みtupleだけをStore購読へ通知する', () => {
        vi.stubGlobal('MediaSource', {isTypeSupported: () => true});
        const capabilities = structuredClone(KONOMITV_BS4K_PLAYBACK_CAPABILITIES);
        capabilities.audio[1].recorded_available = true;
        capabilities.audio[1].recorded_reason_code = null;
        const vp9_opus_combination = capabilities.live_combinations.find(
            (combination) => combination.video_codec === 'vp9' && combination.audio_codec === 'opus',
        );
        if (vp9_opus_combination === undefined) throw new Error('VP9 + Opus fixture is unavailable.');
        vp9_opus_combination.available = false;
        vp9_opus_combination.reason_code = 'UnsupportedCombination';

        const settings_store = useSettingsStore();
        settings_store.settings.konomitv_bs4k_playback_video_codec = 'avc';
        settings_store.settings.konomitv_bs4k_playback_audio_codec = 'opus';
        const observed_codec_pairs: Array<{video: string; audio: string}> = [];
        settings_store.$subscribe((_mutation, state) => {
            observed_codec_pairs.push({
                video: state.settings.konomitv_bs4k_playback_video_codec,
                audio: state.settings.konomitv_bs4k_playback_audio_codec,
            });
        }, {flush: 'sync'});

        // 映像だけVP9へ変更すると、VP9を固定して成立するAACを同じmutationへ含める。
        const video_change = Videos.resolveKonomiTVBS4KPlaybackCombinationForVideoCodecChange(
            capabilities,
            'FFmpeg',
            'vp9',
            settings_store.settings.konomitv_bs4k_playback_audio_codec,
            {is_bs4k: false, streaming_quality: '1080p'},
        );
        if (video_change === null) throw new Error('VP9 + AAC fixture is unavailable.');
        settings_store.updateKonomiTVBS4KPlaybackCodecPair(
            false,
            false,
            video_change.video.codec,
            video_change.audio.codec,
        );

        // 続けて音声だけOpusへ変更すると、Opusを固定して成立するAVCへ同時に切り替える。
        const audio_change = Videos.resolveKonomiTVBS4KPlaybackCombinationForAudioCodecChange(
            capabilities,
            'FFmpeg',
            settings_store.settings.konomitv_bs4k_playback_video_codec,
            'opus',
            {is_bs4k: false, streaming_quality: '1080p'},
        );
        if (audio_change === null) throw new Error('AVC + Opus fixture is unavailable.');
        settings_store.updateKonomiTVBS4KPlaybackCodecPair(
            false,
            false,
            audio_change.video.codec,
            audio_change.audio.codec,
        );

        expect(observed_codec_pairs).toEqual([
            {video: 'vp9', audio: 'aac'},
            {video: 'avc', audio: 'opus'},
        ]);
    });

    it('DPlayerの映像codec変更は必要な音声fallbackを同じmutationで保存する', () => {
        vi.stubGlobal('MediaSource', {isTypeSupported: () => true});
        vi.spyOn(Utils, 'isTouchDevice').mockReturnValue(true);
        const capabilities = structuredClone(KONOMITV_BS4K_PLAYBACK_CAPABILITIES);
        capabilities.audio[1].recorded_available = true;
        capabilities.audio[1].recorded_reason_code = null;
        const vp9_opus_combination = capabilities.live_combinations.find(
            (combination) => combination.video_codec === 'vp9' && combination.audio_codec === 'opus',
        );
        if (vp9_opus_combination === undefined) throw new Error('VP9 + Opus fixture is unavailable.');
        vp9_opus_combination.available = false;
        vp9_opus_combination.reason_code = 'UnsupportedCombination';

        const settings_store = useSettingsStore();
        settings_store.settings.konomitv_bs4k_playback_video_codec = 'avc';
        settings_store.settings.konomitv_bs4k_playback_audio_codec = 'opus';
        const observed_codec_pairs: Array<{video: string; audio: string}> = [];
        settings_store.$subscribe((_mutation, state) => {
            observed_codec_pairs.push({
                video: state.settings.konomitv_bs4k_playback_video_codec,
                audio: state.settings.konomitv_bs4k_playback_audio_codec,
            });
        }, {flush: 'sync'});
        const restart_required = vi.fn();
        usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);
        const {container, hide} = createKonomiTVBS4KSettingPanelController(
            capabilities,
            'avc',
            'opus',
        );
        const vp9_item = container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-video-codec-item[data-konomitv-bs4k-codec="vp9"]',
        );
        if (vp9_item === null) throw new Error('VP9 setting item is unavailable.');
        expect(vp9_item.getAttribute('aria-disabled')).toBe('false');

        vp9_item.click();

        expect(observed_codec_pairs).toEqual([{video: 'vp9', audio: 'aac'}]);
        expect(settings_store.settings.konomitv_bs4k_playback_video_codec).toBe('vp9');
        expect(settings_store.settings.konomitv_bs4k_playback_audio_codec).toBe('aac');
        expect(hide).toHaveBeenCalledOnce();
        expect(restart_required).toHaveBeenCalledWith(expect.objectContaining({
            message: '映像コーデックを VP9 に変更しました。',
        }));
    });

    it('DPlayerの音声codec変更は必要な映像fallbackを同じmutationで保存する', () => {
        vi.stubGlobal('MediaSource', {isTypeSupported: () => true});
        vi.spyOn(Utils, 'isTouchDevice').mockReturnValue(true);
        const capabilities = structuredClone(KONOMITV_BS4K_PLAYBACK_CAPABILITIES);
        capabilities.audio[1].recorded_available = true;
        capabilities.audio[1].recorded_reason_code = null;
        const vp9_opus_combination = capabilities.live_combinations.find(
            (combination) => combination.video_codec === 'vp9' && combination.audio_codec === 'opus',
        );
        if (vp9_opus_combination === undefined) throw new Error('VP9 + Opus fixture is unavailable.');
        vp9_opus_combination.available = false;
        vp9_opus_combination.reason_code = 'UnsupportedCombination';

        const settings_store = useSettingsStore();
        settings_store.settings.konomitv_bs4k_playback_video_codec = 'vp9';
        settings_store.settings.konomitv_bs4k_playback_audio_codec = 'aac';
        const observed_codec_pairs: Array<{video: string; audio: string}> = [];
        settings_store.$subscribe((_mutation, state) => {
            observed_codec_pairs.push({
                video: state.settings.konomitv_bs4k_playback_video_codec,
                audio: state.settings.konomitv_bs4k_playback_audio_codec,
            });
        }, {flush: 'sync'});
        const restart_required = vi.fn();
        usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);
        const {container, hide} = createKonomiTVBS4KSettingPanelController(
            capabilities,
            'vp9',
            'aac',
        );
        const opus_item = container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-audio-codec-item[data-konomitv-bs4k-codec="opus"]',
        );
        if (opus_item === null) throw new Error('Opus setting item is unavailable.');
        expect(opus_item.getAttribute('aria-disabled')).toBe('false');

        opus_item.click();

        expect(observed_codec_pairs).toEqual([{video: 'avc', audio: 'opus'}]);
        expect(settings_store.settings.konomitv_bs4k_playback_video_codec).toBe('avc');
        expect(settings_store.settings.konomitv_bs4k_playback_audio_codec).toBe('opus');
        expect(hide).toHaveBeenCalledOnce();
        expect(restart_required).toHaveBeenCalledWith(expect.objectContaining({
            message: '音声コーデックを Opus に変更しました。',
        }));
    });

    it('QSV映像能力がなくてもライブ・ラジオを音声のみとしてAACで初期化できる', () => {
        vi.stubGlobal('MediaSource', {isTypeSupported: () => true});
        const channels_store = useChannelsStore();
        const radio_channel = {
            ...channels_store.channel.current,
            display_channel_id: 'gr999',
            is_radiochannel: true,
        };
        vi.spyOn(channels_store, 'channel', 'get').mockReturnValue({
            previous: radio_channel,
            current: radio_channel,
            next: radio_channel,
        });

        const controller = createKonomiTVBS4KCompatibilityFallbackController(
            'Live',
        ) as KonomiTVBS4KPlaybackCodecResolverController;
        controller.konomitv_bs4k_current_playback_has_video =
            controller.doesKonomiTVBS4KCurrentPlaybackHaveVideo();
        expect(controller.konomitv_bs4k_current_playback_has_video).toBe(false);

        const capabilities_without_qsv_video: IKonomiTVBS4KPlaybackCapabilities = {
            video: [],
            audio: [{
                codec: 'aac',
                live_available: true,
                recorded_available: true,
                live_reason_code: null,
                recorded_reason_code: null,
            }],
            live_combinations: [],
        };
        expect(controller.resolveKonomiTVBS4KPlaybackCodecsForCurrentMedia(
            capabilities_without_qsv_video,
            'QSV',
            'vp9',
            'aac',
            {is_bs4k: false, streaming_quality: '1080p'},
        )).toEqual({
            video_codec: 'avc',
            video_bit_depth: 8,
            audio_codec: 'aac',
        });
    });

    it.each(['Live', 'Video'] as const)(
        '%sが通常/BS4KとWi-Fi/モバイルで同じ共通Store値を参照する',
        (playback_mode) => {
            const settings = useSettingsStore().settings;
            settings.konomitv_bs4k_playback_streaming_quality = '720p';
            settings.konomitv_bs4k_playback_streaming_quality_cellular = '480p';
            settings.konomitv_bs4k_playback_streaming_quality_for_bs4k = '2160p';
            settings.konomitv_bs4k_playback_streaming_quality_for_bs4k_cellular = '540p-30fps';
            settings.konomitv_bs4k_playback_video_codec = 'vp9';
            settings.konomitv_bs4k_playback_video_codec_cellular = 'av1';
            settings.konomitv_bs4k_playback_video_codec_for_bs4k = 'av1';
            settings.konomitv_bs4k_playback_video_codec_for_bs4k_cellular = 'vp9';
            settings.konomitv_bs4k_playback_audio_codec = 'opus';
            settings.konomitv_bs4k_playback_audio_codec_cellular = 'aac';
            settings.konomitv_bs4k_playback_audio_codec_for_bs4k = 'aac';
            settings.konomitv_bs4k_playback_audio_codec_for_bs4k_cellular = 'opus';
            settings.konomitv_bs4k_playback_24fps_mode = true;
            settings.konomitv_bs4k_playback_24fps_mode_cellular = false;

            expect(getKonomiTVBS4KQualityProfile(playback_mode, 'Wi-Fi')).toMatchObject({
                konomitv_bs4k_playback_streaming_quality: '720p',
                konomitv_bs4k_playback_streaming_quality_for_bs4k: '2160p',
                konomitv_bs4k_playback_video_codec: 'vp9',
                konomitv_bs4k_playback_video_codec_for_bs4k: 'av1',
                konomitv_bs4k_playback_audio_codec: 'opus',
                konomitv_bs4k_playback_audio_codec_for_bs4k: 'aac',
                konomitv_bs4k_playback_24fps_mode: true,
            });
            expect(getKonomiTVBS4KQualityProfile(playback_mode, 'Cellular')).toMatchObject({
                konomitv_bs4k_playback_streaming_quality: '480p',
                konomitv_bs4k_playback_streaming_quality_for_bs4k: '540p-30fps',
                konomitv_bs4k_playback_video_codec: 'av1',
                konomitv_bs4k_playback_video_codec_for_bs4k: 'vp9',
                konomitv_bs4k_playback_audio_codec: 'aac',
                konomitv_bs4k_playback_audio_codec_for_bs4k: 'opus',
                konomitv_bs4k_playback_24fps_mode: false,
            });
        },
    );

    it('保存2160pよりレジューム8Kを優先し、初期resolverへVP9 Level 6.1 profileを渡す', () => {
        const settings_store = useSettingsStore();
        settings_store.settings.konomitv_bs4k_playback_streaming_quality_for_bs4k = '2160p';
        const controller = Object.create(
            PlayerController.prototype,
        ) as KonomiTVBS4KQualityProfileController;
        controller.playback_mode = 'Live';
        controller.quality_profile_type = 'Wi-Fi';
        const queried_mime_types: string[] = [];
        vi.stubGlobal('MediaSource', {
            isTypeSupported: (mime_type: string) => {
                queried_mime_types.push(mime_type);
                return mime_type.includes('vp09.02.61.10') === false;
            },
        });

        const profile = controller.resolveKonomiTVBS4KEffectivePlaybackVideoProfile(true, '8K');
        const capability = Videos.resolveKonomiTVBS4KPlaybackVideoCapability(
            KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
            'FFmpeg',
            'vp9',
            profile,
        );

        expect(profile).toEqual({is_bs4k: true, streaming_quality: '4320p'});
        expect(capability).toMatchObject({codec: 'avc', bit_depth: 8});
        expect(queried_mime_types).toContain('video/mp4; codecs="vp09.02.61.10"');
        expect(queried_mime_types).not.toContain('video/mp4; codecs="vp09.02.51.10"');
    });

    it('保存4320pよりレジューム4Kを優先し、8K非対応でも4Kの初期codecを維持する', () => {
        const settings_store = useSettingsStore();
        settings_store.settings.konomitv_bs4k_playback_streaming_quality_for_bs4k = '4320p';
        const controller = Object.create(
            PlayerController.prototype,
        ) as KonomiTVBS4KQualityProfileController;
        controller.playback_mode = 'Video';
        controller.quality_profile_type = 'Wi-Fi';
        const queried_mime_types: string[] = [];
        vi.stubGlobal('MediaSource', {
            isTypeSupported: (mime_type: string) => {
                queried_mime_types.push(mime_type);
                return mime_type.includes('.60.') === false;
            },
        });

        const profile = controller.resolveKonomiTVBS4KEffectivePlaybackVideoProfile(true, '4K');
        const capability = Videos.resolveKonomiTVBS4KPlaybackVideoCapability(
            KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
            'FFmpeg',
            'vp9',
            profile,
        );

        expect(profile).toEqual({is_bs4k: true, streaming_quality: '2160p'});
        expect(capability).toMatchObject({codec: 'vp9', bit_depth: 10});
        expect(queried_mime_types).toContain('video/mp4; codecs="vp09.02.51.10"');
        expect(queried_mime_types).not.toContain('video/mp4; codecs="vp09.02.61.10"');
    });

    it.each([
        ['4320p', '8K'],
        ['2160p', '4K'],
        ['1440p', '1440p'],
        ['1080p-60fps', '1080p (60fps)'],
        ['1080p-30fps', '1080p (30fps)'],
    ] as const)(
        'API画質%sとDPlayer表示名%sを共通変換で往復する',
        (api_quality, display_name) => {
            expect(PlayerUtils.getKonomiTVBS4KPlaybackQualityDisplayName(api_quality)).toBe(display_name);
            expect(PlayerUtils.normalizeKonomiTVBS4KPlaybackAPIQuality(
                display_name,
                true,
                BS4K_LIVE_STREAMING_QUALITIES,
            )).toBe(api_quality);
        },
    );

    it.each([
        [false, false, 'konomitv_bs4k_playback_video_codec', 'konomitv_bs4k_playback_audio_codec'],
        [false, true, 'konomitv_bs4k_playback_video_codec_cellular', 'konomitv_bs4k_playback_audio_codec_cellular'],
        [true, false, 'konomitv_bs4k_playback_video_codec_for_bs4k', 'konomitv_bs4k_playback_audio_codec_for_bs4k'],
        [
            true,
            true,
            'konomitv_bs4k_playback_video_codec_for_bs4k_cellular',
            'konomitv_bs4k_playback_audio_codec_for_bs4k_cellular',
        ],
    ] as const)(
        'DPlayer設定キーをBS4K=%s・モバイル=%sへ正しく分離する',
        (is_bs4k, is_cellular, expected_video_key, expected_audio_key) => {
            expect(getKonomiTVBS4KPlaybackVideoCodecSettingKey(is_bs4k, is_cellular)).toBe(expected_video_key);
            expect(getKonomiTVBS4KPlaybackAudioCodecSettingKey(is_bs4k, is_cellular)).toBe(expected_audio_key);
        },
    );

    it('実initと同じresolverが選択中の通常/BS4K画質に対応するlevelをMSEへ問い合わせる', () => {
        const queried_mime_types: string[] = [];
        vi.stubGlobal('MediaSource', {
            isTypeSupported: (mime_type: string) => {
                queried_mime_types.push(mime_type);
                return true;
            },
        });

        const normal = Videos.resolveKonomiTVBS4KPlaybackVideoCapability(
            KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
            'FFmpeg',
            'vp9',
            {is_bs4k: false, streaming_quality: '1080p-60fps'},
        );
        const bs4k_4k = Videos.resolveKonomiTVBS4KPlaybackVideoCapability(
            KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
            'FFmpeg',
            'vp9',
            {is_bs4k: true, streaming_quality: '2160p'},
        );
        const bs4k_8k = Videos.resolveKonomiTVBS4KPlaybackVideoCapability(
            KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
            'FFmpeg',
            'vp9',
            {is_bs4k: true, streaming_quality: '4320p'},
        );

        expect(normal).toMatchObject({codec: 'vp9', bit_depth: 10});
        expect(bs4k_4k).toMatchObject({codec: 'vp9', bit_depth: 10});
        expect(bs4k_8k).toMatchObject({codec: 'vp9', bit_depth: 10});
        expect(queried_mime_types).toContain('video/mp4; codecs="vp09.02.41.10"');
        expect(queried_mime_types).toContain('video/mp4; codecs="vp09.02.51.10"');
        expect(queried_mime_types).toContain('video/mp4; codecs="vp09.02.61.10"');
        expect(PlayerUtils.getKonomiTVBS4KPlaybackVideoMIMEType(
            'avc',
            8,
            {is_bs4k: false, streaming_quality: '1080p-60fps'},
        )).toBe('video/mp4; codecs="avc1.64002A"');
        expect(PlayerUtils.getKonomiTVBS4KPlaybackVideoMIMEType(
            'avc',
            8,
            {is_bs4k: false, streaming_quality: '1080p'},
        )).toBe('video/mp4; codecs="avc1.640028"');
        expect(PlayerUtils.getKonomiTVBS4KPlaybackVideoMIMEType(
            'av1',
            10,
            {is_bs4k: false, streaming_quality: '1080p'},
        ))
            .toBe('video/mp4; codecs="av01.0.08M.10"');
        expect(PlayerUtils.getKonomiTVBS4KPlaybackVideoMIMEType(
            'av1',
            10,
            {is_bs4k: true, streaming_quality: '2160p'},
        ))
            .toBe('video/mp4; codecs="av01.0.13M.10"');
    });

    it.each([
        ['avc', 8, 'video/mp4; codecs="avc1.64003D"'],
        ['hevc', 8, 'video/mp4; codecs="hvc1.1.6.L183.B0"'],
        ['hevc', 10, 'video/mp4; codecs="hvc1.2.4.L183.B0"'],
        ['vp9', 8, 'video/mp4; codecs="vp09.00.61.08"'],
        ['vp9', 10, 'video/mp4; codecs="vp09.02.61.10"'],
        ['av1', 8, 'video/mp4; codecs="av01.0.17M.08"'],
        ['av1', 10, 'video/mp4; codecs="av01.0.17M.10"'],
    ] as const)(
        'BS4K 8K60の%s %dbitはcodec別の最小Level MIMEを使う',
        (codec, bit_depth, expected_mime_type) => {
            expect(PlayerUtils.getKonomiTVBS4KPlaybackVideoMIMEType(
                codec,
                bit_depth,
                {is_bs4k: true, streaming_quality: '4320p'},
            )).toBe(expected_mime_type);
        },
    );

    it.each([
        [true, '2160p', 'avc', 8, 'video/mp4; codecs="avc1.640034"'],
        [true, '2160p', 'hevc', 10, 'video/mp4; codecs="hvc1.2.4.L153.B0"'],
        [true, '2160p', 'vp9', 10, 'video/mp4; codecs="vp09.02.51.10"'],
        [true, '2160p', 'av1', 10, 'video/mp4; codecs="av01.0.13M.10"'],
        [true, '1440p', 'avc', 8, 'video/mp4; codecs="avc1.640033"'],
        [true, '1440p', 'hevc', 10, 'video/mp4; codecs="hvc1.2.4.L150.B0"'],
        [true, '1440p', 'vp9', 10, 'video/mp4; codecs="vp09.02.50.10"'],
        [true, '1440p', 'av1', 10, 'video/mp4; codecs="av01.0.12M.10"'],
        [false, '1080p-60fps', 'avc', 8, 'video/mp4; codecs="avc1.64002A"'],
        [false, '1080p-60fps', 'hevc', 10, 'video/mp4; codecs="hvc1.2.4.L126.B0"'],
        [false, '1080p-60fps', 'vp9', 10, 'video/mp4; codecs="vp09.02.41.10"'],
        [false, '1080p-60fps', 'av1', 10, 'video/mp4; codecs="av01.0.09M.10"'],
        [true, '1080p-30fps', 'avc', 8, 'video/mp4; codecs="avc1.640028"'],
        [true, '1080p-30fps', 'hevc', 10, 'video/mp4; codecs="hvc1.2.4.L120.B0"'],
        [true, '1080p-30fps', 'vp9', 10, 'video/mp4; codecs="vp09.02.41.10"'],
        [true, '1080p-30fps', 'av1', 10, 'video/mp4; codecs="av01.0.08M.10"'],
        [true, '810p-60fps', 'hevc', 10, 'video/mp4; codecs="hvc1.2.4.L126.B0"'],
        [true, '720p-30fps', 'avc', 8, 'video/mp4; codecs="avc1.64001F"'],
        [true, '720p-30fps', 'hevc', 10, 'video/mp4; codecs="hvc1.2.4.L93.B0"'],
        [true, '720p-30fps', 'vp9', 10, 'video/mp4; codecs="vp09.02.40.10"'],
        [true, '720p-30fps', 'av1', 10, 'video/mp4; codecs="av01.0.05M.10"'],
        [true, '240p-30fps', 'avc', 8, 'video/mp4; codecs="avc1.640015"'],
        [true, '240p-30fps', 'hevc', 10, 'video/mp4; codecs="hvc1.2.4.L60.B0"'],
        [true, '240p-30fps', 'vp9', 10, 'video/mp4; codecs="vp09.02.21.10"'],
        [true, '240p-30fps', 'av1', 10, 'video/mp4; codecs="av01.0.00M.10"'],
    ] as const)(
        'BS4K=%s・画質%sの%s %dbitは実出力上限の最小Levelを使う',
        (is_bs4k, streaming_quality, codec, bit_depth, expected_mime_type) => {
            expect(PlayerUtils.getKonomiTVBS4KPlaybackVideoMIMEType(
                codec,
                bit_depth,
                {is_bs4k, streaming_quality},
            )).toBe(expected_mime_type);
        },
    );

    it.each([
        ['1080p-30fps', '41'],
        ['810p-30fps', '40'],
        ['720p-30fps', '40'],
        ['540p-30fps', '31'],
        ['480p-30fps', '31'],
        ['360p-30fps', '30'],
        ['240p-30fps', '21'],
    ] as const)(
        'BS4K %sのVP9はmpegts.jsと同じ解像度×60fps保守値Level %sを使う',
        (streaming_quality, expected_level) => {
            expect(PlayerUtils.getKonomiTVBS4KPlaybackVideoMIMEType(
                'vp9',
                10,
                {is_bs4k: true, streaming_quality},
            )).toBe(`video/mp4; codecs="vp09.02.${expected_level}.10"`);
        },
    );

    it('BS4K 2160p60 AVC Level 5.2非対応をresolverと設定UI候補へ反映する', () => {
        vi.stubGlobal('MediaSource', {
            isTypeSupported: (mime_type: string) => mime_type.includes('avc1.640034') === false,
        });

        const capability = Videos.resolveKonomiTVBS4KPlaybackVideoCapability(
            KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
            'FFmpeg',
            'avc',
            {is_bs4k: true, streaming_quality: '2160p'},
        );
        const options = Videos.buildKonomiTVBS4KPlaybackVideoCodecOptions(
            'FFmpeg',
            KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
            {is_bs4k: true, streaming_quality: '2160p'},
        );

        expect(capability).toMatchObject({codec: 'vp9', bit_depth: 10});
        expect(options.find((option) => option.value === 'avc')).toMatchObject({
            reason: 'このブラウザの MSE が対応していません',
            props: {disabled: true},
        });
    });

    it('8K非対応でも4K以下のresolverと設定UI候補を誤って無効化しない', () => {
        vi.stubGlobal('MediaSource', {
            isTypeSupported: (mime_type: string) => mime_type.includes('vp09.02.61.10') === false,
        });

        const bs4k_8k = Videos.resolveKonomiTVBS4KPlaybackVideoCapability(
            KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
            'FFmpeg',
            'vp9',
            {is_bs4k: true, streaming_quality: '4320p'},
        );
        const bs4k_4k = Videos.resolveKonomiTVBS4KPlaybackVideoCapability(
            KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
            'FFmpeg',
            'vp9',
            {is_bs4k: true, streaming_quality: '2160p'},
        );
        const options_8k = Videos.buildKonomiTVBS4KPlaybackVideoCodecOptions(
            'FFmpeg',
            KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
            {is_bs4k: true, streaming_quality: '4320p'},
        );
        const options_4k = Videos.buildKonomiTVBS4KPlaybackVideoCodecOptions(
            'FFmpeg',
            KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
            {is_bs4k: true, streaming_quality: '2160p'},
        );

        expect(bs4k_8k).toMatchObject({codec: 'avc', bit_depth: 8});
        expect(bs4k_4k).toMatchObject({codec: 'vp9', bit_depth: 10});
        expect(options_8k.find((option) => option.value === 'vp9')?.props.disabled).toBe(true);
        expect(options_4k.find((option) => option.value === 'vp9')?.props.disabled).toBe(false);
    });

    it('通常1080p60のLevel 4.2非対応を通常1080pの設定UI候補へ波及させない', () => {
        vi.stubGlobal('MediaSource', {
            isTypeSupported: (mime_type: string) => mime_type.includes('avc1.64002A') === false,
        });

        const normal_1080p60 = Videos.resolveKonomiTVBS4KPlaybackVideoCapability(
            KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
            'FFmpeg',
            'avc',
            {is_bs4k: false, streaming_quality: '1080p-60fps'},
        );
        const normal_1080p = Videos.resolveKonomiTVBS4KPlaybackVideoCapability(
            KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
            'FFmpeg',
            'avc',
            {is_bs4k: false, streaming_quality: '1080p'},
        );
        const options_1080p60 = Videos.buildKonomiTVBS4KPlaybackVideoCodecOptions(
            'FFmpeg',
            KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
            {is_bs4k: false, streaming_quality: '1080p-60fps'},
        );
        const options_1080p = Videos.buildKonomiTVBS4KPlaybackVideoCodecOptions(
            'FFmpeg',
            KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
            {is_bs4k: false, streaming_quality: '1080p'},
        );

        expect(normal_1080p60).toMatchObject({codec: 'vp9', bit_depth: 10});
        expect(normal_1080p).toMatchObject({codec: 'avc', bit_depth: 8});
        expect(options_1080p60.find((option) => option.value === 'avc')?.props.disabled).toBe(true);
        expect(options_1080p.find((option) => option.value === 'avc')?.props.disabled).toBe(false);
    });

    it('MediaSourceとManagedMediaSourceが共存すると映像・音声とも両方の対応を要求する', () => {
        const media_source_is_type_supported = vi.fn(() => true);
        const managed_media_source_is_type_supported = vi.fn(() => true);
        vi.stubGlobal('MediaSource', {isTypeSupported: media_source_is_type_supported});
        vi.stubGlobal('ManagedMediaSource', {isTypeSupported: managed_media_source_is_type_supported});

        expect(PlayerUtils.isKonomiTVBS4KPlaybackVideoCodecSupported(
            'avc',
            8,
            {is_bs4k: false, streaming_quality: '1080p'},
        )).toBe(true);
        expect(PlayerUtils.isKonomiTVBS4KPlaybackAudioCodecSupported('aac')).toBe(true);
        expect(media_source_is_type_supported).toHaveBeenCalledTimes(2);
        expect(managed_media_source_is_type_supported).toHaveBeenCalledTimes(2);
    });

    it('共存するMediaSource実装のどちらかが非対応なら共通profileで無効化する', () => {
        vi.stubGlobal('MediaSource', {isTypeSupported: () => true});
        vi.stubGlobal('ManagedMediaSource', {isTypeSupported: () => false});

        expect(PlayerUtils.isKonomiTVBS4KPlaybackVideoCodecSupported(
            'avc',
            8,
            {is_bs4k: false, streaming_quality: '1080p'},
        )).toBe(false);
        expect(PlayerUtils.isKonomiTVBS4KPlaybackAudioCodecSupported('aac')).toBe(false);
    });

    it('MediaSourceが存在しない場合だけManagedMediaSourceへフォールバックする', () => {
        const managed_media_source_is_type_supported = vi.fn(() => true);
        vi.stubGlobal('MediaSource', undefined);
        vi.stubGlobal('ManagedMediaSource', {isTypeSupported: managed_media_source_is_type_supported});

        expect(PlayerUtils.isKonomiTVBS4KPlaybackVideoCodecSupported(
            'avc',
            8,
            {is_bs4k: false, streaming_quality: '1080p'},
        )).toBe(true);
        expect(PlayerUtils.isKonomiTVBS4KPlaybackAudioCodecSupported('aac')).toBe(true);
        expect(managed_media_source_is_type_supported).toHaveBeenCalledTimes(2);
    });

    it.each([
        [
            'Version/17.5 Safari/605.1.15',
            'audio/mp4; codecs="Opus"',
        ],
        [
            'Mozilla/5.0 Chrome/126.0.0.0 Safari/537.36',
            'audio/mp4; codecs="opus"',
        ],
    ])('UA=%s のOpus MIME表記をブラウザ実装へ合わせる', (user_agent, expected_mime_type) => {
        const is_type_supported = vi.fn(() => true);
        vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(user_agent);
        vi.stubGlobal('MediaSource', {isTypeSupported: is_type_supported});

        expect(PlayerUtils.isKonomiTVBS4KPlaybackAudioCodecSupported('opus')).toBe(true);
        expect(is_type_supported).toHaveBeenCalledWith(expected_mime_type);
    });

    it('サーバー非対応とブラウザMSE非対応を日本語理由付きのdisabled項目にする', () => {
        vi.stubGlobal('MediaSource', {
            isTypeSupported: (mime_type: string) => mime_type.includes('opus') === false,
        });
        const capabilities = structuredClone(KONOMITV_BS4K_PLAYBACK_CAPABILITIES);
        const vp9_aac_combination = capabilities.live_combinations.find(
            (combination) => combination.video_codec === 'vp9' && combination.audio_codec === 'aac',
        );
        if (vp9_aac_combination === undefined) throw new Error('VP9 + AAC fixture is unavailable.');
        vp9_aac_combination.available = false;
        vp9_aac_combination.reason_code = 'FeatureDisabled';
        capabilities.audio[1].recorded_available = true;
        capabilities.audio[1].recorded_reason_code = null;

        const video_options = Videos.buildKonomiTVBS4KPlaybackVideoCodecOptions(
            'FFmpeg',
            capabilities,
            {is_bs4k: false, streaming_quality: '1080p'},
        );
        const audio_options = Videos.buildKonomiTVBS4KPlaybackAudioCodecOptions(capabilities);
        const vp9 = video_options.find((option) => option.value === 'vp9');
        const opus = audio_options.find((option) => option.value === 'opus');

        expect(vp9).toMatchObject({
            reason: '高度コーデック機能が無効です',
            props: {disabled: true},
        });
        expect(vp9?.title).toContain('高度コーデック機能が無効です');
        expect(vp9?.title).not.toContain('FeatureDisabled');
        expect(opus).toMatchObject({
            reason: 'このブラウザの MSE が対応していません',
            props: {disabled: true},
        });
    });

    it.each([
        {
            label: '通常画質',
            is_bs4k: false,
            selected_codec: 'avc' as const,
            current_quality: '1080p' as const,
            target_quality: '1080p-60fps' as const,
            unsupported_mime_fragment: 'avc1.64002A',
        },
        {
            label: 'BS4K画質',
            is_bs4k: true,
            selected_codec: 'vp9' as const,
            current_quality: '2160p' as const,
            target_quality: '4320p' as const,
            unsupported_mime_fragment: 'vp09.02.61.10',
        },
    ])(
        '$labelで画質→codec→別画質と変更してもMSE非対応の組み合わせを選択できない',
        ({
            is_bs4k,
            selected_codec,
            current_quality,
            target_quality,
            unsupported_mime_fragment,
        }) => {
            vi.stubGlobal('MediaSource', {
                isTypeSupported: (mime_type: string) =>
                    mime_type.includes(unsupported_mime_fragment) === false,
            });

            // 現在画質で codec を選択できることを先に確認し、その codec のまま別画質へ移る操作を再現する。
            const selected_codec_option = Videos.buildKonomiTVBS4KPlaybackVideoCodecOptions(
                'FFmpeg',
                KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
                {is_bs4k, streaming_quality: current_quality},
            ).find((konomitv_bs4k_option) => konomitv_bs4k_option.value === selected_codec);
            expect(selected_codec_option?.props.disabled).toBe(false);

            const quality_options = Videos.buildKonomiTVBS4KPlaybackQualityOptions(
                'FFmpeg',
                KONOMITV_BS4K_PLAYBACK_CAPABILITIES,
                selected_codec,
                is_bs4k,
                [
                    {title: '現在画質', value: current_quality},
                    {title: '変更先画質', value: target_quality},
                ],
            );
            expect(quality_options.find((option) => option.value === current_quality))
                .toMatchObject({props: {disabled: false}, reason: null});
            expect(quality_options.find((option) => option.value === target_quality))
                .toMatchObject({
                    props: {disabled: true},
                    reason: 'このブラウザの MSE が対応していません',
                });
        },
    );
});


describe('ライブ通常APIのcodec query', () => {
    it('mpegts/events/psi-archived-dataへ同じ正規順序のqueryを伝播する', () => {
        const codec_query = PlayerUtils.buildKonomiTVBS4KLivePlaybackCodecQuery({
            video_codec: 'vp9',
            video_bit_depth: 10,
            audio_codec: 'opus',
        });
        const mpegts_url = PlayerUtils.buildKonomiTVBS4KLiveAPIEndpointURL(
            'gr011',
            '1080p-60fps',
            'mpegts',
            codec_query,
        );
        const player = {
            quality: {url: mpegts_url},
        } as unknown as DPlayer;
        const event_url = PlayerUtils.buildKonomiTVBS4KLiveAPIEndpointURLFromDPlayer(player, 'gr011', 'events');
        const psi_url = PlayerUtils.buildKonomiTVBS4KLiveAPIEndpointURLFromDPlayer(player, 'gr011', 'psi-archived-data');

        expect(codec_query).toBe('video_codec=vp9&video_bit_depth=10&audio_codec=opus');
        expect(new URL(mpegts_url, location.origin).search.slice(1)).toBe(codec_query);
        expect(new URL(event_url, location.origin).search.slice(1)).toBe(codec_query);
        expect(new URL(psi_url, location.origin).search.slice(1)).toBe(codec_query);
        expect(event_url).toContain('/1080p-60fps/events?');
        expect(psi_url).toContain('/1080p-60fps/psi-archived-data?');
    });
});


describe('DPlayer画質切替のKonomiTV-BS4K能力guard', () => {
    beforeEach(() => {
        localStorage.clear();
        setActivePinia(createPinia());
        disableKonomiTVBS4KAutoQualityMode();
        vi.stubGlobal('ManagedMediaSource', undefined);
    });

    afterEach(() => {
        vi.restoreAllMocks();
        vi.unstubAllGlobals();
    });

    it('2160pからMSE対応4320pへの切替だけをDPlayerへ渡す', () => {
        vi.stubGlobal('MediaSource', {isTypeSupported: () => true});
        const {controller, player, originalSwitchQuality, notice} =
            createKonomiTVBS4KQualitySwitchGuardController();
        controller.setupKonomiTVBS4KPlaybackQualitySwitchGuard(1, player);

        player.switchQuality(0);

        expect(originalSwitchQuality).toHaveBeenCalledOnce();
        expect(originalSwitchQuality).toHaveBeenCalledWith(0);
        expect(notice).not.toHaveBeenCalled();
    });

    it('2160pからMSE非対応4320pへの切替はvideo差替え前に拒否する', () => {
        vi.stubGlobal('MediaSource', {
            isTypeSupported: (mime_type: string) => mime_type.includes('vp09.02.61.10') === false,
        });
        const {controller, player, originalSwitchQuality, notice} =
            createKonomiTVBS4KQualitySwitchGuardController();
        controller.setupKonomiTVBS4KPlaybackQualitySwitchGuard(1, player);

        player.switchQuality(0);

        expect(originalSwitchQuality).not.toHaveBeenCalled();
        expect(notice).toHaveBeenCalledOnce();
        expect(notice).toHaveBeenCalledWith(
            expect.stringContaining('8K は現在の映像コーデックでは再生できません'),
            4000,
            undefined,
            'rgb(var(--v-theme-error-readable))',
        );
        expect(controller.konomitv_bs4k_playback_video_profile_for_current_playback)
            .toEqual({is_bs4k: true, streaming_quality: '2160p'});
        expect(controller.konomitv_bs4k_compatibility_playback_fallback_attempted).toBe(false);
    });

    it('quality_startが渡す対応4320pでcurrent profileを更新する', () => {
        vi.stubGlobal('MediaSource', {isTypeSupported: () => true});
        const {controller, player, pause} = createKonomiTVBS4KQualitySwitchGuardController();

        expect(controller.handleKonomiTVBS4KPlaybackQualityStart(
            1,
            player,
            {name: '8K', url: '/streams/live/bs4k/4320p/mpegts'},
        )).toBe(true);

        expect(controller.konomitv_bs4k_playback_video_profile_for_current_playback)
            .toEqual({is_bs4k: true, streaming_quality: '4320p'});
        expect(pause).not.toHaveBeenCalled();
    });

    it('guardを迂回した非対応quality_startは直前画質への再起動を一度だけ要求する', () => {
        vi.stubGlobal('MediaSource', {
            isTypeSupported: (mime_type: string) => mime_type.includes('vp09.02.61.10') === false,
        });
        const {controller, player, pause} = createKonomiTVBS4KQualitySwitchGuardController();
        const restart_required = vi.fn();
        usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);
        const target_quality = {name: '8K', url: '/streams/live/bs4k/4320p/mpegts'};

        expect(controller.handleKonomiTVBS4KPlaybackQualityStart(1, player, target_quality)).toBe(false);
        expect(controller.handleKonomiTVBS4KPlaybackQualityStart(1, player, target_quality)).toBe(false);

        expect(pause).toHaveBeenCalledTimes(2);
        expect(restart_required).toHaveBeenCalledOnce();
        expect(restart_required).toHaveBeenCalledWith(expect.objectContaining({
            should_resume_quality: false,
            konomitv_bs4k_resume_quality: '4K',
        }));
        expect(controller.konomitv_bs4k_playback_video_profile_for_current_playback)
            .toEqual({is_bs4k: true, streaming_quality: '2160p'});
        expect(controller.konomitv_bs4k_compatibility_playback_fallback_attempted).toBe(false);
    });
});


describe.each(['Live', 'Video'] as const)('%s実再生フォールバック', (playback_mode) => {
    beforeEach(() => {
        localStorage.clear();
        setActivePinia(createPinia());
        disableKonomiTVBS4KAutoQualityMode();
    });

    it('保存設定と能力判定を変えずAVC/AACへ一度だけ再初期化を要求する', () => {
        const controller = createKonomiTVBS4KCompatibilityFallbackController(playback_mode);
        const settings_store = useSettingsStore();
        const snackbars_store = useSnackbarsStore();
        settings_store.settings.konomitv_bs4k_playback_video_codec = 'vp9';
        settings_store.settings.konomitv_bs4k_playback_audio_codec = 'opus';
        const restart_required = vi.fn();
        usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);

        expect(controller.requestKonomiTVBS4KCompatibilityPlaybackFallback('append failed')).toBe(true);
        expect(controller.isKonomiTVBS4KCompatibilityPlaybackFallbackPending()).toBe(true);
        expect(controller.requestKonomiTVBS4KCompatibilityPlaybackFallback('append failed again')).toBe(false);

        expect(controller.konomitv_bs4k_compatibility_playback_fallback_attempted).toBe(true);
        expect(controller.konomitv_bs4k_force_recorded_aac_audio_codec).toBe(true);
        expect(settings_store.settings.konomitv_bs4k_playback_video_codec).toBe('vp9');
        expect(settings_store.settings.konomitv_bs4k_playback_audio_codec).toBe('opus');
        expect(restart_required).toHaveBeenCalledTimes(1);
        expect(restart_required).toHaveBeenCalledWith(expect.objectContaining({
            message: expect.stringMatching(/理由: append failed.*能力判定結果や保存設定は変更せず.*実再生 PASS とは扱いません/),
            should_resume_quality: true,
        }));
        expect(snackbars_store.snackbars).toHaveLength(1);
        expect(snackbars_store.snackbars[0]).toMatchObject({
            level: 'warning',
            text: expect.stringMatching(/理由: append failed.*今回だけ AVC \/ AAC/),
        });

        controller.konomitv_bs4k_playback_video_codec_for_current_playback = 'avc';
        controller.konomitv_bs4k_playback_audio_codec_for_current_playback = 'aac';
        expect(controller.isKonomiTVBS4KCompatibilityPlaybackFallbackPending()).toBe(false);
    });
});

describe('自動モードの実再生フォールバック', () => {
    beforeEach(() => {
        localStorage.clear();
        setActivePinia(createPinia());
        vi.stubGlobal('MediaSource', {isTypeSupported: () => true});
    });

    afterEach(() => {
        vi.unstubAllGlobals();
    });

    it('ProbeFailedのAV1を飛ばして利用可能なHEVCへ切り替える', () => {
        const controller = createKonomiTVBS4KCompatibilityFallbackController(
            'Live',
        ) as KonomiTVBS4KAutoCompatibilityFallbackController;
        controller.quality_profile_type = 'Wi-Fi';
        controller.konomitv_bs4k_current_playback_has_video = true;
        controller.konomitv_bs4k_playback_encoder_for_current_playback = 'FFmpeg';
        controller.konomitv_bs4k_playback_video_profile_for_current_playback = {
            is_bs4k: false,
            streaming_quality: '1080p',
        };
        const capabilities: IKonomiTVBS4KPlaybackCapabilities = {
            video: [
                ...structuredClone(KONOMITV_BS4K_PLAYBACK_CAPABILITIES.video),
                {
                    encoder: 'FFmpeg',
                    codec: 'av1',
                    bit_depth: 10,
                    profile: 'libsvtav1',
                    live_available: false,
                    recorded_available: true,
                    live_reason_code: 'ProbeFailed',
                    recorded_reason_code: null,
                },
                {
                    encoder: 'FFmpeg',
                    codec: 'hevc',
                    bit_depth: 10,
                    profile: 'libx265',
                    live_available: true,
                    recorded_available: true,
                    live_reason_code: null,
                    recorded_reason_code: null,
                },
            ],
            audio: structuredClone(KONOMITV_BS4K_PLAYBACK_CAPABILITIES.audio),
            live_combinations: [
                ...structuredClone(KONOMITV_BS4K_PLAYBACK_CAPABILITIES.live_combinations),
                {
                    encoder: 'FFmpeg',
                    video_codec: 'av1',
                    video_bit_depth: 10,
                    audio_codec: 'aac',
                    available: false,
                    reason_code: 'ProbeFailed',
                },
                {
                    encoder: 'FFmpeg',
                    video_codec: 'hevc',
                    video_bit_depth: 10,
                    audio_codec: 'aac',
                    available: true,
                    reason_code: null,
                },
            ],
        };
        controller.konomitv_bs4k_playback_capabilities_for_current_playback = capabilities;
        controller.konomitv_bs4k_playback_capabilities_for_ui = capabilities;
        const restart_required = vi.fn();
        usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);

        expect(controller.requestKonomiTVBS4KCompatibilityPlaybackFallback('VP9 append failed')).toBe(true);

        expect(controller.konomitv_bs4k_auto_session_override).toMatchObject({
            video_codec: 'hevc',
            audio_codec: 'aac',
        });
        expect(controller.konomitv_bs4k_auto_failed_video_codecs).toEqual(new Set(['vp9']));
        expect(controller.konomitv_bs4k_compatibility_playback_fallback_attempted).toBe(false);
        expect(restart_required).toHaveBeenCalledOnce();
        expect(restart_required).toHaveBeenCalledWith(expect.objectContaining({
            message: expect.stringMatching(/VP9.*HEVC/),
            should_resume_quality: true,
        }));
    });
});

describe('ライブ音声トラック表示', () => {
    beforeEach(() => {
        localStorage.clear();
        setActivePinia(createPinia());
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('PMTの音声PIDが1本ならDPlayerの固定2項目を1項目へ減らす', () => {
        const container = document.createElement('div');
        container.innerHTML = `
            <div class="dplayer-setting-box">
                <div class="dplayer-setting-audio"><span class="dplayer-label-value"></span></div>
                <div class="dplayer-setting-audio-panel">
                    <div class="dplayer-setting-audio-item dplayer-setting-audio-current">
                        <span class="dplayer-label">Track1</span>
                    </div>
                    <div class="dplayer-setting-audio-item">
                        <span class="dplayer-label">Track2</span>
                    </div>
                </div>
            </div>
        `;
        const controller = Object.create(PlayerController.prototype) as LiveAudioTrackLabelController;
        controller.playback_mode = 'Live';
        controller.live_selected_audio_track_index = 0;
        controller.player = {container, plugins: {mpegts: {}}} as unknown as DPlayer;
        const console_warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);

        controller.applyAudioTrackLabels({
            hasAudio: true,
            audioTrackCount: 1,
            audioTracks: [{name: 'Track1'}],
        });

        expect(container.querySelectorAll('.dplayer-setting-audio-item')).toHaveLength(1);
        expect(container.querySelector('.dplayer-setting-audio-current .dplayer-label')?.textContent)
            .toContain('Track1');
        expect(console_warn).not.toHaveBeenCalledWith(expect.stringContaining('Audio track count mismatch'));
    });
});

describe('native canplay欠落時の再生準備判定', () => {
    const controller = Object.create(PlayerController.prototype) as MediaReadinessController;

    it('HAVE_FUTURE_DATAならcanplay通知がなくても開始可能と判定する', () => {
        const video = {
            readyState: 3,
            currentTime: 10,
            buffered: {length: 0},
        } as HTMLVideoElement;

        expect(controller.isMediaReadyToStartPlayback(video)).toBe(true);
    });

    it('readyStateが遅れてもcurrentTimeより先の実バッファがあれば開始可能と判定する', () => {
        const video = {
            readyState: 2,
            currentTime: 10,
            buffered: {
                length: 1,
                start: () => 9,
                end: () => 11,
            },
        } as unknown as HTMLVideoElement;

        expect(controller.isMediaReadyToStartPlayback(video)).toBe(true);
    });

    it('実バッファがcurrentTimeへ届いていない間は開始しない', () => {
        const video = {
            readyState: 2,
            currentTime: 10,
            buffered: {
                length: 1,
                start: () => 11,
                end: () => 12,
            },
        } as unknown as HTMLVideoElement;

        expect(controller.isMediaReadyToStartPlayback(video)).toBe(false);
    });
});

describe('高度codecのサーバー側失敗フォールバック', () => {
    beforeEach(() => {
        localStorage.clear();
        setActivePinia(createPinia());
        disableKonomiTVBS4KAutoQualityMode();
    });

    it('ライブのBridge停止はAVC/AACへ一度だけ切り替える', () => {
        const controller = createKonomiTVBS4KCompatibilityFallbackController(
            'Live',
        ) as KonomiTVBS4KServerPipelineFallbackController;
        const restart_required = vi.fn();
        usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);

        expect(controller.handleKonomiTVBS4KLivePlaybackPipelineStatus(
            'Restart',
            'TS Codec Bridge が停止したため、エンコードタスクを再起動しています… (ER-07B)',
        )).toBe(true);
        expect(controller.handleKonomiTVBS4KLivePlaybackPipelineStatus(
            'Offline',
            'BridgeUnavailable',
        )).toBe(true);
        expect(restart_required).toHaveBeenCalledOnce();
        expect(restart_required).toHaveBeenCalledWith(expect.objectContaining({
            message: expect.stringContaining('Live Restart: TS Codec Bridge'),
            should_resume_quality: true,
        }));
    });

    it.each([
        'チューナーの起動に失敗しました。空きチューナーが不足しています。(E-02E)',
        'チューナーからの放送波の受信がタイムアウトしました。(E-11)',
        'この時間は放送を休止しています。(E-04F)',
        'ストリーミング接続が切断されました。',
    ])('ライブの一般障害「%s」ではcodec fallbackしない', (detail) => {
        const controller = createKonomiTVBS4KCompatibilityFallbackController(
            'Live',
        ) as KonomiTVBS4KServerPipelineFallbackController;
        const restart_required = vi.fn();
        usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);

        expect(controller.handleKonomiTVBS4KLivePlaybackPipelineStatus(
            'Offline',
            detail,
        )).toBe(false);
        expect(controller.konomitv_bs4k_compatibility_playback_fallback_attempted).toBe(false);
        expect(restart_required).not.toHaveBeenCalled();
    });

    it('録画playlistの422とsegmentの5xxは最初の1回だけAVC/AACへ切り替える', () => {
        const controller = createKonomiTVBS4KCompatibilityFallbackController(
            'Video',
        ) as KonomiTVBS4KServerPipelineFallbackController;
        const restart_required = vi.fn();
        usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);

        expect(controller.handleKonomiTVBS4KRecordedHLSLoadError(
            createKonomiTVBS4KHLSHTTPError(
                ErrorDetails.MANIFEST_LOAD_ERROR,
                'https://example.test/api/streams/video/1/1080p/playlist',
                422,
            ),
        )).toBe(true);
        expect(controller.handleKonomiTVBS4KRecordedHLSLoadError(
            createKonomiTVBS4KHLSHTTPError(
                ErrorDetails.FRAG_LOAD_ERROR,
                'https://example.test/api/streams/video/1/1080p/segment',
                503,
            ),
        )).toBe(false);
        expect(restart_required).toHaveBeenCalledOnce();
        expect(restart_required).toHaveBeenCalledWith(expect.objectContaining({
            message: expect.stringContaining('hls.js manifestLoadError: HTTP 422'),
        }));
    });

    it.each([
        [0, ErrorDetails.FRAG_LOAD_ERROR],
        [404, ErrorDetails.MANIFEST_LOAD_ERROR],
        [401, ErrorDetails.LEVEL_LOAD_ERROR],
    ] as const)(
        '録画HLSのHTTP %sは一般回線・認証・不存在障害としてfallbackしない',
        (status_code, details) => {
            const controller = createKonomiTVBS4KCompatibilityFallbackController(
                'Video',
            ) as KonomiTVBS4KServerPipelineFallbackController;
            const restart_required = vi.fn();
            usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);

            expect(controller.handleKonomiTVBS4KRecordedHLSLoadError(
                createKonomiTVBS4KHLSHTTPError(
                    details,
                    'https://example.test/api/streams/video/1/1080p/segment',
                    status_code,
                ),
            )).toBe(false);
            expect(controller.konomitv_bs4k_compatibility_playback_fallback_attempted).toBe(false);
            expect(restart_required).not.toHaveBeenCalled();
        },
    );

    it('AVC/AAC再生中はBridge名を含む誤通知でもfallbackしない', () => {
        const controller = createKonomiTVBS4KCompatibilityFallbackController(
            'Live',
        ) as KonomiTVBS4KServerPipelineFallbackController;
        controller.konomitv_bs4k_playback_video_codec_for_current_playback = 'avc';
        controller.konomitv_bs4k_playback_audio_codec_for_current_playback = 'aac';

        expect(controller.handleKonomiTVBS4KLivePlaybackPipelineStatus(
            'Restart',
            'TS Codec Bridge (ER-07B)',
        )).toBe(false);
    });
});


describe.each(['Live', 'Video'] as const)('%s native error listener', (playback_mode) => {
    beforeEach(() => {
        localStorage.clear();
        setActivePinia(createPinia());
        disableKonomiTVBS4KAutoQualityMode();
        usePlayerStore().live_stream_status = 'ONAir';
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('複数回の画質切り替え相当の登録要求後も実errorを一度だけ処理する', async () => {
        vi.spyOn(Utils, 'sleep').mockResolvedValue();
        const {controller, player, on, getErrorHandler} =
            createKonomiTVBS4KNativeErrorController(playback_mode);
        const restart_required = vi.fn();
        usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);

        // 初期化直後と複数回の quality_start から同じ登録処理が呼ばれる状況を再現する。
        controller.setupKonomiTVBS4KNativePlaybackErrorHandler(1, player);
        controller.setupKonomiTVBS4KNativePlaybackErrorHandler(1, player);
        controller.setupKonomiTVBS4KNativePlaybackErrorHandler(1, player);
        expect(on).toHaveBeenCalledTimes(1);

        const error_handler = getErrorHandler();
        if (error_handler === null) throw new Error('native error handler was not registered.');
        await error_handler();

        expect(controller.konomitv_bs4k_compatibility_playback_fallback_attempted).toBe(true);
        expect(restart_required).toHaveBeenCalledTimes(1);
    });

});


describe('Live native error listenerの世代分離', () => {
    beforeEach(() => {
        localStorage.clear();
        setActivePinia(createPinia());
        disableKonomiTVBS4KAutoQualityMode();
        usePlayerStore().live_stream_status = 'ONAir';
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('await中に初期化世代が変わった旧listenerはfallbackもrestartも要求しない', async () => {
        const sleep = createDeferred();
        vi.spyOn(Utils, 'sleep').mockReturnValue(sleep.promise);
        const {controller, player, getErrorHandler} =
            createKonomiTVBS4KNativeErrorController('Live');
        const restart_required = vi.fn();
        usePlayerStore().event_emitter.on('PlayerRestartRequired', restart_required);
        controller.setupKonomiTVBS4KNativePlaybackErrorHandler(1, player);

        const error_handler = getErrorHandler();
        if (error_handler === null) throw new Error('native error handler was not registered.');
        const error_handling = error_handler();
        controller.lifecycle_generation = 2;
        sleep.resolve(undefined);
        await error_handling;

        expect(controller.konomitv_bs4k_compatibility_playback_fallback_attempted).toBe(false);
        expect(restart_required).not.toHaveBeenCalled();
    });
});
