import Utils from '@/utils';


export const KONOMITV_BS4K_SPEED_TEST_WORKER_COMMIT = '892674a084a3dd354d823545cd0191023325b89c';
export const KONOMITV_BS4K_SPEED_TEST_WORKER_CACHE_QUERY = '892674a084a3';
export const KONOMITV_BS4K_SPEED_TEST_WORKER_URL =
    `/vendor/librespeed/speedtest_worker.js?v=${KONOMITV_BS4K_SPEED_TEST_WORKER_CACHE_QUERY}`;
export const KONOMITV_BS4K_SPEED_TEST_STATUS_INTERVAL_MS = 200;

export type KonomiTVBS4KSpeedTestUiState =
    'Idle' | 'Preparing' | 'Pinging' | 'Downloading' | 'Uploading' | 'Completed' | 'Cancelled' | 'Error';

export type KonomiTVBS4KSpeedTestBroadcastType = 'Terrestrial' | 'BS4K';
export type KonomiTVBS4KSpeedTestCodec = 'AVC' | 'HEVC' | 'VP9' | 'AV1';
export type KonomiTVBS4KSpeedTestBasis = 'VariableBitrate' | 'FixedMuxrate';

export interface IKonomiTVBS4KSpeedTestQualityThreshold {
    broadcast_type: KonomiTVBS4KSpeedTestBroadcastType;
    codec: KonomiTVBS4KSpeedTestCodec;
    quality: string;
    required_mbps: number;
    basis: KonomiTVBS4KSpeedTestBasis;
}

export interface IKonomiTVBS4KSpeedTestWorkerStatus {
    testState: number;
    dlStatus: string;
    ulStatus: string;
    pingStatus: string;
    jitterStatus: string;
    dlProgress: number;
    ulProgress: number;
    pingProgress: number;
}

export interface IKonomiTVBS4KSpeedTestResult {
    download_mbps: number;
    upload_mbps: number;
    rtt_ms: number;
    jitter_ms: number;
}

export interface IKonomiTVBS4KSpeedTestWorkerOptions {
    test_order: 'P_D_U';
    time_auto: true;
    time_dl_max: 10;
    time_ul_max: 10;
    time_dlGraceTime: 1.5;
    time_ulGraceTime: 3;
    count_ping: 20;
    ping_allowPerformanceApi: true;
    xhr_dlMultistream: 5;
    xhr_ulMultistream: 3;
    xhr_multistreamDelay: 300;
    xhr_ul_blob_megabytes: 20;
    garbagePhp_chunkSize: 100;
    xhr_dlUseBlob: false;
    xhr_ignoreErrors: 0;
    enable_quirks: true;
    useMebibits: false;
    overheadCompensationFactor: 1.0;
    telemetry_level: 0;
    getIp_ispInfo: false;
    url_dl: string;
    url_ul: string;
    url_ping: string;
}

export type KonomiTVBS4KSpeedTestWorkerFactory = (url: string) => Worker;


export function BuildKonomiTVBS4KSpeedTestWorkerOptions(
    api_base_url: string = Utils.api_base_url,
): IKonomiTVBS4KSpeedTestWorkerOptions {
    // Desktop / Mobile で負荷条件を分けず、8K 視聴を同じ基準で測る。
    return {
        test_order: 'P_D_U',
        time_auto: true,
        time_dl_max: 10,
        time_ul_max: 10,
        time_dlGraceTime: 1.5,
        time_ulGraceTime: 3,
        count_ping: 20,
        ping_allowPerformanceApi: true,
        xhr_dlMultistream: 5,
        xhr_ulMultistream: 3,
        xhr_multistreamDelay: 300,
        xhr_ul_blob_megabytes: 20,
        garbagePhp_chunkSize: 100,
        xhr_dlUseBlob: false,
        xhr_ignoreErrors: 0,
        enable_quirks: true,
        useMebibits: false,
        overheadCompensationFactor: 1.0,
        telemetry_level: 0,
        getIp_ispInfo: false,
        url_dl: `${api_base_url}/konomitv-bs4k/speed-test/garbage`,
        url_ul: `${api_base_url}/konomitv-bs4k/speed-test/empty`,
        url_ping: `${api_base_url}/konomitv-bs4k/speed-test/empty`,
    };
}

export function BuildKonomiTVBS4KSpeedTestStartMessage(
    api_base_url: string = Utils.api_base_url,
): string {
    return `start ${JSON.stringify(BuildKonomiTVBS4KSpeedTestWorkerOptions(api_base_url))}`;
}

export function MapKonomiTVBS4KSpeedTestState(test_state: number): KonomiTVBS4KSpeedTestUiState | null {
    switch (test_state) {
        case -1:
            return 'Idle';
        case 0:
            return 'Preparing';
        case 1:
            return 'Downloading';
        case 2:
            return 'Pinging';
        case 3:
            return 'Uploading';
        case 4:
            return 'Completed';
        case 5:
            return 'Cancelled';
        default:
            return null;
    }
}

function IsFiniteMetric(value: unknown): value is number {
    return typeof value === 'number' && Number.isFinite(value);
}

function ParseMetricString(value: unknown): number | null {
    if (typeof value !== 'string' || value === '' || value === 'Fail') {
        return null;
    }
    const parsed = Number(value);
    if (Number.isFinite(parsed) === false) {
        return null;
    }
    return parsed;
}

