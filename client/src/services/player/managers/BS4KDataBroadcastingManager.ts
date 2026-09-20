import DPlayer from 'dplayer';
import {
    AribReceiverHost,
    BroadcastVfsSession,
    ServiceWorkerBroadcastVfs,
} from 'libaribhtml5';

import type {
    AribMediaPlane,
    AribMediaPlaneAdapter,
    AribMediaPlaneUnmountReason,
    AribViewerParticipationNotification,
    ProgramInfo,
    RuntimeEvent,
    RuntimeWindow,
} from 'libaribhtml5';

import router from '@/router';
import PlayerManager from '@/services/player/PlayerManager';
import useChannelsStore from '@/stores/ChannelsStore';
import useSettingsStore from '@/stores/SettingsStore';
import { dayjs, PlayerUtils } from '@/utils';


type BS4KApplicationResource = {
    contextId: number;
    path: string;
    contentType: string;
    data: string;
};

type BS4KApplicationResourceRemoved = {
    contextId: number;
    path: string;
};

type BS4KApplicationState = {
    contextId: number;
    applicationType: number;
    organizationId: number;
    applicationId: number;
    controlCode: number;
    applicationDescriptorPresent: boolean;
    serviceBound: boolean;
    visibility: number;
    presentApplicationPriority: boolean;
    applicationPriority: number;
    entryPath: string;
    transportUrls: string[];
    entryReady: boolean;
};

type BS4KBroadcastClock = {
    mediaTimeValue: string;
    mediaTimeTimescale: number;
    broadcastTimeValue: string;
    broadcastTimeTimescale: number;
};

type BS4KLayoutConfiguration = {
    contextId: number;
    backgroundColorRgb: number | null;
};

type BS4KEventInfo = {
    contextId: number;
    sourcePacketId: number;
    tableId: number;
    version: number;
    currentNext: boolean;
    sectionNumber: number;
    lastSectionNumber: number;
    serviceId: number;
    tlvStreamId: number;
    originalNetworkId: number;
    eventId: number;
    startTimeUnixMilliseconds: number | null;
    durationSeconds: number | null;
    runningStatus: number;
    freeCaMode: boolean;
    language: string;
    title: string;
    description: string;
    extendedDescription: string;
};

type BS4KStreamEvent = {
    contextId: number;
    eventMessageTag: number;
    messageGroupId: number;
    messageVersion: number;
    currentNext: boolean;
    timeMode: number;
    messageId: number;
    privateData: string;
};

type BS4KViewerParticipationNotification = {
    contextId: number;
    sourcePacketId: number;
    eventMessageTag: number;
    dataEventId: number;
    messageGroupId: number;
    version: number;
    currentNext: boolean;
    sectionNumber: number;
    lastSectionNumber: number;
    inputOffset: string;
};

type BS4KDatacastEventType = (
    'snapshot_begin' |
    'snapshot_end' |
    'application_state' |
    'application_resource' |
    'application_resource_removed' |
    'application_resources_reset' |
    'broadcast_clock' |
    'layout_configuration' |
    'event_info' |
    'stream_event' |
    'viewer_participation'
);

type StoredApplicationResource = Omit<BS4KApplicationResource, 'data'> & {
    data: Uint8Array;
};

type ManagedApplication = {
    key: string;
    state: BS4KApplicationState;
    entry: string;
};

interface KonomiTVBS4KRuntimeHostWindow extends Window {
    __ARIB_HTML5_INSTALL__?: (target: RuntimeWindow) => void;
}

const RECEIVER_KEY_MAP: Record<number, number> = {
    1: 38,
    2: 40,
    3: 37,
    4: 39,
    18: 13,
    19: 461,
    20: 457,
    21: 406,
    22: 403,
    23: 404,
    24: 405,
};

const LEGACY_RECEIVER_INFO_PREFIX = 'KonomiTV-BMLBrowser_nvram_prefix=receiverinfo%2F';
const KONOMITV_BS4K_EXTERNAL_NETWORK_PERMISSION_PATH = '.konomitv-bs4k-external-network-enabled';


/**
 * DPlayerのvideo要素を移動せず、ARIB HTML5アプリケーションが指定する映像面へ合わせる。
 * MSE接続済みの要素をiframeへ移動するとMediaSourceが閉じるため、位置と大きさだけを更新する。
 */
class KonomiTVBS4KMediaPlaneAdapter implements AribMediaPlaneAdapter {

    public readonly renderMode = 'external' as const;

    private readonly player: DPlayer;
    private managed_video: HTMLVideoElement | null = null;
    private managed_hdr_canvas: HTMLCanvasElement | null = null;
    private hdr_canvas_original_styles = new Map<string, string>();
    private video_wrap_aspect_original_width: string | null = null;
    private application_visible = true;
    private last_plane: AribMediaPlane | null = null;

    constructor(player: DPlayer) {
        this.player = player;
    }

    public mountMediaPlane(_object: HTMLElement, plane: AribMediaPlane): void {
        this.last_plane = plane;
        if (this.application_visible) this.apply(plane);
        else this.restoreNormalLayout();
    }

    public updateMediaPlane(_object: HTMLElement, plane: AribMediaPlane): void {
        this.last_plane = plane;
        if (this.application_visible) this.apply(plane);
        else this.restoreNormalLayout();
    }

    public unmountMediaPlane(reason: AribMediaPlaneUnmountReason): void {
        // アプリ終了とManager破棄ではDPlayer本来のレイアウトへ完全に戻す。
        if (reason === 'application-exit' || reason === 'host-destroy') {
            this.last_plane = null;
            this.restoreNormalLayout();
            return;
        }

        // ページ遷移中や映像objectが消えた間は、直前の映像が全面に残らないよう隠す。
        if (this.managed_video !== null) this.managed_video.style.visibility = 'hidden';
        if (this.managed_hdr_canvas !== null) this.managed_hdr_canvas.style.visibility = 'hidden';
    }

    public setApplicationVisible(visible: boolean): void {
        if (this.application_visible === visible) return;
        this.application_visible = visible;
        if (visible && this.last_plane !== null) this.apply(this.last_plane);
        else if (!visible) this.restoreNormalLayout();
    }

