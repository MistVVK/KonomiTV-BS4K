
/**
 * シリーズの表示名を外部名優先で組み立てる。
 * Bangumi 原名を最優先とし、TMDb 名は Season 2 以上にバインド済みのときだけ末尾へ半角 S{N} を付ける。
 * どちらも無ければローカル題名を使う。ソートキーや検索の照合対象はこの表示名に寄せず、従来のローカル題名のまま。
 */
export function formatSeriesDisplayName(series: {
    title: string;
    bangumi_subject_name: string | null;
    tmdb_name: string | null;
    tmdb_season_number: number | null;
}): string {
    if (series.bangumi_subject_name) {
        return series.bangumi_subject_name;
    }
    if (series.tmdb_name) {
        if (series.tmdb_season_number !== null && series.tmdb_season_number > 1) {
            return `${series.tmdb_name} S${series.tmdb_season_number}`;
        }
        return series.tmdb_name;
    }
    return series.title;
}
