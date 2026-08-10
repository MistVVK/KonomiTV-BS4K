import { afterEach, describe, expect, it, vi } from 'vitest';

import APIClient from '@/services/APIClient';
import Videos, { type IKonomiTVBS4KPlaybackCapabilities } from '@/services/Videos';
import { PlayerUtils } from '@/utils';


const buildCapabilities = (
    combinations: Array<{
        video_codec: 'avc' | 'hevc' | 'vp9' | 'av1';
        video_bit_depth: 8 | 10;
        audio_codec: 'aac' | 'opus';
    }>,
): IKonomiTVBS4KPlaybackCapabilities => ({
    video: combinations.map((combination) => ({
        encoder: 'FFmpeg',
        codec: combination.video_codec,
        bit_depth: combination.video_bit_depth,
        profile: 'test',
        live_available: true,
        recorded_available: true,
        live_reason_code: null,
        recorded_reason_code: null,
    })).filter((capability, index, source) => source.findIndex((candidate) =>
        candidate.codec === capability.codec && candidate.bit_depth === capability.bit_depth
    ) === index),
    audio: combinations.map((combination) => ({
        codec: combination.audio_codec,
        live_available: true,
        recorded_available: true,
        live_reason_code: null,
        recorded_reason_code: null,
    })).filter((capability, index, source) => source.findIndex((candidate) =>
        candidate.codec === capability.codec
    ) === index),
    live_combinations: combinations.map((combination) => ({
        encoder: 'FFmpeg',
        ...combination,
        available: true,
        reason_code: null,
    })),
});


