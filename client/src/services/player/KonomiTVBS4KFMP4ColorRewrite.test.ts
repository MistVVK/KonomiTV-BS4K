
import { describe, expect, it } from 'vitest';

import {
    createKonomiTVBS4KColorRewriteSession,
} from '@/services/player/KonomiTVBS4KColorRewriteLoader';
import {
    CICP_BT709_PRIMARIES,
    CICP_HLG_TRANSFER,
    CICP_PQ_TRANSFER,
    CICP_SRGB_TRANSFER,
    rewriteKonomiTVBS4KFMP4InitSegment,
    rewriteKonomiTVBS4KFMP4MediaSegment,
    type KonomiTVBS4KFMP4VideoColorState,
} from '@/services/player/KonomiTVBS4KFMP4ColorRewrite';
import { resolveKonomiTVBS4KHdrOutput } from '@/services/player/KonomiTVBS4KHdrPolicy';


/**
 * テスト用のビットライター。Exp-Golomb (ue/se) と任意ビット数の書き込みを扱う。
 * 最後に rbsp_trailing_bits 相当の停止ビットを付けてバイト列へ確定する。
 */
class BitWriter {

    private bits: number[] = [];

    public writeBits(value: number, count: number): void {
        for (let index = count - 1; index >= 0; index--) {
            this.bits.push((value >>> index) & 1);
        }
    }

    public writeUEG(value: number): void {
        const code_num = value + 1;
        const bit_length = Math.floor(Math.log2(code_num)) + 1;
        for (let index = 0; index < bit_length - 1; index++) {
            this.bits.push(0);
        }
        this.writeBits(code_num, bit_length);
    }

    public writeSEG(value: number): void {
        this.writeUEG(value <= 0 ? -2 * value : 2 * value - 1);
    }

    public toBytes(): Uint8Array {
        // rbsp_trailing_bits: 停止ビット 1 + 0 詰め
        this.bits.push(1);
        while (this.bits.length % 8 !== 0) {
            this.bits.push(0);
        }
        const output = new Uint8Array(this.bits.length / 8);
        for (let index = 0; index < output.byteLength; index++) {
            let value = 0;
            for (let bit = 0; bit < 8; bit++) {
                value = (value << 1) | this.bits[index * 8 + bit];
            }
            output[index] = value;
        }
        return output;
    }
}

/** RBSP にエミュレーション防止バイトを挿入して EBSP にする (実在の SPS NAL と同じ形式にする) */
function rbspToEbsp(rbsp: Uint8Array): Uint8Array {
    const output: number[] = [];
    let zero_count = 0;
    for (const value of rbsp) {
        if (zero_count === 2 && value <= 3) {
            output.push(3);
            zero_count = 0;
        }
        output.push(value);
        zero_count = value === 0 ? zero_count + 1 : 0;
    }
    return Uint8Array.from(output);
}

/** MP4 ボックスを組み立てる */
function box(type: string, ...contents: Uint8Array[]): Uint8Array {
    const content_length = contents.reduce((sum, content) => sum + content.byteLength, 0);
    const output = new Uint8Array(8 + content_length);
    new DataView(output.buffer).setUint32(0, 8 + content_length, false);
    for (let index = 0; index < 4; index++) {
        output[4 + index] = type.charCodeAt(index);
    }
    let offset = 8;
    for (const content of contents) {
        output.set(content, offset);
        offset += content.byteLength;
    }
    return output;
}

function concat(...parts: Uint8Array[]): Uint8Array {
    const output = new Uint8Array(parts.reduce((sum, part) => sum + part.byteLength, 0));
    let offset = 0;
    for (const part of parts) {
        output.set(part, offset);
        offset += part.byteLength;
    }
    return output;
}

/** VisualSampleEntry の固定 78 バイト (中身は書換え対象外なので 0x01 埋めでよい) */
function visualSampleEntryHeader(): Uint8Array {
    return new Uint8Array(78).fill(1);
}

/** colr (nclx) ボックスを組み立てる */
function colrBox(primaries: number, transfer: number, matrix: number): Uint8Array {
    const content = new Uint8Array(11);
    content.set([0x6E, 0x63, 0x6C, 0x78], 0);  // 'nclx'
    new DataView(content.buffer).setUint16(4, primaries, false);
    new DataView(content.buffer).setUint16(6, transfer, false);
    new DataView(content.buffer).setUint16(8, matrix, false);
    return box('colr', content);
}

