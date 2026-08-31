
import DPlayer from 'dplayer';
import mpegts from 'mpegts.js';
import { watch, type WatchStopHandle } from 'vue';

import {
    classifyKonomiTVBS4KHdrSource,
    resolveKonomiTVBS4KHdrOutput,
    resolveKonomiTVBS4KHdrRewriteMode,
    resolveKonomiTVBS4KLiveMpegtsColorRewrite,
    shouldDrawKonomiTVBS4KHdrCanvas,
    type KonomiTVBS4KHdrRewriteMode,
    type KonomiTVBS4KHdrSourceKind,
} from '@/services/player/KonomiTVBS4KHdrPolicy';
import PlayerManager from '@/services/player/PlayerManager';
import usePlayerStore from '@/stores/PlayerStore';
import useSettingsStore from '@/stores/SettingsStore';


const HDR_CANVAS_CLASS = 'konomitv-bs4k-hdr-canvas';

const HLG_SDR_FRAGMENT_SOURCE = `#version 300 es
precision highp float;
uniform sampler2D u_image;
uniform int u_source;
uniform int u_split;
in vec2 v_uv;
out vec4 out_color;

float clamp01(float value) {
    return clamp(value, 0.0, 1.0);
}

float inverseHlgOetf(float signal) {
    const float a = 0.17883277;
    const float b = 0.28466892;
    const float c = 0.55991073;
    float value = clamp01(signal);
    return value <= 0.5 ? value * value / 3.0 : (exp((value - c) / a) + b) / 12.0;
}

float inversePqEotf(float signal) {
    const float m1 = 2610.0 / 16384.0;
    const float m2 = 2523.0 / 32.0;
    const float c1 = 3424.0 / 4096.0;
    const float c2 = 2413.0 / 128.0;
    const float c3 = 2392.0 / 128.0;
    float value = clamp01(signal);
    float raised = pow(value, 1.0 / m2);
    float numerator = max(raised - c1, 0.0);
    float denominator = max(c2 - c3 * raised, 1e-6);
    return 10000.0 * pow(numerator / denominator, 1.0 / m1);
}

float prototypeToneMap(float hdr_luminance) {
    const float k1 = 0.83802;
    const float k2 = 15.09968;
    const float k3 = 0.74204;
    const float k4 = 78.99439;
    float inflection = 58.5 / k1;
    float mapped = hdr_luminance < inflection
        ? k1 * hdr_luminance
        : k2 * log(hdr_luminance / inflection - k3) + k4;
    const float reference_white = 90.7;
    const float method_peak = 118.4;
    if (mapped <= reference_white) {
        return clamp01(mapped / 100.0);
    }
    float ratio = clamp01((mapped - reference_white) / (method_peak - reference_white));
    return reference_white / 100.0 + (1.0 - reference_white / 100.0) * (2.0 * ratio - ratio * ratio);
}

float prototypeSdrLumaFit(float luminance) {
    float input_value = clamp01(luminance);
    const float contrast = 1.7;
    return input_value / (input_value + contrast * (1.0 - input_value));
}

vec3 softMapBt709Gamut(vec3 color) {
    const vec3 weights = vec3(0.2126, 0.7152, 0.0722);
    float luma = clamp01(dot(color, weights));
    if (luma <= 0.0) return vec3(0.0);
    if (luma >= 1.0) return vec3(1.0);
    vec3 delta = color - vec3(luma);
    float boundary_scale = 1e20;
    for (int index = 0; index < 3; index++) {
        float component = delta[index];
        if (component > 0.0) {
            boundary_scale = min(boundary_scale, (1.0 - luma) / component);
        } else if (component < 0.0) {
            boundary_scale = min(boundary_scale, -luma / component);
        }
    }
    float normalized_chroma = 1.0 / boundary_scale;
    const float knee = 0.50;
    if (normalized_chroma <= knee) return color;
    float distance = (normalized_chroma - knee) / (1.0 - knee);
    float mapped_chroma = knee + (1.0 - knee) * (1.0 - exp(-distance));
    return clamp(vec3(luma) + delta * (mapped_chroma / normalized_chroma), 0.0, 1.0);
}

float srgbOetf(float linear) {
    float value = clamp01(linear);
    return value <= 0.0031308 ? 12.92 * value : 1.055 * pow(value, 1.0 / 2.4) - 0.055;
}

vec3 mapToneMappedRgb(vec3 input_rgb, int source) {
    vec3 scene;
    if (source == 1) {
        float red_nits = inversePqEotf(input_rgb.r);
        float green_nits = inversePqEotf(input_rgb.g);
        float blue_nits = inversePqEotf(input_rgb.b);
        scene = vec3(red_nits, green_nits, blue_nits) / 1000.0;
    } else {
        scene = vec3(
            inverseHlgOetf(input_rgb.r),
            inverseHlgOetf(input_rgb.g),
            inverseHlgOetf(input_rgb.b)
        );
        float scene_luma = dot(scene, vec3(0.2627, 0.6780, 0.0593));
        if (scene_luma <= 0.0) return vec3(0.0);
        scene *= 1000.0 * pow(scene_luma, 0.2);
        scene /= 1000.0;
    }

    vec3 display = scene * 1000.0;
    const float crosstalk = 0.075;
    float sum = display.r + display.g + display.b;
    display = (1.0 - 3.0 * crosstalk) * display + crosstalk * sum;

    const mat3 bt2020_to_xyz = mat3(
        0.6370, 0.2627, 0.0000,
        0.1446, 0.6780, 0.0281,
        0.1689, 0.0593, 1.0610
    );
    const mat3 xyz_to_bt2020 = mat3(
        1.7167, -0.6667, 0.0176,
        -0.3557, 1.6165, -0.0428,
        -0.2534, 0.0158, 0.9421
    );
    vec3 xyz = bt2020_to_xyz * display;
    if (xyz.g <= 0.0) return vec3(0.0);
    xyz *= 100.0 * prototypeToneMap(xyz.g) / xyz.g;
    vec3 sdr2020 = xyz_to_bt2020 * xyz;
    float inverse = 1.0 / (1.0 - 3.0 * crosstalk);
    float sdr_sum = sdr2020.r + sdr2020.g + sdr2020.b;
    sdr2020 = inverse * ((1.0 - crosstalk) * sdr2020 - crosstalk * (sdr_sum - sdr2020));
    sdr2020 /= 100.0;

    const mat3 bt2020_to_bt709 = mat3(
        1.660491, -0.124550, -0.018151,
        -0.587641, 1.132900, -0.100579,
        -0.072850, -0.008350, 1.118730
    );
    vec3 sdr709 = bt2020_to_bt709 * sdr2020;
    float sdr_luma = dot(sdr709, vec3(0.2126, 0.7152, 0.0722));
    if (sdr_luma <= 0.0) return vec3(0.0);
    sdr709 = softMapBt709Gamut(sdr709 * (prototypeSdrLumaFit(sdr_luma) / sdr_luma));
    return vec3(srgbOetf(sdr709.r), srgbOetf(sdr709.g), srgbOetf(sdr709.b));
}

void main() {
    vec4 source = texture(u_image, v_uv);
    // Debug は同一 canvas の右半面をシェーダ未通しにする（キャプチャの backing が左右比較を持つ）。
    // ライブ Debug の画面上は clip で右半面の canvas を隠し、下の <video> 素通しを見せる。
    if (u_split == 1 && v_uv.x >= 0.5) {
        out_color = vec4(source.rgb, 1.0);
        return;
    }
    out_color = vec4(mapToneMappedRgb(source.rgb, u_source), 1.0);
}
`;