export function ParseKonomiTVBS4KSpeedTestWorkerStatus(
    raw: string,
): IKonomiTVBS4KSpeedTestWorkerStatus | null {
    let parsed: unknown;
    try {
        parsed = JSON.parse(raw);
    } catch {
        return null;
    }
    if (parsed === null || typeof parsed !== 'object') {
        return null;
    }
    const record = parsed as Record<string, unknown>;
    if (IsFiniteMetric(record.testState) === false) {
        return null;
    }
    if (typeof record.dlStatus !== 'string' || typeof record.ulStatus !== 'string') {
        return null;
    }
    if (typeof record.pingStatus !== 'string' || typeof record.jitterStatus !== 'string') {
        return null;
    }
    if (IsFiniteMetric(record.dlProgress) === false || IsFiniteMetric(record.ulProgress) === false) {
        return null;
    }
    if (IsFiniteMetric(record.pingProgress) === false) {
        return null;
    }
    return {
        testState: record.testState,
        dlStatus: record.dlStatus,
        ulStatus: record.ulStatus,
        pingStatus: record.pingStatus,
        jitterStatus: record.jitterStatus,
        dlProgress: record.dlProgress,
        ulProgress: record.ulProgress,
        pingProgress: record.pingProgress,
    };
}

export function ResolveKonomiTVBS4KSpeedTestResult(
    status: IKonomiTVBS4KSpeedTestWorkerStatus,
): IKonomiTVBS4KSpeedTestResult | null {
    if (status.testState !== 4) {
        return null;
    }
    const download_mbps = ParseMetricString(status.dlStatus);
    const upload_mbps = ParseMetricString(status.ulStatus);
    const rtt_ms = ParseMetricString(status.pingStatus);
    const jitter_ms = ParseMetricString(status.jitterStatus);
    if (download_mbps === null || upload_mbps === null || rtt_ms === null || jitter_ms === null) {
        return null;
    }
    return {download_mbps, upload_mbps, rtt_ms, jitter_ms};
}

export function SelectKonomiTVBS4KSpeedTestRecommendedQuality(
    thresholds: readonly IKonomiTVBS4KSpeedTestQualityThreshold[],
    broadcast_type: KonomiTVBS4KSpeedTestBroadcastType,
    codec: KonomiTVBS4KSpeedTestCodec,
    download_mbps: number,
): IKonomiTVBS4KSpeedTestQualityThreshold | null {
    const matched = thresholds.filter(threshold =>
        threshold.broadcast_type === broadcast_type && threshold.codec === codec,
    );
    for (const threshold of matched) {
        if (download_mbps >= threshold.required_mbps) {
            return threshold;
        }
    }
    return null;
}


export class KonomiTVBS4KSpeedTestWorkerController {
    private worker: Worker | null = null;
    private status_timer: number | null = null;
    private running = false;
    private aborting = false;

    constructor(
        private readonly create_worker: KonomiTVBS4KSpeedTestWorkerFactory =
        (url: string) => new Worker(url),
        private readonly api_base_url: string = Utils.api_base_url,
    ) {}

    isRunning(): boolean {
        return this.running;
    }

    start(on_status: (status: IKonomiTVBS4KSpeedTestWorkerStatus) => void, on_error: (reason: string) => void): boolean {
        // Worker は1インスタンス1回しか start できないため、測定ごとに作り直す。
        // 準備中に abort された場合は、その後の start で大量通信を始めない。
        if (this.running === true || this.aborting === true) {
            return false;
        }
        this.running = true;
        try {
            const worker = this.create_worker(KONOMITV_BS4K_SPEED_TEST_WORKER_URL);
            this.worker = worker;
            worker.onmessage = (event: MessageEvent<string>) => {
                const status = ParseKonomiTVBS4KSpeedTestWorkerStatus(event.data);
                if (status === null) {
                    on_error('Worker の応答を解釈できませんでした。');
                    return;
                }
                on_status(status);
            };
            worker.onerror = () => {
                on_error('速度測定 Worker でエラーが発生しました。');
            };
            worker.postMessage(BuildKonomiTVBS4KSpeedTestStartMessage(this.api_base_url));
            this.status_timer = window.setInterval(() => {
                this.worker?.postMessage('status');
            }, KONOMITV_BS4K_SPEED_TEST_STATUS_INTERVAL_MS);
            return true;
        } catch {
            // CSP や Worker 制限による同期例外でも、次の測定を開始できる状態へ戻す。
            this.worker?.terminate();
            this.worker = null;
            this.running = false;
            this.aborting = false;
            return false;
        }
    }

    abort(): void {
        this.aborting = true;
        this.worker?.postMessage('abort');
    }

    wasAborted(): boolean {
        return this.aborting;
    }

    cleanup(): void {
        if (this.status_timer !== null) {
            window.clearInterval(this.status_timer);
            this.status_timer = null;
        }
        if (this.worker !== null) {
            try {
                this.worker.postMessage('abort');
            } catch {
                // 既に利用不能な Worker でも terminate() は必ず実行する。
            }
            this.worker.terminate();
            this.worker = null;
        }
        this.running = false;
        this.aborting = false;
    }
}
