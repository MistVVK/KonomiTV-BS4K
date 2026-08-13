<template>
    <div class="watch-player" :class="{
        'watch-player--loading': playerStore.is_loading,
        'watch-player--virtual-keyboard-display': playerStore.is_virtual_keyboard_display && Utils.hasActiveElementClass('dplayer-comment-input'),
        'watch-player--video': playback_mode === 'Video',
        'watch-player--pure-black': settingsStore.settings.use_pure_black_player_background,
        'watch-player--jikkyo-disabled': settingsStore.is_jikkyo_enabled === false,
    }">
        <div class="watch-player__background-wrapper">
            <div class="watch-player__background" :class="{
                'watch-player__background--display': playerStore.is_background_display,
                'watch-player__background--background-hide': settingsStore.settings.show_player_background_image === false,
            }"
                :style="{backgroundImage: `url(${playerStore.background_url})`}">
                <img class="watch-player__background-logo" src="/assets/images/logo.svg">
            </div>
        </div>
        <v-progress-circular indeterminate size="60" width="6" class="watch-player__buffering"
            :class="{'watch-player__buffering--display': playerStore.is_video_buffering}">
        </v-progress-circular>
        <div v-if="playback_mode === 'Video' && isRecordedAnalysisBlocking"
            class="watch-player__playback-index-status">
            <v-progress-circular v-if="!isRecordedAnalysisFailed"
                indeterminate size="52" width="5" />
            <Icon v-else icon="fluent:error-circle-24-filled" width="52px" />
            <div class="watch-player__playback-index-status-title">{{playbackIndexStatusTitle}}</div>
            <div v-if="!isRecordedAnalysisFailed"
                class="watch-player__playback-index-status-detail">
                {{playbackIndexStageTitle}}
                <template v-if="playerStore.recorded_program.recorded_video.status === 'Recorded' && playerStore.recorded_playback_index_progress !== null">
                    {{Math.round(playerStore.recorded_playback_index_progress * 100)}}%
                </template>
            </div>
            <div v-if="isRecordedAnalysisFailed"
                class="watch-player__playback-index-status-detail">
                {{recordedAnalysisErrorCode}}
            </div>
            <v-btn v-if="isRecordedAnalysisFailed"
                color="primary" variant="flat" class="mt-4" @click="retryRecordedPlaybackIndex">
                再解析する
            </v-btn>
        </div>
        <div class="watch-player__dplayer"></div>
        <div class="watch-player__dplayer-setting-cover"
            :class="{'watch-player__dplayer-setting-cover--display': playerStore.is_player_setting_panel_open}"
            @click="handleSettingCoverClick"></div>
        <div class="watch-player__button"
                @mousemove="playerStore.event_emitter.emit('SetControlDisplayTimer', {event: $event})"
                @touchmove="playerStore.event_emitter.emit('SetControlDisplayTimer', {event: $event})"
                @click="playerStore.event_emitter.emit('SetControlDisplayTimer', {event: $event})">
            <div v-ripple class="switch-button switch-button-up"
                v-ftooltip.top="settingsStore.settings.tv_channel_up_down_buttons_reverse ? '次のチャンネル' : '前のチャンネル'" v-if="playback_mode === 'Live'"
                @click="playerStore.is_zapping = true; $router.push({path: `/tv/watch/${settingsStore.settings.tv_channel_up_down_buttons_reverse ? channelsStore.channel.next.display_channel_id : channelsStore.channel.previous.display_channel_id}`})">
                <Icon class="switch-button-icon" icon="fluent:ios-arrow-left-24-filled" width="32px" style="transform: rotate(90deg)" />
            </div>
            <div v-ripple class="switch-button switch-button-panel"
                :class="{'switch-button-panel--open': playerStore.is_panel_display}"
                @click="playerStore.is_panel_display = !playerStore.is_panel_display">
                <Icon class="switch-button-icon" icon="fluent:navigation-16-filled" width="32px" />
            </div>
            <div v-ripple class="switch-button switch-button-down"
                    v-ftooltip.bottom="settingsStore.settings.tv_channel_up_down_buttons_reverse ? '前のチャンネル' : '次のチャンネル'" v-if="playback_mode === 'Live'"
                    @click="playerStore.is_zapping = true; $router.push({path: `/tv/watch/${settingsStore.settings.tv_channel_up_down_buttons_reverse ? channelsStore.channel.previous.display_channel_id : channelsStore.channel.next.display_channel_id}`})">
                <Icon class="switch-button-icon" icon="fluent:ios-arrow-right-24-filled" width="33px" style="transform: rotate(90deg)" />
            </div>
        </div>
    </div>
