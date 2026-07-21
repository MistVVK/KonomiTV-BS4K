
import type { RouteRecordRaw } from 'vue-router';

import useVersionStore from '@/stores/VersionStore';
import Utils from '@/utils';


/** 設定ナビゲーションに表示するアプリ内リンク。 */
export interface SettingsNavigationRouteItem {
    type: 'Route';
    label: string;
    icon: string;
    iconWidth?: string;
    iconStyle?: string;
    to: string;
    activePaths?: readonly string[];
}

/** 設定ナビゲーションに表示する外部リンク。 */
export interface SettingsNavigationExternalLinkItem {
    type: 'ExternalLink';
    label: string;
    icon: string;
    iconWidth?: string;
    iconStyle?: string;
    href: string;
    ariaLabel: string;
}

export type SettingsNavigationItem = SettingsNavigationRouteItem | SettingsNavigationExternalLinkItem;

/** 目的と適用範囲で整理した設定ナビゲーションの分類。 */
export interface SettingsNavigationCategory {
    label: string;
    items: readonly SettingsNavigationItem[];
}

// データ放送だけは KonomiTV 独自のインライン SVG を使うため、Iconify のアイコン名と衝突しない識別子を保持する。
export const SETTINGS_DATA_BROADCASTING_ICON = 'KonomiTVDataBroadcasting';

// PC のサイドメニューと SP / タブレットの設定一覧は、必ずこの定義から同じ順序で生成する。
export const SETTINGS_NAVIGATION_CATEGORIES: readonly SettingsNavigationCategory[] = [
    {
        label: '個人設定',
        items: [
            {type: 'Route', label: '表示・操作', icon: 'fa-solid:sliders-h', iconStyle: 'padding: 0 3px;', to: '/settings/personal/display'},
            {type: 'Route', label: 'カラーテーマ', icon: 'fluent:paint-brush-24-filled', to: '/settings/personal/color-theme'},
            {type: 'Route', label: '再生・画質', icon: 'fluent:video-clip-multiple-16-filled', to: '/settings/personal/quality'},
            {type: 'Route', label: '字幕・コメント', icon: 'fluent:subtitles-16-filled', to: '/settings/personal/caption-comments'},
            {type: 'Route', label: 'データ放送', icon: SETTINGS_DATA_BROADCASTING_ICON, to: '/settings/personal/data-broadcasting'},
            {type: 'Route', label: 'キャプチャ', icon: 'fluent:image-multiple-16-filled', to: '/settings/personal/capture'},
        ],
    },
    {
        label: 'アカウント・データ',
        items: [
            {type: 'Route', label: 'アカウント・データ', icon: 'fluent:person-20-filled', to: '/settings/account'},
            {type: 'Route', label: 'ニコニコ実況', icon: 'bi:chat-left-text-fill', iconStyle: 'padding: 0 2px;', to: '/settings/account/niconico'},
            {type: 'Route', label: 'Twitter / Bluesky 連携', icon: 'fa-brands:twitter', iconStyle: 'padding: 0 1px;', to: '/settings/account/social'},
        ],
    },
    {
        label: 'サーバー管理',
        items: [
            {type: 'Route', label: '基本・接続', icon: 'fluent:server-surface-16-filled', to: '/settings/server/basic'},
            {type: 'Route', label: '配信・エンコーダー', icon: 'fluent:video-settings-20-filled', to: '/settings/server/streaming'},
            {type: 'Route', label: '録画・ストレージ', icon: 'fluent:hard-drive-20-filled', to: '/settings/server/storage'},
            {
                type: 'Route',
                label: '録画シリーズ',
                icon: 'fluent:collections-20-filled',
                to: '/settings/server/recorded-series',
                activePaths: ['/settings/server/recorded-series/series'],
            },
            {
                type: 'Route',
                label: 'CM管理',
                icon: 'fluent:timeline-20-filled',
                to: '/settings/server/cm-analysis',
                activePaths: ['/settings/server/cm-analysis/logos'],
            },
            {type: 'Route', label: 'ユーザー管理', icon: 'fluent:people-team-20-filled', to: '/settings/server/users'},
        ],
    },
    {
        label: 'メンテナンス',
        items: [
            {type: 'Route', label: '診断・データ保守', icon: 'fluent:wrench-settings-20-filled', to: '/settings/maintenance'},
            {type: 'Route', label: 'サーバー操作', icon: 'fluent:power-20-filled', to: '/settings/maintenance/server'},
        ],
    },
    {
        label: '情報',
        items: [
            {
                type: 'ExternalLink',
                label: 'サードパーティーライセンス',
                icon: 'fluent:document-text-20-filled',
                href: '/api/version/third-party-licenses',
                ariaLabel: 'サードパーティーソフトウェアのライセンス',
            },
        ],
    },
];

