import APIClient from '@/services/APIClient';


// server/app/schemas.py の KonomiTVBS4KCodecSupport* 系列と対応させる。
// 親となる Job のスキーマを最上位に置き、フィールドの定義順に従って子スキーマを並べる。

/** 管理者専用のサーバー診断共有ジョブの状態。実行中は部分結果を含む。 */
export interface IKonomiTVBS4KCodecSupportServerJob {
    status: 'Idle' | 'Running' | 'Completed' | 'Failed';
    progress: number;
    environment_signature: string | null;
    devices: IKonomiTVBS4KCodecSupportServerDevice[];
}

/** 診断対象の CPU / GPU。render node パス・PCI BDF・UUID・stderr 全文は含めない。 */
export interface IKonomiTVBS4KCodecSupportServerDevice {
    id: string;
    label: string;
    kind: 'CPU' | 'GPU';
    vendor: 'Intel' | 'NVIDIA' | 'AMD' | 'None';
    capabilities: IKonomiTVBS4KCodecSupportServerCapability[];
}

/** 1つの映像・音声コーデックの decode / encode 能力。 */
export interface IKonomiTVBS4KCodecSupportServerCapability {
    media_type: 'Video' | 'Audio';
    codec: string;
    profile: string | null;
    bit_depth: 8 | 10 | null;
    decode: IKonomiTVBS4KCodecSupportServerOperationSupport;
    encode: IKonomiTVBS4KCodecSupportServerOperationSupport;
    used_by_konomitv_bs4k: boolean;
}

/** 1つの decode / encode operation の状態と根拠。 */
export interface IKonomiTVBS4KCodecSupportServerOperationSupport {
    status: 'Supported' | 'Likely' | 'Unsupported' | 'Unknown';
    evidence: 'VerifiedProbe' | 'Driver' | 'Binary' | 'ProbeFailed' | 'Unavailable';
    backend: 'FFmpeg' | 'QSV' | 'NVENC' | 'AMF' | null;
    reason_code:
    | 'BinaryUnavailable'
    | 'EncoderUnavailable'
    | 'DecoderUnavailable'
    | 'DeviceUnavailable'
    | 'DeviceInitializationFailed'
    | 'FilterUnavailable'
    | 'EncodeFailed'
    | 'DecodeFailed'
    | 'CodecMismatch'
    | 'BitDepthMismatch'
    | 'ProfileMismatch'
    | 'ProbeTimeout'
    | 'ProbeFailed'
    | 'ProbeInputUnavailable'
    | 'UnsupportedCombination'
    | 'UnsupportedByDevice'
    | null;
    tested_configuration: string | null;
}


/**
 * サーバー側のコーデック対応診断 (FFmpeg / QSV / NVENC / AMF の実 probe) を操作するサービス。
 * 全 API は管理者専用。
 */
class KonomiTVBS4KCodecSupport {

    private static readonly BASE_URL = '/maintenance/konomitv-bs4k-codec-support';

    /**
     * 共有診断ジョブを開始または実行中ジョブへ合流する。
     * @param force true で同一環境署名の結果キャッシュがあっても再診断する
     * @returns job 状態。失敗時は null
     */
    static async startProbe(force: boolean): Promise<IKonomiTVBS4KCodecSupportServerJob | null> {
        const response = await APIClient.post<IKonomiTVBS4KCodecSupportServerJob>(
            `${this.BASE_URL}/probe`,
            null,
            {params: {force: force}},
        );
        if (response.type === 'error') {
            if (response.status !== 401 && response.status !== 403) {
                APIClient.showGenericError(response, 'サーバーのコーデック対応診断を開始できませんでした。');
            }
            return null;
        }
        return response.data;
    }

    /**
     * 共有診断ジョブの状態と部分結果を取得する。
     * @param show_error 失敗時にエラー通知を表示するか。ポーリング中の繰り返し失敗では false にする
     * @returns job 状態。失敗時は null
     */
    static async getState(show_error: boolean = true): Promise<IKonomiTVBS4KCodecSupportServerJob | null> {
        const response = await APIClient.get<IKonomiTVBS4KCodecSupportServerJob>(
            this.BASE_URL,
        );
        if (response.type === 'error') {
            if (show_error === true && response.status !== 401 && response.status !== 403) {
                APIClient.showGenericError(response, 'サーバーのコーデック対応診断の状態を取得できませんでした。');
            }
            return null;
        }
        return response.data;
    }

    /** 実行中の共有診断ジョブをキャンセルする。冪等。回収失敗時は false。 */
    static async cancelProbe(): Promise<boolean> {
        const response = await APIClient.delete(`${this.BASE_URL}/probe`);
        if (response.type === 'error') {
            if (response.status === 503) {
                APIClient.showGenericError(response, '診断のキャンセルは受け付けましたが、外部プロセスの回収が完了していません。');
            } else if (response.status !== 401 && response.status !== 403) {
                APIClient.showGenericError(response, 'サーバーのコーデック対応診断をキャンセルできませんでした。');
            }
            return false;
        }
        return true;
    }
}

export default KonomiTVBS4KCodecSupport;
