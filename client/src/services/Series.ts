
import APIClient from '@/services/APIClient';
import { IChannel } from '@/services/Channels';
import { IRecordedProgram } from '@/services/Videos';


/** シリーズ内で構造化されたシーズン・話数。 */
export interface ISeriesEpisode {
    id: number;
    season_number: number;
    episode_number: string;
}

/** Series API に限って、構造化話数を含む録画番組情報。 */
export interface ISeriesRecordedProgram extends IRecordedProgram {
    /** ローリング更新中の旧サーバー応答ではフィールド自体がない場合がある。 */
    series_episode?: ISeriesEpisode | null;
    /** 番号付き Episode がない録画の正本シーズン・状態。旧サーバーでは未定義。 */
    episode_resolution?: ISeriesEpisodeResolution | null;
}

/** Series 表示と並び替えに必要な録画ごとの話数正本。 */
export interface ISeriesEpisodeResolution {
    season_number: number | null;
    status: 'Pending' | 'Resolved' | 'Unknown' | 'NotNumbered' | 'NoPublishedNumber' | 'NeedsReview' | 'Failed';
}


/** シリーズ情報を表すインターフェース */
export interface ISeries {
    id: number;
    title: string;
    description: string;
    genres: { major: string; middle: string; }[];
    bangumi_subject_id: number | null;
    bangumi_subject_name: string | null;
    bangumi_subject_name_cn: string | null;
    bangumi_subject_summary: string | null;
    bangumi_subject_image_url: string | null;
    episodes?: ISeriesEpisode[];
    broadcast_periods: ISeriesBroadcastPeriod[];
    created_at: string;
    updated_at: string;
}

/** シリーズ情報リストを表すインターフェース */
export interface ISeriesList {
    total: number;
    series_list: ISeries[];
}

/** カタログカード用のシリーズ要約。 */
export interface ISeriesSummary {
    id: number;
    title: string;
    description: string;
    genres: { major: string; middle: string; }[];
    bangumi_subject_id: number | null;
    bangumi_subject_name: string | null;
    bangumi_subject_name_cn: string | null;
    bangumi_subject_summary: string | null;
    bangumi_subject_image_url: string | null;
    recorded_count: number;
    unrecorded_count: number;
    partial_count: number;
    latest_recorded_program_id: number | null;
    updated_at: string;
}

/** カタログ一覧のページング応答。 */
export interface ISeriesSummaryList {
    total: number;
    page_size: number;
    series_list: ISeriesSummary[];
}

/** 放送中グリッドの 1 スロット。時刻は自然時刻。 */
export interface ISeriesOnAirSlot {
    weekday: number;
    hour: number;
    minute: number;
    is_featured: boolean;
    series: ISeriesSummary;
}

/** 放送中グリッドの 1 曜日。 */
export interface ISeriesOnAirDay {
    weekday: number;
    slots: ISeriesOnAirSlot[];
}

/** 放送中グリッド応答。 */
export interface ISeriesOnAirResponse {
    days: ISeriesOnAirDay[];
}

/** `/series/:id` の深いリンク用ページ番号。 */
export interface ISeriesListPosition {
    page: number;
}

/** シリーズ放送期間を表すインターフェース */
export interface ISeriesBroadcastPeriod {
    channel: IChannel;
    start_date: string;
    end_date: string;
    recorded_programs: ISeriesRecordedProgram[];
}


class Series {

    /**
     * シリーズ一覧を取得する
     * @param order ソート順序 ('desc' or 'asc')
     * @param page ページ番号
     * @returns シリーズ一覧情報 or シリーズ一覧情報の取得に失敗した場合は null
     */
    static async fetchSeriesList(order: 'desc' | 'asc' = 'desc', page: number = 1): Promise<ISeriesList | null> {

        // API リクエストを実行
        const response = await APIClient.get<ISeriesList>('/series', {
            params: {
                order,
                page,
            },
        });

        // エラー処理
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'シリーズ一覧を取得できませんでした。');
            return null;
        }

        return response.data;
    }


    /**
     * シリーズ番組を検索する
     * @param query 検索キーワード
     * @param order ソート順序 ('desc' or 'asc')
     * @param page ページ番号
     * @returns 検索結果のシリーズ番組一覧情報 or 検索に失敗した場合は null
     */
    static async searchSeries(query: string, order: 'desc' | 'asc' = 'desc', page: number = 1): Promise<ISeriesList | null> {

        // API リクエストを実行
        const response = await APIClient.get<ISeriesList>('/series/search', {
            params: {
                query,
                order,
                page,
            },
        });

        // エラー処理
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'シリーズ番組の検索に失敗しました。');
            return null;
        }

        return response.data;
    }


    /**
     * シリーズ情報を取得する
     * @param series_id シリーズ ID
     * @returns シリーズ情報 or シリーズ情報の取得に失敗した場合は null
     */
    static async fetchSeries(series_id: number): Promise<ISeries | null> {

        // API リクエストを実行
        const response = await APIClient.get<ISeries>(`/series/${series_id}`);

        // エラー処理
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'シリーズ情報を取得できませんでした。');
            return null;
        }

        return response.data;
    }


    /**
     * カタログカード用のシリーズ要約一覧を取得する
     * @param order ソート順序 ('desc' or 'asc')
     * @param page ページ番号
     * @param query 検索キーワード
     * @returns 要約一覧 or 失敗時は null
     */
    static async fetchSeriesSummaries(
        order: 'desc' | 'asc' = 'desc',
        page: number = 1,
        query: string = '',
    ): Promise<ISeriesSummaryList | null> {

        const response = await APIClient.get<ISeriesSummaryList>('/series/summary', {
            params: {
                order,
                page,
                query,
            },
        });
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'シリーズ一覧を取得できませんでした。');
            return null;
        }
        return response.data;
    }


    /**
     * 今クール相当の週間レギュラーを取得する
     * @returns 曜日ごとのスロット or 失敗時は null
     */
    static async fetchOnAirSeries(): Promise<ISeriesOnAirResponse | null> {

        const response = await APIClient.get<ISeriesOnAirResponse>('/series/on-air');
        if (response.type === 'error') {
            APIClient.showGenericError(response, '放送中のシリーズを取得できませんでした。');
            return null;
        }
        return response.data;
    }


    /**
     * カタログ一覧で指定シリーズが載るページ番号を取得する
     * @param series_id シリーズ ID
     * @param order ソート順序
     * @param query 検索キーワード
     * @returns ページ番号 or 失敗時は null
     */
    static async fetchSeriesListPosition(
        series_id: number,
        order: 'desc' | 'asc' = 'desc',
        query: string = '',
    ): Promise<number | null> {

        const response = await APIClient.get<ISeriesListPosition>('/series/list-position', {
            params: {
                series_id,
                order,
                query,
            },
        });
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'シリーズの一覧位置を取得できませんでした。');
            return null;
        }
        return response.data.page;
    }
}

export default Series;
