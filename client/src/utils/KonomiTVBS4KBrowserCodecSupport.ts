/**
 * KonomiTV-BS4K ブラウザコーデック対応診断モジュール。
 *
 * このモジュールの目的は「このブラウザで利用できる映像・音声処理経路」を表示することであり、
 * GPU の能力を断定することではない。そのため判定には以下の方針を踏まえる。
 *
 * - 対応判定は信頼度で MediaCapabilities > MediaSource / ManagedMediaSource > canPlayType の優先順にする。
 *   優先度の高いソースが「対応」と報告したのに低いソースが「非対応」と報告しているなど、
 *   複数ソースが矛盾する場合は断定せず Unknown を返す。
 * - 使えるのが canPlayType や MSE の isTypeSupported のみなら「推定対応」(Likely) と扱う。
 * - 映像のハードウェアアクセラレーションは推定値であり、エンジン別の主要根拠を使う。
 *   Blink は WebCodecs の prefer-hardware 結果、Gecko / WebKit は MediaCapabilities の
 *   powerEfficient を主要根拠とする (Safari の smooth は参考値に留める)。
 * - KonomiTV-BS4K の実再生可否は PlayerUtils が実際に使う MSE / ManagedMediaSource 判定を
 *   そのまま参照する。診断側の独自判定と実再生経路の判定が分岐しないようにするため。
 */

import type {
    IKonomiTVBS4KPlaybackVideoProfile,
    KonomiTVBS4KPlaybackAudioCodec,
    KonomiTVBS4KPlaybackVideoCodec,
} from '@/stores/SettingsStore';

import { PlayerUtils } from '@/utils/PlayerUtils';
import Utils from '@/utils/Utils';


/** 診断で識別するブラウザエンジン。 */
export type KonomiTVBS4KBrowserEngine = 'Chromium' | 'Gecko' | 'WebKit' | 'Unknown';

/**
 * ブラウザ全体の対応判定。
 * - Supported: 主要な Web API が明示的に「対応」と報告した
 * - Likely: 推定的な API (canPlayType / MSE の isTypeSupported) のみで「対応」を示した
 * - Unsupported: 利用可能な API が明示的に「非対応」と報告した
 * - Unknown: 判定材料が揃わない、または複数ソースが矛盾した
 */
export type KonomiTVBS4KBrowserSupportStatus = 'Supported' | 'Likely' | 'Unsupported' | 'Unknown';

/** 対応判定に効いた Web API の種類。 */
export type KonomiTVBS4KBrowserEvidence =
    'MediaCapabilities' |
    'MediaSource' |
    'CanPlayType' |
    'None';

/** 映像のハードウェアアクセラレーション推定値。推定であることを読み手に示す命名を意図する。 */
export type KonomiTVBS4KBrowserHardwareEstimate =
    'HardwareLikely' |
    'SoftwareLikely' |
    'Unknown' |
    'NotApplicable';

/** 単一 Web API の個別プローブ結果。API 自体が存在しない場合は Unavailable。 */
export type KonomiTVBS4KBrowserWebApiProbe = 'Supported' | 'Unsupported' | 'Unavailable';

/** WebCodecs の isConfigSupported 結果。 */
export type KonomiTVBS4KBrowserWebCodecsProbe = 'Supported' | 'Unsupported' | 'Unavailable';

/**
 * 映像カタログの 1 行。
 *
 * KonomiTV-BS4K の再生で実際に使う構成は used_by_konomitv_bs4k: true であり、
 * その MIME は PlayerUtils.getKonomiTVBS4KPlaybackVideoMIMEType() で生成する。
 * 診断用の比較対象 (AVC Baseline / VP8) だけはこのカタログ内で固定 MIME を持つ。
 * 表示上重要度の高い解像度・fps・bit depth は定義の上部に集約している。
 */
export interface IKonomiTVBS4KBrowserVideoCatalogEntry {
    id: string;
    label: string;
    codec: 'AVC' | 'HEVC' | 'VP9' | 'AV1' | 'VP8';
    profile: string;
    bit_depth: 8 | 10;
    width: number;
    height: number;
    framerate: number;
    /** 診断で使う代表的 bitrate (bps)。実配信値ではなく判定用の代表値。 */
    representative_bitrate: number;
    /** KonomiTV-BS4K の再生で実際に使う構成かどうか。 */
    used_by_konomitv_bs4k: boolean;
    /** MediaSource / MediaCapabilities / canPlayType に渡す MIME。 */
    mime_type: string;
    /** KonomiTV-BS4K 再生時のみ設定する codec / profile。PlayerUtils の判定をそのまま使う。 */
    konomitv_playback_video_codec?: KonomiTVBS4KPlaybackVideoCodec;
    konomitv_playback_profile?: IKonomiTVBS4KPlaybackVideoProfile;
}

/**
 * 音声カタログの 1 行。
 * 音声はブラウザ対応 + WebCodecs のみ表示し、HW / SW の推定は行わない (計画の仕様)。
 */
export interface IKonomiTVBS4KBrowserAudioCatalogEntry {
    id: string;
    label: string;
    codec: 'AAC-LC' | 'HE-AAC' | 'MP3' | 'Opus' | 'Vorbis' | 'FLAC' | 'AC-3' | 'E-AC-3';
    used_by_konomitv_bs4k: boolean;
    /** MediaSource / MediaCapabilities / canPlayType に渡す MIME。 */
    probe_mime_type: string;
    /** MIME の codecs パラメータと異なる WebCodecs codec 文字列が必要な場合だけ指定する。 */
    web_codecs_codec?: string;
    representative_bitrate: number;
    representative_samplerate: number;
    /** KonomiTV-BS4K 再生時のみ設定する codec。PlayerUtils の判定をそのまま使う。 */
    konomitv_playback_audio_codec?: KonomiTVBS4KPlaybackAudioCodec;
}

// 1080p60 / 2160p60 / 4320p60 は計画で定義した代表構成。
// bitrate は判定用の代表値であり、実配信 bitrate とは限りません。
const KONOMITV_BS4K_BROWSER_BITRATE_1080P60 = 12_000_000;
const KONOMITV_BS4K_BROWSER_BITRATE_1080P30 = 6_000_000;
const KONOMITV_BS4K_BROWSER_BITRATE_2160P60 = 40_000_000;
const KONOMITV_BS4K_BROWSER_BITRATE_4320P60 = 120_000_000;
const KONOMITV_BS4K_BROWSER_BITRATE_480P30 = 2_000_000;

function buildKonomiTVBS4KBrowserKonomiTVVideoMIME(
    konomitv_playback_video_codec: KonomiTVBS4KPlaybackVideoCodec,
    bit_depth: 8 | 10,
    profile: IKonomiTVBS4KPlaybackVideoProfile,
): string {
    // KonomiTV-BS4K 本線の行では PlayerUtils が再生時に作る MIME と必ず一致させる。
    const mime_type = PlayerUtils.getKonomiTVBS4KPlaybackVideoMIMEType(
        konomitv_playback_video_codec,
        bit_depth,
        profile,
    );
    if (mime_type === null) {
        throw new Error(
            `Unknown KonomiTVBS4K browser video catalog MIME: ${konomitv_playback_video_codec}/${bit_depth}`,
        );
    }
    return mime_type;
}

/**
 * 映像診断カタログ。
 * KonomiTV-BS4K 本線の codec / profile / bit depth と、比較用の AVC Baseline / VP8 のみを含む。
 */
