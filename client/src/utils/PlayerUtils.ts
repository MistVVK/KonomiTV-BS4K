
import type {
    BS4KLiveStreamingQuality,
    IKonomiTVBS4KPlaybackVideoProfile,
    KonomiTVBS4KPlaybackAudioCodec,
    KonomiTVBS4KPlaybackVideoCodec,
    LiveStreamingQuality,
    VideoStreamingQuality,
} from '@/stores/SettingsStore';
import type DPlayer from 'dplayer';


import Utils from '@/utils/Utils';


/**
 * ライブ/録画番組ストリーミング API でベース画質として設定できる動画の画質
 */
type APIBaseVideoQuality = (
    '4320p' |
    '4320p-hevc' |
    '2160p' |
    '2160p-hevc' |
    '1440p' |
    '1440p-hevc' |
    '1080p-60fps' |
    '1080p-60fps-hevc' |
    '1080p-30fps' |
    '1080p-30fps-hevc' |
    '1080p' |
    '1080p-hevc' |
    '810p-60fps' |
    '810p-60fps-hevc' |
    '810p-30fps' |
    '810p-30fps-hevc' |
    '810p' |
    '810p-hevc' |
    '720p-60fps' |
    '720p-60fps-hevc' |
    '720p-30fps' |
    '720p-30fps-hevc' |
    '720p' |
    '720p-hevc' |
    '540p-30fps' |
    '540p-30fps-hevc' |
    '540p' |
    '540p-hevc' |
    '480p-30fps' |
    '480p-30fps-hevc' |
    '480p' |
    '480p-hevc' |
    '360p-30fps' |
    '360p-30fps-hevc' |
    '360p' |
    '360p-hevc' |
    '240p-30fps' |
    '240p-30fps-hevc' |
    '240p' |
    '240p-hevc'
);

/**
 * ライブストリーミング API で設定できる動画の画質
 */
type LiveAPIVideoQuality = (
    APIBaseVideoQuality |
    `${APIBaseVideoQuality}-10bit` |
    `${APIBaseVideoQuality}-24fps` |
    `${APIBaseVideoQuality}-10bit-24fps`
);

/**
 * 録画番組ストリーミング API で設定できる動画の画質
 */
type VideoAPIVideoQuality = (
    APIBaseVideoQuality |
    `${APIBaseVideoQuality}-10bit` |
    `${APIBaseVideoQuality}-24fps` |
    `${APIBaseVideoQuality}-10bit-24fps`
);


/**
 * KonomiTV-BS4K の DPlayer で選択できる、suffix 付与前の API 画質。
 */
export type KonomiTVBS4KPlaybackSelectableQuality =
    LiveStreamingQuality | BS4KLiveStreamingQuality | VideoStreamingQuality;


/**
 * server/app/constants.py の各 QUALITY が生成する最大解像度・fps を満たす最小 codec level。
 *
 * DPlayer の表示名ではなく suffix 付与前の API 画質をキーにすることで、初期化時と画質切り替え時が
 * 必ず同じ MSE SourceBuffer 条件を参照する。
 */
