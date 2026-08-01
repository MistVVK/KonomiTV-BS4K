import { extractKonomiTVBS4KID3PrivateFrames } from '@/services/player/KonomiTVBS4KID3';


/**
 * dantto4k が ARIB-TTML の MFU を格納する ID3v2.4 PRIV owner。
 *
 * この外装は ARIB 規格そのものではなく、dantto4k と KonomiTV の間だけで使う。
 * 外装を剥がした後の MFU / MPU は ARIB STD-B60 Volume 1 Chapter 9 に従う。
 */
const ARIB_TTML_PRIV_OWNER = 'arib-ttml.js';

const CAPTION_COMPONENT_TAG_BEGIN = 0x30;
const CAPTION_COMPONENT_TAG_END = 0x37;
const SUPERIMPOSE_COMPONENT_TAG_BEGIN = 0x38;
const SUPERIMPOSE_COMPONENT_TAG_END = 0x3F;

const MAX_MFU_SIZE = 500 * 1024;
const MAX_FRAGMENT_COUNT = 16;
const MAX_PENDING_ASSEMBLIES = 128;
// TR-B39 8.2.1 / 8.2.2 では1 MPU最大5 MFU（TTMLを含む）。
const MAX_SUBSAMPLE_NUMBER = 4;


export type ARIBTTMLComponent = 'Caption' | 'Superimpose';


export interface ARIBTTMLAdditionalInfo {
    subtitle_tag: number;
    version: number;
    start_mpu_flag: boolean;
    language: string;
    type: number;
    subtitle_format: number;
    operation_mode: number;
    time_management_mode: number;
    display_mode: number;
    resolution: number;
    compression_type: number;
    start_mpu_sequence_number: number | null;
    reference_start_time: number | null;
    reference_start_time_leap_indicator: number | null;
}


export interface ARIBTTMLResource {
    data_type: number;
    data: Uint8Array;
}


export interface ARIBTTMLPresentationUnit {
    pts: number;
    transport_timestamp: number | null;
    order: number;
    component: ARIBTTMLComponent;
    component_tag: number;
    subtitle_tag: number;
    subtitle_sequence_number: number;
    additional_info: ARIBTTMLAdditionalInfo;
    ttml: string;
    resources: ReadonlyMap<number, ARIBTTMLResource>;
}


interface ARIBTTMLEnvelope {
    pts: number;
    transport_timestamp: number | null;
    component: ARIBTTMLComponent;
    component_tag: number;
    subtitle_tag: number;
    subtitle_sequence_number: number;
    subsample_number: number;
    last_subsample_number: number;
    additional_info: Uint8Array;
    fragment_index: number;
    fragment_count: number;
    raw_mfu_size: number;
    fragment_offset: number;
    fragment: Uint8Array;
}


interface FragmentAssembly {
    envelope: ARIBTTMLEnvelope;
    fragments: Map<number, {offset: number; data: Uint8Array}>;
}


interface ParsedMFU {
    pts: number;
    transport_timestamp: number | null;
    component: ARIBTTMLComponent;
    component_tag: number;
    subtitle_tag: number;
    subtitle_sequence_number: number;
    subsample_number: number;
    last_subsample_number: number;
    data_type: number;
    additional_info: Uint8Array;
    data: Uint8Array;
    subsample_hints: ReadonlyMap<number, {data_type: number; data_size: number}>;
}


interface MPUAssembly {
    first_mfu: ParsedMFU;
    subsamples: Map<number, ParsedMFU>;
}


function readBigEndian16(data: Uint8Array, offset: number): number {
    return (data[offset] << 8) | data[offset + 1];
}


function readBigEndian32(data: Uint8Array, offset: number): number {
    return (
        data[offset] * 0x1000000 +
        (data[offset + 1] << 16) +
        (data[offset + 2] << 8) +
        data[offset + 3]
    );
}


/** envelope v2 の u64 source PTS を読み、MPEG-TS PTS の33bit範囲だけを秒へ変換する。 */
function readSourcePTS(data: Uint8Array, offset: number): number | null {
    if (offset < 0 || offset + 8 > data.length) return null;
    const high = readBigEndian32(data, offset);
    const low = readBigEndian32(data, offset + 4);
    // MPEG-TS PTS は33bitなので、u64の上位31bitが立つ値は受理しない。
    if (high > 1) return null;
    return (high * 0x100000000 + low) / 90_000;
}