    private apply(plane: AribMediaPlane): void {
        const video = this.player.video;
        const hdr_canvas = this.player.template.videoWrapAspect
            .querySelector<HTMLCanvasElement>('.konomitv-bs4k-hdr-canvas');
        if ((this.managed_video !== null && this.managed_video !== video) ||
            (this.managed_hdr_canvas !== null && this.managed_hdr_canvas !== hdr_canvas)) {
            this.restoreNormalLayout();
        }
        this.managed_video = video;

        // videoをabsolute配置しても、通常フロー上の子を失った16:9コンテナがflex内で収縮しないようにする。
        if (this.video_wrap_aspect_original_width === null) {
            this.video_wrap_aspect_original_width = this.player.template.videoWrapAspect.style.width;
        }
        this.player.template.videoWrapAspect.style.width = '100%';

        const percent = (value: number, extent: number): string => `${value / extent * 100}%`;
        const z_index = plane.layer.externalPlacement === 'above-application' ? '2' : '1';
        Object.assign(video.style, {
            position: 'absolute',
            left: percent(plane.x, plane.screenWidth),
            top: percent(plane.y, plane.screenHeight),
            width: percent(plane.width, plane.screenWidth),
            height: percent(plane.height, plane.screenHeight),
            maxWidth: 'none',
            maxHeight: 'none',
            display: 'block',
            visibility: plane.visible ? 'visible' : 'hidden',
            objectFit: 'contain',
            background: '#000',
            pointerEvents: 'none',
            zIndex: z_index,
        });

        // HLG→SDR変換中はcanvasが実際の表示映像なので、元videoと同じmedia planeへ追従させる。
        if (hdr_canvas !== null) {
            if (this.managed_hdr_canvas === null) {
                for (const property of [
                    'position', 'inset', 'left', 'top', 'width', 'height', 'max-width', 'max-height',
                    'visibility', 'pointer-events', 'z-index',
                ]) {
                    this.hdr_canvas_original_styles.set(property, hdr_canvas.style.getPropertyValue(property));
                }
            }
            this.managed_hdr_canvas = hdr_canvas;
            Object.assign(hdr_canvas.style, {
                position: 'absolute',
                inset: 'auto',
                left: percent(plane.x, plane.screenWidth),
                top: percent(plane.y, plane.screenHeight),
                width: percent(plane.width, plane.screenWidth),
                height: percent(plane.height, plane.screenHeight),
                maxWidth: 'none',
                maxHeight: 'none',
                visibility: plane.visible ? 'visible' : 'hidden',
                pointerEvents: 'none',
                zIndex: z_index,
            });
        }
    }

    private restoreNormalLayout(): void {
        if (this.managed_video !== null) {
            for (const property of [
                'position', 'left', 'top', 'width', 'height', 'max-width', 'max-height',
                'display', 'visibility', 'object-fit', 'background', 'pointer-events', 'z-index',
            ]) {
                this.managed_video.style.removeProperty(property);
            }
        }
        this.managed_video = null;

        if (this.video_wrap_aspect_original_width !== null) {
            if (this.video_wrap_aspect_original_width === '') {
                this.player.template.videoWrapAspect.style.removeProperty('width');
            } else {
                this.player.template.videoWrapAspect.style.width = this.video_wrap_aspect_original_width;
            }
            this.video_wrap_aspect_original_width = null;
        }

        if (this.managed_hdr_canvas !== null) {
            for (const [property, value] of this.hdr_canvas_original_styles) {
                if (value === '') this.managed_hdr_canvas.style.removeProperty(property);
                else this.managed_hdr_canvas.style.setProperty(property, value);
            }
        }
        this.managed_hdr_canvas = null;
        this.hdr_canvas_original_styles.clear();
    }
}


/** BS4KのMMT/TLVライブから受信したARIB HTML5データ放送をDPlayerへ統合する。 */
class BS4KDataBroadcastingManager implements PlayerManager {

    public readonly restart_required_when_quality_switched = true;

    private static readonly SSE_EVENT_TYPES: readonly BS4KDatacastEventType[] = [
        'snapshot_begin',
        'snapshot_end',
        'application_state',
        'application_resource',
        'application_resource_removed',
        'application_resources_reset',
        'broadcast_clock',
        'layout_configuration',
        'event_info',
        'stream_event',
        'viewer_participation',
    ];

    private readonly player: DPlayer;
    private readonly remocon_element: HTMLElement;
    private readonly remocon_data_broadcasting_element: HTMLElement;
    private viewport: HTMLDivElement | null = null;
    private iframe: HTMLIFrameElement | null = null;
    private host: AribReceiverHost | null = null;
    private vfs: BroadcastVfsSession | null = null;
    private vfs_backend: ServiceWorkerBroadcastVfs | null = null;
    private event_source: EventSource | null = null;
    private remocon_abort_controller: AbortController | null = null;
    private previous_installer?: (target: RuntimeWindow) => void;
    private runtime_installer?: (target: RuntimeWindow) => void;
    private readonly resources = new Map<string, StoredApplicationResource>();
    private readonly resource_revisions = new Map<string, number>();
    private readonly application_states = new Map<string, BS4KApplicationState>();
    private readonly applications = new Map<string, ManagedApplication>();
    private readonly program_events = new Map<number, BS4KEventInfo>();
    private lifecycle_generation = 0;
    private vfs_generation = 0;
    private event_processing_tail: Promise<void> = Promise.resolve();
    private is_snapshot_in_progress = false;
    private current_context_id: number | null = null;
    private layout_configuration: BS4KLayoutConfiguration | null = null;
    private ready_application_key: string | null = null;
    private active_application_key: string | null = null;
    private pending_application_key: string | null = null;
    private show_requested = false;
    private dispatch_data_key_after_install = false;
    private autostart_scheduled = false;
    private media_plane_adapter: KonomiTVBS4KMediaPlaneAdapter | null = null;
    private visible = false;

    constructor(player: DPlayer) {
        this.player = player;
        this.remocon_element = document.querySelector('.remote-control')!;
        this.remocon_data_broadcasting_element =
            this.remocon_element.querySelector('.remote-control-data-broadcasting')!;
    }

    /** libaribhtml5、VFS、SSE、リモコンを初期化する。 */
    public async init(): Promise<void> {
        const lifecycle_generation = ++this.lifecycle_generation;
        const is_current = (): boolean => this.lifecycle_generation === lifecycle_generation;

        if (useSettingsStore().settings.tv_show_data_broadcasting === false) {
            this.toggleRemoconLoading(false);
            this.toggleRemoconEnabled(false);
            return;
        }

        // Worker自身をscope配下へ配置しているためService-Worker-Allowedヘッダーは不要。
        this.vfs_backend = new ServiceWorkerBroadcastVfs({
            workerUrl: '/data-broadcast/arib-vfs-sw.js',
            baseUrl: '/data-broadcast/',
        });
        this.vfs = new BroadcastVfsSession(this.vfs_backend, {
            onError: error => {
                if (is_current() === false) return;
                console.error('[BS4KDataBroadcastingManager] VFS operation failed.', error);
                this.player.notice(
                    'データ放送リソースの保存に失敗しました。',
                    3000,
                    undefined,
                    'rgb(var(--v-theme-error-readable))',
                );
            },
        });

        this.createReceiverElements();
        this.createReceiverHost();
        this.initRemoconButtons(lifecycle_generation);
        this.resetReceiverState();

        try {
            const vfs_generation = ++this.vfs_generation;
            await this.vfs.beginSession();
            if (vfs_generation !== this.vfs_generation) return;
        } catch (error) {
            if (is_current() === false) return;
            console.error('[BS4KDataBroadcastingManager] Failed to begin VFS session.', error);
            this.toggleRemoconLoading(false);
            return;
        }
        if (is_current() === false) return;

        this.openEventSource(lifecycle_generation);
        console.log('[BS4KDataBroadcastingManager] Initialized.');
    }

