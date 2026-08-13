import type { IRecordedProgram } from '@/services/Videos';
import type {
    BS4KLiveStreamingQuality,
    KonomiTVBS4KPlaybackAudioCodec,
    KonomiTVBS4KPlaybackVideoCodec,
    VideoStreamingQuality,
} from '@/stores/SettingsStore';

import KonomiTVBS4KOfflineDownloadRuntime from '@/services/KonomiTVBS4KOfflineDownloadRuntime';
import OfflineVideoStorage from '@/services/OfflineVideoStorage';
import Utils from '@/utils';


/** オフライン保存ジョブの状態 */
export type OfflineDownloadState = 'Waiting' | 'Downloading' | 'Finalizing' | 'Failed' | 'Cancelled';

/** 画面へ表示するオフライン保存の現在工程 */
export type OfflineDownloadPhase =
    'Queued' | 'Preparing' | 'Encoding' | 'Packaging' | 'Downloading' | 'Finalizing';

/** オフライン保存済み動画 */
export interface IOfflineVideo {
    video_id: number;
    generation_id: string;
    file_hash: string;
    program: IRecordedProgram;
    quality: string;
    video_codec: KonomiTVBS4KPlaybackVideoCodec;
    video_bit_depth: 8 | 10;
    requested_audio_codec: KonomiTVBS4KPlaybackAudioCodec;
    audio_codec: KonomiTVBS4KPlaybackAudioCodec;
    size_bytes: number;
    asset_count: number;
    asset_paths: string[];
    saved_at: number;
}

/** オフライン保存 API へ渡す exact 生成条件 */
export interface IKonomiTVBS4KOfflineDownloadProfile {
    quality: VideoStreamingQuality | BS4KLiveStreamingQuality;
    video_codec: KonomiTVBS4KPlaybackVideoCodec;
    video_bit_depth: 8 | 10;
    audio_codec: KonomiTVBS4KPlaybackAudioCodec;
    is_24fps_mode: boolean;
}

/** 全音声レンディションを含むオフライン保存容量の概算 */
export interface IKonomiTVBS4KOfflineStreamEstimate {
    estimated_size_bytes: number;
    required_size_bytes: number;
}

/** HTTP 接続から独立したサーバー側オフライン保存生成ジョブ */
export interface IKonomiTVBS4KOfflineJob {
    job_id: string;
    video_id: number;
    state: 'Queued' | 'Generating' | 'Ready' | 'Failed' | 'Cancelled';
    phase: 'Queued' | 'Preparing' | 'Encoding' | 'Packaging' | 'Ready' | 'Failed' | 'Cancelled';
    progress: number;
    completed_assets: number;
    total_assets: number;
    completed_bytes: number;
    package_size_bytes: number | null;
    error: string | null;
    created_at: number;
    updated_at: number;
}

/** オフライン保存ジョブ */
export interface IOfflineDownloadJob {
    job_id: string;
    video_id: number;
    generation_id: string;
    program: IRecordedProgram;
    quality: string;
    video_codec: KonomiTVBS4KPlaybackVideoCodec;
    video_bit_depth: 8 | 10;
    requested_audio_codec: KonomiTVBS4KPlaybackAudioCodec;
    state: OfflineDownloadState;
    phase: OfflineDownloadPhase;
    progress: number;
    estimated_size_bytes: number;
    downloaded_bytes: number;
    total_assets: number;
    package_size_bytes: number | null;
    server_job_id: string | null;
    background_fetch_id: string | null;
    error: string | null;
}

/**
 * 録画番組のオフライン保存ジョブとダウンロード応答の展開を管理する。
 */
export default class OfflineVideos {

    static readonly eventTarget = OfflineVideoStorage.eventTarget;

    /** 保存画質セレクタと同じ利用者向けラベル */
    private static readonly QUALITY_LABELS: Record<string, string> = {
        '4320p': '8K',
        '2160p': '4K',
        '1440p': '1440p',
        '1080p-60fps': '1080p (60fps)',
        '1080p-30fps': '1080p (30fps)',
        '1080p': '1080p',
        '810p-60fps': '810p (60fps)',
        '810p-30fps': '810p (30fps)',
        '810p': '810p',
        '720p-60fps': '720p (60fps)',
        '720p-30fps': '720p (30fps)',
        '720p': '720p',
        '540p-30fps': '540p (30fps)',
        '540p': '540p',
        '480p-30fps': '480p (30fps)',
        '480p': '480p',
        '360p-30fps': '360p (30fps)',
        '360p': '360p',
        '240p-30fps': '240p (30fps)',
        '240p': '240p',
    };

    private static foregroundAbortControllers = new Map<string, AbortController>();
    private static foregroundLockReleases = new Map<string, () => void>();
    private static readonly PERSISTENT_STORAGE_REQUEST_TIMEOUT_SECONDS = 3;
    private static readonly COORDINATOR_LOCK_NAME = 'konomitv-bs4k-offline-job-coordinator';
    private static readonly COORDINATOR_INTERVAL_SECONDS = 1;
    private static isCoordinatorStarted = false;

    /** 前景保存中にタブを閉じようとしたときの警告 */
    private static readonly onBeforeUnload = (event: BeforeUnloadEvent): void => {
        event.preventDefault();
        event.returnValue = '';
    };

