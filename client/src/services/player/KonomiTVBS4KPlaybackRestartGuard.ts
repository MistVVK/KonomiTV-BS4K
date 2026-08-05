

/**
 * 同一再生対象・同一 codec pipeline の短時間再起動ループを抑止する。
 * PlayerController の再初期化をまたいで状態を保持し、一時的なデコーダー停止に対する最初の再起動だけを許可する。
 */
export default class KonomiTVBS4KPlaybackRestartGuard {

    // 一度再起動した codec pipeline が再び失敗した場合に、同じ再起動を拒否する時間幅。
    // 60 秒を超えて安定再生できた後の単発エラーは、一時障害として再度1回だけ復旧を試せるようにする。
    private static readonly RESTART_RETRY_WINDOW_MILLISECONDS = 60_000;

    // 直前に自動再起動を許可した再生対象・codec tuple と、その単調増加時刻。
    // PlayerController.init() では消去せず、同じ controller を destroy() → init() した後の再発判定に利用する。
    private last_automatic_restart: {
        restart_key: string;
        requested_at_milliseconds: number;
    } | null = null;

    /** 同じ再生対象・codec tuple の自動再起動を、60 秒の時間幅につき1回だけ許可する。 */
    public requestAutomaticRestart(
        restart_key: string,
        requested_at_milliseconds: number = performance.now(),
    ): boolean {

        // 同じ pipeline を直前に再起動済みなら、再起動を繰り返さずユーザー操作へ委ねる。
        if (
            this.last_automatic_restart?.restart_key === restart_key &&
            requested_at_milliseconds - this.last_automatic_restart.requested_at_milliseconds <
                KonomiTVBS4KPlaybackRestartGuard.RESTART_RETRY_WINDOW_MILLISECONDS
        ) {
            return false;
        }

        // 再生対象・codec tuple が異なる場合、または十分な時間が経過した場合は、新しい復旧枠として記録する。
        this.last_automatic_restart = {
            restart_key,
            requested_at_milliseconds,
        };
        return true;
    }
}
