import type {
    IOfflineDownloadJob,
    IOfflineVideo,
} from '@/services/OfflineVideos';

import OfflineVideoStorage from '@/services/OfflineVideoStorage';


/** オフライン保存ストリーム先頭の exact 生成条件。 */
interface IOfflineVideoStreamMetadata {
    video_id: number;
    file_hash: string;
    quality: string;
    video_codec: IOfflineDownloadJob['video_codec'];
    video_bit_depth: IOfflineDownloadJob['video_bit_depth'];
    requested_audio_codec: IOfflineDownloadJob['requested_audio_codec'];
    audio_codec: IOfflineVideo['audio_codec'];
}

/** Window と Service Worker で共有する保存応答の展開・終端状態処理。 */
export default class KonomiTVBS4KOfflineDownloadRuntime {

    private static readonly STREAM_MAGIC = new TextEncoder().encode('KTVBS4KODL1\n');
    private static readonly MAX_METADATA_BYTES = 1024 * 1024;
    private static readonly MAX_ASSET_PATH_BYTES = 1024;
    private static readonly MAX_MEDIA_TYPE_BYTES = 255;
    private static readonly MAX_ASSET_BYTES = 128 * 1024 * 1024;
    private static readonly TERMINATOR_PATH_LENGTH = 0xffff;