const HLG_SDR_VERTEX_SOURCE = `#version 300 es
const vec2 positions[6] = vec2[](
    vec2(-1.0, -1.0), vec2(1.0, -1.0), vec2(-1.0, 1.0),
    vec2(-1.0, 1.0), vec2(1.0, -1.0), vec2(1.0, 1.0)
);
out vec2 v_uv;
void main() {
    vec2 position = positions[gl_VertexID];
    v_uv = vec2(position.x * 0.5 + 0.5, 1.0 - (position.y * 0.5 + 0.5));
    gl_Position = vec4(position, 0.0, 1.0);
}
`;


type MpegtsColorRewriteMode = 'None' | 'ToneMap' | 'SdrInHlg';

type MpegtsColorPlayer = {
    mediaInfo?: {
        transferCharacteristics?: number | null;
    };
    setVideoColorRewrite?: (mode: MpegtsColorRewriteMode) => void;
    on?: (event: string, listener: (...args: unknown[]) => void) => void;
    off?: (event: string, listener: (...args: unknown[]) => void) => void;
};

function resolveMpegtsColorRewriteMode(mode: KonomiTVBS4KHdrRewriteMode): MpegtsColorRewriteMode {
    // mpegts.js は ToneMap / None / SdrInHlg だけを理解する。
    // ライブ Debug は右半面を <video> 素通しにするため None。SDR 変換だけ ToneMap。
    return mode === 'ToneMap' || mode === 'SdrInHlg' ? mode : 'None';
}


