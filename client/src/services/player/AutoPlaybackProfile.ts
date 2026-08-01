/**
 * 自動画質選択モードの解決ロジック。
 *
 * - 画質: min(ユーザー上限, ソース解像度上限, 回線推定上限) のラダー上で最高段
 * - 視聴中はバッファ停滞・waiting に応じて 1 段ずつ下げる（PlayerController）
 * - 映像 codec: AV1 → VP9 → HEVC → AVC（サーバ∩端末 MSE）
 * - 音声 codec: Opus → AAC（ライブ・録画とも。使えるなら Opus 優先）
 * - 低遅延: ネットワーク品質・速度のみ（既存設定・画質/codec は見ない）
 * - 24fps: 常に false
 */

import type {
    IKonomiTVBS4KPlaybackCapabilities,
    IKonomiTVBS4KPlaybackEncoder,
    IKonomiTVBS4KResolvedPlaybackCombination,
} from '@/services/Videos';
import type {
    BS4KLiveStreamingQuality,
    IKonomiTVBS4KPlaybackVideoProfile,
    KonomiTVBS4KPlaybackAudioCodec,
    KonomiTVBS4KPlaybackStreamingQuality,
    KonomiTVBS4KPlaybackVideoCodec,
} from '@/stores/SettingsStore';

import Videos from '@/services/Videos';
import {
    BS4K_LIVE_STREAMING_QUALITIES,
    LIVE_STREAMING_QUALITIES,
    VIDEO_STREAMING_QUALITIES,
} from '@/stores/SettingsStore';


/**
 * 自動モードの映像 codec 優先順。
 * ライブ・録画ともサーバ∩端末 MSE の積集合で効率優先 (AV1→VP9→HEVC→AVC)。
 * ライブは AV1/VP9/HEVC/AVC をすべて候補にし、使えないものだけスキップする。
 */
export const AUTO_PLAYBACK_LIVE_VIDEO_CODEC_ORDER: readonly KonomiTVBS4KPlaybackVideoCodec[] = [
    'av1',
    'vp9',
    'hevc',
    'avc',
];
export const AUTO_PLAYBACK_RECORDED_VIDEO_CODEC_ORDER: readonly KonomiTVBS4KPlaybackVideoCodec[] = [
    'av1',
    'vp9',
    'hevc',
    'avc',
];
/** @deprecated 互換用。録画向けと同じ効率優先順。 */
export const AUTO_PLAYBACK_VIDEO_CODEC_ORDER = AUTO_PLAYBACK_RECORDED_VIDEO_CODEC_ORDER;

/**
 * 自動モードで音声 codec を試す優先順。
 * ライブ・録画とも Opus → AAC（使えるなら Opus 優先。AAC は音質問題・フォールバック用）。
 */
export const AUTO_PLAYBACK_LIVE_AUDIO_CODEC_ORDER: readonly KonomiTVBS4KPlaybackAudioCodec[] = [
    'opus',
    'aac',
];
export const AUTO_PLAYBACK_RECORDED_AUDIO_CODEC_ORDER: readonly KonomiTVBS4KPlaybackAudioCodec[] = [
    'opus',
    'aac',
];
/** @deprecated 互換用。録画向けと同じ。 */
export const AUTO_PLAYBACK_AUDIO_CODEC_ORDER = AUTO_PLAYBACK_RECORDED_AUDIO_CODEC_ORDER;

/** Network Information API から読む指標（欠落可）。 */
export interface INetworkQualityMetrics {
    type?: string;
    effectiveType?: string;
    downlink?: number;
    rtt?: number;
    saveData?: boolean;
}

/** 画質 ID のおおよその垂直解像度。 */
export function getPlaybackQualityHeight(quality: string): number {
    const match = quality.match(/^(\d+)p/);
    return match !== null ? Number(match[1]) : 0;
}

/** 画質 ID が 60fps 系か。 */
export function isPlaybackQuality60fps(quality: string): boolean {
    return quality.includes('60fps');
}

/**
 * ソースの垂直解像度から、エンコード上限として許す高さ（p 値）を返す。
 * 不明時は null（ソース制約なし）。
 */
