
/**
 * 録画 fMP4 の HDR 色信号書換えモジュール
 *
 * 録画 HLS (オンライン / CacheStorage 上のオフライン保存) が配信する fMP4 の
 * 色信号 (colour_primaries / transfer_characteristics / matrix_coeffs) を、
 * 初期化セグメントとメディアセグメント内の in-band シグナリングの両方について、
 * バイト長を変えずにその場で書き換える。
 *
 * 目的は SDR 変換 (ToneMap) 時にブラウザ独自の HDR 変換パスを抑止することだけであり、
 * HLG / PQ のコード値自体は一切変更しない。書換えポリシーはライブ再生で使っている
 * mpegts.js fork の video-color-rewrite.ts (ToneMap 時は BT.709 primaries + sRGB transfer) と
 * 一致させてあり、HDR 変換本体 (WebGL canvas) もライブと同一のものを再利用する。
 *
 * 安全に書き換えられない (不正・未知の構造の) fMP4 に対してはバイト列を一切改変せず、
 * unsafe フラグで呼び出し側へ通知する。呼び出し側は HDR 素通しでの再生継続へ切り替える。
 */


// CICP (ISO/IEC 23001-8) のコード値
export const CICP_BT709_PRIMARIES = 1;
export const CICP_BT709_TRANSFER = 1;
export const CICP_SRGB_TRANSFER = 13;
export const CICP_PQ_TRANSFER = 16;
export const CICP_HLG_TRANSFER = 18;


/** 色信号の3要素 (CICP コード値) */
export interface KonomiTVBS4KFMP4ColorTuple {
    colour_primaries: number;
    transfer_characteristics: number;
    matrix_coeffs: number;
}

/** 書換えモード。None は素通し、ToneMap は SDR 変換用の信号書換え */
export type KonomiTVBS4KFMP4ColorRewriteMode = 'None' | 'ToneMap';

/** メディアセグメントの in-band 書換えに必要な、初期化セグメントから確定した映像トラックの状態 */
export interface KonomiTVBS4KFMP4VideoColorState {
    codec: 'AVC' | 'HEVC' | 'VP9' | 'AV1';
    // AVC / HEVC の length-prefixed NAL の長さフィールドバイト数 (1 / 2 / 4)。VP9 / AV1 では 0
    nal_length_size: number;
    // 初期化セグメントから検出した元の色信号
    original: KonomiTVBS4KFMP4ColorTuple;
}

/** 書換え結果 */
export interface KonomiTVBS4KFMP4RewriteResult {
    // 書換え後のバイト列。未変更の場合は入力と同じ参照を返す
    data: Uint8Array;
    // 検出した元の色信号。色信号を検出できなかった場合は null
    detected: KonomiTVBS4KFMP4ColorTuple | null;
    // 実際にバイト列を書き換えたか
    rewritten: boolean;
    // 安全に書き換えられない入力を検出したか (true の場合 data は必ず入力のまま)
    unsafe: boolean;
    // 初期化セグメントの書換え時だけ返す、メディアセグメント処理用の映像トラック状態
    video_state: KonomiTVBS4KFMP4VideoColorState | null;
}


/**
 * 書換え後の色信号を決定する。
 * ライブ用 mpegts.js fork の resolveVideoColorRewrite() と同じポリシー。
 * @param original 元の色信号
 * @param mode 書換えモード
 * @returns 書換え後の色信号 (書換え不要なら original と同値)
 */
export function resolveKonomiTVBS4KFMP4ColorRewrite(
    original: KonomiTVBS4KFMP4ColorTuple,
    mode: KonomiTVBS4KFMP4ColorRewriteMode,
): KonomiTVBS4KFMP4ColorTuple {
    // 素通し、または HDR (HLG / PQ) 以外の信号はそのまま返す
    if (mode === 'None' ||
        (original.transfer_characteristics !== CICP_HLG_TRANSFER &&
            original.transfer_characteristics !== CICP_PQ_TRANSFER)) {
        return original;
    }
    // ToneMap: HLG / PQ のコード値は維持したまま、ブラウザの HDR 表示パスを抑止する信号へ変更する
    return {
        colour_primaries: CICP_BT709_PRIMARIES,
        transfer_characteristics: CICP_SRGB_TRANSFER,
        matrix_coeffs: original.matrix_coeffs,
    };
}

/** 色信号が HDR (HLG / PQ) を示すかどうかを返す */
export function isKonomiTVBS4KFMP4HdrColorTuple(tuple: KonomiTVBS4KFMP4ColorTuple): boolean {
    return tuple.transfer_characteristics === CICP_HLG_TRANSFER ||
        tuple.transfer_characteristics === CICP_PQ_TRANSFER;
}

function colorTuplesEqual(
    left: KonomiTVBS4KFMP4ColorTuple,
    right: KonomiTVBS4KFMP4ColorTuple,
): boolean {
    return left.colour_primaries === right.colour_primaries &&
        left.transfer_characteristics === right.transfer_characteristics &&
        left.matrix_coeffs === right.matrix_coeffs;
}


/**
 * Exp-Golomb 符号を読むためのビットリーダー
 * mpegts.js fork の exp-golomb.js と同等の読み進め規則を持つ簡易実装。
 * バッファを読み越した場合は Error を投げ、呼び出し側で不正入力として処理する。
 */
class BitReader {

    private readonly buffer: Uint8Array;
    private byte_index = 0;
    private current_word = 0;
    private current_word_bits_left = 0;

    constructor(buffer: Uint8Array) {
        this.buffer = buffer;
    }

    /** 先頭から消費したビット数を返す */
    public getBitsConsumed(): number {
        return this.byte_index * 8 - this.current_word_bits_left;
    }

    private fillCurrentWord(): void {
        const bytes_left = this.buffer.byteLength - this.byte_index;
        if (bytes_left <= 0) {
            throw new Error('BitReader: no bytes available.');
        }
        const bytes_read = Math.min(4, bytes_left);
        const word = new Uint8Array(4);
        word.set(this.buffer.subarray(this.byte_index, this.byte_index + bytes_read));
        this.current_word = new DataView(word.buffer).getUint32(0, false);
        this.byte_index += bytes_read;
        this.current_word_bits_left = bytes_read * 8;
    }

    public readBits(bits: number): number {
        if (bits > 32) {
            throw new Error('BitReader: readBits() exceeded max 32 bits.');
        }
        // JS のシフト演算は回数が mod 32 になるため、0 ビット読みは専用に扱う
        if (bits === 0) {
            return 0;
        }
        if (bits <= this.current_word_bits_left) {
            const result = this.current_word >>> (32 - bits);
            this.current_word = (this.current_word << bits) >>> 0;
            this.current_word_bits_left -= bits;
            return result;
        }
        let result = this.current_word_bits_left > 0 ? this.current_word : 0;
        result = result >>> (32 - this.current_word_bits_left);
        const bits_need_left = bits - this.current_word_bits_left;
        this.fillCurrentWord();
        const bits_read_next = Math.min(bits_need_left, this.current_word_bits_left);
        const result2 = this.current_word >>> (32 - bits_read_next);
        this.current_word = (this.current_word << bits_read_next) >>> 0;
        this.current_word_bits_left -= bits_read_next;
        return ((result << bits_read_next) | result2) >>> 0;
    }

    public readBool(): boolean {
        return this.readBits(1) === 1;
    }

    public readByte(): number {
        return this.readBits(8);
    }

    private skipLeadingZeros(): number {
        let total_zero_count = 0;
        while (true) {
            if (this.current_word_bits_left === 0) {
                this.fillCurrentWord();
            }
            let found = false;
            let zero_count = 0;
            for (; zero_count < this.current_word_bits_left; zero_count++) {
                if ((this.current_word & (0x80000000 >>> zero_count)) !== 0) {
                    found = true;
                    break;
                }
            }
            if (found === true) {
                this.current_word = (this.current_word << zero_count) >>> 0;
                this.current_word_bits_left -= zero_count;
                return total_zero_count + zero_count;
            }
            // 現在のワードが全て 0 だった。ワード全体を消費して次のワードへ進む
            total_zero_count += this.current_word_bits_left;
            this.current_word = 0;
            this.current_word_bits_left = 0;
            if (this.byte_index >= this.buffer.byteLength) {
                throw new Error('BitReader: Exp-Golomb code exceeds the buffer.');
            }
        }
    }

