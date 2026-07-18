
import Hls, { BufferController, FragmentTracker } from 'hls.js';


/**
 * hls.js のバッファコントローラーを拡張し、サーバーサイドイベントと連携してバッファ範囲を管理するクラス
 * 大元の hls.js の実装がほとんど private なのでやむを得ず @ts-ignore を多用している
 * biim の pseudo.html の実装を KonomiTV 向けに移植したもの
 * ref: https://github.com/tsukumijima/biim/blob/main/pseudo.html
 */
// @ts-ignore
class CustomBufferController extends BufferController {

    // セグメント要求へ付与するシーク世代番号
    // stopLoad() より前に開始済みだった古い要求をサーバー側で識別するために使う
    private static requestGeneration: number = 0;

    public static getRequestGeneration(): number {
        return CustomBufferController.requestGeneration;
    }

    private static advanceRequestGeneration(): void {
        CustomBufferController.requestGeneration += 1;
    }

    // バッファフラッシュ時のイベントハンドラー（独自）
    private onCustomBufferFlushingHandler: () => void;

    // SSE の EventSource インスタンス
    private sse: EventSource | null = null;

    // サーバーからのバッファ範囲情報
    private serverBufferingRange: { begin: number, end: number } | null = null;

    // 初回fragment取得前のレジュームシークを独自処理で中断しないための状態
    private isInitialFragmentBuffered: boolean = false;

    // バッファ削除完了待ちの間に連続シークされた場合、最後の位置だけを採用する
    private isSeekRestartInProgress: boolean = false;
    private pendingSeekPosition: number | null = null;
    private seekDebounceTimerId: ReturnType<typeof setTimeout> | null = null;
    private seekRestartTimeoutId: ReturnType<typeof setTimeout> | null = null;

    constructor(hls: Hls, fragmentTracker: FragmentTracker) {
        super(hls, fragmentTracker);
        this.onCustomBufferFlushingHandler = this.onCustomBufferFlushing.bind(this);
        this.isInitialFragmentBuffered = false;
        this.isSeekRestartInProgress = false;
        this.pendingSeekPosition = null;
        this.seekDebounceTimerId = null;
        this.seekRestartTimeoutId = null;
    }

    /**
     * メディアアタッチ時のイベントハンドラー（上書き）
     */
    protected onMediaAttaching(event: any, data: any): void {
        // 親クラスの onMediaAttaching を呼び出す
        // @ts-ignore
        super.onMediaAttaching(event, data);
        // HLS インスタンスと HTMLMediaElement を取得
        // @ts-ignore
        const hls: Hls = this.hls;
        // @ts-ignore
        const media: HTMLMediaElement = this.media;
        // PlayerController の再初期化前に残っていた要求と、新しい再生セッションの要求を分離する
        CustomBufferController.advanceRequestGeneration();
        // 録画VODのシークはhls.js標準処理に任せる。
        // 独自の全バッファ破棄やマニフェスト再読み込みは、代替音声のロード状態を壊す。
        media.addEventListener('seeking', this.onCustomBufferFlushingHandler);
        hls.once(Hls.Events.FRAG_BUFFERED, () => {
            this.isInitialFragmentBuffered = true;
        });
        // マスタープレイリストAPIが録画セッションを作成した後にSSEへ接続する。
        // media attach直後では/bufferが先着して422となり、不要な再接続が発生する。
        hls.once(Hls.Events.MANIFEST_PARSED, () => {
            if (this.sse !== null || hls.url == null) return;
            this.sse = new EventSource(hls.url.replace('playlist', 'buffer'));
            this.sse.addEventListener('buffer_range_update', (event) => {
                this.serverBufferingRange = JSON.parse(event.data);
                console.log('[CustomBufferController] Updated Server Buffering Range:', this.serverBufferingRange);
            });
        });
    }


    /**
     * メディアデタッチ時のイベントハンドラー（上書き）
     */
    protected onMediaDetaching(): void {
        // @ts-ignore
        const media: HTMLMediaElement = this.media;
        media.removeEventListener('seeking', this.onCustomBufferFlushingHandler);
        this.isInitialFragmentBuffered = false;
        this.isSeekRestartInProgress = false;
        this.pendingSeekPosition = null;
        if (this.seekDebounceTimerId !== null) clearTimeout(this.seekDebounceTimerId);
        if (this.seekRestartTimeoutId !== null) clearTimeout(this.seekRestartTimeoutId);
        this.seekDebounceTimerId = null;
        this.seekRestartTimeoutId = null;
        // SSE の接続を終了
        if (this.sse) {
            this.sse.close();
            this.sse = null;
        }
        // 親クラスの onMediaDetaching を呼び出す
        // @ts-ignore
        super.onMediaDetaching();
    }