export function estimateSourceHeightCeiling(source_height: number | null | undefined): number | null {
    if (source_height === null || source_height === undefined || source_height <= 0) {
        return null;
    }
    if (source_height >= 4000) return 4320;
    if (source_height >= 2000) return 2160;
    if (source_height >= 1400) return 1440;
    if (source_height >= 1000) return 1080;
    if (source_height >= 800) return 810;
    if (source_height >= 700) return 720;
    if (source_height >= 500) return 540;
    if (source_height >= 400) return 480;
    if (source_height >= 300) return 360;
    return 240;
}

/**
 * Network Information から推定するエンコード高さ上限（p 値）。
 * 指標がほぼ無い場合は null（回線制約なし）。
 *
 * AVC 寄りビットレートに約 1.2 倍の余裕を見た保守値。
 * AV1/HEVC では実効的に余裕が出るが、開始不能を避ける方が優先。
 */
export function estimateNetworkHeightCeiling(
    metrics: INetworkQualityMetrics = {},
): number | null {
    if (metrics.saveData === true) {
        return 360;
    }

    let etype_ceiling: number | null = null;
    if (typeof metrics.effectiveType === 'string' && metrics.effectiveType !== '') {
        switch (metrics.effectiveType) {
            case 'slow-2g':
            case '2g':
                etype_ceiling = 240;
                break;
            case '3g':
                etype_ceiling = 480;
                break;
            // 4g は downlink に任せる（4g でも細い回線は多い）
            default:
                break;
        }
    }

    let downlink_ceiling: number | null = null;
    if (typeof metrics.downlink === 'number' && Number.isFinite(metrics.downlink)) {
        const downlink = metrics.downlink;
        if (downlink < 0.8) downlink_ceiling = 240;
        else if (downlink < 1.5) downlink_ceiling = 360;
        else if (downlink < 2.5) downlink_ceiling = 480;
        else if (downlink < 4) downlink_ceiling = 540;
        else if (downlink < 6) downlink_ceiling = 720;
        else if (downlink < 8) downlink_ceiling = 810;
        else if (downlink < 12) downlink_ceiling = 1080;
        else if (downlink < 16) downlink_ceiling = 1440;
        else if (downlink < 25) downlink_ceiling = 2160;
        else downlink_ceiling = 4320;
    }

    if (etype_ceiling === null && downlink_ceiling === null) {
        return null;
    }

    let ceiling = etype_ceiling !== null && downlink_ceiling !== null ?
        Math.min(etype_ceiling, downlink_ceiling) :
        (etype_ceiling ?? downlink_ceiling)!;

    // 高 RTT は輻輳寄りとみなし、1 段相当ゆるく下げる
    if (typeof metrics.rtt === 'number' && metrics.rtt > 150 && ceiling > 240) {
        if (ceiling >= 2160) ceiling = 1440;
        else if (ceiling >= 1440) ceiling = 1080;
        else if (ceiling >= 1080) ceiling = 810;
        else if (ceiling >= 810) ceiling = 720;
        else if (ceiling >= 720) ceiling = 540;
        else if (ceiling >= 540) ceiling = 480;
        else if (ceiling >= 480) ceiling = 360;
        else ceiling = 240;
    }

    return ceiling;
}

/**
 * 回線指標から 60fps 段を許可するか。
 * 指標欠落時は true（高さ上限だけで絞る）。細い回線での 60fps 開始失敗を避ける。
 */
export function estimateNetworkAllows60fps(
    metrics: INetworkQualityMetrics = {},
): boolean {
    if (metrics.saveData === true) {
        return false;
    }
    if (
        typeof metrics.effectiveType === 'string' &&
        metrics.effectiveType !== '' &&
        metrics.effectiveType !== '4g'
    ) {
        return false;
    }
    if (typeof metrics.downlink === 'number' && metrics.downlink < 12) {
        return false;
    }
    if (typeof metrics.rtt === 'number' && metrics.rtt > 120) {
        return false;
    }
    return true;
}

/**
 * ラダー上でユーザー上限以下かつソース高さ以下・回線上限以下の最高画質を返す。
 * ladder は高画質→低画質の順。上限は「その段以下」を許可する。
 *
 * @param network_metrics 省略時は回線制約なし（従来互換）。自動モード開始時は渡す。
 */
