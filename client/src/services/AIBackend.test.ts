import { beforeEach, describe, expect, it, vi } from 'vitest';

import AIBackend from '@/services/AIBackend';
import APIClient from '@/services/APIClient';
import {
    ACP_CONNECTION_TEST_CLIENT_EXTRA_SEC,
    ACP_HARD_TIMEOUT_SEC,
} from '@/utils/RecordedEpisodeResolution';


vi.mock('@/services/APIClient', () => ({
    default: {
        post: vi.fn(),
        showGenericError: vi.fn(),
    },
}));


describe('AIBackend ACP 接続試験', () => {
    beforeEach(() => {
        vi.mocked(APIClient.post).mockReset();
        vi.mocked(APIClient.showGenericError).mockReset();
    });

    it('ACPはサーバー絶対上限の後も10分間HardTimeout応答を待つ', async () => {
        vi.mocked(APIClient.post).mockResolvedValue({
            type: 'success',
            status: 200,
            headers: {},
            data: {
                success: false,
                latency_ms: 0,
                model: 'acp:codex:test-model',
                message: 'HardTimeout',
                checks: null,
            },
        });

        await AIBackend.testACPConnection('AcpCodex', 'CandidateSelection');

        expect(ACP_CONNECTION_TEST_CLIENT_EXTRA_SEC).toBe(10 * 60);
        expect(APIClient.post).toHaveBeenCalledWith(
            '/ai-backends/acp/test',
            {backend_kind: 'AcpCodex', capability: 'CandidateSelection'},
            {
                timeout: (ACP_HARD_TIMEOUT_SEC + 10 * 60) * 1000,
            },
        );
    });
});