const KONOMITV_BS4K_PLAYBACK_VIDEO_LEVELS: Record<KonomiTVBS4KPlaybackSelectableQuality, {
    avc: string;
    hevc: string;
    vp9: string;
    av1: string;
}> = {
    // 7680x4320 / 60fps
    '4320p': {avc: '3D', hevc: '183', vp9: '61', av1: '17'},
    // 3840x2160 / 60fps
    '2160p': {avc: '34', hevc: '153', vp9: '51', av1: '13'},
    // 2560x1440 / 60fps
    '1440p': {avc: '33', hevc: '150', vp9: '50', av1: '12'},
    // 1440x1080 / 60fps
    '1080p-60fps': {avc: '2A', hevc: '126', vp9: '41', av1: '09'},
    // 1440x1080 / 30fps
    // mpegts.js の VP9 sequence header は fps を取得できず、全画質で解像度を常に 60fps として level 化する。
    '1080p-30fps': {avc: '28', hevc: '120', vp9: '41', av1: '08'},
    '1080p': {avc: '28', hevc: '120', vp9: '41', av1: '08'},
    // 1440x810 / 60fps
    '810p-60fps': {avc: '2A', hevc: '126', vp9: '40', av1: '08'},
    // 1440x810 / 30fps
    '810p-30fps': {avc: '20', hevc: '120', vp9: '40', av1: '08'},
    '810p': {avc: '20', hevc: '120', vp9: '40', av1: '08'},
    // 1280x720 / 60fps
    '720p-60fps': {avc: '20', hevc: '120', vp9: '40', av1: '08'},
    // 1280x720 / 30fps
    '720p-30fps': {avc: '1F', hevc: '93', vp9: '40', av1: '05'},
    '720p': {avc: '1F', hevc: '93', vp9: '40', av1: '05'},
    // 960x540 / 30fps
    '540p-30fps': {avc: '1F', hevc: '90', vp9: '31', av1: '04'},
    '540p': {avc: '1F', hevc: '90', vp9: '31', av1: '04'},
    // 854x480 / 30fps
    '480p-30fps': {avc: '1F', hevc: '90', vp9: '31', av1: '04'},
    '480p': {avc: '1F', hevc: '90', vp9: '31', av1: '04'},
    // 640x360 / 30fps
    '360p-30fps': {avc: '1E', hevc: '63', vp9: '30', av1: '01'},
    '360p': {avc: '1E', hevc: '63', vp9: '30', av1: '01'},
    // 426x240 / 30fps
    '240p-30fps': {avc: '15', hevc: '60', vp9: '21', av1: '00'},
    '240p': {avc: '15', hevc: '60', vp9: '21', av1: '00'},
};


/**
 * プレイヤー周りのユーティリティ
 */
export class PlayerUtils {

    /**
     * codec / bit depth / 実際の選択画質ごとに、mpegts.js・hls.js が生成し得る
     * SourceBuffer の MIME を返す。
     */
    static getKonomiTVBS4KPlaybackVideoMIMEType(
        konomitv_bs4k_codec: KonomiTVBS4KPlaybackVideoCodec,
        konomitv_bs4k_bit_depth: 8 | 10,
        konomitv_bs4k_profile: IKonomiTVBS4KPlaybackVideoProfile,
    ): string | null {
        const konomitv_bs4k_levels =
            KONOMITV_BS4K_PLAYBACK_VIDEO_LEVELS[konomitv_bs4k_profile.streaming_quality];
        const konomitv_bs4k_mime_types:
        Record<KonomiTVBS4KPlaybackVideoCodec, Record<8 | 10, string | null>> = {
            avc: {8: `video/mp4; codecs="avc1.6400${konomitv_bs4k_levels.avc}"`, 10: null},
            hevc: {
                8: `video/mp4; codecs="hvc1.1.6.L${konomitv_bs4k_levels.hevc}.B0"`,
                10: `video/mp4; codecs="hvc1.2.4.L${konomitv_bs4k_levels.hevc}.B0"`,
            },
            vp9: {
                8: `video/mp4; codecs="vp09.00.${konomitv_bs4k_levels.vp9}.08"`,
                10: `video/mp4; codecs="vp09.02.${konomitv_bs4k_levels.vp9}.10"`,
            },
            av1: {
                8: `video/mp4; codecs="av01.0.${konomitv_bs4k_levels.av1}M.08"`,
                10: `video/mp4; codecs="av01.0.${konomitv_bs4k_levels.av1}M.10"`,
            },
        };
        return konomitv_bs4k_mime_types[konomitv_bs4k_codec][konomitv_bs4k_bit_depth];
    }