/**
 * ライブ視聴: HLG / PQ をデコード後の画素で SDR へ変換し、必要なら SPS / AV1 の色シグナリングを書き換える。
 * BS4K 録画再生 (オンライン / オフライン) でも同じ canvas 変換を再利用する。
 * 録画側の fMP4 色信号書換えは KonomiTVBS4KColorRewriteLoader が fragment loader 境界で行い、
 * 検出した transfer_characteristics は PlayerStore 経由でこのマネージャへ届く。
 */
class KonomiTVBS4KHlgSdrManager implements PlayerManager {

    public readonly restart_required_when_quality_switched = true;

    private readonly player: DPlayer;
    private destroyed = false;
    private canvas: HTMLCanvasElement | null = null;
    private gl: WebGL2RenderingContext | null = null;
    private texture: WebGLTexture | null = null;
    private program: WebGLProgram | null = null;
    private source_location: WebGLUniformLocation | null = null;
    // Debug 左右比較用。1 のとき右半面をシェーダ未通しにする u_split の location。
    private split_location: WebGLUniformLocation | null = null;
    private frame_handle: number | null = null;
    private resize_observer: ResizeObserver | null = null;
    private watch_stops: WatchStopHandle[] = [];
    private media_info_handler: ((...args: unknown[]) => void) | null = null;
    private source_kind: KonomiTVBS4KHdrSourceKind = 'None';
    private rewrite_mode: KonomiTVBS4KHdrRewriteMode = 'None';
    // 直近で mpegts.js へ渡した色信号書換え。同じ値の再適用はライブ切断の原因になるため省略する。
    // 初期値は PlayerController の mpegts config と同じ希望出力から決め、初回の同値再送を避ける。
    private mpegts_rewrite_mode: MpegtsColorRewriteMode | null = null;
    private transfer_characteristics: number | null = null;
    // 録画 fMP4 の書換え不能を利用者へ通知済みかどうか (同一失敗につき1回に抑える)
    private unsafe_notice_shown = false;

    constructor(player: DPlayer) {
        this.player = player;
    }

