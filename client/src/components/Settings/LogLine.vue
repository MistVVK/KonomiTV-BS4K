<template>
    <template v-if="formatted.level !== null">{{ formatted.prefix }}<span class="log-line__level" :style="logLevelStyle">{{ formatted.level }}</span>:{{ formatted.rest }}</template>
    <template v-else>{{ formatted.rest }}</template>
</template>
<script lang="ts" setup>

import { computed } from 'vue';

import { formatLogLine } from '@/utils/LogLineUtils';

// ログ行を表示するコンポーネント
// ログ本文に含まれる任意の文字列 (ユーザー名など) が HTML として解釈される stored XSS を防ぐため、
// 色付け対象のログレベル部分だけを span 化し、本文はテキストノードとして表示する
const props = defineProps<{
    line: string;
}>();

// ログ行をログレベル (色付け対象) と本文へ分解する
const formatted = computed(() => formatLogLine(props.line));

// ログレベルの色定義 (CRITICAL は色を持たず、デフォルトの文字色のまま表示する)
const LOG_LEVEL_COLORS: Record<string, string> = {
    DEBUG: '#7cbfcb',
    INFO: '#aeca91',
    WARNING: '#e5cb95',
    ERROR: '#da8789',
};

// ログレベル文字列に対応する文字色の style を返す
const logLevelStyle = computed(() => {
    if (formatted.value.level === null) {
        return '';
    }
    const color = LOG_LEVEL_COLORS[formatted.value.level];
    return color !== undefined ? `color: ${color};` : '';
});

</script>