/**
 * 現在のサーバーで利用できる機能に合わせた設定ナビゲーションを返す。
 * ニコニコ実況 / NX-Jikkyo がサーバー全体で無効な場合は、設定ページへの導線自体を表示しない。
 */
export function getSettingsNavigationCategories(jikkyo_enabled_on_server: boolean): readonly SettingsNavigationCategory[] {
    return SETTINGS_NAVIGATION_CATEGORIES.map(category => ({
        ...category,
        items: category.items.filter(item =>
            jikkyo_enabled_on_server === true || item.type !== 'Route' || item.to !== '/settings/account/niconico',
        ),
    }));
}

/** ニコニコ実況設定への直接アクセスを、サーバーの実稼働設定に基づいて制御する。 */
async function redirectDisabledJikkyoSettings(): Promise<true | {path: string}> {
    const version_store = useVersionStore();
    await version_store.fetchServerVersion();
    if (version_store.is_jikkyo_enabled_on_server === false) {
        return {path: '/settings/account'};
    }
    return true;
}

// 設定画面の正規 URL。同じ目的の設定は同一ページに集約し、保存方式や API は変更しない。
const CANONICAL_SETTINGS_ROUTES: RouteRecordRaw[] = [
    {
        path: '/settings/personal/display',
        name: 'Settings Personal Display',
        component: () => import('@/views/Settings/General.vue'),
        props: {section: 'display'},
    },
    {
        path: '/settings/personal/color-theme',
        name: 'Settings Personal Color Theme',
        component: () => import('@/views/Settings/ColorTheme.vue'),
    },
    {
        path: '/settings/personal/quality',
        name: 'Settings Personal Quality',
        component: () => import('@/views/Settings/Playback.vue'),
    },
    {
        path: '/settings/personal/caption-comments',
        name: 'Settings Personal Caption Comments',
        component: () => import('@/views/Settings/CaptionComments.vue'),
    },
    {
        path: '/settings/personal/data-broadcasting',
        name: 'Settings Personal Data Broadcasting',
        component: () => import('@/views/Settings/DataBroadcasting.vue'),
    },
    {
        path: '/settings/personal/capture',
        name: 'Settings Personal Capture',
        component: () => import('@/views/Settings/Capture.vue'),
    },
    {
        path: '/settings/account',
        name: 'Settings Account Data',
        component: () => import('@/views/Settings/Account.vue'),
        props: {section: 'all'},
    },
    {
        path: '/settings/account/niconico',
        name: 'Settings Account Niconico',
        component: () => import('@/views/Settings/Jikkyo.vue'),
        props: {section: 'account'},
        beforeEnter: redirectDisabledJikkyoSettings,
    },
    {
        path: '/settings/account/social',
        name: 'Settings Account Social',
        component: () => import('@/views/Settings/Twitter.vue'),
    },
    {
        path: '/settings/server/basic',
        name: 'Settings Server Basic',
        component: () => import('@/views/Settings/Server.vue'),
        props: {section: 'basic'},
    },
    {
        path: '/settings/server/streaming',
        name: 'Settings Server Streaming',
        component: () => import('@/views/Settings/Streaming.vue'),
    },
    {
        path: '/settings/server/storage',
        name: 'Settings Server Storage',
        component: () => import('@/views/Settings/Server.vue'),
        props: {section: 'storage'},
    },
    {
        path: '/settings/server/recorded-series',
        name: 'Settings Server Recorded Series',
        component: () => import('@/views/Settings/RecordedSeries.vue'),
    },
    {
        path: '/settings/server/recorded-series/series',
        name: 'Settings Server Recorded Series Management',
        component: () => import('@/views/Settings/RecordedSeriesManagement.vue'),
    },
    {
        path: '/settings/server/cm-analysis',
        name: 'Settings Server CM Analysis',
        component: () => import('@/views/Settings/CMAnalysis.vue'),
    },
    {
        path: '/settings/server/cm-analysis/logos',
        name: 'Settings Server CM Logo Management',
        component: () => import('@/views/Settings/CMLogoManagement.vue'),
    },
    {
        path: '/settings/server/users',
        name: 'Settings Server Users',
        component: () => import('@/views/Settings/Server.vue'),
        props: {section: 'users'},
    },
    {
        path: '/settings/maintenance',
        name: 'Settings Maintenance Diagnostics Data',
        component: () => import('@/views/Settings/DiagnosticsMaintenance.vue'),
    },
    {
        path: '/settings/maintenance/server',
        name: 'Settings Maintenance Server',
        component: () => import('@/views/Settings/Maintenance.vue'),
        props: {section: 'server'},
    },
];