    /** Background Fetch または前景 Fetch の応答を CacheStorage へ展開する。 */
    static async finalizeResponse(jobID: string, response: Response): Promise<void> {
        const job = await OfflineVideoStorage.getJob(jobID);
        if (job === null || response.body === null) {
            throw new Error('保存ジョブまたはレスポンス本体が見つかりません。');
        }
        job.state = 'Downloading';
        if (await OfflineVideoStorage.updateActiveJob(job) === false) {
            throw new Error('オフライン保存ジョブはすでに終了しています。');
        }

        const reader = response.body.getReader();
        const pendingChunks: Uint8Array[] = [];
        let pendingChunkIndex = 0;
        let pendingChunkOffset = 0;
        let pendingLength = 0;
        let isStreamFinished = false;
        let sizeBytes = 0;
        let assetCount = 0;
        let lastPersistedProgressBytes = job.downloaded_bytes;
        let lastPersistedProgressAt = Date.now();
        const assetPaths: string[] = [];
        const assetPathSet = new Set<string>();
        const cache = await OfflineVideoStorage.openCache();
        const generationBaseURL = OfflineVideoStorage.getGenerationBaseURL(job.video_id, job.generation_id);

        /** 指定バイト数をネットワークチャンク列から読み取る。 */
        const readBytes = async (length: number): Promise<Uint8Array> => {
            while (pendingLength < length && isStreamFinished === false) {
                const result = await reader.read();
                if (result.done === true) {
                    isStreamFinished = true;
                    break;
                }
                pendingChunks.push(result.value);
                pendingLength += result.value.byteLength;
                job.downloaded_bytes += result.value.byteLength;

                // ネットワークチャンクごとの書き込みを避け、進捗表示に十分な間隔だけ IndexedDB を更新する
                const currentTime = Date.now();
                if (job.downloaded_bytes - lastPersistedProgressBytes >= 1024 * 1024 || currentTime - lastPersistedProgressAt >= 250) {
                    if (await OfflineVideoStorage.updateActiveJob(job) === false) {
                        throw new Error('オフライン保存がキャンセルされました。');
                    }
                    lastPersistedProgressBytes = job.downloaded_bytes;
                    lastPersistedProgressAt = currentTime;
                }
            }
            if (pendingLength < length) {
                throw new Error('オフライン保存データが途中で終了しました。');
            }

            const output = new Uint8Array(length);
            let outputOffset = 0;
            while (outputOffset < length) {
                const chunk = pendingChunks[pendingChunkIndex];
                const availableChunkLength = chunk.byteLength - pendingChunkOffset;
                const copyLength = Math.min(availableChunkLength, length - outputOffset);
                output.set(chunk.subarray(pendingChunkOffset, pendingChunkOffset + copyLength), outputOffset);
                outputOffset += copyLength;
                pendingChunkOffset += copyLength;
                if (pendingChunkOffset === chunk.byteLength) {
                    pendingChunkIndex++;
                    pendingChunkOffset = 0;
                }
            }
            pendingLength -= length;
            if (pendingChunkIndex >= 32) {
                pendingChunks.splice(0, pendingChunkIndex);
                pendingChunkIndex = 0;
            }
            return output;
        };

        try {
            const magic = await readBytes(this.STREAM_MAGIC.byteLength);
            if (magic.some((value, index) => value !== this.STREAM_MAGIC[index])) {
                throw new Error('オフライン保存データの形式を認識できません。');
            }

            const metadataLength = new DataView((await readBytes(4)).buffer).getUint32(0);
            if (metadataLength === 0 || metadataLength > this.MAX_METADATA_BYTES) {
                throw new Error('オフライン保存データのメタデータ長が不正です。');
            }
            const metadata = JSON.parse(new TextDecoder().decode(await readBytes(metadataLength))) as IOfflineVideoStreamMetadata;
            if (metadata.video_id !== job.video_id || metadata.file_hash !== job.program.recorded_video.file_hash ||
                metadata.quality !== job.quality || metadata.video_codec !== job.video_codec ||
                metadata.video_bit_depth !== job.video_bit_depth ||
                metadata.requested_audio_codec !== job.requested_audio_codec) {
                throw new Error('オフライン保存データのメタデータが保存ジョブと一致しません。');
            }

            while (true) {
                const pathLength = new DataView((await readBytes(2)).buffer).getUint16(0);
                if (pathLength === this.TERMINATOR_PATH_LENGTH) {
                    const terminator = new DataView((await readBytes(12)).buffer);
                    if (terminator.getUint32(0) !== assetCount || Number(terminator.getBigUint64(4)) !== sizeBytes) {
                        throw new Error('オフライン保存データの件数または合計サイズが一致しません。');
                    }
                    break;
                }
                const recordHeader = new DataView((await readBytes(10)).buffer);
                const mediaTypeLength = recordHeader.getUint16(0);
                const assetLength = Number(recordHeader.getBigUint64(2));
                if (pathLength === 0 || pathLength > this.MAX_ASSET_PATH_BYTES ||
                    mediaTypeLength === 0 || mediaTypeLength > this.MAX_MEDIA_TYPE_BYTES ||
                    assetLength === 0 || assetLength > this.MAX_ASSET_BYTES) {
                    throw new Error('オフライン保存データのアセットヘッダーが不正です。');
                }
                const assetPath = new TextDecoder('utf-8', {fatal: true}).decode(await readBytes(pathLength));
                const mediaType = new TextDecoder().decode(await readBytes(mediaTypeLength));
                if (this.isValidAssetPath(assetPath) === false || /^[\x20-\x7e]+$/.test(mediaType) === false) {
                    throw new Error('オフライン保存データのアセット情報が不正です。');
                }
                if (assetPathSet.has(assetPath) === true) {
                    throw new Error('オフライン保存データに重複したアセットがあります。');
                }
                const assetData = await readBytes(assetLength);
                await cache.put(`${generationBaseURL}/${assetPath}`, new Response(assetData, {
                    headers: {'Content-Type': mediaType},
                }));
                assetPaths.push(assetPath);
                assetPathSet.add(assetPath);
                assetCount++;
                sizeBytes += assetLength;
            }

            if (pendingLength !== 0 || (await reader.read()).done !== true) {
                throw new Error('オフライン保存データの終端以降に余分なデータがあります。');
            }
            job.state = 'Finalizing';
            if (await OfflineVideoStorage.updateActiveJob(job) === false) {
                throw new Error('オフライン保存がキャンセルされました。');
            }
            if (assetPathSet.has('playlist.m3u8') === false ||
                (job.program.recorded_video.has_video === true && assetPathSet.has('video/playlist.m3u8') === false) ||
                assetPaths.some(path => path.startsWith('audio/') && path.endsWith('/playlist.m3u8')) === false) {
                throw new Error('オフライン保存データに必要なプレイリストがありません。');
            }

            const previousVideo = await OfflineVideoStorage.getVideo(job.video_id);
            const video: IOfflineVideo = {
                video_id: job.video_id,
                generation_id: job.generation_id,
                file_hash: metadata.file_hash,
                program: job.program,
                quality: job.quality,
                video_codec: metadata.video_codec,
                video_bit_depth: metadata.video_bit_depth,
                requested_audio_codec: metadata.requested_audio_codec,
                audio_codec: metadata.audio_codec,
                size_bytes: sizeBytes,
                asset_count: assetCount,
                asset_paths: assetPaths,
                saved_at: Date.now(),
            };
            if (await OfflineVideoStorage.completeJob(job, video) === false) {
                await OfflineVideoStorage.deleteGeneration(job.video_id, job.generation_id);
                return;
            }
            if (previousVideo !== null && previousVideo.generation_id !== video.generation_id) {
                await OfflineVideoStorage.deleteGeneration(previousVideo.video_id, previousVideo.generation_id);
            }
            this.notifyWindowClients();
        } catch (error) {
            try {
                await reader.cancel();
            } catch (cancelError) {
                console.warn('[KonomiTVBS4KOfflineDownloadRuntime] Failed to cancel the response reader:', cancelError);
            }
            await this.markJobFailed(job.job_id, error instanceof Error ? error.message : 'オフライン保存データを処理できませんでした。');
            const savedVideo = await OfflineVideoStorage.getStoredVideo(job.video_id);
            if (savedVideo?.generation_id !== job.generation_id) {
                await OfflineVideoStorage.deleteGeneration(job.video_id, job.generation_id);
            }
            throw error;
        }
    }

