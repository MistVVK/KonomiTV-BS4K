import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
    AUTO_PLAYBACK_LIVE_AUDIO_CODEC_ORDER,
    AUTO_PLAYBACK_LIVE_VIDEO_CODEC_ORDER,
    AUTO_PLAYBACK_RECORDED_AUDIO_CODEC_ORDER,
    estimateNetworkAllows60fps,
    estimateNetworkHeightCeiling,
    estimateSourceHeightCeiling,
    getNextLowerAutoPlaybackQuality,
    getPlaybackQualityHeight,
    resolveAutoLowLatencyFromNetwork,
    resolveAutoPlaybackQuality,
    resolveEfficiencyFirstPlaybackCombination,
} from '@/services/player/AutoPlaybackProfile';
import Videos from '@/services/Videos';
import { BS4K_LIVE_STREAMING_QUALITIES, LIVE_STREAMING_QUALITIES } from '@/stores/SettingsStore';

vi.mock('@/services/Videos', () => ({
    default: {
        resolveKonomiTVBS4KExactPlaybackCombination: vi.fn(),
    },
}));


describe('AutoPlaybackProfile', () => {
    it('ライブ映像 codec 候補は AV1→VP9→HEVC→AVC の効率優先', () => {
        expect([...AUTO_PLAYBACK_LIVE_VIDEO_CODEC_ORDER]).toEqual([
            'av1',
            'vp9',
            'hevc',
            'avc',
        ]);
    });

    it('自動モードの音声はライブ・録画とも Opus→AAC', () => {
        expect([...AUTO_PLAYBACK_LIVE_AUDIO_CODEC_ORDER]).toEqual(['opus', 'aac']);
        expect([...AUTO_PLAYBACK_RECORDED_AUDIO_CODEC_ORDER]).toEqual(['opus', 'aac']);
    });

    describe('estimateSourceHeightCeiling', () => {
        it('ソース解像度からエンコード上限の高さを返す', () => {
            expect(estimateSourceHeightCeiling(1080)).toBe(1080);
            expect(estimateSourceHeightCeiling(2160)).toBe(2160);
            expect(estimateSourceHeightCeiling(720)).toBe(720);
            expect(estimateSourceHeightCeiling(null)).toBeNull();
        });
    });

    describe('resolveAutoPlaybackQuality', () => {
        it('ユーザー上限とソース高さの交差で最高画質を選ぶ', () => {
            expect(resolveAutoPlaybackQuality(
                LIVE_STREAMING_QUALITIES,
                '1080p-60fps',
                1080,
            )).toBe('1080p-60fps');

            expect(resolveAutoPlaybackQuality(
                LIVE_STREAMING_QUALITIES,
                '1080p-60fps',
                720,
            )).toBe('720p');

            expect(resolveAutoPlaybackQuality(
                LIVE_STREAMING_QUALITIES,
                '720p',
                1080,
            )).toBe('720p');
        });

        it('BS4K ラダーでも上限とソースを尊重する', () => {
            expect(resolveAutoPlaybackQuality(
                BS4K_LIVE_STREAMING_QUALITIES,
                '2160p',
                1080,
            )).toBe('1080p-60fps');

            expect(resolveAutoPlaybackQuality(
                BS4K_LIVE_STREAMING_QUALITIES,
                '4320p',
                2160,
            )).toBe('2160p');
        });

        it('ソース不明時はユーザー上限まで上げる', () => {
            expect(resolveAutoPlaybackQuality(
                LIVE_STREAMING_QUALITIES,
                '1080p',
                null,
            )).toBe('1080p');
        });

        it('細い回線では回線上限以下に抑える（開始不能を避ける）', () => {
            expect(resolveAutoPlaybackQuality(
                LIVE_STREAMING_QUALITIES,
                '1080p-60fps',
                1080,
                {downlink: 2, effectiveType: '4g'},
            )).toBe('480p');

            expect(resolveAutoPlaybackQuality(
                LIVE_STREAMING_QUALITIES,
                '1080p-60fps',
                1080,
                {downlink: 5, effectiveType: '4g'},
            )).toBe('720p');
        });

        it('60fps 不可の回線では同高さの 30fps/非60 段を選ぶ', () => {
            expect(resolveAutoPlaybackQuality(
                LIVE_STREAMING_QUALITIES,
                '1080p-60fps',
                1080,
                {downlink: 10, effectiveType: '4g'},
            )).toBe('1080p');

            expect(resolveAutoPlaybackQuality(
                BS4K_LIVE_STREAMING_QUALITIES,
                '1080p-60fps',
                1080,
                {downlink: 10, effectiveType: '4g'},
            )).toBe('1080p-30fps');
        });

        it('回線指標なしでは従来どおりソース・ユーザー上限のみ', () => {
            expect(resolveAutoPlaybackQuality(
                LIVE_STREAMING_QUALITIES,
                '1080p-60fps',
                1080,
                {},
            )).toBe('1080p-60fps');
        });
    });

    describe('estimateNetworkHeightCeiling', () => {
        it('downlink / effectiveType / saveData から高さ上限を返す', () => {
            expect(estimateNetworkHeightCeiling({downlink: 1})).toBe(360);
            expect(estimateNetworkHeightCeiling({downlink: 3})).toBe(540);
            expect(estimateNetworkHeightCeiling({downlink: 20})).toBe(2160);
            expect(estimateNetworkHeightCeiling({effectiveType: '3g'})).toBe(480);
            expect(estimateNetworkHeightCeiling({saveData: true})).toBe(360);
            expect(estimateNetworkHeightCeiling({})).toBeNull();
        });

        it('高 RTT では 1 段相当下げる', () => {
            expect(estimateNetworkHeightCeiling({downlink: 20, rtt: 200})).toBe(1440);
        });
    });

    describe('estimateNetworkAllows60fps', () => {
        it('細い回線では 60fps を許可しない', () => {
            expect(estimateNetworkAllows60fps({downlink: 8})).toBe(false);
            expect(estimateNetworkAllows60fps({downlink: 20, rtt: 30})).toBe(true);
            expect(estimateNetworkAllows60fps({})).toBe(true);
        });
    });

    describe('getNextLowerAutoPlaybackQuality', () => {
        it('ラダー上で 1 段下を返す', () => {
            expect(getNextLowerAutoPlaybackQuality(
                LIVE_STREAMING_QUALITIES,
                '1080p-60fps',
            )).toBe('1080p');
            expect(getNextLowerAutoPlaybackQuality(
                LIVE_STREAMING_QUALITIES,
                '720p',
            )).toBe('540p');
            expect(getNextLowerAutoPlaybackQuality(
                LIVE_STREAMING_QUALITIES,
                '240p',
            )).toBeNull();
        });

        it('BS4K ラダーでも 60fps から 30fps へ下がる', () => {
            expect(getNextLowerAutoPlaybackQuality(
                BS4K_LIVE_STREAMING_QUALITIES,
                '1080p-60fps',
            )).toBe('1080p-30fps');
        });
    });

    describe('resolveAutoLowLatencyFromNetwork', () => {
        it('既存設定を見ずネットワーク指標だけで判定する', () => {
            expect(resolveAutoLowLatencyFromNetwork({
                type: 'wifi',
                effectiveType: '4g',
                downlink: 20,
                rtt: 30,
            })).toBe(true);

            expect(resolveAutoLowLatencyFromNetwork({
                type: 'cellular',
                effectiveType: '4g',
                downlink: 50,
                rtt: 20,
            })).toBe(false);

            expect(resolveAutoLowLatencyFromNetwork({
                type: 'wifi',
                downlink: 2,
                rtt: 30,
            })).toBe(false);

            expect(resolveAutoLowLatencyFromNetwork({
                type: 'wifi',
                downlink: 20,
                rtt: 200,
            })).toBe(false);

            expect(resolveAutoLowLatencyFromNetwork({
                saveData: true,
                type: 'wifi',
                downlink: 50,
                rtt: 10,
            })).toBe(false);

            // 指標欠落は安全側 OFF
            expect(resolveAutoLowLatencyFromNetwork({})).toBe(false);
        });
    });

    describe('resolveEfficiencyFirstPlaybackCombination', () => {
        const profile = {
            streaming_quality: '1080p' as const,
            is_bs4k: false,
        };
        const empty_caps = {video: [], audio: [], live_combinations: []};

        beforeEach(() => {
            vi.mocked(Videos.resolveKonomiTVBS4KExactPlaybackCombination).mockReset();
        });

        it('AV1 不可なら VP9 を試し、AVC へ直行しない', () => {
            vi.mocked(Videos.resolveKonomiTVBS4KExactPlaybackCombination).mockImplementation(
                (_caps, _enc, video_codec, audio_codec) => {
                    if (video_codec === 'av1') return null;
                    if (video_codec === 'vp9' && audio_codec === 'opus') {
                        return {
                            video: {codec: 'vp9', bit_depth: 10},
                            audio: {codec: 'opus'},
                            live: {available: true},
                        } as never;
                    }
                    return null;
                },
            );
            const result = resolveEfficiencyFirstPlaybackCombination(
                empty_caps as never,
                'QSV',
                profile as never,
                true,
            );
            expect(result?.video.codec).toBe('vp9');
            expect(result?.audio.codec).toBe('opus');
            const calls = vi.mocked(Videos.resolveKonomiTVBS4KExactPlaybackCombination).mock.calls
                .map((call) => call[2]);
            expect(calls.indexOf('av1')).toBeLessThan(calls.indexOf('vp9'));
            expect(calls.includes('avc') === false || calls.indexOf('vp9') < calls.indexOf('avc')).toBe(true);
        });

        it('ライブでも Opus が使えるなら AAC より優先する', () => {
            vi.mocked(Videos.resolveKonomiTVBS4KExactPlaybackCombination).mockImplementation(
                (_caps, _enc, video_codec, audio_codec) => {
                    if (video_codec === 'av1' && audio_codec === 'opus') {
                        return {
                            video: {codec: 'av1', bit_depth: 10},
                            audio: {codec: 'opus'},
                            live: {available: true},
                        } as never;
                    }
                    if (video_codec === 'av1' && audio_codec === 'aac') {
                        return {
                            video: {codec: 'av1', bit_depth: 10},
                            audio: {codec: 'aac'},
                            live: {available: true},
                        } as never;
                    }
                    return null;
                },
            );
            const result = resolveEfficiencyFirstPlaybackCombination(
                empty_caps as never,
                'QSV',
                profile as never,
                true,
            );
            expect(result?.audio.codec).toBe('opus');
            const audio_calls = vi.mocked(Videos.resolveKonomiTVBS4KExactPlaybackCombination).mock.calls
                .filter((call) => call[2] === 'av1')
                .map((call) => call[3]);
            expect(audio_calls[0]).toBe('opus');
        });

        it('除外リストの codec は飛ばして次段を選ぶ', () => {
            vi.mocked(Videos.resolveKonomiTVBS4KExactPlaybackCombination).mockImplementation(
                (_caps, _enc, video_codec, audio_codec) => {
                    if (video_codec === 'hevc' && audio_codec === 'opus') {
                        return {
                            video: {codec: 'hevc', bit_depth: 10},
                            audio: {codec: 'opus'},
                            live: {available: true},
                        } as never;
                    }
                    return null;
                },
            );
            const result = resolveEfficiencyFirstPlaybackCombination(
                empty_caps as never,
                'QSV',
                profile as never,
                true,
                ['av1', 'vp9'],
            );
            expect(result?.video.codec).toBe('hevc');
            expect(result?.audio.codec).toBe('opus');
        });
    });

    describe('getPlaybackQualityHeight', () => {
        it('画質 ID から高さを取り出す', () => {
            expect(getPlaybackQualityHeight('1080p-60fps')).toBe(1080);
            expect(getPlaybackQualityHeight('4320p')).toBe(4320);
        });
    });
});