export const KONOMITV_BS4K_BROWSER_VIDEO_CATALOG: readonly IKonomiTVBS4KBrowserVideoCatalogEntry[] = [
    {
        id: 'avc-baseline-8-1080p30',
        label: 'AVC (H.264) Baseline 8bit',
        codec: 'AVC',
        profile: 'Baseline',
        bit_depth: 8,
        width: 1920,
        height: 1080,
        framerate: 30,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_1080P30,
        used_by_konomitv_bs4k: false,
        // Baseline は再生本線に使わないため、診断用の固定 codec string を使う。
        mime_type: 'video/mp4; codecs="avc1.42001E"',
    },
    {
        id: 'avc-high-8-1080p60',
        label: 'AVC (H.264) High 8bit',
        codec: 'AVC',
        profile: 'High',
        bit_depth: 8,
        width: 1440,
        height: 1080,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_1080P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('avc', 8, {
            is_bs4k: false,
            streaming_quality: '1080p-60fps',
        }),
        konomitv_playback_video_codec: 'avc',
        konomitv_playback_profile: {is_bs4k: false, streaming_quality: '1080p-60fps'},
    },
    {
        id: 'avc-high-8-2160p60',
        label: 'AVC (H.264) High 8bit',
        codec: 'AVC',
        profile: 'High',
        bit_depth: 8,
        width: 3840,
        height: 2160,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_2160P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('avc', 8, {
            is_bs4k: true,
            streaming_quality: '2160p',
        }),
        konomitv_playback_video_codec: 'avc',
        konomitv_playback_profile: {is_bs4k: true, streaming_quality: '2160p'},
    },
    {
        id: 'hevc-main-8-1080p60',
        label: 'HEVC (H.265) Main 8bit',
        codec: 'HEVC',
        profile: 'Main',
        bit_depth: 8,
        width: 1440,
        height: 1080,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_1080P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('hevc', 8, {
            is_bs4k: false,
            streaming_quality: '1080p-60fps',
        }),
        konomitv_playback_video_codec: 'hevc',
        konomitv_playback_profile: {is_bs4k: false, streaming_quality: '1080p-60fps'},
    },
    {
        id: 'hevc-main-8-2160p60',
        label: 'HEVC (H.265) Main 8bit',
        codec: 'HEVC',
        profile: 'Main',
        bit_depth: 8,
        width: 3840,
        height: 2160,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_2160P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('hevc', 8, {
            is_bs4k: true,
            streaming_quality: '2160p',
        }),
        konomitv_playback_video_codec: 'hevc',
        konomitv_playback_profile: {is_bs4k: true, streaming_quality: '2160p'},
    },
    {
        id: 'hevc-main10-10-1080p60',
        label: 'HEVC (H.265) Main 10bit',
        codec: 'HEVC',
        profile: 'Main10',
        bit_depth: 10,
        width: 1440,
        height: 1080,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_1080P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('hevc', 10, {
            is_bs4k: false,
            streaming_quality: '1080p-60fps',
        }),
        konomitv_playback_video_codec: 'hevc',
        konomitv_playback_profile: {is_bs4k: false, streaming_quality: '1080p-60fps'},
    },
    {
        id: 'hevc-main10-10-2160p60',
        label: 'HEVC (H.265) Main 10bit',
        codec: 'HEVC',
        profile: 'Main10',
        bit_depth: 10,
        width: 3840,
        height: 2160,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_2160P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('hevc', 10, {
            is_bs4k: true,
            streaming_quality: '2160p',
        }),
        konomitv_playback_video_codec: 'hevc',
        konomitv_playback_profile: {is_bs4k: true, streaming_quality: '2160p'},
    },
    {
        id: 'hevc-main10-10-4320p60',
        label: 'HEVC (H.265) Main 10bit',
        codec: 'HEVC',
        profile: 'Main10',
        bit_depth: 10,
        width: 7680,
        height: 4320,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_4320P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('hevc', 10, {
            is_bs4k: true,
            streaming_quality: '4320p',
        }),
        konomitv_playback_video_codec: 'hevc',
        konomitv_playback_profile: {is_bs4k: true, streaming_quality: '4320p'},
    },
    {
        id: 'vp9-profile0-8-1080p60',
        label: 'VP9 Profile 0 8bit',
        codec: 'VP9',
        profile: 'Profile 0',
        bit_depth: 8,
        width: 1440,
        height: 1080,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_1080P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('vp9', 8, {
            is_bs4k: false,
            streaming_quality: '1080p-60fps',
        }),
        konomitv_playback_video_codec: 'vp9',
        konomitv_playback_profile: {is_bs4k: false, streaming_quality: '1080p-60fps'},
    },
    {
        id: 'vp9-profile0-8-2160p60',
        label: 'VP9 Profile 0 8bit',
        codec: 'VP9',
        profile: 'Profile 0',
        bit_depth: 8,
        width: 3840,
        height: 2160,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_2160P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('vp9', 8, {
            is_bs4k: true,
            streaming_quality: '2160p',
        }),
        konomitv_playback_video_codec: 'vp9',
        konomitv_playback_profile: {is_bs4k: true, streaming_quality: '2160p'},
    },
    {
        id: 'vp9-profile0-8-4320p60',
        label: 'VP9 Profile 0 8bit',
        codec: 'VP9',
        profile: 'Profile 0',
        bit_depth: 8,
        width: 7680,
        height: 4320,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_4320P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('vp9', 8, {
            is_bs4k: true,
            streaming_quality: '4320p',
        }),
        konomitv_playback_video_codec: 'vp9',
        konomitv_playback_profile: {is_bs4k: true, streaming_quality: '4320p'},
    },
    {
        id: 'vp9-profile2-10-1080p60',
        label: 'VP9 Profile 2 10bit',
        codec: 'VP9',
        profile: 'Profile 2',
        bit_depth: 10,
        width: 1440,
        height: 1080,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_1080P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('vp9', 10, {
            is_bs4k: false,
            streaming_quality: '1080p-60fps',
        }),
        konomitv_playback_video_codec: 'vp9',
        konomitv_playback_profile: {is_bs4k: false, streaming_quality: '1080p-60fps'},
    },
    {
        id: 'vp9-profile2-10-2160p60',
        label: 'VP9 Profile 2 10bit',
        codec: 'VP9',
        profile: 'Profile 2',
        bit_depth: 10,
        width: 3840,
        height: 2160,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_2160P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('vp9', 10, {
            is_bs4k: true,
            streaming_quality: '2160p',
        }),
        konomitv_playback_video_codec: 'vp9',
        konomitv_playback_profile: {is_bs4k: true, streaming_quality: '2160p'},
    },
    {
        id: 'vp9-profile2-10-4320p60',
        label: 'VP9 Profile 2 10bit',
        codec: 'VP9',
        profile: 'Profile 2',
        bit_depth: 10,
        width: 7680,
        height: 4320,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_4320P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('vp9', 10, {
            is_bs4k: true,
            streaming_quality: '4320p',
        }),
        konomitv_playback_video_codec: 'vp9',
        konomitv_playback_profile: {is_bs4k: true, streaming_quality: '4320p'},
    },
    {
        id: 'av1-main-8-1080p60',
        label: 'AV1 Main 8bit',
        codec: 'AV1',
        profile: 'Main',
        bit_depth: 8,
        width: 1440,
        height: 1080,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_1080P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('av1', 8, {
            is_bs4k: false,
            streaming_quality: '1080p-60fps',
        }),
        konomitv_playback_video_codec: 'av1',
        konomitv_playback_profile: {is_bs4k: false, streaming_quality: '1080p-60fps'},
    },
    {
        id: 'av1-main-8-2160p60',
        label: 'AV1 Main 8bit',
        codec: 'AV1',
        profile: 'Main',
        bit_depth: 8,
        width: 3840,
        height: 2160,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_2160P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('av1', 8, {
            is_bs4k: true,
            streaming_quality: '2160p',
        }),
        konomitv_playback_video_codec: 'av1',
        konomitv_playback_profile: {is_bs4k: true, streaming_quality: '2160p'},
    },
    {
        id: 'av1-main-8-4320p60',
        label: 'AV1 Main 8bit',
        codec: 'AV1',
        profile: 'Main',
        bit_depth: 8,
        width: 7680,
        height: 4320,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_4320P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('av1', 8, {
            is_bs4k: true,
            streaming_quality: '4320p',
        }),
        konomitv_playback_video_codec: 'av1',
        konomitv_playback_profile: {is_bs4k: true, streaming_quality: '4320p'},
    },
    {
        id: 'av1-main-10-1080p60',
        label: 'AV1 Main 10bit',
        codec: 'AV1',
        profile: 'Main',
        bit_depth: 10,
        width: 1440,
        height: 1080,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_1080P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('av1', 10, {
            is_bs4k: false,
            streaming_quality: '1080p-60fps',
        }),
        konomitv_playback_video_codec: 'av1',
        konomitv_playback_profile: {is_bs4k: false, streaming_quality: '1080p-60fps'},
    },
    {
        id: 'av1-main-10-2160p60',
        label: 'AV1 Main 10bit',
        codec: 'AV1',
        profile: 'Main',
        bit_depth: 10,
        width: 3840,
        height: 2160,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_2160P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('av1', 10, {
            is_bs4k: true,
            streaming_quality: '2160p',
        }),
        konomitv_playback_video_codec: 'av1',
        konomitv_playback_profile: {is_bs4k: true, streaming_quality: '2160p'},
    },
    {
        id: 'av1-main-10-4320p60',
        label: 'AV1 Main 10bit',
        codec: 'AV1',
        profile: 'Main',
        bit_depth: 10,
        width: 7680,
        height: 4320,
        framerate: 60,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_4320P60,
        used_by_konomitv_bs4k: true,
        mime_type: buildKonomiTVBS4KBrowserKonomiTVVideoMIME('av1', 10, {
            is_bs4k: true,
            streaming_quality: '4320p',
        }),
        konomitv_playback_video_codec: 'av1',
        konomitv_playback_profile: {is_bs4k: true, streaming_quality: '4320p'},
    },
    {
        id: 'vp8-8-480p30',
        label: 'VP8 8bit',
        codec: 'VP8',
        profile: '-',
        bit_depth: 8,
        width: 854,
        height: 480,
        framerate: 30,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_480P30,
        used_by_konomitv_bs4k: false,
        // VP8 は WebM でのみ現代的に使うため、診断用の固定 MIME を使う。
        mime_type: 'video/webm; codecs="vp8"',
    },
    {
        id: 'vp8-8-1080p30',
        label: 'VP8 8bit',
        codec: 'VP8',
        profile: '-',
        bit_depth: 8,
        width: 1920,
        height: 1080,
        framerate: 30,
        representative_bitrate: KONOMITV_BS4K_BROWSER_BITRATE_1080P30,
        used_by_konomitv_bs4k: false,
        mime_type: 'video/webm; codecs="vp8"',
    },
];

