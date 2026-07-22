import ARIBTTMLDecoder, {
    type ARIBTTMLComponent,
    type ARIBTTMLPresentationUnit,
    type ARIBTTMLResource,
} from '@/services/player/ARIBTTMLDecoder';


const SVG_NAMESPACE = 'http://www.w3.org/2000/svg';
const XHTML_NAMESPACE = 'http://www.w3.org/1999/xhtml';
const TTML_NAMESPACE = 'http://www.w3.org/ns/ttml';
const TTML_STYLE_NAMESPACE = 'http://www.w3.org/ns/ttml#styling';
const SMPTE_NAMESPACES = new Set([
    'http://www.smpte-ra.org/schemas/2052-1/2013/smpte-tt',
    // TR-B39 の放送運用例と実BS4K波ではこちらの別名も用いられる。
    'http://www.smpte-ra.org/schemas/2052-1/2013/smpte-ttml/v1.0',
]);
const ARIB_TTML_NAMESPACES = new Set([
    'http://www.arib.or.jp/ns/arib-tt',
    'http://www.arib.or.jp/ns/arib-ttml/v1_0',
]);

const LOGICAL_VIDEO_WIDTH = 3840;
const LOGICAL_VIDEO_HEIGHT = 2160;
const MAX_TTML_DOCUMENT_SIZE = 2 * 1024 * 1024;
const MAX_CUES_PER_COMPONENT = 512;
const MPEG_TS_PTS_WRAP_SECONDS = (2 ** 33) / 90_000;


export interface ARIBTTMLRendererOptions {
    normal_font?: string;
    force_stroke_color?: boolean | string;
    force_background_color?: string;
    show_superimpose?: boolean;
    playback_mode?: 'Live' | 'Playback';
    preferred_caption_language?: 'auto' | 'jpn' | 'eng';
    rom_sound_callback?: (index: number, loop: boolean, offset: number) => unknown;
}


interface TimedHTMLElement {
    element: HTMLElement;
    begin: number;
    end: number;
}


interface AnimatedHTMLElement {
    element: HTMLElement;
    delay: number;
    begin: number;
}


interface TimedAudioSource {
    src: string;
    loop: boolean;
    begin: number;
    end: number;
}


interface ParsedTTMLDocument {
    root: HTMLDivElement | null;
    is_clear: boolean;
    plane_width: number;
    plane_height: number;
    base_time: number;
    end_time: number;
    timed_elements: TimedHTMLElement[];
    animated_elements: AnimatedHTMLElement[];
    audio_sources: TimedAudioSource[];
    content_timings: ElementTiming[];
    text_content: string | null;
}


interface PresentationCue {
    unit: ARIBTTMLPresentationUnit;
    document: ParsedTTMLDocument;
    nominal_start_time: number;
    start_time: number;
    end_time: number;
}


interface ComponentState {
    host: HTMLDivElement;
    cues_by_component: Map<number, PresentationCue[]>;
    current_cue: PresentationCue | null;
    is_visible: boolean;
    has_visible_content: boolean;
    playing_audio: Map<number, HTMLAudioElement>;
    playing_rom_sounds: Map<number, () => void>;
    pending_rom_sounds: Map<number, {cancelled: boolean}>;
    triggered_audio: Set<number>;
}


type AuxiliaryAudioKind = 'AIFF' | 'RomSound' | 'QuickReportRomSound';


interface AuxiliaryAudioRegistration {
    state: ComponentState;
    cue: PresentationCue;
    index: number;
    kind: AuxiliaryAudioKind;
    priority_time: number;
}


interface StyleContext {
    styles: Map<string, Element>;
    regions: Map<string, Element>;
    arib_ruby_annotations: Map<Element, string>;
    arib_ruby_bases: Map<Element, string>;
    embedded_images: Map<string, string>;
    resource_urls: Map<number, string>;
    animation_names: Map<string, string>;
    animation_styles: string[];
    external_fonts: ExternalFontDefinition[];
    external_font_families: Set<string>;
    unit_order: number;
}


interface ElementTiming {
    begin: number;
    end: number;
}


interface ExternalFontGlyph {
    path: string;
    horizontal_advance: number;
    vertical_origin_x: number;
    vertical_origin_y: number;
    vertical_advance: number;
    orientation: 'h' | 'v' | null;
}


interface ExternalFontDefinition {
    family: string;
    ranges: [number, number][];
    units_per_em: number;
    ascent: number;
    descent_depth: number;
    horizontal_origin_x: number;
    horizontal_origin_y: number;
    glyphs: ReadonlyMap<number, readonly ExternalFontGlyph[]>;
}


type CSSProperties = Record<string, string>;


function getXMLID(element: Element): string | null {
    return element.getAttributeNS('http://www.w3.org/XML/1998/namespace', 'id') ?? element.getAttribute('xml:id');
}


function getARIBTTMLAttribute(element: Element, local_name: string): string | null {
    const attribute = [...element.attributes].find((candidate) =>
        candidate.localName === local_name && ARIB_TTML_NAMESPACES.has(candidate.namespaceURI ?? '')
    );
    return attribute?.value ?? null;
}


interface ARIBRubyRelations {
    annotations: Map<Element, string>;
    bases: Map<Element, string>;
}


/** style の多段参照を通常の style 解決と同じ後勝ち順でたどり、ruby 属性だけを解決する。 */
function resolveARIBRubyAttribute(
    element: Element,
    styles: ReadonlyMap<string, Element>,
    resolving: Set<string> = new Set(),
): string | null {
    let result: string | null = null;
    const style_references = element.getAttribute('style')?.trim().split(/\s+/).filter(Boolean) ?? [];
    for (const style_id of style_references) {
        if (resolving.has(style_id)) continue;
        const style_element = styles.get(style_id);
        if (style_element === undefined) continue;
        resolving.add(style_id);
        result = resolveARIBRubyAttribute(style_element, styles, resolving) ?? result;
        resolving.delete(style_id);
    }
    return getARIBTTMLAttribute(element, 'ruby') ?? result;
}


/**
 * arib-tt:ruby はルビ文字側から親文字の xml:id を参照する意味リンクであり、
 * 文字の位置やサイズには影響しない。曖昧な参照で本文を欠落させないよう、
 * 同一 body 内で一意に解決できる div / p / span 間の関係だけを採用する。
 */
function collectARIBRubyRelations(
    body: Element,
    styles: ReadonlyMap<string, Element>,
    regions: ReadonlyMap<string, Element>,
): ARIBRubyRelations {
    const annotations = new Map<Element, string>();
    const bases = new Map<Element, string>();
    const content_elements = [...body.getElementsByTagNameNS(TTML_NAMESPACE, '*')].filter((element) =>
        ['div', 'p', 'span'].includes(element.localName)
    );
    const elements_by_id = new Map<string, Element | null>();
    for (const element of content_elements) {
        const id = getXMLID(element)?.trim();
        if (id === undefined || id === '') continue;
        elements_by_id.set(id, elements_by_id.has(id) ? null : element);
    }
    for (const annotation of content_elements) {
        const region_id = annotation.getAttribute('region');
        const region = region_id === null ? undefined : regions.get(region_id);
        const region_target_id = region === undefined ? null : resolveARIBRubyAttribute(region, styles);
        const target_id = (resolveARIBRubyAttribute(annotation, styles) ?? region_target_id)?.trim();
        if (target_id === undefined || target_id === '') continue;
        const base = elements_by_id.get(target_id);
        if (base === undefined || base === null || base === annotation) continue;
        annotations.set(annotation, target_id);
        bases.set(base, target_id);
    }
    return {annotations, bases};
}


/** flatten された p / div を含め、描画を変えずに解決済みのルビ関係だけを DOM へ保持する。 */
function applyARIBRubyMetadata(
    sources: readonly Element[],
    destination: HTMLElement,
    context: StyleContext,
): void {
    const annotation = [...sources].reverse().find((source) => context.arib_ruby_annotations.has(source));
    const base = [...sources].reverse().find((source) => context.arib_ruby_bases.has(source));
    if (annotation !== undefined) {
        destination.dataset.aribTtmlRubyFor = context.arib_ruby_annotations.get(annotation)!;
    }
    if (base !== undefined) {
        destination.dataset.aribTtmlRubyBase = context.arib_ruby_bases.get(base)!;
    }
}


function parseLengthPair(value: string | null): [number, number] | null {
    if (value === null) return null;
    const values = value.trim().split(/\s+/);
    if (values.length !== 2 || values.some((item) => /^-?\d+(?:\.\d+)?px$/.test(item) === false)) return null;
    return [Number.parseFloat(values[0]), Number.parseFloat(values[1])];
}