    /** SSE、receiver、VFS、DOMを破棄し、再初期化可能な状態へ戻す。 */
    public async destroy(): Promise<void> {
        ++this.lifecycle_generation;
        ++this.vfs_generation;

        this.event_source?.close();
        this.event_source = null;
        this.remocon_abort_controller?.abort();
        this.remocon_abort_controller = null;

        // 既にキューへ入った旧SSEイベントが次世代の状態へ書き込まないことを確認してから破棄する。
        await this.event_processing_tail.catch(() => undefined);
        this.event_processing_tail = Promise.resolve();

        this.exitApplication();
        this.host?.setLctBackgroundColor(null);
        this.host?.destroy();
        this.host = null;
        this.viewport?.remove();
        this.iframe?.remove();
        this.viewport = null;
        this.iframe = null;

        const vfs = this.vfs;
        const vfs_backend = this.vfs_backend;
        this.vfs = null;
        this.vfs_backend = null;
        await vfs?.dispose().catch(error => {
            console.error('[BS4KDataBroadcastingManager] VFS session dispose failed.', error);
        });
        await vfs_backend?.dispose().catch(error => {
            console.error('[BS4KDataBroadcastingManager] VFS backend dispose failed.', error);
        });

        this.resources.clear();
        this.resource_revisions.clear();
        this.application_states.clear();
        this.applications.clear();
        this.program_events.clear();
        this.media_plane_adapter = null;
        useChannelsStore().current_program_present = null;
        useChannelsStore().current_program_following = null;
        this.toggleRemoconLoading(true);
        this.toggleRemoconEnabled(false);

        const host_window = window as KonomiTVBS4KRuntimeHostWindow;
        // 旧Managerの遅延destroyが新しいinstallerを消さないよう、所有中の場合だけ戻す。
        if (host_window.__ARIB_HTML5_INSTALL__ === this.runtime_installer) {
            if (this.previous_installer !== undefined) host_window.__ARIB_HTML5_INSTALL__ = this.previous_installer;
            else delete host_window.__ARIB_HTML5_INSTALL__;
        }
        this.runtime_installer = undefined;
        console.log('[BS4KDataBroadcastingManager] Destroyed.');
    }

    /** receiverが描画するviewportとiframeをDPlayerの16:9面へ追加する。 */
    private createReceiverElements(): void {
        this.viewport = document.createElement('div');
        this.viewport.className = 'dplayer-konomitv-bs4k-data-broadcast-viewport';
        Object.assign(this.viewport.style, {
            position: 'absolute',
            inset: '0',
            overflow: 'hidden',
            background: 'transparent',
            opacity: '0',
            pointerEvents: 'none',
            zIndex: '0',
        });

        this.iframe = document.createElement('iframe');
        this.iframe.className = 'dplayer-konomitv-bs4k-data-broadcast';
        this.iframe.setAttribute('aria-hidden', 'true');
        this.iframe.tabIndex = -1;
        Object.assign(this.iframe.style, {
            position: 'absolute',
            inset: '0',
            width: '100%',
            height: '100%',
            border: '0',
            display: 'none',
            opacity: '0',
            pointerEvents: 'none',
            zIndex: '2',
            transformOrigin: '0 0',
            // 親画面のdark color-schemeによる白いiframe backdropを避ける。
            colorScheme: 'only light',
        });

        // 背景、video、iframeを同じ16:9コンテナのsiblingとしてz=0 < 1 < 2で合成する。
        this.player.template.videoWrapAspect.prepend(this.viewport, this.iframe);
    }

    /** ARIB HTML5 receiver hostとiframe runtime installerを構築する。 */
    private createReceiverHost(): void {
        if (this.iframe === null || this.viewport === null) return;

        const receiver_info = this.readReceiverInfo();
        this.media_plane_adapter = new KonomiTVBS4KMediaPlaneAdapter(this.player);
        this.host = new AribReceiverHost({
            iframe: this.iframe,
            viewport: this.viewport,
            broadcastBaseUrl: '/data-broadcast/',
            allowExternalNetwork: useSettingsStore().settings.enable_internet_access_from_data_broadcasting,
            systemInformation: {
                zipcode: receiver_info.zipcode,
                prefecture: receiver_info.prefecture,
                regioncode: receiver_info.regioncode,
            },
            mediaPlaneAdapter: this.media_plane_adapter,
            // B62字幕は本カードの対象外。現行字幕経路を抑止しない。
            onCaptionSubscription: () => undefined,
            // libaribhtml5既定のalert()はKonomiTV-BS4KのUI規約に反するため表示しない。
            onProgramGuideUnavailable: () => undefined,
            onViewerParticipation: () => {
                this.player.notice(
                    '視聴者参加型データ放送が始まりました。リモコンの d ボタンから操作できます。',
                    5000,
                );
            },
            onReplaceApplication: async request => {
                const application = [...this.applications.values()].find(candidate =>
                    candidate.state.organizationId === request.organizationId &&
                    candidate.state.applicationId === request.applicationId);
                if (application === undefined) {
                    throw new Error(`MH-AIT application not found: ${request.organizationId}:${request.applicationId}`);
                }
                await this.loadManagedApplication(application, 'アプリケーション切替中');
            },
            onLifecycle: event => {
                if (event.type === 'installed') {
                    // 放送ページ内のlocation.replace()/reloadではpendingがnullなので既存identityを維持する。
                    if (this.pending_application_key !== null) {
                        this.active_application_key = this.pending_application_key;
                        this.pending_application_key = null;
                    }
                    this.applyApplicationVisibility();
                    if (this.dispatch_data_key_after_install) {
                        this.dispatch_data_key_after_install = false;
                        this.show_requested = false;
                        this.host?.setApplicationInputActive(true);
                        this.host?.dispatchKey(RECEIVER_KEY_MAP[20]);
                    } else if (this.show_requested) {
                        void this.enterApplication();
                    }
                } else if (event.type === 'exited') {
                    this.active_application_key = null;
                    this.pending_application_key = null;
                    this.visible = false;
                    this.media_plane_adapter?.setApplicationVisible(false);
                    if (this.viewport !== null) this.viewport.style.opacity = '0';
                    if (this.iframe !== null) {
                        this.iframe.style.opacity = '0';
                        this.iframe.style.display = 'none';
                        this.iframe.setAttribute('aria-hidden', 'true');
                    }
                } else if (event.type === 'error') {
                    console.error('[BS4KDataBroadcastingManager] Receiver runtime failed.', event.message);
                    this.player.notice(
                        'データ放送を起動できませんでした。',
                        3000,
                        undefined,
                        'rgb(var(--v-theme-error-readable))',
                    );
                }
            },
        });

        const host_window = window as KonomiTVBS4KRuntimeHostWindow;
        this.previous_installer = host_window.__ARIB_HTML5_INSTALL__;
        this.runtime_installer = target => {
            this.host?.installRuntime(target);
            this.installReceiverFontFallback(target);
            this.installNetworkProxy(target);
        };
        host_window.__ARIB_HTML5_INSTALL__ = this.runtime_installer;
    }

