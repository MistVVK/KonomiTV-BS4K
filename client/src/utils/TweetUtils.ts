
import type { ITweet } from '@/services/Twitter';

import { dayjs } from '@/utils';



// リプライツリー実況のモード
export type ReplyThreadMode = 'PerHashtag' | 'PerDay' | 'Disabled';

// Twitter のリプライツリー状態
export interface ITwitterReplyThreadState {
    last_tweet_id: string;
    started_at: string;
    hashtag_key: string;
}

// Bluesky のリプライツリー状態
export interface IBlueskyReplyThreadState {
    root_uri: string;
    root_cid: string;
    parent_uri: string;
    parent_cid: string;
    started_at: string;
    hashtag_key: string;
}

// リプライツリー実況有効時のステータス状態
export interface IReplyThreadDecision {
    send_as_reply: boolean;
    reset_state_after: boolean;
    clear_state: boolean;
}

// Tweet 本文を描画用に分解したセグメント
// text はテキストノードとして表示し、link は固定属性の <a> 要素として表示する
export type TweetTextSegment =
    | { type: 'text'; text: string }
    | { type: 'link'; text: string; url: string };

/**
 * ツイート (Twitter / Bluesky 投稿) を扱う共通ユーティリティ
 * Twitter タブ配下の Timeline / Search 双方で同じ重複検出・並び替えロジックを使うためにまとめている
 */
export class TweetUtils {

    /**
     * ツイートの同一性比較に使うキーを取得する
     * Bluesky 投稿の id は AT URI なので、source + ID だけで識別できる
     * Bluesky のリポストは元投稿と同じ AT URI を持つため、リポストしたユーザーも含める
     * @param tweet ツイート
     * @returns 同一性比較用のキー文字列
     */
    static getTweetIdentityKey(tweet: ITweet): string {
        // Bluesky のリポスト通知は元投稿と同じ AT URI を共有する
        // 投稿本体とリポスト行を同じキーで畳むと、タイムライン上のリポスト表示が消えてしまう
        if (tweet.source === 'Bluesky' && tweet.retweeted_tweet !== null) {
            return `${tweet.source}:${tweet.id}:repost:${tweet.user.id}`;
        }
        return `${tweet.source}:${tweet.id}`;
    }

    /**
     * 既出ツイートのキーセットと突き合わせ、重複しないツイートだけを返す
     * @param tweets フィルタ対象のツイート配列
     * @param existingIds 既出のキーを格納した Set
     * @returns 重複を除いたツイート配列
     */
    static filterDuplicateTweets(tweets: ITweet[], existingIds: Set<string>): ITweet[] {
        return tweets.filter(tweet => !existingIds.has(TweetUtils.getTweetIdentityKey(tweet)));
    }

    /**
     * ツイート配列を投稿時刻の新しい順に破壊的に並べ替える
     * 元の配列を破壊的にソートして返す (呼び出し側で配列を保護したい場合は事前にコピーすること)
     * @param tweets ソート対象のツイート配列
     * @returns ソート後のツイート配列
     */
    static sortTweetsByCreatedAtInPlace(tweets: ITweet[]): ITweet[] {
        return tweets.sort((a, b) => dayjs(b.created_at).valueOf() - dayjs(a.created_at).valueOf());
    }