// ブックマークや外部リンクを壊さないよう、旧 URL は最も近い新しい分類へ転送する。
const LEGACY_SETTINGS_ROUTES: RouteRecordRaw[] = [
    {path: '/settings/general', redirect: '/settings/personal/display'},
    {path: '/settings/quality', redirect: '/settings/personal/quality'},
    {path: '/settings/caption', redirect: '/settings/personal/caption-comments'},
    {path: '/settings/cm-analysis', redirect: '/settings/server/cm-analysis'},
    {path: '/settings/cm-analysis/logos', redirect: '/settings/server/cm-analysis/logos'},
    {path: '/settings/data-broadcasting', redirect: '/settings/personal/data-broadcasting'},
    {path: '/settings/capture', redirect: '/settings/personal/capture'},
    {path: '/settings/account/profile', redirect: '/settings/account'},
    {path: '/settings/account/sync', redirect: '/settings/account'},
    {path: '/settings/account/settings-data', redirect: '/settings/account'},
    {
        path: '/settings/jikkyo',
        redirect: to => ({path: '/settings/account/niconico', query: to.query, hash: to.hash}),
    },
    {path: '/settings/twitter', redirect: '/settings/account/social'},
    {
        path: '/settings/bs4k',
        redirect: to => ({path: '/settings/server/streaming', query: to.query, hash: '#bs4k'}),
    },
    {path: '/settings/server', redirect: '/settings/server/basic'},
    {path: '/settings/personal/quality/bs4k', redirect: '/settings/personal/quality'},
    {path: '/settings/personal/caption', redirect: '/settings/personal/caption-comments'},
    {path: '/settings/personal/comments', redirect: '/settings/personal/caption-comments'},
    {path: '/settings/server/backend', redirect: '/settings/server/basic'},
    {path: '/settings/server/network', redirect: '/settings/server/basic'},
    {
        path: '/settings/server/streaming/bs4k',
        redirect: to => ({path: '/settings/server/streaming', query: to.query, hash: '#bs4k'}),
    },
    {path: '/settings/server/diagnostics', redirect: '/settings/maintenance'},
    {path: '/settings/maintenance/logs', redirect: '/settings/maintenance'},
    {path: '/settings/maintenance/database', redirect: '/settings/maintenance'},
    {path: '/settings/maintenance/analysis', redirect: '/settings/maintenance'},
];

export const SETTINGS_ROUTES: RouteRecordRaw[] = [
    {
        path: '/settings/',
        name: 'Settings Index',
        component: () => import('@/views/Settings/Index.vue'),
        beforeEnter: () => {
            // 狭い画面では設定一覧を挟み、サイドメニューを表示できる広さなら最初の個人設定を直接開く。
            if (Utils.isSmartphoneVertical() || Utils.isSmartphoneHorizontal() || Utils.isTabletVertical()) {
                return true;
            }
            return {path: '/settings/personal/display'};
        },
    },
    ...CANONICAL_SETTINGS_ROUTES,
    ...LEGACY_SETTINGS_ROUTES,
];
