<template>
    <SettingsBase>
        <h2 class="settings__heading">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:wrench-settings-20-filled" width="22px" />
            <span class="ml-2">診断・データ保守</span>
        </h2>
        <div class="settings__description">
            サーバーの診断設定、ログ確認、データベース更新、録画スキャン、各種再解析をまとめて管理します。<br>
            設定の変更や各種操作を行うには、管理者アカウントでログインしてください。<br>
        </div>
        <ServerSettings section="diagnostics" embedded />
        <MaintenanceSettings section="maintenance" embedded />
    </SettingsBase>
</template>

<script lang="ts" setup>

import SettingsBase from '@/views/Settings/Base.vue';
import MaintenanceSettings from '@/views/Settings/Maintenance.vue';
import ServerSettings from '@/views/Settings/Server.vue';

</script>