    /**
     * バッファフラッシュ時のイベントハンドラー（独自）
     */
    private onCustomBufferFlushing(): void {
        // HLS インスタンスと HTMLMediaElement を取得
        // @ts-ignore
        const hls: Hls = this.hls;
        // @ts-ignore
        const media: HTMLMediaElement = this.media;
        if (!media) return;
        if (this.isInitialFragmentBuffered === false) return;

        // シーク位置がバッファの範囲内かチェック
        let isInBufferedRange = false;
        let isAtEnd = false;
        const duration = media.duration;

        // クライアント側のバッファ範囲をチェック
        for (let i = 0; i < media.buffered.length; i++) {
            if (media.currentTime >= media.buffered.start(i) &&
                media.currentTime <= media.buffered.end(i)) {
                isInBufferedRange = true;
                break;
            }
        }
        // サーバー側に生成済みでもMSEへappendされているとは限らないため、
        // シーク再配置の判定にはHTMLMediaElementのbufferedだけを使う。
        // 再生が終了しているかチェック
        if (media.currentTime >= duration - 0.5) {  // 0.5秒の余裕を持たせる
            isAtEnd = true;
        }

        // バッファ範囲外では同じHLSセッションのローダーだけをシーク位置へ再配置する。
        // マスターや代替音声プレイリストを再読み込みせず、選択Trackも維持する。
        console.log('[CustomBufferController] Server Buffering Range:', this.serverBufferingRange, 'Current Time:', media.currentTime);
        if ((!isInBufferedRange && !isAtEnd) || this.isSeekRestartInProgress) {
            this.pendingSeekPosition = media.currentTime;
            CustomBufferController.advanceRequestGeneration();

            // hls.js の標準シーク処理がデバウンス中の位置を取得し始めないよう、最初のイベントで即座に止める。
            // 以降の seeking イベントでは pendingSeekPosition だけを更新し、最後の位置から再開する。
            if (this.isSeekRestartInProgress === false) {
                this.isSeekRestartInProgress = true;
                hls.stopLoad();
            }

            // 既に SourceBuffer の削除を開始している場合は、完了時に最新の pendingSeekPosition が使われる。
            if (this.seekRestartTimeoutId !== null) return;
            if (this.seekDebounceTimerId !== null) clearTimeout(this.seekDebounceTimerId);
            this.seekDebounceTimerId = setTimeout(() => {
                this.seekDebounceTimerId = null;
                console.log('[CustomBufferController] Restarting HLS loaders at seek position...');
                hls.stopLoad();

                // BUFFER_FLUSHED は SourceBuffer ごとに発火するため、映像・音声の両方を待つ。
                // 片方だけ削除された時点で startLoad() すると、旧音声と新映像が混在して再生が停止する。
                // @ts-ignore hls.js の tracks は private だが、カスタム BufferController 内では実体を参照できる
                const expectedBufferTypes = new Set<string>(Object.entries(this.tracks ?? {})
                    .filter(([, track]: [string, any]) => track?.buffer != null)
                    .map(([type]) => type));
                const flushedBufferTypes = new Set<string>();

                const finishRestart = () => {
                    if (this.isSeekRestartInProgress === false) return;
                    if (this.seekRestartTimeoutId !== null) clearTimeout(this.seekRestartTimeoutId);
                    this.seekRestartTimeoutId = null;
                    hls.off(Hls.Events.BUFFER_FLUSHED, onBufferFlushed);
                    const seekPosition = this.pendingSeekPosition ?? media.currentTime;
                    this.pendingSeekPosition = null;
                    this.isSeekRestartInProgress = false;
                    hls.startLoad(seekPosition);
                };

                const onBufferFlushed = (_event: string, data: { type?: string }) => {
                    if (data.type != null) flushedBufferTypes.add(data.type);
                    if ([...expectedBufferTypes].every(type => flushedBufferTypes.has(type))) {
                        finishRestart();
                    }
                };
                hls.on(Hls.Events.BUFFER_FLUSHED, onBufferFlushed);
                // 空バッファなどでBUFFER_FLUSHEDが発火しない場合も停止状態を残さない。
                this.seekRestartTimeoutId = setTimeout(finishRestart, 3000);
                hls.trigger(Hls.Events.BUFFER_FLUSHING, {
                    startOffset: 0,
                    endOffset: Number.POSITIVE_INFINITY,
                    type: null,
                });
            }, 300);
        }
    }
}

export default CustomBufferController;
