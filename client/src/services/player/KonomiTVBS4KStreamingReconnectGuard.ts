/**
 * 帯域不足などでライブストリーミングの接続が短時間に繰り返し切断された場合に、プレイヤーの自動再起動ループを抑止する。
 * PlayerController の再初期化をまたいで状態を保持し、安定再生の実績がないまま接続喪失が連続した場合に自動再起動を拒否する。
 */
export default class KonomiTVBS4KStreamingReconnectGuard {

    // 自動再起動を許可する連続した接続喪失の最大回数。この回数に達した接続喪失では自動再起動せず、ユーザー操作へ委ねる。
    // 帯域不足による切断ループは 1 サイクル 15 〜 40 秒程度で繰り返されるため、3 回目で止めればループ開始から 1 〜 1.5 分程度で停止する
    private static readonly MAX_CONSECUTIVE_RECONNECTS = 3;

    // 前回の接続喪失からこの時間 (ミリ秒) 以上経過していれば、安定再生の実績ありとして連続カウントをリセットする。
    // 一時的なネットワーク切断 (Wi-Fi の切り替わりなど) から復帰して安定再生できた場合に、次の切断をループと誤判定しないための時間幅
    private static readonly STABLE_PLAYBACK_WINDOW_MILLISECONDS = 120_000;

    // 直近の接続喪失による自動再起動の記録 (再生対象・codec pipeline の判定キー・連続回数・最終発生時刻)。
    // PlayerController.init() では消去せず、同じ controller を destroy() → init() した後のループ判定に利用する
    private last_reconnect: {
        restart_key: string;
        consecutive_count: number;
        occurred_at_milliseconds: number;
    } | null = null;

    /**
     * 接続喪失による自動再起動を記録し、今回の再起動を許可するかどうかを返す。
     * 同じ再生対象で安定再生の実績なく接続喪失が MAX_CONSECUTIVE_RECONNECTS 回連続した場合は拒否する。
     */
    public requestReconnect(
        restart_key: string,
        occurred_at_milliseconds: number = performance.now(),
    ): boolean {

        // 再生対象・codec pipeline が変わった場合、または前回の接続喪失から十分な時間 (安定再生の実績) が経過した場合は、
        // 一時的な切断として連続カウントをリセットする
        if (
            this.last_reconnect === null ||
            this.last_reconnect.restart_key !== restart_key ||
            occurred_at_milliseconds - this.last_reconnect.occurred_at_milliseconds >=
                KonomiTVBS4KStreamingReconnectGuard.STABLE_PLAYBACK_WINDOW_MILLISECONDS
        ) {
            this.last_reconnect = {
                restart_key,
                consecutive_count: 1,
                occurred_at_milliseconds,
            };
            return true;
        }

        // 連続した接続喪失として記録し、上限に達したら自動再起動を拒否する
        const consecutive_count = this.last_reconnect.consecutive_count + 1;
        this.last_reconnect = {
            restart_key,
            consecutive_count,
            occurred_at_milliseconds,
        };
        return consecutive_count < KonomiTVBS4KStreamingReconnectGuard.MAX_CONSECUTIVE_RECONNECTS;
    }

    /**
     * 手動再起動などユーザー操作による再接続時に、接続喪失の連続カウントをリセットする。
     * ユーザーが回線状況や画質を見直した上での再接続とみなし、改めて自動復旧を試せるようにする。
     */
    public reset(): void {
        this.last_reconnect = null;
    }
}
