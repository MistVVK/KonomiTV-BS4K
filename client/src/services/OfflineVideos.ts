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
    estimated_size_bytes: number;
    downloaded_bytes: number;
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

        return await OfflineVideoStorage.getJobs();
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
        if ('locks' in navigator) {
            for (const job of jobs) {
                if (job.background_fetch_id !== null || ['Waiting', 'Downloading', 'Finalizing'].includes(job.state) === false) continue;

                // Web Locks を取得できなければ別タブが保存中なので、共有 IndexedDB の状態へ触れない
                await navigator.locks.request(this.getForegroundLockName(job.job_id), {ifAvailable: true}, async (lock) => {
                    if (lock === null) return;
                    const latestJob = await OfflineVideoStorage.getJob(job.job_id);
                    if (latestJob === null || ['Waiting', 'Downloading', 'Finalizing'].includes(latestJob.state) === false) return;
                    await KonomiTVBS4KOfflineDownloadRuntime.markJobFailed(latestJob.job_id, 'ページが閉じられたため、オフライン保存が中断されました。');
                });
            }
        } else {
            // Web Locks 非対応環境では所有タブを識別できないため、前景保存の実行中ジョブは起動時に失敗へ倒して断片を回収する
            for (const job of jobs) {
                if (job.background_fetch_id !== null || ['Waiting', 'Downloading', 'Finalizing'].includes(job.state) === false) continue;
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
            estimated_size_bytes: estimate.estimated_size_bytes,
            downloaded_bytes: 0,
            background_fetch_id: isBackgroundFetchSupported === true ? `konomitv-bs4k-offline-${jobID}` : null,
            error: null,
        };
        const releaseForegroundLock = isBackgroundFetchSupported === false ? await this.acquireForegroundLock(job.job_id) : null;
        try {
            // 同じ録画の重複開始は IndexedDB の readwrite transaction だけでタブ間も含めて直列化する
            await OfflineVideoStorage.putJobIfVideoIdle(job);
        } catch (error) {
            releaseForegroundLock?.();
            throw error;
        }

        let request: Request;
        try {
            // オフライン保存 API は認証不要なので、ページ終了後もブラウザが単独で取得できる通常の Request を作成する
            request = new Request(
                `${Utils.api_base_url}/streams/video/${programSnapshot.id}/${apiQuality}/offline-stream?${codecQuery}`,
            );

            // 番組一覧とシークバーが通信なしでも描画できるよう、小さな付随データは動画本体より先に同じ世代へ保存する
            const cache = await OfflineVideoStorage.openCache();
            const generationBaseURL = OfflineVideoStorage.getGenerationBaseURL(programSnapshot.id, generationID);
            const assetRequests = [
                {source: `${Utils.api_base_url}/videos/${programSnapshot.id}/thumbnail`, destination: `${generationBaseURL}/assets/thumbnail.webp`},
                {source: `${Utils.api_base_url}/videos/${programSnapshot.id}/thumbnail/tiled`, destination: `${generationBaseURL}/assets/thumbnail-tiled.webp`},
            ];
            if (programSnapshot.channel !== null) {
                assetRequests.push({
                    source: `${Utils.api_base_url}/channels/${programSnapshot.channel.id}/logo`,
                    destination: `${generationBaseURL}/assets/channel-logo`,
                });
            }
            await Promise.all(assetRequests.map(async assetRequest => {
                try {
                    const assetResponse = await fetch(assetRequest.source);
                    if (assetResponse.ok === true) {
                        await cache.put(assetRequest.destination, assetResponse);
                    }
                } catch (error) {
                    // 付随データの取得失敗は動画保存を止めず、映像本体を優先する
                    console.warn('[OfflineVideos] Failed to cache an optional offline asset:', error);
                }
            }));

            // 実況 API は応答が遅くなりがちで、保存開始自体はコメントがなくても問題ないため、動画転送の登録を待たせない
            void (async () => {
                const jikkyoSource = `${Utils.api_base_url}/videos/${programSnapshot.id}/jikkyo`;
                const jikkyoDestination = `${generationBaseURL}/assets/jikkyo.json`;
                try {
                    const assetResponse = await fetch(jikkyoSource);
                    if (assetResponse.ok !== true) return;

                    // 動画保存が先に完了するとジョブは削除されるため、完成済み動画も有効な保存先として扱う
                    if (await OfflineVideoStorage.isGenerationReferenced(job.video_id, generationID) === false) {
                        return;
                    }
                    await cache.put(jikkyoDestination, assetResponse);

                    // 書き込み中にキャンセルや再保存で世代が破棄された場合は、遅れて作成した実況だけを回収する
                    if (await OfflineVideoStorage.isGenerationReferenced(job.video_id, generationID) === false) {
                        await cache.delete(jikkyoDestination);
                    }
                } catch (error) {
                    console.warn('[OfflineVideos] Failed to cache an optional offline asset:', error);
                }
            })();

            // 付随データの取得中にも別画面からキャンセルできるため、実際の動画転送を登録する直前に最新状態を確認する
            if (await OfflineVideoStorage.updateActiveJob(job) === false) {
                throw new Error('オフライン保存はキャンセルされました。');
            }
        } catch (error) {
            // 動画転送へ所有権を渡す前の失敗では、作成済みの断片と前景ロックをこの呼び出し内で回収する
            releaseForegroundLock?.();
            await KonomiTVBS4KOfflineDownloadRuntime.markJobFailed(job.job_id, error instanceof Error ? error.message : 'オフライン保存を開始できませんでした。');
            throw error;
        }

        if (isBackgroundFetchSupported === true && job.background_fetch_id !== null) {
            try {
                const registration = await navigator.serviceWorker.getRegistration();
                if (registration === undefined) {
                    throw new Error('Service Worker の登録が見つかりません。');
                }
                const backgroundFetchManager = registration.backgroundFetch;
                if (backgroundFetchManager === undefined) {
                    throw new Error('Background Fetch API を利用できません。');
                }
                await backgroundFetchManager.fetch(job.background_fetch_id, [request], {
                    title: `${programSnapshot.title}をオフライン保存`,
                    icons: [{src: '/assets/images/icons/icon-192px.png', sizes: '192x192', type: 'image/png'}],
                    downloadTotal: estimate.required_size_bytes,
                });

                // 登録処理中にキャンセルされた場合は、ブラウザへ渡した直後の Background Fetch も確実に停止する
                if (await OfflineVideoStorage.updateActiveJob(job) === false) {
                    const backgroundFetch = await backgroundFetchManager.get(job.background_fetch_id);
                    await backgroundFetch?.abort();
                    await OfflineVideoStorage.deleteGeneration(job.video_id, job.generation_id);
                    throw new Error('オフライン保存はキャンセルされました。');
                }
            } catch (error) {
                // 登録失敗時は先に保存した付随データを残さず、一覧へ失敗理由を引き継ぐ
                await KonomiTVBS4KOfflineDownloadRuntime.markJobFailed(job.job_id, error instanceof Error ? error.message : 'バックグラウンド保存を登録できませんでした。');
                throw error;
            }
            return job;
        }

        // Safari などではページが存続している間だけ通常の Fetch で同じ応答を保存する
        try {
            job.state = 'Downloading';
            if (await OfflineVideoStorage.updateActiveJob(job) === false) {
                throw new Error('オフライン保存はキャンセルされました。');
            }
        } catch (error) {
            // 転送処理へ引き渡す前の失敗は、この呼び出しで前景ロックを解放する
            releaseForegroundLock?.();
            await KonomiTVBS4KOfflineDownloadRuntime.markJobFailed(job.job_id, error instanceof Error ? error.message : 'オフライン保存を開始できませんでした。');
            throw error;
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
        return job;
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
            // ブラウザ側の停止要求が失敗しても、ローカルではキャンセル済み世代を参照不能にして断片を回収する
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
