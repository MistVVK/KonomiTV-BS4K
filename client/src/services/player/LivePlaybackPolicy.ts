
/**
 * 1回のライブ再生セッションで共有する低遅延ポリシー
 *
 * PlayerController の初期化時に生成し、DPlayer・mpegts.js・再生開始待ち・
 * 実況コメント同期へ同じオブジェクト参照を渡す。
 * 低遅延 ON/OFF のソフト切替時は同一オブジェクトを in-place 更新し、
 * destroy→init なしで mpegts liveSync とバッファ位置だけを切り替える。
 */
export interface ILivePlaybackPolicy {
    mode: 'LowLatency' | 'Stable';
    target_buffer_seconds: number;
    live_sync_enabled: boolean;
    max_latency_seconds: number | null;
    catch_up_rate: number;
}


export interface ILivePlaybackPolicyOptions {
    requested_low_latency: boolean;
    force_stable: boolean;
    is_safari: boolean;
}


// 低遅延 ON は放送波との差を小さく保つため、0.9秒の再生バッファを目標にする。
const LOW_LATENCY_TARGET_BUFFER_SECONDS = 0.9;

// 低遅延 OFF は通信揺らぎへの耐性を優先し、4.0秒の再生バッファを目標にする。
const STABLE_TARGET_BUFFER_SECONDS = 4.0;

// Safari の MSE はバッファ量が揺らぎやすいため、ON / OFF の双方へ同じ安全余裕を加える。
const SAFARI_BUFFER_MARGIN_SECONDS = 0.3;

// 低遅延 ON でライブ端との差がこの値を超えた場合だけ、mpegts.js に追従を開始させる。
const LOW_LATENCY_MAX_LATENCY_SECONDS = 3;

// 遅延追従中も音声や映像を大きく乱さないよう、既存挙動どおり1.1倍速に限定する。
const LOW_LATENCY_CATCH_UP_RATE = 1.1;


/**
 * 保存設定・BS4K上書き・ブラウザ差を解決し、1再生セッション分のポリシーを生成する
 */
export function createLivePlaybackPolicy(
    options: ILivePlaybackPolicyOptions,
): ILivePlaybackPolicy {

    // 録画再生や BS4K のサーバー上書きでは、保存設定が ON でも安定モードへ固定する。
    const live_sync_enabled = options.requested_low_latency === true && options.force_stable === false;
    let target_buffer_seconds = live_sync_enabled === true ?
        LOW_LATENCY_TARGET_BUFFER_SECONDS : STABLE_TARGET_BUFFER_SECONDS;

    // Safari の ON は1.2秒、OFFは4.3秒となるよう、モード解決後に共通補正を加える。
    if (options.is_safari === true) {
        target_buffer_seconds += SAFARI_BUFFER_MARGIN_SECONDS;
    }

    // ソフト切替で同一参照を in-place 更新するため freeze しない。
    return {
        mode: live_sync_enabled === true ? 'LowLatency' : 'Stable',
        target_buffer_seconds,
        live_sync_enabled,
        max_latency_seconds: live_sync_enabled === true ? LOW_LATENCY_MAX_LATENCY_SECONDS : null,
        catch_up_rate: live_sync_enabled === true ? LOW_LATENCY_CATCH_UP_RATE : 1,
    };
}


/**
 * 既存ポリシーオブジェクトを低遅延 ON/OFF の新値で上書きする（参照は維持）。
 */
export function reconfigureLivePlaybackPolicy(
    policy: ILivePlaybackPolicy,
    options: ILivePlaybackPolicyOptions,
): ILivePlaybackPolicy {
    const next = createLivePlaybackPolicy(options);
    policy.mode = next.mode;
    policy.target_buffer_seconds = next.target_buffer_seconds;
    policy.live_sync_enabled = next.live_sync_enabled;
    policy.max_latency_seconds = next.max_latency_seconds;
    policy.catch_up_rate = next.catch_up_rate;
    return policy;
}


/**
 * 低遅延ライブの表示開始前に一度だけ適用する再生位置を解決する
 */
export function resolveInitialLivePlaybackPositionSeconds(
    live_playback_policy: Readonly<ILivePlaybackPolicy>,
    buffered_start: number,
    buffered_end: number,
    current_time: number,
): number | null {

    // 安定モード、またはすでに目標値へ収まっている場合は現在位置を維持する。
    if (
        live_playback_policy.live_sync_enabled === false ||
        buffered_end - current_time <= live_playback_policy.target_buffer_seconds + 0.1
    ) {
        return null;
    }

    // 受信済み範囲の末尾から目標バッファ分だけ残した位置へ移動する。
    const target_time = Math.max(buffered_end - live_playback_policy.target_buffer_seconds, buffered_start);
    return target_time > current_time ? target_time : null;
}


/**
 * 実況コメントを映像へ同期させる待機秒数を、セッション固定ポリシーと現在の実バッファから解決する
 */
export function resolveLiveCommentDelaySeconds(
    live_playback_policy: Readonly<ILivePlaybackPolicy>,
    buffered_end: number | null,
    current_time: number,
): number {

    // MediaSource の初期化前は、再生開始と同じ目標バッファを使う。
    if (buffered_end === null) {
        return live_playback_policy.target_buffer_seconds;
    }

    // 実バッファ取得後は、現在の映像遅延へ実況コメントを同期する。
    return Math.max(buffered_end - current_time, 0);
}
