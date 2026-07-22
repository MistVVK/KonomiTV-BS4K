<template>
    <SettingsBase>
        <h2 class="settings__heading">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:paint-brush-24-filled" width="24px" />
            <span class="ml-3">カラーテーマ</span>
        </h2>
        <div class="settings__content">
            <div class="settings__item">
                <div class="settings__item-heading">カラーテーマ</div>
                <div class="settings__item-label">
                    KonomiTV-BS4K の画面全体に適用するカラーテーマを選択できます。選択したテーマはすぐに反映されます。<br>
                    番組表の番組セル・ジャンル色・時刻帯色や、コメントなどの機能色は変更されません。<br>
                </div>
                <div class="theme-selector" role="group" aria-label="カラーテーマ">
                    <button v-for="theme in themeOptions" :key="theme.value" v-ripple type="button"
                        class="theme-selector__item"
                        :class="{'theme-selector__item--selected': settingsStore.settings.ui_theme === theme.value}"
                        :aria-pressed="settingsStore.settings.ui_theme === theme.value"
                        @click="settingsStore.settings.ui_theme = theme.value">
                        <span class="theme-selector__preview" :style="{background: theme.preview.background}">
                            <span class="theme-selector__preview-header" :style="{background: theme.preview.surface}">
                                <span class="theme-selector__preview-dot" :style="{background: theme.preview.primary}"></span>
                                <span class="theme-selector__preview-dot theme-selector__preview-dot--accent"
                                    :style="{background: theme.preview.accent}"></span>
                                <span class="theme-selector__preview-line" :style="{background: theme.preview.textMuted}"></span>
                            </span>
                            <span class="theme-selector__preview-content">
                                <span class="theme-selector__preview-panel" :style="{background: theme.preview.surface}">
                                    <span class="theme-selector__preview-heading" :style="{background: theme.preview.text}"></span>
                                    <span class="theme-selector__preview-text" :style="{background: theme.preview.textMuted}"></span>
                                    <span class="theme-selector__preview-button" :style="{background: theme.preview.primary}"></span>
                                </span>
                                <span class="theme-selector__preview-panel theme-selector__preview-panel--sub"
                                    :style="{background: theme.preview.elevated}"></span>
                            </span>
                        </span>
                        <span class="theme-selector__name">
                            <span>{{theme.title}}</span>
                            <span class="theme-selector__type">{{theme.dark ? 'ダーク' : 'ライト'}}</span>
                        </span>
                        <Icon v-if="settingsStore.settings.ui_theme === theme.value"
                            class="theme-selector__check" icon="fluent:checkmark-circle-20-filled" width="22px" />
                    </button>
                </div>
            </div>
        </div>
    </SettingsBase>
</template>
<script lang="ts" setup>

import useSettingsStore from '@/stores/SettingsStore';
import { KONOMITV_THEME_OPTIONS } from '@/themes';
import SettingsBase from '@/views/Settings/Base.vue';


const settingsStore = useSettingsStore();
const themeOptions = Object.freeze(KONOMITV_THEME_OPTIONS);

</script>
<style lang="scss" scoped>

.theme-selector {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 12px;
    margin-top: 18px;
    @include tablet-vertical {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    @include smartphone-vertical {
        grid-template-columns: 1fr;
    }

    &__item {
        position: relative;
        min-width: 0;
        padding: 7px;
        border: 2px solid rgb(var(--v-theme-background-lighten-2));
        border-radius: 11px;
        color: rgb(var(--v-theme-text));
        background: rgb(var(--v-theme-background));
        font: inherit;
        text-align: left;
        cursor: pointer;
        transition: border-color 0.15s, background-color 0.15s, transform 0.15s;

        &:hover {
            background: rgb(var(--v-theme-background-lighten-2));
            transform: translateY(-1px);
        }
        &:focus-visible {
            outline: 3px solid rgb(var(--v-theme-accent));
            outline-offset: 2px;
        }
        &--selected {
            border-color: rgb(var(--v-theme-primary));
        }
    }

    &__preview {
        display: block;
        height: 92px;
        overflow: hidden;
        border-radius: 7px;
        box-shadow: inset 0 0 0 1px rgb(127 127 127 / 18%);
    }
    &__preview-header {
        display: flex;
        align-items: center;
        gap: 5px;
        height: 20px;
        padding: 0 7px;
    }
    &__preview-dot {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        &--accent {
            width: 5px;
            height: 5px;
        }
    }
    &__preview-line {
        width: 35%;
        height: 3px;
        border-radius: 2px;
        opacity: 0.85;
    }
    &__preview-content {
        display: grid;
        grid-template-columns: 1fr 28%;
        gap: 6px;
        height: 72px;
        padding: 8px;
    }
    &__preview-panel {
        display: flex;
        flex-direction: column;
        align-items: flex-start;
        padding: 8px;
        border-radius: 5px;
        &--sub {
            padding: 0;
        }
    }
    &__preview-heading, &__preview-text, &__preview-button {
        display: block;
        border-radius: 2px;
    }
    &__preview-heading {
        width: 55%;
        height: 4px;
        opacity: 0.8;
    }
    &__preview-text {
        width: 78%;
        height: 3px;
        margin-top: 7px;
        opacity: 0.9;
    }
    &__preview-button {
        width: 34%;
        height: 10px;
        margin-top: auto;
    }
    &__name {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        padding: 8px 3px 1px;
        font-size: 13px;
        font-weight: 600;
    }
    &__type {
        color: rgb(var(--v-theme-text-darken-2));
        font-size: 11px;
        font-weight: 500;
    }
    &__check {
        position: absolute;
        top: 12px;
        right: 12px;
        padding: 2px;
        border-radius: 50%;
        color: rgb(var(--v-theme-primary));
        background: rgb(var(--v-theme-background-lighten-1));
    }
}

</style>
