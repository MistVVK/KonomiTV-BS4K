
/** ニコニコ OAuth callback からクライアントへ通知できる固定結果 */
export const NICONICO_OAUTH_RESULTS = [
    'Success',
    'AccessDenied',
    'AuthorizationError',
    'AuthorizationCodeMissing',
    'UserNotFound',
    'TokenAPIError',
    'TokenAPITimeout',
    'UserAPIError',
    'UserAPITimeout',
] as const;

export type NiconicoOAuthResult = typeof NICONICO_OAUTH_RESULTS[number];

export interface INiconicoOAuthPopupMessage {
    'KonomiTV-OAuthPopup': {
        result: NiconicoOAuthResult;
    };
}


/** 値が null と配列を除くオブジェクトかどうかを返す */
function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null && Array.isArray(value) === false;
}


/** 値がクライアントで処理可能な固定 OAuth 結果かどうかを返す */
export function isNiconicoOAuthResult(value: unknown): value is NiconicoOAuthResult {
    return typeof value === 'string' && (NICONICO_OAUTH_RESULTS as readonly string[]).includes(value);
}


/** URL フラグメントから、固定形式のニコニコ OAuth 結果だけを取り出す */
export function parseNiconicoOAuthResult(hash: string): NiconicoOAuthResult | null {
    const params = new URLSearchParams(hash.startsWith('#') ? hash.slice(1) : hash);

    // result の重複や未知のキーを許すと、曖昧な解釈がブラウザ・実装間で生じるため拒否する。
    const param_keys = [...params.keys()];
    if (param_keys.length !== 1 || param_keys[0] !== 'result' || params.getAll('result').length !== 1) {
        return null;
    }

    const result = params.get('result');
    return isNiconicoOAuthResult(result) ? result : null;
}


/** popup から opener へ送る固定形式のメッセージを作成する */
export function createNiconicoOAuthPopupMessage(result: NiconicoOAuthResult): INiconicoOAuthPopupMessage {
    return {
        'KonomiTV-OAuthPopup': {result},
    };
}


/** message イベントが、現在の OAuth popup から同一 Origin で送信された正しい結果かどうかを検証する */
export function isNiconicoOAuthPopupMessageEvent(
    event: MessageEvent,
    expected_source: Window,
    expected_origin: string,
): event is MessageEvent<INiconicoOAuthPopupMessage> {

    // Origin だけでは同一サイト内の別ウインドウから偽装できるため、window.open() の戻り値も一致させる。
    if (event.origin !== expected_origin || event.source !== expected_source) {
        return false;
    }

    // prototype 継承や余分なキーに依存せず、固定した1階層のメッセージ形式だけを受理する。
    if (isRecord(event.data) === false || Object.keys(event.data).length !== 1) {
        return false;
    }
    const payload = event.data['KonomiTV-OAuthPopup'];
    if (isRecord(payload) === false || Object.keys(payload).length !== 1) {
        return false;
    }
    return isNiconicoOAuthResult(payload.result);
}
