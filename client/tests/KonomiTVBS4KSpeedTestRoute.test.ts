import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { SETTINGS_NAVIGATION_CATEGORIES, SETTINGS_ROUTES } from '@/router/settings';
import KonomiTVBS4KSpeedTest from '@/services/KonomiTVBS4KSpeedTest';
import useUserStore from '@/stores/UserStore';
import KonomiTVBS4KSpeedTestView from '@/views/Settings/KonomiTVBS4KSpeedTest.vue';


const clientRoot = dirname(dirname(fileURLToPath(import.meta.url)));

vi.mock('@/services/KonomiTVBS4KSpeedTest', () => ({
    default: {
        createSession: vi.fn(),
        deleteSession: vi.fn(),
    },
}));


describe('サーバー接続速度の route とナビ', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        setActivePinia(createPinia());
        localStorage.clear();
    });

    it('情報カテゴリの先頭に正規 route を置く', () => {
        const info = SETTINGS_NAVIGATION_CATEGORIES.find(category => category.label === '情報');
        expect(info).toBeDefined();
        expect(info!.items[0]).toMatchObject({
            type: 'Route',
            label: 'サーバー接続速度',
            to: '/settings/info/speed-test',
        });
        expect(SETTINGS_ROUTES.some(route => route.path === '/settings/info/speed-test')).toBe(true);
        expect(SETTINGS_ROUTES.some(route => route.path === '/speedtest')).toBe(false);
    });

    it('未ログイン時は測定ボタンを出さずログイン導線を出す', async () => {
        const user_store = useUserStore();
        user_store.fetchUser = vi.fn().mockResolvedValue(null);
        user_store.user = null;
        const wrapper = mount(KonomiTVBS4KSpeedTestView, {
            global: {
                stubs: {
                    SettingsBase: {template: '<div><slot /></div>'},
                    Icon: true,
                    'v-btn': {template: '<a><slot /></a>', props: ['to']},
                    'v-progress-linear': true,
                },
            },
        });
        await flushPromises();
        expect(wrapper.text()).toContain('ログインが必要です');
        expect(wrapper.text()).not.toContain('測定開始');
        wrapper.unmount();
    });

    it('ユーザー取得中に unmount しても pagehide listener を後から登録しない', async () => {
        const user_store = useUserStore();
        let resolveFetchUser!: (value: null) => void;
        user_store.fetchUser = vi.fn().mockImplementation(() => new Promise<null>((resolve) => {
            resolveFetchUser = resolve;
        }));
        user_store.user = null;
        const addEventListener = vi.spyOn(window, 'addEventListener');
        const wrapper = mount(KonomiTVBS4KSpeedTestView, {
            global: {
                stubs: {
                    SettingsBase: {template: '<div><slot /></div>'},
                    Icon: true,
                    'v-btn': {template: '<a><slot /></a>', props: ['to']},
                    'v-progress-linear': true,
                },
            },
        });
        wrapper.unmount();
        resolveFetchUser(null);
        await flushPromises();
        expect(addEventListener.mock.calls.some(([type]) => type === 'pagehide')).toBe(false);
        addEventListener.mockRestore();
    });

    it('pagehide でこのタブの Worker と session 所有状態を終了する', async () => {
        const user_store = useUserStore();
        user_store.user = {id: 1} as NonNullable<typeof user_store.user>;
        user_store.fetchUser = vi.fn().mockResolvedValue(user_store.user);
        vi.mocked(KonomiTVBS4KSpeedTest.createSession).mockResolvedValue({
            expires_in_seconds: 90,
            limits: {
                download_streams: 5,
                upload_streams: 3,
                download_seconds: 10,
                upload_seconds: 10,
            },
            quality_thresholds: [],
        });
        vi.mocked(KonomiTVBS4KSpeedTest.deleteSession).mockResolvedValue();
        const fetchMock = vi.fn().mockResolvedValue(new Response(null, {status: 204}));
        vi.stubGlobal('fetch', fetchMock);
        vi.stubGlobal('Worker', class {
            onmessage: ((event: MessageEvent<string>) => void) | null = null;
            onerror: (() => void) | null = null;
            postMessage(): void {}
            terminate(): void {}
        });
        localStorage.setItem('KonomiTV-AccessToken', 'test-token');

        const wrapper = mount(KonomiTVBS4KSpeedTestView, {
            global: {
                stubs: {
                    SettingsBase: {template: '<div><slot /></div>'},
                    Icon: true,
                    'v-btn': {template: '<button><slot /></button>'},
                    'v-progress-linear': true,
                },
            },
        });
        await flushPromises();
        const startButton = wrapper.findAll('button').find(button => button.text().includes('測定開始'));
        expect(startButton).toBeDefined();
        await startButton!.trigger('click');
        await flushPromises();

        window.dispatchEvent(new Event('pagehide'));
        await flushPromises();
        expect(fetchMock).toHaveBeenCalledTimes(1);
        expect(wrapper.text()).toContain('測定をキャンセルしました');
        wrapper.unmount();
        expect(fetchMock).toHaveBeenCalledTimes(1);
        vi.unstubAllGlobals();
    });

    it('LibreSpeed vendor を PWA precache から除外する', () => {
        const viteConfig = readFileSync(join(clientRoot, 'vite.config.mts'), 'utf8');
        expect(viteConfig).toContain('globIgnores: [\'**/vendor/librespeed/**\']');
    });
});