export function resolveAutoPlaybackQuality<T extends string>(
    ladder: readonly T[],
    max_quality: T,
    source_height: number | null | undefined,
    network_metrics?: INetworkQualityMetrics | null,
): T {
    const found_index = ladder.indexOf(max_quality);
    // 上限が見つからない場合は最高段から探索する
    const start_index = found_index >= 0 ? found_index : 0;
    const source_ceiling = estimateSourceHeightCeiling(source_height);
    const network_ceiling = network_metrics != null ?
        estimateNetworkHeightCeiling(network_metrics) :
        null;
    const allow_60fps = network_metrics != null ?
        estimateNetworkAllows60fps(network_metrics) :
        true;

    for (let index = start_index; index < ladder.length; index++) {
        const quality = ladder[index];
        const height = getPlaybackQualityHeight(quality);
        if (source_ceiling !== null && height > source_ceiling) {
            continue;
        }
        if (network_ceiling !== null && height > network_ceiling) {
            continue;
        }
        if (allow_60fps === false && isPlaybackQuality60fps(quality) === true) {
            continue;
        }
        return quality;
    }
    // 上限・ソース・回線制約で全滅した場合は最低段
    return ladder[ladder.length - 1];
}

/**
 * 現在画質の 1 段下を返す。既に最低段・ラダー外なら null。
 * ladder は高画質→低画質の順。
 */
export function getNextLowerAutoPlaybackQuality<T extends string>(
    ladder: readonly T[],
    current_quality: T | string,
): T | null {
    const index = ladder.indexOf(current_quality as T);
    if (index >= 0) {
        if (index >= ladder.length - 1) {
            return null;
        }
        return ladder[index + 1];
    }

    // 表示名やレガシー ID などラダー外の文字列: 高さ・fps から次段を推定
    const current_height = getPlaybackQualityHeight(current_quality);
    if (current_height <= 0) {
        return null;
    }
    const current_is_60 = isPlaybackQuality60fps(current_quality);
    for (let i = 0; i < ladder.length; i++) {
        const quality = ladder[i];
        const height = getPlaybackQualityHeight(quality);
        if (height > current_height) {
            continue;
        }
        if (height === current_height) {
            // 同高さなら 60fps → 30fps/非60 を「下」とみなす
            if (current_is_60 === true && isPlaybackQuality60fps(quality) === false) {
                return quality;
            }
            continue;
        }
        // 初めて current より低い高さに到達した段
        return quality;
    }
    return null;
}

/** 通常 / 録画 / BS4K に応じた画質ラダー。 */
export function getAutoPlaybackQualityLadder(
    is_bs4k: boolean,
    is_live: boolean,
): readonly (KonomiTVBS4KPlaybackStreamingQuality | BS4KLiveStreamingQuality)[] {
    if (is_bs4k === true) {
        return BS4K_LIVE_STREAMING_QUALITIES;
    }
    return is_live === true ? LIVE_STREAMING_QUALITIES : VIDEO_STREAMING_QUALITIES;
}

/**
 * Network Information API を読む（未対応環境は空オブジェクト）。
 */
export function readNetworkQualityMetrics(
    connection: unknown = typeof navigator !== 'undefined' ?
        (navigator as Navigator & {connection?: unknown}).connection :
        undefined,
): INetworkQualityMetrics {
    if (connection === null || connection === undefined || typeof connection !== 'object') {
        return {};
    }
    const net = connection as Record<string, unknown>;
    const metrics: INetworkQualityMetrics = {};
    if (typeof net.type === 'string') metrics.type = net.type;
    if (typeof net.effectiveType === 'string') metrics.effectiveType = net.effectiveType;
    if (typeof net.downlink === 'number' && Number.isFinite(net.downlink)) {
        metrics.downlink = net.downlink;
    }
    if (typeof net.rtt === 'number' && Number.isFinite(net.rtt)) {
        metrics.rtt = net.rtt;
    }
    if (typeof net.saveData === 'boolean') metrics.saveData = net.saveData;
    return metrics;
}

