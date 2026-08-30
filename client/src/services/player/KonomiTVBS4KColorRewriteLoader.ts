
import type {
    FragmentLoaderContext,
    HlsConfig,
    Loader,
    LoaderCallbacks,
    LoaderConfiguration,
    LoaderResponse,
} from 'hls.js';

import {
    rewriteKonomiTVBS4KFMP4InitSegment,
    rewriteKonomiTVBS4KFMP4MediaSegment,
    type KonomiTVBS4KFMP4ColorRewriteMode,
    type KonomiTVBS4KFMP4VideoColorState,
} from '@/services/player/KonomiTVBS4KFMP4ColorRewrite';
import usePlayerStore from '@/stores/PlayerStore';


/**
 * 録画再生セッション (DPlayer / hls.js の1初期化) に紐づく fMP4 色信号書換えの状態。
 * PlayerController が初期化時に生成し、hls.js の設定オブジェクトへ非標準キーで同梱する。
 * セッション中は mode を変えず、HDR 出力の変更はプレイヤー再起動 (MSE 初期化からのやり直し) で反映する。
 */
export interface KonomiTVBS4KColorRewriteSession {
    // 'None' は HDR 素通し、'ToneMap' は SDR 変換用の信号書換え
    mode: KonomiTVBS4KFMP4ColorRewriteMode;
    // 初期化セグメントから確定した映像トラックの状態 (メディアセグメントの in-band 書換えに使う)
    video_state: KonomiTVBS4KFMP4VideoColorState | null;
    // 安全に書き換えられない fMP4 を検出したら true。canvas を無効化して HDR 素通しで継続する
    unsafe: boolean;
    // 利用者通知と英語ログを同一失敗につき1回に抑えるためのラッチ
    unsafe_notified: boolean;
}

/** hls.js の設定へセッションを同梱するための拡張型 */
export type KonomiTVBS4KColorRewriteHlsConfig = HlsConfig & {
    konomitv_bs4k_color_rewrite_session?: KonomiTVBS4KColorRewriteSession;
};

/**
 * 録画 HLS の fragment loader に挟まって fMP4 の HDR 色信号だけを動的に書き換える Loader。
 * オンラインの録画 API と CacheStorage 上のオフライン保存のどちらも同じこの経路を通る。
 * オフライン保存アセット自体は複製・変更せず、Service Worker / サーバー API の契約も変えない。
 *
 * hls.js はメディアセグメントを progressive (chunk 分割) で受け取るが、色信号はチャンク境界を
 * 跨ぎうるため、この Loader では onProgress を外してバッファ全体が揃ってから書き換える。
 * 録画 fMP4 はトランスmux不要で MSE へ直接積まれるため、進行コールバックを渡さなくても
 * バッファリングの正しさは保たれる。
 */
class KonomiTVBS4KColorRewriteLoader implements Loader<FragmentLoaderContext> {

    private readonly inner: Loader<FragmentLoaderContext>;
    private readonly session: KonomiTVBS4KColorRewriteSession | null;

    constructor(config: HlsConfig) {
        // fLoader だけを差し替えるため、既定の loader (FetchLoader / XHRLoader) を内部で使う
        const InnerLoader = config.loader;
        this.inner = new InnerLoader(config) as Loader<FragmentLoaderContext>;
        this.session = (config as KonomiTVBS4KColorRewriteHlsConfig).konomitv_bs4k_color_rewrite_session ?? null;
    }

    public get context(): FragmentLoaderContext | null {
        return this.inner.context;
    }

    public get stats() {
        return this.inner.stats;
    }

    public destroy(): void {
        this.inner.destroy();
    }

    public abort(): void {
        this.inner.abort();
    }

    public getCacheAge(): number | null {
        return this.inner.getCacheAge?.() ?? null;
    }

    public getResponseHeader(name: string): string | null {
        return this.inner.getResponseHeader?.(name) ?? null;
    }

