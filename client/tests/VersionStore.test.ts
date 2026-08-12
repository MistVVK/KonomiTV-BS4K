import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import Version, { IVersionInformation } from '@/services/Version';
import useVersionStore from '@/stores/VersionStore';


function createVersionInfo(overrides: Partial<IVersionInformation> = {}): IVersionInformation {
    return {
        version: '1.1.2',
        upstream_version: '0.14.1',
        latest_version: '1.1.2',
        git_commit: 'test',
        environment: 'Linux-Docker',
        backend: 'EDCB',
        encoder: 'FFmpeg',
        encoder_bs4k: 'FFmpeg',
        bs4k_ignore_viewer_low_latency: true,
        jikkyo_enabled: false,
        ...overrides,
    };
}


describe('VersionStore', () => {

    beforeEach(() => {
        setActivePinia(createPinia());
    });


    it('通常の取得失敗を隠さない設定で Version Service を呼ぶ', async () => {
        const fetch_server_version = vi.spyOn(Version, 'fetchServerVersion').mockResolvedValue(null);
        const store = useVersionStore();
        const abort_controller = new AbortController();

        await expect(store.fetchServerVersion(true, abort_controller.signal)).resolves.toBeNull();
        expect(fetch_server_version).toHaveBeenCalledWith(false, abort_controller.signal);
        expect(store.is_server_version_fetch_failed).toBe(true);
    });


    it.each([
        // ベース版が同じなら -dev の有無に関わらず更新なし
        { version: '1.1.2', latest_version: '1.1.2', expected: false },
        { version: '1.1.2-dev', latest_version: '1.1.2', expected: false },
        { version: '1.1.2', latest_version: '1.1.2-dev', expected: false },
        { version: '1.1.2-dev', latest_version: '1.1.2-dev', expected: false },
        // ベース版が違うときだけ更新あり
        { version: '1.1.1', latest_version: '1.1.2', expected: true },
        { version: '1.1.1-dev', latest_version: '1.1.2', expected: true },
        { version: '1.1.2-dev', latest_version: '1.1.3', expected: true },
    ])('is_update_available はベース版番号だけで判定する (version=$version latest=$latest_version)', ({
        version,
        latest_version,
        expected,
    }) => {
        const store = useVersionStore();
        store.server_version_info = createVersionInfo({ version, latest_version });
        expect(store.is_update_available).toBe(expected);
    });


    it('version または latest_version が未取得なら更新なし', () => {
        const store = useVersionStore();
        expect(store.is_update_available).toBe(false);

        store.server_version_info = createVersionInfo({ latest_version: null });
        expect(store.is_update_available).toBe(false);
    });
});