function equalBytes(left: Uint8Array, right: Uint8Array): boolean {
    if (left.length !== right.length) return false;
    return left.every((value, index) => value === right[index]);
}


function getComponent(component_tag: number): ARIBTTMLComponent | null {
    if (CAPTION_COMPONENT_TAG_BEGIN <= component_tag && component_tag <= CAPTION_COMPONENT_TAG_END) {
        return 'Caption';
    }
    if (SUPERIMPOSE_COMPONENT_TAG_BEGIN <= component_tag && component_tag <= SUPERIMPOSE_COMPONENT_TAG_END) {
        return 'Superimpose';
    }
    return null;
}


/** dantto4k 固有の PRIV payload を検証し、MFU fragment 情報へ変換する。 */
function parseEnvelope(
    pts: number,
    transport_timestamp: number | null,
    payload: Uint8Array,
): ARIBTTMLEnvelope | null {
    if (payload.length < 20 || (payload[0] !== 1 && payload[0] !== 2)) return null;
    const envelope_version = payload[0];
    const envelope_header_length = envelope_version === 2 ? 28 : 20;
    if (payload.length < envelope_header_length) return null;
    const component_tag = payload[1];
    const component = getComponent(component_tag);
    if (component === null) return null;

    const additional_info_length = readBigEndian16(payload, 6);
    const fragment_index = readBigEndian16(payload, 8);
    const fragment_count = readBigEndian16(payload, 10);
    const raw_mfu_size = readBigEndian32(payload, 12);
    const fragment_offset = readBigEndian32(payload, 16);
    // 独自外装v1は変換前source PTSを持たない20 byte headerなので、呼び出し元のTS PTSを使う。
    // v2は28 byte headerの末尾に変換前source PTSを持ち、ライブ再多重化後もその値を優先する。
    // 既存録画と新規録画の両方を再生し続けるため、v1/v2は恒久的に受理する。
    let envelope_transport_timestamp = transport_timestamp;
    if (envelope_version === 2) {
        envelope_transport_timestamp = readSourcePTS(payload, 20);
        if (envelope_transport_timestamp === null) return null;
    }
    const fragment_begin = envelope_header_length + additional_info_length;
    if (
        fragment_count === 0 || fragment_count > MAX_FRAGMENT_COUNT ||
        fragment_index >= fragment_count ||
        raw_mfu_size < 7 || raw_mfu_size > MAX_MFU_SIZE ||
        fragment_begin > payload.length ||
        fragment_offset > raw_mfu_size ||
        payload.length - fragment_begin > raw_mfu_size - fragment_offset
    ) {
        return null;
    }

    return {
        pts,
        transport_timestamp: envelope_transport_timestamp,
        component,
        component_tag,
        subtitle_tag: payload[2],
        subtitle_sequence_number: payload[3],
        subsample_number: payload[4],
        last_subsample_number: payload[5],
        additional_info: payload.slice(envelope_header_length, fragment_begin),
        fragment_index,
        fragment_count,
        raw_mfu_size,
        fragment_offset,
        fragment: payload.slice(fragment_begin),
    };
}