    public readUEG(): number {
        const leading_zeros = this.skipLeadingZeros();
        return this.readBits(leading_zeros + 1) - 1;
    }

    public readSEG(): number {
        const value = this.readUEG();
        if ((value & 0x01) !== 0) {
            return (value + 1) >>> 1;
        }
        return -1 * (value >>> 1);
    }
}


/** EBSP からエミュレーション防止バイト (00 00 03 の 03) を取り除いて RBSP を返す */
function ebspToRbsp(ebsp: Uint8Array): Uint8Array {
    const destination = new Uint8Array(ebsp.byteLength);
    let destination_index = 0;
    for (let index = 0; index < ebsp.byteLength; index++) {
        if (index >= 2 && ebsp[index] === 0x03 && ebsp[index - 1] === 0x00 && ebsp[index - 2] === 0x00) {
            continue;
        }
        destination[destination_index] = ebsp[index];
        destination_index++;
    }
    return new Uint8Array(destination.buffer, 0, destination_index);
}

/**
 * EBSP 内に禁止パターン (00 00 00 / 00 00 01 / 00 00 02) が無いことを検証する。
 * 書換えによって新たなスタートコードやエミュレーション防止違反が生じていないことの確認に使う。
 */
function isValidEbsp(ebsp: Uint8Array): boolean {
    for (let index = 2; index < ebsp.byteLength; index++) {
        if (ebsp[index - 2] === 0x00 && ebsp[index - 1] === 0x00 && ebsp[index] <= 0x02) {
            return false;
        }
    }
    return true;
}

/**
 * RBSP 基準のビット位置に 24bit の色信号を、EBSP のバイト列へその場で (長さを変えずに) 書き込む。
 * エミュレーション防止バイトは RBSP のビット位置に含まれないため、1ビットずつ EBSP 上の実位置へ対応付けて書く。
 * 書換え後に禁止パターンが生じる場合は null を返す (安全に書き換えられない入力)。
 */
function writeColorTupleToNalEbsp(
    nal_ebsp: Uint8Array,
    rbsp_bit_offset: number,
    tuple: KonomiTVBS4KFMP4ColorTuple,
): Uint8Array | null {
    const output = nal_ebsp.slice();
    // 24bit の書込み値 (colour_primaries / transfer_characteristics / matrix_coeffs の順)
    const value = ((tuple.colour_primaries & 0xFF) << 16) |
        ((tuple.transfer_characteristics & 0xFF) << 8) |
        (tuple.matrix_coeffs & 0xFF);
    let rbsp_bit_position = 0;
    let written_bits = 0;
    for (let byte_index = 0; byte_index < output.byteLength && written_bits < 24; byte_index++) {
        // エミュレーション防止バイトは RBSP のビット位置にカウントしない
        if (byte_index >= 2 && output[byte_index] === 0x03 &&
            output[byte_index - 1] === 0x00 && output[byte_index - 2] === 0x00) {
            continue;
        }
        for (let bit_in_byte = 0; bit_in_byte < 8 && written_bits < 24; bit_in_byte++) {
            if (rbsp_bit_position >= rbsp_bit_offset) {
                const source_bit = (value >>> (23 - written_bits)) & 1;
                const bit_mask = 1 << (7 - bit_in_byte);
                if (source_bit === 1) {
                    output[byte_index] |= bit_mask;
                } else {
                    output[byte_index] &= ~bit_mask;
                }
                written_bits++;
            }
            rbsp_bit_position++;
        }
    }
    // 24bit 全てを書き込める位置が無かった、または禁止パターンを作ってしまった場合は不安全
    if (written_bits < 24 || isValidEbsp(output) === false) {
        return null;
    }
    return output;
}


/** SPS / sequence header から読み取った色信号の情報 */
interface ParsedColorSignaling {
    color: KonomiTVBS4KFMP4ColorTuple;
    // RBSP (AV1 では OBU ペイロード) 先頭から見た colour_primaries のビット位置。
    // 色信号フィールド自体が存在しない場合は null (ビット列の挿入は行わない)
    color_bit_offset: number | null;
}

/**
 * H.264 (AVC) の SPS NAL (NAL ヘッダ1バイトを含む EBSP) を解析し、VUI の色信号とそのビット位置を返す。
 * 色信号の位置特定だけが目的なので、colour_description を読んだ時点で打ち切る。
 * mpegts.js fork の sps-parser.js の読み進め規則に基づく。
 */
function parseAvcSpsColor(nal_ebsp: Uint8Array): ParsedColorSignaling | null {
    const unspecified: ParsedColorSignaling = {
        color: {colour_primaries: 2, transfer_characteristics: 2, matrix_coeffs: 2},
        color_bit_offset: null,
    };
    try {
        const rbsp = ebspToRbsp(nal_ebsp);
        const reader = new BitReader(rbsp);
        reader.readByte();  // NAL ヘッダ (forbidden_zero_bit / nal_ref_idc / nal_unit_type)
        const profile_idc = reader.readByte();
        reader.readByte();  // constraint_set_flags + reserved_zero
        reader.readByte();  // level_idc
        reader.readUEG();  // seq_parameter_set_id
        let chroma_format_idc = 1;
        if ([100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135, 144].includes(profile_idc)) {
            chroma_format_idc = reader.readUEG();
            if (chroma_format_idc === 3) {
                reader.readBits(1);  // separate_colour_plane_flag
            }
            reader.readUEG();  // bit_depth_luma_minus8
            reader.readUEG();  // bit_depth_chroma_minus8
            reader.readBits(1);  // qpprime_y_zero_transform_bypass_flag
            if (reader.readBool()) {  // seq_scaling_matrix_present_flag
                const scaling_list_count = chroma_format_idc !== 3 ? 8 : 12;
                for (let index = 0; index < scaling_list_count; index++) {
                    if (reader.readBool()) {  // seq_scaling_list_present_flag
                        skipH264ScalingList(reader, index < 6 ? 16 : 64);
                    }
                }
            }
        }
        reader.readUEG();  // log2_max_frame_num_minus4
        const pic_order_cnt_type = reader.readUEG();
        if (pic_order_cnt_type === 0) {
            reader.readUEG();  // log2_max_pic_order_cnt_lsb_minus4
        } else if (pic_order_cnt_type === 1) {
            reader.readBits(1);  // delta_pic_order_always_zero_flag
            reader.readSEG();  // offset_for_non_ref_pic
            reader.readSEG();  // offset_for_top_to_bottom_field
            const num_ref_frames_in_pic_order_cnt_cycle = reader.readUEG();
            for (let index = 0; index < num_ref_frames_in_pic_order_cnt_cycle; index++) {
                reader.readSEG();  // offset_for_ref_frame
            }
        }
        reader.readUEG();  // max_num_ref_frames
        reader.readBits(1);  // gaps_in_frame_num_value_allowed_flag
        reader.readUEG();  // pic_width_in_mbs_minus1
        reader.readUEG();  // pic_height_in_map_units_minus1
        const frame_mbs_only_flag = reader.readBits(1);
        if (frame_mbs_only_flag === 0) {
            reader.readBits(1);  // mb_adaptive_frame_field_flag
        }
        reader.readBits(1);  // direct_8x8_inference_flag
        if (reader.readBool()) {  // frame_cropping_flag
            reader.readUEG();  // frame_crop_left_offset
            reader.readUEG();  // frame_crop_right_offset
            reader.readUEG();  // frame_crop_top_offset
            reader.readUEG();  // frame_crop_bottom_offset
        }
        if (reader.readBool() === false) {  // vui_parameters_present_flag
            return unspecified;
        }
        if (reader.readBool()) {  // aspect_ratio_info_present_flag
            const aspect_ratio_idc = reader.readByte();
            if (aspect_ratio_idc === 255) {  // EXTENDED_SAR
                reader.readBits(16);  // sar_width
                reader.readBits(16);  // sar_height
            }
        }
        if (reader.readBool()) {  // overscan_info_present_flag
            reader.readBool();  // overscan_appropriate_flag
        }
        if (reader.readBool() === false) {  // video_signal_type_present_flag
            return unspecified;
        }
        reader.readBits(3);  // video_format
        reader.readBool();  // video_full_range_flag
        if (reader.readBool() === false) {  // colour_description_present_flag
            return unspecified;
        }
        const color_bit_offset = reader.getBitsConsumed();
        const colour_primaries = reader.readByte();
        const transfer_characteristics = reader.readByte();
        const matrix_coeffs = reader.readByte();
        return {color: {colour_primaries, transfer_characteristics, matrix_coeffs}, color_bit_offset};
    } catch {
        return null;
    }
}

