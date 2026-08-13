import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { IOfflineVideo } from '@/services/OfflineVideos';

import OfflineVideosView from '@/views/OfflineVideos.vue';

const offlineVideosMocks = vi.hoisted(() => ({
    eventTarget: new EventTarget(),
    getVideos: vi.fn(),
    getJobs: vi.fn(),
    cancel: vi.fn(),
    dismissJob: vi.fn(),
}));

vi.mock('@/services/OfflineVideos', () => ({
    default: offlineVideosMocks,
}));

let originalStorageDescriptor: PropertyDescriptor | undefined;

beforeEach(() => {
    vi.clearAllMocks();
    originalStorageDescriptor = Object.getOwnPropertyDescriptor(navigator, 'storage');
});

afterEach(() => {
    if (originalStorageDescriptor !== undefined) {
        Object.defineProperty(navigator, 'storage', originalStorageDescriptor);
    } else {
        Reflect.deleteProperty(navigator, 'storage');
    }
});

describe('オフライン保存一覧の更新通知', () => {

    it('読み取り中に届いた保存確定通知を捨てず、確定後の一覧を再読込する', async () => {
        let currentVideos: IOfflineVideo[] = [];
        const estimate = vi.fn().mockResolvedValue({usage: 0, quota: 1024});
        Object.defineProperty(navigator, 'storage', {
            configurable: true,
            value: {estimate},
        });
        offlineVideosMocks.getVideos.mockImplementation(async () => currentVideos);
        offlineVideosMocks.getJobs.mockResolvedValue([]);

        const wrapper = mount(OfflineVideosView, {
            global: {
                stubs: {
                    Breadcrumbs: true,
                    HeaderBar: true,
                    Icon: true,
                    Navigation: true,
                    SPHeaderBar: true,
                    RecordedProgramList: {
                        props: ['programs'],
                        template: '<div data-test="program-count">{{programs.length}}</div>',
                    },
                },
            },
        });
        await flushPromises();
        expect(wrapper.get('[data-test="program-count"]').text()).toBe('0');

        // 2回目の refresh を容量取得で止め、その間に保存済み動画と change 通知を到着させる。
        let releaseEstimate: (() => void) | undefined;
        const blockedEstimate = new Promise<void>((resolve) => {
            releaseEstimate = resolve;
        });
        estimate.mockImplementationOnce(async () => {
            await blockedEstimate;
            return {usage: 0, quota: 1024};
        });
        offlineVideosMocks.eventTarget.dispatchEvent(new Event('change'));
        await vi.waitFor(() => expect(estimate).toHaveBeenCalledTimes(2));

        currentVideos = [{
            video_id: 177,
            size_bytes: 1,
            program: {},
        } as IOfflineVideo];
        offlineVideosMocks.eventTarget.dispatchEvent(new Event('change'));
        releaseEstimate?.();

        await vi.waitFor(() => expect(wrapper.get('[data-test="program-count"]').text()).toBe('1'));
        wrapper.unmount();
    });

});
