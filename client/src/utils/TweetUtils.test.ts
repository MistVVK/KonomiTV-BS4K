import { describe, expect, it } from 'vitest';

import { TweetUtils } from '@/utils/TweetUtils';

describe('TweetUtils.tokenizeTweetText', () => {

    it('本文に含まれる HTML や event handler がセグメントとして分離され、HTML 要素へ変換されない', () => {
        const segments = TweetUtils.tokenizeTweetText('<img src=x onerror=alert(1)>', 'Twitter');
        expect(segments).toEqual([{ type: 'text', text: '<img src=x onerror=alert(1)>' }]);
    });

    it('Bluesky 本文に含まれる HTML や event handler もセグメントとして分離され、HTML 要素へ変換されない', () => {
        const segments = TweetUtils.tokenizeTweetText('abc<script>alert(1)</script>def', 'Bluesky');
        expect(segments).toEqual([{ type: 'text', text: 'abc<script>alert(1)</script>def' }]);
    });

    it('URL は本文と分離したリンクセグメントになり、href へ渡す URL はもとの文字列のまま保たれる', () => {
        const segments = TweetUtils.tokenizeTweetText('see https://example.com/abc def', 'Twitter');
        expect(segments).toEqual([
            { type: 'text', text: 'see ' },
            { type: 'link', text: 'https://example.com/abc', url: 'https://example.com/abc' },
            { type: 'text', text: ' def' },
        ]);
    });

    it('event handler を含む URL はリンクセグメントの url 属性に閉じ込められ、属性からの脱出が発生しない', () => {
        // クォートを含む URL は :href バインディングで属性値としてエスケープされるため、onmouseover 等の属性を追加できない
        const url = 'https://example.com/foo"onmouseover="alert(1)';
        const segments = TweetUtils.tokenizeTweetText(url, 'Twitter');
        expect(segments).toEqual([{ type: 'link', text: url, url }]);
    });

    it('空白を含む event handler 風の文字列は URL の残りとしてテキストセグメントへ分離される', () => {
        const segments = TweetUtils.tokenizeTweetText('https://example.com/" onmouseover="alert(1)', 'Twitter');
        expect(segments).toEqual([
            { type: 'link', text: 'https://example.com/"', url: 'https://example.com/"' },
            { type: 'text', text: ' onmouseover="alert(1)' },
        ]);
    });

    it('javascript: URL はリンク化されずテキストセグメントのままになる', () => {
        const segments = TweetUtils.tokenizeTweetText('javascript:alert(1)', 'Twitter');
        expect(segments).toEqual([{ type: 'text', text: 'javascript:alert(1)' }]);
    });

    it('URL 内部の @ や # はメンション・ハッシュタグとして処理されない', () => {
        const segments = TweetUtils.tokenizeTweetText('https://x.com/@foo#bar', 'Twitter');
        expect(segments).toEqual([
            { type: 'link', text: 'https://x.com/@foo#bar', url: 'https://x.com/@foo#bar' },
        ]);
    });

    it('Twitter のメンションは x.com のプロフィール URL へのリンクセグメントになる', () => {
        const segments = TweetUtils.tokenizeTweetText('@konomi_tv さん', 'Twitter');
        expect(segments).toEqual([
            { type: 'link', text: '@konomi_tv', url: 'https://x.com/konomi_tv' },
            { type: 'text', text: ' さん' },
        ]);
    });

    it('Bluesky のメンションは bsky.app のプロフィール URL へのリンクセグメントになる', () => {
        const segments = TweetUtils.tokenizeTweetText('@user.example.com さん', 'Bluesky');
        expect(segments).toEqual([
            { type: 'link', text: '@user.example.com', url: 'https://bsky.app/profile/user.example.com' },
            { type: 'text', text: ' さん' },
        ]);
    });

    it('ハッシュタグは検索 URL へのリンクセグメントになり、全角の # も半角に正規化される', () => {
        const segments = TweetUtils.tokenizeTweetText('＃テレビ', 'Twitter');
        expect(segments).toEqual([
            { type: 'link', text: '#テレビ', url: 'https://x.com/hashtag/%E3%83%86%E3%83%AC%E3%83%93' },
        ]);
    });

    it('URL ・メンション・ハッシュタグが混在する本文を正しい順序でセグメントへ分割する', () => {
        const segments = TweetUtils.tokenizeTweetText('see https://x.com/a @foo #bar', 'Twitter');
        expect(segments).toEqual([
            { type: 'text', text: 'see ' },
            { type: 'link', text: 'https://x.com/a', url: 'https://x.com/a' },
            { type: 'text', text: ' ' },
            { type: 'link', text: '@foo', url: 'https://x.com/foo' },
            { type: 'text', text: ' ' },
            { type: 'link', text: '#bar', url: 'https://x.com/hashtag/bar' },
        ]);
    });

    it('改行はテキストセグメントにそのまま含まれ、表示上の改行が維持される', () => {
        const segments = TweetUtils.tokenizeTweetText('1行目\n2行目', 'Twitter');
        expect(segments).toEqual([{ type: 'text', text: '1行目\n2行目' }]);
    });

    it('本文中の __URL_PLACEHOLDER_<n>__ 文字列と衝突せず、テキストのまま表示される', () => {
        expect(TweetUtils.tokenizeTweetText('__URL_PLACEHOLDER_0__', 'Twitter'))
            .toEqual([{ type: 'text', text: '__URL_PLACEHOLDER_0__' }]);
        expect(TweetUtils.tokenizeTweetText('https://x.com/a __URL_PLACEHOLDER_0__', 'Twitter'))
            .toEqual([
                { type: 'link', text: 'https://x.com/a', url: 'https://x.com/a' },
                { type: 'text', text: ' __URL_PLACEHOLDER_0__' },
            ]);
    });

    it('URL の直前にメンションが続く場合も、メンションと URL が別々のセグメントになる', () => {
        const segments = TweetUtils.tokenizeTweetText('@foohttps://x.com/bar', 'Twitter');
        expect(segments).toEqual([
            { type: 'link', text: '@foo', url: 'https://x.com/foo' },
            { type: 'link', text: 'https://x.com/bar', url: 'https://x.com/bar' },
        ]);
    });

    it('空文字列はセグメントを生成しない', () => {
        expect(TweetUtils.tokenizeTweetText('', 'Twitter')).toEqual([]);
    });
});