/**
 * ネットワーク品質・速度のみで低遅延の可否を返す。
 * 既存の低遅延設定・画質・codec は参照しない。
 *
 * 第 1 版閾値:
 * - saveData → OFF
 * - type=cellular → OFF
 * - effectiveType があり 4g 以外 → OFF
 * - downlink があり 5Mbps 未満 → OFF
 * - rtt があり 100ms 超 → OFF
 * - 有用な指標がほぼ無い → OFF（安全側）
 */
export function resolveAutoLowLatencyFromNetwork(
    metrics: INetworkQualityMetrics = readNetworkQualityMetrics(),
): boolean {
    if (metrics.saveData === true) {
        return false;
    }
    if (typeof metrics.type === 'string' && metrics.type.toLowerCase() === 'cellular') {
        return false;
    }
    if (
        typeof metrics.effectiveType === 'string' &&
        metrics.effectiveType !== '' &&
        metrics.effectiveType !== '4g'
    ) {
        return false;
    }
    if (typeof metrics.downlink === 'number' && metrics.downlink < 5) {
        return false;
    }
    if (typeof metrics.rtt === 'number' && metrics.rtt > 100) {
        return false;
    }

    const has_usable_metric =
        typeof metrics.type === 'string' ||
        typeof metrics.effectiveType === 'string' ||
        typeof metrics.downlink === 'number' ||
        typeof metrics.rtt === 'number';
    if (has_usable_metric === false) {
        return false;
    }

    // type が wifi/ethernet、または effectiveType=4g / 十分な downlink+rtt
    if (typeof metrics.type === 'string') {
        const type = metrics.type.toLowerCase();
        if (type === 'wifi' || type === 'ethernet') {
            return true;
        }
    }
    if (metrics.effectiveType === '4g') {
        return true;
    }
    if (
        typeof metrics.downlink === 'number' && metrics.downlink >= 5 &&
        typeof metrics.rtt === 'number' && metrics.rtt <= 100
    ) {
        return true;
    }
    // type だけあるが cellular 以外（例: mixed）で指標不足 → 安全側 OFF
    return false;
}

/**
 * 自動モード向けに映像・音声の同一 combination を解決する。
 *
 * 映像は AV1→VP9→HEVC→AVC を exact 一致だけで試し、途中の codec を飛ばさない。
 * 音声はライブ・録画とも Opus→AAC。
 * 全滅時のみ AVC+AAC。
 *
 * @param is_live ライブ時はライブ向け音声順、録画時は録画向け音声順を使う
 * @param excluded_video_codecs 実再生失敗などで除外する映像 codec
 */
export function resolveEfficiencyFirstPlaybackCombination(
    capabilities: IKonomiTVBS4KPlaybackCapabilities,
    encoder: IKonomiTVBS4KPlaybackEncoder,
    profile: IKonomiTVBS4KPlaybackVideoProfile,
    is_live: boolean = false,
    excluded_video_codecs: ReadonlySet<KonomiTVBS4KPlaybackVideoCodec> | readonly KonomiTVBS4KPlaybackVideoCodec[] = [],
): IKonomiTVBS4KResolvedPlaybackCombination | null {
    const video_order = is_live === true ?
        AUTO_PLAYBACK_LIVE_VIDEO_CODEC_ORDER :
        AUTO_PLAYBACK_RECORDED_VIDEO_CODEC_ORDER;
    const audio_order = is_live === true ?
        AUTO_PLAYBACK_LIVE_AUDIO_CODEC_ORDER :
        AUTO_PLAYBACK_RECORDED_AUDIO_CODEC_ORDER;
    const excluded = excluded_video_codecs instanceof Set ?
        excluded_video_codecs :
        new Set(excluded_video_codecs);

    for (const video_codec of video_order) {
        if (excluded.has(video_codec) === true) {
            continue;
        }
        for (const audio_codec of audio_order) {
            // exact のみ。汎用 resolver の内部 fallback（要求→AVC 直行）を使わない。
            const combination = Videos.resolveKonomiTVBS4KExactPlaybackCombination(
                capabilities,
                encoder,
                video_codec,
                audio_codec,
                profile,
            );
            if (combination !== null) {
                return combination;
            }
        }
    }
    if (excluded.has('avc') === true) {
        return null;
    }
    return Videos.resolveKonomiTVBS4KExactPlaybackCombination(
        capabilities,
        encoder,
        'avc',
        'aac',
        profile,
    );
}
