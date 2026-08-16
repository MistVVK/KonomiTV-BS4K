import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import { ILiveChannelDefault } from '@/services/Channels';
import PlayerController from '@/services/player/PlayerController';
import useChannelsStore from '@/stores/ChannelsStore';
import useSettingsStore from '@/stores/SettingsStore';
import useVersionStore from '@/stores/VersionStore';


type LowLatencyController = {
    playback_mode: 'Live';
    quality_profile_type: 'Wi-Fi' | 'Cellular';
};


function createBS4KController(quality_profile_type: 'Wi-Fi' | 'Cellular'): LowLatencyController {
    const channels_store = useChannelsStore();
    channels_store.channels_list.BS4K = [Object.preventExtensions({
        ...structuredClone(ILiveChannelDefault),
        display_channel_id: 'bs4k101',
        type: 'BS4K' as const,
    })];
    channels_store.is_channels_list_initial_updated = true;
    channels_store.display_channel_id = 'bs4k101';

    const controller = Object.create(PlayerController.prototype) as LowLatencyController;
    controller.playback_mode = 'Live';
    controller.quality_profile_type = quality_profile_type;
    return controller;
}


function resolveLowLatencyMode(controller: LowLatencyController): boolean {
    const getter = Object.getOwnPropertyDescriptor(PlayerController.prototype, 'tv_low_latency_mode')?.get;
    if (getter === undefined) throw new Error('低遅延モード判定を取得できません。');
    return getter.call(controller) as boolean;
}


function setBS4KIgnoreViewerLowLatency(value: boolean): void {
    useVersionStore().server_version_info = {
        version: '1.0.0',
        upstream_version: '0.14.1',
        git_commit: 'test',
        latest_version: null,
        environment: 'Linux-Docker',
        backend: 'EDCB',
        encoder: 'FFmpeg',
        encoder_bs4k: 'FFmpeg',
        bs4k_ignore_viewer_low_latency: value,
        konomitv_bs4k_live_transport: 'Tlv',
        jikkyo_enabled: false,
    };
}


describe('BS4K低遅延モード', () => {
    beforeEach(() => {
        localStorage.clear();
        setActivePinia(createPinia());
        setBS4KIgnoreViewerLowLatency(false);
    });

    it('Wi-FiとモバイルでBS4K専用設定を個別に参照する', () => {
        const settings_store = useSettingsStore();
        settings_store.settings.tv_low_latency_mode_for_bs4k = true;
        settings_store.settings.tv_low_latency_mode_for_bs4k_cellular = false;

        expect(resolveLowLatencyMode(createBS4KController('Wi-Fi'))).toBe(true);
        expect(resolveLowLatencyMode(createBS4KController('Cellular'))).toBe(false);
    });

    it('サーバー側の通常バッファ強制設定を優先する', () => {
        useSettingsStore().settings.tv_low_latency_mode_for_bs4k = true;
        setBS4KIgnoreViewerLowLatency(true);

        expect(resolveLowLatencyMode(createBS4KController('Wi-Fi'))).toBe(false);
    });
});