describe('KonomiTV-BS4K playback profile resolver', () => {

    afterEach(() => {
        vi.restoreAllMocks();
    });


    it('映像と音声の候補を希望値から互換側だけへ並べる', () => {
        expect(Videos.getKonomiTVBS4KLowerVideoCodecOrder('av1')).toEqual(['av1', 'vp9', 'hevc', 'avc']);
        expect(Videos.getKonomiTVBS4KLowerVideoCodecOrder('vp9')).toEqual(['vp9', 'hevc', 'avc']);
        expect(Videos.getKonomiTVBS4KLowerVideoCodecOrder('hevc')).toEqual(['hevc', 'avc']);
        expect(Videos.getKonomiTVBS4KLowerVideoCodecOrder('avc')).toEqual(['avc']);
        expect(Videos.getKonomiTVBS4KLowerAudioCodecOrder('opus')).toEqual(['opus', 'aac']);
        expect(Videos.getKonomiTVBS4KLowerAudioCodecOrder('aac')).toEqual(['aac']);
    });


    it('live・recorded・browserの積集合からAV1より下位のVP9/AACを選ぶ', () => {
        vi.spyOn(PlayerUtils, 'isKonomiTVBS4KPlaybackVideoCodecSupported').mockReturnValue(true);
        vi.spyOn(PlayerUtils, 'isKonomiTVBS4KPlaybackAudioCodecSupported').mockReturnValue(true);
        const capabilities = buildCapabilities([
            {video_codec: 'vp9', video_bit_depth: 10, audio_codec: 'aac'},
            {video_codec: 'avc', video_bit_depth: 8, audio_codec: 'aac'},
        ]);

        const resolved = Videos.resolveKonomiTVBS4KPlaybackCombination(
            capabilities,
            'FFmpeg',
            'av1',
            'opus',
            {is_bs4k: false, streaming_quality: '1080p'},
        );

        expect(resolved?.video.codec).toBe('vp9');
        expect(resolved?.video.bit_depth).toBe(10);
        expect(resolved?.audio.codec).toBe('aac');
    });


    it('AVC/AAC希望時に上位codecへ戻らない', () => {
        vi.spyOn(PlayerUtils, 'isKonomiTVBS4KPlaybackVideoCodecSupported').mockReturnValue(true);
        vi.spyOn(PlayerUtils, 'isKonomiTVBS4KPlaybackAudioCodecSupported').mockReturnValue(true);
        const capabilities = buildCapabilities([
            {video_codec: 'av1', video_bit_depth: 10, audio_codec: 'opus'},
        ]);

        expect(Videos.resolveKonomiTVBS4KPlaybackCombination(
            capabilities,
            'FFmpeg',
            'avc',
            'aac',
            {is_bs4k: false, streaming_quality: '1080p'},
        )).toBeNull();
    });


    it('targeted能力API障害時だけAVC 8bit/AAC互換tupleを返す', async () => {
        vi.spyOn(PlayerUtils, 'isKonomiTVBS4KPlaybackVideoCodecSupported').mockReturnValue(true);
        vi.spyOn(PlayerUtils, 'isKonomiTVBS4KPlaybackAudioCodecSupported').mockReturnValue(true);
        vi.spyOn(APIClient, 'get').mockResolvedValue({type: 'error'} as never);

        await expect(Videos.preflightKonomiTVBS4KPlaybackProfile(
            'FFmpeg',
            'av1',
            'opus',
            {is_bs4k: false, streaming_quality: '1080p'},
            'Live',
            true,
        )).resolves.toEqual({
            video_codec: 'avc',
            video_bit_depth: 8,
            audio_codec: 'aac',
            fallback_reason: 'CapabilityAPIUnavailable',
        });
    });


    it('録画preflightはlive combinationがなくてもrecorded能力からexact tupleを返す', async () => {
        vi.spyOn(PlayerUtils, 'isKonomiTVBS4KPlaybackVideoCodecSupported').mockReturnValue(true);
        vi.spyOn(PlayerUtils, 'isKonomiTVBS4KPlaybackAudioCodecSupported').mockReturnValue(true);
        const api_get = vi.spyOn(APIClient, 'get').mockResolvedValue({
            type: 'success',
            data: {
                video: [{
                    encoder: 'FFmpeg',
                    codec: 'av1',
                    bit_depth: 10,
                    profile: 'Main',
                    live_available: false,
                    recorded_available: true,
                    live_reason_code: 'BridgeUnavailable',
                    recorded_reason_code: null,
                }],
                audio: [{
                    codec: 'opus',
                    live_available: false,
                    recorded_available: true,
                    live_reason_code: 'BridgeUnavailable',
                    recorded_reason_code: null,
                }],
                live_combinations: [],
            },
        } as never);

        await expect(Videos.preflightKonomiTVBS4KPlaybackProfile(
            'FFmpeg',
            'av1',
            'opus',
            {is_bs4k: false, streaming_quality: '1080p'},
            'Video',
            true,
        )).resolves.toEqual({
            video_codec: 'av1',
            video_bit_depth: 10,
            audio_codec: 'opus',
            fallback_reason: 'None',
        });
        expect(api_get).toHaveBeenCalledWith(
            '/streams/video/konomitv-bs4k-playback-capabilities/targeted',
            expect.objectContaining({
                params: expect.objectContaining({playback_mode: 'Video'}),
            }),
        );
    });


    it('route離脱のAbortSignalをtargeted APIへ渡し、API障害fallbackとして扱わない', async () => {
        vi.spyOn(PlayerUtils, 'isKonomiTVBS4KPlaybackVideoCodecSupported').mockReturnValue(true);
        vi.spyOn(PlayerUtils, 'isKonomiTVBS4KPlaybackAudioCodecSupported').mockReturnValue(true);
        const owner = new AbortController();
        vi.spyOn(APIClient, 'get').mockImplementation(async (_url, config) => {
            expect(config?.signal).toBe(owner.signal);
            owner.abort();
            return {type: 'error'} as never;
        });

        await expect(Videos.preflightKonomiTVBS4KPlaybackProfile(
            'FFmpeg',
            'av1',
            'opus',
            {is_bs4k: false, streaming_quality: '1080p'},
            'Live',
            true,
            false,
            owner.signal,
        )).rejects.toMatchObject({name: 'AbortError'});
    });


    it('ブラウザMSE非対応候補ではtargeted能力APIを呼ばない', async () => {
        vi.spyOn(PlayerUtils, 'isKonomiTVBS4KPlaybackVideoCodecSupported').mockReturnValue(false);
        vi.spyOn(PlayerUtils, 'isKonomiTVBS4KPlaybackAudioCodecSupported').mockReturnValue(true);
        const api_get = vi.spyOn(APIClient, 'get');

        await expect(Videos.preflightKonomiTVBS4KPlaybackProfile(
            'FFmpeg',
            'av1',
            'opus',
            {is_bs4k: false, streaming_quality: '1080p'},
            'Live',
            true,
        )).resolves.toBeNull();
        expect(api_get).not.toHaveBeenCalled();
    });


    it('画質選択肢に能力不足の理由を付けつつ、希望値として選択可能に保つ', () => {
        vi.spyOn(PlayerUtils, 'isKonomiTVBS4KPlaybackVideoCodecSupported').mockReturnValue(true);
        vi.spyOn(PlayerUtils, 'isKonomiTVBS4KPlaybackAudioCodecSupported').mockReturnValue(true);

        const options = Videos.buildKonomiTVBS4KPlaybackQualityOptions(
            'FFmpeg',
            {video: [], audio: [], live_combinations: []},
            'av1',
            false,
            [{title: '1080p', value: '1080p'}],
            'opus',
        );

        expect(options).toEqual([{
            title: '1080p（このコーデック構成は利用できません）',
            value: '1080p',
            reason: 'このコーデック構成は利用できません',
            props: {disabled: false},
        }]);
    });
});
