import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { IRecordedProgram } from '@/services/Videos';

import KonomiTVBS4KOfflineDownloadRuntime from '@/services/KonomiTVBS4KOfflineDownloadRuntime';
import OfflineVideos, {
    type IKonomiTVBS4KOfflineDownloadProfile,
    type IKonomiTVBS4KOfflineJob,
    type IOfflineDownloadJob,
} from '@/services/OfflineVideos';
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
        getJobs: vi.fn(),
        getJob: vi.fn(),
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
    const serverJob: IKonomiTVBS4KOfflineJob = {
        job_id: 'a'.repeat(32),
        video_id: 1,
        state: 'Queued',
        phase: 'Queued',
        progress: 0,
        completed_assets: 0,
        total_assets: 0,
        completed_bytes: 0,
        package_size_bytes: null,
        error: null,
        created_at: 1,
        updated_at: 1,
    };

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
            if (url.includes('/offline-jobs?')) {
                return Promise.resolve(new Response(JSON.stringify(serverJob), {status: 202}));
            }
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
            .mockResolvedValueOnce(true)
            .mockResolvedValueOnce(true)
            .mockResolvedValueOnce(true)
            .mockResolvedValueOnce(false);
        vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
            const url = input instanceof Request ? input.url : input.toString();
            if (url.includes('/offline-jobs?')) {
                return Promise.resolve(new Response(JSON.stringify(serverJob), {status: 202}));
            }
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


describe('OfflineVideos のサーバー進捗反映', () => {
    it('生成進捗を全体の0～80%へ割り当て、状態を Downloading にしない', () => {
        const job = {
            job_id: 'job-1',
            video_id: 42,
            state: 'Waiting',
            phase: 'Queued',
            progress: 0,
            estimated_size_bytes: 1000,
            downloaded_bytes: 0,
            total_assets: 0,
            package_size_bytes: null,
        } as IOfflineDownloadJob;

        OfflineVideos.applyServerJobProgress(job, {
            job_id: 'b'.repeat(32),
            video_id: 42,
            state: 'Generating',
            phase: 'Encoding',
            progress: 0.4,
            completed_assets: 4,
            total_assets: 10,
            completed_bytes: 250,
            package_size_bytes: null,
            error: null,
            created_at: 1,
            updated_at: 2,
        });

        expect(job.progress).toBeCloseTo(0.32);
        expect(job.phase).toBe('Encoding');
        expect(job.total_assets).toBe(10);
        expect(job.state).toBe('Waiting');
    });

    it('Ready になるまで転送せず、完成後は正確なサイズで Background Fetch を開始する', async () => {
        const job = {
            job_id: 'local-job',
            video_id: 42,
            generation_id: 'generation-1',
            program: {id: 42, title: '長時間番組'} as IRecordedProgram,
            quality: '240p',
            video_codec: 'av1',
            video_bit_depth: 10,
            requested_audio_codec: 'opus',
            state: 'Waiting',
            phase: 'Encoding',
            progress: 0.6,
            estimated_size_bytes: 1_800,
            downloaded_bytes: 0,
            total_assets: 10,
            package_size_bytes: null,
            server_job_id: 'b'.repeat(32),
            background_fetch_id: 'konomitv-bs4k-offline-local-job',
            error: null,
        } as IOfflineDownloadJob;
        const readyServerJob: IKonomiTVBS4KOfflineJob = {
            job_id: 'b'.repeat(32),
            video_id: 42,
            state: 'Ready',
            phase: 'Ready',
            progress: 1,
            completed_assets: 10,
            total_assets: 10,
            completed_bytes: 1_500,
            package_size_bytes: 2_000,
            error: null,
            created_at: 1,
            updated_at: 2,
        };
        const backgroundFetchManager = {
            get: vi.fn().mockResolvedValue(undefined),
            fetch: vi.fn().mockResolvedValue({}),
        };
        Object.defineProperty(navigator, 'serviceWorker', {
            configurable: true,
            value: {
                getRegistration: vi.fn().mockResolvedValue({backgroundFetch: backgroundFetchManager}),
            },
        });
        vi.mocked(OfflineVideoStorage.getJobs).mockResolvedValue([job]);
        vi.mocked(OfflineVideoStorage.getJob).mockResolvedValue(job);
        vi.mocked(OfflineVideoStorage.updateActiveJob).mockResolvedValue(true);
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(readyServerJob), {status: 200})));

        await OfflineVideos.synchronizeJobsOnce();

        expect(backgroundFetchManager.fetch).toHaveBeenCalledTimes(1);
        const [backgroundFetchID, requests, options] = backgroundFetchManager.fetch.mock.calls[0];
        expect(backgroundFetchID).toBe(job.background_fetch_id);
        expect((requests[0] as Request).url).toContain(`/offline-jobs/${job.server_job_id}/download`);
        expect(options.downloadTotal).toBe(2_000);
        const persistedJob = vi.mocked(OfflineVideoStorage.updateActiveJob).mock.calls.at(-1)?.[0];
        expect(persistedJob?.state).toBe('Downloading');
        expect(persistedJob?.progress).toBe(0.8);
    });
});


describe('OfflineVideos の永続ストレージ要求', () => {
    afterEach(() => {
        vi.useRealTimers();
    });

    it('ブラウザの権限要求が未解決でも一定時間後に警告へフォールバックする', async () => {
        vi.useFakeTimers();
        Object.defineProperty(navigator, 'storage', {
            configurable: true,
            value: {
                persist: vi.fn(() => new Promise<boolean>(() => undefined)),
            },
        });

        const result = OfflineVideos.requestPersistentStorage();
        await vi.advanceTimersByTimeAsync(3_000);

        await expect(result).resolves.toBe(false);
    });
});