/**
 * 音声診断カタログ。
 * KonomiTV-BS4K 本線は AAC-LC と Opus のみ。残りは比較用の診断対象。
 */
export const KONOMITV_BS4K_BROWSER_AUDIO_CATALOG: readonly IKonomiTVBS4KBrowserAudioCatalogEntry[] = [
    {
        id: 'aac-lc',
        label: 'AAC-LC',
        codec: 'AAC-LC',
        used_by_konomitv_bs4k: true,
        // KonomiTV-BS4K 本線は PlayerUtils と同じ mp4a.40.2 を使う。
        probe_mime_type: PlayerUtils.getKonomiTVBS4KPlaybackAudioMIMEType('aac'),
        representative_bitrate: 192_000,
        representative_samplerate: 48_000,
        konomitv_playback_audio_codec: 'aac',
    },
    {
        id: 'aac-he',
        label: 'HE-AAC (AAC-Plus)',
        codec: 'HE-AAC',
        used_by_konomitv_bs4k: false,
        probe_mime_type: 'audio/mp4; codecs="mp4a.40.5"',
        representative_bitrate: 64_000,
        representative_samplerate: 48_000,
    },
    {
        id: 'mp3',
        label: 'MP3',
        codec: 'MP3',
        used_by_konomitv_bs4k: false,
        probe_mime_type: 'audio/mpeg',
        // MP3 の MIME には codecs パラメータがないため、WebCodecs 用の codec 文字列を明示する。
        web_codecs_codec: 'mp3',
        representative_bitrate: 192_000,
        representative_samplerate: 44_100,
    },
    {
        id: 'opus',
        label: 'Opus',
        codec: 'Opus',
        used_by_konomitv_bs4k: true,
        // MIME は実再生経路と同じく PlayerUtils の生成値を使う。Safari は ISO BMFF の
        // sample entry 名である大文字 Opus しか受け付けず、ブラウザ対応判定と実再生判定が割れるため。
        probe_mime_type: PlayerUtils.getKonomiTVBS4KPlaybackAudioMIMEType('opus'),
        // WebCodecs の codec string 登録値は小文字の opus のため、MIME とは別に保持する。
        web_codecs_codec: 'opus',
        representative_bitrate: 128_000,
        representative_samplerate: 48_000,
        konomitv_playback_audio_codec: 'opus',
    },
    {
        id: 'vorbis',
        label: 'Vorbis',
        codec: 'Vorbis',
        used_by_konomitv_bs4k: false,
        probe_mime_type: 'audio/webm; codecs="vorbis"',
        representative_bitrate: 128_000,
        representative_samplerate: 48_000,
    },
    {
        id: 'flac',
        label: 'FLAC',
        codec: 'FLAC',
        used_by_konomitv_bs4k: false,
        probe_mime_type: 'audio/flac',
        web_codecs_codec: 'flac',
        representative_bitrate: 1_000_000,
        representative_samplerate: 44_100,
    },
    {
        id: 'ac3',
        label: 'AC-3',
        codec: 'AC-3',
        used_by_konomitv_bs4k: false,
        probe_mime_type: 'audio/ac3',
        web_codecs_codec: 'ac-3',
        representative_bitrate: 448_000,
        representative_samplerate: 48_000,
    },
    {
        id: 'eac3',
        label: 'E-AC-3',
        codec: 'E-AC-3',
        used_by_konomitv_bs4k: false,
        // IANA 登録の elementary stream MIME は audio/eac3。audio/ec-3 は登録されていない。
        probe_mime_type: 'audio/eac3',
        // MP4 に格納する場合の codec identifier は ec-3。
        web_codecs_codec: 'ec-3',
        representative_bitrate: 640_000,
        representative_samplerate: 48_000,
    },
];

/** 診断で利用する Web API の存在確認結果。 */
export interface IKonomiTVBS4KBrowserCodecSupportApiPresence {
    media_source: boolean;
    managed_media_source: boolean;
    media_capabilities: boolean;
    web_codecs_video_decoder: boolean;
    web_codecs_video_encoder: boolean;
    web_codecs_audio_decoder: boolean;
    web_codecs_audio_encoder: boolean;
}

/** 実行環境の概要。結果 JSON の先頭と UI の概要カードに使う。 */
export interface IKonomiTVBS4KBrowserCodecSupportEnvironment {
    user_agent: string;
    engine: KonomiTVBS4KBrowserEngine;
    webgl_version: 'WebGL2' | 'WebGL1' | null;
    webgl_renderer: string | null;
    webgpu: boolean;
    apis: IKonomiTVBS4KBrowserCodecSupportApiPresence;
}

/** 映像 decode 1 行の診断結果。 */
export interface IKonomiTVBS4KBrowserVideoDecodeResult {
    id: string;
    label: string;
    codec: string;
    profile: string;
    bit_depth: 8 | 10;
    /** '1920x1080 60fps' のような検査構成の表示名。 */
    tested_configuration: string;
    representative_bitrate: number;
    used_by_konomitv_bs4k: boolean;
    browser_support: KonomiTVBS4KBrowserSupportStatus;
    browser_evidence: KonomiTVBS4KBrowserEvidence;
    hardware_estimate: KonomiTVBS4KBrowserHardwareEstimate;
    /** MediaCapabilities の powerEfficient。API 未対応時は null。参考値として表示する。 */
    power_efficient: boolean | null;
    web_codecs_prefer_hardware: KonomiTVBS4KBrowserWebCodecsProbe;
    web_codecs_no_preference: KonomiTVBS4KBrowserWebCodecsProbe;
    /** KonomiTV-BS4K の実再生経路 (MSE / ManagedMediaSource) で可能かどうか。比較行は null。 */
    konomitv_playback: boolean | null;
}

