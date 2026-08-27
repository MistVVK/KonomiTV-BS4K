import { afterEach, describe, expect, it, vi } from 'vitest';

import { PlayerUtils } from '@/utils';
import {
    KONOMITV_BS4K_BROWSER_AUDIO_CATALOG,
    KONOMITV_BS4K_BROWSER_VIDEO_CATALOG,
    estimateKonomiTVBS4KBrowserVideoHardware,
    judgeKonomiTVBS4KBrowserAudioSupport,
    judgeKonomiTVBS4KBrowserVideoSupport,
    runKonomiTVBS4KBrowserCodecSupportDiagnostics,
} from '@/utils/KonomiTVBS4KBrowserCodecSupport';


describe('KONOMITV_BS4K_BROWSER_VIDEO_CATALOG', () => {
    it('ID が一意である', () => {
        const ids = KONOMITV_BS4K_BROWSER_VIDEO_CATALOG.map(entry => entry.id);
        expect(new Set(ids).size).toBe(ids.length);
    });

    it('KonomiTV-BS4K 本線の行は AVC / HEVC / VP9 / AV1 を含む', () => {
        const used_codecs = new Set(
            KONOMITV_BS4K_BROWSER_VIDEO_CATALOG
                .filter(entry => entry.used_by_konomitv_bs4k === true)
                .map(entry => entry.codec),
        );
        expect(used_codecs.has('AVC')).toBe(true);
        expect(used_codecs.has('HEVC')).toBe(true);
        expect(used_codecs.has('VP9')).toBe(true);
        expect(used_codecs.has('AV1')).toBe(true);
    });

    it('KonomiTV-BS4K 本線の MIME は PlayerUtils が生成する MIME と一致する', () => {
        for (const entry of KONOMITV_BS4K_BROWSER_VIDEO_CATALOG) {
            if (
                entry.used_by_konomitv_bs4k === false ||
                entry.konomitv_playback_video_codec === undefined ||
                entry.konomitv_playback_profile === undefined
            ) {
                continue;
            }
            const expected = PlayerUtils.getKonomiTVBS4KPlaybackVideoMIMEType(
                entry.konomitv_playback_video_codec,
                entry.bit_depth,
                entry.konomitv_playback_profile,
            );
            expect(entry.mime_type).toBe(expected);
        }
    });
});

describe('KONOMITV_BS4K_BROWSER_AUDIO_CATALOG', () => {
    it('ID が一意であり KonomiTV-BS4K 本線は AAC-LC と Opus のみ', () => {
        const ids = KONOMITV_BS4K_BROWSER_AUDIO_CATALOG.map(entry => entry.id);
        expect(new Set(ids).size).toBe(ids.length);
        const used = KONOMITV_BS4K_BROWSER_AUDIO_CATALOG
            .filter(entry => entry.used_by_konomitv_bs4k === true)
            .map(entry => entry.codec)
            .sort();
        expect(used).toEqual(['AAC-LC', 'Opus']);
    });
});

