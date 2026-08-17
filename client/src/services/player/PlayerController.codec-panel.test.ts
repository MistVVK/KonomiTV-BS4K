import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';

import { ILiveChannelDefault } from '@/services/Channels';
import PlayerController, {
    calculateLiveSyncTarget,
    generateRecordedPlaybackSessionID,
} from '@/services/player/PlayerController';
import { IProgramDefault } from '@/services/Programs';
import Videos from '@/services/Videos';
import useChannelsStore from '@/stores/ChannelsStore';
import usePlayerStore from '@/stores/PlayerStore';
import useSettingsStore, { LIVE_STREAMING_QUALITIES } from '@/stores/SettingsStore';
import useVersionStore from '@/stores/VersionStore';
import { PlayerUtils } from '@/utils';


type PlaybackMode = 'Live' | 'Video';

type FakePlayer = {
    container: HTMLElement;
    template: {
        settingBox: HTMLElement;
        settingOriginPanel: HTMLElement;
        audio: HTMLElement;
    };
    setting: {
        show: () => void;
        hide: () => void;
    };
    qualityIndex: number;
    options: {
        video: {
            quality: Array<{name: string; url: string}>;
        };
    };
    notice: ReturnType<typeof vi.fn>;
    plugins: {
        mpegts: {
            switchAudioTrack: ReturnType<typeof vi.fn>;
        };
    };
};

type TestablePlayerController = {
    playback_mode: PlaybackMode;
    quality_profile_type: 'Wi-Fi' | 'Cellular';
    player: FakePlayer;
    applyAudioTrackLabels: (media_info?: {[key: string]: any}) => void;
    buildLiveAudioTrackDisplayEntries: (media_info: {[key: string]: any}) => Array<{
        label: string;
        selectableTrackIndex: number | null;
    }>;
    setupSettingPanelHandler: () => void;
    initialization_generation: number;
    owner_signal: AbortSignal | null;
    destroyed: boolean;
    destroying: boolean;
    live_selected_audio_track_index: number;
};


it('非HTTPSでも利用可能な乱数から録画セッションIDを生成する', () => {
    const random_source: Pick<Crypto, 'getRandomValues'> = {
        getRandomValues: <T extends ArrayBufferView | null>(array: T): T => {
            (array as Uint8Array).set([0x00, 0x01, 0xab, 0xff]);
            return array;
        },
    };

    expect(generateRecordedPlaybackSessionID(random_source)).toBe('0001abff');
});


function createTimeRanges(ranges: Array<[number, number]>): TimeRanges {
    return {
        length: ranges.length,
        start: (index: number) => ranges[index][0],
        end: (index: number) => ranges[index][1],
    };
}


describe('ライブ末尾同期先の算出', () => {
    it('有限の擬似 duration ではなく最後の実バッファ範囲を基準にする', () => {
        const buffered = createTimeRanges([[0, 12], [20, 42]]);

        expect(calculateLiveSyncTarget(buffered, 3.9)).toBeCloseTo(38.1);
        expect(calculateLiveSyncTarget(createTimeRanges([[100, 102]]), 3.9)).toBe(100);
        expect(calculateLiveSyncTarget(createTimeRanges([]), 3.9)).toBeNull();
    });
});


function createFakePlayer(
    current_quality_name = '720p',
): { player: FakePlayer; original_hide: ReturnType<typeof vi.fn> } {
    const container = document.createElement('div');
    container.innerHTML = `
        <div class="dplayer-setting-box">
            <div class="dplayer-setting-origin-panel">
                <div class="dplayer-setting-item dplayer-setting-audio">
                    <span class="dplayer-label-value"></span>
                    <div class="dplayer-toggle"><svg data-icon="arrow"></svg></div>
                </div>
            </div>
            <div class="dplayer-setting-audio-panel">
                <div class="dplayer-setting-audio-header">
                    <div class="dplayer-toggle"><svg data-icon="back"></svg></div>
                </div>
                <div class="dplayer-setting-audio-item">
                    <div class="dplayer-toggle"><svg data-icon="check"></svg></div>
                </div>
            </div>
        </div>
    `;
    const setting_box = container.querySelector<HTMLElement>('.dplayer-setting-box')!;
    const setting_origin_panel = container.querySelector<HTMLElement>('.dplayer-setting-origin-panel')!;
    const audio = container.querySelector<HTMLElement>('.dplayer-setting-audio')!;
    const original_hide = vi.fn(() => {
        setting_box.classList.remove('dplayer-setting-box-open');
    });
    const original_show = vi.fn(() => {
        setting_box.classList.add('dplayer-setting-box-open');
    });
    return {
        player: {
            container,
            template: {
                settingBox: setting_box,
                settingOriginPanel: setting_origin_panel,
                audio,
            },
            setting: {
                show: original_show,
                hide: original_hide,
            },
            qualityIndex: 0,
            options: {
                video: {
                    quality: [{
                        name: current_quality_name,
                        url: '/api/streams/live/bs4k101/720p/mpegts?use_rain_fallback=1',
                    }],
                },
            },
            notice: vi.fn(),
            plugins: {
                mpegts: {
                    switchAudioTrack: vi.fn(),
                },
            },
        },
        original_hide,
    };
}


