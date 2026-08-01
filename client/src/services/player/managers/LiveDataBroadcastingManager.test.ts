import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createDeferred } from '../../../../tests/helpers/deferred';

import { ILiveChannelDefault } from '@/services/Channels';
import LiveDataBroadcastingManager from '@/services/player/managers/LiveDataBroadcastingManager';
import { IProgramDefault } from '@/services/Programs';
import useChannelsStore from '@/stores/ChannelsStore';
import useSettingsStore from '@/stores/SettingsStore';


const mocks = vi.hoisted(() => {
    type BMLEventListener = (event: {detail: unknown}) => void;

    class MockBMLBrowser {

        public static readonly instances: MockBMLBrowser[] = [];

        public readonly fonts = [{load: vi.fn()}];
        public readonly content = {
            processKeyDown: vi.fn(),
            processKeyUp: vi.fn(),
        };
        public readonly destroy = vi.fn(async () => {});
        public readonly emitMessage = vi.fn();
        public readonly options: Record<string, unknown>;
        public readonly video_element = document.createElement('div');
        private readonly listeners = new Map<string, BMLEventListener[]>();

        public constructor(options: Record<string, unknown>) {
            this.options = options;
            this.video_element.append(document.createElement('p'));
            MockBMLBrowser.instances.push(this);
        }

        public addEventListener(type: string, listener: BMLEventListener): void {
            const listeners = this.listeners.get(type) ?? [];
            listeners.push(listener);
            this.listeners.set(type, listeners);
        }

        public emit(type: string, detail: unknown): void {
            for (const listener of this.listeners.get(type) ?? []) {
                listener({detail});
            }
        }

        public getVideoElement(): HTMLElement {
            return this.video_element;
        }
    }

    return {
        MockBMLBrowser,
        proxy_results: [] as unknown[],
        proxy_constructor: vi.fn(),
        router_push: vi.fn(),
    };
});


vi.mock('comlink', () => ({
    proxy: <Callback>(callback: Callback): Callback => callback,
}));

vi.mock('web-bml', () => ({
    AribKeyCode: {Digit0: 0},
    BMLBrowser: mocks.MockBMLBrowser,
}));

vi.mock('@/router', () => ({
    default: {push: mocks.router_push},
}));

vi.mock('@/workers/LivePSIArchivedDataDecoderProxy', () => ({
    default: function MockLivePSIArchivedDataDecoderProxy(...args: unknown[]) {
        mocks.proxy_constructor(...args);
        const result = mocks.proxy_results.shift();
        if (result === undefined) throw new Error('Mock decoder result is not configured.');
        return result;
    },
}));

vi.mock('@/utils', async (import_original) => {
    const original = await import_original<typeof import('@/utils')>();
    return {
        ...original,
        PlayerUtils: {
            ...original.PlayerUtils,
            extractLiveAPIQualityFromDPlayer: () => '1080p',
            extractKonomiTVBS4KLivePlaybackCodecQueryFromDPlayer: () => '?video_codec=hevc',
        },
    };
});


type WorkerMessageCallback = (message: unknown) => void | Promise<void>;


function createDecoder() {
    let callback: WorkerMessageCallback | null = null;
    return {
        destroy: vi.fn(),
        run: vi.fn((worker_callback: WorkerMessageCallback) => {
            callback = worker_callback;
        }),
        get callback(): WorkerMessageCallback {
            if (callback === null) throw new Error('Worker callback is not registered.');
            return callback;
        },
    };
}


class MockResizeObserver {

    public static readonly instances: MockResizeObserver[] = [];

    public readonly observe = vi.fn();
    public readonly disconnect = vi.fn();

    public constructor(private readonly callback: ResizeObserverCallback) {
        MockResizeObserver.instances.push(this);
    }

    public emit(width: number, height: number): void {
        this.callback([{
            contentRect: {width, height},
        } as ResizeObserverEntry], this as unknown as ResizeObserver);
    }
}


function setCurrentChannels(): void {
    const channels_store = useChannelsStore();
    const current = Object.preventExtensions({
        ...structuredClone(ILiveChannelDefault),
        id: 'NID1-SID1',
        display_channel_id: 'gr011',
        network_id: 1,
        service_id: 1,
        remocon_id: 1,
        channel_number: '011',
    });
    const another = Object.preventExtensions({
        ...structuredClone(ILiveChannelDefault),
        id: 'NID2-SID2',
        display_channel_id: 'gr021',
        network_id: 2,
        service_id: 2,
        remocon_id: 2,
        channel_number: '021',
    });
    channels_store.channels_list.GR = [current, another];
    channels_store.is_channels_list_initial_updated = true;
    channels_store.display_channel_id = current.display_channel_id;
}


function createManager() {
    const video_wrap = document.createElement('div');
    const video_wrap_aspect = document.createElement('div');
    video_wrap.append(video_wrap_aspect);

    const remote_control = document.createElement('div');
    remote_control.classList.add('remote-control');
    const data_broadcasting = document.createElement('div');
    data_broadcasting.classList.add('remote-control-data-broadcasting');
    const button = document.createElement('button');
    button.dataset.aribKeyCode = '1';
    data_broadcasting.append(button);
    remote_control.append(data_broadcasting);
    document.body.append(video_wrap, remote_control);

    const player = {
        template: {
            videoWrap: video_wrap,
            videoWrapAspect: video_wrap_aspect,
        },
        notice: vi.fn(),
    };
    return {
        manager: new LiveDataBroadcastingManager(player as never),
        player,
        video_wrap,
        video_wrap_aspect,
    };
}


