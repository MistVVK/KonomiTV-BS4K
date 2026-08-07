import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import LogLine from '@/components/Settings/LogLine.vue';

describe('LogLine', () => {

    it('悪意ある文字列を含むログ行がテキストとして表示され、注入要素や event handler が生成されない', () => {
        const wrapper = mount(LogLine, {
            props: { line: '[username: <img src=x onerror=alert(1)>] WARNING: login failed' },
        });
        expect(wrapper.find('img').exists()).toBe(false);
        // span 以外の要素や on* 属性を持たないことを検証する
        expect(wrapper.findAll('*').every((el) => el.element.tagName === 'SPAN')).toBe(true);
        expect(wrapper.findAll('*').every((el) => Object.keys(el.attributes()).every((attr) => !attr.startsWith('on')))).toBe(true);
        expect(wrapper.text()).toContain('<img src=x onerror=alert(1)>');
        // 色付け対象のログレベルだけが span で表示される
        const levelSpan = wrapper.find('span');
        expect(levelSpan.exists()).toBe(true);
        expect(levelSpan.text()).toBe('WARNING');
        expect(levelSpan.attributes('style')).toContain('color:');
    });

    it('ログレベルを含まない行は span なしでテキストのまま表示される', () => {
        const wrapper = mount(LogLine, {
            props: { line: '<script>alert(1)</script>' },
        });
        expect(wrapper.find('span').exists()).toBe(false);
        expect(wrapper.find('script').exists()).toBe(false);
        expect(wrapper.text()).toBe('<script>alert(1)</script>');
    });
});