    public async init(): Promise<void> {
        this.destroyed = false;
        this.installCanvas();
        this.bindMediaInfo();
        this.watch_stops = [
            watch(
                () => useSettingsStore().settings.konomitv_bs4k_hdr_output,
                () => this.applyPolicy(),
            ),
            watch(
                () => usePlayerStore().konomitv_bs4k_playback_hdr_output_override,
                () => this.applyPolicy(),
            ),
            watch(
                () => usePlayerStore().b60_video_transfer,
                () => this.applyPolicy(),
            ),
            watch(
                () => usePlayerStore().mh_eit_hdr_hint,
                () => this.applyPolicy(),
            ),
            // 録画 HLS では fragment loader が fMP4 から検出した transfer_characteristics を書き込む
            watch(
                () => usePlayerStore().sps_transfer_characteristics,
                () => this.applyPolicy(),
            ),
            // fMP4 を安全に書き換えられなかった場合は canvas を無効化して HDR 素通しへ切り替える
            watch(
                () => usePlayerStore().konomitv_bs4k_recorded_color_rewrite_unsafe,
                () => this.applyPolicy(),
            ),
        ];
        this.mpegts_rewrite_mode = resolveKonomiTVBS4KLiveMpegtsColorRewrite(
            resolveKonomiTVBS4KHdrOutput(
                useSettingsStore().settings.konomitv_bs4k_hdr_output,
                usePlayerStore().konomitv_bs4k_playback_hdr_output_override,
            ),
        );
        this.applyPolicy();
        this.startFrameLoop();
    }

    public async destroy(): Promise<void> {
        this.destroyed = true;
        for (const stop of this.watch_stops) {
            stop();
        }
        this.watch_stops = [];
        this.unbindMediaInfo();
        this.stopFrameLoop();
        this.disposeGl();
        this.canvas?.remove();
        this.canvas = null;
        // 画質切替では同じインスタンスを destroy → init するため、旧 mpegts への適用済みモードを捨てる。
        this.mpegts_rewrite_mode = null;
    }

    private getMpegtsPlayer(): MpegtsColorPlayer | null {
        return (this.player.plugins.mpegts as MpegtsColorPlayer | undefined) ?? null;
    }

    private applyMpegtsColorRewrite(mode: KonomiTVBS4KHdrRewriteMode): void {
        const mpegts_mode = resolveMpegtsColorRewriteMode(mode);
        if (this.mpegts_rewrite_mode === mpegts_mode) {
            return;
        }
        // ソース未検出の None は初期 config を維持する。未検出 None を送ると SDR 開始時の ToneMap を潰す。
        if (mode === 'None' && this.source_kind === 'None') {
            return;
        }
        // 同じモードの再送はライブ切断の原因になる。値が変わる切替だけ setVideoColorRewrite する。
        // colour/transfer だけの差では mpegts.js は新しい InitSegment を出さない。
        const mpegts_player = this.getMpegtsPlayer();
        mpegts_player?.setVideoColorRewrite?.(mpegts_mode);
        this.mpegts_rewrite_mode = mpegts_mode;
    }

    private installCanvas(): void {
        const wrap = this.player.template.videoWrapAspect ?? this.player.template.videoWrap;
        this.canvas = wrap.querySelector<HTMLCanvasElement>(`.${HDR_CANVAS_CLASS}`);
        if (this.canvas === null) {
            this.canvas = document.createElement('canvas');
            this.canvas.className = HDR_CANVAS_CLASS;
            this.canvas.setAttribute('aria-hidden', 'true');
            wrap.append(this.canvas);
        }
        this.canvas.style.position = 'absolute';
        this.canvas.style.inset = '0';
        this.canvas.style.width = '100%';
        this.canvas.style.height = '100%';
        this.canvas.style.zIndex = '0';
        this.canvas.style.pointerEvents = 'none';
        this.canvas.style.clipPath = 'none';
        this.canvas.style.display = 'none';
        this.resize_observer = new ResizeObserver(() => this.syncCanvasSize());
        this.resize_observer.observe(wrap);
        this.syncCanvasSize();
    }

    private syncCanvasSize(): void {
        if (this.canvas === null) {
            return;
        }
        const video = this.player.video;
        const width = Math.max(video.videoWidth || 0, 1);
        const height = Math.max(video.videoHeight || 0, 1);
        if (this.canvas.width !== width || this.canvas.height !== height) {
            this.canvas.width = width;
            this.canvas.height = height;
        }
    }