describe('judgeKonomiTVBS4KBrowserVideoSupport', () => {
    it('MediaCapabilities が対応なら Supported を返す', () => {
        const result = judgeKonomiTVBS4KBrowserVideoSupport({
            media_capabilities: 'Supported',
            media_source: 'Supported',
            can_play_type: 'No',
        });
        expect(result.support).toBe('Supported');
        expect(result.evidence).toBe('MediaCapabilities');
    });

    it('MediaCapabilities が対応でも MSE が非対応なら矛盾として Unknown を返す', () => {
        const result = judgeKonomiTVBS4KBrowserVideoSupport({
            media_capabilities: 'Supported',
            media_source: 'Unsupported',
            can_play_type: 'No',
        });
        expect(result.support).toBe('Unknown');
    });

    it('MediaCapabilities が非対応で他 API も非対応なら Unsupported を返す', () => {
        const result = judgeKonomiTVBS4KBrowserVideoSupport({
            media_capabilities: 'Unsupported',
            media_source: 'Unavailable',
            can_play_type: 'No',
        });
        expect(result.support).toBe('Unsupported');
    });

    it('MediaCapabilities が非対応なのに MSE が対応なら矛盾として Unknown を返す', () => {
        const result = judgeKonomiTVBS4KBrowserVideoSupport({
            media_capabilities: 'Unsupported',
            media_source: 'Supported',
            can_play_type: 'No',
        });
        expect(result.support).toBe('Unknown');
    });

    it('MediaCapabilities がない環境で MSE が対応なら Likely を返す', () => {
        const result = judgeKonomiTVBS4KBrowserVideoSupport({
            media_capabilities: 'Unavailable',
            media_source: 'Supported',
            can_play_type: 'No',
        });
        expect(result.support).toBe('Likely');
        expect(result.evidence).toBe('MediaSource');
    });

    it('MSE が非対応なのに canPlayType が対応を示すなら矛盾として Unknown を返す', () => {
        const result = judgeKonomiTVBS4KBrowserVideoSupport({
            media_capabilities: 'Unavailable',
            media_source: 'Unsupported',
            can_play_type: 'Probably',
        });
        expect(result.support).toBe('Unknown');
    });

    it('canPlayType のみで maybe なら Likely を返す', () => {
        const result = judgeKonomiTVBS4KBrowserVideoSupport({
            media_capabilities: 'Unavailable',
            media_source: 'Unavailable',
            can_play_type: 'Maybe',
        });
        expect(result.support).toBe('Likely');
        expect(result.evidence).toBe('CanPlayType');
    });

    it('全 API が非対応なら Unsupported を返す', () => {
        const result = judgeKonomiTVBS4KBrowserVideoSupport({
            media_capabilities: 'Unavailable',
            media_source: 'Unavailable',
            can_play_type: 'No',
        });
        expect(result.support).toBe('Unsupported');
    });
});

describe('judgeKonomiTVBS4KBrowserAudioSupport', () => {
    it('MediaCapabilities が対応なら Supported を返す', () => {
        const result = judgeKonomiTVBS4KBrowserAudioSupport({
            media_capabilities: 'Supported',
            can_play_type: 'No',
        });
        expect(result.support).toBe('Supported');
        expect(result.evidence).toBe('MediaCapabilities');
    });

    it('MediaCapabilities が非対応で canPlayType も非対応なら Unsupported を返す', () => {
        const result = judgeKonomiTVBS4KBrowserAudioSupport({
            media_capabilities: 'Unsupported',
            can_play_type: 'No',
        });
        expect(result.support).toBe('Unsupported');
    });

    it('MediaCapabilities が非対応なのに canPlayType が対応を示すなら Unknown を返す', () => {
        const result = judgeKonomiTVBS4KBrowserAudioSupport({
            media_capabilities: 'Unsupported',
            can_play_type: 'Probably',
        });
        expect(result.support).toBe('Unknown');
    });

    it('MediaCapabilities がない環境で canPlayType が maybe なら Likely を返す', () => {
        const result = judgeKonomiTVBS4KBrowserAudioSupport({
            media_capabilities: 'Unavailable',
            can_play_type: 'Maybe',
        });
        expect(result.support).toBe('Likely');
        expect(result.evidence).toBe('CanPlayType');
    });
});