</template>
<script setup lang="ts">

import { computed, PropType } from 'vue';

import useChannelsStore from '@/stores/ChannelsStore';
import usePlayerStore from '@/stores/PlayerStore';
import useSettingsStore from '@/stores/SettingsStore';
import Utils from '@/utils';

// Props の定義
defineProps({
    playback_mode: {
        type: String as PropType<'Live' | 'Video'>,
        required: true,
    },
});

// Store の初期化
const channelsStore = useChannelsStore();
const playerStore = usePlayerStore();
const settingsStore = useSettingsStore();

// 軽量Metadataと現行Versionの再生索引だけを再生開始条件にする。
// CM判定とサムネイル生成の状態はここへ含めない。
const isRecordedAnalysisBlocking = computed(() => {
    if (playerStore.is_offline_playback === true) return false;
    const recorded_video = playerStore.recorded_program.recorded_video;
    return recorded_video.status !== 'Recorded' || recorded_video.playback_index_state !== 'Ready';
});

const isRecordedAnalysisFailed = computed(() => {
    const recorded_video = playerStore.recorded_program.recorded_video;
    return recorded_video.status === 'AnalysisFailed' || recorded_video.playback_index_state === 'Failed';
});

// 視聴画面を開いた時点でPending・Staleも優先度0の解析へ投入されるため、プレイヤー領域では解析中と表示する
const playbackIndexStatusTitle = computed(() => {
    const recorded_video = playerStore.recorded_program.recorded_video;
    if (recorded_video.status === 'AnalysisFailed') return '録画メタデータの解析に失敗しました';
    if (recorded_video.status !== 'Recorded') return '録画メタデータを解析中…';
    return recorded_video.playback_index_state === 'Failed' ?
        '録画再生用索引の解析に失敗しました' : '録画再生用索引を解析中…';
});

// サーバーが返す処理段階を、進捗率だけでは分からない作業内容とともに表示する
const playbackIndexStageTitle = computed(() => {
    const recorded_video = playerStore.recorded_program.recorded_video;
    if (recorded_video.status === 'Recording') return '録画完了を待っています';
    if (recorded_video.status === 'Analyzing') return 'ファイル情報・番組情報を解析中';
    const stage_titles = {
        Queued: '解析待ち',
        Probing: 'ストリームを確認中',
        Scanning: '映像・音声フレームを走査中',
        Finalizing: '索引を確定中',
        Complete: '解析完了',
        Failed: '解析失敗',
    } as const;
    const stage = playerStore.recorded_playback_index_stage;
    return stage !== null ? stage_titles[stage] : '解析を開始しています';
});

const recordedAnalysisErrorCode = computed(() => {
    return playerStore.recorded_program.recorded_video.status === 'AnalysisFailed' ?
        'MetadataAnalysisFailed' :
        (playerStore.recorded_program.recorded_video.playback_index_error_code ?? 'UnknownError');
});

// PlayerControllerはまだ存在しないため、Storeのイベントを通じて視聴画面の初期化処理へ再試行を依頼する
const retryRecordedPlaybackIndex = () => {
    playerStore.event_emitter.emit('RetryRecordedPlaybackIndex');
};

// watch-player__dplayer-setting-cover がクリックされたとき、設定パネルを閉じる
const handleSettingCoverClick = () => {
    const dplayer_mask = document.querySelector<HTMLDivElement>('.dplayer-mask');
    if (dplayer_mask) {
        // dplayer-mask をクリックすることで、player.setting.hide() が内部的に呼び出され、設定パネルが閉じられる
        dplayer_mask.click();
    }
};

</script>
<style lang="scss">

.watch-player__playback-index-status {
    position: absolute;
    z-index: 5;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    color: rgb(var(--v-theme-text));
    background: rgb(var(--v-theme-background));

    &-title {
        margin-top: 18px;
        font-size: 18px;
        font-weight: 600;
    }

    &-detail {
        margin-top: 8px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 14px;
    }
}