/** 映像 encode 1 行の診断結果。全行は「診断用」のラベルで表示する。 */
export interface IKonomiTVBS4KBrowserVideoEncodeResult {
    id: string;
    label: string;
    codec: string;
    tested_configuration: string;
    web_codecs_prefer_hardware: KonomiTVBS4KBrowserWebCodecsProbe;
    web_codecs_prefer_software: KonomiTVBS4KBrowserWebCodecsProbe;
    web_codecs_no_preference: KonomiTVBS4KBrowserWebCodecsProbe;
}

/** 音声 1 行の診断結果。 */
export interface IKonomiTVBS4KBrowserAudioResult {
    id: string;
    label: string;
    codec: string;
    used_by_konomitv_bs4k: boolean;
    browser_support: KonomiTVBS4KBrowserSupportStatus;
    browser_evidence: KonomiTVBS4KBrowserEvidence;
    web_codecs_decode: KonomiTVBS4KBrowserWebCodecsProbe;
    web_codecs_encode: KonomiTVBS4KBrowserWebCodecsProbe;
    /** KonomiTV-BS4K の実再生経路で可能かどうか。比較行は null。 */
    konomitv_playback: boolean | null;
}

/** 診断全体の結果。UI 状態とコピー JSON の両方にそのまま使う。 */
export interface IKonomiTVBS4KBrowserCodecSupportResult {
    environment: IKonomiTVBS4KBrowserCodecSupportEnvironment;
    video_decode: IKonomiTVBS4KBrowserVideoDecodeResult[];
    video_encode: IKonomiTVBS4KBrowserVideoEncodeResult[];
    audio: IKonomiTVBS4KBrowserAudioResult[];
}

// WebCodecs の config 形状は仕様の改訂で入れ子 { video: {...} } / { audio: {...} } 形状から
// フラット { codec, ... } 形状へ変わり、実装によってサポート形状が異なる。
// 両形状をここで定義し、プローブ時に両方試す。
interface IKonomiTVBS4KBrowserWebCodecsVideoConfig {
    codec?: string;
    codedWidth?: number;
    codedHeight?: number;
    width?: number;
    height?: number;
    bitrate?: number;
    framerate?: number;
    video?: {
        codec: string;
        codedWidth?: number;
        codedHeight?: number;
        width?: number;
        height?: number;
        bitrate?: number;
        framerate?: number;
    };
    hardwareAcceleration?: 'no-preference' | 'prefer-hardware' | 'prefer-software';
}

interface IKonomiTVBS4KBrowserWebCodecsAudioConfig {
    codec?: string;
    numberOfChannels?: number;
    sampleRate?: number;
    bitrate?: number;
    audio?: {
        codec: string;
        channels?: number;
        bitrate?: number;
        samplerate?: number;
    };
}

interface IKonomiTVBS4KBrowserWebCodecsAudioStatic {
    isConfigSupported(config: IKonomiTVBS4KBrowserWebCodecsAudioConfig): Promise<{
        supported?: boolean;
    }>;
}

// 個別プローブを有限時間で打ち切り、ハングした API があってもカタログの残りを診断する。
// Gecko の decodingInfo() は実 decoder の初期化を内部で直列化するため、短い timeout では
// 実在する対応証拠を失う。Firefox Android で AV1 の応答が 2.5 秒を超える実例があるため 15 秒にする。
const KONOMITV_BS4K_BROWSER_PROBE_TIMEOUT_MS = 15000;
const KONOMITV_BS4K_BROWSER_PROBE_CONCURRENCY = 6;
// MediaCapabilities は Gecko で内部直列化されるため、証拠を失わないよう常に 1 件ずつ実行する。
const KONOMITV_BS4K_BROWSER_MEDIA_CAPABILITIES_CONCURRENCY = 1;

/** 個別プローブの終了状態。Rejected / TimedOut は行単位の Unavailable として扱う。 */
export type KonomiTVBS4KBrowserProbeOutcome<T> =
    {status: 'Resolved'; value: T} |
    {status: 'TimedOut' | 'Rejected' | 'Aborted'};

// supported: true が返った WebCodecs config 形状を以降の行でも使い、2 形状の直列試行を避ける。
// supported: false の形状は実装が未知の形状を「非対応」と解決している可能性があるため採用しない。
let konomitv_bs4k_browser_video_decoder_shape: 'CodedSize' | 'WidthHeight' | 'Nested' | null = null;
let konomitv_bs4k_browser_video_encoder_shape: 'WidthHeight' | 'Nested' | null = null;

async function withKonomiTVBS4KBrowserProbeTimeout<T>(
    probe: Promise<T>,
    signal?: AbortSignal,
): Promise<KonomiTVBS4KBrowserProbeOutcome<T>> {
    if (signal?.aborted === true) {
        return {status: 'Aborted'};
    }
    let timeout_handle: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<KonomiTVBS4KBrowserProbeOutcome<T>>(resolve => {
        timeout_handle = setTimeout(
            () => resolve({status: 'TimedOut'}),
            KONOMITV_BS4K_BROWSER_PROBE_TIMEOUT_MS,
        );
    });
    let on_abort: (() => void) | undefined;
    const abort = signal === undefined ? null : new Promise<KonomiTVBS4KBrowserProbeOutcome<T>>(resolve => {
        on_abort = (): void => resolve({status: 'Aborted'});
        signal.addEventListener('abort', on_abort, {once: true});
    });
    // isConfigSupported / decodingInfo は設定不正などで reject するため、行単位では Unavailable 扱い。
    const resolved = probe.then<KonomiTVBS4KBrowserProbeOutcome<T>, KonomiTVBS4KBrowserProbeOutcome<T>>(
        value => ({status: 'Resolved', value}),
        () => ({status: 'Rejected'}),
    );
    try {
        return await Promise.race(abort === null ? [resolved, timeout] : [resolved, timeout, abort]);
    } finally {
        if (timeout_handle !== undefined) {
            clearTimeout(timeout_handle);
        }
        if (on_abort !== undefined) {
            signal?.removeEventListener('abort', on_abort);
        }
    }
}

async function mapKonomiTVBS4KBrowserProbes<Item, Result>(
    items: readonly Item[],
    mapper: (item: Item, index: number) => Promise<Result>,
    signal: AbortSignal,
    concurrency: number = KONOMITV_BS4K_BROWSER_PROBE_CONCURRENCY,
): Promise<Result[]> {
    const results: Result[] = new Array(items.length);
    let next_index = 0;
    const worker_count = Math.min(concurrency, items.length);
    async function worker(): Promise<void> {
        while (next_index < items.length) {
            if (signal.aborted === true) {
                throw new DOMException('Browser codec diagnostics aborted.', 'AbortError');
            }
            const index = next_index;
            next_index += 1;
            results[index] = await mapper(items[index], index);
        }
    }
    await Promise.all(Array.from({length: worker_count}, () => worker()));
    return results;
}

/**
 * 実行環境の概要を検出する。
 *
 * WebGL レンダラは GPU の「参考情報」であり、HW アクセラレーションの判定根拠には使わない。
 * 実際の HW 推定は WebCodecs / MediaCapabilities の API 結果から行うため。
 */