    private bindMediaInfo(): void {
        const mpegts_player = this.getMpegtsPlayer();
        if (mpegts_player === null) {
            return;
        }
        this.transfer_characteristics = mpegts_player.mediaInfo?.transferCharacteristics ?? null;
        usePlayerStore().sps_transfer_characteristics = this.transfer_characteristics;
        this.media_info_handler = (...args: unknown[]) => {
            if (this.destroyed === true) {
                return;
            }
            const info = args[0] as {transferCharacteristics?: number | null} | undefined;
            this.transfer_characteristics = info?.transferCharacteristics ?? null;
            usePlayerStore().sps_transfer_characteristics = this.transfer_characteristics;
            this.applyPolicy();
        };
        mpegts_player.on?.(mpegts.Events.MEDIA_INFO, this.media_info_handler);
    }

    private unbindMediaInfo(): void {
        const mpegts_player = this.getMpegtsPlayer();
        if (mpegts_player !== null && this.media_info_handler !== null) {
            mpegts_player.off?.(mpegts.Events.MEDIA_INFO, this.media_info_handler);
        }
        this.media_info_handler = null;
    }

    private applyPolicy(): void {
        if (this.destroyed === true) {
            return;
        }
        const player_store = usePlayerStore();
        const settings_store = useSettingsStore();
        const desired_output = resolveKonomiTVBS4KHdrOutput(
            settings_store.settings.konomitv_bs4k_hdr_output,
            player_store.konomitv_bs4k_playback_hdr_output_override,
        );
        // transfer_characteristics はライブでは mpegts.js の mediaInfo から、
        // 録画 HLS では fragment loader が fMP4 から検出した値を、それぞれ PlayerStore 経由で受け取る
        this.transfer_characteristics = player_store.sps_transfer_characteristics;
        this.source_kind = classifyKonomiTVBS4KHdrSource(this.transfer_characteristics);
        this.rewrite_mode = resolveKonomiTVBS4KHdrRewriteMode(
            this.transfer_characteristics,
            player_store.b60_video_transfer,
            desired_output,
        );
        this.applyMpegtsColorRewrite(this.rewrite_mode);
        // 録画 fMP4 を安全に書き換えられなかった場合は、ブラウザの HDR 表示との二重変換を避けるため
        // canvas を無効化して HDR 素通しで再生を継続する (通知はセッション中1回だけ)
        let show_canvas = shouldDrawKonomiTVBS4KHdrCanvas(this.rewrite_mode);
        if (player_store.konomitv_bs4k_recorded_color_rewrite_unsafe === true) {
            if (show_canvas === true && this.unsafe_notice_shown === false) {
                this.unsafe_notice_shown = true;
                this.player.notice(
                    'この録画の HDR 信号は安全に書き換えられないため、SDR 変換を行わず HDR のまま再生します。',
                );
            }
            show_canvas = false;
        }
        if (this.canvas !== null) {
            this.canvas.style.display = show_canvas === true ? 'block' : 'none';
            // ライブ Debug は左半分だけ canvas。右半分は下の <video>（mpegts None）を素通しする。
            const clip_left_half = this.rewrite_mode === 'Debug' && this.player.options.live === true;
            this.canvas.style.clipPath = clip_left_half === true ? 'inset(0 50% 0 0)' : 'none';
        }
        if (show_canvas === true) {
            this.ensureGl();
        }
    }

