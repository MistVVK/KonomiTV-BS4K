<template>
    <div>
        <div class="settings__item">
            <div class="settings__item-heading">設定をエクスポート</div>
            <div class="settings__item-label">
                このデバイス (ブラウザ) に保存されている設定データを、エクスポート (ダウンロード) できます。<br>
                ダウンロードした設定データ (KonomiTV-Settings.json) は、[設定をインポート] からインポートできます。異なるサーバーの KonomiTV を同じ設定で使いたいときなどに使ってください。<br>
            </div>
        </div>
        <v-btn class="settings__save-button mt-4" variant="flat" @click="exportSettings()">
            <Icon icon="fa6-solid:download" class="mr-3" height="19px" />設定をエクスポート
        </v-btn>
        <div class="settings__item">
            <div class="settings__item-heading text-error-lighten-1">設定をインポート</div>
            <div class="settings__item-label">
                [設定をエクスポート] でダウンロードした設定データを、このデバイス (ブラウザ) にインポートできます。<br>
                マイリスト・視聴履歴を含めてインポートするかは、ボタンを押した後の確認ダイヤログで選択できます。<br>
                <strong class="text-error-lighten-1">設定をインポートすると、現在のデバイス設定はすべて上書きされます。元に戻すことはできません。</strong><br>
                <strong class="text-error-lighten-1">設定のデバイス間同期がオンのときは、同期が有効なすべてのデバイスに反映されます。</strong>十分ご注意ください。<br>
            </div>
            <v-file-input class="settings__item-form" color="primary" variant="outlined" hide-details
                label="設定データ (KonomiTV-Settings.json) を選択"
                :density="is_form_dense ? 'compact' : 'default'"
                accept="application/json"
                prepend-icon=""
                prepend-inner-icon="mdi-paperclip"
                v-model="import_settings_file">
            </v-file-input>
        </div>
        <v-btn class="settings__save-button bg-error mt-5" variant="flat" @click="openImportSettingsDialog()">
            <Icon icon="fa6-solid:upload" class="mr-3" height="19px" />設定をインポート
        </v-btn>
        <v-dialog max-width="560" v-model="import_settings_dialog">
            <v-card>
                <v-card-title class="import-settings-dialog__title d-flex justify-center font-weight-bold pt-6">マイリスト・視聴履歴もインポートしますか？</v-card-title>
                <v-card-text class="pt-2 pb-5">
                    マイリストと視聴履歴は、このサーバーの録画番組と紐づいています。<br>
                    別の KonomiTV サーバーの設定をインポートすると、保存されている録画番組が異なるため、身に覚えのない番組が表示されてしまいます。<br>
                    <p class="mt-2">
                        <strong>同じサーバーのバックアップから復元する際は「上書き」、別のサーバーへ設定を移行する際は「維持」を選択してください。</strong><br>
                    </p>
                    <div class="d-flex justify-center align-center mt-3">
                        <div class="mx-auto pr-md-4">
                            <div class="d-flex align-center flex-column flex-md-row">
                                <div class="d-inline-block text-center text-md-right mr-md-2" style="min-width: 150px;">インポートするデータ:</div> <strong>マイリスト {{import_settings_mylist_count}}件 / 視聴履歴 {{import_settings_watched_history_count}}件</strong>
                            </div>
                            <div class="d-flex align-center flex-column flex-md-row">
                                <div class="d-inline-block text-center text-md-right mr-md-2" style="min-width: 150px;">このデバイス:</div> <strong>マイリスト {{settingsStore.settings.mylist.length}}件 / 視聴履歴 {{settingsStore.settings.watched_history.length}}件</strong>
                            </div>
                        </div>
                    </div>
                </v-card-text>
                <div class="d-flex flex-column px-4 pb-6 import-settings-dialog">
                    <v-btn class="settings__save-button text-error-lighten-1" color="background-lighten-1" variant="flat"
                        @click="executeImport(true)">
                        <Icon icon="fluent:document-arrow-down-16-filled" class="mr-2" height="22px" />
                        マイリスト・視聴履歴を<br class="smartphone-vertical-only">上書きしてインポート
                    </v-btn>
                    <v-btn class="settings__save-button text-error-lighten-1 mt-3" color="background-lighten-1" variant="flat"
                        @click="executeImport(false)">
                        <Icon icon="fluent:document-checkmark-16-filled" class="mr-2" height="22px" />
                        マイリスト・視聴履歴は<br class="smartphone-vertical-only">維持してインポート
                    </v-btn>
                    <v-btn class="settings__save-button mt-3" color="background-lighten-1" variant="flat"
                        @click="import_settings_dialog = false">
                        <Icon icon="fluent:dismiss-16-filled" class="mr-2" height="22px" />
                        キャンセル
                    </v-btn>
                </div>
            </v-card>
        </v-dialog>
        <div class="settings__item">
            <div class="settings__item-heading text-error-lighten-1">設定を初期状態にリセット</div>
            <div class="settings__item-label">
                このデバイス (ブラウザ) に保存されている設定データを、初期状態のデフォルト値にリセットできます。<br>
                <strong class="text-error-lighten-1">設定をリセットすると、元に戻すことはできません。</strong><br>
                <strong class="text-error-lighten-1">設定のデバイス間同期がオンのときは、同期が有効なすべてのデバイスに反映されます。</strong>十分ご注意ください。<br>
            </div>
        </div>
        <v-btn class="settings__save-button bg-error mt-5" variant="flat" @click="resetSettings()">
            <Icon icon="material-symbols:device-reset-rounded" class="mr-2" height="23px" />設定をリセット
        </v-btn>
    </div>