    /** suffix 付与前の API 画質を、DPlayer に表示する画質名へ変換する。 */
    static getKonomiTVBS4KPlaybackQualityDisplayName(
        konomitv_bs4k_api_quality: KonomiTVBS4KPlaybackSelectableQuality,
    ): string {
        if (konomitv_bs4k_api_quality === '4320p') return '8K';
        if (konomitv_bs4k_api_quality === '2160p') return '4K';
        if (konomitv_bs4k_api_quality.endsWith('-60fps')) {
            return `${konomitv_bs4k_api_quality.replace('-60fps', '')} (60fps)`;
        }
        if (konomitv_bs4k_api_quality.endsWith('-30fps')) {
            return `${konomitv_bs4k_api_quality.replace('-30fps', '')} (30fps)`;
        }
        return konomitv_bs4k_api_quality;
    }


    /**
     * 保存値・レジューム値・DPlayer の表示名を、suffix 付与前の API 画質へ正規化する。
     *
     * 未知の表示名を既定画質へ黙って読み替えると、画質切り替え時の MSE 判定を別画質で通してしまうため null を返す。
     */
    static normalizeKonomiTVBS4KPlaybackAPIQuality(
        konomitv_bs4k_quality_name: string,
        is_konomitv_bs4k: boolean,
        konomitv_bs4k_available_qualities: readonly KonomiTVBS4KPlaybackSelectableQuality[],
    ): KonomiTVBS4KPlaybackSelectableQuality | null {
        const konomitv_bs4k_legacy_quality_map: Record<string, BS4KLiveStreamingQuality> = {
            '1080p': '1080p-30fps',
            '810p': '810p-30fps',
            '720p': '720p-30fps',
            '540p': '540p-30fps',
            '480p': '480p-30fps',
            '360p': '360p-30fps',
            '240p': '240p-30fps',
        };
        let konomitv_bs4k_normalized_quality = konomitv_bs4k_quality_name.trim();
        if (konomitv_bs4k_normalized_quality === '8K') {
            konomitv_bs4k_normalized_quality = '4320p';
        } else if (konomitv_bs4k_normalized_quality === '4K') {
            konomitv_bs4k_normalized_quality = '2160p';
        } else {
            const konomitv_bs4k_display_name_match =
                konomitv_bs4k_normalized_quality.match(/^(.+) \((60|30)fps\)$/);
            if (konomitv_bs4k_display_name_match !== null) {
                konomitv_bs4k_normalized_quality =
                    `${konomitv_bs4k_display_name_match[1]}-${konomitv_bs4k_display_name_match[2]}fps`;
            }
        }
        if (is_konomitv_bs4k === true) {
            konomitv_bs4k_normalized_quality =
                konomitv_bs4k_legacy_quality_map[konomitv_bs4k_normalized_quality] ??
                konomitv_bs4k_normalized_quality;
        }
        return konomitv_bs4k_available_qualities.includes(
            konomitv_bs4k_normalized_quality as KonomiTVBS4KPlaybackSelectableQuality,
        ) ?
            konomitv_bs4k_normalized_quality as KonomiTVBS4KPlaybackSelectableQuality :
            null;
    }


    /** 現在のブラウザが指定映像codecのMSE SourceBufferを作成できるか返す。 */
    static isKonomiTVBS4KPlaybackVideoCodecSupported(
        konomitv_bs4k_codec: KonomiTVBS4KPlaybackVideoCodec,
        konomitv_bs4k_bit_depth: 8 | 10,
        konomitv_bs4k_profile: IKonomiTVBS4KPlaybackVideoProfile,
    ): boolean {
        const konomitv_bs4k_mime_type = this.getKonomiTVBS4KPlaybackVideoMIMEType(
            konomitv_bs4k_codec,
            konomitv_bs4k_bit_depth,
            konomitv_bs4k_profile,
        );
        if (konomitv_bs4k_mime_type === null) return false;
        return this.isKonomiTVBS4KPlaybackMIMETypeSupported(konomitv_bs4k_mime_type);
    }