function parseAdditionalInfo(data: Uint8Array): ARIBTTMLAdditionalInfo | null {
    if (data.length < 8) return null;
    const version = data[1] >> 4;
    const start_mpu_sequence_number_flag = (data[1] & 0x08) !== 0;
    const time_management_mode = data[6] >> 4;
    let cursor = 8;
    let start_mpu_sequence_number: number | null = null;
    if (start_mpu_sequence_number_flag) {
        if (cursor + 4 > data.length) return null;
        start_mpu_sequence_number = readBigEndian32(data, cursor);
        cursor += 4;
    }
    let reference_start_time: number | null = null;
    let reference_start_time_leap_indicator: number | null = null;
    if (time_management_mode === 0x02) {
        // NTP long format: 32bit seconds from 1900-01-01 + 32bit fraction。
        if (cursor + 9 > data.length) return null;
        const ntp_seconds = readBigEndian32(data, cursor);
        const ntp_fraction = readBigEndian32(data, cursor + 4) / 0x100000000;
        reference_start_time = ntp_seconds - 2_208_988_800 + ntp_fraction;
        cursor += 8;
        reference_start_time_leap_indicator = data[cursor] >> 6;
        cursor += 1;
    }
    // 現行BS4K運用のsubtitle_info_versionは0。将来版を現行レイアウトとして誤読しない。
    if (version !== 0 || cursor !== data.length) return null;
    return {
        subtitle_tag: data[0],
        version,
        start_mpu_flag: start_mpu_sequence_number_flag,
        language: String.fromCharCode(...data.slice(2, 5)),
        type: data[5] >> 6,
        subtitle_format: (data[5] >> 2) & 0x0F,
        operation_mode: data[5] & 0x03,
        time_management_mode,
        display_mode: data[6] & 0x0F,
        resolution: data[7] >> 4,
        compression_type: data[7] & 0x0F,
        start_mpu_sequence_number,
        reference_start_time,
        reference_start_time_leap_indicator,
    };
}


/** ARIB STD-B60 の字幕 MFU header を剥がす。 */
function parseMFU(envelope: ARIBTTMLEnvelope, raw_mfu: Uint8Array): ParsedMFU | null {
    if (
        raw_mfu.length < 7 ||
        raw_mfu[0] !== envelope.subtitle_tag ||
        raw_mfu[1] !== envelope.subtitle_sequence_number ||
        raw_mfu[2] !== envelope.subsample_number ||
        raw_mfu[3] !== envelope.last_subsample_number ||
        envelope.subsample_number > envelope.last_subsample_number ||
        envelope.last_subsample_number > MAX_SUBSAMPLE_NUMBER
    ) {
        return null;
    }

    const flags = raw_mfu[4];
    const data_type = flags >> 4;
    const length_extension_flag = (flags & 0x08) !== 0;
    const subsample_info_list_flag = (flags & 0x04) !== 0;
    // reserved は実際の BS4K 波では '11' で送られる。値を 0 に固定せず読み飛ばす。
    // TR-B39のBS4K運用ではsubsample_info_list_flagは常に0。
    if (subsample_info_list_flag) return null;
    let cursor = 5;
    const data_size_length = length_extension_flag ? 4 : 2;
    if (cursor + data_size_length > raw_mfu.length) return null;
    const data_size = length_extension_flag ?
        readBigEndian32(raw_mfu, cursor) : readBigEndian16(raw_mfu, cursor);
    cursor += data_size_length;

    const subsample_hints = new Map<number, {data_type: number; data_size: number}>();
    if (
        envelope.subsample_number === 0 &&
        envelope.last_subsample_number > 0 &&
        subsample_info_list_flag
    ) {
        // B60 Table 9-1: 各項目は data_type 4bit + reserved 4bit と data_size で構成される。
        for (let index = 1; index <= envelope.last_subsample_number; index += 1) {
            if (cursor + 1 + data_size_length > raw_mfu.length) return null;
            const hint_data_type = raw_mfu[cursor] >> 4;
            cursor += 1;
            const hint_data_size = length_extension_flag ?
                readBigEndian32(raw_mfu, cursor) : readBigEndian16(raw_mfu, cursor);
            cursor += data_size_length;
            subsample_hints.set(index, {data_type: hint_data_type, data_size: hint_data_size});
        }
    }
    if (data_size !== raw_mfu.length - cursor) return null;

    return {
        pts: envelope.pts,
        transport_timestamp: envelope.transport_timestamp,
        component: envelope.component,
        component_tag: envelope.component_tag,
        subtitle_tag: envelope.subtitle_tag,
        subtitle_sequence_number: envelope.subtitle_sequence_number,
        subsample_number: envelope.subsample_number,
        last_subsample_number: envelope.last_subsample_number,
        data_type,
        additional_info: envelope.additional_info,
        data: raw_mfu.slice(cursor, cursor + data_size),
        subsample_hints,
    };
}


/**
 * ライブと録画の双方で共用する ARIB-TTML transport decoder。
 * 入力元は media timeline 上のPTS秒と、生の ID3 タグだけを渡せばよい。
 */
