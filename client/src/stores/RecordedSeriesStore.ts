import { defineStore } from 'pinia';

import type { ISeries, ISeriesRecordedProgram } from '@/services/Series';
import type { VideoSeriesSortDirection, VideoSeriesSortKey } from '@/stores/SettingsStore';

import { dayjs } from '@/utils';


const japanese_title_collator = new Intl.Collator('ja-JP', {
    numeric: true,
    sensitivity: 'base',
});


/** Series API に含まれる再生可能な録画を、同じ録画 ID の重複だけ取り除いて平坦化する。 */
export function flattenRecordedSeriesPrograms(series: ISeries): ISeriesRecordedProgram[] {
    const unique_programs = new Map<number, ISeriesRecordedProgram>();
    for (const period of series.broadcast_periods) {
        for (const program of period.recorded_programs) {
            // reverse relation の prefetch には解析中・失敗した録画も含まれ得るため、Recorded だけを対象にする。
            if (program.recorded_video.status === 'Recorded') {
                unique_programs.set(program.id, program);
            }
        }
    }
    return Array.from(unique_programs.values());
}


/** 放送日時、次いで録画番組 ID の昇順で比較する。 */
function compareByBroadcastDateAscending(first: ISeriesRecordedProgram, second: ISeriesRecordedProgram): number {
    return dayjs(first.start_time).valueOf() - dayjs(second.start_time).valueOf() || first.id - second.id;
}


/** Decimal 文字列として返る話数を数値比較する。 */
function compareEpisodeNumber(first: string, second: string): number {
    const first_number = Number(first);
    const second_number = Number(second);
    if (Number.isFinite(first_number) && Number.isFinite(second_number)) {
        return first_number - second_number;
    }
    // API が一時的に想定外の値を返しても並び替え全体を壊さず、決定的な順序を維持する。
    return first.localeCompare(second, 'en', {numeric: true});
}


/** Series 一覧で実際に表示する録画タイトル。五十音順も同じ文字列を使う。 */
export function getRecordedSeriesProgramDisplayTitle(program: ISeriesRecordedProgram): string {
    return program.subtitle?.trim() || program.title;
}


/**
 * Series 内の録画を、プレイヤーの表示と連続再生で共通利用する規則に沿って並べる。
 * 同一話・同一タイトルなどの同値要素は、選択方向にかかわらず放送日時、ID の昇順にする。
 */
export function sortRecordedSeriesPrograms(
    programs: readonly ISeriesRecordedProgram[],
    sort_key: VideoSeriesSortKey,
    sort_direction: VideoSeriesSortDirection,
): ISeriesRecordedProgram[] {
    const direction = sort_direction === 'Asc' ? 1 : -1;
    return [...programs].sort((first, second) => {
        if (sort_key === 'SeasonEpisode') {
            // ローリング更新中の旧 API 応答でフィールド自体がない場合も、話数不明として安全に扱う。
            const first_episode = first.series_episode ?? null;
            const second_episode = second.series_episode ?? null;

            // 話数不明は昇順・降順のどちらでも末尾に固定する。
            if (first_episode === null && second_episode !== null) return 1;
            if (first_episode !== null && second_episode === null) return -1;
            if (first_episode !== null && second_episode !== null) {
                const episode_order =
                    first_episode.season_number - second_episode.season_number ||
                    compareEpisodeNumber(first_episode.episode_number, second_episode.episode_number);
                if (episode_order !== 0) return episode_order * direction;
            }
            return compareByBroadcastDateAscending(first, second);
        }

        if (sort_key === 'BroadcastDate') {
            const broadcast_order = dayjs(first.start_time).valueOf() - dayjs(second.start_time).valueOf();
            return broadcast_order !== 0 ? broadcast_order * direction : first.id - second.id;
        }

        const first_title = getRecordedSeriesProgramDisplayTitle(first).normalize('NFKC');
        const second_title = getRecordedSeriesProgramDisplayTitle(second).normalize('NFKC');
        const title_order = japanese_title_collator.compare(first_title, second_title);
        return title_order !== 0 ? title_order * direction : compareByBroadcastDateAscending(first, second);
    });
}


/** Series API の取得結果と、表示・連続再生で共通利用する順序を保持するストア。 */
const useRecordedSeriesStore = defineStore('recordedSeries', {
    state: () => ({
        series_by_id: {} as Record<number, ISeries>,
    }),
    getters: {
        getSeries: state => (series_id: number): ISeries | null => {
            return state.series_by_id[series_id] ?? null;
        },
        getOrderedPrograms: state => (
            series_id: number,
            sort_key: VideoSeriesSortKey,
            sort_direction: VideoSeriesSortDirection,
        ): ISeriesRecordedProgram[] => {
            const series = state.series_by_id[series_id];
            if (series === undefined) return [];
            return sortRecordedSeriesPrograms(flattenRecordedSeriesPrograms(series), sort_key, sort_direction);
        },
    },
    actions: {
        setSeries(series: ISeries): void {
            // 視聴画面では同時に1シリーズだけを使うため、全話を含む過去Seriesを無期限保持しない。
            this.series_by_id = {[series.id]: series};
        },
    },
});


export default useRecordedSeriesStore;