    /** 現在のブラウザが指定音声codecのMSE SourceBufferを作成できるか返す。 */
    static isKonomiTVBS4KPlaybackAudioCodecSupported(
        konomitv_bs4k_codec: KonomiTVBS4KPlaybackAudioCodec,
    ): boolean {
        const konomitv_bs4k_mime_type = konomitv_bs4k_codec === 'opus' ?
            `audio/mp4; codecs="${Utils.isSafari() ? 'Opus' : 'opus'}"` :
            'audio/mp4; codecs="mp4a.40.2"';
        return this.isKonomiTVBS4KPlaybackMIMETypeSupported(konomitv_bs4k_mime_type);
    }


    /**
     * ライブと録画が実際に選ぶ全MediaSource実装でMIMEを利用できるか返す。
     *
     * 両APIが共存するSafariでは、mpegts.jsはMediaSourceを、hls.jsは
     * preferManagedMediaSourceによりManagedMediaSourceを使うため、共通profileには両方の対応が必要。
     */
    private static isKonomiTVBS4KPlaybackMIMETypeSupported(konomitv_bs4k_mime_type: string): boolean {
        const konomitv_bs4k_media_source_api = window.MediaSource;
        const konomitv_bs4k_managed_media_source_api = (
            window as Window & {
                ManagedMediaSource?: {isTypeSupported: (konomitv_bs4k_mime_type: string) => boolean};
            }
        ).ManagedMediaSource;
        if (
            konomitv_bs4k_media_source_api === undefined &&
            konomitv_bs4k_managed_media_source_api === undefined
        ) {
            return false;
        }
        return (
            konomitv_bs4k_media_source_api === undefined ||
            konomitv_bs4k_media_source_api.isTypeSupported(konomitv_bs4k_mime_type) === true
        ) && (
            konomitv_bs4k_managed_media_source_api === undefined ||
            konomitv_bs4k_managed_media_source_api.isTypeSupported(konomitv_bs4k_mime_type) === true
        );
    }


    /** ライブ通常APIへ渡すcodec queryを安定した順序で生成する。 */
    static buildKonomiTVBS4KLivePlaybackCodecQuery(konomitv_bs4k_profile: {
        video_codec: KonomiTVBS4KPlaybackVideoCodec;
        video_bit_depth: 8 | 10;
        audio_codec: KonomiTVBS4KPlaybackAudioCodec;
    }): string {
        return new URLSearchParams({
            video_codec: konomitv_bs4k_profile.video_codec,
            video_bit_depth: konomitv_bs4k_profile.video_bit_depth.toString(),
            audio_codec: konomitv_bs4k_profile.audio_codec,
        }).toString();
    }

    /**
     * DPlayer のインスタンスからライブストリーミング API で設定できる画質を取得する
     * @param player DPlayer のインスタンス
     * @returns API で設定できる画質 (取得できなかった場合は基本復旧不能だが、一応 "1080p" を返す)
     */
    static extractLiveAPIQualityFromDPlayer(player: DPlayer): LiveAPIVideoQuality {
        if (player.quality === null) {
            return '1080p';
        }
        const regex = /streams\/live\/[a-z0-9-]*\/(.*)\/mpegts/;
        const match = player.quality.url.match(regex);
        return match ? (match[1] as LiveAPIVideoQuality) : '1080p';
    }


    /**
     * 再生中の MPEG-TS URL から codec query だけを正規順序で取り出す。
     * events / psi-archived-data を必ず同一 LiveStream 共有キーへ接続するために使う。
     */
    static extractKonomiTVBS4KLivePlaybackCodecQueryFromDPlayer(
        konomitv_bs4k_player: DPlayer,
    ): string {
        if (konomitv_bs4k_player.quality === null) return '';
        const konomitv_bs4k_source_url =
            new URL(konomitv_bs4k_player.quality.url, window.location.origin);
        const konomitv_bs4k_query = new URLSearchParams();
        for (
            const konomitv_bs4k_key of
            ['video_codec', 'video_bit_depth', 'audio_codec'] as const
        ) {
            const konomitv_bs4k_value =
                konomitv_bs4k_source_url.searchParams.get(konomitv_bs4k_key);
            if (konomitv_bs4k_value !== null) {
                konomitv_bs4k_query.set(konomitv_bs4k_key, konomitv_bs4k_value);
            }
        }
        return konomitv_bs4k_query.toString();
    }