function createController(playback_mode: PlaybackMode, current_quality_name = '720p'): {
    controller: TestablePlayerController;
    player: FakePlayer;
    original_hide: ReturnType<typeof vi.fn>;
} {
    const { player, original_hide } = createFakePlayer(current_quality_name);
    const controller = Object.create(PlayerController.prototype) as TestablePlayerController;
    controller.playback_mode = playback_mode;
    controller.quality_profile_type = 'Wi-Fi';
    controller.initialization_generation = 1;
    controller.owner_signal = null;
    controller.destroyed = false;
    controller.destroying = false;
    controller.live_selected_audio_track_index = 0;
    controller.player = player;
    controller.applyAudioTrackLabels = vi.fn();
    controller.setupSettingPanelHandler();
    return { controller, player, original_hide };
}


describe('ワンセグ再生コーデック制約', () => {
    it.each([
        ['av1', ['hevc', 'avc']],
        ['vp9', ['hevc', 'avc']],
        ['hevc', ['hevc', 'avc']],
        ['avc', ['avc']],
    ] as const)('保存映像コーデック %s から再生時だけ %j に制限する', (video_codec, expected_order) => {
        expect(Videos.getKonomiTVBS4KLowerVideoCodecOrder(video_codec, true)).toEqual(expected_order);
    });

    it.each(['opus', 'aac'] as const)('保存音声コーデック %s から再生時だけ AAC に制限する', (audio_codec) => {
        expect(Videos.getKonomiTVBS4KLowerAudioCodecOrder(audio_codec, true)).toEqual(['aac']);
    });

    it('録画ワンセグにはライブ専用制約を適用しない', () => {
        expect(Videos.getKonomiTVBS4KLowerVideoCodecOrder('av1', false)).toEqual(['av1', 'vp9', 'hevc', 'avc']);
        expect(Videos.getKonomiTVBS4KLowerAudioCodecOrder('opus', false)).toEqual(['opus', 'aac']);
    });
});


describe('低解像度ライブ画質制約', () => {
    it.each([
        ['480i', ['480p', '360p', '240p']],
        ['480p', ['480p', '360p', '240p']],
        ['720p', ['720p', '540p', '480p', '360p', '240p']],
        ['1080i', LIVE_STREAMING_QUALITIES],
        [null, LIVE_STREAMING_QUALITIES],
        ['Unknown', LIVE_STREAMING_QUALITIES],
    ] as const)('入力解像度 %s に対して入力を超えない画質一覧を返す', (video_resolution, expected_qualities) => {
        expect(PlayerUtils.getKonomiTVBS4KLiveStreamingQualitiesForSourceResolution(
            video_resolution,
            LIVE_STREAMING_QUALITIES,
        )).toEqual(expected_qualities);
    });
});


