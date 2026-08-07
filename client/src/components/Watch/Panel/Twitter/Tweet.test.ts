import { DOMWrapper, mount } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { describe, expect, it } from 'vitest';

import Tweet from '@/components/Watch/Panel/Twitter/Tweet.vue';
import { ITweet } from '@/services/Twitter';
import { dayjs } from '@/utils';

// テスト用のツイートを生成する
const createTweet = (text: string, extra: Partial<ITweet> = {}): ITweet => ({
    source: 'Twitter',
    id: '123',
    created_at: dayjs('2026-08-07T12:00:00+09:00').toDate(),
    user: { source: 'Twitter', id: '1', name: 'テスト', screen_name: 'test_user', icon_url: '' },
    text,
    lang: 'ja',
    via: '',
    image_urls: null,
    movie_url: null,
    retweet_count: 0,
    retweeted: false,
    favorite_count: 0,
    favorited: false,
    retweeted_tweet: null,
    quoted_tweet: null,
    ...extra,
});

// 本文コンテナ内の要素がすべて <a> かつ event handler 属性を持たないことを検証する
const expectNoInjectionIn = (container: DOMWrapper<Element>) => {
    const elements = container.findAll('*');
    for (const element of elements) {
        expect(element.element.tagName).toBe('A');
        for (const attribute of Object.keys(element.attributes())) {
            expect(attribute.startsWith('on')).toBe(false);
        }
    }
};

// テスト用のマウントヘルパー (pinia を注入し、未解決の ripple ディレクティブ警告を抑える)
const mountTweet = (tweet: ITweet) => {
    return mount(Tweet, {
        props: { tweet },
        global: {
            plugins: [createPinia()],
            directives: { ripple: {} },
        },
    });
};

describe('Tweet', () => {

    it('悪意ある本文が HTML 要素や event handler として描画されない', () => {
        const wrapper = mountTweet(createTweet('<img src=x onerror=alert(1)><script>alert(1)</script>'));
        const textContainer = wrapper.find('.tweet__text');
        expectNoInjectionIn(textContainer);
        // 悪意ある文字列はエスケープされた状態のテキストとして表示される
        expect(textContainer.text()).toContain('<img src=x onerror=alert(1)>');
        expect(textContainer.text()).toContain('<script>alert(1)</script>');
    });

    it('event handler を含む URL は href 属性値に閉じ込められ、要素属性として生成されない', () => {
        const wrapper = mountTweet(createTweet('https://example.com/foo"onmouseover="alert(1)'));
        const textContainer = wrapper.find('.tweet__text');
        expectNoInjectionIn(textContainer);
        // onmouseover は href 属性の値の中に文字列として含まれるだけで、要素属性にはならない
        const link = textContainer.find('a');
        expect(link.attributes('href')).toBe('https://example.com/foo"onmouseover="alert(1)');
        expect(link.attributes('onmouseover')).toBeUndefined();
    });

    it('通常の URL・メンション・ハッシュタグはリンクとして表示される', () => {
        const wrapper = mountTweet(createTweet('see https://x.com/abc @konomi_tv #test'));
        const links = wrapper.find('.tweet__text').findAll('a.tweet-link');
        expect(links).toHaveLength(3);
        expect(links[0].attributes('href')).toBe('https://x.com/abc');
        expect(links[1].attributes('href')).toBe('https://x.com/konomi_tv');
        expect(links[2].attributes('href')).toBe('https://x.com/hashtag/test');
        expect(wrapper.find('.tweet__text').text()).toContain('see ');
    });

    it('Bluesky のメンションは bsky.app へのリンクとして表示される', () => {
        const wrapper = mountTweet(createTweet('@user.example.com さん', { source: 'Bluesky' }));
        const link = wrapper.find('.tweet__text').find('a.tweet-link');
        expect(link.attributes('href')).toBe('https://bsky.app/profile/user.example.com');
        expect(link.text()).toBe('@user.example.com');
    });

    it('改行はテキストのまま表示される', () => {
        const wrapper = mountTweet(createTweet('1行目\n2行目'));
        expect(wrapper.find('.tweet__text').text()).toContain('1行目\n2行目');
    });

    it('本文中の __URL_PLACEHOLDER_<n>__ 文字列はテキストのまま表示される', () => {
        const wrapper = mountTweet(createTweet('__URL_PLACEHOLDER_0__ https://x.com/a'));
        const textContainer = wrapper.find('.tweet__text');
        expect(textContainer.findAll('a.tweet-link')).toHaveLength(1);
        expect(textContainer.text()).toContain('__URL_PLACEHOLDER_0__');
    });

    it('引用ツイートの悪意ある本文も HTML 要素や event handler として描画されない', () => {
        const wrapper = mountTweet(createTweet('元の本文', {
            quoted_tweet: createTweet('<img src=x onerror=alert(1)>'),
        }));
        const quotedTextContainer = wrapper.find('.tweet__quoted-text');
        expectNoInjectionIn(quotedTextContainer);
        expect(quotedTextContainer.text()).toContain('<img src=x onerror=alert(1)>');
    });
});
