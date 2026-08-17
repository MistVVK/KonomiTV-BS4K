import { afterEach, describe, expect, it, vi } from 'vitest';

import {
    BuildKonomiTVBS4KSpeedTestStartMessage,
    BuildKonomiTVBS4KSpeedTestWorkerOptions,
    KONOMITV_BS4K_SPEED_TEST_STATUS_INTERVAL_MS,
    KONOMITV_BS4K_SPEED_TEST_WORKER_URL,
    KonomiTVBS4KSpeedTestWorkerController,
    MapKonomiTVBS4KSpeedTestState,
    ParseKonomiTVBS4KSpeedTestWorkerStatus,
    ResolveKonomiTVBS4KSpeedTestResult,
    SelectKonomiTVBS4KSpeedTestRecommendedQuality,
    type IKonomiTVBS4KSpeedTestQualityThreshold,
} from '@/utils/KonomiTVBS4KSpeedTestWorker';


class FakeWorker {
    readonly posted: string[] = [];
    terminated = false;
    onmessage: ((event: MessageEvent<string>) => void) | null = null;
    onerror: (() => void) | null = null;

    postMessage(message: string): void {
        this.posted.push(message);
    }

    terminate(): void {
        this.terminated = true;
    }
}

const sampleThresholds: IKonomiTVBS4KSpeedTestQualityThreshold[] = [
    {broadcast_type: 'Terrestrial', codec: 'AV1', quality: '1080p-60fps', required_mbps: 20, basis: 'FixedMuxrate'},
    {broadcast_type: 'Terrestrial', codec: 'AV1', quality: '1080p', required_mbps: 15, basis: 'FixedMuxrate'},
    {broadcast_type: 'Terrestrial', codec: 'AV1', quality: '240p', required_mbps: 2, basis: 'FixedMuxrate'},
    {broadcast_type: 'BS4K', codec: 'HEVC', quality: '2160p', required_mbps: 30, basis: 'VariableBitrate'},
];