    /**
     * Background Fetch を利用できるかを返す。
     * @returns 利用できる場合は true
     */
    static async isBackgroundFetchSupported(): Promise<boolean> {

        // Service Worker の登録がなければ Background Fetch の管理オブジェクトも取得できない
        if ('serviceWorker' in navigator === false) {
            return false;
        }
        const registration = await navigator.serviceWorker.getRegistration();
        return registration?.backgroundFetch !== undefined;
    }

    /**
     * オフライン保存データの永続化を要求する。
     * Firefox などで権限要求が解決されない場合も、既存の注意表示へ進めるため一定時間で打ち切る。
     * @returns 永続ストレージを利用できる場合は true
     */
    static async requestPersistentStorage(): Promise<boolean> {

        if (navigator.storage?.persist === undefined) return true;

        let timeoutID: number | null = null;
        try {
            return await Promise.race([
                navigator.storage.persist(),
                new Promise<boolean>((resolve) => {
                    timeoutID = window.setTimeout(
                        () => resolve(false),
                        this.PERSISTENT_STORAGE_REQUEST_TIMEOUT_SECONDS * 1000,
                    );
                }),
            ]);
        } catch (error) {
            // 永続化要求の失敗だけで保存を禁止せず、ブラウザによる自動削除の可能性を利用者へ確認する
            console.warn('[OfflineVideos] Failed to request persistent storage:', error);
            return false;
        } finally {
            if (timeoutID !== null) window.clearTimeout(timeoutID);
        }
    }

    /**
     * 保存済み動画を取得する。
     * @returns 保存日時が新しい順の動画一覧
     */
    static async getVideos(): Promise<IOfflineVideo[]> {
        return await OfflineVideoStorage.getVideos();
    }

    /**
     * 録画番組 ID に対応する保存済み動画を取得する。
     * @param videoID 録画番組 ID
     * @returns 保存済み動画（存在しない場合は null）
     */
    static async getVideo(videoID: number): Promise<IOfflineVideo | null> {
        return await OfflineVideoStorage.getVideo(videoID);
    }

    /**
     * 実行中または完了した保存ジョブを取得する。
     * @returns 保存ジョブ一覧
     */
    static async getJobs(): Promise<IOfflineDownloadJob[]> {

        return (await OfflineVideoStorage.getJobs()).map(job => this.normalizeJob(job));
    }

    /** 旧クライアントが残したジョブにも、新しい永続ジョブ契約の既定値を補う。 */
    private static normalizeJob(job: IOfflineDownloadJob): IOfflineDownloadJob {
        const fallbackProgress = job.estimated_size_bytes > 0
            ? Math.min(0.98, job.downloaded_bytes / job.estimated_size_bytes)
            : 0;
        return {
            ...job,
            phase: job.phase ?? (job.state === 'Downloading' ? 'Downloading' :
                job.state === 'Finalizing' ? 'Finalizing' : 'Queued'),
            progress: Number.isFinite(job.progress) ? Math.max(0, Math.min(1, job.progress)) : fallbackProgress,
            total_assets: job.total_assets ?? 0,
            package_size_bytes: job.package_size_bytes ?? null,
            server_job_id: job.server_job_id ?? null,
        };
    }

    /**
     * 指定した録画番組の実行中保存ジョブを取得する。
     * @param videoID 録画番組 ID
     * @returns 実行中ジョブ (存在しない場合は null)
     */
    static async getActiveJobForVideo(videoID: number): Promise<IOfflineDownloadJob | null> {

        const jobs = await this.getJobs();
        return jobs.find(job =>
            job.video_id === videoID && ['Waiting', 'Downloading', 'Finalizing'].includes(job.state),
        ) ?? null;
    }

    /**
     * ページ内 Fetch で保存中のジョブがあるかを返す。
     * @returns 前景保存中なら true
     */
    static async hasActiveForegroundDownload(): Promise<boolean> {

        // IndexedDB は別タブとも共有されるため、このページが所有するロックだけを更新延期の判定に使う
        return this.foregroundLockReleases.size > 0;
    }

    /**
     * 前回のページ終了で中断された前景保存ジョブを回収する。
     */
    static async recoverInterruptedForegroundDownloads(): Promise<void> {

        const jobs = await this.getJobs();

        // 旧ストリーミング方式で残った未完ジョブには対応するサーバージョブがないため、明示的に失敗へ移す。
        for (const job of jobs) {
            if (['Waiting', 'Downloading', 'Finalizing'].includes(job.state) && job.server_job_id === null) {
                if (job.background_fetch_id !== null && 'serviceWorker' in navigator) {
                    const registration = await navigator.serviceWorker.getRegistration();
                    const backgroundFetch = await registration?.backgroundFetch?.get(job.background_fetch_id);
                    await backgroundFetch?.abort();
                } else {
                    this.foregroundAbortControllers.get(job.job_id)?.abort();
                }
                await KonomiTVBS4KOfflineDownloadRuntime.markJobFailed(
                    job.job_id,
                    '保存方式が更新されたため、この未完了ジョブは再開できません。もう一度保存してください。',
                );
            }
        }

        if ('locks' in navigator) {
            for (const job of jobs) {
                // サーバー生成中の Waiting はページに依存しない。前景転送を開始済みのジョブだけを中断判定する。
                if (job.background_fetch_id !== null || ['Downloading', 'Finalizing'].includes(job.state) === false) continue;

                // Web Locks を取得できなければ別タブが保存中なので、共有 IndexedDB の状態へ触れない
                await navigator.locks.request(this.getForegroundLockName(job.job_id), {ifAvailable: true}, async (lock) => {
                    if (lock === null) return;
                    const latestJob = await OfflineVideoStorage.getJob(job.job_id);
                    if (latestJob === null || ['Downloading', 'Finalizing'].includes(latestJob.state) === false) return;
                    await KonomiTVBS4KOfflineDownloadRuntime.markJobFailed(latestJob.job_id, 'ページが閉じられたため、オフライン保存が中断されました。');
                });
            }
        } else {
            // Web Locks 非対応環境では所有タブを識別できないため、前景保存の実行中ジョブは起動時に失敗へ倒して断片を回収する
            for (const job of jobs) {
                if (job.background_fetch_id !== null || ['Downloading', 'Finalizing'].includes(job.state) === false) continue;
                await KonomiTVBS4KOfflineDownloadRuntime.markJobFailed(job.job_id, 'ページが閉じられたため、オフライン保存が中断されました。');
            }
        }

        await OfflineVideoStorage.cleanupOrphanedGenerations();
    }