/** stsd (映像サンプルエントリ1件) から moov までを組み立てる */
function moovWithSampleEntry(entry: Uint8Array): Uint8Array {
    const stsd_header = new Uint8Array(8);  // version/flags + entry_count=1
    new DataView(stsd_header.buffer).setUint32(4, 1, false);
    return box('moov', box('trak', box('mdia', box('minf', box('stbl', box('stsd', stsd_header, entry))))));
}

function ftypBox(): Uint8Array {
    return box('ftyp', new Uint8Array([0x69, 0x73, 0x6F, 0x6D, 0, 0, 0, 1, 0x69, 0x73, 0x6F, 0x6D]));  // isom
}


/** HLG (BT.2020) を通知する H.264 High10 SPS NAL (NAL ヘッダ 0x67 付き) */
function buildAvcSps(primaries: number, transfer: number, matrix: number): Uint8Array {
    const writer = new BitWriter();
    writer.writeBits(100, 8);  // profile_idc (High10)
    writer.writeBits(0, 8);  // constraint_set_flags
    writer.writeBits(51, 8);  // level_idc
    writer.writeUEG(0);  // seq_parameter_set_id
    writer.writeUEG(1);  // chroma_format_idc (4:2:0)
    writer.writeUEG(2);  // bit_depth_luma_minus8 (10bit)
    writer.writeUEG(2);  // bit_depth_chroma_minus8
    writer.writeBits(0, 1);  // qpprime_y_zero_transform_bypass_flag
    writer.writeBits(0, 1);  // seq_scaling_matrix_present_flag
    writer.writeUEG(0);  // log2_max_frame_num_minus4
    writer.writeUEG(0);  // pic_order_cnt_type
    writer.writeUEG(0);  // log2_max_pic_order_cnt_lsb_minus4
    writer.writeUEG(1);  // max_num_ref_frames
    writer.writeBits(0, 1);  // gaps_in_frame_num_value_allowed_flag
    writer.writeUEG(239);  // pic_width_in_mbs_minus1 (3840)
    writer.writeUEG(134);  // pic_height_in_map_units_minus1 (2160)
    writer.writeBits(1, 1);  // frame_mbs_only_flag
    writer.writeBits(1, 1);  // direct_8x8_inference_flag
    writer.writeBits(0, 1);  // frame_cropping_flag
    writer.writeBits(1, 1);  // vui_parameters_present_flag
    writer.writeBits(0, 1);  // aspect_ratio_info_present_flag
    writer.writeBits(0, 1);  // overscan_info_present_flag
    writer.writeBits(1, 1);  // video_signal_type_present_flag
    writer.writeBits(5, 3);  // video_format
    writer.writeBits(0, 1);  // video_full_range_flag
    writer.writeBits(1, 1);  // colour_description_present_flag
    writer.writeBits(primaries, 8);
    writer.writeBits(transfer, 8);
    writer.writeBits(matrix, 8);
    return concat(new Uint8Array([0x67]), rbspToEbsp(writer.toBytes()));
}