/** H.264 のスケーリングリストを読み飛ばす (seq_scaling_list_present_flag 内の要素) */
function skipH264ScalingList(reader: BitReader, size: number): void {
    let last_scale = 8;
    let next_scale = 8;
    for (let index = 0; index < size; index++) {
        if (next_scale !== 0) {
            const delta_scale = reader.readSEG();
            next_scale = (last_scale + delta_scale + 256) % 256;
        }
        last_scale = next_scale === 0 ? last_scale : next_scale;
    }
}

/**
 * H.265 (HEVC) の SPS NAL (NAL ヘッダ2バイトを含む EBSP) を解析し、VUI の色信号とそのビット位置を返す。
 * 色信号の位置特定だけが目的なので、colour_description を読んだ時点で打ち切る。
 * mpegts.js fork の h265-parser.js の読み進め規則に基づく。
 */
function parseHevcSpsColor(nal_ebsp: Uint8Array): ParsedColorSignaling | null {
    const unspecified: ParsedColorSignaling = {
        color: {colour_primaries: 2, transfer_characteristics: 2, matrix_coeffs: 2},
        color_bit_offset: null,
    };
    try {
        const rbsp = ebspToRbsp(nal_ebsp);
        const reader = new BitReader(rbsp);
        reader.readByte();  // NAL ヘッダ上位 (forbidden_zero_bit / nal_unit_type の前半)
        reader.readByte();  // NAL ヘッダ下位 (nuh_layer_id / temporal_id_plus1)
        reader.readBits(4);  // sps_video_parameter_set_id
        const max_sub_layers_minus1 = reader.readBits(3);
        reader.readBool();  // sps_temporal_id_nesting_flag
        // profile_tier_level
        reader.readBits(2);  // general_profile_space
        reader.readBool();  // general_tier_flag
        reader.readBits(5);  // general_profile_idc
        reader.readBits(32);  // general_profile_compatibility_flags
        reader.readBits(32);  // general_constraint_indicator_flags (上位32bit)
        reader.readBits(16);  // general_constraint_indicator_flags (下位16bit)
        reader.readByte();  // general_level_idc
        const sub_layer_profile_present_flags: boolean[] = [];
        const sub_layer_level_present_flags: boolean[] = [];
        for (let index = 0; index < max_sub_layers_minus1; index++) {
            sub_layer_profile_present_flags.push(reader.readBool());
            sub_layer_level_present_flags.push(reader.readBool());
        }
        if (max_sub_layers_minus1 > 0) {
            for (let index = max_sub_layers_minus1; index < 8; index++) {
                reader.readBits(2);  // reserved_zero_2bits
            }
        }
        for (let index = 0; index < max_sub_layers_minus1; index++) {
            if (sub_layer_profile_present_flags[index] === true) {
                reader.readByte();  // sub_layer_profile_space / tier_flag / profile_idc
                reader.readBits(32);  // sub_layer_profile_compatibility_flag
                reader.readBits(40);  // sub_layer_constraint_indicator_flags
            }
            if (sub_layer_level_present_flags[index] === true) {
                reader.readByte();  // sub_layer_level_idc
            }
        }
        reader.readUEG();  // sps_seq_parameter_set_id
        const chroma_format_idc = reader.readUEG();
        if (chroma_format_idc === 3) {
            reader.readBits(1);  // separate_colour_plane_flag
        }
        reader.readUEG();  // pic_width_in_luma_samples
        reader.readUEG();  // pic_height_in_luma_samples
        if (reader.readBool()) {  // conformance_window_flag
            reader.readUEG();  // conf_win_left_offset
            reader.readUEG();  // conf_win_right_offset
            reader.readUEG();  // conf_win_top_offset
            reader.readUEG();  // conf_win_bottom_offset
        }
        reader.readUEG();  // bit_depth_luma_minus8
        reader.readUEG();  // bit_depth_chroma_minus8
        const log2_max_pic_order_cnt_lsb_minus4 = reader.readUEG();
        const sub_layer_ordering_info_present_flag = reader.readBool();
        for (let index = sub_layer_ordering_info_present_flag === true ? 0 : max_sub_layers_minus1;
            index <= max_sub_layers_minus1; index++) {
            reader.readUEG();  // sps_max_dec_pic_buffering_minus1
            reader.readUEG();  // sps_max_num_reorder_pics
            reader.readUEG();  // sps_max_latency_increase_plus1
        }
        reader.readUEG();  // log2_min_luma_coding_block_size_minus3
        reader.readUEG();  // log2_diff_max_min_luma_coding_block_size
        reader.readUEG();  // log2_min_luma_transform_block_size_minus2
        reader.readUEG();  // log2_diff_max_min_luma_transform_block_size
        reader.readUEG();  // max_transform_hierarchy_depth_inter
        reader.readUEG();  // max_transform_hierarchy_depth_intra
        if (reader.readBool()) {  // scaling_list_enabled_flag
            if (reader.readBool()) {  // sps_scaling_list_data_present_flag
                skipH265ScalingListData(reader);
            }
        }
        reader.readBool();  // amp_enabled_flag
        reader.readBool();  // sample_adaptive_offset_enabled_flag
        if (reader.readBool()) {  // pcm_enabled_flag
            reader.readByte();  // pcm_sample_bit_depth_luma_minus1 / chroma_minus1
            reader.readUEG();  // log2_min_pcm_luma_coding_block_size_minus3
            reader.readUEG();  // log2_diff_max_min_pcm_luma_coding_block_size
            reader.readBool();  // pcm_loop_filter_disabled_flag
        }
        const num_short_term_ref_pic_sets = reader.readUEG();
        let num_delta_pocs = 0;
        for (let index = 0; index < num_short_term_ref_pic_sets; index++) {
            let inter_ref_pic_set_prediction_flag = false;
            if (index !== 0) {
                inter_ref_pic_set_prediction_flag = reader.readBool();
            }
            if (inter_ref_pic_set_prediction_flag === true) {
                if (index === num_short_term_ref_pic_sets) {
                    reader.readUEG();  // delta_idx_sps
                }
                reader.readBool();  // delta_rps_sign
                reader.readUEG();  // abs_delta_rps_minus1
                let next_num_delta_pocs = 0;
                for (let inner = 0; inner <= num_delta_pocs; inner++) {
                    const used_by_curr_pic_flag = reader.readBool();
                    let use_delta_flag = false;
                    if (used_by_curr_pic_flag === false) {
                        use_delta_flag = reader.readBool();
                    }
                    if (used_by_curr_pic_flag === true || use_delta_flag === true) {
                        next_num_delta_pocs++;
                    }
                }
                num_delta_pocs = next_num_delta_pocs;
            } else {
                const num_negative_pics = reader.readUEG();
                const num_positive_pics = reader.readUEG();
                num_delta_pocs = num_negative_pics + num_positive_pics;
                for (let inner = 0; inner < num_negative_pics; inner++) {
                    reader.readUEG();  // delta_poc_s0_minus1
                    reader.readBool();  // used_by_curr_pic_s0_flag
                }
                for (let inner = 0; inner < num_positive_pics; inner++) {
                    reader.readUEG();  // delta_poc_s1_minus1
                    reader.readBool();  // used_by_curr_pic_s1_flag
                }
            }
        }
        if (reader.readBool()) {  // long_term_ref_pics_present_flag
            const num_long_term_ref_pics_sps = reader.readUEG();
            for (let index = 0; index < num_long_term_ref_pics_sps; index++) {
                reader.readBits(log2_max_pic_order_cnt_lsb_minus4 + 4);  // lt_ref_pic_poc_lsb_sps
                reader.readBits(1);  // used_by_curr_pic_lt_sps_flag
            }
        }
        reader.readBool();  // sps_temporal_mvp_enabled_flag
        reader.readBool();  // strong_intra_smoothing_enabled_flag
        if (reader.readBool() === false) {  // vui_parameters_present_flag
            return unspecified;
        }
        if (reader.readBool()) {  // aspect_ratio_info_present_flag
            const aspect_ratio_idc = reader.readByte();
            if (aspect_ratio_idc === 255) {  // EXTENDED_SAR
                reader.readBits(16);  // sar_width
                reader.readBits(16);  // sar_height
            }
        }
        if (reader.readBool()) {  // overscan_info_present_flag
            reader.readBool();  // overscan_appropriate_flag
        }
        if (reader.readBool() === false) {  // video_signal_type_present_flag
            return unspecified;
        }
        reader.readBits(3);  // video_format
        reader.readBool();  // video_full_range_flag
        if (reader.readBool() === false) {  // colour_description_present_flag
            return unspecified;
        }
        const color_bit_offset = reader.getBitsConsumed();
        const colour_primaries = reader.readByte();
        const transfer_characteristics = reader.readByte();
        const matrix_coeffs = reader.readByte();
        return {color: {colour_primaries, transfer_characteristics, matrix_coeffs}, color_bit_offset};
    } catch {
        return null;
    }
}

