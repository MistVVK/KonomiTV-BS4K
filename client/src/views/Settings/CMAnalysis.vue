<template>
    <SettingsBase>
        <h2 class="settings__heading">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:timeline-20-filled" width="25px" />
            <span class="ml-3">CM管理</span>
        </h2>
        <div class="settings__description">
            手動編集した KonomiTV-BS4K chapter YAML、または基本名方式の .chapter.txt を優先して同期します。<br>
            KonomiTV-BS4K の自動解析結果は録画横の YAML に保存します。<br>
            この設定はすべてのユーザーと端末で共有されます。
        </div>
        <div class="settings__content" :class="{'settings__content--loading': is_loading}">
            <div class="settings__item settings__item--switch">
                <label class="settings__item-heading" for="cm_analysis_enabled">新しい CM 解析を有効にする</label>
                <label class="settings__item-label" for="cm_analysis_enabled">
                    無効にしても、既存の chapter 読み込みや保存済みの CM 区間・ロゴ・履歴は維持されます。<br>
                    実行中の解析は停止しません。
                </label>
                <v-switch id="cm_analysis_enabled" class="settings__item-switch" color="primary" hide-details
                    v-model="settings.enabled" />
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">共有ロゴフォルダ</div>
                <div class="settings__item-label">
                    空欄時は KonomiTV-BS4K 内部フォルダを使用します。指定する場合はホスト側の絶対パスを入力してください。<br>
                    利用不能な場合に内部フォルダへ自動切り替えは行いません。
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" hide-details
                    placeholder="/absolute/host/path" v-model="logo_directory" />
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">CM 解析の除外ディレクトリ</div>
                <div class="settings__item-label">
                    1 行に 1 つ、ホスト側の絶対パスを入力します。通常の録画登録・再生・索引・サムネイルと既存 chapter の読込は除外されません。
                </div>
                <v-textarea class="settings__item-form" color="primary" variant="outlined" rows="5" hide-details
                    placeholder="/recordings/without-cm-analysis" v-model="excluded_directories_text" />
            </div>
            <v-btn class="settings__save-button mt-5" variant="flat" :loading="is_saving" @click="saveSettings()">
                <Icon icon="fluent:save-20-filled" class="mr-2" width="22px" />CM 解析設定を更新
            </v-btn>
            <v-divider class="mt-7"></v-divider>
            <div class="settings__item">
                <div class="settings__item-heading">共有ロゴの管理</div>
                <div class="settings__item-label">
                    単一ロゴの追加・無効化・SID/NID/TSID サービス割り当て・「ロゴなし」指定・missing 履歴を管理します。
                </div>
                <v-btn class="settings__save-button mt-4" variant="flat" to="/settings/server/cm-analysis/logos">
                    <Icon icon="fluent:image-stack-20-filled" class="mr-2" width="22px" />CMロゴ管理を開く
                </v-btn>
            </div>
        </div>
    </SettingsBase>
</template>

<script setup lang="ts">

import { onMounted, ref } from 'vue';

import Message from '@/message';
import CMAnalysis, { ICMAnalysisSettings } from '@/services/CMAnalysis';
import SettingsBase from '@/views/Settings/Base.vue';


const settings = ref<ICMAnalysisSettings>({enabled: false, logo_directory: null, excluded_directories: []});
const logo_directory = ref('');
const excluded_directories_text = ref('');
const is_loading = ref(true);
const is_saving = ref(false);

onMounted(async () => {
    const fetched_settings = await CMAnalysis.fetchSettings();
    if (fetched_settings !== null) {
        settings.value = fetched_settings;
        logo_directory.value = fetched_settings.logo_directory ?? '';
        excluded_directories_text.value = fetched_settings.excluded_directories.join('\n');
    }
    is_loading.value = false;
});

async function saveSettings(): Promise<void> {
    is_saving.value = true;
    const updated_settings: ICMAnalysisSettings = {
        enabled: settings.value.enabled,
        logo_directory: logo_directory.value.trim() || null,
        excluded_directories: excluded_directories_text.value.split('\n').map((path) => path.trim()).filter(Boolean),
    };
    if (await CMAnalysis.updateSettings(updated_settings)) {
        settings.value = updated_settings;
        Message.success('CM 解析設定を更新しました。');
    }
    is_saving.value = false;
}

</script>
