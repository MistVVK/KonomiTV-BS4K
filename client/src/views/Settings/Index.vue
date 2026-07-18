<template>
    <div class="route-container">
        <HeaderBar />
        <main>
            <Navigation />
            <SPHeaderBar :hide-on-smartphone-vertical="true" />
            <v-card class="settings-container d-flex px-5 py-5 mx-auto" elevation="0" width="100%" max-width="1000">
                <nav class="settings-navigation">
                    <h1 class="mt-2 d-flex align-center" style="font-size: 24px;">
                        <a v-ripple class="settings-navigation__back-button" @click="$router.back()">
                            <Icon icon="fluent:chevron-left-12-filled" width="27px" />
                        </a>
                        <span>設定</span>
                    </h1>
                    <section v-for="category in settingsNavigationCategories" :key="category.label"
                        class="settings-navigation__category">
                        <h2 class="settings-navigation__category-heading">{{category.label}}</h2>
                        <v-btn v-for="item in category.items"
                            :key="item.type === 'Route' ? item.to : item.href"
                            variant="flat" class="settings-navigation__button"
                            :to="item.type === 'Route' ? item.to : undefined"
                            :exact="item.type === 'Route'"
                            :href="item.type === 'ExternalLink' ? item.href : undefined"
                            :target="item.type === 'ExternalLink' ? '_blank' : undefined"
                            :rel="item.type === 'ExternalLink' ? 'noopener noreferrer' : undefined"
                            :aria-label="item.type === 'ExternalLink' ? item.ariaLabel : undefined">
                            <svg v-if="item.icon === settingsDataBroadcastingIcon"
                                width="26px" height="26px" viewBox="0 0 512 512">
                                <path fill="currentColor" d="M248.039 381.326L355.039 67.8258C367.539 28.3257 395.039 34.3258 406.539 34.3258C431.039 34.3258 453.376 61.3258 441.039 96.8258C362.639 322.426 343.539 375.326 340.539 384.826C338.486 391.326 342.039 391.326 345.539 391.326C377.039 391.326 386.539 418.326 386.539 435.326C386.539 458.826 371.539 477.326 350.039 477.326H214.539C179.039 477.326 85.8269 431.3 88.0387 335.826C91.0387 206.326 192.039 183.326 243.539 183.326H296.539L265.539 272.326H243.539C185.539 272.326 174.113 314.826 176.039 334.326C180.039 374.826 215.039 389.814 237.039 390.326C244.539 390.5 246.039 386.826 248.039 381.326Z" />
                            </svg>
                            <Icon v-else :icon="item.icon" :width="item.iconWidth ?? '26px'" :style="item.iconStyle" />
                            <span class="ml-4">{{item.label}}</span>
                        </v-btn>
                    </section>
                </nav>
            </v-card>
        </main>
    </div>
</template>
<script lang="ts" setup>

import HeaderBar from '@/components/HeaderBar.vue';
import Navigation from '@/components/Navigation.vue';
import SPHeaderBar from '@/components/SPHeaderBar.vue';
import { SETTINGS_DATA_BROADCASTING_ICON, SETTINGS_NAVIGATION_CATEGORIES } from '@/router/settings';

const settingsDataBroadcastingIcon = SETTINGS_DATA_BROADCASTING_ICON;
const settingsNavigationCategories = SETTINGS_NAVIGATION_CATEGORIES;

</script>
<style lang="scss" scoped>

.settings-container {
    background: rgb(var(--v-theme-background)) !important;
    width: 100%;
    min-width: 0;
    @include smartphone-horizontal {
        padding: 16px 20px !important;
    }
    @include smartphone-horizontal-short {
        padding: 16px 16px !important;
    }
    @include smartphone-vertical {
        padding: 16px 16px !important;
    }

    .settings-navigation {
        display: flex;
        flex-direction: column;
        flex-shrink: 0;
        width: 100%;
        transform: none !important;
        visibility: visible !important;

        .settings-navigation__category {
            display: flex;
            flex-direction: column;

            &:first-of-type {
                margin-top: 20px;
            }

            & + .settings-navigation__category {
                margin-top: 18px;
            }
        }

        .settings-navigation__category-heading {
            padding: 0 10px 8px;
            color: rgb(var(--v-theme-text-darken-1));
            font-size: 13px;
            font-weight: bold;
            letter-spacing: 0.04em;
        }

        .settings-navigation__button {
            justify-content: left !important;
            width: 100%;
            height: 54px;
            margin-bottom: 6px;
            border-radius: 6px;
            font-size: 16px;
            color: rgb(var(--v-theme-text)) !important;
            background: rgb(var(--v-theme-background-lighten-1)) !important;

            &--version {
                display: none;
                @include smartphone-vertical {
                    display: flex;
                }
                &-highlight {
                    color: rgb(var(--v-theme-secondary-lighten-1)) !important;
                }
            }
        }

        .settings-navigation__back-button {
            display: none;
            position: relative;
            left: -8px;
            padding: 6px;
            border-radius: 50%;
            color: rgb(var(--v-theme-text));
            cursor: pointer;
            @include smartphone-vertical {
                display: flex;
            }

            + span {
                @include smartphone-vertical {
                    margin-left: -4px;
                }
            }
        }

        h1 {
            @include smartphone-horizontal {
                font-size: 22px !important;
            }
        }
    }
}

</style>
