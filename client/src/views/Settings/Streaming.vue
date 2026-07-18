<template>
    <SettingsBase>
        <h2 class="settings__heading">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:video-settings-20-filled" width="22px" />
            <span class="ml-2">配信・エンコーダー</span>
        </h2>
        <div class="settings__description">
            通常放送と BS4K の配信・エンコーダー設定を管理します。<br>
            未保存の変更はタブを切り替えると破棄されるため、各タブで設定を更新してから切り替えてください。<br>
        </div>
        <v-tabs class="settings__tab" color="primary" bg-color="transparent" align-tabs="center" v-model="active_tab">
            <v-tab value="normal">通常放送</v-tab>
            <v-tab value="bs4k">BS4K</v-tab>
        </v-tabs>
        <ServerSettings v-if="active_tab === 'normal'" section="streaming" embedded />
        <BS4KSettings v-else section="server" embedded />
    </SettingsBase>
</template>

<script lang="ts" setup>

import { ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import SettingsBase from '@/views/Settings/Base.vue';
import BS4KSettings from '@/views/Settings/BS4K.vue';
import ServerSettings from '@/views/Settings/Server.vue';

type StreamingSettingsTab = 'normal' | 'bs4k';

const route = useRoute();
const router = useRouter();
const active_tab = ref<StreamingSettingsTab>(route.hash === '#bs4k' ? 'bs4k' : 'normal');

watch(active_tab, (tab) => {
    const hash = tab === 'bs4k' ? '#bs4k' : '';
    if (route.hash !== hash) {
        void router.replace({hash});
    }
});

watch(() => route.hash, (hash) => {
    active_tab.value = hash === '#bs4k' ? 'bs4k' : 'normal';
});

</script>
