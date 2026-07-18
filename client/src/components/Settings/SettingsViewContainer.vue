<template>
    <SettingsBase v-if="embedded === false">
        <slot></slot>
    </SettingsBase>
    <slot v-else></slot>
</template>
<script lang="ts">

import { defineComponent } from 'vue';

import SettingsBase from '@/views/Settings/Base.vue';

export default defineComponent({
    name: 'Settings-ViewContainer',
    components: {
        SettingsBase,
    },
    props: {
        embedded: {
            type: Boolean,
            default: false,
        },
    },
});

</script>