/** HLG/PQ (BT.2020) を通知する H.265 Main10 SPS NAL (NAL ヘッダ 0x42 0x01 付き) */
function buildHevcSps(primaries: number, transfer: number, matrix: number): Uint8Array {
    const writer = new BitWriter();
    writer.writeBits(0, 4);  // sps_video_parameter_set_id
    writer.writeBits(0, 3);  // sps_max_sub_layers_minus1
    writer.writeBits(1, 1);  // sps_temporal_id_nesting_flag
    // profile_tier_level
    writer.writeBits(0, 2);  // general_profile_space
    writer.writeBits(0, 1);  // general_tier_flag
    writer.writeBits(2, 5);  // general_profile_idc (Main10)
    writer.writeBits(0x60000000, 32);  // general_profile_compatibility_flags
    writer.writeBits(0, 32);  // general_constraint_indicator_flags (上位)
    writer.writeBits(0, 16);  // general_constraint_indicator_flags (下位)
    writer.writeBits(153, 8);  // general_level_idc
    writer.writeUEG(0);  // sps_seq_parameter_set_id
    writer.writeUEG(1);  // chroma_format_idc (4:2:0)
    writer.writeUEG(3839);  // pic_width_in_luma_samples
    writer.writeUEG(2159);  // pic_height_in_luma_samples
    writer.writeBits(0, 1);  // conformance_window_flag
    writer.writeUEG(2);  // bit_depth_luma_minus8 (10bit)
    writer.writeUEG(2);  // bit_depth_chroma_minus8
    writer.writeUEG(4);  // log2_max_pic_order_cnt_lsb_minus4
    writer.writeBits(0, 1);  // sub_layer_ordering_info_present_flag
    writer.writeUEG(0);  // sps_max_dec_pic_buffering_minus1
    writer.writeUEG(0);  // sps_max_num_reorder_pics
    writer.writeUEG(0);  // sps_max_latency_increase_plus1
    writer.writeUEG(0);  // log2_min_luma_coding_block_size_minus3
    writer.writeUEG(3);  // log2_diff_max_min_luma_coding_block_size
    writer.writeUEG(0);  // log2_min_luma_transform_block_size_minus2
    writer.writeUEG(3);  // log2_diff_max_min_luma_transform_block_size
    writer.writeUEG(0);  // max_transform_hierarchy_depth_inter
    writer.writeUEG(0);  // max_transform_hierarchy_depth_intra
    writer.writeBits(0, 1);  // scaling_list_enabled_flag
    writer.writeBits(1, 1);  // amp_enabled_flag
    writer.writeBits(1, 1);  // sample_adaptive_offset_enabled_flag
    writer.writeBits(0, 1);  // pcm_enabled_flag
    writer.writeUEG(0);  // num_short_term_ref_pic_sets
    writer.writeBits(0, 1);  // long_term_ref_pics_present_flag
    writer.writeBits(1, 1);  // sps_temporal_mvp_enabled_flag
    writer.writeBits(1, 1);  // strong_intra_smoothing_enabled_flag
    writer.writeBits(1, 1);  // vui_parameters_present_flag
    writer.writeBits(0, 1);  // aspect_ratio_info_present_flag
    writer.writeBits(0, 1);  // overscan_info_present_flag
    writer.writeBits(1, 1);  // video_signal_type_present_flag
    writer.writeBits(5, 3);  // video_format
    writer.writeBits(0, 1);  // video_full_range_flag
    writer.writeBits(1, 1);  // colour_description_present_flag
    writer.writeBits(primaries, 8);
    writer.writeBits(transfer, 8);
    writer.writeBits(matrix, 8);
    return concat(new Uint8Array([0x42, 0x01]), rbspToEbsp(writer.toBytes()));
}

/** AV1 の sequence header OBU (obu_has_size_field=1) */
function buildAv1SequenceHeaderObu(primaries: number, transfer: number, matrix: number): Uint8Array {
    const writer = new BitWriter();
    writer.writeBits(0, 3);  // seq_profile (0)
    writer.writeBits(0, 1);  // still_picture
    writer.writeBits(0, 1);  // reduced_still_picture_header
    writer.writeBits(0, 1);  // timing_info_present_flag
    writer.writeBits(0, 1);  // initial_display_delay_present_flag
    writer.writeBits(0, 5);  // operating_points_cnt_minus_1
    writer.writeBits(0, 12);  // operating_point_idc[0]
    writer.writeBits(7, 5);  // seq_level_idx[0] (7 以下なので tier ビットは無い)
    writer.writeBits(11, 4);  // frame_width_bits_minus_1
    writer.writeBits(11, 4);  // frame_height_bits_minus_1
    writer.writeBits(3839, 12);  // max_frame_width_minus_1
    writer.writeBits(2159, 12);  // max_frame_height_minus_1
    writer.writeBits(0, 1);  // frame_id_numbers_present_flag
    writer.writeBits(1, 1);  // use_128x128_superblock
    writer.writeBits(1, 1);  // enable_filter_intra
    writer.writeBits(0, 1);  // enable_intra_edge_filter
    writer.writeBits(0, 1);  // enable_interintra_compound
    writer.writeBits(0, 1);  // enable_masked_compound
    writer.writeBits(0, 1);  // enable_warped_motion
    writer.writeBits(0, 1);  // enable_dual_filter
    writer.writeBits(0, 1);  // enable_order_hint
    writer.writeBits(1, 1);  // seq_choose_screen_content_tools (SELECT)
    writer.writeBits(1, 1);  // seq_choose_integer_mv (SELECT)
    writer.writeBits(0, 1);  // enable_superres
    writer.writeBits(1, 1);  // enable_cdef
    writer.writeBits(0, 1);  // enable_restoration
    // color_config()
    writer.writeBits(1, 1);  // high_bitdepth (10bit)
    writer.writeBits(0, 1);  // mono_chrome
    writer.writeBits(1, 1);  // color_description_present_flag
    writer.writeBits(primaries, 8);
    writer.writeBits(transfer, 8);
    writer.writeBits(matrix, 8);
    const payload = writer.toBytes();
    const header = new Uint8Array([0x0A, payload.byteLength]);  // type=1 (sequence header) + has_size_field
    return concat(header, payload);
}