/** H.265 の scaling_list_data() を読み飛ばす */
function skipH265ScalingListData(reader: BitReader): void {
    for (let size_id = 0; size_id < 4; size_id++) {
        for (let matrix_id = 0; matrix_id < (size_id === 3 ? 2 : 6); matrix_id++) {
            const scaling_list_pred_mode_flag = reader.readBool();
            if (scaling_list_pred_mode_flag === false) {
                reader.readUEG();  // scaling_list_pred_matrix_id_delta
            } else {
                const coef_num = Math.min(64, 1 << (4 + (size_id << 1)));
                if (size_id > 1) {
                    reader.readSEG();  // scaling_list_dc_coef_minus8
                }
                for (let index = 0; index < coef_num; index++) {
                    reader.readSEG();  // scaling_list_delta_coef
                }
            }
        }
    }
}

/**
 * AV1 の sequence header OBU のペイロードを解析し、色信号とそのビット位置を返す。
 * 色信号の位置特定だけが目的なので、matrix_coefficients を読んだ時点で打ち切る。
 * mpegts.js fork の av1-parser.ts の読み進め規則に基づく。
 */
function parseAv1SequenceHeaderColor(payload: Uint8Array): ParsedColorSignaling | null {
    const unspecified: ParsedColorSignaling = {
        color: {colour_primaries: 2, transfer_characteristics: 2, matrix_coeffs: 2},
        color_bit_offset: null,
    };
    try {
        const reader = new BitReader(payload);
        const seq_profile = reader.readBits(3);
        const still_picture = reader.readBool();
        const reduced_still_picture_header = reader.readBool();
        if (seq_profile > 2 || (reduced_still_picture_header === true && still_picture === false)) {
            return null;
        }
        if (reduced_still_picture_header === true) {
            reader.readBits(5);  // seq_level_idx[0]
        } else {
            let decoder_model_info_present_flag = false;
            let buffer_delay_length_minus_1 = 0;
            if (reader.readBool()) {  // timing_info_present_flag
                const num_units_in_display_tick = reader.readBits(32);
                const time_scale = reader.readBits(32);
                if (num_units_in_display_tick === 0 || time_scale === 0) {
                    return null;
                }
                if (reader.readBool()) {  // equal_picture_interval
                    reader.readUEG();  // num_ticks_per_picture_minus_1
                }
                decoder_model_info_present_flag = reader.readBool();
                if (decoder_model_info_present_flag === true) {
                    buffer_delay_length_minus_1 = reader.readBits(5);
                    reader.readBits(32);  // num_units_in_decoding_tick
                    reader.readBits(5);  // buffer_removal_time_length_minus_1
                    reader.readBits(5);  // frame_presentation_time_length_minus_1
                }
            }
            const initial_display_delay_present_flag = reader.readBool();
            const operating_points_cnt_minus_1 = reader.readBits(5);
            for (let index = 0; index <= operating_points_cnt_minus_1; index++) {
                reader.readBits(12);  // operating_point_idc
                const seq_level_idx = reader.readBits(5);
                if (seq_level_idx > 7) {
                    reader.readBits(1);  // seq_tier
                }
                if (decoder_model_info_present_flag === true) {
                    if (reader.readBool()) {  // decoder_model_present_for_this_op
                        reader.readBits(buffer_delay_length_minus_1 + 1);  // decoder_buffer_delay
                        reader.readBits(buffer_delay_length_minus_1 + 1);  // encoder_buffer_delay
                        reader.readBool();  // low_delay_mode_flag
                    }
                }
                if (initial_display_delay_present_flag === true) {
                    if (reader.readBool()) {  // initial_display_delay_present_for_this_op
                        reader.readBits(4);  // initial_display_delay_minus_1
                    }
                }
            }
        }
        const frame_width_bits_minus_1 = reader.readBits(4);
        const frame_height_bits_minus_1 = reader.readBits(4);
        reader.readBits(frame_width_bits_minus_1 + 1);  // max_frame_width_minus_1
        reader.readBits(frame_height_bits_minus_1 + 1);  // max_frame_height_minus_1
        if (reduced_still_picture_header === false) {
            if (reader.readBool()) {  // frame_id_numbers_present_flag
                reader.readBits(4);  // delta_frame_id_length_minus_2
                reader.readBits(3);  // additional_frame_id_length_minus_1
            }
        }
        reader.readBool();  // use_128x128_superblock
        reader.readBool();  // enable_filter_intra
        reader.readBool();  // enable_intra_edge_filter
        if (reduced_still_picture_header === false) {
            reader.readBool();  // enable_interintra_compound
            reader.readBool();  // enable_masked_compound
            reader.readBool();  // enable_warped_motion
            reader.readBool();  // enable_dual_filter
            const enable_order_hint = reader.readBool();
            if (enable_order_hint === true) {
                reader.readBool();  // enable_jnt_comp
                reader.readBool();  // enable_ref_frame_mvs
            }
            let seq_force_screen_content_tools = 2;  // SELECT_SCREEN_CONTENT_TOOLS
            if (reader.readBool()) {  // seq_choose_screen_content_tools
                seq_force_screen_content_tools = 2;
            } else {
                seq_force_screen_content_tools = reader.readBits(1);
            }
            // SELECT (2) の場合も seq_choose_integer_mv を読む (0 以外なら読むのが AV1 仕様)
            if (seq_force_screen_content_tools > 0) {
                if (reader.readBool()) {  // seq_choose_integer_mv
                    // seq_force_integer_mv = SELECT_INTEGER_MV (読み飛ばしだけでよい)
                } else {
                    reader.readBits(1);  // seq_force_integer_mv
                }
            }
            if (enable_order_hint === true) {
                reader.readBits(3);  // order_hint_bits_minus_1
            }
        }
        reader.readBool();  // enable_superres
        reader.readBool();  // enable_cdef
        reader.readBool();  // enable_restoration
        // color_config()
        const high_bitdepth = reader.readBool();
        let bit_depth = 8;
        if (seq_profile === 2 && high_bitdepth === true) {
            const twelve_bit = reader.readBool();
            bit_depth = twelve_bit === true ? 12 : 10;
        } else {
            bit_depth = high_bitdepth === true ? 10 : 8;
        }
        const mono_chrome = seq_profile === 1 ? false : reader.readBool();
        if (reader.readBool() === false) {  // color_description_present_flag
            return unspecified;
        }
        const color_bit_offset = reader.getBitsConsumed();
        const colour_primaries = reader.readByte();
        const transfer_characteristics = reader.readByte();
        const matrix_coeffs = reader.readByte();
        // mono_chrome / bit_depth はこの後の構文の解析には不要 (ここで打ち切る) だが、
        // 読み進め規則の整合のため変数として保持している
        void bit_depth;
        void mono_chrome;
        return {color: {colour_primaries, transfer_characteristics, matrix_coeffs}, color_bit_offset};
    } catch {
        return null;
    }
}