export function detectKonomiTVBS4KBrowserCodecSupportEnvironment():
IKonomiTVBS4KBrowserCodecSupportEnvironment {
    // Utils の UA 判定をそのまま使う。3 系すべてに該当しない UA は Unknown。
    let engine: KonomiTVBS4KBrowserEngine = 'Unknown';
    if (Utils.isChromium() === true) {
        engine = 'Chromium';
    } else if (Utils.isFirefox() === true) {
        engine = 'Gecko';
    } else if (Utils.isSafari() === true) {
        engine = 'WebKit';
    }

    let webgl_version: IKonomiTVBS4KBrowserCodecSupportEnvironment['webgl_version'] = null;
    let webgl_renderer: string | null = null;
    try {
        const canvas = document.createElement('canvas');
        const webgl2_context = canvas.getContext('webgl2');
        const webgl1_context = webgl2_context === null ? canvas.getContext('webgl') : null;
        const context = webgl2_context ?? webgl1_context;
        webgl_version = webgl2_context !== null ? 'WebGL2' : (webgl1_context !== null ? 'WebGL1' : null);
        if (context !== null) {
            // 拡張が使えるなら GPU 名、使えないならブラウザが露出する抽象名を表示する。
            const debug_extension = context.getExtension('WEBGL_debug_renderer_info');
            let renderer: unknown = null;
            if (debug_extension !== null) {
                renderer = context.getParameter(debug_extension.UNMASKED_RENDERER_WEBGL);
            } else {
                renderer = context.getParameter(context.RENDERER);
            }
            if (typeof renderer === 'string' && renderer.length > 0) {
                webgl_renderer = renderer;
            }
        }
    } catch {
        // canvas 生成やコンテキスト取得に失敗しても診断全体は続ける。
    }

    const webgpu_navigator = navigator as Navigator & {gpu?: unknown};
    return {
        user_agent: navigator.userAgent,
        engine,
        webgl_version,
        webgl_renderer,
        webgpu: webgpu_navigator.gpu !== undefined,
        apis: {
            media_source: window.MediaSource !== undefined,
            managed_media_source: (
                window as Window & {ManagedMediaSource?: unknown}
            ).ManagedMediaSource !== undefined,
            media_capabilities: navigator.mediaCapabilities !== undefined,
            web_codecs_video_decoder: typeof VideoDecoder !== 'undefined',
            web_codecs_video_encoder: typeof VideoEncoder !== 'undefined',
            web_codecs_audio_decoder: (
                globalThis as {AudioDecoder?: unknown}
            ).AudioDecoder !== undefined,
            web_codecs_audio_encoder: (
                globalThis as {AudioEncoder?: unknown}
            ).AudioEncoder !== undefined,
        },
    };
}

/**
 * 映像 decode の対応判定を行う。
 *
 * 信頼度で MediaCapabilities > MediaSource / ManagedMediaSource > canPlayType を優先し、
 * 優先度の高いソースと低いソースの報告が矛盾する場合は断定せず Unknown を返す。
 */
export function judgeKonomiTVBS4KBrowserVideoSupport(params: {
    media_capabilities: KonomiTVBS4KBrowserWebApiProbe;
    media_source: KonomiTVBS4KBrowserWebApiProbe;
    can_play_type: 'Probably' | 'Maybe' | 'No';
}): {
        support: KonomiTVBS4KBrowserSupportStatus;
        evidence: KonomiTVBS4KBrowserEvidence;
    } {
    const {media_capabilities, media_source, can_play_type} = params;
    if (media_capabilities === 'Supported') {
        // MediaCapabilities が対応でも MSE が非対応なら実再生経路と矛盾するので断定しない。
        if (media_source === 'Unsupported') {
            return {support: 'Unknown', evidence: 'MediaCapabilities'};
        }
        return {support: 'Supported', evidence: 'MediaCapabilities'};
    }
    if (media_capabilities === 'Unsupported') {
        // MediaCapabilities が「非対応」なのに他 API が「対応」を示す場合は実装差とみなす。
        if (media_source === 'Supported' || can_play_type !== 'No') {
            return {support: 'Unknown', evidence: 'MediaCapabilities'};
        }
        return {support: 'Unsupported', evidence: 'MediaCapabilities'};
    }
    // MediaCapabilities がない環境。MSE の isTypeSupported は実再生経路を直接示すため次優先。
    if (media_source === 'Supported') {
        return {support: 'Likely', evidence: 'MediaSource'};
    }
    if (media_source === 'Unsupported') {
        if (can_play_type !== 'No') {
            return {support: 'Unknown', evidence: 'MediaSource'};
        }
        return {support: 'Unsupported', evidence: 'MediaSource'};
    }
    // canPlayType のみ。'probably' / 'maybe' は推定対応に留める。
    if (can_play_type === 'No') {
        return {support: 'Unsupported', evidence: 'CanPlayType'};
    }
    return {support: 'Likely', evidence: 'CanPlayType'};
}

/**
 * 音声の対応判定を行う。
 * 音声は MSE 列を持たないため、MediaCapabilities > canPlayType の 2 段で判定する。
 */
export function judgeKonomiTVBS4KBrowserAudioSupport(params: {
    media_capabilities: KonomiTVBS4KBrowserWebApiProbe;
    can_play_type: 'Probably' | 'Maybe' | 'No';
}): {
        support: KonomiTVBS4KBrowserSupportStatus;
        evidence: KonomiTVBS4KBrowserEvidence;
    } {
    const {media_capabilities, can_play_type} = params;
    if (media_capabilities === 'Supported') {
        return {support: 'Supported', evidence: 'MediaCapabilities'};
    }
    if (media_capabilities === 'Unsupported') {
        if (can_play_type !== 'No') {
            return {support: 'Unknown', evidence: 'MediaCapabilities'};
        }
        return {support: 'Unsupported', evidence: 'MediaCapabilities'};
    }
    if (can_play_type === 'No') {
        return {support: 'Unsupported', evidence: 'CanPlayType'};
    }
    return {support: 'Likely', evidence: 'CanPlayType'};
}

/**
 * 映像のハードウェアアクセラレーションを推定する。
 *
 * エンジン別の主要根拠:
 * - Chromium: WebCodecs の prefer-hardware 結果が主。未利用時は MediaCapabilities の powerEfficient。
 * - Gecko / WebKit: MediaCapabilities の powerEfficient が主。
 * - Unknown: WebCodecs と powerEfficient が揃って一致した時のみ断定する。
 */
export function estimateKonomiTVBS4KBrowserVideoHardware(params: {
    engine: KonomiTVBS4KBrowserEngine;
    browser_support: KonomiTVBS4KBrowserSupportStatus;
    power_efficient: boolean | null;
    web_codecs_prefer_hardware: KonomiTVBS4KBrowserWebCodecsProbe;
    web_codecs_no_preference: KonomiTVBS4KBrowserWebCodecsProbe;
}): KonomiTVBS4KBrowserHardwareEstimate {
    const {engine, browser_support, power_efficient, web_codecs_prefer_hardware, web_codecs_no_preference} = params;
    if (browser_support === 'Unsupported') {
        return 'NotApplicable';
    }
    if (browser_support === 'Unknown') {
        return 'Unknown';
    }
    if (engine === 'Chromium') {
        // WebCodecs の prefer-hardware が「対応」なら HW 推定、
        // prefer-hardware 非対応で通常設定は対応なら SW 推定。
        if (web_codecs_prefer_hardware === 'Supported') {
            return 'HardwareLikely';
        }
        if (web_codecs_prefer_hardware === 'Unsupported') {
            if (web_codecs_no_preference === 'Supported') {
                return 'SoftwareLikely';
            }
            return 'Unknown';
        }
        // WebCodecs 未対応環境は powerEfficient にフォールバックする。
        if (power_efficient === true) {
            return 'HardwareLikely';
        }
        if (power_efficient === false) {
            return 'SoftwareLikely';
        }
        return 'Unknown';
    }
    if (engine === 'Gecko' || engine === 'WebKit') {
        if (power_efficient === true) {
            return 'HardwareLikely';
        }
        if (power_efficient === false) {
            return 'SoftwareLikely';
        }
        return 'Unknown';
    }
    // Unknown エンジン: 両ソースが揃って一致した時のみ推定する。
    if (web_codecs_prefer_hardware === 'Supported' && power_efficient === true) {
        return 'HardwareLikely';
    }
    if (web_codecs_no_preference === 'Supported' && power_efficient === false) {
        return 'SoftwareLikely';
    }
    return 'Unknown';
}