// DPlayer のデフォルトスタイルを上書き
.watch-player__dplayer {
    @include smartphone-vertical {
        overflow: visible !important;
    }
    svg circle, svg path {
        fill: rgb(var(--v-theme-player-on-overlay)) !important;
    }
    .dplayer-bezel {
        color: rgb(var(--v-theme-player-on-overlay));
        .dplayer-bezel-icon {
            background: rgba(var(--v-theme-player-overlay), 0.82) !important;
        }
    }
    .dplayer-video-wrap {
        background: transparent !important;
        .dplayer-video-wrap-aspect {
            transition: opacity 0.2s cubic-bezier(0.4, 0.38, 0.49, 0.94);
            opacity: 1;
        }
        .dplayer-danmaku {
            max-width: 100%;
            max-height: calc(100% - var(--comment-area-vertical-margin, 0px));
            aspect-ratio: var(--comment-area-aspect-ratio, 16 / 9);
            transition: max-height 0.5s cubic-bezier(0.42, 0.19, 0.53, 0.87), aspect-ratio 0.5s cubic-bezier(0.42, 0.19, 0.53, 0.87);
            will-change: aspect-ratio;
            overflow: hidden;
        }
        .dplayer-bml-browser {
            display: block;
            position: absolute;
            width: var(--bml-browser-width, 960px);
            height: var(--bml-browser-height, 540px);
            color: rgb(0, 0, 0);
            overflow: hidden;
            transform-origin: center;
            transform: scale(var(--bml-browser-scale-factor-width, 1), var(--bml-browser-scale-factor-height, 1));
            transition: opacity 0.2s cubic-bezier(0.4, 0.38, 0.49, 0.94);
            opacity: 1;
            aspect-ratio: 16 / 9;
        }
        .dplayer-danloading {
            display: none !important;
        }
        .dplayer-loading-icon {
            // ローディング表示は自前でやるため不要
            display: none !important;
        }
    }
    .dplayer-controller-mask {
        height: 82px !important;
        background: linear-gradient(to bottom, transparent, rgba(var(--v-theme-player-overlay), 0.92)) !important;
        opacity: 0 !important;
        visibility: hidden;
        transition: opacity 0.3s ease, visibility 0.3s ease !important;
        @include tablet-vertical {
            height: 66px !important;
        }
        @include smartphone-horizontal {
            height: 66px !important;
        }
        @include smartphone-vertical {
            height: 66px !important;
        }
    }
    .dplayer-bar-wrap .dplayer-bar {
        background: rgba(var(--v-theme-player-on-overlay), 0.2) !important;
        .dplayer-loaded {
            background: rgb(var(--v-theme-player-on-overlay)) !important;
        }
    }

    .dplayer-controller {
        padding-left: calc(68px + 18px) !important;
        padding-right: calc(0px + 18px) !important;
        padding-bottom: 6px !important;
        transition: opacity 0.3s ease, visibility 0.3s ease;
        opacity: 0 !important;
        visibility: hidden;
        @include tablet-vertical {
            padding-left: calc(0px + 18px) !important;
            padding-right: calc(0px + 18px) !important;
        }
        @include smartphone-horizontal {
            padding-left: calc(0px + 18px) !important;
            padding-right: calc(0px + 18px) !important;
        }
        @include smartphone-vertical {
            padding-left: calc(0px + 18px) !important;
            padding-right: calc(0px + 18px) !important;
        }

        .dplayer-bar-wrap {
            bottom: 54px !important;
            width: calc(100% - 68px - (18px * 2));
            box-sizing: border-box;
            @include tablet-vertical {
                width: calc(100% - (18px * 2));
            }
            @include smartphone-horizontal {
                width: calc(100% - (18px * 2));
            }
            @include smartphone-vertical {
                width: calc(100% - (18px * 2));
            }
        }
        .dplayer-icons {
            bottom: auto !important;
            &.dplayer-icons-left {
                .dplayer-time, .dplayer-live-badge {
                    color: rgb(var(--v-theme-player-on-overlay)) !important;
                }
                .dplayer-volume {
                    .dplayer-volume-bar {
                        background: rgb(var(--v-theme-player-on-overlay)) !important;
                    }
                    // Document Picture-in-Picture ウインドウでは非表示
                    @media all and (display-mode: picture-in-picture) {
                        display: none;
                    }
                }
            }
            &.dplayer-icons-right {
                right: 22px !important;
                @include tablet-vertical {
                    right: 11px !important;
                }
                @include smartphone-horizontal {
                    right: 11px !important;
                }
                @include smartphone-vertical {
                    right: 11px !important;
                }
            }
            .dplayer-icon {
                @include tablet-vertical {
                    &.dplayer-pip-icon:after {
                        left: 25%;
                    }
                    &.dplayer-full-icon:after {
                        left: -20%;
                    }
                    // Document Picture-in-Picture ウインドウでは Picture-in-Picture ボタンのツールチップを左に寄せる
                    &.dplayer-pip-icon:after {
                        @media all and (display-mode: picture-in-picture) {
                            left: -25%;
                        }
                    }
                }
                @include smartphone-horizontal {
                    &.dplayer-pip-icon:after {
                        left: 25%;
                    }
                    &.dplayer-full-icon:after {
                        left: -20%;
                    }
                    // Document Picture-in-Picture ウインドウでは Picture-in-Picture ボタンのツールチップを左に寄せる
                    &.dplayer-pip-icon:after {
                        @media all and (display-mode: picture-in-picture) {
                            left: -25%;
                        }
                    }
                }
                @include smartphone-vertical {
                    &.dplayer-pip-icon:after {
                        left: 25%;
                    }
                    &.dplayer-full-icon:after {
                        left: -20%;
                    }
                    // Document Picture-in-Picture ウインドウでは Picture-in-Picture ボタンのツールチップを左に寄せる
                    &.dplayer-pip-icon:after {
                        @media all and (display-mode: picture-in-picture) {
                            left: -25%;
                        }
                    }
                }
                &.dplayer-capture-icon, &.dplayer-comment-capture-icon {
                    transition: background-color 0.08s ease;
                    border-radius: 6px;
                    &.dplayer-capturing {
                        background: rgb(var(--v-theme-secondary-lighten-1));
                        .dplayer-icon-content {
                            opacity: 1;
                        }
                    }
                }
                &.dplayer-comment-capture-icon {
                    padding: 7.3px !important;
                    @include smartphone-vertical {
                        padding: 5.4px !important;
                    }
                }
                // ブラウザフルスクリーンボタンを削除（実質あまり意味がないため）
                &.dplayer-full-in-icon {
                    display: none !important;
                }
                // Document Picture-in-Picture ウインドウでは非表示
                &.dplayer-full-icon {
                    @media all and (display-mode: picture-in-picture) {
                        display: none;
                    }
                }
            }
        }
        .dplayer-comment-box {
            transition: opacity 0.3s ease, visibility 0.3s ease !important;
            .dplayer-comment-setting-icon {
                z-index: 5;
            }
            .dplayer-comment-input {
                color: rgb(var(--v-theme-player-on-overlay)) !important;
                background: rgba(var(--v-theme-player-overlay), 0.88) !important;
                &::placeholder {
                    color: rgb(var(--v-theme-player-on-overlay)) !important;
                }
                transition: box-shadow 0.09s ease;
                appearance: none;
                -webkit-appearance: none;
                &:focus {
                    box-shadow: rgb(var(--v-theme-accent)) 0 0 0 3.5px;
                }
                // iOS Safari でフォーカス時にズームされる問題への対処
                @supports (-webkit-touch-callout: none) {
                    @include smartphone-horizontal {
                        width: calc(100% * 1.142857) !important;
                        height: calc(100% * 1.142857) !important;
                        font-size: 16px !important;
                        transform: scale(0.875);
                        transform-origin: 0 0;
                    }
                    @include smartphone-vertical {
                        width: calc(100% * 1.142857) !important;
                        height: calc(100% * 1.142857) !important;
                        font-size: 16px !important;
                        transform: scale(0.875);
                        transform-origin: 0 0;
                    }
                }
            }
        }
    }
    .dplayer-notice {
        padding: 16px 22px !important;
        margin-right: 30px;
        border-radius: 4px !important;
        font-size: 15px !important;
        line-height: 1.6;
        color: rgb(var(--v-theme-player-on-overlay));
        background: rgba(var(--v-theme-player-overlay), 0.92) !important;
        @include tablet-vertical {
            top: auto;
            left: 16px !important;
            padding: 12px 16px !important;
            margin-right: 16px;
            font-size: 13.5px !important;
        }
        @include smartphone-horizontal {
            padding: 12px 16px !important;
            margin-right: 16px;
            font-size: 13.5px !important;
        }
        @include smartphone-vertical {
            top: auto;
            left: 16px !important;
            padding: 12px 16px !important;
            margin-right: 16px;
            font-size: 13.5px !important;
        }
    }
    .dplayer-info-panel {
        transition: top 0.3s, left 0.3s;
        color: rgb(var(--v-theme-player-on-overlay)) !important;
        background: rgba(var(--v-theme-player-overlay), 0.92) !important;
    }
    .dplayer-setting-box {
        z-index: 10 !important;
        color: rgb(var(--v-theme-player-on-overlay)) !important;
        background: rgba(var(--v-theme-player-overlay), 0.96) !important;
        // 長い音声トラック名（言語・Dual Mono・主副音声）が途中で切れないようにする
        width: 280px;
        max-width: calc(100% - 40px);
        .dplayer-label,
        .dplayer-label-value {
            color: rgb(var(--v-theme-player-on-overlay)) !important;
        }
        .dplayer-setting-item:hover,
        .dplayer-setting-quality-item:hover,
        .dplayer-setting-speed-item:hover,
        .dplayer-setting-audio-item:hover,
        .dplayer-konomitv-bs4k-setting-video-codec-item:hover,
        .dplayer-konomitv-bs4k-setting-audio-codec-item:hover,
        .dplayer-setting-header:hover {
            background: rgba(var(--v-theme-primary), 0.12) !important;
        }
        .dplayer-setting-origin-panel,
        .dplayer-setting-quality-panel,
        .dplayer-setting-speed-panel,
        .dplayer-setting-audio-panel,
        .dplayer-konomitv-bs4k-setting-video-codec-panel,
        .dplayer-konomitv-bs4k-setting-audio-codec-panel {
            scrollbar-color: rgba(var(--v-theme-player-on-overlay), 0.24) transparent;
            &::-webkit-scrollbar-thumb {
                background: rgba(var(--v-theme-player-on-overlay), 0.24) !important;
            }
        }
        .dplayer-setting-header {
            border-bottom-color: rgba(var(--v-theme-player-on-overlay), 0.15) !important;
        }
        .dplayer-danmaku-bar {
            background: rgb(var(--v-theme-player-on-overlay)) !important;
        }
        @include tablet-vertical {
            height: calc(100% - 60px) !important;
        }
        &.dplayer-setting-box-audio {
            // DPlayer の固定値 (2トラック分) ではなく、実際に表示する音声トラック数に合わせる
            clip-path: inset(calc(100% - var(--audio-panel-height, 114px)) 0 0 round 7px) !important;
        }
        &.dplayer-konomitv-bs4k-setting-box-video-codec,
        &.dplayer-konomitv-bs4k-setting-box-audio-codec {
            // DPlayer が保持する元パネルの inline clip-path を壊さず、独自サブパネルの表示中だけ元パネルを退避する
            .dplayer-setting-origin-panel {
                transform: translateX(-100%);
            }
        }
        &.dplayer-konomitv-bs4k-setting-box-video-codec {
            clip-path: inset(calc(100% - var(--konomitv-bs4k-video-codec-panel-height, 114px)) 0 0 round 7px) !important;

            .dplayer-konomitv-bs4k-setting-video-codec-panel {
                transform: translateX(0%) !important;
            }
        }
        &.dplayer-konomitv-bs4k-setting-box-audio-codec {
            clip-path: inset(calc(100% - var(--konomitv-bs4k-audio-codec-panel-height, 114px)) 0 0 round 7px) !important;

            .dplayer-konomitv-bs4k-setting-audio-codec-panel {
                transform: translateX(0%) !important;
            }
        }
        .dplayer-konomitv-bs4k-setting-video-codec-item--disabled,
        .dplayer-konomitv-bs4k-setting-audio-codec-item--disabled {
            cursor: default !important;
            opacity: 0.45;
            pointer-events: none;
        }
        .dplayer-konomitv-bs4k-setting-low-latency-mode {
            // 実効状態を示すだけの行なので、他の設定項目のように操作できる印象を与えない
            cursor: default !important;
            &:hover {
                background: transparent !important;
            }
        }
        .dplayer-setting-origin-panel {
            .dplayer-setting-item.dplayer-setting-lshaped-screen-crop,
            .dplayer-setting-item.dplayer-setting-keyboard-shortcut {
                // Document Picture-in-Picture ウインドウでは非表示
                @media all and (display-mode: picture-in-picture) {
                    display: none;
                }
            }
        }
        .dplayer-setting-audio-panel {
            // ライブで配信 TS に存在しない DPlayer の固定音声項目は表示しない。
            // 録画だけは現在位置に存在しない音声も全体の候補として残し、選択不能であることを示す。
            .dplayer-setting-audio-item.dplayer-setting-audio-item--disabled:not(.dplayer-setting-audio-item--status) {
                display: none;

                .watch-player--video & {
                    display: flex;
                    cursor: default;
                    opacity: 0.45;
                    pointer-events: none;
                }
            }
            // 「音声不明」「音声なし」は選択できない状態表示として扱う
            .dplayer-setting-audio-item.dplayer-setting-audio-item--status {
                cursor: default;
                pointer-events: none;

                .dplayer-toggle {
                    visibility: hidden !important;
                }
            }
        }
    }
    // 録画の音声設定は 0/1 トラックでも状態確認できるよう常時表示する
    &.dplayer-no-audio-switching .dplayer-setting-box .dplayer-setting-audio {
        display: flex !important;
    }
    &.dplayer-audio-only {
        .dplayer-video-wrap-aspect::after {
            content: '音声のみ';
            position: absolute;
            inset: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            color: rgba(255, 255, 255, 0.72);
            font-size: 18px;
            pointer-events: none;
        }
        .dplayer-setting-quality,
        .dplayer-setting-lshaped-screen-crop,
        .dplayer-camera-icon,
        .dplayer-subtitle-btn {
            display: none !important;
        }
    }
    .dplayer-comment-setting-box {
        z-index: 10 !important;
        color: rgb(var(--v-theme-player-on-overlay)) !important;
        background: rgba(var(--v-theme-player-overlay), 0.96) !important;
        scrollbar-color: rgba(var(--v-theme-player-on-overlay), 0.24) transparent;
        &::-webkit-scrollbar-thumb {
            background: rgba(var(--v-theme-player-on-overlay), 0.24) !important;
        }
        input {
            color: rgb(var(--v-theme-player-on-overlay)) !important;
        }
        .dplayer-comment-setting-title {
            color: rgb(var(--v-theme-player-on-overlay));
        }
        .dplayer-comment-setting-type, .dplayer-comment-setting-size {
            span {
                color: rgb(var(--v-theme-player-on-overlay));
                border: 1px solid rgb(var(--v-theme-player-on-overlay));
            }
            input:checked + span {
                background: rgb(var(--v-theme-player-on-overlay));
                color: rgb(var(--v-theme-player-overlay));
            }
        }
    }

    // モバイルのみ適用されるスタイル
    &.dplayer-mobile {
        .dplayer-controller {
            padding-left: calc(68px + 30px) !important;
            padding-right: calc(0px + 30px) !important;
            .dplayer-bar-wrap {
                bottom: 51px !important;
                width: calc(100% - 68px - (30px * 2));
                @include tablet-vertical {
                    width: calc(100% - (18px * 2));
                }
                @include smartphone-horizontal {
                    width: calc(100% - (18px * 2));
                }
                @include smartphone-vertical {
                    // スマホ縦画面のみ、シークバーをプレイヤーの下辺に配置
                    width: 100%;
                    left: 0px !important;
                    bottom: -6px !important;
                    z-index: 100;
                }
                .dplayer-thumb {
                    // タッチデバイスのみ、コントロール表示時は常にシークバーのつまみを表示する
                    @media (hover: none) {
                        transform: scale(1) !important;
                    }
                }
            }
            @include tablet-vertical {
                padding-left: calc(0px + 18px) !important;
                padding-right: calc(0px + 18px) !important;
            }
            @include smartphone-horizontal {
                padding-left: calc(0px + 18px) !important;
                padding-right: calc(0px + 18px) !important;
            }
            @include smartphone-vertical {
                padding-left: calc(0px + 18px) !important;
                padding-right: calc(0px + 18px) !important;
            }
        }
        &.dplayer-hide-controller .dplayer-controller {
            transform: none !important;
        }
    }

    // 狭小幅デバイスのみ適用されるスタイル
    &.dplayer-narrow {
        .dplayer-icons.dplayer-icons-right {
            right: 14px !important;
        }
    }
}