    /** 再生中の画質・codec queryと同じLiveStreamのSSEへ接続する。 */
    private openEventSource(lifecycle_generation: number): void {
        const channels_store = useChannelsStore();
        const event_source_url = PlayerUtils.buildKonomiTVBS4KLiveAPIEndpointURLFromDPlayer(
            this.player,
            channels_store.channel.current.display_channel_id,
            'data-broadcast',
        );
        const event_source = new EventSource(event_source_url);
        this.event_source = event_source;
        const is_current = (): boolean => (
            this.lifecycle_generation === lifecycle_generation &&
            this.event_source === event_source
        );

        event_source.addEventListener('open', () => {
            if (is_current()) console.log('[BS4KDataBroadcastingManager] EventSource opened.');
        });
        event_source.addEventListener('error', () => {
            if (is_current()) console.warn('[BS4KDataBroadcastingManager] EventSource disconnected; waiting for retry.');
        });
        for (const event_type of BS4KDataBroadcastingManager.SSE_EVENT_TYPES) {
            event_source.addEventListener(event_type, event_raw => {
                if (is_current() === false || !(event_raw instanceof MessageEvent)) return;
                let payload: unknown;
                try {
                    payload = JSON.parse(event_raw.data);
                } catch (error) {
                    console.error('[BS4KDataBroadcastingManager] Invalid SSE event payload.', error);
                    return;
                }
                this.enqueueDatacastEvent(event_type, payload, lifecycle_generation);
            });
        }
    }

    /** 非同期VFS操作を含むSSEイベントを受信順のまま処理する。 */
    private enqueueDatacastEvent(
        event_type: BS4KDatacastEventType,
        payload: unknown,
        lifecycle_generation: number,
    ): void {
        this.event_processing_tail = this.event_processing_tail.then(async () => {
            if (this.lifecycle_generation !== lifecycle_generation) return;
            await this.handleDatacastEvent(event_type, payload);
        }).catch(error => {
            if (this.lifecycle_generation !== lifecycle_generation) return;
            console.error(`[BS4KDataBroadcastingManager] Failed to apply ${event_type}.`, error);
        });
    }

    /** 1件のSSEイベントをclient shimの状態とlibaribhtml5へ反映する。 */
    private async handleDatacastEvent(event_type: BS4KDatacastEventType, payload: unknown): Promise<void> {
        if (event_type === 'snapshot_begin' || event_type === 'application_resources_reset') {
            await this.beginDatacastSession();
            return;
        }
        if (event_type === 'snapshot_end') {
            await this.finishDatacastSnapshot();
            return;
        }
        if (event_type === 'application_state') {
            this.handleApplicationState(payload as BS4KApplicationState);
            return;
        }
        if (event_type === 'application_resource') {
            this.handleApplicationResource(payload as BS4KApplicationResource);
            return;
        }
        if (event_type === 'application_resource_removed') {
            await this.handleApplicationResourceRemoved(payload as BS4KApplicationResourceRemoved);
            return;
        }
        if (event_type === 'broadcast_clock') {
            this.handleBroadcastClock(payload as BS4KBroadcastClock);
            return;
        }
        if (event_type === 'layout_configuration') {
            this.handleLayoutConfiguration(payload as BS4KLayoutConfiguration);
            return;
        }
        if (event_type === 'event_info') {
            await this.handleEventInfo(payload as BS4KEventInfo);
            return;
        }
        if (event_type === 'stream_event') {
            this.handleStreamEvent(payload as BS4KStreamEvent);
            return;
        }
        if (event_type === 'viewer_participation') {
            this.handleViewerParticipation(payload as BS4KViewerParticipationNotification);
        }
    }

    /** snapshot/reset境界で旧サービスの状態とVFSをまとめて破棄する。 */
    private async beginDatacastSession(): Promise<void> {
        this.resources.clear();
        this.resource_revisions.clear();
        this.application_states.clear();
        this.applications.clear();
        this.program_events.clear();
        this.is_snapshot_in_progress = true;
        this.current_context_id = null;
        this.layout_configuration = null;
        this.resetReceiverState();
        ++this.vfs_generation;
        await this.vfs?.beginSession();
    }

    /** snapshot終端で選局serviceのcontextを確定し、そのcontextだけをVFSへ公開する。 */
    private async finishDatacastSnapshot(): Promise<void> {
        this.is_snapshot_in_progress = false;
        if (this.current_context_id === null) {
            const application_contexts = new Set(
                [...this.application_states.values()].map(state => state.contextId),
            );
            // MH-EITがない放送でも、application contextが一意なら誤選択の余地がないため利用できる。
            if (application_contexts.size === 1) {
                this.current_context_id = application_contexts.values().next().value ?? null;
            }
        }
        await this.rebuildCurrentContextVfs();
        this.refreshManagedApplications();
        this.applyCurrentLayoutConfiguration();
    }

    /** receiver側でサービスごとに保持してはならない状態を初期値へ戻す。 */
    private resetReceiverState(): void {
        this.ready_application_key = null;
        this.active_application_key = null;
        this.pending_application_key = null;
        this.show_requested = false;
        this.dispatch_data_key_after_install = false;
        this.autostart_scheduled = false;
        this.host?.resetCaptions();
        this.host?.setCaptionTracks([]);
        this.host?.clearBroadcastClock();
        this.host?.clearProgramInfo();
        this.seedProgramIdentity();
        this.host?.setLctBackgroundColor(null);
        this.host?.resetViewerParticipationNotifications();
        this.exitApplication();
        useChannelsStore().current_program_present = null;
        useChannelsStore().current_program_following = null;
        this.toggleRemoconLoading(true);
        this.toggleRemoconEnabled(false);
    }

    /** MH-AITがMH-EITより先に届いても現在サービスを参照できるよう選局情報を先に渡す。 */
    private seedProgramIdentity(): void {
        const channel = useChannelsStore().channel.current;
        if (channel.service_id === null || channel.service_id === 0) return;
        const program_info: ProgramInfo = {service_id: channel.service_id};
        if (channel.network_id !== null && channel.network_id !== 0) {
            program_info.original_network_id = channel.network_id;
        }
        this.host?.setProgramInfo(program_info);
    }

