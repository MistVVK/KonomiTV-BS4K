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

    it('ログレベルを含まない行は level が null になり、行全体が rest として返される', () => {
        const result = formatLogLine('<img src=x onerror=alert(1)>');
        expect(result).toEqual({
            level: null,
            prefix: '',
            rest: '<img src=x onerror=alert(1)>',
        });
    });

    it('行頭の最初のログレベルだけを色付け対象にする', () => {
        const result = formatLogLine('CRITICAL: first INFO: second');
        expect(result.level).toBe('CRITICAL');
        expect(result.prefix).toBe('');
        expect(result.rest).toBe(' first INFO: second');
    });
});
