import { describe, expect, it } from 'vitest';

import type DPlayer from 'dplayer';

import { PlayerUtils } from '@/utils';


describe('KonomiTV-BS4K live URL helper', () => {

    it('mpegts・events・PSIへ同じ正規順codec tupleを伝播する', () => {
        const query = PlayerUtils.buildKonomiTVBS4KLivePlaybackCodecQuery({
            video_codec: 'av1',
            video_bit_depth: 10,
            audio_codec: 'opus',
            use_rain_fallback: true,
        });
        expect(query).toBe('video_codec=av1&video_bit_depth=10&audio_codec=opus&use_rain_fallback=1');

        const mpegts_url = PlayerUtils.buildKonomiTVBS4KLiveAPIEndpointURL(
            'bs4k101', '1080p-60fps-10bit', 'mpegts', query,
        );
        const player = {
            quality: {url: mpegts_url},
        } as unknown as DPlayer;

        expect(PlayerUtils.extractKonomiTVBS4KLivePlaybackCodecQueryFromDPlayer(player)).toBe(query);
        for (const endpoint of ['mpegts', 'events', 'psi-archived-data'] as const) {
            expect(PlayerUtils.buildKonomiTVBS4KLiveAPIEndpointURLFromDPlayer(
                player,
                'bs4k101',
                endpoint,
            )).toBe(
                PlayerUtils.buildKonomiTVBS4KLiveAPIEndpointURL(
                    'bs4k101',
                    '1080p-60fps-10bit',
                    endpoint,
                    query,
                ),
            );
        }
    });
});