    /** 実行中ジョブを失敗状態へ更新し、未完成世代を削除する。 */
    static async markJobFailed(jobID: string, error: string): Promise<void> {
        const job = await OfflineVideoStorage.transitionActiveJobToTerminalState(jobID, 'Failed', error);
        if (job === null) return;
        await OfflineVideoStorage.deleteGeneration(job.video_id, job.generation_id);
        this.notifyWindowClients();
    }

    /** 実行中ジョブをキャンセル状態へ更新し、ジョブと未完成世代を削除する。 */
    static async markJobCancelled(jobID: string): Promise<void> {
        const job = await OfflineVideoStorage.transitionActiveJobToTerminalState(jobID, 'Cancelled', null);
        if (job === null) return;
        await OfflineVideoStorage.deleteGeneration(job.video_id, job.generation_id);
        await OfflineVideoStorage.deleteJob(job.job_id);
        this.notifyWindowClients();
    }

    /** サーバーが許可する正規化済み相対アセットパスだけを受け入れる。 */
    private static isValidAssetPath(path: string): boolean {
        return path.length > 0 && path.startsWith('/') === false && path.includes('\\') === false &&
            path.includes('?') === false && path.includes('#') === false &&
            path.split('/').every(part => part !== '' && part !== '.' && part !== '..') &&
            /^[0-9A-Za-z._/-]+$/.test(path);
    }

    /** Service Worker 内の変更を、EventTarget を共有できないページへ通知する。 */
    private static notifyWindowClients(): void {
        if (typeof window !== 'undefined') return;

        const serviceWorkerScope = self as unknown as ServiceWorkerGlobalScope;
        void serviceWorkerScope.clients.matchAll({type: 'window', includeUncontrolled: true}).then((clients) => {
            for (const client of clients) {
                client.postMessage({type: 'konomitv-bs4k-offline-videos-change'});
            }
        });
    }
}