describe('ISDB-S3の8ch超音声表示', () => {
    beforeEach(() => {
        localStorage.clear();
        document.body.innerHTML = '';
        setActivePinia(createPinia());

        const channels_store = useChannelsStore();
        channels_store.channels_list.BS4K = [Object.preventExtensions({
            ...structuredClone(ILiveChannelDefault),
            display_channel_id: 'bs4k101',
            type: 'BS4K' as const,
        })];
        channels_store.is_channels_list_initial_updated = true;
        channels_store.display_channel_id = 'bs4k101';
    });

    it('24chを選択不可で残し、後続の5.1chとステレオを実PMTトラックへ対応させる', () => {
        const channels_store = useChannelsStore();
        channels_store.current_program_present = {
            ...structuredClone(IProgramDefault),
            audio_components: [
                {
                    component_tag: 0x10,
                    channel_count: 24,
                    language: '日本語',
                    audio_type: '3/3/3-5/2/3-3/0/0.2モード',
                    sampling_rate: '48kHz',
                },
                {
                    component_tag: 0x11,
                    channel_count: 6,
                    language: '日本語',
                    audio_type: '3/2+LFEモード(3/2.1モード)',
                    sampling_rate: '48kHz',
                },
                {
                    component_tag: 0x12,
                    channel_count: 2,
                    language: '英語',
                    audio_type: '2/0モード(ステレオ)',
                    sampling_rate: '48kHz',
                },
            ],
        };
        const { controller } = createController('Live');

        const entries = controller.buildLiveAudioTrackDisplayEntries({
            audioTrackCount: 2,
            audioTrackComponentTags: [0x11, 0x12],
            hasAudio: true,
        });

        expect(entries).toEqual([
            {label: 'Track1 日本語 (22.2ch) [選択不可]', selectableTrackIndex: null},
            {label: 'Track2 日本語 (5.1ch)', selectableTrackIndex: 0},
            {label: 'Track3 英語 (Stereo)', selectableTrackIndex: 1},
        ]);
    });

    it('選択不可行は切り替えず、後続表示行を実PMTトラック番号で切り替える', () => {
        const channels_store = useChannelsStore();
        channels_store.current_program_present = {
            ...structuredClone(IProgramDefault),
            primary_audio_type: '3/3/3-5/2/3-3/0/0.2モード',
            primary_audio_language: '日本語',
            secondary_audio_type: '3/2+LFEモード(3/2.1モード)',
            secondary_audio_language: '日本語',
            audio_components: [],
        };
        const { controller, player } = createController('Live');
        const prototype = PlayerController.prototype as unknown as {
            applyAudioTrackLabels(
                this: TestablePlayerController,
                media_info?: {[key: string]: any},
            ): void;
        };
        controller.applyAudioTrackLabels = prototype.applyAudioTrackLabels.bind(controller);
        controller.applyAudioTrackLabels({
            audioTrackCount: 2,
            audioTrackComponentTags: [0x11, 0x12],
            hasAudio: true,
        });

        const audio_items = Array.from(
            player.container.querySelectorAll<HTMLElement>('.dplayer-setting-audio-item'),
        );
        expect(audio_items).toHaveLength(3);
        expect(audio_items.map(item => item.textContent)).toEqual([
            'Track1 日本語 (22.2ch) [選択不可]',
            'Track2 日本語 (5.1ch)',
            'Track3 言語不明',
        ]);
        expect(audio_items[0].classList.contains('dplayer-setting-audio-item--unsupported')).toBe(true);
        expect(audio_items[0].getAttribute('aria-disabled')).toBe('true');

        audio_items[0].click();
        expect(player.plugins.mpegts.switchAudioTrack).not.toHaveBeenCalled();
        audio_items[1].click();
        expect(player.plugins.mpegts.switchAudioTrack).toHaveBeenCalledWith(0);
        audio_items[2].click();
        expect(player.plugins.mpegts.switchAudioTrack).toHaveBeenLastCalledWith(1);
    });
});


describe('DPlayer設定パネル: 低遅延モード表示', () => {
    beforeEach(() => {
        localStorage.clear();
        document.body.innerHTML = '';
        setActivePinia(createPinia());

        const channels_store = useChannelsStore();
        channels_store.channels_list.GR = [Object.preventExtensions({
            ...structuredClone(ILiveChannelDefault),
            display_channel_id: 'gr011',
            type: 'GR' as const,
        })];
        channels_store.is_channels_list_initial_updated = true;
        channels_store.display_channel_id = 'gr011';
    });

    it.each([true, false])('通常ライブの実効状態 %s を読み取り専用で表示する', (is_low_latency_mode) => {
        useSettingsStore().settings.tv_low_latency_mode = is_low_latency_mode;
        const { player } = createController('Live');
        const setting_item = player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-low-latency-mode',
        );

        expect(setting_item).not.toBeNull();
        expect(setting_item?.querySelector('.dplayer-label')?.textContent).toBe('低遅延モード');
        expect(setting_item?.querySelector('.dplayer-konomitv-bs4k-setting-low-latency-mode-value')?.textContent?.trim())
            .toBe(is_low_latency_mode === true ? 'ON' : 'OFF');
        expect(setting_item?.querySelector('input')).toBeNull();
        expect(setting_item?.hasAttribute('role')).toBe(false);
        expect(setting_item?.hasAttribute('tabindex')).toBe(false);
    });

    it('録画再生には低遅延モードを表示しない', () => {
        const { player } = createController('Video');

        expect(player.container.querySelector('.dplayer-konomitv-bs4k-setting-low-latency-mode')).toBeNull();
    });

    it('BS4Kでサーバーが通常バッファを強制する場合はOFFと表示する', () => {
        const channels_store = useChannelsStore();
        channels_store.channels_list.GR = [];
        channels_store.channels_list.BS4K = [Object.preventExtensions({
            ...structuredClone(ILiveChannelDefault),
            display_channel_id: 'bs4k101',
            type: 'BS4K' as const,
        })];
        channels_store.display_channel_id = 'bs4k101';
        useSettingsStore().settings.tv_low_latency_mode_for_bs4k = true;
        useVersionStore().server_version_info = {
            version: '1.0.0',
            upstream_version: '0.14.1',
            git_commit: 'test',
            latest_version: null,
            environment: 'Linux-Docker',
            backend: 'EDCB',
            encoder: 'FFmpeg',
            encoder_bs4k: 'FFmpeg',
            bs4k_ignore_viewer_low_latency: true,
            konomitv_bs4k_live_transport: 'Tlv',
            jikkyo_enabled: false,
        };

        const { player } = createController('Live');
        expect(player.container.querySelector(
            '.dplayer-konomitv-bs4k-setting-low-latency-mode-value',
        )?.textContent?.trim()).toBe('OFF');
    });
});