    /**
     * 録画番組のオフライン保存を開始する。
     * @param program 保存する録画番組
     * @param profile 画質・映像 codec・bit depth・音声 codec・24fps の生成条件
     * @param estimate サーバーが全音声レンディション込みで算出した容量見積もり
     * @returns 作成した保存ジョブ
     */
    static async start(
        program: IRecordedProgram,
        profile: IKonomiTVBS4KOfflineDownloadProfile,
        estimate: IKonomiTVBS4KOfflineStreamEstimate,
    ): Promise<IOfflineDownloadJob> {

        // Vue コンポーネントから渡される番組情報はリアクティブ Proxy のため、そのままでは IndexedDB の構造化複製に失敗する
        // API 由来の JSON データだけを保存時点のスナップショットへ変換し、Service Worker からも安全に読み出せる値へ固定する
        const programSnapshot = JSON.parse(JSON.stringify(program)) as IRecordedProgram;

        const apiQuality = this.buildAPIQuality(profile.quality, profile.is_24fps_mode);
        const codecQuery = this.buildCodecQuery(profile);

        // Background Fetch は一時応答と展開後キャッシュが併存するため、前景保存の2倍を空き容量判定へ使う
        const isBackgroundFetchSupported = await this.isBackgroundFetchSupported();
        const storageEstimate = await navigator.storage?.estimate() ?? {};
        const availableBytes = (storageEstimate.quota ?? 0) - (storageEstimate.usage ?? 0);
        const requiredBytes = estimate.required_size_bytes * (isBackgroundFetchSupported === true ? 2 : 1);
        if (storageEstimate.quota !== undefined && availableBytes < requiredBytes) {
            throw new Error(`オフライン保存に必要な空き容量が不足しています。必要: ${Utils.formatBytes(requiredBytes)} / 空き: ${Utils.formatBytes(Math.max(0, availableBytes))}`);
        }

        // 同じ録画番組を保存し直す場合も、旧世代を消さず新しい世代へ書き込む
        const generationID = crypto.randomUUID();
        const jobID = crypto.randomUUID();
        const job: IOfflineDownloadJob = {
            job_id: jobID,
            video_id: programSnapshot.id,
            generation_id: generationID,
            program: programSnapshot,
            quality: apiQuality,
            video_codec: profile.video_codec,
            video_bit_depth: profile.video_bit_depth,
            requested_audio_codec: profile.audio_codec,
            state: 'Waiting',
            phase: 'Queued',
            progress: 0,
            estimated_size_bytes: estimate.estimated_size_bytes,
            downloaded_bytes: 0,
            total_assets: 0,
            package_size_bytes: null,
            server_job_id: null,
            background_fetch_id: isBackgroundFetchSupported === true ? `konomitv-bs4k-offline-${jobID}` : null,
            error: null,
        };
        // 同じ録画の重複開始は IndexedDB の readwrite transaction だけでタブ間も含めて直列化する
        await OfflineVideoStorage.putJobIfVideoIdle(job);

        try {
            // POST は生成受付だけを行うため、長時間エンコード中にページや HTTP 接続が閉じてもサーバージョブは継続する。
            const response = await fetch(
                `${Utils.api_base_url}/streams/video/${programSnapshot.id}/${apiQuality}/offline-jobs?${codecQuery}`,
                {method: 'POST'},
            );
            if (response.ok === false) {
                throw new Error(`オフライン保存生成 API が HTTP ${response.status} を返しました。`);
            }
            const serverJob = await response.json() as IKonomiTVBS4KOfflineJob;
            job.server_job_id = serverJob.job_id;
            if (serverJob.video_id !== job.video_id) {
                throw new Error('サーバーが作成した保存ジョブの録画 ID が一致しません。');
            }
            this.applyServerJobProgress(job, serverJob);
            if (await OfflineVideoStorage.updateActiveJob(job) === false) {
                await this.releaseServerJob(job);
                throw new Error('オフライン保存はキャンセルされました。');
            }

            // 小さな付随データは生成ジョブと並行して保存し、動画エンコード開始を待たせない。
            void this.cacheOptionalAssets(job).catch((error: unknown) => {
                console.warn('[OfflineVideos] Failed to open the optional offline asset cache:', error);
            });
        } catch (error) {
            // 受付失敗はローカルジョブへ理由を残し、生成済みの付随データ断片も共通 Runtime で回収する。
            await this.releaseServerJob(job);
            await KonomiTVBS4KOfflineDownloadRuntime.markJobFailed(job.job_id, error instanceof Error ? error.message : 'オフライン保存を開始できませんでした。');
            throw error;
        }
        return job;
    }