describe('KonomiTVBS4KSpeedTestWorker', () => {
    afterEach(() => {
        vi.useRealTimers();
    });

    it('Desktop / Mobile 共通の start JSON と絶対 API URL を送る', () => {
        const message = BuildKonomiTVBS4KSpeedTestStartMessage('https://example.test/api');
        expect(message.startsWith('start ')).toBe(true);
        const options = JSON.parse(message.slice('start '.length)) as ReturnType<typeof BuildKonomiTVBS4KSpeedTestWorkerOptions>;
        expect(options.test_order).toBe('P_D_U');
        expect(options.xhr_dlMultistream).toBe(5);
        expect(options.xhr_ulMultistream).toBe(3);
        expect(options.garbagePhp_chunkSize).toBe(100);
        expect(options.xhr_ul_blob_megabytes).toBe(20);
        expect(options.time_dl_max).toBe(10);
        expect(options.time_ul_max).toBe(10);
        expect(options.time_auto).toBe(true);
        expect(options.count_ping).toBe(20);
        expect(options.overheadCompensationFactor).toBe(1.0);
        expect(options.useMebibits).toBe(false);
        expect(options.telemetry_level).toBe(0);
        expect(options.getIp_ispInfo).toBe(false);
        expect(options.url_dl).toBe('https://example.test/api/konomitv-bs4k/speed-test/garbage');
        expect(options.url_ul).toBe('https://example.test/api/konomitv-bs4k/speed-test/empty');
        expect(options.url_ping).toBe('https://example.test/api/konomitv-bs4k/speed-test/empty');
        expect(options.url_dl.startsWith('https://example.test/api/')).toBe(true);
    });

    it('status の各 testState を画面状態へ写す', () => {
        expect(MapKonomiTVBS4KSpeedTestState(-1)).toBe('Idle');
        expect(MapKonomiTVBS4KSpeedTestState(0)).toBe('Preparing');
        expect(MapKonomiTVBS4KSpeedTestState(1)).toBe('Downloading');
        expect(MapKonomiTVBS4KSpeedTestState(2)).toBe('Pinging');
        expect(MapKonomiTVBS4KSpeedTestState(3)).toBe('Uploading');
        expect(MapKonomiTVBS4KSpeedTestState(4)).toBe('Completed');
        expect(MapKonomiTVBS4KSpeedTestState(5)).toBe('Cancelled');
    });

    it('Fail・不正JSON・NaN を成功結果として扱わない', () => {
        expect(ParseKonomiTVBS4KSpeedTestWorkerStatus('{')).toBeNull();
        const failed = ParseKonomiTVBS4KSpeedTestWorkerStatus(JSON.stringify({
            testState: 4,
            dlStatus: 'Fail',
            ulStatus: '10.00',
            pingStatus: '12.00',
            jitterStatus: '1.00',
            dlProgress: 1,
            ulProgress: 1,
            pingProgress: 1,
        }));
        expect(failed).not.toBeNull();
        expect(ResolveKonomiTVBS4KSpeedTestResult(failed!)).toBeNull();
        const nanStatus = ParseKonomiTVBS4KSpeedTestWorkerStatus(JSON.stringify({
            testState: Number.NaN,
            dlStatus: '10.00',
            ulStatus: '10.00',
            pingStatus: '12.00',
            jitterStatus: '1.00',
            dlProgress: 1,
            ulProgress: 1,
            pingProgress: 1,
        }));
        expect(nanStatus).toBeNull();
    });

    it('200ms で status を送り、再測定時は Worker を再生成する', () => {
        vi.useFakeTimers();
        const workers: FakeWorker[] = [];
        const controller = new KonomiTVBS4KSpeedTestWorkerController((url) => {
            expect(url).toBe(KONOMITV_BS4K_SPEED_TEST_WORKER_URL);
            const worker = new FakeWorker();
            workers.push(worker);
            return worker as unknown as Worker;
        }, 'https://example.test/api');

        expect(controller.start(() => undefined, () => undefined)).toBe(true);
        expect(controller.start(() => undefined, () => undefined)).toBe(false);
        expect(workers[0].posted[0]?.startsWith('start ')).toBe(true);
        vi.advanceTimersByTime(KONOMITV_BS4K_SPEED_TEST_STATUS_INTERVAL_MS);
        expect(workers[0].posted).toContain('status');

        controller.cleanup();
        expect(workers[0].posted).toContain('abort');
        expect(workers[0].terminated).toBe(true);

        expect(controller.start(() => undefined, () => undefined)).toBe(true);
        expect(workers).toHaveLength(2);
        controller.cleanup();
    });

    it('start 前の abort では Worker を起動しない', () => {
        const worker = new FakeWorker();
        const controller = new KonomiTVBS4KSpeedTestWorkerController(
            () => worker as unknown as Worker,
            'https://example.test/api',
        );
        controller.abort();
        expect(controller.start(() => undefined, () => undefined)).toBe(false);
        expect(worker.posted).toEqual([]);
        controller.cleanup();
        expect(controller.start(() => undefined, () => undefined)).toBe(true);
        controller.cleanup();
    });

    it('Worker の同期生成エラーでは実行中状態と測定枠を残さない', () => {
        const createWorker = vi.fn(() => {
            throw new DOMException('Blocked by Content Security Policy', 'SecurityError');
        });
        const controller = new KonomiTVBS4KSpeedTestWorkerController(
            createWorker,
            'https://example.test/api',
        );
        expect(controller.start(() => undefined, () => undefined)).toBe(false);
        expect(controller.isRunning()).toBe(false);
        expect(controller.start(() => undefined, () => undefined)).toBe(false);
        expect(createWorker).toHaveBeenCalledTimes(2);
        controller.cleanup();
    });

    it('完了・失敗・キャンセルで cleanup する', () => {
        const worker = new FakeWorker();
        const controller = new KonomiTVBS4KSpeedTestWorkerController(
            () => worker as unknown as Worker,
            'https://example.test/api',
        );
        controller.start(() => undefined, () => undefined);
        controller.abort();
        expect(worker.posted).toContain('abort');
        controller.cleanup();
        expect(worker.terminated).toBe(true);
        expect(controller.isRunning()).toBe(false);
    });

    it('通常放送 / BS4K × codec 別に推奨画質を選ぶ', () => {
        expect(SelectKonomiTVBS4KSpeedTestRecommendedQuality(sampleThresholds, 'Terrestrial', 'AV1', 16)?.quality)
            .toBe('1080p');
        expect(SelectKonomiTVBS4KSpeedTestRecommendedQuality(sampleThresholds, 'Terrestrial', 'AV1', 1))
            .toBeNull();
        expect(SelectKonomiTVBS4KSpeedTestRecommendedQuality(sampleThresholds, 'BS4K', 'HEVC', 40)?.quality)
            .toBe('2160p');
    });
});