function extractKonomiTVBS4KBrowserMIMECodec(mime_type: string): string {
    const match = mime_type.match(/\bcodecs\s*=\s*"([^"]+)"/i);
    if (match === null) {
        throw new Error(`MIME type does not contain a codecs parameter: ${mime_type}`);
    }
    return match[1];
}

function probeKonomiTVBS4KBrowserCanPlayType(
    video_element: HTMLVideoElement,
    mime_type: string,
): 'Probably' | 'Maybe' | 'No' {
    try {
        const result = video_element.canPlayType(mime_type);
        if (result === 'probably') return 'Probably';
        if (result === 'maybe') return 'Maybe';
        return 'No';
    } catch {
        // canPlayType が極端に短い MIME でも例外を出す実装があるため、行単位では No 扱い。
        return 'No';
    }
}

/** MediaCapabilities プローブの詳細結果。失敗時は理由を保持し、判定材料を失わないようにする。 */
interface IKonomiTVBS4KBrowserMediaCapabilitiesResult {
    probe: KonomiTVBS4KBrowserWebApiProbe;
    power_efficient: boolean | null;
    failure: 'TimedOut' | 'Rejected' | 'Aborted' | null;
}

async function probeKonomiTVBS4KBrowserMediaCapabilitiesVideo(params: {
    available: boolean;
    mime_type: string;
    width: number;
    height: number;
    bitrate: number;
    framerate: number;
    signal?: AbortSignal;
}): Promise<IKonomiTVBS4KBrowserMediaCapabilitiesResult> {
    if (params.available === false) {
        return {probe: 'Unavailable', power_efficient: null, failure: null};
    }
    const outcome = await withKonomiTVBS4KBrowserProbeTimeout(
        navigator.mediaCapabilities.decodingInfo({
            type: 'media-source',
            video: {
                contentType: params.mime_type,
                width: params.width,
                height: params.height,
                bitrate: params.bitrate,
                framerate: params.framerate,
            },
        }),
        params.signal,
    );
    if (outcome.status !== 'Resolved') {
        return {probe: 'Unavailable', power_efficient: null, failure: outcome.status};
    }
    return {
        probe: outcome.value.supported === true ? 'Supported' : 'Unsupported',
        power_efficient: outcome.value.powerEfficient,
        failure: null,
    };
}

async function probeKonomiTVBS4KBrowserMediaCapabilitiesAudio(params: {
    available: boolean;
    mime_type: string;
    bitrate: number;
    samplerate: number;
    // 本線コーデック (AAC-LC / Opus) は実再生経路である MSE ('media-source') で照会し、
    // 比較用コーデックはブラウザの decode 能力そのものを見るため 'file' で照会する。
    // Gecko の MSE は audio/mpeg などを受け付けないため、比較用まで 'media-source' で
    // 照会すると decode 可能なのに非対応・不明と表示されてしまう。
    media_capabilities_type: 'media-source' | 'file';
    signal?: AbortSignal;
}): Promise<IKonomiTVBS4KBrowserMediaCapabilitiesResult> {
    if (params.available === false) {
        return {probe: 'Unavailable', power_efficient: null, failure: null};
    }
    const outcome = await withKonomiTVBS4KBrowserProbeTimeout(
        navigator.mediaCapabilities.decodingInfo({
            type: params.media_capabilities_type,
            audio: {
                contentType: params.mime_type,
                channels: '2',
                bitrate: params.bitrate,
                samplerate: params.samplerate,
            },
        }),
        params.signal,
    );
    if (outcome.status !== 'Resolved') {
        return {probe: 'Unavailable', power_efficient: null, failure: outcome.status};
    }
    return {
        probe: outcome.value.supported === true ? 'Supported' : 'Unsupported',
        power_efficient: outcome.value.powerEfficient,
        failure: null,
    };
}

function probeKonomiTVBS4KBrowserMediaSource(mime_type: string): KonomiTVBS4KBrowserWebApiProbe {
    const media_source_api = window.MediaSource;
    const managed_media_source_api = (
        window as Window & {
            ManagedMediaSource?: {isTypeSupported: (mime_type: string) => boolean};
        }
    ).ManagedMediaSource;
    if (media_source_api === undefined && managed_media_source_api === undefined) {
        return 'Unavailable';
    }
    // PlayerUtils と同じく、両 API が共存する環境では両方が true のときのみ対応扱いにする。
    const media_source_ok =
        media_source_api === undefined ||
        media_source_api.isTypeSupported(mime_type) === true;
    const managed_media_source_ok =
        managed_media_source_api === undefined ||
        managed_media_source_api.isTypeSupported(mime_type) === true;
    return media_source_ok && managed_media_source_ok ? 'Supported' : 'Unsupported';
}

/**
 * 新旧両方の config 形状で isConfigSupported を試し、
 * 拒否されない形状の結果を返す。
 * どちらの形状も reject した場合は Unavailable。
 */
function withKonomiTVBS4KBrowserHardwareAcceleration(
    config: IKonomiTVBS4KBrowserWebCodecsVideoConfig,
    hardware_acceleration?: 'prefer-hardware' | 'prefer-software' | 'no-preference',
): IKonomiTVBS4KBrowserWebCodecsVideoConfig {
    if (hardware_acceleration === undefined) {
        return config;
    }
    return {...config, hardwareAcceleration: hardware_acceleration};
}

async function probeKonomiTVBS4KBrowserVideoDecoderWebCodecs(params: {
    mime_type: string;
    codec: string;
    width: number;
    height: number;
    hardware_acceleration?: 'prefer-hardware' | 'prefer-software' | 'no-preference';
    signal?: AbortSignal;
}): Promise<KonomiTVBS4KBrowserWebCodecsProbe> {
    // WebCodecs の VideoDecoderConfig に type フィールドは存在しない。未知フィールドを拒否する
    // 実装で全行が N/A にならないよう、MediaCapabilities 用の container 情報は渡さない。
    const candidates: Array<{
        shape: 'CodedSize' | 'WidthHeight' | 'Nested';
        config: IKonomiTVBS4KBrowserWebCodecsVideoConfig;
    }> = [
        {
            // 現行 VideoDecoderConfig は codedWidth / codedHeight で解像度を見る。
            shape: 'CodedSize',
            config: withKonomiTVBS4KBrowserHardwareAcceleration({
                codec: params.codec,
                codedWidth: params.width,
                codedHeight: params.height,
            }, params.hardware_acceleration),
        },
        {
            shape: 'WidthHeight',
            config: withKonomiTVBS4KBrowserHardwareAcceleration({
                codec: params.codec,
                width: params.width,
                height: params.height,
            }, params.hardware_acceleration),
        },
        {
            shape: 'Nested',
            config: withKonomiTVBS4KBrowserHardwareAcceleration({
                video: {
                    codec: params.codec,
                    codedWidth: params.width,
                    codedHeight: params.height,
                    width: params.width,
                    height: params.height,
                },
            }, params.hardware_acceleration),
        },
    ];
    const preferred = konomitv_bs4k_browser_video_decoder_shape;
    const ordered = preferred === null ?
        candidates :
        [
            ...candidates.filter(candidate => candidate.shape === preferred),
            ...candidates.filter(candidate => candidate.shape !== preferred),
        ];
    // 形状の固定は supported: true が返った形状だけにする。supported: false で固定すると、
    // その形状を「非対応」と解決する実装で残りの形状を試さず全行が誤った形状に張り付くため。
    let resolved_unsupported = false;
    for (const candidate of ordered) {
        const outcome = await withKonomiTVBS4KBrowserProbeTimeout(
            VideoDecoder.isConfigSupported(candidate.config as unknown as VideoDecoderConfig),
            params.signal,
        );
        if (outcome.status === 'Resolved') {
            if (outcome.value.supported === true) {
                konomitv_bs4k_browser_video_decoder_shape = candidate.shape;
                return 'Supported';
            }
            resolved_unsupported = true;
        }
    }
    return resolved_unsupported === true ? 'Unsupported' : 'Unavailable';
}

