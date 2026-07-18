<template>
    <SettingsBase>
        <h2 class="settings__heading">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:subtitles-16-filled" width="25px" />
            <span class="ml-3">字幕・コメント</span>
        </h2>
        <SettingsCaption embedded />
        <SettingsJikkyo embedded section="comments" />
    </SettingsBase>
</template>
<script lang="ts">

import { defineComponent } from 'vue';

import SettingsBase from '@/views/Settings/Base.vue';
import SettingsCaption from '@/views/Settings/Caption.vue';
import SettingsJikkyo from '@/views/Settings/Jikkyo.vue';

export default defineComponent({
    name: 'Settings-CaptionComments',
    components: {
        SettingsBase,
        SettingsCaption,
        SettingsJikkyo,
    },
});

</script>
