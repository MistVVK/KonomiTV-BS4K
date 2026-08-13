import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import useSettingsStore, {
    getNormalizedLocalClientSettings,
    ILocalClientSettingsDefault,
    migrateKonomiTVBS4KPlaybackProfileSettings,
} from '@/stores/SettingsStore';


const KONOMITV_BS4K_PLAYBACK_PROFILE_KEYS = [
    'konomitv_bs4k_playback_streaming_quality',
    'konomitv_bs4k_playback_streaming_quality_cellular',
    'konomitv_bs4k_playback_streaming_quality_for_bs4k',
    'konomitv_bs4k_playback_streaming_quality_for_bs4k_cellular',
    'konomitv_bs4k_playback_video_codec',
    'konomitv_bs4k_playback_video_codec_cellular',
    'konomitv_bs4k_playback_video_codec_for_bs4k',
    'konomitv_bs4k_playback_video_codec_for_bs4k_cellular',
    'konomitv_bs4k_playback_audio_codec',
    'konomitv_bs4k_playback_audio_codec_cellular',
    'konomitv_bs4k_playback_audio_codec_for_bs4k',
    'konomitv_bs4k_playback_audio_codec_for_bs4k_cellular',
    'konomitv_bs4k_playback_24fps_mode',
    'konomitv_bs4k_playback_24fps_mode_cellular',
    'konomitv_bs4k_playback_24fps_mode_for_bs4k',
    'konomitv_bs4k_playback_24fps_mode_for_bs4k_cellular',
    'konomitv_bs4k_playback_profile_migration_conflict_notice_pending',
] as const;

const PROFILE_PRIORITY_CASES = [
    {
        name: '通常/Wi-Fi画質',
        canonical_key: 'konomitv_bs4k_playback_streaming_quality',
        live_key: 'tv_streaming_quality',
        recorded_key: 'video_streaming_quality',
        live_value: '720p',
        recorded_value: '540p',
    },
    {
        name: '通常/モバイル映像',
        canonical_key: 'konomitv_bs4k_playback_video_codec_cellular',
        live_key: 'tv_encoding_codec_cellular',
        recorded_key: 'video_encoding_codec_cellular',
        live_value: 'avc',
        recorded_value: 'av1',
    },
    {
        name: 'BS4K/Wi-Fi画質',
        canonical_key: 'konomitv_bs4k_playback_streaming_quality_for_bs4k',
        live_key: 'bs4k_streaming_quality',
        recorded_key: 'bs4k_video_streaming_quality',
        live_value: '2160p',
        recorded_value: '1440p',
    },
    {
        name: 'BS4K/モバイル映像',
        canonical_key: 'konomitv_bs4k_playback_video_codec_for_bs4k_cellular',
        live_key: 'bs4k_tv_encoding_codec_cellular',
        recorded_key: 'bs4k_video_encoding_codec_cellular',
        live_value: 'avc',
        recorded_value: 'vp9',
    },
] as const;

const CANDIDATE_KEY_CASES = [
    ['playback_streaming_quality', 'konomitv_bs4k_playback_streaming_quality', '720p'],
    ['playback_streaming_quality_cellular', 'konomitv_bs4k_playback_streaming_quality_cellular', '540p'],
    ['bs4k_playback_streaming_quality', 'konomitv_bs4k_playback_streaming_quality_for_bs4k', '2160p'],
    [
        'bs4k_playback_streaming_quality_cellular',
        'konomitv_bs4k_playback_streaming_quality_for_bs4k_cellular',
        '720p-30fps',
    ],
    ['playback_video_codec', 'konomitv_bs4k_playback_video_codec', 'vp9'],
    ['playback_video_codec_cellular', 'konomitv_bs4k_playback_video_codec_cellular', 'av1'],
    ['bs4k_playback_video_codec', 'konomitv_bs4k_playback_video_codec_for_bs4k', 'av1'],
    ['bs4k_playback_video_codec_cellular', 'konomitv_bs4k_playback_video_codec_for_bs4k_cellular', 'vp9'],
    ['playback_audio_codec', 'konomitv_bs4k_playback_audio_codec', 'opus'],
    ['playback_audio_codec_cellular', 'konomitv_bs4k_playback_audio_codec_cellular', 'opus'],
    ['bs4k_playback_audio_codec', 'konomitv_bs4k_playback_audio_codec_for_bs4k', 'opus'],
    ['bs4k_playback_audio_codec_cellular', 'konomitv_bs4k_playback_audio_codec_for_bs4k_cellular', 'opus'],
    ['playback_24fps_mode', 'konomitv_bs4k_playback_24fps_mode', true],
    ['playback_24fps_mode_cellular', 'konomitv_bs4k_playback_24fps_mode_cellular', true],
    ['bs4k_playback_24fps_mode', 'konomitv_bs4k_playback_24fps_mode_for_bs4k', true],
    ['bs4k_playback_24fps_mode_cellular', 'konomitv_bs4k_playback_24fps_mode_for_bs4k_cellular', true],
    [
        'playback_profile_migration_conflict_notice_pending',
        'konomitv_bs4k_playback_profile_migration_conflict_notice_pending',
        true,
    ],
] as const;