/** AVC の初期化セグメント (avcC + colr 付き avc1 エントリ) */
function buildAvcInitSegment(primaries: number, transfer: number, matrix: number): Uint8Array {
    const sps = buildAvcSps(primaries, transfer, matrix);
    const avcc = new Uint8Array(6 + 2 + sps.byteLength + 1);
    avcc.set([1, 100, 0, 51, 0xFF, 0xE1], 0);  // version/profile/compat/level/lengthSizeMinusOne=4/numOfSPS=1
    new DataView(avcc.buffer).setUint16(6, sps.byteLength, false);
    avcc.set(sps, 8);
    avcc[8 + sps.byteLength] = 0;  // numOfPictureParameterSets = 0
    const entry = box('avc1', visualSampleEntryHeader(), box('avcC', avcc), colrBox(primaries, transfer, matrix));
    return concat(ftypBox(), moovWithSampleEntry(entry));
}

/** HEVC の初期化セグメント (hvcC + colr 付き hvc1 エントリ) */
function buildHevcInitSegment(primaries: number, transfer: number, matrix: number): Uint8Array {
    const sps = buildHevcSps(primaries, transfer, matrix);
    const record_header = new Uint8Array(23);
    record_header[0] = 1;  // configurationVersion
    record_header[1] = (0 << 6) | (0 << 5) | 2;  // profile_space/tier/profile_idc (Main10)
    record_header[12] = 153;  // general_level_idc
    record_header[21] = 0xFC | 3;  // lengthSizeMinusOne = 4
    record_header[22] = 1;  // numOfArrays
    const array_header = new Uint8Array(3 + 2);
    array_header[0] = 0x80 | 33;  // array_completeness + NAL type (SPS)
    new DataView(array_header.buffer).setUint16(1, 1, false);  // numNalus
    new DataView(array_header.buffer).setUint16(3, sps.byteLength, false);
    const hvcc = box('hvcC', record_header, array_header, sps);
    const entry = box('hvc1', visualSampleEntryHeader(), hvcc, colrBox(primaries, transfer, matrix));
    return concat(ftypBox(), moovWithSampleEntry(entry));
}

/** VP9 の初期化セグメント (vpcC + colr 付き vp09 エントリ) */
function buildVp9InitSegment(primaries: number, transfer: number, matrix: number): Uint8Array {
    const vpcc_content = new Uint8Array(12);
    vpcc_content[0] = 1;  // version
    vpcc_content[4] = 0;  // profile
    vpcc_content[5] = 61;  // level
    vpcc_content[6] = (10 << 4) | (1 << 1);  // bitDepth=10 / 4:2:0 / fullRange=0
    vpcc_content[7] = primaries;
    vpcc_content[8] = transfer;
    vpcc_content[9] = matrix;
    const entry = box('vp09', visualSampleEntryHeader(), box('vpcC', vpcc_content), colrBox(primaries, transfer, matrix));
    return concat(ftypBox(), moovWithSampleEntry(entry));
}