/**
 * AV1 OBU (obu_has_size_field=1 前提) のシーケンス内で sequence header OBU を探し、色信号を書き換える。
 * AV1 にはエミュレーション防止が無いため、ペイロードへ直接ビット書込みできる。
 * @returns 書換え結果。見つからない場合は rewritten=false、解析不能なら null (不安全)
 */
function rewriteAv1ObuSequenceColor(
    obus: Uint8Array,
    mode: KonomiTVBS4KFMP4ColorRewriteMode,
): {data: Uint8Array; detected: KonomiTVBS4KFMP4ColorTuple | null; rewritten: boolean} | null {
    let output: Uint8Array | null = null;
    let detected: KonomiTVBS4KFMP4ColorTuple | null = null;
    let rewritten = false;
    let offset = 0;
    while (offset < obus.byteLength) {
        const obu_start = offset;
        const header = obus[offset];
        if ((header & 0x80) !== 0) {  // obu_forbidden_bit
            return null;
        }
        const obu_type = (header >>> 3) & 0x0F;
        const has_extension = (header & 0x04) !== 0;
        const has_size_field = (header & 0x02) !== 0;
        offset++;
        if (has_extension === true) {
            offset++;  // obu_extension_header
        }
        if (has_size_field === false) {
            // AV1-ISOBMFF では obu_has_size_field=1 が必須。サイズが分からない OBU 列は安全に扱えない
            return null;
        }
        // LEB128 のサイズフィールド
        let payload_size = 0;
        let leb_shift = 0;
        let leb_done = false;
        for (let index = 0; index < 8; index++) {
            if (offset >= obus.byteLength) {
                return null;
            }
            const value = obus[offset++];
            payload_size += (value & 0x7F) * (2 ** leb_shift);
            leb_shift += 7;
            if ((value & 0x80) === 0) {
                leb_done = true;
                break;
            }
        }
        if (leb_done === false || offset + payload_size > obus.byteLength) {
            return null;
        }
        if (obu_type === 1) {  // OBU_SEQUENCE_HEADER
            const payload = obus.subarray(offset, offset + payload_size);
            const parsed = parseAv1SequenceHeaderColor(payload);
            if (parsed === null) {
                return null;
            }
            detected = parsed.color;
            const effective = resolveKonomiTVBS4KFMP4ColorRewrite(parsed.color, mode);
            if (parsed.color_bit_offset !== null && colorTuplesEqual(parsed.color, effective) === false) {
                if (output === null) {
                    output = obus.slice();
                }
                // AV1 のペイロードにはエミュレーション防止が無いので、ペイロード内のビット位置へ直接書く
                writeBitsToBytes(
                    output,
                    (offset - obu_start) * 8 + (parsed.color_bit_offset ?? 0),
                    24,
                    (effective.colour_primaries << 16) |
                        (effective.transfer_characteristics << 8) |
                        effective.matrix_coeffs,
                );
                rewritten = true;
            }
        }
        offset += payload_size;
    }
    return {data: output ?? obus, detected, rewritten};
}

/** バイト列の任意のビット位置へ値を書き込む (ビッグエンディアンのビット順) */
function writeBitsToBytes(bytes: Uint8Array, bit_offset: number, bit_count: number, value: number): void {
    for (let index = 0; index < bit_count; index++) {
        const position = bit_offset + index;
        const byte_index = position >> 3;
        const bit_in_byte = 7 - (position & 7);
        const bit = (value >>> (bit_count - 1 - index)) & 1;
        if (bit === 1) {
            bytes[byte_index] |= (1 << bit_in_byte);
        } else {
            bytes[byte_index] &= ~(1 << bit_in_byte);
        }
    }
}


/** MP4 ボックスの範囲情報 */
interface MP4BoxRange {
    type: string;
    // ボックスヘッダの先頭オフセット
    start: number;
    // コンテンツの先頭オフセット (ヘッダ直後)
    content_start: number;
    // ボックスの終端オフセット (排他)
    end: number;
}

/**
 * MP4 ボックス列を走査する。
 * @returns ボックスの配列。構造が不正な場合は null
 */
function parseMP4Boxes(data: Uint8Array, start: number, end: number): MP4BoxRange[] | null {
    const boxes: MP4BoxRange[] = [];
    let offset = start;
    while (offset + 8 <= end) {
        const size = new DataView(data.buffer, data.byteOffset + offset, 4).getUint32(0, false);
        const type = String.fromCharCode(data[offset + 4], data[offset + 5], data[offset + 6], data[offset + 7]);
        let header_size = 8;
        let box_end: number;
        if (size === 1) {
            // 64bit largesize
            if (offset + 16 > end) {
                return null;
            }
            const view = new DataView(data.buffer, data.byteOffset + offset + 8, 8);
            const large_size = view.getUint32(0, false) * (2 ** 32) + view.getUint32(4, false);
            header_size = 16;
            box_end = offset + large_size;
        } else if (size === 0) {
            // ファイル末尾まで続くボックス
            box_end = end;
        } else {
            box_end = offset + size;
        }
        if (box_end > end || box_end < offset + header_size) {
            return null;
        }
        boxes.push({type, start: offset, content_start: offset + header_size, end: box_end});
        offset = box_end;
    }
    if (offset !== end) {
        return null;
    }
    return boxes;
}

/** 指定したタイプの子ボックスを探す */
function findChildBox(data: Uint8Array, parent: MP4BoxRange, type: string): MP4BoxRange | null {
    const children = parseMP4Boxes(data, parent.content_start, parent.end);
    if (children === null) {
        return null;
    }
    return children.find((box) => box.type === type) ?? null;
}

/** 16bit の CICP 値を書き込む (colr ボックス用) */
function writeUint16ToBytes(bytes: Uint8Array, offset: number, value: number): void {
    bytes[offset] = (value >>> 8) & 0xFF;
    bytes[offset + 1] = value & 0xFF;
}


/**
 * サンプルエントリ内の colr (nclx) ボックスの色信号を、effective と一致しない場合だけ書き換える。
 * colr が無い・形式が nclx 以外・既に一致している場合は output をそのまま返す。
 */
function rewriteColrBox(
    data: Uint8Array,
    output: Uint8Array | null,
    entry: MP4BoxRange,
    effective: KonomiTVBS4KFMP4ColorTuple,
): Uint8Array | null {
    const children = parseMP4Boxes(data, entry.content_start + 78, entry.end);
    if (children === null) {
        return output;
    }
    for (const child of children) {
        if (child.type !== 'colr') {
            continue;
        }
        // nclx 形式 (colour_type 4バイト + 16bit x 3 + full_range 1bit) だけを扱う
        if (child.end - child.content_start < 10 ||
            String.fromCharCode(data[child.content_start], data[child.content_start + 1],
                data[child.content_start + 2], data[child.content_start + 3]) !== 'nclx') {
            continue;
        }
        const current: KonomiTVBS4KFMP4ColorTuple = {
            colour_primaries: (data[child.content_start + 4] << 8) | data[child.content_start + 5],
            transfer_characteristics: (data[child.content_start + 6] << 8) | data[child.content_start + 7],
            matrix_coeffs: (data[child.content_start + 8] << 8) | data[child.content_start + 9],
        };
        // 既に effective と一致している場合は書き換えない (未変更扱いを維持する)
        if (colorTuplesEqual(current, effective) === true) {
            continue;
        }
        if (output === null) {
            output = data.slice();
        }
        writeUint16ToBytes(output, child.content_start + 4, effective.colour_primaries);
        writeUint16ToBytes(output, child.content_start + 6, effective.transfer_characteristics);
        writeUint16ToBytes(output, child.content_start + 8, effective.matrix_coeffs);
    }
    return output;
}


/** AVC/HEVC の NAL 書換え結果 */
interface NalRewriteOutcome {
    data: Uint8Array;
    detected: KonomiTVBS4KFMP4ColorTuple | null;
    rewritten: boolean;
    unsafe: boolean;
}

/**
 * AVC (H.264) の SPS NAL を解析し、必要なら色信号を書き換える。
 * @param nal_ebsp NAL ヘッダを含む SPS の EBSP
 */