describe('LiveDataBroadcastingManager lifecycle generation', () => {
    beforeEach(() => {
        vi.useFakeTimers();
        localStorage.clear();
        sessionStorage.clear();
        document.body.replaceChildren();
        setActivePinia(createPinia());
        setCurrentChannels();
        mocks.MockBMLBrowser.instances.length = 0;
        mocks.proxy_results.length = 0;
        mocks.proxy_constructor.mockClear();
        mocks.router_push.mockClear();
        MockResizeObserver.instances.length = 0;
        vi.stubGlobal('ResizeObserver', MockResizeObserver);
    });

    afterEach(() => {
        vi.useRealTimers();
        vi.unstubAllGlobals();
    });

    it('Proxy 生成待ち中に破棄された旧 init の Worker を即時破棄する', async () => {
        useSettingsStore().settings.tv_show_data_broadcasting = false;
        const deferred = createDeferred<ReturnType<typeof createDecoder>>();
        const decoder = createDecoder();
        mocks.proxy_results.push(deferred.promise);
        const {manager} = createManager();

        const initializing = manager.init();
        await manager.destroy();
        deferred.resolve(decoder);
        await initializing;

        expect(decoder.destroy).toHaveBeenCalledTimes(1);
        expect(decoder.run).not.toHaveBeenCalled();
    });

    it('旧 Worker の Comlink callback を新世代の番組 Store へ反映しない', async () => {
        useSettingsStore().settings.tv_show_data_broadcasting = false;
        const old_decoder = createDecoder();
        const current_decoder = createDecoder();
        mocks.proxy_results.push(old_decoder, current_decoder);
        const {manager} = createManager();
        const channels_store = useChannelsStore();

        await manager.init();
        await manager.destroy();
        await manager.init();

        const old_program = {...structuredClone(IProgramDefault), id: 100};
        await old_decoder.callback({
            type: 'IProgramPF',
            present_or_following: 'Present',
            program: old_program,
        });
        expect(channels_store.current_program_present).toBeNull();

        const current_program = {...structuredClone(IProgramDefault), id: 200};
        await current_decoder.callback({
            type: 'IProgramPF',
            present_or_following: 'Present',
            program: current_program,
        });
        expect(channels_store.current_program_present?.id).toBe(200);
    });

    it('旧 BML/ResizeObserver/遅延選局が新世代の DOM と routing を変更しない', async () => {
        useSettingsStore().settings.tv_show_data_broadcasting = true;
        mocks.proxy_results.push(createDecoder(), createDecoder());
        const {manager, player, video_wrap_aspect} = createManager();

        await manager.init();
        const old_bml_browser = mocks.MockBMLBrowser.instances[0];
        const old_resize_observer = MockResizeObserver.instances[0];
        const epg = old_bml_browser.options.epg as {
            tune: (network_id: number, transport_stream_id: number, service_id: number) => boolean;
        };
        expect(epg.tune(2, 0, 2)).toBe(true);
        expect(epg.tune(999, 0, 999)).toBe(false);

        await manager.destroy();
        await manager.init();
        const current_bml_browser = mocks.MockBMLBrowser.instances[1];
        const current_container = player.template.videoWrap.querySelector<HTMLElement>('.dplayer-bml-browser')!;

        old_bml_browser.emit('load', {resolution: {width: 720, height: 480}});
        old_resize_observer.emit(123, 456);
        expect(video_wrap_aspect.parentElement).toBe(player.template.videoWrap);
        expect(current_bml_browser.video_element.contains(video_wrap_aspect)).toBe(false);
        expect(current_container.style.getPropertyValue('--bml-browser-scale-factor-width')).toBe('');

        await vi.advanceTimersByTimeAsync(3_000);
        expect(mocks.router_push).not.toHaveBeenCalled();
        expect(mocks.proxy_constructor).toHaveBeenCalledTimes(2);
    });

    it('現世代 BML の同一チャンネル選局は destroy 後に一度だけ再初期化する', async () => {
        useSettingsStore().settings.tv_show_data_broadcasting = true;
        mocks.proxy_results.push(createDecoder(), createDecoder());
        const {manager} = createManager();

        await manager.init();
        const bml_browser = mocks.MockBMLBrowser.instances[0];
        const epg = bml_browser.options.epg as {
            tune: (network_id: number, transport_stream_id: number, service_id: number) => boolean;
        };
        expect(epg.tune(1, 0, 1)).toBe(true);
        for (let index = 0; index < 5; index++) await Promise.resolve();

        expect(bml_browser.destroy).toHaveBeenCalledTimes(1);
        expect(mocks.proxy_constructor).toHaveBeenCalledTimes(2);
        expect(mocks.MockBMLBrowser.instances).toHaveLength(2);
    });
});
