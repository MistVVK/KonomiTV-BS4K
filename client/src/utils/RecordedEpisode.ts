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

/** Episodeを持たない確定状態を、任意の正本シーズン付きで表示する。 */
export function formatRecordedUnnumberedEpisodeLabel(
    seasonNumber: number | null,
    status: 'NotNumbered' | 'NoPublishedNumber',
): string {
    const prefix = seasonNumber === null ? '' : `S${seasonNumber}・`;
    return `${prefix}${status === 'NoPublishedNumber' ? '公開話数なし' : '話数番号なし'}`;
}