async function probeKonomiTVBS4KBrowserVideoEncoderWebCodecs(params: {
    mime_type: string;
    codec: string;
    width: number;
    height: number;
    bitrate: number;
    framerate: number;
    hardware_acceleration?: 'prefer-hardware' | 'prefer-software' | 'no-preference';
    signal?: AbortSignal;
}): Promise<KonomiTVBS4KBrowserWebCodecsProbe> {
    const candidates: Array<{
        shape: 'WidthHeight' | 'Nested';
        config: IKonomiTVBS4KBrowserWebCodecsVideoConfig;
    }> = [
        {
            // VideoEncoderConfig は width / height / bitrate / framerate が正本。
            shape: 'WidthHeight',
            config: withKonomiTVBS4KBrowserHardwareAcceleration({
                codec: params.codec,
                width: params.width,
                height: params.height,
                bitrate: params.bitrate,
                framerate: params.framerate,
            }, params.hardware_acceleration),
        },
        {
            shape: 'Nested',
            config: withKonomiTVBS4KBrowserHardwareAcceleration({
                video: {
                    codec: params.codec,
                    width: params.width,
                    height: params.height,
                    bitrate: params.bitrate,
                    framerate: params.framerate,
                },
            }, params.hardware_acceleration),
        },
    ];
    const preferred = konomitv_bs4k_browser_video_encoder_shape;
    const ordered = preferred === null ?
        candidates :
        [
            ...candidates.filter(candidate => candidate.shape === preferred),
            ...candidates.filter(candidate => candidate.shape !== preferred),
        ];
    // decoder と同じく、形状の固定は supported: true が返った形状だけにする
    let resolved_unsupported = false;
    for (const candidate of ordered) {
        const outcome = await withKonomiTVBS4KBrowserProbeTimeout(
            VideoEncoder.isConfigSupported(candidate.config as unknown as VideoEncoderConfig),
            params.signal,
        );
        if (outcome.status === 'Resolved') {
            if (outcome.value.supported === true) {
                konomitv_bs4k_browser_video_encoder_shape = candidate.shape;
                return 'Supported';
            }
            resolved_unsupported = true;
        }
    }
    return resolved_unsupported === true ? 'Unsupported' : 'Unavailable';
}

async function probeKonomiTVBS4KBrowserVideoCodec(params: {
    available: boolean;
    mime_type: string;
    codec: string;
    width: number;
    height: number;
    bitrate: number;
    framerate: number;
    hardware_acceleration?: 'prefer-hardware' | 'prefer-software' | 'no-preference';
    signal?: AbortSignal;
}): Promise<KonomiTVBS4KBrowserWebCodecsProbe> {
    if (params.available === false) {
        return 'Unavailable';
    }
    return await probeKonomiTVBS4KBrowserVideoDecoderWebCodecs({
        mime_type: params.mime_type,
        codec: params.codec,
        width: params.width,
        height: params.height,
        hardware_acceleration: params.hardware_acceleration,
        signal: params.signal,
    });
}

async function probeKonomiTVBS4KBrowserVideoEncoder(params: {
    available: boolean;
    mime_type: string;
    codec: string;
    width: number;
    height: number;
    bitrate: number;
    framerate: number;
    hardware_acceleration?: 'prefer-hardware' | 'prefer-software' | 'no-preference';
    signal?: AbortSignal;
}): Promise<KonomiTVBS4KBrowserWebCodecsProbe> {
    if (params.available === false) {
        return 'Unavailable';
    }
    return await probeKonomiTVBS4KBrowserVideoEncoderWebCodecs(params);
}

async function probeKonomiTVBS4KBrowserAudioWebCodecs(params: {
    api: 'AudioDecoder' | 'AudioEncoder';
    mime_type: string;
    codec: string;
    bitrate: number;
    samplerate: number;
    signal?: AbortSignal;
}): Promise<KonomiTVBS4KBrowserWebCodecsProbe> {
    // AudioDecoder / AudioEncoder は TS 5.5 の lib.dom に未定義のため、ローカル型で呼ぶ。
    const audio_apis = (globalThis as {
        AudioDecoder?: IKonomiTVBS4KBrowserWebCodecsAudioStatic;
        AudioEncoder?: IKonomiTVBS4KBrowserWebCodecsAudioStatic;
    });
    const target_api = audio_apis[params.api];
    if (target_api === undefined) {
        return 'Unavailable';
    }
    // AudioDecoderConfig は仕様どおり codec / numberOfChannels / sampleRate が必須。
    // 入れ子形状は古い実験実装向けのフォールバック。type は MediaCapabilities 用の情報で
    // WebCodecs の config には存在しないため渡さない。
    const flat_config: IKonomiTVBS4KBrowserWebCodecsAudioConfig = {
        codec: params.codec,
        numberOfChannels: 2,
        sampleRate: params.samplerate,
        bitrate: params.bitrate,
    };
    const nested_config: IKonomiTVBS4KBrowserWebCodecsAudioConfig = {
        audio: {
            codec: params.codec,
            channels: 2,
            bitrate: params.bitrate,
            samplerate: params.samplerate,
        },
    };
    // flat が supported: false で解決しても nested を試す。false で打ち切ると、
    // 未知の形状を「非対応」と解決する実装で誤った非対応表示になるため。
    let resolved_unsupported = false;
    for (const config of [flat_config, nested_config]) {
        const outcome = await withKonomiTVBS4KBrowserProbeTimeout(
            target_api.isConfigSupported(config),
            params.signal,
        );
        if (outcome.status === 'Resolved') {
            if (outcome.value.supported === true) {
                return 'Supported';
            }
            resolved_unsupported = true;
        }
    }
    return resolved_unsupported === true ? 'Unsupported' : 'Unavailable';
}

/**
 * 全カタログに対してブラウザ診断を実行する。
 *
 * 1 行の失敗が他の行へ波及しない。ハングした API で画面が止まらないよう、
 * 行は並列に評価し、各 API の timeout と呼び出し元の AbortSignal で打ち切る。
 */
