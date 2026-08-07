import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';


const playback_source = readFileSync(resolve(process.cwd(), 'src/views/Settings/Playback.vue'), 'utf-8');
const settings_routes_source = readFileSync(
    resolve(process.cwd(), 'src/router/settings.ts'),
    'utf-8',
);


describe('再生・画質設定ルート契約', () => {

    it('正規の画質設定ルートが共通Playbackコンポーネントへ到達する', () => {
        expect(settings_routes_source).toContain('path: \'/settings/personal/quality\'');
        expect(settings_routes_source).toContain('component: () => import(\'@/views/Settings/Playback.vue\')');
    });

    it('公開 runtime 情報を先に取得してから能力表示を解決する', () => {
        const version_fetch_index = playback_source.indexOf(
            'await versionStore.fetchServerVersion(true)',
        );
        const capabilities_fetch_index = playback_source.indexOf(
            'await Videos.fetchKonomiTVBS4KPlaybackCapabilities()',
        );
        expect(version_fetch_index).toBeGreaterThan(-1);
        expect(capabilities_fetch_index).toBeGreaterThan(version_fetch_index);
    });

    it('ライブと録画の実効候補を別々の能力で解決して表示する', () => {
        expect(playback_source).toContain('ライブ実効候補:');
        expect(playback_source).toContain('録画実効候補:');
        expect(playback_source).toContain('playback_profile.value,\n        \'Live\',');
        expect(playback_source).toContain('playback_profile.value,\n        \'Video\',');
    });

    it('BS4Kでは24fpsスイッチを表示・保存せず、60p適用外を明示する', () => {
        expect(playback_source).toContain('v-if="is_bs4k === true" class="settings__item settings__item--sync-disabled"');
        expect(playback_source).toContain('v-else class="settings__item settings__item--switch settings__item--sync-disabled"');
        expect(playback_source).toContain('BS4K は 60p プログレッシブ放送のため');
        expect(playback_source).toContain('if (is_bs4k.value === true) return;');
    });

    it('低遅延スイッチは通常放送とBS4Kの両方に表示し、サーバー強制時だけ無効化する', () => {
        const low_latency_heading_index = playback_source.indexOf('ライブ視聴を低遅延にする');
        const low_latency_switch_index = playback_source.indexOf(':model-value="selected_low_latency"');
        expect(low_latency_heading_index).toBeGreaterThan(-1);
        expect(low_latency_switch_index).toBeGreaterThan(low_latency_heading_index);
        expect(playback_source.slice(low_latency_heading_index - 200, low_latency_heading_index)).not.toContain(
            'v-if="is_bs4k === false"',
        );
        expect(playback_source).toContain(':disabled="is_bs4k_low_latency_forced"');
        expect(playback_source).toContain('if (is_bs4k_low_latency_forced.value === true) return;');
        expect(playback_source).toContain('patchSetting(key, value === true);');
    });
});