/** AV1 の初期化セグメント (av1C configOBUs + colr 付き av01 エントリ) */
function buildAv1InitSegment(primaries: number, transfer: number, matrix: number): Uint8Array {
    const av1c_header = new Uint8Array([0x81, 0x00, 0x0C, 0x00]);  // marker=1 version=1 / profile0 level7 / 10bit 4:2:0
    const av1c = box('av1C', av1c_header, buildAv1SequenceHeaderObu(primaries, transfer, matrix));
    const entry = box('av01', visualSampleEntryHeader(), av1c, colrBox(primaries, transfer, matrix));
    return concat(ftypBox(), moovWithSampleEntry(entry));
}

/** HEVC のメディアセグメント (in-band SPS + ダミーのスライス NAL を持つ1サンプル) */
function buildHevcMediaSegment(primaries: number, transfer: number, matrix: number): Uint8Array {
    const sps = buildHevcSps(primaries, transfer, matrix);
    const slice_nal = new Uint8Array([0x02, 0x01, 0xAA, 0xBB, 0xCC]);  // 適当なスライス NAL
    const sample = new Uint8Array(4 + sps.byteLength + 4 + slice_nal.byteLength);
    new DataView(sample.buffer).setUint32(0, sps.byteLength, false);
    sample.set(sps, 4);
    new DataView(sample.buffer).setUint32(4 + sps.byteLength, slice_nal.byteLength, false);
    sample.set(slice_nal, 4 + sps.byteLength + 4);

    const styp = box('styp', new Uint8Array([0x6D, 0x73, 0x64, 0x68, 0, 0, 0, 0]));  // 'msdh'
    const mfhd = box('mfhd', new Uint8Array(8));
    const tfhd_content = new Uint8Array(8);  // version/flags(default-base-is-moof) + track_ID
    tfhd_content[1] = 0x02;  // flags = 0x020000 (default_base_is_moof)
    tfhd_content[7] = 1;  // track_ID
    const tfhd = box('tfhd', tfhd_content);
    // trun は box() が内容をコピーするため、data_offset と sample_size を確定してから組み立てる
    const moof_size = 8 + mfhd.byteLength + (8 + tfhd.byteLength + (8 + 16));
    const trun_content = new Uint8Array(16);
    trun_content[1] = 0x00;
    trun_content[2] = 0x02;
    trun_content[3] = 0x01;  // flags = data-offset-present + sample-size-present
    new DataView(trun_content.buffer).setUint32(4, 1, false);  // sample_count
    new DataView(trun_content.buffer).setInt32(8, moof_size + 8, false);  // data_offset (moof 先頭からの相対)
    new DataView(trun_content.buffer).setUint32(12, sample.byteLength, false);  // sample_size
    const trun = box('trun', trun_content);
    const moof = box('moof', mfhd, box('traf', tfhd, trun));
    const mdat = box('mdat', sample);
    return concat(styp, moof, mdat);
}


