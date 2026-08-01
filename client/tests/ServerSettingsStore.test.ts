import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { createDeferred } from './helpers/deferred';

import Settings, { type IServerSettings, IServerSettingsDefault } from '@/services/Settings';
import useServerSettingsStore from '@/stores/ServerSettingsStore';


describe('ServerSettingsStore lifecycle', () => {

    beforeEach(() => {
        setActivePinia(createPinia());
    });


    it('破棄済み caller の完了では store を更新しない', async () => {
        const deferred = createDeferred<IServerSettings | null>();
        vi.spyOn(Settings, 'fetchServerSettings').mockReturnValue(deferred.promise);
        const store = useServerSettingsStore();
        const abort_controller = new AbortController();

        const fetch_promise = store.fetchServerSettingsOnce(abort_controller.signal);
        abort_controller.abort();
        deferred.resolve(structuredClone(IServerSettingsDefault));

        expect(await fetch_promise).toBeNull();
        expect(store.is_loaded).toBe(false);
    });


    it('共有 fetch を待つ生存 caller だけが store を hydrate する', async () => {
        const deferred = createDeferred<IServerSettings | null>();
        vi.spyOn(Settings, 'fetchServerSettings').mockReturnValue(deferred.promise);
        const store = useServerSettingsStore();
        const old_caller = new AbortController();
        const current_caller = new AbortController();

        const old_fetch = store.fetchServerSettingsOnce(old_caller.signal);
        const current_fetch = store.fetchServerSettingsOnce(current_caller.signal);
        old_caller.abort();
        deferred.resolve(structuredClone(IServerSettingsDefault));

        expect(await old_fetch).toBeNull();
        expect(await current_fetch).toEqual(IServerSettingsDefault);
        expect(store.is_loaded).toBe(true);
        expect(Settings.fetchServerSettings).toHaveBeenCalledTimes(1);
    });


    it('取得した対象パスをホスト絶対パスのまま保持する', async () => {
        const settings = structuredClone(IServerSettingsDefault);
        settings.server.https_mode = 'certificate';
        settings.server.custom_https_certificate = '/mnt/Certificates/server.crt';
        settings.server.custom_https_private_key = '/mnt/Certificates/server.key';
        settings.compatibility_api.enabled = true;
        settings.compatibility_api.https_mode = 'certificate';
        settings.compatibility_api.custom_https_certificate = '/mnt/Certificates/compatibility.crt';
        settings.compatibility_api.custom_https_private_key = '/mnt/Certificates/compatibility.key';
        settings.tv.debug_mode_ts_path = '/mnt/Test/debug.ts';
        settings.video.recorded_folders = ['/mnt/TV-Record'];
        settings.video.exclude_scan_paths = ['/mnt/TV-Record/host-rootfs/Temp'];
        settings.video.recorded_fmp4_cache_folder = '/mnt/KonomiTV-Cache';
        settings.capture.upload_folders = ['/mnt/TV-Capture'];
        vi.spyOn(Settings, 'fetchServerSettings').mockResolvedValue(settings);
        const store = useServerSettingsStore();

        const fetched = await store.fetchServerSettingsOnce();

        expect(fetched).toEqual(settings);
        const path_values = [
            ...store.server_settings.video.recorded_folders,
            ...store.server_settings.video.exclude_scan_paths,
            store.server_settings.video.recorded_fmp4_cache_folder,
            ...store.server_settings.capture.upload_folders,
            store.server_settings.tv.debug_mode_ts_path,
            store.server_settings.server.custom_https_certificate,
            store.server_settings.server.custom_https_private_key,
            store.server_settings.compatibility_api.custom_https_certificate,
            store.server_settings.compatibility_api.custom_https_private_key,
        ].filter((path): path is string => path !== null);
        expect(path_values.every(path => path.startsWith('/host-rootfs') === false)).toBe(true);
        expect(store.server_settings.video.exclude_scan_paths).toEqual(['/mnt/TV-Record/host-rootfs/Temp']);
    });


    it('対象パスをホスト絶対パスのまま更新APIへ送る', async () => {
        const settings = structuredClone(IServerSettingsDefault);
        settings.video.recorded_folders = ['/mnt/TV-Record'];
        settings.video.exclude_scan_paths = ['/mnt/TV-Record/Temp'];
        settings.video.recorded_fmp4_cache_folder = '/mnt/KonomiTV-Cache';
        settings.capture.upload_folders = ['/mnt/TV-Capture'];
        const update = vi.spyOn(Settings, 'updateServerSettings').mockResolvedValue(true);
        const store = useServerSettingsStore();

        expect(await store.updateServerSettings(settings)).toBe(true);

        expect(update).toHaveBeenCalledWith(expect.objectContaining({
            video: expect.objectContaining({
                recorded_folders: ['/mnt/TV-Record'],
                exclude_scan_paths: ['/mnt/TV-Record/Temp'],
                recorded_fmp4_cache_folder: '/mnt/KonomiTV-Cache',
            }),
            capture: {
                upload_folders: ['/mnt/TV-Capture'],
            },
        }));
    });
});