export default class ARIBTTMLDecoder {

    private readonly fragment_assemblies = new Map<string, FragmentAssembly>();
    private readonly mpu_assemblies = new Map<string, MPUAssembly>();
    private presentation_order = 0;

    public reset(): void {
        this.fragment_assemblies.clear();
        this.mpu_assemblies.clear();
        this.presentation_order = 0;
    }

    public pushID3v2Data(
        pts: number,
        data: Uint8Array,
        transport_timestamp: number | null = null,
    ): ARIBTTMLPresentationUnit[] {
        if (Number.isFinite(pts) === false || pts < 0) return [];
        if (transport_timestamp !== null && Number.isFinite(transport_timestamp) === false) return [];
        const presentation_units: ARIBTTMLPresentationUnit[] = [];
        for (const frame of extractKonomiTVBS4KID3PrivateFrames(data)) {
            if (frame.owner !== ARIB_TTML_PRIV_OWNER) continue;
            const envelope = parseEnvelope(pts, transport_timestamp, frame.payload);
            if (envelope === null) continue;
            const raw_mfu = this.pushFragment(envelope);
            if (raw_mfu === null) continue;
            const mfu = parseMFU(envelope, raw_mfu);
            if (mfu === null) continue;
            const presentation_unit = this.pushMFU(mfu);
            if (presentation_unit !== null) presentation_units.push(presentation_unit);
        }
        return presentation_units;
    }

    private pushFragment(envelope: ARIBTTMLEnvelope): Uint8Array | null {
        const pts_ticks = Math.round(envelope.pts * 90_000);
        const key = [
            pts_ticks,
            envelope.component_tag,
            envelope.subtitle_tag,
            envelope.subtitle_sequence_number,
            envelope.subsample_number,
            envelope.last_subsample_number,
            envelope.raw_mfu_size,
        ].join(':');
        let assembly = this.fragment_assemblies.get(key);
        if (assembly === undefined) {
            assembly = {envelope, fragments: new Map()};
            this.fragment_assemblies.set(key, assembly);
            if (this.fragment_assemblies.size > MAX_PENDING_ASSEMBLIES) {
                this.fragment_assemblies.delete(this.fragment_assemblies.keys().next().value!);
            }
        } else if (
            assembly.envelope.fragment_count !== envelope.fragment_count ||
            equalBytes(assembly.envelope.additional_info, envelope.additional_info) === false
            || assembly.envelope.transport_timestamp !== envelope.transport_timestamp
        ) {
            this.fragment_assemblies.delete(key);
            return null;
        }

        const duplicate = assembly.fragments.get(envelope.fragment_index);
        if (duplicate !== undefined) {
            if (duplicate.offset !== envelope.fragment_offset || equalBytes(duplicate.data, envelope.fragment) === false) {
                this.fragment_assemblies.delete(key);
            }
            return null;
        }
        assembly.fragments.set(envelope.fragment_index, {
            offset: envelope.fragment_offset,
            data: envelope.fragment,
        });
        if (assembly.fragments.size !== envelope.fragment_count) return null;

        const ordered_fragments = [...assembly.fragments.entries()].sort(([left], [right]) => left - right);
        const raw_mfu = new Uint8Array(envelope.raw_mfu_size);
        let expected_offset = 0;
        for (const [fragment_index, fragment] of ordered_fragments) {
            if (
                fragment_index >= envelope.fragment_count ||
                fragment.offset !== expected_offset ||
                fragment.offset + fragment.data.length > raw_mfu.length
            ) {
                this.fragment_assemblies.delete(key);
                return null;
            }
            raw_mfu.set(fragment.data, fragment.offset);
            expected_offset += fragment.data.length;
        }
        this.fragment_assemblies.delete(key);
        return expected_offset === raw_mfu.length ? raw_mfu : null;
    }