    /** サムネイル・ロゴ・実況を、動画パッケージ生成と独立して同じ保存世代へ格納する。 */
    private static async cacheOptionalAssets(job: IOfflineDownloadJob): Promise<void> {
        const cache = await OfflineVideoStorage.openCache();
        const generationBaseURL = OfflineVideoStorage.getGenerationBaseURL(job.video_id, job.generation_id);
        const assetRequests = [
            {source: `${Utils.api_base_url}/videos/${job.video_id}/thumbnail`, destination: `${generationBaseURL}/assets/thumbnail.webp`},
            {source: `${Utils.api_base_url}/videos/${job.video_id}/thumbnail/tiled`, destination: `${generationBaseURL}/assets/thumbnail-tiled.webp`},
        ];
        if (job.program.channel !== null) {
            assetRequests.push({
                source: `${Utils.api_base_url}/channels/${job.program.channel.id}/logo`,
                destination: `${generationBaseURL}/assets/channel-logo`,
            });
        }
        await Promise.all(assetRequests.map(async assetRequest => {
            try {
                const assetResponse = await fetch(assetRequest.source);
                if (assetResponse.ok === true &&
                    await OfflineVideoStorage.isGenerationReferenced(job.video_id, job.generation_id)) {
                    await cache.put(assetRequest.destination, assetResponse);
                }
            } catch (error) {
                // 付随データの取得失敗は動画保存を止めず、完成済み fMP4 パッケージを優先する。
                console.warn('[OfflineVideos] Failed to cache an optional offline asset:', error);
            }
        }));

        // 取得中に生成失敗・キャンセルが確定した場合は、遅れて保存した画像断片をまとめて回収する。
        if (await OfflineVideoStorage.isGenerationReferenced(job.video_id, job.generation_id) === false) {
            await OfflineVideoStorage.deleteGeneration(job.video_id, job.generation_id);
            return;
        }

        const jikkyoSource = `${Utils.api_base_url}/videos/${job.video_id}/jikkyo`;
        const jikkyoDestination = `${generationBaseURL}/assets/jikkyo.json`;
        try {
            const assetResponse = await fetch(jikkyoSource);
            if (assetResponse.ok !== true) return;
            if (await OfflineVideoStorage.isGenerationReferenced(job.video_id, job.generation_id) === false) {
                return;
            }
            await cache.put(jikkyoDestination, assetResponse);
            if (await OfflineVideoStorage.isGenerationReferenced(job.video_id, job.generation_id) === false) {
                await cache.delete(jikkyoDestination);
            }
        } catch (error) {
            console.warn('[OfflineVideos] Failed to cache an optional offline asset:', error);
        }
    }

    /** Background Fetch 非対応ブラウザで、Ready パッケージの前景取得と展開を開始する。 */
    private static async beginForegroundDownload(job: IOfflineDownloadJob, request: Request): Promise<void> {
        const releaseForegroundLock = await this.acquireForegroundLock(job.job_id);
        job.state = 'Downloading';
        job.phase = 'Downloading';
        job.progress = Math.max(job.progress, 0.8);
        if (await OfflineVideoStorage.updateActiveJob(job) === false) {
            releaseForegroundLock?.();
            return;
        }
        const abortController = new AbortController();
        this.foregroundAbortControllers.set(job.job_id, abortController);
        void (async () => {
            let wakeLock: WakeLockSentinel | null = null;
            let releaseForegroundActivityProtection: (() => void) | null = null;
            try {
                // 前景保存中だけ画面の自動消灯を抑え、Safari などで JavaScript 実行が止まる可能性を下げる
                if ('wakeLock' in navigator) {
                    try {
                        wakeLock = await navigator.wakeLock.request('screen');
                    } catch (error) {
                        console.warn('[OfflineVideos] Failed to acquire a screen wake lock:', error);
                    }
                }

                // 無音音声は Chrome のバックグラウンド保護対象にならないため、ローカル WebRTC 接続でページ凍結を抑える
                // マイク権限や外部サーバーを使わない RTCDataChannel を、動画転送が終わるまで開いたまま保持する
                try {
                    releaseForegroundActivityProtection = await this.acquireForegroundActivityProtection();
                } catch (error) {
                    console.warn('[OfflineVideos] Failed to acquire foreground activity protection:', error);
                }
                const response = await fetch(request, {signal: abortController.signal});
                if (response.ok === false) {
                    throw new Error(`オフライン保存 API が HTTP ${response.status} を返しました。`);
                }
                await KonomiTVBS4KOfflineDownloadRuntime.finalizeResponse(job.job_id, response);
            } catch (error) {
                console.error('[OfflineVideos] Foreground offline download failed:', error);
                const latestJob = await OfflineVideoStorage.getJob(job.job_id);
                if (latestJob !== null && ['Waiting', 'Downloading', 'Finalizing'].includes(latestJob.state)) {
                    // 利用者による中止とページ終了による fetch 中断の双方で、未完の断片を残さない
                    if (abortController.signal.aborted === true) {
                        await KonomiTVBS4KOfflineDownloadRuntime.markJobCancelled(job.job_id);
                    } else {
                        await KonomiTVBS4KOfflineDownloadRuntime.markJobFailed(job.job_id, error instanceof Error ? error.message : 'オフライン保存に失敗しました。');
                    }
                }
            } finally {
                // ページ凍結防止用の WebRTC 接続を先に閉じ、前景保存中だけの資源利用に限定する
                releaseForegroundActivityProtection?.();

                // Wake Lock の解放失敗でも、前景保存の所有情報と Web Lock は必ず回収する
                this.foregroundAbortControllers.delete(job.job_id);
                releaseForegroundLock?.();
                try {
                    await wakeLock?.release();
                } catch (error) {
                    console.warn('[OfflineVideos] Failed to release the screen wake lock:', error);
                }
            }
        })();
    }