    /** Base64リソースをVFSとentry解決用mapへ反映する。 */
    private handleApplicationResource(resource: BS4KApplicationResource): void {
        if (this.vfs === null || typeof resource?.path !== 'string' || typeof resource?.data !== 'string') return;
        const path = this.normalizeBroadcastPath(resource.path);
        if (path === null) return;

        const data = this.decodeBase64(resource.data);
        if (data === null) return;
        const content_type = this.normalizeResourceContentType(path, resource.contentType);
        const stored: StoredApplicationResource = {
            contextId: resource.contextId,
            path,
            contentType: content_type,
            // 放送側scriptより先にreceiverDeviceを同期導入する。
            data: content_type.startsWith('text/html') ? this.injectRuntimeBootstrap(data) : data,
        };
        const key = this.resourceKey(resource.contextId, path);
        this.resources.set(key, stored);
        if (this.is_snapshot_in_progress || resource.contextId !== this.current_context_id) return;
        const revision = this.vfs.enqueue(stored);
        this.resource_revisions.set(key, revision);
        const lifecycle_generation = this.lifecycle_generation;
        const vfs_generation = this.vfs_generation;
        void this.vfs.waitFor(revision).then(() => {
            if (lifecycle_generation !== this.lifecycle_generation || vfs_generation !== this.vfs_generation) return;
            if (this.resource_revisions.get(key) === revision) this.host?.notifyDataResource(path, 'updated');
        }).catch(error => {
            if (lifecycle_generation !== this.lifecycle_generation || vfs_generation !== this.vfs_generation) return;
            console.error('[BS4KDataBroadcastingManager] Failed to publish a VFS resource.', error);
        });
        this.refreshManagedApplications(resource.contextId);
        if (this.show_requested) void this.enterApplication();
    }

    /** 削除済みresourceをWorker内へ残さないよう、残存mapからVFS sessionを再構築する。 */
    private async handleApplicationResourceRemoved(resource: BS4KApplicationResourceRemoved): Promise<void> {
        const path = this.normalizeBroadcastPath(resource?.path);
        if (path === null || this.vfs === null) return;
        const key = this.resourceKey(resource.contextId, path);
        this.resources.delete(key);
        this.resource_revisions.delete(key);
        if (resource.contextId !== this.current_context_id) return;
        this.host?.notifyDataResource(path, 'deleted');
        await this.rebuildCurrentContextVfs();
        this.refreshManagedApplications(resource.contextId);
    }

    /** 現在serviceのresourceだけでVFSを再構築し、他contextの同名pathとの衝突を防ぐ。 */
    private async rebuildCurrentContextVfs(): Promise<void> {
        if (this.vfs === null) return;
        // 旧revisionを待機中の読込は新sessionへ持ち越さず、再構築後のentryでやり直す。
        if (this.pending_application_key !== null) {
            this.pending_application_key = null;
            this.show_requested = true;
        }
        ++this.vfs_generation;
        await this.vfs.beginSession();
        this.resource_revisions.clear();
        // Worker内の同一origin proxyも設定OFF時は拒否できるよう、VFS sessionへ権限を明示する。
        if (useSettingsStore().settings.enable_internet_access_from_data_broadcasting) {
            this.vfs.enqueue({
                path: KONOMITV_BS4K_EXTERNAL_NETWORK_PERMISSION_PATH,
                contentType: 'application/octet-stream',
                data: new Uint8Array(),
            });
        }
        if (this.current_context_id === null) return;
        for (const stored of this.resources.values()) {
            if (stored.contextId !== this.current_context_id) continue;
            const key = this.resourceKey(stored.contextId, stored.path);
            this.resource_revisions.set(key, this.vfs.enqueue(stored));
        }
    }

    /** application差分を保存し、client側resource mapからentryを再解決する。 */
    private handleApplicationState(state: BS4KApplicationState): void {
        if (!state || !Number.isInteger(state.contextId)) return;
        const key = this.applicationKey(state);
        if (state.controlCode === 0x04) {
            this.application_states.delete(key);
            this.removeManagedApplication(key);
            return;
        }
        this.application_states.set(key, {...state});
        if (this.is_snapshot_in_progress || state.contextId !== this.current_context_id) return;
        this.refreshManagedApplication(state);
    }

    /** 指定contextまたは全applicationのentry候補を現在のresource mapと照合する。 */
    private refreshManagedApplications(context_id?: number): void {
        // 途中の候補で旧ready applicationを起動せず、全候補を解決してからユーザー操作を再試行する。
        const retry_show_request = this.show_requested;
        this.show_requested = false;
        for (const application of [...this.applications.values()]) {
            if (application.state.contextId !== this.current_context_id) {
                this.removeManagedApplication(application.key);
            }
        }
        for (const state of this.application_states.values()) {
            if (state.contextId !== this.current_context_id) continue;
            if (context_id === undefined || state.contextId === context_id) this.refreshManagedApplication(state);
        }
        if (retry_show_request) {
            this.show_requested = true;
            void this.enterApplication();
        }
    }

    /** 1 applicationのentryを解決し、リモコン入口とAUTOSTART候補へ反映する。 */
    private refreshManagedApplication(state: BS4KApplicationState): void {
        const key = this.applicationKey(state);
        const entry = this.resolveApplicationEntry(state);
        if (entry === null) {
            this.removeManagedApplication(key);
            return;
        }

        const previous = this.applications.get(key);
        if (this.pending_application_key === key && previous?.entry !== entry) {
            this.pending_application_key = null;
            this.show_requested = true;
        }
        const application: ManagedApplication = {key, state: {...state}, entry};
        this.applications.set(key, application);
        const current = this.ready_application_key === null
            ? null
            : this.applications.get(this.ready_application_key) ?? null;
        const current_is_present = current?.state.controlCode === 0x02;
        const candidate_is_present = state.controlCode === 0x02;
        const current_priority = Number(current?.state.applicationPriority ?? 0);
        const candidate_priority = Number(state.applicationPriority ?? 0);
        if (current === null ||
            (candidate_is_present && !current_is_present) ||
            (candidate_is_present === current_is_present && candidate_priority >= current_priority)) {
            this.ready_application_key = key;
        }

        this.toggleRemoconLoading(false);
        this.toggleRemoconEnabled(true);
        if (this.show_requested) void this.enterApplication();
        if (this.active_application_key === key) this.applyApplicationVisibility();
        if (state.controlCode === 0x01) this.scheduleAutostart();
    }

    /** resource消失・KILL時にapplication選択と実行状態を整合させる。 */
    private removeManagedApplication(key: string): void {
        this.applications.delete(key);
        if (this.ready_application_key === key) {
            this.ready_application_key = null;
            this.selectReadyApplication();
        }
        if (this.active_application_key === key || this.pending_application_key === key) this.exitApplication();
        if (this.applications.size === 0) {
            this.toggleRemoconLoading(false);
            this.toggleRemoconEnabled(false);
        }
    }

    /** 残存applicationからPRESENT優先・priority降順でリモコン入口を選び直す。 */
    private selectReadyApplication(): void {
        const candidate = [...this.applications.values()].sort((left, right) => {
            const left_present = left.state.controlCode === 0x02 ? 1 : 0;
            const right_present = right.state.controlCode === 0x02 ? 1 : 0;
            return right_present - left_present || right.state.applicationPriority - left.state.applicationPriority;
        })[0];
        this.ready_application_key = candidate?.key ?? null;
    }