    private pushMFU(mfu: ParsedMFU): ARIBTTMLPresentationUnit | null {
        const pts_ticks = Math.round(mfu.pts * 90_000);
        const key = [
            pts_ticks,
            mfu.component_tag,
            mfu.subtitle_tag,
            mfu.subtitle_sequence_number,
        ].join(':');
        let assembly = this.mpu_assemblies.get(key);
        if (assembly === undefined) {
            assembly = {first_mfu: mfu, subsamples: new Map()};
            this.mpu_assemblies.set(key, assembly);
            if (this.mpu_assemblies.size > MAX_PENDING_ASSEMBLIES) {
                this.mpu_assemblies.delete(this.mpu_assemblies.keys().next().value!);
            }
        } else if (
            assembly.first_mfu.last_subsample_number !== mfu.last_subsample_number ||
            equalBytes(assembly.first_mfu.additional_info, mfu.additional_info) === false
            || assembly.first_mfu.transport_timestamp !== mfu.transport_timestamp
        ) {
            this.mpu_assemblies.delete(key);
            return null;
        }

        const duplicate = assembly.subsamples.get(mfu.subsample_number);
        if (duplicate !== undefined) {
            if (duplicate.data_type !== mfu.data_type || equalBytes(duplicate.data, mfu.data) === false) {
                this.mpu_assemblies.delete(key);
            }
            return null;
        }
        assembly.subsamples.set(mfu.subsample_number, mfu);
        // TR-B39の1 MPU最大500KBを、未完成assemblyの段階でも越えさせない。
        if ([...assembly.subsamples.values()].reduce((size, subsample) => size + subsample.data.length, 0) > MAX_MFU_SIZE) {
            this.mpu_assemblies.delete(key);
            return null;
        }
        if (assembly.subsamples.size !== mfu.last_subsample_number + 1) return null;
        for (let index = 0; index <= mfu.last_subsample_number; index += 1) {
            if (assembly.subsamples.has(index) === false) return null;
        }
        for (const [index, hint] of assembly.subsamples.get(0)!.subsample_hints) {
            const hinted_subsample = assembly.subsamples.get(index)!;
            if (hint.data_type !== hinted_subsample.data_type || hint.data_size !== hinted_subsample.data.length) {
                this.mpu_assemblies.delete(key);
                return null;
            }
        }
        this.mpu_assemblies.delete(key);

        const document_mfu = assembly.subsamples.get(0)!;
        const additional_info = parseAdditionalInfo(document_mfu.additional_info);
        if (
            document_mfu.data_type !== 0 ||
            additional_info === null ||
            additional_info.subtitle_tag !== document_mfu.subtitle_tag ||
            additional_info.operation_mode !== 0x01 ||
            additional_info.subtitle_format !== 0x00 ||
            ![0x00, 0x01].includes(additional_info.resolution) ||
            (document_mfu.component === 'Caption' && ![0x02, 0x0F].includes(additional_info.time_management_mode)) ||
            (document_mfu.component === 'Superimpose' && additional_info.time_management_mode !== 0x0F) ||
            additional_info.compression_type !== 0
        ) {
            return null;
        }

        let ttml: string;
        try {
            ttml = new TextDecoder('utf-8', {fatal: true}).decode(document_mfu.data);
        } catch (error) {
            return null;
        }
        const resources = new Map<number, ARIBTTMLResource>();
        for (let index = 1; index <= document_mfu.last_subsample_number; index += 1) {
            const resource_mfu = assembly.subsamples.get(index)!;
            // TR-B39の運用対象はPNG / AIFF-C / SVG Fontだけ。
            if (![0x01, 0x03, 0x06].includes(resource_mfu.data_type)) return null;
            resources.set(index, {data_type: resource_mfu.data_type, data: resource_mfu.data});
        }
        return {
            pts: document_mfu.pts,
            transport_timestamp: document_mfu.transport_timestamp,
            order: this.presentation_order++,
            component: document_mfu.component,
            component_tag: document_mfu.component_tag,
            subtitle_tag: document_mfu.subtitle_tag,
            subtitle_sequence_number: document_mfu.subtitle_sequence_number,
            additional_info,
            ttml,
            resources,
        };
    }
}


export {
    CAPTION_COMPONENT_TAG_BEGIN,
    CAPTION_COMPONENT_TAG_END,
    SUPERIMPOSE_COMPONENT_TAG_BEGIN,
    SUPERIMPOSE_COMPONENT_TAG_END,
};