describe('DPlayer設定パネル: 降雨対応映像表示', () => {
    beforeEach(() => {
        localStorage.clear();
        document.body.innerHTML = '';
        setActivePinia(createPinia());

        useVersionStore().server_version_info = {
            version: '1.0.0',
            upstream_version: '0.14.1',
            git_commit: 'test',
            latest_version: null,
            environment: 'Linux-Docker',
            backend: 'EDCB',
            encoder: 'FFmpeg',
            encoder_bs4k: 'FFmpeg',
            bs4k_ignore_viewer_low_latency: false,
            konomitv_bs4k_live_transport: 'Tlv',
            jikkyo_enabled: false,
        };
    });

    it.each([
        [null, null, '判定中', '判定中'],
        [false, true, '通常（主階層）', '実施中'],
        [true, false, '降雨対応（低階層）', '未実施'],
    ] as const)('SID 101 では映像階層 %s と送出状態 %s を独立して表示する', (
        is_rain_fallback,
        is_rain_fallback_broadcasting,
        expected_rain_fallback,
        expected_broadcasting,
    ) => {
        const channels_store = useChannelsStore();
        channels_store.channels_list.BS4K = [Object.preventExtensions({
            ...structuredClone(ILiveChannelDefault),
            network_id: 0x000B,
            service_id: 101,
            display_channel_id: 'bs4k101',
            type: 'BS4K' as const,
        })];
        channels_store.is_channels_list_initial_updated = true;
        channels_store.display_channel_id = 'bs4k101';
        const player_store = usePlayerStore();
        player_store.is_rain_fallback = is_rain_fallback;
        player_store.is_rain_fallback_broadcasting = is_rain_fallback_broadcasting;

        const { player } = createController('Live');

        expect(player.container.querySelector(
            '.dplayer-konomitv-bs4k-setting-rain-fallback-status .dplayer-label',
        )?.textContent).toBe('映像階層');
        expect(player.container.querySelector(
            '.dplayer-konomitv-bs4k-setting-rain-fallback-status-value',
        )?.textContent?.trim()).toBe(expected_rain_fallback);
        expect(player.container.querySelector(
            '.dplayer-konomitv-bs4k-setting-rain-fallback-broadcasting-status .dplayer-label',
        )?.textContent).toBe('降雨対応放送');
        expect(player.container.querySelector(
            '.dplayer-konomitv-bs4k-setting-rain-fallback-broadcasting-status-value',
        )?.textContent?.trim()).toBe(expected_broadcasting);
        expect(player.container.querySelector('.dplayer-konomitv-bs4k-setting-rain-fallback')).not.toBeNull();
    });

    it('単一の状態更新で両表示を追従させ、プレイヤーを再起動しない', async () => {
        const channels_store = useChannelsStore();
        channels_store.channels_list.BS4K = [Object.preventExtensions({
            ...structuredClone(ILiveChannelDefault),
            network_id: 0x000B,
            service_id: 101,
            display_channel_id: 'bs4k101',
            type: 'BS4K' as const,
        })];
        channels_store.is_channels_list_initial_updated = true;
        channels_store.display_channel_id = 'bs4k101';
        const player_store = usePlayerStore();
        const restart_handler = vi.fn();
        player_store.event_emitter.on('PlayerRestartRequired', restart_handler);
        const { player } = createController('Live');

        player_store.is_rain_fallback = true;
        player_store.is_rain_fallback_broadcasting = false;
        await nextTick();

        expect(player.container.querySelector(
            '.dplayer-konomitv-bs4k-setting-rain-fallback-status-value',
        )?.textContent).toBe('降雨対応（低階層）');
        expect(player.container.querySelector(
            '.dplayer-konomitv-bs4k-setting-rain-fallback-broadcasting-status-value',
        )?.textContent).toBe('未実施');
        expect(restart_handler).not.toHaveBeenCalled();
    });

    it('対象外 SID では降雨対応映像の状態とトグルを表示しない', () => {
        const channels_store = useChannelsStore();
        channels_store.channels_list.BS4K = [Object.preventExtensions({
            ...structuredClone(ILiveChannelDefault),
            network_id: 0x000B,
            service_id: 191,
            display_channel_id: 'bs4k191',
            type: 'BS4K' as const,
        })];
        channels_store.is_channels_list_initial_updated = true;
        channels_store.display_channel_id = 'bs4k191';

        const { player } = createController('Live');

        expect(player.container.querySelector('.dplayer-konomitv-bs4k-setting-rain-fallback-status')).toBeNull();
        expect(player.container.querySelector(
            '.dplayer-konomitv-bs4k-setting-rain-fallback-broadcasting-status',
        )).toBeNull();
        expect(player.container.querySelector('.dplayer-konomitv-bs4k-setting-rain-fallback')).toBeNull();
    });

    it.each(['8K', '4K', '1440p'])('%s 視聴中に自動使用をOFFにしてもプレイヤーを再起動しない', (
        current_quality_name,
    ) => {
        const channels_store = useChannelsStore();
        channels_store.channels_list.BS4K = [Object.preventExtensions({
            ...structuredClone(ILiveChannelDefault),
            network_id: 0x000B,
            service_id: 101,
            display_channel_id: 'bs4k101',
            type: 'BS4K' as const,
        })];
        channels_store.is_channels_list_initial_updated = true;
        channels_store.display_channel_id = 'bs4k101';
        const player_store = usePlayerStore();
        const restart_handler = vi.fn();
        player_store.event_emitter.on('PlayerRestartRequired', restart_handler);

        const { player } = createController('Live', current_quality_name);
        player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-rain-fallback',
        )!.click();

        expect(useSettingsStore().settings.tv_use_rain_fallback_for_bs4k).toBe(false);
        expect(new URL(player.options.video.quality[0].url).searchParams.get('use_rain_fallback')).toBe('0');
        expect(restart_handler).not.toHaveBeenCalled();
        expect(player.notice).toHaveBeenCalledWith(
            '設定を保存しました。1080p 以下の画質へ切り替えたときから適用されます。',
        );
    });

    it('1080p 以下の視聴中はトグル変更後にプレイヤーを再起動する', () => {
        const channels_store = useChannelsStore();
        channels_store.channels_list.BS4K = [Object.preventExtensions({
            ...structuredClone(ILiveChannelDefault),
            network_id: 0x000B,
            service_id: 102,
            display_channel_id: 'bs4k102',
            type: 'BS4K' as const,
        })];
        channels_store.is_channels_list_initial_updated = true;
        channels_store.display_channel_id = 'bs4k102';
        const player_store = usePlayerStore();
        const restart_handler = vi.fn();
        player_store.event_emitter.on('PlayerRestartRequired', restart_handler);

        const { player } = createController('Live', '1080p (60fps)');
        player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-rain-fallback',
        )!.click();

        expect(restart_handler).toHaveBeenCalledTimes(1);
    });
});