    /**
     * ハッシュタグセットを正規化してリプライツリー判定用キーへ変換する
     * @param hashtags 実際に投稿本文へ付与するハッシュタグ一覧
     * @returns 大文字小文字と順序を無視した正規化キー
     */
    static normalizeHashtagKey(hashtags: string[]): string {
        return [...new Set(hashtags
            .map(hashtag => hashtag.replace(/^#/, '').toLowerCase()))]
            .sort()
            .join(',');
    }

    /**
     * 朝 4 時を境界にした実況日キーを返す
     * @param value 判定対象の日時
     * @returns 朝 4 時境界に丸めた Unix epoch (ミリ秒)
     */
    static floorTo4amBoundary(value: string | ReturnType<typeof dayjs>): number {
        const date = dayjs(value);
        // 深夜帯の実況は前日の番組枠として扱い、深夜アニメの途中で日付だけが変わってもツリーを分断しない
        const adjusted = date.hour() < 4 ? date.subtract(1, 'day') : date;
        return adjusted.hour(4).minute(0).second(0).millisecond(0).valueOf();
    }

    /**
     * 現在の設定と保存済み状態からリプライツリーとして送信するかを判定する
     * @param args.mode リプライツリー実況モード
     * @param args.state アカウントごとの保存済みリプライツリー状態
     * @param args.current_hashtag_key 現在投稿するハッシュタグセットの正規化キー
     * @param args.now 判定に使う現在時刻
     * @returns 送信方法と送信成功後の状態更新方針
     */
    static decideReplyThread(args: {
        mode: ReplyThreadMode;
        state: {started_at: string; hashtag_key: string;} | undefined;
        current_hashtag_key: string;
        now: ReturnType<typeof dayjs>;
    }): IReplyThreadDecision {

        if (args.mode === 'Disabled') {
            return {
                send_as_reply: false,
                reset_state_after: false,
                clear_state: false,
            };
        }

        if (args.mode === 'PerHashtag') {
            // ハッシュタグ単位のツリーでは、タグなし投稿を文脈のない単独投稿として扱い、前回ツリーも明示的に切る
            if (args.current_hashtag_key === '') {
                return {
                    send_as_reply: false,
                    reset_state_after: false,
                    clear_state: true,
                };
            }
            if (args.state === undefined || args.state.hashtag_key !== args.current_hashtag_key) {
                return {
                    send_as_reply: false,
                    reset_state_after: true,
                    clear_state: false,
                };
            }
            return {
                send_as_reply: true,
                reset_state_after: false,
                clear_state: false,
            };
        }

        if (args.mode === 'PerDay') {
            if (args.state === undefined) {
                return {
                    send_as_reply: false,
                    reset_state_after: true,
                    clear_state: false,
                };
            }

            // 1 日 1 ツリーでは朝 4 時境界だけを見るため、番組タグの有無や変更はツリー切替条件に含めない
            if (TweetUtils.floorTo4amBoundary(args.state.started_at) !== TweetUtils.floorTo4amBoundary(args.now)) {
                return {
                    send_as_reply: false,
                    reset_state_after: true,
                    clear_state: false,
                };
            }
            return {
                send_as_reply: true,
                reset_state_after: false,
                clear_state: false,
            };
        }

        const unknown_mode: never = args.mode;
        throw new Error(`Unknown reply thread mode: ${unknown_mode}`);
    }

    /**
     * Tweet 本文を URL・メンション・ハッシュタグのリンクセグメントとテキストセグメントへ分解する
     * 以前は HTML 文字列を組み立てて v-html へ渡していたが、本文に含まれる任意の文字列が
     * そのまま HTML として解釈される stored XSS になるため、リンク要素とテキストを分離して
     * 呼び出し側でテキストノードと固定属性の <a> 要素として描画する
     * なお、本文を書き換えるプレースホルダー方式は本文中の文字列と衝突して欠落・重複が起きるため使わず、
     * URL の前後へ挟まれたテキスト部分だけへメンション・ハッシュタグの走査を行う
     * @param text 表示対象のツイート本文
     * @param source 投稿元サービス (Twitter / Bluesky)
     * @returns 描画用に分解したセグメント配列
     */
    static tokenizeTweetText(text: string, source: ITweet['source']): TweetTextSegment[] {

        const urlRegex = /(https?:\/\/[^\s]+)/g;
        const segments: TweetTextSegment[] = [];

        // URL を先に確定させ、URL 内部の @ や # をメンション・ハッシュタグとして処理しないようにする
        // テキストを書き換えないため、URL と本文の境界をカーソルで走査して URL の前後のテキスト部分だけへ走査を行う
        let cursor = 0;
        for (const urlMatch of text.matchAll(urlRegex)) {
            // URL より前のテキスト部分を追加する
            TweetUtils.appendInlineSegments(segments, text.slice(cursor, urlMatch.index), source);
            // URL は固定属性のリンクセグメントとして追加する
            segments.push({ type: 'link', text: urlMatch[0], url: urlMatch[0] });
            cursor = urlMatch.index + urlMatch[0].length;
        }
        // 最後の URL 以降のテキスト部分を追加する
        TweetUtils.appendInlineSegments(segments, text.slice(cursor), source);
        return segments;
    }

    /**
     * テキスト部分に含まれるメンション・ハッシュタグをセグメント化して末尾へ追加する
     * @param segments 追加先のセグメント配列
     * @param text メンション・ハッシュタグを探すテキスト部分
     * @param source 投稿元サービス (Twitter / Bluesky)
     */
    private static appendInlineSegments(segments: TweetTextSegment[], text: string, source: ITweet['source']): void {

        // メンションとハッシュタグをセグメントとして切り出す正規表現
        // Bluesky のメンションは handle 形式 (例: @user.example.com) で、Twitter は @ に英数字・アンダースコアが続く短縮形
        // キャプチャグループ: 1=メンション全体, 2=スクリーンネーム, 3=ハッシュタグ全体, 4=ハッシュタグ文字列
        const mentionPattern = source === 'Bluesky' ? '@([a-zA-Z0-9][a-zA-Z0-9.-]*\\.[a-zA-Z][a-zA-Z0-9.-]*)' : '@(\\w+)';
        const hashtagPattern = '[#＃]([\\w\\p{Script=Hiragana}\\p{Script=Katakana}\\p{Script=Han}ー]+)';
        const inlineRegex = new RegExp(`(${mentionPattern})|(${hashtagPattern})`, 'gu');

        let cursor = 0;
        for (const match of text.matchAll(inlineRegex)) {
            // マッチ位置までのテキスト部分をテキストセグメントとして追加する
            if (match.index > cursor) {
                segments.push({ type: 'text', text: text.slice(cursor, match.index) });
            }
            if (match[1] !== undefined) {
                // メンションはサービスごとのプロフィール URL へのリンクにする
                const screenName = match[2];
                const mentionUrl = source === 'Bluesky' ? `https://bsky.app/profile/${screenName}` : `https://x.com/${screenName}`;
                segments.push({ type: 'link', text: `@${screenName}`, url: mentionUrl });
            } else {
                // ハッシュタグはサービスごとのハッシュタグ検索 URL へのリンクにする
                const hashtag = match[4];
                const hashtagUrl = source === 'Bluesky' ?
                    `https://bsky.app/hashtag/${encodeURIComponent(hashtag)}` :
                    `https://x.com/hashtag/${encodeURIComponent(hashtag)}`;
                segments.push({ type: 'link', text: `#${hashtag}`, url: hashtagUrl });
            }
            cursor = match.index + match[0].length;
        }
        // 最後のマッチ以降のテキスト部分を追加する
        if (cursor < text.length) {
            segments.push({ type: 'text', text: text.slice(cursor) });
        }
    }
}