    private ensureGl(): void {
        if (this.gl !== null || this.canvas === null) {
            return;
        }
        const gl = this.canvas.getContext('webgl2', {
            alpha: false,
            antialias: false,
            depth: false,
            stencil: false,
            premultipliedAlpha: false,
            preserveDrawingBuffer: true,
        });
        if (gl === null) {
            console.warn('[KonomiTVBS4KHlgSdrManager] WebGL2 is unavailable. HDR canvas conversion is disabled.');
            return;
        }
        const vertex = this.compileShader(gl, gl.VERTEX_SHADER, HLG_SDR_VERTEX_SOURCE);
        const fragment = this.compileShader(gl, gl.FRAGMENT_SHADER, HLG_SDR_FRAGMENT_SOURCE);
        if (vertex === null || fragment === null) {
            return;
        }
        const program = gl.createProgram();
        if (program === null) {
            return;
        }
        gl.attachShader(program, vertex);
        gl.attachShader(program, fragment);
        gl.linkProgram(program);
        if (gl.getProgramParameter(program, gl.LINK_STATUS) === false) {
            console.warn('[KonomiTVBS4KHlgSdrManager] Failed to link the HDR shader.', gl.getProgramInfoLog(program));
            return;
        }
        this.program = program;
        this.source_location = gl.getUniformLocation(program, 'u_source');
        this.split_location = gl.getUniformLocation(program, 'u_split');
        this.texture = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, this.texture);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        this.gl = gl;
    }

    private compileShader(
        gl: WebGL2RenderingContext,
        type: number,
        source: string,
    ): WebGLShader | null {
        const shader = gl.createShader(type);
        if (shader === null) {
            return null;
        }
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        if (gl.getShaderParameter(shader, gl.COMPILE_STATUS) === false) {
            console.warn('[KonomiTVBS4KHlgSdrManager] Failed to compile the HDR shader.', gl.getShaderInfoLog(shader));
            gl.deleteShader(shader);
            return null;
        }
        return shader;
    }

    private startFrameLoop(): void {
        const video = this.player.video as HTMLVideoElement & {
            requestVideoFrameCallback?: (callback: () => void) => number;
            cancelVideoFrameCallback?: (handle: number) => void;
        };
        const draw = (): void => {
            if (this.destroyed === true) {
                return;
            }
            this.drawFrame();
            if (typeof video.requestVideoFrameCallback === 'function') {
                this.frame_handle = video.requestVideoFrameCallback(draw);
                return;
            }
            this.frame_handle = window.requestAnimationFrame(draw);
        };
        draw();
    }

    private stopFrameLoop(): void {
        const video = this.player.video as HTMLVideoElement & {
            cancelVideoFrameCallback?: (handle: number) => void;
        };
        if (this.frame_handle !== null) {
            if (typeof video.cancelVideoFrameCallback === 'function') {
                video.cancelVideoFrameCallback(this.frame_handle);
            } else {
                window.cancelAnimationFrame(this.frame_handle);
            }
        }
        this.frame_handle = null;
        this.resize_observer?.disconnect();
        this.resize_observer = null;
    }

    private drawFrame(): void {
        if (
            this.destroyed === true ||
            this.canvas === null ||
            this.gl === null ||
            this.program === null ||
            this.texture === null ||
            shouldDrawKonomiTVBS4KHdrCanvas(this.rewrite_mode) === false
        ) {
            return;
        }
        const video = this.player.video;
        if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA || video.videoWidth === 0) {
            return;
        }
        this.syncCanvasSize();
        const gl = this.gl;
        gl.viewport(0, 0, this.canvas.width, this.canvas.height);
        gl.useProgram(this.program);
        gl.uniform1i(this.source_location, this.source_kind === 'Pq' ? 1 : 0);
        // Debug は backing canvas を左右比較にする。ライブ画面は clip で右半面を隠すだけ。
        gl.uniform1i(this.split_location, this.rewrite_mode === 'Debug' ? 1 : 0);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.texture);
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, 0);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
        gl.drawArrays(gl.TRIANGLES, 0, 6);
    }

    private disposeGl(): void {
        if (this.gl !== null) {
            if (this.texture !== null) {
                this.gl.deleteTexture(this.texture);
            }
            if (this.program !== null) {
                this.gl.deleteProgram(this.program);
            }
        }
        this.gl = null;
        this.texture = null;
        this.program = null;
        this.source_location = null;
        this.split_location = null;
    }
}

export function getKonomiTVBS4KHdrCaptureCanvas(player: DPlayer): HTMLCanvasElement | null {
    const canvas = player.container.querySelector<HTMLCanvasElement>(`.${HDR_CANVAS_CLASS}`);
    if (canvas === null || canvas.style.display === 'none') {
        return null;
    }
    return canvas;
}

export default KonomiTVBS4KHlgSdrManager;
