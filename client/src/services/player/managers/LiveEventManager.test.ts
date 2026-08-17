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


type LiveStreamStatusEvent = {
    status: 'Offline' | 'Standby' | 'ONAir' | 'Idling' | 'Restart';
    detail: string;
    started_at: number;
    updated_at: number;
    client_count: number;
    is_rain_fallback: boolean | null;
    is_rain_fallback_broadcasting: boolean | null;
};


function createLiveStreamStatusEvent(
    overrides: Partial<LiveStreamStatusEvent> = {},
): LiveStreamStatusEvent {
    return {
        status: 'ONAir',
        detail: '',
        started_at: 0,
        updated_at: 0,
        client_count: 1,
        is_rain_fallback: null,
        is_rain_fallback_broadcasting: null,
        ...overrides,
    };
}


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
        MockEventSource.instances[0].emit('status_update', createLiveStreamStatusEvent({
            status: 'Idling',
            detail: 'old stream',
        }));
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

        old_eventsource.emit('clients_update', createLiveStreamStatusEvent({
            client_count: 99,
            is_rain_fallback: true,
            is_rain_fallback_broadcasting: true,
        }));
        expect(channels_store.viewer_count).not.toBe(99);
        expect(usePlayerStore().is_rain_fallback).toBeNull();
        expect(usePlayerStore().is_rain_fallback_broadcasting).toBeNull();

        MockEventSource.instances[1].emit('clients_update', createLiveStreamStatusEvent({
            client_count: 2,
            is_rain_fallback: false,
            is_rain_fallback_broadcasting: true,
        }));
        expect(channels_store.viewer_count).toBe(2);
        expect(usePlayerStore().is_rain_fallback).toBe(false);
        expect(usePlayerStore().is_rain_fallback_broadcasting).toBe(true);
    });

    it.each([
        ['initial_update', null, false],
        ['status_update', true, null],
        ['detail_update', false, true],
        ['clients_update', null, true],
    ] as const)('%s で映像階層と降雨対応放送の送出状態を反映する', async (
        event_name,
        is_rain_fallback,
        is_rain_fallback_broadcasting,
    ) => {
        const player_store = usePlayerStore();
        const manager = new LiveEventManager(createPlayer() as never);

        await manager.init();
        MockEventSource.instances[0].emit(event_name, createLiveStreamStatusEvent({
            is_rain_fallback,
            is_rain_fallback_broadcasting,
        }));

        expect(player_store.is_rain_fallback).toBe(is_rain_fallback);
        expect(player_store.is_rain_fallback_broadcasting).toBe(is_rain_fallback_broadcasting);
        await manager.destroy();
    });

    it('Restart は両状態を反映してからプレイヤー再起動を要求する', async () => {
        const player_store = usePlayerStore();
        const state_when_restart_required = vi.fn();
        player_store.event_emitter.on('PlayerRestartRequired', () => {
            state_when_restart_required(
                player_store.is_rain_fallback,
                player_store.is_rain_fallback_broadcasting,
            );
        });
        const manager = new LiveEventManager(createPlayer() as never);

        await manager.init();
        MockEventSource.instances[0].emit('status_update', createLiveStreamStatusEvent({
            status: 'Restart',
            detail: 'restart required',
            is_rain_fallback: true,
            is_rain_fallback_broadcasting: false,
        }));

        expect(state_when_restart_required).toHaveBeenCalledWith(true, false);
    });

    it('destroy で映像階層と降雨対応放送の送出状態を未判定へ戻す', async () => {
        const player_store = usePlayerStore();
        const manager = new LiveEventManager(createPlayer() as never);

        await manager.init();
        MockEventSource.instances[0].emit('initial_update', createLiveStreamStatusEvent({
            is_rain_fallback: true,
            is_rain_fallback_broadcasting: false,
        }));
        await manager.destroy();

        expect(player_store.is_rain_fallback).toBeNull();
        expect(player_store.is_rain_fallback_broadcasting).toBeNull();
    });
});
