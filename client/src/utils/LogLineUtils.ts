// ログ行を「色付け対象のログレベル」と「それ以外の本文」へ分解したもの
export interface FormattedLogLine {
    // 色付け対象のログレベル文字列。ログレベルを含まない行では null
    level: string | null;
    // ログレベルとコロンより前の本文 (テキストとして表示する)
    prefix: string;
    // ログレベルとコロンより後の本文 (テキストとして表示する)
    rest: string;
}

/**
 * ログ行をログレベル (色付け対象) と本文へ分解する
 * 以前は行全体を HTML 文字列へ変換して v-html で表示していたが、ログ本文に含まれる任意の文字列
 * (ユーザー名など) がそのまま HTML として解釈される stored XSS になるため、ログレベル部分だけを
 * 切り出し、本文はテキストノードとして表示できるようにする
 * @param line 分解対象のログ行
 * @returns ログレベルと本文に分解した結果
 */
export function formatLogLine(line: string): FormattedLogLine {

    // ログレベルのパターン (行内で最初に現れるものだけを色付け対象にする)
    const logLevelPattern = /(DEBUG|INFO|WARNING|ERROR|CRITICAL):/;
    const match = line.match(logLevelPattern);
    if (!match || match.index === undefined) {
        return { level: null, prefix: '', rest: line };
    }
    return {
        level: match[1],
        prefix: line.slice(0, match.index),
        rest: line.slice(match.index + match[0].length),
    };
}