function rewriteAvcSpsNal(nal_ebsp: Uint8Array, mode: KonomiTVBS4KFMP4ColorRewriteMode): NalRewriteOutcome {
    const parsed = parseAvcSpsColor(nal_ebsp);
    if (parsed === null) {
        return {data: nal_ebsp, detected: null, rewritten: false, unsafe: true};
    }
    const effective = resolveKonomiTVBS4KFMP4ColorRewrite(parsed.color, mode);
    if (parsed.color_bit_offset === null || colorTuplesEqual(parsed.color, effective) === true) {
        return {data: nal_ebsp, detected: parsed.color, rewritten: false, unsafe: false};
    }
    const rewritten = writeColorTupleToNalEbsp(nal_ebsp, parsed.color_bit_offset, effective);
    if (rewritten === null) {
        return {data: nal_ebsp, detected: parsed.color, rewritten: false, unsafe: true};
    }
    return {data: rewritten, detected: parsed.color, rewritten: true, unsafe: false};
}

/**
 * HEVC (H.265) の SPS NAL を解析し、必要なら色信号を書き換える。
 * @param nal_ebsp NAL ヘッダ2バイトを含む SPS の EBSP
 */
function rewriteHevcSpsNal(nal_ebsp: Uint8Array, mode: KonomiTVBS4KFMP4ColorRewriteMode): NalRewriteOutcome {
    const parsed = parseHevcSpsColor(nal_ebsp);
    if (parsed === null) {
        return {data: nal_ebsp, detected: null, rewritten: false, unsafe: true};
    }
    const effective = resolveKonomiTVBS4KFMP4ColorRewrite(parsed.color, mode);
    if (parsed.color_bit_offset === null || colorTuplesEqual(parsed.color, effective) === true) {
        return {data: nal_ebsp, detected: parsed.color, rewritten: false, unsafe: false};
    }
    const rewritten = writeColorTupleToNalEbsp(nal_ebsp, parsed.color_bit_offset, effective);
    if (rewritten === null) {
        return {data: nal_ebsp, detected: parsed.color, rewritten: false, unsafe: true};
    }
    return {data: rewritten, detected: parsed.color, rewritten: true, unsafe: false};
}


/** 初期化セグメントの映像サンプルエントリ解析結果 */
interface SampleEntryScan {
    codec: 'AVC' | 'HEVC' | 'VP9' | 'AV1';
    nal_length_size: number;
    detected: KonomiTVBS4KFMP4ColorTuple | null;
    output: Uint8Array | null;
    rewritten: boolean;
    unsafe: boolean;
}

/**
 * avc1 / avc3 サンプルエントリ内の avcC を解析し、SPS の色信号を書き換える。
 */
function scanAvcSampleEntry(
    data: Uint8Array,
    entry: MP4BoxRange,
    mode: KonomiTVBS4KFMP4ColorRewriteMode,
): SampleEntryScan {
    const result: SampleEntryScan = {
        codec: 'AVC', nal_length_size: 4, detected: null, output: null, rewritten: false, unsafe: false,
    };
    const children = parseMP4Boxes(data, entry.content_start + 78, entry.end);
    if (children === null) {
        result.unsafe = true;
        return result;
    }
    const avcc = children.find((box) => box.type === 'avcC');
    if (avcc === undefined) {
        result.unsafe = true;
        return result;
    }
    const base = avcc.content_start;
    // AVCDecoderConfigurationRecord: version(1) profile(1) compat(1) level(1) lengthSizeMinusOne(1) numOfSPS(1)
    if (base + 7 > avcc.end) {
        result.unsafe = true;
        return result;
    }
    result.nal_length_size = (data[base + 4] & 0x03) + 1;
    const sps_count = data[base + 5] & 0x1F;
    let offset = base + 6;
    for (let index = 0; index < sps_count; index++) {
        if (offset + 2 > avcc.end) {
            result.unsafe = true;
            return result;
        }
        const sps_length = (data[offset] << 8) | data[offset + 1];
        offset += 2;
        if (offset + sps_length > avcc.end) {
            result.unsafe = true;
            return result;
        }
        const outcome = rewriteAvcSpsNal(data.subarray(offset, offset + sps_length), mode);
        if (outcome.unsafe === true) {
            result.unsafe = true;
            return result;
        }
        if (outcome.detected !== null && result.detected === null) {
            result.detected = outcome.detected;
        }
        if (outcome.rewritten === true) {
            if (result.output === null) {
                result.output = data.slice();
            }
            result.output.set(outcome.data, offset);
            result.rewritten = true;
        }
        offset += sps_length;
    }
    return result;
}

/**
 * hvc1 / hev1 サンプルエントリ内の hvcC を解析し、SPS の色信号を書き換える。
 */
function scanHevcSampleEntry(
    data: Uint8Array,
    entry: MP4BoxRange,
    mode: KonomiTVBS4KFMP4ColorRewriteMode,
): SampleEntryScan {
    const result: SampleEntryScan = {
        codec: 'HEVC', nal_length_size: 4, detected: null, output: null, rewritten: false, unsafe: false,
    };
    const children = parseMP4Boxes(data, entry.content_start + 78, entry.end);
    if (children === null) {
        result.unsafe = true;
        return result;
    }
    const hvcc = children.find((box) => box.type === 'hvcC');
    if (hvcc === undefined) {
        result.unsafe = true;
        return result;
    }
    const base = hvcc.content_start;
    // HEVCDecoderConfigurationRecord は固定 23 バイト + NAL 配列
    if (base + 23 > hvcc.end) {
        result.unsafe = true;
        return result;
    }
    result.nal_length_size = (data[base + 21] & 0x03) + 1;
    const array_count = data[base + 22];
    let offset = base + 23;
    for (let array_index = 0; array_index < array_count; array_index++) {
        if (offset + 3 > hvcc.end) {
            result.unsafe = true;
            return result;
        }
        const nal_type = data[offset] & 0x3F;
        const nal_count = (data[offset + 1] << 8) | data[offset + 2];
        offset += 3;
        for (let nal_index = 0; nal_index < nal_count; nal_index++) {
            if (offset + 2 > hvcc.end) {
                result.unsafe = true;
                return result;
            }
            const nal_length = (data[offset] << 8) | data[offset + 1];
            offset += 2;
            if (offset + nal_length > hvcc.end) {
                result.unsafe = true;
                return result;
            }
            // SPS (NAL タイプ 33) だけが色信号を持つ
            if (nal_type === 33) {
                const outcome = rewriteHevcSpsNal(data.subarray(offset, offset + nal_length), mode);
                if (outcome.unsafe === true) {
                    result.unsafe = true;
                    return result;
                }
                if (outcome.detected !== null && result.detected === null) {
                    result.detected = outcome.detected;
                }
                if (outcome.rewritten === true) {
                    if (result.output === null) {
                        result.output = data.slice();
                    }
                    result.output.set(outcome.data, offset);
                    result.rewritten = true;
                }
            }
            offset += nal_length;
        }
    }
    return result;
}

/**
 * vp09 サンプルエントリ内の vpcC を解析し、色信号を書き換える。
 * VP9 の転送特性はコンテナのシグナリングだけが担う (ビットストリームは色域しか通知しない) ため、
 * vpcC と colr の書換えだけで完結する。
 */
function scanVp9SampleEntry(
    data: Uint8Array,
    entry: MP4BoxRange,
    mode: KonomiTVBS4KFMP4ColorRewriteMode,
): SampleEntryScan {
    const result: SampleEntryScan = {
        codec: 'VP9', nal_length_size: 0, detected: null, output: null, rewritten: false, unsafe: false,
    };
    const children = parseMP4Boxes(data, entry.content_start + 78, entry.end);
    if (children === null) {
        result.unsafe = true;
        return result;
    }
    const vpcc = children.find((box) => box.type === 'vpcC');
    if (vpcc === undefined) {
        result.unsafe = true;
        return result;
    }
    const base = vpcc.content_start;
    // version(1) flags(3) profile(1) level(1) bitDepth/chroma/fullRange(1) colourPrimaries(1) transfer(1) matrix(1)
    if (base + 10 > vpcc.end) {
        result.unsafe = true;
        return result;
    }
    const detected: KonomiTVBS4KFMP4ColorTuple = {
        colour_primaries: data[base + 7],
        transfer_characteristics: data[base + 8],
        matrix_coeffs: data[base + 9],
    };
    result.detected = detected;
    const effective = resolveKonomiTVBS4KFMP4ColorRewrite(detected, mode);
    if (colorTuplesEqual(detected, effective) === false) {
        result.output = data.slice();
        result.output[base + 7] = effective.colour_primaries;
        result.output[base + 8] = effective.transfer_characteristics;
        result.output[base + 9] = effective.matrix_coeffs;
        result.rewritten = true;
    }
    return result;
}