// 実況機能が無効な場合も DPlayer の danmaku インスタンスは内部互換性のため維持し、操作 UI だけを完全に隠す
.watch-player--jikkyo-disabled .watch-player__dplayer {
    .dplayer-icons.dplayer-comment-box,
    .dplayer-controller .dplayer-icons .dplayer-comment,
    .dplayer-comment-setting-box,
    .dplayer-setting-item.dplayer-setting-showdan,
    .dplayer-setting-item.dplayer-setting-danunlimit,
    .dplayer-setting-item.dplayer-setting-danmaku,
    .dplayer-comment-capture-icon {
        display: none !important;
    }
}

// ロード中は DPlayer 内の動画と BML ブラウザを非表示にする
.watch-player.watch-player--loading {
    .watch-player__dplayer {
        .dplayer-video-wrap-aspect, .dplayer-bml-browser {
            opacity: 0 !important;
        }
    }
}

// 仮想キーボード表示時
.watch-player.watch-player--virtual-keyboard-display {
    .watch-player__dplayer {
        .dplayer-controller-mask {
            position: absolute;
            bottom: env(keyboard-inset-height, 0px) !important;
            @include tablet-vertical {
                bottom: 0px !important;
            }
            @include smartphone-vertical {
                bottom: 0px !important;
            }
        }
        .dplayer-icons.dplayer-comment-box {
            position: absolute;
            bottom: calc(env(keyboard-inset-height, 0px) + 4px) !important;
            @include tablet-vertical {
                bottom: 6px !important;
            }
            @include smartphone-vertical {
                bottom: 6px !important;
            }
        }
    }
}

