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
            通常と BS4K の配信・エンコーダー設定、および両方に適用する共通設定を管理します。<br>
            未保存の変更はタブを切り替えても保持されます。ページ下部のボタンでまとめて更新してください。<br>
        </div>
        <v-tabs class="settings__tab" color="primary" bg-color="transparent" align-tabs="center" v-model="active_tab">
            <v-tab value="normal">通常</v-tab>
            <v-tab value="bs4k">BS4K</v-tab>
        </v-tabs>
        <ServerSettings v-if="active_tab === 'normal'" v-model:server-settings="server_settings"
            section="streaming" :show-save-button="false" embedded />
        <BS4KSettings v-else v-model:server-settings="server_settings"
            section="server" :show-save-button="false" embedded />
        <ServerSettings v-model:server-settings="server_settings"
            section="streaming-common" :show-save-button="false" embedded />
        <div class="settings__content" :class="{'settings__content--disabled': is_disabled}">
            <div class="settings__content-heading">
                <Icon icon="fluent:arrow-counterclockwise-20-filled" width="22px" />
                <span class="ml-2">反映</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">配信・エンコーダー設定を更新</div>
                <div class="settings__item-label">
                    通常・BS4K・共通の設定をまとめて config.yaml に保存します。<br>
                    保存した変更を反映するには KonomiTV-BS4K サーバーの再起動が必要です。<br>
                </div>
            </div>
            <v-btn class="settings__save-button bg-secondary mt-5" variant="flat"
                :disabled="is_disabled" @click="updateServerSettings()">
                <Icon icon="fluent:save-16-filled" class="mr-2" height="23px" />配信・エンコーダー設定を更新
            </v-btn>
        </div>
    </SettingsBase>
</template>

<script lang="ts" setup>

import { storeToRefs } from 'pinia';
import { ref, toRaw, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import type { IServerSettings } from '@/services/Settings';

import Message from '@/message';
import useServerSettingsStore from '@/stores/ServerSettingsStore';
import useUserStore from '@/stores/UserStore';
import SettingsBase from '@/views/Settings/Base.vue';
import BS4KSettings from '@/views/Settings/BS4K.vue';
import ServerSettings from '@/views/Settings/Server.vue';

type StreamingSettingsTab = 'normal' | 'bs4k';

const route = useRoute();
const router = useRouter();
const active_tab = ref<StreamingSettingsTab>(route.hash === '#bs4k' ? 'bs4k' : 'normal');

// 通常・BS4K・共通設定を同じドラフトで編集し、別 section の保存で値を巻き戻さない
const server_settings_store = useServerSettingsStore();
const { server_settings: base_server_settings } = storeToRefs(server_settings_store);
const server_settings = ref<IServerSettings>(structuredClone(toRaw(base_server_settings.value)));

function resetServerSettingsDraft(): void {
    server_settings.value = structuredClone(toRaw(base_server_settings.value));
}

server_settings_store.fetchServerSettingsOnce().then((settings) => {
    if (settings !== null) {
        resetServerSettingsDraft();
    }
});

const is_disabled = ref(true);
const user_store = useUserStore();
user_store.fetchUser().then((user) => {
    if (user && user.is_admin) {
        is_disabled.value = false;
    }
});

async function updateServerSettings(): Promise<void> {
    const result = await server_settings_store.updateServerSettings(server_settings.value);
    if (result === true) {
        resetServerSettingsDraft();
        Message.success('配信・エンコーダー設定を更新しました。\n変更を反映するためには、KonomiTV-BS4K サーバーを再起動してください。');
    }
}

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
