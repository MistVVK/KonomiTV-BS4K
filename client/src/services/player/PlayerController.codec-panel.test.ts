import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ILiveChannelDefault } from '@/services/Channels';
import PlayerController, { generateRecordedPlaybackSessionID } from '@/services/player/PlayerController';
import Videos from '@/services/Videos';
import useChannelsStore from '@/stores/ChannelsStore';
import usePlayerStore from '@/stores/PlayerStore';
import useSettingsStore from '@/stores/SettingsStore';


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
            quality: Array<{name: string}>;
        };
    };
    notice: ReturnType<typeof vi.fn>;
};

type TestablePlayerController = {
    playback_mode: PlaybackMode;
    quality_profile_type: 'Wi-Fi' | 'Cellular';
    player: FakePlayer;
    applyAudioTrackLabels: () => void;
    setupSettingPanelHandler: () => void;
    initialization_generation: number;
    owner_signal: AbortSignal | null;
    destroyed: boolean;
    destroying: boolean;
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


function createFakePlayer(): { player: FakePlayer; original_hide: ReturnType<typeof vi.fn> } {
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
                    quality: [{name: '720p'}],
                },
            },
            notice: vi.fn(),
        },
        original_hide,
    };
}


function createController(playback_mode: PlaybackMode): {
    controller: TestablePlayerController;
    player: FakePlayer;
    original_hide: ReturnType<typeof vi.fn>;
} {
    const { player, original_hide } = createFakePlayer();
    const controller = Object.create(PlayerController.prototype) as TestablePlayerController;
    controller.playback_mode = playback_mode;
    controller.quality_profile_type = 'Wi-Fi';
    controller.initialization_generation = 1;
    controller.owner_signal = null;
    controller.destroyed = false;
    controller.destroying = false;
    controller.player = player;
    controller.applyAudioTrackLabels = vi.fn();
    controller.setupSettingPanelHandler();
    return { controller, player, original_hide };
}


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

    it('clickとtouch由来clickで映像・音声パネルを開き、戻る操作で閉じる', () => {
        const { player } = createController(playback_mode);
        const setting_box = player.template.settingBox;
        const bubbled_click = vi.fn();
        setting_box.addEventListener('click', bubbled_click);

        const video_button = player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-video-codec',
        )!;
        video_button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        expect(setting_box.classList.contains('dplayer-konomitv-bs4k-setting-box-video-codec')).toBe(true);
        expect(setting_box.style.getPropertyValue('--konomitv-bs4k-video-codec-panel-height')).toBe('174px');
        expect(bubbled_click).not.toHaveBeenCalled();

        const video_back = player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-video-codec-header',
        )!;
        video_back.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        expect(setting_box.classList.contains('dplayer-konomitv-bs4k-setting-box-video-codec')).toBe(false);

        const audio_button = player.container.querySelector<HTMLElement>(
            '.dplayer-konomitv-bs4k-setting-audio-codec',
        )!;
        audio_button.dispatchEvent(new PointerEvent('click', { bubbles: true, pointerType: 'touch' }));
        expect(setting_box.classList.contains('dplayer-konomitv-bs4k-setting-box-audio-codec')).toBe(true);
        expect(setting_box.style.getPropertyValue('--konomitv-bs4k-audio-codec-panel-height')).toBe('114px');
        expect(bubbled_click).not.toHaveBeenCalled();
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
