import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { IRecordedProgram } from '@/services/Videos';

import KonomiTVBS4KOfflineDownloadRuntime from '@/services/KonomiTVBS4KOfflineDownloadRuntime';
import OfflineVideos, { type IKonomiTVBS4KOfflineDownloadProfile } from '@/services/OfflineVideos';
import OfflineVideoStorage from '@/services/OfflineVideoStorage';


vi.mock('@/services/KonomiTVBS4KOfflineDownloadRuntime', () => ({
    default: {
        finalizeResponse: vi.fn(),
        markJobFailed: vi.fn(),
    },
}));

vi.mock('@/services/OfflineVideoStorage', () => ({
    default: {
        eventTarget: new EventTarget(),
        putJobIfVideoIdle: vi.fn(),
        openCache: vi.fn(),
        getGenerationBaseURL: vi.fn(),
        updateActiveJob: vi.fn(),
        isGenerationReferenced: vi.fn(),
    },
}));


describe('OfflineVideos の実況保存', () => {
    const program = {
        id: 1,
        title: 'テスト番組',
        channel: null,
        recorded_video: {file_hash: 'test-file-hash'},
    } as IRecordedProgram;
    const profile: IKonomiTVBS4KOfflineDownloadProfile = {
        quality: '720p',
        video_codec: 'avc',
        video_bit_depth: 8,
        audio_codec: 'aac',
        is_24fps_mode: false,
    };
    const cache = {
        put: vi.fn(),
        delete: vi.fn(),
    } as unknown as Cache;

    beforeEach(() => {
        // 前景ジョブの所有確認用 Web Lock は開始排他の簡素化後も維持する
        Object.defineProperty(navigator, 'locks', {
            configurable: true,
            value: {
                request: vi.fn(async (_name: string, callback: () => Promise<unknown>) => await callback()),
            },
        });
        vi.mocked(OfflineVideoStorage.putJobIfVideoIdle).mockResolvedValue();
        vi.mocked(OfflineVideoStorage.openCache).mockResolvedValue(cache);
        vi.mocked(OfflineVideoStorage.getGenerationBaseURL).mockImplementation((videoID, generationID) =>
            `https://example.test/local/offline-videos/${videoID}/${generationID}`,
        );
        vi.mocked(OfflineVideoStorage.updateActiveJob).mockResolvedValue(true);
        vi.mocked(OfflineVideoStorage.isGenerationReferenced).mockResolvedValue(true);
        vi.mocked(KonomiTVBS4KOfflineDownloadRuntime.finalizeResponse).mockResolvedValue();
        vi.mocked(cache.put).mockReset();
        vi.mocked(cache.delete).mockReset();
    });

    it('動画完了でジョブが削除された後に到着した実況も完成済み世代へ保存する', async () => {
        let resolveJikkyo!: (response: Response) => void;
        const jikkyoResponse = new Promise<Response>((resolve) => {
            resolveJikkyo = resolve;
        });
        vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
            const url = input instanceof Request ? input.url : input.toString();
            if (url.endsWith('/jikkyo')) return jikkyoResponse;
            return Promise.resolve(new Response('{}', {status: 200}));
        }));

        const job = await OfflineVideos.start(program, profile, {
            estimated_size_bytes: 1_000,
            required_size_bytes: 1_100,
        });
        const response = new Response('{"comments": []}', {
            status: 200,
            headers: {'Content-Type': 'application/json'},
        });
        resolveJikkyo(response);

        const destination = `https://example.test/local/offline-videos/${program.id}/${job.generation_id}/assets/jikkyo.json`;
        await vi.waitFor(() => {
            expect(OfflineVideoStorage.isGenerationReferenced).toHaveBeenCalledWith(program.id, job.generation_id);
            expect(cache.put).toHaveBeenCalledWith(destination, response);
        });
    });

    it('実況の書き込み中に保存世代が破棄された場合は実況を削除する', async () => {
        vi.mocked(OfflineVideoStorage.isGenerationReferenced)
            .mockResolvedValueOnce(true)
            .mockResolvedValueOnce(false);
        vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
            const url = input instanceof Request ? input.url : input.toString();
            return Promise.resolve(new Response(url.endsWith('/jikkyo') ? '{"comments": []}' : '{}', {status: 200}));
        }));

        const job = await OfflineVideos.start(program, profile, {
            estimated_size_bytes: 1_000,
            required_size_bytes: 1_100,
        });
        const destination = `https://example.test/local/offline-videos/${program.id}/${job.generation_id}/assets/jikkyo.json`;

        await vi.waitFor(() => {
            expect(cache.put).toHaveBeenCalledWith(destination, expect.any(Response));
            expect(cache.delete).toHaveBeenCalledWith(destination);
        });
    });
});