    /** 指定画質・codec queryを持つライブ通常API URLを、全endpoint共通の規則で生成する。 */
    static buildKonomiTVBS4KLiveAPIEndpointURL(
        konomitv_bs4k_display_channel_id: string,
        konomitv_bs4k_api_quality: string,
        konomitv_bs4k_endpoint: 'mpegts' | 'events' | 'psi-archived-data',
        konomitv_bs4k_codec_query: string,
    ): string {
        const konomitv_bs4k_url =
            `${Utils.api_base_url}/streams/live/${konomitv_bs4k_display_channel_id}/` +
            `${konomitv_bs4k_api_quality}/${konomitv_bs4k_endpoint}`;
        return konomitv_bs4k_codec_query === '' ?
            konomitv_bs4k_url :
            `${konomitv_bs4k_url}?${konomitv_bs4k_codec_query}`;
    }


    /** 再生中のMPEG-TSと同じ画質・codec queryを持つライブ通常API URLを生成する。 */
    static buildKonomiTVBS4KLiveAPIEndpointURLFromDPlayer(
        konomitv_bs4k_player: DPlayer,
        konomitv_bs4k_display_channel_id: string,
        konomitv_bs4k_endpoint: 'mpegts' | 'events' | 'psi-archived-data',
    ): string {
        const konomitv_bs4k_api_quality =
            this.extractLiveAPIQualityFromDPlayer(konomitv_bs4k_player);
        const konomitv_bs4k_codec_query =
            this.extractKonomiTVBS4KLivePlaybackCodecQueryFromDPlayer(konomitv_bs4k_player);
        return this.buildKonomiTVBS4KLiveAPIEndpointURL(
            konomitv_bs4k_display_channel_id,
            konomitv_bs4k_api_quality,
            konomitv_bs4k_endpoint,
            konomitv_bs4k_codec_query,
        );
    }


    /**
     * DPlayer のインスタンスから録画番組ストリーミング API で設定できる画質を取得する
     * @param player DPlayer のインスタンス
     * @returns API で設定できる画質 (取得できなかった場合は基本復旧不能だが、一応 "1080p" を返す)
     */
    static extractVideoAPIQualityFromDPlayer(player: DPlayer): VideoAPIVideoQuality {
        if (player.quality === null) {
            return '1080p';
        }
        const regex = /streams\/video\/[0-9]*\/(.*)\/playlist/;
        const match = player.quality.url.match(regex);
        return match ? (match[1] as VideoAPIVideoQuality) : '1080p';
    }


    /**
     * DPlayer のインスタンスから URL クエリパラメーターにある session_id を取得する
     * @param player DPlayer のインスタンス
     * @returns URL クエリパラメーターにある session_id (取得できなかった場合は null)
     */
    static extractSessionIdFromDPlayer(player: DPlayer): string | null {
        if (player.quality === null) {
            return null;
        }
        const url = new URL(player.quality.url);
        return url.searchParams.get('session_id');
    }


    /**
     * プレイヤーの背景写真をランダムで取得し、その URL を返す
     * @returns ランダムで設定されたプレイヤーの背景写真の URL
     */
    static generatePlayerBackgroundURL(): string {
        const background_count = 90;  // 90種類から選択
        const random = (Math.floor(Math.random() * background_count) + 1);
        return `/assets/images/player-backgrounds/${random.toString().padStart(2, '0')}.jpg`;
    }


    /**
     * Network Information API から現在接続されているネットワーク回線の種類を取得する
     * navigator.connection.type が利用できない場合は null を返す
     * "Wi-Fi" には Cellular 以外のすべてのネットワークが含まれる
     * @returns ネットワークの種類 (Wi-Fi / Cellular)、取得できなかった場合は null
     */
    static getNetworkCircuitType(): 'Wi-Fi' | 'Cellular' | null {
        if (navigator.connection && navigator.connection.type) {
            switch (navigator.connection.type) {
                case 'cellular':
                    return 'Cellular';
                // 複数の Android 端末での検証の結果、モバイル回線 (4G/5G) に接続されているにも関わらず "unknown" が返されることがある
                // 一方 Wi-Fi 接続時は確実に "wi-fi" が返されるため、"unknown" の場合は "Cellular" として扱う
                case 'unknown':
                    return 'Cellular';
                default:
                    return 'Wi-Fi';
            }
        } else {
            console.warn('[PlayerUtils] navigator.connection.type is not available.');
            return null;
        }
    }