// ビデオ視聴時のみ適用されるスタイル
.watch-player.watch-player--video {
    .watch-player__dplayer {
        // コメント送信用ボタンを削除
        .dplayer-controller .dplayer-icons .dplayer-comment {
            display: none !important;
        }
    }
}

// Safari のみ、アイコンに hover しても opacity が変わらないようにする
// hover すると 1px ずれてしまい見苦しくなる Safari の描画バグを回避するための苦肉の策
// ref: https://qiita.com/Butterthon/items/10e6b58d883236aa3838
_::-webkit-full-page-media, _:future, :root .dplayer-icon:hover .dplayer-icon-content {
    opacity: 0.8 !important;
}

// Safari では上の hover 補正が DPlayer の字幕オフ表示より強く効き、オフ状態でも字幕アイコンが明るく見えてしまう
// DPlayer は字幕オフ時にボタンの aria-label も切り替えるため、style 属性の文字列表現には依存しない
_::-webkit-full-page-media, _:future, :root .dplayer-subtitle-icon[aria-label='字幕を表示する']:hover .dplayer-icon-content {
    opacity: 0.4 !important;
}

</style>
<style lang="scss" scoped>

.watch-player {
    display: flex;
    position: relative;
    width: 100%;
    height: 100%;
    background-size: contain;
    background-position: center;
    &.watch-player--pure-black {
        background-color: #000000;
    }
    @include tablet-vertical {
        aspect-ratio: 16 / 9;
    }
    @include smartphone-vertical {
        aspect-ratio: 16 / 9;
    }

    &.watch-player--video {
        .watch-player__button {
            right: 0px;
            .switch-button {
                border-top-right-radius: 0px;
                border-bottom-right-radius: 0px;
            }
        }
    }

    .watch-player__dplayer-setting-cover {
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        background-color: rgba(0, 0, 0, 0.5);
        opacity: 0;
        visibility: hidden;
        transition: opacity 0.3s, visibility 0.3s;
        z-index: 50;

        &--display {
            // タッチデバイスかつスマホ縦画面のみ、設定パネルを開いた時にカバーを表示する
            @media (hover: none) {
                @include smartphone-vertical {
                    opacity: 1;
                    visibility: visible;
                }
            }
        }
    }

    .watch-player__background-wrapper {
        position: absolute;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;

        .watch-player__background {
            position: relative;
            top: 50%;
            left: 50%;
            max-height: 100%;
            aspect-ratio: 16 / 9;
            transform: translate(-50%, -50%);
            background-blend-mode: overlay;
            background-color: rgba(14, 14, 18, 0.35);
            background-size: cover;
            background-image: none;
            opacity: 0;
            visibility: hidden;
            transition: opacity 0.4s cubic-bezier(0.4, 0.38, 0.49, 0.94), visibility 0.4s cubic-bezier(0.4, 0.38, 0.49, 0.94);
            will-change: opacity;

            &--display {
                opacity: 1;
                visibility: visible;
            }
            &--background-hide {
                background-image: none !important;
                background-color: #101010;
            }

            .watch-player__background-logo {
                display: inline-block;
                position: absolute;
                height: 34px;
                right: 56px;
                bottom: 44px;
                // プレイヤー背景は選択テーマにかかわらず暗いため、SVG の白文字を常に使用する
                color-scheme: dark;
                filter: drop-shadow(0px 0px 5px rgb(var(--v-theme-black)));

                @include tablet-vertical {
                    height: 30px;
                    right: 34px;
                    bottom: 30px;
                }
                @include smartphone-horizontal {
                    height: 25px;
                    right: 30px;
                    bottom: 24px;
                }
                @include smartphone-vertical {
                    height: 22px;
                    right: 30px;
                    bottom: 24px;
                }
            }
        }
    }

    .watch-player__buffering {
        position: absolute;
        top: 50%;
        left: 50%;
        transform: translate(-50%, -50%);
        color: rgb(var(--v-theme-background-lighten-3));
        filter: drop-shadow(0px 0px 3px rgba(0, 0, 0, 0.3));
        opacity: 0;
        visibility: hidden;
        transition: opacity 0.2s cubic-bezier(0.4, 0.38, 0.49, 0.94), visibility 0.2s cubic-bezier(0.4, 0.38, 0.49, 0.94);
        will-change: opacity;
        z-index: 3;
        pointer-events: none;  // クリックイベントを無効化 (重要)

        &--display {
            opacity: 1;
            visibility: visible;
        }
    }

    .watch-player__dplayer {
        width: 100%;
    }

    .watch-player__button {
        display: flex;
        justify-content: space-around;
        flex-direction: column;
        position: absolute;
        top: 50%;
        right: 28px;
        height: 190px;
        transform: translateY(-50%);
        opacity: 0;
        visibility: hidden;
        transition: opacity 0.3s, visibility 0.3s;
        @include tablet-vertical {
            right: 15px;
            height: 128px;
        }
        @include smartphone-horizontal {
            right: 15px;
            height: 155px;
        }
        @include smartphone-vertical {
            right: 15px;
            height: 100px;
        }

        // Document Picture-in-Picture ウインドウでは非表示
        @media all and (display-mode: picture-in-picture) {
            display: none;
        }

        .switch-button {
            display: flex;
            justify-content: center;
            align-items: center;
            width: 48px;
            height: 48px;
            color: rgb(var(--v-theme-player-on-overlay));
            background: rgba(var(--v-theme-player-overlay), 0.82);
            border-radius: 7px;
            transition: background-color 0.15s;
            user-select: none;
            cursor: pointer;
            @include smartphone-horizontal {
                width: 38px;
                height: 38px;
                border-radius: 5px;
            }
            @include smartphone-vertical {
                width: 38px;
                height: 38px;
                border-radius: 5px;
            }

            &:hover {
                background: rgba(var(--v-theme-player-overlay), 0.95);
            }
            // タッチデバイスで hover を無効にする
            @media (hover: none) {
                &:hover {
                    background: rgba(var(--v-theme-player-overlay), 0.82);
                }
            }

            svg {
                @include smartphone-horizontal {
                    height: 27px;
                }
                @include smartphone-vertical {
                    height: 27px;
                }
            }

            .switch-button-icon {
                position: relative;
            }

            &-up > .switch-button-icon {
                top: 6px;
            }
            &-panel {
                &.switch-button-panel--open {
                    color: rgb(var(--v-theme-primary));
                }
                @include tablet-vertical {
                    display: none;
                }
                @include smartphone-vertical {
                    display: none;
                }
            }
            &-panel > .switch-button-icon {
                top: 1.5px;
                transition: color 0.4s cubic-bezier(0.26, 0.68, 0.55, 0.99);
            }
            &-down > .switch-button-icon {
                bottom: 4px;
            }
        }
    }
}

</style>