describe('KonomiTVBS4KFMP4ColorRewrite (初期化セグメント)', () => {
    it('AVC の HLG 信号を ToneMap で SDR 用の信号へ書き換える', () => {
        const init = buildAvcInitSegment(9, CICP_HLG_TRANSFER, 9);
        const result = rewriteKonomiTVBS4KFMP4InitSegment(init, 'ToneMap');

        expect(result.unsafe).toBe(false);
        expect(result.rewritten).toBe(true);
        expect(result.detected).toEqual({colour_primaries: 9, transfer_characteristics: CICP_HLG_TRANSFER, matrix_coeffs: 9});
        expect(result.video_state).toMatchObject({codec: 'AVC', nal_length_size: 4});
        // バイト長は変わらず、書換え後は SDR 用の信号 (BT.709 primaries + sRGB transfer) として読める
        expect(result.data.byteLength).toBe(init.byteLength);
        const verify = rewriteKonomiTVBS4KFMP4InitSegment(result.data, 'None');
        expect(verify.detected).toEqual({
            colour_primaries: CICP_BT709_PRIMARIES,
            transfer_characteristics: CICP_SRGB_TRANSFER,
            matrix_coeffs: 9,
        });
    });

    it('HEVC の PQ 信号を ToneMap で SDR 用の信号へ書き換える', () => {
        const init = buildHevcInitSegment(9, CICP_PQ_TRANSFER, 9);
        const result = rewriteKonomiTVBS4KFMP4InitSegment(init, 'ToneMap');

        expect(result.unsafe).toBe(false);
        expect(result.rewritten).toBe(true);
        expect(result.detected).toEqual({colour_primaries: 9, transfer_characteristics: CICP_PQ_TRANSFER, matrix_coeffs: 9});
        expect(result.video_state).toMatchObject({codec: 'HEVC', nal_length_size: 4});
        expect(result.data.byteLength).toBe(init.byteLength);
        const verify = rewriteKonomiTVBS4KFMP4InitSegment(result.data, 'None');
        expect(verify.detected).toEqual({
            colour_primaries: CICP_BT709_PRIMARIES,
            transfer_characteristics: CICP_SRGB_TRANSFER,
            matrix_coeffs: 9,
        });
    });

    it('VP9 の PQ 信号を ToneMap で SDR 用の信号へ書き換える', () => {
        const init = buildVp9InitSegment(9, CICP_PQ_TRANSFER, 9);
        const result = rewriteKonomiTVBS4KFMP4InitSegment(init, 'ToneMap');

        expect(result.unsafe).toBe(false);
        expect(result.rewritten).toBe(true);
        expect(result.detected).toEqual({colour_primaries: 9, transfer_characteristics: CICP_PQ_TRANSFER, matrix_coeffs: 9});
        expect(result.video_state).toMatchObject({codec: 'VP9'});
        const verify = rewriteKonomiTVBS4KFMP4InitSegment(result.data, 'None');
        expect(verify.detected).toEqual({
            colour_primaries: CICP_BT709_PRIMARIES,
            transfer_characteristics: CICP_SRGB_TRANSFER,
            matrix_coeffs: 9,
        });
    });

    it('AV1 の HLG 信号を ToneMap で SDR 用の信号へ書き換える', () => {
        const init = buildAv1InitSegment(9, CICP_HLG_TRANSFER, 9);
        const result = rewriteKonomiTVBS4KFMP4InitSegment(init, 'ToneMap');

        expect(result.unsafe).toBe(false);
        expect(result.rewritten).toBe(true);
        expect(result.detected).toEqual({colour_primaries: 9, transfer_characteristics: CICP_HLG_TRANSFER, matrix_coeffs: 9});
        expect(result.video_state).toMatchObject({codec: 'AV1'});
        const verify = rewriteKonomiTVBS4KFMP4InitSegment(result.data, 'None');
        expect(verify.detected).toEqual({
            colour_primaries: CICP_BT709_PRIMARIES,
            transfer_characteristics: CICP_SRGB_TRANSFER,
            matrix_coeffs: 9,
        });
    });

    it('None モードでは4コーデックともバイト列を一切変更しない', () => {
        const inits = [
            buildAvcInitSegment(9, CICP_HLG_TRANSFER, 9),
            buildHevcInitSegment(9, CICP_PQ_TRANSFER, 9),
            buildVp9InitSegment(9, CICP_PQ_TRANSFER, 9),
            buildAv1InitSegment(9, CICP_HLG_TRANSFER, 9),
        ];
        for (const init of inits) {
            const result = rewriteKonomiTVBS4KFMP4InitSegment(init, 'None');
            expect(result.unsafe).toBe(false);
            expect(result.rewritten).toBe(false);
            expect(Buffer.from(result.data).equals(Buffer.from(init))).toBe(true);
        }
    });

    it('非 HDR (BT.709) の信号は ToneMap モードでも変更しない', () => {
        const inits = [
            buildAvcInitSegment(1, 1, 1),
            buildHevcInitSegment(1, 1, 1),
            buildVp9InitSegment(1, 1, 1),
            buildAv1InitSegment(1, 1, 1),
        ];
        for (const init of inits) {
            const result = rewriteKonomiTVBS4KFMP4InitSegment(init, 'ToneMap');
            expect(result.unsafe).toBe(false);
            expect(result.rewritten).toBe(false);
            expect(result.detected).toEqual({colour_primaries: 1, transfer_characteristics: 1, matrix_coeffs: 1});
            expect(Buffer.from(result.data).equals(Buffer.from(init))).toBe(true);
        }
    });

    it('破損した初期化セグメントはバイト列を改変せず unsafe で素通しする', () => {
        const init = buildHevcInitSegment(9, CICP_PQ_TRANSFER, 9);
        const truncated = init.subarray(0, init.byteLength - 5);  // moov の途中で切れた入力
        const result = rewriteKonomiTVBS4KFMP4InitSegment(truncated, 'ToneMap');
        expect(result.unsafe).toBe(true);
        expect(result.rewritten).toBe(false);
        expect(Buffer.from(result.data).equals(Buffer.from(truncated))).toBe(true);
    });
});