describe.each<PlaybackMode>(['Live', 'Video'])('DPlayer codecサブパネル: %s', (playback_mode) => {
    beforeEach(() => {
        localStorage.clear();
        document.body.innerHTML = '';
        setActivePinia(createPinia());

        const channels_store = useChannelsStore();
        channels_store.channels_list.GR = [Object.preventExtensions({
            ...structuredClone(ILiveChannelDefault),
            display_channel_id: 'gr011',
            type: 'GR' as const,
        })];
        channels_store.is_channels_list_initial_updated = true;
        channels_store.display_channel_id = 'gr011';
        usePlayerStore().recorded_program.network_id = 0x0004;
        usePlayerStore().recorded_program.recorded_video.has_video = true;
        vi.spyOn(Videos, 'preflightKonomiTVBS4KPlaybackProfile').mockImplementation(
            async (_encoder, video_codec, audio_codec) => ({
                video_codec,
                video_bit_depth: video_codec === 'hevc' ? 10 : 8,
                audio_codec,
                fallback_reason: 'None',
            }),
        );
    });

    it('EnterとSpaceで開く・戻る・映像選択・音声選択を操作できる', async () => {
        const player_store = usePlayerStore();
        const restart_handler = vi.fn();
        player_store.event_emitter.on('PlayerRestartRequired', restart_handler);
        const { player } = createController(playback_mode);
        const setting_box = player.template.settingBox;
        const bubbled_keydown = vi.fn();
        setting_box.addEventListener('keydown', bubbled_keydown);

        const video_button = player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-video-codec',
        )!;
        video_button.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));
        expect(setting_box.classList.contains('dplayer-konomitv-bs4k-setting-box-video-codec')).toBe(false);
        bubbled_keydown.mockClear();

        const enter_event = new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true});
        expect(video_button.dispatchEvent(enter_event)).toBe(false);
        expect(setting_box.classList.contains('dplayer-konomitv-bs4k-setting-box-video-codec')).toBe(true);
        expect(bubbled_keydown).not.toHaveBeenCalled();

        const video_back = player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-video-codec-header',
        )!;
        const space_event = new KeyboardEvent('keydown', {key: ' ', bubbles: true, cancelable: true});
        expect(video_back.dispatchEvent(space_event)).toBe(false);
        expect(setting_box.classList.contains('dplayer-konomitv-bs4k-setting-box-video-codec')).toBe(false);

        const audio_button = player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-audio-codec',
        )!;
        audio_button.dispatchEvent(new KeyboardEvent('keydown', {key: ' ', bubbles: true, cancelable: true}));
        expect(setting_box.classList.contains('dplayer-konomitv-bs4k-setting-box-audio-codec')).toBe(true);
        const audio_back = player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-audio-codec-header',
        )!;
        audio_back.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
        expect(setting_box.classList.contains('dplayer-konomitv-bs4k-setting-box-audio-codec')).toBe(false);

        audio_button.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
        player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-audio-codec-item[data-codec="aac"]',
        )!.dispatchEvent(new KeyboardEvent('keydown', {key: ' ', bubbles: true, cancelable: true}));
        await vi.waitFor(() => expect(restart_handler).toHaveBeenCalledTimes(1));
        expect(player_store.konomitv_bs4k_playback_codec_override?.audio_codec).toBe('aac');

        video_button.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
        player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-video-codec-item[data-codec="hevc"]',
        )!.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
        await vi.waitFor(() => expect(restart_handler).toHaveBeenCalledTimes(2));
        expect(player_store.konomitv_bs4k_playback_codec_override?.video_codec).toBe('hevc');
    });


    it('現在画質で事前検査し、画面scope overrideだけへ保存して再起動する', async () => {
        const settings_store = useSettingsStore();
        const player_store = usePlayerStore();
        const saved_settings_json = JSON.stringify(settings_store.settings);
        const restart_handler = vi.fn();
        player_store.event_emitter.on('PlayerRestartRequired', restart_handler);
        const { player, original_hide } = createController(playback_mode);

        player.container.querySelector<HTMLElement>('.dplayer-konomitv-bs4k-setting-video-codec')!.click();
        player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-video-codec-item[data-codec="hevc"]',
        )!.click();
        await vi.waitFor(() => expect(restart_handler).toHaveBeenCalledTimes(1));
        expect(player_store.konomitv_bs4k_playback_codec_override).toEqual({
            video_codec: 'hevc',
            audio_codec: 'opus',
        });

        player.container.querySelector<HTMLElement>('.dplayer-konomitv-bs4k-setting-audio-codec')!.click();
        player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-audio-codec-item[data-codec="aac"]',
        )!.click();
        await vi.waitFor(() => expect(restart_handler).toHaveBeenCalledTimes(2));
        expect(player_store.konomitv_bs4k_playback_codec_override).toEqual({
            video_codec: 'hevc',
            audio_codec: 'aac',
        });
        expect(JSON.stringify(settings_store.settings)).toBe(saved_settings_json);
        expect(original_hide).toHaveBeenCalledTimes(2);
        expect(restart_handler).toHaveBeenCalledTimes(2);
        expect(Videos.preflightKonomiTVBS4KPlaybackProfile).toHaveBeenNthCalledWith(
            1,
            expect.any(String),
            'hevc',
            'opus',
            {is_bs4k: false, streaming_quality: '720p'},
            playback_mode,
            true,
            false,
            undefined,
        );
    });

    it('下位互換候補ゼロなら旧player・pin・overrideを保持し、再起動しない', async () => {
        vi.mocked(Videos.preflightKonomiTVBS4KPlaybackProfile).mockResolvedValueOnce(null);
        const player_store = usePlayerStore();
        player_store.konomitv_bs4k_playback_codec_override = {
            video_codec: 'avc',
            audio_codec: 'aac',
        };
        player_store.konomitv_bs4k_effective_playback_profile = {
            target_key: `${playback_mode}:gr011:Wi-Fi`,
            encoder: 'FFmpeg',
            requested_video_codec: 'avc',
            requested_audio_codec: 'aac',
            video_codec: 'avc',
            video_bit_depth: 8,
            audio_codec: 'aac',
        };
        const previous_pin = {...player_store.konomitv_bs4k_effective_playback_profile};
        const restart_handler = vi.fn();
        player_store.event_emitter.on('PlayerRestartRequired', restart_handler);
        const { controller, player, original_hide } = createController(playback_mode);

        player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-video-codec-item[data-codec="av1"]',
        )!.click();
        await vi.waitFor(() => expect(player.notice).toHaveBeenCalled());

        expect(controller.player).toBe(player);
        expect(player_store.konomitv_bs4k_playback_codec_override).toEqual({
            video_codec: 'avc',
            audio_codec: 'aac',
        });
        expect(player_store.konomitv_bs4k_effective_playback_profile).toEqual(previous_pin);
        expect(restart_handler).not.toHaveBeenCalled();
        expect(original_hide).not.toHaveBeenCalled();
    });

    it('事前検査中に世代が変わった古い応答はStoreとDOMを書き換えない', async () => {
        let release_preflight!: (value: {
            video_codec: 'av1';
            video_bit_depth: 8;
            audio_codec: 'opus';
            fallback_reason: 'None';
        }) => void;
        const pending_preflight = new Promise<{
            video_codec: 'av1';
            video_bit_depth: 8;
            audio_codec: 'opus';
            fallback_reason: 'None';
        }>((resolve) => { release_preflight = resolve; });
        vi.mocked(Videos.preflightKonomiTVBS4KPlaybackProfile).mockReturnValueOnce(pending_preflight);
        const player_store = usePlayerStore();
        const restart_handler = vi.fn();
        player_store.event_emitter.on('PlayerRestartRequired', restart_handler);
        const { controller, player } = createController(playback_mode);

        player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-video-codec-item[data-codec="av1"]',
        )!.click();
        controller.initialization_generation += 1;
        release_preflight({
            video_codec: 'av1',
            video_bit_depth: 8,
            audio_codec: 'opus',
            fallback_reason: 'None',
        });
        await Promise.resolve();
        await Promise.resolve();

        expect(player_store.konomitv_bs4k_playback_codec_override).toBeNull();
        expect(player_store.konomitv_bs4k_effective_playback_profile).toBeNull();
        expect(restart_handler).not.toHaveBeenCalled();
    });

    it('プレイヤー再初期化後の新しいDOMにもhandlerを登録する', () => {
        const first = createController(playback_mode);
        const second = createController(playback_mode);

        first.player.container.querySelector<HTMLElement>('.dplayer-konomitv-bs4k-setting-video-codec')!.click();
        second.player.container.querySelector<HTMLElement>('.dplayer-konomitv-bs4k-setting-video-codec')!.click();

        expect(first.player.template.settingBox.classList.contains(
            'dplayer-konomitv-bs4k-setting-box-video-codec',
        )).toBe(true);
        expect(second.player.template.settingBox.classList.contains(
            'dplayer-konomitv-bs4k-setting-box-video-codec',
        )).toBe(true);
    });
});


