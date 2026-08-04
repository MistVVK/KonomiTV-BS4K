import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import Version from '@/services/Version';
import useVersionStore from '@/stores/VersionStore';


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
});
