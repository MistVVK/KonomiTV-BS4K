import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import LiveEventManager from '@/services/player/managers/LiveEventManager';
import useChannelsStore from '@/stores/ChannelsStore';
import usePlayerStore from '@/stores/PlayerStore';


vi.mock('@/utils', async (import_original) => {
    const original = await import_original<typeof import('@/utils')>();
    return {
        ...original,
        PlayerUtils: {
            ...original.PlayerUtils,
            buildKonomiTVBS4KLiveAPIEndpointURLFromDPlayer: () => '/api/streams/live/test/events',
        },
    };
});


type EventListener = (event: Event | MessageEvent) => void;


class MockEventSource {

    public static readonly instances: MockEventSource[] = [];

    public readonly close = vi.fn();
    private readonly listeners = new Map<string, EventListener[]>();

    public constructor(public readonly url: string) {
        MockEventSource.instances.push(this);
    }

    public addEventListener(type: string, listener: EventListener): void {
        const listeners = this.listeners.get(type) ?? [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
    }

    public emit(type: string, data?: unknown): void {
        const event = data === undefined ?
            new Event(type) :
            new MessageEvent(type, {data: JSON.stringify(data)});
        for (const listener of this.listeners.get(type) ?? []) {
            listener(event);
        }
    }
}


function createPlayer() {
    const container = document.createElement('div');
    const notice = document.createElement('div');
    const video = document.createElement('video');
    container.append(video, notice);
    return {
        container,
        video,
        template: {notice},
        danmaku: {clear: vi.fn()},
        notice: vi.fn(),
        hideNotice: vi.fn(),
    };
}


describe('LiveEventManager lifecycle generation', () => {
    beforeEach(() => {
        vi.useFakeTimers();
        setActivePinia(createPinia());
        MockEventSource.instances.length = 0;
        vi.stubGlobal('EventSource', MockEventSource);
    });

    it('旧 init の3秒接続待ちが新世代の表示状態を上書きしない', async () => {
        const player_store = usePlayerStore();
        const manager = new LiveEventManager(createPlayer() as never);

        await manager.init();
        await manager.destroy();
        await manager.init();
        MockEventSource.instances[1].emit('open');

        player_store.is_video_buffering = false;
        player_store.is_background_display = false;
        await vi.advanceTimersByTimeAsync(3_000);

        expect(player_store.is_video_buffering).toBe(false);
        expect(player_store.is_background_display).toBe(false);
    });

    it('旧世代の Idling 待機完了後にプレイヤー再起動を要求しない', async () => {
        const player_store = usePlayerStore();
        const emit = vi.spyOn(player_store.event_emitter, 'emit');
        const manager = new LiveEventManager(createPlayer() as never);

        await manager.init();
        MockEventSource.instances[0].emit('status_update', {
            status: 'Idling',
            detail: 'old stream',
            started_at: 0,
            updated_at: 0,
            client_count: 1,
        });
        await manager.destroy();
        await manager.init();
        MockEventSource.instances[1].emit('open');
        await vi.advanceTimersByTimeAsync(1_000);

        expect(emit).not.toHaveBeenCalledWith(
            'PlayerRestartRequired',
            expect.anything(),
        );
    });

    it('close 済み旧 EventSource の queued event を Store へ反映しない', async () => {
        const channels_store = useChannelsStore();
        const manager = new LiveEventManager(createPlayer() as never);

        await manager.init();
        const old_eventsource = MockEventSource.instances[0];
        await manager.destroy();
        await manager.init();

        old_eventsource.emit('clients_update', {
            status: 'ONAir',
            detail: '',
            started_at: 0,
            updated_at: 0,
            client_count: 99,
        });
        expect(channels_store.viewer_count).not.toBe(99);

        MockEventSource.instances[1].emit('clients_update', {
            status: 'ONAir',
            detail: '',
            started_at: 0,
            updated_at: 0,
            client_count: 2,
        });
        expect(channels_store.viewer_count).toBe(2);
    });

    it('detail_update で降雨対応映像の使用状態を直ちに反映する', async () => {
        const player_store = usePlayerStore();
        const manager = new LiveEventManager(createPlayer() as never);

        await manager.init();
        MockEventSource.instances[0].emit('detail_update', {
            status: 'Standby',
            detail: '降雨対応放送を使用してエンコードを開始しています…',
            started_at: 0,
            updated_at: 1,
            client_count: 1,
            is_rain_fallback: true,
        });

        expect(player_store.is_rain_fallback).toBe(true);
        await manager.destroy();
        expect(player_store.is_rain_fallback).toBe(false);
    });
});