describe('DPlayer codecサブパネル: ワンセグライブ', () => {
    beforeEach(() => {
        localStorage.clear();
        document.body.innerHTML = '';
        setActivePinia(createPinia());

        const channels_store = useChannelsStore();
        channels_store.channels_list.GR = [Object.preventExtensions({
            ...structuredClone(ILiveChannelDefault),
            display_channel_id: 'gr013',
            type: 'GR' as const,
            is_oneseg: true,
        })];
        channels_store.is_channels_list_initial_updated = true;
        channels_store.display_channel_id = 'gr013';
        vi.spyOn(Videos, 'preflightKonomiTVBS4KPlaybackProfile').mockResolvedValue({
            video_codec: 'hevc',
            video_bit_depth: 8,
            audio_codec: 'aac',
            fallback_reason: 'CapabilityFallback',
        });
    });

    it('AV1選択を240p HEVC/AACへ一時フォールバックし、保存設定は変更しない', async () => {
        const settings_store = useSettingsStore();
        const player_store = usePlayerStore();
        const saved_settings_json = JSON.stringify(settings_store.settings);
        const restart_handler = vi.fn();
        player_store.event_emitter.on('PlayerRestartRequired', restart_handler);
        const { player } = createController('Live');

        player.container.querySelector<HTMLElement>('.dplayer-konomitv-bs4k-setting-video-codec')!.click();
        player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-video-codec-item[data-codec="av1"]',
        )!.click();

        await vi.waitFor(() => expect(restart_handler).toHaveBeenCalledTimes(1));
        expect(Videos.preflightKonomiTVBS4KPlaybackProfile).toHaveBeenCalledWith(
            expect.any(String),
            'av1',
            'opus',
            {is_bs4k: false, streaming_quality: '240p'},
            'Live',
            true,
            true,
            undefined,
        );
        expect(player_store.konomitv_bs4k_playback_codec_override).toEqual({
            video_codec: 'av1',
            audio_codec: 'opus',
        });
        expect(player_store.konomitv_bs4k_effective_playback_profile).toMatchObject({
            requested_video_codec: 'av1',
            requested_audio_codec: 'opus',
            video_codec: 'hevc',
            audio_codec: 'aac',
        });
        expect(JSON.stringify(settings_store.settings)).toBe(saved_settings_json);
    });

    it('実効HEVC/AACへチェックを移し、希望AV1/Opusとの自動フォールバックを表示する', () => {
        const settings_store = useSettingsStore();
        const player_store = usePlayerStore();
        const saved_settings_json = JSON.stringify(settings_store.settings);
        player_store.konomitv_bs4k_effective_playback_profile = {
            target_key: 'Live:gr013:Wi-Fi',
            encoder: 'FFmpeg',
            requested_video_codec: 'av1',
            requested_audio_codec: 'opus',
            video_codec: 'hevc',
            video_bit_depth: 8,
            audio_codec: 'aac',
        };

        const { player } = createController('Live');
        expect(player.container.querySelector(
            '.dplayer-konomitv-bs4k-setting-video-codec-value',
        )?.textContent).toBe('HEVC（AV1→自動）');
        expect(player.container.querySelector(
            '.dplayer-konomitv-bs4k-setting-audio-codec-value',
        )?.textContent).toBe('AAC（Opus→自動）');

        const video_items = Array.from(player.container.querySelectorAll<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-video-codec-item',
        ));
        const audio_items = Array.from(player.container.querySelectorAll<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-audio-codec-item',
        ));
        const av1_item = video_items.find((item) => item.dataset.codec === 'av1')!;
        const hevc_item = video_items.find((item) => item.dataset.codec === 'hevc')!;
        const opus_item = audio_items.find((item) => item.dataset.codec === 'opus')!;
        const aac_item = audio_items.find((item) => item.dataset.codec === 'aac')!;

        expect(av1_item.querySelector('.dplayer-konomitv-bs4k-setting-video-codec-status')?.textContent).toBe('希望');
        expect(hevc_item.querySelector('.dplayer-konomitv-bs4k-setting-video-codec-status')?.textContent).toBe('再生中');
        expect(hevc_item.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-video-codec-check',
        )?.style.visibility).toBe('visible');
        expect(av1_item.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-video-codec-check',
        )?.style.visibility).toBe('hidden');
        expect(opus_item.querySelector('.dplayer-konomitv-bs4k-setting-audio-codec-status')?.textContent).toBe('希望');
        expect(aac_item.querySelector('.dplayer-konomitv-bs4k-setting-audio-codec-status')?.textContent).toBe('再生中');
        expect(aac_item.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-audio-codec-check',
        )?.style.visibility).toBe('visible');
        expect(opus_item.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-audio-codec-check',
        )?.style.visibility).toBe('hidden');
        expect(JSON.stringify(settings_store.settings)).toBe(saved_settings_json);
    });

    it('フォールバックなしでは従来どおり希望値だけを表示する', () => {
        const player_store = usePlayerStore();
        player_store.konomitv_bs4k_effective_playback_profile = {
            target_key: 'Live:gr013:Wi-Fi',
            encoder: 'FFmpeg',
            requested_video_codec: 'av1',
            requested_audio_codec: 'opus',
            video_codec: 'av1',
            video_bit_depth: 8,
            audio_codec: 'opus',
        };

        const { player } = createController('Live');
        expect(player.container.querySelector(
            '.dplayer-konomitv-bs4k-setting-video-codec-value',
        )?.textContent).toBe('AV1');
        expect(player.container.querySelector(
            '.dplayer-konomitv-bs4k-setting-audio-codec-value',
        )?.textContent).toBe('Opus');
        expect(Array.from(player.container.querySelectorAll(
            '.dplayer-konomitv-bs4k-setting-video-codec-status, ' +
            '.dplayer-konomitv-bs4k-setting-audio-codec-status',
        )).every((status) => status.textContent === '')).toBe(true);
    });
});