    /** 保存開始時に指定する API 画質文字列を組み立てる。 */
    static buildAPIQuality(baseQuality: string, is24fpsMode: boolean): string {
        let apiQuality = baseQuality;
        if (is24fpsMode === true) {
            apiQuality += '-24fps';
        }
        return apiQuality;
    }

    /** exact 生成条件を共通 API query へ変換する。 */
    static buildCodecQuery(profile: IKonomiTVBS4KOfflineDownloadProfile): string {
        return new URLSearchParams({
            video_codec: profile.video_codec,
            video_bit_depth: profile.video_bit_depth.toString(),
            audio_codec: profile.audio_codec,
        }).toString();
    }

    /** 全音声レンディションを含む保存容量をサーバーへ問い合わせる。 */
    static async fetchEstimate(
        videoID: number,
        profile: IKonomiTVBS4KOfflineDownloadProfile,
        signal?: AbortSignal,
    ): Promise<IKonomiTVBS4KOfflineStreamEstimate> {
        const apiQuality = this.buildAPIQuality(profile.quality, profile.is_24fps_mode);
        const response = await fetch(
            `${Utils.api_base_url}/streams/video/${videoID}/${apiQuality}/offline-estimate?${this.buildCodecQuery(profile)}`,
            {signal},
        );
        if (response.ok === false) {
            throw new Error(`容量見積もり API が HTTP ${response.status} を返しました。`);
        }
        return await response.json() as IKonomiTVBS4KOfflineStreamEstimate;
    }

    /** サーバージョブの生成進捗を、全体の0～80%へ単調に割り当てる。 */
    static applyServerJobProgress(job: IOfflineDownloadJob, serverJob: IKonomiTVBS4KOfflineJob): void {
        const phaseMap: Partial<Record<IKonomiTVBS4KOfflineJob['phase'], OfflineDownloadPhase>> = {
            Queued: 'Queued',
            Preparing: 'Preparing',
            Encoding: 'Encoding',
            Packaging: 'Packaging',
            Ready: 'Packaging',
        };
        job.phase = phaseMap[serverJob.phase] ?? job.phase;
        job.progress = Math.max(job.progress, Math.min(0.8, serverJob.progress * 0.8));
        job.total_assets = Math.max(job.total_assets, serverJob.total_assets);
        job.package_size_bytes = serverJob.package_size_bytes ?? job.package_size_bytes;
    }

    /** 複数タブのうち Web Lock を取得した1タブだけで、全保存ジョブの同期を継続する。 */
    static startCoordinator(): void {
        if (this.isCoordinatorStarted === true) return;
        this.isCoordinatorStarted = true;

        const RunCoordinator = async (): Promise<void> => {
            while (this.isCoordinatorStarted === true) {
                try {
                    await this.synchronizeJobsOnce();
                } catch (error) {
                    // 一時的な IndexedDB・Service Worker 障害でも leader を手放さず、次周期で回復を試みる。
                    console.warn('[OfflineVideos] Offline job coordinator iteration failed:', error);
                }
                await Utils.sleep(this.COORDINATOR_INTERVAL_SECONDS);
            }
        };

        if ('locks' in navigator) {
            // 各タブは同じ名前で待機し、所有タブが閉じた場合だけ次のタブが自動的に同期を引き継ぐ。
            void navigator.locks.request(this.COORDINATOR_LOCK_NAME, RunCoordinator).catch((error: unknown) => {
                console.warn('[OfflineVideos] Offline job coordinator lock failed:', error);
            });
        } else {
            // Web Locks 非対応ブラウザでは単一タブ利用を前提に同じ同期処理を実行する。
            void RunCoordinator();
        }
    }

    /** leader タブの1周期分だけ、サーバー生成・Background Fetch 進捗・Ready 遷移を同期する。 */
    static async synchronizeJobsOnce(): Promise<void> {
        const jobs = await this.getJobs();
        for (const job of jobs) {
            if (job.state === 'Waiting') {
                if (job.server_job_id === null) continue;
                const serverJob = await this.fetchServerJob(job);
                if (serverJob === null) continue;
                if (serverJob === 'Missing') {
                    await KonomiTVBS4KOfflineDownloadRuntime.markJobFailed(
                        job.job_id,
                        'サーバー側のオフライン保存ジョブが見つかりません。もう一度保存してください。',
                    );
                    continue;
                }
                if (serverJob.state === 'Failed') {
                    await KonomiTVBS4KOfflineDownloadRuntime.markJobFailed(
                        job.job_id,
                        serverJob.error ?? 'サーバーでオフライン保存パッケージを生成できませんでした。',
                    );
                    continue;
                }
                if (serverJob.state === 'Cancelled') {
                    await KonomiTVBS4KOfflineDownloadRuntime.markJobCancelled(job.job_id);
                    continue;
                }
                this.applyServerJobProgress(job, serverJob);
                if (await OfflineVideoStorage.updateActiveJob(job) === false) {
                    await this.releaseServerJob(job);
                    continue;
                }
                if (serverJob.state === 'Ready') {
                    await this.beginReadyDownload(job);
                }
            } else if (job.state === 'Downloading' && job.background_fetch_id !== null) {
                await this.synchronizeBackgroundFetchProgress(job);
            }
        }
    }