export async function runKonomiTVBS4KBrowserCodecSupportDiagnostics(
    signal?: AbortSignal,
): Promise<IKonomiTVBS4KBrowserCodecSupportResult> {
    // 各 API 呼び出しは個別 timeout を持つため、全体 timeout で後半の行を欠落させない。
    const diagnostics_signal = signal ?? new AbortController().signal;
    const environment = detectKonomiTVBS4KBrowserCodecSupportEnvironment();
    const video_element = document.createElement('video');
    // 前回の診断で採用した WebCodecs config 形状のキャッシュを捨て、今回の環境で再判定する
    konomitv_bs4k_browser_video_decoder_shape = null;
    konomitv_bs4k_browser_video_encoder_shape = null;

    // MediaCapabilities を先に全行で確定させる。Gecko の decodingInfo() は実 decoder の
    // 初期化を内部で直列化し、6 並列の呼び出しでは全行が timeout して証拠を失うため、
    // この API だけは他のプローブと分離して 1 件ずつ実行する。
    const video_media_capabilities = await mapKonomiTVBS4KBrowserProbes(
        KONOMITV_BS4K_BROWSER_VIDEO_CATALOG,
        async (entry) => await probeKonomiTVBS4KBrowserMediaCapabilitiesVideo({
            available: environment.apis.media_capabilities,
            mime_type: entry.mime_type,
            width: entry.width,
            height: entry.height,
            bitrate: entry.representative_bitrate,
            framerate: entry.framerate,
            signal: diagnostics_signal,
        }),
        diagnostics_signal,
        KONOMITV_BS4K_BROWSER_MEDIA_CAPABILITIES_CONCURRENCY,
    );
    const audio_media_capabilities = await mapKonomiTVBS4KBrowserProbes(
        KONOMITV_BS4K_BROWSER_AUDIO_CATALOG,
        async (entry) => await probeKonomiTVBS4KBrowserMediaCapabilitiesAudio({
            available: environment.apis.media_capabilities,
            mime_type: entry.probe_mime_type,
            bitrate: entry.representative_bitrate,
            samplerate: entry.representative_samplerate,
            // 本線は MSE 経路、比較用はファイル再生としての decode 能力を照会する
            media_capabilities_type: entry.used_by_konomitv_bs4k === true ? 'media-source' : 'file',
            signal: diagnostics_signal,
        }),
        diagnostics_signal,
        KONOMITV_BS4K_BROWSER_MEDIA_CAPABILITIES_CONCURRENCY,
    );

    const video_decode = await mapKonomiTVBS4KBrowserProbes(
        KONOMITV_BS4K_BROWSER_VIDEO_CATALOG,
        async (entry, index) => {
            const tested_configuration =
                `${entry.width}x${entry.height} ${entry.framerate}fps`;
            const media_capabilities = video_media_capabilities[index];
            const media_source = probeKonomiTVBS4KBrowserMediaSource(entry.mime_type);
            const can_play_type = probeKonomiTVBS4KBrowserCanPlayType(video_element, entry.mime_type);
            const web_codecs_prefer_hardware =
                await probeKonomiTVBS4KBrowserVideoCodec({
                    available: environment.apis.web_codecs_video_decoder,
                    mime_type: entry.mime_type,
                    codec: extractKonomiTVBS4KBrowserMIMECodec(entry.mime_type),
                    width: entry.width,
                    height: entry.height,
                    bitrate: entry.representative_bitrate,
                    framerate: entry.framerate,
                    hardware_acceleration: 'prefer-hardware',
                    signal: diagnostics_signal,
                });
            const web_codecs_no_preference =
                await probeKonomiTVBS4KBrowserVideoCodec({
                    available: environment.apis.web_codecs_video_decoder,
                    mime_type: entry.mime_type,
                    codec: extractKonomiTVBS4KBrowserMIMECodec(entry.mime_type),
                    width: entry.width,
                    height: entry.height,
                    bitrate: entry.representative_bitrate,
                    framerate: entry.framerate,
                    signal: diagnostics_signal,
                });
            const judgment = judgeKonomiTVBS4KBrowserVideoSupport({
                media_capabilities: media_capabilities.probe,
                media_source,
                can_play_type,
            });
            const hardware_estimate = estimateKonomiTVBS4KBrowserVideoHardware({
                engine: environment.engine,
                browser_support: judgment.support,
                power_efficient: media_capabilities.power_efficient,
                web_codecs_prefer_hardware,
                web_codecs_no_preference,
            });
            let konomitv_playback: boolean | null = null;
            if (
                entry.konomitv_playback_video_codec !== undefined &&
                entry.konomitv_playback_profile !== undefined
            ) {
                // 実再生経路の判定を PlayerUtils へ一元化し、診断と再生で結論が分岐しないようにする。
                konomitv_playback = PlayerUtils.isKonomiTVBS4KPlaybackVideoCodecSupported(
                    entry.konomitv_playback_video_codec,
                    entry.bit_depth,
                    entry.konomitv_playback_profile,
                );
            }
            return {
                id: entry.id,
                label: entry.label,
                codec: entry.codec,
                profile: entry.profile,
                bit_depth: entry.bit_depth,
                tested_configuration,
                representative_bitrate: entry.representative_bitrate,
                used_by_konomitv_bs4k: entry.used_by_konomitv_bs4k,
                browser_support: judgment.support,
                browser_evidence: judgment.evidence,
                hardware_estimate,
                power_efficient: media_capabilities.power_efficient,
                web_codecs_prefer_hardware,
                web_codecs_no_preference,
                konomitv_playback,
            };
        },
        diagnostics_signal,
    );

    const video_encode = await mapKonomiTVBS4KBrowserProbes(
        KONOMITV_BS4K_BROWSER_VIDEO_CATALOG,
        async (entry) => {
            const tested_configuration =
                `${entry.width}x${entry.height} ${entry.framerate}fps`;
            return {
                id: entry.id,
                label: entry.label,
                codec: entry.codec,
                tested_configuration,
                web_codecs_prefer_hardware:
                    await probeKonomiTVBS4KBrowserVideoEncoder({
                        available: environment.apis.web_codecs_video_encoder,
                        mime_type: entry.mime_type,
                        codec: extractKonomiTVBS4KBrowserMIMECodec(entry.mime_type),
                        width: entry.width,
                        height: entry.height,
                        bitrate: entry.representative_bitrate,
                        framerate: entry.framerate,
                        hardware_acceleration: 'prefer-hardware',
                        signal: diagnostics_signal,
                    }),
                web_codecs_prefer_software:
                    await probeKonomiTVBS4KBrowserVideoEncoder({
                        available: environment.apis.web_codecs_video_encoder,
                        mime_type: entry.mime_type,
                        codec: extractKonomiTVBS4KBrowserMIMECodec(entry.mime_type),
                        width: entry.width,
                        height: entry.height,
                        bitrate: entry.representative_bitrate,
                        framerate: entry.framerate,
                        hardware_acceleration: 'prefer-software',
                        signal: diagnostics_signal,
                    }),
                web_codecs_no_preference:
                    await probeKonomiTVBS4KBrowserVideoEncoder({
                        available: environment.apis.web_codecs_video_encoder,
                        mime_type: entry.mime_type,
                        codec: extractKonomiTVBS4KBrowserMIMECodec(entry.mime_type),
                        width: entry.width,
                        height: entry.height,
                        bitrate: entry.representative_bitrate,
                        framerate: entry.framerate,
                        signal: diagnostics_signal,
                    }),
            };
        },
        diagnostics_signal,
    );

    const audio = await mapKonomiTVBS4KBrowserProbes(
        KONOMITV_BS4K_BROWSER_AUDIO_CATALOG,
        async (entry, index) => {
            const media_capabilities = audio_media_capabilities[index];
            const can_play_type = probeKonomiTVBS4KBrowserCanPlayType(video_element, entry.probe_mime_type);
            const web_codecs_decode =
                await probeKonomiTVBS4KBrowserAudioWebCodecs({
                    api: 'AudioDecoder',
                    mime_type: entry.probe_mime_type,
                    codec: entry.web_codecs_codec ?? extractKonomiTVBS4KBrowserMIMECodec(entry.probe_mime_type),
                    bitrate: entry.representative_bitrate,
                    samplerate: entry.representative_samplerate,
                    signal: diagnostics_signal,
                });
            const web_codecs_encode =
                await probeKonomiTVBS4KBrowserAudioWebCodecs({
                    api: 'AudioEncoder',
                    mime_type: entry.probe_mime_type,
                    codec: entry.web_codecs_codec ?? extractKonomiTVBS4KBrowserMIMECodec(entry.probe_mime_type),
                    bitrate: entry.representative_bitrate,
                    samplerate: entry.representative_samplerate,
                    signal: diagnostics_signal,
                });
            const judgment = judgeKonomiTVBS4KBrowserAudioSupport({
                media_capabilities: media_capabilities.probe,
                can_play_type,
            });
            let konomitv_playback: boolean | null = null;
            if (entry.konomitv_playback_audio_codec !== undefined) {
                konomitv_playback = PlayerUtils.isKonomiTVBS4KPlaybackAudioCodecSupported(
                    entry.konomitv_playback_audio_codec,
                );
            }
            return {
                id: entry.id,
                label: entry.label,
                codec: entry.codec,
                used_by_konomitv_bs4k: entry.used_by_konomitv_bs4k,
                browser_support: judgment.support,
                browser_evidence: judgment.evidence,
                web_codecs_decode,
                web_codecs_encode,
                konomitv_playback,
            };
        },
        diagnostics_signal,
    );

    return {environment, video_decode, video_encode, audio};
}