</template>
<script lang="ts">

import { mapStores } from 'pinia';
import { defineComponent } from 'vue';

import Message from '@/message';
import useSettingsStore from '@/stores/SettingsStore';
import Utils from '@/utils';

export default defineComponent({
    name: 'Settings-SettingsData',
    data() {
        return {

            // フォームを小さくするかどうか
            is_form_dense: Utils.isSmartphoneHorizontal(),

            // 選択された設定データ (KonomiTV-Settings.json) が入る
            import_settings_file: null as File | null,

            // 設定インポートの確認ダイヤログを表示するか
            import_settings_dialog: false,

            // インポートする設定データに含まれるマイリスト・視聴履歴の件数 (確認ダイヤログでの表示用)
            import_settings_mylist_count: 0,
            import_settings_watched_history_count: 0,
        };
    },
    computed: {
        ...mapStores(useSettingsStore),
    },
    methods: {

        // 設定データをエクスポートする
        exportSettings() {

            // 設定データを JSON 化して取得
            const settings_json = JSON.stringify(this.settingsStore.settings, null, 4);

            // ダウンロードさせるために一旦 Blob にしてから、KonomiTV-Settings.json としてダウンロード
            const settings_json_blob = new Blob([settings_json], {type: 'application/json'});
            Utils.downloadBlobData(settings_json_blob, 'KonomiTV-Settings.json');
            Message.success('設定をエクスポートしました。');
        },

        // 設定インポートの確認ダイヤログを開く
        // インポート実行前に、マイリスト・視聴履歴を上書きするか維持するかをユーザーに選択させる
        async openImportSettingsDialog() {

            // 設定データが選択されていないときは実行しない
            if (this.import_settings_file === null) {
                Message.error('インポートする設定データを選択してください！');
                return;
            }

            // ダイヤログにマイリスト・視聴履歴の件数を表示するため、選択されたファイルを先読みしてパースする
            let parsed_settings: {[key: string]: any} = {};
            try {
                parsed_settings = JSON.parse(await this.import_settings_file.text());
            } catch (error) {
                Message.error('設定データが不正なため、インポートできませんでした。');
                return;
            }

            // 取り込むデータに含まれるマイリスト・視聴履歴の件数を取得する
            // 生の設定データなので、念のため配列かどうか確認してから件数を数える
            this.import_settings_mylist_count = Array.isArray(parsed_settings.mylist) ? parsed_settings.mylist.length : 0;
            this.import_settings_watched_history_count = Array.isArray(parsed_settings.watched_history) ? parsed_settings.watched_history.length : 0;

            // 確認ダイヤログを表示
            this.import_settings_dialog = true;
        },

        // 設定データをインポートする
        // include_environment_specific: マイリスト・視聴履歴などの環境固有値も上書きするか (false なら現在のデバイスの値を維持する)
        async executeImport(include_environment_specific: boolean) {

            // ダイヤログを閉じる
            this.import_settings_dialog = false;

            // ダイヤログを開いた時点でファイルは選択済みだが、念のため再確認する
            if (this.import_settings_file === null) {
                return;
            }

            // 設定データのインポートを実行
            const result = await this.settingsStore.importClientSettings(this.import_settings_file, include_environment_specific);
            if (result === true) {
                Message.success('設定をインポートしました。');
                window.setTimeout(() => this.$router.go(0), 500);  // 念のためリロード
            } else {
                Message.error('設定データが不正なため、インポートできませんでした。');
            }
        },

        // 設定データをリセットする
        async resetSettings() {
            await this.settingsStore.resetClientSettings();
            Message.success('設定をリセットしました。');
            window.setTimeout(() => this.$router.go(0), 500);  // 念のためリロード
        },
    }
});

</script>
<style lang="scss" scoped>

// 設定インポートの確認ダイヤログのタイトル
.import-settings-dialog__title {
    // タイトルが長くスマホ縦では 1 行に収まらないため、折り返しを許可して中央寄せする
    // (Vuetify の v-card-title はデフォルトで white-space: nowrap のため、明示的に上書きする)
    white-space: normal;
    text-align: center;
    line-height: 1.5;
}

// 設定インポートの確認ダイヤログ内のアクションボタン (Account.vue の設定競合ダイヤログと同じ見た目に揃える)
.import-settings-dialog {
    .v-btn {
        @include smartphone-vertical {
            height: 50px !important;
            text-align: left;
        }
        // スマホ縦のときだけボタン文言を 2 行に折り返す
        br.smartphone-vertical-only {
            display: none;
            @include smartphone-vertical {
                display: inline;
            }
        }
    }
}

</style>