    /** サーバー側ジョブ状態を取得し、通信失敗は再試行、404だけは永続欠損として区別する。 */
    private static async fetchServerJob(job: IOfflineDownloadJob): Promise<IKonomiTVBS4KOfflineJob | 'Missing' | null> {
        if (job.server_job_id === null) return 'Missing';
        try {
            const response = await fetch(
                `${Utils.api_base_url}/streams/video/${job.video_id}/offline-jobs/${job.server_job_id}`,
            );
            if (response.status === 404) return 'Missing';
            if (response.ok === false) {
                console.warn(`[OfflineVideos] Offline job status API returned HTTP ${response.status}.`);
                return null;
            }
            return await response.json() as IKonomiTVBS4KOfflineJob;
        } catch (error) {
            // サーバー生成は接続断中も続くため、ネットワークエラーでローカルジョブを失敗へ確定しない。
            console.warn('[OfflineVideos] Failed to fetch offline job status:', error);
            return null;
        }
    }

    /** Ready パッケージを1回だけ Background Fetch または前景 Fetch へ引き渡す。 */
    private static async beginReadyDownload(job: IOfflineDownloadJob): Promise<void> {
        if (job.server_job_id === null || job.package_size_bytes === null || job.package_size_bytes <= 0) {
            await KonomiTVBS4KOfflineDownloadRuntime.markJobFailed(job.job_id, '完成済みパッケージのサイズを取得できませんでした。');
            return;
        }
        const latestJob = await OfflineVideoStorage.getJob(job.job_id);
        if (latestJob === null || latestJob.state !== 'Waiting') return;
        const currentJob = this.normalizeJob(latestJob);
        currentJob.package_size_bytes = job.package_size_bytes;
        currentJob.total_assets = job.total_assets;
        currentJob.progress = Math.max(currentJob.progress, 0.8);
        const request = new Request(
            `${Utils.api_base_url}/streams/video/${job.video_id}/offline-jobs/${job.server_job_id}/download`,
        );

        if (currentJob.background_fetch_id !== null && 'serviceWorker' in navigator) {
            try {
                const registration = await navigator.serviceWorker.getRegistration();
                const backgroundFetchManager = registration?.backgroundFetch;
                if (backgroundFetchManager !== undefined) {
                    const existingFetch = await backgroundFetchManager.get(currentJob.background_fetch_id);
                    if (existingFetch === undefined) {
                        await backgroundFetchManager.fetch(currentJob.background_fetch_id, [request], {
                            title: `${currentJob.program.title}をオフライン保存`,
                            icons: [{src: '/assets/images/icons/icon-192px.png', sizes: '192x192', type: 'image/png'}],
                            downloadTotal: currentJob.package_size_bytes,
                        });
                    }
                    currentJob.state = 'Downloading';
                    currentJob.phase = 'Downloading';
                    if (await OfflineVideoStorage.updateActiveJob(currentJob) === false) {
                        const backgroundFetch = await backgroundFetchManager.get(currentJob.background_fetch_id);
                        await backgroundFetch?.abort();
                    }
                    return;
                }
            } catch (error) {
                await KonomiTVBS4KOfflineDownloadRuntime.markJobFailed(
                    currentJob.job_id,
                    error instanceof Error ? error.message : 'バックグラウンド保存を登録できませんでした。',
                );
                return;
            }
        }

        // Firefox など Background Fetch 非対応環境では、Ready 後にだけ固定長ファイルを前景取得する。
        currentJob.background_fetch_id = null;
        await this.beginForegroundDownload(currentJob, request);
    }

    /** Background Fetch の正確な固定長受信量を、全体進捗の80～98%へ反映する。 */
    private static async synchronizeBackgroundFetchProgress(job: IOfflineDownloadJob): Promise<void> {
        if ('serviceWorker' in navigator === false || job.background_fetch_id === null) return;
        const registration = await navigator.serviceWorker.getRegistration();
        const backgroundFetch = await registration?.backgroundFetch?.get(job.background_fetch_id);
        if (backgroundFetch === undefined) return;
        job.downloaded_bytes = Math.max(job.downloaded_bytes, backgroundFetch.downloaded);
        if (job.package_size_bytes !== null && job.package_size_bytes > 0) {
            const downloadProgress = Math.min(1, job.downloaded_bytes / job.package_size_bytes);
            job.progress = Math.max(job.progress, 0.8 + downloadProgress * 0.18);
        }
        job.phase = 'Downloading';
        await OfflineVideoStorage.updateActiveJob(job);
    }

    /** サーバー側生成ジョブと完成パッケージを best effort で解放する。 */
    private static async releaseServerJob(job: IOfflineDownloadJob): Promise<void> {
        if (job.server_job_id === null) return;
        try {
            const response = await fetch(
                `${Utils.api_base_url}/streams/video/${job.video_id}/offline-jobs/${job.server_job_id}`,
                {method: 'DELETE'},
            );
            if (response.ok === false && response.status !== 404) {
                console.warn(`[OfflineVideos] Offline job delete API returned HTTP ${response.status}.`);
            }
        } catch (error) {
            // サーバー側にも保持期限があるため、通信断時は端末側 cleanup を優先して次回回収へ委ねる。
            console.warn('[OfflineVideos] Failed to release the server offline job:', error);
        }
    }