function parseTimeExpression(value: string | null): number | null {
    if (value === null || value === '') return null;
    if (value === 'indefinite') return Number.POSITIVE_INFINITY;
    const clock_match = value.match(/^(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?$/);
    if (clock_match !== null) {
        const fraction = clock_match[4] ? Number.parseFloat(`0.${clock_match[4]}`) : 0;
        return Number(clock_match[1]) * 3600 + Number(clock_match[2]) * 60 + Number(clock_match[3]) + fraction;
    }
    const offset_match = value.match(/^(-?\d+(?:\.\d+)?)(h|m|s|ms)$/);
    if (offset_match === null) return null;
    const scale = {h: 3600, m: 60, s: 1, ms: 0.001}[offset_match[2] as 'h' | 'm' | 's' | 'ms'];
    return Number.parseFloat(offset_match[1]) * scale;
}


function parseCSSSeconds(value: string): number {
    const match = value.trim().match(/^(-?\d+(?:\.\d+)?)(ms|s)?$/);
    if (match === null) return 0;
    return Number.parseFloat(match[1]) * (match[2] === 'ms' ? 0.001 : 1);
}


function computeTiming(element: Element, parent: ElementTiming): ElementTiming {
    const begin_value = parseTimeExpression(element.getAttribute('begin'));
    const end_value = parseTimeExpression(element.getAttribute('end'));
    const duration_value = parseTimeExpression(element.getAttribute('dur'));
    const begin = begin_value === null ? parent.begin :
        begin_value === Number.POSITIVE_INFINITY ? parent.begin : parent.begin + begin_value;
    let end = parent.end;
    if (end_value !== null && end_value !== Number.POSITIVE_INFINITY) {
        end = Math.min(end, parent.begin + end_value);
    } else if (duration_value !== null && duration_value !== Number.POSITIVE_INFINITY) {
        end = Math.min(end, begin + duration_value);
    } else if (end_value === Number.POSITIVE_INFINITY || duration_value === Number.POSITIVE_INFINITY) {
        end = Number.POSITIVE_INFINITY;
    }
    return {begin, end: Math.max(begin, end)};
}


function computeElementTiming(element: Element, timing_root: Element): ElementTiming {
    const ancestors: Element[] = [];
    let current: Element | null = element;
    while (current !== null) {
        ancestors.unshift(current);
        if (current === timing_root) break;
        current = current.parentElement;
    }
    let timing: ElementTiming = {begin: 0, end: Number.POSITIVE_INFINITY};
    for (const ancestor of ancestors) timing = computeTiming(ancestor, timing);
    return timing;
}


/** Unix/NTP由来の時刻を、33bitで周回するMPEG-TS PTSの受信時刻近傍へ戻す。 */
function getWrappedTimestampDelta(target_timestamp: number, transport_timestamp: number): number {
    let delta = (target_timestamp - transport_timestamp) % MPEG_TS_PTS_WRAP_SECONDS;
    if (delta > MPEG_TS_PTS_WRAP_SECONDS / 2) delta -= MPEG_TS_PTS_WRAP_SECONDS;
    if (delta < -MPEG_TS_PTS_WRAP_SECONDS / 2) delta += MPEG_TS_PTS_WRAP_SECONDS;
    return delta;
}


function encodeBase64(data: Uint8Array): string {
    let binary = '';
    const chunk_size = 0x8000;
    for (let offset = 0; offset < data.length; offset += chunk_size) {
        binary += String.fromCharCode(...data.subarray(offset, offset + chunk_size));
    }
    return btoa(binary);
}


function readFourCC(data: Uint8Array, offset: number): string {
    if (offset < 0 || offset + 4 > data.length) return '';
    return String.fromCharCode(data[offset], data[offset + 1], data[offset + 2], data[offset + 3]);
}


function readBigEndian16(data: Uint8Array, offset: number): number {
    return (data[offset] << 8) | data[offset + 1];
}


function readBigEndian32(data: Uint8Array, offset: number): number {
    return data[offset] * 0x1000000 + (data[offset + 1] << 16) + (data[offset + 2] << 8) + data[offset + 3];
}


function readExtended80SampleRate(data: Uint8Array, offset: number): number | null {
    if (offset < 0 || offset + 10 > data.length) return null;
    const sign = data[offset] >> 7;
    const exponent = ((data[offset] & 0x7F) << 8) | data[offset + 1];
    let mantissa = 0n;
    for (let index = 0; index < 8; index += 1) {
        mantissa = (mantissa << 8n) | BigInt(data[offset + 2 + index]);
    }
    if (sign !== 0 || exponent === 0 || exponent === 0x7FFF || (mantissa & (1n << 63n)) === 0n) return null;
    const value = Number(mantissa) * (2 ** (exponent - 16_383 - 63));
    return Number.isSafeInteger(value) && [12_000, 24_000, 48_000].includes(value) ? value : null;
}


/** ARIB運用の非圧縮AIFF-C PCMを、Chromiumが再生可能なRIFF/WAVE PCMへ変換する。 */
function convertAIFFCToWAV(data: Uint8Array): Uint8Array | null {
    if (data.length < 12 || readFourCC(data, 0) !== 'FORM' || readFourCC(data, 8) !== 'AIFC') return null;
    const form_end = 8 + readBigEndian32(data, 4);
    if (form_end < 12 || form_end > data.length) return null;

    let channels: number | null = null;
    let sample_frames: number | null = null;
    let bits_per_sample: number | null = null;
    let sample_rate: number | null = null;
    let compression_type: string | null = null;
    let sound_data: Uint8Array | null = null;
    let found_fver = false;
    let found_valid_fver = false;
    for (let cursor = 12; cursor + 8 <= form_end;) {
        const chunk_id = readFourCC(data, cursor);
        const chunk_size = readBigEndian32(data, cursor + 4);
        const chunk_begin = cursor + 8;
        const chunk_end = chunk_begin + chunk_size;
        if (chunk_end > form_end) return null;
        if (chunk_id === 'FVER') {
            found_fver = true;
            if (chunk_size < 4) return null;
            found_valid_fver = readBigEndian32(data, chunk_begin) === 0xA2805140;
        } else if (chunk_id === 'COMM' && chunk_size >= 22) {
            channels = readBigEndian16(data, chunk_begin);
            sample_frames = readBigEndian32(data, chunk_begin + 2);
            bits_per_sample = readBigEndian16(data, chunk_begin + 6);
            sample_rate = readExtended80SampleRate(data, chunk_begin + 8);
            compression_type = readFourCC(data, chunk_begin + 18);
        } else if (chunk_id === 'SSND' && chunk_size >= 8) {
            const sound_offset = readBigEndian32(data, chunk_begin);
            const block_size = readBigEndian32(data, chunk_begin + 4);
            const pcm_begin = chunk_begin + 8 + sound_offset;
            if (block_size !== 0 || pcm_begin > chunk_end) return null;
            sound_data = data.slice(pcm_begin, chunk_end);
        }
        cursor = chunk_end + (chunk_size & 1);
        if (cursor > form_end) return null;
    }
    if (
        channels === null || ![1, 2].includes(channels) ||
        sample_frames === null || bits_per_sample === null || ![8, 16].includes(bits_per_sample) ||
        sample_rate === null || compression_type !== 'NONE' || sound_data === null
    ) {
        return null;
    }
    // FVERはAIFCで必須だが、実装差を考慮し未格納は許容し、格納済みの不正値だけを拒否する。
    if (found_fver && found_valid_fver === false) return null;

    const bytes_per_sample = bits_per_sample / 8;
    const pcm_size = sample_frames * channels * bytes_per_sample;
    if (Number.isSafeInteger(pcm_size) === false || pcm_size < 0 || pcm_size > sound_data.length) return null;
    const pad_size = pcm_size & 1;
    const output = new Uint8Array(44 + pcm_size + pad_size);
    const view = new DataView(output.buffer);
    const write_fourcc = (offset: number, value: string) => {
        for (let index = 0; index < 4; index += 1) output[offset + index] = value.charCodeAt(index);
    };
    write_fourcc(0, 'RIFF');
    view.setUint32(4, 36 + pcm_size + pad_size, true);
    write_fourcc(8, 'WAVE');
    write_fourcc(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, channels, true);
    view.setUint32(24, sample_rate, true);
    view.setUint32(28, sample_rate * channels * bytes_per_sample, true);
    view.setUint16(32, channels * bytes_per_sample, true);
    view.setUint16(34, bits_per_sample, true);
    write_fourcc(36, 'data');
    view.setUint32(40, pcm_size, true);
    if (bits_per_sample === 8) {
        for (let index = 0; index < pcm_size; index += 1) output[44 + index] = sound_data[index] ^ 0x80;
    } else {
        for (let index = 0; index < pcm_size; index += 2) {
            output[44 + index] = sound_data[index + 1];
            output[44 + index + 1] = sound_data[index];
        }
    }
    return output;
}


function resourceToDataURL(resource: ARIBTTMLResource): string | null {
    let resource_data = resource.data;
    let mime_type = ({
        0x01: 'image/png',
        0x02: 'image/svg+xml',
        0x04: 'audio/mpeg',
        0x05: 'audio/mp4',
        0x07: 'font/woff',
    } as Record<number, string>)[resource.data_type];
    if (resource.data_type === 0x03) {
        const wav = convertAIFFCToWAV(resource.data);
        if (wav === null) return null;
        resource_data = wav;
        mime_type = 'audio/wav';
    }
    if (mime_type === undefined) return null;
    return `data:${mime_type};base64,${encodeBase64(resource_data)}`;
}


function resolveResourceURL(value: string, context: StyleContext): string | null {
    const resource_match = value.match(/^subt:\/\/(\d+)$/);
    if (resource_match !== null) return context.resource_urls.get(Number(resource_match[1])) ?? null;
    if (value.startsWith('#')) return context.embedded_images.get(value.slice(1)) ?? null;
    return null;
}


function applyStyle(element: HTMLElement, properties: CSSProperties): void {
    for (const [property, value] of Object.entries(properties)) {
        element.style.setProperty(property, value);
    }
}


function normalizeColor(value: string): string {
    // CSS Color 4 では #RRGGBBAA がそのまま利用できる。
    return value.trim();
}


function parseARIBColor(value: string | null): string | null {
    if (value === null || /^#[0-9A-F]{6}(?:[0-9A-F]{2})?$/i.test(value.trim()) === false) return null;
    return value.trim();
}


function parseARIBOpacity(value: string | null): string | null {
    if (value === null || /^(?:0(?:\.\d+)?|1(?:\.0+)?)$/.test(value.trim()) === false) return null;
    const opacity = Number.parseFloat(value);
    return Number.isFinite(opacity) && opacity >= 0 && opacity <= 1 ? String(opacity) : null;
}


function mapWritingMode(value: string): CSSProperties {
    if (value === 'tbrl') return {'writing-mode': 'vertical-rl'};
    if (value === 'tblr') return {'writing-mode': 'vertical-lr'};
    if (value === 'rltb') return {'writing-mode': 'horizontal-tb', direction: 'rtl'};
    return {'writing-mode': 'horizontal-tb', direction: 'ltr'};
}


function mapTextOutline(value: string): CSSProperties {
    if (value === 'none') return {'-webkit-text-stroke-width': '0'};
    const tokens = value.trim().split(/\s+/);
    const color = tokens.find((token) => token.startsWith('#') || /^[a-z]+$/i.test(token)) ?? 'currentColor';
    const lengths = tokens.filter((token) => /^\d+(?:\.\d+)?px$/.test(token));
    const thickness = lengths[0] ?? '2px';
    const result: CSSProperties = {
        '-webkit-text-stroke-color': normalizeColor(color),
        '-webkit-text-stroke-width': thickness,
        'paint-order': 'stroke fill',
    };
    if (lengths[1] !== undefined && Number.parseFloat(lengths[1]) > 0) {
        result['text-shadow'] = `0 0 ${lengths[1]} ${normalizeColor(color)}`;
    }
    return result;
}


/** plane絶対座標のARIB originを、CSS containing blockからの相対座標へ変換する。 */
function adjustRegionPosition(
    element: HTMLElement,
    inherited_writing_mode: string = '',
    has_region_origin: boolean = true,
): void {
    if (element.style.position !== 'absolute' || element.dataset.aribPlaneLeft !== undefined) return;
    const left = element.style.left.match(/^(-?\d+(?:\.\d+)?)px$/);
    const top = element.style.top.match(/^(-?\d+(?:\.\d+)?)px$/);
    if (left === null || top === null) return;

    const is_vertical = (element.style.writingMode || inherited_writing_mode) === 'vertical-rl';
    const width = element.style.width.match(/^(\d+(?:\.\d+)?)px$/);
    if (is_vertical && width === null) return;
    const raw_left = Number.parseFloat(left[1]);
    const raw_top = Number.parseFloat(top[1]);
    // regionを参照しないparagraph wrapperはspan regionのための中立なcontaining block。
    // 子からwriting-modeを継承しても、plane全幅を右上originとして補正してはならない。
    const vertical_offset = is_vertical && has_region_origin ? Number.parseFloat(width![1]) : 0;
    const plane_left = raw_left - vertical_offset;

    let containing_plane_left = 0;
    let containing_plane_top = 0;
    for (let ancestor = element.parentElement; ancestor !== null; ancestor = ancestor.parentElement) {
        if (ancestor.dataset.aribPlaneLeft !== undefined && ancestor.dataset.aribPlaneTop !== undefined) {
            containing_plane_left = Number.parseFloat(ancestor.dataset.aribPlaneLeft);
            containing_plane_top = Number.parseFloat(ancestor.dataset.aribPlaneTop);
            break;
        }
    }
    element.dataset.aribPlaneLeft = String(plane_left);
    element.dataset.aribPlaneTop = String(raw_top);
    if (is_vertical) {
        element.dataset.aribVerticalOriginAdjusted = 'true';
        element.style.setProperty('--arib-vertical-origin-offset', `${vertical_offset}px`);
    }
    element.style.setProperty('--arib-containing-plane-left', `${containing_plane_left}px`);
    element.style.setProperty('--arib-containing-plane-top', `${containing_plane_top}px`);
    element.style.setProperty('--arib-animation-base-origin-x', `${raw_left}px`);
    element.style.setProperty('--arib-animation-base-origin-y', `${raw_top}px`);
    element.style.left = `${plane_left - containing_plane_left}px`;
    element.style.top = `${raw_top - containing_plane_top}px`;
}


function setAnimationBaseStyle(element: HTMLElement): void {
    let inherited_color = '';
    for (let current: HTMLElement | null = element; current !== null; current = current.parentElement) {
        if (current.style.color !== '') {
            inherited_color = current.style.color;
            break;
        }
    }
    element.style.setProperty(
        '--arib-animation-base-background-color',
        element.style.backgroundColor || 'transparent',
    );
    element.style.setProperty('--arib-animation-base-color', inherited_color || 'white');
    element.style.setProperty('--arib-animation-base-opacity', element.style.opacity || '1');
    if (element.style.getPropertyValue('--arib-animation-base-origin-x') === '') {
        element.style.setProperty('--arib-animation-base-origin-x', element.style.left || '0px');
    }
    if (element.style.getPropertyValue('--arib-animation-base-origin-y') === '') {
        element.style.setProperty('--arib-animation-base-origin-y', element.style.top || '0px');
    }
}


function sanitizeCSSIdentifier(value: string): string {
    return value.replace(/[^A-Za-z0-9_-]/g, '_');
}


function normalizeFontFamilyName(value: string): string {
    return value.trim().replace(/^['"]|['"]$/g, '').replace(/\s+/g, ' ').toLowerCase();
}


/**
 * TTML / ARIB 拡張の style attributes をブラウザーの CSS へ写像する。
 * 未知属性は無視し、外部URLは同一MPUの subt:// resource 以外を読み込まない。
 */
function collectInlineStyle(element: Element, context: StyleContext, options: ARIBTTMLRendererOptions): CSSProperties {
    const result: CSSProperties = {};
    for (const attribute of [...element.attributes]) {
        const value = attribute.value.trim();
        if (attribute.namespaceURI === TTML_STYLE_NAMESPACE) {
            switch (attribute.localName) {
                case 'backgroundColor': result['background-color'] = normalizeColor(value); break;
                case 'color': result.color = normalizeColor(value); break;
                case 'direction': result.direction = value; break;
                case 'display': result.display = value === 'none' ? 'none' : ''; break;
                case 'displayAlign': {
                    result.display = 'flex';
                    result['flex-direction'] = 'column';
                    result['justify-content'] = value === 'center' ? 'center' : value === 'after' ? 'flex-end' : 'flex-start';
                    break;
                }
                case 'extent': {
                    const extent = parseLengthPair(value);
                    if (extent !== null) {
                        result.width = `${extent[0]}px`;
                        result.height = `${extent[1]}px`;
                    }
                    break;
                }
                case 'fontFamily': {
                    // 受信機内蔵フォントはユーザー設定を優先するが、同一MPUで送られた外字フォントの
                    // familyまで置換すると私用領域文字が描画不能になるため、そのfamilyだけ保持する。
                    const external_family = [...context.external_font_families].find((family) =>
                        normalizeFontFamilyName(family) === normalizeFontFamilyName(value)
                    );
                    result['--arib-logical-font-family'] = value;
                    result['font-family'] = external_family ?? options.normal_font ?? value;
                    break;
                }
                case 'fontSize': {
                    const sizes = value.split(/\s+/).filter((size) => /^\d+(?:\.\d+)?px$/.test(size));
                    if (sizes.length === 1) {
                        result['font-size'] = sizes[0];
                    } else if (sizes.length >= 2) {
                        const width = Number.parseFloat(sizes[0]);
                        const height = Number.parseFloat(sizes[1]);
                        result['font-size'] = sizes[1];
                        if (height > 0 && width !== height) result['--arib-font-scale-x'] = String(width / height);
                    }
                    break;
                }
                case 'fontStyle': result['font-style'] = value; break;
                case 'fontWeight': result['font-weight'] = value; break;
                case 'lineHeight': result['line-height'] = value; break;
                case 'opacity': result.opacity = value; break;
                case 'origin': {
                    const origin = parseLengthPair(value);
                    if (origin !== null) {
                        result.left = `${origin[0]}px`;
                        result.top = `${origin[1]}px`;
                    }
                    break;
                }
                case 'padding': result.padding = value; break;
                case 'textAlign': result['text-align'] = value === 'start' ? 'start' : value === 'end' ? 'end' : value; break;
                case 'textDecoration': result['text-decoration'] = value; break;
                case 'textOutline': Object.assign(result, mapTextOutline(value)); break;
                case 'unicodeBidi': result['unicode-bidi'] = value; break;
                case 'visibility': result.visibility = value; break;
                case 'wrapOption': result['white-space'] = value === 'noWrap' ? 'nowrap' : 'normal'; break;
                case 'writingMode': Object.assign(result, mapWritingMode(value)); break;
                case 'zIndex': result['z-index'] = value; break;
            }
        } else if (ARIB_TTML_NAMESPACES.has(attribute.namespaceURI ?? '')) {
            switch (attribute.localName) {
                case 'border': result.border = value; break;
                case 'border-top': result['border-top'] = value; break;
                case 'border-bottom': result['border-bottom'] = value; break;
                case 'border-left': result['border-left'] = value; break;
                case 'border-right': result['border-right'] = value; break;
                case 'letter-spacing': result['letter-spacing'] = value; break;
                case 'text-shadow': result['text-shadow'] = value; break;
                case 'animation': {
                    const tokens = value.split(/\s+/);
                    const mapped_name = context.animation_names.get(tokens[0]);
                    if (
                        tokens.length !== 6 || mapped_name === undefined ||
                        /^\d+(?:\.\d+)?ms$/.test(tokens[1]) === false ||
                        !['linear', 'step-end'].includes(tokens[2]) ||
                        /^\d+(?:\.\d+)?ms$/.test(tokens[3]) === false ||
                        /^\d+$/.test(tokens[4]) === false || tokens[5] !== 'normal'
                    ) break;
                    const duration = Number.parseFloat(tokens[1]);
                    const delay = Number.parseFloat(tokens[3]);
                    const iteration_count = Number.parseInt(tokens[4], 10);
                    if (
                        duration <= 0 || duration > 86_400_000 || delay > 86_400_000 ||
                        iteration_count < 1 || iteration_count > 10_000
                    ) break;
                    result.animation = `${mapped_name} ${duration}ms ${tokens[2]} ${delay}ms ${iteration_count} normal`;
                    break;
                }
            }
        } else if (SMPTE_NAMESPACES.has(attribute.namespaceURI ?? '') && attribute.localName === 'backgroundImage') {
            const resource_url = resolveResourceURL(value, context);
            if (resource_url !== null) {
                result['background-image'] = `url("${resource_url}")`;
                result['background-repeat'] = 'no-repeat';
                result['background-size'] = '100% 100%';
            }
        }
    }
    if (options.force_background_color !== undefined && element.localName === 'span') {
        result['background-color'] = options.force_background_color;
    }
    if (options.force_stroke_color !== undefined && options.force_stroke_color !== false) {
        result['-webkit-text-stroke-color'] = typeof options.force_stroke_color === 'string' ?
            options.force_stroke_color : 'black';
        result['-webkit-text-stroke-width'] ??= '2px';
        result['paint-order'] = 'stroke fill';
    }
    return result;
}


function resolveStyle(
    element: Element,
    context: StyleContext,
    options: ARIBTTMLRendererOptions,
    resolving: Set<string> = new Set(),
): CSSProperties {
    const result: CSSProperties = {};
    const style_references = element.getAttribute('style')?.trim().split(/\s+/).filter(Boolean) ?? [];
    for (const style_id of style_references) {
        if (resolving.has(style_id)) continue;
        const style_element = context.styles.get(style_id);
        if (style_element === undefined) continue;
        resolving.add(style_id);
        Object.assign(result, resolveStyle(style_element, context, options, resolving));
        resolving.delete(style_id);
    }
    Object.assign(result, collectInlineStyle(element, context, options));
    return result;
}


function getInheritedLogicalFontFamily(element: HTMLElement): string {
    let current: HTMLElement | null = element;
    while (current !== null) {
        const family = current.style.getPropertyValue('--arib-logical-font-family');
        if (family !== '') return normalizeFontFamilyName(family);
        current = current.parentElement;
    }
    return 'round gothic';
}


function getInheritedWritingMode(element: HTMLElement): string {
    let current: HTMLElement | null = element;
    while (current !== null) {
        if (current.style.writingMode !== '') return current.style.writingMode;
        current = current.parentElement;
    }
    return 'horizontal-tb';
}


function getInheritedPixelLetterSpacing(element: HTMLElement): number | null {
    let current: HTMLElement | null = element;
    while (current !== null) {
        const match = current.style.letterSpacing.match(/^(-?\d+(?:\.\d+)?)px$/);
        if (match !== null) return Number.parseFloat(match[1]);
        current = current.parentElement;
    }
    return null;
}


function createExternalGlyph(
    character: string,
    font: ExternalFontDefinition,
    glyph: ExternalFontGlyph,
    is_vertical: boolean,
): SVGSVGElement {
    const svg = document.createElementNS(SVG_NAMESPACE, 'svg');
    if (is_vertical) {
        svg.setAttribute('viewBox', `0 0 ${font.units_per_em} ${glyph.vertical_advance}`);
        svg.setAttribute('width', '1em');
        svg.setAttribute('height', `${glyph.vertical_advance / font.units_per_em}em`);
    } else {
        const height = font.ascent + font.descent_depth;
        svg.setAttribute('viewBox', `0 0 ${glyph.horizontal_advance} ${height}`);
        svg.setAttribute('width', `${glyph.horizontal_advance / font.units_per_em}em`);
        svg.setAttribute('height', `${height / font.units_per_em}em`);
        svg.style.verticalAlign = `${-font.descent_depth / font.units_per_em}em`;
    }
    svg.setAttribute('aria-label', character);
    svg.dataset.aribGaijiText = character;
    svg.style.display = 'inline-block';
    svg.style.overflow = 'visible';
    const path = document.createElementNS(SVG_NAMESPACE, 'path');
    path.setAttribute('d', glyph.path);
    path.setAttribute(
        'transform',
        is_vertical ?
            `translate(${font.units_per_em / 2 - glyph.vertical_origin_x} ${glyph.vertical_origin_y}) scale(1 -1)` :
            `translate(${-font.horizontal_origin_x} ${font.ascent + font.horizontal_origin_y}) scale(1 -1)`,
    );
    path.setAttribute('fill', 'currentColor');
    svg.appendChild(path);
    return svg;
}


function appendRenderedText(
    value: string,
    destination: HTMLElement,
    context: StyleContext,
): void {
    const logical_font_family = getInheritedLogicalFontFamily(destination);
    const is_vertical = getInheritedWritingMode(destination).startsWith('vertical');
    let pending_text = '';
    const flush_text = () => {
        if (pending_text === '') return;
        destination.appendChild(document.createTextNode(pending_text));
        pending_text = '';
    };
    for (const character of Array.from(value)) {
        const code_point = character.codePointAt(0)!;
        const candidates = context.external_fonts.filter((font) =>
            normalizeFontFamilyName(font.family) === logical_font_family &&
            font.glyphs.get(code_point)?.some((glyph) =>
                glyph.orientation === null || glyph.orientation === (is_vertical ? 'v' : 'h')
            ) === true && font.ranges.some(([begin, end]) => begin <= code_point && code_point <= end)
        );
        const font = candidates[0];
        const glyph = font?.glyphs.get(code_point)?.find((candidate) =>
            candidate.orientation === null || candidate.orientation === (is_vertical ? 'v' : 'h')
        );
        if (font === undefined || glyph === undefined) {
            pending_text += character;
            continue;
        }
        flush_text();
        destination.appendChild(createExternalGlyph(character, font, glyph, is_vertical));
    }
    flush_text();
}


function appendTextContent(
    source: Element,
    destination: HTMLElement,
    context: StyleContext,
    options: ARIBTTMLRendererOptions,
    parent_timing: ElementTiming,
    timed_elements: TimedHTMLElement[],
    animated_elements: AnimatedHTMLElement[],
    depth: number = 0,
): void {
    // 放送入力で異常に深いXMLを受けても、再帰でメインスレッドを落とさない。
    if (depth > 64) return;
    for (const node of [...source.childNodes]) {
        if (node.nodeType === Node.TEXT_NODE) {
            let scale_source: HTMLElement | null = destination;
            let scale_x = 1;
            while (scale_source !== null) {
                const value = Number.parseFloat(scale_source.style.getPropertyValue('--arib-font-scale-x'));
                if (Number.isFinite(value) && value > 0) {
                    scale_x = value;
                    break;
                }
                scale_source = scale_source.parentElement;
            }
            if (scale_x === 1) {
                appendRenderedText(node.nodeValue ?? '', destination, context);
            } else {
                // fontSizeの2値指定は「幅 高さ」。font-stretchはフォントface選択であり幾何変換では
                // ないため、glyphを実際に横縮小し、描画後にlayout advanceも同じ比率へ詰める。
                const scaled_text = document.createElement('span');
                scaled_text.style.display = 'inline-block';
                scaled_text.style.transform = `scaleX(${scale_x})`;
                scaled_text.style.transformOrigin = 'left center';
                // 横書きのletter-spacingは文字幅と別のpx値なので、glyphだけを横縮小しても
                // 指定値を維持する。transform前に逆倍率を掛け、描画後の実寸を元のpxへ戻す。
                if (getInheritedWritingMode(destination).startsWith('vertical') === false) {
                    const letter_spacing = getInheritedPixelLetterSpacing(destination);
                    if (letter_spacing !== null) scaled_text.style.letterSpacing = `${letter_spacing / scale_x}px`;
                }
                scaled_text.dataset.aribFontScaleX = String(scale_x);
                destination.appendChild(scaled_text);
                appendRenderedText(node.nodeValue ?? '', scaled_text, context);
            }
            continue;
        }
        if (node.nodeType !== Node.ELEMENT_NODE) continue;
        const child = node as Element;
        if (child.localName === 'br' && (child.namespaceURI === TTML_NAMESPACE || child.namespaceURI === null)) {
            destination.appendChild(document.createElement('br'));
            continue;
        }
        if (child.localName === 'audio' && ARIB_TTML_NAMESPACES.has(child.namespaceURI ?? '')) continue;

        const child_id = getXMLID(child);
        const html_element = document.createElement(child.localName === 'span' ? 'span' : 'div');
        if (child_id !== null) {
            html_element.dataset.aribTtmlId = child_id;
        }
        applyARIBRubyMetadata([child], html_element, context);
        const child_region_id = child.getAttribute('region');
        const child_region = child_region_id === null ? undefined : context.regions.get(child_region_id);
        if (child_region !== undefined) {
            html_element.style.position = 'absolute';
            applyStyle(html_element, resolveStyle(child_region, context, options));
        }
        applyStyle(html_element, resolveStyle(child, context, options));
        // TR-B39ではoverflow属性を運用せず、独立regionの表示域外を常にマスクする。
        if (child_region !== undefined) html_element.style.overflow = 'hidden';
        // 親の font / writing-mode / color を外字・animation基底値へ継承できるよう、
        // 子孫を処理する前にDOM treeへ接続する。
        destination.appendChild(html_element);
        adjustRegionPosition(html_element, getInheritedWritingMode(destination));
        const timing = computeTiming(child, parent_timing);
        if (timing.begin !== parent_timing.begin || timing.end !== parent_timing.end) {
            timed_elements.push({element: html_element, begin: timing.begin, end: timing.end});
        }
        if (html_element.style.animationName !== '') {
            const delay = parseCSSSeconds(html_element.style.animationDelay);
            setAnimationBaseStyle(html_element);
            html_element.style.animationPlayState = 'paused';
            html_element.style.animationFillMode ||= 'both';
            animated_elements.push({element: html_element, delay, begin: timing.begin});
        }
        appendTextContent(child, html_element, context, options, timing, timed_elements, animated_elements, depth + 1);
    }
}


function adjustScaledFontLayout(root: HTMLElement): void {
    for (const element of [...root.querySelectorAll<HTMLElement>('[data-arib-font-scale-x]')]) {
        const scale_x = Number.parseFloat(element.dataset.aribFontScaleX ?? '1');
        if (Number.isFinite(scale_x) === false || scale_x <= 0 || scale_x === 1) continue;
        const unscaled_width = element.offsetWidth;
        if (getComputedStyle(element).writingMode.startsWith('vertical')) {
            element.style.marginLeft = `${-(unscaled_width * (1 - scale_x)) / 2}px`;
            element.style.marginRight = element.style.marginLeft;
        } else {
            element.style.marginRight = `${-unscaled_width * (1 - scale_x)}px`;
        }
    }
}


/** 表示対象の文字・画像・音声が有効になるTTML時刻を、親子の相対時刻を解決して列挙する。 */
function collectContentTimings(
    source: Element,
    parent_timing: ElementTiming,
    context: StyleContext,
    result: ElementTiming[],
    depth: number = 0,
): void {
    if (depth > 64) return;
    const timing = computeTiming(source, parent_timing);
    const has_direct_text = [...source.childNodes].some((node) =>
        node.nodeType === Node.TEXT_NODE && /\S/u.test(node.nodeValue ?? '')
    );
    const background_image = [...source.attributes].find((attribute) =>
        SMPTE_NAMESPACES.has(attribute.namespaceURI ?? '') && attribute.localName === 'backgroundImage'
    );
    const has_background_image = background_image !== undefined &&
        resolveResourceURL(background_image.value, context) !== null;
    const audio_source = source.localName === 'audio' && ARIB_TTML_NAMESPACES.has(source.namespaceURI ?? '') ?
        source.getAttribute('src') : null;
    const has_audio = audio_source !== null && (
        audio_source.startsWith('romsound://') || resolveResourceURL(audio_source, context) !== null
    );
    if (has_direct_text || has_background_image || has_audio) result.push(timing);
    for (const child of [...source.children]) {
        collectContentTimings(child, timing, context, result, depth + 1);
    }
}


/** DOMが未接続のfallbackでも、ルビの読みを親文字へ重複連結しない本文を作る。 */
function getTTMLTextContent(root: Element, context: StyleContext): string | null {
    const collect = (node: Node, depth: number): string => {
        if (depth > 64) return '';
        if (node.nodeType === Node.TEXT_NODE) return node.nodeValue ?? '';
        if (node.nodeType !== Node.ELEMENT_NODE) return '';
        const element = node as Element;
        if (context.arib_ruby_annotations.has(element)) return '';
        if (element.localName === 'br' && (element.namespaceURI === TTML_NAMESPACE || element.namespaceURI === null)) {
            return '\n';
        }
        return [...element.childNodes].map((child) => collect(child, depth + 1)).join('');
    };
    return collect(root, 0).replace(/\s+/gu, ' ').trim() || null;
}


function haveEqualResources(
    left: ReadonlyMap<number, ARIBTTMLResource>,
    right: ReadonlyMap<number, ARIBTTMLResource>,
): boolean {
    if (left.size !== right.size) return false;
    for (const [index, left_resource] of left) {
        const right_resource = right.get(index);
        if (
            right_resource === undefined || left_resource.data_type !== right_resource.data_type ||
            left_resource.data.length !== right_resource.data.length ||
            left_resource.data.some((value, offset) => value !== right_resource.data[offset])
        ) {
            return false;
        }
    }
    return true;
}


function isSamePresentationUnit(left: ARIBTTMLPresentationUnit, right: ARIBTTMLPresentationUnit): boolean {
    return left.pts === right.pts &&
        left.transport_timestamp === right.transport_timestamp &&
        left.component_tag === right.component_tag &&
        left.subtitle_tag === right.subtitle_tag &&
        left.subtitle_sequence_number === right.subtitle_sequence_number &&
        left.ttml === right.ttml &&
        haveEqualResources(left.resources, right.resources);
}


function getVisibleTextContent(root: HTMLElement): string | null {
    const collect = (node: Node, ancestor_is_hidden: boolean): string => {
        if (node.nodeType === Node.TEXT_NODE) return ancestor_is_hidden ? '' : node.nodeValue ?? '';
        if (node.nodeType !== Node.ELEMENT_NODE) return '';
        const element = node as HTMLElement;
        const is_hidden = ancestor_is_hidden || element.localName === 'style' ||
            element.style.display === 'none' || element.style.visibility === 'hidden';
        if (is_hidden) return '';
        // ARIBルビの読みは別regionで可視表示したまま、字幕本文の抽出では親文字と重複させない。
        if (element.dataset.aribTtmlRubyFor !== undefined) return '';
        if (element.dataset.aribGaijiText !== undefined) return element.dataset.aribGaijiText;
        if (element.localName === 'br') return '\n';
        return [...element.childNodes].map((child) => collect(child, false)).join('');
    };
    return collect(root, false).replace(/\s+/gu, ' ').trim() || null;
}


function collectElementsByID(root: Document, local_name: string): Map<string, Element> {
    const result = new Map<string, Element>();
    for (const element of [...root.getElementsByTagNameNS('*', local_name)]) {
        const id = getXMLID(element);
        if (id !== null) result.set(id, element);
    }
    return result;
}


function parseUnicodeRanges(value: string | null): [number, number][] | null {
    if (value === null) return null;
    const ranges: [number, number][] = [];
    let code_point_count = 0;
    for (const item of value.split(',')) {
        const match = item.trim().match(/^U\+([0-9A-F]{1,6})(?:-([0-9A-F]{1,6}))?$/i);
        if (match === null) return null;
        const begin = Number.parseInt(match[1], 16);
        const end = Number.parseInt(match[2] ?? match[1], 16);
        if (begin > end || end > 0x10FFFF) return null;
        code_point_count += end - begin + 1;
        if (code_point_count > 188) return null;
        ranges.push([begin, end]);
    }
    return ranges.length > 0 ? ranges : null;
}


function parseSVGNumber(value: string | null, fallback: number): number | null {
    if (value === null) return fallback;
    if (/^-?\d+(?:\.\d+)?$/.test(value.trim()) === false) return null;
    const parsed = Number.parseFloat(value);
    return Number.isFinite(parsed) ? parsed : null;
}


/** SVG 1.1 Fontからscript等を一切取り込まず、外字glyphのpath数値だけを抽出する。 */
function parseExternalSVGFont(
    data: Uint8Array,
    family: string,
    ranges: [number, number][],
    max_glyph_count: number,
): ExternalFontDefinition | null {
    if (max_glyph_count <= 0) return null;
    let source: string;
    try {
        source = new TextDecoder('utf-8', {fatal: true}).decode(data);
    } catch (error) {
        return null;
    }
    if (/<!DOCTYPE/i.test(source)) return null;
    const xml = new DOMParser().parseFromString(source, 'image/svg+xml');
    if (xml.getElementsByTagName('parsererror').length > 0) return null;
    const font = xml.getElementsByTagNameNS('*', 'font')[0];
    const font_face = xml.getElementsByTagNameNS('*', 'font-face')[0];
    if (font === undefined || font_face === undefined) return null;
    const units_per_em = parseSVGNumber(font_face.getAttribute('units-per-em'), 1000);
    const ascent = parseSVGNumber(font_face.getAttribute('ascent'), units_per_em ?? 1000);
    const raw_descent = parseSVGNumber(font_face.getAttribute('descent'), 0);
    const default_advance = parseSVGNumber(font.getAttribute('horiz-adv-x'), 0);
    const horizontal_origin_x = parseSVGNumber(font.getAttribute('horiz-origin-x'), 0);
    const horizontal_origin_y = parseSVGNumber(font.getAttribute('horiz-origin-y'), 0);
    if (
        units_per_em === null || units_per_em <= 0 || units_per_em > 100_000 ||
        ascent === null || ascent <= 0 || raw_descent === null ||
        default_advance === null || default_advance <= 0 ||
        horizontal_origin_x === null || horizontal_origin_y === null
    ) {
        return null;
    }
    const descent_depth = Math.abs(raw_descent);
    const vertical_origin_x = parseSVGNumber(font.getAttribute('vert-origin-x'), default_advance / 2);
    const vertical_origin_y = parseSVGNumber(font.getAttribute('vert-origin-y'), ascent);
    const vertical_advance = parseSVGNumber(font.getAttribute('vert-adv-y'), units_per_em);
    const metric_limit = units_per_em * 10;
    if (
        Math.abs(ascent) > metric_limit || descent_depth > metric_limit ||
        Math.abs(horizontal_origin_x) > metric_limit || Math.abs(horizontal_origin_y) > metric_limit ||
        vertical_origin_x === null || Math.abs(vertical_origin_x) > metric_limit ||
        vertical_origin_y === null || Math.abs(vertical_origin_y) > metric_limit ||
        vertical_advance === null || vertical_advance <= 0 || vertical_advance > metric_limit
    ) return null;

    const glyphs = new Map<number, ExternalFontGlyph[]>();
    for (const glyph of [...font.getElementsByTagNameNS('*', 'glyph')].slice(0, Math.min(188, max_glyph_count))) {
        const unicode = glyph.getAttribute('unicode');
        const characters = unicode === null ? [] : Array.from(unicode);
        const path = glyph.getAttribute('d');
        const horizontal_advance = parseSVGNumber(glyph.getAttribute('horiz-adv-x'), default_advance);
        const glyph_vertical_origin_x = parseSVGNumber(glyph.getAttribute('vert-origin-x'), vertical_origin_x);
        const glyph_vertical_origin_y = parseSVGNumber(glyph.getAttribute('vert-origin-y'), vertical_origin_y);
        const glyph_vertical_advance = parseSVGNumber(glyph.getAttribute('vert-adv-y'), vertical_advance);
        const orientation_value = glyph.getAttribute('orientation');
        const orientation = orientation_value === null ? null :
            orientation_value === 'h' || orientation_value === 'v' ? orientation_value : undefined;
        if (
            characters.length !== 1 || path === null || path.length > 128 * 1024 ||
            /^[MmZzLlHhVvCcSsQqTtAa0-9+.,eE\s-]+$/.test(path) === false ||
            horizontal_advance === null || horizontal_advance <= 0 || horizontal_advance > metric_limit ||
            glyph_vertical_origin_x === null || Math.abs(glyph_vertical_origin_x) > metric_limit ||
            glyph_vertical_origin_y === null || Math.abs(glyph_vertical_origin_y) > metric_limit ||
            glyph_vertical_advance === null || glyph_vertical_advance <= 0 || glyph_vertical_advance > metric_limit ||
            orientation === undefined
        ) {
            continue;
        }
        const code_point = characters[0].codePointAt(0)!;
        if (ranges.some(([begin, end]) => begin <= code_point && code_point <= end)) {
            const definitions = glyphs.get(code_point) ?? [];
            definitions.push({
                path,
                horizontal_advance,
                vertical_origin_x: glyph_vertical_origin_x,
                vertical_origin_y: glyph_vertical_origin_y,
                vertical_advance: glyph_vertical_advance,
                orientation,
            });
            glyphs.set(code_point, definitions);
        }
    }
    if (glyphs.size === 0) return null;
    return {
        family,
        ranges,
        units_per_em,
        ascent,
        descent_depth,
        horizontal_origin_x,
        horizontal_origin_y,
        glyphs,
    };
}


function buildAnimationStyles(xml: Document, context: StyleContext): void {
    for (const keyframes of [...xml.getElementsByTagNameNS('*', 'keyframes')]) {
        if (ARIB_TTML_NAMESPACES.has(keyframes.namespaceURI ?? '') === false) continue;
        const source_name = keyframes.getAttribute('animationName');
        if (source_name === null) continue;
        const keyframe_elements = [...keyframes.children];
        if (
            keyframe_elements.length < 2 || keyframe_elements.length > 10 ||
            keyframe_elements.some((element) => element.localName !== 'keyframe')
        ) continue;

        const parsed_frames: {position: number; properties: CSSProperties}[] = [];
        let is_valid = true;
        for (const keyframe of keyframe_elements) {
            const position_value = keyframe.getAttribute('position');
            const position_match = position_value?.match(/^(\d+(?:\.\d+)?)%$/) ?? null;
            const position = position_match === null ? Number.NaN : Number.parseFloat(position_match[1]);
            if (Number.isFinite(position) === false || position < 0 || position > 100) {
                is_valid = false;
                break;
            }

            const properties: CSSProperties = {};
            const background_color_value = keyframe.getAttributeNS(TTML_STYLE_NAMESPACE, 'backgroundColor');
            const color_value = keyframe.getAttributeNS(TTML_STYLE_NAMESPACE, 'color');
            const opacity_value = keyframe.getAttributeNS(TTML_STYLE_NAMESPACE, 'opacity');
            const origin_value = keyframe.getAttributeNS(TTML_STYLE_NAMESPACE, 'origin');
            const background_color = parseARIBColor(background_color_value);
            const color = parseARIBColor(color_value);
            const opacity = parseARIBOpacity(opacity_value);
            const origin = parseLengthPair(origin_value);
            if (
                (background_color_value !== null && background_color === null) ||
                (color_value !== null && color === null) ||
                (opacity_value !== null && opacity === null) ||
                (origin_value !== null && origin === null)
            ) {
                is_valid = false;
                break;
            }
            if (background_color !== null) properties['background-color'] = background_color;
            if (color !== null) properties.color = color;
            if (opacity !== null) properties.opacity = opacity;
            if (origin !== null) {
                properties.left = `${origin[0]}px`;
                properties.top = `${origin[1]}px`;
            }
            parsed_frames.push({position, properties});
        }
        if (
            is_valid === false || parsed_frames[0].position !== 0 ||
            parsed_frames[parsed_frames.length - 1].position !== 100 ||
            parsed_frames.some((frame, index) => index > 0 && frame.position <= parsed_frames[index - 1].position)
        ) continue;

        const target_name = `arib_ttml_${context.unit_order}_${context.animation_names.size}_${
            sanitizeCSSIdentifier(source_name)
        }`;
        context.animation_names.set(source_name, target_name);
        const frames: string[] = [];
        const inherited_properties: CSSProperties = {
            'background-color': 'var(--arib-animation-base-background-color)',
            color: 'var(--arib-animation-base-color)',
            opacity: 'var(--arib-animation-base-opacity)',
            left: 'var(--arib-animation-base-origin-x)',
            top: 'var(--arib-animation-base-origin-y)',
        };
        for (const frame of parsed_frames) {
            // ARIBでは省略属性をCSSの補間に任せず、直前keyframeの値を継続する。
            Object.assign(inherited_properties, frame.properties);
            const declarations = Object.entries(inherited_properties).map(([property, value]) => {
                if (property === 'left') return `left:calc(${value} - var(--arib-vertical-origin-offset, 0px) - var(--arib-containing-plane-left, 0px))`;
                if (property === 'top') return `top:calc(${value} - var(--arib-containing-plane-top, 0px))`;
                return `${property}:${value}`;
            }).join(';');
            frames.push(`${frame.position}%{${declarations}}`);
        }
        context.animation_styles.push(`@keyframes ${target_name}{${frames.join('')}}`);
    }
}


function parseTTMLDocument(
    unit: ARIBTTMLPresentationUnit,
    options: ARIBTTMLRendererOptions,
): ParsedTTMLDocument | null {
    if (
        unit.ttml.length > MAX_TTML_DOCUMENT_SIZE ||
        /<!DOCTYPE/i.test(unit.ttml)
    ) {
        return null;
    }
    const xml = new DOMParser().parseFromString(unit.ttml, 'application/xml');
    if (xml.getElementsByTagName('parsererror').length > 0 || xml.documentElement.localName !== 'tt') return null;

    const root_extent = parseLengthPair(xml.documentElement.getAttributeNS(TTML_STYLE_NAMESPACE, 'extent'));
    const resolution = unit.additional_info.resolution;
    const default_plane = resolution === 1 ? [3840, 2160] : [1920, 1080];
    const plane_width = root_extent?.[0] ?? default_plane[0];
    const plane_height = root_extent?.[1] ?? default_plane[1];
    const body = [...xml.documentElement.children].find((element) => element.localName === 'body');
    if (body === undefined) {
        return {
            root: null,
            is_clear: true,
            plane_width,
            plane_height,
            base_time: 0,
            end_time: Number.POSITIVE_INFINITY,
            timed_elements: [],
            animated_elements: [],
            audio_sources: [],
            content_timings: [],
            text_content: null,
        };
    }
    const styles = collectElementsByID(xml, 'style');
    const regions = collectElementsByID(xml, 'region');
    const arib_ruby_relations = collectARIBRubyRelations(body, styles, regions);

    const resource_urls = new Map<number, string>();
    for (const [index, resource] of unit.resources) {
        const url = resourceToDataURL(resource);
        if (url !== null) resource_urls.set(index, url);
    }
    const embedded_images = new Map<string, string>();
    for (const smpte_namespace of SMPTE_NAMESPACES) {
        for (const image of [...xml.getElementsByTagNameNS(smpte_namespace, 'image')]) {
            const id = getXMLID(image);
            if (id === null || image.getAttribute('encoding')?.toLowerCase() !== 'base64') continue;
            const image_type = image.getAttribute('imageType')?.toLowerCase();
            if (image_type !== 'png') continue;
            embedded_images.set(id, `data:image/png;base64,${(image.textContent ?? '').replace(/\s+/g, '')}`);
        }
    }
    const font_face_elements = [...xml.getElementsByTagNameNS('*', 'font-face')].filter((element) =>
        ARIB_TTML_NAMESPACES.has(element.namespaceURI ?? '')
    ).slice(0, 5);
    const external_fonts: ExternalFontDefinition[] = [];
    const external_font_families = new Set<string>();
    const accepted_woff_font_faces = new Set<Element>();
    let external_glyph_count = 0;
    for (const font_face of font_face_elements) {
        const family = font_face.getAttribute('font-family')?.trim();
        const ranges = parseUnicodeRanges(font_face.getAttribute('unicode-range'));
        const source = [...font_face.children].find((element) =>
            element.localName === 'src' && ARIB_TTML_NAMESPACES.has(element.namespaceURI ?? '')
        );
        const source_url = source?.getAttribute('url')?.trim();
        const source_format = source?.getAttribute('format')?.toLowerCase() ?? 'svg';
        const resource_match = source_url?.match(/^subt:\/\/(\d+)$/);
        if (
            family === undefined || family === '' || ranges === null ||
            resource_match === null || resource_match === undefined
        ) continue;
        const resource = unit.resources.get(Number(resource_match[1]));
        if (resource === undefined) continue;
        if (resource.data_type === 0x06 && source_format === 'svg') {
            const definition = parseExternalSVGFont(resource.data, family, ranges, 188 - external_glyph_count);
            if (definition !== null) {
                external_fonts.push(definition);
                external_font_families.add(family);
                external_glyph_count += [...definition.glyphs.values()].reduce(
                    (count, definitions) => count + definitions.length,
                    0,
                );
            }
        } else if (
            resource.data_type === 0x07 && source_format === 'woff' &&
            ranges.reduce((count, [begin, end]) => count + end - begin + 1, 0) <= 188 - external_glyph_count
        ) {
            external_font_families.add(family);
            external_glyph_count += ranges.reduce((count, [begin, end]) => count + end - begin + 1, 0);
            accepted_woff_font_faces.add(font_face);
        }
    }
    const context: StyleContext = {
        styles,
        regions,
        arib_ruby_annotations: arib_ruby_relations.annotations,
        arib_ruby_bases: arib_ruby_relations.bases,
        embedded_images,
        resource_urls,
        animation_names: new Map(),
        animation_styles: [],
        external_fonts,
        external_font_families,
        unit_order: unit.order,
    };
    buildAnimationStyles(xml, context);

    const root = document.createElement('div');
    root.setAttribute('xmlns', XHTML_NAMESPACE);
    root.style.position = 'relative';
    root.style.width = `${plane_width}px`;
    root.style.height = `${plane_height}px`;
    root.style.overflow = 'hidden';
    root.style.transformOrigin = 'left top';
    root.style.transform = `scale(${LOGICAL_VIDEO_WIDTH / plane_width}, ${LOGICAL_VIDEO_HEIGHT / plane_height})`;
    root.style.color = 'white';
    root.style.backgroundColor = 'transparent';
    root.style.fontFamily = options.normal_font ?? 'sans-serif';
    root.style.setProperty('--arib-logical-font-family', 'round gothic');

    if (context.animation_styles.length > 0) {
        const style = document.createElement('style');
        style.textContent = context.animation_styles.join('');
        root.appendChild(style);
    }

    // 同一regionを参照するpも別々の時刻を持つため、各pに独立した絶対配置wrapperを作る。
    const paragraphs = [...body.getElementsByTagNameNS(TTML_NAMESPACE, 'p')];
    const image_sources = [body, ...body.getElementsByTagNameNS('*', '*')].filter((element) => {
        const has_background_image = [...element.attributes].some((attribute) =>
            SMPTE_NAMESPACES.has(attribute.namespaceURI ?? '') && attribute.localName === 'backgroundImage' &&
            resolveResourceURL(attribute.value, context) !== null
        );
        if (has_background_image === false) return false;
        // p内部の画像はpと一緒に描画し、pを内包するdivも二重描画しない。
        let ancestor = element.parentElement;
        while (ancestor !== null && ancestor !== body) {
            if (ancestor.localName === 'p' && ancestor.namespaceURI === TTML_NAMESPACE) return false;
            ancestor = ancestor.parentElement;
        }
        return element.getElementsByTagNameNS(TTML_NAMESPACE, 'p').length === 0;
    });
    const paragraph_sources = [...new Set<Element>([...paragraphs, ...image_sources])].sort((left, right) =>
        left === right ? 0 : left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1
    );
    if (paragraph_sources.length === 0) paragraph_sources.push(body);
    const timed_elements: TimedHTMLElement[] = [];
    const animated_elements: AnimatedHTMLElement[] = [];
    for (const paragraph of paragraph_sources) {
        const ancestors: Element[] = [];
        let ancestor: Element | null = paragraph;
        while (ancestor !== null && ancestor !== xml.documentElement) {
            ancestors.unshift(ancestor);
            ancestor = ancestor.parentElement;
        }

        let timing: ElementTiming = {begin: 0, end: Number.POSITIVE_INFINITY};
        for (const timed_ancestor of ancestors) timing = computeTiming(timed_ancestor, timing);
        const region_id = [...ancestors].reverse().map((element) => element.getAttribute('region')).find(Boolean) ?? null;
        const region = region_id === null ? undefined : context.regions.get(region_id);
        const wrapper = document.createElement('div');
        wrapper.style.position = 'absolute';
        wrapper.style.left = '0';
        wrapper.style.top = '0';
        wrapper.style.width = `${plane_width}px`;
        wrapper.style.height = `${plane_height}px`;
        wrapper.style.overflow = 'hidden';
        if (region !== undefined) applyStyle(wrapper, resolveStyle(region, context, options));
        for (const styled_ancestor of ancestors) applyStyle(wrapper, resolveStyle(styled_ancestor, context, options));
        // p / div はwrapperへ平坦化されるため、この経路でもルビの意味リンクを保持する。
        applyARIBRubyMetadata(ancestors, wrapper, context);
        // TR-B39の運用ではregionの外側を常にマスクする。非運用のoverflow指定では解除させない。
        wrapper.style.overflow = 'hidden';
        // 運用例のようにwritingModeがspan側だけに指定される場合も、regionのorigin解釈は同じ。
        if (wrapper.style.writingMode === '') {
            const vertical_source = [...paragraph.getElementsByTagNameNS(TTML_NAMESPACE, 'span')].find((span) => {
                const span_region_id = span.getAttribute('region');
                const span_region = span_region_id === null ? undefined : context.regions.get(span_region_id);
                const effective_style: CSSProperties = {};
                if (span_region !== undefined) Object.assign(effective_style, resolveStyle(span_region, context, options));
                Object.assign(effective_style, resolveStyle(span, context, options));
                return effective_style['writing-mode'] === 'vertical-rl';
            });
            if (vertical_source !== undefined) wrapper.style.writingMode = 'vertical-rl';
        }
        root.appendChild(wrapper);
        adjustRegionPosition(wrapper, '', region !== undefined);
        timed_elements.push({element: wrapper, begin: timing.begin, end: timing.end});
        if (wrapper.style.animationName !== '') {
            const delay = parseCSSSeconds(wrapper.style.animationDelay);
            setAnimationBaseStyle(wrapper);
            wrapper.style.animationPlayState = 'paused';
            wrapper.style.animationFillMode ||= 'both';
            animated_elements.push({element: wrapper, delay, begin: timing.begin});
        }
        appendTextContent(paragraph, wrapper, context, options, timing, timed_elements, animated_elements);
    }

    // 現行運用ではPNG resourceを smpte:backgroundImage から参照する。
    // 非組み込みSVG font / WOFFも同じMPUの subt:// URLだけを @font-face へ展開する。
    const font_face_rules: string[] = [];
    for (const font_face of font_face_elements) {
        if (accepted_woff_font_faces.has(font_face) === false) continue;
        const family = font_face.getAttribute('font-family');
        const ranges = parseUnicodeRanges(font_face.getAttribute('unicode-range'));
        const source = [...font_face.children].find((element) =>
            element.localName === 'src' && ARIB_TTML_NAMESPACES.has(element.namespaceURI ?? '')
        );
        const source_url = source?.getAttribute('url')?.trim();
        const resource_match = source_url?.match(/^subt:\/\/(\d+)$/);
        if (
            family === null || ranges === null || source_url === undefined ||
            resource_match === null || resource_match === undefined
        ) continue;
        const resource = unit.resources.get(Number(resource_match[1]));
        if (resource?.data_type !== 0x07 || source?.getAttribute('format')?.toLowerCase() !== 'woff') continue;
        const resolved_url = resolveResourceURL(source_url, context);
        if (resolved_url === null) continue;
        const unicode_range = ranges.map(([begin, end]) =>
            begin === end ? `U+${begin.toString(16).toUpperCase()}` :
                `U+${begin.toString(16).toUpperCase()}-${end.toString(16).toUpperCase()}`
        ).join(',');
        const safe_family = family.replace(/["\\\r\n]/g, '');
        font_face_rules.push(
            `@font-face{font-family:"${safe_family}";src:url("${resolved_url}") format("woff");unicode-range:${unicode_range}}`,
        );
    }
    if (font_face_rules.length > 0) {
        const style = document.createElement('style');
        style.textContent = font_face_rules.join('');
        root.prepend(style);
    }

    const audio_sources: TimedAudioSource[] = [];
    for (const audio of [...xml.getElementsByTagNameNS('*', 'audio')]) {
        if (ARIB_TTML_NAMESPACES.has(audio.namespaceURI ?? '') === false) continue;
        const source = audio.getAttribute('src');
        if (source === null) continue;
        const resolved_source = source.startsWith('romsound://') ? source : resolveResourceURL(source, context);
        if (resolved_source !== null) {
            const timing = computeElementTiming(audio, body);
            audio_sources.push({
                src: resolved_source,
                loop: ['1', 'true'].includes(audio.getAttribute('loop')?.toLowerCase() ?? ''),
                begin: timing.begin,
                end: timing.end,
            });
        }
    }

    const content_timings: ElementTiming[] = [];
    collectContentTimings(body, {begin: 0, end: Number.POSITIVE_INFINITY}, context, content_timings);
    const finite_begins = content_timings.map((timing) => timing.begin).filter(Number.isFinite);
    const finite_ends = content_timings.map((timing) => timing.end).filter(Number.isFinite);
    const base_time = finite_begins.length > 0 ? Math.min(...finite_begins) : 0;
    const end_time = content_timings.some((timing) => Number.isFinite(timing.end) === false) ?
        Number.POSITIVE_INFINITY : finite_ends.length > 0 ? Math.max(...finite_ends) : Number.POSITIVE_INFINITY;
    const text_content = getTTMLTextContent(body, context);
    // STD-B62の空TTMLに加え、STD-B69の従来字幕変換で生成される空spanもクリアとして扱う。
    // 画像だけ・音声だけの文書はcontent_timingsへ入るため、誤ってクリアにはならない。
    const is_clear = content_timings.length === 0;
    return {
        root: is_clear ? null : root,
        is_clear,
        plane_width,
        plane_height,
        base_time,
        end_time,
        timed_elements,
        animated_elements,
        audio_sources,
        content_timings,
        text_content,
    };
}


/**
 * ARIB-TTML の字幕・文字スーパーを同じコードで描画する。
 * 0x30..0x37 と 0x38..0x3F はデコーダー内部で選別し、独立した二層へ出力する。
 */
export default class ARIBTTMLRenderer {

    private readonly decoder = new ARIBTTMLDecoder();
    private readonly options: ARIBTTMLRendererOptions;
    private media: HTMLVideoElement | null = null;
    private container: HTMLElement | null = null;
    private svg: SVGSVGElement | null = null;
    private caption_state: ComponentState | null = null;
    private superimpose_state: ComponentState | null = null;
    private preferred_caption_component_tag = 0x30;
    private preferred_superimpose_component_tag = 0x38;
    private preferred_caption_language: 'auto' | 'jpn' | 'eng';
    // TR-B39 7.2.3 の同時再生可否を字幕・文字スーパーの両レイヤーで共通判定する。
    // callback待ちの内蔵音も予約時点から数え、最大2音を越えないようにする。
    private readonly active_auxiliary_audio = new Set<AuxiliaryAudioRegistration>();
    private animation_frame_id: number | null = null;
    private readonly media_play_handler = (): void => this.renderCurrentTime();
    private readonly media_pause_handler = (): void => this.pauseAudio();
    private readonly media_seeking_handler = (): void => {
        this.stopAudio(undefined, true);
        this.renderCurrentTime();
    };

    public constructor(options: ARIBTTMLRendererOptions = {}) {
        this.options = options;
        this.preferred_caption_language = options.preferred_caption_language ?? 'auto';
    }

    public attachMedia(media: HTMLVideoElement, container?: HTMLElement): void {
        this.detachMedia();
        // 画質切り替え・ライブ再接続を跨いだMFU断片が、新しいTSの断片と混ざらないようにする。
        this.decoder.reset();
        this.media = media;
        this.container = container ?? media.parentElement;
        if (this.container === null) return;

        this.svg = document.createElementNS(SVG_NAMESPACE, 'svg');
        this.svg.setAttribute('viewBox', `0 0 ${LOGICAL_VIDEO_WIDTH} ${LOGICAL_VIDEO_HEIGHT}`);
        this.svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
        this.svg.style.position = 'absolute';
        this.svg.style.inset = '0';
        this.svg.style.width = '100%';
        this.svg.style.height = '100%';
        this.svg.style.pointerEvents = 'none';
        this.svg.style.overflow = 'hidden';
        this.svg.style.zIndex = '1';
        this.svg.dataset.aribTtmlRenderer = 'true';

        const create_state = (component: ARIBTTMLComponent): ComponentState => {
            const foreign_object = document.createElementNS(SVG_NAMESPACE, 'foreignObject');
            foreign_object.setAttribute('x', '0');
            foreign_object.setAttribute('y', '0');
            foreign_object.setAttribute('width', String(LOGICAL_VIDEO_WIDTH));
            foreign_object.setAttribute('height', String(LOGICAL_VIDEO_HEIGHT));
            foreign_object.dataset.aribTtmlComponent = component;
            const host = document.createElementNS(XHTML_NAMESPACE, 'div') as HTMLDivElement;
            host.setAttribute('xmlns', XHTML_NAMESPACE);
            host.style.position = 'relative';
            host.style.width = `${LOGICAL_VIDEO_WIDTH}px`;
            host.style.height = `${LOGICAL_VIDEO_HEIGHT}px`;
            host.style.overflow = 'hidden';
            // visibility:hidden は TTML 内部の visibility:visible で子要素だけ再表示できてしまう。
            // 字幕レイヤー全体は、子孫から解除できず非表示中もレイアウトを計測できる opacity で制御する。
            host.style.opacity = '0';
            foreign_object.appendChild(host);
            this.svg!.appendChild(foreign_object);
            return {
                host,
                cues_by_component: new Map(),
                current_cue: null,
                is_visible: true,
                has_visible_content: false,
                playing_audio: new Map(),
                playing_rom_sounds: new Map(),
                pending_rom_sounds: new Map(),
                triggered_audio: new Set(),
            };
        };
        this.caption_state = create_state('Caption');
        // SVGの後勝ち描画順により、文字スーパーを字幕より常に前面にする。
        this.superimpose_state = create_state('Superimpose');
        this.superimpose_state.is_visible = this.options.show_superimpose ?? true;
        this.container.appendChild(this.svg);
        this.media.addEventListener('play', this.media_play_handler);
        this.media.addEventListener('pause', this.media_pause_handler);
        this.media.addEventListener('seeking', this.media_seeking_handler);
        this.startRendering();
    }

    public detachMedia(): void {
        this.stopRendering();
        this.stopAudio(undefined, true);
        this.media?.removeEventListener('play', this.media_play_handler);
        this.media?.removeEventListener('pause', this.media_pause_handler);
        this.media?.removeEventListener('seeking', this.media_seeking_handler);
        if (this.svg !== null) this.svg.remove();
        this.svg = null;
        this.caption_state = null;
        this.superimpose_state = null;
        this.media = null;
        this.container = null;
    }

    public dispose(): void {
        this.detachMedia();
        this.decoder.reset();
    }

    /** シーク復元前に、transport断片と両レイヤーの既存cueをすべて破棄する。 */
    public reset(): void {
        this.decoder.reset();
        this.stopAudio(undefined, true);
        for (const state of [this.caption_state, this.superimpose_state]) {
            if (state === null) continue;
            state.cues_by_component.clear();
            state.current_cue = null;
            state.has_visible_content = false;
            state.host.replaceChildren();
        }
    }

    public pushID3v2Data(pts: number, data: Uint8Array, transport_timestamp: number | null = null): boolean {
        let accepted = false;
        for (const unit of this.decoder.pushID3v2Data(pts, data, transport_timestamp)) {
            const state = unit.component === 'Caption' ? this.caption_state : this.superimpose_state;
            if (state === null) continue;
            // 録画側は先読み範囲が重なるため、同じraw ID3が複数回届く。transport上同一の
            // presentation unitはDOMを作り直さず無視し、同一PTSの別文書は維持する。
            const component_cues = state.cues_by_component.get(unit.component_tag) ?? [];
            if (component_cues.some((cue) => isSamePresentationUnit(cue.unit, unit))) {
                accepted = true;
                continue;
            }
            const document = parseTTMLDocument(unit, this.options);
            if (document === null) continue;

            const controls_time = unit.additional_info.time_management_mode === 0x02;
            // TMD=0010のTTML時刻はreference_start_timeを起点とする。dantto4kのID3 PTSは
            // MFU受信時刻なので、元PTSとの絶対時刻差をmedia timelineへ加えて早着MFUを正しく待つ。
            const reference_start_time = unit.additional_info.reference_start_time;
            const nominal_start_time = controls_time && document.is_clear === false && reference_start_time !== null &&
                unit.transport_timestamp !== null ? unit.pts + getWrappedTimestampDelta(
                    reference_start_time + document.base_time,
                    unit.transport_timestamp,
                ) : unit.pts;
            // 遅着した更新文書は受信前へ遡って既存文書を上書きできない。表示内の経過時刻は
            // nominal基準を保ちつつ、cueの選択開始だけをMFU受信時刻以降へ丸める。
            const start_time = Math.max(unit.pts, nominal_start_time);
            const end_time = controls_time && Number.isFinite(document.end_time) ?
                nominal_start_time + Math.max(0, document.end_time - document.base_time) : Number.POSITIVE_INFINITY;
            const cue: PresentationCue = {
                unit,
                document,
                nominal_start_time,
                start_time,
                end_time,
            };
            component_cues.push(cue);
            component_cues.sort((left, right) =>
                left.start_time - right.start_time || left.unit.order - right.unit.order
            );
            if (component_cues.length > MAX_CUES_PER_COMPONENT) {
                component_cues.splice(0, component_cues.length - MAX_CUES_PER_COMPONENT);
            }
            state.cues_by_component.set(unit.component_tag, component_cues);
            accepted = true;
        }
        if (accepted) this.renderCurrentTime();
        return accepted;
    }

    public showCaption(): void {
        if (this.caption_state === null) return;
        this.caption_state.is_visible = true;
        this.renderCurrentTime();
    }

    public hideCaption(): void {
        if (this.caption_state === null) return;
        this.caption_state.is_visible = false;
        // renderCurrentTime() を待たず、字幕ボタンの操作と同じフレームで確実に隠す。
        this.caption_state.host.style.opacity = '0';
        this.stopAudio(this.caption_state, true);
        this.renderCurrentTime();
    }

    public setSuperimposeVisibility(is_visible: boolean): void {
        if (this.superimpose_state === null) return;
        this.superimpose_state.is_visible = is_visible;
        if (is_visible === false) {
            this.superimpose_state.host.style.opacity = '0';
            this.stopAudio(this.superimpose_state, true);
        }
        this.renderCurrentTime();
    }

    /** 二言語字幕では選択assetだけを描画する。未送出なら運用中の最小componentへフォールバックする。 */
    public setCaptionComponentTag(component_tag: number): void {
        if (component_tag < 0x30 || component_tag > 0x37) return;
        this.preferred_caption_component_tag = component_tag;
        if (this.caption_state !== null) {
            this.stopAudio(this.caption_state, true);
            this.caption_state.current_cue = null;
            this.caption_state.host.replaceChildren();
        }
        this.renderCurrentTime();
    }

    public setCaptionLanguage(language: 'auto' | 'jpn' | 'eng'): void {
        this.preferred_caption_language = language;
        if (this.caption_state !== null) {
            this.stopAudio(this.caption_state, true);
            this.caption_state.current_cue = null;
            this.caption_state.host.replaceChildren();
        }
        this.renderCurrentTime();
    }

    public isCaptionPresent(): boolean {
        return this.caption_state !== null &&
            this.caption_state.current_cue !== null &&
            this.isCueVisible(this.caption_state, this.caption_state.current_cue) &&
            this.caption_state.current_cue.document.is_clear === false &&
            this.caption_state.has_visible_content;
    }

    public isSuperimposePresent(): boolean {
        return this.superimpose_state !== null &&
            this.superimpose_state.current_cue !== null &&
            this.isCueVisible(this.superimpose_state, this.superimpose_state.current_cue) &&
            this.superimpose_state.current_cue.document.is_clear === false &&
            this.superimpose_state.has_visible_content;
    }

    public getCaptionTextContent(): string | null {
        if (this.isCaptionPresent() === false || this.caption_state === null) return null;
        return getVisibleTextContent(this.caption_state.host) ?? this.caption_state.current_cue?.document.text_content ?? null;
    }

    /**
     * キャプチャ合成用に、現在のTTMLレイヤーを映像と同じ解像度のCanvasへ変換する。
     * SVG foreignObject の変換に対応しないブラウザーでは null を返し、映像再生自体は継続する。
     */
    public async createRawCanvas(component: ARIBTTMLComponent): Promise<HTMLCanvasElement | null> {
        if (this.svg === null || this.media === null) return null;
        const is_present = component === 'Caption' ? this.isCaptionPresent() : this.isSuperimposePresent();
        if (is_present === false) return null;
        const clone = this.svg.cloneNode(true) as SVGSVGElement;
        for (const layer of [...clone.querySelectorAll('foreignObject')]) {
            layer.setAttribute('visibility', layer.getAttribute('data-arib-ttml-component') === component ? 'visible' : 'hidden');
        }
        clone.setAttribute('width', String(LOGICAL_VIDEO_WIDTH));
        clone.setAttribute('height', String(LOGICAL_VIDEO_HEIGHT));
        try {
            const blob = new Blob([new XMLSerializer().serializeToString(clone)], {type: 'image/svg+xml'});
            const bitmap = await createImageBitmap(blob);
            const canvas = document.createElement('canvas');
            canvas.width = this.media.videoWidth || LOGICAL_VIDEO_WIDTH;
            canvas.height = this.media.videoHeight || LOGICAL_VIDEO_HEIGHT;
            canvas.getContext('2d')?.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
            bitmap.close();
            return canvas;
        } catch (error) {
            console.warn('[ARIBTTMLRenderer] Failed to rasterize the subtitle overlay.', error);
            return null;
        }
    }

    private startRendering(): void {
        if (this.animation_frame_id !== null) return;
        const render = () => {
            this.renderCurrentTime();
            this.animation_frame_id = window.requestAnimationFrame(render);
        };
        this.animation_frame_id = window.requestAnimationFrame(render);
    }

    private stopRendering(): void {
        if (this.animation_frame_id === null) return;
        window.cancelAnimationFrame(this.animation_frame_id);
        this.animation_frame_id = null;
    }

    private renderCurrentTime(): void {
        if (this.media === null) return;
        for (const state of [this.caption_state, this.superimpose_state]) {
            if (state === null) continue;
            const preferred_component_tag = state === this.caption_state ?
                this.preferred_caption_component_tag : this.preferred_superimpose_component_tag;
            const available_component_tags = [...state.cues_by_component.keys()].sort((left, right) => left - right);
            const display_modes = available_component_tags.map((component_tag) => {
                const cue = this.findCue(
                    state.cues_by_component.get(component_tag) ?? [],
                    this.media!.currentTime,
                );
                return {component_tag, mode: cue === null ? null : this.getCueDisplayMode(cue)};
            });
            // 二言語asset間でDMFが不一致という非準拠送出に限り、自動表示assetを優先する。
            // 両方が同じDMFなら、8.11.4どおりユーザーの言語/component選択を優先する。
            const has_automatic_component = display_modes.some(({mode}) => mode === 0);
            const has_non_automatic_component = display_modes.some(({mode}) => mode !== null && mode !== 0);
            const automatic_component_tag = has_automatic_component && has_non_automatic_component ?
                display_modes.find(({mode}) => mode === 0)?.component_tag : undefined;
            const language_component_tag = state === this.caption_state && this.preferred_caption_language !== 'auto' ?
                available_component_tags.find((component_tag) =>
                    state.cues_by_component.get(component_tag)?.[0]?.unit.additional_info.language ===
                        this.preferred_caption_language
                ) : undefined;
            const selected_component_tag = automatic_component_tag ??
                language_component_tag ?? (state.cues_by_component.has(preferred_component_tag) ?
                preferred_component_tag : available_component_tags[0]);
            const selected_cues = Number.isFinite(selected_component_tag) ?
                state.cues_by_component.get(selected_component_tag) ?? [] : [];
            const cue = this.findCue(selected_cues, this.media.currentTime);
            if (cue !== state.current_cue) {
                this.stopAudio(state, true);
                state.current_cue = cue;
                state.has_visible_content = false;
                state.host.replaceChildren();
                if (cue !== null && cue.document.root !== null && cue.document.is_clear === false) {
                    state.host.appendChild(cue.document.root);
                    adjustScaledFontLayout(cue.document.root);
                }
            }
            // TTML の timed element は visibility を個別に切り替えるため、親の visibility:hidden では
            // 子要素の visibility:visible に表示を戻される。レイヤー全体は opacity で確実に遮断する。
            state.host.style.opacity = cue !== null && this.isCueVisible(state, cue) ? '1' : '0';
            if (cue === null || cue.document.root === null) {
                state.has_visible_content = false;
                continue;
            }

            const controls_time = cue.unit.additional_info.time_management_mode === 0x02;
            const document_time = controls_time ?
                cue.document.base_time + Math.max(0, this.media.currentTime - cue.nominal_start_time) : cue.document.base_time;
            for (const timed_element of cue.document.timed_elements) {
                const active = controls_time === false ||
                    (timed_element.begin <= document_time && document_time < timed_element.end);
                timed_element.element.style.visibility = active ? 'visible' : 'hidden';
            }
            state.has_visible_content = controls_time === false || cue.document.content_timings.some((timing) =>
                timing.begin <= document_time && document_time < timing.end
            );
            this.synchronizeAudio(state, cue, document_time, controls_time);
            for (const animated_element of cue.document.animated_elements) {
                const elapsed = controls_time ? Math.max(0, document_time - animated_element.begin) :
                    Math.max(0, this.media.currentTime - cue.start_time);
                animated_element.element.style.animationDelay = `${animated_element.delay - elapsed}s`;
                animated_element.element.style.animationPlayState = 'paused';
            }
        }
    }

    private findCue(cues: PresentationCue[], current_time: number): PresentationCue | null {
        let begin = 0;
        let end = cues.length;
        while (begin < end) {
            const middle = Math.floor((begin + end) / 2);
            if (cues[middle].start_time <= current_time) begin = middle + 1;
            else end = middle;
        }
        if (begin === 0) return null;
        const cue = cues[begin - 1];
        return current_time < cue.end_time ? cue : null;
    }

    private getCueDisplayMode(cue: PresentationCue): number {
        return this.options.playback_mode === 'Playback' ?
            cue.unit.additional_info.display_mode & 0x03 : cue.unit.additional_info.display_mode >> 2;
    }

    private isCueVisible(state: ComponentState, cue: PresentationCue): boolean {
        const display_mode = this.getCueDisplayMode(cue);
        // 字幕ボタンによる明示的な非表示は、送出側の自動表示指定よりも優先する。
        // 文字スーパーは緊急情報にも使われるため、従来どおり自動表示指定を優先する。
        if (state === this.caption_state && state.is_visible === false) return false;
        // 00=自動表示、10=選択表示。
        if (display_mode === 0) return true;
        if (display_mode === 2) return state.is_visible;
        return false;
    }

    private compareAuxiliaryAudioPriority(
        left: AuxiliaryAudioRegistration,
        right: AuxiliaryAudioRegistration,
    ): number {
        return left.priority_time - right.priority_time ||
            left.cue.unit.order - right.cue.unit.order || left.index - right.index;
    }

    private getAuxiliaryAudioRegistration(
        state: ComponentState,
        index: number,
    ): AuxiliaryAudioRegistration | undefined {
        return [...this.active_auxiliary_audio].find((registration) =>
            registration.state === state && registration.index === index
        );
    }

    private releaseAuxiliaryAudio(registration: AuxiliaryAudioRegistration | undefined): void {
        if (registration !== undefined) this.active_auxiliary_audio.delete(registration);
    }

    /**
     * TR-B39 7.2.3 の競合規則に従って補助音の再生枠を予約する。
     *
     * - AIFF-C同士、通常内蔵音同士、速報内蔵音同士は後発だけを残す。
     * - AIFF-Cと通常内蔵音、速報内蔵音と各通常音は共存できる。
     * - 速報内蔵音を必ず保護しつつ、全体では新しい2音までに制限する。
     */
    private reserveAuxiliaryAudio(
        state: ComponentState,
        cue: PresentationCue,
        index: number,
        kind: AuxiliaryAudioKind,
        priority_time: number,
    ): AuxiliaryAudioRegistration | null {
        const incoming: AuxiliaryAudioRegistration = {state, cue, index, kind, priority_time};
        const same_kind = [...this.active_auxiliary_audio].filter((registration) => registration.kind === kind);
        for (const registration of same_kind) {
            // pause復帰やシーク復元で古い指定が後から走っても、実際に後発の音を奪わない。
            if (this.compareAuxiliaryAudioPriority(incoming, registration) <= 0) return null;
        }
        // 同じ方式の音は同時再生不可。後発を予約する前に従来音を止める。
        for (const registration of same_kind) {
            this.stopAudioAtIndex(registration.state, registration.index, false);
        }

        while (this.active_auxiliary_audio.size >= 2) {
            // 速報内蔵音は常に保護する。それ以外は後発優先なので、最古の通常音を外す。
            const regular_audio = [...this.active_auxiliary_audio]
                .filter((registration) => registration.kind !== 'QuickReportRomSound')
                .sort((left, right) => this.compareAuxiliaryAudioPriority(left, right));
            const oldest_regular_audio = regular_audio[0];
            if (oldest_regular_audio === undefined) return null;
            if (
                kind !== 'QuickReportRomSound' &&
                this.compareAuxiliaryAudioPriority(incoming, oldest_regular_audio) <= 0
            ) {
                return null;
            }
            this.stopAudioAtIndex(oldest_regular_audio.state, oldest_regular_audio.index, false);
        }
        this.active_auxiliary_audio.add(incoming);
        return incoming;
    }

    private synchronizeAudio(
        state: ComponentState,
        cue: PresentationCue,
        document_time: number,
        controls_time: boolean,
    ): void {
        const media = this.media;
        if (media === null || media.paused || this.isCueVisible(state, cue) === false) {
            this.pauseAudio(state);
            return;
        }
        cue.document.audio_sources.forEach((source, index) => {
            const is_active = controls_time === false ||
                (source.begin <= document_time && document_time < source.end);
            const existing_audio = state.playing_audio.get(index);
            if (is_active === false) {
                this.stopAudioAtIndex(state, index, true);
                return;
            }
            if (existing_audio !== undefined) {
                existing_audio.muted = media.muted;
                existing_audio.volume = media.volume;
                existing_audio.playbackRate = media.playbackRate;
                if (existing_audio.paused) {
                    void existing_audio.play().catch(() => {
                        if (state.playing_audio.get(index) !== existing_audio) return;
                        const registration = this.getAuxiliaryAudioRegistration(state, index);
                        existing_audio.src = '';
                        state.playing_audio.delete(index);
                        this.releaseAuxiliaryAudio(registration);
                        this.scheduleAudioRetry(state, cue, index);
                    });
                }
                return;
            }
            if (state.playing_rom_sounds.has(index) || state.pending_rom_sounds.has(index)) return;
            if (state.triggered_audio.has(index)) return;
            const elapsed = controls_time ? Math.max(0, document_time - source.begin) : 0;
            const rom_sound = source.src.match(/^romsound:\/\/(\d+)$/);
            let kind: AuxiliaryAudioKind = 'AIFF';
            let sound_id: number | null = null;
            if (rom_sound !== null) {
                sound_id = Number(rom_sound[1]);
                // TR-B39 Table 7-5で音が割り当てられているIDは0..13。
                if (sound_id < 0 || sound_id > 13) {
                    state.triggered_audio.add(index);
                    return;
                }
                // シークで非loop内蔵音の途中へ入った場合、先頭から鳴らし直さない。
                if (source.loop === false && elapsed > 0.1) {
                    state.triggered_audio.add(index);
                    return;
                }
                const callback = this.options.rom_sound_callback;
                if (callback === undefined) {
                    state.triggered_audio.add(index);
                    return;
                }
                // TR-B39でいう「速報内蔵音」は、自動表示の文字スーパーで鳴らす内蔵音。
                kind = state === this.superimpose_state && this.getCueDisplayMode(cue) === 0 ?
                    'QuickReportRomSound' : 'RomSound';
            }
            state.triggered_audio.add(index);
            const priority_time = controls_time ?
                cue.nominal_start_time + source.begin - cue.document.base_time : cue.start_time;
            const registration = this.reserveAuxiliaryAudio(state, cue, index, kind, priority_time);
            if (registration === null) return;

            if (rom_sound !== null && sound_id !== null) {
                const callback = this.options.rom_sound_callback!;
                const pending = {cancelled: false};
                state.pending_rom_sounds.set(index, pending);
                let result: unknown;
                try {
                    // 既存B24 PRAの01..14.wav番号へ合わせるため、TTML sound_idには1を足す。
                    result = callback(sound_id + 1, source.loop, elapsed);
                } catch (error) {
                    state.pending_rom_sounds.delete(index);
                    this.releaseAuxiliaryAudio(registration);
                    this.scheduleAudioRetry(state, cue, index);
                    return;
                }
                void Promise.resolve(result).then((stop_handle) => {
                    if (state.pending_rom_sounds.get(index) === pending) state.pending_rom_sounds.delete(index);
                    const stop = typeof stop_handle === 'function' ? stop_handle as () => void : null;
                    if (
                        pending.cancelled || this.active_auxiliary_audio.has(registration) === false ||
                        state.current_cue !== cue
                    ) {
                        stop?.();
                    } else if (stop !== null) {
                        state.playing_rom_sounds.set(index, stop);
                    } else {
                        this.releaseAuxiliaryAudio(registration);
                    }
                }).catch(() => {
                    if (state.pending_rom_sounds.get(index) === pending) state.pending_rom_sounds.delete(index);
                    const should_retry = pending.cancelled === false &&
                        this.active_auxiliary_audio.has(registration) && state.current_cue === cue;
                    this.releaseAuxiliaryAudio(registration);
                    if (should_retry) this.scheduleAudioRetry(state, cue, index);
                });
                return;
            }

            const audio = new Audio(source.src);
            audio.loop = source.loop;
            audio.muted = media.muted;
            audio.volume = media.volume;
            audio.playbackRate = media.playbackRate;
            const synchronize_position = () => {
                if (Number.isFinite(audio.duration) === false || audio.duration <= 0 || elapsed <= 0) return;
                const target = source.loop ? elapsed % audio.duration : Math.min(elapsed, Math.max(0, audio.duration - 0.01));
                try {
                    audio.currentTime = target;
                } catch (error) {
                    // metadata未読込時のseek失敗はloadedmetadata側でもう一度処理する。
                }
            };
            audio.addEventListener('loadedmetadata', synchronize_position, {once: true});
            audio.addEventListener('ended', () => {
                if (state.playing_audio.get(index) === audio) state.playing_audio.delete(index);
                this.releaseAuxiliaryAudio(registration);
            }, {once: true});
            state.playing_audio.set(index, audio);
            synchronize_position();
            void audio.play().catch(() => {
                if (state.playing_audio.get(index) !== audio) return;
                audio.src = '';
                state.playing_audio.delete(index);
                this.releaseAuxiliaryAudio(registration);
                this.scheduleAudioRetry(state, cue, index);
            });
        });
    }

    private scheduleAudioRetry(state: ComponentState, cue: PresentationCue, index: number): void {
        window.setTimeout(() => {
            if (state.current_cue === cue) state.triggered_audio.delete(index);
        }, 1000);
    }

    private stopAudioAtIndex(state: ComponentState, index: number, reset_trigger: boolean): void {
        // 非同期の play()/ROM callback が後から完了しても新しい予約を消さないよう、
        // この state/index に現在ひも付く登録だけを先に解除する。
        const registrations = [...this.active_auxiliary_audio].filter((registration) =>
            registration.state === state && registration.index === index
        );
        for (const registration of registrations) this.active_auxiliary_audio.delete(registration);
        const audio = state.playing_audio.get(index);
        if (audio !== undefined) {
            audio.pause();
            audio.src = '';
            state.playing_audio.delete(index);
        }
        const stop_rom_sound = state.playing_rom_sounds.get(index);
        if (stop_rom_sound !== undefined) {
            try {
                stop_rom_sound();
            } catch (error) {
                // 既に終了済みのAudioBufferSourceNode.stop()などは無視する。
            }
            state.playing_rom_sounds.delete(index);
        }
        const pending_rom_sound = state.pending_rom_sounds.get(index);
        if (pending_rom_sound !== undefined) {
            pending_rom_sound.cancelled = true;
            state.pending_rom_sounds.delete(index);
        }
        if (reset_trigger) state.triggered_audio.delete(index);
    }

    private pauseAudio(target_state?: ComponentState): void {
        const states = target_state === undefined ? [this.caption_state, this.superimpose_state] : [target_state];
        for (const state of states) {
            if (state === null) continue;
            for (const audio of state.playing_audio.values()) audio.pause();
            const rom_sound_indices = new Set([
                ...state.playing_rom_sounds.keys(),
                ...state.pending_rom_sounds.keys(),
            ]);
            for (const index of rom_sound_indices) this.stopAudioAtIndex(state, index, true);
        }
    }

    private stopAudio(target_state?: ComponentState, reset_triggers: boolean = false): void {
        const states = target_state === undefined ? [this.caption_state, this.superimpose_state] : [target_state];
        for (const state of states) {
            if (state === null) continue;
            const indices = new Set([
                ...state.playing_audio.keys(),
                ...state.playing_rom_sounds.keys(),
                ...state.pending_rom_sounds.keys(),
                ...[...this.active_auxiliary_audio]
                    .filter((registration) => registration.state === state)
                    .map((registration) => registration.index),
            ]);
            for (const index of indices) this.stopAudioAtIndex(state, index, reset_triggers);
            if (reset_triggers) state.triggered_audio.clear();
        }
    }
}