    /**
     * 現在のブラウザで H.265 / HEVC 映像が再生できるかどうかを取得する
     * ref: https://github.com/StaZhu/enable-chromium-hevc-hardware-decoding#mediacapabilities
     * @returns 再生できるなら true、できないなら false
     */
    static isHEVCVideoSupported(): boolean {
        // hvc1.1.6.L123.B0 の部分は呪文 (HEVC であることと、そのプロファイルを示す値らしい)
        return document.createElement('video').canPlayType('video/mp4; codecs="hvc1.1.6.L123.B0"') === 'probably';
    }


    /**
     * 現在のブラウザで H.265 / HEVC Main10 映像が再生できるかどうかを取得する
     * @returns 再生できるなら true、できないなら false
     */
    static async isHEVC10bitVideoSupported(): Promise<boolean> {
        // 本来の Main10 プロファイルは hvc1.2.4.L123.B0 らしいが、mpegts.js の MIME 文字列生成ロジックに合わせてこれで検証している
        const video_content_type = 'video/mp4; codecs="hvc1.2.1.L123.B0"';
        const audio_content_type = 'audio/mp4; codecs="mp4a.40.2"';

        // HEVC 10bit は透過的に有効化するため、MediaCapabilities で対応を判断できる環境だけを対象にする
        // ここで対象外になっても通常の HEVC 8bit 再生へ戻せるため、互換性を優先する
        if (navigator.mediaCapabilities === undefined) {
            return false;
        }

        // mpegts.js は映像と音声の SourceBuffer を別々に作る
        // iPhone Safari では通常の MediaSource がなく ManagedMediaSource だけが存在するため、mpegts.js と同じく両方を確認する
        const media_source_api = window.MediaSource;
        const managed_media_source_api = window.ManagedMediaSource;
        const is_source_buffer_supported =
            (media_source_api !== undefined &&
             media_source_api.isTypeSupported(video_content_type) === true &&
             media_source_api.isTypeSupported(audio_content_type) === true) ||
            (managed_media_source_api !== undefined &&
             managed_media_source_api.isTypeSupported(video_content_type) === true &&
             managed_media_source_api.isTypeSupported(audio_content_type) === true);
        if (is_source_buffer_supported === false) {
            return false;
        }

        try {
            const decoding_info = await navigator.mediaCapabilities.decodingInfo({
                type: 'media-source',
                audio: {
                    contentType: audio_content_type,
                    channels: '2',
                    bitrate: 192000,
                    samplerate: 48000,
                },
                video: {
                    contentType: video_content_type,
                    width: 1920,
                    height: 1080,
                    bitrate: 5200000,
                    framerate: 60,
                },
            });

            // Safari の MediaCapabilities は、iPhone 実機で実際には滑らかに再生できる HEVC 8bit / 10bit でも smooth: false を返すことがある
            // supported と powerEfficient は true を返すため、Safari では smooth を参考値として扱う
            if (Utils.isSafari() === true) {
                return decoding_info.supported === true && decoding_info.powerEfficient === true;
            }

            // Android タブレットでは HEVC 10bit 再生に対応しない個体が多いため、Safari 以外では smooth も必須にする
            return decoding_info.supported === true && decoding_info.smooth === true && decoding_info.powerEfficient === true;
        } catch (error) {
            // MediaCapabilities API の実装差で例外が出ても、通常の HEVC 8bit 再生へ戻せば視聴は継続できる
            console.warn('[PlayerUtils] Failed to check HEVC 10bit playback support.', error);
            return false;
        }
    }
}
