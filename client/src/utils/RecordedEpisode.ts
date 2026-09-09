/**
 * APIの構造化話数を、指数表記を使わない最大3桁の小数として表示する。
 * Tortoise DecimalFieldを通った旧応答では、10が `1E+1` になることがあるためクライアントでも防御する。
 */
export function formatRecordedEpisodeNumber(episode_number: string): string {
    const normalized_episode_number = episode_number.trim();
    if (normalized_episode_number === '') return episode_number;

    const numeric_episode_number = Number(normalized_episode_number);
    if (
        Number.isFinite(numeric_episode_number) === false ||
        numeric_episode_number < 0 ||
        numeric_episode_number > 9_999_999.999
    ) {
        return episode_number;
    }
    return numeric_episode_number.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
}

/** 番号付き回を管理画面・視聴画面で共通の短い表記へ整形する。 */
export function formatRecordedEpisodeLabel(seasonNumber: number, episodeNumber: string): string {
    return `S${seasonNumber}・第${formatRecordedEpisodeNumber(episodeNumber)}話`;
}

/**
 * 録画の副題を、話数に紐づく題名として表示できるか検証する。
 * シリーズ名・枠名・作品名の反復や装飾だけの値は採用せず、内容を表す副題だけを返す。
 */
export function extractValidEpisodeSubtitle(
    subtitle: string | null | undefined,
    excludedLabels: (string | null | undefined)[],
): string | null {
    const trimmedSubtitle = subtitle?.trim();
    if (trimmedSubtitle === undefined || hasMeaningfulLabelContent(trimmedSubtitle) === false) return null;
    const normalizedSubtitle = normalizeComparableLabel(trimmedSubtitle);
    if (excludedLabels.some((label) => label !== null && label !== undefined
        && normalizeComparableLabel(label) === normalizedSubtitle)) {
        return null;
    }
    return trimmedSubtitle;
}

function normalizeComparableLabel(label: string): string {
    // 幅・空白・大文字小文字だけの差を除き、題名中の意味のある記号は同一性比較でも保持する。
    return label.normalize('NFKC').toLocaleLowerCase('ja-JP').replace(/\s/gu, '');
}

function hasMeaningfulLabelContent(label: string): boolean {
    // variation selector や不可視の書式文字が残っても、文字・数字を含まない装飾だけの値は採用しない。
    return /[\p{L}\p{N}]/u.test(label.normalize('NFKC'));
}

/** Episodeを持たない確定状態を、任意の正本シーズン付きで表示する。 */
export function formatRecordedUnnumberedEpisodeLabel(
    seasonNumber: number | null,
    status: 'NotNumbered' | 'NoPublishedNumber',
): string {
    const prefix = seasonNumber === null ? '' : `S${seasonNumber}・`;
    return `${prefix}${status === 'NoPublishedNumber' ? '公開話数なし' : '話数番号なし'}`;
}
