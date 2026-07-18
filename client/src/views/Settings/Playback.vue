<template>
    <SettingsBase>
        <h2 class="settings__heading">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:video-clip-multiple-16-filled" width="26px" />
            <span class="ml-3">再生・画質</span>
        </h2>
        <div class="settings__description">
            通常放送と BS4K のライブ視聴・録画再生に使う画質や映像コーデックを、この端末向けに設定します。<br>
        </div>
        <Quality embedded />
        <BS4K section="quality" embedded />
    </SettingsBase>
</template>
<script lang="ts" setup>

import SettingsBase from '@/views/Settings/Base.vue';
import BS4K from '@/views/Settings/BS4K.vue';
import Quality from '@/views/Settings/Quality.vue';

</script>