    /**
     * オフライン保存容量を利用者向けに整形する。
     * @param bytes バイト数
     * @param approximate 見積もり表示なら true
     * @returns 約 2.2GB や 340MB などの文字列
     */
    static formatOfflineSize(bytes: number, approximate: boolean): string {

        const formatted = OfflineVideos.formatOfflineSizeValue(bytes, approximate);
        return approximate === true ? `約${formatted}` : formatted;
    }

    /**
     * オフライン保存容量の単位付き文字列を返す。
     * 見積もり表示では GB 以上は小数1桁、MB は10MB刻みに丸める。
     * 実測表示では GB 以上は小数2桁、MB は1MB単位の整数、それ未満は通常の自動単位切り替え。
     * @param bytes バイト数
     * @param approximate 見積もり表示なら true
     * @returns 2.2GB や 340MB などの文字列
     */
    private static formatOfflineSizeValue(bytes: number, approximate: boolean): string {

        const oneMB = 1024 * 1024;
        const oneGB = oneMB * 1024;

        // GB 以上は見積もりなら粗く、実測なら小数2桁まで表示する
        if (bytes >= oneGB) {
            return Utils.formatBytes(bytes, approximate === true ? 1 : 2);
        }

        if (bytes >= oneMB) {
            const megabytes = bytes / oneMB;
            // 見積もりだけ 344MB のような細かい値を避け、340MB のように10MB刻みへ丸める
            if (approximate === true) {
                const roundedMegabytes = Math.round(megabytes / 10) * 10;
                return `${roundedMegabytes}MB`;
            }
            // 実測値は1MB単位でそのまま表示する
            return `${Math.round(megabytes)}MB`;
        }

        return Utils.formatBytes(bytes, approximate === true ? 1 : 0);
    }

    /**
     * API 画質文字列から利用者向けの画質ラベルを返す。
     * 24fps の内部接尾辞は一覧表示では省略する。
     * @param quality API 画質
     * @returns 720p や 1080p (60fps) などの表示ラベル
     */
    static formatQualityLabel(quality: string): string {

        // 保存開始時と同じ順序で付与される接尾辞だけを外し、解像度ベースのラベルへ戻す
        const baseQuality = quality.replace(/-24fps/g, '');
        return OfflineVideos.QUALITY_LABELS[baseQuality] ?? baseQuality;
    }

    /**
     * 終端状態の保存ジョブを一覧から消す。
     * @param jobID 保存ジョブ ID
     */
    static async dismissJob(jobID: string): Promise<void> {
        const job = await OfflineVideoStorage.getJob(jobID);
        if (job !== null) {
            await this.releaseServerJob(this.normalizeJob(job));
        }
        await OfflineVideoStorage.deleteJob(jobID);
    }

    /**
     * 保存ジョブをキャンセルする。
     * @param jobID 保存ジョブ ID
     */
    static async cancel(jobID: string): Promise<void> {

        // 完了確定と同じ IndexedDB 上でキャンセルを確定し、勝った処理だけが保存世代を削除する
        const job = await OfflineVideoStorage.transitionActiveJobToTerminalState(jobID, 'Cancelled', null);
        if (job === null) return;

        // Background Fetch の停止はブラウザへ任せ、前景 Fetch は世代削除と状態変更で後続処理を無効化する
        try {
            if (job.background_fetch_id !== null && 'serviceWorker' in navigator) {
                const registration = await navigator.serviceWorker.getRegistration();
                const backgroundFetch = await registration?.backgroundFetch?.get(job.background_fetch_id);
                await backgroundFetch?.abort();
            } else {
                this.foregroundAbortControllers.get(job.job_id)?.abort();
            }
        } finally {
            // ブラウザ側の停止要求が失敗しても、サーバー生成とローカル未完成世代の双方を回収する。
            await this.releaseServerJob(this.normalizeJob(job));
            await OfflineVideoStorage.deleteGeneration(job.video_id, job.generation_id);
            await OfflineVideoStorage.deleteJob(job.job_id);
        }
    }

    /**
     * 保存済み動画を端末から削除する。
     * @param videoID 録画番組 ID
     */
    static async deleteVideo(videoID: number): Promise<void> {

        const video = await this.getVideo(videoID);
        if (video === null) return;
        await OfflineVideoStorage.deleteVideo(video);
    }

    /**
     * 保存済み動画の HLS プレイリスト URLを返す。
     * @param video 保存済み動画
     * @returns Service Worker が配信するプレイリスト URL
     */
    static getPlaylistURL(video: IOfflineVideo): string {
        return OfflineVideoStorage.getPlaylistURL(video);
    }

    /**
     * 保存済み動画に付随する画像・JSON の URLを返す。
     * @param video 保存済み動画
     * @param assetName 付随データ名
     * @returns Service Worker が配信する付随データ URL
     */
    static getAssetURL(video: IOfflineVideo, assetName: 'thumbnail.webp' | 'thumbnail-tiled.webp' | 'channel-logo' | 'jikkyo.json'): string {
        return OfflineVideoStorage.getAssetURL(video, assetName);
    }

    private static getForegroundLockName(jobID: string): string {
        return `konomitv-bs4k-offline-foreground-${jobID}`;
    }

