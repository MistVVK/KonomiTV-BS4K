import type { KonomiTVBS4KHdrOutput } from '@/stores/SettingsStore';


export type KonomiTVBS4KHdrRewriteMode = 'None' | 'ToneMap' | 'SdrInHlg' | 'Debug';
export type KonomiTVBS4KHdrSourceKind = 'None' | 'Hlg' | 'Pq';
export type KonomiTVBS4KHdrDesiredOutput = 'HDR' | 'SDR' | 'Debug';

export const CICP_BT709_TRANSFER = 1;
export const CICP_BT2020_12_TRANSFER = 14;
export const CICP_PQ_TRANSFER = 16;
export const CICP_HLG_TRANSFER = 18;
export const B60_UHD_SDR = 3;
export const B60_HLG = 5;

export function detectKonomiTVBS4KHdrDisplayCapability(): boolean {
    if (typeof window.matchMedia === 'function') {
        if (window.matchMedia('(video-dynamic-range: high)').matches === true) {
            return true;
        }
        if (window.matchMedia('(dynamic-range: high)').matches === true) {
            return true;
        }
        if (
            window.screen.pixelDepth > 24 &&
            window.matchMedia('(color-gamut: p3)').matches === true
        ) {
            return true;
        }
    }
    // 判定不能は SDR 変換側。HDR 番組の白飛びより被害が小さい。
    return false;
}

export function resolveKonomiTVBS4KHdrOutput(
    setting: KonomiTVBS4KHdrOutput,
    override: KonomiTVBS4KHdrOutput | 'Debug' | null,
): KonomiTVBS4KHdrDesiredOutput {
    const requested = override ?? setting;
    // Debug は Auto の HDR/SDR 判定に落とさない。視聴中 override 専用の左右比較モード。
    if (requested === 'Debug') {
        return 'Debug';
    }
    if (requested === 'HDR' || requested === 'SDR') {
        return requested;
    }
    return detectKonomiTVBS4KHdrDisplayCapability() === true ? 'HDR' : 'SDR';
}

export function classifyKonomiTVBS4KHdrSource(
    transfer_characteristics: number | null,
): KonomiTVBS4KHdrSourceKind {
    if (transfer_characteristics === CICP_HLG_TRANSFER) {
        return 'Hlg';
    }
    if (transfer_characteristics === CICP_PQ_TRANSFER) {
        return 'Pq';
    }
    return 'None';
}

export function resolveKonomiTVBS4KHdrRewriteMode(
    transfer_characteristics: number | null,
    b60_video_transfer: number | null,
    desired_output: KonomiTVBS4KHdrDesiredOutput,
): KonomiTVBS4KHdrRewriteMode {
    const source = classifyKonomiTVBS4KHdrSource(transfer_characteristics);
    if (source === 'None') {
        return 'None';
    }
    if (source === 'Hlg' && b60_video_transfer === B60_UHD_SDR) {
        return 'SdrInHlg';
    }
    // Debug の色信号書換えは SDR 変換と同じ ToneMap。canvas 側だけ左右比較する。
    if (desired_output === 'Debug') {
        return 'Debug';
    }
    if (desired_output === 'SDR') {
        return 'ToneMap';
    }
    return 'None';
}

export function shouldDrawKonomiTVBS4KHdrCanvas(mode: KonomiTVBS4KHdrRewriteMode): boolean {
    return mode === 'ToneMap' || mode === 'Debug';
}