function createLegacySettings(): Record<string, unknown> {
    const settings: Record<string, unknown> = structuredClone(ILocalClientSettingsDefault);
    for (const key of KONOMITV_BS4K_PLAYBACK_PROFILE_KEYS) {
        delete settings[key];
    }
    return settings;
}


describe('共通再生プロファイル移行', () => {
    beforeEach(() => {
        localStorage.clear();
        setActivePinia(createPinia());
    });

    it('片方だけ既定値から変更されている場合は変更側を採用する', () => {
        const settings = createLegacySettings();
        settings.video_streaming_quality = '720p';
        settings.tv_encoding_codec = 'hevc';

        const migrated = migrateKonomiTVBS4KPlaybackProfileSettings(settings);

        expect(migrated.konomitv_bs4k_playback_streaming_quality).toBe('720p');
        expect(migrated.konomitv_bs4k_playback_video_codec).toBe('hevc');
        expect(migrated.konomitv_bs4k_playback_profile_migration_conflict_notice_pending).toBeUndefined();
    });

    it('旧ライブ・録画の両方が競合していれば旧ライブ側を採用して通知を一度だけ保留する', () => {
        const settings = createLegacySettings();
        settings.tv_streaming_quality = '720p';
        settings.video_streaming_quality = '540p';
        settings.bs4k_tv_encoding_codec = 'avc';
        settings.bs4k_video_encoding_codec = 'av1';

        const migrated = migrateKonomiTVBS4KPlaybackProfileSettings(settings);

        expect(migrated.konomitv_bs4k_playback_streaming_quality).toBe('720p');
        expect(migrated.konomitv_bs4k_playback_video_codec_for_bs4k).toBe('avc');
        expect(migrated.konomitv_bs4k_playback_profile_migration_conflict_notice_pending).toBe(true);
    });

    it.each(PROFILE_PRIORITY_CASES)(
        '$name は旧ライブ・録画が異なる変更値なら旧ライブ側を優先する',
        ({canonical_key, live_key, recorded_key, live_value, recorded_value}) => {
            const settings = createLegacySettings();
            settings[live_key] = live_value;
            settings[recorded_key] = recorded_value;

            const migrated = migrateKonomiTVBS4KPlaybackProfileSettings(settings);

            expect(migrated[canonical_key]).toBe(live_value);
            expect(migrated.konomitv_bs4k_playback_profile_migration_conflict_notice_pending).toBe(true);
        },
    );

    it('通常/BS4KとWi-Fi/モバイルの片側だけを変更しても、他の3領域へ値を漏らさない', () => {
        const settings = createLegacySettings();
        settings.video_streaming_quality_cellular = '720p';
        settings.bs4k_tv_encoding_codec = 'avc';
        settings.video_24fps_mode = true;

        const migrated = migrateKonomiTVBS4KPlaybackProfileSettings(settings);

        expect(migrated.konomitv_bs4k_playback_streaming_quality).toBe('1080p');
        expect(migrated.konomitv_bs4k_playback_streaming_quality_cellular).toBe('720p');
        expect(migrated.konomitv_bs4k_playback_streaming_quality_for_bs4k).toBe('1080p-60fps');
        expect(migrated.konomitv_bs4k_playback_streaming_quality_for_bs4k_cellular).toBe('540p-30fps');
        expect(migrated.konomitv_bs4k_playback_video_codec).toBe('avc');
        expect(migrated.konomitv_bs4k_playback_video_codec_for_bs4k).toBe('avc');
        expect(migrated.konomitv_bs4k_playback_24fps_mode).toBe(true);
        expect(migrated.konomitv_bs4k_playback_24fps_mode_cellular).toBe(false);
        expect(migrated.konomitv_bs4k_playback_24fps_mode_for_bs4k).toBe(false);
        expect(migrated.konomitv_bs4k_playback_24fps_mode_for_bs4k_cellular).toBe(false);
    });

    it('BS4K専用24fps値は通常放送の旧値を複製せず、正規・開発途中キーだけを保持する', () => {
        const legacy_only = createLegacySettings();
        legacy_only.tv_24fps_mode = true;
        legacy_only.video_24fps_mode = true;
        legacy_only.tv_24fps_mode_cellular = true;
        legacy_only.video_24fps_mode_cellular = true;

        const migrated_legacy_only = migrateKonomiTVBS4KPlaybackProfileSettings(legacy_only);
        expect(migrated_legacy_only.konomitv_bs4k_playback_24fps_mode).toBe(true);
        expect(migrated_legacy_only.konomitv_bs4k_playback_24fps_mode_cellular).toBe(true);
        expect(migrated_legacy_only.konomitv_bs4k_playback_24fps_mode_for_bs4k).toBe(false);
        expect(migrated_legacy_only.konomitv_bs4k_playback_24fps_mode_for_bs4k_cellular).toBe(false);

        const candidate_keys = createLegacySettings();
        candidate_keys.bs4k_playback_24fps_mode = true;
        candidate_keys.bs4k_playback_24fps_mode_cellular = true;
        const migrated_candidate_keys = migrateKonomiTVBS4KPlaybackProfileSettings(candidate_keys);
        expect(migrated_candidate_keys.konomitv_bs4k_playback_24fps_mode_for_bs4k).toBe(true);
        expect(migrated_candidate_keys.konomitv_bs4k_playback_24fps_mode_for_bs4k_cellular).toBe(true);

        const canonical_keys = createLegacySettings();
        canonical_keys.konomitv_bs4k_playback_24fps_mode_for_bs4k = true;
        canonical_keys.konomitv_bs4k_playback_24fps_mode_for_bs4k_cellular = true;
        const migrated_canonical_keys = migrateKonomiTVBS4KPlaybackProfileSettings(canonical_keys);
        expect(migrated_canonical_keys.konomitv_bs4k_playback_24fps_mode_for_bs4k).toBe(true);
        expect(migrated_canonical_keys.konomitv_bs4k_playback_24fps_mode_for_bs4k_cellular).toBe(true);
    });

    it.each([
        ['video_audio_encoding_codec', 'konomitv_bs4k_playback_audio_codec'],
        ['video_audio_encoding_codec_cellular', 'konomitv_bs4k_playback_audio_codec_cellular'],
        ['bs4k_video_audio_encoding_codec', 'konomitv_bs4k_playback_audio_codec_for_bs4k'],
        ['bs4k_video_audio_encoding_codec_cellular', 'konomitv_bs4k_playback_audio_codec_for_bs4k_cellular'],
    ] as const)(
        '音声はライブ側に旧設定がないため旧録画キー %s を %s へ採用する',
        (recorded_key, canonical_key) => {
            const settings = createLegacySettings();
            settings[recorded_key] = 'opus';

            const migrated = migrateKonomiTVBS4KPlaybackProfileSettings(settings);

            expect(migrated[canonical_key]).toBe('opus');
        },
    );

    it('新キー作成後は旧キーが変化しても再移行せず通知も再度立てない', () => {
        const settings = createLegacySettings();
        settings.tv_encoding_codec = 'hevc';
        settings.video_encoding_codec = 'av1';
        const first = migrateKonomiTVBS4KPlaybackProfileSettings(settings);
        first.konomitv_bs4k_playback_profile_migration_conflict_notice_pending = false;
        first.tv_encoding_codec = 'avc';
        first.video_encoding_codec = 'vp9';

        const second = migrateKonomiTVBS4KPlaybackProfileSettings(first);

        expect(second.konomitv_bs4k_playback_video_codec).toBe('hevc');
        expect(second.konomitv_bs4k_playback_profile_migration_conflict_notice_pending).toBe(false);
        expect(second).toEqual(first);
    });

    it.each(CANDIDATE_KEY_CASES)(
        '開発途中の汎用キー %s を専用キー %s へ一度だけ引き継ぐ',
        (candidate_key, canonical_key, value) => {
            const settings = createLegacySettings();
            settings[candidate_key] = value;

            const first = migrateKonomiTVBS4KPlaybackProfileSettings(settings);
            expect(first[canonical_key]).toBe(value);

            first[candidate_key] = ILocalClientSettingsDefault[
                canonical_key as keyof typeof ILocalClientSettingsDefault
            ];
            const second = migrateKonomiTVBS4KPlaybackProfileSettings(first);
            expect(second[canonical_key]).toBe(value);
        },
    );

    it('専用キーが存在すれば開発途中キーと旧ライブ・録画キーのどちらでも上書きしない', () => {
        const settings = createLegacySettings();
        settings.konomitv_bs4k_playback_video_codec = 'av1';
        settings.playback_video_codec = 'vp9';
        settings.tv_encoding_codec = 'hevc';
        settings.video_encoding_codec = 'avc';

        const migrated = migrateKonomiTVBS4KPlaybackProfileSettings(settings);

        expect(migrated.konomitv_bs4k_playback_video_codec).toBe('av1');
    });

    it('競合通知はStoreとLocalStorageの双方で一度だけ消費する', () => {
        const settings = createLegacySettings();
        settings.tv_streaming_quality = '720p';
        settings.video_streaming_quality = '540p';
        localStorage.setItem('KonomiTV-Settings', JSON.stringify(settings));
        const settings_store = useSettingsStore();

        expect(settings_store.consumeKonomiTVBS4KPlaybackProfileMigrationConflictNotice()).toBe(true);
        expect(settings_store.consumeKonomiTVBS4KPlaybackProfileMigrationConflictNotice()).toBe(false);

        const stored = JSON.parse(localStorage.getItem('KonomiTV-Settings') ?? '{}');
        expect(stored.konomitv_bs4k_playback_profile_migration_conflict_notice_pending).toBe(false);

        setActivePinia(createPinia());
        expect(useSettingsStore().consumeKonomiTVBS4KPlaybackProfileMigrationConflictNotice()).toBe(false);
    });

    it('ノーマライズ後も旧キーを残し、新共通キーだけを安全な値へ補正する', () => {
        const settings = createLegacySettings();
        settings.video_encoding_codec = 'av1';
        settings.video_audio_encoding_codec = 'opus';

        const normalized = getNormalizedLocalClientSettings(settings);

        expect(normalized.konomitv_bs4k_playback_video_codec).toBe('av1');
        expect(normalized.konomitv_bs4k_playback_audio_codec).toBe('opus');
        expect(normalized.tv_encoding_codec).toBe(ILocalClientSettingsDefault.tv_encoding_codec);
        expect(normalized.video_encoding_codec).toBe('av1');
        expect('playback_video_codec' in normalized).toBe(false);
    });

    it('不正なオフライン保存設定を現行の既定値へ戻す', () => {
        const settings: Record<string, unknown> = structuredClone(ILocalClientSettingsDefault);
        settings.konomitv_bs4k_offline_video_streaming_quality = 'invalid';
        settings.konomitv_bs4k_offline_video_streaming_quality_for_bs4k = null;
        settings.konomitv_bs4k_offline_video_codec = 'mpeg2';
        settings.konomitv_bs4k_offline_video_codec_for_bs4k = 1;
        settings.konomitv_bs4k_offline_audio_codec = 'mp3';
        settings.konomitv_bs4k_offline_audio_codec_for_bs4k = false;
        settings.konomitv_bs4k_offline_video_24fps_mode = 'false';

        const normalized = getNormalizedLocalClientSettings(settings);

        expect(normalized.konomitv_bs4k_offline_video_streaming_quality).toBe(
            ILocalClientSettingsDefault.konomitv_bs4k_offline_video_streaming_quality,
        );
        expect(normalized.konomitv_bs4k_offline_video_streaming_quality_for_bs4k).toBe(
            ILocalClientSettingsDefault.konomitv_bs4k_offline_video_streaming_quality_for_bs4k,
        );
        expect(normalized.konomitv_bs4k_offline_video_codec).toBe(
            ILocalClientSettingsDefault.konomitv_bs4k_offline_video_codec,
        );
        expect(normalized.konomitv_bs4k_offline_video_codec_for_bs4k).toBe(
            ILocalClientSettingsDefault.konomitv_bs4k_offline_video_codec_for_bs4k,
        );
        expect(normalized.konomitv_bs4k_offline_audio_codec).toBe(
            ILocalClientSettingsDefault.konomitv_bs4k_offline_audio_codec,
        );
        expect(normalized.konomitv_bs4k_offline_audio_codec_for_bs4k).toBe(
            ILocalClientSettingsDefault.konomitv_bs4k_offline_audio_codec_for_bs4k,
        );
        expect(normalized.konomitv_bs4k_offline_video_24fps_mode).toBe(
            ILocalClientSettingsDefault.konomitv_bs4k_offline_video_24fps_mode,
        );
    });

    it('有効なオフライン保存設定と旧 BS4K 画質を保持・移行する', () => {
        const settings: Record<string, unknown> = structuredClone(ILocalClientSettingsDefault);
        settings.konomitv_bs4k_offline_video_streaming_quality = '540p';
        settings.konomitv_bs4k_offline_video_streaming_quality_for_bs4k = '720p';
        settings.konomitv_bs4k_offline_video_codec = 'vp9';
        settings.konomitv_bs4k_offline_video_codec_for_bs4k = 'hevc';
        settings.konomitv_bs4k_offline_audio_codec = 'aac';
        settings.konomitv_bs4k_offline_audio_codec_for_bs4k = 'opus';
        settings.konomitv_bs4k_offline_video_24fps_mode = true;

        const normalized = getNormalizedLocalClientSettings(settings);

        expect(normalized.konomitv_bs4k_offline_video_streaming_quality).toBe('540p');
        expect(normalized.konomitv_bs4k_offline_video_streaming_quality_for_bs4k).toBe('720p-30fps');
        expect(normalized.konomitv_bs4k_offline_video_codec).toBe('vp9');
        expect(normalized.konomitv_bs4k_offline_video_codec_for_bs4k).toBe('hevc');
        expect(normalized.konomitv_bs4k_offline_audio_codec).toBe('aac');
        expect(normalized.konomitv_bs4k_offline_audio_codec_for_bs4k).toBe('opus');
        expect(normalized.konomitv_bs4k_offline_video_24fps_mode).toBe(true);
    });
});