describe('estimateKonomiTVBS4KBrowserVideoHardware', () => {
    it('ブラウザ非対応なら NotApplicable を返す', () => {
        const result = estimateKonomiTVBS4KBrowserVideoHardware({
            engine: 'Chromium',
            browser_support: 'Unsupported',
            power_efficient: true,
            web_codecs_prefer_hardware: 'Supported',
            web_codecs_no_preference: 'Supported',
        });
        expect(result).toBe('NotApplicable');
    });

    it('ブラウザ対応が不明なら Unknown を返す', () => {
        const result = estimateKonomiTVBS4KBrowserVideoHardware({
            engine: 'Chromium',
            browser_support: 'Unknown',
            power_efficient: true,
            web_codecs_prefer_hardware: 'Supported',
            web_codecs_no_preference: 'Supported',
        });
        expect(result).toBe('Unknown');
    });

    it('Chromium では WebCodecs の prefer-hardware が主根拠になる', () => {
        expect(estimateKonomiTVBS4KBrowserVideoHardware({
            engine: 'Chromium',
            browser_support: 'Supported',
            power_efficient: false,
            web_codecs_prefer_hardware: 'Supported',
            web_codecs_no_preference: 'Supported',
        })).toBe('HardwareLikely');
        expect(estimateKonomiTVBS4KBrowserVideoHardware({
            engine: 'Chromium',
            browser_support: 'Supported',
            power_efficient: true,
            web_codecs_prefer_hardware: 'Unsupported',
            web_codecs_no_preference: 'Supported',
        })).toBe('SoftwareLikely');
    });

    it('Chromium で WebCodecs が無いなら powerEfficient にフォールバックする', () => {
        expect(estimateKonomiTVBS4KBrowserVideoHardware({
            engine: 'Chromium',
            browser_support: 'Likely',
            power_efficient: true,
            web_codecs_prefer_hardware: 'Unavailable',
            web_codecs_no_preference: 'Unavailable',
        })).toBe('HardwareLikely');
        expect(estimateKonomiTVBS4KBrowserVideoHardware({
            engine: 'Chromium',
            browser_support: 'Likely',
            power_efficient: false,
            web_codecs_prefer_hardware: 'Unavailable',
            web_codecs_no_preference: 'Unavailable',
        })).toBe('SoftwareLikely');
    });

    it('Gecko / WebKit では powerEfficient が主根拠になる', () => {
        for (const engine of ['Gecko', 'WebKit'] as const) {
            expect(estimateKonomiTVBS4KBrowserVideoHardware({
                engine,
                browser_support: 'Supported',
                power_efficient: true,
                web_codecs_prefer_hardware: 'Unavailable',
                web_codecs_no_preference: 'Unavailable',
            })).toBe('HardwareLikely');
            expect(estimateKonomiTVBS4KBrowserVideoHardware({
                engine,
                browser_support: 'Supported',
                power_efficient: false,
                web_codecs_prefer_hardware: 'Unavailable',
                web_codecs_no_preference: 'Unavailable',
            })).toBe('SoftwareLikely');
            expect(estimateKonomiTVBS4KBrowserVideoHardware({
                engine,
                browser_support: 'Supported',
                power_efficient: null,
                web_codecs_prefer_hardware: 'Unavailable',
                web_codecs_no_preference: 'Unavailable',
            })).toBe('Unknown');
        }
    });

    it('Unknown エンジンでは両ソースが一致した時のみ推定する', () => {
        expect(estimateKonomiTVBS4KBrowserVideoHardware({
            engine: 'Unknown',
            browser_support: 'Supported',
            power_efficient: true,
            web_codecs_prefer_hardware: 'Supported',
            web_codecs_no_preference: 'Supported',
        })).toBe('HardwareLikely');
        expect(estimateKonomiTVBS4KBrowserVideoHardware({
            engine: 'Unknown',
            browser_support: 'Supported',
            power_efficient: false,
            web_codecs_prefer_hardware: 'Unsupported',
            web_codecs_no_preference: 'Supported',
        })).toBe('SoftwareLikely');
        expect(estimateKonomiTVBS4KBrowserVideoHardware({
            engine: 'Unknown',
            browser_support: 'Supported',
            power_efficient: true,
            web_codecs_prefer_hardware: 'Unsupported',
            web_codecs_no_preference: 'Supported',
        })).toBe('Unknown');
    });
});