describe('KonomiTVBS4KFMP4ColorRewrite (メディアセグメント in-band)', () => {
    const video_state: KonomiTVBS4KFMP4VideoColorState = {
        codec: 'HEVC',
        nal_length_size: 4,
        original: {colour_primaries: 9, transfer_characteristics: CICP_PQ_TRANSFER, matrix_coeffs: 9},
    };

    it('HEVC の in-band SPS の PQ 信号を ToneMap で書き換える', () => {
        const segment = buildHevcMediaSegment(9, CICP_PQ_TRANSFER, 9);
        const result = rewriteKonomiTVBS4KFMP4MediaSegment(segment, 'ToneMap', video_state);

        expect(result.unsafe).toBe(false);
        expect(result.rewritten).toBe(true);
        expect(result.detected).toEqual({colour_primaries: 9, transfer_characteristics: CICP_PQ_TRANSFER, matrix_coeffs: 9});
        expect(result.data.byteLength).toBe(segment.byteLength);
        // 書換え後のセグメントを再度処理すると SDR 用の信号として読め、これ以上は書き換えない
        const verify = rewriteKonomiTVBS4KFMP4MediaSegment(result.data, 'ToneMap', {
            ...video_state,
            original: {colour_primaries: CICP_BT709_PRIMARIES, transfer_characteristics: CICP_SRGB_TRANSFER, matrix_coeffs: 9},
        });
        expect(verify.unsafe).toBe(false);
        expect(verify.rewritten).toBe(false);
        expect(verify.detected).toEqual({
            colour_primaries: CICP_BT709_PRIMARIES,
            transfer_characteristics: CICP_SRGB_TRANSFER,
            matrix_coeffs: 9,
        });
    });

    it('None モードではメディアセグメントを変更しない', () => {
        const segment = buildHevcMediaSegment(9, CICP_PQ_TRANSFER, 9);
        const result = rewriteKonomiTVBS4KFMP4MediaSegment(segment, 'None', video_state);
        expect(result.unsafe).toBe(false);
        expect(result.rewritten).toBe(false);
        expect(Buffer.from(result.data).equals(Buffer.from(segment))).toBe(true);
    });

    it('破損したメディアセグメントはバイト列を改変せず unsafe で素通しする', () => {
        const segment = buildHevcMediaSegment(9, CICP_PQ_TRANSFER, 9);
        const truncated = segment.subarray(0, segment.byteLength - 3);  // mdat 中途半端な入力
        const result = rewriteKonomiTVBS4KFMP4MediaSegment(truncated, 'ToneMap', video_state);
        expect(result.unsafe).toBe(true);
        expect(Buffer.from(result.data).equals(Buffer.from(truncated))).toBe(true);
    });
});


describe('HDR 出力の選択状態と書換えモードの対応', () => {
    it('override 優先で SDR 変換時だけ ToneMap になる', () => {
        // 視聴中だけの override が設定の既定値へ優先する
        expect(resolveKonomiTVBS4KHdrOutput('Auto', 'SDR')).toBe('SDR');
        expect(resolveKonomiTVBS4KHdrOutput('HDR', 'SDR')).toBe('SDR');
        expect(resolveKonomiTVBS4KHdrOutput('SDR', 'HDR')).toBe('HDR');
        expect(resolveKonomiTVBS4KHdrOutput('SDR', null)).toBe('SDR');
        expect(resolveKonomiTVBS4KHdrOutput('HDR', null)).toBe('HDR');

        // 書換えセッションは SDR 変換が必要な場合だけ ToneMap で初期化される
        expect(createKonomiTVBS4KColorRewriteSession('SDR').mode).toBe('ToneMap');
        expect(createKonomiTVBS4KColorRewriteSession('HDR').mode).toBe('None');
    });
});
