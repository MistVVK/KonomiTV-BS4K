import { describe, expect, it } from 'vitest';

import type { IRecordedEpisodeAssignmentResolution } from '@/services/RecordedSeries';

import {
    canAdoptAIEpisodeResolution,
    canCloseEpisodeAssignmentDialog,
    episodeLookupOutcomeLabel,
    episodeLookupOutcomeDisplayLabel,
    episodeResolutionStatusLabel,
    markResolutionLookupPending,
    recordedEpisodeResolutionErrorMessage,
    recordedEpisodeResolutionProposal,
    recordedEpisodeResolutionRationale,
} from '@/utils/RecordedEpisodeResolution';


function createResolution(
    overrides: Partial<IRecordedEpisodeAssignmentResolution> = {},
): IRecordedEpisodeAssignmentResolution {
    return {
        status: 'NeedsReview',
        season_number: null,
        source: 'WebSearch',
        lookup_outcome: null,
        proposed_outcome: null,
        proposed_season_number: null,
        proposed_episode_number: null,
        confidence: null,
        web_search_performed: false,
        citations: [],
        rationale_short: null,
        manual_season_number: null,
        manual_episode_number: null,
        manual_status: null,
        error_code: null,
        error_message: null,
        ...overrides,
    };
}


describe('RecordedEpisodeResolution 表示契約', () => {

    it('lookup_outcome がない検索済み旧行は status・source・実行記録を表示する', () => {
        const resolution = createResolution({
            status: 'NeedsReview',
            source: 'WebSearch',
            web_search_performed: true,
        });

        expect(episodeLookupOutcomeDisplayLabel(resolution)).toBe(
            '要確認（AI Web 検索・Web 検索実行済み）',
        );
        expect(episodeLookupOutcomeDisplayLabel(resolution)).not.toContain('未実行');
    });

    it('lookup_outcome がない決定論的な旧行は status と source へフォールバックする', () => {
        const resolution = createResolution({
            status: 'Resolved',
            source: 'Local',
        });

        expect(episodeLookupOutcomeDisplayLabel(resolution)).toBe(
            '確定（ローカル情報・Web 検索記録なし）',
        );
    });

    it('一括生成済み Episode は AI source を専用ラベルで表示する', () => {
        const resolution = createResolution({
            status: 'Resolved',
            source: 'AI',
        });

        expect(episodeLookupOutcomeDisplayLabel(resolution)).toBe(
            '確定（AI 一括生成・Web 検索記録なし）',
        );
    });

    it('NotNumbered を NoPublishedNumber と区別して話数番号なしと表示する', () => {
        expect(episodeLookupOutcomeLabel('NotNumbered')).toBe('検索根拠から話数番号なし');
        expect(episodeResolutionStatusLabel('NotNumbered')).toBe('話数番号なし');
        expect(episodeLookupOutcomeLabel('NoPublishedNumber')).toBe('検索根拠から公開話数なし');
        expect(episodeResolutionStatusLabel('NoPublishedNumber')).toBe('公開話数なし');
    });

    it('判定根拠と安全なエラーを互いに隠さず個別に返す', () => {
        const resolution = createResolution({
            rationale_short: '  公式の放送日と一致しました。  ',
            error_code: 'EpisodeLookupFailed',
        });

        expect(recordedEpisodeResolutionRationale(resolution)).toBe('公式の放送日と一致しました。');
        expect(recordedEpisodeResolutionErrorMessage(resolution)).toBe('話数 Web 検索に失敗しました。');
    });

    it('完全な AI 提案だけを保存用の編集値として返す', () => {
        expect(recordedEpisodeResolutionProposal(createResolution({
            proposed_outcome: 'Resolved',
            proposed_season_number: 2,
            proposed_episode_number: '12.5',
        }))).toEqual({
            seasonNumber: 2,
            episodeNumber: '12.5',
        });
        expect(recordedEpisodeResolutionProposal(createResolution({
            proposed_outcome: 'Resolved',
            proposed_season_number: 2,
            proposed_episode_number: null,
        }))).toBeNull();
        expect(recordedEpisodeResolutionProposal(null)).toBeNull();
    });

    it.each([
        ['LowConfidence', 'Web 検索結果の信頼度が受理条件を満たしませんでした。'],
        ['EpisodeLookupFailed', '話数 Web 検索に失敗しました。'],
        ['Timeout', 'AI バックエンドからの応答が一定時間途絶えたためタイムアウトしました。'],
        [
            'HardTimeout',
            'AI バックエンドの総実行時間（他の ACP 実行待ちを含む）が安全上限の 60 分を超えたため停止しました。',
        ],
    ])('既知コード %s を安全な日本語へ補完する', (errorCode, expected) => {
        const resolution = createResolution({error_code: errorCode});

        expect(recordedEpisodeResolutionErrorMessage(resolution)).toBe(expected);
    });

    it('保存・再検索開始・再検索監視のいずれかが進行中なら閉じられない', () => {
        expect(canCloseEpisodeAssignmentDialog(false, false, false)).toBe(true);
        expect(canCloseEpisodeAssignmentDialog(true, false, false)).toBe(false);
        expect(canCloseEpisodeAssignmentDialog(false, true, false)).toBe(false);
        expect(canCloseEpisodeAssignmentDialog(false, false, true)).toBe(false);
    });

    it('再検索開始の楽観更新は error / 根拠を消し、前回成功 evidence は維持する', () => {
        const resolution = createResolution({
            status: 'Resolved',
            source: 'WebSearch',
            lookup_outcome: 'SearchFailed',
            proposed_season_number: 1,
            proposed_episode_number: '10',
            confidence: 0.99,
            web_search_performed: true,
            citations: [{url: 'https://example.com/ep', title: '公式'}],
            rationale_short: '前回の成功根拠',
            error_code: 'EpisodeLookupFailed',
            error_message: '話数 Web 検索に失敗しました。',
        });

        expect(markResolutionLookupPending(resolution)).toEqual({
            ...resolution,
            lookup_outcome: 'Pending',
            error_code: null,
            error_message: null,
            rationale_short: null,
        });
    });

    it('AI の番号付きまたは番号なし提案があるときだけ採用可能と判定する', () => {
        expect(canAdoptAIEpisodeResolution(createResolution())).toBe(false);
        expect(canAdoptAIEpisodeResolution(createResolution({
            proposed_outcome: 'Resolved',
            proposed_season_number: 1,
            proposed_episode_number: '3',
        }))).toBe(true);
        expect(canAdoptAIEpisodeResolution(createResolution({
            proposed_outcome: 'NotNumbered',
        }))).toBe(true);
    });
});