/**
 * av01 サンプルエントリ内の av1C を解析し、configOBUs 内の sequence header OBU の色信号を書き換える。
 */
function scanAv1SampleEntry(
    data: Uint8Array,
    entry: MP4BoxRange,
    mode: KonomiTVBS4KFMP4ColorRewriteMode,
): SampleEntryScan {
    const result: SampleEntryScan = {
        codec: 'AV1', nal_length_size: 0, detected: null, output: null, rewritten: false, unsafe: false,
    };
    const children = parseMP4Boxes(data, entry.content_start + 78, entry.end);
    if (children === null) {
        result.unsafe = true;
        return result;
    }
    const av1c = children.find((box) => box.type === 'av1C');
    if (av1c === undefined) {
        result.unsafe = true;
        return result;
    }
    // av1C の先頭 4 バイト (marker/version/profile/level/tier/bitdepth/subsampling/delay) の後に configOBUs が続く
    if (av1c.content_start + 4 > av1c.end) {
        result.unsafe = true;
        return result;
    }
    const obus_start = av1c.content_start + 4;
    if (obus_start === av1c.end) {
        // configOBUs が空の av1C (in-band の sequence header のみ) は初期化セグメントからは検出できない
        return result;
    }
    const outcome = rewriteAv1ObuSequenceColor(data.subarray(obus_start, av1c.end), mode);
    if (outcome === null) {
        result.unsafe = true;
        return result;
    }
    result.detected = outcome.detected;
    if (outcome.rewritten === true) {
        result.output = data.slice();
        result.output.set(outcome.data, obus_start);
        result.rewritten = true;
    }
    return result;
}


/**
 * 録画 fMP4 の初期化セグメントを解析し、必要なら色信号を書き換える。
 * @param data 初期化セグメントのバイト列
 * @param mode 書換えモード
 * @returns 書換え結果
 */
export function rewriteKonomiTVBS4KFMP4InitSegment(
    data: Uint8Array,
    mode: KonomiTVBS4KFMP4ColorRewriteMode,
): KonomiTVBS4KFMP4RewriteResult {
    const failure: KonomiTVBS4KFMP4RewriteResult = {
        data, detected: null, rewritten: false, unsafe: true, video_state: null,
    };
    const top_boxes = parseMP4Boxes(data, 0, data.byteLength);
    if (top_boxes === null) {
        return failure;
    }
    const moov = top_boxes.find((box) => box.type === 'moov');
    if (moov === undefined) {
        return failure;
    }
    const traks = parseMP4Boxes(data, moov.content_start, moov.end);
    if (traks === null) {
        return failure;
    }
    let output: Uint8Array | null = null;
    let rewritten = false;
    let detected: KonomiTVBS4KFMP4ColorTuple | null = null;
    let video_state: KonomiTVBS4KFMP4VideoColorState | null = null;
    for (const trak of traks.filter((box) => box.type === 'trak')) {
        const mdia = findChildBox(data, trak, 'mdia');
        const minf = mdia !== null ? findChildBox(data, mdia, 'minf') : null;
        const stbl = minf !== null ? findChildBox(data, minf, 'stbl') : null;
        const stsd = stbl !== null ? findChildBox(data, stbl, 'stsd') : null;
        if (stsd === null) {
            continue;
        }
        // stsd は version/flags(4) + entry_count(4) の後にサンプルエントリが続く
        if (stsd.content_start + 8 > stsd.end) {
            return failure;
        }
        const entries = parseMP4Boxes(data, stsd.content_start + 8, stsd.end);
        if (entries === null) {
            return failure;
        }
        for (const entry of entries) {
            let scan: SampleEntryScan | null = null;
            if (entry.type === 'avc1' || entry.type === 'avc3') {
                scan = scanAvcSampleEntry(data, entry, mode);
            } else if (entry.type === 'hvc1' || entry.type === 'hev1') {
                scan = scanHevcSampleEntry(data, entry, mode);
            } else if (entry.type === 'vp09') {
                scan = scanVp9SampleEntry(data, entry, mode);
            } else if (entry.type === 'av01') {
                scan = scanAv1SampleEntry(data, entry, mode);
            } else {
                // 音声などの映像以外のエントリは対象外
                continue;
            }
            if (scan.unsafe === true) {
                return failure;
            }
            // 検出した色信号と映像状態は最初の映像エントリのものを採用する。
            // avc3 / hev1 / 空の av1C のように初期化セグメントに色信号を持たない場合でも、
            // in-band の色信号をメディアセグメント側で処理できるよう codec と NAL 長は保持する。
            if (video_state === null) {
                detected = scan.detected;
                video_state = {
                    codec: scan.codec,
                    nal_length_size: scan.nal_length_size,
                    original: scan.detected ?? {colour_primaries: 2, transfer_characteristics: 2, matrix_coeffs: 2},
                };
            }
            if (scan.rewritten === true && scan.output !== null) {
                // サンプルエントリのコーデック設定を書き換えたバージョンを基点にする
                output = scan.output;
                rewritten = true;
            }
            // colr ボックスは、コーデック設定の信号を実際に書き換えた場合だけ同じ値へ揃える。
            // 書換え対象外 (非 HDR・素通し) やコーデック側が unspecified の場合に colr の実値を壊さないための条件
            if (scan.detected !== null &&
                colorTuplesEqual(
                    scan.detected,
                    resolveKonomiTVBS4KFMP4ColorRewrite(scan.detected, mode),
                ) === false) {
                const effective = resolveKonomiTVBS4KFMP4ColorRewrite(scan.detected, mode);
                const base_data = output ?? data;
                const colr_output = rewriteColrBox(base_data, output, entry, effective);
                if (colr_output !== output) {
                    output = colr_output;
                    rewritten = true;
                }
            }
        }
    }
    return {
        data: output ?? data,
        detected,
        rewritten,
        unsafe: false,
        video_state,
    };
}


/**
 * 録画 fMP4 のメディアセグメントを解析し、in-band の色信号 (SPS NAL / AV1 sequence header OBU) を
 * 必要なら書き換える。初期化セグメントで得た映像状態が必要。
 * @param data メディアセグメントのバイト列
 * @param mode 書換えモード
 * @param video_state 初期化セグメントから確定した映像トラックの状態
 * @returns 書換え結果
 */
