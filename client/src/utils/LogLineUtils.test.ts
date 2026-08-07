import { describe, expect, it } from 'vitest';

import { formatLogLine } from '@/utils/LogLineUtils';

describe('formatLogLine', () => {

    it('ログレベルを色付け対象として本文と分離する', () => {
        const result = formatLogLine('2026/08/07 12:00:00 INFO: message body');
        expect(result).toEqual({
            level: 'INFO',
            prefix: '2026/08/07 12:00:00 ',
            rest: ' message body',
        });
    });

    it('本文に HTML のような悪意ある文字列が含まれても、もとの文字列のまま本文として保持される', () => {
        const result = formatLogLine('[username: <img src=x onerror=alert(1)>] WARNING: login failed');
        expect(result.level).toBe('WARNING');
        expect(result.rest).toBe(' login failed');
        // 本文は HTML へ変換されず、テキストとして表示するための生の文字列として返される
        expect(result.prefix).toBe('[username: <img src=x onerror=alert(1)>] ');
    });

    it('ログレベルを含まない行は level が null になり、行全体が rest として返される', () => {
        const result = formatLogLine('<img src=x onerror=alert(1)>');
        expect(result).toEqual({
            level: null,
            prefix: '',
            rest: '<img src=x onerror=alert(1)>',
        });
    });

    it('CRITICAL もログレベルとして検出される', () => {
        const result = formatLogLine('CRITICAL: fatal error');
        expect(result.level).toBe('CRITICAL');
        expect(result.prefix).toBe('');
        expect(result.rest).toBe(' fatal error');
    });

    it('ログレベルが行頭にある場合は prefix が空文字になる', () => {
        const result = formatLogLine('INFO: started');
        expect(result.level).toBe('INFO');
        expect(result.prefix).toBe('');
        expect(result.rest).toBe(' started');
    });

    it('行内で最初に現れるログレベルだけを色付け対象にする', () => {
        const result = formatLogLine('DEBUG: first INFO: second');
        expect(result.level).toBe('DEBUG');
        expect(result.rest).toBe(' first INFO: second');
    });
});
