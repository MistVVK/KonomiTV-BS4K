<template>
    <span v-if="activeJobCount > 0" class="offline-download-badge">{{activeJobCount}}</span>
</template>
<script lang="ts" setup>

import { useOfflineDownloadJobCount } from '@/utils/useOfflineDownloadJobCount';

const { activeJobCount } = useOfflineDownloadJobCount();

</script>
<style lang="scss" scoped>

.offline-download-badge {
    display: flex;
    align-items: center;
    justify-content: center;
    position: absolute;
    top: -2px;
    right: -6px;
    min-width: 17px;
    height: 17px;
    padding: 0 4px;
    border-radius: 9px;
    color: white;
    background: rgb(var(--v-theme-primary));
    font-size: 10px;
    font-weight: bold;
    pointer-events: none;
}

</style>