    /** card-3で固定した規則に従い、raw entryとtransport prefixをresource mapへ照合する。 */
    private resolveApplicationEntry(state: BS4KApplicationState): string | null {
        const raw_entry = this.normalizeBroadcastPath(state.entryPath);
        if (raw_entry === null) return null;
        if (this.resources.has(this.resourceKey(state.contextId, raw_entry))) return raw_entry;

        for (const transport_url of state.transportUrls) {
            // URL scheme付きtransportは放送VFSの相対entry候補として扱わない。
            if (transport_url.includes('://')) continue;
            const prefix = this.normalizeBroadcastPath(transport_url);
            if (prefix === null) continue;
            const candidate = this.normalizeBroadcastPath(`${prefix}/${raw_entry}`);
            if (candidate !== null && this.resources.has(this.resourceKey(state.contextId, candidate))) {
                return candidate;
            }
        }
        return null;
    }

    /** パストラバーサルをscope外へ逃がさず、VFSで用いる相対pathへ正規化する。 */
    private normalizeBroadcastPath(value: string): string | null {
        if (typeof value !== 'string' || value.includes('\0')) return null;
        const path = value.split(/[?#]/, 1)[0].replaceAll('\\', '/');
        const segments: string[] = [];
        for (const segment of path.split('/')) {
            if (segment === '' || segment === '.') continue;
            if (segment === '..') {
                if (segments.length === 0) return null;
                segments.pop();
                continue;
            }
            segments.push(segment);
        }
        return segments.length > 0 ? segments.join('/') : null;
    }

    private resourceKey(context_id: number, path: string): string {
        return `${context_id}:${path}`;
    }

    private applicationKey(state: BS4KApplicationState): string {
        return `${state.contextId}:${state.applicationType}:${state.organizationId}:${state.applicationId}`;
    }

    /** visibility descriptorに従いユーザーへ表示するapplicationか判定する。 */
    private applicationIsUserVisible(application: ManagedApplication): boolean {
        // descriptor不在時は可視性を推定せず、実行を継続したままユーザー向け合成面だけを隠す。
        return application.state.applicationDescriptorPresent && application.state.visibility === 0x03;
    }

    /** AUTOSTART候補を同一microtask内でpriority順に一度だけ起動する。 */
    private scheduleAutostart(): void {
        if (this.autostart_scheduled) return;
        this.autostart_scheduled = true;
        const lifecycle_generation = this.lifecycle_generation;
        const vfs_generation = this.vfs_generation;
        queueMicrotask(() => {
            this.autostart_scheduled = false;
            if (lifecycle_generation !== this.lifecycle_generation || vfs_generation !== this.vfs_generation ||
                this.pending_application_key !== null) return;
            const candidate = [...this.applications.values()]
                .filter(application => application.state.controlCode === 0x01)
                .sort((left, right) => right.state.applicationPriority - left.state.applicationPriority)[0];
            if (candidate === undefined || candidate.key === this.active_application_key) return;
            if (this.active_application_key !== null) {
                const active = this.applications.get(this.active_application_key);
                if (active?.state.controlCode !== 0x02 || active.state.presentApplicationPriority) return;
                this.exitApplication();
            }
            void this.loadManagedApplication(candidate, 'アプリケーション読込中');
        });
    }

    /** NTP epochの放送時刻をUnix epochへ変換し、映像時刻へ追従するclockとして設定する。 */
    private handleBroadcastClock(clock: BS4KBroadcastClock): void {
        if (!clock || this.host === null || !(clock.broadcastTimeTimescale > 0) || !(clock.mediaTimeTimescale > 0)) return;
        try {
            const epoch_milliseconds = Number(BigInt(clock.broadcastTimeValue)) * 1000 /
                clock.broadcastTimeTimescale - 2208988800 * 1000;
            const media_time_seconds = Number(BigInt(clock.mediaTimeValue)) / clock.mediaTimeTimescale;
            if (!Number.isFinite(epoch_milliseconds) || !Number.isFinite(media_time_seconds)) return;
            this.host.setBroadcastClock({
                epochMilliseconds: epoch_milliseconds,
                mediaTimeSeconds: media_time_seconds,
                currentMediaTimeSeconds: () => this.player.video.currentTime,
            });
            this.updateProgramInfo();
        } catch {
            // 不正な64bit値は現在時刻を壊さず破棄する。
        }
    }

    /** LCTが指定する背景平面だけをreceiverへ反映する。 */
    private handleLayoutConfiguration(layout: BS4KLayoutConfiguration): void {
        if (!layout || !Number.isInteger(layout.contextId)) return;
        this.layout_configuration = layout;
        this.applyCurrentLayoutConfiguration();
    }

    private applyCurrentLayoutConfiguration(): void {
        const layout = this.layout_configuration;
        this.host?.setLctBackgroundColor(
            layout !== null && layout.contextId === this.current_context_id ? layout.backgroundColorRgb : null,
        );
    }

    /** EMTをlibaribhtml5のRuntimeEventへ変換する。 */
    private handleStreamEvent(event: BS4KStreamEvent): void {
        if (!event || event.contextId !== this.current_context_id || event.currentNext === false ||
            event.timeMode !== 0 || this.host === null) return;
        const private_data = this.decodeBase64(event.privateData);
        if (private_data === null) return;

        const present = this.program_events.get(0);
        const source: RuntimeEvent['source'] = {event_message_tag: event.eventMessageTag};
        if (present !== undefined) {
            source.original_network_id = present.originalNetworkId;
            source.tlv_stream_id = present.tlvStreamId;
            source.service_id = present.serviceId;
        }
        this.host.emitStreamEvent({
            source,
            message_group_id: event.messageGroupId,
            message_id: event.messageId,
            message_version: event.messageVersion,
            private_data_byte: this.bytesToDomString(private_data),
        });
    }

    /** TR-B39視聴者参加通知をreceiver hostへ渡す。 */
    private handleViewerParticipation(event: BS4KViewerParticipationNotification): void {
        if (!event || event.contextId !== this.current_context_id || event.currentNext === false || this.host === null) return;
        try {
            const notification: AribViewerParticipationNotification = {
                ...event,
                inputOffset: BigInt(event.inputOffset),
            };
            this.host.notifyViewerParticipationCorner(notification);
        } catch {
            // 不正な64bit値を持つ通知はreceiverへ渡さない。
        }
    }

    /** MH-EIT p/fを保持し、現在・次番組をreceiverとChannelsStoreへ反映する。 */
    private async handleEventInfo(event: BS4KEventInfo): Promise<void> {
        if (!event || event.tableId !== 0x8b || event.currentNext === false || ![0, 1].includes(event.sectionNumber)) return;
        const current_service_id = useChannelsStore().channel.current.service_id;
        if (event.serviceId !== current_service_id) return;
        const context_changed = this.current_context_id !== event.contextId;
        this.current_context_id = event.contextId;
        this.program_events.set(event.sectionNumber, event);
        this.updateProgramInfo();
        if (context_changed && this.is_snapshot_in_progress === false) {
            await this.rebuildCurrentContextVfs();
            this.refreshManagedApplications();
            this.applyCurrentLayoutConfiguration();
        }
    }

    /** 受信済みMH-EITからlibaribhtml5の番組情報を組み立てる。 */
    private updateProgramInfo(): void {
        const present = this.program_events.get(0);
        if (present === undefined || present.startTimeUnixMilliseconds === null || present.durationSeconds === null) return;
        const program_info: ProgramInfo = {
            original_network_id: present.originalNetworkId,
            transport_stream_id: present.tlvStreamId,
            service_id: present.serviceId,
            event_id: present.eventId,
            name: present.title,
            event_name: present.title,
            start_time: dayjs(present.startTimeUnixMilliseconds).toDate(),
            duration: present.durationSeconds * 1000,
            desc: present.description,
            event_text: present.description,
            running_status: present.runningStatus,
            free_ca_mode: present.freeCaMode,
        };
        const following = this.program_events.get(1);
        if (following !== undefined && following.startTimeUnixMilliseconds !== null && following.durationSeconds !== null) {
            Object.assign(program_info, {
                f_event_id: following.eventId,
                f_name: following.title,
                f_start_time: dayjs(following.startTimeUnixMilliseconds).toDate(),
                f_duration: following.durationSeconds * 1000,
                f_desc: following.description,
            });
        }
        this.host?.setProgramInfo(program_info);
    }

    /** リモコン操作を通常選局と表示中applicationへ振り分ける。 */
    private initRemoconButtons(lifecycle_generation: number): void {
        this.remocon_abort_controller = new AbortController();
        const signal = this.remocon_abort_controller.signal;
        this.remocon_element.querySelectorAll<HTMLButtonElement>('button').forEach(button => {
            button.addEventListener('click', () => {
                if (lifecycle_generation !== this.lifecycle_generation) return;
                const key_code = Number(button.dataset.aribKeyCode);
                const remocon_id = button.dataset.remoconId !== undefined ? Number(button.dataset.remoconId) : null;

                // application非表示時の数字キーは従来どおりチャンネル選局へ使う。
                if (remocon_id !== null && this.visible === false) {
                    const channels_store = useChannelsStore();
                    const channel = channels_store.channel.current;
                    const switch_channel = channels_store.getChannelByRemoconID(
                        channel.type,
                        remocon_id,
                        channel.is_oneseg,
                    );
                    if (switch_channel !== null && switch_channel.display_channel_id !== channels_store.display_channel_id) {
                        void router.push({path: `/tv/watch/${switch_channel.display_channel_id}`});
                    }
                    return;
                }
                if (key_code === 20 && this.visible === false) {
                    void this.enterApplication();
                    return;
                }
                const receiver_key = remocon_id !== null ? this.resolveNumberKey(remocon_id) : RECEIVER_KEY_MAP[key_code];
                if (receiver_key !== undefined) {
                    this.host?.setApplicationInputActive(true);
                    this.host?.dispatchKey(receiver_key);
                }
            }, {signal});
        });
    }

    /** dボタン要求をready applicationの起動または表示中applicationへのキー配送へ変換する。 */
    private async enterApplication(): Promise<void> {
        if (this.ready_application_key === null) {
            if (this.active_application_key !== null) {
                this.show_requested = false;
                this.host?.setApplicationInputActive(true);
                this.host?.dispatchKey(RECEIVER_KEY_MAP[20]);
            } else {
                // MH-AITまたはentry resourceがまだ揃わない場合は後続差分で再試行する。
                this.show_requested = true;
            }
            return;
        }
        const application = this.applications.get(this.ready_application_key);
        if (application === undefined) {
            this.show_requested = true;
            return;
        }
        if (application.key === this.active_application_key) {
            this.show_requested = false;
            this.host?.setApplicationInputActive(true);
            this.host?.dispatchKey(RECEIVER_KEY_MAP[20]);
            return;
        }
        if (this.pending_application_key !== null) {
            this.show_requested = true;
            return;
        }
        this.dispatch_data_key_after_install = application.state.controlCode === 0x01;
        this.show_requested = false;
        await this.loadManagedApplication(application, 'アプリケーション読込中');
    }

    /** entryのVFS書込み完了を待ち、scope内URLとしてreceiverへロードする。 */
    private async loadManagedApplication(application: ManagedApplication, status: string): Promise<void> {
        if (this.vfs === null || this.iframe === null || this.pending_application_key !== null) return;
        const lifecycle_generation = this.lifecycle_generation;
        const vfs_generation = this.vfs_generation;
        const revision = this.resource_revisions.get(this.resourceKey(application.state.contextId, application.entry));
        if (revision === undefined) return;
        this.pending_application_key = application.key;
        try {
            await this.vfs.waitFor(revision);
            await this.vfs.ensure(application.entry, revision);
            if (lifecycle_generation !== this.lifecycle_generation || vfs_generation !== this.vfs_generation ||
                this.pending_application_key !== application.key ||
                this.applications.get(application.key)?.entry !== application.entry) return;
            const base_url = new URL('/data-broadcast/', location.origin);
            const application_url = new URL(application.entry.replace(/^\/+/, ''), base_url);
            if (application_url.origin !== location.origin || !application_url.pathname.startsWith(base_url.pathname)) {
                throw new Error(`Application entry escaped receiver scope: ${application.entry}`);
            }
            this.iframe.style.display = 'block';
            this.iframe.style.opacity = '0';
            if (this.viewport !== null) this.viewport.style.opacity = '0';
            this.media_plane_adapter?.setApplicationVisible(this.applicationIsUserVisible(application));
            this.host?.setApplicationInformation({
                type: `0x${application.state.applicationType.toString(16).padStart(4, '0')}`,
                organizationId: application.state.organizationId,
                applicationId: application.state.applicationId,
                controlCode: application.state.controlCode === 0x01
                    ? 'AUTOSTART'
                    : application.state.controlCode === 0x02 ? 'PRESENT' : '',
            });
            this.host?.loadApplication(application_url.href, status);
        } catch (error) {
            if (lifecycle_generation !== this.lifecycle_generation || vfs_generation !== this.vfs_generation) return;
            if (this.pending_application_key === application.key) this.pending_application_key = null;
            this.exitApplication();
            console.error('[BS4KDataBroadcastingManager] Failed to enter application.', error);
            this.player.notice(
                'データ放送を起動できませんでした。',
                3000,
                undefined,
                'rgb(var(--v-theme-error-readable))',
            );
        }
    }

    /** active applicationのvisibilityをiframeと映像面へ同時反映する。 */
    private applyApplicationVisibility(): void {
        const application = this.active_application_key === null
            ? null
            : this.applications.get(this.active_application_key) ?? null;
        const visible = application !== null && this.applicationIsUserVisible(application);
        this.visible = visible;
        this.host?.setApplicationInputActive(visible);
        this.media_plane_adapter?.setApplicationVisible(visible);
        if (this.viewport !== null) this.viewport.style.opacity = visible ? '1' : '0';
        if (this.iframe !== null) {
            // 非表示AUTOSTARTも実行は継続し、ユーザー向け合成面だけを隠す。
            this.iframe.style.display = application === null ? 'none' : 'block';
            this.iframe.style.opacity = visible ? '1' : '0';
            this.iframe.setAttribute('aria-hidden', visible ? 'false' : 'true');
        }
    }

    /** receiver applicationを終了しDPlayer本来の映像配置へ戻す。 */
    private exitApplication(): void {
        this.host?.exitApplication();
        this.active_application_key = null;
        this.pending_application_key = null;
        this.dispatch_data_key_after_install = false;
        this.visible = false;
        this.host?.setApplicationInputActive(false);
        this.media_plane_adapter?.setApplicationVisible(false);
        if (this.viewport !== null) this.viewport.style.opacity = '0';
        if (this.iframe !== null) {
            this.iframe.style.opacity = '0';
            this.iframe.style.display = 'none';
            this.iframe.setAttribute('aria-hidden', 'true');
        }
    }

    private resolveNumberKey(remocon_id: number): number | undefined {
        if (remocon_id >= 1 && remocon_id <= 9) return 48 + remocon_id;
        if (remocon_id === 10) return 48;
        return undefined;
    }

    /** ARIB記号フォントを放送ページの既定fallbackへ追加する。 */
    private installReceiverFontFallback(target: RuntimeWindow): void {
        if (target.document.querySelector('style[data-konomitv-bs4k-arib-font-fallback]') !== null) return;
        const style = target.document.createElement('style');
        style.dataset.konomitvBs4kAribFontFallback = '';
        style.textContent = `
            html, body {
                font-family: "ARIB Symbols", "Hiragino Kaku Gothic ProN", "Yu Gothic", YuGothic,
                    Meiryo, "Noto Sans CJK JP", "Noto Sans JP", sans-serif;
            }
        `;
        (target.document.head ?? target.document.documentElement).append(style);
    }

    /** 放送ページのFetch/XHR/sendBeaconを既存データ放送プロキシへ接続する。 */
    private installNetworkProxy(target: RuntimeWindow): void {
        if (useSettingsStore().settings.enable_internet_access_from_data_broadcasting === false) return;

        const proxyUrl = (value: string): string | null => {
            let url: URL;
            try {
                url = new URL(value, target.location.href);
            } catch {
                return null;
            }
            if (!['http:', 'https:'].includes(url.protocol) || url.origin === target.location.origin) return null;
            return new URL(
                `/api/data-broadcasting/request/${encodeURIComponent(url.href)}`,
                target.location.origin,
            ).href;
        };

        const original_fetch = target.fetch.bind(target);
        target.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
            const is_request = input instanceof target.Request;
            const request_url = is_request ? (input as Request).url : String(input);
            const proxied_url = proxyUrl(request_url);
            if (proxied_url === null) return original_fetch(input, init);
            const proxied_input = is_request ? new target.Request(proxied_url, input as Request) : proxied_url;
            return original_fetch(proxied_input, init);
        };

        const original_open = target.XMLHttpRequest.prototype.open;
        target.XMLHttpRequest.prototype.open = function(
            this: XMLHttpRequest,
            method: string,
            url: string | URL,
            ...args: unknown[]
        ): void {
            const proxied_url = proxyUrl(String(url));
            Reflect.apply(original_open, this, [method, proxied_url ?? url, ...args]);
        } as typeof target.XMLHttpRequest.prototype.open;

        if (typeof target.navigator.sendBeacon === 'function') {
            const original_send_beacon = target.navigator.sendBeacon.bind(target.navigator);
            Object.defineProperty(target.navigator, 'sendBeacon', {
                configurable: true,
                value: (url: string | URL, data?: BodyInit | null): boolean =>
                    original_send_beacon(proxyUrl(String(url)) ?? url, data),
            });
        }
    }

    private normalizeResourceContentType(path: string, content_type: string): string {
        const pathname = path.toLowerCase();
        if (pathname.endsWith('.html') || pathname.endsWith('.htm')) return 'text/html; charset=utf-8';
        if (pathname.endsWith('.css')) return 'text/css; charset=utf-8';
        return content_type;
    }

    private injectRuntimeBootstrap(data: Uint8Array): Uint8Array {
        const source = new TextDecoder().decode(data);
        const bootstrap = '<script>top.__ARIB_HTML5_INSTALL__?.(window)</script>';
        const head = /<head(?:\s[^>]*)?>/i.exec(source);
        const prepared = head === null ? `${bootstrap}${source}` :
            `${source.slice(0, head.index + head[0].length)}${bootstrap}${source.slice(head.index + head[0].length)}`;
        return new TextEncoder().encode(prepared);
    }

    /** 既存BMLBrowserと同じLocalStorageから受信者情報を互換読取する。 */
    private readReceiverInfo(): {zipcode: string | null; prefecture: number | null; regioncode: number | null} {
        const decode = (name: string): string | null => {
            const value = localStorage.getItem(`${LEGACY_RECEIVER_INFO_PREFIX}${name}`);
            if (value === null) return null;
            try {
                return window.atob(value);
            } catch {
                return null;
            }
        };
        const zipcode = decode('zipcode');
        const prefecture_raw = decode('prefecture');
        const regioncode_raw = decode('regioncode');
        return {
            zipcode: zipcode?.match(/^\d{7}$/) ? zipcode : null,
            prefecture: prefecture_raw?.length === 1 ? prefecture_raw.charCodeAt(0) : null,
            regioncode: regioncode_raw?.length === 2
                ? (regioncode_raw.charCodeAt(0) << 8) | regioncode_raw.charCodeAt(1)
                : null,
        };
    }

    private decodeBase64(value: string): Uint8Array | null {
        try {
            const binary = window.atob(value);
            const result = new Uint8Array(binary.length);
            for (let index = 0; index < binary.length; index++) result[index] = binary.charCodeAt(index);
            return result;
        } catch {
            return null;
        }
    }

    private bytesToDomString(data: Uint8Array): string {
        let result = '';
        for (let offset = 0; offset < data.byteLength; offset += 8192) {
            result += String.fromCharCode(...data.subarray(offset, offset + 8192));
        }
        return result;
    }

    private toggleRemoconLoading(loading: boolean): void {
        this.remocon_data_broadcasting_element.classList.toggle('remote-control-data-broadcasting--loading', loading);
    }

    private toggleRemoconEnabled(enabled: boolean): void {
        this.remocon_data_broadcasting_element.classList.toggle('remote-control-data-broadcasting--disabled', !enabled);
    }
}

export default BS4KDataBroadcastingManager;