describe('runKonomiTVBS4KBrowserCodecSupportDiagnostics', () => {
    const original_media_capabilities = navigator.mediaCapabilities;
    const original_media_source = window.MediaSource;
    const original_video_decoder = globalThis.VideoDecoder;
    const original_video_encoder = globalThis.VideoEncoder;
    const original_audio_decoder = (globalThis as {AudioDecoder?: unknown}).AudioDecoder;
    const original_audio_encoder = (globalThis as {AudioEncoder?: unknown}).AudioEncoder;
    const can_play_type_spy = vi.spyOn(HTMLVideoElement.prototype, 'canPlayType');

    afterEach(() => {
        Object.defineProperty(navigator, 'mediaCapabilities', {
            value: original_media_capabilities,
            configurable: true,
            writable: true,
        });
        window.MediaSource = original_media_source;
        globalThis.VideoDecoder = original_video_decoder;
        globalThis.VideoEncoder = original_video_encoder;
        (globalThis as {AudioDecoder?: unknown}).AudioDecoder = original_audio_decoder;
        (globalThis as {AudioEncoder?: unknown}).AudioEncoder = original_audio_encoder;
        // mockRestore() すると spy が prototype から切り離され、以降の mockReturnValue が効かなくなる。
        // 接続を維持したまま既定値 ('' → No) へ戻す。
        can_play_type_spy.mockReset();
        can_play_type_spy.mockReturnValue('');
    });

    it('全 API が対応する環境では映像・音声の全行で対応を返す', async () => {
        Object.defineProperty(navigator, 'mediaCapabilities', {
            value: {
                decodingInfo: vi.fn().mockResolvedValue({
                    supported: true,
                    smooth: true,
                    powerEfficient: true,
                }),
            },
            configurable: true,
            writable: true,
        });
        window.MediaSource = class {
            static isTypeSupported(): boolean {
                return true;
            }
        };
        globalThis.VideoDecoder = {
            isConfigSupported: vi.fn().mockResolvedValue({supported: true}),
        };
        globalThis.VideoEncoder = {
            isConfigSupported: vi.fn().mockResolvedValue({supported: true}),
        };
        const audio_static = {
            isConfigSupported: vi.fn().mockResolvedValue({supported: true}),
        };
        (globalThis as {AudioDecoder?: unknown}).AudioDecoder = audio_static;
        (globalThis as {AudioEncoder?: unknown}).AudioEncoder = audio_static;
        can_play_type_spy.mockReturnValue('probably');

        const result = await runKonomiTVBS4KBrowserCodecSupportDiagnostics();

        // 環境の API 存在確認
        expect(result.environment.apis).toEqual({
            media_source: true,
            managed_media_source: false,
            media_capabilities: true,
            web_codecs_video_decoder: true,
            web_codecs_video_encoder: true,
            web_codecs_audio_decoder: true,
            web_codecs_audio_encoder: true,
        });

        // 映像 decode: 全行 Supported + WebCodecs 対応。
        // happy-dom の UA は Chromium / Firefox / Safari どれもに該当しないため
        // engine は Unknown となり、WebCodecs + powerEfficient の一致で HardwareLikely になる。
        expect(result.video_decode.length).toBe(KONOMITV_BS4K_BROWSER_VIDEO_CATALOG.length);
        for (const row of result.video_decode) {
            expect(row.browser_support).toBe('Supported');
            expect(row.web_codecs_prefer_hardware).toBe('Supported');
            expect(row.web_codecs_no_preference).toBe('Supported');
            expect(row.hardware_estimate).toBe('HardwareLikely');
            if (row.used_by_konomitv_bs4k === true) {
                expect(row.konomitv_playback).toBe(true);
            } else {
                expect(row.konomitv_playback).toBeNull();
            }
        }

        // 映像 encode: 全行 3 設定とも対応。
        expect(result.video_encode.length).toBe(KONOMITV_BS4K_BROWSER_VIDEO_CATALOG.length);
        for (const row of result.video_encode) {
            expect(row.web_codecs_prefer_hardware).toBe('Supported');
            expect(row.web_codecs_prefer_software).toBe('Supported');
            expect(row.web_codecs_no_preference).toBe('Supported');
        }

        // 音声: 全行対応。KonomiTV 本線行だけ konomitv_playback を持つ。
        expect(result.audio.length).toBe(KONOMITV_BS4K_BROWSER_AUDIO_CATALOG.length);
        for (const row of result.audio) {
            expect(row.browser_support).toBe('Supported');
            expect(row.web_codecs_decode).toBe('Supported');
            expect(row.web_codecs_encode).toBe('Supported');
            if (row.used_by_konomitv_bs4k === true) {
                expect(row.konomitv_playback).toBe(true);
            } else {
                expect(row.konomitv_playback).toBeNull();
            }
        }
    });

    it('全 API がない環境では判定材料なしとして Unknown / N/A を返す', async () => {
        Object.defineProperty(navigator, 'mediaCapabilities', {
            value: undefined,
            configurable: true,
            writable: true,
        });
        window.MediaSource = undefined;
        globalThis.VideoDecoder = undefined;
        globalThis.VideoEncoder = undefined;
        (globalThis as {AudioDecoder?: unknown}).AudioDecoder = undefined;
        (globalThis as {AudioEncoder?: unknown}).AudioEncoder = undefined;
        // happy-dom の canPlayType は '' を返す。
        can_play_type_spy.mockReturnValue('');

        const result = await runKonomiTVBS4KBrowserCodecSupportDiagnostics();

        expect(result.environment.apis).toEqual({
            media_source: false,
            managed_media_source: false,
            media_capabilities: false,
            web_codecs_video_decoder: false,
            web_codecs_video_encoder: false,
            web_codecs_audio_decoder: false,
            web_codecs_audio_encoder: false,
        });
        for (const row of result.video_decode) {
            // canPlayType '' → 'No' だが MSE / MC も無い場合は Unsupported になる。
            expect(row.browser_support).toBe('Unsupported');
            expect(row.web_codecs_prefer_hardware).toBe('Unavailable');
            expect(row.web_codecs_no_preference).toBe('Unavailable');
            expect(row.hardware_estimate).toBe('NotApplicable');
            if (row.used_by_konomitv_bs4k === true) {
                expect(row.konomitv_playback).toBe(false);
            }
        }
        for (const row of result.video_encode) {
            expect(row.web_codecs_prefer_hardware).toBe('Unavailable');
            expect(row.web_codecs_prefer_software).toBe('Unavailable');
            expect(row.web_codecs_no_preference).toBe('Unavailable');
        }
        for (const row of result.audio) {
            expect(row.browser_support).toBe('Unsupported');
            expect(row.web_codecs_decode).toBe('Unavailable');
            expect(row.web_codecs_encode).toBe('Unavailable');
        }
    });

    it('WebCodecs の形状は supported:false で固定せず supported:true の形状を採用する', async () => {
        Object.defineProperty(navigator, 'mediaCapabilities', {
            value: {
                decodingInfo: vi.fn().mockResolvedValue({
                    supported: true,
                    smooth: true,
                    powerEfficient: true,
                }),
            },
            configurable: true,
            writable: true,
        });
        window.MediaSource = class {
            static isTypeSupported(): boolean {
                return true;
            }
        };
        // decoder: CodedSize は supported:false、WidthHeight は true を返す実装を再現。
        // 旧実装では CodedSize の false で形状が固定され全行が誤った形状に張り付いた。
        const decoder_mock = vi.fn()
            .mockResolvedValueOnce({supported: false})
            .mockResolvedValue({supported: true});
        globalThis.VideoDecoder = {
            isConfigSupported: decoder_mock,
        };
        // encoder / audio は通常通り true。
        globalThis.VideoEncoder = {
            isConfigSupported: vi.fn().mockResolvedValue({supported: true}),
        };
        const audio_static = {
            isConfigSupported: vi.fn().mockResolvedValue({supported: true}),
        };
        (globalThis as {AudioDecoder?: unknown}).AudioDecoder = audio_static;
        (globalThis as {AudioEncoder?: unknown}).AudioEncoder = audio_static;
        can_play_type_spy.mockReturnValue('probably');

        const result = await runKonomiTVBS4KBrowserCodecSupportDiagnostics();

        // decoder の全行が Supported になる（false 形状に張り付いていない）ことを確認する
        for (const row of result.video_decode) {
            expect(row.web_codecs_prefer_hardware).toBe('Supported');
        }
    });
});