export function rewriteKonomiTVBS4KFMP4MediaSegment(
    data: Uint8Array,
    mode: KonomiTVBS4KFMP4ColorRewriteMode,
    video_state: KonomiTVBS4KFMP4VideoColorState,
): KonomiTVBS4KFMP4RewriteResult {
    const unchanged: KonomiTVBS4KFMP4RewriteResult = {
        data, detected: null, rewritten: false, unsafe: false, video_state: null,
    };
    const failure: KonomiTVBS4KFMP4RewriteResult = {
        data, detected: null, rewritten: false, unsafe: true, video_state: null,
    };
    // 書換えが不要なら走査自体を行わない (VP9 は in-band の転送特性を持たない)
    if (mode === 'None' || video_state.codec === 'VP9') {
        return unchanged;
    }
    const top_boxes = parseMP4Boxes(data, 0, data.byteLength);
    if (top_boxes === null) {
        return failure;
    }
    const moof = top_boxes.find((box) => box.type === 'moof');
    const mdat = top_boxes.find((box) => box.type === 'mdat');
    if (moof === undefined || mdat === undefined) {
        // styp だけのセグメントなどは無害だが、moof/mdat を欠くメディアセグメントは扱えない
        return failure;
    }
    const trafs = (() => {
        const moof_children = parseMP4Boxes(data, moof.content_start, moof.end);
        return moof_children?.filter((box) => box.type === 'traf') ?? null;
    })();
    if (trafs === null) {
        return failure;
    }
    let output: Uint8Array | null = null;
    let rewritten = false;
    let detected: KonomiTVBS4KFMP4ColorTuple | null = null;
    for (const traf of trafs) {
        // 1ファイル1映像トラックの録画 fMP4 では最初の traf が映像
        const tfhd = findChildBox(data, traf, 'tfhd');
        if (tfhd === null || tfhd.content_start + 4 > tfhd.end) {
            return failure;
        }
        const tfhd_flags = (data[tfhd.content_start + 1] << 16) |
            (data[tfhd.content_start + 2] << 8) | data[tfhd.content_start + 3];
        let tfhd_offset = tfhd.content_start + 4 + 4;  // version/flags + track_ID
        if ((tfhd_flags & 0x000001) !== 0) {
            tfhd_offset += 8;  // base_data_offset (本実装では default_base_moof 前提のため参照しない)
        }
        if ((tfhd_flags & 0x000002) !== 0) {
            tfhd_offset += 4;  // sample_description_index
        }
        if ((tfhd_flags & 0x000008) !== 0) {
            tfhd_offset += 4;  // default_sample_duration
        }
        let default_sample_size = 0;
        if ((tfhd_flags & 0x000010) !== 0) {
            if (tfhd_offset + 4 > tfhd.end) {
                return failure;
            }
            default_sample_size = new DataView(data.buffer, data.byteOffset + tfhd_offset, 4).getUint32(0, false);
            tfhd_offset += 4;
        }
        const truns = (() => {
            const children = parseMP4Boxes(data, traf.content_start, traf.end);
            return children?.filter((box) => box.type === 'trun') ?? null;
        })();
        if (truns === null) {
            return failure;
        }
        let current_data_offset = 0;
        let is_first_trun = true;
        for (const trun of truns) {
            if (trun.content_start + 8 > trun.end) {
                return failure;
            }
            const trun_flags = (data[trun.content_start + 1] << 16) |
                (data[trun.content_start + 2] << 8) | data[trun.content_start + 3];
            const sample_count = new DataView(data.buffer, data.byteOffset + trun.content_start + 4, 4)
                .getUint32(0, false);
            let trun_offset = trun.content_start + 8;
            let data_offset: number | null = null;
            if ((trun_flags & 0x000001) !== 0) {
                if (trun_offset + 4 > trun.end) {
                    return failure;
                }
                data_offset = new DataView(data.buffer, data.byteOffset + trun_offset, 4).getInt32(0, false);
                trun_offset += 4;
            }
            if ((trun_flags & 0x000004) !== 0) {
                trun_offset += 4;  // first_sample_flags
            }
            const has_sample_duration = (trun_flags & 0x000100) !== 0;
            const has_sample_size = (trun_flags & 0x000200) !== 0;
            const has_sample_flags = (trun_flags & 0x000400) !== 0;
            const has_sample_cto = (trun_flags & 0x000800) !== 0;
            if (has_sample_size === false && default_sample_size === 0) {
                return failure;
            }
            // data_offset は moof 先頭からの相対 (default_base_moof)。省略時は直前の trun の末尾に続く
            if (data_offset !== null) {
                current_data_offset = moof.start + data_offset;
            } else if (is_first_trun === true) {
                // data_offset を持たない最初の trun の位置を確定できないため安全側に倒す
                return failure;
            }
            is_first_trun = false;
            for (let sample_index = 0; sample_index < sample_count; sample_index++) {
                if (has_sample_duration === true) {
                    trun_offset += 4;
                }
                let sample_size = default_sample_size;
                if (has_sample_size === true) {
                    if (trun_offset + 4 > trun.end) {
                        return failure;
                    }
                    sample_size = new DataView(data.buffer, data.byteOffset + trun_offset, 4).getUint32(0, false);
                    trun_offset += 4;
                }
                if (has_sample_flags === true) {
                    trun_offset += 4;
                }
                if (has_sample_cto === true) {
                    trun_offset += 4;
                }
                if (sample_size === 0) {
                    continue;
                }
                if (current_data_offset + sample_size > mdat.end) {
                    return failure;
                }
                const sample_start = current_data_offset;
                current_data_offset += sample_size;
                // サンプル内の in-band 色信号を書き換える
                const outcome = rewriteSampleInBandColor(
                    output ?? data,
                    sample_start,
                    sample_size,
                    mode,
                    video_state,
                );
                if (outcome.unsafe === true) {
                    return failure;
                }
                if (detected === null && outcome.detected !== null) {
                    detected = outcome.detected;
                }
                if (outcome.rewritten === true) {
                    output = outcome.data;
                    rewritten = true;
                }
            }
        }
    }
    return {
        data: output ?? data,
        detected,
        rewritten,
        unsafe: false,
        video_state: null,
    };
}

/** サンプル内の in-band 色信号書換え結果 */
interface InBandRewriteOutcome {
    data: Uint8Array;
    // サンプル内の SPS / sequence header から検出した色信号 (見つからなければ null)
    detected: KonomiTVBS4KFMP4ColorTuple | null;
    rewritten: boolean;
    unsafe: boolean;
}

/**
 * 1サンプル内の in-band 色信号を書き換える。
 * AVC / HEVC は length-prefixed NAL から SPS を探し、AV1 は OBU 列から sequence header を探す。
 */
function rewriteSampleInBandColor(
    base_data: Uint8Array,
    sample_start: number,
    sample_size: number,
    mode: KonomiTVBS4KFMP4ColorRewriteMode,
    video_state: KonomiTVBS4KFMP4VideoColorState,
): InBandRewriteOutcome {
    const sample_end = sample_start + sample_size;
    if (video_state.codec === 'AV1') {
        const outcome = rewriteAv1ObuSequenceColor(
            base_data.subarray(sample_start, sample_end),
            mode,
        );
        if (outcome === null) {
            return {data: base_data, detected: null, rewritten: false, unsafe: true};
        }
        if (outcome.rewritten === true) {
            const output = base_data.slice();
            output.set(outcome.data, sample_start);
            return {data: output, detected: outcome.detected, rewritten: true, unsafe: false};
        }
        return {data: base_data, detected: outcome.detected, rewritten: false, unsafe: false};
    }
    // AVC / HEVC の length-prefixed NAL を走査する
    const nal_length_size = video_state.nal_length_size;
    if (nal_length_size !== 1 && nal_length_size !== 2 && nal_length_size !== 4) {
        return {data: base_data, detected: null, rewritten: false, unsafe: true};
    }
    let output: Uint8Array | null = null;
    let detected: KonomiTVBS4KFMP4ColorTuple | null = null;
    let offset = sample_start;
    while (offset < sample_end) {
        if (offset + nal_length_size > sample_end) {
            return {data: base_data, detected, rewritten: output !== null, unsafe: true};
        }
        let nal_length = 0;
        for (let index = 0; index < nal_length_size; index++) {
            nal_length = nal_length * 256 + base_data[offset + index];
        }
        offset += nal_length_size;
        if (nal_length <= 0 || offset + nal_length > sample_end) {
            return {data: base_data, detected, rewritten: output !== null, unsafe: true};
        }
        const nal_header = base_data[offset];
        const is_sps = video_state.codec === 'AVC' ?
            (nal_header & 0x1F) === 7 :
            ((nal_header >>> 1) & 0x3F) === 33;
        if (is_sps === true) {
            const source = output ?? base_data;
            const outcome = video_state.codec === 'AVC' ?
                rewriteAvcSpsNal(source.subarray(offset, offset + nal_length), mode) :
                rewriteHevcSpsNal(source.subarray(offset, offset + nal_length), mode);
            if (outcome.unsafe === true) {
                return {data: base_data, detected, rewritten: output !== null, unsafe: true};
            }
            if (detected === null && outcome.detected !== null) {
                detected = outcome.detected;
            }
            if (outcome.rewritten === true) {
                if (output === null) {
                    output = base_data.slice();
                }
                output.set(outcome.data, offset);
            }
        }
        offset += nal_length;
    }
    return {data: output ?? base_data, detected, rewritten: output !== null, unsafe: false};
}