    /**
     * 前景保存中のページ凍結を抑えるローカル WebRTC 接続を開始する。
     * @returns WebRTC 接続を閉じる関数（利用できない環境では null）
     */
    private static async acquireForegroundActivityProtection(): Promise<(() => void) | null> {

        if ('RTCPeerConnection' in window === false) return null;

        const offerPeerConnection = new RTCPeerConnection({iceServers: []});
        const answerPeerConnection = new RTCPeerConnection({iceServers: []});
        const offerDataChannel = offerPeerConnection.createDataChannel('konomitv-bs4k-offline-download');
        const dataChannels = [offerDataChannel];

        /** ICE 候補を SDP に集約し、外部の STUN サーバーなしでローカル接続を確立できるまで待つ */
        const waitForICEGathering = async (peerConnection: RTCPeerConnection): Promise<void> => {
            if (peerConnection.iceGatheringState === 'complete') return;
            await new Promise<void>((resolve, reject) => {
                const timeoutID = window.setTimeout(() => {
                    peerConnection.removeEventListener('icegatheringstatechange', onICEGatheringStateChange);
                    reject(new Error('WebRTC のローカル接続準備がタイムアウトしました。'));
                }, 5_000);
                const onICEGatheringStateChange = (): void => {
                    if (peerConnection.iceGatheringState !== 'complete') return;
                    window.clearTimeout(timeoutID);
                    peerConnection.removeEventListener('icegatheringstatechange', onICEGatheringStateChange);
                    resolve();
                };
                peerConnection.addEventListener('icegatheringstatechange', onICEGatheringStateChange);
            });
        };

        try {
            answerPeerConnection.addEventListener('datachannel', (event) => {
                dataChannels.push(event.channel);
            }, {once: true});

            // ICE 候補を含む Offer / Answer を同一ページ内で交換し、ネットワーク上の相手を必要としない接続にする
            await offerPeerConnection.setLocalDescription(await offerPeerConnection.createOffer());
            await waitForICEGathering(offerPeerConnection);
            const offerDescription = offerPeerConnection.localDescription;
            if (offerDescription === null) {
                throw new Error('WebRTC の Offer を作成できませんでした。');
            }
            await answerPeerConnection.setRemoteDescription(offerDescription);
            await answerPeerConnection.setLocalDescription(await answerPeerConnection.createAnswer());
            await waitForICEGathering(answerPeerConnection);
            const answerDescription = answerPeerConnection.localDescription;
            if (answerDescription === null) {
                throw new Error('WebRTC の Answer を作成できませんでした。');
            }
            await offerPeerConnection.setRemoteDescription(answerDescription);

            // Chrome が保護対象と判定するのは開いた DataChannel なので、接続完了後に動画転送へ進む
            if (offerDataChannel.readyState !== 'open') {
                await new Promise<void>((resolve, reject) => {
                    const timeoutID = window.setTimeout(() => {
                        reject(new Error('WebRTC のローカル接続がタイムアウトしました。'));
                    }, 5_000);
                    offerDataChannel.addEventListener('open', () => {
                        window.clearTimeout(timeoutID);
                        resolve();
                    }, {once: true});
                });
            }

            return () => {
                dataChannels.forEach(dataChannel => dataChannel.close());
                offerPeerConnection.close();
                answerPeerConnection.close();
            };
        } catch (error) {
            dataChannels.forEach(dataChannel => dataChannel.close());
            offerPeerConnection.close();
            answerPeerConnection.close();
            throw error;
        }
    }

    /** 前景保存中だけタブ閉じ警告を有効化する */
    private static updateForegroundDownloadWarning(): void {
        if (this.foregroundLockReleases.size > 0) {
            window.addEventListener('beforeunload', OfflineVideos.onBeforeUnload);
        } else {
            window.removeEventListener('beforeunload', OfflineVideos.onBeforeUnload);
        }
    }

    private static async acquireForegroundLock(jobID: string): Promise<(() => void) | null> {
        if ('locks' in navigator === false) return null;

        let resolveLockAcquired: (() => void) | null = null;
        let rejectLockAcquired: ((reason: unknown) => void) | null = null;
        let resolveLockRelease: (() => void) | null = null;
        let isLockAcquired = false;
        const lockAcquired = new Promise<void>((resolve, reject) => {
            resolveLockAcquired = resolve;
            rejectLockAcquired = reject;
        });
        const lockRelease = new Promise<void>((resolve) => {
            resolveLockRelease = resolve;
        });

        // リクエストを完了させず保持し、別タブが同じジョブを中断済みと誤認するのを防ぐ
        void navigator.locks.request(this.getForegroundLockName(jobID), async () => {
            this.foregroundLockReleases.set(jobID, () => resolveLockRelease?.());
            isLockAcquired = true;
            resolveLockAcquired?.();
            await lockRelease;
            this.foregroundLockReleases.delete(jobID);
            this.updateForegroundDownloadWarning();
        }).catch((error: unknown) => {
            // ロック取得前の API 失敗を呼び出し元へ返し、開始処理を永久待機させない
            if (isLockAcquired === false) rejectLockAcquired?.(error);
        });
        await lockAcquired;
        this.updateForegroundDownloadWarning();
        return () => resolveLockRelease?.();
    }

}

// Service Worker からの保存状態更新をページ側 EventTarget へ橋渡しする
if (typeof window !== 'undefined' && 'serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', (event: MessageEvent) => {
        if (event.data?.type === 'konomitv-bs4k-offline-videos-change') {
            OfflineVideos.eventTarget.dispatchEvent(new Event('change'));
        }
    });
}