describe('共通再生プロファイルのcodec組み合わせ更新', () => {
    beforeEach(() => {
        localStorage.clear();
        setActivePinia(createPinia());
    });

    it.each([
        [
            false,
            false,
            'konomitv_bs4k_playback_video_codec',
            'konomitv_bs4k_playback_audio_codec',
        ],
        [
            false,
            true,
            'konomitv_bs4k_playback_video_codec_cellular',
            'konomitv_bs4k_playback_audio_codec_cellular',
        ],
        [
            true,
            false,
            'konomitv_bs4k_playback_video_codec_for_bs4k',
            'konomitv_bs4k_playback_audio_codec_for_bs4k',
        ],
        [
            true,
            true,
            'konomitv_bs4k_playback_video_codec_for_bs4k_cellular',
            'konomitv_bs4k_playback_audio_codec_for_bs4k_cellular',
        ],
    ] as const)(
        'BS4K=%s・モバイル=%sの映像/音声を1回のmutationで完成済みtupleへ更新する',
        (is_bs4k, is_cellular, video_key, audio_key) => {
            const settings_store = useSettingsStore();
            const observed_codec_pairs: Array<{video: string; audio: string}> = [];
            settings_store.$subscribe((_mutation, state) => {
                observed_codec_pairs.push({
                    video: state.settings[video_key],
                    audio: state.settings[audio_key],
                });
            }, {flush: 'sync'});

            settings_store.updateKonomiTVBS4KPlaybackCodecPair(
                is_bs4k,
                is_cellular,
                'av1',
                'aac',
            );

            expect(observed_codec_pairs).toEqual([{video: 'av1', audio: 'aac'}]);
            expect(settings_store.settings[video_key]).toBe('av1');
            expect(settings_store.settings[audio_key]).toBe('aac');
        },
    );
});