    public load(
        context: FragmentLoaderContext,
        config: LoaderConfiguration,
        callbacks: LoaderCallbacks<FragmentLoaderContext>,
    ): void {
        const session = this.session;
        // セッションなし・映像以外 (音声・字幕)・バイナリ以外の要求はそのまま通す
        if (session === null ||
            context.responseType !== 'arraybuffer' ||
            context.frag.type !== 'main') {
            this.inner.load(context, config, callbacks);
            return;
        }
        // 進行コールバックを外してバッファ全体を一度に受け取る (チャンク境界を跨ぐ色信号を書き換えるため)
        const wrapped_callbacks: LoaderCallbacks<FragmentLoaderContext> = {
            ...callbacks,
            onProgress: undefined,
            onSuccess: (response: LoaderResponse, stats, loaded_context, network_details) => {
                callbacks.onSuccess(this.rewriteResponse(response, context), stats, loaded_context, network_details);
            },
        };
        this.inner.load(context, {...config, highWaterMark: Infinity}, wrapped_callbacks);
    }

    /**
     * 取得済みの fMP4 バッファへ色信号書換えを適用する。
     * 初期化セグメント (frag.sn === 'initSegment') とメディアセグメントの両方を扱う。
     */
    private rewriteResponse(response: LoaderResponse, context: FragmentLoaderContext): LoaderResponse {
        const session = this.session;
        if (session === null || session.unsafe === true ||
            response.data === undefined || !(response.data instanceof ArrayBuffer) ||
            response.data.byteLength === 0) {
            return response;
        }
        const bytes = new Uint8Array(response.data);
        const is_init_segment = context.frag.sn === 'initSegment';
        const result = is_init_segment === true ?
            rewriteKonomiTVBS4KFMP4InitSegment(bytes, session.mode) :
            (session.video_state !== null ?
                rewriteKonomiTVBS4KFMP4MediaSegment(bytes, session.mode, session.video_state) :
                null);
        // メディアセグメントより先に初期化セグメントが来るのが正常。来ていなければそのまま通す
        if (result === null) {
            return response;
        }

        const player_store = usePlayerStore();
        // 検出した元の色信号を PlayerStore へ反映し、HlgSdrManager の canvas 判定へ渡す
        if (result.detected !== null &&
            player_store.sps_transfer_characteristics !== result.detected.transfer_characteristics) {
            player_store.sps_transfer_characteristics = result.detected.transfer_characteristics;
        }
        if (is_init_segment === true && result.video_state !== null) {
            session.video_state = result.video_state;
        }
        if (result.unsafe === true) {
            // 安全に書き換えられない fMP4 はバイト列を改変せず、canvas を無効化して HDR 素通しで継続する。
            // 利用者通知と英語ログは同一失敗につき1回だけ行う。
            session.unsafe = true;
            if (session.unsafe_notified === false) {
                session.unsafe_notified = true;
                console.warn(
                    '[KonomiTVBS4KColorRewriteLoader] Failed to safely rewrite the fMP4 color signaling. ' +
                    'HDR canvas conversion is disabled and playback continues with HDR passthrough.',
                );
                player_store.konomitv_bs4k_recorded_color_rewrite_unsafe = true;
            }
            return response;
        }
        if (result.rewritten === false) {
            return response;
        }
        const rewritten = result.data;
        return {
            ...response,
            data: rewritten.byteOffset === 0 && rewritten.byteLength === rewritten.buffer.byteLength ?
                rewritten.buffer : rewritten.slice().buffer,
        };
    }
}

/** セッション状態を初期化する。HDR 出力の選択肢 (override 優先) から書換えモードを決める */
export function createKonomiTVBS4KColorRewriteSession(
    desired_output: 'HDR' | 'SDR',
): KonomiTVBS4KColorRewriteSession {
    return {
        mode: desired_output === 'SDR' ? 'ToneMap' : 'None',
        video_state: null,
        unsafe: false,
        unsafe_notified: false,
    };
}

export default KonomiTVBS4KColorRewriteLoader;
