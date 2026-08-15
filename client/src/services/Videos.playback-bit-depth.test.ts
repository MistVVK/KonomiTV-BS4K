import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { IKonomiTVBS4KPlaybackVideoProfile } from '@/stores/SettingsStore';

import APIClient from '@/services/APIClient';
import Videos, { type IKonomiTVBS4KPlaybackCapabilities } from '@/services/Videos';
import Utils, { PlayerUtils } from '@/utils';


vi.mock('@/services/APIClient', () => ({
    default: {
        get: vi.fn(),
    },
}));


describe('録画再生の AV1 bit depth 選択', () => {
    const profile: IKonomiTVBS4KPlaybackVideoProfile = {
        is_bs4k: false,
        streaming_quality: '1080p',
    };
    const capabilities: IKonomiTVBS4KPlaybackCapabilities = {
        video: [10, 8].map(bit_depth => ({
            encoder: 'QSV' as const,
            codec: 'av1' as const,
            bit_depth: bit_depth as 8 | 10,
            profile: 'Main',
            live_available: true,
            recorded_available: true,
            live_reason_code: null,
            recorded_reason_code: null,
        })),
        audio: [{
            codec: 'opus',
            live_available: true,
            recorded_available: true,
            live_reason_code: null,
            recorded_reason_code: null,
        }],
        live_combinations: [10, 8].map(video_bit_depth => ({
            encoder: 'QSV' as const,
            video_codec: 'av1' as const,
            video_bit_depth: video_bit_depth as 8 | 10,
            audio_codec: 'opus' as const,
            available: true,
            reason_code: null,
        })),
    };

    beforeEach(() => {
        vi.mocked(APIClient.get).mockReset();
        vi.mocked(APIClient.get).mockResolvedValue({
            type: 'success',
            status: 200,
            headers: {},
            data: capabilities,
        });
        vi.spyOn(PlayerUtils, 'isKonomiTVBS4KPlaybackVideoCodecSupported').mockReturnValue(true);
        vi.spyOn(PlayerUtils, 'isKonomiTVBS4KPlaybackAudioCodecSupported').mockReturnValue(true);
        vi.spyOn(Utils, 'isChromium').mockReturnValue(true);
        vi.spyOn(Utils, 'isDesktopLinux').mockReturnValue(true);
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('Linux Chromium の録画再生では AV1 8bit を先に選ぶ', async () => {
        const result = await Videos.preflightKonomiTVBS4KPlaybackProfile(
            'QSV',
            'av1',
            'opus',
            profile,
            'Video',
            true,
        );

        expect(result).toMatchObject({
            video_codec: 'av1',
            video_bit_depth: 8,
            audio_codec: 'opus',
        });
        expect(APIClient.get).toHaveBeenCalledWith(
            '/streams/video/konomitv-bs4k-playback-capabilities/targeted',
            expect.objectContaining({
                params: expect.objectContaining({video_bit_depths: '8,10'}),
            }),
        );
    });

    it('Chromium 以外の録画再生では従来どおり AV1 10bit を先に選ぶ', async () => {
        vi.mocked(Utils.isChromium).mockReturnValue(false);

        const result = await Videos.preflightKonomiTVBS4KPlaybackProfile(
            'QSV',
            'av1',
            'opus',
            profile,
            'Video',
            true,
        );

        expect(result).toMatchObject({video_codec: 'av1', video_bit_depth: 10, audio_codec: 'opus'});
        expect(APIClient.get).toHaveBeenCalledWith(
            '/streams/video/konomitv-bs4k-playback-capabilities/targeted',
            expect.objectContaining({
                params: expect.objectContaining({video_bit_depths: '10,8'}),
            }),
        );
    });

    it('Linux Chromium でもライブ再生の AV1 10bit 優先は変更しない', async () => {
        const result = await Videos.preflightKonomiTVBS4KPlaybackProfile(
            'QSV',
            'av1',
            'opus',
            profile,
            'Live',
            true,
        );

        expect(result).toMatchObject({video_codec: 'av1', video_bit_depth: 10, audio_codec: 'opus'});
    });
});
